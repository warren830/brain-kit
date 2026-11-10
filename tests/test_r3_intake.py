"""Review round 3 on document intake: regressions from round 2's guard on spreading (R3-01, R3-02).

Password-shaped values are built at runtime, so the source holds none (the commit hook scans for them).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


if __name__ == "__main__":
    unittest.main()
