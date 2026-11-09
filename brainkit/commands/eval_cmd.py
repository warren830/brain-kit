"""`brain eval`: Eval v1 over all runs (brainkit/evaluate.py).

  brain eval [--ref REF] [--since DATE] [--json] [--credits FILE] [--ledger PATH] [--vouchers DIR] [--stuck-hours N]
              runs: the tree's (merged), the unmerged ones on <prefix>/* branches here, and the ledger's
  brain eval gaps [--file] [same options]     knowledge / test / golden-case gaps worth filing; --file files them
                                              as intents (source eval-gap) when brainkit.intents is installed
"""
from __future__ import annotations

import inspect
import json

from .. import config
from .. import evaluate as EV
from ..common import BrainError, repo_root
from .trace_cmd import common_args, default_vouchers, load_credits

GAP_KIND = {"knowledge": "knowledge", "test": "test", "golden": "other"}


def _data(a, repo, cfg):
    return EV.collect(repo, cfg, ref=a.ref, since=a.since, ledger_path=a.ledger,
                      vouchers_dir=a.vouchers or default_vouchers(repo), credits=load_credits(a.credits),
                      as_of=a.as_of, allowed_signers=a.allowed_signers, min_runs=a.min_runs,
                      stuck_hours=a.stuck_hours)


def _intents_add():
    """brainkit.intents.add when that module is there (another feature); None otherwise."""
    try:
        from .. import intents
    except Exception:  # noqa: BLE001  not installed, or broken: fall back to printing
        return None
    add = getattr(intents, "add", None)
    return add if callable(add) else None


def _intents_filed(repo):
    """The eval-gap intents filed so far (brainkit.intents.scan), or None when that module is not there."""
    try:
        from .. import intents
        docs, _ = intents.scan(repo)
    except Exception:  # noqa: BLE001  not installed, or broken: intents.add still dedupes what it can
        return None
    return [d for d in docs if isinstance(d, dict) and d.get("source") == "eval-gap"]


def _title(t) -> str:
    return " ".join(str(t or "").split())


def file_gaps(repo, cfg, gs, add, out=print, existing=None) -> int:
    """File each gap with intents.add (source eval-gap). `existing`: the eval-gap intents filed before. A gap's title
    is its identity (kind + subject, no counts; evaluate._gap): one already filed under that title is reported as
    "already filed" and not filed again, although its body (the counts) changed since. intents.add itself dedupes on
    source + title + body only."""
    filed = {}
    for d in sorted(existing or [], key=lambda d: (str(d.get("created_at", "")), str(d.get("key", "")))):
        if not (isinstance(d.get("closed"), dict) and d["closed"].get("duplicate_of")):
            filed.setdefault(_title(d.get("title")), d)
    params = inspect.signature(add).parameters
    bad = 0
    for g in gs:
        prior = filed.get(_title(g["title"]))
        if prior:
            out("already filed %s: %s" % (prior.get("key", "?"), g["title"]))
            continue
        kw = {"acs": g["acs"], "refs": g["refs"], "key": None}   # key = the new intent's key: let it claim the next
        if "extra" in params:
            kw["extra"] = dict({"gap_kind": GAP_KIND[g["kind"]]}, **({"found_in": g["key"]} if g["key"] else {}))
        try:
            doc = add(repo, cfg, "eval-gap", g["title"], g["body"], **kw)
        except (BrainError, TypeError, ValueError) as e:
            bad += 1
            out("not filed %s: %s" % (g["id"], e))
            continue
        filed.setdefault(_title(g["title"]), doc or {})
        out("%s %s: %s" % ("already filed" if (doc or {}).get("duplicate") else "filed", (doc or {}).get("key", "?"),
                           g["title"]))
    return bad


def cmd_eval(a):
    repo = repo_root()
    cfg = config.load(repo)
    data = _data(a, repo, cfg)
    if getattr(a, "op", None) == "gaps":
        gs = EV.gaps(data)
        if a.json and not a.file:
            print(json.dumps(gs, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if a.file:
            add = _intents_add()
            if add is None:
                print("brainkit.intents is not available: printing the gaps instead of filing them.")
            else:
                return 1 if file_gaps(repo, cfg, gs, add, existing=_intents_filed(repo)) else 0
        if not gs:
            print("没有要记的缺口。")
        for g in gs:
            print("[%s] %s %s\n    %s\n    refs: %s" % (g["kind"], g["id"], g["title"], g["body"],
                                                    ", ".join(g["refs"])))
        return 0
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(EV.report(data))
    return 0


def _eval_args(s, suppress=False):
    import argparse
    d = {"default": argparse.SUPPRESS} if suppress else {}
    common_args(s, suppress)
    s.add_argument("--since", help="only this period: from this date (YYYY-MM-DD)", **d)
    s.add_argument("--min-runs", type=int, help="loaded by this many runs and never cited = withdraw candidate",
                   **(d or {"default": 3}))
    s.add_argument("--stuck-hours", type=float, help="an unmerged run with nothing new for this many hours is stuck "
                   "(review queue; default %g)" % EV.STUCK_HOURS, **(d or {"default": EV.STUCK_HOURS}))


def register(sub):
    s = sub.add_parser("eval", help="Eval v1: autonomy, delivery, interventions, knowledge, decision funnel")
    _eval_args(s)
    ops = s.add_subparsers(dest="op")
    g = ops.add_parser("gaps", help="knowledge / test / golden-case gaps worth filing")
    g.add_argument("--file", action="store_true", help="file them as intents (source eval-gap)")
    _eval_args(g, suppress=True)
    s.set_defaults(fn=cmd_eval)
