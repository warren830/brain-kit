"""Gaps -> the next requirement (brain-gap blocks in reflect), the reflector session, rejected review items."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import claims, cli, config, intents, reflect  # noqa: E402
from brainkit import entries as E  # noqa: E402
from tests.helpers import ROOT, commit_file, git_repo, sh, tmpdir  # noqa: E402

GAPS = """
```brain-gap
title: 退款对账没有自动测试 #2
kind: test
refs: AC-2, D-4, src/pay/reconcile.py
---
对账只在验收时手工核对过（D-4）。要一个每天跑的测试：两边金额和笔数一致。
```
```brain-gap
title: 没人知道清算批次的切点
kind: knowledge
---
run 里问了两次（D-2、D-5）才得到答案；Brain 里没有。
```
"""
PROPOSAL = """
## Brain proposals
```brain-proposal
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


def claim_keys(repo):
    return [r.rsplit("/", 1)[1] for r in
            sh("git", "-C", repo, "for-each-ref", "--format=%(refname)", "refs/heads/claim/").stdout.split()]


class GapBlocks(unittest.TestCase):
    def test_parse(self):
        gaps, probs = reflect.parse_gaps(PROPOSAL + GAPS)
        self.assertEqual(probs, [])
        self.assertEqual([(g.kind, g.title) for g in gaps], [("test", "退款对账没有自动测试 #2"),
                                                             ("knowledge", "没人知道清算批次的切点")])
        self.assertEqual(gaps[0].refs, ["AC-2", "D-4", "src/pay/reconcile.py"])
        self.assertEqual(reflect.parse(PROPOSAL + GAPS)[1], [])            # proposals parse as before
        self.assertEqual(len(reflect.parse(PROPOSAL + GAPS)[0]), 1)
        self.assertIn("brain-gap block is not closed", reflect.parse_gaps("```brain-gap\ntitle: x\n")[1][0])

    def test_plan_problems(self):
        repo = git_repo(os.path.join(tmpdir(self), "r"))
        commit_file(repo, "README", "x\n")
        cfg = dict(config.DEFAULTS, req_prefix="OMS")
        bad = reflect.parse_gaps("""```brain-gap
title: x
kind: vibes
owner: someone
---
联系 someone@example.com
```
```brain-gap
title: 空的
kind: other
---
```
""")[0]
        _, probs = reflect.plan_gaps(repo, cfg, bad, "OMS-3")
        text = " | ".join(probs)
        for want in ("kind must be one of", "unknown fields owner", "personal data", "body: 1-"):
            self.assertIn(want, text)


class ReflectGaps(unittest.TestCase):
    def setUp(self):
        os.environ.pop("GITLAB_TOKEN", None)
        os.environ["BRAIN_USER"] = "alice"
        self.addCleanup(os.environ.pop, "BRAIN_USER", None)
        self.repo = git_repo(os.path.join(tmpdir(self), "r"))
        commit_file(self.repo, ".brain/kit.json", json.dumps({"version": 1, "req_prefix": "OMS"}))
        commit_file(self.repo, "brain/TECH.md", "# TECH\n")
        self.base = sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip()
        claims.claim(self.repo, "OMS", "alice", "退款", key="OMS-3")
        sh("git", "-C", self.repo, "switch", "-q", "-c", "ai/alice/OMS-3-x")
        commit_file(self.repo, "runs/OMS-3/run.json", json.dumps({"key": "OMS-3", "intent": "261108-oms-3-x",
                                                                  "brain_commit": self.base}))

    def decisions(self, text):
        commit_file(self.repo, "aidlc-docs/261108-oms-3-x/decisions.md", "# Decisions\n" + text)

    def test_preview_apply_and_idempotent(self):
        self.decisions(PROPOSAL + GAPS)
        rc, out = run_cli(self.repo, "reflect")
        self.assertEqual(rc, 0, out)
        self.assertIn("1 个提案、2 个缺口", out)
        self.assertIn("退款对账没有自动测试 #2：新需求", out)
        self.assertIn("并登记 2 个缺口", out)
        self.assertEqual(claim_keys(self.repo), ["OMS-3"])                    # the preview files nothing
        wt = os.path.join(tmpdir(self), "wt")
        rc, out = run_cli(self.repo, "reflect", "--apply", "--worktree", wt)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(claim_keys(self.repo)), ["OMS-3", "OMS-4", "OMS-5"])
        gap = claims.read(self.repo, "OMS-4")
        self.assertEqual((gap["source"], gap["gap_kind"], gap["found_in"], gap["owner"], gap["refs"][0]),
                         ("gap", "test", "OMS-3", "alice", "AC-2"))
        self.assertEqual(intents.doc_problems(gap), [])
        msg = sh("git", "-C", wt, "log", "-1", "--format=%B").stdout
        self.assertIn("缺口 → 下一个需求", msg)
        self.assertIn("OMS-4 · test · 退款对账没有自动测试 #2", msg)
        self.assertIn("T-OMS-3-1", msg)
        cfg = config.load(self.repo)
        self.assertEqual([d["key"] for d in intents.list_open(self.repo, cfg)], ["OMS-4", "OMS-5"])
        # after the Brain MR merged, reflecting again files nothing and opens nothing
        sh("git", "-C", self.repo, "switch", "-q", "main")
        sh("git", "-C", self.repo, "merge", "-q", "--no-edit", "brain/OMS-3-reflect-1")
        sh("git", "-C", self.repo, "merge", "-q", "--no-edit", "ai/alice/OMS-3-x")
        rc, out = run_cli(self.repo, "reflect", "--key", "OMS-3", "--ref", "main", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("已登记为 OMS-4，跳过", out)
        self.assertIn("没有新东西", out)
        self.assertEqual(sorted(claim_keys(self.repo)), ["OMS-3", "OMS-4", "OMS-5"])

    def test_only_gaps_no_brain_branch(self):
        self.decisions(GAPS)
        rc, out = run_cli(self.repo, "reflect")
        self.assertIn("加 --apply 登记这 2 个缺口", out)
        rc, out = run_cli(self.repo, "reflect", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("登记为 OMS-4", out)
        self.assertEqual(sh("git", "-C", self.repo, "branch", "--list", "brain/*").stdout.strip(), "")

    def test_bad_gap_refuses_everything(self):
        self.decisions(PROPOSAL + GAPS + "```brain-gap\ntitle: 找 [人名-1]\nkind: knowledge\n---\nx\n```\n")
        rc, out = run_cli(self.repo, "reflect", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("masked placeholder", out)
        self.assertEqual(claim_keys(self.repo), ["OMS-3"])
        self.assertEqual(sh("git", "-C", self.repo, "branch", "--list", "brain/*").stdout.strip(), "")

    def test_from_reflector_file(self):
        self.decisions("no blocks here\n")
        f = os.path.join(tmpdir(self), "reflect-proposals.md")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(GAPS)
        sh("git", "-C", self.repo, "switch", "-q", "main")
        rc, out = run_cli(self.repo, "reflect", "--key", "OMS-3", "--from", f, "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(claim_keys(self.repo)), ["OMS-3", "OMS-4", "OMS-5"])

    def test_draft_prints_and_changes_nothing(self):
        self.decisions(PROPOSAL)
        before = sh("git", "-C", self.repo, "worktree", "list").stdout
        rc, out = run_cli(self.repo, "reflect", "--draft", "OMS-3")
        self.assertEqual(rc, 0, out)
        self.assertIn("worktree add --detach", out)
        self.assertIn("ai/alice/OMS-3-x", out)                     # not merged yet: the run branch
        self.assertIn("- intent: 261108-oms-3-x", out)
        self.assertIn("brain reflect --key OMS-3 --from", out)
        self.assertIn("# brain-reflector", out)                    # the whole prompt rides in the message
        self.assertIn("brain trace <KEY> --json", out)
        self.assertEqual(sh("git", "-C", self.repo, "worktree", "list").stdout, before)
        sh("git", "-C", self.repo, "switch", "-q", "main")
        sh("git", "-C", self.repo, "merge", "-q", "--no-edit", "ai/alice/OMS-3-x")
        rc, out = run_cli(self.repo, "reflect", "--draft", "OMS-3")
        self.assertIn("- ref: main", out)                          # merged: main
        self.assertEqual(run_cli(self.repo, "reflect", "--draft", "OMS-9")[0], 2)
        self.assertEqual(run_cli(self.repo, "reflect", "--draft", "OMS-3", "--apply")[0], 2)


class RejectedItems(unittest.TestCase):
    def test_each_rejected_item_needs_a_correction(self):
        d = tmpdir(self)
        os.makedirs(os.path.join(d, "RUN-OMS-3-1"))
        with open(os.path.join(d, "RUN-OMS-3-1", "V-3c4d5e6f70.json"), "w") as f:
            json.dump({"voucher": {"kind": "answer", "voucher_id": "V-3c4d5e6f70", "decision": "D-9",
                                   "choice": "reject", "review_items": ["D-3", "A-1", "A-2"],
                                   "rejected_items": ["D-3", "A-1"]}, "sig": "x"}, f)
        props = reflect.parse("""```brain-proposal
file: PRODUCT
type: correction
src: A-1, V-3c4d5e6f70
---
旧订单没有退款单号时也显示退款按钮。
```
""")[0]
        lost = reflect.rejected_without_correction(d, "OMS-3", props)
        self.assertEqual(len(lost), 1)
        self.assertIn("D-3 in D-9", lost[0])
        # a correction for the whole voucher id alone does not cover the items one by one
        props2 = reflect.parse("```brain-proposal\nfile: PRODUCT\ntype: correction\nsrc: V-3c4d5e6f70\n---\nx\n```\n")[0]
        self.assertEqual(len(reflect.rejected_without_correction(d, "OMS-3", props2)), 2)
        self.assertTrue(E.SRC_ITEM_RE.match("A-1") and E.SRC_ITEM_RE.match("D-3"))   # item ids are valid sources


class ReflectorSpec(unittest.TestCase):
    def test_agent_and_prompt(self):
        with open(os.path.join(ROOT, "remote", "agents", "brain-reflector.json"), encoding="utf-8") as f:
            spec = json.load(f)
        self.assertEqual(spec["name"], "brain-reflector")
        self.assertEqual(spec["model"], "claude-opus-5.5")
        self.assertEqual(spec["tools"], ["read", "write", "shell"])
        self.assertTrue(spec["prompt"].endswith("/remote/prompts/brain-reflector.md"))
        self.assertIn("~/.kiro/agents/brain-reflector.json", spec["_notes"])
        self.assertNotIn("hooks", spec)
        forbidden = config.DEFAULTS["forbidden_tools"]
        self.assertFalse(set(spec["tools"] + spec["allowedTools"]) & set(forbidden))
        with open(reflect.REFLECTOR_PROMPT, encoding="utf-8") as f:
            prompt = f.read()
        for want in ("brain trace <KEY> --json", "decisions.md", "aidlc-state.md", "audit", "park.json",
                     "V-*.json", "runs/<KEY>/reflect-proposals.md", "```brain-gap", "Never edit `brain/`"):
            self.assertIn(want, prompt)


if __name__ == "__main__":
    unittest.main()
