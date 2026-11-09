"""Round 1 review findings on the intake converters (R1-47 ... R1-63): each test builds its files at runtime."""
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    """sheets: [(name, state or None, [[cell text, ...], ...], extra worksheet XML after sheetData)]."""
    wb = '<?xml version="1.0"?><workbook %s %s><sheets>' % (S, R)
    rels = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    parts = list(files)
    for i, (name, state, rows, tail) in enumerate(sheets, 1):
        wb += '<sheet name="%s" sheetId="%d"%s r:id="rId%d"/>' % (name, i, ' state="%s"' % state if state else "", i)
        rels += '<Relationship Id="rId%d" Target="worksheets/sheet%d.xml"/>' % (i, i)
        data = "".join('<row r="%d">%s</row>' % (r, "".join(
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


if __name__ == "__main__":
    unittest.main()
