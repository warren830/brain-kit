"""Review round 1 on document intake: the checks, the package and the `brain intake` command (R1-50 ... R1-70).

Key-shaped and token-shaped values are built at runtime, so the source holds none (the commit hook scans for them).
"""
import json
import os
import random
import sys
import time
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



class R1_56EmphasisedLabels(Base):
    """A label or a name in Markdown emphasis was not masked: **负责人**：李娜, **Owner:** Alice Wang, Reported by **Bob
    Chen**."""

    def people(self, text):
        return [h.value for h in checks.scan(text) if h.cat == "pii" and h.kind == "person"]

    def test_emphasis(self):
        for text, names in (("**负责人**：李娜", ["李娜"]), ("- **联系人：** 王芳", ["王芳"]), ("负责人：**张伟**、**李娜**", ["张伟", "李娜"]),
                            ("*处理人*：王芳", ["王芳"]), ("**Owner:** Alice Wang", ["Alice Wang"]),
                            ("- Reported by **Bob Chen**", ["Bob Chen"]), ("__Assignee__: Alice Wang", ["Alice Wang"])):
            self.assertEqual(self.people(text), names, text)
        self.assertEqual(self.people("owner_id: Alice Wang"), [])        # a field name, not the Owner label
        self.assertEqual(self.people("**负责人**：产品经理"), [])           # a role stays a role

    def test_masked_in_the_package(self):
        self.file("需求.md", "# 退款需求\n\n负责人：张伟\n\n**负责人**：李娜\n\n- **联系人：** 王芳\n- **Owner:** Alice Wang\n"
                  "- Reported by **Bob Chen**\n")
        text = self.segments(self.build())
        for raw in ("张伟", "李娜", "王芳", "Alice Wang", "Bob Chen"):
            self.assertNotIn(raw, text)
        self.assertIn("**负责人**：[人名-2]", text)
        self.assertIn("- Reported by **[人名-5]**", text)



class R1_60CompanyWords(Base):
    """--business mask took 银行卡 / 证券账户 for company names, and a heading plus the next paragraph ("Payment Methods" /
    "Bank cards ...") for one Latin company name, which merged them into one line."""

    def business(self, text):
        return [(h.kind, h.value) for h in checks.scan(text) if h.cat == "business"]

    def test_words_are_not_companies(self):
        for text in ("用户绑定银行卡后才能申请退款，退款原路返回银行卡。", "客户的证券账户冻结时不能退款。", "填写银行账号和银行名称",
                     "## Payment Methods\n\nBank cards and e-wallets are supported.",
                     "Send a contract notification email after each refund.", "The contract no longer applies."):
            self.assertEqual(self.business(text), [], text)

    def test_companies_and_contracts_still_found(self):
        self.assertIn(("company", "汇丰银行"), self.business("汇丰银行提供清算接口"))
        self.assertIn(("company", "Example Bank Limited"), self.business("Counterparty: Example Bank Limited."))
        self.assertIn(("contract", "HT-2026-001"), self.business("合同编号：HT-2026-001"))
        self.assertIn(("contract", "C-12345"), self.business("Contract No. C-12345"))

    def test_package_keeps_the_words_and_the_heading(self):
        self.file("退款需求.md", "# 退款需求\n\n用户绑定银行卡后才能申请退款，退款原路返回银行卡。\n\n客户的证券账户冻结时不能退款。\n\n"
                  "## Payment Methods\n\nBank cards and e-wallets are supported. Send a contract notification email after "
                  "each refund.\n")
        rep = self.build()
        text = self.segments(rep)
        self.assertNotIn("[公司-", text)
        self.assertNotIn("[合同号-", text)
        self.assertIn("## Payment Methods\n\nBank cards", text)
        self.assertIn("| D01#S002 | 退款需求 / Payment Methods |", read(os.path.join(self.store, "packages", rep["package"],
                                                                                 "toc.md")))



class R1_55ClassificationMarks(Base):
    """Gate 2 took ordinary words for a classification mark (机密性, 秘密问题, 机密字段, restricted to, confidentiality) and
    blocked the whole package; real marks must still win over the class the person gave."""

    WORDS = ("## 非功能需求\n- 传输加密，保证数据的机密性、完整性和可用性", "用户忘记密码时回答秘密问题，答对后重置。",
             "用户的机密字段（证件号）要加密存储。", "Access to the refund report is restricted to admins.",
             "The partner signs a confidentiality agreement before the API key is issued.",
             "- The mobile app is a public client, the backend a confidential client (OAuth 2.0)",
             "- 不要把签名密钥当成普通配置，它是秘密，放 KMS", "本需求不涉及机密", "密级为秘密的文件需要审批",
             "系统按文件密级（公开、内部、秘密、机密、绝密）控制访问", "| 字段 | 密级 |\n|---|---|\n| 证件号 | 机密 |",
             "- 公开\n- 内部\n- 秘密\n- 机密\n- 绝密", "## 机密数据\n加密存储", "机密：泄露会造成严重损害",
             "见 https://wiki.example.com/restricted/refunds")
    MARKS = (("機密文件\n内容", "confidential"), ("**CONFIDENTIAL**", "confidential"), ("【机密】", "confidential"),
             ("# 机密", "confidential"), ("Company Confidential", "confidential"), ("密级：机密", "confidential"),
             ("| 文档密级 | 秘密 | 版本 | v1 |", "confidential"), ("Classification: Restricted", "confidential"),
             ("本文件为机密文件，请勿外传", "confidential"), ("本资料属于公司机密。", "confidential"),
             ("This document is confidential.", "confidential"),
             ("This document contains confidential information.", "confidential"),
             ("Partner API Guide | Confidential | Page 3", "confidential"), ("Confidential, do not distribute", "confidential"),
             ("Strictly Confidential – Do not distribute", "secret"), ("绝密★启用前", "secret"), ("Top Secret", "secret"),
             ("Internal Use Only", "internal"), ("本文件仅供内部使用", "internal"))

    def test_words_in_sentences_are_not_marks(self):
        for text in self.WORDS:
            self.assertIsNone(package.detect_class("# 退款需求（内部）\n\n%s\n" % text, []), text)
        self.assertIsNone(package.detect_class("正文", ["数据机密性规范 v1.0"]))

    def test_marks(self):
        for text, cls in self.MARKS:
            self.assertEqual(package.detect_class("# 退款需求\n\n%s\n\n## 规则\n14 天内可退。\n" % text, []), cls, text)
        for label, cls in (("Highly Confidential", "confidential"), ("XYZ Partner Guide Confidential Page 1", "confidential"),
                           ("绝密", "secret")):
            self.assertEqual(package.detect_class("# 退款需求\n", [label]), cls, label)

    def test_gate_2_takes_the_words_and_still_stops_a_mark(self):
        for i, text in enumerate(self.WORDS[:7]):
            self.file("w%d/req.md" % i, "# 退款需求（内部）\n\n%s\n" % text)
            rep = self.build([os.path.join(self.src, "w%d" % i)])
            self.assertEqual(rep["classification"], "internal", text)
        self.file("m/req.md", "# 合作方指南\n\n**CONFIDENTIAL**\n\n用户的机密字段要加密存储。\n")
        with self.assertRaises(BrainError) as cm:
            self.build([os.path.join(self.src, "m")])
        self.assertIn("allow_confidential", str(cm.exception))



def _dedupe_reference(hits):
    """The old quadratic _dedupe, to compare against."""
    rank = {"secret": 0, "pii": 1, "business": 2, "review": 3, "link": 4, "instruction": 5}
    hits.sort(key=lambda h: (h.start, rank[h.cat], -(h.end - h.start)))
    kept = []
    for h in hits:
        if h.cat in ("link", "instruction"):
            kept.append(h)
            continue
        clash = next((k for k in kept if k.cat not in ("link", "instruction") and h.start < k.end and k.start < h.end),
                     None)
        if clash is None:
            kept.append(h)
        elif rank[h.cat] < rank[clash.cat]:
            kept.remove(clash)
            kept.append(h)
    return sorted(kept, key=lambda h: h.start)


class R1_69LinearDedupe(Base):
    """The checks took time quadratic in the number of hits (_dedupe compared every hit with every hit kept): one
    dry run of a 1.1 MB refund CSV took minutes."""

    def test_same_result_as_before(self):
        rnd = random.Random(69)
        cats = ("secret", "pii", "business", "review", "link", "instruction")
        for _ in range(300):
            hits = []
            for _ in range(rnd.randint(0, 25)):
                a = rnd.randint(0, 60)
                hits.append(checks.Hit(rnd.choice(cats), "k", a, a + rnd.randint(1, 12), "v"))
            key = lambda hs: [(h.cat, h.start, h.end) for h in hs]   # noqa: E731
            self.assertEqual(key(checks._dedupe(list(hits))), key(_dedupe_reference(list(hits))))

    def test_many_hits(self):
        hits = [checks.Hit("business", "amount", 20 * i, 20 * i + 10, "HKD %d" % i) for i in range(50000)]
        hits += [checks.Hit("pii", "phone", 20 * i + 5, 20 * i + 15, "p") for i in range(0, 50000, 7)]
        t = time.time()
        kept = checks._dedupe(hits)
        self.assertLess(time.time() - t, 10)            # minutes before; well under a second now
        self.assertEqual(len(kept), 50000)
        self.assertEqual(sum(1 for h in kept if h.cat == "pii"), len(range(0, 50000, 7)))

    def test_big_csv_dry_run(self):
        rows = ["退款单号,订单号,退款金额,手续费,状态"]
        rows += ["RF%09d,OD%09d,HKD %d.00,HKD %d.50,已退款" % (i, i, 100 + i % 900, i % 50) for i in range(6000)]
        self.file("退款明细.csv", "\n".join(rows) + "\n")
        t = time.time()
        rep = self.build(dry_run=True)
        self.assertLess(time.time() - t, 60)
        self.assertIn("amount×12000", rep["findings"][0])



FAKE_GITLEAKS = """#!%s
# stand-in for gitleaks v8: `gitleaks dir FILE --report-format json --report-path R ...`; reports what gitleaks
# reports for a Stripe live key (rule stripe-access-token)
import json, re, sys
a = sys.argv[1:]
if not a or a[0] != "dir":
    sys.exit(1)
out = []
with open(a[1], encoding="utf-8", errors="replace") as f:
    for n, line in enumerate(f, 1):
        for m in re.finditer(r"sk_live_[0-9A-Za-z]{24,}", line):
            out.append({"RuleID": "stripe-access-token", "StartLine": n, "Match": m.group(0), "Secret": m.group(0)})
with open(a[a.index("--report-path") + 1], "w") as f:
    json.dump(out, f)
"""


class R1_62GitleaksFinds(Base):
    """With gitleaks installed, a key only gitleaks knows (a Stripe key) was neither masked nor listed in findings.md."""

    def setUp(self):
        super().setUp()
        bin_dir = os.path.join(self.d, "bin")
        tool = write(os.path.join(bin_dir, "gitleaks"), FAKE_GITLEAKS % sys.executable)
        os.chmod(tool, 0o755)
        old = os.environ.get("PATH", "")
        os.environ["PATH"] = bin_dir + os.pathsep + old
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        self.key = "sk_" + "live_" + "FAKE" + "0" * 24              # built here: no key-shaped literal in the source

    def test_masked_and_listed(self):
        self.file("支付接入.md", "# 支付接入\n\n生产环境用这把 Stripe key：%s\n\n<!-- 备用：%s -->\n" % (self.key, self.key))
        rep = self.build()
        blob = self.package_text(rep)
        self.assertNotIn(self.key, blob)
        self.assertIn("生产环境用这把 Stripe key：[密钥-1]", self.segments(rep))
        base = os.path.join(self.store, "packages", rep["package"])
        self.assertIn("备用：[密钥-1]", read(os.path.join(base, "extras", "hidden.md")))
        findings = read(os.path.join(base, "findings.md"))
        self.assertIn("密钥（已打码，请轮换） gitleaks:stripe-access-token×2", findings)    # the body and the hidden area

    def test_a_find_outside_the_text_is_listed(self):
        self.file("a.html", '<html><body><p>见 <a href="https://pay.example.com/?k=%s">文档</a></p></body></html>' % self.key)
        rep = self.build()
        self.assertNotIn(self.key, self.package_text(rep))
        self.assertIn("gitleaks 在原文件第 1 行找到 stripe-access-token", rep["findings"][0])

    def test_placeholders_do_not_collide(self):
        m = checks.Masker()
        self.assertEqual([m.placeholder("bearer", "a" * 20), m.placeholder("token", "b" * 20),
                          m.placeholder("kv_secret", "c" * 8), m.placeholder("gitleaks:x", "d" * 8)],
                         ["[令牌-1]", "[令牌-2]", "[密钥-1]", "[密钥-2]"])



class R1_64SecretMessage(Base):
    """--classification secret said 材料是绝密（），不收，只留哈希: empty brackets, and nothing is kept (README: 绝密不收)."""

    def test_given_secret(self):
        self.file("req.md", "# 退款\n\n## 验收\n1. 14 天内可退\n")
        rep = self.build(classification="secret", dry_run=True)
        self.assertEqual(rep["blocking"], ["门 2：材料按绝密处理（你选的是 secret）：绝密材料不收，不写材料包。"])
        with self.assertRaises(BrainError) as cm:
            self.build(classification="secret")
        self.assertNotIn("（）", str(cm.exception))
        self.assertNotIn("哈希", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.store, "packages")))

    def test_marked_secret(self):
        self.file("a.md", "绝密★启用前\n\n# 方案\n")
        self.file("b.md", "# 退款\n")
        with self.assertRaises(BrainError) as cm:
            self.build()
        self.assertIn("（src/a.md=secret）", str(cm.exception))
        self.assertIn("--exclude", str(cm.exception))



class R1_65FindingsListWhatWasMasked(Base):
    """findings.md counted only what each file's own scan found: a value masked because another file had it, or one
    masked in the hidden area, was not listed, and the file read 无."""

    def test_findings(self):
        self.file("a.md", "# a\n\n客服電話 9123 4567\n")
        self.file("b.md", "# b\n\n有问题请打 9123 4567\n")                       # a phone only because a.md says so
        self.file("c.md", "# c\n\n正文<!-- 联系 zhangwei@example.com -->\n")
        rep = self.build()
        base = os.path.join(self.store, "packages", rep["package"])
        self.assertIn("有问题请打 [电话-1]", read(os.path.join(base, "segments", "D02-S001.md")))
        self.assertIn("联系 [邮箱-1]", read(os.path.join(base, "extras", "hidden.md")))
        findings = read(os.path.join(base, "findings.md"))
        self.assertIn("- **D02 src/b.md**：个人信息（已打码） phone×1", findings)
        self.assertIn("- **D03 src/c.md**：个人信息（已打码） email×1；隐藏内容 1 处", findings)
        manifest = json.loads(read(os.path.join(base, "manifest.json")))
        self.assertEqual(manifest["sources"][1]["findings"], {"pii": {"phone": 1}})



class R1_66NothingTakenIn(Base):
    """When every file was refused (an old .doc and a screenshot), an empty package was written and the run started."""

    def setUp(self):
        super().setUp()
        self.file("退款需求.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1old word file", "wb")
        self.file("截图.png", b"\x89PNG\r\n\x1a\n....", "wb")

    def test_stops_with_the_reasons(self):
        with self.assertRaises(BrainError) as cm:
            self.build()
        msg = str(cm.exception)
        self.assertIn("没有一个文件收得进来，不写材料包", msg)
        self.assertIn("src/退款需求.doc（不收 .doc 格式", msg)
        self.assertIn("src/截图.png（图片不单独收", msg)
        self.assertFalse(os.path.exists(os.path.join(self.store, "packages")))

    def test_dry_run_lists_it(self):
        rep = self.build(dry_run=True)
        self.assertEqual(len(rep["blocking"]), 1)
        self.assertIn("没有一个文件收得进来", rep["blocking"][0])

    def test_one_file_taken_in_is_enough(self):
        self.file("req.md", "# 退款\n\n14 天内可退。\n")
        rep = self.build()
        self.assertEqual((rep["sources"], len(rep["refused"])), (1, 2))


if __name__ == "__main__":
    unittest.main()
