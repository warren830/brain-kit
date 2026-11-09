"""Review round 1 (cf), group k1-ci: CI and push rules. Each class names its item; the tests fail on 0d52e97.

  R1-15  push-rules in a person's pipeline only judged commits the bot authored: a bot commit with the owner as
         author (committer: the bot) went green on the owner's Retry or next push.
  R1-16  ci/brain-kit.gitlab-ci.yml ran brain-run-check and brain-push-rules only for MRs from ai/*: the bot's MR
         from any other branch got no brain-kit check on GitLab Free.
"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import ci, sign  # noqa: E402
from brainkit.common import write_json  # noqa: E402
from tests.helpers import ROOT, commit_file, git_repo, keypair, sh, tmpdir  # noqa: E402

BOT = "brain-bot@noreply.invalid"
BOT_ENV = {"GIT_AUTHOR_NAME": "brain-bot", "GIT_AUTHOR_EMAIL": BOT, "GIT_COMMITTER_NAME": "brain-bot",
           "GIT_COMMITTER_EMAIL": BOT}
OWNER_ENV = {"GIT_AUTHOR_NAME": "alice", "GIT_AUTHOR_EMAIL": "alice@example.com", "GIT_COMMITTER_NAME": "alice",
             "GIT_COMMITTER_EMAIL": "alice@example.com"}
FORGED_ENV = dict(OWNER_ENV, GIT_COMMITTER_NAME="brain-bot", GIT_COMMITTER_EMAIL=BOT)    # the bot, author line alice
BR = "ai/alice/PAY-1-refund"
INTENT = "261108-pay-1-refund"


def run_record(brain_commit, handoff=None):
    return {"schema": 1, "key": "PAY-1", "tracker": "none", "owner": "alice", "branch": BR, "intent": INTENT,
            "brain_commit": brain_commit, "kit_version": "0.1.0", "origin": "local", "expected_paths": [],
            "materials": [], "created_at": "2026-11-08T00:00:00Z", "claim_checked": True, "handoff": handoff}


class HandedOver:
    """A project whose run branch BR was handed over (signed hand-over voucher in BRAIN_VOUCHERS_DIR), as the CI job
    sees it: target main, diff base main."""

    def __init__(self, test, kit=None, risk_paths=None):
        d = self.d = tmpdir(test)
        r = self.repo = git_repo(os.path.join(d, "proj"))
        commit_file(r, ".brain/kit.json", json.dumps(dict({"version": 1, "req_prefix": "PAY"}, **(kit or {}))), "kit")
        if risk_paths is not None:
            commit_file(r, "gates/risk-paths", risk_paths, "risk paths")
        bc = commit_file(r, "brain/TECH.md", "# TECH\n", "brain")
        commit_file(r, "AGENTS.md", "# AGENTS\nRead brain/ first.\n", "agents")
        self.origin = os.path.join(d, "origin.git")
        sh("git", "clone", "-q", "--bare", r, self.origin)
        sh("git", "-C", r, "remote", "add", "origin", self.origin)
        sh("git", "-C", r, "fetch", "-q", "origin")
        self.base = sh("git", "-C", r, "rev-parse", "main").stdout.strip()
        sh("git", "-C", r, "switch", "-q", "-c", BR)
        rec = run_record(bc)
        commit_file(r, "runs/PAY-1/run.json", json.dumps(rec), "PAY-1: start run", env=OWNER_ENV)
        commit_file(r, "aidlc-docs/%s/aidlc-state.md" % INTENT, "# state\n", "work", env=OWNER_ENV)
        rec["handoff"] = {"requested_at": "2026-11-08T01:00:00Z", "by": "alice", "local_head": "x"}
        self.handoff = commit_file(r, "runs/PAY-1/run.json", json.dumps(rec), "PAY-1: hand off", env=OWNER_ENV)
        self.env = {"CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main", "CI_MERGE_REQUEST_DIFF_BASE_SHA": self.base}
        if sign.available():
            key, allowed = keypair(d)
            vdir = os.path.join(d, "vouchers")
            v = {"schema": 1, "kind": "handoff", "run_id": "RUN-PAY-1-1", "branch": BR, "sha": self.handoff,
                 "owner": "alice", "key": "PAY-1", "intent": INTENT, "voucher_id": "V-0123456789"}
            write_json(os.path.join(vdir, "RUN-PAY-1-1", "V-0123456789.json"), {"voucher": v, "sig": sign.sign(v, key)})
            self.env.update(BRAIN_VOUCHERS_DIR=vdir, BRAIN_ALLOWED_SIGNERS=allowed)

    def rules(self, user, branch=BR):
        return ci.push_rules(self.repo, branch, user, self.env)


# ---------- R1-15: a bot commit is the bot's whoever its author line names ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class BotCommitsWithAPersonsAuthorLine(unittest.TestCase):
    def setUp(self):
        self.p = HandedOver(self)

    def test_the_owners_retry_and_next_push_stay_red(self):
        p = self.p
        commit_file(p.repo, "gates/risk-paths", "# none\n", "PAY-1: fix flaky test", env=FORGED_ENV)
        self.assertTrue(p.rules("brain-bot"))                                  # control: the bot's pipeline is red
        for when in ("Retry", "next push"):
            if when == "next push":
                commit_file(p.repo, "notes.md", "looked at the MR\n", "notes", env=OWNER_ENV)
            text = " ".join(p.rules("alice"))
            self.assertIn("may not change gates/risk-paths", text, when)
            self.assertIn("authored by alice@example.com but committed by the bot", text, when)

    def test_a_forged_author_line_on_plain_code(self):
        p = self.p
        commit_file(p.repo, "src/refund.py", "x = 1\n", "PAY-1: step 1", env=BOT_ENV)
        self.assertEqual(p.rules("alice"), [])                                 # control: the bot's own commit
        commit_file(p.repo, "src/refund.py", "x = 2\n", "PAY-1: step 2", env=FORGED_ENV)
        text = " ".join(p.rules("alice"))
        self.assertIn("authored by alice@example.com but committed by the bot", text)
        self.assertIn("authored by alice@example.com but committed by the bot", " ".join(p.rules("brain-bot")))

    def test_the_bots_pipeline_names_the_path_too(self):
        p = self.p
        commit_file(p.repo, "ci/x.yml", "job: {}\n", "PAY-1: tweak CI", env=FORGED_ENV)
        self.assertIn("may not change ci/x.yml", " ".join(p.rules("brain-bot")))


# ---------- R1-16: brain-kit's CI jobs run in every MR pipeline ----------

def template_jobs():
    """{job: its text} of ci/brain-kit.gitlab-ci.yml (top-level keys)."""
    with open(os.path.join(ROOT, "ci", "brain-kit.gitlab-ci.yml"), encoding="utf-8") as f:
        text = f.read()
    parts = re.split(r"(?m)^([A-Za-z][\w-]*):[ \t]*$", text)
    return dict(zip(parts[1::2], parts[2::2]))


class EveryMergeRequestIsChecked(unittest.TestCase):
    SRC = "fix/flaky-test"

    def test_the_template_runs_all_three_jobs_in_every_mr_pipeline(self):
        jobs = template_jobs()
        self.assertEqual(sorted(jobs), ["brain-lint", "brain-push-rules", "brain-run-check"])
        for name, body in jobs.items():
            rules = re.findall(r"(?m)^\s+- if: '(.*)'$", body)
            self.assertEqual(rules, ['$CI_PIPELINE_SOURCE == "merge_request_event"'], name)
            self.assertNotIn("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME", body, name)
            self.assertNotIn("CI_COMMIT_BRANCH", body, name)

    def setUp(self):
        d = tmpdir(self)
        r = self.repo = git_repo(os.path.join(d, "proj"))
        commit_file(r, ".brain/kit.json", json.dumps({"version": 1, "req_prefix": "PAY"}), "kit")
        commit_file(r, "gates/risk-paths", "src/pay/**\n", "risk paths")
        origin = os.path.join(d, "origin.git")
        sh("git", "clone", "-q", "--bare", r, origin)
        sh("git", "-C", r, "remote", "add", "origin", origin)
        sh("git", "-C", r, "fetch", "-q", "origin")
        self.env = {"CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main",
                    "CI_MERGE_REQUEST_DIFF_BASE_SHA": sh("git", "-C", r, "rev-parse", "main").stdout.strip()}
        sh("git", "-C", r, "switch", "-q", "-c", self.SRC)

    def rules(self, user):
        return " ".join(ci.push_rules(self.repo, self.SRC, user, self.env))

    def test_the_bots_mr_from_another_branch_is_refused_whoever_runs_the_pipeline(self):
        commit_file(self.repo, "src/app.py", "x = 1\n", "fix flaky test", env=BOT_ENV)
        self.assertIn("bot accounts may only push ai/* branches", self.rules("brain-bot"))
        for user in ("alice", "tl"):                                   # Retry, or a push on top, by a person
            self.assertIn("bot accounts push only ai/<owner>/<JIRA>-<slug> run branches", self.rules(user), user)
        commit_file(self.repo, "notes.md", "x\n", "notes", env=OWNER_ENV)
        self.assertIn("run branches", self.rules("alice"))
        self.assertEqual(ci.check_run(self.repo, self.SRC, self.env), [])     # not a run branch: nothing to check

    def test_a_persons_own_mr_from_any_branch_passes(self):
        commit_file(self.repo, "src/app.py", "x = 1\n", "fix flaky test", env=OWNER_ENV)
        self.assertEqual(self.rules("alice"), "")


if __name__ == "__main__":
    unittest.main()
