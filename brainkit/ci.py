"""`brain-ci`: checks that run in the MR pipeline (the CI result is a real gate only when it is a required
status the bot cannot write; see the main plan §6).

  brain-ci check-run      run.json valid and matches the branch; Lean intent present; Brain commit in history;
                          no material packages committed; risk paths changed only with an approved design
                          (risk paths read from the target branch); bot commits need a hand-over voucher.
  brain-ci push-rules     the pre-receive rules, run in the MR pipeline instead (GitLab Free / no server hooks):
                          a person's pipeline (GITLAB_USER_LOGIN) judges the MR's new commits as their one push, and
                          the bot's commits in it by the bot's rules (bot_commit_rules); a bot's pipeline judges each
                          commit by who made it, from the hand-over voucher on (bot_push_rules; needs
                          BRAIN_VOUCHERS_DIR / BRAIN_ALLOWED_SIGNERS). Weaker than the hook: it stops the merge, not
                          the push.
  Vouchers name commits; after GitLab's Rebase a voucher counts for the rebased commit (rebased_commit).
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
import unicodedata

from . import config, lean, riskpaths, runrec, sign
from .common import BrainError, git, git_out, load_json, parse_branch, parse_iso, utcnow


def load_voucher(path):
    data = load_json(path)
    if not isinstance(data, dict) or "voucher" not in data or "sig" not in data:
        raise BrainError("%s is not a voucher file" % path)
    return data["voucher"], data["sig"]


SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def _has_commit(repo, sha) -> bool:
    return git(repo, "cat-file", "-e", sha + "^{commit}", check=False).returncode == 0


def _fetch_commit(repo, sha):
    """Best effort: fetch a commit the branch no longer holds (after a rebase). GitLab keeps every MR head it has
    seen (keep-around refs) and serves it by id; nothing happens when there is no origin or the server refuses."""
    if git(repo, "remote", "get-url", "origin", check=False).returncode != 0:
        return
    try:
        subprocess.run(["git", "-C", repo, "fetch", "-q", "--no-tags", "origin", sha], capture_output=True,
                       timeout=120, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except (OSError, subprocess.SubprocessError):
        pass


def _git_bytes(repo, *args, stdin=None) -> bytes:
    p = subprocess.run(["git", "-C", repo] + list(args), input=stdin, capture_output=True)
    if p.returncode != 0:
        raise BrainError("git %s failed: %s" % (" ".join(args[:4]), p.stderr.decode("utf-8", "replace").strip()))
    return p.stdout


COMMIT_LINE_RE = re.compile(rb"^commit ([0-9a-f]{40,64})$", re.M)
FILE_LINES = (b"diff --git ", b"old mode ", b"new mode ", b"new file mode ", b"deleted file mode ")


def commit_changes(repo, *revs) -> list:
    """[(change id, sha)] of the commits `git log <revs>` lists, oldest first. The change id is what a rebase keeps of
    a commit: its author (email and time), its patch as `git patch-id --verbatim` sees it (stable: per file, the
    lines it removes and adds and the 3 lines of context around them, verbatim, whitespace included, without the hunk
    positions, which move when the base moves) and its files' name and mode lines. Not the committer (the person who
    rebased) or the message. So the same line added elsewhere in a file is another change. A merge that only merges
    (nothing in it differs from all of its parents: its changes are its parents') is left out; a merge with changes
    of its own (an edit, a conflict resolved in it) gets the change id None, which matches nothing."""
    from .pre_receive import own_changes
    revs = list(revs)
    rows = _git_bytes(repo, "log", "--topo-order", "--reverse", "--format=%H%x00%P%x00%ae%x00%at", *revs)
    patch = _git_bytes(repo, "log", "--topo-order", "--reverse", "--no-merges", "-p", "--binary", "-U3", "--no-color",
                       "--no-ext-diff", "--no-textconv", "--no-renames", "--diff-algorithm=myers", "--format=commit %H",
                       *revs)
    try:
        ids = _git_bytes(repo, "patch-id", "--verbatim", stdin=patch).decode("ascii", "replace")
    except BrainError as e:
        raise BrainError("%s: comparing commits across a rebase needs `git patch-id --verbatim` (git 2.39 or later)"
                         % e)
    pid = {}
    for line in ids.splitlines():
        p_id, _, sha = line.partition(" ")
        pid[sha.strip()] = p_id
    files = {}
    parts = COMMIT_LINE_RE.split(patch)                 # ['', sha, its patch, sha, its patch, ...]
    for sha, body in zip(parts[1::2], parts[2::2]):
        files[sha.decode("ascii")] = b"\n".join(x for x in body.split(b"\n") if x.startswith(FILE_LINES))
    out = []
    for row in rows.decode("utf-8", "replace").splitlines():
        sha, parents, email, at = (row.split("\0") + ["", "", "", ""])[:4]
        if len(parents.split()) > 1:
            try:
                own = own_changes([sha], cwd=repo)
            except RuntimeError as e:
                raise BrainError(str(e))
            if own:
                out.append((None, sha))
            continue
        h = hashlib.sha256(("%s %s\n%s\n" % (email.lower(), at, pid.get(sha, ""))).encode("utf-8"))
        h.update(files.get(sha, b""))
        out.append((h.hexdigest(), sha))
    return out


def rebased_commit(repo, sha, upstream, rec_path, _head=None):
    """The commit of HEAD's history that is commit `sha` rebased, or None. Vouchers name a commit (the hand-over
    commit, the MR head a question was asked at); GitLab's Rebase gives every commit of the branch a new sha. A voucher
    still counts for the rebased commit when the rebase replayed the same work up to it:
      - the commits from `upstream` (the MR's diff base) to `sha`, and the first as many commits from `upstream` to
        HEAD, have the same change ids in the same order (commit_changes: same author and time, same patch with its
        context, as `git patch-id --verbatim` has it), and the last of them stands for `sha`; merges that only merge
        are left out, and one with changes of its own in either part matches nothing;
      - `rec_path` (runs/<KEY>/run.json) is the same blob in both: the record that was handed over.
    So a commit cannot be moved before or after the vouched one, edited, added or dropped there, a line cannot move to
    another place, a merge cannot bring a change of its own, and a rebase that resolved a conflict in that part of the
    branch (or onto a target branch that changed lines next to the branch's) loses the voucher (merge the target
    branch in instead: merging keeps every sha). `sha` is fetched from origin when this clone does not have it."""
    if not SHA40_RE.match(str(sha or "")) or not upstream:
        return None
    if not _has_commit(repo, sha):
        _fetch_commit(repo, sha)
        if not _has_commit(repo, sha):
            return None
    then = commit_changes(repo, sha, "--not", upstream)
    if not then or then[-1][1] != sha or any(c is None for c, _ in then):
        return None                     # a merge, a commit already on the target branch, a merge with its own change
    now = _head if _head is not None else commit_changes(repo, "HEAD", "--not", upstream)
    if len(now) < len(then) or [c for c, _ in now[:len(then)]] != [c for c, _ in then]:
        return None
    eq = now[len(then) - 1][1]
    a = git(repo, "rev-parse", "-q", "--verify", "%s:%s" % (sha, rec_path), check=False).stdout.strip()
    b = git(repo, "rev-parse", "-q", "--verify", "%s:%s" % (eq, rec_path), check=False).stdout.strip()
    return eq if a and a == b else None


def signed_vouchers(vouchers_dir, allowed_signers, key, repo, upstream=None, runs_dir="runs"):
    """(good, bad): signed vouchers for runs of `key` whose sha is in this branch's history. With `upstream` (the MR's
    diff base) a voucher whose commit was rebased counts for the rebased commit (rebased_commit): it comes back as a
    copy whose `sha` is that commit and `rebased_from` the signed one."""
    good, bad = [], []
    if not vouchers_dir or not allowed_signers:
        return good, bad
    head_changes = []
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
            if not upstream:
                continue
            if not head_changes:
                head_changes.append(commit_changes(repo, "HEAD", "--not", upstream))
            eq = rebased_commit(repo, str(v.get("sha", "")), upstream, runrec.path(runs_dir, key).replace(os.sep, "/"),
                                _head=head_changes[0])
            if not eq:
                continue
            v = dict(v, sha=eq, rebased_from=v.get("sha"))
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


def glob_match(path, pattern, folder=False) -> bool:
    """Is the file `path` under the gates/risk-paths `pattern`? The .gitignore rules (riskpaths): src/pay, /src/pay/,
    *.sql, src/**/x. folder=True: `path` is a submodule, a folder to git. BrainError for a pattern those rules do not
    support."""
    return riskpaths.matches(path, pattern, folder)


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


def changed_paths(repo, *revs, paths=()) -> list:
    """Every path `git diff <revs>` changes, each as it is: no rename detection (a file moved out of a risk path is
    a change of both its names) and NUL-separated (no C-quoting of odd names, nothing stripped)."""
    cmd = ["git", "-C", repo, "diff", "--no-renames", "--no-ext-diff", "-z", "--name-only"] + list(revs)
    if paths:
        cmd += ["--"] + list(paths)
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise BrainError("git diff %s failed: %s" % (" ".join(revs), p.stderr.decode("utf-8", "replace").strip()))
    return [x for x in p.stdout.decode("utf-8", "surrogateescape").split("\0") if x]


def gitlinks(repo, *revs) -> set:
    """The paths `git diff <revs>` changes that are a submodule (a gitlink, mode 160000) on either side."""
    p = subprocess.run(["git", "-C", repo, "diff", "--no-renames", "--no-ext-diff", "-z", "--raw"] + list(revs),
                       capture_output=True)
    if p.returncode != 0:
        raise BrainError("git diff %s failed: %s" % (" ".join(revs), p.stderr.decode("utf-8", "replace").strip()))
    out = p.stdout.decode("utf-8", "surrogateescape").split("\0")
    return {path for meta, path in zip(out[::2], out[1::2]) if "160000" in meta[1:].split()[:2]}


def _fold(path) -> str:
    """A path as a case-insensitive disk (macOS, Windows) compares it: there Agents.md is AGENTS.md."""
    return unicodedata.normalize("NFC", path).casefold()


def in_brain(path, brain_dir) -> bool:
    """Is `path` the Brain's (brain_dir and below, AGENTS.md) in any letter case? A clone on a case-insensitive disk
    checks Agents.md or Brain/TECH.md out over the real file, so those count as changing it too."""
    p, d = _fold(path), _fold(str(brain_dir).strip("/"))
    return p == _fold("AGENTS.md") or p == d or p.startswith(d + "/")


def _index_entries(repo) -> list:
    """[(mode, path)] of the checkout's index, NUL-separated (odd names are not quoted)."""
    p = subprocess.run(["git", "-C", repo, "ls-files", "-s", "-z"], capture_output=True)
    if p.returncode != 0:
        raise BrainError("git ls-files failed: %s" % p.stderr.decode("utf-8", "replace").strip())
    out = []
    for rec in p.stdout.decode("utf-8", "surrogateescape").split("\0"):
        meta, tab, path = rec.partition("\t")
        if tab:
            out.append((meta.split(" ", 1)[0], path))
    return out


def lean_hashes(data) -> set:
    """sha256 hex of a file as Lean may have hashed it. Lean hashes the checked-out file; git stores the blob, which
    may differ only in line ends (`eol=crlf` attributes, core.autocrlf): the blob, its LF form and its CRLF form."""
    lf = data.replace(b"\r\n", b"\n")
    return {hashlib.sha256(x).hexdigest() for x in (data, lf, lf.replace(b"\n", b"\r\n"))}


def design_approval_covers(repo, voucher, design_path, risky) -> str:
    """'' when this DESIGN approval still covers the MR, else why not (see check_route)."""
    h = str(voucher.get("lean_request", "")).split(" ", 1)[-1].strip().lower()
    at = str(voucher.get("sha", ""))
    if not re.match(r"^[0-9a-f]{6,64}$", h) or not re.match(r"^[0-9a-f]{40}$", at):
        return "%s: not a DESIGN <hash> approval at a commit" % voucher.get("voucher_id", "?")
    now, then = _blob(repo, "HEAD", design_path), _blob(repo, at, design_path)
    if now is not None and now == then and any(x.startswith(h) for x in lean_hashes(now)):
        return ""                                                  # a. the design is the file that was approved
    after = changed_paths(repo, at, "HEAD")
    moved = sorted(set(after) & set(risky))
    if not moved:
        return ""                                                  # b. the risky code was all there when approved
    return ("%s approved DESIGN %s at %s; since then application-design.md is not that file and risk paths changed "
            "(%s)" % (voucher.get("voucher_id", "?"), h[:8], at[:12], ", ".join(moved[:5])))


def _design_hash(v) -> str:
    """The design a DESIGN answer is about: the first 8 hex digits Lean names (`lean request DESIGN` prints 8)."""
    lr = str(v.get("lean_request", ""))
    return lr[len("DESIGN "):].strip().lower()[:8] if lr.startswith("DESIGN ") else ""


def _decision_number(v) -> int:
    m = re.match(r"^D-([0-9]+)$", str(v.get("decision", "")))
    return int(m.group(1)) if m else 0


def latest_design_answers(repo, vouchers) -> list:
    """The newest signed DESIGN answer for each design hash. Newest: asked at a later commit in this branch's history
    (more commits behind it: a descendant always has more), then issued later, then the higher decision number."""
    depth, best = {}, {}
    for v in vouchers:
        h = _design_hash(v)
        if v.get("kind") != "answer" or not re.match(r"^[0-9a-f]{6,8}$", h):
            continue
        at = str(v.get("sha", ""))
        if at not in depth:
            p = git(repo, "rev-list", "--count", at, check=False) if re.match(r"^[0-9a-f]{40}$", at) else None
            depth[at] = int(p.stdout.strip()) if p is not None and p.returncode == 0 else -1
        key = (depth[at], str(v.get("issued_at", "")), _decision_number(v))
        if h not in best or key > best[h][0]:
            best[h] = (key, v)
    return [v for _, v in best.values()]


def check_route(repo, rec, cfg, tgt, base, vouchers) -> list:
    """The skip floor: changes to risk paths need the design stage and a design approval signed by the bridge.

    Risk paths come from the target branch. A design approval is a bridge answer voucher (approve, lean_request
    DESIGN ...) for this requirement: a ledger line on the branch is not evidence, anyone can write one.

    An approval covers the design as it was approved, not what came after. The voucher names it twice: `sha`, the
    MR head the question was asked at (the file the approver could read), and the hash in lean_request, Lean's hash
    of application-design.md (`lean request DESIGN` prints the first 8 hex digits of the file's sha256; an approval
    line matches when the file's sha256 starts with them; Lean hashes the checked-out file, so a CRLF checkout is
    hashed as such: see lean_hashes). Per design hash the newest signed answer wins: a later rejection of the same
    design withdraws an earlier approval (and a later approval restores it). One approval is enough when either holds:
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
    bad = ["%s: %s: brain-ci fails every run MR until it is fixed" % (cfg["risk_paths_file"], why)
           for why in (riskpaths.problem(x) for x in pats) if why]
    pats = [x for x in pats if not riskpaths.problem(x)]
    changed, links = changed_paths(repo, base, "HEAD"), gitlinks(repo, base, "HEAD")
    risky = sorted({f for f in changed if any(glob_match(f, x, f in links) for x in pats)})
    if not risky:
        return bad
    shown = ", ".join(risky[:5])
    ev = lean_events(repo, rec["intent"])
    skipped = any(e.get("event") == "STAGE_SKIPPED" and (e.get("data") or {}).get("stage") == "application-design"
                  for e in ev)
    design_path = "%s/%s/inception/application-design/application-design.md" % (lean.DOCS, rec["intent"])
    designed = os.path.exists(os.path.join(repo, design_path))
    if skipped or not designed:
        return bad + ["risk paths changed (%s) but application design was %s: this change needs the design stage"
                      % (shown, "skipped" if skipped else "not written")]
    approvals = [v for v in latest_design_answers(repo, vouchers) if v.get("choice") == "approve"]
    if not approvals and rec.get("parent"):
        return ["risk paths changed (%s): no design approval signed by the bridge for %s or its parent %s. A split "
                "child cannot get one: it is not handed over to the team runner, so the bridge never asks for its "
                "DESIGN on an MR, and in the split flow the parent's DESIGN is approved in local Lean, which CI does "
                "not count (the parent is handed over only after every child has merged). Either do not split this "
                "requirement, or move the risk-path change out of the units into the parent run, which asks for "
                "the DESIGN approval through the bridge once it is handed over (docs 4.7)"
                % (shown, rec["key"], rec["parent"])]
    if not approvals:
        return bad + ["risk paths changed (%s): no design approval signed by the bridge for %s (ask through the MR: "
                      "park.json with lean_request DESIGN ...)" % (shown, rec["key"])]
    why = [design_approval_covers(repo, v, design_path, risky) for v in approvals]
    if all(why):
        return bad + ["risk paths changed (%s): no design approval covers them any more (%s): ask for a fresh DESIGN "
                      "approval through the MR" % (shown, "; ".join(why[:3]))]
    return bad


def _claimed_child(repo, rec) -> bool:
    """Does claim/<KEY> (origin's, fetched) say this run is the child of rec["parent"] for rec["unit"]?"""
    from . import claims
    doc = claims.read(repo, rec.get("key"))
    return isinstance(doc, dict) and doc.get("parent") == rec.get("parent") and doc.get("unit") == rec.get("unit")


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
    links = [p for mode, p in _index_entries(repo) if mode == "120000" and in_brain(p, cfg["brain_dir"])]
    if links:
        probs.append("symlinks in the Brain (%s): the Brain must be real files" % ", ".join(links[:5]))
    touched = [p for p in changed_paths(repo, "%s..HEAD" % base) if in_brain(p, cfg["brain_dir"])]
    if touched:
        probs.append("a run branch changed the Brain (%s): write brain-proposal blocks in decisions.md instead; "
                     "`brain reflect` turns them into a Brain MR for the file owners" % ", ".join(touched[:5]))
    vdir, signers = env.get("BRAIN_VOUCHERS_DIR", ""), env.get("BRAIN_ALLOWED_SIGNERS", "")
    vouchers, bad = signed_vouchers(vdir, signers, b["key"], repo, upstream=base, runs_dir=cfg["runs_dir"])
    probs += bad
    design = list(vouchers)
    if rec.get("parent") and _claimed_child(repo, rec):
        # a split child builds one unit of its parent's Lean intent: the design it follows is the parent's, so the
        # parent's signed DESIGN answers count for its risk paths (hand-over vouchers stay per branch, below). Only
        # when claim/<KEY> says it is that parent's child: run.json alone is whatever the branch says.
        pv, pbad = signed_vouchers(vdir, signers, rec["parent"], repo, upstream=base, runs_dir=cfg["runs_dir"])
        design += [v for v in pv if v.get("kind") == "answer"]
        probs += pbad
    probs += check_route(repo, rec, cfg, tgt, base, design)
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
    for k in ("branch_prefix", "default_branch", "bot_users", "bot_emails", "bot_forbidden_paths", "runs_dir"):
        hook_cfg[k] = cfg.get(k, hook_cfg.get(k))
    # the hook compares with refs/heads/<default>; point it at the target branch inside this CI checkout
    git(repo, "update-ref", "refs/heads/" + hook_cfg["default_branch"], tgt.ref)
    head = git_out(repo, "rev-parse", "HEAD")
    if (user or "").lower() not in {u.lower() for u in hook_cfg["bot_users"]}:
        probs = pre_receive.decide([(tgt.base, head, "refs/heads/" + branch)], user, hook_cfg, cwd=repo)
        return probs + bot_commit_rules(repo, branch, env, tgt, cfg, hook_cfg, head)
    return bot_push_rules(repo, branch, user, env, tgt, cfg, hook_cfg, head)


def handover_commit(repo, branch, key, env, upstream, runs_dir="runs"):
    """(commit, signed, why not): the commit of this branch's newest signed hand-over voucher (BRAIN_VOUCHERS_DIR,
    verified with BRAIN_ALLOWED_SIGNERS), on HEAD's history or rebased (rebased_commit), and the commit the bridge
    signed (the same one, or the one it was rebased from); (None, None, reason) without one."""
    vdir, signers = env.get("BRAIN_VOUCHERS_DIR", ""), env.get("BRAIN_ALLOWED_SIGNERS", "")
    if not vdir or not signers:
        return None, None, "BRAIN_VOUCHERS_DIR / BRAIN_ALLOWED_SIGNERS are not set in this job"
    vouchers, bad = signed_vouchers(vdir, signers, key, repo, upstream=upstream, runs_dir=runs_dir)
    signed = {}
    for v in vouchers:
        if v.get("kind") == "handoff" and v.get("branch") == branch:
            signed.setdefault(v["sha"], v.get("rebased_from") or v["sha"])
    if not signed:
        return None, None, "no signed hand-over voucher for %s whose commit is in this branch's history%s" % (
            branch, " (%s)" % "; ".join(bad[:3]) if bad else "")
    depth = {s: int(git_out(repo, "rev-list", "--count", s)) for s in signed}
    hand = max(depth, key=lambda s: (depth[s], s))
    return hand, signed[hand], ""


def _who(repo, commits) -> dict:
    """{commit: (author email, committer email)}, lowercased."""
    who = {}
    for c in commits:
        ae, _, ce = git_out(repo, "log", "-1", "--format=%ae%x00%ce", c).partition("\0")
        who[c] = (ae.strip().lower(), ce.strip().lower())
    return who


def run_owner(repo, signed, bots) -> set:
    """The run's owner as the signed hand-over names them: the author and committer of the commit the bridge signed,
    not of its rebased copy (whoever presses GitLab's Rebase commits that one)."""
    ae, _, ce = git_out(repo, "log", "-1", "--format=%ae%x00%ce", signed).partition("\0")
    return {ae.strip().lower(), ce.strip().lower()} - bots - {""}


def _bots_commits(commits, who, bots) -> list:
    """The commits the bot made: authored or committed as one of bot_emails. A bot commit with a person's author line
    is still the bot's (the hook refuses it; CI judges it by the bot's rules too)."""
    return [c for c in commits if who[c][0] in bots or who[c][1] in bots]


def bot_commit_rules(repo, branch, env, tgt, cfg, hook_cfg, head) -> list:
    """The bot's per-commit rules in a pipeline a person started (their push, GitLab's Rebase, "Run pipeline" or
    "Retry" on the bot's red head): whoever starts the pipeline, the commits the bot made in the MR (authored or
    committed as the bot: _bots_commits) follow the bot's path rules; the ones it committed are authored as the bot,
    and the ones it authored are committed by the bot or the run's owner (as in bot_push_rules); they are on a run
    branch (the hook lets the bot push nothing else: an MR from any other branch with bot commits is refused). Without
    them a person's next push, or a re-run, would turn the bot's red pipeline green."""
    from . import pre_receive
    bots = {e.lower() for e in hook_cfg.get("bot_emails") or []}
    default = hook_cfg["default_branch"]
    commits = pre_receive.new_commits(tgt.base, head, default, cwd=repo)
    # a promotion MR (main -> production): the bot's commits already on the default branch came through their run MRs
    if env.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME") != default and git(
            repo, "fetch", "-q", "origin", "+refs/heads/%s:refs/brain-ci/default" % default, check=False).returncode == 0:
        fresh = set(git_out(repo, "rev-list", head, "^" + tgt.base, "^refs/brain-ci/default").split())
        commits = [c for c in commits if c in fresh]
    who = _who(repo, commits)
    mine = _bots_commits(commits, who, bots)
    if not mine:
        return []
    b = parse_branch(branch, hook_cfg["branch_prefix"])
    probs = pre_receive.bot_path_problems(branch, mine, hook_cfg, cwd=repo)
    if not b:
        probs.append("%s: the bot made commits in this MR (%s), but bot accounts push only %s/<owner>/<JIRA>-<slug> "
                     "run branches: the bot's work comes through its run branch's MR" % (
                         branch, ", ".join(c[:12] for c in mine[:3]), hook_cfg["branch_prefix"]))
    forged = [c for c in mine if who[c][0] not in bots]
    if forged:
        probs.append("%s: bot commits are authored as the bot (%s): %s" % (
            branch, ", ".join(sorted(bots)) or "-",
            "; ".join("%s authored by %s but committed by the bot" % (c[:12], who[c][0] or "-") for c in forged[:3])))
    odd = [c for c in mine if who[c][1] not in bots]
    if odd:
        hand, signed, why = handover_commit(repo, branch, b["key"], env, tgt.base, cfg["runs_dir"]) if b else (
            None, None, "%s is not a run branch" % branch)
        owner = run_owner(repo, signed, bots) if hand else set()
        bad = [c for c in odd if who[c][1] not in owner]
        if bad:
            probs.append("%s: commits the bot authored are committed by the bot (%s) or the run's owner (%s)%s: %s" % (
                branch, ", ".join(sorted(bots)) or "-", ", ".join(sorted(owner)) or "-",
                "" if hand else " (the owner is not known here: %s)" % why,
                "; ".join("%s committed by %s" % (c[:12], who[c][1] or "-") for c in bad[:3])))
    return probs


def bot_push_rules(repo, branch, user, env, tgt, cfg, hook_cfg, head) -> list:
    """push-rules in a pipeline the bot's push started. The pipeline's user is the last pusher, not the author of every
    commit in the MR: the owner's own commits (`brain start`, local work, `brain handoff`) are not the bot's push.
    So, as the pre-receive hook judges each push by its pusher, commits are judged by who made them:
      - the branch: what the hook allows the bot (a well-formed <prefix>/<owner>/<KEY>-<slug> branch);
      - the bot's commits (author or committer in bot_emails: _bots_commits), wherever they are in the MR: the bot's
        path rules (no gates/, CI, .brain/, the kit's own files, ..., no runs/<KEY>/run.json); the ones the bot
        authored are committed by the bot or by the run's owner (GitLab's Rebase); a pipeline a person starts applies
        these too (bot_commit_rules);
      - the commits after the hand-over commit (the newest signed hand-over voucher for this branch) that are not the
        bot's: committed by the run's owner, people's rules (their own branch); anyone else is refused, and a bot
        committing another author's work is refused as the hook refuses it;
      - the commits before it are the owner's, judged when they pushed them.
    The run's owner is the author and committer of the commit the bridge signed (run_owner), not of its copy after a
    rebase: someone else's Rebase does not make them the owner. Without a hand-over voucher nothing tells the owner's
    commits from the bot's: the pipeline fails and says why. Weaker than the hook: it sees commits, not pushes (an
    author line can be forged; the hook refuses that)."""
    from . import pre_receive
    probs = pre_receive.decide([(head, head, "refs/heads/" + branch)], user, hook_cfg, cwd=repo)
    if probs:
        return probs
    default = hook_cfg["default_branch"]
    bots = {e.lower() for e in hook_cfg.get("bot_emails") or []}
    commits = pre_receive.new_commits(tgt.base, head, default, cwd=repo)
    who = _who(repo, commits)
    mine = _bots_commits(commits, who, bots)
    if mine:
        probs += pre_receive.bot_path_problems(branch, mine, hook_cfg, cwd=repo)
    b = parse_branch(branch, hook_cfg["branch_prefix"])
    hand, signed, why = handover_commit(repo, branch, b["key"], env, tgt.base, cfg["runs_dir"])
    if not hand:
        probs.append("%s: this pipeline was started by the bot (%s), but %s: without the hand-over commit push-rules "
                     "cannot tell the owner's commits from the bot's. A run branch gets bot commits only after a "
                     "hand-over the bridge signed; check the job's vouchers (brain-intake) and the bridge's hand-over "
                     "comment on the MR" % (branch, user, why))
        return probs
    owner = run_owner(repo, signed, bots)
    after = set(pre_receive.new_commits(hand, head, default, cwd=repo))
    alien = []
    for c in commits:
        ae, ce = who[c]
        if ae in bots:
            if ce not in bots | owner:
                alien.append("%s committed by %s" % (c[:12], ce or "-"))
        elif c in after and ce in bots:
            alien.append("%s authored by %s but committed by the bot" % (c[:12], ae or "-"))
        elif c in after and ce not in owner:
            alien.append("%s committed by %s" % (c[:12], ce or "-"))
    if alien:
        probs.append("%s: after the hand-over (%s) only the bot (%s) and the run's owner (%s) commit on this branch, "
                     "and bot commits are authored as the bot: %s" % (
                         branch, hand[:12], ", ".join(sorted(bots)) or "-", ", ".join(sorted(owner)) or "-",
                         "; ".join(alien[:3])))
    return probs


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
