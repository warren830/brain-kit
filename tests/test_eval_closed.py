"""The user's decision (2026-11-10): a requirement closed with `brain intent close` is closed in `brain eval`, a split
child or not, whether its run branch is still there or not: not in progress, not stuck. When its run branch is still
here, eval names it (`leftover`) so its owner can delete it and close its MR."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brainkit import claims, evaluate as EV, intents  # noqa: E402
from brainkit.config import DEFAULTS, merged  # noqa: E402
from tests.test_qa_r3_trace import BR, start_run, temp_home  # noqa: E402
from tests.test_qa_trace_eval import KIT, World  # noqa: E402

AS_OF = "2026-11-20T00:00:00Z"          # days after the run's last commit: an open run would be stuck


class ClosedOrdinaryRequirement(unittest.TestCase):
    def setUp(self):
        temp_home(self)
        self.w = World(self)
        self.cfg = merged(DEFAULTS, KIT)
        claims.claim(self.w.repo, "PAY", "alice", "refund", key="PAY-1")
        start_run(self.w)                             # its run branch ai/alice/PAY-1-refund, not merged
        self.w.git("switch", "-q", "main")

    def collect(self):
        return EV.collect(self.w.repo, self.cfg, as_of=AS_OF)

    def test_control_open_and_stuck_while_its_claim_is_open(self):
        st = self.collect()["states"]["PAY-1"]
        self.assertEqual((st["value"], st["stuck"]), ("open", True))
        self.assertNotIn("closed_by", st)
        self.assertNotIn("leftover", st)

    def test_closed_is_closed_and_its_branch_is_named(self):
        intents.close(self.w.repo, "PAY-1", "不做了", by="alice")
        d = self.collect()
        st = d["states"]["PAY-1"]
        self.assertEqual((st["value"], st["closed_by"], st["stuck"]), ("closed", "claim", False))
        self.assertEqual(st["leftover"], {"branch": BR, "where": ["local"]})
        row = next(r for r in d["batch"] if r["key"] == "PAY-1")
        self.assertEqual((row["state"], row["closed_by"], row["leftover"]["branch"]), ("closed", "claim", BR))
        reasons = {r["rule"]: r for r in next(q["reasons"] for q in d["review_queue"] if q["key"] == "PAY-1")}
        self.assertNotIn("stuck", reasons)
        text = reasons["closed_unmerged"]["text"]
        self.assertTrue(text.startswith("需求关了（brain intent close），没有合并；分支 %s 还在（本地）" % BR), text)
        self.assertIn("`git branch -D %s`" % BR, text)
        self.assertIn("MR 如果还开着，也关掉", text)
        report = EV.report(d)
        self.assertIn("| PAY-1 | 需求关了，没合并 |", report)
        self.assertNotIn("PAY-1：", "\n".join(x for x in report.splitlines() if "没有进展" in x))

    def test_origin_copy_is_named_too(self):
        origin = os.path.join(self.w.d, "origin.git")
        self.w.git("clone", "-q", "--bare", self.w.repo, origin)
        self.w.git("remote", "add", "origin", origin)
        self.w.git("fetch", "-q", "origin")
        intents.close(self.w.repo, "PAY-1", "不做了", by="alice")
        self.assertEqual(self.collect()["states"]["PAY-1"]["leftover"], {"branch": BR, "where": ["local", "origin"]})


if __name__ == "__main__":
    unittest.main()
