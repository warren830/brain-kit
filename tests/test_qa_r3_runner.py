"""QA round 3 (unattended end-to-end flow), runner side: findings 1, 2, 5 and the runner-prompt / hand-over parts of
3 and 4. Each class says which finding; the tests fail on 48b6b97.

  1  Lean's PreToolUse hook blocked runs/<KEY>/park.json (the runner's only way to reach a person) before the
     REQUIREMENTS / DESIGN / PLAN approvals and after a rejection. Fix: the KiroCrew adapter lets through exactly
     this run's park.json (file-write tool, single target); everything else goes to Lean as before.
  2  brain-ci push-rules in the bot's pipeline judged the owner's start / hand-over commits as the bot's push.
  5  after GitLab's Rebase every voucher sha was gone from the branch: check-run failed for good.
  3, 4  brain-run.md: REQUIREMENTS then DESIGN (never INCEPTION), "问题没发出", risk paths need a bridge DESIGN
     approval; `brain handoff` warns when risk paths have none.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import ci, cli, lean, pre_receive, sign  # noqa: E402
from brainkit.common import load_json, write_json  # noqa: E402
from tests.helpers import LEAN_SRC, ROOT, commit_file, git_repo, keypair, sh, tmpdir  # noqa: E402
from tests.test_cli import brain, fake_tools  # noqa: E402

BOT = "brain-bot@noreply.invalid"
BOT_ENV = {"GIT_AUTHOR_NAME": "brain-bot", "GIT_AUTHOR_EMAIL": BOT, "GIT_COMMITTER_NAME": "brain-bot",
           "GIT_COMMITTER_EMAIL": BOT}
OWNER_ENV = {"GIT_AUTHOR_NAME": "alice", "GIT_AUTHOR_EMAIL": "alice@example.com", "GIT_COMMITTER_NAME": "alice",
             "GIT_COMMITTER_EMAIL": "alice@example.com"}
BR = "ai/alice/PAY-1-refund"
INTENT = "261108-pay-1-refund"
HAS_LEAN = os.path.exists(os.path.join(LEAN_SRC, "install.sh"))

REQ = """# Requirements: PAY-1 refund

AI-DLC stage: Inception · Requirements Analysis · Intent: {intent}
Size: large

## Goal
Customers can ask for a refund within 14 days of payment.

## Not doing
- partial refunds

## Acceptance criteria
| ID | Given / when | Then (expected) |
|---|---|---|
| AC-1 | paid 3 days ago, refund asked | refund created |

## Non-functional targets
- none beyond today's system

## Assumptions
- A-1: calendar days

## Questions and answers
1. Q: calendar or business days?
   A: calendar

## Contracts touched
- none
"""
DESIGN = """# Application Design: PAY-1 refund

AI-DLC stage: Inception · Application Design · Intent: {intent}

## Components and responsibilities
| Component | Responsibility | New / changed / existing |
|---|---|---|
| src/pay/refund.py | refund rules | new |

## Interfaces and methods
| Component | Method / endpoint / event | Input -> output | Errors |
|---|---|---|---|
| refund | refund(order) | order -> refund | 422 |

## Dependencies
api -> refund

## Data model
Refund(order_id, status)

## Key decisions
- calendar days{extra}
"""
PLAN = """# Code Generation Plan: app (PAY-1 refund)

AI-DLC stage: Construction · Code Generation · Unit: app · Intent: {intent}

## Unit context
AC-1; changes src/pay/refund.py

## Steps
- [ ] 1. Walking skeleton: refund() returns pending · covers: AC-1 · done when: test passes

## Tests
| AC / BR | Test (file::name) | Kind |
|---|---|---|
| AC-1 | tests/test_refund.py::test_ok | unit |
"""


def home_and_tools(test):
    old = os.environ.get("HOME")
    os.environ["HOME"] = tmpdir(test)                 # never the developer's ~/.kiro or ~/.brain
    test.addCleanup(lambda: os.environ.__setitem__("HOME", old) if old is not None else os.environ.pop("HOME", None))
    fake_tools(test)


def lean_repo(test):
    """A project with brain-kit and the pinned Lean, run PAY-1 started on ai/alice/PAY-1-refund, intent in use."""
    home_and_tools(test)
    d = tmpdir(test)
    env = mock.patch.dict(os.environ, {"BRAIN_USER": "alice", "BRAIN_INTAKE_DIR": os.path.join(d, "no-intake")})
    env.start()
    test.addCleanup(env.stop)
    os.environ.pop("GITLAB_TOKEN", None)
    r = git_repo(os.path.join(d, "proj"))
    commit_file(r, "src/app.py", "print('hi')\n", "init")
    rc, out = brain(r, "install", r, "--lean", LEAN_SRC, "--team", "pay", "--prefix", "PAY")
    test.assertEqual(rc, 0, out)
    sh("git", "-C", r, "add", "-A")
    sh("git", "-C", r, "commit", "-q", "-m", "install kit")
    rc, out = brain(r, "start", "refund")
    test.assertEqual(rc, 0, out)
    return d, r


# ---------- finding 1: the runner can always write its own park.json ----------

@unittest.skipUnless(HAS_LEAN, "AI-DLC Lean checkout not found")
class RunnerParksInEveryApprovalState(unittest.TestCase):
    def setUp(self):
        self.d, self.repo = lean_repo(self)
        lj = os.path.join(self.repo, "aidlc-docs", "lean.json")
        lc = load_json(lj)
        lc["plan"] = "always"                         # PLAN approvals on, as in regulated mode
        write_json(lj, lc)
        self.intent = load_json(os.path.join(self.repo, "runs", "PAY-1", "run.json"))["intent"]
        self.docs = "aidlc-docs/%s/" % self.intent
        self.lean("use", self.intent)

    def lean(self, *args):
        p = subprocess.run([sys.executable, ".kiro/aidlc-lean/aidlc_lean.py"] + list(args), cwd=self.repo,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout

    def hook(self, event, payload):
        """What KiroCrew runs for the brain-run agent: python3 .kiro/brain/lean-hook.py <event>."""
        payload = dict(payload, cwd=self.repo, hook_event_name=event, acp_session_id="runner-1")
        return subprocess.run([sys.executable, ".kiro/brain/lean-hook.py", event], input=json.dumps(payload),
                              text=True, capture_output=True, cwd=self.repo)

    def write(self, rel, text):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def tool(self, path, tool="fs_write", **extra):
        return self.hook("PreToolUse", {"tool_name": tool, "tool_input": dict({"path": path, "text": "{}"}, **extra)})

    def park(self, park):
        return self.hook("PreToolUse", {"tool_name": "fs_write", "tool_input": {
            "path": os.path.join(self.repo, "runs", "PAY-1", "park.json"),
            "text": json.dumps(dict({"schema": 1, "status": "waiting", "options": []}, **park), ensure_ascii=False)}})

    def request(self, kind):
        return re.search(r"approve %s ([0-9a-f]{8})" % kind, self.lean("request", kind)).group(1)

    def say(self, line, expect):
        r = self.hook("UserPromptSubmit", {"prompt": line})
        self.assertIn(expect, r.stdout + r.stderr)

    def may_park(self, what, park):
        r = self.park(park)
        self.assertEqual(r.returncode, 0, "%s: the runner cannot write runs/PAY-1/park.json: %s"
                         % (what, r.stderr.strip()[:300]))
        code = self.tool("src/pay/refund.py")         # control: Lean still gates the code
        self.assertEqual(code.returncode, 2, "%s: code was not gated" % what)
        self.assertIn("[AI-DLC Lean] blocked", code.stderr)

    def test_before_requirements_before_design_after_a_rejection_and_with_plan_pending(self):
        self.may_park("before REQUIREMENTS", {"kind": "clarify", "ask": "owner", "question": "按自然日还是交易日？",
                                              "options": [{"id": "A", "text": "自然日"}, {"id": "B", "text": "交易日"}]})
        self.lean("stage", "start", "requirements")
        self.write(self.docs + "inception/requirements/requirements.md", REQ.format(intent=self.intent))
        self.lean("stage", "done", "requirements", "--note", "AC-1")
        h = self.request("REQUIREMENTS")
        self.may_park("REQUIREMENTS pending", {"kind": "clarify", "ask": "product", "question": "请批准需求",
                                               "lean_request": "REQUIREMENTS " + h})
        self.say("approve REQUIREMENTS " + h, "Recorded: REQUIREMENTS approved")
        self.lean("stage", "start", "application-design")
        design = self.docs + "inception/application-design/application-design.md"
        self.write(design, DESIGN.format(intent=self.intent, extra=""))
        self.lean("stage", "done", "application-design", "--note", "one module")
        h = self.request("DESIGN")
        self.may_park("DESIGN pending", {"kind": "design", "ask": "builder", "question": "请批准设计",
                                         "lean_request": "DESIGN " + h})
        self.say("reject DESIGN too big for one module", "Recorded: DESIGN rejected")
        self.may_park("after a DESIGN rejection", {"kind": "clarify", "ask": "owner", "question": "拆成两个模块？",
                                                   "options": [{"id": "A", "text": "拆"}, {"id": "B", "text": "不拆"}]})
        self.write(design, DESIGN.format(intent=self.intent, extra="\n- two modules"))
        h = self.request("DESIGN")
        self.say("approve DESIGN " + h, "Recorded: DESIGN approved")
        self.lean("stage", "start", "code-generation")
        self.write(self.docs + "construction/plans/app-code-generation-plan.md", PLAN.format(intent=self.intent))
        h = self.request("PLAN")
        self.may_park("PLAN pending", {"kind": "plan", "ask": "builder", "question": "请批准计划",
                                       "lean_request": "PLAN " + h})

    def test_everything_else_still_goes_to_lean(self):
        """Before REQUIREMENTS every code path is gated: nothing below may slip through as "the park.json"."""
        repo = self.repo
        self.write("runs/PAY-2/run.json", json.dumps({"key": "PAY-2", "branch": "ai/bob/PAY-2-other"}))
        outside = os.path.join(tmpdir(self), "runs", "PAY-1", "park.json")
        cases = {
            "run.json": self.tool("runs/PAY-1/run.json"),
            "another key's park.json": self.tool("runs/PAY-2/park.json"),
            "another runs/ file": self.tool("runs/PAY-1/notes.json"),
            "a dot-dot path to it": self.tool("runs/PAY-1/../PAY-1/park.json"),
            "a dot-dot path out of it": self.tool("runs/PAY-1/../../src/app.py"),
            "outside the workspace": self.tool(outside),
            "a second target": self.tool("runs/PAY-1/park.json", destination="src/app.py"),
            "a second path key": self.tool("runs/PAY-1/park.json", file_path="src/app.py"),
            "a delegation tool": self.tool("runs/PAY-1/park.json", tool="invoke_sub_agent"),
            "an unknown write command": self.tool("runs/PAY-1/park.json", command="rename"),
        }
        for what, r in cases.items():
            self.assertEqual(r.returncode, 2, "%s was let through: %s" % (what, r.stderr))
        park = os.path.join(repo, "runs", "PAY-1", "park.json")
        os.symlink(os.path.join("..", "..", "src", "app.py"), park)          # park.json is a symlink to code
        self.assertEqual(self.tool("runs/PAY-1/park.json").returncode, 2)
        os.remove(park)
        os.link(os.path.join(repo, "src", "app.py"), park)                   # ... or a hard link to it
        self.assertEqual(self.tool("runs/PAY-1/park.json").returncode, 2)
        os.remove(park)
        self.assertEqual(self.tool("runs/PAY-1/park.json").returncode, 0)   # the real one
        # the key comes from the branch and run.json naming it: no branch, or a run.json for another branch, no key
        rp = os.path.join(repo, "runs", "PAY-1", "run.json")
        rec = load_json(rp)
        write_json(rp, dict(rec, branch="ai/alice/PAY-1-other"))
        self.assertEqual(self.tool("runs/PAY-1/park.json").returncode, 2)
        write_json(rp, rec)
        sh("git", "-C", repo, "checkout", "-q", "--detach")
        self.assertEqual(self.tool("runs/PAY-1/park.json").returncode, 2)
        sh("git", "-C", repo, "checkout", "-q", BR)
        # a symlinked runs/<KEY>/ folder
        os.rename(os.path.join(repo, "runs", "PAY-1"), os.path.join(repo, "src", "PAY-1"))
        os.symlink(os.path.join("..", "src", "PAY-1"), os.path.join(repo, "runs", "PAY-1"))
        self.assertEqual(self.tool("runs/PAY-1/park.json").returncode, 2)

    def test_the_branch_name_without_a_key_takes_the_one_run_json_naming_it(self):
        sh("git", "-C", self.repo, "branch", "-q", "-m", "work")
        rp = os.path.join(self.repo, "runs", "PAY-1", "run.json")
        write_json(rp, dict(load_json(rp), branch="work"))
        self.assertEqual(self.tool("runs/PAY-1/park.json").returncode, 0)
        self.write("runs/PAY-2/run.json", json.dumps({"key": "PAY-2", "branch": "work"}))
        self.assertEqual(self.tool("runs/PAY-1/park.json").returncode, 2)   # two runs name it: none

    def test_the_runner_commits_the_park_and_writes_mr_md_through_the_shell(self):
        """MR.md needs no allowance: `brain mr-desc --write` runs in the shell, which Lean does not gate by path."""
        self.assertEqual(self.park({"kind": "clarify", "ask": "owner", "question": "q?",
                                    "options": [{"id": "A", "text": "a"}]}).returncode, 0)
        for cmd in ("brain mr-desc --write", "git add runs/PAY-1/park.json runs/PAY-1/MR.md && git commit -q -m park",
                    "git push origin HEAD:" + BR):
            r = self.hook("PreToolUse", {"tool_name": "execute_bash", "tool_input": {"command": cmd}})
            self.assertEqual(r.returncode, 0, "%s: %s" % (cmd, r.stderr))


class AdapterVersionIsChecked(unittest.TestCase):
    """doctor and handoff (runner_kit_problems) refuse an adapter without the park.json allowance."""

    def test_the_shipped_adapter_has_it(self):
        with open(os.path.join(cli.KIT_SRC, cli.CREW_HOOK), encoding="utf-8") as f:
            self.assertEqual(cli.crew_hook_version(f.read()), cli.CREW_HOOK_VERSION)

    def test_runner_kit_problems(self):
        repo = git_repo(os.path.join(tmpdir(self), "r"))
        commit_file(repo, ".kiro/aidlc-lean/aidlc_lean.py", "# lean\n")
        commit_file(repo, cli.CREW_HOOK, "# adapter of 48b6b97\n")
        probs = cli.runner_kit_problems(repo)
        self.assertEqual(len(probs), 1)
        self.assertIn("older adapter (version 0, needs %d)" % cli.CREW_HOOK_VERSION, probs[0])
        with open(os.path.join(cli.KIT_SRC, cli.CREW_HOOK), encoding="utf-8") as f:
            commit_file(repo, cli.CREW_HOOK, f.read())
        self.assertEqual(cli.runner_kit_problems(repo), [])
        with open(os.path.join(repo, cli.CREW_HOOK), "w") as f:            # the disk does not count: git does
            f.write("# old\n")
        self.assertEqual(cli.runner_kit_problems(repo), [])

    @unittest.skipUnless(HAS_LEAN, "AI-DLC Lean checkout not found")
    def test_doctor_and_handoff(self):
        d, repo = lean_repo(self)
        rc, out = brain(repo, "doctor", "--offline")
        self.assertIn("OK   %s lets a run write its own runs/<KEY>/park.json" % cli.CREW_HOOK, out)
        with open(os.path.join(repo, cli.CREW_HOOK), "w") as f:
            f.write("# adapter of 48b6b97\n")
        rc, out = brain(repo, "doctor", "--offline")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL %s is an older adapter" % cli.CREW_HOOK, out)
        rec = load_json(os.path.join(repo, "runs", "PAY-1", "run.json"))
        state = os.path.join(repo, "aidlc-docs", rec["intent"], "aidlc-state.md")
        with open(state) as f:
            text = f.read()
        with open(state, "w") as f:
            f.write(text.replace("Status: Inception: starting", "Status: Construction app: step 1 of 3 done")
                    .replace(lean.TEMPLATE_NEXT[0], "Write step 2 of 3"))
        sh("git", "-C", repo, "commit", "-q", "-am", "work and an old adapter")
        rc, out = brain(repo, "handoff", "--no-push")
        self.assertEqual(rc, 2)
        self.assertIn("older adapter", out)


# ---------- finding 2: push-rules in the bot's pipeline ----------

def voucher_file(vdir, key, v):
    write_json(os.path.join(vdir, v["run_id"], v["voucher_id"] + ".json"), {"voucher": v, "sig": sign.sign(v, key)})


def run_record(brain_commit, handoff=None):
    return {"schema": 1, "key": "PAY-1", "tracker": "none", "owner": "alice", "branch": BR, "intent": INTENT,
            "brain_commit": brain_commit, "kit_version": "0.1.0", "origin": "local", "expected_paths": [],
            "materials": [], "created_at": "2026-11-08T00:00:00Z", "claim_checked": True, "handoff": handoff}


@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class PushRulesInTheBotsPipeline(unittest.TestCase):
    """Ported from qa_r3/test_02 with the bridge's hand-over voucher in BRAIN_VOUCHERS_DIR, as the CI job has it."""

    def setUp(self):
        home_and_tools(self)
        d = self.d = tmpdir(self)
        r = self.repo = git_repo(os.path.join(d, "proj"))
        commit_file(r, ".brain/kit.json", json.dumps({"version": 1, "req_prefix": "PAY"}), "kit")
        bc = commit_file(r, "brain/TECH.md", "# TECH\n", "brain")
        self.origin = os.path.join(d, "origin.git")
        sh("git", "clone", "-q", "--bare", r, self.origin)
        sh("git", "-C", r, "remote", "add", "origin", self.origin)
        sh("git", "-C", r, "fetch", "-q", "origin")
        self.base = sh("git", "-C", r, "rev-parse", "main").stdout.strip()
        sh("git", "-C", r, "switch", "-q", "-c", BR)
        rec = run_record(bc)
        commit_file(r, "runs/PAY-1/run.json", json.dumps(rec), "PAY-1: start run", env=OWNER_ENV)
        commit_file(r, "aidlc-docs/%s/aidlc-state.md" % INTENT, "# state\n", "work", env=OWNER_ENV)
        commit_file(r, "src/refund.py", "x = 1\n", "PAY-1: local work", env=OWNER_ENV)
        rec["handoff"] = {"requested_at": "2026-11-08T01:00:00Z", "by": "alice", "local_head": "x"}
        self.handoff = commit_file(r, "runs/PAY-1/run.json", json.dumps(rec), "PAY-1: hand off to team runner",
                                   env=OWNER_ENV)
        commit_file(r, "src/refund.py", "x = 2\n", "PAY-1: finish step 3", env=BOT_ENV)
        self.bot_head = commit_file(r, "runs/PAY-1/park.json", json.dumps({"schema": 1, "status": "waiting"}),
                                    "PAY-1: park", env=BOT_ENV)
        sh("git", "-C", r, "push", "-q", "origin", "HEAD:refs/heads/" + BR)
        self.key, allowed = keypair(d)
        self.vdir = os.path.join(d, "vouchers")
        self.voucher(self.handoff)
        self.env = {"CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main", "CI_MERGE_REQUEST_DIFF_BASE_SHA": self.base,
                    "BRAIN_VOUCHERS_DIR": self.vdir, "BRAIN_ALLOWED_SIGNERS": allowed}

    def voucher(self, sha, branch=BR, vid="V-0123456789"):
        voucher_file(self.vdir, self.key, {"schema": 1, "kind": "handoff", "run_id": "RUN-PAY-1-1", "branch": branch,
                                           "sha": sha, "owner": "alice", "key": "PAY-1", "intent": INTENT,
                                           "voucher_id": vid})

    def rules(self, user="brain-bot", env=None):
        return ci.push_rules(self.repo, BR, user, env or self.env)

    def test_pre_receive_accepts_the_runners_push(self):
        self.assertEqual(pre_receive.decide([(self.handoff, self.bot_head, "refs/heads/" + BR)], "brain-bot",
                                            dict(pre_receive.DEFAULT_CFG), cwd=self.origin), [])

    def test_the_bots_pipeline_accepts_the_same_push(self):
        self.assertEqual(self.rules(), [])
        self.assertEqual(self.rules("alice"), [])                          # the owner's pipeline as before

    def test_without_a_hand_over_voucher_it_fails_and_says_why(self):
        for env in ({k: v for k, v in self.env.items() if not k.startswith("BRAIN_")},
                    dict(self.env, BRAIN_VOUCHERS_DIR=os.path.join(self.d, "none"))):
            probs = self.rules(env=env)
            self.assertEqual(len(probs), 1, probs)
            self.assertIn("started by the bot", probs[0])
            self.assertIn("hand-over", probs[0])
        for f in os.listdir(os.path.join(self.vdir, "RUN-PAY-1-1")):
            os.remove(os.path.join(self.vdir, "RUN-PAY-1-1", f))
        self.voucher(self.handoff, branch="ai/alice/PAY-1-other")             # another branch's voucher
        self.assertIn("no signed hand-over voucher for %s" % BR, " ".join(self.rules()))

    def test_the_bots_own_commits_still_follow_the_bot_rules(self):
        rec = load_json(os.path.join(self.repo, "runs", "PAY-1", "run.json"))
        commit_file(self.repo, "runs/PAY-1/run.json", json.dumps(dict(rec, expected_paths=["src"])), "bot", env=BOT_ENV)
        commit_file(self.repo, "ci/x.yml", "x\n", "bot edits CI", env=BOT_ENV)
        text = " ".join(self.rules())
        self.assertIn("may not change runs/PAY-1/run.json", text)
        self.assertIn("may not change ci/x.yml", text)

    def test_after_the_hand_over_only_the_owner_and_the_bot_commit(self):
        commit_file(self.repo, "src/owner_fix.py", "y = 1\n", "the owner's fix after the hand-over", env=OWNER_ENV)
        self.assertEqual(self.rules(), [])
        commit_file(self.repo, "src/forged.py", "z = 1\n", "the bot commits as alice",
                    env=dict(OWNER_ENV, GIT_COMMITTER_EMAIL=BOT))
        self.assertIn("authored by alice@example.com but committed by the bot", " ".join(self.rules()))
        sh("git", "-C", self.repo, "reset", "-q", "--hard", "HEAD~1")
        commit_file(self.repo, "src/carol.py", "z = 1\n", "a third person",
                    env={"GIT_AUTHOR_EMAIL": "carol@example.com", "GIT_COMMITTER_EMAIL": "carol@example.com"})
        self.assertIn("committed by carol@example.com", " ".join(self.rules()))

    def test_after_the_owners_rebase_the_bots_next_push_passes(self):
        r = self.repo
        sh("git", "-C", r, "switch", "-q", "main")
        commit_file(r, "src/other.py", "y = 1\n", "PAY-2 merged first", env=OWNER_ENV)
        sh("git", "-C", r, "push", "-q", "origin", "main")
        sh("git", "-C", r, "switch", "-q", BR)
        sh("git", "-C", r, "rebase", "-q", "main", env={"GIT_COMMITTER_NAME": "alice",
                                                       "GIT_COMMITTER_EMAIL": "alice@example.com"})
        commit_file(r, "src/refund.py", "x = 3\n", "PAY-1: the bot goes on", env=BOT_ENV)
        new_base = sh("git", "-C", r, "rev-parse", "main").stdout.strip()
        sh("git", "-C", r, "push", "-q", "-f", "origin", "HEAD:refs/heads/" + BR)
        self.assertEqual(self.rules(env=dict(self.env, CI_MERGE_REQUEST_DIFF_BASE_SHA=new_base)), [])


# ---------- finding 5: vouchers after GitLab's Rebase ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class VouchersSurviveARebase(unittest.TestCase):
    """Ported from qa_r3/test_05, with merge-main, an edited rebase and a DESIGN approval on a risk path."""

    def setUp(self):
        home_and_tools(self)
        d = self.d = tmpdir(self)
        w = self.w = git_repo(os.path.join(d, "work"))
        commit_file(w, ".brain/kit.json", json.dumps({"version": 1, "req_prefix": "PAY"}), "kit")
        commit_file(w, "gates/risk-paths", "src/pay/**\n", "risk paths")
        bc = commit_file(w, "brain/TECH.md", "# TECH\n", "brain")
        self.origin = os.path.join(d, "origin.git")
        sh("git", "clone", "-q", "--bare", w, self.origin)
        sh("git", "-C", w, "remote", "add", "origin", self.origin)
        sh("git", "-C", w, "switch", "-q", "-c", BR)
        rec = run_record(bc)
        commit_file(w, "runs/PAY-1/run.json", json.dumps(rec), "PAY-1: start run", env=OWNER_ENV)
        self.design_text = "# Application Design\n\nrefund rules in src/pay/refund.py\n"
        commit_file(w, "aidlc-docs/%s/inception/application-design/application-design.md" % INTENT,
                    self.design_text, "design", env=OWNER_ENV)
        rec["handoff"] = {"requested_at": "2026-11-08T01:00:00Z", "by": "alice", "local_head": "x"}
        self.handoff = commit_file(w, "runs/PAY-1/run.json", json.dumps(rec), "PAY-1: hand off", env=OWNER_ENV)
        self.asked = commit_file(w, "runs/PAY-1/park.json", json.dumps({"schema": 1, "status": "waiting"}),
                                 "PAY-1: park DESIGN", env=BOT_ENV)
        commit_file(w, "src/pay/refund.py", "x = 1\n", "PAY-1: code", env=BOT_ENV)
        sh("git", "-C", w, "push", "-q", "origin", BR)
        self.key, self.allowed = keypair(d)
        self.vdir = os.path.join(d, "vouchers")
        voucher_file(self.vdir, self.key, {"schema": 1, "kind": "handoff", "run_id": "RUN-PAY-1-1", "branch": BR,
                                           "sha": self.handoff, "owner": "alice", "key": "PAY-1", "intent": INTENT,
                                           "voucher_id": "V-0123456789"})
        voucher_file(self.vdir, self.key, {"schema": 1, "kind": "answer", "decision": "D-1", "run_id": "RUN-PAY-1-1",
                                           "sha": self.asked, "choice": "approve", "decision_kind": "design",
                                           "lean_request": "DESIGN " + hashlib.sha256(
                                               self.design_text.encode()).hexdigest()[:8],
                                           "voucher_id": "V-1123456789"})

    def ci_on(self):
        clone = os.path.join(tmpdir(self), "ci")
        sh("git", "clone", "-q", self.origin, clone)
        sh("git", "-C", clone, "checkout", "-q", "-B", BR, "origin/" + BR)
        base = sh("git", "-C", clone, "merge-base", "HEAD", "origin/main").stdout.strip()
        return ci.check_run(clone, BR, {"CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main",
                                        "CI_MERGE_REQUEST_DIFF_BASE_SHA": base, "BRAIN_VOUCHERS_DIR": self.vdir,
                                        "BRAIN_ALLOWED_SIGNERS": self.allowed})

    def main_moves_on(self):
        w = self.w
        sh("git", "-C", w, "switch", "-q", "main")
        commit_file(w, "src/other.py", "y = 1\n", "PAY-2 merged first", env=OWNER_ENV)
        sh("git", "-C", w, "push", "-q", "origin", "main")
        sh("git", "-C", w, "switch", "-q", BR)

    def rebase(self, *extra, env=None):
        sh("git", "-C", self.w, "rebase", "-q", *extra, "main",
           env=dict({"GIT_COMMITTER_NAME": "alice", "GIT_COMMITTER_EMAIL": "alice@example.com"}, **(env or {})))
        sh("git", "-C", self.w, "push", "-q", "-f", "origin", BR)        # what GitLab's Rebase button does

    def test_control_before_the_rebase(self):
        self.assertEqual(self.ci_on(), [])

    def test_after_gitlabs_rebase(self):
        self.main_moves_on()
        self.rebase()
        self.assertEqual(self.ci_on(), [])                 # hand-over voucher and the DESIGN approval both count

    def test_after_merging_main_in(self):
        self.main_moves_on()
        sh("git", "-C", self.w, "merge", "-q", "--no-edit", "main", env=OWNER_ENV)
        sh("git", "-C", self.w, "push", "-q", "origin", BR)
        self.assertEqual(self.ci_on(), [])

    def test_a_rebase_that_changed_the_vouched_part_loses_the_vouchers(self):
        self.main_moves_on()
        # the owner's commit before the hand-over is edited during the rebase (here: a reworded file)
        seq = os.path.join(self.d, "seq.sh")
        with open(seq, "w") as f:
            f.write("#!/bin/sh\nsed -i.bak '2s/^pick/edit/' \"$1\"\n")
        os.chmod(seq, 0o755)
        env = {"GIT_SEQUENCE_EDITOR": seq, "GIT_COMMITTER_NAME": "alice", "GIT_COMMITTER_EMAIL": "alice@example.com"}
        sh("git", "-C", self.w, "rebase", "-q", "-i", "main", env=env)
        path = os.path.join(self.w, "aidlc-docs", INTENT, "inception", "application-design", "application-design.md")
        with open(path, "a") as f:
            f.write("and a change nobody approved\n")
        sh("git", "-C", self.w, "commit", "-q", "-a", "--amend", "--no-edit", env=env)
        sh("git", "-C", self.w, "rebase", "--continue", env=env)
        sh("git", "-C", self.w, "push", "-q", "-f", "origin", BR)
        text = " ".join(self.ci_on())
        self.assertIn("without a valid hand-over voucher", text)
        self.assertIn("no design approval signed by the bridge", text)

    def test_reordered_commits_do_not_match(self):
        """A commit moved from after the question to before it: the approval no longer stands for that history."""
        repo = git_repo(os.path.join(self.d, "plain"))
        commit_file(repo, "a.txt", "a\n", "base")
        up = sh("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()
        commit_file(repo, "runs/PAY-1/run.json", "{}\n", "rec", env=OWNER_ENV)
        q = commit_file(repo, "b.txt", "b\n", "asked here", env=BOT_ENV)
        commit_file(repo, "c.txt", "c\n", "after", env=BOT_ENV)
        self.assertEqual(ci.rebased_commit(repo, q, up, "runs/PAY-1/run.json"), q)        # on the branch: itself
        sh("git", "-C", repo, "switch", "-q", "-c", "re", up)
        sh("git", "-C", repo, "cherry-pick", "main~2", env=OWNER_ENV)                # rec
        sh("git", "-C", repo, "cherry-pick", "main", env=OWNER_ENV)                  # c moved before b
        sh("git", "-C", repo, "cherry-pick", "main~1", env=OWNER_ENV)                # b
        self.assertIsNone(ci.rebased_commit(repo, q, up, "runs/PAY-1/run.json"))
        sh("git", "-C", repo, "switch", "-q", "-c", "same", up)
        for c in ("main~2", "main~1", "main"):
            sh("git", "-C", repo, "cherry-pick", c, env=OWNER_ENV)
        self.assertEqual(ci.rebased_commit(repo, q, up, "runs/PAY-1/run.json"),
                         sh("git", "-C", repo, "rev-parse", "HEAD~1").stdout.strip())


# ---------- findings 3 and 4: the runner's prompt and the hand-over ----------

class RunnerPrompt(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "remote", "prompts", "brain-run.md"), encoding="utf-8") as f:
            self.text = " ".join(f.read().split())

    def test_requirements_then_design_never_inception(self):
        self.assertIn("REQUIREMENTS then DESIGN", self.text)
        self.assertIn("Never park `INCEPTION <hash>`", self.text)
        self.assertIn("the bridge refuses it", self.text)

    def test_what_comes_back_when_a_question_was_not_asked(self):
        self.assertIn("starts `问题没发出` with the reason", self.text)
        self.assertIn("Fix park.json for that reason", self.text)
        self.assertIn("the push is what makes the bridge read it again", self.text)

    def test_risk_paths_need_a_bridge_design_approval_before_code(self):
        self.assertIn("Risk paths need a DESIGN approval asked through the bridge, before code", self.text)
        self.assertIn("does not count for CI", self.text)
        self.assertIn('"lean_request": "DESIGN <hash>"', self.text)

    def test_the_bots_git_identity(self):
        self.assertIn("the bot account's git identity", self.text)
        self.assertIn("Never set `user.name` / `user.email`", self.text)


@unittest.skipUnless(HAS_LEAN, "AI-DLC Lean checkout not found")
class HandoffWarnsAboutRiskPaths(unittest.TestCase):
    def setUp(self):
        self.d, self.repo = lean_repo(self)
        r = self.repo
        sh("git", "-C", r, "switch", "-q", "main")
        commit_file(r, "gates/risk-paths", "src/pay/**\n", "risk paths")
        sh("git", "-C", r, "switch", "-q", BR)
        sh("git", "-C", r, "merge", "-q", "--no-edit", "main")
        rec = load_json(os.path.join(r, "runs", "PAY-1", "run.json"))
        self.state = os.path.join(r, "aidlc-docs", rec["intent"], "aidlc-state.md")
        with open(self.state) as f:
            text = f.read()
        with open(self.state, "w") as f:
            f.write(text.replace("Status: Inception: starting", "Status: Construction app: step 1 of 3 done")
                    .replace(lean.TEMPLATE_NEXT[0], "Write step 2 of 3"))

    def handoff(self):
        sh("git", "-C", self.repo, "commit", "-q", "-am", "work")
        rc, out = brain(self.repo, "handoff", "--no-push")
        self.assertEqual(rc, 0, out)
        return out

    def test_a_risk_path_change_without_a_bridge_design_approval(self):
        commit_file(self.repo, "src/pay/refund.py", "x = 1\n", "risky")
        out = self.handoff()
        self.assertIn("WARN: this run changes risk paths (src/pay/refund.py; gates/risk-paths)", out)
        self.assertIn("A DESIGN approved in your own Lean chat does not count", out)
        self.assertIn("the hand-over note", out)

    def test_planned_risk_paths_and_a_note_that_says_so(self):
        rp = os.path.join(self.repo, "runs", "PAY-1", "run.json")
        write_json(rp, dict(load_json(rp), expected_paths=["src/pay"]))
        with open(self.state) as f:
            text = f.read()
        with open(self.state, "w") as f:
            f.write(re.sub(r"(### Next\s*\n)", r"\g<1>1. Ask for the DESIGN approval through the bridge first\n",
                           text, count=1))
        out = self.handoff()
        self.assertIn("plans to change (run.json expected_paths) risk paths (src/pay;", out)
        self.assertNotIn("the hand-over note", out)

    def test_no_risk_path_no_warning(self):
        commit_file(self.repo, "src/app2.py", "x = 1\n", "plain")
        self.assertNotIn("risk paths", self.handoff())


if __name__ == "__main__":
    unittest.main()
