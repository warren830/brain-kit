"""What a team gets without changing a setting: confidential material is taken in (`intake.allow_confidential:
false` refuses it), and every shipped agent may search and fetch the web."""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import config  # noqa: E402
from brainkit.common import load_json, write_json  # noqa: E402
from tests.helpers import ROOT  # noqa: E402
from tests.test_r1_intake_checks import CliBase  # noqa: E402

WEB = {"web_search", "web_fetch"}
AGENTS = ("kit/.kiro/agents/brain-dev.md", "crew/brain-dev.json", "remote/agents/brain-run.json",
          "remote/agents/brain-reflector.json")
UNATTENDED = ("remote/agents/brain-run.json", "remote/agents/brain-reflector.json")


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


def tool_lists(rel):
    """(tools, allowedTools) of a shipped agent spec, as Kiro reads it."""
    path = os.path.join(ROOT, rel)
    if rel.endswith(".md"):
        with open(path, encoding="utf-8") as f:
            front = f.read().split("\n---\n", 1)[0]
        return json.loads(re.search(r"(?m)^tools:(.*)$", front).group(1)), []
    spec = load_json(path)
    return spec["tools"], spec.get("allowedTools", [])


class EveryAgentMayUseTheWeb(unittest.TestCase):
    def test_every_shipped_agent_lists_the_web_tools_and_no_forbidden_one(self):
        forbidden = set(config.DEFAULTS["forbidden_tools"])
        for rel in AGENTS:
            with self.subTest(rel):
                tools, allowed = tool_lists(rel)
                self.assertLessEqual(WEB, set(tools))
                self.assertFalse((set(tools) | set(allowed)) & forbidden)

    def test_the_unattended_agents_never_wait_for_an_approval_to_use_the_web(self):
        for rel in UNATTENDED:
            with self.subTest(rel):
                self.assertLessEqual(WEB, set(tool_lists(rel)[1]))


if __name__ == "__main__":
    unittest.main()
