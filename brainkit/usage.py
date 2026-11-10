"""`brain usage`: is the Brain being used? (plan §F: 知识有用率, 重复纠正率, 过时与冲突)

A run uses an entry when its Lean records (aidlc-docs/<intent>/*.md, mostly decisions.md) cite the entry id.
Ids inside brain-proposal blocks do not count: proposing to change an entry is not using it.
For each run (runs/<KEY>/run.json) the entries it loaded are the Brain at its brain_commit.

  useful rate      entries cited at least once / current entries
  per run          cited / loaded at the run's brain commit
  repeat rate      sum of repeats on corrections / (corrections + their repeats); the target is near 0
  never cited      current entries loaded by at least --min-runs runs and never cited: withdraw candidates
  stale citations  runs citing entries that are archived now (they leaned on old knowledge)
  unknown ids      cited ids that are not in the Brain at all (typos, or entries from an unmerged MR)

One row per intent, the run whose run.json names it. A split (brainkit/split.py) has several runs on one intent: the
parent and its children (run.json "parent", "unit"). Then each run has its own row and the intent's pages are
attributed: construction/<unit>/... and construction/plans/<unit>-code-generation-plan.md (Lean's unit pages) to the
child of that unit, every other page (decisions.md included) to the parent, the run without "parent".
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from . import entries as E, lean, reflect
from .common import git

CITE_RE = re.compile(r"(?<![A-Za-z0-9-])([PTIJ]-[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}-[1-9][0-9]{0,3})(?![0-9-])")
LEGACY_CITE_RE = re.compile(r"(?<![A-Za-z0-9-])([PTIJ]-[0-9]{1,5})(?![0-9-])")
# Lean's unit pages (aidlc_lean.py STAGES, PLAN_FILE_RE): construction/<unit>/<stage>/..., the code-generation plan
UNIT_PAGE_RE = re.compile(r"^construction/(?:plans/(?P<plan>[^/]+)-code-generation-plan\.md|(?P<dir>[^/]+)/.+)$")
NOT_UNITS = ("plans", "build-and-test")          # Lean's RESERVED_UNITS: folders under construction/ that are not units


def strip_proposals(text) -> str:
    """The text without its brain-proposal and brain-gap blocks, cut where reflect cuts them: a code block inside
    one does not end it."""
    drop = set()
    for open_re in (reflect.FENCE_OPEN_RE, reflect.GAP_OPEN_RE):
        for start, end, _ in reflect.block_spans(text, open_re):
            drop.update(range(start, end))
    return "\n".join(line for i, line in enumerate(text.split("\n")) if i not in drop)


def _tree_files(repo, ref, prefix):
    """{path: text} for .md and run.json files under prefix, from the working tree (ref None) or a git ref."""
    out = {}
    if ref is None:
        root = os.path.join(repo, prefix)
        for d, _, files in os.walk(root):
            for name in files:
                if name.endswith(".md") or name == "run.json":
                    p = os.path.join(d, name)
                    with open(p, encoding="utf-8", errors="replace") as f:
                        out[os.path.relpath(p, repo)] = f.read()
        return out
    p = git(repo, "ls-tree", "-r", "-z", "--name-only", ref, "--", prefix, check=False)
    for path in p.stdout.split("\0"):
        if path.endswith(".md") or path.endswith("/run.json"):
            s = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (ref, path)], capture_output=True)
            if s.returncode == 0:
                out[path] = s.stdout.decode("utf-8", errors="replace")
    return out


def _read(repo, ref, rel) -> str:
    if ref is None:
        p = os.path.join(repo, rel)
        if not os.path.isfile(p):
            return ""
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    s = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (ref, rel)], capture_output=True)
    return s.stdout.decode("utf-8", errors="replace") if s.returncode == 0 else ""


def not_intents(repo, ref) -> set:
    """Lean's reserved(): the folders under aidlc-docs/ that are not intents. audit/, contracts/, and the contracts
    folder by its name in aidlc-docs/lean.json (contracts_dir)."""
    try:
        cfg = json.loads(_read(repo, ref, lean.CONFIG) or "{}")
    except ValueError:
        cfg = {}
    names = {"audit", "contracts"}
    parts = str((cfg if isinstance(cfg, dict) else {}).get("contracts_dir") or "").strip("/").split("/")
    if len(parts) > 1 and parts[0] == lean.DOCS:
        names.add(parts[1])
    return names


def page_unit(rel):
    """The unit a page of an intent is about (rel: its path under aidlc-docs/<intent>/), or None."""
    m = UNIT_PAGE_RE.match(rel)
    u = (m.group("plan") or m.group("dir")) if m else None
    return None if u in NOT_UNITS else u


def _is_child(rec) -> bool:
    return isinstance(rec.get("parent"), str) and bool(rec["parent"]) and isinstance(rec.get("unit"), str)


def _key_order(rec):
    m = re.match(r"^(.*)-([0-9]+)$", str(rec.get("key", "")))
    return (m.group(1), int(m.group(2))) if m else (str(rec.get("key", "")), 0)


def _add(into, ids):
    for i, n in ids.items():
        into[i] = into.get(i, 0) + n


def _rows_of(intent, recs, pages) -> list:
    """[(run.json, who counts as the run, {cited id: count})] for one intent. Without a split child: one row, the
    last run.json read, with every page. A split: one row per run, the unit pages to the child of that unit (each, if
    two name it), the other pages to the runs without "parent" (a row of its own, key "", when none is here)."""
    kids = [r for r in recs if _is_child(r)]
    if not kids:
        ids = {}
        for c in pages.values():
            _add(ids, c)
        return [(recs[-1] if recs else {}, intent, ids)]
    others = sorted((r for r in recs if not _is_child(r)), key=_key_order)
    kids.sort(key=_key_order)
    by_unit = {}
    for r in kids:
        by_unit.setdefault(r["unit"], []).append(r)
    every = others + kids
    at = {id(r): i for i, r in enumerate(every)}
    got, rest = [{} for _ in every], {}
    for rel, c in sorted(pages.items()):
        u = page_unit(rel)
        to = by_unit[u] if u in by_unit else others
        for r in to:
            _add(got[at[id(r)]], c)
        if not to:
            _add(rest, c)
    rows = [(r, (intent, r.get("key", "")), got[i]) for i, r in enumerate(every)]
    return ([({}, intent, rest)] if rest and not others else []) + rows


def collect(repo, cfg, ref=None) -> dict:
    brain = E.Brain.from_git(repo, ref, cfg["brain_dir"]) if ref else E.Brain.load(os.path.join(repo, cfg["brain_dir"]))
    current = brain.main()
    archived = brain.archived()
    legacy_known = {i for i in list(current) + list(archived) if E.LEGACY_ID_RE.match(i)}
    runs = {}
    for path, text in _tree_files(repo, ref, cfg["runs_dir"]).items():
        if path.endswith("run.json"):
            try:
                rec = json.loads(text)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("intent"), str) and rec["intent"]:
                if not isinstance(rec.get("brain_commit"), str):
                    rec = dict(rec, brain_commit=None)
                runs.setdefault(rec["intent"], []).append(rec)
    cites = {}                                    # intent -> {page under aidlc-docs/<intent>/: {id: count}}
    skip = not_intents(repo, ref)
    for path, text in _tree_files(repo, ref, lean.DOCS).items():
        parts = path.replace(os.sep, "/").split("/")
        if len(parts) < 3 or not path.endswith(".md") or parts[1] in skip:
            continue
        intent = parts[1]
        body = strip_proposals(text)
        found = CITE_RE.findall(body) + [i for i in LEGACY_CITE_RE.findall(body) if i in legacy_known]
        if found:
            c = cites.setdefault(intent, {}).setdefault("/".join(parts[2:]), {})
            for i in found:
                c[i] = c.get(i, 0) + 1
    loaded_cache = {}
    per_run, loaded_by, cited_by = [], {}, {}
    for intent in sorted(set(runs) | set(cites)):
        for rec, who, ids in _rows_of(intent, runs.get(intent, []), cites.get(intent, {})):
            bc = rec.get("brain_commit")
            loaded, old_archive = None, set()
            if bc:
                if bc not in loaded_cache:
                    ok = git(repo, "cat-file", "-e", bc + "^{commit}", check=False).returncode == 0
                    if ok:
                        then = E.Brain.from_git(repo, bc, cfg["brain_dir"])
                        loaded_cache[bc] = (set(then.main()), set(then.archived()))
                    else:
                        loaded_cache[bc] = (None, set())
                loaded, old_archive = loaded_cache[bc]
            cited = set(ids)
            for i in loaded or ():
                loaded_by.setdefault(i, set()).add(who)
            for i in cited:
                cited_by.setdefault(i, {"count": 0, "runs": set()})["runs"].add(who)
            known = set(current) | set(archived) | (loaded or set()) | old_archive
            # stale: already archived when the run started (it leaned on old knowledge). An entry archived later
            # was current when the run cited it, so that citation counts as use.
            stale = old_archive if loaded is not None else set(archived)
            row = {"intent": intent, "key": rec.get("key", ""), "loaded": None if loaded is None else len(loaded),
                   "cited": sorted(cited & loaded) if loaded is not None else sorted(cited & set(current)),
                   "stale": sorted(cited & stale),
                   "unknown": sorted(i for i in cited if i not in known)}
            if _is_child(rec):
                row.update(parent=rec["parent"], unit=rec["unit"])
            per_run.append(row)
    for intent, pages in cites.items():
        for c in pages.values():
            for i, n in c.items():
                cited_by.setdefault(i, {"count": 0, "runs": set()})["count"] += n
    entries = []
    for eid, (f, e) in sorted(current.items()):
        cb = cited_by.get(eid, {"count": 0, "runs": set()})
        entries.append({"id": eid, "file": f.key, "type": e.type, "repeats": max(e.repeats, 0),
                        "cites": cb["count"], "runs": len(cb["runs"]), "loaded_by": len(loaded_by.get(eid, ()))})
    corrections = [x for x in entries if x["type"] == "correction"]
    rep = sum(x["repeats"] for x in corrections)
    return {
        "ref": ref or "working tree",
        "entries": entries,
        "by_file": {k: sum(1 for x in entries if x["file"] == k) for k in E.FILES},
        "archived": len(archived), "purged": len(brain.purged()),
        "runs": per_run,
        "useful": [sum(1 for x in entries if x["cites"]), len(entries)],
        "repeat_rate": [rep, len(corrections) + rep],
        "repeats_any": sorted(((x["id"], x["repeats"]) for x in entries if x["repeats"]), key=lambda t: -t[1]),
    }


def _pct(a, b):
    return "%d/%d（%s）" % (a, b, "%.0f%%" % (100.0 * a / b) if b else "-")


def report(data, min_runs=3) -> str:
    out = ["Brain 用量（%s）" % data["ref"]]
    bf = data["by_file"]
    out.append("条目：%d 个（%s）；已归档 %d；已清除 %d" % (
        len(data["entries"]), "，".join("%s %d" % (k, v) for k, v in bf.items()), data["archived"], data["purged"]))
    out.append("run：%d 个" % len(data["runs"]))
    out.append("知识有用率（被引用过的条目 / 现有条目）：%s" % _pct(*data["useful"]))
    out.append("重复纠正率（correction 的 repeats / 全部纠正）：%s" % _pct(*data["repeat_rate"]))
    if data["runs"]:
        out.append("")
        out.append("每个 run：")
        for r in data["runs"]:
            loaded = "装载 %d 条" % r["loaded"] if r["loaded"] is not None else "装载数不明（没有 brain_commit）"
            out.append("  %s %s%s：%s，引用 %d 条%s" % (r["key"] or "-", r["intent"],
                                                    "（%s 的小需求，单元 %s，只算它的单元页）" % (r["parent"], r["unit"])
                                                    if r.get("parent") else "", loaded, len(r["cited"]),
                                                    "（%s）" % ", ".join(r["cited"]) if r["cited"] else ""))
    never = [x for x in data["entries"] if not x["cites"] and x["loaded_by"] >= min_runs]
    if never:
        out.append("")
        out.append("至少 %d 个 run 装载过、从没被引用（撤回候选）：" % min_runs)
        out += ["  %s（%s %s，装载 %d 次）" % (x["id"], x["file"], x["type"], x["loaded_by"]) for x in never]
    unused = [x for x in data["entries"] if not x["cites"] and x["loaded_by"] < min_runs]
    if unused:
        out.append("")
        out.append("还没被引用（装载过的 run 不到 %d 个，先不判断）：%s" % (min_runs, ", ".join(x["id"] for x in unused)))
    if data["repeats_any"]:
        out.append("")
        out.append("重复出现的教训：" + "，".join("%s ×%d%s" % (i, n, "（改成 gate）" if n >= 2 else "")
                                           for i, n in data["repeats_any"]))
    shared = {r["intent"] for r in data["runs"] if r.get("parent")}     # a split: several runs on one intent

    def who(r):
        return "%s %s" % (r["key"] or "-", r["intent"]) if r["intent"] in shared else r["intent"]
    stale = [(who(r), r["stale"]) for r in data["runs"] if r["stale"]]
    if stale:
        out.append("")
        out.append("引用了已归档的条目（用了过时知识）：")
        out += ["  %s：%s" % (i, ", ".join(s)) for i, s in stale]
    unknown = [(who(r), r["unknown"]) for r in data["runs"] if r["unknown"]]
    if unknown:
        out.append("")
        out.append("引用了 Brain 里没有的条目号（写错了，或还没合并）：")
        out += ["  %s：%s" % (i, ", ".join(s)) for i, s in unknown]
    return "\n".join(out)
