"""Regressions for the round-1 review of the split feature (brainkit/split.py, claims.py, commands/intent_cmd.py).
Each test is named after the entry it covers (R1-nn in the review's bug list)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import split  # noqa: E402
from tests import test_split as T  # noqa: E402  (a module, so its TestCases are not collected again here)
from tests.helpers import git_repo, sh, tmpdir  # noqa: E402

STATE = """# AI-DLC State: x

Intent: {intent}
<!-- lean:begin progress -->
region
<!-- lean:end progress -->

## Session notes
Status: Inception: starting
### Next
1. Read the code.
### Notes for the next session
-
"""


class SyncUnfinishedMerge(unittest.TestCase):
    """R1-36: `brain sync` once more while its conflicted merge is not resolved."""

    def setUp(self):
        T.isolate(self)
        self.repo = repo = git_repo(os.path.join(tmpdir(self), "repo"))
        sh("git", "-C", repo, "config", "brain.user", "bob")
        self.intent = "261110-pay-1-big"
        self.st = "aidlc-docs/%s/aidlc-state.md" % self.intent
        T.write(repo, ".brain/kit.json", json.dumps({"version": 1, "req_prefix": "PAY", "tracker": "none"}))
        base = STATE.format(intent=self.intent)
        T.write(repo, self.st, base)
        T.commit_all(repo, "base")
        sh("git", "-C", repo, "switch", "-q", "-c", "ai/alice/PAY-1-big")
        T.write(repo, self.st, base.replace("Status: Inception: starting", "Status: Construction: waiting"))
        T.commit_all(repo, "PAY-1: status")
        sh("git", "-C", repo, "switch", "-q", "-c", "ai/bob/PAY-4-jobs", "main")
        rec = {"schema": 1, "key": "PAY-4", "tracker": "none", "owner": "bob", "branch": "ai/bob/PAY-4-jobs",
               "intent": self.intent, "brain_commit": "a" * 40, "kit_version": "0.1.0", "origin": "local",
               "expected_paths": ["jobs/**"], "materials": [], "created_at": "2026-11-09T08:00:00Z",
               "claim_checked": True, "handoff": None, "parent": "PAY-1", "unit": "jobs",
               "target_branch": "ai/alice/PAY-1-big", "lean_team": "jobs"}
        T.write(repo, "runs/PAY-4/run.json", json.dumps(rec, indent=2))
        T.write(repo, self.st, base.replace("Status: Inception: starting", "Status: Construction jobs: building"))
        T.commit_all(repo, "PAY-4: status")

    def head(self):
        return sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip()

    def test_r1_36_a_second_sync_refuses_an_unfinished_merge(self):
        rc, out = T.brain(self.repo, "sync")
        self.assertEqual(rc, 1, out)                       # both sides changed the Status line: left for bob
        self.assertIn("Session notes changed on both sides", out)
        before = self.head()
        rc, out = T.brain(self.repo, "sync")
        self.assertEqual(rc, 2, out)
        self.assertIn("brain: the last merge here is not finished", out)
        self.assertIn(self.st, out)
        self.assertEqual(self.head(), before)              # nothing committed, the merge is still bob's to finish
        self.assertNotIn("<<<<<<<", sh("git", "-C", self.repo, "show", "HEAD:" + self.st).stdout)
        self.assertIn("UU " + self.st, sh("git", "-C", self.repo, "status", "--porcelain").stdout)
        # a conflict-marked aidlc-state.md is not committed for you either, merge or not
        sh("git", "-C", self.repo, "merge", "--abort")
        with open(os.path.join(self.repo, self.st), "a", encoding="utf-8") as f:
            f.write("<<<<<<< ours\n- a\n=======\n- b\n>>>>>>> theirs\n")
        rc, out = T.brain(self.repo, "sync")
        self.assertEqual(rc, 2, out)
        self.assertIn("conflict markers", out)
        self.assertEqual(self.head(), before)


class NotesMerge(unittest.TestCase):
    """R1-37: Session notes merged by ### subsection, Lean's template ending in "### Notes for the next session"."""

    BASE = STATE.format(intent="261110-pay-1-big").replace("### Notes for the next session\n-\n",
                                                           "### Notes for the next session\n- \n")
    API = "### Next (team api)\nStatus: Construction api: done\n1. review and merge\n"
    JOBS = "### Next (team jobs)\nStatus: Construction jobs: done\n1. review and merge\n"

    def test_r1_37_a_subsection_only_one_side_changed_is_not_a_conflict(self):
        base = self.BASE
        self.assertTrue(base.endswith("### Notes for the next session\n- \n"))
        edited = base.replace("### Notes for the next session\n- \n",
                              "### Notes for the next session\n- api: the refund id is RF+9\n")
        # ours edits the last subsection (the end of the file) and appends its own; theirs only appends its own
        for ours, theirs in ((edited + self.API, base + self.JOBS), (base + self.JOBS, edited + self.API)):
            text, conflicts = split.merge_state(base, ours, theirs)
            self.assertEqual(conflicts, [], text)
            self.assertNotIn("<<<<<<<", text)
            self.assertEqual(text.count("### Notes for the next session"), 1, text)
            self.assertIn("### Notes for the next session\n- api: the refund id is RF+9\n", text)
            self.assertIn(self.API, text)
            self.assertIn(self.JOBS, text)
            self.assertTrue(text.endswith("1. review and merge\n"), text[-80:])
        # one side only: its file as it is
        self.assertEqual(split.merge_state(base, edited, base), (edited, []))
        self.assertEqual(split.merge_state(base, base, edited + self.API), (edited + self.API, []))
        # both sides changed that subsection differently: still a conflict, both kept
        text, conflicts = split.merge_state(base, edited, base.replace("- \n", "- jobs: rerun at 03:00\n"))
        self.assertEqual(conflicts, ["### Notes for the next session"])
        self.assertIn("- api: the refund id is RF+9\n=======\n### Notes for the next session\n- jobs: rerun", text)


if __name__ == "__main__":
    unittest.main()
