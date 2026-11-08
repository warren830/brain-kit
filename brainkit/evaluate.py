"""`brain eval`: does the Brain-driven flow work? (plan §F, Eval v1). Built on the run traces (brainkit.trace) and
Brain usage (brainkit.usage); read-only.

  自主度            completed hand-over runs with no human act after hand-over beyond the required approvals
                    (Lean REQUIREMENTS / DESIGN / INCEPTION / PLAN / RELEASE, acceptance) and delivered correctly
                    ÷ completed hand-over runs whose correctness is known
  交付正确率        merged runs accepted the first time and with no revert / hotfix / defect claim within 14 days
                    ÷ merged runs whose 14 days are over and whose acceptance is on record
  介入落在判断点    human acts at judgment points (Lean approvals, rejections, refused approvals; bridge answers to
                    park questions) ÷ all human acts (also: people's commits on the run branch after hand-over,
                    "allow test change", KiroCrew manual stop / restart when the app passes them in)
  重复纠正率 / 知识有用率   from brainkit.usage
  过时与冲突        stale citations + same-term lint warnings + entries replaced / withdrawn in the period
  credits           total credits of the completed runs ÷ correct deliveries (diagnostic only, no cap)
  决策漏斗          every decision in the period, by who settled it: person > check > brain (cites an entry) >
                    agent (decisions.md Who=agent, assumptions A-n) > unknown; per ISO week
  复看队列          runs that need a human look, each with its reasons
Answers given by an eval oracle (`eval_oracle: true`) are never counted.
"""
from __future__ import annotations

import hashlib
import statistics

from . import brainlint, entries as E, trace as T, usage
from .common import BrainError, JIRA_RE

REQUIRED = {"REQUIREMENTS", "DESIGN", "INCEPTION", "PLAN", "RELEASE", "requirements", "design", "plan", "acceptance"}
CATEGORIES = ("brain", "check", "agent", "person", "unknown")
CAT_LABEL = {"brain": "Brain 条目（引用）", "check": "确定性检查", "agent": "agent 自己定（权限内）", "person": "人",
             "unknown": "看不出"}
SAME_TERM = "same name, different entries"
COST_OUTLIER_X = 2.0     # credits above this many times the median of the runs with credits
VERIFY_FAIL_ROUNDS = 3


def run_keys(repo, cfg, ref=None) -> list:
    tree = T.Tree(repo, ref)
    keys = set()
    for p in tree.files(cfg["runs_dir"]):
        parts = p.split("/")
        if len(parts) == 3 and parts[2] == "run.json" and JIRA_RE.match(parts[1]):
            keys.add(parts[1])
    return sorted(keys)


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
    """'correct' | 'wrong' | 'pending' (14 days not over) | 'unknown' (no acceptance on record) | None (not merged)."""
    o = t["outcome"]
    if not o["merged_at"]["value"]:
        return None
    rv, ft = o["reverted_or_hotfixed_within_14d"]["value"], o["accepted"]["first_time"]
    if rv is True or ft is False:
        return "wrong"
    if ft is None:
        return "unknown"
    if rv is None:
        return "pending"
    return "correct"


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
    if d["by"] == "check":
        return "check"
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
    import os
    return E.Brain.load(os.path.join(repo, cfg["brain_dir"]))


def collect(repo, cfg, ref=None, since=None, traces=None, ledger_path=None, vouchers_dir=None, credits=None,
            as_of=None, allowed_signers=None, min_runs=3) -> dict:
    if since is not None:
        since = T._utc(since)
        if not since:
            raise BrainError("--since must be a date like 2026-11-01")
    u = usage.collect(repo, cfg, ref=ref)
    if traces is None:
        traces = [T.build(repo, cfg, k, ref=ref, ledger_path=ledger_path, vouchers_dir=vouchers_dir, credits=credits,
                          as_of=as_of, allowed_signers=allowed_signers, _usage=u) for k in run_keys(repo, cfg, ref)]
    traces = sorted(traces, key=lambda t: t["key"])
    completed = [t for t in traces if done_at(t) and _in(done_at(t), since)]
    active = [t for t in traces if since is None or (last_at(t) and last_at(t) >= since)]

    # 交付正确率
    merged = [t for t in completed if t["outcome"]["merged_at"]["value"]]
    corr = {t["key"]: correctness(t) for t in merged}
    delivery = {"value": [sum(1 for v in corr.values() if v == "correct"),
                          sum(1 for v in corr.values() if v in ("correct", "wrong"))],
                "by_run": corr,
                "pending": sorted(k for k, v in corr.items() if v == "pending"),
                "unknown_acceptance": sorted(k for k, v in corr.items() if v == "unknown"),
                "rule": "一次通过验收（没有被驳回过的 RELEASE / 验收），合并后 14 天内没有回滚、点名这个需求的修复提交、"
                        "或点名它的新缺陷认领"}

    # 自主度
    rows, num, den = [], 0, 0
    for t in completed:
        if not t["outcome"]["handed_over"]["value"]:
            rows.append({"key": t["key"], "status": "local only"})
            continue
        c = correctness(t) if t["outcome"]["merged_at"]["value"] else (
            "wrong" if t["outcome"]["accepted"]["first_time"] is False else "pending")
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
            "tokens": None, "tokens_why": T.NO_TOKENS,
            "rule": "完成的 run 花的 credits 合计 ÷ 正确交付数；只用来诊断，不设上限"}

    # 决策漏斗
    counts = {c: 0 for c in CATEGORIES}
    weeks, unanswered, oracle = {}, 0, 0
    for t in traces:
        oracle += len(t.get("oracle") or [])
        for d in t["decisions"]:
            if not _in(d.get("at"), since):
                continue
            c = category(d)
            if c is None:
                unanswered += 1 if d["source"] == "bridge" else 0
                continue
            counts[c] += 1
            w = weeks.setdefault(week(d.get("at")), {x: 0 for x in CATEGORIES})
            w[c] += 1
    funnel = {"total": sum(counts.values()), "counts": counts, "weeks": dict(sorted(weeks.items())),
              "unanswered_questions": unanswered, "oracle_answers_excluded": oracle,
              "rule": "每条决策只算一次：人（桥送的回答、decisions.md 里 Who 是人）> 确定性检查（Who 是 verify/CI/lint/"
                      "gate）> Brain（引用了条目号）> agent（Who=agent，或需求页的假设 A-n）> 看不出（Who 空着）"}

    # 复看队列
    med = statistics.median([t["cost"]["credits"] for t in traces if t["cost"]["credits"] is not None]) \
        if sum(1 for t in traces if t["cost"]["credits"] is not None) >= 3 else None
    queue = []
    for t in active:
        rs = review_reasons(t, med)
        if rs:
            queue.append({"key": t["key"], "intent": t["intent"], "reasons": rs})
    queue.sort(key=lambda q: (-len(q["reasons"]), q["key"]))

    return {"ref": T.Tree(repo, ref).label, "since": since, "as_of": traces[0]["as_of"] if traces else None,
            "runs": len(traces), "completed": [t["key"] for t in completed],
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

def _gap(kind, title, body, refs, key=None):
    gid = "G-" + hashlib.sha256(("%s|%s" % (kind, title)).encode()).hexdigest()[:8]
    return {"id": gid, "kind": kind, "title": title, "body": body, "refs": sorted(set(r for r in refs if r)),
            "key": key, "acs": None}


def gaps(data) -> list:
    """Knowledge, test and golden-case gaps worth filing (deterministic; each has a stable id)."""
    out = []
    for t in data["traces"]:
        cited = set((t["brain"]["proposals"] or {}).get("correction_src") or [])
        for h in t["humans"]:
            if h["kind"] != "rejection":
                continue
            if (h.get("voucher_id") and h["voucher_id"] in cited) or (h.get("decision") and h["decision"] in cited):
                continue
            what = h.get("decision") or h.get("what") or "?"
            out.append(_gap("knowledge", "%s：%s 被驳回，但没有写成 correction" % (t["key"], what),
                            "人驳回过这一步，decisions.md 里没有引用它的 correction / repeats 提案。下一个 run 还会犯同样的错。"
                            "请写一条 correction（或 repeats 已有条目），走 brain reflect。",
                            [t["key"], h.get("decision"), h.get("voucher_id")] + (t["run_ids"] or []), t["key"]))
        rv = t["outcome"]["reverted_or_hotfixed_within_14d"]
        if t["outcome"]["accepted"]["rejections"] or rv["value"]:
            why = "验收被驳回" if t["outcome"]["accepted"]["rejections"] else "合并后 14 天内回滚或热修"
            out.append(_gap("golden", "%s 做成 golden 题（%s）" % (t["key"], why),
                            "base 用 run 开始时的 Brain commit %s，意图和验收标准取自 %s，隐藏测试用后来补上的测试。"
                            % ((t["brain_commit"] or "?")[:12], t["intent"] or "?"),
                            [t["key"], t["intent"]], t["key"]))
    for eid, n in data["usage"]["repeats_any"]:
        if n >= 2:
            out.append(_gap("test", "%s 重复了 %d 次：改成 gate" % (eid, n),
                            "同一个教训回来 %d 次了。写成会失败的检查（测试、lint、CI 规则），不要再加文字。" % n, [eid]))
    min_runs = data.get("min_runs", 3)
    for x in data["usage"]["entries"]:
        if not x["cites"] and x["loaded_by"] >= min_runs:
            out.append(_gap("knowledge", "%s 装载 %d 次、从没被引用：撤回候选" % (x["id"], x["loaded_by"]),
                            "这条知识没起作用。看一下是写得不清楚、放错了文件，还是该撤回（brain entry withdraw）。",
                            [x["id"]]))
    for s in data["staleness"]["stale"]:
        out.append(_gap("knowledge", "%s 引用了已归档的条目 %s" % (s["key"] or s["intent"], ", ".join(s["ids"])),
                        "run 用了过时的知识。核对它的决策是否还对，必要时开新需求修正。", [s["key"]] + s["ids"],
                        s["key"] or None))
    seen, uniq = set(), []
    for g in out:
        if g["id"] not in seen:
            seen.add(g["id"])
            uniq.append(g)
    return uniq


# ---------- text ----------

def _pct(a, b):
    return "%d/%d（%s）" % (a, b, "%.0f%%" % (100.0 * a / b) if b else "-")


def report(data) -> str:
    out = ["Eval（%s%s）：%d 个需求，完成 %d 个" % (data["ref"], "，从 %s 起" % data["since"] if data["since"] else "",
                                            data["runs"], len(data["completed"]))]
    cov = data["coverage"]
    lack = [n for n, ok in (("桥 ledger", cov["ledger"]), ("凭证", cov["vouchers"])) if not ok]
    if lack:
        out.append("没给：%s（桥上的问答数不到）" % "、".join(lack))
    a = data["autonomy"]
    out.append("")
    out.append("自主度：%s" % _pct(*a["value"]))
    out.append("  算法：" + a["rule"])
    for r in a["runs"]:
        if r["status"] != "counted":
            out.append("  %s：%s" % (r["key"], {"local only": "没交接（只在本地做），不算",
                                              "pending": "交付对不对还不知道"}.get(r["status"], r["status"])))
        elif r["beyond"]:
            out.append("  %s：交接后有人介入（%s）" % (r["key"], "、".join(r["beyond"])))
    d = data["delivery"]
    out.append("交付正确率：%s" % _pct(*d["value"]))
    if d["pending"]:
        out.append("  14 天还没满：" + ", ".join(d["pending"]))
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
    if c["credits_total"] is None:
        out.append("credits：没有数据（KiroCrew 没传）。token 数读不到：%s" % c["tokens_why"])
    else:
        out.append("credits：合计 %s（%d/%d 个完成的 run 有数），每个正确交付 %s。只用来诊断" % (
            c["credits_total"], c["runs_with_credits"], c["completed"],
            c["per_success"] if c["per_success"] is not None else "-（还没有正确交付）"))
    f = data["funnel"]
    out.append("")
    out.append("决策漏斗：%d 条" % f["total"])
    for k in CATEGORIES:
        if f["counts"][k]:
            out.append("  %s：%s" % (CAT_LABEL[k], _pct(f["counts"][k], f["total"])))
    if f["weeks"]:
        out.append("  每周（Brain / 检查 / agent / 人 / 看不出）：")
        out += ["    %s：%s" % (w, " / ".join(str(v[k]) for k in CATEGORIES)) for w, v in f["weeks"].items()]
    if f["unanswered_questions"]:
        out.append("  还没回答的问题：%d" % f["unanswered_questions"])
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
