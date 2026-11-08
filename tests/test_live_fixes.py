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


if __name__ == "__main__":
    unittest.main()
