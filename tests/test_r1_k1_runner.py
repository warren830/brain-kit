"""Review round 1 (ai-native-dev-cf on 0d52e97), the team runner's side: the runnerd unit, runnerd's config, the hook
adapter's park.json allowance and the bridge's park.json questions. Each class names its finding; its tests fail on
0d52e97.

  R1-02  deploy/brain-runnerd.service (ProtectSystem=strict) left runnerd's brain-intake clone, examples/runnerd.json
         intake_dir, read-only: its `git pull` failed, and a run with a newly published material package never got a
         workspace.
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import sign  # noqa: E402
from brainkit.bridge import decisions, runnerd  # noqa: E402
from brainkit.common import BrainError, load_json, write_json  # noqa: E402
from tests import test_bridge as tb  # noqa: E402
from tests import test_qa_r3_bridge as r3  # noqa: E402
from tests import test_qa_r3_runner as r3r  # noqa: E402
from tests import test_qa_r4_bridge as r4  # noqa: E402
from tests.helpers import LEAN_SRC, ROOT, commit_file, git_repo, sh, tmpdir  # noqa: E402

HAS_LEAN = os.path.exists(os.path.join(LEAN_SRC, "install.sh"))
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
RUNNERD_UNIT = os.path.join(ROOT, "deploy", "brain-runnerd.service")
BRIDGE_UNIT = os.path.join(ROOT, "deploy", "brain-bridge.service")
RUNNERD_EXAMPLE = os.path.join(ROOT, "examples", "runnerd.json")
BRIDGE_EXAMPLE = os.path.join(ROOT, "examples", "bridge.json")
# the folders of runnerd.json that runnerd writes (the rest it only reads)
RUNNERD_WRITES = ("spool_dir", "cache_dir", "runs_root", "intake_dir")
ADAPTER = os.path.join(ROOT, "kit", ".kiro", "brain", "lean-hook.py")
BR = "ai/alice/PAY-1-refund"
RUN = "RUN-PAY-1-1"


# ---------- R1-02: the runnerd unit and the folders runnerd writes ----------

def service(unit) -> dict:
    """The [Service] settings of a systemd unit file: name -> its values, in order."""
    out, section = {}, None
    with open(unit, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("["):
                section = line
            elif section == "[Service]" and "=" in line:
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), []).append(v.strip())
    return out


def read_write_paths(unit) -> list:
    """ReadWritePaths= of a unit: space-separated, possibly given more than once; a '-' in front only means a missing
    one is skipped."""
    return [p.lstrip("-+").rstrip("/") for v in service(unit).get("ReadWritePaths", []) for p in v.split()]


def covered(path, rw) -> bool:
    return any(path == p or path.startswith(p + "/") for p in rw)


def strict(test, top, rw):
    """ProtectSystem=strict on a file tree standing for "/" (`top`): everything outside ReadWritePaths loses its write
    bits (EACCES here, EROFS under systemd: git fails the same way); given back at cleanup."""
    def walk():
        for here, dirs, files in os.walk(top):
            for p in [here] + [os.path.join(here, n) for n in dirs + files]:
                if not os.path.islink(p):
                    yield p

    def give_back():
        for p in walk():
            os.chmod(p, os.stat(p).st_mode | stat.S_IWUSR)
    test.addCleanup(give_back)
    for p in list(walk()):
        if not covered("/" + os.path.relpath(p, top) if p != top else "/", rw):
            os.chmod(p, os.stat(p).st_mode & ~0o222)


class R102RunnerdUnit(unittest.TestCase):
    def test_the_unit_lets_runnerd_write_every_folder_the_example_config_names(self):
        s = service(RUNNERD_UNIT)
        self.assertEqual(s.get("ProtectSystem"), ["strict"])           # all else is read-only: so this matters
        rw, cfg = read_write_paths(RUNNERD_UNIT), load_json(RUNNERD_EXAMPLE)
        self.assertEqual([(k, cfg[k]) for k in RUNNERD_WRITES if not covered(cfg[k], rw)], [],
                         "not in ReadWritePaths of %s: %s" % (RUNNERD_UNIT, rw))

    def test_the_bridge_unit_as_well(self):
        rw, cfg = read_write_paths(BRIDGE_UNIT), load_json(BRIDGE_EXAMPLE)
        writes = [cfg["spool_dir"], cfg["intake_dir"], cfg["vouchers_dir"], os.path.dirname(cfg["ledger"])]
        self.assertEqual(service(BRIDGE_UNIT).get("ProtectSystem"), ["strict"])
        self.assertEqual([p for p in writes if not covered(p, rw)], [])

    @unittest.skipUnless(sign.available(), "ssh-keygen not found")
    @unittest.skipIf(IS_ROOT, "root writes read-only files")
    def test_runnerd_under_the_unit_pulls_a_package_published_after_its_clone(self):
        t = r3.fixture(self)
        cfg = load_json(RUNNERD_EXAMPLE)
        top = os.path.join(t.d, "host")                 # the runner's "/": the example's folders under it
        at = {k: top + cfg[k] for k in RUNNERD_WRITES}
        for k in ("spool_dir", "cache_dir", "runs_root"):
            os.makedirs(at[k], exist_ok=True)
        # brain-intake: its remote, the bridge's clone, and brainbot's clone at intake_dir, made when the runner was set
        # up: before the package below was published
        remote = os.path.join(t.d, "intake.git")
        sh("git", "init", "-q", "--bare", "-b", "main", remote)
        seed = git_repo(os.path.join(t.d, "seed"))
        commit_file(seed, "README", "brain-intake\n")
        sh("git", "-C", seed, "push", "-q", remote, "main")
        os.makedirs(os.path.dirname(at["intake_dir"]), exist_ok=True)
        sh("git", "clone", "-q", remote, at["intake_dir"])
        sh("git", "clone", "-q", remote, os.path.join(t.d, "intake"))
        mat = t.make_package()                          # into the bridge's clone (t.d/intake), then published
        sh("git", "-C", t.cfg["intake_dir"], "add", "-A")
        sh("git", "-C", t.cfg["intake_dir"], "commit", "-q", "-m", "materials for PAY-1")
        sh("git", "-C", t.cfg["intake_dir"], "push", "-q", "origin", "main")
        t.cfg["spool_dir"] = at["spool_dir"]
        t.mrs[5]["sha"] = t.push("runs/PAY-1/run.json", json.dumps(t.run_json(materials=[mat])))
        t.bridge = t.new_bridge()
        self.assertEqual(t.bridge.process(tb.P, t.bridge.kit(tb.P), t.mrs[5]), "accepted")
        strict(self, top, read_write_paths(RUNNERD_UNIT))
        self.assertEqual(runnerd.once(dict(t.runner_cfg, **at), out=lambda *_: None), 1)
        failed = os.path.join(at["spool_dir"], "failed", RUN + ".json")
        self.assertFalse(os.path.exists(failed), os.path.exists(failed) and load_json(failed)["error"])
        ready = load_json(os.path.join(at["spool_dir"], "ready", RUN + ".json"))
        toc = os.path.join(ready["path"], ".brain-materials", mat["package"], "toc.md")
        self.assertTrue(os.path.isfile(toc))
        self.assertFalse(os.access(toc, os.W_OK))       # copied read-only


if __name__ == "__main__":
    unittest.main()
