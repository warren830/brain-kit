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
default branch run_stretch, rules R1-R5), which bridge runs are this branch's (scope_runs, rule H0: a re-run of the
key on another branch has its own hand-over, answers and acceptance), which `brain handoff` handed it over (handover,
rules H1-H4: the one the bridge accepted; with a ledger given, only the bridge's records count), and which time is
used (a commit's "at" is its author date; a merge's is the committer date of the commit that landed it). Branch
copies (local, origin) that are older, ahead or diverged are named in
src.git.tip_note. The run's own commits are never counted as hotfixes; after the hand-over, a revert or a person's
commit naming the key with a fix word is never the run's own; a hotfix changes something outside brain/, runs/ and
aidlc-docs/ (A1, A2).

People: only GitLab usernames and pseudonyms already in the sources. Git and Lean identities (emails, names, hosts)
become a pseudonym G-<6 hex> of the email; Lean's audit file names (<host>-<6 hex>.jsonl) become L-<6 hex>; emails
and home directories in free text (blocked commands, stage notes, failed check commands) are replaced. Human message
text is never copied, except what decisions.md holds.
Answers an eval oracle gave (golden tasks, `eval_oracle: true`) are kept apart and never count as a person's.

A split (brainkit/split.py): a big requirement (the parent) and its children, each a run of its own that continues
the parent's Lean intent for one unit. Their records share the intent's folder, so each run keeps only its own:
  child   (run.json "parent"): its Lean events are its clone's ledger file(s) (aidlc-docs/audit/<host>-<lean_clone>
          .jsonl; without lean_clone, the events of Lean team lean_team); its commits are judged against the parent's
          run branch (target_branch: its MR goes there), its merge against the default branch as for any run (it gets
          there with its parent); "family" says whether it merged into the parent's branch (intents.child_merged: its
          run.json there has its `brain handoff` record "mr", and its owner's run branch, while one is here, has its
          tip there; read on origin's copy as last fetched when there is one, else the local one).
  parent  (other run.json files name it as parent: in the tree read, i.e. merged into its branch, and on their own
          run branches here, not merged yet; a child's run.json a sibling carried in (`--on`) is not its merge: such a
          child is read from its own branch): its events are the intent's minus its children's clone files, its
          commits its branch's minus its children's. "family" lists each child with its numbers (from the child's own
          trace) and rolls them up with the parent's.
  both    decisions.md and requirements.md are the intent's: a row is the run's whose commit wrote that line (git
          blame of the tree read; a line not committed yet: the run whose branch is checked out). Brain usage: each
          run's own usage row (brainkit.usage attributes unit pages to the child of that unit).
A run that is not part of a split has no "family" key: its trace is what it was before splits existed.
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

from . import intents, lean, reflect, sign, usage
from .bridge.ledger import RESERVED
from .common import BRANCH_RE, JIRA_RE, USER_RE, ZERO_SHA, BrainError, git, iso, parse_branch, utcnow

SCHEMA = 1
WINDOW_DAYS = 14
STAGE_STATE = {"STAGE_STARTED": "started", "STAGE_COMPLETED": "done", "STAGE_SKIPPED": "skipped"}
# Lean 0.3's optional phases (aidlc_lean.py PHASES, IDEATION_STAGES, IDEATION_REQUIRED): a whole phase skipped is one
# PHASE_SKIPPED {phase, stages, reason} record
PHASE_NAMES = {"ideation": "Ideation", "operations": "Operations"}
IDEATION_IDS = ("intent-capture", "market-research", "feasibility", "scope-definition", "team-formation",
                "rough-mockups", "approval-handoff")
IDEATION_REQUIRED = ("intent-capture", "scope-definition", "approval-handoff")
LEAN_APPROVALS = ("IDEATION", "REQUIREMENTS", "DESIGN", "INCEPTION", "PLAN", "RELEASE")
ACCEPT_WHAT = ("RELEASE", "acceptance")
VOUCHER_RE = re.compile(r"(?<![0-9A-Za-z])V-[0-9a-f]{10}(?![0-9a-f])")
PSEUD_RE = re.compile(r"(?<![0-9A-Za-z])P-[0-9A-F]{4}(?![0-9A-Za-z])")
DID_RE = re.compile(r"(?<![0-9A-Za-z-])D-[1-9][0-9]*(?![0-9])")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
ASSUMPTION_RE = re.compile(r"^\s*[-*]?\s*(A-[1-9][0-9]*)\s*[:：.)]\s*(\S.*)$")
# "regression" is a fix word, "regression test(s)" is not: "add regression tests for PAY-4" fixes nothing
FIX_RE = re.compile(r"(?i)(hot-?fix|\bfix(?:es|ed)?\b|\bbug|defect|regression(?![\s_-]*(?:tests?|suites?|cases?|checks?)\b)"
                    r"|\brevert|roll ?back|修复|热修|回滚|缺陷|故障)")
HEX_RE = re.compile(r"^[0-9a-f]{7,40}$")
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
    """Any ISO time (Lean ms + Z, git +08:00) -> YYYY-MM-DDTHH:MM:SSZ; a bare date stays a date; junk, and a day that
    does not exist (2026-02-30, 2026-13-01), -> None."""
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    if DATE_RE.match(s):
        try:
            datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
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


def ideation_in_use(events) -> bool:
    """Lean 0.3's rule (aidlc_lean.py Project.ideation_in_use): an Ideation stage has a start or done record newer
    than the last phase skip of Ideation (with no phase skip: any start or done record)."""
    used = skip = -1
    for k, e in enumerate(events):
        d = _data(e)
        if e.get("event") == "PHASE_SKIPPED" and d.get("phase") == "ideation":
            skip = k
        elif e.get("event") in ("STAGE_STARTED", "STAGE_COMPLETED") and d.get("stage") in IDEATION_IDS:
            used = k
    return used > skip


def stages_of(events) -> list:
    """Each stage's last state (done / skipped / started), by Lean 0.3's rule (aidlc_lean.py stage_event): the
    stage's own last STAGE_* record, or a later PHASE_SKIPPED {phase, stages, reason} that covered it (`lean new`
    without --ideation, `lean stage skip ideation|operations`; the row's `phase_skipped` names the phase). Once
    Ideation is in use again, its phase skips no longer count, and a stage Ideation needs (IDEATION_REQUIRED) that was
    skipped on its own before has no record until it is started or done. Rows in the order of their first record;
    the stages of one phase skip in Lean's order."""
    in_use = ideation_in_use(events)
    rows = {}

    def put(sid, st, note, e, phase=None, pos=0):
        row = rows.setdefault(sid, {"id": sid, "first_at": e["_at"], "times": {},
                                    "_order": (0, pos, "") if phase else (1, 0, sid)})
        row["times"][st] = row["times"].get(st, 0) + 1
        row.update(state=st, at=e["_at"], note=scrub(note) if isinstance(note, str) else None, src=e["_src"])
        if phase:
            row["phase_skipped"] = phase
        else:
            row.pop("phase_skipped", None)

    for e in events:
        ev, d = e.get("event"), _data(e)
        if ev in STAGE_STATE:
            sid = str(d.get("stage") or "?") + ("/" + str(d["unit"]) if d.get("unit") else "")
            put(sid, STAGE_STATE[ev], d.get("note") if ev == "STAGE_COMPLETED" else d.get("reason")
                if ev == "STAGE_SKIPPED" else None, e)
        elif ev == "PHASE_SKIPPED" and d.get("phase") in PHASE_NAMES and isinstance(d.get("stages"), list) and \
                not (d["phase"] == "ideation" and in_use):
            for pos, sid in enumerate(s for s in d["stages"] if isinstance(s, str) and s):
                put(sid, "skipped", d.get("reason"), e, d["phase"], pos)
    if in_use:
        for sid in IDEATION_REQUIRED:
            if rows.get(sid, {}).get("state") == "skipped":
                del rows[sid]
    out = sorted(rows.values(), key=lambda r: (r["first_at"] or "",) + r["_order"])
    for r in out:
        del r["_order"]
    return out


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


def _branch_in(text):
    """The branch a run.json text names, or None."""
    try:
        r = json.loads(text or "null")
    except ValueError:
        return None
    b = r.get("branch") if isinstance(r, dict) else None
    return b if isinstance(b, str) and b else None


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


def fresher_than_worktree(repo, rp, hist):
    """{"head", "read", "note"} when the working tree is behind what the run's history is read at (hist["tip"]: the
    freshest copy of its branch, or the copy that merged): HEAD is an ancestor of that commit and not the commit
    itself (the owner's checkout stopped at the hand-over; fetched, not pulled). The run is then read where a clone
    without that checkout reads it: the default branch once it merged there (when its run.json is there), else that
    commit (by the ref that names it). None: the working tree is read."""
    tip, head = hist.get("tip"), _commit(repo, "HEAD")
    if not tip or not head or head == tip or not _ancestor(repo, head, tip):
        return None
    src = hist.get("tip_src")
    cands = [(hist["default_ref"], "the default branch: the run merged")] if hist.get("merge") and \
        hist.get("default_ref") else []
    cands.append((src if isinstance(src, str) and src.startswith("refs/") and _commit(repo, src) == tip else tip,
                  src or "the run's last commit"))
    for r, why in cands:
        if r and _show(repo, r, rp) is not None:
            label = r if r != tip else tip[:12]
            return {"head": head, "read": r, "note": "HEAD %s is behind %s (%s): the run is read there, not from the "
                    "working tree (its uncommitted changes are not read)" % (head[:12], label, why)}
    return None


def git_history(repo, cfg, rec, ref, key, refs=None, target=None):
    """Commits of the run branch, the hand-over commit, the merge, park.json history. `target` (a split child):
    (the parent's run branch name, the ref or commit it is read at, or None): the run's own commits are judged against
    it as they are against the default branch for any other run (rules M1-M6, C1-C3: its MR goes there), and the
    commit the branch started from is left out too (`brain start --on`: a sibling's branch); out["into_parent"] is
    how it reached the parent's branch, out["merge"] its merge into the default branch."""
    branch = rec.get("branch") if isinstance(rec.get("branch"), str) else ""
    bc = rec.get("brain_commit") if isinstance(rec.get("brain_commit"), str) else None
    intent = rec.get("intent") if isinstance(rec.get("intent"), str) else None
    rp = "%s/%s/run.json" % (cfg["runs_dir"], key)
    pp = "%s/%s/park.json" % (cfg["runs_dir"], key)
    refs = _refs(repo) if refs is None else refs
    dref = default_ref(repo, cfg, ref, refs)
    gcfg, gref = (dict(cfg, default_branch=target[0]), target[1]) if target else (cfg, dref)
    copies = copies_of(repo, branch, ref, refs)
    tip, tip_how, tip_note = freshest(repo, copies)
    out = {"tip": tip, "tip_src": tip_how, "tip_note": tip_note, "default_ref": dref, "merge": None, "commits": None,
           "range": None, "handover_commit": None, "park": None, "requests": [], "_log": [], "_rp": rp}
    land = landing(repo, gcfg, gref, key, intent, branch, copies, refs) if gref else None
    if target:
        main = landing(repo, cfg, dref, key, intent, branch, copies, refs) if dref else None
        out.update(merge=main["merge"] if main else None, into_parent=land["merge"] if land else None)
    if land:
        if not target:
            out["merge"] = land["merge"]
        if not land["tip"]:
            return out
        tip = land["tip"]
        out.update(tip=tip, tip_src=land["tip_src"], tip_note="; ".join(x for x in land["notes"] if x) or None)
        exclude = land["exclude"]
    elif tip:
        exclude = [gref] if gref else ([bc] if _commit(repo, bc) else [])
    else:
        return out
    rng = [tip] + ["^" + x for x in exclude]
    base = _base_of(repo, tip, rp) if target else None
    if base and base not in exclude:
        rng.append("^" + base)
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
    `led` and `vouchers` hold this branch's runs only (rule H0, scope_runs: a re-run of the key on another branch
    has its own hand-over).
      H1  the bridge accepted a hand-over: a hand-over voucher (its signature not bad), a ledger hand-over row
          'accepted', or a ledger run (not one being signed); the first of them. The hand-over is the last request
          at or before its commit: the commit itself or one of its ancestors; for a rebased copy, the request whose
          run.json hand-over is the same; else the last request made before the bridge accepted it.
      H2  nothing accepted, and the ledger has the bridge refusing a request: not handed over (the runner never had
          the run; what the owner did next is the local half).
      H3  nothing accepted and the ledger (given) has no refusal either: not handed over. The ledger is the
          bridge's record and the runner gets a run only from the bridge: no row for any request (`brain handoff`
          stopped before the label, exit 1; the bridge has not checked it yet), a check still going on, or a refusal
          that cannot be found because the run's commits are not here (squash merge, branch gone). Runner commits
          after a request are named in `how` (is it the bridge's ledger?), never taken as the hand-over.
      H4  no ledger given and no hand-over voucher: the last request before the bot's first commit after a request
          (an earlier one did not go through); no bot commit: the last request. Without the run's commits: what
          run.json says.
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
    if led is not None:
        rows = sorted({str(h["state"]) for h in led.get("handoffs", [])})
        notes = (["its hand-over rows: %s" % ", ".join(rows)] if rows else []) + (
            ["the runner committed after the request all the same (is this the bridge's ledger?)"] if bot is not None
            else []) + (["the run's commits are not here (a refusal cannot be looked up)"] if commits is None else [])
        return dict(out, value=False, commit=None, at=None, rule="H3",
                    how="the bridge ledger has no record of accepting a hand-over" +
                        ("; " + "; ".join(notes) if notes else ""))
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


def md_decisions(text, path, bot_users, lines=None):
    """decisions.md rows; `lines` (a split): only the rows on these line numbers (the run's own)."""
    out = []
    for n, row in decision_rows(text or ""):
        did, dec = row.get("id", ""), row.get("decision", "")
        if not did or not dec or (lines is not None and n not in lines):
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


def assumptions(text, path, at, at_src, lines=None):
    out, on = [], False
    for n, line in enumerate((text or "").split("\n"), 1):
        if line.startswith("## "):
            on = line[3:].strip().lower().startswith("assumption")
            continue
        m = ASSUMPTION_RE.match(line) if on and (lines is None or n in lines) else None
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

def open_ledger(path):
    """The bridge ledger (sqlite), opened read-only. BrainError when it is not there, is a directory, or is not a
    sqlite database (sqlite only finds that out at the first query: one is made here)."""
    if not os.path.exists(path):
        raise BrainError("bridge ledger %s not found" % path)
    if os.path.isdir(path):
        raise BrainError("bridge ledger %s is a directory: give the ledger file itself (the bridge's ledger.sqlite)"
                         % path)
    db = None
    try:
        db = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(os.path.abspath(path)), uri=True)
        db.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as e:
        if db is not None:
            db.close()
        raise BrainError("bridge ledger %s is not a sqlite database (%s)" % (path, e))
    db.row_factory = sqlite3.Row
    return db


def ledger_rows(path, key, shas=()):
    """The bridge ledger's rows for this requirement: its runs (a run id reserved while a hand-over is signed,
    state 'signing', is not a run yet), the hand-over checks of their MRs and of the commits in `shas` (the run's
    `brain handoff` commits: a refused hand-over has no run), and the runs' decisions."""
    db = open_ledger(path)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        runs = [dict(r) for r in db.execute(
            "SELECT run_id, project, iid, sha, voucher_id, state, created, updated FROM runs WHERE req=? AND "
            "state != ? ORDER BY created, run_id", (key, RESERVED))] if "runs" in tables else []
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


def _commit_of(sha):
    """The commit of a ledger sha ("<commit>#<label event>" for a hand-over row), or None when it is not one."""
    s = str(sha or "").split("#", 1)[0]
    return s if HEX_RE.match(s) else None


def scope_runs(repo, rp, branch, led, vouchers):
    """Rule H0: which of the requirement's bridge runs belong to the branch read here (run.json's `branch`). A
    requirement can be run again on a new branch (its first MR closed): each branch has its own hand-over, questions,
    answers, acceptance and KiroCrew acts, and the old run's are never this one's. A run's branch is the one its
    hand-over voucher names; else the one runs/<KEY>/run.json names at the commit the bridge accepted (the run's
    sha, or its hand-over row's); else the branch of another run of the same MR (an MR has one source branch). A
    hand-over row without a run (refused, being checked) goes by its commit's run.json, else by its MR. A run whose
    branch cannot be told counts as this branch's.
    -> (ledger rows, vouchers, others): `led` (None stays None) and `vouchers` with only this branch's runs, and
    {run_id: {"run_id", "branch", "state", "src"}} of the runs of other branches."""
    if not branch:
        return led, vouchers, {}
    runs, hos = (led or {}).get("runs", []), (led or {}).get("handoffs", [])
    named = {}
    for v in vouchers:
        if v.get("kind") == "handoff" and isinstance(v.get("run_id"), str) and isinstance(v.get("branch"), str) \
                and v["branch"]:
            named.setdefault(v["run_id"], (v["branch"], "hand-over voucher %s" % v.get("_src")))
    shas = sorted({c for c in [_commit_of(r.get("sha")) for r in runs] + [_commit_of(h.get("sha")) for h in hos] if c})
    at = {s: _branch_in(text) for s, text in zip(shas, _blobs(repo, ["%s:%s" % (s, rp) for s in shas]))}
    for rows, table in ((runs, "runs"), (hos, "handoffs")):
        for r in rows:
            c = _commit_of(r.get("sha"))
            if r.get("run_id") and at.get(c):
                named.setdefault(r["run_id"], (at[c], "%s at %s (bridge ledger %s)" % (rp, c[:12], table)))

    def mr_key(project, iid):
        return str(project), str(iid)
    mrs = {}
    for r in runs:
        if r["run_id"] in named:
            mrs.setdefault(mr_key(r.get("project"), r.get("iid")), named[r["run_id"]])
    for v in vouchers:
        if v.get("kind") == "handoff" and v.get("run_id") in named and v.get("mr_iid") is not None:
            mrs.setdefault(mr_key(v.get("project"), v.get("mr_iid")), named[v["run_id"]])
    for r in runs:
        b = mrs.get(mr_key(r.get("project"), r.get("iid")))
        if r["run_id"] not in named and b:
            named[r["run_id"]] = (b[0], "another run of the same MR (%s)" % b[1])
    other = {rid: b for rid, b in named.items() if b[0] != branch}
    if not other:
        return led, vouchers, {}

    def foreign(h):
        if h.get("run_id") in named:
            return h["run_id"] in other
        b = at.get(_commit_of(h.get("sha"))) or (mrs.get(mr_key(h.get("project"), h.get("iid"))) or (None,))[0]
        return bool(b) and b != branch
    state = {r["run_id"]: r.get("state") for r in runs}
    others = {rid: {"run_id": rid, "branch": b, "state": state.get(rid), "src": src}
              for rid, (b, src) in sorted(other.items())}
    if led is not None:
        led = dict(led, runs=[r for r in runs if r["run_id"] not in other],
                   handoffs=[h for h in hos if not foreign(h)],
                   decisions=[d for d in led.get("decisions", []) if d.get("run_id") not in other])
    return led, [v for v in vouchers if v.get("run_id") not in other], others


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
    """(the KiroCrew app's record(s) for this requirement or None, why there are none, the key of each record: a
    run id, the requirement key, or None for an unkeyed one). A number or one record is this requirement's (brain
    eval passes one only when there is one run). Keyed records count when the key is the requirement key, a run id
    the ledger / vouchers know, or RUN-<KEY>-<n> (a run id names its requirement)."""
    shape = credits_shape(credits)
    if shape == "none":
        return None, NOT_PASSED, []
    if shape == "bad":
        return None, "credits passed in, but not a number or a JSON object", []
    if shape == "one":
        return [credits if isinstance(credits, dict) else {"credits": credits}], None, [None]
    pool = credits.get("runs") if isinstance(credits.get("runs"), dict) else credits
    rx = re.compile(r"^RUN-%s-[1-9][0-9]*$" % re.escape(key))
    keys = [k for k in sorted(pool) if (k == key or k in run_ids or rx.match(k)) and
            (isinstance(pool[k], dict) or _is_num(pool[k]))]
    recs = [pool[k] if isinstance(pool[k], dict) else {"credits": pool[k]} for k in keys]
    return (recs, None, keys) if recs else (None, "credits passed in, but none for %s or its runs" % key, [])


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


# the bridge has a person's answer to the question: delivered to the run, or on its way (being signed and sent; sent
# and not known to have landed). The run waits on no one: on the runner (or the bridge) to go on
ANSWERED = ("answered", "delivered", "delivery_unknown")
ANSWERED_LABEL = {"answered": "桥上收到了、还没送到 run", "delivered": "已经送到 run", "delivery_unknown": "送达不明"}
# ... and what holds the run up then (the review queue's stuck reason)
ANSWERED_NEXT = {"answered": "桥在签名或发送（看桥的日志）",
                 "delivered": "run 没接着做（park.json 还在等：去 KiroCrew 看 slot）",
                 "delivery_unknown": "桥不知道送到没有（看桥的日志和 KiroCrew 的 slot）"}


def _on_this_park(d, first, row, log, log_shas):
    """Was bridge decision `d` asked about the park.json question `row` (at the tip), whose latest asking starts at
    park row `first`? The bridge asks at the MR head: a commit that has `first`'s commit in its history. A decision
    whose commit is not among the run's commits is placed by time; with no park history, by its Lean request (or
    its kind)."""
    if first and first.get("commit") and d.get("sha") in log_shas:
        return first["commit"] in _ancestry(log, d["sha"])
    if first and first.get("at"):
        return bool(d.get("asked_at")) and d["asked_at"] >= first["at"]
    return d.get("lean_request") == row["lean_request"] if row["lean_request"] else d.get("kind") == row["kind"]


def parked(repo, cfg, key, tree, hist, decisions):
    """(waiting_on, answered) of an unmerged run, from its park.json now (at the run's tip) and the bridge's questions.
    waiting_on: what it waits on a person for: park.json waiting with no answer yet, or an open bridge question;
    {"kind", "ask", "lean_request", "since", "decision", "src"} or None.
    answered: park.json is still waiting, but the bridge already has the answer to that question (decision state
    answered / delivered / delivery_unknown, rule E2): the run waits on no one, the runner did not go on (or the
    bridge did not deliver yet); {"decision", "state", "choice", "at", "kind", "ask", "lean_request", "since",
    "src"} or None. At most one of the two is set."""
    pp = "%s/%s/park.json" % (cfg["runs_dir"], key)
    text = _show(repo, hist["tip"], pp) if hist.get("tip") else tree.read(pp)
    row = _park_row(text, "%s@%s" % (pp, hist["tip"][:12]) if hist.get("tip") else "%s (%s)" % (pp, tree.label),
                    None)
    bridge = [d for d in decisions if d["source"] == "bridge"]
    open_q = [d for d in bridge if not d.get("choice") and d.get("state") in OPEN_QUESTION]
    if row and row["status"] == "waiting":
        same = (row["kind"], row["lean_request"], row["question_sha256"])
        first = None    # the latest asking of this question: the park rows at the end of the history that ask it
        for q in reversed(hist["park"] or []):
            if (q["kind"], q["lean_request"], q["question_sha256"]) != same or q["status"] != "waiting":
                break
            first = q
        log = hist.get("_log") or []
        log_shas = {c["sha"] for c in log}
        mine = [d for d in bridge if _on_this_park(d, first, row, log, log_shas)]
        last = mine[-1] if mine else None
        if last and last.get("choice") and last.get("state") in ANSWERED:
            return None, {"decision": last["id"], "state": last["state"], "choice": last["choice"],
                          "at": last.get("at"), "kind": row["kind"], "ask": row["ask"],
                          "lean_request": row["lean_request"], "since": first["at"] if first else None,
                          "src": [row["src"]] + list(last.get("src") or [])}
        asked = [d for d in mine if d in open_q] or open_q
        return {"kind": row["kind"], "ask": row["ask"], "lean_request": row["lean_request"],
                "since": first["at"] if first else None,
                "decision": asked[-1]["id"] if asked else None, "src": row["src"]}, None
    if open_q:
        d = open_q[-1]
        return {"kind": d.get("kind"), "ask": None, "lean_request": d.get("lean_request"), "since": d.get("asked_at"),
                "decision": d["id"], "src": "bridge ledger decisions"}, None
    return None, None


def waiting_on(repo, cfg, key, tree, hist, decisions):
    """What an unmerged run waits on a person for (parked(): None when the bridge already has the answer)."""
    return parked(repo, cfg, key, tree, hist, decisions)[0]


# ---------- a split: the parent and its children (brainkit/split.py) ----------

CLONE_RE = re.compile(r"^[0-9a-f]{6}$")                    # runrec: lean_clone, the random part of a Lean clone id
RUN_JSON_RE = r"^%s/([A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6})/run\.json$"
BLAME_RE = re.compile(r"^([0-9a-f]{40}) [0-9]+ ([0-9]+)(?: [0-9]+)?$")
NUMBERS = ("stages_done", "verify_rounds", "verify_failed", "commits_bot", "commits_person", "blocked",
           "human_at_judgment", "human_outside_judgment", "decisions", "questions", "credits")
NUMBER_SRC = {
    "stages_done": "stages: stages whose last Lean record is STAGE_COMPLETED, of the run's own Lean events",
    "verify_rounds": "checks.verify: the run's own Lean VERIFY events",
    "verify_failed": "checks.verify_failed: those not ok",
    "commits_bot": "actions.commits by the bot (kit.json bot_emails); null when the run's commits are not here",
    "commits_person": "actions.commits by a person",
    "blocked": "actions.blocked: the run's own Lean BLOCKED events",
    "human_at_judgment": "humans with judgment_point true (Lean approvals, rejections, refused approvals; bridge "
                         "answers)",
    "human_outside_judgment": "humans with judgment_point false (pushes after hand-over, allow test change, KiroCrew "
                              "manual acts)",
    "decisions": "decisions: decisions.md rows and assumptions A-n its commits wrote (git blame), bridge decisions of "
                 "its runs",
    "questions": "questions asked: distinct park.json questions or bridge questions, the larger (as brain eval)",
    "credits": "cost.credits, passed in by the KiroCrew app; null when not known",
}
_CACHE = {}   # content read at a commit (immutable): (what, repo, ...) -> value


def _cached(k, make):
    if k not in _CACHE:
        if len(_CACHE) > 4096:
            _CACHE.clear()
        _CACHE[k] = make()
    return _CACHE[k]


def _key_order(k):
    m = re.match(r"^(.*)-([0-9]+)$", str(k))
    return (m.group(1), int(m.group(2))) if m else (str(k), 0)


def child_of(rec, key):
    """The split fields of a child's run.json {parent, unit, target_branch, lean_team, lean_clone, built_on} (a
    field that is not well formed: None), or None when the run is not a child of a split."""
    p = rec.get("parent") if isinstance(rec, dict) else None
    if not (isinstance(p, str) and JIRA_RE.match(p) and p != key):
        return None
    out = {"parent": p}
    for f, ok in (("unit", lambda v: True), ("target_branch", BRANCH_RE.match), ("lean_team", lambda v: True),
                  ("lean_clone", CLONE_RE.match), ("built_on", JIRA_RE.match)):
        v = rec.get(f)
        out[f] = v if isinstance(v, str) and v and ok(v) else None
    return out


def _parse_runs(cfg, paths, texts) -> dict:
    rx = re.compile(RUN_JSON_RE % re.escape(cfg["runs_dir"]))
    out = {}
    for p, text in zip(paths, texts):
        try:
            rec = json.loads(text) if text is not None else None
        except ValueError:
            rec = None
        out[rx.match(p).group(1)] = rec if isinstance(rec, dict) else None
    return out


def runs_at(repo, cfg, sha) -> dict:
    """{key: run.json record, or None when it is not a JSON object} of runs/<KEY>/run.json in commit `sha`."""
    if not sha:
        return {}

    def make():
        rx = re.compile(RUN_JSON_RE % re.escape(cfg["runs_dir"]))
        p = git(repo, "ls-tree", "-r", "-z", "--name-only", sha, "--", cfg["runs_dir"], check=False)
        paths = sorted(x for x in p.stdout.split("\0") if rx.match(x)) if p.returncode == 0 else []
        return _parse_runs(cfg, paths, _blobs(repo, ["%s:%s" % (sha, x) for x in paths]))
    return _cached(("runs", repo, sha, cfg["runs_dir"]), make)


def runs_in(repo, cfg, tree) -> dict:
    """runs_at for the tree read (the working tree is read each time: it changes)."""
    if tree.ref:
        return runs_at(repo, cfg, _commit(repo, tree.ref))
    rx = re.compile(RUN_JSON_RE % re.escape(cfg["runs_dir"]))
    paths = [p for p in tree.files(cfg["runs_dir"]) if rx.match(p)]
    return _parse_runs(cfg, paths, [tree.read(p) for p in paths])


def branch_runs(repo, cfg, have=(), refs=None) -> dict:
    """{key: (ref to read, run.json record or None)} for the run branches here (<prefix>/<owner>/<KEY>-<slug>, local
    and origin's) of the requirements not in `have`: of each key, the branch whose tip has runs/<KEY>/run.json
    (naming that branch) and was committed last; of that branch's copies, the freshest (origin's when they
    diverged). brain eval reads the unmerged runs from these (evaluate.run_branches)."""
    refs = _refs(repo) if refs is None else refs
    prefix, by = cfg["branch_prefix"], {}
    for r, sha in sorted(refs.items()):
        space = next((x for x in ("refs/remotes/origin/", "refs/heads/") if r.startswith(x + prefix + "/")), None)
        b = parse_branch(r[len(space):], prefix) if space else None
        if b and b["key"] not in have and JIRA_RE.match(b["key"]):
            by.setdefault(b["key"], {}).setdefault(r[len(space):], []).append((r, sha))
    state = tuple((k, n, tuple(c)) for k, bs in sorted(by.items()) for n, c in sorted(bs.items()))

    def make():
        cands = []
        for key, branches in sorted(by.items()):
            for name, copies in sorted(branches.items()):
                copies = sorted(copies, key=lambda x: not x[0].startswith("refs/remotes/"))
                tip, ref, _ = freshest(repo, copies)
                cands.append((key, name, tip, ref))
        texts = _blobs(repo, ["%s:%s/%s/run.json" % (tip, cfg["runs_dir"], key) for key, _, tip, _ in cands])
        found = {}
        for (key, name, tip, ref), text in zip(cands, texts):
            if text is None:
                continue
            try:
                rec = json.loads(text)
            except ValueError:
                rec = None                    # read anyway: the trace says why it cannot be used (skipped)
            if isinstance(rec, dict) and isinstance(rec.get("branch"), str) and rec["branch"] != name:
                continue
            found.setdefault(key, []).append((tip, ref, rec if isinstance(rec, dict) else None))
        out = {}
        for key, rows in found.items():
            best = None
            for tip, ref, rec in rows:        # the branch committed last (the first one of those committed together)
                when = (_cdate(repo, tip) or "") if len(rows) > 1 else ""
                if best is None or when > best[0]:
                    best = (when, ref, rec)
            out[key] = (best[1], best[2])
        return out
    return _cached(("branches", repo, cfg["runs_dir"], prefix, frozenset(have), state), make)


def children_of(repo, cfg, key, tree, refs) -> list:
    """[(child key, ref to read it at, run.json)] of the runs that name `key` as their parent, in key order: those in
    the tree read (merged into the parent's branch; or with it into the default branch), read there (tree.ref); and
    those on their own run branches here (local or origin's), not merged yet, read from their branch. A child whose
    run.json is in the tree read but that has not merged (a sibling started from its branch and carried it in) is read
    from its own branch when it has one."""
    here = runs_in(repo, cfg, tree)
    own = {k: (r, rec) for k, (r, rec) in branch_runs(repo, cfg, (), refs).items()
           if child_of(rec, k) and rec["parent"] == key}
    out = {}
    for k, rec in here.items():
        cf = child_of(rec, k)
        if k == key or not cf or cf["parent"] != key:
            continue
        carried = k in own and not merged_into_parent(repo, cfg, k, rec, cf, refs)["value"]
        out[k] = own[k] if carried else (tree.ref, rec)
    for k, v in own.items():
        out.setdefault(k, v)
    return [(k,) + out[k] for k in sorted(out, key=_key_order)]


def _target(repo, cfg, tree, cf, ref, refs):
    """(parent's run branch name, ref or commit to read it at or None, how it was found) for a child, or None when
    its run.json names no target_branch."""
    tb = cf["target_branch"]
    if not tb:
        return None
    r = default_ref(repo, dict(cfg, default_branch=tb), ref, refs)
    if r:
        return tb, r, "the parent's run branch %s" % r
    # gone (deleted after it merged): the parent's tip as it merged into the default branch
    prec = runs_in(repo, cfg, tree).get(cf["parent"])
    dref = default_ref(repo, cfg, ref, refs)
    pb = prec.get("branch") if isinstance(prec, dict) and isinstance(prec.get("branch"), str) else None
    if pb == tb and dref:
        pi = prec.get("intent") if isinstance(prec.get("intent"), str) else None
        land = landing(repo, cfg, dref, cf["parent"], pi, pb, copies_of(repo, pb, None, refs), refs)
        if land and land.get("tip"):
            return tb, land["tip"], "%s as it merged into %s (%s, %s): the branch is gone" % (
                tb, dref, land["merge"]["commit"][:12], land["merge"]["how"])
    return tb, None, "the parent's run branch %s is not here or on origin, and not merged into %s" % (
        tb, dref or cfg["default_branch"])


def tips_of(cfg, refs) -> dict:
    """{key: {run branch name: tip sha}} of the run branches in `refs` (origin's tip when both have one): what
    intents.run_branches gives, as last fetched (trace does not ask origin)."""
    prefix, out = cfg["branch_prefix"], {}
    for space in ("refs/heads/", "refs/remotes/origin/"):            # origin's last: its tip wins
        for r, sha in sorted(refs.items()):
            b = parse_branch(r[len(space):], prefix) if r.startswith(space + prefix + "/") else None
            if b:
                out.setdefault(b["key"], {})[r[len(space):]] = sha
    return out


def merged_at(repo, cfg, key, rec, sha, refs) -> bool:
    """intents.child_merged for the child `key` (its run.json `rec`) at commit `sha`."""
    return intents.child_merged(repo, cfg, {"key": key, "parent": rec.get("parent"), "owner": rec.get("owner")}, sha,
                                tips_of(cfg, refs))


def merged_into_parent(repo, cfg, key, rec, cf, refs) -> dict:
    """Has a child merged into its parent's run branch (intents.child_merged: its runs/<KEY>/run.json there has the
    record of its `brain handoff`, "mr", and its owner's run branch, while one is here, has its tip there; its
    run.json alone proves nothing, a sibling that started from its branch carries it too). Read on the parent's
    branch (origin's copy as last fetched when there is one, else the local one: trace does not fetch), then on the
    default branch (it got there with its parent). {"value", "ref", "src"}."""
    rp = "%s/%s/run.json" % (cfg["runs_dir"], key)
    tb = cf.get("target_branch")
    if not tb:
        return {"value": False, "ref": None, "src": "%s names no target_branch" % rp}
    rule = "intents.child_merged: %s there with its `brain handoff` record (mr), and %s's run branch, while one is " \
           "here, with its tip there" % (rp, rec.get("owner") or "its owner")
    tref = next((r for r in ("refs/remotes/origin/" + tb, "refs/heads/" + tb) if refs.get(r)), None)
    d = default_ref(repo, cfg, None, refs)
    for r in (tref, d):
        sha = (refs.get(r) or _commit(repo, r)) if r else None
        if sha and merged_at(repo, cfg, key, rec, sha, refs):
            return {"value": True, "ref": r, "src": "%s (%s)%s: %s" % (
                r, sha[:12], ", the parent's run branch" if r == tref else ", the default branch: it merged with its "
                "parent" if not tref else "", rule)}
    if not tref:
        return {"value": False, "ref": None, "src": "%s is not here or on origin (as last fetched), and %s has not "
                "merged into %s: %s" % (tb, key, d or cfg["default_branch"], rule)}
    text = _show(repo, refs[tref], rp)
    try:
        there = json.loads(text) if text is not None else None
    except ValueError:
        there = None
    why = "%s is not there" % rp if text is None else "%s there has no `brain handoff` record (mr): a sibling that " \
        "started from or merged its branch carried it in" % rp if not (isinstance(there, dict) and
                                                                        isinstance(there.get("mr"), dict)) else \
        "its run branch has commits that are not there (after its hand-off)"
    return {"value": False, "ref": tref, "src": "%s (%s), the parent's run branch: %s. %s" % (
        tref, refs[tref][:12], why, rule)}


def _base_of(repo, tip, rp):
    """The commit a run branch started from: the first parent of the oldest commit on its first-parent line that
    added runs/<KEY>/run.json (`brain start`), or None."""
    lines = [x for x in (_g(repo, "log", "--first-parent", "--diff-filter=A", "--format=%P", tip, "--", rp)
                         or "").split("\n") if x.strip()]
    return lines[-1].split()[0] if lines else None


def blame(repo, tree, path):
    """{line number: commit sha} of a file as the tree read has it (git blame; a working-tree line not committed
    yet: ZERO_SHA), or None when git cannot blame it (the file is not committed)."""
    def make():
        out = _raw(repo, "blame", "--porcelain", *([tree.ref] if tree.ref else []), "--", path)
        if out is None:
            return None
        rows = {}
        for line in out.split("\n"):
            m = BLAME_RE.match(line)
            if m:
                rows[int(m.group(2))] = m.group(1)
        return rows
    sha = _commit(repo, tree.ref) if tree.ref else None
    return _cached(("blame", repo, sha, path), make) if sha else make()


def _head_branch(repo):
    out = _g(repo, "symbolic-ref", "-q", "--short", "HEAD")
    return out.strip() if out else None


def owned_lines(repo, tree, path, mine, theirs, branch, their_branches):
    """The line numbers of a shared Lean page (decisions.md, requirements.md) that are the run's: written by one of
    its commits (`mine`, a child) or by none of its children's (`theirs`, a parent; mine None). A line not committed
    yet belongs to the run whose branch is checked out (the parent when it is neither). None: every line (git cannot
    blame the file, and the run is a parent); an empty set: none (a child)."""
    rows = blame(repo, tree, path)
    if rows is None:
        return None if mine is None else set()
    head = _head_branch(repo) if tree.ref is None and ZERO_SHA in rows.values() else None
    out = set()
    for n, sha in rows.items():
        if sha == ZERO_SHA:
            ok = head == branch if mine is not None else head not in their_branches
        else:
            ok = sha in mine if mine is not None else sha not in theirs
        if ok:
            out.add(n)
    return out


def _only(text, lines):
    """The text with the lines not in `lines` blanked (line numbers kept)."""
    if text is None or lines is None:
        return text
    return "\n".join(x if i in lines else "" for i, x in enumerate(text.split("\n"), 1))


def questions_asked(t) -> int:
    """How many questions the run parked on: distinct park.json questions, or bridge questions (the larger)."""
    park = {(q.get("kind"), q.get("lean_request"), q.get("question_sha256")) for q in t["actions"]["questions"] or []
            if q.get("kind") or q.get("question_sha256")}
    asked = {d["id"] for d in list(t["decisions"]) + list(t.get("oracle") or []) if d.get("source") == "bridge"}
    return max(len(park), len(asked))


def numbers(t) -> dict:
    """A run's numbers for a split's roll-up (NUMBERS; where each comes from: NUMBER_SRC)."""
    c, commits = t["checks"], t["actions"]["commits"]
    jp = [h["judgment_point"] for h in t["humans"]]
    return {"stages_done": sum(1 for s in t["stages"] if s["state"] == "done"),
            "verify_rounds": len(c["verify"]) if c else None, "verify_failed": c["verify_failed"] if c else None,
            "commits_bot": None if commits is None else sum(1 for x in commits if x["by"] == "bot"),
            "commits_person": None if commits is None else sum(1 for x in commits if x["by"] != "bot"),
            "blocked": len(t["actions"]["blocked"]), "human_at_judgment": jp.count(True),
            "human_outside_judgment": jp.count(False), "decisions": len(t["decisions"]),
            "questions": questions_asked(t), "credits": t["cost"]["credits"]}


def rollup(rows) -> tuple:
    """(sums, [what is not known: "KEY field"]): each number summed over the runs that have it; None when none has."""
    total, unknown = {}, []
    for f in NUMBERS:
        vals = [n[f] for _, n in rows if n is not None and n[f] is not None]
        unknown += ["%s %s" % (k, f) for k, n in rows if n is None or n[f] is None]
        s = sum(vals) if vals else None
        total[f] = round(s, 4) if isinstance(s, float) else s
    return total, sorted(set(unknown), key=lambda x: (_key_order(x.split(" ")[0]), x))


def _team_of(e):
    a = e.get("actor")
    return a.get("team") if isinstance(a, dict) else None


def _claims(cf):
    """A test (file path, event) -> is it this child's Lean record; and what it is, for src."""
    if cf["lean_clone"]:
        end = "-%s.jsonl" % cf["lean_clone"]
        return (lambda path, e: os.path.basename(path).endswith(end)), "its clone's ledger file(s) (*-%s.jsonl, " \
            "run.json lean_clone)" % cf["lean_clone"]
    team = cf["lean_team"] or (re.sub(r"[^A-Za-z0-9_]", "_", cf["unit"]) if cf["unit"] else None)
    if team:
        return (lambda path, e: _team_of(e) == team), "Lean events of team %s (run.json has no lean_clone)" % team
    return (lambda path, e: False), "none: run.json has no lean_clone, lean_team or unit"


def _split_events(tree, events, chains, mine=None, theirs=()):
    """(events, chains) a split run keeps: a child's (`mine`: its test), or a parent's (all but `theirs`: tests)."""
    labels = audit_labels([p for p in tree.files(lean.DOCS + "/audit") if p.endswith(".jsonl")])

    def keep(path, e):
        return mine(path, e) if mine else not any(t(path, e) for t in theirs)
    events = [e for e in events if keep(e["_file"], e)]
    files = {e["_file"] for e in events}
    # a ledger file stays when the run keeps one of its records, or (no record of this intent in it) when the file
    # itself is the run's
    chains = {labels[p]: chains[labels[p]] for p in sorted(labels) if labels[p] in chains and
              (p in files or keep(p, {}))}
    return events, chains


def _member_line(t):
    return "trace of %s read from %s" % (t["key"], t["ref"])


# ---------- the trace ----------

def build(repo, cfg, key, ref=None, ledger_path=None, vouchers_dir=None, credits=None, as_of=None,
          allowed_signers=None, _usage=None, _cache=None) -> dict:
    """The trace of one requirement key. `credits`: a number, a KiroCrew record {credits|total, turns, manual,
    context_used, context_window}, or {"runs": {RUN-id or KEY: record}}. `as_of`: ISO time the 14-day window is
    judged at (default now; recorded in the trace). `_cache` (brain eval): {(key, ref): trace} of the traces made
    with these same arguments; a parent's children are taken from it and every trace made is put in it."""
    if not JIRA_RE.match(key or ""):
        raise BrainError("%r is not a requirement key like PAY-1" % key)
    if _cache is not None and (key, ref) in _cache:
        return _cache[(key, ref)]
    t = _build(repo, cfg, key, ref, ledger_path, vouchers_dir, credits, as_of, allowed_signers, _usage, _cache)
    if _cache is not None:
        _cache[(key, ref)] = t
    return t


def _build(repo, cfg, key, ref, ledger_path, vouchers_dir, credits, as_of, allowed_signers, _usage, _cache):
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

    refs = _refs(repo)
    # ----- a split (brainkit/split.py): a child keeps its own records, a parent leaves its children's out -----
    cf = child_of(rec, key)
    kids = children_of(repo, cfg, key, tree, refs) if cf is None else []
    kid_tests = [_claims(child_of(r, k))[0] for k, _, r in kids]
    events, chains = lean_events(tree, intent) if intent else ([], {})
    if cf:
        test, events_src = _claims(cf)
        events, chains = _split_events(tree, events, chains, mine=test)
    elif kids:
        events, chains = _split_events(tree, events, chains, theirs=kid_tests)
    if not chains:
        missing.append("Lean audit (no aidlc-docs/audit/*.jsonl in %s)" % tree.label if not cf else
                       "Lean audit (%s: not in %s)" % (events_src, tree.label))
    docs = "%s/%s" % (lean.DOCS, intent) if intent else None
    dec_path = docs + "/decisions.md" if docs else None
    dec_text = tree.read(dec_path) if dec_path else None
    req_path = docs + "/inception/requirements/requirements.md" if docs else None

    target = _target(repo, cfg, tree, cf, ref, refs) if cf else None
    hist = git_history(repo, cfg, rec, ref, key, refs, target=target[:2] if target else None) if branch else {
        "tip": None, "tip_src": None, "tip_note": None, "default_ref": default_ref(repo, cfg, ref, refs), "merge": None,
        "commits": None, "range": None, "handover_commit": None, "park": None, "requests": [], "_log": [], "_rp": rp}
    stale = fresher_than_worktree(repo, rp, hist) if ref is None else None
    if stale:      # the owner's old checkout of the run branch: read what any other clone reads (docs/eval.md §2)
        t = _build(repo, cfg, key, stale["read"], ledger_path, vouchers_dir, credits, as_of, allowed_signers, None,
                   _cache)
        t["src"]["working_tree"] = stale
        return t
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
    # rule H0: the runs of this key on other branches (a re-run on a new branch) are not this branch's: from here on
    # `led` and `vouchers` hold this branch's runs only (run_ids keeps them all; other_runs lists the others)
    led, vouchers, others = scope_runs(repo, rp, branch, led, vouchers)

    # ----- the hand-over (rules H1-H4): which `brain handoff` the runner got, if any -----
    hand = handover(repo, hist, handoff, led, vouchers)
    handed, handoff_at = hand["value"], hand["at"]
    hist["handover_commit"] = hand["commit"]
    if hist["commits"] is not None:
        log_shas = {c["sha"] for c in hist["_log"]}
        before = _ancestry(hist["_log"], hand["commit"]) if handed and hand["commit"] in log_shas else None
        for c in hist["commits"]:
            if not handed:
                c["after_handover"] = False
            elif before is not None:
                c["after_handover"] = c["sha"] not in before
            else:   # the hand-over commit is not among the commits read: by time (author time vs the request's)
                c["after_handover"] = c["at"] > handoff_at if c["at"] and handoff_at else None

    # ----- a split parent: its children's own traces (their numbers; their commits are not the parent's) -----
    u = _usage if _usage is not None else usage.collect(repo, cfg, ref=ref)
    members, theirs = [], set()
    for k, kref, krec in kids:
        kt, why = None, None
        try:
            kt = build(repo, cfg, k, ref=kref, ledger_path=ledger_path, vouchers_dir=vouchers_dir,
                       credits=None if credits_shape(credits) == "one" else credits, as_of=as_of,
                       allowed_signers=allowed_signers, _usage=u if kref == ref else None, _cache=_cache)
            theirs |= {c["sha"] for c in kt["actions"]["commits"] or []}
        except BrainError as e:
            why = str(e)
        except (ValueError, TypeError, KeyError, AttributeError) as e:   # as brain eval: a record nobody foresaw
            why = "%s while reading it: %s" % (type(e).__name__, e)
        members.append((k, kref, krec, kt, why))
    if kids and hist["commits"] is not None:
        hist["commits"] = [c for c in hist["commits"] if c["sha"] not in theirs]
    # decisions.md and requirements.md are the intent's: the lines this run's commits wrote
    lines = {}
    if cf or kids:
        mine = {c["sha"] for c in hist["commits"] or []} if cf else None
        their_branches = {r.get("branch") for _, _, r in kids if isinstance(r.get("branch"), str)}
        for path in (dec_path, req_path):
            if path and tree.read(path) is not None:
                lines[path] = owned_lines(repo, tree, path, mine, theirs, branch, their_branches)

    # ----- decisions -----
    decisions = md_decisions(dec_text, dec_path, cfg.get("bot_users", []), lines.get(dec_path)) \
        if dec_text is not None else []
    if dec_text is None and docs:
        missing.append("%s (not in %s)" % (dec_path, tree.label))
    req_done = [e for e in events if e.get("event") == "STAGE_COMPLETED" and _data(e).get("stage") == "requirements"]
    a_at, a_src = ((req_done[-1]["_at"], req_done[-1]["_src"]) if req_done else
                   (_utc(rec.get("created_at")), run_src + " created_at"))
    decisions += assumptions(tree.read(req_path), req_path, a_at, a_src, lines.get(req_path)) if req_path else []
    bridge = {}
    for d in (led or {}).get("decisions", []):
        # refused: the signer would not sign the answer, so it never reached the run: no answer (no person's
        # decision, no acceptance); the run still waits, and the review queue says why (question_problem)
        answer = d["answer"] if d["state"] != "refused" else None
        bridge[(d["run_id"], d["id"])] = {
            "id": d["id"], "source": "bridge", "run_id": d["run_id"], "by": "bridge", "kind": d["kind"],
            "lean_request": d["lean_request"], "state": d["state"], "choice": answer,
            "who": d["answerer"] if d["answerer"] and USER_RE.match(str(d["answerer"])) else None,
            "voucher_id": d["voucher_id"], "at": _utc(d["updated"]) if answer else None,
            "asked_at": _utc(d["created"]), "sha": _commit_of(d.get("sha")), "eval_oracle": False,
            "signature": None, "text": None, "cites": [], "rejected_items": None, "recorded_in": None,
            "src": ["bridge ledger decisions"]}
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
                                    "sha": None, "eval_oracle": False, "signature": None, "text": None, "cites": [],
                                    "rejected_items": None, "recorded_in": None, "src": []})
        items = v.get("rejected_items")   # batch review: the items of review_items the answer rejected
        row.update(kind=_s(v.get("decision_kind")) or row["kind"],
                   lean_request=_s(v.get("lean_request")) or row["lean_request"],
                   choice=_s(v.get("choice")), voucher_id=_s(v.get("voucher_id")),
                   sha=row["sha"] or _commit_of(v.get("sha")),
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
        actor = pseudonym((e.get("actor") or {}).get("git_email") if isinstance(e.get("actor"), dict) else None)
        # a pseudonym in the words ("... (via gitlab P-xxxx V-...)") is only a claim: it is the act's `who` when
        # nothing could check it (no vouchers, no ledger read); when they were read and hold no such answer, the
        # act is the Lean record's own identity's
        who = actor if not ps or vouchers_dir or ledger_path else ps.group(0)
        kind = {"APPROVED": "approval", "REJECTED": "rejection", "APPROVAL_REFUSED": "approval_refused",
                "HUMAN_TURN": "allow_test_change"}[ev]
        humans.append({"at": e["_at"], "kind": kind,
                       "what": d.get("kind") if ev != "HUMAN_TURN" and isinstance(d.get("kind"), str) else None,
                       "choice": None, "who": who,
                       "channel": "bridge" if vid or chan.lower().startswith("gitlab") else "local",
                       "decision": None, "voucher_id": vid, "judgment_point": ev != "HUMAN_TURN",
                       "after_handover": None, "check": d.get("check") if ev == "APPROVAL_REFUSED" else None,
                       "src": [e["_src"]]})
    for c in hist["commits"] or []:
        # after_handover None: handed over, but neither the commit graph nor the times tell: kept, never dropped
        if c["by"] == "person" and c["after_handover"] is not False:
            humans.append({"at": c["at"], "kind": "push_after_handover", "what": "records" if c["records_only"]
                           else "code", "choice": None, "who": c["who"], "channel": "git", "decision": None,
                           "voucher_id": None, "judgment_point": False, "after_handover": c["after_handover"],
                           "sha": c["sha"], "src": [c["src"]]})
    crecs, credits_why, ckeys = _credits(credits, key, run_ids)
    for k, r in zip(ckeys, crecs or []):
        if k in others:          # a KiroCrew act on another branch's run (rule H0) is not this run's
            continue
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
    urow = next((r for r in u["runs"] if r["intent"] == intent and r["key"] == key), None)
    if urow is None and not (cf or kids):
        urow = next((r for r in u["runs"] if r["intent"] == intent), None)
    brain = {"brain_commit": rec.get("brain_commit") if isinstance(rec.get("brain_commit"), str) else None,
             "loaded": urow["loaded"] if urow else None,
             "cited": urow["cited"] if urow else [], "stale": urow["stale"] if urow else [],
             "unknown": urow["unknown"] if urow else [],
             "proposals": proposals_of(_only(dec_text, lines.get(dec_path)), dec_path) if dec_text is not None
             else None,
             "src": "brainkit.usage (%s): loaded = Brain at brain_commit; cited = ids in aidlc-docs/%s/*.md outside "
                    "brain-proposal blocks" % (u["ref"], intent)}
    if cf or kids:
        brain["src"] += " (a split: its own usage row, the unit pages construction/<unit>/ and construction/plans/" \
                        "<unit>-code-generation-plan.md the child's of that unit, the other pages the parent's; " \
                        "proposals: the decisions.md lines its commits wrote)"

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
    waiting, answered = (None, None) if merged else parked(repo, cfg, key, tree, hist, decisions)
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
        "waiting_on": waiting,
        "answered": answered,
    }

    if credits is None:
        missing.append("credits (not passed in)")
    elif not crecs:
        missing.append("credits (%s)" % credits_why)
    missing.append("pushes (git keeps no push log; person commits after hand-over stand in for them)")
    missing.append("CI pipelines (GitLab, not readable here)")
    t = {
        "schema": SCHEMA, "key": key, "as_of": iso(as_of_dt), "ref": tree.label,
        "intent": intent, "owner": owner, "branch": branch,
        "brain_commit": brain["brain_commit"], "run_ids": run_ids, "other_runs": list(others.values()),
        "src": {"key": "argument", "intent": run_src, "owner": run_src, "branch": run_src,
                "brain_commit": run_src, "run_ids": "bridge ledger runs + voucher files",
                "other_runs": "runs of this key on another branch (rule H0): not read into this trace",
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
    if cf:
        t["family"] = _child_family(repo, cfg, key, rec, cf, refs, target, hist, events_src, lines, dec_path,
                                    req_path, t)
    elif kids:
        t["family"] = _parent_family(t, members, kid_tests, lines, dec_path, req_path, hist)
    return t


def _shared_src(lines, dec_path, req_path, whose):
    out = []
    for path in (dec_path, req_path):
        if path in lines:
            n = lines[path]
            out.append("%s: %s (git blame)" % (path, "every line (git cannot blame it)" if n is None else
                                                "%d line(s) %s" % (len(n), whose)))
    return "; ".join(out) or "no decisions.md or requirements.md in the tree read"


def _child_family(repo, cfg, key, rec, cf, refs, target, hist, events_src, lines, dec_path, req_path, t) -> dict:
    into = hist.get("into_parent")
    fam = {"role": "child", "parent": cf["parent"], "unit": cf["unit"], "target_branch": cf["target_branch"],
           "merged_into_parent": merged_into_parent(repo, cfg, key, rec, cf, refs), "numbers": numbers(t),
           "src": {"events": "Lean audit: %s" % events_src,
                   "commits": "git log %s: judged against %s" % (hist["range"], target[2]) if target and
                   hist["range"] else "not here (%s)" % (target[2] if target else "run.json names no target_branch"),
                   "into_parent": "%s (%s)" % (into["how"], into["src"]) if into else None,
                   "decisions": _shared_src(lines, dec_path, req_path, "written by its commits"),
                   "numbers": NUMBER_SRC}}
    if cf["built_on"]:
        fam["built_on"] = cf["built_on"]
    return fam


def _parent_family(t, members, kid_tests, lines, dec_path, req_path, hist) -> dict:
    children, rows = [], [(t["key"], numbers(t))]
    acts = [(t["outcome"]["last_activity"], t["key"])]
    for k, kref, krec, kt, why in members:
        cf = child_of(krec, k)
        if kt is not None:
            m = (kt.get("family") or {}).get("merged_into_parent")
            n = numbers(kt)
            acts.append((kt["outcome"]["last_activity"], k))
        else:
            m, n = None, None
        children.append({"key": k, "unit": cf["unit"], "merged_into_parent": m or {
            "value": False, "ref": None, "src": "its trace was not read"}, "numbers": n,
            "src": _member_line(kt) if kt is not None else "trace of %s not read: %s" % (k, why)})
        rows.append((k, n))
    total, unknown = rollup(rows)
    la = max(((a["at"], a.get("src"), k) for a, k in acts if a and a.get("at")), key=lambda x: (x[0], x[2]),
             default=None)
    gone = [c["key"] for c in children if c["numbers"] is None or c["numbers"]["commits_bot"] is None]
    return {"role": "parent", "children": children,
            "rollup": {"own": rows[0][1], "total": total, "unknown": unknown,
                       "last_activity": {"at": la[0], "key": la[2], "src": la[1]} if la else
                       {"at": None, "key": None, "src": None}},
            "src": {"children": "run.json files naming %s as parent: in the tree read (%s), and on their own run "
                                "branches here (local or origin's), each read there" % (t["key"], t["ref"]),
                    "events": "Lean audit: the intent's events minus its children's (%d child ledger test(s): their "
                              "clone files, or their team)" % len(kid_tests),
                    "commits": "git log %s minus its children's commits%s" % (
                        hist["range"], " (not known for %s: theirs may be counted here)" % ", ".join(gone)
                        if gone else "") if hist["range"] else "not here",
                    "decisions": _shared_src(lines, dec_path, req_path, "not written by a child's commit"),
                    "total": "its own numbers plus each child's, summed where known; unknown lists what is not",
                    "numbers": NUMBER_SRC}}


# ---------- text ----------

def _nums(n) -> str:
    if n is None:
        return "读不到"

    def v(f):
        return "?" if n[f] is None else n[f]
    return "阶段做完 %s，verify %s 次（失败 %s），提交 机器人 %s / 人 %s，被拦 %s，人 判断点内 %s / 外 %s，决策 %s，提问 %s，" \
        "credits %s" % tuple(v(f) for f in NUMBERS)


def render_family(f) -> list:
    """A split's lines: a child's one line; a parent's line per child and the total."""
    if not f:
        return []
    if f["role"] == "child":
        m = f["merged_into_parent"]
        return ["小需求：%s 的单元 %s；%s %s%s" % (
            f["parent"], f["unit"] or "?", "已合进" if m["value"] else "还没合进", f["target_branch"] or "大需求的分支",
            "（从 %s 的分支开始）" % f["built_on"] if f.get("built_on") else "")]
    kids = f["children"]
    out = ["大需求：拆成的小需求有 %d 个 run，已合进 %d 个" % (
        len(kids), sum(1 for c in kids if c["merged_into_parent"]["value"]))]
    for c in kids:
        out.append("  %s %s（%s）：%s" % (c["key"], c["unit"] or "?", "已合进" if c["merged_into_parent"]["value"]
                                         else "没合进", _nums(c["numbers"])))
    out.append("  合计（大需求自己 + 小需求）：%s" % _nums(f["rollup"]["total"]))
    return out


def render(t) -> str:
    out = ["%s · %s · %s（%s）" % (t["key"], t["intent"] or "-", t["branch"] or "-", t["ref"])]
    out.append("负责人 %s；Brain %s；run %s" % (t["owner"] or "-", (t["brain_commit"] or "-")[:10],
                                          ", ".join(t["run_ids"]) or "-"))
    if t.get("other_runs"):
        out.append("别的分支上的 run（不算在这里）：" + "；".join("%s（%s，桥上 %s）" % (
            o["run_id"], o["branch"], o["state"] or "?") for o in t["other_runs"]))
    chains = t["src"]["lean_audit"]
    if chains:
        out.append("Lean 日志：" + "；".join("%s %d 条 %s" % (os.path.basename(p), c["records"],
                                                           "链完好" if c["chain"] == "ok" else c["chain"])
                                         for p, c in sorted(chains.items())))
    wt = t["src"].get("working_tree")
    if wt:
        out.append("工作区（HEAD %s）比 %s 旧：读的是那里，工作区里没提交的改动不算" % (
            wt["head"][:12], wt["read"] if wt["read"].startswith("refs/") else wt["read"][:12]))
    g = t["src"]["git"]
    if g.get("tip_note"):
        out.append("分支：用的是 %s；%s" % (g["tip_src"], g["tip_note"]))
    out += render_family(t.get("family"))
    if t["stages"]:
        mark = {"done": "x", "skipped": "S", "started": "-"}
        out.append("")
        out.append("阶段：")
        said = set()
        for s in t["stages"]:
            ph = s.get("phase_skipped")
            if not ph:
                out.append("  [%s] %s · %s" % (mark[s["state"]], s["id"], s["at"] or "-"))
            elif s["src"] not in said:          # a phase skipped whole: one line for the record, as Lean shows it
                said.add(s["src"])
                n = sum(1 for x in t["stages"] if x.get("phase_skipped") and x["src"] == s["src"])
                out.append("  [S] %s（整段跳过 %d 个阶段：%s）· %s" % (PHASE_NAMES.get(ph, ph), n, s["note"] or "-",
                                                             s["at"] or "-"))
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
        out.append("  在等%s回答：%s%s（从 %s 起）" % (" %s " % w["ask"] if w.get("ask") else "", w["kind"] or "?",
                                             " " + w["lean_request"] if w["lean_request"] else "", w["since"] or "?"))
    a = o.get("answered")
    if a:
        out.append("  有了回答：%s 的回答%s（%s）：%s" % (a["decision"], ANSWERED_LABEL.get(a["state"], a["state"]),
                                                a["at"] or "?", ANSWERED_NEXT.get(a["state"], "")))
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
