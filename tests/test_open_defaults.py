"""What a team gets without changing a setting: confidential material is taken in (`intake.allow_confidential:
false` refuses it)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit.common import load_json, write_json  # noqa: E402
from tests.test_r1_intake_checks import CliBase  # noqa: E402


class ConfidentialIsTakenByDefault(CliBase):
    """`brain intake` in a project whose kit.json is the one `brain install` writes."""

    def setUp(self):
        super().setUp()
        self.kit = os.path.join(self.repo, ".brain", "kit.json")
        self.file("合作方指南.md", "# 合作方指南\n\nConfidential\n\n退款要在 14 天内发起。\n")

    def intake(self, store):
        return self.brain("intake", self.src, "--key", "REQ-1", "--classification", "internal", "--consent",
                          "--store", store)

    def test_a_confidential_document_is_packaged_as_confidential(self):
        shipped = load_json(self.kit)
        no_intake = {k: v for k, v in shipped.items() if k != "intake"}
        for name, kit in (("templates/kit.json", shipped), ("config.DEFAULTS", no_intake)):
            with self.subTest(name):
                write_json(self.kit, kit)
                store = os.path.join(self.d, name.replace("/", "-"))
                rc, out = self.intake(store)
                self.assertEqual(rc, 0, out)
                self.assertIn("密级 confidential", out)
                manifest = load_json(os.path.join(store, "packages", "PKG-REQ-1-1", "manifest.json"))
                self.assertEqual(manifest["classification"]["effective"], "confidential")

    def test_the_switch_set_false_refuses_it(self):
        kit = load_json(self.kit)
        kit["intake"]["allow_confidential"] = False
        write_json(self.kit, kit)
        rc, out = self.intake(self.store)
        self.assertEqual(rc, 2, out)
        self.assertIn("门 2", out)
        self.assertIn("allow_confidential", out)
        self.assertFalse(os.path.exists(os.path.join(self.store, "packages", "PKG-REQ-1-1")))


if __name__ == "__main__":
    unittest.main()
