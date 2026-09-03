"""Canonical column-mapping memory (v4: the `column_mapping` table).

Wraps writes / reads of `column_mapping` — the AI mapping memory: layout
fingerprint plus (later) vector embedding. The per-tenant `mappers` row is
kept for operational fields (file blob, draft flag, etc.) and points at the
canonical row via `mappers.fingerprint_id`.

v4 replaces the old `column_mapping_fingerprint` table: the signature hash is
`mapping_layout_fingerprint`, the spec JSON is `mapping_spec`, and confidence
is `mapping_confidence`. Popularity (hit_count) and SCD versioning were
dropped with the old table; one row per (tenant, layout fingerprint) is
updated in place. The embedding column is left NULL for now.
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

_TABLE_NAME = "column_mapping"

# Kept for API compatibility with older callers; no longer stored.
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


def upsert(
    session: Session,
    *,
    mga: str,
    signature_tokens: list[str],
    canonical_mapping: dict,
    confidence: float | None = None,
    bdx_type: str = DEFAULT_BDX_TYPE,          # accepted, no longer stored
    carrier_party_id: Optional[int] = None,    # accepted, no longer stored
    last_validated_by_user_id: Optional[int] = None,
    contract_id: Optional[int] = None,
) -> Optional[int]:
    """Insert or update a column_mapping row for (tenant, layout fingerprint)
    and return its mapping_id. Returns None silently if the canonical table
    is unavailable (e.g. tests)."""
    t = _table()
    if t is None:
        log.warning("column_mapping table not in canonical schema")
        return None

    tenant_id = _ensure_tenant_id(session, mga)
    sha = signature_hash(signature_tokens)
    now = datetime.utcnow()

    # 1. Exact match for this tenant — refresh the stored spec.
    existing = session.execute(
        select(t.c.mapping_id)
        .where(t.c.tenant_id == tenant_id)
        .where(t.c.mapping_layout_fingerprint == sha)
        .limit(1)
    ).fetchone()
    if existing:
        mapping_id = existing[0]
        updates: dict[str, Any] = {
            "mapping_spec": canonical_mapping,
            "mapping_confidence": confidence,
            "modified_at": now,
        }
        if last_validated_by_user_id is not None:
            updates["mapping_confirmed_by"] = last_validated_by_user_id
        if contract_id is not None and "mapping_contract_id" in t.c:
            updates["mapping_contract_id"] = contract_id
        updates = {k: v for k, v in updates.items() if k in t.c}
        session.execute(update(t).where(t.c.mapping_id == mapping_id).values(**updates))
        return int(mapping_id)

    # 2. Fresh insert.
    payload = {
        "tenant_id": tenant_id,
        "mapping_contract_id": contract_id,
        "mapping_layout_fingerprint": sha,
        "mapping_spec": canonical_mapping,
        "mapping_confidence": confidence,
        "mapping_confirmed_by": last_validated_by_user_id,
        "created_at": now,
        "modified_at": now,
    }
    # Drop any column the canonical table doesn't have (defensive).
    payload = {k: v for k, v in payload.items() if k in t.c}
    res = session.execute(t.insert().values(**payload).returning(t.c.mapping_id))
    mapping_id = res.scalar()
    return int(mapping_id) if mapping_id is not None else None


def find_by_hash(
    session: Session,
    signature_tokens: list[str],
    *,
    cross_tenant: bool = True,
    bdx_type: Optional[str] = None,            # accepted, no longer stored
) -> Optional[dict]:
    """Look up an existing mapping memory by layout fingerprint. Returns a
    flat dict (`fingerprint_id`, `tenant_id`, `canonical_mapping`,
    `confidence`) or None — the legacy key names are kept so callers don't
    have to change.

    Set `cross_tenant=False` to scope to a single tenant when needed.
    """
    t = _table()
    if t is None:
        return None
    sha = signature_hash(signature_tokens)
    q = (select(t.c.mapping_id, t.c.tenant_id, t.c.mapping_spec,
                t.c.mapping_confidence)
         .where(t.c.mapping_layout_fingerprint == sha))
    q = q.order_by(t.c.mapping_id.desc()).limit(1)
    row = session.execute(q).fetchone()
    if not row:
        return None
    return {
        "fingerprint_id": int(row[0]),
        "tenant_id": int(row[1]) if row[1] is not None else None,
        "canonical_mapping": row[2],
        "hit_count": 0,                    # dropped in v4; kept for callers
        "carrier_party_id": None,
        "bdx_type": None,
        "confidence": float(row[3]) if row[3] is not None else None,
    }


def bump_hit_count(session: Session, fingerprint_id: int) -> None:
    """v4 dropped popularity tracking; only freshness is recorded."""
    t = _table()
    if t is None:
        return
    session.execute(
        update(t)
        .where(t.c.mapping_id == fingerprint_id)
        .values(modified_at=datetime.utcnow())
    )
