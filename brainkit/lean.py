"""Thin wrapper over AI-DLC Lean (installed in the project as .kiro/aidlc-lean/aidlc_lean.py).

brain-kit does not change Lean; it only calls its CLI and reads aidlc-state.md.
"""
from __future__ import annotations

import os
import re
import sys

from .common import BrainError, run

SCRIPT = os.path.join(".kiro", "aidlc-lean", "aidlc_lean.py")
DOCS = "aidlc-docs"
TEMPLATE_STATUS = "Status: Inception: starting"


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
