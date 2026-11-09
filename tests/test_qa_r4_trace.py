"""QA round 4 on the Run Trace and Eval (brainkit/trace.py, brainkit/evaluate.py, `brain trace`, `brain eval`),
ported from the reviewer's qa_r4 suite (#30 ... #33), and one test per round-4 minor fixed in those files. Each test
names its finding or minor; the docstring says what was wrong. Everything is local: a bare origin, a work clone and
an eval clone in a temp dir, a sqlite ledger and voucher files on disk, a throwaway HOME; no network."""
import hashlib
import json
import os
import re
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brainkit import evaluate as EV, trace  # noqa: E402
from brainkit.bridge.decisions import SCHEMA as DSCHEMA  # noqa: E402
from brainkit.bridge.ledger import SCHEMA as LSCHEMA  # noqa: E402
from tests.helpers import git_repo, sh, tmpdir  # noqa: E402
from tests.test_qa_trace_eval import KIT, _Keep, audit, run_cli, table  # noqa: E402

BOT = "brain-bot@noreply.invalid"
ALICE, LEAD, BOB = "alice@example.com", "lead@example.com", "bob@example.com"
NAMES = {BOT: "brain-bot", ALICE: "alice", LEAD: "lead", BOB: "bob"}
AS_OF = "2026-11-30T12:00:00Z"
PROJECT = "grp/pay"
PSEUD = {"carol": "P-C4C4", "dave": "P-D4D4"}


def vid(seed):
    return "V-" + hashlib.sha256(str(seed).encode()).hexdigest()[:10]


def who(email, date):
    n = NAMES[email]
    return {"GIT_AUTHOR_NAME": n, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": n, "GIT_COMMITTER_EMAIL": email,
            "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}


def home_dir(owner):
    """A throwaway HOME (never the real ~/.brain or ~/.kiro), with a git identity."""
    home = tmpdir(owner)
    with open(os.path.join(home, ".gitconfig"), "w", encoding="utf-8") as f:
        f.write("[user]\n\tname = QA\n\temail = qa@example.com\n")
    return home


def temp_home(test):
    p = mock.patch.dict(os.environ, {"HOME": home_dir(test)})
    p.start()
    test.addCleanup(p.stop)


def _no_hooks(repo_git_dir, d):
    """As tests.helpers.no_hooks_config, for the bare origin: this throwaway repo runs no machine-wide hooks."""
    empty = os.path.join(d, "no-hooks")
    os.makedirs(empty, exist_ok=True)
    with open(os.path.join(repo_git_dir, "config"), "a", encoding="utf-8") as f:
        f.write("[core]\n\thooksPath = %s\n[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n" % empty)


class Batch:
    """The reviewer's r4_batch.Batch: a bare origin, a work clone where the owner, the runner and "GitLab" commit,
    an eval clone that fetches; a bridge ledger (sqlite), voucher files, a KiroCrew credits file."""

    def __init__(self, d):
        self.d = d
        self.origin = os.path.join(d, "origin.git")
        sh("git", "init", "-q", "--bare", "-b", "main", self.origin)
        _no_hooks(self.origin, d)
        self.repo = git_repo(os.path.join(d, "work"))
        self.git("remote", "add", "origin", self.origin)
        self.eval_repo = None
        self.vouchers = os.path.join(d, "vouchers")
        self.ledger_path = os.path.join(d, "ledger.sqlite")
        self.db = sqlite3.connect(self.ledger_path, isolation_level=None)
        self.db.executescript(LSCHEMA + DSCHEMA)
        self.dn = 0
        self.runs = {}
        self.commit({".brain/kit.json": json.dumps(KIT), "brain/TECH.md": "# TECH\n", "brain/PRODUCT.md": "# PRODUCT\n",
                     "README.md": "x\n", "src/app.py": "app = 1\n"}, "init", "2026-10-01T00:00:00Z", LEAD)
        self.push("main")

    # ---------- git ----------
    def git(self, *args, env=None, repo=None):
        return sh("git", "-C", repo or self.repo, *args, env=env)

    def rev(self, r="HEAD", repo=None):
        return self.git("rev-parse", r, repo=repo).stdout.strip()

    def commit(self, files, msg, date, email=ALICE):
        for rel, text in files.items():
            p = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg, env=who(email, date))
        return self.rev()

    def push(self, *refs, force=False):
        self.git("push", "-q", *(["-f"] if force else []), "origin", *refs)

    def make_eval_clone(self):
        """The team's eval machine: a clone made now (fetches later without --prune)."""
        self.eval_repo = git_repo(os.path.join(self.d, "eval"))
        self.git("remote", "add", "origin", self.origin, repo=self.eval_repo)
        self.fetch()
        self.git("checkout", "-q", "-B", "main", "origin/main", repo=self.eval_repo)

    def fetch(self):
        self.git("fetch", "-q", "origin", repo=self.eval_repo)

    # ---------- the bridge ----------
    def sql(self, q, args=()):
        self.db.execute(q, args)

    def voucher(self, run_id, doc):
        p = os.path.join(self.vouchers, run_id, doc["voucher_id"] + ".json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"voucher": doc, "sig": "x"}, f)

    def accept(self, r, at, state="dispatched", updated=None):
        """The bridge accepted the hand-over at commit r['handoff']: hand-over check, run row, hand-over voucher."""
        run_id = "RUN-%s-1" % r["key"]
        v = vid("handoff " + r["key"])
        key = "%s#%d" % (r["handoff"], r["iid"] * 10)
        self.sql("INSERT INTO handoffs(project,iid,sha,state,run_id,voucher_id,reason,created,updated) VALUES "
                 "(?,?,?,?,?,?,?,?,?)", (PROJECT, r["iid"], key, "accepted", run_id, v, None, at, at))
        self.sql("INSERT INTO runs(run_id,project,req,iid,sha,voucher_id,state,owner,created,updated) VALUES "
                 "(?,?,?,?,?,?,?,?,?,?)", (run_id, PROJECT, r["key"], r["iid"], r["handoff"], v, state, "alice", at,
                                           updated or at))
        self.voucher(run_id, {"schema": 1, "kind": "handoff", "run_id": run_id, "project": PROJECT,
                              "mr_iid": r["iid"], "sha": r["handoff"], "branch": r["branch"], "owner": "alice",
                              "key": r["key"], "intent": r["intent"], "issued_at": at, "voucher_id": v})
        r["run_id"] = run_id
        return run_id

    def reject_handoff(self, r, at, reason):
        key = "%s#%d" % (r["handoff"], r["iid"] * 10)
        self.sql("INSERT INTO handoffs(project,iid,sha,state,run_id,voucher_id,reason,created,updated) VALUES "
                 "(?,?,?,?,?,?,?,?,?)", (PROJECT, r["iid"], key, "rejected", None, None, reason, at, at))

    def set_run(self, r, state, at):
        self.sql("UPDATE runs SET state=?, updated=? WHERE run_id=?", (state, at, r["run_id"]))

    def question(self, r, kind, lean_request, asked, answer=None, answerer=None, answered=None, items=None,
                 state=None):
        """A bridge decision on the run's MR at its current head; answered: a delivered answer + its voucher
        (state: another state for it, e.g. refused: the signer would not sign it, so there is no voucher)."""
        self.dn += 1
        did = "D-%d" % self.dn
        state = state or ("delivered" if answer else "open")
        v = vid("answer " + did) if answer and state != "refused" else None
        self.sql("INSERT INTO decisions(id,run_id,project,iid,sha,park_hash,kind,lean_request,state,answer,answerer,"
                 "voucher_id,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (did, r["run_id"], PROJECT, r["iid"], self.rev(), "ph-" + did, kind, lean_request,
                  state, answer, answerer, v, asked, answered or asked))
        if v:
            doc = {"schema": 1, "kind": "answer", "decision": did, "run_id": r["run_id"], "project": PROJECT,
                   "mr_iid": r["iid"], "sha": self.rev(), "park_hash": "ph-" + did, "decision_kind": kind,
                   "choice": answer, "answerer": PSEUD[answerer], "issued_at": answered, "voucher_id": v}
            if lean_request:
                doc["lean_request"] = lean_request
            if items:
                doc["rejected_items"] = items
            self.voucher(r["run_id"], doc)
        return did, v

    # ---------- a run ----------
    def start(self, key, slug, day, iid, month=11):
        intent = "26%02d%02d-%s-%s" % (month, day, key.lower(), slug)
        br = "ai/alice/%s-%s" % (key, slug)
        self.git("switch", "-q", "-c", br, "main")
        d = "2026-%02d-%02d" % (month, day)
        rec = {"schema": 1, "key": key, "tracker": "none", "owner": "alice", "branch": br, "intent": intent,
               "brain_commit": self.rev("main"), "kit_version": "0.1.0", "origin": "local", "expected_paths": [],
               "materials": [], "created_at": d + "T01:00:00Z", "claim_checked": True, "handoff": None}
        docs = "aidlc-docs/%s/" % intent
        local = [(d + "T01:00:00.000Z", "INTENT_CREATED", intent, {"title": key}),
                 (d + "T01:01:00.000Z", "STAGE_STARTED", intent, {"stage": "requirements", "unit": None}),
                 (d + "T01:30:00.000Z", "STAGE_COMPLETED", intent, {"stage": "requirements", "unit": None,
                                                                    "note": "AC-1"}),
                 (d + "T01:40:00.000Z", "APPROVED", intent, {"kind": "REQUIREMENTS", "sha256": "ab" * 32,
                                                             "words": "approve REQUIREMENTS abababab"})]
        r = {"key": key, "slug": slug, "intent": intent, "branch": br, "docs": docs, "rec": rec, "iid": iid,
             "day": d, "rows": [["D-1", d, "requirements", "%s 按最常见的做法" % key, "需求没写", "agent"]],
             "runner": []}
        r["start"] = self.commit({"runs/%s/run.json" % key: json.dumps(rec),
                                  docs + "decisions.md": "# Decisions\n\n" + table(r["rows"]),
                                  docs + "inception/requirements/requirements.md":
                                      "# Requirements\n\n## Assumptions\n- A-1: 只处理新订单\n",
                                  "aidlc-docs/audit/local-%s.jsonl" % key: audit(local, ALICE)},
                                 "%s: start run" % key, d + "T02:00:00Z")
        self.runs[key] = r
        return r

    def handoff(self, r, at):
        r["rec"]["handoff"] = {"requested_at": at, "by": "alice", "local_head": self.rev()}
        r["handoff"] = self.commit({"runs/%s/run.json" % r["key"]: json.dumps(r["rec"])},
                                   "%s: hand off to team runner" % r["key"], at)
        self.push(r["branch"])
        return r["handoff"]

    def bot(self, r, at, files=None, msg=None, events=(), park=None, rows=()):
        """A runner commit (bot identity): files, Lean events (runner audit), park.json, decisions.md rows."""
        self.git("switch", "-q", r["branch"])
        files = dict(files or {})
        if events:
            r["runner"] += list(events)
            files["aidlc-docs/audit/runner-%s.jsonl" % r["key"]] = audit(r["runner"], BOT, host="runner")
        if park is not None:
            files["runs/%s/park.json" % r["key"]] = json.dumps(park)
        if rows:
            r["rows"] += list(rows)
            files[r["docs"] + "decisions.md"] = "# Decisions\n\n" + table(r["rows"])
        sha = self.commit(files, msg or "%s: work" % r["key"], at, BOT)
        self.push(r["branch"])
        return sha

    def accept_cycle(self, r, park_at, asked, answered, answerer, choice, release_hash, done_at, items=None):
        """lean release + request RELEASE, park for acceptance, the bridge's answer, the runner records it."""
        lr = "RELEASE %s" % release_hash
        park = {"schema": 1, "status": "waiting", "kind": "acceptance", "ask": "product", "options": [],
                "question": "请验收 %s（%s）" % (r["key"], release_hash), "lean_request": lr}
        self.bot(r, park_at, msg="%s: ready for acceptance" % r["key"], park=park,
                 files={r["docs"] + "operations/release.md": "# Release %s\n" % release_hash})
        did, v = self.question(r, "acceptance", lr, asked, answer=choice, answerer=answerer, answered=answered,
                               items=items)
        verb = "approve" if choice == "approve" else "reject"
        words = "%s %s (via gitlab %s %s)" % (verb, lr, PSEUD[answerer], v)
        ev = [(done_at.replace("Z", ".000Z"), "APPROVED" if choice == "approve" else "REJECTED", r["intent"],
               dict({"kind": "RELEASE", "sha256": release_hash * 8, "words": words},
                    **({"claimed_channel": "gitlab %s %s" % (PSEUD[answerer], v)} if choice == "approve" else {})))]
        if choice == "approve":
            ev.append((done_at.replace("Z", ".001Z"), "STAGE_COMPLETED", r["intent"],
                       {"stage": "release", "unit": None, "note": "RELEASE approved"}))
        self.bot(r, done_at, msg="%s: acceptance %s" % (r["key"], "approved" if choice == "approve" else "rejected"),
                 events=ev, park=dict(park, status="answered"),
                 rows=[["D-%d" % (len(r["rows"]) + 1), done_at[:10], "release", "验收 %s" % verb,
                        "桥上回答 %s" % did, "%s %s" % (PSEUD[answerer], v)]])
        return did, v

    # ---------- GitLab ----------
    def merge_commit(self, r, at):
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--no-ff", r["branch"], "-m", "Merge branch '%s' into 'main'" % r["branch"],
                 "-m", "%s %s" % (r["key"], r["slug"]), "-m", "See merge request %s!%d" % (PROJECT, r["iid"]),
                 env=who(LEAD, at))
        self.push("main")
        self.push(":" + r["branch"])                    # GitLab deletes the source branch
        return self.rev("main")

    def rebase_ff(self, r, at, delete_branch):
        """GitLab's rebase + fast-forward: the source branch is rebased onto main (committer: the merger)."""
        self.git("switch", "-q", r["branch"])
        self.git("rebase", "-q", "main", env={"GIT_COMMITTER_DATE": at, "GIT_COMMITTER_EMAIL": LEAD,
                                              "GIT_COMMITTER_NAME": "lead"})
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--ff-only", r["branch"])
        self.push("main")
        if delete_branch:
            self.push(":" + r["branch"])
        else:
            self.push(r["branch"], force=True)
        return self.rev("main")

    def squash(self, r, at):
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--squash", r["branch"], env=who(LEAD, at))
        self.git("commit", "-q", "-m", "%s: %s" % (r["key"], r["slug"]), env=who(LEAD, at))
        self.push("main")
        return self.rev("main")

    def on_main(self, files, msg, at, email=LEAD):
        self.git("switch", "-q", "main")
        sha = self.commit(files, msg, at, email)
        self.push("main")
        return sha


def build(d):
    """The reviewer's batch of 9 requirements (r4_batch.build), made under d:
      PAY-1 refund   GitLab merge commit; a clarify answer on the bridge; acceptance approved first time; a
                     records-only "fix typo" after the merge (A2)
      PAY-2 fees     rebased onto main and fast-forwarded (branch kept); acceptance approved first time
      PAY-3 export   rebased, branch deleted (the eval clone keeps the stale ref); acceptance rejected, then approved
      PAY-4 invoice  squash (branch kept); bob pushed after the hand-over; a hotfix 2 days after the merge
      PAY-5 report   open: the runner's last push 2 h before --as-of
      PAY-6 audit    stuck: parked on an open clarify question 98 h before --as-of; a KiroCrew manual restart
      PAY-7 notify   the bridge refused the hand-over (H2); no run
      PAY-8 sync     the runner failed (ledger 'failed'); no runner commit
      PAY-9 legacy   the MR was closed, not merged (ledger 'closed')"""
    b = Batch(d)
    code = lambda r: {"src/%s.py" % r["slug"]: "%s = 1\n" % r["slug"]}  # noqa: E731

    def runner_start(r, at):
        b.bot(r, at, files=code(r), msg="%s: code" % r["key"],
              events=[(at.replace("Z", ".000Z"), "SESSION_START", r["intent"], {"session": "k-" + r["key"]}),
                      (at.replace("Z", ".500Z"), "VERIFY", r["intent"], {"ok": True, "fingerprint": "f2f2f2f2",
                                                                         "results": [{"cmd": "make test", "exit": 0}]})])

    p1 = b.start("PAY-1", "refund", 2, 11)
    b.handoff(p1, "2026-11-02T03:00:00Z")
    b.accept(p1, "2026-11-02T03:05:00Z")
    runner_start(p1, "2026-11-02T04:00:00Z")
    q = {"schema": 1, "status": "waiting", "kind": "clarify", "ask": "owner", "question": "退款按自然日还是交易日？",
         "options": [{"id": "A", "text": "自然日"}, {"id": "B", "text": "交易日"}]}
    b.bot(p1, "2026-11-02T05:00:00Z", msg="PAY-1: ask", park=q)
    did, v = b.question(p1, "clarify", None, "2026-11-02T05:10:00Z", answer="A", answerer="carol",
                        answered="2026-11-02T06:00:00Z")
    b.bot(p1, "2026-11-02T07:00:00Z", msg="PAY-1: answered", park=dict(q, status="answered"),
          files={"src/refund.py": "days = 'natural'\n"},
          rows=[["D-2", "2026-11-02", "requirements", "按自然日", "桥上回答 %s" % did, "%s %s" % (PSEUD["carol"], v)]])
    b.accept_cycle(p1, "2026-11-02T08:00:00Z", "2026-11-02T08:10:00Z", "2026-11-02T09:00:00Z", "dave", "approve",
                   "a1a1a1a1", "2026-11-02T10:00:00Z")
    b.git("switch", "-q", "main")
    b.make_eval_clone()
    b.git("switch", "-q", "main")

    p2 = b.start("PAY-2", "fees", 3, 12)
    b.handoff(p2, "2026-11-03T03:00:00Z")
    b.accept(p2, "2026-11-03T03:05:00Z")
    runner_start(p2, "2026-11-03T04:00:00Z")
    b.accept_cycle(p2, "2026-11-03T08:00:00Z", "2026-11-03T08:10:00Z", "2026-11-03T09:00:00Z", "dave", "approve",
                   "b2b2b2b2", "2026-11-03T10:00:00Z")
    b.git("switch", "-q", "main")

    p3 = b.start("PAY-3", "export", 4, 13)
    b.handoff(p3, "2026-11-04T03:00:00Z")
    b.accept(p3, "2026-11-04T03:05:00Z")
    runner_start(p3, "2026-11-04T04:00:00Z")
    b.accept_cycle(p3, "2026-11-04T08:00:00Z", "2026-11-04T08:10:00Z", "2026-11-04T09:00:00Z", "dave", "reject",
                   "c3c3c3c3", "2026-11-04T10:00:00Z")
    b.bot(p3, "2026-11-04T12:00:00Z", files={"src/export.py": "export = 2\n"}, msg="PAY-3: rework after review")
    b.accept_cycle(p3, "2026-11-05T08:00:00Z", "2026-11-05T08:10:00Z", "2026-11-05T09:00:00Z", "dave", "approve",
                   "c4c4c4c4", "2026-11-05T10:00:00Z")
    b.git("switch", "-q", "main")

    p4 = b.start("PAY-4", "invoice", 5, 14)
    b.handoff(p4, "2026-11-05T03:00:00Z")
    b.accept(p4, "2026-11-05T03:05:00Z")
    runner_start(p4, "2026-11-05T04:00:00Z")
    b.git("switch", "-q", p4["branch"])
    p4["bob"] = b.commit({"src/invoice.py": "invoice = 2\n"}, "PAY-4: tweak invoice copy", "2026-11-05T06:00:00Z", BOB)
    b.push(p4["branch"])
    b.accept_cycle(p4, "2026-11-05T08:00:00Z", "2026-11-05T08:10:00Z", "2026-11-05T09:00:00Z", "dave", "approve",
                   "d4d4d4d4", "2026-11-05T10:00:00Z")
    b.git("switch", "-q", "main")
    b.fetch()

    p1["merge"] = b.merge_commit(p1, "2026-11-05T00:00:00Z")
    b.set_run(p1, "merged", "2026-11-05T00:05:00Z")
    b.on_main({"runs/PAY-1/notes.md": "typo fixed\n"}, "PAY-1: fix typo in run notes", "2026-11-06T00:00:00Z")
    p2["merge"] = b.rebase_ff(p2, "2026-11-07T00:00:00Z", delete_branch=False)
    b.set_run(p2, "merged", "2026-11-07T00:05:00Z")
    p3["merge"] = b.rebase_ff(p3, "2026-11-08T00:00:00Z", delete_branch=True)
    b.set_run(p3, "merged", "2026-11-08T00:05:00Z")
    p4["merge"] = b.squash(p4, "2026-11-10T00:00:00Z")
    b.set_run(p4, "merged", "2026-11-10T00:05:00Z")
    b.on_main({"src/invoice.py": "invoice = 3  # rounding\n"}, "hotfix PAY-4: rounding of invoice totals",
              "2026-11-12T00:00:00Z")

    p9 = b.start("PAY-9", "legacy", 14, 19)
    b.handoff(p9, "2026-11-15T03:00:00Z")
    b.accept(p9, "2026-11-15T03:05:00Z")
    runner_start(p9, "2026-11-15T05:00:00Z")
    b.bot(p9, "2026-11-16T07:00:00Z", files={"src/legacy.py": "legacy = 2\n"}, msg="PAY-9: more")
    b.set_run(p9, "closed", "2026-11-17T00:05:00Z")
    b.git("switch", "-q", "main")

    p7 = b.start("PAY-7", "notify", 20, 17)
    b.handoff(p7, "2026-11-20T03:00:00Z")
    b.reject_handoff(p7, "2026-11-20T03:05:00Z", "label 'ai-handoff' was added by bob, not by the branch owner alice")
    b.git("switch", "-q", "main")

    p8 = b.start("PAY-8", "sync", 21, 18)
    b.handoff(p8, "2026-11-21T03:00:00Z")
    b.accept(p8, "2026-11-21T03:05:00Z", state="failed", updated="2026-11-21T03:20:00Z")
    b.git("switch", "-q", "main")

    p6 = b.start("PAY-6", "audit", 24, 16)
    b.handoff(p6, "2026-11-25T03:00:00Z")
    b.accept(p6, "2026-11-25T03:05:00Z")
    runner_start(p6, "2026-11-25T05:00:00Z")
    q6 = {"schema": 1, "status": "waiting", "kind": "clarify", "ask": "owner", "question": "审计日志保留多久？",
          "options": [{"id": "A", "text": "90 天"}, {"id": "B", "text": "1 年"}]}
    b.bot(p6, "2026-11-26T10:00:00Z", msg="PAY-6: ask", park=q6)
    b.question(p6, "clarify", None, "2026-11-26T10:05:00Z")
    b.git("switch", "-q", "main")

    p5 = b.start("PAY-5", "report", 27, 15)
    b.handoff(p5, "2026-11-28T03:00:00Z")
    b.accept(p5, "2026-11-28T03:05:00Z")
    runner_start(p5, "2026-11-29T10:00:00Z")
    b.bot(p5, "2026-11-30T10:00:00Z", files={"src/report.py": "report = 2\n"}, msg="PAY-5: more")
    b.git("switch", "-q", "main")

    return finish(b, credits={"runs": {"RUN-PAY-1-1": 10, "RUN-PAY-2-1": 8, "RUN-PAY-3-1": 30, "RUN-PAY-4-1": 12,
                                       "RUN-PAY-5-1": 4, "RUN-PAY-6-1": {"credits": 6, "manual": [
                                           {"at": "2026-11-27T09:00:00Z", "kind": "restart"}]}, "RUN-PAY-9-1": 5}})


def finish(b, credits=None, prune=False):
    """The eval clone fetches (with --prune when asked; PAY-3's deleted branch stays otherwise), the ledger is
    closed, the credits file written."""
    b.git("fetch", "-q", *(["--prune"] if prune else []), "origin", repo=b.eval_repo)
    b.git("merge", "-q", "--ff-only", "origin/main", repo=b.eval_repo)
    b.db.close()
    b.credits_path = os.path.join(b.d, "credits.json")
    with open(b.credits_path, "w", encoding="utf-8") as f:
        json.dump(credits or {"runs": {}}, f)
    return b


def eval_args(b, as_of=AS_OF):
    return ["--ref", "origin/main", "--ledger", b.ledger_path, "--vouchers", b.vouchers, "--credits", b.credits_path,
            "--as-of", as_of]


def eval_json(b, *extra, as_of=AS_OF):
    """`brain eval --json --ref origin/main --ledger --vouchers --credits --as-of` in the eval clone (in-process)."""
    code, out, err = run_cli(b.eval_repo, ["eval", "--json"] + eval_args(b, as_of) + list(extra))
    if code != 0:
        raise AssertionError("brain eval failed (%s): %s %s" % (code, out[-2000:], err[-2000:]))
    return json.loads(out)


def eval_text(b, *extra, as_of=AS_OF):
    code, out, err = run_cli(b.eval_repo, ["eval"] + eval_args(b, as_of) + list(extra))
    if code != 0:
        raise AssertionError("brain eval failed (%s): %s %s" % (code, out[-2000:], err[-2000:]))
    return out


def headline(text):
    return next(x for x in text.splitlines() if x.startswith("这一批"))


# ---------- #30: a run that failed on the runner, or whose MR was closed, is never autonomous ----------

class FailedAndClosedRunsAreNotAutonomous(unittest.TestCase):
    """#30 (MAJOR): in the 9-run batch, the runner-failed PAY-8 (ledger 'failed', no runner commit) and the closed
    PAY-9 (ledger 'closed', not merged) showed 自主 = 是 and the headline said "交接给运行机的 8 个里，4 个交接后除了
    必经的审批没人插手"; by hand it is 2 (PAY-2, PAY-5). Rule E5: a hand-over run that ended without delivering (the
    runner failed, the MR was closed) is never autonomous (`beyond` says why: runner_failed, closed_unmerged)."""

    @classmethod
    def setUpClass(cls):
        keep = _Keep()
        p = mock.patch.dict(os.environ, {"HOME": home_dir(keep)})
        p.start()
        cls.addClassCleanup(p.stop)
        cls.b = build(tmpdir(keep))
        cls.data = eval_json(cls.b)
        cls.text = EV.report(cls.data)      # what `brain eval` prints for this data (one eval of 9 runs is enough)

    def test_30_failed_and_closed_rows_and_the_headline(self):
        rows = {r["key"]: r for r in self.data["batch"]}
        self.assertEqual((rows["PAY-8"]["state"], rows["PAY-9"]["state"]), ("failed", "closed"))   # the setup holds
        m = re.search(r"交接给运行机的 (\d+) 个里，(\d+) 个交接后除了必经的审批没人插手", headline(self.text))
        self.assertEqual(({k: (rows[k]["autonomous"], rows[k]["beyond"]) for k in ("PAY-8", "PAY-9")},
                          (int(m.group(1)), int(m.group(2))) if m else headline(self.text)),
                         ({"PAY-8": (False, ["runner_failed"]), "PAY-9": (False, ["closed_unmerged"])}, (8, 2)))
        self.assertEqual(sorted(k for k, r in rows.items() if r["autonomous"] is True), ["PAY-2", "PAY-5"])
        self.assertIn("| PAY-8 | 运行机失败 | 否（runner_failed） |", self.text)
        self.assertIn("| PAY-9 | MR 关了（没合并） | 否（closed_unmerged） |", self.text)

    def test_30_the_rest_of_the_batch_by_hand(self):
        """The hand count of the reviewer's batch (and of the minors fixed with it): who did what after the hand-over,
        what each delivery is, what waits on whom."""
        rows = {r["key"]: (r["state"], r["autonomous"], r["judgment"], r["delivery"]) for r in self.data["batch"]}
        self.assertEqual(rows, {"PAY-1": ("merged", False, [3, 0], "correct"),
                                "PAY-2": ("merged", True, [2, 0], "correct"),
                                "PAY-3": ("merged", False, [3, 0], "wrong"),
                                "PAY-4": ("merged", False, [2, 1], "wrong"),
                                "PAY-5": ("open", True, [1, 0], "open"),
                                "PAY-6": ("waiting", False, [1, 1], "open"),
                                "PAY-7": ("open", None, [1, 0], "open"),
                                "PAY-8": ("failed", False, [1, 0], "open"),
                                "PAY-9": ("closed", False, [1, 0], "closed")})
        self.assertEqual(self.data["autonomy"]["value"], [1, 4])
        stuck = {q["key"]: [r["text"] for r in q["reasons"] if r["rule"] == "stuck"] for q in self.data["review_queue"]}
        self.assertIn("在等 owner 回答 D-7：clarify", stuck["PAY-6"][0])          # minor: who is awaited, for what


# ---------- #31: an answer the bridge delivered is not awaited ----------

def world_31(d):
    """Two runs parked on a question the bridge answered and delivered; the runner never pushed again."""
    b = Batch(d)
    b.make_eval_clone()
    p = b.start("PAY-6", "audit", 24, 16)
    b.handoff(p, "2026-11-25T03:00:00Z")
    b.accept(p, "2026-11-25T03:05:00Z")
    b.bot(p, "2026-11-25T05:00:00Z", files={"src/audit.py": "a = 1\n"}, msg="PAY-6: code")
    lr = "RELEASE f6f6f6f6"
    b.bot(p, "2026-11-26T08:00:00Z", msg="PAY-6: ready for acceptance",
          park={"schema": 1, "status": "waiting", "kind": "acceptance", "ask": "product", "options": [],
                "question": "请验收 PAY-6", "lean_request": lr})
    b.question(p, "acceptance", lr, "2026-11-26T08:10:00Z", answer="approve", answerer="dave",
               answered="2026-11-26T09:00:00Z")
    b.git("switch", "-q", "main")
    q = b.start("PAY-5", "report", 24, 15)
    b.handoff(q, "2026-11-25T03:00:00Z")
    b.accept(q, "2026-11-25T03:05:00Z")
    b.bot(q, "2026-11-25T05:00:00Z", files={"src/report.py": "r = 1\n"}, msg="PAY-5: code")
    b.bot(q, "2026-11-26T08:00:00Z", msg="PAY-5: ask",
          park={"schema": 1, "status": "waiting", "kind": "clarify", "ask": "owner", "question": "按月还是按周？",
                "options": [{"id": "A", "text": "月"}, {"id": "B", "text": "周"}]})
    b.question(q, "clarify", None, "2026-11-26T08:10:00Z", answer="A", answerer="carol",
               answered="2026-11-26T09:00:00Z")
    b.git("switch", "-q", "main")
    return finish(b)


class DeliveredAnswerIsNotAwaited(unittest.TestCase):
    """#31 (MAJOR): an answer the bridge already DELIVERED to the run read as "在等回答" (state waiting: park.json at
    the tip is still 'waiting'), and the stuck reason sent the owner after the answerer, when it is the runner that
    did not go on. Rule E2: such a run has its own state, `answered` (有了回答、run 没接着做; outcome.answered names
    the decision; outcome.waiting_on is null: nobody is waited on); E3: stuck, it is in the review queue saying the
    answer reached the run and the run did not go on."""

    def setUp(self):
        temp_home(self)
        self.b = world_31(tmpdir(self))

    def test_31_state_and_stuck_reason_after_a_delivered_answer(self):
        data = eval_json(self.b)
        t = {x["key"]: x for x in data["traces"]}
        self.assertEqual((t["PAY-6"]["outcome"]["accepted"]["value"], t["PAY-6"]["outcome"]["accepted"]["first_time"]),
                         (True, True))
        states = {k: (v["value"], v["stuck"]) for k, v in data["states"].items()}
        stuck = {q["key"]: [r["text"] for r in q["reasons"] if r["rule"] == "stuck"] for q in data["review_queue"]}
        self.assertEqual(states, {"PAY-5": ("answered", True), "PAY-6": ("answered", True)},
                         "states after a delivered answer: %s; stuck reasons: %s" % (states, stuck))
        self.assertFalse(any("在等回答" in x for v in stuck.values() for x in v), stuck)
        self.assertEqual({k: len(v) for k, v in stuck.items()}, {"PAY-5": 1, "PAY-6": 1}, stuck)
        self.assertIn("D-1 的回答已经送到 run", stuck["PAY-6"][0])
        self.assertIn("D-2 的回答已经送到 run", stuck["PAY-5"][0])
        o = {k: (x["outcome"]["waiting_on"], (x["outcome"]["answered"] or {}).get("decision"),
                 (x["outcome"]["answered"] or {}).get("state")) for k, x in t.items()}
        self.assertEqual(o, {"PAY-5": (None, "D-2", "delivered"), "PAY-6": (None, "D-1", "delivered")})
        self.assertEqual({r["key"]: r["state"] for r in data["batch"]}, {"PAY-5": "answered", "PAY-6": "answered"})
        self.assertIn("| PAY-6 | 有了回答、run 没接着做，99 小时没动静 |", eval_text(self.b))

    def test_31_an_open_question_is_still_awaited(self):
        """The same park question with the bridge's question still open: waiting, and the stuck reason says who is
        asked (park.json's ask) what."""
        db = sqlite3.connect(self.b.ledger_path, isolation_level=None)
        db.execute("UPDATE decisions SET state='open', answer=NULL, answerer=NULL, voucher_id=NULL WHERE id='D-1'")
        db.close()
        vdir = os.path.join(self.b.vouchers, "RUN-PAY-6-1")
        for f in sorted(os.listdir(vdir)):
            with open(os.path.join(vdir, f), encoding="utf-8") as fh:
                kind = json.load(fh)["voucher"]["kind"]
            if kind == "answer":                          # no answer was given: no answer voucher either
                os.replace(os.path.join(vdir, f), os.path.join(self.b.d, f))
        data = eval_json(self.b)
        stuck = [r["text"] for q in data["review_queue"] if q["key"] == "PAY-6" for r in q["reasons"]
                 if r["rule"] == "stuck"]
        self.assertIn("在等 product 回答 D-1：acceptance RELEASE f6f6f6f6", stuck[0])
        st = data["states"]["PAY-6"]
        self.assertEqual((st["value"], st["waiting_on"]["decision"], st.get("answered")), ("waiting", "D-1", None))


# ---------- #32: with --ledger, H4 does not apply ----------

def world_32(d):
    """PAY-7: handed over, refused by the bridge, finished locally, squash-merged, its branch deleted and pruned.
    PAY-2: handed over and accepted by the bridge, accepted first time, merged with a merge commit."""
    b = Batch(d)
    b.make_eval_clone()
    r = b.start("PAY-7", "notify", 2, 17)
    b.handoff(r, "2026-11-02T03:00:00Z")
    b.reject_handoff(r, "2026-11-02T03:05:00Z", "label 'ai-handoff' was added by bob, not by the branch owner alice")
    b.commit({"src/notify.py": "n = 1\n"}, "PAY-7: finish locally", "2026-11-02T04:00:00Z")
    ev = [("2026-11-02T05:00:00.000Z", "APPROVED", r["intent"], {"kind": "RELEASE", "sha256": "ee" * 32,
                                                                 "words": "approve RELEASE eeeeeeee"})]
    b.commit({"aidlc-docs/audit/owner2-PAY-7.jsonl": audit(ev, ALICE)}, "PAY-7: release", "2026-11-02T05:00:00Z")
    b.push(r["branch"])
    b.fetch()
    b.squash(r, "2026-11-03T00:00:00Z")
    b.push(":" + r["branch"])                       # source branch deleted
    p = b.start("PAY-2", "fees", 3, 12)
    b.handoff(p, "2026-11-03T03:00:00Z")
    b.accept(p, "2026-11-03T03:05:00Z", state="merged")
    b.bot(p, "2026-11-03T04:00:00Z", files={"src/fees.py": "f = 1\n"}, msg="PAY-2: code")
    b.accept_cycle(p, "2026-11-03T08:00:00Z", "2026-11-03T08:10:00Z", "2026-11-03T09:00:00Z", "dave", "approve",
                   "b2b2b2b2", "2026-11-03T10:00:00Z")
    b.fetch()
    b.merge_commit(p, "2026-11-04T00:00:00Z")
    return finish(b, prune=True)


class RefusedHandoverIsNotHandedOver(unittest.TestCase):
    """#32 (MAJOR): with --ledger given, a hand-over the bridge REFUSED counted as handed over by rule H4 when its
    `brain handoff` commit could not be read (squash merge, branch deleted and pruned: the refusal cannot be looked
    up by its commit): handed_over True next to bridge_accepted False, 自主 = 是, autonomy 2/2. Rules H3 / H4: with
    a ledger, only the bridge's records make a hand-over; H4 (git and run.json alone) is for no ledger, no voucher."""

    def setUp(self):
        temp_home(self)
        self.b = world_32(tmpdir(self))

    def test_32_refused_and_squash_merged_without_its_branch(self):
        data = eval_json(self.b)
        t7 = next(t for t in data["traces"] if t["key"] == "PAY-7")
        ho = t7["outcome"]["handed_over"]
        self.assertEqual(ho["bridge_accepted"], False)                      # the setup holds: nothing accepted
        self.assertEqual((ho["value"], ho["rule"]), (False, "H3"), "handed over by %s although --ledger was given "
                                                                   "and the bridge accepted nothing: %s" % (
                                                                       ho["rule"], ho["how"]))
        self.assertIn("commits are not here", ho["how"])
        rows = {r["key"]: r["autonomous"] for r in data["batch"]}
        self.assertEqual((data["autonomy"]["value"], rows), ([1, 1], {"PAY-2": True, "PAY-7": None}),
                         data["autonomy"]["runs"])

    def test_32_without_a_ledger_h4_still_reads_run_json(self):
        """H4 (no ledger, no hand-over voucher): what run.json says, as before."""
        code, out, err = run_cli(self.b.eval_repo, ["trace", "PAY-7", "--json", "--ref", "origin/main", "--as-of",
                                                    AS_OF])
        self.assertEqual(code, 0, err)
        ho = json.loads(out)["outcome"]["handed_over"]
        self.assertEqual((ho["value"], ho["rule"], ho["bridge_accepted"]), (True, "H4", None))


# ---------- #33: a re-run on a new branch has its own hand-over (H0) ----------

def world_33(d, old_answers=False):
    """RUN-PAY-9-1 on ai/alice/PAY-9-legacy (handed over, accepted, a runner commit; MR closed); RUN-PAY-9-2 on
    ai/alice/PAY-9-legacy2 (from main): handed over and accepted, a runner commit, bob's push, a runner commit.
    old_answers: the old MR also got an acceptance approval on the bridge, and KiroCrew restarted the old run."""
    b = Batch(d)
    b.make_eval_clone()
    old = b.start("PAY-9", "legacy", 14, 19)
    b.handoff(old, "2026-11-15T03:00:00Z")
    b.accept(old, "2026-11-15T03:05:00Z")
    b.bot(old, "2026-11-15T05:00:00Z", files={"src/legacy.py": "l = 1\n"}, msg="PAY-9: code")
    if old_answers:
        b.accept_cycle(old, "2026-11-16T08:00:00Z", "2026-11-16T08:10:00Z", "2026-11-16T09:00:00Z", "dave", "approve",
                       "9a9a9a9a", "2026-11-16T10:00:00Z")
    b.set_run(old, "closed", "2026-11-17T00:05:00Z")
    b.git("switch", "-q", "main")
    new = b.start("PAY-9", "legacy2", 18, 23)
    b.handoff(new, "2026-11-18T03:00:00Z")
    run2, v2 = "RUN-PAY-9-2", vid("handoff PAY-9 again")
    b.sql("INSERT INTO handoffs(project,iid,sha,state,run_id,voucher_id,reason,created,updated) VALUES "
          "(?,?,?,?,?,?,?,?,?)", (PROJECT, 23, new["handoff"] + "#230", "accepted", run2, v2, None,
                                  "2026-11-18T03:05:00Z", "2026-11-18T03:05:00Z"))
    b.sql("INSERT INTO runs(run_id,project,req,iid,sha,voucher_id,state,owner,created,updated) VALUES "
          "(?,?,?,?,?,?,?,?,?,?)", (run2, PROJECT, "PAY-9", 23, new["handoff"], v2, "dispatched", "alice",
                                    "2026-11-18T03:05:00Z", "2026-11-18T03:10:00Z"))
    b.voucher(run2, {"schema": 1, "kind": "handoff", "run_id": run2, "project": PROJECT, "mr_iid": 23,
                     "sha": new["handoff"], "branch": new["branch"], "owner": "alice", "key": "PAY-9",
                     "intent": new["intent"], "issued_at": "2026-11-18T03:05:00Z", "voucher_id": v2})
    new["run_id"] = run2
    b.bot(new, "2026-11-29T05:00:00Z", files={"src/legacy2.py": "l = 1\n"}, msg="PAY-9: code again")
    b.git("switch", "-q", new["branch"])
    b.bob = b.commit({"src/legacy2.py": "l = 2\n"}, "PAY-9: tweak", "2026-11-29T06:00:00Z", BOB)
    b.push(new["branch"])
    b.bot(new, "2026-11-30T09:00:00Z", files={"src/legacy3.py": "l = 3\n"}, msg="PAY-9: more")
    b.git("switch", "-q", "main")
    b.old, b.new = old, new
    credits = {"runs": {"RUN-PAY-9-1": {"credits": 5, "manual": [{"at": "2026-11-16T12:00:00Z", "kind": "restart"}]},
                        "RUN-PAY-9-2": 7}} if old_answers else None
    return finish(b, credits=credits)


class RerunOnANewBranch(unittest.TestCase):
    """#33 (MAJOR): a requirement started again on a new branch after its first MR was closed took the OLD run's
    acceptance as its hand-over (H1 = the earliest acceptance of any run of the key). That commit is not on the new
    branch, so every commit got after_handover None and bob's push after the new hand-over disappeared: 自主 = 是,
    judgment [1, 0], nothing in the review queue. Rule H0: the hand-over, the bridge's questions and answers, the
    acceptance and the KiroCrew acts are those of this branch's runs; the other branch's runs are other_runs."""

    def setUp(self):
        temp_home(self)

    def test_33_handover_is_this_branchs_and_the_push_after_it_counts(self):
        b = world_33(tmpdir(self))
        data = eval_json(b)
        t = data["traces"][0]
        self.assertEqual(t["ref"], "refs/remotes/origin/" + b.new["branch"])   # the setup holds: the new branch
        ho = t["outcome"]["handed_over"]
        pushes = [h["sha"] for h in t["humans"] if h["kind"] == "push_after_handover"]
        row = data["batch"][0]
        self.assertEqual((ho["commit"], ho["rule"], pushes, row["autonomous"], row["judgment"]),
                         (b.new["handoff"], "H1", [b.bob], False, [1, 1]),
                         "hand-over %s (%s); commits after_handover: %s" % (
                             ho["commit"], ho["how"], [(c["sha"][:8], c["by"], c["after_handover"])
                                                       for c in t["actions"]["commits"]]))
        self.assertEqual([(o["run_id"], o["branch"], o["state"]) for o in t["other_runs"]],
                         [("RUN-PAY-9-1", b.old["branch"], "closed")])
        self.assertEqual(([r["run_id"] for r in t["outcome"]["bridge_runs"]], t["run_ids"]),
                         (["RUN-PAY-9-2"], ["RUN-PAY-9-1", "RUN-PAY-9-2"]))
        self.assertEqual([r["rule"] for q in data["review_queue"] if q["key"] == "PAY-9" for r in q["reasons"]],
                         ["outside_judgment"])

    def test_h0_the_old_runs_acceptance_and_kirocrew_acts_are_not_this_runs(self):
        """H0: the old MR's acceptance approval (bridge decision, its voucher) and KiroCrew's restart of the old run
        are not the new branch's: not accepted yet, no platform act, no question asked here; the credits of both runs
        are the requirement's cost."""
        b = world_33(tmpdir(self), old_answers=True)
        data = eval_json(b)
        t = data["traces"][0]
        self.assertEqual(t["ref"], "refs/remotes/origin/" + b.new["branch"])
        o = t["outcome"]
        self.assertEqual((o["accepted"]["value"], o["handed_over"]["commit"]), (None, b.new["handoff"]))
        self.assertEqual(sorted(h["kind"] for h in t["humans"]), ["approval", "push_after_handover"])
        self.assertEqual([d["id"] for d in t["decisions"] if d["source"] == "bridge"], [])
        self.assertEqual((data["batch"][0]["questions"], t["cost"]["credits"]), (0, 12.0))
        # the old branch read on its own (`brain trace PAY-9 --ref <old branch>`): its own run, not the new one
        code, out, err = run_cli(b.eval_repo, ["trace", "PAY-9", "--json", "--ref", "origin/" + b.old["branch"],
                                               "--ledger", b.ledger_path, "--vouchers", b.vouchers, "--credits",
                                               b.credits_path, "--as-of", AS_OF])
        self.assertEqual(code, 0, err)
        old = json.loads(out)
        self.assertEqual((old["outcome"]["handed_over"]["commit"], old["outcome"]["accepted"]["value"],
                          [x["run_id"] for x in old["other_runs"]],
                          sorted(h["kind"] for h in old["humans"])),
                         (b.old["handoff"], True, ["RUN-PAY-9-2"], ["approval", "approval", "platform_restart"]))


# ---------- the round-4 minors in these files ----------

def small(d, key="PAY-1", slug="refund", day=2, iid=11, accept="dispatched"):
    """One run: started, handed over (the bridge accepted it unless accept is None), a runner commit, pushed."""
    b = Batch(d)
    b.make_eval_clone()
    r = b.start(key, slug, day, iid)
    b.handoff(r, "2026-11-%02dT03:00:00Z" % day)
    if accept:
        b.accept(r, "2026-11-%02dT03:05:00Z" % day, state=accept)
    b.bot(r, "2026-11-%02dT04:00:00Z" % day, files={"src/%s.py" % slug: "x = 1\n"}, msg="%s: code" % key)
    b.git("switch", "-q", "main")
    b.r = r
    return b


class Minors(unittest.TestCase):
    def setUp(self):
        temp_home(self)

    def test_minor_regression_tests_are_not_a_hotfix(self):
        """A1 (minor): the fix word "regression" made "add regression tests for PAY-1" a hotfix of PAY-1; a
        regression test (suite, case) fixes nothing. "regression" alone still is a fix word."""
        b = small(tmpdir(self))
        b.merge_commit(b.r, "2026-11-03T00:00:00Z")
        b.on_main({"tests/test_refund.py": "t = 1\n"}, "PAY-1: add regression tests for refunds", "2026-11-04T00:00:00Z")
        fix = b.on_main({"src/refund.py": "x = 2\n"}, "PAY-1 regression in refund totals", "2026-11-05T00:00:00Z")
        data = eval_json(finish(b))
        rv = data["traces"][0]["outcome"]["reverted_or_hotfixed_within_14d"]
        self.assertEqual([e["sha"] for e in rv["evidence"]], [fix], rv)

    def test_minor_a_refused_answer_is_no_decision_and_no_acceptance(self):
        """E4 (minor): an answer the signer refused to sign (decision 'refused') never reached the run, but counted as
        a person's decision and as the acceptance. It is no answer: no act, not accepted, an unanswered question in
        the funnel, and question_problem in the review queue."""
        b = small(tmpdir(self))
        lr = "RELEASE abcdabcd"
        b.bot(b.r, "2026-11-02T05:00:00Z", msg="PAY-1: ready for acceptance",
              park={"schema": 1, "status": "waiting", "kind": "acceptance", "ask": "product", "options": [],
                    "question": "请验收 PAY-1", "lean_request": lr})
        b.question(b.r, "acceptance", lr, "2026-11-02T05:10:00Z", answer="approve", answerer="dave",
                   answered="2026-11-02T06:00:00Z", state="refused")
        b.git("switch", "-q", "main")
        data = eval_json(finish(b))
        t = data["traces"][0]
        self.assertEqual((t["outcome"]["accepted"]["value"], [h["kind"] for h in t["humans"]]), (None, ["approval"]))
        self.assertEqual((data["funnel"]["counts"]["person"], data["funnel"]["unanswered_questions"]), (0, 1))
        self.assertEqual((data["states"]["PAY-1"]["value"], data["states"]["PAY-1"]["answered"]), ("waiting", None))
        self.assertIn("question_problem", [r["rule"] for q in data["review_queue"] for r in q["reasons"]])

    def test_minor_a_claimed_bridge_answer_needs_its_voucher(self):
        """人的身份 (minor): Lean approval words "... (via gitlab P-xxxx V-...)" were attributed to that pseudonym
        without checking the voucher. With vouchers or a ledger read that hold no such answer, the act is the Lean
        record's own identity's; with neither read (nothing to check against), the words still name it."""
        b = small(tmpdir(self))
        fake = vid("never issued")
        ev = [("2026-11-02T05:00:00.000Z", "APPROVED", b.r["intent"],
               {"kind": "DESIGN", "sha256": "dd" * 32, "words": "approve DESIGN dddddddd (via gitlab P-ABCD %s)" % fake,
                "claimed_channel": "gitlab P-ABCD %s" % fake})]
        b.bot(b.r, "2026-11-02T05:00:00Z", msg="PAY-1: design", events=ev)
        b.git("switch", "-q", "main")
        finish(b)

        def design_who(*extra):
            code, out, err = run_cli(b.eval_repo, ["trace", "PAY-1", "--json", "--as-of", AS_OF] + list(extra))
            self.assertEqual(code, 0, err)
            return [h["who"] for h in json.loads(out)["humans"] if h["what"] == "DESIGN"]
        self.assertEqual(design_who("--vouchers", b.vouchers), [trace.pseudonym(BOT)])
        self.assertEqual(design_who("--ledger", b.ledger_path), [trace.pseudonym(BOT)])
        self.assertEqual(design_who(), ["P-ABCD"])

    def test_minor_squash_merged_with_its_branch_gone_is_not_known_autonomous(self):
        """E5 (minor): squash-merged with the branch deleted, the run's own commits cannot be listed, so a person's
        push after the hand-over cannot be seen: 自主 is "?" (None), not 是, and autonomy does not count it."""
        b = small(tmpdir(self))
        b.accept_cycle(b.r, "2026-11-02T08:00:00Z", "2026-11-02T08:10:00Z", "2026-11-02T09:00:00Z", "dave", "approve",
                       "a1a1a1a1", "2026-11-02T10:00:00Z")
        b.squash(b.r, "2026-11-03T00:00:00Z")
        b.push(":" + b.r["branch"])
        b.sql("UPDATE runs SET state='merged'")
        data = eval_json(finish(b, prune=True), as_of="2026-12-31T00:00:00Z")
        row = data["batch"][0]
        self.assertEqual((row["state"], row["handed_over"], row["autonomous"], row["delivery"]),
                         ("merged", True, None, "correct"), row)
        self.assertEqual((data["autonomy"]["value"], [r["status"] for r in data["autonomy"]["runs"]]),
                         ([0, 0], ["unknown (the run's commits are not here)"]))

    def test_minor_ledger_says_merged_is_not_still_open(self):
        """E5 (minor): a ledger-only run (no run.json here) in state merged, and a run whose ledger state is merged but
        whose merge is not here, showed 交付 还没合并; it is merged_not_here (桥说合并了，这里读不到合并)."""
        b = small(tmpdir(self), accept="merged")
        b.sql("INSERT INTO runs(run_id,project,req,iid,sha,voucher_id,state,owner,created,updated) VALUES "
              "(?,?,?,?,?,?,?,?,?,?)", ("RUN-PAY-6-1", PROJECT, "PAY-6", 30, "0" * 40, vid("6"), "merged", "bob",
                                        "2026-11-20T00:00:00Z", "2026-11-21T00:00:00Z"))
        data = eval_json(finish(b))
        rows = {r["key"]: (r["state"], r["delivery"]) for r in data["batch"]}
        self.assertEqual(rows, {"PAY-1": ("merged_not_here", "merged_not_here"),
                                "PAY-6": ("not_readable", "merged_not_here")})
        self.assertIn("| PAY-6 | 读不到（只有桥上的记录）：桥上 merged |", eval_text(b))

    def test_minor_a_signing_reservation_is_not_a_run(self):
        """E1 (minor): a run id reserved while a hand-over is signed (ledger state 'signing') was listed as a run:
        a ledger-only requirement, in the batch, a bridge run of the trace."""
        b = small(tmpdir(self))
        for key, iid in (("PAY-1", 11), ("PAY-7", 17)):
            b.sql("INSERT INTO runs(run_id,project,req,iid,sha,voucher_id,state,owner,created,updated) VALUES "
                  "(?,?,?,?,?,?,?,?,?,?)", ("RUN-%s-9" % key, PROJECT, key, iid, "", "", "signing", "",
                                            "2026-11-29T00:00:00Z", "2026-11-29T00:00:00Z"))
        data = eval_json(finish(b))
        self.assertEqual((data["ledger_only"], [r["key"] for r in data["batch"]]), ([], ["PAY-1"]))
        self.assertEqual([r["run_id"] for r in data["traces"][0]["outcome"]["bridge_runs"]], ["RUN-PAY-1-1"])

    def test_minor_an_unreadable_run_json_has_a_batch_row(self):
        """E5 (minor): a run whose run.json cannot be read (not JSON) was in `skipped` but had no row in the batch."""
        b = small(tmpdir(self))
        b.git("switch", "-q", "-c", "ai/alice/PAY-3-broken", "main")
        b.commit({"runs/PAY-3/run.json": "{not json"}, "PAY-3: start run", "2026-11-04T02:00:00Z")
        b.push("ai/alice/PAY-3-broken")
        b.git("switch", "-q", "main")
        data = eval_json(finish(b))
        self.assertEqual([s["key"] for s in data["skipped"]], ["PAY-3"])
        rows = {r["key"]: r for r in data["batch"]}
        self.assertEqual(sorted(rows), ["PAY-1", "PAY-3"])
        self.assertEqual((rows["PAY-3"]["state"], rows["PAY-3"]["handed_over"], rows["PAY-3"]["autonomous"]),
                         ("unreadable", None, None))
        self.assertIn("| PAY-3 | run.json 读不了 | ? |", eval_text(b))

    def test_minor_since_keeps_a_run_closed_in_the_period(self):
        """E3 (minor): `--since` dropped a run whose MR was closed in the period: its last commit was older, and the
        ledger's closing time was not looked at."""
        b = small(tmpdir(self), accept="closed")
        b.sql("UPDATE runs SET updated='2026-11-20T00:00:00Z'")
        data = eval_json(finish(b), "--since", "2026-11-15")
        self.assertEqual([(r["key"], r["state"]) for r in data["batch"]], [("PAY-1", "closed")])
        self.assertEqual([q["key"] for q in data["review_queue"]], ["PAY-1"])

    def test_minor_brain_trace_ref_main_reads_an_open_run_from_its_branch(self):
        """E1 (minor): `brain trace KEY --ref origin/main` failed for a run not merged yet (no run.json on main); it
        reads the run branch, as `brain eval --ref origin/main` does."""
        b = finish(small(tmpdir(self)))
        code, out, err = run_cli(b.eval_repo, ["trace", "PAY-1", "--json", "--ref", "origin/main", "--as-of", AS_OF])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out)["ref"], "refs/remotes/origin/" + b.r["branch"])
        code, out, err = run_cli(b.eval_repo, ["trace", "PAY-1", "--ref", "origin/main~0"])   # not the default branch
        self.assertNotEqual(code, 0, "a ref that names something else is read as given")

    def test_minor_header_and_credits_count_ledger_only_runs(self):
        """E1 (minor): the report header and the "one credits record, several requirements" check left out the
        requirements only the ledger has: one unkeyed record went to the one run read here."""
        b = small(tmpdir(self))
        b.sql("INSERT INTO runs(run_id,project,req,iid,sha,voucher_id,state,owner,created,updated) VALUES "
              "(?,?,?,?,?,?,?,?,?,?)", ("RUN-PAY-6-1", PROJECT, "PAY-6", 30, "0" * 40, vid("6"), "dispatched", "bob",
                                        "2026-11-20T00:00:00Z", "2026-11-21T00:00:00Z"))
        finish(b, credits={"credits": 9, "turns": []})
        data = eval_json(b)
        self.assertEqual((data["traces"][0]["cost"]["credits"], data["ledger_only"]), (None, ["PAY-6"]))
        self.assertIn("2 个需求", data["cost"]["note"])
        self.assertIn("另有 1 个只在桥上", eval_text(b).splitlines()[0])


class Docs(unittest.TestCase):
    def test_round_4_rules_are_in_docs_eval_md(self):
        with open(os.path.join(ROOT, "docs", "eval.md"), encoding="utf-8") as f:
            text = f.read()
        for rule in ("H0", "H3", "H4", "E2", "E3", "E5"):
            self.assertRegex(text, r"\*\*%s[* ]" % rule)
        for word in ("answered", "runner_failed", "closed_unmerged", "other_runs", "merged_not_here", "unreadable",
                     "regression test"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
