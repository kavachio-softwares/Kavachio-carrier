"""Secrets Kavachio has to be able to USE again — kept encrypted at rest.

An API key is never stored (intake_auth keeps a one-way hash, because we only
ever have to CHECK it). An external SFTP login is the opposite case: Kavachio
logs in to somebody else's server every few minutes, so it needs the password
or private key itself, not a hash of it. Those are encrypted with Fernet
(AES-128-CBC + HMAC-SHA256, from `cryptography`) before they reach the
database, and only decrypted in memory for the moment a connection is made.

THE KEY. INTAKE_SECRET_KEY, a Fernet key:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Several keys, comma separated, rotate: the FIRST encrypts, every one of them
can decrypt. A value that is not a Fernet key is still accepted — it is
stretched into one with SHA-256 — so a long random passphrase works too.

Unset, the key is derived from JWT_SECRET (or the development secret) with
SHA-256, and a warning is logged once. That keeps a development machine
working with no setup, but it ties the stored SFTP passwords to the token
secret: change JWT_SECRET and every saved SFTP login has to be entered again.
Set INTAKE_SECRET_KEY anywhere real.

Read at call time, not import time: the .env is loaded after this module may
already have been imported (the same reason storage.py resolves lazily).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import threading

log = logging.getLogger("kavachio.intake_secrets")

# The same fallback settings.py uses, so an unconfigured development machine
# derives the same key in every process.
_DEV_SECRET = "dev-insecure-change-me"

_lock = threading.Lock()
_cache: dict = {"env": None, "fernet": None}
_warned = {"done": False}


class SecretUnreadable(Exception):
    """A stored secret could not be decrypted — almost always because the key
    it was encrypted with has changed or been removed since."""


def _derive(material: str) -> bytes:
    """Any string -> a valid Fernet key (32 bytes, urlsafe base64)."""
    return base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())


def _as_fernet(value: str):
    from cryptography.fernet import Fernet
    try:
        return Fernet(value.encode("utf-8"))
    except (ValueError, binascii.Error):
        # Not a Fernet key as such — a passphrase. Stretch it into one rather
        # than refusing to start over a formatting detail.
        return Fernet(_derive(value))


def _fernet():
    from cryptography.fernet import Fernet, MultiFernet
    raw = (os.getenv("INTAKE_SECRET_KEY") or "").strip()
    jwt = (os.getenv("JWT_SECRET") or "").strip()
    env = (raw, jwt)
    with _lock:
        if _cache["env"] == env and _cache["fernet"] is not None:
            return _cache["fernet"]
        if raw:
            keys = [k.strip() for k in raw.split(",") if k.strip()]
            fernets = [_as_fernet(k) for k in keys]
            f = fernets[0] if len(fernets) == 1 else MultiFernet(fernets)
        else:
            if not _warned["done"]:
                _warned["done"] = True
                log.warning(
                    "INTAKE_SECRET_KEY is unset — encrypting saved SFTP logins with a key "
                    "derived from JWT_SECRET%s. Set INTAKE_SECRET_KEY before deploying; "
                    "changing JWT_SECRET later would make every saved SFTP login unreadable.",
                    "" if jwt else " (itself unset: the insecure development secret)")
            f = Fernet(_derive(jwt or _DEV_SECRET))
        _cache["env"], _cache["fernet"] = env, f
        return f


def encrypt(plain: str) -> str:
    """Plaintext -> a Fernet token (a str, safe to put in JSON)."""
    return _fernet().encrypt(str(plain).encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """A Fernet token -> plaintext. Raises SecretUnreadable in plain words."""
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(str(token).encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError, TypeError) as exc:
        raise SecretUnreadable(
            "The saved password or key can no longer be read — the server's "
            "encryption key (INTAKE_SECRET_KEY) has changed since it was saved. "
            "Set this channel up again with the password or key.") from exc
