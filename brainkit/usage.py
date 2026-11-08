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
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from . import entries as E
from .common import git

CITE_RE = re.compile(r"(?<![A-Za-z0-9-])([PTIJ]-[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}-[1-9][0-9]{0,3})(?![0-9-])")
LEGACY_CITE_RE = re.compile(r"(?<![A-Za-z0-9-])([PTIJ]-[0-9]{1,5})(?![0-9-])")
PROPOSAL_BLOCK_RE = re.compile(r"^\s*```+\s*brain-(?:proposal|gap)\s*$.*?^\s*```+\s*$", re.M | re.S)


def strip_proposals(text) -> str:
    return PROPOSAL_BLOCK_RE.sub("", text)


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
                runs[rec["intent"]] = rec
    cites = {}                                    # intent -> {id: count}
    for path, text in _tree_files(repo, ref, "aidlc-docs").items():
        parts = path.split("/")
        if len(parts) < 3 or not path.endswith(".md"):
            continue
        intent = parts[1]
        body = strip_proposals(text)
        found = CITE_RE.findall(body) + [i for i in LEGACY_CITE_RE.findall(body) if i in legacy_known]
        if found:
            c = cites.setdefault(intent, {})
            for i in found:
                c[i] = c.get(i, 0) + 1
    loaded_cache = {}
    per_run, loaded_by = [], {}
    for intent in sorted(set(runs) | set(cites)):
        rec = runs.get(intent, {})
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
        cited = set(cites.get(intent, {}))
        for i in loaded or ():
            loaded_by.setdefault(i, set()).add(intent)
        known = set(current) | set(archived) | (loaded or set()) | old_archive
        # stale: already archived when the run started (it leaned on old knowledge). An entry archived later
        # was current when the run cited it, so that citation counts as use.
        stale = old_archive if loaded is not None else set(archived)
        per_run.append({"intent": intent, "key": rec.get("key", ""), "loaded": None if loaded is None else len(loaded),
                        "cited": sorted(cited & loaded) if loaded is not None else sorted(cited & set(current)),
                        "stale": sorted(cited & stale),
                        "unknown": sorted(i for i in cited if i not in known)})
    cited_by = {}
    for intent, c in cites.items():
        for i, n in c.items():
            row = cited_by.setdefault(i, {"count": 0, "runs": set()})
            row["count"] += n
            row["runs"].add(intent)
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
            out.append("  %s %s：%s，引用 %d 条%s" % (r["key"] or "-", r["intent"], loaded, len(r["cited"]),
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
    stale = [(r["intent"], r["stale"]) for r in data["runs"] if r["stale"]]
    if stale:
        out.append("")
        out.append("引用了已归档的条目（用了过时知识）：")
        out += ["  %s：%s" % (i, ", ".join(s)) for i, s in stale]
    unknown = [(r["intent"], r["unknown"]) for r in data["runs"] if r["unknown"]]
    if unknown:
        out.append("")
        out.append("引用了 Brain 里没有的条目号（写错了，或还没合并）：")
        out += ["  %s：%s" % (i, ", ".join(s)) for i, s in unknown]
    return "\n".join(out)
