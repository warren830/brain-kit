"""Team kit config: `.brain/kit.json` in the project repo (people edit it; the bot may not)."""
from __future__ import annotations

import os
import re

from .common import USER_RE, BrainError, load_json

KIT_CONFIG = ".brain/kit.json"

DEFAULTS = {
    "version": 1,
    "team": "",
    "branch_prefix": "ai",
    "default_branch": "main",
    "brain_dir": "brain",
    "brain_files": ["PRODUCT.md", "TECH.md", "IMPROVEMENT.md", "PROJECT.md"],
    "runs_dir": "runs",
    "handoff_label": "ai-handoff",
    "running_label": "ai-running",
    "rejected_label": "ai-handoff-rejected",
    "bot_users": ["brain-bot"],
    "bot_emails": ["brain-bot@noreply.invalid"],
    # the GitLab account the bridge posts as (its questions, replies): lets anyone tell its notes from people's.
    # Empty: the bridge goes by the note ids it recorded; an outside checker (a separate signer) cannot.
    "bridge_user": "",
    # what bot commits may not change (pre_receive.KIT_PATHS, the kit's own files, are refused whatever this says)
    "bot_forbidden_paths": ["gates/", "project-agent.yaml", ".brain/", "CODEOWNERS", ".gitlab/", ".gitlab/CODEOWNERS",
                            "docs/CODEOWNERS", ".gitlab-ci.yml", "ci/", ".kiro/brain/", ".kiro/agents/",
                            ".kiro/hooks/", ".kiro/aidlc-lean/", ".kiro/steering/", ".kiro/skills/"],
    "claim_statuses": ["In Progress", "进行中"],
    "model": "claude-opus-5.5",
    "review_model": "kimi-k3",
    "allowed_models": ["claude-opus-5.5", "kimi-k3"],
    "forbidden_tools": ["search_chat_history", "get_chat_session", "list_sessions"],
    "pins": {"kiro-cli": "", "kirocrew": "", "lean": "0.3"},
    "jira": {"base": "", "api": "2"},
    "gitlab": {"base": "", "project": ""},
    # tracker "none": requirement keys <req_prefix>-<n> and claims are git branches claim/<KEY> (no Jira needed)
    "tracker": "none",
    "req_prefix": "REQ",
    "risk_paths_file": "gates/risk-paths",
    # GitLab users (security / data owners) who may approve a Brain purge; empty: purges are refused
    "purge_approvers": [],
    # who may answer each kind of decision (owner = the run's owner; other names are roles in brain-authz)
    "decision_roles": {"clarify": ["owner", "product"], "ideation": ["product"], "requirements": ["product"], "design": ["builder"],
                       "plan": ["builder"],
                       "acceptance": ["product"], "action": ["security"], "other": ["owner"]},
    "intake": {
        "allow_confidential": True,       # false: gate 2 refuses material classified or marked confidential
        "bulk_pii_limit": 30,             # more personal-data hits than this in one file: refuse the file
        "segment_chars": 6000,
        "max_file_mb": 50,
        "max_total_mb": 200,
    },
}

TRACKERS = ("none", "jira")


def merged(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = merged(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load(repo: str) -> dict:
    path = os.path.join(repo, KIT_CONFIG)
    if not os.path.exists(path):
        raise BrainError("%s is missing: run `brain install` first" % KIT_CONFIG)
    try:
        cfg = merged(DEFAULTS, load_json(path))
    except ValueError as e:
        raise BrainError("%s is not valid JSON: %s" % (KIT_CONFIG, e))
    problems = validate(cfg)
    if problems:
        raise BrainError("%s: %s" % (KIT_CONFIG, "; ".join(problems)))
    return cfg


def validate(cfg: dict) -> list:
    p = []
    if cfg.get("version") != 1:
        p.append("version must be 1")
    if cfg.get("model") not in cfg.get("allowed_models", []):
        p.append("model %r is not in allowed_models" % cfg.get("model"))
    if cfg.get("review_model") not in cfg.get("allowed_models", []):
        p.append("review_model %r is not in allowed_models" % cfg.get("review_model"))
    if "auto" in cfg.get("allowed_models", []):
        p.append("allowed_models may not contain auto")
    if not cfg.get("bot_users"):
        p.append("bot_users is empty")
    bu = cfg.get("bridge_user")
    if bu and not (isinstance(bu, str) and USER_RE.match(bu.lower())):
        p.append("bridge_user must be a GitLab username")
    elif bu and bu.lower() in [str(u).lower() for u in cfg.get("bot_users") or []]:
        p.append("bridge_user must not be one of bot_users (the runner must not be able to post the bridge's "
                 "questions)")
    if not str(cfg.get("branch_prefix", "")).isalpha():
        p.append("branch_prefix must be letters only")
    if cfg.get("tracker") not in TRACKERS:
        p.append("tracker must be one of %s" % (TRACKERS,))
    if not re.match(r"^[A-Z][A-Z0-9]{0,9}$", str(cfg.get("req_prefix", ""))):
        p.append("req_prefix must be like OMS (capital letters/digits, max 10)")
    if cfg.get("tracker") == "jira" and not cfg.get("jira", {}).get("base"):
        p.append("tracker is jira but jira.base is empty")
    return p
