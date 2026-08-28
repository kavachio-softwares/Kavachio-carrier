"""Write a merged canonical record into the relational warehouse.

Input  (one entry from mapper merge):
    {
      "tenant":     {...},           # optional
      "program":    {...},
      "policy":     {...},
      "coverage":   [{...}, ...] | {...}
      "premium_transaction": [...]
      "insured_location": [...]
      "building":   [...]
      "party_address":  [...]        # ambient agent/broker info from POL Data
      "party_contact":  [...]
      "party_license":  [...]
      "claim":      [...]
      ...
    }

Strategy
--------
1. Insert / fetch a `tenant` row (PK → tenant_id).
2. Upsert `program` keyed on (tenant_id, program_name).
3. Upsert `policy` keyed on policy_number. Set program_id from step 2.
4. For each child table whose schema has a `policy_id` column, insert one row
   per array entry with policy_id wired to the policy we just inserted.
5. party_address / party_contact / party_license: if no party_id is supplied,
   we synthesise one "ambient" party per policy so the child rows have a real
   FK to attach to. (Better-than-NULL: lets you query "all addresses for this
   policy's contributing parties".)
6. The function returns the policy_id (or None if no policy was created),
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

from canonical import CANONICAL_TABLES, column_names, pk_column

# Tables that, when present in a record, hang off the policy via policy_id.
POLICY_CHILDREN = (
    "coverage", "premium_transaction", "premium_invoice",
    "insured_location", "policy_attributes",
    "claim", "parametric_coverage_detail", "party_role_in_policy",
)

# Tables that hang off premium_transaction.transaction_id (NOT policy_id).
TRANSACTION_CHILDREN = ("policy_fee", "tax_or_surcharge", "commission")

# Tables that hang off a party via party_id.
PARTY_CHILDREN = ("party_address", "party_contact", "party_license", "party_relationship")

# Tables that hang off insured_location via location_id.
LOCATION_CHILDREN = ("building",)


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
# This used to be "new", which ASSERTED new business on a row we failed to
# classify — a false statement, and an unrecoverable one, because a defaulted row
# then looks byte-identical to a genuine new-business row. The warehouse still
# carries 8 455 rows typed 'new' with a NEGATIVE premium; new business is never
# negative, so those are cancellations and reversals whose code did not map, and
# their original values are gone.
#
# 'unknown' rather than Palms' 'UNK' because we already translate their shortcode
# vocabulary at ingest (NB→new, EN→endorsement) and the enum is lowercase
# snake_case throughout. Requires the enum value — see
# scripts/add_unknown_transaction_type.py. The OUTPUT file carries 'UNK' instead
# (bdx_defaults.OUTPUT_DEFAULT_TRANSACTION_TYPE); the value itself is defined in
# bdx_defaults and imported above.

# BDX shortcode → canonical enum value for transaction_type.
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
    ("policy", "transaction_type"): _TXN_TYPE_MAP,
    ("premium_transaction", "transaction_type"): _TXN_TYPE_MAP,
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
    lookups (tenant / contract / carrier / admin party). A 10k-row upload shares
    one Session, so without this each of these is re-resolved with a DB
    round-trip on EVERY row; here they're resolved once and reused. Stored on
    session.info; only used for rows that don't change within an upload."""
    cache = session.info.setdefault("_ingest_memo", {}).setdefault(bucket, {})
    if key not in cache:
        cache[key] = factory()
    return cache[key]


def _ensure_tenant(session: Session, mga_name: str) -> int:
    """Find-or-create a tenant row keyed by tenant_name (the MGA identifier).

    Supplies sensible defaults for NOT NULL columns that the canonical Postgres
    schema enforces (data_residency_region, internal_codes, is_active).
    """
    def _resolve():
        t = CANONICAL_TABLES["tenant"]
        row = session.execute(select(t.c.tenant_id).where(t.c.tenant_name == mga_name)).fetchone()
        if row:
            return row[0]
        defaults = {
            "tenant_name": mga_name,
            "tenant_type": "mga",
            "data_residency_region": "us-east",
            "internal_codes": {},
            "is_active": True,
        }
        # Only pass columns the table actually has.
        payload = {k: v for k, v in defaults.items() if k in t.c}
        return _insert_row(session, "tenant", payload)
    return _memo(session, "tenant", mga_name, _resolve)


def _ensure_party_with_natural_id(
    session: Session, tenant_id: int, natural_id: str,
    party_type: str = "agency", legal_name: str | None = None,
) -> int | None:
    """Find-or-create a party row keyed by tenant_id + party_natural_id."""
    if "party" not in CANONICAL_TABLES:
        return None
    pt = CANONICAL_TABLES["party"]
    row = session.execute(
        select(pt.c.party_id, pt.c.legal_name)
        .where(pt.c.tenant_id == tenant_id)
        .where(pt.c.party_natural_id == natural_id)
    ).fetchone()
    if row:
        party_id, current = row[0], row[1]
        # Re-ingest: refresh the name when a real one is now supplied (e.g. the
        # mapping was corrected so an insured/carrier name is finally mapped).
        if legal_name and str(legal_name).strip() and legal_name != current:
            session.execute(
                pt.update().where(pt.c.party_id == party_id)
                .values(legal_name=legal_name)
            )
        return party_id
    return _insert_row(session, "party", _filter_to_schema("party", {
        "tenant_id": tenant_id,
        "party_natural_id": natural_id,
        "party_type": party_type,
        "legal_name": legal_name or natural_id,
        "is_organization": True,
        "is_active": True,
    }))


def _ensure_carrier_party(session: Session, tenant_id: int,
                          legal_name: str | None = None) -> int | None:
    """Find-or-create the carrier party. When a real carrier name is supplied
    (e.g. mapped from an 'Issuing Company' column) the party is keyed by that
    name so distinct carriers get distinct rows; otherwise a single generic
    per-tenant carrier is used (legacy behaviour)."""
    name = (legal_name or "").strip()

    def _resolve():
        if name:
            natural = f"carrier::{tenant_id}::{name.lower()}"
            return _ensure_party_with_natural_id(
                session, tenant_id, natural, party_type="carrier", legal_name=name,
            )
        return _ensure_party_with_natural_id(
            session, tenant_id, f"carrier::{tenant_id}",
            party_type="carrier", legal_name=f"Tenant {tenant_id} Carrier",
        )
    return _memo(session, "carrier", (tenant_id, name.lower()), _resolve)


def _ensure_contract(session: Session, tenant_id: int, program_id: int) -> int | None:
    """Find-or-create a placeholder contract per (tenant, program)."""
    if "contract" not in CANONICAL_TABLES:
        return None

    def _resolve():
        t = CANONICAL_TABLES["contract"]
        umr = f"auto::{tenant_id}::{program_id}"
        row = session.execute(
            select(t.c.contract_id).where(t.c.umr == umr)
        ).fetchone()
        if row:
            return row[0]
        return _insert_row(session, "contract", _filter_to_schema("contract", {
            "tenant_id": tenant_id,
            "program_id": program_id,
            "umr": umr,
            "contract_name": f"Auto contract for program {program_id}",
            "contract_type": "binding_authority",
            "inception_dt": date(1970, 1, 1),
            "expiry_dt": date(2999, 12, 31),
            "is_active": True,
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
    That is not theoretical here — program 165 carries 48 contracts and program
    351 carries 38.

    WHY THE PLACEHOLDERS ARE EXCLUDED
    _ensure_contract mints one auto:: contract per (tenant, program) spanning
    1970-01-01 to 2999-12-31. That window matches EVERY date, so without the
    umr filter below the placeholder always wins the join and the effective
    date is never consulted — which is exactly the state this function exists
    to end (21 901 of 22 500 policies currently point at one).

    OVERLAPS: if two real contracts both cover the date the LATEST inception
    wins, so the result is deterministic rather than dependent on row order.
    That is a tie-break, not a licence — overlapping windows are a data problem
    and the warning below is what surfaces them.

    NO MATCH: falls back to the placeholder, exactly as before, and logs. A
    policy whose date no contract covers therefore behaves identically to
    today. Chosen deliberately over rejecting the row (Palms' JOIN would drop
    it): most historic rows predate any real contract, so a hard filter would
    silently discard them. Count the warnings first, tighten afterwards.
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
            .where(t.c.tenant_id == tenant_id)
            .where(t.c.program_id == program_id)
            .where(t.c.inception_dt <= pol_eff_dt)   # >= inception_dt
            .where(t.c.expiry_dt > pol_eff_dt)       # <  expiry_dt  (§2.10)
            .order_by(t.c.inception_dt.desc())
            .limit(1)
        )
        # The auto:: placeholders span all of time; they must never win here.
        if "umr" in t.c:
            stmt = stmt.where(or_(t.c.umr.is_(None), ~t.c.umr.like("auto::%")))
        if "is_active" in t.c:
            stmt = stmt.where(t.c.is_active.is_(True))
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


def _ensure_canonical_upload(
    session: Session, tenant_id: int, filename: str, file_year: int, file_month: int,
    load_type: str = "incremental", mapping_profile_id: int | None = None,
    invalidates_upload_id: int | None = None, num_rows: int | None = None,
) -> int | None:
    """Create an upload row on the canonical side; one per /bdx/upload call.

    Captures lineage: which mapping profile interpreted the file, how the load
    relates to prior loads (incremental/cumulative/restatement), and — for
    restatements — the canonical upload it supersedes.
    """
    if "upload" not in CANONICAL_TABLES:
        return None
    import hashlib, time
    file_hash = hashlib.sha256(f"{filename}::{time.time()}".encode()).hexdigest()
    carrier_id = _ensure_carrier_party(session, tenant_id)
    return _insert_row(session, "upload", _filter_to_schema("upload", {
        "tenant_id": tenant_id,
        "carrier_party_id": carrier_id,
        "bdx_type": "premium_program",
        "file_year": file_year,
        "file_month": file_month,
        "filename": filename,
        "file_hash": file_hash,
        "load_type": load_type,
        "mapping_profile_id": mapping_profile_id,
        "invalidates_upload_id": invalidates_upload_id,
        "num_rows": num_rows,
        "is_approved": True,
    }))


def _ensure_admin_party(session: Session, tenant_id: int) -> int | None:
    """Find-or-create the per-tenant 'program admin' party used as a placeholder
    for FK columns like program.program_admin_party_id (NOT NULL on Postgres)."""
    if "party" not in CANONICAL_TABLES:
        return None

    def _resolve():
        pt = CANONICAL_TABLES["party"]
        natural = f"tenant_admin::{tenant_id}"
        row = session.execute(
            select(pt.c.party_id).where(pt.c.party_natural_id == natural)
        ).fetchone()
        if row:
            return row[0]
        return _insert_row(session, "party", _filter_to_schema("party", {
            "tenant_id": tenant_id,
            "party_natural_id": natural,
            "party_type": "mga",
            "legal_name": f"Tenant {tenant_id} Admin",
            "is_organization": True,
            "is_active": True,
        }))
    return _memo(session, "admin", tenant_id, _resolve)


def _upsert_program(session: Session, tenant_id: int, payload: dict) -> int | None:
    if not payload:
        return None
    name = payload.get("program_name")
    if not name:
        return None
    t = CANONICAL_TABLES["program"]
    row = session.execute(
        select(t.c.program_id)
        .where(t.c.tenant_id == tenant_id)
        .where(t.c.program_name == name)
    ).fetchone()
    if row:
        return row[0]
    admin_party_id = _ensure_admin_party(session, tenant_id)
    carrier_party_id = _ensure_carrier_party(session, tenant_id)
    values = _filter_to_schema("program", {
        **payload,
        "tenant_id": tenant_id,
        "program_admin_party_id": admin_party_id,
        "lead_carrier_party_id": carrier_party_id,
        "is_active": True,
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
                   payload: dict) -> tuple[int | None, bool]:
    """Returns (policy_id, is_new). `is_new` is True when this call CREATED the
    policy (no prior row existed) — the caller can then skip the re-ingest
    child-cleanup, which is a no-op for a brand-new policy but costs ~a dozen
    round-trips per row."""
    if not payload:
        return None, False
    polno = payload.get("policy_number")
    t = CANONICAL_TABLES["policy"]

    # Synthesise the FK-required placeholders the canonical schema demands.
    # (Carrier/insured party helpers also REFRESH their names on re-ingest.)
    # §2.10: bind the policy to the contract in force on its EFFECTIVE DATE,
    # not to a catch-all placeholder. Falls back to the placeholder (previous
    # behaviour) when no real contract covers the date, so nothing that works
    # today stops working.
    contract_id = _resolve_contract(
        session, tenant_id, program_id, payload.get("policy_effective_dt")
    ) if program_id else None
    # Carrier party: named from the mapped carrier/issuing-company column when
    # present, else the generic per-tenant carrier.
    carrier_party_id = _ensure_carrier_party(
        session, tenant_id, legal_name=payload.get("carrier_legal_name"))
    # §2.7: COALESCE(NULLIF(TRIM(insured_name), ''), 'Unknown'). The strip IS the
    # TRIM/NULLIF half — without it a whitespace-only cell survives as a name.
    # This used to fall back to the POLICY NUMBER, which put companies literally
    # called "STI-00025-25" in the party directory. Nothing is lost by naming the
    # party honestly: the natural id already carries the policy number, so the
    # link back to the policy is unchanged.
    _insured = payload.get("insured_legal_name")
    _insured = _insured.strip() if isinstance(_insured, str) else _insured
    insured_party_id = _ensure_party_with_natural_id(
        session, tenant_id, f"insured::{polno}",
        party_type="insured",
        legal_name=_insured or _DEFAULT_INSURED_NAME,
    )
    nor = (payload.get("new_or_renewal") or "").strip().upper()
    txn_type = _TXN_TYPE_MAP.get(nor, _DEFAULT_TRANSACTION_TYPE)

    base = _filter_to_schema("policy", {
        **payload,
        "tenant_id": tenant_id,
        "program_id": program_id,
        "contract_id": contract_id,
        "insured_party_id": insured_party_id,
        "risk_bearing_carrier_party_id": carrier_party_id,
        "writing_company_party_id": carrier_party_id,
        "transaction_type": txn_type,
    })

    if polno:
        stmt = (
            select(t.c.policy_id)
            .where(t.c.tenant_id == tenant_id)
            .where(t.c.policy_number == polno)
        )
        # A 'Modify here' edit can leave a retired (is_current_version=FALSE)
        # version alongside the active one for the same (tenant_id, policy_number).
        # Re-ingest must update the ACTIVE version, never a superseded one. NULL
        # (legacy, never-versioned rows) counts as active, so this is a no-op for
        # un-versioned data.
        if "is_current_version" in t.c:
            stmt = stmt.where(t.c.is_current_version.isnot(False)).order_by(t.c.policy_id.desc())
        row = session.execute(stmt).fetchone()
        if row:
            policy_id = row[0]
            # Re-ingest (Option A — sheet wins, prior edit kept as history): if the
            # sheet changed anything, retire the current active policy version and
            # insert a new active version with the SAME policy_id carrying the sheet
            # values. Unchanged → leave as-is (no churn). Only columns the payload
            # carries are set, so placeholder defaults never clobber real values.
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
    if "policy_effective_dt" in t.c and not values.get("policy_effective_dt"):
        # UNREACHABLE via ingest_record, which now drops a record with no
        # effective date before it gets here. Kept as a backstop because the
        # column is NOT NULL and a future caller could reach _upsert_policy
        # directly — but it logs at ERROR, because a 1970 date silently
        # misattributes the contract, the commission band and the accounting
        # period. If this never fires, the fallback can be deleted outright.
        log.error("policy %s reached _upsert_policy with no effective date — "
                  "the ingest_record guard was bypassed; storing the 1970 "
                  "sentinel, which WILL misattribute contract and commission",
                  polno)
        values["policy_effective_dt"] = date(1970, 1, 1)
    if "policy_expiration_dt" in t.c and not values.get("policy_expiration_dt"):
        values["policy_expiration_dt"] = date(2999, 12, 31)
    return _insert_row(session, "policy", values), True


def _child_defaults(table_name: str, parent_ctx: dict) -> dict:
    """Per-table defaults to satisfy canonical Postgres NOT NULL constraints
    that BDX rows commonly don't carry."""
    pol = parent_ctx.get("policy_payload") or {}
    # Children only build after the parent policy exists, and ingest_record
    # drops a record with no effective date before creating one — so the
    # sentinel below is a backstop for an unparseable date string, not for a
    # missing one. Both cases log, for the same reason as _upsert_policy: a
    # 1970 date reaches booking_dt and accounting_period and looks valid.
    eff = pol.get("policy_effective_dt")
    if not eff:
        log.error("_child_defaults: no policy_effective_dt on the parent policy "
                  "— falling back to the 1970 sentinel")
        eff = date(1970, 1, 1)
    if isinstance(eff, str):
        try:
            eff = datetime.fromisoformat(eff).date()
        except (ValueError, TypeError):
            log.error("_child_defaults: unparseable policy_effective_dt %r — "
                      "falling back to the 1970 sentinel", eff)
            eff = date(1970, 1, 1)

    if table_name == "coverage":
        return {"coverage_type": "GL"}
    if table_name == "premium_transaction":
        from datetime import date as _date
        nor = (pol.get("new_or_renewal") or "").strip().upper()
        # booking_dt is part of the PK — must be non-null.
        # Fall back to today when the source record has no effective date.
        booking = eff if eff is not None else _date.today()
        period = (
            booking.strftime("%Y-%m") if hasattr(booking, "strftime") else "1970-01"
        )
        return {
            "transaction_type": _TXN_TYPE_MAP.get(nor, _DEFAULT_TRANSACTION_TYPE),
            "transaction_effective_dt": eff,
            "booking_dt": booking,
            "accounting_period": period,
            "original_currency": pol.get("currency_iso") or _DEFAULT_CURRENCY,
            # §2.7: COALESCE(exchg_rate, 1.0). No conversion happens at ingest,
            # so the rate is par by default. A BARE 1.0 is indistinguishable from
            # a genuine same-currency rate, though, and BRD §4.4 requires the
            # rate's SOURCE and AS-OF DATE on every converted record. Hence the
            # convention:
            #
            #     fx_rate_date IS NULL  ==  the rate was DEFAULTED, never sourced
            #
            # A looked-up rate always carries its as-of date (§4.4 demands it),
            # so the two can never be confused, and every defaulted row is
            # findable with one predicate — no schema change needed, because
            # premium_transaction has no rate_source column (that lives on
            # fx_rate). The genuinely dangerous case, a non-USD amount sitting at
            # par, is then a single query away.
            "fx_rate_to_reporting_ccy": _DEFAULT_FX_RATE,
            "fx_rate_date": None,
        }
    if table_name == "party_address":
        return {"address_type": "mailing"}
    if table_name == "party_license":
        return {"license_state": "XX", "license_type": "producer"}
    ccy = pol.get("currency_iso") or _DEFAULT_CURRENCY
    if table_name == "policy_fee":
        return {"fee_type": "admin", "fee_amount": _DEFAULT_AMOUNT,
                "currency_iso": ccy}
    if table_name == "tax_or_surcharge":
        return {"tax_type": "premium_tax", "tax_amount": _DEFAULT_AMOUNT,
                "currency_iso": ccy}
    if table_name == "commission":
        return {
            "commission_type": "producing_broker",
            "commission_amount": _DEFAULT_AMOUNT,
            "currency_iso": ccy,
        }
    return {}


def _insert_children(
    session: Session, table_name: str, items: Iterable[dict],
    parent_ctx: dict | None = None, **fks: int | None,
) -> list[int]:
    """Insert a list of dicts as rows of `table_name`, stamped with the given FKs
    and any per-table required defaults."""
    ids: list[int] = []
    cols = column_names(table_name)
    defaults = _child_defaults(table_name, parent_ctx or {})

    # party_role_in_policy carries a natural key: ONE row per (party, role) on a
    # policy. Cross-RECORD duplication is already prevented — _clear_policy_children
    # supersedes the policy's existing role rows before the fresh ones go in, so the
    # last row in the file wins (the same row Appendix 2 §2.4's "ORDER BY
    # policycontact_id DESC" would pick). Nothing supersedes between items INSIDE
    # one record though, so a merged record emitting the same role twice would leave
    # two ACTIVE rows and multiply the policy on every join that touches it.
    # Last occurrence wins here too: dict keeps insertion order and a later key
    # overwrites an earlier one, so the survivor is deterministic.
    if table_name == "party_role_in_policy":
        items = list(items)
        if items and all(isinstance(r, dict) for r in items):
            items = list({(r.get("party_id"), r.get("role")): r
                          for r in items}.values())

    for raw in items:
        merged = {**defaults, **raw}
        for fk, val in fks.items():
            if val is not None and fk in cols:
                merged[fk] = val
        values = _filter_to_schema(table_name, merged)
        if not values:
            continue
        # Skip rows that can't satisfy the table's own required-field check.
        if table_name == "party_contact":
            if not values.get("contact_method") or not values.get("contact_value"):
                continue
        if table_name == "party_license" and not values.get("license_number"):
            continue
        if table_name == "party_address" and not values.get("address_line1"):
            continue
        if table_name == "policy_attributes":
            # NOT NULL columns: attribute_key, scope. The LLM may suggest a
            # mapping that only fills attribute_value (or scope/value_type)
            # without the key — that row would violate the PG constraint, so
            # skip silently rather than 500 the whole ingest.
            if not values.get("attribute_key") or not values.get("scope"):
                continue
        if table_name == "premium_transaction" and not values.get("booking_dt"):
            # booking_dt is part of the PK — cannot be NULL. The merged record
            # may have explicitly set it to None (mapper found no matching column).
            # Fall back to transaction_effective_dt, or today as last resort.
            from datetime import date as _date
            values["booking_dt"] = values.get("transaction_effective_dt") or _date.today()
            # Re-derive accounting_period now that we have a valid date.
            bdt = values["booking_dt"]
            if hasattr(bdt, "strftime"):
                values["accounting_period"] = bdt.strftime("%Y-%m")
        if (table_name == "premium_invoice"
                and values.get("invoice_ref") is not None
                and values.get("tenant_id") is not None):
            # invoice_ref is UNIQUE per tenant (premium_invoice_ref_uq). The same
            # invoice legitimately recurs — one invoice can cover several
            # transactions, and re-ingests replay the same refs — so find-or-create
            # rather than blind-insert, which would raise UniqueViolation.
            inv_t = CANONICAL_TABLES["premium_invoice"]
            inv_pk = pk_column("premium_invoice")
            existing = session.execute(
                select(inv_t.c[inv_pk]).where(
                    inv_t.c.tenant_id == values["tenant_id"],
                    inv_t.c.invoice_ref == values["invoice_ref"],
                )
            ).scalar()
            if existing is not None:
                ids.append(existing)
                continue
        pk = _insert_row(session, table_name, values)
        if pk:
            ids.append(pk)
    return ids


def _ensure_ambient_party(session: Session, tenant_id: int, label: str) -> int | None:
    """Find-or-create a synthetic party so party_address/contact/license have a FK.

    Idempotent on re-upload: keyed by (tenant_id, party_natural_id) it reuses the
    existing ambient party instead of blind-inserting a duplicate (which violated
    the uq_party_tenant_natural unique constraint on the second ingest)."""
    return _ensure_party_with_natural_id(
        session, tenant_id, label, party_type="agency", legal_name=label)


def _clear_policy_children(session: Session, policy_id: int, policy_number: str | None) -> None:
    """Re-ingest cleanup (Option A — sheet wins, prior edits kept as history):
    RETIRE the policy's existing canonical child rows (mark them inactive SCD-2
    history) instead of deleting them, so a re-upload preserves prior edits; the
    caller then inserts the fresh sheet rows as the new ACTIVE versions. Tables
    without SCD columns (ops-shadowed) are still deleted so they don't accumulate.
    """
    # 1a. transaction-children first (keyed by this policy's active transaction_ids)
    pt = CANONICAL_TABLES.get("premium_transaction")
    if pt is not None and "policy_id" in pt.c:
        tstmt = select(pt.c.transaction_id).where(pt.c.policy_id == policy_id)
        if "is_current_version" in pt.c:
            tstmt = tstmt.where(pt.c.is_current_version.isnot(False))
        txn_ids = [r[0] for r in session.execute(tstmt).fetchall()]
        if txn_ids:
            for child in TRANSACTION_CHILDREN:
                ct = CANONICAL_TABLES.get(child)
                if ct is not None and "transaction_id" in ct.c:
                    _supersede(session, ct, ct.c.transaction_id.in_(txn_ids))

    # 1b. location-children (e.g. building) keyed by this policy's location_ids
    loc_table = CANONICAL_TABLES.get("insured_location")
    if loc_table is not None and "policy_id" in loc_table.c:
        lstmt = select(loc_table.c.location_id).where(loc_table.c.policy_id == policy_id)
        if "is_current_version" in loc_table.c:
            lstmt = lstmt.where(loc_table.c.is_current_version.isnot(False))
        loc_ids = [r[0] for r in session.execute(lstmt).fetchall()]
        if loc_ids:
            for child in LOCATION_CHILDREN:
                ct = CANONICAL_TABLES.get(child)
                if ct is not None and "location_id" in ct.c:
                    _supersede(session, ct, ct.c.location_id.in_(loc_ids))

    # policy-children (incl. premium_transaction itself)
    for table_name in POLICY_CHILDREN:
        t = CANONICAL_TABLES.get(table_name)
        if t is None or "policy_id" not in t.c:
            continue
        _supersede(session, t, t.c.policy_id == policy_id)

    # buildings also keyed by policy_id (defensive)
    bld = CANONICAL_TABLES.get("building")
    if bld is not None and "policy_id" in bld.c:
        _supersede(session, bld, bld.c.policy_id == policy_id)

    # ambient party CHILDREN — retire (versioned) / delete (non-versioned). The
    # ambient party row itself is kept and reused (find-or-create) so retired
    # children aren't orphaned.
    if policy_number and "party" in CANONICAL_TABLES:
        party_t = CANONICAL_TABLES["party"]
        natural = f"ambient::{policy_number}"
        row = session.execute(
            select(party_t.c.party_id).where(party_t.c.party_natural_id == natural)
        ).fetchone()
        if row:
            ambient_id = row[0]
            for child in PARTY_CHILDREN:
                ct = CANONICAL_TABLES.get(child)
                if ct is not None and "party_id" in ct.c:
                    _supersede(session, ct, ct.c.party_id == ambient_id)


def ingest_record(session: Session, mga: str, record: dict,
                  canonical_upload_id: int | None = None) -> int | None:
    """Ingest one merged record. Returns the policy_id created (or None).

    Idempotent: if the policy already exists, its child canonical rows are
    wiped and re-inserted with the freshly merged data.

    `canonical_upload_id` (when supplied) is stamped on premium_transaction
    rows to satisfy the canonical schema's NOT NULL upload_id constraint.
    """
    tenant_id = _ensure_tenant(session, mga)
    program_id = _upsert_program(session, tenant_id, record.get("program") or {})
    pol_payload = record.get("policy") or {}
    # Bridge: a mapper that targets the generic `legal_name` (party.legal_name)
    # for an insured-name column lands the value under record["party"]. When the
    # policy payload has no explicit insured name, treat that as the insured's
    # legal name so it reaches the insured party (and the output template).
    # `party` may arrive as a dict (single row) or a list (after _merge_records
    # treats it as a collection) — handle both, and tolerate junk shapes.
    if not pol_payload.get("insured_legal_name"):
        party_blob = record.get("party")
        if isinstance(party_blob, list):
            party_blob = next((p for p in party_blob
                               if isinstance(p, dict) and p.get("legal_name")), None)
        if isinstance(party_blob, dict) and party_blob.get("legal_name"):
            pol_payload = {**pol_payload, "insured_legal_name": party_blob["legal_name"]}
    polno = (pol_payload.get("policy_number") or "")
    polno = polno.strip() if isinstance(polno, str) else polno
    if not polno or (isinstance(polno, str)
                     and polno.casefold() in _POLICY_NUMBER_PLACEHOLDERS):
        # `policy.policy_number` is NOT NULL in canonical Postgres. If the
        # mapping spec didn't produce one for this record (often the case
        # for non-POL sheets that lack a PolicyNumber column), skip the
        # whole record rather than 500-ing the ingest.
        #
        # A PLACEHOLDER is rejected the same way. "0" / "-" is what a person
        # types into a cell they can't fill: a truthy string that sails past a
        # plain falsy check, then becomes a policy AND a party natural id
        # ("insured::-", see _ensure_party_with_natural_id) — so every row
        # carrying the same placeholder collides onto one bogus party.
        log.warning("ingest_record: skipping record with invalid policy_number "
                    "%r (other keys=%s)", polno, list(record.keys()))
        return None
    if not pol_payload.get("policy_effective_dt"):
        # The policy effective date is the join key for contract resolution, the
        # commission rate band, and the accounting period. Substituting one does
        # not produce a single wrong field — it resolves all three confidently to
        # the wrong answer, silently. So a missing date is a HARD FILTER, not a
        # fallback (Palms BDX BRD v1.2, Appendix 2 §2.2), the same treatment a
        # missing policy number already gets above.
        log.warning("ingest_record: skipping policy %s with no "
                    "policy_effective_dt", polno)
        return None
    policy_id, policy_is_new = _upsert_policy(session, tenant_id, program_id, pol_payload)
    # Re-ingest cleanup retires a policy's existing child rows before re-inserting.
    # A brand-new policy has none, so skip it — that saves ~a dozen DB round-trips
    # per row, which dominates the cost of a fresh (10k-row) upload.
    if policy_id is not None and not policy_is_new:
        _clear_policy_children(session, policy_id, polno)

    ctx = {"policy_payload": pol_payload}

    # Children that hang off the policy.
    location_ids: list[int] = []
    txn_ids: list[int] = []
    for table in POLICY_CHILDREN:
        items = _as_list(record.get(table))
        if not items:
            continue
        extra_fks: dict[str, int | None] = {
            "policy_id": policy_id,
            "tenant_id": tenant_id,
        }
        if table == "premium_transaction":
            extra_fks["upload_id"] = canonical_upload_id
        ids = _insert_children(session, table, items, parent_ctx=ctx, **extra_fks)
        if table == "insured_location":
            location_ids = ids
        elif table == "premium_transaction":
            txn_ids = ids

    # Children that hang off premium_transaction. If the BDX provides taxes/
    # commissions/fees without an explicit premium_transaction in the record,
    # we still need a transaction to attach them to — synthesise one.
    needs_txn_parent = any(record.get(t) for t in TRANSACTION_CHILDREN)
    if needs_txn_parent and not txn_ids:
        ids = _insert_children(
            session, "premium_transaction", [{}], parent_ctx=ctx,
            policy_id=policy_id, tenant_id=tenant_id, upload_id=canonical_upload_id,
        )
        txn_ids = ids
    parent_txn_id = txn_ids[0] if txn_ids else None
    admin_party_id = None  # lazily resolved if commission rows exist
    for table in TRANSACTION_CHILDREN:
        items = _as_list(record.get(table))
        if not items or parent_txn_id is None:
            continue
        extra: dict[str, int | None] = {
            "transaction_id": parent_txn_id, "tenant_id": tenant_id,
        }
        if table == "commission":
            if admin_party_id is None:
                admin_party_id = _ensure_admin_party(session, tenant_id)
            extra["recipient_party_id"] = admin_party_id
        _insert_children(session, table, items, parent_ctx=ctx, **extra)

    # Buildings: one per location row (best-effort 1:1 by order)
    buildings = _as_list(record.get("building"))
    for i, bld in enumerate(buildings):
        loc_id = location_ids[i] if i < len(location_ids) else (location_ids[0] if location_ids else None)
        _insert_children(session, "building", [bld], parent_ctx=ctx,
                         location_id=loc_id, tenant_id=tenant_id)

    # Party-side tables: create an ambient party once if any of these are present.
    needs_party = any(record.get(t) for t in PARTY_CHILDREN)
    if needs_party:
        label = f"ambient::{polno or 'unknown'}"
        party_id = _ensure_ambient_party(session, tenant_id, label)
        for table in PARTY_CHILDREN:
            _insert_children(session, table, _as_list(record.get(table)),
                             parent_ctx=ctx,
                             party_id=party_id, tenant_id=tenant_id)

    # User-defined extra fields. The mapper has already grouped them by
    # target entity. Write each group onto that entity's `extras` JSONB
    # column. Entities we don't have an id for yet (e.g. no claim row was
    # created in this record) are skipped.
    extras_by_entity = record.get("extras") or {}
    if isinstance(extras_by_entity, dict) and extras_by_entity:
        _write_entity_extras(
            session, extras_by_entity,
            policy_id=policy_id,
            # `claim` ids come from the children loop above — collect them.
            entity_ids={
                "policy": policy_id,
                # children that just got inserted have their ids in `ids`
                # but ingest_record doesn't keep them around per-table.
                # Fetch the latest rows for this policy_id for the entities
                # that actually had data on this record.
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
            if row_id is None and policy_id is not None and "policy_id" in t.c:
                row = session.execute(
                    select(t.c[pk_name]).where(t.c.policy_id == policy_id)
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
