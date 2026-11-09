"""Round 1 review (k2): start / handoff / install / doctor / claim fixes in the local CLI. One test per finding,
named after its id (R1-nn); each failed before its fix."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import claims, cli, lean  # noqa: E402
from brainkit.common import load_json, write_json  # noqa: E402
from tests.helpers import LEAN_SRC, commit_file, git_repo, sh, tmpdir  # noqa: E402
from tests.test_cli import brain, fake_tools  # noqa: E402

HAVE_LEAN = os.path.exists(os.path.join(LEAN_SRC, "install.sh"))


def env(test, **kv):
    """Set (None: unset) environment variables for one test."""
    for k, v in kv.items():
        old = os.environ.get(k)
        test.addCleanup(lambda k=k, old=old: os.environ.pop(k, None) if old is None else os.environ.__setitem__(k, old))
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def branch(repo):
    return sh("git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def edit_status(repo, intent, status="Status: Construction app: step 1 of 3 done"):
    st = lean.state_path(repo, intent)
    with open(st, encoding="utf-8") as f:
        text = f.read()
    with open(st, "w", encoding="utf-8") as f:
        f.write(text.replace(lean.TEMPLATE_STATUS, status))
    sh("git", "-C", repo, "commit", "-q", "-am", "work")


@unittest.skipUnless(HAVE_LEAN, "AI-DLC Lean checkout not found")
class Project(unittest.TestCase):
    """A project with brain-kit and Lean installed and committed (as tests/test_cli.py LocalFlow, without its tests)."""

    def setUp(self):
        self.d = tmpdir(self)
        env(self, HOME=self.d, GITLAB_TOKEN=None, BRAIN_GITLAB_TOKEN_FILE=os.path.join(self.d, "no-token"),
            BRAIN_USER="alice", BRAIN_INTAKE_DIR=None, AIDLC_LEAN_TEAM=None, BRAIN_MR_VERSION_WAIT="0")
        fake_tools(self)
        self.repo = git_repo(os.path.join(self.d, "proj"))
        commit_file(self.repo, "src/app.py", "print('hi')\n", "init")
        rc, out = brain(self.repo, "install", self.repo, "--lean", LEAN_SRC, "--prefix", "PAY")
        self.assertEqual(rc, 0, out)
        sh("git", "-C", self.repo, "add", "-A")
        sh("git", "-C", self.repo, "commit", "-q", "-m", "install kit")

    def add_origin(self):
        self.origin = os.path.join(self.d, "origin.git")
        sh("git", "init", "-q", "--bare", "-b", "main", self.origin)
        sh("git", "-C", self.repo, "remote", "add", "origin", self.origin)
        sh("git", "-C", self.repo, "push", "-q", "origin", "main")
        return self.origin

    def docs(self, text="# 退款\n\n买家在 14 天内可以退款。\n"):
        d = os.path.join(self.d, "docs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "prd.md"), "w", encoding="utf-8") as f:
            f.write(text)
        return d

    def intake(self, key="PAY-1"):
        store = os.path.join(self.d, "store")
        rc, out = brain(self.repo, "intake", self.docs(), "--key", key, "--classification", "internal", "--consent",
                        "--store", store)
        self.assertEqual(rc, 0, out)
        return store

    def claim_refs(self, repo=None):
        return sh("git", "-C", repo or self.repo, "for-each-ref", "--format=%(refname)", "refs/heads/claim/"
                  ).stdout.split()


class R1_22_PackagePath(Project):
    def test_r1_22_package_given_as_its_path_is_refused_and_the_store_kept(self):
        store = self.intake()
        pkg = os.path.join(store, "packages", "PKG-PAY-1-1")
        files = sorted(os.listdir(pkg))
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1", "--package", pkg, "--store", store)
        self.assertEqual(rc, 2, out)
        self.assertIn("--package takes the package id, not its path: --package PKG-PAY-1-1", out)
        self.assertEqual(sorted(os.listdir(pkg)), files)                   # the store's package is all there
        self.assertEqual(branch(self.repo), "main")
        self.assertEqual(self.claim_refs(), [])                            # refused before claiming
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1", "--package", "PKG-PAY-1-1", "--store", store)
        self.assertEqual(rc, 0, out)

    def test_r1_22_attach_never_copies_a_package_onto_itself(self):
        store = self.intake()
        pkg = os.path.join(store, "packages", "PKG-PAY-1-1")
        with self.assertRaises(cli.BrainError):
            cli.attach_package(self.repo, store, pkg)
        self.assertTrue(os.path.isfile(os.path.join(pkg, "manifest.json")))
        with self.assertRaises(cli.BrainError):
            cli._readonly_copy(pkg, pkg)
        self.assertTrue(os.path.isfile(os.path.join(pkg, "manifest.json")))


if __name__ == "__main__":
    unittest.main()
