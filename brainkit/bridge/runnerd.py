"""`brain-runnerd`: runs as the bot user on the team runner. Turns a signed hand-over request into a
workspace, so the bridge never needs the bot's git credentials and never switches user.

  brain-runnerd --config runnerd.json once|loop

For each spool/requests/<RUN-ID>.json not yet handled:
  verify the bridge signature and expiry -> fetch the project into a bare cache -> git worktree at the
  contract sha on the run branch -> write spool/ready/<RUN-ID>.json {path, head}. Failures go to spool/failed/.
config: {"spool_dir", "cache_dir", "runs_root", "allowed_signers", "clone_url": "ssh://git@gitlab.example/{project}.git",
         "interval": 30}
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

from .. import sign
from ..common import BrainError, git, git_out, load_json, parse_branch, parse_iso, utcnow, write_json

RUN_ID_RE = re.compile(r"^RUN-[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}-[1-9][0-9]{0,5}$")


def _cache(cfg, project) -> str:
    path = os.path.join(cfg["cache_dir"], re.sub(r"[^A-Za-z0-9._-]+", "__", project) + ".git")
    url = cfg["clone_url"].format(project=project)
    if not os.path.isdir(path):
        os.makedirs(cfg["cache_dir"], exist_ok=True)
        git(cfg["cache_dir"], "clone", "-q", "--bare", url, path)
        # Not a mirror: pushing from a worktree must only ever push the one run branch.
        git(path, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    git(path, "fetch", "-q", "--prune", "origin")
    return path


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
    b = parse_branch(v.get("branch", ""))
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
        raise BrainError("workspace %s already exists" % path)
    git(cache, "worktree", "add", "-q", "-B", v["branch"], path, v["sha"])
    git(path, "branch", "-q", "--set-upstream-to", "origin/" + v["branch"])
    head = git_out(path, "rev-parse", "HEAD")
    if head != v["sha"]:
        raise BrainError("worktree head %s != contract sha" % head[:12])
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
    if os.path.isdir(os.path.join(d, ".git")):
        git(d, "pull", "-q", "--ff-only", check=False)
    for m in materials:
        src = os.path.join(d, "packages", m["package"])
        if not os.path.isdir(src):
            raise BrainError("material %s not in %s" % (m["package"], d))
        if intake.manifest_digest(src) != m["manifest_sha256"] or intake.verify_package(src):
            raise BrainError("material %s does not match the contract" % m["package"])
        _readonly_copy(src, os.path.join(workspace, MATERIALS_DIR, m["package"]))


def once(cfg, out=print) -> int:
    spool = cfg["spool_dir"]
    done = 0
    req_dir = os.path.join(spool, "requests")
    if not os.path.isdir(req_dir):
        return 0
    for name in sorted(os.listdir(req_dir)):
        if not name.endswith(".json"):
            continue
        run_id = name[:-5]
        if any(os.path.exists(os.path.join(spool, d, name)) for d in ("ready", "failed")):
            continue
        try:
            ready = prepare(cfg, load_json(os.path.join(req_dir, name)))
            write_json(os.path.join(spool, "ready", name), ready, mode=0o640)
            out("ready %s at %s" % (run_id, ready["path"]))
        except (BrainError, ValueError, KeyError) as e:
            write_json(os.path.join(spool, "failed", name), {"run_id": run_id, "error": str(e)}, mode=0o640)
            out("failed %s: %s" % (run_id, e))
        done += 1
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(prog="brain-runnerd")
    ap.add_argument("--config", required=True)
    ap.add_argument("cmd", choices=("once", "loop"))
    a = ap.parse_args(argv)
    cfg = load_json(a.config)
    try:
        while True:
            once(cfg)
            if a.cmd == "once":
                return 0
            time.sleep(cfg.get("interval", 30))
    except BrainError as e:
        print("brain-runnerd: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
