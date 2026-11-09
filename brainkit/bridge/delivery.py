"""What the bridge sends into a run's KiroCrew slot, and what it tells an MR once (mixed into Bridge).

Every message into a slot - the hand-over, an answer, a notice - goes through the ledger's outbox: it is written
before it is sent and marked sent after. A bridge stopped while it sends (SIGTERM, Ctrl-C) marks the row
'interrupted' on its way out; a crash leaves it 'sending'; a KiroCrew error leaves it 'unknown'. Such a row is
settled by reading the slot, because KiroCrew does not deduplicate by meta.sendId (kirocrew.py): a user message with
that sendId in the slot -> it landed; the slot readable, idle and without it -> it did not, and the same message is
sent again with the same sendId (each round for its first RESEND_FAST sends, then every RESEND_SLOW_S; the MR hears
once after SEND_TRIES); the slot not readable -> it stays 'unknown' (whoever started the send has told the MR) and is
looked at again next round. There is no last try: a message that never landed is still sent once KiroCrew takes it
again, unless what it was for ended (a hand-over KiroCrew did not take for send_give_up_minutes fails its run; an
answer or a notice whose MR head moved on is dropped, see decisions.py).

A notice is a message the bridge sends a run on its own: its question was not asked or became void, its CI failed.
It starts with a fixed word (NOTICE_PREFIXES) and is sent once per key (its sendId is derived from the key). Notices
are written by the bridge; any text in them that came from elsewhere (a CI log) is quoted line by line, and no line
reads as an approval or a command (inert()). A notice whose send was cut off is sent again only while it still holds
(_notice_hold: the MR head it is about, its pipeline, no question waiting at that head); otherwise it is dropped.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from ..common import BrainError, iso, utcnow

SEND_TRIES = 3            # sends of one message (one sendId) that did not land before the MR is told; sending goes on
RESEND_FAST, RESEND_SLOW_S = 10, 300   # its first RESEND_FAST sends go one each round, the later ones one per 5 min
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

    def _send_once(self, run_id, slot, send_id, what, message, sha=None, count=None) -> str:
        """Send `message` into `slot` once (see the module docstring). `sha`: the MR head it is about; `count`: a
        counter (ledger notices) that goes up once, when this call records the message. -> "sent" (now or before),
        "unknown" (it may or may not have landed: self.send_error says why, when KiroCrew answered with an error),
        "busy" (another bridge process is sending it now) or "dropped" (it is not sent any more)."""
        row, new = self.ledger.outbox_put(send_id, run_id, slot, what, message, sha=sha)
        if new:
            if count:
                self.ledger.bump(count, run_id)
            return self._push(send_id, slot, message)
        return self._settle(row)

    def _settle(self, row, give_up_after=None) -> str:
        """An outbox row that is not being sent by this call: sent, being sent elsewhere, or to be settled ->
        "sent" | "busy" | "unknown" | "dropped". A message that did not land is sent again (same sendId) while the
        slot shows it did not: each round for its first RESEND_FAST sends, then every RESEND_SLOW_S; the MR hears once
        after SEND_TRIES. With `give_up_after` (seconds since its first send) one that still did not land then is
        dropped: the caller ends what it was for (a hand-over fails its run)."""
        led, sid, st = self.ledger, row["send_id"], row["state"]
        self.send_error = ""
        if st in ("sent", "dropped"):
            return st
        if st == "sending" and not self._stale(row["updated"]):
            return "busy"
        landed = self._landed(row["slot"], sid)
        if landed:
            led.outbox_set(sid, (st,), "sent")
            self.out("%s: %s had landed in slot %s" % (row["run_id"], sid, row["slot"]))
            return "sent"
        if landed is None:                       # cannot tell: never sent again blindly
            if st != "unknown":
                led.outbox_set(sid, (st,), "unknown")
            return "unknown"
        tries = int(row["tries"] or 0)
        if tries >= SEND_TRIES:
            self._gave_up(row)
            if give_up_after is not None and self._stale(row["created"], give_up_after):
                if not led.outbox_set(sid, (st,), "dropped"):
                    return "busy"
                self.out("%s: %s never landed in slot %s (%d sends); not sent any more" % (
                    row["run_id"], sid, row["slot"], tries))
                return "dropped"
        if tries >= RESEND_FAST and not self._stale(row["updated"], RESEND_SLOW_S):
            if st != "unknown":
                led.outbox_set(sid, (st,), "unknown")
            return "unknown"                         # sent again later (it has been tried often: KiroCrew is down)
        if not led.outbox_set(sid, (st,), "sending", updated=row["updated"]):
            return "busy"                            # another bridge process took it over just now
        self.out("%s: %s did not land in slot %s; sending it again (same sendId, send %d)"
                 % (row["run_id"], sid, row["slot"], tries + 1))
        return self._push(sid, row["slot"], row["message"], attempt=tries + 1)

    def _push(self, send_id, slot, message, attempt=1) -> str:
        led = self.ledger
        self.send_error = ""
        try:
            again = getattr(self.kc, "send_again", None) if attempt > 1 else None
            if again is not None:        # a fresh Idempotency-Key: KiroCrew must not replay the earlier send's error
                again(slot, message, send_id, attempt)
            else:
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
        """Sent SEND_TRIES times and never in the slot: the MR hears once; the bridge goes on sending it."""
        run = self.ledger.run(row["run_id"])
        if run is None:
            return
        what = str(row["what"])
        tail = {"handover": "KiroCrew 一直不收的话，%d 分钟以后这个 run 就算没能开始，到时这里会说怎么重新交接。"
                            % int(getattr(self, "send_give_up_s", 3600) // 60),
                "answer": "回答不用重新给。"}.get(what.split(":", 1)[0], "")
        self.tell_mr_once("gaveup:" + row["send_id"], run["project"], run["iid"], (
            "%s：桥把消息 %s（%s）送了 %d 次，KiroCrew 会话 %s 里都没有（KiroCrew 出错）。桥会接着重送（先每轮一次，送过 %d 次"
            "以后每 %d 分钟一次），KiroCrew 好了就送到；%s一直不行的话请负责人看一下 KiroCrew。")
            % (row["run_id"], row["send_id"], what, int(row["tries"] or 0), row["slot"], RESEND_FAST,
               RESEND_SLOW_S // 60, tail), run_id=row["run_id"])

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

    def notify_run(self, run_row, key, text, sha=None, count=None) -> str:
        """Send the run a notice once for `key` -> "sent" | "unknown" | "busy" | "dropped" | "no-slot". `sha`: the MR
        head it is about (it is not sent again once the MR has moved on); `count`: a counter bumped once, when the
        notice is first recorded (two bridge processes count it once)."""
        if not run_row["slot"] or not getattr(self, "kc", None):
            return "no-slot"
        if not str(text).startswith(NOTICE_PREFIXES):
            raise BrainError("a notice to a run starts with one of %s" % (NOTICE_PREFIXES,))
        r = self._send_once(run_row["run_id"], run_row["slot"], notice_send_id(run_row["run_id"], key),
                            "notice:" + key, inert(text), sha=sha, count=count)
        self.out("%s: notice %s %s" % (run_row["run_id"], key, r))
        return r

    def _notice_hold(self, run, row) -> str:
        """Does a notice whose send was cut off still hold? "" (send it again when it did not land), "hold" (not
        now: looked at again next round) or why it does not (it is dropped). The decision loop knows the MR."""
        return ""

    def _recover_notices(self):
        """Notices whose send was cut off or answered with an error: settled (sent again when they did not land and
        still hold, dropped when they no longer do). One that fails does not hold up the others."""
        errors = []
        for row in self.ledger.outbox_rows("interrupted", "unknown", "sending"):
            if not str(row["what"]).startswith("notice:") or self._stale(row["created"], UNKNOWN_KEPT_S):
                continue
            if row["state"] == "sending" and not self._stale(row["updated"]):
                continue                          # being sent by another bridge process right now
            try:
                run = self.ledger.run(row["run_id"])
                if run is None or run["state"] != "dispatched":
                    why = "the run is %s" % (run["state"] if run is not None else "gone")
                else:
                    why = self._notice_hold(run, row)
                if why == "hold":
                    continue
                if why:
                    if self.ledger.outbox_set(row["send_id"], (row["state"],), "dropped"):
                        self.out("%s: notice %s not sent again: %s" % (row["run_id"], row["what"][7:], why))
                    continue
                self._settle(row)
            except Exception as e:  # noqa: BLE001 - one notice (its MR, its slot) must not hold up the others
                errors.append("%s: %s" % (row["send_id"], e))
        if errors:
            raise BrainError("; ".join(errors))
