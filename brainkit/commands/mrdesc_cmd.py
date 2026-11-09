"""`brain mr-desc [KEY]`: the MR's 变更说明, generated from the run's records (brainkit/mrdesc.py).

  brain mr-desc [KEY] [--ref REF] [--base REF] [--json] [--write] [--credits FILE] [--vouchers DIR]
                [--allowed-signers F]
KEY defaults to this run branch's key. Prints the Markdown (--json: the data). --write also writes it to
runs/<KEY>/MR.md: the same text every time for the same records; regenerate it, never edit it by hand. --write
refuses while aidlc-docs/lean.json counts runs/ as code (see mrdesc.write_problem). --vouchers defaults to
<intake store>/vouchers, --allowed-signers to $BRAIN_ALLOWED_SIGNERS: with both, a risk-path change's DESIGN
approval is checked the way CI checks it.
"""
from __future__ import annotations

import json
import os
import sys

from .. import config, mrdesc
from ..common import BrainError, git_out, parse_branch, repo_root
from .trace_cmd import default_vouchers, load_credits


def _key(repo, cfg, given):
    if given:
        return given
    b = parse_branch(git_out(repo, "rev-parse", "--abbrev-ref", "HEAD"), cfg["branch_prefix"])
    if not b:
        raise BrainError("not on a run branch: pass the KEY (and --ref origin/%s after the run merged)"
                         % cfg["default_branch"])
    return b["key"]


def cmd_mr_desc(a):
    repo = repo_root()
    cfg = config.load(repo)
    key = _key(repo, cfg, a.key)
    if a.write and a.ref:
        raise BrainError("--write writes this workspace's runs/%s/MR.md; it does not go with --ref" % key)
    why = mrdesc.write_problem(repo, cfg, key) if a.write else None
    if why:
        print("brain: 没写 MR.md：%s" % why, file=sys.stderr)
        return 1
    data = mrdesc.build(repo, cfg, key, base=a.base, ref=a.ref, credits=load_credits(a.credits),
                        vouchers_dir=a.vouchers or default_vouchers(repo),
                        allowed_signers=a.allowed_signers or os.environ.get("BRAIN_ALLOWED_SIGNERS") or None)
    text = mrdesc.render(data)
    if a.write:
        print("写好了 %s（由记录生成；改记录，再重新生成）" % mrdesc.write(repo, cfg, key, text), file=sys.stderr)
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) if a.json else text, end="\n" if a.json
          else "")
    return 0


def register(sub):
    s = sub.add_parser("mr-desc", help="the MR's 变更说明 from the run's records (what changed, evidence, how to review)")
    s.add_argument("key", nargs="?", help="requirement key, e.g. PAY-1 (default: this run branch)")
    s.add_argument("--ref", help="read the records from this ref instead of the working tree (e.g. origin/main)")
    s.add_argument("--base", help="diff against this ref (default: the merge base with the default branch)")
    s.add_argument("--json", action="store_true", help="print the data instead of the Markdown")
    s.add_argument("--write", action="store_true", help="also write runs/<KEY>/MR.md")
    s.add_argument("--credits", help="JSON from the KiroCrew app (as for brain trace)")
    s.add_argument("--vouchers", help="brain-intake vouchers dir (default <intake store>/vouchers)")
    s.add_argument("--allowed-signers", help="check DESIGN vouchers against this file (default "
                                             "$BRAIN_ALLOWED_SIGNERS)")
    s.set_defaults(fn=cmd_mr_desc)
