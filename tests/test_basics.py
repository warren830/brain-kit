import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import brainlint, config, lean, runrec  # noqa: E402
from brainkit.common import parse_branch  # noqa: E402
from tests.helpers import tmpdir  # noqa: E402

SHA = "a" * 40


def rec(**over):
    r = runrec.new("PAY-12", "alice", "ai/alice/PAY-12-refund", "261108-pay-12-refund", SHA, "0.1.0", [],
                   "2026-11-08T00:00:00Z", True)
    r.update(over)
    return r


class Names(unittest.TestCase):
    def test_parse_branch(self):
        self.assertEqual(parse_branch("ai/alice/PAY-12-refund"), {"owner": "alice", "key": "PAY-12", "slug": "refund"})
        self.assertEqual(parse_branch("ai/alice/PAY-12")["slug"], "")
        for bad in ("ai/Alice/PAY-12-x", "ai/alice/pay-12-x", "feature/alice/PAY-12-x", "ai/alice", "ai/alice/PAY-0-x",
                    "ai/alice/PAY-12-x/y", "ai/../PAY-1-x"):
            self.assertIsNone(parse_branch(bad), bad)


class RunRecord(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(runrec.validate(rec(), branch="ai/alice/PAY-12-refund"), [])

    def test_mismatch_and_bad_fields(self):
        self.assertIn("run.json is for branch", " ".join(runrec.validate(rec(), branch="ai/bob/PAY-12-refund")))
        self.assertTrue(runrec.validate(rec(owner="bob")))  # branch says alice
        self.assertTrue(runrec.validate(rec(brain_commit="abc")))
        self.assertTrue(runrec.validate(rec(intent="../x")))
        self.assertTrue(runrec.validate(rec(handoff={"requested_at": "x", "by": "bob"})))
        self.assertEqual(runrec.validate(rec(handoff={"requested_at": "x", "by": "alice"})), [])


class Config(unittest.TestCase):
    def test_model_allowlist(self):
        cfg = config.merged(config.DEFAULTS, {"model": "auto"})
        self.assertTrue(config.validate(cfg))
        cfg = config.merged(config.DEFAULTS, {"allowed_models": ["claude-opus-5.5", "kimi-k3", "auto"]})
        self.assertIn("allowed_models may not contain auto", config.validate(cfg))
        self.assertEqual(config.validate(config.merged(config.DEFAULTS, {})), [])


class HandOverNote(unittest.TestCase):
    def test_next_steps(self):
        text = "## Session notes\nStatus: Construction app step 3\n### Next\n1. run tests\n2. fix AC-2\n### Open\n- x\n"
        self.assertEqual(lean.next_steps(text), ["run tests", "fix AC-2"])
        self.assertEqual(lean.handover_text_problems(text), [])

    def test_template_rejected(self):
        text = "Status: Inception: starting\n### Next\n1. read code\n"
        self.assertTrue(lean.handover_text_problems(text))
        self.assertTrue(lean.handover_text_problems("Status: done\n### Next\n\n### Open questions\n"))


class BrainLint(unittest.TestCase):
    def write(self, d, name, text):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_ids(self):
        d = tmpdir(self)
        self.write(d, "TECH.md", "> <!-- id:T-PAY-1-1 type:pitfall --> example\n"
                                 "<!-- id:T-PAY-12-1 type:pitfall side:builder rel:new -->\nx\n"
                                 "<!-- id:T-012 type:decision rel:new -->\ny\n")
        errors, warnings = brainlint.check(d)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(brainlint.next_id(d, "T", "PAY-12"), "T-PAY-12-2")
        self.assertEqual(brainlint.next_id(d, "T", "PAY-1"), "T-PAY-1-1")  # quoted example is not an entry

    def test_duplicates_types_and_replaces(self):
        d = tmpdir(self)
        self.write(d, "TECH.md", "<!-- id:T-PAY-12-1 type:pitfall rel:new -->\n"
                                 "<!-- id:T-PAY-12-2 type:pitfall rel:replaces T-PAY-9-1 -->\n")
        self.write(d, "PRODUCT.md", "<!-- id:T-PAY-12-1 type:nonsense rel:new -->\n")
        errors, _ = brainlint.check(d)
        text = " ".join(errors)
        self.assertIn("already used", text)
        self.assertIn("type must be", text)
        self.assertIn("not in an *-archive.md", text)
        self.write(d, "TECH-archive.md", "<!-- id:T-PAY-9-1 type:pitfall rel:new status:replaced by:T-PAY-12-2 -->\n"
                                         "old\n")
        errors, _ = brainlint.check(d)
        self.assertNotIn("archive", " ".join(errors))


class TokenFile(unittest.TestCase):
    def test_owner_only(self):
        from brainkit import rest
        d = tmpdir(self)
        p = os.path.join(d, "tok")
        with open(p, "w") as f:
            f.write("glpat-test\n")
        os.chmod(p, 0o644)
        old = os.environ.pop("GITLAB_TOKEN", None)
        os.environ["BRAIN_GITLAB_TOKEN_FILE"] = p
        try:
            with self.assertRaises(Exception):
                rest.read_token()
            os.chmod(p, 0o600)
            self.assertEqual(rest.read_token(), "glpat-test")
        finally:
            del os.environ["BRAIN_GITLAB_TOKEN_FILE"]
            if old is not None:
                os.environ["GITLAB_TOKEN"] = old


if __name__ == "__main__":
    unittest.main()
