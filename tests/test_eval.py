"""Eval v1 (brainkit/evaluate.py): metrics over run traces, the decision funnel, the review queue, gaps and the
`brain eval` commands. Uses the two-run world from tests/test_trace.py (PAY-2 messy, PAY-4 clean)."""
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import cli, claims, evaluate as EV  # noqa: E402
from brainkit.commands import eval_cmd  # noqa: E402
from brainkit.common import BrainError  # noqa: E402
from tests.helpers import sh  # noqa: E402
from tests.test_trace import AS_OF, world  # noqa: E402

CREDITS = {"runs": {"RUN-PAY-2-1": {"credits": 4.0, "manual": [{"at": "2026-11-01T05:30:00Z", "kind": "restart"}]},
                    "RUN-PAY-4-1": {"credits": 2}}}


class Eval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = os.path.realpath(tempfile.mkdtemp(prefix="brainkit-"))
        cls.addClassCleanup(shutil.rmtree, d, True)
        cls.w = world(cls, runs=("bad", "good"), d=d)
        cls.data = cls.collect()

    @classmethod
    def collect(cls, **kw):
        w = cls.w
        args = dict(ledger_path=w.ledger_path, vouchers_dir=w.vouchers, credits=CREDITS, as_of=AS_OF, min_runs=2)
        args.update(kw)
        return EV.collect(w.repo, w.cfg, **args)

    def test_autonomy_and_delivery(self):
        a, d = self.data["autonomy"], self.data["delivery"]
        self.assertEqual(a["value"], [1, 2])
        rows = {r["key"]: r for r in a["runs"]}
        self.assertEqual((rows["PAY-4"]["autonomous"], rows["PAY-4"]["correct"]), (True, "correct"))
        self.assertEqual(rows["PAY-2"]["beyond"], ["answer clarify", "platform_restart", "push_after_handover code",
                                                   "rejection RELEASE"])
        self.assertEqual(d["value"], [1, 2])
        self.assertEqual(d["by_run"], {"PAY-2": "wrong", "PAY-4": "correct"})

    def test_judgment_points(self):
        j = self.data["judgment"]
        self.assertEqual(j["value"], [8, 11])
        self.assertEqual([o["kind"] for o in j["outside"]], ["allow_test_change", "push_after_handover",
                                                             "platform_restart"])
        self.assertTrue(all(o["src"] for o in j["outside"]))

    def test_knowledge_metrics(self):
        dt = self.data
        self.assertEqual((dt["repeat_rate"], dt["useful"], dt["useful_loaded"]), ([2, 3], [1, 5], [2, 10]))
        s = dt["staleness"]
        self.assertEqual([x["ids"] for x in s["stale"]], [["T-PAY-9-1"]])
        self.assertEqual(len(s["same_term"]), 1)
        self.assertEqual([x["id"] for x in s["archived"]], ["T-PAY-9-1"])
        self.assertEqual(s["total"], 3)

    def test_cost(self):
        c = self.data["cost"]
        self.assertEqual((c["credits_total"], c["correct_deliveries"], c["per_success"]), (6.0, 1, 6.0))
        self.assertIsNone(c["tokens"])
        none = self.collect(credits=None)["cost"]
        self.assertEqual((none["credits_total"], none["per_success"]), (None, None))

    def test_funnel(self):
        f = self.data["funnel"]
        self.assertEqual(f["counts"], {"brain": 3, "check": 1, "agent": 2, "person": 6, "unknown": 1,
                                       "unclassified": 2})
        self.assertEqual(f["total"], 15)
        # Who "product" and "张三" are not the runner's vocabulary: listed where they are, never guessed or shown
        self.assertEqual([(x["key"], x["id"]) for x in f["unclassified"]], [("PAY-2", "D-2"), ("PAY-2", "D-5")])
        self.assertNotIn("张三", EV.report(self.data))
        self.assertEqual(sum(sum(v.values()) for v in f["weeks"].values()), 15)
        self.assertEqual(list(f["weeks"]), ["2026-W44", "2026-W45"])
        self.assertEqual(f["oracle_answers_excluded"], 1)

    def test_review_queue(self):
        q = {x["key"]: [r["rule"] for r in x["reasons"]] for x in self.data["review_queue"]}
        self.assertEqual(q["PAY-2"], ["acceptance_rejected", "reverted_or_hotfixed", "verify_fail_rounds",
                                      "repeat_corrections", "outside_judgment"])
        self.assertEqual(q["PAY-4"], ["stale_citations"])
        self.assertEqual(self.data["review_queue"][0]["key"], "PAY-2")
        t = next(t for t in self.data["traces"] if t["key"] == "PAY-2")
        self.assertEqual([r["rule"] for r in EV.review_reasons(t, median_credits=1.0)][-1], "cost_outlier")
        self.assertNotIn("cost_outlier", [r["rule"] for r in EV.review_reasons(t, median_credits=3.0)])

    def test_since(self):
        d = self.collect(since="2026-11-03")
        self.assertEqual(d["completed"], ["PAY-4"])
        self.assertEqual((d["delivery"]["value"], d["autonomy"]["value"], d["judgment"]["value"]),
                         ([1, 1], [1, 1], [3, 3]))
        self.assertEqual(d["funnel"]["counts"], {"brain": 2, "check": 0, "agent": 1, "person": 2, "unknown": 0,
                                                 "unclassified": 0})
        self.assertEqual(d["staleness"]["archived"], [])
        with self.assertRaises(BrainError):
            self.collect(since="last week")

    def test_pending_window_and_given_traces(self):
        d = self.collect(as_of="2026-11-10T00:00:00Z")
        # PAY-2 is already known wrong, but its 14 days are not over: it waits like PAY-4 does (N11)
        self.assertEqual((d["delivery"]["value"], d["delivery"]["pending"], d["delivery"]["wrong_open"]),
                         ([0, 0], ["PAY-4"], ["PAY-2"]))
        self.assertEqual(d["autonomy"]["value"], [0, 0])
        ts = copy.deepcopy(self.data["traces"])
        t4 = next(t for t in ts if t["key"] == "PAY-4")
        t4["outcome"]["handed_over"]["value"] = False
        t2 = next(t for t in ts if t["key"] == "PAY-2")
        t2["outcome"]["accepted"].update(first_time=None, rejections=0, value=None)
        t2["outcome"]["reverted_or_hotfixed_within_14d"]["value"] = False
        d = self.collect(traces=ts)
        self.assertEqual({r["key"]: r["status"] for r in d["autonomy"]["runs"]}, {"PAY-2": "pending",
                                                                                  "PAY-4": "local only"})
        self.assertEqual(d["delivery"]["unknown_acceptance"], ["PAY-2"])
        self.assertEqual(d["delivery"]["value"], [1, 1])

    def test_gaps(self):
        gs = EV.gaps(self.data)
        self.assertEqual([(g["kind"], g["key"]) for g in gs[:3]], [("knowledge", "PAY-2"), ("golden", "PAY-2"),
                                                                    ("test", None)])
        rej = gs[0]
        self.assertIn(self.w.runs["bad"].v[3], rej["refs"])
        self.assertIn("被驳回", rej["title"])
        self.assertEqual(gs[2]["title"], "P-PAY-1-1 一再重复：改成 gate")          # no count in the title (N8)
        self.assertIn("回来 2 次", gs[2]["body"])
        withdraw = [g["refs"][0] for g in gs if "撤回候选" in g["title"]]
        self.assertEqual(withdraw, ["P-PAY-1-1", "P-PAY-1-2", "P-PAY-1-3", "T-PAY-1-2"])
        self.assertEqual(len({g["id"] for g in gs}), len(gs))
        self.assertEqual(gs, EV.gaps(self.collect()))                       # stable ids, same order
        # a correction proposal citing the rejection's voucher closes that gap
        data = copy.deepcopy(self.data)
        t2 = next(t for t in data["traces"] if t["key"] == "PAY-2")
        t2["brain"]["proposals"]["correction_src"].append(self.w.runs["bad"].v[3])
        self.assertNotIn(rej["id"], [g["id"] for g in EV.gaps(data)])

    def test_report_text(self):
        text = EV.report(self.data)
        for s in ("自主度：1/2（50%）", "交付正确率：1/2（50%）", "介入落在判断点的比例：8/11（73%）",
                  "重复纠正率：2/3", "过时与冲突：3", "每个正确交付 6.0", "决策漏斗：15 条", "2026-W44",
                  "考题的自动回答（eval_oracle）：1 条，不算", "PAY-2：验收被驳回 1 次"):
            self.assertIn(s, text)

    def test_file_gaps_calls_intents_add(self):
        calls = []

        def add(repo, cfg, source, title, body, acs=None, refs=None, key=None, owner=None, extra=None):
            calls.append((source, title, key, extra, refs))
            if "golden" in title:
                raise BrainError("refused")
            return {"key": "PAY-%d" % (10 + len(calls)), "duplicate": len(calls) == 1}

        out = []
        gs = EV.gaps(self.data)
        bad = eval_cmd.file_gaps(self.w.repo, self.w.cfg, gs, add, out=out.append)
        self.assertEqual(bad, 1)
        self.assertEqual({c[0] for c in calls}, {"eval-gap"})
        self.assertEqual({c[2] for c in calls}, {None})                   # never the run's key: that is claimed
        self.assertEqual(calls[0][3], {"gap_kind": "knowledge", "found_in": "PAY-2"})
        self.assertEqual(calls[2][3], {"gap_kind": "test"})
        self.assertTrue(out[0].startswith("already filed PAY-11"))
        self.assertTrue(any(o.startswith("not filed") for o in out))

        def old_add(repo, cfg, source, title, body, acs=None, refs=None, key=None):
            calls.append(("old", key))
            return {"key": "PAY-99"}
        self.assertEqual(eval_cmd.file_gaps(self.w.repo, self.w.cfg, gs[:1], old_add, out=out.append), 0)
        self.assertEqual(calls[-1], ("old", None))

    def run_cli(self, *args):
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.w.repo)
        env = {"BRAIN_INTAKE_DIR": os.path.join(self.w.d, "no-intake"), "BRAIN_USER": "alice"}
        try:
            with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(out):
                rc = cli.main(list(args))
        finally:
            os.chdir(cwd)
        return rc, out.getvalue()

    def test_cli(self):
        common = ["--as-of", AS_OF, "--ledger", self.w.ledger_path, "--vouchers", self.w.vouchers]
        rc, out = self.run_cli("eval", *common)
        self.assertEqual(rc, 0, out)
        self.assertIn("自主度：1/2", out)
        rc, out = self.run_cli("eval", "--json", *common)
        self.assertEqual(json.loads(out)["delivery"]["value"], [1, 2])
        rc, out = self.run_cli("eval", "gaps", "--min-runs", "2", *common)
        self.assertEqual(rc, 0)
        self.assertIn("[golden]", out)
        self.assertIn("撤回候选", out)
        rc, out = self.run_cli("eval", "--since", "2026-11-03", "gaps", "--json", *common)
        self.assertEqual(rc, 0)
        self.assertTrue(isinstance(json.loads(out), list))
        with mock.patch.object(eval_cmd, "_intents_add", return_value=None):
            rc, out = self.run_cli("eval", "gaps", "--file", *common)
        self.assertEqual(rc, 0)
        self.assertIn("brainkit.intents is not available", out)
        self.assertIn("[knowledge]", out)
        rc, out = self.run_cli("eval", "--since", "nope")
        self.assertEqual(rc, 2)

    @unittest.skipUnless(eval_cmd._intents_add() is not None, "brainkit.intents not installed")
    def test_file_into_real_intents(self):
        d = os.path.realpath(tempfile.mkdtemp(prefix="brainkit-"))
        self.addCleanup(shutil.rmtree, d, True)
        w = world(self, runs=("bad",), d=d)
        for k in ("PAY-1", "PAY-2"):                                       # runs always have their claim
            claims.claim(w.repo, "PAY", "alice", "run %s" % k, key=k)
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(w.repo)
        env = {"BRAIN_INTAKE_DIR": os.path.join(d, "no-intake"), "BRAIN_USER": "alice"}
        try:
            with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(out):
                rc = cli.main(["eval", "gaps", "--file", "--as-of", AS_OF, "--vouchers", w.vouchers])
                rc2 = cli.main(["eval", "gaps", "--file", "--as-of", AS_OF, "--vouchers", w.vouchers])
        finally:
            os.chdir(cwd)
        self.assertEqual((rc, rc2), (0, 0), out.getvalue())
        refs = sh("git", "-C", w.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/claim/").stdout.split()
        self.assertGreaterEqual(len(refs), 3)
        self.assertNotIn("filed PAY-2:", out.getvalue())
        self.assertIn("already filed", out.getvalue())                    # the second round files nothing new
        doc = json.loads(sh("git", "-C", w.repo, "show", "claim/PAY-3:claim.json").stdout)
        self.assertEqual((doc["source"], doc.get("found_in")), ("eval-gap", "PAY-2"))


if __name__ == "__main__":
    unittest.main()
