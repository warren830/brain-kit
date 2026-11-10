"""`brain lint`: the Brain's rules. Static checks on the tree, and with a base (an MR's diff base) the relation
rules on what changed. Errors block the MR (CI job brain-lint); warnings tell the reviewer what to look at.

Static: only the known files (no symlinks, subdirectories, other names, non-UTF-8); ids well formed, unique across
files and archives; the side letter matches the file; the type is one the file takes; headers on their own line,
known attributes only, each once, values well formed (sources are references, not text); archive entries carry
their status; replaces-targets are archived; PURGED.md lines are well formed.
Against a base:
  an existing entry may keep its text (same) or grow at the end (adds-to); any other edit is refused: replace it
  an entry may leave a file only into its archive (replaced / withdrawn) or through PURGED.md, and a purge needs
  an approver listed in purge_approvers (.brain/kit.json on the target branch)
  archives and PURGED.md only grow
  a new entry is rel:new, or rel:replaces X with X archived in the same change
  every new piece of text (new entries, appended text, withdraw notes, text outside entries, PURGED.md's notes,
  AGENTS.md) carries no secrets, personal data, amounts, contract numbers, masked placeholders or instruction lines
Against the target branch's current Brain (CI): new ids must not already be taken there (parallel MRs).
Warnings: entries from documents (review alone), entries that relax tests/logging/auth/dependencies
(Builder + security), text outside entries, repeats >= 2 (make it a gate), one term defined twice, legacy ids.
"""
from __future__ import annotations

import re
from collections import Counter

from . import entries as E
from .intake import checks

PLACEHOLDER_RE = re.compile(r"\[(?:%s)-[0-9]+\]" % "|".join(sorted(set(checks.Masker.LABEL.values()))))
DOC_SRC_RE = re.compile(r"^(?:PKG-|%s)" % E.SEGMENT_REF)
RELAX_RE = re.compile(
    r"(?i)(跳过|不用|不需要|无需|免|关闭|关掉|去掉|放宽|降低|绕过|skip|disable|bypass|turn off|no longer|without|relax)"
    r"[^\n]{0,16}?(测试|test|日志|log|鉴权|认证|授权|auth|校验|检查|check|依赖|dependenc|审批|评审|review|lint|"
    r"coverage|覆盖率|加密|encrypt|TLS|HTTPS|权限|permission)")
TERM_RE = re.compile(r"^\s*(?:[-*]\s*)?\*\*([^*]{1,40})\*\*")
EMAIL_CHAR_RE = re.compile(r"[A-Za-z0-9._%+-]")
PARAM_RE = re.compile(r"^\$[0-9]{1,2}$")          # $1, $2 in SQL, shell, awk: a parameter, not dollars


def _scan(text) -> list:
    """checks.scan, except that one capitalised word after @ is a review hit, not personal data: in a TECH entry it is
    as often a Java annotation (@Transactional, @MockBean) as a person (@Alice). Role words (@Service, @Test) are not
    even that. The other rules still read the word (an Owner column, an e-mail address); where one of them reads it as
    a possible person (| Owner | @Alice |), the @ settles it: personal data."""
    lone = [m for m in checks.MENTION_RE.finditer(text) if re.fullmatch(r"[A-Z][a-zA-Z]+", m.group(1))
            and not (m.start() and EMAIL_CHAR_RE.match(text[m.start() - 1]))]
    if not lone:
        return checks.scan(text)
    chars = list(text)
    for m in lone:
        chars[m.start()] = " "                       # same length: the offsets hold
    hits = checks.scan("".join(chars))
    for m in lone:
        near = [h for h in hits if h.cat not in ("link", "instruction") and h.start < m.end() and m.start() < h.end]
        if any(h.cat == "review" and h.kind == "person" for h in near):
            hits = [h for h in hits if h not in near] + [checks.Hit("pii", "person", m.start(), m.end(), m.group(0))]
        elif checks.latin_person(m.group(1))[0] and not near:
            hits.append(checks.Hit("review", "person", m.start(), m.end(), m.group(0)))
    return hits


def content_problems(text, offset=0) -> tuple:
    """(errors, warnings) for text that is about to enter the Brain. With `offset`, the text before it is context
    only (for example the old part of an entry that grows): only findings that start at or after it count."""
    errs, warns = [], []
    for h in _scan(text):
        if h.end <= offset:
            continue
        if h.cat == "secret":
            errs.append("looks like a secret (%s)" % h.kind)
        elif h.cat == "pii":
            errs.append("personal data (%s)" % h.kind)
        elif h.cat == "business" and h.kind == "amount" and PARAM_RE.match(h.value):
            continue
        elif h.cat == "business" and h.kind in ("amount", "contract"):
            errs.append("%s %r: write the rule, not the number" % ("amount" if h.kind == "amount" else
                                                                    "contract number", h.value))
        elif h.cat == "business":
            warns.append("names a company (%s): keep it only if the team may share it" % h.value)
        elif h.cat == "review":  # may be a name or a password, may be a plain word: a person decides
            warns.append("may be personal data or a secret (%s): check it before this is merged" % h.kind)
        elif h.cat == "instruction":
            errs.append("instruction-like line (%s): the Brain is read by every run" % h.kind)
    tail = text[offset:]
    for m in PLACEHOLDER_RE.finditer(tail):
        errs.append("masked placeholder %s: nobody knows what it stands for" % m.group(0))
    if RELAX_RE.search(tail):
        warns.append("relaxes a safety or quality rule: needs Builder and security approval")
    return sorted(set(errs)), sorted(set(warns))


def _check_values(where, e, errors):
    for k in e.dups:
        errors.append("%s: attribute %s given twice" % (where, k))
    unknown = sorted(set(e.attrs) - E.KNOWN_KEYS)
    if unknown:
        errors.append("%s: unknown header attributes %s" % (where, ", ".join(unknown)))
    if e.extra:
        errors.append("%s: header has unreadable parts" % where)
    side = e.attrs.get("side")
    if side is not None and side not in E.SIDES:
        errors.append("%s: side must be one of %s" % (where, ", ".join(E.SIDES)))
    for s in e.src:
        if not E.SRC_ITEM_RE.match(s):
            errors.append("%s: source %r is not a reference (D01#S003, PKG-…, D-3, V-<10 hex>, RUN-…, an entry id, "
                          "an intent, a key)" % (where, s[:40]))
    for h in E.split_src(e.attrs.get("prop")):
        if not E.PROP_RE.match(h):
            errors.append("%s: prop %r is not a proposal hash" % (where, h[:40]))
    if "status" in e.attrs and e.attrs["status"] not in ("replaced", "withdrawn"):
        errors.append("%s: status must be replaced or withdrawn" % where)
    if "by" in e.attrs and not E.NEW_ID_RE.match(e.attrs["by"]) and not E.LEGACY_ID_RE.match(e.attrs["by"]):
        errors.append("%s: by must be an entry id" % where)
    if "archived" in e.attrs and not E.DATE_RE.match(e.attrs["archived"]):
        errors.append("%s: archived must be a date" % where)
    rel, target = e.rel
    if target and not (E.NEW_ID_RE.match(target) or E.LEGACY_ID_RE.match(target)):
        errors.append("%s: rel target %r is not an entry id" % (where, target[:40]))
    if "repeats" in e.attrs and not re.match(r"^[0-9]{1,4}$", e.attrs["repeats"]):
        errors.append("%s: repeats must be a number" % where)


def _static(b, errors, warnings):
    errors += list(b.problems)
    seen, archived = {}, b.archived()
    purged = b.purged()
    for line in b.purged_text.split("\n"):
        if line.strip() and not line.startswith(("#", ">")) and not E.PURGED_LINE_RE.match(line):
            errors.append("PURGED.md: malformed line %r (- <id> · <date> · pii|secret|confidential · approved by "
                          "<user>)" % line[:80])
    for f in b.files.values():
        for n in f.loose:
            errors.append("%s:%d: entry header must be on its own line" % (f.name, n))
        fk = f.key
        for e in f.entries():
            where = "%s:%d" % (f.name, e.line)
            if E.NEW_ID_RE.match(e.id):
                pass
            elif E.LEGACY_ID_RE.match(e.id):
                warnings.append("%s: legacy sequential id %s; new entries use <side>-<KEY>-<n>" % (where, e.id))
            else:
                errors.append("%s: bad id %r (want e.g. T-OMS-12-1)" % (where, e.id))
            if e.id in seen:
                errors.append("%s: id %s already used at %s" % (where, e.id, seen[e.id]))
            else:
                seen[e.id] = where
            if e.id in purged:
                errors.append("%s: %s was purged; it may not come back" % (where, e.id))
            _check_values(where, e, errors)
            if not e.text():
                errors.append("%s: entry %s has no text" % (where, e.id))
            spec = E.FILES[fk]
            if e.id[:1] != spec["letter"]:
                errors.append("%s: %s entries start with %s-, not %s" % (where, fk, spec["letter"], e.id))
            if e.type not in spec["types"]:
                errors.append("%s: type must be one of %s (%s)" % (where, ", ".join(spec["types"]), fk))
            if e.attrs.get("side") and e.attrs["side"] != spec["side"]:
                errors.append("%s: side must be %s in %s" % (where, spec["side"], fk))
            rel, target = e.rel
            if rel and rel not in E.RELS:
                errors.append("%s: rel must be one of %s" % (where, ", ".join(E.RELS)))
            if e.repeats >= 2:
                warnings.append("%s: %s came back %d times: make it a gate (a check that fails), not more text"
                                % (where, e.id, e.repeats))
            if f.archive:
                st = e.attrs.get("status")
                if st not in ("replaced", "withdrawn"):
                    errors.append("%s: archived entry %s needs status:replaced or status:withdrawn" % (where, e.id))
                if st == "replaced" and not e.attrs.get("by"):
                    errors.append("%s: replaced entry %s needs by:<new id>" % (where, e.id))
            else:
                for k in ("status", "by", "archived"):
                    if k in e.attrs:
                        errors.append("%s: %s has %s: but is not in an archive" % (where, e.id, k))
                if rel == "replaces" and target not in purged:
                    if not target or target not in archived:
                        errors.append("%s: replaces %s, but %s is not in an *-archive.md" % (where, target, target))
                    elif archived[target][1].attrs.get("by") != e.id:
                        errors.append("%s: %s is archived as replaced by %s, not %s" % (
                            where, target, archived[target][1].attrs.get("by"), e.id))
    terms = {}
    for eid, (f, e) in b.main().items():
        m = TERM_RE.match(e.text().split("\n")[0])
        if m and e.type in ("model", "decision", "principle"):
            t = m.group(1).strip().lower()
            if t in terms and terms[t] != eid:
                warnings.append("%s: term '%s' is also defined in %s (same name, different entries)"
                                % (eid, m.group(1).strip(), terms[t]))
            terms.setdefault(t, eid)


def _new_text(where, text, errors, warnings, src, offset=0):
    errs, warns = content_problems(text, offset)
    errors += ["%s: %s" % (where, x) for x in errs]
    warnings += ["%s: %s" % (where, x) for x in warns]
    if any(DOC_SRC_RE.match(s) for s in src):
        warnings.append("%s: comes from a requirement document: review this entry on its own" % where)


def _boilerplate():
    """Lines brain-kit itself writes outside entries (templates, archive heads): not worth a warning."""
    import os
    lines = set()
    tdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "brain")
    for k in E.FILES:
        lines.update(E.ARCHIVE_HEAD.format(name=k).split("\n"))
        lines.add("# %s" % k)
        try:
            with open(os.path.join(tdir, k + ".md"), encoding="utf-8") as f:
                lines.update(f.read().split("\n"))
        except OSError:
            pass
    return lines


BOILERPLATE = _boilerplate()


def _added(lines, old_lines) -> list:
    """The non-blank lines of `lines` that `old_lines` does not have (as many times)."""
    old = Counter(old_lines)
    added = []
    for line in lines:
        if old[line]:
            old[line] -= 1
        elif line.strip():
            added.append(line)
    return added


def _free_text(b, base, errors, warnings):
    """Lines outside entries reach every run as well: new ones get the same content checks. So do PURGED.md's
    headings and notes (its purge lines have a fixed form)."""
    added = _added(b.purged_text.split("\n"), base.purged_text.split("\n"))
    errs, warns = content_problems("\n".join(added))
    errors += ["%s: %s" % (E.PURGED_FILE, x) for x in errs]
    warnings += ["%s: %s" % (E.PURGED_FILE, x) for x in warns]
    for name, f in sorted(b.files.items()):
        added = _added(f.free_lines(), base.files[name].free_lines() if name in base.files else [])
        if not added:
            continue
        text = "\n".join(added)
        errs, warns = content_problems(text)
        errors += ["%s (outside entries): %s" % (name, x) for x in errs]
        warnings += ["%s (outside entries): %s" % (name, x) for x in warns]
        prose = [l for l in added if l not in BOILERPLATE and not E.HEADING_RE.match(l)]
        if prose:
            warnings.append("%s: %d line(s) of text outside entries (not tracked as knowledge; read them): %s"
                            % (name, len(prose), prose[0][:60]))


def _only_grows(where, be, he, errors):
    """Sources and proposal hashes only grow, repeats never go down: in the file and on the way into the archive."""
    if he.repeats < be.repeats:
        errors.append("%s: repeats went down (%d -> %d)" % (where, be.repeats, he.repeats))
    lost = [s for s in be.src if s not in he.src] + \
        [h for h in E.split_src(be.attrs.get("prop")) if h not in E.split_src(he.attrs.get("prop"))]
    if lost:
        errors.append("%s: sources or proposal hashes removed (%s); they only grow" % (where, ", ".join(lost)))


def _diff(b, base, errors, warnings, purge_approvers):
    bm, ba, bp = base.main(), base.archived(), base.purged()
    hm, ha, hp = b.main(), b.archived(), b.purged()
    for eid in bp:
        if eid not in hp or hp[eid] != bp[eid]:
            errors.append("%s: removed or changed in PURGED.md (it only grows)" % eid)
    for eid, line in hp.items():
        if eid in bp:
            continue
        m = E.PURGED_LINE_RE.match(line)
        who = m.group(4) if m else ""
        if purge_approvers is None or who not in purge_approvers:
            errors.append("%s: purged, but %s is not in purge_approvers (.brain/kit.json on the target branch)"
                          % (eid, who or "nobody"))
        else:
            warnings.append("%s: purged in this change: %s must approve this MR; the text is still in git history "
                            "until someone rewrites it" % (eid, who))
        if eid not in bm and eid not in ba:
            errors.append("%s: purged, but it was never an entry" % eid)
    for eid, (bf, be) in bm.items():
        if eid in hm:
            hf, he = hm[eid]
            where = "%s (%s)" % (eid, hf.name)
            if hf.key != bf.key or he.type != be.type or he.attrs.get("side") != be.attrs.get("side"):
                errors.append("%s: file, type or side changed: replace the entry instead" % where)
            old, new = be.text(), he.text()
            if new != old:
                if new.startswith(old + "\n"):
                    _new_text(where, new, errors, warnings, he.src, offset=len(old))
                else:
                    errors.append("%s: text edited in place; only same, adds-to (old text first), replaces, "
                                  "withdraws are allowed" % where)
            _only_grows(where, be, he, errors)
        elif eid in ha:
            af, ae = ha[eid]
            where = "%s (%s)" % (eid, af.name)
            if af.key != bf.key:
                errors.append("%s: archived into %s, but it was in %s" % (eid, af.name, bf.name))
            if ae.type != be.type or ae.attrs.get("side") != be.attrs.get("side"):
                errors.append("%s: type or side changed on the way into the archive; archive the entry as it was"
                              % where)
            old, new = be.text(), ae.text()
            if new != old and not new.startswith(old + "\n"):
                errors.append("%s: archived with different text; archive the entry as it was" % eid)
            elif new != old:
                _new_text(where, new, errors, warnings, [], offset=len(old))
            _only_grows(where, be, ae, errors)
            if ae.attrs.get("status") == "replaced" and ae.attrs.get("by") not in hm and ae.attrs.get("by") not in ha:
                errors.append("%s: archived as replaced by %s, which does not exist" % (eid, ae.attrs.get("by")))
        elif eid in hp:
            pass
        else:
            errors.append("%s: deleted; entries leave only by replaces, withdraws (into the archive) or purge"
                          % eid)
    for eid, (af, ae) in ba.items():
        if eid in ha:
            if ha[eid][1].lines() != ae.lines():
                errors.append("%s: changed inside %s (archives only grow)" % (eid, af.name))
        elif eid not in hp:
            errors.append("%s: removed from %s (archives only grow)" % (eid, af.name))
    for eid, (hf, he) in hm.items():
        if eid in bm:
            continue
        where = "%s (%s)" % (eid, hf.name)
        if eid in ba:
            errors.append("%s: archived id brought back; write a new entry" % where)
            continue
        rel, target = he.rel
        if rel == "replaces":
            if target not in bm:
                errors.append("%s: replaces %s, which is not a current entry" % (where, target))
        elif rel != "new":
            errors.append("%s: a new entry is rel:new or rel:replaces <id>, not %s" % (where, rel or "(none)"))
        if not he.src:
            errors.append("%s: a new entry needs src" % where)
        _new_text(where, he.text(), errors, warnings, he.src)
    for eid in ha:
        if eid not in ba and eid not in bm:
            errors.append("%s: in an archive but never was a current entry" % eid)
    _free_text(b, base, errors, warnings)


def _target(b, base, target, errors):
    """Parallel MRs: ids this change adds must still be free on the target branch as it is now."""
    taken = target.used_ids()
    mine = base.used_ids()
    for eid, (f, e) in list(b.main().items()) + list(b.archived().items()):
        if eid not in mine and eid in taken:
            errors.append("%s: this id was taken on the target branch meanwhile (another MR merged first): close "
                          "this MR and run brain reflect again (it writes the proposals again on a new branch, with "
                          "free ids)" % eid)


def check_agents(head_text, base_text):
    """AGENTS.md is read by every run too: new lines get the content checks."""
    errs, warns = content_problems("\n".join(_added((head_text or "").split("\n"), (base_text or "").split("\n"))))
    return ["AGENTS.md: " + x for x in errs], ["AGENTS.md: " + x for x in warns]


def check(brain_dir, base=None, target=None, purge_approvers=None):
    """(errors, warnings). `base`: the Brain at the diff base (entries.Brain.from_git); `target`: the Brain on the
    target branch now; `purge_approvers`: GitLab users allowed to approve a purge (None: nobody)."""
    b = E.Brain.load(brain_dir) if isinstance(brain_dir, str) else brain_dir
    errors, warnings = [], []
    _static(b, errors, warnings)
    if base is not None:
        _diff(b, base, errors, warnings, purge_approvers)
        if target is not None:
            _target(b, base, target, errors)
    return errors, warnings


def next_id(brain_dir, side, key) -> str:
    return E.Brain.load(brain_dir).next_id(key, side)
