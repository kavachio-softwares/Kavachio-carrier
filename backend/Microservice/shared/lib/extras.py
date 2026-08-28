"""User-defined "extra field" definitions and value resolution.

No new tables. Three existing JSONB stores power the feature:

  • `tenant.internal_codes` (canonical) — per-tenant dict at sub-key `extras`
    holds the definitions:
        {"extras": {
            "tria_premium": {
                "display_name": "TRIA Premium",
                "description":  "Terrorism Risk Insurance Act premium portion",
                "data_type":    "currency",
                "shared":       False,
                "created_at":   "2026-…",
                "adopted_from_tenant_id": null
            },
            …
        }}

  • `column_mapping_fingerprint.canonical_mapping` (canonical) — same dict the
    cache already uses, with extra entries prefixed `_xf:`:
        {"POL Data": {
            "_xf:tria_premium": "POL Data :: TRIAPremium",
            "policy_number":   "POL Data :: PolicyNumber",
            …
        }}

  • `<entity>.extras` JSONB columns (canonical: policy, claim, coverage,
    premium_transaction, insured_location, building) — actual ingested
    values, one JSON object per entity row.

This module wraps reads/writes against those three stores.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

log = logging.getLogger("bdx.extras")

XF_PREFIX = "_xf:"
DEFAULT_ENTITY = "policy"
ALLOWED_ENTITIES = (
    "policy", "claim", "coverage", "premium_transaction",
    "insured_location", "building",
)
ALLOWED_DATA_TYPES = ("string", "number", "currency", "date", "bool")


def is_extras_key(canonical: str) -> bool:
    return isinstance(canonical, str) and canonical.startswith(XF_PREFIX)


def parse_xf_key(canonical: str) -> tuple[str, str]:
    """`_xf:tria_premium` → ('policy', 'tria_premium').
    `_xf:claim:tpa_ref` → ('claim',  'tpa_ref').
    Returns (entity, key). Entity defaults to 'policy' for backward compat."""
    body = canonical[len(XF_PREFIX):] if is_extras_key(canonical) else canonical
    if ":" in body:
        ent, _, key = body.partition(":")
        ent = ent.strip().lower()
        if ent not in ALLOWED_ENTITIES:
            ent = DEFAULT_ENTITY
        return ent, key.strip()
    return DEFAULT_ENTITY, body.strip()


def make_xf_key(entity: str, key: str) -> str:
    ent = entity if entity in ALLOWED_ENTITIES else DEFAULT_ENTITY
    return f"{XF_PREFIX}{ent}:{normalise_key(key)}" if ent != DEFAULT_ENTITY \
        else f"{XF_PREFIX}{normalise_key(key)}"


def strip_prefix(canonical: str) -> str:
    return canonical[len(XF_PREFIX):] if is_extras_key(canonical) else canonical


def normalise_key(raw: str) -> str:
    """snake_case key suitable for use as a JSON property."""
    k = re.sub(r"[^a-z0-9_]+", "_", str(raw or "").strip().lower()).strip("_")
    if not k:
        k = "extra"
    if k[0].isdigit():
        k = f"_{k}"
    return k


# ---- definition store -----------------------------------------------------

def _tenant_table():
    from canonical import CANONICAL_TABLES
    return CANONICAL_TABLES.get("tenant")


def _get_tenant_row(session: Session, tenant_id: int):
    t = _tenant_table()
    if t is None:
        return None
    return session.execute(
        select(t).where(t.c.tenant_id == tenant_id)
    ).fetchone()


def _read_extras_dict(session: Session, tenant_id: int) -> dict:
    row = _get_tenant_row(session, tenant_id)
    if not row:
        return {}
    codes = row._mapping.get("internal_codes")
    # internal_codes is sometimes stored as a JSON list (not a dict) — guard so a
    # malformed tenant row doesn't 500 the whole endpoint.
    if not isinstance(codes, dict):
        return {}
    return dict(codes.get("extras") or {})


def _write_extras_dict(session: Session, tenant_id: int, extras: dict) -> None:
    t = _tenant_table()
    if t is None:
        return
    row = _get_tenant_row(session, tenant_id)
    existing = row._mapping.get("internal_codes") if row else None
    # Preserve a dict's other keys; replace a non-dict (list/null) wholesale.
    codes = dict(existing) if isinstance(existing, dict) else {}
    codes["extras"] = extras
    session.execute(
        update(t).where(t.c.tenant_id == tenant_id)
        .values(internal_codes=codes, modified_at=datetime.utcnow())
    )


def list_definitions(session: Session, tenant_id: int,
                     include_shared: bool = True) -> dict[str, dict]:
    """Return the dict of extras visible to `tenant_id` — own + (optionally)
    shared definitions from any other tenant. Keys are deduped (own wins)."""
    own = _read_extras_dict(session, tenant_id)
    if not include_shared:
        return dict(own)
    out = dict(own)
    # Scan all tenants for shared extras.
    t = _tenant_table()
    if t is None:
        return out
    others = session.execute(
        select(t.c.tenant_id, t.c.internal_codes).where(t.c.tenant_id != tenant_id)
    ).fetchall()
    for tid, codes in others:
        # A tenant's internal_codes may be a JSON list (or null) rather than the
        # expected dict — skip those instead of raising on .get().
        if not isinstance(codes, dict):
            continue
        for key, definition in (codes.get("extras") or {}).items():
            if not isinstance(definition, dict):
                continue
            if not definition.get("shared"):
                continue
            if key in out:
                continue
            out[key] = {**definition, "_origin_tenant_id": tid}
    return out


def upsert_definition(session: Session, tenant_id: int, key: str,
                      definition: dict) -> dict:
    """Insert or update a definition on the tenant's internal_codes.extras."""
    extras = _read_extras_dict(session, tenant_id)
    key = normalise_key(key)
    existing = extras.get(key) or {}
    merged = {
        "display_name": definition.get("display_name") or existing.get("display_name") or key,
        "description":  definition.get("description")  or existing.get("description")  or "",
        "data_type":    definition.get("data_type")    or existing.get("data_type")    or "string",
        "shared":       bool(definition.get("shared", existing.get("shared", False))),
        "created_at":   existing.get("created_at") or datetime.utcnow().isoformat(),
        "modified_at":  datetime.utcnow().isoformat(),
        "adopted_from_tenant_id":
            definition.get("adopted_from_tenant_id")
            or existing.get("adopted_from_tenant_id"),
    }
    if merged["data_type"] not in ALLOWED_DATA_TYPES:
        merged["data_type"] = "string"
    extras[key] = merged
    _write_extras_dict(session, tenant_id, extras)
    return {key: merged}


def adopt_definition(session: Session, tenant_id: int, key: str) -> dict | None:
    """Copy a shared definition (from any tenant) into this tenant's extras."""
    visible = list_definitions(session, tenant_id, include_shared=True)
    if key not in visible:
        return None
    def_ = dict(visible[key])
    origin = def_.pop("_origin_tenant_id", None)
    def_["adopted_from_tenant_id"] = origin
    def_["shared"] = False  # the local copy isn't automatically re-shared
    extras = _read_extras_dict(session, tenant_id)
    extras[key] = def_
    _write_extras_dict(session, tenant_id, extras)
    return {key: def_}


# ---- value coercion --------------------------------------------------------

def coerce(value: Any, data_type: str) -> Any:
    """Cast a raw cell value to the type the definition declares. Failures
    are non-fatal — original string is returned as a fall-through."""
    if value is None or value == "":
        return None
    t = (data_type or "string").lower()
    try:
        if t == "number":   return float(value)
        if t == "currency": return float(str(value).replace(",", "").replace("$", ""))
        if t == "bool":     return str(value).strip().lower() in ("true", "1", "y", "yes")
        if t == "date":
            import pandas as pd
            return pd.to_datetime(value).date().isoformat()
    except Exception:
        return str(value)
    return str(value)
