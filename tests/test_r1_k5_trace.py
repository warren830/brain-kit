"""Round-1 review fixes in `brain trace` and `brain eval` (brainkit/trace.py, evaluate.py, commands/trace_cmd.py,
eval_cmd.py). Each class names the review entry (delivery-loop/round-1-bugs.md) it guards."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import cli, trace  # noqa: E402
from brainkit import evaluate as EV  # noqa: E402
from brainkit.common import BrainError  # noqa: E402
from tests.helpers import tmpdir  # noqa: E402
from tests.test_trace import commit, git_repo  # noqa: E402

KIT = {"version": 1, "req_prefix": "PAY", "bot_users": ["brain-bot"], "bot_emails": ["brain-bot@noreply.invalid"]}


def tiny_repo(test, files=None):
    """A repo with the kit config and one run (PAY-1), committed on main."""
    repo = git_repo(os.path.join(tmpdir(test), "proj"))
    rec = {"schema": 1, "key": "PAY-1", "owner": "alice", "branch": "ai/alice/PAY-1-x", "intent": "261101-pay-1-x",
           "brain_commit": None, "created_at": "2026-11-01T00:00:00Z", "handoff": None}
    commit(repo, dict({".brain/kit.json": json.dumps(KIT), "runs/PAY-1/run.json": json.dumps(rec)}, **(files or {})),
           "PAY-1: start", "2026-11-01T00:00:00Z")
    return repo


def run_cli(repo, *args):
    out, cwd = io.StringIO(), os.getcwd()
    os.chdir(repo)
    try:
        with mock.patch.dict(os.environ, {"BRAIN_INTAKE_DIR": os.path.join(repo, "no-intake"), "BRAIN_USER": "alice"}), \
                redirect_stdout(out), redirect_stderr(out):
            rc = cli.main(list(args))
    finally:
        os.chdir(cwd)
    return rc, out.getvalue()


class R1_95_ImpossibleDates(unittest.TestCase):
    """--as-of 2026-02-30 crashed with a traceback; --since 2026-13-01 was taken as the start of the period."""

    def test_a_date_that_does_not_exist_is_not_a_date(self):
        for s in ("2026-02-30", "2026-13-01", "2026-00-10", "2025-02-29"):
            self.assertIsNone(trace._utc(s), s)
            self.assertIsNone(trace._dt(s), s)
        self.assertEqual(trace._utc("2024-02-29"), "2024-02-29")          # a leap day is a date
        self.assertEqual(trace._utc("2026-11-01"), "2026-11-01")

    def test_eval_and_trace_say_so(self):
        cfg = {"runs_dir": "runs"}
        with self.assertRaisesRegex(BrainError, "--since must be a date"):
            EV.collect("/nonexistent", cfg, since="2026-13-01")
        with self.assertRaisesRegex(BrainError, "not an ISO time"):
            EV.collect("/nonexistent", cfg, as_of="2026-13-01")
        with self.assertRaisesRegex(BrainError, "not an ISO time"):
            trace.build("/nonexistent", cfg, "PAY-1", as_of="2026-02-30")

    def test_cli_exit_2_without_a_traceback(self):
        repo = tiny_repo(self)
        for args in (("trace", "PAY-1", "--as-of", "2026-02-30"), ("eval", "--as-of", "2026-13-01"),
                     ("eval", "--since", "2026-13-01")):
            rc, out = run_cli(repo, *args)
            self.assertEqual(rc, 2, (args, out))
            self.assertNotIn("Traceback", out)
            self.assertTrue(out.startswith("brain: "), (args, out))



if __name__ == "__main__":
    unittest.main()
