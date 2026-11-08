import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import claims, pre_receive  # noqa: E402
from brainkit.common import BrainError, ZERO_SHA  # noqa: E402
from tests.helpers import commit_file, git_repo, sh, tmpdir  # noqa: E402


class LocalClaims(unittest.TestCase):
    def setUp(self):
        self.repo = git_repo(os.path.join(tmpdir(self), "r"))
        commit_file(self.repo, "README", "x\n")

    def test_next_free_key_and_explicit_key(self):
        a = claims.claim(self.repo, "OMS", "alice", "refunds")
        b = claims.claim(self.repo, "OMS", "bob", "fees")
        self.assertEqual((a["key"], b["key"]), ("OMS-1", "OMS-2"))
        self.assertEqual(claims.read(self.repo, "OMS-1")["owner"], "alice")
        self.assertEqual(claims.claim(self.repo, "OMS", "alice", "again", key="OMS-1")["owner"], "alice")  # idempotent
        with self.assertRaises(BrainError):
            claims.claim(self.repo, "OMS", "bob", "steal", key="OMS-1")
        self.assertEqual(claims.claim(self.repo, "OMS", "carol", "x", key="OMS-7")["key"], "OMS-7")
        self.assertEqual(claims.claim(self.repo, "OMS", "carol", "y")["key"], "OMS-8")
        # the work tree is untouched
        self.assertEqual(sh("git", "-C", self.repo, "status", "--porcelain").stdout, "")


class RemoteClaims(unittest.TestCase):
    def test_two_clones_never_get_the_same_key(self):
        d = tmpdir(self)
        seed = git_repo(os.path.join(d, "seed"))
        commit_file(seed, "README", "x\n")
        origin = os.path.join(d, "origin.git")
        sh("git", "clone", "-q", "--bare", seed, origin)
        a = os.path.join(d, "a")
        b = os.path.join(d, "b")
        for c in (a, b):
            sh("git", "clone", "-q", origin, c)
            sh("git", "-C", c, "config", "user.email", "t@example.com")
            sh("git", "-C", c, "config", "user.name", "t")
        # b computes "next free" from a stale view: simulate by claiming in a first
        self.assertEqual(claims.claim(a, "OMS", "alice", "one")["key"], "OMS-1")
        self.assertEqual(claims.claim(b, "OMS", "bob", "two")["key"], "OMS-2")
        # a race on an explicit key: the second push is refused by git itself
        claims.claim(a, "OMS", "alice", "x", key="OMS-5")
        with self.assertRaises(BrainError):
            claims.claim(b, "OMS", "bob", "x", key="OMS-5")
        self.assertEqual(claims.read(b, "OMS-5")["owner"], "alice")


class ClaimHook(unittest.TestCase):
    def setUp(self):
        self.repo = git_repo(os.path.join(tmpdir(self), "r"))
        commit_file(self.repo, "README", "x\n")

    def commit(self, doc):
        return claims._commit(self.repo, doc)

    def decide(self, old, new, user, key="OMS-1"):
        return pre_receive.decide([(old, new, "refs/heads/claim/" + key)], user, pre_receive.DEFAULT_CFG, cwd=self.repo)

    def test_rules(self):
        mine = self.commit(claims.claim_doc("OMS-1", "alice", "t"))
        self.assertEqual(self.decide(ZERO_SHA, mine, "alice"), [])
        self.assertIn("must name", self.decide(ZERO_SHA, mine, "bob")[0])       # claiming in someone else's name
        self.assertIn("must name", self.decide(ZERO_SHA, mine, "alice", "OMS-2")[0])  # key mismatch
        self.assertIn("bot", self.decide(ZERO_SHA, mine, "brain-bot")[0])
        self.assertIn("claim/<PREFIX>-<n>", self.decide(ZERO_SHA, mine, "alice", "oms")[0])
        theirs = self.commit(claims.claim_doc("OMS-1", "bob", "t"))
        self.assertIn("claimed by alice", self.decide(mine, theirs, "bob")[0])  # takeover by force push
        self.assertIn("claimed by alice", self.decide(mine, ZERO_SHA, "bob")[0])  # release by someone else
        self.assertEqual(self.decide(mine, ZERO_SHA, "alice"), [])                # owner releases
        bad = claims._commit(self.repo, {"not": "a claim"})
        self.assertTrue(self.decide(ZERO_SHA, bad, "alice"))


if __name__ == "__main__":
    unittest.main()
