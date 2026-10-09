"""Logins for Kavachio's own SFTP server — one per broker's SFTP channel.

THE MODEL (6 Oct 2026). The SFTP server is KAVACHIO's, not the broker's. A
carrier gives a broker an SFTP channel; Kavachio makes the broker a login on its
own server and emails it to them; the broker uploads; sftp_poller collects. The
login sees TWO folders of its own channel — `incoming` to upload into, and
`outbound` where Kavachio answers each file — and nothing else on the server:
not the channel's other folders (processed, rejected, …), not another broker's,
not another carrier's. sftp_server is what enforces that.

(The other SFTP kind — Kavachio signing in to a server somebody else runs — is
sftp_pull, untouched. Its routes have an "sftp://…" address; these have a bare
"carrier/broker" folder.)

WHERE A LOGIN LIVES. In intake_credential, the table API keys already use — no
new table and no migration. One row per channel:

    key_prefix    the user name. Unique across the table, exactly like a key
                  prefix: it is the public handle a sign-in is looked up by.
    key_hash      HMAC-SHA256(pepper, "sftp:<user>:<password>") — the same keyed
                  fingerprint as an API key (intake_auth.hash_key), with the user
                  name inside it so the value can never double as an API key.
    last4         NULL. No part of a password is ever shown anywhere.
    label         "SFTP login".
    ip_allowlist  honoured exactly as for API keys (NULL = from anywhere).
    last_used_at  the last successful sign-in (throttled to once a minute).
    revoked_at    set = the login is refused.

A new password REPLACES the old one on the same row: the broker keeps their user
name, and the old password stops working the moment the new one exists.

THE PASSWORD is generated here and never typed by anyone: 20 characters from a
56-symbol alphabet (~116 bits) in four groups of five, so it can be read out
over the phone. It exists in plaintext only on its way into the broker's email
(and on the carrier's screen, once, when that email cannot go). Because it is
random rather than chosen there is no dictionary to slow down, so the keyed
fingerprint API keys use is the right store, not bcrypt.
"""
from __future__ import annotations

import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import intake_service as svc
from db import SessionLocal
from intake_models import IntakeCredential, IntakeRoute

log = logging.getLogger("kavachio.sftp_accounts")

LABEL = "SFTP login"

# No 0/O, 1/l/I: a password that is read out or retyped must survive it.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"

# What a sign-in may type: lower-case letters, digits and hyphens, starting with
# a letter. Logins made before 9 Oct 2026 have hyphens, so they still match.
_USER_RE = re.compile(r"^[a-z][a-z0-9-]{2,31}$")
# What a NEW login is made as: letters and digits only. Azure Blob Storage SFTP
# (the planned host) allows nothing else in a local user's name, and AWS
# Transfer Family accepts it too — so moving brokers there keeps their names.
_NEW_USER_RE = re.compile(r"^[a-z][a-z0-9]{2,31}$")


@dataclass(frozen=True)
class Login:
    """Who signed in, resolved entirely from our own database."""
    username: str
    credential_id: int
    route_id: int
    tenant_id: int
    broker_party_id: Optional[int]
    # The channel's folder. Signed in, the login sees two folders of it — its
    # `incoming` (upload) and `outbound` (Kavachio's replies) — and nothing else.
    folder: Path


def is_hosted(route) -> bool:
    """An SFTP channel on Kavachio's own server — the kind that has a login."""
    return (getattr(route, "channel", None) == "sftp"
            and not svc.is_external_sftp(route))


def new_password() -> str:
    """Kq7vX-m2pRt-9wHb4-nZc6d"""
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(5))
                    for _ in range(4))


def fingerprint(username: str, password: str) -> str:
    from intake_auth import hash_key
    return hash_key(f"sftp:{username}:{password}")


def _base_username(broker_name: str, carrier_name: str) -> str:
    """"halsteadinsurisk": the broker first — it is their login — then the
    carrier, so a broker who sends to two carriers can tell the two apart.
    Letters and digits only (see _NEW_USER_RE)."""
    b = svc.slugify(broker_name).replace("-", "")[:18]
    c = svc.slugify(carrier_name).replace("-", "")[:10]
    base = f"{b}{c}" or "broker"
    if not base[0].isalpha():
        base = f"b{base}"
    return base[:28]


def _free_username(s, base: str, route_id: int) -> str:
    """A user name nobody on the server has — across EVERY carrier, because
    one server signs everybody in. The table's unique key_prefix backs it."""
    def taken(u: str) -> bool:
        return s.query(IntakeCredential.id).filter(
            IntakeCredential.key_prefix == u).first() is not None

    tail = str(route_id)
    for u in (base, f"{base[:32 - len(tail)]}{tail}"):
        if _NEW_USER_RE.match(u) and not taken(u):
            return u
    while True:
        u = f"{base[:24]}{secrets.randbelow(10**6):06d}"
        if _NEW_USER_RE.match(u) and not taken(u):
            return u


def broker_still_on(s, route) -> bool:
    """Is the channel's broker still on what the channel is for — its
    programme, or (a channel for all their programmes) on at least one?
    A broker taken off (hierarchy_routes.programme_broker_remove) cannot sign
    in; added back, they can again, with the same login."""
    if not route.broker_party_id:
        return False
    if route.program_id is not None:
        return svc.broker_on_programme(s, route.broker_party_id, route.program_id)
    return bool(svc.broker_programmes(s, route.tenant_id, route.broker_party_id))


def drop_removed(tenant_id: int, broker_party_id: int) -> int:
    """Disconnect a broker who has just been taken off a programme from every
    channel of theirs on Kavachio's server that no longer lets them in. Their
    next sign-in is refused by verify(). Returns how many sessions closed."""
    import sftp_server
    closed = 0
    with SessionLocal() as s:
        routes = (s.query(IntakeRoute)
                  .filter(IntakeRoute.tenant_id == tenant_id,
                          IntakeRoute.broker_party_id == broker_party_id,
                          IntakeRoute.channel == "sftp").all())
        for route in routes:
            if is_hosted(route) and not broker_still_on(s, route):
                closed += sftp_server.drop_sessions(route.id)
    return closed


def credential(s, route_id: int) -> Optional[IntakeCredential]:
    """The channel's login, live or not. API keys are only ever made for API
    channels (create_key refuses anything else), so a credential on an SFTP
    channel IS its login."""
    return (s.query(IntakeCredential)
            .filter(IntakeCredential.route_id == route_id)
            .order_by(IntakeCredential.id.desc()).first())


def logins_for_tenant(s, tenant_id: int) -> dict[int, IntakeCredential]:
    """{route_id: login} for every SFTP channel on Kavachio's server — one query."""
    rows = (s.query(IntakeCredential)
            .join(IntakeRoute, IntakeRoute.id == IntakeCredential.route_id)
            .filter(IntakeRoute.tenant_id == tenant_id,
                    IntakeRoute.channel == "sftp",
                    ~IntakeRoute.address.like("sftp://%"))
            .order_by(IntakeCredential.id).all())
    return {c.route_id: c for c in rows}


def issue(s, route, *, broker_name: str = "", carrier_name: str = "",
          user_id: Optional[int] = None) -> tuple[IntakeCredential, str]:
    """Give the channel a login, or a new password for the one it has.

    Returns (credential, password). The password is in plaintext ONLY here —
    send it, and never store or log it. The caller commits."""
    if not is_hosted(route):
        raise ValueError("only an SFTP channel on Kavachio's own server has a login")
    password = new_password()
    # Explicitly timezone-aware: a naive utcnow() is read as local time by a
    # timestamptz column on a database that is not set to UTC.
    now = datetime.now(timezone.utc)
    cred = credential(s, route.id)
    if cred is None:
        username = _free_username(s, _base_username(broker_name, carrier_name), route.id)
        cred = IntakeCredential(
            route_id=route.id, tenant_id=route.tenant_id, key_prefix=username,
            key_hash=fingerprint(username, password), last4=None, label=LABEL,
            created_by_user_id=user_id, created_at=now)
        s.add(cred)
    else:
        cred.key_hash = fingerprint(cred.key_prefix, password)
        cred.revoked_at = None
        # "Issued" means this password. Signed in "never" with it is exactly
        # what a carrier wants to see after sending a broker a new one.
        cred.created_at = now
        cred.created_by_user_id = user_id
        cred.last_used_at = None
    # The folder has to exist before the broker's first sign-in lands in it.
    svc.ensure_route_dirs(route)
    s.flush()
    return cred, password


def verify(username: str, password: str, ip: Optional[str]) -> Optional[Login]:
    """A sign-in on Kavachio's SFTP server: the Login, or None.

    Every refusal is the same None — unknown user, wrong password, revoked,
    switched off, broker taken off the programme, wrong address — so a caller
    cannot tell which guesses are getting warmer. Raises only when the database itself cannot be reached;
    that is not the broker's fault and must not count against them."""
    from intake_auth import _ip_allowed

    user = (username or "").strip().lower()
    # Computed whether or not the user exists, so an unknown name takes as
    # long to refuse as a wrong password.
    candidate = fingerprint(user, password or "")
    if not _USER_RE.match(user) or not password:
        return None
    with SessionLocal() as s:
        row = (s.query(IntakeCredential, IntakeRoute)
               .join(IntakeRoute, IntakeRoute.id == IntakeCredential.route_id)
               .filter(IntakeCredential.key_prefix == user,
                       IntakeRoute.channel == "sftp").first())
        if row is None:
            return None
        cred, route = row
        if not hmac.compare_digest(cred.key_hash or "", candidate):
            return None
        now = datetime.now(timezone.utc)
        if (not is_hosted(route) or cred.revoked_at is not None
                or (cred.expires_at is not None and cred.expires_at < now)
                or not _ip_allowed(ip, cred.ip_allowlist)
                or not route.is_enabled
                or not broker_still_on(s, route)):
            return None
        # The address is generated ("carrier/broker"), never typed — but a
        # folder outside SFTP_ROOT is refused rather than trusted.
        root = svc.sftp_root()
        folder = (root / route.address).resolve()
        if folder == root or root not in folder.parents:
            log.error("sftp: route %s has an address outside SFTP_ROOT — refused", route.id)
            return None
        svc.ensure_route_dirs(route)
        login = Login(username=user, credential_id=cred.id, route_id=route.id,
                      tenant_id=route.tenant_id, broker_party_id=route.broker_party_id,
                      folder=folder)
        # Throttled like an API key's: a broker's job that signs in every five
        # minutes should not turn this row into a write hotspot.
        if cred.last_used_at is None or (now - cred.last_used_at) > timedelta(minutes=1):
            cred.last_used_at = now
            s.commit()
        return login
