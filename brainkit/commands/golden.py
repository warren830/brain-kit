"""`brain golden`: golden tasks and ablation (see brainkit/golden.py).

  brain golden snapshot TASK_DIR --out DIR [--variant full|no-PRODUCT|no-TECH|no-IMPROVEMENT] [--repo PATH]
                       [--mask-secrets] [--json]
  brain golden score TASK_DIR WORKDIR [--json]
  brain golden plan --tasks DIR [--variants full,no-TECH] [--repeats 3] [--json]
  brain golden summarize RESULTS.json [--tasks DIR] [--min-drop 0.05] [--json]
Running the jobs is the KiroCrew app's part (golden.run(jobs, runner, tasks) with its own dispatch callback).
"""
from __future__ import annotations

import json
import os
import sys

from .. import config
from .. import golden as G
from ..common import BrainError, repo_root


def _dirs(repo) -> tuple:
    """(brain_dir, runs_dir) from the project's kit.json, else the defaults."""
    try:
        cfg = config.load(repo)
    except BrainError:
        cfg = config.DEFAULTS
    return cfg.get("brain_dir") or "brain", cfg.get("runs_dir") or "runs"


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_snapshot(a):
    task = G.load_task(a.task_dir)
    repo = os.path.abspath(a.repo) if a.repo else repo_root()
    brain_dir, runs_dir = _dirs(repo)
    man = G.snapshot(repo, task["base_sha"], a.out, variant=a.variant, brain_dir=brain_dir, runs_dir=runs_dir,
                     mask_secrets=a.mask_secrets)
    man["task"] = task["id"]
    if man.get("override"):
        print("WARN: --mask-secrets: secret-looking values in %s were masked, not refused (%d masked, %d files "
              "dropped); the manifest records it as override %s" % (
                  a.out, sum(1 for m in man["masked"] if m["why"] != "dev default"),
                  sum(1 for r in man["removed"] if "--mask-secrets" in r["why"]), man["override"]), file=sys.stderr)
    if a.json:
        _print(man)
        return 0
    print("快照 %s（%s，%s）：%d 个文件，Brain 文件 %s" % (a.out, task["id"], man["variant"], man["files"],
                                                ", ".join(man["brain_files"]) or "无"))
    for r in man["removed"]:
        print("  去掉 %s（%s）" % (r["path"], r["why"]))
    for m in man.get("masked", []):
        print("  打码 %s 第 %d 行 %s（%s）" % (m["path"], m["line"], m["name"], m["why"]))
    for r in man.get("scrubbed", []):
        print("  删掉 %s 里的 %d 个 brain-proposal 块、%d 行引用（%s 的条目在别处的副本）" % (
            r["path"], r["blocks"], r["lines"], G.ABLATES[man["variant"]]))
    for path in man.get("submodules", []):
        print("  子模块 %s：内容不在快照里（git archive 不带子模块），是个空目录" % path)
    if man.get("git_ceiling"):
        print("  注意：快照在 git 仓库 %s 下面，是 GIT_CEILING_DIRECTORIES=%s 让 git 看不到它；agent 的 run 也必须带上"
              "同样的设置" % (man["git_ceiling"]["repo"], man["git_ceiling"]["GIT_CEILING_DIRECTORIES"]))
    print("  tree sha256 %s；清单没写进快照（agent 看不到自己是哪一组）" % man["tree_sha256"][:16])
    return 0


def cmd_score(a):
    task = G.load_task(a.task_dir)
    res = G.score(os.path.abspath(a.workdir), task)
    if a.json:
        _print(res)
    else:
        print("%s：通过 %d，失败 %d（%s，退出码 %d），得分 %.2f" % (
            res["task"], res["passed"], res["failed"], res["parsed"], res["exit"], res["score"]))
    return 0 if res["ok"] else 1


def cmd_plan(a):
    tasks = G.load_tasks(a.tasks)
    variants = [v.strip() for v in a.variants.split(",") if v.strip()] if a.variants else list(G.VARIANTS)
    jobs = G.plan(tasks, variants, a.repeats)
    if a.json:
        _print(jobs)
        return 0
    print("%d 道题 × %d 组 × %d 次 = %d 个 run（自动答题都标 eval_oracle）" % (
        len(tasks), len(variants), a.repeats, len(jobs)))
    for t in tasks:
        print("  %s @ %s：%s" % (t["id"], t["base_sha"][:12], ", ".join(variants)))
    return 0


def cmd_summarize(a):
    try:
        with open(a.results, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise BrainError("cannot read %s: %s" % (a.results, e))
    except ValueError as e:
        raise BrainError("%s is not valid JSON: %s" % (a.results, e))
    results = data.get("results") if isinstance(data, dict) else data
    if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
        raise BrainError("%s must be a list of results (or {\"results\": [...]})" % a.results)
    tasks = G.load_tasks(a.tasks) if a.tasks else None
    s = G.summarize(results, tasks=tasks, min_drop=a.min_drop)
    if a.json:
        _print(s)
    else:
        print(G.report(s))
    return 0


def register(sub):
    s = sub.add_parser("golden", help="golden tasks and ablation: snapshot, score, plan, summarize")
    gs = s.add_subparsers(dest="golden_op", required=True)
    x = gs.add_parser("snapshot", help="git archive of the task's base_sha into a fresh dir (no .git, no tokens)")
    x.add_argument("task_dir", help="tasks/<id> (holds task.json)")
    x.add_argument("--out", required=True, help="new, empty directory outside any git repository")
    x.add_argument("--variant", choices=G.VARIANTS, default="full")
    x.add_argument("--repo", help="the project repo (default: the repo you are in)")
    x.add_argument("--mask-secrets", action="store_true",
                   help="override: mask secret-looking values (drop private-key files) instead of refusing; listed "
                        "in the manifest, which records the override")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_snapshot)
    x = gs.add_parser("score", help="apply the hidden tests in WORKDIR and run the task's test command")
    x.add_argument("task_dir")
    x.add_argument("workdir")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_score)
    x = gs.add_parser("plan", help="tasks x variants x repeats -> jobs")
    x.add_argument("--tasks", required=True, help="directory with <id>/task.json (or a repo with tasks/)")
    x.add_argument("--variants", help="comma separated (default: %s)" % ",".join(G.VARIANTS))
    x.add_argument("--repeats", type=int, default=3)
    x.add_argument("--json", action="store_true", help="print the jobs as JSON")
    x.set_defaults(fn=cmd_plan)
    x = gs.add_parser("summarize", help="per variant mean/stdev; real drops; withdraw candidates")
    x.add_argument("results", help="JSON list of results (as golden.run returns) or {\"results\": [...]}")
    x.add_argument("--tasks", help="task directory, for must_use (withdraw candidates)")
    x.add_argument("--min-drop", type=float, default=0.05)
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_summarize)
