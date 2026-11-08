"""Golden tasks and ablation: snapshot hygiene, hidden tests, scoring, plan, run with a fake runner, the drop rule."""
import io
import json
import os
import shlex
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import cli, golden as G  # noqa: E402
from brainkit.common import BrainError  # noqa: E402
from tests.helpers import git_repo, sh, tmpdir  # noqa: E402

PY = sys.executable
CALC_BUG = "def add(a, b):\n    return a - b\n"
CALC_FIX = "def add(a, b):\n    return a + b\n"
CALC_WORSE = "def add(a, b):\n    return abs(a) + b\n"
VISIBLE = "import unittest\nfrom calc import add\n\n\nclass V(unittest.TestCase):\n    def test_one(self):\n" \
          "        self.assertEqual(add(1, 0) >= 0, True)\n"
HIDDEN = ("import unittest\nfrom calc import add\n\n\nclass H(unittest.TestCase):\n"
          "    def test_a(self):\n        self.assertEqual(add(1, 2), 3)\n\n"
          "    def test_b(self):\n        self.assertEqual(add(0, 0), 0)\n\n"
          "    def test_c(self):\n        self.assertEqual(add(-1, 1), 0)\n\n"
          "    def test_d(self):\n        self.assertEqual(add(2, 2), 4)\n")
PATCHED = ["import unittest", "from calc import add", "", "", "class P(unittest.TestCase):",
           "    def test_neg(self):", "        self.assertEqual(add(-2, -3), -5)"]


def entry(eid, etype, side, text):
    return "<!-- id:%s type:%s side:%s rel:new src:D-1 -->\n%s\n" % (eid, etype, side, text)


def new_file_patch(path, lines):
    body = "".join("+%s\n" % l for l in lines)
    return ("diff --git a/{p} b/{p}\nnew file mode 100644\n--- /dev/null\n+++ b/{p}\n@@ -0,0 +1,{n} @@\n{b}"
            .format(p=path, n=len(lines), b=body))


def run_cli(repo, *args):
    out = io.StringIO()
    cwd = os.getcwd()
    os.chdir(repo)
    try:
        with redirect_stdout(out), redirect_stderr(out):
            rc = cli.main(list(args))
    finally:
        os.chdir(cwd)
    return rc, out.getvalue()


def make_repo(test, extra=None):
    """A small project with a Brain, a buggy add(), a visible test, an .env and a git credentials file (one commit:
    each commit runs the machine's pre-commit scanner)."""
    d = tmpdir(test)
    repo = git_repo(os.path.join(d, "proj"))
    files = {
        "brain/PRODUCT.md": "# PRODUCT\n\n" + entry("P-PAY-1-1", "principle", "product", "rule p"),
        "brain/TECH.md": "# TECH\n\n" + entry("T-PAY-1-1", "pitfall", "builder", "add means plus"),
        "brain/TECH-archive.md": "# TECH archive\n",
        "brain/IMPROVEMENT.md": "# IMPROVEMENT\n\n" + entry("I-PAY-1-1", "process", "builder", "r"),
        "brain/PROJECT.md": "# PROJECT\n",
        "AGENTS.md": "# Agents\n",
        "calc.py": CALC_BUG,
        "tests/__init__.py": "",
        "tests/test_calc.py": VISIBLE,
        "sub/.env": "DEBUG=1\n",
        ".env.local": "DEBUG=2\n",
        ".git-credentials": "https://" + "someone" + ":" + "pw" + "@git.example.invalid\n",
        ".npmrc": "registry=https://registry.example.invalid/\n",
    }
    files.update(extra or {})
    for rel, text in files.items():
        p = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    sh("git", "-C", repo, "add", "-A")
    sh("git", "-C", repo, "commit", "-q", "-m", "base")
    base = sh("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()
    sh("git", "-C", repo, "remote", "add", "origin", "https://git.example.invalid/grp/proj.git")
    return d, repo, base


def make_task(root, tid, base, **over):
    tdir = os.path.join(root, "tasks", tid)
    os.makedirs(os.path.join(tdir, "hidden", "tests"))
    with open(os.path.join(tdir, "hidden", "tests", "test_hidden.py"), "w") as f:
        f.write(HIDDEN)
    with open(os.path.join(tdir, "extra.patch"), "w") as f:
        f.write(new_file_patch("tests/test_patched.py", PATCHED))
    t = {"id": tid, "repo": "proj", "base_sha": base, "intent": "fix add()", "acceptance": ["AC-1 add adds"],
         "hidden_tests": ["hidden/tests/test_hidden.py", "extra.patch"],
         "test_command": [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
         "must_use": ["T-PAY-1-1"], "oracle_answers": {"RELEASE": "approve"}}
    t.update(over)
    with open(os.path.join(tdir, "task.json"), "w") as f:
        json.dump(t, f)
    return tdir


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.d, self.repo, self.base = make_repo(self)

    def test_full_snapshot_is_clean(self):
        out = os.path.join(self.d, "snap")
        man = G.snapshot(self.repo, self.base, out)
        self.assertFalse(os.path.exists(os.path.join(out, ".git")))
        self.assertTrue(os.path.exists(os.path.join(out, "calc.py")))
        removed = {r["path"]: r["why"] for r in man["removed"]}
        self.assertEqual(removed, {".git-credentials": "credential file", "sub/.env": "env file",
                                   ".env.local": "env file"})
        self.assertTrue(os.path.exists(os.path.join(out, ".npmrc")))      # no auth line: kept
        self.assertEqual(man["brain_files"], ["IMPROVEMENT.md", "PRODUCT.md", "PROJECT.md", "TECH-archive.md",
                                              "TECH.md"])
        self.assertEqual(man["variant"], "full")
        self.assertEqual(man["base_sha"], self.base)
        # nothing in the snapshot says which variant it is, or where the repo lives
        for d, _, files in os.walk(out):
            for name in files:
                with open(os.path.join(d, name), encoding="utf-8", errors="replace") as f:
                    text = f.read()
                self.assertNotIn("git.example.invalid", text, name)
                self.assertNotIn("no-TECH", text, name)
        # deterministic
        man2 = G.snapshot(self.repo, self.base, os.path.join(self.d, "snap2"))
        self.assertEqual(man["tree_sha256"], man2["tree_sha256"])
        self.assertEqual(man["files"], man2["files"])

    def test_variants_remove_exactly_their_files(self):
        full = set(G.snapshot(self.repo, self.base, os.path.join(self.d, "full"))["brain_files"])
        for v, gone in (("no-PRODUCT", {"PRODUCT.md"}), ("no-TECH", {"TECH.md", "TECH-archive.md"}),
                        ("no-IMPROVEMENT", {"IMPROVEMENT.md"})):
            out = os.path.join(self.d, v)
            man = G.snapshot(self.repo, self.base, out, variant=v)
            self.assertEqual(full - set(man["brain_files"]), gone, v)
            self.assertEqual(sorted(r["path"] for r in man["removed"] if r["why"].startswith("ablation")),
                             sorted("brain/" + g for g in gone))
            self.assertTrue(os.path.exists(os.path.join(out, "AGENTS.md")))
        with self.assertRaises(BrainError):
            G.snapshot(self.repo, self.base, os.path.join(self.d, "x"), variant="no-PROJECT")

    def test_refuses_non_empty_out_and_unknown_commit(self):
        out = os.path.join(self.d, "busy")
        os.makedirs(out)
        open(os.path.join(out, "f"), "w").close()
        with self.assertRaises(BrainError):
            G.snapshot(self.repo, self.base, out)
        self.assertTrue(os.path.exists(os.path.join(out, "f")))        # left alone
        with self.assertRaises(BrainError):
            G.snapshot(self.repo, "0" * 40, os.path.join(self.d, "y"))

    def test_key_in_tree_refuses_and_leaves_nothing(self):
        # The archive "contains" a key: written in right after extraction, so no key is ever committed (the
        # machine's pre-commit scanner refuses that, rightly) and nothing key-shaped sits in this source file.
        block = "-----BEGIN " + "OPENSSH " + "PRIVATE KEY-----\nabc\n-----END " + "OPENSSH PRIVATE KEY-----\n"
        cloud = "AKIA" + "IOSFODNN7" + "EXAMPLE"
        real = G._extract
        for rel, text, rule in (("deploy/id_key", block, "private key"), ("conf.ini", "k=%s\n" % cloud,
                                                                         "cloud access key")):
            def extract(tar, out, rel=rel, text=text):
                real(tar, out)
                p = os.path.join(out, *rel.split("/"))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(text)
            out = os.path.join(self.d, "snap-" + rule.replace(" ", "-"))
            with mock.patch.object(G, "_extract", extract):
                with self.assertRaises(BrainError) as cm:
                    G.snapshot(self.repo, self.base, out)
            self.assertIn("%s (%s)" % (rel, rule), str(cm.exception))
            self.assertNotIn(cloud, str(cm.exception))                     # paths and rule names, never values
            self.assertFalse(os.path.exists(out))

    def test_npm_auth_file_removed(self):
        d, repo, base = make_repo(self, extra={"web/.npmrc": "//registry.example.invalid/:_authToken=${NPM}\n"})
        man = G.snapshot(repo, base, os.path.join(d, "s"))
        self.assertIn({"path": "web/.npmrc", "why": "credential file (npm auth)"}, man["removed"])


class Tasks(unittest.TestCase):
    def test_load_and_validate(self):
        d, repo, base = make_repo(self)
        tdir = make_task(d, "G-1", base, test_command="%s -m unittest" % shlex.quote(PY))
        t = G.load_task(tdir)
        self.assertEqual(t["test_command"], [PY, "-m", "unittest"])
        self.assertEqual(t["_dir"], tdir)
        bad = make_task(d, "G-2", "abc", hidden_tests=["/etc/passwd", "../x.py", "tests/t.py", "hidden/none.py"],
                        acceptance=[], must_use=["not-an-id"], oracle_answers={"re:(": "A"})
        with self.assertRaises(BrainError) as cm:
            G.load_task(bad)
        msg = str(cm.exception)
        for part in ("base_sha", "'/etc/passwd'", "'../x.py'", "tests/t.py must be", "hidden/none.py not found",
                     "acceptance", "must_use", "bad regex"):
            self.assertIn(part, msg)
        with self.assertRaises(BrainError):
            G.load_tasks(os.path.join(d, "tasks"))          # G-2 is bad: the whole load fails, all listed
        make_task(d, "G-0", base)
        os.remove(os.path.join(bad, "task.json"))
        self.assertEqual([t["id"] for t in G.load_tasks(d)], ["G-0", "G-1"])


class Score(unittest.TestCase):
    def setUp(self):
        self.d, self.repo, self.base = make_repo(self)
        self.task = G.load_task(make_task(self.d, "G-1", self.base))

    def workdir(self, name, calc):
        out = os.path.join(self.d, name)
        G.snapshot(self.repo, self.base, out)
        with open(os.path.join(out, "calc.py"), "w") as f:
            f.write(calc)
        return out

    def test_hidden_tests_and_counts(self):
        w = self.workdir("good", CALC_FIX)
        res = G.score(w, self.task)
        self.assertTrue(os.path.exists(os.path.join(w, "tests", "test_hidden.py")))
        self.assertTrue(os.path.exists(os.path.join(w, "tests", "test_patched.py")))
        self.assertEqual((res["passed"], res["failed"], res["parsed"], res["ok"], res["score"]),
                         (6, 0, "unittest", True, 1.0))
        res = G.score(self.workdir("worse", CALC_WORSE), self.task)
        self.assertEqual((res["passed"], res["failed"], res["ok"]), (4, 2, False))   # add(-1,1) and add(-2,-3)
        self.assertAlmostEqual(res["score"], 4 / 6, places=5)
        self.assertNotIn("output", res)
        self.assertEqual(len(res["output_sha256"]), 64)

    def test_patch_that_does_not_apply(self):
        w = self.workdir("pre", CALC_FIX)
        with open(os.path.join(w, "tests", "test_patched.py"), "w") as f:
            f.write("# the agent wrote this file\n")
        with self.assertRaises(BrainError):
            G.score(w, self.task)

    def test_pytest_output_and_exit_fallback(self):
        w = self.workdir("py", CALC_FIX)
        t = dict(self.task, hidden_tests=["hidden/tests/test_hidden.py"],
                 test_command=[PY, "-c", "print('==== 2 passed, 1 failed, 1 error in 0.10s ===='); raise SystemExit(1)"])
        res = G.score(w, t)
        self.assertEqual((res["passed"], res["failed"], res["parsed"]), (2, 2, "pytest"))
        t["test_command"] = [PY, "-c", "raise SystemExit(3)"]
        res = G.score(w, t)
        self.assertEqual((res["passed"], res["failed"], res["parsed"], res["exit"]), (0, 1, "exit", 3))
        self.assertEqual(G.parse_counts("Ran 5 tests in 0.1s\n\nOK (skipped=2)\n", 0), (3, 0, "unittest"))
        self.assertEqual(G.parse_counts("Ran 4 tests in 0.1s\n\nFAILED (failures=1, errors=1)\n", 1),
                         (2, 2, "unittest"))

    def test_env_is_scrubbed(self):
        w = self.workdir("env", CALC_FIX)
        probe = ("import os, sys\nleak = [k for k in ('GITLAB_TOKEN', 'MY_SECRET', 'AWS_PROFILE', 'BRAIN_USER') "
                 "if k in os.environ]\nprint('Ran 1 test in 0.0s')\nprint('FAILED (failures=1)' if leak else 'OK')\n"
                 "sys.exit(1 if leak else 0)\n")
        t = dict(self.task, hidden_tests=["hidden/tests/test_hidden.py"], test_command=[PY, "-c", probe])
        env = {"GITLAB_TOKEN": "x", "MY_SECRET": "y", "AWS_PROFILE": "z", "BRAIN_USER": "alice"}
        with mock.patch.dict(os.environ, env):
            res = G.score(w, t)
            self.assertTrue(res["ok"], res)
            kept = G.scrubbed_env()
        self.assertIn("PATH", kept)
        self.assertFalse(set(env) & set(kept))


class PlanRunSummarize(unittest.TestCase):
    def test_plan(self):
        tasks = [{"id": "B", "base_sha": "b" * 40}, {"id": "A", "base_sha": "a" * 40}]
        jobs = G.plan(tasks, G.VARIANTS, 3)
        self.assertEqual(len(jobs), 2 * 4 * 3)
        self.assertEqual(jobs[0]["job_id"], "A/full/1")
        self.assertEqual(jobs[3]["job_id"], "A/no-IMPROVEMENT/1")
        self.assertTrue(all(j["eval_oracle"] for j in jobs))
        self.assertEqual(len({j["job_id"] for j in jobs}), len(jobs))
        self.assertEqual(jobs, G.plan(list(reversed(tasks)), G.VARIANTS, 3))
        for bad in (["full", "full"], ["no-PROJECT"], []):
            with self.assertRaises(BrainError):
                G.plan(tasks, bad, 1)
        with self.assertRaises(BrainError):
            G.plan(tasks, ["full"], 0)

    def test_run_with_fake_runner_and_summary(self):
        d, repo, base = make_repo(self)
        task = G.load_task(make_task(d, "G-1", base))
        jobs = G.plan([task], ["full", "no-TECH", "no-PRODUCT"], 2)
        jobs.append(dict(jobs[0], job_id="G-1/full/boom", repeat=99))

        def runner(job):
            if job["repeat"] == 99:
                raise RuntimeError("slot died\nwith a traceback line")
            w = os.path.join(d, "work", job["job_id"].replace("/", "_"))
            G.snapshot(repo, job["base_sha"], w, variant=job["variant"])
            # the fake agent "reads" the Brain: without TECH.md it writes a worse fix
            knows = os.path.exists(os.path.join(w, "brain", "TECH.md"))
            with open(os.path.join(w, "calc.py"), "w") as f:
                f.write(CALC_FIX if knows else CALC_WORSE)
            park = {"kind": "acceptance", "lean_request": "RELEASE 1a2b3c4d", "options": []}
            return {"workdir": w, "oracle_answers": [G.oracle_answer(task, park)], "credits": 12.5}

        results = G.run(jobs, runner, [task])
        self.assertEqual(len(results), 7)
        boom = results[-1]
        self.assertIsNone(boom["score"])
        self.assertEqual(boom["error"], "RuntimeError: slot died with a traceback line")
        self.assertTrue(all(r["eval_oracle"] for r in results))
        self.assertTrue(all(a["eval_oracle"] for r in results for a in r["oracle_answers"]))
        scored = {r["job_id"]: r["score"] for r in results if r["score"] is not None}
        self.assertEqual(scored["G-1/full/1"], 1.0)
        self.assertAlmostEqual(scored["G-1/no-TECH/2"], 4 / 6, places=5)
        self.assertEqual(results[0]["credits"], 12.5)

        s = G.summarize(results, tasks=[task])
        self.assertEqual(s["errors"], 1)
        self.assertEqual(s["per_task"]["G-1"]["full"]["n"], 2)        # the failed job is not a 0
        self.assertEqual(s["per_task"]["G-1"]["full"]["errors"], 1)
        rows = {c["variant"]: c for c in s["comparisons"]}
        self.assertEqual(rows["no-TECH"]["verdict"], "drop")
        self.assertAlmostEqual(rows["no-TECH"]["chance"], 1 / 6, places=5)
        self.assertEqual(rows["no-PRODUCT"]["verdict"], "no drop")
        self.assertEqual(s["files"], {"TECH": "load-bearing", "PRODUCT": "never dropped"})
        self.assertEqual(s["withdraw_candidates"], [])                # T-PAY-1-1 carried weight
        self.assertTrue(s["eval_oracle"])
        self.assertEqual(s["oracle_answers_used"], 6)
        text = G.report(s)
        self.assertIn("这块知识在承重", text)
        self.assertIn("eval_oracle", text)

    def test_drop_rule(self):
        def res(task, variant, scores):
            return [{"task": task, "variant": variant, "score": x} for x in scores]
        results = (res("A", "full", [1.0, 0.9, 1.0]) + res("A", "no-TECH", [0.5, 0.6, 0.4])     # separated
                   + res("A", "no-PRODUCT", [0.95, 0.85, 1.0])                                  # overlap
                   + res("A", "no-IMPROVEMENT", [0.8])                                          # 1 run only
                   + res("B", "full", [1.0, 1.0, 1.0]) + res("B", "no-TECH", [0.98, 0.99, 0.97])  # tiny drop
                   + res("B", "no-PRODUCT", [1.0, 1.0, 1.0])
                   + res("C", "no-TECH", [0.1, 0.2]))                                           # no full
        tasks = [{"id": "A", "must_use": ["P-PAY-1-1", "T-PAY-1-1", "I-PAY-1-1", "J-PAY-1-1"]},
                 {"id": "B", "must_use": ["P-PAY-1-1", "T-PAY-2-1"]}]
        s = G.summarize(results, tasks=tasks)
        v = {(c["task"], c["variant"]): c for c in s["comparisons"]}
        self.assertEqual(v[("A", "no-TECH")]["verdict"], "drop")
        self.assertAlmostEqual(v[("A", "no-TECH")]["chance"], 0.05)
        self.assertEqual(v[("A", "no-PRODUCT")]["verdict"], "no drop")
        self.assertFalse(v[("A", "no-PRODUCT")]["separated"])
        self.assertEqual(v[("A", "no-IMPROVEMENT")]["verdict"], "not enough runs")
        self.assertEqual(v[("B", "no-TECH")]["verdict"], "no drop")          # separated but below min_drop
        self.assertTrue(v[("B", "no-TECH")]["separated"])
        self.assertEqual(v[("C", "no-TECH")]["verdict"], "no full baseline")
        self.assertEqual([w["id"] for w in s["withdraw_candidates"]], ["P-PAY-1-1", "T-PAY-2-1"])
        self.assertEqual([w["id"] for w in s["must_use_not_judged"]], ["I-PAY-1-1"])   # J: no variant ablates it
        self.assertEqual(s["by_variant"]["no-TECH"]["real_drops"], 1)
        self.assertEqual(s["files"]["PRODUCT"], "never dropped")
        self.assertEqual(s["files"]["IMPROVEMENT"], "not judged")
        self.assertIn("5%", s["rule"])
        self.assertIn("次数不够", G.report(s))


class Oracle(unittest.TestCase):
    TASK = {"id": "G-1", "oracle_answers": {"RELEASE": "approve", "acceptance": "reject", "re:退款.*上限": "B",
                                            "单号": "A", "clarify": "A"}}

    def test_matching_order(self):
        o = G.oracle_answer
        a = o(self.TASK, {"kind": "acceptance", "lean_request": "RELEASE 1a2b3c4d", "options": []})
        self.assertEqual((a["choice"], a["matched"], a["eval_oracle"]), ("approve", "RELEASE", True))
        t2 = {"id": "G-2", "oracle_answers": {"re:退款.*上限": "B", "单号": "A"}}
        opts = [{"id": "A", "text": "x"}, {"id": "B", "text": "y"}]
        a = o(t2, {"kind": "clarify", "question": "退款金额的上限按哪条算？", "options": opts})
        self.assertEqual((a["choice"], a["matched"]), ("B", "re:退款.*上限"))
        a = o(t2, {"kind": "clarify", "question": "单号格式？", "options": opts})
        self.assertEqual((a["choice"], a["matched"], a["task"]), ("A", "单号", "G-2"))
        self.assertIsNone(o(t2, {"kind": "design", "question": "用哪个库？", "options": opts}))
        a = o(self.TASK, {"kind": "clarify", "question": "单号格式？", "options": opts})
        self.assertEqual(a["matched"], "clarify")                     # the park kind key wins over text
        with self.assertRaises(BrainError):
            o({"id": "G-3", "oracle_answers": {"design": "C"}}, {"kind": "design", "question": "?", "options": opts})
        with self.assertRaises(BrainError):
            o({"id": "G-3", "oracle_answers": {"DESIGN": "A"}},
              {"kind": "design", "lean_request": "DESIGN 1a2b3c4d", "options": []})


class Cli(unittest.TestCase):
    def test_plan_snapshot_score_summarize(self):
        d, repo, base = make_repo(self)
        make_task(d, "G-1", base)
        rc, out = run_cli(repo, "golden", "plan", "--tasks", os.path.join(d, "tasks"), "--variants",
                          "full,no-TECH", "--repeats", "2", "--json")
        self.assertEqual(rc, 0, out)
        jobs = json.loads(out)
        self.assertEqual(len(jobs), 4)
        rc, out = run_cli(repo, "golden", "plan", "--tasks", d)
        self.assertEqual(rc, 0, out)
        self.assertIn("1 道题 × 4 组 × 3 次 = 12 个 run", out)
        snap = os.path.join(d, "snap")
        rc, out = run_cli(repo, "golden", "snapshot", os.path.join(d, "tasks", "G-1"), "--out", snap,
                          "--variant", "no-TECH", "--json")
        self.assertEqual(rc, 0, out)
        man = json.loads(out)
        self.assertEqual((man["task"], man["variant"]), ("G-1", "no-TECH"))
        self.assertNotIn("TECH.md", man["brain_files"])
        self.assertFalse(os.path.exists(os.path.join(snap, ".git")))
        with open(os.path.join(snap, "calc.py"), "w") as f:
            f.write(CALC_FIX)
        rc, out = run_cli(repo, "golden", "score", os.path.join(d, "tasks", "G-1"), snap)
        self.assertEqual(rc, 0, out)
        self.assertIn("通过 6，失败 0", out)
        rc, out = run_cli(repo, "golden", "snapshot", os.path.join(d, "tasks", "G-1"), "--out", snap)
        self.assertEqual(rc, 2)                                        # not empty: BrainError, exit 2
        rpath = os.path.join(d, "results.json")
        with open(rpath, "w") as f:
            json.dump({"results": [{"task": "G-1", "variant": "full", "score": 1.0},
                                   {"task": "G-1", "variant": "full", "score": 1.0},
                                   {"task": "G-1", "variant": "no-TECH", "score": 0.5},
                                   {"task": "G-1", "variant": "no-TECH", "score": 0.4}]}, f)
        rc, out = run_cli(repo, "golden", "summarize", rpath, "--tasks", os.path.join(d, "tasks"))
        self.assertEqual(rc, 0, out)
        self.assertIn("G-1 no-TECH", out)
        rc, out = run_cli(repo, "golden", "summarize", rpath, "--json")
        self.assertEqual(json.loads(out)["by_variant"]["no-TECH"]["real_drops"], 1)


if __name__ == "__main__":
    unittest.main()
