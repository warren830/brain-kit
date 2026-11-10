"""Review round 2 on document intake: regressions from round 1's fixes (R2-01, R2-11, R2-12, R2-08).

Password-shaped values are built at runtime, so the source holds none (the commit hook scans for them).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit.intake import checks  # noqa: E402
from tests.test_r1_intake_checks import Base, no_gitleaks  # noqa: E402


class R2_01SpreadWholeWordsOfSureValues(Base):
    """R1-48 spread a name or a password into every substring: RefundService became Refund[人名-3], 高峰期 became
    [人名-1]期, and acceptance criteria lost their words. Team names in a name column, @Service and @所有人 counted as
    sure names."""

    def setUp(self):
        super().setUp()
        no_gitleaks(self)

    def test_code_and_words_keep_their_letters(self):
        self.file("设计.md", "# 退款服务设计\n\n```java\n@Service\npublic class RefundService {\n    @Autowired\n"
                  "    private OrderService orderService;\n    public Refund create(@Valid RefundRequest req) { }\n}\n"
                  "@Data\npublic class RefundRequest { }\n```\n\n单元测试类 RefundServiceTest。\n")
        self.file("清单.md", "# 需求清单\n\n| 编号 | 需求 | 提出人 | 负责人 |\n|---|---|---|---|\n"
                  "| R-1 | 商家后台查看退款 | 商家 | 风控 |\n| R-3 | 高峰期限流 | 运营 | 高峰 |\n\n"
                  "商家在商家后台发起退款；风控规则命中时转人工。\n高峰期 QPS 目标 5000。\n")
        self.file("backlog.md", "# Backlog\n\n| Item | Owner |\n|---|---|\n| Refund API | Billing |\n\n"
                  "The Billing API returns 201.\n")
        self.file("纪要.md", "# 评审纪要\n\n@所有人 请在周五前确认。\n\n- AC-3：订单详情页所有人都可以查看。\n")
        self.file("峰值.md", "# 峰值需求\n\n负责人：高峰\n\n- AC-3：高峰期支付成功率不低于 99.9%。\n")
        text = self.segments(self.build())
        for kept in ("public class RefundService {", "private OrderService orderService;", "@Service", "@Data",
                     "create(@Valid RefundRequest req)", "单元测试类 RefundServiceTest。", "| 商家 | 风控 |",
                     "商家在商家后台发起退款；风控规则命中时转人工。", "高峰期 QPS 目标 5000。", "| Refund API | Billing |",
                     "The Billing API returns 201.", "@所有人 请在周五前确认。", "订单详情页所有人都可以查看",
                     "高峰期支付成功率"):
            self.assertIn(kept, text)
        self.assertEqual(text.count("[人名-"), 2)               # 负责人：高峰 and its cell (R3-01), nothing else

    def test_unsure_name_column_values_are_listed_to_check(self):
        self.file("清单.md", "# 清单\n\n| 需求 | 负责人 |\n|---|---|\n| 退款 | 风控 |\n| 对账 | Billing |\n")
        rep = self.build()
        self.assertNotIn("[人名-", self.segments(rep))
        self.assertIn("请你确认 2 处", self.package_text(rep))

    def test_sure_names_still_spread_as_whole_words(self):
        self.file("a.md", "# a\n\n| 需求 | 负责人 |\n|---|---|\n| 退款 | 张伟 |\n\nOwner: Tim Chen\n")
        self.file("b.md", "# b\n\n张伟说修好了，Tim Chen 复核。Timeout 30 s，Tim Chenery 不是他。\n")
        text = self.segments(self.build())
        self.assertNotIn("张伟", text)
        self.assertIn("[人名-1]说修好了", text)
        self.assertIn("[人名-2] 复核", text)
        self.assertIn("Timeout 30 s，Tim Chenery 不是他", text)

    def test_passwords_spread_as_whole_words(self):
        short, digits = "qwer" + "ty", "1234" + "56"
        self.file("账号.md", "# 账号\n\n测试账号 tester，password: %s\n\n默认密码为 %s。\n\npassword: password\n\n"
                  "token=refresh_token\n" % (short, digits))
        self.file("说明.md", "# 说明\n\n联调用 tester / %s 登录。\n\n- 输入错误的 password 5 次锁定\n"
                  "- 订单号 OD20%s7 可查\n- grant_type 取 refresh_token 时换新令牌\n- 键盘 %suiop 一行\n"
                  % (short, digits, short))
        text = self.segments(self.build())
        self.assertNotIn(short + " ", text)
        self.assertIn("联调用 tester / [密钥-", text)
        self.assertIn("输入错误的 password 5 次锁定", text)
        self.assertIn("订单号 OD20%s7 可查" % digits, text)
        self.assertIn("grant_type 取 refresh_token 时换新令牌", text)
        self.assertIn("键盘 %suiop 一行" % short, text)
        self.assertNotIn("password: password", text)            # still masked where it was found

    def test_annotations_are_no_mentions(self):
        hits = checks.scan("```java\n@Service\npublic class A { @Autowired B b; }\n```\n\n`@Transactional` 包住写操作\n")
        self.assertEqual([h for h in hits if h.kind == "person"], [])
        self.assertEqual([(h.cat, h.value) for h in checks.scan("请 @李娜 确认，@Alice Wang 复核") if h.kind == "person"],
                         [("pii", "@李娜"), ("pii", "@Alice Wang")])



class R2_11PiiHoldingASecretIsMaskedWhole(Base):
    """A PIN that spread (支付密码：8888) landed inside a phone or an email of another file: the secret hit took the
    place of the whole phone hit, so 139 and 1234 were shown around [密钥-1]."""

    def test_phone_and_email_stay_whole(self):
        no_gitleaks(self)
        pin = "88" + "88"
        self.file("环境.md", "# 测试环境\n\n支付密码：%s\n" % pin)
        self.file("联系人.md", "# 联系\n\n客服经理手机 139 %s 1234\n\n邮箱 kefu.%s@example.com\n" % (pin, pin))
        text = self.segments(self.build())
        self.assertIn("客服经理手机 [电话-1]", text)
        self.assertIn("邮箱 [邮箱-1]", text)
        self.assertNotIn("1234", text)


class R2_12PasswordRuleIsNoPassword(Base):
    """「密码：8-16位」was read as the password 8-16 and spread to the user-name rule's 8-16."""

    def test_length_rules(self):
        for text in ("- AC-1：密码：8-16位，必须包含大小写字母和数字。", "password: 8-16 characters", "密码：6-20 个字符"):
            self.assertEqual([h for h in checks.scan(text) if h.cat == "secret"], [], text)
        self.file("注册需求.md", "# 注册需求\n\n- AC-1：密码：8-16位，必须包含大小写字母和数字。\n"
                  "- AC-2：用户名长度 8-16 个字符。\n")
        text = self.segments(self.build())
        self.assertIn("用户名长度 8-16 个字符", text)
        self.assertIn("密码：8-16位", text)


class R2_08WanAloneIsACount(Base):
    """The user's decision (2026-11-10): 万 / 亿 with neither a currency nor 元 is a count (峰值 QPS 10万, 日活 100万),
    not an amount; 5 万元 and 人民币 300 万 stay amounts."""

    def amounts(self, text):
        return [h.value for h in checks.scan(text) if h.kind == "amount"]

    def test_counts_and_amounts(self):
        for text in ("峰值 QPS 10万", "日活 100万，峰值 QPS 5万。", "数据量：2亿", "存量订单 800萬。", "预算 500万左右"):
            self.assertEqual(self.amounts(text), [], text)
        for text, value in (("退款超过 5 万元时人工审核", "5 万元"), ("授信额度人民币 300 万", "人民币 300 万"),
                            ("100萬港元", "100萬港元"), ("HKD 5千万", "HKD 5千万")):
            self.assertEqual(self.amounts(text), [value], text)

    def test_prd_without_money_passes_gate_3(self):
        self.file("性能需求.md", "# 性能需求\n\n- 大促峰值 QPS 10万，日活 100万。\n- 大促时峰值 15万 QPS\n")
        rep = self.build(business=None)
        self.assertIn("峰值 QPS 10万，日活 100万", self.segments(rep))


if __name__ == "__main__":
    unittest.main()
