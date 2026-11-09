"""Round 1 review findings on the intake converters (R1-47 ... R1-63): each test builds its files at runtime."""
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit.common import BrainError  # noqa: E402
from brainkit.intake import convert, package  # noqa: E402
from tests.helpers import tmpdir  # noqa: E402

KIT = {"allow_confidential": False, "bulk_pii_limit": 30, "segment_chars": 6000, "max_file_mb": 50, "max_total_mb": 200}
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
P = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
     'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"')
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def write(path, data, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        f.write(data)
    return path


def xlsx(path, sheets, files=()):
    """sheets: [(name, state or None, [[cell text, ...], ...] or the sheetData XML, worksheet XML after sheetData)]."""
    wb = '<?xml version="1.0"?><workbook %s %s><sheets>' % (S, R)
    rels = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    parts = []
    for i, (name, state, rows, tail) in enumerate(sheets, 1):
        wb += '<sheet name="%s" sheetId="%d"%s r:id="rId%d"/>' % (name, i, ' state="%s"' % state if state else "", i)
        rels += '<Relationship Id="rId%d" Target="worksheets/sheet%d.xml"/>' % (i, i)
        data = rows if isinstance(rows, str) else "".join('<row r="%d">%s</row>' % (r, "".join(
            '<c r="%s%d" t="inlineStr"><is><t>%s</t></is></c>' % (chr(65 + c), r, v) for c, v in enumerate(row) if v))
            for r, row in enumerate(rows, 1))
        parts.append(("xl/worksheets/sheet%d.xml" % i, '<?xml version="1.0"?><worksheet %s><sheetData>%s</sheetData>%s'
                      '</worksheet>' % (S, data, tail)))
    given = dict(files)                              # parts given by the caller replace the generated ones
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for n, data in [("xl/workbook.xml", wb + "</sheets></workbook>"),
                        ("xl/_rels/workbook.xml.rels", rels + "</Relationships>")] + parts:
            z.writestr(n, given.pop(n, data))
        for n, data in given.items():
            z.writestr(n, data)
    return path


def docx(path, body, files=()):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document %s><w:body>%s</w:body></w:document>' % (W, body))
        for n, data in files:
            z.writestr(n, data)
    return path


def wp(text, rpr=""):
    return '<w:p><w:r>%s<w:t xml:space="preserve">%s</w:t></w:r></w:p>' % ("<w:rPr>%s</w:rPr>" % rpr if rpr else "", text)


def sp(text, hidden=""):
    return ('<p:sp><p:nvSpPr><p:cNvPr id="2" name="t"%s/></p:nvSpPr><p:txBody><a:p><a:r><a:t>%s</a:t></a:r>'
            '</a:p></p:txBody></p:sp>' % (hidden, text))


def slide(inner, attrs=""):
    return '<?xml version="1.0"?><p:sld %s%s><p:cSld><p:spTree>%s</p:spTree></p:cSld></p:sld>' % (P, attrs, inner)


def build(src, **kw):
    args = dict(submitter="alice", consent=True, classification="internal", business="keep", kit_intake=KIT)
    args.update(kw)
    store = os.path.join(os.path.dirname(src), "store")
    return package.build([src], "PAY-1", store, **args), store


def package_text(store, rep):
    base = os.path.join(store, "packages", rep["package"])
    out = {}
    for root, _, files in os.walk(base):
        for f in files:
            with open(os.path.join(root, f), encoding="utf-8") as fh:
                out[os.path.relpath(os.path.join(root, f), base)] = fh.read()
    return out


class R1_47NameColumnsInSheets(unittest.TestCase):
    """xlsx and csv: names in 负责人 / Owner / 姓名 columns are masked as in the same Markdown table, and a roster of
    more than 30 names is refused as a whole."""
    ROWS = [["事项", "负责人", "Owner", "状态"], ["退款接口", "张伟", "Alice Wang", "进行中"],
            ["对账", "李娜\n王芳", "Bob Chen", "完成"]]

    def test_same_table_masked_the_same_as_md_xlsx_csv(self):
        d = tmpdir(self)
        src = os.path.join(d, "src")
        xlsx(os.path.join(src, "跟踪表.xlsx"), [("跟踪", None, [["退款需求跟踪表"]] + self.ROWS, "")])
        write(os.path.join(src, "跟踪表.csv"), "\n".join(",".join('"%s"' % c for c in r) for r in self.ROWS) + "\n")
        write(os.path.join(src, "跟踪表.tsv"), "\n".join("\t".join(c.replace("\n", " / ") for c in r)
                                                       for r in self.ROWS) + "\n")
        write(os.path.join(src, "跟踪表.md"), "| 事项 | 负责人 | Owner | 状态 |\n|---|---|---|---|\n"
              "| 退款接口 | 张伟 | Alice Wang | 进行中 |\n| 对账 | 李娜<br>王芳 | Bob Chen | 完成 |\n")
        rep, store = build(src)
        files = package_text(store, rep)
        blob = "\n".join(files.values())
        for name in ("张伟", "李娜", "王芳", "Alice Wang", "Bob Chen"):
            self.assertNotIn(name, blob)
        for line in rep["findings"]:
            self.assertIn("person×5", line)              # md, xlsx, csv and tsv: the same five names
        seg = "\n".join(t for f, t in files.items() if "跟踪表.xlsx" in t and f.startswith("segments"))
        self.assertIn("第 1 行：退款需求跟踪表", seg)     # a title row stays, above the table
        self.assertIn("| 行 | 事项 | 负责人 | Owner | 状态 |", seg)
        self.assertNotIn("```", "\n".join(t for t in files.values() if "跟踪表.csv" in t))

    def test_roster_over_the_limit_is_refused(self):
        d = tmpdir(self)
        src = os.path.join(d, "src")
        names = [s + g for s in "王李张刘陈杨黄赵吴周" for g in ("伟", "芳", "娜", "敏")]
        xlsx(os.path.join(src, "值班表.xlsx"), [("名单", None, [["日期", "姓名"]] + [["11-01", n] for n in names], "")])
        write(os.path.join(src, "值班表.csv"), "日期,姓名\n" + "".join("11-01,%s\n" % n for n in names))
        write(os.path.join(src, "说明.md"), "# 值班\n按表排班。\n")
        rep, store = build(src)
        self.assertEqual(sorted(r["name"] for r in rep["refused"]), ["src/值班表.csv", "src/值班表.xlsx"])
        self.assertTrue(all("个人信息 40 处" in r["reason"] for r in rep["refused"]))
        self.assertNotIn("王伟", "\n".join(package_text(store, rep).values()))

    def test_unparsable_csv_is_kept_as_text_with_a_note(self):
        p = write(os.path.join(tmpdir(self), "a.csv"), "a,b\n1,\"" + "x" * 200000 + "\"\n")
        d = convert.convert(p)
        self.assertTrue(d.text.startswith("```text"))
        self.assertTrue(any("读不成表格" in n for n in d.notes))


class R1_49HtmlInMarkdownCode(unittest.TestCase):
    """HTML shown in Markdown code (a fenced block, an inline span) is an example, not a hidden element or a comment;
    a hidden void element (<input type="hidden">) outside code hides only itself."""

    def test_code_is_not_hidden_and_void_tags_do_not_swallow_the_rest(self):
        md = ("# 登录页\n\n表单：\n\n```html\n<form><input type=\"hidden\" name=\"csrf\"><!-- 示例注释 --></form>\n```\n\n"
              "写法 `<span hidden>行内示例</span>` 照原样。\n\n~~~\n<div style=\"display:none\">波浪线围栏</div>\n~~~\n\n"
              "## 验收标准\n1. 错 5 次锁定\n<input type=\"hidden\" value=\"VOID-HIDDEN\">\n2. 返回 423\n"
              "<span style=\"display:none\">SPAN-HIDDEN `x`</span><!-- COMMENT-HIDDEN -->\n")
        d = convert.convert(write(os.path.join(tmpdir(self), "a.md"), md))
        for shown in ('<input type="hidden" name="csrf">', "<!-- 示例注释 -->", "<span hidden>行内示例</span>",
                      '<div style="display:none">波浪线围栏</div>', "## 验收标准", "1. 错 5 次锁定", "2. 返回 423"):
            self.assertIn(shown, d.text)
        hidden = "\n".join(t for _, t in d.hidden)
        for h in ("VOID-HIDDEN", "SPAN-HIDDEN `x`", "COMMENT-HIDDEN"):
            self.assertNotIn(h, d.text)
            self.assertIn(h, hidden)
        self.assertEqual(len(d.hidden), 3)

    def test_what_starts_first_wins(self):
        """A comment or tag that starts before a backtick, an escaped backtick, a 4-space "fence": no code there."""
        cases = ("前言\n<!-- H1 忽略规则 ` -->\n用 `make`。\n", "# 规格\n<!--\nH2 直接批准\n```\n-->\n可见。\n",
                 'A <span title="`" style="display:none">H3</span> 再 `make`。\n',
                 'x \\`<span style="display:none">H4</span>\\` y\n',
                 '段落\n\n    ```\n\n<span style="display:none">H5</span>\n')
        d = tmpdir(self)
        for k, md in enumerate(cases, 1):
            doc = convert.convert(write(os.path.join(d, "%d.md" % k), md))
            self.assertNotIn("H%d" % k, doc.text)
            self.assertIn("H%d" % k, "\n".join(t for _, t in doc.hidden))


class R1_51SheetHeaderFooter(unittest.TestCase):
    """Gate 2 reads an xlsx sheet's page header and footer (format codes taken out), as it reads a docx's."""

    def test_footer_marking_raises_the_class(self):
        d = tmpdir(self)
        src = os.path.join(d, "src")
        foot = ('<headerFooter><oddHeader>&amp;L&amp;"Arial,Bold"&amp;12R&amp;&amp;D</oddHeader>'
                '<oddFooter>&amp;CConfidential&amp;R第 &amp;P 页</oddFooter></headerFooter>')
        p = xlsx(os.path.join(src, "规则.xlsx"), [("规则", None, [["规则", "说明"], ["R1", "14 天内可退"]], foot)])
        self.assertEqual(convert.convert(p).labels[:2], ["R&D", "Confidential 第 页"])
        with self.assertRaises(BrainError) as cm:
            build(src)
        self.assertIn("规则.xlsx=confidential", str(cm.exception))
        self.assertEqual(build(src, kit_intake=dict(KIT, allow_confidential=True))[0]["classification"], "confidential")


class R1_53InvisibleContent(unittest.TestCase):
    """White and tiny text in html (as in md), white text in an xlsx cell without a fill, a hidden pptx group and a
    slide hidden with show="false" go to the hidden area; white text on a coloured background stays."""

    def test_html_and_md_white_and_tiny_text(self):
        d = tmpdir(self)
        snip = ('<p>可见。</p><p style="color:#ffffff">WHITE-P</p><span style="color: white; font-size: 1px">TINY-S</span>'
                '<span style="font-size:0.1em">TINY-EM</span><font color="#FFF">WHITE-FONT</font>'
                '<table><tr style="background-color:#1F4E79"><th style="color:#fff">表头白字</th></tr></table>'
                '<p style="background:#333;color:white">深底白字</p><span style="background-color:#fff">白底</span>'
                '<span aria-hidden="true">读屏不读</span><div style="overflow:hidden">溢出隐藏</div>')
        for name, text in (("a.html", "<html><body>%s</body></html>" % snip), ("a.md", "# 需求\n\n%s\n" % snip)):
            doc = convert.convert(write(os.path.join(d, name), text))
            hidden = "\n".join(t for _, t in doc.hidden)
            for h in ("WHITE-P", "TINY-S", "TINY-EM", "WHITE-FONT"):
                self.assertNotIn(h, doc.text, name)
                self.assertIn(h, hidden, name)
            for shown in ("可见", "表头白字", "深底白字", "白底", "读屏不读", "溢出隐藏"):
                self.assertIn(shown, doc.text, name)

    def test_css_read_as_a_browser_reads_it(self):
        """!important in any case, CSS comments, a repeated style attribute (the first counts), transparent text,
        the nearest background (a white one inside a dark one), hsl() and gradients, a colour attribute without #,
        <div .../> and <span/> that open an element, a ">" inside a quoted attribute."""
        cases = ('<span style="display:none !IMPORTANT; display:inline">H1</span>', '<span style="display:none/**/">H2</span>',
                 '<span style="visibility:hidden ! important">H3</span>', '<span style="display:none" style="color:red">H4</span>',
                 '<span style="color:transparent;background:#000">H5</span>',
                 '<div style="background:#000"><div style="background:#fff"><span style="color:#fff">H6</span></div></div>',
                 '<span style="color:#fff;background:hsl(0,0%,100%)">H7</span>',
                 '<span style="color:#fff;background:linear-gradient(#fff,#fff)">H8</span>', '<font color="fefefe">H9</font>',
                 '<div style="display:none"/>H10</div>', '<span style="display:none">a <span/> b</span> H11</span>',
                 '<span title=">" style="display:none">H12</span>', '<span style="color: white /* x */">H13</span>')
        shown = ('<div style="background:#fff"><div style="background:navy"><span style="color:#fff">深底白字</span></div></div>'
                 '<span style="color:#fff;background:rgb(0 0 0 / 80%)">半透明黑底</span><span style="color: rgb(.,.,.)">怪颜色</span>')
        d = tmpdir(self)
        for ext, wrap in ((".md", "# 需求\n\n%s\n"), (".html", "<html><body><p>%s</p></body></html>")):
            for k, snip in enumerate(cases, 1):
                doc = convert.convert(write(os.path.join(d, "%d%s" % (k, ext)), wrap % snip))
                self.assertNotIn("H%d" % k, doc.text, (ext, snip))
                self.assertIn("H%d" % k, "\n".join(t for _, t in doc.hidden), (ext, snip))
            doc = convert.convert(write(os.path.join(d, "shown" + ext), wrap % shown))
            for x in ("深底白字", "半透明黑底", "怪颜色"):
                self.assertIn(x, doc.text, ext)

    def test_xlsx_white_font_without_fill(self):
        styles = ('<?xml version="1.0"?><styleSheet %s><fonts><font><sz val="11"/></font>'
                  '<font><color rgb="FFFFFFFF"/><sz val="11"/></font><font><color theme="0"/></font>'
                  '<font><color theme="0" tint="-0.5"/></font><font><sz val="1"/></font><font><color theme="0" '
                  'tint="-inf"/></font></fonts>'
                  '<fills><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
                  '<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E79"/></patternFill></fill></fills>'
                  '<cellXfs><xf fontId="0" fillId="0"/><xf fontId="1" fillId="0"/><xf fontId="2" fillId="0"/>'
                  '<xf fontId="3" fillId="0"/><xf fontId="1" fillId="2"/><xf fontId="4" fillId="0"/></cellXfs>'
                  '</styleSheet>') % S
        cells = [("A1", "0", "普通"), ("B1", "1", "WHITE-RGB"), ("C1", "2", "WHITE-THEME"), ("D1", "3", "深灰可见"),
                 ("E1", "4", "深底白字"), ("F1", "5", "TINY-FONT")]
        row = '<row r="1">%s</row>' % "".join('<c r="%s" s="%s" t="inlineStr"><is><t>%s</t></is></c>' % c for c in cells)
        p = xlsx(os.path.join(tmpdir(self), "a.xlsx"), [("表", None, row, "")], [("xl/styles.xml", styles)])
        doc = convert.convert(p)
        hidden = "\n".join(t for _, t in doc.hidden)
        for h in ("WHITE-RGB", "WHITE-THEME", "TINY-FONT"):
            self.assertNotIn(h, doc.text)
            self.assertIn(h, hidden)
        for shown in ("普通", "深灰可见", "深底白字"):
            self.assertIn(shown, doc.text)

    def test_pptx_hidden_group_and_slide(self):
        def grp(inner, hidden=""):
            return ('<p:grpSp><p:nvGrpSpPr><p:cNvPr id="5" name="g"%s/></p:nvGrpSpPr><p:grpSpPr/>%s</p:grpSp>'
                    % (hidden, inner))

        path = os.path.join(tmpdir(self), "a.pptx")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("ppt/slides/slide1.xml", slide(sp("VISIBLE") + grp(sp("IN-GROUP")) +
                                                      grp(sp("HIDDEN-GROUP") + grp(sp("HIDDEN-NESTED")), ' hidden="1"')))
            z.writestr("ppt/slides/slide2.xml", slide(sp("HIDDEN-SLIDE-FALSE"), ' show="false"'))
            z.writestr("ppt/slides/slide3.xml", slide(sp("HIDDEN-SLIDE-0"), ' show="0"'))
        doc = convert.convert(path)
        hidden = "\n".join(t for _, t in doc.hidden)
        self.assertIn("VISIBLE", doc.text)
        self.assertIn("IN-GROUP", doc.text)
        for h in ("HIDDEN-GROUP", "HIDDEN-NESTED", "HIDDEN-SLIDE-FALSE", "HIDDEN-SLIDE-0"):
            self.assertNotIn(h, doc.text)
            self.assertIn(h, hidden)


class R1_54NothingDroppedSilently(unittest.TestCase):
    """Content in docx content controls (w:sdt), pptx tables, and xlsx rows and cells without r= reaches the package;
    a 机密 in a cover-page content control reaches gate 2; comments and speaker notes go to the hidden area."""

    def test_docx_content_controls(self):
        def sdt(inner):
            return '<w:sdt><w:sdtPr><w:alias w:val="块"/></w:sdtPr><w:sdtContent>%s</w:sdtContent></w:sdt>' % inner

        def tc(text):
            return "<w:tc>%s</w:tc>" % wp(text)

        table = ("<w:tbl><w:tr>%s%s</w:tr>%s</w:tbl>" % (tc("事项"), sdt(tc("负责人")),
                                                         sdt("<w:tr>%s%s</w:tr>" % (tc("退款接口"), tc("张伟")))))
        body = (wp("退款需求") + sdt(wp("AC-1 十四天内可退") + sdt(wp("AC-2 嵌套控件"))) + sdt(table) +
                '<w:customXml w:element="x">%s</w:customXml>' % wp("AC-3 自定义 XML") + wp("（完）"))
        d = tmpdir(self)
        doc = convert.convert(docx(os.path.join(d, "a.docx"), body))
        for shown in ("退款需求", "AC-1 十四天内可退", "AC-2 嵌套控件", "AC-3 自定义 XML", "（完）"):
            self.assertIn(shown, doc.text)
        self.assertIn("| 事项 | 负责人 |\n|---|---|\n| 退款接口 | 张伟 |", doc.text)
        src = os.path.join(d, "cover")
        os.makedirs(src)
        docx(os.path.join(src, "需求.docx"), sdt(wp("CONFIDENTIAL 机密")) + wp("正文"))
        with self.assertRaises(BrainError) as cm:
            build(src)
        self.assertIn("需求.docx=confidential", str(cm.exception))

    def test_pptx_tables_charts_and_notes(self):
        def frame(graphic, hidden=""):
            return ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="f"%s/><p:cNvGraphicFramePr/><p:nvPr/>'
                    '</p:nvGraphicFramePr><a:graphic><a:graphicData uri="x">%s</a:graphicData></a:graphic>'
                    '</p:graphicFrame>' % (hidden, graphic))

        def table(*rows):
            return "<a:tbl>%s</a:tbl>" % "".join("<a:tr>%s</a:tr>" % "".join(
                '<a:tc><a:txBody><a:p><a:r><a:t>%s</a:t></a:r></a:p></a:txBody></a:tc>' % c for c in r) for r in rows)

        notes = slide(sp("备注：HIDDEN-NOTES") + '<p:sp><p:nvSpPr><p:cNvPr id="3" name="n"/><p:cNvSpPr/><p:nvPr>'
                      '<p:ph type="sldNum"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>7</a:t></a:r></a:p></p:txBody>'
                      '</p:sp>').replace("p:sld ", "p:notes ").replace("</p:sld>", "</p:notes>")
        rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                'notesSlide" Target="../notesSlides/notesSlide1.xml"/></Relationships>')
        path = os.path.join(tmpdir(self), "a.pptx")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("ppt/slides/slide1.xml", slide(sp("验收标准") + frame(table(["编号", "内容"], ["AC-3", "显示退款状态"])) +
                                                      frame(table(["HIDDEN-TABLE"]), ' hidden="1"') + frame("<chart/>")))
            z.writestr("ppt/slides/_rels/slide1.xml.rels", rels)
            z.writestr("ppt/notesSlides/notesSlide1.xml", notes)
        doc = convert.convert(path)
        self.assertIn("| 编号 | 内容 |\n|---|---|\n| AC-3 | 显示退款状态 |", doc.text)
        hidden = "\n".join(t for _, t in doc.hidden)
        for h in ("HIDDEN-TABLE", "备注：HIDDEN-NOTES"):
            self.assertNotIn(h, doc.text)
            self.assertIn(h, hidden)
        self.assertNotIn("7", hidden)                        # the slide number placeholder is not a note
        self.assertTrue(any("图表、SmartArt 或嵌入对象 1 个（幻灯片 1）" in n for n in doc.notes))

    def test_xlsx_rows_and_cells_without_r_comments_and_missing_sheets(self):
        rows = ('<row><c t="inlineStr"><is><t>编号</t></is></c><c t="inlineStr"><is><t>负责人</t></is></c></row>'
                '<row><c t="inlineStr"><is><t>AC-4</t></is></c><c t="inlineStr"><is><t>张伟</t></is></c></row>'
                '<row r="5"><c r="B5" t="inlineStr"><is><t>B5 有 r</t></is></c><c t="inlineStr"><is><t>C5 接着</t></is>'
                '</c></row><row><c t="inlineStr"><is><t>A6</t></is></c></row>')
        rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                'comments" Target="../comments1.xml"/></Relationships>')
        comments = ('<?xml version="1.0"?><comments %s><authors><author>someone</author></authors><commentList>'
                    '<comment ref="B2" authorId="0"><text><r><t>HIDDEN-COMMENT</t></r></text></comment>'
                    '</commentList></comments>' % S)
        d = tmpdir(self)
        p = xlsx(os.path.join(d, "a.xlsx"), [("规则", None, rows, "")],
                 [("xl/worksheets/_rels/sheet1.xml.rels", rels), ("xl/comments1.xml", comments)])
        doc = convert.convert(p)
        self.assertIn("| 行 | 编号 | 负责人 |  |\n|---|---|---|---|\n| 2 | AC-4 | 张伟 |  |\n| 5 |  | B5 有 r | C5 接着 |"
                      "\n| 6 | A6 |  |  |", doc.text)
        self.assertIn(("Excel 批注 规则!B2", "HIDDEN-COMMENT"), doc.hidden)
        self.assertNotIn("someone", str(doc.hidden))
        p2 = xlsx(os.path.join(d, "b.xlsx"), [("规则", None, rows, "")], [("xl/workbook.xml", (
            '<workbook %s %s><sheets><sheet name="规则" r:id="rId1"/><sheet name="缺页" r:id="rId9"/></sheets>'
            '</workbook>' % (S, R)))])
        self.assertTrue(any("缺页" in n for n in convert.convert(p2).notes))   # a sheet whose part is missing


class R1_57WhiteOnDarkFill(unittest.TestCase):
    """White text with a fill behind it (cell, table, table-style header row, paragraph, run shading or highlight)
    is visible; white text on the page and <w:vanish/> stay hidden; <w:vanish w:val="0"/> is not hidden."""

    def test_header_shading_is_not_hiding(self):
        white = '<w:color w:val="FFFFFF"/>'

        def cell(text, tcpr="", rpr=white):
            return '<w:tc>%s%s</w:tc>' % ("<w:tcPr>%s</w:tcPr>" % tcpr if tcpr else "", wp(text, rpr))

        dark = '<w:shd w:val="clear" w:color="auto" w:fill="1F4E79"/>'
        direct = ("<w:tbl><w:tr>%s%s</w:tr><w:tr>%s%s</w:tr></w:tbl>"
                  % (cell("任务", dark), cell("修订人", dark), cell("退款接口", rpr=""), cell("张伟", rpr="")))
        styled = ('<w:tbl><w:tblPr><w:tblStyle w:val="Grid4"/><w:tblLook w:val="04A0"/></w:tblPr><w:tr>%s%s</w:tr>'
                  '<w:tr>%s%s</w:tr></w:tbl>' % (cell("任务"), cell("负责人"), cell("对账"), cell("WHITE-BODY-CELL")))
        styles = ('<?xml version="1.0"?><w:styles %s><w:style w:type="table" w:styleId="Grid4"><w:tblStylePr '
                  'w:type="firstRow"><w:tcPr><w:shd w:val="clear" w:color="auto" w:fill="4472C4"/></w:tcPr>'
                  '</w:tblStylePr></w:style><w:style w:type="paragraph" w:styleId="WhiteHead"><w:rPr>%s</w:rPr>'
                  '</w:style></w:styles>') % (W, white)
        body = (direct + styled +
                '<w:p><w:pPr><w:shd w:val="clear" w:fill="000000"/></w:pPr><w:r><w:rPr>%s</w:rPr><w:t>黑底段落白字</w:t>'
                '</w:r></w:p>' % white + wp("高亮白字", white + '<w:highlight w:val="darkBlue"/>') +
                '<w:p><w:pPr><w:pStyle w:val="WhiteHead"/><w:shd w:val="clear" w:fill="1F4E79"/></w:pPr><w:r>'
                '<w:t>样式白字深底</w:t></w:r></w:p>' + wp("WHITE-ON-PAGE", white) +
                '<w:p><w:pPr><w:pStyle w:val="WhiteHead"/></w:pPr><w:r><w:t>WHITE-STYLE-ON-PAGE</w:t></w:r></w:p>' +
                wp("VANISH", "<w:vanish/>") + wp("不隐藏", '<w:vanish w:val="0"/>'))
        d = tmpdir(self)
        src = os.path.join(d, "src")
        os.makedirs(src)
        doc = convert.convert(docx(os.path.join(src, "分工.docx"), body, [("word/styles.xml", styles)]))
        self.assertIn("| 任务 | 修订人 |\n|---|---|\n| 退款接口 | 张伟 |", doc.text)
        self.assertIn("| 任务 | 负责人 |", doc.text)
        for shown in ("黑底段落白字", "高亮白字", "样式白字深底", "不隐藏"):
            self.assertIn(shown, doc.text)
        hidden = "\n".join(t for _, t in doc.hidden)
        for h in ("WHITE-BODY-CELL", "WHITE-ON-PAGE", "WHITE-STYLE-ON-PAGE", "VANISH"):
            self.assertNotIn(h, doc.text)
            self.assertIn(h, hidden)
        rep, store = build(src)
        blob = "\n".join(package_text(store, rep).values())
        self.assertNotIn("张伟", blob)                       # the 修订人 column keeps its header, so it is masked
        self.assertIn("person×1", rep["findings"][0])

    def test_the_nearest_layer_counts(self):
        """White over dark is white (a white cell in a dark table, a white run shading in a dark cell); a solid
        pattern shows its pattern colour; the default paragraph style, the document defaults and the theme colour
        count."""
        white = '<w:color w:val="FFFFFF"/>'
        dark = '<w:shd w:val="clear" w:fill="1F4E79"/>'
        body = ('<w:tbl><w:tblPr>%s</w:tblPr><w:tr><w:tc><w:tcPr><w:shd w:val="clear" w:fill="FFFFFF"/></w:tcPr>%s</w:tc>'
                '<w:tc><w:tcPr>%s</w:tcPr>%s</w:tc><w:tc>%s</w:tc></w:tr></w:tbl>'
                % (dark, wp("H1", white), dark, wp("H2", white + '<w:shd w:val="clear" w:fill="FFFFFF"/>'),
                   wp("表底深色可见", white)) +
                wp("H3", white + '<w:shd w:val="solid" w:color="FFFFFF" w:fill="000000"/>') +
                wp("H4", '<w:color w:val="000000" w:themeColor="background1"/>') +
                wp("主题浅色加深可见", '<w:color w:val="BFBFBF" w:themeColor="background1" w:themeShade="BF"/>'))
        for styles, text in (('<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:rPr><w:vanish/></w:rPr>'
                              '</w:style>', "H5"),
                             ('<w:docDefaults><w:rPrDefault><w:rPr>%s</w:rPr></w:rPrDefault></w:docDefaults>' % white,
                              "H6")):
            path = docx(os.path.join(tmpdir(self), "a.docx"), body + wp(text),
                        [("word/styles.xml", '<?xml version="1.0"?><w:styles %s>%s</w:styles>' % (W, styles))])
            doc = convert.convert(path)
            hidden = "\n".join(t for _, t in doc.hidden)
            for h in ("H1", "H2", "H3", "H4", text):
                self.assertNotIn(h, doc.text, (styles, h))
                self.assertIn(h, hidden)
        doc = convert.convert(docx(os.path.join(tmpdir(self), "b.docx"), body))
        for shown in ("表底深色可见", "主题浅色加深可见"):
            self.assertIn(shown, doc.text)


class R1_58Big5(unittest.TestCase):
    """A Big5 text (HK / TW exports) is read as Big5, not as GB18030 mojibake, so its names and phones are masked;
    GB text stays GB."""
    TRAD = "退款需求\r\n負責人：陳大文\r\n電話：9123 4567\r\n退款須在十四日內完成，唔該跟進。\r\n"
    SIMP = "退款需求\n负责人：张伟\n退款须在十四日内完成。\n"

    def test_big5_and_gb_texts(self):
        d = tmpdir(self)
        src = os.path.join(d, "src")
        write(os.path.join(src, "big5.txt"), self.TRAD.encode("big5hkscs"), "wb")
        write(os.path.join(src, "cp950.md"), ("# 價格\n\n上限 €100，負責人：張偉\n").encode("cp950"), "wb")
        write(os.path.join(src, "gb.txt"), self.SIMP.encode("gb18030"), "wb")
        write(os.path.join(src, "gb-trad.txt"), self.TRAD.encode("gb18030"), "wb")   # traditional text saved as GBK
        for name, want in (("big5.txt", "負責人：陳大文"), ("cp950.md", "上限 €100"), ("gb.txt", "负责人：张伟"),
                           ("gb-trad.txt", "負責人：陳大文")):
            self.assertIn(want, convert.convert(os.path.join(src, name)).text, name)
        rep, store = build(src)
        blob = "\n".join(package_text(store, rep).values())
        for raw in ("陳大文", "張偉", "张伟", "9123 4567"):
            self.assertNotIn(raw, blob)
        self.assertIn("phone×1", [f for f in rep["findings"] if "big5.txt" in f][0])


class R1_61MarkingsOnEverySheet(unittest.TestCase):
    """Gate 2 reads each sheet's head and tail (a hidden sheet marked 绝密, a 机密 sheet in the middle of a long
    workbook) and the hidden area, which goes into the package too."""
    LONG = [["编号", "需求"]] + [["R%d" % i, "退款规则说明第 %d 条：按原路退回，状态写入退款单。" % i] for i in range(150)]

    def gate2(self, src):
        return build(src, dry_run=True)[0]

    def test_hidden_and_middle_sheets_and_hidden_text(self):
        d = tmpdir(self)
        a = os.path.join(d, "a")
        xlsx(os.path.join(a, "x.xlsx"), [("需求", None, [["编号", "需求"], ["R1", "14 天内可退"]], ""),
                                         ("内部定价", "hidden", [["绝密 · Top Secret"], ["渠道", "底价费率"]], "")])
        rep = self.gate2(a)
        self.assertEqual(rep["classification"], "secret")
        self.assertTrue(any("绝密" in b for b in rep["blocking"]))
        b = os.path.join(d, "b")
        xlsx(os.path.join(b, "x.xlsx"), [("需求", None, self.LONG, ""), ("费率", None, [["机密 · Confidential"],
                                                                                    ["银联", "0.18%"]], ""),
                                         ("附录", None, self.LONG, "")])
        self.assertEqual(self.gate2(b)["classification"], "confidential")
        c = os.path.join(d, "c")
        os.makedirs(c)
        docx(os.path.join(c, "x.docx"), wp("退款需求") + wp("绝密：底价", "<w:vanish/>"))
        self.assertEqual(self.gate2(c)["classification"], "secret")
        e = os.path.join(d, "e")
        xlsx(os.path.join(e, "x.xlsx"), [("需求", None, self.LONG, ""), ("附录", None, self.LONG, "")])
        self.assertEqual(self.gate2(e)["classification"], "internal")


class R1_63OneBadFile(unittest.TestCase):
    """A damaged docx (zlib.error in a member) or a file that cannot be read is refused with a reason; the other
    files are taken in as usual."""

    def test_damaged_and_unreadable_files_are_refused_alone(self):
        d = tmpdir(self)
        src = os.path.join(d, "src")
        write(os.path.join(src, "ok.md"), "# 退款\n\n14 天内可退。\n")
        bad = os.path.join(src, "坏.docx")
        with zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", '<w:document %s><w:body>%s</w:body></w:document>'
                       % (W, wp("第一条：退款规则说明，十四天内可以退款。") * 200))
        with zipfile.ZipFile(bad) as z:
            info = z.getinfo("word/document.xml")
        data = bytearray(open(bad, "rb").read())
        start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        for k in range(start + 10, start + 40):              # flip bytes inside the compressed member
            data[k] ^= 0x5A
        write(bad, bytes(data), "wb")
        locked = write(os.path.join(src, "附件", "旧版.md"), "# 旧版\n")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o644)
        os.symlink(os.path.join(d, "gone.md"), os.path.join(src, "断链.md"))
        if os.access(locked, os.R_OK):
            self.skipTest("running as a user who can read a mode-000 file")
        rep, store = build(src)
        self.assertEqual(rep["sources"], 1)
        reasons = {r["name"]: r["reason"] for r in rep["refused"]}
        self.assertIn("文件内容损坏或格式不对", reasons["src/坏.docx"])
        self.assertIn("读不了这个文件", reasons["src/附件/旧版.md"])
        self.assertIn("读不了这个文件", reasons["src/断链.md"])
        self.assertIn("14 天内可退", "\n".join(package_text(store, rep).values()))


if __name__ == "__main__":
    unittest.main()
