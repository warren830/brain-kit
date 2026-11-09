"""QA round 3 on the Run Trace and Eval, ported from the reviewer's qa_r3 suite (#11 ... #15), and one test per rule
those findings added to docs/eval.md: the hand-over (H1-H4), R3 only after the hand-over, a merge made on another
branch (M1b), what counts as a hotfix (A1, A2), and how Eval sees an unfinished batch (E1-E5). Each test names its
finding or rule; the docstring says what was wrong (findings) or what the rule is. Everything is local: temp git
repos, a sqlite ledger on disk, a throwaway HOME; no network."""
import datetime
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brainkit import evaluate as EV, trace  # noqa: E402
from tests.helpers import tmpdir  # noqa: E402
from tests.test_qa_trace_eval import AS_OF, BOT, World, audit, base_run, release, run_cli, table, vid  # noqa: E402

ALICE = "alice@example.com"
LEAD = "lead@example.com"
BR = "ai/alice/PAY-1-refund"
INTENT = "261102-pay-1-refund"


def env_at(date, email=ALICE):
    return {"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_EMAIL": email}


def temp_home(test):
    """HOME is a temp dir (`brain reflect` makes worktrees under ~/.brain/worktrees: never the real one)."""
    home = tmpdir(test)
    with open(os.path.join(home, ".gitconfig"), "w", encoding="utf-8") as f:
        f.write("[user]\n\tname = QA\n\temail = qa@example.com\n")
    p = mock.patch.dict(os.environ, {"HOME": home})
    p.start()
    test.addCleanup(p.stop)
    return home


def start_run(w, key="PAY-1", br=BR, intent=INTENT, day=2):
    """`brain start` on a run branch (local half: REQUIREMENTS approved). -> (run.json record, local Lean events)."""
    base = w.rev("main")
    w.git("switch", "-q", "-c", br, "main")
    d = "2026-11-%02d" % day
    rec = {"schema": 1, "key": key, "tracker": "none", "owner": "alice", "branch": br, "intent": intent,
           "brain_commit": base, "kit_version": "0.1.0", "origin": "local", "expected_paths": [], "materials": [],
           "created_at": d + "T02:00:00Z", "claim_checked": True, "handoff": None}
    ev = [(d + "T02:00:00.000Z", "INTENT_CREATED", intent, {"title": key}),
          (d + "T02:20:00.000Z", "APPROVED", intent, {"kind": "REQUIREMENTS", "sha256": "ab" * 32,
                                                     "words": "approve REQUIREMENTS abababab"})]
    w.commit({"runs/%s/run.json" % key: json.dumps(rec),
              "aidlc-docs/%s/decisions.md" % intent: "# Decisions\n\n" + table([["D-1", d, "requirements", "x", "y",
                                                                                  "agent"]]),
              "aidlc-docs/audit/local-%s.jsonl" % key: audit(ev, ALICE)}, "%s: start run" % key, d + "T02:00:00Z")
    return rec, ev


def hand_off(w, rec, key, at):
    rec["handoff"] = {"requested_at": at, "by": "alice", "local_head": w.rev()}
    return w.commit({"runs/%s/run.json" % key: json.dumps(rec)}, "%s: hand off to team runner" % key, at)


def runner_half(w, key="PAY-1", intent=INTENT, day=2, release_at="06:00"):
    """The runner's commits (bot), its RELEASE approved through the bridge."""
    d = "2026-11-%02d" % day
    (_, _), ev, data = release()
    runner = [(d + "T04:00:00.000Z", "SESSION_START", intent, {"session": "k1"}),
              (d + "T04:30:00.000Z", "VERIFY", intent, {"ok": True, "fingerprint": "f2f2f2f2", "results": []}),
              (d + "T%s:00.000Z" % release_at, ev, intent, data)]
    w.commit({"src/%s.py" % key.lower().replace("-", "_"): "x = 1\n",
              "aidlc-docs/audit/runner-%s.jsonl" % key: audit(runner, BOT)}, "%s: code" % key, d + "T05:00:00Z", BOT)
    return w.commit({"aidlc-docs/%s/notes.md" % intent: "done\n"}, "%s: records" % key, d + "T07:00:00Z", BOT)


def judge(w, key="PAY-1", **kw):
    t = trace.build(w.repo, w.cfg(), key, as_of=AS_OF, **kw)
    led = {"ledger_path": kw["ledger_path"]} if kw.get("ledger_path") else {}
    return t, EV.collect(w.repo, w.cfg(), as_of=AS_OF, traces=[t], **led)


def pushes(t):
    return [h for h in t["humans"] if h["kind"] == "push_after_handover"]


# ---------- #11: the hand-over is the one the bridge accepted (H1-H4) ----------

class Handover(unittest.TestCase):
    def test_11_h4_a_second_brain_handoff_after_exit_1_is_not_a_push(self):
        """#11: the first `brain handoff` did not go through (exit 1, NOT labelled); the second did. The trace took
        the FIRST run.json with a handoff as the hand-over, so the owner's second `brain handoff` commit became a
        person's push after the hand-over: not autonomous, and an intervention outside a judgment point. Rule H4 (no
        record from the bridge): the hand-over is the last request before the runner's first commit."""
        w = World(self)
        rec, _ = start_run(w)
        hand_off(w, rec, "PAY-1", "2026-11-02T03:00:00Z")
        second = hand_off(w, rec, "PAY-1", "2026-11-02T03:10:00Z")
        runner_half(w)
        w.merge({"branch": BR, "intent": INTENT, "key": "PAY-1"}, "merge", date="2026-11-03T00:00:00Z")
        t, d = judge(w)
        ho = t["outcome"]["handed_over"]
        self.assertEqual(pushes(t), [], "the owner's second `brain handoff` commit is counted as a push")
        self.assertEqual((ho["value"], ho["commit"], ho["rule"], ho["requests"], ho["at"]),
                         (True, second, "H4", 2, "2026-11-02T03:10:00Z"), ho)
        self.assertEqual((d["autonomy"]["value"], d["judgment"]["value"]), ([1, 1], [2, 2]), d["autonomy"]["runs"])
        self.assertEqual([h["taken"] for h in t["actions"]["handovers"] if h["kind"] == "requested"], [False, True])

    def test_11_h2_a_refused_handover_finished_locally_is_local_only(self):
        """#11: the bridge REFUSED the hand-over (ledger handoffs 'rejected', no run, no contract); the owner finished
        locally and merged. The trace said handed over (run.json has a handoff), Eval counted it as a hand-over run
        that was not autonomous, and the owner's local commits as pushes after the hand-over. Rule H2: refused and
        nothing accepted = not handed over (local only); its acts are the local approvals."""
        w = World(self)
        rec, ev = start_run(w)
        handover = hand_off(w, rec, "PAY-1", "2026-11-02T03:00:00Z")
        key = "%s#901" % handover                         # what handoff.py writes: "<head>#<label event>"
        w.ledger.claim_handoff("grp/pay", 5, key)
        w.ledger.set_handoff("grp/pay", 5, key, "checking", "rejected",
                             reason="label 'ai-handoff' was added by bob, not by the branch owner alice")
        w.close_ledger()
        ev.append(("2026-11-02T05:00:00.000Z", "APPROVED", INTENT, {"kind": "RELEASE", "sha256": "ef" * 32,
                                                                   "words": "approve RELEASE efefefef"}))
        w.commit({"src/refund.py": "x = 1\n"}, "PAY-1: finish refunds", "2026-11-02T04:00:00Z")
        w.commit({"aidlc-docs/audit/local-PAY-1.jsonl": audit(ev, ALICE)}, "PAY-1: release", "2026-11-02T05:00:00Z")
        w.merge({"branch": BR, "intent": INTENT, "key": "PAY-1"}, "merge", date="2026-11-03T00:00:00Z")
        t, d = judge(w, ledger_path=w.ledger_path)
        ho = t["outcome"]["handed_over"]
        self.assertEqual((ho["value"], ho["bridge_accepted"], ho["requested"], ho["rule"]), (False, False, True, "H2"))
        self.assertEqual((d["autonomy"]["runs"][0]["status"], d["autonomy"]["value"], d["judgment"]["value"],
                          d["delivery"]["by_run"]), ("local only", [0, 0], [2, 2], {"PAY-1": "correct"}),
                         [(o["kind"], o["at"]) for o in d["judgment"]["outside"]])
        self.assertIn("bridge rejected", [h["kind"] for h in t["actions"]["handovers"]])

    def test_h1_the_first_accepted_handover_and_a_later_one_is_a_push(self):
        """H1: the bridge accepted the first request (ledger 'accepted', its run); the runner worked; the owner ran
        `brain handoff` again later (a re-run). The hand-over is the accepted one; the second `brain handoff`
        commit is a person's push after it."""
        w = World(self)
        rec, _ = start_run(w)
        first = hand_off(w, rec, "PAY-1", "2026-11-02T03:00:00Z")
        w.ledger.claim_handoff("grp/pay", 5, first + "#1")
        w.ledger.set_handoff("grp/pay", 5, first + "#1", "checking", "accepted", run_id="RUN-PAY-1-1",
                             voucher_id=vid("h1"))
        w.ledger.add_run("RUN-PAY-1-1", "grp/pay", "PAY-1", 5, first, vid("h1"), owner="alice")
        w.close_ledger()
        w.commit({"src/a.py": "a\n"}, "PAY-1: step 1", "2026-11-02T04:00:00Z", BOT)
        again = hand_off(w, rec, "PAY-1", "2026-11-02T05:00:00Z")
        t, _ = judge(w, ledger_path=w.ledger_path)
        ho = t["outcome"]["handed_over"]
        self.assertEqual((ho["value"], ho["commit"], ho["rule"], ho["bridge_accepted"]), (True, first, "H1", True))
        self.assertEqual([h["sha"] for h in pushes(t)], [again])

    def test_h1_accepted_at_a_later_head_takes_the_request_before_it(self):
        """H1: the bridge's sha is the MR head it checked; when that is a commit after the `brain handoff` commit,
        the hand-over is the last request at or before it (here: the only one)."""
        w, r = base_run(self)
        w.ledger.add_run("RUN-PAY-1-1", "grp/pay", "PAY-1", 5, r["tip"], vid("h1"), owner="alice")
        w.close_ledger()
        handover = w.git("log", "--format=%H", "--grep=PAY-1: hand off", r["branch"]).stdout.split()[0]
        t, _ = judge(w, ledger_path=w.ledger_path)
        self.assertEqual((t["outcome"]["handed_over"]["commit"], t["outcome"]["handed_over"]["rule"]),
                         (handover, "H1"))

    def test_h3_no_record_and_no_runner_commit_is_not_a_handover(self):
        """H3: the ledger (given) has no row for the run's `brain handoff` commit, there is no hand-over voucher,
        and the runner made no commit after it (exit 1: never labelled): not handed over."""
        w = World(self)
        rec, _ = start_run(w)
        hand_off(w, rec, "PAY-1", "2026-11-02T03:00:00Z")
        w.commit({"src/refund.py": "x = 1\n"}, "PAY-1: finish refunds", "2026-11-02T04:00:00Z")
        w.ledger.add_run("RUN-PAY-9-1", "grp/pay", "PAY-9", 7, w.rev("main"), vid("other"), owner="bob")
        w.close_ledger()
        t, _ = judge(w, ledger_path=w.ledger_path)
        ho = t["outcome"]["handed_over"]
        self.assertEqual((ho["value"], ho["rule"], ho["bridge_accepted"]), (False, "H3", False), ho)
        self.assertEqual(pushes(t), [])
        t, _ = judge(w)                                    # no ledger: git only (H4), as before
        self.assertEqual((t["outcome"]["handed_over"]["value"], t["outcome"]["handed_over"]["rule"]), (True, "H4"))
        self.assertEqual([h["what"] for h in pushes(t)], ["code"])


# ---------- #12: R3 only after the hand-over ----------

def run_with_local_revert(w):
    """`brain start`, the owner's local half (an experiment reverted, a fix), `brain handoff`, the runner's half."""
    rec, _ = start_run(w)
    w.commit({"src/refund.py": "x = 1\n"}, "PAY-1: add refund model", "2026-11-02T02:30:00Z")
    tried = w.commit({"src/cache.py": "c = 1\n"}, "PAY-1: try caching", "2026-11-02T02:40:00Z")
    w.git("revert", "--no-edit", tried, env=env_at("2026-11-02T02:45:00Z"))
    w.commit({"src/refund.py": "x = 2\n", "tests/test_refund.py": "t = 1\n"}, "PAY-1: fix failing test",
             "2026-11-02T02:50:00Z")
    hand_off(w, rec, "PAY-1", "2026-11-02T03:00:00Z")
    (_, _), ev, data = release()
    runner = [("2026-11-02T04:00:00.000Z", "SESSION_START", INTENT, {"session": "k1"}),
              ("2026-11-02T06:00:00.000Z", ev, INTENT, data)]
    w.commit({"src/refund.py": "x = 3\n", "aidlc-docs/audit/runner-PAY-1.jsonl": audit(runner[:1], BOT)},
             "PAY-1: finish step 3 (AC-2)", "2026-11-02T04:00:00Z", BOT)
    w.commit({"src/refund.py": "x = 4\n"}, "PAY-1: fix lint (CI round 1)", "2026-11-02T05:00:00Z", BOT)
    return w.commit({"aidlc-docs/%s/notes.md" % INTENT: "done\n", "aidlc-docs/audit/runner-PAY-1.jsonl":
                     audit(runner, BOT)}, "PAY-1: RELEASE approved", "2026-11-02T07:00:00Z", BOT)


class FastForwardBranchGone(unittest.TestCase):
    """#12: fast-forward merge, no copy of the branch here. The owner's local revert ('Revert "PAY-1: try
    caching"') and "PAY-1: fix failing test" before the hand-over ended the run's stretch right after S (R3): the
    run read as a squash merge at `brain start`'s commit, no commits listed, and the run's own later commits (the
    runner's "fix lint" too) as hotfixes. Rule R3 applies only after the hand-over: from S to the last `brain
    handoff` commit everything is the run's own."""

    def setUp(self):
        self.w = World(self)
        self.last = run_with_local_revert(self.w)

    def assert_hand_count(self):
        t, d = judge(self.w)
        m, rv = t["outcome"]["merged_at"], t["outcome"]["reverted_or_hotfixed_within_14d"]
        self.assertEqual((rv["value"], d["delivery"]["by_run"], d["autonomy"]["value"]),
                         (False, {"PAY-1": "correct"}, [1, 1]),
                         (m, [(e.get("sha", "")[:8], e["rule"]) for e in rv["evidence"]]))
        return t, m

    def test_12_control_owner_still_has_the_stale_branch(self):
        w = self.w
        handover = w.git("log", "--format=%H", "--grep=hand off", BR).stdout.split()[0]
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--ff-only", BR)
        w.git("branch", "-q", "-f", BR, handover)
        _, m = self.assert_hand_count()
        self.assertEqual(m["commit"], self.last)

    def test_12_r3_fast_forward_branch_gone(self):
        w = self.w
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--ff-only", BR)
        w.git("branch", "-q", "-D", BR)
        t, m = self.assert_hand_count()
        self.assertEqual((m["commit"], m["value"], len(t["actions"]["commits"])),
                         (self.last, "2026-11-02T07:00:00Z", 9), m)

    def test_12_r3_fast_forward_after_gitlabs_rebase_branch_gone(self):
        w = self.w
        w.git("switch", "-q", "main")
        w.commit({"other.txt": "o\n"}, "unrelated work on main", "2026-11-02T08:00:00Z", LEAD)
        base = w.git("merge-base", BR, "main").stdout.strip()
        todo = w.git("rev-list", "--reverse", "--no-merges", "%s..%s" % (base, BR)).stdout.split()
        w.git("switch", "-q", "-c", "rebased", "main")
        t0 = datetime.datetime(2026, 11, 3)
        for i, c in enumerate(todo):
            w.git("cherry-pick", "--allow-empty", c,
                  env={"GIT_COMMITTER_DATE": (t0 + datetime.timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "GIT_COMMITTER_EMAIL": ALICE})
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--ff-only", "rebased")
        w.git("branch", "-q", "-D", "rebased", BR)
        _, m = self.assert_hand_count()
        self.assertEqual(m["commit"], w.rev("main"))

    def test_r3_still_ends_the_stretch_after_the_handover(self):
        """R3: after the hand-over, a person's commit naming the key with a fix word still ends the run (branch gone,
        fast-forward): it is a hotfix, and the run ends at the runner's last commit before it."""
        w = self.w
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--ff-only", BR)
        w.git("branch", "-q", "-D", BR)
        fix = w.commit({"src/refund.py": "x = 9\n"}, "hotfix PAY-1 rounding", "2026-11-04T00:00:00Z", LEAD)
        t, _ = judge(w)
        rv = t["outcome"]["reverted_or_hotfixed_within_14d"]
        self.assertEqual((t["outcome"]["merged_at"]["commit"], rv["value"], [e["sha"] for e in rv["evidence"]]),
                         (self.last, True, [fix]))


# ---------- #13: a merge made on another branch (M1b) ----------

class Foxtrot(unittest.TestCase):
    def setUp(self):
        w = self.w = World(self)
        self.a = w.run("PAY-1", "refund", 2, runner_events=[release()])
        w.git("switch", "-q", "main")
        self.b = w.run("PAY-2", "fees", 3, runner_events=[release()])     # started before A merged
        w.git("switch", "-q", "main")

    def foxtrot(self):
        """B's runner brings B up to date by merging main in; B is fast-forwarded; both branches deleted."""
        w, b = self.w, self.b
        w.git("switch", "-q", b["branch"])
        w.git("merge", "-q", "--no-ff", "--no-edit", "main", "-m", "Merge branch 'main' into '%s'" % b["branch"],
              env=env_at("2026-11-25T00:00:00Z", BOT))
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--ff-only", b["branch"])
        for br in (self.a["branch"], b["branch"]):
            w.git("branch", "-q", "-D", br)

    def test_13_earlier_run_keeps_its_merge_and_its_hotfix(self):
        """#13: A fast-forwarded, hotfixed 3 days later; B merges main in and is fast-forwarded. M1's fallback (S is
        a merge whose second parent has run.json) took B's runner merge for A's MR merge: A "merged" 23 days late,
        and its real hotfix sat inside that merge's history. Rule M1b: a merge "into '<another branch>'" is not the
        run's merge; the run's merge is looked for on its second parent's first-parent line."""
        w = self.w
        w.git("merge", "-q", "--ff-only", self.a["branch"])
        w.commit({"src/refund.py": "x = 9\n"}, "hotfix PAY-1 rounding of refunds", "2026-11-05T00:00:00Z", LEAD)
        self.foxtrot()
        t, d = judge(w)
        m, rv = t["outcome"]["merged_at"], t["outcome"]["reverted_or_hotfixed_within_14d"]
        self.assertEqual((m["value"], rv["value"], d["delivery"]["by_run"]),
                         ("2026-11-02T07:00:00Z", True, {"PAY-1": "wrong"}), (m, rv["evidence"]))
        self.assertIn("ai/alice/PAY-2-fees", m["via"])

    def test_m1b_a_merge_commit_behind_the_foxtrot(self):
        """M1b: A merged with GitLab's merge commit, then B (main merged in) fast-forwarded: A's merge is its own
        merge commit (found on the second parent's line), at its time; B is judged at its own merge."""
        w = self.w
        merge = w.merge(self.a, "merge", date="2026-11-04T00:00:00Z")
        self.foxtrot()
        t, _ = judge(w)
        m = t["outcome"]["merged_at"]
        self.assertEqual((m["commit"], m["value"], m["how"]), (merge, "2026-11-04T00:00:00Z", "merge commit"), m)
        b, _ = judge(w, "PAY-2")
        self.assertEqual((b["outcome"]["merged_at"]["value"], b["outcome"]["merged_at"]["via"]),
                         ("2026-11-25T00:00:00Z", None))
        self.assertNotIn(self.a["tip"], [c["sha"] for c in b["actions"]["commits"]])


# ---------- #14: what counts as a hotfix (A1, A2) ----------

PROPOSALS = """
## Brain proposals

```brain-proposal
file: TECH
type: pitfall
rel: new
src: D-1
---
退款单号必须先查重再落库。
```

```brain-gap
title: 部分退款失败时的回滚没有自动测试
kind: test
refs: AC-2
---
部分退款失败后要回滚已扣的手续费，现在没有测试覆盖。
```
"""


class Hotfix(unittest.TestCase):
    def test_14_a2_the_reflect_brain_mr_is_not_a_hotfix(self):
        """#14: after the merge the owner runs `brain reflect --apply`; its Brain MR (brain/TECH.md only) names PAY-1
        and the gap title holds a fix word (回滚): it counted as a hotfix, delivery 'wrong'. Rule A2: a hotfix or
        revert counts only when it changes something outside brain/, runs/ and aidlc-docs/; the others are listed
        in `ignored`."""
        temp_home(self)
        w, r = base_run(self)
        with open(os.path.join(w.repo, r["docs"] + "decisions.md"), encoding="utf-8") as f:
            text = f.read()
        w.commit({r["docs"] + "decisions.md": text + PROPOSALS}, "PAY-1: brain proposals", "2026-11-02T07:30:00Z", BOT)
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        day = "2026-11-04T00:00:00Z"
        code, out, err = run_cli(w.repo, ["reflect", "--key", "PAY-1", "--ref", "main", "--apply"],
                                 env={"BRAIN_USER": "alice", "GIT_AUTHOR_DATE": day, "GIT_COMMITTER_DATE": day})
        self.assertEqual(code, 0, out + err)
        br = "brain/PAY-1-reflect-1"
        title = w.git("log", "-1", "--format=%s", br).stdout.strip()
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--no-ff", br, "-m", "Merge branch '%s' into 'main'" % br, "-m", title,
              "-m", "See merge request grp/pay!9", env=env_at("2026-11-05T00:00:00Z", LEAD))
        self.assertEqual(w.git("diff", "--name-only", "HEAD^1", "HEAD").stdout.split(), ["brain/TECH.md"])
        t, d = judge(w)
        rv = t["outcome"]["reverted_or_hotfixed_within_14d"]
        self.assertEqual((rv["value"], d["delivery"]["by_run"], d["autonomy"]["value"]),
                         (False, {"PAY-1": "correct"}, [1, 1]), rv)
        self.assertIn(w.rev(br), [x["sha"] for x in rv["ignored"]])           # listed, not counted

    def test_a2_a_records_only_fix_is_listed_not_counted(self):
        """A2: a person's "PAY-1: fix notes" after the merge that changes only runs/PAY-1/ is not a hotfix."""
        w, r = base_run(self)
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        notes = w.commit({"runs/PAY-1/notes.md": "typo\n"}, "PAY-1: fix notes typo", "2026-11-04T00:00:00Z", LEAD)
        rv = judge(w)[0]["outcome"]["reverted_or_hotfixed_within_14d"]
        self.assertEqual((rv["value"], [x["sha"] for x in rv["ignored"]]), (False, [notes]), rv)

    def test_a1_a_commit_at_the_merge_second_is_after_it(self):
        """A1 (minor): "after the merge" is the graph (on main, not in the merge's history); the time only bounds
        the 14 days. A hotfix landed in the very second of the merge was skipped (`<=`)."""
        w, r = base_run(self)
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        fix = w.commit({"src/refund.py": "x = 9\n"}, "hotfix PAY-1", "2026-11-03T00:00:00Z", LEAD)
        rv = judge(w)[0]["outcome"]["reverted_or_hotfixed_within_14d"]
        self.assertEqual((rv["value"], [e["sha"] for e in rv["evidence"]]), (True, [fix]), rv)


# ---------- #15: Eval sees the whole batch (E1-E5) ----------

DESIGN = ((5, 0), "APPROVED", {"kind": "DESIGN", "sha256": "8c" * 32,
                               "words": "approve DESIGN 8cf2cbce (via gitlab P-ABCD %s)" % vid("d2"),
                               "claimed_channel": "gitlab P-ABCD %s" % vid("d2")})
PARK = {"schema": 1, "status": "waiting", "kind": "acceptance", "ask": "product", "question": "请验收",
        "options": [], "lean_request": "RELEASE efefefef"}


def decision(w, run, did, state, sha, created="2026-11-06T05:00:00Z"):
    w.ledger.db.execute(
        "INSERT INTO decisions(id,run_id,project,iid,sha,park_hash,kind,lean_request,state,answer,answerer,"
        "voucher_id,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, run, "grp/pay", 1, sha, "h-" + did, "clarify", None, state, None, None, None, created, created))


class Batch(unittest.TestCase):
    """#15: `brain eval` on main saw only the merged runs: a run still open (parked for weeks, being worked on, its
    MR closed) was not in runs, autonomy, the review queue or skipped, and its human acts were not in the judgment
    ratio, even with --ledger. A batch where 1 of 2 runs got stuck read "1 run, autonomy 1/1, nothing to review"."""

    @classmethod
    def setUpClass(cls):
        from tests.test_qa_trace_eval import _Keep
        w = cls.w0 = World(_Keep())
        r1 = w.run("PAY-1", "refund", 2, runner_events=[release()])
        w.merge(r1, "merge", date="2026-11-03T00:00:00Z")
        r2 = w.run("PAY-2", "export", 4, runner_events=[DESIGN], park=PARK)
        w.git("update-ref", "refs/remotes/origin/" + r2["branch"], r2["tip"])   # the runner pushed it
        L = w.ledger
        L.add_run("RUN-PAY-2-1", "grp/pay", "PAY-2", 6, r2["tip"], vid("h2"), owner="alice")
        L.set_run("RUN-PAY-2-1", "spooled", "dispatched")
        for key, slug, day, state in (("PAY-3", "fail", 5, "failed"), ("PAY-4", "lost", 6, "send_unknown"),
                                      ("PAY-5", "gone", 7, "closed")):
            w.git("switch", "-q", "main")
            r = w.run(key, slug, day)
            run = "RUN-%s-1" % key
            L.add_run(run, "grp/pay", key, day, r["tip"], vid(key), owner="alice")
            L.set_run(run, "spooled", state)
            if key == "PAY-4":
                decision(w, run, "D-41", "void", r["tip"])
                decision(w, run, "D-42", "invalid", r["tip"])
        L.add_run("RUN-PAY-6-1", "grp/pay", "PAY-6", 9, w.rev("main"), vid("h6"), owner="bob")   # no branch here
        L.set_run("RUN-PAY-6-1", "spooled", "dispatched")
        L.db.execute("UPDATE runs SET created=?, updated=?", ("2026-11-08T06:00:00Z", "2026-11-08T06:00:00Z"))
        w.close_ledger()
        w.git("switch", "-q", "main")                                              # judge from main

    def setUp(self):
        self.w = self.w0.copy(self)

    def collect(self, **kw):
        return EV.collect(self.w.repo, self.w.cfg(), as_of=AS_OF, ledger_path=self.w.ledger_path, **kw)

    def test_15_e1_the_stuck_run_is_in_the_eval(self):
        """E1: the runs are the tree's, the unmerged ones on run branches here (read from the branch), and the
        ledger's. PAY-2's REQUIREMENTS and DESIGN approvals count in the judgment ratio."""
        d = self.collect()
        self.assertEqual((d["runs"], d["judgment"]["value"]), (5, [7, 7]), d["judgment"]["by_kind"])
        self.assertEqual(sorted(d["open_branches"]), ["PAY-2", "PAY-3", "PAY-4", "PAY-5"])
        self.assertEqual(d["open_branches"]["PAY-2"], "refs/remotes/origin/ai/alice/PAY-2-export")
        self.assertEqual(([s["key"] for s in d["skipped"]], d["ledger_only"]), (["PAY-6"], ["PAY-6"]))

    def test_e2_e3_states_and_review_reasons(self):
        """E2 / E3: each unmerged run's state, and the reasons that put it in the review queue: stuck (no progress
        for --stuck-hours, with what it waits on), the runner failed, a *_unknown state, a bridge question void /
        invalid, the MR closed, a ledger run whose run.json is nowhere here."""
        d = self.collect()
        self.assertEqual({k: v["value"] for k, v in d["states"].items()},
                         {"PAY-1": "merged", "PAY-2": "waiting", "PAY-3": "failed", "PAY-4": "unknown",
                          "PAY-5": "closed"})
        rules = {q["key"]: sorted(r["rule"] for r in q["reasons"]) for q in d["review_queue"]}
        self.assertEqual(rules, {"PAY-2": ["stuck"], "PAY-3": ["runner_failed"],
                                 "PAY-4": ["question_problem", "state_unknown", "stuck"],
                                 "PAY-5": ["closed_unmerged"], "PAY-6": ["not_readable"]})
        stuck = next(r for q in d["review_queue"] if q["key"] == "PAY-2" for r in q["reasons"])
        self.assertIn("acceptance RELEASE efefefef", stuck["text"])
        d = self.collect(stuck_hours=10 ** 6)
        self.assertNotIn("PAY-2", [q["key"] for q in d["review_queue"]])

    def test_e4_funnel_keeps_dropped_questions_apart(self):
        """E4 (minor): the funnel counted stale, void, invalid and closed bridge questions as 还没回答; they are
        counted apart (no answer is expected any more)."""
        d = self.collect()
        f = d["funnel"]
        self.assertEqual((f["unanswered_questions"], f["dropped_questions"]),
                         (0, {"stale": 0, "void": 1, "invalid": 1, "closed": 0}))
        self.assertIn("不再等回答的问题", EV.report(d))

    def test_e5_batch_summary_table(self):
        """E5: one row per requirement: state, autonomous, human acts at / outside judgment points, questions asked,
        credits, delivered correctly or still open; in the JSON (`batch`) and at the top of the report."""
        d = self.collect()
        rows = {r["key"]: r for r in d["batch"]}
        self.assertEqual(sorted(rows), ["PAY-1", "PAY-2", "PAY-3", "PAY-4", "PAY-5", "PAY-6"])
        self.assertEqual({k: (r["state"], r["autonomous"], r["judgment"], r["questions"], r["delivery"])
                          for k, r in rows.items() if k in ("PAY-1", "PAY-2", "PAY-6")},
                         {"PAY-1": ("merged", True, [2, 0], 0, "correct"),
                          "PAY-2": ("waiting", True, [2, 0], 1, "open"),
                          "PAY-6": ("not_readable", None, None, None, "open")})
        self.assertTrue(rows["PAY-2"]["stuck"])
        text = EV.report(d)
        lines = text.splitlines()
        top = lines.index(next(x for x in lines if x.startswith("这一批")))
        self.assertLess(top, lines.index(next(x for x in lines if x.startswith("自主度"))))
        self.assertIn("| PAY-2 | 在等回答，", text)
        self.assertIn("| PAY-5 | MR 关了（没合并） |", text)

    def test_e1_brain_trace_reads_an_unmerged_run_from_its_branch(self):
        """E1: `brain trace PAY-2` on main (run.json only on the run branch) reads the branch `brain eval` reads."""
        code, out, err = run_cli(self.w.repo, ["trace", "PAY-2", "--json", "--as-of", AS_OF])
        self.assertEqual(code, 0, err)
        t = json.loads(out)
        self.assertEqual((t["ref"], t["outcome"]["waiting_on"]["kind"]),
                         ("refs/remotes/origin/ai/alice/PAY-2-export", "acceptance"))

    def test_e5_cli_json_and_stuck_hours(self):
        """E5: `brain eval --json --stuck-hours N` (the option reaches collect)."""
        code, out, err = run_cli(self.w.repo, ["eval", "--json", "--as-of", AS_OF, "--ledger", self.w.ledger_path,
                                               "--stuck-hours", "1000000"])
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        self.assertEqual((d["stuck_hours"], [r["key"] for r in d["batch"] if r["stuck"]]), (1000000.0, []))


# ---------- the rules are written down ----------

class Docs(unittest.TestCase):
    def test_every_new_rule_is_in_docs_eval_md(self):
        with open(os.path.join(ROOT, "docs", "eval.md"), encoding="utf-8") as f:
            text = f.read()
        for rule in ("H1", "H2", "H3", "H4", "M1b", "A1", "A2", "E1", "E2", "E3", "E4", "E5"):
            self.assertRegex(text, r"\*\*%s[* ]" % rule)
        self.assertIn("--stuck-hours", text)


if __name__ == "__main__":
    unittest.main()
