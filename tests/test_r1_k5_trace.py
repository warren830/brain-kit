"""Round-1 review fixes in `brain trace` and `brain eval` (brainkit/trace.py, evaluate.py, commands/trace_cmd.py,
eval_cmd.py). Each class names the review entry (delivery-loop/round-1-bugs.md) it guards."""
import hashlib
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import claims, cli, config, intents, split, trace  # noqa: E402
from brainkit import evaluate as EV  # noqa: E402
from brainkit.common import BrainError  # noqa: E402
from tests.helpers import tmpdir  # noqa: E402
from tests import test_qa_trace_eval as Q  # noqa: E402
from tests import test_split_trace as SP  # noqa: E402
from tests.test_trace import AS_OF, commit, git_repo, world  # noqa: E402

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


class R1_94_SinceStaleCitations(unittest.TestCase):
    """brain eval --since (只看这一期) still counted the stale citations of runs outside the period (C-06: tests/
    test_trace.py's world, PAY-2 and PAY-4 both merged early November, PAY-4 cites the archived T-PAY-9-1)."""

    @classmethod
    def setUpClass(cls):
        import shutil
        import tempfile
        d = os.path.realpath(tempfile.mkdtemp(prefix="brainkit-"))
        cls.addClassCleanup(shutil.rmtree, d, True)
        cls.w = world(cls, runs=("bad", "good"), d=d)

    def collect(self, **kw):
        w = self.w
        return EV.collect(w.repo, w.cfg, ledger_path=w.ledger_path, vouchers_dir=w.vouchers, as_of=AS_OF, **kw)

    def test_only_the_runs_of_the_period(self):
        every = self.collect()
        self.assertEqual([(x["key"], x["ids"]) for x in every["staleness"]["stale"]], [("PAY-4", ["T-PAY-9-1"])])
        inside = self.collect(since="2026-11-03")             # PAY-4 merged in the period: still counted
        self.assertEqual([x["key"] for x in inside["staleness"]["stale"]], ["PAY-4"])
        later = self.collect(since="2026-11-20")              # both runs are before it
        self.assertEqual(later["completed"], [])
        self.assertEqual((later["staleness"]["stale"], later["staleness"]["unknown"]), ([], []))
        self.assertEqual(later["staleness"]["total"], len(later["staleness"]["same_term"]) +
                         len(later["staleness"]["archived"]))
        text = EV.report(later)
        self.assertIn("过时与冲突：%d（过时引用 0，" % later["staleness"]["total"], text)
        self.assertNotIn("引用了已归档", text)
        self.assertNotIn("stale|", "".join(g["subject"] for g in EV.gaps(later)))


class R1_89_FamilyLikeIntentList(unittest.TestCase):
    """brain eval's 大需求 row counted only the children with a run.json: with one of three units merged and two not
    started it said 小需求都合进了，等发布; a closed child (brain intent close) stayed an open, stuck run. Now it says
    已合并 n/m as `brain intent list` does (m: the children that are not closed, started or not)."""

    def setUp(self):
        self.w = SP.family_world(self)
        repo = self.w.repo
        split = {"intent": SP.INTENT, "branch": SP.PB}
        claims.claim(repo, "PAY", "alice", "big", key="PAY-1")
        for key, owner, unit in (("PAY-2", "alice", "api"), ("PAY-3", "bob", "web"), ("PAY-4", None, "jobs")):
            claims.claim(repo, "PAY", owner, unit, key=key, extra={"source": "split", "parent": "PAY-1", "unit": unit,
                                                                   "split": split, "filed_by": "alice"})

    def intent_list(self):
        docs, _ = intents.listing(self.w.repo, self.w.cfg)
        f = split.families(self.w.repo, self.w.cfg, docs)["PAY-1"]
        return f["progress"]["merged"], f["progress"]["total"], f["state"]

    def collect(self):
        return EV.collect(self.w.repo, self.w.cfg, as_of=SP.AS_OF, stuck_hours=40)

    def test_a_child_not_started_counts(self):
        d = self.collect()
        f = d["families"][0]
        self.assertEqual((f["merged"], f["total"], f["state"]), self.intent_list()[:2] + ("in_progress",))
        self.assertEqual((f["merged"], f["total"]), (1, 3))
        self.assertEqual((f["children"], f["not_started"], f["closed"]), (["PAY-2", "PAY-3"], ["PAY-4"], []))
        self.assertIn("  PAY-1：小需求在做；已合并 1/3（有 run 的 PAY-2、PAY-3；没开工的 PAY-4）；判断点内 / 外 2 / 1；",
                      EV.report(d))

    def test_closed_children_are_not_counted_and_not_stuck(self):
        intents.close(self.w.repo, "PAY-3", "不做网页了", by="bob")
        intents.close(self.w.repo, "PAY-4", "不做对账了", by="alice")
        self.assertEqual(self.intent_list(), (1, 1, "ready_to_release"))
        d = EV.collect(self.w.repo, self.w.cfg, as_of="2026-11-20T00:00:00Z")      # days after PAY-3's last commit
        f = d["families"][0]
        self.assertEqual((f["merged"], f["total"], f["state"], f["not_started"], f["closed"]),
                         (1, 1, "ready_to_release", [], ["PAY-3", "PAY-4"]))
        st = d["states"]["PAY-3"]
        self.assertEqual((st["value"], st["closed_by"], st["stuck"]), ("closed", "claim", False))
        row = next(r for r in d["batch"] if r["key"] == "PAY-3")
        self.assertEqual((row["state"], row["closed_by"], row["delivery"]), ("closed", "claim", "closed"))
        reasons = {r["rule"]: r for r in next(q["reasons"] for q in d["review_queue"] if q["key"] == "PAY-3")}
        self.assertNotIn("stuck", reasons)
        self.assertEqual((reasons["closed_unmerged"]["text"], reasons["closed_unmerged"]["evidence"]),
                         ("需求关了（brain intent close），没有合并", "claim/PAY-3"))
        text = EV.report(d)
        self.assertIn("| PAY-3 | 需求关了，没合并（PAY-1 的小需求） |", text)
        self.assertIn("  PAY-1：小需求都合并了，等大需求评审和发布；已合并 1/1（有 run 的 PAY-2；已关闭的 PAY-3、PAY-4）；",
                      text)
        self.assertNotIn("PAY-3：", "\n".join(x for x in text.splitlines() if "没有进展" in x))

    def test_without_claims_as_before(self):
        # the synthetic family of tests/test_split_trace.py has no claims: the runs are the children
        w = SP.family_world(self)
        f = EV.collect(w.repo, w.cfg, as_of=SP.AS_OF, stuck_hours=40)["families"][0]
        self.assertEqual((f["children"], f["merged"], f["total"], f["not_started"], f["closed"]),
                         (["PAY-2", "PAY-3"], 1, 2, [], []))


class R1_92_RevertedWithItsBranchGone(unittest.TestCase):
    """GitLab's Revert of a merged MR (the revert of its merge commit) takes runs/<KEY>/run.json off main; with the
    source branch deleted on merge (GitLab's default) brain eval found the run nowhere: it left the 交付正确率
    denominator (1/1 instead of 1/2), and `brain trace KEY --ref main` said not found (the review's sM.py)."""

    @classmethod
    def setUpClass(cls):
        w = cls.w = Q.World(types.SimpleNamespace(addCleanup=cls.addClassCleanup))
        w.merge(w.run("PAY-4", "export", 4, runner_events=[Q.release()]), "merge", date="2026-11-05T00:00:00Z",
                delete_branch=True)
        cls.merge = w.merge(w.run("PAY-1", "refund", 6, runner_events=[Q.release()]), "merge",
                            date="2026-11-07T00:00:00Z", delete_branch=True)
        env = {"GIT_COMMITTER_DATE": "2026-11-09T00:00:00Z", "GIT_AUTHOR_DATE": "2026-11-09T00:00:00Z",
               "GIT_COMMITTER_EMAIL": "lead@example.com", "GIT_AUTHOR_EMAIL": "lead@example.com"}
        w.git("revert", "--no-commit", "-m", "1", cls.merge, env=env)
        w.git("commit", "-q", "-m", 'Revert "%s"\n\nThis reverts merge request !12' % w.git(
            "log", "-1", "--format=%s", cls.merge).stdout.strip(), env=env)
        cls.revert = w.rev("main")

    def test_eval_reads_it_where_it_was_last_there(self):
        for ref in ("main", None):               # --ref main, and the working tree on main
            with self.subTest(ref=ref):
                d = EV.collect(self.w.repo, self.w.cfg(), ref=ref, as_of=Q.AS_OF)
                self.assertEqual(d["removed"], {"PAY-1": {"read_at": self.merge, "removed_in": self.revert,
                                                          "revert": True}})
                self.assertEqual(d["delivery"]["by_run"], {"PAY-1": "wrong", "PAY-4": "correct"})
                self.assertEqual(d["delivery"]["value"], [1, 2])
                t = next(t for t in d["traces"] if t["key"] == "PAY-1")
                self.assertEqual(t["ref"], self.merge)
                self.assertTrue(t["outcome"]["reverted_or_hotfixed_within_14d"]["value"])
                self.assertIn("从删掉它之前的提交读：PAY-1（%s，MR 被 revert）" % self.merge[:12], EV.report(d))

    def test_trace_finds_it_too(self):
        for args in (["--ref", "main"], []):
            with self.subTest(args=args):
                rc, out, err = Q.run_cli(self.w.repo, ["trace", "PAY-1", "--json", "--as-of", Q.AS_OF] + args)
                self.assertEqual(rc, 0, out + err)
                t = json.loads(out)
                self.assertEqual((t["ref"], t["outcome"]["merged_at"]["commit"]), (self.merge, self.merge))

    def test_a_run_never_on_main_is_not_brought_back(self):
        self.assertEqual(EV.removed_runs(self.w.repo, self.w.cfg(), "main", have={"PAY-1"}), {})
        self.assertEqual(EV.removed_runs(self.w.repo, self.w.cfg(), self.merge), {})     # before the revert


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
