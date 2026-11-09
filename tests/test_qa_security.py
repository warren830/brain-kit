"""Security QA findings on c9e59d1 (S1-S13, K2, N2) and the unconfirmed items checked with them.

Each test failed on c9e59d1. Most are the reviewer's repros, ported; the rest pin the fix's own rules."""
import hashlib
import http.client
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import ci, cli, config, lean, rest, sign  # noqa: E402
from brainkit.bridge import decisions, handoff  # noqa: E402
from brainkit.bridge.handoff import Bridge, load_bridge_config  # noqa: E402
from brainkit.bridge.kirocrew import KiroCrew  # noqa: E402
from brainkit.common import BrainError, load_json, write_json  # noqa: E402
from brainkit.rest import GitLab, Jira, head_pushed_at  # noqa: E402
from tests import test_bridge as tb  # noqa: E402
from tests.helpers import LEAN_SRC, Fake, commit_file, git_repo, keypair, sh, tmpdir  # noqa: E402

LEAN_PY = os.path.join(LEAN_SRC, "kit", ".kiro", "aidlc-lean", "aidlc_lean.py")
BR = "ai/alice/PAY-1-refund"
INTENT = "261108-pay-1-refund"
DESIGN = "aidlc-docs/%s/inception/application-design/application-design.md" % INTENT


def env(test, **kv):
    """Set environment variables for one test."""
    for k, v in kv.items():
        old = os.environ.get(k)
        test.addCleanup(lambda k=k, old=old: os.environ.pop(k, None) if old is None else os.environ.__setitem__(k, old))
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def fixture(test):
    t = tb.BridgeTest("test_kit_config_comes_from_main")
    t.setUp()
    test.addCleanup(t.doCleanups)
    return t


def brain(repo, *args):
    out = io.StringIO()
    cwd = os.getcwd()
    os.chdir(repo)
    try:
        with redirect_stdout(out), redirect_stderr(out):
            rc = cli.main(list(args))
    finally:
        os.chdir(cwd)
    return rc, out.getvalue()


def run_repo(test, extra=()):
    """A run branch with run.json, a Lean intent and risk paths (for brain-ci)."""
    d = tmpdir(test)
    r = git_repo(os.path.join(d, "proj"))
    for rel, text in extra:
        commit_file(r, rel, text)
    commit_file(r, ".brain/kit.json", json.dumps({"version": 1}))
    commit_file(r, "gates/risk-paths", "src/pay/**\n")
    brain_commit = commit_file(r, "brain/TECH.md", "# TECH\n")
    sh("git", "-C", r, "switch", "-q", "-c", BR)
    rec = {"schema": 1, "key": "PAY-1", "owner": "alice", "branch": BR, "intent": INTENT,
           "brain_commit": brain_commit, "kit_version": "0.1.0", "origin": "local", "expected_paths": [],
           "created_at": "2026-11-08T00:00:00Z", "claim_checked": True}
    commit_file(r, "runs/PAY-1/run.json", json.dumps(rec))
    commit_file(r, "aidlc-docs/%s/aidlc-state.md" % INTENT, "# AI-DLC State\n")
    return d, r


# ---------- S1: `brain handoff` labels only the commit it pushed; the bridge takes only the hand-over commit ----------

class World:
    """A small GitLab: one MR over a bare repo; it records an MR version whenever it sees a new branch head."""

    def __init__(self, origin):
        self.origin, self.clock, self.versions, self.events, self.notes = origin, 0, [], [], []
        self.mr_iid, self.labels, self.concurrent_push, self.push_first = None, set(), None, False

    def tick(self):
        self.clock += 1
        return (datetime(2026, 11, 9, tzinfo=timezone.utc) + timedelta(seconds=self.clock)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")

    def head(self):
        p = subprocess.run(["git", "-C", self.origin, "rev-parse", "refs/heads/" + BR], capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else ""

    def record(self):
        h = self.head()
        if h and (not self.versions or self.versions[0]["head_commit_sha"] != h):
            self.versions.insert(0, {"id": len(self.versions) + 1, "head_commit_sha": h, "created_at": self.tick()})

    def mr(self):
        self.record()
        return {"iid": self.mr_iid, "source_branch": BR, "target_branch": "main", "sha": self.head(),
                "source_project_id": 7, "target_project_id": 7, "state": "opened", "labels": sorted(self.labels)}

    def fake(self):
        f = Fake()
        gl = "/api/v4/projects/grp/repo"

        def mrs(m, qy, b):
            if self.mr_iid is None:
                return 200, []
            mr = self.mr()
            if qy.get("labels") and qy["labels"] not in self.labels:
                return 200, []
            return 200, [mr]

        def create(m, qy, b):
            self.mr_iid = 5
            return 201, self.mr()

        def versions(m, qy, b):
            if self.concurrent_push and self.push_first:   # someone pushes before GitLab got round to the owner's push
                a, self.concurrent_push = self.concurrent_push, None
                a()
            self.record()
            if self.concurrent_push:                      # someone pushes right after GitLab recorded the owner's push
                a, self.concurrent_push = self.concurrent_push, None
                a()
                self.record()
            return 200, self.versions if qy.get("page", "1") == "1" else []

        def put(m, qy, b):
            for lab in (b.get("remove_labels") or "").split(","):
                if lab in self.labels:
                    self.labels.discard(lab)
                    self.events.append({"id": 900 + len(self.events), "action": "remove", "label": {"name": lab},
                                        "user": {"username": "alice"}, "created_at": self.tick()})
            for lab in (b.get("add_labels") or "").split(","):
                if lab and lab not in self.labels:          # GitLab: adding a label the MR has records nothing
                    self.labels.add(lab)
                    self.events.append({"id": 900 + len(self.events), "action": "add", "label": {"name": lab},
                                        "user": {"username": "alice"}, "created_at": self.tick()})
            return 200, {"iid": 5}

        def raw(m, qy, b):
            p = subprocess.run(["git", "-C", self.origin, "show", "%s:%s" % (qy.get("ref", ""), m.group(1))],
                               capture_output=True)
            return (200, p.stdout) if p.returncode == 0 else (404, {"message": "404"})

        def commit(m, qy, b):
            p = subprocess.run(["git", "-C", self.origin, "rev-list", "--parents", "-n", "1", m.group(1)],
                               capture_output=True, text=True)
            ids = p.stdout.split()
            return (200, {"id": ids[0], "parent_ids": ids[1:]}) if p.returncode == 0 and ids else (404, {})

        def note(m, qy, b):
            n = {"id": 2000 + len(self.notes), "body": b["body"], "author": {"username": "brain-bridge"},
                 "created_at": self.tick()}
            self.notes.append(n)
            return 201, n

        f.on("GET", r"/api/v4/user", lambda m, qy, b: (200, {"username": "alice"}))
        f.on("GET", gl, lambda m, qy, b: (200, {"default_branch": "main"}))
        f.on("GET", gl + "/merge_requests", mrs)
        f.on("POST", gl + "/merge_requests", create)
        f.on("GET", gl + r"/merge_requests/5", lambda m, qy, b: (200, self.mr()))
        f.on("PUT", gl + r"/merge_requests/5", put)
        f.on("GET", gl + r"/merge_requests/5/versions", versions)
        f.on("GET", gl + r"/merge_requests/5/resource_label_events",
             lambda m, qy, b: (200, self.events if qy.get("page", "1") == "1" else []))
        f.on("POST", gl + r"/merge_requests/5/notes", note)
        f.on("GET", gl + r"/repository/files/(.+)/raw", raw)
        f.on("GET", gl + r"/repository/commits/([0-9a-f]+)", commit)
        return f


@unittest.skipUnless(os.path.exists(os.path.join(LEAN_SRC, "install.sh")) and sign.available(),
                     "Lean checkout or ssh-keygen missing")
class HandoffLabelsWhatItPushed(unittest.TestCase):
    def setUp(self):
        d = self.d = tmpdir(self)
        env(self, HOME=d, GITLAB_TOKEN=None, BRAIN_GITLAB_TOKEN_FILE=os.path.join(d, "no-token"), BRAIN_USER="alice",
            BRAIN_MR_VERSION_WAIT="5")
        r = self.repo = git_repo(os.path.join(d, "proj"))
        commit_file(r, "src/app.py", "print('hi')\n", "init")
        rc, out = brain(r, "install", r, "--lean", LEAN_SRC, "--team", "pay", "--prefix", "PAY")
        self.assertEqual(rc, 0, out)
        kit = load_json(os.path.join(r, ".brain", "kit.json"))
        kit["gitlab"] = {"base": "https://gitlab.invalid", "project": "grp/repo"}
        with open(os.path.join(r, ".brain", "kit.json"), "w") as f:
            json.dump(kit, f)
        sh("git", "-C", r, "add", "-A")
        sh("git", "-C", r, "commit", "-q", "-m", "install kit")
        self.origin = os.path.join(d, "origin.git")
        sh("git", "init", "-q", "--bare", "-b", "main", self.origin)
        sh("git", "-C", r, "remote", "add", "origin", self.origin)
        sh("git", "-C", r, "push", "-q", "origin", "main")
        rc, out = brain(r, "start", "refund", "--key", "PAY-1")
        self.assertEqual(rc, 0, out)
        rec = load_json(os.path.join(r, "runs", "PAY-1", "run.json"))
        state = lean.state_path(r, rec["intent"])
        with open(state) as f:
            text = f.read()
        with open(state, "w") as f:
            f.write(text.replace(lean.TEMPLATE_STATUS, "Status: Construction app: step 1 of 3 done"))
        sh("git", "-C", r, "commit", "-q", "-am", "work")
        self.world = World(self.origin)
        self.fake = self.world.fake()
        cli_gitlab = cli._gitlab
        cli._gitlab = lambda cfg: GitLab("https://gitlab.invalid", "t", self.fake)
        self.addCleanup(setattr, cli, "_gitlab", cli_gitlab)
        self.foreign = None

    def push_by_another_person(self):
        other = os.path.join(self.d, "other")
        if not os.path.exists(other):
            sh("git", "clone", "-q", self.origin, other)
        sh("git", "-C", other, "fetch", "-q", "origin")
        sh("git", "-C", other, "checkout", "-q", "-B", BR, "origin/" + BR)
        self.foreign = commit_file(other, "src/pay/limits.py", "LIMIT_CHECK = False\n", "not the owner's commit",
                                   env={"GIT_AUTHOR_NAME": "bob", "GIT_COMMITTER_NAME": "bob"})
        sh("git", "-C", other, "push", "-q", "origin", BR)

    def bridge(self):
        key, _ = keypair(self.d)
        ids = os.path.join(self.d, "identities.json")
        with open(ids, "w") as f:
            json.dump({"users": {"alice": {}}, "roles": {}}, f)
        cfg = {"projects": ["grp/repo"], "identities": ids, "signing_key": key,
               "vouchers_dir": os.path.join(self.d, "v"), "spool_dir": os.path.join(self.d, "spool"),
               "ledger": os.path.join(self.d, "ledger.sqlite"), "max_inflight": 12, "max_per_repo": 3,
               "contract_ttl_hours": 72, "kirocrew": {"agent": "brain-run", "memory_mode": "incognito"}}
        return Bridge(cfg, GitLab("https://gitlab.invalid", "t", self.fake), None, None, out=lambda *_: None), cfg

    def handed_over(self):
        """What the bridge hands over now (None: nothing)."""
        if "ai-handoff" not in self.world.labels:
            return None
        br, cfg = self.bridge()
        if br.process("grp/repo", br.kit("grp/repo"), br.gl.mr("grp/repo", 5)) != "accepted":
            return None
        vdir = os.path.join(cfg["vouchers_dir"], "RUN-PAY-1-1")
        return load_json(os.path.join(vdir, os.listdir(vdir)[0]))["voucher"]["sha"]

    def check_foreign_push(self):
        rc, out = brain(self.repo, "handoff")
        pushed = sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(self.world.head(), pushed)
        self.assertNotIn("ai-handoff", self.world.labels, out)
        self.assertIn("NOT labelled", out)
        self.assertNotEqual(self.handed_over(), self.foreign)
        return out

    def test_a_push_right_after_the_owners_is_not_labelled(self):
        self.world.concurrent_push = self.push_by_another_person
        self.assertIn("someone pushed", self.check_foreign_push())

    def test_the_wait_timing_out_does_not_label(self):
        env(self, BRAIN_MR_VERSION_WAIT="1")
        self.world.concurrent_push, self.world.push_first = self.push_by_another_person, True
        self.check_foreign_push()

    def test_no_version_for_the_push_within_the_wait_does_not_label(self):
        env(self, BRAIN_MR_VERSION_WAIT="1")
        self.world.record = lambda: None                    # GitLab is slow to record the push
        rc, out = brain(self.repo, "handoff")
        self.assertEqual(rc, 0, out)
        self.assertIn("has not recorded", out)
        self.assertNotIn("ai-handoff", self.world.labels)

    def test_a_clean_handoff_is_labelled_and_accepted(self):
        rc, out = brain(self.repo, "handoff")
        self.assertEqual(rc, 0, out)
        self.assertIn("Handed off", out)
        self.assertEqual(self.handed_over(), sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip())

    def test_a_label_already_on_the_mr_is_taken_off_and_added_again(self):
        """GitLab records no event for adding a label the MR already has: the bridge would see the old event."""
        self.world.mr_iid = 5
        sh("git", "-C", self.repo, "push", "-q", "-u", "origin", BR)
        self.world.record()
        self.world.labels.add("ai-handoff")
        self.world.events.append({"id": 800, "action": "add", "label": {"name": "ai-handoff"},
                                  "user": {"username": "alice"}, "created_at": self.world.tick()})
        rc, out = brain(self.repo, "handoff")
        self.assertEqual(rc, 0, out)
        acts = [(e["action"], e["label"]["name"]) for e in self.world.events[1:]]
        self.assertEqual(acts, [("remove", "ai-handoff"), ("add", "ai-handoff")])
        self.assertEqual(self.handed_over(), sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip())


@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class BridgeTakesOnlyTheHandoffCommit(unittest.TestCase):
    def test_a_commit_on_top_of_the_handoff_commit(self):
        """The rest of the race: X lands on top of H after the CLI looked, GitLab records X before the label."""
        t = fixture(self)
        x = t.push("src/pay/limits.py", "LIMIT_CHECK = False\n", msg="someone else's commit")
        t.mrs[5]["sha"] = x                                  # recorded at tb.PUSHED, before alice's label
        t.rejected_with("not the hand-over commit")
        self.assertFalse(os.path.exists(t.cfg["vouchers_dir"]))
        probs, _ = t.bridge.verify(tb.P, t.bridge.kit(tb.P), t.mrs[5])           # an outside signer finds the same
        self.assertTrue(any("not the hand-over commit" in p for p in probs), probs)
        # alice looks at X, keeps it and runs `brain handoff` again: a new hand-over commit on top
        h2 = t.push("runs/PAY-1/run.json", json.dumps(t.run_json(handoff={
            "requested_at": "2026-11-09T02:00:00Z", "by": "alice", "local_head": x})))
        t.mrs[5]["sha"] = h2
        t.events[5].append(t.ev("alice", "add"))
        self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "accepted", t.notes())

    def test_parents_gitlab_does_not_show_are_refused(self):
        t = fixture(self)
        t.fake.routes.insert(0, ("GET", __import__("re").compile(r"^%s/repository/commits/%s$" % (tb.GL, t.sha)),
                                 lambda m, qy, b: (200, {"id": t.sha})))
        t.rejected_with("cannot tell")


# ---------- S2: an edited reply is not an answer ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class EditedReplyIsNotAnAnswer(unittest.TestCase):
    def test_a_design_approval_by_editing_a_builders_comment(self):
        t = fixture(self)
        t.dispatched()
        t.park(kind="design", options=[], lean_request="DESIGN 8cf2cbce")
        self.assertEqual(t.bridge.watch(), 1)
        t.reply(5, "dave", "I will read the design tomorrow")
        n = t.notes_db[5][-1]
        n["updated_at"] = n["created_at"]
        n["body"], n["updated_at"] = "answer D-1 approve", tb.tick()       # someone who may edit others' notes
        self.assertEqual(t.bridge.collect(), 0)
        self.assertFalse(os.path.isdir(os.path.join(t.cfg["vouchers_dir"], "RUN-PAY-1-1")) and t.answers())
        self.assertTrue(any("编辑过" in x["body"] and "新的回复" in x["body"] for x in t.notes_db[5]))
        d = dict(t.bridge.ledger.db.execute("SELECT * FROM decisions WHERE id='D-1'").fetchone())
        raw = t.bridge.gl.file_raw(tb.P, "runs/PAY-1/park.json", t.mrs[5]["sha"])
        v = decisions.check_answer({"id": "D-1", "run_id": d["run_id"], "sha": d["sha"], "park_hash": d["park_hash"]},
                                   raw, t.bridge.gl.mr_notes(tb.P, 5), t.bridge.kit(tb.P), t.bridge.roles, "alice",
                                   t.mrs[5]["sha"], head_pushed_at(t.bridge.gl.mr_versions(tb.P, 5), d["sha"]))
        self.assertNotEqual(v.status, "answer")                             # an outside signer agrees
        t.reply(5, "dave", "answer D-1 approve")                            # a new reply counts
        t.notes_db[5][-1]["updated_at"] = t.notes_db[5][-1]["created_at"]
        self.assertEqual(t.bridge.collect(), 1)


# ---------- S3: one run per requirement, also from two threads ----------

class SlowSigner:
    def __init__(self, key):
        self.key, self.barrier = key, threading.Barrier(2, timeout=3)

    def sign(self, voucher):
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return sign.KeySigner(self.key).sign(voucher)


@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class OneRunPerRequirementAcrossThreads(unittest.TestCase):
    def test_two_mrs_for_one_requirement_from_two_threads(self):
        t = fixture(self)
        t.mrs[7] = t.mr(7, tb.BRANCH, t.sha)
        t.events[7] = [t.ev("alice", "add")]
        br = Bridge(t.cfg, GitLab("https://gl.example", "t", t.fake), Jira("https://jira.example", "t", "2", t.fake),
                    KiroCrew("http://127.0.0.1:5476", "brain-bridge", "app-secret", t.fake), out=lambda *_: None,
                    signer=SlowSigner(t.key))
        kit = br.kit(tb.P)
        results, errors = {}, {}

        def run(iid):
            try:
                results[iid] = br.process(tb.P, kit, t.mrs[iid])
            except Exception as e:  # noqa: BLE001 - reported below
                errors[iid] = "%s: %s" % (type(e).__name__, e)

        threads = [threading.Thread(target=run, args=(i,)) for i in (5, 7)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(30)
        vdir = os.path.join(t.cfg["vouchers_dir"], "RUN-PAY-1-1")
        signed = [load_json(os.path.join(vdir, f))["voucher"] for f in sorted(os.listdir(vdir))] \
            if os.path.isdir(vdir) else []
        self.assertEqual((sorted(results.values()), errors, len(signed)), (["accepted", "rejected"], {}, 1))
        row = br.ledger.run("RUN-PAY-1-1")
        self.assertEqual((row["state"], row["voucher_id"]), ("spooled", signed[0]["voucher_id"]))
        self.assertIsNone(br.ledger.run("RUN-PAY-1-2"))

    def test_a_failed_signature_gives_the_run_id_back(self):
        t = fixture(self)

        class Away:
            def sign(self, voucher):
                raise sign.SignerUnavailable("not there")
        t.bridge.signer = Away()
        with self.assertRaises(sign.SignerUnavailable):
            t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5])
        self.assertIsNone(t.bridge.ledger.open_run_for("PAY-1"))
        t.bridge.signer = sign.KeySigner(t.key)
        self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "accepted")
        self.assertEqual(t.bridge.ledger.run("RUN-PAY-1-1")["state"], "spooled")


# ---------- S4: the newest signed word on a DESIGN hash wins ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class LaterRejectionWithdrawsTheApproval(unittest.TestCase):
    def setUp(self):
        d, self.repo = run_repo(self)
        commit_file(self.repo, DESIGN, "# design v1\n")
        self.first_ask = commit_file(self.repo, "src/pay/charge.py", "x = 1\n", "risky code, DESIGN question D-1")
        self.key, self.allowed = keypair(d)
        self.vdir = os.path.join(d, "vouchers")
        self.env = {"BRAIN_VOUCHERS_DIR": self.vdir, "BRAIN_ALLOWED_SIGNERS": self.allowed}
        self.h = hashlib.sha256(b"# design v1\n").hexdigest()[:8]

    def voucher(self, vid, did, at, choice):
        v = {"schema": 1, "kind": "answer", "decision": did, "run_id": "RUN-PAY-1-1", "sha": at, "choice": choice,
             "lean_request": "DESIGN " + self.h, "decision_kind": "design", "voucher_id": vid}
        write_json(os.path.join(self.vdir, "RUN-PAY-1-1", vid + ".json"), {"voucher": v, "sig": sign.sign(v, self.key)})

    def test_a_later_reject_of_the_same_design(self):
        self.voucher("V-1", "D-1", self.first_ask, "approve")
        later = commit_file(self.repo, "src/pay/refund.py", "y = 2\n", "more risky code, DESIGN question D-2")
        self.voucher("V-2", "D-2", later, "reject")
        self.assertTrue(ci.check_run(self.repo, BR, self.env))
        # asked once more and approved: the newest word is yes again
        self.voucher("V-3", "D-3", commit_file(self.repo, "src/other.py", "z\n"), "approve")
        self.assertEqual(ci.check_run(self.repo, BR, self.env), [])

    def test_an_older_reject_does_not_undo_a_newer_approval(self):
        self.voucher("V-1", "D-1", self.first_ask, "reject")
        self.voucher("V-2", "D-2", commit_file(self.repo, "src/other.py", "z\n"), "approve")
        self.assertEqual(ci.check_run(self.repo, BR, self.env), [])


# ---------- S5: every risk-path change is seen (renames, names git would quote) ----------

class RiskPathChangesAreSeen(unittest.TestCase):
    def setUp(self):
        _, self.repo = run_repo(self, extra=[("src/pay/limits.py",
                                              "".join("LIMIT_%d = %d\n" % (i, i) for i in range(30)))])

    def risky(self):
        return [p for p in ci.check_run(self.repo, BR, {}) if "risk paths changed" in p]

    def test_a_risk_path_file_moved_out(self):
        os.makedirs(os.path.join(self.repo, "src", "legacy"), exist_ok=True)
        sh("git", "-C", self.repo, "mv", "src/pay/limits.py", "src/legacy/limits.py")
        sh("git", "-C", self.repo, "commit", "-q", "-m", "move")
        self.assertTrue(self.risky())

    def test_a_risk_path_file_git_prints_quoted(self):
        commit_file(self.repo, 'src/pay/rules"v2.json', '{"limit_check": false}\n')
        self.assertTrue(self.risky())

    def test_design_approval_sees_a_quoted_name_changed_after_it(self):
        """design_approval_covers' list of changes since the approval reads names as they are, not C-quoted."""
        repo = self.repo
        commit_file(repo, DESIGN, "# design v1\n")
        at = commit_file(repo, "src/pay/charge.py", "x\n")
        commit_file(repo, DESIGN, "# design v2\n")
        odd = 'src/pay/rules"v2.json'
        commit_file(repo, odd, "{}\n")
        v = {"voucher_id": "V-1", "lean_request": "DESIGN " + hashlib.sha256(b"# design v1\n").hexdigest()[:8],
             "sha": at}
        self.assertIn(odd, ci.design_approval_covers(repo, v, DESIGN, [odd]))


# ---------- S6: a dropped HTTP error body is a BrainError ----------

class _Body(io.RawIOBase):
    def __init__(self, exc):
        self.exc = exc

    def readable(self):
        return True

    def readinto(self, b):
        raise self.exc


def error_with_unreadable_body(exc):
    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, io.BufferedReader(_Body(exc)))
    return urlopen


class StopLoop(Exception):
    pass


class ErrorBodyReads(unittest.TestCase):
    EXC = (socket.timeout("timed out"), http.client.IncompleteRead(b"abc", 10), ConnectionResetError("reset"))

    def setUp(self):
        self.addCleanup(setattr, urllib.request, "urlopen", urllib.request.urlopen)

    def test_transport_raises_brainerror(self):
        for exc in self.EXC:
            urllib.request.urlopen = error_with_unreadable_body(exc)
            gl = rest.GitLab("https://gitlab.invalid", "t", rest.urllib_transport)
            with self.subTest(exc=type(exc).__name__), self.assertRaises(BrainError):
                gl.mr_notes("grp/repo", 5)

    def test_bridge_loop_survives_the_round(self):
        urllib.request.urlopen = error_with_unreadable_body(socket.timeout("timed out"))

        class Stub:
            kc, cfg = object(), {"interval": 0}

            def poll(self):
                rest.GitLab("https://gitlab.invalid", "t", rest.urllib_transport).project("grp/repo")

            dispatch = watch = collect = reconcile = lambda self: None

        old_build, old_sleep = handoff.build, handoff.time.sleep
        handoff.build = lambda path, transport=None: Stub()

        def stop(_):
            raise StopLoop()
        handoff.time.sleep = stop
        self.addCleanup(setattr, handoff, "build", old_build)
        self.addCleanup(setattr, handoff.time, "sleep", old_sleep)
        with redirect_stderr(io.StringIO()), self.assertRaises(StopLoop):
            handoff.main(["--config", "unused.json", "loop"])


# ---------- S7: a signer error leaves the answer to be delivered later ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class SignerErrorDoesNotStrandTheAnswer(unittest.TestCase):
    def test_key_missing_for_two_rounds(self):
        t = fixture(self)
        t.dispatched()
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A")
        good = t.bridge.signer
        t.bridge.signer = sign.KeySigner(os.path.join(t.d, "key-not-mounted-yet"))
        for _ in range(2):
            with self.assertRaises(BrainError):
                t.bridge.collect()
        state = t.bridge.ledger.db.execute("SELECT state FROM decisions WHERE id='D-1'").fetchone()[0]
        self.assertEqual(state, "open")
        told = [n["body"] for n in t.notes_db[5] if n["body"].startswith("D-1 ") and "没能签名" in n["body"]]
        self.assertEqual(len(told), 1)                                       # the MR is told once
        self.assertNotIn("key-not-mounted-yet", told[0])                     # no bridge host paths on the MR
        t.bridge.signer = good
        self.assertEqual(t.bridge.collect(), 1)


# ---------- S8: the spool signer's config is checked ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class SpoolSignerConfig(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)

    def cfg(self, **signer):
        s = {"type": "spool", "spool_dir": os.path.join(self.d, "signer"),
             "allowed_signers": os.path.join(self.d, "allowed_signers")}
        s.update(signer)
        return {"projects": ["grp/repo"], "identities": os.path.join(self.d, "ids.json"),
                "vouchers_dir": os.path.join(self.d, "v"), "spool_dir": os.path.join(self.d, "spool"),
                "ledger": os.path.join(self.d, "ledger.sqlite"), "signer": s}

    def test_missing_allowed_signers_is_a_config_problem_not_a_refusal(self):
        key, _ = keypair(tmpdir(self))
        cfg = self.cfg(allowed_signers=os.path.join(self.d, "no-such-file"), timeout=2, poll=0.01)
        s = sign.signer_from_config(cfg)
        v = {"kind": "answer", "run_id": "RUN-PAY-1-1", "choice": "A"}
        rid = s.req_id(v)
        write_json(os.path.join(cfg["signer"]["spool_dir"], "signed", rid + ".json"),
                   {"req_id": rid, "voucher": v, "sig": sign.sign(v, key)})
        with self.assertRaises(BrainError) as e:
            s.sign(v)
        self.assertNotIsInstance(e.exception, sign.SignerRefused)
        self.assertIn("allowed_signers", str(e.exception))
        with open(os.path.join(self.d, "ids.json"), "w") as f:
            f.write('{"users": {}}')
        with self.assertRaises(BrainError) as e:                              # the bridge does not start on it
            Bridge(cfg, None, None, out=lambda *_: None)
        self.assertIn("no-such-file", str(e.exception))

    def test_timeout_or_poll_that_is_not_a_number(self):
        with open(os.path.join(self.d, "ids.json"), "w") as f:
            f.write('{"users": {}}')
        for bad in ({"timeout": "30s"}, {"poll": 0}, {"timeout": True}, {"timeout": float("nan")}):
            p = os.path.join(self.d, "bridge.json")
            with open(p, "w") as f:
                json.dump(self.cfg(**bad), f)
            with self.subTest(bad=bad), self.assertRaises(BrainError) as e:
                load_bridge_config(p)
            self.assertIn("a number of seconds", str(e.exception))


# ---------- S9: an approve/reject question without options can be answered ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class ApproveRejectQuestion(unittest.TestCase):
    def test_a_dangerous_action_question_answered_as_the_bridge_says(self):
        t = fixture(self)
        with open(t.cfg["identities"], "w") as f:
            f.write('{"users": {"alice": {"jira": "alice"}, "erin": {}}, "roles": {"security": ["erin"]}}')
        t.bridge = t.new_bridge()
        t.dispatched()
        over = dict(kind="action", options=[], question="May the run drop the legacy refunds table?")
        t.park(**over)
        self.assertEqual(t.bridge.watch(), 1)
        q = [n["body"] for n in t.notes_db[5] if n["body"].startswith("**D-1")][0]
        self.assertIn("`answer D-1 approve` 或 `answer D-1 reject`", q)
        t.reply(5, "erin", "answer D-1 B")
        self.assertEqual(t.bridge.collect(), 0)
        t.reply(5, "erin", "answer D-1 approve")
        self.assertEqual(t.bridge.collect(), 1)
        self.assertEqual(t.answers()[0]["choice"], "approve")
        self.assertNotIn("lean_request", t.answers()[0])


# ---------- S10: Lean's design hash with a CRLF checkout ----------

@unittest.skipUnless(sign.available() and os.path.exists(LEAN_PY), "ssh-keygen or Lean missing")
class ApprovedDesignWithCrlfCheckout(unittest.TestCase):
    def test_unchanged_design_still_covers(self):
        d, r = run_repo(self, extra=[(".gitattributes", "*.md text eol=crlf\n")])
        commit_file(r, DESIGN, "# design v1\nrefunds keep the limit check\n")
        os.unlink(os.path.join(r, DESIGN))
        sh("git", "-C", r, "checkout", "--", DESIGN)
        asked_at = commit_file(r, "src/pay/charge.py", "x = 1\n", "risky code, then the DESIGN question")
        spec = importlib.util.spec_from_file_location("aidlc_lean_qa", LEAN_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        h = mod.sha256_file(os.path.join(r, DESIGN))[:8]
        key, allowed = keypair(d)
        v = {"schema": 1, "kind": "answer", "decision": "D-1", "run_id": "RUN-PAY-1-1", "sha": asked_at,
             "choice": "approve", "lean_request": "DESIGN " + h, "voucher_id": "V-1"}
        vdir = os.path.join(d, "vouchers")
        write_json(os.path.join(vdir, "RUN-PAY-1-1", "V-1.json"), {"voucher": v, "sig": sign.sign(v, key)})
        commit_file(r, "src/pay/refund.py", "y = 2\n", "more risky code; the design is untouched")
        e = {"BRAIN_VOUCHERS_DIR": vdir, "BRAIN_ALLOWED_SIGNERS": allowed}
        self.assertEqual(ci.check_run(r, BR, e), [])
        commit_file(r, DESIGN, "# design v2\nrefunds skip the limit check\n")      # a changed design still does not
        self.assertTrue(ci.check_run(r, BR, e))


# ---------- S11: exactly a full set of pages is read, not refused ----------

class ExactlyAtTheCap(unittest.TestCase):
    @staticmethod
    def gitlab(total):
        f = Fake()
        rows = [{"id": i, "body": "x", "head_commit_sha": "a" * 40, "created_at": "2026-11-09T00:00:00Z"}
                for i in range(total)]

        def page(m, qy, b):
            n, p = int(qy["per_page"]), int(qy["page"])
            return 200, rows[(p - 1) * n: p * n]

        f.on("GET", r"/api/v4/projects/g/r/merge_requests/1/(notes|versions|resource_label_events)", page)
        return GitLab("https://gitlab.invalid", "t", f)

    def test_all_pages_read(self):
        for name, call, total in (("mr_notes", lambda g: g.mr_notes("g/r", 1), 2000),
                                  ("mr_versions", lambda g: g.mr_versions("g/r", 1), 2000),
                                  ("label_events", lambda g: g.label_events("g/r", 1), 5000)):
            with self.subTest(name):
                self.assertEqual(len(call(self.gitlab(total))), total)
                with self.assertRaises(BrainError):
                    call(self.gitlab(total + 1))


# ---------- S12: GitLab usernames the old pattern did not know ----------

class OwnQuestionIsRecognised(unittest.TestCase):
    def test_role_members_with_other_valid_usernames(self):
        park = {"schema": 1, "status": "waiting", "kind": "clarify", "question": "q?",
                "options": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}]}
        raw = json.dumps(park).encode()
        sha, ph = "a" * 40, hashlib.sha256(raw).hexdigest()
        kit = config.merged(config.DEFAULTS, {"bridge_user": "brain-bridge"})
        d = {"id": "D-1", "run_id": "RUN-PAY-1-1", "sha": sha, "park_hash": ph}
        for member in ("_carol", "c" * 70, "carol.k_", "Carol-K", "x"):
            with self.subTest(member=member[:12]):
                roles = {"product": [member]}
                who = sorted(decisions.allowed_people(kit, "clarify", "alice", roles))
                q = {"id": 10, "author": {"username": "brain-bridge"}, "created_at": "2026-11-09T01:00:00Z",
                     "body": decisions.question_text("D-1", "RUN-PAY-1-1", park, who, "clarify", sha, ph)}
                a = {"id": 11, "author": {"username": "alice"}, "body": "answer D-1 A",
                     "created_at": "2026-11-09T02:00:00Z"}
                v = decisions.check_answer(d, raw, [q, a], kit, roles, "alice", sha, "2026-11-09T00:00:00Z",
                                           question_id=10)
                self.assertEqual(v.status, "answer", "%r: %s" % (v, v.reason))


# ---------- S13 and crew-agent: Lean's PreToolUse sees every tool; --force refreshes what brain-kit owns ----------

def hook(ev):
    return "python3 %s %s" % (cli.CREW_HOOK, ev)


class PreToolUseMustSeeEveryTool(unittest.TestCase):
    OLD = {"hooks": {"agentSpawn": [{"command": hook("SessionStart")}],
                     "userPromptSubmit": [{"command": hook("UserPromptSubmit")}],
                     "preToolUse": [{"matcher": "read", "command": hook("PreToolUse")}]}}
    NEW = {"kiroUpgrade": {"revision": 2}, "hooks": [
        {"trigger": "agentSpawn", "action": {"type": "command", "command": hook("SessionStart")}},
        {"trigger": "userPromptSubmit", "action": {"type": "command", "command": hook("UserPromptSubmit")}},
        {"trigger": "preToolUse", "matcher": "read", "action": {"type": "command", "command": hook("PreToolUse")}}]}

    def test_narrow_matcher(self):
        for name, spec in (("dict format", self.OLD), ("list format", self.NEW)):
            with self.subTest(name):
                self.assertIn(("preToolUse", "PreToolUse"), cli.missing_lean_hooks(spec))
        absent = json.loads(json.dumps(self.OLD))
        del absent["hooks"]["preToolUse"][0]["matcher"]                    # no matcher: every tool
        self.assertEqual(cli.missing_lean_hooks(absent), [])

    def crew(self, spec, *args):
        home = tmpdir(self)
        env(self, HOME=home)
        p = cli.crew_agent_path()
        if spec is not None:
            write_json(p, spec)
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            rc = cli.main(["crew-agent"] + list(args))
        return rc, load_json(p), out.getvalue()

    def test_force_replaces_the_narrow_hook_and_keeps_other_tools(self):
        other = {"name": "other-0", "trigger": "preToolUse", "matcher": "shell",
                 "action": {"type": "command", "command": "other.sh"}}
        spec = json.loads(json.dumps(self.NEW))
        spec["hooks"].append(other)
        self.assertEqual(self.crew(spec)[0], 2)
        rc, have, _ = self.crew(spec, "--force")
        self.assertEqual(rc, 0)
        self.assertEqual(cli.missing_lean_hooks(have), [])
        self.assertIn(other, have["hooks"])
        lean_pre = [h for h in have["hooks"] if cli.CREW_HOOK in json.dumps(h) and h["trigger"] == "preToolUse"]
        self.assertEqual([h.get("matcher") for h in lean_pre], ["*"])        # the narrow one is gone

    def test_force_refreshes_an_outdated_spec(self):
        ship = load_json(cli.CREW_AGENT_SRC)
        old = dict(ship, prompt="an old prompt", model="old-model", extra={"kept": True})
        self.assertEqual(self.crew(old)[0], 2)                               # outdated: says so, changes nothing
        rc, have, _ = self.crew(old, "--force")
        self.assertEqual(rc, 0)
        self.assertEqual((have["prompt"], have["model"], have["extra"]),
                         (ship["prompt"], ship["model"], {"kept": True}))
        self.assertEqual(self.crew(have)[0], 0)

    def test_hooks_for_an_upgraded_spec_without_hooks_are_in_its_format(self):
        spec = cli.add_lean_hooks({"name": "brain-dev", "kiroUpgrade": {"revision": 2}}, cli.CREW_EVENTS)
        self.assertIsInstance(spec["hooks"], list)
        self.assertEqual(cli.missing_lean_hooks(spec), [])


# ---------- K2: a signer slower than the timeout ----------

def slow_service(spool, key, stop, delay):
    req, seen = os.path.join(spool, "requests"), set()
    while not stop.is_set():
        for name in sorted(os.listdir(req)) if os.path.isdir(req) else []:
            if name in seen or not name.endswith(".json"):
                continue
            try:
                r = load_json(os.path.join(req, name))
            except (OSError, ValueError):
                continue
            seen.add(name)
            stop.wait(delay)
            write_json(os.path.join(spool, "signed", name), {"req_id": r["req_id"], "voucher": r["voucher"],
                                                             "sig": sign.KeySigner(key).sign(r["voucher"])})
        stop.wait(0.01)


@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class LateSignatureIsUsed(unittest.TestCase):
    def setUp(self):
        t = self.t = fixture(self)
        self.spool = os.path.join(t.d, "signer-spool")
        del t.cfg["signing_key"]
        t.cfg["signer"] = {"type": "spool", "spool_dir": self.spool, "allowed_signers": t.allowed, "timeout": 0.3,
                           "poll": 0.01}
        stop = threading.Event()
        th = threading.Thread(target=slow_service, args=(self.spool, t.key, stop, 0.6))
        th.start()
        self.addCleanup(th.join)
        self.addCleanup(stop.set)
        t.bridge = t.new_bridge()

    def signed(self):
        d = os.path.join(self.spool, "signed")
        return os.listdir(d) if os.path.isdir(d) else []

    def test_handoff(self):
        t, outcomes = self.t, []
        for _ in range(3):
            try:
                outcomes.append(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]))
            except sign.SignerUnavailable:
                outcomes.append("unavailable")
            time.sleep(1.1)                                  # the next round is a later second
        self.assertEqual(outcomes, ["unavailable", "accepted", "seen"])
        self.assertEqual(len(self.signed()), 1)

    def test_answer(self):
        t = self.t
        t.bridge.signer = sign.KeySigner(t.key)              # the hand-over signs at once
        t.dispatched()
        t.bridge.signer = sign.signer_from_config(t.cfg)
        t.park()
        t.bridge.watch()
        t.reply(5, "alice", "answer D-1 A")
        self.assertEqual(t.bridge.collect(), 0)              # the signer is late
        time.sleep(1.0)
        self.assertEqual(t.bridge.collect(), 1)              # its late signature is used
        self.assertEqual(len(self.signed()), 1)


# ---------- N2: a batch review reply that lists items in other words ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class BatchReviewListsEveryItem(unittest.TestCase):
    def reply_and_collect(self, body):
        t = self.t = fixture(self)
        t.acceptance()
        t.bridge.watch()
        t.reply(5, "carol", body)
        return t.bridge.collect()

    def test_more_items_in_the_note_are_refused(self):
        for body in ("answer D-1 reject A-1 和 D-3 都不对", "answer D-1 reject A-1;D-3 不对",
                     "answer D-1 reject A-1/D-3", "answer D-1 reject A-1 and d-3"):
            with self.subTest(body=body):
                self.assertEqual(self.reply_and_collect(body), 0)
                self.assertTrue(any("项号" in n["body"] and "不算" in n["body"] for n in self.t.notes_db[5]),
                                [n["body"] for n in self.t.notes_db[5]])

    def test_a_note_without_item_ids_still_works(self):
        self.assertEqual(self.reply_and_collect("answer D-1 reject A-1，D-3 按交易日"), 1)
        self.assertEqual(self.t.answers()[0]["rejected_items"], ["D-3", "A-1"])


# ---------- S14: an answer counts only for the question note the ledger recorded ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class QuestionNoteIsTheRecordedOne(unittest.TestCase):
    def ask(self):
        t = self.t = fixture(self)
        t.dispatched()
        t.park()
        self.assertEqual(t.bridge.watch(), 1)
        return [n for n in t.notes_db[5] if n["body"].startswith("**D-1")][0]

    def repost(self, q):
        """Whoever holds the bridge's token deletes the question and posts it again, word for word."""
        t = self.t
        t.notes_db[5].remove(q)
        t.notes_db.setdefault(0, []).append(q)          # GitLab never reuses a note id; the fake counts notes
        t.add_note(None, 5, {"body": q["body"]})
        return t.notes_db[5][-1]

    def view(self, decision):
        """check_answer as a signer calls it with the decision record (no question_id argument)."""
        t = self.t
        sha = decision["sha"]
        return decisions.check_answer(decision, t.bridge.gl.file_raw(tb.P, "runs/PAY-1/park.json", sha),
                                      t.bridge.gl.mr_notes(tb.P, 5), t.bridge.kit(tb.P), t.bridge.roles, "alice",
                                      t.mrs[5]["sha"], head_pushed_at(t.bridge.gl.mr_versions(tb.P, 5), sha))

    def test_a_reposted_question_does_not_rebind_the_decision(self):
        q = self.ask()
        t = self.t
        copy = self.repost(q)
        self.assertNotEqual(copy["id"], q["id"])
        t.reply(5, "alice", "answer D-1 A")
        row = dict(t.bridge.ledger.db.execute("SELECT * FROM decisions WHERE id='D-1'").fetchone())
        v = self.view(row)                                   # the ledger row names the note it was bound to
        self.assertEqual(v.status, "void", v)
        self.assertIn("not on the MR", v.reason)
        for _ in range(2):
            self.assertEqual(t.bridge.collect(), 0)
        self.assertEqual(row["state"], "open")
        self.assertEqual(t.bridge.ledger.db.execute("SELECT state FROM decisions WHERE id='D-1'").fetchone()[0], "void")
        self.assertEqual(sum(n["body"].startswith("D-1 作废") for n in t.notes_db[5]), 1)    # told once
        self.assertEqual(t.bridge.watch(), 0)                # not asked again, not bound to the copy
        self.assertEqual(len(t.sends()), 1)                  # the hand-over only

    def test_the_answer_voucher_names_its_question_note(self):
        q = self.ask()
        t = self.t
        t.reply(5, "alice", "answer D-1 A")
        self.assertEqual(t.bridge.collect(), 1)
        a = t.answers()[0]
        self.assertEqual(a["question_note"], q["id"])
        d = {"id": a["decision"], "run_id": a["run_id"], "sha": a["sha"], "park_hash": a["park_hash"],
             "question_note": a["question_note"]}
        self.assertEqual((self.view(d).status, self.view(d).choice), ("answer", "A"))
        self.repost(q)
        t.reply(5, "alice", "answer D-1 A")
        self.assertEqual(self.view(d).status, "void")        # from the voucher alone, the copy binds nothing


# ---------- unconfirmed items, checked ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class LabelEventsInAnyOrder(unittest.TestCase):
    def test_the_newest_event_wins_whatever_the_list_order(self):
        t = fixture(self)
        t.events[5] = list(reversed([t.ev("alice", "add"), t.ev("alice", "remove"), t.ev("bob", "add")]))
        t.rejected_with("added by bob")


@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class WatchAcrossThreads(unittest.TestCase):
    def test_two_threads_ask_one_question(self):
        t = fixture(self)
        t.dispatched()
        t.park()
        barrier, raw = threading.Barrier(2, timeout=3), t.bridge.gl.file_raw

        def file_raw(project, path, ref):
            if path.endswith("park.json"):
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
            return raw(project, path, ref)
        t.bridge.gl.file_raw = file_raw
        errors = []

        def watch():
            try:
                t.bridge.watch()
            except Exception as e:  # noqa: BLE001 - asserted below
                errors.append("%s: %s" % (type(e).__name__, e))
        threads = [threading.Thread(target=watch) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(t.bridge.ledger.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)
        self.assertEqual(len([n for n in t.notes_db[5] if n["body"].startswith("**D-1")]), 1)


if __name__ == "__main__":
    unittest.main()
