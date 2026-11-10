"""QA round 2 findings on intake screening and golden tasks (ported from qa_r2/test_mod_*.py).

Each class names its finding. Secrets and key-shaped values are built at runtime (a pre-commit scanner refuses them
in source files).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brainkit import brainlint, intents  # noqa: E402
from brainkit import golden as G  # noqa: E402
from brainkit.common import BrainError  # noqa: E402
from brainkit.intake import checks, package  # noqa: E402
from tests.helpers import git_repo  # noqa: E402
from tests.test_golden import ceiling_at_the_temp_root, make_task, restore_ceiling  # noqa: E402
from tests.test_qa_modules import commit_all, head, project, run_cli  # noqa: E402

KIT_INTAKE = {"allow_confidential": False, "bulk_pii_limit": 30, "segment_chars": 6000, "max_file_mb": 50,
              "max_total_mb": 200}
PW = "PASS" + "WORD"
_SAVED = {}


def setUpModule():
    _SAVED["ceiling"] = ceiling_at_the_temp_root()
    _SAVED["home"] = os.environ.get("HOME")
    _SAVED["tmp_home"] = tempfile.mkdtemp(prefix="qa-r2-home-")
    os.environ["HOME"] = _SAVED["tmp_home"]


def tearDownModule():
    restore_ceiling(_SAVED["ceiling"])
    if _SAVED["home"] is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _SAVED["home"]
    shutil.rmtree(_SAVED["tmp_home"], ignore_errors=True)


def build(test, files, **kw):
    """package.build on a folder with these files; returns (report, all segment text, findings.md or "")."""
    d = os.path.realpath(tempfile.mkdtemp(prefix="qa-r2-intake-"))
    test.addCleanup(shutil.rmtree, d, True)
    src = os.path.join(d, "docs")
    for name, text in files.items():
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, name), "w", encoding="utf-8") as f:
            f.write(text)
    store = os.path.join(d, "store")
    rep = package.build([src], "PAY-1", store, submitter="alice", consent=True, classification="internal",
                        business="keep", kit_intake=KIT_INTAKE, **kw)
    if kw.get("dry_run"):
        return rep, "", ""
    base = os.path.join(store, "packages", rep["package"])
    seg_dir = os.path.join(base, "segments")
    text = ""
    for n in sorted(os.listdir(seg_dir)):
        with open(os.path.join(seg_dir, n), encoding="utf-8") as f:
            text += f.read()
    with open(os.path.join(base, "findings.md"), encoding="utf-8") as f:
        return rep, text, f.read()


def masked(text):
    return checks.apply(text, checks.scan(text), False, checks.Masker())


# ---------- 3 (blocker): a person in a 负责人 / Owner cell next to a team or role ----------

class R3NameCells(unittest.TestCase):
    TABLE = "| 事项 | %s |\n|---|---|\n| 退款接口 | %s |\n"
    CELLS = {                         # cell -> the person that must not survive, the part that stays
        "person and a team": ("负责人", "张伟、支付组", "张伟", "支付组"),
        "person and a role": ("负责人", "王芳/产品经理", "王芳", "产品经理"),
        "latin person and a team": ("Owner", "Alice Wang, Payments Team", "Alice Wang", "Payments Team"),
        "cantonese surname To": ("Owner", "Peter To", "Peter To", None),
        "cantonese surname Or": ("负责人", "Kenneth Or", "Kenneth Or", None),
        "surname that is a CI tool": ("Owner", "Sarah Jenkins", "Sarah Jenkins", None),
        "surname + job title": ("负责人", "李娜、王工程师", "王工程师", None),
        "person and a team in brackets": ("负责人", "陈静（结算组）", "陈静", "结算组"),
    }

    def test_each_part_is_judged_and_only_the_person_is_masked(self):
        for label, (header, cell, person, kept) in self.CELLS.items():
            with self.subTest(label):
                out = masked(self.TABLE % (header, cell))
                self.assertNotIn(person, out)
                self.assertIn("[人名-", out)
                if kept:
                    self.assertIn(kept, out)
        self.assertEqual(masked(self.TABLE % ("负责人", "张伟、支付组")).split("\n")[2], "| 退款接口 | [人名-1]、支付组 |")

    def test_roles_teams_and_tools_alone_stay(self):
        for cell in ("产品经理", "支付组", "Payments Team", "Jenkins", "待定"):
            with self.subTest(cell):
                self.assertEqual([h for h in checks.scan(self.TABLE % ("负责人", cell)) if h.kind == "person"], [])

    def test_intent_and_brain_text_with_the_table_are_refused(self):
        for label, (header, cell, person, _) in self.CELLS.items():
            with self.subTest(label):
                errs, _ = intents.problems("alert", "退款接口超时", "值班表：\n" + self.TABLE % (header, cell))
                self.assertTrue(errs, "intent accepted with %r in a %s column" % (person, header))
                self.assertIn("personal data (person)", brainlint.content_problems(self.TABLE % (header, cell))[0])

    def test_intake_package_holds_no_name(self):
        _, text, _ = build(self, {"raci.md": "# 退款项目分工\n\n| 事项 | 负责人 |\n|---|---|\n| 退款接口 | 张伟、支付组 |\n"
                                             "| 对账 | Alice Wang, Payments Team |\n"})
        self.assertNotIn("张伟", text)
        self.assertNotIn("Alice Wang", text)
        self.assertIn("支付组", text)
        self.assertIn("Payments Team", text)


# ---------- 6: Title-Case words after owner / contact / reported by ... are not always a person ----------

class R6LatinLabels(unittest.TestCase):
    ALERTS = {
        "monitoring tool": "Reported by New Relic: checkout p99 latency above 2s for 15 minutes.",
        "health check": "Alert raised by Health Check on /api/refunds, 5 failures in a row.",
        "zabbix": "Reported by Zabbix Server: disk usage on db-2 above the threshold.",
        "company": "Requested by Acme Corp: export the refund list as CSV.",
    }
    BRAIN_TEXT = {
        "feature name": "Contact Form Validation trims whitespace before the email check.",
        "process name": "The Account Owner Transfer Process locks the account until the new owner accepts.",
        "schedule name": "The On-call Rotation Schedule is the source of truth for escalations.",
        "UI section": "Reviewer Comments Section stays collapsed on mobile.",
    }
    FEATURES = ["List View", "Detail Page", "Import Wizard", "Merge Rules", "Export Job", "Search Box", "Tag Editor",
                "Sync Status", "Avatar Upload", "Phone Field", "Email Field", "Address Block", "Birthday Field",
                "Notes Panel", "Activity Feed", "Timeline View", "Duplicate Check", "Bulk Delete", "Bulk Edit",
                "Filter Bar", "Sort Order", "Column Picker", "Share Dialog", "Print View", "Archive Rule",
                "Restore Flow", "Access Rule", "Quota Banner", "Error Toast", "Empty State", "Label Chips",
                "Group Picker"]

    def test_alert_intents_are_filed(self):
        for label, body in self.ALERTS.items():
            with self.subTest(label):
                self.assertEqual(intents.problems("alert", "Checkout alert", body)[0], [], body)

    def test_brain_text_about_features_is_accepted(self):
        for label, text in self.BRAIN_TEXT.items():
            with self.subTest(label):
                self.assertEqual(brainlint.content_problems(text)[0], [], text)

    def test_people_are_still_refused(self):
        for text in ("Reported by Alice Wang", "On-call: Zhang Wei acknowledged the alert",
                     "Assignee: Mary-Jane Smith", "Escalated to Chan Tai Man.", "Owner: Alice Wang"):
            with self.subTest(text):
                self.assertIn("personal data (person)", brainlint.content_problems(text)[0])

    def test_intake_keeps_feature_names(self):
        _, text, _ = build(self, {"prd.md": "# Contacts PRD\n\n## Contact Form Validation\n\n"
                                            "Form Validation must reject an empty email address.\n\n"
                                            "## Account Owner Transfer Process\n\n"
                                            "The Transfer Process needs the new owner to accept.\n"})
        self.assertIn("Form Validation must reject an empty email address.", text)
        self.assertIn("The Transfer Process needs the new owner to accept.", text)
        self.assertNotIn("[人名-", text)

    def test_a_feature_list_is_not_bulk_personal_data(self):
        doc = "# CRM contacts\n\n" + "\n".join("- Contact %s: as in the mockup." % f for f in self.FEATURES) + "\n"
        rep, _, _ = build(self, {"crm.md": doc}, dry_run=True)
        self.assertEqual(rep["refused"], [])

    def test_unsure_name_goes_to_the_submitter_not_masked_not_refused(self):
        line = "电话 13812345678 Reported by Sam Example today."
        self.assertEqual(brainlint.content_problems("Reported by Sam Example today.")[0], [])
        rep, text, findings = build(self, {"a.md": "# 告警\n\n%s\n" % line})
        self.assertIn("Reported by Sam Example today.", text)                # not masked
        self.assertNotIn("13812345678", text)
        self.assertIn("请你确认 1 处", findings)
        self.assertIn("「Reported by Sam Example」", findings)
        self.assertNotIn("13812345678", findings + json.dumps(rep, ensure_ascii=False))   # the quote stops at a hit


# ---------- 7: common words after a Chinese label, and masking package-wide ----------

class R7CjkLabels(unittest.TestCase):
    LINES = {"customer": "提出人：顾客", "consultant": "对接人：顾问", "executive VP (a role)": "审批人：常务副总",
             "day shift": "值班：白班 9:00-18:00", "the scheduler": "执行人：任务调度"}

    def test_not_personal_data(self):
        for label, line in self.LINES.items():
            with self.subTest(label):
                self.assertEqual(brainlint.content_problems(line)[0], [], line)

    def test_names_after_labels_still_count(self):
        for line in ("负责人：张伟，已确认", "負責人：陳大文", "联系人：王芳、李娜", "处理人：张经理", "负责人：欧阳娜娜。",
                     "值班：张伟", "负责人：周一鸣，"):
            with self.subTest(line):
                self.assertIn("personal data (person)", brainlint.content_problems(line)[0])

    def test_intake_does_not_mask_customer_everywhere(self):
        _, text, _ = build(self, {"feedback.md": "# 反馈\n\n提出人：顾客\n\n退款太慢。\n",
                                  "refund.md": "# 退款\n\n顾客可以在 App 里查看退款进度。顾客取消订单后，退款原路返回。\n"})
        self.assertIn("顾客可以在 App 里查看退款进度。顾客取消订单后，退款原路返回。", text)
        self.assertNotIn("[人名-", text)

    def test_label_names_spread_like_values_found_by_their_form(self):
        # R1-48 (the user's decision of 2026-11-10): a name found next to a label is masked wherever it occurs, as a
        # phone is; this test used to pin the opposite (the name stayed where it was not labelled)
        _, text, _ = build(self, {"a.md": "# 值班\n\n负责人：张伟。電話：9123 4567\n",
                                  "b.md": "# 记录\n\n张伟说修好了，回拨 9123 4567。\n"})
        self.assertIn("负责人：[人名-1]", text)
        self.assertIn("[人名-1]说修好了", text)
        self.assertNotIn("9123 4567", text)


# ---------- 8: formats, algorithms and rules after 密码为 / password is ----------

class R8SaidSecrets(unittest.TestCase):
    POLICY = {
        "6-digit PIN rule": "The payment password is 6-digit and numeric only.",
        "encoding": "The password is base64-encoded before it is sent to the HSM.",
        "national cipher": "密码为 SM4-CBC 加密存储。",
        "one-time codes": "口令为 TOTP 动态口令。",
        "empty value": "密码为 null 时提示用户设置。",
        "a type": "token: string",
        "masked already": "password: ******",
    }

    def test_policy_text_is_not_a_secret(self):
        for label, text in self.POLICY.items():
            with self.subTest(label):
                self.assertEqual(brainlint.content_problems(text)[0], [], text)

    def test_passwords_still_count(self):
        for text in ("测试环境数据库密码是 Hunter2024，请轮换", "the staging password is Hunter2024", "口令是 abcd1234",
                     "api_key = sk-live-" + "abc123def456", "密码：abc123（测试环境）"):
            with self.subTest(text):
                self.assertIn("looks like a secret (kv_secret)", brainlint.content_problems(text)[0])
        self.assertEqual(masked("密码：abc123（测试环境）"), "密码：[密钥-1]（测试环境）")   # the value only

    def test_intake_keeps_the_spec(self):
        rep, text, _ = build(self, {"spec.md": "# 退款\n\n## 支付密码\n\n密码为 null 时提示用户设置。\n\n## 接口\n\n"
                                               "字段 refund_at 为 null 表示未退款。\n\n## 签名\n\n"
                                               "口令为 TOTP 动态口令，签名用 TOTP 生成的码。\n"})
        self.assertIn("字段 refund_at 为 null 表示未退款。", text)
        self.assertIn("签名用 TOTP 生成的码", text)
        self.assertNotIn("请轮换", "\n".join(rep["findings"]))

    def test_letters_only_after_mima_shi_goes_to_the_submitter(self):
        self.assertEqual(brainlint.content_problems("测试库的说明：密码是 hunter 吧")[0], [])
        rep, text, findings = build(self, {"a.md": "# 测试库\n\n密码是 hunter 吧\n"})
        self.assertIn("密码是 hunter 吧", text)
        self.assertIn("请你确认 1 处", findings)
        self.assertIn("「密码是 …」", findings)
        self.assertNotIn("hunter", findings)                                # the possible password is not repeated
        self.assertNotIn("请轮换", findings)


# ---------- 10: dev defaults in compose / CI files refused every golden snapshot ----------

class R10GoldenDevPasswords(unittest.TestCase):
    CASES = {
        "compose, map form": ("docker-compose.yml", "services:\n  db:\n    image: postgres:16\n    environment:\n"
                                                    "      POSTGRES_%s: postgres\n" % PW, True),
        "compose, list form": ("docker-compose.yml", "services:\n  db:\n    environment:\n"
                                                     "      - POSTGRES_%s=postgres\n" % PW, True),
        "CI test service": (".gitlab-ci.yml", "test:\n  services: [postgres]\n  variables:\n"
                                              "    POSTGRES_%s: runner-test\n  script: [make test]\n" % PW, True),
        "header name setting": ("config/app.yml", "CSRF_" + "TOKEN: X-CSRF-Token\n", False),
        "numeric limit": ("config/llm.yml", "MAX_" + "TOKEN: 4096\n", False),
    }
    SECRET = "DB_%s=" % PW + "Zq8" + "-v41ue-k9"

    @classmethod
    def setUpClass(cls):
        cls.d = os.path.realpath(tempfile.mkdtemp(prefix="qa-r2-golden-"))
        cls.addClassCleanup(shutil.rmtree, cls.d, True)
        cls.repo = git_repo(os.path.join(cls.d, "proj"))
        cls.sha = commit_all(cls.repo, {".brain/kit.json": json.dumps({"version": 1, "req_prefix": "PAY",
                                                                       "tracker": "none"}),
                                        "src/app.py": "print('hi')\n"}, "init")
        cls.task = make_task(cls.d, "G-1", cls.sha)

    def snap(self, name, files, **kw):
        real = G._extract

        def extract(tar, out):
            real(tar, out)
            for rel, text in files.items():
                p = os.path.join(out, *rel.split("/"))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(text)
        out = os.path.join(self.d, name)
        with mock.patch.object(G, "_extract", extract):
            return out, G.snapshot(self.repo, self.sha, out, **kw)

    def read(self, out, rel):
        with open(os.path.join(out, *rel.split("/"))) as f:
            return f.read()

    def test_snapshot_is_made_dev_defaults_masked_and_listed(self):
        for n, (label, (rel, text, dev)) in enumerate(sorted(self.CASES.items())):
            with self.subTest(label):
                try:
                    out, man = self.snap("snap-%d" % n, {rel: text})
                except BrainError as e:
                    self.fail("snapshot refused for %s: %s" % (rel, e))
                self.assertTrue(os.path.isfile(os.path.join(out, "src", "app.py")))
                got = self.read(out, rel)
                if dev:
                    self.assertEqual([(m["path"], m["name"], m["why"]) for m in man["masked"]],
                                     [(rel, "POSTGRES_" + PW, "dev default")])
                    self.assertIn("masked-by-brain-1", got)
                    self.assertNotIn(": postgres\n", got)
                    self.assertNotIn("runner-test", got)
                else:
                    self.assertEqual((man["masked"], got), ([], text))      # not a secret: untouched
                self.assertIsNone(man["override"])

    def test_same_dev_default_same_placeholder(self):
        out, man = self.snap("same", {"docker-compose.yml": "x:\n  POSTGRES_%s: postgres\n" % PW,
                                      "config/app.env.yml": "DB_%s: postgres\n" % PW})
        self.assertEqual(len(man["masked"]), 2)
        self.assertIn("masked-by-brain-1", self.read(out, "docker-compose.yml"))
        self.assertIn("masked-by-brain-1", self.read(out, "config/app.env.yml"))

    def test_secret_looking_value_refused_unless_the_override_is_given(self):
        with self.assertRaises(BrainError) as cm:
            self.snap("refused", {"deploy/app.cfg": self.SECRET + "\n"})
        self.assertIn("deploy/app.cfg (password assignment)", str(cm.exception))
        self.assertIn("--mask-secrets", str(cm.exception))
        self.assertNotIn("v41ue", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.d, "refused")))
        key = "-----BEGIN " + "OPENSSH " + "PRIVATE KEY-----\nabc\n-----END " + "OPENSSH PRIVATE KEY-----\n"
        out, man = self.snap("override", {"deploy/app.cfg": self.SECRET + "\n", "deploy/id_key": key},
                             mask_secrets=True)
        self.assertEqual(man["override"], "mask-secrets")
        self.assertEqual([(m["path"], m["line"], m["why"]) for m in man["masked"]],
                         [("deploy/app.cfg", 1, "secret (--mask-secrets)")])
        self.assertIn({"path": "deploy/id_key", "why": "private key (--mask-secrets)"}, man["removed"])
        self.assertFalse(os.path.exists(os.path.join(out, "deploy", "id_key")))
        self.assertEqual(self.read(out, "deploy/app.cfg"), "DB_%s=masked-by-brain-1\n" % PW)
        self.assertNotIn("v41ue", json.dumps(man))

    def test_cli_override_is_explicit_and_logged(self):
        real = G._extract

        def extract(tar, out):
            real(tar, out)
            with open(os.path.join(out, "app.cfg"), "w") as f:
                f.write(self.SECRET + "\n")
        with mock.patch.object(G, "_extract", extract):
            code, out = run_cli(self.repo, ["golden", "snapshot", self.task, "--out", os.path.join(self.d, "c1")])
            self.assertEqual(code, 2, out)
            self.assertIn("app.cfg (password assignment)", out)
            code, out = run_cli(self.repo, ["golden", "snapshot", self.task, "--out", os.path.join(self.d, "c2"),
                                            "--mask-secrets"])
        self.assertEqual(code, 0, out)
        self.assertIn("WARN: --mask-secrets", out)
        self.assertIn("打码 app.cfg 第 1 行 DB_%s（secret (--mask-secrets)）" % PW, out)


# ---------- 11: the ablation must not touch instructions or id-shaped words ----------

class R11AblationScrub(unittest.TestCase):
    TECH = ("# TECH\n\n<!-- id:T-PAY-3-1 type:pitfall side:builder rel:new src:D-1 -->\n重试最多 3 次。\n\n"
            "<!-- id:T-1 type:pitfall side:builder -->\n回调要幂等。\n")
    SETTLEMENT = "# 对账\n\n- 每天 T-1 日的交易在早上 6 点前对完。\n- 退款单号是 RF + 9 位。\n- 重试规则见 T-PAY-3-1。\n"
    DECISIONS = ("# Decisions\n\n| ID | Stage | Decision | Who |\n|---|---|---|---|\n"
                 "| D-1 | code | 重试最多 3 次（T-PAY-3-1） | agent |\n"
                 "| D-2 | code | 回调幂等（`T-1`） | agent |\n"
                 "| D-3 | code | 每天 T-1 日对账 | agent |\n"
                 "| D-4 | code | 示例号 T-PAY-88-1 和 T-PAY-3-10 不是这次去掉的 | agent |\n\n"
                 "```brain-proposal\nfile: TECH\ntype: pitfall\nsrc: D-1\n---\n重试最多 3 次。\n```\n")
    GUIDE = "# 怎么写提案\n\n```brain-proposal\nfile: TECH\ntype: pitfall\nsrc: D-1\n---\n例子\n```\n"
    KIT_FILES = (".kiro/steering/brain.md", ".kiro/skills/brain-reflect/SKILL.md")

    @classmethod
    def setUpClass(cls):
        cls.d = os.path.realpath(tempfile.mkdtemp(prefix="qa-r2-abl-"))
        cls.addClassCleanup(shutil.rmtree, cls.d, True)
        repo = git_repo(os.path.join(cls.d, "proj"))
        files = {".brain/kit.json": json.dumps({"version": 1, "req_prefix": "PAY", "tracker": "none"}),
                 "brain/TECH.md": cls.TECH, "docs/settlement.md": cls.SETTLEMENT, "docs/guide.md": cls.GUIDE,
                 "aidlc-docs/261109-pay-3-retry/decisions.md": cls.DECISIONS,
                 "AGENTS.md": "# Agents\n\n- 重试见 T-PAY-3-1。\n"}
        for rel in cls.KIT_FILES:                    # what `brain install` copies from kit/
            with open(os.path.join(ROOT, "kit", *rel.split("/")), encoding="utf-8") as f:
                files[rel] = f.read()
        sha = commit_all(repo, files, "init")
        cls.full = os.path.join(cls.d, "full")
        cls.abl = os.path.join(cls.d, "no-tech")
        G.snapshot(repo, sha, cls.full)
        cls.man = G.snapshot(repo, sha, cls.abl, variant="no-TECH")

    def read(self, root, rel):
        with open(os.path.join(root, *rel.split("/")), encoding="utf-8") as f:
            return f.read()

    def test_instruction_files_are_the_same_in_every_variant(self):
        for rel in self.KIT_FILES + ("AGENTS.md",):
            with self.subTest(rel):
                self.assertEqual(self.read(self.abl, rel), self.read(self.full, rel), self.man["scrubbed"])

    def test_a_t_minus_1_business_rule_is_not_an_entry_citation(self):
        text = self.read(self.abl, "docs/settlement.md")
        self.assertIn("每天 T-1 日的交易在早上 6 点前对完。", text)
        self.assertNotIn("T-PAY-3-1", text)                            # a full id of the removed file: cut
        self.assertIn("退款单号是 RF + 9 位。", text)

    def test_run_docs_lose_only_the_removed_entries(self):
        text = self.read(self.abl, "aidlc-docs/261109-pay-3-retry/decisions.md")
        self.assertNotIn("D-1 |", text)                                # cites T-PAY-3-1
        self.assertNotIn("D-2 |", text)                                # cites `T-1`, a citation shape
        self.assertIn("| D-3 | code | 每天 T-1 日对账 | agent |", text)   # T-1 as a word
        self.assertIn("T-PAY-88-1 和 T-PAY-3-10", text)                 # other ids, and not a prefix match
        self.assertNotIn("brain-proposal", text)
        self.assertIn("```brain-proposal\nfile: TECH", self.read(self.abl, "docs/guide.md"))   # not a run doc
        self.assertEqual(sorted((s["path"], s["blocks"], s["lines"]) for s in self.man["scrubbed"]),
                         [("aidlc-docs/261109-pay-3-retry/decisions.md", 1, 2), ("docs/settlement.md", 0, 1)])


# ---------- 40: TMPDIR inside a git checkout, and GIT_CEILING_DIRECTORIES ----------

class R40TmpdirInARepo(unittest.TestCase):
    def checkout(self):
        d = os.path.realpath(tempfile.mkdtemp(prefix="qa-r2-ci-"))
        self.addCleanup(shutil.rmtree, d, True)
        checkout = os.path.join(d, "checkout")
        subprocess.run(["git", "init", "-q", "-b", "main", checkout], check=True, capture_output=True)
        tmp = os.path.join(checkout, "tmp")
        os.makedirs(tmp)
        return checkout, tmp

    def run_tests(self, names, env):
        p = subprocess.run([sys.executable, "-m", "unittest"] + names, cwd=ROOT, capture_output=True, text=True,
                           env=env, timeout=300)
        self.assertEqual(p.returncode, 0, (p.stdout + p.stderr)[-1500:])

    def test_golden_tests_pass_with_tmpdir_inside_a_checkout(self):
        checkout, tmp = self.checkout()                 # the reviewer's case: the CI job sets the ceiling
        self.run_tests(["tests.test_qa_modules.N4EnvFiles"],
                       dict(os.environ, TMPDIR=tmp, GIT_CEILING_DIRECTORIES=checkout))

    def test_the_tests_do_not_need_a_ceiling_from_outside(self):
        checkout, tmp = self.checkout()
        env = dict(os.environ, TMPDIR=tmp)
        env.pop("GIT_CEILING_DIRECTORIES", None)
        self.run_tests(["tests.test_golden.Snapshot.test_full_snapshot_is_clean",
                        "tests.test_qa_modules.N23SnapshotInsideARepo"], env)

    def test_ceiling_is_honoured_and_recorded(self):
        checkout, tmp = self.checkout()
        d, repo = project(self)
        out = os.path.join(tmp, "job-1", "snap")
        with self.assertRaises(BrainError) as cm:
            G.snapshot(repo, head(repo), out)
        self.assertIn("GIT_CEILING_DIRECTORIES", str(cm.exception))
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": checkout}):
            man = G.snapshot(repo, head(repo), out)
            self.assertEqual(man["git_ceiling"], {"repo": checkout, "GIT_CEILING_DIRECTORIES": checkout})
            task = make_task(d, "G-1", head(repo))
            code, text = run_cli(repo, ["golden", "snapshot", task, "--out", os.path.join(tmp, "job-2", "snap")])
        self.assertEqual(code, 0, text)
        self.assertIn("agent 的 run 也必须带上同样的设置", text)
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": repo}), self.assertRaises(BrainError):
            G.snapshot(repo, head(repo), os.path.join(repo, "inside"))   # in the project itself: never


if __name__ == "__main__":
    unittest.main()
