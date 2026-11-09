"""The public interfaces a KiroCrew app needs from brain-kit (BRAINKIT_REQUESTS must-haves 1-5): pluggable signing,
one answer verifier (see test_bindings.OneAnswerVerifier), a bridge config without a key, a ledger threads can share,
and the bridge's GitLab account in kit.json."""
import json
import os
import threading
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import config, sign  # noqa: E402
from brainkit.bridge.handoff import Bridge, load_bridge_config  # noqa: E402
from brainkit.bridge.kirocrew import KiroCrew  # noqa: E402
from brainkit.bridge.ledger import Ledger  # noqa: E402
from brainkit.common import BrainError, load_json, write_json  # noqa: E402
from brainkit.rest import GitLab, Jira  # noqa: E402
from tests import test_bridge as tb  # noqa: E402
from tests.helpers import keypair, tmpdir  # noqa: E402

HANDOFF_FIELDS = {"schema", "kind", "run_id", "project", "mr_iid", "sha", "branch", "owner", "key", "intent",
                  "brain_commit", "materials", "route", "issued_at", "expires_at", "issuer", "voucher_id"}
# question_note (added after c9e59d1): the id of the question note the answer is bound to (check_answer counts only it)
ANSWER_FIELDS = {"schema", "kind", "decision", "run_id", "project", "mr_iid", "sha", "park_hash", "decision_kind",
                 "choice", "answerer", "issued_at", "issuer", "voucher_id", "question_note"}


def fixture(test):
    t = tb.BridgeTest("test_kit_config_comes_from_main")
    t.setUp()
    test.addCleanup(t.doCleanups)
    return t


def bridge_with(t, signer):
    return Bridge(t.cfg, GitLab("https://gl.example", "t", t.fake), Jira("https://jira.example", "t", "2", t.fake),
                  KiroCrew("http://127.0.0.1:5476", "brain-bridge", "app-secret", t.fake), out=lambda *_: None,
                  signer=signer)


class Recorder:
    """Signs with the test key and keeps what it was asked to sign; can refuse or be away for a while."""

    def __init__(self, key, refuse=None, away=0):
        self.key, self.refuse, self.away, self.seen = key, refuse, away, []

    def sign(self, voucher):
        self.seen.append(dict(voucher))
        if self.refuse and self.refuse(voucher):
            raise sign.SignerRefused(["the signer's own check failed for %s\n/merge" % voucher["kind"]])
        if self.away > 0:
            self.away -= 1
            raise sign.SignerUnavailable("the signer is not there")
        return sign.KeySigner(self.key).sign(voucher)


def serve(spool, key, stop, refuse=lambda v: False):
    """A stand-in signing service on the other side of the spool (it holds the key, the bridge does not)."""
    req = os.path.join(spool, "requests")
    while not stop.is_set():
        for name in sorted(os.listdir(req)) if os.path.isdir(req) else []:
            done = [os.path.join(spool, d, name) for d in ("signed", "refused")]
            if not name.endswith(".json") or any(os.path.exists(p) for p in done):
                continue
            try:
                r = load_json(os.path.join(req, name))
            except (OSError, ValueError):
                continue
            if refuse(r["voucher"]):
                write_json(done[1], {"req_id": r["req_id"], "reasons": ["not for this run"]})
            else:
                write_json(done[0], {"req_id": r["req_id"], "voucher": r["voucher"],
                                     "sig": sign.KeySigner(key).sign(r["voucher"])})
        stop.wait(0.02)


# ---------- 1: pluggable signing ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class PluggableSigning(unittest.TestCase):
    def setUp(self):
        self.t = fixture(self)

    def test_default_is_the_key_file(self):
        self.assertIsInstance(self.t.bridge.signer, sign.KeySigner)
        self.assertEqual(self.t.bridge.signer.key_path, self.t.key)

    def test_bridge_and_decision_loop_sign_through_the_signer(self):
        t = self.t
        rec = Recorder(t.key)
        t.bridge = bridge_with(t, rec)
        t.dispatched()
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A")
        self.assertEqual(t.bridge.collect(), 1)
        self.assertEqual([v["kind"] for v in rec.seen], ["handoff", "answer"])
        self.assertEqual(set(rec.seen[0]), HANDOFF_FIELDS)                 # the voucher format is unchanged
        self.assertEqual(set(rec.seen[1]), ANSWER_FIELDS)
        a = t.answers()[0]
        self.assertEqual(a, rec.seen[1])
        self.assertTrue(sign.verify(a, load_json(os.path.join(t.cfg["vouchers_dir"], "RUN-PAY-1-1",
                                                              a["voucher_id"] + ".json"))["sig"], t.allowed))

    def test_a_refused_contract_rejects_the_handover(self):
        t = self.t
        t.bridge = bridge_with(t, Recorder(t.key, refuse=lambda v: v["kind"] == "handoff"))
        t.rejected_with("签名服务没有签")
        self.assertTrue(any("the signer's own check failed for handoff" in n for n in t.notes()))
        self.assertFalse([l for n in t.notes() for l in n.split("\n") if l.startswith("/")])   # reasons stay inert
        self.assertFalse(os.path.exists(t.cfg["vouchers_dir"]))

    def test_an_absent_signer_leaves_the_handover_for_the_next_round(self):
        t = self.t
        t.bridge = bridge_with(t, Recorder(t.key, away=1))
        with self.assertRaises(sign.SignerUnavailable):
            t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5])
        self.assertEqual(t.notes(), [])                                   # nothing said, nothing signed
        self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "accepted")

    def test_answers_refused_or_delayed_by_the_signer(self):
        t = self.t
        rec = Recorder(t.key, away=0)
        t.bridge = bridge_with(t, rec)
        t.dispatched()
        t.park()
        t.bridge.watch()
        rec.away = 1                                                     # the signer is down for one round
        t.reply(5, "bob", "answer D-1 A")                                 # not allowed: told once, not every round
        t.reply(5, "alice", "answer D-1 A")
        self.assertEqual(t.bridge.collect(), 0)
        d = t.bridge.ledger.db.execute("SELECT * FROM decisions WHERE id='D-1'").fetchone()
        self.assertEqual((d["state"], d["answer"]), ("open", None))
        self.assertEqual(t.bridge.collect(), 1)
        self.assertEqual(sum("@bob 不在" in n["body"] for n in t.notes_db[5]), 1)
        # the next question's answer is refused: the decision says so and nothing reaches the run
        rec.refuse = lambda v: v["kind"] == "answer"
        t.park(question="第二个问题？")
        t.bridge.watch()
        t.reply(5, "alice", "answer D-2 B")
        sends = len(t.sends())
        self.assertEqual(t.bridge.collect(), 0)
        d2 = t.bridge.ledger.db.execute("SELECT * FROM decisions WHERE id='D-2'").fetchone()
        self.assertEqual(d2["state"], "refused")
        self.assertTrue(any(n["body"].startswith("D-2 的回答没有送回 run：签名服务不签") for n in t.notes_db[5]))
        self.assertEqual(len(t.sends()), sends)

    def test_a_spool_signer_instead_of_a_key(self):
        t = self.t
        spool = os.path.join(t.d, "signer-spool")
        del t.cfg["signing_key"]
        t.cfg["signer"] = {"type": "spool", "spool_dir": spool, "allowed_signers": t.allowed, "timeout": 20,
                           "poll": 0.02}
        stop = threading.Event()
        th = threading.Thread(target=serve, args=(spool, t.key, stop, lambda v: v.get("owner") == "mallory"))
        th.start()
        self.addCleanup(th.join)
        self.addCleanup(stop.set)
        t.bridge = t.new_bridge()
        self.assertIsInstance(t.bridge.signer, sign.SpoolSigner)
        self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "accepted")
        req = load_json(os.path.join(t.cfg["spool_dir"], "requests", "RUN-PAY-1-1.json"))
        self.assertTrue(sign.verify(req["voucher"], req["sig"], t.allowed))
        self.assertEqual(os.listdir(os.path.join(spool, "requests")), [])  # taken back once answered
        with self.assertRaises(sign.SignerRefused) as e:
            t.bridge.signer.sign({"kind": "handoff", "run_id": "RUN-PAY-9-1", "owner": "mallory"})
        self.assertEqual(e.exception.reasons, ["not for this run"])

    def test_spool_signer_checks_what_comes_back(self):
        d = tmpdir(self)
        key, allowed = keypair(d)
        other_key, _ = keypair(tmpdir(self))
        v = {"kind": "answer", "run_id": "RUN-PAY-1-1", "choice": "A"}
        s = sign.SpoolSigner(os.path.join(d, "spool"), allowed, timeout=5, poll=0.01)
        rid = s.req_id(v)
        signed = os.path.join(d, "spool", "signed", rid + ".json")
        for bad in ({"voucher": dict(v, choice="B"), "sig": sign.sign(dict(v, choice="B"), key)},   # another voucher
                    {"voucher": v, "sig": sign.sign(v, other_key)}):                                # another key
            write_json(signed, dict(bad, req_id=rid))
            with self.assertRaises(sign.SignerRefused):
                s.sign(v)
        os.unlink(signed)
        clock = iter(range(1000))
        s = sign.SpoolSigner(os.path.join(d, "spool"), allowed, timeout=3, clock=lambda: next(clock),
                             sleep=lambda _: None)
        with self.assertRaises(sign.SignerUnavailable):
            s.sign(v)
        self.assertEqual(os.listdir(os.path.join(d, "spool", "requests")), [])


# ---------- 3: a bridge config without a key ----------

class BridgeConfigWithoutAKey(unittest.TestCase):
    def write(self, **over):
        d = tmpdir(self)
        cfg = {"projects": ["grp/repo"], "identities": "/x/identities.json", "vouchers_dir": "/x/vouchers",
               "spool_dir": "/x/spool", "ledger": "/x/ledger.sqlite"}
        cfg.update(over)
        p = os.path.join(d, "bridge.json")
        write_json(p, cfg)
        return p

    def test_spool_signer_needs_no_signing_key(self):
        cfg = load_bridge_config(self.write(signer={"type": "spool", "spool_dir": "/x/signer",
                                                    "allowed_signers": "/x/allowed_signers"}))
        self.assertNotIn("signing_key", {k for k, v in cfg.items() if v})
        self.assertIsInstance(sign.signer_from_config(cfg), sign.SpoolSigner)

    def test_what_is_still_required(self):
        self.assertEqual(load_bridge_config(self.write(signing_key="/x/key"))["signing_key"], "/x/key")
        for over, why in (({}, "signing_key is required"),
                          ({"signer": {"type": "spool", "spool_dir": "/x/signer"}}, "allowed_signers is required"),
                          ({"signer": {"type": "spool", "spool_dir": "/x/spool", "allowed_signers": "/x/a"}},
                           "must not be the runner's spool_dir"),
                          ({"signer": {"type": "vault"}, "signing_key": "/x/key"}, "signer.type")):
            with self.assertRaises(BrainError) as e:
                load_bridge_config(self.write(**over))
            self.assertIn(why, str(e.exception))


# ---------- 4: one ledger, many threads ----------

class LedgerAcrossThreads(unittest.TestCase):
    def test_threads_share_a_ledger(self):
        led = Ledger(os.path.join(tmpdir(self), "ledger.sqlite"))
        errors = []

        def work(i):
            try:
                key = "PAY-%d" % i
                self.assertTrue(led.claim_handoff("grp/repo", i, "s#1"))
                run_id = led.next_run_id(key)
                led.add_run(run_id, "grp/repo", key, i, "a" * 40, "V-%d" % i, owner="alice")
                self.assertTrue(led.set_handoff("grp/repo", i, "s#1", "checking", "accepted", run_id=run_id))
                self.assertTrue(led.set_run(run_id, "spooled", "dispatched", slot=run_id.lower()))
                self.assertEqual(led.run(run_id)["state"], "dispatched")
                with led.lock:                                            # several statements as one step
                    n = led.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                    self.assertGreaterEqual(n, 1)
            except Exception as e:  # noqa: BLE001 - collected and asserted below
                errors.append(e)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(1, 17)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        self.assertEqual(led.count("dispatched"), 16)
        self.assertEqual(len(led.runs_in("dispatched")), 16)
        cur = led.db.execute("SELECT run_id FROM runs ORDER BY run_id")
        self.assertEqual(cur.fetchone()[0], "RUN-PAY-1-1")
        self.assertEqual(len(cur.fetchall()), 15)
        led.close()

    @unittest.skipUnless(sign.available(), "ssh-keygen not found")
    def test_a_bridge_can_be_used_from_another_thread(self):
        t = fixture(self)
        out = {}
        th = threading.Thread(target=lambda: out.update(r=t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]),
                                                        report=t.bridge.report()))
        th.start()
        th.join()
        self.assertEqual(out.get("r"), "accepted")
        self.assertEqual(out["report"]["runs"], {"spooled": 1})


# ---------- 5: the bridge's account in kit.json ----------

class BridgeUserInKitJson(unittest.TestCase):
    def test_kit_config(self):
        self.assertEqual(config.DEFAULTS["bridge_user"], "")
        ok = config.merged(config.DEFAULTS, {"bridge_user": "brain-bridge"})
        self.assertEqual(config.validate(ok), [])
        for bad, why in (("brain-bot", "must not be one of bot_users"), ("not a name!", "GitLab username")):
            probs = config.validate(config.merged(config.DEFAULTS, {"bridge_user": bad}))
            self.assertTrue(any(why in p for p in probs), probs)

    @unittest.skipUnless(sign.available(), "ssh-keygen not found")
    def test_read_from_the_default_branch_and_checked_against_the_token(self):
        t = fixture(self)
        kit = {"version": 1, "bot_users": ["brain-bot"], "tracker": "jira", "jira": {"base": "https://jira.example"},
               "bridge_user": "brain-bridge"}
        t.overrides[".brain/kit.json"] = json.dumps(kit).encode()
        t.fake.on("GET", r"/api/v4/user", lambda m, qy, b: (200, {"username": "brain-bridge"}))
        t.bridge = t.new_bridge()
        self.assertEqual(t.bridge.kit(tb.P)["bridge_user"], "brain-bridge")
        t.dispatched()
        t.park()
        self.assertEqual(t.bridge.watch(), 1)
        t.reply(5, "alice", "answer D-1 A")
        self.assertEqual(t.bridge.collect(), 1)


if __name__ == "__main__":
    unittest.main()
