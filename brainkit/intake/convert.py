"""File -> Markdown text, with hidden content moved aside. Stdlib only (zip + XML for Office files).

Each converter returns a Doc. Hidden content (HTML comments, display:none, Word hidden/white/tiny text,
hidden sheets/rows/columns, ';;;' cells, hidden slides) goes to Doc.hidden, never into Doc.text.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET
from xml.parsers import expat

ZW_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u202a-\u202e\u2066-\u2069]")
HTML_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.S)
TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)\b([^>]*)>")
HIDDEN_ATTR_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.0-9]*[1-9])"
                            r"|opacity\s*:\s*0(?![.0-9]*[1-9])|color\s*:\s*(?:#fff\b|#ffffff\b|white\b)|\bhidden\b", re.I)


def strip_hidden_html(text):
    """Remove inline HTML elements that render invisibly (nesting-aware). Returns (text, [hidden strings])."""
    out, hidden = [], []
    pos = 0
    tokens = list(TAG_RE.finditer(text))
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.group(1) == "" and HIDDEN_ATTR_RE.search(t.group(3) or "") and not t.group(3).rstrip().endswith("/"):
            name = t.group(2).lower()
            depth = 1
            j = i + 1
            while j < len(tokens) and depth:
                u = tokens[j]
                if u.group(2).lower() == name:
                    depth += -1 if u.group(1) == "/" else (0 if (u.group(3) or "").rstrip().endswith("/") else 1)
                j += 1
            end = tokens[j - 1].end() if depth == 0 else len(text)
            out.append(text[pos:t.start()])
            hidden.append(TAG_RE.sub("", text[t.end():tokens[j - 1].start() if depth == 0 else len(text)]))
            pos = end
            i = j
            continue
        i += 1
    out.append(text[pos:])
    return "".join(out), hidden
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
    for enc in ("gb18030", "big5"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    raise Refused("文本编码无法识别（不是 UTF-8 / GB18030 / Big5）")


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

def convert_text(path, name) -> Doc:
    ext = os.path.splitext(name)[1].lower()
    with open(path, "rb") as f:
        text = decode_text(f.read()).replace("\r\n", "\n").replace("\r", "\n")
    d = Doc(name, "markdown" if ext in (".md", ".markdown") else "text")
    for m in HTML_COMMENT_RE.finditer(text):
        d.hide("HTML 注释", m.group(1))
    text = HTML_COMMENT_RE.sub("", text)
    text, hid = strip_hidden_html(text)
    for h in hid:
        d.hide("隐藏样式的文字", h)
    if ZW_RE.search(text):
        d.notes.append("去掉了零宽或方向控制字符 %d 个" % len(ZW_RE.findall(text)))
        text = ZW_RE.sub("", text)
    d.images = len(MD_IMAGE_RE.findall(text))
    if d.kind == "text":
        fence = "````" if "```" in text else "```"
        text = "%stext\n%s\n%s" % (fence, text.strip("\n"), fence)
    d.text = text
    return d


# ---------- HTML ----------

HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|pt|em)?\s*(?:;|$)"
                             r"|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)", re.I)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "nav", "aside", "ul", "ol", "dl", "dt", "dd",
         "form", "fieldset", "blockquote", "pre", "figure", "figcaption", "label", "button"}


class _HTML(HTMLParser):
    def __init__(self, doc):
        super().__init__(convert_charrefs=True)
        self.doc = doc
        self.out = []
        self.stack = []          # (tag, hidden?)
        self.hidden_buf = []
        self.skip_depth = 0      # inside script/style/template
        self.row = None
        self.table_rows = []
        self.scripts = 0

    @property
    def hidden(self):
        return any(h for _, h in self.stack)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "template"):
            self.skip_depth += 1
            self.scripts += tag == "script"
            return
        if tag == "img":
            self.doc.images += 1
            if not self.hidden and a.get("alt"):
                self.out.append(" [图片: %s] " % a.get("alt"))
            return
        if tag in VOID:
            if tag == "br" and not self.skip_depth:
                (self.hidden_buf if self.hidden else self.out).append("\n")
            return
        hid = "hidden" in a or bool(HIDDEN_STYLE_RE.search(a.get("style") or "")) or a.get("type") == "hidden"
        self.stack.append((tag, hid))
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

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag in VOID:
            return
        # pop to the matching tag (tolerate bad nesting)
        idx = max((i for i, (t, _) in enumerate(self.stack) if t == tag), default=None)
        if idx is None:
            return
        was_hidden = self.hidden
        closing = self.stack[idx:]
        del self.stack[idx:]
        if was_hidden and not self.hidden and any(h for _, h in closing):
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
}


def _q(prefix, tag):
    return "{%s}%s" % (NS[prefix], tag)


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


def _col_index(ref):
    m = re.match(r"([A-Z]+)", ref or "")
    n = 0
    for ch in (m.group(1) if m else ""):
        n = n * 26 + ord(ch) - 64
    return n


def _col_name(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


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
    st = _xml(z, "xl/styles.xml")
    if st is not None:
        fmts = {}
        nf = st.find(_q("s", "numFmts"))
        for f in (nf.findall(_q("s", "numFmt")) if nf is not None else []):
            fmts[f.get("numFmtId")] = f.get("formatCode", "")
        xfs = st.find(_q("s", "cellXfs"))
        for i, xf in enumerate(xfs.findall(_q("s", "xf")) if xfs is not None else []):
            if re.fullmatch(r";;;?", fmts.get(xf.get("numFmtId"), "").strip()):
                hidden_fmt.add(str(i))
    parts = []
    cells = 0
    sheets = wb.find(_q("s", "sheets"))
    for sh in (sheets.findall(_q("s", "sheet")) if sheets is not None else []):
        sname = sh.get("name", "?")
        state = sh.get("state", "visible")
        target = rels.get(sh.get(_q("r", "id")), "")
        root = _xml(z, target) if target else None
        if root is None:
            continue
        hidden_cols = set()
        cols = root.find(_q("s", "cols"))
        for c in (cols.findall(_q("s", "col")) if cols is not None else []):
            if c.get("hidden") in ("1", "true"):
                hidden_cols.update(range(int(c.get("min", "0")), int(c.get("max", "0")) + 1))
        grid = {}
        maxc = 0
        data = root.find(_q("s", "sheetData"))
        for row in (data.findall(_q("s", "row")) if data is not None else []):
            rnum = int(row.get("r", "0") or 0)
            row_hidden = row.get("hidden") in ("1", "true")
            for c in row.findall(_q("s", "c")):
                cells += 1
                if cells > max_cells:
                    break
                ref = c.get("r", "")
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
                ci = _col_index(ref)
                where = "%s!%s" % (sname, ref)
                if state != "visible":
                    d.hide("隐藏工作表 %s" % where, val)
                elif row_hidden or ci in hidden_cols:
                    d.hide("隐藏行/列 %s" % where, val)
                elif c.get("s") in hidden_fmt:
                    d.hide("';;;' 格式隐藏 %s" % where, val)
                else:
                    grid[(rnum, ci)] = val
                    maxc = max(maxc, ci)
        if state != "visible":
            d.notes.append("工作表「%s」是隐藏的（%s），内容已移到隐藏区" % (sname, state))
            continue
        if not grid:
            parts.append("## 工作表：%s\n\n（空）\n" % sname)
            continue
        rows = sorted({r for r, _ in grid})
        header = "| 行 | " + " | ".join(_col_name(i) for i in range(1, maxc + 1)) + " |"
        lines = [header, "|" + "---|" * (maxc + 1)]
        for r in rows:
            vals = [" ".join(grid.get((r, i), "").split()).replace("|", "\\|") for i in range(1, maxc + 1)]
            lines.append("| %d | %s |" % (r, " | ".join(vals)))
        parts.append("## 工作表：%s\n\n%s\n" % (sname, "\n".join(lines)))
    if cells > max_cells:
        d.notes.append("单元格超过 %d 个，后面的没收" % max_cells)
    d.images = sum(1 for n in z.namelist() if n.startswith("xl/media/"))
    d.text = "\n".join(parts)
    return d


def _w_hidden(rpr):
    if rpr is None:
        return False
    if rpr.find(_q("w", "vanish")) is not None or rpr.find(_q("w", "specVanish")) is not None:
        return True
    color = rpr.find(_q("w", "color"))
    if color is not None and (color.get(_q("w", "val")) or "").upper() in ("FFFFFF", "FFFFFE"):
        return True
    sz = rpr.find(_q("w", "sz"))
    try:
        if sz is not None and int(sz.get(_q("w", "val"), "24")) < 8:  # half-points: < 4pt
            return True
    except ValueError:
        pass
    return False


def _w_hidden_styles(z):
    root = _xml(z, "word/styles.xml")
    out = set()
    for st in (root.iter(_q("w", "style")) if root is not None else []):
        if _w_hidden(st.find(_q("w", "rPr"))):
            out.add(st.get(_q("w", "styleId")))
    return out


def _w_paragraph(p, d, where, hidden_styles=frozenset()):
    style = p.find("%s/%s" % (_q("w", "pPr"), _q("w", "pStyle")))
    sv = (style.get(_q("w", "val")) if style is not None else "") or ""
    para_hidden = sv in hidden_styles  # paragraph style that hides its text
    vis, hid = [], []
    for r in p.iter(_q("w", "r")):
        rpr = r.find(_q("w", "rPr"))
        rstyle = rpr.find(_q("w", "rStyle")) if rpr is not None else None
        styled = rstyle is not None and rstyle.get(_q("w", "val")) in hidden_styles
        target = hid if (para_hidden or styled or _w_hidden(rpr)) else vis
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


def convert_docx(path, name) -> Doc:
    d = Doc(name, "docx")
    z = _open_zip(path)
    _custom_labels(z, d)
    root = _xml(z, "word/document.xml")
    if root is None:
        raise Refused("不是有效的 docx（没有 document.xml）")
    body = root.find(_q("w", "body"))
    hs = _w_hidden_styles(z)
    out = []
    for i, el in enumerate(list(body) if body is not None else []):
        if el.tag == _q("w", "p"):
            t = _w_paragraph(el, d, "Word 隐藏文字（第 %d 段）" % (i + 1), hs)
            if t:
                out.append(t)
        elif el.tag == _q("w", "tbl"):
            rows = []
            for tr in el.iter(_q("w", "tr")):
                cells = []
                for tc in tr.findall(_q("w", "tc")):
                    ps = [_w_paragraph(p, d, "Word 表格隐藏文字", hs) for p in tc.iter(_q("w", "p"))]
                    cells.append(" ".join(x for x in ps if x).replace("|", "\\|").replace("\n", " "))
                rows.append(cells)
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
                lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                out.append("\n".join(lines))
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


def convert_pptx(path, name) -> Doc:
    d = Doc(name, "pptx")
    z = _open_zip(path)
    _custom_labels(z, d)
    slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                    key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
    out = []
    for n in slides:
        root = _xml(z, n)
        num = re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)
        d.images += len(list(root.iter(_q("p", "pic"))))
        visible = []
        for sp in root.iter(_q("p", "sp")):
            nv = sp.find("%s/%s" % (_q("p", "nvSpPr"), _q("p", "cNvPr")))
            t = "\n".join(x.text for x in sp.iter(_q("a", "t")) if x.text and x.text.strip())
            if nv is not None and nv.get("hidden") in ("1", "true"):
                d.hide("隐藏形状（幻灯片 %s）" % num, t)
            elif t:
                visible.append(t)
        text = "\n".join(visible)
        if root.get("show") == "0":
            d.hide("隐藏幻灯片 %s" % num, text)
            continue
        out.append("## 幻灯片 %s\n\n%s" % (num, text or "（无文字）"))
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
            RuntimeError, EOFError) as e:
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
