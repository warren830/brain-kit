"""Bridge ledger (sqlite). Every state change is compare-and-set, so a decision takes effect once.

Threads may share one Ledger: `ledger.db` is one sqlite connection whose every statement runs under one lock, and
its rows are read before the lock is let go. Code that needs several statements as one step (read a count, then
insert) holds `ledger.lock` around them; it is re-entrant."""
from __future__ import annotations

import json
import os
import sqlite3
import threading

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


class _Rows:
    """A statement's result, read in full while the lock was held (sqlite3 cursors must not be shared)."""

    def __init__(self, rows, rowcount, lastrowid, description):
        self._rows, self._next = rows, 0
        self.rowcount, self.lastrowid, self.description = rowcount, lastrowid, description

    def fetchone(self):
        if self._next >= len(self._rows):
            return None
        self._next += 1
        return self._rows[self._next - 1]

    def fetchall(self):
        rest, self._next = self._rows[self._next:], len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


class SharedConnection:
    """One sqlite connection that any thread may use: each call runs under `lock` (autocommit, as before)."""

    def __init__(self, path):
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()

    def execute(self, sql, params=()):
        with self.lock:
            cur = self._conn.execute(sql, params)
            return _Rows(cur.fetchall(), cur.rowcount, cur.lastrowid, cur.description)

    def executemany(self, sql, seq):
        with self.lock:
            cur = self._conn.executemany(sql, seq)
            return _Rows([], cur.rowcount, cur.lastrowid, cur.description)

    def executescript(self, script):
        with self.lock:
            self._conn.executescript(script)

    def close(self):
        with self.lock:
            self._conn.close()

    def __getattr__(self, name):  # commit, total_changes, ... of the connection itself
        return getattr(self._conn, name)


class Ledger:
    def __init__(self, path):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.db = SharedConnection(path)
        self.lock = self.db.lock
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

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
