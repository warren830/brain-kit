"""A split requirement in `brain trace`, `brain eval` and `brain usage` (brainkit/trace.py, evaluate.py, usage.py):
each child keeps only its own records, the parent leaves its children's out and rolls their numbers up.

Two worlds: a synthetic family with exact timestamps (fast; every number checked), and one made end to end with a
real AI-DLC Lean install and `brain split` / `brain start --key` (tests/test_split.py's helpers)."""
import hashlib
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import cli, config, trace, usage  # noqa: E402
from brainkit import evaluate as EV  # noqa: E402
from brainkit.common import load_json  # noqa: E402
from tests import test_split as S  # noqa: E402  (a module, so its TestCase classes are not collected again here)
from tests.helpers import sh, tmpdir  # noqa: E402
from tests.test_trace import commit, git_repo, table  # noqa: E402

BOT = "brain-bot@noreply.invalid"
KIT = {"version": 1, "req_prefix": "PAY", "bot_users": ["brain-bot"], "bot_emails": [BOT]}
TECH = "# TECH\n\n" + "".join("<!-- id:T-PAY-1-%d type:pitfall side:builder rel:new src:D-1 -->\nrule %d\n\n" % (n, n)
                               for n in (1, 2, 3))
INTENT = "261101-pay-1-big"
PB = "ai/alice/PAY-1-big"
DOCS = "aidlc-docs/%s/" % INTENT
AS_OF = "2026-11-06T00:00:00Z"


def audit(events, email, team=None):
    """Lean's hash chain (aidlc_lean.py Project.append), with the clone's team in each record's actor."""
    out, prev = [], "GENESIS"
    for n, (ts, event, data) in enumerate(events, 1):
        rec = {"seq": n, "ts": ts, "event": event, "intent": INTENT,
               "actor": {"git_email": email, "git_name": "n", "os_user": "u", "host": "h", "team": team},
               "data": data, "prev": prev}
        rec["hash"] = hashlib.sha256(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        prev = rec["hash"]
        out.append(json.dumps(rec, ensure_ascii=False))
    return "\n".join(out) + "\n"


def run_json(key, owner, branch, base, at, **child):
    return json.dumps(dict({"schema": 1, "key": key, "tracker": "none", "owner": owner, "branch": branch,
                            "intent": INTENT, "brain_commit": base, "kit_version": "0.1.0", "origin": "local",
                            "expected_paths": [], "materials": [], "created_at": at, "claim_checked": True,
                            "handoff": None}, **child))


def child_rec(unit, clone, **more):
    return dict({"parent": "PAY-1", "unit": unit, "target_branch": PB, "lean_team": unit, "lean_clone": clone}, **more)


def hand_off(repo, key, owner, branch, base, created, unit, clone, at, **more):
    """A child's `brain handoff`: its run.json records the request to merge into the parent's branch ("mr")."""
    return commit(repo, {"runs/%s/run.json" % key: run_json(key, owner, branch, base, created, **child_rec(
        unit, clone, mr={"requested_at": at, "by": owner, "local_head": "0" * 40}, **more))},
        "%s: hand off" % key, at, "%s@example.com" % owner)


ROW1 = ["D-1", "2026-11-01", "application-design", "三个单元，按 T-PAY-1-1", "设计", "agent"]
ROW2 = ["D-2", "2026-11-02", "code-generation", "api 用 T-PAY-1-3 的重试", "", "agent"]
ROW3 = ["D-3", "2026-11-03", "code-generation", "web 只显示状态", "", "agent"]
REQ = "# Requirements\n\n## Assumptions\n- A-1: 只退原支付方式\n"
PARENT_EVENTS = [("2026-11-01T00:00:00.000Z", "INTENT_CREATED", {"title": "big"}),
                 ("2026-11-01T00:10:00.000Z", "STAGE_COMPLETED", {"stage": "requirements", "unit": None,
                                                                  "note": "AC-1..3"}),
                 ("2026-11-01T00:20:00.000Z", "APPROVED", {"kind": "REQUIREMENTS", "words": "approve REQUIREMENTS x"}),
                 ("2026-11-01T00:30:00.000Z", "STAGE_COMPLETED", {"stage": "application-design", "unit": None,
                                                                  "note": "three units"}),
                 ("2026-11-01T00:40:00.000Z", "STAGE_COMPLETED", {"stage": "units", "unit": None, "note": "api, web"}),
                 ("2026-11-01T00:50:00.000Z", "APPROVED", {"kind": "DESIGN", "words": "approve DESIGN y"})]
API_EVENTS = [("2026-11-02T00:00:00.000Z", "SESSION_START", {"session": "a1"}),
              ("2026-11-02T01:00:00.000Z", "STAGE_STARTED", {"stage": "code-generation", "unit": "api"}),
              ("2026-11-02T01:10:00.000Z", "BLOCKED", {"tool": "fs_write", "path": "web/x.js", "gate": "paths"}),
              ("2026-11-02T01:20:00.000Z", "VERIFY", {"ok": False, "results": [{"cmd": "pytest", "exit": 1}]}),
              ("2026-11-02T01:30:00.000Z", "VERIFY", {"ok": True, "results": [{"cmd": "pytest", "exit": 0}]}),
              ("2026-11-02T01:40:00.000Z", "STAGE_COMPLETED", {"stage": "code-generation", "unit": "api",
                                                               "note": "api built"})]
WEB_EVENTS = [("2026-11-03T01:00:00.000Z", "STAGE_STARTED", {"stage": "code-generation", "unit": "web"}),
              ("2026-11-04T12:00:00.000Z", "HUMAN_TURN", {"session": "w1", "turn": 1, "sha256": "0" * 64, "chars": 9,
                                                          "allow_protected": True})]


def family_world(test, d=None):
    """main; PAY-1's run branch (Inception); PAY-2 (unit api) built and merged into it, its branch deleted; PAY-3
    (unit web) on its own branch, not merged; PAY-5, a run that is not part of a split."""
    d = d or tmpdir(test)
    repo = git_repo(os.path.join(d, "proj"))
    base = commit(repo, {".brain/kit.json": json.dumps(KIT), "brain/TECH.md": TECH, "README.md": "x\n"}, "init",
                  "2026-10-30T00:00:00Z", "lead@example.com")
    sh("git", "-C", repo, "switch", "-q", "-c", PB)
    split_at = commit(repo, {"runs/PAY-1/run.json": run_json("PAY-1", "alice", PB, base, "2026-11-01T00:00:00Z"),
                             DOCS + "decisions.md": "# Decisions\n\n" + table([ROW1]),
                             DOCS + "inception/requirements/requirements.md": REQ,
                             "aidlc-docs/audit/hostp-aaaaaa.jsonl": audit(PARENT_EVENTS, "alice@example.com")},
                      "PAY-1: inception", "2026-11-01T01:00:00Z", "alice@example.com")
    # PAY-2 (alice, unit api): starts from the parent's branch, builds, merges into it; its branch is deleted
    sh("git", "-C", repo, "switch", "-q", "-c", "ai/alice/PAY-2-api", PB)
    api = [commit(repo, {"runs/PAY-2/run.json": run_json("PAY-2", "alice", "ai/alice/PAY-2-api", base,
                                                         "2026-11-02T00:00:00Z", **child_rec("api", "bbbbbb")),
                         "aidlc-docs/audit/hosta-bbbbbb.jsonl": audit(API_EVENTS[:1], "alice@example.com", "api")},
                  "PAY-2: start run", "2026-11-02T00:00:00Z", "alice@example.com")]
    api.append(commit(repo, {"aidlc-docs/audit/hosta-bbbbbb.jsonl": audit(API_EVENTS, "alice@example.com", "api"),
                             DOCS + "construction/plans/api-code-generation-plan.md": "# Plan\n\nper T-PAY-1-2\n",
                             DOCS + "decisions.md": "# Decisions\n\n" + table([ROW1, ROW2]),
                             "services/api/a.py": "x = 1\n"}, "PAY-2: unit api", "2026-11-02T02:00:00Z",
                      "alice@example.com"))
    api.append(hand_off(repo, "PAY-2", "alice", "ai/alice/PAY-2-api", base, "2026-11-02T00:00:00Z", "api", "bbbbbb",
                        "2026-11-02T03:00:00Z"))
    # PAY-3 (bob, unit web): on its own branch from the parent's, not merged
    sh("git", "-C", repo, "switch", "-q", "-c", "ai/bob/PAY-3-web", split_at)
    web = commit(repo, {"runs/PAY-3/run.json": run_json("PAY-3", "bob", "ai/bob/PAY-3-web", base,
                                                        "2026-11-03T00:00:00Z", **child_rec("web", "cccccc")),
                        "aidlc-docs/audit/hostb-cccccc.jsonl": audit(WEB_EVENTS, "bob@example.com", "web"),
                        DOCS + "construction/web/functional-design/functional-design.md": "# FD\n\nper T-PAY-1-3\n",
                        DOCS + "decisions.md": "# Decisions\n\n" + table([ROW1, ROW3]), "web/x.js": "x\n"},
                 "PAY-3: unit web", "2026-11-04T12:10:00Z", "bob@example.com")
    sh("git", "-C", repo, "switch", "-q", PB)
    sh("git", "-C", repo, "merge", "-q", "--no-ff", "ai/alice/PAY-2-api", "-m",
       "Merge branch 'ai/alice/PAY-2-api' into '%s'" % PB,
       env={"GIT_COMMITTER_DATE": "2026-11-04T00:00:00Z", "GIT_AUTHOR_DATE": "2026-11-04T00:00:00Z",
            "GIT_AUTHOR_EMAIL": "alice@example.com", "GIT_COMMITTER_EMAIL": "alice@example.com"})
    merge = sh("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()
    sh("git", "-C", repo, "branch", "-q", "-D", "ai/alice/PAY-2-api")
    # PAY-5: a run of its own (no split), on main's side
    sh("git", "-C", repo, "switch", "-q", "-c", "ai/alice/PAY-5-solo", "main")
    solo = json.loads(run_json("PAY-5", "alice", "ai/alice/PAY-5-solo", base, "2026-11-05T00:00:00Z"))
    solo["intent"] = "261105-pay-5-solo"
    commit(repo, {"runs/PAY-5/run.json": json.dumps(solo),
                  "aidlc-docs/261105-pay-5-solo/decisions.md": "# Decisions\n\n" + table([ROW1])},
           "PAY-5: start", "2026-11-05T00:00:00Z", "alice@example.com")
    sh("git", "-C", repo, "switch", "-q", "main")
    return types.SimpleNamespace(d=d, repo=repo, cfg=config.load(repo), base=base, split_at=split_at, api=api,
                                 web=web, merge=merge)


def merge(repo, branch, into, at, resolve=None):
    """GitLab's merge commit of `branch` into `into`; `resolve`: {path: text} for the conflicts a person resolves."""
    env = {"GIT_COMMITTER_DATE": at, "GIT_AUTHOR_DATE": at, "GIT_AUTHOR_EMAIL": "alice@example.com",
           "GIT_COMMITTER_EMAIL": "alice@example.com"}
    msg = "Merge branch '%s' into '%s'" % (branch, into)
    sh("git", "-C", repo, "switch", "-q", into)
    p = sh("git", "-C", repo, "merge", "-q", "--no-ff", branch, "-m", msg, env=env, check=not resolve)
    if p.returncode:
        for rel, text in resolve.items():
            with open(os.path.join(repo, rel), "w", encoding="utf-8") as f:
                f.write(text)
            sh("git", "-C", repo, "add", "--", rel)
        sh("git", "-C", repo, "commit", "-q", "-m", msg, env=env)
    return sh("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import shutil
        import tempfile
        d = os.path.realpath(tempfile.mkdtemp(prefix="brainkit-"))
        cls.addClassCleanup(shutil.rmtree, d, True)
        cls.w = family_world(cls, d=d)
        cls.credits = {"runs": {"PAY-2": 3.5, "PAY-3": {"credits": 1.0}}}

    def build(self, key, ref=None, **kw):
        kw.setdefault("as_of", AS_OF)
        return trace.build(self.w.repo, self.w.cfg, key, ref=ref or EV.run_branches(self.w.repo, self.w.cfg)[key],
                           **kw)

    def test_child_merged_into_parent_has_only_its_own(self):
        t = self.build("PAY-2")
        self.assertEqual(t["ref"], "refs/heads/" + PB)                # its branch is gone: read from the parent's
        self.assertEqual([(s["id"], s["state"]) for s in t["stages"]], [("code-generation/api", "done")])
        self.assertEqual(list(t["src"]["lean_audit"]), [trace.audit_labels(
            ["aidlc-docs/audit/hosta-bbbbbb.jsonl"])["aidlc-docs/audit/hosta-bbbbbb.jsonl"]])
        self.assertEqual((len(t["checks"]["verify"]), t["checks"]["verify_failed"], len(t["actions"]["blocked"])),
                         (2, 1, 1))
        self.assertEqual([c["sha"] for c in t["actions"]["commits"]], self.w.api)   # not the parent's history
        self.assertEqual([d["id"] for d in t["decisions"]], ["D-2"])  # the decisions.md line its commit wrote
        self.assertEqual(t["humans"], [])
        self.assertIsNone(t["outcome"]["merged_at"]["value"])         # not on main yet
        f = t["family"]
        self.assertEqual({k: f[k] for k in ("role", "parent", "unit", "target_branch")},
                         {"role": "child", "parent": "PAY-1", "unit": "api", "target_branch": PB})
        m = f["merged_into_parent"]
        self.assertEqual((m["value"], m["ref"]), (True, "refs/heads/" + PB))
        self.assertTrue(m["src"].startswith("refs/heads/%s (%s), the parent's run branch: intents.child_merged" % (
            PB, self.w.merge[:12])), m["src"])
        self.assertIn("*-bbbbbb.jsonl", f["src"]["events"])
        self.assertIn("merge commit", f["src"]["into_parent"])
        self.assertEqual(t["brain"]["cited"], ["T-PAY-1-2"])          # its unit's plan page
        self.assertIn("小需求：PAY-1 的单元 api；已合进 %s" % PB, trace.render(t))

    def test_child_on_its_own_branch(self):
        t = self.build("PAY-3")
        self.assertEqual(t["ref"], "refs/heads/ai/bob/PAY-3-web")
        self.assertEqual([c["sha"] for c in t["actions"]["commits"]], [self.w.web])
        self.assertEqual([(s["id"], s["state"]) for s in t["stages"]], [("code-generation/web", "started")])
        self.assertEqual([(h["kind"], h["judgment_point"]) for h in t["humans"]], [("allow_test_change", False)])
        self.assertEqual([d["id"] for d in t["decisions"]], ["D-3"])
        self.assertEqual(t["family"]["merged_into_parent"]["value"], False)
        self.assertIn("runs/PAY-3/run.json is not there", t["family"]["merged_into_parent"]["src"])
        self.assertEqual(t["brain"]["cited"], ["T-PAY-1-3"])
        self.assertIn("还没合进 %s" % PB, trace.render(t))

    def test_parent_rolls_up_its_children(self):
        t = self.build("PAY-1", credits=self.credits)
        self.assertEqual([(s["id"], s["state"]) for s in t["stages"]],
                         [("requirements", "done"), ("application-design", "done"), ("units", "done")])
        self.assertEqual(len(t["src"]["lean_audit"]), 1)              # the children's ledger files are theirs
        self.assertEqual([c["sha"] for c in t["actions"]["commits"]], [self.w.split_at, self.w.merge])
        self.assertEqual(sorted(d["id"] for d in t["decisions"]), ["A-1", "D-1"])
        self.assertEqual([(h["kind"], h["what"]) for h in t["humans"]],
                         [("approval", "REQUIREMENTS"), ("approval", "DESIGN")])
        self.assertEqual(t["brain"]["cited"], ["T-PAY-1-1", "T-PAY-1-3"])   # decisions.md: the parent's page
        f = t["family"]
        self.assertEqual(f["role"], "parent")
        self.assertEqual([(c["key"], c["unit"], c["merged_into_parent"]["value"]) for c in f["children"]],
                         [("PAY-2", "api", True), ("PAY-3", "web", False)])
        self.assertEqual(f["children"][0]["src"], "trace of PAY-2 read from refs/heads/%s" % PB)
        self.assertEqual(f["children"][1]["src"], "trace of PAY-3 read from refs/heads/ai/bob/PAY-3-web")
        own = {"stages_done": 3, "verify_rounds": 0, "verify_failed": 0, "commits_bot": 0, "commits_person": 2,
               "blocked": 0, "human_at_judgment": 2, "human_outside_judgment": 0, "decisions": 2, "questions": 0,
               "credits": None}
        api = dict(own, stages_done=1, verify_rounds=2, verify_failed=1, commits_person=3, blocked=1,
                   human_at_judgment=0, decisions=1, credits=3.5)
        web = dict(own, stages_done=0, commits_person=1, human_at_judgment=0, human_outside_judgment=1,
                   decisions=1, credits=1.0)
        self.assertEqual(f["rollup"]["own"], own)
        self.assertEqual([c["numbers"] for c in f["children"]], [api, web])
        self.assertEqual(f["rollup"]["total"], {"stages_done": 4, "verify_rounds": 2, "verify_failed": 1,
                                                "commits_bot": 0, "commits_person": 6, "blocked": 1,
                                                "human_at_judgment": 2, "human_outside_judgment": 1, "decisions": 4,
                                                "questions": 0, "credits": 4.5})
        self.assertEqual(f["rollup"]["unknown"], ["PAY-1 credits"])
        self.assertEqual((f["rollup"]["last_activity"]["key"], f["rollup"]["last_activity"]["at"]),
                         ("PAY-3", "2026-11-04T12:10:00Z"))
        self.assertEqual(t["outcome"]["last_activity"]["at"], "2026-11-04T00:00:00Z")   # its own: the merge
        self.assertEqual(set(f["src"]["numbers"]), set(trace.NUMBERS))
        text = trace.render(t)
        self.assertIn("大需求：拆成的小需求有 2 个 run，已合进 1 个", text)
        self.assertIn("  PAY-3 web（没合进）：阶段做完 0，verify 0 次（失败 0），提交 机器人 0 / 人 1", text)
        self.assertIn("  合计（大需求自己 + 小需求）：阶段做完 4，verify 2 次（失败 1）", text)
        # the same trace again: deterministic
        self.assertEqual(json.dumps(t, sort_keys=True), json.dumps(self.build("PAY-1", credits=self.credits),
                                                                   sort_keys=True))

    def test_not_a_split_has_no_family(self):
        t = self.build("PAY-5")
        self.assertNotIn("family", t)
        self.assertEqual([d["id"] for d in t["decisions"]], ["D-1"])
        self.assertNotIn("大需求", trace.render(t))

    def test_eval_states_and_families(self):
        d = EV.collect(self.w.repo, self.w.cfg, as_of=AS_OF, credits=self.credits, stuck_hours=40)
        self.assertEqual(d["open_branches"], {"PAY-1": "refs/heads/" + PB, "PAY-2": "refs/heads/" + PB,
                                              "PAY-3": "refs/heads/ai/bob/PAY-3-web",
                                              "PAY-5": "refs/heads/ai/alice/PAY-5-solo"})
        self.assertEqual({k: v["value"] for k, v in d["states"].items()},
                         {"PAY-1": "open", "PAY-2": "merged_into_parent", "PAY-3": "open", "PAY-5": "open"})
        self.assertFalse(d["states"]["PAY-2"]["stuck"])                 # 2 days without a commit, merged: not stuck
        # PAY-1's own last record is 48 hours old, but PAY-3 moved 36 hours ago: the family is not stuck
        self.assertEqual((d["states"]["PAY-1"]["last_activity"], d["states"]["PAY-1"]["stuck"]),
                         ("2026-11-04T12:10:00Z", False))
        self.assertEqual(d["states"]["PAY-1"]["family_activity"]["key"], "PAY-3")
        self.assertNotIn("PAY-2", [q["key"] for q in d["review_queue"] if any(r["rule"] == "stuck"
                                                                               for r in q["reasons"])])
        rows = {r["key"]: r for r in d["batch"]}
        self.assertEqual((rows["PAY-2"]["parent"], rows["PAY-3"]["parent"]), ("PAY-1", "PAY-1"))
        self.assertNotIn("parent", rows["PAY-1"])
        self.assertNotIn("parent", rows["PAY-5"])
        self.assertEqual(d["families"], [{"key": "PAY-1", "children": ["PAY-2", "PAY-3"], "not_started": [],
                                          "closed": [], "merged": 1, "total": 2, "state": "in_progress",
                                          "judgment": [2, 1], "questions": 0, "credits": 4.5}])
        # every trace is made once: the children's in the family are the ones eval lists
        by = {t["key"]: t for t in d["traces"]}
        self.assertEqual(by["PAY-1"]["family"]["children"][0]["numbers"], trace.numbers(by["PAY-2"]))
        text = EV.report(d)
        self.assertIn("已合进大需求（PAY-1 的小需求）", text)
        self.assertIn("  PAY-1：小需求在做；已合并 1/2（有 run 的 PAY-2、PAY-3）；判断点内 / 外 2 / 1；提问 0；"
                      "credits 4.5", text)
        # the funnel and the judgment metric count each act once
        self.assertEqual(d["judgment"]["value"], [2, 3])

    def test_trace_command_finds_a_child_on_its_parents_branch(self):
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.w.repo)
        try:
            with redirect_stdout(out), redirect_stderr(out):
                rc = cli.main(["trace", "PAY-2", "--json", "--as-of", AS_OF])
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["family"]["role"], "child")

    def test_a_merged_child_is_read_where_it_merged(self):
        # its own branch still there: brain eval and brain trace read it as merged, on the parent's branch
        repo, br = self.w.repo, "ai/alice/PAY-2-api"
        sh("git", "-C", repo, "branch", "-q", br, self.w.api[-1])
        self.addCleanup(sh, "git", "-C", repo, "branch", "-q", "-D", br)
        self.assertEqual(EV.run_branches(repo, self.w.cfg)["PAY-2"], "refs/heads/" + PB)
        t = self.build("PAY-2")
        self.assertEqual([c["sha"] for c in t["actions"]["commits"]], self.w.api)
        self.assertIn("GitLab's merge commit", t["family"]["src"]["into_parent"])
        # a commit after its hand-off that has not merged: not merged into the parent (intents.child_merged)
        tree = sh("git", "-C", repo, "rev-parse", self.w.api[-1] + "^{tree}").stdout.strip()
        more = sh("git", "-C", repo, "commit-tree", tree, "-p", self.w.api[-1], "-m", "PAY-2: more").stdout.strip()
        sh("git", "-C", repo, "update-ref", "refs/heads/" + br, more)
        self.assertEqual(EV.run_branches(repo, self.w.cfg)["PAY-2"], "refs/heads/" + br)
        m = self.build("PAY-2")["family"]["merged_into_parent"]
        self.assertFalse(m["value"])
        self.assertIn("its run branch has commits that are not there", m["src"])

    def test_usage_rows_per_run(self):
        u = usage.collect(self.w.repo, self.w.cfg, ref="refs/heads/" + PB)
        rows = [(r["key"], r.get("parent"), r.get("unit"), r["cited"]) for r in u["runs"]]
        self.assertEqual(rows, [("PAY-1", None, None, ["T-PAY-1-1", "T-PAY-1-3"]),
                                ("PAY-2", "PAY-1", "api", ["T-PAY-1-2"])])
        self.assertEqual({x["id"]: (x["cites"], x["runs"], x["loaded_by"]) for x in u["entries"]},
                         {"T-PAY-1-1": (1, 1, 2), "T-PAY-1-2": (1, 1, 2), "T-PAY-1-3": (1, 1, 2)})
        self.assertIn("PAY-2 %s（PAY-1 的小需求，单元 api，只算它的单元页）" % INTENT, usage.report(u))
        solo = usage.collect(self.w.repo, self.w.cfg, ref="refs/heads/ai/alice/PAY-5-solo")
        self.assertEqual(solo["runs"], [{"intent": "261105-pay-5-solo", "key": "PAY-5", "loaded": 3,
                                         "cited": ["T-PAY-1-1"], "stale": [], "unknown": []}])


class Released(unittest.TestCase):
    """Every child merged into the parent's branch, the parent merged into main, every run branch deleted: the
    children are read from main, their commits found through the parent's merge."""

    @classmethod
    def setUpClass(cls):
        import shutil
        import tempfile
        d = os.path.realpath(tempfile.mkdtemp(prefix="brainkit-"))
        cls.addClassCleanup(shutil.rmtree, d, True)
        w = cls.w = family_world(cls, d=d)
        sh("git", "-C", w.repo, "switch", "-q", "ai/bob/PAY-3-web")
        w.web_mr = hand_off(w.repo, "PAY-3", "bob", "ai/bob/PAY-3-web", w.base, "2026-11-03T00:00:00Z", "web",
                            "cccccc", "2026-11-04T12:30:00Z")
        # both children added a row under D-1: the parent's owner keeps both (git blame gives each its commit)
        merge(w.repo, "ai/bob/PAY-3-web", PB, "2026-11-04T13:00:00Z",
              resolve={DOCS + "decisions.md": "# Decisions\n\n" + table([ROW1, ROW2, ROW3])})
        w.main_merge = merge(w.repo, PB, "main", "2026-11-05T00:00:00Z")
        for b in (PB, "ai/bob/PAY-3-web"):
            sh("git", "-C", w.repo, "branch", "-q", "-D", b)

    def test_children_read_from_main(self):
        w = self.w
        api = trace.build(w.repo, w.cfg, "PAY-2", as_of=AS_OF)
        self.assertEqual([c["sha"] for c in api["actions"]["commits"]], w.api)
        self.assertEqual(api["outcome"]["merged_at"]["commit"], w.main_merge)   # it reached main with its parent
        self.assertEqual(api["family"]["merged_into_parent"]["value"], True)
        self.assertEqual(api["family"]["merged_into_parent"]["ref"], "refs/heads/main")
        self.assertIn("the branch is gone", api["family"]["src"]["commits"])
        self.assertEqual([d["id"] for d in api["decisions"]], ["D-2"])
        parent = trace.build(w.repo, w.cfg, "PAY-1", as_of=AS_OF)
        f = parent["family"]
        self.assertEqual([(c["key"], c["merged_into_parent"]["value"]) for c in f["children"]],
                         [("PAY-2", True), ("PAY-3", True)])
        self.assertEqual(len(parent["actions"]["commits"]), 3)         # inception and the two merges into it
        self.assertEqual(sorted(d["id"] for d in parent["decisions"]), ["A-1", "D-1"])
        self.assertEqual(f["rollup"]["total"]["commits_person"], 3 + 3 + 2)
        self.assertEqual([c["numbers"]["decisions"] for c in f["children"]], [1, 1])     # D-2, D-3
        self.assertEqual(f["rollup"]["total"]["decisions"], 4)
        d = EV.collect(w.repo, w.cfg, as_of=AS_OF)
        self.assertEqual({k: v["value"] for k, v in d["states"].items()},
                         {"PAY-1": "merged", "PAY-2": "merged", "PAY-3": "merged", "PAY-5": "open"})
        self.assertEqual([(r["key"], r["merged"], r["total"], r["state"]) for r in d["families"]],
                         [("PAY-1", 2, 2, "merged")])


class Carried(unittest.TestCase):
    """PAY-6 (unit admin) started from PAY-3's branch (`brain start --on PAY-3`) and merged into the parent's branch
    first: PAY-3's run.json and commit came in with it, but PAY-3 has not merged (no hand-off record there)."""

    @classmethod
    def setUpClass(cls):
        import shutil
        import tempfile
        d = os.path.realpath(tempfile.mkdtemp(prefix="brainkit-"))
        cls.addClassCleanup(shutil.rmtree, d, True)
        w = cls.w = family_world(cls, d=d)
        br = "ai/alice/PAY-6-admin"
        sh("git", "-C", w.repo, "switch", "-q", "-c", br, w.web)
        w.admin = [commit(w.repo, {"runs/PAY-6/run.json": run_json("PAY-6", "alice", br, w.base,
                                                                   "2026-11-05T00:00:00Z",
                                                                   **child_rec("admin", "dddddd", built_on="PAY-3")),
                                   "admin/a.py": "x\n"}, "PAY-6: start run", "2026-11-05T00:00:00Z",
                          "alice@example.com")]
        w.admin.append(hand_off(w.repo, "PAY-6", "alice", br, w.base, "2026-11-05T00:00:00Z", "admin", "dddddd",
                                "2026-11-05T01:00:00Z", built_on="PAY-3"))
        merge(w.repo, br, PB, "2026-11-05T02:00:00Z",
              resolve={DOCS + "decisions.md": "# Decisions\n\n" + table([ROW1, ROW2, ROW3])})
        sh("git", "-C", w.repo, "branch", "-q", "-D", br)
        sh("git", "-C", w.repo, "switch", "-q", "main")

    def test_a_carried_run_json_is_not_a_merge(self):
        w = self.w
        self.assertEqual(EV.run_branches(w.repo, w.cfg)["PAY-3"], "refs/heads/ai/bob/PAY-3-web")
        admin = trace.build(w.repo, w.cfg, "PAY-6", ref="refs/heads/" + PB, as_of=AS_OF)
        self.assertEqual([c["sha"] for c in admin["actions"]["commits"]], w.admin)   # not PAY-3's commit
        self.assertEqual((admin["family"]["merged_into_parent"]["value"], admin["family"]["built_on"]),
                         (True, "PAY-3"))
        parent = trace.build(w.repo, w.cfg, "PAY-1", ref="refs/heads/" + PB, as_of=AS_OF)
        f = parent["family"]
        self.assertEqual([(c["key"], c["merged_into_parent"]["value"], c["src"].rsplit(" ", 1)[-1])
                          for c in f["children"]],
                         [("PAY-2", True, "refs/heads/" + PB), ("PAY-3", False, "refs/heads/ai/bob/PAY-3-web"),
                          ("PAY-6", True, "refs/heads/" + PB)])
        self.assertIn("has no `brain handoff` record (mr)", f["children"][1]["merged_into_parent"]["src"])
        # the commits that came in with PAY-6 are its and PAY-3's, not the parent's; so is PAY-3's decisions.md row
        self.assertEqual(len(parent["actions"]["commits"]), 3)         # inception and the two merges into it
        self.assertEqual(sorted(d["id"] for d in parent["decisions"]), ["A-1", "D-1"])
        d = EV.collect(w.repo, w.cfg, as_of=AS_OF)
        self.assertEqual({k: d["states"][k]["value"] for k in ("PAY-2", "PAY-3", "PAY-6")},
                         {"PAY-2": "merged_into_parent", "PAY-3": "open", "PAY-6": "merged_into_parent"})
        self.assertEqual([(r["key"], r["children"], r["merged"], r["total"]) for r in d["families"]],
                         [("PAY-1", ["PAY-2", "PAY-3", "PAY-6"], 2, 3)])


class Pieces(unittest.TestCase):
    def test_child_fields(self):
        self.assertIsNone(trace.child_of({"key": "PAY-1"}, "PAY-1"))
        self.assertIsNone(trace.child_of({"parent": "PAY-1"}, "PAY-1"))           # not its own parent
        self.assertIsNone(trace.child_of({"parent": "nope"}, "PAY-2"))
        self.assertEqual(trace.child_of(dict(child_rec("api", "XYZ"), built_on="PAY-3"), "PAY-2"),
                         {"parent": "PAY-1", "unit": "api", "target_branch": PB, "lean_team": "api",
                          "lean_clone": None, "built_on": "PAY-3"})

    def test_without_lean_clone_its_team(self):
        tree = types.SimpleNamespace(files=lambda prefix: ["aidlc-docs/audit/h-aaaaaa.jsonl",
                                                           "aidlc-docs/audit/h-bbbbbb.jsonl"])
        ev = [{"_file": "aidlc-docs/audit/h-aaaaaa.jsonl", "actor": {"team": None}, "seq": 1},
              {"_file": "aidlc-docs/audit/h-bbbbbb.jsonl", "actor": {"team": "web_2"}, "seq": 1},
              {"_file": "aidlc-docs/audit/h-bbbbbb.jsonl", "actor": {"team": "api"}, "seq": 2}]
        labels = trace.audit_labels(tree.files(""))
        chains = {x: {"records": 1, "chain": "ok"} for x in labels.values()}
        cf = trace.child_of(dict(child_rec("web.2", None), lean_team=None), "PAY-3")
        test, src = trace._claims(cf)
        self.assertEqual(src, "Lean events of team web_2 (run.json has no lean_clone)")   # Lean's name for the unit
        mine, kept = trace._split_events(tree, ev, chains, mine=test)
        self.assertEqual([e["seq"] for e in mine], [1])
        self.assertEqual(list(kept), [labels["aidlc-docs/audit/h-bbbbbb.jsonl"]])
        rest, _ = trace._split_events(tree, ev, chains, theirs=[test])
        self.assertEqual([e["actor"]["team"] for e in rest], [None, "api"])

    def test_rollup(self):
        a = dict.fromkeys(trace.NUMBERS, 1)
        b = dict(a, credits=None, commits_bot=None)
        total, unknown = trace.rollup([("PAY-1", a), ("PAY-2", b), ("PAY-10", None)])
        self.assertEqual((total["stages_done"], total["credits"], total["commits_bot"]), (2, 1, 1))
        self.assertEqual(unknown[:3], ["PAY-2 commits_bot", "PAY-2 credits", "PAY-10 blocked"])
        self.assertEqual(trace.rollup([("PAY-1", b)])[0]["credits"], None)


class UsagePieces(unittest.TestCase):
    def test_unit_pages(self):
        self.assertEqual([usage.page_unit(p) for p in (
            "construction/plans/api-code-generation-plan.md", "construction/api/functional-design/fd.md",
            "construction/plans/notes.md", "construction/build-and-test/build-and-test-summary.md", "decisions.md",
            "inception/application-design/unit-of-work.md")], ["api", "api", None, None, None, None])

    def test_rows_of(self):
        p, a, b = {"key": "PAY-1"}, {"key": "PAY-2", "parent": "PAY-1", "unit": "api"}, \
            {"key": "PAY-10", "parent": "PAY-1", "unit": "api2"}
        pages = {"decisions.md": {"T-X-1-1": 2}, "construction/plans/api-code-generation-plan.md": {"T-X-1-2": 1},
                 "construction/zz/x.md": {"T-X-1-3": 1}}
        rows = usage._rows_of("i", [b, a, p], pages)
        self.assertEqual([(r["key"], who, ids) for r, who, ids in rows],
                         [("PAY-1", ("i", "PAY-1"), {"T-X-1-1": 2, "T-X-1-3": 1}), ("PAY-2", ("i", "PAY-2"),
                                                                                    {"T-X-1-2": 1}),
                          ("PAY-10", ("i", "PAY-10"), {})])
        # only children here: the other pages are nobody's run (a row of their own, as for an intent without a run)
        rows = usage._rows_of("i", [a], pages)
        self.assertEqual([(r.get("key"), who) for r, who, _ in rows], [(None, "i"), ("PAY-2", ("i", "PAY-2"))])
        # no child: one row, the last run.json read, every page (as before splits)
        rows = usage._rows_of("i", [{"key": "PAY-7"}, {"key": "PAY-8"}], pages)
        self.assertEqual([(r["key"], who, sum(ids.values())) for r, who, ids in rows], [("PAY-8", "i", 4)])


@unittest.skipUnless(S.HAVE_LEAN, "AI-DLC Lean checkout not found")
class RealFamily(unittest.TestCase):
    """End to end with real Lean: alice splits PAY-1 into api / web / jobs; api (alice) is built, handed off
    (`brain handoff`) and merged into PAY-1's branch, its own branch deleted; jobs (bob) is built on its own branch,
    not handed off or merged; web not started."""
    run_as, ok, claim, wt, inception = S.Family.run_as, S.Family.ok, S.Family.claim, S.Family.wt, S.Family.inception
    hand_over = S.Family.hand_over

    def setUp(self):
        self.home = S.isolate(self)
        self.p = S.Project(self)
        self.alice = self.p.clone("alice")
        self.bob = self.p.clone("bob")

    def build_unit(self, wt, unit, acs, files, cite):
        S.lean(wt, "stage", "start", "code-generation", "--unit", unit)
        S.write(wt, "aidlc-docs/%s/construction/plans/%s-code-generation-plan.md" % (self.intent, unit),
                S.PLAN.format(unit=unit, acs=acs).replace("## Steps", "per %s\n\n## Steps" % cite))
        for rel, text in files.items():
            S.write(wt, rel, text)
        S.lean(wt, "stage", "done", "code-generation", "--unit", unit, "--note", "unit %s built" % unit)
        S.commit_all(wt, "%s: unit %s" % (os.path.basename(wt), unit))
        sh("git", "-C", wt, "push", "-q", "origin", "HEAD")

    def trace(self, key):
        rc, out = self.run_as("alice", self.alice, "trace", key, "--json")
        self.assertEqual(rc, 0, out)
        return json.loads(out)

    def test_trace_eval_usage_of_a_real_family(self):
        self.inception()
        self.ok("alice", self.alice, "split", "PAY-1")
        self.ok("alice", self.alice, "start", "--key", "PAY-2")
        self.ok("bob", self.bob, "start", "--key", "PAY-4")
        wt_api, wt_jobs = self.wt("alice", "PAY-2"), self.wt("bob", "PAY-4")
        self.build_unit(wt_api, "api", "AC-1", {"services/api/refunds.py": "def refund():\n    return 201\n"},
                        "T-PAY-7-1")
        self.build_unit(wt_jobs, "jobs", "AC-4", {"jobs/reconcile.py": "def run():\n    return 0\n"}, "T-PAY-8-1")
        S.write(self.alice, "aidlc-docs/%s/inception/application-design/notes.md" % self.intent, "per T-PAY-9-1\n")
        S.commit_all(self.alice, "PAY-1: notes")
        sh("git", "-C", self.alice, "push", "-q", "origin", "HEAD")
        self.hand_over("alice", wt_api, "api")
        self.merge_api()
        sh("git", "-C", self.alice, "fetch", "-q", "--prune", "origin")

        api = self.trace("PAY-2")
        rec = load_json(os.path.join(self.alice, "runs", "PAY-2", "run.json"))
        self.assertEqual(api["family"]["role"], "child")
        self.assertTrue(api["family"]["merged_into_parent"]["value"])
        self.assertEqual(api["family"]["merged_into_parent"]["ref"], "refs/remotes/origin/ai/alice/PAY-1-big")
        self.assertIn("-%s.jsonl" % rec["lean_clone"], api["family"]["src"]["events"])
        self.assertEqual([s["id"] for s in api["stages"]], ["code-generation/api"])
        self.assertEqual(len(api["src"]["lean_audit"]), 1)
        self.assertEqual(api["humans"], [])                          # the approvals were the parent's
        self.assertEqual(api["brain"]["unknown"], ["T-PAY-7-1"])     # its plan page
        self.assertGreaterEqual(len(api["actions"]["commits"]), 3)    # start, unit, hand-off: not the parent's
        self.assertEqual({c["by"] for c in api["actions"]["commits"]}, {"person"})

        jobs = self.trace("PAY-4")
        self.assertFalse(jobs["family"]["merged_into_parent"]["value"])
        self.assertEqual([s["id"] for s in jobs["stages"]], ["code-generation/jobs"])
        self.assertEqual(jobs["brain"]["unknown"], ["T-PAY-8-1"])

        parent = self.trace("PAY-1")
        f = parent["family"]
        self.assertEqual([(c["key"], c["unit"], c["merged_into_parent"]["value"]) for c in f["children"]],
                         [("PAY-2", "api", True), ("PAY-4", "jobs", False)])
        ids = [s["id"] for s in parent["stages"]]
        self.assertNotIn("code-generation/api", ids)
        self.assertIn("units", ids)
        self.assertEqual(sorted(h["what"] for h in parent["humans"]), ["DESIGN", "REQUIREMENTS"])
        self.assertEqual(parent["brain"]["unknown"], ["T-PAY-9-1"])  # the parent's own pages only
        shas = {c["sha"] for c in parent["actions"]["commits"]}
        self.assertFalse(shas & {c["sha"] for c in api["actions"]["commits"]})
        total = f["rollup"]["total"]
        self.assertEqual(total["stages_done"], f["rollup"]["own"]["stages_done"] + 2)
        self.assertEqual(total["human_at_judgment"], 2)
        self.assertEqual(total["commits_person"], len(shas) + len(api["actions"]["commits"]) +
                         len(jobs["actions"]["commits"]))
        self.assertEqual([c["numbers"] for c in f["children"]], [trace.numbers(api), trace.numbers(jobs)])

        # alice's checkout is PAY-1's branch: PAY-1 and PAY-2 are in the tree read; bob's is main: all from branches,
        # PAY-2 (its branch deleted) from PAY-1's
        sh("git", "-C", self.bob, "fetch", "-q", "--prune", "origin")
        for who, where, here in (("alice", self.alice, {"PAY-4"}), ("bob", self.bob, {"PAY-1", "PAY-2", "PAY-4"})):
            rc, out = self.run_as(who, where, "eval", "--json")
            self.assertEqual(rc, 0, out)
            d = json.loads(out)
            self.assertEqual(set(d["open_branches"]), here)
            self.assertEqual({k: d["states"][k]["value"] for k in ("PAY-1", "PAY-2", "PAY-4")},
                             {"PAY-1": "open", "PAY-2": "merged_into_parent", "PAY-4": "open"})
            # web (PAY-3) is filed by brain split, not started: one of the 3 (as brain intent list counts)
            self.assertEqual([(r["key"], r["children"], r["not_started"], r["merged"], r["total"], r["judgment"])
                              for r in d["families"]], [("PAY-1", ["PAY-2", "PAY-4"], ["PAY-3"], 1, 3, [2, 0])])
            self.assertEqual({r["key"]: r.get("parent") for r in d["batch"]},
                             {"PAY-1": None, "PAY-2": "PAY-1", "PAY-4": "PAY-1"})
        self.assertEqual(d["open_branches"]["PAY-2"], "refs/remotes/origin/ai/alice/PAY-1-big")
        rc, out = self.run_as("bob", self.bob, "eval")
        self.assertIn("已合进大需求", out)
        self.assertIn("大需求（拆成小需求的", out)

        rc, out = self.run_as("alice", self.alice, "usage", "--json")
        self.assertEqual(rc, 0, out)
        rows = {r["key"]: r for r in json.loads(out)["runs"] if r["intent"] == self.intent}
        self.assertEqual(sorted(rows), ["PAY-1", "PAY-2"])
        self.assertEqual((rows["PAY-1"]["unknown"], rows["PAY-2"]["unknown"]), (["T-PAY-9-1"], ["T-PAY-7-1"]))

    def merge_api(self):
        """alice merges PAY-2's MR into PAY-1's run branch (GitLab's merge commit) and deletes its branch."""
        a = self.alice
        sh("git", "-C", a, "fetch", "-q", "origin")
        sh("git", "-C", a, "merge", "-q", "--no-ff", "-m", "Merge branch 'ai/alice/PAY-2-api' into "
           "'ai/alice/PAY-1-big'", "origin/ai/alice/PAY-2-api")
        sh("git", "-C", a, "push", "-q", "origin", "ai/alice/PAY-1-big")
        sh("git", "-C", a, "worktree", "remove", "--force", self.wt("alice", "PAY-2"))
        sh("git", "-C", a, "branch", "-q", "-D", "ai/alice/PAY-2-api")
        sh("git", "-C", a, "push", "-q", "origin", "--delete", "ai/alice/PAY-2-api")


if __name__ == "__main__":
    unittest.main()
