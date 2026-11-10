"""Round 1 (K5) golden fixes: R1-96 the ablation drops the Brain entries that restate a removed one; R1-98 the oracle
accepts what the bridge accepts."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import entries as E, golden as G  # noqa: E402
from tests.helpers import git_repo, sh, tmpdir  # noqa: E402
from tests.test_golden import ClassScope, ceiling_at_the_temp_root, restore_ceiling  # noqa: E402

_OLD_CEILING = []


def setUpModule():
    _OLD_CEILING.append(ceiling_at_the_temp_root())


def tearDownModule():
    restore_ceiling(_OLD_CEILING.pop())


def entry(eid, text, src="D-1", etype="pitfall", side="builder"):
    return "<!-- id:%s type:%s side:%s rel:new src:%s -->\n%s\n" % (eid, etype, side, src, text)


RULE = "退款单号是 RF 加 9 位数字。"


class R96AblationDropsRestatingEntries(unittest.TestCase):
    """no-TECH removed TECH.md and the lines citing its ids in docs/ and the run documents, but kept the entries in
    the other Brain files that restate a TECH entry citing its id (C-08)."""

    @classmethod
    def setUpClass(cls):
        cls.d = tmpdir(ClassScope(cls))
        repo = git_repo(os.path.join(cls.d, "proj"))
        improvement = "\n".join([
            "# IMPROVEMENT", "",
            entry("I-PAY-1-1", "上次把退款单号写成 7 位，返工了：%s（见 T-PAY-1-1）。" % RULE.rstrip("。")),
            entry("I-PAY-1-2", "单号的教训记在 TECH 里。", src="T-PAY-1-1"),       # derived from it: src names it
            entry("I-PAY-1-4", "回调先查重。\n第二行。", src="D-4"),                  # other knowledge: stays
            entry("I-PAY-1-5", "示例号 T-PAY-1-10 不是这条。", src="D-5"),            # another id: stays
            entry("I-PAY-1-6", "回调要幂等，见 `T-1`。", src="D-6"),                  # a legacy id as a citation
            entry("I-PAY-1-7", "每天 T-1 日的交易早上对完。", src="D-7"),              # T-1 as a word: stays
            entry("I-PAY-1-8", "旧规则。", src="D-8,T-1"),                             # a legacy id in src
            "## 单号（T-PAY-1-1）", "",                                                # a free line citing it
            entry("I-PAY-1-9", "最后一条。", src="D-9"),
        ])
        files = {
            "brain/TECH.md": "# TECH\n\n" + entry("T-PAY-1-1", RULE) + "\n" + entry("T-1", "回调要幂等。"),
            "brain/IMPROVEMENT.md": improvement,
            "brain/PRODUCT.md": "# PRODUCT\n\n" + entry("P-PAY-1-1", "单号规则见 T-PAY-1-1。", etype="principle",
                                                        side="product"),
            "README.md": "x\n",
        }
        for rel, text in files.items():
            p = os.path.join(repo, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        sh("git", "-C", repo, "add", "-A")
        sh("git", "-C", repo, "commit", "-q", "-m", "base")
        sha = sh("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()
        cls.full_dir, cls.abl_dir = os.path.join(cls.d, "full"), os.path.join(cls.d, "no-tech")
        cls.full = G.snapshot(repo, sha, cls.full_dir)
        cls.man = G.snapshot(repo, sha, cls.abl_dir, variant="no-TECH")
        cls.noimp_dir = os.path.join(cls.d, "no-imp")
        cls.noimp = G.snapshot(repo, sha, cls.noimp_dir, variant="no-IMPROVEMENT")

    def read(self, root, rel):
        with open(os.path.join(root, *rel.split("/")), encoding="utf-8") as f:
            return f.read()

    def test_no_brain_file_keeps_a_copy_of_the_removed_entries(self):
        for d, _, names in os.walk(self.abl_dir):
            for n in names:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    text = f.read()
                self.assertNotRegex(text, r"T-PAY-1-1(?![0-9])", n)
                self.assertNotIn("RF 加 9 位", text, n)

    def test_whole_entries_go_and_the_rest_stays_as_it_was(self):
        text = self.read(self.abl_dir, "brain/IMPROVEMENT.md")
        kept = [e.id for e in E.BrainFile("IMPROVEMENT.md", text).entries()]
        self.assertEqual(kept, ["I-PAY-1-4", "I-PAY-1-5", "I-PAY-1-7", "I-PAY-1-9"])
        for eid in kept:                                   # untouched, byte for byte
            block = [b for b in E.BrainFile("x.md", self.read(self.full_dir, "brain/IMPROVEMENT.md")).entries()
                     if b.id == eid][0]
            self.assertIn("\n".join(block.lines()), text)
        self.assertNotIn("\n\n\n", text)                   # no gaps where entries were
        self.assertEqual(self.read(self.abl_dir, "brain/PRODUCT.md"), "# PRODUCT\n")
        scrubbed = {s["path"]: s for s in self.man["scrubbed"]}
        self.assertEqual(scrubbed["brain/IMPROVEMENT.md"]["entries"],
                         ["I-PAY-1-1", "I-PAY-1-2", "I-PAY-1-6", "I-PAY-1-8"])
        self.assertEqual(scrubbed["brain/PRODUCT.md"]["entries"], ["P-PAY-1-1"])

    def test_other_variants_and_the_full_snapshot_do_not_touch_them(self):
        self.assertEqual(self.full["scrubbed"], [])
        self.assertEqual(self.read(self.noimp_dir, "brain/TECH.md"), self.read(self.full_dir, "brain/TECH.md"))
        self.assertEqual(self.read(self.noimp_dir, "brain/PRODUCT.md"), self.read(self.full_dir, "brain/PRODUCT.md"))


if __name__ == "__main__":
    unittest.main()
