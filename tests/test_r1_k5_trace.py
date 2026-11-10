"""Round-1 review fixes in `brain trace` and `brain eval` (brainkit/trace.py, evaluate.py, commands/trace_cmd.py,
eval_cmd.py). Each class names the review entry (delivery-loop/round-1-bugs.md) it guards."""
import hashlib
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import cli, config, trace  # noqa: E402
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



class R1_99_LedgerNotSqlite(unittest.TestCase):
    """--ledger naming a directory, or a file that is not sqlite: a traceback (a missing one was a clean error)."""

    def setUp(self):
        self.repo = tiny_repo(self)
        self.folder = os.path.join(self.repo, "bridge")
        os.makedirs(self.folder)
        self.json = os.path.join(self.repo, "ledger.json")
        with open(self.json, "w", encoding="utf-8") as f:
            f.write('{"runs": []}\n')

    def test_library_raises_brain_error(self):
        for path, why in ((self.folder, "is a directory"), (self.json, "not a sqlite database")):
            with self.assertRaisesRegex(BrainError, why):
                EV.ledger_runs(path)
            with self.assertRaisesRegex(BrainError, why):
                trace.ledger_rows(path, "PAY-1")
        empty = os.path.join(self.repo, "empty.sqlite")
        open(empty, "w").close()
        self.assertEqual(EV.ledger_runs(empty), {})        # an empty file is an empty sqlite database: no runs

    def test_cli_exit_2_without_a_traceback(self):
        for args in (("eval", "--ledger", self.folder), ("eval", "--ledger", self.json),
                     ("trace", "PAY-1", "--ledger", self.folder), ("trace", "PAY-1", "--ledger", self.json)):
            rc, out = run_cli(self.repo, *args)
            self.assertEqual(rc, 2, (args, out))
            self.assertNotIn("Traceback", out)
            self.assertTrue(out.startswith("brain: bridge ledger "), (args, out))


IDEATION = ["intent-capture", "market-research", "feasibility", "scope-definition", "team-formation",
            "rough-mockups", "approval-handoff"]
OPERATIONS = ["deployment-pipeline", "environment-provisioning", "deployment-execution", "observability-setup",
              "incident-response", "performance-validation", "feedback-optimization"]
NOT_REQUESTED = "not requested (lean new without --ideation)"


def events(*recs):
    """Lean audit records as trace.lean_events gives them (oldest first), one second apart."""
    return [{"event": ev, "data": data, "intent": "261101-pay-1-x", "_at": "2026-11-01T00:00:%02dZ" % n,
             "_src": "aidlc-docs/audit/L-AAAAAA.jsonl#%d" % (n + 1), "_file": "aidlc-docs/audit/h-aaaaaa.jsonl"}
            for n, (ev, data) in enumerate(recs)]


def phase_skip(phase, stages, reason):
    return ("PHASE_SKIPPED", {"phase": phase, "stages": list(stages), "reason": reason})


def stage(ev, sid, **more):
    return (ev, dict({"stage": sid, "unit": None}, **more))


class R1_104_PhaseSkipped(unittest.TestCase):
    """Lean 0.3 records a skipped Ideation / Operations as one PHASE_SKIPPED {phase, stages, reason} (`lean new`
    without --ideation, `lean stage skip ideation|operations`); trace read only STAGE_*: the phases did not show,
    and an Ideation stage re-opened and then skipped with its phase stayed "started"."""

    def test_reopened_then_skipped_ideation_and_skipped_operations(self):
        # sD.py of the review: brain start; lean stage start intent-capture; stage skip ideation; stage skip
        # operations; stage done requirements
        evs = events(("INTENT_CREATED", {"title": "x", "ideation": False}), phase_skip("ideation", IDEATION, NOT_REQUESTED),
                     stage("STAGE_STARTED", "requirements"), stage("STAGE_STARTED", "intent-capture"),
                     phase_skip("ideation", IDEATION, "不做构想了 alice@example.com"),
                     phase_skip("operations", OPERATIONS, "不上线"),
                     stage("STAGE_COMPLETED", "requirements", note="AC-1"))
        rows = {s["id"]: s for s in trace.stages_of(evs)}
        self.assertEqual({k: s["state"] for k, s in rows.items()},
                         dict({k: "skipped" for k in IDEATION + OPERATIONS}, requirements="done"))
        ic = rows["intent-capture"]
        self.assertEqual((ic["phase_skipped"], ic["at"], ic["src"], ic["times"]),
                         ("ideation", evs[4]["_at"], evs[4]["_src"], {"skipped": 2, "started": 1}))
        self.assertEqual(ic["note"], "不做构想了 " + trace.pseudonym("alice@example.com"))   # scrubbed like any note
        self.assertEqual(rows["deployment-pipeline"]["note"], "不上线")
        self.assertNotIn("phase_skipped", rows["requirements"])
        # Lean's order: Ideation (recorded first, its stages in Lean's order), then the rest by time
        self.assertEqual([s["id"] for s in trace.stages_of(evs)], IDEATION + ["requirements"] + OPERATIONS)

    def test_ideation_in_use_again(self):
        # re-opened and not skipped again: the earlier phase skip no longer counts (Lean's stage_event); a stage
        # Ideation needs, skipped on its own while Ideation was not in use, is required again: no record
        evs = events(phase_skip("ideation", IDEATION, NOT_REQUESTED), stage("STAGE_SKIPPED", "scope-definition",
                                                                            reason="no"),
                     stage("STAGE_SKIPPED", "market-research", reason="not needed"),
                     stage("STAGE_STARTED", "intent-capture"))
        self.assertEqual([(s["id"], s["state"]) for s in trace.stages_of(evs)],
                         [("market-research", "skipped"), ("intent-capture", "started")])

    def test_a_stage_done_after_the_phase_skip_is_done(self):
        # `lean stage skip operations` after RELEASE, then one Operations stage done: its own later record decides
        evs = events(phase_skip("operations", OPERATIONS, "later"), stage("STAGE_COMPLETED", "observability-setup",
                                                                          note="dashboards"))
        rows = {s["id"]: s for s in trace.stages_of(evs)}
        self.assertEqual((rows["observability-setup"]["state"], rows["deployment-pipeline"]["state"]),
                         ("done", "skipped"))
        self.assertNotIn("phase_skipped", rows["observability-setup"])

    def test_trace_and_its_text(self):
        recs = [("INTENT_CREATED", {"title": "x", "ideation": False}), phase_skip("ideation", IDEATION, NOT_REQUESTED),
                stage("STAGE_STARTED", "requirements", created=True), phase_skip("operations", OPERATIONS[1:], "不上线")]
        repo = tiny_repo(self, {"aidlc-docs/audit/h-aaaaaa.jsonl": audit(recs)})
        t = trace.build(repo, config.load(repo), "PAY-1", as_of="2026-11-02T00:00:00Z")
        self.assertEqual([(s["id"], s["state"]) for s in t["stages"]],
                         [(k, "skipped") for k in IDEATION] + [("requirements", "started")] +
                         [(k, "skipped") for k in OPERATIONS[1:]])
        self.assertEqual(list(t["src"]["lean_audit"].values())[0]["chain"], "ok")
        text = trace.render(t)
        # one line per phase skip, not one per stage
        self.assertIn("  [S] Ideation（整段跳过 7 个阶段：%s）· 2026-11-01T00:00:02Z" % NOT_REQUESTED, text)
        self.assertIn("  [-] requirements · 2026-11-01T00:00:03Z", text)
        self.assertIn("  [S] Operations（整段跳过 6 个阶段：不上线）· 2026-11-01T00:00:04Z", text)
        self.assertNotIn("intent-capture", text)
        self.assertEqual(trace.numbers(t)["stages_done"], 0)


def audit(recs, email="alice@example.com"):
    """An audit file as Lean writes it (aidlc_lean.py Project.append: the hash chain), one second apart."""
    out, prev = [], "GENESIS"
    for n, (event, data) in enumerate(recs, 1):
        rec = {"seq": n, "ts": "2026-11-01T00:00:%02d.000Z" % n, "event": event, "intent": "261101-pay-1-x",
               "actor": {"git_email": email, "git_name": "n", "os_user": "u", "host": "h", "team": None},
               "data": data, "prev": prev}
        rec["hash"] = hashlib.sha256(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        prev = rec["hash"]
        out.append(json.dumps(rec, ensure_ascii=False))
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    unittest.main()
