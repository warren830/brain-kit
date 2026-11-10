"""A big requirement split into small ones that different people claim and build in parallel (brainkit/split.py).

End to end through a real AI-DLC Lean install (vendor/aidlc-lean), a bare origin and two clones, alice's and bob's.
Nobody sets BRAIN_USER, brain.user or a GitLab token and the repo has no GitLab: each person is their login name
(cli._me), switched here through LOGNAME/USER.
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import ci, claims, cli, config, intents, mrdesc, pre_receive, split  # noqa: E402
from brainkit.common import ZERO_SHA, BrainError, load_json, paths_overlap, write_json  # noqa: E402
from tests.helpers import LEAN_SRC, commit_file, git_repo, no_hooks_config, sh, tmpdir  # noqa: E402

HAVE_LEAN = os.path.exists(os.path.join(LEAN_SRC, "install.sh"))
LEAN = os.path.join(".kiro", "aidlc-lean", "aidlc_lean.py")

REQUIREMENTS = """# Requirements: 退款大需求

AI-DLC stage: Inception · Requirements Analysis · Intent: {intent}
Size: large

## Goal
买家在订单页申请退款，后台按规则自动审核，每晚对账。

## Not doing
- 部分退款

## Acceptance criteria
| ID | Given / when | Then (expected) |
|---|---|---|
| AC-1 | POST /refunds 一个 14 天内的订单 | 201，退款单状态 pending |
| AC-2 | POST /refunds 一个 15 天前的订单 | 422 too_old |
| AC-3 | 订单页点"申请退款" | 显示退款单状态 |
| AC-4 | 每晚 02:00 对账任务 | 两边笔数一致，不一致时告警 |

## Non-functional targets
- p95 < 300 ms

## Assumptions
- A-1: 只退原支付方式

## Questions and answers
1. Q: 退款期限？
   A: 14 天
"""
STORIES = """# User Stories: 退款大需求

## Personas
- 买家：申请退款

## Stories
| ID | As a | I want | So that | ACs |
|---|---|---|---|---|
| US-1 | 买家 | 通过接口提交退款 | 钱能退回 | AC-1, AC-2 |
| US-2 | 买家 | 在订单页看到退款状态 | 知道进度 | AC-3 |
| US-3 | 运营 | 每晚自动对账 | 账不出错 | AC-4 |
"""
DESIGN = """# Application Design: 退款大需求

## Components and responsibilities
| Component | Responsibility | New / changed / existing |
|---|---|---|
| refunds api | 建退款单 | new |
| web | 订单页退款按钮 | changed |
| jobs | 夜间对账 | new |

## Key decisions
- 三个单元，api 先做，web 用它的接口{extra}
"""
UNITS = """# Units of Work: 退款大需求

| Unit | Owner team | Paths | Depends on | Contracts | Stories / ACs | Status |
|---|---|---|---|---|---|---|
{rows}
"""
ROWS = ["| api | api | services/api/**, aidlc-docs/contracts/api/** | - | aidlc-docs/contracts/api/refunds.yaml | US-1 "
        "| not started |",
        "| web | web | web/** | api | aidlc-docs/contracts/api/refunds.yaml | US-2 | not started |",
        "| jobs | jobs | jobs/** | - | - | US-3 | not started |"]
PLAN = """# Code Generation Plan: {unit}

## Unit context
unit {unit}

## Steps
- [x] 1. Walking skeleton · covers: {acs} · done when: test passes

## Tests
| AC / BR | Test (file::name) | Kind |
|---|---|---|
| {acs} | tests/test_{unit}.py::test_ok | unit |
"""


def write(base, rel, text):
    p = os.path.join(base, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def lean(where, *args, check=True):
    return sh(sys.executable, LEAN, *args, cwd=where, check=check)


def request(where, kind):
    p = lean(where, "request", kind, check=False)
    m = re.search(r"approve %s ([0-9a-f]{8})" % kind, p.stdout + p.stderr)
    assert m, p.stdout + p.stderr
    return m.group(1)


def approve(where, kind):
    """The person types the approval line Lean asked for (Lean's own prompt hook records it)."""
    line = "approve %s %s" % (kind, request(where, kind))
    p = subprocess.run([sys.executable, LEAN, "hook", "UserPromptSubmit"], cwd=where, text=True, capture_output=True,
                       input=json.dumps({"prompt": line, "session_id": "s-%s" % kind.lower()}))
    assert p.returncode == 0 and "NOT recorded" not in p.stdout + p.stderr, p.stdout + p.stderr


def commit_all(where, msg):
    sh("git", "-C", where, "add", "-A")
    sh("git", "-C", where, "commit", "-q", "-m", msg)


@contextlib.contextmanager
def login(name):
    """Act as `name`: the login name is all brain-kit has here (no GitLab, no brain.user, no BRAIN_USER)."""
    old = {k: os.environ.get(k) for k in ("LOGNAME", "USER")}
    os.environ["LOGNAME"] = os.environ["USER"] = name
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def brain(where, *args):
    out = io.StringIO()
    cwd = os.getcwd()
    os.chdir(where)
    try:
        with redirect_stdout(out), redirect_stderr(out):
            rc = cli.main(list(args))
    finally:
        os.chdir(cwd)
    return rc, out.getvalue()


class Project:
    """A bare origin with brain-kit and Lean installed on main, and clones for people."""

    def __init__(self, test, multi_team=False):
        self.test = test
        d = self.d = tmpdir(test)
        seed = git_repo(os.path.join(d, "seed"))
        commit_file(seed, "src/app.py", "print('hi')\n", "init")
        rc, out = brain(seed, "install", seed, "--lean", LEAN_SRC, "--prefix", "PAY")
        assert rc == 0, out
        cfg = load_json(os.path.join(seed, "aidlc-docs", "lean.json"))
        cfg["checks"] = ["true"]
        if multi_team:          # a person's choice in lean.json; OWNERS maps each unit's contract folder to it
            cfg["multi_team"] = True
            write(seed, "aidlc-docs/contracts/OWNERS", "# <path glob> <team>\naidlc-docs/contracts/api/** api\n")
        write_json(os.path.join(seed, "aidlc-docs", "lean.json"), cfg)
        commit_all(seed, "install kit")
        self.origin = os.path.join(d, "origin.git")
        sh("git", "clone", "-q", "--bare", seed, self.origin)

    def clone(self, name):
        path = os.path.join(self.d, name)
        sh("git", "clone", "-q", self.origin, path)
        with open(os.path.join(path, ".git", "config"), "a", encoding="utf-8") as f:
            f.write("[user]\n\tname = %s\n\temail = %s@example.com\n[commit]\n\tgpgsign = false\n" % (name, name)
                    + no_hooks_config(path))
        return path


def isolate(test):
    """No identity but the login name; HOME (worktrees under ~/.brain) in a temp folder."""
    for k in ("BRAIN_USER", "GITLAB_TOKEN", "AIDLC_LEAN_TEAM"):
        if k in os.environ:
            test.addCleanup(os.environ.__setitem__, k, os.environ.pop(k))
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = tmpdir(test)
    test.addCleanup(os.environ.__setitem__, "HOME", old_home)
    return os.environ["HOME"]


@unittest.skipUnless(HAVE_LEAN, "AI-DLC Lean checkout not found")
class Family(unittest.TestCase):
    def setUp(self):
        self.home = isolate(self)
        self.p = Project(self)
        self.alice = self.p.clone("alice")
        self.bob = self.p.clone("bob")

    # ---------- helpers ----------

    def run_as(self, who, where, *args):
        with login(who):
            return brain(where, *args)

    def ok(self, who, where, *args):
        rc, out = self.run_as(who, where, *args)
        self.assertEqual(rc, 0, out)
        return out

    def refused(self, who, where, *args):
        rc, out = self.run_as(who, where, *args)
        self.assertEqual(rc, 2, out)
        return out

    def claim(self, key, where=None):
        return claims.read(where or self.alice, key)

    def inception(self, rows=ROWS, approve_design=True):
        """alice's parent run: requirements, stories, design and units, approved; units recorded before DESIGN."""
        out = self.ok("alice", self.alice, "start", "big", "--title", "退款大需求")
        self.assertIn("Started PAY-1", out)
        rec = load_json(os.path.join(self.alice, "runs", "PAY-1", "run.json"))
        self.intent = intent = rec["intent"]
        docs = "aidlc-docs/%s" % intent
        a = self.alice
        lean(a, "stage", "skip", "reverse-engineering", "--reason", "greenfield")
        write(a, docs + "/inception/requirements/requirements.md", REQUIREMENTS.format(intent=intent))
        lean(a, "stage", "done", "requirements", "--note", "AC-1..4")
        lean(a, "stage", "start", "user-stories")
        write(a, docs + "/inception/user-stories/stories.md", STORIES)
        lean(a, "stage", "done", "user-stories", "--note", "US-1..3")
        lean(a, "stage", "start", "application-design")
        write(a, docs + "/inception/application-design/application-design.md", DESIGN.format(extra=""))
        lean(a, "stage", "done", "application-design", "--note", "three units")
        lean(a, "stage", "start", "units")
        write(a, docs + "/inception/application-design/unit-of-work.md", UNITS.format(rows="\n".join(rows)))
        lean(a, "stage", "done", "units", "--note", "api, web, jobs")
        approve(a, "REQUIREMENTS")
        if approve_design:
            approve(a, "DESIGN")
        commit_all(a, "PAY-1: inception")
        return intent

    def build_unit(self, wt, unit, acs, files):
        """A child's Lean work on its unit: plan, code, `stage done code-generation --unit`."""
        lean(wt, "stage", "start", "code-generation", "--unit", unit)
        write(wt, "aidlc-docs/%s/construction/plans/%s-code-generation-plan.md" % (self.intent, unit),
              PLAN.format(unit=unit, acs=acs))
        for rel, text in files.items():
            write(wt, rel, text)
        lean(wt, "stage", "done", "code-generation", "--unit", unit, "--note", "unit %s built" % unit)
        commit_all(wt, "%s: unit %s" % (os.path.basename(wt), unit))
        sh("git", "-C", wt, "push", "-q", "origin", "HEAD")

    def hand_over(self, who, wt, unit):
        """A child's `brain handoff`: its own `### Next (team <unit>)` note, then the MR into the parent's branch (no
        GitLab here: it pushes and says to open the MR by hand). Its run.json records the request ("mr")."""
        with open(os.path.join(wt, "aidlc-docs", self.intent, "aidlc-state.md"), "a", encoding="utf-8") as f:
            f.write("### Next (team %s)\nStatus: Construction %s: done\n1. review and merge\n" % (
                split.lean_team(unit), unit))
        commit_all(wt, "%s: notes" % unit)
        out = self.ok(who, wt, "handoff")
        self.assertIn("open its MR into ai/alice/PAY-1-big by hand, for PAY-1's owner to review and merge", out)
        return out

    def merge_into_parent(self, branch):
        """The parent's owner merges a child's MR into the parent's run branch (as GitLab would, no conflicts)."""
        a = self.alice
        sh("git", "-C", a, "fetch", "-q", "origin")
        sh("git", "-C", a, "merge", "-q", "--no-ff", "--no-edit", "origin/" + branch)
        sh("git", "-C", a, "push", "-q", "origin", "ai/alice/PAY-1-big")

    def family(self, who="alice", where=None):
        rc, out = self.run_as(who, where or self.alice, "intent", "list", "--all", "--json")
        self.assertEqual(rc, 0, out)
        return {d["key"]: d for d in json.loads(out)}

    def wt(self, owner, key):
        return split.worktree_path(getattr(self, owner), key)

    # ---------- the whole way ----------

    def test_split_claim_build_release(self):
        self.inception()
        # a dry run shows the three children and files nothing
        out = self.ok("alice", self.alice, "split", "PAY-1", "--dry-run")
        self.assertIn("预览", out)
        self.assertEqual(out.count("新登记"), 3, out)
        self.assertIsNone(self.claim("PAY-2"))

        out = self.ok("alice", self.alice, "split", "PAY-1")
        self.assertIn("PAY-1 拆成 3 个小需求", out)
        api, web, jobs = self.claim("PAY-2"), self.claim("PAY-3"), self.claim("PAY-4")
        self.assertEqual([api["unit"], web["unit"], jobs["unit"]], ["api", "web", "jobs"])
        for c in (api, web, jobs):
            self.assertIsNone(c["owner"])                      # 待认领
            self.assertEqual((c["filed_by"], c["parent"], c["source"]), ("alice", "PAY-1", "split"))
            self.assertEqual(c["split"]["branch"], "ai/alice/PAY-1-big")
            self.assertEqual(intents.doc_problems(c), [])
        self.assertEqual(api["expected_paths"], ["services/api/**", "aidlc-docs/contracts/api/**"])
        self.assertEqual(web["depends_on"], ["PAY-2"])
        self.assertEqual(api["contracts"], ["aidlc-docs/contracts/api/refunds.yaml"])
        self.assertEqual(api["title"], "api · 通过接口提交退款")           # from the story Lean links to the unit
        self.assertEqual(api["acs"], ["AC-1: POST /refunds 一个 14 天内的订单 → 201，退款单状态 pending",
                                      "AC-2: POST /refunds 一个 15 天前的订单 → 422 too_old"])
        self.assertEqual(jobs["acs"], ["AC-4: 每晚 02:00 对账任务 → 两边笔数一致，不一致时告警"])
        # filing is idempotent: the same parent and unit are filed once
        out = self.ok("alice", self.alice, "split", "PAY-1")
        self.assertEqual(out.count("没变"), 3, out)
        self.assertIsNone(self.claim("PAY-5"))

        # bob sees them in his clone: the parent, its children, state and dependencies
        rc, out = self.run_as("bob", self.bob, "intent", "list")
        self.assertEqual(rc, 0, out)
        self.assertIn("大需求：已合并 0/3", out)
        self.assertRegex(out, r"PAY-3 +web +待认领 .*依赖 PAY-2（待认领）")

        # alice claims api, bob claims jobs; bob's claim of api is refused
        self.assertIn("Claimed PAY-2 for alice", self.ok("alice", self.alice, "claim", "PAY-2"))
        self.assertIn("Claimed PAY-4 for bob", self.ok("bob", self.bob, "claim", "PAY-4"))
        self.assertIn("PAY-2 is claimed by alice", self.refused("bob", self.bob, "claim", "PAY-2"))
        self.assertEqual(self.claim("PAY-2")["owner"], "alice")
        self.assertIn("already yours", self.ok("alice", self.alice, "claim", "PAY-2"))

        # web depends on api, which has not merged: bob cannot start it (and does not claim it on the way)
        out = self.refused("bob", self.bob, "start", "--key", "PAY-3")
        self.assertIn("PAY-3 depends on PAY-2 (unit api, claimed by alice), which has not merged into "
                      "ai/alice/PAY-1-big yet", out)
        self.assertIn("--on PAY-2", out)
        self.assertIsNone(self.claim("PAY-3")["owner"])

        # alice starts api: its own branch and worktree, continuing the parent's Lean intent for unit api
        out = self.ok("alice", self.alice, "start", "--key", "PAY-2")
        wt_api = self.wt("alice", "PAY-2")
        self.assertIn("Started PAY-2 (unit api of PAY-1) on ai/alice/PAY-2-api", out)
        rec = load_json(os.path.join(wt_api, "runs", "PAY-2", "run.json"))
        self.assertEqual((rec["intent"], rec["parent"], rec["unit"], rec["target_branch"], rec["lean_team"]),
                         (self.intent, "PAY-1", "api", "ai/alice/PAY-1-big", "api"))
        self.assertEqual(rec["expected_paths"], ["services/api/**", "aidlc-docs/contracts/api/**"])
        self.assertRegex(rec["lean_clone"], r"^[0-9a-f]{6}$")
        self.assertEqual(load_json(os.path.join(wt_api, ".aidlc-lean", "local.json"))["team"], "api")
        self.assertIn("Active intent: aidlc-docs/%s" % self.intent, lean(wt_api, "status").stdout)
        with open(os.path.join(wt_api, "runs", "PAY-2", "intent.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("- Unit: api (Lean `--unit api`)", md)
        self.assertIn("- AC-1: POST /refunds", md)
        # the main checkout stays on the parent's branch; the child's branch is on origin
        self.assertEqual(sh("git", "-C", self.alice, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
                         "ai/alice/PAY-1-big")
        self.assertIn("ai/alice/PAY-2-api", sh("git", "-C", self.p.origin, "branch").stdout)

        # bob starts jobs (no dependencies) in parallel
        self.ok("bob", self.bob, "start", "--key", "PAY-4")
        wt_jobs = self.wt("bob", "PAY-4")

        # the parent cannot be handed over or closed while its children are building
        self.assertIn("not merged into ai/alice/PAY-1-big yet", self.refused("alice", self.alice, "handoff",
                                                                              "--no-push"))
        self.assertIn("neither merged nor closed", self.refused("alice", self.alice, "intent", "close", "PAY-1",
                                                                "--reason", "x"))

        # api is built; CI accepts the child's branch against its MR target (the parent's branch), and the MR
        # description is about the unit (its plan, its ACs; the release is the parent's)
        self.build_unit(wt_api, "api", "AC-1", {"services/api/refunds.py": "def refund():\n    return 201\n"})
        self.assertEqual(ci.check_run(wt_api, "ai/alice/PAY-2-api",
                                      {"CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "ai/alice/PAY-1-big"}), [])
        d = mrdesc.build(wt_api, config.load(wt_api), "PAY-2")
        self.assertEqual((d["family"]["parent"], d["family"]["unit"]), ("PAY-1", "api"))
        self.assertEqual([x["unit"] for x in d["plans"]], ["api"])
        self.assertEqual([x["id"] for x in d["acceptance"]], ["AC-1", "AC-2"])
        self.assertNotIn("RELEASE", [m["kind"] for m in d["approvals"]["missing"]])
        self.assertIn("这是 PAY-1 拆出来的小需求：只做单元 `api`", mrdesc.render(d))
        # pushed but not handed off, it has not merged even if the parent's branch held its commits
        self.assertEqual(self.family()["PAY-2"]["state"], "in_progress")
        self.hand_over("alice", wt_api, "api")
        self.merge_into_parent("ai/alice/PAY-2-api")
        fam = self.family()
        self.assertEqual(fam["PAY-2"]["state"], "merged")
        self.assertEqual(fam["PAY-1"]["progress"]["text"], "1/3 merged")

        # now web can start (starting claims it), from the parent's branch with api in it
        out = self.ok("bob", self.bob, "start", "--key", "PAY-3")
        self.assertIn("Claimed PAY-3 for bob", out)
        wt_web = self.wt("bob", "PAY-3")
        self.assertTrue(os.path.exists(os.path.join(wt_web, "services", "api", "refunds.py")))

        # jobs was built next to api: both changed Lean's aidlc-state.md; brain sync resolves it
        self.build_unit(wt_jobs, "jobs", "AC-4", {"jobs/reconcile.py": "def run():\n    return 0\n"})
        self.assertEqual(subprocess.run(["git", "-C", wt_jobs, "merge", "--no-commit", "--no-ff",
                                         "origin/ai/alice/PAY-1-big"], capture_output=True).returncode, 1)
        sh("git", "-C", wt_jobs, "merge", "--abort")              # git alone stops on the conflict
        out = self.ok("bob", wt_jobs, "sync")
        self.assertIn("Lean's generated files were resolved", out)
        with open(os.path.join(wt_jobs, "aidlc-docs", self.intent, "aidlc-state.md"), encoding="utf-8") as f:
            state = f.read()
        self.assertNotIn("<<<<<<<", state)
        self.assertIn("Code Generation (api) — done", state)       # Lean wrote its progress from the merged ledger
        self.assertIn("Code Generation (jobs) — done", state)
        self.hand_over("bob", wt_jobs, "jobs")
        self.merge_into_parent("ai/bob/PAY-4-jobs")
        self.assertEqual(self.family()["PAY-1"]["progress"]["merged"], 2)

        self.build_unit(wt_web, "web", "AC-3", {"web/refund.js": "export const x = 1;\n"})
        self.ok("bob", wt_web, "sync")
        self.hand_over("bob", wt_web, "web")
        self.merge_into_parent("ai/bob/PAY-3-web")
        with open(os.path.join(self.alice, "aidlc-docs", self.intent, "aidlc-state.md"), encoding="utf-8") as f:
            notes = f.read()
        for team in ("api", "jobs", "web"):                         # every child's own hand-over note survived
            self.assertIn("### Next (team %s)" % team, notes)
        fam = self.family("bob", self.bob)
        self.assertEqual(fam["PAY-1"]["progress"], {"merged": 3, "total": 3, "unclaimed": 0, "claimed": 0,
                                                    "in_progress": 0, "closed": 0, "text": "3/3 merged"})
        self.assertEqual(fam["PAY-1"]["family_state"], "ready_to_release")
        self.assertEqual([fam[k]["blocked_by"] for k in ("PAY-2", "PAY-3", "PAY-4")], [[], [], []])

        # the parent's owner reviews and releases; then the parent is done (it closes)
        a = self.alice
        lean(a, "status")
        self.assertIn("verify: PASS", lean(a, "verify").stdout)
        write(a, "aidlc-docs/%s/construction/build-and-test/review.md" % self.intent, "# Review\n\nnone left\n")
        commit_all(a, "PAY-1: verify and review")
        self.assertIn("Wrote", lean(a, "release").stdout)
        approve(a, "RELEASE")
        commit_all(a, "PAY-1: released")
        sh("git", "-C", a, "push", "-q", "origin", "ai/alice/PAY-1-big")
        fam = self.family("bob", self.bob)
        self.assertEqual((fam["PAY-1"]["family_state"], fam["PAY-1"]["released"]), ("done", True))
        rc, out = self.run_as("bob", self.bob, "intent", "list")
        self.assertNotIn("PAY-1", out)                              # done: no longer in the open list
        out = self.ok("alice", self.alice, "intent", "close", "PAY-1", "--reason", "发布了，三个小需求都合并了")
        self.assertIn("PAY-1 已关闭", out)
        self.assertEqual(self.family()["PAY-1"]["family_state"], "closed")

    def test_split_needs_the_design_approval_to_cover_the_units(self):
        self.inception(approve_design=False)
        out = self.refused("alice", self.alice, "split", "PAY-1")
        self.assertIn("application-design.md is draft", out)
        # the units recorded after the DESIGN approval: the approval did not cover them
        docs = "aidlc-docs/%s" % self.intent
        approve(self.alice, "DESIGN")
        write(self.alice, docs + "/inception/application-design/unit-of-work.md",
              UNITS.format(rows="\n".join(ROWS[:2])))
        commit_all(self.alice, "edit units")
        out = self.refused("alice", self.alice, "split", "PAY-1")
        self.assertIn("unit-of-work.md changed after `lean stage done units` recorded it", out)
        lean(self.alice, "stage", "done", "units", "--note", "two units now")
        commit_all(self.alice, "record units")
        out = self.refused("alice", self.alice, "split", "PAY-1")
        self.assertIn("is not the one recorded last before the DESIGN approval", out)
        approve(self.alice, "DESIGN")                              # the same design, approved again: now it covers
        commit_all(self.alice, "design again")
        self.ok("alice", self.alice, "split", "PAY-1")
        self.assertEqual(self.claim("PAY-3")["unit"], "web")
        self.assertIsNone(self.claim("PAY-4"))
        # nothing uncommitted may be split, and only the parent's owner splits
        write(self.alice, docs + "/decisions.md", "# Decisions\n")
        self.assertIn("commit Lean's records first", self.refused("alice", self.alice, "split", "PAY-1"))
        sh("git", "-C", self.alice, "checkout", "--", docs + "/decisions.md")
        self.assertIn("belongs to alice", self.refused("bob", self.alice, "split", "PAY-1"))

    def test_resplit_after_a_new_design(self):
        self.inception()
        self.ok("alice", self.alice, "split", "PAY-1")
        self.ok("bob", self.bob, "claim", "PAY-4")                # bob takes jobs
        # a new design: api also owns services/shared, jobs is dropped, admin is new, web stays
        docs = "aidlc-docs/%s" % self.intent
        rows = ["| api | api | services/api/**, aidlc-docs/contracts/api/**, services/shared/** | - | "
                "aidlc-docs/contracts/api/refunds.yaml | US-1 | not started |", ROWS[1],
                "| admin | admin | admin/** | api | - | AC-2 | not started |"]
        write(self.alice, docs + "/inception/application-design/unit-of-work.md", UNITS.format(rows="\n".join(rows)))
        lean(self.alice, "stage", "done", "units", "--note", "admin in, jobs out")
        write(self.alice, docs + "/inception/application-design/application-design.md",
              DESIGN.format(extra="\n- admin 单元替掉 jobs"))
        lean(self.alice, "stage", "done", "application-design", "--note", "admin replaces jobs")
        approve(self.alice, "DESIGN")
        commit_all(self.alice, "PAY-1: new design")
        out = self.ok("alice", self.alice, "split", "PAY-1", "--dry-run")
        self.assertIn("预览", out)
        self.assertNotIn("services/shared", json.dumps(self.claim("PAY-2")))   # a dry run writes nothing
        out = self.ok("alice", self.alice, "split", "PAY-1")
        api, jobs, admin = self.claim("PAY-2"), self.claim("PAY-4"), self.claim("PAY-5")
        self.assertEqual(api["expected_paths"], ["services/api/**", "aidlc-docs/contracts/api/**",
                                                 "services/shared/**"])                     # unowned: updated
        self.assertEqual(api["owner"], None)
        self.assertEqual((admin["unit"], admin["depends_on"], admin["owner"]), ("admin", ["PAY-2"], None))  # new
        self.assertFalse(jobs.get("closed"))                       # claimed by bob: never closed or re-pointed
        self.assertEqual(jobs["owner"], "bob")
        self.assertIn("单元没了，但已经有人认领或开工", out)
        self.assertIn("没变", out)                                  # web
        # an unowned child whose unit is gone is closed with a reason
        self.ok("bob", self.bob, "claim", "--release", "PAY-4")
        self.assertIsNone(self.claim("PAY-4")["owner"])
        self.ok("alice", self.alice, "split", "PAY-1")
        self.assertIn("unit jobs is no longer in PAY-1's unit split", self.claim("PAY-4")["closed"]["reason"])
        # a child claimed before a change is not re-pointed by the split; its owner gets the new split at start
        self.ok("bob", self.bob, "claim", "PAY-5")
        rows[2] = "| admin | admin | admin/**, tools/admin/** | api | - | AC-2 | not started |"
        write(self.alice, docs + "/inception/application-design/unit-of-work.md", UNITS.format(rows="\n".join(rows)))
        lean(self.alice, "stage", "done", "units", "--note", "admin tools")
        approve(self.alice, "DESIGN")
        commit_all(self.alice, "PAY-1: admin tools")
        out = self.ok("alice", self.alice, "split", "PAY-1")
        self.assertIn("拆分变了，但已经有人认领或开工：没改", out)
        self.assertEqual(self.claim("PAY-5")["expected_paths"], ["admin/**"])
        self.ok("alice", self.alice, "claim", "PAY-2")
        self.ok("alice", self.alice, "start", "--key", "PAY-2")
        self.build_unit(self.wt("alice", "PAY-2"), "api", "AC-1", {"services/api/a.py": "x = 1\n"})
        self.hand_over("alice", self.wt("alice", "PAY-2"), "api")
        self.merge_into_parent("ai/alice/PAY-2-api")
        out = self.ok("bob", self.bob, "start", "--key", "PAY-5")
        self.assertIn("PAY-5 now follows PAY-1's current unit split (expected_paths admin/** -> admin/**, "
                      "tools/admin/**)", out)
        self.assertEqual(self.claim("PAY-5")["expected_paths"], ["admin/**", "tools/admin/**"])

    def test_build_on_a_dependency_branch(self):
        self.inception()
        self.ok("alice", self.alice, "split", "PAY-1")
        self.ok("alice", self.alice, "start", "--key", "PAY-2")
        self.assertIn("--on PAY-4: PAY-3 does not depend on it", self.refused("bob", self.bob, "start", "--key",
                                                                               "PAY-3", "--on", "PAY-4"))
        out = self.ok("bob", self.bob, "start", "--key", "PAY-3", "--on", "PAY-2")
        self.assertIn("from PAY-2's branch", out)
        rec = load_json(os.path.join(self.wt("bob", "PAY-3"), "runs", "PAY-3", "run.json"))
        self.assertEqual((rec["built_on"], rec["target_branch"]), ("PAY-2", "ai/alice/PAY-1-big"))
        self.assertTrue(os.path.exists(os.path.join(self.wt("bob", "PAY-3"), "runs", "PAY-2", "run.json")))
        # a child's hand-over note is its own `### Next (team <unit>)` subsection, not the parent's Next
        wt = self.wt("bob", "PAY-3")
        rc, out = self.run_as("bob", wt, "handoff", "--no-push")
        self.assertIn("no '### Next (team web)' items", out)
        st = os.path.join(wt, "aidlc-docs", self.intent, "aidlc-state.md")
        with open(st, "a", encoding="utf-8") as f:
            f.write("### Next (team web)\nStatus: Construction web: step 1 of 2\n1. step 2: the refund button\n")
        commit_all(wt, "notes")
        # it says it carries PAY-2's commits until PAY-2 merges; a child goes to its parent's owner (an MR into the
        # parent's branch with the generated description), not to the team runner: no hand-over record, no label
        out = self.ok("bob", wt, "handoff", "--no-push")
        self.assertIn("started from PAY-2's branch, which has not merged", out)
        self.assertIn("Ready for PAY-1's owner: push ai/bob/PAY-3-web and open its MR into ai/alice/PAY-1-big", out)
        rec = load_json(os.path.join(wt, "runs", "PAY-3", "run.json"))
        self.assertEqual((rec["handoff"], rec["mr"]["by"]), (None, "bob"))   # a merge request, not a runner hand-over
        self.assertIn("runs/PAY-3/MR.md", sh("git", "-C", wt, "show", "--name-only", "HEAD").stdout)
        self.assertEqual(cli._dirty(wt), [])
        # web merges first, with api's start commit inside: api has not merged for that (review #2)
        sh("git", "-C", wt, "push", "-q", "origin", "HEAD")
        self.merge_into_parent("ai/bob/PAY-3-web")
        fam = self.family()
        self.assertEqual((fam["PAY-3"]["state"], fam["PAY-2"]["state"]), ("merged", "in_progress"))
        self.assertEqual(fam["PAY-1"]["progress"]["merged"], 1)

    def test_bots_and_strangers(self):
        self.inception()
        self.ok("alice", self.alice, "split", "PAY-1")
        os.environ["BRAIN_USER"] = "brain-bot"
        try:
            self.assertIn("bot account", self.refused("alice", self.bob, "claim", "PAY-2"))
        finally:
            os.environ.pop("BRAIN_USER")
        # bob cannot release or close what is not his, or what nobody owns
        self.ok("bob", self.bob, "claim", "PAY-2")
        self.assertIn("claimed by bob", self.refused("alice", self.alice, "claim", "--release", "PAY-2"))
        self.assertIn("only alice may close it", self.refused("bob", self.bob, "intent", "close", "PAY-3",
                                                              "--reason", "x"))
        # a parent cannot be split from a child's branch
        self.ok("bob", self.bob, "start", "--key", "PAY-2")
        self.assertIn("is itself a child of PAY-1", self.refused("bob", self.wt("bob", "PAY-2"), "split", "PAY-2"))


@unittest.skipUnless(HAVE_LEAN, "AI-DLC Lean checkout not found")
class ReviewFixes(Family):
    """Regressions for the defects an independent review found in the first version (numbers as it gave them)."""

    test_split_claim_build_release = test_split_needs_the_design_approval_to_cover_the_units = None
    test_resplit_after_a_new_design = test_build_on_a_dependency_branch = test_bots_and_strangers = None

    def test_3_the_split_recorded_last_before_the_approval_counts(self):
        self.inception(approve_design=False)
        docs = "aidlc-docs/%s/inception/application-design/unit-of-work.md" % self.intent
        write(self.alice, docs, UNITS.format(rows="\n".join(ROWS[:2])))
        lean(self.alice, "stage", "done", "units", "--note", "two units")
        approve(self.alice, "DESIGN")                             # the person saw two units
        write(self.alice, docs, UNITS.format(rows="\n".join(ROWS)))
        lean(self.alice, "stage", "done", "units", "--note", "three again")   # recorded once before, too
        commit_all(self.alice, "units again")
        self.assertIn("is not the one recorded last before the DESIGN approval",
                      self.refused("alice", self.alice, "split", "PAY-1"))
        self.assertIsNone(self.claim("PAY-2"))

    def test_12_units_and_approval_in_two_clones_are_refused(self):
        self.inception(approve_design=False)
        other = os.path.join(self.p.d, "alice-second")
        sh("git", "-C", self.alice, "worktree", "add", "-q", "--detach", other, "HEAD")
        approve(other, "DESIGN")                                  # another Lean clone: its own ledger file
        commit_all(other, "DESIGN approved in another clone")
        sh("git", "-C", self.alice, "merge", "-q", "--ff-only", sh("git", "-C", other, "rev-parse", "HEAD")
           .stdout.strip())
        self.assertIn("recorded in another Lean clone than the DESIGN approval",
                      self.refused("alice", self.alice, "split", "PAY-1"))

    def test_5_someone_elses_branch_neither_blocks_nor_serves_as_a_dependency(self):
        self.inception()
        self.ok("alice", self.alice, "split", "PAY-1")
        self.ok("alice", self.alice, "claim", "PAY-2")
        sh("git", "-C", self.bob, "fetch", "-q", "origin")
        tip = sh("git", "-C", self.bob, "rev-parse", "origin/ai/alice/PAY-1-big").stdout.strip()
        for squat in ("ai/aaron/PAY-4-x", "ai/aaron/PAY-2-x"):     # anyone may push ai/<themselves>/<any key>-x
            sh("git", "-C", self.bob, "push", "-q", "origin", "%s:refs/heads/%s" % (tip, squat))
        self.ok("bob", self.bob, "start", "--key", "PAY-4")       # aaron's branch is not PAY-4's run
        self.assertIn("--on PAY-2: it has no run branch of its owner (alice) yet",
                      self.refused("bob", self.bob, "start", "--key", "PAY-3", "--on", "PAY-2"))
        self.assertIsNone(self.claim("PAY-3")["owner"])

    def test_6_a_claim_taken_while_the_split_is_applied_is_left_alone(self):
        self.inception()
        self.ok("alice", self.alice, "split", "PAY-1")
        docs = "aidlc-docs/%s" % self.intent
        rows = [ROWS[0].replace("services/api/**", "services/api/**, services/shared/**")] + ROWS[1:]
        write(self.alice, docs + "/inception/application-design/unit-of-work.md", UNITS.format(rows="\n".join(rows)))
        lean(self.alice, "stage", "done", "units", "--note", "api owns shared")
        approve(self.alice, "DESIGN")
        commit_all(self.alice, "new split")
        with login("alice"):
            from brainkit import config as C
            cfg = C.load(self.alice)
            p = split.plan(self.alice, cfg, "alice", "PAY-1")
            self.assertEqual([a["kind"] for a in p["actions"] if a["unit"] == "api"], ["update"])
            claims.take(self.bob, "PAY-2", "bob")                  # bob claims it in between
            split.apply(self.alice, cfg, "alice", p)
        api = self.claim("PAY-2")
        self.assertEqual((api["owner"], api["expected_paths"]), ("bob", ["services/api/**",
                                                                          "aidlc-docs/contracts/api/**"]))
        self.assertIn("claimed by bob meanwhile", " ".join(next(a for a in p["actions"] if a["unit"] == "api")["why"]))

    def test_7_a_start_that_fails_gives_the_claim_back(self):
        self.inception()
        self.ok("alice", self.alice, "split", "PAY-1")
        blocker = os.path.join(self.p.d, "a-file")
        write(self.p.d, "a-file", "x\n")
        out = self.refused("bob", self.bob, "start", "--key", "PAY-4", "--worktree", os.path.join(blocker, "wt"))
        self.assertIn("Released PAY-4 again", out)
        self.assertIsNone(self.claim("PAY-4")["owner"])

    def test_14_a_second_open_child_of_one_unit_is_closed(self):
        self.inception()
        self.ok("alice", self.alice, "split", "PAY-1")
        api = self.claim("PAY-2")
        extra = {k: v for k, v in api.items() if k not in ("key", "owner", "created_at", "schema")}
        dup = claims.claim(self.alice, "PAY", None, api["title"], extra=extra)        # as a parallel split would
        out = self.ok("alice", self.alice, "split", "PAY-1")
        self.assertIn("重复，关掉", out)
        self.assertIn("duplicate child of PAY-1 for unit api", self.claim(dup["key"])["closed"]["reason"])
        self.assertFalse(self.claim("PAY-2").get("closed"))


@unittest.skipUnless(HAVE_LEAN, "AI-DLC Lean checkout not found")
class MultiTeam(Family):
    """Lean's multi-team mode with the mapping team = unit: Lean's own Next sends each child to its unit, and a
    contract change between two children goes through Lean's CR flow (cr new, the owning unit approves, the
    requester is unlocked once the approval reaches it)."""

    ROWS = ["| api | api | services/api/**, aidlc-docs/contracts/api/** | - | aidlc-docs/contracts/api/refunds.yaml "
            "| US-1 | not started |",
            "| web | web | web/** | - | aidlc-docs/contracts/api/refunds.yaml | US-2 | not started |"]
    CONTRACT = "aidlc-docs/contracts/api/refunds.yaml"

    def setUp(self):
        self.home = isolate(self)
        self.p = Project(self, multi_team=True)
        self.alice = self.p.clone("alice")
        self.bob = self.p.clone("bob")
        lean(self.alice, "init", "--team", "lead")              # the parent's own clone: a team that owns no unit

    def write_allowed(self, where, rel):
        """Would Lean's PreToolUse hook let the agent write this file? (exit 0 allows, 2 blocks)"""
        p = subprocess.run([sys.executable, LEAN, "hook", "PreToolUse"], cwd=where, text=True, capture_output=True,
                           input=json.dumps({"session_id": "s1", "tool_name": "fs_write",
                                             "tool_input": {"path": os.path.join(where, rel), "text": "x"}}))
        self.assertIn(p.returncode, (0, 2), p.stderr)
        return p.returncode == 0, p.stderr

    # the inherited end-to-end tests run on the standard-mode project only
    test_split_claim_build_release = test_split_needs_the_design_approval_to_cover_the_units = None
    test_resplit_after_a_new_design = test_build_on_a_dependency_branch = test_bots_and_strangers = None

    def test_lean_guides_each_child_and_contracts_go_through_crs(self):
        self.inception(rows=self.ROWS)
        out = self.ok("alice", self.alice, "split", "PAY-1", "--dry-run")
        self.assertNotIn("is not multi-team", out)
        self.assertNotIn("this clone's Lean team", out)            # the parent's clone is team lead, no unit
        lean(self.alice, "init", "--team", "api")
        commit_all(self.alice, "ledger")
        self.assertIn("this clone's Lean team is api, a unit's", self.ok("alice", self.alice, "split", "PAY-1",
                                                                          "--dry-run"))
        lean(self.alice, "init", "--team", "lead")
        commit_all(self.alice, "ledger")
        self.ok("alice", self.alice, "split", "PAY-1")
        self.assertIn("wait for unit api (team api), web (team web)", lean(self.alice, "status").stdout)
        self.ok("alice", self.alice, "start", "--key", "PAY-2")
        self.ok("bob", self.bob, "start", "--key", "PAY-3")
        api, web = self.wt("alice", "PAY-2"), self.wt("bob", "PAY-3")
        self.assertIn("Next: unit api:", lean(api, "status").stdout)
        self.assertIn("Next: unit web:", lean(web, "status").stdout)
        # api owns its contract; web may not change it without a change request api approved
        self.assertTrue(self.write_allowed(api, self.CONTRACT)[0])
        write(api, self.CONTRACT, "openapi: 3.0.0\n")
        commit_all(api, "PAY-2: refunds contract")
        sh("git", "-C", api, "push", "-q", "origin", "HEAD")
        ok, why = self.write_allowed(web, self.CONTRACT)
        self.assertFalse(ok)
        self.assertIn("belongs to team api", why)
        out = lean(web, "cr", "new", "add-reason", "--owner", "api", "--paths", self.CONTRACT).stdout
        cr = re.search(r"aidlc-docs/contracts/changes/(CR-web-1-add-reason\.md)", out).group(1)
        commit_all(web, "PAY-3: CR-web-1")
        sh("git", "-C", web, "push", "-q", "origin", "HEAD")
        # api takes just the CR file from web's branch and approves it in its own worktree (Lean team api)
        sh("git", "-C", api, "fetch", "-q", "origin")
        sh("git", "-C", api, "checkout", "origin/ai/bob/PAY-3-web", "--", "aidlc-docs/contracts/changes/" + cr)
        commit_all(api, "PAY-2: CR-web-1 from web")
        approve(api, "CR-web-1")
        commit_all(api, "PAY-2: CR-web-1 approved")
        sh("git", "-C", api, "push", "-q", "origin", "HEAD")
        self.assertFalse(self.write_allowed(web, self.CONTRACT)[0])         # the approval has not reached web yet
        self.ok("bob", web, "sync", "--from", "origin/ai/alice/PAY-2-api")
        self.assertTrue(self.write_allowed(web, self.CONTRACT)[0])          # Lean's CR flow unlocked it for web


class FiledForOthers(unittest.TestCase):
    """Any intent can be filed without an owner (`brain intent add --unclaimed`); `brain claim KEY` takes it."""

    def setUp(self):
        isolate(self)
        self.repo = git_repo(os.path.join(tmpdir(self), "r"))
        commit_file(self.repo, ".brain/kit.json", json.dumps({"version": 1, "req_prefix": "PAY", "tracker": "none"}))

    def test_unclaimed_intent(self):
        with login("alice"):
            rc, out = brain(self.repo, "intent", "add", "--source", "debt", "--title", "回调加重试", "--body",
                            "回调失败不重试。", "--unclaimed")
        self.assertEqual(rc, 0, out)
        self.assertIn("待认领", out)
        doc = claims.read(self.repo, "PAY-1")
        self.assertEqual((doc["owner"], doc["filed_by"]), (None, "alice"))
        with login("bob"):
            rc, out = brain(self.repo, "intent", "list")
            self.assertRegex(out, r"PAY-1 +open +debt +- +回调加重试")
            rc, out = brain(self.repo, "claim", "PAY-1")
        self.assertIn("Claimed PAY-1 for bob", out)
        with login("carol"):
            rc, out = brain(self.repo, "claim", "PAY-1")
        self.assertEqual(rc, 2)
        self.assertIn("PAY-1 is claimed by bob", out)
        with login("bob"):
            self.assertEqual(brain(self.repo, "claim", "--release", "PAY-1")[0], 0)
        self.assertIsNone(claims.read(self.repo, "PAY-1")["owner"])
        # the old way still works: `brain claim --key` claims a new key for whoever runs it
        with login("carol"):
            rc, out = brain(self.repo, "claim", "--key", "PAY-5", "--title", "x")
        self.assertIn("Claimed PAY-5 for carol", out)


class ClaimRules(unittest.TestCase):
    """The pre-receive rules for claim/<KEY> with an owner that can be none (each rule once)."""

    def setUp(self):
        self.repo = git_repo(os.path.join(tmpdir(self), "r"))
        commit_file(self.repo, "README", "x\n")
        self.parent = claims._commit(self.repo, claims.claim_doc("PAY-1", "alice", "big"))
        sh("git", "-C", self.repo, "update-ref", "refs/heads/claim/PAY-1", self.parent)

    def child(self, **over):
        body = "Unit api of PAY-1."
        doc = claims.claim_doc("PAY-2", None, "api · x", {
            "source": "split", "body": body, "acs": [], "refs": [], "hash": intents.digest("split", "api · x", body),
            "filed_by": "alice", "parent": "PAY-1", "unit": "api", "expected_paths": ["services/api/**"],
            "depends_on": [], "contracts": [], "split": {"intent": "261109-pay-1-big", "branch": "ai/alice/PAY-1-big",
                                                         "commit": "a" * 40, "design_sha256": "b" * 64,
                                                         "units_sha256": "c" * 64}})
        doc["created_at"] = "2026-11-09T07:00:00Z"
        doc.update(over)
        return doc

    def push(self, old, doc, user, key="PAY-2"):
        new = claims._commit(self.repo, doc, parent=None if old == ZERO_SHA else old) if doc is not None else ZERO_SHA
        return new, pre_receive.decide([(old, new, "refs/heads/claim/" + key)], user, pre_receive.DEFAULT_CFG,
                                       cwd=self.repo)

    def test_filed_unowned_by_the_parent_owner_only(self):
        tip, r = self.push(ZERO_SHA, self.child(), "alice")
        self.assertEqual(r, [])
        self.assertIn("filed by PAY-1's owner (alice), not by bob",
                      self.push(ZERO_SHA, self.child(filed_by="bob"), "bob")[1][0])
        self.assertIn("must name owner bob, or no owner and filed_by bob",
                      self.push(ZERO_SHA, self.child(), "bob")[1][0])
        self.assertIn("bot accounts may not claim", self.push(ZERO_SHA, self.child(), "brain-bot")[1][0])
        # content checks apply as for any intent
        leak = self.child(body="call someone@example.com", hash=intents.digest("split", "api · x",
                                                                              "call someone@example.com"))
        self.assertIn("personal data", self.push(ZERO_SHA, leak, "alice")[1][0])
        nobody = {k: v for k, v in self.child().items() if k != "filed_by"}
        self.assertIn("no owner and filed_by alice", self.push(ZERO_SHA, nobody, "alice")[1][0])
        self.assertIn("a claim without an owner needs filed_by (who filed it)", intents.doc_problems(nobody))

    def test_owner_from_none_to_the_pusher_only(self):
        tip, _ = self.push(ZERO_SHA, self.child(), "alice")
        mine, r = self.push(tip, self.child(owner="bob", claimed_at="2026-11-09T08:00:00Z"), "bob")
        self.assertEqual(r, [])
        # not to someone else, and claiming changes nothing else
        self.assertIn("from none to the person who pushes only", self.push(tip, self.child(owner="carol"),
                                                                           "bob")[1][0])
        self.assertIn("claiming changes only the owner (it also changed expected_paths)", self.push(
            tip, self.child(owner="bob", expected_paths=["web/**"]), "bob")[1][0])

    def test_nobody_takes_a_claimed_one_and_the_owner_releases(self):
        tip, _ = self.push(ZERO_SHA, self.child(), "alice")
        bobs, _ = self.push(tip, self.child(owner="bob"), "bob")
        self.assertIn("claimed by bob", self.push(bobs, self.child(owner="carol"), "carol")[1][0])
        self.assertIn("claimed by bob", self.push(bobs, self.child(owner=None), "carol")[1][0])
        self.assertIn("nobody may take a claimed one", self.push(bobs, self.child(owner="carol"), "bob")[1][0])
        self.assertEqual(self.push(bobs, self.child(owner=None), "bob")[1], [])          # back to none
        self.assertIn("a release changes only the owner", self.push(bobs, self.child(owner=None, title="t"),
                                                                    "bob")[1][0])
        self.assertIn("claimed by bob", self.push(bobs, None, "alice")[1][0])            # nor delete it

    def test_unowned_is_managed_by_who_filed_it_or_the_parents_owner(self):
        tip, _ = self.push(ZERO_SHA, self.child(), "alice")
        closed = self.child(closed={"at": "2026-11-09T08:00:00Z", "by": "alice", "reason": "unit gone"})
        shut, r = self.push(tip, closed, "alice")
        self.assertEqual(r, [])
        self.assertIn("closed; a closed requirement is not claimed", self.push(shut, dict(closed, owner="bob"),
                                                                               "bob")[1][0])
        self.assertIn("only alice (who filed it) or the parent's owner may change it",
                      self.push(tip, closed, "bob")[1][0])
        self.assertIn("parent, unit never change", self.push(tip, self.child(parent="PAY-9", unit="web"),
                                                             "alice")[1][0])
        self.assertIn("never deleted", self.push(tip, None, "bob")[1][0])     # a child is closed, not deleted
        # the parent's new owner manages the children it left unowned
        p2 = claims._commit(self.repo, claims.claim_doc("PAY-1", "carol", "big"), parent=self.parent)
        sh("git", "-C", self.repo, "update-ref", "refs/heads/claim/PAY-1", p2)
        self.assertEqual(self.push(tip, closed, "carol")[1], [])

    def test_a_child_is_filed_unowned_by_the_parents_owner_only(self):
        """Review #1: an owned child (owner mallory, parent PAY-1) was accepted; now no one files an owned child, and
        only PAY-1's owner files its children."""
        self.assertIn("filed by PAY-1's owner (alice), not by mallory",
                      self.push(ZERO_SHA, self.child(owner="mallory", filed_by="mallory"), "mallory")[1][0])
        self.assertIn("a child of a split is filed unowned", self.push(ZERO_SHA, self.child(owner="alice"),
                                                                       "alice")[1][0])
        self.assertIn("filed by PAY-1's owner", self.push(ZERO_SHA, {k: v for k, v in self.child(owner="mallory")
                                                                     .items() if k != "filed_by"}, "mallory")[1][0])
        # filed_by names the pusher on any new claim, so a released claim cannot pretend someone else filed it
        plain = dict(claims.claim_doc("PAY-8", "mallory", "t"), filed_by="alice")
        self.assertIn("filed_by mallory", pre_receive.decide([(ZERO_SHA, claims._commit(self.repo, plain),
                                                               "refs/heads/claim/PAY-8")], "mallory",
                                                             pre_receive.DEFAULT_CFG, cwd=self.repo)[0])

    def test_a_child_is_never_deleted_and_its_split_stays_while_owned(self):
        """Review #1 (delete and re-create) and #4 (an owner rewriting split.branch)."""
        tip, _ = self.push(ZERO_SHA, self.child(), "alice")
        self.assertIn("never deleted", self.push(tip, None, "alice")[1][0])
        bobs, _ = self.push(tip, self.child(owner="bob"), "bob")
        self.assertIn("never deleted", self.push(bobs, None, "bob")[1][0])
        moved = self.child(owner="bob", split=dict(self.child()["split"], branch="ai/bob/PAY-2-api"))
        self.assertIn("split never changes while someone owns the claim", self.push(bobs, moved, "bob")[1][0])
        self.assertEqual(self.push(bobs, self.child(owner="bob", expected_paths=["services/api/**", "x/**"]),
                                   "bob")[1], [])                  # its owner takes a newer split's paths

    def test_old_claims_and_rules_unchanged(self):
        plain = claims.claim_doc("PAY-7", "alice", "t")
        old = claims._commit(self.repo, plain)
        self.assertEqual(pre_receive.decide([(ZERO_SHA, old, "refs/heads/claim/PAY-7")], "alice",
                                            pre_receive.DEFAULT_CFG, cwd=self.repo), [])
        self.assertIn("must name", pre_receive.decide([(ZERO_SHA, old, "refs/heads/claim/PAY-7")], "bob",
                                                      pre_receive.DEFAULT_CFG, cwd=self.repo)[0])
        released = claims._commit(self.repo, dict(plain, owner=None, filed_by="alice"), parent=old)
        self.assertEqual(pre_receive.decide([(old, released, "refs/heads/claim/PAY-7")], "alice",
                                            pre_receive.DEFAULT_CFG, cwd=self.repo), [])


class RemoteTake(unittest.TestCase):
    """Two people take the same unowned claim: the first push wins, the second gets a clear message."""

    def test_first_push_wins(self):
        d = tmpdir(self)
        seed = git_repo(os.path.join(d, "seed"))
        commit_file(seed, "README", "x\n")
        origin = os.path.join(d, "origin.git")
        sh("git", "clone", "-q", "--bare", seed, origin)
        a, b = os.path.join(d, "a"), os.path.join(d, "b")
        for c in (a, b):
            sh("git", "clone", "-q", origin, c)
            with open(os.path.join(c, ".git", "config"), "a", encoding="utf-8") as f:
                f.write("[user]\n\tname = t\n\temail = t@example.com\n" + no_hooks_config(c))
        claims.claim(a, "PAY", None, "unit api", key="PAY-2", extra={"filed_by": "alice"})
        stale = claims._tip_doc(b, "PAY-2")                       # carol reads the tip before bob's push lands
        self.assertEqual(claims.take(a, "PAY-2", "bob")["owner"], "bob")
        with mock.patch.object(claims, "_tip_doc", side_effect=[stale, claims._tip_doc(b, "PAY-2")]):
            with self.assertRaises(BrainError) as e:
                claims.take(b, "PAY-2", "carol")
        self.assertIn("PAY-2 was just claimed by bob", str(e.exception))
        self.assertEqual(claims.read(b, "PAY-2")["owner"], "bob")
        with self.assertRaises(BrainError) as e:
            claims.take(b, "PAY-2", "carol")
        self.assertIn("PAY-2 is claimed by bob", str(e.exception))
        self.assertEqual(claims.release(a, "PAY-2", "bob")["owner"], None)
        self.assertEqual(claims.take(b, "PAY-2", "carol")["owner"], "carol")


class Pieces(unittest.TestCase):
    def test_units_as_lean_reads_them(self):
        units, probs = split.parse_units(UNITS.format(rows="\n".join(ROWS)))
        self.assertEqual(probs, [])
        self.assertEqual([(u["name"], u["paths"], u["depends_on"]) for u in units],
                         [("api", ["services/api/**", "aidlc-docs/contracts/api/**"], []), ("web", ["web/**"], ["api"]),
                          ("jobs", ["jobs/**"], [])])
        self.assertEqual(units[0]["stories"], ["US-1"])
        _, probs = split.parse_units(UNITS.format(rows="\n".join([
            "| a | a | a/ | b | - | - | - |", "| b | b | b/ | a | - | - | - |", "| c | c | | z | - | - | - |",
            "| plans | x | p/ | - | - | - | - |"])))
        self.assertTrue(any("circle" in p for p in probs), probs)
        self.assertTrue(any("unit c has no Paths" in p for p in probs), probs)
        self.assertTrue(any("depends on z" in p for p in probs), probs)
        self.assertTrue(any("'plans' cannot be a folder name" in p for p in probs), probs)

    def test_dirty_keeps_the_first_rows_path(self):
        """cli._dirty: git status --porcelain rows start with a space (" M path"); the first must not lose it, or
        the hand-over's ignore list (run.json, MR.md) misses it."""
        repo = git_repo(os.path.join(tmpdir(self), "r"))
        commit_file(repo, "runs/PAY-1/MR.md", "a\n")
        write(repo, "runs/PAY-1/MR.md", "b\n")
        self.assertEqual(cli._dirty(repo, ignore=("runs/PAY-1/MR.md",)), [])
        self.assertEqual(cli._dirty(repo), [" M runs/PAY-1/MR.md"])

    def test_8_sync_merges_the_header_three_ways(self):
        base = "# S\nIntent: x\nOwner team: lead\n%s\nregion\n%s\n## Session notes\n" % (split.BEGIN_MARK,
                                                                                         split.END_MARK)
        theirs = base.replace("Owner team: lead", "Owner team: payments")
        text, conflicts = split.merge_state(base, base, theirs)
        self.assertEqual(conflicts, [])
        self.assertIn("Owner team: payments", text)               # their edit is kept, not silently ours
        text, conflicts = split.merge_state(base, base.replace("lead", "ops"), theirs)
        self.assertEqual(conflicts, ["(header)"])

    def test_9_team_names_as_lean_compares_them(self):
        from brainkit import lean as L
        notes = "## Session notes\n### Next (team web-ui)\nStatus: x\n1. step\n### Other\n"
        self.assertEqual(L.team_next_steps(notes, split.lean_team("web-ui")), ["Status: x", "step"])
        _, probs = split.parse_units(UNITS.format(rows="| a-b | - | a/ | - | - | - | - |\n"
                                                     "| a_b | - | b/ | - | - | - | - |"))
        self.assertTrue(any("would be one Lean team (a_b)" in p for p in probs), probs)

    def test_11_released_without_its_release_md(self):
        repo = git_repo(os.path.join(tmpdir(self), "r"))
        intent = "261109-pay-1-big"
        ev = {"seq": 1, "ts": "2026-11-09T08:00:00.000Z", "event": "APPROVED", "intent": intent,
              "data": {"kind": "RELEASE", "path": "aidlc-docs/%s/operations/release.md" % intent, "sha256": "a" * 64}}
        write(repo, "aidlc-docs/audit/host-abcdef.jsonl", json.dumps(ev) + "\n")
        self.assertTrue(split.released(mrdesc._Tree(repo, None), intent))

    def test_13_ci_lends_the_parents_design_only_to_a_claimed_child(self):
        repo = git_repo(os.path.join(tmpdir(self), "r"))
        commit_file(repo, "README", "x\n")
        body = "Unit api of PAY-1."
        sp = {"intent": "261109-pay-1-big", "branch": "ai/alice/PAY-1-big", "commit": "a" * 40,
              "design_sha256": "b" * 64, "units_sha256": "c" * 64}
        claims.claim(repo, "PAY", None, "api · x", key="PAY-2", extra={
            "source": "split", "body": body, "acs": [], "refs": [], "hash": intents.digest("split", "api · x", body),
            "filed_by": "alice", "parent": "PAY-1", "unit": "api", "expected_paths": ["a/**"], "depends_on": [],
            "contracts": [], "split": sp})
        self.assertTrue(ci._claimed_child(repo, {"key": "PAY-2", "parent": "PAY-1", "unit": "api"}))
        self.assertFalse(ci._claimed_child(repo, {"key": "PAY-2", "parent": "PAY-9", "unit": "api"}))
        self.assertFalse(ci._claimed_child(repo, {"key": "PAY-3", "parent": "PAY-1", "unit": "api"}))

    def test_15_a_refusing_server_is_not_a_taken_key(self):
        d = tmpdir(self)
        seed = git_repo(os.path.join(d, "seed"))
        commit_file(seed, "README", "x\n")
        origin = os.path.join(d, "origin.git")
        sh("git", "clone", "-q", "--bare", seed, origin)
        hook = os.path.join(origin, "hooks", "pre-receive")
        write(origin, "hooks/pre-receive", "#!/bin/sh\necho 'GL-HOOK-ERR: no claims today' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        sh("git", "-C", origin, "config", "core.hooksPath", os.path.join(origin, "hooks"))
        c = os.path.join(d, "c")
        sh("git", "clone", "-q", origin, c)
        with open(os.path.join(c, ".git", "config"), "a", encoding="utf-8") as f:
            f.write("[user]\n\tname = t\n\temail = t@example.com\n" + no_hooks_config(c))
        with self.assertRaises(BrainError) as e:
            claims.claim(c, "PAY", "alice", "t")
        self.assertIn("could not push claim PAY-1", str(e.exception))
        self.assertIn("no claims today", str(e.exception))

    def test_paths_overlap(self):
        self.assertTrue(paths_overlap("services/api/**", "services/api/handlers.py"))
        self.assertTrue(paths_overlap("web/*.ts", "web/src/a.ts"))
        self.assertTrue(paths_overlap("src/refund", "src/refund/fees.py"))
        self.assertFalse(paths_overlap("services/api/**", "services/apix/a.py"))
        self.assertFalse(paths_overlap("web/**", "jobs/**"))

    def test_session_notes_merge_by_team_subsection(self):
        base = "# S\n%s\nregion\n%s\n## Session notes\nStatus: x\n### Next\n1. a\n" % (split.BEGIN_MARK,
                                                                                    split.END_MARK)
        ours = base + "### Next (team api)\n1. api step 2\n"
        theirs = base.replace("region", "other region") + "### Next (team jobs)\n1. jobs step 1\n"
        text, conflicts = split.merge_state(base, ours, theirs)
        self.assertEqual(conflicts, [])
        self.assertIn("### Next (team api)\n1. api step 2", text)
        self.assertIn("### Next (team jobs)\n1. jobs step 1", text)
        self.assertIn("\nregion\n", text)                         # ours; Lean writes the region again after
        text, conflicts = split.merge_state(base, base.replace("Status: x", "Status: y"),
                                            base.replace("Status: x", "Status: z"))
        self.assertEqual(conflicts, ["(top)"])
        self.assertIn("<<<<<<< ours", text)


if __name__ == "__main__":
    unittest.main()
