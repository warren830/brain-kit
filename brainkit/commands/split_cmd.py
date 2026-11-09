"""`brain split` and `brain sync`: a big requirement split into children that different people build (split.py).

  brain split PARENT [--dry-run] [--json] [--no-push]
        on PARENT's run branch, after the DESIGN approval: file one child per unit of the approved unit-of-work.md
        (待认领: no owner), or bring the children in line with a newer approved split. Pushes the branch first, so
        the children can start from it. --dry-run shows what it would do and writes nothing.
  brain sync [--from REF]
        on a run branch: merge REF in (a child: default its parent's branch), Lean's generated files resolved
"""
from __future__ import annotations

import json

from .. import claims, config, split
from ..common import BrainError, git, repo_root

KIND_TEXT = {"new": "新登记（待认领）", "same": "没变", "update": "改了（还没人认领，已更新）",
             "changed": "拆分变了，但已经有人认领或开工：没改", "close": "单元没了，关掉（还没人认领）",
             "removed": "单元没了，但已经有人认领或开工：没关", "close_duplicate": "重复，关掉",
             "duplicate": "重复，但已经有人认领：没关", "raced": "同时拆了两次，这个关掉"}
ATTENTION = ("changed", "removed", "duplicate")


def _me(repo, cfg):
    from ..cli import _me as me
    return me(repo, cfg)


def _row(a, docs_by_key, units):
    key = a["key"] or "(新号)"
    f = a.get("fields") or {}
    paths = ", ".join(f.get("expected_paths") or (a.get("doc") or {}).get("expected_paths") or [])
    deps = ["%s（新）" % d[5:-1] if d.startswith("(new ") else d for d in (f.get("depends_on") or []) if d]
    owner = (a.get("doc") or {}).get("owner")
    line = "  %-10s %-12s %s%s%s%s" % (key, a["unit"] or "-", KIND_TEXT.get(a["kind"], a["kind"]),
                                       "  " + paths if paths and a["kind"] in ("new", "update", "same") else "",
                                       "  依赖 " + ", ".join(deps) if deps else "",
                                       "  （%s）" % owner if owner else "")
    out = [line]
    if a.get("why") and a["kind"] in ATTENTION + ("update", "raced"):
        out.append("             " + "；".join(a["why"]))
    return out


def cmd_split(a):
    repo = repo_root()
    cfg = config.load(repo)
    me = _me(repo, cfg)
    if me in cfg["bot_users"]:
        raise BrainError("%s is a bot account: bots never file or claim requirements" % me)
    p = split.plan(repo, cfg, me, a.parent)
    pushed = False
    if not a.dry_run and claims._has_origin(repo) and not a.no_push:
        # the children start from this branch on origin: it must hold the commit the split is read from
        git(repo, "push", "-q", "-u", "origin", p["branch"])
        pushed = True
    if not a.dry_run:
        p = split.apply(repo, cfg, me, p)
    info = p["info"]
    if a.json:
        print(json.dumps({"parent": p["parent"]["key"], "branch": p["branch"], "commit": p["commit"],
                          "dry_run": bool(a.dry_run), "units_sha256": info["units_sha256"],
                          "design_sha256": info["design_sha256"], "warnings": p["warnings"],
                          "actions": [{"unit": x["unit"], "kind": x["kind"], "key": x["key"],
                                       "why": x.get("why") or [],
                                       "fields": {k: v for k, v in (x.get("fields") or {}).items()
                                                  if k not in ("split",)}} for x in p["actions"]]},
                         ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    for w in p["warnings"]:
        print("WARN " + w)
    print("%s 拆成 %d 个小需求（unit-of-work.md %s，DESIGN %s 批过，%s）%s：" % (
        p["parent"]["key"], len(info["units"]), info["units_sha256"][:8], (info["design_sha256"] or "")[:8],
        (info["design_at"] or "")[:10], "（预览，什么都没登记）" if a.dry_run else ""))
    units = {u["name"]: u for u in info["units"]}
    for x in p["actions"]:
        for line in _row(x, {}, units):
            print(line)
    need = [x for x in p["actions"] if x["kind"] in ATTENTION]
    if need:
        print("要和认领的人说一声：拆分变了的小需求不会被自动改掉。开工时（brain start --key）按现在的拆分走；已经开工的，"
              "在自己的分支上 `brain sync` 拿到新的 unit-of-work.md；不做了就 `brain intent close <KEY>`。")
    if not a.dry_run:
        print("%s下一步：大家 `brain claim <KEY>` 认领、`brain start --key <KEY>` 开工；`brain intent list` 看进度。" % (
            "已推送 %s。" % p["branch"] if pushed else ""))
    return 0


def cmd_sync(a):
    repo = repo_root()
    cfg = config.load(repo)
    return split.sync(repo, cfg, a.from_ref, _me(repo, cfg))


def register(sub):
    s = sub.add_parser("split", help="file a big requirement's approved units as children others claim")
    s.add_argument("parent", metavar="PARENT")
    s.add_argument("--dry-run", action="store_true", help="show what it would file, update or close; write nothing")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-push", action="store_true", help="do not push the parent's branch first")
    s.set_defaults(fn=cmd_split)
    s = sub.add_parser("sync", help="merge the parent's branch (or --from REF) into this run branch; Lean's "
                                    "generated files resolved")
    s.add_argument("--from", dest="from_ref", metavar="REF")
    s.set_defaults(fn=cmd_sync)
