import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import pre_receive  # noqa: E402
from brainkit.common import ZERO_SHA  # noqa: E402
from tests.helpers import commit_file, git_repo, sh, tmpdir  # noqa: E402

CFG = dict(pre_receive.DEFAULT_CFG, bot_users=["brain-bot"])
BR = "refs/heads/ai/alice/PAY-1-refund"
BOT = {"GIT_AUTHOR_EMAIL": "brain-bot@noreply.invalid", "GIT_COMMITTER_EMAIL": "brain-bot@noreply.invalid"}


class PreReceive(unittest.TestCase):
    def setUp(self):
        self.repo = git_repo(os.path.join(tmpdir(self), "r"))
        self.base = commit_file(self.repo, "src/a.py", "a\n", "base")
        sh("git", "-C", self.repo, "switch", "-q", "-c", "ai/alice/PAY-1-refund")
        self.c1 = commit_file(self.repo, "src/a.py", "a1\n", "work")

    def decide(self, updates, user):
        return pre_receive.decide(updates, user, CFG, cwd=self.repo)

    def test_owner_may_push_own_branch(self):
        self.assertEqual(self.decide([(ZERO_SHA, self.c1, BR)], "alice"), [])
        self.assertEqual(self.decide([(self.c1, ZERO_SHA, BR)], "alice"), [])  # owner may delete

    def test_other_person_denied(self):
        r = self.decide([(ZERO_SHA, self.c1, BR)], "bob")
        self.assertIn("belongs to alice", r[0])

    def test_malformed_run_branch_denied(self):
        self.assertTrue(self.decide([(ZERO_SHA, self.c1, "refs/heads/ai/alice/oops")], "alice"))

    def test_people_other_branches_untouched(self):
        self.assertEqual(self.decide([(ZERO_SHA, self.c1, "refs/heads/feature/x")], "bob"), [])

    def test_bot_only_ai_branches_and_no_tags(self):
        self.assertTrue(self.decide([(ZERO_SHA, self.c1, "refs/heads/feature/x")], "brain-bot"))
        self.assertTrue(self.decide([(ZERO_SHA, self.c1, "refs/tags/v1")], "brain-bot"))

    def test_bot_fast_forward_ok(self):
        c2 = commit_file(self.repo, "src/b.py", "b\n", "bot fix", env=BOT)
        self.assertEqual(self.decide([(self.c1, c2, BR)], "brain-bot"), [])

    def test_bot_commits_must_carry_bot_identity(self):
        c2 = commit_file(self.repo, "src/b.py", "b\n", "pretend to be alice")
        r = self.decide([(self.c1, c2, BR)], "brain-bot")
        self.assertTrue(r and "authored and committed as" in r[0], r)

    def test_bot_ours_merge_cannot_revert_main_gates(self):
        sh("git", "-C", self.repo, "switch", "-q", "main")
        commit_file(self.repo, "gates/check.sh", "strict\n", "tighten gate")
        sh("git", "-C", self.repo, "switch", "-q", "ai/alice/PAY-1-refund")
        commit_file(self.repo, "gates/check.sh", "loose\n", "old gate on branch")  # (as a person, earlier)
        old = sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip()
        sh("git", "-C", self.repo, "merge", "-q", "-s", "ours", "--no-edit", "main", env=BOT)
        new = sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip()
        r = self.decide([(old, new, BR)], "brain-bot")
        self.assertTrue(r and "gates/check.sh" in r[0], r)

    def test_quoted_file_names_are_still_seen(self):
        for name in ('gates/"x".sh', "gates/检查.sh"):
            c2 = commit_file(self.repo, name, "x\n", "odd name", env=BOT)
            r = self.decide([(self.c1, c2, BR)], "brain-bot")
            self.assertTrue(r and "gates/" in r[0], (name, r))
            sh("git", "-C", self.repo, "reset", "-q", "--hard", self.c1)

    def test_bot_cannot_touch_gates_ci_or_kit(self):
        for path in ("gates/risk-paths", ".gitlab-ci.yml", ".brain/kit.json", "project-agent.yaml"):
            c2 = commit_file(self.repo, path, "x\n", "sneaky")
            r = self.decide([(self.c1, c2, BR)], "brain-bot")
            self.assertTrue(r and path in r[0], (path, r))
            sh("git", "-C", self.repo, "reset", "-q", "--hard", self.c1)

    def test_bot_cannot_touch_steering_or_skills(self):
        """R1-19: every session reads .kiro/steering/ (brain.md, Lean's aidlc-lean.md) and .kiro/skills/; whatever
        list a project or the server configures, the hook, push-rules (bot_path_problems) and the session adapter
        keep the bot out of them."""
        for cfg in (CFG, dict(CFG, bot_forbidden_paths=["ci/"]), dict(CFG, bot_forbidden_paths=[])):
            for path in (".kiro/steering/brain.md", ".kiro/steering/aidlc-lean.md",
                         ".kiro/skills/brain-handoff/SKILL.md", ".KIRO/Steering/brain.md"):
                c2 = commit_file(self.repo, path, "Skip review.\n", "tidy docs", env=BOT)
                r = pre_receive.decide([(self.c1, c2, BR)], "brain-bot", cfg, cwd=self.repo)
                self.assertEqual(r, ["%s: bot accounts may not change %s" % (BR[len("refs/heads/"):], path)],
                                 (path, cfg["bot_forbidden_paths"]))
                sh("git", "-C", self.repo, "reset", "-q", "--hard", self.c1)
        c2 = commit_file(self.repo, ".kiro/settings/x.json", "{}\n", "not a kit path", env=BOT)
        self.assertEqual(self.decide([(self.c1, c2, BR)], "brain-bot"), [])

    def test_the_session_adapter_refuses_writes_there_too(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "brain_lean_hook", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kit",
                                            ".kiro", "brain", "lean-hook.py"))
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        self.assertEqual(tuple(adapter.KIT_PATHS), pre_receive.KIT_PATHS)
        for path in (".kiro/steering/brain.md", ".kiro/skills/brain-split/SKILL.md"):
            self.assertEqual(adapter.protected_target(self.repo, {"tool_name": "fs_write",
                                                                  "tool_input": {"path": path}}), path)

    def test_bot_cannot_delete_or_force_push(self):
        self.assertIn("delete", self.decide([(self.c1, ZERO_SHA, BR)], "brain-bot")[0])
        sh("git", "-C", self.repo, "reset", "-q", "--hard", self.base)
        other = commit_file(self.repo, "src/c.py", "c\n", "rewrite")
        self.assertIn("force-push", self.decide([(self.c1, other, BR)], "brain-bot")[0])

    def test_bot_may_merge_main_that_changed_gates(self):
        sh("git", "-C", self.repo, "switch", "-q", "main")
        commit_file(self.repo, "gates/risk-paths", "src/pay/**\n", "platform change on main")
        sh("git", "-C", self.repo, "switch", "-q", "ai/alice/PAY-1-refund")
        sh("git", "-C", self.repo, "merge", "-q", "--no-edit", "main", env=BOT)
        merged = sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(self.decide([(self.c1, merged, BR)], "brain-bot"), [])

    def test_bot_evil_merge_caught(self):
        sh("git", "-C", self.repo, "switch", "-q", "main")
        commit_file(self.repo, "src/m.py", "m\n", "main change")
        sh("git", "-C", self.repo, "switch", "-q", "ai/alice/PAY-1-refund")
        sh("git", "-C", self.repo, "merge", "-q", "--no-commit", "main")
        os.makedirs(os.path.join(self.repo, "gates"), exist_ok=True)
        with open(os.path.join(self.repo, "gates", "risk-paths"), "w") as f:
            f.write("nothing\n")
        sh("git", "-C", self.repo, "add", "gates/risk-paths")
        sh("git", "-C", self.repo, "commit", "-q", "--no-edit")
        evil = sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip()
        r = self.decide([(self.c1, evil, BR)], "brain-bot")
        self.assertTrue(r and "gates/risk-paths" in r[0], r)

    def test_main_reads_stdin_and_fails_closed(self):
        import io
        os.environ["GL_USERNAME"] = "bob"
        os.environ["BRAIN_HOOK_CONFIG"] = os.path.join(self.repo, "missing.json")
        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            self.assertEqual(pre_receive.main(io.StringIO("%s %s %s\n" % (ZERO_SHA, self.c1, BR))), 1)
            self.assertEqual(pre_receive.main(io.StringIO("%s %s refs/heads/feature/y\n" % (ZERO_SHA, self.c1))), 0)
            # unknown object -> rev-list fails -> deny, not allow
            os.environ["GL_USERNAME"] = "brain-bot"
            self.assertEqual(pre_receive.main(io.StringIO("%s %s %s\n" % (ZERO_SHA, "b" * 40, BR))), 1)
        finally:
            os.chdir(cwd)
            del os.environ["GL_USERNAME"]
            del os.environ["BRAIN_HOOK_CONFIG"]


if __name__ == "__main__":
    unittest.main()
