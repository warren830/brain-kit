"""Bridge ledger (sqlite). Every state change is compare-and-set, so a decision takes effect once.

Threads may share one Ledger: `ledger.db` is one sqlite connection whose every statement runs under one lock, and
its rows are read before the lock is let go. Code that needs several statements as one step (read a count, then
insert) holds `ledger.lock` around them; it is re-entrant."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import timedelta

from ..common import BrainError, iso, utcnow

OPEN_RUN_STATES = ("spooled", "ready", "dispatched", "send_unknown")
# A run id taken for a hand-over whose contract is being signed (reserve_run): the requirement counts as having an
# open run, so a second hand-over of it is refused while the first one is signed.
RESERVED = "signing"

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
CREATE TABLE IF NOT EXISTS pending_vouchers (
  ref TEXT PRIMARY KEY, voucher TEXT NOT NULL, created TEXT NOT NULL);
"""
# pending_vouchers: the voucher last sent to a signer that did not answer in time, by what it is for (a hand-over
# check, a decision). The next round asks for that same voucher, so a late signature is used, not left over.


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
        """An open run of `req` (a reservation counts), or None."""
        states = OPEN_RUN_STATES + (RESERVED,)
        q = "SELECT * FROM runs WHERE req=? AND state IN (%s)" % ",".join("?" * len(states))
        return self.db.execute(q, (req,) + states).fetchone()

    @staticmethod
    def is_reservation_for(row, project, iid, sha) -> bool:
        """Is this open-run row the reservation of the hand-over (project, MR, head) being signed right now?"""
        return row["state"] == RESERVED and (row["project"], int(row["iid"]), row["sha"]) == (project, int(iid), sha)

    def reserve_run(self, req, project, iid, sha, stale_after=900):
        """Take the next run id of `req` for this hand-over, as one step: None when `req` has an open run already.
        A reservation older than `stale_after` seconds was left by a bridge that stopped while signing: dropped."""
        now = utcnow()
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")      # one step for other processes on this ledger file, too
            except sqlite3.Error as e:
                raise BrainError("ledger: could not reserve a run for %s: %s" % (req, e))
            try:
                self.db.execute("DELETE FROM runs WHERE req=? AND state=? AND updated<?",
                                (req, RESERVED, iso(now - timedelta(seconds=stale_after))))
                run_id = None
                if self.open_run_for(req) is None:
                    run_id = self.next_run_id(req)
                    self.db.execute("INSERT INTO runs(run_id,project,req,iid,sha,voucher_id,state,created,updated) "
                                    "VALUES (?,?,?,?,?,?,?,?,?)", (run_id, project, req, iid, sha, "", RESERVED,
                                                                   iso(now), iso(now)))
                self.db.execute("COMMIT")
                return run_id
            except BaseException as e:
                try:
                    self.db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if isinstance(e, sqlite3.Error):
                    raise BrainError("ledger: could not reserve a run for %s: %s" % (req, e))
                raise

    def release_run(self, run_id):
        """Give a reserved run id back (its contract was not signed)."""
        self.db.execute("DELETE FROM runs WHERE run_id=? AND state=?", (run_id, RESERVED))

    def add_run(self, run_id, project, req, iid, sha, voucher_id, paths=(), owner="", voucher=None):
        """Record the run as 'spooled': its reservation (reserve_run) when there is one, else a new row."""
        now = iso(utcnow())
        vals = (project, req, iid, sha, voucher_id, "spooled", json.dumps(list(paths)), owner,
                json.dumps(voucher or {}, ensure_ascii=False))
        try:
            with self.lock:
                if self.db.execute("UPDATE runs SET project=?,req=?,iid=?,sha=?,voucher_id=?,state=?,paths=?,owner=?,"
                                   "voucher=?,updated=? WHERE run_id=? AND state=?",
                                   vals + (now, run_id, RESERVED)).rowcount:
                    return
                self.db.execute("INSERT INTO runs(project,req,iid,sha,voucher_id,state,paths,owner,voucher,run_id,"
                                "created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", vals + (run_id, now, now))
        except sqlite3.IntegrityError as e:
            raise BrainError("ledger: run %s is already recorded: %s" % (run_id, e))

    # vouchers a signer has not answered for yet
    def pending_voucher(self, ref, newer_than=3600):
        """The voucher last asked for under `ref` within `newer_than` seconds, or None."""
        row = self.db.execute("SELECT voucher, created FROM pending_vouchers WHERE ref=?", (ref,)).fetchone()
        if row is None or row["created"] < iso(utcnow() - timedelta(seconds=newer_than)):
            return None
        try:
            v = json.loads(row["voucher"])
        except ValueError:
            return None
        return v if isinstance(v, dict) else None

    def set_pending_voucher(self, ref, voucher):
        now = utcnow()
        with self.lock:
            self.db.execute("DELETE FROM pending_vouchers WHERE created<?", (iso(now - timedelta(days=7)),))
            old = self.db.execute("SELECT voucher FROM pending_vouchers WHERE ref=?", (ref,)).fetchone()
            text = json.dumps(voucher, ensure_ascii=False, sort_keys=True)
            if old is not None and old["voucher"] == text:
                return                                  # the same request again: it keeps its age
            self.db.execute("INSERT OR REPLACE INTO pending_vouchers(ref, voucher, created) VALUES (?,?,?)",
                            (ref, text, iso(now)))

    def drop_pending_voucher(self, ref):
        self.db.execute("DELETE FROM pending_vouchers WHERE ref=?", (ref,))

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
