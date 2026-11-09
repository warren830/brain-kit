"""What the bridge sends into a run's KiroCrew slot, and what it tells an MR once (mixed into Bridge).

Every message into a slot - the hand-over, an answer, a notice - goes through the ledger's outbox: it is written
before it is sent and marked sent after. A bridge stopped while it sends (SIGTERM, Ctrl-C) marks the row
'interrupted' on its way out; a crash leaves it 'sending'; a KiroCrew error leaves it 'unknown'. Such a row is
settled by reading the slot, because KiroCrew does not deduplicate by meta.sendId (kirocrew.py): a user message with
that sendId in the slot -> it landed; the slot readable, idle and without it -> it did not, and the same message is
sent again with the same sendId (at most SEND_TRIES sends); the slot not readable -> it stays 'unknown' (whoever
started the send has told the MR) and is looked at again next round.

A notice is a message the bridge sends a run on its own: its question was not asked or became void, its CI failed.
It starts with a fixed word (NOTICE_PREFIXES) and is sent once per key (its sendId is derived from the key). Notices
are written by the bridge; any text in them that came from elsewhere (a CI log) is quoted line by line, and no line
reads as an approval or a command (inert()).
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from ..common import BrainError, iso, utcnow

SEND_TRIES = 3            # how often one message (one sendId) is sent at most
UNKNOWN_KEPT_S = 86400    # a notice whose outcome stays unknown is looked at for this long
NOTICE_PREFIXES = ("问题没发出：", "问题作废：", "CI 没过：")


def inert(text) -> str:
    """A message the bridge writes for a run: a line that reads as an approval, a quick action or "ignore the rules"
    is quoted ("> "), so neither Lean nor the agent takes it as one."""
    from ..intake import checks
    out = []
    for line in str(text).replace("\r\n", "\n").split("\n"):
        if checks.APPROVAL_LINE_RE.match(line) or checks.INJECTION_RE.search(line) or line.lstrip().startswith("/"):
            line = "> " + line
        out.append(line)
    return "\n".join(out)


def one_line(text, limit=300) -> str:
    """Text from elsewhere (an error, a job name) as one inert line in inline code."""
    s = " ".join(str(text or "").split())[:limit]
    return "`%s`" % s.replace("`", "ˋ")


def notice_send_id(run_id, key) -> str:
    """The sendId of a notice: the same for the same run and key (sent once)."""
    return "N-" + hashlib.sha256(("%s\n%s" % (run_id, key)).encode("utf-8")).hexdigest()[:20]


class Delivery:
    """Needs self.ledger, self.kc, self.gl, self.out (and self.reservation_ttl: how old a 'sending' row must be to
    count as left by a bridge that stopped)."""

    send_error = ""

    def _stale(self, ts, seconds=None) -> bool:
        if seconds is None:
            seconds = getattr(self, "reservation_ttl", 900.0)
        return str(ts or "") < iso(utcnow() - timedelta(seconds=seconds))

    # ---------- sending ----------

    def _send_once(self, run_id, slot, send_id, what, message) -> str:
        """Send `message` into `slot` once (see the module docstring). -> "sent" (now or before), "unknown" (it may
        or may not have landed: self.send_error says why, when KiroCrew answered with an error) or "busy" (another
        bridge process is sending it now)."""
        row, new = self.ledger.outbox_put(send_id, run_id, slot, what, message)
        if new:
            return self._push(send_id, slot, message)
        return self._settle(row)

    def _settle(self, row) -> str:
        """An outbox row that is not being sent by this call: sent, being sent elsewhere, or to be settled."""
        led, sid, st = self.ledger, row["send_id"], row["state"]
        self.send_error = ""
        if st == "sent":
            return "sent"
        if st == "sending" and not self._stale(row["updated"]):
            return "busy"
        landed = self._landed(row["slot"], sid)
        if landed:
            led.outbox_set(sid, (st,), "sent")
            self.out("%s: %s had landed in slot %s" % (row["run_id"], sid, row["slot"]))
            return "sent"
        if landed is None or int(row["tries"] or 0) >= SEND_TRIES:
            if st != "unknown":
                led.outbox_set(sid, (st,), "unknown")
            if landed is False:
                self._gave_up(row)
            return "unknown"
        if not led.outbox_set(sid, (st,), "sending", updated=row["updated"]):
            return "busy"                            # another bridge process took it over just now
        self.out("%s: %s did not land in slot %s; sending it again (same sendId)" % (row["run_id"], sid, row["slot"]))
        return self._push(sid, row["slot"], row["message"])

    def _push(self, send_id, slot, message) -> str:
        led = self.ledger
        self.send_error = ""
        try:
            self.kc.send(slot, message, send_id)
        except Exception as e:  # noqa: BLE001 - a 5xx, a read timeout, KiroCrew restarting: it may have landed
            led.outbox_set(send_id, "sending", "unknown", tried=True)
            self.send_error = str(e)
            self.out("send %s to slot %s: outcome unknown: %s" % (send_id, slot, e))
            return "unknown"
        except BaseException:
            # stopped while sending (SIGTERM, Ctrl-C): settled at the next start by reading the slot
            led.outbox_set(send_id, "sending", "interrupted", tried=True)
            raise
        led.outbox_set(send_id, "sending", "sent", tried=True)
        return "sent"

    def _landed(self, slot, send_id):
        """Did a message with this sendId reach the slot? True / False / None (cannot tell: the slot cannot be read,
        or it is busy or has queued messages that may hold it)."""
        if not slot or not getattr(self, "kc", None):
            return None
        try:
            data = self.kc.slot(slot)
        except Exception as e:  # noqa: BLE001
            self.out("slot %s cannot be read: %s" % (slot, e))
            return None
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            return None
        queue = data.get("queue") if isinstance(data.get("queue"), list) else []

        def has(rows):
            for m in rows:
                if not isinstance(m, dict):
                    continue
                meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
                if send_id in (meta.get("sendId"), m.get("sendId")):
                    return True
            return False
        if has(data["messages"]) or has(queue):
            return True
        # not there: it did not land only when the slot says it is idle with nothing queued (the brain app's slot
        # read shows neither, and a queued message may not be in the transcript yet)
        idle = "running" in data and not data["running"] and isinstance(data.get("queue"), list) and not queue \
            and not data.get("pending_approval") and not data.get("queue_depth")
        return False if idle else None

    def _gave_up(self, row):
        """Sent SEND_TRIES times and never in the slot: the bridge stops sending it; the MR hears once."""
        run = self.ledger.run(row["run_id"])
        if run is None:
            return
        self.tell_mr_once("gaveup:" + row["send_id"], run["project"], run["iid"], (
            "%s：桥把消息 %s（%s）送了 %d 次，KiroCrew 会话 %s 里都没有，不再自动送了。请负责人在 KiroCrew 里看一下这个会话。")
            % (row["run_id"], row["send_id"], row["what"], int(row["tries"] or 0), row["slot"]), run_id=row["run_id"])

    # ---------- telling ----------

    def tell_mr_once(self, key, project, iid, text, run_id=None) -> bool:
        """Post `text` on the MR once for `key` (the ledger's notices). False when it was told before."""
        if not self.ledger.claim_notice(key, run_id, project, iid):
            return False
        try:
            self.gl.comment(project, iid, text)
        except BaseException:
            self.ledger.release_notice(key)
            raise
        self.ledger.notice_told(key)
        return True

    def notify_run(self, run_row, key, text) -> str:
        """Send the run a notice once for `key` -> "sent" | "unknown" | "busy" | "no-slot"."""
        if not run_row["slot"] or not getattr(self, "kc", None):
            return "no-slot"
        if not str(text).startswith(NOTICE_PREFIXES):
            raise BrainError("a notice to a run starts with one of %s" % (NOTICE_PREFIXES,))
        r = self._send_once(run_row["run_id"], run_row["slot"], notice_send_id(run_row["run_id"], key),
                            "notice:" + key, inert(text))
        self.out("%s: notice %s %s" % (run_row["run_id"], key, r))
        return r

    def _recover_notices(self):
        """Notices whose send was cut off or answered with an error: settled (sent again when they did not land)."""
        for row in self.ledger.outbox_rows("interrupted", "unknown", "sending"):
            if not str(row["what"]).startswith("notice:") or self._stale(row["created"], UNKNOWN_KEPT_S):
                continue
            run = self.ledger.run(row["run_id"])
            if run is None or run["state"] != "dispatched":
                continue
            self._settle(row)
