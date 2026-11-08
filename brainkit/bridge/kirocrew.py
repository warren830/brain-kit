"""KiroCrew gateway client for the bridge app (loopback only; app token from the app secret).

Endpoints read from KiroCrew 0.11.2 source (kiro_crew/dashboard): POST /api/apps/<app>/token with
X-App-Secret; POST /api/chat/slots; POST /api/chat/slots/<slot>/project; POST /api/chat?ws=1;
GET /api/chat/slots/<slot>; POST /api/chat/slots/<slot>/stop. Messages sent this way are recorded as
turnActor=app. Field-level behaviour is still to be confirmed on the runner (V1).
"""
from __future__ import annotations

import json
import re

from ..common import BrainError
from ..rest import HttpError, Rest, q

SEND_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class KiroCrew:
    def __init__(self, base, app, secret, transport=None):
        if not re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/?$", base or ""):
            raise BrainError("kirocrew.base must be a loopback URL (the dashboard is never exposed): %r" % base)
        self.rest = Rest(base, transport=transport)
        self.app = app
        self.secret = secret
        self._token = None

    def _tok(self):
        if not self._token:
            status, payload = self.rest.transport("POST", self.rest.base + "/api/apps/%s/token" % q(self.app),
                                                  {"X-App-Secret": self.secret}, b"")
            if status != 200:
                raise BrainError("KiroCrew app token exchange failed: HTTP %d" % status)
            try:
                tok = (json.loads(payload.decode("utf-8")) or {}).get("token")
            except ValueError:
                tok = None
            if not tok:
                raise BrainError("KiroCrew app token exchange returned no token")
            self._token = tok
        return self._token

    def _call(self, method, path, body=None, query=None):
        for attempt in (1, 2):
            qq = dict(query or {})
            qq["token"] = self._tok()
            try:
                return self.rest.call(method, path, query=qq, body=body, ok=(200, 201, 202, 204))
            except HttpError as e:
                if e.status in (401, 403) and attempt == 1:
                    self._token = None  # expired (5 min for ?token=) or gateway restarted
                    continue
                raise

    def create_slot(self, name, agent, model, memory_mode="incognito"):
        return self._call("POST", "/api/chat/slots",
                          body={"name": name, "agent": agent, "model": model, "memory_mode": memory_mode})

    def set_project(self, name, path):
        return self._call("POST", "/api/chat/slots/%s/project" % q(name), body={"project": path})

    def send(self, name, message, send_id):
        if not SEND_ID_RE.match(send_id):
            raise BrainError("sendId must match [A-Za-z0-9_-]{1,128}")
        return self._call("POST", "/api/chat", query={"ws": "1"},
                          body={"slot": name, "message": message, "meta": {"sendId": send_id}})

    def slot(self, name):
        return self._call("GET", "/api/chat/slots/%s" % q(name))

    def stop(self, name):
        return self._call("POST", "/api/chat/slots/%s/stop" % q(name), body={})
