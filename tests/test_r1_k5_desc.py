"""Round-1 review fixes in the MR description (brainkit/mrdesc.py) and a split child's `brain handoff` (cli.py).

Synthetic worlds with exact records (no Lean install: fast). Each test is the reviewer's repro of one entry of
delivery-loop/round-1-bugs.md, and fails on 0d52e97."""
import hashlib
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import cli, config, mrdesc  # noqa: E402
from tests import test_split_trace as ST  # noqa: E402  (a module: its TestCase classes are not collected again)
from tests.helpers import LEAN_SRC, sh, tmpdir  # noqa: E402
from tests.test_trace import commit, git_repo  # noqa: E402

INTENT, DOCS = ST.INTENT, ST.DOCS               # one intent, aidlc-docs/<intent>/
BR = "ai/alice/PAY-1-refund"
REQ = DOCS + mrdesc.REQ_DOC
TEMPLATE = os.path.join(LEAN_SRC, "kit", ".kiro", "aidlc-lean", "templates", "requirements.md")
STARTED = [("2026-11-01T00:00:00.000Z", "INTENT_CREATED", {"title": "refund"}),
           ("2026-11-01T00:00:01.000Z", "PHASE_SKIPPED", {"phase": "ideation", "stages": [], "reason": "not used"}),
           ("2026-11-01T00:00:02.000Z", "STAGE_STARTED", {"stage": "requirements", "unit": None})]


def run_world(test, files=None, events=STARTED, main=None):
    """main (kit.json and `main` files) and PAY-1's run branch, checked out: run.json, the Lean audit (`events`, one
    clone), `files`."""
    repo = git_repo(os.path.join(tmpdir(test), "proj"))
    base = commit(repo, dict({".brain/kit.json": json.dumps(ST.KIT), "README.md": "x\n", "src/app.py": "x = 1\n"},
                             **(main or {})), "init", "2026-10-30T00:00:00Z", "lead@example.com")
    sh("git", "-C", repo, "switch", "-q", "-c", BR)
    commit(repo, dict({"runs/PAY-1/run.json": ST.run_json("PAY-1", "alice", BR, base, "2026-11-01T00:00:00Z"),
                       "aidlc-docs/audit/hosta-aaaaaa.jsonl": ST.audit(events, "alice@example.com")}, **(files or {})),
           "PAY-1: start run", "2026-11-01T01:00:00Z")
    return repo, config.load(repo)


def write(repo, rel, text):
    p = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def brain(repo, *args):
    out = io.StringIO()
    cwd = os.getcwd()
    os.chdir(repo)
    try:
        with redirect_stdout(out), redirect_stderr(out):
            rc = cli.main(list(args))
    finally:
        os.chdir(cwd)
    return rc, out.getvalue()


def isolated(test):
    """No GitLab token, no intake store, HOME in a temp folder."""
    env = mock.patch.dict(os.environ, {"HOME": tmpdir(test), "BRAIN_INTAKE_DIR": os.path.join(tmpdir(test), "none")})
    env.start()
    test.addCleanup(env.stop)
    for k in ("GITLAB_TOKEN", "BRAIN_ALLOWED_SIGNERS", "BRAIN_USER", "AIDLC_LEAN_TEAM"):
        os.environ.pop(k, None)


class ChildHandoffCommitsFirst(unittest.TestCase):
    """R1-87: a split child's `brain handoff` wrote MR.md before its commit: the description it committed said
    「另有 1 个改动没提交」 (its own run.json) and left that change out."""

    def setUp(self):
        isolated(self)
        self.w = ST.family_world(self)
        self.repo = self.w.repo
        sh("git", "-C", self.repo, "switch", "-q", "ai/bob/PAY-3-web")
        commit(self.repo, {ST.DOCS + "aidlc-state.md": "# State\n\n## Session notes\n### Next (team web)\n"
                                                         "Status: web done\n1. review and merge\n"},
               "PAY-3: notes", "2026-11-04T13:00:00Z", "bob@example.com")
        os.environ["BRAIN_USER"] = "bob"

    def test_the_committed_description_is_of_the_commit(self):
        before = sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip()
        rc, out = brain(self.repo, "handoff", "--no-push")
        self.assertEqual(rc, 0, out)
        # one commit on top, with run.json's request to merge and the description; nothing left uncommitted
        self.assertEqual(sh("git", "-C", self.repo, "rev-parse", "HEAD^").stdout.strip(), before)
        self.assertEqual(sh("git", "-C", self.repo, "show", "--name-only", "--format=", "HEAD").stdout.split(),
                         ["runs/PAY-3/MR.md", "runs/PAY-3/run.json"])
        self.assertEqual(sh("git", "-C", self.repo, "status", "--porcelain").stdout, "")
        self.assertIn("mr", json.loads(sh("git", "-C", self.repo, "show", "HEAD:runs/PAY-3/run.json").stdout))
        committed = sh("git", "-C", self.repo, "show", "HEAD:runs/PAY-3/MR.md").stdout
        self.assertNotIn("没提交", committed)
        self.assertIn("| runs（run 记录） | 1 |", committed)            # run.json's change is in the MR
        # made again from the same commit, the description is the same
        rc, now = brain(self.repo, "mr-desc", "PAY-3")
        self.assertEqual(rc, 0, now)
        self.assertEqual(now, committed)
        self.assertIn("Wrote runs/PAY-3/MR.md from the records into the ready-to-merge commit", out)



@unittest.skipUnless(os.path.isfile(TEMPLATE), "AI-DLC Lean checkout not found")
class JustStarted(unittest.TestCase):
    """R1-97: right after `brain start` the description said both 「需求还没写」 and REQUIREMENTS 「写了，还没请求审批」."""

    def setUp(self):
        isolated(self)
        with open(TEMPLATE, encoding="utf-8") as f:
            tpl = f.read().replace("{{TITLE}}", "refund").replace("{{INTENT}}", INTENT)
        self.repo, self.cfg = run_world(self, {REQ: tpl})

    def test_lean_s_template_is_not_written(self):
        d = mrdesc.build(self.repo, self.cfg, "PAY-1")
        self.assertEqual([(m["kind"], m["state"]) for m in d["approvals"]["missing"]][:1], [("REQUIREMENTS",
                                                                                              "template")])
        text = mrdesc.render(d)
        self.assertIn("- 还缺：REQUIREMENTS（还是模板，还没写）；", text)
        self.assertIn("- 需求还没写（requirements.md 还是模板）", text)
        self.assertNotIn("写了，还没请求审批", text)
        # written (a Goal, an acceptance criterion), it is a draft
        write(self.repo, REQ, "# Requirements: refund\n\n## Goal\n退款。\n\n## Acceptance criteria\n"
                              "| ID | Given / when | Then (expected) |\n|---|---|---|\n| AC-1 | 3 天前的订单 | 退款成功 |\n")
        text = mrdesc.render(mrdesc.build(self.repo, self.cfg, "PAY-1"))
        self.assertIn("- 还缺：REQUIREMENTS（写了，还没请求审批）；", text)
        self.assertNotIn("需求还没写", text)



def lean_project(repo):
    """The pinned Lean's Project on repo (its own rules, read-only here)."""
    sys.path.insert(0, os.path.join(LEAN_SRC, "kit", ".kiro", "aidlc-lean"))
    try:
        import aidlc_lean
    finally:
        sys.path.pop(0)
    return aidlc_lean.Project(repo)


@unittest.skipUnless(os.path.isfile(TEMPLATE), "AI-DLC Lean checkout not found")
class IdeationSkipped(unittest.TestCase):
    """R1-103: Ideation skipped with `lean stage skip ideation` (also after re-opening it) still showed 「还缺：IDEATION」;
    Lean's rule (Project.ideation_in_use): a start or done record newer than the last phase skip."""

    def at(self, n):
        return "2026-11-01T00:%02d:00.000Z" % n

    def missing(self, events):
        repo, cfg = run_world(self, {REQ: "# Requirements: refund\n\n## Goal\n退款。\n"}, events=events)
        kinds = [m["kind"] for m in mrdesc.build(repo, cfg, "PAY-1")["approvals"]["missing"]]
        self.assertEqual("IDEATION" in kinds, lean_project(repo).ideation_in_use(INTENT))   # as Lean says
        return kinds

    def test_skipped_reopened_skipped_again(self):
        isolated(self)
        new = [(self.at(0), "INTENT_CREATED", {"title": "refund"}),
               (self.at(1), "PHASE_SKIPPED", {"phase": "ideation", "stages": [], "reason": "not used"})]
        reopen = [(self.at(2), "STAGE_STARTED", {"stage": "intent-capture", "unit": None})]
        skip = [(self.at(3), "PHASE_SKIPPED", {"phase": "ideation", "stages": ["intent-capture"], "reason": "no"})]
        self.assertNotIn("IDEATION", self.missing(new))
        self.assertEqual(self.missing(new + reopen)[0], "IDEATION")          # re-opened: in use
        self.assertNotIn("IDEATION", self.missing(new + reopen + skip))      # skipped again: not

    def test_new_with_ideation_then_skipped(self):
        isolated(self)
        events = [(self.at(0), "INTENT_CREATED", {"title": "refund"}),
                  (self.at(1), "STAGE_STARTED", {"stage": "intent-capture", "unit": None}),
                  (self.at(2), "STAGE_COMPLETED", {"stage": "intent-capture", "unit": None, "note": "x"})]
        self.assertEqual(self.missing(events)[0], "IDEATION")
        events.append((self.at(3), "PHASE_SKIPPED", {"phase": "ideation", "stages": ["market-research"],
                                                    "reason": "no ideation after all"}))
        self.assertNotIn("IDEATION", self.missing(events))



REVIEW = """# Review: 退款上限

| Finding | Severity | File:line | Fix |
|---|---|---|---|
| 金额比较用了浮点数 | high | src/refund.py:2 | {high} |
| 缺少 422 的错误码文档 | low | src/refund.py:2 | fixed |

结论：{end}
"""


class ReviewFixesRead(unittest.TestCase):
    """R1-105: a review.md as Lean's release skill writes it (finding, severity, file:line, fix), every finding fixed:
    「怎么审」 still said a high-severity finding was open."""

    def describe(self, high, end="两条都修了，可以发布。"):
        isolated(self)
        repo, cfg = run_world(self, {DOCS + mrdesc.REVIEW_DOC: REVIEW.format(high=high, end=end)})
        d = mrdesc.build(repo, cfg, "PAY-1")
        return d, mrdesc.render(d)

    def test_every_finding_fixed(self):
        d, text = self.describe("fixed：改成整数分（D-3）")
        self.assertFalse([p for p in d["pointers"] if "评审" in p], d["pointers"])
        self.assertEqual(d["review"]["high"], {"fixed": 1})
        self.assertIn("- review.md：2 条发现（高 1、低 1），都修了\n- 高：金额比较用了浮点数（修了）\n- 结论：两条都修了，可以发布。",
                      text)
        d, _ = self.describe("已修：改成整数分")
        self.assertEqual(d["review"]["high"], {"fixed": 1})

    def test_open_and_unmarked_findings_still_point_there(self):
        for cell in ("", "not fixed: 下个需求改", "未修"):
            d, text = self.describe(cell, end="还有一条没修。")
            self.assertIn("评审还有 1 条高严重度发现没修（review.md）", d["pointers"], cell)
            self.assertIn("- review.md：2 条发现（高 1、低 1），修了 1 条，没修 1 条\n- 高：金额比较用了浮点数（没修）", text)
        d, text = self.describe("改成整数分")         # a fix written; nothing says it was made
        self.assertIn("评审有 1 条高严重度发现，review.md 没写修了没有：确认修了", d["pointers"])
        self.assertIn("- 高：金额比较用了浮点数\n", text)



class DesignApprovedOnlyLocally(unittest.TestCase):
    """R1-93: a risk-path change whose DESIGN was approved only in a local Lean chat: the description said 「签名由 CI
    核对」, but CI's brain-run-check counts only a bridge-signed DESIGN approval and fails the MR."""

    def describe(self, words):
        isolated(self)
        design = DOCS + mrdesc.DESIGN_DOC
        text = "# Application Design: refund\n\n退款服务放在 src/pay/refund.py。\n"
        sha = hashlib.sha256(text.encode()).hexdigest()
        events = STARTED + [("2026-11-01T00:30:00.000Z", "APPROVED", dict(
            {"kind": "DESIGN", "path": design, "sha256": sha, "words": words}, **(
                {"claimed_channel": words.split("(via ", 1)[1].rstrip(")")} if "(via " in words else {})))]
        repo, cfg = run_world(self, {design: text, "src/pay/refund.py": "def refund():\n    return 1\n"}, events,
                              main={"gates/risk-paths": "src/pay/**\n"})
        d = mrdesc.build(repo, cfg, "PAY-1")
        return sha[:8], d, mrdesc.render(d)

    def test_a_local_approval_is_not_one_ci_counts(self):
        h, d, text = self.describe("approve DESIGN 1234abcd")
        self.assertNotIn("签名由 CI 核对", text)
        self.assertNotIn("CI 的 brain-run-check 会核对", text)
        self.assertEqual(d["changes"]["risk"]["design"]["state"], "lean_local")
        self.assertIn("- 设计批准：Lean 记了 DESIGN %s 的批准，但只是在本地会话里批的；CI 的 brain-run-check 不认它，只认桥签过的 "
                      "DESIGN 批准，没有就会拦（在 MR 上问：park.json 写 lean_request DESIGN …）。" % h, text)
        self.assertIn("改到了 risk path src/pay/refund.py：DESIGN %s 只在本地批过，CI 不认（要桥签的批准）" % h, d["pointers"])

    def test_the_bridge_s_approval_is_still_checked_by_ci(self):
        h, d, text = self.describe("approve DESIGN 1234abcd (via gitlab P-ABCD V-0123456789)")
        self.assertEqual(d["changes"]["risk"]["design"]["state"], "lean_approved")
        self.assertIn("改到了 risk path src/pay/refund.py：Lean 记了 DESIGN %s 的批准，签名由 CI 核对" % h, d["pointers"])


if __name__ == "__main__":
    unittest.main()
