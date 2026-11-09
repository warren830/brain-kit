"""QA findings on golden tasks, intents, intake screening, reflect --draft and entries (ported from qa_findings/).

Each class names its finding. Secrets and key-shaped values are built at runtime (a pre-commit scanner refuses them
in source files).
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
import warnings
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import brainlint, cli, intents  # noqa: E402
from brainkit import entries as E  # noqa: E402
from brainkit import golden as G  # noqa: E402
from brainkit.common import JIRA_RE, BrainError  # noqa: E402
from brainkit.intake import checks  # noqa: E402
from tests.helpers import commit_file, git_repo, sh, tmpdir  # noqa: E402

PY = sys.executable
KIT = {"version": 1, "req_prefix": "PAY", "bot_users": ["brain-bot"], "tracker": "none"}
RULE = "重试最多 3 次。"
TECH = ("# TECH\n\n<!-- id:T-PAY-1-1 type:pitfall side:builder rel:new src:D-1 -->\n退款单号是 RF + 9 位。\n\n"
        "<!-- id:T-PAY-1-2 type:pitfall side:builder rel:new src:D-1 -->\n%s\n" % RULE)
PRODUCT = "# PRODUCT\n\n<!-- id:P-PAY-1-1 type:model side:product rel:new src:D-1 -->\n**退款单**：一次退款的记录。\n"
PASSWORD_LINE = "DB_" + "PASSWORD=" + "supersecret"           # runtime: no secret-shaped literal in the source


def run_cli(repo, args, env=None):
    """`brain <args>` in-process with cwd = repo. Returns (exit code, stdout + stderr)."""
    old_cwd, old_env = os.getcwd(), dict(os.environ)
    out = io.StringIO()
    try:
        os.chdir(repo)
        os.environ.pop("GITLAB_TOKEN", None)
        os.environ.update(env or {})
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = cli.main(list(args))
            except SystemExit as e:
                code = e.code
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)
    return code, out.getvalue()


def project(test, files=None):
    """A repo with kit.json and a small Brain, one commit per file set. Returns (dir, repo)."""
    d = tmpdir(test)
    repo = git_repo(os.path.join(d, "proj"))
    init = {".brain/kit.json": json.dumps(KIT), "brain/TECH.md": TECH, "brain/PRODUCT.md": PRODUCT,
            "README.md": "x\n"}
    init.update(files or {})
    commit_all(repo, init, "init")
    return d, repo


def commit_all(repo, files, msg):
    for rel, text in files.items():
        p = os.path.join(repo, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    sh("git", "-C", repo, "add", "-A")
    sh("git", "-C", repo, "commit", "-q", "-m", msg)
    return sh("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()


def head(repo):
    return sh("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()


def grep(root, needle):
    hits = []
    for d, _, files in os.walk(root):
        for n in files:
            p = os.path.join(d, n)
            if os.path.islink(p):
                continue
            with open(p, encoding="utf-8", errors="replace") as f:
                if needle in f.read():
                    hits.append(os.path.relpath(p, root))
    return sorted(hits)


# ---------- N3: intent screening (and the shared checks) ----------

class N3Screening(unittest.TestCase):
    REFUSED = {
        "name after a Chinese label": "支付回调超时告警，负责人：张伟，已确认",
        "on-call name": "On-call: Zhang Wei acknowledged the alert",
        "reporter name": "Reported by Alice Wang",
        "mobile with spaces": "值班电话 138 0013 8000",
        "mobile with dashes": "联系手机 +86-138-0013-8000",
        "password in a sentence": "测试环境数据库密码是 Hunter2024，请轮换",
        "owner, traditional": "負責人：陳大文",
        "two names": "联系人：王芳、李娜",
        "assignee": "Assignee: Mary-Jane Smith",
        "surname and title": "处理人：张经理",
        "password is": "the staging password is Hunter2024",
        "口令": "口令是 abcd1234",
    }
    ALLOWED = {
        "role after a label": "负责人：产品经理",
        "team after a label": "负责人：支付组；联系人：客服",
        "role words": "审批人：财务总监，提出人：业务方",
        "templates": "> 负责人：产品。写什么：意图、用户、非目标、术语。",
        "template 2": "> 负责人：Builder（架构）。写什么：架构、约定、运行时的坑。",
        "latin team": "Owner: Payments Team; Reported by Grafana Alerting",
        "single first name": "Owner: Alice",
        "tenors": "期限 3M、6M、12M",
        "a long order id": "订单号 1380013800012",
        "a dashed code": "批次 ORD-138-2026-0001 已重放",
        "password rule": "密码是否正确由网关判断；password is required；密码为 8-16 位",
        "masked": "密码是 ********",
        "weekday": "值班：周一",
    }

    def test_personal_data_and_secrets_are_refused(self):
        for label, text in self.REFUSED.items():
            with self.subTest(label):
                errs, _ = intents.problems("alert", "支付回调超时", text)
                self.assertTrue(errs, "accepted: %r" % text)

    def test_roles_teams_and_rules_are_not_names_or_secrets(self):
        for label, text in self.ALLOWED.items():
            with self.subTest(label):
                errs, _ = brainlint.content_problems(text)
                self.assertEqual(errs, [], text)

    def test_end_to_end_nothing_is_filed(self):
        d, repo = project(self)
        code, out = run_cli(repo, ["intent", "add", "--source", "alert", "--title", "支付回调超时告警",
                                   "--body", "负责人：张伟，值班电话 138 0013 8000"], env={"BRAIN_USER": "carol"})
        self.assertNotEqual(code, 0, out)
        self.assertIn("personal data (person)", out)
        self.assertIn("personal data (phone)", out)
        self.assertEqual(sh("git", "-C", repo, "for-each-ref", "refs/heads/claim/").stdout, "")

    def test_intake_masks_the_name_span_only(self):
        m = checks.Masker()
        text = "负责人：张伟，电话 138 0013 8000。修订人：產品經理。On-call: Zhang Wei acknowledged."
        out = checks.apply(text, checks.scan(text), False, m)
        self.assertEqual(out, "负责人：[人名-1]，电话 [电话-1]。修订人：產品經理。On-call: [人名-2] acknowledged.")

    def test_role_cells_in_name_columns_are_not_names(self):
        kinds = lambda t: [(h.kind, h.value) for h in checks.scan(t) if h.kind == "person"]   # noqa: E731
        self.assertEqual(kinds("| 事项 | 负责人 |\n|---|---|\n| 审批 | 产品经理 |\n| 开发 | 支付组 |\n"), [])
        self.assertEqual(kinds("| 事项 | 负责人 |\n|---|---|\n| 审批 | 张经理 |\n"), [("person", "张经理")])
        self.assertEqual(kinds("| 版本 | 修訂人 |\n|---|---|\n| v1 | 張三 |\n"), [("person", "張三")])


# ---------- N26: intent add --key ----------

class N26BadKeys(unittest.TestCase):
    def test_malformed_keys_are_refused(self):
        d, repo = project(self)
        for key in ("PAY-x", "PAY-007", "PAY-0", "PAY-12345678", "PAY-1-2"):
            with self.subTest(key):
                code, out = run_cli(repo, ["intent", "add", "--source", "debt", "--title", "t " + key, "--body",
                                           "b " + key, "--key", key], env={"BRAIN_USER": "carol"})
                self.assertNotEqual(code, 0, "claimed %s: %s" % (key, out.strip()))
                self.assertIn("not a requirement key", out)
        self.assertEqual(sh("git", "-C", repo, "for-each-ref", "refs/heads/claim/").stdout, "")

    def test_auto_numbering_stays_valid(self):
        d, repo = project(self)
        env = {"BRAIN_USER": "carol"}
        run_cli(repo, ["intent", "add", "--source", "debt", "--title", "typo", "--body", "b", "--key",
                       "PAY-12345678"], env=env)
        code, out = run_cli(repo, ["intent", "add", "--source", "debt", "--title", "next", "--body", "b2"], env=env)
        key = out.split("登记了 ", 1)[-1].split("（", 1)[0]
        self.assertTrue(JIRA_RE.match(key), "the next intent got the invalid key %r" % key)


# ---------- N27: reflect --draft is read-only ----------

class N27DraftReadOnly(unittest.TestCase):
    def setUp(self):
        self.d, self.repo = project(self)
        sh("git", "-C", self.repo, "switch", "-q", "-c", "ai/alice/PAY-1-refund")
        commit_all(self.repo, {"runs/PAY-1/run.json": json.dumps({"key": "PAY-1", "intent": "261102-pay-1-refund"}),
                               "aidlc-docs/261102-pay-1-refund/decisions.md": "# Decisions\n"}, "PAY-1: start")
        sh("git", "-C", self.repo, "switch", "-q", "main")
        self.bare = os.path.join(self.d, "origin.git")
        sh("git", "init", "-q", "--bare", self.bare)
        sh("git", "-C", self.repo, "remote", "add", "origin", self.bare)
        sh("git", "-C", self.repo, "push", "-q", "origin", "main", "ai/alice/PAY-1-refund")

    def refs(self):
        return set(sh("git", "-C", self.repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout.split("\n"))

    def test_run_only_on_origin_changes_no_refs(self):
        sh("git", "-C", self.repo, "branch", "-q", "-D", "ai/alice/PAY-1-refund")
        sh("git", "-C", self.repo, "update-ref", "-d", "refs/remotes/origin/ai/alice/PAY-1-refund")
        before = self.refs()
        fetch_head = os.path.join(self.repo, ".git", "FETCH_HEAD")
        had_fetch_head = os.path.exists(fetch_head)
        code, out = run_cli(self.repo, ["reflect", "--draft", "PAY-1"], env={"BRAIN_USER": "carol"})
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(self.refs() - before), [], "--draft changed refs")
        self.assertEqual(os.path.exists(fetch_head), had_fetch_head, "--draft fetched")
        self.assertIn("git -C %s fetch origin +refs/heads/ai/alice/PAY-1-refund:refs/remotes/origin/ai/alice/"
                      "PAY-1-refund" % self.repo, out)                    # the person fetches, not --draft
        self.assertIn("- ref: origin/ai/alice/PAY-1-refund", out)
        self.assertIn("- intent: 261102-pay-1-refund", out)             # the objects are still here

    def test_local_branch_is_used_without_contacting_origin(self):
        before = self.refs()
        os.rename(self.bare, self.bare + ".gone")                        # origin unreachable: still works
        code, out = run_cli(self.repo, ["reflect", "--draft", "PAY-1"], env={"BRAIN_USER": "carol"})
        self.assertEqual(code, 0, out)
        self.assertIn("- ref: origin/ai/alice/PAY-1-refund", out)        # the remote-tracking copy comes first
        self.assertEqual(self.refs(), before)


# ---------- golden: snapshot hygiene ----------

class N4EnvFiles(unittest.TestCase):
    FILES = {".envrc": "export %s\n" % PASSWORD_LINE, ".env_local": PASSWORD_LINE + "\n",
             ".env-prod": PASSWORD_LINE + "\n", "config/.env.prod": PASSWORD_LINE + "\n",
             ".env.example": "DB_" + "PASSWORD=\n", "src/app.py": "print('hi')\n"}

    def test_env_star_files_are_not_in_the_snapshot(self):
        d, repo = project(self, self.FILES)
        out = os.path.join(d, "snap")
        man = G.snapshot(repo, head(repo), out)
        removed = {r["path"]: r["why"] for r in man["removed"]}
        for name in (".envrc", ".env_local", ".env-prod", "config/.env.prod", ".env.example"):
            self.assertEqual(removed.get(name), "env file", name)
            self.assertFalse(os.path.lexists(os.path.join(out, *name.split("/"))), name)
        self.assertEqual(grep(out, "supersecret"), [])

    def test_other_secret_files_are_dropped(self):
        files = {".pgpass": "db:5432:*:u:x\n", "ops/.my.cnf": "[client]\nuser=u\n", "web/.htpasswd": "u:x\n",
                 ".docker/config.json": "{}\n", ".aws/credentials": "[default]\n", ".kube/config": "apiVersion: v1\n",
                 "certs/client.p12": "x", "certs/app.jks": "x", ".vault-token": "x\n", ".s3cfg": "[default]\n",
                 ".yarnrc.yml": "npm" + "AuthToken: abc\n", "docs/.yarnrc.yml": "nodeLinker: pnp\n"}
        d, repo = project(self, files)
        out = os.path.join(d, "snap")
        man = G.snapshot(repo, head(repo), out)
        removed = {r["path"] for r in man["removed"]}
        self.assertEqual(removed, set(files) - {"docs/.yarnrc.yml"})
        self.assertTrue(os.path.exists(os.path.join(out, "docs", ".yarnrc.yml")))

    def test_password_assignment_refuses_the_snapshot(self):
        real = G._extract
        for rel, text, refused in (("deploy/app.env", PASSWORD_LINE + "\n", True),
                                   ("deploy/run.sh", "export " + PASSWORD_LINE + "\n", True),
                                   ("config/app.yml", 'SECRET_KEY: "' + "abc" + 'defghij"\n', True),
                                   ("config/app.ini", "API_TOKEN = 'abcd" + "efgh'\n", True),
                                   ("compose.yml", "  - DB_" + "PASSWORD=${DB_PASSWORD}\n", False),
                                   ("conf.cfg", "TOKEN_TTL = 3600\nPASSWORD_MIN = 8\nAPI_TOKEN = None\n", False),
                                   ("tests/fixture.py", 'SECRET_KEY = "' + "abc" + 'defghij"\n', False),   # code
                                   ("README.md", "export API_" + "TOKEN=your-token-here\n", False)):     # docs
            def extract(tar, out, rel=rel, text=text):
                real(tar, out)
                p = os.path.join(out, *rel.split("/"))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(text)
            d, repo = project(self)
            out = os.path.join(d, "snap")
            with self.subTest(rel), mock.patch.object(G, "_extract", extract):
                if refused:
                    with self.assertRaises(BrainError) as cm:
                        G.snapshot(repo, head(repo), out)
                    self.assertIn("%s (password assignment)" % rel, str(cm.exception))
                    self.assertNotIn("supersecret", str(cm.exception))
                    self.assertFalse(os.path.exists(out))
                else:
                    G.snapshot(repo, head(repo), out)


class N21Gitmodules(unittest.TestCase):
    def test_no_remote_and_no_credential_in_the_snapshot(self):
        url = "https://oauth2:" + "secretpassword123" + "@gitlab.example.com/grp/lib.git"
        d, repo = project(self, {".gitmodules": '[submodule "lib"]\n\tpath = lib\n\turl = %s\n' % url})
        sh("git", "-C", repo, "update-index", "--add", "--cacheinfo", "160000,%s,lib" % ("1" * 40))
        sh("git", "-C", repo, "commit", "-q", "-m", "submodule")
        out = os.path.join(d, "snap")
        man = G.snapshot(repo, head(repo), out)
        self.assertEqual(grep(out, "secretpassword123"), [])
        self.assertIn({"path": ".gitmodules", "why": "git metadata (submodule remotes)"}, man["removed"])
        self.assertEqual(man["submodules"], ["lib"])                      # its content is not in the snapshot


class N22SymlinkEscape(unittest.TestCase):
    def links(self, test, pairs):
        d, repo = project(test)
        for name, target in pairs:
            os.symlink(target, os.path.join(repo, name))
        sh("git", "-C", repo, "add", "-A")
        sh("git", "-C", repo, "commit", "-q", "-m", "links")
        return d, repo

    def test_link_through_a_link_cannot_leave_the_snapshot(self):
        d, repo = self.links(self, [("up", "."), ("esc", "up/..")])
        out = os.path.join(d, "snap")
        with self.assertRaises(BrainError) as cm:
            G.snapshot(repo, head(repo), out)
        self.assertIn("esc", str(cm.exception))
        self.assertFalse(os.path.exists(out))

    def test_link_resolved_after_a_later_link(self):
        # a -> b/x/../.. looks inside while b does not exist yet; b -> up (-> .) makes it point at the parent
        d, repo = self.links(self, [("a", "b/x/../.."), ("b", "up"), ("up", ".")])
        with self.assertRaises(BrainError):
            G.snapshot(repo, head(repo), os.path.join(d, "snap"))

    def test_links_inside_are_kept(self):
        d, repo = self.links(self, [("docs", "brain"), ("t", "brain/TECH.md")])
        out = os.path.join(d, "snap")
        G.snapshot(repo, head(repo), out)
        self.assertEqual(os.readlink(os.path.join(out, "t")), "brain/TECH.md")


class N23SnapshotInsideARepo(unittest.TestCase):
    def task(self, d, base):
        tdir = os.path.join(d, "tasks", "G-1")
        os.makedirs(os.path.join(tdir, "hidden"))
        with open(os.path.join(tdir, "hidden", "t.py"), "w") as f:
            f.write("x\n")
        with open(os.path.join(tdir, "task.json"), "w") as f:
            json.dump({"id": "G-1", "base_sha": base, "intent": "x", "acceptance": ["AC-1"],
                       "hidden_tests": ["hidden/t.py"], "test_command": ["true"]}, f)
        return tdir

    def test_snapshot_under_the_project_is_refused(self):
        d, repo = project(self)
        base = head(repo)
        sh("git", "-C", repo, "remote", "add", "origin", "https://gitlab.example.com/grp/proj.git")
        commit_all(repo, {"src/fix.py": "the later fix\n"}, "PAY-1 the fix the hidden test checks")
        code, out = run_cli(repo, ["golden", "snapshot", self.task(d, base), "--out", "g1"])
        self.assertEqual(code, 2, out)
        self.assertIn("inside the git work tree", out)
        self.assertFalse(os.path.exists(os.path.join(repo, "g1")))

    def test_snapshot_inside_another_repo_is_refused(self):
        d, repo = project(self)
        other = git_repo(os.path.join(d, "other"))
        with self.assertRaises(BrainError):
            G.snapshot(repo, head(repo), os.path.join(other, "deep", "snap"))
        self.assertFalse(os.path.exists(os.path.join(other, "deep")))
        G.snapshot(repo, head(repo), os.path.join(d, "fine", "snap"))   # beside the repo: fine


# ---------- N9: ablation removes the ablated knowledge ----------

class N9AblationLeak(unittest.TestCase):
    def test_no_tech_snapshot_holds_no_tech_entry_text(self):
        docs = "aidlc-docs/261102-pay-1-retry/"
        decisions = ("# Decisions\n\n| ID | Date | Stage | Decision | Why | Who |\n|---|---|---|---|---|---|\n"
                     "| D-1 | 2026-11-02 | code | 重试最多 3 次（T-PAY-1-2） | CI 不稳 | agent |\n"
                     "| D-2 | 2026-11-02 | code | 单号按 P-PAY-1-1 | 样例 | agent |\n\n"
                     "```brain-proposal\nfile: TECH\ntype: pitfall\nrel: new\nsrc: D-1\n---\n%s\n```\n\n"
                     "```brain-proposal\nrel: adds-to T-PAY-1-1\nsrc: D-1\n---\n退款单号是 RF + 9 位。旧单 7 位。\n```\n\n"
                     "```brain-proposal\nfile: PRODUCT\ntype: model\nsrc: D-2\n---\n退款单有状态。\n```\n" % RULE)
        reflected = "```brain-proposal\nfile: tech\ntype: pitfall\nsrc: D-1\n---\n%s\n```\n" % RULE
        d, repo = project(self, {docs + "decisions.md": decisions, "runs/PAY-1/reflect-proposals.md": reflected,
                                 "src/retry.py": "MAX = 3\n"})
        out = os.path.join(d, "snap")
        man = G.snapshot(repo, head(repo), out, variant="no-TECH")
        self.assertNotIn("TECH.md", man["brain_files"])
        self.assertEqual(grep(out, "重试最多 3 次"), [])
        self.assertEqual(grep(out, "T-PAY-1-"), [])
        with open(os.path.join(out, *(docs + "decisions.md").split("/")), encoding="utf-8") as f:
            kept = f.read()
        self.assertIn("| D-2 | 2026-11-02 | code | 单号按 P-PAY-1-1 |", kept)     # other files' knowledge stays
        self.assertIn("退款单有状态。", kept)
        self.assertEqual(man["scrubbed"], [{"path": docs + "decisions.md", "blocks": 2, "lines": 1},
                                           {"path": "runs/PAY-1/reflect-proposals.md", "blocks": 1, "lines": 0}])
        full = G.snapshot(repo, head(repo), os.path.join(d, "full"))
        self.assertEqual(full["scrubbed"], [])
        self.assertEqual(grep(os.path.join(d, "full"), "T-PAY-1-2"), [docs + "decisions.md", "brain/TECH.md"])


# ---------- N10, N24: scoring and the drop rule ----------

class N10ZeroTests(unittest.TestCase):
    TEST = ("import unittest\n\nclass T(unittest.TestCase):\n    def test_limit(self):\n"
            "        from src.refund import limit\n        self.assertEqual(limit(), 100)\n")

    def test_no_test_ran_is_an_error_not_a_score(self):
        d, repo = project(self, {"src/refund.py": "def limit():\n    return 100\n"})
        tdir = os.path.join(d, "tasks", "G-1")
        os.makedirs(os.path.join(tdir, "hidden", "tests"))
        with open(os.path.join(tdir, "hidden", "tests", "test_refund.py"), "w") as f:
            f.write(self.TEST)
        with open(os.path.join(tdir, "task.json"), "w") as f:      # no tests/__init__.py: discovery finds nothing
            json.dump({"id": "G-1", "base_sha": head(repo), "intent": "退款上限", "acceptance": ["AC-1"],
                       "hidden_tests": ["hidden/tests/test_refund.py"], "test_command": [PY, "-m", "unittest"]}, f)
        task = G.load_task(tdir)
        work = os.path.join(d, "work")
        G.snapshot(repo, head(repo), work)
        res = G.run([{"job_id": "G-1/full/1", "task": "G-1", "variant": "full", "repeat": 1}],
                    lambda job: work, [task])[0]
        self.assertIsNone(res["score"], res)
        self.assertIn("no test ran", res["error"])

    def test_counts(self):
        self.assertEqual(G.parse_counts("Ran 0 tests in 0.000s\n\nOK\n", 0), (0, 0, "unittest"))
        self.assertEqual(G.parse_counts("Ran 0 tests in 0.000s\n\nNO TESTS RAN\n", 5), (0, 0, "unittest"))
        self.assertEqual(G.parse_counts("==== no tests ran in 0.01s ====\n", 5), (0, 0, "pytest"))
        self.assertEqual(G.parse_counts("==== 3 skipped in 0.01s ====\n", 0), (0, 0, "pytest"))
        self.assertEqual(G.parse_counts("Ran 2 tests in 0.1s\n\nOK (skipped=2)\n", 0), (0, 0, "unittest"))


class N24MinDropBoundary(unittest.TestCase):
    def test_drop_equal_to_min_drop_counts(self):
        res = [{"task": "G-1", "variant": "full", "score": 0.3}] * 3 + \
              [{"task": "G-1", "variant": "no-TECH", "score": 0.25}] * 3
        c = G.summarize(res, min_drop=0.05)["comparisons"][0]
        self.assertTrue(c["separated"])
        self.assertEqual(c["drop"], 0.05)
        self.assertEqual(c["verdict"], "drop")
        res = [{"task": "G-1", "variant": "full", "score": 0.3}] * 3 + \
              [{"task": "G-1", "variant": "no-TECH", "score": 0.250001}] * 3
        self.assertEqual(G.summarize(res, min_drop=0.05)["comparisons"][0]["verdict"], "no drop")


# ---------- M1: no deprecated utcnow ----------

class M1Today(unittest.TestCase):
    def test_today_uses_an_aware_utc_clock(self):
        with mock.patch.object(E, "utcnow", create=True,
                               return_value=datetime(2026, 1, 2, 23, 59, tzinfo=timezone.utc)):
            self.assertEqual(E.today(), "2026-01-02")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.assertRegex(E.today(), r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


if __name__ == "__main__":
    unittest.main()
