"""Round-1 review fixes in the MR description (brainkit/mrdesc.py) and a split child's `brain handoff` (cli.py).

Synthetic worlds with exact records (no Lean install: fast). Each test is the reviewer's repro of one entry of
delivery-loop/round-1-bugs.md, and fails on 0d52e97."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import cli, mrdesc  # noqa: E402
from tests import test_split_trace as ST  # noqa: E402  (a module: its TestCase classes are not collected again)
from tests.helpers import sh, tmpdir  # noqa: E402
from tests.test_trace import commit  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
