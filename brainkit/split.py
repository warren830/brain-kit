"""`brain split`: a big requirement (the parent) becomes small ones (its children) that different people claim and
build in parallel, each as one unit of the parent's AI-DLC Lean intent.

The split is Lean's own Units Generation, approved by a person: the parent's run goes through Inception as usual,
and inception/application-design/unit-of-work.md lists each unit (name, owner team, paths, depends on, contracts,
stories / ACs). brain-kit reads Lean only through its files: the ledger (aidlc-docs/audit/*.jsonl), the pages, and
unit-of-work.md the way Lean's Project.units() reads it. A split is filed only when, on the committed parent branch:
  - requirements.md and application-design.md are approved and unchanged since (REQUIREMENTS + DESIGN, or INCEPTION);
  - the last Units Generation record (`lean stage done units`) is of unit-of-work.md as it is now, and that content
    was recorded before the DESIGN approval: Lean's DESIGN hash covers application-design.md only, so this is how
    the person's approval reaches the split (docs/lean-requests.md, R1);
  - the intent is not released, and there are at least two units, each with Paths.

Children are claims (intents.py): source "split", no owner (filed_by the parent's owner) until `brain claim` takes
one, and parent, unit, expected_paths (the unit's Paths), depends_on (the children of the units it depends on),
contracts, a title and ACs from the stories and acceptance criteria Lean links to the unit, and where the split came
from. One open child per parent and unit: filing is idempotent. A new split (a new DESIGN approval) files new units,
updates the children nobody has taken, closes the unowned ones whose unit is gone, and only reports what changed for
a child someone has claimed or started: a claimed child is never re-pointed (its owner takes the current split
when they start it).

A child run (start_child) continues the parent's intent for its unit: its own branch <prefix>/<you>/<KEY>-<slug> from
the parent's run branch, in its own git worktree (~/.brain/worktrees/<repo>/<KEY>), which is its own Lean clone with
Lean team = the unit (`lean init --team`, `lean use <the parent's intent>`). It starts only when every dependency has
merged into the parent's branch, or (--on DEP) from the branch of the one that has not. Its MR goes into the parent's
branch; the parent's owner reviews and releases once every child has merged (families()).
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from . import brainlint, claims, intents, lean, runrec
from . import mrdesc as M
from .common import (BrainError, SLUG_RE, branch_name, git, git_out, iso, parse_branch, paths_overlap, utcnow,
                     write_json)

UNITS_DOC = "inception/application-design/unit-of-work.md"
STORIES_DOC = "inception/user-stories/stories.md"
STAGE_EVENTS = ("STAGE_STARTED", "STAGE_COMPLETED", "STAGE_SKIPPED")
ID_RE = re.compile(r"(?<![A-Za-z0-9-])((?:US|AC|BR)-[1-9][0-9]{0,3})(?![0-9])")
# a range of ids in Stories / ACs: AC-1..AC-3, AC-1..3, AC-1~3, AC-1–3; spaced dashes and words (AC-1 – AC-3,
# AC-1 to AC-3, AC-1 到 AC-3) only with both ends named ("US-1 – 3 screens" is not a range)
RANGE_RE = re.compile(r"(?<![A-Za-z0-9-])(US|AC|BR)-([1-9][0-9]{0,3})"
                      r"(?:\s*(?:\.{2,3}|…|~|～)\s*(?:(US|AC|BR)-)?|[–—](?:(US|AC|BR)-)?"
                      r"|\s*(?:[–—]|\bto\b|到|至)\s*(US|AC|BR)-)"
                      r"([1-9][0-9]{0,3})(?![0-9])")
# a Stories / ACs column, by its whole name ("Data store" is not one): stories, ACs, or both (Stories / ACs)
_S = r"(?:user\s+)?stor(?:y|ies)(?:\s+ids?)?|用户故事|故事"
_A = r"acs?(?:\s+ids?)?|acceptance\s+criteria|验收标准|验收条件"
_J = r"\s*(?:/|&|\+|,|\band\b|和|与|及)\s*"
STORIES_COL_RE = re.compile(r"^(?:(?:{s})(?:{j}(?:{a}))?|(?:{a})(?:{j}(?:{s}))?)$".format(s=_S, a=_A, j=_J))
LOOSE_RANGE_RE = re.compile(r"(?<![A-Za-z0-9-])(?:US|AC|BR)-[0-9]+(?:\s*(?:\.{2,3}|…|~|～)|\s*[–—]\s*[0-9])")
CLONE_RE = re.compile(r"^[0-9a-f]{6}$")
FIELDS = ("title", "body", "acs", "refs", "expected_paths", "depends_on", "contracts")
BEGIN_MARK = "<!-- lean:begin progress -->"          # aidlc_lean.py BEGIN_MARK / END_MARK
END_MARK = "<!-- lean:end progress -->"


def lean_team(unit) -> str:
    """The team name `lean init --team <unit>` stores (Lean keeps letters, digits and _)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", unit)


def _sha(b) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------- unit-of-work.md, as Lean reads it (aidlc_lean.py first_table, table_cells, split_paths, units) ----------

def _lean_cells(line) -> list:
    s = line.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") else s
    return [c.strip() for c in s.split("|")]


def _lean_first_table(text):
    """(header cells, rows of cells) of the first Markdown table outside HTML comments (Lean's first_table)."""
    lines = re.sub(r"(?s)<!--.*?-->", "", text or "").splitlines()
    for i in range(len(lines) - 1):
        sep = lines[i + 1].strip()
        if lines[i].strip().startswith("|") and "---" in sep and re.match(r"^[|:\- \t]+$", sep):
            rows = []
            for line in lines[i + 2:]:
                if not line.strip().startswith("|"):
                    break
                rows.append(_lean_cells(line))
            return _lean_cells(lines[i]), rows
    return [], []


def _lean_paths(cell) -> list:
    cell = cell.replace("`", " ").replace("<br>", " ")
    return [p[2:] if p.startswith("./") else p for p in re.split(r"[,;\s]+", cell) if p and p not in ("-", "—")]


def _col(header, test):
    return next((i for i, h in enumerate(header) if test(h.lower())), None)


def _cell(cells, i) -> str:
    return cells[i] if i is not None and i < len(cells) else ""


def _items(cell) -> list:
    """A cell's list items (split at , ; and <br>), without the empty markers."""
    out = []
    for x in re.split(r"[,;]|<br>", (cell or "").replace("`", "")):
        x = " ".join(x.split()).strip("* ")
        if x and x.lower() not in ("-", "—", "none", "n/a"):
            out.append(x)
    return out


def _ids(cell, unit, warnings) -> list:
    """The US-/AC-/BR- ids in a Stories / ACs cell, in order, ranges (AC-1..AC-3) expanded. A range that cannot be
    expanded keeps its two ends and adds a warning: an id is never dropped silently."""
    cell, out, pos = cell or "", [], 0

    def plain(part):
        out.extend(ID_RE.findall(part))
        for m in LOOSE_RANGE_RE.finditer(part):
            warnings.append("unit %s: %r in Stories / ACs reads like a range brain-kit cannot expand: write "
                            "AC-n..AC-m or the ids it means" % (unit, m.group(0).strip()))

    for m in RANGE_RE.finditer(cell):
        plain(cell[pos:m.start()])
        pos = m.end()
        kind, a, b = m.group(1), int(m.group(2)), int(m.group(6))
        kind2 = m.group(3) or m.group(4) or m.group(5) or kind
        why = ("it mixes %s and %s" % (kind, kind2) if kind2 != kind else "it runs backwards" if b < a else
               "it has more than %d ids" % intents.LIMITS["acs"] if b - a >= intents.LIMITS["acs"] else "")
        if why:
            warnings.append("unit %s: %r in Stories / ACs is not expanded (%s): only %s-%d and %s-%d are taken; "
                            "write the ids it means" % (unit, m.group(0), why, kind, a, kind2, b))
            out += ["%s-%d" % (kind, a), "%s-%d" % (kind2, b)]
        else:
            out += ["%s-%d" % (kind, n) for n in range(a, b + 1)]
    plain(cell[pos:])
    return out


def parse_units(text, warnings=None) -> tuple:
    """(units, problems) of unit-of-work.md. A unit: {name, owner, paths, depends_on (unit names), contracts,
    stories (US-/AC-/BR- ids, ranges expanded), status}. Names and paths as Lean's Project.units() reads them (first
    table, first column, the first header with "path" / "owner" or "team"); the other columns as the template names
    them, Stories / ACs by the whole name (every such column). `warnings` (a list) gets what is read but not as
    written: a range that cannot be expanded, no Stories / ACs column."""
    warnings = [] if warnings is None else warnings
    header, rows = _lean_first_table(text)
    pcol = _col(header, lambda h: "path" in h)
    ocol = _col(header, lambda h: "owner" in h or "team" in h)
    dcol = _col(header, lambda h: "depend" in h)
    ccol = _col(header, lambda h: "contract" in h)
    scols = [i for i, h in enumerate(header) if STORIES_COL_RE.match(" ".join(re.sub(r"[`*]", " ", h.lower()).split()))]
    stcol = _col(header, lambda h: "status" in h)
    units, problems, seen = [], [], set()
    for cells in rows:
        name = cells[0].strip().strip("`*").strip() if cells else ""
        if not name or name in ("-", "...", "…") or "<" in name or "{{" in name:
            continue
        if not intents.UNIT_RE.match(name) or name in intents.RESERVED_UNITS:
            problems.append("unit %r cannot be a folder name (Lean ignores it): letters, digits . _ -, not plans or "
                            "build-and-test" % name)
            continue
        if name in seen:
            problems.append("unit %s is listed twice" % name)
            continue
        seen.add(name)
        deps = [d.strip("`*") for d in re.split(r"[,;\s]+", _cell(cells, dcol).replace("<br>", " "))]
        units.append({"name": name, "owner": _cell(cells, ocol).strip("`* "),
                      "paths": _lean_paths(_cell(cells, pcol)) if pcol is not None else [],
                      "depends_on": [d for d in deps if d and d.lower() not in ("-", "—", "none", "n/a")],
                      "contracts": _items(_cell(cells, ccol)),
                      "stories": list(dict.fromkeys(_ids("; ".join(_cell(cells, i) for i in scols), name,
                                                          warnings))),
                      "status": _cell(cells, stcol)})
    if pcol is None and units:
        problems.append("unit-of-work.md has no Paths column (Lean's gates read it)")
    if not scols and units:
        warnings.append("unit-of-work.md has no Stories / ACs column (its columns: %s): the children get no stories "
                        "or ACs from it; name the column Stories / ACs" % ", ".join(h for h in header if h))
    names = {u["name"] for u in units}
    for u in units:
        if not u["paths"]:
            problems.append("unit %s has no Paths (its code and contract folders, e.g. services/%s/)" % (u["name"],
                                                                                                  u["name"]))
        for p in u["paths"]:
            why = intents.path_problem(p)
            if why:
                problems.append("unit %s: %s" % (u["name"], why))
        if len(u["paths"]) > intents.LIMITS["paths"]:
            problems.append("unit %s has more than %d paths" % (u["name"], intents.LIMITS["paths"]))
        for d in u["depends_on"]:
            if d == u["name"]:
                problems.append("unit %s depends on itself" % d)
            elif d not in names:
                problems.append("unit %s depends on %s, which is not a unit in the table" % (u["name"], d))
    teams = {}
    for u in units:
        teams.setdefault(lean_team(u["name"]), []).append(u["name"])
    for t, same in sorted(teams.items()):
        if len(same) > 1:
            problems.append("units %s would be one Lean team (%s): name them apart" % (", ".join(same), t))
    cycle = _cycle(units)
    if cycle:
        problems.append("units depend on each other in a circle: %s" % " -> ".join(cycle))
    return units, problems


def _cycle(units):
    deps = {u["name"]: [d for d in u["depends_on"] if d != u["name"]] for u in units}
    state, path = {}, []

    def visit(n):
        state[n] = 1
        path.append(n)
        for d in deps.get(n, []):
            if d not in deps:
                continue
            if state.get(d) == 1:
                return path[path.index(d):] + [d]
            if state.get(d) is None:
                found = visit(d)
                if found:
                    return found
        state[n] = 2
        path.pop()
        return None

    for u in units:
        if state.get(u["name"]) is None:
            found = visit(u["name"])
            if found:
                return found
    return None


def topo(units) -> list:
    """Units with every dependency before the units that need it (table order otherwise)."""
    done, out, by = set(), [], {u["name"]: u for u in units}

    def add(u, seen=()):
        if u["name"] in done or u["name"] in seen:
            return
        for d in u["depends_on"]:
            if d in by:
                add(by[d], seen + (u["name"],))
        done.add(u["name"])
        out.append(u)

    for u in units:
        add(u)
    return out


def parse_stories(text) -> dict:
    """{US-n: {"as", "want", "so", "acs"}} from stories.md's table (ID | As a | I want | So that | ACs)."""
    out = {}
    for header, rows in M._tables(text or ""):
        i = M._col(header, "id")
        if i is None:
            continue
        a, w, s, c = M._col(header, "as a"), M._col(header, "want"), M._col(header, "so that"), M._col(header, "ac")
        for cells in rows:
            sid = cells[i].strip("`* ") if i < len(cells) else ""
            if not re.fullmatch(r"US-[1-9][0-9]{0,3}", sid) or sid in out:
                continue
            out[sid] = {"as": _cell(cells, a), "want": _cell(cells, w), "so": _cell(cells, s),
                        "acs": [x for x in ID_RE.findall(_cell(cells, c)) if x.startswith("AC-")]}
    return out


# ---------- the parent's Lean records ----------

def _stage_events(events, stage):
    return [e for e in events if e.get("event") in STAGE_EVENTS and M._d(e).get("stage") == stage
            and not M._d(e).get("unit")]


def _seq(e) -> int:
    return e.get("seq") if isinstance(e.get("seq"), int) else -1


def released(tree, intent) -> bool:
    """RELEASE approved for the intent, as Lean's Project.released(): the last approval or rejection that covers
    operations/release.md is an approval (whatever the file says now, or if it is gone)."""
    path = "%s/%s/%s" % (lean.DOCS, intent, M.RELEASE_DOC)
    last = None
    for e in M._events(tree, intent):
        if e.get("event") in ("APPROVED", "REJECTED") and M._covers(M._d(e), path):
            last = e
    return bool(last) and last.get("event") == "APPROVED"


def approved_split(tree, intent) -> dict:
    """The unit split a person approved, read from `tree` (mrdesc._Tree: the working tree or a commit). Raises
    BrainError listing every reason when there is none (fail closed). Returns {units, units_sha256, design_sha256,
    design_at, requirements (text), stories (text or None), warnings}."""
    docs = "%s/%s" % (lean.DOCS, intent)
    paths = {"req": "%s/%s" % (docs, M.REQ_DOC), "design": "%s/%s" % (docs, M.DESIGN_DOC),
             "units": "%s/%s" % (docs, UNITS_DOC), "stories": "%s/%s" % (docs, STORIES_DOC)}
    events = M._events(tree, intent)
    probs = []
    if released(tree, intent):
        probs.append("%s is released (RELEASE approved): its record is closed, so it can no longer be split" % intent)
    req = M._doc_state(events, tree, paths["req"])
    if req["state"] != "approved":
        probs.append("requirements.md is %s: it needs a current REQUIREMENTS (or INCEPTION) approval" % req["state"])
    des = M._doc_state(events, tree, paths["design"])
    if des["state"] != "approved":
        skipped = _stage_events(events, "application-design")
        if skipped and skipped[-1].get("event") == "STAGE_SKIPPED":
            probs.append("the application design was skipped (Size: small): a split needs an approved design, the "
                         "units are approved with it")
        else:
            probs.append("application-design.md is %s: it needs a current DESIGN (or INCEPTION) approval"
                         % des["state"])
    raw = tree.raw(paths["units"])
    units, units_sha, read_warnings = [], None, []
    if raw is None:
        probs.append("%s is missing: run Units Generation (`lean stage start units`)" % UNITS_DOC)
    else:
        units_sha = _sha(raw)
        ev = _stage_events(events, "units")
        last = ev[-1] if ev else None
        recorded = last and last.get("event") == "STAGE_COMPLETED" and \
            (M._d(last).get("outputs") or {}).get(paths["units"]) == units_sha
        if last is None:
            probs.append("Units Generation has no record: fill in unit-of-work.md, then `lean stage done units --note "
                         "...`, before the DESIGN approval")
        elif last.get("event") == "STAGE_SKIPPED":
            probs.append("Units Generation was skipped (one unit): there is nothing to split")
        elif last.get("event") == "STAGE_STARTED":
            probs.append("Units Generation is in progress again: finish it (`lean stage done units --note ...`) and "
                         "ask for the DESIGN approval again")
        elif not recorded:
            probs.append("unit-of-work.md changed after `lean stage done units` recorded it (sha %s now): record it "
                         "again and ask for the DESIGN approval again" % units_sha[:8])
        elif des["state"] == "approved" and des["event"] is not None:
            # What the person saw when they approved: the last Units Generation record before the approval. Only
            # within one clone's ledger is "before" certain (its seq); another clone's clock may be off.
            d_ev = des["event"]
            mine = [e for e in ev if e.get("_file") == d_ev.get("_file")]
            prior = [e for e in mine if _seq(e) < _seq(d_ev)]
            if not mine:
                probs.append("Units Generation was recorded in another Lean clone than the DESIGN approval, so which "
                             "came first is not certain: record the units (`lean stage done units`) and approve DESIGN "
                             "again in the same clone")
            elif not prior or prior[-1].get("event") != "STAGE_COMPLETED" or \
                    (M._d(prior[-1]).get("outputs") or {}).get(paths["units"]) != units_sha:
                probs.append("the unit split (unit-of-work.md sha %s) is not the one recorded last before the DESIGN "
                             "approval of %s, so the approval does not cover it (Lean's DESIGN hash covers "
                             "application-design.md only). Ask for DESIGN again: `lean request DESIGN`; approving the "
                             "unchanged design again is fine" % (units_sha[:8], str(d_ev.get("ts") or "")[:10]))
        text = raw.decode("utf-8", "replace")
        units, uprobs = parse_units(text, read_warnings)
        probs += uprobs
        if not uprobs and len(units) < 2:
            probs.append("unit-of-work.md lists %d unit: there is nothing to split (build it in this run)"
                         % len(units))
    if probs:
        raise BrainError("no approved unit split in %s:\n  - %s" % (docs, "\n  - ".join(probs)))
    warnings = list(read_warnings)
    for i, a in enumerate(units):
        for b in units[i + 1:]:
            both = sorted({p for p in a["paths"] for q in b["paths"] if paths_overlap(p, q)})
            if both:
                warnings.append("units %s and %s share paths (%s): Lean's units meet only at contracts, and their "
                                "children will get overlap warnings" % (a["name"], b["name"], ", ".join(both[:3])))
    return {"units": units, "units_sha256": units_sha,
            "design_sha256": M._covered_sha(M._d(des["event"]), paths["design"]),
            "design_at": des["event"].get("ts"), "requirements": tree.read(paths["req"]) or "",
            "stories": tree.read(paths["stories"]), "warnings": warnings}


# ---------- what a child carries ----------

def _clean(text, limit) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _ok(text) -> bool:
    return not brainlint.content_problems(text)[0]


def child_fields(parent_key, parent_title, info, unit, keys, rec, commit) -> tuple:
    """(claim fields, warnings) of the child for `unit` (a parse_units row). keys: {unit name: child key} of the
    units it depends on. Text copied from Lean's pages passes the Brain's content checks or is left out (the id
    stays): a flagged acceptance criterion is never stored on the claim branch."""
    warnings = []
    u = unit["name"]
    stories = parse_stories(info["stories"])
    acs = {a["id"]: a for a in (M._requirements(info["requirements"]) or {}).get("acs", [])}
    ids, story_ids = [], []
    for x in unit["stories"]:
        if x.startswith("US-"):
            story_ids.append(x)
            ids += stories.get(x, {}).get("acs", [])
            if x not in stories:
                warnings.append("unit %s names %s, which is not in stories.md" % (u, x))
        elif x.startswith("AC-"):
            ids.append(x)
    ids = list(dict.fromkeys(ids))
    ac_lines = []
    for aid in ids[:intents.LIMITS["acs"]]:
        a = acs.get(aid)
        if not a:
            warnings.append("unit %s names %s, which is not in requirements.md" % (u, aid))
            ac_lines.append(aid)
            continue
        line = _clean("%s: %s → %s" % (aid, a["given"], a["then"]) if a["given"] and a["then"] else
                      "%s: %s" % (aid, a["given"] or a["then"]), intents.LIMITS["ac"])
        if _ok(line):
            ac_lines.append(line)
        else:
            warnings.append("%s (unit %s): text not copied, it does not pass the content checks (%s); the child "
                            "reads it in requirements.md" % (aid, u, "; ".join(brainlint.content_problems(line)[0])))
            ac_lines.append(aid)
    want = next((stories[s]["want"] for s in story_ids if s in stories and stories[s]["want"].strip()), "")
    title = ""
    for t in ("%s · %s" % (u, want) if want else "", "%s · %s" % (u, parent_title or ""),
              "%s · %s" % (u, parent_key)):
        t = _clean(t, intents.LIMITS["title"])
        if t.strip(" ·") and _ok(t):
            title = t
            break
    deps = ["%s (%s)" % (keys.get(d, "?"), d) for d in unit["depends_on"]]
    # no hashes here: a new DESIGN approval that leaves this unit as it was must not change it (the hashes are in
    # the claim's split field, which is not compared)
    lines = ["Unit %s of %s, from the unit split approved with its design (%s/%s/%s)." % (
        u, parent_key, lean.DOCS, rec["intent"], UNITS_DOC),
        "Paths: %s" % ", ".join(unit["paths"]),
        "Depends on: %s" % (", ".join(deps) or "-")]
    contracts = []
    for c in unit["contracts"]:
        c = _clean(c, intents.LIMITS["contract"])
        if _ok(c):
            contracts.append(c)
        else:
            warnings.append("unit %s: contract %r does not pass the content checks: not copied" % (u, c[:40]))
    if contracts:
        lines.append("Contracts: %s" % "; ".join(contracts))
    if story_ids:
        lines.append("Stories:")
    shown = story_ids[:12]                       # the body stays well under intents.LIMITS["body"]
    if len(story_ids) > len(shown):
        lines.append("- %s (and %d more in stories.md)" % (", ".join(story_ids[len(shown):][:5]),
                                                           len(story_ids) - len(shown)))
    for s in shown:
        st = stories.get(s)
        line = _clean("- %s: as %s, I want %s, so that %s" % (s, st["as"], st["want"], st["so"]), 400) if st else \
            "- %s" % s
        if st and not _ok(line):
            warnings.append("%s (unit %s): story text not copied, it does not pass the content checks" % (s, u))
            line = "- %s" % s
        lines.append(line)
    refs = ["%s/%s/%s" % (lean.DOCS, rec["intent"], UNITS_DOC)] + story_ids
    fields = {"source": "split", "title": title, "body": "\n".join(lines), "acs": ac_lines,
              "refs": refs[:intents.LIMITS["refs"]], "parent": parent_key, "unit": u,
              "expected_paths": list(unit["paths"]), "depends_on": [keys[d] for d in unit["depends_on"] if d in keys],
              "contracts": contracts,
              "split": {"intent": rec["intent"], "branch": rec["branch"], "commit": commit,
                        "design_sha256": info["design_sha256"], "units_sha256": info["units_sha256"]}}
    fields["hash"] = intents.digest("split", fields["title"], fields["body"])
    return fields, warnings


def field_problems(key, fields) -> list:
    """Everything intents and pre-receive check, for these fields on a claim `key` (fail closed before a push)."""
    doc = dict(fields, key=key, owner=None, filed_by="x")
    errs, _ = intents.problems("split", fields["title"], fields["body"], fields["acs"], fields["refs"])
    return errs + intents.child_problems(doc)


def same_fields(doc, fields) -> bool:
    return all(doc.get(k) == fields.get(k) for k in FIELDS)


def changes(doc, fields) -> list:
    """What differs between a child as filed and the split now, for people to read."""
    out = []
    for k in ("expected_paths", "depends_on", "contracts", "acs"):
        if doc.get(k) != fields.get(k):
            out.append("%s %s -> %s" % (k, ", ".join(doc.get(k) or []) or "-", ", ".join(fields.get(k) or []) or "-"))
    if doc.get("title") != fields.get("title"):
        out.append("title %r -> %r" % (doc.get("title"), fields.get("title")))
    if not out and doc.get("body") != fields.get("body"):
        out.append("description")
    return out


# ---------- the parent ----------

def _current_parent(repo, cfg, me, key=None):
    """(branch, run.json, parent claim) of the parent run checked out here, owned by me."""
    from .cli import _current
    br, b, rp, rec = _current(repo, cfg)
    if key and b["key"] != key:
        raise BrainError("this branch (%s) is the run of %s, not %s: run `brain split %s` on %s's run branch"
                         % (br, b["key"], key, key, key))
    if rec["owner"] != me:
        raise BrainError("%s's run belongs to %s; only its owner splits it" % (b["key"], rec["owner"]))
    if rec.get("parent"):
        raise BrainError("%s is itself a child of %s (unit %s): split the parent, not a child" % (
            b["key"], rec["parent"], rec.get("unit")))
    doc = claims.read(repo, b["key"])
    if not isinstance(doc, dict):
        raise BrainError("%s has no claim (claim/%s)" % (b["key"], b["key"]))
    if doc.get("owner") != me:
        raise BrainError("%s is claimed by %s; only they split it" % (b["key"], doc.get("owner") or "nobody"))
    if doc.get("closed"):
        raise BrainError("%s is closed (%s)" % (b["key"], (doc["closed"] or {}).get("reason", "")))
    return br, rec, doc


def _clone_team(repo):
    """The Lean team of this clone (`lean init --team`: .aidlc-lean/local.json), or None."""
    try:
        with open(os.path.join(repo, ".aidlc-lean", "local.json"), encoding="utf-8") as f:
            team = json.load(f).get("team")
    except (OSError, ValueError, AttributeError):
        return None
    return team if isinstance(team, str) and team else None


def children_of(docs, parent_key) -> list:
    return sorted((d for d in docs if d.get("parent") == parent_key), key=lambda d: intents._order(d)[1:])


def open_by_unit(children) -> tuple:
    """({unit: the earliest open child}, [later open children of the same unit])."""
    by, dupes = {}, []
    for d in children:
        if d.get("closed"):
            continue
        if d.get("unit") in by:
            dupes.append(d)
        else:
            by[d.get("unit")] = d
    return by, dupes


def plan(repo, cfg, me, key=None, docs=None) -> dict:
    """What `brain split` would do on the parent run checked out here (nothing is written). Returns {parent, branch,
    rec, info, commit, actions, warnings, docs}; an action: {unit, kind, key, fields, doc, why}. kind: new (file it) |
    same | update (unowned, changed: update it) | changed (claimed or started: only reported) | close (unowned, its unit
    is gone) | removed (claimed or started, its unit is gone: only reported) | close_duplicate | duplicate."""
    if cfg.get("tracker") != "none":
        raise BrainError("tracker is %s: children are claims (tracker none)" % cfg.get("tracker"))
    br, rec, pdoc = _current_parent(repo, cfg, me, key)
    dirty = git_out(repo, "status", "--porcelain", "--untracked-files=all", "--", lean.DOCS, cfg["runs_dir"])
    if dirty:
        raise BrainError("commit Lean's records first: the split is read from the committed branch\n  "
                         + "\n  ".join(dirty.splitlines()[:10]))
    commit = git_out(repo, "rev-parse", "HEAD")
    info = approved_split(M._Tree(repo, "HEAD"), rec["intent"])
    warnings = list(info["warnings"])
    lcfg = M._lean_cfg(M._Tree(repo, "HEAD"))
    if lcfg.get("multi_team"):
        mine = os.environ.get("AIDLC_LEAN_TEAM") or _clone_team(repo)
        if mine and any(lean.same_team(repo, mine, lean_team(u["name"])) for u in info["units"]):
            warnings.append("multi-team: this clone's Lean team is %s, a unit's: Lean would send the parent's run to "
                            "build that unit. The parent's clone needs a team that is no unit (`lean init --team "
                            "lead`)" % mine)
        for u in info["units"]:
            if u["owner"] and u["owner"] != "-" and not lean.same_team(repo, u["owner"], lean_team(u["name"])):
                warnings.append("multi-team: unit %s has Owner team %s, but its child runs Lean as team %s: Lean's "
                                "Next would send it elsewhere (set Owner team to %s)" % (
                                    u["name"], u["owner"], lean_team(u["name"]), lean_team(u["name"])))
    elif any(u["contracts"] for u in info["units"]):
        warnings.append("the units name contracts, but %s is not multi-team: Lean's CR flow between units needs "
                        '"multi_team": true (a person sets it) and aidlc-docs/contracts/OWNERS mapping each unit\'s '
                        "contract folder to the unit" % lean.CONFIG)
    if docs is None:
        docs = intents.listing(repo, cfg)[0]
    kids = children_of(docs, pdoc["key"])
    by_unit, dupes = open_by_unit(kids)
    names = {u["name"] for u in info["units"]}
    keys = {u: d["key"] for u, d in by_unit.items()}
    actions = []
    for u in topo(info["units"]):
        doc = by_unit.get(u["name"])
        fields, warn = child_fields(pdoc["key"], pdoc.get("title"), info, u, keys, rec, commit)
        warnings += warn
        if doc is None:
            actions.append({"unit": u["name"], "kind": "new", "key": None, "fields": fields, "doc": None})
            keys[u["name"]] = "(new %s)" % u["name"]    # apply() files in this order, so the real key is known by then
            continue
        if same_fields(doc, fields):
            actions.append({"unit": u["name"], "kind": "same", "key": doc["key"], "fields": fields, "doc": doc})
        elif doc["state"] == "unclaimed":
            actions.append({"unit": u["name"], "kind": "update", "key": doc["key"], "fields": fields, "doc": doc,
                            "why": changes(doc, fields)})
        else:
            actions.append({"unit": u["name"], "kind": "changed", "key": doc["key"], "fields": fields, "doc": doc,
                            "why": changes(doc, fields)})
    for d in list(by_unit.values()) + dupes:
        gone = d.get("unit") not in names
        dup = d in dupes
        if not gone and not dup:
            continue
        kind = ("close" if d["state"] == "unclaimed" else "removed") if gone else (
            "close_duplicate" if d["state"] == "unclaimed" else "duplicate")
        actions.append({"unit": d.get("unit"), "kind": kind, "key": d["key"], "fields": None, "doc": d})
    return {"parent": pdoc, "branch": br, "rec": rec, "info": info, "commit": commit, "actions": actions,
            "warnings": warnings, "docs": docs}


def apply(repo, cfg, me, p) -> dict:
    """File, update and close what plan() said. Children are filed in dependency order, so each new child's
    depends_on names keys that exist. Returns p with each action's key and "done"."""
    keys = {a["unit"]: a["key"] for a in p["actions"] if a["fields"] is not None and a["key"]}
    by = {u["name"]: u for u in p["info"]["units"]}
    pkey = p["parent"]["key"]
    reason_gone = "unit %%s is no longer in %s's unit split (unit-of-work.md %s)" % (pkey,
                                                                                 p["info"]["units_sha256"][:8])
    for a in p["actions"]:
        if a["kind"] in ("new", "update"):
            fields, _ = child_fields(pkey, p["parent"].get("title"), p["info"], by[a["unit"]], keys, p["rec"],
                                     p["commit"])
            bad = field_problems(a["key"] or "%s-9999999" % cfg["req_prefix"], fields)
            if bad:
                raise BrainError("not filed (refused, not masked): unit %s: %s" % (a["unit"], "; ".join(bad)))
            if a["kind"] == "new":
                doc = claims.claim(repo, cfg["req_prefix"], None, fields["title"], extra=dict(fields, filed_by=me))
                a["key"] = keys[a["unit"]] = doc["key"]
                a["fields"], a["done"] = fields, True
                continue
            # the plan saw it unowned; write only if it still is, on the very tip read now
            tip, cur = claims._tip_doc(repo, a["key"])
            if not claims.unowned(cur) or cur.get("closed"):
                a.update(kind="changed", done=False, why=["claimed by %s meanwhile: not changed" % (
                    (cur or {}).get("owner") or "someone")] + (a.get("why") or []))
                continue
            new = dict({k: v for k, v in cur.items() if k in intents.KNOWN_FIELDS}, **fields)
            claims.update(repo, a["key"], new, what="update", expect=tip)
            a["fields"], a["done"] = fields, True
        elif a["kind"] in ("close", "close_duplicate"):
            reason = reason_gone % a["unit"] if a["kind"] == "close" else \
                "duplicate child of %s for unit %s" % (pkey, a["unit"])
            try:
                intents.close(repo, a["key"], reason, by=me, unowned_only=True)
                a["done"] = True
            except BrainError as e:                       # taken meanwhile: its owner decides now
                a.update(kind="removed" if a["kind"] == "close" else "duplicate", done=False, why=[str(e)])
    # someone split the same parent at the same time: the earliest child of each unit stays (as intents.add); the
    # later ones I filed and nobody took are closed, whichever run filed them
    docs = intents.listing(repo, cfg)[0]
    by_unit, dupes = open_by_unit(children_of(docs, pkey))
    for d in dupes:
        if d.get("filed_by") == me and d["state"] == "unclaimed":
            first = by_unit[d.get("unit")]["key"]
            try:
                intents.close(repo, d["key"], "duplicate of %s (split at the same time)" % first, by=me,
                              unowned_only=True)
            except BrainError:
                continue
            p["actions"].append({"unit": d.get("unit"), "kind": "raced", "key": d["key"], "fields": None, "doc": d,
                                 "done": True, "why": ["duplicate of %s" % first]})
    return p


# ---------- progress: the parent and its children ----------

parent_ref = intents.branch_ref


def main_ref(repo, cfg):
    r = ("refs/remotes/origin/%s" if claims._has_origin(repo) else "refs/heads/%s") % cfg["default_branch"]
    return r if git(repo, "rev-parse", "--verify", "-q", r + "^{commit}", check=False).returncode == 0 else None


def _parent_intent(repo, cfg, key, ref):
    if not ref:
        return None
    p = git(repo, "show", "%s:%s" % (ref, runrec.path(cfg["runs_dir"], key).replace(os.sep, "/")), check=False)
    try:
        rec = json.loads(p.stdout) if p.returncode == 0 else None
    except ValueError:
        rec = None
    return rec.get("intent") if isinstance(rec, dict) and isinstance(rec.get("intent"), str) else None


def families(repo, cfg, docs) -> dict:
    """{parent key: family} for every parent with children in `docs` (intents.listing). A family:
      {"key", "title", "owner", "state", "branch", "intent", "released", "children": [child keys, filing order],
       "progress": {"merged", "total", "unclaimed", "claimed", "in_progress", "closed", "text"}}
    total counts the children that are not closed; state: closed (the parent's claim is closed) | done (released
    and every child merged: the parent closes) | ready_to_release (every child merged) | in_progress."""
    by_key = {d.get("key"): d for d in docs}
    out = {}
    for pkey in sorted({d["parent"] for d in docs if d.get("parent")}, key=lambda k: intents._order({"key": k})[1:]):
        kids = children_of(docs, pkey)
        branch = next((d["split"]["branch"] for d in reversed(kids) if isinstance(d.get("split"), dict)), None)
        ref = parent_ref(repo, branch) if branch else None
        mref = main_ref(repo, cfg)
        intent = _parent_intent(repo, cfg, pkey, ref) or _parent_intent(repo, cfg, pkey, mref)
        rel = False
        for r in (ref, mref):
            if r and intent and released(M._Tree(repo, r), intent):
                rel = True
                break
        live = [d for d in kids if d["state"] != "closed"]
        count = {s: sum(1 for d in kids if d["state"] == s) for s in intents.STATES}
        merged, total = count["merged"], len(live)
        pdoc = by_key.get(pkey) or {}
        if pdoc.get("closed"):
            st = "closed"
        elif total and merged == total and rel:
            st = "done"
        elif total and merged == total:
            st = "ready_to_release"
        else:
            st = "in_progress"
        out[pkey] = {"key": pkey, "title": pdoc.get("title"), "owner": pdoc.get("owner"), "state": st,
                     "branch": branch, "intent": intent, "released": rel, "children": [d["key"] for d in kids],
                     "progress": {"merged": merged, "total": total, "unclaimed": count["unclaimed"],
                                  "claimed": count["claimed"], "in_progress": count["in_progress"],
                                  "closed": count["closed"], "text": "%d/%d merged" % (merged, total)}}
    return out


def blocked_by(doc, docs) -> list:
    """The dependencies of a child that have not merged (keys)."""
    by_key = {d.get("key"): d for d in docs}
    return [k for k in doc.get("depends_on") or [] if (by_key.get(k) or {}).get("state") != "merged"]


def enrich(repo, cfg, docs) -> tuple:
    """(docs with "progress" on parents and "blocked_by" on children, families)."""
    fam = families(repo, cfg, docs)
    out = []
    for d in docs:
        d = dict(d)
        if d.get("key") in fam:
            f = fam[d["key"]]
            d.update(children=f["children"], progress=f["progress"], family_state=f["state"], released=f["released"])
        if d.get("parent"):
            d["blocked_by"] = blocked_by(d, docs)
        out.append(d)
    return out, fam


def handoff_problems(repo, cfg, rec) -> list:
    """Why `brain handoff` must not hand this run over: a parent whose children have not all merged (its review and
    RELEASE come after them)."""
    docs = intents.listing(repo, cfg)[0]
    kids = [d for d in children_of(docs, rec["key"]) if d["state"] != "closed"]
    waiting = [d for d in kids if d["state"] != "merged"]
    if not waiting:
        return []
    return ["%s is split into %d children and %d of them %s not merged into %s yet (%s): the parent's review and "
            "RELEASE come once every child has merged (`brain intent list`)" % (
                rec["key"], len(kids), len(waiting), "has" if len(waiting) == 1 else "have", rec["branch"],
                ", ".join("%s %s: %s" % (d["key"], d.get("unit"), _who(d)) for d in waiting[:6]))]


def handoff_warnings(repo, cfg, rec) -> list:
    """A child built on a sibling's branch: say so while that sibling has not merged."""
    on = rec.get("built_on")
    if not on or not rec.get("target_branch"):
        return []
    ref = parent_ref(repo, rec["target_branch"])
    dep = claims.read(repo, on)
    if ref and isinstance(dep, dict) and intents.child_merged(repo, cfg, dep, ref, intents.run_branches(repo, cfg)):
        return []
    return ["%s started from %s's branch, which has not merged into %s yet: this MR carries its commits until it "
            "does (`brain sync` once it has)" % (rec["key"], on, rec["target_branch"])]


# ---------- a child run ----------

def _run_branches_for(repo, cfg, key) -> list:
    """Run branches of `key` here or on origin: [(name, ref to read)], origin's first."""
    prefix, out = cfg["branch_prefix"], []
    if claims._has_origin(repo):
        p = git(repo, "ls-remote", "origin", "refs/heads/%s/*" % prefix, check=False)
        for line in p.stdout.splitlines():
            name = line.split("\t", 1)[1][len("refs/heads/"):] if "\t" in line else ""
            b = parse_branch(name, prefix)
            if b and b["key"] == key:
                git(repo, "fetch", "-q", "origin", "+refs/heads/%s:refs/remotes/origin/%s" % (name, name), check=False)
                out.append((name, "refs/remotes/origin/%s" % name))
    for full in git_out(repo, "for-each-ref", "--format=%(refname)", "refs/heads/%s/" % prefix).split():
        name = full[len("refs/heads/"):]
        b = parse_branch(name, prefix)
        if b and b["key"] == key and name not in [n for n, _ in out]:
            out.append((name, full))
    return out


def worktree_path(repo, key) -> str:
    from .reflect import worktree_root
    return os.path.join(worktree_root(repo), key)


def slugify(unit) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", unit.lower()).strip("-")[:40].strip("-")
    return s if SLUG_RE.match(s or "") else "unit"


def start_child(repo, cfg, a, doc, me, version) -> int:
    """`brain start [slug] --key CHILD`: see the module docstring. Every check runs before anything is written
    (the claim is taken last, right before the branch is made)."""
    key, pkey, unit = doc["key"], doc["parent"], doc["unit"]
    if doc.get("closed"):
        raise BrainError("%s is closed (%s)" % (key, (doc["closed"] or {}).get("reason", "")))
    if doc.get("owner") and doc["owner"] != me:
        raise BrainError("%s is claimed by %s" % (key, doc["owner"]))
    if getattr(a, "paths", None):
        raise BrainError("%s is unit %s of %s: its paths are the unit's Paths in unit-of-work.md (no --paths)" % (
            key, unit, pkey))
    if getattr(a, "doc", None) or getattr(a, "package", None):
        raise BrainError("%s continues %s's Lean intent: the requirement documents are the parent run's (no --doc or "
                         "--package)" % (key, pkey))
    team = lean_team(unit)
    env_team = os.environ.get("AIDLC_LEAN_TEAM")
    if env_team and env_team != team:
        raise BrainError("AIDLC_LEAN_TEAM is set to %s here: it overrides the Lean team of %s's worktree (%s). Unset "
                         "it (`unset AIDLC_LEAN_TEAM`) for child runs" % (env_team, key, team))
    pdoc = claims.read(repo, pkey)
    if not isinstance(pdoc, dict):
        raise BrainError("%s's parent %s has no claim" % (key, pkey))
    if pdoc.get("closed"):
        raise BrainError("%s's parent %s is closed (%s)" % (key, pkey, (pdoc["closed"] or {}).get("reason", "")))
    sp = doc["split"]
    base = parent_ref(repo, sp["branch"])
    if not base:
        raise BrainError("%s's parent run branch %s is not here or on origin: %s's owner pushes it" % (
            key, sp["branch"], pkey))
    if git(repo, "merge-base", "--is-ancestor", sp["commit"], base, check=False).returncode != 0:
        raise BrainError("%s does not contain the commit %s was split at (%s): %s's owner pushes it" % (
            sp["branch"], pkey, sp["commit"][:12], pkey))
    if _parent_intent(repo, cfg, pkey, base) != sp["intent"]:
        raise BrainError("%s on %s is not the run of Lean intent %s any more" % (
            runrec.path(cfg["runs_dir"], pkey), sp["branch"], sp["intent"]))
    info = approved_split(M._Tree(repo, base), sp["intent"])
    units = {u["name"]: u for u in info["units"]}
    if unit not in units:
        raise BrainError("unit %s is no longer in %s's unit split (unit-of-work.md %s on %s): ask %s; close %s with "
                         "`brain intent close %s --reason ...`" % (unit, pkey, info["units_sha256"][:8], sp["branch"],
                                                                   pdoc.get("owner"), key, key))
    docs = intents.listing(repo, cfg)[0]
    by_unit, _ = open_by_unit(children_of(docs, pkey))
    by_key = {d["key"]: d for d in docs}
    keys = {u: d["key"] for u, d in by_unit.items()}
    dep_keys = []
    for d in units[unit]["depends_on"]:
        if d not in keys:
            raise BrainError("%s depends on unit %s, which has no open child: ask %s's owner to run `brain split %s`"
                             % (key, d, pkey, pkey))
        dep_keys.append(keys[d])
    mref = main_ref(repo, cfg)
    pbase = base                                  # the parent's branch: where the child's MR goes
    tips = intents.run_branches(repo, cfg)
    # a dependency counts once it has merged into the parent's branch (the branch this child starts from)
    merged = {k for k in dep_keys if k in by_key and intents.child_merged(repo, cfg, by_key[k], pbase, tips)}
    on, built_on = getattr(a, "on", None), None
    waiting = [k for k in dep_keys if k not in merged]
    if on:
        if on not in dep_keys:
            raise BrainError("--on %s: %s does not depend on it (its dependencies: %s)" % (
                on, key, ", ".join(dep_keys) or "none"))
        if on in merged:
            raise BrainError("--on %s: it has merged into %s already; start without --on" % (on, sp["branch"]))
        dep = by_key[on]
        theirs = [(n, r) for n, r in _run_branches_for(repo, cfg, on)
                  if (parse_branch(n, cfg["branch_prefix"]) or {}).get("owner") == dep.get("owner")]
        if not dep.get("owner") or not theirs:
            raise BrainError("--on %s: it has no run branch of its owner (%s) yet, here or on origin" % (
                on, dep.get("owner") or "nobody has claimed it"))
        ref = theirs[0][1]
        drec = _run_rec(repo, cfg, on, ref)
        if git(repo, "merge-base", "--is-ancestor", sp["commit"], ref, check=False).returncode != 0 or \
                not drec or drec.get("parent") != pkey or drec.get("unit") != dep.get("unit"):
            raise BrainError("--on %s: %s is not its run of unit %s started from %s's split" % (
                on, theirs[0][0], dep.get("unit"), pkey))
        base, built_on = ref, on
        waiting.remove(on)
    if waiting:
        who = ["%s (unit %s, %s)" % (k, (by_key.get(k) or {}).get("unit"), _who(by_key.get(k))) for k in waiting]
        if on:                                    # --on builds on one branch: the others have to merge first
            hint = " --on builds on one dependency's branch (%s's); wait for %s to merge." % (
                on, "it" if len(waiting) == 1 else "them")
        elif len(waiting) == 1:
            hint = " Wait for it, or build on its branch: `brain start --key %s --on %s`." % (key, waiting[0])
        else:
            hint = " Wait for them to merge."
        raise BrainError("%s depends on %s, which %s not merged into %s yet.%s" % (
            key, ", ".join(who), "has" if len(waiting) == 1 else "have", sp["branch"], hint))
    rp = runrec.path(cfg["runs_dir"], key).replace(os.sep, "/")
    for ref in (pbase, base, mref):
        if ref and git(repo, "cat-file", "-e", "%s:%s" % (ref, rp), check=False).returncode == 0:
            raise BrainError("%s already exists on %s: this requirement already has a run" % (rp, ref))
    mine = [n for n, _ in _run_branches_for(repo, cfg, key)
            if (parse_branch(n, cfg["branch_prefix"]) or {}).get("owner") == me]   # others' branches are not its run
    if mine:
        raise BrainError("%s already has your run: %s" % (key, ", ".join(mine)))
    slug = (a.slug or slugify(unit)).lower()
    if not SLUG_RE.match(slug):
        raise BrainError("slug must be lowercase letters, digits and '-' (max 40)")
    br = branch_name(cfg["branch_prefix"], me, key, slug)
    wt = os.path.abspath(os.path.expanduser(a.worktree)) if getattr(a, "worktree", None) else worktree_path(repo, key)
    if os.path.exists(wt) and not os.path.isdir(wt):
        raise BrainError("%s is a file, not a folder: pass --worktree a folder that does not exist yet (or is empty)"
                         % wt)
    try:
        used = os.path.exists(wt) and bool(os.listdir(wt))
    except OSError as e:
        raise BrainError("cannot read the worktree folder %s: %s" % (wt, e))
    if used:
        raise BrainError("%s exists and is not empty: remove it (`git worktree remove %s`) or pass --worktree" % (
            wt, wt))
    fields, warns = child_fields(pkey, pdoc.get("title"), info, units[unit], keys, _parent_rec(sp), sp["commit"])
    bad = field_problems(key, fields)
    if bad:
        raise BrainError("claim/%s would not pass the checks with the current split: %s" % (key, "; ".join(bad)))
    # ---- writes from here on: the claim, then the branch and its worktree; a claim taken here is given back
    # when the start does not get as far as its first commit
    took = False
    try:
        if claims.unowned(doc):
            doc = claims.take(repo, key, me)
            took = True
            print("Claimed %s for %s." % (key, me))
        if not same_fields(doc, fields):
            why = changes(doc, fields)
            # the owner takes the current split's unit; `split` (where it came from) stays as filed
            new = dict({k: v for k, v in doc.items() if k in intents.KNOWN_FIELDS},
                       **{k: v for k, v in fields.items() if k != "split"})
            claims.update(repo, key, new, what="update")
            print("%s now follows %s's current unit split (%s)." % (key, pkey, "; ".join(why)))
            doc = new
        _start_worktree(repo, cfg, a, doc, fields, me, version, key, pkey, unit, team, sp, base, mref, br, wt, rp,
                        built_on, by_key, warns)
    except BaseException:
        if took:
            try:
                claims.release(repo, key, me)
                print("Released %s again: the start did not finish." % key)
            except Exception:  # noqa: BLE001 - the error that stopped the start is the one to show
                pass
        raise
    return 0


def _run_rec(repo, cfg, key, ref):
    p = git(repo, "show", "%s:%s" % (ref, runrec.path(cfg["runs_dir"], key).replace(os.sep, "/")), check=False)
    try:
        rec = json.loads(p.stdout) if p.returncode == 0 else None
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _start_worktree(repo, cfg, a, doc, fields, me, version, key, pkey, unit, team, sp, base, mref, br, wt, rp,
                    built_on, by_key, warns):
    """The child's branch and worktree, its Lean clone (team, intent), run.json and intent.md, one commit, a push."""
    for w in warns:
        print("WARN: " + w)
    mb = git(repo, "merge-base", mref, base, check=False).stdout.strip() if mref else ""
    brain_commit = mb or git_out(repo, "rev-parse", base + "^{commit}")
    try:
        os.makedirs(os.path.dirname(wt), exist_ok=True)
    except OSError as e:
        raise BrainError("cannot make the worktree's folder %s: %s" % (os.path.dirname(wt), e))
    git(repo, "worktree", "add", "-q", "--no-track", "-b", br, wt, base)
    try:
        lean.call(wt, "init", "--team", team)
        lean.call(wt, "use", sp["intent"])
        clone = ""
        try:
            with open(os.path.join(wt, ".aidlc-lean", "clone-id"), encoding="utf-8") as f:
                clone = f.read().strip().rsplit("-", 1)[-1]
        except OSError:
            pass
        rec = runrec.new(key, me, br, sp["intent"], brain_commit, version, fields["expected_paths"], iso(utcnow()),
                         True, tracker=cfg["tracker"], materials=[])
        rec.update(parent=pkey, unit=unit, target_branch=sp["branch"], lean_team=team)
        if CLONE_RE.match(clone):
            rec["lean_clone"] = clone
        if built_on:
            rec["built_on"] = built_on
        write_json(os.path.join(wt, rp), rec)
        md = os.path.join(cfg["runs_dir"], key, intents.INTENT_MD)
        names = {k: (by_key.get(k) or {}).get("unit") for k in fields["depends_on"]}
        with open(os.path.join(wt, md), "w", encoding="utf-8") as f:
            f.write(intents.render_child_md(dict(doc, **fields), names))
        git(wt, "add", "--", rp, md, os.path.join(lean.DOCS, "audit"))
        git(wt, "commit", "-q", "-m", "%s: start run (unit %s of %s, brain %s)" % (key, unit, pkey, brain_commit[:10]))
    except BaseException:
        git(repo, "worktree", "remove", "--force", wt, check=False)
        git(repo, "branch", "-D", br, check=False)
        raise
    pushed = False
    if claims._has_origin(repo) and not a.no_push:
        p = git(wt, "push", "-q", "-u", "origin", br, check=False)   # the run is made: a failed push does not undo it
        pushed = p.returncode == 0
        if not pushed:
            print("WARN: not pushed (%s): push it with `git -C %s push -u origin %s`" % (
                (p.stderr or p.stdout).strip()[:300], wt, br))
    print("Started %s (unit %s of %s) on %s from %s (Lean intent %s, team %s, Brain %s)%s." % (
        key, unit, pkey, br, built_on and "%s's branch" % built_on or sp["branch"], sp["intent"], team,
        brain_commit[:10], ", pushed" if pushed else ""))
    print("Worktree: %s (its own Lean clone). Open Kiro there with the brain-dev agent." % wt)
    print("Intent: %s (the unit, its paths, ACs and contracts; the agent reads it first)" % md)
    if built_on:
        print("Built on %s, which has not merged yet: your MR carries its commits until it does; `brain sync` brings "
              "in %s once it has." % (built_on, sp["branch"]))
    print("Next: build unit %s only (`lean stage ... --unit %s`); `brain sync` merges %s in; `brain handoff` opens the "
          "MR into %s." % (unit, unit, sp["branch"], sp["branch"]))


def _who(doc) -> str:
    if not doc:
        return "not filed"
    return {"unclaimed": "unclaimed", "claimed": "claimed by %s" % doc.get("owner"),
            "in_progress": "%s is building it" % doc.get("owner"), "merged": "merged",
            "closed": "closed"}.get(doc.get("state"), doc.get("state") or "?")


def _parent_rec(sp) -> dict:
    return {"intent": sp["intent"], "branch": sp["branch"]}


# ---------- brain sync: merge the parent's branch (or any ref) in, Lean's generated files resolved ----------

def _blocks(text) -> list:
    """[(heading line or '', [lines])] of Session notes split at ### headings, in order. The newline that ends the
    text is not a line of the last subsection: the same subsection reads the same at the end and in the middle."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out, head, buf = [], "", []
    for line in lines:
        if line.startswith("### "):
            out.append((head, buf))
            head, buf = line.strip(), [line]
        else:
            buf.append(line)
    out.append((head, buf))
    return out


def _bare(lines):
    """A subsection's lines without its trailing blank lines (what is compared), or None."""
    if lines is None:
        return None
    n = len(lines)
    while n and not lines[n - 1].strip():
        n -= 1
    return tuple(lines[:n])


def _keyed(blocks) -> dict:
    seen, out = {}, {}
    for head, text in blocks:
        n = seen.get(head, 0)
        seen[head] = n + 1
        out[(head, n)] = text
    return out


def merge_notes(base, ours, theirs) -> tuple:
    """(Session notes, conflicts): a three-way merge by ### subsection (each team keeps its own `### Next (team X)`).
    A subsection both sides changed differently keeps both, between conflict markers, and counts as a conflict."""
    b, o, t = (_keyed(_blocks(x)) for x in (base, ours, theirs))
    order = list(o) + [k for k in t if k not in o]
    out, conflicts = [], []
    for k in order:
        bo, oo, to = b.get(k), o.get(k), t.get(k)
        nb, no, nt = _bare(bo), _bare(oo), _bare(to)
        if no == nt:
            r = oo if oo is not None else to
        elif no == nb:
            r = to
        elif nt == nb:
            r = oo
        else:
            conflicts.append(k[0] or "(top)")
            r = ["<<<<<<< ours"] + list(oo or []) + ["======="] + list(to or []) + [">>>>>>> theirs"]
        if r is not None:
            out += r
    end = "\n" if out and (ours.endswith("\n") or theirs.endswith("\n")) else ""
    return "\n".join(out) + end, conflicts


def _state_parts(text):
    """(header, generated region, Session notes) of an aidlc-state.md, or None without Lean's markers."""
    if not text or BEGIN_MARK not in text or END_MARK not in text:
        return None
    header, _, rest = text.partition(BEGIN_MARK)
    region, _, notes = rest.partition(END_MARK)
    return header, region, notes


def merge_state(base, ours, theirs) -> tuple:
    """(aidlc-state.md, conflicts) for a conflicted merge of one. The generated region is ours (Lean writes it again
    from the merged ledger right after); the header (title, Intent, Owner team) and the Session notes are merged
    three ways, the notes by ### subsection; what both sides changed differently keeps both between conflict markers
    and counts as a conflict. (None, [why]) when ours or theirs has no generated region to split at."""
    o, t = _state_parts(ours), _state_parts(theirs)
    if not o or not t:
        return None, ["no Lean progress markers"]
    b = _state_parts(base) or ("", "", "")
    conflicts = []
    if o[0] == t[0] or t[0] == b[0]:
        header = o[0]
    elif o[0] == b[0]:
        header = t[0]
    else:
        header = "<<<<<<< ours\n%s=======\n%s>>>>>>> theirs\n" % (o[0], t[0])
        conflicts.append("(header)")
    notes, more = merge_notes(b[2], o[2], t[2])
    return header + BEGIN_MARK + o[1] + END_MARK + notes, conflicts + more


def _stage(repo, n, path):
    p = git(repo, "show", ":%d:%s" % (n, path), check=False)
    return p.stdout if p.returncode == 0 else None


UNMERGED = ("DD", "AU", "UD", "UA", "DU", "AA", "UU")      # git status --porcelain: a path git has not merged
MARKER_RE = re.compile(r"(?m)^(?:<{7}|>{7})(?: |$)")


def _unfinished_merge(repo) -> tuple:
    """(MERGE_HEAD is set, [paths git still has unmerged]) here."""
    rows = git(repo, "status", "--porcelain", "--untracked-files=no").stdout.split("\n")
    merging = git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0
    return merging, [l[3:] for l in rows if l[:2] in UNMERGED]


def _marked(repo, path) -> bool:
    """The file has conflict markers (<<<<<<< or >>>>>>> at a line start)."""
    try:
        with open(os.path.join(repo, path), encoding="utf-8", errors="replace") as f:
            return MARKER_RE.search(f.read()) is not None
    except OSError:
        return False


def sync(repo, cfg, src=None, me=None) -> int:
    """Merge `src` (default: this child run's target branch, fetched) into the run branch checked out here. A
    conflict in Lean's own files is resolved: aidlc-state.md (merge_state, then `lean status` writes the region
    again), build-and-test-summary.md (ours: the next `lean verify` writes it). Anything else, a Lean ledger file
    included, is left for the person. Exit 0 when merged, 1 when conflicts are left. A merge left unfinished here
    (MERGE_HEAD, unmerged paths) is refused, and a file with conflict markers is never committed for you."""
    from .cli import _current
    br, b, rp, rec = _current(repo, cfg)
    if me and rec["owner"] != me:
        raise BrainError("this run belongs to %s" % rec["owner"])
    merging, unmerged = _unfinished_merge(repo)
    if merging or unmerged:
        # what an earlier sync left: committing it now would commit the conflict markers
        raise BrainError("the last merge here is not finished (%s): resolve the conflicts by hand, `git add` the files "
                         "and `git commit` (or undo it: `git merge --abort`), then `brain sync` again. Nothing was "
                         "committed" % "; ".join((["MERGE_HEAD is set"] if merging else []) +
                                                 (["unmerged: " + ", ".join(unmerged[:10])] if unmerged else [])))
    if src is None:
        target = rec.get("target_branch")
        if not target:
            raise BrainError("%s is not a child of a split: name what to merge, `brain sync --from <ref>`" % b["key"])
        src = parent_ref(repo, target)
        if not src:
            raise BrainError("%s is not here or on origin" % target)
    elif src.startswith("origin/") and claims._has_origin(repo):     # a branch on origin: fetch it first
        name = src[len("origin/"):]
        git(repo, "fetch", "-q", "origin", "+refs/heads/%s:refs/remotes/origin/%s" % (name, name), check=False)
    elif git(repo, "rev-parse", "--verify", "-q", src + "^{commit}", check=False).returncode != 0 and \
            parse_branch(src, cfg["branch_prefix"]):
        src = parent_ref(repo, src) or src                            # a run branch by name, origin's copy first
    if git(repo, "rev-parse", "--verify", "-q", src + "^{commit}", check=False).returncode != 0:
        raise BrainError("%s is not a commit here (fetch it first)" % src)
    state_doc = "%s/%s/aidlc-state.md" % (lean.DOCS, rec["intent"])
    dirty = [l[3:] for l in git(repo, "status", "--porcelain", "--untracked-files=no").stdout.split("\n")
             if l.strip()]
    if dirty:
        # Lean writes its ledger and aidlc-state.md all the time (hooks, `lean status`): those are committed for you
        if not all(x == state_doc or x.startswith(lean.DOCS + "/audit/") for x in dirty):
            raise BrainError("commit or stash your changes first:\n  " + "\n  ".join(dirty[:10]))
        marked = [x for x in dirty if _marked(repo, x)]
        if marked:
            raise BrainError("%s %s conflict markers (<<<<<<< / >>>>>>>): resolve them by hand first. Nothing was "
                             "committed" % (", ".join(marked), "has" if len(marked) == 1 else "have"))
        git(repo, "add", "--", *dirty)
        git(repo, "commit", "-q", "-m", "%s: Lean records before merging %s" % (b["key"], src))
    p = git(repo, "merge", "--no-edit", src, check=False)
    if p.returncode == 0:
        if "Already up to date" in p.stdout + p.stderr:
            print("%s already has %s." % (br, src))
            return 0
        _refresh_state(repo, rec["intent"], state_doc, "%s: Lean progress after merging %s" % (b["key"], src))
        print("Merged %s into %s." % (src, br))
        return 0
    conflicted = [x for x in git_out(repo, "diff", "--name-only", "--diff-filter=U").split("\n") if x]
    if not conflicted:
        raise BrainError("git merge failed: %s" % (p.stderr or p.stdout).strip())
    left, notes_conflicts = [], []
    for path in conflicted:
        if re.match(r"^%s/[^/]+/aidlc-state\.md$" % re.escape(lean.DOCS), path):
            text, conflicts = merge_state(_stage(repo, 1, path), _stage(repo, 2, path) or "",
                                          _stage(repo, 3, path) or "")
            if text is None:
                left.append(path)
                continue
            with open(os.path.join(repo, path), "w", encoding="utf-8") as f:
                f.write(text)
            if conflicts:
                notes_conflicts.append("%s (%s)" % (path, ", ".join(conflicts)))
                continue
            git(repo, "add", "--", path)
        elif re.match(r"^%s/[^/]+/construction/build-and-test/build-and-test-summary\.md$" % re.escape(lean.DOCS),
                      path):
            git(repo, "checkout", "--ours", "--", path)
            git(repo, "add", "--", path)
        else:
            left.append(path)
    if left or notes_conflicts:
        print("Merged %s with conflicts left for you:" % src)
        for x in left:
            why = " (a Lean ledger file: one Lean clone wrote on both branches; keep both clones' lines in order "\
                  "only if you are sure, else ask)" if x.startswith(lean.DOCS + "/audit/") else ""
            print("  %s%s" % (x, why))
        for x in notes_conflicts:
            print("  %s: Session notes changed on both sides; merge the marked subsections by hand" % x)
        print("Resolve them, `git add` them, then `git commit`; `lean status` writes Lean's progress again.")
        return 1
    _refresh_state(repo, rec["intent"], state_doc, None)
    marked = [x for x in conflicted if _marked(repo, x)]
    if marked:                                    # markers that came with a side: never committed for you
        print("Merged %s with conflicts left for you:" % src)
        for x in marked:
            print("  %s: has conflict markers (<<<<<<< / >>>>>>>)" % x)
        print("Resolve them, `git add` them, then `git commit`; `lean status` writes Lean's progress again.")
        return 1
    git(repo, "commit", "-q", "--no-edit")
    print("Merged %s into %s; Lean's generated files were resolved (aidlc-state.md written again from the merged "
          "ledger)." % (src, br))
    return 0


def _refresh_state(repo, intent, state_doc, msg):
    """`lean status` writes the progress region of the active intent again (after `lean use` when another intent is
    active in this clone); the page and Lean's ledger are staged. With msg, what changed is committed."""
    try:
        with open(os.path.join(repo, ".aidlc-lean", "active.json"), encoding="utf-8") as f:
            active = json.load(f).get("intent")
    except (OSError, ValueError, AttributeError):
        active = None
    if active != intent:
        lean.call(repo, "use", intent, check=False)
    lean.call(repo, "status", check=False)
    git(repo, "add", "--", state_doc, os.path.join(lean.DOCS, "audit"), check=False)
    if msg and git(repo, "diff", "--cached", "--quiet", check=False).returncode != 0:
        git(repo, "commit", "-q", "-m", msg)


__all__ = ["approved_split", "parse_units", "plan", "apply", "families", "enrich", "start_child", "sync",
           "lean_team", "merge_state"]
