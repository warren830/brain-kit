"""Thin wrapper over AI-DLC Lean (installed in the project as .kiro/aidlc-lean/aidlc_lean.py).

brain-kit does not change Lean; it only calls its CLI and reads aidlc-state.md. The one exception is `brain install`
adding brain-kit's record folder to aidlc-docs/lean.json fingerprint_exclude (merge_fingerprint_exclude): additions
only, never a removal, and the person who runs it reviews and commits the file (it is human-owned).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile

from .common import BrainError, run

SCRIPT = os.path.join(".kiro", "aidlc-lean", "aidlc_lean.py")
DOCS = "aidlc-docs"
CONFIG = DOCS + "/lean.json"
TEMPLATE_STATUS = "Status: Inception: starting"
# Lean's DEFAULT_CONFIG fingerprint_exclude (aidlc_lean.py). A lean.json that sets the key replaces this list.
DEFAULT_FINGERPRINT_EXCLUDE = (DOCS + "/**", ".kiro/**")


def installed(repo) -> bool:
    return os.path.exists(os.path.join(repo, SCRIPT)) and os.path.exists(os.path.join(repo, DOCS, "lean.json"))


def call(repo, *args, check=True):
    if not installed(repo):
        raise BrainError("AI-DLC Lean is not installed here (run `brain install --lean <path>`)")
    return run([sys.executable, SCRIPT] + list(args), cwd=repo, check=check)


def install(repo, lean_src, extra=()):
    script = os.path.join(lean_src, "install.sh")
    if not os.path.exists(script):
        raise BrainError("not an AI-DLC Lean checkout (no install.sh): %s" % lean_src)
    return run(["bash", script, repo] + list(extra))


def fingerprint_exclude(cfg) -> list:
    """The fingerprint_exclude Lean uses for this lean.json content: its own list when it sets one, else Lean's
    default. BrainError when it is set but is not a list of strings (Lean would not read it the way it looks)."""
    if "fingerprint_exclude" not in cfg:
        return list(DEFAULT_FINGERPRINT_EXCLUDE)
    pats = cfg["fingerprint_exclude"]
    if not isinstance(pats, list) or not all(isinstance(p, str) for p in pats):
        raise BrainError("%s fingerprint_exclude is not a list of patterns" % CONFIG)
    return list(pats)


def merge_fingerprint_exclude(repo, patterns) -> list:
    """Add `patterns` (and Lean's own defaults, when lean.json's list leaves them out) to aidlc-docs/lean.json
    fingerprint_exclude. Never removes or reorders an entry, and leaves the rest of the file as it is (key order
    kept). Returns what was added ([] when nothing was missing). lean.json is human-owned: the caller tells the
    person to review and commit it. BrainError when lean.json is missing or cannot be read as Lean reads it."""
    path = os.path.join(repo, CONFIG)
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except OSError as e:
        raise BrainError("%s cannot be read: %s" % (CONFIG, e))
    except ValueError as e:
        raise BrainError("%s is not valid JSON: %s" % (CONFIG, e))
    if not isinstance(cfg, dict):
        raise BrainError("%s is not a JSON object" % CONFIG)
    have = fingerprint_exclude(cfg)
    added = []
    for p in list(DEFAULT_FINGERPRINT_EXCLUDE) + list(patterns):
        if p not in have and p not in added:
            added.append(p)
    if not added:
        return []
    cfg["fingerprint_exclude"] = have + added
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")     # as Lean's `init` writes it
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return added


def new_intent(repo, slug, title) -> str:
    """Create a Lean intent; return its folder name (yymmdd-slug)."""
    out = call(repo, "new", slug, "--title", title).stdout
    m = re.search(r"Created %s/([0-9]{6}-[a-z0-9-]+)" % DOCS, out)
    if not m:
        raise BrainError("could not read the new intent name from Lean:\n" + out)
    return m.group(1)


def state_path(repo, intent) -> str:
    return os.path.join(repo, DOCS, intent, "aidlc-state.md")


def next_steps(text: str) -> list:
    """Items under '### Next' in the Session notes."""
    m = re.search(r"^### Next\s*$(.*?)(?=^### |\Z)", text, re.M | re.S)
    if not m:
        return []
    items = []
    for line in m.group(1).splitlines():
        s = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
        if s:
            items.append(s)
    return items


def handover_problems(repo, intent) -> list:
    """Why aidlc-state.md is not yet a usable hand-over note."""
    p = state_path(repo, intent)
    if not os.path.exists(p):
        return ["%s is missing" % os.path.relpath(p, repo)]
    with open(p, encoding="utf-8") as f:
        return handover_text_problems(f.read())


def handover_text_problems(text: str) -> list:
    probs = []
    if TEMPLATE_STATUS in text:
        probs.append("Session notes still say '%s': update Status" % TEMPLATE_STATUS)
    if not next_steps(text):
        probs.append("Session notes have no '### Next' items")
    return probs
