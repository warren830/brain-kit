"""Round-1 review findings on `brain reflect` and `brain usage` (R1-73, 78, 79, 80, 81, 83, 84): one test each."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import brainlint, cli  # noqa: E402
from brainkit import entries as E  # noqa: E402
from tests.helpers import commit_file, git_repo, sh, tmpdir  # noqa: E402

TECH = "# TECH\n\n<!-- id:T-OMS-1-1 type:pitfall side:builder rel:new src:D01#S021 -->\n退款单号是 RF + 9 位。\n"
ONE = """```brain-proposal
file: TECH
type: guideline
src: D-1, RUN-OMS-3-1
---
金额一律用十进制计算，不用浮点。
```
"""


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


def isolate_home(test):
    """`brain reflect --apply` makes worktrees under ~/.brain/worktrees: a temp HOME, never the real one."""
    old = os.environ.get("HOME")
    os.environ["HOME"] = tmpdir(test)
    test.addCleanup(lambda: os.environ.__setitem__("HOME", old) if old is not None else os.environ.pop("HOME", None))
    for k in ("GITLAB_TOKEN", "BRAIN_USER"):
        if k in os.environ:
            test.addCleanup(os.environ.__setitem__, k, os.environ.pop(k))
    return os.environ["HOME"]


def project(path, decisions):
    """A repo with a Brain on main and a run branch ai/alice/OMS-3-x whose decisions.md holds `decisions`."""
    repo = git_repo(path)
    commit_file(repo, ".brain/kit.json", json.dumps({"version": 1, "req_prefix": "OMS"}))
    commit_file(repo, "brain/TECH.md", TECH)
    commit_file(repo, "brain/PRODUCT.md", "# PRODUCT\n")
    base = sh("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()
    sh("git", "-C", repo, "switch", "-q", "-c", "ai/alice/OMS-3-x")
    commit_file(repo, "runs/OMS-3/run.json", json.dumps({"key": "OMS-3", "intent": "261108-oms-3-x",
                                                         "brain_commit": base}))
    commit_file(repo, "aidlc-docs/261108-oms-3-x/decisions.md", "# Decisions\n\n" + decisions)
    return repo


def brain_branches(repo):
    return sh("git", "-C", repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/brain/").stdout.split()


class Reflect(unittest.TestCase):
    def setUp(self):
        self.home = isolate_home(self)
        self.d = tmpdir(self)

    def test_r1_78_the_same_proposal_twice_is_written_once(self):
        repo = project(os.path.join(self.d, "r"), ONE + "\n" + ONE)
        wt = os.path.join(self.d, "wt")
        rc, out = run_cli(repo, "reflect", "--apply", "--worktree", wt)
        self.assertEqual(rc, 0, out)
        main = E.Brain.load(os.path.join(wt, "brain")).main()
        self.assertEqual(sorted(main), ["T-OMS-1-1", "T-OMS-3-1"], out)
        self.assertIn("same proposal as proposal at line", out)

    def test_r1_81_apply_after_no_commit_does_not_write_again(self):
        repo = project(os.path.join(self.d, "r"), ONE)
        wt = os.path.join(self.d, "wt")
        rc, out = run_cli(repo, "reflect", "--apply", "--no-commit", "--worktree", wt)
        self.assertEqual(rc, 0, out)
        self.assertIn("T-OMS-3-1", E.Brain.load(os.path.join(wt, "brain")).main())
        rc, out = run_cli(repo, "reflect", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(brain_branches(repo), ["brain/OMS-3-reflect-1"], out)
        self.assertIn(wt, out)

    def test_r1_73_rerun_after_the_id_was_taken_writes_again(self):
        repo = project(os.path.join(self.d, "r"), ONE)
        rc, out = run_cli(repo, "reflect", "--apply", "--worktree", os.path.join(self.d, "wt1"))
        self.assertEqual(rc, 0, out)
        # another MR, made before this one was visible, merges first and takes T-OMS-3-1
        sh("git", "-C", repo, "switch", "-q", "main")
        b = E.Brain.load(os.path.join(repo, "brain"))
        self.assertEqual(b.new("TECH", "pitfall", "对账按自然日切。", "OMS-3", "D-9"), "T-OMS-3-1")
        b.save()
        sh("git", "-C", repo, "commit", "-q", "-am", "other MR")
        sh("git", "-C", repo, "switch", "-q", "ai/alice/OMS-3-x")

        def ci_errors(branch):
            mb = sh("git", "-C", repo, "merge-base", "main", branch).stdout.strip()
            return brainlint.check(E.Brain.from_git(repo, branch), base=E.Brain.from_git(repo, mb),
                                   target=E.Brain.from_git(repo, "main"))[0]
        self.assertIn("taken on the target branch", " ".join(ci_errors("brain/OMS-3-reflect-1")))
        rc, out = run_cli(repo, "reflect", "--apply", "--worktree", os.path.join(self.d, "wt2"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(brain_branches(repo), ["brain/OMS-3-reflect-1", "brain/OMS-3-reflect-2"], out)
        self.assertIn("brain/OMS-3-reflect-1", out)
        self.assertEqual(ci_errors("brain/OMS-3-reflect-2"), [])
        new = E.Brain.from_git(repo, "brain/OMS-3-reflect-2").main()
        self.assertEqual([e.text() for eid, (f, e) in new.items() if eid not in ("T-OMS-1-1", "T-OMS-3-1")],
                         ["金额一律用十进制计算，不用浮点。"])
        rc, out = run_cli(repo, "reflect", "--apply", "--worktree", os.path.join(self.d, "wt3"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(brain_branches(repo)), 2, out)


if __name__ == "__main__":
    unittest.main()
