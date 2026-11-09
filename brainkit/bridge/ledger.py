"""Bridge ledger (sqlite). Every state change is compare-and-set, so a decision takes effect once.

Threads may share one Ledger: `ledger.db` is one sqlite connection whose every statement runs under one lock, and
its rows are read before the lock is let go. Code that needs several statements as one step (read a count, then
insert) holds `ledger.lock` around them; it is re-entrant."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
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
CREATE TABLE IF NOT EXISTS mr_desc_notes (
  project TEXT NOT NULL, iid INTEGER NOT NULL, run_id TEXT, note_id INTEGER, md_hash TEXT, sha TEXT,
  state TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
  PRIMARY KEY (project, iid));
CREATE TABLE IF NOT EXISTS outbox (
  send_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, slot TEXT, what TEXT NOT NULL, message TEXT NOT NULL,
  state TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notices (
  key TEXT PRIMARY KEY, run_id TEXT, project TEXT, iid INTEGER, state TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, updated TEXT NOT NULL);
"""
# pending_vouchers: the voucher last sent to a signer that did not answer in time, by what it is for (a hand-over
# check, a decision). The next round asks for that same voucher, so a late signature is used, not left over.
# mr_desc_notes: the bridge's own 「变更说明」 comment on an MR (runs/<KEY>/MR.md, posted before an acceptance
# question and edited when MR.md changes). note_id: the comment; md_hash: sha256 of the MR.md it shows ("missing":
# the run wrote none); sha: the MR head it was last checked at. state: wanted (to be posted), posting (one bridge
# process posts it now; taken over once stale), posted. A ledger from before this table gets it when it is opened
# (CREATE TABLE IF NOT EXISTS); columns added later go in MR_DESC_ADDED_COLUMNS.
# outbox: every message the bridge sends into a run's KiroCrew slot (the hand-over, an answer, a notice), written
# before it is sent, so a bridge that stops mid-send knows what it was sending. send_id: meta.sendId (the voucher id,
# or N-<hash> for a notice); what: handover:<run> | answer:<D-n> | notice:<key>. state: sending (being sent now, by
# this or another bridge process), interrupted (the bridge was stopped while it sent: it may or may not have
# landed), sent, unknown (KiroCrew answered with an error: it may or may not have landed). A row is settled again by
# reading the slot (a user message with that sendId landed); tries: how often it was sent.
# notices: things the bridge tells once (an MR note, a counter of attempts), by key. state: telling (one bridge
# process posts it now; taken over once stale), told, counting (only `count` matters).
MR_DESC_ADDED_COLUMNS = ()
OUTBOX_STATES = ("sending", "interrupted", "sent", "unknown")


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
    # how often, and how long apart, giving a reservation back is tried when sqlite is busy ("database is locked":
    # another process on this ledger file holds its write lock longer than sqlite's own 5 s wait)
    RELEASE_TRIES, RELEASE_PAUSE = 3, 1.0

    def __init__(self, path):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.db = SharedConnection(path)
        self.lock = self.db.lock
        self.sleep = time.sleep
        self.db.executescript(SCHEMA)
        self._add_columns("mr_desc_notes", MR_DESC_ADDED_COLUMNS)

    def _add_columns(self, table, columns):
        """Columns a newer brain-kit added to `table`, added to a ledger file made before them ((name, type) pairs)."""
        if not columns:
            return
        with self.lock:
            have = {r[1] for r in self.db.execute("PRAGMA table_info(%s)" % table)}
            for name, kind in columns:
                if name not in have:
                    try:
                        self.db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, kind))
                    except sqlite3.OperationalError:      # another process added it meanwhile
                        pass

    def close(self):
        self.db.close()

    # handoffs: one row per (MR, head sha)
    def claim_handoff(self, project, iid, sha, stale_after=None) -> bool:
        """Insert as 'checking'; False if this MR+sha was already seen. With `stale_after` (seconds), a 'checking'
        row older than that was left by a bridge that stopped while checking it: it is taken over (True)."""
        now = utcnow()
        cur = self.db.execute("INSERT OR IGNORE INTO handoffs(project,iid,sha,state,created,updated) "
                              "VALUES (?,?,?,?,?,?)", (project, iid, sha, "checking", iso(now), iso(now)))
        if cur.rowcount == 1:
            return True
        if stale_after is None:
            return False
        return self.db.execute("UPDATE handoffs SET updated=? WHERE project=? AND iid=? AND sha=? AND "
                               "state='checking' AND updated<?", (iso(now), project, iid, sha,
                                                                  iso(now - timedelta(seconds=stale_after)))
                               ).rowcount == 1

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

    def open_run_for(self, req, stale_after=None):
        """An open run of `req` (a reservation counts), or None. With `stale_after` (seconds), a reservation older
        than that does not count: a bridge that stopped while signing left it (reserve_run drops it)."""
        states = OPEN_RUN_STATES + (RESERVED,)
        q = "SELECT * FROM runs WHERE req=? AND state IN (%s)" % ",".join("?" * len(states))
        args = (req,) + states
        if stale_after is not None:
            q += " AND NOT (state=? AND updated<?)"
            args += (RESERVED, iso(utcnow() - timedelta(seconds=stale_after)))
        return self.db.execute(q + " ORDER BY created", args).fetchone()

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

    def release_run(self, run_id) -> bool:
        """Give a reserved run id back (its contract was not signed). Never raises (it runs while another error is
        on its way up): when sqlite stays busy the reservation is left, and counts for nobody once it is stale
        (open_run_for, reserve_run, drop_stale). False then."""
        for n in range(self.RELEASE_TRIES):
            try:
                self.db.execute("DELETE FROM runs WHERE run_id=? AND state=?", (run_id, RESERVED))
                return True
            except sqlite3.Error:
                if n + 1 < self.RELEASE_TRIES:
                    self.sleep(self.RELEASE_PAUSE * (n + 1))
        return False

    def drop_stale(self, stale_after) -> int:
        """Rows a bridge that stopped mid-way left behind, older than `stale_after` seconds: run reservations and
        hand-over checks still 'checking' (looked at again next poll). How many were dropped."""
        cut = iso(utcnow() - timedelta(seconds=stale_after))
        with self.lock:
            n = self.db.execute("DELETE FROM runs WHERE state=? AND updated<?", (RESERVED, cut)).rowcount
            n += self.db.execute("DELETE FROM handoffs WHERE state='checking' AND updated<?", (cut,)).rowcount
        return n

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

    # the bridge's 「变更说明」 comment on an MR (see mr_desc_notes above); every change is compare-and-set
    def mr_desc_note(self, project, iid):
        """The MR's description comment row, or None when none is wanted yet."""
        return self.db.execute("SELECT * FROM mr_desc_notes WHERE project=? AND iid=?", (project, int(iid))).fetchone()

    def want_mr_desc_note(self, project, iid, run_id) -> bool:
        """Record that the MR gets a description comment (state 'wanted'). False when it has a row already."""
        now = iso(utcnow())
        return self.db.execute("INSERT OR IGNORE INTO mr_desc_notes(project,iid,run_id,state,created,updated) "
                               "VALUES (?,?,?,?,?,?)", (project, int(iid), run_id, "wanted", now, now)).rowcount == 1

    def claim_mr_desc_post(self, project, iid, stale_after=300) -> bool:
        """Claim posting the comment (wanted -> posting): one bridge process posts it. A claim older than
        `stale_after` seconds was left by a bridge that stopped while posting: taken over."""
        now = utcnow()
        return self.db.execute("UPDATE mr_desc_notes SET state='posting', updated=? WHERE project=? AND iid=? AND "
                               "(state='wanted' OR (state='posting' AND updated<?))",
                               (iso(now), project, int(iid), iso(now - timedelta(seconds=stale_after)))).rowcount == 1

    def release_mr_desc_post(self, project, iid):
        """The post failed: wanted again (posted next round)."""
        self.db.execute("UPDATE mr_desc_notes SET state='wanted', updated=? WHERE project=? AND iid=? AND "
                        "state='posting'", (iso(utcnow()), project, int(iid)))

    def set_mr_desc_note(self, project, iid, note_id, md_hash, sha) -> bool:
        """Posted (posting -> posted): the comment's id (None: GitLab did not say), what it shows, at which head."""
        return self.db.execute("UPDATE mr_desc_notes SET state='posted', note_id=?, md_hash=?, sha=?, updated=? "
                               "WHERE project=? AND iid=? AND state='posting'",
                               (note_id, md_hash, sha, iso(utcnow()), project, int(iid))).rowcount == 1

    def mr_desc_checked(self, project, iid, note_id, md_hash, sha) -> bool:
        """The posted comment now shows `md_hash` (edited, or the same MR.md at a newer head `sha`)."""
        return self.db.execute("UPDATE mr_desc_notes SET md_hash=?, sha=?, updated=? WHERE project=? AND iid=? AND "
                               "state='posted' AND note_id IS ?",
                               (md_hash, sha, iso(utcnow()), project, int(iid), note_id)).rowcount == 1

    def mr_desc_gone(self, project, iid, note_id) -> bool:
        """The posted comment cannot be edited (deleted, or GitLab gave no id): wanted again, as a new comment."""
        return self.db.execute("UPDATE mr_desc_notes SET state='wanted', note_id=NULL, updated=? WHERE project=? AND "
                               "iid=? AND state='posted' AND note_id IS ?",
                               (iso(utcnow()), project, int(iid), note_id)).rowcount == 1

    # the outbox (see above): compare-and-set on state
    def outbox(self, send_id):
        return self.db.execute("SELECT * FROM outbox WHERE send_id=?", (send_id,)).fetchone()

    def outbox_put(self, send_id, run_id, slot, what, message):
        """Record a message about to be sent (state 'sending'). An earlier row with this send id is kept as it is
        (the caller looks at its state). -> (the row, whether it is new)."""
        now = iso(utcnow())
        new = self.db.execute("INSERT OR IGNORE INTO outbox(send_id,run_id,slot,what,message,state,tries,created,"
                              "updated) VALUES (?,?,?,?,?,?,?,?,?)",
                              (send_id, run_id, slot, what, message, "sending", 0, now, now)).rowcount == 1
        return self.outbox(send_id), new

    def outbox_set(self, send_id, frm, to, tried=False, updated=None) -> bool:
        """frm: a state or a tuple of states. `updated`: only when the row was last changed then (a stale row is
        taken over by one bridge process only)."""
        frm = (frm,) if isinstance(frm, str) else tuple(frm)
        q = ("UPDATE outbox SET state=?, updated=?%s WHERE send_id=? AND state IN (%s)%s"
             % (", tries=tries+1" if tried else "", ",".join("?" * len(frm)), " AND updated=?" if updated else ""))
        args = (to, iso(utcnow()), send_id) + frm + ((updated,) if updated else ())
        return self.db.execute(q, args).rowcount == 1

    def outbox_rows(self, *states, older_than=None):
        """Rows in these states; with `older_than` (seconds), only those not updated since then."""
        q = "SELECT * FROM outbox WHERE state IN (%s)" % ",".join("?" * len(states))
        args = tuple(states)
        if older_than is not None:
            q += " AND updated<?"
            args += (iso(utcnow() - timedelta(seconds=older_than)),)
        return self.db.execute(q + " ORDER BY created", args).fetchall()

    # notices: told once (see above)
    def notice(self, key):
        return self.db.execute("SELECT * FROM notices WHERE key=?", (key,)).fetchone()

    def claim_notice(self, key, run_id=None, project=None, iid=None, stale_after=300) -> bool:
        """Claim telling `key` (-> 'telling'): True once, until it is told or given back. A claim older than
        `stale_after` seconds was left by a bridge that stopped while telling: taken over."""
        now = utcnow()
        with self.lock:
            if self.db.execute("INSERT OR IGNORE INTO notices(key,run_id,project,iid,state,count,created,updated) "
                               "VALUES (?,?,?,?,?,?,?,?)", (key, run_id, project, iid, "telling", 0, iso(now),
                                                            iso(now))).rowcount == 1:
                return True
            return self.db.execute("UPDATE notices SET state='telling', updated=? WHERE key=? AND (state='counting' "
                                   "OR (state='telling' AND updated<?))",
                                   (iso(now), key, iso(now - timedelta(seconds=stale_after)))).rowcount == 1

    def notice_told(self, key):
        self.db.execute("UPDATE notices SET state='told', updated=? WHERE key=?", (iso(utcnow()), key))

    def release_notice(self, key):
        """Telling failed: told again next time (a counter row keeps its count)."""
        with self.lock:
            self.db.execute("DELETE FROM notices WHERE key=? AND state='telling' AND count=0", (key,))
            self.db.execute("UPDATE notices SET state='counting', updated=? WHERE key=? AND state='telling'",
                            (iso(utcnow()), key))

    def bump(self, key, run_id=None) -> int:
        """Add one to the counter `key` (a row in state 'counting'); -> the new count."""
        now = iso(utcnow())
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO notices(key,run_id,state,count,created,updated) "
                            "VALUES (?,?,?,?,?,?)", (key, run_id, "counting", 0, now, now))
            self.db.execute("UPDATE notices SET count=count+1, updated=? WHERE key=?", (now, key))
            return int(self.notice(key)["count"])

    def drop_count(self, key):
        """A counter starts again (a row in state 'counting' only)."""
        self.db.execute("DELETE FROM notices WHERE key=? AND state='counting'", (key,))

    def count_of(self, key) -> int:
        row = self.notice(key)
        return int(row["count"]) if row is not None else 0

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
