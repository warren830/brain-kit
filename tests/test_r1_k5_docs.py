"""Round 1 (K5), decided as docs: R1-13 what 3.4 says happens while GitLab GraphQL is down; R1-20 a split child may
not change risk paths (4.7), and brain-run-check says why when one does."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit.bridge import decisions  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def usage_doc():
    with open(os.path.join(ROOT, "docs", "安装和使用.md"), encoding="utf-8") as f:
        return f.read()


def section(text, head):
    start = text.index(head)
    m = re.compile(r"^#{2,3} ", re.M).search(text, start + len(head))
    return text[start:m.start() if m else len(text)]


class R13GraphqlDownDoc(unittest.TestCase):
    """3.4 said a touched reply does not count while GraphQL is down and the bridge asks for another one; the bridge
    holds it instead, checks it again each round, and tells the MR that no new reply is needed."""

    def bullet(self):
        lines = [l for l in section(usage_doc(), "### 3.4").splitlines() if "GraphQL" in l]
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    def test_the_doc_says_the_answer_waits(self):
        b = self.bullet()
        self.assertFalse("这个接口不通时，被动过的回复不算，桥会请回答的人另发一条" in b, "the old sentence is still there")
        for part in ("下一轮再查", "不用重新回复", "edit_check_rounds", "edit_check_wait_minutes"):
            self.assertIn(part, b)

    def test_the_bridge_tells_the_mr_the_same(self):
        told = []

        class Ledger:
            def bump(self, key, run_id=None):
                return 3

            def notice(self, key):
                return {"created": "2026-11-10T00:00:00Z"}

        class Loop:
            ledger, cfg = Ledger(), {}

            def out(self, text):
                pass

            def tell_mr_once(self, key, project, iid, text, run_id=None):
                told.append(text)

        decisions.DecisionLoop._edits_unreadable(Loop(), {"id": "D-1", "run_id": "RUN-PAY-1-1", "project": 1,
                                                          "iid": 5})
        self.assertEqual(len(told), 1)
        self.assertIn("回答先等着", told[0])
        self.assertIn("不用重新回复", told[0])


if __name__ == "__main__":
    unittest.main()
