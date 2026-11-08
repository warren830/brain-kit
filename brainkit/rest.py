"""Minimal JSON REST clients for GitLab, Jira and the KiroCrew gateway.

Every client takes a `transport(method, url, headers, body_bytes) -> (status, body_bytes)`
so tests can inject a fake instead of opening sockets.
"""
from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .common import BrainError


def urllib_transport(method, url, headers, body, timeout=20):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise BrainError("cannot reach %s: %s" % (urllib.parse.urlsplit(url).netloc, e.reason))
    except (OSError, http.client.HTTPException) as e:  # timeouts and dropped connections mid-response
        raise BrainError("%s %s failed: %s" % (method, urllib.parse.urlsplit(url).netloc, e or type(e).__name__))


try:  # optional machine-local transport (gitignored): transport_for(base) -> transport or None
    from . import local_transport
except ImportError:  # pragma: no cover
    local_transport = None


DEFAULT_TOKEN_FILE = "~/.brain/gitlab-token"


def read_token(env_name="GITLAB_TOKEN", file_env="BRAIN_GITLAB_TOKEN_FILE", default_file=DEFAULT_TOKEN_FILE) -> str:
    """A token from the environment, else from a file only its owner can read (never printed anywhere)."""
    tok = os.environ.get(env_name, "").strip()
    if tok:
        return tok
    path = os.path.expanduser(os.environ.get(file_env) or default_file)
    if not os.path.exists(path):
        return ""
    st = os.stat(path)
    if st.st_mode & 0o077:
        raise BrainError("%s is readable by others; run: chmod 600 %s" % (path, path))
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


class Rest:
    def __init__(self, base, headers=None, transport=None):
        if not base:
            raise BrainError("REST base URL is not configured")
        self.base = base.rstrip("/")
        self.headers = dict(headers or {})
        self.transport = transport or urllib_transport

    def call(self, method, path, query=None, body=None, ok=(200, 201, 204), raw=False):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = dict(self.headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, payload = self.transport(method, url, headers, data)
        if status not in ok:
            snippet = (payload or b"")[:300].decode("utf-8", "replace")
            raise HttpError(status, "%s %s -> HTTP %d: %s" % (method, path, status, snippet))
        if raw:
            return payload
        if not payload:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except ValueError:
            raise BrainError("%s %s returned non-JSON" % (method, path))


class HttpError(BrainError):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def q(s) -> str:
    return urllib.parse.quote(str(s), safe="")


class GitLab(Rest):
    """GitLab REST v4. `project` is a numeric id or 'group/repo'."""

    def __init__(self, base, token, transport=None):
        if transport is None and local_transport is not None:
            transport = local_transport.transport_for(base)
        super().__init__(base.rstrip("/") + "/api/v4", {"PRIVATE-TOKEN": token or ""}, transport)

    def current_user(self):
        return self.call("GET", "/user")

    def project(self, project):
        return self.call("GET", "/projects/%s" % q(project))

    def mrs_with_label(self, project, label):
        return self.call("GET", "/projects/%s/merge_requests" % q(project),
                         {"state": "opened", "labels": label, "per_page": 100}) or []

    def mr(self, project, iid):
        return self.call("GET", "/projects/%s/merge_requests/%d" % (q(project), int(iid)))

    def mr_for_branch(self, project, branch):
        rows = self.call("GET", "/projects/%s/merge_requests" % q(project),
                         {"state": "opened", "source_branch": branch}) or []
        return rows[0] if rows else None

    def create_mr(self, project, source, target, title, labels, description=None):
        body = {"source_branch": source, "target_branch": target, "title": title,
                "labels": ",".join(labels), "remove_source_branch": False}
        if description:
            body["description"] = description
        return self.call("POST", "/projects/%s/merge_requests" % q(project), body=body)

    def edit_labels(self, project, iid, add=(), remove=()):
        body = {}
        if add:
            body["add_labels"] = ",".join(add)
        if remove:
            body["remove_labels"] = ",".join(remove)
        return self.call("PUT", "/projects/%s/merge_requests/%d" % (q(project), int(iid)), body=body)

    def label_events(self, project, iid, per_page=100, max_pages=50):
        out = []
        for page in range(1, max_pages + 1):
            rows = self.call("GET", "/projects/%s/merge_requests/%d/resource_label_events" % (q(project), int(iid)),
                             {"per_page": per_page, "page": page}) or []
            out += rows
            if len(rows) < per_page:
                return out
        raise BrainError("too many label events on !%s" % iid)

    def comment(self, project, iid, text):
        return self.call("POST", "/projects/%s/merge_requests/%d/notes" % (q(project), int(iid)), body={"body": text})

    def mr_notes(self, project, iid, per_page=100, max_pages=20):
        """All notes on an MR, oldest first."""
        out = []
        for page in range(1, max_pages + 1):
            rows = self.call("GET", "/projects/%s/merge_requests/%d/notes" % (q(project), int(iid)),
                             {"sort": "asc", "order_by": "created_at", "per_page": per_page, "page": page}) or []
            out += rows
            if len(rows) < per_page:
                break
        return out

    def file_raw(self, project, path, ref):
        """File bytes at ref, or None when it does not exist."""
        try:
            return self.call("GET", "/projects/%s/repository/files/%s/raw" % (q(project), q(path)), {"ref": ref},
                             raw=True)
        except HttpError as e:
            if e.status == 404:
                return None
            raise

    def commit_exists(self, project, sha):
        try:
            self.call("GET", "/projects/%s/repository/commits/%s" % (q(project), q(sha)))
            return True
        except HttpError as e:
            if e.status == 404:
                return False
            raise


class Jira(Rest):
    """Jira REST (Server/DC api 2 by default; Cloud works with api 3 for these reads)."""

    def __init__(self, base, token, api="2", transport=None, basic_user=None):
        if basic_user:  # Cloud: email + API token
            import base64
            auth = "Basic " + base64.b64encode(("%s:%s" % (basic_user, token)).encode()).decode()
        else:  # DC: personal access token
            auth = "Bearer %s" % (token or "")
        super().__init__(base.rstrip("/") + "/rest/api/" + str(api), {"Authorization": auth,
                                                                       "Accept": "application/json"}, transport)

    def myself(self):
        return self.call("GET", "/myself")

    def issue(self, key):
        return self.call("GET", "/issue/%s" % q(key), {"fields": "assignee,status"})

    def comment(self, key, text):
        return self.call("POST", "/issue/%s/comment" % q(key), body={"body": text})


def jira_user_id(user) -> str:
    """Stable id of a Jira user object: accountId (Cloud) or name/key (DC)."""
    if not user:
        return ""
    return str(user.get("accountId") or user.get("name") or user.get("key") or "")
