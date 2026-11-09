"""`brain trace KEY`: one JSON record of what happened to one requirement (plan §F: the Run Trace).

Read-only and deterministic: it reads what is already recorded and writes nothing. Every value says where it
came from (`src`, and the top-level `src` map for the plain fields); what no source holds is null, never guessed.
`missing` lists the sources that were not available, so a null is never mistaken for "did not happen".

Sources
  run.json          runs/<KEY>/run.json (working tree, or --ref)
  Lean audit        aidlc-docs/audit/*.jsonl (hash-chained, one file per clone): stages, approvals, rejections,
                    refused approvals, BLOCKED tool calls, verify results, sessions and turns
  Lean records      aidlc-docs/<intent>/decisions.md (decision table and brain-proposal blocks),
                    inception/requirements/requirements.md (assumptions A-n), build-and-test-summary.md
  park.json         runs/<KEY>/park.json in every commit of the run branch: the questions the runner parked on
  git               the run branch's commits (bot = kit.json bot_emails), the hand-over commit, the merge into the
                    default branch, reverts / hotfix commits / defect claims within 14 days after the merge
  bridge ledger     sqlite, optional (--ledger): runs, handoffs, decisions
  vouchers          brain-intake vouchers/<RUN-ID>/V-*.json, optional: hand-over and answer vouchers
  Brain usage       brainkit.usage: entries loaded at brain_commit, cited ids, stale and unknown citations
  credits           passed in by the KiroCrew app, optional. Kiro reports no token counts (input/output/cache = 0)

Git (docs/eval.md §2 has every rule, each with a test in tests/test_qa_r2_trace.py or tests/test_qa_r3_trace.py):
how the run was merged (landing, rules M1-M6: merge commit, squash + merge commit, a merge made on another branch
(M1b), fast-forward, rebase, squash, branch gone), which commits are the run's own (the range, rules C1-C3, and on the
default branch run_stretch, rules R1-R5), which `brain handoff` handed it over (handover, rules H1-H4: the one the
bridge accepted), and which time is used (a commit's "at" is its author date; a merge's is the committer date of the
commit that landed it). Branch copies (local, origin) that are older, ahead or diverged are named in
src.git.tip_note. The run's own commits are never counted as hotfixes; after the hand-over, a revert or a person's
commit naming the key with a fix word is never the run's own; a hotfix changes something outside brain/, runs/ and
aidlc-docs/ (A1, A2).

People: only GitLab usernames and pseudonyms already in the sources. Git and Lean identities (emails, names, hosts)
become a pseudonym G-<6 hex> of the email; Lean's audit file names (<host>-<6 hex>.jsonl) become L-<6 hex>; emails
and home directories in free text (blocked commands, stage notes, failed check commands) are replaced. Human message
text is never copied, except what decisions.md holds.
Answers an eval oracle gave (golden tasks, `eval_oracle: true`) are kept apart and never count as a person's.
"""
from __future__ import annotations

import functools
import glob
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import urllib.parse
from datetime import datetime, timedelta, timezone

from . import lean, reflect, sign, usage
from .bridge.ledger import RESERVED
from .common import BrainError, JIRA_RE, USER_RE, git, iso, utcnow

SCHEMA = 1
WINDOW_DAYS = 14
STAGE_STATE = {"STAGE_STARTED": "started", "STAGE_COMPLETED": "done", "STAGE_SKIPPED": "skipped"}
LEAN_APPROVALS = ("IDEATION", "REQUIREMENTS", "DESIGN", "INCEPTION", "PLAN", "RELEASE")
ACCEPT_WHAT = ("RELEASE", "acceptance")
VOUCHER_RE = re.compile(r"(?<![0-9A-Za-z])V-[0-9a-f]{10}(?![0-9a-f])")
PSEUD_RE = re.compile(r"(?<![0-9A-Za-z])P-[0-9A-F]{4}(?![0-9A-Za-z])")
DID_RE = re.compile(r"(?<![0-9A-Za-z-])D-[1-9][0-9]*(?![0-9])")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
ASSUMPTION_RE = re.compile(r"^\s*[-*]?\s*(A-[1-9][0-9]*)\s*[:：.)]\s*(\S.*)$")
FIX_RE = re.compile(r"(?i)(hot-?fix|\bfix(?:es|ed)?\b|\bbug|defect|regression|\brevert|roll ?back|修复|热修|回滚|缺陷|故障)")
REVERTS_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
# decisions.md Who: the runner's vocabulary (remote/prompts/brain-run.md §5), plus an optional note in brackets
_ENTRY = r"[PTIJ]-(?:[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}-[1-9][0-9]{0,3}|[0-9]{1,5})"
_NOTE = r"(?:\s*[(（][^()（）]*[)）])?"
WHO_WORD_RE = re.compile(r"^(?i:(agent|check))" + _NOTE + r"$")
WHO_BRAIN_RE = re.compile(r"^(?i:brain)\s+(%s(?:\s*[,，、;；]\s*%s)*)" % (_ENTRY, _ENTRY) + _NOTE + r"$")
WHO_PERSON_RE = re.compile(r"^(P-[0-9A-F]{4})\s+V-[0-9a-f]{10}" + _NOTE + r"$")
WHO_OWNER_RE = re.compile(r"^(?i:owner)" + _NOTE + r"$")   # local half: the run owner answered in chat
# claim sources that report a defect (no source: an old plain claim = product); gap / eval-gap / debt / schedule
# intents say what is missing, not that something broke
DEFECT_SOURCES = (None, "product", "alert")
# identities in free text: emails (and user@host), home directories
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*")
HOME_RE = re.compile(r"(?:/Users|/home)/[^/\s'\"`;|&]+|[A-Za-z]:\\Users\\[^\\\s'\"`;|&]+")
NO_TOKENS = "Kiro 经 KiroCrew 只给 credits，input/output/cache token 数都是 0"


# ---------- small helpers ----------

def _sha(b) -> str:
    return hashlib.sha256(b).hexdigest()


def pseudonym(email):
    """G-<6 hex> for a git / Lean identity: the email itself never leaves this module."""
    e = str(email or "").strip().lower()
    return "G-" + _sha(e.encode())[:6].upper() if e else None


def scrub(text, limit=None):
    """Free text from Lean (a blocked command, a stage note): emails become their pseudonym, home directories ~.
    Scrubbed first, cut to `limit` after, so a cut never leaves half an email."""
    if text is None:
        return None
    s = HOME_RE.sub("~", EMAIL_RE.sub(lambda m: pseudonym(m.group(0)), str(text)))
    return s[:limit] if limit else s


def audit_labels(paths) -> dict:
    """{audit file path: label}. Lean names its file <host>-<6 hex>.jsonl (the clone id): the label is
    aidlc-docs/audit/L-<6 hex of sha256(path)>.jsonl, the same for the same file on every machine."""
    out, used = {}, set()
    for p in sorted(paths):
        h = _sha(p.encode()).upper()
        n = 6
        while "L-" + h[:n] in used:
            n += 2
        used.add("L-" + h[:n])
        out[p] = "%s/audit/L-%s.jsonl" % (lean.DOCS, h[:n])
    return out


def _utc(s):
    """Any ISO time (Lean ms + Z, git +08:00) -> YYYY-MM-DDTHH:MM:SSZ; a bare date stays a date; junk -> None."""
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    if DATE_RE.match(s):
        return s
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return iso(dt)


def _dt(s):
    s = _utc(s)
    if not s:
        return None
    if DATE_RE.match(s):
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _g(repo, *args):
    p = git(repo, *args, check=False)
    return p.stdout if p.returncode == 0 else None


def _commit(repo, rev):
    if not rev or not isinstance(rev, str):
        return None
    out = _g(repo, "rev-parse", "-q", "--verify", rev + "^{commit}")
    return out.strip() if out and out.strip() else None


def _ancestor(repo, a, b) -> bool:
    return git(repo, "merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def _show(repo, rev, path):
    s = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (rev, path)], capture_output=True)
    return s.stdout.decode("utf-8", "replace") if s.returncode == 0 else None


def _short_ref(ref) -> str:
    for p in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if ref.startswith(p):
            return ref[len(p):]
    return ref


class Tree:
    """Files from the working tree (ref None) or a git ref."""

    def __init__(self, repo, ref):
        self.repo, self.ref = repo, ref
        if ref and not _commit(repo, ref):
            raise BrainError("%s is not a commit here (fetch it first)" % ref)

    @property
    def label(self) -> str:
        return self.ref or "working tree"

    def read(self, path):
        if self.ref is None:
            p = os.path.join(self.repo, path)
            if not os.path.isfile(p):
                return None
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read()
        return _show(self.repo, self.ref, path)

    def files(self, prefix) -> list:
        if self.ref is None:
            out = []
            for d, dirs, names in os.walk(os.path.join(self.repo, prefix)):
                dirs.sort()
                out += [os.path.relpath(os.path.join(d, n), self.repo).replace(os.sep, "/") for n in names]
            return sorted(out)
        p = git(self.repo, "ls-tree", "-r", "-z", "--name-only", self.ref, "--", prefix, check=False)
        return sorted(x for x in p.stdout.split("\0") if x) if p.returncode == 0 else []


# ---------- Lean ----------

def lean_events(tree, intent):
    """(events of this intent oldest first, {file label: chain status}). Lean's own rule: each record's hash covers
    the record without the hash; prev links to the record before; seq counts up from 1. Files are named by their
    label (audit_labels): Lean's file name carries the clone's host name."""
    events, chains = [], {}
    paths = [p for p in tree.files(lean.DOCS + "/audit") if p.endswith(".jsonl")]
    labels = audit_labels(paths)
    for path in paths:
        label = labels[path]
        prev, seq, bad, n = "GENESIS", 0, None, 0
        for i, line in enumerate((tree.read(path) or "").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                rec = None
            if not isinstance(rec, dict):
                bad = bad or i
                continue
            if bad is None:
                body = dict(rec)
                h = body.pop("hash", None)
                if body.get("prev") != prev or body.get("seq") != seq + 1 or \
                        _sha(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()) != h:
                    bad = i
                else:
                    prev, seq = h, body["seq"]
            n += 1
            if rec.get("intent") == intent:
                rec["_src"] = "%s#%s" % (label, rec.get("seq", "line %d" % i))
                rec["_file"] = path
                rec["_at"] = _utc(rec.get("ts"))
                events.append(rec)
        chains[label] = {"records": n, "chain": "ok" if bad is None else "broken at line %d" % bad}
    events.sort(key=lambda e: (e["_at"] or "", e["_file"], e.get("seq") if isinstance(e.get("seq"), int) else 0))
    return events, chains


def _data(e) -> dict:
    d = e.get("data")
    return d if isinstance(d, dict) else {}


def stages_of(events) -> list:
    rows = {}
    for e in events:
        if e.get("event") not in STAGE_STATE:
            continue
        d = _data(e)
        sid = str(d.get("stage") or "?") + ("/" + str(d["unit"]) if d.get("unit") else "")
        row = rows.setdefault(sid, {"id": sid, "first_at": e["_at"], "times": {}})
        st = STAGE_STATE[e["event"]]
        row["times"][st] = row["times"].get(st, 0) + 1
        note = d.get("note") if e["event"] == "STAGE_COMPLETED" else d.get("reason") if e["event"] == "STAGE_SKIPPED" \
            else None
        row.update(state=st, at=e["_at"], note=scrub(note) if isinstance(note, str) else None, src=e["_src"])
    return sorted(rows.values(), key=lambda r: (r["first_at"] or "", r["id"]))


def checks_of(events, tree, intent) -> dict:
    rounds = []
    for e in events:
        if e.get("event") != "VERIFY":
            continue
        d = _data(e)
        res = [r for r in d.get("results") or [] if isinstance(r, dict)] if isinstance(d.get("results"), list) else []
        failed = [scrub(r.get("cmd", ""), 120) for r in res if r.get("exit") not in (0, None)]
        drift = d.get("protected_drift")
        rounds.append({"at": e["_at"], "ok": bool(d.get("ok")), "detail": {
            "checks": len(res), "failed": failed, "fingerprint": str(d.get("fingerprint") or "")[:8] or None,
            "protected_drift": len(drift) if isinstance(drift, list) else 0}, "src": e["_src"]})
    path = "%s/%s/construction/build-and-test/build-and-test-summary.md" % (lean.DOCS, intent)
    text = tree.read(path)
    summary = None
    if text is not None:
        m = re.search(r"^- result: (PASS|FAIL)\s*$", text, re.M)
        t = re.search(r"^- time: (\S+)\s*$", text, re.M)
        summary = {"result": m.group(1) if m else None, "at": _utc(t.group(1)) if t else None,
                   "src": "%s (%s)" % (path, tree.label)}
    return {"verify": rounds, "verify_failed": sum(1 for r in rounds if not r["ok"]), "summary": summary,
            "ci": None, "ci_why": "GitLab pipelines are not readable here",
            "src": "Lean audit VERIFY events; build-and-test-summary.md"}


# ---------- git ----------
# The rules here are written down, one by one, in docs/eval.md §2 ("怎么认合并", "这个 run 自己的提交", "用哪个时间");
# tests/test_qa_r2_trace.py has a test for each of them.

GITLAB_MERGE = "Merge branch '%s' into '%s'"                       # GitLab's merge commit subject (M1)
MERGE_INTO_RE = re.compile(r"^Merge\b.* into '?(?P<into>[^'\s]+)'?\s*$")   # git's and GitLab's "... into <branch>"
REVERT_RE = re.compile(r'^Revert "|This reverts commit [0-9a-f]{7,40}', re.M)
LOG_FORMAT = "--format=%x01%H%x00%P%x00%ae%x00%ce%x00%aI%x00%cI%x00%B%x02"


def _raw(repo, *args, data=None):
    """git's stdout as text (bytes that are not UTF-8 replaced), or None when git fails."""
    p = subprocess.run(["git", "-C", repo] + list(args), input=data, capture_output=True)
    return p.stdout.decode("utf-8", "replace") if p.returncode == 0 else None


_META = {}   # (repo, sha) -> (parents, committer date): a commit's never change, so every read is kept


def _remember(repo, sha, parents, committed):
    if len(_META) > 200000:
        _META.clear()
    _META[(repo, sha)] = (tuple(parents), committed)


def _log(repo, *args) -> list:
    """The commits `git log args` lists: {sha, parents, ae, ce, authored, committed, subject, msg, files}.
    -z and --no-renames: file names come as they are (never quoted), a rename lists the old and the new name.
    A merge commit lists no files (git log shows no diff for merges)."""
    rows = []
    for chunk in (_raw(repo, "log", "-z", "--no-renames", "--name-only", LOG_FORMAT, *args) or "").split("\x01")[1:]:
        head, _, files = chunk.partition("\x02")
        f = head.split("\0", 6)
        if len(f) < 7:
            continue
        rows.append({"sha": f[0], "parents": f[1].split(), "ae": f[2].lower(), "ce": f[3].lower(),
                     "authored": _utc(f[4]), "committed": _utc(f[5]), "msg": f[6],
                     "subject": f[6].split("\n", 1)[0].strip(),
                     "files": [x for x in files.lstrip("\0\n").split("\0") if x]})
        _remember(repo, f[0], rows[-1]["parents"], rows[-1]["committed"])
    return rows


def _blobs(repo, specs) -> list:
    """The text of each "<rev>:<path>" in `specs` (None when it is not there), read in one `git cat-file --batch`."""
    if not specs:
        return []
    p = subprocess.run(["git", "-C", repo, "cat-file", "--batch"], input="".join(x + "\n" for x in specs).encode(),
                       capture_output=True)
    out, buf, i = [], p.stdout if p.returncode == 0 else b"", 0
    for _ in specs:
        j = buf.find(b"\n", i)
        if j < 0:
            out.append(None)
            continue
        head = buf[i:j].split()
        if len(head) == 3 and head[2].isdigit():
            n = int(head[2])
            out.append(buf[j + 1:j + 1 + n].decode("utf-8", "replace") if head[1] == b"blob" else None)
            i = j + 1 + n + 1
        else:
            out.append(None)
            i = j + 1
    return out


def _has(repo, rev, path) -> bool:
    return git(repo, "cat-file", "-e", "%s:%s" % (rev, path), check=False).returncode == 0


def _handoff_in(text):
    """The hand-over request in a run.json text (a non-empty object), or None."""
    try:
        r = json.loads(text or "null")
    except ValueError:
        return None
    h = r.get("handoff") if isinstance(r, dict) else None
    return h if isinstance(h, dict) and h else None


def _asks(texts) -> list:
    """Indexes of the run.json versions (oldest first) that ask for a hand-over: the hand-over is set and differs
    from the version before (each `brain handoff` writes a new one)."""
    out, prev = [], None
    for i, text in enumerate(texts):
        h = _handoff_in(text)
        if h and h != prev:
            out.append(i)
        prev = h
    return out


def _subject(repo, sha) -> str:
    return (_g(repo, "log", "-1", "--format=%s", sha) or "").strip()


def _outside(cfg, files) -> list:
    """The files that are not the Brain, a run's records or Lean's records (brain/, runs/, aidlc-docs/)."""
    mine = tuple(d.rstrip("/") + "/" for d in (cfg["brain_dir"], cfg["runs_dir"], lean.DOCS))
    return [f for f in files if not f.startswith(mine)]


def _changed(repo, sha) -> list:
    """The files a commit changes against its first parent (a merge too: what it brought into the branch)."""
    ps = _parents(repo, sha)
    args = [ps[0], sha] if ps else ["--root", sha]
    out = _raw(repo, "diff-tree", "-r", "-z", "--no-commit-id", "--name-only", "--no-renames", *args) or ""
    return [x for x in out.split("\0") if x]


def _refs(repo) -> dict:
    """{ref name: sha} of the local branches and origin's, in one `git for-each-ref`."""
    out = _g(repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/", "refs/remotes/origin/") or ""
    return {r: h for h, _, r in (line.partition(" ") for line in out.splitlines()) if r}


def default_ref(repo, cfg, ref, refs=None):
    d = cfg["default_branch"]
    refs = _refs(repo) if refs is None else refs
    if ref and _short_ref(ref) == d and _commit(repo, ref):   # name it one way, however it was given
        full = ("refs/remotes/origin/" if ref.startswith(("origin/", "refs/remotes/origin/")) else "refs/heads/") + d
        return full if refs.get(full) == _commit(repo, ref) else ref
    for r in ("refs/remotes/origin/" + d, "refs/heads/" + d):
        if refs.get(r):
            return r
    return None


@functools.lru_cache(maxsize=16)
def _merges(repo, dref_sha) -> tuple:
    """((sha, parents, subject), ...) of the merge commits on dref_sha's first-parent line, newest first (a commit's
    history never changes, so one read per default-branch head serves every trace of an eval)."""
    out = []
    for line in (_g(repo, "log", "--merges", "--first-parent", "--format=%H%x00%P%x00%s%x00%cI", dref_sha)
                 or "").splitlines():
        parts = line.split("\0")
        if len(parts) == 4:
            out.append((parts[0], tuple(parts[1].split()), parts[2].strip()))
            _remember(repo, parts[0], parts[1].split(), _utc(parts[3]))
    return tuple(out)


def _parents(repo, sha) -> list:
    if (repo, sha) in _META:
        return list(_META[(repo, sha)][0])
    out = (_g(repo, "log", "-1", "--format=%P%x00%cI", sha) or "").strip().split("\0")
    if len(out) != 2:
        return []
    _remember(repo, sha, out[0].split(), _utc(out[1]))
    return out[0].split()


def first_added(repo, dref, path):
    """S: the first commit on the default branch's first-parent line that brought `path` (oldest), or None."""
    if not dref:
        return None
    out = (_g(repo, "log", "--first-parent", "--diff-filter=A", "--format=%H%x00%P%x00%cI", dref, "--", path)
           or "").splitlines()
    if not out or len(out[-1].split("\0")) != 3:
        return None
    h, ps, cd = out[-1].split("\0")
    _remember(repo, h, ps.split(), _utc(cd))
    return h


def copies_of(repo, branch, ref, refs=None) -> list:
    """[(ref name, sha)] of the run branch here: --ref when it names the branch; else origin's, then the local one."""
    if ref and branch and _short_ref(ref) == branch and _commit(repo, ref):
        return [("ref %s" % ref, _commit(repo, ref))]
    refs = _refs(repo) if refs is None else refs
    return [(r, refs[r]) for r in (("refs/remotes/origin/" + branch, "refs/heads/" + branch) if branch else ())
            if refs.get(r)]


def freshest(repo, copies):
    """(sha, ref, note) of the copy to read: the one that contains the other; diverged: origin's (listed first)."""
    if not copies:
        return None, None, None
    (r0, c0), rest = copies[0], copies[1:]
    if not rest or rest[0][1] == c0:
        return c0, r0, None
    (r1, c1) = rest[0]
    if _ancestor(repo, c1, c0):
        return c0, r0, "%s (%s) is older: %s contains it" % (r1, c1[:12], r0)
    if _ancestor(repo, c0, c1):
        return c1, r1, "%s is ahead of %s (%s): commits not pushed yet" % (r1, r0, c0[:12])
    return c0, r0, "%s (%s) and %s (%s) have diverged: using %s" % (r1, c1[:12], r0, c0[:12], r0)


def _patch_ids(repo, *revs) -> set:
    """Patch-ids (git patch-id --stable) of the non-merge commits `git log revs` lists."""
    p = subprocess.run(["git", "-C", repo, "log", "-p", "--no-merges", "--format=commit %H", *revs],
                       capture_output=True)
    return _pids(repo, p.stdout) if p.returncode == 0 else set()


def _pids(repo, patch) -> set:
    p = subprocess.run(["git", "-C", repo, "patch-id", "--stable"], input=patch, capture_output=True)
    return {line.split()[0] for line in p.stdout.decode("utf-8", "replace").splitlines() if line.strip()}


def _pid_of(repo, sha_list) -> dict:
    """{sha: patch-id} for these commits."""
    if not sha_list:
        return {}
    p = subprocess.run(["git", "-C", repo, "show", "-p", "--format=commit %H", *sha_list], capture_output=True)
    q = subprocess.run(["git", "-C", repo, "patch-id", "--stable"], input=p.stdout, capture_output=True)
    out = {}
    for line in q.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1]] = parts[0]
    return out


def run_stretch(repo, cfg, dref, start, key, intent, branch, floor=None, pids=None, n=0):
    """(commits, copies): the run's own commits on the default branch's first-parent line, oldest first, from S =
    `start` (docs/eval.md §2, rules R1-R5). Walking from S, each commit:
      R1  S, and every commit up to `floor` (the branch's last commit there) or with the patch-id of one of the
          branch's commits (`pids`: a rebased copy) is the run's;
      R2  a merge commit is the run's when it was made on the run branch (subject "Merge ... into <branch>": main or
          origin merged into the branch) or by the bot (kit.json bot_emails); any other merge ends the stretch;
      R3  after the hand-over (the last commit whose run.json asks for one: `brain handoff`), a person's (not the
          bot's) revert ("This reverts commit", 'Revert "') or commit whose message names KEY with a fix word
          (FIX_RE) ends the stretch: never the run's (the bot works only on its run: its reverts and fixes are the
          run's, R4). Before it they are the owner's local half (R5). Anyone's commit that touches another run's
          runs/<KEY2>/ ends the stretch wherever it is;
      R4  a commit that touches runs/<KEY>/ or aidlc-docs/<intent>/, or that the bot made, is the run's;
      R5  any other commit (a person's push that names nothing) is the run's only when a later commit is (R1, R2, R4).
    The stretch is S up to its last own commit."""
    parent = (_parents(repo, start) or [None])[0]
    rng = ("%s..%s" % (parent, dref)) if parent else dref
    shas = (_g(repo, "rev-list", "--first-parent", "--reverse", rng) or "").split()
    shas = shas[shas.index(start):] if start in shas else []
    if not shas:
        return [], set()
    rp = "%s/%s/run.json" % (cfg["runs_dir"], key)
    asked = (_g(repo, "log", "--first-parent", "--reverse", "--format=%H", rng, "--", rp) or "").split()
    pos = {h: i for i, h in enumerate(shas)}
    handed = max([pos.get(asked[i], -1) for i in _asks(_blobs(repo, ["%s:%s" % (h, rp) for h in asked]))] or [-1])
    pid = _pid_of(repo, shas[:n + 5]) if pids else {}
    if floor and floor not in shas:   # the branch's commit came in through a second parent
        floor = merge_of(repo, floor, dref)
    floor_i = shas.index(floor) if floor in shas else 0
    bots = {x.lower() for x in cfg.get("bot_emails", [])}
    runs = cfg["runs_dir"] + "/"
    mine = ("%s%s/" % (runs, key),) + (("%s/%s/" % (lean.DOCS, intent),) if intent else ())
    key_re = re.compile(r"(?<![A-Za-z0-9-])%s(?![0-9])" % re.escape(key))
    copies, last, i = set(), 0, 0
    for k in range(0, len(shas), 64):
        for c in _log(repo, "--no-walk=unsorted", *shas[k:k + 64]):
            bot = c["ae"] in bots or c["ce"] in bots
            if pid.get(c["sha"]) in (pids or ()):
                copies.add(c["sha"])
            if i <= floor_i or c["sha"] in copies:                                    # R1
                own = True
            elif len(c["parents"]) > 1:                                              # R2
                m = MERGE_INTO_RE.match(c["subject"])
                if not (bot or (m and branch and m.group("into") == branch)):
                    return shas[:last + 1], copies
                own = True
            elif (i > handed and not bot and (REVERT_RE.search(c["msg"]) or (key_re.search(c["msg"]) and
                                                                            FIX_RE.search(c["msg"])))) \
                    or any(f.startswith(runs) and JIRA_RE.match(f[len(runs):].split("/", 1)[0])
                           and f[len(runs):].split("/", 1)[0] != key for f in c["files"]):   # R3
                return shas[:last + 1], copies
            else:                                                                    # R4 / R5
                own = bot or any(f.startswith(mine) for f in c["files"])
            if own:
                last = i
            i += 1
    return shas[:last + 1], copies


def merge_of(repo, tip, dref):
    """The first commit on the default branch's first-parent line that contains the tip (binary search: once a
    first-parent commit contains it, every later one does)."""
    if not tip or not dref or not _ancestor(repo, tip, dref):
        return None
    line = list(reversed((_g(repo, "rev-list", "--first-parent", dref) or "").split()))  # oldest first
    lo, hi = 0, len(line) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if _ancestor(repo, tip, line[mid]):
            hi = mid
        else:
            lo = mid + 1
    return line[lo] if line and _ancestor(repo, tip, line[lo]) else None


def _cdate(repo, sha):
    """The committer date (when it landed on the branch it is on: merged, squashed, rebased)."""
    if (repo, sha) not in _META:
        _parents(repo, sha)
    return _META[(repo, sha)][1] if (repo, sha) in _META else None


def _same_changes(repo, base, tip, squash) -> str:
    d = subprocess.run(["git", "-C", repo, "diff", base, tip], capture_output=True)
    same = d.returncode == 0 and _pids(repo, d.stdout) == set(_pid_of(repo, [squash]).values())
    return "the squash commit carries exactly the branch's changes" if same else \
        "the squash commit differs from the branch's changes (conflicts resolved, or the branch moved on)"


def landing(repo, cfg, dref, key, intent, branch, copies, refs=None, _depth=0):
    """How the run reached the default branch, without asking GitLab (docs/eval.md §2, rules M1-M6), or None.
    {"merge": {commit, at, how, src, own?, note?, via?}, "tip": the run's last commit as merged, "tip_src",
     "notes": [what was not used], "exclude": [commits whose history is not the run's]}.
    S = the first commit on the default branch's first-parent line that brought runs/<KEY>/run.json."""
    rp = "%s/%s/run.json" % (cfg["runs_dir"], key)
    dsha = ((refs or {}).get(dref) or _commit(repo, dref)) if dref else None
    s = first_added(repo, dsha, rp) if dsha else None
    if not s:
        return None
    tip, tref, tnote = freshest(repo, copies)
    # M1: a merge commit: GitLab's subject, or S itself when its second parent has run.json
    mc = next(((h, ps, "GitLab's merge commit %s" % h[:12]) for h, ps, subject in _merges(repo, dsha)
               if branch and len(ps) >= 2 and subject == GITLAB_MERGE % (branch, cfg["default_branch"])), None)
    sp = _parents(repo, s)
    if not mc and len(sp) >= 2 and _has(repo, sp[1], rp):
        subject = _subject(repo, s)
        into = MERGE_INTO_RE.match(subject)
        if into and into.group("into") not in (cfg["default_branch"], branch):
            # M1b: S was made on another branch ("Merge branch 'main' into '<other run>'": that run brought main in)
            # and then fast-forwarded into the default branch: not this run's merge. The run came in through S's
            # second parent: its merge is looked for on that commit's first-parent line
            inner = landing(repo, cfg, sp[1], key, intent, branch, copies, refs, _depth + 1) if _depth < 4 else None
            if inner:
                inner["merge"]["via"] = "%s's first-parent line runs through %s, \"%s\" (made on %s, then " \
                                        "fast-forwarded): the run came in through its second parent %s" % (
                                            dref if not _depth else dref[:12], s[:12], subject[:120],
                                            into.group("into"), sp[1][:12])
                return inner
        else:
            mc = (s, sp, "the merge commit %s that brought %s" % (s[:12], rp))
    if mc:
        h, (m1, p2), found = mc[0], mc[1][:2], mc[2]
        mp = _parents(repo, p2) if tip else []
        merge = {"commit": h, "at": _cdate(repo, h), "how": "merge commit",
                 "src": "%s on the first-parent line of %s" % (found, dref)}
        # M1a: GitLab's "squash commits" with a merge commit: the second parent is a squash commit on the merge base
        if tip and len(mp) == 1 and _ancestor(repo, mp[0], m1) and \
                not any(_ancestor(repo, c, p2) or _ancestor(repo, p2, c) for _, c in copies):
            merge.update(how="squash and merge commit (the branch is still here)", own=[p2],
                         note=_same_changes(repo, mp[0], tip, p2))
            return {"merge": merge, "tip": tip, "tip_src": tref, "exclude": [m1],
                    "notes": [tnote, "second parent of merge commit %s (%s) is the squash commit: the branch's "
                                     "commits are used" % (h[:12], p2[:12])]}
        notes = []
        for r, c in copies:
            if c == p2:
                continue
            if _ancestor(repo, c, p2):
                notes.append("%s (%s) is older: the merged branch contains it" % (r, c[:12]))
            elif _ancestor(repo, p2, c):
                notes.append("%s (%s) has commits after the merge: not part of it" % (r, c[:12]))
            else:
                notes.append("%s (%s) has diverged from the merged branch: not used" % (r, c[:12]))
        return {"merge": merge, "tip": p2, "tip_src": "second parent of merge commit %s on %s" % (h[:12], dref),
                "exclude": [m1], "notes": notes}
    base = sp[0] if sp else None
    # M2: fast-forward: S is one of the branch's own commits
    on = [(r, c) for r, c in copies if _ancestor(repo, s, c)] if len(sp) <= 1 else []
    if on:
        t = freshest(repo, on)[0]
        floor = t if _ancestor(repo, t, dsha) else (_g(repo, "merge-base", t, dsha) or "").strip()
        seq, _ = run_stretch(repo, cfg, dref, s, key, intent, branch, floor=floor)
        last = seq[-1]
        notes = []
        for r, c in on:
            if c == last:
                continue
            notes.append("%s (%s) %s" % (r, c[:12], "is older: the run continues on %s" % dref
                                         if _ancestor(repo, c, last) else "has commits after the merge: not part of it"
                                         if _ancestor(repo, last, c) else "has diverged from the merged run: not used"))
        return {"merge": {"commit": last, "at": _cdate(repo, last), "how": "fast-forward", "own": seq,
                          "src": "the run's commits on the first-parent line of %s from %s (the commit that brought "
                                 "%s; git keeps no time of a fast-forward: the last run commit's)" % (dref, s[:12], rp)},
                "tip": last, "tip_src": "last run commit on the first-parent line of %s (fast-forward)" % dref,
                "exclude": [base] if base else [], "notes": notes}
    # M6: the branch's tip is on the default branch anyway (a merge commit with another subject)
    m = merge_of(repo, tip, dref) if tip else None
    if m:
        return {"merge": {"commit": m, "at": _cdate(repo, m), "how": "merge commit" if m != tip else "fast-forward",
                          "src": "first-parent line of %s" % dref},
                "tip": tip, "tip_src": tref, "notes": [tnote],
                "exclude": [_parents(repo, m)[0]] if m != tip else ([base] if base else [])}
    if tip:
        # M3 / M4: the branch is here, its commits are not on the default branch: rebase (copies) or squash
        mb = (_g(repo, "merge-base", tip, dsha) or "").strip()
        bp = _patch_ids(repo, tip, *(["^" + mb] if mb else []))
        n = len((_g(repo, "rev-list", "--no-merges", tip, *(["^" + mb] if mb else [])) or "").split())
        seq, matched = run_stretch(repo, cfg, dref, s, key, intent, branch, pids=bp, n=n)
        src = "%s on the first-parent line of %s from %s (the commit that brought %s)" % (
            "patch-ids of the branch's commits and the run's records" if matched else "the run's records", dref,
            s[:12], rp)
        at = _cdate(repo, seq[-1])
        if matched:   # M3: the run as merged = its copies on the default branch
            return {"merge": {"commit": seq[-1], "at": at, "how": "rebase merge (the branch is still here)",
                              "src": src, "own": seq, "start": s, "rebased": True,
                              "note": "%d of the commits on %s are copies of the branch's" % (len(matched), dref)},
                    "tip": seq[-1], "exclude": [base] if base else [],
                    "tip_src": "last run commit on the first-parent line of %s (rebase merge)" % dref,
                    "notes": ["%s (%s): its commits were rebased onto %s, the copies there are used" % (
                        tref, tip[:12], dref), tnote]}
        # M4: squash: the branch's commits made after the squash commit are not part of it
        made = [(x["sha"], x["authored"]) for x in _log(repo, "--first-parent", tip, *(["^" + mb] if mb else []))]
        cut = next((i for i, (_, a) in enumerate(made) if a and at and a <= at), None)
        notes = [tnote]
        if cut:
            notes.append("%s (%s): %d commit(s) made after the squash merge are not part of it" % (
                tref, tip[:12], cut))
        as_merged = made[cut][0] if cut else tip
        return {"merge": {"commit": seq[-1], "at": at, "how": "squash merge (the branch is still here)", "src": src,
                          "own": seq, "note": _same_changes(repo, mb, as_merged, s) if mb else None},
                "tip": as_merged, "tip_src": tref, "exclude": [dref], "notes": notes}
    # M5: no branch here: the run's commits on the default branch say how it was merged
    seq, _ = run_stretch(repo, cfg, dref, s, key, intent, branch)
    src = "the run's records on the first-parent line of %s from %s (the commit that brought %s)" % (dref, s[:12], rp)
    if len(seq) == 1:   # one commit carries the run: its own commits are not listed
        return {"merge": {"commit": s, "at": _cdate(repo, s), "how": "squash merge (the branch is gone): the commit "
                          "that brought run.json", "src": src, "own": seq}, "tip": None}
    return {"merge": {"commit": seq[-1], "at": _cdate(repo, seq[-1]), "how": "fast-forward or rebase merge (the "
                      "branch is gone): the last run commit", "src": src, "own": seq},
            "tip": seq[-1], "exclude": [base] if base else [], "notes": [],
            "tip_src": "last run commit on the first-parent line of %s (fast-forward or rebase merge, the branch is "
                       "gone)" % dref}


def foreign_parents(repo, merges, rp) -> list:
    """The other parents (not the first) of these merge commits [(sha, parents)] that have no runs/<KEY>/run.json:
    main merged into the branch (the runner may not force-push). Their history is main's, not the run's (rule C2)."""
    ps = sorted({p for _, parents in merges for p in parents[1:]})
    if not ps:
        return []
    out = _raw(repo, "cat-file", "--batch-check", data="".join("%s:%s\n" % (p, rp) for p in ps).encode()) or ""
    return [p for p, line in zip(ps, out.splitlines()) if line.endswith(" missing")]


def _ancestry(log, sha) -> set:
    """`sha` and its ancestors among the commits of `log` (the parents git log gave: no git call)."""
    parents = {c["sha"]: c["parents"] for c in log}
    seen, todo = set(), [sha]
    while todo:
        h = todo.pop()
        if h in parents and h not in seen:
            seen.add(h)
            todo += parents[h]
    return seen


def git_history(repo, cfg, rec, ref, key, refs=None):
    """Commits of the run branch, the hand-over commit, the merge, park.json history."""
    branch = rec.get("branch") if isinstance(rec.get("branch"), str) else ""
    bc = rec.get("brain_commit") if isinstance(rec.get("brain_commit"), str) else None
    intent = rec.get("intent") if isinstance(rec.get("intent"), str) else None
    rp = "%s/%s/run.json" % (cfg["runs_dir"], key)
    pp = "%s/%s/park.json" % (cfg["runs_dir"], key)
    refs = _refs(repo) if refs is None else refs
    dref = default_ref(repo, cfg, ref, refs)
    copies = copies_of(repo, branch, ref, refs)
    tip, tip_how, tip_note = freshest(repo, copies)
    out = {"tip": tip, "tip_src": tip_how, "tip_note": tip_note, "default_ref": dref, "merge": None, "commits": None,
           "range": None, "handover_commit": None, "park": None, "requests": [], "_log": [], "_rp": rp}
    land = landing(repo, cfg, dref, key, intent, branch, copies, refs)
    if land:
        out["merge"] = land["merge"]
        if not land["tip"]:
            return out
        tip = land["tip"]
        out.update(tip=tip, tip_src=land["tip_src"], tip_note="; ".join(x for x in land["notes"] if x) or None)
        exclude = land["exclude"]
    elif tip:
        exclude = [dref] if dref else ([bc] if _commit(repo, bc) else [])
    else:
        return out
    rng = [tip] + ["^" + x for x in exclude]
    log = _log(repo, "--topo-order", "--reverse", *rng)
    foreign = foreign_parents(repo, [(c["sha"], c["parents"]) for c in log if len(c["parents"]) > 1], rp)
    if foreign:   # C2: main merged into the branch
        rng += ["^" + x for x in foreign]
        log = _log(repo, "--topo-order", "--reverse", *rng)
    out["range"] = " ".join([tip[:12]] + ([x[:41] for x in rng[1:]] or ["^(root)"]))
    bots = {x.lower() for x in cfg.get("bot_emails", [])}
    commits = []
    for c in log:
        bot = c["ae"] in bots or c["ce"] in bots
        commits.append({"sha": c["sha"], "at": c["authored"], "committed_at": c["committed"],
                        "by": "bot" if bot else "person", "who": None if bot else pseudonym(c["ae"]),
                        "files": len(c["files"]),
                        "records_only": bool(c["files"]) and all(f.startswith((lean.DOCS + "/", cfg["runs_dir"] + "/"))
                                                                  for f in c["files"]),
                        "merge": len(c["parents"]) > 1, "after_handover": None, "src": "git log " + out["range"]})
    # the hand-over requests (each `brain handoff` commit: run.json asks for a new one) and park.json's versions, in
    # one read; which request handed the run over is decided with the bridge's records (handover(), rules H1-H4)
    rps = [c for c in log if rp in c["files"]]
    pps = [c for c in log if pp in c["files"]]
    texts = _blobs(repo, ["%s:%s" % (c["sha"], rp) for c in rps] + ["%s:%s" % (c["sha"], pp) for c in pps])
    for i in _asks(texts[:len(rps)]):
        c, h = rps[i], _handoff_in(texts[i])
        out["requests"].append({"commit": c["sha"], "at": _utc(h.get("requested_at")) or c["authored"],
                                "by": h["by"] if isinstance(h.get("by"), str) and USER_RE.match(h["by"]) else None,
                                "src": "%s@%s" % (rp, c["sha"][:12]), "_h": h})
    out["_log"], out["_exclude"] = log, rng[1:]
    out["commits"] = commits
    park, last = [], None
    for c, text in zip(pps, texts[len(rps):]):
        row = _park_row(text, "%s@%s" % (pp, c["sha"][:12]), c["authored"], c["sha"])
        if row and (row["kind"], row["lean_request"], row["status"], row["question_sha256"]) != last:
            park.append(row)
            last = (row["kind"], row["lean_request"], row["status"], row["question_sha256"])
    out["park"] = park
    return out


def _park_row(raw, src, at, commit=None):
    if raw is None:
        return None
    try:
        p = json.loads(raw)
    except ValueError:
        return {"at": at, "commit": commit, "kind": None, "ask": None, "lean_request": None, "status": "not JSON",
                "options": None, "refs": [], "question_sha256": None, "src": src}
    if not isinstance(p, dict):
        return None
    q = p.get("question")
    return {"at": at, "commit": commit, "kind": p.get("kind") if isinstance(p.get("kind"), str) else None,
            "ask": p.get("ask") if isinstance(p.get("ask"), str) else None,
            "lean_request": p.get("lean_request") if isinstance(p.get("lean_request"), str) else None,
            "status": p.get("status") if isinstance(p.get("status"), str) else None,
            "options": len(p["options"]) if isinstance(p.get("options"), list) else None,
            "refs": [str(x)[:40] for x in p.get("refs") or [] if isinstance(x, str)][:10] if isinstance(
                p.get("refs"), list) else [],
            "question_sha256": _sha(q.encode())[:12] if isinstance(q, str) else None, "src": src}


def handover(repo, hist, rec_handoff, led, vouchers) -> dict:
    """Which `brain handoff` handed the run over (docs/eval.md §2, rules H1-H4). A request is a commit of the run
    whose run.json asks for a new hand-over (hist["requests"], oldest first); `led` is None when no ledger was read.
      H1  the bridge accepted a hand-over: a hand-over voucher (its signature not bad), a ledger hand-over row
          'accepted', or a ledger run (not one being signed); the first of them. The hand-over is the last request
          at or before its commit: the commit itself or one of its ancestors; for a rebased copy, the request whose
          run.json hand-over is the same; else the last request made before the bridge accepted it.
      H2  nothing accepted, and the ledger has the bridge refusing a request: not handed over (the runner never had
          the run; what the owner did next is the local half).
      H3  nothing accepted, the ledger has no row for any request, and the bot made no commit after the first one:
          not handed over (`brain handoff` stopped before the label, exit 1; or the bridge has not checked it yet).
      H4  no record from the bridge: the last request before the bot's first commit after a request (an earlier one
          did not go through); no bot commit: the last request. Without the run's commits: what run.json says.
    -> {"value", "commit", "at", "rule", "how", "bridge_accepted", "requests"}."""
    reqs, log, commits = hist.get("requests") or [], hist.get("_log") or [], hist.get("commits")
    in_log = {c["sha"] for c in log}
    rec_at = _utc(rec_handoff.get("requested_at")) if rec_handoff else None
    acc = []
    for v in vouchers:
        if v.get("kind") == "handoff" and isinstance(v.get("sha"), str) and v.get("_sig") != "bad":
            acc.append((_utc(v.get("issued_at")) or "~", v["sha"], "hand-over voucher %s" % v.get("_src")))
    for h in (led or {}).get("handoffs", []):
        if h["state"] == "accepted":
            acc.append((_utc(h["updated"]) or "~", str(h["sha"]).split("#", 1)[0], "bridge ledger handoffs (accepted)"))
    for r in (led or {}).get("runs", []):
        if r["state"] != RESERVED:
            acc.append((_utc(r["created"]) or "~", str(r["sha"]), "bridge ledger runs (%s)" % r["run_id"]))
    out = {"requests": len(reqs), "bridge_accepted": True if acc else (False if led is not None else None)}
    if acc:
        at, sha, src = min(acc)
        cands = []
        if sha in in_log:
            before = _ancestry(log, sha)
            cands = [r for r in reqs if r["commit"] in before]
        elif reqs and _commit(repo, sha):
            h = _handoff_in(_show(repo, sha, hist["_rp"]))
            cands = [r for r in reqs if h and r["_h"] == h] or [r for r in reqs if _ancestor(repo, r["commit"], sha)]
        if not cands and at != "~":
            cands = [r for r in reqs if r["at"] and r["at"] <= at]
        r = cands[-1] if cands else None
        return dict(out, value=True, commit=r["commit"] if r else (sha if sha in in_log else None),
                    at=r["at"] if r else rec_at, rule="H1",
                    how="the bridge accepted the hand-over at %s (%s)" % (sha[:12], src))
    if led is not None and any(h["state"] == "rejected" for h in led.get("handoffs", [])):
        return dict(out, value=False, commit=None, at=None, rule="H2",
                    how="the bridge refused the hand-over and accepted none: the run stayed local")
    pos = {c["sha"]: i for i, c in enumerate(commits or [])}
    asked = [(pos[r["commit"]], r) for r in reqs if r["commit"] in pos]
    bot = next((i for i, c in enumerate(commits or []) if c["by"] == "bot" and asked and i > asked[0][0]), None)
    if led is not None and not led.get("handoffs") and asked and bot is None:
        return dict(out, value=False, commit=None, at=None, rule="H3",
                    how="the bridge ledger has no record of a hand-over and the runner made no commit after it")
    if asked:
        r = [x for i, x in asked if bot is None or i < bot][-1]
        return dict(out, value=True, commit=r["commit"], at=r["at"], rule="H4",
                    how="no hand-over record from the bridge: the last `brain handoff` commit %s"
                        % ("before the runner's first commit" if bot is not None else "(the runner made no commit)"))
    return dict(out, value=bool(rec_handoff), commit=None, at=rec_at, rule="H4" if rec_handoff else None,
                how="run.json's handoff (the run's commits are not here)" if rec_handoff else "run.json has no handoff")


def after_merge(repo, cfg, key, merge, commits, dref, as_of, refs=None):
    """Reverts, hotfix commits and defect claims within WINDOW_DAYS after the merge. Messages are read, not kept.
    "After the merge" = on the default branch and not in the merge's history (no `git log --since`: one commit with
    an older committer date would stop the walk); the window is then judged by each commit's committer date. The
    run's own commits (the branch's, and its copies on the default branch) are never a hotfix, and neither is a
    commit that changes only the Brain, run records or Lean records (brain/, runs/, aidlc-docs/: `brain reflect
    --apply` and its MR name the key, and a gap's title often holds a fix word); those are listed in `ignored`. A
    commit at the very second of the merge is after it when it is not in the merge's history. A claim counts as a
    defect only when it reports one (source product or alert, or an old claim without a source)."""
    if not merge or not merge.get("at"):
        return {"value": None, "window_closed": None, "window_end": None, "evidence": [], "ignored": [],
                "src": "no merge found", "rules": None}
    start = _dt(merge["at"])
    end = start + timedelta(days=WINDOW_DAYS)
    closed = as_of >= end
    until = min(end, as_of)            # judged "as of": later commits are not looked at
    own = {c["sha"] for c in commits or []} | {merge["commit"]} | set(merge.get("own") or [])
    key_re = re.compile(r"(?<![A-Za-z0-9-])%s(?![0-9])" % re.escape(key))
    ev, ignored = [], []
    log = _g(repo, "log", "--format=%x01%H%x00%cI%x00%B", dref, "^" + merge["commit"]) or ""
    for chunk in log.split("\x01")[1:]:
        h, cd, body = (chunk.split("\0", 2) + ["", ""])[:3]
        if h in own:
            continue
        at = _utc(cd)
        if not at or _dt(at) < start or _dt(at) > until:
            continue
        rv = [s for s in REVERTS_RE.findall(body) if any(o.startswith(s) for o in own)]
        rule = "reverts %s" % rv[0][:12] if rv else "names %s with a fix word" % key if (
            key_re.search(body) and FIX_RE.search(body)) else None
        if not rule:
            continue
        row = {"sha": h, "at": at, "rule": rule, "src": "git log %s" % dref}
        if _outside(cfg, _changed(repo, h)):
            ev.append(row)
        else:
            ignored.append(dict(row, why="changes only %s/, %s/ or %s/" % (cfg["brain_dir"], cfg["runs_dir"],
                                                                          lean.DOCS)))
    refs = _refs(repo) if refs is None else refs
    claims = sorted(r for r in refs if r.startswith(("refs/heads/claim/", "refs/remotes/origin/claim/")) and
                    r.rsplit("/", 1)[-1] != key)
    seen = set()
    for r, text in zip(claims, _blobs(repo, ["%s:claim.json" % refs[r] for r in claims])):
        k2 = r.rsplit("/", 1)[-1]
        if k2 in seen:
            continue
        try:
            doc = json.loads(text or "null")
        except ValueError:
            continue
        if not isinstance(doc, dict) or doc.get("source") not in DEFECT_SOURCES:
            continue
        at = _utc(doc.get("created_at"))
        title = str(doc.get("title") or "")
        if at and start <= _dt(at) <= until and key_re.search(title) and FIX_RE.search(title):
            seen.add(k2)
            ev.append({"claim": "claim/" + k2, "at": at, "rule": "defect claim names %s" % key, "src": r})
    return {"value": True if ev else (False if closed else None), "window_closed": closed, "window_end": iso(end),
            "evidence": sorted(ev, key=lambda x: (x["at"], x.get("sha", x.get("claim")))),
            "ignored": sorted(ignored, key=lambda x: (x["at"], x["sha"])),
            "rules": "revert of a run commit; a commit after the merge (not one of the run's own) naming %s with "
                     "hotfix/fix/bug/defect/revert/修复/热修/回滚/缺陷; either one only when it changes something "
                     "outside %s/, %s/ and %s/; a new claim/<KEY> reporting a defect (source product or alert, or "
                     "none) whose title names %s with such a word" % (key, cfg["brain_dir"], cfg["runs_dir"],
                                                                       lean.DOCS, key),
            "src": "git log %s ^%s, claim/* refs" % (dref, merge["commit"][:12])}


# ---------- decisions.md and requirements.md ----------

def _cells(line):
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def decision_rows(text):
    """[(line, {column: cell})] for table rows under a header that has ID and Decision columns."""
    rows, header, fence = [], None, False
    for n, line in enumerate(text.split("\n"), 1):
        if re.match(r"^\s*(```|~~~)", line):
            fence, header = not fence, None
            continue
        if fence:
            continue
        if not line.strip().startswith("|"):
            header = None
            continue
        cells = _cells(line)
        if header is None:
            low = [c.lower() for c in cells]
            if "id" in low and "decision" in low:
                header = low
            continue
        if all(re.match(r"^:?-{2,}:?$", c) for c in cells if c):
            continue
        rows.append((n, dict(zip(header, cells + [""] * (len(header) - len(cells))))))
    return rows


def classify_who(who, bot_users=()):
    """(by, who shown) from a decisions.md Who cell, in the runner's vocabulary (remote/prompts/brain-run.md §5):
      agent                     -> agent        (also a kit.json bot user: the runner's account)
      brain <entry id>[, <id>]  -> brain        (who shown: the ids)
      check                     -> check
      <P-xxxx> <V-xxxxxxxxxx>   -> person       (who shown: the pseudonym)
      owner                     -> person       (local half: the run owner answered in chat; kit/.kiro/steering)
    each optionally followed by a note in brackets ("agent (A-2)"). Markdown code marks are ignored anywhere in the
    cell (brain `T-PAY-1-1`: the steering writes entry ids as code). Empty -> (None, None). Anything else is
    ("unclassified", None): not guessed, and not shown (it may be a real name); eval lists where it is."""
    w = " ".join(str(who or "").replace("`", " ").split())
    if not w:
        return None, None
    if w.lower() in {b.lower() for b in bot_users}:
        return "agent", w.lower()
    m = WHO_WORD_RE.match(w)
    if m:
        return m.group(1).lower(), m.group(1).lower()
    m = WHO_BRAIN_RE.match(w)
    if m:
        return "brain", ", ".join(re.findall(_ENTRY, m.group(1)))
    m = WHO_PERSON_RE.match(w)
    if m:
        return "person", m.group(1)
    if WHO_OWNER_RE.match(w):
        return "person", "owner"
    return "unclassified", None


def md_decisions(text, path, bot_users):
    out = []
    for n, row in decision_rows(text or ""):
        did, dec = row.get("id", ""), row.get("decision", "")
        if not did or not dec:
            continue
        by, who = classify_who(row.get("who"), bot_users)
        whole = " ".join(v for k, v in row.items() if k != "id")
        date = row.get("date", "")
        out.append({"id": did, "source": "decisions.md", "at": date if DATE_RE.match(date) else None,
                    "stage": row.get("stage") or None, "by": by, "who": who,
                    "text": dec, "why": row.get("why") or None,
                    "cites": sorted(set(usage.CITE_RE.findall(whole))),
                    "refs": {"vouchers": sorted(set(VOUCHER_RE.findall(whole))),
                             "decisions": sorted(set(DID_RE.findall(whole)) - {did})},
                    "bridge_decision": None, "src": "%s:%d" % (path, n)})
    return out


def assumptions(text, path, at, at_src):
    out, on = [], False
    for n, line in enumerate((text or "").split("\n"), 1):
        if line.startswith("## "):
            on = line[3:].strip().lower().startswith("assumption")
            continue
        m = ASSUMPTION_RE.match(line) if on else None
        if m and not m.group(2).strip().startswith("<!--"):
            out.append({"id": m.group(1), "source": "requirements.md", "at": at, "stage": "requirements",
                        "by": "agent", "who": "agent", "text": m.group(2).strip(), "why": None,
                        "cites": sorted(set(usage.CITE_RE.findall(m.group(2)))), "refs": {"vouchers": [],
                                                                                         "decisions": []},
                        "bridge_decision": None, "src": "%s:%d (date: %s)" % (path, n, at_src)})
    return out


def proposals_of(text, path) -> dict:
    props, probs = reflect.parse(text or "")
    by_rel = {}
    for p in props:
        by_rel[p.rel] = by_rel.get(p.rel, 0) + 1
    corr = [p for p in props if p.fields.get("type") == "correction" or p.rel == "repeats"]
    return {"total": len(props), "by_rel": by_rel, "corrections": sum(1 for p in props
                                                                     if p.fields.get("type") == "correction"),
            "repeats": sorted(p.target for p in props if p.rel == "repeats" and p.target),
            "correction_src": sorted({s for p in corr for s in p.src}), "problems": len(probs),
            "src": "%s (brain-proposal blocks)" % path}


# ---------- bridge ledger and vouchers ----------

def ledger_rows(path, key, shas=()):
    """The bridge ledger's rows for this requirement: its runs, the hand-over checks of their MRs and of the commits
    in `shas` (the run's `brain handoff` commits: a refused hand-over has no run), and the runs' decisions."""
    if not os.path.exists(path):
        raise BrainError("bridge ledger %s not found" % path)
    uri = "file:%s?mode=ro" % urllib.parse.quote(os.path.abspath(path))
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        runs = [dict(r) for r in db.execute(
            "SELECT run_id, project, iid, sha, voucher_id, state, created, updated FROM runs WHERE req=? "
            "ORDER BY created, run_id", (key,))] if "runs" in tables else []
        handoffs = []
        if "handoffs" in tables:
            cols = "SELECT project, iid, sha, state, run_id, voucher_id, reason, created, updated FROM handoffs "
            for pi in sorted({(r["project"], r["iid"]) for r in runs}):
                handoffs += [dict(r) for r in db.execute(cols + "WHERE project=? AND iid=? ORDER BY created, sha", pi)]
            have = {(h["project"], h["iid"], h["sha"]) for h in handoffs}
            todo = sorted({s for s in shas if isinstance(s, str) and s})
            for k in range(0, len(todo), 400):   # sha = "<commit>#<label event>"
                q = cols + "WHERE (CASE WHEN instr(sha, '#') > 0 THEN substr(sha, 1, instr(sha, '#') - 1) ELSE sha " \
                           "END) IN (%s) ORDER BY created, sha" % ",".join("?" * len(todo[k:k + 400]))
                handoffs += [dict(r) for r in db.execute(q, todo[k:k + 400])
                             if (r["project"], r["iid"], r["sha"]) not in have]
            handoffs.sort(key=lambda h: (str(h["created"]), str(h["sha"])))
        decisions = []
        if "decisions" in tables and runs:
            q = "SELECT id, run_id, kind, lean_request, state, answer, answerer, voucher_id, sha, created, updated " \
                "FROM decisions WHERE run_id IN (%s) ORDER BY created, id" % ",".join("?" * len(runs))
            decisions = [dict(r) for r in db.execute(q, [r["run_id"] for r in runs])]
    finally:
        db.close()
    return {"runs": runs, "handoffs": handoffs, "decisions": decisions}


def voucher_files(vdir, key, allowed_signers=None):
    out = []
    rx = re.compile(r"^RUN-%s-[1-9][0-9]*$" % re.escape(key))
    for path in sorted(glob.glob(os.path.join(vdir, "RUN-%s-*" % key, "V-*.json"))):
        run_dir = os.path.basename(os.path.dirname(path))
        if not rx.match(run_dir):
            continue
        src = "vouchers/%s/%s" % (run_dir, os.path.basename(path))
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError, UnicodeDecodeError):
            out.append({"_src": src, "_error": "not a voucher file"})
            continue
        v = data.get("voucher") if isinstance(data, dict) else None
        if not isinstance(v, dict) or not str(v.get("run_id", "")).startswith("RUN-%s-" % key):
            continue
        v = dict(v, _src=src)
        if allowed_signers:
            s = data.get("sig")
            v["_sig"] = "ok" if isinstance(s, str) and sign.verify(
                {k: x for k, x in v.items() if not k.startswith("_")}, s, allowed_signers) else "bad"
        else:
            v["_sig"] = "not checked"
        out.append(v)
    return out


NOT_PASSED = "credits not passed in (the KiroCrew app passes them; they are not readable here)"


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def credits_shape(credits) -> str:
    """none | one (a number or a single record: one run's) | keyed ({"runs": {KEY|RUN-id: record}}) | bad."""
    if credits is None:
        return "none"
    if _is_num(credits):
        return "one"
    if not isinstance(credits, dict):
        return "bad"
    return "one" if any(k in credits for k in ("credits", "total", "turns", "manual")) else "keyed"


def _credits(credits, key, run_ids):
    """(the KiroCrew app's record(s) for this requirement or None, why there are none). A number or one record is
    this requirement's (brain eval passes one only when there is one run). Keyed records count when the key is the
    requirement key, a run id the ledger / vouchers know, or RUN-<KEY>-<n> (a run id names its requirement)."""
    shape = credits_shape(credits)
    if shape == "none":
        return None, NOT_PASSED
    if shape == "bad":
        return None, "credits passed in, but not a number or a JSON object"
    if shape == "one":
        return [credits if isinstance(credits, dict) else {"credits": credits}], None
    pool = credits.get("runs") if isinstance(credits.get("runs"), dict) else credits
    rx = re.compile(r"^RUN-%s-[1-9][0-9]*$" % re.escape(key))
    picked = [pool[k] for k in sorted(pool) if (k == key or k in run_ids or rx.match(k)) and
              (isinstance(pool[k], dict) or _is_num(pool[k]))]
    recs = [p if isinstance(p, dict) else {"credits": p} for p in picked]
    return (recs, None) if recs else (None, "credits passed in, but none for %s or its runs" % key)


def _num(x):
    return float(x) if _is_num(x) and x >= 0 else None


def cost_of(recs, why=NOT_PASSED) -> dict:
    if not recs:
        return {"credits": None, "per_turn": None, "context": None, "tokens": None, "tokens_why": NO_TOKENS,
                "src": None, "why": why}
    total, turns, ctx, ok = 0.0, [], None, False
    for r in recs:
        t = _num(r.get("credits", r.get("total")))
        rt = [x for x in r.get("turns") or [] if isinstance(x, dict)] if isinstance(r.get("turns"), list) else []
        for x in rt:
            turns.append({"turn": x.get("turn") if isinstance(x.get("turn"), int) else None,
                          "credits": _num(x.get("credits")),
                          "agent": str(x["agent"])[:40] if isinstance(x.get("agent"), str) else None,
                          "at": _utc(x.get("at")) if isinstance(x.get("at"), str) else None})
        if t is None and rt:
            t = sum(x["credits"] for x in turns[-len(rt):] if x["credits"] is not None)
        if t is not None:
            total += t
            ok = True
        if _num(r.get("context_used")) is not None and _num(r.get("context_window")):
            ctx = {"used": _num(r["context_used"]), "window": _num(r["context_window"]),
                   "note": "context size, not billed tokens"}
    return {"credits": round(total, 4) if ok else None, "per_turn": turns or None, "context": ctx, "tokens": None,
            "tokens_why": NO_TOKENS, "src": "passed in by the KiroCrew app"}


# ---------- an open run: when it last moved, what it waits on ----------

OPEN_QUESTION = ("open", "posting", "asking")


def last_activity(events, hist, led, vouchers) -> dict:
    """{"at", "src"}: the latest time anything happened on the run (a commit made or landed, a Lean record, a park
    question, a bridge ledger row changed, a voucher issued), or at None."""
    marks = [(e["_at"], e["_src"]) for e in events if e.get("_at")]
    marks += [(x, c["src"]) for c in hist["commits"] or [] for x in (c["at"], c["committed_at"]) if x]
    marks += [(q["at"], q["src"]) for q in hist["park"] or [] if q.get("at")]
    for table in ("runs", "handoffs", "decisions"):
        for r in (led or {}).get(table, []):
            for x in (r.get("created"), r.get("updated")):
                if _utc(x):
                    marks.append((_utc(x), "bridge ledger %s" % table))
    marks += [(_utc(v.get("issued_at")), str(v.get("_src"))) for v in vouchers if _utc(v.get("issued_at"))]
    marks = [m for m in marks if m[0] and not DATE_RE.match(m[0])]
    at, src = max(marks) if marks else (None, None)
    return {"at": at, "src": src}


def waiting_on(repo, cfg, key, tree, hist, decisions):
    """What an unmerged run waits on: its park.json now (at the run's tip) when it is waiting, or an open bridge
    question. {"kind", "ask", "lean_request", "since", "decision", "src"} or None."""
    pp = "%s/%s/park.json" % (cfg["runs_dir"], key)
    text = _show(repo, hist["tip"], pp) if hist.get("tip") else tree.read(pp)
    row = _park_row(text, "%s@%s" % (pp, hist["tip"][:12]) if hist.get("tip") else "%s (%s)" % (pp, tree.label),
                    None)
    open_q = [d for d in decisions if d["source"] == "bridge" and not d.get("choice") and d.get("state") in
              OPEN_QUESTION]
    if row and row["status"] == "waiting":
        same = [q for q in hist["park"] or [] if (q["kind"], q["lean_request"], q["question_sha256"]) ==
                (row["kind"], row["lean_request"], row["question_sha256"]) and q["status"] == "waiting"]
        return {"kind": row["kind"], "ask": row["ask"], "lean_request": row["lean_request"],
                "since": same[0]["at"] if same else None,
                "decision": open_q[-1]["id"] if open_q else None, "src": row["src"]}
    if open_q:
        d = open_q[-1]
        return {"kind": d.get("kind"), "ask": None, "lean_request": d.get("lean_request"), "since": d.get("asked_at"),
                "decision": d["id"], "src": "bridge ledger decisions"}
    return None


# ---------- the trace ----------

def build(repo, cfg, key, ref=None, ledger_path=None, vouchers_dir=None, credits=None, as_of=None,
          allowed_signers=None, _usage=None) -> dict:
    """The trace of one requirement key. `credits`: a number, a KiroCrew record {credits|total, turns, manual,
    context_used, context_window}, or {"runs": {RUN-id or KEY: record}}. `as_of`: ISO time the 14-day window is
    judged at (default now; recorded in the trace)."""
    if not JIRA_RE.match(key or ""):
        raise BrainError("%r is not a requirement key like PAY-1" % key)
    tree = Tree(repo, ref)
    as_of_dt = _dt(as_of) if as_of else utcnow()
    if as_of and not as_of_dt:
        raise BrainError("as_of %r is not an ISO time" % as_of)
    rp = "%s/%s/run.json" % (cfg["runs_dir"], key)
    raw = tree.read(rp)
    if raw is None:
        raise BrainError("%s not found in %s (a merged run: --ref origin/%s)" % (rp, tree.label, cfg["default_branch"]))
    try:
        rec = json.loads(raw)
    except ValueError:
        raise BrainError("%s is not valid JSON" % rp)
    if not isinstance(rec, dict):
        raise BrainError("%s is not an object" % rp)
    run_src = "%s (%s)" % (rp, tree.label)
    intent = rec.get("intent") if isinstance(rec.get("intent"), str) and rec.get("intent") else None
    owner = rec.get("owner") if isinstance(rec.get("owner"), str) and USER_RE.match(rec.get("owner")) else None
    branch = rec.get("branch") if isinstance(rec.get("branch"), str) and rec.get("branch") else None
    handoff = rec.get("handoff") if isinstance(rec.get("handoff"), dict) else None
    missing = ["%s: %s is not a %s (ignored)" % (run_src, f, what) for f, what in (
        ("intent", "string"), ("branch", "string"), ("brain_commit", "string"), ("handoff", "JSON object"))
        if rec.get(f) is not None and not isinstance(rec[f], dict if f == "handoff" else str)]

    events, chains = lean_events(tree, intent) if intent else ([], {})
    if not chains:
        missing.append("Lean audit (no aidlc-docs/audit/*.jsonl in %s)" % tree.label)
    docs = "%s/%s" % (lean.DOCS, intent) if intent else None
    dec_path = docs + "/decisions.md" if docs else None
    dec_text = tree.read(dec_path) if dec_path else None
    req_path = docs + "/inception/requirements/requirements.md" if docs else None

    refs = _refs(repo)
    hist = git_history(repo, cfg, rec, ref, key, refs) if branch else {
        "tip": None, "tip_src": None, "tip_note": None, "default_ref": default_ref(repo, cfg, ref, refs), "merge": None,
        "commits": None, "range": None, "handover_commit": None, "park": None, "requests": [], "_log": [], "_rp": rp}
    if hist["tip"] is None:
        missing.append("run branch %s (not found locally, on origin, or as a merge on the default branch)"
                       % branch)
        row = _park_row(tree.read("%s/%s/park.json" % (cfg["runs_dir"], key)),
                        "%s/%s/park.json (%s)" % (cfg["runs_dir"], key, tree.label), None)
        hist["park"] = [row] if row else []

    led = None
    if ledger_path:
        led = ledger_rows(ledger_path, key, [r["commit"] for r in hist["requests"]])
    else:
        missing.append("bridge ledger (--ledger not given)")
    vouchers = []
    if vouchers_dir:
        vouchers = voucher_files(vouchers_dir, key, allowed_signers)
    else:
        missing.append("vouchers (--vouchers not given and no intake store)")
    run_ids = sorted({r["run_id"] for r in (led or {}).get("runs", [])} |
                     {v["run_id"] for v in vouchers if v.get("run_id")})

    # ----- the hand-over (rules H1-H4): which `brain handoff` the runner got, if any -----
    hand = handover(repo, hist, handoff, led, vouchers)
    handed, handoff_at = hand["value"], hand["at"]
    hist["handover_commit"] = hand["commit"]
    if hist["commits"] is not None:
        log_shas = {c["sha"] for c in hist["_log"]}
        before = _ancestry(hist["_log"], hand["commit"]) if handed and hand["commit"] in log_shas else None
        for c in hist["commits"]:
            c["after_handover"] = False if not handed else (c["sha"] not in before) if before is not None else None

    # ----- decisions -----
    decisions = md_decisions(dec_text, dec_path, cfg.get("bot_users", [])) if dec_text is not None else []
    if dec_text is None and docs:
        missing.append("%s (not in %s)" % (dec_path, tree.label))
    req_done = [e for e in events if e.get("event") == "STAGE_COMPLETED" and _data(e).get("stage") == "requirements"]
    a_at, a_src = ((req_done[-1]["_at"], req_done[-1]["_src"]) if req_done else
                   (_utc(rec.get("created_at")), run_src + " created_at"))
    decisions += assumptions(tree.read(req_path), req_path, a_at, a_src) if req_path else []
    bridge = {}
    for d in (led or {}).get("decisions", []):
        bridge[(d["run_id"], d["id"])] = {
            "id": d["id"], "source": "bridge", "run_id": d["run_id"], "by": "bridge", "kind": d["kind"],
            "lean_request": d["lean_request"], "state": d["state"], "choice": d["answer"],
            "who": d["answerer"] if d["answerer"] and USER_RE.match(str(d["answerer"])) else None,
            "voucher_id": d["voucher_id"], "at": _utc(d["updated"]) if d["answer"] else None,
            "asked_at": _utc(d["created"]), "eval_oracle": False, "signature": None, "text": None, "cites": [],
            "rejected_items": None, "recorded_in": None, "src": ["bridge ledger decisions"]}
    oracle = []

    def _s(x):
        return x if isinstance(x, str) and x else None
    for v in vouchers:
        if v.get("kind") != "answer" or not _s(v.get("decision")):
            continue
        k = (v.get("run_id"), v.get("decision"))
        row = bridge.setdefault(k, {"id": v.get("decision"), "source": "bridge", "run_id": v.get("run_id"),
                                    "by": "bridge", "kind": None, "lean_request": None, "state": None,
                                    "choice": None, "who": None, "voucher_id": None, "at": None, "asked_at": None,
                                    "eval_oracle": False, "signature": None, "text": None, "cites": [],
                                    "rejected_items": None, "recorded_in": None, "src": []})
        items = v.get("rejected_items")   # batch review: the items of review_items the answer rejected
        row.update(kind=_s(v.get("decision_kind")) or row["kind"],
                   lean_request=_s(v.get("lean_request")) or row["lean_request"],
                   choice=_s(v.get("choice")), voucher_id=_s(v.get("voucher_id")),
                   at=_utc(v.get("issued_at")) or row["at"], signature=v.get("_sig"),
                   eval_oracle=v.get("eval_oracle") is True,
                   rejected_items=[str(x)[:40] for x in items if isinstance(x, str)][:50]
                   if isinstance(items, list) and items else row["rejected_items"])
        if isinstance(v.get("answerer"), str) and PSEUD_RE.fullmatch(v["answerer"]):
            row["who"] = v["answerer"]
        row["src"] = row["src"] + [v["_src"]]
    by_voucher = {b["voucher_id"]: b for b in bridge.values() if b["voucher_id"]}
    for d in decisions:
        for vid in d["refs"]["vouchers"]:
            if vid in by_voucher and by_voucher[vid]["recorded_in"] is None:
                by_voucher[vid]["recorded_in"] = "%s (%s)" % (d["id"], d["src"])
                d["bridge_decision"] = by_voucher[vid]["id"]
    for b in sorted(bridge.values(), key=lambda x: (x["asked_at"] or x["at"] or "", x["id"])):
        (oracle if b["eval_oracle"] else decisions).append(b)

    # ----- humans -----
    humans = []
    vmap = {}
    for b in bridge.values():
        if b["eval_oracle"] or not b["choice"]:
            continue
        kind = {"approve": "approval", "reject": "rejection"}.get(b["choice"], "answer")
        what = b["lean_request"].split(" ", 1)[0] if b["lean_request"] else b["kind"]
        h = {"at": b["at"], "kind": kind, "what": what, "choice": b["choice"], "who": b["who"], "channel": "bridge",
             "decision": b["id"], "voucher_id": b["voucher_id"], "judgment_point": True, "after_handover": None,
             "src": list(b["src"])}
        if kind == "rejection" and b["rejected_items"]:
            h["rejected_items"] = b["rejected_items"]
        humans.append(h)
        if b["voucher_id"]:
            vmap[b["voucher_id"]] = h
    for e in events:
        ev = e.get("event")
        if ev not in ("APPROVED", "REJECTED", "APPROVAL_REFUSED") and not (
                ev == "HUMAN_TURN" and _data(e).get("allow_protected")):
            continue
        d = _data(e)
        chan = str(d.get("claimed_channel") or "")
        words = str(d.get("words") or "")
        vid = (VOUCHER_RE.findall(chan) or VOUCHER_RE.findall(words) or [None])[0]
        if vid and vid in vmap:
            vmap[vid]["src"].append(e["_src"])
            vmap[vid]["lean_event"] = ev
            continue
        if vid and any(o["voucher_id"] == vid for o in oracle):
            continue
        ps = PSEUD_RE.search(chan) or PSEUD_RE.search(words)
        kind = {"APPROVED": "approval", "REJECTED": "rejection", "APPROVAL_REFUSED": "approval_refused",
                "HUMAN_TURN": "allow_test_change"}[ev]
        humans.append({"at": e["_at"], "kind": kind,
                       "what": d.get("kind") if ev != "HUMAN_TURN" and isinstance(d.get("kind"), str) else None,
                       "choice": None, "who": ps.group(0) if ps else pseudonym((e.get("actor") or {}).get("git_email")
                                                                                if isinstance(e.get("actor"), dict)
                                                                                else None),
                       "channel": "bridge" if vid or chan.lower().startswith("gitlab") else "local",
                       "decision": None, "voucher_id": vid, "judgment_point": ev != "HUMAN_TURN",
                       "after_handover": None, "check": d.get("check") if ev == "APPROVAL_REFUSED" else None,
                       "src": [e["_src"]]})
    for c in hist["commits"] or []:
        if c["by"] == "person" and c["after_handover"]:
            humans.append({"at": c["at"], "kind": "push_after_handover", "what": "records" if c["records_only"]
                           else "code", "choice": None, "who": c["who"], "channel": "git", "decision": None,
                           "voucher_id": None, "judgment_point": False, "after_handover": True, "sha": c["sha"],
                           "src": [c["src"]]})
    crecs, credits_why = _credits(credits, key, run_ids)
    for r in crecs or []:
        for m in r.get("manual") or [] if isinstance(r.get("manual"), list) else []:
            if isinstance(m, dict) and isinstance(m.get("kind"), str):
                who = m.get("who") if isinstance(m.get("who"), str) and (
                    USER_RE.match(m["who"]) or PSEUD_RE.fullmatch(m["who"])) else None
                humans.append({"at": _utc(m.get("at")) if isinstance(m.get("at"), str) else None,
                               "kind": "platform_" + re.sub(r"[^a-z_]", "", m["kind"].lower())[:20],
                               "what": None, "choice": None, "who": who, "channel": "kirocrew", "decision": None,
                               "voucher_id": None, "judgment_point": False, "after_handover": None,
                               "src": ["passed in by the KiroCrew app (manual)"]})
    for h in humans:
        if h["after_handover"] is None and h["at"] and handed and handoff_at:
            h["after_handover"] = h["at"] > handoff_at
        elif h["after_handover"] is None and not handed:
            h["after_handover"] = False
    humans.sort(key=lambda h: (h["at"] or "", h["kind"], h.get("decision") or "", h.get("sha") or ""))

    # ----- actions -----
    blocked = [{"at": e["_at"], "gate": _data(e).get("gate"), "tool": _data(e).get("tool"),
                "path": scrub(_data(e)["path"]) if _data(e).get("path") else None,
                "command": scrub(_data(e)["command"], 120) if _data(e).get("command") else None, "src": e["_src"]}
               for e in events if e.get("event") == "BLOCKED"]
    turns = {}
    for e in events:
        if e.get("event") in ("SESSION_START", "SUBAGENT_START", "HUMAN_TURN", "HOST_TURN", "SUBAGENT_PROMPT"):
            half = "unknown" if handed and not handoff_at else (
                "after_handover" if handed and e["_at"] and e["_at"] > handoff_at else "before_handover")
            t = turns.setdefault(e["event"], {})
            t[half] = t.get(half, 0) + 1
    handovers = [{"at": r["at"], "kind": "requested", "by": r["by"], "commit": r["commit"],
                  "taken": handed and r["commit"] == hand["commit"], "src": r["src"]} for r in hist["requests"]]
    if handoff and not handovers:
        handovers.append({"at": _utc(handoff.get("requested_at")), "kind": "requested", "by": handoff.get("by")
                          if isinstance(handoff.get("by"), str) and USER_RE.match(handoff["by"]) else None,
                          "commit": hist["handover_commit"], "taken": handed, "src": run_src + " handoff"})
    for h in (led or {}).get("handoffs", []):
        handovers.append({"at": _utc(h["updated"]), "kind": "bridge " + str(h["state"]), "run_id": h["run_id"],
                          "mr_iid": h["iid"], "sha": str(h["sha"]).split("#", 1)[0],
                          "voucher_id": h["voucher_id"],
                          "reason": scrub(h["reason"], 300) if h["reason"] else None, "src": "bridge ledger handoffs"})
    for v in vouchers:
        if v.get("kind") == "handoff":
            handovers.append({"at": _utc(v.get("issued_at")), "kind": "voucher", "run_id": v.get("run_id"),
                              "sha": v.get("sha"), "voucher_id": v.get("voucher_id"), "signature": v.get("_sig"),
                              "src": v["_src"]})
    handovers.sort(key=lambda x: (x["at"] or "", x["kind"]))

    # ----- brain -----
    u = _usage if _usage is not None else usage.collect(repo, cfg, ref=ref)
    urow = next((r for r in u["runs"] if r["intent"] == intent), None)
    brain = {"brain_commit": rec.get("brain_commit") if isinstance(rec.get("brain_commit"), str) else None,
             "loaded": urow["loaded"] if urow else None,
             "cited": urow["cited"] if urow else [], "stale": urow["stale"] if urow else [],
             "unknown": urow["unknown"] if urow else [],
             "proposals": proposals_of(dec_text, dec_path) if dec_text is not None else None,
             "src": "brainkit.usage (%s): loaded = Brain at brain_commit; cited = ids in aidlc-docs/%s/*.md outside "
                    "brain-proposal blocks" % (u["ref"], intent)}

    # ----- outcome -----
    acc = [h for h in humans if h["what"] in ACCEPT_WHAT and h["kind"] in ("approval", "rejection")]
    first_ok = next((h for h in acc if h["kind"] == "approval"), None)
    rejections = [h for h in acc if h["kind"] == "rejection"]
    accepted = {"value": True if first_ok else (False if rejections else None),
                "first_time": (not any(r["at"] and first_ok["at"] and r["at"] < first_ok["at"] for r in rejections))
                if first_ok else (False if rejections else None),
                "at": first_ok["at"] if first_ok else None, "rejections": len(rejections),
                "src": "RELEASE approvals/rejections (Lean audit) and acceptance answers (bridge)"}
    merged = hist["merge"]
    led_states = sorted({str(r["state"]) for r in (led or {}).get("runs", [])}) if led else None
    merged_at = {"value": merged["at"] if merged else None, "commit": merged["commit"] if merged else None,
                 "how": merged["how"] if merged else None,
                 "src": merged["src"] if merged else "no merge into %s found" % (hist["default_ref"] or
                                                                                   cfg["default_branch"]),
                 "note": merged.get("note") if merged else None, "bridge_state": led_states,
                 "via": merged.get("via") if merged else None}
    if not merged and led_states and "merged" in led_states:
        merged_at["note"] = "the bridge ledger says the MR was merged, but %s here has no merge of it (fetch it?)" % (
            hist["default_ref"] or cfg["default_branch"])
    outcome = {
        "handed_over": {"value": handed, "at": handoff_at, "bridge_accepted": hand["bridge_accepted"],
                        "requested": bool(handoff or hist["requests"]), "requests": hand["requests"],
                        "commit": hand["commit"], "rule": hand["rule"], "how": hand["how"],
                        "src": run_src + " handoff; the run's `brain handoff` commits" +
                               ("; bridge ledger handoffs and runs" if led is not None else "") +
                               ("; hand-over vouchers" if any(v.get("kind") == "handoff" for v in vouchers) else "")},
        "accepted": accepted,
        "merged_at": merged_at,
        "reverted_or_hotfixed_within_14d": after_merge(repo, cfg, key, merged, hist["commits"], hist["default_ref"],
                                                       as_of_dt, refs),
        "bridge_runs": [{"run_id": r["run_id"], "state": r["state"], "created": _utc(r["created"]),
                         "updated": _utc(r["updated"]), "src": "bridge ledger runs"}
                        for r in led["runs"]] if led is not None else None,
        "last_activity": last_activity(events, hist, led, vouchers),
        "waiting_on": None if merged else waiting_on(repo, cfg, key, tree, hist, decisions),
    }

    if credits is None:
        missing.append("credits (not passed in)")
    elif not crecs:
        missing.append("credits (%s)" % credits_why)
    missing.append("pushes (git keeps no push log; person commits after hand-over stand in for them)")
    missing.append("CI pipelines (GitLab, not readable here)")
    return {
        "schema": SCHEMA, "key": key, "as_of": iso(as_of_dt), "ref": tree.label,
        "intent": intent, "owner": owner, "branch": branch,
        "brain_commit": brain["brain_commit"], "run_ids": run_ids,
        "src": {"key": "argument", "intent": run_src, "owner": run_src, "branch": run_src,
                "brain_commit": run_src, "run_ids": "bridge ledger runs + voucher files",
                "as_of": "argument" if as_of else "clock", "lean_audit": chains, "git": {
                    "tip": hist["tip"], "tip_src": hist["tip_src"], "tip_note": hist["tip_note"],
                    "range": hist["range"], "default_ref": hist["default_ref"]}},
        "stages": stages_of(events),
        "actions": {"commits": hist["commits"], "pushes": None, "blocked": blocked, "turns": turns,
                    "handovers": handovers, "questions": hist["park"]},
        "decisions": decisions,
        "oracle": oracle,
        "checks": checks_of(events, tree, intent) if intent else None,
        "humans": humans,
        "brain": brain,
        "cost": cost_of(crecs, credits_why),
        "outcome": outcome,
        "missing": missing,
    }


# ---------- text ----------

def render(t) -> str:
    out = ["%s · %s · %s（%s）" % (t["key"], t["intent"] or "-", t["branch"] or "-", t["ref"])]
    out.append("负责人 %s；Brain %s；run %s" % (t["owner"] or "-", (t["brain_commit"] or "-")[:10],
                                          ", ".join(t["run_ids"]) or "-"))
    chains = t["src"]["lean_audit"]
    if chains:
        out.append("Lean 日志：" + "；".join("%s %d 条 %s" % (os.path.basename(p), c["records"],
                                                           "链完好" if c["chain"] == "ok" else c["chain"])
                                         for p, c in sorted(chains.items())))
    g = t["src"]["git"]
    if g.get("tip_note"):
        out.append("分支：用的是 %s；%s" % (g["tip_src"], g["tip_note"]))
    if t["stages"]:
        mark = {"done": "x", "skipped": "S", "started": "-"}
        out.append("")
        out.append("阶段：")
        out += ["  [%s] %s · %s" % (mark[s["state"]], s["id"], s["at"] or "-") for s in t["stages"]]
    c = t["checks"]
    if c and c["verify"]:
        out.append("verify：%d 次，失败 %d 次；最后一次 %s" % (len(c["verify"]), c["verify_failed"],
                                                     "通过" if c["verify"][-1]["ok"] else "失败"))
    a = t["actions"]
    if a["commits"] is not None:
        bots = sum(1 for x in a["commits"] if x["by"] == "bot")
        out.append("提交：%d 个（机器人 %d，人 %d）" % (len(a["commits"]), bots, len(a["commits"]) - bots))
    if a["blocked"]:
        out.append("被 Lean 拦下：%d 次（%s）" % (len(a["blocked"]), "、".join(sorted({str(b["gate"]) for b in a["blocked"]}))))
    if t["humans"]:
        out.append("")
        out.append("人：")
        for h in t["humans"]:
            out.append("  %s %s%s%s · %s%s" % (h["at"] or "-", h["kind"], " " + h["what"] if h["what"] else "",
                                             " " + h["choice"] if h["choice"] and h["kind"] == "answer" else "",
                                             h["who"] or "?", "" if h["judgment_point"] else "（判断点之外）"))
    n_by = {}
    for d in t["decisions"]:
        n_by[d["by"] or "unknown"] = n_by.get(d["by"] or "unknown", 0) + 1
    if t["decisions"]:
        out.append("决策：%d 条（%s）" % (len(t["decisions"]), "，".join("%s %d" % kv for kv in sorted(n_by.items()))))
    b = t["brain"]
    out.append("Brain：装载 %s 条，引用 %d 条%s" % (b["loaded"] if b["loaded"] is not None else "?", len(b["cited"]),
                                            "，过时引用 %s" % ", ".join(b["stale"]) if b["stale"] else ""))
    cost = t["cost"]
    out.append("credits：%s" % (cost["credits"] if cost["credits"] is not None else
                               "没有（KiroCrew 没传）" if cost.get("why") == NOT_PASSED else "没有（%s）" % cost.get("why")))
    o = t["outcome"]
    m = o["merged_at"]
    out.append("")
    out.append("交接：%s；验收：%s%s；合并：%s%s" % (
        "是" if o["handed_over"]["value"] else "否",
        {True: "通过", False: "被驳回", None: "没有记录"}[o["accepted"]["value"]],
        "（一次通过）" if o["accepted"]["first_time"] else ("（驳回 %d 次）" % o["accepted"]["rejections"]
                                                       if o["accepted"]["rejections"] else ""),
        m["value"] or "没有", "（%s）" % m["how"] if m.get("how") else ""))
    ho = o["handed_over"]
    if ho.get("requested") and (not ho["value"] or (ho.get("requests") or 0) > 1):
        out.append("  交接：%s（`brain handoff` %d 次；%s）" % ("交接的是 %s" % ho["commit"][:12] if ho["value"] and
                                                         ho.get("commit") else "是" if ho["value"] else "没有交接",
                                                         ho.get("requests") or 0, ho.get("how")))
    if m.get("via"):
        out.append("  " + m["via"])
    if m.get("note"):
        out.append("  " + m["note"])
    w = o.get("waiting_on")
    if w:
        out.append("  在等：%s%s（从 %s 起）" % (w["kind"] or "?", " " + w["lean_request"] if w["lean_request"] else "",
                                         w["since"] or "?"))
    if o.get("last_activity", {}).get("at") and not m["value"]:
        out.append("  最后一次有动静：%s" % o["last_activity"]["at"])
    r = o["reverted_or_hotfixed_within_14d"]
    if r["window_end"]:
        out.append("14 天内回滚/热修/缺陷：%s（窗口到 %s%s）" % (
            {True: "有", False: "没有", None: "还不知道"}[r["value"]], r["window_end"],
            "，已结束" if r["window_closed"] else "，还没结束"))
        out += ["  %s %s %s" % (x["at"], (x.get("sha") or x.get("claim", ""))[:12], x["rule"]) for x in r["evidence"]]
        out += ["  不算：%s %s %s（%s）" % (x["at"], x["sha"][:12], x["rule"], x["why"]) for x in r.get("ignored") or []]
    if t["missing"]:
        out.append("")
        out.append("没读到：" + "；".join(t["missing"]))
    return "\n".join(out)
