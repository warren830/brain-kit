"""Round-1 review fixes, group K1 (hand-over, bridge, runner): regression tests for R1-01, 03, 05, 06, 07, 09, 14."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from brainkit import lean  # noqa: E402
from brainkit.common import load_json  # noqa: E402
from tests import test_cli as tc  # noqa: E402
from tests.helpers import LEAN_SRC, sh, tmpdir  # noqa: E402


class _Flow(unittest.TestCase):
    """test_cli.LocalFlow's set-up (a temp HOME, a project with the kit installed) without inheriting its tests."""
    setUp = tc.LocalFlow.setUp
    doc = tc.LocalFlow.doc

    def started(self, *extra):
        rc, out = tc.brain(self.repo, "start", "refund", "--key", "PAY-1", "--doc", self.doc(), "--classification",
                           "internal", "--consent", *extra)
        self.assertEqual(rc, 0, out)
        rec = load_json(os.path.join(self.repo, "runs", "PAY-1", "run.json"))
        st = lean.state_path(self.repo, rec["intent"])
        with open(st, encoding="utf-8") as f:
            text = f.read().replace(lean.TEMPLATE_STATUS, "Status: Inception: requirements drafted")
        with open(st, "w", encoding="utf-8") as f:
            f.write(text)
        sh("git", "-C", self.repo, "commit", "-q", "-am", "work")
        proj = os.path.join(tmpdir(self), "proj.git")
        sh("git", "init", "-q", "--bare", "-b", "main", proj)
        sh("git", "-C", self.repo, "remote", "add", "origin", proj)
        return rec


@unittest.skipUnless(os.path.exists(os.path.join(LEAN_SRC, "install.sh")), "AI-DLC Lean checkout not found")
class StoreInsideAnotherRepo(_Flow):
    """R1-03: the default store ~/.brain/intake inside a dotfiles repo at $HOME is not a brain-intake clone."""

    def test_handoff_does_not_publish_to_an_enclosing_repo(self):
        home = os.environ["HOME"]
        dot = os.path.join(tmpdir(self), "dotfiles.git")
        sh("git", "init", "-q", "--bare", "-b", "main", dot)
        sh("git", "init", "-q", "-b", "main", home)
        for k, v in (("user.name", "alice"), ("user.email", "alice@example.com"), ("commit.gpgsign", "false")):
            sh("git", "-C", home, "config", k, v)
        with open(os.path.join(home, ".bashrc"), "w", encoding="utf-8") as f:
            f.write("export PS1='$ '\n")
        sh("git", "-C", home, "add", ".bashrc")
        sh("git", "-C", home, "commit", "-q", "-m", "dotfiles")
        sh("git", "-C", home, "remote", "add", "origin", dot)
        sh("git", "-C", home, "push", "-q", "origin", "main")
        self.started()                                       # no --store, no brain.intake: ~/.brain/intake
        rc, out = tc.brain(self.repo, "handoff", "--no-label")
        self.assertIn("is not a git clone of brain-intake", out)
        self.assertEqual(sh("git", "-C", home, "log", "--format=%s").stdout.split(), ["dotfiles"])
        files = sh("git", "-C", dot, "ls-tree", "-r", "--name-only", "main").stdout.split()
        self.assertEqual(files, [".bashrc"])
        self.assertEqual(sh("git", "-C", home, "diff", "--cached", "--name-only").stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
