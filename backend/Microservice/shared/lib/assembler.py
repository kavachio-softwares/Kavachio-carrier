"""Read canonical tables and reassemble per-policy JSON for /dwh."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from canonical import CANONICAL_TABLES

POLICY_CHILDREN = (
    "coverage", "premium_transaction", "premium_invoice",
    "insured_location", "policy_attributes",
    "claim", "parametric_coverage_detail", "party_role_in_policy",
)

# Tables linked to premium_transaction.transaction_id
TRANSACTION_CHILDREN = ("policy_fee", "tax_or_surcharge", "commission")


def _row_to_dict(row, table) -> dict:
    return {col.name: row._mapping[col.name] for col in table.c
            if row._mapping[col.name] is not None}


from scd2_sql import VERSIONED_TABLES


def _active(t):
    """SCD-2 filter: keep only the CURRENT version of a row.

    `is_current_version IS NOT FALSE` treats NULL (legacy, never-versioned rows)
    as current, so this is a no-op until a 'Modify here' edit has versioned a
    row — then the superseded version (is_current_version = FALSE) is hidden.

    Gated to VERSIONED_TABLES: only those pure-canonical tables physically have
    the is_current_version column. Other names (e.g. `program`, `party_contact`)
    are physically backed by an OPS table that has no such column, so filtering
    them would generate SQL referencing a non-existent column."""
    if t.name in VERSIONED_TABLES and "is_current_version" in t.c:
        return t.c.is_current_version.isnot(False)
    return None


def _sel(t, *where):
    """select(t) constrained to the given clauses AND the active-version filter."""
    stmt = select(t)
    for w in where:
        if w is not None:
            stmt = stmt.where(w)
    active = _active(t)
    if active is not None:
        stmt = stmt.where(active)
    return stmt


def fetch_policy(session: Session, policy_id: int) -> dict:
    """Return one fully-assembled policy: scalar parents + child collections.

    Reads only the CURRENT SCD-2 version of every row (see `_active`)."""
    out: dict[str, Any] = {}

    policy_t = CANONICAL_TABLES["policy"]
    pol_row = session.execute(_sel(policy_t, policy_t.c.policy_id == policy_id)).fetchone()
    if not pol_row:
        return out
    out["policy"] = _row_to_dict(pol_row, policy_t)

    # Surface related-party NAMES onto the policy dict so output templates can
    # resolve `insured_legal_name` / `carrier_legal_name`. These are synthetic
    # policy "columns": the names actually live on the party table (referenced
    # via insured_party_id / writing_company / risk_bearing_carrier FKs).
    if "party" in CANONICAL_TABLES:
        pt = CANONICAL_TABLES["party"]

        def _party_name(pid):
            if not pid:
                return None
            r = session.execute(
                select(pt.c.legal_name).where(pt.c.party_id == pid)
            ).fetchone()
            return r[0] if r else None

        ins_name = _party_name(out["policy"].get("insured_party_id"))
        car_name = _party_name(
            out["policy"].get("writing_company_party_id")
            or out["policy"].get("risk_bearing_carrier_party_id")
        )
        if ins_name:
            out["policy"]["insured_legal_name"] = ins_name
        if car_name:
            out["policy"]["carrier_legal_name"] = car_name

    # Scalar parent: program (via policy.program_id)
    program_id = out["policy"].get("program_id")
    if program_id and "program" in CANONICAL_TABLES:
        pt = CANONICAL_TABLES["program"]
        prog_row = session.execute(_sel(pt, pt.c.program_id == program_id)).fetchone()
        if prog_row:
            out["program"] = _row_to_dict(prog_row, pt)

    # Children with policy_id FK
    location_ids: list[int] = []
    txn_ids: list[int] = []
    for table_name in POLICY_CHILDREN:
        t = CANONICAL_TABLES.get(table_name)
        if t is None or "policy_id" not in t.c:
            continue
        rows = session.execute(_sel(t, t.c.policy_id == policy_id)).fetchall()
        if not rows:
            continue
        out[table_name] = [_row_to_dict(r, t) for r in rows]
        if table_name == "insured_location":
            location_ids = [r._mapping["location_id"] for r in rows
                            if "location_id" in r._mapping]
        elif table_name == "premium_transaction":
            txn_ids = [r._mapping["transaction_id"] for r in rows
                       if "transaction_id" in r._mapping]

    # Children hanging off premium_transaction.transaction_id
    if txn_ids:
        for table_name in TRANSACTION_CHILDREN:
            t = CANONICAL_TABLES.get(table_name)
            if t is None or "transaction_id" not in t.c:
                continue
            rows = session.execute(_sel(t, t.c.transaction_id.in_(txn_ids))).fetchall()
            if rows:
                out[table_name] = [_row_to_dict(r, t) for r in rows]

    # Buildings (location_id FK)
    if location_ids and "building" in CANONICAL_TABLES:
        bt = CANONICAL_TABLES["building"]
        if "location_id" in bt.c:
            b_rows = session.execute(_sel(bt, bt.c.location_id.in_(location_ids))).fetchall()
            if b_rows:
                out["building"] = [_row_to_dict(r, bt) for r in b_rows]

    # Party-side rows attached to the policy's ambient party (created by the
    # ingester so party_address/contact/license rows have a real FK to hang on).
    polno = out["policy"].get("policy_number")
    if polno and "party" in CANONICAL_TABLES:
        pt = CANONICAL_TABLES["party"]
        ambient = session.execute(
            select(pt.c.party_id).where(pt.c.party_natural_id == f"ambient::{polno}")
        ).fetchone()
        if ambient:
            party_id = ambient[0]
            # party_contact is assembled for export but is NOT in VERSIONED_TABLES
            # (it's shadowed by an ops table with no SCD columns) — _sel leaves it
            # unfiltered and 'Modify here' reports its fields as not-editable.
            for table_name in ("party_address", "party_contact", "party_license"):
                t = CANONICAL_TABLES.get(table_name)
                if t is None or "party_id" not in t.c:
                    continue
                rows = session.execute(_sel(t, t.c.party_id == party_id)).fetchall()
                if rows:
                    out[table_name] = [_row_to_dict(r, t) for r in rows]

    return out


def fetch_policies(session: Session, policy_ids: list[int]) -> list[dict]:
    """Bulk variant; preserves order of policy_ids."""
    return [fetch_policy(session, pid) for pid in policy_ids if pid is not None]
