"""QA round 4 (bridge, runnerd): the reviewer's findings on 92d5e90 (session d4), ported, and the fixes' own cases.
Each test failed on 91e2fee. Numbers are the reviewer's (qa_r4/, outside this repo); "minor" ones are from its list
of minors.

The rules, as in round 3: in an unattended batch a run never waits for ever with nobody told, and a run gets one
answer per question. Every dead end either recovers by itself or tells the run and the MR, once."""
import json
import os
import unittest

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import sign  # noqa: E402
from brainkit.bridge import decisions, delivery, handoff, runnerd  # noqa: E402
from brainkit.bridge.handoff import Bridge  # noqa: E402
from brainkit.bridge.kirocrew import KiroCrew  # noqa: E402
from brainkit.common import BrainError, load_json  # noqa: E402
from brainkit.rest import GitLab, Jira  # noqa: E402
from tests import test_bridge as tb  # noqa: E402
from tests import test_qa_r3_bridge as r3  # noqa: E402
from tests import test_runnerd as tr  # noqa: E402
from tests.helpers import ROOT, sh  # noqa: E402

P, GL, RUN, SLOT = tb.P, tb.GL, r3.RUN, "run-pay-1-1"
fixture, later, notes, to_run, state, run_state, second_run = (r3.fixture, r3.later, r3.notes, r3.to_run, r3.state,
                                                               r3.run_state, r3.second_run)


class Slots:
    """KiroCrew's GET /api/chat/slots/<slot>: every message that landed, "running" while the run's session works on
    a turn, an empty queue."""

    def __init__(self, t):
        self.t, self.running = t, set()
        t.fake.on("GET", r"/api/chat/slots/([^/]+)", self.read)

    def read(self, m, qy, b):
        slot = m.group(1)
        return 200, {"key": slot, "messages": list(self.t.slot_msgs.get(slot, [])), "running": slot in self.running,
                     "queue": []}


def kc_send(t, handler):
    """Swap the fake KiroCrew's POST /api/chat handler -> the one it replaces."""
    for i, (meth, rx, h) in enumerate(t.fake.routes):
        if meth == "POST" and rx.pattern == "^/api/chat$":
            t.fake.routes[i] = (meth, rx, handler)
            return h
    raise AssertionError("no POST /api/chat route in the fake")


def kept_but_timed_out(t):
    """KiroCrew under load: it keeps the message, but answers after the bridge's read timeout."""
    def send(m, qy, body):
        t.slot_msgs.setdefault(body["slot"], []).append(
            {"role": "user", "content": body["message"], "meta": dict(body.get("meta") or {})})
        raise BrainError("POST 127.0.0.1:5476 failed: The read operation timed out")
    return send


def landed(t, prefix, slot=SLOT):
    """Messages that reached the run's slot and start with `prefix`."""
    return [m["content"] for m in t.slot_msgs.get(slot, []) if m["content"].startswith(prefix)]


def rounds(t, n=1, runnerd_cfg=None):
    """n rounds of `brain-bridge loop` without poll (with runnerd first, given its config); an error of a step is
    logged by the loop and does not stop the round."""
    for _ in range(n):
        if runnerd_cfg is not None:
            runnerd.once(runnerd_cfg, out=lambda *_: None)
        for step in (t.bridge.dispatch, t.bridge.watch, t.bridge.collect, t.bridge.reconcile):
            try:
                step()
            except BrainError:
                pass


def handoff_again(t, requested_at="2026-11-08T03:00:00Z"):
    """The owner runs `brain handoff` again on the MR branch: a commit that writes run.json's hand-over, pushed, and
    the hand-over label again -> the bridge's verdict."""
    rec = t.run_json()
    rec["handoff"] = dict(rec["handoff"], requested_at=requested_at)
    t.mrs[5]["sha"] = t.push("runs/PAY-1/run.json", json.dumps(rec), msg="PAY-1: hand off to team runner")
    t.events[5].append(t.ev("alice", "add"))
    return t.bridge.process(P, t.bridge.kit(P), t.mrs[5])


def pipeline(t, pid, status, sha=None, iid=5):
    """The MR's head pipeline (GitLab's head_pipeline on the MR)."""
    t.mrs[iid]["head_pipeline"] = {"id": pid, "sha": sha or t.mrs[iid]["sha"], "status": status}


def ci_routes(t):
    jobs = [{"id": 901, "name": "unit-tests", "stage": "test", "status": "failed", "failure_reason": "script_failure"}]
    t.fake.on("GET", GL + r"/pipelines/(\d+)/jobs", lambda m, qy, b: (200, jobs))
    t.fake.on("GET", GL + r"/jobs/(\d+)/trace", lambda m, qy, b: (200, b"FAILED tests/test_refund.py::test_14_days\n"))


def ci_notices(t, pid=""):
    return [m for m in to_run(t) if m.startswith("CI 没过：MR 的流水线 #%s" % pid)]


def decision(t, did="D-1"):
    return t.bridge.ledger.db.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()


def asked(t):
    return [n.split(" ", 1)[0].strip("*") for n in notes(t, start="**D-")]


class Refuses:
    """A signing service that checks an answer and will not sign it."""
    timeout = 30

    def sign(self, voucher):
        raise sign.SignerRefused(["the answerer is not allowed by the signing policy"])


# ---------- 05: an answered question is asked again once the head moves ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class AnsweredQuestionIsNotAskedAgain(unittest.TestCase):
    """The head moves before park.json says answered (the run's next work commit, the owner's push, GitLab's Rebase):
    the same park.json is not asked as D-2, and the run gets one answer."""

    def setUp(self):
        t = self.t = fixture(self)
        t.dispatched()

    def answered(self):
        t = self.t
        t.park()
        self.assertEqual(t.bridge.watch(), 1)
        t.reply(5, "alice", "answer D-1 A")
        self.assertEqual(t.bridge.collect(), 1)                              # D-1 delivered: A

    def test_05_the_run_pushes_work_before_flipping_park_json(self):
        t = self.t
        self.answered()
        t.mrs[5]["sha"] = t.push("src/refund.py", "x = 2\n")                 # the run's next work commit
        t.bridge.watch()
        t.reply(5, "carol", "answer D-2 B")
        t.bridge.collect()
        answers = [m.split("\n")[0] for m in to_run(t) if m.startswith("决策 ")]
        self.assertEqual((asked(t), len(answers)), (["D-1"], 1))

    def test_05_the_owner_pushes_while_the_run_works_on_the_answer(self):
        t = self.t
        self.answered()
        t.mrs[5]["sha"] = t.push("docs/typo.md", "fixed\n", msg="owner: typo")
        t.bridge.watch()
        self.assertEqual(asked(t), ["D-1"])

    def test_05_the_run_is_told_once_and_the_mr_nothing(self):
        t = self.t
        self.answered()
        before = len(notes(t))
        for i in range(2):                                                   # two pushes, park.json still waiting
            t.mrs[5]["sha"] = t.push("src/refund.py", "x = %d\n" % (i + 2))
            for _ in range(2):
                t.bridge.watch()
                t.bridge.collect()
        told = [m for m in to_run(t) if m.startswith("问题没发出：")]
        self.assertEqual(len(told), 1, to_run(t))
        self.assertIn("D-1 已经答过了", told[0])
        self.assertIn(decision(t)["voucher_id"], told[0])
        self.assertEqual(notes(t)[before:], [])                              # nothing on the MR

    def test_05_once_the_run_moved_on_the_same_question_is_a_new_one(self):
        t = self.t
        self.answered()
        t.mrs[5]["sha"] = t.push("src/refund.py", "x = 2\n")                 # park.json still waiting: not asked
        self.assertEqual(t.bridge.watch(), 0)
        t.mrs[5]["sha"] = t.push("runs/PAY-1/park.json", json.dumps({"schema": 1, "status": "answered"}))
        self.assertEqual(t.bridge.watch(), 0)                                # the run went on with the answer
        t.park()                                                             # later: the very same question again
        self.assertEqual(t.bridge.watch(), 1)
        self.assertEqual(asked(t), ["D-1", "D-2"])

    def test_05_a_refused_answer_blocks_the_run_and_its_question_is_not_asked_again(self):
        """minor: a refused answer left the run waiting for ever."""
        t = self.t
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A")
        good, t.bridge.signer = t.bridge.signer, Refuses()
        t.bridge.collect()
        self.assertEqual(state(t), "refused")
        told = [m for m in to_run(t) if m.startswith("问题没发出：D-1")]
        self.assertEqual(len(told), 1)
        self.assertIn("签名服务不签", told[0])
        self.assertIn("问题没发出，等人处理", told[0])
        t.bridge.signer = good
        t.mrs[5]["sha"] = t.push("aidlc-docs/%s/aidlc-state.md" % tb.INTENT, tb.STATE_OK + "Status: blocked\n")
        for _ in range(2):
            t.bridge.watch()
            t.bridge.collect()
        self.assertEqual((asked(t), len([m for m in to_run(t) if m.startswith("问题没发出：")])), (["D-1"], 1))


# ---------- 06: a CI failure after the acceptance reopens the code ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class AcceptanceIsAskedOnAGreenPipeline(unittest.TestCase):
    """The product accepts only a commit whose own pipeline passed. Lean 0.3 closes the intent once RELEASE is
    approved (fixes go to a new intent), so a CI fix after the acceptance would merge code nobody accepted. The
    question waits for CI instead, and a failure after the acceptance goes to the MR, not to the run."""
    ITEMS = [{"id": "A-1", "text": "旧订单不显示退款按钮"}]

    def setUp(self):
        t = self.t = fixture(self)
        t.dispatched()
        ci_routes(t)

    def acceptance(self, h="8cf2cbce"):
        return self.t.park(kind="acceptance", options=[], lean_request="RELEASE " + h, question="请验收 AC-1..AC-3",
                           review_items=self.ITEMS)

    def test_06_not_asked_while_the_heads_pipeline_failed(self):
        t = self.t
        self.acceptance()
        pipeline(t, 77, "failed")
        for _ in range(3):
            self.assertEqual(t.bridge.watch(), 0)
        self.assertEqual(notes(t, start="**D-"), [])
        ci = ci_notices(t, 77)
        self.assertEqual(len(ci), 1, to_run(t))
        self.assertIn("lean request RELEASE", ci[0])                    # fix, release again, park again
        self.acceptance("9d3e0a17")                                     # the run does: a new head, its pipeline
        pipeline(t, 78, "running")
        self.assertEqual(t.bridge.watch(), 0)
        pipeline(t, 78, "success")
        self.assertEqual(t.bridge.watch(), 1)
        self.assertIn("RELEASE 9d3e0a17", notes(t, start="**D-1")[0])

    def test_06_a_running_pipeline_holds_the_question_and_a_long_one_is_told(self):
        t = self.t
        self.acceptance()
        pipeline(t, 78, "running")
        for _ in range(2):
            self.assertEqual(t.bridge.watch(), 0)
        self.assertEqual((notes(t, start="**D-"), ci_notices(t), [n for n in notes(t) if "流水线 #78" in n]),
                         ([], [], []))
        with later(decisions, minutes=61):
            for _ in range(2):
                self.assertEqual(t.bridge.watch(), 0)
        self.assertEqual(len([n for n in notes(t) if "在等 MR 的流水线 #78" in n]), 1)
        pipeline(t, 78, "success")
        self.assertEqual(t.bridge.watch(), 1)

    def test_06_a_canceled_pipeline_is_told_on_the_mr(self):
        t = self.t
        self.acceptance()
        pipeline(t, 79, "canceled")
        for _ in range(2):
            self.assertEqual(t.bridge.watch(), 0)
        self.assertEqual(len([n for n in notes(t) if "流水线 #79 现在是 canceled" in n]), 1)
        self.assertEqual((notes(t, start="**D-"), ci_notices(t)), ([], []))

    def test_06_a_failure_after_the_acceptance_goes_to_the_mr_not_the_run(self):
        t = self.t
        sha = self.acceptance()
        pipeline(t, 80, "success")
        self.assertEqual(t.bridge.watch(), 1)
        t.reply(5, "carol", "answer D-1 approve 可以上线")
        self.assertEqual(t.bridge.collect(), 1)                         # RELEASE approved and delivered
        pipeline(t, 81, "failed")                                       # the pipeline is run again: it fails
        for _ in range(2):
            t.bridge.watch()
        t.mrs[5]["sha"] = t.push("runs/PAY-1/park.json", json.dumps({"schema": 1, "status": "answered"}))
        pipeline(t, 82, "failed")                                       # the run's records commit fails too
        for _ in range(2):
            t.bridge.watch()
        self.assertEqual(ci_notices(t), [])
        told = [n for n in notes(t) if "验收通过" in n]
        self.assertEqual(len(told), 1)
        self.assertIn("#81", told[0])
        self.assertIn(sha[:12], told[0])

    def test_06_the_runner_prompt_stops_after_the_acceptance(self):
        with open(os.path.join(ROOT, "remote", "prompts", "brain-run.md"), encoding="utf-8") as f:
            text = " ".join(f.read().split())
        self.assertIn("only on a commit whose MR pipeline passed", text)
        self.assertIn("Change no code after it", text)
        self.assertIn("do not run Lean's `ci-pipeline` or Operations stages", text)


# ---------- 50: three failed sends stranded the run for good ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class ResendHasNoLastTry(unittest.TestCase):
    """After three sends KiroCrew answered with an error the bridge stopped for good: the run stayed send_unknown or
    delivery_unknown and a new hand-over was refused. Now the MR hears after three, the bridge goes on sending (each
    round, then every 5 minutes), and a hand-over KiroCrew never takes ends its run after send_give_up_minutes, so
    that the owner can hand over again."""

    def setUp(self):
        self.t = fixture(self)
        self.slots = Slots(self.t)                       # KiroCrew's dashboard answers slot reads all along

    def told(self):
        return [n for n in notes(self.t) if "：桥把消息" in n]

    def test_50_a_hand_over_gets_there_once_kirocrew_takes_messages_again(self):
        t = self.t
        t.accept_and_prepare()
        t.fail_send = True                               # 503 on POST /api/chat for four rounds
        rounds(t, 4)
        t.fail_send = False                              # KiroCrew is healthy again
        rounds(t, 3)
        self.assertEqual((run_state(t), len(landed(t, "接手合同"))), ("dispatched", 1))
        told = self.told()
        self.assertEqual(len(told), 1, notes(t))
        self.assertIn("桥会接着重送", told[0])

    def test_50_an_answer_gets_there_once_kirocrew_takes_messages_again(self):
        t = self.t
        t.dispatched()
        t.park()
        self.assertEqual(t.bridge.watch(), 1)
        t.reply(5, "alice", "answer D-1 A")
        t.fail_send = True
        for _ in range(4):
            t.bridge.collect()
        t.fail_send = False
        rounds(t, 3)
        t.reply(5, "alice", "answer D-1 A")              # alice answers again, as nothing seemed to happen
        rounds(t, 2)
        self.assertEqual((state(t), len(landed(t, "决策 D-1 已答"))), ("delivered", 1))
        self.assertEqual(len(notes(t, start="D-1 已收到")), 1)
        self.assertIn("回答不用重新给", self.told()[0])

    def test_50_a_hand_over_kirocrew_never_takes_ends_the_run_and_the_owner_hands_over_again(self):
        t = self.t
        t.accept_and_prepare()
        t.fail_send = True
        rounds(t, 4)
        self.assertEqual(run_state(t), "send_unknown")
        with later(delivery, handoff, decisions, minutes=61):
            rounds(t, 1)
        self.assertEqual(run_state(t), "failed")
        told = [n for n in notes(t) if n.startswith(RUN + " 没能开始")]
        self.assertEqual(len(told), 1, notes(t))
        self.assertIn("brain handoff", told[0])
        self.assertIn({"add_labels": "ai-handoff-rejected", "remove_labels": "ai-running"}, t.labels())
        t.fail_send = False
        sends = len(t.sends())
        self.assertEqual(handoff_again(t), "accepted")
        rounds(t, 1, runnerd_cfg=t.runner_cfg)
        self.assertEqual(run_state(t, "RUN-PAY-1-2"), "dispatched")
        self.assertEqual([s["slot"] for s in t.sends()[sends:]], ["run-pay-1-2"])   # the failed run's: never again

    def test_50_resends_slow_down_after_ten_and_each_has_its_own_idempotency_key(self):
        t = self.t
        t.dispatched()
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A")
        t.fail_send = True

        def tried():
            return [c for c in t.fake.called("POST", r"/api/chat") if c[3]["message"].startswith("决策 D-1 已答")]
        for _ in range(14):
            t.bridge.collect()
        self.assertEqual(len(tried()), 10)
        self.assertEqual(len({c[3]["meta"]["sendId"] for c in tried()}), 1)              # one message, one sendId
        self.assertEqual(len({c[4].get("Idempotency-Key") for c in tried()}), 10)         # each send its own key
        with later(delivery, minutes=6):
            t.bridge.collect()
        self.assertEqual(len(tried()), 11)
        self.assertEqual(len(self.told()), 1)


# ---------- 51: admission did not count sends that may have landed ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class AdmissionCountsPossiblyLandedSends(unittest.TestCase):
    def setUp(self):
        t = self.t = fixture(self)
        t.cfg["max_per_repo"] = 1
        t.bridge = t.new_bridge()

    def sessions_at_work(self):
        return sorted(s for s, msgs in self.t.slot_msgs.items() if any(m["content"].startswith("接手合同") for m in msgs))

    def test_51_a_hand_over_that_timed_out(self):
        t = self.t
        t.accept_and_prepare()                                               # PAY-1 ready
        second_run(t)                                                        # PAY-2 ready
        kc_send(t, kept_but_timed_out(t))
        t.bridge.dispatch()
        self.assertEqual(self.sessions_at_work(), ["run-pay-1-1"])
        self.assertEqual(run_state(t, "RUN-PAY-2-1"), "spooled")

    def test_51_an_answer_that_timed_out(self):
        t = self.t
        Slots(t)
        t.dispatched()                                                       # PAY-1 at work
        t.park()
        self.assertEqual(t.bridge.watch(), 1)
        t.reply(5, "alice", "answer D-1 A")
        real = kc_send(t, kept_but_timed_out(t))
        t.bridge.collect()                                                   # the answer landed
        self.assertEqual(state(t), "delivery_unknown")
        kc_send(t, real)
        second_run(t)                                                        # PAY-2 ready
        t.bridge.dispatch()
        self.assertEqual(self.sessions_at_work(), ["run-pay-1-1"])


# ---------- 52: an obsolete CI notice was sent again to a parked run ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class ObsoleteNoticeIsNotSentAgain(unittest.TestCase):
    def setUp(self):
        t = self.t = fixture(self)
        t.dispatched()
        self.slots = Slots(t)
        ci_routes(t)

    def test_52_a_ci_notice_for_an_old_commit_does_not_reach_the_parked_run(self):
        t, slots = self.t, self.slots
        slots.running.add(SLOT)                                     # the run's session is at work
        t.mrs[5]["sha"] = t.push("src/refund.py", "def refund(days):\n    return days < 14\n")
        pipeline(t, 82, "failed")                                   # 1. its pipeline fails
        t.bridge.watch()                                            # the head's park.json is read first
        t.fail_send = True                                          # 2. KiroCrew answers 500
        t.bridge.watch()
        t.bridge.collect()
        t.fail_send = False
        self.assertEqual(landed(t, "CI 没过"), [])
        t.park()                                                    # 3. the run asks a question at a new head
        pipeline(t, 83, "running")
        slots.running.discard(SLOT)                                 # and ends its turn
        self.assertEqual(t.bridge.watch(), 1)
        self.assertEqual(state(t), "open")
        t.bridge.collect()                                          # 4. the bridge's next look
        t.bridge.watch()
        self.assertEqual(landed(t, "CI 没过"), [])
        row = t.bridge.ledger.db.execute("SELECT state FROM outbox WHERE what='notice:ci:82'").fetchone()
        self.assertEqual(row["state"], "dropped")


# ---------- 53: an answer was sent again after the head moved: two answers for one question ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class AnswerAfterTheHeadMoved(unittest.TestCase):
    def setUp(self):
        t = self.t = fixture(self)
        t.dispatched()
        self.slots = Slots(t)
        ci_routes(t)

    def unknown(self):
        """D-1 asked at X (X's pipeline failed); alice answers A; KiroCrew 500, not kept: delivery_unknown."""
        t = self.t
        x = t.park()
        pipeline(t, 90, "failed")
        self.assertEqual(t.bridge.watch(), 1)
        t.reply(5, "alice", "answer D-1 A")
        t.fail_send = True
        t.bridge.collect()
        t.fail_send = False
        self.assertEqual(state(t), "delivery_unknown")
        return x

    def test_53_two_answers_for_one_question(self):
        t, slots = self.t, self.slots
        self.unknown()
        t.bridge.watch()                                                  # 3. X's CI failure
        slots.running.add(SLOT)                                           #    the run works
        t.bridge.collect()
        self.assertEqual(landed(t, "决策 D-1 已答"), [])
        y = t.mrs[5]["sha"] = t.push("src/refund.py", "def refund(days):\n    return days <= 14\n", msg="fix CI")
        pipeline(t, 91, "running", sha=y)
        slots.running.discard(SLOT)                                       # 4. pushed, turn ended
        self.assertEqual(t.bridge.watch(), 1)                             #    D-2: the same question at Y
        t.bridge.collect()
        t.reply(5, "carol", "answer D-2 B")                               # 5. carol answers the fresh question
        t.bridge.collect()
        got = [m.split("\n")[0] for m in landed(t, "决策 D-")]
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0].startswith("决策 D-2 已答"))
        self.assertEqual(state(t), "stale")
        self.assertEqual(len([n for n in notes(t, start="D-1 作废：") if "还没送到 run" in n]), 1)

    def test_53_no_ci_notice_while_the_answer_is_on_its_way(self):
        t, slots = self.t, self.slots
        self.unknown()
        slots.running.add(SLOT)                                           # cannot tell whether it landed
        for _ in range(3):
            t.bridge.watch()
            t.bridge.collect()
        self.assertEqual(landed(t, "CI 没过"), [])

    def test_53_an_answer_that_landed_before_the_push_is_delivered_and_not_asked_again(self):
        t, slots = self.t, self.slots
        t.park()
        self.assertEqual(t.bridge.watch(), 1)
        t.reply(5, "alice", "answer D-1 A")
        real = kc_send(t, kept_but_timed_out(t))
        slots.running.add(SLOT)                                           # it landed; the session works on it
        t.bridge.collect()
        self.assertEqual(state(t), "delivery_unknown")
        kc_send(t, real)
        t.mrs[5]["sha"] = t.push("src/refund.py", "x = 2\n")              # its next commit, park.json not flipped
        slots.running.discard(SLOT)
        self.assertEqual(t.bridge.watch(), 0)
        self.assertEqual((state(t), asked(t)), ("delivered", ["D-1"]))
        self.assertEqual((len(landed(t, "决策 D-1 已答")), len(notes(t, start="D-1 已收到"))), (1, 1))


# ---------- 54: a re-hand-over is blocked by an earlier run's worktree ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class RehandoverAfterAnEarlierWorktree(unittest.TestCase):
    def setUp(self):
        self.t = fixture(self)

    def runnerd_and_dispatch(self):
        runnerd.once(self.t.runner_cfg, out=lambda *_: None)
        self.t.bridge.dispatch()

    def failed(self, run_id):
        p = os.path.join(self.t.cfg["spool_dir"], "failed", run_id + ".json")
        return load_json(p)["error"].replace("\n", " ")[-300:] if os.path.exists(p) else ""

    def workspace(self, run_id=RUN):
        return os.path.join(self.t.runner_cfg["runs_root"], run_id)

    def test_54a_after_a_failed_run(self):
        t = self.t
        t.push(".kiro/aidlc-lean/aidlc_lean.py", "# AI-DLC Lean (stub)\n", msg="Lean")          # 1.
        self.assertEqual(handoff_again(t, requested_at="2026-11-08T02:00:00Z"), "accepted")
        self.runnerd_and_dispatch()                                                          # 2.
        self.assertEqual(run_state(t), "failed")
        self.assertIn("lean-hook.py", self.failed(RUN))
        self.assertTrue(os.path.isdir(self.workspace()), "precondition: runnerd made the worktree")
        t.push(".kiro/brain/lean-hook.py", "# the KiroCrew hook adapter\n", msg="adapter")    # 3.
        self.assertEqual(handoff_again(t, requested_at="2026-11-08T04:00:00Z"), "accepted")
        self.runnerd_and_dispatch()
        self.assertEqual(run_state(t, "RUN-PAY-1-2"), "dispatched", self.failed("RUN-PAY-1-2"))
        self.assertFalse(os.path.exists(self.workspace()))                  # the failed run's worktree is gone

    def test_54b_after_runnerd_was_restarted_during_worktree_add(self):
        t = self.t
        self.assertEqual(t.bridge.process(P, t.bridge.kit(P), t.mrs[5]), "accepted")
        cache = runnerd._cache(t.runner_cfg, P)                  # runnerd had fetched the project, then was stopped
        sh("git", "-C", cache, "worktree", "add", "-q", "--no-checkout", "-B", tb.BRANCH, self.workspace(),
           t.mrs[5]["sha"])                                      # while git checked the files out
        self.runnerd_and_dispatch()                              # the restarted runnerd
        self.assertEqual(run_state(t), "dispatched", self.failed(RUN))
        self.assertTrue(os.path.exists(os.path.join(self.workspace(), "runs", "PAY-1", "run.json")))


@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class RunnerdFreesTheBranchOnlyFromAnEarlierRun(unittest.TestCase):
    def setUp(self):
        r = self.r = tr.Runnerd("test_prepares_worktree_at_contract_sha")
        r.setUp()
        self.addCleanup(r.doCleanups)

    def error(self, run_id):
        return load_json(os.path.join(self.r.cfg["spool_dir"], "failed", run_id + ".json"))["error"]

    def head_branch(self, run_id):
        path = os.path.join(self.r.cfg["runs_root"], run_id)
        return sh("git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def test_54_a_later_runs_worktree_is_never_removed(self):
        r = self.r
        r.request("RUN-PAY-1-2")                                # the requirement's newer run: prepared, at work
        self.assertEqual(runnerd.once(r.cfg, out=lambda *_: None), 1)
        r.request("RUN-PAY-1-1")                                # an older request runnerd still tries
        self.assertEqual(runnerd.once(r.cfg, out=lambda *_: None), 1)
        self.assertIn("not an earlier run of PAY-1", self.error("RUN-PAY-1-1"))
        self.assertEqual(self.head_branch("RUN-PAY-1-2"), tr.BRANCH)

    def test_54_an_earlier_runs_worktree_with_read_only_materials_is_removed(self):
        r = self.r
        r.request("RUN-PAY-1-1")
        self.assertEqual(runnerd.once(r.cfg, out=lambda *_: None), 1)
        old = os.path.join(r.cfg["runs_root"], "RUN-PAY-1-1")
        from brainkit.cli import MATERIALS_DIR, _readonly_copy
        src = os.path.join(r.cfg["runs_root"], "pkg-src")
        os.makedirs(src)
        with open(os.path.join(src, "toc.md"), "w", encoding="utf-8") as f:
            f.write("# toc\n")
        _readonly_copy(src, os.path.join(old, MATERIALS_DIR, "PKG-1"))   # as copy_materials leaves it
        with open(os.path.join(old, "src", "a.py"), "w", encoding="utf-8") as f:
            f.write("the old run's unfinished change\n")
        r.request("RUN-PAY-1-2")
        logged = []
        self.assertEqual(runnerd.once(r.cfg, out=logged.append), 1)
        self.assertTrue(os.path.exists(os.path.join(r.cfg["spool_dir"], "ready", "RUN-PAY-1-2.json")), logged)
        self.assertFalse(os.path.exists(old))
        self.assertEqual(self.head_branch("RUN-PAY-1-2"), tr.BRANCH)
        self.assertTrue(any("removed the worktree of RUN-PAY-1-1" in x for x in logged), logged)


# ---------- 55: a hand-over runnerd never picks up is never told ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class RunnerdSilenceIsTold(unittest.TestCase):
    def test_55_told_once_and_the_run_still_starts(self):
        t = fixture(self)
        self.assertEqual(t.bridge.process(P, t.bridge.kit(P), t.mrs[5]), "accepted")
        rounds(t, 2)                                             # runnerd is down: nothing in ready/ or failed/
        with later(handoff, delivery, decisions, minutes=180):  # three hours on
            rounds(t, 3)
        told = [n for n in notes(t) if n.startswith(RUN + " 还没开始") and "brain-runnerd" in n]
        self.assertEqual(len(told), 1, notes(t))
        self.assertEqual(run_state(t), "spooled")
        with later(handoff, delivery, decisions, minutes=200):  # runnerd is back
            rounds(t, 2, runnerd_cfg=t.runner_cfg)
        self.assertEqual(run_state(t), "dispatched")
        self.assertEqual(len([n for n in notes(t) if "还没开始" in n]), 1)


# ---------- minors ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class Minors(unittest.TestCase):
    def setUp(self):
        self.t = fixture(self)

    def test_minor_one_notice_that_fails_does_not_stop_the_others(self):
        t = self.t
        t.dispatched()
        Slots(t)
        now = "2026-11-09T00:00:00Z"
        sids = [delivery.notice_send_id(RUN, k) for k in ("invalid:D-7", "invalid:D-8")]
        for sid, key in zip(sids, ("invalid:D-7", "invalid:D-8")):        # two notices whose send was cut off
            t.bridge.ledger.db.execute("INSERT INTO outbox(send_id,run_id,slot,what,message,state,tries,created,"
                                       "updated) VALUES (?,?,?,?,?,?,?,?,?)",
                                       (sid, RUN, SLOT, "notice:" + key, "问题没发出：" + key, "interrupted", 1,
                                        delivery.iso(delivery.utcnow()), now))
        real = t.bridge._landed

        def broken(slot, send_id):
            if send_id == sids[0]:
                raise BrainError("slot read: unexpected reply")
            return real(slot, send_id)
        t.bridge._landed = broken
        with self.assertRaises(BrainError):
            t.bridge.collect()
        self.assertEqual(landed(t, "问题没发出：invalid:D-8"), ["问题没发出：invalid:D-8"])

    def test_minor_reconcile_stops_a_session_whose_hand_over_may_have_landed(self):
        t = self.t
        t.accept_and_prepare()
        kc_send(t, kept_but_timed_out(t))
        t.bridge.dispatch()
        self.assertEqual(run_state(t), "send_unknown")
        t.mr_state[5] = "closed"
        t.bridge.reconcile()
        self.assertEqual(run_state(t), "closed")
        self.assertEqual(len(t.fake.called("POST", r"/api/chat/slots/run-pay-1-1/stop")), 1)

    def test_minor_an_answer_of_a_merged_run_is_closed(self):
        t = self.t
        t.dispatched()
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A")
        t.fail_send = True
        t.bridge.collect()
        t.fail_send = False
        self.assertEqual(state(t), "delivery_unknown")
        t.mr_state[5] = "merged"
        t.bridge.reconcile()
        t.bridge.collect()
        self.assertEqual(state(t), "closed")

    def test_minor_a_question_left_asking_goes_stale_once_the_head_moves(self):
        t = self.t
        t.dispatched()
        t.park()

        def stopped(project, iid, text):
            raise SystemExit(143)
        t.bridge.gl.comment = stopped
        with self.assertRaises(SystemExit):
            t.bridge.watch()
        t.bridge.ledger.db.execute("UPDATE decisions SET state='asking'")       # as a SIGKILL would leave it
        t.bridge = t.new_bridge()
        t.mrs[5]["sha"] = t.push("src/refund.py", "x = 2\n")                    # the head moves first
        self.assertEqual(t.bridge.active_runs(P), 0)
        with later(decisions, minutes=10):
            t.bridge.collect()
        self.assertEqual(state(t), "stale")
        self.assertEqual(t.bridge.active_runs(P), 1)                            # the run counts as at work again
        self.assertEqual(notes(t, start="D-1 作废"), [])                         # it was never on the MR

    def test_minor_two_bridges_count_one_ci_notice_once(self):
        t = self.t
        t.dispatched()
        ci_routes(t)
        t.mrs[5]["sha"] = t.push("src/refund.py", "x = 1\n")
        pipeline(t, 77, "failed")
        t.bridge.watch()                                             # the question step looks at the head first
        other = Bridge(t.cfg, GitLab("https://gl.example", "t", t.fake), Jira("https://jira.example", "t", "2", t.fake),
                       KiroCrew("http://127.0.0.1:5476", "brain-bridge", "app-secret", t.fake), out=lambda *_: None)
        real = t.bridge.gl.pipeline_jobs

        def jobs(project, pid):
            other.watch()                                            # the other bridge process forwards it meanwhile
            return real(project, pid)
        t.bridge.gl.pipeline_jobs = jobs
        t.bridge.watch()
        self.assertEqual((t.bridge.ledger.count_of("ci:" + RUN), len(ci_notices(t, 77))), (1, 1))

    def test_minor_two_bridges_post_one_conflict_note(self):
        t = self.t
        t.dispatched()
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A")
        t.reply(5, "carol", "answer D-1 B")
        other = Bridge(t.cfg, GitLab("https://gl.example", "t", t.fake), Jira("https://jira.example", "t", "2", t.fake),
                       KiroCrew("http://127.0.0.1:5476", "brain-bridge", "app-secret", t.fake), out=lambda *_: None)
        real = t.bridge.gl.comment

        def comment(project, iid, text):
            if "收到不同的回答" in text:
                other.collect()                                      # the other process reads the same round
            return real(project, iid, text)
        t.bridge.gl.comment = comment
        t.bridge.collect()
        self.assertEqual(len(notes(t, start="D-1 收到不同的回答")), 1)
        t.reply(5, "carol", "answer D-1 A")                          # the next round still works
        self.assertEqual(t.bridge.collect(), 1)

    def test_minor_a_second_answer_while_the_first_is_on_its_way_is_told_so(self):
        t = self.t
        t.dispatched()
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A")
        t.fail_send = True                                           # KiroCrew 500; its slot cannot be read
        t.bridge.collect()
        t.fail_send = False
        t.park(question="退款要不要通知用户？")                         # the run goes on and asks another question
        self.assertEqual(t.bridge.watch(), 1)
        self.assertEqual(state(t), "delivery_unknown")
        t.reply(5, "alice", "answer D-1 B")
        t.bridge.collect()
        told = [n for n in notes(t) if n.startswith("@alice：D-1")]
        self.assertEqual(len(told), 1, notes(t))
        self.assertIn("还在把它送回 run", told[0])


if __name__ == "__main__":
    unittest.main()
