"""Review round 1 (ai-native-dev-cf on 0d52e97), the team runner's side: the runnerd unit, runnerd's config, the hook
adapter's park.json allowance and the bridge's park.json questions. Each class names its finding; its tests fail on
0d52e97.

  R1-02  deploy/brain-runnerd.service (ProtectSystem=strict) left runnerd's brain-intake clone, examples/runnerd.json
         intake_dir, read-only: its `git pull` failed, and a run with a newly published material package never got a
         workspace.
  R1-04  the acceptance question went out while the head's own pipeline was not on the MR yet (head_pipeline null, or
         still the previous commit's); the pipeline then failed, and the product's approve reached the run as RELEASE.
  R1-10  the park.json allowance took one spelling only: ./runs/<KEY>/park.json, runs//..., runs/<KEY>/./park.json and
         an absolute path through a symlinked folder (/tmp for /private/tmp) went to Lean, which blocks them as code.
  R1-11  park.json refs given as a string passed validate_park, and the question showed it one character at a time.
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


# ---------- R1-04: the acceptance question waits for the head's own pipeline ----------

@unittest.skipUnless(sign.available(), "ssh-keygen not found")
class R104AcceptanceWaitsForTheHeadsPipeline(unittest.TestCase):
    """GitLab creates a push's pipeline a moment after the push: until then the MR's head_pipeline is null, or (older
    GitLab) still the previous commit's. That is not a commit without CI: the question waits for the head's own."""

    def setUp(self):
        t = self.t = r4.fixture(self)
        t.dispatched()
        r4.ci_routes(t)
        self.mr_pipelines, self.pipelines_down = [], False        # GET .../merge_requests/:iid/pipelines
        t.fake.on("GET", tb.GL + r"/merge_requests/(\d+)/pipelines", self.list_pipelines)

    def list_pipelines(self, m, qy, b):
        return (502, b"Bad Gateway") if self.pipelines_down else (200, list(self.mr_pipelines))

    def acceptance(self, h="8cf2cbce"):
        return self.t.park(kind="acceptance", options=[], lean_request="RELEASE " + h, question="请验收 AC-1..AC-3")

    def approve_reaches_nobody(self):
        t = self.t
        t.reply(5, "carol", "answer D-1 approve")
        self.assertEqual(t.bridge.collect(), 0)
        self.assertEqual([m for m in r4.to_run(t) if m.startswith("决策 ")], [])

    def test_head_pipeline_still_the_previous_commits(self):
        t = self.t
        old = t.mrs[5]["sha"]
        r4.pipeline(t, 41, "failed", sha=old)            # the run's last push failed; it fixed and parked again
        new = self.acceptance()
        self.assertEqual(t.bridge.watch(), 0)
        self.assertEqual((r4.asked(t), r4.ci_notices(t)), ([], []))   # nor is the old failure sent again
        r4.pipeline(t, 42, "failed", sha=new)            # a moment later: the head's own, failed
        self.assertEqual(t.bridge.watch(), 0)
        self.assertEqual(r4.asked(t), [])
        self.assertEqual(len(r4.ci_notices(t, 42)), 1, r4.to_run(t))   # not held back by a question
        self.approve_reaches_nobody()

    def test_head_pipeline_null_on_an_mr_that_runs_ci(self):
        t = self.t
        self.mr_pipelines = [{"id": 76, "sha": t.mrs[5]["sha"], "status": "success"}]
        new = self.acceptance()
        t.mrs[5]["head_pipeline"] = None
        for _ in range(2):
            self.assertEqual(t.bridge.watch(), 0)
        r4.pipeline(t, 77, "failed", sha=new)
        self.assertEqual(t.bridge.watch(), 0)
        self.assertEqual(len(r4.ci_notices(t, 77)), 1, r4.to_run(t))
        self.approve_reaches_nobody()
        self.acceptance("9d3e0a17")                      # fixed, released and parked again
        t.mrs[5]["head_pipeline"] = None
        self.assertEqual(t.bridge.watch(), 0)
        r4.pipeline(t, 78, "success")
        self.assertEqual(t.bridge.watch(), 1)
        self.assertIn("RELEASE 9d3e0a17", r4.notes(t, start="**D-1")[0])

    def test_an_mr_with_no_pipeline_at_all_is_asked_at_once(self):
        self.acceptance()                                # no CI: brain-run §6 "or that has none"
        self.assertEqual(self.t.bridge.watch(), 1)

    def test_pipelines_that_cannot_be_read_hold_the_question(self):
        t = self.t
        self.pipelines_down = True
        self.acceptance()
        with self.assertRaises(BrainError):
            t.bridge.watch()
        self.assertEqual(r4.asked(t), [])
        self.pipelines_down = False
        self.assertEqual(t.bridge.watch(), 1)            # GitLab back, no pipeline on the MR: asked

    def test_a_head_that_gets_no_pipeline_is_told_on_the_mr_once(self):
        t = self.t
        r4.pipeline(t, 41, "success", sha=t.mrs[5]["sha"])
        head = self.acceptance()
        for _ in range(2):
            self.assertEqual(t.bridge.watch(), 0)
        told = lambda: [n for n in r4.notes(t) if "没有它的流水线" in n]  # noqa: E731
        self.assertEqual(told(), [])
        with r4.later(decisions, minutes=61):
            for _ in range(2):
                self.assertEqual(t.bridge.watch(), 0)
        self.assertEqual(len(told()), 1)
        self.assertIn(head[:12], told()[0])
        self.assertEqual((r4.asked(t), r4.ci_notices(t)), ([], []))


# ---------- R1-10: every spelling of the run's own park.json ----------

def load_adapter():
    spec = importlib.util.spec_from_file_location("brain_lean_hook_r1", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class R110ParkJsonSpelling(unittest.TestCase):
    def setUp(self):
        self.lh = load_adapter()
        d = tmpdir(self)
        self.root = git_repo(os.path.join(d, "ws"))
        commit_file(self.root, "src/app.py", "print('hi')\n")
        sh("git", "-C", self.root, "switch", "-q", "-c", BR)
        write_json(os.path.join(self.root, "runs", "PAY-1", "run.json"), {"key": "PAY-1", "branch": BR})
        self.via = os.path.join(d, "via")                # a symlinked folder above the workspace (/tmp, say)
        os.symlink(self.root, self.via)

    def allowed(self, path, **tin):
        return self.lh.park_target(self.root, {"tool_name": "fs_write", "tool_input": dict(
            {"command": "create", "path": path, "file_text": "{}"}, **tin)})

    def test_every_spelling_of_this_runs_park_json(self):
        r, v = self.root, self.via
        for p in ("runs/PAY-1/park.json", "./runs/PAY-1/park.json", "runs//PAY-1/park.json", "runs/PAY-1/./park.json",
                  r + "/runs/PAY-1/park.json", r + "/./runs//PAY-1/park.json", v + "/runs/PAY-1/park.json",
                  v + "//runs/PAY-1/./park.json"):
            self.assertTrue(self.allowed(p), p)

    def test_nothing_else(self):
        r, v = self.root, self.via
        os.symlink(r, os.path.join(r, "loop"))           # a symlink inside the workspace, to the workspace
        outside = os.path.join(tmpdir(self), "runs", "PAY-1", "park.json")
        for p in ("runs/PAY-1/../PAY-1/park.json", "src/../runs/PAY-1/park.json", v + "/../via/runs/PAY-1/park.json",
                  "runs/PAY-1/park.json/", "runs/PAY-1/park.json/.", "./runs/PAY-1/", "loop/runs/PAY-1/park.json",
                  r + "/loop/runs/PAY-1/park.json", "./runs/PAY-2/park.json", "./runs/PAY-1/run.json",
                  "/runs/PAY-1/park.json", outside, "src/app.py"):
            self.assertFalse(self.allowed(p), p)
        self.assertFalse(self.allowed("./runs/PAY-1/park.json", file_path="runs/PAY-1/park.json"))  # two spellings
        # runs/<KEY>/ symlinked elsewhere: refused however it is spelled
        os.rename(os.path.join(r, "runs", "PAY-1"), os.path.join(r, "src", "PAY-1"))
        os.symlink(os.path.join("..", "src", "PAY-1"), os.path.join(r, "runs", "PAY-1"))
        for p in ("runs/PAY-1/park.json", "./runs/PAY-1/park.json", v + "/runs/PAY-1/park.json"):
            self.assertFalse(self.allowed(p), p)

    @unittest.skipUnless(HAS_LEAN, "AI-DLC Lean checkout not found")
    def test_through_the_hook_from_a_workspace_opened_through_a_symlink(self):
        d, repo = r3r.lean_repo(self)
        via = os.path.join(tmpdir(self), "via")
        os.symlink(repo, via)

        def hook(path):
            payload = {"tool_name": "fs_write", "session_id": "s1", "cwd": via, "hook_event_name": "PreToolUse",
                       "tool_input": {"command": "create", "path": path, "file_text": "{\"schema\": 1}"}}
            return subprocess.run([sys.executable, ".kiro/brain/lean-hook.py", "PreToolUse"], cwd=via, text=True,
                                  capture_output=True, input=json.dumps(payload))
        for p in ("./runs/PAY-1/park.json", "runs//PAY-1/park.json", via + "/runs/PAY-1/park.json"):
            r = hook(p)
            self.assertEqual(r.returncode, 0, "%s: %s" % (p, r.stderr.strip()[:300]))
        code = hook("./src/app.py")                      # control: Lean still gates the code
        self.assertEqual(code.returncode, 2)
        self.assertIn("[AI-DLC Lean] blocked", code.stderr)


# ---------- R1-11: refs is a list of strings ----------

class R111RefsAreAList(unittest.TestCase):
    PARK = {"schema": 1, "status": "waiting", "kind": "clarify", "question": "退款按自然日还是交易日？",
            "options": [{"id": "A", "text": "自然日"}, {"id": "B", "text": "交易日"}]}

    def test_only_a_list_of_strings(self):
        for refs in ("D01#S003, AC-2", "", ["D01#S003", 7], {"AC-2": "D01#S003"}):
            probs = decisions.validate_park(dict(self.PARK, refs=refs))
            self.assertTrue(any(x.startswith("refs must be a list of strings") for x in probs), (refs, probs))
        for refs in (["D01#S003", "AC-2"], []):
            self.assertEqual(decisions.validate_park(dict(self.PARK, refs=refs)), [], refs)
        self.assertEqual(decisions.validate_park(self.PARK), [])

    def test_the_question_names_each_ref(self):
        q = decisions.question_text("D-1", RUN, dict(self.PARK, refs=["D01#S003", "AC-2"]), ["alice"], "clarify",
                                    "a" * 40, "b" * 64)
        self.assertIn("依据：D01#S003, AC-2", q)
        q = decisions.question_text("D-1", RUN, dict(self.PARK, refs="D01#S003"), ["alice"], "clarify", "a" * 40,
                                    "b" * 64)
        self.assertNotIn("依据", q)                       # never shown a character at a time

    @unittest.skipUnless(sign.available(), "ssh-keygen not found")
    def test_a_string_is_not_asked_and_the_run_is_told(self):
        t = r3.fixture(self)
        t.dispatched()
        t.park(refs="D01#S003, AC-2")
        self.assertEqual(t.bridge.watch(), 0)
        self.assertEqual(r3.notes(t, start="**D-"), [])
        self.assertIn("refs must be a list of strings", r3.notes(t, start="D-1 无法发出")[0])
        self.assertTrue(r3.to_run(t)[0].startswith("问题没发出："))


if __name__ == "__main__":
    unittest.main()
