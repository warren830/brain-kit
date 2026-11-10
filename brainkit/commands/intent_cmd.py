"""`brain intent`: requirements from Builders (tech debt, alerts) and the system (scheduled checks, eval gaps).

  brain intent add --source alert|debt|schedule|eval-gap --title T (--body B | --body-file F)
                   [--acs AC ...] [--refs REF ...] [--key KEY] [--unclaimed] [--json]
                                   file an intent: claims the next free key (the same source, title and text once);
                                   --unclaimed files it for others (待认领): `brain claim KEY` takes it
  brain intent list [--all] [--json]   open intents: claimed, no run started, not closed (--all: every claim);
                                       a big requirement split with `brain split` shows with its children (state,
                                       owner, dependencies) and its progress, n/m merged, until it is done
  brain intent show KEY [--json]
  brain intent close KEY --reason R    the owner closes an intent (it stays, so it still dedupes)

Then `brain start <slug> --key KEY` starts a run on it; the run reads runs/<KEY>/intent.md. See docs/intents.md.
"""
from __future__ import annotations

import json
import sys

from .. import config, intents, split
from ..common import BrainError, repo_root

CLI_SOURCES = ("alert", "debt", "schedule", "eval-gap")


def _body(a) -> str:
    if a.body_file:
        if a.body_file == "-":
            return sys.stdin.read()
        with open(a.body_file, encoding="utf-8") as f:
            return f.read()
    return a.body or ""


def _me(repo, cfg):
    from ..cli import _me as me
    return me(repo, cfg)


def cmd_add(a):
    repo = repo_root()
    cfg = config.load(repo)
    doc = intents.add(repo, cfg, a.source, a.title, _body(a), acs=a.acs, refs=a.refs, key=a.key,
                      owner=_me(repo, cfg), unclaimed=a.unclaimed)
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    for w in doc.get("warnings", []):
        print("WARN", w)
    if doc["duplicate"]:
        state = "，已关闭：%s" % doc["closed"].get("reason", "") if doc.get("closed") else ""
        print("已经登记过：%s（%s，%s%s）。同样的来源、标题和正文只登记一次。" % (
            doc["key"], doc.get("source", "product"), doc.get("owner"), state))
    else:
        if doc.get("owner") is None:
            print("登记了 %s（%s，待认领）：%s\n认领：brain claim %s" % (doc["key"], doc["source"], doc["title"],
                                                                doc["key"]))
        else:
            print("登记了 %s（%s）：%s\n开工：brain start <slug> --key %s" % (doc["key"], doc["source"], doc["title"],
                                                                     doc["key"]))
    return 0


STATE_TEXT = {"unclaimed": "待认领", "claimed": "%s 认领了", "in_progress": "%s 在做", "merged": "已合并",
              "closed": "已关闭"}
FAMILY_TEXT = {"in_progress": "进行中", "ready_to_release": "都合并了，等大需求评审和发布",
               "done": "已完成（发布了，小需求都合并了）", "closed": "已关闭"}


def state_text(d) -> str:
    t = STATE_TEXT.get(d.get("state"), d.get("state") or "?")
    return t % (d.get("owner") or "?") if "%s" in t else t


def shown(docs, fam, every) -> list:
    """What `intent list` shows: every claim with --all; otherwise the open ones (as before) and each big requirement
    that is not done yet, with its children (closed children left out)."""
    if every:
        return docs
    live = {k for k, f in fam.items() if f["state"] not in ("done", "closed")}
    return [d for d in docs if d["key"] in live or (d.get("parent") in live and d["state"] != "closed") or
            (d["status"] == "open" and d.get("parent") not in fam)]


def cmd_list(a):
    repo = repo_root()
    cfg = config.load(repo)
    docs, warn = intents.listing(repo, cfg)
    docs, fam = split.enrich(repo, cfg, docs)
    docs = shown(docs, fam, a.all)
    if a.json:
        print(json.dumps(docs, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if warn:
        print("WARN", warn)
    if not docs:
        print("没有%s的需求。" % ("" if a.all else "待开工"))
        return 0
    keys = {d["key"] for d in docs}
    by_key = {d["key"]: d for d in docs}
    for d in docs:
        if d.get("parent") in keys:
            continue                                   # printed under its parent
        closed = d.get("closed") if isinstance(d.get("closed"), dict) else {}
        line = "%-10s %-8s %-9s %-14s %s%s" % (d["key"], d["status"], d["source"], d.get("owner") or "-",
                                             d.get("title", ""), "（%s）" % closed.get("reason", "") if closed else "")
        f = fam.get(d["key"])
        if f:
            pr = f["progress"]
            line += "  · 大需求：已合并 %d/%d，%s" % (pr["merged"], pr["total"], FAMILY_TEXT.get(f["state"], f["state"])
                                                     if pr["total"] or not pr["closed"] else "小需求都关了")
        print(line)
        if not f:
            continue
        kids = [by_key[k] for k in f["children"] if k in by_key]
        for i, c in enumerate(kids):
            deps = []
            for k in c.get("depends_on") or []:
                dep = by_key.get(k)
                deps.append("%s（%s）" % (k, state_text(dep) if dep else "?"))
            print("  %s %-10s %-12s %-14s %s%s" % ("└" if i == len(kids) - 1 else "├", c["key"], c.get("unit") or "-",
                                                state_text(c), c.get("title", ""),
                                                "  依赖 " + "、".join(deps) if deps else ""))
    return 0


def cmd_show(a):
    repo = repo_root()
    doc = intents.get(repo, a.key)
    if not doc:
        raise BrainError("%s is not claimed" % a.key)
    filed_text = intents.is_intent(doc)     # as filed: listing() adds "source" to every claim, plain ones too
    family = None
    if doc.get("parent") or doc.get("source") == "split" or _has_children(repo, a.key):
        cfg = config.load(repo)
        docs, fam = split.enrich(repo, cfg, intents.listing(repo, cfg)[0])
        doc = next((d for d in docs if d["key"] == a.key), doc)
        family = fam.get(a.key) or fam.get(doc.get("parent"))
        by_key = {d["key"]: d for d in docs}
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if filed_text:
        print(intents.render_md(doc).rstrip())
    else:                                   # a plain claim (`brain start` / `brain claim`): no text was filed with it
        print("# %s · %s\n\nSource: product · claimed by %s at %s" % (doc.get("key"), doc.get("title", ""),
                                                                    doc.get("owner") or "-", doc.get("created_at")))
    if family and family["key"] == a.key:
        pr = family["progress"]
        print("\n大需求：已合并 %d/%d，%s" % (pr["merged"], pr["total"], FAMILY_TEXT.get(family["state"],
                                                                                     family["state"])))
        for k in family["children"]:
            c = by_key.get(k, {})
            print("  %-10s %-12s %s" % (k, c.get("unit") or "-", state_text(c)))
    elif family:
        print("\n%s 的小需求：%s%s" % (doc.get("parent"), state_text(doc), "；在等 %s 合并" % "、".join(
            doc["blocked_by"]) if doc.get("blocked_by") else ""))
    if doc.get("closed"):
        c = doc["closed"]
        print("\n已关闭：%s（%s，%s）" % (c.get("reason", ""), c.get("by", ""), c.get("at", "")))
    return 0


def _has_children(repo, key) -> bool:
    docs, _ = intents.scan(repo)
    return any(d.get("parent") == key for d in docs)


def cmd_close(a):
    repo = repo_root()
    cfg = config.load(repo)
    if _has_children(repo, a.key):
        docs = intents.listing(repo, cfg)[0]
        left = [d for d in split.children_of(docs, a.key) if d["state"] not in ("merged", "closed")]
        if left:
            raise BrainError("%s has children that are neither merged nor closed (%s): close them first (`brain intent "
                             "close <KEY>`), or let them merge" % (a.key, ", ".join("%s %s" % (d["key"], state_text(d))
                                                                                   for d in left)))
    doc = intents.close(repo, a.key, a.reason, by=_me(repo, cfg))
    print("%s 已关闭：%s" % (a.key, doc["closed"]["reason"]))
    return 0


def register(sub):
    s = sub.add_parser("intent", help="requirements from tech debt, alerts, scheduled checks and eval gaps")
    ss = s.add_subparsers(dest="op", required=True)
    x = ss.add_parser("add", help="file an intent (claims the next free key)")
    x.add_argument("--source", required=True, choices=CLI_SOURCES)
    x.add_argument("--title", required=True)
    g = x.add_mutually_exclusive_group(required=True)
    g.add_argument("--body")
    g.add_argument("--body-file", help="file with the text ('-' for stdin)")
    x.add_argument("--acs", nargs="+", action="extend", default=[], help="acceptance criteria, one per argument")
    x.add_argument("--refs", nargs="+", action="extend", default=[], help="references: paths, entry ids, links")
    x.add_argument("--key", help="claim this key instead of the next free one")
    x.add_argument("--unclaimed", action="store_true", help="file it for others (待认领): no owner until someone "
                                                            "runs `brain claim KEY`")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_add)
    x = ss.add_parser("list", help="open intents (claimed, no run yet, not closed)")
    x.add_argument("--all", action="store_true", help="every claim, with its status")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_list)
    x = ss.add_parser("show")
    x.add_argument("key")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_show)
    x = ss.add_parser("close", help="the owner closes an intent")
    x.add_argument("key")
    x.add_argument("--reason", required=True)
    x.set_defaults(fn=cmd_close)
    # `brain reflect --draft KEY` lives in cmd_reflect; its flag is added here so main() stays as it is.
    r = sub.choices.get("reflect")
    if r is not None and "--draft" not in r._option_string_actions:
        r.add_argument("--draft", metavar="KEY", help="print the instructions and the message for a reflector "
                                                       "session (local KiroCrew) on KEY's accepted run")
