"""`brain`: the local half. Start a run on your own machine, hand it to the team runner.

  brain install <repo> [--lean PATH] [--team NAME] [--prefix OMS]   copy the kit (and AI-DLC Lean) into a project
  brain doctor                                    check this machine and repo before work
  brain claim [--key KEY] --title T               claim a requirement key (tracker "none": next free <prefix>-<n>)
  brain intake PATH... --key KEY --classification C --consent [--business keep|mask] [--dry-run]
                                                  check and mask requirement documents into a material package
  brain start <slug> [--key KEY] [--title T] [--doc PATH ...] [--package PKG] [--paths a,b]
                                                  claim (or check the claim), branch, Lean intent, run.json
  brain handoff [--no-push] [--no-label]          publish materials, commit the hand-over (with runs/<KEY>/MR.md),
                                                  set the MR description, label the MR
                                                  (exit 1: pushed but NOT labelled, nothing handed over)
  brain crew-agent [--force | --reset]            install the KiroCrew version of brain-dev (~/.kiro/agents/brain-dev.json)
                                                  KiroCrew does not read .kiro/hooks/: Lean's hooks ride on this spec;
                                                  --force adds what is missing (Lean's hooks, absent fields) and keeps
                                                  your model, tools, resources and prompt; --reset takes brain-kit's
  brain status
  brain lint [--base REF] [--static]              Brain rules; against the merge-base with main by default
  brain entry add|replace|withdraw|same|adds-to|repeat|purge|show ...
                                                  the only way to edit Brain entries and archives (deterministic)
  brain reflect [--key KEY] [--ref REF | --from FILE] [--apply [--no-commit] [--push]]
                                                  a run's brain-proposal blocks -> a Brain MR (brain/<KEY>-reflect-<n>)
  brain usage [--ref REF] [--json] [--min-runs N] which entries runs cite, useful rate, repeat rate
  brain intent add|list|show|close ...            requirements from alerts, tech debt, schedules, Eval gaps (commands/)
  brain reflect --draft KEY                       instructions for a fresh session to draft the proposals
  brain trace KEY / brain eval / brain golden ... run trace, Eval v1, golden tasks and ablation (commands/)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import time

from . import __version__, brainlint, claims, config, entries, lean, mrdesc, reflect, runrec, usage
from .common import (BrainError, JIRA_RE, SLUG_RE, USER_RE, branch_name, git, git_net, git_out, iso, load_json,
                     parse_branch, repo_root, run, utcnow, write_json)
from .intake import package as intake
from .rest import GitLab, HttpError, Jira, jira_user_id, read_token

KIT_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kit")
TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
CREW_AGENT_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crew", "brain-dev.json")
CREW_HOOK = ".kiro/brain/lean-hook.py"
# The adapter version the team runner needs: 2 lets the run write its own runs/<KEY>/park.json in every Lean approval
# state (kit/.kiro/brain/lean-hook.py, ADAPTER_VERSION).
CREW_HOOK_VERSION = 2
VENDORED_LEAN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor", "aidlc-lean")


def crew_hook_version(text) -> int:
    """ADAPTER_VERSION of a hook adapter's source (0: none, an adapter older than the park.json allowance)."""
    m = re.search(r"^ADAPTER_VERSION = ([0-9]+)\b", text or "", re.M)
    return int(m.group(1)) if m else 0


def crew_hook_problem(text) -> str:
    """Why this adapter source is too old for the team runner, or ''."""
    have = crew_hook_version(text)
    if have >= CREW_HOOK_VERSION:
        return ""
    return ("%s is an older adapter (version %d, needs %d): the team runner could not write runs/<KEY>/park.json, its "
            "only way to ask a person, before the Lean approvals; run `brain install` and commit it on main through an "
            "MR" % (CREW_HOOK, have, CREW_HOOK_VERSION))


def crew_agent_path():
    return os.path.join(os.path.expanduser("~"), ".kiro", "agents", "brain-dev.json")
MATERIALS_DIR = ".brain-materials"
GITIGNORE_LINES = (MATERIALS_DIR + "/",)


# ---------- helpers ----------

def _has_origin(repo) -> bool:
    return git(repo, "remote", "get-url", "origin", check=False).returncode == 0


def _dirty(repo, ignore=()) -> list:
    rows = git_out(repo, "status", "--porcelain", "--untracked-files=no").splitlines()
    return [r for r in rows if r[3:].strip() not in ignore]


def _gitlab(cfg):
    tok = read_token()
    if not cfg["gitlab"]["base"] or not tok:
        return None
    return GitLab(cfg["gitlab"]["base"], tok)


def _me(repo, cfg) -> str:
    gl = _gitlab(cfg)
    if gl:
        return str(gl.current_user()["username"]).lower()
    who = os.environ.get("BRAIN_USER") or git(repo, "config", "brain.user", check=False).stdout.strip()
    if not who and not cfg["gitlab"]["base"]:
        # No GitLab here: the name only names this machine's claims and branches (ai/<name>/...); no server checks
        # it and no bridge compares it, so the login name will do. With GitLab it must be the GitLab username.
        who = _login_name()
        if not who:
            raise BrainError("who are you? your login name is not usable as a name here: "
                             "`git config brain.user <name>` (lowercase letters, digits, . _ -)")
    if not who:
        raise BrainError("who are you? this repo uses GitLab (%s): set GITLAB_TOKEN (preferred) or "
                         "`git config brain.user <your GitLab username>`; it must be your GitLab username: GitLab "
                         "checks who pushes ai/<name>/... and the bridge checks who handed over" % cfg["gitlab"]["base"])
    if not USER_RE.match(who.lower()):
        raise BrainError("brain.user %r is not a GitLab username" % who)
    return who.lower()


def _login_name() -> str:
    """This machine's login name, lowercased, when it is a usable name; '' otherwise."""
    import getpass
    try:
        name = getpass.getuser().strip().lower()
    except Exception:  # noqa: BLE001 - no login name (no USER, no passwd entry): ask for one
        return ""
    return name if USER_RE.match(name) else ""


def _store(repo, override=None) -> str:
    """Where material packages live on this machine (a clone of brain-intake when shared)."""
    s = override or os.environ.get("BRAIN_INTAKE_DIR") or git(repo, "config", "brain.intake", check=False).stdout.strip()
    s = os.path.abspath(os.path.expanduser(s or "~/.brain/intake"))
    os.makedirs(os.path.join(s, "packages"), exist_ok=True)
    return s


def _check_jira_claim(cfg, key):
    tok = os.environ.get("JIRA_TOKEN", "")
    if not cfg["jira"]["base"] or not tok:
        raise BrainError("Jira is not configured (jira.base in %s, JIRA_TOKEN env). Use --no-jira only for a dry "
                         "run; the runner re-checks the claim." % config.KIT_CONFIG)
    j = Jira(cfg["jira"]["base"], tok, cfg["jira"].get("api", "2"), basic_user=os.environ.get("JIRA_USER") or None)
    me = jira_user_id(j.myself())
    fields = (j.issue(key) or {}).get("fields") or {}
    if jira_user_id(fields.get("assignee")) != me:
        raise BrainError("%s is not assigned to you in Jira; claim it first" % key)
    status = ((fields.get("status") or {}).get("name")) or ""
    if status not in cfg["claim_statuses"]:
        raise BrainError("%s is '%s'; move it to one of %s first" % (key, status, cfg["claim_statuses"]))


def _claim(repo, cfg, me, key, title):
    """Tracker none: claim `key` (or the next free one). Returns the key."""
    if key:
        from .intents import key_problem
        key_problem(key, cfg["req_prefix"])     # same rule as `brain intent add --key`
    doc = claims.claim(repo, cfg["req_prefix"], me, title or "", key=key)
    return doc["key"]


def _brain_base(repo, cfg):
    if _has_origin(repo):
        git(repo, "fetch", "-q", "origin", cfg["default_branch"])
        base = "origin/" + cfg["default_branch"]
    else:
        base = "HEAD"
    return base, git_out(repo, "rev-parse", base + "^{commit}")


def _readonly_copy(src, dst):
    if os.path.exists(dst):
        _make_writable(dst)
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    for root, dirs, files in os.walk(dst):
        for f in files:
            os.chmod(os.path.join(root, f), stat.S_IRUSR | stat.S_IRGRP)
        for d in dirs:
            os.chmod(os.path.join(root, d), stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
    os.chmod(dst, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)


def _make_writable(path):
    for root, dirs, files in os.walk(path):
        os.chmod(root, stat.S_IRWXU)
        for f in files:
            os.chmod(os.path.join(root, f), stat.S_IRUSR | stat.S_IWUSR)


def attach_package(repo, store, pkg) -> dict:
    """Copy a checked package read-only into the run's workspace; return its run.json entry."""
    src = os.path.join(store, "packages", pkg)
    if not os.path.isdir(src):
        raise BrainError("package %s not found in %s" % (pkg, store))
    probs = intake.verify_package(src)
    if probs:
        raise BrainError("package %s is damaged: %s" % (pkg, "; ".join(probs)))
    _readonly_copy(src, os.path.join(repo, MATERIALS_DIR, pkg))
    return {"package": pkg, "manifest_sha256": intake.manifest_digest(src)}


def _print_report(rep):
    print("材料包 %s：%d 个文件收进 %d 段，密级 %s%s" % (rep["package"], rep["sources"], rep["segments"],
                                               rep["classification"], "（预览，未写入）" if rep.get("dry") else ""))
    for line in rep["findings"]:
        print("  " + line)
    for b in rep.get("blocking", []):
        print("  不能提交：" + b)


# ---------- install ----------

def cmd_install(a):
    repo = os.path.abspath(a.repo)
    if not os.path.isdir(repo):
        raise BrainError("not a directory: %s" % repo)
    lean_src = os.path.abspath(a.lean) if a.lean else (VENDORED_LEAN if os.path.exists(
        os.path.join(VENDORED_LEAN, "install.sh")) else None)
    if lean_src:
        lean.install(repo, lean_src)
    elif not lean.installed(repo):
        print("WARN: AI-DLC Lean is not installed: run `git submodule update --init` in brain-kit "
              "(vendor/aidlc-lean), or pass --lean <path-to-aidlc-lean>")
    for dirpath, _, files in os.walk(KIT_SRC):
        for name in files:
            src = os.path.join(dirpath, name)
            dst = os.path.join(repo, os.path.relpath(src, KIT_SRC))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
    kit_cfg = os.path.join(repo, config.KIT_CONFIG)
    if not os.path.exists(kit_cfg):
        cfg = load_json(os.path.join(TEMPLATES, "kit.json"))
        if a.team:
            cfg["team"] = a.team
        if a.prefix:
            cfg["req_prefix"] = a.prefix
        write_json(kit_cfg, cfg)
        print("Wrote %s: check tracker/req_prefix, gitlab, bot_users and pins, then commit it." % config.KIT_CONFIG)
    _exclude_run_records(repo, config.merged(config.DEFAULTS, load_json(kit_cfg)))
    bd = os.path.join(repo, load_json(kit_cfg).get("brain_dir", "brain"))
    os.makedirs(bd, exist_ok=True)
    for name in config.DEFAULTS["brain_files"]:
        p = os.path.join(bd, name)
        if not os.path.exists(p):
            shutil.copyfile(os.path.join(TEMPLATES, "brain", name), p)
    if not os.path.exists(os.path.join(repo, "AGENTS.md")):
        shutil.copyfile(os.path.join(TEMPLATES, "AGENTS.md"), os.path.join(repo, "AGENTS.md"))
    gi = os.path.join(repo, ".gitignore")
    have = []
    if os.path.exists(gi):
        with open(gi, encoding="utf-8") as f:
            have = f.read().splitlines()
    with open(gi, "a", encoding="utf-8") as f:
        for line in GITIGNORE_LINES:
            if line not in have:
                f.write(line + "\n")
    print("brain-kit %s installed in %s. Next: `brain doctor`." % (__version__, repo))
    return 0


def _exclude_run_records(repo, cfg):
    """Lean counts every file outside fingerprint_exclude as code: a park.json or MR.md the run writes after `lean
    release` would make Lean refuse the RELEASE approval the bridge sends back. Add runs/** (and Lean's own defaults
    when lean.json's list leaves them out) to aidlc-docs/lean.json; never remove an entry. The file is human-owned."""
    if not lean.installed(repo):
        return
    want = mrdesc.runs_exclude(cfg)
    try:
        added = lean.merge_fingerprint_exclude(repo, [want])
    except BrainError as e:
        print("WARN: %s; fingerprint_exclude not changed. Add \"%s\" to it by hand, or Lean counts the run records "
              "(park.json, MR.md) as code and refuses the RELEASE approval." % (e, want))
        return
    if added:
        print("Added %s to %s fingerprint_exclude: Lean does not count the run records (park.json, MR.md) as code, so "
              "writing them after `lean release` does not void the RELEASE approval. Nothing was removed. %s is "
              "human-owned (the agent cannot edit it): review and commit it." % (
                  ", ".join('"%s"' % p for p in added), lean.CONFIG, lean.CONFIG))


# ---------- doctor ----------

def _frontmatter(path) -> dict:
    out = {}
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _version_of(cmd) -> str:
    if not shutil.which(cmd[0]):
        return ""
    p = run(cmd, check=False)
    text = (p.stdout or p.stderr).strip()
    return text.splitlines()[0] if p.returncode == 0 and text else ""


def doctor_checks(repo, cfg, fetch=True) -> list:
    out = []
    add = out.append
    add(("OK", "kit config valid (%s), tracker %s, keys %s-<n>" % (config.KIT_CONFIG, cfg["tracker"],
                                                                     cfg["req_prefix"])))
    if lean.installed(repo):
        p = lean.call(repo, "doctor", check=False)
        last = ((p.stdout + p.stderr).strip().splitlines() or ["(no output)"])[-1].strip(" -")
        # Lean is a guardrail, not the gate (CI and GitLab are): its findings warn, they do not block.
        add(("OK" if p.returncode == 0 else "WARN", "AI-DLC Lean doctor: %s" % ("ok" if p.returncode == 0 else last)))
        why = mrdesc.write_problem(repo, cfg)
        add(("FAIL", "%s fingerprint_exclude: %s。%s" % (lean.CONFIG, why, mrdesc.exclude_fix(cfg))) if why else
            ("OK", "Lean does not count %s/ as code (%s fingerprint_exclude)" % (cfg["runs_dir"], lean.CONFIG)))
        hook = os.path.join(repo, CREW_HOOK)
        if os.path.isfile(hook):
            with open(hook, encoding="utf-8", errors="replace") as f:
                why = crew_hook_problem(f.read())
            add(("FAIL", why) if why else
                ("OK", "%s lets a run write its own %s/<KEY>/park.json whatever Lean's approvals (version %d)"
                 % (CREW_HOOK, cfg["runs_dir"], CREW_HOOK_VERSION)))
        else:
            add(("FAIL", "%s missing in this repo (run `brain install`): KiroCrew sessions, the team runner's too, run "
                         "Lean through it" % CREW_HOOK))
    else:
        add(("FAIL", "AI-DLC Lean not installed"))
    agent = os.path.join(repo, ".kiro", "agents", "brain-dev.md")
    if os.path.exists(agent):
        fm = _frontmatter(agent)
        model = fm.get("model", "")
        add(("OK" if model == cfg["model"] else "FAIL", "brain-dev model: %s (want %s)" % (model or "-", cfg["model"])))
        bad = [t for t in cfg["forbidden_tools"] if t in fm.get("tools", "")]
        add(("FAIL" if bad else "OK", "brain-dev forbidden tools: %s" % (", ".join(bad) or "none")))
    else:
        add(("FAIL", "missing .kiro/agents/brain-dev.md (run `brain install`)"))
    bd = os.path.join(repo, cfg["brain_dir"])
    missing = [n for n in cfg["brain_files"] if not os.path.exists(os.path.join(bd, n))]
    add(("FAIL" if missing else "OK", "Brain files in %s/: %s" % (cfg["brain_dir"], "missing " + ", ".join(missing)
                                                                  if missing else "all present")))
    add(("OK" if os.path.exists(os.path.join(repo, "AGENTS.md")) else "FAIL", "AGENTS.md at repo root"))
    ignored = git(repo, "check-ignore", "-q", MATERIALS_DIR + "/x", check=False).returncode == 0
    add(("OK" if ignored else "FAIL", "%s/ is git-ignored (materials never get committed)" % MATERIALS_DIR))
    if _has_origin(repo) and fetch:
        p = git(repo, "fetch", "-q", "origin", cfg["default_branch"], check=False)
        if p.returncode != 0:
            add(("WARN", "cannot fetch origin/%s: Brain freshness unknown" % cfg["default_branch"]))
        else:
            stale = git(repo, "diff", "--quiet", "HEAD", "origin/" + cfg["default_branch"], "--",
                        cfg["brain_dir"], "AGENTS.md", check=False).returncode != 0
            add(("WARN" if stale else "OK", "Brain vs origin/%s: %s" % (cfg["default_branch"],
                                                                         "differs, merge or rebase" if stale else "same")))
    else:
        add(("WARN", "no origin remote: Brain freshness not checked"))
    for tool, cmd in (("kiro-cli", ["kiro-cli", "--version"]), ("kirocrew", ["kirocrew", "--version"])):
        want = cfg["pins"].get(tool, "")
        have = _version_of(cmd)
        if not have:
            add(("WARN", "%s not found on PATH" % tool))
        elif want and want not in have:
            add(("FAIL", "%s version %s, pinned %s" % (tool, have, want)))
        else:
            add(("OK", "%s %s%s" % (tool, have, "" if want else " (not pinned)")))
    return out


CREW_EVENTS = (("agentSpawn", "SessionStart"), ("userPromptSubmit", "UserPromptSubmit"), ("preToolUse", "PreToolUse"))


def agent_hooks(spec) -> list:
    """[(trigger, command, matcher)] from an agent spec in either format: the original
    {"hooks": {"agentSpawn": [{"command": ...}]}} or the list Kiro rewrites specs into on upgrade (kiroUpgrade,
    seen 2026-11-08): {"hooks": [{"trigger": "agentSpawn", "action": {"type": "command", "command": ...}}]}.
    matcher is None when the hook has none."""
    hooks = spec.get("hooks") if isinstance(spec, dict) else None
    out = []
    if isinstance(hooks, dict):
        for trig, rows in hooks.items():
            for h in rows if isinstance(rows, list) else []:
                if isinstance(h, dict):
                    out.append((str(trig), str(h.get("command") or ""), h.get("matcher")))
    elif isinstance(hooks, list):
        for h in hooks:
            if isinstance(h, dict):
                act = h.get("action") if isinstance(h.get("action"), dict) else {}
                out.append((str(h.get("trigger") or ""), str(act.get("command") or h.get("command") or ""),
                            h.get("matcher")))
    return out


def agent_hook_commands(spec) -> dict:
    """{trigger: [command, ...]} (see agent_hooks; kept for callers of the earlier interface)."""
    out = {}
    for t, c, _ in agent_hooks(spec):
        out.setdefault(t, []).append(c)
    return out


def _is_lean(ev, command) -> bool:
    return CREW_HOOK in command and ev in command


def _lean_hook_ok(trigger, ev, command, matcher) -> bool:
    """Lean's hook for `ev`, and for PreToolUse one that sees every tool call: matcher "*" or none (a guard that
    only sees some tools lets the others through)."""
    return _is_lean(ev, command) and (trigger != "preToolUse" or matcher in (None, "", "*"))


def missing_lean_hooks(spec) -> list:
    """[(trigger, lean event)] of Lean's three hooks the spec does not carry (as they must be, under their trigger)."""
    hooks = agent_hooks(spec)
    return [(t, ev) for t, ev in CREW_EVENTS if not any(tt == t and _lean_hook_ok(t, ev, c, m) for tt, c, m in hooks)]


def add_lean_hooks(spec, missing) -> dict:
    """Add the missing hooks in the spec's own format (Kiro's list format for an upgraded spec, even one without
    hooks yet); a Lean hook for the same trigger that is wrong (a PreToolUse guard with a narrow matcher) is replaced.
    Everything else (other tools' hooks, kiroUpgrade) stays."""
    spec = dict(spec)
    hooks = spec.get("hooks")
    wrong = {(t, ev) for t, ev in missing}

    def stale(trig, command):
        return any(trig == t and _is_lean(ev, command) for t, ev in wrong)

    if isinstance(hooks, list) or ("kiroUpgrade" in spec and not hooks):
        out = [h for h in (hooks or []) if not (isinstance(h, dict) and stale(
            str(h.get("trigger") or ""), str((h.get("action") if isinstance(h.get("action"), dict) else {})
                                             .get("command") or h.get("command") or "")))]
        for t, ev in missing:
            h = {"name": "brain-%s" % t, "trigger": t,
                 "action": {"type": "command", "command": "python3 %s %s" % (CREW_HOOK, ev)}, "timeout": 10}
            if t == "preToolUse":
                h["matcher"] = "*"
            out.append(h)
    else:
        out = {k: [h for h in v if not (isinstance(h, dict) and stale(str(k), str(h.get("command") or "")))]
               if isinstance(v, list) else v for k, v in hooks.items()} if isinstance(hooks, dict) else {}
        for t, ev in missing:
            h = {"command": "python3 %s %s" % (CREW_HOOK, ev)}
            if t == "preToolUse":
                h = {"matcher": "*", "command": h["command"]}
            out[t] = (out[t] if isinstance(out.get(t), list) else []) + [h]
    spec["hooks"] = out
    return spec


# brain-dev.json fields brain-kit writes when they are absent; the owner may set their own (a model their KiroCrew
# offers, an MCP tool): `brain crew-agent --reset` takes brain-kit's again. Any other field (Kiro's own, other
# tools') and other tools' hooks are left as they are. What brain-kit enforces is Lean's hooks.
CREW_OWNED_FIELDS = ("name", "description", "model", "prompt", "tools", "resources")


def crew_agent_custom(have, ship) -> list:
    """brain-kit's fields the installed spec sets to its own values (kept unless `--reset`)."""
    return [k for k in CREW_OWNED_FIELDS if k in ship and k in have and have[k] != ship[k]]


def crew_agent_update(have, ship, reset=False) -> tuple:
    """(spec, what changes) to bring an installed brain-dev.json up to what brain-kit needs: Lean's hooks, and
    brain-kit's fields where the spec has none. reset=True: brain-kit's fields replace the owner's as well (not
    when the installed one has another JSON type: Kiro rewrote it, leave it)."""
    new, changed = dict(have), []
    for k in CREW_OWNED_FIELDS:
        if k not in ship or have.get(k) == ship[k]:
            continue
        if k in have and (not reset or type(have[k]) is not type(ship[k])):
            continue
        new[k] = ship[k]
        changed.append(k)
    missing = missing_lean_hooks(new)
    if missing:
        new = add_lean_hooks(new, missing)
        changed += ["Lean's %s hook" % t for t, _ in missing]
    return new, changed


def crew_checks(repo) -> list:
    """KiroCrew: is Lean wired in (agent-spec hooks) and is the engine the one we tested (KAS / v3)?"""
    out = []
    if not shutil.which("kirocrew"):
        return out
    p = crew_agent_path()
    if not os.path.exists(p):
        out.append(("WARN", "KiroCrew: ~/.kiro/agents/brain-dev.json missing; Lean's hooks will not run in KiroCrew "
                            "(run `brain crew-agent`)"))
    else:
        try:
            have = load_json(p)
            missing = missing_lean_hooks(have)
            add = ("OK", "KiroCrew brain-dev carries Lean's 3 hooks") if not missing else \
                ("FAIL", "KiroCrew brain-dev.json lacks Lean's %s hook(s) as Lean needs them (run `brain crew-agent "
                         "--force`)" % ", ".join(t for t, _ in missing))
            out.append(add)
            ship = load_json(CREW_AGENT_SRC)
            _, changed = crew_agent_update(have, ship)
            fields = [c for c in changed if c in CREW_OWNED_FIELDS]
            if fields:
                out.append(("WARN", "KiroCrew brain-dev.json has no %s (`brain crew-agent --force` adds brain-kit's)"
                                    % ", ".join(fields)))
            custom = crew_agent_custom(have, ship)
            if custom:
                out.append(("OK", "KiroCrew brain-dev uses your own %s (`brain crew-agent --reset` takes brain-kit's)"
                                  % ", ".join(custom)))
        except ValueError:
            out.append(("FAIL", "~/.kiro/agents/brain-dev.json is not valid JSON"))
    if not os.path.exists(os.path.join(repo, CREW_HOOK)) and not lean.installed(repo):   # else doctor_checks says it
        out.append(("FAIL", "%s missing in this repo (run `brain install`)" % CREW_HOOK))
    be = run(["kirocrew", "config", "get", "agent.acp_backend"], check=False).stdout.strip().splitlines()
    be = be[-1].strip() if be else ""
    out.append(("OK" if be in ("kas", "") else "WARN",
                "KiroCrew engine: %s%s" % (be or "default (KAS)",
                                           "" if be in ("kas", "") else " (Lean via agent hooks is tested on kas / v3 only)")))
    return out


def cmd_crew_agent(a):
    p = crew_agent_path()
    ship = load_json(CREW_AGENT_SRC)
    if os.path.exists(p):
        try:
            have = load_json(p)
        except ValueError:
            raise BrainError("%s is not valid JSON; fix or remove it by hand" % p)
        if not isinstance(have, dict):
            raise BrainError("%s is not an agent spec (a JSON object); fix or remove it by hand" % p)
        reset = getattr(a, "reset", False)
        new, changed = crew_agent_update(have, ship, reset=reset)
        custom = [] if reset else crew_agent_custom(have, ship)
        kept = (" Your own %s stay (`brain crew-agent --reset` takes brain-kit's)." % ", ".join(custom)) if custom \
            else ""
        if not changed:
            print("%s is up to date (Lean's hooks included).%s" % (p, kept))
            return 0
        if not (a.force or reset):
            raise BrainError("%s lacks %s. Rerun with --force to add them (your model, tools, resources, prompt and "
                             "other tools' hooks stay)" % (p, ", ".join(changed)))
        write_json(p, new)
        print("%s %s: %s; other tools' hooks and other settings kept.%s"
              % ("Reset" if reset else "Updated", p, ", ".join(changed), kept))
        return 0
    os.makedirs(os.path.dirname(p), exist_ok=True)
    write_json(p, ship)
    print("Installed %s. In KiroCrew pick agent brain-dev; use engine kas (v3): "
          "`kirocrew config set agent.acp_backend kas`." % p)
    return 0


def cmd_doctor(a):
    repo = repo_root()
    cfg = config.load(repo)
    rows = doctor_checks(repo, cfg, fetch=not a.offline) + crew_checks(repo)
    for level, msg in rows:
        print("%-4s %s" % (level, msg))
    return 1 if any(l == "FAIL" for l, _ in rows) else 0


# ---------- claim / intake ----------

def cmd_claim(a):
    repo = repo_root()
    cfg = config.load(repo)
    if cfg["tracker"] != "none":
        raise BrainError("tracker is %s: claim in Jira instead" % cfg["tracker"])
    me = _me(repo, cfg)
    if me in cfg["bot_users"]:
        raise BrainError("%s is a bot account" % me)
    key = _claim(repo, cfg, me, a.key, a.title)
    print("Claimed %s for %s." % (key, me))
    return 0


def _intake(repo, cfg, a, key, me):
    rep = intake.build(a.doc, key, _store(repo, a.store), submitter=me, consent=a.consent,
                       classification=a.classification, business=a.business, kit_intake=cfg["intake"],
                       dry_run=getattr(a, "dry_run", False), exclude=a.exclude or ())
    rep["dry"] = getattr(a, "dry_run", False)
    _print_report(rep)
    return rep


def cmd_intake(a):
    repo = repo_root()
    cfg = config.load(repo)
    if not JIRA_RE.match(a.key or ""):
        raise BrainError("--key must be a requirement key like %s-1" % cfg["req_prefix"])
    rep = _intake(repo, cfg, a, a.key, _me(repo, cfg))
    return 1 if rep.get("blocking") else 0


# ---------- start ----------

def cmd_start(a):
    repo = repo_root()
    cfg = config.load(repo)
    slug = a.slug.lower()
    if not SLUG_RE.match(slug):
        raise BrainError("slug must be lowercase letters, digits and '-' (max 40)")
    if _dirty(repo):
        raise BrainError("working tree has uncommitted changes; commit or stash first")
    if not lean.installed(repo):
        raise BrainError("AI-DLC Lean is not installed; run `brain install --lean <path>`")
    me = _me(repo, cfg)
    if me in cfg["bot_users"]:
        raise BrainError("%s is a bot account; local runs belong to a person" % me)
    if a.doc and a.package:
        raise BrainError("use --doc (take documents in now) or --package (an existing package), not both")
    if a.doc and (not a.consent or not a.classification):
        raise BrainError("--doc needs --consent and --classification (see `brain intake --help`)")
    from . import intents
    doc = None
    if cfg["tracker"] == "jira":
        if not a.key or not JIRA_RE.match(a.key):
            raise BrainError("tracker is jira: pass --key <JIRA key>")
        key = a.key
        if not a.no_jira:
            _check_jira_claim(cfg, key)
        checked = not a.no_jira
    else:
        key = a.key
        if key:
            doc = claims.read(repo, key)
            if doc and doc.get("owner") != me:
                raise BrainError("%s is claimed by %s" % (key, doc.get("owner")))
            if doc and doc.get("closed"):
                raise BrainError("%s is closed (%s)" % (key, (doc["closed"] or {}).get("reason", "")))
            # a malformed claim is refused; content today's rules flag in text accepted when it was filed (it is
            # on the claim branch already) is shown, not refused
            bad = intents.doc_problems(doc, content=False) if intents.is_intent(doc) else []
            if bad:
                raise BrainError("claim/%s: %s" % (key, "; ".join(bad)))
            for w in (intents.doc_problems(doc) if intents.is_intent(doc) else []):
                if w not in bad:
                    print("WARN: claim/%s: %s (filed before this rule; check runs/%s/%s before you hand over)"
                          % (key, w, key, intents.INTENT_MD))
        key = _claim(repo, cfg, me, key, a.title or (doc or {}).get("title") or slug.replace("-", " "))
        checked = True
    title = a.title or (doc or {}).get("title") or slug.replace("-", " ")
    rec_path = runrec.path(cfg["runs_dir"], key)
    base, brain_commit = _brain_base(repo, cfg)
    if git(repo, "cat-file", "-e", "%s:%s" % (base, rec_path), check=False).returncode == 0:
        raise BrainError("%s already exists on %s: this requirement already has a run" % (rec_path, base))
    br = branch_name(cfg["branch_prefix"], me, key, slug)
    if git(repo, "rev-parse", "--verify", "-q", "refs/heads/" + br, check=False).returncode == 0:
        raise BrainError("branch %s already exists" % br)
    if _has_origin(repo) and git(repo, "ls-remote", "--exit-code", "--heads", "origin", br,
                                 check=False).returncode == 0:
        raise BrainError("branch %s already exists on origin" % br)
    materials = []
    store = _store(repo, a.store)
    if a.doc:
        rep = _intake(repo, cfg, a, key, me)
        a.package = rep["package"]
    git(repo, "switch", "-q", "-c", br, base)
    if a.package:
        materials.append(attach_package(repo, store, a.package))
    intent = lean.new_intent(repo, "%s-%s" % (key.lower(), slug), "%s %s" % (key, title))
    paths = [p.strip() for p in (a.paths or "").split(",") if p.strip()]
    rec = runrec.new(key, me, br, intent, brain_commit, __version__, paths, iso(utcnow()), checked,
                     tracker=cfg["tracker"], materials=materials)
    write_json(os.path.join(repo, rec_path), rec)
    intent_md = []    # the claim's text (an intent from debt, alert, schedule, eval or gap) for the run's agent
    if intents.is_intent(doc):
        intent_md.append(os.path.join(cfg["runs_dir"], key, intents.INTENT_MD))
        with open(os.path.join(repo, intent_md[0]), "w", encoding="utf-8") as f:
            f.write(intents.render_md(doc))
    git(repo, "add", "--", rec_path, os.path.join(lean.DOCS, intent), *intent_md)
    if os.path.isdir(os.path.join(repo, lean.DOCS, "audit")):
        git(repo, "add", "--", os.path.join(lean.DOCS, "audit"))
    git(repo, "commit", "-q", "-m", "%s: start run (brain %s)" % (key, brain_commit[:10]))
    pushed = False
    if _has_origin(repo) and not a.no_push:
        git(repo, "push", "-q", "-u", "origin", br)
        pushed = True
    print("Started %s on %s (Lean intent %s, Brain %s)%s." % (key, br, intent, brain_commit[:10],
                                                                ", pushed" if pushed else ""))
    if materials:
        print("Materials: %s/%s/ (read-only; start from toc.md)" % (MATERIALS_DIR, materials[0]["package"]))
    if intent_md:
        print("Intent: %s (source %s; the agent reads it first)" % (intent_md[0], doc.get("source", "product")))
    print("Next: open Kiro here with the brain-dev agent; keep %s/%s/aidlc-state.md current;"
          " `brain handoff` when the runner should take over." % (lean.DOCS, intent))
    return 0


# ---------- handoff ----------

def _current(repo, cfg):
    br = git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    b = parse_branch(br, cfg["branch_prefix"])
    if not b:
        raise BrainError("current branch %s is not a run branch (%s/<you>/<KEY>-<slug>)" % (br, cfg["branch_prefix"]))
    rp = os.path.join(repo, runrec.path(cfg["runs_dir"], b["key"]))
    if not os.path.exists(rp):
        raise BrainError("%s is missing on this branch" % os.path.relpath(rp, repo))
    rec = load_json(rp)
    probs = runrec.validate(rec, branch=br, prefix=cfg["branch_prefix"])
    if probs:
        raise BrainError("run.json: " + "; ".join(probs))
    return br, b, rp, rec


def publish_materials(store, rec) -> list:
    """Make sure each package is in the shared intake repo. Returns warnings (empty when all published)."""
    warn = []
    for m in rec.get("materials", []):
        pdir = os.path.join(store, "packages", m["package"])
        if not os.path.isdir(pdir) or intake.manifest_digest(pdir) != m["manifest_sha256"]:
            raise BrainError("package %s in %s does not match run.json" % (m["package"], store))
    if not rec.get("materials"):
        return warn
    if git(store, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        return ["intake store %s is not a git clone of brain-intake: the team runner cannot get the materials" % store]
    for m in rec["materials"]:
        git(store, "add", "--", os.path.join("packages", m["package"]))
    if git(store, "diff", "--cached", "--quiet", check=False).returncode != 0:
        git(store, "commit", "-q", "-m", "materials for %s" % rec["key"])
    if git(store, "remote", "get-url", "origin", check=False).returncode == 0:
        # someone else may have published since this clone last pulled: take theirs first (each package is its own
        # folder, so a rebase does not meet a conflict), then push; a push that loses a race is tried once more
        branch = git_out(store, "rev-parse", "--abbrev-ref", "HEAD")
        for attempt in (1, 2):
            git_net(store, "pull", "-q", "--rebase", "origin", branch, check=False)    # fails on a first publish
            gdir = git_out(store, "rev-parse", "--absolute-git-dir")
            if any(os.path.isdir(os.path.join(gdir, d)) for d in ("rebase-merge", "rebase-apply")):
                git(store, "rebase", "--abort", check=False)
                raise BrainError("intake store %s: the materials do not rebase onto origin/%s; sort it out in that "
                                 "clone and run this again" % (store, branch))
            if git_net(store, "push", "-q", "origin", "HEAD", check=attempt == 2).returncode == 0:
                break
    else:
        warn.append("intake store has no origin: packages are committed locally only")
    return warn


def _label_check(gl, project, iid, sha, timeout=None):
    """('', mr) when the MR may be labelled for `sha`: GitLab has recorded its push (an MR version) and the MR head
    is still `sha`. Otherwise (why, mr or None). The bridge hands over the MR head it sees when the label lands, so
    only the commit this command pushed is labelled: if someone pushed on top of it, the label would hand over
    their commit under the owner's name."""
    from .rest import head_pushed_at
    if timeout is None:
        try:
            timeout = max(0.0, float(os.environ.get("BRAIN_MR_VERSION_WAIT", "60")))
        except ValueError:
            timeout = 60.0
    deadline = time.monotonic() + timeout
    while True:
        try:
            recorded = head_pushed_at(gl.mr_versions(project, iid), sha) is not None
        except BrainError:
            recorded = False
        if recorded:
            mr = gl.mr(project, iid) or {}
            if mr.get("sha") == sha:
                return "", mr
            return ("the MR head is now %s, not %s which this hand-over pushed: someone pushed to the branch after "
                    "you" % (str(mr.get("sha") or "?")[:12], sha[:12])), mr
        left = deadline - time.monotonic()
        if left <= 0:
            return "GitLab has not recorded the push of %s on the MR within %ds" % (sha[:12], timeout), None
        time.sleep(min(2.0, left))


def _label_names(mr) -> list:
    return [x if isinstance(x, str) else str((x or {}).get("name") or "") for x in (mr or {}).get("labels") or []]


def runner_kit_problems(repo) -> list:
    """What the team runner's session needs in git (not just on this disk): Lean, and the hook adapter that
    carries Lean's hooks in KiroCrew. Without the adapter every tool call of the runner's session is blocked."""
    probs = []
    for path, why in ((".kiro/aidlc-lean/aidlc_lean.py", "AI-DLC Lean"),
                      (CREW_HOOK, "the KiroCrew hook adapter (brain install wrote it)")):
        if git(repo, "ls-files", "--error-unmatch", "--", path, check=False).returncode != 0:
            probs.append("%s is not committed: %s; commit it on main through an MR" % (path, why))
        elif path == CREW_HOOK:
            p = git(repo, "show", ":" + CREW_HOOK, check=False)       # the committed (staged) file, not the disk's
            why_old = crew_hook_problem(p.stdout if p.returncode == 0 else "")
            if why_old:
                probs.append(why_old)
    return probs


def _risk_matches(path, pats, prefix=False) -> bool:
    """Does `path` fall under a risk-paths pattern? prefix=True: `path` is a folder or glob from run.json
    expected_paths ("src/pay"), which names everything below it too."""
    from . import ci
    cands = [path] + ([path.rstrip("/") + "/"] if prefix else [])
    return any(ci.glob_match(c, x) for c in cands for x in pats)


def handoff_risk_warnings(repo, cfg, rec) -> list:
    """[] or what the owner (and the hand-over note) must know: this run changes, or plans to change (run.json
    expected_paths), files under gates/risk-paths and no DESIGN approval signed by the bridge covers them yet. brain-ci
    check-run needs one for the MR; a DESIGN approved in the owner's own Lean chat does not count. So the runner asks
    for it through the bridge (lean request DESIGN, park.json with lean_request DESIGN) before it writes more code."""
    from . import ci
    from .commands.trace_cmd import default_vouchers
    ref = next((r for r in ("origin/" + cfg["default_branch"], cfg["default_branch"])
                if git(repo, "rev-parse", "--verify", "-q", r + "^{commit}", check=False).returncode == 0), None)
    text = git(repo, "show", "%s:%s" % (ref, cfg["risk_paths_file"]), check=False).stdout if ref else ""
    if not text and os.path.isfile(os.path.join(repo, cfg["risk_paths_file"])):
        with open(os.path.join(repo, cfg["risk_paths_file"]), encoding="utf-8", errors="replace") as f:
            text = f.read()
    pats = [x for x in (line.split("#", 1)[0].strip() for line in (text or "").splitlines()) if x]
    if not pats:
        return []
    base = git(repo, "merge-base", ref, "HEAD", check=False).stdout.strip() if ref else ""
    changed = [p for p in (ci.changed_paths(repo, base, "HEAD") if base else []) if _risk_matches(p, pats)]
    planned = [p for p in rec.get("expected_paths") or [] if isinstance(p, str) and _risk_matches(p, pats, True)]
    if not changed and not planned:
        return []
    vdir, signers = default_vouchers(repo), os.environ.get("BRAIN_ALLOWED_SIGNERS") or ""
    if changed and vdir and signers and os.path.isfile(signers):
        design = "%s/%s/inception/application-design/application-design.md" % (lean.DOCS, rec["intent"])
        try:
            vouchers, _ = ci.signed_vouchers(vdir, signers, rec["key"], repo, upstream=base or None)
            approvals = [v for v in ci.latest_design_answers(repo, vouchers) if v.get("choice") == "approve"]
            if any(not ci.design_approval_covers(repo, v, design, changed) for v in approvals):
                return []
        except (BrainError, OSError, ValueError) as e:
            print("WARN: bridge-signed DESIGN approvals not checked here (%s)" % e)
    shown = ", ".join((changed or planned)[:5])
    out = ["this run %s risk paths (%s; %s) and no DESIGN approval signed by the bridge covers %s yet: brain-ci "
           "check-run will fail the MR until one does. A DESIGN approved in your own Lean chat does not count. The "
           "runner asks for it through the bridge (lean request DESIGN, then park.json with lean_request DESIGN) "
           "before it writes more code." % ("changes" if changed else "plans to change (run.json expected_paths)",
                                            shown, cfg["risk_paths_file"], "them" if len(changed or planned) > 1
                                            else "it")]
    note = lean.state_path(repo, rec["intent"])
    steps = []
    if os.path.isfile(note):
        with open(note, encoding="utf-8", errors="replace") as f:
            steps = lean.next_steps(f.read())
    if not any("DESIGN" in s for s in steps):
        out.append("the hand-over note (%s, ### Next) does not say so; its first item should be: \"Ask for the "
                   "DESIGN approval through the bridge (risk paths %s) before more code\"" % (
                       os.path.relpath(note, repo), shown))
    return out


def handoff_description(repo, cfg, key):
    """Write runs/<KEY>/MR.md from the records (mrdesc.build / render) and, when it changed, amend it into the
    hand-over commit just made: the MR head stays the one hand-over commit the bridge takes (one parent, it changes
    run.json). Returns the description, or None after a warning when it could not be written (the hand-over goes
    on without it; the working tree is left clean)."""
    from .commands.trace_cmd import default_vouchers
    md = mrdesc.md_path(cfg, key)
    try:
        text = mrdesc.render(mrdesc.build(repo, cfg, key, vouchers_dir=default_vouchers(repo),
                                          allowed_signers=os.environ.get("BRAIN_ALLOWED_SIGNERS") or None))
        mrdesc.write(repo, cfg, key, text)
        git(repo, "add", "--", md)
        if git(repo, "diff", "--cached", "--quiet", "--", md, check=False).returncode != 0:
            git(repo, "commit", "-q", "--amend", "--no-edit")
    except Exception as e:  # noqa: BLE001  the description helps the reviewers; it does not hold up the hand-over
        git(repo, "reset", "-q", "--", md, check=False)
        if git(repo, "cat-file", "-e", "HEAD:%s" % md, check=False).returncode == 0:
            git(repo, "checkout", "-q", "HEAD", "--", md, check=False)
        elif os.path.lexists(os.path.join(repo, md)):
            os.remove(os.path.join(repo, md))
        print("WARN: %s not written (%s): the hand-over goes on without the MR description."
              % (md, str(e).replace(repo, ".")))
        return None
    print("Wrote %s from the records into the hand-over commit (`brain mr-desc` regenerates it)." % md)
    return text


def _create_mr(gl, project, branch, target, title, desc):
    """A new MR with the description; when GitLab refuses it with one, without (and a warning)."""
    if desc is None:
        return gl.create_mr(project, branch, target, title, [])
    try:
        return gl.create_mr(project, branch, target, title, [], description=desc)
    except HttpError as e:
        print("WARN: GitLab did not take the MR description (%s); opening the MR without it." % e)
        return gl.create_mr(project, branch, target, title, [])


def cmd_handoff(a):
    repo = repo_root()
    cfg = config.load(repo)
    br, b, rp, rec = _current(repo, cfg)
    me = _me(repo, cfg)
    if me != rec["owner"]:
        raise BrainError("this run belongs to %s; only the owner hands it off" % rec["owner"])
    probs = lean.handover_problems(repo, rec["intent"])
    if probs:
        raise BrainError("hand-over note not ready (%s/%s/aidlc-state.md): %s"
                         % (lean.DOCS, rec["intent"], "; ".join(probs)))
    rel = os.path.relpath(rp, repo)
    md = mrdesc.md_path(cfg, rec["key"])
    dirty = _dirty(repo, ignore=(rel, md))     # MR.md is generated again below
    if dirty:
        raise BrainError("commit your work first:\n  " + "\n  ".join(dirty))
    missing = runner_kit_problems(repo)
    if missing:
        raise BrainError("the team runner could not run this branch: " + "; ".join(missing))
    why = mrdesc.write_problem(repo, cfg, rec["key"])
    if why:
        raise BrainError("not handed over: Lean would refuse this run's RELEASE approval later. %s。%s"
                         % (why, mrdesc.exclude_fix(cfg)))
    try:
        warnings = handoff_risk_warnings(repo, cfg, rec)
    except (BrainError, OSError, ValueError) as e:     # a warning only: it never holds up the hand-over
        warnings = ["risk paths not checked (%s)" % e]
    for w in warnings:
        print("WARN: " + w)
    if not a.no_push:
        for w in publish_materials(_store(repo, a.store), rec):
            print("WARN: " + w)
    rec["handoff"] = {"requested_at": iso(utcnow()), "by": me, "local_head": git_out(repo, "rev-parse", "HEAD")}
    write_json(rp, rec)
    git(repo, "add", "--", rel)
    git(repo, "commit", "-q", "-m", "%s: hand off to team runner" % rec["key"])
    desc = handoff_description(repo, cfg, rec["key"])
    if a.no_push or not _has_origin(repo):
        print("Committed the hand-over; not pushed. Push %s and add label '%s' to its MR%s." % (
            br, cfg["handoff_label"], " (its description is %s)" % md if desc is not None else ""))
        return 0
    git(repo, "push", "-q", "-u", "origin", br)
    if a.no_label:
        print("Pushed %s. Add label '%s' to its MR when ready." % (br, cfg["handoff_label"]))
        return 0
    gl = _gitlab(cfg)
    if not gl or not cfg["gitlab"]["project"]:
        print("Pushed %s. GitLab not configured here: add label '%s' to its MR by hand." % (br, cfg["handoff_label"]))
        return 0
    proj = cfg["gitlab"]["project"]
    head = git_out(repo, "rev-parse", "HEAD")
    mr = gl.mr_for_branch(proj, br)
    if not mr:
        mr = _create_mr(gl, proj, br, cfg["default_branch"], "%s %s" % (rec["key"], b["slug"].replace("-", " ")), desc)
    elif desc is not None:
        try:
            gl.edit_mr(proj, mr["iid"], description=desc)
        except BrainError as e:
            print("WARN: the description of MR !%s was not set (%s); it is in %s on the branch. The hand-over goes on."
                  % (mr["iid"], e, md))
    # The bridge only takes a hand-over whose label is newer than the push GitLab recorded, and whose MR head is
    # this hand-over commit. GitLab records the MR version a moment after the push: wait for it, then look at the
    # MR head once more right before labelling.
    label = cfg["handoff_label"]
    why, now = _label_check(gl, proj, mr["iid"], head)
    if why:
        # exit 1: nothing was handed over (a caller going by the exit status must not take it for a hand-over)
        print("Pushed %s, but NOT labelled MR !%s: %s.\nNothing was handed over. Look at the MR: if its latest version "
              "is %s and nothing came after it, add the label '%s' by hand; if someone pushed on top, pull, look at "
              "their commit and run `brain handoff` again." % (head[:12], mr["iid"], why, head[:12], label))
        return 1
    if label in _label_names(now):
        # GitLab records no event when a label the MR already has is added again; the bridge would see the old one
        gl.edit_labels(proj, mr["iid"], remove=[label])
    gl.edit_labels(proj, mr["iid"], add=[label], remove=[cfg["rejected_label"]])
    print("Handed off: MR !%s labelled '%s' at %s. The bridge checks it and the team runner continues." %
          (mr["iid"], label, head[:12]))
    return 0


# ---------- status / lint ----------

def cmd_status(a):
    repo = repo_root()
    cfg = config.load(repo)
    br, b, rp, rec = _current(repo, cfg)
    print(json.dumps({k: rec.get(k) for k in ("key", "owner", "branch", "intent", "brain_commit", "origin",
                                              "materials", "handoff")}, indent=2, ensure_ascii=False))
    print(lean.call(repo, "status", check=False).stdout.strip())
    return 0


def _print_lint(errors, warnings):
    for w in warnings:
        print("WARN", w)
    for e in errors:
        print("FAIL", e)


def _default_base(repo, cfg):
    """Lint compares with the merge-base of HEAD and main: on main itself that is HEAD (your uncommitted edits)."""
    for ref in ("origin/" + cfg["default_branch"], cfg["default_branch"]):
        if git(repo, "rev-parse", "-q", "--verify", ref + "^{commit}", check=False).returncode == 0:
            mb = git(repo, "merge-base", "HEAD", ref, check=False).stdout.strip()
            if mb:
                return mb
    return None


def _agents_text(repo, ref=None):
    if ref is None:
        p = os.path.join(repo, "AGENTS.md")
        if not os.path.exists(p):
            return ""
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    return git(repo, "show", "%s:AGENTS.md" % ref, check=False).stdout


def _target_brain(repo, cfg):
    ref = "origin/" + cfg["default_branch"]
    if git(repo, "rev-parse", "-q", "--verify", ref + "^{commit}", check=False).returncode != 0:
        return None
    return entries.Brain.from_git(repo, ref, cfg["brain_dir"])


def cmd_lint(a):
    repo = repo_root()
    cfg = config.load(repo)
    base_ref = None if a.static else (a.base or _default_base(repo, cfg))
    base = entries.Brain.from_git(repo, base_ref, cfg["brain_dir"]) if base_ref else None
    brain = entries.Brain.load(os.path.join(repo, cfg["brain_dir"]))
    errors, warnings = brainlint.check(brain, base=base, target=_target_brain(repo, cfg) if base else None,
                                       purge_approvers=cfg.get("purge_approvers") or [])
    if base_ref:
        e2, w2 = brainlint.check_agents(_agents_text(repo), _agents_text(repo, base_ref))
        errors, warnings = errors + e2, warnings + w2
    _print_lint(errors, warnings)
    print("brain lint: %d 个条目%s：%d 个错误，%d 个提醒" % (
        len(brain.main()), "，对照 %s" % git_out(repo, "rev-parse", "--short", base_ref) if base_ref else "（只查静态规则）",
        len(errors), len(warnings)))
    return 1 if errors else 0


# ---------- Brain entries (the deterministic editor) ----------

def _entry_key(repo, cfg, given):
    if given:
        return given
    br = git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    m = re.match(r"^brain/([A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,6})(?:-|$)", br)
    if m:
        return m.group(1)
    b = parse_branch(br, cfg["branch_prefix"])
    if b:
        return b["key"]
    raise BrainError("--key is required: the requirement this knowledge comes from (e.g. OMS-1)")


def _entry_text(a):
    if getattr(a, "text_file", None):
        if a.text_file == "-":
            return sys.stdin.read()
        with open(a.text_file, encoding="utf-8") as f:
            return f.read()
    return getattr(a, "text", None) or ""


def cmd_entry(a):
    repo = repo_root()
    cfg = config.load(repo)
    bd = os.path.join(repo, cfg["brain_dir"])
    if a.op == "show":
        brain = entries.Brain.load(bd)
        hit = brain.main().get(a.id) or brain.archived().get(a.id)
        if not hit:
            raise BrainError("entry %s not found" % a.id)
        print("%s\n%s" % (hit[1].header(), "\n".join(hit[1].body)))
        return 0
    before = entries.Brain.load(bd)
    brain = entries.Brain.load(bd)
    used = reflect.used_on_branches(repo, cfg["brain_dir"]) if a.op in ("add", "replace") else set()
    if a.op == "add":
        res = brain.new(a.file, a.type, _entry_text(a), _entry_key(repo, cfg, a.key), a.src, section=a.section,
                        used_extra=used)
    elif a.op == "replace":
        res = brain.replace(a.id, _entry_text(a), _entry_key(repo, cfg, a.key), a.src, etype=a.type, used_extra=used)
    elif a.op == "withdraw":
        res = brain.withdraw(a.id, a.reason, a.src)
    elif a.op == "same":
        res = brain.same(a.id, a.src)
    elif a.op == "adds-to":
        res = brain.adds_to(a.id, _entry_text(a), a.src)
    elif a.op == "repeat":
        n = brain.repeat(a.id, a.src)
        res = "%s（第 %d 次重复%s）" % (a.id, n, "：该改成 gate 了" if n >= 2 else "")
    else:
        res = brain.purge(a.id, a.reason, a.approved_by)
    errors, warnings = brainlint.check(brain, base=before, purge_approvers=cfg.get("purge_approvers") or [])
    _print_lint(errors, warnings)
    if errors:
        print("brain entry: 没有写入（上面的错误要先改）")
        return 1
    written = brain.save()
    print("%s %s：写入 %s" % (a.op, res, ", ".join(os.path.join(cfg["brain_dir"], w) for w in written)))
    if a.op == "purge":
        print("git 历史里还有原文：请安全或数据负责人按流程改写历史（例如 git filter-repo），再强制推送受保护分支。")
    return 0


# ---------- reflect / usage ----------

def _fetch_brain_refs(repo, cfg):
    if not _has_origin(repo):
        return cfg["default_branch"]
    if git(repo, "fetch", "-q", "origin", cfg["default_branch"], check=False).returncode != 0:
        print("WARN cannot fetch origin/%s (offline, or your sign-in expired); using the last fetched copy"
              % cfg["default_branch"])
        return "origin/" + cfg["default_branch"]
    git(repo, "fetch", "-q", "--prune", "origin", "+refs/heads/brain/*:refs/remotes/origin/brain/*", check=False)
    return "origin/" + cfg["default_branch"]


def _reflect_key(repo, cfg, given):
    if given:
        return given
    b = parse_branch(git_out(repo, "rev-parse", "--abbrev-ref", "HEAD"), cfg["branch_prefix"])
    if not b:
        raise BrainError("not on a run branch: pass --key KEY (and --ref origin/%s after the run merged)"
                         % cfg["default_branch"])
    return b["key"]


def cmd_reflect(a):
    repo = repo_root()
    cfg = config.load(repo)
    store = a.vouchers or os.path.join(os.path.abspath(os.path.expanduser(
        os.environ.get("BRAIN_INTAKE_DIR") or git(repo, "config", "brain.intake", check=False).stdout.strip()
        or "~/.brain/intake")), "vouchers")
    if getattr(a, "draft", None):          # print the reflector session's instructions; changes nothing
        if a.apply or a.from_file:
            raise BrainError("--draft only prints the reflector's instructions (no --apply, no --from)")
        print(reflect.draft(repo, cfg, a.draft, ref=a.ref, vouchers_dir=store), end="")
        return 0
    key = _reflect_key(repo, cfg, a.key)
    base = _fetch_brain_refs(repo, cfg)
    mcfg = reflect.main_cfg(repo, base)
    text, label = reflect.source_text(repo, cfg, key, ref=a.ref, from_file=a.from_file)
    proposals, probs = reflect.parse(text)
    gaps, more = reflect.parse_gaps(text)
    probs += more
    if not proposals and not gaps and not probs:
        print("%s 里没有 brain-proposal 块：这个 run 没有提出 Brain 条目。" % label)
        return 0
    brain = entries.Brain.from_git(repo, base, mcfg["brain_dir"])
    used, pending = reflect.open_branches(repo, mcfg["brain_dir"])
    ops, more, notes = reflect.plan(brain, proposals, key, pending=pending, used_extra=used)
    probs += more
    rows, more = reflect.plan_gaps(repo, cfg, gaps, key)
    probs += more
    lost = reflect.rejected_without_correction(store, key, proposals)
    print("brain reflect %s：%s 里有 %d 个提案%s，对照 %s" % (key, label, len(proposals),
                                                      "、%d 个缺口" % len(gaps) if gaps else "", base))
    for op in ops:
        print("  %-9s %s %s" % (op.kind, op.kw.get("file") or op.kw.get("eid"), op.kw.get("type", "")))
    for line in reflect.gap_lines(rows, cfg["tracker"]):
        print(line)
    for n in notes:
        print("  NOTE", n)
    for w in lost:
        print("  WARN", w)
    for p in probs:
        print("  FAIL", p)
    if probs:
        return 1
    fresh = reflect.new_gaps(rows) if cfg["tracker"] == "none" else []

    def file_gaps():
        reflect.file_gaps(repo, cfg, rows, key, _me(repo, cfg))
        for line in reflect.gap_lines(fresh, cfg["tracker"]):
            print(line)

    if not ops or not reflect.substantive(ops):
        if proposals:
            print("这些提案都已写进 Brain，没有新东西：不开 MR。" if not ops else "只有补出处（same），没有新知识：不开 MR。")
        if fresh and a.apply:
            file_gaps()
        elif fresh:
            print("这是预览。加 --apply 登记这 %d 个缺口（每个成为一个新需求）。" % len(fresh))
        return 0
    if not a.apply:
        print("这是预览。加 --apply 在 main 上开新分支写入（不动你当前的工作区）%s。"
              % ("，并登记 %d 个缺口" % len(fresh) if fresh else ""))
        return 0
    if fresh:
        file_gaps()
    br = reflect.branch_name(repo, key)
    wt = os.path.abspath(a.worktree) if a.worktree else os.path.join(reflect.worktree_root(repo),
                                                                     br.replace("/", "-"))
    if os.path.exists(wt):
        raise BrainError("%s already exists" % wt)
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    git(repo, "worktree", "add", "-q", "--no-track", "-b", br, wt, base)
    try:
        bd = os.path.join(wt, mcfg["brain_dir"])
        b2 = entries.Brain.load(bd)
        reflect.apply(b2, ops, key, used_extra=used)
        b2.save()
        errors, warnings = brainlint.check(bd, base=entries.Brain.from_git(repo, base, mcfg["brain_dir"]),
                                           purge_approvers=mcfg.get("purge_approvers") or [])
        if errors:
            raise BrainError("brain lint refused the result:\n  " + "\n  ".join(errors))
        desc = reflect.mr_description(key, ops, warnings + lost, label, gaps=rows)
        title = "Brain: %s reflect（%d 条）" % (key, sum(1 for op in ops if op.kind != "same"))
        with open(wt + ".mr.md", "w", encoding="utf-8") as f:
            f.write("%s\n\n%s" % (title, desc))
        if not a.no_commit:
            git(wt, "add", "-A", "--", mcfg["brain_dir"])
            git(wt, "commit", "-q", "-m", "%s\n\n%s" % (title, desc))
    except BaseException:
        git(repo, "worktree", "remove", "--force", wt, check=False)
        git(repo, "branch", "-D", br, check=False)
        if os.path.exists(wt + ".mr.md"):
            os.remove(wt + ".mr.md")
        raise
    for op in ops:
        print("  %s %s%s" % (op.kind, op.result, "（%s）" % op.note if op.note else ""))
    if a.no_commit:
        print("写在 %s（分支 %s），没有提交。MR 描述：%s.mr.md" % (wt, br, wt))
        return 0
    if not a.push:
        print("已提交到本地分支 %s（%s）。看过以后推送：git -C %s push -u origin %s" % (br, wt, wt, br))
        return 0
    git(wt, "push", "-q", "-u", "origin", br)
    gl = _gitlab(cfg)
    if gl and cfg["gitlab"]["project"]:
        mr = gl.create_mr(cfg["gitlab"]["project"], br, cfg["default_branch"], title, ["brain-reflect"],
                          description=desc)
        print("已推送并开 MR：%s" % (mr or {}).get("web_url", "?"))
    else:
        print("已推送 %s；GitLab 没配置 token，请手动开 MR（描述在 %s.mr.md）" % (br, wt))
    return 0


def cmd_usage(a):
    repo = repo_root()
    cfg = config.load(repo)
    data = usage.collect(repo, cfg, ref=a.ref)
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=sorted))
    else:
        print(usage.report(data, min_runs=a.min_runs))
    return 0


def _intake_args(s, required_key):
    s.add_argument("--classification", choices=("public", "internal", "confidential", "secret", "unsure"),
                   help="gate 2: your classification of the documents ('unsure' counts as confidential)")
    s.add_argument("--consent", action="store_true", help="gate 1: I am the author or have the author's consent")
    s.add_argument("--business", choices=("keep", "mask"), help="gate 3: amounts/companies/contract numbers")
    s.add_argument("--store", help="intake store (default $BRAIN_INTAKE_DIR, git config brain.intake, ~/.brain/intake)")
    s.add_argument("--exclude", action="append", help="leave out files matching this glob (repeatable)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="brain", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version="brain-kit " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("install")
    s.add_argument("repo")
    s.add_argument("--lean", help="path to an AI-DLC Lean checkout (default: brain-kit's vendor/aidlc-lean)")
    s.add_argument("--team")
    s.add_argument("--prefix", help="requirement key prefix, e.g. OMS")
    s.set_defaults(fn=cmd_install)
    s = sub.add_parser("doctor")
    s.add_argument("--offline", action="store_true", help="skip git fetch")
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("claim")
    s.add_argument("--key")
    s.add_argument("--title", default="")
    s.set_defaults(fn=cmd_claim)
    s = sub.add_parser("intake")
    s.add_argument("doc", nargs="+", help="files or folders (a Feishu/Lark export folder works)")
    s.add_argument("--key", required=True)
    s.add_argument("--dry-run", action="store_true", help="show the findings, write nothing")
    _intake_args(s, True)
    s.set_defaults(fn=cmd_intake)
    s = sub.add_parser("start")
    s.add_argument("slug")
    s.add_argument("--key", help="requirement key (tracker none: omit to claim the next free one)")
    s.add_argument("--title")
    s.add_argument("--doc", nargs="+", help="take these documents in now (runs the five gates)")
    s.add_argument("--package", help="attach an existing material package PKG-<KEY>-<n>")
    s.add_argument("--paths", help="modules you expect to change, comma separated")
    s.add_argument("--no-jira", action="store_true", help="tracker jira only: skip the claim check (dry run)")
    s.add_argument("--no-push", action="store_true")
    _intake_args(s, False)
    s.set_defaults(fn=cmd_start)
    s = sub.add_parser("handoff")
    s.add_argument("--no-push", action="store_true")
    s.add_argument("--no-label", action="store_true")
    s.add_argument("--store")
    s.set_defaults(fn=cmd_handoff)
    s = sub.add_parser("crew-agent")
    s.add_argument("--force", action="store_true", help="add Lean's hooks and absent fields; keep your settings")
    s.add_argument("--reset", action="store_true", help="take brain-kit's model, tools, resources and prompt too")
    s.set_defaults(fn=cmd_crew_agent)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("lint")
    s.add_argument("--base", help="compare with this ref (default: merge-base with main)")
    s.add_argument("--static", action="store_true", help="only the static rules, no relation rules")
    s.set_defaults(fn=cmd_lint)
    s = sub.add_parser("entry", help="edit Brain entries (never by hand)")
    es = s.add_subparsers(dest="op", required=True)

    def text_args(x):
        g = x.add_mutually_exclusive_group(required=True)
        g.add_argument("--text")
        g.add_argument("--text-file", help="file with the entry text ('-' for stdin)")

    x = es.add_parser("add")
    x.add_argument("file", help="PRODUCT | TECH | IMPROVEMENT | PROJECT")
    x.add_argument("type")
    x.add_argument("--key", help="requirement the knowledge comes from (default: from the branch)")
    x.add_argument("--src", required=True, help="sources, comma separated (D01#S003, V-..., RUN-..., D-3)")
    x.add_argument("--section", help="put it under this '## ' section")
    text_args(x)
    x = es.add_parser("replace")
    x.add_argument("id")
    x.add_argument("--key")
    x.add_argument("--src", required=True)
    x.add_argument("--type")
    text_args(x)
    x = es.add_parser("withdraw")
    x.add_argument("id")
    x.add_argument("--reason", required=True)
    x.add_argument("--src", required=True)
    for name in ("same", "repeat"):
        x = es.add_parser(name)
        x.add_argument("id")
        x.add_argument("--src", required=True)
    x = es.add_parser("adds-to")
    x.add_argument("id")
    x.add_argument("--src", required=True)
    text_args(x)
    x = es.add_parser("purge")
    x.add_argument("id")
    x.add_argument("--reason", required=True, choices=entries.PURGE_REASONS)
    x.add_argument("--approved-by", required=True, help="GitLab user of the security or data owner")
    x = es.add_parser("show")
    x.add_argument("id")
    s.set_defaults(fn=cmd_entry)
    s = sub.add_parser("reflect", help="turn a run's brain-proposal blocks into a Brain MR")
    s.add_argument("--key", help="requirement key (default: this run branch)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--ref", help="read run.json and decisions.md from this ref (e.g. origin/main after merge)")
    g.add_argument("--from", dest="from_file", help="read proposals from this file instead")
    s.add_argument("--apply", action="store_true", help="write them on a new branch brain/<KEY>-reflect-<n>")
    s.add_argument("--no-commit", action="store_true", help="with --apply: leave the changes uncommitted")
    s.add_argument("--push", action="store_true", help="with --apply: push and open the MR")
    s.add_argument("--worktree", help="where to put the new branch's worktree (default ~/.brain/worktrees/...)")
    s.add_argument("--vouchers", help="brain-intake vouchers dir (default <intake store>/vouchers)")
    s.set_defaults(fn=cmd_reflect)
    s = sub.add_parser("usage", help="which Brain entries runs use")
    s.add_argument("--ref", help="read from this ref instead of the working tree (e.g. origin/main)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--min-runs", type=int, default=3, help="loaded by this many runs and never cited = candidate")
    s.set_defaults(fn=cmd_usage)
    import importlib
    import pkgutil
    from . import commands
    for m in sorted(pkgutil.iter_modules(commands.__path__), key=lambda m: m.name):
        importlib.import_module("%s.%s" % (commands.__name__, m.name)).register(sub)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except BrainError as e:
        print("brain: %s" % e, file=sys.stderr)
        return 2
    except UnicodeDecodeError as e:
        print("brain: a file is not UTF-8 text: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
