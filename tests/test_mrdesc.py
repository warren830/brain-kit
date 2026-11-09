"""MR 变更说明 (brainkit/mrdesc.py, `brain mr-desc`): built from a run's records through a real AI-DLC Lean install.

One full run (requirements, design, plan with its Tests table, verify, decisions from agent / owner / brain / a
person, an assumption, a risk-path change, review, release, proposals and a gap), and one just started. Every
section is checked against the records, and nothing a record does not hold may appear."""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import cli, config, mrdesc, sign  # noqa: E402
from brainkit.common import load_json, write_json  # noqa: E402
from tests.helpers import LEAN_SRC, commit_file, git_repo, keypair, sh, tmpdir  # noqa: E402

KEY = "PAY-1"
BRANCH = "ai/alice/PAY-1-refund"
VOUCHER = "V-" + hashlib.sha256(b"mr-desc test voucher").hexdigest()[:10]
PERSON = "P-" + "ABCD"
TECH = ("\n<!-- id:T-PAY-1-1 type:pitfall side:builder rel:new src:D-1 -->\n退款单号是 RF + 9 位。\n")
REQUIREMENTS = """# Requirements: 退款

AI-DLC stage: Inception · Requirements Analysis · Intent: {intent}
Size: large

## Goal
14 天内的订单可以在线退款，钱原路退回。客服不用再手工退款。

## Not doing
- 部分退款
- 跨币种退款

## Acceptance criteria
| ID | Given / when | Then (expected) |
|---|---|---|
| AC-1 | 3 天前的订单申请退款 | 退款成功，状态 refunded |
| AC-2 | 20 天前的订单申请退款 | 403 refund_window |
| AC-3 | 第 14 天的订单申请退款 | 退款成功 |

## Non-functional targets
- none beyond today's system

## Assumptions
- A-1: 只有一种币种
- A-2:

## Questions and answers
1. Q: 要不要部分退款？
   A: 不要
"""
PLAN = """# Code Generation Plan: app (退款)

## Unit context
AC-1..3

## Steps
- [x] 1. Walking skeleton: refund endpoint · covers: AC-1 · done when: test passes
- [x] 2. Window check · covers: AC-2, AC-3 · done when: tests pass

## Tests
| AC / BR | Test (file::name) | Kind |
|---|---|---|
| AC-1 | tests/test_refund.py::test_refund_ok | unit |
| AC-2 | tests/test_window.py::test_too_old | unit |
"""
DECISIONS = """# Decisions: 退款

| ID | Date | Stage | Decision | Why | Who |
|---|---|---|---|---|---|
| D-1 | 2026-11-09 | requirements | 退款单号按 RF + 9 位 | PRD 和样例不一致，按 T-PAY-1-1 | brain T-PAY-1-1 |
| D-2 | 2026-11-09 | code-generation | AC-3 的第 14 天按自然日算，当天还能退 | 需求没写；最常见的做法 | agent |
| D-3 | 2026-11-09 | requirements | 不做部分退款 | 问过 carol@corp.example，见 /Users/alice/notes.md | owner |
| D-4 | 2026-11-09 | design | 退款走原支付渠道 | 产品在 MR 上答的 | {person} {voucher} |
| D-5 | 2026-11-09 | code-generation | 日志不记卡号 | 张三说的 | 张三 |

## Brain proposals

```brain-proposal
file: TECH
type: pitfall
rel: new
src: D-2, AC-3
---
退款窗口按自然日算，第 14 天当天还能退。
```

```brain-gap
title: 退款对账没有自动测试
kind: test
refs: AC-2
---
对账报表没有测试，下个需求补上。
```
"""
REVIEW = """# Review

| Finding | Severity | File:line | Fix |
|---|---|---|---|
| 金额用 float | medium | src/pay/refund.py:3 | 改成 Decimal |
| 日志太多 | low | src/pay/refund.py:9 | 删掉 |

No blocking issues left after the fixes.
"""


def lean(repo, *args, check=True):
    return sh(sys.executable, ".kiro/aidlc-lean/aidlc_lean.py", *args, cwd=repo, check=check)


def approve(repo, line):
    """The person types an approval line: Lean's prompt hook records it (as in a KiroCrew session)."""
    p = subprocess.run([sys.executable, ".kiro/aidlc-lean/aidlc_lean.py", "hook", "UserPromptSubmit"], cwd=repo,
                       text=True, capture_output=True, input=json.dumps({"prompt": line, "session_id": "s1"}))
    assert p.returncode == 0, p.stdout + p.stderr


def request(repo, kind):
    p = lean(repo, "request", kind, check=False)
    m = re.search(r"approve %s ([0-9a-f]{8})" % kind, p.stdout + p.stderr)
    assert m, p.stdout + p.stderr
    return m.group(1)


def write(repo, rel, text):
    p = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def commit_all(repo, msg):
    sh("git", "-C", repo, "add", "-A")
    sh("git", "-C", repo, "commit", "-q", "-m", msg)


def brain(repo, *args):
    out, err = io.StringIO(), io.StringIO()
    cwd = os.getcwd()
    os.chdir(repo)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(args))
    finally:
        os.chdir(cwd)
    return rc, out.getvalue(), err.getvalue()


def new_project(d, exclude_runs=True):
    """A project with brain-kit and Lean installed on main, a Brain entry, risk paths and a passing check."""
    repo = git_repo(os.path.join(d, "proj"))
    commit_file(repo, "src/app.py", "print('hi')\n", "init")
    rc, out, err = brain(repo, "install", repo, "--lean", LEAN_SRC, "--team", "pay", "--prefix", "PAY")
    assert rc == 0, out + err
    lj = os.path.join(repo, "aidlc-docs", "lean.json")
    cfg = load_json(lj)
    cfg["checks"] = ["true"]
    if exclude_runs:
        cfg["fingerprint_exclude"] = ["aidlc-docs/**", ".kiro/**", "runs/**"]
    write_json(lj, cfg)
    write(repo, "gates/risk-paths", "# needs a design approval\nsrc/pay/**\n")
    with open(os.path.join(repo, "brain", "TECH.md"), "a", encoding="utf-8") as f:
        f.write(TECH)
    commit_all(repo, "install kit")
    rc, out, err = brain(repo, "start", "refund", "--key", KEY)
    assert rc == 0, out + err
    return repo, load_json(os.path.join(repo, "runs", KEY, "run.json"))["intent"]


def full_run(repo, intent):
    """Requirements -> design (approved through the bridge's channel) -> plan -> code -> verify -> review -> release."""
    docs = "aidlc-docs/%s" % intent
    write(repo, docs + "/inception/requirements/requirements.md", REQUIREMENTS.format(intent=intent))
    lean(repo, "stage", "done", "requirements", "--note", "AC-1..3, A-1")
    approve(repo, "approve REQUIREMENTS %s" % request(repo, "REQUIREMENTS"))
    lean(repo, "stage", "start", "application-design")
    write(repo, docs + "/inception/application-design/application-design.md",
          "# Application Design: 退款\n\n退款服务放在 src/pay/refund.py，调用原支付渠道的退款接口。\n")
    lean(repo, "stage", "done", "application-design", "--note", "one service")
    design = request(repo, "DESIGN")
    approve(repo, "approve DESIGN %s (via gitlab %s %s)" % (design, PERSON, VOUCHER))
    lean(repo, "stage", "start", "code-generation", "--unit", "app")
    write(repo, docs + "/construction/plans/app-code-generation-plan.md", PLAN)
    write(repo, "src/pay/refund.py", "def refund(order):\n    return 'refunded'\n")
    write(repo, "tests/test_refund.py", "def test_refund_ok():\n    pass\n\n\ndef test_too_old():\n    pass\n")
    write(repo, docs + "/decisions.md", DECISIONS.format(person=PERSON, voucher=VOUCHER))
    lean(repo, "stage", "done", "code-generation", "--unit", "app", "--note", "refund endpoint")
    commit_all(repo, "PAY-1: refund")
    out = lean(repo, "verify").stdout
    assert "verify: PASS" in out, out
    write(repo, docs + "/construction/build-and-test/review.md", REVIEW)
    commit_all(repo, "PAY-1: verify and review")
    p = lean(repo, "release", check=False)
    assert p.returncode == 0, p.stdout + p.stderr
    rel = os.path.join(repo, docs, "operations", "release.md")
    with open(rel, encoding="utf-8") as f:
        text = f.read()
    with open(rel, "w", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n- 对账报表留给下一个需求\n")
    release = request(repo, "RELEASE")
    commit_all(repo, "PAY-1: release record")
    return design, release


def verify_fp(repo, intent):
    fps = []
    for name in sorted(os.listdir(os.path.join(repo, "aidlc-docs", "audit"))):
        with open(os.path.join(repo, "aidlc-docs", "audit", name), encoding="utf-8") as f:
            fps += [json.loads(x)["data"]["fingerprint"] for x in f if '"VERIFY"' in x]
    return fps[-1]


def section(text, title):
    m = re.search(r"^### %s\n(.*?)(?=^### |^---$)" % re.escape(title), text, re.M | re.S)
    return m.group(1) if m else None


@unittest.skipUnless(os.path.exists(os.path.join(LEAN_SRC, "install.sh")), "AI-DLC Lean checkout not found")
class MrDescFullRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="brainkit-mrdesc-")
        cls.addClassCleanup(shutil.rmtree, cls.base, True)
        cls.env = mock.patch.dict(os.environ, {"BRAIN_USER": "alice", "HOME": os.path.join(cls.base, "home"),
                                               "BRAIN_INTAKE_DIR": os.path.join(cls.base, "no-intake")})
        cls.env.start()
        cls.addClassCleanup(cls.env.stop)
        for k in ("GITLAB_TOKEN", "BRAIN_ALLOWED_SIGNERS"):
            os.environ.pop(k, None)
        cls.template, cls.intent = new_project(os.path.join(cls.base, "t"))
        cls.design, cls.release = full_run(cls.template, cls.intent)

    def setUp(self):
        self.repo = os.path.join(tmpdir(self), "proj")
        shutil.copytree(self.template, self.repo, symlinks=True)
        self.cfg = config.load(self.repo)

    def build(self, **kw):
        return mrdesc.build(self.repo, self.cfg, KEY, **kw)

    def test_every_section_from_the_records(self):
        credits = {"credits": 12.5, "turns": [{"turn": 1, "credits": 5}, {"turn": 2, "credits": 7.5}]}
        d = self.build(credits=credits)
        text = mrdesc.render(d)
        fp = verify_fp(self.repo, self.intent)
        lines = text.splitlines()
        self.assertLess(len(lines), 120, text)
        self.assertEqual(lines[0], "## PAY-1 · 退款")
        # 一句话: the Goal's first sentence, Size, where the run is and what it waits for
        self.assertIn("**一句话**：14 天内的订单可以在线退款，钱原路退回（规模 large）。现在：Lean 在「发布」进行中，"
                      "在本地开发（还没交接），等 RELEASE 审批。", text)
        did = section(text, "做了什么 / 没做什么")
        self.assertIn("- 目标：14 天内的订单可以在线退款，钱原路退回。客服不用再手工退款。", did)
        self.assertIn("- 不做：部分退款；跨币种退款", did)
        self.assertIn("- 代码计划：app 做完 2/2 步", did)
        # 验收标准和证据: tests from the plan's Tests table, the latest verify with its date and fingerprint
        ev = section(text, "验收标准和证据")
        self.assertIn("最近一次 verify：通过 · ", ev)
        self.assertIn("代码指纹 %s，1 条检查全过，之后代码没变。" % fp[:8], ev)
        self.assertIn("| AC-1 | 3 天前的订单申请退款 → 退款成功，状态 refunded | `tests/test_refund.py::test_refund_ok` "
                      "| 通过 · %s · %s |" % (d["verify"]["at"][:10], fp[:8]), ev)
        self.assertIn("| AC-2 | 20 天前的订单申请退款 → 403 refund_window | `tests/test_window.py::test_too_old`（文件不在） "
                      "| 通过 · %s · %s |" % (d["verify"]["at"][:10], fp[:8]), ev)
        self.assertIn("| AC-3 | 第 14 天的订单申请退款 → 退款成功 | — | 没有对应的测试 |", ev)
        # 改了什么: by top-level folder against the merge base with main; the risk path and its design approval
        ch = section(text, "改了什么")
        base = sh("git", "-C", self.repo, "merge-base", "HEAD", "main").stdout.strip()
        self.assertIn("和 main 的分叉点 %s 比：" % base[:12], ch)
        self.assertRegex(ch, r"\| src \| 1 \| \+2 \| −0 \|")
        self.assertRegex(ch, r"\| tests \| 1 \| \+6 \| −0 \|")
        self.assertRegex(ch, r"\| aidlc-docs（Lean 记录） \| \d+ \|")
        self.assertRegex(ch, r"\| runs（run 记录） \| 1 \|")
        self.assertIn("- 代码文件：`src/pay/refund.py`（+2 −0）、`tests/test_refund.py`（+6 −0）。", ch)
        self.assertIn("- 改到 risk path（main:gates/risk-paths）：`src/pay/refund.py`。", ch)
        self.assertIn("- 设计批准：Lean 记了 DESIGN %s 的批准（经 gitlab %s %s）；签名本机没核对" % (
            self.design, PERSON, VOUCHER), ch)
        self.assertNotIn("没提交", ch)
        # 关键决定: Who in the fixed vocabulary, Brain ids, the agent's own marked; an unknown Who is not shown
        dec = section(text, "关键决定")
        self.assertIn("| D-1 | 退款单号按 RF + 9 位 | PRD 和样例不一致，按 T-PAY-1-1 | brain T-PAY-1-1 | T-PAY-1-1 |", dec)
        self.assertIn("| D-2 | AC-3 的第 14 天按自然日算，当天还能退 | 需求没写；最常见的做法 | agent 自己定的（交付时请过一眼） | — |",
                      dec)
        self.assertIn("| owner（会话里答的） |", dec)
        self.assertIn("| %s（%s） |" % (PERSON, VOUCHER), dec)
        self.assertIn("| D-5 | 日志不记卡号 | 张三说的 | 没按约定写（第 9 行） |", dec)
        self.assertLess(dec.index("| D-1 "), dec.index("| D-5 "))
        # 假设和待确认: A-n (the empty A-2 is not one), release.md Open items
        pend = section(text, "假设和待确认")
        self.assertEqual(pend.strip().splitlines(), ["- A-1：只有一种币种", "- 待办（release.md）：对账报表留给下一个需求"])
        # 审批: what Lean recorded (local, and the channel the bridge's line claimed), and what is still missing
        ap = section(text, "审批")
        self.assertRegex(ap, r"- REQUIREMENTS [0-9a-f]{8} · 2\d{3}-\d\d-\d\d \d\d:\d\d UTC · 本地会话 · 仍有效")
        self.assertIn("- DESIGN %s · " % self.design, ap)
        self.assertIn(" · 经 gitlab %s %s · 仍有效" % (PERSON, VOUCHER), ap)
        self.assertIn("- 还缺：RELEASE（已请求 %s，等产品批）" % self.release, ap)
        rv = section(text, "评审")
        self.assertIn("- review.md：2 条发现（中 1、低 1）", rv)
        self.assertIn("- No blocking issues left after the fixes.", rv)
        br = section(text, "Brain")
        self.assertIn("- 用到的条目：T-PAY-1-1", br)
        self.assertIn("  - TECH pitfall（new）：退款窗口按自然日算，第 14 天当天还能退。", br)
        self.assertIn("- 缺口（reflect 后成为新需求）：退款对账没有自动测试（test）", br)
        how = section(text, "怎么审")
        self.assertEqual(how.strip().splitlines(), [
            "1. 改到了 risk path src/pay/refund.py：Lean 记了 DESIGN %s 的批准，签名由 CI 核对" % self.design,
            "2. 先看 AC-3：它的边界是 agent 自己定的（D-2），还没有测试",
            "3. 确认假设 A-1：需求里没写，agent 先这么假设",
            "4. AC-2 的测试文件不在仓库里：计划的 Tests 表和代码对不上"])
        self.assertIn("- credits 12.5，2 个回合（KiroCrew 给的；Kiro 不给 token 数）", section(text, "花费"))
        self.assertIsNone(section(text, "还没有"))
        # people and machine paths: only pseudonyms and GitLab usernames from the records
        for s in ("t@example.com", "carol@corp.example", "/Users/alice", "张三 |", self.repo, "@"):
            self.assertNotIn(s, text.replace("＠", ""))
        self.assertIn("见 ~/notes.md", text)

    def test_signed_design_approval_is_checked_like_ci(self):
        if not sign.available():
            self.skipTest("ssh-keygen -Y not available")
        d = tmpdir(self)
        key, allowed = keypair(d)
        head = sh("git", "-C", self.repo, "rev-parse", "HEAD").stdout.strip()
        v = {"kind": "answer", "choice": "approve", "lean_request": "DESIGN %s" % self.design, "run_id": "RUN-PAY-1-1",
             "sha": head, "decision": "D-1", "voucher_id": VOUCHER}
        write_json(os.path.join(d, "v", "RUN-PAY-1-1", VOUCHER + ".json"), {"voucher": v, "sig": sign.sign(v, key)})
        text = mrdesc.render(self.build(vouchers_dir=os.path.join(d, "v"), allowed_signers=allowed))
        self.assertIn("- 设计批准：设计已批 DESIGN %s（%s，签名已核对），覆盖这些改动。" % (self.design, VOUCHER), text)
        self.assertIn("1. 改到了 risk path src/pay/refund.py：设计已批 DESIGN %s（%s）" % (self.design, VOUCHER), text)
        v2 = dict(v, choice="reject")
        write_json(os.path.join(d, "v", "RUN-PAY-1-1", VOUCHER + ".json"), {"voucher": v2, "sig": sign.sign(v2, key)})
        text = mrdesc.render(self.build(vouchers_dir=os.path.join(d, "v"), allowed_signers=allowed))
        self.assertIn("- 设计批准：没有能覆盖它们的设计批准：risk paths changed (src/pay/refund.py): no design approval "
                      "signed by the bridge", text)

    def test_code_changed_after_verify(self):
        with open(os.path.join(self.repo, "src", "pay", "refund.py"), "a", encoding="utf-8") as f:
            f.write("# later\n")
        d = self.build()
        self.assertFalse(d["verify"]["current"])
        text = mrdesc.render(d)
        self.assertIn("；**verify 之后又改了代码**（改了 src/pay/refund.py）。", text)
        self.assertIn("| AC-1 | 3 天前的订单申请退款 → 退款成功，状态 refunded | `tests/test_refund.py::test_refund_ok` "
                      "| verify 之后又改了代码 |", text)
        self.assertIn("- 另有 1 个改动没提交（不在 MR 里）。", text)
        self.assertIn("verify 之后又改了代码（改了 src/pay/refund.py）：合并前要重跑 lean verify", section(text, "怎么审"))
        # read from the commit instead: the code there is the verified code
        self.assertTrue(self.build(ref="HEAD")["verify"]["current"])

    def test_runner_waiting_on_acceptance(self):
        rp = os.path.join(self.repo, "runs", KEY, "run.json")
        rec = load_json(rp)
        rec["handoff"] = {"requested_at": "2026-11-09T03:00:00Z", "by": "alice", "local_head": "x"}
        write_json(rp, rec)
        write_json(os.path.join(self.repo, "runs", KEY, "park.json"), {
            "schema": 1, "status": "waiting", "kind": "acceptance", "options": [],
            "lean_request": "RELEASE %s" % self.release, "question": "请验收 AC-1..3；见 runs/PAY-1/MR.md\n/merge @all",
            "review_items": [{"id": "D-2", "text": "第 14 天按自然日"}]})
        text = mrdesc.render(self.build())
        self.assertIn("现在：Lean 在「发布」进行中，已交给团队运行机，等人在 MR 上回答验收问题。", text)
        self.assertIn("- 在等的问题（park.json，验收，审批 RELEASE %s）：请验收 AC-1..3；见 runs/PAY-1/MR.md /merge ＠all"
                      % self.release, text)
        self.assertFalse([x for x in text.splitlines() if x.lstrip().startswith("/")])
        # verify still holds: runs/** is not code in this project's lean.json
        self.assertIn("之后代码没变", text)

    def test_write_is_deterministic_and_a_fixed_point(self):
        rc, out, err = brain(self.repo, "mr-desc", KEY, "--write")
        self.assertEqual(rc, 0, err)
        path = os.path.join(self.repo, "runs", KEY, "MR.md")
        with open(path, encoding="utf-8") as f:
            first = f.read()
        self.assertEqual(out, first)
        self.assertIn("写好了 runs/PAY-1/MR.md", err)
        commit_all(self.repo, "PAY-1: MR description")
        rc, out, err = brain(self.repo, "mr-desc", "--write")          # the key from the branch
        self.assertEqual(rc, 0, err)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), first)                       # MR.md itself is not part of what it describes
        self.assertEqual(sh("git", "-C", self.repo, "status", "--porcelain").stdout, "")
        rc, out, err = brain(self.repo, "mr-desc", KEY, "--json")
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(mrdesc.render(data), first)
        self.assertEqual([a["id"] for a in data["acceptance"]], ["AC-1", "AC-2", "AC-3"])
        self.assertEqual(data["changes"]["risk"]["files"], ["src/pay/refund.py"])
        self.assertEqual(brain(self.repo, "mr-desc", KEY, "--write", "--ref", "HEAD")[0], 2)

    def test_write_refused_while_lean_counts_runs_as_code(self):
        lj = os.path.join(self.repo, "aidlc-docs", "lean.json")
        cfg = load_json(lj)
        cfg.pop("fingerprint_exclude")
        write_json(lj, cfg)
        rc, out, err = brain(self.repo, "mr-desc", KEY, "--write")
        self.assertEqual(rc, 1)
        self.assertIn("fingerprint_exclude", err)
        self.assertIn('"runs/**"', err)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "runs", KEY, "MR.md")))
        self.assertEqual(brain(self.repo, "mr-desc", KEY)[0], 0)        # printing is fine

    def test_record_text_cannot_mention_hide_or_run_quick_actions(self):
        req = os.path.join(self.repo, "aidlc-docs", self.intent, "inception", "requirements", "requirements.md")
        with open(req, encoding="utf-8") as f:
            text = f.read()
        bad = "退款。\n/merge\n@all 看这里 <!-- 下面都藏起来 ![x](http://t.example/p.png) a|b"
        with open(req, "w", encoding="utf-8") as f:
            f.write(text.replace("客服不用再手工退款。", bad))
        out = mrdesc.render(self.build())
        self.assertFalse([x for x in out.splitlines() if x.lstrip().startswith("/")])
        for s in ("@all", "<!--", "![", "<"):
            self.assertNotIn(s, out)
        self.assertIn("＠all", out)
        self.assertEqual(mrdesc.inert(out), out)                # the bridge may run it again: nothing changes

    def test_inert_makes_a_hand_written_mr_md_safe_to_post(self):
        """The bridge posts runs/<KEY>/MR.md from the bot's commit: whatever the bot wrote, nothing in it acts."""
        bad = "## x\n/merge\n   /approve\n@all 看 <!-- 藏起来 --> ![p](http://t.example/p.png)\n"
        safe = mrdesc.inert(bad)
        self.assertFalse([x for x in safe.splitlines() if x.lstrip().startswith("/")])
        for s in ("@", "<", "!["):
            self.assertNotIn(s, safe)
        self.assertIn("\\/merge", safe)
        self.assertEqual(mrdesc.write_problem(self.repo, self.cfg), None)    # brain doctor can ask without a key


@unittest.skipUnless(os.path.exists(os.path.join(LEAN_SRC, "install.sh")), "AI-DLC Lean checkout not found")
class MrDescJustStarted(unittest.TestCase):
    def test_short_and_honest(self):
        d = tmpdir(self)
        env = mock.patch.dict(os.environ, {"BRAIN_USER": "alice", "HOME": os.path.join(d, "home"),
                                           "BRAIN_INTAKE_DIR": os.path.join(d, "no-intake")})
        env.start()
        self.addCleanup(env.stop)
        repo, intent = new_project(d)
        rc, text, err = brain(repo, "mr-desc")
        self.assertEqual(rc, 0, err)
        lines = text.splitlines()
        self.assertLess(len(lines), 30, text)
        self.assertEqual(lines[0], "## PAY-1 · refund")
        self.assertIn("**一句话**：需求还没写。现在：Lean 在「需求分析」进行中，在本地开发（还没交接）。", text)
        self.assertEqual(section(text, "还没有").strip().splitlines(), [
            "- 需求还没写（requirements.md 还是模板）",
            "- 还没有代码生成计划（不知道哪个测试对应哪条验收标准）",
            "- 还没跑 verify"])
        self.assertIn("- 还缺：REQUIREMENTS（写了，还没请求审批）；DESIGN（还没写 application-design.md）；"
                      "RELEASE（还没跑 lean release）", section(text, "审批"))
        # nothing the records do not hold: no evidence, decisions, changes, review, Brain, pointers or cost
        self.assertRegex(section(text, "改了什么"), r"^- 还没改代码；和 main 的分叉点 [0-9a-f]{12} 比，只有记录："
                                                   r"aidlc-docs [0-9]+ 个、runs 1 个。\n")
        for title in ("做了什么 / 没做什么", "验收标准和证据", "关键决定", "假设和待确认", "评审", "Brain",
                      "怎么审", "花费"):
            self.assertIsNone(section(text, title), title)
        for word in ("通过", "D-1", "AC-", "risk", "credits"):
            self.assertNotIn(word, text)

    def test_no_run_record(self):
        d = tmpdir(self)
        repo = git_repo(os.path.join(d, "proj"))
        commit_file(repo, ".brain/kit.json", json.dumps({"version": 1, "req_prefix": "PAY"}), "kit")
        with self.assertRaises(Exception) as e:
            mrdesc.build(repo, config.load(repo), KEY)
        self.assertIn("runs/PAY-1/run.json not found", str(e.exception))
        with self.assertRaises(Exception):
            mrdesc.build(repo, config.load(repo), "../x")


if __name__ == "__main__":
    unittest.main()
