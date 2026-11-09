"""Brain entries, and the only code that edits them (the plan's `brain-tool`).

An entry is one header line and the text under it, up to the next header, the next heading or the end of file:

  <!-- id:T-OMS-1-2 type:pitfall side:builder rel:new src:D01#S030,V-1a2b3c4d5e prop:9f3c2a1b -->
  退款单号是 RF + 9 位；样例文件里的 7 位是旧格式。

Ids are <side letter>-<KEY>-<n> (P product, T tech, I improvement, J project). Two people writing entries for
different requirements can never pick the same id; reflect also skips ids used on open brain/* branches.

What may happen to an entry (brain lint checks every MR against these):
  new          a new id
  same         text unchanged, sources added
  adds-to      the old text stays, as the start of the new text
  replaces X   a new entry; X moves to <FILE>-archive.md (status:replaced by:<new>)
  repeats      the same lesson came back: repeats +1 (2 or more: make it a gate)
  withdraws    the entry moves to the archive (status:withdrawn), with the reason
  purge        security or data owner only: removed from the file and the archive, id kept in PURGED.md
Archives only grow. People and AI never hand-edit them; these functions do.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess

from .common import BrainError, utcnow

FILES = {
    "PRODUCT": {"letter": "P", "side": "product", "types": ("principle", "decision", "model", "correction")},
    "TECH": {"letter": "T", "side": "builder",
             "types": ("principle", "decision", "model", "guideline", "pitfall", "correction")},
    "IMPROVEMENT": {"letter": "I", "side": "builder", "types": ("pitfall", "process", "guideline", "decision")},
    "PROJECT": {"letter": "J", "side": "shared", "types": ("decision", "process")},
}
LETTER_FILE = {v["letter"]: k for k, v in FILES.items()}
TYPES = ("principle", "correction", "decision", "model", "guideline", "pitfall", "process")
RELS = ("new", "same", "adds-to", "replaces", "repeats", "withdraws")
PURGE_REASONS = ("pii", "secret", "confidential")
PURGED_FILE = "PURGED.md"

NEW_ID_RE = re.compile(r"^[A-Z]-[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}-[1-9][0-9]{0,3}$")
LEGACY_ID_RE = re.compile(r"^[A-Z]-[0-9]{1,5}$")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}$")
HEADER_RE = re.compile(r"^\s*<!--\s*id:(\S+)(.*?)-->\s*$")
LOOSE_HEADER_RE = re.compile(r"<!--\s*id:")
HEADING_RE = re.compile(r"^#{1,6}\s")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
PURGED_RE = re.compile(r"^- ([A-Z]-[A-Z0-9-]+) · ")
PURGED_LINE_RE = re.compile(r"^- ([A-Z]-[A-Z0-9-]+) · ([0-9]{4}-[0-9]{2}-[0-9]{2}) · (%s) · approved by "
                            r"([a-z0-9][a-z0-9._-]{0,63})(?: · props ([0-9a-f]{8}(?:,[0-9a-f]{8})*))?$"
                            % "|".join(PURGE_REASONS))
# Sources are references, never free text: segments, packages, decisions, vouchers, runs, entries, intents, keys.
SRC_ITEM_RE = re.compile(r"^(?:D[0-9]{2}#S[0-9]{3}|PKG-[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}-[1-9][0-9]{0,3}"
                         r"|D-[1-9][0-9]{0,4}|V-[0-9a-f]{10}|RUN-[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}-[1-9][0-9]{0,5}"
                         r"|[A-Z]-[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6}-[1-9][0-9]{0,3}|[A-Z]-[0-9]{1,5}"
                         r"|[0-9]{6}-[a-z0-9][a-z0-9-]{0,80}|[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6})$")
PROP_RE = re.compile(r"^[0-9a-f]{8}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
SECTION_RE = re.compile(r"^[^\n#<>`\[\]|]{1,40}$")
SIDES = ("product", "builder", "shared")
ORDER = ("type", "side", "rel", "repeats", "src", "prop", "status", "by", "archived")
KNOWN_KEYS = set(ORDER)
MAIN_NAMES = tuple("%s.md" % k for k in FILES)
ARCHIVE_NAMES = tuple("%s-archive.md" % k for k in FILES)
ALLOWED_NAMES = MAIN_NAMES + ARCHIVE_NAMES + (PURGED_FILE,)


def today() -> str:
    """The UTC date (common.utcnow is timezone-aware: no deprecated naive utcnow)."""
    return utcnow().strftime("%Y-%m-%d")


def norm(text) -> str:
    """Text compared for same / adds-to: trailing spaces and blank lines at the ends do not count."""
    lines = [l.rstrip() for l in (text if isinstance(text, list) else text.split("\n"))]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def file_key(name) -> str:
    """'TECH', 'TECH.md', 'brain/TECH.md', 'tech' -> 'TECH' (main files only)."""
    base = os.path.basename(str(name)).upper()
    base = base[:-3] if base.endswith(".MD") else base
    if base not in FILES:
        raise BrainError("unknown Brain file %r (one of %s)" % (name, ", ".join(FILES)))
    return base


def split_src(value) -> list:
    out = []
    for part in re.split(r"[,\s]+", value or ""):
        if part and part not in out:
            out.append(part)
    return out


def _add_prop(e, prop):
    if prop:
        have = split_src(e.attrs.get("prop"))
        if prop not in have:
            e.attrs["prop"] = ",".join(have + [prop])


class Entry:
    def __init__(self, eid, attrs, body, line=0, extra=None, dups=None):
        self.id = eid
        self.attrs = attrs           # dict, rel value may be "replaces T-X"
        self.body = body             # list of lines
        self.line = line
        self.extra = extra or []     # tokens the header could not read
        self.dups = dups or []       # attributes given twice

    @classmethod
    def parse_header(cls, line, line_no=0):
        m = HEADER_RE.match(line)
        if not m:
            return None
        attrs, extra, dups, last = {}, [], [], None
        for tok in m.group(2).split():
            if ":" in tok:
                k, v = tok.split(":", 1)
                if k in attrs:
                    dups.append(k)
                attrs[k] = v
                last = k
            elif last == "rel" and " " not in attrs["rel"]:
                attrs["rel"] += " " + tok
            else:
                extra.append(tok)
        return cls(m.group(1), attrs, [], line_no, extra, dups)

    @property
    def type(self):
        return self.attrs.get("type", "")

    @property
    def rel(self):
        parts = self.attrs.get("rel", "").split(" ", 1)
        return parts[0], (parts[1] if len(parts) > 1 else "")

    @property
    def src(self):
        return split_src(self.attrs.get("src", ""))

    @property
    def repeats(self):
        try:
            return int(self.attrs.get("repeats", "0"))
        except ValueError:
            return -1

    def text(self):
        return norm(self.body)

    def header(self):
        keys = [k for k in ORDER if k in self.attrs] + sorted(k for k in self.attrs if k not in ORDER)
        return "<!-- id:%s %s -->" % (self.id, " ".join("%s:%s" % (k, self.attrs[k]) for k in keys))

    def lines(self):
        return [self.header()] + self.body

    def add_src(self, src):
        have = self.src
        for s in split_src(",".join(src) if isinstance(src, (list, tuple)) else src):
            if s not in have:
                have.append(s)
        self.attrs["src"] = ",".join(have)


class BrainFile:
    """One Markdown file as blocks: plain lines (kept as they are) and entries."""

    def __init__(self, name, text):
        self.name = name                       # e.g. TECH.md or TECH-archive.md
        self.blocks = []
        self.loose = []                        # line numbers with an entry header that is not on its own line
        self._parse(text)

    @property
    def archive(self):
        return self.name.endswith("-archive.md")

    @property
    def key(self):
        base = self.name[:-len("-archive.md")] if self.archive else self.name[:-3]
        if base not in FILES:
            raise BrainError("unknown Brain file %s" % self.name)
        return base

    def free_lines(self):
        """Lines outside entries (headings, help text): runs read them too."""
        return [b for b in self.blocks if isinstance(b, str)]

    def _parse(self, text):
        cur, fence = None, False
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        for i, line in enumerate(lines, 1):
            if FENCE_RE.match(line):
                fence = not fence
            head = None if fence or line.lstrip().startswith(">") else Entry.parse_header(line, i)
            if head:
                self._close(cur)
                cur = head
                self.blocks.append(cur)
                continue
            if not fence and LOOSE_HEADER_RE.search(line) and not line.lstrip().startswith(">"):
                self.loose.append(i)
            if cur is not None and not fence and HEADING_RE.match(line):
                self._close(cur)
                cur = None
            if cur is not None:
                cur.body.append(line)
            else:
                self.blocks.append(line)
        self._close(cur)

    def _close(self, e):
        """Blank lines after an entry belong to the file, not to the entry."""
        if e is None:
            return
        tail = []
        while e.body and not e.body[-1].strip():
            tail.append(e.body.pop())
        self.blocks.extend(tail)

    def entries(self):
        return [b for b in self.blocks if isinstance(b, Entry)]

    def render(self):
        out = []
        for b in self.blocks:
            out.extend(b.lines() if isinstance(b, Entry) else [b])
        return "\n".join(out).rstrip("\n") + "\n"

    def index(self, eid):
        for i, b in enumerate(self.blocks):
            if isinstance(b, Entry) and b.id == eid:
                return i
        return -1

    def remove(self, eid):
        i = self.index(eid)
        if i < 0:
            return None
        e = self.blocks.pop(i)
        # drop one blank line around the hole so the file does not grow gaps
        if i < len(self.blocks) and isinstance(self.blocks[i], str) and not self.blocks[i].strip():
            if i == 0 or (isinstance(self.blocks[i - 1], str) and not self.blocks[i - 1].strip()):
                self.blocks.pop(i)
        return e

    def insert(self, e, at=None, section=None):
        if at is not None:
            self.blocks[at:at] = [e]
            return
        pos = len(self.blocks)
        if section:
            pos = self._section_end(section)
        while pos > 0 and isinstance(self.blocks[pos - 1], str) and not self.blocks[pos - 1].strip():
            pos -= 1
        new = ["", e]
        if pos < len(self.blocks):
            new.append("")
        self.blocks[pos:pos] = new

    def _section_end(self, section):
        title = section.strip().lstrip("#").strip()
        start = None
        for i, b in enumerate(self.blocks):
            if isinstance(b, str) and re.match(r"^##\s+%s\s*$" % re.escape(title), b):
                start = i
                break
        if start is None:
            while self.blocks and isinstance(self.blocks[-1], str) and not self.blocks[-1].strip():
                self.blocks.pop()
            self.blocks += ["", "## " + title]
            return len(self.blocks)
        for j in range(start + 1, len(self.blocks)):
            b = self.blocks[j]
            if isinstance(b, str) and re.match(r"^#{1,2}\s", b):
                return j
        return len(self.blocks)


ARCHIVE_HEAD = ("# {name} archive\n\n> 被覆盖或撤回的条目，只追加。由 `brain entry` / `brain reflect` 生成，不要手改；"
                "run 不读这个文件。\n")


class Brain:
    """All Brain files of one tree (a directory, or a git ref), and the deterministic edits."""

    def __init__(self, texts, path=None, problems=None):
        self.path = path
        self.problems = list(problems or [])      # what loading found: symlinks, unknown files, bad encoding
        for name in texts:
            if name not in ALLOWED_NAMES:
                self.problems.append("%s: unknown file in the Brain (allowed: %s)" % (name, ", ".join(ALLOWED_NAMES)))
        self.files = {name: BrainFile(name, text) for name, text in texts.items()
                      if name in ALLOWED_NAMES and name != PURGED_FILE}
        self.purged_text = texts.get(PURGED_FILE, "")
        self.dirty = set()

    # ----- loading -----
    @classmethod
    def load(cls, brain_dir):
        texts, probs = {}, []
        if os.path.islink(brain_dir):
            probs.append("%s is a symlink: the Brain must be real files" % brain_dir)
        elif os.path.isdir(brain_dir):
            for name in sorted(os.listdir(brain_dir)):
                p = os.path.join(brain_dir, name)
                if os.path.islink(p):
                    probs.append("%s: symlink in the Brain (lint and CI would not see edits to its target)" % name)
                elif os.path.isdir(p):
                    probs.append("%s/: subdirectory in the Brain" % name)
                else:
                    with open(p, "rb") as f:
                        raw = f.read()
                    try:
                        texts[name] = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                    except UnicodeDecodeError:
                        probs.append("%s: not UTF-8 text" % name)
        return cls(texts, brain_dir, probs)

    @classmethod
    def from_git(cls, repo, ref, rel_dir="brain"):
        """The Brain as it is at `ref` (empty when the ref has no Brain). A ref that does not exist is an error:
        an empty base would let every change through."""
        def g(*args):
            return subprocess.run(["git", "-C", repo] + list(args), capture_output=True)
        if g("rev-parse", "-q", "--verify", "%s^{commit}" % ref).returncode != 0:
            raise BrainError("%s is not a commit here (fetch it first)" % ref)
        texts, probs = {}, []
        top = g("ls-tree", "-z", ref, "--", rel_dir.rstrip("/")).stdout.split(b"\0")
        top = [t for t in top if t]
        if not top:
            return cls({}, problems=probs)
        if top[0].split(b" ", 1)[0] == b"120000":
            return cls({}, problems=["%s is a symlink at %s" % (rel_dir, ref)])
        for row in g("ls-tree", "-z", "%s:%s" % (ref, rel_dir)).stdout.split(b"\0"):
            if not row:
                continue
            meta, name = row.split(b"\t", 1)
            mode, kind, sha = meta.decode().split(" ")
            try:
                name = name.decode("utf-8")
            except UnicodeDecodeError:
                probs.append("a file name in %s is not UTF-8" % rel_dir)
                continue
            if mode == "120000":
                probs.append("%s: symlink in the Brain at %s" % (name, ref))
            elif kind != "blob":
                probs.append("%s/: subdirectory in the Brain at %s" % (name, ref))
            else:
                raw = g("cat-file", "blob", sha).stdout
                try:
                    texts[name] = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                except UnicodeDecodeError:
                    probs.append("%s: not UTF-8 text at %s" % (name, ref))
        return cls(texts, problems=probs)

    def copy(self):
        texts = {n: f.render() for n, f in self.files.items()}
        if self.purged_text:
            texts[PURGED_FILE] = self.purged_text
        return Brain(texts, self.path, self.problems)

    # ----- reading -----
    def main_files(self):
        return [f for f in self.files.values() if f.name in MAIN_NAMES]

    def archive_files(self):
        return [f for f in self.files.values() if f.archive]

    def main(self):
        """{id: (file, entry)} for current entries."""
        return {e.id: (f, e) for f in self.main_files() for e in f.entries()}

    def archived(self):
        return {e.id: (f, e) for f in self.archive_files() for e in f.entries()}

    def purged(self):
        out = {}
        for line in self.purged_text.split("\n"):
            m = PURGED_RE.match(line)
            if m:
                out[m.group(1)] = line
        return out

    def purged_props(self):
        out = {}
        for eid, line in self.purged().items():
            m = PURGED_LINE_RE.match(line)
            for h in split_src(m.group(5) if m else ""):
                out[h] = eid
        return out

    def used_ids(self):
        ids = set(self.purged())
        for f in self.files.values():
            ids.update(e.id for e in f.entries())
        return ids

    def props(self):
        """{proposal hash: entry id}: which reflect proposals are already in (entries, archives, purged)."""
        out = self.purged_props()
        out.update({h: e.id for f in self.files.values() for e in f.entries() for h in split_src(e.attrs.get("prop"))})
        return out

    def get(self, eid):
        hit = self.main().get(eid)
        if not hit:
            where = "archived" if eid in self.archived() else ("purged" if eid in self.purged() else "not found")
            raise BrainError("entry %s is %s" % (eid, where))
        return hit

    def next_id(self, key, letter, used_extra=()):
        if not KEY_RE.match(key or ""):
            raise BrainError("requirement key %r is not like OMS-12" % key)
        used = self.used_ids() | set(used_extra)
        n = 1
        while "%s-%s-%d" % (letter, key, n) in used:
            n += 1
        return "%s-%s-%d" % (letter, key, n)

    # ----- editing -----
    def _file(self, key, archive=False):
        name = "%s%s.md" % (key, "-archive" if archive else "")
        if name not in self.files:
            self.files[name] = BrainFile(name, ARCHIVE_HEAD.format(name=key) if archive else "# %s\n" % key)
        self.dirty.add(name)
        return self.files[name]

    def _to_archive(self, f, e, status, note=None, by=None, remove=True):
        if remove:
            f.remove(e.id)
        self.dirty.add(f.name)
        e.attrs["status"] = status
        if by:
            e.attrs["by"] = by
        e.attrs["archived"] = today()
        if note:
            e.body = e.body + ["", "> " + note.replace("\n", " ")]
        self._file(f.key, archive=True).insert(e)

    @staticmethod
    def _body(text):
        body = norm(text)
        if not body:
            raise BrainError("entry text is empty")
        lines = body.split("\n")
        if any(LOOSE_HEADER_RE.search(l) or HEADING_RE.match(l) for l in lines):
            raise BrainError("entry text may not contain headings or entry headers")
        if sum(1 for l in lines if FENCE_RE.match(l)) % 2:
            raise BrainError("entry text has an unclosed code fence")
        return lines

    def add(self, key_file, etype, text, key, src, section=None, prop=None, used_extra=(), rel="new"):
        k = file_key(key_file)
        spec = FILES[k]
        if etype not in spec["types"]:
            raise BrainError("%s takes %s entries, not %r" % (k, ", ".join(spec["types"]), etype))
        src = split_src(",".join(src) if isinstance(src, (list, tuple)) else src)
        if not src:
            raise BrainError("an entry needs a source (src): a segment, a decision, a voucher or a run")
        eid = self.next_id(key, spec["letter"], used_extra)
        attrs = {"type": etype, "side": spec["side"], "rel": rel, "src": ",".join(src)}
        if prop:
            attrs["prop"] = prop
        e = Entry(eid, attrs, self._body(text))
        f = self._file(k)
        return eid, f, e, section

    def new(self, key_file, etype, text, key, src, section=None, prop=None, used_extra=()):
        if section is not None and not SECTION_RE.match(section.strip().lstrip("#").strip() or "-"):
            raise BrainError("section %r: up to 40 characters, no #, <, >, `, [, ], |" % section)
        eid, f, e, section = self.add(key_file, etype, text, key, src, section, prop, used_extra)
        f.insert(e, section=section)
        return eid

    def replace(self, old_id, text, key, src, etype=None, prop=None, used_extra=()):
        f, old = self.get(old_id)
        eid, _, e, _ = self.add(f.key, etype or old.type, text, key, src, prop=prop, used_extra=used_extra,
                                rel="replaces " + old_id)
        f.blocks[f.index(old_id)] = e          # the new entry takes the old one's place
        self._to_archive(f, old, "replaced", by=eid, remove=False)
        return eid

    def withdraw(self, eid, reason, src, prop=None):
        f, e = self.get(eid)
        if not norm(reason or ""):
            raise BrainError("say why the entry is withdrawn")
        e.add_src(src)
        _add_prop(e, prop)
        self._to_archive(f, e, "withdrawn", note="撤回：" + norm(reason))
        return eid

    def same(self, eid, src, prop=None):
        f, e = self.get(eid)
        e.add_src(src)
        _add_prop(e, prop)
        self.dirty.add(f.name)
        return eid

    def adds_to(self, eid, text, src, prop=None):
        f, e = self.get(eid)
        e.body = e.body + [""] + self._body(text)
        e.add_src(src)
        e.attrs["rel"] = "adds-to"
        _add_prop(e, prop)
        self.dirty.add(f.name)
        return eid

    def repeat(self, eid, src, prop=None):
        f, e = self.get(eid)
        e.attrs["repeats"] = str(max(e.repeats, 0) + 1)
        e.add_src(src)
        _add_prop(e, prop)
        self.dirty.add(f.name)
        return e.repeats

    def purge(self, eid, reason, approver):
        """Remove an entry everywhere (personal data, secrets, confidential material taken in by mistake).
        Git history still has it: rewriting history is a separate, manual step."""
        if reason not in PURGE_REASONS:
            raise BrainError("purge is only for %s" % ", ".join(PURGE_REASONS))
        if not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", approver or ""):
            raise BrainError("--approved-by must be the GitLab user of the security or data owner")
        props, found = [], False
        for f in list(self.files.values()):
            while True:
                e = f.remove(eid)
                if e is None:
                    break
                found = True
                self.dirty.add(f.name)
                # keep the proposal hashes so reflect never writes the purged text back
                props += [h for h in split_src(e.attrs.get("prop")) if h not in props]
        if not found:
            raise BrainError("entry %s not found" % eid)
        if not self.purged_text:
            self.purged_text = ("# PURGED\n\n> 被清除的条目号（正文已删除）。只追加；清除要安全或数据负责人批准，"
                                "git 历史需另外改写。\n\n")
        self.purged_text = self.purged_text.rstrip("\n") + "\n- %s · %s · %s · approved by %s%s\n" % (
            eid, today(), reason, approver, " · props " + ",".join(props) if props else "")
        self.dirty.add(PURGED_FILE)
        return eid

    def save(self, brain_dir=None):
        d = brain_dir or self.path
        if not d:
            raise BrainError("nowhere to save the Brain")
        os.makedirs(d, exist_ok=True)
        for name in sorted(self.dirty):
            text = self.purged_text if name == PURGED_FILE else self.files[name].render()
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(text)
        written = sorted(self.dirty)
        self.dirty = set()
        return written


def prop_hash(*parts) -> str:
    return hashlib.sha256("\x00".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:8]
