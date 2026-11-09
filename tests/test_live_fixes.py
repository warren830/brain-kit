"""Regression tests for what the first live run on a real GitLab found (V1, 2026-11-08)."""
import io
import os
import socket
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import cli, rest  # noqa: E402
from brainkit.bridge import handoff  # noqa: E402
from brainkit.common import BrainError  # noqa: E402
from tests import test_bridge as tb  # noqa: E402
from tests.helpers import commit_file, git_repo, sh, tmpdir  # noqa: E402


class EmptyIdentityIsAPerson(unittest.TestCase):
    """Without Jira a person's brain-authz entry is {}: that is a known person, not a missing one."""

    def test_tracker_none_with_empty_identity_is_accepted(self):
        t = tb.BridgeTest("test_kit_config_comes_from_main")
        t.setUp()
        try:
            import json
            with open(t.cfg["identities"], "w") as f:
                json.dump({"users": {"alice": {}}, "roles": {}}, f)
            t.bridge = t.new_bridge()
            t.use_tracker_none()
            t.push_claim("alice")
            self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "accepted", t.notes())
        finally:
            t.doCleanups()


class NetworkErrorsAreBrainErrors(unittest.TestCase):
    """A read timeout in the middle of a response used to escape as socket.timeout and kill the bridge."""

    def test_timeout_while_reading(self):
        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                raise socket.timeout("The read operation timed out")

        with mock.patch("urllib.request.urlopen", return_value=Resp()):
            with self.assertRaises(BrainError):
                rest.urllib_transport("GET", "https://gitlab.example/api/v4/x", {}, None)


class LoopSurvivesABadRound(unittest.TestCase):
    def test_loop_logs_and_goes_on(self):
        calls = []

        class FakeBridge:
            cfg = {"interval": 0}
            kc = object()

            def poll(self):
                calls.append("poll")
                raise BrainError("GitLab timed out")

            def __getattr__(self, name):
                return lambda: calls.append(name)

        class Stop(Exception):
            pass

        err = io.StringIO()
        with mock.patch.object(handoff, "build", return_value=FakeBridge()), \
                mock.patch.object(handoff.time, "sleep", side_effect=Stop), redirect_stderr(err):
            with self.assertRaises(Stop):
                handoff.main(["--config", "unused.json", "loop"])
        self.assertEqual(calls, ["poll", "dispatch", "watch", "collect", "reconcile"])
        self.assertIn("poll failed this round", err.getvalue())
        # a single step still fails loudly
        with mock.patch.object(handoff, "build", return_value=FakeBridge()), redirect_stderr(io.StringIO()):
            self.assertEqual(handoff.main(["--config", "unused.json", "poll"]), 2)


class RunnerNeedsTheHookAdapterInGit(unittest.TestCase):
    """The adapter existed on disk but was never committed: the runner's session would block every tool call."""

    def test_handoff_check(self):
        repo = git_repo(os.path.join(tmpdir(self), "r"))
        commit_file(repo, ".kiro/aidlc-lean/aidlc_lean.py", "# lean\n")
        probs = cli.runner_kit_problems(repo)
        self.assertEqual(len(probs), 1)
        self.assertIn("lean-hook.py", probs[0])
        commit_file(repo, cli.CREW_HOOK, "# adapter\n")
        self.assertEqual(cli.runner_kit_problems(repo), [])
        sh("git", "-C", repo, "rm", "-q", "--cached", cli.CREW_HOOK)       # on disk only again
        self.assertTrue(cli.runner_kit_problems(repo))


class KiroUpgradedAgentSpec(unittest.TestCase):
    """Kiro rewrote ~/.kiro/agents/*.json into a list format on upgrade (2026-11-08), next to other tools' hooks."""

    def test_both_formats(self):
        cmd = lambda ev: "python3 %s %s" % (cli.CREW_HOOK, ev)
        old = {"hooks": {"agentSpawn": [{"command": cmd("SessionStart")}],
                         "userPromptSubmit": [{"command": cmd("UserPromptSubmit")}],
                         "preToolUse": [{"matcher": "*", "command": cmd("PreToolUse")}]}}
        new = {"kiroUpgrade": {"revision": 2}, "hooks": [
            {"name": "agentSpawn-0", "trigger": "agentSpawn", "action": {"type": "command", "command": cmd("SessionStart")}},
            {"name": "agentSpawn-1", "trigger": "agentSpawn", "action": {"type": "command", "command": "other.sh"}},
            {"name": "preToolUse-0", "trigger": "preToolUse", "matcher": "*",
             "action": {"type": "command", "command": cmd("PreToolUse")}},
            {"name": "userPromptSubmit-0", "trigger": "userPromptSubmit",
             "action": {"type": "command", "command": cmd("UserPromptSubmit")}}]}
        self.assertEqual(cli.missing_lean_hooks(old), [])
        self.assertEqual(cli.missing_lean_hooks(new), [])
        # a Lean hook under the wrong trigger does not count
        wrong = {"hooks": [{"trigger": "stop", "action": {"command": cmd("PreToolUse")}}]}
        self.assertEqual(len(cli.missing_lean_hooks(wrong)), 3)
        fixed = cli.add_lean_hooks(wrong, cli.missing_lean_hooks(wrong))
        self.assertEqual(cli.missing_lean_hooks(fixed), [])
        self.assertIn(wrong["hooks"][0], fixed["hooks"])


class ReflectWorktreeNamedAfterTheRepo(unittest.TestCase):
    """Live regression 2026-11-09: reflect run inside a requirement's worktree named its worktree folder after the
    requirement (~/.brain/worktrees/subtract) instead of the repository."""

    def test_worktree_root_uses_the_main_repo(self):
        from brainkit import reflect
        d = tmpdir(self)
        repo = git_repo(os.path.join(d, "demo"))
        commit_file(repo, "a.txt", "a\n")
        wt = os.path.join(d, "subtract")
        sh("git", "-C", repo, "worktree", "add", "-q", "--detach", wt)
        self.assertEqual(os.path.basename(reflect.worktree_root(wt)), "demo")
        self.assertEqual(os.path.basename(reflect.worktree_root(repo)), "demo")


class GitLabEditSignalAsMeasured(unittest.TestCase):
    """Measured on gitlab.aws.dev 2026-11-09 (MR !2 of the trial): an award emoji moves REST updated_at and GraphQL
    lastEditedAt but leaves lastEditedBy empty; a text edit sets lastEditedBy; an untouched note has lastEditedAt equal
    to its creation time. A reply someone only reacted to must still count."""

    def note(self, updated):
        return {"id": 7, "author": {"username": "dave"}, "created_at": "2026-11-08T14:51:13.961Z", "updated_at": updated,
                "body": "answer D-1 A"}

    def test_edit_state(self):
        from brainkit.bridge import decisions as d
        created = "2026-11-08T14:51:13.961Z"
        self.assertEqual(d.edit_state(self.note(created), {"last_edited_at": created, "last_edited_by": None}), "original")
        emoji = "2026-11-09T02:55:02.082Z"
        self.assertEqual(d.edit_state(self.note(emoji), {"last_edited_at": emoji, "last_edited_by": None}), "original")
        self.assertEqual(d.edit_state(self.note(emoji), {"last_edited_at": emoji, "last_edited_by": "mallory"}), "edited")
        self.assertEqual(d.edit_state(self.note(emoji), {"last_edited_at": emoji, "last_edited_by": "dave"}), "original")
        self.assertEqual(d.edit_state(self.note(emoji), None), "unknown")


if __name__ == "__main__":
    unittest.main()
