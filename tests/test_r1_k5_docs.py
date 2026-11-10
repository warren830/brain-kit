"""Round 1 (K5), decided as docs: R1-13 what 3.4 says happens while GitLab GraphQL is down; R1-20 a split child may
not change risk paths (4.7), and brain-run-check says why when one does."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import ci  # noqa: E402
from brainkit.bridge import decisions  # noqa: E402
from tests.helpers import git_repo, sh, tmpdir  # noqa: E402

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


class R20SplitChildRiskPaths(unittest.TestCase):
    """A split child that changes a risk path can never pass brain-run-check: it is not handed to the runner, and
    the parent's DESIGN was approved in local Lean. 4.7 says so; the check says why and what to do."""
    INTENT = "261109-pay-1-big"

    @classmethod
    def setUpClass(cls):
        scope = type("Scope", (), {})()
        scope.addCleanup = cls.addClassCleanup
        cls.repo = repo = git_repo(os.path.join(tmpdir(scope), "proj"))
        design = "aidlc-docs/%s/inception/application-design/application-design.md" % cls.INTENT
        for rel, text in (("gates/risk-paths", "services/api/**\n"), (design, "# Application Design\n")):
            p = os.path.join(repo, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        sh("git", "-C", repo, "add", "-A")
        sh("git", "-C", repo, "commit", "-q", "-m", "base")
        cls.base = sh("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()
        os.makedirs(os.path.join(repo, "services", "api"))
        with open(os.path.join(repo, "services", "api", "refunds.py"), "w") as f:
            f.write("def refund(): pass\n")
        sh("git", "-C", repo, "add", "-A")
        sh("git", "-C", repo, "commit", "-q", "-m", "PAY-2: api")

    class Target:
        trusted = True

        def file(self, path):
            return "services/api/**\n" if path == "gates/risk-paths" else None

    def route(self, **rec):
        rec = dict({"key": "PAY-2", "intent": self.INTENT, "brain_commit": self.base}, **rec)
        return ci.check_route(self.repo, rec, {"risk_paths_file": "gates/risk-paths"}, self.Target(), self.base, [])

    def test_a_split_child_is_told_why_and_what_to_do(self):
        probs = self.route(parent="PAY-1", unit="api")
        self.assertEqual(len(probs), 1, probs)
        p = probs[0]
        self.assertTrue(p.startswith("risk paths changed (services/api/refunds.py): no design approval signed by the "
                                     "bridge for PAY-2 or its parent PAY-1"), p)
        for part in ("split child", "cannot get one", "local Lean", "do not split", "parent run"):
            self.assertIn(part, p)
        self.assertNotIn("ask through the MR", p)          # what a normal run does; a child cannot

    def test_a_normal_run_keeps_its_message(self):
        self.assertEqual(self.route(), ["risk paths changed (services/api/refunds.py): no design approval signed by "
                                        "the bridge for PAY-2 (ask through the MR: park.json with lean_request "
                                        "DESIGN ...)"])

    def test_the_doc_says_a_child_may_not_change_risk_paths(self):
        s = section(usage_doc(), "### 4.7")
        self.assertFalse("CI 查 risk-paths 时，大需求经桥签过的 DESIGN 批准也算数。" in s, "the old sentence is still there")
        for part in ("小需求不能改 risk-paths", "不拆", "让大需求自己改"):
            self.assertIn(part, s)


if __name__ == "__main__":
    unittest.main()
