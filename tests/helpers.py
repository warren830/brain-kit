"""Test helpers: temp git repos and an in-process fake for GitLab / Jira / KiroCrew (no sockets)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEAN_SRC = os.environ.get("BRAIN_TEST_LEAN") or os.path.join(os.path.dirname(ROOT), "aidlc", "aidlc-lean")  # an AI-DLC Lean checkout


def sh(*cmd, cwd=None, check=True, env=None):
    e = dict(os.environ)
    e.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
              "GIT_COMMITTER_EMAIL": "t@example.com"})
    e.update(env or {})
    p = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, env=e)
    if check and p.returncode != 0:
        raise AssertionError("%s failed: %s %s" % (" ".join(cmd), p.stdout, p.stderr))
    return p


def tmpdir(test):
    d = tempfile.mkdtemp(prefix="brainkit-")
    test.addCleanup(shutil.rmtree, d, True)
    return os.path.realpath(d)


def git_repo(path):
    os.makedirs(path, exist_ok=True)
    sh("git", "init", "-q", "-b", "main", path)
    sh("git", "-C", path, "config", "user.name", "t")
    sh("git", "-C", path, "config", "user.email", "t@example.com")
    sh("git", "-C", path, "config", "commit.gpgsign", "false")
    return path


def commit_file(repo, rel, text, msg="c", env=None):
    p = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    sh("git", "-C", repo, "add", "--", rel)
    sh("git", "-C", repo, "commit", "-q", "-m", msg, env=env)
    return sh("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()


def keypair(d):
    key = os.path.join(d, "bridge_ed25519")
    sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key, "-C", "brain-bridge")
    from brainkit import sign
    allowed = os.path.join(d, "allowed_signers")
    sign.write_allowed_signers(allowed, key + ".pub")
    return key, allowed


class Fake:
    """Routes (method, regex on path) -> handler(match, query, body) -> (status, obj_or_bytes)."""

    def __init__(self):
        self.routes = []
        self.calls = []

    def on(self, method, pattern, handler):
        self.routes.append((method, re.compile("^" + pattern + "$"), handler))

    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(u.path)
        query = dict(urllib.parse.parse_qsl(u.query))
        data = json.loads(body.decode()) if body else None
        self.calls.append((method, path, query, data, dict(headers)))
        for m, rx, h in self.routes:
            mm = rx.match(path)
            if m == method and mm:
                status, out = h(mm, query, data)
                if isinstance(out, (bytes, bytearray)):
                    return status, bytes(out)
                return status, json.dumps(out).encode() if out is not None else b""
        return 404, b'{"message":"404 Not Found (fake)"}'

    def called(self, method, pattern):
        rx = re.compile("^" + pattern + "$")
        return [c for c in self.calls if c[0] == method and rx.match(c[1])]
