"""Decision loop over GitLab MR comments (no Jira or Feishu needed).

A run on the team runner that needs a person writes runs/<KEY>/park.json, pushes, and ends its turn:
  {"schema": 1, "status": "waiting", "kind": "clarify|design|plan|acceptance|action|other",
   "ask": "owner|product|builder|security|data", "question": "...", "options": [{"id": "A", "text": "..."}],
   "recommend": "A", "lean_request": "INCEPTION 8cf2cbce", "refs": ["D01#S003"]}
The bridge posts it on the MR as D-<n>, waits for a reply line `answer D-<n> <A|approve|reject> [note]`, and checks:
  right person   the GitLab author is allowed for this kind of decision (decision_roles from main, not from the run)
  right question the reply names this decision id; the decision belongs to this run
  still current  the MR head has not moved since the question (otherwise the question is void)
  once only      the first valid reply wins (compare-and-set); later replies are told so;
                 different answers from different people in one round are not delivered (conflict)
  audit trail    a signed answer voucher in brain-intake, and a reply comment on the MR
Run-written text is posted inside a code block: GitLab quick actions and @mentions in it do nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from .. import sign
from ..common import BrainError, iso, utcnow, write_json
from ..intake import checks

KINDS = ("clarify", "design", "plan", "acceptance", "action", "other")
LEAN_KINDS = ("REQUIREMENTS", "DESIGN", "PLAN", "RELEASE")   # INCEPTION: ask REQUIREMENTS and DESIGN separately
# A Lean approval is answered by the people for that approval, whatever kind the run wrote in park.json.
LEAN_KIND_ROLE = {"REQUIREMENTS": "requirements", "DESIGN": "design", "PLAN": "plan", "RELEASE": "acceptance"}
DEFAULT_ROLES = {"clarify": ["owner", "product"], "requirements": ["product"], "design": ["builder"],
                 "plan": ["builder"], "acceptance": ["product"], "action": ["security"], "other": ["owner"]}
ANSWER_RE = re.compile(r"^\s*(?:answer|答|回答)\s+(D-[1-9][0-9]*)\s+([A-H]|approve|reject)\b[ \t:：-]*(.*)$", re.I)
LEAN_REQ_RE = re.compile(r"^(%s) ([0-9a-f]{6,64})$" % "|".join(LEAN_KINDS))
KIND_LABEL = {"clarify": "澄清", "design": "设计", "plan": "计划", "acceptance": "验收", "action": "危险动作",
              "other": "其他"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, project TEXT NOT NULL, iid INTEGER NOT NULL, sha TEXT NOT NULL,
  park_hash TEXT NOT NULL, kind TEXT NOT NULL, lean_request TEXT, state TEXT NOT NULL, note_id INTEGER,
  answer TEXT, answerer TEXT, voucher_id TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
  UNIQUE (run_id, sha, park_hash));
"""


def validate_park(park) -> list:
    p = []
    if not isinstance(park, dict) or park.get("schema") != 1:
        return ["park.json must be an object with schema 1"]
    if park.get("kind") not in KINDS:
        p.append("kind must be one of %s" % (KINDS,))
    q = park.get("question")
    if not isinstance(q, str) or not q.strip() or len(q) > 2000:
        p.append("question must be 1-2000 characters")
    opts = park.get("options", [])
    if not isinstance(opts, list) or len(opts) > 8:
        p.append("options must be a list of at most 8")
    else:
        ids = [o.get("id") for o in opts if isinstance(o, dict)]
        if len(ids) != len(opts) or len(set(ids)) != len(ids) or any(not re.match(r"^[A-H]$", str(i)) for i in ids):
            p.append("option ids must be distinct letters A-H")
        if any(len(str(o.get("text", ""))) > 500 for o in opts if isinstance(o, dict)):
            p.append("option text max 500 characters")
    lr = park.get("lean_request")
    if lr is not None and not LEAN_REQ_RE.match(str(lr)):
        p.append("lean_request must be '<KIND> <hash>' with KIND one of %s (INCEPTION: ask REQUIREMENTS and "
                 "DESIGN separately)" % (LEAN_KINDS,))
    if lr and opts:
        p.append("a Lean approval request is answered approve/reject; leave options empty")
    return p


def _block(text) -> str:
    """Run-written text, shown verbatim and inert (no quick actions, mentions or markdown)."""
    return "```text\n%s\n```" % str(text).replace("```", "ˋˋˋ")


def _neutral_note(note) -> str:
    """A person's free-text note: one line, at most 300 chars, no line that reads as an approval or command."""
    one = " ".join(str(note or "").split())[:300]
    if checks.APPROVAL_LINE_RE.match(one) or checks.INJECTION_RE.search(one):
        one = "> " + one
    return one


class DecisionLoop:
    """Mixed into Bridge: needs self.gl, self.ledger (db), self.kc, self.cfg, self.kit(), self.ids, self.roles."""

    def _ddb(self):
        self.ledger.db.executescript(SCHEMA)
        return self.ledger.db

    @staticmethod
    def effective_kind(kind, lean_request) -> str:
        if lean_request:
            return LEAN_KIND_ROLE[str(lean_request).split(" ", 1)[0]]
        return kind

    def pseudonym(self, username) -> str:
        salt = self.cfg.get("pseudonym_salt", "")
        return "P-" + hashlib.sha256((salt + username).encode()).hexdigest()[:4].upper()

    def allowed(self, kit, kind, owner) -> set:
        table = dict(DEFAULT_ROLES, **(kit.get("decision_roles") or {}))
        roles = table.get(kind, [])
        people = set()
        for r in roles:
            if r == "owner":
                people.add(owner)
            else:
                people.update(u.lower() for u in self.roles.get(r, []))
        return people - {u.lower() for u in kit["bot_users"]}

    # ---------- questions ----------

    def watch(self):
        """Post new park.json questions from dispatched runs."""
        db = self._ddb()
        posted = 0
        for row in self.ledger.runs_in("dispatched"):
            mr = self.gl.mr(row["project"], row["iid"])
            head = mr.get("sha", "")
            if head == row["last_sha"]:
                continue
            kit = self.kit(row["project"])
            raw = self.gl.file_raw(row["project"], "%s/%s/park.json" % (kit["runs_dir"], row["req"]), head)
            seen = lambda: db.execute("UPDATE runs SET last_sha=? WHERE run_id=?", (head, row["run_id"]))  # noqa
            if raw is None:
                seen()
                continue
            try:
                park = json.loads(raw.decode("utf-8"))
            except ValueError:
                self.gl.comment(row["project"], row["iid"], "run 写的 park.json 不是合法 JSON，已忽略。")
                seen()
                continue
            if not isinstance(park, dict) or park.get("status") != "waiting":
                seen()
                continue
            ph = hashlib.sha256(raw).hexdigest()
            if db.execute("SELECT 1 FROM decisions WHERE run_id=? AND sha=? AND park_hash=?",
                          (row["run_id"], head, ph)).fetchone():
                seen()
                continue
            probs = validate_park(park)
            owner = row["owner"]
            kind = self.effective_kind(park.get("kind"), park.get("lean_request")) if not probs else "other"
            who = sorted(self.allowed(kit, kind, owner)) if not probs else []
            if not probs and not who:
                probs.append("没有人被配置为可以回答「%s」类决策（decision_roles / brain-authz roles）" % park.get("kind"))
            n = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] + 1
            did = "D-%d" % n
            now = iso(utcnow())
            state = "invalid" if probs else "open"
            db.execute("INSERT INTO decisions(id,run_id,project,iid,sha,park_hash,kind,lean_request,state,created,updated)"
                       " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (did, row["run_id"], row["project"], row["iid"], head, ph,
                                                           kind, park.get("lean_request"), state,
                                                           now, now))
            if probs:
                self.gl.comment(row["project"], row["iid"], "%s 无法发出（%s）：\n%s" % (
                    did, row["run_id"], "\n".join("- " + x for x in probs)))
                seen()
                continue
            note = self.gl.comment(row["project"], row["iid"], self.question_text(did, row, park, who, kind))
            db.execute("UPDATE decisions SET note_id=? WHERE id=?", ((note or {}).get("id", 0), did))
            seen()
            posted += 1
            self.out("asked %s for %s (%s)" % (did, row["run_id"], ", ".join(who)))
        return posted

    def question_text(self, did, row, park, who, kind) -> str:
        label = {"requirements": "需求批准"}.get(kind, KIND_LABEL.get(kind, kind))
        lines = ["**%s · %s · %s** 需要 %s 回答" % (did, label, row["run_id"],
                                                  "、".join("@" + u for u in who))]
        body = [park["question"]]
        for o in park.get("options", []):
            body.append("%s. %s" % (o["id"], o.get("text", "")))
        if park.get("recommend"):
            body.append("推荐：%s" % park["recommend"])
        if park.get("lean_request"):
            body.append("审批：%s" % park["lean_request"])
        if park.get("refs"):
            body.append("依据：%s" % ", ".join(str(x) for x in park["refs"][:10]))
        lines.append(_block("\n".join(body)))
        how = "`answer %s A`" % did if park.get("options") else "`answer %s approve` 或 `answer %s reject`" % (did, did)
        lines.append("回复一行 %s，后面可以加一句说明。只认第一条有效回复；代码有新推送时，这个问题作废。" % how)
        return "\n\n".join(lines)

    # ---------- answers ----------

    def collect(self):
        """Check replies to open decisions and deliver valid ones to the run."""
        db = self._ddb()
        delivered = 0
        for d in db.execute("SELECT * FROM decisions WHERE state='open' ORDER BY created").fetchall():
            row = self.ledger.run(d["run_id"])
            kit = self.kit(d["project"])
            mr = self.gl.mr(d["project"], d["iid"])
            if mr.get("state") != "opened" or row is None or row["state"] != "dispatched":
                db.execute("UPDATE decisions SET state='closed', updated=? WHERE id=? AND state='open'",
                           (iso(utcnow()), d["id"]))
                continue
            if mr.get("sha") != d["sha"]:
                if db.execute("UPDATE decisions SET state='stale', updated=? WHERE id=? AND state='open'",
                              (iso(utcnow()), d["id"])).rowcount:
                    self.gl.comment(d["project"], d["iid"], "%s 作废：问题发出后分支又有了新推送。run 需要时会重新提问。" % d["id"])
                continue
            who = self.allowed(kit, d["kind"], row["owner"])
            valid, last_id = {}, d["note_id"] or 0
            for note in self.gl.mr_notes(d["project"], d["iid"]):
                nid = int(note.get("id", 0))
                if nid <= (d["note_id"] or 0) or note.get("system"):
                    continue
                last_id = max(last_id, nid)
                m = ANSWER_RE.match((note.get("body") or "").split("\n", 1)[0])
                if not m or m.group(1).upper() != d["id"]:
                    continue
                author = ((note.get("author") or {}).get("username") or "").lower()
                choice = m.group(2)
                choice = choice.lower() if choice.lower() in ("approve", "reject") else choice.upper()
                if author not in who:
                    self.gl.comment(d["project"], d["iid"], "@%s 不在 %s 的可回答名单里，这条回复不算。" % (author, d["id"]))
                    continue
                if not self._choice_ok(d, choice):
                    self.gl.comment(d["project"], d["iid"], "%s 没有选项 %s，这条回复不算。" % (d["id"], choice))
                    continue
                valid.setdefault(author, (choice, m.group(3), nid))
            if not valid:
                # replies already looked at are not looked at (or answered) again
                db.execute("UPDATE decisions SET note_id=? WHERE id=? AND state='open'", (last_id, d["id"]))
                continue
            choices = {c for c, _, _ in valid.values()}
            if len(choices) > 1:
                db.execute("UPDATE decisions SET note_id=?, updated=? WHERE id=?", (last_id, iso(utcnow()), d["id"]))
                self.gl.comment(d["project"], d["iid"], "%s 收到不同的回答（%s），都不送。请商量后由一个人重新回答。"
                                % (d["id"], "、".join("@%s=%s" % (a, c) for a, (c, _, _) in sorted(valid.items()))))
                continue
            author, (choice, note, nid) = sorted(valid.items(), key=lambda kv: kv[1][2])[0]
            if self.deliver(d, row, author, choice, note):
                delivered += 1
        return delivered

    def _choice_ok(self, d, choice) -> bool:
        if d["lean_request"]:
            return choice in ("approve", "reject")
        req = self._park_options(d)
        return choice in req

    def _park_options(self, d):
        kit = self.kit(d["project"])
        row = self.ledger.run(d["run_id"])
        raw = self.gl.file_raw(d["project"], "%s/%s/park.json" % (kit["runs_dir"], row["req"]), d["sha"])
        park = json.loads(raw.decode("utf-8")) if raw else {}
        return {o.get("id") for o in park.get("options", [])}

    def deliver(self, d, row, author, choice, note) -> bool:
        db = self.ledger.db
        if not db.execute("UPDATE decisions SET state='answered', answer=?, answerer=?, updated=? "
                          "WHERE id=? AND state='open'", (choice, author, iso(utcnow()), d["id"])).rowcount:
            return False  # someone else delivered it first
        pseud = self.pseudonym(author)
        v = {"schema": 1, "kind": "answer", "decision": d["id"], "run_id": d["run_id"], "project": d["project"],
             "mr_iid": d["iid"], "sha": d["sha"], "park_hash": d["park_hash"], "decision_kind": d["kind"],
             "choice": choice, "answerer": pseud, "issued_at": iso(utcnow()), "issuer": sign.PRINCIPAL}
        if d["lean_request"]:
            v["lean_request"] = d["lean_request"]
        v["voucher_id"] = "V-" + sign.digest(v)[:10]
        sig = sign.sign(v, self.cfg["signing_key"])
        write_json(os.path.join(self.cfg["vouchers_dir"], d["run_id"], v["voucher_id"] + ".json"),
                   {"voucher": v, "sig": sig})
        if self.cfg.get("publish_vouchers"):
            self.publish(d["run_id"], v["voucher_id"])
        db.execute("UPDATE decisions SET voucher_id=? WHERE id=?", (v["voucher_id"], d["id"]))
        msg = self.answer_message(d, choice, note, pseud, v["voucher_id"])
        try:
            self.kc.send(row["slot"], msg, v["voucher_id"])
        except BrainError as e:
            db.execute("UPDATE decisions SET state='delivery_unknown', updated=? WHERE id=?", (iso(utcnow()), d["id"]))
            self.out("%s: delivery unknown, check slot %s by hand: %s" % (d["id"], row["slot"], e))
            return False
        db.execute("UPDATE decisions SET state='delivered', updated=? WHERE id=? AND state='answered'",
                   (iso(utcnow()), d["id"]))
        self.gl.comment(d["project"], d["iid"], "%s 已收到 @%s 的回答：%s，已送回 %s（凭证 %s）。"
                        % (d["id"], author, choice, d["run_id"], v["voucher_id"]))
        self.out("delivered %s=%s to %s" % (d["id"], choice, d["run_id"]))
        return True

    @staticmethod
    def answer_message(d, choice, note, pseud, voucher_id) -> str:
        """Ids, the choice and a neutralized one-line note. For a Lean request, the one approval line Lean reads."""
        lines = ["决策 %s 已答（凭证 %s，答复人 %s）：%s。" % (d["id"], voucher_id, pseud, choice)]
        n = _neutral_note(note)
        if n:
            lines.append("说明：%s" % n)
        lines.append("把 park.json 的 status 改成 answered，按这个回答继续。")
        if d["lean_request"]:
            kind, h = d["lean_request"].split(" ", 1)
            verb = "approve" if choice == "approve" else "reject"
            lines.append("%s %s %s (via gitlab %s %s)" % (verb, kind, h, pseud, voucher_id))
        return "\n".join(lines)

    # ---------- report ----------

    def report(self) -> dict:
        db = self._ddb()
        out = {"runs": {}, "decisions": {}, "handoffs": {}}
        for r in db.execute("SELECT state, COUNT(*) FROM runs GROUP BY state"):
            out["runs"][r[0]] = r[1]
        for r in db.execute("SELECT state, COUNT(*) FROM decisions GROUP BY state"):
            out["decisions"][r[0]] = r[1]
        for r in db.execute("SELECT state, COUNT(*) FROM handoffs GROUP BY state"):
            out["handoffs"][r[0]] = r[1]
        reasons = {}
        for (reason,) in db.execute("SELECT reason FROM handoffs WHERE state='rejected'"):
            for part in (reason or "").split("; "):
                key = re.sub(r"[0-9a-f]{12,}|![0-9]+|\b[A-Z][A-Z0-9]*-[0-9]+\b", "…", part)[:80]
                reasons[key] = reasons.get(key, 0) + 1
        out["top_rejections"] = sorted(reasons.items(), key=lambda kv: -kv[1])[:5]
        waits = []
        for c, u in db.execute("SELECT created, updated FROM decisions WHERE state='delivered'"):
            from ..common import parse_iso
            waits.append((parse_iso(u) - parse_iso(c)).total_seconds() / 3600)
        out["answer_hours_median"] = sorted(waits)[len(waits) // 2] if waits else None
        per_run = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        runs = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        out["questions_per_run"] = round(per_run / runs, 2) if runs else None
        return out
