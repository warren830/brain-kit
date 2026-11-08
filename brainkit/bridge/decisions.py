"""Decision loop over GitLab MR comments (no Jira or Feishu needed).

A run on the team runner that needs a person writes runs/<KEY>/park.json, pushes, and ends its turn:
  {"schema": 1, "status": "waiting", "kind": "clarify|design|plan|acceptance|action|other",
   "ask": "owner|product|builder|security|data", "question": "...", "options": [{"id": "A", "text": "..."}],
   "recommend": "A", "lean_request": "INCEPTION 8cf2cbce", "refs": ["D01#S003"]}
An acceptance question may carry "review_items": [{"id": "D-7", "text": "..."}, {"id": "A-3", "text": "..."}]: every
decision the agent took itself and every assumption, reviewed in one batch. They are posted as a checklist; the reply
`answer D-<n> approve` approves them all, `answer D-<n> reject A-3,D-7 [note]` rejects the listed ones (delivered to
the run as corrections; the voucher records rejected_items).
The bridge posts it on the MR as D-<n> (once GitLab has recorded the push as an MR version); the question's first
line names the commit and park.json's hash. It waits for a reply line `answer D-<n> <A|approve|reject> [note]`.
What counts is one pure function, check_answer(), which an outside signer can import too:
  bound          the question note is on the MR, is the text the bridge posts for park.json at that commit (not
                 edited), was posted after GitLab recorded that push, and no other note binds this D-n to another
                 run, commit or park.json (a D-n is never bound twice)
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
import sqlite3

from .. import sign
from ..common import SHA_RE, BrainError, iso, plain, utcnow, write_json
from ..intake import checks
from ..rest import gl_time, head_pushed_at

KINDS = ("clarify", "design", "plan", "acceptance", "action", "other")
LEAN_KINDS = ("REQUIREMENTS", "DESIGN", "PLAN", "RELEASE")   # INCEPTION: ask REQUIREMENTS and DESIGN separately
# A Lean approval is answered by the people for that approval, whatever kind the run wrote in park.json.
LEAN_KIND_ROLE = {"REQUIREMENTS": "requirements", "DESIGN": "design", "PLAN": "plan", "RELEASE": "acceptance"}
DEFAULT_ROLES = {"clarify": ["owner", "product"], "requirements": ["product"], "design": ["builder"],
                 "plan": ["builder"], "acceptance": ["product"], "action": ["security"], "other": ["owner"]}
ANSWER_RE = re.compile(r"^\s*(?:answer|答|回答)\s+(D-[1-9][0-9]*)\s+([A-H]|approve|reject)\b[ \t:：-]*(.*)$", re.I)
LEAN_REQ_RE = re.compile(r"^(%s) ([0-9a-f]{6,64})$" % "|".join(LEAN_KINDS))
ITEM_ID_RE = re.compile(r"^[AD]-[1-9][0-9]{0,3}$")     # review items: decisions D-n (Who = agent), assumptions A-n
ITEMS_RE = re.compile(r"^([AD]-[1-9][0-9]{0,3}(?:(?:\s*[,，、]\s*|\s+)[AD]-[1-9][0-9]{0,3})*)(?![0-9A-Za-z_-])"
                      r"[ \t:：,，、-]*(.*)$", re.I)
MAX_REVIEW_ITEMS = 50
KIND_LABEL = {"clarify": "澄清", "design": "设计", "plan": "计划", "acceptance": "验收", "action": "危险动作",
              "other": "其他"}
DECISION_ID_RE = re.compile(r"^D-[1-9][0-9]*$")
RUN_ID = r"RUN-[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}-[1-9][0-9]{0,5}"
USER = r"[a-z0-9][a-z0-9._-]{0,63}"
# the first line of a question note (question_text): decision, kind, run, commit and park.json hash, who answers
Q_HEAD_RE = re.compile(r"^\*\*(D-[1-9][0-9]*) · ([^·*\n]+) · (%s)\*\* · 提交 ([0-9a-f]{12}) · park ([0-9a-f]{12}) · "
                       r"需要 (@%s(?:、@%s)*) 回答$" % (RUN_ID, USER, USER))
# the bridge's note that ends a round with different answers: replies before it no longer count
CONFLICT_RE = re.compile(r"^(D-[1-9][0-9]*) 收到不同的回答（")

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, project TEXT NOT NULL, iid INTEGER NOT NULL, sha TEXT NOT NULL,
  park_hash TEXT NOT NULL, kind TEXT NOT NULL, lean_request TEXT, state TEXT NOT NULL, note_id INTEGER,
  answer TEXT, answerer TEXT, voucher_id TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
  qnote_id INTEGER, told_id INTEGER,
  UNIQUE (run_id, sha, park_hash));
"""
# note_id: replies after this note count (the question, or the end of a round); qnote_id: the question note;
# told_id: replies up to here have been told why they do not count. States: posting (row written, question not on
# the MR yet), open, answered, delivered, delivery_unknown, refused (the signer would not sign), stale, void, invalid,
# closed.
ADDED_COLUMNS = ("qnote_id", "told_id")


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
    items = park.get("review_items")
    if items is not None:
        kind = LEAN_KIND_ROLE.get(str(lr).split(" ", 1)[0]) if lr and LEAN_REQ_RE.match(str(lr)) else park.get("kind")
        if kind != "acceptance" or opts:
            p.append("review_items go with an acceptance question answered approve/reject (no options)")
        if not isinstance(items, list) or len(items) > MAX_REVIEW_ITEMS:
            p.append("review_items must be a list of at most %d" % MAX_REVIEW_ITEMS)
        else:
            ids = [i.get("id") if isinstance(i, dict) else None for i in items]
            if any(not ITEM_ID_RE.match(str(i)) for i in ids) or len(set(ids)) != len(ids):
                p.append("review_items ids must be distinct D-<n> (decisions) or A-<n> (assumptions)")
            if any(not isinstance(i, dict) or not plain(i.get("text")) or not str(i.get("text")).strip()
                   or len(i["text"]) > 200 for i in items):
                p.append("review_items text: one line, 1-200 characters")
    return p


def _block(text) -> str:
    """Run-written text, shown verbatim and inert (no quick actions, mentions or markdown)."""
    return "```text\n%s\n```" % str(text).replace("```", "ˋˋˋ")


def _inline(text) -> str:
    """One line of run-written text as inline code: no mentions, links or markdown; quick actions need a line start."""
    return "`%s`" % " ".join(str(text).split()).replace("`", "ˋ")


def _neutral_note(note) -> str:
    """A person's free-text note: one line, at most 300 chars, no line that reads as an approval or command."""
    one = " ".join(str(note or "").split())[:300]
    if checks.APPROVAL_LINE_RE.match(one) or checks.INJECTION_RE.search(one):
        one = "> " + one
    return one


# ---------- the rules, as pure functions (the bridge and an outside signer use the same ones) ----------

def effective_kind(kind, lean_request) -> str:
    """A Lean approval is answered by the people for that approval, whatever kind park.json names."""
    if lean_request:
        return LEAN_KIND_ROLE[str(lean_request).split(" ", 1)[0]]
    return kind


def allowed_people(kit, kind, owner, roles) -> set:
    """Who may answer a decision of `kind`: decision_roles from the kit (main), people from brain-authz roles.
    Never a bot, never the bridge's own account."""
    table = dict(DEFAULT_ROLES, **(kit.get("decision_roles") or {}))
    people = set()
    for r in table.get(kind, []):
        if r == "owner":
            people.add(owner)
        else:
            people.update(str(u).lower() for u in (roles or {}).get(r, []))
    people -= {str(u).lower() for u in kit.get("bot_users") or []}
    people.discard(str(kit.get("bridge_user") or "").lower())
    return people


def question_text(did, run_id, park, who, kind, sha, park_hash) -> str:
    """The question note for park.json. The first line binds it: decision id, run, the commit the question was asked
    at and park.json's sha256 (12 hex each), and who may answer."""
    label = {"requirements": "需求批准"}.get(kind, KIND_LABEL.get(kind, kind))
    lines = ["**%s · %s · %s** · 提交 %s · park %s · 需要 %s 回答" % (
        did, label, run_id, sha[:12], park_hash[:12], "、".join("@" + u for u in who))]
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
    items = park.get("review_items") or []
    if items:
        lines.append("待审项（agent 在权限内自己定的默认做法和假设，一次审完）：\n" + "\n".join(
            "- [ ] %s %s" % (i["id"], _inline(i["text"])) for i in items))
    how = "`answer %s A`" % did if park.get("options") else "`answer %s approve` 或 `answer %s reject`" % (did, did)
    if items:
        how = ("`answer %s approve`（全部通过），或 `answer %s reject %s 说明`（只驳回列出的项，驳回的项作为纠正送回 run；"
               "不列项就是整个驳回）" % (did, did, ",".join(i["id"] for i in items[:2])))
    lines.append("回复一行 %s，后面可以加一句说明。只认第一条有效回复；代码有新推送时，这个问题作废。" % how)
    return "\n\n".join(lines)


def conflict_text(did, valid) -> str:
    return "%s 收到不同的回答（%s），都不送。请商量后由一个人重新回答。" % (
        did, "、".join("@%s=%s%s" % (a, c, " " + ",".join(items) if items else "")
                      for a, (c, _, _, items) in sorted(valid.items())))


class Verdict:
    """What check_answer found.

    status  answer    deliver it: author, choice, note (the reply's free text), items (rejected review items, in
                      park.json's order), review (every review item id), note_id (the reply)
            none      no reply counts yet
            conflict  allowed people answered differently: conflict = {author: (choice, note, note_id, items)}
            stale     the MR head moved since the question
            void      the question is not bound to its push and park.json (reason says why): no reply to it counts
    reason   why it is not an answer (English, ids and hashes only)
    feedback [(note id, text)]: replies that do not count and why (to tell the people who wrote them)
    last_id  the newest note looked at
    """

    def __init__(self, status, reason="", **kw):
        self.status, self.reason = status, reason
        self.author, self.choice, self.note = kw.get("author", ""), kw.get("choice", ""), kw.get("note", "")
        self.items, self.review = tuple(kw.get("items", ())), list(kw.get("review", ()))
        self.note_id, self.last_id = kw.get("note_id", 0), kw.get("last_id", 0)
        self.feedback, self.conflict = list(kw.get("feedback", ())), dict(kw.get("conflict", {}))
        self.kind, self.who, self.question = kw.get("kind", ""), set(kw.get("who", ())), kw.get("question")

    @property
    def ok(self) -> bool:
        return self.status == "answer"

    def __repr__(self):
        return "Verdict(%s%s)" % (self.status, ", " + self.reason if self.reason else "")


def _nid(note) -> int:
    try:
        return int(note.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _author(note) -> str:
    return str(((note.get("author") or {}).get("username")) or "").lower()


def _first(note) -> str:
    return str(note.get("body") or "").split("\n", 1)[0].rstrip("\r")


def _norm(text) -> str:
    return "\n".join(l.rstrip() for l in str(text or "").replace("\r\n", "\n").split("\n")).strip()


def check_answer(decision, park_raw, notes, kit, roles, owner, head, pushed_at, question_id=None, after=None):
    """Does an answer on the MR count for this decision, and which one? Pure: no GitLab, no ledger, no clock.

    decision     {"id": "D-5", "run_id": "RUN-PAY-3-1", "sha": <40 hex>, "park_hash": <sha256 hex of park.json>}
                 (a ledger row, or an answer voucher's decision/run_id/sha/park_hash); "kind" and "lean_request",
                 when given, must agree with park.json
    park_raw     the bytes of runs/<KEY>/park.json at that sha, or None
    notes        every note on the MR, as GitLab returns them (id, body, author.username, created_at, system)
    kit          the kit config from the default branch (decision_roles, bot_users, bridge_user)
    roles        brain-authz roles: {"product": ["carol"], ...}
    owner        the run's owner
    head         the MR head now
    pushed_at    when GitLab recorded `sha` as the MR head (rest.head_pushed_at of the MR versions); None: unknown
    question_id  the note id the bridge recorded when it posted the question (None: it is found on the MR; then
                 kit bridge_user tells the bridge's notes from people's)
    after        only replies after this note count (the bridge's round: a conflict starts a new one); default the
                 question. With bridge_user known, the bridge's own conflict notes end a round as well.
    -> Verdict
    """
    did, run_id = str(decision.get("id") or ""), str(decision.get("run_id") or "")
    sha, ph = str(decision.get("sha") or ""), str(decision.get("park_hash") or "")

    def void(why, **kw):
        return Verdict("void", why, **kw)

    if not DECISION_ID_RE.match(did) or not re.match(r"^%s$" % RUN_ID, run_id) or not SHA_RE.match(sha) \
            or not re.match(r"^[0-9a-f]{64}$", ph):
        return void("the decision record is not well-formed")
    if head != sha:
        return Verdict("stale", "the MR head moved from %s to %s after the question" % (sha[:12], str(head)[:12]))
    if park_raw is None or hashlib.sha256(park_raw).hexdigest() != ph:
        return void("park.json at %s is not the one the question was asked from" % sha[:12])
    try:
        park = json.loads(park_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return void("park.json at %s is not JSON" % sha[:12])
    probs = validate_park(park)
    if probs or park.get("status") != "waiting":
        return void("park.json at %s is not a valid waiting question" % sha[:12])
    kind = effective_kind(park.get("kind"), park.get("lean_request"))
    if decision.get("kind") and decision["kind"] != kind:
        return void("the decision is of kind %s, park.json asks %s" % (decision["kind"], kind))
    if "lean_request" in decision.keys() and (decision["lean_request"] or None) != (park.get("lean_request") or None):
        return void("the decision's Lean request is not park.json's")
    who = allowed_people(kit, kind, owner, roles)
    if not who:
        return void("nobody may answer a %s decision" % kind)

    notes = sorted((n for n in notes or [] if isinstance(n, dict)), key=_nid)
    bridge = str(kit.get("bridge_user") or "").lower()
    if question_id:
        posted = [n for n in notes if _nid(n) == int(question_id)]
        if not posted:
            return void("the question note %s is not on the MR (deleted?)" % question_id)
        if bridge and _author(posted[0]) != bridge:
            return void("the question note was posted by %s, not by the bridge account %s"
                        % (_author(posted[0]), bridge))
        bridge = bridge or _author(posted[0])      # the bridge's account, as GitLab shows its own note
    asked = []
    for n in notes:
        m = Q_HEAD_RE.match(_first(n))
        if n.get("system") or not m or m.group(1) != did or (bridge and _author(n) != bridge):
            continue
        if (m.group(3), m.group(4), m.group(5)) != (run_id, sha[:12], ph[:12]):
            return void("%s on the MR is bound to %s @ %s (park %s): a decision id is never bound twice"
                        % (did, m.group(3), m.group(4), m.group(5)))
        want = question_text(did, run_id, park, [u[1:] for u in m.group(6).split("、")], kind, sha, ph)
        if _norm(n.get("body")) != _norm(want):
            return void("note %d is not the question the bridge posts for park.json at %s (edited?)"
                        % (_nid(n), sha[:12]))
        asked.append(n)
    if not asked:
        return void("no question %s for %s @ %s on the MR%s" % (did, run_id, sha[:12],
                                                               " by %s" % bridge if bridge else ""))
    if question_id and int(question_id) not in [_nid(n) for n in asked]:
        return void("note %s is not the question %s" % (question_id, did))
    q = asked[0]   # the first copy (a retried post may leave two equal ones)
    when, pushed = gl_time(q.get("created_at")), gl_time(pushed_at)
    if pushed is None:
        return void("GitLab shows no MR version with head %s" % sha[:12])
    if when is None or when < pushed:
        return void("%s was asked before %s was last pushed: replies to an older push do not count" % (did, sha[:12]))

    start = max(_nid(q), int(after or 0))
    if bridge:
        for n in notes:
            m = CONFLICT_RE.match(_first(n))
            if m and m.group(1) == did and _author(n) == bridge and _nid(n) > start:
                start = _nid(n)
    review = [i["id"] for i in park.get("review_items") or [] if isinstance(i, dict) and "id" in i] \
        if kind == "acceptance" else []
    options = {o.get("id") for o in park.get("options", []) if isinstance(o, dict)}
    valid, feedback, last = {}, [], start
    for n in notes:
        nid = _nid(n)
        if nid <= start or n.get("system"):
            continue
        last = max(last, nid)
        m = ANSWER_RE.match(str(n.get("body") or "").split("\n", 1)[0])
        if not m or m.group(1).upper() != did:
            continue
        author, choice = _author(n), m.group(2)
        choice = choice.lower() if choice.lower() in ("approve", "reject") else choice.upper()
        if author not in who:
            feedback.append((nid, "@%s 不在 %s 的可回答名单里，这条回复不算。" % (author, did)))
            continue
        if not (choice in ("approve", "reject") if park.get("lean_request") else choice in options):
            feedback.append((nid, "%s 没有选项 %s，这条回复不算。" % (did, choice)))
            continue
        note, items = m.group(3), ()
        im = ITEMS_RE.match(note or "") if review and choice == "reject" else None
        if im:
            listed = [x.upper() for x in re.split(r"[\s,，、]+", im.group(1)) if x]
            unknown = [i for i in listed if i not in review]
            if unknown:
                feedback.append((nid, "%s 没有待审项 %s，这条回复不算。" % (did, "、".join(unknown))))
                continue
            items = tuple(i for i in review if i in listed)     # the park's order, each once
            note = im.group(2)
        valid.setdefault(author, (choice, note, nid, items))
    common = dict(feedback=feedback, last_id=last, kind=kind, who=who, question=q, review=review)
    if not valid:
        return Verdict("none", "no reply counts yet", **common)
    if len({(c, items) for c, _, _, items in valid.values()}) > 1:
        return Verdict("conflict", "allowed people answered differently", conflict=valid, **common)
    author, (choice, note, nid, items) = min(valid.items(), key=lambda kv: kv[1][2])
    return Verdict("answer", author=author, choice=choice, note=note, items=items, note_id=nid, **common)


class DecisionLoop:
    """Mixed into Bridge: needs self.gl, self.ledger (db), self.kc, self.cfg, self.kit(), self.ids, self.roles,
    self.signer."""

    def _ddb(self):
        db = self.ledger.db
        if getattr(self, "_ddb_ready", False):
            return db
        with self.ledger.lock:
            db.executescript(SCHEMA)
            have = {r[1] for r in db.execute("PRAGMA table_info(decisions)")}
            for col in ADDED_COLUMNS:
                if col in have:
                    continue
                try:
                    db.execute("ALTER TABLE decisions ADD COLUMN %s INTEGER" % col)
                except sqlite3.OperationalError:      # another process added it meanwhile
                    continue
                if col == "qnote_id":
                    # Questions an older bridge asked name neither their commit nor park.json: ask them once more.
                    db.execute("UPDATE runs SET last_sha=NULL WHERE run_id IN "
                               "(SELECT run_id FROM decisions WHERE state='open')")
                    db.execute("UPDATE decisions SET state='posting' WHERE state='open'")
        self._ddb_ready = True
        return db

    effective_kind = staticmethod(effective_kind)

    def pseudonym(self, username) -> str:
        salt = self.cfg.get("pseudonym_salt", "")
        return "P-" + hashlib.sha256((salt + username).encode()).hexdigest()[:4].upper()

    def allowed(self, kit, kind, owner) -> set:
        return allowed_people(kit, kind, owner, self.roles)

    def question_text(self, did, row, park, who, kind, sha, park_hash) -> str:
        return question_text(did, row["run_id"], park, who, kind, sha, park_hash)

    def check_bridge_user(self, kit):
        """kit.json bridge_user must be the account this bridge's token posts as, or no answer would count."""
        want = str(kit.get("bridge_user") or "").lower()
        if not want:
            return
        if getattr(self, "_gitlab_me", None) is None:
            self._gitlab_me = str((self.gl.current_user() or {}).get("username") or "").lower()
        if self._gitlab_me != want:
            raise BrainError("kit.json bridge_user is %s but the bridge's GitLab token is %s: its questions would "
                             "not count" % (want, self._gitlab_me or "unknown"))

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
            old = db.execute("SELECT * FROM decisions WHERE run_id=? AND sha=? AND park_hash=?",
                             (row["run_id"], head, ph)).fetchone()
            if old is not None and old["state"] != "posting":
                seen()
                continue
            probs = validate_park(park)
            owner = row["owner"]
            kind = self.effective_kind(park.get("kind"), park.get("lean_request")) if not probs else "other"
            who = sorted(self.allowed(kit, kind, owner)) if not probs else []
            if not probs and not who:
                probs.append("没有人被配置为可以回答「%s」类决策（decision_roles / brain-authz roles）" % park.get("kind"))
            if not probs:
                self.check_bridge_user(kit)
                if head_pushed_at(self.gl.mr_versions(row["project"], row["iid"]), head) is None:
                    # asked before GitLab records the push, the question would read as older than its commit
                    self.out("%s: GitLab has no MR version for %s yet; asking next round" % (row["run_id"], head[:12]))
                    continue
            now = iso(utcnow())
            if old is not None:
                did = old["id"]
                if probs:
                    db.execute("UPDATE decisions SET state='invalid', updated=? WHERE id=?", (now, did))
            else:
                with self.ledger.lock:
                    did = "D-%d" % (db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] + 1)
                    db.execute("INSERT INTO decisions(id,run_id,project,iid,sha,park_hash,kind,lean_request,state,"
                               "created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (did, row["run_id"], row["project"], row["iid"], head, ph, kind,
                                park.get("lean_request"), "invalid" if probs else "posting", now, now))
            if probs:
                self.gl.comment(row["project"], row["iid"], "%s 无法发出（%s）：\n%s" % (
                    did, row["run_id"], "\n".join("- " + x for x in probs)))
                seen()
                continue
            # a failed post leaves the row 'posting' and the head unseen: the next round posts it again
            note = self.gl.comment(row["project"], row["iid"], question_text(did, row["run_id"], park, who, kind,
                                                                             head, ph))
            nid = int((note or {}).get("id") or 0)
            db.execute("UPDATE decisions SET state='open', qnote_id=?, note_id=?, told_id=?, updated=? "
                       "WHERE id=? AND state='posting'", (nid, nid, nid, iso(utcnow()), did))
            seen()
            posted += 1
            self.out("asked %s for %s (%s)" % (did, row["run_id"], ", ".join(who)))
        return posted

    # ---------- answers ----------

    def collect(self):
        """Check replies to open decisions (check_answer decides what counts) and deliver valid ones to the run."""
        db = self._ddb()
        delivered, errors = 0, []
        for d in db.execute("SELECT * FROM decisions WHERE state IN ('open', 'posting') ORDER BY created").fetchall():
            try:
                delivered += 1 if self._collect_one(db, d) else 0
            except BrainError as e:   # one MR GitLab will not show (or shows too much of) must not hold up the rest
                errors.append("%s: %s" % (d["id"], e))
        if errors:
            raise BrainError("collect: not checked this round: " + "; ".join(errors))
        return delivered

    def _collect_one(self, db, d) -> bool:
        row = self.ledger.run(d["run_id"])
        mr = self.gl.mr(d["project"], d["iid"])
        now = iso(utcnow())
        if mr.get("state") != "opened" or row is None or row["state"] != "dispatched":
            db.execute("UPDATE decisions SET state='closed', updated=? WHERE id=? AND state=?",
                       (now, d["id"], d["state"]))
            return False
        if mr.get("sha") != d["sha"]:
            if db.execute("UPDATE decisions SET state='stale', updated=? WHERE id=? AND state=?",
                          (now, d["id"], d["state"])).rowcount:
                self.gl.comment(d["project"], d["iid"], "%s 作废：问题发出后分支又有了新推送。run 需要时会重新提问。" % d["id"])
            return False
        if d["state"] == "posting":
            return False                                      # watch posts it
        kit = self.kit(d["project"])
        pushed = head_pushed_at(self.gl.mr_versions(d["project"], d["iid"]), d["sha"])
        if pushed is None:
            self.out("%s: GitLab shows no MR version for %s; looked at next round" % (d["id"], d["sha"][:12]))
            return False
        raw = self.gl.file_raw(d["project"], "%s/%s/park.json" % (kit["runs_dir"], row["req"]), d["sha"])
        notes = self.gl.mr_notes(d["project"], d["iid"])
        v = check_answer(dict(d), raw, notes, kit, self.roles, row["owner"], mr.get("sha", ""), pushed,
                         question_id=d["qnote_id"], after=d["note_id"])
        told = d["told_id"] or 0
        for nid, text in v.feedback:                          # each reply that does not count is told so once
            if nid > told:
                self.gl.comment(d["project"], d["iid"], text)
        if v.status in ("void", "stale"):
            if db.execute("UPDATE decisions SET state=?, told_id=?, updated=? WHERE id=? AND state='open'",
                          (v.status, max(told, v.last_id), now, d["id"])).rowcount:
                self.gl.comment(d["project"], d["iid"], "%s 作废（%s），对它的回答都不算。run 重新推送后会重新提问。"
                                % (d["id"], v.reason))
                self.out("%s void: %s" % (d["id"], v.reason))
            return False
        db.execute("UPDATE decisions SET told_id=? WHERE id=? AND state='open'", (max(told, v.last_id), d["id"]))
        if v.status == "none":
            # replies already looked at are not looked at (or answered) again
            db.execute("UPDATE decisions SET note_id=? WHERE id=? AND state='open'", (v.last_id, d["id"]))
            return False
        if v.status == "conflict":
            note = self.gl.comment(d["project"], d["iid"], conflict_text(d["id"], v.conflict))
            start = max(v.last_id, int((note or {}).get("id") or 0))   # a new round starts after the bridge's note
            db.execute("UPDATE decisions SET note_id=?, told_id=?, updated=? WHERE id=? AND state='open'",
                       (start, start, now, d["id"]))
            return False
        return self.deliver(d, row, v.author, v.choice, v.note, items=v.items, review=v.review)

    def deliver(self, d, row, author, choice, note, items=(), review=()) -> bool:
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
        if review:
            v["review_items"] = list(review)          # what this answer reviewed in one batch
        if items:
            v["rejected_items"] = list(items)         # rejected one by one; the rest stand
        v["voucher_id"] = "V-" + sign.digest(v)[:10]
        try:
            sig = self.signer.sign(v)
        except sign.SignerRefused as e:
            db.execute("UPDATE decisions SET state='refused', updated=? WHERE id=? AND state='answered'",
                       (iso(utcnow()), d["id"]))
            self.gl.comment(d["project"], d["iid"], "%s 的回答没有送回 run：签名服务不签（%s）。请负责人看一下签名服务的记录。"
                            % (d["id"], "；".join(e.reasons)[:500]))
            self.out("%s: the signer refused the answer: %s" % (d["id"], e))
            return False
        except sign.SignerUnavailable as e:
            # nothing was signed or sent: the question is open again and looked at next round
            db.execute("UPDATE decisions SET state='open', answer=NULL, answerer=NULL, updated=? "
                       "WHERE id=? AND state='answered'", (iso(utcnow()), d["id"]))
            self.out("%s: signer unavailable, the answer stays open: %s" % (d["id"], e))
            return False
        write_json(os.path.join(self.cfg["vouchers_dir"], d["run_id"], v["voucher_id"] + ".json"),
                   {"voucher": v, "sig": sig})
        if self.cfg.get("publish_vouchers"):
            self.publish(d["run_id"], v["voucher_id"])
        db.execute("UPDATE decisions SET voucher_id=? WHERE id=?", (v["voucher_id"], d["id"]))
        msg = self.answer_message(d, choice, note, pseud, v["voucher_id"], items=items, review=review)
        try:
            self.kc.send(row["slot"], msg, v["voucher_id"])
        except BrainError as e:
            db.execute("UPDATE decisions SET state='delivery_unknown', updated=? WHERE id=?", (iso(utcnow()), d["id"]))
            self.out("%s: delivery unknown, check slot %s by hand: %s" % (d["id"], row["slot"], e))
            return False
        db.execute("UPDATE decisions SET state='delivered', updated=? WHERE id=? AND state='answered'",
                   (iso(utcnow()), d["id"]))
        self.gl.comment(d["project"], d["iid"], "%s 已收到 @%s 的回答：%s%s，已送回 %s（凭证 %s）。"
                        % (d["id"], author, choice, "（驳回 %s）" % "、".join(items) if items else "",
                           d["run_id"], v["voucher_id"]))
        self.out("delivered %s=%s to %s" % (d["id"], choice, d["run_id"]))
        return True

    @staticmethod
    def answer_message(d, choice, note, pseud, voucher_id, items=(), review=()) -> str:
        """Ids, the choice and a neutralized one-line note. For a Lean request, the one approval line Lean reads."""
        lines = ["决策 %s 已答（凭证 %s，答复人 %s）：%s。" % (d["id"], voucher_id, pseud, choice)]
        if items:
            rest = [i for i in review if i not in items]
            lines.append("驳回的待审项：%s；%s。" % (", ".join(items), "其余待审项通过：%s" % ", ".join(rest)
                                                 if rest else "没有其余待审项"))
        elif review and choice == "approve":
            lines.append("待审项全部通过：%s。" % ", ".join(review))
        n = _neutral_note(note)
        if n:
            lines.append("说明：%s" % n)
        if items:
            lines.append("把驳回的每一项当作纠正：改掉它，在 decisions.md 记一行（写明 %s、%s 和项号），并为每一项写一个"
                         " type: correction 的 brain-proposal（src 写项号和 %s）；改完重新请求验收。"
                         % (d["id"], voucher_id, voucher_id))
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
