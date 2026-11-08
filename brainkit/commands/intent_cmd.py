"""`brain intent`: requirements from Builders (tech debt, alerts) and the system (scheduled checks, eval gaps).

  brain intent add --source alert|debt|schedule|eval-gap --title T (--body B | --body-file F)
                   [--acs AC ...] [--refs REF ...] [--key KEY] [--json]
                                   file an intent: claims the next free key (the same source, title and text once)
  brain intent list [--all] [--json]   open intents: claimed, no run started, not closed (--all: every claim)
  brain intent show KEY [--json]
  brain intent close KEY --reason R    the owner closes an intent (it stays, so it still dedupes)

Then `brain start <slug> --key KEY` starts a run on it; the run reads runs/<KEY>/intent.md. See docs/intents.md.
"""
from __future__ import annotations

import json
import sys

from .. import config, intents
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
                      owner=_me(repo, cfg))
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
        print("登记了 %s（%s）：%s\n开工：brain start <slug> --key %s" % (doc["key"], doc["source"], doc["title"],
                                                                 doc["key"]))
    return 0


def cmd_list(a):
    repo = repo_root()
    cfg = config.load(repo)
    docs, warn = intents.listing(repo, cfg)
    if not a.all:
        docs = [d for d in docs if d["status"] == "open"]
    if a.json:
        print(json.dumps(docs, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if warn:
        print("WARN", warn)
    if not docs:
        print("没有%s的需求。" % ("" if a.all else "待开工"))
        return 0
    for d in docs:
        closed = d.get("closed") if isinstance(d.get("closed"), dict) else {}
        print("%-10s %-8s %-9s %-14s %s%s" % (d["key"], d["status"], d["source"], d.get("owner", ""),
                                            d.get("title", ""), "（%s）" % closed.get("reason", "") if closed else ""))
    return 0


def cmd_show(a):
    repo = repo_root()
    doc = intents.get(repo, a.key)
    if not doc:
        raise BrainError("%s is not claimed" % a.key)
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(intents.render_md(doc).rstrip())
    if doc.get("closed"):
        c = doc["closed"]
        print("\n已关闭：%s（%s，%s）" % (c.get("reason", ""), c.get("by", ""), c.get("at", "")))
    return 0


def cmd_close(a):
    repo = repo_root()
    cfg = config.load(repo)
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
