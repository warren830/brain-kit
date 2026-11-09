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

A second block kind records what is missing, for a later run (not the Brain):

  ```brain-gap
  title: 退款对账没有自动测试          one line: the next requirement's title
  kind: test                         knowledge | test | other
  refs: D-4, AC-2, src/pay/reconcile.py    optional
  ---
  What is missing and why it matters (the next requirement's text).
  ```

`--apply` files each new gap as an intent (intents.add, source gap: a claim/<KEY> the person running reflect owns),
skipping gaps filed before (same title and text), and lists them in the Brain MR description; the preview shows them.
"""
from __future__ import annotations

import glob
import os
import re

from . import brainlint, config, entries as E, intents
from .common import BrainError, git, git_out, load_json, parse_branch

FENCE_OPEN_RE = re.compile(r"^\s*(`{3,})\s*brain-proposal\s*$")
GAP_OPEN_RE = re.compile(r"^\s*(`{3,})\s*brain-gap\s*$")
GAP_FIELDS = ("title", "kind", "refs")
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


def _blocks(text, open_re, what, free=()) -> tuple:
    """([(fields, body, start line)], problems) for the fenced blocks `open_re` opens. Fields in `free` keep '#'."""
    spans, probs = _spans(text, open_re, what, free)
    return [(f, b, start) for f, b, start, _, closed in spans if closed], probs


def block_spans(text, open_re=FENCE_OPEN_RE) -> list:
    """[(first line index, end line index (exclusive), fields)] of the fenced blocks `open_re` opens, the fences
    included; a block that is not closed runs to the end. For tools that cut blocks out (golden ablation)."""
    return [(start - 1, end, f) for f, _, start, end, _ in _spans(text, open_re, "block")[0]]


def _spans(text, open_re, what, free=()) -> tuple:
    """([(fields, body, start line, end line index (exclusive), closed)], problems)."""
    blocks, probs = [], []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = open_re.match(lines[i])
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
            probs.append("line %d: %s block is not closed" % (start, what))
            blocks.append((_fields(lines[start:], free)[0], "", start, len(lines), False))
            break
        fields, body, more = _fields(lines[start:j], free, start)
        probs += more
        blocks.append((fields, body, start, j + 1, True))
        i = j + 1
    return blocks, probs


def _fields(block, free=(), start=0) -> tuple:
    """(fields, body, problems) of the lines inside one block."""
    fields, body, in_body, probs = {}, [], False, []
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
            if k not in free:
                v = re.sub(r"\s+#.*$", "", v)          # trailing "# comment" after a value
            v = v.strip()
            if k in fields:
                probs.append("line %d: %s given twice" % (start + n + 1, k))
            fields[k] = v
        else:
            probs.append("line %d: expected 'key: value' or '---'" % (start + n + 1))
    return fields, "\n".join(body), probs


def parse(text) -> tuple:
    """(proposals, problems) from a decisions.md (or any Markdown)."""
    blocks, probs = _blocks(text, FENCE_OPEN_RE, "brain-proposal")
    return [Proposal(f, b, n) for f, b, n in blocks], probs


class Gap:
    """A brain-gap block: something missing (knowledge, a test) that becomes the next requirement."""

    def __init__(self, fields, body, line):
        self.fields, self.line = fields, line
        self.body = intents.norm_body(body)
        self.title = " ".join(fields.get("title", "").split())
        self.kind = fields.get("kind", "")
        self.refs = [r.strip() for r in re.split(r"[,，]", fields.get("refs", "")) if r.strip()]
        self.hash = intents.digest("gap", self.title, self.body)

    def label(self):
        return "gap at line %d" % self.line


def parse_gaps(text) -> tuple:
    """(gaps, problems) from a decisions.md or a reflector's output."""
    blocks, probs = _blocks(text, GAP_OPEN_RE, "brain-gap", free=("title",))
    return [Gap(f, b, n) for f, b, n in blocks], probs


def plan_gaps(repo, cfg, gaps, key) -> tuple:
    """(rows, problems). Each row {"gap", "existing" (claim filed before, or None), "filed" (after file_gaps)}.
    A gap that fails the checks refuses the whole reflect, like a bad proposal."""
    rows, probs, seen = [], [], {}
    for g in gaps:
        where = g.label()
        unknown = set(g.fields) - set(GAP_FIELDS)
        if unknown:
            probs.append("%s: unknown fields %s" % (where, ", ".join(sorted(unknown))))
        if g.kind not in intents.GAP_KINDS:
            probs.append("%s: kind must be one of %s" % (where, ", ".join(intents.GAP_KINDS)))
        errs, _ = intents.problems("gap", g.title, g.body, (), g.refs, {"found_in": key})
        probs += ["%s: %s" % (where, e) for e in errs]
        if g.hash in seen:
            continue                     # the same gap twice in one file: once
        seen[g.hash] = g
        rows.append({"gap": g, "existing": None, "filed": None})
    if rows and not probs and cfg.get("tracker") == "none":
        docs, _ = intents.scan(repo)
        for r in rows:
            r["existing"] = intents.first_with(docs, r["gap"].hash)
    return rows, probs


def new_gaps(rows) -> list:
    return [r for r in rows if not r["existing"]]


def file_gaps(repo, cfg, rows, key, owner) -> list:
    """File the new gaps as intents (source gap). Rows filed before are left alone."""
    if cfg.get("tracker") != "none":
        return rows
    for r in rows:
        if r["existing"]:
            continue
        g = r["gap"]
        doc = intents.add(repo, cfg, "gap", g.title, g.body, refs=g.refs, owner=owner,
                          extra={"gap_kind": g.kind, "found_in": key})
        if doc.get("duplicate"):
            r["existing"] = doc
        else:
            r["filed"] = doc
    return rows


def gap_lines(rows, tracker="none") -> list:
    out = []
    for r in rows:
        g = r["gap"]
        if r["filed"]:
            what = "登记为 %s" % r["filed"]["key"]
        elif r["existing"]:
            what = "已登记为 %s，跳过" % r["existing"]["key"]
        elif tracker != "none":
            what = "tracker 是 %s：请在那里登记" % tracker
        else:
            what = "新需求（--apply 时登记，source gap）"
        out.append("  %-9s %-9s %s：%s" % ("gap", g.kind, g.title, what))
    return out


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
        if v.get("eval_oracle"):  # golden-task oracle answers are not real decisions
            continue
        if v.get("kind") != "answer" or v.get("choice") not in REJECT_CHOICES:
            continue
        items = v.get("rejected_items")
        if isinstance(items, list) and items:      # batch review: each rejected item needs its own correction
            for it in items:
                if it not in cited:
                    out.append("%s in %s (%s) was rejected, but no correction or repeats proposal cites %s"
                               % (it, v.get("decision"), v.get("voucher_id"), it))
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


def mr_description(key, ops, warnings, source, gaps=None) -> str:
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
    if gaps:
        out += ["", "缺口 → 下一个需求（`brain intent list` 能看到，`brain start <slug> --key <KEY>` 开工）："]
        out += ["- %s · %s · %s%s" % ((r["filed"] or r["existing"] or {}).get("key", "未登记"), r["gap"].kind,
                                     r["gap"].title, "" if r["filed"] else "（之前已登记）" if r["existing"]
                                     else "（tracker 不是 none：请在 tracker 里登记）") for r in gaps]
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
    """~/.brain/worktrees/<repo name>: the main repository's name, also when reflect runs inside a run's worktree
    (a worktree's own folder is named after the requirement, not the repo)."""
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False).stdout.strip()
    main = os.path.dirname(common) if common.endswith(os.sep + ".git") else (common or repo)
    return os.path.join(os.path.expanduser("~"), ".brain", "worktrees", os.path.basename(os.path.realpath(main)))


def main_cfg(repo, base):
    """Kit config as on main (brain_dir and friends are not taken from a run branch)."""
    import json
    p = git(repo, "show", "%s:%s" % (base, config.KIT_CONFIG), check=False)
    if p.returncode != 0:
        return config.load(repo)
    return config.merged(config.DEFAULTS, json.loads(p.stdout))


# ---------- the reflector session ----------

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFLECTOR_PROMPT = os.path.join(KIT_ROOT, "remote", "prompts", "brain-reflector.md")
REFLECTOR_AGENT = os.path.join(KIT_ROOT, "remote", "agents", "brain-reflector.json")


def _has(repo, rev) -> bool:
    return git(repo, "cat-file", "-e", rev, check=False).returncode == 0


def locate_run(repo, cfg, key) -> dict:
    """Where the accepted run is, from the refs already on this machine; nothing is fetched and no ref moves.
    Order: the default branch once merged (origin/<main>, then <main>), else its run branch (origin's copy first,
    then a local branch). Only when none is here, `git ls-remote origin` (read-only) looks for the run branch; the
    person fetches it (the returned "fetch" command). Returns {"ref", "read" (a revision to read run.json from, or
    None when its commit is not here yet), "fetch" (a command, or None)}."""
    rel = "%s/%s/run.json" % (cfg["runs_dir"], key)
    main, prefix = cfg["default_branch"], cfg["branch_prefix"]
    has_origin = git(repo, "remote", "get-url", "origin", check=False).returncode == 0
    for ref in (("origin/" + main,) if has_origin else ()) + (main,):
        if _has(repo, "%s:%s" % (ref, rel)):
            return {"ref": ref, "read": ref, "fetch": None}
    for space, short in (("refs/remotes/origin/%s/" % prefix, "origin/"), ("refs/heads/%s/" % prefix, "")):
        for full in git_out(repo, "for-each-ref", "--format=%(refname)", space).split():
            name = prefix + "/" + full[len(space):]
            b = parse_branch(name, prefix)
            if b and b["key"] == key:
                return {"ref": short + name, "read": short + name, "fetch": None}
    if has_origin:
        p = git(repo, "ls-remote", "origin", "refs/heads/%s/*" % prefix, check=False)
        if p.returncode != 0:
            why = ((p.stderr or p.stdout).strip().splitlines() or ["exit %d" % p.returncode])[-1][:200]
            raise BrainError("no run for %s on this machine (not on %s, no %s/*/%s-* branch here), and origin could not "
                             "be asked (git ls-remote origin failed: %s; offline?). --draft does not fetch: once origin "
                             "can be reached, run `git fetch origin`, or pass --ref" % (key, main, prefix, key, why))
        out = p.stdout
        for line in sorted(out.splitlines()):
            sha, _, full = line.partition("\t")
            name = full[len("refs/heads/"):]
            b = parse_branch(name, prefix)
            if b and b["key"] == key:
                return {"ref": "origin/" + name, "read": sha if _has(repo, sha + "^{commit}") else None,
                        "fetch": "git -C %s fetch origin +refs/heads/%s:refs/remotes/origin/%s" % (repo, name, name)}
    raise BrainError("no run for %s here (not on %s, no %s/*/%s-* branch on this machine%s). --draft does not fetch: "
                     "run `git fetch origin` first, or pass --ref" % (key, main, prefix, key,
                                                                      " or on origin" if has_origin else ""))


def run_ref(repo, cfg, key) -> str:
    """The ref of the accepted run (locate_run; read-only)."""
    return locate_run(repo, cfg, key)["ref"]


def draft(repo, cfg, key, ref=None, vouchers_dir=None) -> str:
    """What a person does to run the reflector in a local KiroCrew session, and the exact message to send it.
    Print-only: nothing is fetched, no ref, worktree or file changes (locate_run)."""
    loc = {"ref": ref, "read": ref, "fetch": None} if ref else locate_run(repo, cfg, key)
    ref = loc["ref"]
    rel = "%s/%s/run.json" % (cfg["runs_dir"], key)
    if loc["read"]:
        p = git(repo, "show", "%s:%s" % (loc["read"], rel), check=False)
        if p.returncode != 0:
            raise BrainError("%s has no %s" % (ref, rel))
        intent = _intent(p.stdout, "%s:%s" % (ref, rel))
        intent_line = "- intent: %s (aidlc-docs/%s/)" % (intent, intent)
    else:
        intent_line = "- intent: the \"intent\" field of %s in this workspace (aidlc-docs/<intent>/)" % rel
    wt = os.path.join(worktree_root(repo), "reflector-%s" % key)
    out = "%s/%s/reflect-proposals.md" % (cfg["runs_dir"], key)
    with open(REFLECTOR_PROMPT, encoding="utf-8") as f:
        prompt = f.read().rstrip()
    message = "\n".join([
        "You are brain-reflector for %s, in a fresh session. This run:" % key,
        "- key: %s" % key,
        "- ref: %s (this workspace is a detached checkout of it)" % ref,
        intent_line,
        "- workspace: %s" % wt,
        "- vouchers: %s (answers the bridge signed; may be missing on this machine)"
        % os.path.join(vouchers_dir or "<brain-intake>/vouchers", "RUN-%s-*" % key),
        "- output: %s (in this workspace only)" % out,
        "",
        prompt])
    first = ["1. 给它建一个单独的工作区（不动你现在的目录）："]
    if loc["fetch"]:
        first = ["1. 这个 run 的分支只在 origin 上，先取下来，再给它建一个单独的工作区（不动你现在的目录）：",
                 "     " + loc["fetch"]]
    steps = [
        "brain reflect --draft %s：在本机 KiroCrew 里开一个新会话，让它读完这个 run，写出 Brain 提案和缺口。" % key,
        "（只看本机已有的分支，不 fetch、不改任何东西；要最新的，先 git fetch origin 再跑一次。）",
        ""] + first + [
        "     git -C %s worktree add --detach %s %s" % (repo, wt, ref),
        "2. 装 agent（一次）：把 %s 拷到 ~/.kiro/agents/brain-reflector.json，" % REFLECTOR_AGENT,
        "   prompt 改成 file://%s；不装也行，用默认 agent，下面的消息里已经带着全部说明。" % REFLECTOR_PROMPT,
        "3. 在 KiroCrew 里新建会话：工作目录选 %s，agent 选 brain-reflector，模型 claude-opus-5.5；" % wt,
        "   把下面两条 ==== 之间的内容整段发给它。",
        "4. 它写完以后，在项目里先预览，再写入（写 Brain 分支、登记缺口）：",
        "     brain reflect --key %s --from %s" % (key, os.path.join(wt, out)),
        "     brain reflect --key %s --from %s --apply" % (key, os.path.join(wt, out)),
        "5. 用完删掉工作区：git -C %s worktree remove --force %s" % (repo, wt),
        "",
        "==== 发给会话的内容 ====",
        message,
        "==== 结束 ===="]
    return "\n".join(steps) + "\n"
