"""Review round 2, group k1-risk: gates/risk-paths (brainkit/riskpaths.py). Each class names its item; the tests fail
on 2b8f45e.

  R1-17  (the submodule case) a folder rule (vendor/pay-sdk/, vendor/*/) did not cover a submodule at that folder:
         check-run let a run branch move the submodule with no design approval. git reads a submodule as a folder.
"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import ci, riskpaths  # noqa: E402
from tests.helpers import commit_file, git_repo, sh, tmpdir  # noqa: E402
from tests.test_r1_k1_ci import BR, INTENT, OWNER_ENV, PATHS, PATTERNS, run_record  # noqa: E402


def gitlink(repo, path, sha=None):
    """Point the submodule at `path` to commit `sha` in the index; no sha: remove it."""
    if sha:
        sh("git", "-C", repo, "update-index", "--add", "--cacheinfo", "160000,%s,%s" % (sha, path))
    else:
        sh("git", "-C", repo, "update-index", "--force-remove", "--", path)


# ---------- R1-17: a submodule is the folder it sits at ----------

class SubmoduleIsAFolder(unittest.TestCase):
    def test_same_answers_as_git_check_ignore_for_folders(self):
        d = git_repo(os.path.join(tmpdir(self), "g"))
        for path in PATHS:
            os.makedirs(os.path.join(d, path), exist_ok=True)     # each path a folder, as a submodule is to git
        empty = os.path.join(d, ".git", "no-excludes")
        open(empty, "w").close()
        for pat in PATTERNS:
            with open(os.path.join(d, ".gitignore"), "w", encoding="utf-8") as f:
                f.write(pat + "\n")
            p = subprocess.run(["git", "-C", d, "-c", "core.excludesFile=" + empty, "check-ignore", "--no-index",
                                "--stdin", "-z", "-n", "-v"],
                               input="\0".join(PATHS) + "\0", capture_output=True, text=True)
            out = p.stdout.split("\0")
            git = {out[i + 3]: bool(out[i + 2]) for i in range(0, len(out) - 3, 4)}
            for path in PATHS:
                self.assertEqual(riskpaths.matches(path, pat, folder=True), git[path], "%r vs %r" % (pat, path))

    def test_check_run_wants_a_design_when_a_submodule_moves(self):
        d = tmpdir(self)
        r = git_repo(os.path.join(d, "proj"))
        commit_file(r, ".brain/kit.json", json.dumps({"version": 1, "req_prefix": "PAY"}), "kit")
        commit_file(r, "gates/risk-paths", "vendor/pay-sdk/\nvendor/old-sdk/\nvendor/notes/\n", "risk paths")
        gitlink(r, "vendor/pay-sdk", "1" * 40)
        gitlink(r, "vendor/old-sdk", "1" * 40)
        commit_file(r, "vendor/notes", "v1\n", "two SDKs, and a file named like a folder rule")
        bc = commit_file(r, "brain/TECH.md", "# TECH\n", "brain")
        origin = os.path.join(d, "origin.git")
        sh("git", "clone", "-q", "--bare", r, origin)
        sh("git", "-C", r, "remote", "add", "origin", origin)
        sh("git", "-C", r, "fetch", "-q", "origin")
        env = {"CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main",
               "CI_MERGE_REQUEST_DIFF_BASE_SHA": sh("git", "-C", r, "rev-parse", "main").stdout.strip()}
        sh("git", "-C", r, "switch", "-q", "-c", BR)
        commit_file(r, "runs/PAY-1/run.json", json.dumps(run_record(bc)), "start", env=OWNER_ENV)
        commit_file(r, "aidlc-docs/%s/aidlc-state.md" % INTENT, "# state\n", "state", env=OWNER_ENV)
        gitlink(r, "vendor/pay-sdk", "2" * 40)
        gitlink(r, "vendor/old-sdk")
        commit_file(r, "vendor/notes", "v2\n", "move one SDK, drop the other", env=OWNER_ENV)
        probs = ci.check_run(r, BR, env)
        self.assertEqual(len(probs), 1, probs)
        self.assertIn("risk paths changed (vendor/old-sdk, vendor/pay-sdk) but application design was not written",
                      probs[0])


if __name__ == "__main__":
    unittest.main()
