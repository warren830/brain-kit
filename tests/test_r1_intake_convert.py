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
    parts = list(files)
    for i, (name, state, rows, tail) in enumerate(sheets, 1):
        wb += '<sheet name="%s" sheetId="%d"%s r:id="rId%d"/>' % (name, i, ' state="%s"' % state if state else "", i)
        rels += '<Relationship Id="rId%d" Target="worksheets/sheet%d.xml"/>' % (i, i)
        data = rows if isinstance(rows, str) else "".join('<row r="%d">%s</row>' % (r, "".join(
            '<c r="%s%d" t="inlineStr"><is><t>%s</t></is></c>' % (chr(65 + c), r, v) for c, v in enumerate(row) if v))
            for r, row in enumerate(rows, 1))
        parts.append(("xl/worksheets/sheet%d.xml" % i, '<?xml version="1.0"?><worksheet %s><sheetData>%s</sheetData>%s'
                      '</worksheet>' % (S, data, tail)))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", wb + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", rels + "</Relationships>")
        for n, data in parts:
            z.writestr(n, data)
    return path


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


class R1_51SheetHeaderFooter(unittest.TestCase):
    """Gate 2 reads an xlsx sheet's page header and footer (format codes taken out), as it reads a docx's."""

    def test_footer_marking_raises_the_class(self):
        d = tmpdir(self)
        src = os.path.join(d, "src")
        foot = ('<headerFooter><oddHeader>&amp;L&amp;"Arial,Bold"&amp;12R&amp;&amp;D</oddHeader>'
                '<oddFooter>&amp;CConfidential&amp;R第 &amp;P 页</oddFooter></headerFooter>')
        p = xlsx(os.path.join(src, "规则.xlsx"), [("规则", None, [["规则", "说明"], ["R1", "14 天内可退"]], foot)])
        self.assertEqual(convert.convert(p).labels, ["R&D", "Confidential 第 页"])
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

    def test_xlsx_white_font_without_fill(self):
        styles = ('<?xml version="1.0"?><styleSheet %s><fonts><font><sz val="11"/></font>'
                  '<font><color rgb="FFFFFFFF"/><sz val="11"/></font><font><color theme="0"/></font>'
                  '<font><color theme="0" tint="-0.5"/></font><font><sz val="1"/></font></fonts>'
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
        P = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
             'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"')

        def sp(text, hidden=""):
            return ('<p:sp><p:nvSpPr><p:cNvPr id="2" name="t"%s/></p:nvSpPr><p:txBody><a:p><a:r><a:t>%s</a:t></a:r>'
                    '</a:p></p:txBody></p:sp>' % (hidden, text))

        def grp(inner, hidden=""):
            return ('<p:grpSp><p:nvGrpSpPr><p:cNvPr id="5" name="g"%s/></p:nvGrpSpPr><p:grpSpPr/>%s</p:grpSp>'
                    % (hidden, inner))

        def slide(inner, attrs=""):
            return '<?xml version="1.0"?><p:sld %s%s><p:cSld><p:spTree>%s</p:spTree></p:cSld></p:sld>' % (P, attrs, inner)

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


if __name__ == "__main__":
    unittest.main()
