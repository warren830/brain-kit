"""Shared helpers: names, git, JSON files, time."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

ZERO_SHA = "0" * 40
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
JIRA_RE = re.compile(r"^[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}$")
USER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# ai/<owner>/<JIRA>-<slug>
BRANCH_RE = re.compile(r"^(?P<prefix>[a-z]+)/(?P<owner>[a-z0-9][a-z0-9._-]{0,63})/"
                       r"(?P<key>[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6})(?:-(?P<slug>[a-z0-9][a-z0-9-]{0,39}))?$")


class BrainError(Exception):
    """A user-facing failure: the message says what is wrong and what to do."""


def path_base(spec: str) -> str:
    """The folder (or file) a repo path or glob is about: the spec itself without './' and trailing '/'; for a glob
    (*, ?, [), the folder before its first wildcard ('' at the top level). Lean reads a unit's Paths as fnmatch globs,
    where * also matches '/', so a glob covers everything below that folder."""
    s = str(spec or "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    cut = min([s.find(c) for c in "*?[" if c in s] or [-1])
    if cut >= 0:
        s = s[:cut]
        s = s[:s.rfind("/")] if "/" in s else ""
    return s.strip("/")


def paths_overlap(a: str, b: str) -> bool:
    """Could one file be under both repo paths / globs? One names the other or a folder that holds it (a glob counts
    as its folder: path_base). Errs on the side of yes: it feeds warnings."""
    x, y = path_base(a), path_base(b)
    if not x or not y:
        return True
    return x == y or x.startswith(y + "/") or y.startswith(x + "/")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def later(hours: float) -> str:
    return iso(utcnow() + timedelta(hours=hours))


def branch_name(prefix: str, owner: str, jira: str, slug: str) -> str:
    return "%s/%s/%s-%s" % (prefix, owner, jira, slug)


def parse_branch(name: str, prefix: str = "ai"):
    """Return dict(owner, jira, slug) for a run branch, or None."""
    m = BRANCH_RE.match(name or "")
    if not m or m.group("prefix") != prefix:
        return None
    return {"owner": m.group("owner"), "key": m.group("key"), "slug": m.group("slug") or ""}


def run(cmd, cwd=None, check=True, input_text=None, env=None):
    """Run a command; return CompletedProcess with text output."""
    p = subprocess.run(cmd, cwd=cwd, input=input_text, capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise BrainError("command failed (%d): %s\n%s" % (p.returncode, " ".join(cmd), (p.stderr or p.stdout).strip()))
    return p


def git(repo, *args, check=True):
    return run(["git", "-C", repo] + list(args), check=check)


def git_out(repo, *args) -> str:
    return git(repo, *args).stdout.strip()


class NetworkError(BrainError):
    """A git command that talks to a remote failed or did not finish in time: worth trying again later."""


NET_TIMEOUT = 120.0


def git_net(repo, *args, timeout=NET_TIMEOUT, check=True):
    """A git command that talks to a remote (clone, fetch, pull, push). It never waits for a password prompt, and
    it is stopped after `timeout` seconds together with everything it started (ssh, a credential helper: they run
    in its own process group), so a hung connection cannot hold the caller. NetworkError when it times out, or when
    it fails and `check` is set; otherwise its CompletedProcess."""
    cmd = ["git", "-C", repo] + list(args)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env,
                         start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException as e:   # the timeout, or a stop (SIGTERM) while it ran: nothing it started stays behind
        try:
            os.killpg(p.pid, 9)
        except (OSError, AttributeError):
            p.kill()
        p.communicate()
        if isinstance(e, subprocess.TimeoutExpired):
            raise NetworkError("%s did not finish within %gs" % (" ".join(cmd), timeout))
        raise
    out, err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    if check and p.returncode != 0:
        raise NetworkError("command failed (%d): %s\n%s" % (p.returncode, " ".join(cmd), (err or out).strip()))
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def repo_root(start=None) -> str:
    p = run(["git", "-C", start or os.getcwd(), "rev-parse", "--show-toplevel"], check=False)
    if p.returncode != 0:
        raise BrainError("not inside a git repository")
    return p.stdout.strip()


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def canonical(obj) -> bytes:
    """Stable bytes for hashing and signing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


CTRL_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")


def plain(value) -> bool:
    """True for a one-line string without control characters (safe to put in a message line)."""
    return isinstance(value, str) and not CTRL_RE.search(value)


def write_json(path, obj, mode=None):
    """Atomic write (tmp file + rename) so readers never see half a file. `mode` e.g. 0o640 for shared spools."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.write("\n")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
