"""Review round 2, group k1: CI. Each class names its item; the tests fail on 2b8f45e.

  R2-05  a promotion MR a person opens (main -> production, develop -> main) carries the bot's commits that are
         already on the default branch, merged through their own run MRs: push-rules counted them as the bot's new
         commits on a branch that is not a run branch, and stayed red. Only the bot commits the MR brings that the
         default branch does not have yet are judged (user's decision).
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import ci  # noqa: E402
from tests.helpers import commit_file, git_repo, sh, tmpdir  # noqa: E402

BOT = "brain-bot@noreply.invalid"
BOT_ENV = {"GIT_AUTHOR_NAME": "brain-bot", "GIT_AUTHOR_EMAIL": BOT, "GIT_COMMITTER_NAME": "brain-bot",
           "GIT_COMMITTER_EMAIL": BOT}
OWNER_ENV = {"GIT_AUTHOR_NAME": "alice", "GIT_AUTHOR_EMAIL": "alice@example.com", "GIT_COMMITTER_NAME": "alice",
             "GIT_COMMITTER_EMAIL": "alice@example.com"}
REFUSED = "bot accounts push only ai/<owner>/<JIRA>-<slug> run branches"


class PromotionMergeRequests(unittest.TestCase):
    """The team merges run MRs into the default branch, then promotes it by an MR into an environment branch."""

    def project(self, default, env_branch):
        d = tmpdir(self)
        r = self.repo = git_repo(os.path.join(d, "proj"))
        kit = {"version": 1, "req_prefix": "PAY"}
        if default != "main":
            kit["default_branch"] = default
        commit_file(r, ".brain/kit.json", json.dumps(kit), "kit")
        if default != "main":
            sh("git", "-C", r, "switch", "-q", "-c", default)
        if env_branch != "main":
            sh("git", "-C", r, "branch", env_branch)                   # the environment branch, at the old default
        sh("git", "-C", r, "switch", "-q", "-c", "ai/alice/PAY-1-refund")
        commit_file(r, "runs/PAY-1/run.json", "{}\n", "PAY-1: start run", env=OWNER_ENV)
        commit_file(r, "src/refund.py", "x = 1\n", "PAY-1: step 1", env=BOT_ENV)       # the runner's work
        sh("git", "-C", r, "switch", "-q", default)
        sh("git", "-C", r, "merge", "-q", "--no-ff", "-m", "Merge branch 'ai/alice/PAY-1-refund'",
           "ai/alice/PAY-1-refund", env=dict(os.environ, **OWNER_ENV))             # the run MR, merged
        self.origin = os.path.join(d, "origin.git")
        sh("git", "clone", "-q", "--bare", r, self.origin)
        sh("git", "-C", r, "remote", "add", "origin", self.origin)
        sh("git", "-C", r, "fetch", "-q", "origin")
        self.env = {"CI_MERGE_REQUEST_TARGET_BRANCH_NAME": env_branch,
                    "CI_MERGE_REQUEST_DIFF_BASE_SHA": sh("git", "-C", r, "rev-parse", env_branch).stdout.strip()}
        return r

    def rules(self, source):
        sh("git", "-C", self.repo, "checkout", "-q", "--detach", source)    # CI checks out the MR head
        return ci.push_rules(self.repo, source, "alice", self.env)

    def test_main_into_production(self):
        self.project("main", "production")
        self.assertEqual(self.rules("main"), [])

    def test_develop_into_main(self):
        self.project("develop", "main")
        self.assertEqual(self.rules("develop"), [])

    def test_a_bot_commit_the_default_branch_does_not_have_is_still_refused(self):
        r = self.project("main", "production")
        sh("git", "-C", r, "switch", "-q", "-c", "hotfix", "main")
        commit_file(r, "src/refund.py", "x = 2\n", "hotfix", env=BOT_ENV)
        sh("git", "-C", r, "push", "-q", "origin", "hotfix")
        new, merged = (sh("git", "-C", r, "rev-parse", x).stdout.strip()[:12] for x in ("hotfix", "main^2"))
        probs = " ".join(self.rules("hotfix"))
        self.assertIn(REFUSED, probs)
        self.assertIn(new, probs)
        self.assertNotIn(merged, probs)


if __name__ == "__main__":
    unittest.main()
