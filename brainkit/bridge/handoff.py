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
  5. run.json at the MR head is valid for this branch and was handed off by the owner;
  6. the Lean hand-over note (aidlc-state.md) at the MR head is filled in;
  7. the Brain commit exists; material packages are in brain-intake with the recorded hash;
     no other run is open for this requirement.
The kit config is read from the default branch, never from the MR branch.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .. import claims, config as kitconfig, lean, runrec, sign
from ..intake import package as intake
from ..common import BrainError, git, iso, later, load_json, parse_branch, plain, utcnow, write_json
from ..rest import DEFAULT_TOKEN_FILE, GitLab, HttpError, Jira, gl_time, head_pushed_at, jira_user_id, read_token
from .decisions import DecisionLoop
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


class Bridge(DecisionLoop):
    def __init__(self, cfg, gitlab, jira, kirocrew=None, out=print, signer=None):
        """`signer`: any object with sign(voucher) -> signature; default the one bridge.json names (sign.py)."""
        self.cfg = cfg
        self.gl = gitlab
        self.jira = jira
        self.kc = kirocrew
        self.out = out
        # a subclass may also bring its signer as a class attribute
        self.signer = signer or getattr(self, "signer", None) or sign.signer_from_config(cfg)
        self.ledger = Ledger(cfg["ledger"])
        authz = load_json(cfg["identities"])
        self.ids = authz.get("users", {})
        self.roles = authz.get("roles", {})
        self._kit = {}

    # ---------- config from the default branch ----------

    def kit(self, project) -> dict:
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
        return self._kit[project]

    # ---------- checks ----------

    def last_label_event(self, project, iid, label):
        """The latest add/remove event on `label` (all pages), or None."""
        last = None
        for ev in self.gl.label_events(project, iid):
            if (ev.get("label") or {}).get("name") == label and ev.get("action") in ("add", "remove"):
                last = ev
        return last

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
            return ["%s was pushed (%s) after the label '%s' was added (%s): that commit was not handed over; add the "
                    "label again to hand it over" % (sha[:12], iso(pushed), label, iso(added))]
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
        f.update(intent=rec["intent"], brain_commit=rec["brain_commit"], materials=rec.get("materials", []),
                 expected_paths=rec.get("expected_paths", []))
        p += self.check_materials(f["materials"])
        state = self.gl.file_raw(project, "%s/%s/aidlc-state.md" % (lean.DOCS, rec["intent"]), sha)
        if state is None:
            p.append("Lean hand-over note %s/%s/aidlc-state.md missing" % (lean.DOCS, rec["intent"]))
        else:
            p += ["hand-over note: " + x for x in lean.handover_text_problems(state.decode("utf-8", "replace"))]
        if not self.gl.commit_exists(project, rec["brain_commit"]):
            p.append("brain_commit %s does not exist" % rec["brain_commit"][:12])
        if self.ledger.open_run_for(b["key"]):
            p.append("%s already has an open run on the team runner" % b["key"])
        return p, f

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
        if os.path.isdir(os.path.join(d, ".git")):
            git(d, "pull", "-q", "--ff-only", check=False)
        probs = []
        for m in materials:
            pdir = os.path.join(d, "packages", m["package"])
            if not os.path.isdir(pdir):
                probs.append("material %s is not published to brain-intake" % m["package"])
                continue
            if intake.manifest_digest(pdir) != m["manifest_sha256"]:
                probs.append("material %s in brain-intake differs from run.json" % m["package"])
            probs += ["material %s: %s" % (m["package"], x) for x in intake.verify_package(pdir)]
        return probs

    def overlaps(self, project, key, paths) -> list:
        """Open runs in the same project whose expected paths overlap these."""
        out = []
        mine = [x.rstrip("/") for x in paths if x]
        for row in self.ledger.runs_in(*OPEN_RUN_STATES):
            if row["project"] != project or row["req"] == key:
                continue
            theirs = [x.rstrip("/") for x in json.loads(row["paths"] or "[]") if x]
            both = sorted({a for a in mine for t in theirs if a == t or a.startswith(t + "/") or t.startswith(a + "/")})
            if both:
                out.append((row["run_id"], both))
        return out

    # ---------- poll ----------

    def poll(self):
        for proj in self.cfg["projects"]:
            project = proj["project"] if isinstance(proj, dict) else proj
            kit = self.kit(project)
            for mr in self.gl.mrs_with_label(project, kit["handoff_label"]):
                try:
                    self.process(project, kit, mr)
                except BrainError as e:  # one bad MR must not stop the others; it is retried next round
                    self.out("error on %s!%s: %s" % (project, mr.get("iid"), e))

    def process(self, project, kit, mr):
        iid, sha = int(mr["iid"]), mr.get("sha", "")
        ev = self.last_label_event(project, iid, kit["handoff_label"])
        adder, event = self._adder(ev)
        # One check per (head commit, label event): re-adding the label after fixing Jira re-checks the same commit.
        key = "%s#%s" % (sha, event)
        if not self.ledger.claim_handoff(project, iid, key):
            return "seen"
        try:
            return self._process(project, kit, mr, iid, sha, key, adder, label_event=ev)
        except Exception:
            # Leave nothing half-done: an MR still 'checking' is retried on the next poll.
            self.ledger.release_handoff(project, iid, key)
            raise

    def _process(self, project, kit, mr, iid, sha, key, adder, label_event=None):
        probs, f = self.verify(project, kit, mr, adder, label_event=label_event)
        if probs:
            self.ledger.set_handoff(project, iid, key, "checking", "rejected", reason="; ".join(probs)[:2000])
            self.gl.comment(project, iid, "AI 接手被拒（%s）：\n" % sha[:12] + "\n".join("- " + x for x in probs) +
                            "\n\n改好后重新推送，再打一次标签 `%s`。" % kit["handoff_label"])
            self.gl.edit_labels(project, iid, add=[kit["rejected_label"]], remove=[kit["handoff_label"]])
            self.out("rejected %s!%d @%s: %s" % (project, iid, sha[:12], "; ".join(probs)))
            return "rejected"
        overlap_note = ""
        for other, both in self.overlaps(project, f["key"], f["expected_paths"]):
            overlap_note += "\n\n注意：和 %s 预计改动重叠（%s），合并时留意顺序。" % (other, ", ".join(both[:5]))
        run_id = self.ledger.next_run_id(f["key"])
        voucher = {
            "schema": 1, "kind": "handoff", "run_id": run_id, "project": project, "mr_iid": iid, "sha": sha,
            "branch": f["branch"], "owner": f["owner"], "key": f["key"], "intent": f["intent"],
            "brain_commit": f["brain_commit"], "materials": f["materials"], "route": "recheck",
            "issued_at": iso(utcnow()),
            "expires_at": later(self.cfg["contract_ttl_hours"]), "issuer": sign.PRINCIPAL,
        }
        voucher["voucher_id"] = "V-" + sign.digest(voucher)[:10]
        try:
            sig = self.signer.sign(voucher)     # SignerUnavailable: nothing signed, checked again next round
        except sign.SignerRefused as e:
            why = "; ".join(e.reasons)
            self.ledger.set_handoff(project, iid, key, "checking", "rejected", reason=("signer: " + why)[:2000])
            self.gl.comment(project, iid, "AI 接手被拒（%s）：交接的检查都过了，但签名服务没有签这份合同：\n%s\n\n"
                            "请负责人看一下签名服务的记录；改好后重新推送，再打一次标签 `%s`。"
                            % (sha[:12], "\n".join("- " + x for x in e.reasons), kit["handoff_label"]))
            self.gl.edit_labels(project, iid, add=[kit["rejected_label"]], remove=[kit["handoff_label"]])
            self.out("signer refused %s!%d @%s: %s" % (project, iid, sha[:12], why))
            return "rejected"
        write_json(os.path.join(self.cfg["vouchers_dir"], run_id, voucher["voucher_id"] + ".json"),
                   {"voucher": voucher, "sig": sig})
        if self.cfg.get("publish_vouchers"):
            self.publish(run_id, voucher["voucher_id"])
        write_json(os.path.join(self.cfg["spool_dir"], "requests", run_id + ".json"),
                   {"voucher": voucher, "sig": sig}, mode=0o640)
        self.ledger.add_run(run_id, project, f["key"], iid, sha, voucher["voucher_id"], paths=f["expected_paths"],
                            owner=f["owner"], voucher=voucher)
        self.ledger.set_handoff(project, iid, key, "checking", "accepted", run_id=run_id,
                                voucher_id=voucher["voucher_id"])
        self.gl.edit_labels(project, iid, add=[kit["running_label"]], remove=[kit["handoff_label"],
                                                                                kit["rejected_label"]])
        note = "AI 已接手：%s，凭证 %s，从 %s 继续。" % (run_id, voucher["voucher_id"], sha[:12]) + overlap_note
        self.gl.comment(project, iid, note)
        if kit["tracker"] == "jira" and self.jira:
            self.jira.comment(f["key"], note + " MR !%d" % iid)
        self.out("accepted %s!%d @%s -> %s" % (project, iid, sha[:12], run_id))
        return "accepted"

    def publish(self, run_id, voucher_id):
        """Commit and push the voucher to the brain-intake repo (the bridge is its only writer)."""
        vd = self.cfg["vouchers_dir"]
        git(vd, "add", "--", run_id)
        git(vd, "commit", "-q", "-m", "voucher %s %s" % (run_id, voucher_id))
        if git(vd, "push", "-q", check=False).returncode != 0:
            git(vd, "pull", "-q", "--rebase")
            git(vd, "push", "-q")

    # ---------- dispatch ----------

    def message(self, v) -> str:
        """Numbers and hashes only: no requirement text, no approval lines, nothing that can break a line."""
        for k in ("voucher_id", "run_id", "branch", "sha", "key", "intent"):
            if not plain(v.get(k)):
                raise BrainError("voucher field %s is not a plain one-line value" % k)
        return ("接手合同 %s（%s，摘要 sha256:%s）。工作区已检出 %s @ %s。\n"
                "按 brain-run 的接手步骤继续：先读 runs/%s/run.json 和 %s/%s/aidlc-state.md；"
                "本地记录里写的结果一律重新验证。"
                % (v["voucher_id"], v["run_id"], sign.digest(v)[:16], v["branch"], v["sha"][:12], v["key"],
                   lean.DOCS, v["intent"]))

    def dispatch(self):
        ready_dir = os.path.join(self.cfg["spool_dir"], "ready")
        sent = 0
        for row in self.ledger.runs_in("spooled"):
            rf = os.path.join(ready_dir, row["run_id"] + ".json")
            if not os.path.exists(rf):
                continue
            if self.ledger.count("dispatched") >= self.cfg["max_inflight"]:
                self.out("admission: %d runs in flight; %s waits" % (self.cfg["max_inflight"], row["run_id"]))
                break
            if self.ledger.count("dispatched", row["project"]) >= self.cfg["max_per_repo"]:
                continue
            ready = load_json(rf)
            v = json.loads(row["voucher"])  # the bridge's own copy, not the shared spool
            expect = os.path.realpath(os.path.join(self.cfg["runner_runs_root"], row["run_id"])) \
                if self.cfg.get("runner_runs_root") else None
            if expect and os.path.realpath(str(ready.get("path", ""))) != expect:
                self.ledger.set_run(row["run_id"], "spooled", "failed")
                self.out("%s: workspace path %r is not %s" % (row["run_id"], ready.get("path"), expect))
                continue
            if ready.get("head") != v["sha"]:
                self.ledger.set_run(row["run_id"], "spooled", "failed")
                self.out("%s: workspace head %s != contract sha" % (row["run_id"], ready.get("head")))
                continue
            if not self.ledger.set_run(row["run_id"], "spooled", "ready", path=ready["path"]):
                continue
            slot = row["run_id"].lower()
            kit = self.kit(row["project"])
            kc = self.cfg["kirocrew"]
            try:
                self.kc.create_slot(slot, kc["agent"], kit["model"], kc.get("memory_mode", "incognito"))
                self.kc.set_project(slot, ready["path"])
            except BrainError as e:
                self.ledger.set_run(row["run_id"], "ready", "spooled")  # nothing was sent: retry next round
                self.out("%s: session setup failed, will retry: %s" % (row["run_id"], e))
                continue
            try:
                self.kc.send(slot, self.message(v), v["voucher_id"])
            except BrainError as e:
                # The send may or may not have landed: never resend automatically (deliver once).
                self.ledger.set_run(row["run_id"], "ready", "send_unknown", slot=slot)
                self.out("%s: send outcome unknown, check slot %s by hand: %s" % (row["run_id"], slot, e))
                continue
            self.ledger.set_run(row["run_id"], "ready", "dispatched", slot=slot)
            self.out("dispatched %s to KiroCrew slot %s" % (row["run_id"], slot))
            sent += 1
        return sent

    # ---------- reconcile ----------

    def reconcile(self):
        for row in self.ledger.runs_in("dispatched", "spooled", "ready", "send_unknown"):
            mr = self.gl.mr(row["project"], row["iid"])
            if mr.get("state") in ("merged", "closed"):
                if row["state"] == "dispatched" and row["slot"] and self.kc:
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
                except BrainError as e:
                    if a.cmd != "loop":
                        raise
                    # one bad round (network, GitLab, Midway) must not stop the bridge: log, go on, retry next round
                    print("brain-bridge: %s failed this round: %s" % (step, e), file=sys.stderr)
            if a.cmd != "loop":
                return 0
            time.sleep(br.cfg["interval"])
    except BrainError as e:
        print("brain-bridge: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
