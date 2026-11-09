"""QA round 2 (bridge, CLI, hooks): the reviewer's findings on 9935b11/a5c1e8b, ported. Each test failed on a5c1e8b.

Numbers are the reviewer's. The reviewer's own repros are in qa_r2/ (outside this repo); where one needed a second
process or c9e59d1's code, the port writes the same ledger rows with the ledger's own calls."""
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import brainlint, claims, cli, config, intents, pre_receive, sign  # noqa: E402
from brainkit.bridge import decisions, handoff, ledger as ledger_mod  # noqa: E402
from brainkit.bridge.handoff import Bridge  # noqa: E402
from brainkit.bridge.kirocrew import KiroCrew  # noqa: E402
from brainkit.common import JIRA_RE, BrainError, load_json, write_json  # noqa: E402
from brainkit.rest import GitLab, Jira  # noqa: E402
from tests import test_bridge as tb  # noqa: E402
from tests import test_qa_security as qs  # noqa: E402
from tests.helpers import LEAN_SRC, commit_file, git_repo, sh, tmpdir  # noqa: E402

BOT = "brain-bot@noreply.invalid"
BOT_ENV = {"GIT_AUTHOR_NAME": "brain-bot", "GIT_AUTHOR_EMAIL": BOT, "GIT_COMMITTER_NAME": "brain-bot",
           "GIT_COMMITTER_EMAIL": BOT}
HAVE_LEAN = os.path.exists(os.path.join(LEAN_SRC, "install.sh"))


def fixture(test):
    qs.env(test, HOME=tmpdir(test))
    return qs.fixture(test)


def vouchers(t, kind=None):
    vdir = os.path.join(t.cfg["vouchers_dir"], "RUN-PAY-1-1")
    out = [load_json(os.path.join(vdir, f))["voucher"] for f in sorted(os.listdir(vdir))] if os.path.isdir(vdir) else []
    return [v for v in out if kind is None or v["kind"] == kind]


def state(t, did="D-1"):
    return t.bridge.ledger.db.execute("SELECT state FROM decisions WHERE id=?", (did,)).fetchone()[0]


def later(minutes):
    """The ledger's clock, `minutes` on (reservations and hand-over checks age by it)."""
    real = ledger_mod.utcnow
    return mock.patch.object(ledger_mod, "utcnow", lambda: real() + timedelta(minutes=minutes))


# ---------- 1: a bot's commit on top of the hand-over is not handed over ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class BotCommitIsNeverTheHandover(unittest.TestCase):
    def test_bot_commit_on_top_that_rewrites_run_json(self):
        t = fixture(self)
        h = t.sha
        sh("git", "-C", t.work, "switch", "-q", tb.BRANCH)
        rec = t.run_json()
        rec["handoff"] = dict(rec["handoff"], requested_at="2026-11-08T01:00:05Z")       # looks like a hand-over
        with open(os.path.join(t.work, "runs/PAY-1/run.json"), "w") as f:
            json.dump(rec, f)
        commit_file(t.work, "src/pay/limits.py", "LIMIT_CHECK = False\n", "runner: tidy", env=BOT_ENV)
        x = sh("git", "-C", t.work, "rev-parse", "HEAD").stdout.strip()
        sh("git", "-C", t.work, "push", "-q", "-f", t.origin, "%s:refs/heads/%s" % (tb.BRANCH, tb.BRANCH))
        self.assertEqual(sh("git", "-C", t.origin, "rev-list", "--parents", "-n", "1", x).stdout.split()[1:], [h])
        t.mrs[5]["sha"] = x                                   # GitLab recorded X before alice's label
        t.rejected_with("made by a bot account")
        self.assertEqual(vouchers(t), [])

    def test_gitlab_not_showing_the_commits_people_is_refused(self):
        t = fixture(self)
        t.fake.routes.insert(0, ("GET", re.compile(r"^%s/repository/commits/%s$" % (tb.GL, t.sha)),
                                 lambda m, qy, b: (200, {"id": t.sha, "parent_ids": [t.sha]})))
        t.rejected_with("who authored and committed")


class BotsDoNotWriteRunJson(unittest.TestCase):
    """pre-receive: the runner may write park.json and its work under runs/<KEY>/, never run.json."""

    def setUp(self):
        self.repo = git_repo(os.path.join(tmpdir(self), "r"))
        commit_file(self.repo, "src/a.py", "a\n", "base")
        sh("git", "-C", self.repo, "switch", "-q", "-c", "ai/alice/PAY-1-refund")
        self.c1 = commit_file(self.repo, "runs/PAY-1/run.json", "{}\n", "start")
        self.cfg = dict(pre_receive.DEFAULT_CFG)

    def push(self, rel, text):
        new = commit_file(self.repo, rel, text, "runner", env=BOT_ENV)
        return pre_receive.decide([(self.c1, new, "refs/heads/ai/alice/PAY-1-refund")], "brain-bot", self.cfg,
                                  cwd=self.repo)

    def test_park_json_is_fine(self):
        self.assertEqual(self.push("runs/PAY-1/park.json", '{"status": "waiting"}\n'), [])

    def test_run_json_is_refused(self):
        r = self.push("runs/PAY-1/run.json", '{"handoff": {}}\n')
        self.assertTrue(r and "may not change runs/PAY-1/run.json" in r[0], r)

    def test_merging_main_into_the_run_branch_is_fine(self):
        sh("git", "-C", self.repo, "switch", "-q", "main")
        commit_file(self.repo, "src/b.py", "b\n", "main moves on")
        sh("git", "-C", self.repo, "switch", "-q", "ai/alice/PAY-1-refund")
        sh("git", "-C", self.repo, "merge", "-q", "--no-edit", "main", env=BOT_ENV)
        new = sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(pre_receive.decide([(self.c1, new, "refs/heads/ai/alice/PAY-1-refund")], "brain-bot",
                                            self.cfg, cwd=self.repo), [])

    def test_runs_dir_from_the_config(self):
        self.cfg["runs_dir"] = "work/runs"
        self.assertEqual(self.push("runs/PAY-1/run.json", '{"x": 1}\n'), [])   # not a run record here
        r = self.push("work/runs/PAY-1/run.json", "{}\n")
        self.assertTrue(r and "work/runs/PAY-1/run.json" in r[0], r)


# ---------- 2, 12: what counts as an edited reply ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class EditedRepliesAndConflicts(unittest.TestCase):
    def two_builders(self):
        t = self.t = fixture(self)
        with open(t.cfg["identities"], "w") as f:
            json.dump({"users": {"alice": {"jira": "alice"}, "dave": {}, "erin": {}, "bob": {}},
                       "roles": {"builder": ["dave", "erin"]}}, f)
        t.bridge = t.new_bridge()
        t.dispatched()
        t.park(kind="design", options=[], lean_request="DESIGN 8cf2cbce", question="请批准设计")
        self.assertEqual(t.bridge.watch(), 1)
        return t

    def test_2_a_touched_disagreement_still_conflicts(self):
        """The reviewer's case: erin's reject only had its updated_at moved (GitLab shows no edit)."""
        t = self.two_builders()
        t.reply(5, "dave", "answer D-1 approve")
        t.reply(5, "erin", "answer D-1 reject 设计漏了限额检查")
        t.notes_db[5][-1]["updated_at"] = tb.tick()
        self.assertEqual((t.bridge.collect(), vouchers(t, "answer")), (0, []))
        self.assertTrue(any(n["body"].startswith("D-1 收到不同的回答") for n in t.notes_db[5]))

    def test_2_an_edited_disagreement_holds_the_round(self):
        """erin's reply was edited by someone else before the bridge read it: it does not count, and it still
        stops dave's approve from going out; a new round then starts after the bridge's note."""
        t = self.two_builders()
        t.reply(5, "dave", "answer D-1 approve")
        t.reply(5, "erin", "answer D-1 reject 设计漏了限额检查")
        n = t.notes_db[5][-1]
        n["body"], n["updated_at"], n["last_edited_by"] = "answer D-1 reject", tb.tick(), "mo"
        self.assertEqual((t.bridge.collect(), vouchers(t, "answer")), (0, []))
        said = [x["body"] for x in t.notes_db[5]]
        self.assertTrue(any(x.startswith("D-1 收到不同的回答") and "@erin" in x and "被改过" in x for x in said), said)
        self.assertTrue(any(x.startswith("D-1：") and "编辑过" in x for x in said), said)
        t.reply(5, "dave", "answer D-1 approve")                              # the new round
        self.assertEqual(t.bridge.collect(), 1)
        self.assertEqual(vouchers(t, "answer")[0]["choice"], "approve")

    def test_12_an_award_emoji_before_the_bridge_reads_the_answer(self):
        t = fixture(self)
        t.dispatched()
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A 按自然日")
        t.notes_db[5][-1]["updated_at"] = tb.tick()          # bob's :thumbsup: moves it; GraphQL shows no edit
        self.assertEqual(t.bridge.collect(), 1)
        self.assertFalse([n for n in t.notes_db[5] if n["body"].startswith("D-1：")])
        self.assertTrue(t.fake.called("POST", r"/api/graphql"))

    def test_12_edited_before_the_first_poll_by_someone_else(self):
        """dave posted something harmless; someone with edit rights made it an approval before the bridge looked."""
        t = self.two_builders()
        t.reply(5, "dave", "I will read the design tomorrow")
        n = t.notes_db[5][-1]
        n["body"], n["updated_at"] = "answer D-1 approve", tb.tick()      # the GraphQL fake: last edited by a Maintainer
        for _ in range(2):
            self.assertEqual(t.bridge.collect(), 0)
        told = [x["body"] for x in t.notes_db[5] if x["body"].startswith("D-1：")]
        self.assertEqual(len(told), 1, told)                                # told once
        self.assertIn("新的回复", told[0])
        t.reply(5, "dave", "answer D-1 approve")
        self.assertEqual(t.bridge.collect(), 1)

    def test_12_edited_by_its_author_before_the_first_poll_counts(self):
        t = self.two_builders()
        t.reply(5, "dave", "answer D-1 aprove")
        n = t.notes_db[5][-1]
        n["body"], n["updated_at"], n["last_edited_by"] = "answer D-1 approve", tb.tick(), "dave"
        self.assertEqual(t.bridge.collect(), 1)

    def test_12_graphql_unavailable_fails_closed_for_the_round(self):
        """GitLab GraphQL down at a touched reply's first sight: it does not count that round, and nothing about it
        is recorded; read again next round (QA r3: a transient failure is not a permanent dead end)."""
        t = fixture(self)
        t.dispatched()
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A")
        t.notes_db[5][-1]["updated_at"] = tb.tick()
        t.graphql_down = True
        self.assertEqual(t.bridge.collect(), 0)
        self.assertEqual(state(t), "open")
        t.graphql_down = False
        self.assertEqual(t.bridge.collect(), 1)                             # never edited: delivered
        self.assertFalse([x["body"] for x in t.notes_db[5] if x["body"].startswith("D-1：")])

    def test_12_a_reply_changed_after_the_bridge_read_it(self):
        """The text hash taken at first sight covers later edits (here: during a conflict round)."""
        t = self.two_builders()
        t.reply(5, "dave", "answer D-1 approve")
        t.reply(5, "erin", "answer D-1 reject")
        self.assertEqual(t.bridge.collect(), 0)                             # a conflict: a new round
        t.reply(5, "erin", "answer D-1 approve")
        n = t.notes_db[5][-1]
        rows = t.bridge.reply_states(dict(t.bridge.ledger.db.execute(
            "SELECT * FROM decisions WHERE id='D-1'").fetchone()), t.notes_db[5])
        self.assertEqual(rows[n["id"]], "original")
        n["body"] = "answer D-1 reject"                                     # changed after the bridge read it
        self.assertEqual(t.bridge.collect(), 0)
        self.assertTrue(any(x["body"].startswith("D-1：") and "编辑过" in x["body"] for x in t.notes_db[5]))

    def test_ticking_the_review_checklist_keeps_the_question(self):
        t = fixture(self)
        t.acceptance()
        t.bridge.watch()
        q = [n for n in t.notes_db[5] if n["body"].startswith("**D-1")][0]
        self.assertIn("- [ ] ", q["body"])
        q["body"] = q["body"].replace("- [ ] ", "- [x] ", 1)                 # carol ticks the first item
        q["updated_at"] = tb.tick()
        t.reply(5, "carol", "answer D-1 approve")
        self.assertEqual(t.bridge.collect(), 1)
        self.assertEqual(state(t), "delivered")

    def test_an_outside_signer_without_edit_knowledge_fails_closed(self):
        """check_answer without `edits` (no ledger): a touched reply does not count; with GraphQL's answer it does."""
        t = fixture(self)
        t.dispatched()
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A")
        a = t.notes_db[5][-1]
        a["updated_at"] = tb.tick()
        d = dict(t.bridge.ledger.db.execute("SELECT * FROM decisions WHERE id='D-1'").fetchone())
        raw = t.bridge.gl.file_raw(tb.P, "runs/PAY-1/park.json", d["sha"])
        notes = t.bridge.gl.mr_notes(tb.P, 5)
        args = (dict(d), raw, notes, t.bridge.kit(tb.P), t.bridge.roles, "alice", d["sha"], tb.PUSHED)
        self.assertEqual(decisions.check_answer(*args).status, "none")
        info = t.bridge.gl.note_edits(tb.P, 5)
        edits = {a["id"]: decisions.edit_state(a, info.get(a["id"]))}
        self.assertEqual(decisions.check_answer(*args, edits=edits).status, "answer")


# ---------- 13: an accepted answer the bridge could not sign ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class ReopenedAnswer(unittest.TestCase):
    def accepted_not_signed(self):
        t = self.t = fixture(self)
        t.dispatched()
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A 按自然日")
        reply = t.notes_db[5][-1]
        reply["updated_at"] = reply["created_at"]
        self.good = t.bridge.signer
        t.bridge.signer = sign.KeySigner(os.path.join(t.d, "key-not-mounted-yet"))
        with self.assertRaises(BrainError):
            t.bridge.collect()
        self.assertTrue(any("不用重新回答" in n["body"] for n in t.notes_db[5]))
        return t, reply

    def test_touched_after_it_was_accepted_is_still_delivered(self):
        t, reply = self.accepted_not_signed()
        reply["updated_at"] = tb.tick()                                     # an award emoji, a resolved thread
        t.bridge.signer = self.good
        self.assertEqual(sum(t.bridge.collect() for _ in range(3)), 1)
        self.assertEqual(state(t), "delivered")

    def test_edited_after_it_was_accepted_is_told(self):
        t, reply = self.accepted_not_signed()
        reply["body"], reply["updated_at"] = "answer D-1 B 按交易日", tb.tick()
        t.bridge.signer = self.good
        self.assertEqual(sum(t.bridge.collect() for _ in range(3)), 0)
        told = [n["body"] for n in t.notes_db[5] if n["body"].startswith("D-1：") and "编辑过" in n["body"]]
        self.assertEqual(len(told), 1)


# ---------- 14: publishing a voucher again after a failed push ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class PublishRetry(unittest.TestCase):
    def setUp(self):
        t = self.t = fixture(self)
        self.remote = os.path.join(t.d, "intake.git")
        sh("git", "init", "-q", "--bare", "-b", "main", self.remote)
        seed = git_repo(os.path.join(t.d, "seed"))
        commit_file(seed, "vouchers/README", "vouchers\n")
        sh("git", "-C", seed, "push", "-q", self.remote, "main")
        self.clone = os.path.join(t.d, "intake")
        sh("git", "clone", "-q", self.remote, self.clone)
        sh("git", "-C", self.clone, "config", "user.name", "brain-bridge")
        sh("git", "-C", self.clone, "config", "user.email", "bridge@example.com")
        t.cfg.update(vouchers_dir=os.path.join(self.clone, "vouchers"), publish_vouchers=True)
        t.bridge = t.new_bridge()

    def published(self):
        return sh("git", "-C", self.remote, "ls-tree", "-r", "--name-only", "main").stdout

    def away(self):
        os.rename(self.remote, self.remote + ".away")

    def back(self):
        os.rename(self.remote + ".away", self.remote)

    def test_handover_after_one_failed_push(self):
        t = self.t
        self.away()
        with self.assertRaises(BrainError):
            t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5])
        self.back()
        self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "accepted")
        self.assertIn("vouchers/RUN-PAY-1-1/V-", self.published())
        self.assertEqual(len(vouchers(t, "handoff")), 1)

    def test_answer_after_one_failed_push(self):
        t = self.t
        t.dispatched()
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A")
        self.away()
        with self.assertRaises(BrainError):
            t.bridge.collect()
        self.back()
        self.assertEqual(t.bridge.collect(), 1)
        self.assertEqual(state(t), "delivered")
        self.assertEqual(len([f for f in self.published().split() if f.startswith("vouchers/RUN-PAY-1-1/")]), 2)

    def test_a_push_that_hangs_ends_after_publish_timeout(self):
        t = self.t
        sh("git", "-C", self.clone, "config", "protocol.ext.allow", "always")
        sh("git", "-C", self.clone, "remote", "set-url", "origin", "ext::sh -c sleep% 30")
        t.cfg["publish_timeout"] = 0.5
        start = time.monotonic()
        with self.assertRaises(BrainError) as e:
            t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5])
        self.assertLess(time.monotonic() - start, 20)
        self.assertIn("did not finish", str(e.exception))
        self.assertIsNone(t.bridge.ledger.open_run_for("PAY-1"))           # nothing half-done


# ---------- 5, 24, 26: run reservations left by a stopped bridge ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class LeftOverReservation(unittest.TestCase):
    def stopped_while_signing(self):
        """What a bridge stopped (SIGTERM, a reboot) while signing MR !5's contract leaves: the hand-over check
        'checking' and the run id reserved (the reviewer's repro stops a real bridge process and finds these)."""
        t = self.t = fixture(self)
        led = t.bridge.ledger
        self.assertTrue(led.claim_handoff(tb.P, 5, "%s#%s" % (t.sha, t.events[5][-1]["id"])))
        self.assertEqual(led.reserve_run("PAY-1", tb.P, 5, t.sha), "RUN-PAY-1-1")
        return t

    def test_5_the_owners_next_handover_commit_an_hour_later(self):
        t = self.stopped_while_signing()
        rec = t.run_json(handoff={"requested_at": "2026-11-09T02:00:00Z", "by": "alice", "local_head": "y"})
        t.mrs[5]["sha"] = t.push("runs/PAY-1/run.json", json.dumps(rec))
        t.events[5].append(t.ev("alice", "add"))
        with later(60):
            self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "accepted", t.notes()[-1:])

    def test_5_another_mr_of_the_requirement_an_hour_later(self):
        t = self.stopped_while_signing()
        t.mrs[7] = t.mr(7, tb.BRANCH, t.sha)
        t.events[7] = [t.ev("alice", "add")]
        with later(60):
            self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[7]), "accepted", t.notes()[-1:])

    def test_5_reconcile_clears_what_is_stale_and_report_stops_counting_it(self):
        t = self.stopped_while_signing()
        t.bridge.reconcile()
        self.assertEqual(t.bridge.report()["runs"], {"signing": 1})        # fresh: the signer may still answer
        with later(60):
            t.bridge.reconcile()
        rep = t.bridge.report()
        self.assertEqual((rep["runs"], rep["handoffs"]), ({}, {}))

    def test_24_the_owner_labels_the_same_commit_again(self):
        t = self.stopped_while_signing()
        self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "seen")
        t.events[5].append(t.ev("alice", "add"))
        self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "waiting")   # not a rejection
        self.assertEqual((t.notes(), t.labels()), ([], []))
        with later(16):
            t.bridge.poll()
        self.assertEqual(t.bridge.ledger.run("RUN-PAY-1-1")["state"], "spooled")
        self.assertFalse([n for n in t.notes() if "already has an open run" in n])

    def test_24_a_check_left_checking_is_taken_over_once_stale(self):
        t = self.t = fixture(self)
        self.assertTrue(t.bridge.ledger.claim_handoff(tb.P, 5, "%s#%s" % (t.sha, t.events[5][-1]["id"])))
        self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "seen")
        with later(16):
            self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "accepted")

    def test_26_a_signer_whose_timeout_is_not_seconds(self):
        t = fixture(self)
        for odd in ((3.05, 27), timedelta(seconds=30), "30s", None):
            class ApiSigner:
                timeout = odd

                def sign(self, voucher, key=t.key):
                    return sign.KeySigner(key).sign(voucher)
            t.bridge.signer = ApiSigner()
            self.assertEqual(t.bridge.reservation_ttl, 900.0)
        t.bridge.signer.timeout = "600"                                      # a number of seconds, as a string
        self.assertEqual(t.bridge.reservation_ttl, 2400.0)
        t.bridge.signer.timeout = (3.05, 27)
        t.bridge.poll()
        self.assertEqual(t.bridge.ledger.run("RUN-PAY-1-1")["state"], "spooled")

    def test_sigterm_mid_signing_gives_everything_back(self):
        """brain-bridge turns SIGTERM into SystemExit: the reservation and the hand-over check are given back."""
        t = fixture(self)

        class Stopped:
            def sign(self, voucher):
                handoff._terminate(15, None)
        t.bridge.signer = Stopped()
        with self.assertRaises(SystemExit) as e:
            t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5])
        self.assertEqual(e.exception.code, 143)
        db = t.bridge.ledger.db
        self.assertEqual((db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                          db.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0]), (0, 0))

    def test_release_run_when_sqlite_is_busy(self):
        t = fixture(self)
        led = t.bridge.ledger
        led.sleep = lambda s: None
        self.assertEqual(led.reserve_run("PAY-1", tb.P, 5, t.sha), "RUN-PAY-1-1")
        real, fails = led.db.execute, [1]

        def busy(sql, params=()):
            if sql.startswith("DELETE FROM runs") and fails[0]:
                fails[0] -= 1
                raise sqlite3.OperationalError("database is locked")
            return real(sql, params)
        with mock.patch.object(led.db, "execute", busy):
            self.assertTrue(led.release_run("RUN-PAY-1-1"))                  # tried again: given back
        self.assertIsNone(led.open_run_for("PAY-1"))
        self.assertEqual(led.reserve_run("PAY-1", tb.P, 5, t.sha), "RUN-PAY-1-1")
        fails[0] = 99
        with mock.patch.object(led.db, "execute", busy):
            self.assertFalse(led.release_run("RUN-PAY-1-1"))                 # never raises
        self.assertIsNotNone(led.open_run_for("PAY-1", stale_after=900))
        with later(16):
            self.assertIsNone(led.open_run_for("PAY-1", stale_after=900))    # left over: counts for nobody


# ---------- 25: an open decision an older bridge recorded without its question note ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class OldOpenDecisionWithoutQuestionNote(unittest.TestCase):
    def test_it_is_asked_again_and_answered(self):
        t = fixture(self)
        t.dispatched()
        t.park()
        self.assertEqual(t.bridge.watch(), 1)
        db = t.bridge.ledger.db
        db.execute("UPDATE decisions SET qnote_id=0 WHERE id='D-1'")   # what c9e59d1 stored for a post without an id
        t.reply(5, "alice", "answer D-1 B")                              # a reply to the old question
        t.bridge = t.new_bridge()                                        # the new bridge opens that ledger
        self.assertEqual(t.bridge.collect(), 0)
        self.assertEqual(state(t), "posting")
        self.assertEqual(t.bridge.watch(), 1)                            # asked once more
        self.assertEqual(state(t), "open")
        t.reply(5, "alice", "answer D-1 B")
        self.assertEqual(t.bridge.collect(), 1)
        self.assertFalse([n for n in t.notes_db[5] if n["body"].startswith("D-1 作废")])


# ---------- 27: configs that ran on c9e59d1 ----------

class SignerConfigThatUsedToWork(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        qs.env(self, HOME=self.d, QA_R2_GITLAB_TOKEN="not-used")

    def config(self, **signer):
        d = self.d
        with open(os.path.join(d, "identities.json"), "w") as f:
            json.dump({"users": {"alice": {}}, "roles": {}}, f)
        s = {"type": "spool", "spool_dir": os.path.join(d, "signer"),
             "allowed_signers": os.path.join(d, "signing-service.pub")}           # not deployed on this host yet
        s.update(signer)
        cfg = {"projects": ["grp/repo"], "identities": os.path.join(d, "identities.json"),
               "vouchers_dir": os.path.join(d, "vouchers"), "spool_dir": os.path.join(d, "spool"),
               "ledger": os.path.join(d, "ledger.sqlite"), "signer": s,
               "gitlab": {"base": "https://gitlab.invalid", "token_env": "QA_R2_GITLAB_TOKEN"}}
        p = os.path.join(d, "bridge.json")
        with open(p, "w") as f:
            json.dump(cfg, f)
        return p

    def test_report_without_the_signing_services_public_key(self):
        p = self.config()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = handoff.main(["--config", p, "report"])
        self.assertEqual(rc, 0, err.getvalue().strip())
        self.assertIn("runs", json.loads(out.getvalue()))

    def test_timeout_and_poll_written_as_numeric_strings(self):
        cfg = handoff.load_bridge_config(self.config(timeout="30", poll=" 0.5 "))
        self.assertEqual(cfg["signer"]["timeout"], "30")
        s = sign.signer_from_config(cfg)
        self.assertEqual((s.timeout, s.poll), (30.0, 0.5))
        for bad in ("30s", "0", "-1", "nan", "inf", True):
            with self.subTest(bad=bad), self.assertRaises(BrainError):
                handoff.load_bridge_config(self.config(timeout=bad))


# ---------- 28: two bridge processes on one ledger ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class WatchFromTwoProcesses(unittest.TestCase):
    def bridge(self, t):
        return Bridge(t.cfg, GitLab("https://gl.example", "t", t.fake), Jira("https://jira.example", "t", "2", t.fake),
                      KiroCrew("http://127.0.0.1:5476", "brain-bridge", "app-secret", t.fake), out=lambda *_: None)

    def test_two_bridges_on_one_ledger_ask_one_question(self):
        t = fixture(self)
        t.dispatched()
        t.park()
        first, second = self.bridge(t), self.bridge(t)                  # each its own sqlite connection
        posting, second_done, errors = threading.Event(), threading.Event(), []
        post = first.gl.comment

        def slow_post(project, iid, text):
            if text.startswith("**D-"):
                posting.set()
                second_done.wait(30)
            return post(project, iid, text)
        first.gl.comment = slow_post

        def watch():
            try:
                first.watch()
            except Exception as e:  # noqa: BLE001 - asserted below
                errors.append("first: %s: %s" % (type(e).__name__, e))
        th = threading.Thread(target=watch)
        th.start()
        self.assertTrue(posting.wait(60))
        try:
            self.assertEqual(second.watch(), 0)
        finally:
            second_done.set()
        th.join(60)
        asked = [n for n in t.notes_db[5] if n["body"].startswith("**D-1")]
        self.assertEqual((t.bridge.ledger.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], len(asked),
                          errors), (1, 1, []))
        self.assertEqual(state(t), "open")

    def test_a_post_left_by_a_stopped_bridge_is_asked_again(self):
        t = fixture(self)
        t.dispatched()
        t.park()

        def stopped(project, iid, text):
            raise SystemExit(143)
        t.bridge.gl.comment = stopped
        with self.assertRaises(SystemExit):
            t.bridge.watch()
        self.assertEqual(state(t), "posting")                          # given back on the way out
        t.bridge.ledger.db.execute("UPDATE decisions SET state='asking'")   # as a SIGKILL would leave it
        t.bridge = t.new_bridge()
        self.assertEqual(t.bridge.watch(), 0)                          # another bridge may be posting it
        with mock.patch.object(decisions, "utcnow", lambda: ledger_mod.utcnow() + timedelta(minutes=10)):
            self.assertEqual(t.bridge.watch(), 1)
        self.assertEqual(len([n for n in t.notes_db[5] if n["body"].startswith("**D-1")]), 1)


# ---------- 21, 22, 23: `brain handoff` and what the bridge tells the owner ----------

@unittest.skipUnless(HAVE_LEAN and sign.available(), "Lean checkout or ssh-keygen missing")
class HandoffCli(unittest.TestCase):
    def setUp(self):
        h = self.h = qs.HandoffLabelsWhatItPushed("test_a_clean_handoff_is_labelled_and_accepted")
        h.setUp()
        self.addCleanup(h.doCleanups)

    def test_22_someone_pushed_after_the_owner_exits_1(self):
        h = self.h
        h.world.concurrent_push = h.push_by_another_person
        rc, out = qs.brain(h.repo, "handoff")
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT labelled", out)
        self.assertNotIn("ai-handoff", h.world.labels)

    def test_22_gitlab_did_not_record_the_push_in_time_exits_1(self):
        h = self.h
        qs.env(self, BRAIN_MR_VERSION_WAIT="0")
        h.world.record = lambda: None
        rc, out = qs.brain(h.repo, "handoff")
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT labelled", out)

    def test_21_a_poll_between_the_clis_remove_and_add(self):
        h, w = self.h, self.h.world
        w.mr_iid = 5
        sh("git", "-C", h.repo, "push", "-q", "-u", "origin", qs.BR)
        w.record()
        w.labels.add("ai-handoff")
        w.events.append({"id": 800, "action": "add", "label": {"name": "ai-handoff"},
                         "user": {"username": "alice"}, "created_at": w.tick()})
        br, _ = h.bridge()
        kit = br.kit("grp/repo")
        results = []
        put = [r for r in h.fake.routes if r[0] == "PUT"][0][2]

        def put_hook(m, qy, b):
            out = put(m, qy, b)
            if (b or {}).get("remove_labels") == "ai-handoff" and not (b or {}).get("add_labels"):
                results.append(br.process("grp/repo", kit, w.mr()))      # the poll lands between remove and add
            return out
        h.fake.routes.insert(0, ("PUT", re.compile("^/api/v4/projects/grp/repo/merge_requests/5$"), put_hook))
        rc, out = qs.brain(h.repo, "handoff")
        self.assertEqual((rc, results), (0, ["unlabelled"]), out)
        for mr in br.gl.mrs_with_label("grp/repo", "ai-handoff"):
            results.append(br.process("grp/repo", kit, mr))
        self.assertEqual(results, ["unlabelled", "accepted"])
        self.assertFalse([n for n in w.notes if "AI 接手被拒" in n["body"]])


class LabelCheckWait(unittest.TestCase):
    """`brain handoff` waits up to BRAIN_MR_VERSION_WAIT for GitLab to record its push, in steps of at most 2 s,
    and not past the deadline (tests set the wait to 0 instead of sleeping)."""

    def test_steps_end_at_the_deadline(self):
        clock, slept = [0.0], []

        def sleep(s):
            slept.append(s)
            clock[0] += s

        class Slow:
            def mr_versions(self, project, iid):
                return []
        with mock.patch.object(cli.time, "monotonic", lambda: clock[0]), mock.patch.object(cli.time, "sleep", sleep):
            why, mr = cli._label_check(Slow(), "grp/repo", 5, "a" * 40, timeout=5)
            self.assertEqual((slept, mr), ([2.0, 2.0, 1.0], None))
            self.assertIn("has not recorded", why)
            del slept[:]
            qs.env(self, BRAIN_MR_VERSION_WAIT="0")
            self.assertIn("has not recorded", cli._label_check(Slow(), "grp/repo", 5, "a" * 40)[0])
            self.assertEqual(slept, [])


@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class RejectionAdvice(unittest.TestCase):
    def test_23_following_the_rejection_comment(self):
        t = fixture(self)
        f = t.push("src/pay/forgotten.py", "x = 1\n", msg="alice: the file I forgot")
        t.mrs[5]["sha"] = f
        t.versions[5] = [{"id": 2, "head_commit_sha": f, "created_at": tb.tick()},     # pushed after the label
                         {"id": 1, "head_commit_sha": t.sha, "created_at": tb.PUSHED}]
        self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "rejected")
        first = t.notes()[-1]
        self.assertIn("run `brain handoff` again", first)
        self.assertNotIn("add the label again to hand it over", first)
        self.assertIn("重新运行 `brain handoff`", first)
        # alice does what it says: `brain handoff` commits the hand-over on top of F, pushes, labels
        h2 = t.push("runs/PAY-1/run.json", json.dumps(t.run_json(handoff={
            "requested_at": "2026-11-09T02:00:00Z", "by": "alice", "local_head": f})))
        t.mrs[5]["sha"] = h2
        t.versions[5].insert(0, {"id": 3, "head_commit_sha": h2, "created_at": tb.tick()})
        t.events[5].append(t.ev("alice", "add"))
        self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "accepted", t.notes()[-1])

    def test_a_rejection_overtaken_by_a_newer_label_changes_no_labels(self):
        t = fixture(self)
        t.issue["assignee"] = {"name": "bob"}
        events = [r for r in t.fake.routes if r[0] == "GET" and "resource_label_events" in r[1].pattern][0][2]
        calls = []

        def relabelled_meanwhile(m, qy, b):
            calls.append(1)
            if len(calls) == 2:                                   # after the check read the event, before its verdict
                t.events[5].append(t.ev("alice", "add"))
            return events(m, qy, b)
        t.fake.routes.insert(0, ("GET", re.compile(r"^%s/merge_requests/(\d+)/resource_label_events$" % tb.GL),
                                 relabelled_meanwhile))
        self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "rejected")
        self.assertEqual((t.notes(), t.labels()), ([], []))      # the newer label is checked next round


# ---------- 29: the owner's own crew agent settings ----------

class CrewAgentKeepsTheOwnersSettings(unittest.TestCase):
    def setUp(self):
        home = tmpdir(self)
        qs.env(self, HOME=home)
        self.path = os.path.join(home, ".kiro", "agents", "brain-dev.json")
        spec = load_json(cli.CREW_AGENT_SRC)
        spec["model"] = "claude-sonnet-4.5"
        spec["tools"] = spec["tools"] + ["@payments-mcp"]
        self.spec = spec
        write_json(self.path, spec)

    def crew(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            rc = cli.main(["crew-agent"] + list(args))
        return rc, out.getvalue()

    def test_all_hooks_present_is_nothing_to_do(self):
        rc, out = self.crew()
        self.assertEqual(rc, 0, out)
        self.assertEqual(load_json(self.path), self.spec)
        self.assertIn("--reset", out)

    def test_force_to_add_a_missing_hook_keeps_model_and_tools(self):
        write_json(self.path, dict(self.spec, hooks={k: v for k, v in self.spec["hooks"].items()
                                                     if k != "preToolUse"}))
        rc, out = self.crew("--force")
        self.assertEqual(rc, 0, out)
        got = load_json(self.path)
        self.assertEqual(cli.missing_lean_hooks(got), [])
        self.assertEqual((got["model"], got["tools"]), (self.spec["model"], self.spec["tools"]))

    def test_reset_takes_brain_kits(self):
        rc, out = self.crew("--reset")
        self.assertEqual(rc, 0, out)
        ship = load_json(cli.CREW_AGENT_SRC)
        got = load_json(self.path)
        self.assertEqual((got["model"], got["tools"]), (ship["model"], ship["tools"]))

    def test_doctor_does_not_warn_about_the_owners_choice(self):
        with mock.patch.object(cli.shutil, "which", lambda name: "/usr/bin/" + name if name == "kirocrew" else None), \
                mock.patch.object(cli, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "kas\n", "")):
            rows = cli.crew_checks(tmpdir(self))
        self.assertFalse([r for r in rows if r[0] == "WARN" and "brain-dev" in r[1]], rows)
        self.assertTrue([r for r in rows if r[0] == "OK" and "your own model, tools" in r[1]], rows)


# ---------- 9, 35, 36: intents, keys, reflect --draft ----------

TITLE = "Checkout p99 above 2s"
BODY = "Reported by New Relic: checkout p99 latency above 2s for 15 minutes."


def a_rule_that_flags_the_old_text():
    """Today's content rules flag text an intent was filed with under older ones (the reviewer's case: a person
    rule that took "New Relic" for a name). Patched in, so this does not depend on the rule of the day."""
    real = brainlint.content_problems

    def flagged(text):
        errs, warns = real(text)
        return (list(errs) + ["personal data (person)"] if "New Relic" in str(text) else errs), warns
    return mock.patch.object(brainlint, "content_problems", flagged)


class OldIntent(unittest.TestCase):
    def setUp(self):
        d = tmpdir(self)
        qs.env(self, HOME=d, BRAIN_USER="alice", GITLAB_TOKEN=None)
        self.repo = git_repo(os.path.join(d, "r"))
        commit_file(self.repo, ".brain/kit.json", json.dumps({"version": 1, "req_prefix": "PAY", "tracker": "none"}))
        fields = {"source": "alert", "body": BODY, "acs": [], "refs": [],
                  "hash": intents.digest("alert", TITLE, BODY)}
        self.doc = claims.claim(self.repo, "PAY", "alice", TITLE, extra=fields)
        p = a_rule_that_flags_the_old_text()
        p.start()
        self.addCleanup(p.stop)
        self.assertTrue(intents.doc_problems(self.doc))                  # today's rules refuse its text

    def test_owner_can_close_it_through_the_hook(self):
        old = claims._commit(self.repo, self.doc)
        closed = dict(self.doc, closed={"at": "2026-11-09T00:00:00Z", "by": "alice", "reason": "threshold raised"})
        new = claims._commit(self.repo, closed, parent=old)
        self.assertEqual(pre_receive.decide([(old, new, "refs/heads/claim/PAY-1")], "alice", pre_receive.DEFAULT_CFG,
                                            cwd=self.repo), [])

    def test_a_change_to_the_text_is_still_checked(self):
        old = claims._commit(self.repo, self.doc)
        body = BODY + " Seen by New Relic again."
        changed = dict(self.doc, body=body, hash=intents.digest("alert", TITLE, body))
        new = claims._commit(self.repo, changed, parent=old)
        r = pre_receive.decide([(old, new, "refs/heads/claim/PAY-1")], "alice", pre_receive.DEFAULT_CFG, cwd=self.repo)
        self.assertTrue(r and "personal data" in r[0], r)
        bad_close = dict(self.doc, closed={"at": "x", "by": "alice", "reason": "New Relic said so"})
        new = claims._commit(self.repo, bad_close, parent=old)
        r = pre_receive.decide([(old, new, "refs/heads/claim/PAY-1")], "alice", pre_receive.DEFAULT_CFG, cwd=self.repo)
        self.assertTrue(r and "closed.reason" in r[0], r)
        new = claims._commit(self.repo, dict(self.doc, extra=1), parent=old)
        r = pre_receive.decide([(old, new, "refs/heads/claim/PAY-1")], "alice", pre_receive.DEFAULT_CFG, cwd=self.repo)
        self.assertTrue(r and "unknown fields extra" in r[0], r)

    def test_the_daily_job_gets_the_same_key_back(self):
        doc = intents.add(self.repo, config.load(self.repo), "alert", TITLE, BODY, owner="alice")
        self.assertEqual((doc["key"], doc["duplicate"]), ("PAY-1", True))
        with self.assertRaises(BrainError):                               # new text is refused as before
            intents.add(self.repo, config.load(self.repo), "alert", TITLE, BODY + " New Relic", owner="alice")

    @unittest.skipUnless(HAVE_LEAN, "AI-DLC Lean checkout not found")
    def test_start_on_it(self):
        repo = git_repo(os.path.join(tmpdir(self), "proj"))
        commit_file(repo, "src/app.py", "print('hi')\n", "init")
        rc, out = qs.brain(repo, "install", repo, "--lean", LEAN_SRC, "--prefix", "PAY")
        self.assertEqual(rc, 0, out)
        sh("git", "-C", repo, "add", "-A")
        sh("git", "-C", repo, "commit", "-q", "-m", "install kit")
        fields = {k: self.doc[k] for k in ("source", "body", "acs", "refs", "hash")}
        claims.claim(repo, "PAY", "alice", TITLE, extra=fields)
        rc, out = qs.brain(repo, "start", "latency", "--key", "PAY-1")
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN: claim/PAY-1: body: personal data", out)
        self.assertTrue(os.path.exists(os.path.join(repo, "runs", "PAY-1", "intent.md")))

    def test_start_still_refuses_a_malformed_claim(self):
        bad = dict(self.doc, hash="0" * 16)
        self.assertIn("hash does not match source, title and body", intents.doc_problems(bad, content=False))


class OverlongRunsFolder(unittest.TestCase):
    def setUp(self):
        d = tmpdir(self)
        qs.env(self, HOME=d, BRAIN_USER="carol", GITLAB_TOKEN=None)
        self.repo = git_repo(os.path.join(d, "r"))
        commit_file(self.repo, ".brain/kit.json", json.dumps({"version": 1, "req_prefix": "PAY", "tracker": "none"}))
        commit_file(self.repo, "runs/PAY-12345678/notes.md", "typo folder\n")

    def test_claim_gets_a_valid_key(self):
        doc = claims.claim(self.repo, "PAY", "carol", "next")
        self.assertEqual(doc["key"], "PAY-1")
        self.assertTrue(JIRA_RE.match(doc["key"]))

    def test_intent_add_gets_a_valid_key(self):
        rc, out = qs.brain(self.repo, "intent", "add", "--source", "debt", "--title", "retry", "--body", "add retries",
                           "--json")
        self.assertEqual(rc, 0, out)
        self.assertTrue(JIRA_RE.match(json.loads(out)["key"]), out)

    def test_no_key_past_seven_digits(self):
        commit_file(self.repo, "runs/PAY-9999999/notes.md", "the last one\n")
        with self.assertRaises(BrainError) as e:
            claims.claim(self.repo, "PAY", "carol", "next")
        self.assertIn("at most 7 digits", str(e.exception))


class DraftWithOriginUnreachable(unittest.TestCase):
    def test_error_says_origin_was_not_reached(self):
        d = tmpdir(self)
        qs.env(self, HOME=d, BRAIN_USER="carol", GITLAB_TOKEN=None)
        repo = git_repo(os.path.join(d, "p"))
        commit_file(repo, ".brain/kit.json", json.dumps({"version": 1, "req_prefix": "PAY", "tracker": "none"}))
        sh("git", "-C", repo, "switch", "-q", "-c", "ai/alice/PAY-1-refund")
        commit_file(repo, "runs/PAY-1/run.json", json.dumps({"key": "PAY-1", "intent": "261102-pay-1-refund"}))
        sh("git", "-C", repo, "switch", "-q", "main")
        bare = os.path.join(d, "origin.git")
        sh("git", "init", "-q", "--bare", bare)
        sh("git", "-C", repo, "remote", "add", "origin", bare)
        sh("git", "-C", repo, "push", "-q", "origin", "main", "ai/alice/PAY-1-refund")
        sh("git", "-C", repo, "branch", "-q", "-D", "ai/alice/PAY-1-refund")
        sh("git", "-C", repo, "update-ref", "-d", "refs/remotes/origin/ai/alice/PAY-1-refund")
        os.rename(bare, bare + ".gone")
        rc, out = qs.brain(repo, "reflect", "--draft", "PAY-1")
        self.assertEqual(rc, 2, out)
        self.assertNotIn("or on origin", out)
        self.assertIn("origin could not be asked", out)


# ---------- 38: the late-signature tests on a slow machine ----------

class SlowSshKeygen:
    def __init__(self, real, delay):
        self.real, self.delay = real, delay

    def __call__(self, cmd, *a, **kw):
        if list(cmd[:4]) == ["ssh-keygen", "-q", "-Y", "sign"]:
            time.sleep(self.delay)
        return self.real(cmd, *a, **kw)


@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class LateSignatureOnASlowBox(unittest.TestCase):
    """ssh-keygen -Y sign made slower than the fixed sleeps the old test took for 'the next round'."""

    def run_original(self, name):
        qs.env(self, HOME=tmpdir(self))
        with mock.patch.object(sign, "run", SlowSshKeygen(sign.run, 1.5)):
            result = unittest.TestResult()
            qs.LateSignatureIsUsed(name).run(result)
        return result

    def test_handoff(self):
        r = self.run_original("test_handoff")
        self.assertEqual((r.failures, r.errors), ([], []))

    def test_answer(self):
        r = self.run_original("test_answer")
        self.assertEqual((r.failures, r.errors), ([], []))


if __name__ == "__main__":
    unittest.main()
