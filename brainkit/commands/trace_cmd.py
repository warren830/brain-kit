"""`brain trace KEY`: the run trace of one requirement (brainkit/trace.py).

  brain trace KEY [--ref REF] [--json] [--credits FILE] [--ledger PATH] [--vouchers DIR] [--allowed-signers F]
                  [--as-of TIME]
Read-only. --vouchers defaults to <intake store>/vouchers when it exists ($BRAIN_INTAKE_DIR, git config
brain.intake, ~/.brain/intake). --credits is the JSON the KiroCrew app passes in (credits are not readable here).
Without --ref, or with --ref naming the default branch (origin/main), a run whose run.json is not there (not merged
yet) is read from its run branch here (the same branch `brain eval` reads: evaluate.run_branches; a split child merged
into its parent's run branch: from that branch). A split's parent and children get a "family" section: the child's
own records only, the parent's with its children's numbers rolled up (brainkit/trace.py).
"""
from __future__ import annotations

import json
import os

from .. import config
from .. import trace as T
from ..common import BrainError, git, load_json, repo_root


def default_vouchers(repo):
    s = os.environ.get("BRAIN_INTAKE_DIR") or git(repo, "config", "brain.intake", check=False).stdout.strip() \
        or "~/.brain/intake"
    d = os.path.join(os.path.abspath(os.path.expanduser(s)), "vouchers")
    return d if os.path.isdir(d) else None


def load_credits(path):
    if not path:
        return None
    try:
        return load_json(path)
    except (OSError, ValueError) as e:
        raise BrainError("--credits %s: %s" % (path, e))


def common_args(s, suppress=False):
    """Options shared by `brain trace` and `brain eval` (suppress: on a sub-subcommand, keep the parent's value
    unless given again)."""
    import argparse
    d = {"default": argparse.SUPPRESS} if suppress else {}
    s.add_argument("--ref", help="read from this ref instead of the working tree (e.g. origin/main)", **d)
    s.add_argument("--json", action="store_true", **d)
    s.add_argument("--credits", help="JSON from the KiroCrew app: credits (per run / per turn), manual stop/restart",
                   **d)
    s.add_argument("--ledger", help="bridge ledger (sqlite), read-only", **d)
    s.add_argument("--vouchers", help="brain-intake vouchers dir (default <intake store>/vouchers)", **d)
    s.add_argument("--allowed-signers", help="check voucher signatures against this allowed_signers file", **d)
    s.add_argument("--as-of", help="judge the 14-day window at this time (default now)", **d)


def ref_for(repo, cfg, key, ref):
    """What to read: --ref (or the working tree) when run.json is there; when it is not and --ref is not given or
    names the default branch (`--ref origin/main` for a run not merged yet), the run branch `brain eval` reads
    (evaluate.run_branches), or, when there is none, the commit before the one that took run.json off that history
    (GitLab's Revert of the whole MR, its branch deleted: evaluate.removed_runs); else --ref as given (the trace says
    what is missing)."""
    if T.Tree(repo, ref).read("%s/%s/run.json" % (cfg["runs_dir"], key)) is not None:
        return ref
    if ref and T._short_ref(ref) != cfg["default_branch"]:
        return ref
    from .. import evaluate as EV
    return EV.run_branches(repo, cfg).get(key) or (EV.removed_runs(repo, cfg, ref).get(key) or {}).get("read_at") \
        or ref


def cmd_trace(a):
    repo = repo_root()
    cfg = config.load(repo)
    ref = ref_for(repo, cfg, a.key, a.ref)
    t = T.build(repo, cfg, a.key, ref=ref, ledger_path=a.ledger, vouchers_dir=a.vouchers or default_vouchers(repo),
                credits=load_credits(a.credits), as_of=a.as_of, allowed_signers=a.allowed_signers)
    print(json.dumps(t, ensure_ascii=False, indent=2, sort_keys=True) if a.json else T.render(t))
    return 0


def register(sub):
    s = sub.add_parser("trace", help="one requirement's run trace: stages, actions, decisions, checks, people, cost")
    s.add_argument("key", help="requirement key, e.g. PAY-1")
    common_args(s)
    s.set_defaults(fn=cmd_trace)
