"""Round 1 review (k2): start / handoff / install / doctor / claim fixes in the local CLI. One test per finding,
named after its id (R1-nn); each failed before its fix."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainkit import claims, cli, lean  # noqa: E402
from brainkit.common import load_json, write_json  # noqa: E402
from tests.helpers import LEAN_SRC, commit_file, git_repo, sh, tmpdir  # noqa: E402
from tests.test_cli import brain, fake_tools  # noqa: E402

HAVE_LEAN = os.path.exists(os.path.join(LEAN_SRC, "install.sh"))


def env(test, **kv):
    """Set (None: unset) environment variables for one test."""
    for k, v in kv.items():
        old = os.environ.get(k)
        test.addCleanup(lambda k=k, old=old: os.environ.pop(k, None) if old is None else os.environ.__setitem__(k, old))
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def branch(repo):
    return sh("git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def edit_status(repo, intent, status="Status: Construction app: step 1 of 3 done"):
    """Fill in the hand-over note: Status and the first Next item (the template's are not a note)."""
    st = lean.state_path(repo, intent)
    with open(st, encoding="utf-8") as f:
        text = f.read()
    with open(st, "w", encoding="utf-8") as f:
        f.write(text.replace(lean.TEMPLATE_STATUS, status).replace(lean.TEMPLATE_NEXT[0], "Write step 2 of 3"))
    sh("git", "-C", repo, "commit", "-q", "-am", "work")


@unittest.skipUnless(HAVE_LEAN, "AI-DLC Lean checkout not found")
class Project(unittest.TestCase):
    """A project with brain-kit and Lean installed and committed (as tests/test_cli.py LocalFlow, without its tests)."""

    def setUp(self):
        self.d = tmpdir(self)
        env(self, HOME=self.d, GITLAB_TOKEN=None, BRAIN_GITLAB_TOKEN_FILE=os.path.join(self.d, "no-token"),
            BRAIN_USER="alice", BRAIN_INTAKE_DIR=None, AIDLC_LEAN_TEAM=None, BRAIN_MR_VERSION_WAIT="0")
        fake_tools(self)
        self.repo = git_repo(os.path.join(self.d, "proj"))
        commit_file(self.repo, "src/app.py", "print('hi')\n", "init")
        rc, out = brain(self.repo, "install", self.repo, "--lean", LEAN_SRC, "--prefix", "PAY")
        self.assertEqual(rc, 0, out)
        sh("git", "-C", self.repo, "add", "-A")
        sh("git", "-C", self.repo, "commit", "-q", "-m", "install kit")

    def add_origin(self):
        self.origin = os.path.join(self.d, "origin.git")
        sh("git", "init", "-q", "--bare", "-b", "main", self.origin)
        sh("git", "-C", self.repo, "remote", "add", "origin", self.origin)
        sh("git", "-C", self.repo, "push", "-q", "origin", "main")
        return self.origin

    def docs(self, text="# 退款\n\n买家在 14 天内可以退款。\n"):
        d = os.path.join(self.d, "docs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "prd.md"), "w", encoding="utf-8") as f:
            f.write(text)
        return d

    def intake(self, key="PAY-1"):
        store = os.path.join(self.d, "store")
        rc, out = brain(self.repo, "intake", self.docs(), "--key", key, "--classification", "internal", "--consent",
                        "--store", store)
        self.assertEqual(rc, 0, out)
        return store

    def claim_refs(self, repo=None):
        return sh("git", "-C", repo or self.repo, "for-each-ref", "--format=%(refname)", "refs/heads/claim/"
                  ).stdout.split()


class R1_22_PackagePath(Project):
    def test_r1_22_package_given_as_its_path_is_refused_and_the_store_kept(self):
        store = self.intake()
        pkg = os.path.join(store, "packages", "PKG-PAY-1-1")
        files = sorted(os.listdir(pkg))
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1", "--package", pkg, "--store", store)
        self.assertEqual(rc, 2, out)
        self.assertIn("--package takes the package id, not its path: --package PKG-PAY-1-1", out)
        self.assertEqual(sorted(os.listdir(pkg)), files)                   # the store's package is all there
        self.assertEqual(branch(self.repo), "main")
        self.assertEqual(self.claim_refs(), [])                            # refused before claiming
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1", "--package", "PKG-PAY-1-1", "--store", store)
        self.assertEqual(rc, 0, out)

    def test_r1_22_attach_never_copies_a_package_onto_itself(self):
        store = self.intake()
        pkg = os.path.join(store, "packages", "PKG-PAY-1-1")
        with self.assertRaises(cli.BrainError):
            cli.attach_package(self.repo, store, pkg)
        self.assertTrue(os.path.isfile(os.path.join(pkg, "manifest.json")))
        with self.assertRaises(cli.BrainError):
            cli._readonly_copy(pkg, pkg)
        self.assertTrue(os.path.isfile(os.path.join(pkg, "manifest.json")))


class R1_33_ClosedUnclaimed(Project):
    def test_r1_33_start_on_a_closed_unclaimed_intent_says_it_is_closed(self):
        rc, out = brain(self.repo, "intent", "add", "--source", "debt", "--title", "退款表加索引", "--body",
                        "退款表按 order_id 查询很慢，加索引", "--unclaimed")
        self.assertEqual(rc, 0, out)
        self.assertEqual(brain(self.repo, "intent", "close", "PAY-1", "--reason", "不做了")[0], 0)
        rc, out = brain(self.repo, "start", "idx", "--key", "PAY-1")
        self.assertEqual(rc, 2, out)
        self.assertIn("PAY-1 is closed (不做了)", out)
        self.assertNotIn("None", out)
        self.assertEqual(branch(self.repo), "main")


class R1_25_StartChecksRunJson(Project):
    def test_r1_25_paths_run_json_cannot_hold_are_refused_before_the_claim(self):
        for p in ("packages/@acme/pay", "web/app/(shop)/refund", "web/pages/[id].tsx", "src/退款", "src/pay,docs/需求"):
            rc, out = brain(self.repo, "start", "refund", "--paths", p)
            self.assertEqual(rc, 2, out)
            self.assertIn("--paths: ", out)
            self.assertIn("is not a path run.json can hold", out)
            self.assertEqual(branch(self.repo), "main")
            self.assertEqual(self.claim_refs(), [], out)
        rc, out = brain(self.repo, "start", "refund", "--paths", "packages/*acme/pay,src/*")
        self.assertEqual(rc, 0, out)
        self.assertEqual(brain(self.repo, "status")[0], 0)

    def test_r1_25_a_package_of_another_requirement_is_refused_before_the_claim(self):
        store = self.intake("PAY-1")
        rc, out = brain(self.repo, "start", "refund", "--package", "PKG-PAY-1-1", "--store", store)
        self.assertEqual(rc, 2, out)
        self.assertIn("PKG-PAY-1-1 is PAY-1's material package", out)
        self.assertIn("--key PAY-1 --package PKG-PAY-1-1", out)
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-2", "--package", "PKG-PAY-1-1", "--store", store)
        self.assertEqual(rc, 2, out)
        self.assertIn("not PAY-2's", out)
        self.assertEqual((branch(self.repo), self.claim_refs()), ("main", []))
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1", "--package", "PKG-PAY-1-1", "--store", store)
        self.assertEqual(rc, 0, out)
        self.assertEqual(brain(self.repo, "status")[0], 0)


class R1_23_UnfinishedStartPutsBack(Project):
    def setUp(self):
        super().setUp()
        self.add_origin()

    def origin_claims(self):
        return sh("git", "-C", self.repo, "ls-remote", "origin", "refs/heads/claim/*").stdout.split()[1::2]

    def test_r1_23_a_document_gate_failing_gives_the_new_claim_back(self):
        docs = self.docs("# 退款\n\n单笔最多 5,000 元。\n")
        rc, out = brain(self.repo, "start", "refund", "--doc", docs, "--classification", "internal", "--consent",
                        "--store", os.path.join(self.d, "store"))          # forgot --business
        self.assertEqual(rc, 2, out)
        self.assertIn("Gave PAY-1 back (claim/PAY-1 deleted)", out)
        self.assertEqual((self.origin_claims(), self.claim_refs(), branch(self.repo)), ([], [], "main"))
        rc, out = brain(self.repo, "start", "refund", "--doc", docs, "--classification", "internal", "--consent",
                        "--business", "mask", "--store", os.path.join(self.d, "store"))
        self.assertEqual(rc, 0, out)
        self.assertIn("Started PAY-1 on ai/alice/PAY-1-refund", out)        # the same key, the same branch

    def test_r1_23_a_missing_package_is_refused_before_the_claim(self):
        rc, out = brain(self.repo, "start", "refund", "--package", "PKG-PAY-9-1", "--key", "PAY-9",
                        "--store", os.path.join(self.d, "store"))
        self.assertEqual(rc, 2, out)
        self.assertIn("package PKG-PAY-9-1 not found", out)
        self.assertEqual((self.origin_claims(), branch(self.repo)), ([], "main"))

    def test_r1_23_a_failure_on_the_run_branch_removes_it_and_what_it_wrote(self):
        store = self.intake("PAY-1")
        lean_json = os.path.join(self.repo, "aidlc-docs", "lean.json")
        with open(lean_json, encoding="utf-8") as f:
            before = f.read()

        def half_done(repo, slug, title):          # Lean wrote some of its files, then stopped
            commit_dir = os.path.join(repo, "aidlc-docs", "261110-pay-1-refund")
            os.makedirs(commit_dir)
            with open(os.path.join(commit_dir, "aidlc-state.md"), "w") as f:
                f.write("# half\n")
            with open(lean_json, "a") as f:
                f.write("\n")
            raise cli.BrainError("Lean stopped")

        from unittest import mock
        with mock.patch.object(lean, "new_intent", side_effect=half_done):
            rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1", "--package", "PKG-PAY-1-1",
                            "--store", store)
        self.assertEqual(rc, 2, out)
        self.assertIn("Lean stopped", out)
        self.assertIn("Removed branch ai/alice/PAY-1-refund", out)
        self.assertEqual(branch(self.repo), "main")
        self.assertEqual(sh("git", "-C", self.repo, "branch", "--list", "ai/*").stdout.strip(), "")
        self.assertEqual(sh("git", "-C", self.repo, "status", "--porcelain").stdout, "")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "aidlc-docs", "261110-pay-1-refund")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".brain-materials", "PKG-PAY-1-1")))
        with open(lean_json, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(self.origin_claims(), [])
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1", "--package", "PKG-PAY-1-1", "--store", store)
        self.assertEqual(rc, 0, out)
        self.assertEqual(brain(self.repo, "status")[0], 0)

    def test_r1_23_a_claim_that_was_mine_already_stays(self):
        self.assertEqual(brain(self.repo, "claim", "--key", "PAY-3", "--title", "fees")[0], 0)
        rc, out = brain(self.repo, "start", "fees", "--key", "PAY-3", "--package", "PKG-PAY-3-1",
                        "--store", os.path.join(self.d, "store"))
        self.assertEqual(rc, 2, out)
        from unittest import mock
        with mock.patch.object(lean, "new_intent", side_effect=cli.BrainError("Lean stopped")):
            rc, out = brain(self.repo, "start", "fees", "--key", "PAY-3")
        self.assertEqual(rc, 2, out)
        self.assertNotIn("Gave PAY-3 back", out)
        self.assertEqual(self.origin_claims(), ["refs/heads/claim/PAY-3"])
        self.assertEqual(branch(self.repo), "main")


@unittest.skipUnless(HAVE_LEAN, "AI-DLC Lean checkout not found")
class R1_24_GitignoreLastLine(unittest.TestCase):
    def test_r1_24_install_appends_after_a_last_line_without_newline(self):
        d = tmpdir(self)
        env(self, HOME=d, BRAIN_USER="alice")
        fake_tools(self)
        repo = git_repo(os.path.join(d, "p"))
        commit_file(repo, ".gitignore", "node_modules/\n.aidlc-lean/\ndist", "init")      # no final newline
        rc, out = brain(repo, "install", repo, "--lean", LEAN_SRC, "--prefix", "PAY")
        self.assertEqual(rc, 0, out)
        with open(os.path.join(repo, ".gitignore"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertIn("dist", lines)
        self.assertIn(".brain-materials/", lines)
        for p in ("dist/bundle.js", ".brain-materials/x", ".aidlc-lean/x"):
            self.assertEqual(sh("git", "-C", repo, "check-ignore", "-q", p, check=False).returncode, 0, p)
        rc, out = brain(repo, "install", repo, "--lean", LEAN_SRC)               # again: nothing added twice
        with open(os.path.join(repo, ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(f.read().splitlines().count(".brain-materials/"), 1)


@unittest.skipUnless(HAVE_LEAN, "AI-DLC Lean checkout not found")
class R1_30_InstallPrefix(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        env(self, HOME=self.d, BRAIN_USER="alice")
        fake_tools(self)
        self.repo = git_repo(os.path.join(self.d, "p"))
        commit_file(self.repo, "a.py", "x\n", "init")

    def test_r1_30_a_lowercase_prefix_is_taken_in_capitals(self):
        rc, out = brain(self.repo, "install", self.repo, "--lean", LEAN_SRC, "--prefix", "pay")
        self.assertEqual(rc, 0, out)
        self.assertEqual(load_json(os.path.join(self.repo, ".brain", "kit.json"))["req_prefix"], "PAY")
        rc, out = brain(self.repo, "doctor", "--offline")
        self.assertIn("keys PAY-<n>", out)

    def test_r1_30_a_prefix_kit_json_cannot_hold_is_refused_before_anything_is_installed(self):
        rc, out = brain(self.repo, "install", self.repo, "--lean", LEAN_SRC, "--prefix", "pay-ments")
        self.assertEqual(rc, 2, out)
        self.assertIn("req_prefix must be like OMS", out)
        self.assertIn("nothing installed", out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".brain")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".kiro")))


class R1_31_DoctorOffline(Project):
    def test_r1_31_offline_with_an_origin_says_it_did_not_fetch(self):
        rc, out = brain(self.repo, "doctor", "--offline")
        self.assertIn("WARN no origin remote: Brain freshness not checked", out)     # really none
        self.add_origin()
        rc, out = brain(self.repo, "doctor", "--offline")
        self.assertNotIn("no origin remote", out)
        self.assertIn("WARN Brain vs origin/main not checked (--offline: nothing fetched)", out)
        rc, out = brain(self.repo, "doctor")
        self.assertIn("OK   Brain vs origin/main: same", out)


class R1_32_InstallKeepsTheTeamsModel(Project):
    def test_r1_32_install_writes_kit_json_model_into_brain_dev(self):
        kp = os.path.join(self.repo, ".brain", "kit.json")
        kit = load_json(kp)
        other = [m for m in kit["allowed_models"] if m != kit["model"]][0]
        kit["model"] = other
        write_json(kp, kit)
        rc, out = brain(self.repo, "doctor", "--offline")
        self.assertIn("FAIL brain-dev model", out)                   # brain-dev.md still names the default
        rc, out = brain(self.repo, "install", self.repo, "--lean", LEAN_SRC)      # the upgrade path
        self.assertEqual(rc, 0, out)
        rc, out = brain(self.repo, "doctor", "--offline")
        self.assertIn("OK   brain-dev model: %s (want %s)" % (other, other), out)
        md = os.path.join(self.repo, ".kiro", "agents", "brain-dev.md")
        with open(md, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("model: "), 1, text[:300])
        with open(os.path.join(cli.KIT_SRC, ".kiro", "agents", "brain-dev.md"), encoding="utf-8") as f:
            ship = f.read()
        self.assertEqual(text.splitlines()[4:], ship.splitlines()[4:])    # nothing else changed


class R1_102_LeanPin(Project):
    def set_lean_version(self, v):
        p = os.path.join(self.repo, lean.SCRIPT)
        with open(p, encoding="utf-8") as f:
            text = f.read()
        import re
        with open(p, "w", encoding="utf-8") as f:
            f.write(re.sub(r'^VERSION = "[^"]*"', 'VERSION = "%s"' % v, text, count=1, flags=re.M))

    def test_r1_102_doctor_checks_the_lean_pin(self):
        have = lean.version(self.repo)
        self.assertRegex(have, r"^0\.3(\.|$)")
        rc, out = brain(self.repo, "doctor", "--offline")
        self.assertIn("OK   AI-DLC Lean %s (pinned 0.3)" % have, out)
        self.set_lean_version("0.2.0")
        rc, out = brain(self.repo, "doctor", "--offline")
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL AI-DLC Lean 0.2.0 installed, pinned 0.3", out)
        self.set_lean_version("0.30.0")                                  # not the 0.3 line
        self.assertIn("FAIL AI-DLC Lean 0.30.0 installed", brain(self.repo, "doctor", "--offline")[1])
        kp = os.path.join(self.repo, ".brain", "kit.json")
        kit = load_json(kp)
        kit["pins"]["lean"] = ""
        write_json(kp, kit)
        self.assertIn("OK   AI-DLC Lean 0.30.0 (not pinned)", brain(self.repo, "doctor", "--offline")[1])


class R1_26_PushedNotLabelledExits1(Project):
    def setUp(self):
        super().setUp()
        self.add_origin()

    def ready(self, gitlab=None):
        if gitlab is not None:
            kp = os.path.join(self.repo, ".brain", "kit.json")
            kit = load_json(kp)
            kit["gitlab"] = gitlab
            write_json(kp, kit)
            sh("git", "-C", self.repo, "commit", "-q", "-am", "gitlab")
            sh("git", "-C", self.repo, "push", "-q", "origin", "main")
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1")
        self.assertEqual(rc, 0, out)
        edit_status(self.repo, load_json(os.path.join(self.repo, "runs", "PAY-1", "run.json"))["intent"])

    def pushed(self):
        return sh("git", "-C", self.origin, "log", "-1", "--format=%s", "ai/alice/PAY-1-refund").stdout.strip()

    def test_r1_26_no_token_exits_1_and_says_what_is_missing(self):
        self.ready({"base": "https://gitlab.example.invalid", "project": "grp/repo"})
        rc, out = brain(self.repo, "handoff")
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT labelled: missing a GitLab token on this machine", out)
        self.assertIn("Nothing was handed over", out)
        self.assertEqual(self.pushed(), "PAY-1: hand off to team runner")

    def test_r1_26_gitlab_not_in_kit_json_exits_1(self):
        self.ready()
        rc, out = brain(self.repo, "handoff")
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT labelled: missing gitlab.base in .brain/kit.json; gitlab.project in .brain/kit.json", out)

    def test_r1_26_no_label_exits_1(self):
        self.ready()
        rc, out = brain(self.repo, "handoff", "--no-label")
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT labelled (--no-label): nothing was handed over yet", out)
        self.assertEqual(self.pushed(), "PAY-1: hand off to team runner")


class R1_27_NoPushSaysMaterialsAreNotPublished(Project):
    def test_r1_27_no_push_with_a_package_says_it_is_not_published(self):
        self.add_origin()
        remote = os.path.join(self.d, "intake.git")
        sh("git", "init", "-q", "--bare", "-b", "main", remote)
        store = os.path.join(self.d, "intake")
        sh("git", "clone", "-q", remote, store)
        for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
            sh("git", "-C", store, "config", k, v)
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1", "--doc", self.docs(), "--classification",
                        "internal", "--consent", "--store", store)
        self.assertEqual(rc, 0, out)
        edit_status(self.repo, load_json(os.path.join(self.repo, "runs", "PAY-1", "run.json"))["intent"])
        rc, out = brain(self.repo, "handoff", "--no-push", "--store", store)
        self.assertEqual(rc, 0, out)
        self.assertIn("material package PKG-PAY-1-1 not published to brain-intake", out)
        self.assertIn("run `brain handoff` again without --no-push", out)
        self.assertNotIn("Push ai/alice/PAY-1-refund and add label", out)
        self.assertEqual(sh("git", "-C", remote, "rev-list", "--all").stdout, "")     # nothing published
        rc, out = brain(self.repo, "handoff", "--no-label", "--store", store)            # as the message says
        self.assertEqual(rc, 1, out)
        self.assertIn("packages/PKG-PAY-1-1/manifest.json",
                      sh("git", "-C", remote, "ls-tree", "-r", "--name-only", "main").stdout)

    def test_r1_27_no_push_without_a_package_says_push_and_label(self):
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1")
        self.assertEqual(rc, 0, out)
        edit_status(self.repo, load_json(os.path.join(self.repo, "runs", "PAY-1", "run.json"))["intent"])
        rc, out = brain(self.repo, "handoff", "--no-push")
        self.assertEqual(rc, 0, out)
        self.assertIn("Push ai/alice/PAY-1-refund and add label 'ai-handoff' to its MR", out)


class R1_28_TemplateNext(Project):
    def test_r1_28_template_next_is_not_a_hand_over_note(self):
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1")
        self.assertEqual(rc, 0, out)
        st = lean.state_path(self.repo, load_json(os.path.join(self.repo, "runs", "PAY-1", "run.json"))["intent"])
        with open(st, encoding="utf-8") as f:
            text = f.read()
        with open(st, "w", encoding="utf-8") as f:
            f.write(text.replace(lean.TEMPLATE_STATUS, "Status: Construction: done"))       # Status only
        sh("git", "-C", self.repo, "commit", "-q", "-am", "status only")
        with open(st, encoding="utf-8") as f:
            probs = lean.handover_text_problems(f.read())
        self.assertEqual(probs, ["Session notes' '### Next' is still Lean's template: write what the next session "
                                 "does"])
        rc, out = brain(self.repo, "handoff", "--no-push")
        self.assertEqual(rc, 2, out)
        self.assertIn("is still Lean's template", out)
        edit_status(self.repo, load_json(os.path.join(self.repo, "runs", "PAY-1", "run.json"))["intent"])
        self.assertEqual(brain(self.repo, "handoff", "--no-push")[0], 0)

    def test_r1_28_template_next_is_lean_s_own(self):
        """TEMPLATE_NEXT is what the Lean brain-kit ships writes (its template, and `new --ideation`)."""
        with open(os.path.join(LEAN_SRC, "kit", ".kiro", "aidlc-lean", "templates", "aidlc-state.md"),
                  encoding="utf-8") as f:
            self.assertEqual(lean.next_steps(f.read()), list(lean.TEMPLATE_NEXT[:2]))
        rc, out = brain(self.repo, "start", "refund", "--key", "PAY-1")
        lean.call(self.repo, "new", "idea", "--ideation")
        idea = [d for d in os.listdir(os.path.join(self.repo, lean.DOCS)) if d.endswith("-idea")][0]
        with open(lean.state_path(self.repo, idea), encoding="utf-8") as f:
            self.assertEqual(lean.next_steps(f.read()), list(lean.TEMPLATE_NEXT[2:]))
        self.assertEqual(lean.handover_text_problems("Status: x\n### Next\n1. %s\n2. own step\n"
                                                     % lean.TEMPLATE_NEXT[1]), [])


if __name__ == "__main__":
    unittest.main()
