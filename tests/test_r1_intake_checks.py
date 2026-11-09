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


if __name__ == "__main__":
    unittest.main()
