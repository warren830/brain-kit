"""Review round 3 on document intake: regressions from round 2's guard on spreading (R3-01, R3-02).

Password-shaped values are built at runtime, so the source holds none (the commit hook scans for them).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit.intake import checks  # noqa: E402
from tests.test_r1_intake_checks import Base, no_gitleaks  # noqa: E402


class R3_01AWordNameIsMaskedWhereFound(Base):
    """R2-01 put 高峰 among the common words, so 负责人：高峰 (a common name) was no longer masked at all. It is masked
    where it was found and does not spread, so 高峰期 elsewhere keeps its letters."""

    def test_masked_at_the_label_not_spread(self):
        no_gitleaks(self)
        self.file("a.md", "# 纪要\n\n负责人：高峰\n\n联系人：高峰\n")
        self.file("b.md", "# 需求\n\n| 事项 | 负责人 |\n|---|---|\n| 限流 | 高峰 |\n\n- AC-4 高峰期 QPS 5000。\n")
        text = self.segments(self.build())
        self.assertIn("负责人：[人名-1]", text)
        self.assertIn("联系人：[人名-1]", text)
        self.assertIn("| 限流 | [人名-1] |", text)
        self.assertIn("- AC-4 高峰期 QPS 5000。", text)


class R3_02OnlyAFieldNameStaysPut(Base):
    """R2-01's guard kept any password with secret, key, api or token in it from spreading, so Prod_Secret_2026 was
    masked in one file and shown in another. Only a value that is itself a field name stays put."""

    def test_real_passwords_spread_field_names_do_not(self):
        no_gitleaks(self)
        real = ["Prod_" + "Secret_2026", "live-api-" + "key-7f3k", "MyToken#" + "2026", "my-secret-" + "pass"]
        names = ["refresh_token", "X-Api-Key", "access_token", "client_secret", "admin"]
        self.file("环境.md", "# 环境\n\n" + "".join("- password: %s\n" % v for v in real + names))
        self.file("接口.md", "# 接口\n\n" + "".join("- 登录口令填 %s 即可。\n" % v for v in real + names))
        text = self.segments(self.build())
        for v in real:
            self.assertNotIn(v, text)
        for v in names:
            self.assertIn("- 登录口令填 %s 即可。" % v, text)
            self.assertNotIn("password: %s" % v, text)           # still masked where it was found
        self.assertTrue(checks._spreads("kv_secret", real[0]))


if __name__ == "__main__":
    unittest.main()
