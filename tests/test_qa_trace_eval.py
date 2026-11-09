"""QA findings on the Run Trace and Eval (brainkit/trace.py, brainkit/evaluate.py, brain eval gaps --file), ported
from the reviewer's qa_findings suite. Each test names its finding (N1 ...); the docstring says what was wrong.
Everything is local: temp git repos, a sqlite ledger and voucher files on disk."""
import atexit
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
import warnings
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import cli, config, evaluate as EV, trace  # noqa: E402
from brainkit.bridge.decisions import SCHEMA as DECISIONS_SCHEMA  # noqa: E402
from brainkit.bridge.ledger import Ledger  # noqa: E402
from brainkit.commands import eval_cmd  # noqa: E402
from brainkit.common import iso, utcnow, write_json  # noqa: E402
from tests.helpers import sh, tmpdir  # noqa: E402
from tests.test_trace import git_repo  # noqa: E402

BOT = "brain-bot@noreply.invalid"
KIT = {"version": 1, "req_prefix": "PAY", "bot_users": ["brain-bot"], "bot_emails": [BOT], "tracker": "none"}
TECH = ("# TECH\n\n<!-- id:T-PAY-1-1 type:pitfall side:builder rel:new src:D-1 -->\n退款单号是 RF + 9 位。\n\n"
        "<!-- id:T-PAY-1-2 type:pitfall side:builder rel:new src:D-1 -->\n重试最多 3 次。\n")
PRODUCT = "# PRODUCT\n\n<!-- id:P-PAY-1-1 type:model side:product rel:new src:D-1 -->\n**退款单**：一次退款的记录。\n"
AS_OF = "2026-12-31T00:00:00Z"


def vid(seed):
    return "V-" + hashlib.sha256(str(seed).encode()).hexdigest()[:10]


def audit(events, email, host="h"):
    """Hash-chained Lean audit lines (Lean's rule: hash over the record without the hash; prev; seq from 1)."""
    out, prev = [], "GENESIS"
    for n, (ts, event, intent, data) in enumerate(events, 1):
        rec = {"seq": n, "ts": ts, "event": event, "intent": intent,
               "actor": {"git_email": email, "git_name": "n", "os_user": "u", "host": host, "team": None},
               "data": data, "prev": prev}
        rec["hash"] = hashlib.sha256(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        prev = rec["hash"]
        out.append(json.dumps(rec, ensure_ascii=False))
    return "\n".join(out) + "\n"


def table(rows):
    return "| ID | Date | Stage | Decision | Why | Who |\n|---|---|---|---|---|---|\n" + \
           "".join("| %s |\n" % " | ".join(r) for r in rows)


def release(event="APPROVED", h=8, v=None):
    v = v or vid("release")
    words = "%s RELEASE efefefef (via gitlab P-ABCD %s)" % ("approve" if event == "APPROVED" else "reject", v)
    data = {"kind": "RELEASE", "sha256": "ef" * 32, "words": words}
    if event == "APPROVED":
        data["claimed_channel"] = "gitlab P-ABCD %s" % v
    return ((h, 0), event, data)


class World:
    """One project repo with kit.json and a small Brain; runs (local half, hand-over, runner), merges."""

    def __init__(self, test):
        self.d = tmpdir(test)
        self.repo = os.path.join(self.d, "proj")
        git_repo(self.repo)                      # no hooks, no background maintenance: see there
        self.commit({".brain/kit.json": json.dumps(KIT), "brain/TECH.md": TECH, "brain/PRODUCT.md": PRODUCT,
                     "README.md": "x\n"}, "init", "2026-10-01T00:00:00Z", "lead@example.com")
        self.vouchers = os.path.join(self.d, "vouchers")
        self.ledger_path = os.path.join(self.d, "ledger.sqlite")
        self._ledger = None

    def git(self, *args, env=None):
        return sh("git", "-C", self.repo, *args, env=env)

    def rev(self, r="HEAD"):
        return self.git("rev-parse", r).stdout.strip()

    def commit(self, files, msg, date, email="alice@example.com"):
        for rel, text in files.items():
            p = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg, env={"GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email,
                                                 "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        return self.rev()

    def cfg(self):
        return config.load(self.repo)

    def run(self, key, slug, day, decisions=None, local_events=None, runner_events=None,
            local_audit="aidlc-docs/audit/local-%(key)s.jsonl", runner_audit="aidlc-docs/audit/runner-%(key)s.jsonl",
            person_after_handover=False, park=None, month=11):
        """Start (owner, local half) -> hand over -> runner commits (bot). Leaves the repo on the run branch."""
        intent = "26%02d%02d-%s-%s" % (month, day, key.lower(), slug)
        br = "ai/alice/%s-%s" % (key, slug)
        base = self.rev("main")
        self.git("switch", "-q", "-c", br, "main")
        t = lambda h, m=0: "2026-%02d-%02dT%02d:%02d:00Z" % (month, day, h, m)  # noqa: E731
        lt = lambda h, m=0: "2026-%02d-%02dT%02d:%02d:00.000Z" % (month, day, h, m)  # noqa: E731
        rec = {"schema": 1, "key": key, "tracker": "none", "owner": "alice", "branch": br, "intent": intent,
               "brain_commit": base, "kit_version": "0.1.0", "origin": "local", "expected_paths": [], "materials": [],
               "created_at": t(0), "claim_checked": True, "handoff": None}
        docs = "aidlc-docs/%s/" % intent
        local = [(lt(0), "INTENT_CREATED", intent, {"title": key}),
                 (lt(0, 1), "STAGE_STARTED", intent, {"stage": "requirements", "unit": None}),
                 (lt(1), "STAGE_COMPLETED", intent, {"stage": "requirements", "unit": None, "note": "AC-1"}),
                 (lt(1, 30), "APPROVED", intent, {"kind": "REQUIREMENTS", "sha256": "ab" * 32, "session": "s1",
                                                 "words": "approve REQUIREMENTS abababab"})]
        local += [(lt(*hm), ev, intent, data) for hm, ev, data in (local_events or [])]
        local.sort(key=lambda e: e[0])
        rows = decisions if decisions is not None else [["D-1", "2026-%02d-%02d" % (month, day), "requirements",
                                                         "单号按 T-PAY-1-1", "样例", "agent"]]
        la = local_audit % {"key": key}
        self.commit({"runs/%s/run.json" % key: json.dumps(rec), docs + "decisions.md": "# Decisions\n\n" + table(rows),
                     docs + "inception/requirements/requirements.md":
                         "# Requirements\n\n## Assumptions\n- A-1: 只退 14 天内的订单\n",
                     la: audit(local, "alice@example.com")}, "%s: start" % key, t(2))
        info = {"key": key, "intent": intent, "branch": br, "base": base, "docs": docs}
        rec["handoff"] = {"requested_at": t(3), "by": "alice", "local_head": "x"}
        self.commit({"runs/%s/run.json" % key: json.dumps(rec)}, "%s: hand off" % key, t(3))
        runner = [(lt(4), "SESSION_START", intent, {"session": "k1"}),
                  (lt(4, 30), "VERIFY", intent, {"ok": True, "fingerprint": "f2f2f2f2", "results": [
                      {"cmd": "python3 -m unittest", "exit": 0}]})]
        runner += [(lt(*hm), ev, intent, data) for hm, ev, data in (runner_events or [])]
        runner.sort(key=lambda e: e[0])
        files = {"src/%s.py" % slug: "x = 1\n", runner_audit % {"key": key}: audit(runner, BOT)}
        if park is not None:
            files["runs/%s/park.json" % key] = json.dumps(park)
        self.commit(files, "%s: code" % key, t(5), BOT)
        if person_after_handover:
            self.commit({"src/%s.py" % slug: "x = 2\n"}, "%s: tweak" % key, t(6), "bob@example.com")
        self.commit({docs + "notes.md": "done\n"}, "%s: records" % key, t(7), BOT)
        info["tip"] = self.rev()
        return info

    def merge(self, info, how="merge", date=None, delete_branch=False):
        """how: merge (--no-ff, GitLab message) | squash | ff | rebase (GitLab rebases on the server: the local
        branch keeps the old commits)."""
        br = info["branch"]
        md = date or "2026-11-%02dT00:00:00Z" % (int(info["intent"][4:6]) + 1)
        env = {"GIT_COMMITTER_DATE": md, "GIT_AUTHOR_DATE": md, "GIT_COMMITTER_EMAIL": "lead@example.com",
               "GIT_AUTHOR_EMAIL": "lead@example.com"}
        if how == "rebase":
            self.git("switch", "-q", "-c", "tmp-rebase", br)
            self.git("rebase", "-q", "main", env={"GIT_COMMITTER_DATE": md, "GIT_COMMITTER_EMAIL": "lead@example.com"})
            self.git("switch", "-q", "main")
            self.git("merge", "-q", "--ff-only", "tmp-rebase")
            self.git("branch", "-q", "-D", "tmp-rebase")
        else:
            self.git("switch", "-q", "main")
        if how == "merge":
            self.git("merge", "-q", "--no-ff", br, "-m", "Merge branch '%s' into 'main'" % br, env=env)
        elif how == "ff":
            self.git("merge", "-q", "--ff-only", br, env=env)
        elif how == "squash":
            self.git("merge", "-q", "--squash", br, env=env)
            self.git("commit", "-q", "-m", "%s %s (squashed)" % (info["key"], info["intent"]), env=env)
        if delete_branch:
            self.git("branch", "-q", "-D", br)
        return self.rev("main")

    @property
    def ledger(self):
        if self._ledger is None:
            self._ledger = Ledger(self.ledger_path)
            self._ledger.db.executescript(DECISIONS_SCHEMA)
        return self._ledger

    def close_ledger(self):
        if self._ledger is not None:
            self._ledger.db.close()
            self._ledger = None

    def voucher(self, run_id, doc):
        write_json(os.path.join(self.vouchers, run_id, doc["voucher_id"] + ".json"), {"voucher": doc, "sig": "x"})

    def copy(self, test):
        """A World of the test's own: a copy of this one's directory (a copied git repo is a repo of its own)."""
        w = World.__new__(World)
        w.d = tmpdir(test)
        shutil.copytree(self.d, w.d, symlinks=True, dirs_exist_ok=True)
        w.repo, w.vouchers, w.ledger_path = (os.path.join(w.d, os.path.relpath(x, self.d))
                                             for x in (self.repo, self.vouchers, self.ledger_path))
        w._ledger = None
        return w


class _Keep:
    """A home for World directories that outlive one test (removed when the interpreter exits)."""

    def addCleanup(self, fn, *args, **kw):
        atexit.register(fn, *args, **kw)


_TEMPLATES = {}


def base_run(test, **kw):
    """(World, run info) of the test's own: run PAY-1 (refund, day 2; the runner's RELEASE approval, plus `kw` for
    World.run), left on its run branch. Each variant is made once and copied for every test that asks for it."""
    k = json.dumps(kw, sort_keys=True)
    if k not in _TEMPLATES:
        w = World(_Keep())
        _TEMPLATES[k] = (w, w.run("PAY-1", "refund", 2, runner_events=[release()], **kw))
    w, r = _TEMPLATES[k]
    return w.copy(test), dict(r)


def run_cli(repo, args, env=None):
    """`brain <args>` in-process with cwd = repo: (exit code, stdout, stderr). The intake store is one that does not
    exist next to the repo (unless env names one): the developer's own ~/.brain/intake is never read."""
    old_cwd, old_env = os.getcwd(), dict(os.environ)
    out, err = io.StringIO(), io.StringIO()
    try:
        os.chdir(repo)
        os.environ.pop("GITLAB_TOKEN", None)
        os.environ["BRAIN_INTAKE_DIR"] = os.path.join(os.path.dirname(os.path.abspath(repo)), "no-intake")
        os.environ.update(env or {})
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(args))
            except SystemExit as e:
                code = e.code
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)
    return code, out.getvalue(), err.getvalue()


def _intents():
    try:
        from brainkit import intents
    except Exception:  # noqa: BLE001
        return None
    return intents if callable(getattr(intents, "add", None)) else None


PARK = {"schema": 1, "status": "waiting", "kind": "clarify", "ask": "owner", "question": "退款按自然日还是交易日？",
        "options": [{"id": "A", "text": "自然日"}, {"id": "B", "text": "交易日"}]}


class BranchAndMerge(unittest.TestCase):
    def test_n1_owner_clone_gives_the_same_trace_as_a_teammate_clone(self):
        """N1: the owner's stale local run branch (still at the hand-over commit) hid the runner's commits, a
        person's push and the park question: autonomy differed by machine."""
        w, r = base_run(self, person_after_handover=True, park=PARK)
        handover = w.git("log", "--format=%H", "--grep=PAY-1: hand off", r["branch"]).stdout.split()[0]
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        w.git("branch", "-q", "-D", r["branch"])                       # a teammate's clone
        mate = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        self.assertIn("second parent of merge commit", mate["src"]["git"]["tip_src"])
        self.assertEqual(len(mate["actions"]["commits"]), 5)
        w.git("branch", "-q", r["branch"], handover)                   # the owner's clone: stale local branch
        owner = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        self.assertEqual(len(owner["actions"]["commits"]), 5, owner["src"]["git"])
        self.assertEqual([h["kind"] for h in owner["humans"]], [h["kind"] for h in mate["humans"]])
        self.assertEqual(len(owner["actions"]["questions"]), 1)
        self.assertIn("refs/heads/" + r["branch"], owner["src"]["git"]["tip_note"])   # what it did not use
        a_mate = EV.collect(w.repo, w.cfg(), as_of=AS_OF, traces=[mate])["autonomy"]["value"]
        a_owner = EV.collect(w.repo, w.cfg(), as_of=AS_OF, traces=[owner])["autonomy"]["value"]
        self.assertEqual((a_mate, a_owner), ([0, 1], [0, 1]))

    def test_n1_freshest_of_local_and_origin_and_divergence_is_said(self):
        """N1: before the merge, origin's copy (the runner's pushes) wins over a stale local branch; when the two
        diverged, origin is used and the trace says so."""
        w = World(self)
        r = w.run("PAY-1", "refund", 2, person_after_handover=True)
        handover = w.git("log", "--format=%H", "--grep=PAY-1: hand off", r["branch"]).stdout.split()[0]
        w.git("update-ref", "refs/remotes/origin/" + r["branch"], r["tip"])
        w.git("switch", "-q", "--detach", r["tip"])                     # run.json in the working tree
        w.git("branch", "-q", "-f", r["branch"], handover)
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        self.assertEqual((t["src"]["git"]["tip"], t["src"]["git"]["tip_src"]),
                         (r["tip"], "refs/remotes/origin/" + r["branch"]))
        self.assertEqual(len(t["actions"]["commits"]), 5)
        w.git("switch", "-q", r["branch"])                              # a local commit origin never got
        w.commit({"src/local.py": "z = 1\n"}, "local only", "2026-11-02T09:00:00Z")
        w.git("switch", "-q", "--detach", r["tip"])
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        self.assertEqual(t["src"]["git"]["tip"], r["tip"])
        self.assertIn("diverged", t["src"]["git"]["tip_note"])
        self.assertIn("diverged", trace.render(t))

    def test_n6_squash_merge_counts_whether_or_not_the_branch_was_deleted(self):
        """N6: a squash merge with the branch still present was 'not merged' (merge_of needs ancestry)."""
        for delete in (True, False):
            with self.subTest(deleted=delete):
                w, r = base_run(self)
                w.merge(r, "squash", date="2026-11-03T00:00:00Z", delete_branch=delete)
                t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
                self.assertEqual(t["outcome"]["merged_at"]["value"], "2026-11-03T00:00:00Z", t["outcome"]["merged_at"])
                self.assertIn("squash", t["outcome"]["merged_at"]["how"])
                d = EV.collect(w.repo, w.cfg(), as_of=AS_OF, traces=[t])
                # branch deleted: the run's own commits cannot be listed, so a person's push after the hand-over
                # cannot be seen either: autonomy is not known (QA round 4), the delivery still is
                self.assertEqual((d["delivery"]["value"], d["autonomy"]["value"]),
                                 ([1, 1], [0, 0] if delete else [1, 1]), d["autonomy"]["runs"])
                if not delete:
                    self.assertEqual(len(t["actions"]["commits"]), 4)   # the branch's own commits are listed

    def test_n6_rebase_merge_with_the_old_branch_left(self):
        """N6: GitLab rebased the branch on the server (new commits on main, the local branch keeps the old ones):
        matched by patch-id; the merge is the last rebased commit, and none of them is a hotfix."""
        w, r = base_run(self)
        w.commit({"src/fix.py": "y\n"}, "PAY-1: fix failing test", "2026-11-02T08:00:00Z", BOT)   # in the run
        handover = w.git("log", "--format=%H", "--grep=PAY-1: hand off", r["branch"]).stdout.split()[0]
        w.git("switch", "-q", "main")
        w.commit({"other.txt": "o\n"}, "unrelated", "2026-11-02T10:00:00Z", "lead@example.com")  # main moved on
        w.merge(r, "rebase", date="2026-11-03T00:00:00Z")
        for local in ("tip", "stale at the hand-over"):
            with self.subTest(local_branch=local):
                if local != "tip":
                    w.git("branch", "-q", "-f", r["branch"], handover)
                t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
                m = t["outcome"]["merged_at"]
                self.assertEqual((m["value"], m["commit"]), ("2026-11-03T00:00:00Z", w.rev("main")), m)
                self.assertIn("rebase", m["how"])
                rv = t["outcome"]["reverted_or_hotfixed_within_14d"]
                self.assertIs(rv["value"], False, rv)
                d = EV.collect(w.repo, w.cfg(), as_of=AS_OF, traces=[t])
                self.assertEqual(d["delivery"]["by_run"], {"PAY-1": "correct"})

    def test_n7_ff_merged_run_with_deleted_branch_is_judged_from_its_merge(self):
        """N7: a fast-forward merge with the branch deleted took merged_at = the run's start commit and counted the
        run's own later commits as hotfixes."""
        w = World(self)
        r = w.run("PAY-1", "login", 2, runner_events=[release()], person_after_handover=True)
        w.commit({"src/login.py": "y = 1\n"}, "PAY-1: fix failing test", "2026-11-02T08:30:00Z", BOT)
        w.merge(r, "ff", delete_branch=True)
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        o = t["outcome"]
        self.assertEqual(o["merged_at"]["value"], "2026-11-02T08:30:00Z", o["merged_at"])
        self.assertFalse(o["reverted_or_hotfixed_within_14d"]["value"], o["reverted_or_hotfixed_within_14d"])
        self.assertEqual(len(t["actions"]["commits"]), 6)                # the run's commits, bob's push included
        d = EV.collect(w.repo, w.cfg(), as_of=AS_OF, traces=[t])["delivery"]
        self.assertEqual(d["by_run"], {"PAY-1": "correct"})
        # a later hotfix by a person is still found
        w.commit({"src/login.py": "y = 2\n"}, "hotfix PAY-1 login", "2026-11-04T00:00:00Z", "lead@example.com")
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        self.assertEqual(t["outcome"]["merged_at"]["value"], "2026-11-02T08:30:00Z")
        self.assertIs(t["outcome"]["reverted_or_hotfixed_within_14d"]["value"], True)

    def test_n12_hotfix_is_found_behind_an_old_dated_commit(self):
        """N12: `git log --since` stopped at a later commit with an older committer date and hid the hotfix."""
        w = World(self)
        r = w.run("PAY-1", "refund", 2)
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        w.commit({"src/refund.py": "x = 3\n"}, "hotfix PAY-1 rounding", "2026-11-05T00:00:00Z", "lead@example.com")
        w.commit({"docs/x.md": "y\n"}, "docs", "2026-10-01T00:00:00Z", "lead@example.com")
        o = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)["outcome"]["reverted_or_hotfixed_within_14d"]
        self.assertTrue(o["value"], o)

    def test_n13_gap_from_the_runs_own_reflect_is_not_a_defect_claim(self):
        """N13: a gap intent (source gap, found_in PAY-1) whose title names PAY-1 and 'bug' counted as a defect."""
        intents = _intents()
        if intents is None:
            self.skipTest("brainkit.intents not installed")
        w = World(self)
        now = utcnow()
        r = w.run("PAY-1", "refund", 2)
        w.merge(r, "merge", date=iso(now - timedelta(days=3)))
        doc = intents.add(w.repo, w.cfg(), "gap", "PAY-1 的退款 bug 没有回归测试", "验收时手工发现，需要每天跑的测试。",
                          owner="carol", extra={"gap_kind": "test", "found_in": "PAY-1"})
        self.assertEqual((doc["source"], doc["found_in"]), ("gap", "PAY-1"))
        rv = trace.build(w.repo, w.cfg(), "PAY-1", as_of=iso(now + timedelta(days=20)))["outcome"][
            "reverted_or_hotfixed_within_14d"]
        self.assertFalse(rv["value"], rv["evidence"])


class Privacy(unittest.TestCase):
    def test_n5_trace_does_not_carry_the_clone_host_name(self):
        """N5: Lean names its audit file <host>-<6 hex>.jsonl; the trace copied it into JSON and text."""
        host = "alicesmacbookpro"
        w = World(self)
        r = w.run("PAY-1", "refund", 2, local_audit="aidlc-docs/audit/%s-1a2b3c.jsonl" % host,
                  runner_audit="aidlc-docs/audit/brainrunner01-9f8e7d.jsonl")
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        blob = json.dumps(t, ensure_ascii=False) + trace.render(t)
        self.assertNotIn(host, blob)
        self.assertNotIn("brainrunner01", blob)
        self.assertEqual(len(t["src"]["lean_audit"]), 2)
        self.assertTrue(all(p.startswith("aidlc-docs/audit/L-") for p in t["src"]["lean_audit"]))
        self.assertEqual(t["src"]["lean_audit"], trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)["src"]["lean_audit"])

    def test_n20_no_email_or_home_path_in_the_trace(self):
        """N20: blocked commands and stage notes were copied with emails and /Users/<name> paths."""
        cmd = ("cd /Users/alice.wang/work/pay && git -c user.email=alice.wang@example.com commit -m x "
               "aidlc-docs/lean.json")
        blocked = ((4, 10), "BLOCKED", {"tool": "execute_bash", "gate": "records", "command": cmd,
                                        "path": "/home/alice.wang/pay/aidlc-docs/lean.json"})
        note = ((4, 20), "STAGE_COMPLETED", {"stage": "code-generation", "unit": None,
                                             "note": "ran in /Users/alice.wang/work, asked alice.wang@example.com"})
        failed = ((4, 25), "VERIFY", {"ok": False, "results": [{"cmd": "/Users/alice.wang/bin/lint", "exit": 1}]})
        w = World(self)
        r = w.run("PAY-1", "refund", 2, runner_events=[blocked, note, failed])
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        blob = json.dumps(t, ensure_ascii=False)
        self.assertEqual(len(t["actions"]["blocked"]), 1)
        for s in ("alice.wang", "example.com"):
            self.assertNotIn(s, blob)
        self.assertIn(trace.pseudonym("alice.wang@example.com"), t["actions"]["blocked"][0]["command"])
        self.assertIn("~/work/pay", t["actions"]["blocked"][0]["command"])


class Credits(unittest.TestCase):
    def runs(self, n=3):
        w = World(self)
        for i, day in enumerate((2, 4, 6)[:n], 1):
            r = w.run("PAY-%d" % i, "s%d" % i, day)
            w.merge(r, "merge", date="2026-11-%02dT12:00:00Z" % (day + 1))
        return w

    def test_n14_one_record_is_not_given_to_every_run(self):
        """N14: a single unkeyed credits record (and its manual restart) was applied to every run."""
        w = self.runs()
        one = {"credits": 12.5, "manual": [{"at": "2026-11-02T06:00:00Z", "kind": "restart"}]}
        d = EV.collect(w.repo, w.cfg(), as_of=AS_OF, credits=one)
        self.assertEqual([t["key"] for t in d["traces"] if t["cost"]["credits"] is not None], [])
        self.assertEqual([t["key"] for t in d["traces"] if any(h["kind"] == "platform_restart" for h in t["humans"])],
                         [])
        self.assertIn("3", d["cost"]["note"])
        self.assertIn(d["cost"]["note"], EV.report(d))
        alone = World(self)                                               # one run: the short form is fine
        r = alone.run("PAY-1", "s1", 2)
        alone.merge(r, "merge", date="2026-11-03T12:00:00Z")
        d = EV.collect(alone.repo, alone.cfg(), as_of=AS_OF, credits=one)
        self.assertEqual(d["traces"][0]["cost"]["credits"], 12.5)

    def test_n15_run_id_keyed_credits_are_used(self):
        """N15: credits keyed RUN-<KEY>-<n> were dropped without a ledger or vouchers to learn the run ids."""
        w = self.runs(1)
        credits = {"runs": {"RUN-PAY-1-1": {"credits": 12.5,
                                            "manual": [{"at": "2026-11-02T05:00:00Z", "kind": "restart"}]},
                            "RUN-PAY-11-1": {"credits": 99}}}
        t = trace.build(w.repo, w.cfg(), "PAY-1", credits=credits, as_of=AS_OF)
        self.assertEqual(t["cost"]["credits"], 12.5)
        self.assertIn("platform_restart", [h["kind"] for h in t["humans"]])
        none = trace.build(w.repo, w.cfg(), "PAY-1", credits={"runs": {"RUN-PAY-2-1": {"credits": 1}}}, as_of=AS_OF)
        self.assertIsNone(none["cost"]["credits"])
        self.assertIn("none for PAY-1", none["cost"]["why"])


class Metrics(unittest.TestCase):
    def test_n11_open_window_runs_are_not_in_the_denominator(self):
        """N11: a run known to be wrong went into the delivery denominator before its 14 days were over."""
        w = World(self)
        r1 = w.run("PAY-1", "a", 2, runner_events=[release(v=vid(1))])
        w.merge(r1, "merge", date="2026-11-03T00:00:00Z")
        r2 = w.run("PAY-2", "b", 24, runner_events=[release(v=vid(2))])
        w.merge(r2, "merge", date="2026-11-25T00:00:00Z")
        r3 = w.run("PAY-3", "c", 25, runner_events=[release("REJECTED", 8, vid(3)), release("APPROVED", 9, vid(4))])
        w.merge(r3, "merge", date="2026-11-26T00:00:00Z")
        d = EV.collect(w.repo, w.cfg(), as_of="2026-11-28T00:00:00Z")
        dl = d["delivery"]
        self.assertEqual((dl["pending"], dl["wrong_open"], dl["value"]), (["PAY-2"], ["PAY-3"], [1, 1]), dl)
        self.assertEqual(d["autonomy"]["value"], [1, 1])
        self.assertIn("PAY-3", EV.report(d))
        later = EV.collect(w.repo, w.cfg(), as_of=AS_OF)["delivery"]
        self.assertEqual((later["value"], later["by_run"]["PAY-3"]), ([2, 3], "wrong"))

    def test_n11_reverted_without_acceptance_is_not_counted(self):
        w = World(self)
        r = w.run("PAY-1", "a", 2)
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        w.commit({"src/a.py": "x = 9\n"}, "hotfix PAY-1", "2026-11-04T00:00:00Z", "lead@example.com")
        d = EV.collect(w.repo, w.cfg(), as_of=AS_OF)["delivery"]
        self.assertEqual((d["value"], d["unknown_acceptance"]), ([0, 0], ["PAY-1"]))

    def test_n18_period_counts(self):
        """N18: with --since, an open question asked in the period was not counted, an old oracle answer was."""
        w = World(self)
        r = w.run("PAY-1", "refund", 20)
        w.merge(r, "merge", date="2026-11-21T00:00:00Z")
        run = "RUN-PAY-1-1"
        w.ledger.add_run(run, "grp/repo", "PAY-1", 1, r["tip"], vid("h"), owner="alice")
        w.ledger.db.execute(
            "INSERT INTO decisions(id,run_id,project,iid,sha,park_hash,kind,lean_request,state,answer,answerer,"
            "voucher_id,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("D-1", run, "grp/repo", 1, r["tip"], "h1", "clarify", None, "open", None, None, None,
             "2026-11-20T05:00:00Z", "2026-11-20T05:00:00Z"))
        w.close_ledger()
        w.voucher(run, {"schema": 1, "kind": "answer", "decision": "D-9", "run_id": run, "decision_kind": "clarify",
                        "choice": "B", "answerer": "P-0RAC", "issued_at": "2026-10-01T05:01:00Z",
                        "voucher_id": vid("oracle"), "eval_oracle": True})
        kw = dict(as_of=AS_OF, ledger_path=w.ledger_path, vouchers_dir=w.vouchers)
        f = EV.collect(w.repo, w.cfg(), since="2026-11-15", **kw)["funnel"]
        self.assertEqual((f["unanswered_questions"], f["oracle_answers_excluded"]), (1, 0))
        f = EV.collect(w.repo, w.cfg(), **kw)["funnel"]
        self.assertEqual((f["unanswered_questions"], f["oracle_answers_excluded"]), (1, 1))
        f = EV.collect(w.repo, w.cfg(), since="2026-11-25", **kw)["funnel"]
        self.assertEqual((f["unanswered_questions"], f["oracle_answers_excluded"]), (0, 0))

    def test_n19_who_vocabulary(self):
        """N19: Who values outside the runner's vocabulary (agent | brain <entry id> | check | <pseudonym> <voucher
        id>) were guessed (first word only; any username-shaped word, 'TBD' included, was a person). Now they are
        'unclassified' and listed, never shown."""
        c = trace.classify_who
        v = vid("x")
        for who in ("carol, dave", "产品-张三", "John Smith (PM)", "TBD", "product", "alice", "张三", "P-AB12",
                    "brain", "agent smith", "checker"):
            with self.subTest(who):
                self.assertEqual(c(who, ["brain-bot"]), ("unclassified", None))
        self.assertEqual(c("agent"), ("agent", "agent"))
        self.assertEqual(c("`agent`"), ("agent", "agent"))
        self.assertEqual(c("Agent (A-2)"), ("agent", "agent"))
        self.assertEqual(c("brain-bot", ["brain-bot"]), ("agent", "brain-bot"))
        self.assertEqual(c("brain T-PAY-1-1"), ("brain", "T-PAY-1-1"))
        self.assertEqual(c("brain T-PAY-1-1, P-PAY-1-1"), ("brain", "T-PAY-1-1, P-PAY-1-1"))
        self.assertEqual(c("check"), ("check", "check"))
        self.assertEqual(c("check (lint)"), ("check", "check"))
        self.assertEqual(c("P-0305 %s" % v), ("person", "P-0305"))
        self.assertEqual(c(""), (None, None))

    def test_n19_funnel_reports_unclassified_rows(self):
        rows = [["D-1", "2026-11-02", "requirements", "单号按 T-PAY-1-1", "样例", "agent"],
                ["D-2", "2026-11-02", "code", "按产品说的", "", "产品-张三"],
                ["D-3", "2026-11-02", "code", "重试 3 次", "", "check"],
                ["D-4", "2026-11-02", "code", "沿用 T-PAY-1-2", "", "brain T-PAY-1-2"],
                ["D-5", "2026-11-02", "code", "日志字段", "", ""]]
        w = World(self)
        r = w.run("PAY-1", "refund", 2, decisions=rows)
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        d = EV.collect(w.repo, w.cfg(), as_of=AS_OF)
        f = d["funnel"]
        self.assertEqual(f["counts"], {"brain": 2, "check": 1, "agent": 1, "person": 0, "unknown": 1,
                                       "unclassified": 1})
        self.assertEqual([(x["key"], x["id"]) for x in f["unclassified"]], [("PAY-1", "D-2")])
        text = EV.report(d)
        self.assertIn("D-2", text)
        self.assertNotIn("张三", text + json.dumps(d, ensure_ascii=False))


class Gaps(unittest.TestCase):
    def test_n8_same_withdraw_candidate_is_filed_once(self):
        """N8: the count was in the gap's title (and so in its id and the intent's dedupe hash): one more run
        filed the same gap again."""
        intents = _intents()
        if intents is None:
            self.skipTest("brainkit.intents not installed")
        w = World(self)
        env = {"BRAIN_USER": "carol", "BRAIN_INTAKE_DIR": os.path.join(w.d, "no-intake")}
        for n, day in enumerate((2, 4, 6), 1):
            r = w.run("PAY-%d" % n, "s%d" % n, day)
            w.merge(r, "merge", date="2026-11-%02dT12:00:00Z" % (day + 1))
        ids = {g["id"] for g in EV.gaps(EV.collect(w.repo, w.cfg(), as_of=AS_OF))}
        code, out, err = run_cli(w.repo, ["eval", "gaps", "--file"], env=env)
        self.assertEqual(code, 0, err + out)
        self.assertIn("P-PAY-1-1", out)
        code, out2, err = run_cli(w.repo, ["eval", "gaps", "--file"], env=env)
        self.assertNotIn("filed PAY", out2.replace("already filed", ""))
        r = w.run("PAY-20", "s20", 10)                                    # one more run loads the same entry
        w.merge(r, "merge", date="2026-11-11T12:00:00Z")
        gs = EV.gaps(EV.collect(w.repo, w.cfg(), as_of=AS_OF))
        self.assertTrue(ids <= {g["id"] for g in gs})                     # same gaps, same ids
        self.assertTrue(any("4 次" in g["body"] for g in gs if g["title"].startswith("P-PAY-1-1 ")))
        code, out3, err = run_cli(w.repo, ["eval", "gaps", "--file"], env=env)
        self.assertEqual(code, 0, err + out3)
        docs, _ = intents.scan(w.repo)
        about = [d["key"] for d in docs if d.get("source") == "eval-gap" and d["title"].startswith("P-PAY-1-1 ")]
        self.assertEqual(len(about), 1, about)

    def test_n8_file_gaps_skips_what_is_already_filed(self):
        gs = [EV._gap("knowledge", "withdraw|P-PAY-1-1", "P-PAY-1-1 装载过、从没被引用：撤回候选", "装载 4 次", ["P-PAY-1-1"])]
        calls, out = [], []
        filed = [{"key": "PAY-7", "source": "eval-gap", "title": gs[0]["title"], "created_at": "2026-11-01T00:00:00Z"}]
        bad = eval_cmd.file_gaps("/nonexistent", {}, gs, lambda *a, **k: calls.append(a), out=out.append,
                                 existing=filed)
        self.assertEqual((bad, calls), (0, []))
        self.assertEqual(out, ["already filed PAY-7: " + gs[0]["title"]])

    def test_n16_corrected_local_rejection_is_not_a_gap(self):
        """N16: a local Lean rejection (no voucher, no bridge decision) stayed a knowledge gap even with a
        correction citing the decisions.md row that records it."""
        rows = [["D-1", "2026-11-02", "requirements", "单号按 T-PAY-1-1", "样例", "agent"],
                ["D-2", "2026-11-02", "requirements", "产品驳回了需求：退款不能部分退，已改", "人驳回", "agent"]]
        reject = ((1, 20), "REJECTED", {"kind": "REQUIREMENTS", "sha256": "ab" * 32,
                                        "words": "reject REQUIREMENTS 部分退款不行"})
        correction = "\n```brain-proposal\nfile: PRODUCT\ntype: correction\nrel: new\nsrc: D-2\n---\n退款不能部分退。\n```\n"
        w = World(self)
        r = w.run("PAY-1", "refund", 2, decisions=rows, local_events=[reject])
        p = r["docs"] + "decisions.md"
        with open(os.path.join(w.repo, p), encoding="utf-8") as f:
            text = f.read()
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        d = EV.collect(w.repo, w.cfg(), as_of=AS_OF)
        self.assertEqual([g["kind"] for g in EV.gaps(d) if "被驳回" in g["title"]], ["knowledge"])  # no correction
        w2 = World(self)                                                   # the same run with the correction
        r = w2.run("PAY-1", "refund", 2, decisions=rows, local_events=[reject])
        w2.commit({p: text + correction}, "PAY-1: correction", "2026-11-02T08:00:00Z", BOT)
        w2.merge(r, "merge", date="2026-11-03T00:00:00Z")
        d = EV.collect(w2.repo, w2.cfg(), as_of=AS_OF)
        self.assertEqual(d["traces"][0]["brain"]["proposals"]["correction_src"], ["D-2"])
        self.assertEqual([g["title"] for g in EV.gaps(d) if g["kind"] == "knowledge"], [])

    def test_n17_eval_gaps_report_the_rejected_item_reflect_warns_about(self):
        """N17: a batch review rejected D-1 and A-1; a correction for A-1 only 'covered' the whole rejection."""
        from brainkit import reflect
        v_rej, v_ok = vid("reject"), vid("approve")
        rows = [["D-1", "2026-11-02", "code", "退款按自然日计", "需求没写", "agent"],
                ["D-2", "2026-11-02", "requirements", "单号按 T-PAY-1-1", "样例", "agent"]]
        events = [((8, 0), "REJECTED", {"kind": "RELEASE", "sha256": "ef" * 32,
                                        "words": "reject RELEASE efefefef (via gitlab P-ABCD %s)" % v_rej}),
                  release("APPROVED", 9, v_ok)]
        w = World(self)
        r = w.run("PAY-1", "refund", 2, decisions=rows, runner_events=events)
        p = r["docs"] + "decisions.md"
        with open(os.path.join(w.repo, p), encoding="utf-8") as f:
            text = f.read() + ("\n```brain-proposal\nfile: PRODUCT\ntype: correction\nrel: new\nsrc: A-1, %s\n---\n"
                               "旧订单也能退。\n```\n" % v_rej)
        w.commit({p: text}, "PAY-1: correction for A-1", "2026-11-02T08:30:00Z", BOT)
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        run = "RUN-PAY-1-1"
        common = {"schema": 1, "kind": "answer", "run_id": run, "decision_kind": "acceptance", "answerer": "P-ABCD",
                  "lean_request": "RELEASE efefefef", "review_items": ["D-1", "A-1"]}
        w.voucher(run, dict(common, decision="D-5", choice="reject", issued_at="2026-11-02T08:00:00Z",
                            voucher_id=v_rej, rejected_items=["D-1", "A-1"]))
        w.voucher(run, dict(common, decision="D-6", choice="approve", issued_at="2026-11-02T09:00:00Z",
                            voucher_id=v_ok))
        warned = reflect.rejected_without_correction(w.vouchers, "PAY-1", reflect.parse(text)[0])
        self.assertTrue(any("D-1" in x for x in warned))
        d = EV.collect(w.repo, w.cfg(), as_of=AS_OF, vouchers_dir=w.vouchers)
        rej = [h for h in d["traces"][0]["humans"] if h["kind"] == "rejection"]
        self.assertEqual([h["rejected_items"] for h in rej], [["D-1", "A-1"]])
        know = [g["title"] for g in EV.gaps(d) if g["kind"] == "knowledge"]
        self.assertEqual([g for g in know if "被驳回" in g], ["PAY-1：D-1（D-5 里）被驳回，但没有写成 correction"])


class BadInput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = World(_Keep())
        for n, day in enumerate((2, 4), 1):
            r = cls.template.run("PAY-%d" % n, "s%d" % n, day)
            cls.template.merge(r, "merge", date="2026-11-%02dT12:00:00Z" % (day + 1))

    def setUp(self):
        self.w = self.template.copy(self)                  # two merged runs, the test's own copy

    def test_n25_eval_survives_a_run_json_with_conflict_markers(self):
        """N25: one malformed run.json aborted the whole `brain eval` / `eval gaps`."""
        self.w.commit({"runs/PAY-3/run.json": "<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> x\n"}, "bad",
                      "2026-11-07T00:00:00Z")
        env = {"BRAIN_INTAKE_DIR": os.path.join(self.w.d, "no-intake")}
        code, out, err = run_cli(self.w.repo, ["eval"], env=env)
        self.assertEqual(code, 0, err)
        self.assertIn("PAY-3", out)
        code, out, err = run_cli(self.w.repo, ["eval", "--json"], env=env)
        self.assertEqual(json.loads(out)["skipped"], [{"key": "PAY-3", "why": "runs/PAY-3/run.json is not valid JSON"}])
        self.assertEqual(run_cli(self.w.repo, ["eval", "gaps"], env=env)[0], 0)

    def test_n25_odd_field_types_are_not_a_traceback(self):
        """N25: a run.json whose branch is not a string crashed `brain trace` with a TypeError."""
        self.w.commit({"runs/PAY-3/run.json": json.dumps({"key": "PAY-3", "intent": "261107-pay-3-x",
                                                          "branch": ["ai/alice/PAY-3-x"], "brain_commit": 7,
                                                          "handoff": {"requested_at": 3, "by": ["x"]}})},
                      "odd", "2026-11-07T00:00:00Z")
        t = trace.build(self.w.repo, self.w.cfg(), "PAY-3", as_of=AS_OF)
        self.assertEqual((t["branch"], t["brain_commit"]), (None, None))
        self.assertTrue(any("branch" in m for m in t["missing"]), t["missing"])
        code, out, err = run_cli(self.w.repo, ["trace", "PAY-3"])
        self.assertEqual(code, 0, err)


class NoDeprecationWarnings(unittest.TestCase):
    def test_m1_trace_and_eval_raise_no_deprecation_warning(self):
        """M1: Python 3.12 with -W error must not fail on a DeprecationWarning (datetime.utcnow() and the like)."""
        w, r = base_run(self)
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            t = trace.build(w.repo, w.cfg(), "PAY-1")
            trace.render(t)
            d = EV.collect(w.repo, w.cfg())
            EV.report(d)
            EV.gaps(d)


class WhoOwnerIsAPerson(unittest.TestCase):
    """The local half's steering lets the owner's chat answers be recorded as Who = owner."""

    def test_owner(self):
        from brainkit import trace
        self.assertEqual(trace.classify_who("owner"), ("person", "owner"))
        self.assertEqual(trace.classify_who("Owner (chat)"), ("person", "owner"))
        self.assertEqual(trace.classify_who("owner, carol")[0], "unclassified")


if __name__ == "__main__":
    unittest.main()
