"""Run Trace (brainkit/trace.py): one requirement's record from run.json, the Lean audit, git, the bridge ledger,
vouchers, Brain usage and credits. Synthetic repos with exact timestamps, plus one run through a real Lean install."""
import hashlib
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import cli, config, sign, trace  # noqa: E402
from brainkit.bridge.decisions import SCHEMA as DECISIONS_SCHEMA  # noqa: E402
from brainkit.bridge.ledger import Ledger  # noqa: E402
from brainkit.common import BrainError, write_json  # noqa: E402
from tests.helpers import LEAN_SRC, git_repo, keypair, sh, tmpdir  # noqa: E402

BOT = "brain-bot@noreply.invalid"
KIT = {"version": 1, "req_prefix": "PAY", "bot_users": ["brain-bot"], "bot_emails": [BOT]}
TECH = ("# TECH\n\n<!-- id:T-PAY-1-1 type:pitfall side:builder rel:new src:D-1 -->\n退款单号是 RF + 9 位。\n\n"
        "<!-- id:T-PAY-1-2 type:pitfall side:builder rel:new src:D-1 -->\n重试最多 3 次。\n")
TECH_ARCHIVE = ("# TECH archive\n\n<!-- id:T-PAY-9-1 type:pitfall side:builder rel:new src:D-1 status:withdrawn "
                "archived:2026-10-29 -->\n旧的退款规则。\n\n> 过时\n")
PRODUCT = ("# PRODUCT\n\n<!-- id:P-PAY-1-1 type:correction side:product rel:new repeats:2 src:D-1 -->\n退款只退原路。\n\n"
           "<!-- id:P-PAY-1-2 type:model side:product rel:new src:D-1 -->\n**退款单**：一次退款的记录。\n\n"
           "<!-- id:P-PAY-1-3 type:model side:product rel:new src:D-1 -->\n**退款单**：退款申请。\n")
AS_OF = "2026-11-30T00:00:00Z"


def vid(key, n):
    return "V-" + hashlib.sha256(("%s-%d" % (key, n)).encode()).hexdigest()[:10]


def audit(events, email):
    """Hash-chained Lean audit lines, made with Lean's own rule (aidlc_lean.py Project.append)."""
    out, prev = [], "GENESIS"
    for n, (ts, event, intent, data) in enumerate(events, 1):
        rec = {"seq": n, "ts": ts, "event": event, "intent": intent,
               "actor": {"git_email": email, "git_name": "n", "os_user": "u", "host": "h", "team": None},
               "data": data, "prev": prev}
        rec["hash"] = hashlib.sha256(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        prev = rec["hash"]
        out.append(json.dumps(rec, ensure_ascii=False))
    return "\n".join(out) + "\n"


def commit(repo, files, msg, date, email="alice@example.com"):
    for rel, text in files.items():
        p = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    sh("git", "-C", repo, "add", "-A")
    sh("git", "-C", repo, "commit", "-q", "-m", msg, env={"GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email,
                                                          "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    return sh("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()


def table(rows):
    return "| ID | Date | Stage | Decision | Why | Who |\n|---|---|---|---|---|---|\n" + \
           "".join("| %s |\n" % " | ".join(r) for r in rows)


def add_run(w, key, slug, day, bad):
    """One requirement from claim to merge. bad: the messy one (rejections, a person's push after hand-over,
    3 failed verifies, a hotfix after the merge); otherwise a clean autonomous run."""
    repo = w.repo
    intent = "2611%02d-%s-%s" % (day, key.lower(), slug)
    br = "ai/alice/%s-%s" % (key, slug)
    run_id = "RUN-%s-1" % key
    base = sh("git", "-C", repo, "rev-parse", "main").stdout.strip()
    sh("git", "-C", repo, "switch", "-q", "-c", br, "main")
    t = lambda h, m=0: "2026-11-%02dT%02d:%02d:00Z" % (day, h, m)  # noqa: E731
    lt = lambda h, m=0: "2026-11-%02dT%02d:%02d:00.000Z" % (day, h, m)  # noqa: E731
    v = {n: vid(key, n) for n in range(1, 7)}
    rec = {"schema": 1, "key": key, "tracker": "none", "owner": "alice", "branch": br, "intent": intent,
           "brain_commit": base, "kit_version": "0.1.0", "origin": "local", "expected_paths": [], "materials": [],
           "created_at": t(0), "claim_checked": True, "handoff": None}
    docs = "aidlc-docs/%s/" % intent
    local = [(lt(0), "INTENT_CREATED", intent, {"title": key}),
             (lt(0, 1), "STAGE_STARTED", intent, {"stage": "requirements", "unit": None}),
             (lt(1), "STAGE_COMPLETED", intent, {"stage": "requirements", "unit": None, "note": "AC-1..2, A-1"}),
             (lt(1, 10), "HUMAN_TURN", intent, {"session": "s1", "turn": 1, "sha256": "0" * 64, "chars": 9,
                                               "allow_protected": False}),
             (lt(1, 30), "APPROVED", intent, {"kind": "REQUIREMENTS", "path": docs + "r.md", "sha256": "ab" * 32,
                                             "session": "s1", "words": "approve REQUIREMENTS abababab"}),
             (lt(1, 50), "STAGE_SKIPPED", intent, {"stage": "user-stories", "unit": None, "reason": "one persona"}),
             (lt(1, 55), "SESSION_START", "261101-other", {"session": "s9"})]
    if bad:
        local += [(lt(1, 40), "BLOCKED", intent, {"tool": "fs_write", "path": "src/refund.py", "gate": "design",
                                                  "reason": "design not approved"}),
                  (lt(2), "VERIFY", intent, {"ok": False, "fingerprint": "f1f1f1f1aa", "results": [
                      {"cmd": "python3 -m unittest", "exit": 1, "secs": 1, "output_sha256": "0"}]}),
                  (lt(2, 1), "HUMAN_TURN", intent, {"session": "s1", "turn": 2, "sha256": "1" * 64, "chars": 16,
                                                   "allow_protected": True})]
    local.sort(key=lambda e: e[0])
    rows = [["D-1", "2026-11-%02d" % day, "requirements", "单号按 T-PAY-1-1", "PRD 和样例不一致", "agent"]]
    if bad:
        rows += [["D-2", "2026-11-01", "design", "新建 refunds 表", "最简单", "product"],
                 ["D-3", "2026-11-01", "code", "重试 3 次", "CI 不稳", "verify"],
                 ["D-4", "2026-11-01", "code", "日志字段", "", ""],
                 ["D-5", "2026-11-01", "", "张三定的 X", "", "张三"]]
    else:
        rows += [["D-2", "2026-11-%02d" % day, "code", "沿用旧的 T-PAY-9-1 规则", "", "agent"]]
    req = "# Requirements\n\n## Assumptions\n- A-1: 只退 14 天内的订单\n- A-2: \n\n## Questions and answers\n1. Q:\n"
    commit(repo, {"runs/%s/run.json" % key: json.dumps(rec), docs + "decisions.md": "# Decisions\n\n" + table(rows),
                  docs + "inception/requirements/requirements.md": req,
                  "aidlc-docs/audit/local-%s.jsonl" % key: audit(local, "alice@example.com")},
           "%s: start" % key, t(2, 10))
    rec["handoff"] = {"requested_at": t(3), "by": "alice", "local_head": "x"}
    commit(repo, {"runs/%s/run.json" % key: json.dumps(rec)}, "%s: hand off" % key, t(3))
    runner = [(lt(4), "SESSION_START", intent, {"session": "k1"}),
              (lt(4, 1), "HUMAN_TURN", intent, {"session": "k1", "turn": 1, "sha256": "2" * 64, "chars": 50,
                                               "allow_protected": False}),
              (lt(4, 10), "APPROVED", intent, {"kind": "DESIGN", "sha256": "cd" * 32,
                                              "words": "approve DESIGN cdcdcdcd",
                                              "claimed_channel": "gitlab P-ABCD %s" % v[1]}),
              (lt(4, 30), "VERIFY", intent, {"ok": True, "fingerprint": "f2f2f2f2", "results": [
                  {"cmd": "python3 -m unittest", "exit": 0, "secs": 1, "output_sha256": "0"}]}),
              (lt(4, 40), "STAGE_COMPLETED", intent, {"stage": "code-generation", "unit": "app", "note": "done"}),
              (lt(7), "APPROVED", intent, {"kind": "RELEASE", "sha256": "ef" * 32, "words": "approve RELEASE efefefef",
                                          "claimed_channel": "gitlab P-ABCD %s" % v[4]}),
              (lt(7, 0), "STAGE_COMPLETED", intent, {"stage": "release", "unit": None, "note": "RELEASE approved"})]
    if bad:
        runner += [(lt(4, 5), "VERIFY", intent, {"ok": False, "results": [{"cmd": "lint", "exit": 2}]}),
                   (lt(4, 20), "VERIFY", intent, {"ok": False, "results": [{"cmd": "lint", "exit": 2}]}),
                   (lt(6), "REJECTED", intent, {"kind": "RELEASE", "sha256": "ef" * 32,
                                               "words": "reject RELEASE SECRET-HUMAN-WORDS (via gitlab P-ABCD %s)"
                                                        % v[3]})]
    runner.sort(key=lambda e: e[0])
    park = {"schema": 1, "status": "waiting", "kind": "design", "ask": "builder", "question": "QUESTION-TEXT?",
            "options": [], "lean_request": "DESIGN cdcdcdcd"}
    files = {"src/%s.py" % slug: "x = 1\n", "aidlc-docs/audit/runner-%s.jsonl" % key: audit(runner, BOT),
             "runs/%s/park.json" % key: json.dumps(park)}
    commit(repo, files, "%s: code" % key, t(4, 50), BOT)
    if bad:
        commit(repo, {"src/%s.py" % slug: "x = 2\n"}, "%s: my fix" % key, t(5), "bob@example.com")
        rows.append(["D-6", "2026-11-01", "code", "按 D-2 的回答 A（%s）" % v[2], "", "product (P-ABCD)"])
        prop = ("\n## Brain proposals\n\n```brain-proposal\nfile: TECH\ntype: pitfall\nrel: repeats T-PAY-1-2\n"
                "src: D-4\n---\n又重试超过 3 次。\n```\n")
    else:
        prop = ""
    commit(repo, {"runs/%s/park.json" % key: json.dumps(dict(park, status="answered")),
                  docs + "decisions.md": "# Decisions\n\n" + table(rows) + prop}, "%s: answered" % key, t(7, 10), BOT)
    tip = sh("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()
    sh("git", "-C", repo, "switch", "-q", "main")
    md = "2026-11-%02dT00:00:00Z" % (day + 1)
    sh("git", "-C", repo, "merge", "-q", "--no-ff", br, "-m", "Merge branch '%s' into 'main'" % br,
       env={"GIT_COMMITTER_DATE": md, "GIT_AUTHOR_DATE": md, "GIT_COMMITTER_EMAIL": "lead@example.com"})
    # bridge: ledger rows and vouchers
    db = w.ledger.db
    w.ledger.add_run(run_id, "grp/repo", key, day, tip, v[6], owner="alice")
    w.ledger.set_run(run_id, "spooled", "closed")
    w.ledger.claim_handoff("grp/repo", day, tip + "#1")
    w.ledger.set_handoff("grp/repo", day, tip + "#1", "checking", "accepted", run_id=run_id, voucher_id=v[6])
    answers = [(1, "design", "DESIGN cdcdcdcd", "approve")]
    if bad:
        answers += [(2, "clarify", None, "A"), (3, "acceptance", "RELEASE efefefef", "reject")]
    answers += [(4, "acceptance", "RELEASE efefefef", "approve")]
    vdir = os.path.join(w.vouchers, run_id)
    vdoc = {"schema": 1, "kind": "handoff", "run_id": run_id, "sha": tip, "branch": br, "issued_at": t(3, 5),
            "voucher_id": v[6]}
    write_json(os.path.join(vdir, v[6] + ".json"), {"voucher": vdoc, "sig": w.sign(vdoc)})
    for n, kind, lr, choice in answers:
        w.dn += 1
        did = "D-%d" % w.dn
        db.execute("INSERT INTO decisions(id,run_id,project,iid,sha,park_hash,kind,lean_request,state,answer,answerer,"
                   "voucher_id,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (did, run_id, "grp/repo", day, tip, "h%d" % w.dn, kind, lr, "delivered", choice, "carol", v[n],
                    t(3 + n), t(3 + n, 9)))
        if bad and n == 2:
            w.clarify = did
        vdoc = {"schema": 1, "kind": "answer", "decision": did, "run_id": run_id, "decision_kind": kind,
                "choice": choice, "answerer": "P-ABCD", "issued_at": t(3 + n, 9), "voucher_id": v[n]}
        if lr:
            vdoc["lean_request"] = lr
        write_json(os.path.join(vdir, v[n] + ".json"), {"voucher": vdoc, "sig": w.sign(vdoc)})
    if bad:  # a golden-task answer that reached the same run by mistake: never a person's
        w.dn += 1
        vdoc = {"schema": 1, "kind": "answer", "decision": "D-%d" % w.dn, "run_id": run_id, "decision_kind": "clarify",
                "choice": "B", "answerer": "P-0RAC", "issued_at": t(5, 30), "voucher_id": v[5], "eval_oracle": True}
        write_json(os.path.join(vdir, v[5] + ".json"), {"voucher": vdoc, "sig": w.sign(vdoc)})
    return types.SimpleNamespace(key=key, intent=intent, branch=br, base=base, tip=tip, run_id=run_id, v=v)


def world(test, runs=("bad",), signed=False, d=None):
    d = d or tmpdir(test)
    w = types.SimpleNamespace(d=d, repo=git_repo(os.path.join(d, "proj")), vouchers=os.path.join(d, "vouchers"),
                              ledger_path=os.path.join(d, "ledger.sqlite"), dn=0, runs={}, allowed=None)
    w.ledger = Ledger(w.ledger_path)
    w.ledger.db.executescript(DECISIONS_SCHEMA)
    if signed and sign.available():
        key, w.allowed = keypair(d)
        w.sign = lambda v: sign.sign(v, key)
    else:
        w.sign = lambda v: "unsigned"
    commit(w.repo, {".brain/kit.json": json.dumps(KIT), "brain/TECH.md": TECH, "brain/TECH-archive.md": TECH_ARCHIVE,
                    "brain/PRODUCT.md": PRODUCT, "README.md": "x\n"}, "init", "2026-10-30T00:00:00Z",
           "lead@example.com")
    plan = {"bad": ("PAY-2", "refund", 1), "good": ("PAY-4", "export", 3)}
    for r in runs:
        k, slug, day = plan[r]
        w.runs[r] = add_run(w, k, slug, day, r == "bad")
    if "bad" in runs:  # a hotfix naming PAY-2 three days after its merge, and an unrelated commit
        commit(w.repo, {"src/refund.py": "x = 3\n"}, "hotfix PAY-2 rounding", "2026-11-05T00:00:00Z",
               "lead@example.com")
        commit(w.repo, {"README.md": "y\n"}, "docs", "2026-11-06T00:00:00Z", "lead@example.com")
    w.ledger.db.close()
    w.cfg = config.load(w.repo)
    return w


def build(w, key="PAY-2", **kw):
    kw.setdefault("ledger_path", w.ledger_path)
    kw.setdefault("vouchers_dir", w.vouchers)
    kw.setdefault("as_of", AS_OF)
    return trace.build(w.repo, w.cfg, key, **kw)


class Trace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import shutil
        import tempfile
        d = os.path.realpath(tempfile.mkdtemp(prefix="brainkit-"))
        cls.addClassCleanup(shutil.rmtree, d, True)
        cls.w = world(cls, d=d)
        cls.credits = {"runs": {"RUN-PAY-2-1": {"turns": [{"turn": 1, "credits": 2.5, "agent": "brain-run"},
                                                          {"turn": 2, "credits": 1.5}],
                                                "manual": [{"at": "2026-11-01T05:30:00Z", "kind": "restart"}],
                                                "context_used": 1000, "context_window": 200000}}}
        cls.t = build(cls.w, credits=cls.credits)

    def test_run_fields_and_sources(self):
        t, r = self.t, self.w.runs["bad"]
        self.assertEqual((t["schema"], t["key"], t["intent"], t["owner"], t["branch"], t["brain_commit"]),
                         (1, "PAY-2", r.intent, "alice", r.branch, r.base))
        self.assertEqual(t["run_ids"], ["RUN-PAY-2-1"])
        self.assertEqual(t["src"]["intent"], "runs/PAY-2/run.json (working tree)")
        self.assertEqual({p: c["chain"] for p, c in t["src"]["lean_audit"].items()},
                         {"aidlc-docs/audit/local-PAY-2.jsonl": "ok", "aidlc-docs/audit/runner-PAY-2.jsonl": "ok"})
        self.assertEqual(t["src"]["git"]["tip"], r.tip)
        self.assertEqual(t["as_of"], AS_OF)

    def test_stages_checks_actions(self):
        t = self.t
        self.assertEqual([(s["id"], s["state"]) for s in t["stages"]],
                         [("requirements", "done"), ("user-stories", "skipped"), ("code-generation/app", "done"),
                          ("release", "done")])
        self.assertEqual(t["stages"][1]["note"], "one persona")
        self.assertTrue(all(s["src"].startswith("aidlc-docs/audit/") for s in t["stages"]))
        c = t["checks"]
        self.assertEqual((len(c["verify"]), c["verify_failed"]), (4, 3))
        self.assertEqual(c["verify"][0]["detail"]["failed"], ["python3 -m unittest"])
        self.assertIsNone(c["ci"])
        a = t["actions"]
        self.assertEqual([(x["by"], x["after_handover"]) for x in a["commits"]],
                         [("person", False), ("person", False), ("bot", True), ("person", True), ("bot", True)])
        self.assertTrue(all(x["who"] is None or x["who"].startswith("G-") for x in a["commits"]))
        self.assertEqual([(b["gate"], b["path"]) for b in a["blocked"]], [("design", "src/refund.py")])
        self.assertIsNone(a["pushes"])
        self.assertEqual(a["turns"]["HUMAN_TURN"], {"before_handover": 2, "after_handover": 1})
        self.assertNotIn("SESSION_START", [k for k, v in a["turns"].items() if "before_handover" in v])
        self.assertEqual([h["kind"] for h in a["handovers"]], ["requested", "voucher", "bridge accepted"])
        self.assertEqual([(q["status"], q["lean_request"]) for q in a["questions"]],
                         [("waiting", "DESIGN cdcdcdcd"), ("answered", "DESIGN cdcdcdcd")])

    def test_decisions(self):
        t = self.t
        md = {d["id"]: d for d in t["decisions"] if d["source"] == "decisions.md"}
        self.assertEqual({k: d["by"] for k, d in md.items()},
                         {"D-1": "agent", "D-2": "person", "D-3": "check", "D-4": None, "D-5": "person",
                          "D-6": "person"})
        self.assertEqual(md["D-1"]["cites"], ["T-PAY-1-1"])
        self.assertIsNone(md["D-5"]["who"])                       # a name in the Who column is not repeated
        self.assertEqual(md["D-6"]["bridge_decision"], self.w.clarify)
        self.assertTrue(md["D-1"]["src"].endswith("decisions.md:5"))
        a = [d for d in t["decisions"] if d["source"] == "requirements.md"]
        self.assertEqual([(d["id"], d["by"], d["at"]) for d in a], [("A-1", "agent", "2026-11-01T01:00:00Z")])
        b = [d for d in t["decisions"] if d["source"] == "bridge"]
        self.assertEqual([(d["kind"], d["choice"], d["who"]) for d in b],
                         [("design", "approve", "P-ABCD"), ("clarify", "A", "P-ABCD"),
                          ("acceptance", "reject", "P-ABCD"),
                          ("acceptance", "approve", "P-ABCD")])
        self.assertEqual(b[1]["recorded_in"].split(" ")[0], "D-6")
        self.assertEqual([o["eval_oracle"] for o in t["oracle"]], [True])

    def test_humans(self):
        hs = self.t["humans"]
        self.assertEqual([(h["kind"], h["what"], h["judgment_point"], h["after_handover"]) for h in hs], [
            ("approval", "REQUIREMENTS", True, False), ("allow_test_change", None, False, False),
            ("approval", "DESIGN", True, True), ("push_after_handover", "code", False, True),
            ("answer", "clarify", True, True), ("platform_restart", None, False, True),
            ("rejection", "RELEASE", True, True), ("approval", "RELEASE", True, True)])
        self.assertEqual(hs[0]["who"], trace.pseudonym("alice@example.com"))
        self.assertEqual(hs[0]["channel"], "local")
        # ledger row, voucher and Lean's APPROVED / REJECTED: one act, three sources
        self.assertEqual([x.split("/")[0].split(" ")[0] for x in hs[2]["src"]], ["bridge", "vouchers", "aidlc-docs"])
        self.assertEqual(len(hs[6]["src"]), 3)
        self.assertEqual(hs[3]["who"], trace.pseudonym("bob@example.com"))
        self.assertNotIn("P-0RAC", json.dumps(hs))                # the oracle's answer is nobody's act

    def test_no_personal_data_or_human_words(self):
        text = json.dumps(self.t, ensure_ascii=False)
        for s in ("alice@example.com", "bob@example.com", "lead@example.com", "SECRET-HUMAN-WORDS", "QUESTION-TEXT",
                  "carol"):
            self.assertNotIn(s, text, s)

    def test_brain_cost_outcome(self):
        t = self.t
        self.assertEqual((t["brain"]["loaded"], t["brain"]["cited"], t["brain"]["stale"]), (5, ["T-PAY-1-1"], []))
        self.assertEqual(t["brain"]["proposals"]["repeats"], ["T-PAY-1-2"])
        self.assertEqual(t["cost"]["credits"], 4.0)
        self.assertEqual([x["credits"] for x in t["cost"]["per_turn"]], [2.5, 1.5])
        self.assertIsNone(t["cost"]["tokens"])
        o = t["outcome"]
        self.assertEqual((o["handed_over"]["value"], o["handed_over"]["bridge_accepted"]), (True, True))
        self.assertEqual((o["accepted"]["value"], o["accepted"]["first_time"], o["accepted"]["rejections"]),
                         (True, False, 1))
        self.assertEqual((o["merged_at"]["value"], o["merged_at"]["how"]), ("2026-11-02T00:00:00Z", "merge commit"))
        r = o["reverted_or_hotfixed_within_14d"]
        self.assertEqual((r["value"], r["window_closed"]), (True, True))
        self.assertEqual([e["rule"] for e in r["evidence"]], ["names PAY-2 with a fix word"])

    def test_deterministic_and_read_only(self):
        before = sh("git", "-C", self.w.repo, "status", "--porcelain", "--ignored").stdout
        refs = sh("git", "-C", self.w.repo, "for-each-ref").stdout
        t2 = build(self.w, credits=self.credits)
        self.assertEqual(json.dumps(self.t, sort_keys=True), json.dumps(t2, sort_keys=True))
        self.assertEqual(before, sh("git", "-C", self.w.repo, "status", "--porcelain", "--ignored").stdout)
        self.assertEqual(refs, sh("git", "-C", self.w.repo, "for-each-ref").stdout)

    def test_ref_reads_the_same(self):
        t2 = build(self.w, ref="main", credits=self.credits)
        self.assertEqual(t2["ref"], "main")
        self.assertEqual(json.dumps(self.t, sort_keys=True).replace("working tree", "main"),
                         json.dumps(t2, sort_keys=True))

    def test_missing_sources_are_null_not_guessed(self):
        t = build(self.w, ledger_path=None, vouchers_dir=None)
        self.assertIn("bridge ledger (--ledger not given)", t["missing"])
        self.assertIn("credits (not passed in)", t["missing"])
        self.assertEqual(t["run_ids"], [])
        self.assertEqual([d["source"] for d in t["decisions"]].count("bridge"), 0)
        self.assertIsNone(t["cost"]["credits"])
        self.assertIsNone(t["outcome"]["handed_over"]["bridge_accepted"])
        # without vouchers, the Lean approvals that came through the bridge are still there (who: the pseudonym)
        self.assertEqual([h["who"] for h in t["humans"] if h["what"] == "DESIGN"], ["P-ABCD"])

    def test_deleted_branch_found_through_the_merge(self):
        sh("git", "-C", self.w.repo, "branch", "-q", "-D", self.w.runs["bad"].branch)
        self.addCleanup(sh, "git", "-C", self.w.repo, "branch", "-q", self.w.runs["bad"].branch, self.w.runs["bad"].tip)
        t = build(self.w)
        self.assertIn("second parent of merge commit", t["src"]["git"]["tip_src"])
        self.assertEqual(len(t["actions"]["commits"]), 5)
        self.assertEqual(t["outcome"]["merged_at"]["value"], "2026-11-02T00:00:00Z")

    def test_window_still_open(self):
        t = build(self.w, as_of="2026-11-03T00:00:00Z")          # before the hotfix: not known yet
        r = t["outcome"]["reverted_or_hotfixed_within_14d"]
        self.assertEqual((r["value"], r["window_closed"], r["evidence"]), (None, False, []))
        t = build(self.w, as_of="2026-11-10T00:00:00Z")          # after it, window still open: known
        r = t["outcome"]["reverted_or_hotfixed_within_14d"]
        self.assertEqual((r["value"], r["window_closed"]), (True, False))

    def test_cli(self):
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.w.repo)
        try:
            with redirect_stdout(out), redirect_stderr(out):
                rc = cli.main(["trace", "PAY-2", "--ledger", self.w.ledger_path, "--vouchers", self.w.vouchers,
                               "--as-of", AS_OF])
                rc2 = cli.main(["trace", "PAY-2", "--json", "--as-of", AS_OF, "--vouchers", self.w.vouchers])
                rc3 = cli.main(["trace", "PAY-99"])
        finally:
            os.chdir(cwd)
        text = out.getvalue()
        self.assertEqual((rc, rc2, rc3), (0, 0, 2))
        self.assertIn("验收：通过（驳回 1 次）", text)
        self.assertIn("判断点之外", text)
        self.assertIn('"key": "PAY-2"', text)
        self.assertIn("runs/PAY-99/run.json not found", text)


class TraceEdges(unittest.TestCase):
    def test_minimal_run_and_bad_input(self):
        repo = git_repo(os.path.join(tmpdir(self), "r"))
        commit(repo, {".brain/kit.json": json.dumps(KIT), "brain/TECH.md": TECH}, "init", "2026-11-01T00:00:00Z")
        bc = sh("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()
        rec = {"schema": 1, "key": "PAY-3", "owner": "alice", "branch": "ai/alice/PAY-3-x", "intent": "261101-x",
               "brain_commit": bc, "handoff": None}
        # squash merge: the branch is gone, run.json arrived on main in one commit
        commit(repo, {"runs/PAY-3/run.json": json.dumps(rec)}, "PAY-3 squashed", "2026-11-02T00:00:00Z")
        cfg = config.load(repo)
        t = trace.build(repo, cfg, "PAY-3", as_of=AS_OF)
        self.assertEqual((t["stages"], t["humans"], t["decisions"]), ([], [], []))
        self.assertIsNone(t["actions"]["commits"])
        self.assertEqual(t["outcome"]["accepted"], dict(t["outcome"]["accepted"], value=None, first_time=None))
        self.assertEqual(t["outcome"]["merged_at"]["value"], "2026-11-02T00:00:00Z")
        self.assertIn("squash", t["outcome"]["merged_at"]["how"])
        self.assertIs(t["outcome"]["reverted_or_hotfixed_within_14d"]["value"], False)
        self.assertEqual(t["brain"]["loaded"], 2)
        self.assertTrue(any(m.startswith("Lean audit") for m in t["missing"]))
        with self.assertRaises(BrainError):
            trace.build(repo, cfg, "pay-3")
        with self.assertRaises(BrainError):
            trace.build(repo, cfg, "PAY-4")
        with self.assertRaises(BrainError):
            trace.build(repo, cfg, "PAY-3", ref="no-such-ref")
        with self.assertRaises(BrainError):
            trace.build(repo, cfg, "PAY-3", ledger_path=os.path.join(repo, "nope.sqlite"))

    def test_broken_chain_is_reported(self):
        repo = git_repo(os.path.join(tmpdir(self), "r"))
        lines = audit([("2026-11-01T00:00:00.000Z", "INTENT_CREATED", "261101-x", {}),
                       ("2026-11-01T00:01:00.000Z", "STAGE_COMPLETED", "261101-x", {"stage": "requirements"})],
                      "a@example.com").replace('"requirements"', '"release"')
        rec = {"schema": 1, "key": "PAY-3", "owner": "alice", "intent": "261101-x", "handoff": None}
        commit(repo, {".brain/kit.json": json.dumps(KIT), "runs/PAY-3/run.json": json.dumps(rec),
                      "aidlc-docs/audit/c.jsonl": lines}, "init", "2026-11-01T00:00:00Z")
        t = trace.build(repo, config.load(repo), "PAY-3", as_of=AS_OF)
        self.assertEqual(t["src"]["lean_audit"]["aidlc-docs/audit/c.jsonl"]["chain"], "broken at line 2")
        self.assertEqual([s["id"] for s in t["stages"]], ["release"])   # still shown, flagged by the chain status

    def test_who_column(self):
        c = trace.classify_who
        self.assertEqual(c("agent (A-2)"), ("agent", "agent"))
        self.assertEqual(c("brain-bot", ["brain-bot"]), ("agent", "brain-bot"))
        self.assertEqual(c("lean verify"), ("check", "lean"))
        self.assertEqual(c("product via V-0123456789"), ("person", "product"))
        self.assertEqual(c("P-AB12"), ("person", "P-AB12"))
        self.assertEqual(c("alice"), ("person", "alice"))
        self.assertEqual(c("王小明"), ("person", None))
        self.assertEqual(c("Dr. Who Esq."), (None, None))
        self.assertEqual(c(""), (None, None))

    @unittest.skipUnless(sign.available(), "ssh-keygen not found")
    def test_voucher_signatures(self):
        w = world(self, runs=("good",), signed=True)
        t = build(w, "PAY-4", allowed_signers=w.allowed)
        self.assertEqual({h.get("signature") for h in t["actions"]["handovers"] if h["kind"] == "voucher"}, {"ok"})
        p = os.path.join(w.vouchers, "RUN-PAY-4-1", vid("PAY-4", 1) + ".json")
        with open(p) as f:
            doc = json.load(f)
        doc["voucher"]["choice"] = "reject"
        write_json(p, doc)
        t = build(w, "PAY-4", allowed_signers=w.allowed)
        self.assertEqual([d["signature"] for d in t["decisions"] if d["source"] == "bridge"], ["bad", "ok"])


@unittest.skipUnless(os.path.exists(os.path.join(LEAN_SRC, "install.sh")), "AI-DLC Lean checkout not found")
class TraceRealLean(unittest.TestCase):
    """The audit events a real Lean install writes are the ones the trace reads."""

    def test_real_lean_events(self):
        os.environ["BRAIN_USER"] = "alice"
        self.addCleanup(os.environ.pop, "BRAIN_USER", None)
        repo = git_repo(os.path.join(tmpdir(self), "proj"))
        commit(repo, {"src/app.py": "print('hi')\n"}, "init", "2026-11-01T00:00:00Z")
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(repo)
        try:
            with redirect_stdout(out), redirect_stderr(out):
                self.assertEqual(cli.main(["install", repo, "--lean", LEAN_SRC, "--prefix", "PAY"]), 0)
                sh("git", "-C", repo, "add", "-A")
                sh("git", "-C", repo, "commit", "-q", "-m", "kit")
                self.assertEqual(cli.main(["start", "refund", "--key", "PAY-1"]), 0, out.getvalue())
        finally:
            os.chdir(cwd)
        cfg = config.load(repo)
        with open(os.path.join(repo, "runs", "PAY-1", "run.json")) as f:
            rec = json.load(f)
        intent = rec["intent"]
        lean = [sys.executable, ".kiro/aidlc-lean/aidlc_lean.py"]
        req = os.path.join(repo, "aidlc-docs", intent, "inception", "requirements", "requirements.md")
        with open(req, "w") as f:
            f.write("# Requirements: refund\n\nSize: small\n\n## Goal\nRefunds within 14 days.\n\n"
                    "## Not doing\n- partial\n"
                    "\n## Acceptance criteria\n| ID | Given / when | Then (expected) |\n|---|---|---|\n"
                    "| AC-1 | order 3 days old | refund ok |\n\n"
                    "## Non-functional targets\n- none beyond today's system\n"
                    "\n## Assumptions\n- A-1: one currency\n\n## Questions and answers\n1. Q: partial?\n   A: no\n")
        sh(*lean, "stage", "done", "requirements", "--note", "AC-1, A-1", cwd=repo)
        p = sh(*lean, "request", "REQUIREMENTS", cwd=repo, check=False)
        import re
        m = re.search(r"approve REQUIREMENTS ([0-9a-f]{8})", p.stdout + p.stderr)
        self.assertTrue(m, p.stdout + p.stderr)
        import subprocess
        subprocess.run(lean + ["hook", "UserPromptSubmit"], cwd=repo, text=True, capture_output=True,
                       input=json.dumps({"prompt": "approve REQUIREMENTS %s" % m.group(1), "session_id": "s1"}))
        b = subprocess.run(lean + ["hook", "PreToolUse"], cwd=repo, text=True, capture_output=True,
                           input=json.dumps({"tool_name": "fs_write", "tool_input": {"path": "src/refund.py"},
                                             "session_id": "s1"}))
        self.assertEqual(b.returncode, 2, b.stderr)               # design not approved yet: Lean blocks the write
        t = trace.build(repo, cfg, "PAY-1")
        self.assertEqual({c["chain"] for c in t["src"]["lean_audit"].values()}, {"ok"})
        self.assertIn(("requirements", "done"), [(s["id"], s["state"]) for s in t["stages"]])
        self.assertEqual([(h["kind"], h["what"], h["channel"]) for h in t["humans"]],
                         [("approval", "REQUIREMENTS", "local")])
        self.assertEqual([x["gate"] for x in t["actions"]["blocked"]], ["design"])
        self.assertEqual([d["id"] for d in t["decisions"] if d["source"] == "requirements.md"], ["A-1"])
        self.assertEqual(t["actions"]["turns"]["HUMAN_TURN"], {"before_handover": 1})
        self.assertNotIn("@", json.dumps(t["humans"]))


if __name__ == "__main__":
    unittest.main()
