"""File -> Markdown text, with hidden content moved aside. Stdlib only (zip + XML for Office files).

Each converter returns a Doc. Hidden content (HTML comments, display:none, white text with no background and text
under 4pt in Word, HTML and xlsx, hidden sheets/rows/columns, ';;;' cells, hidden slides, shapes and groups) goes to
Doc.hidden, never into Doc.text.
"""
from __future__ import annotations

import colorsys
import csv
import io
import lzma
import os
import posixpath
import re
import shutil
import struct
import subprocess
import zipfile
import zlib
from html.parser import HTMLParser
from xml.etree import ElementTree as ET
from xml.parsers import expat

from . import checks

ZW_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u202a-\u202e\u2066-\u2069]")
HTML_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.S)
# a tag; a quoted attribute value may hold a ">" (if the quotes do not pair up, up to the first ">")
TAG_RE = re.compile(r"""<(/?)([A-Za-z][A-Za-z0-9]*)\b((?:"[^"]*"|'[^']*'|[^>"'])*|[^>]*)>""")
ATTR_RE = re.compile(r"""([^\s=/>"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+)))?""")
HIDING_CLASSES = {"hidden", "hide", "d-none", "invisible", "sr-only", "visually-hidden"}
CSS_NAMED = {   # near-white names, and common darker ones (an unknown name is no colour we can judge)
    "white": "ffffff", "snow": "fffafa", "ivory": "fffff0", "ghostwhite": "f8f8ff", "whitesmoke": "f5f5f5",
    "floralwhite": "fffaf0", "mintcream": "f5fffa", "azure": "f0ffff", "seashell": "fff5ee", "black": "000000",
    "silver": "c0c0c0", "gray": "808080", "grey": "808080", "maroon": "800000", "red": "ff0000", "purple": "800080",
    "fuchsia": "ff00ff", "green": "008000", "lime": "00ff00", "olive": "808000", "yellow": "ffff00", "navy": "000080",
    "blue": "0000ff", "teal": "008080", "aqua": "00ffff", "cyan": "00ffff", "orange": "ffa500", "darkblue": "00008b",
    "darkred": "8b0000", "darkgreen": "006400", "darkgray": "a9a9a9", "darkgrey": "a9a9a9", "dimgray": "696969",
    "lightgray": "d3d3d3", "brown": "a52a2a", "gold": "ffd700", "steelblue": "4682b4", "royalblue": "4169e1",
    "midnightblue": "191970", "darkslategray": "2f4f4f", "slategray": "708090"}
CSS_COLOUR_RE = re.compile(r"#[0-9a-f]{3,8}\b|(?:rgb|hsl)a?\([^)]*\)|\b[a-z]+\b")


def tag_attrs(s) -> dict:
    """{name: value} of the attributes in a tag's text (names lower case; a bare attribute is ''; of a repeated
    attribute the first counts, as in a browser)."""
    out = {}
    for m in ATTR_RE.finditer(s):
        out.setdefault(m.group(1).lower(), next((v for v in m.groups()[1:] if v is not None), ""))
    return out


def _css(style) -> dict:
    """{property: value} of an inline style: comments out, names and values lower case; a later declaration wins,
    unless an earlier one of the same property is !important and it is not."""
    out, strong = {}, set()
    for decl in re.sub(r"/\*.*?(?:\*/|$)", " ", style or "", flags=re.S).split(";"):
        k, sep, v = decl.partition(":")
        if not sep:
            continue
        k, v = k.strip().lower(), v.strip().lower()
        important = re.search(r"!\s*important\s*$", v)
        v = v[:important.start()].strip() if important else v
        if k in strong and not important:
            continue
        out[k] = v
        if important:
            strong.add(k)
    return out


def _css_rgba(v, bare_hex=False):
    """(r, g, b, alpha) of a CSS colour (#rgb, #rrggbb(aa), rgb(), hsl() with or without alpha, a common name), else
    None. bare_hex: an HTML colour attribute, where the # may be left out (<font color="fefefe">)."""
    v = (v or "").strip().lower()
    if v == "transparent":
        return 0, 0, 0, 0.0
    v = "#" + CSS_NAMED[v] if v in CSS_NAMED else ("#" + v if bare_hex and re.fullmatch(r"[0-9a-f]{3}|[0-9a-f]{6}", v)
                                                     else v)
    try:
        m = re.fullmatch(r"#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})", v)
        if m:
            h = m.group(1)
            h = "".join(c * 2 for c in h) if len(h) <= 4 else h
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), (int(h[6:8], 16) / 255 if len(h) == 8 else 1.0)
        m = re.fullmatch(r"(rgb|hsl)a?\(\s*([0-9.]+)(deg|%)?[\s,]+([0-9.]+)(%?)[\s,]+([0-9.]+)(%?)"
                         r"(?:[\s,/]+([0-9.]+)(%?))?\s*\)", v)
        if not m:
            return None
        alpha = 1.0 if m.group(8) is None else float(m.group(8)) / (100 if m.group(9) else 1)
        if m.group(1) == "hsl":
            r, g, b = colorsys.hls_to_rgb(float(m.group(2)) / 360 % 1, float(m.group(6)) / 100, float(m.group(4)) / 100)
            return r * 255, g * 255, b * 255, alpha

        def num(x, pct):
            return float(x) * 255 / 100 if pct else float(x)
        return num(m.group(2), m.group(3) == "%"), num(m.group(4), m.group(5)), num(m.group(6), m.group(7)), alpha
    except ValueError:
        return None


def _css_clear(v, bare_hex=False) -> bool:
    """A text colour that does not show on any background: transparent, or nearly."""
    c = _css_rgba(v, bare_hex)
    return c is not None and c[3] < 0.1


def _css_white(v, bare_hex=False) -> bool:
    """A text colour that does not show on a white page: white or near white."""
    c = _css_rgba(v, bare_hex)
    return c is not None and min(c[:3]) >= 240


def _font_tiny(v) -> bool:
    """A font size under 4pt (the line Word's check uses): 0, 1px, 0.2em ..."""
    m = re.match(r"\s*([0-9]*\.?[0-9]+)\s*(px|pt|em|rem|%)?", v or "")
    if not m:
        return False
    pt = float(m.group(1)) * {"px": 0.75, "pt": 1, "em": 12, "rem": 12, "%": 0.12}.get(m.group(2) or "px")
    return pt < 4


def has_background(attrs):
    """What the element paints behind its text: True a colour white text shows on, False white, None nothing (or
    nothing we can tell: transparent, an image): then what is behind it shows."""
    css = _css(attrs.get("style"))
    for v, bare in ((css.get("background-color"), False), (css.get("background"), False),
                    (attrs.get("bgcolor"), True)):
        if not v:
            continue
        cols = [c for c in (_css_rgba(x, bare) for x in CSS_COLOUR_RE.findall(v)) if c is not None and c[3] >= 0.5]
        if cols:
            return any(min(c[:3]) < 240 for c in cols)
    return None


def nearest_background(layers) -> bool:
    """The background that shows: the nearest layer that paints one (an element, then those around it); a white
    page when none does."""
    return next((b for b in reversed(layers) if b is not None), False)


def element_hides(tag, attrs, on_background=False) -> bool:
    """An element renders its content invisibly: the hidden attribute, a hidden input, a hiding class, display:none,
    visibility:hidden, opacity near 0, a font under 4pt, transparent text, or white text with no colour behind it
    (its own background, else on_background: the one that shows through from around it)."""
    css = _css(attrs.get("style"))
    try:
        faint = float(css.get("opacity", "1").rstrip("%")) / (100 if css.get("opacity", "").endswith("%") else 1) < 0.1
    except ValueError:
        faint = False
    colour, bare = css.get("color"), False
    if colour is None and tag == "font":
        colour, bare = attrs.get("color"), True
    own = has_background(attrs)
    behind = own if own is not None else on_background
    return ("hidden" in attrs or (tag == "input" and attrs.get("type", "").lower() == "hidden")
            or bool(HIDING_CLASSES & set(attrs.get("class", "").lower().split()))
            or css.get("display") == "none" or css.get("visibility") in ("hidden", "collapse") or faint
            or _font_tiny(css.get("font-size")) or (tag == "font" and attrs.get("size", "").strip() == "0")
            or (colour is not None and (_css_clear(colour, bare) or (_css_white(colour, bare) and not behind))))


def strip_hidden_html(text):
    """Remove inline HTML elements that render invisibly (nesting-aware). Returns (text, [hidden strings])."""
    out, hidden = [], []
    pos = 0
    tokens = list(TAG_RE.finditer(text))
    around = []                                      # (tag, its background: has_background) of the open elements
    i = 0
    while i < len(tokens):
        t = tokens[i]
        name = t.group(2).lower()
        attrs = tag_attrs(t.group(3) or "")
        bare = name in VOID                          # <div .../> opens a div, as in a browser
        if t.group(1) == "/":
            k = max((k for k, (n, _) in enumerate(around) if n == name), default=None)
            if k is not None:
                del around[k:]
        elif element_hides(name, attrs, nearest_background([b for _, b in around])):
            if bare:                                 # <input type="hidden" value=...>: no content, no end tag
                out.append(text[pos:t.start()])
                hidden.append(t.group(0))
                pos = t.end()
                i += 1
                continue
            depth = 1
            j = i + 1
            while j < len(tokens) and depth:
                u = tokens[j]
                if u.group(2).lower() == name:
                    depth += -1 if u.group(1) == "/" else 1
                j += 1
            end = tokens[j - 1].end() if depth == 0 else len(text)
            out.append(text[pos:t.start()])
            hidden.append(TAG_RE.sub("", text[t.end():tokens[j - 1].start() if depth == 0 else len(text)]))
            pos = end
            i = j
            continue
        elif not bare:
            around.append((name, has_background(attrs)))
        i += 1
    out.append(text[pos:])
    return "".join(out), hidden


MD_FENCE_RE = re.compile(r"(?m)^ {0,3}(?:(`{3,})[^`\n]*|(~{3,})[^\n]*)$")    # up to 3 spaces: 4 is no fence
# what may start a Markdown construct, leftmost first: a comment, a raw HTML tag (its attributes may hold a
# backtick), a fence, a backtick run
MD_TOKEN_RE = re.compile(r"<!--|" + TAG_RE.pattern + r"|(?m:^ {0,3}(?:`{3,}[^`\n]*|~{3,}[^\n]*)$)|`+")
MD_PARA_END_RE = re.compile(r"\n[ \t]*\n|\n {0,3}(?:`{3,}|~{3,})")       # a code span ends in its paragraph


def protect_md_code(text):
    """Markdown code (fenced blocks, inline `code`) is shown as written: HTML in it is an example, not a hidden
    element or a comment. Read left to right as Markdown does: whatever starts first wins, so a comment or a tag
    that starts before a backtick hides it, and an escaped backtick starts nothing. Returns (text with each code
    part swapped for a mark, restore(s) putting them back)."""
    spans = []
    pos = 0
    while True:
        m = MD_TOKEN_RE.search(text, pos)
        if not m:
            break
        tok = m.group(0)
        if tok == "<!--":
            close = text.find("-->", m.end())
            pos = len(text) if close < 0 else close + 3
        elif tok.startswith("<"):
            pos = m.end()
        elif m.group(0).lstrip(" ")[:3] in ("```", "~~~") and (m.start() == 0 or text[m.start() - 1] == "\n"):
            fence = re.match(r" *(`+|~+)", tok).group(1)
            close = re.compile(r"(?m)^ {0,3}%s{%d,}[ \t]*$" % (re.escape(fence[0]), len(fence))).search(text, m.end())
            end = close.end() if close else len(text)        # an unclosed fence runs to the end
            spans.append((m.start(), end))
            pos = end
        else:
            a = m.start()
            slashes = len(text[:a]) - len(text[:a].rstrip("\\"))
            if slashes % 2:                                    # \` is a plain backtick
                a += 1
            n = m.end() - a
            pos = m.end()
            if n == 0:
                continue
            stop = MD_PARA_END_RE.search(text, pos)
            close = re.compile(r"(?<!`)`{%d}(?!`)" % n).search(text, pos, stop.start() if stop else len(text))
            if close:
                spans.append((a, close.end()))
                pos = close.end()
    taken = set(text)
    mark = next(chr(c) for c in list(range(0xE000, 0xF900)) + list(range(0xF0000, 0xFFFFE)) if chr(c) not in taken)
    parts, kept, last = [], [], 0
    for a, b in spans:
        parts.append(text[last:a] + "%s%d%s" % (mark, len(kept), mark))
        kept.append(text[a:b])
        last = b
    parts.append(text[last:])
    back = re.compile("%s([0-9]+)%s" % (re.escape(mark), re.escape(mark)))
    return "".join(parts), lambda s: back.sub(lambda m: kept[int(m.group(1))], s)


MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
TEXT_EXT = {".md", ".markdown", ".txt", ".csv", ".tsv", ".log", ".json", ".yaml", ".yml"}
HTML_EXT = {".html", ".htm"}
REFUSED_EXT = {".doc", ".xls", ".ppt", ".rtf", ".zip", ".7z", ".rar", ".tar", ".gz", ".xlsm", ".docm", ".pptm",
               ".exe", ".dll", ".dmg", ".pkg", ".msg", ".eml"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".svg"}
MAX_UNZIPPED = 200 * 1024 * 1024
MAX_RATIO = 200


class Refused(Exception):
    """The file is not taken in (with the reason for the person)."""


class Doc:
    def __init__(self, name, kind):
        self.name = name
        self.kind = kind
        self.text = ""
        self.hidden = []      # [(where, text)]
        self.images = 0
        self.notes = []       # things the person should know (e.g. "no hidden-text check for PDF")
        self.labels = []      # classification markers found in properties/headers

    def hide(self, where, text):
        t = ZW_RE.sub("", text or "").strip()
        if t:
            self.hidden.append((where, t))


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    read = []
    for enc in ("gb18030", "big5hkscs", "cp950"):
        try:
            read.append(data.decode(enc))
        except UnicodeDecodeError:
            pass
    if not read:
        raise Refused("文本编码无法识别（不是 UTF-8 / GB18030 / Big5）")
    # GB18030 takes almost any byte pairs, Big5 too: the reading with more common characters is the right one (on
    # a tie, GB18030)
    return max(read, key=_cjk_plausible)


def _cjk_plausible(text, sample=20000) -> float:
    """The share of the Chinese characters (and private-use, kana, Greek, Cyrillic ... ones) in the first `sample` of
    them that are common: in GB2312 level 1 or Big5's frequent set. Big5 read as GB18030, or GB read as Big5, gives
    many rare, private-use and kana characters (Big5 punctuation becomes private use in GB18030)."""
    good = seen = 0
    for c in text:
        o = ord(c)
        if MISREAD_RE.match(c):                      # kana, Greek, Cyrillic, bopomofo, box drawing: a misread
            seen += 1
        elif 0x4E00 <= o <= 0x9FFF or 0xE000 <= o <= 0xF8FF or 0x3400 <= o <= 0x4DBF or o >= 0x20000:
            seen += 1
            good += _common_hanzi(c)
        else:
            continue
        if seen >= sample:
            break
    return good / seen if seen else 0.0


# what a Big5 text read as GB18030 (or the other way) is full of, and a Chinese requirement hardly has
MISREAD_RE = re.compile("[\u0370-\u04ff\u2500-\u257f\u3041-\u30ff\u3105-\u312f]")


def _common_hanzi(c) -> bool:
    for enc, lo, hi in (("gb2312", 0xB0A1, 0xD7FE), ("big5", 0xA440, 0xC67E)):
        try:
            b = c.encode(enc)
        except UnicodeEncodeError:
            continue
        if len(b) == 2 and lo <= (b[0] << 8 | b[1]) <= hi:
            return True
    return False


def sniff(path) -> str:
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"
    if b"\x00" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "binary"
    return "text"


# ---------- text / markdown ----------

def header_row(rows) -> int:
    """Which of the rows is a table's header: among the first rows with two or more cells (one, for one column), the
    first that names a column gate 4 masks (负责人, Owner ...: a date or title row above it is no header), else the
    first of them."""
    need = min(2, len(rows[0][1]))
    full = [k for k, (_, vals) in enumerate(rows) if sum(1 for v in vals if v.strip()) >= need]
    named = [k for k in full[:10] if any(checks.NAME_COL_RE.search(v.strip()) for v in rows[k][1])]
    return named[0] if named else (full[0] if full else 0)


def table_markdown(rows) -> str:
    """rows: [(row number, [cell, ...])], all the same width -> a Markdown table. Its header is the first row with
    two or more cells (see header_row): a sheet or a csv export has its column names there, and gate 4 finds the name
    columns (负责人, Owner ...) by the table header, as in a Markdown table. Rows above it (a title, a marking) are
    lines before the table. The first column (行) keeps the row numbers; a line break in a cell is <br>
    (张伟 / 李娜 on two lines are two people)."""
    def cell(v):
        return "<br>".join(" ".join(x.split()) for x in v.split("\n") if x.strip()).replace("|", "\\|")

    width = len(rows[0][1])
    h = header_row(rows)
    before = ["第 %d 行：%s" % (r, " · ".join(cell(v) for v in vals if v.strip())) for r, vals in rows[:h]]
    lines = ["| 行 | " + " | ".join(cell(v) for v in rows[h][1]) + " |", "|" + "---|" * (width + 1)]
    lines += ["| %d | %s |" % (r, " | ".join(cell(v) for v in vals)) for r, vals in rows[h + 1:]]
    return "\n\n".join(before + ["\n".join(lines)])


def _csv_rows(text, ext):
    """[(record number, [cell, ...])] of a csv / tsv (the delimiter of a .csv: the one of , ; tab that splits the
    first line most), empty records left out, all padded to one width."""
    first = next((x for x in text.split("\n") if x.strip()), "")
    delims = ("\t",) if ext == ".tsv" else (",", ";", "\t")
    delim = max(delims, key=lambda c: len(next(csv.reader([first], delimiter=c), [])))
    rows = [(n, r) for n, r in enumerate(csv.reader(io.StringIO(text), delimiter=delim), 1)
            if any(x.strip() for x in r)]
    width = max((len(r) for _, r in rows), default=0)
    return [(n, r + [""] * (width - len(r))) for n, r in rows]


def convert_text(path, name) -> Doc:
    ext = os.path.splitext(name)[1].lower()
    with open(path, "rb") as f:
        text = decode_text(f.read()).replace("\r\n", "\n").replace("\r", "\n")
    d = Doc(name, "markdown" if ext in (".md", ".markdown") else "csv" if ext in (".csv", ".tsv") else "text")
    text, restore = protect_md_code(text) if d.kind == "markdown" else (text, lambda s: s)
    for m in HTML_COMMENT_RE.finditer(text):
        d.hide("HTML 注释", restore(m.group(1)))
    text = HTML_COMMENT_RE.sub("", text)
    text, hid = strip_hidden_html(text)
    for h in hid:
        d.hide("隐藏样式的文字", restore(h))
    text = restore(text)
    if ZW_RE.search(text):
        d.notes.append("去掉了零宽或方向控制字符 %d 个" % len(ZW_RE.findall(text)))
        text = ZW_RE.sub("", text)
    d.images = len(MD_IMAGE_RE.findall(text))
    if d.kind == "csv":
        # a table, as the same rows in an xlsx or a Markdown file: gate 4 reads the name columns by the header
        try:
            rows = _csv_rows(text, ext)
        except csv.Error as e:
            rows = None
            d.notes.append("读不成表格（%s），按纯文本收：表里人名列的人名没有按列打码" % e)
        if rows:
            d.text = table_markdown(rows) + "\n"
            return d
        d.kind = "text"
    if d.kind == "text":
        fence = "````" if "```" in text else "```"
        text = "%stext\n%s\n%s" % (fence, text.strip("\n"), fence)
    d.text = text
    return d


# ---------- HTML ----------

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "nav", "aside", "ul", "ol", "dl", "dt", "dd",
         "form", "fieldset", "blockquote", "pre", "figure", "figcaption", "label", "button"}


class _HTML(HTMLParser):
    def __init__(self, doc):
        super().__init__(convert_charrefs=True)
        self.doc = doc
        self.out = []
        self.stack = []          # (tag, hidden?, its background: has_background)
        self.hidden_buf = []
        self.skip_depth = 0      # inside script/style/template
        self.row = None
        self.table_rows = []
        self.scripts = 0

    @property
    def hidden(self):
        return any(h for _, h, _ in self.stack)

    def handle_starttag(self, tag, attrs):
        a = {}
        for k, v in attrs:                           # of a repeated attribute the first counts, as in a browser
            a.setdefault(k.lower(), v or "")
        if tag in ("script", "style", "noscript", "template"):
            self.skip_depth += 1
            self.scripts += tag == "script"
            return
        on_bg = nearest_background([bg for _, _, bg in self.stack])
        if tag == "img":
            self.doc.images += 1
            if a.get("alt") and not self.skip_depth:
                if self.hidden or element_hides(tag, a, on_bg):
                    self.doc.hide("HTML 隐藏图片的说明文字", a["alt"])
                else:
                    self.out.append(" [图片: %s] " % a["alt"])
            return
        if tag in VOID:
            if tag == "br" and not self.skip_depth:
                (self.hidden_buf if self.hidden else self.out).append("\n")
            return
        hid = element_hides(tag, a, on_bg)
        self.stack.append((tag, hid, has_background(a)))
        if self.skip_depth or self.hidden:
            return
        if re.match(r"^h[1-6]$", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.out.append("\x00cell\x00")
        elif tag in BLOCK:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)             # <div .../> opens a div in a browser; a void tag has no end

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag in VOID:
            return
        # pop to the matching tag (tolerate bad nesting)
        idx = max((i for i, (t, _, _) in enumerate(self.stack) if t == tag), default=None)
        if idx is None:
            return
        was_hidden = self.hidden
        closing = self.stack[idx:]
        del self.stack[idx:]
        if was_hidden and not self.hidden and any(h for _, h, _ in closing):
            self.doc.hide("HTML 隐藏元素 <%s>" % tag, "".join(self.hidden_buf))
            self.hidden_buf = []
            return
        if self.skip_depth or self.hidden:
            return
        if tag in ("td", "th"):
            text = "".join(self.out)
            start = text.rfind("\x00cell\x00")
            cell = text[start + 6:] if start >= 0 else ""
            self.out = [text[:start]] if start >= 0 else self.out
            if self.row is not None:
                self.row.append(" ".join(cell.split()).replace("|", "\\|"))
        elif tag == "tr" and self.row is not None:
            self.table_rows.append(self.row)
            self.row = None
        elif tag == "table" and self.table_rows:
            width = max(len(r) for r in self.table_rows)
            rows = [r + [""] * (width - len(r)) for r in self.table_rows]
            lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
            lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            self.out.append("\n\n" + "\n".join(lines) + "\n\n")
            self.table_rows = []
        elif re.match(r"^h[1-6]$", tag) or tag in BLOCK or tag == "li":
            self.out.append("\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        (self.hidden_buf if self.hidden else self.out).append(data)

    def handle_comment(self, data):
        self.doc.hide("HTML 注释", data)


def convert_html(path, name) -> Doc:
    with open(path, "rb") as f:
        text = decode_text(f.read())
    d = Doc(name, "html")
    p = _HTML(d)
    p.feed(text)
    p.close()
    if p.hidden_buf:
        d.hide("HTML 隐藏元素", "".join(p.hidden_buf))
    out = ZW_RE.sub("", "".join(p.out)).replace("\x00cell\x00", " ")
    out = re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t\u00a0]+", " ", out))
    d.text = re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"
    if p.scripts:
        d.notes.append("去掉了 %d 段脚本（不收）" % p.scripts)
    if "<style" in text.lower():
        d.notes.append("页面有样式表：用 CSS 类隐藏的内容可能没被识别为隐藏")
    return d


# ---------- Office (zip + XML) ----------

def _open_zip(path):
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise Refused("文件损坏，或不是 Office 文件")
    total = 0
    packed = 0
    for info in z.infolist():
        if info.flag_bits & 0x1:
            raise Refused("文件加密了，请另存为不加密的版本")
        if info.filename.lower().endswith("vbaproject.bin"):
            raise Refused("文件带宏，请另存为不带宏的版本")
        total += info.file_size
        packed += info.compress_size
    if total > MAX_UNZIPPED or (packed and total / max(packed, 1) > MAX_RATIO):
        raise Refused("解压后过大（疑似压缩炸弹）")
    return z


def _no_dtd(*_):
    raise Refused("文件里有 XML DOCTYPE 或实体声明，不收")


def _xml(z, name):
    try:
        data = z.read(name)
    except KeyError:
        return None
    # Pre-scan with expat itself (any encoding, BOM or declaration): refuse DOCTYPE and entity declarations.
    scan = expat.ParserCreate()
    scan.StartDoctypeDeclHandler = _no_dtd
    scan.EntityDeclHandler = _no_dtd
    scan.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        scan.Parse(data, True)
        return ET.fromstring(data)
    except (expat.ExpatError, ET.ParseError) as e:
        raise Refused("XML 损坏（%s）：%s" % (name, e))


NS = {
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "cp": "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
    "vt": "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}


def _q(prefix, tag):
    return "{%s}%s" % (NS[prefix], tag)


def _part_rels(z, part):
    """[(the relationship type's last word, target part)] of a package part, from its _rels/<name>.rels."""
    base, fname = posixpath.split(part)
    root = _xml(z, posixpath.join(base, "_rels", fname + ".rels"))
    out = []
    for rel in (root.findall(_q("pr", "Relationship")) if root is not None else []):
        t = rel.get("Target", "")
        if rel.get("TargetMode") != "External" and t:
            out.append((rel.get("Type", "").rsplit("/", 1)[-1],
                        t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join(base, t))))
    return out


def _md_rows(rows) -> str:
    """A Markdown table of rows of cell texts, the first row its header (Word and PowerPoint tables)."""
    width = max(len(r) for r in rows)
    rows = [[c.replace("|", "\\|").replace("\n", " ") for c in r] + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    return "\n".join(lines + ["| " + " | ".join(r) + " |" for r in rows[1:]])


def _custom_labels(z, doc):
    root = _xml(z, "docProps/custom.xml")
    if root is None:
        return
    for prop in root.findall(_q("cp", "property")):
        name = prop.get("name", "")
        if name.startswith("MSIP_Label_") and name.endswith("_Name"):
            val = "".join(prop.itertext()).strip()
            if val:
                doc.labels.append(val)


def _col_letters(n):
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _col_index(ref):
    m = re.match(r"([A-Z]+)", ref or "")
    n = 0
    for ch in (m.group(1) if m else ""):
        n = n * 26 + ord(ch) - 64
    return n


def head_and_tail(text, head=4000, tail=2000) -> str:
    """The part of a text gate 2 looks at for a classification marking (package.detect_class reads a document's
    first 4000 and last 2000 characters)."""
    return text if len(text) <= head + tail else text[:head] + "\n" + text[-tail:]


# the default indexed colours that matter here (a workbook's own <indexedColors> replaces them): black and white
XL_PALETTE = {0: "000000", 1: "FFFFFF", 8: "000000", 9: "FFFFFF", 64: "000000", 65: "FFFFFF"}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _xl_list(st, group, item):
    g = st.find(_q("s", group))
    return g.findall(_q("s", item)) if g is not None else []


def _xl_theme(z):
    """The theme colours ('RRGGBB') in the order <color theme="n"> counts them: lt1, dk1, lt2, dk2, accents ..."""
    root = _xml(z, "xl/theme/theme1.xml")
    scheme = root.find(".//" + _q("a", "clrScheme")) if root is not None else None
    if scheme is None:
        return ["FFFFFF", "000000", "E7E6E6", "44546A"]
    vals = []
    for el in scheme:                                # dk1 lt1 dk2 lt2 accent1..6 hlink folHlink
        c, sysc = el.find(_q("a", "srgbClr")), el.find(_q("a", "sysClr"))
        vals.append(c.get("val") if c is not None else sysc.get("lastClr") if sysc is not None else None)
    return [vals[k] if k < len(vals) else None for k in [1, 0, 3, 2] + list(range(4, len(vals)))]


def _xl_rgb(color, theme, palette):
    """'RRGGBB' of a SpreadsheetML <color> (rgb, theme or indexed, with its tint), or None (auto, unknown)."""
    if color is None:
        return None
    v = None
    if color.get("rgb"):
        v = color.get("rgb")[-6:]
    elif _int(color.get("theme")) is not None:
        k = _int(color.get("theme"))
        v = theme[k] if 0 <= k < len(theme) else None
    elif _int(color.get("indexed")) is not None:
        v = palette.get(_int(color.get("indexed")))
    if not v or not re.fullmatch(r"[0-9A-Fa-f]{6}", v):
        return None
    try:
        tint = max(-1.0, min(1.0, float(color.get("tint") or 0)))
    except ValueError:
        tint = 0.0
    if tint:
        h, l, sat = colorsys.rgb_to_hls(*(int(v[k:k + 2], 16) / 255 for k in (0, 2, 4)))
        l = l * (1 + tint) if tint < 0 else l * (1 - tint) + tint
        v = "".join("%02X" % round(x * 255) for x in colorsys.hls_to_rgb(h, l, sat))
    return v


def _near_white(rgb) -> bool:
    return bool(rgb) and min(int(rgb[k:k + 2], 16) for k in (0, 2, 4)) >= 0xF0


def _xl_font_unseen(font, theme, palette):
    """(white text, a font under 4pt) of a <font>."""
    sz = font.find(_q("s", "sz"))
    try:
        tiny = sz is not None and float(sz.get("val", "11")) < 4
    except ValueError:
        tiny = False
    return _near_white(_xl_rgb(font.find(_q("s", "color")), theme, palette)), tiny


def _xl_fill_paints(fill, theme, palette) -> bool:
    """The <fill> paints the cell in a colour that is not white (white text on it shows)."""
    pf = fill.find(_q("s", "patternFill"))
    if pf is None:
        return fill.find(_q("s", "gradientFill")) is not None
    kind = pf.get("patternType") or "none"
    if kind == "none":
        return False
    rgb = _xl_rgb(pf.find(_q("s", "fgColor")), theme, palette)
    return not _near_white(rgb) if rgb else kind == "solid"     # solid without a colour: automatic (black)


# codes in a sheet's header / footer text: &L &C &R sections, &P page, &D date, &"font,style", &12 size, &K color
XL_HF_CODE_RE = re.compile(r'&(?:&|"[^"]*"|K(?:[0-9A-Fa-f]{6}|[0-9]{2}[+-][0-9]{3})|[0-9]+|[A-Za-z+-])')


def _xl_comment_text(c, authors) -> str:
    """A cell note's text without its author: Excel starts a note with the author's name in bold ("张伟:")."""
    runs = c.findall("%s/%s" % (_q("s", "text"), _q("s", "r")))
    if runs and runs[0].find("%s/%s" % (_q("s", "rPr"), _q("s", "b"))) is not None and \
            "".join(t.text or "" for t in runs[0].iter(_q("s", "t"))).rstrip().endswith((":", "：")):
        runs[0].clear()
    text = "".join(t.text or "" for t in c.iter(_q("s", "t")))
    author = authors[_int(c.get("authorId"))] if _int(c.get("authorId")) is not None and \
        0 <= _int(c.get("authorId")) < len(authors) else ""
    if author and text.lstrip().startswith(author):
        text = re.sub(r"^\s*%s\s*[:：]?" % re.escape(author), "", text)
    return text


def convert_xlsx(path, name, max_cells=20000) -> Doc:
    d = Doc(name, "xlsx")
    z = _open_zip(path)
    _custom_labels(z, d)
    wb = _xml(z, "xl/workbook.xml")
    if wb is None:
        raise Refused("不是有效的 xlsx（没有 workbook.xml）")
    rels = {}
    rr = _xml(z, "xl/_rels/workbook.xml.rels")
    for rel in (rr.findall(_q("pr", "Relationship")) if rr is not None else []):
        t = rel.get("Target", "")
        rels[rel.get("Id")] = t.lstrip("/") if t.startswith("/") else "xl/" + t
    shared = []
    ss = _xml(z, "xl/sharedStrings.xml")
    for si in (ss.findall(_q("s", "si")) if ss is not None else []):
        shared.append("".join(t.text or "" for t in si.iter(_q("s", "t"))))
    hidden_fmt = set()
    faint = set()                                    # cell styles with white text and no fill, or a font under 4pt
    st = _xml(z, "xl/styles.xml")
    if st is not None:
        fmts = {}
        nf = st.find(_q("s", "numFmts"))
        for f in (nf.findall(_q("s", "numFmt")) if nf is not None else []):
            fmts[f.get("numFmtId")] = f.get("formatCode", "")
        theme = _xl_theme(z)
        palette = dict(XL_PALETTE)
        ix = st.find("%s/%s" % (_q("s", "colors"), _q("s", "indexedColors")))
        if ix is not None:
            palette = {k: (c.get("rgb") or "")[-6:] for k, c in enumerate(ix.findall(_q("s", "rgbColor")))}
        fonts = [_xl_font_unseen(f, theme, palette) for f in _xl_list(st, "fonts", "font")]
        fills = [_xl_fill_paints(f, theme, palette) for f in _xl_list(st, "fills", "fill")]
        for i, xf in enumerate(_xl_list(st, "cellXfs", "xf")):
            if re.fullmatch(r";;;?", fmts.get(xf.get("numFmtId"), "").strip()):
                hidden_fmt.add(str(i))
            font, fill = _int(xf.get("fontId")), _int(xf.get("fillId"))
            white, tiny = fonts[font] if font is not None and font < len(fonts) else (False, False)
            if tiny or (white and not (fill is not None and fill < len(fills) and fills[fill])):
                faint.add(str(i))
    parts = []
    cells = 0
    sheets = wb.find(_q("s", "sheets"))
    for sh in (sheets.findall(_q("s", "sheet")) if sheets is not None else []):
        sname = sh.get("name", "?")
        state = sh.get("state", "visible")
        target = rels.get(sh.get(_q("r", "id")), "")
        root = _xml(z, target) if target else None
        if root is None:
            d.notes.append("工作表「%s」的内容读不到（文件里没有这一页），没收" % sname)
            continue
        hf = root.find(_q("s", "headerFooter"))
        for part in (list(hf) if hf is not None else []):   # odd / even / first page header and footer
            text = " ".join(XL_HF_CODE_RE.sub(lambda m: "&" if m.group(0) == "&&" else " ", part.text or "").split())
            if text:
                d.labels.append(text[:200])          # gate 2 reads them, as a docx header or footer
        hidden_cols = set()
        cols = root.find(_q("s", "cols"))
        for c in (cols.findall(_q("s", "col")) if cols is not None else []):
            if c.get("hidden") in ("1", "true"):
                hidden_cols.update(range(int(c.get("min", "0")), int(c.get("max", "0")) + 1))
        grid = {}
        every, hidden = {}, {}                       # every cell of the sheet; the hidden ones (with why)
        values = []                                  # every cell of the sheet, hidden ones too, in order
        data = root.find(_q("s", "sheetData"))
        rnum = 0
        for row in (data.findall(_q("s", "row")) if data is not None else []):
            rnum = _int(row.get("r")) or rnum + 1     # r= is optional on rows and cells: then the next one
            row_hidden = row.get("hidden") in ("1", "true")
            ci = 0
            for c in row.findall(_q("s", "c")):
                cells += 1
                if cells > max_cells:
                    break
                ci = _col_index(c.get("r")) or ci + 1
                ref = "%s%d" % (_col_letters(ci), rnum)
                t = c.get("t", "n")
                v = c.find(_q("s", "v"))
                if t == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(_q("s", "t")))
                elif t == "b" and v is not None:
                    val = "TRUE" if v.text == "1" else "FALSE"
                else:
                    val = v.text if v is not None and v.text else ""
                if not val:
                    continue
                values.append(val)
                every[(rnum, ci)] = val
                if state != "visible":
                    hidden[(rnum, ci)] = "隐藏工作表"
                elif row_hidden or ci in hidden_cols:
                    hidden[(rnum, ci)] = "隐藏行/列"
                elif c.get("s", "0") in hidden_fmt:
                    hidden[(rnum, ci)] = "';;;' 格式隐藏"
                elif c.get("s", "0") in faint:
                    hidden[(rnum, ci)] = "白字或极小的字"
                else:
                    grid[(rnum, ci)] = val
        # each sheet is a document of its own for gate 2 (a hidden sheet marked 绝密, a 机密 sheet in the middle of a
        # long workbook): its head and tail, as gate 2 reads a document's
        d.labels.append(head_and_tail("%s\n%s" % (sname, "\n".join(values))))
        if hidden:
            # the hidden cells as a table under the sheet's header row, so gate 4 masks the names of a hidden 负责人
            # column (or sheet) as in a visible one, and they count toward the file's limit
            width = max(c for _, c in every)
            rows = [(r, [every.get((r, i), "") for i in range(1, width + 1)]) for r in sorted({r for r, _ in every})]
            head = rows[header_row(rows)]
            rows = [head] + [(r, [hidden.get((r, i)) and every[(r, i)] or "" for i in range(1, width + 1)])
                             for r, _ in rows if r != head[0] and any((r, i) in hidden for i in range(1, width + 1))]
            d.hide("%s（工作表 %s）" % ("、".join(sorted(set(hidden.values()))), sname), table_markdown(rows))
        for kind, part in _part_rels(z, target):
            cm = _xml(z, part) if kind == "comments" else None
            authors = [a.text or "" for a in cm.iter(_q("s", "author"))] if cm is not None else []
            for c in (cm.iter(_q("s", "comment")) if cm is not None else []):
                d.hide("Excel 批注 %s!%s" % (sname, c.get("ref", "")), _xl_comment_text(c, authors))
        if state != "visible":
            d.notes.append("工作表「%s」是隐藏的（%s），内容已移到隐藏区" % (sname, state))
            continue
        if not grid:
            parts.append("## 工作表：%s\n\n（空）\n" % sname)
            continue
        maxc = max(c for _, c in grid)
        rows = [(r, [grid.get((r, i), "") for i in range(1, maxc + 1)]) for r in sorted({r for r, _ in grid})]
        parts.append("## 工作表：%s\n\n%s\n" % (sname, table_markdown(rows)))
    if cells > max_cells:
        d.notes.append("单元格超过 %d 个，后面的没收" % max_cells)
    d.images = sum(1 for n in z.namelist() if n.startswith("xl/media/"))
    d.text = "\n".join(parts)
    return d


def _w_on(el) -> bool:
    """A Word on/off property is on: present, and no w:val="0" / "false" / "off"."""
    return el is not None and (el.get(_q("w", "val")) or "true").lower() not in ("0", "false", "off")


HEX6_RE = re.compile(r"[0-9A-Fa-f]{6}")


def _w_paints(shd):
    """What a w:shd paints behind the text: True a colour white text shows on, False white, None nothing (no
    shading: what is behind it shows). A solid pattern shows its pattern colour (auto: black), else the fill."""
    if shd is None or (shd.get(_q("w", "val")) or "").lower() == "nil":
        return None
    if (shd.get(_q("w", "val")) or "").lower() == "solid":
        colour = shd.get(_q("w", "color")) or "auto"
        return colour.lower() == "auto" or not (HEX6_RE.fullmatch(colour) and _near_white(colour))
    fill = shd.get(_q("w", "fill")) or ""
    if HEX6_RE.fullmatch(fill):
        return not _near_white(fill)
    theme = shd.get(_q("w", "themeFill"))
    return (theme not in ("background1", "light1")) if theme else None


def _w_white(color) -> bool:
    """A w:color is white: its theme colour is the light background one (not shaded darker), or, without a theme
    colour, its value is near white."""
    theme = color.get(_q("w", "themeColor"))
    if theme:
        shade = color.get(_q("w", "themeShade"))
        return theme in ("background1", "light1") and (not shade or not HEX6_RE.fullmatch(shade * 3)
                                                         or int(shade, 16) >= 0xF0)
    val = color.get(_q("w", "val")) or ""
    return bool(HEX6_RE.fullmatch(val)) and _near_white(val)


def _w_rpr(rpr) -> dict:
    """The look of a w:rPr, only what it sets: vanish (hidden), tiny (under 4pt), white (white text), paints (what
    the run's highlight, else its shading, paints behind it: see _w_paints)."""
    out = {}
    if rpr is None:
        return out
    for tag in ("vanish", "specVanish"):
        if rpr.find(_q("w", tag)) is not None:
            out["vanish"] = out.get("vanish", False) or _w_on(rpr.find(_q("w", tag)))
    sz = _int(_w_val(rpr, _q("w", "sz")))
    if sz is not None:
        out["tiny"] = sz < 8                             # half-points: under 4pt
    color = rpr.find(_q("w", "color"))
    if color is not None:
        out["white"] = _w_white(color)
    hl, shd = rpr.find(_q("w", "highlight")), rpr.find(_q("w", "shd"))
    if hl is not None or shd is not None:
        val = (hl.get(_q("w", "val")) or "none").lower() if hl is not None else "none"
        out["paints"] = (val != "white") if val != "none" else _w_paints(shd)
    return out


class _WStyles:
    """word/styles.xml: the run look and the paragraph / table shading of a style, through its basedOn chain; the
    document defaults and the default paragraph style apply to a paragraph that names no style."""

    def __init__(self, z):
        root = _xml(z, "word/styles.xml")
        found = list(root.iter(_q("w", "style"))) if root is not None else []
        self.by_id = {st.get(_q("w", "styleId")): st for st in found}
        self.default_para = next((st.get(_q("w", "styleId")) for st in found if st.get(_q("w", "type")) == "paragraph"
                                  and st.get(_q("w", "default")) in ("1", "true", "on")), None)
        dd = root.find("%s/%s/%s" % (_q("w", "docDefaults"), _q("w", "rPrDefault"), _q("w", "rPr"))) \
            if root is not None else None
        self.doc_defaults = _w_rpr(dd)

    def chain(self, sid):
        seen = []
        while sid in self.by_id and sid not in seen and len(seen) < 20:
            seen.append(sid)
            based = self.by_id[sid].find(_q("w", "basedOn"))
            sid = based.get(_q("w", "val")) if based is not None else None
        return [self.by_id[x] for x in reversed(seen)]       # the base first

    def rpr(self, sid) -> dict:
        out = {}
        for st in self.chain(sid):
            out.update(_w_rpr(st.find(_q("w", "rPr"))))
        return out

    def para_rpr(self, sid) -> dict:
        """The run look a paragraph gives its runs: document defaults, then its style (or the default one)."""
        out = dict(self.doc_defaults)
        out.update(self.rpr(sid or self.default_para))
        return out

    def paints(self, sid, path):
        """What the last style in the chain that sets the shading at this path paints (see _w_paints)."""
        res = None
        for st in self.chain(sid):
            shd = st.find(path)
            if shd is not None:
                res = _w_paints(shd)
        return res

    def first_row_paints(self, sid):
        res = None
        for st in self.chain(sid):
            for cond in st.findall(_q("w", "tblStylePr")):
                shd = cond.find("%s/%s" % (_q("w", "tcPr"), _q("w", "shd")))
                if cond.get(_q("w", "type")) == "firstRow" and shd is not None:
                    res = _w_paints(shd)
        return res


def _first_set(*layers):
    """The first layer (nearest first) that paints something, white or not; None when none does."""
    return next((x for x in layers if x is not None), None)


def _w_first_row_on(tpr) -> bool:
    """The table turns on its style's header-row look (w:firstRow, or bit 0x20 of an older w:val; on when unset)."""
    look = tpr.find(_q("w", "tblLook")) if tpr is not None else None
    if look is None:
        return True
    if look.get(_q("w", "firstRow")) is not None:
        return look.get(_q("w", "firstRow")).lower() in ("1", "true", "on")
    try:
        return bool(int(look.get(_q("w", "val")) or "20", 16) & 0x20)
    except ValueError:
        return True


def _w_val(el, path):
    x = el.find(path)
    return x.get(_q("w", "val")) if x is not None else None


def _w_paragraph(p, d, where, styles, behind=None):
    """The visible text of a paragraph (hidden runs go to d.hidden). A run is hidden when it vanishes, is under 4pt,
    or is white with nothing coloured behind it. What is behind it is the nearest layer that paints something: the
    run's highlight or shading, the paragraph's shading (direct, else its style's), then `behind` (its cell, then
    table); the white page when none does. White text on a dark header fill is a header, not hidden text."""
    shd_path = "%s/%s" % (_q("w", "pPr"), _q("w", "shd"))
    sv = _w_val(p, "%s/%s" % (_q("w", "pPr"), _q("w", "pStyle"))) or ""
    pstyle = styles.para_rpr(sv)
    own = p.find(shd_path)
    para = _w_paints(own) if own is not None else styles.paints(sv or styles.default_para, shd_path)
    vis, hid = [], []
    for r in p.iter(_q("w", "r")):
        rpr = r.find(_q("w", "rPr"))
        look = dict(pstyle)
        rs = _w_val(rpr, _q("w", "rStyle")) if rpr is not None else None
        if rs:
            look.update(styles.rpr(rs))
        look.update(_w_rpr(rpr))
        painted = _first_set(look.get("paints"), para, behind)
        unseen = look.get("vanish") or look.get("tiny") or (look.get("white") and not painted)
        target = hid if unseen else vis
        for node in r:
            if node.tag == _q("w", "t"):
                target.append(node.text or "")
            elif node.tag == _q("w", "tab"):
                target.append("\t")
            elif node.tag in (_q("w", "br"), _q("w", "cr")):
                target.append("\n")
            elif node.tag in (_q("w", "drawing"), _q("w", "pict")):
                d.images += 1
    if hid:
        d.hide(where, "".join(hid))
    text = "".join(vis).strip()
    m = re.match(r"(?i)^(heading|标题|標題)\s*([1-6])$", sv.replace(" ", "")) or re.match(r"(?i)^heading([1-6])$", sv)
    if text and m:
        level = int(m.groups()[-1])
        return "#" * level + " " + text
    if text and sv.lower() == "title":
        return "# " + text
    if text and re.match(r"(?i)^list", sv):
        return "- " + text
    return text


def _w_items(parent, tags):
    """The children of a body, table, row or cell with one of these tags, in order, also inside content controls
    (w:sdt: a cover page, a form field, a template block) and custom XML: Word shows what is in them."""
    for el in (list(parent) if parent is not None else []):
        if el.tag in tags:
            yield el
        elif el.tag == _q("w", "sdt"):
            yield from _w_items(el.find(_q("w", "sdtContent")), tags)
        elif el.tag == _q("w", "customXml"):
            yield from _w_items(el, tags)


def convert_docx(path, name) -> Doc:
    d = Doc(name, "docx")
    z = _open_zip(path)
    _custom_labels(z, d)
    root = _xml(z, "word/document.xml")
    if root is None:
        raise Refused("不是有效的 docx（没有 document.xml）")
    body = root.find(_q("w", "body"))
    styles = _WStyles(z)
    out = []
    for i, el in enumerate(_w_items(body, (_q("w", "p"), _q("w", "tbl")))):
        if el.tag == _q("w", "p"):
            t = _w_paragraph(el, d, "Word 隐藏文字（第 %d 段）" % (i + 1), styles)
            if t:
                out.append(t)
        else:
            rows = []
            tpr = el.find(_q("w", "tblPr"))
            ts = _w_val(tpr, _q("w", "tblStyle")) if tpr is not None else None
            tc_shd, tbl_shd = "%s/%s" % (_q("w", "tcPr"), _q("w", "shd")), "%s/%s" % (_q("w", "tblPr"), _q("w", "shd"))
            # behind a cell, nearest first: its own shading, the style's header-row shading (first row), the style's
            # cell shading, the table's shading, the style's table shading
            style_cell = styles.paints(ts, tc_shd) if ts else None
            table = _first_set(_w_paints(tpr.find(_q("w", "shd"))) if tpr is not None else None,
                               styles.paints(ts, tbl_shd) if ts else None)
            head = styles.first_row_paints(ts) if ts and _w_first_row_on(tpr) else None
            for k, tr in enumerate(_w_items(el, (_q("w", "tr"),))):
                cells = []
                for tc in _w_items(tr, (_q("w", "tc"),)):
                    behind = _first_set(_w_paints(tc.find(tc_shd)), head if k == 0 else None, style_cell, table)
                    ps = [_w_paragraph(p, d, "Word 表格隐藏文字", styles, behind) for p in tc.iter(_q("w", "p"))]
                    cells.append(" ".join(x for x in ps if x))
                rows.append(cells)
            if rows:
                out.append(_md_rows(rows))
    chunks = len(list(body.iter(_q("w", "altChunk")))) if body is not None else 0
    if chunks:
        d.notes.append("有 %d 处嵌入的文档块（altChunk）：里面的内容没收，请另存为普通 docx 再交" % chunks)
    for n in sorted(z.namelist()):
        if re.match(r"word/(header|footer)\d*\.xml$", n):
            hf = _xml(z, n)
            text = " ".join(t.text or "" for t in hf.iter(_q("w", "t"))).strip() if hf is not None else ""
            if text:
                d.labels.append(text[:200])
    if "word/comments.xml" in z.namelist():
        cm = _xml(z, "word/comments.xml")
        if cm is not None:
            d.hide("Word 批注", "\n".join(" ".join(t.text or "" for t in c.iter(_q("w", "t")))
                                          for c in cm.iter(_q("w", "comment"))))
    d.text = ZW_RE.sub("", "\n\n".join(out)) + "\n"
    return d


def _p_shapes(tree, hidden=False):
    """(hidden, shape) for each shape of a slide's shape tree, in order, groups opened: what is in a hidden group is
    hidden. Of an mc:AlternateContent, the first choice is read."""
    for el in (list(tree) if tree is not None else []):
        if el.tag == _q("mc", "AlternateContent"):
            pick = el.find(_q("mc", "Choice"))
            yield from _p_shapes(pick if pick is not None else el.find(_q("mc", "Fallback")), hidden)
            continue
        nv = next((x for x in el if x.tag.startswith("{%s}nv" % NS["p"])), None)
        cnv = nv.find(_q("p", "cNvPr")) if nv is not None else None
        h = hidden or (cnv is not None and cnv.get("hidden") in ("1", "true"))
        if el.tag == _q("p", "grpSp"):
            yield from _p_shapes(el, h)
        elif el.tag in (_q("p", "sp"), _q("p", "graphicFrame")):
            yield h, el


NOTES_SKIP = ("sldNum", "sldImg", "dt", "hdr", "ftr")   # a notes page's slide picture, number, date, header, footer


def _a_text(p) -> str:
    return "".join(x.text or "" for x in p.iter(_q("a", "t")))


def _p_placeholder(sp, kinds) -> bool:
    ph = sp.find("%s/%s/%s" % (_q("p", "nvSpPr"), _q("p", "nvPr"), _q("p", "ph")))
    return ph is not None and ph.get("type") in kinds


def convert_pptx(path, name) -> Doc:
    d = Doc(name, "pptx")
    z = _open_zip(path)
    _custom_labels(z, d)
    slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                    key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
    out = []
    unread = []                                      # slides with a chart, SmartArt or embedded object
    for n in slides:
        root = _xml(z, n)
        num = re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)
        d.images += len(list(root.iter(_q("p", "pic"))))
        visible = []
        for hidden, sp in _p_shapes(root.find("%s/%s" % (_q("p", "cSld"), _q("p", "spTree")))):
            if sp.tag == _q("p", "graphicFrame"):
                tbl = sp.find(".//" + _q("a", "tbl"))
                if tbl is None:
                    unread.append(num)
                    continue
                rows = [[" ".join(_a_text(p) for p in tc.iter(_q("a", "p"))).strip()
                         for tc in tr.findall(_q("a", "tc"))] for tr in tbl.findall(_q("a", "tr"))]
                t = "\n%s\n" % _md_rows(rows) if any(any(r) for r in rows) else ""
            else:
                t = "\n".join(x.text for x in sp.iter(_q("a", "t")) if x.text and x.text.strip())
            if hidden:
                d.hide("隐藏形状（幻灯片 %s）" % num, t)
            elif t:
                visible.append(t)
        text = "\n".join(visible).strip("\n")
        for kind, part in _part_rels(z, n):
            notes = _xml(z, part) if kind == "notesSlide" else None
            if notes is not None:                    # speaker notes: not on the slide
                d.hide("演讲者备注（幻灯片 %s）" % num, "\n".join(
                    _a_text(p) for sp in notes.iter(_q("p", "sp")) if not _p_placeholder(sp, NOTES_SKIP)
                    for p in sp.iter(_q("a", "p"))))
        if root.get("show") in ("0", "false"):
            d.hide("隐藏幻灯片 %s" % num, text)
            continue
        out.append("## 幻灯片 %s\n\n%s" % (num, text or "（无文字）"))
    if unread:
        d.notes.append("图表、SmartArt 或嵌入对象 %d 个（幻灯片 %s）：里面的文字没收，涉及的需求要人确认"
                       % (len(unread), "、".join(sorted(set(unread), key=int))))
    d.notes.append("PPT 只收文字；页面外或透明的形状没做检查")
    d.text = ZW_RE.sub("", "\n\n".join(out)) + "\n"
    return d


def convert_pdf(path, name) -> Doc:
    tool = shutil.which("markitdown")
    if not tool:
        raise Refused("PDF 需要 MarkItDown（没装），或者请另存为 docx 再交")
    p = subprocess.run([tool, path], capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise Refused("PDF 转换失败：%s" % (p.stderr or "").strip()[:200])
    text = ZW_RE.sub("", p.stdout or "")
    if len(text.strip()) < 50:
        raise Refused("PDF 几乎没有文字（可能是扫描件），需要 OCR，请交文字版")
    d = Doc(name, "pdf")
    d.text = text
    d.notes.append("PDF 没做隐藏文字和图片检查")
    return d


def convert(path, name=None) -> Doc:
    try:
        return _convert(path, name)
    except Refused:
        raise
    except (ET.ParseError, ValueError, KeyError, IndexError, zipfile.BadZipFile, UnicodeDecodeError, OSError,
            RuntimeError, EOFError, OverflowError, zlib.error, lzma.LZMAError, struct.error) as e:
        raise Refused("文件内容损坏或格式不对：%s" % (str(e)[:200] or type(e).__name__))


def _convert(path, name=None) -> Doc:
    name = name or os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if ext in REFUSED_EXT:
        raise Refused("不收 %s 格式：请另存为 docx / xlsx / pptx / pdf / md 再交" % ext)
    kind = sniff(path)
    if ext in IMAGE_EXT:
        raise Refused("图片不单独收（没有 OCR）：请把图里的文字写进文档")
    if kind == "ole":
        raise Refused("老格式或加密的 Office 文件：请另存为 docx / xlsx / pptx")
    if ext == ".xlsx":
        if kind != "zip":
            raise Refused("扩展名是 .xlsx，但内容不是")
        return convert_xlsx(path, name)
    if ext == ".docx":
        if kind != "zip":
            raise Refused("扩展名是 .docx，但内容不是")
        return convert_docx(path, name)
    if ext == ".pptx":
        if kind != "zip":
            raise Refused("扩展名是 .pptx，但内容不是")
        return convert_pptx(path, name)
    if ext == ".pdf":
        if kind != "pdf":
            raise Refused("扩展名是 .pdf，但内容不是")
        return convert_pdf(path, name)
    if kind != "text":
        raise Refused("看起来不是文本文件（%s）" % kind)
    if ext in HTML_EXT:
        return convert_html(path, name)
    if ext in TEXT_EXT or ext == "":
        return convert_text(path, name)
    raise Refused("不认识的格式 %s" % (ext or "（无扩展名）"))
