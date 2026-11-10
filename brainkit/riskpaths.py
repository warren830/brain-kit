"""Risk-path patterns (gates/risk-paths): one set of rules, the way .gitignore reads a pattern, for brain-ci
check-run, the MR description, `brain handoff` and the bridge's hand-over hint. matches() is for a file git names;
meets() for a run's expected path (a file, a folder or a glob that names everything below it).

One pattern per line (the kit cuts a line at '#'):
  src/pay      the file or folder src/pay and everything below it. A '/' at the start or in the middle anchors a
               pattern at the top of the repository: /src/pay is the same.
  src/pay/     a trailing '/' means folders only: everything below the folder src/pay, and a submodule at src/pay
               (git reads a submodule as a folder).
  *.sql, pay   no '/' (but a trailing one): at any depth (db/migrations/001.sql; the folder src/pay).
  src/**/k.*   '**' as a whole name: '**/' at the start is any folder, '/**/' zero or more folders, '/**' at the end
               everything inside. '*', '?' and [a-z] / [!a-z] stay inside one name.
A pattern that matches a folder covers every file below it. Letter case counts (git on Linux, where CI runs).
Refused with a BrainError, not read some other way: '!' (re-include), '\\' (escapes), '**' inside a name
(src/**.py: write src/**/*.py), [[:alpha:]] classes, '[' without ']', and empty, '.' or '..' names (./src, src//pay)."""
from __future__ import annotations

import re

from .common import BrainError, path_base

STAR = "**"                     # an item of a pattern: zero or more whole names (folders)
_ANY = re.compile(r"[^/]+")     # an item: one name, whatever it is
_CACHE = {}


def _regex(items):
    return re.compile("".join("(?:[^/]+/)*" if x is STAR else x.pattern + "/" for x in items))


class Pattern:
    """A parsed risk-path pattern. `items` are the files it covers name by name: STAR, or the regex of one name;
    `folder_items` the folders it covers."""

    def __init__(self, text, items, folder_items):
        self.text = text
        self.items = items
        self.rx, self.folder_rx = _regex(items), _regex(folder_items)

    def matches(self, path, folder=False) -> bool:
        """Does it cover `path` (a file as git names it; folder: a folder, such as a submodule's gitlink)?"""
        return (self.folder_rx if folder else self.rx).fullmatch(str(path) + "/") is not None


def _bad(text, why):
    return BrainError("risk path %r: %s" % (text, why))


def _name(text, name):
    """The regex of one name of a pattern (no '/', not '**')."""
    out, i = [], 0
    while i < len(name):
        c = name[i]
        if c == "*":
            if name.startswith("**", i):
                raise _bad(text, "'**' only as a whole name (src/**/*.py, **/x, src/**), not inside one")
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            neg = j < len(name) and name[j] in "!^"
            if neg:
                j += 1
            ranges = []
            while j < len(name) and (name[j] != "]" or not ranges):     # a ']' first is one of the characters
                if name.startswith("[:", j):
                    raise _bad(text, "[:class:] is not supported: write the characters ([a-z0-9])")
                lo = hi = name[j]
                if j + 2 < len(name) and name[j + 1] == "-" and name[j + 2] != "]":
                    hi = name[j + 2]
                    j += 3
                else:
                    j += 1
                if lo > hi:
                    raise _bad(text, "the range %s-%s is empty" % (lo, hi))
                ranges.append(re.escape(lo) + ("-" + re.escape(hi) if hi != lo else ""))
            if j >= len(name):
                raise _bad(text, "'[' without its ']'")
            out.append("(?!/)[%s%s]" % ("^" if neg else "", "".join(ranges)))
            i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out))


def _squash(items):
    out = []
    for x in items:
        if not (x is STAR and out and out[-1] is STAR):
            out.append(x)
    return out


def parse(text) -> Pattern:
    """The pattern of one gates/risk-paths line (comment already cut). Raises BrainError for what is not supported."""
    hit = _CACHE.get(text)
    if hit is not None:
        return hit
    pat = str(text).strip()
    if not pat:
        raise _bad(text, "empty")
    if pat.startswith("!"):
        raise _bad(text, "'!' (re-include) is not supported: list only what is risky")
    if "\\" in pat:
        raise _bad(text, "'\\' is not supported (no escapes; folders are separated by '/')")
    folder = pat.endswith("/")
    if folder:
        pat = pat[:-1]
    anchored = pat.startswith("/")
    if anchored:
        pat = pat[1:]
    names = pat.split("/")
    if any(n in ("", ".", "..") for n in names):
        raise _bad(text, "empty, '.' or '..' names match nothing in git (write src/pay, not ./src/pay or src//pay)")
    items = [] if anchored or len(names) > 1 else [STAR]          # no '/' (but a trailing one): at any depth
    items += [STAR if n == "**" else _name(text, n) for n in names]
    items = _squash(items)
    if names[-1] == "**":                                         # 'x/**': everything inside x, not x itself
        items[-1:] = [_ANY, STAR]
    folder_items = _squash(items + [STAR])                        # a folder it matches covers all below it
    file_items = _squash(items + [_ANY, STAR]) if folder else folder_items    # x/: a file x is not the folder x
    p = Pattern(text, file_items, folder_items)
    if len(_CACHE) > 4096:
        _CACHE.clear()
    _CACHE[text] = p
    return p


def problem(text) -> str:
    """'' when `text` is a pattern these rules read, else why not."""
    try:
        parse(text)
    except BrainError as e:
        return str(e)
    return ""


def matches(path, pattern, folder=False) -> bool:
    """Is the file `path` (as git names it) under the risk-path `pattern`? folder=True: `path` is a folder to git (a
    submodule's gitlink), which src/pay/ covers too. BrainError for a pattern not supported."""
    return parse(pattern).matches(path, folder)


def _meet(names, items, below) -> bool:
    """Is there a path that the items cover and that is `names` (below: `names` or something under them)?"""
    seen = {}

    def go(i, j):
        if (i, j) not in seen:
            if j == len(items):
                r = i == len(names)
            elif i == len(names):
                r = below or all(x is STAR for x in items[j:])
            elif items[j] is STAR:
                r = go(i, j + 1) or go(i + 1, j)
            else:
                r = items[j].fullmatch(names[i]) is not None and go(i + 1, j + 1)
            seen[(i, j)] = r
        return seen[(i, j)]
    return go(0, 0)


def _plain_file(path) -> bool:
    """Is a run's expected path plainly a file: no glob, no trailing '/', an extension in its last name (guide.md,
    .eslintrc.json)? Anything else may be a folder (pay, Makefile, .github, conf.d/): Lean reads a plain Path as
    itself and everything below it, and `brain start --paths` asks for modules."""
    s = str(path).strip()
    return not any(c in s for c in "*?[") and re.fullmatch(r".+\.[^.]+", s.rsplit("/", 1)[-1]) is not None


def meets(path, pattern) -> bool:
    """Could a file that `path` names be under the risk-path `pattern`? `path` is a run's expected path (run.json
    expected_paths, a split unit's Paths). Plainly a file (docs/guide.md) names only itself: it meets what covers
    it. Anything else may be a folder, which names everything below it too, so a parent folder of a risk path (src
    for src/pay/**) meets it, and so does a pattern at any depth (*.sql, pay). A glob counts as its folder
    (common.path_base: Lean reads Paths as fnmatch globs, where * also matches '/'); one at the top level (*.py, **)
    meets every pattern. Errs on the side of yes: it feeds warnings. BrainError for a pattern these rules do not
    support."""
    p = parse(pattern)
    if not str(path or "").strip():
        return False
    base = path_base(path)
    return not base or _meet(base.split("/"), p.items, not _plain_file(path))
