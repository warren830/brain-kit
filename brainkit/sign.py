"""Vouchers: canonical JSON signed with Ed25519 through `ssh-keygen -Y`.

CI and the runner verify offline with an allowed_signers file; no Python crypto package is needed.
In production the private key sits on the bridge host only (or behind Vault/KMS via ssh-agent).

The bridge signs through a signer object, `signer.sign(voucher) -> armored signature`:
  KeySigner    the default: the private key file in bridge.json `signing_key`
  SpoolSigner  bridge.json "signer": {"type": "spool", "spool_dir": ..., "allowed_signers": ...}: a separate signing
               service (another OS user holds the key) answers through spool files and may refuse
A signer that will not sign raises SignerRefused (with reasons); one that does not answer raises SignerUnavailable
(nothing was signed: try again later). The voucher format is the same whoever signs it.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time

from .common import BrainError, canonical, iso, run, utcnow, write_json

NAMESPACE = "brain-voucher"
PRINCIPAL = "brain-bridge"


def available() -> bool:
    return shutil.which("ssh-keygen") is not None


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def sign(obj, key_path) -> str:
    """Return the armored SSH signature over canonical(obj)."""
    if not os.path.exists(key_path):
        raise BrainError("signing key not found: %s" % key_path)
    with tempfile.TemporaryDirectory() as d:
        msg = os.path.join(d, "m")
        with open(msg, "wb") as f:
            f.write(canonical(obj))
        run(["ssh-keygen", "-q", "-Y", "sign", "-f", key_path, "-n", NAMESPACE, msg])
        with open(msg + ".sig", encoding="ascii") as f:
            return f.read()


def verify(obj, signature, allowed_signers, principal=PRINCIPAL) -> bool:
    with tempfile.TemporaryDirectory() as d:
        sig = os.path.join(d, "s")
        with open(sig, "w", encoding="ascii") as f:
            f.write(signature)
        p = run(["ssh-keygen", "-Y", "verify", "-f", allowed_signers, "-I", principal, "-n", NAMESPACE, "-s", sig],
                check=False, input_text=canonical(obj).decode("utf-8"))
        return p.returncode == 0


def write_allowed_signers(path, pubkey_path, principal=PRINCIPAL):
    with open(pubkey_path, encoding="ascii") as f:
        pub = f.read().strip()
    with open(path, "w", encoding="ascii") as f:
        f.write('%s namespaces="%s" %s\n' % (principal, NAMESPACE, pub))


# ---------- signers ----------

class SignerRefused(BrainError):
    """The signer checked the voucher and will not sign it. Each reason is one line (they are posted on the MR)."""

    def __init__(self, reasons):
        self.reasons = [" ".join(str(r).split())[:300] for r in (reasons or [])] or ["no reason given"]
        super().__init__("the signer refused: " + "; ".join(self.reasons))


class SignerUnavailable(BrainError):
    """The signer did not answer in time: nothing was signed."""


class KeySigner:
    """Signs with the bridge's private key file (ssh-keygen -Y sign)."""

    def __init__(self, key_path):
        self.key_path = key_path

    def sign(self, voucher) -> str:
        return sign(voucher, self.key_path)


MAX_SPOOL_FILE = 64 * 1024


def _read_small(path):
    """JSON from a small regular file, opened without following a symlink or blocking on a FIFO (another OS user
    writes the spool's answers). None when there is no such file yet, or it is not one."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SPOOL_FILE:
            return None
        data = os.read(fd, MAX_SPOOL_FILE + 1)
    finally:
        os.close(fd)
    try:
        return json.loads(data.decode("utf-8")) if len(data) <= MAX_SPOOL_FILE else None
    except (ValueError, UnicodeDecodeError):
        return None


class SpoolSigner:
    """A signing service in another process, reached through spool files only (no socket):

      <spool>/requests/<req>.json  written here:      {"schema": 1, "req_id", "kind", "created", "voucher"}
      <spool>/signed/<req>.json    written by it:     {"req_id", "voucher", "sig"}
      <spool>/refused/<req>.json   written by it:     {"req_id", "reasons": ["..."]}

    <req> is <kind>-<run_id>-<first 12 hex of the voucher's digest>. A signature counts only when the signed voucher
    is exactly the one asked for and verifies against allowed_signers (the bridge's public key). The request is taken
    back when an answer arrives or the wait ends. Keep this spool apart from runnerd's (bridge.json spool_dir)."""

    def __init__(self, spool_dir, allowed_signers, timeout=30.0, poll=0.25, clock=time.monotonic, sleep=time.sleep):
        self.spool, self.allowed = spool_dir, allowed_signers
        self.timeout, self.poll, self.clock, self.sleep = float(timeout), float(poll), clock, sleep

    @staticmethod
    def req_id(voucher) -> str:
        kind = re.sub(r"[^a-z]", "", str(voucher.get("kind") or "x")) or "x"
        run_id = re.sub(r"[^A-Za-z0-9-]", "", str(voucher.get("run_id") or "x"))[:60] or "x"
        return "%s-%s-%s" % (kind, run_id, digest(voucher)[:12])

    def sign(self, voucher) -> str:
        if not isinstance(voucher, dict):
            raise SignerRefused(["only a JSON object is signed"])
        if not os.path.isfile(self.allowed) or not os.access(self.allowed, os.R_OK):
            # the bridge's own config, not the signing service: no signature could ever verify. A plain BrainError
            # (nothing asked, nothing refused): the hand-over or answer is looked at again once it is fixed.
            raise BrainError("bridge config: signer.allowed_signers %s is not a readable file: no signature can be "
                             "checked" % self.allowed)
        rid = self.req_id(voucher)
        req = os.path.join(self.spool, "requests", rid + ".json")
        write_json(req, {"schema": 1, "req_id": rid, "kind": voucher.get("kind"), "created": iso(utcnow()),
                         "voucher": voucher}, mode=0o640)
        end = self.clock() + self.timeout
        try:
            while True:
                got = _read_small(os.path.join(self.spool, "signed", rid + ".json"))
                if isinstance(got, dict):
                    sig = got.get("sig")
                    if got.get("voucher") != voucher:
                        raise SignerRefused(["the signer signed another voucher than the one asked for (%s)" % rid])
                    if not isinstance(sig, str) or not verify(voucher, sig, self.allowed):
                        raise SignerRefused(["the signature does not verify against %s (%s)" % (self.allowed, rid)])
                    return sig
                no = _read_small(os.path.join(self.spool, "refused", rid + ".json"))
                if isinstance(no, dict):
                    reasons = no.get("reasons") if isinstance(no.get("reasons"), list) else []
                    raise SignerRefused(reasons[:10])
                if self.clock() >= end:
                    raise SignerUnavailable("the signer did not answer %s within %ds" % (rid, self.timeout))
                self.sleep(self.poll)
        finally:
            try:
                os.unlink(req)
            except OSError:
                pass


SIGNER_TYPES = ("key", "spool")


def _signer_cfg(cfg) -> dict:
    s = cfg.get("signer") or {}
    return {"type": s} if isinstance(s, str) else dict(s) if isinstance(s, dict) else {"type": repr(s)}


def seconds(x, default=None):
    """A number of seconds > 0 as a float: an int or float, or a string that spells one ("30", " 2.5 "), as
    c9e59d1 read signer.timeout with float(). `default` for anything else (a bool, NaN, "30s", a tuple)."""
    if isinstance(x, bool) or not isinstance(x, (int, float, str)):
        return default
    try:
        f = float(x.strip() if isinstance(x, str) else x)
    except (TypeError, ValueError, OverflowError):
        return default
    return f if math.isfinite(f) and f > 0 else default


def _seconds(x) -> bool:
    return seconds(x) is not None


def signer_problems(cfg) -> list:
    """What is wrong with the signing part of a bridge config (`signing_key`, `signer`). Files are not looked at here
    (signer_file_problems; a spool signer checks its allowed_signers each time it signs, so commands that never
    sign - report, dispatch, reconcile - run while the signing service's public key is being deployed)."""
    s = _signer_cfg(cfg)
    kind = s.get("type") or "key"
    if kind not in SIGNER_TYPES:
        return ["signer.type must be one of %s, not %s" % (SIGNER_TYPES, kind)]
    if kind == "key":
        key = s.get("key") or cfg.get("signing_key")
        if not key:
            return ["signing_key is required (or a signer of type spool)"]
        return [] if isinstance(key, str) else ["signing_key must be a path"]
    p = ["signer.%s is required for a spool signer" % k for k in ("spool_dir", "allowed_signers") if not s.get(k)]
    p += ["signer.%s must be a path" % k for k in ("spool_dir", "allowed_signers") if s.get(k)
          and not isinstance(s[k], str)]
    p += ["signer.%s must be a number of seconds > 0, not %r" % (k, s[k]) for k in ("timeout", "poll")
          if k in s and not _seconds(s[k])]
    if p:
        return p
    if s.get("spool_dir") and cfg.get("spool_dir") and \
            os.path.realpath(s["spool_dir"]) == os.path.realpath(cfg["spool_dir"]):
        p.append("signer.spool_dir must not be the runner's spool_dir (runnerd reads its requests/)")
    return p


def signer_file_problems(cfg) -> list:
    """Files a bridge config's signer needs that are not there: a spool signer's allowed_signers (without it every
    signature the service returns would look forged, and be taken for a refusal)."""
    s = _signer_cfg(cfg)
    if (s.get("type") or "key") == "spool" and isinstance(s.get("allowed_signers"), str) and s["allowed_signers"] \
            and not os.path.isfile(s["allowed_signers"]):
        return ["signer.allowed_signers %s does not exist (the signing service's public key, as an ssh "
                "allowed_signers file)" % s["allowed_signers"]]
    return []


def signer_from_config(cfg):
    """The signer a bridge config names: the key file signer unless "signer" says otherwise."""
    p = signer_problems(cfg)
    s = _signer_cfg(cfg)
    if (s.get("type") or "key") == "key":
        # a missing key fails when something is signed, as it always did
        return KeySigner(s.get("key") or cfg.get("signing_key", ""))
    if p:
        raise BrainError("bridge config: " + "; ".join(p))
    return SpoolSigner(s["spool_dir"], s["allowed_signers"], timeout=seconds(s.get("timeout", 30), 30.0),
                       poll=seconds(s.get("poll", 0.25), 0.25))
