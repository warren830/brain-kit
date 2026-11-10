"""Review round 2 on document intake: R2-02, gate 2 knows the common marks of a whole document again.

R1-55 reads a classification mark as a label, not as any use of the word, but it left out common labels: the word in
brackets before a title (【绝密】退款系统设计方案), a title that starts with the word and a colon, a "do not forward" notice
(公司机密，未经许可不得转发), a sentence about what the document holds (此文件含有机密信息), Proprietary & Confidential,
and a company name before the word (Acme Pay Confidential, a pptx footer). Files marked so were taken in as internal.
"""
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit.common import BrainError  # noqa: E402
from brainkit.intake import package  # noqa: E402
from tests.test_r1_intake_checks import Base  # noqa: E402
from tests.test_r1_intake_convert import P, docx, slide, wp  # noqa: E402

TITLE = "# {}\n\n- AC-1：退款 T+1 到账。\n"
FIRST = "{}\n\n# 退款\n\n退款 14 天内可退。\n"
NEAR = "# 退款系统设计方案\n\n{}\n\n- AC-1：退款 T+1 到账。\n"


def ph(text, kind):
    """A slide shape in a placeholder: title, body, ftr (the footer), sldNum (the slide number)."""
    return ('<p:sp><p:nvSpPr><p:cNvPr id="2" name="%s"/><p:cNvSpPr/><p:nvPr><p:ph type="%s"/></p:nvPr></p:nvSpPr>'
            '<p:txBody><a:p><a:r><a:t>%s</a:t></a:r></a:p></p:txBody></p:sp>' % (kind, kind, text))


class R2_02WholeDocumentMarks(Base):
    ANYWHERE = (("【机密】退款方案", "confidential"), ("【绝密】退款系统设计方案", "secret"),
                ("（机密）退款系统设计方案", "confidential"), ("【機密】退款流程需求（香港）", "confidential"),
                ("[CONFIDENTIAL] Refund System Design", "confidential"), ("公司机密，未经许可不得转发", "confidential"),
                ("此文件含有机密信息，请妥善保管", "confidential"),
                ("本文档包含商业机密信息，未经授权不得外传。", "confidential"),
                ("本方案涉及公司机密内容，仅限项目组使用。", "confidential"), ("内容机密，仅限项目组成员阅读", "confidential"),
                ("注意：以下内容为机密", "confidential"), ("Confidential and Proprietary", "confidential"),
                ("Proprietary & Confidential", "confidential"),
                ("Acme Pay Confidential and Proprietary", "confidential"), ("Acme Pay Confidential", "confidential"))
    TITLES = (("机密：退款系统设计方案", "confidential"), ("CONFIDENTIAL: Refund System Design", "confidential"),
              ("Strictly Confidential: Refund System Design", "secret"), ("退款系统设计方案（绝密）", "secret"))
    AC = ("AC-1：用户回答秘密问题后才能重置支付密码。", "AC-2: Access to the refund report is restricted to admins.",
          "AC-3：导出时机密字段（证件号）一律脱敏。", "AC-4: A confidential client may hide its name on the invoice.")
    WORDS = AC + ("机密：泄露会造成严重损害", "本需求涉及用户的机密信息，需要加密存储。", "以下文件为机密，下载前要审批。",
                  "This document is NOT confidential.", "- 证件号（机密）")

    def test_marks(self):
        for text, cls in self.ANYWHERE:
            for where in (TITLE, FIRST, NEAR):
                self.assertEqual(package.detect_class(where.format(text), []), cls, where.format(text))
        for text, cls in self.TITLES:
            for where in (TITLE, FIRST):
                self.assertEqual(package.detect_class(where.format(text), []), cls, where.format(text))

    def test_words_in_sentences_are_still_not_marks(self):
        for text in self.WORDS:
            self.assertIsNone(package.detect_class(NEAR.format(text), []), text)
        for text in self.AC:                         # a cell, a note or a comment on its own
            self.assertIsNone(package.detect_class(text, []), text)

    def test_top_secret_title_refuses_the_package(self):
        self.file("d/设计.md", TITLE.format("【绝密】退款系统设计方案"))
        with self.assertRaises(BrainError) as cm:
            self.build([os.path.join(self.src, "d")])
        self.assertIn("门 2：材料按绝密处理（d/设计.md=secret）", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.store, "packages")))

    def test_marked_cover_title_in_docx_and_html(self):
        path = os.path.join(self.src, "x", "方案.docx")
        os.makedirs(os.path.dirname(path))
        docx(path, wp("【机密】退款系统设计方案") + wp("AC-1：退款 T+1 到账。"))
        self.file("y/design.html", "<html><body><h1>[CONFIDENTIAL] Refund System Design</h1>"
                                   "<p>AC-1: refund in T+1.</p></body></html>")
        for sub, name in (("x", "x/方案.docx"), ("y", "y/design.html")):
            with self.assertRaises(BrainError) as cm:
                self.build([os.path.join(self.src, sub)])
            self.assertIn("%s=confidential" % name, str(cm.exception))
            self.assertIn("allow_confidential", str(cm.exception))

    def test_pptx_footer_on_every_slide(self):
        path = os.path.join(self.src, "p", "review.pptx")
        os.makedirs(os.path.dirname(path))
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("ppt/presentation.xml", '<?xml version="1.0"?><p:presentation %s/>' % P)
            for n, shape in ((1, ph("Refund redesign", "title")), (2, ph("AC-1: refund in T+1", "body"))):
                z.writestr("ppt/slides/slide%d.xml" % n,
                           slide(shape + ph("Acme Pay Confidential", "ftr") + ph(str(n + 1), "sldNum")))
        with self.assertRaises(BrainError) as cm:
            self.build([os.path.dirname(path)])
        self.assertIn("p/review.pptx=confidential", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
