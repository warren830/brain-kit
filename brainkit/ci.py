"""`brain-ci`: checks that run in the MR pipeline (the CI result is a real gate only when it is a required
status the bot cannot write; see the main plan §6).

  brain-ci check-run      run.json valid and matches the branch; Lean intent present; Brain commit in history;
                          no material packages committed; risk paths changed only with an approved design
                          (risk paths read from the target branch); bot commits need a hand-over voucher.
  brain-ci push-rules     the pre-receive rules, run in the MR pipeline instead (GitLab Free / no server hooks):
                          the pipeline's user (GITLAB_USER_LOGIN) and the MR's new commits are judged as one push.
                          Weaker than the hook: it stops the merge, not the push.
  brain-ci brain-lint      the Brain's rules (brain lint) against the MR's diff base, config from the target branch.
                          Run it on every MR: Brain MRs (brain/<KEY>-reflect-<n>) and anything else that touches brain/.
  brain-ci verify-voucher FILE --allowed-signers F
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys

from . import config, lean, runrec, sign
from .common import BrainError, git, git_out, load_json, parse_branch, parse_iso, utcnow


def load_voucher(path):
    data = load_json(path)
    if not isinstance(data, dict) or "voucher" not in data or "sig" not in data:
        raise BrainError("%s is not a voucher file" % path)
    return data["voucher"], data["sig"]


def signed_vouchers(vouchers_dir, allowed_signers, key, repo):
    """(good, bad): signed vouchers for runs of `key` whose sha is in this branch's history."""
    good, bad = [], []
    if not vouchers_dir or not allowed_signers:
        return good, bad
    for path in sorted(glob.glob(os.path.join(vouchers_dir, "RUN-%s-*" % key, "*.json"))):
        try:
            v, s = load_voucher(path)
        except (BrainError, ValueError) as e:
            bad.append("%s: %s" % (path, e))
            continue
        if not sign.verify(v, s, allowed_signers):
            bad.append("%s: signature does not verify" % os.path.basename(path))
            continue
        if not str(v.get("run_id", "")).startswith("RUN-%s-" % key):
            continue
        if v.get("eval_oracle"):  # golden-task answers from the oracle never count for a real MR
            continue
        if git(repo, "merge-base", "--is-ancestor", str(v.get("sha", "")), "HEAD", check=False).returncode != 0:
            continue
        good.append(v)
    return good, bad


class Target:
    """What an MR is measured against: the target branch, never anything on the MR branch itself.

    In an MR pipeline GitLab gives CI_MERGE_REQUEST_TARGET_BRANCH_NAME and CI_MERGE_REQUEST_DIFF_BASE_SHA.
    Outside one (local runs, tests) it falls back to the repo's own config and the run's Brain commit, and says so."""

    def __init__(self, repo, env):
        self.repo = repo
        self.ref = None
        name = env.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME")
        if name and git(repo, "fetch", "-q", "origin", "+refs/heads/%s:refs/brain-ci/target" % name,
                        check=False).returncode == 0:
            self.ref = "refs/brain-ci/target"
        self.base = env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or ""
        if not self.base and self.ref:
            self.base = git_out(repo, "merge-base", "HEAD", self.ref)
        self.trusted = bool(self.ref)

    def file(self, path):
        if not self.ref:
            return None
        p = git(self.repo, "show", "%s:%s" % (self.ref, path), check=False)
        return p.stdout if p.returncode == 0 else None

    def cfg(self):
        if not self.ref:
            return config.load(self.repo)
        raw = self.file(config.KIT_CONFIG)
        if raw is None:
            raise BrainError("%s is missing on the target branch" % config.KIT_CONFIG)
        cfg = config.merged(config.DEFAULTS, json.loads(raw))
        probs = config.validate(cfg)
        if probs:
            raise BrainError("target %s: %s" % (config.KIT_CONFIG, "; ".join(probs)))
        return cfg


def glob_match(path, pattern) -> bool:
    """gitignore-like: '**' spans directories, '*' and '?' stay inside one; a trailing '/' means the whole dir."""
    pat = pattern.strip()
    if pat.endswith("/"):
        pat += "**"
    rx = ""
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            rx += "(?:.*/)?"
            i += 3
        elif pat.startswith("**", i):
            rx += ".*"
            i += 2
        elif pat[i] == "*":
            rx += "[^/]*"
            i += 1
        elif pat[i] == "?":
            rx += "[^/]"
            i += 1
        else:
            rx += re.escape(pat[i])
            i += 1
    return re.fullmatch(rx, path) is not None


def lean_events(repo, intent):
    out = []
    for f in sorted(glob.glob(os.path.join(repo, lean.DOCS, "audit", "*.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("intent") == intent:
                    out.append(e)
    return sorted(out, key=lambda e: str(e.get("ts", "")))


def _blob(repo, rev, path):
    """The bytes of `path` at `rev`, or None (raw: no text decoding, no end-of-line conversion)."""
    p = subprocess.run(["git", "-C", repo, "cat-file", "blob", "%s:%s" % (rev, path)], capture_output=True)
    return p.stdout if p.returncode == 0 else None


def design_approval_covers(repo, voucher, design_path, risky) -> str:
    """'' when this DESIGN approval still covers the MR, else why not (see check_route)."""
    h = str(voucher.get("lean_request", "")).split(" ", 1)[-1].strip().lower()
    at = str(voucher.get("sha", ""))
    if not re.match(r"^[0-9a-f]{6,64}$", h) or not re.match(r"^[0-9a-f]{40}$", at):
        return "%s: not a DESIGN <hash> approval at a commit" % voucher.get("voucher_id", "?")
    now, then = _blob(repo, "HEAD", design_path), _blob(repo, at, design_path)
    if now is not None and now == then and hashlib.sha256(now).hexdigest().startswith(h):
        return ""                                                  # a. the design is the file that was approved
    after = git_out(repo, "-c", "core.quotepath=false", "diff", "--no-renames", "--name-only", at, "HEAD").splitlines()
    moved = sorted(set(after) & set(risky))
    if not moved:
        return ""                                                  # b. the risky code was all there when approved
    return ("%s approved DESIGN %s at %s; since then application-design.md is not that file and risk paths changed "
            "(%s)" % (voucher.get("voucher_id", "?"), h[:8], at[:12], ", ".join(moved[:5])))


def check_route(repo, rec, cfg, tgt, base, vouchers) -> list:
    """The skip floor: changes to risk paths need the design stage and a design approval signed by the bridge.

    Risk paths come from the target branch. A design approval is a bridge answer voucher (approve, lean_request
    DESIGN ...) for this requirement: a ledger line on the branch is not evidence, anyone can write one.

    An approval covers the design as it was approved, not what came after. The voucher names it twice: `sha`, the
    MR head the question was asked at (the file the approver could read), and the hash in lean_request, Lean's hash
    of application-design.md (`lean request DESIGN` prints the first 8 hex digits of the file's sha256; an approval
    line matches when the file's sha256 starts with them). One approval is enough when either holds:
      a. application-design.md at HEAD is the approved file: its sha256 starts with the voucher's hash, and its bytes
         equal the file at the voucher's commit (8 hex digits alone can be matched by a crafted file in minutes);
      b. no risk path that this MR changes was changed after the voucher's commit: the risky code was all there when
         the approver looked, whatever the design says now.
    Otherwise the design and the risky code both moved on after the approval: the run asks for a fresh one."""
    text = tgt.file(cfg["risk_paths_file"])
    if text is None and not tgt.trusted:
        p = git(repo, "show", "%s:%s" % (rec["brain_commit"], cfg["risk_paths_file"]), check=False)
        text = p.stdout if p.returncode == 0 else None
    if not text:
        return []
    pats = [l.split("#", 1)[0].strip() for l in text.splitlines()]
    pats = [x for x in pats if x]
    changed = [x for x in git_out(repo, "-c", "core.quotepath=false", "diff", "--name-only", base, "HEAD").splitlines()]
    risky = sorted({f for f in changed if any(glob_match(f, x) for x in pats)})
    if not risky:
        return []
    shown = ", ".join(risky[:5])
    ev = lean_events(repo, rec["intent"])
    skipped = any(e.get("event") == "STAGE_SKIPPED" and (e.get("data") or {}).get("stage") == "application-design"
                  for e in ev)
    design_path = "%s/%s/inception/application-design/application-design.md" % (lean.DOCS, rec["intent"])
    designed = os.path.exists(os.path.join(repo, design_path))
    if skipped or not designed:
        return ["risk paths changed (%s) but application design was %s: this change needs the design stage"
                % (shown, "skipped" if skipped else "not written")]
    approvals = [v for v in vouchers if v.get("kind") == "answer" and v.get("choice") == "approve"
                 and str(v.get("lean_request", "")).startswith("DESIGN ")]
    if not approvals:
        return ["risk paths changed (%s): no design approval signed by the bridge for %s (ask through the MR: "
                "park.json with lean_request DESIGN ...)" % (shown, rec["key"])]
    why = [design_approval_covers(repo, v, design_path, risky) for v in approvals]
    if all(why):
        return ["risk paths changed (%s): no design approval covers them any more (%s): ask for a fresh DESIGN "
                "approval through the MR" % (shown, "; ".join(why[:3]))]
    return []


def check_run(repo, branch, env) -> list:
    tgt = Target(repo, env)
    cfg = tgt.cfg()
    b = parse_branch(branch, cfg["branch_prefix"])
    if not b:
        return []
    probs = []
    rp = os.path.join(repo, runrec.path(cfg["runs_dir"], b["key"]))
    if not os.path.exists(rp):
        return ["%s is missing" % os.path.relpath(rp, repo)]
    rec = load_json(rp)
    probs += ["run.json: " + p for p in runrec.validate(rec, branch=branch, prefix=cfg["branch_prefix"])]
    if probs:
        return probs
    if not os.path.isdir(os.path.join(repo, lean.DOCS, rec["intent"])):
        probs.append("Lean intent %s/%s is missing" % (lean.DOCS, rec["intent"]))
    if git(repo, "merge-base", "--is-ancestor", rec["brain_commit"], "HEAD", check=False).returncode != 0:
        probs.append("brain_commit %s is not in this branch's history (use GIT_DEPTH: 0)" % rec["brain_commit"][:12])
        return probs
    if tgt.ref and git(repo, "merge-base", "--is-ancestor", rec["brain_commit"], tgt.ref,
                       check=False).returncode != 0:
        probs.append("brain_commit %s is not on the target branch" % rec["brain_commit"][:12])
        return probs
    base = tgt.base or rec["brain_commit"]
    if git_out(repo, "ls-files", "--", ".brain-materials"):
        probs.append("material packages are committed (.brain-materials/ must stay out of git)")
    links = [l.split("\t", 1)[1] for l in git_out(repo, "ls-files", "-s", "--", cfg["brain_dir"], "AGENTS.md").split("\n")
             if l.startswith("120000 ")]
    if links:
        probs.append("symlinks in the Brain (%s): the Brain must be real files" % ", ".join(links[:5]))
    touched = git_out(repo, "-c", "core.quotePath=false", "diff", "--no-renames", "--name-only", "%s..HEAD" % base,
                      "--", cfg["brain_dir"], "AGENTS.md").split("\n")
    touched = [t for t in touched if t]
    if touched:
        probs.append("a run branch changed the Brain (%s): write brain-proposal blocks in decisions.md instead; "
                     "`brain reflect` turns them into a Brain MR for the file owners" % ", ".join(touched[:5]))
    vdir, signers = env.get("BRAIN_VOUCHERS_DIR", ""), env.get("BRAIN_ALLOWED_SIGNERS", "")
    vouchers, bad = signed_vouchers(vdir, signers, b["key"], repo)
    probs += bad
    probs += check_route(repo, rec, cfg, tgt, base, vouchers)
    bot_emails = {e.lower() for e in cfg.get("bot_emails", [])}
    people = set(git_out(repo, "log", "--format=%ce%n%ae", "%s..HEAD" % base).lower().split())
    if bot_emails & people:
        if not vdir or not signers:
            probs.append("the bot committed here, but BRAIN_VOUCHERS_DIR / BRAIN_ALLOWED_SIGNERS are not set")
        elif not [v for v in vouchers if v.get("kind") == "handoff" and v.get("branch") == branch]:
            probs.append("the bot committed here without a valid hand-over voucher for %s" % branch)
    if not tgt.trusted:
        probs = [p + " [local check: config read from this branch]" for p in probs] if probs else []
    return probs


def cmd_check_run(a):
    repo = os.environ.get("CI_PROJECT_DIR") or os.getcwd()
    branch = (os.environ.get("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME") or os.environ.get("CI_COMMIT_REF_NAME")
              or git_out(repo, "rev-parse", "--abbrev-ref", "HEAD"))
    if os.environ.get("GITLAB_CI") and not os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME"):
        print("FAIL brain-ci check-run must run in a merge request pipeline (it compares against the target branch)")
        return 1
    tgt = Target(repo, os.environ)
    if not parse_branch(branch, tgt.cfg()["branch_prefix"]):
        print("brain-ci: %s is not a run branch; nothing to check" % branch)
        return 0
    probs = check_run(repo, branch, os.environ)
    for p in probs:
        print("FAIL %s" % p)
    print("brain-ci: %s %s" % (branch, "FAILED" if probs else "ok"))
    return 1 if probs else 0


def push_rules(repo, branch, user, env) -> list:
    tgt = Target(repo, env)
    cfg = tgt.cfg()
    if not tgt.ref or not tgt.base:
        return ["push-rules needs an MR pipeline (target branch and diff base)"]
    from . import pre_receive
    hook_cfg = dict(pre_receive.DEFAULT_CFG)
    for k in ("branch_prefix", "default_branch", "bot_users", "bot_emails", "bot_forbidden_paths"):
        hook_cfg[k] = cfg[k]
    # the hook compares with refs/heads/<default>; point it at the target branch inside this CI checkout
    git(repo, "update-ref", "refs/heads/" + hook_cfg["default_branch"], tgt.ref)
    head = git_out(repo, "rev-parse", "HEAD")
    return pre_receive.decide([(tgt.base, head, "refs/heads/" + branch)], user, hook_cfg, cwd=repo)


def cmd_push_rules(a):
    repo = os.environ.get("CI_PROJECT_DIR") or os.getcwd()
    branch = os.environ.get("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME", "")
    user = os.environ.get("GITLAB_USER_LOGIN", "")
    if not branch or not user:
        print("FAIL brain-ci push-rules must run in a merge request pipeline")
        return 1
    probs = push_rules(repo, branch, user, os.environ)
    for p in probs:
        print("FAIL %s" % p)
    print("brain-ci push-rules: %s by %s %s" % (branch, user, "FAILED" if probs else "ok"))
    return 1 if probs else 0


def brain_lint(repo, env) -> tuple:
    """Fails closed: without the target branch (config, current Brain) and the diff base there is nothing to trust."""
    from . import brainlint, entries
    tgt = Target(repo, env)
    if not tgt.trusted or not tgt.base:
        return ["brain-lint needs an MR pipeline whose target branch can be fetched (config and diff base come from "
                "it)"], []
    cfg = tgt.cfg()
    base = entries.Brain.from_git(repo, tgt.base, cfg["brain_dir"])
    errors, warnings = brainlint.check(os.path.join(repo, cfg["brain_dir"]), base=base,
                                       target=entries.Brain.from_git(repo, tgt.ref, cfg["brain_dir"]),
                                       purge_approvers=cfg.get("purge_approvers") or [])
    head_agents = ""
    if os.path.exists(os.path.join(repo, "AGENTS.md")):
        with open(os.path.join(repo, "AGENTS.md"), encoding="utf-8", errors="replace") as f:
            head_agents = f.read()
    e2, w2 = brainlint.check_agents(head_agents, git(repo, "show", "%s:AGENTS.md" % tgt.base, check=False).stdout)
    return errors + e2, warnings + w2


def cmd_brain_lint(a):
    repo = os.environ.get("CI_PROJECT_DIR") or os.getcwd()
    errors, warnings = brain_lint(repo, os.environ)
    for w in warnings:
        print("WARN %s" % w)
    for e in errors:
        print("FAIL %s" % e)
    print("brain-ci brain-lint: %s" % ("FAILED" if errors else "ok"))
    return 1 if errors else 0


def cmd_verify_voucher(a):
    v, s = load_voucher(a.file)
    ok = sign.verify(v, s, a.allowed_signers)
    exp = v.get("expires_at")
    expired = bool(exp) and parse_iso(exp) < utcnow()
    print(json.dumps({"valid_signature": ok, "expired": expired, "voucher_id": v.get("voucher_id")}))
    return 0 if ok and not expired else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="brain-ci")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-run").set_defaults(fn=cmd_check_run)
    sub.add_parser("push-rules").set_defaults(fn=cmd_push_rules)
    sub.add_parser("brain-lint").set_defaults(fn=cmd_brain_lint)
    s = sub.add_parser("verify-voucher")
    s.add_argument("file")
    s.add_argument("--allowed-signers", required=True)
    s.set_defaults(fn=cmd_verify_voucher)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except BrainError as e:
        print("brain-ci: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
