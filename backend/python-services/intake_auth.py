"""Feature 10.2 — API keys for the machine-to-machine way in.

SFTP identifies a broker by the folder the file landed in: the folder IS the
identity. A machine POST has no folder, so the key does that job. It is bound to
one `intake_route`, and the route already carries the broker and (since the 10.2
migration) the programme — so a request carries the file and nothing else, and a
sender cannot claim to be someone they are not, because they hold only their own
key.

    kv_live_7d2e4b9016fa_8fJq2mZr7vT4wYbN1cLx0dK5sHgP3aQe
    │  │    │            └─ secret · 32 chars, proves it is really them
    │  │    └─ prefix · 12 hex chars, the indexed lookup handle
    │  └─ live | test
    └─ fixed marker, so a leaked key is greppable in logs and scrubbable

Stored as a keyed fingerprint, NOT bcrypt. bcrypt is right for app_user
passwords and wrong here: this runs on every machine request, bcrypt costs
~100 ms by design, and these keys carry ~190 bits of entropy — there is no
dictionary to slow down.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Header, HTTPException, Request

from db import SessionLocal
from intake_models import IntakeCredential, IntakeRoute

log = logging.getLogger("kavachio.intake.auth")

KEY_RE = re.compile(r"kv_(live|test)_[0-9a-f]{12}_[A-Za-z0-9]{20,64}")

# Alphanumeric only, deliberately. `secrets.token_urlsafe` emits "-" and "_",
# and an underscore inside the secret would split the key into five parts and
# make it un-parseable. 62**32 is ~190 bits.
_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_DEV_PEPPER = "dev-insecure-change-me"


def _pepper() -> str:
    p = os.getenv("INTAKE_KEY_PEPPER", "").strip()
    if not p:
        log.warning("INTAKE_KEY_PEPPER is unset — using an insecure development "
                    "pepper. Set it before deploying, or every issued key is "
                    "forgeable by anyone who can read the source.")
        return _DEV_PEPPER
    return p


def hash_key(full_key: str) -> str:
    """One-way fingerprint. Same key in, same fingerprint out — but you cannot
    work backwards from the fingerprint to the key."""
    return hmac.new(_pepper().encode(), full_key.encode(), hashlib.sha256).hexdigest()


def mint_key(env: str = "live") -> tuple[str, str, str]:
    """Returns (full_key, prefix, fingerprint). `full_key` is the ONLY time the
    plaintext exists on our side — hand it back to the admin and never store it."""
    prefix = secrets.token_hex(6)
    secret = "".join(secrets.choice(_ALPHABET) for _ in range(32))
    full = f"kv_{env}_{prefix}_{secret}"
    return full, prefix, hash_key(full)


def mask(prefix: str, last4: Optional[str]) -> str:
    """`kv_live_7d2e4b9016fa_…3aQe` — enough to tell two keys apart in the UI,
    useless to whoever is reading over the admin's shoulder."""
    return f"kv_live_{prefix}_…{last4}" if last4 else f"kv_live_{prefix}"


def scrub(text: str) -> str:
    """Redact anything key-shaped. The usual leak is an exception handler that
    dumps request headers."""
    return KEY_RE.sub("kv_***REDACTED***", text or "")


@dataclass(frozen=True)
class IntakePrincipal:
    """Who is sending, resolved entirely from our own database. Nothing on this
    object came from the request body."""
    tenant_id: int              # the carrier
    route_id: int
    credential_id: int
    broker_party_id: Optional[int]
    program_id: Optional[int]   # set when the route is pinned to one programme
    file_style: Optional[str] = None


def _client_ip(request: Request) -> Optional[str]:
    """The FIRST X-Forwarded-For entry — nginx appends the real peer there.
    Never trust anything further down the list; a client can forge it."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _ip_allowed(ip: Optional[str], allowlist) -> bool:
    if not allowlist:
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if addr in ipaddress.ip_network(str(entry), strict=False):
                return True
        except ValueError:
            continue
    return False


def _invalid() -> HTTPException:
    """EVERY authentication failure returns this, byte for byte — bad format,
    unknown prefix, wrong secret, revoked, expired, blocked IP. Distinct
    messages would tell an attacker which guesses were getting warmer."""
    return HTTPException(401, {"error": "invalid_api_key",
                               "message": "The API key is not valid."})


def current_intake_principal(
    request: Request,
    x_api_key: str = Header(default="", alias="X-API-Key"),
    authorization: str = Header(default=""),
) -> IntakePrincipal:
    """Turn an API key into a trusted (carrier, broker, programme).

    Accepts `X-API-Key`, or `Authorization: Bearer kv_…` for clients that can
    only set one auth header.
    """
    raw = (x_api_key or "").strip()
    if not raw and authorization.startswith("Bearer "):
        raw = authorization[7:].strip()

    # maxsplit=3 so the secret is taken whole; the prefix is always field three.
    parts = raw.split("_", 3)
    if len(parts) != 4 or parts[0] != "kv" or not parts[2]:
        raise _invalid()

    with SessionLocal() as s:
        cred = (s.query(IntakeCredential)
                .filter(IntakeCredential.key_prefix == parts[2]).first())
        if cred is None:
            raise _invalid()
        # Constant-time, so the comparison cannot be probed byte by byte.
        if not hmac.compare_digest(cred.key_hash, hash_key(raw)):
            raise _invalid()
        if cred.revoked_at is not None:
            raise _invalid()
        now = datetime.now(timezone.utc)
        if cred.expires_at is not None and cred.expires_at < now:
            raise _invalid()
        if not _ip_allowed(_client_ip(request), cred.ip_allowlist):
            raise _invalid()

        route = s.get(IntakeRoute, cred.route_id)
        if route is None:
            raise _invalid()

        # A switched-off route turns files away WITH a note rather than
        # accepting them silently — the sender is told, so nothing vanishes.
        if not route.is_enabled:
            raise HTTPException(403, {
                "error": "route_disabled",
                "message": "This way in has been switched off by the carrier. "
                           "Contact them before sending again."})

        # Throttled to once a minute: otherwise every request writes a row and
        # this table becomes a write hotspot for no operational gain.
        if cred.last_used_at is None or (now - cred.last_used_at) > timedelta(minutes=1):
            cred.last_used_at = now
            s.commit()

        return IntakePrincipal(
            tenant_id=cred.tenant_id,
            route_id=route.id,
            credential_id=cred.id,
            broker_party_id=route.broker_party_id,
            program_id=getattr(route, "program_id", None),
            file_style=route.file_style,
        )
