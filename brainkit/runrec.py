"""The run record: `runs/<KEY>/run.json`, committed on the run branch.

It carries numbers and hashes only (no requirement text). Lean's
`aidlc-docs/<intent>/aidlc-state.md` is the hand-over note; run.json points to it.
KEY is a Jira key (tracker "jira") or a claimed <req_prefix>-<n> (tracker "none").
"""
from __future__ import annotations

import os
import re

from .common import JIRA_RE, SHA_RE, USER_RE, parse_branch

SCHEMA = 1
ORIGINS = ("local", "remote")
PKG_RE = re.compile(r"^PKG-[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}-[1-9][0-9]{0,4}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
INTENT_RE = re.compile(r"^[0-9]{6}-[a-z0-9][a-z0-9-]{0,80}$")   # Lean: yymmdd-slug
PATH_RE = re.compile(r"^[A-Za-z0-9_./*-][A-Za-z0-9_./* -]{0,199}$")


def path(runs_dir: str, key: str) -> str:
    return os.path.join(runs_dir, key, "run.json")


def new(key, owner, branch, intent, brain_commit, kit_version, expected_paths, created_at, claim_checked,
        tracker="none", materials=()):
    return {
        "schema": SCHEMA,
        "key": key,
        "tracker": tracker,
        "owner": owner,
        "branch": branch,
        "intent": intent,
        "brain_commit": brain_commit,
        "kit_version": kit_version,
        "origin": "local",
        "expected_paths": list(expected_paths),
        "materials": list(materials),
        "created_at": created_at,
        "claim_checked": bool(claim_checked),
        "handoff": None,
    }


def validate(rec, branch=None, prefix="ai") -> list:
    """Problems with a run record; with `branch`, also check it matches the branch name."""
    if not isinstance(rec, dict):
        return ["run.json is not an object"]
    p = []
    if rec.get("schema") != SCHEMA:
        p.append("schema must be %d" % SCHEMA)
    if not JIRA_RE.match(str(rec.get("key", ""))):
        p.append("key is not like OMS-12")
    if not USER_RE.match(str(rec.get("owner", ""))):
        p.append("owner is not a username")
    if not SHA_RE.match(str(rec.get("brain_commit", ""))):
        p.append("brain_commit is not a full commit sha")
    if not INTENT_RE.match(str(rec.get("intent", ""))):
        p.append("intent must be a Lean intent folder name (yymmdd-slug)")
    if rec.get("origin") not in ORIGINS:
        p.append("origin must be one of %s" % (ORIGINS,))
    eps = rec.get("expected_paths", [])
    if not isinstance(eps, list) or len(eps) > 50 or not all(isinstance(x, str) and PATH_RE.match(x) and ".." not in x
                                                           for x in eps):
        p.append("expected_paths must be a list of up to 50 repo paths")
    mats = rec.get("materials", [])
    if not isinstance(mats, list):
        p.append("materials must be a list")
    else:
        for m in mats:
            if not isinstance(m, dict) or not PKG_RE.match(str(m.get("package", ""))) \
                    or not HEX64_RE.match(str(m.get("manifest_sha256", ""))):
                p.append("materials entries need package PKG-<KEY>-<n> and manifest_sha256")
            elif not m["package"].startswith("PKG-%s-" % rec.get("key")):
                p.append("material %s is not for %s" % (m["package"], rec.get("key")))
    b = parse_branch(str(rec.get("branch", "")), prefix)
    if not b:
        p.append("branch is not %s/<owner>/<KEY>-<slug>" % prefix)
    elif b["owner"] != rec.get("owner") or b["key"] != rec.get("key"):
        p.append("branch does not match owner/key")
    if branch is not None and rec.get("branch") != branch:
        p.append("run.json is for branch %r, not %r" % (rec.get("branch"), branch))
    h = rec.get("handoff")
    if h is not None:
        if not isinstance(h, dict) or not h.get("requested_at") or h.get("by") != rec.get("owner"):
            p.append("handoff must have requested_at and by == owner")
    return p
