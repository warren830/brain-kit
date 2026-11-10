"""Golden tasks and ablation (plan §F "golden task" / "消融"): fixed exam questions, run with and without parts of
the Brain, to see which knowledge is load-bearing.

A task is a directory `tasks/<id>/` with `task.json`:
  {"id": "G-1", "repo": "informational", "base_sha": "<40 hex>", "intent": "what to build or fix",
   "acceptance": ["AC-1 ..."], "hidden_tests": ["hidden.patch", "hidden/tests/test_x.py"],
   "test_command": ["python3", "-m", "unittest"], "must_use": ["T-PAY-1-2"],
   "oracle_answers": {"RELEASE": "approve", "re:退款.*上限": "B", "单号": "A"}}
hidden_tests are kept outside the snapshot: a .patch/.diff is applied in the work dir with `git apply`; any other
path lives under `hidden/` in the task dir and is copied to the same path without the `hidden/` prefix.

  snapshot()  `git archive` of base_sha into a fresh dir outside any git repository: no .git, .gitmodules or
              remotes, no credential / env files or key stores, no keys or secret-looking password lines (dev
              defaults such as POSTGRES_PASSWORD: postgres are masked), no symlink that resolves outside; ablation
              variants drop one Brain file (and its archive) and the copies of its entries (their proposal blocks in
              run documents, the entries of other Brain files and the lines citing their ids); instruction files
              stay the same in every variant. The manifest is returned, never written into the snapshot, so the
              agent cannot learn its variant.
  score()     apply the hidden tests, run the task's test command (scrubbed env), count passed / failed; a run in
              which no test ran is an error, not a 0.
  plan()      tasks x variants x repeats -> jobs.
  run()       runner(job) -> work dir (the KiroCrew app dispatches for real; tests use a fake), then score.
  summarize() per variant mean / stdev and whether a drop exceeds normal variation (RULE below).
Answers given by the oracle are marked eval_oracle: true wherever they are recorded: they never count for a real MR.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import statistics
import subprocess
import tarfile
import tempfile

from . import entries as E, reflect
from .common import BrainError, SHA_RE, git
from .intake import checks

VARIANTS = ("full", "no-PRODUCT", "no-TECH", "no-IMPROVEMENT")
ABLATES = {"full": None, "no-PRODUCT": "PRODUCT", "no-TECH": "TECH", "no-IMPROVEMENT": "IMPROVEMENT"}
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CREDENTIAL_NAMES = (".git-credentials", ".netrc", "_netrc", ".pypirc", ".pgpass", ".my.cnf", ".htpasswd",
                    ".dockercfg", ".vault-token", ".s3cfg", ".boto", ".terraformrc", "terraform.rc")
CREDENTIAL_PATHS = (".aws/credentials", ".docker/config.json", ".kube/config")     # in any folder
KEY_STORE_EXTS = (".p12", ".pfx", ".jks", ".keystore")      # binary: the text scan cannot look inside
NPM_AUTH_RE = re.compile(r"(?im)^[^#;\n]*(?:_authToken|_auth|_password)\s*=")
YARN_AUTH_RE = re.compile(r"(?im)^[^#\n]*\bnpmAuth(?:Token|Ident)\s*:")
# A credential assigned on its own line, env / shell / YAML style: an upper-case name ending in PASSWORD, SECRET,
# TOKEN, API_KEY ... (DB_PASSWORD=..., export X_TOKEN=..., SECRET_KEY: ..., PASSWORD = "..."). The value is a literal:
# unquoted right after '=' (or ': '), or quoted; never ${VAR}, $VAR, an expression, a call (os.environ[...]).
PASSWORD_LINE_RE = re.compile(
    r"(?m)^[ \t]*(?:export[ \t]+|set[ \t]+|-[ \t]+)?(?P<name>[A-Z0-9_]*(?:PASSWORD|PASSWD|_PASS|_PWD|SECRET|"
    r"SECRET_KEY|TOKEN|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIALS?))"
    r"(?:(?:=|:[ \t]+)(?![\"'])(?!(?:None|null|NULL|nil|true|false|True|False|[*xX]+)[ \t]*(?:#.*)?$)"
    r"(?P<u>[^\s\"'$#{}()<>\[\]`,;]{4,})[ \t]*(?:#.*)?$"
    r"|[ \t]*[=:][ \t]*(?:\"(?P<dq>[^\"$\n{]{4,})\"|'(?P<sq>[^'$\n{]{4,})'))")
# Source code and docs are left to the other rules: test fixtures and README examples (API_TOKEN: 'fake-...',
# export X_TOKEN=your-token) would refuse almost every repository.
PASSWORD_SKIP_EXTS = (".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go", ".java", ".kt", ".kts",
                      ".scala", ".rb", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".swift", ".m",
                      ".md", ".markdown", ".rst", ".adoc", ".txt", ".ipynb")
# What such a line holds that is no secret: a number for a limit (MAX_TOKEN: 4096), a header name (CSRF_TOKEN:
# X-CSRF-Token). A well-known development default (POSTGRES_PASSWORD: postgres in docker-compose, a CI service's
# runner-test, changeme, the service's own name) is masked in place and listed, not refused.
QUANTITY_PARTS = {"MAX", "MIN", "NUM", "COUNT", "LIMIT", "LEN", "LENGTH", "SIZE", "TTL", "EXPIRY", "EXPIRES",
                  "TIMEOUT", "AGE", "LIFETIME", "BUDGET"}
HEADER_NAME_RE = re.compile(r"^(?:X-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|[A-Z][a-z]+(?:-[A-Z][a-z]+)+)$")
DEV_DEFAULTS = set((
    "postgres postgresql mysql mariadb root admin administrator password passwd pass secret guest user test demo "
    "changeme changeit letmein welcome default redis mongo mongodb rabbitmq minio minioadmin elastic kibana keycloak "
    "airflow superset grafana jenkins sonar sonarqube oracle sa toor qwerty 123456 1234567 12345678 123456789 "
    "password1 password123 admin123 abc123 secret123 test123 pass123 root123 passw0rd p@ssw0rd").split())
DEV_PARTS = set(("test tests testing dev devel development local localhost ci example examples sample demo dummy "
                 "fake mock changeme placeholder your here xxx todo insecure unsafe default").split())
MASK_FORMAT = "masked-by-brain-%d"      # the same value gets the same placeholder in one snapshot
SCAN_MAX_BYTES = 2 * 1024 * 1024
ENV_DROP_PARTS = ("TOKEN", "SECRET", "PASSWORD", "KEY", "CREDENTIAL")
ENV_DROP_PREFIXES = ("GITLAB_", "AWS_", "BRAIN_", "JIRA_", "KIROCREW_", "GIT_CONFIG_", "GIT_ASKPASS", "SSH_AUTH_SOCK",
                     "SSH_ASKPASS")    # GIT_CONFIG_*: KEY_n / VALUE_n come in pairs (and may carry auth headers)
RULE = ("A drop of variant V on task T is real (exceeds normal variation) when both sides have at least 2 scored "
        "repeats, every V repeat scores strictly below every full repeat (no overlap), and the mean drop is at "
        "least min_drop. With 3 full and 3 ablated runs, complete separation happens by chance with probability "
        "1/C(6,3) = 5% (exact one-sided Mann-Whitney, U = 0); each comparison reports 1/C(n1+n2, n1). Fewer than 2 "
        "scored repeats on a side: not enough runs, never judged.")


# ---------- tasks ----------

def _rel_ok(p) -> bool:
    if not isinstance(p, str) or not p or os.path.isabs(p) or p.startswith(("/", "\\")):
        return False
    return ".." not in re.split(r"[\\/]+", p)


def load_task(task_dir) -> dict:
    """task.json, checked. All problems are listed in one BrainError."""
    task_dir = os.path.abspath(task_dir)
    path = os.path.join(task_dir, "task.json")
    try:
        with open(path, encoding="utf-8") as f:
            t = json.load(f)
    except OSError:
        raise BrainError("%s not found" % path)
    except ValueError as e:
        raise BrainError("%s is not valid JSON: %s" % (path, e))
    if not isinstance(t, dict):
        raise BrainError("%s is not a JSON object" % path)
    p = []
    tid = t.get("id")
    if not isinstance(tid, str) or not TASK_ID_RE.match(tid):
        p.append("id must be letters, digits, '.', '_' or '-' (max 64)")
    elif tid != os.path.basename(task_dir):
        p.append("id %r does not match the folder name %r (tasks/<id>/task.json)" % (tid, os.path.basename(task_dir)))
    if not SHA_RE.match(str(t.get("base_sha", ""))):
        p.append("base_sha must be a full 40-hex commit sha")
    if not isinstance(t.get("intent"), str) or not t["intent"].strip():
        p.append("intent is required")
    acs = t.get("acceptance")
    if not isinstance(acs, list) or not acs or not all(isinstance(a, str) and a.strip() for a in acs):
        p.append("acceptance must be a non-empty list of strings")
    hidden = t.get("hidden_tests")
    if not isinstance(hidden, list) or not hidden:
        p.append("hidden_tests must be a non-empty list of paths in the task folder")
    else:
        for h in hidden:
            if not _rel_ok(h):
                p.append("hidden_tests: %r must be a relative path without '..'" % (h,))
            elif h.endswith((".patch", ".diff")):
                if not os.path.isfile(os.path.join(task_dir, h)):
                    p.append("hidden_tests: patch %s not found in the task folder" % h)
            elif not h.replace("\\", "/").startswith("hidden/") or h.rstrip("/") == "hidden":
                p.append("hidden_tests: %s must be a .patch/.diff or a path under hidden/" % h)
            elif not os.path.exists(os.path.join(task_dir, h)):
                p.append("hidden_tests: %s not found in the task folder" % h)
    cmd = t.get("test_command")
    if isinstance(cmd, str):
        try:
            cmd = shlex.split(cmd)
        except ValueError as e:
            p.append("test_command: %s" % e)
            cmd = None
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) and c for c in cmd):
        p.append("test_command must be an argv list (or a string; it is split, never run by a shell)")
    mu = t.get("must_use", [])
    if not isinstance(mu, list) or not all(isinstance(i, str) and (E.NEW_ID_RE.match(i) or E.LEGACY_ID_RE.match(i))
                                           for i in mu):
        p.append("must_use must be a list of Brain entry ids (e.g. T-PAY-1-2)")
    oa = t.get("oracle_answers", {})
    if not isinstance(oa, dict) or not all(isinstance(k, str) and k and isinstance(v, str) and v
                                           for k, v in oa.items()):
        p.append("oracle_answers must map a match (lean kind, park kind, 're:<regex>' or text) to a choice")
    else:
        for k in oa:
            if k.startswith("re:"):
                try:
                    re.compile(k[3:])
                except re.error as e:
                    p.append("oracle_answers: bad regex %r: %s" % (k, e))
    if p:
        raise BrainError("task %s: %s" % (task_dir, "; ".join(p)))
    out = dict(t, test_command=cmd, must_use=list(mu), oracle_answers=dict(oa))
    out["_dir"] = task_dir
    return out


def load_tasks(root) -> list:
    """Every <root>/*/task.json (or <root>/tasks/*/task.json), sorted by id."""
    root = os.path.abspath(root)
    dirs = sorted(d for d in os.listdir(root) if os.path.isfile(os.path.join(root, d, "task.json"))) \
        if os.path.isdir(root) else []
    if not dirs and os.path.isdir(os.path.join(root, "tasks")):
        return load_tasks(os.path.join(root, "tasks"))
    if not dirs:
        raise BrainError("no tasks under %s (want tasks/<id>/task.json)" % root)
    tasks, probs = [], []
    for d in dirs:
        try:
            tasks.append(load_task(os.path.join(root, d)))
        except BrainError as e:
            probs.append(str(e))
    if probs:
        raise BrainError("\n".join(probs))
    return sorted(tasks, key=lambda t: t["id"])


def _task_map(tasks) -> dict:
    if tasks is None:
        return {}
    if isinstance(tasks, dict):
        return dict(tasks)
    return {t["id"]: t for t in tasks}


# ---------- snapshot ----------

def _inside(root, path) -> bool:
    r = os.path.realpath(root)
    p = os.path.realpath(path)
    return p == r or p.startswith(r + os.sep)


def _extract(tar_path, out_dir):
    with tarfile.open(tar_path, "r:") as tf:
        for m in tf.getmembers():
            name = m.name
            if not _rel_ok(name):
                raise BrainError("archive member %r is not a safe relative path" % name)
            dst = os.path.join(out_dir, *name.split("/"))
            if m.isdir():
                os.makedirs(dst, exist_ok=True)
                continue
            if m.islnk() or m.ischr() or m.isblk() or m.isfifo() or m.isdev():
                raise BrainError("archive member %s is a hard link or device" % name)
            if not (m.isfile() or m.issym()):
                continue                          # other header kinds (pax) carry no files
            parent = os.path.dirname(dst)
            os.makedirs(parent, exist_ok=True)
            if not _inside(out_dir, parent):
                raise BrainError("archive member %s would be written outside the snapshot" % name)
            if os.path.lexists(dst):
                raise BrainError("archive member %s comes twice" % name)
            if m.issym():
                target = m.linkname
                inner = os.path.normpath(os.path.join(os.path.dirname(name), target))
                if os.path.isabs(target) or inner == ".." or inner.startswith(".." + os.sep):
                    raise BrainError("symlink %s -> %s points outside the snapshot" % (name, target))
                os.symlink(target, dst)
                continue
            src = tf.extractfile(m)
            with open(dst, "wb") as f:
                shutil.copyfileobj(src, f)
            os.chmod(dst, 0o755 if m.mode & 0o111 else 0o644)


def _check_links(root):
    """Every symlink must resolve (through other links, as the file system does) to a place inside the snapshot.
    Checked on the finished tree: a link can look harmless until a later link or a removal changes what it
    points through (up -> . and esc -> up/.. escape although normpath("up/..") is ".")."""
    for d, dirs, files in os.walk(root):
        for name in sorted(dirs + files):
            p = os.path.join(d, name)
            if os.path.islink(p) and not _inside(root, p):
                raise BrainError("symlink %s -> %s points outside the snapshot" % (
                    os.path.relpath(p, root).replace(os.sep, "/"), os.readlink(p)))


def _walk_files(root):
    """Relative posix paths of files and symlinks under root, sorted."""
    out = []
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for name in dirs:
            if os.path.islink(os.path.join(d, name)):
                files.append(name)
        for name in files:
            out.append(os.path.relpath(os.path.join(d, name), root).replace(os.sep, "/"))
    return sorted(set(out))


def _remove(root, rel):
    p = os.path.join(root, *rel.split("/"))
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p)
    elif os.path.lexists(p):
        os.unlink(p)


def tree_digest(root) -> str:
    """sha256 over sorted (path, kind, content hash): the same tree gives the same digest on any machine."""
    h = hashlib.sha256()
    for rel in _walk_files(root):
        p = os.path.join(root, *rel.split("/"))
        if os.path.islink(p):
            line = "%s\tL\t%s\n" % (rel, os.readlink(p))
        else:
            with open(p, "rb") as f:
                c = hashlib.sha256(f.read()).hexdigest()
            line = "%s\t%s\t%s\n" % (rel, "x" if os.stat(p).st_mode & 0o111 else "f", c)
        h.update(line.encode("utf-8"))
    return h.hexdigest()


def _is_env_file(name) -> bool:
    """.env, .env.<x>, .env-<x>, .env_<x>, .envrc, .envrc.<x> (docs/eval.md: `.env*`)."""
    return name in (".env", ".envrc") or name.startswith((".env.", ".env-", ".env_", ".envrc."))


def _why_removed(root, rel, name) -> str:
    p = os.path.join(root, *rel.split("/"))
    if name == ".git":
        return "git metadata"
    if name == ".gitmodules":
        return "git metadata (submodule remotes)"     # remote URLs, maybe with a password in them
    if name in CREDENTIAL_NAMES or any(rel == c or rel.endswith("/" + c) for c in CREDENTIAL_PATHS):
        return "credential file"
    if _is_env_file(name):
        return "env file"
    if name.lower().endswith(KEY_STORE_EXTS):
        return "key store"
    for want, rx in ((".npmrc", NPM_AUTH_RE), (".yarnrc.yml", YARN_AUTH_RE)):
        if name == want and not os.path.islink(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                if rx.search(f.read()):
                    return "credential file (npm auth)"
    return ""


def _hygiene(root) -> list:
    """Remove git metadata, credential and env files, key stores; return [{path, why}]."""
    removed = []
    for d, dirs, files in os.walk(root):
        if ".git" in dirs:
            dirs.remove(".git")
            rel = os.path.relpath(os.path.join(d, ".git"), root).replace(os.sep, "/")
            _remove(root, rel)
            removed.append({"path": rel, "why": "git metadata"})
        for name in sorted(files):
            rel = os.path.relpath(os.path.join(d, name), root).replace(os.sep, "/")
            why = _why_removed(root, rel, name)
            if why:
                _remove(root, rel)
                removed.append({"path": rel, "why": why})
    return removed


def _submodules(repo, sha) -> list:
    """Paths of submodules (gitlinks) in the commit: git archive leaves them empty."""
    out = git(repo, "ls-tree", "-r", "-z", sha).stdout
    return sorted(e.split("\t", 1)[1] for e in out.split("\0") if e.startswith("160000 ") and "\t" in e)


# Where copies of a Brain entry live on: the run documents (aidlc-docs/<intent>/decisions.md, runs/<KEY>/...).
# Instruction files are never edited by an ablation: every variant runs with the same instructions.
RUN_DOC_DIRS = ("aidlc-docs",)
INSTRUCTION_DIRS = {".kiro", ".claude", ".cursor", ".github", ".amazonq", ".windsurf", ".continue", ".clinerules"}
INSTRUCTION_NAMES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md"}


def _instruction_file(rel) -> bool:
    parts = rel.split("/")
    return parts[-1] in INSTRUCTION_NAMES or any(p in INSTRUCTION_DIRS for p in parts[:-1])


def _scrub_ablated(root, brain_dir, fkey, ids, runs_dir="runs") -> list:
    """A Brain entry lives on outside its file: the run's brain-proposal block it came from and the decision rows
    that cite its id stay in aidlc-docs/<intent>/decisions.md (and a reflector's runs/<KEY>/reflect-proposals.md),
    and an entry in another Brain file may restate it. Drop the copies of the removed entries (`ids`, from the
    removed files) in Markdown:
      - in the Brain files that stay (brain/IMPROVEMENT.md, ...): every entry that restates one, whole (_brain_copies);
      - in run documents (aidlc-docs/**, <runs_dir>/**): the brain-proposal blocks for `fkey` (file: <fkey>, a rel
        target in it, or citing one of `ids`) and every other line that cites one of `ids`;
      - elsewhere: only the lines that cite one of the full ids (T-PAY-1-2).
    An id counts as a whole token and only when it is one of `ids` (T-PAY-88-1 in an example is not). A legacy id
    (T-1, also a trade date: T-1 日) counts only in run documents and Brain entries, and only written as a citation:
    `T-1`, (T-1), [T-1], per T-1 (or in an entry's src / rel). Instruction files (.kiro/**, AGENTS.md, ...) are never
    edited. Returns [{path, blocks, lines}], with "entries" (the ids dropped) for a Brain file."""
    def alt(xs):
        return "|".join(re.escape(i) for i in sorted(xs))
    full = [i for i in ids if E.NEW_ID_RE.match(i)]
    legacy = [i for i in ids if E.LEGACY_ID_RE.match(i)]
    full_re = re.compile(r"(?<![A-Za-z0-9-])(?:%s)(?![A-Za-z0-9-])" % alt(full)) if full else None
    legacy_re = re.compile(r"`(?:{0})`|[(\[（【,，、][ \t]*(?:{0})[ \t]*(?=[)\]）】,，、])"
                           r"|(?i:\b(?:per|see|cites?|cited)[ \t]+)(?:{0})(?![A-Za-z0-9-])".format(alt(legacy))) \
        if legacy else None
    run_dirs = tuple(d.strip("/") + "/" for d in RUN_DOC_DIRS + (runs_dir,) if d.strip("/"))

    def cites(line, run_doc):
        return bool(full_re and full_re.search(line)) or bool(run_doc and legacy_re and legacy_re.search(line))

    out = []
    for rel in _walk_files(root):
        p = os.path.join(root, *rel.split("/"))
        if not rel.lower().endswith(".md") or _instruction_file(rel) or os.path.islink(p) \
                or os.path.getsize(p) > SCAN_MAX_BYTES:
            continue
        in_brain = rel.startswith(brain_dir + "/")
        run_doc = not in_brain and rel.startswith(run_dirs)
        if not run_doc and not in_brain and not full_re:
            continue
        with open(p, "rb") as fh:
            raw = fh.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if in_brain:
            drop, gone = _brain_copies(text, set(ids), cites)
            if drop:
                with open(p, "w", encoding="utf-8", newline="") as fh:
                    fh.write("\n".join(_without(text.split("\n"), drop)))
                out.append({"path": rel, "blocks": 0, "lines": len(drop), "entries": gone})
            continue
        if not cites(text, run_doc) and not (run_doc and "brain-proposal" in text):
            continue
        lines = text.split("\n")
        drop, blocks = set(), 0
        for start, end, fields in (reflect.block_spans(text) if run_doc else ()):
            target = (fields.get("rel") or "").split()[1:2]
            try:
                fk = E.file_key(fields["file"]) if fields.get("file") else None
            except BrainError:
                fk = None
            if fk is None and target:
                fk = E.LETTER_FILE.get(target[0][:1])
            if fk == fkey or any(cites(l, run_doc) for l in lines[start:end]):
                drop.update(range(start, end))
                blocks += 1
        cited = [n for n, l in enumerate(lines) if n not in drop and cites(l, run_doc)]
        if not drop and not cited:
            continue
        drop.update(cited)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write("\n".join(l for n, l in enumerate(lines) if n not in drop))
        out.append({"path": rel, "blocks": blocks, "lines": len(cited)})
    return out


def _entry_ids(path) -> set:
    if not os.path.isfile(path) or os.path.islink(path):
        return set()
    with open(path, encoding="utf-8", errors="replace") as fh:
        return {m.group(1) for m in (E.HEADER_RE.match(l) for l in fh.read().split("\n")) if m}


def _brain_copies(text, ids, cites) -> tuple:
    """In a Brain file that stays in the snapshot, what restates a removed entry: (line numbers, entry ids). An entry
    goes whole (header and text: one line alone would leave half an entry) when it cites one of `ids`: a full id
    anywhere in it, a legacy one written as a citation or named in its src / rel. So does a line outside entries
    that cites a full id, as in any other Markdown file."""
    lines = text.split("\n")
    drop, gone, inside = set(), [], set()
    for e in E.BrainFile("x.md", text).entries():
        span = range(e.line - 1, e.line + len(e.body))
        inside.update(span)
        if set(e.src) & ids or set(e.rel[1].split()) & ids or any(cites(lines[n], True) for n in span):
            drop.update(span)
            gone.append(e.id)
    drop.update(n for n, l in enumerate(lines) if n not in inside and cites(l, False))
    return drop, gone


def _without(lines, drop) -> list:
    """`lines` without the numbers in `drop`; where lines were cut, a blank line right after another goes too, so
    the file shows no gap."""
    out, cut = [], False
    for n, l in enumerate(lines):
        if n in drop:
            cut = True
        elif cut and not l.strip() and (not out or not out[-1].strip()):
            continue
        else:
            out.append(l)
            cut = False
    return out


def _git_repo_above(path):
    """The git repository (work tree or git dir) that `path` lies in, or None. GIT_* settings are ignored (an agent
    in the snapshot would not have them either), except GIT_CEILING_DIRECTORIES: git's own way to say "do not look
    above here", for a temp dir that has to be inside a checkout (CI); the agent's run then needs the same setting,
    so snapshot() records it (_ceiling_note)."""
    d = os.path.realpath(path)
    while not os.path.isdir(d):
        d = os.path.dirname(d)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k == "GIT_CEILING_DIRECTORIES"}
    p = subprocess.run(["git", "-C", d, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True, env=env)
    if p.returncode != 0:
        return None
    top = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True, env=env)
    return (top.stdout.strip() if top.returncode == 0 and top.stdout.strip() else p.stdout.strip()) or d


def _credential_kind(name, value) -> str:
    """For a credential line's literal value: '' (no secret: a limit, a header name), 'dev default' or 'secret'."""
    if re.match(r"^[0-9]+(?:\.[0-9]+)?[a-z]{0,2}$", value) and set(name.split("_")) & QUANTITY_PARTS:
        return ""
    if HEADER_NAME_RE.match(value) and not re.search(r"PASS|PWD", name):
        return ""                                    # CSRF_TOKEN: X-CSRF-Token (a passphrase may look like one)
    low = value.lower()
    words = [w for w in re.split(r"[^a-z0-9]+", low) if w]
    if low in DEV_DEFAULTS or any(w in DEV_PARTS for w in words) or \
            low in {x.lower() for x in name.split("_") if len(x) >= 4}:
        return "dev default"                         # POSTGRES_PASSWORD: postgres, runner-test
    return "secret"


def _secrets(root, mask_secrets=False) -> tuple:
    """Scan the text files for private keys, cloud access keys, tokens and credential lines (not in code and docs).
    Development defaults are masked in place; secrets refuse the snapshot, or with mask_secrets (the explicit
    override) are masked too, and a file with a private key is dropped. Returns (refused [{path, rule}], masked
    [{path, line, name, why}], dropped [{path, why}]): paths, line numbers and names, never a value."""
    refused, masked, dropped, placeholder = [], [], [], {}
    for rel in _walk_files(root):
        p = os.path.join(root, *rel.split("/"))
        if os.path.islink(p) or os.path.getsize(p) > SCAN_MAX_BYTES:
            continue
        with open(p, "rb") as f:
            raw = f.read()
        if b"\0" in raw[:8192]:
            continue
        text = raw.decode("utf-8", errors="surrogateescape")       # written back byte for byte
        if checks.PRIVATE_KEY_RE.search(text):
            if mask_secrets:
                _remove(root, rel)
                dropped.append({"path": rel, "why": "private key (--mask-secrets)"})
                continue
            refused.append({"path": rel, "rule": "private key"})
        spans = []                                   # (start, end, name, why)
        for label, rx, group in (("cloud access key", checks.CLOUD_KEY_RE, 0), ("token", checks.TOKEN_RE, 1)):
            found = [m.span(group) for m in rx.finditer(text)]
            if found and not mask_secrets:
                refused.append({"path": rel, "rule": label})
            spans += [(a, b, label, "%s (--mask-secrets)" % label) for a, b in found if mask_secrets]
        if not rel.lower().endswith(PASSWORD_SKIP_EXTS):
            refuse = False
            for m in PASSWORD_LINE_RE.finditer(text):
                g = next(k for k in ("u", "dq", "sq") if m.group(k) is not None)
                kind = _credential_kind(m.group("name"), m.group(g))
                if kind == "secret" and not mask_secrets:
                    refuse = True
                elif kind:
                    spans.append(m.span(g) + (m.group("name"), kind if kind == "dev default"
                                              else "secret (--mask-secrets)"))
            if refuse:
                refused.append({"path": rel, "rule": "password assignment"})
        if not spans:
            continue
        out, pos = [], 0
        for a, b, name, why in sorted(spans):
            if a < pos:
                continue
            v = text[a:b]
            placeholder.setdefault(v, MASK_FORMAT % (len(placeholder) + 1))
            out += [text[pos:a], placeholder[v]]
            pos = b
            masked.append({"path": rel, "line": text.count("\n", 0, a) + 1, "name": name, "why": why})
        out.append(text[pos:])
        with open(p, "wb") as f:
            f.write("".join(out).encode("utf-8", errors="surrogateescape"))
    return refused, masked, dropped


def _ceiling_note(out_dir):
    """When GIT_CEILING_DIRECTORIES is what hides a repository above out_dir: {repo, GIT_CEILING_DIRECTORIES}."""
    ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
    if not ceiling:
        return None
    d = os.path.dirname(os.path.realpath(out_dir))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return {"repo": d, "GIT_CEILING_DIRECTORIES": ceiling}
        if os.path.dirname(d) == d:
            return None
        d = os.path.dirname(d)


def snapshot(repo, base_sha, out_dir, variant="full", brain_dir="brain", runs_dir="runs", mask_secrets=False) -> dict:
    """Extract base_sha into a NEW empty out_dir (no .git, no remotes, no credentials); for an ablation variant drop
    the Brain file, its archive and the proposal blocks and lines that carry its entries elsewhere (_scrub_ablated).
    Development defaults in credential lines are masked (manifest "masked"). Returns the manifest (not written into
    the snapshot). Refused, with nothing left behind: an out_dir inside a git repository (git would find that
    repository from the snapshot; GIT_CEILING_DIRECTORIES is honoured and then recorded as "git_ceiling"), a symlink
    that resolves outside, a key or a secret-looking credential (with mask_secrets, the explicit override: masked,
    a private-key file dropped, all listed, and the manifest says "override": "mask-secrets")."""
    if variant not in VARIANTS:
        raise BrainError("variant must be one of %s" % ", ".join(VARIANTS))
    p = git(repo, "rev-parse", "-q", "--verify", "%s^{commit}" % base_sha, check=False)
    if p.returncode != 0:
        raise BrainError("%s is not a commit in %s" % (base_sha, repo))
    sha = p.stdout.strip()
    out_dir = os.path.abspath(out_dir)
    if os.path.lexists(out_dir) and (not os.path.isdir(out_dir) or os.listdir(out_dir)):
        raise BrainError("%s exists and is not empty: a snapshot goes into a fresh directory" % out_dir)
    above = os.path.realpath(repo) if _inside(repo, out_dir) else _git_repo_above(out_dir)
    if above:
        raise BrainError("%s is inside the git work tree (or repository) %s: from the snapshot, git would find that "
                         "repository, its history and remotes. Put snapshots in a directory outside any git "
                         "repository (or, when it has to be there, set GIT_CEILING_DIRECTORIES to a directory between "
                         "the two, for this command and for the agent's run)." % (out_dir, above))
    ceiling = _ceiling_note(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="brain-golden-") as tmp:
            tar_path = os.path.join(tmp, "base.tar")
            git(repo, "archive", "--format=tar", "--output=" + tar_path, sha)
            _extract(tar_path, out_dir)
        _check_links(out_dir)                         # before anything is removed through a link
        removed = _hygiene(out_dir)
        bd = brain_dir.strip("/")
        f = ABLATES[variant]
        scrubbed = []
        if f:
            ids = set()
            for name in ("%s.md" % f, "%s-archive.md" % f):
                rel = "%s/%s" % (bd, name)
                ids |= _entry_ids(os.path.join(out_dir, *rel.split("/")))
                if os.path.lexists(os.path.join(out_dir, *rel.split("/"))):
                    _remove(out_dir, rel)
                    removed.append({"path": rel, "why": "ablation (%s)" % variant})
            scrubbed = _scrub_ablated(out_dir, bd, f, ids, runs_dir)
        _check_links(out_dir)                         # a removal can change what a link resolves through
        refused, masked, dropped = _secrets(out_dir, mask_secrets)
        if refused:
            raise BrainError("snapshot of %s refused: %s. A snapshot that leaks a key must not exist: pick another "
                             "base_sha or clean the tree first; if these are test fixtures or throwaway values, "
                             "`--mask-secrets` masks them (and drops private-key files), and the manifest records "
                             "that override." % (sha[:12], ", ".join("%s (%s)" % (h["path"], h["rule"])
                                                                    for h in refused)))
        removed += dropped
        bpath = os.path.join(out_dir, *bd.split("/"))
        brain_files = sorted(os.listdir(bpath)) if os.path.isdir(bpath) else []
        return {"base_sha": sha, "variant": variant, "removed": removed, "scrubbed": scrubbed, "masked": masked,
                "override": "mask-secrets" if mask_secrets else None, "git_ceiling": ceiling,
                "submodules": _submodules(repo, sha), "files": len(_walk_files(out_dir)),
                "brain_files": brain_files, "tree_sha256": tree_digest(out_dir)}
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise


# ---------- score ----------

def scrubbed_env(env=None) -> dict:
    """The test command's environment: no tokens, secrets, keys, ssh agent, or GitLab / cloud / bridge / git config
    settings."""
    out = {}
    for k, v in (os.environ if env is None else env).items():
        up = k.upper()
        if any(x in up for x in ENV_DROP_PARTS) or up.startswith(ENV_DROP_PREFIXES):
            continue
        out[k] = v
    return out


def apply_hidden(workdir, task):
    """Put the task's hidden tests into the work dir. A patch that does not apply raises BrainError."""
    tdir = task["_dir"]
    env = dict(scrubbed_env(), GIT_CEILING_DIRECTORIES=os.path.dirname(os.path.realpath(workdir)))
    for h in task["hidden_tests"]:
        if not _rel_ok(h):
            raise BrainError("hidden test path %r is not safe" % (h,))
        src = os.path.join(tdir, h)
        if h.endswith((".patch", ".diff")):
            # GIT_CEILING_DIRECTORIES: even inside another repository, git apply works on the work dir as plain files
            p = subprocess.run(["git", "apply", "--whitespace=nowarn", os.path.abspath(src)], cwd=workdir,
                               capture_output=True, text=True, env=env)
            if p.returncode != 0:
                raise BrainError("hidden patch %s does not apply: %s" % (h, " ".join((p.stderr or "").split())[:300]))
            continue
        rel = h.replace("\\", "/")[len("hidden/"):].strip("/")
        dst = os.path.join(workdir, *rel.split("/"))
        if not _inside(workdir, os.path.dirname(dst)) or os.path.islink(dst):
            raise BrainError("hidden test %s would be written outside the work dir" % h)
        if os.path.isdir(src):
            for d, _, files in os.walk(src):
                for name in files:
                    s = os.path.join(d, name)
                    t = os.path.join(dst, os.path.relpath(s, src))
                    os.makedirs(os.path.dirname(t), exist_ok=True)
                    if not _inside(workdir, os.path.dirname(t)) or os.path.islink(t):
                        raise BrainError("hidden test %s would be written outside the work dir" % h)
                    shutil.copyfile(s, t)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not _inside(workdir, os.path.dirname(dst)):
                raise BrainError("hidden test %s would be written outside the work dir" % h)
            shutil.copyfile(src, dst)


UNITTEST_RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.M)
UNITTEST_END_RE = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
PYTEST_NONE_RE = re.compile(r"\bno tests ran\b|^=*\s*(?:[0-9]+ (?:skipped|deselected|xfailed|xpassed|warnings?)"
                            r"(?:, )?)+ in [0-9.]+s\b")
PYTEST_ITEM_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)\b")


def parse_counts(out, exit_code) -> tuple:
    """(passed, failed, parser) from unittest or pytest output; else from the exit code. (0, 0, parser) when the
    output says no test ran (none found, or all skipped): score() turns that into an error, never a 0."""
    ran = UNITTEST_RAN_RE.findall(out)
    if ran:
        total = int(ran[-1])
        if total == 0:
            return 0, 0, "unittest"          # "Ran 0 tests": OK on Python <= 3.11, NO TESTS RAN (exit 5) after
        ends = UNITTEST_END_RE.findall(out)
        counts = {}
        if ends:
            for part in (ends[-1][1] or "").split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    if v.strip().isdigit():
                        counts[k.strip()] = int(v.strip())
        failed = counts.get("failures", 0) + counts.get("errors", 0) + counts.get("unexpected successes", 0)
        if not ends and exit_code != 0:
            failed = max(failed, 1)
        passed = max(total - failed - counts.get("skipped", 0), 0)
        return passed, failed, "unittest"
    for line in reversed(out.splitlines()):
        if " in " in line and re.search(r"\b\d+ (passed|failed|errors?)\b", line):
            c = {}
            for n, word in PYTEST_ITEM_RE.findall(line):
                word = "error" if word.startswith("error") else word
                c[word] = c.get(word, 0) + int(n)
            return c.get("passed", 0), c.get("failed", 0) + c.get("error", 0), "pytest"
        if PYTEST_NONE_RE.search(line):
            return 0, 0, "pytest"                # "no tests ran in 0.01s", "3 skipped in 0.01s"
    return (1, 0, "exit") if exit_code == 0 else (0, 1, "exit")


def score(workdir, task, timeout=600) -> dict:
    """Apply the hidden tests and run the task's test command; pass/fail counts. Output bodies are not kept,
    only their sha256."""
    if not os.path.isdir(workdir):
        raise BrainError("work dir %s does not exist" % workdir)
    apply_hidden(workdir, task)
    cmd = task["test_command"] if isinstance(task["test_command"], list) else shlex.split(task["test_command"])
    try:
        p = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout, env=scrubbed_env())
        code, out = p.returncode, (p.stdout or "") + "\n" + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        def text(b):
            return b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")
        code, out = 124, text(e.stdout) + "\n" + text(e.stderr)
    except OSError as e:
        raise BrainError("test command %s cannot run: %s" % (cmd[0], e))
    passed, failed, how = parse_counts(out, code)
    total = passed + failed
    if not total:
        raise BrainError("no test ran (%s output, exit %d): no pass or fail to count, so no score. Check "
                         "test_command and hidden_tests (unittest discovery needs an __init__.py in tests/; all "
                         "skipped counts as none)" % (how, code))
    return {"task": task["id"], "passed": passed, "failed": failed, "exit": code, "parsed": how,
            "score": round(passed / total, 6), "ok": code == 0 and failed == 0,
            "output_sha256": hashlib.sha256(out.encode("utf-8", "replace")).hexdigest()}


# ---------- plan / oracle / run ----------

def plan(tasks, variants=VARIANTS, repeats=3) -> list:
    """Jobs, repeat-major per task so a cut-short batch still covers every variant."""
    variants = list(variants)
    bad = [v for v in variants if v not in VARIANTS]
    if bad or not variants or len(set(variants)) != len(variants):
        raise BrainError("variants must be distinct values from %s (got %s)" % (", ".join(VARIANTS),
                                                                                ", ".join(variants) or "none"))
    if not isinstance(repeats, int) or repeats < 1:
        raise BrainError("repeats must be 1 or more")
    jobs = []
    for t in sorted(_task_map(tasks).values(), key=lambda t: t["id"]):
        for n in range(1, repeats + 1):
            for v in variants:
                jobs.append({"job_id": "%s/%s/%d" % (t["id"], v, n), "task": t["id"], "variant": v, "repeat": n,
                             "base_sha": t["base_sha"], "eval_oracle": True})
    return jobs


def oracle_answer(task, park):
    """The oracle's answer to a park.json question, or None (a real person would have been needed).
    Order: a key equal to the Lean request kind (RELEASE, DESIGN, ...) or the park kind (acceptance, ...); then
    're:<regex>' keys searched in the question; then plain text keys found in the question."""
    answers = task.get("oracle_answers") or {}
    lr = park.get("lean_request") if isinstance(park, dict) else None
    lean_kind = str(lr).split(" ", 1)[0] if lr else None
    question = str((park or {}).get("question") or "")
    hit = None
    for k in (lean_kind, (park or {}).get("kind")):
        if k and k in answers:
            hit = k
            break
    if hit is None:
        for k in sorted(x for x in answers if x.startswith("re:")):
            if re.search(k[3:], question):
                hit = k
                break
    if hit is None:
        for k in sorted(x for x in answers if not x.startswith("re:")):
            if k in question:
                hit = k
                break
    if hit is None:
        return None
    # what the bridge takes (bridge/decisions.py check_answer): approve / reject (any letter case) for a Lean request
    # or a question without options, else one of the options (the letter upper-cased)
    choice = answers[hit]
    choice = choice.lower() if choice.lower() in ("approve", "reject") else choice.upper()
    options = {o.get("id") for o in ((park or {}).get("options") or []) if isinstance(o, dict)}
    allowed = {"approve", "reject"} if lr or not options else options
    if choice not in allowed:
        raise BrainError("task %s: oracle answer %r (for %r) is not one of %s" % (
            task.get("id"), answers[hit], hit, ", ".join(sorted(str(x) for x in allowed))))
    return {"choice": choice, "matched": hit, "task": task.get("id"), "eval_oracle": True}


def _one_line(e) -> str:
    return " ".join(("%s: %s" % (type(e).__name__, e)).split())[:300]


def run(jobs, runner, tasks, score_fn=score) -> list:
    """For each job: runner(job) -> work dir (or {"workdir", "oracle_answers", "credits"}), then score it.
    A runner or scoring failure is recorded with score None (counted apart, never as 0)."""
    tmap = _task_map(tasks)
    results = []
    for job in jobs:
        rec = dict(job, eval_oracle=True, oracle_answers=[], credits=None)
        try:
            task = tmap.get(job["task"])
            if task is None:
                raise BrainError("job %s: task %s not given" % (job.get("job_id"), job.get("task")))
            out = runner(dict(job))
            if isinstance(out, dict):
                workdir = out.get("workdir")
                rec["oracle_answers"] = [dict(a, eval_oracle=True) if isinstance(a, dict)
                                         else {"value": a, "eval_oracle": True}
                                         for a in (out.get("oracle_answers") or [])]
                c = out.get("credits")
                rec["credits"] = c if isinstance(c, (int, float)) and not isinstance(c, bool) else None
            else:
                workdir = out
            if not isinstance(workdir, str) or not os.path.isdir(workdir):
                raise BrainError("runner returned no work dir for %s" % job.get("job_id"))
            res = score_fn(workdir, task)
            rec.update({k: res[k] for k in ("passed", "failed", "exit", "parsed", "score", "ok") if k in res})
            rec["error"] = None
        except Exception as e:  # noqa: BLE001 - one failed job must not stop the batch
            rec.update(score=None, ok=False, error=_one_line(e))
        results.append(rec)
    return results


# ---------- summarize ----------

def _stats(xs) -> dict:
    return {"n": len(xs), "mean": round(statistics.mean(xs), 6) if xs else None,
            "stdev": round(statistics.stdev(xs), 6) if len(xs) >= 2 else None,
            "min": min(xs) if xs else None, "max": max(xs) if xs else None}


def summarize(results, tasks=None, min_drop=0.05) -> dict:
    """Per (task, variant) and per variant; real drops by RULE; load-bearing files; withdraw candidates."""
    tmap = _task_map(tasks)
    scores, errors = {}, {}
    oracle_used = 0
    for r in results:
        key = (r.get("task"), r.get("variant"))
        scores.setdefault(key, [])
        oracle_used += len(r.get("oracle_answers") or [])
        if r.get("score") is None:
            errors[key] = errors.get(key, 0) + 1
        else:
            scores[key].append(float(r["score"]))
    task_ids = sorted({t for t, _ in scores})
    variants = [v for v in VARIANTS if any(k[1] == v for k in scores)]
    per_task = {}
    for t in task_ids:
        per_task[t] = {}
        for v in variants:
            if (t, v) in scores:
                per_task[t][v] = dict(_stats(scores[(t, v)]), errors=errors.get((t, v), 0))
    comparisons = []
    for t in task_ids:
        full = scores.get((t, "full"), [])
        for v in variants:
            if v == "full" or (t, v) not in scores:
                continue
            ab = scores[(t, v)]
            row = {"task": t, "variant": v, "file": ABLATES[v], "n_full": len(full), "n_variant": len(ab),
                   "mean_full": round(statistics.mean(full), 6) if full else None,
                   "mean_variant": round(statistics.mean(ab), 6) if ab else None,
                   "drop": None, "separated": None, "chance": None}
            if not full:
                row["verdict"] = "no full baseline"
            elif len(full) < 2 or len(ab) < 2:
                row["verdict"] = "not enough runs"
            else:
                # Scores are stored with 6 decimals; the mean drop is compared at that precision, so binary float
                # error cannot flip the boundary (0.3 - 0.25 = 0.04999999999999999 is a drop of 0.05 >= 0.05).
                drop = round(statistics.mean(full) - statistics.mean(ab), 6)
                sep = max(ab) < min(full)
                row.update(drop=drop, separated=sep,
                           chance=round(1.0 / math.comb(len(full) + len(ab), len(full)), 6))
                row["verdict"] = "drop" if sep and drop >= round(min_drop, 6) else "no drop"
            comparisons.append(row)
    by_variant = {}
    for v in variants:
        means = [per_task[t][v]["mean"] for t in task_ids if v in per_task[t] and per_task[t][v]["mean"] is not None]
        rows = [c for c in comparisons if c["variant"] == v]
        by_variant[v] = {"tasks": len(means), "mean": round(statistics.mean(means), 6) if means else None,
                         "judged": sum(1 for c in rows if c["verdict"] in ("drop", "no drop")),
                         "real_drops": sum(1 for c in rows if c["verdict"] == "drop"),
                         "errors": sum(n for (t, vv), n in errors.items() if vv == v)}
    load_bearing = [{"task": c["task"], "variant": c["variant"], "file": c["file"]}
                    for c in comparisons if c["verdict"] == "drop"]
    files = {}
    for v in variants:
        if v == "full":
            continue
        b = by_variant[v]
        files[ABLATES[v]] = "load-bearing" if b["real_drops"] else ("never dropped" if b["judged"] else "not judged")
    verdict = {(c["task"], c["variant"]): c["verdict"] for c in comparisons}
    must = {}
    for tid, t in sorted(tmap.items()):
        for eid in t.get("must_use") or []:
            must.setdefault(eid, []).append(tid)
    withdraw, not_judged = [], []
    for eid, tids in sorted(must.items()):
        f = E.LETTER_FILE.get(eid[:1])
        v = "no-%s" % f if f else None
        if v not in VARIANTS or v not in variants:
            continue
        vs = [verdict.get((t, v)) for t in tids]
        if all(x in ("drop", "no drop") for x in vs):
            if "drop" not in vs:
                withdraw.append({"id": eid, "file": f, "variant": v, "tasks": tids})
        else:
            not_judged.append({"id": eid, "file": f, "variant": v, "tasks": tids})
    return {"rule": RULE, "min_drop": min_drop, "eval_oracle": True, "oracle_answers_used": oracle_used,
            "runs": len(results), "errors": sum(errors.values()), "tasks": task_ids, "variants": variants,
            "per_task": per_task, "by_variant": by_variant, "comparisons": comparisons,
            "load_bearing": load_bearing, "files": files, "withdraw_candidates": withdraw,
            "must_use_not_judged": not_judged}


def _f(x) -> str:
    return "-" if x is None else "%.2f" % x


def report(s) -> str:
    out = ["Golden 考题和消融：%d 个 run，%d 道题，出错 %d 个（出错的不计分，也不算 0 分）" % (
        s["runs"], len(s["tasks"]), s["errors"])]
    out.append("判定：去掉某块知识后，每一次都比 full 的每一次低（没有重叠），平均掉分至少 %.2f，才算真掉分。"
               % s["min_drop"])
    out.append("  为什么：3 次对 3 次时，没掉分却碰巧完全分开的概率是 1/20（5%）。两边不到 2 次不判断。")
    if s["by_variant"]:
        out.append("")
        out.append("各组：")
        for v, b in s["by_variant"].items():
            extra = "" if v == "full" else "，真掉分 %d 道 / 判定 %d 道" % (b["real_drops"], b["judged"])
            out.append("  %s：平均 %s（%d 道题）%s，出错 %d" % (v, _f(b["mean"]), b["tasks"], extra, b["errors"]))
    if s["comparisons"]:
        out.append("")
        out.append("每道题：")
        for c in s["comparisons"]:
            head = "  %s %s：" % (c["task"], c["variant"])
            if c["verdict"] == "no full baseline":
                out.append(head + "没有 full 的成绩，没法比")
            elif c["verdict"] == "not enough runs":
                out.append(head + "次数不够（full %d 次，消融 %d 次），先不判断" % (c["n_full"], c["n_variant"]))
            elif c["verdict"] == "drop":
                out.append(head + "%s → %s，掉 %s，每次都低于 full（碰巧的概率 %.0f%%）：这块知识在承重" % (
                    _f(c["mean_full"]), _f(c["mean_variant"]), _f(c["drop"]), 100 * c["chance"]))
            else:
                why = "有重叠" if not c["separated"] else "掉得太少"
                out.append(head + "%s → %s，%s，不算掉分" % (_f(c["mean_full"]), _f(c["mean_variant"]), why))
    if s["files"]:
        out.append("")
        label = {"load-bearing": "在承重", "never dropped": "从没掉过分", "not judged": "还没法判断"}
        out.append("各文件：" + "；".join("%s %s" % (f, label[st]) for f, st in sorted(s["files"].items())))
    if s["withdraw_candidates"]:
        out.append("")
        out.append("撤回候选（考题写明要用，去掉所在文件后从不掉分）：")
        out += ["  %s（%s，题目 %s）" % (w["id"], w["file"], ", ".join(w["tasks"])) for w in s["withdraw_candidates"]]
    if s["must_use_not_judged"]:
        out.append("还没法判断的必用条目：" + ", ".join(w["id"] for w in s["must_use_not_judged"]))
    out.append("")
    out.append("自动答题（oracle）%d 次，都标了 eval_oracle，不算真实 MR 的回答。" % s["oracle_answers_used"])
    return "\n".join(out)
