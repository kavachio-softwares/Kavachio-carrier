"""Write a merged canonical record into the relational warehouse (v4 model).

Input  (one entry from mapper merge — keys are canonical v4 table names):
    {
      "tenant":        {...},           # optional
      "program":       {...},
      "policyholder":  {...},           # the insured, its own entity
      "policy":        {...},
      "coverage":      [{...}, ...] | {...}
      "premium_transaction": [...]
      "risk_location": [...]
      "tax_line":      [...]            # hang off premium_transaction
      "commission_line": [...]
      "claim":         [...]
      "claim_transaction": [...]        # hang off the claim
      "claim_fee_line": [...]
      "claim_reserve": [...]
      "ingested_party": [...]           # party names read off the bordereau
      "party_license": [...]
      ...
    }

Strategy
--------
1. Insert / fetch a `tenant` row keyed on tenant_code (PK → tenant_id).
2. Upsert `program` keyed on (tenant_id, program_name).
3. Upsert `policyholder` keyed on policyholder_natural_key (per tenant).
4. Upsert `policy` keyed on policy_number; wire policy_program_id,
   policy_contract_id (resolved BY EFFECTIVE DATE) and policy_policyholder_id.
5. For each child table, insert one row per array entry with the child's own
   FK column (`<table>_policy_id`, `<table>_premium_transaction_id`,
   `<table>_claim_id`) wired to the parent just created.
6. Party names read off the file land in `ingested_party` — never in the
   curated `party` directory.
7. The function returns the policy_id (or None if no policy was created),
   which the caller links to an upload via the upload_policy table.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Iterable

log = logging.getLogger("bdx.ingester")

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, delete, func, insert, or_, select, text
from sqlalchemy.orm import Session

from canonical import CANONICAL_TABLES, column_names, pk_column, tenant_col

# Tables that, when present in a record, hang off the policy via their own
# `<table>_policy_id` FK column.
POLICY_CHILDREN = ("coverage", "premium_transaction", "risk_location", "claim")

# Tables that hang off premium_transaction via `<table>_premium_transaction_id`.
TRANSACTION_CHILDREN = ("tax_line", "commission_line")

# Tables that hang off a claim via `<table>_claim_id`.
CLAIM_CHILDREN = ("claim_transaction", "claim_fee_line", "claim_reserve")

# Tables that hang off a party via their own party FK.
PARTY_CHILDREN = ("party_license",)

# FK column a child table uses to point at its parent.
_CHILD_FK = {
    "coverage": "coverage_policy_id",
    "premium_transaction": "premium_transaction_policy_id",
    "risk_location": "risk_location_policy_id",
    "claim": "claim_policy_id",
    "tax_line": "tax_line_premium_transaction_id",
    "commission_line": "commission_line_premium_transaction_id",
    "claim_transaction": "claim_transaction_claim_id",
    "claim_fee_line": "claim_fee_line_claim_id",
    "claim_reserve": "reserve_claim_id",
    "party_license": "license_party_id",
    "coverage_participation": "coverage_participation_coverage_id",
}


def _as_list(v: Any) -> list[dict]:
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict) and x]
    if isinstance(v, dict):
        return [v] if v else []
    return []


_TRUTHY = {"true", "t", "yes", "y", "1"}
_FALSY = {"false", "f", "no", "n", "0"}


def _excel_serial_to_datetime(serial) -> datetime:
    """Convert an Excel/1900 date serial number to a datetime.

    Excel stores dates as the number of days since its epoch, which is
    1899-12-30 (it incorrectly treats 1900 as a leap year, so 1899-12-30
    rather than 1900-01-01 gives the correct modern dates). The fractional
    part is the time of day. BDX spreadsheets frequently carry dates as these
    raw serials (e.g. 45931) when the cell isn't date-formatted, so a date/
    datetime column receiving a bare number is almost always an Excel serial.
    """
    f = float(serial)
    if f <= 0:
        raise ValueError("non-positive Excel serial")
    return datetime(1899, 12, 30) + timedelta(days=f)


def _coerce(value: Any, sa_type) -> Any:
    """Type-coerce values before insertion (SQLite types are strict)."""
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except TypeError:
        pass

    # Boolean: accept Yes/No/Y/N/True/False/1/0 strings
    if isinstance(sa_type, Boolean):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            s = value.strip().lower()
            if s in _TRUTHY:
                return True
            if s in _FALSY:
                return False
            return None  # unparseable boolean → store NULL
        return None

    # Date (also handles SQLite Date, which is strict about date objects)
    if isinstance(sa_type, Date) and not isinstance(sa_type, DateTime):
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            # Bare number on a date column → Excel serial date.
            try:
                return _excel_serial_to_datetime(value).date()
            except (ValueError, OverflowError):
                return None
        if isinstance(value, str):
            s = value.strip()
            try:
                # Fast path: YYYYMMDD compact
                if len(s) == 8 and s.isdigit():
                    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
                # YYYYMM accounting period → first of month (v4 stores the
                # accounting period as a DATE column)
                if len(s) == 6 and s.isdigit() and 1 <= int(s[4:6]) <= 12:
                    return date(int(s[:4]), int(s[4:6]), 1)
                # Fast path: ISO YYYY-MM-DD (and ISO datetimes)
                return datetime.fromisoformat(s).date()
            except (ValueError, TypeError):
                pass
            # Fallback: let dateutil handle MM/DD/YYYY, DD-Mon-YYYY, etc.
            try:
                from dateutil import parser as _dp
                return _dp.parse(s, dayfirst=False).date()
            except Exception:
                return None
        return None

    if isinstance(sa_type, DateTime):
        if isinstance(value, datetime):
            return value
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            try:
                return _excel_serial_to_datetime(value)
            except (ValueError, OverflowError):
                return None
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except (ValueError, TypeError):
                pass
            # Fallback: dateutil handles most human datetime formats
            try:
                from dateutil import parser as _dp
                return _dp.parse(value.strip(), dayfirst=False)
            except Exception:
                return None
        return None

    # Integer: accept numeric strings; drop non-numeric
    if isinstance(sa_type, Integer):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            s = value.replace(",", "").strip()
            try:
                return int(float(s))
            except ValueError:
                return None
        return None

    # Numeric / Decimal: accept numeric strings
    if isinstance(sa_type, Numeric):
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            s = value.replace(",", "").replace("$", "").strip()
            try:
                return float(s)
            except ValueError:
                return None
        return None

    return value


# Values that LOOK like a policy number but are the absence of one wearing a
# costume — what a person types into a cell they can't fill. Compared casefolded
# after a strip, so "N/A", "n/a" and " - " all match. Kept as a module constant
# (not inlined) so it is greppable and testable; see the guard in ingest_record.
_POLICY_NUMBER_PLACEHOLDERS = {
    "0", "-", "--", "---", ".", "n/a", "na", "n.a.", "none", "null",
    "unknown", "tbd", "tba", "#n/a", "nan",
}

# Ingest defaults for values the BDX does not carry.
#
# These MUST match Appendix 2 §2.7 of the Palms BDX Ingestion BRD v1.2. The same
# rows pass through Palms' PRS sub-ledger pipeline, which applies its own
# COALESCE with these exact values — a different default on either side produces
# a reconciliation break that nothing errors on, because both systems succeed and
# simply disagree about the number. Change these only in concert with Palms,
# never as part of a refactor. Named (not inlined) so the coupling is greppable,
# and asserted in the tests so a drift fails the build rather than the books.
# Sourced from bdx_defaults so the OUTPUT render (direct_lane.eval_rule) and this
# WAREHOUSE write cannot drift apart — a divergence there is silent on both sides.
from bdx_defaults import (                    # noqa: E402  (grouped with its constants)
    DEFAULT_AMOUNT as _DEFAULT_AMOUNT,
    DEFAULT_CURRENCY as _DEFAULT_CURRENCY,
    DEFAULT_FX_RATE as _DEFAULT_FX_RATE,
    DEFAULT_INSURED_NAME as _DEFAULT_INSURED_NAME,
    DEFAULT_TRANSACTION_TYPE as _DEFAULT_TRANSACTION_TYPE,
)

# §2.8: COALESCE(prs_transaction_short_types, 'UNK') — "Null transaction codes
# are not permitted in output. 'UNK' is the required default."
#
# 'unknown' rather than Palms' 'UNK' because we already translate their shortcode
# vocabulary at ingest (NB→new, EN→endorsement) and the enum is lowercase
# snake_case throughout. The OUTPUT file carries 'UNK' instead
# (bdx_defaults.OUTPUT_DEFAULT_TRANSACTION_TYPE); the value itself is defined in
# bdx_defaults and imported above.

# BDX shortcode → canonical enum value for the transaction type.
_TXN_TYPE_MAP = {
    # New business
    "NB": "new", "N": "new", "NEW": "new", "NEW BUSINESS": "new",
    # Renewal
    "RB": "renewal", "R": "renewal", "RW": "renewal", "RENEWAL": "renewal",
    # Endorsement — includes "Endorsement - Extension", "Endorsement - Amendment", etc.
    "EN": "endorsement", "E": "endorsement", "END": "endorsement",
    "ENDORSEMENT": "endorsement", "ENDT": "endorsement",
    "ENDORSEMENT - EXTENSION": "endorsement",
    "ENDORSEMENT - AMENDMENT": "endorsement",
    "ENDORSEMENT - CORRECTION": "endorsement",
    "ENDORSEMENT - ADDITION": "endorsement",

    "MTA": "endorsement",  # Mid-Term Adjustment
    # Cancellation
    "CN": "cancellation", "C": "cancellation", "CANCEL": "cancellation",
    "CANCELLATION": "cancellation", "CANC": "cancellation",
    # Reinstatement
    "RI": "reinstatement", "REINSTATE": "reinstatement", "REINSTATEMENT": "reinstatement",
    # Installment
    "IN": "installment", "INSTALLMENT": "installment", "INSTALMENT": "installment",
    # Adjustment
    "AD": "adjustment", "ADJUSTMENT": "adjustment", "ADJ": "adjustment",
    # Flat cancellation
    "FC": "flat_cancellation", "FLATCANCEL": "flat_cancellation",
    "FLAT CANCELLATION": "flat_cancellation", "FLAT CANCEL": "flat_cancellation",
    # Audit
    "AU": "audit", "AUDIT": "audit",
}

# Map BDX shortcodes to canonical enum values. The keys are (table, column);
# match on UPPERCASED stripped input value.
_ENUM_NORMALISE: dict[tuple[str, str], dict[str, str]] = {
    ("premium_transaction", "premium_transaction_type"): _TXN_TYPE_MAP,
}


def _normalise_enum(table: str, col: str, value):
    if not isinstance(value, str):
        return value
    m = _ENUM_NORMALISE.get((table, col))
    if not m:
        return value
    key = value.strip().upper()
    # Exact match first
    if key in m:
        return m[key]
    # Prefix fallback: "Endorsement - Return Premium", "Cancellation - Flat", etc.
    for prefix, canonical in m.items():
        if key.startswith(prefix + " ") or key.startswith(prefix + "-") or key.startswith(prefix + " -"):
            return canonical
    return value


def _filter_to_schema(table_name: str, payload: dict) -> dict:
    """Drop columns the canonical table doesn't have; coerce remaining values
    and normalise BDX shortcodes to canonical enum values."""
    t = CANONICAL_TABLES.get(table_name)
    if t is None:
        return {}
    cols = t.c
    out = {}
    for k, v in payload.items():
        if k not in cols:
            continue
        v = _normalise_enum(table_name, k, v)
        out[k] = _coerce(v, cols[k].type)
    return out


def _insert_row(session: Session, table_name: str, payload: dict) -> int | None:
    """Insert a row, return its PK."""
    t = CANONICAL_TABLES.get(table_name)
    if t is None or not payload:
        return None
    res = session.execute(insert(t).values(**payload))
    pk = res.inserted_primary_key
    return pk[0] if pk else None


def _memo(session: Session, bucket: str, key, factory):
    """Per-session (≈ per-upload) memoization for INVARIANT find-or-create
    lookups (tenant / contract / policyholder). A 10k-row upload shares
    one Session, so without this each of these is re-resolved with a DB
    round-trip on EVERY row; here they're resolved once and reused. Stored on
    session.info; only used for rows that don't change within an upload."""
    cache = session.info.setdefault("_ingest_memo", {}).setdefault(bucket, {})
    if key not in cache:
        cache[key] = factory()
    return cache[key]


def _ensure_tenant(session: Session, mga_name: str) -> int:
    """Find-or-create a tenant row keyed by tenant_code (the MGA identifier)."""
    def _resolve():
        t = CANONICAL_TABLES["tenant"]
        row = session.execute(select(t.c.tenant_id).where(t.c.tenant_code == mga_name)).fetchone()
        if row:
            return row[0]
        return _insert_row(session, "tenant", _filter_to_schema("tenant", {
            "tenant_code": mga_name,
            "tenant_legal_name": mga_name,
            "tenant_type": "carrier",
            "tenant_is_active": True,
        }))
    return _memo(session, "tenant", mga_name, _resolve)


def _ensure_party_with_reference(
    session: Session, tenant_id: int, reference: str,
    party_type: str = "agency", legal_name: str | None = None,
) -> int | None:
    """Find-or-create a curated party row keyed by tenant + party_reference."""
    if "party" not in CANONICAL_TABLES:
        return None
    pt = CANONICAL_TABLES["party"]
    row = session.execute(
        select(pt.c.party_id, pt.c.party_legal_name)
        .where(pt.c.party_tenant_id == tenant_id)
        .where(pt.c.party_reference == reference)
    ).fetchone()
    if row:
        party_id, current = row[0], row[1]
        # Re-ingest: refresh the name when a real one is now supplied.
        if legal_name and str(legal_name).strip() and legal_name != current:
            session.execute(
                pt.update().where(pt.c.party_id == party_id)
                .values(party_legal_name=legal_name)
            )
        return party_id
    return _insert_row(session, "party", _filter_to_schema("party", {
        "party_tenant_id": tenant_id,
        "party_reference": reference,
        "party_type": party_type,
        "party_legal_name": legal_name or reference,
        "party_is_active": True,
    }))


def _ensure_carrier_party(session: Session, tenant_id: int,
                          legal_name: str | None = None) -> int | None:
    """Find-or-create the party row that IS this carrier.

    A tenant no longer picks which carrier it writes for — it IS the carrier
    (see app_routes.my_carrier_party). Screens that must still store a
    carrier_party_id (Bordereau Setup, pipelines, output templates) resolve it
    here. Keyed on the stable reference `carrier::<tenant_id>`, so a tenant has
    exactly one of these forever and a rename never spawns a second.
    """
    def _resolve():
        return _ensure_party_with_reference(
            session, tenant_id, f"carrier::{tenant_id}",
            party_type="carrier",
            legal_name=(legal_name or "").strip() or f"Tenant {tenant_id} Carrier",
        )
    return _memo(session, "carrier", tenant_id, _resolve)


def _ensure_policyholder(session: Session, tenant_id: int, payload: dict,
                         polno: str | None) -> int | None:
    """Find-or-create the insured as its own entity (v4: policyholder).

    Keyed on policyholder_natural_key. When the file supplies a legal name and
    a tax id / registration number, the key is derived from those (so the same
    company insured three times is ONE row); otherwise it falls back to the
    policy number, which keeps distinct unknown insureds distinct.
    Policyholders are tenant-scoped: the same company insured by two carriers
    is two rows.
    """
    if "policyholder" not in CANONICAL_TABLES:
        return None
    name = payload.get("policyholder_legal_name")
    name = name.strip() if isinstance(name, str) else None
    tax = (payload.get("policyholder_tax_id")
           or payload.get("policyholder_registration_number"))
    tax = str(tax).strip() if tax not in (None, "") else None
    if name and tax:
        natural = f"{tenant_id}::{name.casefold()}::{tax.casefold()}"
    elif name:
        natural = f"{tenant_id}::{name.casefold()}"
    else:
        natural = f"{tenant_id}::policy::{polno or 'unknown'}"

    def _resolve():
        t = CANONICAL_TABLES["policyholder"]
        row = session.execute(
            select(t.c.policyholder_id, t.c.policyholder_legal_name)
            .where(t.c.policyholder_tenant_id == tenant_id)
            .where(t.c.policyholder_natural_key == natural)
        ).fetchone()
        if row:
            ph_id, current = row[0], row[1]
            # §2.7 refresh: a corrected mapping can supply the real name later.
            if name and name != current:
                session.execute(
                    t.update().where(t.c.policyholder_id == ph_id)
                    .values(policyholder_legal_name=name)
                )
            return ph_id
        return _insert_row(session, "policyholder", _filter_to_schema("policyholder", {
            **payload,
            "policyholder_tenant_id": tenant_id,
            "policyholder_natural_key": natural,
            "policyholder_legal_name": name or _DEFAULT_INSURED_NAME,
        }))
    return _memo(session, "policyholder", natural, _resolve)


def _ensure_contract(session: Session, tenant_id: int, program_id: int) -> int | None:
    """Find-or-create a placeholder contract per (tenant, program)."""
    if "contract" not in CANONICAL_TABLES:
        return None

    def _resolve():
        t = CANONICAL_TABLES["contract"]
        umr = f"auto::{tenant_id}::{program_id}"
        row = session.execute(
            select(t.c.contract_id).where(t.c.contract_primary_umr == umr)
        ).fetchone()
        if row:
            return row[0]
        return _insert_row(session, "contract", _filter_to_schema("contract", {
            "tenant_id": tenant_id,
            "contract_program_id": program_id,
            "contract_primary_umr": umr,
            "contract_name": f"Auto contract for program {program_id}",
            "contract_type": "binding_authority",
            "contract_inception_date": date(1970, 1, 1),
            "contract_expiry_date": date(2999, 12, 31),
            "contract_status": "active",
        }))
    return _memo(session, "contract", (tenant_id, program_id), _resolve)


def _resolve_contract(session: Session, tenant_id: int, program_id: int,
                      pol_eff_dt) -> int | None:
    """Resolve the contract a policy belongs to BY ITS EFFECTIVE DATE.

    Palms BDX Ingestion BRD v1.2, Appendix 2 §2.10:

        policy.pol_eff_dt >= contracts.inception_date
        AND policy.pol_eff_dt <  contracts.expiry_date

    A policy belongs to the contract in force on the day it STARTS: inception
    counts, expiry does not. The `<` is load-bearing and must not become `<=`.
    Contracts run back to back, so on a renewal day a `<=` matches BOTH the
    expiring contract and the incoming one, and the premium is counted twice.

    WHY THE PLACEHOLDERS ARE EXCLUDED
    _ensure_contract mints one auto:: contract per (tenant, program) spanning
    1970-01-01 to 2999-12-31. That window matches EVERY date, so without the
    umr filter below the placeholder always wins the join and the effective
    date is never consulted.

    OVERLAPS: if two real contracts both cover the date the LATEST inception
    wins, so the result is deterministic rather than dependent on row order.

    NO MATCH: falls back to the placeholder, exactly as before, and logs.
    """
    if "contract" not in CANONICAL_TABLES:
        return None
    if not pol_eff_dt:
        # Unreachable via ingest_record — §2.2's guard drops a record with no
        # effective date before _upsert_policy runs. Kept so a future caller
        # cannot silently get a date-blind join.
        return _ensure_contract(session, tenant_id, program_id)

    t = CANONICAL_TABLES["contract"]

    def _resolve():
        stmt = (
            select(t.c.contract_id)
            .where(t.c[tenant_col("contract")] == tenant_id)
            .where(t.c.contract_program_id == program_id)
            .where(t.c.contract_inception_date <= pol_eff_dt)   # >= inception
            .where(t.c.contract_expiry_date > pol_eff_dt)       # <  expiry (§2.10)
            .order_by(t.c.contract_inception_date.desc())
            .limit(1)
        )
        # The auto:: placeholders span all of time; they must never win here.
        stmt = stmt.where(or_(t.c.contract_primary_umr.is_(None),
                              ~t.c.contract_primary_umr.like("auto::%")))
        if "contract_status" in t.c:
            stmt = stmt.where(or_(t.c.contract_status.is_(None),
                                  t.c.contract_status.notin_(("terminated", "expired"))))
        if "is_current_version" in t.c:
            stmt = stmt.where(t.c.is_current_version.isnot(False))
        row = session.execute(stmt).fetchone()
        if row:
            return row[0]
        log.warning("no contract covers policy effective %s on program %s "
                    "(tenant %s) — using the placeholder contract; this row is "
                    "not bound to a real contract window", pol_eff_dt,
                    program_id, tenant_id)
        return _ensure_contract(session, tenant_id, program_id)

    # The date MUST be part of the memo key. _memo is per-session (≈ per-upload)
    # and keyed only on (tenant, program) the first row's contract would be
    # handed to every later row whatever its date — defeating the whole point.
    return _memo(session, "contract_by_date",
                 (tenant_id, program_id, pol_eff_dt), _resolve)


def _contract_broker_party_id(session: Session, contract_id: int | None) -> int | None:
    """policy_contract_broker_party_id is DERIVED, never supplied: it is set
    from the policy's contract and held there by a composite FK, so a policy
    can never name a different broker than its contract."""
    if not contract_id or "contract" not in CANONICAL_TABLES:
        return None

    def _resolve():
        t = CANONICAL_TABLES["contract"]
        row = session.execute(
            select(t.c.contract_broker_party_id).where(t.c.contract_id == contract_id)
        ).fetchone()
        return row[0] if row else None
    return _memo(session, "contract_broker", contract_id, _resolve)


def _ensure_canonical_upload(
    session: Session, tenant_id: int, filename: str, file_year: int, file_month: int,
    load_type: str = "incremental", mapping_profile_id: int | None = None,
    invalidates_upload_id: int | None = None, num_rows: int | None = None,
) -> int | None:
    """Create an upload row on the canonical side; one per /bdx/upload call.

    v4: the period is a date range (upload_period_start / upload_period_end),
    the row count is upload_rows_total and approval is an upload_status value.
    """
    if "upload" not in CANONICAL_TABLES:
        return None
    import calendar, hashlib, time
    file_hash = hashlib.sha256(f"{filename}::{time.time()}".encode()).hexdigest()
    period_start = period_end = None
    if file_year and file_month:
        period_start = date(int(file_year), int(file_month), 1)
        period_end = date(int(file_year), int(file_month),
                          calendar.monthrange(int(file_year), int(file_month))[1])
    return _insert_row(session, "upload", _filter_to_schema("upload", {
        "tenant_id": tenant_id,
        "upload_bordereau_type": "premium",
        "upload_filename": filename,
        "upload_file_hash": file_hash,
        "upload_period_start": period_start,
        "upload_period_end": period_end,
        "upload_load_type_detected": load_type,
        "upload_rows_total": num_rows,
        "upload_status": "approved",
    }))


def _upsert_program(session: Session, tenant_id: int, payload: dict) -> int | None:
    if not payload:
        return None
    name = payload.get("program_name")
    if not name:
        return None
    t = CANONICAL_TABLES["program"]
    row = session.execute(
        select(t.c.program_id)
        .where(t.c.program_tenant_id == tenant_id)
        .where(t.c.program_name == name)
    ).fetchone()
    if row:
        return row[0]
    values = _filter_to_schema("program", {
        **payload,
        "program_tenant_id": tenant_id,
        "program_status": payload.get("program_status") or "active",
    })
    return _insert_row(session, "program", values)


_SCD_BOOKKEEPING = ("is_current_version", "valid_from", "valid_until", "version_no")


def _norm_val(v) -> str | None:
    """Loose comparison key so 1000000 == 1000000.00 and dates compare by day."""
    if v is None:
        return None
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (ValueError, TypeError):
        if isinstance(v, (date, datetime)):
            return str(v)[:10]
        return str(v).strip()


def _supersede(session: Session, t, whereclause) -> None:
    """Re-ingest (Option A): RETIRE matching active rows (keep as inactive SCD-2
    history) when the table is versioned; otherwise delete them (so non-versioned
    / ops-shadowed tables don't accumulate)."""
    if "is_current_version" in t.c:
        session.execute(
            t.update().where(whereclause)
            .where(t.c.is_current_version.isnot(False))
            .values(is_current_version=False, valid_until=func.now())
        )
    else:
        session.execute(t.delete().where(whereclause))


def _scd2_version_inplace(session: Session, table: str, pk_col: str, pk_value,
                          new_values: dict) -> None:
    """Re-ingest (Option A) for a kept-id row (policy): retire the current active
    version and insert a NEW active version with the SAME id, overlaying
    new_values. The prior version is preserved as inactive history. Falls back to
    a plain UPDATE for tables without SCD columns."""
    t = CANONICAL_TABLES.get(table)
    if t is None or not new_values:
        return
    if "is_current_version" not in t.c:
        session.execute(t.update().where(t.c[pk_col] == pk_value).values(**new_values))
        return

    phys = {r[0] for r in session.execute(
        text("SELECT column_name FROM information_schema.columns "
             "WHERE table_name = :t AND table_schema = current_schema()"),
        {"t": table})}
    edited = [c for c in new_values if c in t.c and c in phys
              and c not in _SCD_BOOKKEEPING and c != pk_col]
    if not edited:
        return

    # 1) retire the active version
    session.execute(text(
        f"UPDATE {table} SET is_current_version = FALSE, valid_until = now() "
        f"WHERE {pk_col} = :pk AND is_current_version IS NOT FALSE"), {"pk": pk_value})

    # 2) insert a new active version (SAME id) = copy of the retired row + edits
    ident = False
    try:
        from scd2_sql import pk_is_identity_always
        ident = pk_is_identity_always(session, table)
    except Exception:  # noqa: BLE001
        pass
    overriding = " OVERRIDING SYSTEM VALUE" if ident else ""
    copy_cols = [c for c in t.c.keys() if c in phys
                 and c not in _SCD_BOOKKEEPING and c not in edited]
    insert_cols = copy_cols + edited + list(_SCD_BOOKKEEPING)
    params = {"pk": pk_value}
    sel = list(copy_cols)
    for c in edited:
        params[f"v_{c}"] = new_values[c]
        sel.append(f":v_{c}")
    sel += ["TRUE", "now()", "TIMESTAMP '2999-12-31'", "COALESCE(version_no, 1) + 1"]
    session.execute(text(
        f"INSERT INTO {table} ({', '.join(insert_cols)}){overriding} "
        f"SELECT {', '.join(sel)} FROM {table} WHERE {pk_col} = :pk "
        f"ORDER BY version_no DESC NULLS LAST LIMIT 1"), params)


def _upsert_policy(session: Session, tenant_id: int, program_id: int | None,
                   payload: dict, policyholder_payload: dict | None = None,
                   ) -> tuple[int | None, bool]:
    """Returns (policy_id, is_new). `is_new` is True when this call CREATED the
    policy (no prior row existed) — the caller can then skip the re-ingest
    child-cleanup, which is a no-op for a brand-new policy but costs ~a dozen
    round-trips per row."""
    if not payload:
        return None, False
    polno = payload.get("policy_number")
    t = CANONICAL_TABLES["policy"]

    # §2.10: bind the policy to the contract in force on its EFFECTIVE DATE,
    # not to a catch-all placeholder. Falls back to the placeholder (previous
    # behaviour) when no real contract covers the date.
    contract_id = _resolve_contract(
        session, tenant_id, program_id, payload.get("policy_effective_date")
    ) if program_id else None
    # v4: the insured is its own entity, keyed on policyholder_natural_key.
    policyholder_id = _ensure_policyholder(
        session, tenant_id, policyholder_payload or {}, polno)

    base = _filter_to_schema("policy", {
        **payload,
        "tenant_id": tenant_id,
        "policy_program_id": program_id,
        "policy_contract_id": contract_id,
        "policy_policyholder_id": policyholder_id,
        # Derived, never supplied: the broker comes from the contract.
        "policy_contract_broker_party_id": _contract_broker_party_id(session, contract_id),
    })

    if polno:
        stmt = (
            select(t.c.policy_id)
            .where(t.c[tenant_col("policy")] == tenant_id)
            .where(t.c.policy_number == polno)
        )
        # A 'Modify here' edit can leave a retired (is_current_version=FALSE)
        # version alongside the active one for the same (tenant, policy_number).
        # Re-ingest must update the ACTIVE version, never a superseded one.
        if "is_current_version" in t.c:
            stmt = stmt.where(t.c.is_current_version.isnot(False)).order_by(t.c.policy_id.desc())
        row = session.execute(stmt).fetchone()
        if row:
            policy_id = row[0]
            # Re-ingest (Option A — sheet wins, prior edit kept as history).
            if base:
                cur = (session.execute(
                    select(t).where(t.c.policy_id == policy_id)
                    .where(t.c.is_current_version.isnot(False))
                ).mappings().first() if "is_current_version" in t.c else None)
                changed = (cur is None) or any(
                    _norm_val(cur.get(k)) != _norm_val(v) for k, v in base.items())
                if changed:
                    _scd2_version_inplace(session, "policy", "policy_id", policy_id, base)
            return policy_id, False

    # New policy: apply last-resort defaults for the NOT NULL date columns.
    values = dict(base)
    if "policy_effective_date" in t.c and not values.get("policy_effective_date"):
        # UNREACHABLE via ingest_record, which now drops a record with no
        # effective date before it gets here. Kept as a backstop because a
        # future caller could reach _upsert_policy directly — but it logs at
        # ERROR, because a 1970 date silently misattributes the contract, the
        # commission band and the accounting period.
        log.error("policy %s reached _upsert_policy with no effective date — "
                  "the ingest_record guard was bypassed; storing the 1970 "
                  "sentinel, which WILL misattribute contract and commission",
                  polno)
        values["policy_effective_date"] = date(1970, 1, 1)
    if "policy_expiration_date" in t.c and not values.get("policy_expiration_date"):
        values["policy_expiration_date"] = date(2999, 12, 31)
    return _insert_row(session, "policy", values), True


def _child_defaults(table_name: str, parent_ctx: dict) -> dict:
    """Per-table defaults to satisfy canonical Postgres NOT NULL constraints
    that BDX rows commonly don't carry."""
    pol = parent_ctx.get("policy_payload") or {}

    if table_name == "premium_transaction":
        from datetime import date as _date
        # Children only build after the parent policy exists, and ingest_record
        # drops a record with no effective date before creating one — so the
        # sentinel below is a backstop for an unparseable date string, not for
        # a missing one. Both cases log, for the same reason as _upsert_policy:
        # a 1970 date reaches the booking and accounting dates and looks valid.
        eff = pol.get("policy_effective_date")
        if not eff:
            log.error("_child_defaults: no policy_effective_date on the parent "
                      "policy — falling back to the 1970 sentinel")
            eff = date(1970, 1, 1)
        if isinstance(eff, str):
            try:
                eff = datetime.fromisoformat(eff).date()
            except (ValueError, TypeError):
                log.error("_child_defaults: unparseable policy_effective_date %r "
                          "— falling back to the 1970 sentinel", eff)
                eff = date(1970, 1, 1)
        booking = eff if eff is not None else _date.today()
        return {
            "premium_transaction_type": _DEFAULT_TRANSACTION_TYPE,
            "premium_transaction_effective_date": eff,
            "premium_transaction_booking_date": booking,
            "premium_transaction_accounting_date": booking,
            "premium_transaction_original_currency":
                pol.get("policy_sum_insured_currency") or _DEFAULT_CURRENCY,
            # §2.7: COALESCE(exchg_rate, 1.0). No conversion happens at ingest,
            # so the rate is par by default. A BARE 1.0 is indistinguishable
            # from a genuine same-currency rate, though, and BRD §4.4 requires
            # the rate's SOURCE and AS-OF DATE on every converted record. Hence
            # the convention:
            #
            #     premium_transaction_fx_rate_date IS NULL
            #         ==  the rate was DEFAULTED, never sourced
            #
            # A looked-up rate always carries its as-of date (§4.4 demands it),
            # so the two can never be confused, and every defaulted row is
            # findable with one predicate.
            "premium_transaction_exchange_rate": _DEFAULT_FX_RATE,
            "premium_transaction_fx_rate_date": None,
        }
    if table_name == "party_license":
        return {"license_state": "XX", "license_type": "producer"}
    if table_name == "tax_line":
        return {"tax_line_type": "premium_tax", "tax_line_amount": _DEFAULT_AMOUNT}
    if table_name == "commission_line":
        return {
            "commission_line_type": "producing_broker",
            "commission_line_amount": _DEFAULT_AMOUNT,
        }
    return {}


def _insert_children(
    session: Session, table_name: str, items: Iterable[dict],
    parent_ctx: dict | None = None, **fks: int | None,
) -> list[int]:
    """Insert a list of dicts as rows of `table_name`, stamped with the given FKs
    and any per-table required defaults. FK kwargs may use either the child's
    own column name or a generic name resolved via _CHILD_FK."""
    ids: list[int] = []
    cols = column_names(table_name)
    defaults = _child_defaults(table_name, parent_ctx or {})

    for raw in items:
        merged = {**defaults, **raw}
        for fk, val in fks.items():
            if val is None:
                continue
            col = fk if fk in cols else _CHILD_FK.get(table_name) if fk == "parent_id" else fk
            if col in cols:
                merged[col] = val
        values = _filter_to_schema(table_name, merged)
        if not values:
            continue
        # Skip rows that can't satisfy the table's own required-field check.
        if table_name == "party_license" and not values.get("license_number"):
            continue
        if table_name == "ingested_party" and not values.get("ingested_party_legal_name"):
            continue
        if table_name == "premium_transaction" and not values.get("premium_transaction_booking_date"):
            # The booking date must be non-null. The merged record may have
            # explicitly set it to None (mapper found no matching column).
            # Fall back to the effective date, or today as last resort.
            from datetime import date as _date
            values["premium_transaction_booking_date"] = (
                values.get("premium_transaction_effective_date") or _date.today())
            # Re-derive the accounting date now that we have a valid date.
            values.setdefault("premium_transaction_accounting_date",
                              values["premium_transaction_booking_date"])
        pk = _insert_row(session, table_name, values)
        if pk:
            ids.append(pk)
    return ids


def _ingest_parties(session: Session, tenant_id: int, upload_id: int | None,
                    items: list[dict]) -> list[int]:
    """v4: party names read off a bordereau land in ingested_party — never in
    the curated party directory. When a name matches a curated party of the
    tenant, the row is linked via ingested_party_matched_party_id."""
    if "ingested_party" not in CANONICAL_TABLES or not items:
        return []
    pt = CANONICAL_TABLES.get("party")

    def _match(name: str | None) -> int | None:
        if not name or pt is None:
            return None
        key = name.strip().casefold()
        if not key:
            return None

        def _resolve():
            row = session.execute(
                select(pt.c.party_id)
                .where(pt.c.party_tenant_id == tenant_id)
                .where(func.lower(pt.c.party_legal_name) == key)
            ).fetchone()
            return row[0] if row else None
        return _memo(session, "ingested_party_match", key, _resolve)

    enriched = []
    for raw in items:
        if not isinstance(raw, dict) or not raw:
            continue
        matched = _match(raw.get("ingested_party_legal_name"))
        enriched.append({
            **raw,
            "ingested_party_matched_party_id": matched,
            "ingested_party_match_confidence": "exact" if matched else None,
        })
    return _insert_children(
        session, "ingested_party", enriched,
        ingested_party_tenant_id=tenant_id, ingested_party_upload_id=upload_id,
    )


def _ensure_ambient_party(session: Session, tenant_id: int, label: str) -> int | None:
    """Find-or-create a synthetic party so party_license rows have a FK.

    Idempotent on re-upload: keyed by (tenant, party_reference) it reuses the
    existing ambient party instead of blind-inserting a duplicate."""
    return _ensure_party_with_reference(
        session, tenant_id, label, party_type="agency", legal_name=label)


def _clear_policy_children(session: Session, policy_id: int, policy_number: str | None) -> None:
    """Re-ingest cleanup (Option A — sheet wins, prior edits kept as history):
    RETIRE the policy's existing canonical child rows (mark them inactive SCD-2
    history) instead of deleting them, so a re-upload preserves prior edits; the
    caller then inserts the fresh sheet rows as the new ACTIVE versions. Tables
    without SCD columns are still deleted so they don't accumulate.
    """
    # 1a. transaction-children first (keyed by this policy's active transaction ids)
    pt = CANONICAL_TABLES.get("premium_transaction")
    if pt is not None and "premium_transaction_policy_id" in pt.c:
        tstmt = select(pt.c.premium_transaction_id).where(
            pt.c.premium_transaction_policy_id == policy_id)
        if "is_current_version" in pt.c:
            tstmt = tstmt.where(pt.c.is_current_version.isnot(False))
        txn_ids = [r[0] for r in session.execute(tstmt).fetchall()]
        if txn_ids:
            for child in TRANSACTION_CHILDREN:
                ct = CANONICAL_TABLES.get(child)
                fk = _CHILD_FK.get(child)
                if ct is not None and fk in ct.c:
                    _supersede(session, ct, ct.c[fk].in_(txn_ids))

    # 1b. claim-children keyed by this policy's claim ids
    clt = CANONICAL_TABLES.get("claim")
    if clt is not None and "claim_policy_id" in clt.c:
        cstmt = select(clt.c.claim_id).where(clt.c.claim_policy_id == policy_id)
        if "is_current_version" in clt.c:
            cstmt = cstmt.where(clt.c.is_current_version.isnot(False))
        claim_ids = [r[0] for r in session.execute(cstmt).fetchall()]
        if claim_ids:
            for child in CLAIM_CHILDREN:
                ct = CANONICAL_TABLES.get(child)
                fk = _CHILD_FK.get(child)
                if ct is not None and fk in ct.c:
                    _supersede(session, ct, ct.c[fk].in_(claim_ids))

    # 1c. coverage-children (participations) keyed by this policy's coverage ids
    cov = CANONICAL_TABLES.get("coverage")
    if cov is not None and "coverage_policy_id" in cov.c:
        vstmt = select(cov.c.coverage_id).where(cov.c.coverage_policy_id == policy_id)
        if "is_current_version" in cov.c:
            vstmt = vstmt.where(cov.c.is_current_version.isnot(False))
        cov_ids = [r[0] for r in session.execute(vstmt).fetchall()]
        if cov_ids:
            cp = CANONICAL_TABLES.get("coverage_participation")
            if cp is not None and "coverage_participation_coverage_id" in cp.c:
                _supersede(session, cp,
                           cp.c.coverage_participation_coverage_id.in_(cov_ids))

    # policy-children (incl. premium_transaction itself)
    for table_name in POLICY_CHILDREN:
        t = CANONICAL_TABLES.get(table_name)
        fk = _CHILD_FK.get(table_name)
        if t is None or fk not in t.c:
            continue
        _supersede(session, t, t.c[fk] == policy_id)

    # ambient party CHILDREN — retire (versioned) / delete (non-versioned). The
    # ambient party row itself is kept and reused (find-or-create) so retired
    # children aren't orphaned.
    if policy_number and "party" in CANONICAL_TABLES:
        party_t = CANONICAL_TABLES["party"]
        reference = f"ambient::{policy_number}"
        row = session.execute(
            select(party_t.c.party_id).where(party_t.c.party_reference == reference)
        ).fetchone()
        if row:
            ambient_id = row[0]
            for child in PARTY_CHILDREN:
                ct = CANONICAL_TABLES.get(child)
                fk = _CHILD_FK.get(child)
                if ct is not None and fk in ct.c:
                    _supersede(session, ct, ct.c[fk] == ambient_id)


def ingest_record(session: Session, mga: str, record: dict,
                  canonical_upload_id: int | None = None) -> int | None:
    """Ingest one merged record. Returns the policy_id created (or None).

    Idempotent: if the policy already exists, its child canonical rows are
    retired and re-inserted with the freshly merged data.

    `canonical_upload_id` (when supplied) is stamped on premium_transaction
    rows (ingestion lineage) and on ingested_party rows.
    """
    tenant_id = _ensure_tenant(session, mga)
    program_id = _upsert_program(session, tenant_id, record.get("program") or {})
    pol_payload = record.get("policy") or {}
    ph_payload = record.get("policyholder") or {}
    if isinstance(ph_payload, list):
        ph_payload = next((p for p in ph_payload if isinstance(p, dict) and p), {})
    # Bridge: a mapper that targets the party legal-name field for an
    # insured-name column lands the value under record["ingested_party"] or
    # record["party"]. When the policyholder payload has no name, treat that
    # as the insured's legal name so it reaches the policyholder entity.
    if not ph_payload.get("policyholder_legal_name"):
        for bridge_key, name_col in (("ingested_party", "ingested_party_legal_name"),
                                     ("party", "party_legal_name")):
            blob = record.get(bridge_key)
            if isinstance(blob, list):
                blob = next((p for p in blob
                             if isinstance(p, dict) and p.get(name_col)), None)
            if isinstance(blob, dict) and blob.get(name_col):
                ph_payload = {**ph_payload,
                              "policyholder_legal_name": blob[name_col]}
                break
    polno = (pol_payload.get("policy_number") or "")
    polno = polno.strip() if isinstance(polno, str) else polno
    if not polno or (isinstance(polno, str)
                     and polno.casefold() in _POLICY_NUMBER_PLACEHOLDERS):
        # `policy.policy_number` is required. If the mapping spec didn't
        # produce one for this record (often the case for non-POL sheets that
        # lack a PolicyNumber column), skip the whole record rather than
        # 500-ing the ingest.
        #
        # A PLACEHOLDER is rejected the same way. "0" / "-" is what a person
        # types into a cell they can't fill: a truthy string that sails past a
        # plain falsy check, then becomes a policy AND a policyholder natural
        # key — so every row carrying the same placeholder collides onto one
        # bogus policyholder.
        log.warning("ingest_record: skipping record with invalid policy_number "
                    "%r (other keys=%s)", polno, list(record.keys()))
        return None
    if not pol_payload.get("policy_effective_date"):
        # The policy effective date is the join key for contract resolution, the
        # commission rate band, and the accounting period. Substituting one does
        # not produce a single wrong field — it resolves all three confidently to
        # the wrong answer, silently. So a missing date is a HARD FILTER, not a
        # fallback (Palms BDX BRD v1.2, Appendix 2 §2.2), the same treatment a
        # missing policy number already gets above.
        log.warning("ingest_record: skipping policy %s with no "
                    "policy_effective_date", polno)
        return None
    policy_id, policy_is_new = _upsert_policy(
        session, tenant_id, program_id, pol_payload, ph_payload)
    # Re-ingest cleanup retires a policy's existing child rows before re-inserting.
    # A brand-new policy has none, so skip it — that saves ~a dozen DB round-trips
    # per row, which dominates the cost of a fresh (10k-row) upload.
    if policy_id is not None and not policy_is_new:
        _clear_policy_children(session, policy_id, polno)

    ctx = {"policy_payload": pol_payload}

    # Party names read off the file → ingested_party (with curated-party match).
    _ingest_parties(session, tenant_id, canonical_upload_id,
                    _as_list(record.get("ingested_party")))

    # Children that hang off the policy.
    coverage_ids: list[int] = []
    txn_ids: list[int] = []
    claim_ids: list[int] = []
    for table in POLICY_CHILDREN:
        items = _as_list(record.get(table))
        if not items:
            continue
        extra_fks: dict[str, int | None] = {
            _CHILD_FK[table]: policy_id,
            "tenant_id": tenant_id,
        }
        if table == "premium_transaction":
            extra_fks["premium_transaction_upload_id"] = canonical_upload_id
        ids = _insert_children(session, table, items, parent_ctx=ctx, **extra_fks)
        if table == "coverage":
            coverage_ids = ids
        elif table == "premium_transaction":
            txn_ids = ids
        elif table == "claim":
            claim_ids = ids

    # Children that hang off premium_transaction. If the BDX provides taxes/
    # commissions without an explicit premium_transaction in the record, we
    # still need a transaction to attach them to — synthesise one.
    needs_txn_parent = any(record.get(t) for t in TRANSACTION_CHILDREN)
    if needs_txn_parent and not txn_ids:
        ids = _insert_children(
            session, "premium_transaction", [{}], parent_ctx=ctx,
            premium_transaction_policy_id=policy_id, tenant_id=tenant_id,
            premium_transaction_upload_id=canonical_upload_id,
        )
        txn_ids = ids
    parent_txn_id = txn_ids[0] if txn_ids else None
    for table in TRANSACTION_CHILDREN:
        items = _as_list(record.get(table))
        if not items or parent_txn_id is None:
            continue
        _insert_children(session, table, items, parent_ctx=ctx,
                         tenant_id=tenant_id,
                         **{_CHILD_FK[table]: parent_txn_id})

    # Children that hang off the claim (fee lines, movements, reserves).
    parent_claim_id = claim_ids[0] if claim_ids else None
    for table in CLAIM_CHILDREN:
        items = _as_list(record.get(table))
        if not items or parent_claim_id is None:
            continue
        _insert_children(session, table, items, parent_ctx=ctx,
                         tenant_id=tenant_id,
                         **{_CHILD_FK[table]: parent_claim_id})

    # Participations hang off the first coverage of the record.
    cp_items = _as_list(record.get("coverage_participation"))
    if cp_items and coverage_ids:
        _insert_children(session, "coverage_participation", cp_items,
                         parent_ctx=ctx, tenant_id=tenant_id,
                         coverage_participation_coverage_id=coverage_ids[0])

    # Party-side tables: create an ambient party once if any of these are present.
    needs_party = any(record.get(t) for t in PARTY_CHILDREN)
    if needs_party:
        label = f"ambient::{polno or 'unknown'}"
        party_id = _ensure_ambient_party(session, tenant_id, label)
        for table in PARTY_CHILDREN:
            _insert_children(session, table, _as_list(record.get(table)),
                             parent_ctx=ctx, tenant_id=tenant_id,
                             **{_CHILD_FK[table]: party_id})

    # User-defined extra fields. The mapper has already grouped them by
    # target entity. Write each group onto that entity's `extras` JSONB
    # column. Entities we don't have an id for yet (e.g. no claim row was
    # created in this record) are skipped.
    extras_by_entity = record.get("extras") or {}
    if isinstance(extras_by_entity, dict) and extras_by_entity:
        _write_entity_extras(
            session, extras_by_entity,
            policy_id=policy_id,
            entity_ids={
                "policy": policy_id,
                "coverage": coverage_ids[0] if coverage_ids else None,
                "premium_transaction": parent_txn_id,
                "claim": parent_claim_id,
            },
        )

    return policy_id


def _write_entity_extras(
    session: Session,
    extras_by_entity: dict,
    *,
    policy_id: int | None,
    entity_ids: dict[str, int | None],
) -> None:
    """Merge extras into the JSONB column of each target entity's most
    recent row for this policy. Missing target rows are skipped."""
    from canonical import CANONICAL_TABLES, EXTRA_FIELD_ENTITY_TABLES

    for entity, kv in (extras_by_entity or {}).items():
        if not isinstance(kv, dict) or not kv or entity not in EXTRA_FIELD_ENTITY_TABLES:
            continue
        t = CANONICAL_TABLES.get(entity)
        if t is None or "extras" not in t.c:
            continue

        # Pick the target row id. For policy we already have it; for
        # children we take the most recent row attached to this policy.
        pk_name = pk_column(entity)
        if pk_name is None:
            continue
        if entity == "policy":
            row_id = policy_id
        else:
            row_id = entity_ids.get(entity)
            fk = _CHILD_FK.get(entity)
            if row_id is None and policy_id is not None and fk and fk in t.c:
                row = session.execute(
                    select(t.c[pk_name]).where(t.c[fk] == policy_id)
                    .order_by(t.c[pk_name].desc()).limit(1)
                ).fetchone()
                row_id = row[0] if row else None
        if row_id is None:
            continue

        existing = session.execute(
            select(t.c.extras).where(t.c[pk_name] == row_id)
        ).scalar() or {}
        merged = {**(existing if isinstance(existing, dict) else {}), **kv}
        session.execute(
            t.update().where(t.c[pk_name] == row_id).values(extras=merged)
        )
