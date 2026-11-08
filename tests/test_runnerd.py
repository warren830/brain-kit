import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import sign  # noqa: E402
from brainkit.bridge import runnerd  # noqa: E402
from brainkit.common import later, load_json, write_json  # noqa: E402
from tests.helpers import commit_file, git_repo, keypair, sh, tmpdir  # noqa: E402

BRANCH = "ai/alice/PAY-1-refund"


@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class Runnerd(unittest.TestCase):
    def setUp(self):
        d = tmpdir(self)
        work = git_repo(os.path.join(d, "work"))
        commit_file(work, "src/a.py", "a\n")
        sh("git", "-C", work, "switch", "-q", "-c", BRANCH)
        self.sha = commit_file(work, "src/a.py", "a1\n")
        self.origin = os.path.join(d, "origin.git")
        sh("git", "clone", "-q", "--bare", work, self.origin)
        self.key, allowed = keypair(d)
        self.cfg = {"spool_dir": os.path.join(d, "spool"), "cache_dir": os.path.join(d, "cache"),
                    "runs_root": os.path.join(d, "runs"), "allowed_signers": allowed, "clone_url": self.origin}
        os.makedirs(self.cfg["runs_root"])

    def request(self, run_id="RUN-PAY-1-1", **over):
        v = {"kind": "handoff", "run_id": run_id, "project": "grp/repo", "branch": BRANCH, "owner": "alice",
             "key": "PAY-1", "sha": self.sha, "expires_at": later(1)}
        v.update(over)
        write_json(os.path.join(self.cfg["spool_dir"], "requests", run_id + ".json"),
                   {"voucher": v, "sig": sign.sign(v, self.key)})

    def test_prepares_worktree_at_contract_sha(self):
        self.request()
        self.assertEqual(runnerd.once(self.cfg, out=lambda *_: None), 1)
        ready = load_json(os.path.join(self.cfg["spool_dir"], "ready", "RUN-PAY-1-1.json"))
        self.assertEqual(ready["head"], self.sha)
        self.assertEqual(sh("git", "-C", ready["path"], "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), BRANCH)
        up = sh("git", "-C", ready["path"], "rev-parse", "--abbrev-ref", "@{u}").stdout.strip()
        self.assertEqual(up, "origin/" + BRANCH)
        self.assertEqual(runnerd.once(self.cfg, out=lambda *_: None), 0)  # handled once

    def fails(self, why):
        self.assertEqual(runnerd.once(self.cfg, out=lambda *_: None), 1)
        name = os.listdir(os.path.join(self.cfg["spool_dir"], "failed"))[0]
        err = load_json(os.path.join(self.cfg["spool_dir"], "failed", name))["error"]
        self.assertIn(why, err)

    def test_tampered_request_fails(self):
        self.request()
        p = os.path.join(self.cfg["spool_dir"], "requests", "RUN-PAY-1-1.json")
        req = load_json(p)
        req["voucher"]["branch"] = "ai/mallory/PAY-1-refund"
        write_json(p, req)
        self.fails("signature")

    def test_expired_fails(self):
        self.request(expires_at="2020-01-01T00:00:00Z")
        self.fails("expired")

    def test_sha_not_on_branch_fails(self):
        self.request(sha="c" * 40)
        self.fails("not found")

    def test_owner_mismatch_fails(self):
        self.request(owner="bob")
        self.fails("do not agree")


if __name__ == "__main__":
    unittest.main()
