"""Vouchers: canonical JSON signed with Ed25519 through `ssh-keygen -Y`.

CI and the runner verify offline with an allowed_signers file; no Python crypto package is needed.
In production the private key sits on the bridge host only (or behind Vault/KMS via ssh-agent).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile

from .common import BrainError, canonical, run

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
