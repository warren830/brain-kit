#!/usr/bin/env python3
"""KiroCrew -> AI-DLC Lean hook adapter (installed by brain-kit; Lean itself is unchanged).

KiroCrew does not read a project's .kiro/hooks/ (v3 workspace hooks). It fires the hooks written in an agent's spec
instead, in the session's workspace, with the session id as `acp_session_id` / $KIRO_SESSION_ID. Lean reads
`session_id`. This fills that in and runs Lean's own hook, passing its output and exit code straight through
(exit 2 on PreToolUse blocks the tool).
"""
import json
import os
import subprocess
import sys

event = sys.argv[1] if len(sys.argv) > 1 else ""
raw = sys.stdin.read() if not sys.stdin.isatty() else ""
try:
    payload = json.loads(raw) if raw.strip() else {}
except ValueError:
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
p = subprocess.run([sys.executable, lean, "hook", event], input=json.dumps(payload), text=True,
                   capture_output=True, cwd=root)
sys.stdout.write(p.stdout)
sys.stderr.write(p.stderr)
sys.exit(p.returncode)
