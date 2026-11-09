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


class UnitsTable(unittest.TestCase):
    """unit-of-work.md's Stories / ACs column, read for each child's ACs."""

    INTENT = "261110-pay-1-big"

    def child_acs(self, unit):
        info = {"stories": T.STORIES, "requirements": T.REQUIREMENTS.format(intent=self.INTENT),
                "design_sha256": "b" * 64, "units_sha256": "c" * 64}
        rec = {"intent": self.INTENT, "branch": "ai/alice/PAY-1-big"}
        fields, _ = split.child_fields("PAY-1", "退款大需求", info, unit, {}, rec, "a" * 40)
        return [a.split(":")[0] for a in fields["acs"]]

    def test_r1_38_an_ac_range_is_expanded(self):
        text = T.UNITS.format(rows="\n".join([
            "| api | api | services/api/** | - | - | AC-1..AC-3 | not started |",
            "| web | web | web/** | api | - | US-2, AC-4..5 | not started |",
            "| jobs | jobs | jobs/** | - | - | AC-3..AC-1 | not started |"]))
        units, probs = split.parse_units(text)
        self.assertEqual(probs, [])
        self.assertEqual([u["stories"] for u in units],
                         [["AC-1", "AC-2", "AC-3"], ["US-2", "AC-4", "AC-5"], ["AC-3", "AC-1"]])
        self.assertEqual(self.child_acs(units[0]), ["AC-1", "AC-2", "AC-3"])     # AC-2 is not dropped
        # a range it cannot expand keeps its ends and says so
        warnings = []
        split.parse_units(text, warnings)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("unit jobs: 'AC-3..AC-1' in Stories / ACs is not expanded (it runs backwards)", warnings[0])

    def test_r1_39_a_column_named_like_store_is_not_the_stories_column(self):
        text = """# Units of Work: x

| Unit | Owner team | Paths | Data store | Depends on | Contracts | Stories / ACs | Status |
|---|---|---|---|---|---|---|---|
| api | api | services/api/** | PostgreSQL refunds | - | - | US-1, AC-1, AC-2 | not started |
| jobs | jobs | jobs/** | - | - | - | US-3, AC-4 | not started |
"""
        units, probs = split.parse_units(text)
        self.assertEqual(probs, [])
        self.assertEqual([u["stories"] for u in units], [["US-1", "AC-1", "AC-2"], ["US-3", "AC-4"]])
        self.assertEqual(self.child_acs(units[0]), ["AC-1", "AC-2"])
        # no column it knows by name: every child would get no ACs, so it says so
        warnings = []
        units, probs = split.parse_units(text.replace("Stories / ACs", "Delivers"), warnings)
        self.assertEqual(([u["stories"] for u in units], probs), ([[], []], []))
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("unit-of-work.md has no Stories / ACs column (its columns: Unit, Owner team, Paths, Data store",
                      warnings[0])


class ClaimRace(unittest.TestCase):
    """R1-40: two people claim the next free key at once. The server's hook lets bob's claim of PAY-1 in while
    alice's push of PAY-1 is between git's ref check and its lock, which is how the race reaches the loser."""

    def test_r1_40_the_later_claim_takes_the_next_key(self):
        d = tmpdir(self)
        seed = git_repo(os.path.join(d, "seed"))
        T.commit_file(seed, "README", "x\n")
        origin = os.path.join(d, "origin.git")
        sh("git", "clone", "-q", "--bare", seed, origin)
        people = {}
        for who in ("alice", "bob"):
            c = people[who] = os.path.join(d, who)
            sh("git", "clone", "-q", origin, c)
            with open(os.path.join(c, ".git", "config"), "a", encoding="utf-8") as f:
                f.write("[user]\n\tname = %s\n\temail = %s@example.com\n" % (who, who) + T.no_hooks_config(c))
        bobs = split.claims._commit(people["bob"], split.claims.claim_doc("PAY-1", "bob", "对账告警"))
        sh("git", "-C", people["bob"], "push", "-q", "origin", "%s:refs/racing/PAY-1" % bobs)   # its objects only
        T.write(origin, "hooks/pre-receive", "#!/bin/sh\nif [ ! -e raced ]; then\n  : > raced\n"
                "  env -u GIT_QUARANTINE_PATH git update-ref refs/heads/claim/PAY-1 %s\nfi\nexit 0\n" % bobs)
        os.chmod(os.path.join(origin, "hooks", "pre-receive"), 0o755)
        sh("git", "-C", origin, "config", "core.hooksPath", os.path.join(origin, "hooks"))
        doc = split.claims.claim(people["alice"], "PAY", "alice", "退款加索引")
        self.assertEqual(doc["key"], "PAY-2")
        self.assertTrue(os.path.exists(os.path.join(origin, "raced")))           # the race did happen
        self.assertEqual(split.claims.read(people["alice"], "PAY-1")["owner"], "bob")
        self.assertEqual(split.claims.read(people["bob"], "PAY-2")["owner"], "alice")


if __name__ == "__main__":
    unittest.main()
