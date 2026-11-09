"""Local half against a real AI-DLC Lean checkout: install -> doctor -> start -> handoff -> CI check."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import ci, cli, lean, sign  # noqa: E402
from brainkit.common import load_json, write_json  # noqa: E402
from tests.helpers import LEAN_SRC, commit_file, git_repo, keypair, sh, tmpdir  # noqa: E402


FAKE_KIRO_CLI = """#!/bin/sh
case "$1" in
  --version) echo "kiro-cli 0.0.0-fake"; exit 0 ;;
esac
echo "fake kiro-cli: $* is not faked" >&2
exit 1
"""
FAKE_KIROCREW = """#!/bin/sh
case "$*" in
  --version) echo "kirocrew 0.0.0-fake"; exit 0 ;;
  "config get agent.acp_backend") echo "kas"; exit 0 ;;
esac
echo "fake kirocrew: $* is not faked" >&2
exit 1
"""


def fake_tools(test):
    """kiro-cli and kirocrew stand-ins first on PATH: doctor's results do not depend on this machine, and nothing
    reaches a live KiroCrew."""
    d = tmpdir(test)
    for name, text in (("kiro-cli", FAKE_KIRO_CLI), ("kirocrew", FAKE_KIROCREW)):
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write(text)
        os.chmod(p, 0o755)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + old
    test.addCleanup(os.environ.__setitem__, "PATH", old)
    return d


def brain(repo, *args):
    out, err = io.StringIO(), io.StringIO()
    cwd = os.getcwd()
    os.chdir(repo)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(args))
    finally:
        os.chdir(cwd)
    return rc, out.getvalue() + err.getvalue()


@unittest.skipUnless(os.path.exists(os.path.join(LEAN_SRC, "install.sh")), "AI-DLC Lean checkout not found")
class LocalFlow(unittest.TestCase):
    def setUp(self):
        os.environ.pop("GITLAB_TOKEN", None)
        os.environ["BRAIN_USER"] = "alice"
        self.addCleanup(os.environ.pop, "BRAIN_USER", None)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = tmpdir(self)          # never read the developer's ~/.kiro, ~/.brain
        self.addCleanup(os.environ.__setitem__, "HOME", old_home)
        fake_tools(self)                           # never run the developer's kiro-cli / kirocrew
        self.repo = git_repo(os.path.join(tmpdir(self), "proj"))
        commit_file(self.repo, "src/app.py", "print('hi')\n", "init")
        rc, out = brain(self.repo, "install", self.repo, "--lean", LEAN_SRC, "--team", "pay", "--prefix", "PAY")
        self.assertEqual(rc, 0, out)
        sh("git", "-C", self.repo, "add", "-A")
        sh("git", "-C", self.repo, "commit", "-q", "-m", "install kit")

    def test_doctor(self):
        rc, out = brain(self.repo, "doctor", "--offline")
        self.assertIn("OK   brain-dev model: claude-opus-5.5", out)
        # fresh repo: Lean's hooks have not run in Kiro yet -> a warning, not a failure
        self.assertRegex(out, r"(OK|WARN) +AI-DLC Lean doctor")
        self.assertIn("forbidden tools: none", out)
        self.assertIn("OK   kiro-cli kiro-cli 0.0.0-fake (not pinned)", out)      # the stand-ins, not this machine's
        self.assertIn("OK   KiroCrew engine: kas", out)
        self.assertNotIn("FAIL", out)
        self.assertEqual(rc, 0)

    def test_doctor_checks_the_crew_agent(self):
        """With KiroCrew there, doctor looks at brain-dev.json: Lean's PreToolUse guard must see every tool."""
        p = cli.crew_agent_path()
        spec = load_json(cli.CREW_AGENT_SRC)
        write_json(p, spec)
        rc, out = brain(self.repo, "doctor", "--offline")
        self.assertIn("OK   KiroCrew brain-dev carries Lean's 3 hooks", out)
        spec["hooks"]["preToolUse"][0]["matcher"] = "read"
        write_json(p, spec)
        rc, out = brain(self.repo, "doctor", "--offline")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL KiroCrew brain-dev.json lacks Lean's preToolUse hook(s)", out)
        self.assertEqual(brain(self.repo, "crew-agent", "--force")[0], 0)
        self.assertNotIn("FAIL", brain(self.repo, "doctor", "--offline")[1])

    def test_doctor_catches_wrong_model(self):
        p = os.path.join(self.repo, ".kiro", "agents", "brain-dev.md")
        with open(p) as f:
            text = f.read()
        with open(p, "w") as f:
            f.write(text.replace("model: claude-opus-5.5", "model: auto"))
        rc, out = brain(self.repo, "doctor", "--offline")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL brain-dev model: auto", out)

    def test_start_handoff_and_ci(self):
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1", "--paths", "src/refund")
        self.assertEqual(rc, 0, out)
        br = sh("git", "-C", self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(br, "ai/alice/PAY-1-refund")
        rec = load_json(os.path.join(self.repo, "runs", "PAY-1", "run.json"))
        self.assertEqual(rec["owner"], "alice")
        self.assertTrue(rec["claim_checked"])
        self.assertTrue(os.path.isdir(os.path.join(self.repo, lean.DOCS, rec["intent"])))
        self.assertEqual(rec["brain_commit"], sh("git", "-C", self.repo, "rev-parse", "main").stdout.strip())

        # same issue twice -> refused
        rc, out = brain(self.repo, "start", "again", "--key", "PAY-1")
        self.assertNotEqual(rc, 0)

        # hand-over refused while the note is still the template
        rc, out = brain(self.repo, "handoff", "--no-push")
        self.assertEqual(rc, 2)
        self.assertIn("hand-over note not ready", out)

        state = lean.state_path(self.repo, rec["intent"])
        with open(state) as f:
            text = f.read()
        text = text.replace(lean.TEMPLATE_STATUS, "Status: Construction app: step 1 of 3 done")
        with open(state, "w") as f:
            f.write(text)
        rc, out = brain(self.repo, "handoff", "--no-push")
        self.assertEqual(rc, 2)
        self.assertIn("commit your work first", out)
        sh("git", "-C", self.repo, "commit", "-q", "-am", "work")

        # someone else cannot hand over alice's run
        os.environ["BRAIN_USER"] = "bob"
        rc, out = brain(self.repo, "handoff", "--no-push")
        self.assertIn("belongs to alice", out)
        os.environ["BRAIN_USER"] = "alice"

        rc, out = brain(self.repo, "handoff", "--no-push")
        self.assertEqual(rc, 0, out)
        rec = load_json(os.path.join(self.repo, "runs", "PAY-1", "run.json"))
        self.assertEqual(rec["handoff"]["by"], "alice")
        self.assertEqual(sh("git", "-C", self.repo, "status", "--porcelain", "--untracked-files=no").stdout, "")

        # CI on the run branch: clean
        self.assertEqual(ci.check_run(self.repo, "ai/alice/PAY-1-refund", {}), [])
        # a bot commit without a voucher fails CI
        commit_file(self.repo, "src/fix.py", "x\n", "bot fix",
                    env={"GIT_COMMITTER_EMAIL": "brain-bot@noreply.invalid"})
        probs = ci.check_run(self.repo, "ai/alice/PAY-1-refund", {})
        self.assertTrue(any("BRAIN_VOUCHERS_DIR" in p for p in probs), probs)

        if sign.available():
            d = tmpdir(self)
            key, allowed = keypair(d)
            head = sh("git", "-C", self.repo, "rev-parse", "HEAD~1").stdout.strip()
            v = {"kind": "handoff", "branch": "ai/alice/PAY-1-refund", "sha": head, "run_id": "RUN-PAY-1-1"}
            write_json(os.path.join(d, "vouchers", "RUN-PAY-1-1", "V-1.json"), {"voucher": v, "sig": sign.sign(v, key)})
            env = {"BRAIN_VOUCHERS_DIR": os.path.join(d, "vouchers"), "BRAIN_ALLOWED_SIGNERS": allowed}
            self.assertEqual(ci.check_run(self.repo, "ai/alice/PAY-1-refund", env), [])
            # a voucher for another branch does not count
            v2 = dict(v, branch="ai/alice/PAY-1-other")
            write_json(os.path.join(d, "vouchers", "RUN-PAY-1-1", "V-1.json"), {"voucher": v2, "sig": sign.sign(v2, key)})
            self.assertTrue(ci.check_run(self.repo, "ai/alice/PAY-1-refund", env))

    def test_bot_cannot_start(self):
        os.environ["BRAIN_USER"] = "brain-bot"
        rc, out = brain(self.repo, "start", "x", "--key", "PAY-2")
        self.assertIn("bot account", out)

    def test_ci_rejects_mismatched_run_json(self):
        self.assertEqual(brain(self.repo, "start", "x", "--key", "PAY-3")[0], 0)
        p = os.path.join(self.repo, "runs", "PAY-3", "run.json")
        rec = load_json(p)
        rec["owner"] = "bob"
        write_json(p, rec)
        self.assertTrue(ci.check_run(self.repo, "ai/alice/PAY-3-x", {}))
        self.assertEqual(ci.check_run(self.repo, "feature/whatever", {}), [])


    # ---------- documents ----------

    def doc(self):
        d = tmpdir(self)
        p = os.path.join(d, "req.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# 退款\n14 天内可退。负责人 @Wang Wei。\n")
        return p

    def test_start_with_document(self):
        store = os.path.join(tmpdir(self), "intake")
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1", "--doc", self.doc(), "--classification",
                        "internal", "--consent", "--store", store)
        self.assertEqual(rc, 0, out)
        rec = load_json(os.path.join(self.repo, "runs", "PAY-1", "run.json"))
        self.assertEqual(rec["materials"][0]["package"], "PKG-PAY-1-1")
        pkg = os.path.join(self.repo, ".brain-materials", "PKG-PAY-1-1")
        self.assertTrue(os.path.exists(os.path.join(pkg, "toc.md")))
        self.assertFalse(os.access(os.path.join(pkg, "toc.md"), os.W_OK))
        self.assertEqual(sh("git", "-C", self.repo, "check-ignore", "-q", ".brain-materials/x", check=False).returncode, 0)
        self.assertEqual(ci.check_run(self.repo, "ai/alice/PAY-1-refund", {}), [])
        # someone force-adds the materials: CI fails
        sh("git", "-C", self.repo, "add", "-f", ".brain-materials")
        sh("git", "-C", self.repo, "commit", "-q", "-m", "oops")
        self.assertTrue(any("committed" in p for p in ci.check_run(self.repo, "ai/alice/PAY-1-refund", {})))

    def test_start_doc_needs_consent_and_class(self):
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1", "--doc", self.doc())
        self.assertEqual(rc, 2)
        self.assertIn("--consent", out)

    def test_handoff_publishes_materials(self):
        d = tmpdir(self)
        remote = os.path.join(d, "intake.git")
        sh("git", "init", "-q", "--bare", "-b", "main", remote)
        store = os.path.join(d, "intake")
        sh("git", "clone", "-q", remote, store)
        for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
            sh("git", "-C", store, "config", k, v)
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1", "--doc", self.doc(), "--classification",
                        "internal", "--consent", "--store", store)
        self.assertEqual(rc, 0, out)
        rec = load_json(os.path.join(self.repo, "runs", "PAY-1", "run.json"))
        state = lean.state_path(self.repo, rec["intent"])
        with open(state) as f:
            text = f.read()
        with open(state, "w") as f:
            f.write(text.replace(lean.TEMPLATE_STATUS, "Status: Inception: requirements drafted"))
        sh("git", "-C", self.repo, "commit", "-q", "-am", "work")
        # no origin for the project repo: hand-over stays local, but materials are pushed to brain-intake
        sh("git", "-C", self.repo, "remote", "add", "origin", os.path.join(d, "nowhere.git"))
        sh("git", "init", "-q", "--bare", "-b", "main", os.path.join(d, "nowhere.git"))
        rc, out = brain(self.repo, "handoff", "--no-label", "--store", store)
        self.assertEqual(rc, 0, out)
        files = sh("git", "-C", remote, "ls-tree", "-r", "--name-only", "main").stdout
        self.assertIn("packages/PKG-PAY-1-1/manifest.json", files)

    def test_claims_without_jira(self):
        rc, out = brain(self.repo, "claim", "--title", "fees")
        self.assertIn("Claimed PAY-1", out)
        rc, out = brain(self.repo, "start", "refund")  # next free key
        self.assertEqual(rc, 0, out)
        self.assertTrue(load_json(os.path.join(self.repo, "runs", "PAY-2", "run.json")))
        os.environ["BRAIN_USER"] = "bob"
        sh("git", "-C", self.repo, "switch", "-q", "main")
        rc, out = brain(self.repo, "start", "steal", "--key", "PAY-1")
        self.assertIn("claimed by alice", out)

    # ---------- route gate ----------

    def test_risk_paths_need_design(self):
        commit_file(self.repo, "gates/risk-paths", "# risky\nsrc/pay/**\n", "risk paths")
        self.assertEqual(brain(self.repo, "start", "refund", "--key", "PAY-1")[0], 0)
        rec = load_json(os.path.join(self.repo, "runs", "PAY-1", "run.json"))
        commit_file(self.repo, "src/other.py", "x\n", "safe change")
        self.assertEqual(ci.check_run(self.repo, "ai/alice/PAY-1-refund", {}), [])
        commit_file(self.repo, "src/pay/charge.py", "x\n", "risky change")
        probs = ci.check_run(self.repo, "ai/alice/PAY-1-refund", {})
        self.assertTrue(any("needs the design stage" in p for p in probs), probs)
        # a design doc plus a forged APPROVED line in the Lean record is NOT enough
        design = os.path.join(self.repo, lean.DOCS, rec["intent"], "inception", "application-design",
                              "application-design.md")
        os.makedirs(os.path.dirname(design), exist_ok=True)
        with open(design, "w") as f:
            f.write("# design\n")
        audit = os.path.join(self.repo, lean.DOCS, "audit", "test.jsonl")
        with open(audit, "a") as f:
            f.write(json.dumps({"seq": 1, "ts": "2026-11-08T00:00:00Z", "event": "APPROVED", "intent": rec["intent"],
                                "data": {"kind": "DESIGN"}}) + "\n")
        probs = ci.check_run(self.repo, "ai/alice/PAY-1-refund", {})
        self.assertTrue(any("no design approval signed by the bridge" in p for p in probs), probs)
        # a bridge-signed design approval for this requirement clears it
        if sign.available():
            d = tmpdir(self)
            key, allowed = keypair(d)
            head = sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip()
            v = {"kind": "answer", "choice": "approve", "lean_request": "DESIGN 8cf2cbce", "run_id": "RUN-PAY-1-1",
                 "sha": head, "decision": "D-1"}
            write_json(os.path.join(d, "v", "RUN-PAY-1-1", "V-a.json"), {"voucher": v, "sig": sign.sign(v, key)})
            env = {"BRAIN_VOUCHERS_DIR": os.path.join(d, "v"), "BRAIN_ALLOWED_SIGNERS": allowed}
            self.assertEqual(ci.check_run(self.repo, "ai/alice/PAY-1-refund", env), [])
            # a rejection, or an approval of something else, does not count
            v2 = dict(v, choice="reject")
            write_json(os.path.join(d, "v", "RUN-PAY-1-1", "V-a.json"), {"voucher": v2, "sig": sign.sign(v2, key)})
            self.assertTrue(ci.check_run(self.repo, "ai/alice/PAY-1-refund", env))

    def test_ci_uses_target_branch_config_and_base(self):
        """In an MR pipeline the kit config, risk paths and diff base come from the target branch."""
        d = tmpdir(self)
        origin = os.path.join(d, "origin.git")
        commit_file(self.repo, "gates/risk-paths", "src/pay/**\n", "risk paths")
        sh("git", "clone", "-q", "--bare", self.repo, origin)
        sh("git", "-C", self.repo, "remote", "add", "origin", origin)
        self.assertEqual(brain(self.repo, "start", "refund", "--key", "PAY-1")[0], 0)
        c1 = commit_file(self.repo, "src/pay/charge.py", "x\n", "risky change")
        # the MR author weakens the branch's own kit config and moves brain_commit past the risky commit
        p = os.path.join(self.repo, ".brain", "kit.json")
        cfg = load_json(p)
        cfg["risk_paths_file"] = "nope"
        cfg["bot_emails"] = []
        write_json(p, cfg)
        rp = os.path.join(self.repo, "runs", "PAY-1", "run.json")
        rec = load_json(rp)
        rec["brain_commit"] = c1
        write_json(rp, rec)
        sh("git", "-C", self.repo, "commit", "-q", "-am", "weaken")
        base = sh("git", "-C", origin, "rev-parse", "main").stdout.strip()
        env = {"CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main", "CI_MERGE_REQUEST_DIFF_BASE_SHA": base}
        probs = ci.check_run(self.repo, "ai/alice/PAY-1-refund", env)
        self.assertTrue(any("not on the target branch" in x for x in probs), probs)
        rec["brain_commit"] = base
        write_json(rp, rec)
        sh("git", "-C", self.repo, "commit", "-q", "-am", "restore brain commit")
        probs = ci.check_run(self.repo, "ai/alice/PAY-1-refund", env)
        self.assertTrue(any("needs the design stage" in x for x in probs), probs)  # weakened config ignored

    def test_push_rules_in_ci(self):
        d = tmpdir(self)
        origin = os.path.join(d, "origin.git")
        sh("git", "clone", "-q", "--bare", self.repo, origin)
        sh("git", "-C", self.repo, "remote", "add", "origin", origin)
        self.assertEqual(brain(self.repo, "start", "refund", "--key", "PAY-1")[0], 0)
        base = sh("git", "-C", origin, "rev-parse", "main").stdout.strip()
        env = {"CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main", "CI_MERGE_REQUEST_DIFF_BASE_SHA": base}
        self.assertEqual(ci.push_rules(self.repo, "ai/alice/PAY-1-refund", "alice", env), [])
        self.assertTrue(ci.push_rules(self.repo, "ai/alice/PAY-1-refund", "bob", env))
        bot = {"GIT_AUTHOR_EMAIL": "brain-bot@noreply.invalid", "GIT_COMMITTER_EMAIL": "brain-bot@noreply.invalid"}
        commit_file(self.repo, ".gitlab-ci.yml", "x\n", "bot edits CI", env=bot)
        probs = ci.push_rules(self.repo, "ai/alice/PAY-1-refund", "brain-bot", env)
        self.assertTrue(any(".gitlab-ci.yml" in p for p in probs), probs)

    def test_crew_hook_adapter_feeds_lean(self):
        """KiroCrew fires agent-spec hooks with acp_session_id; the adapter hands Lean a session_id and its verdict."""
        import subprocess
        self.assertEqual(brain(self.repo, "start", "refund", "--key", "PAY-1")[0], 0)
        hook = os.path.join(self.repo, ".kiro", "brain", "lean-hook.py")
        def fire(event, payload):
            payload = dict(payload, cwd=self.repo, hook_event_name=event, acp_session_id="sess-crew-1")
            return subprocess.run([sys.executable, hook, event], input=json.dumps(payload), text=True,
                                  capture_output=True, cwd=self.repo)
        self.assertEqual(fire("SessionStart", {}).returncode, 0)
        self.assertEqual(fire("UserPromptSubmit", {"prompt": "hi"}).returncode, 0)
        turns = load_json(os.path.join(self.repo, ".aidlc-lean", "turn.json"))
        self.assertIn("sess-crew-1", json.dumps(turns))
        r = fire("PreToolUse", {"tool_name": "fs_write", "tool_input": {"filePath": "src/x.py", "content": "x"}})
        self.assertEqual(r.returncode, 2)
        self.assertIn("blocked (requirements)", r.stderr)
        r = fire("PreToolUse", {"tool_name": "fs_write", "tool_input": {"path": "aidlc-docs/notes.md"}})
        self.assertEqual(r.returncode, 0)

    def test_crew_agent_install(self):
        home = tmpdir(self)
        old = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            self.assertEqual(brain(self.repo, "crew-agent")[0], 0)
            spec = load_json(os.path.join(home, ".kiro", "agents", "brain-dev.json"))
            self.assertIn("PreToolUse", json.dumps(spec["hooks"]))
            self.assertEqual(brain(self.repo, "crew-agent")[0], 0)       # idempotent
            p = os.path.join(home, ".kiro", "agents", "brain-dev.json")
            # Kiro's upgraded list format with another tool's hook and no Lean hooks
            other = {"name": "other-0", "trigger": "stop", "action": {"type": "command", "command": "other.sh"}}
            write_json(p, {"name": "brain-dev", "kiroUpgrade": {"revision": 2}, "hooks": [other]})
            self.assertEqual(brain(self.repo, "crew-agent")[0], 2)       # missing hooks: needs --force
            self.assertEqual(brain(self.repo, "crew-agent", "--force")[0], 0)
            spec = load_json(p)
            self.assertIn(other, spec["hooks"])                           # other tools' hooks kept
            self.assertEqual(spec["kiroUpgrade"], {"revision": 2})
            self.assertEqual(cli.missing_lean_hooks(spec), [])
            self.assertEqual(brain(self.repo, "crew-agent")[0], 0)       # now nothing to do
        finally:
            os.environ["HOME"] = old

    def test_lint(self):
        with open(os.path.join(self.repo, "brain", "TECH.md"), "a") as f:
            f.write("<!-- id:T-PAY-1-1 type:pitfall rel:new -->\na\n<!-- id:T-PAY-1-1 type:pitfall rel:new -->\nb\n")
        rc, out = brain(self.repo, "lint")
        self.assertEqual(rc, 1)
        self.assertIn("already used", out)


if __name__ == "__main__":
    unittest.main()
