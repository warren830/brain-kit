"""Requirement keys and claims without Jira (tracker "none").

A claim is a branch `claim/<KEY>` holding one file, claim.json {key, owner, title, created_at}. Creating a branch
that already exists is refused by git itself, so two people can never claim the same key; the pre-receive hook
checks that claim.json names the person who pushes it. Keys are <req_prefix>-<n>; a new key takes the next free n.
"""
from __future__ import annotations

import json
import os
import re

from .common import ZERO_SHA, BrainError, git, git_out, iso, run, utcnow

CLAIM_PREFIX = "refs/heads/claim/"
CLAIM_FILE = "claim.json"


def ref(key) -> str:
    return CLAIM_PREFIX + key


def _has_origin(repo) -> bool:
    return git(repo, "remote", "get-url", "origin", check=False).returncode == 0


def claim_doc(key, owner, title) -> dict:
    return {"schema": 1, "key": key, "owner": owner, "title": title[:200], "created_at": iso(utcnow())}


def _commit(repo, doc) -> str:
    """A parentless commit whose tree is just claim.json (the work tree is not touched)."""
    blob = run(["git", "-C", repo, "hash-object", "-w", "--stdin"],
               input_text=json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n").stdout.strip()
    tree = run(["git", "-C", repo, "mktree"], input_text="100644 blob %s\t%s\n" % (blob, CLAIM_FILE)).stdout.strip()
    return run(["git", "-C", repo, "commit-tree", tree, "-m", "claim %s by %s" % (doc.get("key"), doc.get("owner"))]
               ).stdout.strip()


def taken_numbers(repo, prefix) -> set:
    rx = re.compile(r"refs/heads/claim/%s-([1-9][0-9]*)$" % re.escape(prefix))
    if _has_origin(repo):
        out = git_out(repo, "ls-remote", "origin", CLAIM_PREFIX + "*")
        names = [l.split("\t", 1)[1] for l in out.splitlines() if "\t" in l]
    else:
        names = git_out(repo, "for-each-ref", "--format=%(refname)", CLAIM_PREFIX).splitlines()
    return {int(m.group(1)) for m in (rx.search(n) for n in names) if m}


def _try_create(repo, key, sha) -> bool:
    """Create claim/<KEY> only if it does not exist. False when someone else got there first."""
    if _has_origin(repo):
        p = git(repo, "push", "-q", "origin", "%s:%s" % (sha, ref(key)), check=False)
        if p.returncode == 0:
            git(repo, "update-ref", ref(key), sha)
            return True
        msg = (p.stderr + p.stdout).lower()
        if "rejected" in msg or "already exists" in msg or "non-fast-forward" in msg or "fetch first" in msg:
            return False
        raise BrainError("could not push claim %s: %s" % (key, (p.stderr or p.stdout).strip()))
    return git(repo, "update-ref", ref(key), sha, ZERO_SHA, check=False).returncode == 0


def claim(repo, prefix, owner, title, key=None, retries=8) -> dict:
    """Claim `key`, or the next free <prefix>-<n>. Returns the claim document."""
    if key:
        doc = claim_doc(key, owner, title)
        if not _try_create(repo, key, _commit(repo, doc)):
            existing = read(repo, key)
            if existing and existing.get("owner") == owner:
                return existing
            raise BrainError("%s is already claimed by %s" % (key, (existing or {}).get("owner", "someone")))
        return doc
    n = max(taken_numbers(repo, prefix) or {0}) + 1
    for _ in range(retries):
        k = "%s-%d" % (prefix, n)
        doc = claim_doc(k, owner, title)
        if _try_create(repo, k, _commit(repo, doc)):
            return doc
        n += 1
    raise BrainError("could not find a free %s-<n> after %d tries" % (prefix, retries))


def read(repo, key):
    """The claim document for `key`, or None."""
    if _has_origin(repo):
        p = git(repo, "fetch", "-q", "origin", "+%s:refs/remotes/origin/claim/%s" % (ref(key), key), check=False)
        if p.returncode != 0:
            return None
        src = "refs/remotes/origin/claim/%s" % key
    else:
        src = ref(key)
    p = git(repo, "show", "%s:%s" % (src, CLAIM_FILE), check=False)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except ValueError:
        return None


def validate_doc(doc, key, owner=None) -> list:
    p = []
    if not isinstance(doc, dict):
        return ["claim.json is not an object"]
    if doc.get("key") != key:
        p.append("claim.json key %r != %s" % (doc.get("key"), key))
    if owner is not None and doc.get("owner") != owner:
        p.append("claim.json owner %r != %s" % (doc.get("owner"), owner))
    return p
