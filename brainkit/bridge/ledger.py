"""Bridge ledger (sqlite). Every state change is compare-and-set, so a decision takes effect once."""
from __future__ import annotations

import json
import os
import sqlite3

from ..common import iso, utcnow

OPEN_RUN_STATES = ("spooled", "ready", "dispatched", "send_unknown")

SCHEMA = """
CREATE TABLE IF NOT EXISTS handoffs (
  project TEXT NOT NULL, iid INTEGER NOT NULL, sha TEXT NOT NULL,
  state TEXT NOT NULL, run_id TEXT, voucher_id TEXT, reason TEXT,
  created TEXT NOT NULL, updated TEXT NOT NULL,
  PRIMARY KEY (project, iid, sha));
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, project TEXT NOT NULL, req TEXT NOT NULL, iid INTEGER NOT NULL,
  sha TEXT NOT NULL, voucher_id TEXT NOT NULL, state TEXT NOT NULL, slot TEXT, path TEXT, paths TEXT,
  owner TEXT, voucher TEXT, last_sha TEXT,
  created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS runs_req ON runs(req);
"""


class Ledger:
    def __init__(self, path):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # handoffs: one row per (MR, head sha)
    def claim_handoff(self, project, iid, sha) -> bool:
        """Insert as 'checking'; False if this MR+sha was already seen."""
        now = iso(utcnow())
        cur = self.db.execute("INSERT OR IGNORE INTO handoffs(project,iid,sha,state,created,updated) "
                              "VALUES (?,?,?,?,?,?)", (project, iid, sha, "checking", now, now))
        return cur.rowcount == 1

    def release_handoff(self, project, iid, sha):
        self.db.execute("DELETE FROM handoffs WHERE project=? AND iid=? AND sha=? AND state='checking'",
                        (project, iid, sha))

    def set_handoff(self, project, iid, sha, frm, to, **fields) -> bool:
        sets = ", ".join(["state=?", "updated=?"] + ["%s=?" % k for k in fields])
        args = [to, iso(utcnow())] + list(fields.values()) + [project, iid, sha, frm]
        cur = self.db.execute("UPDATE handoffs SET %s WHERE project=? AND iid=? AND sha=? AND state=?" % sets, args)
        return cur.rowcount == 1

    def handoff(self, project, iid, sha):
        return self.db.execute("SELECT * FROM handoffs WHERE project=? AND iid=? AND sha=?",
                               (project, iid, sha)).fetchone()

    # runs
    def next_run_id(self, req) -> str:
        n = self.db.execute("SELECT COUNT(*) FROM runs WHERE req=?", (req,)).fetchone()[0]
        return "RUN-%s-%d" % (req, n + 1)

    def open_run_for(self, req):
        q = "SELECT * FROM runs WHERE req=? AND state IN (%s)" % ",".join("?" * len(OPEN_RUN_STATES))
        return self.db.execute(q, (req,) + OPEN_RUN_STATES).fetchone()

    def add_run(self, run_id, project, req, iid, sha, voucher_id, paths=(), owner="", voucher=None):
        now = iso(utcnow())
        self.db.execute("INSERT INTO runs(run_id,project,req,iid,sha,voucher_id,state,paths,owner,voucher,created,"
                        "updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, project, req, iid, sha, voucher_id, "spooled", json.dumps(list(paths)), owner,
                         json.dumps(voucher or {}, ensure_ascii=False), now, now))

    def set_run(self, run_id, frm, to, **fields) -> bool:
        sets = ", ".join(["state=?", "updated=?"] + ["%s=?" % k for k in fields])
        args = [to, iso(utcnow())] + list(fields.values()) + [run_id, frm]
        return self.db.execute("UPDATE runs SET %s WHERE run_id=? AND state=?" % sets, args).rowcount == 1

    def run(self, run_id):
        return self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def runs_in(self, *states):
        q = "SELECT * FROM runs WHERE state IN (%s) ORDER BY created" % ",".join("?" * len(states))
        return self.db.execute(q, states).fetchall()

    def count(self, state, project=None) -> int:
        if project is None:
            return self.db.execute("SELECT COUNT(*) FROM runs WHERE state=?", (state,)).fetchone()[0]
        return self.db.execute("SELECT COUNT(*) FROM runs WHERE state=? AND project=?",
                               (state, project)).fetchone()[0]
