"""Regression tests for BRAINKIT_REQUESTS items 6-9 (the Brain app's adversarial review): a hand-over is bound to the
commit the owner labelled, an answer to its question and push, a design approval to the design it approved, and a
flood of MR notes is an error instead of a short list."""
import hashlib
import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import ci, config, sign  # noqa: E402
from brainkit.bridge import decisions  # noqa: E402
from brainkit.common import BrainError, write_json  # noqa: E402
from brainkit.rest import GitLab, gl_time, head_pushed_at  # noqa: E402
from tests import test_bridge as tb  # noqa: E402
from tests.helpers import LEAN_SRC, Fake, commit_file, git_repo, keypair, sh, tmpdir  # noqa: E402

OLD_DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, project TEXT NOT NULL, iid INTEGER NOT NULL, sha TEXT NOT NULL,
  park_hash TEXT NOT NULL, kind TEXT NOT NULL, lean_request TEXT, state TEXT NOT NULL, note_id INTEGER,
  answer TEXT, answerer TEXT, voucher_id TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
  UNIQUE (run_id, sha, park_hash));
"""


def bridge(test):
    """test_bridge's repos and GitLab / Jira / KiroCrew fake, for one test."""
    t = tb.BridgeTest("test_kit_config_comes_from_main")
    t.setUp()
    test.addCleanup(t.doCleanups)
    return t


def park_hash(t, sha):
    return hashlib.sha256(t.bridge.gl.file_raw(tb.P, "runs/PAY-1/park.json", sha)).hexdigest()


def decision(t, did="D-1"):
    return t.bridge.ledger.db.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()


# ---------- 6: a hand-over is the commit the owner labelled ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class HandoverIsTheLabelledCommit(unittest.TestCase):
    def setUp(self):
        self.t = bridge(self)

    def test_a_push_after_the_label_is_not_handed_over(self):
        t = self.t
        labelled = t.sha
        # after alice labelled, someone who may push to ai/* adds a commit; its dates claim it is years old
        sh("git", "-C", t.work, "switch", "-q", tb.BRANCH)
        old = {"GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"}
        pushed = commit_file(t.work, "src/later.py", "x = 1\n", "looks old", env=old)
        sh("git", "-C", t.work, "push", "-q", "-f", t.origin, "%s:refs/heads/%s" % (tb.BRANCH, tb.BRANCH))
        t.mrs[5]["sha"] = pushed
        t.versions[5] = [{"id": 2, "head_commit_sha": pushed, "created_at": tb.tick()},     # GitLab's own time
                         {"id": 1, "head_commit_sha": labelled, "created_at": tb.PUSHED}]
        t.rejected_with("was pushed")
        self.assertTrue(any("after the label 'ai-handoff' was added" in n for n in t.notes()), t.notes())
        # what an outside signer calls (no label event passed in) finds the same
        probs, _ = t.bridge.verify(tb.P, t.bridge.kit(tb.P), t.mrs[5])
        self.assertTrue(any("was pushed" in p for p in probs), probs)
        # alice looks at the MR and labels again: that commit is still not the hand-over commit (it does not write
        # run.json), so a label alone does not hand it over (S1)
        t.events[5].append(t.ev("alice", "add"))
        t.rejected_with("not the hand-over commit")
        # she keeps it and runs `brain handoff` again: a hand-over commit on top, labelled after its push
        h2 = t.push("runs/PAY-1/run.json", json.dumps(t.run_json(handoff={
            "requested_at": "2026-11-09T02:00:00Z", "by": "alice", "local_head": pushed})))
        t.mrs[5]["sha"] = h2
        t.versions[5] = [{"id": 3, "head_commit_sha": h2, "created_at": tb.tick()}] + t.versions[5]
        t.events[5].append(t.ev("alice", "add"))
        self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "accepted", t.notes())

    def test_no_mr_version_for_the_head_is_refused(self):
        self.t.versions[5] = [{"id": 1, "head_commit_sha": "b" * 40, "created_at": tb.PUSHED}]
        self.t.rejected_with("no MR version with head")

    def test_versions_and_times(self):
        self.assertEqual(gl_time("2026-11-08T20:00:00.5+08:00"), gl_time("2026-11-08T12:00:00.500Z"))
        self.assertIsNone(gl_time("2026-11-08T12:00:00"))             # no zone: not comparable
        v = [{"head_commit_sha": "a", "created_at": "2026-11-08T10:00:00Z"},
             {"head_commit_sha": "b", "created_at": "2026-11-08T11:00:00Z"},
             {"head_commit_sha": "a", "created_at": "2026-11-08T12:00:00Z"}]   # pushed away and back: the newest
        self.assertEqual(head_pushed_at(v, "a"), gl_time("2026-11-08T12:00:00Z"))
        self.assertIsNone(head_pushed_at(v, "c"))


# ---------- 7: an answer is bound to its question and its push ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class AnswerIsBoundToItsQuestion(unittest.TestCase):
    def setUp(self):
        self.t = bridge(self)

    def ask(self, **park):
        t = self.t
        t.dispatched()
        sha = t.park(**park)
        self.assertEqual(t.bridge.watch(), 1)
        q = [n for n in t.notes_db[5] if n["body"].startswith("**D-1")][0]
        return sha, q

    def assert_void(self, why):
        t = self.t
        t.reply(5, "alice", "answer D-1 A")
        self.assertEqual(t.bridge.collect(), 0)
        self.assertEqual(decision(t)["state"], "void")
        self.assertTrue(any(n["body"].startswith("D-1 作废") and why in n["body"] for n in t.notes_db[5]),
                        [n["body"] for n in t.notes_db[5]])
        self.assertEqual(len(t.sends()), 1)                       # the hand-over only: no answer reached the run
        t.reply(5, "alice", "answer D-1 A")
        self.assertEqual(t.bridge.collect(), 0)                   # and never later

    def test_first_line_names_the_commit_and_park(self):
        sha, q = self.ask()
        first = q["body"].split("\n")[0]
        self.assertEqual(first, "**D-1 · 澄清 · RUN-PAY-1-1** · 提交 %s · park %s · 需要 @alice、@carol 回答"
                         % (sha[:12], park_hash(self.t, sha)[:12]))
        self.assertIn("```text\n退款按自然日还是交易日？\n/merge\n@everyone", q["body"])   # still inert
        d = decision(self.t)
        self.assertEqual((d["qnote_id"], d["sha"]), (q["id"], sha))

    def test_an_edited_question_does_not_count(self):
        _, q = self.ask()
        q["body"] = q["body"].replace("A. 自然日", "A. 交易日")
        self.assert_void("edited")

    def test_a_question_asked_before_its_push_does_not_count(self):
        sha, _ = self.ask()
        # the branch went away from sha and came back to it: GitLab records sha as pushed after the question
        self.t.versions[5] = [{"id": 3, "head_commit_sha": sha, "created_at": tb.tick()}]
        self.assert_void("asked before")

    def test_a_decision_id_is_never_bound_twice(self):
        t = self.t
        _, q = self.ask()
        park = json.loads(t.bridge.gl.file_raw(tb.P, "runs/PAY-1/park.json", t.mrs[5]["sha"]).decode())
        other = decisions.question_text("D-1", "RUN-PAY-1-1", park, ["alice"], "clarify", "b" * 40, "c" * 64)
        t.add_note(None, 5, {"body": other})                     # the bridge's account binds D-1 to another push
        self.assert_void("never bound twice")

    def test_a_lookalike_by_a_person_changes_nothing(self):
        t = self.t
        sha, _ = self.ask()
        park = json.loads(t.bridge.gl.file_raw(tb.P, "runs/PAY-1/park.json", sha).decode())
        fake = decisions.question_text("D-1", "RUN-PAY-1-1", park, ["bob"], "clarify", "b" * 40, "c" * 64)
        t.add_note(None, 5, {"body": fake}, author="bob")         # the bridge knows its account from its own note
        t.reply(5, "alice", "answer D-1 A")
        self.assertEqual(t.bridge.collect(), 1)

    def test_a_deleted_question_does_not_count(self):
        _, q = self.ask()
        self.t.notes_db[5].remove(q)
        self.t.notes_db.setdefault(0, []).append(q)     # GitLab never reuses a note id; the fake counts notes
        self.assert_void("not on the MR")

    def test_questions_of_an_older_bridge_are_asked_again(self):
        """A ledger from before this change: its open question names neither commit nor park, so it is posted once
        more in the new layout; replies to the old one do not count."""
        t = self.t
        t.dispatched()
        sha = t.park()
        ph = park_hash(t, sha)
        db = t.bridge.ledger.db
        db.executescript(OLD_DECISIONS_SCHEMA)
        t.add_note(None, 5, {"body": "**D-1 · 澄清 · RUN-PAY-1-1** 需要 @alice 回答\n\n```text\nold\n```"})
        old_q = t.notes_db[5][-1]["id"]
        db.execute("INSERT INTO decisions(id,run_id,project,iid,sha,park_hash,kind,state,note_id,created,updated) "
                   "VALUES ('D-1','RUN-PAY-1-1',?,5,?,?,'clarify','open',?,'2026-11-08T00:00:00Z',"
                   "'2026-11-08T00:00:00Z')", (tb.P, sha, ph, old_q))
        db.execute("UPDATE runs SET last_sha=? WHERE run_id='RUN-PAY-1-1'", (sha,))
        t.reply(5, "alice", "answer D-1 B")                       # an answer to the unbound question
        t.bridge = t.new_bridge()
        self.assertEqual(t.bridge.collect(), 0)                   # waits for the question to be posted again
        self.assertEqual(t.bridge.watch(), 1)
        qs = [n for n in t.notes_db[5] if n["body"].startswith("**D-1 · 澄清 · RUN-PAY-1-1** · 提交 %s" % sha[:12])]
        self.assertEqual(len(qs), 1)
        self.assertEqual(t.bridge.collect(), 0)                   # "answer D-1 B" came before it
        t.reply(5, "alice", "answer D-1 A")
        self.assertEqual(t.bridge.collect(), 1)
        self.assertIn("：A。", t.sends()[-1]["message"])
        cols = {r[1] for r in sqlite3.connect(t.cfg["ledger"]).execute("PRAGMA table_info(decisions)")}
        self.assertTrue({"qnote_id", "told_id"} <= cols)


@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class OneAnswerVerifier(unittest.TestCase):
    """check_answer is pure: what the bridge delivers, an outside signer finds from the MR alone."""

    def setUp(self):
        self.t = bridge(self)
        t = self.t
        t.overrides[".brain/kit.json"] = json.dumps({"version": 1, "bot_users": ["brain-bot"], "tracker": "jira",
                                                     "jira": {"base": "https://jira.example"},
                                                     "bridge_user": "brain-bridge"}).encode()
        t.fake.on("GET", r"/api/v4/user", lambda m, qy, b: (200, {"username": "brain-bridge"}))
        t.bridge = t.new_bridge()

    def signer_view(self, voucher):
        t = self.t
        kit = t.bridge.kit(tb.P)
        raw = t.bridge.gl.file_raw(tb.P, "runs/PAY-1/park.json", voucher["sha"])
        pushed = head_pushed_at(t.bridge.gl.mr_versions(tb.P, 5), voucher["sha"])
        d = {"id": voucher["decision"], "run_id": voucher["run_id"], "sha": voucher["sha"],
             "park_hash": voucher["park_hash"], "kind": voucher["decision_kind"],
             "lean_request": voucher.get("lean_request")}
        return decisions.check_answer(d, raw, t.bridge.gl.mr_notes(tb.P, 5), kit, t.bridge.roles, "alice",
                                      t.mrs[5]["sha"], pushed)        # no question id, no cursor: the MR only

    def test_signer_agrees_after_a_conflict_round(self):
        t = self.t
        t.dispatched()
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A")
        t.reply(5, "carol", "answer D-1 B")
        self.assertEqual(t.bridge.collect(), 0)                   # conflict: a new round starts after the bridge's note
        t.reply(5, "carol", "answer D-1 A")
        self.assertEqual(t.bridge.collect(), 1)
        ans = [v for v in t.answers()][0]
        v = self.signer_view(ans)
        self.assertEqual((v.status, v.author, v.choice), ("answer", "carol", "A"))
        self.assertEqual(t.bridge.pseudonym(v.author), ans["answerer"])

    def test_the_bridge_account_never_answers_and_must_be_the_tokens(self):
        t = self.t
        with open(t.cfg["identities"], "w") as f:
            json.dump({"users": {"alice": {"jira": "alice"}, "carol": {}}, "roles": {"product": ["carol",
                                                                                                "brain-bridge"]}}, f)
        t.bridge = t.new_bridge()
        self.assertEqual(t.bridge.allowed(t.bridge.kit(tb.P), "acceptance", "alice"), {"carol"})
        t.fake.routes.insert(0, ("GET", __import__("re").compile(r"^/api/v4/user$"),
                                 lambda m, qy, b: (200, {"username": "someone-else"})))
        t.dispatched()
        t.park()
        with self.assertRaises(BrainError) as e:
            t.bridge.watch()
        self.assertIn("bridge_user is brain-bridge", str(e.exception))

    def test_pure_rules(self):
        park = {"schema": 1, "status": "waiting", "kind": "clarify", "question": "q?",
                "options": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}]}
        raw = json.dumps(park).encode()
        sha, ph = "a" * 40, hashlib.sha256(raw).hexdigest()
        kit = config.merged(config.DEFAULTS, {"bridge_user": "brain-bridge"})
        roles = {"product": ["carol"]}
        q = {"id": 10, "author": {"username": "brain-bridge"}, "created_at": "2026-11-09T01:00:00Z",
             "body": decisions.question_text("D-1", "RUN-PAY-1-1", park, ["alice", "carol"], "clarify", sha, ph)}

        def note(i, user, body):
            return {"id": i, "author": {"username": user}, "body": body, "created_at": "2026-11-09T02:00:00Z"}

        d = {"id": "D-1", "run_id": "RUN-PAY-1-1", "sha": sha, "park_hash": ph}
        pushed = "2026-11-09T00:00:00Z"
        notes = [q, note(11, "bob", "answer D-1 A"), note(12, "carol", "answer D-1 B 交易日")]
        v = decisions.check_answer(d, raw, notes, kit, roles, "alice", sha, pushed)
        self.assertEqual((v.ok, v.author, v.choice, v.note), (True, "carol", "B", "交易日"))
        self.assertEqual(v.feedback, [(11, "@bob 不在 D-1 的可回答名单里，这条回复不算。")])
        self.assertEqual(decisions.check_answer(d, raw, notes[:2], kit, roles, "alice", sha, pushed).status, "none")
        self.assertEqual(decisions.check_answer(d, raw, notes, kit, roles, "alice", "b" * 40, pushed).status, "stale")
        both = notes + [note(13, "alice", "answer D-1 A")]
        self.assertEqual(decisions.check_answer(d, raw, both, kit, roles, "alice", sha, pushed).status, "conflict")
        for why, args in (("not the one", (b"{}", notes, sha, pushed)),
                          ("no MR version", (raw, notes, sha, None)),
                          ("asked before", (raw, notes, sha, "2026-11-09T01:00:01Z")),
                          ("no question", (raw, [dict(q, author={"username": "mallory"})] + notes[1:], sha, pushed))):
            v = decisions.check_answer(d, args[0], args[1], kit, roles, "alice", args[2], args[3])
            self.assertEqual(v.status, "void", why)
            self.assertIn(why, v.reason)


# ---------- 8: a design approval covers the design as approved ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class DesignApprovalCoversTheApprovedDesign(unittest.TestCase):
    BR = "ai/alice/PAY-1-refund"
    INTENT = "261108-pay-1-refund"
    DESIGN = "aidlc-docs/261108-pay-1-refund/inception/application-design/application-design.md"

    def setUp(self):
        d = tmpdir(self)
        r = self.repo = git_repo(os.path.join(d, "proj"))
        commit_file(r, ".brain/kit.json", json.dumps({"version": 1}))
        commit_file(r, "gates/risk-paths", "src/pay/**\n")
        brain_commit = commit_file(r, "brain/TECH.md", "# TECH\n")
        sh("git", "-C", r, "switch", "-q", "-c", self.BR)
        rec = {"schema": 1, "key": "PAY-1", "owner": "alice", "branch": self.BR, "intent": self.INTENT,
               "brain_commit": brain_commit, "kit_version": "0.1.0", "origin": "local", "expected_paths": [],
               "created_at": "2026-11-08T00:00:00Z", "claim_checked": True}
        commit_file(r, "runs/PAY-1/run.json", json.dumps(rec))
        commit_file(r, "aidlc-docs/%s/aidlc-state.md" % self.INTENT, "# AI-DLC State\n")
        commit_file(r, self.DESIGN, "# design v1\n")
        self.asked_at = commit_file(r, "src/pay/charge.py", "x = 1\n", "risky code, then the DESIGN question")
        self.key, self.allowed = keypair(d)
        self.vdir = os.path.join(d, "vouchers")
        self.env = {"BRAIN_VOUCHERS_DIR": self.vdir, "BRAIN_ALLOWED_SIGNERS": self.allowed}

    @staticmethod
    def lean_hash(text):
        return hashlib.sha256(text.encode()).hexdigest()[:8]

    def approve(self, at, text, vid="V-1"):
        v = {"schema": 1, "kind": "answer", "decision": "D-1", "run_id": "RUN-PAY-1-1", "sha": at,
             "choice": "approve", "lean_request": "DESIGN " + self.lean_hash(text), "voucher_id": vid}
        write_json(os.path.join(self.vdir, "RUN-PAY-1-1", vid + ".json"), {"voucher": v, "sig": sign.sign(v, self.key)})

    def probs(self):
        return ci.check_run(self.repo, self.BR, self.env)

    def test_lean_hashes_like_this(self):
        lean_py = os.path.join(LEAN_SRC, "kit", ".kiro", "aidlc-lean", "aidlc_lean.py")
        if not os.path.exists(lean_py):
            self.skipTest("AI-DLC Lean checkout not found")
        import importlib.util
        spec = importlib.util.spec_from_file_location("aidlc_lean_h", lean_py)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.sha256_file(os.path.join(self.repo, self.DESIGN))[:8], self.lean_hash("# design v1\n"))

    def test_the_approved_design_covers_later_risky_code(self):
        self.approve(self.asked_at, "# design v1\n")
        commit_file(self.repo, "src/pay/refund.py", "y = 2\n")
        self.assertEqual(self.probs(), [])

    def test_a_changed_design_with_later_risky_code_needs_a_fresh_approval(self):
        self.approve(self.asked_at, "# design v1\n")
        commit_file(self.repo, self.DESIGN, "# design v2: refunds skip the limit check\n")
        commit_file(self.repo, "src/pay/refund.py", "y = 2\n")
        probs = self.probs()
        self.assertTrue(any("no design approval covers them" in p and "src/pay/refund.py" in p for p in probs), probs)
        # a fresh approval of the design as it is now covers it
        self.approve(sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip(),
                     "# design v2: refunds skip the limit check\n", vid="V-2")
        self.assertEqual(self.probs(), [])

    def test_a_changed_design_alone_keeps_the_approval(self):
        self.approve(self.asked_at, "# design v1\n")
        commit_file(self.repo, self.DESIGN, "# design v1, typo fixed\n")     # no risk path changed after approval
        self.assertEqual(self.probs(), [])

    def test_the_hash_must_name_the_file_at_the_approved_commit(self):
        # the run asked "DESIGN <hash of v2>" while the MR showed v1, and committed v2 with more risky code later
        self.approve(self.asked_at, "# design v2\n")
        commit_file(self.repo, self.DESIGN, "# design v2\n")
        commit_file(self.repo, "src/pay/refund.py", "y = 2\n")
        self.assertTrue(any("no design approval covers them" in p for p in self.probs()), self.probs())


# ---------- 9: too many notes is an error ----------

class NotesAreReadInFullOrNotAtAll(unittest.TestCase):
    def gitlab(self, total):
        f = Fake()
        rows = [{"id": i, "body": "x"} for i in range(total)]

        def page(m, qy, b):
            n, p = int(qy["per_page"]), int(qy["page"])
            return 200, rows[(p - 1) * n: p * n]

        f.on("GET", r"/api/v4/projects/g/r/merge_requests/1/(notes|versions)", page)
        return GitLab("https://gl.example", "t", f)

    def test_page_cap_raises(self):
        gl = self.gitlab(20 * 100 + 1)
        with self.assertRaises(BrainError):
            gl.mr_notes("g/r", 1)
        with self.assertRaises(BrainError):
            gl.mr_versions("g/r", 1)
        self.assertEqual(len(self.gitlab(150).mr_notes("g/r", 1)), 150)
        self.assertEqual(len(self.gitlab(1999).mr_notes("g/r", 1)), 1999)

    @unittest.skipUnless(sign.available(), "ssh-keygen not found")
    def test_collect_does_not_decide_on_a_partial_list(self):
        t = bridge(self)
        t.dispatched()
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A")
        t.fake.routes.insert(0, ("GET", __import__("re").compile(r"^%s/merge_requests/5/notes$" % tb.GL),
                                 lambda m, qy, b: (200, [{"id": 1, "body": "flood", "author": {"username": "x"}}]
                                                   * int(qy["per_page"]))))
        with self.assertRaises(BrainError):
            t.bridge.collect()
        self.assertEqual(decision(t)["state"], "open")
        self.assertEqual(len(t.sends()), 1)


if __name__ == "__main__":
    unittest.main()
