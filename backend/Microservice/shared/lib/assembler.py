"""Read canonical tables and reassemble per-policy JSON for /dwh (v4 model)."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from canonical import CANONICAL_TABLES

POLICY_CHILDREN = ("coverage", "premium_transaction", "risk_location", "claim")

# Tables linked to premium_transaction via <table>_premium_transaction_id
TRANSACTION_CHILDREN = ("tax_line", "commission_line")

# Tables linked to claim via their claim FK
CLAIM_CHILDREN = ("claim_transaction", "claim_fee_line", "claim_reserve")

# child table → its FK column pointing at the parent
_FK = {
    "coverage": "coverage_policy_id",
    "premium_transaction": "premium_transaction_policy_id",
    "risk_location": "risk_location_policy_id",
    "claim": "claim_policy_id",
    "tax_line": "tax_line_premium_transaction_id",
    "commission_line": "commission_line_premium_transaction_id",
    "claim_transaction": "claim_transaction_claim_id",
    "claim_fee_line": "claim_fee_line_claim_id",
    "claim_reserve": "reserve_claim_id",
    "coverage_participation": "coverage_participation_coverage_id",
}


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
    the is_current_version column. Other names are physically backed by an OPS
    table that has no such column, so filtering them would generate SQL
    referencing a non-existent column."""
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

    # Scalar parent: policyholder (via policy.policy_policyholder_id)
    policyholder_id = out["policy"].get("policy_policyholder_id")
    if policyholder_id and "policyholder" in CANONICAL_TABLES:
        pht = CANONICAL_TABLES["policyholder"]
        ph_row = session.execute(
            _sel(pht, pht.c.policyholder_id == policyholder_id)).fetchone()
        if ph_row:
            out["policyholder"] = _row_to_dict(ph_row, pht)

    # Scalar parent: program (via policy.policy_program_id)
    program_id = out["policy"].get("policy_program_id")
    if program_id and "program" in CANONICAL_TABLES:
        pt = CANONICAL_TABLES["program"]
        prog_row = session.execute(_sel(pt, pt.c.program_id == program_id)).fetchone()
        if prog_row:
            out["program"] = _row_to_dict(prog_row, pt)

    # Scalar parent: contract (via policy.policy_contract_id)
    contract_id = out["policy"].get("policy_contract_id")
    if contract_id and "contract" in CANONICAL_TABLES:
        ct = CANONICAL_TABLES["contract"]
        c_row = session.execute(_sel(ct, ct.c.contract_id == contract_id)).fetchone()
        if c_row:
            out["contract"] = _row_to_dict(c_row, ct)

    # Children with a policy FK
    coverage_ids: list[int] = []
    txn_ids: list[int] = []
    claim_ids: list[int] = []
    for table_name in POLICY_CHILDREN:
        t = CANONICAL_TABLES.get(table_name)
        fk = _FK.get(table_name)
        if t is None or fk not in t.c:
            continue
        rows = session.execute(_sel(t, t.c[fk] == policy_id)).fetchall()
        if not rows:
            continue
        out[table_name] = [_row_to_dict(r, t) for r in rows]
        if table_name == "coverage":
            coverage_ids = [r._mapping["coverage_id"] for r in rows
                            if "coverage_id" in r._mapping]
        elif table_name == "premium_transaction":
            txn_ids = [r._mapping["premium_transaction_id"] for r in rows
                       if "premium_transaction_id" in r._mapping]
        elif table_name == "claim":
            claim_ids = [r._mapping["claim_id"] for r in rows
                         if "claim_id" in r._mapping]

    # Children hanging off premium_transaction
    if txn_ids:
        for table_name in TRANSACTION_CHILDREN:
            t = CANONICAL_TABLES.get(table_name)
            fk = _FK.get(table_name)
            if t is None or fk not in t.c:
                continue
            rows = session.execute(_sel(t, t.c[fk].in_(txn_ids))).fetchall()
            if rows:
                out[table_name] = [_row_to_dict(r, t) for r in rows]

    # Children hanging off the claim
    if claim_ids:
        for table_name in CLAIM_CHILDREN:
            t = CANONICAL_TABLES.get(table_name)
            fk = _FK.get(table_name)
            if t is None or fk not in t.c:
                continue
            rows = session.execute(_sel(t, t.c[fk].in_(claim_ids))).fetchall()
            if rows:
                out[table_name] = [_row_to_dict(r, t) for r in rows]

    # Participations hanging off the coverages
    if coverage_ids and "coverage_participation" in CANONICAL_TABLES:
        cpt = CANONICAL_TABLES["coverage_participation"]
        fk = _FK["coverage_participation"]
        if fk in cpt.c:
            cp_rows = session.execute(_sel(cpt, cpt.c[fk].in_(coverage_ids))).fetchall()
            if cp_rows:
                out["coverage_participation"] = [_row_to_dict(r, cpt) for r in cp_rows]

    # Party-side rows attached to the policy's ambient party (created by the
    # ingester so party_license rows have a real FK to hang on).
    polno = out["policy"].get("policy_number")
    if polno and "party" in CANONICAL_TABLES:
        pt = CANONICAL_TABLES["party"]
        ambient = session.execute(
            select(pt.c.party_id).where(pt.c.party_reference == f"ambient::{polno}")
        ).fetchone()
        if ambient:
            party_id = ambient[0]
            lt = CANONICAL_TABLES.get("party_license")
            if lt is not None and "license_party_id" in lt.c:
                rows = session.execute(
                    _sel(lt, lt.c.license_party_id == party_id)).fetchall()
                if rows:
                    out["party_license"] = [_row_to_dict(r, lt) for r in rows]

    return out


def fetch_policies(session: Session, policy_ids: list[int]) -> list[dict]:
    """Bulk variant; preserves order of policy_ids."""
    return [fetch_policy(session, pid) for pid in policy_ids if pid is not None]
