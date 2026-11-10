"""Review round 2 on document intake: regressions from round 1's fixes (R2-01).

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
        self.assertNotIn("[人名-", text)

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




if __name__ == "__main__":
    unittest.main()
