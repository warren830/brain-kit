"""`brain reflect`: turn a run's Brain proposals into a Brain MR.

During a run the agent writes what it learned into the Lean intent's decisions.md, one fenced block per entry:

  ```brain-proposal
  file: TECH                 PRODUCT | TECH | IMPROVEMENT | PROJECT
  type: pitfall              one the file takes (brain lint lists them)
  rel: new                   new | replaces T-OMS-1-2 | adds-to <id> | same <id> | repeats <id> | withdraws <id>
  section: 外部系统           optional: the "## " section a new entry goes under
  src: D01#S030, V-1a2b3c4d5e    where it came from: segments, decision ids, voucher ids, RUN-ids
  ---
  The entry text (Markdown; for withdraws: the reason; for same / repeats: may be empty).
  ```

Reflect reads those blocks, checks them, gives new entries ids <letter>-<KEY>-<n> (skipping ids on main and on
open brain/* branches), and applies them with the deterministic editor (entries.py) in a fresh worktree on a new
branch brain/<KEY>-reflect-<n> from main. brain lint (relation rules against main) must pass, or nothing is kept.
Proposals already applied (same proposal hash in the Brain) are skipped; a reflect that only adds sources opens
no MR. With --push it pushes the branch and opens the MR for the file owners (CODEOWNERS) to review.
"""
from __future__ import annotations

import glob
import os
import re

from . import brainlint, config, entries as E
from .common import BrainError, git, git_out, load_json

FENCE_OPEN_RE = re.compile(r"^\s*(`{3,})\s*brain-proposal\s*$")
FENCE_CLOSE_RE = re.compile(r"^\s*(`{3,})\s*$")
INNER_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*\S")
ID_ARG_RE = re.compile(r"^[A-Z]-[A-Z0-9-]+$")
REJECT_CHOICES = ("reject",)


class Proposal:
    def __init__(self, fields, body, line):
        self.fields, self.body, self.line = fields, body, line
        rel = (fields.get("rel") or "new").split()
        self.rel = rel[0]
        self.target = rel[1] if len(rel) > 1 else ""
        self.src = E.split_src(fields.get("src", ""))
        self.hash = E.prop_hash(fields.get("file", ""), fields.get("type", ""), fields.get("rel", ""),
                                fields.get("section", ""), ",".join(self.src), E.norm(body))

    def label(self):
        return "proposal at line %d" % self.line


def parse(text) -> tuple:
    """(proposals, problems) from a decisions.md (or any Markdown)."""
    props, probs = [], []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = FENCE_OPEN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        ticks = len(m.group(1))
        start = i + 1
        j, inner = start, None
        while j < len(lines):
            line = lines[j]
            if inner:
                if re.match(r"^\s*%s\s*$" % re.escape(inner), line):
                    inner = None
            else:
                c = FENCE_CLOSE_RE.match(line)
                if c and len(c.group(1)) >= ticks:
                    break
                o = INNER_OPEN_RE.match(line)
                if o:                         # a code block inside the entry text (```python ... ```)
                    inner = o.group(1)
            j += 1
        if j >= len(lines):
            probs.append("line %d: brain-proposal block is not closed" % (start))
            break
        block = lines[start:j]
        fields, body, in_body = {}, [], False
        for n, line in enumerate(block):
            if in_body:
                body.append(line)
            elif line.strip() == "---":
                in_body = True
            elif not line.strip():
                continue
            elif ":" in line:
                k, v = line.split(":", 1)
                k = k.strip().lower()
                v = re.sub(r"\s+#.*$", "", v).strip()      # trailing "# comment" after a value
                if k in fields:
                    probs.append("line %d: %s given twice" % (start + n + 1, k))
                fields[k] = v
            else:
                probs.append("line %d: expected 'key: value' or '---'" % (start + n + 1))
        props.append(Proposal(fields, "\n".join(body), start))
        i = j + 1
    return props, probs


class Op:
    def __init__(self, kind, proposal, **kw):
        self.kind, self.p = kind, proposal
        self.kw = kw
        self.result = None
        self.note = ""


def plan(brain, proposals, key, pending=None, used_extra=()) -> tuple:
    """(ops, problems, notes). Problems block the whole reflect; notes are shown.
    `pending`: {proposal hash: branch} already written on open brain/* branches (MRs not merged yet)."""
    ops, probs, notes = [], [], []
    props = brain.props()
    pending = pending or {}
    main = brain.main()
    texts = {e.text(): eid for eid, (f, e) in main.items()}
    for p in proposals:
        where = p.label()
        f = p.fields
        if p.hash in props:
            notes.append("%s: already in the Brain as %s, skipped" % (where, props[p.hash]))
            continue
        if p.hash in pending:
            notes.append("%s: already on %s (MR not merged yet), skipped" % (where, pending[p.hash]))
            continue
        unknown = set(f) - {"file", "type", "rel", "section", "src"}
        if unknown:
            probs.append("%s: unknown fields %s" % (where, ", ".join(sorted(unknown))))
        if p.rel not in E.RELS:
            probs.append("%s: rel must be one of %s" % (where, ", ".join(E.RELS)))
            continue
        if not p.src:
            probs.append("%s: src is required (segment, decision id, voucher id or RUN-id)" % where)
        bad_src = [s for s in p.src if not E.SRC_ITEM_RE.match(s)]
        if bad_src:
            probs.append("%s: source %s is not a reference" % (where, ", ".join(bad_src)))
        body = E.norm(p.body)
        target = None
        if p.rel != "new":
            if not ID_ARG_RE.match(p.target or ""):
                probs.append("%s: rel %s needs an entry id" % (where, p.rel))
                continue
            if p.target not in main:
                state = "archived" if p.target in brain.archived() else "not in the Brain"
                probs.append("%s: %s is %s" % (where, p.target, state))
                continue
            target = main[p.target]
        try:
            fk = E.file_key(f["file"]) if f.get("file") else (target[0].key if target else None)
        except BrainError as e:
            probs.append("%s: %s" % (where, e))
            continue
        if not fk:
            probs.append("%s: file is required" % where)
            continue
        if target and target[0].key != fk:
            probs.append("%s: %s is in %s, not %s" % (where, p.target, target[0].key, fk))
            continue
        etype = f.get("type") or (target[1].type if target else "")
        if p.rel in ("new", "replaces"):
            if etype not in E.FILES[fk]["types"]:
                probs.append("%s: %s takes %s, not %r" % (where, fk, ", ".join(E.FILES[fk]["types"]), etype))
                continue
        if p.rel in ("new", "replaces", "adds-to", "withdraws") and not body:
            probs.append("%s: text is empty%s" % (where, " (give the reason)" if p.rel == "withdraws" else ""))
            continue
        if p.rel in ("new", "replaces", "adds-to", "withdraws"):
            errs, _ = brainlint.content_problems(body)
            probs += ["%s: %s" % (where, x) for x in errs]
        sec = f.get("section")
        if sec:
            if p.rel != "new":
                probs.append("%s: section is only for new entries" % where)
            elif not E.SECTION_RE.match(sec):
                probs.append("%s: section: up to 40 characters, no #, <, >, `, [, ], |" % where)
            else:
                probs += ["%s: section: %s" % (where, x) for x in brainlint.content_problems(sec)[0]]
        if p.rel == "new" and body in texts:
            notes.append("%s: same text as %s: recorded as same (sources added)" % (where, texts[body]))
            ops.append(Op("same", p, eid=texts[body], type=main[texts[body]][1].type))
            continue
        if p.rel == "new":
            ops.append(Op("new", p, file=fk, type=etype, section=f.get("section") or None))
        elif p.rel == "replaces":
            ops.append(Op("replaces", p, eid=p.target, type=etype))
        else:
            ops.append(Op(p.rel, p, eid=p.target, type=target[1].type))
    if not probs and ops:
        probs += dry_run(brain, ops, key, used_extra)
    return ops, probs, notes


def dry_run(brain, ops, key, used_extra=()) -> list:
    """Apply to a copy and lint the result against the original, so the preview fails exactly when --apply would."""
    trial = brain.copy()
    out = []
    for op in ops:
        try:
            apply(trial, [op], key, used_extra)
        except BrainError as e:
            out.append("%s: %s" % (op.p.label(), e))
        op.result, op.note = None, ""
    if not out:
        errors, _ = brainlint.check(trial, base=brain)
        out += errors
    return out


def apply(brain, ops, key, used_extra=()):
    for op in ops:
        p, kw = op.p, op.kw
        body = E.norm(p.body)
        if op.kind == "new":
            op.result = brain.new(kw["file"], kw["type"], body, key, p.src, section=kw["section"], prop=p.hash,
                                  used_extra=used_extra)
        elif op.kind == "replaces":
            op.result = brain.replace(kw["eid"], body, key, p.src, etype=kw["type"], prop=p.hash,
                                      used_extra=used_extra)
        elif op.kind == "adds-to":
            op.result = brain.adds_to(kw["eid"], body, p.src, prop=p.hash)
        elif op.kind == "same":
            op.result = brain.same(kw["eid"], p.src, prop=p.hash)
        elif op.kind == "repeats":
            n = brain.repeat(kw["eid"], p.src, prop=p.hash)
            op.result = kw["eid"]
            if n >= 2:
                op.note = "came back %d times: make it a gate" % n
        elif op.kind == "withdraws":
            op.result = brain.withdraw(kw["eid"], body, p.src, prop=p.hash)
    return ops


def substantive(ops) -> bool:
    """Only 'same' (sources added) is not worth an MR."""
    return any(op.kind != "same" for op in ops)


def rejected_without_correction(vouchers_dir, key, proposals) -> list:
    """Answers that rejected something but no proposal names them: a lesson about to be lost."""
    out = []
    cited = set()
    for p in proposals:
        if p.fields.get("type") == "correction" or p.rel == "repeats":
            cited.update(p.src)
    for path in sorted(glob.glob(os.path.join(vouchers_dir or "", "RUN-%s-*" % key, "V-*.json"))):
        try:
            data = load_json(path)
        except (ValueError, OSError, UnicodeDecodeError):
            continue
        v = data.get("voucher") if isinstance(data, dict) else None
        if not isinstance(v, dict):
            continue
        if v.get("kind") != "answer" or v.get("choice") not in REJECT_CHOICES:
            continue
        if v.get("voucher_id") not in cited and v.get("decision") not in cited:
            out.append("%s (%s) was rejected, but no correction or repeats proposal cites it"
                       % (v.get("decision"), v.get("voucher_id")))
    return out


# ---------- git side ----------

def open_branches(repo, brain_dir) -> tuple:
    """(ids, {proposal hash: branch}) on open Brain branches (local and origin): parallel reflects and
    `brain entry` never pick an id that is already taken there, and a re-run never writes the same proposal twice."""
    refs = git_out(repo, "for-each-ref", "--format=%(refname)", "refs/heads/brain/", "refs/remotes/origin/brain/")
    ids, props = set(), {}
    for ref in refs.split():
        b = E.Brain.from_git(repo, ref, brain_dir)
        ids |= b.used_ids()
        short = ref.replace("refs/heads/", "").replace("refs/remotes/", "")
        for h in b.props():
            props.setdefault(h, short)
    return ids, props


def used_on_branches(repo, brain_dir) -> set:
    return open_branches(repo, brain_dir)[0]


def branch_name(repo, key) -> str:
    refs = git_out(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/brain/",
                   "refs/remotes/origin/brain/").split()
    names = {r.split("/", 1)[1] if r.startswith("origin/") else r for r in refs}
    n = 1
    while "brain/%s-reflect-%d" % (key, n) in names:
        n += 1
    return "brain/%s-reflect-%d" % (key, n)


def mr_description(key, ops, warnings, source) -> str:
    rows = []
    for op in ops:
        p = op.p
        how = []
        if any(brainlint.DOC_SRC_RE.match(s) for s in p.src):
            how.append("来自文档：单条审")
        if brainlint.RELAX_RE.search(E.norm(p.body)):
            how.append("放宽类：Builder + 安全两人批")
        if op.note:
            how.append(op.note)
        fk = op.kw.get("file") or (op.result or "?")[:1]
        fk = E.LETTER_FILE.get(fk, fk)
        rows.append("| %s | %s | %s | %s | %s | %s |" % (
            op.result, fk, op.kw.get("type", ""), op.kind + (" " + op.kw["eid"] if op.kind != "new" and
                                                                op.kw.get("eid") != op.result else ""),
            ", ".join(p.src), "；".join(how) or "按文件负责人批"))
    out = ["Brain 回写：%s（来源：%s）" % (key, source), "",
           "| 条目 | 文件 | 类型 | 关系 | 出处 | 怎么审 |", "|---|---|---|---|---|---|"] + rows
    out += ["", "- 谁批：PRODUCT 归产品；TECH、IMPROVEMENT 归 Builder；PROJECT 两边各一人（CODEOWNERS）。",
            "- 由 `brain reflect` 生成，`brain lint` 已按 main 检查关系规则。正文请看 diff。"]
    if warnings:
        out += ["", "提醒："] + ["- " + w for w in warnings]
    return "\n".join(out) + "\n"


def source_text(repo, cfg, key, ref=None, from_file=None) -> tuple:
    """(text, label): the decisions.md of the run for `key` (working tree, or `ref`), or --from FILE."""
    if from_file:
        with open(from_file, encoding="utf-8") as f:
            return f.read(), os.path.basename(from_file)
    rel_run = "%s/%s/run.json" % (cfg["runs_dir"], key)
    if ref:
        p = git(repo, "show", "%s:%s" % (ref, rel_run), check=False)
        if p.returncode != 0:
            raise BrainError("%s has no %s" % (ref, rel_run))
        intent = _intent(p.stdout, "%s:%s" % (ref, rel_run))
        rel = "aidlc-docs/%s/decisions.md" % intent
        p = git(repo, "show", "%s:%s" % (ref, rel), check=False)
        if p.returncode != 0:
            raise BrainError("%s has no %s" % (ref, rel))
        return p.stdout, "%s:%s" % (ref, rel)
    path = os.path.join(repo, rel_run)
    if not os.path.exists(path):
        raise BrainError("%s not found here; pass --ref origin/%s after the run merged, or --from FILE"
                         % (rel_run, cfg["default_branch"]))
    with open(path, encoding="utf-8") as f:
        intent = _intent(f.read(), rel_run)
    rel = os.path.join("aidlc-docs", intent, "decisions.md")
    if not os.path.exists(os.path.join(repo, rel)):
        raise BrainError("%s not found" % rel)
    with open(os.path.join(repo, rel), encoding="utf-8") as f:
        return f.read(), rel


def _intent(raw, where) -> str:
    import json
    try:
        rec = json.loads(raw)
    except ValueError as e:
        raise BrainError("%s is not valid JSON: %s" % (where, e))
    intent = rec.get("intent") if isinstance(rec, dict) else None
    if not isinstance(intent, str) or not re.match(r"^[0-9]{6}-[a-z0-9][a-z0-9-]{0,80}$", intent):
        raise BrainError("%s has no valid intent" % where)
    return intent


def worktree_root(repo) -> str:
    return os.path.join(os.path.expanduser("~"), ".brain", "worktrees", os.path.basename(os.path.realpath(repo)))


def main_cfg(repo, base):
    """Kit config as on main (brain_dir and friends are not taken from a run branch)."""
    import json
    p = git(repo, "show", "%s:%s" % (base, config.KIT_CONFIG), check=False)
    if p.returncode != 0:
        return config.load(repo)
    return config.merged(config.DEFAULTS, json.loads(p.stdout))
