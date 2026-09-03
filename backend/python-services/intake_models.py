"""ORM for the two intake tables (feature 10 — File Intake Channels).

Both tables ALREADY EXIST in the canonical schema; this module only maps them,
so there is no migration and nothing else in the app changes. They live here
rather than in db.py so adding intake touches no existing file.

  intake_route   one row per "way in" a broker uses. The route is what turns a
                 file that arrived with nobody logged in into a known broker:
                 the folder it landed in IS the identity.
  file_arrival   one row per file that reached us, however it reached us —
                 including the ones we refused. Nothing is ever silently
                 dropped, so a refused file still gets a row.

Constraints worth knowing (they are enforced by the database, not here):
  intake_route.channel     upload | email | sftp | api | cloud_folder
  intake_route.file_style  whole_book | changes_only          (NOT NULL)
  UNIQUE (tenant_id, channel, address)
  file_arrival.outcome     accepted | turned_away   ← only two values
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String, Text,
)
from sqlalchemy.dialects.postgresql import JSONB

from db import Base


class IntakeRoute(Base):
    """A way in, belonging to one broker.

    `address` holds the PATH ONLY (e.g. "insurisk/corvin"), never the full
    sftp:// URL. The host is deployment config and changes between environments;
    baking it into every row would make every address stale the day the host
    moves. The API composes the display URL from SFTP_HOST at read time.
    """
    __tablename__ = "intake_route"

    id = Column("route_id", BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, ForeignKey("tenant.tenant_id"), nullable=False, index=True)
    broker_party_id = Column(BigInteger, ForeignKey("party.party_id"), nullable=True, index=True)
    channel = Column(Text, nullable=False)          # upload|email|sftp|api|cloud_folder
    address = Column(Text, nullable=False)
    display_name = Column(Text, nullable=True)
    is_enabled = Column(Boolean, nullable=False, default=True)
    # What a normal file from this broker looks like. Not a correctness switch —
    # rows already loaded are recognised either way — it only tells the checks
    # what to expect, so an incremental file is not read as a shrunken book.
    file_style = Column(Text, nullable=False, default="whole_book")
    # Order the collector tries a broker's routes in. Only meaningful for routes
    # we PULL from (a folder we look in); a broker who pushes chose already.
    fallback_rank = Column(Integer, nullable=True)
    # Feature 10.2 — which programme this route is for. NULL = broker-wide (the
    # old behaviour, and every existing SFTP route): the broker is known but the
    # programme is not, so a broker on two programmes cannot be resolved. Set it
    # and the route is pinned, which is the recommended shape for an API key —
    # one key per (programme, broker) pair, nothing for the sender to supply.
    program_id = Column(BigInteger, ForeignKey("program.program_id"), nullable=True, index=True)
    note = Column(Text, nullable=True)
    created_by_user_id = Column(BigInteger, ForeignKey("app_user.user_id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    disabled_at = Column(DateTime(timezone=True), nullable=True)
    disabled_by_user_id = Column(BigInteger, ForeignKey("app_user.user_id"), nullable=True)


class FileArrival(Base):
    """One file that reached us — accepted or refused.

    `outcome` has only two permitted values in the database. The design also
    describes a third state ("Held, someone decides" — a suspected duplicate, an
    empty file, a file with no live contract yet). There is no value for it, so
    those land as `turned_away` with a reason that says they are held. Adding a
    real 'held' value needs a one-line migration relaxing the CHECK; see the
    note in intake_service.CHECKS.
    """
    __tablename__ = "file_arrival"

    id = Column("arrival_id", BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, ForeignKey("tenant.tenant_id"), nullable=False, index=True)
    route_id = Column(BigInteger, ForeignKey("intake_route.route_id"), nullable=True, index=True)
    matched_broker_party_id = Column(BigInteger, ForeignKey("party.party_id"), nullable=True)
    # Who the sender CLAIMED to be, before we matched them — the From: address on
    # an email, the SSH user on SFTP. Kept even when it matches nobody, because
    # "who tried?" is the first question asked about a refused file.
    claimed_sender = Column(Text, nullable=True)
    filename = Column(Text, nullable=False)
    file_size_bytes = Column(BigInteger, nullable=True)
    file_hash_sha256 = Column(Text, nullable=True, index=True)
    received_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    outcome = Column(Text, nullable=False)          # accepted | turned_away
    turned_away_reason = Column(Text, nullable=True)
    sender_notified_at = Column(DateTime(timezone=True), nullable=True)
    sender_notified_via = Column(Text, nullable=True)
    bdx_upload_id = Column(BigInteger, nullable=True)
    # Feature 10.2 — the reference a partner quotes back at us. Never hand out
    # arrival_id: a sequential integer lets anyone count other people's traffic.
    public_ref = Column(Text, nullable=True)
    # Set by an API caller; unique per tenant, so a retrying cron job gets the
    # original receipt back instead of loading the same month twice.
    idempotency_key = Column(Text, nullable=True)
    blob_ref = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class IntakeCredential(Base):
    """An API key, bound to one intake_route (feature 10.2).

    SFTP identifies a broker by which folder the file landed in — the folder IS
    the identity. An API caller has no folder, so the key does that job: it
    points at a route, and the route already carries the broker and (since the
    10.2 migration) the programme. Nothing about who is sending ever comes from
    the request body.

    The key itself is NEVER stored. `key_hash` is HMAC-SHA256(pepper, key) with
    the pepper held outside the database, so a dump alone yields no working
    keys; `key_prefix` is the indexed handle parsed out of the request header.
    Revoked, never deleted — arrivals point at the credential that carried them.
    """
    __tablename__ = "intake_credential"

    id = Column("credential_id", BigInteger, primary_key=True)
    route_id = Column(BigInteger, ForeignKey("intake_route.route_id"),
                      nullable=False, index=True)
    tenant_id = Column(BigInteger, ForeignKey("tenant.tenant_id"), nullable=False)
    key_prefix = Column(Text, nullable=False, unique=True)
    key_hash = Column(Text, nullable=False)
    last4 = Column(Text, nullable=True)          # all the UI ever shows
    label = Column(Text, nullable=True)          # "Halstead nightly job"
    ip_allowlist = Column(JSONB, nullable=True)  # CIDR list; NULL = any source
    expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_by_user_id = Column(BigInteger, ForeignKey("app_user.user_id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
