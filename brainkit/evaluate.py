"""`brain eval`: does the Brain-driven flow work? (plan §F, Eval v1). Built on the run traces (brainkit.trace) and
Brain usage (brainkit.usage); read-only.

  自主度            completed hand-over runs with no human act after hand-over beyond the required approvals
                    (Lean REQUIREMENTS / DESIGN / INCEPTION / PLAN / RELEASE, acceptance) and delivered correctly
                    ÷ completed hand-over runs whose correctness is known
  交付正确率        merged runs accepted the first time and with no revert / hotfix / defect claim within 14 days
                    ÷ merged runs whose 14 days are over and whose acceptance is on record (a run already known to
                    be wrong waits for its 14 days like a clean one: listed, not counted yet)
  介入落在判断点    human acts at judgment points (Lean approvals, rejections, refused approvals; bridge answers to
                    park questions) ÷ all human acts (also: people's commits on the run branch after hand-over,
                    "allow test change", KiroCrew manual stop / restart when the app passes them in)
  重复纠正率 / 知识有用率   from brainkit.usage
  过时与冲突        stale citations + same-term lint warnings + entries replaced / withdrawn in the period
  credits           total credits of the completed runs ÷ correct deliveries (diagnostic only, no cap)
  决策漏斗          every decision in the period, by who settled it: person (bridge answers; Who = <pseudonym>
                    <voucher id>, or owner: the local half) > check > brain (Who = brain <id>, or the row cites an
                    entry) > agent (Who = agent, assumptions A-n) > unknown (Who empty); a Who outside that vocabulary
                    is "unclassified" and listed (key, id, where), never guessed; per ISO week
  复看队列          runs that need a human look, each with its reasons; also every unmerged run that is stuck (no
                    progress for --stuck-hours), closed, failed on the runner, in a *_unknown state, or whose bridge
                    question went void / invalid / unknown
  这一批            one row per requirement: state, autonomous or not, human acts at / outside judgment points,
                    questions asked, credits, delivered correctly or still open
Runs are found in the tree read (runs/<KEY>/run.json: merged ones, on main), on run branches here (<prefix>/*, local
and origin's: the unmerged ones, read from the branch) and in the bridge ledger (a run whose run.json is nowhere here
is listed as skipped, with its ledger state). Answers given by an eval oracle (`eval_oracle: true`) are never
counted. A run whose record cannot be read is skipped and listed with the reason (`skipped`); the others are still
evaluated.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import statistics
import urllib.parse

from . import brainlint, entries as E, trace as T, usage
from .common import BrainError, JIRA_RE, parse_branch, utcnow

REQUIRED = {"IDEATION", "REQUIREMENTS", "DESIGN", "INCEPTION", "PLAN", "RELEASE", "ideation", "requirements",
            "design", "plan", "acceptance"}
CATEGORIES = ("brain", "check", "agent", "person", "unknown", "unclassified")
CAT_LABEL = {"brain": "Brain 条目（引用）", "check": "确定性检查", "agent": "agent 自己定（权限内）", "person": "人",
             "unknown": "看不出（Who 空着）", "unclassified": "没归类（Who 不是规定的写法）"}
# a local Lean rejection has no voucher to cite: the decisions.md rows of the rejected stage, from that day on,
# are the ones that can record it. Lean's approvals and the stages they cover (aidlc_lean.py, "Stages in order"):
# INCEPTION = requirements + application-design, PLAN = code-generation; Stage cells name the stage
STAGE_WORDS = {"IDEATION": ("ideation", "intent", "scope", "构想"), "REQUIREMENTS": ("requirement",), "DESIGN": ("design",),
               "INCEPTION": ("inception", "requirement", "design"), "PLAN": ("plan", "code"),
               "RELEASE": ("release", "acceptance", "验收")}
WHO_WORDS = "agent / brain <条目号> / check / <化名> <凭证号> / owner（本地半程，负责人在对话里答的）"
SAME_TERM = "same name, different entries"
COST_OUTLIER_X = 2.0     # credits above this many times the median of the runs with credits
VERIFY_FAIL_ROUNDS = 3
STUCK_HOURS = 24         # an unmerged run with nothing new for this long is stuck (brain eval --stuck-hours)
# bridge decision states: no answer is expected any more (the funnel counts them apart from the open questions)
DROPPED = {"stale": "过时（MR 更新了，问题重发）", "void": "作废", "invalid": "无效", "closed": "MR 关了"}
# ... and the ones that leave an unmerged run stuck (review queue)
QUESTION_PROBLEMS = {"void": "作废", "invalid": "无效", "delivery_unknown": "送达不明", "refused": "签名服务拒签"}
STATE_LABEL = {"merged": "已合并", "accepted": "验收过了、没合并", "waiting": "在等回答", "open": "进行中",
               "closed": "MR 关了（没合并）", "failed": "运行机失败", "unknown": "派单结果不明",
               "merged_not_here": "桥说已合并（这里的 main 没有）", "not_readable": "读不到（只有桥上的记录）"}
DELIVERY_LABEL = {"correct": "对", "wrong": "不对", "pending": "14 天未满", "wrong_open": "有问题（14 天未满）",
                  "unknown": "没有验收记录", "open": "还没合并", "closed": "没合并"}


def run_keys(repo, cfg, ref=None) -> list:
    tree = T.Tree(repo, ref)
    keys = set()
    for p in tree.files(cfg["runs_dir"]):
        parts = p.split("/")
        if len(parts) == 3 and parts[2] == "run.json" and JIRA_RE.match(parts[1]):
            keys.add(parts[1])
    return sorted(keys)


def run_branches(repo, cfg, have=()) -> dict:
    """{key: ref to read} for the run branches here (<prefix>/<owner>/<KEY>-<slug>, local and origin's) of the
    requirements not in `have`: of each key, the branch whose tip has runs/<KEY>/run.json (naming that branch) and
    was committed last; of that branch's copies, the freshest (trace.freshest: origin's when they diverged)."""
    prefix, by = cfg["branch_prefix"], {}
    for r, sha in sorted(T._refs(repo).items()):
        space = next((x for x in ("refs/remotes/origin/", "refs/heads/") if r.startswith(x + prefix + "/")), None)
        b = parse_branch(r[len(space):], prefix) if space else None
        if b and b["key"] not in have and JIRA_RE.match(b["key"]):
            by.setdefault(b["key"], {}).setdefault(r[len(space):], []).append((r, sha))
    out = {}
    for key, branches in sorted(by.items()):
        best = None
        for name, copies in sorted(branches.items()):
            copies.sort(key=lambda x: not x[0].startswith("refs/remotes/"))
            tip, ref, _ = T.freshest(repo, copies)
            text = T._show(repo, tip, "%s/%s/run.json" % (cfg["runs_dir"], key))
            if text is None:
                continue
            try:
                rec = json.loads(text)
            except ValueError:
                rec = None                    # read anyway: the trace says why it cannot be used (skipped)
            if isinstance(rec, dict) and isinstance(rec.get("branch"), str) and rec["branch"] != name:
                continue
            when = T._cdate(repo, tip) or ""
            if best is None or when > best[0]:
                best = (when, ref)
        if best:
            out[key] = best[1]
    return out


def ledger_runs(path) -> dict:
    """{requirement key: [run rows, oldest first]} from the bridge ledger (opened read-only)."""
    if not path:
        return {}
    if not os.path.exists(path):
        raise BrainError("bridge ledger %s not found" % path)
    db = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(os.path.abspath(path)), uri=True)
    db.row_factory = sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
            return {}
        out = {}
        for r in db.execute("SELECT run_id, req, state, created, updated FROM runs ORDER BY created, run_id"):
            out.setdefault(str(r["req"]), []).append({"run_id": r["run_id"], "state": r["state"],
                                                      "created": T._utc(r["created"]),
                                                      "updated": T._utc(r["updated"]), "src": "bridge ledger runs"})
        return out
    finally:
        db.close()


def _in(at, since) -> bool:
    return since is None or (at is not None and at >= since)


def done_at(t):
    return t["outcome"]["merged_at"]["value"] or t["outcome"]["accepted"]["at"]


def last_at(t):
    ats = [s["at"] for s in t["stages"]] + [h["at"] for h in t["humans"]] + [d.get("at") for d in t["decisions"]]
    ats += [c["at"] for c in t["actions"]["commits"] or []] + [done_at(t)]
    ats = [a for a in ats if a]
    return max(ats) if ats else None


def correctness(t):
    """'correct' | 'wrong' (both only once the 14 days are over) | 'wrong_open' (already known wrong, 14 days not
    over) | 'pending' (14 days not over) | 'unknown' (no acceptance on record) | None (not merged).
    Only 'correct' and 'wrong' are counted: failures and successes of a period are judged at the same age."""
    o = t["outcome"]
    if not o["merged_at"]["value"]:
        return None
    r = o["reverted_or_hotfixed_within_14d"]
    rv, ft = r["value"], o["accepted"]["first_time"]
    if ft is None:
        return "unknown"
    bad = rv is True or ft is False
    if not r.get("window_closed"):
        return "wrong_open" if bad else "pending"
    return "wrong" if bad else "correct"


def beyond_required(t) -> list:
    """Human acts after hand-over other than the required approvals; None in the list = could not tell."""
    out = []
    for h in t["humans"]:
        if h["kind"] in ("approval", "approval_refused") and h["what"] in REQUIRED:
            continue
        if h["after_handover"] is True:
            out.append(h)
        elif h["after_handover"] is None:
            out.append(None)
    return out


def category(d):
    """Who settled a decision (None: counted elsewhere: an answer the bridge delivered, or not answered)."""
    if d["source"] == "bridge":
        return "person" if d.get("choice") else None
    if d.get("bridge_decision"):
        return None
    if d["by"] in ("person", "bridge"):
        return "person"
    if d["by"] in ("check", "brain", "unclassified"):
        return d["by"]
    if d["cites"]:
        return "brain"
    if d["by"] == "agent":
        return "agent"
    return "unknown"


def week(at):
    dt = T._dt(at) if at else None
    if not dt:
        return "no date"
    y, w, _ = dt.date().isocalendar()
    return "%04d-W%02d" % (y, w)


def _brain(repo, cfg, ref):
    if ref:
        return E.Brain.from_git(repo, ref, cfg["brain_dir"])
    return E.Brain.load(os.path.join(repo, cfg["brain_dir"]))


def build_traces(repo, cfg, ref=None, ledger_path=None, vouchers_dir=None, credits=None, as_of=None,
                 allowed_signers=None, _usage=None, found=None) -> tuple:
    """(traces, skipped, credits note). The runs: those in the tree read (`ref`), the unmerged ones on run branches
    here (each read from its branch), and the bridge ledger's (one whose run.json is nowhere here is skipped, with
    its ledger rows). A run whose record cannot be read is skipped with the reason, not fatal. One unkeyed credits
    record (a number, or {credits, turns, manual}) is one run's: with more runs it is not given to any of them (it
    would multiply), and the note says so. `found` (a dict) gets {"branches": {key: ref}, "ledger_only": [keys]}."""
    if ledger_path and not os.path.exists(ledger_path):
        raise BrainError("bridge ledger %s not found" % ledger_path)
    keys = run_keys(repo, cfg, ref)
    branches = run_branches(repo, cfg, set(keys))
    led = ledger_runs(ledger_path)
    gone = sorted(k for k in led if JIRA_RE.match(k) and k not in keys and k not in branches)
    if found is not None:
        found.update(branches=dict(branches), ledger_only=list(gone))
    note = None
    if T.credits_shape(credits) == "one" and len(keys) + len(branches) > 1:
        note = "--credits 只有一条记录，却有 %d 个需求：不知道是谁的，没有算给任何一个（请按 run 号或需求号给）" % (
            len(keys) + len(branches))
        credits = {"runs": {}}
    traces, skipped = [], []
    for k, r, u in [(k, ref, _usage) for k in keys] + [(k, branches[k], None) for k in sorted(branches)]:
        try:
            traces.append(T.build(repo, cfg, k, ref=r, ledger_path=ledger_path, vouchers_dir=vouchers_dir,
                                  credits=credits, as_of=as_of, allowed_signers=allowed_signers, _usage=u))
        except BrainError as e:
            skipped.append({"key": k, "why": str(e)})
        except (ValueError, TypeError, KeyError, AttributeError) as e:   # a record shaped in a way nobody foresaw
            skipped.append({"key": k, "why": "%s while reading it: %s" % (type(e).__name__, e)})
    for k in gone:
        rows = led[k]
        skipped.append({"key": k, "state": "not_readable", "bridge_runs": rows,
                        "why": "桥上有 %s（%s），这里读不到 %s/%s/run.json：不在 %s，也没有它的 %s/* 分支（要 fetch？）" % (
                            rows[-1]["run_id"], rows[-1]["state"], cfg["runs_dir"], k, T.Tree(repo, ref).label,
                            cfg["branch_prefix"])})
    return traces, skipped, note


def run_state(t, as_of=None, stuck_hours=STUCK_HOURS) -> dict:
    """Where a run is: {"value", "bridge" (the ledger state of its last run), "last_activity", "idle_hours",
    "stuck", "waiting_on"}. value: merged | merged_not_here (the ledger says merged; git here has no merge) |
    closed (MR closed, not merged) | failed (the runner could not start it) | unknown (a *_unknown ledger state: the
    bridge cannot tell whether it was sent) | waiting (parked on a question, or a bridge question is open) |
    accepted (accepted, not merged yet) | open. Stuck: not merged, closed or failed, and nothing new for
    `stuck_hours` (commits, Lean records, park questions, ledger rows, vouchers)."""
    o = t["outcome"]
    br = sorted(o.get("bridge_runs") or [], key=lambda r: (r.get("created") or "", r.get("run_id") or ""))
    last = str(br[-1]["state"]) if br else None
    la = (o.get("last_activity") or {}).get("at")
    now = as_of or T._dt(t.get("as_of")) or utcnow()
    idle = round((now - T._dt(la)).total_seconds() / 3600.0, 1) if la and T._dt(la) else None
    if o["merged_at"]["value"]:
        v = "merged"
    elif last in ("merged", "closed", "failed"):
        v = {"merged": "merged_not_here"}.get(last, last)
    elif last and last.endswith("_unknown"):
        v = "unknown"
    elif o.get("waiting_on"):
        v = "waiting"
    elif o["accepted"]["value"]:
        v = "accepted"
    else:
        v = "open"
    stuck = v in ("open", "waiting", "accepted", "unknown") and idle is not None and idle >= stuck_hours
    return {"value": v, "bridge": last, "last_activity": la, "idle_hours": idle, "stuck": stuck,
            "waiting_on": o.get("waiting_on")}


def open_reasons(t, st, stuck_hours=STUCK_HOURS) -> list:
    """Why an unmerged run needs a look (review queue): stuck, closed, failed on the runner, an unknown bridge state,
    a bridge question that went void / invalid / unknown, or merged by the ledger but not here."""
    out, v = [], st["value"]
    if v == "merged":
        return out
    if st["stuck"]:
        w = st["waiting_on"]
        out.append({"rule": "stuck", "text": "%.0f 小时没有进展（最后一次 %s，≥%g 小时算停住）%s" % (
            st["idle_hours"], st["last_activity"], stuck_hours,
            "，在等回答：%s%s" % (w.get("kind") or "?", " " + w["lean_request"] if w.get("lean_request") else "")
            if w else ""), "evidence": [(t["outcome"].get("last_activity") or {}).get("src")]})
    if v == "closed":
        out.append({"rule": "closed_unmerged", "text": "MR 关了，没有合并", "evidence": "bridge ledger runs"})
    if v == "failed":
        out.append({"rule": "runner_failed", "text": "运行机没能开始这个 run（桥上是 failed：brain-runnerd 失败）",
                    "evidence": "bridge ledger runs"})
    if v == "unknown":
        out.append({"rule": "state_unknown", "text": "桥不知道派单成没成（%s）：去 KiroCrew 看 slot" % st["bridge"],
                    "evidence": "bridge ledger runs"})
    if v == "merged_not_here":
        out.append({"rule": "merge_not_here", "text": "桥说 MR 合并了，这里的 main 没有它（要 fetch）",
                    "evidence": "bridge ledger runs"})
    bad = [d for d in t["decisions"] if d["source"] == "bridge" and d.get("state") in QUESTION_PROBLEMS]
    if bad:
        out.append({"rule": "question_problem", "text": "桥上的问题没走通：" + "、".join(
            "%s %s（%s）" % (d["id"], QUESTION_PROBLEMS[d["state"]], d["state"]) for d in bad),
            "evidence": [d["id"] for d in bad]})
    return out


def questions_asked(t) -> int:
    """How many questions the run parked on: distinct park.json questions, or bridge questions (the larger)."""
    park = {(q.get("kind"), q.get("lean_request"), q.get("question_sha256")) for q in t["actions"]["questions"] or []
            if q.get("kind") or q.get("question_sha256")}
    asked = {d["id"] for d in list(t["decisions"]) + list(t.get("oracle") or []) if d.get("source") == "bridge"}
    return max(len(park), len(asked))


def batch(traces, skipped, states) -> list:
    """One row per requirement (the batch summary): {key, state, stuck, idle_hours, handed_over, autonomous (None:
    not handed over, or a human act without a time), beyond (what people did after the hand-over besides the
    required approvals), judgment [at judgment points, outside], questions, credits, delivery (correct | wrong |
    pending | wrong_open | unknown | open | closed)}."""
    rows = []
    for t in sorted(traces, key=lambda x: x["key"]):
        st = states[t["key"]]
        handed = bool(t["outcome"]["handed_over"]["value"])
        b = beyond_required(t) if handed else []
        jp = [h["judgment_point"] for h in t["humans"]]
        rows.append({"key": t["key"], "state": st["value"], "stuck": st["stuck"], "idle_hours": st["idle_hours"],
                     "handed_over": handed, "autonomous": (None if None in b else not b) if handed else None,
                     "beyond": sorted({h["kind"] + (" " + h["what"] if h["what"] else "") for h in b if h}),
                     "judgment": [jp.count(True), jp.count(False)], "questions": questions_asked(t),
                     "credits": t["cost"]["credits"],
                     "delivery": correctness(t) if t["outcome"]["merged_at"]["value"] else (
                         "closed" if st["value"] == "closed" else "open")})
    for s in skipped:
        if s.get("state") == "not_readable":
            last = (s.get("bridge_runs") or [{}])[-1]
            rows.append({"key": s["key"], "state": "not_readable", "bridge": last.get("state"), "stuck": None,
                         "idle_hours": None, "handed_over": True, "autonomous": None, "beyond": [],
                         "judgment": None, "questions": None, "credits": None,
                         "delivery": "closed" if last.get("state") == "closed" else "open"})
    return sorted(rows, key=lambda r: r["key"])


def collect(repo, cfg, ref=None, since=None, traces=None, ledger_path=None, vouchers_dir=None, credits=None,
            as_of=None, allowed_signers=None, min_runs=3, stuck_hours=STUCK_HOURS) -> dict:
    if since is not None:
        since = T._utc(since)
        if not since:
            raise BrainError("--since must be a date like 2026-11-01")
    if as_of and not T._dt(as_of):
        raise BrainError("as_of %r is not an ISO time" % as_of)
    u = usage.collect(repo, cfg, ref=ref)
    skipped, credits_note, found = [], None, {}
    if traces is None:
        traces, skipped, credits_note = build_traces(repo, cfg, ref=ref, ledger_path=ledger_path,
                                                     vouchers_dir=vouchers_dir, credits=credits, as_of=as_of,
                                                     allowed_signers=allowed_signers, _usage=u, found=found)
    traces = sorted(traces, key=lambda t: t["key"])
    now = T._dt(traces[0]["as_of"]) if traces else (T._dt(as_of) if as_of else utcnow())
    states = {t["key"]: run_state(t, now, stuck_hours) for t in traces}
    completed = [t for t in traces if done_at(t) and _in(done_at(t), since)]
    # in the period: something happened in it, or the run is still open (stuck or not, it is part of the batch)
    active = [t for t in traces if since is None or (last_at(t) and last_at(t) >= since) or
              states[t["key"]]["value"] not in ("merged", "closed")]

    # 交付正确率
    merged = [t for t in completed if t["outcome"]["merged_at"]["value"]]
    corr = {t["key"]: correctness(t) for t in merged}
    delivery = {"value": [sum(1 for v in corr.values() if v == "correct"),
                          sum(1 for v in corr.values() if v in ("correct", "wrong"))],
                "by_run": corr,
                "pending": sorted(k for k, v in corr.items() if v == "pending"),
                "wrong_open": sorted(k for k, v in corr.items() if v == "wrong_open"),
                "unknown_acceptance": sorted(k for k, v in corr.items() if v == "unknown"),
                "rule": "一次通过验收（没有被驳回过的 RELEASE / 验收），合并后 14 天内没有回滚、点名这个需求的修复提交、"
                        "或点名它的新缺陷认领；只算 14 天已满、有验收记录的交付（已知出了问题、14 天还没满的，也等满了再算）"}

    # 自主度
    rows, num, den = [], 0, 0
    for t in completed:
        if not t["outcome"]["handed_over"]["value"]:
            rows.append({"key": t["key"], "status": "local only"})
            continue
        # not merged yet: known wrong or not, it waits for its merge and 14 days like a merged one (N11)
        c = correctness(t) if t["outcome"]["merged_at"]["value"] else (
            "wrong_open" if t["outcome"]["accepted"]["first_time"] is False else "pending")
        b = beyond_required(t)
        auto = None if None in b else not b
        row = {"key": t["key"], "correct": c, "autonomous": auto,
               "beyond": sorted({h["kind"] + (" " + h["what"] if h["what"] else "") for h in b if h})}
        if c in ("correct", "wrong") and auto is not None:
            den += 1
            num += 1 if auto and c == "correct" else 0
            row["status"] = "counted"
        else:
            row["status"] = "pending" if c not in ("correct", "wrong") else "unknown (human acts without a time)"
        rows.append(row)
    autonomy = {"value": [num, den], "runs": rows,
                "rule": "交接之后，除了必经的审批（REQUIREMENTS / DESIGN / INCEPTION / PLAN / RELEASE、验收），没有人做过"
                        "任何事，而且交付正确；本地半程是人自己带着 AI 做，不算介入"}

    # 介入落在判断点
    acts = [(t["key"], h) for t in traces for h in t["humans"] if _in(h["at"], since)]
    by_kind = {}
    for _, h in acts:
        k = h["kind"] + ("（判断点）" if h["judgment_point"] else "（判断点之外）")
        by_kind[k] = by_kind.get(k, 0) + 1
    judgment = {"value": [sum(1 for _, h in acts if h["judgment_point"]), len(acts)], "by_kind": by_kind,
                "outside": [{"key": k, "at": h["at"], "kind": h["kind"], "what": h["what"], "who": h["who"],
                             "sha": h.get("sha"), "src": h["src"]} for k, h in acts if not h["judgment_point"]],
                "rule": "判断点 = Lean 的审批口令（批准、驳回、没被接受的口令）和桥上的 park 问题；其余人做的事都算判断点之外",
                "not_measured": "复盘认定“确实需要人判断”这一半要人看；这里只看介入落没落在规则表上的点"}

    # 过时与冲突
    brain = _brain(repo, cfg, ref)
    _, warnings = brainlint.check(brain)
    same_term = sorted(w for w in warnings if SAME_TERM in w)
    archived = []
    for eid, (f, e) in sorted(brain.archived().items()):
        when = e.attrs.get("archived")
        if e.attrs.get("status") in ("replaced", "withdrawn") and (since is None or (when and when >= since[:10])):
            archived.append({"id": eid, "status": e.attrs.get("status"), "archived": when, "by": e.attrs.get("by")})
    stale = [{"intent": r["intent"], "key": r["key"], "ids": r["stale"]} for r in u["runs"] if r["stale"]]
    unknown = [{"intent": r["intent"], "key": r["key"], "ids": r["unknown"]} for r in u["runs"] if r["unknown"]]
    staleness = {"stale": stale, "same_term": same_term, "archived": archived, "unknown": unknown,
                 "total": sum(len(s["ids"]) for s in stale) + len(same_term) + len(archived),
                 "not_measured": "桥上“回答冲突”只发了 MR 评论，没进 ledger，这里数不到"}

    # credits
    with_c = [t for t in completed if t["cost"]["credits"] is not None]
    total_c = round(sum(t["cost"]["credits"] for t in with_c), 4) if with_c else None
    good = delivery["value"][0]
    cost = {"credits_total": total_c, "runs_with_credits": len(with_c), "completed": len(completed),
            "correct_deliveries": good,
            "per_success": round(total_c / good, 4) if total_c is not None and good else None,
            "tokens": None, "tokens_why": T.NO_TOKENS, "note": credits_note,
            "rule": "完成的 run 花的 credits 合计 ÷ 正确交付数；只用来诊断，不设上限"}

    # 决策漏斗
    counts = {c: 0 for c in CATEGORIES}
    weeks, unanswered, oracle, unclassified = {}, 0, 0, []
    dropped = {k: 0 for k in DROPPED}
    for t in traces:
        oracle += sum(1 for o in t.get("oracle") or [] if _in(o.get("at") or o.get("asked_at"), since))
        for d in t["decisions"]:
            c = category(d)
            if c is None:   # a question without an answer counts in the period it was asked
                if d["source"] == "bridge" and not d.get("choice") and _in(d.get("asked_at") or d.get("at"), since):
                    if d.get("state") in DROPPED:      # no answer is expected any more: not "unanswered"
                        dropped[d["state"]] += 1
                    else:
                        unanswered += 1
                continue
            if not _in(d.get("at"), since):
                continue
            counts[c] += 1
            w = weeks.setdefault(week(d.get("at")), {x: 0 for x in CATEGORIES})
            w[c] += 1
            if c == "unclassified":
                unclassified.append({"key": t["key"], "id": d["id"], "src": d["src"]})
    funnel = {"total": sum(counts.values()), "counts": counts, "weeks": dict(sorted(weeks.items())),
              "unanswered_questions": unanswered, "dropped_questions": dropped,
              "oracle_answers_excluded": oracle, "unclassified": unclassified,
              "rule": "每条决策只算一次：人（桥送的回答；Who 是“化名 凭证号”，或本地半程的 owner）> 确定性检查（Who=check）> Brain（Who=brain "
                      "条目号，或这一行引用了条目号）> agent（Who=agent，或需求页的假设 A-n）> 看不出（Who 空着）；Who 写了别的，"
                      "算“没归类”，列出在哪一行，不猜"}

    # 复看队列
    med = statistics.median([t["cost"]["credits"] for t in traces if t["cost"]["credits"] is not None]) \
        if sum(1 for t in traces if t["cost"]["credits"] is not None) >= 3 else None
    queue = []
    for t in active:
        rs = review_reasons(t, med) + open_reasons(t, states[t["key"]], stuck_hours)
        if rs:
            queue.append({"key": t["key"], "intent": t["intent"], "state": states[t["key"]]["value"], "reasons": rs})
    for s in skipped:
        if s.get("state") == "not_readable" and (s.get("bridge_runs") or [{}])[-1].get("state") != "closed":
            queue.append({"key": s["key"], "intent": None, "state": "not_readable",
                          "reasons": [{"rule": "not_readable", "text": s["why"], "evidence": "bridge ledger runs"}]})
    queue.sort(key=lambda q: (-len(q["reasons"]), q["key"]))

    return {"ref": T.Tree(repo, ref).label, "since": since, "as_of": traces[0]["as_of"] if traces else None,
            "runs": len(traces), "skipped": skipped, "completed": [t["key"] for t in completed],
            "states": {k: v for k, v in sorted(states.items())}, "stuck_hours": stuck_hours,
            "open_branches": found.get("branches", {}), "ledger_only": found.get("ledger_only", []),
            "batch": batch(active, skipped, states),
            "autonomy": autonomy, "delivery": delivery, "judgment": judgment,
            "repeat_rate": u["repeat_rate"], "useful": u["useful"],
            "useful_loaded": _per_run_useful(u), "staleness": staleness, "cost": cost, "funnel": funnel,
            "review_queue": queue, "min_runs": min_runs,
            "usage": {"entries": u["entries"], "runs": u["runs"], "repeats_any": u["repeats_any"]},
            "coverage": {"ledger": bool(ledger_path), "vouchers": bool(vouchers_dir),
                         "credits": sum(1 for t in traces if t["cost"]["credits"] is not None)},
            "traces": traces}


def _per_run_useful(u):
    rows = [(len(r["cited"]), r["loaded"]) for r in u["runs"] if r["loaded"]]
    return [sum(a for a, _ in rows), sum(b for _, b in rows)]


def review_reasons(t, median_credits=None) -> list:
    out = []
    o = t["outcome"]
    if o["accepted"]["rejections"]:
        out.append({"rule": "acceptance_rejected", "text": "验收被驳回 %d 次" % o["accepted"]["rejections"],
                    "evidence": o["accepted"]["src"]})
    rv = o["reverted_or_hotfixed_within_14d"]
    if rv["value"]:
        out.append({"rule": "reverted_or_hotfixed", "text": "合并后 14 天内有回滚/热修/缺陷",
                    "evidence": [x.get("sha") or x.get("claim") for x in rv["evidence"]]})
    vf = (t["checks"] or {}).get("verify_failed") or 0
    if vf >= VERIFY_FAIL_ROUNDS:
        out.append({"rule": "verify_fail_rounds", "text": "verify 失败 %d 次（≥%d）" % (vf, VERIFY_FAIL_ROUNDS),
                    "evidence": [r["src"] for r in t["checks"]["verify"] if not r["ok"]]})
    props = t["brain"]["proposals"] or {}
    rej = [h for h in t["humans"] if h["kind"] == "rejection"]
    if props.get("repeats") or len(rej) >= 2:
        out.append({"rule": "repeat_corrections",
                    "text": "同样的纠正又来了（%s）" % (", ".join(props.get("repeats") or []) or "被驳回 %d 次" % len(rej)),
                    "evidence": (props.get("repeats") or []) + [h["src"][0] for h in rej]})
    if t["brain"]["stale"]:
        out.append({"rule": "stale_citations", "text": "引用了已归档的条目：" + ", ".join(t["brain"]["stale"]),
                    "evidence": t["brain"]["src"]})
    outside = [h for h in t["humans"] if not h["judgment_point"]]
    if outside:
        out.append({"rule": "outside_judgment", "text": "判断点之外有人介入 %d 次（%s）" % (
            len(outside), "、".join(sorted({h["kind"] for h in outside}))), "evidence": [h["src"][0] for h in outside]})
    c = t["cost"]["credits"]
    if median_credits and c is not None and c > COST_OUTLIER_X * median_credits:
        out.append({"rule": "cost_outlier", "text": "credits %.1f，超过中位数 %.1f 的 %g 倍" % (
            c, median_credits, COST_OUTLIER_X), "evidence": t["cost"]["src"]})
    return out


# ---------- gaps worth filing ----------

def _gap(kind, subject, title, body, refs, key=None):
    """A gap's identity is its kind and subject (what it is about), never a count: the id is a hash of the two, and
    the title is made from them alone, so filing it again (eval gaps --file) finds the same one. Counts and other
    details that change from one eval to the next go in the body."""
    gid = "G-" + hashlib.sha256(("%s|%s" % (kind, subject)).encode()).hexdigest()[:8]
    return {"id": gid, "kind": kind, "subject": subject, "title": title, "body": body,
            "refs": sorted(set(r for r in refs if r)), "key": key, "acs": None}


def recorded_in(t, h) -> list:
    """decisions.md rows that can record a local Lean rejection (no voucher, no bridge decision to cite): rows of the
    rejected stage (STAGE_WORDS: REQUIREMENTS -> requirements; INCEPTION -> requirements or (application-)design;
    PLAN -> plan or code(-generation); RELEASE -> release / acceptance), dated that day or later (or undated)."""
    words = STAGE_WORDS.get(str(h.get("what") or "").upper())
    day = (h.get("at") or "")[:10]
    if not words:
        return []
    return [d["id"] for d in t["decisions"] if d["source"] == "decisions.md"
            and any(w in str(d.get("stage") or "").lower() for w in words) and (not d.get("at") or d["at"] >= day)]


LESSON = "下一个 run 还会犯同样的错。请写一条 correction（或 repeats 已有条目），走 brain reflect。"


def gaps(data) -> list:
    """Knowledge, test and golden-case gaps worth filing (deterministic; each has a stable id).
    A rejection is covered by a correction / repeats proposal whose src names: each rejected item (batch review:
    rejected_items, the same rule as brain reflect), else the answer's voucher or bridge decision; a local Lean
    rejection (nothing of its own to cite): a decisions.md row that records it (recorded_in)."""
    out = []
    for t in data["traces"]:
        cited = set((t["brain"]["proposals"] or {}).get("correction_src") or [])
        for h in t["humans"]:
            if h["kind"] != "rejection":
                continue
            refs = [t["key"], h.get("decision"), h.get("voucher_id")] + (t["run_ids"] or [])
            if h.get("rejected_items"):
                for it in h["rejected_items"]:
                    if it not in cited:
                        where = h.get("decision") or h.get("what") or "?"
                        out.append(_gap("knowledge", "rejected item|%s|%s|%s" % (t["key"], where, it),
                                        "%s：%s（%s 里）被驳回，但没有写成 correction" % (t["key"], it, where),
                                        "验收时人驳回了 %s（%s，凭证 %s），没有一条 correction / repeats 提案的 src 写着 %s。%s"
                                        % (it, where, h.get("voucher_id") or "-", it, LESSON), refs + [it], t["key"]))
                continue
            if (h.get("voucher_id") and h["voucher_id"] in cited) or (h.get("decision") and h["decision"] in cited):
                continue
            rows = [] if h.get("voucher_id") or h.get("decision") else recorded_in(t, h)
            if any(r in cited for r in rows):
                continue
            what = h.get("decision") or h.get("what") or "?"
            out.append(_gap("knowledge", "rejection|%s|%s" % (t["key"], what),
                            "%s：%s 被驳回，但没有写成 correction" % (t["key"], what),
                            "人驳回过这一步，decisions.md 里没有引用它的 correction / repeats 提案%s。%s" % (
                                "（本地驳回：提案的 src 写记下这次驳回的那一行，%s）" % "、".join(rows) if rows else "",
                                LESSON), refs, t["key"]))
        rv = t["outcome"]["reverted_or_hotfixed_within_14d"]
        if t["outcome"]["accepted"]["rejections"] or rv["value"]:
            why = "验收被驳回" if t["outcome"]["accepted"]["rejections"] else "合并后 14 天内回滚或热修"
            out.append(_gap("golden", "golden|%s" % t["key"], "%s 做成 golden 题" % t["key"],
                            "%s。base 用 run 开始时的 Brain commit %s，意图和验收标准取自 %s，隐藏测试用后来补上的测试。"
                            % (why, (t["brain_commit"] or "?")[:12], t["intent"] or "?"),
                            [t["key"], t["intent"]], t["key"]))
    for eid, n in data["usage"]["repeats_any"]:
        if n >= 2:
            out.append(_gap("test", "repeats|%s" % eid, "%s 一再重复：改成 gate" % eid,
                            "同一个教训回来 %d 次了。写成会失败的检查（测试、lint、CI 规则），不要再加文字。" % n, [eid]))
    min_runs = data.get("min_runs", 3)
    for x in data["usage"]["entries"]:
        if not x["cites"] and x["loaded_by"] >= min_runs:
            out.append(_gap("knowledge", "withdraw|%s" % x["id"], "%s 装载过、从没被引用：撤回候选" % x["id"],
                            "装载 %d 次，一次也没被引用。这条知识没起作用。看一下是写得不清楚、放错了文件，还是该撤回"
                            "（brain entry withdraw）。" % x["loaded_by"], [x["id"]]))
    for s in data["staleness"]["stale"]:
        who = s["key"] or s["intent"]
        out.append(_gap("knowledge", "stale|%s" % who, "%s 引用了已归档的条目" % who,
                        "引用了 %s。run 用了过时的知识。核对它的决策是否还对，必要时开新需求修正。" % ", ".join(s["ids"]),
                        [s["key"]] + s["ids"], s["key"] or None))
    seen, uniq = set(), []
    for g in out:
        if g["id"] not in seen:
            seen.add(g["id"])
            uniq.append(g)
    return uniq


# ---------- text ----------

def _pct(a, b):
    return "%d/%d（%s）" % (a, b, "%.0f%%" % (100.0 * a / b) if b else "-")


def _yes(v, handed=True) -> str:
    return "本地" if not handed else {True: "是", False: "否", None: "?"}[v]


def batch_report(data) -> list:
    """The batch summary: one line saying how many ran on their own, and one table row per requirement."""
    rows = data.get("batch")
    if not rows:
        return []
    handed = [r for r in rows if r["handed_over"] and r["state"] != "not_readable"]
    auto = [r for r in handed if r["autonomous"]]
    good = [r for r in auto if r["delivery"] == "correct"]
    out = ["", "这一批：%d 个需求；交接给运行机的 %d 个里，%d 个交接后除了必经的审批没人插手，其中 %d 个已合并、"
               "14 天后确认交付对" % (len(rows), len(handed), len(auto), len(good)),
           "  | 需求 | 状态 | 自主 | 判断点内 / 外 | 提问 | credits | 交付 |", "  |---|---|---|---|---|---|---|"]
    for r in rows:
        state = STATE_LABEL.get(r["state"], r["state"])
        if r["state"] == "not_readable" and r.get("bridge"):
            state += "：桥上 %s" % r["bridge"]
        if r.get("stuck"):
            state += "，%.0f 小时没动静" % r["idle_hours"]
        j = r.get("judgment")
        out.append("  | %s | %s | %s | %s | %s | %s | %s |" % (
            r["key"], state, _yes(r["autonomous"], r["handed_over"]) + ("（%s）" % "、".join(r["beyond"])
                                                                         if r["beyond"] else ""),
            "%d / %d" % tuple(j) if j else "-", "-" if r["questions"] is None else r["questions"],
            "-" if r["credits"] is None else r["credits"], DELIVERY_LABEL.get(r["delivery"], r["delivery"])))
    return out


def report(data) -> str:
    out = ["Eval（%s%s）：%d 个需求，完成 %d 个" % (data["ref"], "，从 %s 起" % data["since"] if data["since"] else "",
                                            data["runs"], len(data["completed"]))]
    for s in data.get("skipped") or []:
        out.append("跳过 %s（读不了，不算）：%s" % (s["key"], s["why"]))
    if data.get("open_branches"):
        out.append("没合并的 %d 个从分支读：%s" % (len(data["open_branches"]), "，".join(
            "%s（%s）" % (k, T._short_ref(r)) for k, r in sorted(data["open_branches"].items()))))
    cov = data["coverage"]
    lack = [n for n, ok in (("桥 ledger", cov["ledger"]), ("凭证", cov["vouchers"])) if not ok]
    if lack:
        out.append("没给：%s（桥上的问答数不到%s）" % ("、".join(lack), "；没合并、分支也不在这里的 run 看不到"
                                                       if not cov["ledger"] else ""))
    out += batch_report(data)
    a = data["autonomy"]
    out.append("")
    out.append("自主度：%s" % _pct(*a["value"]))
    out.append("  算法：" + a["rule"])
    for r in a["runs"]:
        if r["status"] != "counted":
            out.append("  %s：%s" % (r["key"], "已知有问题，合并满 14 天才算" if r.get("correct") == "wrong_open" else
                                     {"local only": "没交接（只在本地做），不算",
                                      "pending": "交付对不对还不知道"}.get(r["status"], r["status"])))
        elif r["beyond"]:
            out.append("  %s：交接后有人介入（%s）" % (r["key"], "、".join(r["beyond"])))
    d = data["delivery"]
    out.append("交付正确率：%s" % _pct(*d["value"]))
    if d["pending"]:
        out.append("  14 天还没满：" + ", ".join(d["pending"]))
    if d.get("wrong_open"):
        out.append("  已知有问题、14 天还没满（满了再算）：" + ", ".join(d["wrong_open"]))
    if d["unknown_acceptance"]:
        out.append("  没有验收记录（不算）：" + ", ".join(d["unknown_acceptance"]))
    j = data["judgment"]
    out.append("介入落在判断点的比例：%s" % _pct(*j["value"]))
    for o in j["outside"]:
        out.append("  判断点之外：%s %s %s%s" % (o["key"], o["at"] or "-", o["kind"], "（%s）" % o["who"] if o["who"] else ""))
    out.append("重复纠正率：%s" % _pct(*data["repeat_rate"]))
    out.append("知识有用率（被引用过的条目 / 现有条目）：%s；每个 run 引用 / 装载合计：%s" % (
        _pct(*data["useful"]), _pct(*data["useful_loaded"])))
    s = data["staleness"]
    out.append("过时与冲突：%d（过时引用 %d，同名术语 %d，本期覆盖或撤回 %d）" % (
        s["total"], sum(len(x["ids"]) for x in s["stale"]), len(s["same_term"]), len(s["archived"])))
    for x in s["stale"]:
        out.append("  %s 引用了已归档：%s" % (x["key"] or x["intent"], ", ".join(x["ids"])))
    for w in s["same_term"]:
        out.append("  " + w)
    c = data["cost"]
    if c.get("note"):
        out.append("credits：" + c["note"])
    elif c["credits_total"] is None:
        out.append("credits：没有数据（KiroCrew 没传）。token 数读不到：%s" % c["tokens_why"])
    else:
        out.append("credits：合计 %s（%d/%d 个完成的 run 有数），每个正确交付 %s。只用来诊断" % (
            c["credits_total"], c["runs_with_credits"], c["completed"],
            c["per_success"] if c["per_success"] is not None else "-（还没有正确交付）"))
    f = data["funnel"]
    out.append("")
    out.append("决策漏斗：%d 条" % f["total"])
    for k in CATEGORIES:
        if f["counts"].get(k):
            out.append("  %s：%s" % (CAT_LABEL[k], _pct(f["counts"][k], f["total"])))
    for x in f.get("unclassified") or []:
        out.append("    没归类：%s %s（%s）：Who 要写 %s" % (x["key"], x["id"], x["src"], WHO_WORDS))
    if f["weeks"]:
        out.append("  每周（Brain / 检查 / agent / 人 / 看不出 / 没归类）：")
        out += ["    %s：%s" % (w, " / ".join(str(v.get(k, 0)) for k in CATEGORIES)) for w, v in f["weeks"].items()]
    if f["unanswered_questions"]:
        out.append("  还没回答的问题：%d" % f["unanswered_questions"])
    gone = [(k, n) for k, n in (f.get("dropped_questions") or {}).items() if n]
    if gone:
        out.append("  不再等回答的问题（不算“还没回答”）：%s" % "，".join("%s %d" % (DROPPED.get(k, k), n)
                                                              for k, n in sorted(gone)))
    if f["oracle_answers_excluded"]:
        out.append("  考题的自动回答（eval_oracle）：%d 条，不算" % f["oracle_answers_excluded"])
    out.append("")
    if data["review_queue"]:
        out.append("要人复看的 run：")
        for q in data["review_queue"]:
            out.append("  %s：%s" % (q["key"], "；".join(r["text"] for r in q["reasons"])))
    else:
        out.append("要人复看的 run：没有")
    return "\n".join(out)
