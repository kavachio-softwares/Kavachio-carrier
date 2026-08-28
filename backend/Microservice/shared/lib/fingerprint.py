"""Canonical column-mapping fingerprint store.

Wraps writes / reads of `kavachio.column_mapping_fingerprint` (the table
defined in data_model.py). The current per-tenant `mappers` row is kept
for operational fields (file blob, draft flag, etc.) and points at the
canonical fingerprint via `mappers.fingerprint_id`.

This is the cross-tenant, SCD-versioned, popularity-tracked home for
column-mapping specs. Down the line it is the place where pgvector
similarity search will live; the embedding column is left NULL for now.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from canonical import CANONICAL_TABLES

log = logging.getLogger("bdx.fingerprint")

_TABLE_NAME = "column_mapping_fingerprint"

# Default bdx_type when none is supplied. Keeps the column NOT NULL satisfied
# without forcing the rest of the app to thread the value through yet.
DEFAULT_BDX_TYPE = "premium_program"


def signature_hash(signature_tokens: list[str]) -> str:
    """SHA-256 of the canonicalised signature. `signature_tokens` is the
    sorted lowercase ["sheet :: column", …] list we already produce in
    mapper.signature_multi."""
    canon = "\n".join(sorted(t.strip() for t in signature_tokens if t and t.strip()))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _table() -> Any | None:
    return CANONICAL_TABLES.get(_TABLE_NAME)


def _ensure_tenant_id(session: Session, mga: str) -> int:
    """Reuse ingester._ensure_tenant if available; otherwise inline the
    lookup so this module doesn't hard-depend on the ingestion path."""
    from ingester import _ensure_tenant
    return _ensure_tenant(session, mga)


def _ensure_default_carrier(session: Session, tenant_id: int) -> int | None:
    """Canonical `column_mapping_fingerprint.carrier_party_id` is NOT NULL
    in the actual Postgres schema. When the caller didn't supply a carrier
    (most onboarding flows don't know it yet), reuse the placeholder party
    the ingester already creates per tenant."""
    try:
        from ingester import _ensure_carrier_party
        return _ensure_carrier_party(session, tenant_id)
    except Exception as e:
        log.warning("Could not ensure default carrier party for tenant %s: %s",
                    tenant_id, e)
        return None


def upsert(
    session: Session,
    *,
    mga: str,
    signature_tokens: list[str],
    canonical_mapping: dict,
    confidence: float | None = None,
    bdx_type: str = DEFAULT_BDX_TYPE,
    carrier_party_id: Optional[int] = None,
    last_validated_by_user_id: Optional[int] = None,
) -> Optional[int]:
    """Insert or update a fingerprint row for (tenant, signature_hash) and
    return its fingerprint_id. Bumps hit_count when an existing row matches.
    Returns None silently if the canonical table is unavailable (e.g. tests)."""
    t = _table()
    if t is None:
        log.warning("column_mapping_fingerprint table not in canonical schema")
        return None

    tenant_id = _ensure_tenant_id(session, mga)
    if carrier_party_id is None:
        carrier_party_id = _ensure_default_carrier(session, tenant_id)
    sha = signature_hash(signature_tokens)
    content_sha = hashlib.sha256(
        repr(sorted(canonical_mapping.items())).encode("utf-8")
    ).hexdigest()
    now = datetime.utcnow()

    # 1. Exact match for this tenant — bump hit_count + refresh content.
    existing = session.execute(
        select(t.c.fingerprint_id, t.c.content_fingerprint)
        .where(t.c.tenant_id == tenant_id)
        .where(t.c.column_signature_hash == sha)
        .where(t.c.is_current_version == True)  # noqa: E712
        .limit(1)
    ).fetchone()
    if existing:
        fp_id, prev_content = existing
        updates: dict[str, Any] = {
            "hit_count": (t.c.hit_count + 1),
            "modified_at": now,
        }
        if prev_content != content_sha:
            # Spec changed — refresh canonical_mapping + content fingerprint.
            updates.update({
                "canonical_mapping": canonical_mapping,
                "content_fingerprint": content_sha,
                "confidence": confidence,
                "last_validated_by_user_id": last_validated_by_user_id,
            })
        session.execute(update(t).where(t.c.fingerprint_id == fp_id).values(**updates))
        return int(fp_id)

    # 2. Fresh insert.
    payload = {
        "tenant_id": tenant_id,
        "carrier_party_id": carrier_party_id,
        "bdx_type": bdx_type,
        "column_signature_hash": sha,
        "canonical_mapping": canonical_mapping,
        "confidence": confidence,
        "last_validated_by_user_id": last_validated_by_user_id,
        "hit_count": 1,
        "is_current_version": True,
        "entity_fingerprint": sha,        # the signature IS the entity key
        "content_fingerprint": content_sha,
        "valid_from": now,
        "valid_until": datetime(2999, 12, 31),
        "created_at": now,
        "modified_at": now,
    }
    # Drop any column the canonical table doesn't have (defensive).
    payload = {k: v for k, v in payload.items() if k in t.c}
    res = session.execute(t.insert().values(**payload).returning(t.c.fingerprint_id))
    fp_id = res.scalar()
    return int(fp_id) if fp_id is not None else None


def find_by_hash(
    session: Session,
    signature_tokens: list[str],
    *,
    cross_tenant: bool = True,
    bdx_type: Optional[str] = None,
) -> Optional[dict]:
    """Look up an existing fingerprint by hash. Returns a flat dict
    (`fingerprint_id`, `tenant_id`, `canonical_mapping`, `hit_count`,
    `carrier_party_id`, `bdx_type`) or None.

    Set `cross_tenant=False` to scope to a single tenant when needed.
    """
    t = _table()
    if t is None:
        return None
    sha = signature_hash(signature_tokens)
    q = (select(t.c.fingerprint_id, t.c.tenant_id, t.c.canonical_mapping,
                 t.c.hit_count, t.c.carrier_party_id, t.c.bdx_type,
                 t.c.confidence)
         .where(t.c.column_signature_hash == sha)
         .where(t.c.is_current_version == True))  # noqa: E712
    if bdx_type:
        q = q.where(t.c.bdx_type == bdx_type)
    q = q.order_by(t.c.hit_count.desc(), t.c.fingerprint_id.desc()).limit(1)
    row = session.execute(q).fetchone()
    if not row:
        return None
    return {
        "fingerprint_id": int(row[0]),
        "tenant_id": int(row[1]),
        "canonical_mapping": row[2],
        "hit_count": int(row[3] or 0),
        "carrier_party_id": row[4],
        "bdx_type": row[5],
        "confidence": float(row[6]) if row[6] is not None else None,
    }


def bump_hit_count(session: Session, fingerprint_id: int) -> None:
    t = _table()
    if t is None:
        return
    session.execute(
        update(t)
        .where(t.c.fingerprint_id == fingerprint_id)
        .values(hit_count=t.c.hit_count + 1, modified_at=datetime.utcnow())
    )
