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

People: only GitLab usernames and pseudonyms already in the sources. Git and Lean identities (emails, names, hosts)
become a pseudonym G-<6 hex> of the email. Human message text is never copied, except what decisions.md holds.
Answers an eval oracle gave (golden tasks, `eval_oracle: true`) are kept apart and never count as a person's.
"""
from __future__ import annotations

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
from .common import BrainError, JIRA_RE, USER_RE, git, iso, utcnow

SCHEMA = 1
WINDOW_DAYS = 14
STAGE_STATE = {"STAGE_STARTED": "started", "STAGE_COMPLETED": "done", "STAGE_SKIPPED": "skipped"}
LEAN_APPROVALS = ("REQUIREMENTS", "DESIGN", "INCEPTION", "PLAN", "RELEASE")
ACCEPT_WHAT = ("RELEASE", "acceptance")
VOUCHER_RE = re.compile(r"(?<![0-9A-Za-z])V-[0-9a-f]{10}(?![0-9a-f])")
PSEUD_RE = re.compile(r"(?<![0-9A-Za-z])P-[0-9A-F]{4}(?![0-9A-Za-z])")
DID_RE = re.compile(r"(?<![0-9A-Za-z-])D-[1-9][0-9]*(?![0-9])")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
ASSUMPTION_RE = re.compile(r"^\s*[-*]?\s*(A-[1-9][0-9]*)\s*[:：.)]\s*(\S.*)$")
FIX_RE = re.compile(r"(?i)(hot-?fix|\bfix(?:es|ed)?\b|\bbug|defect|regression|\brevert|roll ?back|修复|热修|回滚|缺陷|故障)")
REVERTS_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
SPLIT_RE = re.compile(r"[\s,/;:()（）、+，：]+")
AGENT_WORDS = {"agent", "ai", "assistant", "brain-run", "brain-dev", "kiro", "claude", "model", "llm", "bot",
               "智能体", "机器人"}
CHECK_WORDS = {"check", "checks", "ci", "verify", "lean", "test", "tests", "lint", "gate", "gates", "hook", "pipeline",
               "检查", "测试"}
PERSON_WORDS = {"human", "person", "owner", "product", "builder", "security", "data", "pm", "po", "tl", "lead",
                "reviewer", "user", "产品", "人", "负责人", "开发", "安全", "评审", "业务"}
BRIDGE_WORDS = {"bridge", "桥"}
NO_TOKENS = "Kiro 经 KiroCrew 只给 credits，input/output/cache token 数都是 0"


# ---------- small helpers ----------

def _sha(b) -> str:
    return hashlib.sha256(b).hexdigest()


def pseudonym(email):
    """G-<6 hex> for a git / Lean identity: the email itself never leaves this module."""
    e = str(email or "").strip().lower()
    return "G-" + _sha(e.encode())[:6].upper() if e else None


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
    if not rev:
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
    """(events of this intent oldest first, {file: chain status}). Lean's own rule: each record's hash covers the
    record without the hash; prev links to the record before; seq counts up from 1."""
    events, chains = [], {}
    for path in [p for p in tree.files(lean.DOCS + "/audit") if p.endswith(".jsonl")]:
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
                rec["_src"] = "%s#%s" % (path, rec.get("seq", "line %d" % i))
                rec["_file"] = path
                rec["_at"] = _utc(rec.get("ts"))
                events.append(rec)
        chains[path] = {"records": n, "chain": "ok" if bad is None else "broken at line %d" % bad}
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
        row.update(state=st, at=e["_at"], note=note if isinstance(note, str) else None, src=e["_src"])
    return sorted(rows.values(), key=lambda r: (r["first_at"] or "", r["id"]))


def checks_of(events, tree, intent) -> dict:
    rounds = []
    for e in events:
        if e.get("event") != "VERIFY":
            continue
        d = _data(e)
        res = [r for r in d.get("results") or [] if isinstance(r, dict)]
        failed = [str(r.get("cmd", ""))[:120] for r in res if r.get("exit") not in (0, None)]
        rounds.append({"at": e["_at"], "ok": bool(d.get("ok")), "detail": {
            "checks": len(res), "failed": failed, "fingerprint": str(d.get("fingerprint") or "")[:8] or None,
            "protected_drift": len(d.get("protected_drift") or [])}, "src": e["_src"]})
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

def default_ref(repo, cfg, ref):
    d = cfg["default_branch"]
    if ref and _short_ref(ref) == d and _commit(repo, ref):   # name it one way, however it was given
        full = ("refs/remotes/origin/" if ref.startswith(("origin/", "refs/remotes/origin/")) else "refs/heads/") + d
        return full if _commit(repo, full) == _commit(repo, ref) else ref
    for r in ("refs/remotes/origin/" + d, "refs/heads/" + d):
        if _commit(repo, r):
            return r
    return None


def branch_tip(repo, branch, ref, dref):
    """(sha, how) of the run branch's last commit, or (None, None)."""
    if ref and _short_ref(ref) == branch and _commit(repo, ref):
        return _commit(repo, ref), "ref %s" % ref
    for r in ("refs/heads/" + branch, "refs/remotes/origin/" + branch):
        c = _commit(repo, r)
        if c:
            return c, r
    if dref:   # merged and deleted: GitLab's merge commit says "Merge branch '<branch>' into '<default>'"
        for line in (_g(repo, "log", "--merges", "--first-parent", "--format=%H%x00%P%x00%s", dref) or "").splitlines():
            parts = line.split("\0")
            if len(parts) == 3 and ("'%s'" % branch) in parts[2] and len(parts[1].split()) >= 2:
                return parts[1].split()[1], "second parent of merge commit %s on %s" % (parts[0][:12], dref)
    return None, None


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


def added_on(repo, dref, path):
    """Merged without a findable branch tip (squash or rebase merge, branch deleted): the first commit on the
    default branch's first-parent line that added run.json."""
    if not dref:
        return None
    out = (_g(repo, "log", "--first-parent", "--diff-filter=A", "--format=%H%x00%cI", dref, "--", path) or "").split()
    if not out:
        return None
    h, cd = (out[-1].split("\0") + [""])[:2]
    return {"commit": h, "at": _utc(cd), "how": "added run.json (squash or rebase merge)",
            "src": "first commit on the first-parent line of %s that added %s" % (dref, path)}


def _cdate(repo, sha):
    out = _g(repo, "log", "-1", "--format=%cI", sha)
    return _utc(out.strip()) if out else None


def git_history(repo, cfg, rec, ref, key):
    """Commits of the run branch, the hand-over commit, the merge, park.json history."""
    branch, bc = rec.get("branch") or "", rec.get("brain_commit")
    dref = default_ref(repo, cfg, ref)
    tip, tip_how = branch_tip(repo, branch, ref, dref)
    out = {"tip": tip, "tip_src": tip_how, "default_ref": dref, "merge": None, "commits": None, "range": None,
           "handover_commit": None, "park": None}
    if not tip:
        out["merge"] = added_on(repo, dref, "%s/%s/run.json" % (cfg["runs_dir"], key))
        return out
    m = merge_of(repo, tip, dref)
    if m:
        out["merge"] = {"commit": m, "at": _cdate(repo, m), "how": "merge commit" if m != tip else "fast-forward",
                        "src": "first-parent line of %s" % dref}
    if m and m != tip:
        exclude = m + "^1"
    elif dref and not m:
        exclude = dref
    else:
        exclude = bc if _commit(repo, bc or "") else None
    rng = [tip] + (["^" + exclude] if exclude else [])
    out["range"] = "%s ^%s" % (tip[:12], exclude[:40] if exclude else "(root)")
    bots = {x.lower() for x in cfg.get("bot_emails", [])}
    log = _g(repo, "-c", "core.quotepath=false", "log", "--topo-order", "--reverse",
             "--format=%x01%H%x00%ae%x00%ce%x00%cI%x00%P", "--name-only", *rng) or ""
    commits = []
    for chunk in log.split("\x01")[1:]:
        lines = chunk.split("\n")
        h, ae, ce, cd, parents = (lines[0].split("\0") + [""] * 5)[:5]
        files = [x for x in lines[1:] if x.strip()]
        bot = ae.lower() in bots or ce.lower() in bots
        commits.append({"sha": h, "at": _utc(cd), "by": "bot" if bot else "person",
                        "who": None if bot else pseudonym(ae), "files": len(files),
                        "records_only": bool(files) and all(f.startswith((lean.DOCS + "/", cfg["runs_dir"] + "/"))
                                                            for f in files),
                        "merge": len(parents.split()) > 1, "after_handover": None, "src": "git log " + out["range"]})
    rp = "%s/%s/run.json" % (cfg["runs_dir"], key)
    hand = None
    for h in (_g(repo, "log", "--topo-order", "--reverse", "--format=%H", *rng, "--", rp) or "").split():
        try:
            r = json.loads(_show(repo, h, rp) or "null")
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("handoff"):
            hand = h
            break
    out["handover_commit"] = hand
    if hand:
        after = set((_g(repo, "rev-list", tip, "^" + hand) or "").split())
        for c in commits:
            c["after_handover"] = c["sha"] in after
    elif not rec.get("handoff"):
        for c in commits:
            c["after_handover"] = False
    out["commits"] = commits
    pp = "%s/%s/park.json" % (cfg["runs_dir"], key)
    park, last = [], None
    for line in (_g(repo, "log", "--topo-order", "--reverse", "--format=%H%x00%cI", *rng, "--", pp) or "").splitlines():
        h, cd = (line.split("\0") + [""])[:2]
        row = _park_row(_show(repo, h, pp), "%s@%s" % (pp, h[:12]), _utc(cd), h)
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


def after_merge(repo, cfg, key, merge, commits, dref, as_of):
    """Reverts, hotfix commits and defect claims within WINDOW_DAYS after the merge. Messages are read, not kept."""
    if not merge or not merge.get("at"):
        return {"value": None, "window_closed": None, "window_end": None, "evidence": [],
                "src": "no merge found", "rules": None}
    start = _dt(merge["at"])
    end = start + timedelta(days=WINDOW_DAYS)
    closed = as_of >= end
    until = min(end, as_of)            # judged "as of": later commits are not looked at
    own = {c["sha"] for c in commits or []} | {merge["commit"]}
    key_re = re.compile(r"(?<![A-Za-z0-9-])%s(?![0-9])" % re.escape(key))
    ev = []
    log = _g(repo, "log", "--format=%x01%H%x00%cI%x00%B", "--since=" + merge["at"], "--until=" + iso(until),
             dref) or ""
    for chunk in log.split("\x01")[1:]:
        h, cd, body = (chunk.split("\0", 2) + ["", ""])[:3]
        if h in own:
            continue
        at = _utc(cd)
        if not at or _dt(at) <= start or _dt(at) > until:
            continue
        rv = [s for s in REVERTS_RE.findall(body) if any(o.startswith(s) for o in own)]
        if rv:
            ev.append({"sha": h, "at": at, "rule": "reverts %s" % rv[0][:12], "src": "git log %s" % dref})
        elif key_re.search(body) and FIX_RE.search(body):
            ev.append({"sha": h, "at": at, "rule": "names %s with a fix word" % key, "src": "git log %s" % dref})
    refs = (_g(repo, "for-each-ref", "--format=%(refname)", "refs/heads/claim/", "refs/remotes/origin/claim/") or "")
    seen = set()
    for r in sorted(refs.split()):
        k2 = r.rsplit("/", 1)[-1]
        if k2 == key or k2 in seen:
            continue
        try:
            doc = json.loads(_show(repo, r, "claim.json") or "null")
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        at = _utc(doc.get("created_at"))
        title = str(doc.get("title") or "")
        if at and start < _dt(at) <= until and key_re.search(title) and FIX_RE.search(title):
            seen.add(k2)
            ev.append({"claim": "claim/" + k2, "at": at, "rule": "defect claim names %s" % key, "src": r})
    return {"value": True if ev else (False if closed else None), "window_closed": closed, "window_end": iso(end),
            "evidence": sorted(ev, key=lambda x: (x["at"], x.get("sha", x.get("claim")))),
            "rules": "revert of a run commit; a commit naming %s with hotfix/fix/bug/defect/revert/修复/热修/回滚/缺陷; "
                     "a new claim/<KEY> whose title names %s with such a word" % (key, key),
            "src": "git log %s, claim/* refs" % dref}


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
    """(by, who shown) from a decisions.md Who cell. by: agent | check | bridge | person | None (unknown).
    A cell that is not a role word, a username or a pseudonym is not shown (it may be a real name)."""
    w = str(who or "").strip()
    if not w:
        return None, None
    low = w.lower()
    toks = [t for t in SPLIT_RE.split(low) if t]
    bots = {b.lower() for b in bot_users}
    pseud = PSEUD_RE.search(w)
    if toks and toks[0] in BRIDGE_WORDS:
        return "bridge", toks[0]
    if toks and (toks[0] in AGENT_WORDS or toks[0] in bots):
        return "agent", toks[0]
    if toks and (toks[0] in CHECK_WORDS or low in ("brain lint", "lean verify")):
        return "check", toks[0]
    if pseud:
        return "person", pseud.group(0)
    if toks and toks[0] in PERSON_WORDS:
        return "person", toks[0]
    if VOUCHER_RE.search(w):
        return "person", None
    if USER_RE.match(low):
        return "person", low
    if re.match(r"^[一-鿿]{2,4}$", w):
        return "person", None
    return None, None


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

def ledger_rows(path, key):
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
            for pi in sorted({(r["project"], r["iid"]) for r in runs}):
                handoffs += [dict(r) for r in db.execute(
                    "SELECT project, iid, sha, state, run_id, voucher_id, reason, created, updated FROM handoffs "
                    "WHERE project=? AND iid=? ORDER BY created, sha", pi)]
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


def _credits(credits, key, run_ids):
    """The KiroCrew app's record(s) for this requirement: a number, one record, or {"runs": {RUN-id|KEY: record}}."""
    if credits is None:
        return None
    if isinstance(credits, (int, float)) and not isinstance(credits, bool):
        return [{"credits": credits}]
    if not isinstance(credits, dict):
        return None
    if any(k in credits for k in ("credits", "total", "turns", "manual")):
        return [credits]
    pool = credits.get("runs") if isinstance(credits.get("runs"), dict) else credits
    picked = [pool[k] for k in sorted(pool) if (k == key or k in run_ids) and isinstance(pool[k], (dict, int, float))]
    return [p if isinstance(p, dict) else {"credits": p} for p in picked] or None


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 else None


def cost_of(recs) -> dict:
    if not recs:
        return {"credits": None, "per_turn": None, "context": None, "tokens": None, "tokens_why": NO_TOKENS,
                "src": None, "why": "credits not passed in (the KiroCrew app passes them; they are not readable here)"}
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
    handoff = rec.get("handoff") if isinstance(rec.get("handoff"), dict) else None
    handoff_at = _utc(handoff.get("requested_at")) if handoff else None
    missing = []

    events, chains = lean_events(tree, intent) if intent else ([], {})
    if not chains:
        missing.append("Lean audit (no aidlc-docs/audit/*.jsonl in %s)" % tree.label)
    docs = "%s/%s" % (lean.DOCS, intent) if intent else None
    dec_path = docs + "/decisions.md" if docs else None
    dec_text = tree.read(dec_path) if dec_path else None
    req_path = docs + "/inception/requirements/requirements.md" if docs else None

    hist = git_history(repo, cfg, rec, ref, key) if rec.get("branch") else {
        "tip": None, "tip_src": None, "default_ref": default_ref(repo, cfg, ref), "merge": None, "commits": None,
        "range": None, "handover_commit": None, "park": None}
    if hist["tip"] is None:
        missing.append("run branch %s (not found locally, on origin, or as a merge on the default branch)"
                       % rec.get("branch"))
        row = _park_row(tree.read("%s/%s/park.json" % (cfg["runs_dir"], key)),
                        "%s/%s/park.json (%s)" % (cfg["runs_dir"], key, tree.label), None)
        hist["park"] = [row] if row else []

    led = None
    if ledger_path:
        led = ledger_rows(ledger_path, key)
    else:
        missing.append("bridge ledger (--ledger not given)")
    vouchers = []
    if vouchers_dir:
        vouchers = voucher_files(vouchers_dir, key, allowed_signers)
    else:
        missing.append("vouchers (--vouchers not given and no intake store)")
    run_ids = sorted({r["run_id"] for r in (led or {}).get("runs", [])} |
                     {v["run_id"] for v in vouchers if v.get("run_id")})

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
            "recorded_in": None, "src": ["bridge ledger decisions"]}
    oracle = []
    for v in vouchers:
        if v.get("kind") != "answer" or not v.get("decision"):
            continue
        k = (v.get("run_id"), v.get("decision"))
        row = bridge.setdefault(k, {"id": v.get("decision"), "source": "bridge", "run_id": v.get("run_id"),
                                    "by": "bridge", "kind": None, "lean_request": None, "state": None,
                                    "choice": None, "who": None, "voucher_id": None, "at": None, "asked_at": None,
                                    "eval_oracle": False, "signature": None, "text": None, "cites": [],
                                    "recorded_in": None, "src": []})
        row.update(kind=v.get("decision_kind") or row["kind"],
                   lean_request=v.get("lean_request") or row["lean_request"],
                   choice=v.get("choice"), voucher_id=v.get("voucher_id"), at=_utc(v.get("issued_at")) or row["at"],
                   signature=v.get("_sig"), eval_oracle=v.get("eval_oracle") is True)
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
        humans.append({"at": e["_at"], "kind": kind, "what": d.get("kind") if ev != "HUMAN_TURN" else None,
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
    crecs = _credits(credits, key, run_ids)
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
        if h["after_handover"] is None and h["at"] and handoff_at:
            h["after_handover"] = h["at"] > handoff_at
        elif h["after_handover"] is None and handoff is None and h["at"]:
            h["after_handover"] = False
    humans.sort(key=lambda h: (h["at"] or "", h["kind"], h.get("decision") or "", h.get("sha") or ""))

    # ----- actions -----
    blocked = [{"at": e["_at"], "gate": _data(e).get("gate"), "tool": _data(e).get("tool"),
                "path": _data(e).get("path"),
                "command": str(_data(e)["command"])[:120] if _data(e).get("command") else None, "src": e["_src"]}
               for e in events if e.get("event") == "BLOCKED"]
    turns = {}
    for e in events:
        if e.get("event") in ("SESSION_START", "SUBAGENT_START", "HUMAN_TURN", "HOST_TURN", "SUBAGENT_PROMPT"):
            half = "unknown" if not handoff_at and handoff else (
                "after_handover" if handoff_at and e["_at"] and e["_at"] > handoff_at else "before_handover")
            t = turns.setdefault(e["event"], {})
            t[half] = t.get(half, 0) + 1
    handovers = []
    if handoff:
        handovers.append({"at": handoff_at, "kind": "requested", "by": handoff.get("by")
                          if isinstance(handoff.get("by"), str) and USER_RE.match(handoff["by"]) else None,
                          "commit": hist["handover_commit"], "src": run_src + " handoff"})
    for h in (led or {}).get("handoffs", []):
        handovers.append({"at": _utc(h["updated"]), "kind": "bridge " + str(h["state"]), "run_id": h["run_id"],
                          "mr_iid": h["iid"], "sha": str(h["sha"]).split("#", 1)[0],
                          "voucher_id": h["voucher_id"],
                          "reason": str(h["reason"])[:300] if h["reason"] else None, "src": "bridge ledger handoffs"})
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
    outcome = {
        "handed_over": {"value": bool(handoff), "at": handoff_at,
                        "bridge_accepted": any(h["kind"] == "bridge accepted" for h in handovers) if led else None,
                        "src": run_src + " handoff" + ("; bridge ledger handoffs" if led else "")},
        "accepted": accepted,
        "merged_at": {"value": merged["at"] if merged else None, "commit": merged["commit"] if merged else None,
                      "how": merged["how"] if merged else None,
                      "src": merged["src"] if merged else "no merge into %s found" % (hist["default_ref"] or
                                                                                        cfg["default_branch"])},
        "reverted_or_hotfixed_within_14d": after_merge(repo, cfg, key, merged, hist["commits"], hist["default_ref"],
                                                       as_of_dt),
    }

    if credits is None:
        missing.append("credits (not passed in)")
    missing.append("pushes (git keeps no push log; person commits after hand-over stand in for them)")
    missing.append("CI pipelines (GitLab, not readable here)")
    return {
        "schema": SCHEMA, "key": key, "as_of": iso(as_of_dt), "ref": tree.label,
        "intent": intent, "owner": owner, "branch": rec.get("branch") if isinstance(rec.get("branch"), str) else None,
        "brain_commit": brain["brain_commit"], "run_ids": run_ids,
        "src": {"key": "argument", "intent": run_src, "owner": run_src, "branch": run_src,
                "brain_commit": run_src, "run_ids": "bridge ledger runs + voucher files",
                "as_of": "argument" if as_of else "clock", "lean_audit": chains, "git": {
                    "tip": hist["tip"], "tip_src": hist["tip_src"], "range": hist["range"],
                    "default_ref": hist["default_ref"]}},
        "stages": stages_of(events),
        "actions": {"commits": hist["commits"], "pushes": None, "blocked": blocked, "turns": turns,
                    "handovers": handovers, "questions": hist["park"]},
        "decisions": decisions,
        "oracle": oracle,
        "checks": checks_of(events, tree, intent) if intent else None,
        "humans": humans,
        "brain": brain,
        "cost": cost_of(crecs),
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
    out.append("credits：%s" % (cost["credits"] if cost["credits"] is not None else "没有（KiroCrew 没传）"))
    o = t["outcome"]
    out.append("")
    out.append("交接：%s；验收：%s%s；合并：%s" % (
        "是" if o["handed_over"]["value"] else "否",
        {True: "通过", False: "被驳回", None: "没有记录"}[o["accepted"]["value"]],
        "（一次通过）" if o["accepted"]["first_time"] else ("（驳回 %d 次）" % o["accepted"]["rejections"]
                                                       if o["accepted"]["rejections"] else ""),
        o["merged_at"]["value"] or "没有"))
    r = o["reverted_or_hotfixed_within_14d"]
    if r["window_end"]:
        out.append("14 天内回滚/热修/缺陷：%s（窗口到 %s%s）" % (
            {True: "有", False: "没有", None: "还不知道"}[r["value"]], r["window_end"],
            "，已结束" if r["window_closed"] else "，还没结束"))
        out += ["  %s %s %s" % (x["at"], (x.get("sha") or x.get("claim", ""))[:12], x["rule"]) for x in r["evidence"]]
    if t["missing"]:
        out.append("")
        out.append("没读到：" + "；".join(t["missing"]))
    return "\n".join(out)
