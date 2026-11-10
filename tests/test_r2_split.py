"""R2-30: Lean 0.3.1 compares team names through its team_key (`web-ui` and `web_ui` are one team), so `brain split`
in multi-team mode must not warn that a unit named web-ui, Owner team web-ui, is sent elsewhere by Lean's Next, nor
tell the person to change its Owner team. The warnings still fire where Lean really reads two teams, and with Lean
0.3.0, which compared the Owner team cell as written."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brainkit import lean as L  # noqa: E402
from tests import test_split as T  # noqa: E402
from tests.helpers import tmpdir  # noqa: E402


def units(owner_of_web_ui):
    return ["| api | api | services/api/**, aidlc-docs/contracts/api/** | - | aidlc-docs/contracts/api/refunds.yaml "
            "| US-1 | not started |",
            "| web-ui | %s | web/** | - | aidlc-docs/contracts/api/refunds.yaml | US-2 | not started |"
            % owner_of_web_ui]


class R2_30_HyphenUnitInMultiTeam(T.MultiTeam):
    def dry_run(self):
        return self.ok("alice", self.alice, "split", "PAY-1", "--dry-run")

    def test_owner_team_written_like_the_unit_is_no_warning(self):
        self.inception(rows=units("web-ui"))
        out = self.dry_run()
        self.assertNotIn("has Owner team", out)
        self.assertNotIn("set Owner team", out)
        self.ok("alice", self.alice, "split", "PAY-1")
        self.ok("bob", self.bob, "start", "--key", "PAY-3")
        web_ui = self.wt("bob", "PAY-3")
        self.assertIn("Next: unit web-ui:", T.lean(web_ui, "status").stdout)  # Lean itself sends it to its own unit

    def test_another_owner_team_still_warns(self):
        self.inception(rows=units("payments"))
        self.assertIn("unit web-ui has Owner team payments, but its child runs Lean as team web_ui", self.dry_run())

    def test_the_parents_clone_named_like_a_unit_still_warns(self):
        self.inception(rows=units("web-ui"))
        T.lean(self.alice, "init", "--team", "web-ui")           # 0.3.1 keeps the name as written
        T.commit_all(self.alice, "ledger")
        self.assertIn("this clone's Lean team is web-ui, a unit's", self.dry_run())


for _name in [n for n in dir(T.MultiTeam) if n.startswith("test_")]:       # run only the tests above
    if _name not in R2_30_HyphenUnitInMultiTeam.__dict__:
        setattr(R2_30_HyphenUnitInMultiTeam, _name, None)


class R2_30_SameTeamFollowsTheInstalledLean(unittest.TestCase):
    def repo_with_lean(self, version):
        repo = tmpdir(self)
        os.makedirs(os.path.join(repo, os.path.dirname(L.SCRIPT)))
        with open(os.path.join(repo, L.SCRIPT), "w", encoding="utf-8") as f:
            f.write('VERSION = "%s"\n' % version)
        return repo

    def test_lean_031_reads_hyphen_and_underscore_as_one_team(self):
        repo = self.repo_with_lean("0.3.1")
        self.assertTrue(L.same_team(repo, "web-ui", "web_ui"))
        self.assertFalse(L.same_team(repo, "支付", "__"))      # Lean keeps letters of any script
        self.assertFalse(L.same_team(repo, "payments", "web_ui"))

    def test_lean_030_compared_as_written(self):
        repo = self.repo_with_lean("0.3.0")
        self.assertFalse(L.same_team(repo, "web-ui", "web_ui"))
        self.assertTrue(L.same_team(repo, "web_ui", "web_ui"))


if __name__ == "__main__":
    unittest.main()
