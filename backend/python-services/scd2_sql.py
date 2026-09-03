"""SCD-2 "Modify here" support: reverse-resolve an output-template column to a
concrete canonical (table, column, row) and GENERATE the SQL that versions it.

Design (SAME-ID versioning, per user decision):
  * A new version REUSES the same surrogate id (policy_id / coverage_id / …) and
    is distinguished by `version_no` + `is_current_version`. So one logical entity
    has several rows that share its id — which is exactly why the single-column
    primary key is dropped (see scripts/scd2_modify_here.sql). The effective key
    becomes (id, version_no).
  * The previous row is retired: is_current_version = FALSE, valid_until = now().
  * The new row is inserted as a COPY of the latest row, same id, version_no + 1,
    is_current_version = TRUE, with the edited value. No surrogate id ever changes,
    so child rows / upload_policy / FKs need NO re-pointing.
  * Reads pick the active version via `is_current_version IS NOT FALSE`
    (assembler._active), so the superseded version is hidden from the export.

Scope = the pure-canonical per-policy tables the assembler assembles into a policy
(VERSIONED_TABLES). Two distinct reasons keep tables OUT of scope:
  * tenant / program / contract — shared ancestors; one row serves many policies,
    so per-policy versioning would wrongly affect siblings.
  * party / policyholder — party is physically shadowed by an ops table (db.py)
    with no SCD columns; policyholder is a shared ancestor (one insured serves
    many policies), so a per-policy edit would wrongly affect siblings.
Tables defined in data_model.py but never assembled/ingested (cession,
reinsurance_arrangement, …) also can't be flagged or edited.

NOTHING here writes to the database. `build_scd2_sql` returns a self-contained
PostgreSQL transaction the operator runs by hand. Re-validation (so an edit can't
introduce a NEW exception) happens in validation_routes BEFORE this SQL is offered.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text

from canonical import CANONICAL_TABLES, pk_column

# The pure-canonical, per-policy tables that can be edited + versioned. Every one
# of these is (a) NOT shadowed by an ops table (so it physically has the SCD
# columns), and (b) assembled into a policy by assembler.fetch_policy (so a
# flagged output value can be traced to one of its rows). Shared ancestors
# (tenant/program/contract) and shadowed tables (party, party_contact) are out of
# scope. Keep this in sync with scripts/scd2_modify_here.sql and assembler._active.
VERSIONED_TABLES = {
    "policy", "coverage", "premium_transaction", "risk_location", "claim",
    "claim_transaction", "claim_fee_line", "claim_reserve",
    "tax_line", "commission_line", "coverage_participation",
    "party_license",
}

# SCD-2 bookkeeping columns set explicitly on the new version (never blind-copied).
_SCD_COLS = ("is_current_version", "valid_from", "valid_until", "version_no")

# valid_from/valid_until are NOT NULL on the live schema; the convention for an
# OPEN (current) row is the far-future sentinel, not NULL.
_ACTIVE_VALID_UNTIL = "TIMESTAMP '2999-12-31'"

# Columns the operator's one-time DDL must add before any edit can be versioned.
REQUIRED_SCD_COLUMNS = ("is_current_version", "valid_from", "valid_until", "version_no")


# ---- value → SQL literal ----------------------------------------------------

def sql_literal(value: Any) -> str:
    """Render a typed Python value as a PostgreSQL literal for embedding in the
    generated SQL text. Strings are single-quote escaped."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return "TIMESTAMP '" + value.isoformat(sep=" ") + "'"
    if isinstance(value, date):
        return "DATE '" + value.isoformat() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def coerce_value(table: str, column: str, raw: Any) -> Any:
    """Coerce a user-entered value to the canonical column's type (reuses the
    ingester's coercion so dates/booleans/numbers parse identically)."""
    from ingester import _coerce  # lazy: ingester imports heavily
    t = CANONICAL_TABLES.get(table)
    if t is None or column not in t.c:
        return raw
    return _coerce(raw, t.c[column].type)


# ---- schema inspection (read-only) -----------------------------------------

def physical_columns(s, table: str) -> set[str]:
    """The columns that PHYSICALLY exist on `table` in the live database.

    Used so the generated INSERT only copies columns that actually exist (the
    model carries a version_no-less definition and a dynamically-appended
    `extras` column that may be absent in some deployments)."""
    rows = s.execute(
        text("SELECT column_name FROM information_schema.columns "
             "WHERE table_name = :t AND table_schema = current_schema()"),
        {"t": table},
    ).fetchall()
    return {r[0] for r in rows}


def missing_scd_columns(s, table: str) -> list[str]:
    present = physical_columns(s, table)
    return [c for c in REQUIRED_SCD_COLUMNS if c not in present]


def pk_is_identity_always(s, table: str) -> bool:
    """True if the table's surrogate id is a GENERATED ALWAYS AS IDENTITY column.

    Such a column rejects an explicit value on INSERT unless OVERRIDING SYSTEM
    VALUE is given — and same-id versioning supplies the id explicitly. (On the
    live DB all 15 versioned tables are GENERATED ALWAYS.)"""
    col = pk_column(table)
    r = s.execute(
        text("SELECT identity_generation FROM information_schema.columns "
             "WHERE table_name = :t AND column_name = :c AND table_schema = current_schema()"),
        {"t": table, "c": col},
    ).scalar()
    return r == "ALWAYS"


# ---- field_path → canonical (table, column) ---------------------------------

def _load_structure(s, template_id: int) -> Optional[dict]:
    import json as _json
    raw = s.execute(
        text("SELECT structure FROM export_templates WHERE id = :t"),
        {"t": template_id},
    ).scalar()
    if not raw:
        return None
    return _json.loads(raw) if isinstance(raw, str) else raw


def _find_column_entry(structure: dict, field_path: str) -> Optional[dict]:
    """Find the template column entry whose output name == field_path.

    Case-insensitive: the exception's field_path and the template column name both
    originate from spreadsheet headers but pass through separate services, so a
    casing difference shouldn't silently fail the lookup."""
    target = (field_path or "").strip().lower()
    for sh in structure.get("sheets", []):
        for c in sh.get("columns", []):
            if (c.get("column_name") or "").strip().lower() == target:
                return c
    return None


def resolve_field(s, template_id: int, field_path: str) -> dict:
    """Map an output column name to a concrete canonical (table, column).

    Returns a dict with `editable` plus, when editable, table/column/transform.
    When not editable, `reason` explains why (so the UI can grey the row out).
    """
    if not field_path:
        return {"editable": False, "reason": "no field name on this exception"}
    structure = _load_structure(s, template_id)
    if not structure:
        return {"editable": False, "reason": "output template has no structure"}

    entry = _find_column_entry(structure, field_path)
    if entry is None:
        return {"editable": False,
                "reason": f"'{field_path}' is not a column in this output template "
                          "(likely an aggregate/derived rule with no single source cell)"}

    if entry.get("static_value") is not None:
        return {"editable": False, "reason": "this column is a fixed/static value"}

    canonical_field = entry.get("canonical_field")
    transform = entry.get("transform")
    if not canonical_field:
        return {"editable": False, "reason": "this column maps to no canonical field"}

    from extras import is_extras_key
    if is_extras_key(canonical_field):
        return {"editable": False,
                "reason": "user-defined extra field (stored as JSON, not editable here)"}

    from data_model import DATA_MODEL
    meta = DATA_MODEL.get(canonical_field)
    if not meta or not meta.get("table") or not meta.get("column"):
        return {"editable": False, "reason": "this column has no concrete data-model cell"}

    table, column = meta["table"], meta["column"]
    if table not in VERSIONED_TABLES:
        return {"editable": False,
                "reason": f"value lives on '{table}', which is not a versionable "
                          "per-policy table (shared/ancestor or ops-shadowed)"}

    t = CANONICAL_TABLES.get(table)
    if t is None or column not in t.c:
        return {"editable": False, "reason": f"column {table}.{column} not in schema"}

    if column == pk_column(table):
        return {"editable": False, "reason": "cannot edit a surrogate key column"}

    return {
        "editable": True,
        "canonical_field": canonical_field,
        "table": table,
        "column": column,
        "transform": transform,
        "is_scalar": table == "policy",
    }


def find_target_in_upload(s, upload_id: int, table: str, column: str,
                          transform: Optional[str], actual_value: Any) -> dict:
    """Locate the row to version when the exception has NO policy (e.g. an
    aggregate/grouped rule like the Aggregate Limit cap): scan every policy in the
    upload for the row whose post-transform value equals the flagged value. Unique
    match → editable; otherwise report (a SUM of several rows can't be fixed by one
    edit)."""
    from assembler import fetch_policies
    from exporter import _apply_transform

    pk = pk_column(table)
    pid_rows = s.execute(
        text("SELECT policy_id FROM upload_policy WHERE upload_id = :u ORDER BY policy_id"),
        {"u": upload_id},
    ).fetchall()
    policy_ids = [r[0] for r in pid_rows]
    if not policy_ids:
        return {"found": False, "reason": "no policies for this upload"}

    def _norm(v):
        if v is None:
            return None
        try:
            f = float(v)
            return str(int(f)) if f == int(f) else str(f)
        except (ValueError, TypeError):
            return str(v).strip()

    want = _norm(actual_value)
    matches = []
    for p in fetch_policies(s, policy_ids):
        pid = (p.get("policy") or {}).get("policy_id")
        rows = [p.get("policy") or {}] if table == "policy" else (p.get(table) or [])
        for row in rows:
            if want is not None and _norm(_apply_transform(row.get(column), transform)) == want:
                matches.append({"policy_id": pid, "target_id": row.get(pk),
                                "current_value": row.get(column)})
    if not matches:
        return {"found": False,
                "reason": f"no single {table} row in this upload has {column} = "
                          f"{actual_value} (it may be a SUM of several rows — fix "
                          "those rows individually)"}
    m = matches[0]
    return {"found": True, "pk_col": pk, "target_id": m["target_id"],
            "policy_id": m["policy_id"], "current_value": m["current_value"],
            "ambiguous": len(matches) > 1}


def pick_target_row(s, table: str, policy_id: int, column: str,
                    transform: Optional[str], actual_value: Any) -> dict:
    """Identify the exact row to version, via the ASSEMBLED policy (which already
    resolves every child table through its FK chain and returns only the active
    version). For child tables the row is matched by its post-transform value
    against the exception's actual_value; ties / no-match fall back to the first
    row and flag `ambiguous` so the UI tells the operator to verify the id.
    """
    from assembler import fetch_policy
    from exporter import _apply_transform

    pk = pk_column(table)
    p = fetch_policy(s, policy_id)
    if not p or not p.get("policy"):
        return {"found": False,
                "reason": f"policy {policy_id} not found / has no active version"}

    if table == "policy":
        row = p["policy"]
        return {"found": True, "pk_col": pk, "target_id": row.get(pk),
                "policy_id": policy_id, "current_value": row.get(column),
                "ambiguous": False}

    rows = p.get(table) or []
    if not rows:
        return {"found": False, "reason": f"no active {table} row for policy {policy_id}"}

    def _norm(v):
        if v is None:
            return None
        try:  # numeric-insensitive compare so 1000000 == 1000000.00
            f = float(v)
            return str(int(f)) if f == int(f) else str(f)
        except (ValueError, TypeError):
            return str(v).strip()

    want = _norm(actual_value)
    matches = [r for r in rows
               if want is not None and _norm(_apply_transform(r.get(column), transform)) == want]
    if len(matches) == 1:
        r = matches[0]              # unique value match
        ambiguous = False
    elif len(rows) == 1:
        r = rows[0]                 # only one candidate row → unambiguous
        ambiguous = False
    else:
        r = rows[0]                 # several rows, no unique match → flag for review
        ambiguous = True
    return {"found": True, "pk_col": pk, "target_id": r.get(pk),
            "policy_id": policy_id, "current_value": r.get(column),
            "ambiguous": ambiguous}


# ---- SQL generation ---------------------------------------------------------

def _copy_columns(table: str, edited_columns,
                  physical_cols: Optional[set[str]] = None) -> list[str]:
    """Columns to copy verbatim from the latest row into the new version. For
    SAME-ID versioning we KEEP the surrogate PK (the new row reuses the id); only
    the edited column(s) and the SCD bookkeeping columns are set explicitly.

    Intersected with `physical_cols` so the INSERT never references a model-only
    column (e.g. the dynamically-appended `extras`, absent in some deployments)."""
    t = CANONICAL_TABLES[table]
    skip = {*edited_columns, *_SCD_COLS}
    cols = [c for c in t.c.keys() if c not in skip]
    if physical_cols is not None:
        cols = [c for c in cols if c in physical_cols]
    return cols


def build_scd2_sql(*, table: str, pk_col: str, target_id: int,
                   edits: dict, exception_ids=None,
                   physical_cols: Optional[set[str]] = None,
                   id_identity: bool = False) -> str:
    """Build the self-contained SAME-ID SCD-2 transaction that versions ONE row,
    applying ALL its column edits in a SINGLE new version (so editing several
    fields of one record yields one new row, not several).

    `edits` is {column: coerced_value}; `exception_ids` are resolved together.
    Requires the table's PK to have been dropped/replaced (scripts/...) so the
    reused id can repeat; id_identity=True emits OVERRIDING SYSTEM VALUE for a
    GENERATED ALWAYS AS IDENTITY column."""
    edited_cols = list(edits.keys())
    copy_cols = _copy_columns(table, edited_cols, physical_cols)  # includes the PK
    insert_cols = copy_cols + edited_cols + list(_SCD_COLS)
    overriding = " OVERRIDING SYSTEM VALUE" if id_identity else ""
    select_exprs = list(copy_cols) + [sql_literal(edits[c]) for c in edited_cols] + [
        "TRUE",                         # is_current_version
        "now()",                        # valid_from
        _ACTIVE_VALID_UNTIL,            # valid_until (NOT NULL → far-future sentinel)
        "COALESCE(version_no, 1) + 1",  # version_no
    ]
    cols_csv = ", ".join(insert_cols)
    sel_csv = ", ".join(select_exprs)
    tid = int(target_id)

    # Wrapped in a DO block with a ROW_COUNT guard so that if the source row is
    # gone by execution time (e.g. a re-upload's _clear_policy_children deleted it
    # between preview and run), the INSERT's 0-row no-op RAISES instead of silently
    # marking the exceptions resolved. The whole thing is one atomic transaction.
    lines: list[str] = []
    lines.append("BEGIN;")
    lines.append("DO $$")
    lines.append("DECLARE n integer;")
    lines.append("BEGIN")
    lines.append(f"  -- 1) retire the current active {table} version ({pk_col} = {tid})")
    lines.append(f"  UPDATE {table} SET is_current_version = FALSE, valid_until = now()")
    lines.append(f"   WHERE {pk_col} = {tid} AND is_current_version IS NOT FALSE;")
    lines.append("")
    lines.append(f"  -- 2) insert ONE new active version — SAME {pk_col}, version_no + 1, "
                 f"{len(edited_cols)} edited column(s)")
    lines.append(f"  INSERT INTO {table} ({cols_csv}){overriding}")
    lines.append(f"  SELECT {sel_csv}")
    lines.append(f"    FROM {table} WHERE {pk_col} = {tid}")
    lines.append(f"    ORDER BY version_no DESC NULLS LAST LIMIT 1;")
    lines.append("  GET DIAGNOSTICS n = ROW_COUNT;")
    lines.append("  IF n <> 1 THEN")
    lines.append(f"    RAISE EXCEPTION 'SCD-2: expected to version 1 {table} row "
                 f"({pk_col}=%), but inserted % — nothing changed', {tid}, n;")
    lines.append("  END IF;")
    for eid in (exception_ids or []):
        lines.append("  UPDATE validation_exception SET exception_status = 'resolved' "
                     f"WHERE exception_id = {int(eid)};")
    lines.append("END $$;")
    lines.append("COMMIT;")
    return "\n".join(lines)


def tables_touched(table: str, exception_id: Optional[int]) -> list[str]:
    out = [f"{table} (retire current row → is_current_version=FALSE; "
           "INSERT new active version, SAME id, version_no+1)"]
    if exception_id is not None:
        out.append("validation_exception (exception_status → resolved)")
    return out
