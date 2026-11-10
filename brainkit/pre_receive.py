"""GitLab server-side pre-receive hook (a real gate: the bot cannot change it).

Claim branches (claim/<KEY>): only the person claim.json names may create, change or delete one; an intent's
text fields (intents.py: source, body, acs, refs, closed) must pass the Brain's content checks - all of them when the
claim is created, the ones a push changes after that (closing a claim filed under older rules still works).
Filed for others (待认领, "owner": null): created by the person filed_by names (a split child: only unowned, by the
owner of its parent); its owner may go from none to the person who pushes only (claiming changes nothing else); an
owner may release it back to none; nobody may take a claimed one. While nobody owns it, whoever filed it or the
parent's owner may change, close or delete it; a split child is closed, never deleted, and its `split` record does
not change while someone owns it. key, parent, unit, source and filed_by never change. Bots never claim.
Rules for run branches (<prefix>/<owner>/<JIRA>-<slug>):
  bot accounts   may push only branches under <prefix>/ (well-formed names), never delete or force-push,
                 never push tags, never change bot_forbidden_paths (gates/, project-agent.yaml, CI, ...) or the
                 kit's own files (KIT_PATHS, whatever the config says), and never change <runs_dir>/<KEY>/run.json
                 (only `brain start` and `brain handoff`, run by the owner, write it; the runner writes park.json and
                 its work next to it). Every bot commit is authored and committed as one of bot_emails.
  people         may push under <prefix>/ only to <prefix>/<their own username>/...; other branches are
                 left to GitLab's normal protection.

Install: link `bin/brain-pre-receive` as a server hook (custom_hooks/pre-receive or the global
server_hooks dir). Config: JSON at $BRAIN_HOOK_CONFIG (default /etc/brain-kit/pre-receive.json):
  {"default": {...}, "projects": {"group/repo": {...}}}   keys as in .brain/kit.json
bot_emails: when the config does not set them, those of .brain/kit.json on the project's default branch (what
brain-ci reads; the bot changes neither: main is protected, .brain/ is a forbidden path), else the kit's default.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from .common import JIRA_RE, ZERO_SHA, parse_branch

# The kit's own files (KIT_PATHS of kit/.kiro/brain/lean-hook.py): they decide what a run may do (AI-DLC Lean, its
# hooks, the agent specs, the steering and skills every session reads, the hook adapter, the kit config, gates/). The
# runner's sessions may not change them, and bot commits may not either, whatever a project's or the server's
# bot_forbidden_paths says.
KIT_PATHS = (".kiro/brain/", ".kiro/agents/", ".kiro/hooks/", ".kiro/aidlc-lean/", ".kiro/steering/", ".kiro/skills/",
             ".brain/kit.json", "gates/")

DEFAULT_CFG = {
    "branch_prefix": "ai",
    "default_branch": "main",
    "bot_users": ["brain-bot"],
    "bot_emails": ["brain-bot@noreply.invalid"],
    "bot_forbidden_paths": ["gates/", "project-agent.yaml", ".brain/", "CODEOWNERS", ".gitlab/", ".gitlab/CODEOWNERS",
                            "docs/CODEOWNERS", ".gitlab-ci.yml", "ci/", ".kiro/brain/", ".kiro/agents/",
                            ".kiro/hooks/", ".kiro/aidlc-lean/", ".kiro/steering/", ".kiro/skills/"],
    "runs_dir": "runs",
}


def run_record_path(path, runs_dir="runs") -> bool:
    """Is `path` a run's run.json (<runs_dir>/<KEY>/run.json, any key)?"""
    return re.match(r"^%s/[^/]+/run\.json$" % re.escape(str(runs_dir or "runs").strip("/")), path) is not None


def _git(*args, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True)


def _forbidden(path, rules) -> bool:
    for r in rules:
        if r.endswith("/"):
            if path.startswith(r):
                return True
        elif path == r:
            return True
    return False


def new_commits(old, new, default_branch, cwd=None) -> list:
    """Commits this push brings that are not already on the default branch."""
    args = ["rev-list", new]
    if old == ZERO_SHA:
        args += ["--not", "--all"]
    else:
        args.append("^" + old)
        if _git("rev-parse", "--verify", "-q", "refs/heads/" + default_branch, cwd=cwd).returncode == 0:
            args.append("^refs/heads/" + default_branch)
    p = _git(*args, cwd=cwd)
    if p.returncode != 0:
        raise RuntimeError("rev-list failed: " + p.stderr.strip())
    return [c for c in p.stdout.split() if c]


def _names(args, cwd):
    # plumbing: no rename detection unless asked (diff.renames is not read); said anyway, a rename is two paths
    p = _git("diff-tree", "-z", "--no-renames", "--no-commit-id", "--name-only", *args, cwd=cwd)
    if p.returncode != 0:
        raise RuntimeError("diff-tree failed: " + p.stderr.strip())
    return {x for x in p.stdout.split("\0") if x}


def changed_paths(commits, default_branch="main", cwd=None) -> set:
    """Paths each commit changes (NUL-separated, so odd file names are not quoted away).

    A merge is compared with every parent that is already on the default branch: a merge that keeps the branch's
    old gate files ('-s ours') shows up as a change against main. Other merges count what differs from all parents."""
    out = set()
    main_ref = "refs/heads/" + default_branch
    has_main = _git("rev-parse", "--verify", "-q", main_ref, cwd=cwd).returncode == 0
    for c in commits:
        parents = _git("rev-list", "--parents", "-n", "1", c, cwd=cwd).stdout.split()[1:]
        if len(parents) <= 1:
            out |= _names(["--root", "-r", c], cwd)
            continue
        out |= _names(["--cc", c], cwd)
        for par in parents:
            if has_main and _git("merge-base", "--is-ancestor", par, main_ref, cwd=cwd).returncode == 0:
                out |= _names(["-r", par, c], cwd)
    return out


def own_changes(commits, cwd=None) -> set:
    """Paths the commits themselves change: a merge counts only what differs from every parent (what the merge
    resolved), so merging the default branch into a run branch does not count the branch's own files as changed."""
    out = set()
    for c in commits:
        parents = _git("rev-list", "--parents", "-n", "1", c, cwd=cwd).stdout.split()[1:]
        out |= _names(["--root", "-r", c] if len(parents) <= 1 else ["--cc", c], cwd)
    return out


def committer_emails(commits, cwd=None) -> set:
    out = set()
    for c in commits:
        p = _git("log", "-1", "--format=%ce%x00%ae", c, cwd=cwd)
        out.update(x.strip().lower() for x in p.stdout.split("\0") if x.strip())
    return out


def is_ff(old, new, cwd=None) -> bool:
    return _git("merge-base", "--is-ancestor", old, new, cwd=cwd).returncode == 0


def _claim_owner(sha, cwd):
    p = subprocess.run(["git", "show", "%s:claim.json" % sha], cwd=cwd, capture_output=True)
    if p.returncode != 0:
        return None, "has no claim.json"
    try:
        doc = json.loads(p.stdout.decode("utf-8"))
    except ValueError:            # also UnicodeDecodeError: claim.json is UTF-8 whatever the server's locale
        return None, "claim.json is not JSON"
    return doc, ""


IMMUTABLE = ("key", "parent", "unit", "source", "filed_by")
_GONE = object()


def _ref_claim(key, cwd):
    """claim.json of claim/<key> as the server has it before this push (None when there is none)."""
    if not JIRA_RE.match(str(key or "")):
        return None
    doc, err = _claim_owner("refs/heads/claim/%s" % key, cwd)
    return doc if not err and isinstance(doc, dict) else None


def _may_manage(before, user, cwd) -> bool:
    """An unowned claim is managed by whoever filed it and by the owner of its parent."""
    if before.get("filed_by") == user:
        return True
    parent = _ref_claim(before.get("parent"), cwd) if before.get("parent") else None
    return bool(parent) and parent.get("owner") == user


def _unowned(doc) -> bool:
    return isinstance(doc, dict) and "owner" in doc and doc["owner"] is None


def _is_child(doc) -> bool:
    """A child of a split (brain split): it names a parent, a unit or a split, or has source split."""
    return isinstance(doc, dict) and (doc.get("source") == "split" or any(k in doc for k in ("parent", "unit",
                                                                                              "split")))


def claim_rules(old, new, branch, user, is_bot, cwd) -> list:
    """claim/<KEY>: created once by the person it names (or, filed for others, by the person filed_by names; a child
    of a split only unowned, by its parent's owner); an owner goes from none to the pusher only, back to none by the
    owner, and nobody takes a claimed one. A child is closed, never deleted, and its split record stays as filed
    while someone owns it."""
    key = branch[len("claim/"):]
    if not JIRA_RE.match(key):
        return ["%s: claim branches are claim/<PREFIX>-<n>" % branch]
    if is_bot:
        return ["%s: bot accounts may not claim requirements" % branch]
    before = None
    if old != ZERO_SHA:
        before, err = _claim_owner(old, cwd)
        holder = (before or {}).get("owner") if isinstance(before, dict) else None
        if not _unowned(before) and holder != user:
            return ["%s: claimed by %s; only they may change or release it" % (branch, holder or "unknown")]
        if new == ZERO_SHA and _is_child(before):
            return ["%s: a child of a split is closed with a reason (`brain intent close`), never deleted" % branch]
        if _unowned(before) and new == ZERO_SHA and not _may_manage(before, user, cwd):
            return ["%s: not claimed yet; only %s (who filed it) or the parent's owner may delete it"
                    % (branch, before.get("filed_by") or "who filed it")]
    if new == ZERO_SHA:
        return []
    doc, err = _claim_owner(new, cwd)
    if err:
        return ["%s: %s" % (branch, err)]
    if not isinstance(doc, dict) or doc.get("key") != key:
        return ["%s: claim.json must name key %s and owner %s" % (branch, key, user)]
    owner = doc.get("owner")
    if before is None:                                  # a new claim
        if "filed_by" in doc and doc.get("filed_by") != user:
            return ["%s: claim.json must name owner %s, or no owner and filed_by %s" % (branch, user, user)]
        if _is_child(doc):                              # only the parent's owner files its children, unowned
            parent = _ref_claim(doc.get("parent"), cwd)
            if not parent or parent.get("owner") != user:
                return ["%s: a child of %s is filed by %s's owner (%s), not by %s" % (
                    branch, doc.get("parent"), doc.get("parent"), (parent or {}).get("owner") or "nobody", user)]
            if not _unowned(doc) or doc.get("filed_by") != user:
                return ["%s: a child of a split is filed unowned: claim.json must have no owner and filed_by %s"
                        % (branch, user)]
        elif _unowned(doc):
            if doc.get("filed_by") != user:
                return ["%s: claim.json must name owner %s, or no owner and filed_by %s" % (branch, user, user)]
        elif owner != user:
            return ["%s: claim.json must name key %s and owner %s" % (branch, key, user)]
    else:
        changed = sorted(k for k in IMMUTABLE if doc.get(k, _GONE) != before.get(k, _GONE)
                         and not (k == "filed_by" and k not in before and _unowned(doc) and doc.get(k) == user))
        if changed:
            return ["%s: %s never change%s" % (branch, ", ".join(changed), "s" if len(changed) == 1 else "")]
        if not _unowned(before):                        # owned by the pusher (checked above)
            if _unowned(doc):                           # released: only the owner goes back to none
                rest = sorted(k for k in set(doc) | set(before) if k not in ("owner", "claimed_at", "filed_by")
                              and doc.get(k, _GONE) != before.get(k, _GONE))
                if rest:
                    return ["%s: a release changes only the owner (it also changed %s)" % (branch, ", ".join(rest))]
            elif owner != user:
                return ["%s: claimed by %s; nobody may take a claimed one (its owner may keep it or release it)"
                        % (branch, user)]
            elif doc.get("split", _GONE) != before.get("split", _GONE):
                # the split record says where the child came from (its parent's run branch): only the parent's side
                # changes it, while nobody owns the child
                return ["%s: split never changes while someone owns the claim" % branch]
        elif owner is None and "owner" in doc:          # still unowned: changed by who filed it or the parent's owner
            if not _may_manage(before, user, cwd):
                return ["%s: not claimed yet; only %s (who filed it) or the parent's owner may change it"
                        % (branch, before.get("filed_by") or "who filed it")]
        elif owner == user:                             # claimed: none -> the pusher, nothing else changes
            if before.get("closed"):
                return ["%s: closed; a closed requirement is not claimed" % branch]
            rest = sorted(k for k in set(doc) | set(before) if k not in ("owner", "claimed_at")
                          and doc.get(k, _GONE) != before.get(k, _GONE))
            if rest:
                return ["%s: claiming changes only the owner (it also changed %s)" % (branch, ", ".join(rest))]
        else:
            return ["%s: an owner may go from none to the person who pushes only (%s pushed owner %s)"
                    % (branch, user, owner if owner is not None else "missing")]
    from . import intents  # an intent's text reaches the run's agent: same checks as `brain intent add`
    # an update is checked for what it changes: text the claim was accepted with stays as it was filed
    bad = intents.doc_problems(doc, before=before if isinstance(before, dict) else None)
    if bad:
        return ["%s: claim.json: %s" % (branch, "; ".join(bad[:5]))]
    return []


def bot_path_problems(branch, commits, cfg, cwd=None) -> list:
    """The bot's path rules for these commits (what they change, not who made them): no bot_forbidden_paths and no
    KIT_PATHS (in any letter case: a checkout on a case-insensitive disk writes .KIRO/brain/x over .kiro/brain/x), no
    <runs_dir>/<KEY>/run.json. brain-ci push-rules applies them to the bot's commits of an MR too."""
    reasons = []
    paths = changed_paths(commits, cfg["default_branch"], cwd)
    rules = [r.casefold() for r in list(cfg.get("bot_forbidden_paths") or []) + list(KIT_PATHS)]
    bad = sorted(p for p in paths if _forbidden(p.casefold(), rules))
    if bad:
        reasons.append("%s: bot accounts may not change %s" % (branch, ", ".join(bad[:5])))
    recs = sorted(p for p in own_changes(commits, cwd) if run_record_path(p, cfg.get("runs_dir", "runs")))
    if recs:
        reasons.append("%s: bot accounts may not change %s (run.json is written by `brain start` and "
                       "`brain handoff` only)" % (branch, ", ".join(recs[:5])))
    return reasons


def decide(updates, user, cfg, cwd=None) -> list:
    """Return a list of denial reasons (empty = accept). updates: [(old, new, ref)]."""
    prefix = cfg["branch_prefix"]
    bots = {b.lower() for b in cfg["bot_users"]}
    user = (user or "").lower()
    is_bot = user in bots
    reasons = []
    for old, new, ref in updates:
        if not ref.startswith("refs/heads/"):
            if is_bot:
                reasons.append("%s: bot accounts may only push branches" % ref)
            continue
        branch = ref[len("refs/heads/"):]
        if branch.startswith("claim/"):
            reasons += claim_rules(old, new, branch, user, is_bot, cwd)
            continue
        under = branch.startswith(prefix + "/")
        b = parse_branch(branch, prefix) if under else None
        if is_bot:
            if not under:
                reasons.append("%s: bot accounts may only push %s/* branches" % (branch, prefix))
                continue
            if not b:
                reasons.append("%s: not %s/<owner>/<JIRA>-<slug>" % (branch, prefix))
                continue
            if new == ZERO_SHA:
                reasons.append("%s: bot accounts may not delete branches" % branch)
                continue
            if old != ZERO_SHA and not is_ff(old, new, cwd):
                reasons.append("%s: bot accounts may not force-push" % branch)
                continue
            commits = new_commits(old, new, cfg["default_branch"], cwd)
            reasons += bot_path_problems(branch, commits, cfg, cwd)
            emails = {e.lower() for e in cfg.get("bot_emails", [])}
            alien = sorted(committer_emails(commits, cwd) - emails)
            if alien:
                reasons.append("%s: bot commits must be authored and committed as %s (found %s)"
                               % (branch, ", ".join(sorted(emails)), ", ".join(alien[:3])))
        elif under:
            if not b:
                reasons.append("%s: run branches are %s/<you>/<JIRA>-<slug>" % (branch, prefix))
            elif b["owner"] != user:
                reasons.append("%s: %s/%s/* belongs to %s" % (branch, prefix, b["owner"], b["owner"]))
    return reasons


def kit_bot_emails(default_branch, cwd=None) -> list:
    """bot_emails of .brain/kit.json on the default branch as the server has it before this push ([] when it has no
    such list): the addresses brain-ci recognises the bot by."""
    p = subprocess.run(["git", "show", "refs/heads/%s:.brain/kit.json" % default_branch], cwd=cwd,
                       capture_output=True)
    if p.returncode != 0:
        return []
    try:
        doc = json.loads(p.stdout.decode("utf-8"))
    except ValueError:            # also UnicodeDecodeError
        return []
    emails = doc.get("bot_emails") if isinstance(doc, dict) else None
    return [e for e in emails if isinstance(e, str) and e.strip()] if isinstance(emails, list) else []


def load_cfg(project_path, cwd=None) -> dict:
    """The hook's config for this project. bot_emails the config does not set come from the project's kit.json on the
    default branch (kit_bot_emails), so the hook and brain-ci recognise the same bot commits."""
    path = os.environ.get("BRAIN_HOOK_CONFIG", "/etc/brain-kit/pre-receive.json")
    cfg = dict(DEFAULT_CFG)
    own = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        own.update(data.get("default", {}))
        own.update(data.get("projects", {}).get(project_path or "", {}))
    cfg.update(own)
    if "bot_emails" not in own:
        cfg["bot_emails"] = kit_bot_emails(cfg["default_branch"], cwd) or cfg["bot_emails"]
    return cfg


def main(stdin=None) -> int:
    stdin = stdin or sys.stdin
    updates = []
    for line in stdin:
        parts = line.split()
        if len(parts) == 3:
            updates.append(tuple(parts))
    user = os.environ.get("GL_USERNAME", "")
    try:
        cfg = load_cfg(os.environ.get("GL_PROJECT_PATH", ""))
        reasons = decide(updates, user, cfg)
    except Exception as e:  # fail closed
        print("GL-HOOK-ERR: brain pre-receive could not check this push: %s" % e)
        return 1
    for r in reasons:
        print("GL-HOOK-ERR: %s" % r)
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
