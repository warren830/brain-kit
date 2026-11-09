"""`brain mr-desc`: the MR's 变更说明 — what changed, what it does, how to review it — built from the run's records.

Every line comes from a record; what no record holds is said in one line ("还没跑 verify"), never filled in.
build() reads, render() formats; write() is the only writer (runs/<KEY>/MR.md: `brain mr-desc --write`, and
`brain handoff` into its hand-over commit). The bridge posts the file as its 「变更说明」 comment through inert().

Sources (working tree, or --ref)
  run.json            runs/<KEY>/run.json: intent, owner, branch, hand-over
  requirements.md     aidlc-docs/<intent>/inception/requirements/requirements.md: Goal, Not doing, Acceptance
                      criteria, Assumptions A-n, Size
  plans               construction/plans/<unit>-code-generation-plan.md: steps ticked, the Tests table (AC/BR -> test)
  Lean audit          aidlc-docs/audit/*.jsonl: stages, approvals (and the channel an approval line claimed), requests,
                      the latest VERIFY (result, checks, code fingerprint, the commit it ran on)
  build-and-test      build-and-test-summary.md (when the audit has no VERIFY), review.md (findings table)
  release.md          operations/release.md: Open items
  decisions.md        the decision table (Who in the fixed vocabulary), brain-proposal and brain-gap blocks
  park.json           runs/<KEY>/park.json: the question the run is waiting on
  git                 the diff against the merge base with the default branch (runs/<KEY>/MR.md itself left out);
                      gates/risk-paths from the default branch; DESIGN coverage by brainkit.ci (signed vouchers)
  run trace           brainkit.trace.build(), when it works: Brain citations that are archived or unknown, credits
Current code: Lean's own code fingerprint (Lean's Project.fingerprint(), run in a subprocess) against the one the
latest verify recorded; with --ref, the files changed since the verify's commit.

People: GitLab usernames and pseudonyms already in the records only. Emails and home directories in record text
are replaced (trace.scrub); an unrecognised decisions.md Who cell is not shown (it may be a real name).
Record text is put on one line, with '@' and '<' made inert: no mention, quick action or HTML comment reaches GitLab.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys

from . import ci, lean, reflect, runrec, usage
from . import trace as T
from .common import JIRA_RE, USER_RE, BrainError, git, plain
from .intake import checks

SCHEMA = 1
MR_FILE = "MR.md"
# a reply line the bridge reads as an answer (bridge.decisions.ANSWER_RE starts like this): inert() quotes it
ANSWER_LINE_RE = re.compile(r"(?i)^\s*(?:answer|答|回答)\s+D-[0-9]")
REQ_DOC = "inception/requirements/requirements.md"
# Lean 0.3 Ideation (only with `lean new --ideation`): IDEATION covers the Ideation documents; the hand-off brief
# is always among them
IDEATION_DOC = "ideation/approval-handoff/initiative-brief.md"
IDEATION_IDS = ("intent-capture", "market-research", "feasibility", "scope-definition", "team-formation",
                "rough-mockups", "approval-handoff")
DESIGN_DOC = "inception/application-design/application-design.md"
PLANS_DIR = "construction/plans"
SUMMARY_DOC = "construction/build-and-test/build-and-test-summary.md"
REVIEW_DOC = "construction/build-and-test/review.md"
RELEASE_DOC = "operations/release.md"
DECISIONS_DOC = "decisions.md"
LEAN_EXCLUDE = list(lean.DEFAULT_FINGERPRINT_EXCLUDE)     # Lean's default fingerprint_exclude
STAGE_NAMES = {"intent-capture": "意图捕获", "market-research": "市场调研", "feasibility": "可行性评估",
               "scope-definition": "范围定义", "team-formation": "团队组建", "rough-mockups": "草图原型",
               "approval-handoff": "审批交接", "ci-pipeline": "CI 流水线", "deployment-pipeline": "部署流水线",
               "environment-provisioning": "环境准备", "deployment-execution": "部署执行",
               "observability-setup": "可观测性", "incident-response": "故障响应",
               "performance-validation": "性能验证", "feedback-optimization": "反馈优化",
               "reverse-engineering": "逆向工程", "requirements": "需求分析", "user-stories": "用户故事",
               "application-design": "应用设计", "units": "拆分单元", "functional-design": "功能设计",
               "nfr-design": "非功能设计", "infrastructure-design": "基础设施设计", "code-generation": "代码生成",
               "build-and-test": "构建测试", "release": "发布"}
STAGE_STATE = {"STAGE_STARTED": "进行中", "STAGE_COMPLETED": "做完", "STAGE_SKIPPED": "跳过"}
KIND_LABEL = {"clarify": "澄清", "design": "设计", "plan": "计划", "acceptance": "验收", "action": "危险动作",
              "other": "其他"}
APPROVER = {"IDEATION": "产品", "REQUIREMENTS": "产品", "DESIGN": "Builder", "PLAN": "Builder", "RELEASE": "产品"}
MISSING_DOC = {"IDEATION": "还没写构想文档（initiative-brief.md）", "REQUIREMENTS": "还没有 requirements.md", "DESIGN": "还没写 application-design.md",
               "PLAN": "还没有代码生成计划", "RELEASE": "还没跑 lean release"}
LIMIT = {"ac": 12, "decisions": 10, "assumptions": 8, "open_items": 5, "groups": 8, "code": 8, "risky": 5,
         "proposals": 5, "gaps": 3, "not_doing": 5, "approvals": 6, "findings": 3}
# Lean's own patterns (aidlc_lean.py SIZE_RE, CHECKBOX_RE): the first Size line counts; plan checkboxes
SIZE_RE = re.compile(r"(?im)^[ \t>*-]*\**[ \t]*Size[ \t]*\**[ \t]*[:：][ \t]*\**[ \t]*([A-Za-z]+|<)")
CHECKBOX_RE = re.compile(rb"(?m)^([ \t]*[-*] )\[[xX ]\]")
STEP_RE = re.compile(r"(?m)^[ \t]*[-*] \[([xX ])\]")
PLAN_RE = re.compile(r"^(.+)-code-generation-plan\.md$")
AC_ID_RE = re.compile(r"(?<![A-Za-z0-9-])((?:AC|BR)-[1-9][0-9]{0,3})(?![0-9])")
ITEM_RE = re.compile(r"^\s*(?:[-*]|[0-9]+[.)])\s+(\S.*)$")
SEVERITY = (("critical", "严重"), ("high", "高"), ("medium", "中"), ("low", "低"))


# ---------- text: from a record into one inert Markdown line ----------

def _one(text, limit=None) -> str:
    """Record text as one line: emails and home directories replaced (trace.scrub), whitespace collapsed, '@' and '<'
    made inert (no mention, no HTML comment that would hide the rest), images not loaded, cut to `limit`."""
    s = " ".join(T.scrub(str(text or "")).split())
    s = s.replace("@", "＠").replace("<", "＜").replace("![", "!［")
    if limit and len(s) > limit:
        s = s[:limit - 1].rstrip() + "…"
    return s


def inert(text) -> str:
    """Markdown someone else wrote (a runs/<KEY>/MR.md a bot committed), made safe to post on GitLab: no mention, no
    HTML (an HTML comment would hide what follows), no image loaded, no line that GitLab runs as a quick action, and
    no line that reads as an approval, a rejection or an answer (`approve RELEASE …`, `answer D-3 approve`: quoted
    with '> ', so neither Lean nor the bridge, nor a person copying it, takes it as one). Idempotent: render()'s own
    output (it ends with this) comes back unchanged."""
    out = []
    for line in str(text).replace("@", "＠").replace("<", "＜").replace("![", "!［").split("\n"):
        if line.lstrip().startswith("/"):
            line = "\\" + line.lstrip()
        elif checks.APPROVAL_LINE_RE.match(line) or ANSWER_LINE_RE.match(line):
            line = "> " + line
        out.append(line)
    return "\n".join(out)


def _cell(text, limit=None) -> str:
    return _one(text, limit).replace("|", "\\|") or "—"


def _when(ts, date_only=False) -> str:
    m = re.match(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})(?:[T ]([0-9]{2}:[0-9]{2}))?", str(ts or ""))
    if not m:
        return "时间不明"
    return m.group(1) if date_only or not m.group(2) else "%s %s UTC" % (m.group(1), m.group(2))


def _ids(items, limit=6) -> str:
    items = list(items)
    return "、".join(items[:limit]) + ("等 %d 个" % len(items) if len(items) > limit else "")


# ---------- reading the records ----------

def _safe_rel(path) -> bool:
    p = str(path or "")
    return bool(p) and not p.startswith("/") and "\\" not in p and "\0" not in p and \
        ".." not in p.split("/") and not re.match(r"^[A-Za-z]:", p)


class _Tree:
    """Files from the working tree (ref None) or a commit, as bytes. Symlinks and paths outside the repo read as
    missing: a planted link must not pull a file from this machine into the description."""

    def __init__(self, repo, ref):
        self.repo, self.ref, self.commit = repo, ref, None
        if ref:
            self.commit = _commit(repo, ref)
            if not self.commit:
                raise BrainError("%s is not a commit here (fetch it first)" % ref)
        self._root = os.path.realpath(repo)

    @property
    def label(self) -> str:
        return self.ref or "working tree"

    def raw(self, path):
        if not _safe_rel(path):
            return None
        if self.ref is None:
            p = os.path.join(self.repo, path)
            if os.path.islink(p) or not os.path.isfile(p) or \
                    not os.path.realpath(p).startswith(self._root + os.sep):
                return None
            with open(p, "rb") as f:
                return f.read()
        s = subprocess.run(["git", "-C", self.repo, "cat-file", "blob", "%s:%s" % (self.commit, path)],
                           capture_output=True)
        return s.stdout if s.returncode == 0 else None

    def read(self, path):
        b = self.raw(path)
        return None if b is None else b.decode("utf-8", "replace")

    def files(self, prefix) -> list:
        if self.ref is None:
            out = []
            for d, dirs, names in os.walk(os.path.join(self.repo, prefix)):
                dirs[:] = sorted(x for x in dirs if not os.path.islink(os.path.join(d, x)))
                out += [os.path.relpath(os.path.join(d, n), self.repo).replace(os.sep, "/") for n in names]
            return sorted(out)
        p = git(self.repo, "ls-tree", "-r", "-z", "--name-only", self.commit, "--", prefix, check=False)
        return sorted(x for x in p.stdout.split("\0") if x) if p.returncode == 0 else []


def _commit(repo, rev):
    if not rev:
        return None
    p = git(repo, "rev-parse", "-q", "--verify", "%s^{commit}" % rev, check=False)
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def _d(e) -> dict:
    d = e.get("data")
    return d if isinstance(d, dict) else {}


def _events(tree, intent) -> list:
    """The intent's Lean audit records of every clone, oldest first."""
    out = []
    for path in tree.files(lean.DOCS + "/audit"):
        if not path.endswith(".jsonl"):
            continue
        for line in (tree.read(path) or "").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("intent") == intent and isinstance(rec.get("event"), str):
                out.append(rec)
    out.sort(key=lambda e: str(e.get("ts") or ""))
    return out


def _cells(line) -> list:
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _tables(text) -> list:
    """[(header in lower case, [rows of cells])] of the Markdown tables in text, outside fenced blocks."""
    out, header, fence = [], None, False
    for line in (text or "").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fence, header = not fence, None
            continue
        if fence or not line.strip().startswith("|"):
            header = None
            continue
        cells = _cells(line)
        if header is None:
            header = [c.lower() for c in cells]
            out.append((header, []))
        elif not all(re.match(r"^:?-{2,}:?$", c) for c in cells if c):
            out[-1][1].append(cells + [""] * (len(header) - len(cells)))
    return out


def _col(header, *words):
    return next((i for i, h in enumerate(header) if any(w in h for w in words)), None)


def _sections(text) -> dict:
    """{'## ' heading in lower case: its lines}, HTML comments removed."""
    out, cur = {}, None
    for line in re.sub(r"<!--.*?-->", "", text or "", flags=re.S).split("\n"):
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            cur = m.group(1).strip().lower()
            out.setdefault(cur, [])
        elif cur is not None:
            out[cur].append(line)
    return out


def _section(secs, *names):
    return next((v for h, v in secs.items() if any(h.startswith(n) for n in names)), None)


def _items(lines) -> list:
    """Non-empty bullet or numbered items."""
    return [m.group(1).strip() for m in (ITEM_RE.match(x) for x in lines or []) if m and m.group(1).strip()]


def _first_sentence(text, limit=120) -> str:
    m = re.match(r"^(.+?[。！？]|.+?[.!?](?=\s|$))", text)
    return _one(m.group(1) if m else text, limit)


def _requirements(text):
    """What requirements.md says, or None when it is missing; 'written' False while it is still the template."""
    if text is None:
        return None
    secs = _sections(text)
    goal = " ".join(x.strip() for x in _section(secs, "goal") or [] if x.strip())
    title = re.search(r"(?m)^#\s*Requirements:\s*(.+?)\s*$", text)
    size = SIZE_RE.search(text)
    acs = []
    for header, rows in _tables("\n".join(_section(secs, "acceptance") or [])):
        i, g, t = _col(header, "id"), _col(header, "given", "when"), _col(header, "then", "expected")
        for cells in rows if i is not None else []:
            aid = cells[i].strip("`* ")
            given = cells[g] if g is not None else ""
            then = cells[t] if t is not None else ""
            if AC_ID_RE.fullmatch(aid) and (given.strip() or then.strip()):
                acs.append({"id": aid, "given": given, "then": then})
    assumptions = []
    for line in _section(secs, "assumption") or []:
        m = T.ASSUMPTION_RE.match(line)
        if m and m.group(2).strip():
            assumptions.append({"id": m.group(1), "text": m.group(2).strip()})
    not_doing = _items(_section(secs, "not doing"))
    return {"title": title.group(1) if title and "{{" not in title.group(1) else None,
            "size": size.group(1).lower() if size and size.group(1) != "<" else None,
            "goal": goal or None, "not_doing": not_doing, "acs": acs, "assumptions": assumptions,
            "written": bool(goal or acs)}


def _plans(tree, docs) -> list:
    """Each unit's code-generation plan: steps ticked, and AC/BR -> tests from its Tests table."""
    out = []
    d = "%s/%s" % (docs, PLANS_DIR)
    for path in tree.files(d):
        m = PLAN_RE.match(path[len(d) + 1:]) if path.startswith(d + "/") and "/" not in path[len(d) + 1:] else None
        if not m:
            continue
        text = tree.read(path) or ""
        boxes = STEP_RE.findall(re.sub(r"<!--.*?-->", "", text, flags=re.S))
        tests = {}
        for header, rows in _tables("\n".join(_section(_sections(text), "tests") or [])):
            a, t, k = _col(header, "ac", "br"), _col(header, "test"), _col(header, "kind")
            if a is None or t is None:
                continue
            for cells in rows:
                name = cells[t].strip().strip("`").strip()
                if not name:
                    continue
                for aid in AC_ID_RE.findall(cells[a]):
                    tests.setdefault(aid, []).append({"test": name, "kind": cells[k].strip() if k is not None
                                                      else "", "unit": m.group(1)})
        out.append({"unit": m.group(1), "path": path, "steps": len(boxes),
                    "done": sum(1 for b in boxes if b.lower() == "x"), "tests": tests})
    return out


def _lean_cfg(tree) -> dict:
    try:
        cfg = json.loads(tree.read("%s/lean.json" % lean.DOCS) or "{}")
    except ValueError:
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def _record_path(path, lcfg) -> bool:
    """Lean's is_record_path: fingerprint_exclude (aidlc-docs/**, .kiro/** by default), except contracts, and the
    per-clone .aidlc-lean/ state. Not code: changing it leaves Lean's code fingerprint as it is."""
    if path.startswith(".aidlc-lean/"):
        return True
    contracts = str(lcfg.get("contracts_dir") or lean.DOCS + "/contracts").strip("/") + "/"
    if path.startswith(contracts) and path != contracts + "OWNERS" and not path.startswith(contracts + "changes/"):
        return False
    pats = lcfg.get("fingerprint_exclude")
    for pat in pats if isinstance(pats, list) else LEAN_EXCLUDE:
        if isinstance(pat, str) and ((pat.endswith("/") and path.startswith(pat)) or fnmatch.fnmatchcase(path, pat)):
            return True
    return False


def write_problem(repo, cfg, key=None):
    """Why `--write` must not write runs/<KEY>/MR.md here, or None (`brain doctor` can ask it without a key). Lean
    counts every file outside fingerprint_exclude as code: an MR.md (or park.json) written after `lean verify` reads
    as a code change: the commit gate stops the commit, and `lean release` and the RELEASE approval refuse, until
    aidlc-docs/lean.json excludes runs/."""
    if not lean.installed(repo):
        return None
    lcfg = _lean_cfg(_Tree(repo, None))
    if _record_path("%s/%s/%s" % (cfg["runs_dir"], key or "KEY-1", MR_FILE), lcfg):
        return None
    return ("%s/ 下的文件（MR.md、park.json）算在 Lean 的代码指纹里：verify 之后写它们，Lean 会当成代码改了（提交被拦，"
            "release 和 RELEASE 审批要重来）。请人在 %s/lean.json 的 fingerprint_exclude 里加上 \"%s/**\"（AI 改不了"
            "这个文件）" % (cfg["runs_dir"], lean.DOCS, cfg["runs_dir"]))


def runs_exclude(cfg) -> str:
    """The fingerprint_exclude pattern for the run records (runs/**): what `brain install` adds to lean.json."""
    return "%s/**" % cfg["runs_dir"].strip("/")


def exclude_fix(cfg) -> str:
    """How to fix what write_problem() reports (for `brain doctor` and `brain handoff`)."""
    return ("修法：`brain install <项目目录>` 会把 \"%s\" 加进 %s（只加不删），或者手工加；由人看过再提交（最好经 MR 合进 %s）。"
            "改了以后 Lean 的代码指纹会变，之前的 lean verify 要重跑" % (runs_exclude(cfg), lean.CONFIG,
                                                                  cfg["default_branch"]))


def md_path(cfg, key) -> str:
    """runs/<KEY>/MR.md, the repo path of a run's description."""
    return "%s/%s/%s" % (cfg["runs_dir"], key, MR_FILE)


# ---------- Lean: verify, approvals, stage ----------

def lean_fingerprint(repo):
    """Lean's code fingerprint of the working tree, computed by the project's own Lean (its Project.fingerprint());
    None when it cannot be had. Lean keeps a stat cache in .aidlc-lean/ (gitignored), as `lean status` does."""
    kit = os.path.join(repo, ".kiro", "aidlc-lean")
    if not os.path.isfile(os.path.join(kit, "aidlc_lean.py")):
        return None
    code = ("import sys\nsys.path.insert(0, sys.argv[1])\nimport aidlc_lean\n"
            "print(aidlc_lean.Project(sys.argv[2]).fingerprint() or '')\n")
    try:
        # -B: no __pycache__ written into the project's .kiro/aidlc-lean/ (it would show as an uncommitted file)
        p = subprocess.run([sys.executable, "-I", "-B", "-c", code, kit, repo], cwd=repo, capture_output=True,
                           text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    fp = p.stdout.strip()
    return fp if p.returncode == 0 and re.match(r"^[0-9a-f]{64}$", fp) else None


def _verify(events, tree, docs) -> dict:
    """The latest verify of the intent (any clone), from the audit; from build-and-test-summary.md without one."""
    vs = [e for e in events if e.get("event") == "VERIFY"]
    if vs:
        d = _d(vs[-1])
        res = [r for r in d.get("results") or [] if isinstance(r, dict)] if isinstance(d.get("results"), list) else []
        drift = d.get("protected_drift")
        return {"at": vs[-1].get("ts"), "ok": d.get("ok") is True,
                "fingerprint": str(d.get("fingerprint") or "") or None, "checks": len(res), "failed": [{"cmd": _one(r.get("cmd"), 80), "exit": r.get("exit")}
                                               for r in res if r.get("exit") != 0],
                "protected_drift": len(drift) if isinstance(drift, list) else 0,
                "base_commit": d.get("base_commit") if isinstance(d.get("base_commit"), str) else None,
                "uncommitted": d.get("uncommitted") if isinstance(d.get("uncommitted"), int) else None,
                "rounds": len(vs), "failed_rounds": sum(1 for e in vs if _d(e).get("ok") is not True),
                "src": "Lean audit VERIFY"}
    text = tree.read("%s/%s" % (docs, SUMMARY_DOC))
    if text is None:
        return None
    g = lambda rx: (re.search(rx, text, re.M) or [None, None])[1]  # noqa: E731
    return {"at": g(r"^- time: (\S+)\s*$"), "ok": g(r"^- result: (PASS|FAIL)\s*$") == "PASS",
            "fingerprint": g(r"^- code fingerprint: ([0-9a-f]{8,64})\s*$"), "checks": None, "failed": [],
            "protected_drift": 0, "base_commit": None, "uncommitted": None, "rounds": None, "failed_rounds": None,
            "src": "%s/%s" % (docs, SUMMARY_DOC)}


def _changed_since(repo, tree, v, lcfg):
    """Code paths changed since the verify's commit (sorted), or None when that cannot be told by commits (no commit
    recorded, uncommitted code at verify time, or git cannot say)."""
    base = _commit(repo, v.get("base_commit"))
    if not base or v.get("uncommitted") != 0:
        return None
    try:
        if tree.commit:
            paths = ci.changed_paths(repo, base, tree.commit)
        else:
            paths = ci.changed_paths(repo, base)
            p = subprocess.run(["git", "-C", repo, "ls-files", "-o", "--exclude-standard", "-z"], capture_output=True)
            if p.returncode != 0:
                return None
            paths += [x for x in p.stdout.decode("utf-8", "surrogateescape").split("\0") if x]
    except BrainError:
        return None
    return sorted({p for p in paths if not _record_path(p, lcfg)})


def _verify_state(repo, tree, v, lcfg, runs_dir):
    """(current, why): current True / False / None (cannot tell), and what changed when it is not."""
    if not v or not v.get("fingerprint"):
        return None, None
    changed = None
    if tree.ref is None:
        fp = lean_fingerprint(repo)
        if fp and fp == v["fingerprint"]:
            return True, None
        changed = _changed_since(repo, tree, v, lcfg)
        if fp is None and changed is None:
            return None, "没算出现在代码的指纹"
        if fp is None and changed == []:
            return True, None
    else:
        changed = _changed_since(repo, tree, v, lcfg)
        if changed is None:
            return None, "verify 时有没提交的改动，按提交比不出来"
        if not changed:
            return True, None
    if changed and all(p.startswith(runs_dir + "/") for p in changed):
        return False, "只改了 %s/ 下的记录（%s），Lean 也算进代码指纹" % (runs_dir, _ids(changed, 2))
    return False, ("改了 %s" % _ids(changed, 3)) if changed else None


def _covers(d, path) -> bool:
    files = d.get("files")
    return path in files if isinstance(files, dict) else d.get("path") == path


def _covered_sha(d, path):
    files = d.get("files")
    return files.get(path) if isinstance(files, dict) else d.get("sha256")


def _approval_sha(tree, path):
    """The sha256 Lean binds an approval of the file to (a plan's with its checkboxes read as unticked)."""
    b = tree.raw(path)
    if b is None:
        return None
    if "/%s/" % PLANS_DIR in path:
        b = CHECKBOX_RE.sub(rb"\1[ ]", b)
    return hashlib.sha256(b).hexdigest()


def _doc_state(events, tree, path) -> dict:
    """missing | draft | requested | approved | stale | rejected, as Lean's approval_state and pending_request."""
    cur = _approval_sha(tree, path)
    if cur is None:
        return {"state": "missing", "event": None}
    last, pending = None, None
    for e in events:
        ev, d = e.get("event"), _d(e)
        if ev in ("APPROVED", "REJECTED") and _covers(d, path):
            last, pending = e, None
        elif ev == "APPROVAL_REQUESTED" and _covers(d, path):
            files = d.get("files") if isinstance(d.get("files"), dict) else {path: d.get("sha256")}
            pending = e if all((cur if f == path else _approval_sha(tree, f)) == h for f, h in files.items()) else None
    if last and last["event"] == "APPROVED":
        return {"state": "approved" if _covered_sha(_d(last), path) == cur else "stale", "event": last}
    if pending:
        return {"state": "requested", "event": pending}
    if last:
        return {"state": "rejected", "event": last}
    return {"state": "draft", "event": None}


def _channel(d) -> str:
    c = d.get("claimed_channel")
    return "经 %s" % _one(c, 80) if isinstance(c, str) and c.strip() else "本地会话"


def _approvals(events, tree, docs, plans, lcfg, req) -> dict:
    """Approvals Lean recorded, and those still missing for a release."""
    recorded = []
    for e in events:
        ev, d = e.get("event"), _d(e)
        if ev not in ("APPROVED", "REJECTED") or not isinstance(d.get("kind"), str):
            continue
        files = sorted(d["files"]) if isinstance(d.get("files"), dict) else [d.get("path")] if d.get("path") else []
        now = [_approval_sha(tree, f) for f in files]
        current = bool(files) and all(n is not None and n == _covered_sha(d, f) for f, n in zip(files, now))
        recorded.append({"kind": _one(d["kind"], 40), "unit": _one(d.get("unit"), 40) or None,
                         "sha": str(d.get("sha256") or "")[:8] or None, "at": e.get("ts"),
                         "approved": ev == "APPROVED", "channel": _channel(d), "current": current})
    stage = lambda sid: next((e for e in reversed(events) if e.get("event") in STAGE_STATE  # noqa: E731
                              and _d(e).get("stage") == sid), None)
    need = [("REQUIREMENTS", None, "%s/%s" % (docs, REQ_DOC))]
    if any(e.get("event") in STAGE_STATE and _d(e).get("stage") in IDEATION_IDS for e in events):
        need.insert(0, ("IDEATION", None, "%s/%s" % (docs, IDEATION_DOC)))     # Ideation used (not PHASE_SKIPPED)
    skipped = stage("application-design")
    if skipped and skipped["event"] == "STAGE_SKIPPED" and (req or {}).get("size") == "small":
        design_skipped = True
    else:
        design_skipped = False
        need.append(("DESIGN", None, "%s/%s" % (docs, DESIGN_DOC)))
    mode = lcfg.get("plan", "auto")
    if mode == "always" or (mode == "auto" and lcfg.get("regulated")):
        need += [("PLAN", p["unit"], p["path"]) for p in plans] or [("PLAN", None, None)]
    need.append(("RELEASE", None, "%s/%s" % (docs, RELEASE_DOC)))
    missing = []
    for kind, unit, path in need:
        st = _doc_state(events, tree, path) if path else {"state": "missing", "event": None}
        if st["state"] == "approved":
            continue
        missing.append({"kind": kind, "unit": unit, "state": st["state"], "who": APPROVER[kind],
                        "sha": str(_covered_sha(_d(st["event"]), path) or "")[:8] or None if st["event"] else None})
    released = _doc_state(events, tree, "%s/%s" % (docs, RELEASE_DOC))
    return {"recorded": recorded, "missing": missing, "design_skipped": design_skipped,
            "released": bool(released["event"]) and released["event"].get("event") == "APPROVED"
            and released["state"] in ("approved", "stale")}


def _stage(events):
    last = next((e for e in reversed(events) if e.get("event") in STAGE_STATE), None)
    if not last:
        return None
    d = _d(last)
    sid = str(d.get("stage") or "")
    return {"id": sid, "unit": _one(d.get("unit"), 40) or None, "state": STAGE_STATE[last["event"]],
            "name": STAGE_NAMES.get(sid, _one(sid, 40)), "at": last.get("ts")}


# ---------- run records: park.json, release.md, review.md, decisions.md ----------

def _park(raw):
    if raw is None:
        return None
    try:
        p = json.loads(raw)
    except ValueError:
        return {"status": "not JSON"}
    if not isinstance(p, dict):
        return {"status": "not JSON"}
    kind = p.get("kind") if p.get("kind") in KIND_LABEL else "other"
    lr = p.get("lean_request") if isinstance(p.get("lean_request"), str) else None
    return {"status": _one(p.get("status"), 20) or None, "kind": kind, "question": _one(p.get("question"), 160) or None,
            "lean_request": _one(lr, 40) or None,
            "options": len(p["options"]) if isinstance(p.get("options"), list) else 0,
            "review_items": len(p["review_items"]) if isinstance(p.get("review_items"), list) else 0}


def _open_items(text) -> list:
    return [x for x in _items(_section(_sections(text or ""), "open items")) if not x.startswith("(")]


def _review(text):
    if text is None:
        return None
    counts, top, total = {}, [], 0
    for header, rows in _tables(text):
        s, f = _col(header, "severity", "严重"), _col(header, "finding", "发现", "问题")
        if s is None and f is None:
            continue
        for cells in rows:
            if not any(c.strip() for c in cells):
                continue
            total += 1
            sev = cells[s].strip().lower() if s is not None else ""
            label = next((zh for en, zh in SEVERITY if en in sev or zh in sev), "其他")
            counts[label] = counts.get(label, 0) + 1
            if label in ("严重", "高") and f is not None and len(top) < LIMIT["findings"]:
                top.append({"severity": label, "finding": _one(cells[f], 100)})
        break
    notes = [x.strip() for x in re.sub(r"<!--.*?-->", "", text, flags=re.S).split("\n")
             if x.strip() and not x.lstrip().startswith(("#", "|", "```"))]
    return {"findings": total, "by_severity": counts, "top": top, "summary": [_one(x, 160) for x in notes[:2]]}


def _decisions(text, bot_users, acs) -> list:
    out = []
    for n, row in T.decision_rows(text or ""):
        did, dec = row.get("id", "").strip("`* "), row.get("decision", "")
        if not re.match(r"^D-[1-9][0-9]*$", did) or not dec.strip():
            continue
        by, who = T.classify_who(row.get("who"), bot_users)
        whole = " ".join(v for k, v in row.items() if k != "id")
        out.append({"id": did, "date": row.get("date") if T.DATE_RE.match(row.get("date") or "") else None,
                    "stage": _one(row.get("stage"), 30) or None, "decision": _one(dec, 200),
                    "why": _one(row.get("why"), 200) or None, "by": by, "who": who,
                    "vouchers": sorted(set(T.VOUCHER_RE.findall(whole))),
                    "cites": sorted(set(usage.CITE_RE.findall(whole))),
                    "acs": sorted(set(a for a in AC_ID_RE.findall(whole) if a in acs)), "line": n})
    return out


def _who_text(d) -> str:
    by = d["by"]
    if by == "agent":
        return "agent 自己定的（交付时请过一眼）"
    if by == "brain":
        return "brain %s" % d["who"]
    if by == "check":
        return "check（测试 / lint / CI）"
    if by == "person" and d["who"] == "owner":
        return "owner（会话里答的）"
    if by == "person":
        return "%s%s" % (d["who"], "（%s）" % d["vouchers"][0] if d["vouchers"] else "")
    if by is None:
        return "没写"
    return "没按约定写（第 %d 行）" % d["line"]


def _cites(tree, docs) -> list:
    found = set()
    for path in tree.files(docs):
        if path.endswith(".md"):
            found.update(usage.CITE_RE.findall(usage.strip_proposals(tree.read(path) or "")))
    return sorted(found)


# ---------- git: what the MR changes ----------

def _default_ref(repo, cfg):
    for r in ("origin/" + cfg["default_branch"], cfg["default_branch"]):
        if _commit(repo, r):
            return r
    return None


def _changes(repo, cfg, key, tree, base):
    """(changes, why not): the diff from the merge base with the default branch to the tip, by top-level folder."""
    tip = tree.commit or _commit(repo, "HEAD")
    dref = _default_ref(repo, cfg)
    if base:
        b = _commit(repo, base)
        if not b:
            raise BrainError("--base %s is not a commit here" % base)
        label = "%s（%s）" % (_one(base, 60), b[:12])
    else:
        if not dref:
            return None, "找不到 %s（也没有 origin/%s），没法算改了什么" % (cfg["default_branch"], cfg["default_branch"])
        p = git(repo, "merge-base", tip, dref, check=False)
        b = p.stdout.strip()
        if p.returncode != 0 or not b:
            return None, "和 %s 没有共同的提交，没法算改了什么" % dref
        label = "%s 的分叉点 %s" % (dref, b[:12])
    mr = "%s/%s/%s" % (cfg["runs_dir"], key, MR_FILE)
    p = subprocess.run(["git", "-C", repo, "diff", "--no-renames", "--no-ext-diff", "-z", "--numstat", b, tip, "--",
                        ".", ":(exclude)" + mr], capture_output=True)
    if p.returncode != 0:
        return None, "git diff 失败，没法算改了什么"
    files = []
    for rec in p.stdout.decode("utf-8", "surrogateescape").split("\0"):
        parts = rec.split("\t", 2)
        if len(parts) == 3 and parts[2]:
            files.append((parts[2], int(parts[0]) if parts[0].isdigit() else None,
                          int(parts[1]) if parts[1].isdigit() else None))
    kinds = {lean.DOCS: "Lean 记录", cfg["runs_dir"]: "run 记录", ".kiro": "kit", cfg["brain_dir"]: "Brain"}
    groups = {}
    for path, add, dele in files:
        top = path.split("/", 1)[0] if "/" in path else ""
        g = groups.setdefault(top, {"dir": top or "（根目录）", "kind": kinds.get(top), "files": 0, "added": 0,
                                    "deleted": 0, "binary": 0})
        g["files"] += 1
        if add is None:
            g["binary"] += 1
        else:
            g["added"] += add
            g["deleted"] += dele
    order = sorted(groups.values(), key=lambda g: (g["kind"] is not None, -g["files"] if g["kind"] is None else 0,
                                                   g["dir"]))
    uncommitted = None
    if tree.ref is None:
        s = subprocess.run(["git", "-C", repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                           capture_output=True)
        if s.returncode == 0:
            entries, skip = [], False
            for x in s.stdout.decode("utf-8", "surrogateescape").split("\0"):
                if skip or len(x) < 4:
                    skip = False
                    continue
                skip = x[0] in "RC" or x[1] in "RC"
                if x[3:] != mr:
                    entries.append(x[3:])
            uncommitted = len(entries)
    risk = _risk(repo, cfg, tree, dref, [f for f, _, _ in files])
    code = [{"path": _one(f, 120), "added": a, "deleted": x} for f, a, x in sorted(files)
            if kinds.get(f.split("/", 1)[0] if "/" in f else "") is None]
    return {"base": b, "base_label": label, "tip": tip, "files": len(files), "code_files": len(code),
            "code": code[:50],
            "added": sum(a for _, a, _ in files if a), "deleted": sum(d for _, _, d in files if d),
            "groups": order, "uncommitted": uncommitted, "risk": risk, "brain_touched": cfg["brain_dir"] in groups,
            "src": "git diff --no-renames --numstat %s %s (without %s)" % (b[:12], tip[:12], mr)}, None


def _risk(repo, cfg, tree, dref, files):
    rp = cfg["risk_paths_file"]
    text, src = None, None
    if dref:
        s = subprocess.run(["git", "-C", repo, "cat-file", "blob", "%s:%s" % (dref, rp)], capture_output=True)
        if s.returncode == 0:
            text, src = s.stdout.decode("utf-8", "replace"), "%s:%s" % (dref, rp)
    if text is None:
        text, src = tree.read(rp), "%s (%s)" % (rp, tree.label)
    pats = [x for x in (line.split("#", 1)[0].strip() for line in (text or "").splitlines()) if x]
    if not pats:
        return None
    risky = sorted(f for f in files if any(ci.glob_match(f, x) for x in pats))
    return {"patterns": pats, "files": risky, "src": src} if risky else None


def _design_hash(v):
    """The design an answer voucher approves: the hex after 'DESIGN ' in its lean_request (8 digits shown)."""
    m = re.match(r"^DESIGN ([0-9a-f]{6,64})$", str(v.get("lean_request") or ""))
    return m.group(1)[:8] if m else None


class _MainTarget:
    """What brainkit.ci.check_route reads from an MR's target branch, here the default branch (read-only)."""

    def __init__(self, repo, ref):
        self.repo, self.ref, self.trusted = repo, ref, bool(ref)

    def file(self, path):
        if not self.ref:
            return None
        p = git(self.repo, "show", "%s:%s" % (self.ref, path), check=False)
        return p.stdout if p.returncode == 0 else None


def _design_cover(repo, cfg, rec, key, tree, ch, events, docs, approvals, vouchers_dir, allowed_signers) -> dict:
    """Does a DESIGN approval cover the risk-path changes? ci.check_route decides when signed vouchers can be read
    (working tree only); otherwise say what Lean recorded and that the signature was not checked here."""
    design = "%s/%s" % (docs, DESIGN_DOC)
    if approvals["design_skipped"] or tree.raw(design) is None:
        return {"state": "needs_design", "text": "应用设计%s：改 risk path 必须走设计阶段（CI 的 brain-run-check 会拦）"
                % ("跳过了" if approvals["design_skipped"] else "没写"), "short": "缺设计阶段，CI 会拦"}
    if tree.ref is None and vouchers_dir and allowed_signers and os.path.isdir(vouchers_dir) and \
            os.path.isfile(allowed_signers):
        try:
            vouchers, bad = ci.signed_vouchers(vouchers_dir, allowed_signers, key, repo)
            probs = ci.check_route(repo, rec, cfg, _MainTarget(repo, _default_ref(repo, cfg)), ch["base"], vouchers)
        except (BrainError, OSError, ValueError, KeyError, TypeError) as e:
            return {"state": "unknown", "text": "没核对出来（%s）" % _one(str(e).replace(repo, "."), 120),
                    "short": "设计批准没核对出来"}
        if not probs:
            ok = [v for v in ci.latest_design_answers(repo, vouchers) if v.get("choice") == "approve"
                  and not ci.design_approval_covers(repo, v, design, ch["risk"]["files"])]
            v = ok[0] if ok else {}
            h = _design_hash(v)
            return {"state": "covered", "voucher": _one(v.get("voucher_id"), 20) or None, "design": h,
                    "text": "设计已批 DESIGN %s（%s，签名已核对），覆盖这些改动" % (
                        h or "?", _one(v.get("voucher_id"), 20) or "凭证"),
                    "short": "设计已批 DESIGN %s（%s）" % (h or "?", _one(v.get("voucher_id"), 20) or "凭证")}
        return {"state": "not_covered", "text": "没有能覆盖它们的设计批准：%s" % _one(probs[0], 200),
                "short": "没有能覆盖它的设计批准，CI 会拦"}
    st = _doc_state(events, tree, design)
    e = st["event"]
    if st["state"] in ("approved", "stale"):
        return {"state": "lean_" + st["state"], "design": str(_covered_sha(_d(e), design) or "")[:8],
                "text": "Lean 记了 DESIGN %s 的批准（%s）%s；签名本机没核对（CI 的 brain-run-check 会核对）" % (
                    str(_covered_sha(_d(e), design) or "")[:8], _channel(_d(e)),
                    "" if st["state"] == "approved" else "，之后设计又改了"),
                "short": "Lean 记了 DESIGN %s 的批准%s，签名由 CI 核对" % (
                    str(_covered_sha(_d(e), design) or "")[:8], "" if st["state"] == "approved" else "（之后设计又改了）")}
    return {"state": "not_covered", "text": "还没有 DESIGN 批准（CI 的 brain-run-check 会拦）",
            "short": "还没有 DESIGN 批准，CI 会拦"}


# ---------- the description ----------

def build(repo, cfg, key, base=None, ref=None, credits=None, vouchers_dir=None, allowed_signers=None) -> dict:
    """The MR description of requirement `key`, as data (render() makes the Markdown). `ref`: read the records from
    this commit instead of the working tree. `base`: diff against this instead of the merge base with the default
    branch. `credits`: what the KiroCrew app passes to brain trace. `vouchers_dir` + `allowed_signers`: check the
    DESIGN approval's signature for risk-path changes (brain-intake vouchers)."""
    if not JIRA_RE.match(key or ""):
        raise BrainError("%r is not a requirement key like PAY-1" % key)
    tree = _Tree(repo, ref)
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
    intent = rec.get("intent") if isinstance(rec.get("intent"), str) and runrec.INTENT_RE.match(rec["intent"]) \
        else None
    owner = rec.get("owner") if isinstance(rec.get("owner"), str) and USER_RE.match(rec["owner"]) else None
    branch = rec.get("branch") if plain(rec.get("branch")) else None
    handoff = rec.get("handoff") if isinstance(rec.get("handoff"), dict) else None
    missing = []
    docs = "%s/%s" % (lean.DOCS, intent) if intent else None
    if not intent:
        missing.append("run.json 里没有有效的 Lean intent，Lean 的记录都读不到")
    events = _events(tree, intent) if intent else []
    lcfg = _lean_cfg(tree)

    req = _requirements(tree.read("%s/%s" % (docs, REQ_DOC))) if docs else None
    if docs and req is None:
        missing.append("没有 requirements.md")
    elif req is not None and not req["written"]:
        missing.append("需求还没写（requirements.md 还是模板）")
    elif req is not None and not req["acs"]:
        missing.append("需求里还没有验收标准")
    plans = _plans(tree, docs) if docs else []
    if docs and not plans:
        missing.append("还没有代码生成计划（不知道哪个测试对应哪条验收标准）")
    v = _verify(events, tree, docs) if docs else None
    current, why = _verify_state(repo, tree, v, lcfg, cfg["runs_dir"]) if v else (None, None)
    if docs and not v:
        missing.append("还没跑 verify")
    tests = {}
    for p in plans:
        for aid, ts in p["tests"].items():
            tests.setdefault(aid, []).extend(ts)
    acs = []
    for ac in (req or {}).get("acs", []):
        ts = tests.get(ac["id"], [])
        shown = []
        for t in ts:
            fpath = t["test"].split("::", 1)[0].strip()
            exists = None
            if re.match(r"^[\w./-]+\.[A-Za-z0-9]+$", fpath) and _safe_rel(fpath):
                exists = tree.raw(fpath) is not None
            shown.append({"test": _one(t["test"], 80), "kind": _one(t["kind"], 20) or None, "file_exists": exists})
        if not ts:
            result = "没有对应的测试"
        elif not v:
            result = "还没跑 verify"
        elif current is False:
            result = "verify 之后又改了代码"
        else:
            result = "%s · %s · %s" % ("通过" if v["ok"] else "没过", _when(v["at"], True), (v["fingerprint"] or "")[:8])
        acs.append({"id": ac["id"], "text": _one("%s → %s" % (ac["given"].strip(), ac["then"].strip())
                                                 if ac["given"].strip() and ac["then"].strip()
                                                 else ac["given"] or ac["then"], 60),
                    "tests": shown, "result": result})

    ch, why_no_changes = _changes(repo, cfg, key, tree, base)
    if why_no_changes:
        missing.append(why_no_changes)
    elif not ch["files"]:
        missing.append("和 %s 比，还没有提交的改动" % ch["base_label"])
    approvals = _approvals(events, tree, docs, plans, lcfg, req) if docs else {"recorded": [], "missing": [],
                                                                              "design_skipped": False,
                                                                              "released": False}
    if ch and ch["risk"]:
        ch["risk"]["design"] = _design_cover(repo, cfg, rec, key, tree, ch, events, docs, approvals, vouchers_dir,
                                             allowed_signers) if docs else {"state": "unknown",
                                                                            "text": "没有 Lean intent，查不了设计批准"}

    dec_text = tree.read("%s/%s" % (docs, DECISIONS_DOC)) if docs else None
    decisions = _decisions(dec_text, cfg.get("bot_users") or [], {a["id"] for a in acs})
    proposals, pprobs = reflect.parse(dec_text or "")
    gaps, gprobs = reflect.parse_gaps(dec_text or "")
    release_text = tree.read("%s/%s" % (docs, RELEASE_DOC)) if docs else None
    review = _review(tree.read("%s/%s" % (docs, REVIEW_DOC))) if docs else None
    if docs and review is None and plans:
        missing.append("还没有评审（review.md）")
    park = _park(tree.raw("%s/%s/park.json" % (cfg["runs_dir"], key)))

    tr, tr_err = None, None
    try:
        tr = T.build(repo, cfg, key, ref=ref, credits=credits)
    except Exception as e:  # noqa: BLE001  the trace only adds to the description; the records above stand alone
        tr_err = _one(str(e).replace(repo, "."), 160)
    tb = tr.get("brain") if isinstance(tr, dict) and isinstance(tr.get("brain"), dict) else {}
    chains = (tr or {}).get("src", {}).get("lean_audit") if isinstance((tr or {}).get("src"), dict) else None
    broken = sorted(k for k, c in (chains or {}).items() if isinstance(c, dict) and c.get("chain") != "ok")
    cost = (tr or {}).get("cost") if isinstance((tr or {}).get("cost"), dict) else {}
    if tr_err:
        missing.append("run trace 没读出来（%s）：Brain 引用只按 Lean 记录算" % tr_err)

    stage = _stage(events)
    waiting = None
    if park and park.get("status") == "waiting":
        waiting = "等人在 MR 上回答%s问题" % KIND_LABEL.get(park["kind"], "")
    else:
        req_kinds = [m["kind"] for m in approvals["missing"] if m["state"] == "requested"]
        if req_kinds:
            waiting = "等 %s 审批" % "、".join(req_kinds)
    data = {
        "schema": SCHEMA, "key": key, "ref": tree.label, "intent": intent, "owner": owner, "branch": branch,
        "title": _one((req or {}).get("title") or next((_d(e).get("title") for e in events
                                                         if e.get("event") == "INTENT_CREATED"), None) or intent,
                      80) or None,
        "where": {"stage": stage, "handed_over": bool(handoff),
                  "handoff_at": handoff.get("requested_at") if handoff and plain(handoff.get("requested_at")) else None,
                  "waiting": waiting, "released": approvals["released"]},
        "size": (req or {}).get("size"),
        "goal": _one((req or {}).get("goal"), 300) or None,
        "goal_first": _first_sentence((req or {}).get("goal")) if (req or {}).get("goal") else None,
        "not_doing": [_one(x, 100) for x in (req or {}).get("not_doing", [])],
        "plans": [{"unit": p["unit"], "steps": p["steps"], "done": p["done"]} for p in plans],
        "acceptance": acs,
        "verify": dict(v, current=current, why=why) if v else None,
        "changes": ch,
        "decisions": decisions,
        "assumptions": [{"id": a["id"], "text": _one(a["text"], 160)} for a in (req or {}).get("assumptions", [])],
        "open_items": [_one(x, 160) for x in _open_items(release_text)],
        "release_written": release_text is not None,
        "park": park,
        "approvals": approvals,
        "review": review,
        "brain": {"cited": _cites(tree, docs) if docs else [], "stale": list(tb.get("stale") or []),
                  "unknown": list(tb.get("unknown") or []),
                  "proposals": [{"file": _one(p.fields.get("file"), 20), "type": _one(p.fields.get("type"), 20),
                                 "rel": _one(p.rel + (" " + p.target if p.target else ""), 40),
                                 "text": _one(p.body, 80), "src": [_one(s, 40) for s in p.src]} for p in proposals],
                  "gaps": [{"title": _one(g.title, 80), "kind": _one(g.kind, 20)} for g in gaps],
                  "problems": len(pprobs) + len(gprobs)},
        "cost": {"credits": cost.get("credits"), "turns": len(cost.get("per_turn") or [])}
        if cost.get("credits") is not None else None,
        "audit_broken": broken,
        "missing": missing,
        "src": {"run": "%s (%s)" % (rp, tree.label), "lean": "%s/audit/*.jsonl" % lean.DOCS,
                "records": docs, "trace": "brainkit.trace.build" if tr else None},
    }
    data["pointers"] = _pointers(data)
    return data


def _pointers(d) -> list:
    """2-4 concrete places to look first, derived from the data above."""
    out = []
    ch, v = d["changes"] or {}, d["verify"]
    risk = ch.get("risk")
    if risk:
        dz = risk.get("design") or {}
        out.append("改到了 risk path %s：%s" % (_ids(risk["files"], 2), dz.get("short", "设计批准没查")))
    if ch.get("brain_touched"):
        out.append("改到了 brain/：run 分支不能改 Brain（CI 会拦），应写成 brain-proposal")
    if v and v.get("current") is False:
        out.append("verify 之后又改了代码（%s）：合并前要重跑 lean verify" % (v.get("why") or "指纹对不上"))
    elif v and not v["ok"]:
        out.append("verify 没过%s：先看失败的检查" % ("（%s）" % v["failed"][0]["cmd"] if v["failed"] else ""))
    agent = [x for x in d["decisions"] if x["by"] == "agent"]
    named = set()
    for x in agent:
        ac = next((a for a in d["acceptance"] if a["id"] in x["acs"]), None)
        if ac:
            named.add(ac["id"])
            out.append("先看 %s 的测试 `%s`；它的边界是 agent 自己定的（%s）" % (
                ac["id"], ac["tests"][0]["test"].replace("`", "'"), x["id"])
                       if ac["tests"] else "先看 %s：它的边界是 agent 自己定的（%s），还没有测试" % (ac["id"], x["id"]))
            break
    if agent and not any("agent 自己定的" in p for p in out):
        out.append("agent 自己定了 %d 件事（%s）：交付时请过一眼" % (len(agent), _ids(x["id"] for x in agent)))
    if d["assumptions"]:
        out.append("确认假设 %s：需求里没写，agent 先这么假设" % _ids(a["id"] for a in d["assumptions"]))
    untested = [a["id"] for a in d["acceptance"] if not a["tests"] and a["id"] not in named]
    if untested:
        out.append("%s 没有对应的测试：确认怎么验收" % _ids(untested))
    lost = [a["id"] for a in d["acceptance"] if any(t["file_exists"] is False for t in a["tests"])]
    if lost:
        out.append("%s 的测试文件不在仓库里：计划的 Tests 表和代码对不上" % _ids(lost))
    rv = d["review"]
    high = sum((rv or {}).get("by_severity", {}).get(k, 0) for k in ("严重", "高"))
    if high:
        out.append("评审还有 %d 条高严重度发现（review.md）" % high)
    if d["brain"]["stale"] or d["brain"]["unknown"]:
        out.append("引用了%s的 Brain 条目 %s：确认不是过时或写错的号" % (
            "已归档" if d["brain"]["stale"] else "Brain 里没有", _ids(d["brain"]["stale"] or d["brain"]["unknown"])))
    return out[:4]


# ---------- Markdown ----------

def _more(n, where) -> str:
    return "- 另有 %d 条，见 %s。" % (n, where)


def render(d) -> str:
    """The description in Markdown (Chinese, short plain sentences). Deterministic: the same records give the same
    text; nothing about the time it was made."""
    title = d["title"] or "（没有标题）"
    if title.startswith(d["key"] + " "):
        title = title[len(d["key"]) + 1:]
    out = ["## %s · %s" % (d["key"], title), ""]
    w = d["where"]
    st = w["stage"]
    now = ["Lean 在%s%s%s" % ("「%s」" % st["name"], "（%s）" % st["unit"] if st["unit"] else "", st["state"])
           if st else "Lean 还没开始阶段",
           "已交给团队运行机" if w["handed_over"] else "在本地开发（还没交接）"]
    if w["released"]:
        now.append("RELEASE 已批")
    elif w["waiting"]:
        now.append(w["waiting"])
    first = (d["goal_first"] or "需求还没写").rstrip("。.!?！？")
    out.append("**一句话**：%s%s。现在：%s。" % (first, "（规模 %s）" % d["size"] if d["size"] else "", "，".join(now)))
    meta = ["分支 `%s`" % _one(d["branch"], 100) if d["branch"] else None,
            "负责人 %s" % d["owner"] if d["owner"] else None, "intent `%s`" % d["intent"] if d["intent"] else None]
    out.append("（%s）" % "；".join(x for x in meta if x))

    # 做了什么 / 没做什么
    body = []
    if d["goal"] and d["goal"] != d["goal_first"]:
        body.append("- 目标：%s" % d["goal"])
    if d["not_doing"]:
        nd = d["not_doing"][:LIMIT["not_doing"]]
        body.append("- 不做：%s%s" % ("；".join(nd), "（另有 %d 条）" % (len(d["not_doing"]) - len(nd))
                                     if len(d["not_doing"]) > len(nd) else ""))
    if d["plans"]:
        body.append("- 代码计划：%s" % "；".join("%s 做完 %d/%d 步" % (_one(p["unit"], 30), p["done"], p["steps"])
                                            for p in d["plans"]))
    if body:
        out += ["", "### 做了什么 / 没做什么"] + body

    # 验收标准和证据
    v = d["verify"]
    if d["acceptance"] or v:
        out += ["", "### 验收标准和证据"]
        if v:
            checks = ""
            if v["checks"] is not None:
                checks = "，%d 条检查%s" % (v["checks"], "全过" if not v["failed"] else "，没过：" + "、".join(
                    "`%s`（exit %s）" % (f["cmd"].replace("`", "'"), f["exit"]) for f in v["failed"][:3]))
            state = {True: "，之后代码没变", False:"；**verify 之后又改了代码**（%s）" % (v["why"] or "指纹对不上"),
                     None: "；%s" % (v["why"] or "没法核对现在的代码")}[v["current"]] if v["fingerprint"] else ""
            rounds = "；共跑过 %d 次，没过 %d 次" % (v["rounds"], v["failed_rounds"]) if v["rounds"] and \
                v["failed_rounds"] else ""
            out.append("最近一次 verify：%s · %s · 代码指纹 %s%s%s%s。" % (
                "通过" if v["ok"] else "没过", _when(v["at"]), (v["fingerprint"] or "?")[:8], checks, state, rounds))
            if v["protected_drift"]:
                out.append("")
                out.append("验收测试被改过 %d 处，没经过人允许（Lean 判 verify 没过）。" % v["protected_drift"])
        else:
            out.append("还没跑 verify。")
        if d["acceptance"]:
            out += ["", "| AC | 要求 | 测试 | 结果 |", "|---|---|---|---|"]
            for a in d["acceptance"][:LIMIT["ac"]]:
                tests = "；".join("`%s`%s" % (t["test"].replace("`", "'").replace("|", "\\|"),
                                             "（文件不在）" if t["file_exists"] is False else "")
                                 for t in a["tests"][:2]) or "—"
                out.append("| %s | %s | %s | %s |" % (a["id"], _cell(a["text"]), tests, a["result"]))
            out.append("")
            if len(d["acceptance"]) > LIMIT["ac"]:
                out.append(_more(len(d["acceptance"]) - LIMIT["ac"], "requirements.md"))
            if v and any(a["tests"] for a in d["acceptance"]):
                out.append("结果按整次 verify 算（Lean 不分单个测试）；测试名来自代码生成计划的 Tests 表。")

    # 改了什么
    ch = d["changes"]
    if ch and ch["files"] and not ch["code_files"]:
        out += ["", "### 改了什么", "- 还没改代码；和 %s 比，只有记录：%s。" % (ch["base_label"], "、".join(
            "%s %d 个" % (_one(g["dir"], 40), g["files"]) for g in ch["groups"]))]
    elif ch and ch["files"]:
        out += ["", "### 改了什么",
                "和 %s 比：%d 个文件，+%d −%d（不含 MR.md 本身）。" % (ch["base_label"], ch["files"], ch["added"],
                                                          ch["deleted"]),
                "", "| 目录 | 文件 | 增 | 删 |", "|---|---|---|---|"]
        for g in ch["groups"][:LIMIT["groups"]]:
            out.append("| %s%s | %d | +%d | −%d%s |" % (
                _cell(g["dir"], 40), "（%s）" % g["kind"] if g["kind"] else "", g["files"], g["added"], g["deleted"],
                "（另有 %d 个二进制）" % g["binary"] if g["binary"] else ""))
        out.append("")
        if len(ch["groups"]) > LIMIT["groups"]:
            out.append(_more(len(ch["groups"]) - LIMIT["groups"], "MR 的 Changes"))
        if 0 < ch["code_files"] <= LIMIT["code"]:
            out.append("- 代码文件：%s。" % "、".join("`%s`（%s）" % (
                c["path"].replace("`", "'"), "二进制" if c["added"] is None else "+%d −%d" % (c["added"], c["deleted"]))
                for c in ch["code"]))
        if ch["risk"]:
            r = ch["risk"]
            out += ["- 改到 risk path（%s）：%s%s。" % (
                _one(r["src"], 80), "、".join("`%s`" % _one(f, 80).replace("`", "'")
                                             for f in r["files"][:LIMIT["risky"]]),
                "等 %d 个" % len(r["files"]) if len(r["files"]) > LIMIT["risky"] else "")]
            out.append("- 设计批准：%s。" % (r.get("design") or {}).get("text", "没查"))
        if ch["uncommitted"]:
            out.append("- 另有 %d 个改动没提交（不在 MR 里）。" % ch["uncommitted"])

    # 关键决定
    if d["decisions"]:
        decs = d["decisions"]
        shown = [x for x in decs if x["by"] == "agent"][:LIMIT["decisions"]]
        shown += [x for x in decs if x not in shown][:max(0, LIMIT["decisions"] - len(shown))]
        shown.sort(key=lambda x: decs.index(x))
        out += ["", "### 关键决定", "| ID | 决定 | 为什么 | 谁定的 | Brain |", "|---|---|---|---|---|"]
        for x in shown:
            out.append("| %s | %s | %s | %s | %s |" % (x["id"], _cell(x["decision"], 80), _cell(x["why"], 60),
                                                      _who_text(x), "、".join(x["cites"]) or "—"))
        out.append("")
        if len(decs) > len(shown):
            out.append(_more(len(decs) - len(shown), DECISIONS_DOC))

    # 假设和待确认
    pend = ["- %s：%s" % (a["id"], a["text"]) for a in d["assumptions"][:LIMIT["assumptions"]]]
    if len(d["assumptions"]) > LIMIT["assumptions"]:
        pend.append(_more(len(d["assumptions"]) - LIMIT["assumptions"], "requirements.md"))
    pend += ["- 待办（release.md）：%s" % x for x in d["open_items"][:LIMIT["open_items"]]]
    if len(d["open_items"]) > LIMIT["open_items"]:
        pend.append(_more(len(d["open_items"]) - LIMIT["open_items"], RELEASE_DOC))
    p = d["park"]
    if p and p.get("status") == "waiting":
        pend.append("- 在等的问题（park.json，%s%s）：%s" % (
            KIND_LABEL.get(p["kind"], "其他"), "，审批 %s" % p["lean_request"] if p["lean_request"] else "",
            p["question"] or "（没写问题）"))
    elif p and p.get("status") == "not JSON":
        pend.append("- park.json 不是合法 JSON")
    if pend:
        out += ["", "### 假设和待确认"] + pend

    # 审批
    ap = d["approvals"]
    if ap["recorded"] or ap["missing"]:
        out += ["", "### 审批"]
        for a in ap["recorded"][-LIMIT["approvals"]:]:
            state = "驳回" if not a["approved"] else "仍有效" if a["current"] else "之后文件改了，作废"
            out.append("- %s%s %s · %s · %s · %s" % (a["kind"], "（%s）" % a["unit"] if a["unit"] else "",
                                                   a["sha"] or "?", _when(a["at"]), a["channel"], state))
        if len(ap["recorded"]) > LIMIT["approvals"]:
            out.append(_more(len(ap["recorded"]) - LIMIT["approvals"], "Lean audit"))
        if ap["design_skipped"]:
            out.append("- DESIGN：应用设计按 Size: small 跳过了")
        if ap["missing"]:
            items = []
            for m in ap["missing"]:
                t = {"draft": "写了，还没请求审批", "requested": "已请求 %s，等%s批" % (m["sha"] or "?", m["who"]),
                     "stale": "批过，之后文件改了", "rejected": "被驳回了",
                     "missing": MISSING_DOC.get(m["kind"], "文件还没有")}[m["state"]]
                items.append("%s%s（%s）" % (m["kind"], "·%s" % _one(m["unit"], 30) if m["unit"] else "", t))
            out.append("- 还缺：%s" % "；".join(items))

    # 评审
    rv = d["review"]
    if rv:
        out += ["", "### 评审"]
        if rv["findings"]:
            out.append("- review.md：%d 条发现（%s）" % (rv["findings"], "、".join(
                "%s %d" % (k, rv["by_severity"][k]) for k in ("严重", "高", "中", "低", "其他")
                if rv["by_severity"].get(k))))
        else:
            out.append("- review.md 里没有发现表")
        out += ["- %s：%s" % (t["severity"], t["finding"]) for t in rv["top"]]
        out += ["- %s" % s for s in rv["summary"]]

    # Brain
    b = d["brain"]
    bl = []
    if b["cited"]:
        bl.append("- 用到的条目：%s" % _ids(b["cited"], 10))
    if b["stale"]:
        bl.append("- 引用了已归档的条目（过时知识）：%s" % _ids(b["stale"]))
    if b["unknown"]:
        bl.append("- 引用了 Brain 里没有的条目号：%s" % _ids(b["unknown"]))
    if b["proposals"]:
        bl.append("- 等 `brain reflect` 的提案 %d 条：" % len(b["proposals"]))
        bl += ["  - %s %s（%s）：%s" % (x["file"], x["type"], x["rel"], x["text"] or "（没有正文）")
               for x in b["proposals"][:LIMIT["proposals"]]]
        if len(b["proposals"]) > LIMIT["proposals"]:
            bl.append("  " + _more(len(b["proposals"]) - LIMIT["proposals"], DECISIONS_DOC))
    if b["gaps"]:
        bl.append("- 缺口（reflect 后成为新需求）：%s" % "；".join("%s（%s）" % (g["title"], g["kind"] or "?")
                                                       for g in b["gaps"][:LIMIT["gaps"]]))
    if b["problems"]:
        bl.append("- %d 个提案或缺口块格式不对（`brain reflect` 会报出来）" % b["problems"])
    if bl:
        out += ["", "### Brain"] + bl

    if d["pointers"]:
        out += ["", "### 怎么审"] + ["%d. %s" % (i, x) for i, x in enumerate(d["pointers"], 1)]

    if d["cost"]:
        out += ["", "### 花费", "- credits %s%s（KiroCrew 给的；Kiro 不给 token 数）" % (
            ("%.4f" % d["cost"]["credits"]).rstrip("0").rstrip("."),
            "，%d 个回合" % d["cost"]["turns"] if d["cost"]["turns"] else "")]

    gone = list(d["missing"])
    if d["audit_broken"]:
        gone.append("Lean 日志链断了：%s" % "、".join(d["audit_broken"][:3]))
    if gone:
        out += ["", "### 还没有"] + ["- %s" % x for x in gone]
    out += ["", "---", "由 `brain mr-desc` 从记录生成（run.json、Lean 记录、git），别手改。重新生成：`brain mr-desc %s --write`。"
            % d["key"]]
    return inert("\n".join(out) + "\n")


def write(repo, cfg, key, text) -> str:
    """Write runs/<KEY>/MR.md (atomically; a symlink there is replaced, never followed). Returns its repo path."""
    rel = md_path(cfg, key)
    d = os.path.join(repo, cfg["runs_dir"], key)
    if not os.path.isdir(d) or os.path.islink(d):
        raise BrainError("%s/%s/ is not a run folder here" % (cfg["runs_dir"], key))
    tmp = os.path.join(d, ".MR.md.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, os.path.join(d, MR_FILE))
    return rel
