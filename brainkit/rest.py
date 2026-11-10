"""Minimal JSON REST clients for GitLab, Jira and the KiroCrew gateway.

Every client takes a `transport(method, url, headers, body_bytes) -> (status, body_bytes)`
so tests can inject a fake instead of opening sockets.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .common import BrainError


def urllib_transport(method, url, headers, body, timeout=20):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, read_error_body(e, method, url)
    except urllib.error.URLError as e:
        raise BrainError("cannot reach %s: %s" % (urllib.parse.urlsplit(url).netloc, e.reason))
    except (OSError, http.client.HTTPException) as e:  # timeouts and dropped connections mid-response
        raise BrainError("%s %s failed: %s" % (method, urllib.parse.urlsplit(url).netloc, e or type(e).__name__))


def read_error_body(e, method, url) -> bytes:
    """The body of an HTTP error response. Reading it can time out or lose the connection as well (a 502 from a
    proxy, say): that is a BrainError like any other network failure, so callers that survive BrainError survive it."""
    try:
        return e.read()
    except (OSError, http.client.HTTPException, ValueError) as x:
        raise BrainError("%s %s -> HTTP %s, and its body could not be read: %s"
                         % (method, urllib.parse.urlsplit(url).netloc, e.code, x or type(x).__name__))


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

    def call(self, method, path, query=None, body=None, ok=(200, 201, 204), raw=False, headers=None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = dict(self.headers, **(headers or {}))
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


GL_TIME_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:?\d\d)$")


def gl_time(s):
    """A GitLab time (2026-11-08T12:00:00.123Z, 2026-11-08T20:00:00.123+08:00) as an aware UTC datetime.
    None when it cannot be read, or has no zone: such a time cannot be compared with another one."""
    if isinstance(s, datetime):
        return s if s.tzinfo else None
    m = GL_TIME_RE.match(str(s or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, se, frac, zone = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se), int((frac or "0")[:6].ljust(6, "0")),
                      tzinfo=timezone.utc)
    except ValueError:
        return None
    if zone != "Z":
        off = timedelta(hours=int(zone[1:3]), minutes=int(zone[-2:]))
        dt = dt - off if zone[0] == "+" else dt + off
    return dt


def head_pushed_at(versions, sha):
    """When GitLab recorded `sha` as the MR's head: the newest MR version (diff) whose head_commit_sha is `sha`.

    GitLab's server writes these times; a commit's own dates are whatever the pusher typed, so they are never used.
    The newest one: a branch pushed away from `sha` and back to it was pushed again. None when there is none."""
    times = [gl_time(v.get("created_at")) for v in versions or [] if isinstance(v, dict)
             and v.get("head_commit_sha") == sha]
    times = [t for t in times if t is not None]
    return max(times) if times else None


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

    def mrs_for_branch(self, project, branch):
        """The open MRs from `branch`, whatever branch they go into."""
        return [m for m in self.call("GET", "/projects/%s/merge_requests" % q(project),
                                     {"state": "opened", "source_branch": branch}) or [] if isinstance(m, dict)]

    def mr_for_branch(self, project, branch):
        rows = self.mrs_for_branch(project, branch)
        return rows[0] if rows else None

    def create_mr(self, project, source, target, title, labels, description=None):
        body = {"source_branch": source, "target_branch": target, "title": title,
                "labels": ",".join(labels), "remove_source_branch": False}
        if description:
            body["description"] = description
        return self.call("POST", "/projects/%s/merge_requests" % q(project), body=body)

    def edit_mr(self, project, iid, description=None, title=None):
        """Set an MR's description and/or title (PUT /projects/:id/merge_requests/:iid). Needs a token that may
        edit the MR (its author, a Developer or more): the bridge's Reporter account may not."""
        body = {}
        if description is not None:
            body["description"] = description
        if title is not None:
            body["title"] = title
        if not body:
            raise BrainError("edit_mr: nothing to change")
        return self.call("PUT", "/projects/%s/merge_requests/%d" % (q(project), int(iid)), body=body)

    def edit_note(self, project, iid, note_id, text):
        """Replace the text of a note on an MR (PUT .../merge_requests/:iid/notes/:note_id). GitLab lets an account
        edit its own notes (a Reporter too); HttpError 404 when the note is gone."""
        return self.call("PUT", "/projects/%s/merge_requests/%d/notes/%d" % (q(project), int(iid), int(note_id)),
                         body={"body": text})

    def edit_labels(self, project, iid, add=(), remove=()):
        body = {}
        if add:
            body["add_labels"] = ",".join(add)
        if remove:
            body["remove_labels"] = ",".join(remove)
        return self.call("PUT", "/projects/%s/merge_requests/%d" % (q(project), int(iid)), body=body)

    def pages(self, path, query, per_page, max_pages, what):
        """Every row of a paged list, or BrainError when there are more than per_page * max_pages: a list cut short
        must not pass for the whole one. Exactly that many is fine (the page after the last is asked for)."""
        out = []
        for page in range(1, max_pages + 1):
            rows = self.call("GET", path, dict(query, per_page=per_page, page=page)) or []
            out += rows
            if len(rows) < per_page:
                return out
        if self.call("GET", path, dict(query, per_page=per_page, page=max_pages + 1)):
            raise BrainError("too many %s (more than %d): not read" % (what, per_page * max_pages))
        return out

    def label_events(self, project, iid, per_page=100, max_pages=50):
        return self.pages("/projects/%s/merge_requests/%d/resource_label_events" % (q(project), int(iid)), {},
                          per_page, max_pages, "label events on !%s" % iid)

    def comment(self, project, iid, text):
        return self.call("POST", "/projects/%s/merge_requests/%d/notes" % (q(project), int(iid)), body={"body": text})

    def mr_notes(self, project, iid, per_page=100, max_pages=20):
        """All notes on an MR, oldest first. More than max_pages pages is an error, not a short list: a flood of
        notes must not hide a later answer (a conflicting one, say) from whoever reads them."""
        return self.pages("/projects/%s/merge_requests/%d/notes" % (q(project), int(iid)),
                          {"sort": "asc", "order_by": "created_at"}, per_page, max_pages, "notes on !%s" % iid)

    def mr_versions(self, project, iid, per_page=100, max_pages=20):
        """The MR's versions (one per push GitLab recorded: head_commit_sha, created_at), all pages."""
        return self.pages("/projects/%s/merge_requests/%d/versions" % (q(project), int(iid)), {}, per_page,
                          max_pages, "versions on !%s" % iid)

    def file_raw(self, project, path, ref):
        """File bytes at ref, or None when it does not exist."""
        try:
            return self.call("GET", "/projects/%s/repository/files/%s/raw" % (q(project), q(path)), {"ref": ref},
                             raw=True)
        except HttpError as e:
            if e.status == 404:
                return None
            raise

    def commit(self, project, sha):
        """The commit (id, parent_ids, ...) as GitLab shows it, or None when it does not exist."""
        try:
            return self.call("GET", "/projects/%s/repository/commits/%s" % (q(project), q(sha))) or {}
        except HttpError as e:
            if e.status == 404:
                return None
            raise

    def commit_exists(self, project, sha):
        return self.commit(project, sha) is not None

    def pipeline_jobs(self, project, pipeline_id, scope="failed", per_page=100):
        """The jobs of a pipeline in `scope` (GET /projects/:id/pipelines/:pipeline_id/jobs, one page: a pipeline
        with more than `per_page` failed jobs is shown by its first ones)."""
        return self.call("GET", "/projects/%s/pipelines/%d/jobs" % (q(project), int(pipeline_id)),
                         {"scope[]": scope, "per_page": per_page}) or []

    def job_trace(self, project, job_id):
        """A job's log (bytes), or None when GitLab does not show it (no log yet, or no access to it)."""
        try:
            return self.call("GET", "/projects/%s/jobs/%d/trace" % (q(project), int(job_id)), raw=True)
        except HttpError as e:
            if e.status in (403, 404):
                return None
            raise

    # ---------- GraphQL (what REST does not show) ----------

    def graphql_url(self) -> str:
        root = self.base[:-len("/api/v4")] if self.base.endswith("/api/v4") else self.base
        return root + "/api/graphql"

    def graphql(self, query, variables=None):
        """POST <base>/api/graphql with the same token and transport as REST. The response's "data", or BrainError
        (HTTP error, GraphQL errors, no data): a caller that cannot read it must fail closed."""
        headers = dict(self.headers, **{"Content-Type": "application/json"})
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        status, payload = self.transport("POST", self.graphql_url(), headers, body)
        if status != 200:
            raise HttpError(status, "POST /api/graphql -> HTTP %d: %s"
                            % (status, (payload or b"")[:300].decode("utf-8", "replace")))
        try:
            doc = json.loads((payload or b"").decode("utf-8"))
        except ValueError:
            raise BrainError("POST /api/graphql returned non-JSON")
        if not isinstance(doc, dict):
            raise BrainError("POST /api/graphql returned no object")
        errs = doc.get("errors")
        if errs:
            first = errs[0] if isinstance(errs, list) and errs else errs
            msg = first.get("message") if isinstance(first, dict) else first
            raise BrainError("GitLab GraphQL: %s" % " ".join(str(msg).split())[:300])
        if not isinstance(doc.get("data"), dict):
            raise BrainError("GitLab GraphQL returned no data")
        return doc["data"]

    NOTE_EDITS_QUERY = (
        "query($path: ID!, $iid: String!, $after: String) { project(fullPath: $path) { mergeRequest(iid: $iid) { "
        "notes(first: 100, after: $after) { pageInfo { hasNextPage endCursor } "
        "nodes { id author { username } lastEditedAt lastEditedBy { username } } } } } }")

    def note_edits(self, project, iid, max_pages=20) -> dict:
        """{note id: {"author", "last_edited_at", "last_edited_by"}} for the MR's notes, from GitLab GraphQL
        (Note.lastEditedAt / lastEditedBy). Measured on a self-managed GitLab 2026-11-09: lastEditedBy names the editor only
        when a note's text was edited (by its author, or by a Maintainer or admin editing someone else's note; ticking
        a task-list box is a text edit); an award emoji moves updated_at and lastEditedAt but leaves lastEditedBy
        empty, and an untouched note has lastEditedAt equal to its creation time. Use lastEditedBy, not lastEditedAt.
        BrainError when GitLab does not answer (callers fail closed)."""
        path = str(project)
        if path.isdigit():                      # GraphQL takes the project's full path, not its id
            path = str((self.project(project) or {}).get("path_with_namespace") or "")
            if not path:
                raise BrainError("GitLab shows no path for project %s" % project)
        out, after = {}, None
        for _ in range(max_pages):
            data = self.graphql(self.NOTE_EDITS_QUERY, {"path": path, "iid": str(int(iid)), "after": after})
            mr = ((data.get("project") or {}).get("mergeRequest")) if isinstance(data.get("project"), dict) else None
            notes = (mr or {}).get("notes") if isinstance(mr, dict) else None
            if not isinstance(notes, dict) or not isinstance(notes.get("nodes"), list):
                raise BrainError("GitLab GraphQL shows no notes for %s!%s" % (project, iid))
            for n in notes["nodes"]:
                nid = _gid_number(n.get("id")) if isinstance(n, dict) else None
                if nid is None:
                    continue
                by = n.get("lastEditedBy")
                out[nid] = {"author": str(((n.get("author") or {}).get("username")) or "").lower(),
                            "last_edited_at": n.get("lastEditedAt") or None,
                            "last_edited_by": str(by.get("username") or "").lower() or None
                            if isinstance(by, dict) else None}
            info = notes.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return out
            after = info.get("endCursor")
            if not after:
                break
        raise BrainError("too many notes on %s!%s for GraphQL (more than %d pages): not read" % (project, iid,
                                                                                               max_pages))


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


def _gid_number(gid):
    """The number of a GitLab global id: gid://gitlab/Note/123 (also DiscussionNote, DiffNote) -> 123."""
    m = re.match(r"^gid://gitlab/[A-Za-z:]+/([1-9][0-9]*)$", str(gid or ""))
    return int(m.group(1)) if m else None


def jira_user_id(user) -> str:
    """Stable id of a Jira user object: accountId (Cloud) or name/key (DC)."""
    if not user:
        return ""
    return str(user.get("accountId") or user.get("name") or user.get("key") or "")
