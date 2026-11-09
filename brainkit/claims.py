"""Requirement keys and claims without Jira (tracker "none").

A claim is a branch `claim/<KEY>` holding one file, claim.json {key, owner, title, created_at}. Creating a branch
that already exists is refused by git itself, so two people can never claim the same key; the pre-receive hook
checks that claim.json names the person who pushes it. Keys are <req_prefix>-<n>; a new key takes the next free n.
An intent (intents.py) is a claim with more, optional fields (source, body, acs, refs, hash, closed); old claims
without them still read. Only the owner changes a claim, with a fast-forward commit (`update`).

Unowned (待认领): `owner` null and `filed_by` the person who filed it (`brain split` files a big requirement's
children this way). `take` moves the owner from none to the person who pushes, `release` back to none. Both are a
fast-forward of the tip that was read, so when two people take the same claim the first push wins and the second
is refused by git (and by the pre-receive hook: nobody takes a claimed one).
"""
from __future__ import annotations

import json
import os
import re

from .common import ZERO_SHA, BrainError, git, git_out, iso, run, utcnow

CLAIM_PREFIX = "refs/heads/claim/"
CLAIM_FILE = "claim.json"
MAX_DIGITS = 7          # a key's number: 1 to 9999999 (common.JIRA_RE)


def ref(key) -> str:
    return CLAIM_PREFIX + key


def _has_origin(repo) -> bool:
    return git(repo, "remote", "get-url", "origin", check=False).returncode == 0


def claim_doc(key, owner, title, extra=None) -> dict:
    """owner None: filed for others (待认领); extra must then carry filed_by."""
    doc = {"schema": 1, "key": key, "owner": owner, "title": title[:200], "created_at": iso(utcnow())}
    doc.update(extra or {})
    return doc


def _message(doc, parent, what=None) -> str:
    if what:
        return "%s %s by %s" % (what, doc.get("key"), doc.get("owner") or doc.get("filed_by"))
    if doc.get("owner") is None and "owner" in doc:
        return "file %s by %s (unclaimed)" % (doc.get("key"), doc.get("filed_by"))
    return "claim %s by %s" % (doc.get("key"), doc.get("owner"))


def _commit(repo, doc, parent=None, what=None) -> str:
    """A commit whose tree is just claim.json (the work tree is not touched); parentless for a new claim."""
    blob = run(["git", "-C", repo, "hash-object", "-w", "--stdin"],
               input_text=json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n").stdout.strip()
    tree = run(["git", "-C", repo, "mktree"], input_text="100644 blob %s\t%s\n" % (blob, CLAIM_FILE)).stdout.strip()
    return run(["git", "-C", repo, "commit-tree", tree] + (["-p", parent] if parent else []) +
               ["-m", _message(doc, parent, what)]).stdout.strip()


def taken_numbers(repo, prefix) -> set:
    rx = re.compile(r"refs/heads/claim/%s-([1-9][0-9]*)$" % re.escape(prefix))
    if _has_origin(repo):
        out = git_out(repo, "ls-remote", "origin", CLAIM_PREFIX + "*")
        names = [l.split("\t", 1)[1] for l in out.splitlines() if "\t" in l]
    else:
        names = git_out(repo, "for-each-ref", "--format=%(refname)", CLAIM_PREFIX).splitlines()
    # a hand-made ref or runs/ folder with more than 7 digits (or a leading zero) is not a key: it must not push
    # the next key out of the range every command takes
    taken = {int(m.group(1)) for m in (rx.search(n) for n in names) if m and len(m.group(1)) <= MAX_DIGITS}
    run_rx = re.compile(r"^runs/%s-([1-9][0-9]*)/" % re.escape(prefix))
    for tree in ("HEAD", "refs/remotes/origin/main"):
        p = git(repo, "ls-tree", "-r", "--name-only", tree, "--", "runs", check=False)
        taken |= {int(m.group(1)) for m in (run_rx.match(l) for l in p.stdout.splitlines())
                  if m and len(m.group(1)) <= MAX_DIGITS}
    return taken


def _try_create(repo, key, sha) -> bool:
    """Create claim/<KEY> only if it does not exist. False when someone else got there first."""
    if _has_origin(repo):
        p = git(repo, "push", "-q", "origin", "%s:%s" % (sha, ref(key)), check=False)
        if p.returncode == 0:
            git(repo, "update-ref", ref(key), sha)
            return True
        msg = (p.stderr + p.stdout).lower()
        # someone else's push created it between git's ref check and its lock ("remote rejected ... (reference
        # already exists)"), or before: taken, the caller tries the next key
        if "reference already exists" in msg or \
                git(repo, "ls-remote", "--exit-code", "origin", ref(key), check=False).returncode == 0:
            return False
        if "remote rejected" in msg or "hook declined" in msg:      # the server refused this claim: not "taken"
            raise BrainError("could not push claim %s: %s" % (key, (p.stderr or p.stdout).strip()))
        if "rejected" in msg or "already exists" in msg or "non-fast-forward" in msg or "fetch first" in msg:
            return False
        raise BrainError("could not push claim %s: %s" % (key, (p.stderr or p.stdout).strip()))
    return git(repo, "update-ref", ref(key), sha, ZERO_SHA, check=False).returncode == 0


def claim(repo, prefix, owner, title, key=None, retries=8, extra=None) -> dict:
    """Claim `key`, or the next free <prefix>-<n>. Returns the claim document. `extra`: intent fields (intents.py)."""
    if key:
        if not re.match(r"^%s-[1-9][0-9]{0,6}$" % re.escape(prefix), str(key)):
            raise BrainError("%r is not a requirement key for this repo: %s-<n>, n from 1 without leading zeros, "
                             "at most 7 digits" % (key, prefix))
        doc = claim_doc(key, owner, title, extra)
        if not _try_create(repo, key, _commit(repo, doc)):
            existing = read(repo, key)
            if existing and owner is not None and existing.get("owner") == owner:
                return existing
            if unowned(existing):
                raise BrainError("%s is already filed and not claimed yet (待认领): `brain claim %s` takes it"
                                 % (key, key))
            raise BrainError("%s is already claimed by %s" % (key, (existing or {}).get("owner") or "someone"))
        return doc
    n = max(taken_numbers(repo, prefix) or {0}) + 1
    for _ in range(retries):
        if n >= 10 ** MAX_DIGITS:
            raise BrainError("no free %s-<n> with at most %d digits left" % (prefix, MAX_DIGITS))
        k = "%s-%d" % (prefix, n)
        doc = claim_doc(k, owner, title, extra)
        if _try_create(repo, k, _commit(repo, doc)):
            return doc
        n += 1
    raise BrainError("could not find a free %s-<n> after %d tries" % (prefix, retries))


def _tip(repo, key):
    """The ref with claim/<KEY> as the server has it (fetched first), or None."""
    if _has_origin(repo):
        p = git(repo, "fetch", "-q", "origin", "+%s:refs/remotes/origin/claim/%s" % (ref(key), key), check=False)
        if p.returncode != 0:
            return None
        return "refs/remotes/origin/claim/%s" % key
    return ref(key)


def read(repo, key):
    """The claim document for `key`, or None."""
    src = _tip(repo, key)
    if src is None:
        return None
    p = git(repo, "show", "%s:%s" % (src, CLAIM_FILE), check=False)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except ValueError:
        return None


def update(repo, key, doc, what=None, expect=None) -> str:
    """Replace claim.json on claim/<KEY> with a fast-forward commit. The pre-receive hook lets only the owner (an
    unowned claim: the person who filed it, or the owner of its parent). `expect`: the tip `doc` was made from
    (_tip_doc); when the claim moved since, nothing is written (someone else changed it: read it again)."""
    src = _tip(repo, key)
    old = git(repo, "rev-parse", "--verify", "-q", src + "^{commit}", check=False).stdout.strip() if src else ""
    if not old:
        raise BrainError("claim/%s not found" % key)
    if expect and old != expect:
        raise BrainError("claim/%s changed since it was read (someone else pushed it): nothing written; try again"
                         % key)
    sha = _commit(repo, doc, parent=old, what=what)
    ok, why = _push_update(repo, key, old, sha)
    if not ok:
        raise BrainError("could not update claim %s: %s" % (key, why) if why else
                         "claim/%s changed meanwhile; try again" % key)
    return sha


def _push_update(repo, key, old, sha) -> tuple:
    """(True, "") when claim/<KEY> moved from `old` to `sha` (a fast-forward: with an origin the server refuses it
    when someone else moved the claim first; locally update-ref compares and swaps). (False, why) otherwise; why
    is "" when the claim moved meanwhile."""
    if _has_origin(repo):
        p = git(repo, "push", "-q", "origin", "%s:%s" % (sha, ref(key)), check=False)
        if p.returncode != 0:
            msg = (p.stderr + p.stdout).lower()
            if "rejected" in msg and ("non-fast-forward" in msg or "fetch first" in msg or "stale info" in msg):
                return False, ""
            return False, (p.stderr or p.stdout).strip()
        git(repo, "update-ref", "refs/remotes/origin/claim/%s" % key, sha)
        git(repo, "update-ref", ref(key), sha)
        return True, ""
    return git(repo, "update-ref", ref(key), sha, old, check=False).returncode == 0, ""


def _tip_doc(repo, key) -> tuple:
    """(tip sha, claim document) of claim/<KEY> as the server has it (fetched first); (None, None) when there is
    none."""
    src = _tip(repo, key)
    old = git(repo, "rev-parse", "--verify", "-q", src + "^{commit}", check=False).stdout.strip() if src else ""
    if not old:
        return None, None
    p = git(repo, "show", "%s:%s" % (old, CLAIM_FILE), check=False)
    try:
        doc = json.loads(p.stdout) if p.returncode == 0 else None
    except ValueError:
        doc = None
    return old, doc


def unowned(doc) -> bool:
    """Filed for others and not taken yet (待认领): owner is null (old claims always name one)."""
    return isinstance(doc, dict) and "owner" in doc and doc["owner"] is None


def take(repo, key, me) -> dict:
    """Make an unowned claim mine: owner none -> me, one fast-forward of the tip read here. The first push wins:
    when someone took it in between, git refuses ours and the error names them. Returns the new document."""
    old, doc = _tip_doc(repo, key)
    if not old:
        raise BrainError("%s is not filed (no claim/%s)" % (key, key))
    if not isinstance(doc, dict) or doc.get("key") != key:
        raise BrainError("claim/%s has no readable claim.json" % key)
    if doc.get("closed"):
        raise BrainError("%s is closed (%s)" % (key, (doc["closed"] or {}).get("reason", "")))
    if doc.get("owner") == me:
        return doc
    if not unowned(doc):
        raise BrainError("%s is claimed by %s" % (key, doc.get("owner") or "someone"))
    new = dict(doc, owner=me, claimed_at=iso(utcnow()))
    ok, why = _push_update(repo, key, old, _commit(repo, new, parent=old, what="claim"))
    if ok:
        return new
    _, now = _tip_doc(repo, key)
    holder = (now or {}).get("owner")
    if holder and holder != me:
        raise BrainError("%s was just claimed by %s: their claim reached the server first" % (key, holder))
    raise BrainError("could not claim %s: %s" % (key, why or "it changed meanwhile; try again"))


def release(repo, key, me) -> dict:
    """Give my claim back (owner -> none): it is 待认领 again. The person who filed it (or, for an old claim, me)
    stays filed_by."""
    old, doc = _tip_doc(repo, key)
    if not old or not isinstance(doc, dict):
        raise BrainError("%s is not filed (no claim/%s)" % (key, key))
    if doc.get("closed"):
        raise BrainError("%s is closed (%s)" % (key, (doc["closed"] or {}).get("reason", "")))
    if unowned(doc):
        return doc
    if doc.get("owner") != me:
        raise BrainError("%s is claimed by %s; only they may release it" % (key, doc.get("owner") or "someone"))
    new = {k: v for k, v in doc.items() if k != "claimed_at"}
    new.update(owner=None, filed_by=doc.get("filed_by") or me)
    ok, why = _push_update(repo, key, old, _commit(repo, new, parent=old, what="release"))
    if not ok:
        raise BrainError("could not release %s: %s" % (key, why or "it changed meanwhile; try again"))
    return new


def validate_doc(doc, key, owner=None) -> list:
    p = []
    if not isinstance(doc, dict):
        return ["claim.json is not an object"]
    if doc.get("key") != key:
        p.append("claim.json key %r != %s" % (doc.get("key"), key))
    if owner is not None and doc.get("owner") != owner:
        p.append("claim.json owner %r != %s" % (doc.get("owner"), owner))
    return p
