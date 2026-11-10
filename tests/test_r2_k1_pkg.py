"""Round-2 review fixes, group K1 (material packages): regression tests for R2-13."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from brainkit import cli  # noqa: E402
from brainkit.bridge import runnerd  # noqa: E402
from brainkit.bridge.handoff import Bridge  # noqa: E402
from brainkit.intake import package  # noqa: E402
from tests.helpers import git_repo, sh, tmpdir  # noqa: E402

KIT_INTAKE = {"allow_confidential": False, "bulk_pii_limit": 30, "segment_chars": 6000, "max_file_mb": 50,
              "max_total_mb": 200}
PACKAGE = ["README.md", "extras/hidden.md", "findings.md", "manifest.json", "segments/D01-S001.md", "toc.md"]


def built(test):
    """A package of one short document, as `brain intake` writes it: (store, package folder, run.json entry)."""
    d = tmpdir(test)
    src = os.path.join(d, "docs")
    os.makedirs(src)
    with open(os.path.join(src, "req.md"), "w", encoding="utf-8") as f:
        f.write("# 退款\n\n14 天内可退。\n")
    store = os.path.join(d, "store")
    rep = package.build([src], "PAY-1", store, submitter="alice", consent=True, classification="internal",
                        business="keep", kit_intake=KIT_INTAKE)
    return store, os.path.join(store, "packages", rep["package"]), {"package": rep["package"],
                                                                     "manifest_sha256": rep["manifest_sha256"]}


def files_under(d):
    return sorted(os.path.relpath(os.path.join(r, n), d).replace(os.sep, "/") for r, _, ns in os.walk(d) for n in ns)


class FinderFilesInAPackage(unittest.TestCase):
    """R2-13: the .DS_Store Finder leaves in a folder it opened (Thumbs.db, desktop.ini: Explorer) made brain start,
    the bridge and runnerd call the package damaged ('extra .DS_Store'). Intake never takes them in: they are no part
    of the package, so they are not extra, not copied into a workspace and not published."""

    LEFT = (".DS_Store", "segments/.DS_Store", "extras/Thumbs.db", "desktop.ini")

    def setUp(self):
        self.store, self.base, self.entry = built(self)
        for rel in self.LEFT:
            with open(os.path.join(self.base, *rel.split("/")), "wb") as f:
                f.write(b"Bud1")

    def test_the_package_is_intact(self):
        self.assertEqual(package.verify_package(self.base), [])
        bridge = Bridge.check_materials(SimpleNamespace(cfg={"intake_dir": self.store}, git_timeout=60), [self.entry])
        self.assertEqual(bridge, [])

    def test_not_copied_into_a_workspace(self):
        pkg = self.entry["package"]
        ws = tmpdir(self)
        runnerd.copy_materials({"intake_dir": self.store}, [self.entry], ws)
        repo = tmpdir(self)
        self.assertEqual(cli.attach_package(repo, self.store, pkg), self.entry)
        for root in (ws, repo):
            self.assertEqual(files_under(os.path.join(root, cli.MATERIALS_DIR, pkg)), PACKAGE)

    def test_not_published(self):
        git_repo(self.store)
        cli.publish_materials(self.store, {"key": "PAY-1", "materials": [self.entry]})
        files = sh("git", "-C", self.store, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
        self.assertEqual(files, ["packages/%s/%s" % (self.entry["package"], x) for x in PACKAGE])

    def test_a_symbolic_link_by_that_name_is_still_refused(self):
        outside = os.path.join(tmpdir(self), "secret.txt")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("TOKEN=outside-the-package\n")
        os.remove(os.path.join(self.base, ".DS_Store"))
        os.symlink(outside, os.path.join(self.base, ".DS_Store"))
        self.assertEqual(package.verify_package(self.base), ["symbolic link .DS_Store"])


if __name__ == "__main__":
    unittest.main()
