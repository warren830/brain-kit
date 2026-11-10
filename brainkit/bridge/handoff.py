"""`brain-bridge`: take labelled MRs from people's machines and continue them on the team runner.

  brain-bridge --config bridge.json poll        check MRs labelled for hand-over; sign + queue the good ones
  brain-bridge --config bridge.json dispatch    send ready runs into KiroCrew (admission limits apply)
  brain-bridge --config bridge.json watch       post questions runs left in park.json (decision loop)
  brain-bridge --config bridge.json collect     check answers on the MR and deliver valid ones to the run
  brain-bridge --config bridge.json reconcile   close runs whose MR was merged or closed
  brain-bridge --config bridge.json report      counts: runs, decisions, hand-overs, top rejection reasons
  brain-bridge --config bridge.json loop        everything above except report, every `interval` seconds

What a hand-over must prove (otherwise the MR gets a comment and the 'rejected' label):
  1. branch is <prefix>/<owner>/<JIRA>-<slug>, same project (no forks), targets the default branch;
  2. the person who added the hand-over label is the branch owner (from GitLab's label events), and added it after
     GitLab recorded the MR head as pushed (its MR versions; commit dates are the pusher's and are not used);
  3. the owner is in brain-authz and is not a bot;
  4. the requirement is theirs: Jira assignee + claim status (tracker jira), or claim/<KEY> names them (none);
  5. run.json at the MR head is valid for this branch and was handed off by the owner, and the MR head is the commit
     that wrote it (`brain handoff`'s commit): a commit pushed on top of it is not part of the hand-over, and a
     commit a bot authored or committed (kit bot_emails / bot_users) never is;
  6. the Lean hand-over note (aidlc-state.md) at the MR head is filled in;
  7. the Brain commit exists; material packages are in brain-intake with the recorded hash;
     no other run is open for this requirement.
The kit config is read from the default branch, never from the MR branch.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import signal
import sys
import threading
import time
from datetime import timedelta

from .. import claims, config as kitconfig, lean, runrec, sign
from ..intake import package as intake
from ..common import (BrainError, NetworkError, git, git_net, iso, later, load_json, parse_branch, parse_iso,
                      paths_overlap, plain, utcnow, write_json)
from ..rest import DEFAULT_TOKEN_FILE, GitLab, HttpError, Jira, gl_time, head_pushed_at, jira_user_id, read_token
from .decisions import DecisionLoop
from .delivery import one_line
from .kirocrew import KiroCrew
from .ledger import OPEN_RUN_STATES, Ledger

BRIDGE_DEFAULTS = {
    "projects": [],
    "identities": "",
    "signing_key": "",
    "signer": {},   # {}: sign with signing_key; {"type": "spool", "spool_dir", "allowed_signers"}: a signing service
    "vouchers_dir": "",
    "spool_dir": "",
    "ledger": "",
    "max_inflight": 12,
    "max_per_repo": 3,
    "contract_ttl_hours": 72,
    "interval": 60,
    "publish_vouchers": False,
    "publish_timeout": 60,      # seconds for each git push / pull of the brain-intake clones (vouchers, materials)
    "kit_ttl": 0,               # seconds a project's kit.json from main is used before it is read again (0: interval)
    # GitLab GraphQL (who edited a reply) cannot be read: the answer waits; the MR is told after this many rounds in a
    # row, or this many minutes since the first
    "edit_check_rounds": 3,
    "edit_check_wait_minutes": 30,
    # a hand-over KiroCrew does not take (each send answered with an error, the slot shows it did not land) is sent
    # again each round, then every 5 minutes; after this many minutes the run is failed and the MR says how to hand
    # over again
    "send_give_up_minutes": 60,
    # a run brain-runnerd has neither prepared nor failed (nor tried again) for this long: the MR is told once (the run
    # stays queued; it starts once runnerd prepares it)
    "runnerd_wait_minutes": 90,
    # an acceptance question waits for the MR head's pipeline to pass; one still running after this long: the MR hears
    "acceptance_ci_wait_minutes": 60,
    "intake_dir": "",
    "runner_runs_root": "",
    "kirocrew": {"base": "http://127.0.0.1:5476", "app": "brain-bridge", "secret_file": "",
                 "agent": "brain-run", "memory_mode": "incognito"},
}


def load_bridge_config(path) -> dict:
    """bridge.json. signing_key is needed only when the bridge signs with its own key (no other "signer")."""
    cfg = kitconfig.merged(BRIDGE_DEFAULTS, load_json(path))
    for k in ("identities", "vouchers_dir", "spool_dir", "ledger"):
        if not cfg.get(k):
            raise BrainError("bridge config: %s is required" % k)
    probs = sign.signer_problems(cfg)
    if probs:
        raise BrainError("bridge config: " + "; ".join(probs))
    if not cfg["projects"]:
        raise BrainError("bridge config: projects is empty")
    return cfg


# runnerd could not prepare a workspace (spool/failed/<RUN>.json): how often the bridge asks it again after a failure
# it calls transient (runnerd itself tries such a request up to its own `tries` first), and how long after it gave up
REQUEUES, REQUEUE_AFTER_S = 2, 300
SETUP_TELL_AFTER = 3        # rounds of failed KiroCrew session setup before the MR is told (it is retried each round)
# a run waiting for a person (a question open, being posted or asked) holds no session at work: it does not count
# against max_inflight / max_per_repo. An answer whose delivery is unknown may have landed: that run counts.
WAITING_DECISION_STATES = ("open", "posting", "asking")


def _paths_meet(path, pattern) -> bool:
    """Does a run's expected path (a file, a folder or a glob such as services/api/**, naming everything below it)
    meet a risk-path pattern: could a file it names be under it (riskpaths.meets: brain-ci check-run's rules)?"""
    from ..riskpaths import meets
    return meets(path, pattern)


def _event_order(ev):
    """Sort key of a label event: GitLab's event id (it only grows), then GitLab's time."""
    try:
        n = int(ev.get("id") or 0)
    except (TypeError, ValueError):
        n = 0
    t = gl_time(ev.get("created_at"))
    return n, t.timestamp() if t else float("-inf")


def _users(authz) -> dict:
    """brain-authz `users` by lowercased GitLab username: branch owners are lowercased, and GitLab usernames are
    not case-sensitive (roles and bot_users are compared lowercased too)."""
    users = authz.get("users") or {}
    return {str(k).lower(): v for k, v in users.items()} if isinstance(users, dict) else {}


class Bridge(DecisionLoop):
    def __init__(self, cfg, gitlab, jira, kirocrew=None, out=print, signer=None):
        """`signer`: any object with sign(voucher) -> signature; default the one bridge.json names (sign.py)."""
        self.cfg = cfg
        self.gl = gitlab
        self.jira = jira
        self.kc = kirocrew
        self.out = out
        # a subclass may also bring its signer as a class attribute. A spool signer's allowed_signers is checked
        # when it signs (sign.SpoolSigner.sign): report, dispatch and reconcile run without it.
        self.signer = signer or getattr(self, "signer", None) or sign.signer_from_config(cfg)
        self.ledger = Ledger(cfg["ledger"])
        try:
            authz = load_json(cfg["identities"])
        except (OSError, ValueError) as e:
            raise BrainError("bridge config: identities %s cannot be read: %s" % (cfg["identities"], e))
        self.ids = _users(authz)
        self.roles = authz.get("roles", {})
        self._kit = {}
        self._kit_at = {}
        self.clock = time.monotonic        # the kit cache's clock (tests set their own)
        try:
            self._ids_mtime = os.stat(cfg["identities"]).st_mtime
        except OSError:
            self._ids_mtime = None

    @property
    def send_give_up_s(self) -> float:
        """Seconds a hand-over KiroCrew does not take is sent again before its run is failed (bridge.json
        send_give_up_minutes)."""
        return sign.seconds(self.cfg.get("send_give_up_minutes"), 60.0) * 60

    @property
    def reservation_ttl(self) -> float:
        """Seconds after which a run reservation or a 'checking' hand-over row was left by a bridge that stopped
        mid-way: max(900, 4 x the signer's timeout). A signer whose `timeout` is not a number of seconds (an HTTP
        client's (connect, read) pair, say) gets the default."""
        return max(900.0, 4 * (sign.seconds(getattr(self.signer, "timeout", None)) or 0.0))

    # ---------- config from the default branch ----------

    @property
    def kit_ttl(self) -> float:
        return sign.seconds(self.cfg.get("kit_ttl"), None) or sign.seconds(self.cfg.get("interval"), 60.0)

    def kit(self, project) -> dict:
        """kit.json from the project's default branch, read again once it is kit_ttl old (a change on main takes
        effect within a round or so; brain-authz identities are read again when their file changes)."""
        now = self.clock()
        if project in self._kit and now - self._kit_at.get(project, now) >= self.kit_ttl:
            del self._kit[project]
            self._gitlab_me = None              # kit.json bridge_user is checked against the token again
            self._reload_identities()
        if project not in self._kit:
            default = (self.gl.project(project) or {}).get("default_branch") or ""
            if not default:
                raise BrainError("%s: GitLab reports no default branch" % project)
            raw = self.gl.file_raw(project, kitconfig.KIT_CONFIG, default)
            if raw is None:
                raise BrainError("%s: %s not found on %s" % (project, kitconfig.KIT_CONFIG, default))
            cfg = kitconfig.merged(kitconfig.DEFAULTS, json.loads(raw.decode("utf-8")))
            probs = kitconfig.validate(cfg)
            if cfg["default_branch"] != default:
                probs.append("default_branch %r but GitLab says %r" % (cfg["default_branch"], default))
            if probs:
                raise BrainError("%s: kit config: %s" % (project, "; ".join(probs)))
            self._kit[project] = cfg
            self._kit_at[project] = now
        return self._kit[project]

    def _reload_identities(self):
        try:
            m = os.stat(self.cfg["identities"]).st_mtime
            if m == self._ids_mtime:
                return
            authz = load_json(self.cfg["identities"])
        except (OSError, ValueError) as e:
            self.out("identities %s not read again (the ones read before stay): %s" % (self.cfg["identities"], e))
            return
        self.ids, self.roles, self._ids_mtime = _users(authz), authz.get("roles", {}), m

    # ---------- checks ----------

    def last_label_event(self, project, iid, label):
        """The latest add/remove event on `label` (all pages), or None. Latest by GitLab's event id, then its time;
        the order GitLab lists them in is not relied on."""
        evs = [ev for ev in self.gl.label_events(project, iid) if isinstance(ev, dict)
               and (ev.get("label") or {}).get("name") == label and ev.get("action") in ("add", "remove")]
        return max(evs, key=_event_order) if evs else None

    @staticmethod
    def _adder(ev):
        if not ev or ev.get("action") != "add":
            return "", ""
        return ((ev.get("user") or {}).get("username") or "").lower(), str(ev.get("id", ""))

    def label_adder(self, project, iid, label):
        """(username, event id) of the latest event on `label` when that event is an 'add'; ("", "") otherwise."""
        return self._adder(self.last_label_event(project, iid, label))

    def label_after_push(self, project, kit, mr, ev) -> list:
        """The owner hands over the commit the MR showed when they added the label. A commit pushed after the label
        (by the runner's key, or anyone who may push to ai/*) was not handed over: the label must come later than
        the newest MR version whose head is the MR head. Both times are GitLab's own; commit dates are not used."""
        label, sha = kit["handoff_label"], mr.get("sha", "")
        added = gl_time((ev or {}).get("created_at"))
        if added is None:
            return ["the '%s' label event has no time GitLab recorded" % label]
        pushed = head_pushed_at(self.gl.mr_versions(project, mr["iid"]), sha)
        if pushed is None:
            return ["GitLab shows no MR version with head %s: add the label '%s' again once the push shows on the MR"
                    % (sha[:12], label)]
        if pushed > added:
            return ["%s was pushed (%s) after the label '%s' was added (%s): that commit was not handed over; look at "
                    "it, then run `brain handoff` again (it hands over a new commit on top)"
                    % (sha[:12], iso(pushed), label, iso(added))]
        return []

    def verify(self, project, kit, mr, adder=None, label_event=None):
        """(problems, facts). facts carries what the contract needs. `label_event`: the last hand-over label event,
        when the caller has read it already (with `adder`, its user)."""
        p, f = [], {}
        branch = mr.get("source_branch", "")
        b = parse_branch(branch, kit["branch_prefix"])
        if not b:
            return ["branch %s is not %s/<owner>/<JIRA>-<slug>" % (branch, kit["branch_prefix"])], f
        f.update(b, branch=branch)
        if mr.get("source_project_id") != mr.get("target_project_id"):
            p.append("MR comes from a fork")
        if mr.get("target_branch") != kit["default_branch"]:
            p.append("MR targets %s, not %s" % (mr.get("target_branch"), kit["default_branch"]))
        owner = b["owner"]
        if owner in [u.lower() for u in kit["bot_users"]]:
            p.append("bot branches are not handed over")
        if label_event is None:
            label_event = self.last_label_event(project, mr["iid"], kit["handoff_label"])
        if adder is None:
            adder = self._adder(label_event)[0]
        if adder != owner:
            p.append("label '%s' was added by %s, not by the branch owner %s"
                     % (kit["handoff_label"], adder or "unknown", owner))
        else:
            p += self.label_after_push(project, kit, mr, label_event)
        ident = self.ids.get(owner)
        if ident is None:  # {} is a valid entry: a person without a Jira id (tracker none)
            p.append("%s is not in brain-authz identities" % owner)
        elif kit["tracker"] == "jira":
            p += self.check_jira(kit, b["key"], owner, ident)
        else:
            raw_claim = self.gl.file_raw(project, claims.CLAIM_FILE, "claim/" + b["key"])
            if raw_claim is None:
                p.append("%s has no claim (branch claim/%s); run `brain claim`" % (b["key"], b["key"]))
            else:
                try:
                    p += claims.validate_doc(json.loads(raw_claim.decode("utf-8")), b["key"], owner)
                except ValueError:
                    p.append("claim/%s: claim.json is not JSON" % b["key"])
        sha = mr.get("sha", "")
        raw = self.gl.file_raw(project, runrec.path(kit["runs_dir"], b["key"]), sha)
        if raw is None:
            p.append("run.json missing at %s" % sha[:12])
            return p, f
        try:
            rec = json.loads(raw.decode("utf-8"))
        except ValueError:
            p.append("run.json is not valid JSON")
            return p, f
        rp = runrec.validate(rec, branch=branch, prefix=kit["branch_prefix"])
        p += ["run.json: " + x for x in rp]
        if rp:
            return p, f
        if not rec.get("handoff"):
            p.append("run.json has no hand-over (run `brain handoff`)")
        else:
            p += self.handoff_commit_problems(project, sha, runrec.path(kit["runs_dir"], b["key"]), raw, kit=kit)
        f.update(intent=rec["intent"], brain_commit=rec["brain_commit"], materials=rec.get("materials", []),
                 expected_paths=rec.get("expected_paths", []))
        p += self.check_materials(f["materials"])
        state = self.gl.file_raw(project, "%s/%s/aidlc-state.md" % (lean.DOCS, rec["intent"]), sha)
        if state is None:
            p.append("Lean hand-over note %s/%s/aidlc-state.md missing" % (lean.DOCS, rec["intent"]))
        else:
            team = rec.get("lean_team") if rec.get("parent") else None      # a split child: its own subsection
            p += ["hand-over note: " + x for x in lean.handover_text_problems(state.decode("utf-8", "replace"),
                                                                                team)]
        if not self.gl.commit_exists(project, rec["brain_commit"]):
            p.append("brain_commit %s does not exist" % rec["brain_commit"][:12])
        # a reservation left by a bridge that stopped while signing does not count once it is stale (reserve_run
        # drops it); a fresh one of this very hand-over is the one being signed right now
        other = self.ledger.open_run_for(b["key"], stale_after=self.reservation_ttl)
        if other is not None and not self.ledger.is_reservation_for(other, project, mr["iid"], sha):
            p.append("%s already has an open run on the team runner" % b["key"])
        return p, f

    def handoff_commit_problems(self, project, sha, path, raw, kit=None) -> list:
        """The MR head must be the commit that wrote run.json's hand-over (`brain handoff` commits run.json and
        nothing else on top of the owner's work). A commit someone pushes on top of it - between `brain handoff`'s
        push and its label, say - leaves run.json as it was: it was not handed over, however the label times fall.
        Rule: the head has one parent, run.json there differs from run.json at that parent, and the head was
        neither authored nor committed by a bot (kit bot_emails / bot_users: pre-receive makes every bot commit carry
        a bot email, so a runner's commit that rewrites run.json on top of the hand-over is told apart)."""
        c = self.gl.commit(project, sha)
        parents = c.get("parent_ids") if isinstance(c, dict) else None
        if not isinstance(parents, list) or not parents or not all(isinstance(x, str) for x in parents):
            return ["GitLab does not show the parent of %s: cannot tell that it is the hand-over commit" % sha[:12]]
        if len(parents) != 1:
            return ["the MR head %s is a merge, not the hand-over commit `brain handoff` makes: run it again"
                    % sha[:12]]
        kit = kit if kit is not None else self.kit(project)
        people = [(c.get(k + "_email"), c.get(k + "_name")) for k in ("author", "committer")]
        if not all(isinstance(e, str) and e.strip() for e, _ in people):
            return ["GitLab does not show who authored and committed %s: cannot tell that it is the owner's hand-over "
                    "commit" % sha[:12]]
        bot_emails = {str(x).strip().lower() for x in kit.get("bot_emails") or []}
        bot_users = {str(x).strip().lower() for x in kit.get("bot_users") or []}
        bots = sorted({e.strip().lower() for e, n in people if e.strip().lower() in bot_emails or
                       str(n or "").strip().lower() in bot_users})
        if bots:
            return ["the MR head %s was made by a bot account (%s), not by `brain handoff`: a runner's commit is never "
                    "handed over; look at it, then run `brain handoff` again" % (sha[:12], ", ".join(bots))]
        if self.gl.file_raw(project, path, parents[0]) == raw:
            return ["the MR head %s is not the hand-over commit (it does not change %s): a commit pushed after "
                    "`brain handoff` was not handed over; look at it, then run `brain handoff` again"
                    % (sha[:12], path)]
        return []

    def check_jira(self, kit, key, owner, ident) -> list:
        if not self.jira or not ident.get("jira"):
            return ["tracker is jira but %s has no Jira id in brain-authz (or Jira is not configured)" % owner]
        try:
            fields = (self.jira.issue(key) or {}).get("fields") or {}
        except HttpError as e:
            return ["Jira issue %s: HTTP %d" % (key, e.status)]
        p = []
        if jira_user_id(fields.get("assignee")) != ident["jira"]:
            p.append("%s is not assigned to %s in Jira" % (key, owner))
        status = (fields.get("status") or {}).get("name", "")
        if status not in kit["claim_statuses"]:
            p.append("%s is '%s', not in %s" % (key, status, kit["claim_statuses"]))
        return p

    def check_materials(self, materials) -> list:
        """Every package the run uses is in brain-intake with the manifest hash recorded in run.json."""
        if not materials:
            return []
        d = self.cfg.get("intake_dir")
        if not d:
            return ["the run uses material packages but the bridge has no intake_dir"]
        pulled = self.sync_intake(d) if os.path.isdir(os.path.join(d, ".git")) else ""
        probs = []
        for m in materials:
            pdir = os.path.join(d, "packages", m["package"])
            if not os.path.isdir(pdir):
                probs.append("material %s is not published to brain-intake" % m["package"])
                continue
            if intake.manifest_digest(pdir) != m["manifest_sha256"]:
                probs.append("material %s in brain-intake differs from run.json" % m["package"])
            probs += ["material %s: %s" % (m["package"], x) for x in intake.verify_package(pdir)]
        if probs and pulled:
            # the clone could not be brought up to date: not a reason to reject; checked again next round
            raise NetworkError("brain-intake (%s) could not be updated: %s" % (d, pulled))
        return probs

    def sync_intake(self, d) -> str:
        """Bring the bridge's brain-intake clone `d` up to date with origin: '' or why it could not. The bridge's own
        commits there (vouchers whose push failed) are rebased onto origin, not left to block every later pull (each
        voucher and each package is its own folder, so the rebase meets no conflict), and pushed again: a voucher
        that could not be pushed goes out on a later round. A failed push is not a reason to refuse anything."""
        try:
            p = self._pull(d)
        except NetworkError as e:
            return str(e)
        if p.returncode != 0:
            return (p.stderr or p.stdout).strip()[:300] or "git pull failed"
        ahead = git(d, "rev-list", "--count", "@{u}..HEAD", check=False)
        if ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0"):
            try:
                q = git_net(d, "push", "-q", timeout=self.git_timeout, check=False)
                if q.returncode != 0:
                    self.out("brain-intake (%s): %s local commit(s) not pushed yet: %s" % (
                        d, ahead.stdout.strip(), one_line((q.stderr or q.stdout).strip(), 200)))
            except NetworkError as e:
                self.out("brain-intake (%s): local commits not pushed yet: %s" % (d, e))
        return ""

    def push_pending_vouchers(self):
        """Voucher commits a failed push left in the bridge's clone go out on the next round, also when no hand-over
        or answer publishes again. Nothing to do (no network) when the clone is not ahead of origin."""
        vd = self.cfg.get("vouchers_dir")
        if not self.cfg.get("publish_vouchers") or not vd or not os.path.isdir(vd):
            return
        top = git(vd, "rev-parse", "--show-toplevel", check=False)
        ahead = git(vd, "rev-list", "--count", "@{u}..HEAD", check=False)
        if top.returncode == 0 and ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0"):
            why = self.sync_intake(top.stdout.strip())
            if why:
                self.out("brain-intake: voucher commits not pushed yet: %s" % one_line(why, 200))

    @property
    def git_timeout(self) -> float:
        return sign.seconds(self.cfg.get("publish_timeout"), 60.0)

    def overlaps(self, project, key, paths) -> list:
        """Open runs in the same project whose expected paths overlap these."""
        out = []
        mine = [x.rstrip("/") for x in paths if x]
        for row in self.ledger.runs_in(*OPEN_RUN_STATES):
            if row["project"] != project or row["req"] == key:
                continue
            theirs = [x.rstrip("/") for x in json.loads(row["paths"] or "[]") if x]
            # globs too (a split child's paths are its unit's Paths in unit-of-work.md, e.g. services/api/**)
            both = sorted({a for a in mine for t in theirs if paths_overlap(a, t)})
            if both:
                out.append((row["run_id"], both))
        return out

    # ---------- poll ----------

    def poll(self):
        """Check the labelled MRs of every project. A project whose kit.json or MR list cannot be read does not hold
        up the others (as in dispatch, watch and collect): its error is raised at the end, after the rest. First,
        the comments earlier rounds could not post (tell_mr_due)."""
        errors = self.notes_due_round()
        try:
            self.push_pending_vouchers()
        except Exception as e:  # noqa: BLE001
            errors.append("brain-intake: %s" % e)
        for proj in self.cfg["projects"]:
            project = proj["project"] if isinstance(proj, dict) else proj
            try:
                kit = self.kit(project)
                mrs = self.gl.mrs_with_label(project, kit["handoff_label"])
            except Exception as e:  # noqa: BLE001 - logged by the caller; this project is looked at again next round
                errors.append("%s: %s" % (project, e))
                continue
            for mr in mrs:
                try:
                    self.process(project, kit, mr)
                except BrainError as e:  # one bad MR must not stop the others; it is retried next round
                    self.out("error on %s!%s: %s" % (project, mr.get("iid"), e))
        if errors:
            raise BrainError("poll: not done this round: " + "; ".join(errors))

    def process(self, project, kit, mr):
        """-> "accepted", "rejected", "seen" (this head and label event were checked before), "unlabelled" (the
        newest event on the hand-over label takes it off: nothing to do, e.g. `brain handoff` re-adding it right
        now) or "waiting" (this very hand-over is being signed elsewhere: looked at again next round)."""
        iid, sha = int(mr["iid"]), mr.get("sha", "")
        ev = self.last_label_event(project, iid, kit["handoff_label"])
        if ev is not None and ev.get("action") == "remove":
            return "unlabelled"
        adder, event = self._adder(ev)
        # One check per (head commit, label event): re-adding the label after fixing Jira re-checks the same commit.
        # A check left 'checking' by a bridge that stopped mid-way is taken over once it is stale.
        key = "%s#%s" % (sha, event)
        if not self.ledger.claim_handoff(project, iid, key, stale_after=self.reservation_ttl):
            for row in self.ledger.notes_due():   # checked before, but its comment may still be owed
                if row["project"] == project and int(row["iid"]) == iid:
                    try:
                        self._post_due(row)
                    except Exception as e:  # noqa: BLE001 - kept for the next round
                        self.out("%s!%s: comment not posted yet: %s" % (project, iid, one_line(e, 200)))
            return "seen"
        try:
            return self._process(project, kit, mr, iid, sha, key, adder, label_event=ev)
        except BaseException:
            # Leave nothing half-done: an MR still 'checking' is retried on the next poll (also on SIGTERM).
            self.ledger.release_handoff(project, iid, key)
            raise

    def _label_event_is_current(self, project, iid, kit, key) -> bool:
        """Is the label event this check was for (key = sha#event) still the newest one on the hand-over label? A
        newer one (the owner, or `brain handoff`, labelled again meanwhile) is checked next round: this check's
        verdict then goes to the ledger only, so its comment and label change do not undo the newer label."""
        try:
            ev = self.last_label_event(project, iid, kit["handoff_label"])
        except BrainError:
            return True
        return str((ev or {}).get("id", "")) == key.rsplit("#", 1)[-1]

    def _reject(self, project, kit, iid, sha, key, probs):
        self.ledger.set_handoff(project, iid, key, "checking", "rejected", reason="; ".join(probs)[:2000])
        self.out("rejected %s!%d @%s: %s" % (project, iid, sha[:12], "; ".join(probs)))
        if not self._label_event_is_current(project, iid, kit, key):
            return "rejected"
        self.tell_mr_due("rejected:%s!%d:%s" % (project, iid, key), project, iid,
                         "AI 接手被拒（%s）：\n" % sha[:12] + "\n".join("- " + x for x in probs) + "\n\n" +
                         self.retry_advice(kit), add=["rejected_label"], remove=["handoff_label"], check_key=key)
        return "rejected"

    # ---------- MR comments owed after a verdict ----------

    def tell_mr_due(self, key, project, iid, body, add=(), remove=(), run_id=None, check_key=None) -> bool:
        """Post `body` on the MR once for `key` and change its labels (kit label keys: rejected_label, ...), after
        the verdict was recorded. It is written down first (mr_notes_due): when GitLab fails (a 502, a timeout) it
        is posted on a later round (notes_due_round), not lost behind the recorded verdict. check_key: the sha#event
        of the hand-over check it is about. -> True when done now."""
        row = self.ledger.note_due(key, project, iid, body, {"add": list(add), "remove": list(remove)},
                                   run_id=run_id, check_key=check_key)
        try:
            self._post_due(row)
            return True
        except Exception as e:  # noqa: BLE001 - kept; posted next round
            self.out("%s!%s: comment not posted yet (next round): %s" % (project, iid, one_line(e, 200)))
            return False

    def notes_due_round(self):
        """Post what an earlier round could not (tell_mr_due). Errors stay for the next round."""
        errors = []
        for row in self.ledger.notes_due():
            try:
                self._post_due(row)
            except Exception as e:  # noqa: BLE001
                errors.append("%s!%s: %s" % (row["project"], row["iid"], one_line(e, 200)))
        return errors

    def _post_due(self, row):
        project, iid, key = row["project"], int(row["iid"]), row["key"]
        kit = self.kit(project)
        if row["check_key"] and not self._label_event_is_current(project, iid, kit, row["check_key"]):
            self.ledger.note_done(key)      # a newer label event is checked on its own: this verdict is moot
            return
        self.tell_mr_once(key, project, iid, row["body"], run_id=row["run_id"])   # once, also when labels failed
        labels = json.loads(row["labels"] or "{}")
        add, remove = [kit[k] for k in labels.get("add", [])], [kit[k] for k in labels.get("remove", [])]
        if add or remove:
            self.gl.edit_labels(project, iid, add=add, remove=remove)
        self.ledger.note_done(key)

    @staticmethod
    def retry_advice(kit) -> str:
        """What works after a rejection: only `brain handoff`'s own commit is handed over, so a fix pushed on top is
        handed over by running it again; a fix outside the branch (Jira, the claim, brain-authz) needs only the
        label on the same commit."""
        return ("改好后在分支上重新运行 `brain handoff`（它提交交接、推送并打标签）；只改了分支以外的东西（Jira、认领、"
                "brain-authz）的话，也可以直接再打一次标签 `%s`。" % kit["handoff_label"])

    def _process(self, project, kit, mr, iid, sha, key, adder, label_event=None):
        probs, f = self.verify(project, kit, mr, adder, label_event=label_event)
        if probs:
            return self._reject(project, kit, iid, sha, key, probs)
        overlap_note = ""
        for other, both in self.overlaps(project, f["key"], f["expected_paths"]):
            overlap_note += "\n\n注意：和 %s 预计改动重叠（%s），合并时留意顺序。" % (other, ", ".join(both[:5]))
        # The run id is taken before signing, as one step with the check that the requirement has no open run:
        # two hand-overs of one requirement (two MRs, two threads) never both get a contract.
        run_id = self.ledger.reserve_run(f["key"], project, iid, sha, stale_after=self.reservation_ttl)
        if run_id is None:
            other = self.ledger.open_run_for(f["key"], stale_after=self.reservation_ttl)
            if other is not None and self.ledger.is_reservation_for(other, project, iid, sha):
                # this very hand-over is being signed (another thread or process), or a bridge that stopped while
                # signing it left its reservation: not a reason to reject; looked at again next round (a stale one
                # is dropped then)
                self.ledger.release_handoff(project, iid, key)
                self.out("%s!%d @%s: its contract is being signed elsewhere (%s); looked at again next round"
                         % (project, iid, sha[:12], other["run_id"]))
                return "waiting"
            return self._reject(project, kit, iid, sha, key, ["%s already has an open run on the team runner"
                                                              % f["key"]])
        try:
            voucher = {
                "schema": 1, "kind": "handoff", "run_id": run_id, "project": project, "mr_iid": iid, "sha": sha,
                "branch": f["branch"], "owner": f["owner"], "key": f["key"], "intent": f["intent"],
                "brain_commit": f["brain_commit"], "materials": f["materials"], "route": "recheck",
                "issued_at": iso(utcnow()),
                "expires_at": later(self.cfg["contract_ttl_hours"]), "issuer": sign.PRINCIPAL,
            }
            voucher["voucher_id"] = "V-" + sign.digest(voucher)[:10]
            # SignerUnavailable: nothing signed, checked again next round (asking for this same voucher)
            ref = "handoff %s!%d %s" % (project, iid, key)
            voucher, sig = self.sign_voucher(ref, voucher)
        except sign.SignerRefused as e:
            self.ledger.release_run(run_id)
            why = "; ".join(e.reasons)
            self.ledger.set_handoff(project, iid, key, "checking", "rejected", reason=("signer: " + why)[:2000])
            self.out("signer refused %s!%d @%s: %s" % (project, iid, sha[:12], why))
            if self._label_event_is_current(project, iid, kit, key):
                self.tell_mr_due("rejected:%s!%d:%s" % (project, iid, key), project, iid,
                                 "AI 接手被拒（%s）：交接的检查都过了，但签名服务没有签这份合同：\n%s\n\n请负责人看一下签名服务的"
                                 "记录。%s" % (sha[:12], "\n".join("- " + x for x in e.reasons), self.retry_advice(kit)),
                                 add=["rejected_label"], remove=["handoff_label"], check_key=key)
            return "rejected"
        except BaseException:
            self.ledger.release_run(run_id)
            raise
        try:
            write_json(os.path.join(self.cfg["vouchers_dir"], run_id, voucher["voucher_id"] + ".json"),
                       {"voucher": voucher, "sig": sig})
            if self.cfg.get("publish_vouchers"):
                self.publish(run_id, voucher["voucher_id"])
            write_json(os.path.join(self.cfg["spool_dir"], "requests", run_id + ".json"),
                       {"voucher": voucher, "sig": sig}, mode=0o640)
            self.ledger.add_run(run_id, project, f["key"], iid, sha, voucher["voucher_id"],
                                paths=f["expected_paths"], owner=f["owner"], voucher=voucher)
        except BaseException:
            self.ledger.release_run(run_id)     # not recorded: the next round checks again, with this voucher
            raise
        self.ledger.drop_pending_voucher(ref)
        self.ledger.set_handoff(project, iid, key, "checking", "accepted", run_id=run_id,
                                voucher_id=voucher["voucher_id"])
        self.gl.edit_labels(project, iid, add=[kit["running_label"]], remove=[kit["handoff_label"],
                                                                                kit["rejected_label"]])
        note = "AI 已接手：%s，凭证 %s，从 %s 继续。" % (run_id, voucher["voucher_id"], sha[:12]) + overlap_note
        hits = self._risk_hits_quietly(project, kit, f["key"], f["expected_paths"])
        if hits:
            note += ("\n\n注意：预计改动碰到风险路径（%s），还没有桥签的设计审批（DESIGN）：run 改这些路径之前会先在这里请求设计"
                     "审批；本地 Lean 里的设计审批不算。" % "、".join(one_line(x, 100) for x in hits[:5]))
        self._learn_me(self.gl.comment(project, iid, note))
        if kit["tracker"] == "jira" and self.jira:
            self.jira.comment(f["key"], note + " MR !%d" % iid)
        self.out("accepted %s!%d @%s -> %s" % (project, iid, sha[:12], run_id))
        return "accepted"

    # ---------- risk paths at hand-over ----------

    def risk_paths_hit(self, project, kit, key, paths) -> list:
        """The risk-path patterns (kit risk_paths_file on the default branch) that the run's expected paths meet,
        when the bridge has signed no DESIGN approval for `key` yet: brain-run-check (ci.check_route) wants one for
        a change there, and a local Lean approval is not one. A hint for the hand-over; CI decides."""
        paths = [str(x).strip() for x in paths or [] if str(x).strip().rstrip("/")]     # conf.d/ is a folder
        if not paths:
            return []
        raw = self.gl.file_raw(project, kit.get("risk_paths_file") or "gates/risk-paths", kit["default_branch"])
        if not raw:
            return []
        pats = [x.split("#", 1)[0].strip() for x in raw.decode("utf-8", "replace").splitlines()]
        hits = [x for x in pats if x and plain(x) and len(x) <= 200 and any(_paths_meet(e, x) for e in paths)]
        return [] if not hits or self.design_approved(key) else hits

    def _risk_hits_quietly(self, project, kit, key, paths) -> list:
        try:
            return self.risk_paths_hit(project, kit, key, paths)
        except (BrainError, ValueError) as e:
            self.out("%s: risk paths not read: %s" % (key, e))
            return []

    def design_approved(self, key) -> bool:
        """Has the bridge signed an approving DESIGN answer for any run of `key` (its own vouchers)?"""
        for path in glob.glob(os.path.join(self.cfg["vouchers_dir"], "RUN-%s-*" % key, "*.json")):
            try:
                v = (load_json(path) or {}).get("voucher") or {}
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(v, dict) and v.get("kind") == "answer" and v.get("choice") == "approve" and \
                    str(v.get("lean_request") or "").startswith("DESIGN "):
                return True
        return False

    def publish(self, run_id, voucher_id):
        """Commit and push the voucher to the brain-intake repo (the bridge is its only writer). Safe to repeat: a
        round whose push failed left its commit; the next round asks for that same voucher (sign_voucher), finds
        nothing new to commit, and pushes what is there. Each push and pull ends after publish_timeout seconds."""
        vd = self.cfg["vouchers_dir"]
        self._undo_rebase(vd)
        git(vd, "add", "--", run_id)
        if git(vd, "diff", "--cached", "--quiet", check=False).returncode != 0:
            git(vd, "commit", "-q", "-m", "voucher %s %s" % (run_id, voucher_id))
        if self._git_remote(vd, "push", "-q").returncode != 0:
            p = self._pull(vd)
            if p.returncode != 0:
                raise NetworkError("command failed (%d): %s\n%s" % (p.returncode, " ".join(p.args),
                                                                     (p.stderr or p.stdout).strip()))
            self._git_remote(vd, "push", "-q", check=True)

    def _pull(self, d):
        """`git pull --rebase` in a brain-intake clone. A pull that publish_timeout stopped half way (a loaded
        machine), or one the bridge died in, leaves its rebase half done with HEAD detached, and every later pull then
        fails ("You are not currently on a branch"): such a rebase is undone before the pull and after one that did
        not finish, so the clone is back on its branch with its own commits. NetworkError when it timed out."""
        self._undo_rebase(d)
        try:
            p = git_net(d, "pull", "-q", "--rebase", timeout=self.git_timeout, check=False)
        except NetworkError:
            self._undo_rebase(d, killed=True)
            raise
        if p.returncode != 0:
            self._undo_rebase(d)
        return p

    @staticmethod
    def _undo_rebase(d, killed=False):
        gdir = git(d, "rev-parse", "--absolute-git-dir", check=False).stdout.strip()
        if not gdir:
            return
        if killed and os.path.exists(os.path.join(gdir, "index.lock")):
            os.remove(os.path.join(gdir, "index.lock"))      # git_net killed the pull with everything it started
        if any(os.path.isdir(os.path.join(gdir, x)) for x in ("rebase-merge", "rebase-apply")):
            git(d, "rebase", "--abort", check=False)

    def _git_remote(self, repo, *args, check=False):
        """A git command that talks to the vouchers repo's remote (common.git_net): never waits for a password
        prompt, and is stopped after publish_timeout seconds (a hung push must not hold the bridge's round)."""
        return git_net(repo, *args, timeout=self.git_timeout, check=check)

    # ---------- dispatch ----------

    def message(self, v, extra=()) -> str:
        """Numbers and hashes only: no requirement text, no approval lines, nothing that can break a line. `extra`:
        lines the bridge adds (the risk paths the run is about to meet), from main only."""
        for k in ("voucher_id", "run_id", "branch", "sha", "key", "intent"):
            if not plain(v.get(k)):
                raise BrainError("voucher field %s is not a plain one-line value" % k)
        return ("接手合同 %s（%s，摘要 sha256:%s）。工作区已检出 %s @ %s。\n"
                "按 brain-run 的接手步骤继续：先读 runs/%s/run.json 和 %s/%s/aidlc-state.md；"
                "本地记录里写的结果一律重新验证。"
                % (v["voucher_id"], v["run_id"], sign.digest(v)[:16], v["branch"], v["sha"][:12], v["key"],
                   lean.DOCS, v["intent"])) + "".join("\n" + x for x in extra if plain(x))

    def dispatch(self):
        """Send ready runs into KiroCrew (admission limits apply). First, what a stop, a crash or a KiroCrew error
        left half-sent is settled (recover_runs). A run runnerd could not prepare is asked for again or told on the
        MR (spool/failed). One run that fails does not hold up the others: the errors are raised at the end."""
        errors, sent = self.notes_due_round(), 0
        try:
            self.recover_runs()
        except Exception as e:  # noqa: BLE001
            errors.append("recover: %s" % e)
        for row in self.ledger.runs_in("spooled"):
            try:
                r = self._dispatch_one(row)
            except Exception as e:  # noqa: BLE001 - logged by the caller; looked at again next round
                errors.append("%s: %s" % (row["run_id"], e))
                continue
            if r == "full":
                break
            sent += 1 if r == "dispatched" else 0
        if errors:
            raise BrainError("dispatch: not done this round: " + "; ".join(errors))
        return sent

    def active_runs(self, project=None) -> int:
        """Runs whose session is, or may be, at work (in `project`): dispatched, or a hand-over that may have landed
        (send_unknown; ready with its message in the outbox: being sent, or cut off). One that waits for a person
        holds no session at work; one whose answer may have landed (delivery_unknown) does."""
        self._ddb()
        q, args = ("SELECT COUNT(*) FROM runs r WHERE (r.state IN ('dispatched', 'send_unknown') OR (r.state='ready' "
                   "AND EXISTS (SELECT 1 FROM outbox o WHERE o.send_id=r.voucher_id)))"), []
        if project is not None:
            q, args = q + " AND r.project=?", [project]
        q += (" AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.run_id=r.run_id AND d.state IN (%s))"
              % ",".join("?" * len(WAITING_DECISION_STATES)))
        return self.ledger.db.execute(q, tuple(args) + WAITING_DECISION_STATES).fetchone()[0]

    def _dispatch_one(self, row) -> str:
        """-> dispatched | unknown (the send's outcome is not known: the MR was told) | waiting | full."""
        rid = row["run_id"]
        rf = os.path.join(self.cfg["spool_dir"], "ready", rid + ".json")
        if not os.path.exists(rf):
            self._runnerd_failed(row)
            return "waiting"
        if self.active_runs() >= self.cfg["max_inflight"]:
            self.out("admission: %d runs in flight; %s waits" % (self.cfg["max_inflight"], rid))
            self._tell_queued(row, self.cfg["max_inflight"], None)
            return "full"
        if self.active_runs(row["project"]) >= self.cfg["max_per_repo"]:
            self._tell_queued(row, self.cfg["max_per_repo"], row["project"])
            return "waiting"
        ready = load_json(rf)
        v = json.loads(row["voucher"])  # the bridge's own copy, not the shared spool
        expect = os.path.realpath(os.path.join(self.cfg["runner_runs_root"], rid)) \
            if self.cfg.get("runner_runs_root") else None
        if expect and os.path.realpath(str(ready.get("path", ""))) != expect:
            self.out("%s: workspace path %r is not %s" % (rid, ready.get("path"), expect))
            self._fail_run(row, "运行机报的工作区不在运行机的 runs 目录里")
            return "waiting"
        if ready.get("head") != v["sha"]:
            self.out("%s: workspace head %s != contract sha" % (rid, ready.get("head")))
            self._fail_run(row, "运行机建的工作区不在合同的提交上")
            return "waiting"
        kit = self.kit(row["project"])
        if not self.ledger.set_run(rid, "spooled", "ready", path=ready["path"]):
            return "waiting"
        slot = rid.lower()
        try:
            existed = self._setup_slot(slot, kit, ready["path"])
        except Exception as e:  # noqa: BLE001 - nothing was sent: retried next round
            self.ledger.set_run(rid, "ready", "spooled")
            self.out("%s: session setup failed, will retry: %s" % (rid, e))
            if self.ledger.bump("setup:" + rid, rid) >= SETUP_TELL_AFTER:
                self.tell_mr_once("setup:" + rid, row["project"], row["iid"], "%s 还没开始：KiroCrew 开会话失败（%s）。桥每轮"
                                  "重试；一直不行的话请负责人看一下 KiroCrew 和 agent %s。"
                                  % (rid, one_line(e, 200), self.cfg["kirocrew"].get("agent")), run_id=rid)
            return "waiting"
        except BaseException:
            self.ledger.set_run(rid, "ready", "spooled")    # a stop while KiroCrew set the slot up: nothing sent
            raise
        vid = v["voucher_id"]
        msg = self.message(v, extra=self._risk_lines(row, kit))
        ob, new = self.ledger.outbox_put(vid, rid, slot, "handover:" + rid, msg, sha=v["sha"])
        if new and existed and self._landed(slot, vid):
            self.ledger.outbox_set(vid, "sending", "sent")  # an older bridge sent it before it stopped
            r = "sent"
        else:
            r = self._push(vid, slot, msg) if new else self._settle(ob, give_up_after=self.send_give_up_s)
        return self._handover_sent(row, slot, r)

    def _setup_slot(self, slot, kit, path) -> bool:
        """Open the run's KiroCrew slot and set its project. -> whether the slot was there already (a setup a stop
        cut off: KiroCrew, or the brain app, refuses to open it twice)."""
        kc, existed = self.cfg["kirocrew"], False
        try:
            self.kc.create_slot(slot, kc["agent"], kit["model"], kc.get("memory_mode", "incognito"))
        except BrainError:
            try:
                existed = isinstance(self.kc.slot(slot), dict)
            except Exception:  # noqa: BLE001
                existed = False
            if not existed:
                raise
            self.out("slot %s was opened before (a setup that was cut off); going on" % slot)
        self.kc.set_project(slot, path)
        return existed

    def _risk_lines(self, row, kit) -> list:
        try:
            paths = json.loads(row["paths"] or "[]")
        except ValueError:
            paths = []
        hits = self._risk_hits_quietly(row["project"], kit, row["req"], paths)
        if not hits:
            return []
        return ["风险路径：预计改动（run.json 的 expected_paths）碰到 %s 里的 %s，桥还没有签过 %s 的设计审批（DESIGN）。本地 Lean "
                "里的设计审批不算：改这些路径之前，先 `lean request DESIGN`，在 park.json 写 \"lean_request\": \"DESIGN "
                "<hash>\" 停下来，由桥在 MR 上问；不然 brain-run-check 不会过。"
                % (kit.get("risk_paths_file") or "gates/risk-paths", "、".join(one_line(x, 100) for x in hits[:5]),
                   row["req"])]

    def _handover_sent(self, row, slot, r, state="ready") -> str:
        """After the hand-over's send (the run is in `state`): dispatched, its outcome unknown (the MR hears once),
        dropped (KiroCrew did not take it for send_give_up_minutes: the run is failed, the MR says how to hand over
        again) or being sent by another bridge process."""
        rid = row["run_id"]
        if r == "dropped":
            ob = self.ledger.outbox(row["voucher_id"])
            self._fail_run(row, "桥把接手消息送了 %d 次，KiroCrew 会话 %s 一直没有收下（%d 分钟）" % (
                int(ob["tries"] or 0) if ob is not None else 0, slot, int(self.send_give_up_s // 60)), frm=state)
            return "failed"
        if r == "sent":
            if self.ledger.set_run(rid, "ready", "dispatched", slot=slot) or \
                    self.ledger.set_run(rid, "send_unknown", "dispatched", slot=slot):
                self.out("dispatched %s to KiroCrew slot %s" % (rid, slot))
                return "dispatched"
            return "waiting"
        if r == "unknown":
            self.ledger.set_run(rid, "ready", "send_unknown", slot=slot)
            self.out("%s: send outcome unknown (slot %s)" % (rid, slot))
            self.tell_mr_once("send_unknown:" + rid, row["project"], row["iid"], (
                "%s 的接手消息不知道送到没有：KiroCrew 出错%s。桥每轮看一下 KiroCrew 会话 %s：确认没送到就用同一个 sendId 再送一次，"
                "送到了就照常继续；看不了会话的话，请负责人在 KiroCrew 里看一下它。")
                % (rid, "（%s）" % one_line(self.send_error, 200) if self.send_error else "", slot), run_id=rid)
            return "unknown"
        return "waiting"

    def recover_runs(self):
        """Runs a stop, a crash or a KiroCrew error left half-dispatched:
          ready         slot setup was cut off (no outbox row: back to spooled once the row is stale; a stop gives it
                        back on its way out), or the hand-over's send (settled from the outbox);
          send_unknown  settled from the outbox each round (an older bridge's row gets its message rebuilt from the
                        voucher, and the MR is told once).
        (Notices whose send was cut off are settled in collect: recover_answers.) One run that fails does not hold
        up the others."""
        errors = []
        for row in self.ledger.runs_in("ready", "send_unknown"):
            try:
                if row["state"] == "ready":
                    self._recover_ready(row)
                else:
                    self._recover_send_unknown(row)
            except Exception as e:  # noqa: BLE001
                errors.append("%s: %s" % (row["run_id"], e))
        if errors:
            raise BrainError("; ".join(errors))

    def _recover_ready(self, row):
        ob = self.ledger.outbox(row["voucher_id"])
        if ob is None:
            if self._stale(row["updated"]):
                self.ledger.set_run(row["run_id"], "ready", "spooled")
                self.out("%s: its slot setup was cut off; dispatched again" % row["run_id"])
            return
        self._handover_sent(row, ob["slot"], self._settle(ob, give_up_after=self.send_give_up_s))

    def _recover_send_unknown(self, row):
        rid, slot = row["run_id"], row["slot"] or row["run_id"].lower()
        ob = self.ledger.outbox(row["voucher_id"])
        if ob is None:                  # an older bridge's: its message rebuilt from the voucher
            v = json.loads(row["voucher"] or "{}")
            self.ledger.outbox_put(row["voucher_id"], rid, slot, "handover:" + rid, self.message(v), sha=v.get("sha"))
            self.ledger.outbox_set(row["voucher_id"], "sending", "unknown")
            self._handover_sent(dict(row, state="ready"), slot, "unknown")           # the MR hears once
            ob = self.ledger.outbox(row["voucher_id"])
        r = self._settle(ob, give_up_after=self.send_give_up_s)
        if r == "dropped":
            self._handover_sent(row, slot, r, state="send_unknown")
        elif r == "sent" and self.ledger.set_run(rid, "send_unknown", "dispatched", slot=slot):
            self.out("%s: its hand-over had landed (or was sent again); dispatched" % rid)

    def _tell_queued(self, row, limit, project):
        n = self.active_runs(project)
        self.tell_mr_once("queued:" + row["run_id"], row["project"], row["iid"], "%s 在排队：%s已经有 %d 个 run 在跑（上限 "
                          "%d），有空位后桥自动开始，不用做什么。" % (row["run_id"], "这个项目" if project else "运行机上", n,
                                                                   limit), run_id=row["run_id"])

    def _read_failed(self, run_id):
        p = os.path.join(self.cfg["spool_dir"], "failed", run_id + ".json")
        if not os.path.exists(p):
            return None
        try:
            f = load_json(p)
        except (OSError, ValueError):
            return {"error": "spool/failed/%s.json cannot be read" % run_id}
        return f if isinstance(f, dict) else {"error": "spool/failed/%s.json is not an object" % run_id}

    def _runnerd_failed(self, row):
        """runnerd wrote spool/failed/<RUN>.json. While runnerd still tries (a transient failure, not final):
        nothing. A transient failure it gave up on: asked again (the request's "attempt" goes up; at most REQUEUES
        times, REQUEUE_AFTER_S after it gave up), and the MR hears once. Anything else: the run is failed, and the
        MR is told why and what to do."""
        rid = row["run_id"]
        f = self._read_failed(rid)
        if f is None:
            self._runnerd_silent(row, row["updated"])       # nothing from runnerd since the run was spooled
            return
        asked = self.ledger.count_of("requeue:" + rid)
        try:
            attempt = int(f.get("attempt") or 0)
        except (TypeError, ValueError):
            attempt = 0
        if attempt < asked:                         # runnerd has not looked at the newer request yet
            n = self.ledger.notice("requeue:" + rid)
            self._runnerd_silent(row, n["updated"] if n is not None else row["updated"])
            return
        if not f.get("final", True):                # runnerd still tries (it said so then)
            self._runnerd_silent(row, str(f.get("at") or row["updated"]))
            return
        err = one_line(f.get("error") or "unknown error", 300)
        if f.get("transient") and asked < REQUEUES:
            try:
                at = parse_iso(str(f.get("at")))
            except ValueError:
                at = None
            if at is not None and utcnow() - at < timedelta(seconds=REQUEUE_AFTER_S):
                return
            rp = os.path.join(self.cfg["spool_dir"], "requests", rid + ".json")
            req = load_json(rp)
            req["attempt"] = asked + 1
            write_json(rp, req, mode=0o640)
            self.ledger.bump("requeue:" + rid, rid)
            self.out("%s: runnerd failed (%s); asked again (%d/%d)" % (rid, err, asked + 1, REQUEUES))
            self.tell_mr_once("runnerd-retry:" + rid, row["project"], row["iid"], "%s 的工作区还没建成：运行机这几次都没做成"
                              "（%s）。桥已经请运行机过一会儿再试，不用做什么；还不行的话这里会再说。" % (rid, err), run_id=rid)
            return
        self._fail_run(row, "运行机建工作区失败（%s）" % err)

    def _runnerd_silent(self, row, since):
        """runnerd has said nothing about the run since `since` (no workspace, no failure, no new try): after
        runnerd_wait_minutes the MR is told once (runnerd is down, say). The run stays queued: it starts as soon as
        runnerd prepares it."""
        wait = sign.seconds(self.cfg.get("runnerd_wait_minutes"), 90.0)
        if str(since or "") > iso(utcnow() - timedelta(minutes=wait)):
            return
        rid = row["run_id"]
        self.tell_mr_once("runnerd-silent:" + rid, row["project"], row["iid"], (
            "%s 还没开始：运行机（brain-runnerd）%d 分钟里没有准备好工作区，也没有报错，可能没在运行。桥接着等，运行机准备好"
            "以后自动开始，不用重新交接；请负责人看一下 brain-runnerd（比如 `systemctl status brain-runnerd` 和它的 "
            "runnerd.json）。") % (rid, wait), run_id=rid)

    def _fail_run(self, row, why, frm="spooled"):
        """The run cannot start: failed (from state `frm`), and the MR is told once (labels: rejected instead of
        running)."""
        rid = row["run_id"]
        if not self.ledger.set_run(rid, frm, "failed"):
            return
        self.tell_mr_due("failed:" + rid, row["project"], row["iid"], "%s 没能开始：%s。处理好以后在分支上重新运行 "
                         "`brain handoff`（它提交交接、推送并打标签），桥会重新接手。" % (rid, why),
                         add=["rejected_label"], remove=["running_label"], run_id=rid)

    # ---------- reconcile ----------

    def reconcile(self):
        # what a bridge that stopped mid-way left (a reservation, a hand-over still 'checking') is cleared once stale
        n = self.ledger.drop_stale(self.reservation_ttl)
        if n:
            self.out("dropped %d stale reservation(s) / hand-over check(s) left by a stopped bridge" % n)
        errors = []
        for row in self.ledger.runs_in("dispatched", "spooled", "ready", "send_unknown"):
            try:
                self._reconcile_one(row)
            except Exception as e:  # noqa: BLE001 - one MR GitLab will not show must not hold up the others
                errors.append("%s: %s" % (row["run_id"], e))
        if errors:
            raise BrainError("reconcile: not done this round: " + "; ".join(errors))

    def _reconcile_one(self, row):
        mr = self.gl.mr(row["project"], row["iid"])
        if mr.get("state") in ("merged", "closed"):
            # a session is, or may be, at work: stopped (a hand-over whose send is unknown may have landed)
            if row["state"] in ("dispatched", "send_unknown") and row["slot"] and self.kc:
                try:
                    self.kc.stop(row["slot"])
                except BrainError as e:
                    self.out("%s: stop failed: %s" % (row["run_id"], e))
            # "merged" and "closed" stay apart: Eval counts deliveries, not abandoned runs
            self.ledger.set_run(row["run_id"], row["state"], mr.get("state"))
            self.out("%s %s" % (mr.get("state"), row["run_id"]))


def build(cfg_path, transport=None):
    cfg = load_bridge_config(cfg_path)
    gl = GitLab(cfg["gitlab"]["base"], read_token(cfg["gitlab"].get("token_env", "BRIDGE_GITLAB_TOKEN"),
                                                  default_file=cfg["gitlab"].get("token_file", DEFAULT_TOKEN_FILE)),
                transport)
    jc = cfg.get("jira") or {}
    jira = None
    if jc.get("base"):
        jira = Jira(jc["base"], os.environ.get(jc.get("token_env", "BRIDGE_JIRA_TOKEN"), ""), jc.get("api", "2"),
                    transport, basic_user=os.environ.get(jc.get("user_env", ""), "") or None)
    kc = None
    if cfg["kirocrew"].get("secret_file"):
        with open(cfg["kirocrew"]["secret_file"], encoding="utf-8") as fh:
            secret = fh.read().strip()
        kc = KiroCrew(cfg["kirocrew"]["base"], cfg["kirocrew"]["app"], secret, transport)
    return Bridge(cfg, gl, jira, kc)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="brain-bridge")
    ap.add_argument("--config", required=True)
    ap.add_argument("cmd", choices=("poll", "dispatch", "watch", "collect", "reconcile", "report", "loop"))
    a = ap.parse_args(argv)
    # SIGTERM (systemctl stop/restart, a deploy) ends the bridge like Ctrl-C: what it was doing is given back
    # (a run reservation, a hand-over check) instead of being left until it is stale
    old = None
    if threading.current_thread() is threading.main_thread():
        old = signal.signal(signal.SIGTERM, _terminate)
    try:
        return _main(a)
    finally:
        if old is not None:
            signal.signal(signal.SIGTERM, old)


def _terminate(signum, frame):
    raise SystemExit(128 + signum)


def _main(a):
    try:
        br = build(a.config)
        if a.cmd in ("dispatch", "collect", "loop") and not br.kc:
            raise BrainError("kirocrew.secret_file is not set")
        if a.cmd == "report":
            print(json.dumps(br.report(), ensure_ascii=False, indent=2))
            return 0
        while True:
            for step in ("poll", "dispatch", "watch", "collect", "reconcile"):
                if a.cmd not in (step, "loop"):
                    continue
                try:
                    getattr(br, step)()
                except Exception as e:  # noqa: BLE001
                    if a.cmd != "loop":
                        raise
                    # one bad round (network, GitLab, SSO, one run's odd data) must not stop the bridge: log, go
                    # on, retry next round. A stop (SIGTERM, Ctrl-C) is not an Exception and ends the loop.
                    print("brain-bridge: %s failed this round: %s%s" % (
                        step, "" if isinstance(e, BrainError) else type(e).__name__ + ": ", e), file=sys.stderr)
            if a.cmd != "loop":
                return 0
            time.sleep(sign.seconds(br.cfg.get("interval"), 60.0))
    except BrainError as e:
        print("brain-bridge: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
