"""QA round 2 on the Run Trace and Eval, ported from the reviewer's qa_r2 suite (#4 ... #39), and one test per git
rule of docs/eval.md §2: how the run was merged (M1-M6), which commits are the run's own (C1-C3, and on the default
branch R1-R5), which time is used, how file names are read. Each test names its finding or rule; the docstring says
what was wrong (findings) or what the rule is. Everything is local: temp git repos, no network."""
import ast
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brainkit import evaluate as EV, trace  # noqa: E402
from tests.test_qa_trace_eval import AS_OF, BOT, PARK, World, base_run, release, vid  # noqa: E402

LEAD = "lead@example.com"


def env_at(date, email="alice@example.com", committed=None):
    return {"GIT_COMMITTER_DATE": committed or date, "GIT_AUTHOR_DATE": date, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_EMAIL": email}


def handover_of(w, r):
    return w.git("log", "--format=%H", "--grep=%s: hand off" % r["key"], r["branch"]).stdout.split()[0]


def main_moves_on(w, date="2026-11-02T08:00:00Z"):
    """A lead's commit on main while the run is open. Leaves the repo on main."""
    w.git("switch", "-q", "main")
    return w.commit({"other.txt": "o\n"}, "unrelated", date, LEAD)


def merge_main_in(w, r, by=BOT, msg=None, date="2026-11-02T09:00:00Z"):
    """main merged into the run branch (the runner may not force-push): git's own message, or `msg`."""
    w.git("switch", "-q", r["branch"])
    w.git("merge", "-q", "--no-ff", "--no-edit", "main", *(["-m", msg] if msg else []), env=env_at(date, by))
    return w.rev()


def kinds(t):
    return [h["kind"] for h in t["humans"]]


def collect(w, t, **kw):
    return EV.collect(w.repo, w.cfg(), as_of=AS_OF, traces=[t], **kw)


# ---------- how the run was merged (M1-M6) ----------

class M1MergeCommit(unittest.TestCase):
    def test_15_only_gitlabs_subject_names_the_merge(self):
        """#15: any merge on main's first-parent line whose subject merely QUOTED the branch name was taken for the
        MR merge. Rule M1: only `Merge branch '<branch>' into '<default>'`; a `git pull` merge on the run branch
        (then fast-forwarded) is the run's own commit, so the owner's push after the hand-over stays visible."""
        w, r = base_run(self)
        br, runner_tip = r["branch"], r["tip"]
        w.git("update-ref", "refs/remotes/origin/" + br, runner_tip)
        w.git("switch", "-q", "-c", "owner-local", handover_of(w, r))      # the owner's clone, at the hand-over
        w.commit({"src/owner.py": "x = 5\n"}, "PAY-1: owner tweak", "2026-11-02T08:00:00Z")
        w.git("merge", "-q", "--no-ff", runner_tip, "-m", "Merge branch '%s' of gitlab.example.com:grp/pay into %s"
              % (br, br), env=env_at("2026-11-02T08:10:00Z"))            # what `git pull` (merge) writes
        pulled = w.rev()
        w.git("branch", "-q", "-f", br, pulled)
        w.git("update-ref", "refs/remotes/origin/" + br, pulled)
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--ff-only", br)                              # GitLab fast-forward merge
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        self.assertEqual(t["src"]["git"]["tip"], pulled, t["src"]["git"])
        self.assertEqual(t["outcome"]["merged_at"]["how"], "fast-forward")
        self.assertIn("push_after_handover", kinds(t), t["humans"])
        self.assertEqual(collect(w, t)["autonomy"]["value"], [0, 1])

    def test_15_gitlab_conflict_resolution_merge_is_not_the_mr_merge(self):
        """#15: GitLab's "Resolve conflicts" writes "Merge branch 'main' into '<branch>'"; its second parent (an old
        main commit) became the run's tip."""
        w, r = base_run(self)
        unrelated = main_moves_on(w, "2026-11-02T09:00:00Z")
        tip = merge_main_in(w, r, "alice@example.com", "Merge branch 'main' into '%s'" % r["branch"],
                            "2026-11-02T10:00:00Z")
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--ff-only", r["branch"])
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        g = t["src"]["git"]
        self.assertEqual(g["tip"], tip, g)
        shas = [c["sha"] for c in t["actions"]["commits"]]
        self.assertIn(r["tip"], shas, g)
        self.assertNotIn(unrelated, shas, g)                               # C2: main's commit is not the run's

    def test_m1_merge_commit_with_another_subject_is_found_through_s(self):
        """M1: no GitLab subject, but S (the commit that brought run.json) is a merge commit whose second parent has
        run.json: the run as merged is that second parent; a later commit on the branch is not part of it."""
        w, r = base_run(self)
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--no-ff", r["branch"], "-m", "release refunds", env=env_at("2026-11-03T00:00:00Z", LEAD))
        merge = w.rev()
        w.git("switch", "-q", r["branch"])
        w.commit({"runs/PAY-1/reflect-proposals.md": "x\n"}, "PAY-1: proposals", "2026-11-04T00:00:00Z")
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        m = t["outcome"]["merged_at"]
        self.assertEqual((m["commit"], m["value"], m["how"]), (merge, "2026-11-03T00:00:00Z", "merge commit"), m)
        self.assertEqual(t["src"]["git"]["tip"], r["tip"])
        self.assertIn("has commits after the merge: not part of it", t["src"]["git"]["tip_note"])

    def test_18_m1a_squash_and_merge_commit_keeps_the_branch(self):
        """#18: GitLab's "squash commits" with a merge commit (a squash commit on the merge base, then "Merge branch
        '<branch>' into 'main'") threw the branch away: one commit listed, bob's push after the hand-over gone,
        the run counted as autonomous. Rule M1a: the branch's commits are the run's; branch gone: the squash."""
        w, r = base_run(self, person_after_handover=True)   # bob pushes
        br = r["branch"]
        main_moves_on(w, "2026-11-02T10:00:00Z")
        env = env_at("2026-11-03T00:00:00Z", "alice@example.com")
        env["GIT_COMMITTER_EMAIL"] = LEAD
        base = w.git("merge-base", br, "main").stdout.strip()
        w.git("switch", "-q", "-c", "gitlab-squash", base)
        w.git("merge", "-q", "--squash", br, env=env)
        w.git("commit", "-q", "-m", "PAY-1 refund", env=env)
        squash = w.rev()
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--no-ff", "gitlab-squash", "-m", "Merge branch '%s' into 'main'" % br,
              env=dict(env, GIT_AUTHOR_EMAIL=LEAD))
        merge = w.rev("main")
        w.git("branch", "-q", "-D", "gitlab-squash")
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        m = t["outcome"]["merged_at"]
        self.assertEqual((m["value"], m["commit"]), ("2026-11-03T00:00:00Z", merge), m)
        self.assertIn("squash", m["how"])
        self.assertIn("exactly the branch's changes", m["note"])
        self.assertEqual(len(t["actions"]["commits"]), 5, t["src"]["git"])
        self.assertIn("push_after_handover", kinds(t))
        self.assertEqual(collect(w, t)["autonomy"]["value"], [0, 1])
        w.git("branch", "-q", "-D", br)                                    # a clone without the branch
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        self.assertEqual(([c["sha"] for c in t["actions"]["commits"]], t["outcome"]["merged_at"]["commit"]),
                         ([squash], merge))


class M2FastForward(unittest.TestCase):
    def test_16_owner_clone_with_a_stale_local_branch(self):
        """#16: after a fast-forward merge the owner's local branch, still at the hand-over commit, was taken for
        the merge: merged_at at the hand-over, the runner's later commits and park question dropped, the runner's
        own "PAY-1: fix failing test" counted as a hotfix. Rule M2: the run continues on main after the branch's
        last commit there (R1-R5), the same as on a clone without the branch."""
        w, r = base_run(self, park=PARK)
        w.commit({"src/fix.py": "y\n"}, "PAY-1: fix failing test", "2026-11-02T08:00:00Z", BOT)
        handover = handover_of(w, r)
        w.merge(r, "ff", date="2026-11-03T00:00:00Z")
        w.git("branch", "-q", "-D", r["branch"])                           # a teammate's clone
        mate = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        w.git("branch", "-q", r["branch"], handover)                       # the owner's clone
        owner = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        for t in (mate, owner):
            with self.subTest(clone="owner" if t is owner else "teammate"):
                o = t["outcome"]
                self.assertEqual(o["merged_at"]["value"], "2026-11-02T08:00:00Z", (o["merged_at"], t["src"]["git"]))
                self.assertIs(o["reverted_or_hotfixed_within_14d"]["value"], False)
                self.assertEqual((len(t["actions"]["commits"]), len(t["actions"]["questions"])), (5, 1))
                d = collect(w, t)
                self.assertEqual((d["delivery"]["by_run"], d["autonomy"]["value"]), ({"PAY-1": "correct"}, [1, 1]))
        self.assertEqual(owner["outcome"]["merged_at"]["how"], "fast-forward")
        self.assertIn("is older: the run continues on", owner["src"]["git"]["tip_note"])

    def test_17_run_that_merged_main_in(self):
        """#17: the stretch stopped at the run branch's own "Merge branch 'main' into <branch>" (a fast-forward puts
        it on main's first-parent line): merged_at moved back and the runner's later "PAY-1: fix failing test" was
        a hotfix. Rules R2 (a merge made on the run branch, or by the bot, is the run's) and M2."""
        w, r = base_run(self)
        main_moves_on(w)
        merge_main_in(w, r)
        w.commit({"src/fix.py": "y\n"}, "PAY-1: fix failing test", "2026-11-02T09:30:00Z", BOT)
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--ff-only", r["branch"])
        for deleted in (False, True):
            if deleted:
                w.git("branch", "-q", "-D", r["branch"])
            with self.subTest(branch_deleted=deleted):
                t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
                o = t["outcome"]
                self.assertEqual(o["merged_at"]["value"], "2026-11-02T09:30:00Z", (o["merged_at"], t["src"]["git"]))
                self.assertIs(o["reverted_or_hotfixed_within_14d"]["value"], False,
                              o["reverted_or_hotfixed_within_14d"]["evidence"])
                self.assertEqual(collect(w, t)["delivery"]["by_run"], {"PAY-1": "correct"})

    def test_20_c2_mains_commits_merged_into_the_branch_are_not_the_runs(self):
        """#20: after a fast-forward merge, main's commits that came into the run branch by merging main in were
        listed as the run's, a lead's commit became a push after the hand-over (autonomy [0, 1]). Rule C2: a
        merge's other parent without runs/<KEY>/run.json brings main's history, not the run's. C1 for a merge
        commit: the history of its first parent is main's."""
        for how in ("merge", "ff"):
            with self.subTest(how=how):
                w, r = base_run(self)
                unrelated = main_moves_on(w)
                merge_main_in(w, r)
                w.commit({"src/fix.py": "y\n"}, "PAY-1: make the tests pass", "2026-11-02T09:30:00Z", BOT)
                w.merge(r, how, date="2026-11-03T00:00:00Z")
                t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
                self.assertEqual(t["outcome"]["merged_at"]["how"], {"merge": "merge commit", "ff": "fast-forward"}[how])
                self.assertNotIn(unrelated, [c["sha"] for c in t["actions"]["commits"]], t["src"]["git"])
                self.assertEqual(len(t["actions"]["commits"]), 6)
                self.assertNotIn("push_after_handover", kinds(t), t["humans"])
                self.assertEqual(collect(w, t)["autonomy"]["value"], [1, 1])

    def test_19_c3_commit_on_the_branch_after_the_merge(self):
        """#19: a commit on the run branch AFTER the merge was ignored only for a merge commit: after a fast-forward
        the run was "not merged", after a squash the commit was a person's push after the hand-over. Rule C3:
        commits on the branch after the merge are not part of it (M1, M2: by ancestry; M4: author date)."""
        for how in ("merge", "ff", "squash"):
            with self.subTest(how=how):
                w, r = base_run(self)
                w.merge(r, how, date="2026-11-03T00:00:00Z")
                w.git("switch", "-q", r["branch"])
                w.commit({"runs/PAY-1/reflect-proposals.md": "x\n"}, "PAY-1: keep the reflector's proposals",
                         "2026-11-04T00:00:00Z", "alice@example.com")
                w.git("switch", "-q", "main")
                t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
                self.assertEqual(t["outcome"]["merged_at"]["value"],
                                 "2026-11-02T07:00:00Z" if how == "ff" else "2026-11-03T00:00:00Z", t["src"]["git"])
                self.assertEqual(len(t["actions"]["commits"]), 4, t["src"]["git"])
                self.assertNotIn("push_after_handover", kinds(t))
                self.assertTrue(t["src"]["git"]["tip_note"], t["src"]["git"])
                d = collect(w, t)
                self.assertEqual((d["delivery"]["by_run"], d["autonomy"]["value"]), ({"PAY-1": "correct"}, [1, 1]))


class M3toM6(unittest.TestCase):
    def test_m6_octopus_merge(self):
        """M6: the branch tip is on main through a merge that M1 does not name (an octopus merge: run.json comes in
        through the third parent): the first first-parent commit that contains the tip is the merge."""
        w = World(self)
        w.git("switch", "-q", "-c", "other", "main")
        w.commit({"other.txt": "o\n"}, "other work", "2026-11-01T00:00:00Z", LEAD)
        w.git("switch", "-q", "main")
        r = w.run("PAY-1", "refund", 2, runner_events=[release()])
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--no-ff", "other", r["branch"], "-m", "release train",
              env=env_at("2026-11-03T00:00:00Z", LEAD))
        merge = w.rev()
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        m = t["outcome"]["merged_at"]
        self.assertEqual((m["commit"], m["how"]), (merge, "merge commit"), m)
        self.assertEqual(len(t["actions"]["commits"]), 4)

    def test_31_m3_rebase_keeps_when_things_happened(self):
        """#31: after a rebase merge the run's copies on main were dated with their committer date (the rebase), so
        a person's push the day before moved into the merge day's period. Dates: a commit's `at` is its author
        date; `committed_at` (new) is the committer date."""
        w, r = base_run(self, person_after_handover=True)   # bob, 11-02 06:00
        main_moves_on(w, "2026-11-02T10:00:00Z")
        w.merge(r, "rebase", date="2026-11-03T00:00:00Z")
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        self.assertIn("rebase", t["outcome"]["merged_at"]["how"])
        self.assertEqual(t["outcome"]["merged_at"]["value"], "2026-11-03T00:00:00Z")
        self.assertEqual([h["at"] for h in t["humans"] if h["kind"] == "push_after_handover"], ["2026-11-02T06:00:00Z"])
        self.assertEqual({c["committed_at"] for c in t["actions"]["commits"]}, {"2026-11-03T00:00:00Z"})
        d = EV.collect(w.repo, w.cfg(), as_of=AS_OF, since="2026-11-03", traces=[t])
        self.assertEqual(d["judgment"]["value"], [0, 0], d["judgment"]["outside"])

    def test_r1_rebased_copy_of_a_persons_fix_is_the_runs(self):
        """R1 over R3: a person's "PAY-1: fix ..." pushed on the branch before the merge is the run's; after a
        rebase merge its copy on main is recognised by patch-id, not taken for a hotfix."""
        w, r = base_run(self)
        w.commit({"src/refund.py": "x = 4\n"}, "PAY-1: fix typo", "2026-11-02T08:00:00Z", "bob@example.com")
        main_moves_on(w, "2026-11-02T10:00:00Z")
        w.merge(r, "rebase", date="2026-11-03T00:00:00Z")
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        self.assertEqual(t["outcome"]["merged_at"]["commit"], w.rev("main"))
        self.assertIs(t["outcome"]["reverted_or_hotfixed_within_14d"]["value"], False)
        self.assertEqual(len(t["actions"]["commits"]), 5)

    def test_4_m4_m5_revert_of_a_squash_merge(self):
        """#4: a revert of the run touches runs/<KEY>/ and aidlc-docs/<intent>/: it was taken for the run's last
        own commit (merged_at moved to it, the revert never evidence). Rule R3: a revert ends the stretch and is
        never the run's. M4 (branch kept, --ref the branch) and M5 (branch gone, --ref the squash commit)."""
        for delete in (True, False):
            with self.subTest(branch_deleted=delete):
                w, r = base_run(self)
                squash = w.merge(r, "squash", date="2026-11-03T00:00:00Z", delete_branch=delete)
                w.git("revert", "--no-edit", squash, env=env_at("2026-11-04T00:00:00Z", LEAD))
                t = trace.build(w.repo, w.cfg(), "PAY-1", ref=squash if delete else r["branch"], as_of=AS_OF)
                m = t["outcome"]["merged_at"]
                o = t["outcome"]["reverted_or_hotfixed_within_14d"]
                self.assertEqual(((m["value"], m["commit"]), o["value"]), (("2026-11-03T00:00:00Z", squash), True),
                                 (m, o))
                self.assertEqual([e["rule"][:7] for e in o["evidence"]], ["reverts"])
                self.assertEqual(collect(w, t)["delivery"]["by_run"], {"PAY-1": "wrong"})

    def test_m_ledger_says_merged_but_git_has_no_merge(self):
        """The last M rule: the bridge ledger says merged, this clone has no merge (not fetched): merged_at is null
        and its note says so."""
        w, r = base_run(self)
        w.ledger.add_run("RUN-PAY-1-1", "grp/pay", "PAY-1", 1, r["tip"], vid("h"), owner="alice")
        w.ledger.set_run("RUN-PAY-1-1", "spooled", "merged")
        w.close_ledger()
        m = trace.build(w.repo, w.cfg(), "PAY-1", ledger_path=w.ledger_path, as_of=AS_OF)["outcome"]["merged_at"]
        self.assertEqual((m["value"], m["bridge_state"]), (None, ["merged"]))
        self.assertIn("the bridge ledger says the MR was merged", m["note"])

    def test_m4_squash_lists_the_branch_and_not_mains_commits(self):
        """M4 and C1: squash merge with the branch kept: the branch's commits are the run's; main's own commits
        (made while the run was open) are not."""
        w, r = base_run(self)
        unrelated = main_moves_on(w)
        merge_main_in(w, r)
        squash = w.merge(r, "squash", date="2026-11-03T00:00:00Z")
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        m = t["outcome"]["merged_at"]
        self.assertEqual((m["commit"], m["how"]), (squash, "squash merge (the branch is still here)"))
        self.assertNotIn(unrelated, [c["sha"] for c in t["actions"]["commits"]])
        self.assertEqual(len(t["actions"]["commits"]), 5)
        self.assertNotIn("push_after_handover", kinds(t))


# ---------- the run's commits on the default branch (R2-R5), branch gone ----------

class Stretch(unittest.TestCase):
    def test_4_r3_persons_hotfix_that_records_itself_in_the_intent(self):
        """#4: a person's hotfix that writes why in the intent's decisions.md (it touches aidlc-docs/<intent>/) was
        the run's last own commit after a fast-forward merge with the branch deleted. Rule R3: a person's commit
        naming the key with a fix word ends the stretch; the bot's "PAY-1: fix ..." stays the run's (R4)."""
        w, r = base_run(self)
        w.commit({"src/fix.py": "y\n"}, "PAY-1: fix failing test", "2026-11-02T08:00:00Z", BOT)
        w.merge(r, "ff", delete_branch=True)
        merged = w.rev("main")
        w.commit({"src/refund.py": "x = 2\n",
                  r["docs"] + "decisions.md": "# Decisions\n\n| ID | Date | Stage | Decision | Why | Who |\n"
                                               "|---|---|---|---|---|---|\n| D-9 | 2026-11-05 | ops | hotfix | "
                                               "rounding | owner |\n"},
                 "hotfix PAY-1 rounding of refunds", "2026-11-05T00:00:00Z", LEAD)
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        o = t["outcome"]["reverted_or_hotfixed_within_14d"]
        self.assertEqual((t["outcome"]["merged_at"]["commit"], o["value"]), (merged, True), (t["outcome"], o))
        self.assertEqual(collect(w, t)["delivery"]["by_run"], {"PAY-1": "wrong"})

    def test_r3_the_runners_own_revert_stays_the_runs(self):
        """R3 / R4: the runner reverting its own attempt inside the run (branch gone after a fast-forward) is the
        run's, not a revert of the run: the merge is the runner's last commit and nothing is counted after it."""
        w, r = base_run(self)
        tried = w.commit({"src/try.py": "t\n"}, "PAY-1: try a cache", "2026-11-02T08:00:00Z", BOT)
        w.git("revert", "--no-edit", tried, env=env_at("2026-11-02T08:10:00Z", BOT))
        last = w.commit({r["docs"] + "notes.md": "x\n"}, "PAY-1: records", "2026-11-02T08:20:00Z", BOT)
        w.merge(r, "ff", delete_branch=True)
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        self.assertEqual(t["outcome"]["merged_at"]["commit"], last, t["outcome"]["merged_at"])
        self.assertIs(t["outcome"]["reverted_or_hotfixed_within_14d"]["value"], False)

    def test_r2_r3_r5_what_ends_the_stretch(self):
        """R2: a person's GitLab conflict merge on the run branch ("... into '<branch>'") is the run's; R5: a
        person's commit that names nothing is the run's only when a run commit follows. After the run, each of
        these ends the stretch, so a later commit that touches the run's records is not the run's: another MR's
        merge commit into main (R2), a commit that touches another run's runs/<KEY2>/ (R3)."""
        w, r = base_run(self)
        main_moves_on(w)
        merge_main_in(w, r, "alice@example.com", "Merge branch 'main' into '%s'" % r["branch"])
        w.commit({"src/fix.py": "y\n"}, "tidy", "2026-11-02T09:10:00Z", "bob@example.com")         # R5: inside
        last = w.commit({r["docs"] + "notes.md": "x\n"}, "PAY-1: records", "2026-11-02T09:30:00Z", BOT)
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--ff-only", r["branch"])
        w.git("branch", "-q", "-D", r["branch"])
        readme = w.commit({"README.md": "y\n"}, "readme", "2026-11-03T00:00:00Z", LEAD)            # R5: after
        late = {r["docs"] + "late.md": "z\n"}
        got = {}
        w.commit({"runs/PAY-2/run.json": "{}\n"}, "PAY-2: start", "2026-11-04T00:00:00Z", "carol@example.com")
        w.commit(late, "late note", "2026-11-04T01:00:00Z", LEAD)
        got["another run"] = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        w.git("reset", "-q", "--hard", readme)
        w.git("switch", "-q", "-c", "feature", "main")
        w.commit({"src/feature.py": "f\n"}, "feature", "2026-11-03T01:00:00Z", LEAD)
        w.git("switch", "-q", "main")
        w.git("merge", "-q", "--no-ff", "feature", "-m", "Merge branch 'feature' into 'main'",
              env=env_at("2026-11-03T02:00:00Z", LEAD))
        w.commit(late, "late note", "2026-11-04T01:00:00Z", LEAD)
        got["another MR"] = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        for why, t in got.items():
            with self.subTest(ends_at=why):
                self.assertEqual(t["outcome"]["merged_at"]["commit"], last, t["outcome"]["merged_at"])
                self.assertEqual(len(t["actions"]["commits"]), 7, t["src"]["git"])
                self.assertEqual(sorted(h["what"] for h in t["humans"] if h["kind"] == "push_after_handover"),
                                 ["code", "code"])                         # alice's conflict merge, bob's tidy


# ---------- times and file names ----------

class TimesAndNames(unittest.TestCase):
    def test_dates_author_date_for_commits_and_park_rows(self):
        """Dates: a commit's `at` (and a park row's) is its author date, `committed_at` its committer date; the
        merge's time is the committer date of the merge commit."""
        w, r = base_run(self)
        w.git("switch", "-q", r["branch"])
        w.commit({"runs/PAY-1/park.json": '{"kind": "clarify", "status": "waiting", "question": "q?"}'},
                 "PAY-1: park", "2026-11-02T07:30:00Z", BOT)
        w.git("commit", "-q", "--amend", "--no-edit", env=env_at("2026-11-02T07:30:00Z", BOT, "2026-11-02T07:45:00Z"))
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        c = t["actions"]["commits"][-1]
        self.assertEqual((c["at"], c["committed_at"]), ("2026-11-02T07:30:00Z", "2026-11-02T07:45:00Z"))
        self.assertEqual([q["at"] for q in t["actions"]["questions"]], ["2026-11-02T07:30:00Z"])
        self.assertEqual(t["outcome"]["merged_at"]["value"], "2026-11-03T00:00:00Z")

    def test_file_names_are_read_with_z_and_no_renames(self):
        """File names: `git log --name-only` without -z quotes a name with a quote or a tab ("runs/PAY-1/a\\"b.md"),
        so it no longer starts with runs/PAY-1/; with rename detection a move lists only the new name. Both are read
        as they are now: a records-only push is "records", a move out of src/ is "code", and a person's commit that
        only touches the run's records is the run's (R4)."""
        w, r = base_run(self)
        w.git("mv", "src/refund.py", "runs/PAY-1/refund-old.py")
        w.git("commit", "-q", "-m", "move", env=env_at("2026-11-02T08:00:00Z"))
        notes = w.commit({'runs/PAY-1/notes "final".md': "x\n"}, "notes", "2026-11-02T08:30:00Z")
        w.merge(r, "ff", delete_branch=True)
        t = trace.build(w.repo, w.cfg(), "PAY-1", as_of=AS_OF)
        self.assertEqual(t["outcome"]["merged_at"]["commit"], notes, t["outcome"]["merged_at"])
        self.assertEqual([h["what"] for h in t["humans"] if h["kind"] == "push_after_handover"], ["code", "records"])


# ---------- Eval ----------

class EvalFindings(unittest.TestCase):
    def test_32_known_wrong_run_waits_before_its_merge_too(self):
        """#32: autonomy counted an accepted, not yet merged run that was rejected once as "wrong" at once, and
        dropped it again when it was merged (wrong_open): the metric went backwards for the same run."""
        as_of = "2026-11-04T00:00:00Z"
        w = World(self)
        r = w.run("PAY-1", "a", 2, runner_events=[release("REJECTED", 8, vid(3)), release("APPROVED", 9, vid(4))])
        before = EV.collect(w.repo, w.cfg(), as_of=as_of)["autonomy"]      # working tree: the run branch
        w.merge(r, "merge", date="2026-11-03T00:00:00Z")
        after = EV.collect(w.repo, w.cfg(), as_of=as_of)["autonomy"]
        self.assertEqual((before["value"], after["value"]), ([0, 0], [0, 0]), (before["runs"], after["runs"]))
        self.assertEqual(before["runs"][0]["correct"], "wrong_open")

    def test_33_local_rejection_of_inception_or_plan(self):
        """#33: a local Lean rejection of INCEPTION or PLAN stayed a knowledge gap although a correction cited the
        decisions.md row that recorded it: Lean has no stage called inception or plan (INCEPTION covers
        requirements and application-design, PLAN covers code-generation). One run per case, all merged."""
        correction = "\n```brain-proposal\nfile: PRODUCT\ntype: correction\nrel: new\nsrc: D-2\n---\n退款不能部分退。\n```\n"
        cases = {"PAY-1": ("REQUIREMENTS", "requirements"), "PAY-2": ("INCEPTION", "requirements"),
                 "PAY-3": ("INCEPTION", "application-design"), "PAY-4": ("PLAN", "code-generation"),
                 "PAY-5": ("PLAN", "code")}
        w = World(self)
        for n, (key, (kind, stage)) in enumerate(sorted(cases.items()), 1):
            rows = [["D-1", "2026-11-02", "requirements", "单号按 T-PAY-1-1", "样例", "agent"],
                    ["D-2", "2026-11-02", stage, "人驳回了 %s：退款不能部分退，已改" % kind, "人驳回", "agent"]]
            reject = ((1, 20), "REJECTED", {"kind": kind, "sha256": "ab" * 32, "words": "reject %s 不行" % kind})
            r = w.run(key, "s%d" % n, 2, decisions=rows, local_events=[reject])
            p = r["docs"] + "decisions.md"
            with open(os.path.join(w.repo, p), encoding="utf-8") as f:
                text = f.read()
            w.commit({p: text + correction}, "%s: correction" % key, "2026-11-02T08:00:00Z", BOT)
            w.merge(r, "merge", date="2026-11-03T0%d:00:00Z" % n)
        d = EV.collect(w.repo, w.cfg(), as_of=AS_OF)
        self.assertEqual({t["key"]: [h["what"] for h in t["humans"] if h["kind"] == "rejection"] for t in d["traces"]},
                         {k: [v[0]] for k, v in cases.items()})
        self.assertEqual([g["title"] for g in EV.gaps(d) if g["kind"] == "knowledge" and "被驳回" in g["title"]], [])

    def test_34_owner_is_in_the_who_contract(self):
        """#34: `owner` (the local steering's Who word, counted as a person) was missing from docs/eval.md's Who
        vocabulary and from the hint `brain eval` prints for an unclassified row."""
        self.assertEqual(trace.classify_who("owner"), ("person", "owner"))
        with open(os.path.join(ROOT, "docs", "eval.md"), encoding="utf-8") as f:
            line = next(x for x in f.read().splitlines() if x.startswith("Who 的写法"))
        self.assertIn("`owner`", line)
        data = {"ref": "working tree", "since": None, "runs": 1, "completed": [], "skipped": [],
                "coverage": {"ledger": True, "vouchers": True, "credits": 0},
                "autonomy": {"value": [0, 0], "runs": [], "rule": ""},
                "delivery": {"value": [0, 0], "pending": [], "wrong_open": [], "unknown_acceptance": []},
                "judgment": {"value": [0, 0], "outside": []}, "repeat_rate": [0, 0], "useful": [0, 0],
                "useful_loaded": [0, 0], "staleness": {"total": 0, "stale": [], "same_term": [], "archived": []},
                "cost": {"credits_total": None, "tokens_why": "", "note": None},
                "funnel": {"total": 1, "counts": {"unclassified": 1}, "weeks": {}, "unanswered_questions": 0,
                           "oracle_answers_excluded": 0,
                           "unclassified": [{"key": "PAY-1", "id": "D-2", "src": "aidlc-docs/x/decisions.md:4"}]},
                "review_queue": []}
        hint = next(x for x in EV.report(data).splitlines() if "没归类：PAY-1" in x)
        self.assertRegex(hint, r"\bowner\b")


class WhoAsCode(unittest.TestCase):
    def test_30_entry_ids_written_as_code(self):
        """#30: `brain `T-PAY-1-1`` (the steering writes entry ids as code) was "unclassified"; so was a pseudonym
        and voucher written as two code spans."""
        for who in ("brain `T-PAY-1-1`", "`brain` `T-PAY-1-1`", "brain `T-PAY-1-1` (pitfall)"):
            with self.subTest(who):
                self.assertEqual(trace.classify_who(who), ("brain", "T-PAY-1-1"))
        self.assertEqual(trace.classify_who("`P-0305` `V-a1e0771735`"), ("person", "P-0305"))
        self.assertEqual(trace.classify_who("`owner`"), ("person", "owner"))
        self.assertEqual(trace.classify_who("`TBD`"), ("unclassified", None))
        text = ("| ID | Date | Stage | Decision | Why | Who |\n|---|---|---|---|---|---|\n"
                "| D-1 | 2026-11-02 | code | 单号按 RF + 9 位 | 条目这么说 | brain `T-PAY-1-1` |\n")
        self.assertEqual([EV.category(d) for d in trace.md_decisions(text, "aidlc-docs/x/decisions.md", [])],
                         ["brain"])


# ---------- the suite itself ----------

def classes_after_main_guard(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    guard, late = None, []
    for node in tree.body:
        if isinstance(node, ast.If) and "__main__" in ast.dump(node.test):
            guard = node.lineno
        elif guard is not None and isinstance(node, ast.ClassDef):
            late.append("%s (line %d)" % (node.name, node.lineno))
    return late


class SuiteHygiene(unittest.TestCase):
    def test_37_no_test_class_after_the_main_guard(self):
        """#37: tests/test_qa_trace_eval.py defined WhoOwnerIsAPerson after `unittest.main()`: run as a script, that
        test never ran."""
        late = {n: classes_after_main_guard(os.path.join(ROOT, "tests", n)) for n in (
            "test_qa_security.py", "test_qa_trace_eval.py", "test_qa_modules.py", "test_qa_r2_trace.py",
            "test_trace.py", "test_eval.py")}
        self.assertEqual({k: v for k, v in late.items() if v}, {})

    def test_39_the_suite_reads_only_its_own_intake_store(self):
        """#39: BadInput.test_n25_odd_field_types_are_not_a_traceback ran `brain trace` without BRAIN_INTAKE_DIR, so it
        read the developer's ~/.brain/intake/vouchers (or `git config brain.intake`)."""
        home = tempfile.mkdtemp(prefix="brainkit-home-")
        self.addCleanup(shutil.rmtree, home, True)
        with open(os.path.join(home, ".gitconfig"), "w") as f:
            f.write("[user]\n\tname = QA\n\temail = qa@example.com\n")
        os.makedirs(os.path.join(home, ".brain", "intake", "vouchers", "RUN-PAY-3-1"))   # "the developer's"
        seen, real = [], trace.voucher_files

        def spy(vdir, key, allowed_signers=None):
            seen.append(vdir)
            return real(vdir, key, allowed_signers)
        env = {k: v for k, v in os.environ.items() if k != "BRAIN_INTAKE_DIR"}
        env["HOME"] = home
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(trace, "voucher_files", spy):
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromName(
                "tests.test_qa_trace_eval.BadInput.test_n25_odd_field_types_are_not_a_traceback").run(result)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertEqual([d for d in seen if d and os.path.realpath(d).startswith(os.path.realpath(home))], [])


if __name__ == "__main__":
    unittest.main()
