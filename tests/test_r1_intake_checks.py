"""Review round 1 on document intake: the checks, the package and the `brain intake` command (R1-50 ... R1-70).

Key-shaped and token-shaped values are built at runtime, so the source holds none (the commit hook scans for them).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit.common import BrainError  # noqa: E402
from brainkit.intake import checks, package  # noqa: E402
from tests.helpers import tmpdir  # noqa: E402

KIT = {"allow_confidential": False, "bulk_pii_limit": 30, "segment_chars": 6000, "max_file_mb": 50, "max_total_mb": 200}


def write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        f.write(text)
    return path


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def kinds(text):
    return [(h.cat, h.kind) for h in checks.scan(text)]


def no_gitleaks(test):
    """Take every gitleaks off PATH for this test: the built-in rules alone."""
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(d for d in old.split(os.pathsep)
                                         if not os.path.exists(os.path.join(d, "gitleaks")))
    test.addCleanup(os.environ.__setitem__, "PATH", old)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        self.src = os.path.join(self.d, "src")
        self.store = os.path.join(self.d, "store")

    def file(self, rel, text, mode="w"):
        return write(os.path.join(self.src, rel), text, mode)

    def build(self, paths=None, **kw):
        args = dict(submitter="alice", consent=True, classification="internal", business="mask", kit_intake=KIT)
        args.update(kw)
        return package.build(paths or [self.src], "PAY-1", self.store, **args)

    def package_text(self, rep):
        """Every file of the written package, concatenated."""
        base = os.path.join(self.store, "packages", rep["package"])
        out = []
        for root, _, files in os.walk(base):
            for f in sorted(files):
                out.append(read(os.path.join(root, f)))
        return "\n".join(out)

    def segments(self, rep):
        base = os.path.join(self.store, "packages", rep["package"], "segments")
        return "\n".join(read(os.path.join(base, f)) for f in sorted(os.listdir(base)))


class R1_50PgpPrivateKey(Base):
    """Gate 5 refuses a private key in every text form, not only PEM: a PGP key block was taken in, in clear."""

    def test_private_key_forms_are_found_without_gitleaks(self):
        body = "\n" + "\n".join(["lQOYBGVhY2AB" + "Q" * 52] * 3) + "\n=Xy7Q\n"
        for label in ("PGP PRIVATE KEY BLOCK", "SSH2 ENCRYPTED PRIVATE KEY", "OPENSSH PRIVATE KEY", "PRIVATE KEY",
                      "RSA PRIVATE KEY"):
            armour = "-----BEGIN " + label + "-----"
            self.assertIn(("secret", "private_key"), kinds("签名私钥：\n\n" + armour + body), label)
        self.assertNotIn(("secret", "private_key"), kinds("-----BEGIN PGP " + "PUBLIC KEY BLOCK-----" + body))

    def test_pgp_private_key_refuses_the_submission(self):
        no_gitleaks(self)
        armour = "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----"
        self.file("对接说明.md", "# 对接说明\n\n和清算行对接用这把签名私钥：\n\n%s\n\nlQOYBGVhY2AB%s\n=Xy7Q\n%s\n\n"
                  "## 验收\n1. 对账文件签名校验通过\n" % (armour, "Q" * 52, armour.replace("BEGIN", "END")))
        with self.assertRaises(BrainError) as cm:
            self.build()
        self.assertIn("门 5", str(cm.exception))
        self.assertFalse(os.path.isdir(os.path.join(self.store, "packages")))



class R1_70HongKongPhone(Base):
    """(852) 9123 4567, the usual way to write a HK number, was not seen even after 電話 / Tel."""

    def test_bracketed_country_code(self):
        for text in ("客服電話：(852) 2123 4567", "負責同事 Tel: (852) 9123 4567", "(+852) 9123-4567", "00852 9123 4567",
                     "+852 9123 4567"):
            phones = [h.value for h in checks.scan(text) if h.kind == "phone"]
            self.assertEqual(len(phones), 1, text)
            self.assertTrue(phones[0].endswith("4567") and "852" in phones[0], (text, phones))
        self.assertNotIn(("pii", "phone"), kinds("版本 (852) 号文件"))      # no number after the code

    def test_masked_in_the_package(self):
        self.file("聯絡.md", "# 聯絡\n\n客服電話：(852) 2123 4567\n\n負責同事 Tel: (852) 9123 4567\n")
        text = self.segments(self.build())
        for raw in ("2123 4567", "9123 4567", "(852)"):
            self.assertNotIn(raw, text)
        self.assertIn("客服電話：[电话-1]", text)



class R1_52AmountForms(Base):
    """Gate 3 missed amounts with a currency code or name before the number, 千元 and 萬 alone."""

    def amounts(self, text):
        return [h.value for h in checks.scan(text) if h.kind == "amount"]

    def test_forms(self):
        for text, value in (("欧洲站：EUR 300 / 单", "EUR 300"), ("英国站：GBP 1,000 / 月", "GBP 1,000"),
                            ("新加坡站：SGD 2,000 / 月", "SGD 2,000"), ("单笔上限 5千元", "5千元"),
                            ("年度预算 500萬", "500萬"), ("最低認購金額：港幣 50,000", "港幣 50,000"),
                            ("单笔上限：人民币 5,000", "人民币 5,000"), ("单笔上限：美元 1,000", "美元 1,000"),
                            ("手续费：港币 100 元", "港币 100 元"), ("对照：300 EUR", "300 EUR"),
                            ("HKD 5千万", "HKD 5千万"), ("预算 500万左右", "500万")):
            self.assertEqual(self.amounts(text), [value], text)

    def test_not_amounts(self):
        for text in ("期限 3M、6M、12M", "票息 8%", "注册用户 500万用户", "支持 1000万 并发", "第 3 单元 5 个", "5千人"):
            self.assertEqual(self.amounts(text), [], text)

    def test_masked_in_the_package(self):
        self.file("报价.md", "# 报价\n\n- 欧洲站：EUR 300 / 单\n- 英国站：GBP 1,000 / 月\n- 新加坡站：SGD 2,000 / 月\n"
                  "- 国内：单笔上限 5千元，年度预算 500萬\n- 最低認購金額：港幣 50,000\n- 内地：人民币 5,000\n"
                  "- 美国：美元 1,000\n")
        text = self.segments(self.build())
        for raw in ("300", "1,000", "2,000", "5千元", "500萬", "50,000", "5,000"):
            self.assertNotIn(raw, text)
        self.assertIn("- 欧洲站：[金额-1] / 单", text)



class R1_59NameColumns(Base):
    """Names under 处理人 / 对接人 / 跟进人 / 提出人 / 值班人 / 经办人 / Assignee / Reporter columns were not masked (the same
    words as a label, 处理人：张伟, were)."""

    def people(self, text):
        return sorted(h.value for h in checks.scan(text) if h.cat == "pii" and h.kind == "person")

    def test_columns(self):
        t = ("| 时间 | 问题 | 处理人 | 对接人 | 跟进人 | 提出人 | 值班人 | 经办人 |\n|---|---|---|---|---|---|---|---|\n"
             "| 10-01 | 退款超时 | 张伟 | 李娜 | 王芳 | 陈静 | 刘洋 | 赵磊 |\n")
        self.assertEqual(self.people(t), sorted(["张伟", "李娜", "王芳", "陈静", "刘洋", "赵磊"]))
        t = ("| Key | Summary | Assignee | Reporter | Assigned to | Reported by |\n|---|---|---|---|---|---|\n"
             "| PAY-12 | Refund timeout | Alice Wang | Peter Chan | Bob Lee | Carol King |\n")
        self.assertEqual(self.people(t), sorted(["Alice Wang", "Peter Chan", "Bob Lee", "Carol King"]))
        # still not people: a field table, a summary column, a role or a team in a person column
        self.assertEqual(self.people("| Name | Type |\n|---|---|\n| Refund Status | string |\n"), [])
        self.assertEqual(self.people("| 事项 | 处理人 |\n|---|---|\n| 对账 | 支付组 |\n"), [])

    def test_masked_in_the_package(self):
        self.file("故障复盘.md", "# 故障复盘\n\n| 时间 | 问题 | 处理人 | 对接人 |\n|---|---|---|---|\n| 10-01 | 退款超时 | 张伟 | 李娜 |\n")
        self.file("jira-export.md", "# Jira export\n\n| Key | Summary | Assignee | Reporter |\n|---|---|---|---|\n"
                  "| PAY-12 | Refund timeout | Alice Wang | Peter Chan |\n")
        text = self.package_text(self.build())
        for raw in ("张伟", "李娜", "Alice Wang", "Peter Chan"):
            self.assertNotIn(raw, text)
        self.assertIn("| PAY-12 | Refund timeout | [人名-", text)


if __name__ == "__main__":
    unittest.main()
