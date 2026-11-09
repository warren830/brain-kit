"""`brain-runnerd`: runs as the bot user on the team runner. Turns a signed hand-over request into a
workspace, so the bridge never needs the bot's git credentials and never switches user.

  brain-runnerd --config runnerd.json once|loop

For each spool/requests/<RUN-ID>.json not yet handled:
  verify the bridge signature and expiry -> fetch the project into a bare cache -> git worktree at the
  contract sha on the run branch -> write spool/ready/<RUN-ID>.json {path, head}. Failures go to spool/failed/:
  {"run_id", "error", "transient", "tries", "attempt", "final", "at"}. A transient failure (a git fetch, clone or
  pull that failed or timed out: the network, GitLab, an ssh agent not up yet) is tried again on the next rounds, up
  to `tries` times; then it is final. The bridge reads spool/failed: it asks again (the request's "attempt", outside
  the signed voucher, goes up) or tells the MR. A failed request is not looked at again until then.
  A workspace a stopped runnerd left (git worktree add done, materials not copied yet) is used again when it is a
  clean worktree of the project cache at the contract sha on the run branch; otherwise the request fails.
config: {"spool_dir", "cache_dir", "runs_root", "allowed_signers", "clone_url": "ssh://git@gitlab.example/{project}.git",
         "interval": 30, "git_timeout": 300 (seconds for one fetch or pull), "clone_timeout": 3600, "tries": 3,
         "branch_prefix": "" (only this branch prefix; default: the one the signed voucher names)}
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time

from .. import sign
from ..common import (BRANCH_RE, BrainError, NetworkError, git, git_net, git_out, iso, load_json, parse_branch,
                      parse_iso, utcnow, write_json)

RUN_ID_RE = re.compile(r"^RUN-[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}-[1-9][0-9]{0,5}$")
TRIES = 3


def _timeout(cfg, key, default) -> float:
    return sign.seconds(cfg.get(key), default)


def _cache(cfg, project) -> str:
    path = os.path.join(cfg["cache_dir"], re.sub(r"[^A-Za-z0-9._-]+", "__", project) + ".git")
    url = cfg["clone_url"].format(project=project)
    if not os.path.isdir(path):
        os.makedirs(cfg["cache_dir"], exist_ok=True)
        # cloned beside it and moved in place: a clone cut off half-way never passes for the cache
        tmp = "%s.tmp-%d" % (path, os.getpid())
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            git_net(cfg["cache_dir"], "clone", "-q", "--bare", url, tmp, timeout=_timeout(cfg, "clone_timeout", 3600.0))
            # Not a mirror: pushing from a worktree must only ever push the one run branch.
            git(tmp, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
            os.rename(tmp, path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    git_net(path, "fetch", "-q", "--prune", "origin", timeout=_timeout(cfg, "git_timeout", 300.0))
    return path


def _not_reusable(cache, path, v) -> str:
    """'' when the workspace at `path` is one a stopped runnerd left for this contract: a worktree of the project
    cache, at the contract sha, on the run branch, with no change to a tracked file. Else why not."""
    p = git(path, "rev-parse", "--git-common-dir", check=False)
    if p.returncode != 0:
        return "is not a git worktree"
    common = p.stdout.strip()
    if os.path.realpath(common if os.path.isabs(common) else os.path.join(path, common)) != os.path.realpath(cache):
        return "is not a worktree of the project cache"
    if git(path, "rev-parse", "HEAD", check=False).stdout.strip() != v["sha"]:
        return "is not at the contract sha"
    if git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() != v["branch"]:
        return "is not on the run branch"
    if git(path, "status", "--porcelain", "--untracked-files=no", check=False).stdout.strip():
        return "has changes"
    return ""


def prepare(cfg, req) -> dict:
    v, s = req.get("voucher") or {}, req.get("sig") or ""
    if not sign.verify(v, s, cfg["allowed_signers"]):
        raise BrainError("bridge signature does not verify")
    if v.get("kind") != "handoff":
        raise BrainError("not a hand-over voucher")
    if parse_iso(v["expires_at"]) < utcnow():
        raise BrainError("voucher expired at %s" % v["expires_at"])
    run_id = v.get("run_id", "")
    if not RUN_ID_RE.match(run_id):
        raise BrainError("bad run id %r" % run_id)
    m = BRANCH_RE.match(v.get("branch") or "")
    b = parse_branch(v.get("branch", ""), cfg.get("branch_prefix") or (m.group("prefix") if m else "ai"))
    if not b or b["owner"] != v.get("owner") or b["key"] != v.get("key"):
        raise BrainError("voucher branch/owner/jira do not agree")
    cache = _cache(cfg, v["project"])
    if git(cache, "cat-file", "-e", v["sha"] + "^{commit}", check=False).returncode != 0:
        raise BrainError("commit %s not found in %s" % (v["sha"][:12], v["project"]))
    remote_ref = "refs/remotes/origin/" + v["branch"]
    if git(cache, "merge-base", "--is-ancestor", v["sha"], remote_ref, check=False).returncode != 0:
        raise BrainError("contract sha %s is not on %s" % (v["sha"][:12], v["branch"]))
    path = os.path.join(cfg["runs_root"], run_id)
    if os.path.exists(path):
        why = _not_reusable(cache, path, v)
        if why:
            raise BrainError("workspace %s already exists and %s: look at it, move it away, then hand over again"
                             % (path, why))
    else:
        git(cache, "worktree", "prune")         # a worktree whose folder was removed by hand
        git(cache, "worktree", "add", "-q", "-B", v["branch"], path, v["sha"])
    git(path, "branch", "-q", "--set-upstream-to", "origin/" + v["branch"])
    head = git_out(path, "rev-parse", "HEAD")
    if head != v["sha"]:
        raise BrainError("worktree head %s != contract sha" % head[:12])
    if os.path.exists(os.path.join(path, ".kiro", "aidlc-lean", "aidlc_lean.py")) and \
            not os.path.exists(os.path.join(path, ".kiro", "brain", "lean-hook.py")):
        raise BrainError("the branch has AI-DLC Lean but not .kiro/brain/lean-hook.py: the session's hooks would "
                         "block every tool call; commit the adapter on main and hand over again")
    copy_materials(cfg, v.get("materials") or [], path)
    return {"run_id": run_id, "path": os.path.realpath(path), "head": head}


def copy_materials(cfg, materials, workspace):
    """Read-only copies of the run's material packages, checked against the hashes in the signed contract."""
    if not materials:
        return
    from ..cli import MATERIALS_DIR, _readonly_copy
    from ..intake import package as intake
    d = cfg.get("intake_dir")
    if not d:
        raise BrainError("the run uses material packages but runnerd has no intake_dir")
    pulled = ""
    if os.path.isdir(os.path.join(d, ".git")):
        try:
            p = git_net(d, "pull", "-q", "--ff-only", timeout=_timeout(cfg, "git_timeout", 300.0), check=False)
            pulled = "" if p.returncode == 0 else (p.stderr or p.stdout).strip()[:300] or "git pull failed"
        except NetworkError as e:
            pulled = str(e)
    for m in materials:
        src = os.path.join(d, "packages", m["package"])
        bad = not os.path.isdir(src) or intake.manifest_digest(src) != m["manifest_sha256"] or \
            intake.verify_package(src)
        if bad and pulled:          # the clone could not be brought up to date: tried again
            raise NetworkError("brain-intake (%s) could not be updated: %s" % (d, pulled))
        if not os.path.isdir(src):
            raise BrainError("material %s not in %s" % (m["package"], d))
        if bad:
            raise BrainError("material %s does not match the contract" % m["package"])
        _readonly_copy(src, os.path.join(workspace, MATERIALS_DIR, m["package"]))


def _attempt(doc) -> int:
    try:
        return max(0, int((doc or {}).get("attempt") or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def once(cfg, out=print) -> int:
    spool = cfg["spool_dir"]
    done = 0
    req_dir = os.path.join(spool, "requests")
    if not os.path.isdir(req_dir):
        return 0
    tries_max = int(cfg.get("tries") or TRIES)
    for name in sorted(os.listdir(req_dir)):
        if not name.endswith(".json"):
            continue
        run_id = name[:-5]
        if os.path.exists(os.path.join(spool, "ready", name)):
            continue
        fpath = os.path.join(spool, "failed", name)
        try:
            req = load_json(os.path.join(req_dir, name))
        except (OSError, ValueError):
            req = None
        attempt, tries = _attempt(req), 1
        if os.path.exists(fpath):
            try:
                f = load_json(fpath)
            except (OSError, ValueError):
                f = {}
            f = f if isinstance(f, dict) else {}
            if _attempt(f) >= attempt:              # this attempt failed before
                if f.get("final", True) or not f.get("transient"):
                    continue                        # given up (the bridge may ask again: a newer attempt)
                tries = int(f.get("tries") or 1) + 1
        try:
            if not isinstance(req, dict):
                raise BrainError("the request is not readable JSON")
            ready = prepare(cfg, req)
            write_json(os.path.join(spool, "ready", name), ready, mode=0o640)
            if os.path.exists(fpath):
                os.unlink(fpath)
            out("ready %s at %s" % (run_id, ready["path"]))
        except Exception as e:  # noqa: BLE001 - one request must not stop the others; a stop (Ctrl-C) is not caught
            transient = isinstance(e, (NetworkError, OSError))
            final = not transient or tries >= tries_max
            write_json(fpath, {"run_id": run_id, "error": str(e), "transient": transient, "tries": tries,
                               "attempt": attempt, "final": final, "at": iso(utcnow())}, mode=0o640)
            out("failed %s%s: %s" % (run_id, "" if final else " (tried %d of %d, again next round)"
                                     % (tries, tries_max), e))
        done += 1
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(prog="brain-runnerd")
    ap.add_argument("--config", required=True)
    ap.add_argument("cmd", choices=("once", "loop"))
    a = ap.parse_args(argv)
    try:
        cfg = load_json(a.config)
    except (OSError, ValueError) as e:
        print("brain-runnerd: %s cannot be read: %s" % (a.config, e), file=sys.stderr)
        return 2
    while True:
        try:
            once(cfg)
        except Exception as e:  # noqa: BLE001 - one bad round (the spool, the disk) must not stop runnerd
            if a.cmd == "once":
                print("brain-runnerd: %s" % e, file=sys.stderr)
                return 2
            print("brain-runnerd: this round failed: %s" % e, file=sys.stderr)
        if a.cmd == "once":
            return 0
        time.sleep(cfg.get("interval", 30))


if __name__ == "__main__":
    sys.exit(main())
