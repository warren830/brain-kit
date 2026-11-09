#!/usr/bin/env python3
"""KiroCrew -> AI-DLC Lean hook adapter (installed by brain-kit; Lean itself is unchanged).

KiroCrew does not read a project's .kiro/hooks/ (v3 workspace hooks). It fires the hooks written in an agent's spec
instead, in the session's workspace, with the session id as `acp_session_id` / $KIRO_SESSION_ID. Lean reads
`session_id`. This fills that in and runs Lean's own hook, passing its output and exit code straight through
(exit 2 on PreToolUse blocks the tool).

One write does not go to Lean: the run's own question, runs/<KEY>/park.json (park_target). It is how a run reaches a
person, so it must be writable in every approval state (before REQUIREMENTS, before DESIGN, with PLAN pending, after
a rejection), while Lean gates every file outside aidlc-docs/, .kiro/ and free_paths like product code. Only a
file-write tool whose single target is exactly <runs_dir>/<KEY>/park.json of this branch's run is let through: KEY
from the branch name <prefix>/<owner>/<KEY>-<slug>, confirmed by <runs_dir>/<KEY>/run.json naming this branch (or,
when the branch name has no key, the one run.json that names it). Anything else, and anything this cannot tell
(another key, run.json, any other runs/ file, '..', a symlink or hard link, a path outside the workspace, a shell
command, an unknown tool or tool input), goes to Lean unchanged. run.json itself is guarded against the bot by the
pre-receive hook and CI, not here. runs/<KEY>/MR.md needs no allowance: `brain mr-desc --write` writes it through the
shell, and Lean's shell guard does not gate that command.
"""
import json
import os
import re
import stat
import subprocess
import sys

# brain doctor and brain handoff check this number (2: the park.json allowance below). Bump it when the adapter
# changes in a way the team runner depends on.
ADAPTER_VERSION = 2

# File-write tools (Kiro / KiroCrew names). Shell, delegation and unknown tools never take the allowance.
WRITE_TOOLS = frozenset(("fs_write", "write", "write_file", "create_file", "str_replace", "strReplace", "fsWrite",
                         "edit", "edit_file"))
PATH_KEYS = ("path", "file_path", "filePath")
CONTENT_KEYS = frozenset(("command", "text", "file_text", "fileText", "content", "contents", "new_str", "old_str",
                          "newStr", "oldStr", "new_text", "old_text", "newText", "oldText", "insert_line", "insertLine",
                          "summary", "explanation"))
WRITE_COMMANDS = frozenset(("create", "str_replace", "strReplace", "insert", "append", "write", "overwrite"))
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}$")
BRANCH_RE = re.compile(r"^[a-z]+/[a-z0-9][a-z0-9._-]{0,63}/([A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6})"
                       r"(?:-[a-z0-9][a-z0-9-]{0,39})?$")   # brainkit.common.BRANCH_RE
PART_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


def _runs_dir(root):
    """runs_dir from .brain/kit.json (default "runs") as path parts, or None when it is not a plain relative path."""
    value = "runs"
    p = os.path.join(root, ".brain", "kit.json")
    if os.path.lexists(p):
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
        value = cfg.get("runs_dir", "runs") if isinstance(cfg, dict) else None
    if not isinstance(value, str):
        return None
    parts = value.strip("/").split("/")
    return parts if all(PART_RE.match(x) and x not in (".", "..") for x in parts) else None


def _regular(path):
    """A regular file, not a symlink, with one link (a hard link would let a write change another file)."""
    st = os.lstat(path)
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def _run_branch(root, runs, key, branch):
    """Does <runs_dir>/<key>/run.json name this key and branch?"""
    p = os.path.join(root, *(runs + [key, "run.json"]))
    if not os.path.lexists(p) or not _regular(p) or os.path.realpath(p) != os.path.join(
            os.path.realpath(root), *(runs + [key, "run.json"])):
        return False
    with open(p, encoding="utf-8") as f:
        rec = json.load(f)
    return isinstance(rec, dict) and rec.get("key") == key and rec.get("branch") == branch


def run_key(root, runs):
    """This workspace's run KEY, or None when it cannot be told."""
    p = subprocess.run(["git", "-C", root, "symbolic-ref", "-q", "--short", "HEAD"], capture_output=True, text=True,
                       timeout=20)
    branch = p.stdout.strip() if p.returncode == 0 else ""
    if not branch:
        return None
    m = BRANCH_RE.match(branch)
    if m:
        return m.group(1) if _run_branch(root, runs, m.group(1), branch) else None
    base = os.path.join(root, *runs)
    keys = [k for k in (sorted(os.listdir(base)) if os.path.isdir(base) else []) if KEY_RE.match(k)]
    found = [k for k in keys if _run_branch(root, runs, k, branch)]
    return found[0] if len(found) == 1 else None


def park_target(root, payload):
    """True when this PreToolUse is a file write whose only target is this run's <runs_dir>/<KEY>/park.json."""
    tool = payload.get("tool_name")
    tin = payload.get("tool_input")
    if tool not in WRITE_TOOLS or not isinstance(tin, dict) or not tin:
        return False
    if any(k not in PATH_KEYS and k not in CONTENT_KEYS for k in tin):
        return False
    paths = [tin[k] for k in PATH_KEYS if k in tin]
    if not paths or any(not isinstance(x, str) or not x for x in paths) or len(set(paths)) != 1:
        return False
    if "command" in tin and tin["command"] not in WRITE_COMMANDS:
        return False
    runs = _runs_dir(root)
    if not runs:
        return False
    given = paths[0]
    if os.path.isabs(given):
        for r in {root, os.path.realpath(root)}:
            if given.startswith(r.rstrip(os.sep) + os.sep):
                given = given[len(r.rstrip(os.sep)) + 1:]
                break
        else:
            return False
    parts = given.split("/")
    if len(parts) != len(runs) + 2 or parts[:len(runs)] != runs or parts[-1] != "park.json" \
            or not KEY_RE.match(parts[-2]):
        return False
    want = os.path.join(os.path.realpath(root), *parts)
    if os.path.realpath(os.path.join(root, *parts)) != want:
        return False                                    # a symlinked runs/, runs/<KEY>/ or park.json
    if os.path.lexists(want) and not _regular(want):
        return False
    return run_key(root, runs) == parts[-2]


event = sys.argv[1] if len(sys.argv) > 1 else ""
raw = sys.stdin.read() if not sys.stdin.isatty() else ""
try:
    payload = json.loads(raw) if raw.strip() else {}
except ValueError:
    payload = {}
if not isinstance(payload, dict):
    payload = {}
root = payload.get("cwd") or os.getcwd()
lean = os.path.join(root, ".kiro", "aidlc-lean", "aidlc_lean.py")
if not os.path.exists(lean):
    sys.stderr.write("[brain-kit] no AI-DLC Lean in %s; nothing to check\n" % root)
    sys.exit(0)
if not payload.get("session_id"):
    sid = payload.get("acp_session_id") or os.environ.get("KIRO_SESSION_ID") or payload.get("session_key")
    if sid:
        payload["session_id"] = sid
if event == "PreToolUse":
    try:
        allowed = park_target(root, payload)
    except Exception:  # noqa: BLE001  fail closed: whatever cannot be told goes to Lean
        allowed = False
    if allowed:
        sys.exit(0)
p = subprocess.run([sys.executable, lean, "hook", event], input=json.dumps(payload), text=True,
                   capture_output=True, cwd=root)
sys.stdout.write(p.stdout)
sys.stderr.write(p.stderr)
sys.exit(p.returncode)
