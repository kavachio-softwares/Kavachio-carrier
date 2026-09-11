"""Direct Input→Output lane — API routes.

Two decoupled lanes (see docs/Direct_Input_to_Output_Mapping_Process.docx):

  DELIVERY (user-facing, fast)
    POST /direct/upload            input file + output template + contract → landing
                                   JSON + proposed sheet routing & column mapping
    GET  /direct/format/{id}       fetch a format's learned config
    PUT  /direct/format/{id}       user confirms/edits routing + column mapping
    GET  /direct/landing/{id}      fetch a landing record
    POST /direct/render            project landing → output, validate, persist

  DATA (admin, deferred, memoised)
    GET  /admin/mapping-tasks      queue of new formats needing a data-model map
    POST /admin/mapping-tasks/{id}/resolve   approve (mark format mapped + backfill)
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
from difflib import SequenceMatcher
from datetime import datetime
from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import text, func, or_

import direct_lane as dl
import direct_mapper as dm
import direct_render as dr
import missing_columns as mc
import storage  # blob storage abstraction (Azure/Azurite; DB-blob fallback)
from app_routes import _iso_utc, _parse_client_dt
from db import (
    ActivityEvent, AdminMappingTask, CanonicalSession, Contract, DirectFormat,
    ExportTemplate, LandingRecord, Mapper, MissingBdxColumn, OutputExport, Party,
    Pipeline, PipelineContract, Program, ReferenceDocument, SessionLocal, Tenant,
    exception_severity_counts,
)
from exporter import parse_template, spec_sheet_names, is_reference_sheet
from streaming import heartbeat_stream_response
from fingerprint import signature_hash
from ingester import _ensure_tenant, ingest_record
from auth_deps import current_principal, require_role, Principal
from mapper import (
    apply_spec_multi, detect_multiple_tables, filter_findings_to_data_sheets,
    generate_mapping_multi, qualify, read_excel_all_sheets,
    signature_multi,
)

log = logging.getLogger("bdx.direct")

# Minimum similarity for a rule to be listed as "related" to an output column it
# is not directly bound to. Compared column-name-to-column-name (see
# _contract_clauses_by_field); 0.80 keeps near-miss spellings and rejects
# coincidental word overlap. Raise toward 1.0 for stricter, lower for more
# suggestions; >1.0 disables "related" links entirely (exact bindings only).
_RELATED_MIN = float(os.getenv("KAVACHIO_RELATED_MIN", "0.85"))

# IR param values that are data, not column names: regexes, wildcards, operators.
_RULE_PARAM_NOISE = re.compile(r"[\^\$\\\[\]{}()|*+?]")
router = APIRouter()


# ---- helpers ---------------------------------------------------------------

def _coerce_like(new_val, existing):
    """Coerce an override value to the type of the input cell it replaces, so a
    numeric column stays numeric (a string '25000000' would render/validate
    differently). Best-effort: falls back to the raw string.

    Precision is never dropped: a corrected 23.5 stays 23.5 even when the cell it
    replaces was a whole number (int). The correction's own value wins — we keep
    int-ness only when the corrected number is itself whole (25.0 -> 25), so a
    fractional fix like a commission rate isn't silently truncated to 23."""
    if isinstance(existing, bool) or new_val is None:
        return new_val
    if isinstance(existing, (int, float)):
        try:
            f = float(new_val)
        except (TypeError, ValueError):
            return new_val
        return int(f) if f.is_integer() else f
    return new_val


def _apply_landing_corrections(s, landing_id: int, landing_data: dict) -> dict:
    """Overlay saved direct-lane Fix/Approve corrections onto a DEEP COPY of the
    landing data (the raw capture in landing_record.data is never mutated), so the
    next projection renders the corrected values."""
    corr = s.execute(
        text("SELECT input_sheet, input_row_index, source_column, new_value "
             "FROM landing_correction WHERE landing_id = :l AND kind IN "
             "('fix','approve') AND input_sheet IS NOT NULL AND new_value IS NOT NULL"),
        {"l": landing_id},
    ).mappings().all()
    if not corr:
        return landing_data
    data = copy.deepcopy(landing_data or {})
    for c in corr:
        try:
            cell = data["sheets"][c["input_sheet"]]["rows"][c["input_row_index"]]
            cell[c["source_column"]] = _coerce_like(c["new_value"],
                                                    cell.get(c["source_column"]))
        except (KeyError, IndexError, TypeError):
            continue
    return data


def _load_output_corrections(s, landing_id: int) -> list[dict]:
    """Saved corrections that have NO writable input cell, keyed by their OUTPUT
    coordinates.

    A `copy`/`date_reformat` column reverse-maps to one input cell, so its fix is
    overlaid on the raw capture above and flows through projection. Every other
    column kind (const, arithmetic transform, source_sheet, or simply unmapped) is
    computed at projection time from inputs that don't correspond one-to-one, so
    there is nothing to write back to. Those are stored with input_sheet NULL and
    applied by _apply_output_corrections AFTER projection instead — otherwise the
    projection would recompute the original value and the fix would vanish.
    """
    return [dict(r) for r in s.execute(
        text("SELECT output_sheet, output_row, output_field, new_value "
             "FROM landing_correction WHERE landing_id = :l AND kind IN "
             "('fix','approve') AND input_sheet IS NULL AND new_value IS NOT NULL "
             "AND output_sheet IS NOT NULL AND output_row IS NOT NULL "
             "AND output_field IS NOT NULL"),
        {"l": landing_id},
    ).mappings().all()]


def _apply_output_corrections(projected: dict, corrections: list[dict]) -> dict:
    """Overlay input-less corrections onto the PROJECTED output rows.

    ``output_row`` is 1-based (the convention the exception carries and
    dl.resolve_landing_cell already assumes), and project_to_output emits rows in
    gathered order — the same order the provenance list uses — so output row N is
    index N-1. Mutates `projected` in place and returns it.
    """
    if not corrections:
        return projected
    for c in corrections:
        rows = projected.get(c["output_sheet"])
        if not rows:
            continue
        i = int(c["output_row"]) - 1
        if i < 0 or i >= len(rows):
            continue
        field = c["output_field"]
        row = rows[i]
        # Only touch a column the projection actually produced; an override for a
        # field this sheet doesn't have would otherwise inject a phantom column.
        if field not in row:
            continue
        row[field] = _coerce_like(c["new_value"], row.get(field))
    return projected


def _tenant_id(s, mga: str) -> Optional[int]:
    row = s.execute(text("SELECT tenant_id FROM tenant WHERE tenant_code=:m LIMIT 1"),
                    {"m": mga}).fetchone()
    return row[0] if row else None


def resolve_tenant_id(s, principal: Principal, mga: Optional[str] = None) -> int:
    """Tenant from the TRUSTED token (MULTITENANCY_AUTH_CONCEPT.md §6.6). Regular
    users are pinned to their token's tenant — the client `mga` is ignored;
    platform admins may target any tenant via `mga`. Mirrors app_routes."""
    if principal.is_platform_admin:
        tid = _tenant_id(s, mga) if mga else None
        if tid is None:
            raise HTTPException(400, "platform admin must select a tenant (mga)")
        return tid
    if principal.tenant_id is None:
        raise HTTPException(403, "no tenant bound to this user")
    return principal.tenant_id


def assert_tenant_owns(principal: Principal, tenant_id: Optional[int]) -> None:
    """Guard by-id routes: the row must belong to the caller's tenant (platform
    admins bypass). 404 not 403, so ids in other tenants can't be probed."""
    if principal.is_platform_admin:
        return
    if tenant_id != principal.tenant_id:
        raise HTTPException(404, "not found")


def _tenant_name(s, tenant_id) -> Optional[str]:
    if not tenant_id:
        return None
    row = s.execute(text("SELECT tenant_code FROM tenant WHERE tenant_id=:t LIMIT 1"),
                    {"t": tenant_id}).fetchone()
    return row[0] if row else None


def _load_structure(t: ExportTemplate) -> dict:
    structure = t.structure
    if isinstance(structure, str):
        structure = json.loads(structure)
    return structure or {"sheets": []}


def _output_sheet_names(structure: dict) -> list[str]:
    return [str(sh.get("sheet_name")) for sh in structure.get("sheets", [])
            if sh.get("sheet_name") is not None]


# Logical canonical types (data_model.py) grouped into the two type-check kinds.
_DATE_TYPES = {"date", "datetime", "timestamp"}
_NUM_TYPES = {"decimal", "int", "integer", "number", "numeric", "float", "double"}

# The date pattern INSIDE a format hint — the "MM/DD/YYYY" of "Text (MM/DD/YYYY)"
# or "Char(8) YYYYMMDD". A run starts at a y/m/d letter and absorbs the
# separators that hold a pattern together, so surrounding prose can't hide it.
_YMD_RUN = re.compile(r"[ymd][ymd\s./-]*")
# That run, separators removed, must be a clean sequence of y/m/d groups — which
# "MM/DD/YYYY", "YYYYMMDD" and the bare "YYYY" all are (the day test below is
# what separates a date from a year).
_YMD_FMT = re.compile(r"^(?:y{2,4}|m{1,4}|d{1,2})+$")

# A bare 4-digit year value ("2023"), allowing the ".0" tail Excel leaves when
# an integer column is read as a float. No date parser accepts such a value.
_YEAR_VALUE = re.compile(r"^\d{4}(?:\.0+)?$")

# A calendar-formatted date STRING ("01/15/2024", "2024-01-15", "15-Jan-2024") —
# a shape a real numeric amount never takes. Deliberately narrower than "looks
# like a date": a bare Excel-serial number ("45678") is indistinguishable from a
# plain amount by shape alone (see _fmt_type_kind's module note), so this only
# catches the UNAMBIGUOUS case of an actual separated day/month/year string.
_DATE_SAMPLE = re.compile(r"^\d{1,4}[/-](?:\d{1,2}|[A-Za-z]{3,9})[/-]\d{1,4}$")

# A currency/decimal amount STRING ("$1,234.56", "1,234.56", "$500") — a shape
# no date (calendar string or Excel serial) ever takes.
_MONEY_SAMPLE = re.compile(r"^\$\s*-?[\d,]+(\.\d+)?$|^-?\d{1,3}(?:,\d{3})+\.\d{2}$")


def _samples_look_like_dates(samples) -> bool:
    """True when EVERY sampled value is unambiguously a calendar-formatted date
    string, i.e. the column is really a date despite being classified 'number'
    (a stale canonical_field, or an ambiguous field_format). False when there
    are no samples to judge by — absence of data is never treated as a
    mismatch, so an empty column keeps its original classification."""
    seen = 0
    for s in samples or []:
        v = str(s).strip()
        if not v:
            continue
        if not _DATE_SAMPLE.match(v):
            return False
        seen += 1
    return seen > 0


def _samples_look_like_money(samples) -> bool:
    """True when EVERY sampled value is unambiguously a currency/decimal
    amount, i.e. the column is really a number/amount despite being classified
    'date'. False when there are no samples to judge by — see
    _samples_look_like_dates."""
    seen = 0
    for s in samples or []:
        v = str(s).strip()
        if not v:
            continue
        if not _MONEY_SAMPLE.match(v):
            return False
        seen += 1
    return seen > 0


def _samples_all_fail_date(samples) -> bool:
    """True when the column HAS sample values and NOT ONE of them would pass the
    date type-check. That makes a 'date' classification provably wrong for this
    column: the check could only ever flag every row. False when there are no
    samples — absence of data is never treated as a mismatch, so an empty column
    keeps its classification (mirrors _samples_look_like_dates/_money)."""
    from duckdb_validation import parses_as_date, is_empty_date_cell
    seen = 0
    for s in samples or []:
        v = str(s).strip()
        # An EMPTY date cell written as a zero time of day is an absence, not a
        # value that failed — skipped exactly like the blank it stands for, so a
        # column whose sampled rows simply have no date keeps its date check for
        # the rows that do (see duckdb_validation.is_empty_date_cell).
        if not v or is_empty_date_cell(v):
            continue
        if parses_as_date(v):
            return False
        seen += 1
    return seen > 0


def _samples_all_parse_date(samples) -> bool:
    """True when the column HAS samples and EVERY one of them passes the date
    type-check — proof (by the check's own parser) that this is a date column.
    Used only to restore the right check after a wrong one was withdrawn."""
    from duckdb_validation import parses_as_date, is_empty_date_cell
    seen = 0
    for s in samples or []:
        v = str(s).strip()
        # An empty date cell is no evidence either way — it proves nothing about
        # the column's kind, so it neither passes nor fails here.
        if not v or is_empty_date_cell(v):
            continue
        if not parses_as_date(v):
            return False
        seen += 1
    return seen > 0


def _samples_all_excel_serials(samples) -> bool:
    """Every non-empty sample is an Excel date SERIAL — an integer in the
    ~1982–2036 window (deliberately narrow so 5-digit ZIP codes rarely pass).
    Serial-shaped samples are AMBIGUOUS: numerically valid AND plausibly
    dates — so they must never serve as PROOF for the date→number flip in
    _column_types_from_structure. False when there are no samples."""
    seen = 0
    for s in samples or []:
        v = str(s).strip()
        if not v:
            continue
        try:
            f = float(v.replace(",", ""))
        except (TypeError, ValueError):
            return False
        if not (30000 <= f <= 60000 and f == int(f)):
            return False
        seen += 1
    return seen > 0


def _samples_all_parse_number(samples) -> bool:
    """Mirror of _samples_all_parse_date for amounts."""
    from duckdb_validation import parses_as_number
    seen = 0
    for s in samples or []:
        v = str(s).strip()
        if not v:
            continue
        if not parses_as_number(v):
            return False
        seen += 1
    return seen > 0


def _samples_all_fail_number(samples) -> bool:
    """True when the column HAS sample values and NOT ONE of them would pass the
    amount type-check — making a 'number' classification provably wrong for this
    column (the check could only ever flag every row). False when there are no
    samples, so an empty column keeps its classification. Exact mirror of
    _samples_all_fail_date, asked with the type-check's own number reader."""
    from duckdb_validation import parses_as_number
    seen = 0
    for s in samples or []:
        v = str(s).strip()
        if not v:
            continue
        if parses_as_number(v):
            return False
        seen += 1
    return seen > 0


def _fmt_type_kind(fmt: str) -> str | None:
    """Type-check kind implied by a template's declared `field_format`.

    A bare YEAR ("YYYY") or a year-month PERIOD ("YYYYMM") is NOT a date: the
    cell holds 2023 / 202301, which no date parser accepts, so typing such a
    column 'date' made the deterministic type-check flag EVERY row (a "Treaty
    Year" of 2023 warned '"Treaty Year" expects a valid date (e.g. 2026-01-31),
    but found "2023"'). A pattern therefore only reads as a date when it says so
    outright or carries a DAY component; a year/period pattern stays untyped, so
    the column is left unchecked rather than checked against the wrong type."""
    f = (fmt or "").strip().lower()
    if not f:
        return None
    if "date" in f:
        return "date"
    for m in _YMD_RUN.finditer(f):
        core = re.sub(r"[^ymd]", "", m.group())
        # A date needs a DAY beside a month or year — "MM/DD/YYYY", "YYYYMMDD",
        # "DD-MMM-YYYY". The month-or-year test is what keeps the lone "d" of
        # "Decimal" or the "dd" of "midday" from reading as a date pattern.
        if _YMD_FMT.match(core) and "d" in core and ("y" in core or "m" in core):
            return "date"
    if any(k in f for k in ("number", "numeric", "currency", "decimal",
                            "amount", "money", "#,##0", "0.00")):
        return "number"
    return None


def _samples_are_years(samples) -> bool:
    """True when EVERY sampled value of the column is a bare 4-digit year, i.e.
    the column carries a year and not a date — read off the template's own
    sample data (like `exporter._sample_kind`), not off the column name. Guards
    the case where the declared type says 'date' but the data it describes can
    never satisfy it. False when there are no samples to judge by."""
    seen = 0
    for s in samples or []:
        v = str(s).strip()
        if not v:
            continue
        if not _YEAR_VALUE.match(v):
            return False
        seen += 1
    return seen > 0


def _column_types_from_structure(structure: dict) -> dict:
    """Map each output column → a type-check kind ('date' | 'number') for the
    deterministic type-check pass. Preference: the column's canonical field type
    (authoritative, from the data model) then its template field_format hint.
    Columns with no date/number type are omitted (left unchecked). Keyed by the
    STRIPPED sheet name to match the DuckDB table names.

    Reference / lookup tabs are skipped, exactly as they are for rule generation
    (app_routes._template_fields_from_structure) and the setup field picker. A
    reference tab is not reportable data: it is a summary, a code table or a
    lookup, so its cells are not the workbook's transactions and must not raise
    data exceptions. A cession-statement summary tab, for instance, repeats its
    section header part-way down ("Totals" under a column the parser named
    "Totals"), which reads as a text value in a numeric column and produced a
    warning about a cell that is not data at all."""
    try:
        from data_model import DATA_MODEL
    except Exception:
        DATA_MODEL = {}

    # Sample values POOLED per column name across every (non-reference) sheet.
    # The sample-based guards below withdraw a provably-wrong check, but an
    # instance with NO samples kept its classification even when the SAME
    # column on a sibling sheet had samples proving it wrong (a "Referral
    # Required (Y/N)" empty on one sheet, all-Yes on another, still
    # number-checked on the empty one). Same-named columns across a template's
    # sheets are the same concept — the multi-sheet fan-out is built on that —
    # so an instance without samples of its own borrows the pooled evidence.
    pooled: dict = {}
    for sh in (structure or {}).get("sheets", []) or []:
        if is_reference_sheet(sh):
            continue
        for c in (sh.get("columns") or []):
            nm = c.get("column_name")
            for v in (c.get("samples") or []):
                if nm and str(v).strip():
                    pooled.setdefault(nm, []).append(v)

    out: dict = {}
    for sh in (structure or {}).get("sheets", []) or []:
        if is_reference_sheet(sh):
            continue
        sheet = str(sh.get("sheet_name", "")).strip()
        if not sheet:
            continue
        kinds: dict = {}
        for c in (sh.get("columns") or []):
            name = c.get("column_name")
            if not name:
                continue
            samples = c.get("samples") or pooled.get(name)
            fmt = str(c.get("field_format") or "")
            cf = c.get("canonical_field")
            t = (DATA_MODEL.get(cf) or {}).get("type") if cf else None
            kind = "date" if t in _DATE_TYPES else "number" if t in _NUM_TYPES else None
            if kind is None:  # fall back to the template's declared format
                kind = _fmt_type_kind(fmt)
            # Whichever source typed it, a column whose sample values are bare
            # years is never date-checked: "2023" is a valid treaty year but not
            # a parsable date, so the check would flag every row.
            if kind == "date" and _samples_are_years(samples):
                kind = None
            # A stale canonical_field or an ambiguous field_format can type a
            # column as the WRONG kind. When the column's own samples are
            # unambiguously the OTHER kind's shape, withdraw the mismatched
            # check rather than trust the stale classification. (`withdrawn_from`
            # feeds the proven-flip step below: withdrawal alone left the
            # column unchecked even when the samples prove which check it
            # SHOULD have had.)
            withdrawn_from = None
            if kind == "date" and _samples_look_like_money(samples):
                kind, withdrawn_from = None, "date"
            if kind == "number" and _samples_look_like_dates(samples):
                kind, withdrawn_from = None, "number"
            # GENERAL CASE of the two guards above: a 'date' classification that
            # EVERY ONE of the column's own sample values fails is wrong about
            # this column, whatever typed it — so withdraw the check instead of
            # warning on every row. The two guards above only recognise a value
            # that is unmistakably money ("$500", "1,234.56") or a bare year; a
            # plain unformatted amount ("5000", "3000000") is neither, so an
            # amount column mis-typed 'date' still warned on all of its rows.
            # Asked with the type-check's OWN parser (duckdb_validation), so a
            # spelling the check accepts — including a date carrying a time of
            # day — keeps the column checked. Only withdraws a provably wrong
            # check; never types a column as the other kind.
            if kind == "date" and _samples_all_fail_date(samples):
                kind, withdrawn_from = None, "date"
            # SYMMETRIC general guard for 'number': a numeric classification
            # that EVERY ONE of the column's own sample values fails is wrong
            # about this column, whatever typed it — e.g. a "Referral Required
            # (Y/N)" flag whose canonical field was mis-tagged numeric flagged
            # all of its Yes/No cells. Asked with the type-check's OWN number
            # reader (duckdb_validation.parses_as_number), so any spelling the
            # check accepts keeps the column checked. Only withdraws a provably
            # wrong check.
            if kind == "number" and _samples_all_fail_number(samples):
                kind, withdrawn_from = None, "number"
            # PROVEN FLIP after a withdrawal — the one case a column may be
            # retyped rather than left unchecked: every sample passes the OTHER
            # kind's own parser. A "Settlement Due Date (1st Instalment)"
            # mis-tagged with an integer concept was number-checked (flagging
            # every date cell); the withdrawal above removes that, and its
            # all-dates samples prove which check it should have had. Held to
            # the same proof standard as the withdrawals — every non-empty
            # sample must pass — so a mixed or ambiguous column stays
            # unchecked rather than guessed at.
            if withdrawn_from == "number" and _samples_all_parse_date(samples):
                kind = "date"
            elif withdrawn_from == "date" and _samples_all_parse_number(samples) \
                    and not _samples_all_excel_serials(samples):
                # Serial-shaped samples are AMBIGUOUS — numerically valid AND
                # plausibly dates — so they can never serve as PROOF for the
                # date→number flip. A date column whose sample workbook rendered
                # raw Excel serials ("Premium Received Date" sampling 45994)
                # would otherwise be retyped 'number' and flag every properly-
                # rendered date cell (2025-12-03) in the real output as
                # "expects an amount". Such a column stays UNCHECKED instead.
                kind = "number"
            if kind:
                kinds[name] = kind
        if kinds:
            out[sheet] = kinds
    return out


def _routing_input_sheets(routing: Optional[dict]) -> set[str]:
    """Input sheet names actually consumed by a routing (across all routes)."""
    names: set[str] = set()
    for r in (routing or {}).get("routes", []):
        for src in r.get("sources", []):
            if src.get("input_sheet"):
                names.add(str(src["input_sheet"]))
    return names


def _cols_and_samples(sheets_dict: dict[str, "pd.DataFrame"]):
    cols_by_sheet: dict[str, list[str]] = {}
    samples_by_sheet: dict[str, dict[str, list[str]]] = {}
    for name, df in sheets_dict.items():
        cols_by_sheet[str(name)] = [str(c) for c in df.columns]
        sm: dict[str, list[str]] = {}
        for c in df.columns:
            sm[str(c)] = df[c].dropna().astype(str).head(dm.MAX_SAMPLES).tolist()
        samples_by_sheet[str(name)] = sm
    return cols_by_sheet, samples_by_sheet


def _contract_constants(s, contract_id: Optional[int]) -> dict:
    """Best-effort pull of fixed values (e.g. UMR) a contract supplies to the
    output. Looks in Contract.extracted for a flat {key: value} block; returns
    {} when unavailable (mapping `const` literals still work)."""
    if not contract_id:
        return {}
    c = s.get(Contract, contract_id)
    if not c or not c.extracted:
        return {}
    extracted = c.extracted
    if isinstance(extracted, str):
        try:
            extracted = json.loads(extracted)
        except (ValueError, TypeError):
            return {}
    consts = {}
    if isinstance(extracted, dict):
        for k, v in extracted.items():
            if isinstance(v, (str, int, float)):
                consts[k] = v
    return consts


# ---- DELIVERY LANE ---------------------------------------------------------

def _multi_table_error(findings: list[dict]) -> str:
    """The user-facing refusal for a workbook whose sheet(s) stack multiple
    tables. Shown verbatim in the frontend's "Multiple Tables Found" modal, so
    it is written as product copy — sheet names only, no row/header internals
    (those still go to the log below for debugging)."""
    log.info("multi-table refusal: %s",
             "; ".join(f'{f["sheet"]!r} row {f["row"]} headers={f.get("headers")}'
                       for f in findings))
    names = list(dict.fromkeys(f["sheet"] for f in findings))
    if len(names) == 1:
        subject = f'The "{names[0]}" sheet contains multiple tables.'
    else:
        listed = ", ".join(f'"{n}"' for n in names[:-1]) + f' and "{names[-1]}"'
        subject = f"The {listed} sheets contain multiple tables."
    return (subject + " Please keep only one table per worksheet, "
            "save the file, and upload it again.")


@router.post("/direct/peek-sheets")
async def direct_peek_sheets(
    file: UploadFile = File(...),
    kind: str = Form(default="input"),
    skip_rows: int = Form(default=0),
    _p: Principal = Depends(current_principal),
):
    """List the sheet names in an uploaded workbook so the user can pick which
    ones to include. `kind=output` uses the template parser (drops spec/data-
    dictionary sheets, matching what the output template would actually contain);
    `kind=input` lists every readable data sheet."""
    file_bytes = await file.read()
    if kind == "output":
        structure = await run_in_threadpool(parse_template, file_bytes, file.filename)
        names = _output_sheet_names(structure)
    else:
        sheets_dict = await run_in_threadpool(read_excel_all_sheets, file_bytes, skip_rows)
        # Drop data-dictionary / spec sheets — they document the data columns and
        # are not mapping sources, so the input list matches the output list.
        specs = await run_in_threadpool(spec_sheet_names, file_bytes)
        names = [k for k in sheets_dict.keys() if k not in specs]
    # REFUSE a multi-table workbook at the earliest touch point — the moment a
    # file is picked in Bordereau Setup. DATA sheets only: spec sheets are
    # already excluded from `names`, and the detector itself skips
    # summary/reference-named tabs (they are freeform by nature).
    multi = await run_in_threadpool(
        detect_multiple_tables, file_bytes,
        skip_rows if kind != "output" else 0, names)
    if multi:
        # A flagged sheet may still be a summary/reference tab whose NAME
        # carries no hint — ask the same sheet-role classifier the template
        # flow uses before refusing. Only runs when something was flagged.
        if kind == "output":
            headers = {sh.get("sheet_name"): [c.get("column_name")
                                              for c in (sh.get("columns") or [])]
                       for sh in structure.get("sheets", [])}
        else:
            headers = {n: list(map(str, df.columns)) for n, df in sheets_dict.items()}
        multi = await run_in_threadpool(
            filter_findings_to_data_sheets, multi, headers)
    if multi:
        raise HTTPException(400, _multi_table_error(multi))
    return {"sheets": names}


@router.post("/direct/upload")
async def direct_upload(
    mga: str = Form(...),
    file: UploadFile = File(...),
    output_template_id: int = Form(...),
    contract_id: Optional[int] = Form(default=None),
    carrier_party_id: Optional[int] = Form(default=None),
    program_id: Optional[int] = Form(default=None),
    name: Optional[str] = Form(default=None),
    skip_rows: int = Form(default=0),
    selected_sheets: Optional[list[str]] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    """SETUP step: capture an input *template* as a landing record and propose the
    sheet routing + input→output column mapping (scoped to carrier+program).
    Reuses the learned config when the input format has been seen before.

    `selected_sheets` restricts mapping to the sheets the user chose; only those
    are landed, fingerprinted and routed."""
    file_bytes = await file.read()
    sheets_dict = await run_in_threadpool(read_excel_all_sheets, file_bytes, skip_rows)
    if not sheets_dict:
        raise HTTPException(400, "workbook has no readable sheets")
    # Hard stop BEFORE any capture/AI work: a multi-table DATA sheet would be
    # captured as one table and silently corrupt the setup's mapping. Only the
    # sheets that will actually be mapped count — the user's selection when
    # given, else every non-spec sheet; summary/reference-named tabs are
    # skipped by the detector itself.
    _specs = await run_in_threadpool(spec_sheet_names, file_bytes)
    _scope = (selected_sheets if selected_sheets
              else [k for k in sheets_dict.keys() if k not in _specs])
    multi_tables = await run_in_threadpool(
        detect_multiple_tables, file_bytes, skip_rows, _scope)
    if multi_tables:
        # Name-blind summary/reference tabs: the sheet-role classifier (same
        # one the template flow uses) gets the final word before a refusal.
        multi_tables = await run_in_threadpool(
            filter_findings_to_data_sheets, multi_tables,
            {n: list(map(str, df.columns)) for n, df in sheets_dict.items()})
    if multi_tables:
        raise HTTPException(400, _multi_table_error(multi_tables))
    if selected_sheets:
        keep = set(selected_sheets)
        sheets_dict = {k: v for k, v in sheets_dict.items() if k in keep}
        if not sheets_dict:
            raise HTTPException(400, "none of the selected input sheets were found in the workbook")

    landing = await run_in_threadpool(dl.build_landing_record, sheets_dict)
    sig = signature_multi(sheets_dict)
    fp = signature_hash(sig)
    input_sheets = list(landing["sheets"].keys())
    cols_by_sheet, samples_by_sheet = _cols_and_samples(sheets_dict)
    # `file` is closed once this handler returns; the heartbeat body below runs
    # as a task, so read anything still needed off it here.
    source_filename = file.filename

    # Resolved BEFORE the heartbeat stream starts, because a missing template has
    # to stay a real 404 — once the stream begins the status is fixed at 200.
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        tpl = s.get(ExportTemplate, output_template_id)
        if not tpl:
            raise HTTPException(404, "output template not found")
        structure = _load_structure(tpl)
        output_sheets = _output_sheet_names(structure)

    # Everything past here can run for MINUTES: on a layout whose fingerprint is
    # not yet known, `propose_column_mapping` asks Gemini to map every input
    # column to the output template. A plain handler sends no bytes at all until
    # it returns, so that whole time the connection looks idle and gets closed —
    # the browser then has no response to read and reports a bare "Network
    # Error" (no status, no detail). A space every HEARTBEAT_SECS keeps the
    # connection alive and, just as importantly, gets the real response headers
    # (CORS included) out immediately; the JSON result is the final chunk. Same
    # treatment /direct/run and the contract upload already have.
    async def _setup():
        with SessionLocal() as s:
            # Format-level reuse: have we learned this input layout before?
            fmt = (s.query(DirectFormat)
                   .filter(DirectFormat.tenant_id == tid,
                           DirectFormat.fingerprint == fp,
                           DirectFormat.output_template_id == output_template_id)
                   .order_by(DirectFormat.id.desc())
                   .first())
            known = bool(fmt and fmt.approved and fmt.sheet_routing and fmt.column_mapping)

            if known:
                routing = fmt.sheet_routing
                column_mapping = fmt.column_mapping
                candidates = fmt.candidates or {}
                # A format we have seen before keeps the reasoning it was built
                # with — re-deciding would risk moving a column somebody had
                # already confirmed.
                decisions = fmt.mapping_decisions or {}
                fmt.hit_count = (fmt.hit_count or 1) + 1
            else:
                routing = dl.propose_sheet_routing(input_sheets, output_sheets)
                # A previously-confirmed mapping for this format outranks
                # everything the proposer finds — a re-run must never move a
                # column somebody already fixed by hand.
                column_mapping, candidates, decisions = await run_in_threadpool(
                    dm.propose_column_mapping, cols_by_sheet, structure, routing,
                    samples_by_sheet, (fmt.column_mapping if fmt else None), True)
                if fmt is None:
                    fmt = DirectFormat(
                        tenant_id=tid, name=name, fingerprint=fp,
                        output_template_id=output_template_id, contract_id=contract_id,
                        carrier_party_id=carrier_party_id, program_id=program_id,
                        sheet_routing=routing, column_mapping=column_mapping,
                        candidates=candidates, mapping_decisions=decisions,
                        datamodel_mapped=False, approved=0)
                    s.add(fmt)
                else:
                    fmt.sheet_routing = routing
                    fmt.column_mapping = column_mapping
                    fmt.candidates = candidates
                    fmt.mapping_decisions = decisions
                    fmt.contract_id = contract_id or fmt.contract_id
                    fmt.carrier_party_id = carrier_party_id or fmt.carrier_party_id
                    fmt.program_id = program_id or fmt.program_id
                s.flush()

            datamodel_mapped = bool(fmt.datamodel_mapped)
            # DATA-MODEL mapping reuse by INPUT fingerprint: the input→canonical
            # mapping depends only on the input structure, not the output template.
            # So if THIS format isn't mapped yet but a sibling (same tenant + same
            # input fingerprint) already is, inherit its mapper here — a new output
            # template for the same input then flows into the data model without
            # re-mapping. Purely data-lane: the input→output routing/column_mapping
            # above is untouched.
            if not datamodel_mapped:
                sib = (s.query(DirectFormat)
                       .filter(DirectFormat.tenant_id == tid,
                               DirectFormat.fingerprint == fp,
                               DirectFormat.datamodel_mapped.is_(True),
                               DirectFormat.datamodel_mapper_id.isnot(None),
                               DirectFormat.id != fmt.id)
                       .order_by(DirectFormat.id.desc())
                       .first())
                if sib is not None:
                    fmt.datamodel_mapped = True
                    fmt.datamodel_mapper_id = sib.datamodel_mapper_id
                    datamodel_mapped = True
                    log.info("inherited data-model mapping from sibling format %s "
                             "(mapper %s, same input fingerprint) onto format %s",
                             sib.id, sib.datamodel_mapper_id, fmt.id)
            rec = LandingRecord(
                tenant_id=tid, format_id=fmt.id, source_filename=source_filename,
                fingerprint=fp, data=landing, row_count=landing["row_count"],
                datamodel_status="pending")
            s.add(rec)
            s.commit()
            s.refresh(rec)
            s.refresh(fmt)
            format_id, landing_id = fmt.id, rec.id
            datamodel_mapper_id = fmt.datamodel_mapper_id

        # DATA LANE (additive): if this format's input→data-model mapping is already
        # done, push this freshly-landed input straight into the data model — in a
        # background thread so the setup response never waits on ingestion. Mirrors the
        # /direct/run auto-ingest; reuses the gated _ingest_landing_background (which
        # self-checks datamodel_mapped and skips already-loaded landings). The
        # input→output setup logic above is unchanged.
        datamodel_queued = False
        if datamodel_mapped:
            log.info("data model mapping already exists (format %s, mapper %s) — "
                     "directly loading input into the data model (landing %s)",
                     format_id, datamodel_mapper_id, landing_id)
            threading.Thread(target=_ingest_landing_background,
                             args=(landing_id,), daemon=True).start()
            datamodel_queued = True
        else:
            log.info("data model mapping not done for format %s — skipping direct "
                     "input→data-model load (landing %s)", format_id, landing_id)

        try:
            from audit import log_activity, actor_email
            log_activity(tid, actor_email(principal.user_id), "direct_setup_uploaded",
                         target=f"format:{format_id}",
                         details={"format_id": format_id, "landing_id": landing_id,
                                  "output_template_id": output_template_id,
                                  "source_filename": source_filename,
                                  "row_count": landing["row_count"],
                                  "known_format": known,
                                  "datamodel_mapped": datamodel_mapped})
        except Exception:  # noqa: BLE001
            pass

        return {
            "landing_id": landing_id,
            "format_id": format_id,
            "known_format": known,
            "datamodel_mapped": datamodel_mapped,
            "datamodel_queued": datamodel_queued,
            "fingerprint": fp,
            "input_sheets": input_sheets,
            "output_sheets": output_sheets,
            "input_columns": cols_by_sheet,
            "samples": samples_by_sheet,
            "sheet_routing": routing,
            "column_mapping": column_mapping,
            "candidates": candidates,
            # What the mapper could and could not decide. The build screen shows
            # this straight away, because a required column with nothing behind
            # it is the one problem that looks like a success until the file is
            # opened by whoever is waiting for it.
            "mapping_review": _mapping_review(decisions),
            "row_count": landing["row_count"],
        }

    return heartbeat_stream_response(_setup())


class DirectFormatUpdate(BaseModel):
    sheet_routing: Optional[dict] = None
    column_mapping: Optional[dict] = None
    candidates: Optional[dict] = None
    name: Optional[str] = None
    contract_id: Optional[int] = None
    sheet_contracts: Optional[dict] = None  # {output_sheet: contract_id}
    carrier_party_id: Optional[int] = None
    program_id: Optional[int] = None
    approved: Optional[bool] = None


class PipelineContractIn(BaseModel):
    contract_id: int
    sheet_key: Optional[str] = None  # output sheet to pin; None = fallback contract


class PipelineCreate(BaseModel):
    name: Optional[str] = None
    carrier_party_id: int
    program_id: int
    # Which broker this setup is for. Optional: a setup that covers the whole
    # programme leaves it unset, which is what every setup built before the
    # broker level existed looks like.
    broker_party_id: Optional[int] = None
    input_format_id: int
    output_template_id: int
    contracts: list[PipelineContractIn] = []


class PipelineUpdate(BaseModel):
    name: Optional[str] = None
    broker_party_id: Optional[int] = None
    input_format_id: Optional[int] = None
    output_template_id: Optional[int] = None
    contracts: Optional[list[PipelineContractIn]] = None  # replaces the set when given


@router.get("/direct/format/{format_id}")
def direct_format_get(format_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        f = s.get(DirectFormat, format_id)
        if not f:
            raise HTTPException(404, "format not found")
        assert_tenant_owns(principal, f.tenant_id)
        return _format_to_dict(f)


@router.put("/direct/format/{format_id}")
def direct_format_update(format_id: int, body: DirectFormatUpdate,
                         principal: Principal = Depends(current_principal)):
    """User confirmation step: save the reviewed sheet routing + column mapping
    so every future file of this format flows straight through."""
    with SessionLocal() as s:
        f = s.get(DirectFormat, format_id)
        if not f:
            raise HTTPException(404, "format not found")
        assert_tenant_owns(principal, f.tenant_id)
        if body.sheet_routing is not None:
            f.sheet_routing = body.sheet_routing
        if body.column_mapping is not None:
            f.column_mapping = body.column_mapping
        if body.candidates is not None:
            f.candidates = body.candidates
        if body.name is not None:
            f.name = body.name
        if body.contract_id is not None:
            f.contract_id = body.contract_id
        if body.sheet_contracts is not None:
            # {output_sheet: contract_id} — coerce ids to int, drop blanks.
            f.sheet_contracts = {
                str(k): int(v) for k, v in body.sheet_contracts.items()
                if v not in (None, "", 0)
            }
        if body.carrier_party_id is not None:
            f.carrier_party_id = body.carrier_party_id
        if body.program_id is not None:
            f.program_id = body.program_id
        if body.approved is not None:
            f.approved = 1 if body.approved else 0
            # Supersede any prior active setup for the same carrier + program:
            # only one setup is active per (carrier, program) at a time.
            if f.approved and f.carrier_party_id is not None and f.program_id is not None:
                s.query(DirectFormat).filter(
                    DirectFormat.tenant_id == f.tenant_id,
                    DirectFormat.carrier_party_id == f.carrier_party_id,
                    DirectFormat.program_id == f.program_id,
                    DirectFormat.id != f.id,
                ).update({DirectFormat.approved: 0})
        f.modified_at = datetime.utcnow()
        s.commit()
        s.refresh(f)
        try:
            from audit import log_activity, actor_email
            _changed = [k for k in (
                "sheet_routing", "column_mapping", "candidates", "name",
                "contract_id", "sheet_contracts", "carrier_party_id",
                "program_id", "approved",
            ) if getattr(body, k, None) is not None]
            log_activity(f.tenant_id, actor_email(principal.user_id),
                         "bdx_setup_updated", target=f"format:{format_id}",
                         details={"format_id": format_id, "name": f.name,
                                  "changed": _changed})
        except Exception:  # noqa: BLE001
            pass
        return _format_to_dict(f)


@router.delete("/direct/format/{format_id}")
def direct_format_delete(format_id: int,
                         principal: Principal = Depends(current_principal)):
    """Discard a setup (draft or otherwise) for a carrier + program. Used by the
    "Delete draft" action in Bordereau Setup."""
    with SessionLocal() as s:
        f = s.get(DirectFormat, format_id)
        if not f:
            raise HTTPException(404, "format not found")
        assert_tenant_owns(principal, f.tenant_id)
        _audit_tenant_id = f.tenant_id
        _audit_name = f.name
        s.delete(f)
        s.commit()
        try:
            from audit import log_activity, actor_email
            log_activity(_audit_tenant_id, actor_email(principal.user_id),
                         "bdx_setup_deleted", target=f"format:{format_id}",
                         details={"format_id": format_id, "name": _audit_name})
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "deleted_id": format_id}


@router.get("/direct/landing/{landing_id}")
def direct_landing_get(landing_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        r = s.get(LandingRecord, landing_id)
        if not r:
            raise HTTPException(404, "landing record not found")
        assert_tenant_owns(principal, r.tenant_id)
        return {
            "landing_id": r.id, "format_id": r.format_id,
            "source_filename": r.source_filename, "fingerprint": r.fingerprint,
            "row_count": r.row_count, "datamodel_status": r.datamodel_status,
            "data": r.data,
        }


@router.post("/direct/landing/{landing_id}/load-datamodel")
def direct_landing_load_datamodel(landing_id: int, background_tasks: BackgroundTasks):
    """Additive DATA-LANE trigger — push one landing into the canonical data model
    when (and only when) its format's input→data-model mapping is already done.

    Self-contained: it reuses _ingest_landing_background, which re-reads the
    approved mapper from the format and self-gates on datamodel_mapped, so this
    NEVER touches the input→output rendering/mapping path. Safe to call more than
    once — the background loader skips landings already marked loaded.

    Returns queued=False (with a reason) when the mapping isn't established yet or
    the landing is already loaded, so the caller can surface that to the user.
    """
    with SessionLocal() as s:
        rec = s.get(LandingRecord, landing_id)
        if not rec:
            raise HTTPException(404, "landing record not found")
        fmt = s.get(DirectFormat, rec.format_id) if rec.format_id else None
        mapping_ready = bool(fmt and fmt.datamodel_mapped and fmt.datamodel_mapper_id)
        status = rec.datamodel_status

    if status == "loaded":
        return {"landing_id": landing_id, "queued": False,
                "reason": "already loaded", "datamodel_status": status}
    if not mapping_ready:
        return {"landing_id": landing_id, "queued": False,
                "reason": "input→data-model mapping not done for this format",
                "datamodel_status": status}

    # Mapping exists and the landing is pending → load it in the background so the
    # caller never waits on ingestion.
    background_tasks.add_task(_ingest_landing_background, landing_id)
    return {"landing_id": landing_id, "queued": True, "datamodel_status": status}


def _supplement_summary(supp: Optional[dict]) -> Optional[dict]:
    """Trim the stored supplement (which also holds the parsed landing) down to
    what the UI needs: whether it's on and the file's name."""
    if not supp:
        return None
    return {"enabled": bool(supp.get("enabled")), "filename": supp.get("filename")}


def _format_to_dict(f: DirectFormat) -> dict:
    return {
        "id": f.id, "tenant_id": f.tenant_id, "name": f.name,
        "fingerprint": f.fingerprint, "output_template_id": f.output_template_id,
        "contract_id": f.contract_id, "carrier_party_id": f.carrier_party_id,
        "sheet_contracts": f.sheet_contracts or {},
        "supplement": _supplement_summary(f.supplement),
        "program_id": f.program_id, "sheet_routing": f.sheet_routing,
        "column_mapping": f.column_mapping, "candidates": f.candidates or {},
        "datamodel_mapped": bool(f.datamodel_mapped),
        "datamodel_mapper_id": f.datamodel_mapper_id,
        "approved": bool(f.approved), "hit_count": f.hit_count,
    }


# ---- Pipeline helpers ------------------------------------------------------
# A Pipeline binds one Input Template (DirectFormat) + one Output Template
# (ExportTemplate) + 1..N contracts (PipelineContract). The active pipeline for
# a (carrier, program) is what /direct/run executes against.

def _pipeline_contracts(s, pipeline_id: int) -> list[PipelineContract]:
    return (s.query(PipelineContract)
            .filter(PipelineContract.pipeline_id == pipeline_id)
            .order_by(PipelineContract.position, PipelineContract.id)
            .all())


def _asof_governing_date(landing_data: dict, *cfg_sources):
    """The date that decides which contract version governs this run.

    Returns (date | None, note). Reads the configured — or auto-detected —
    governing column out of the landing record and reduces its values to one
    date per the configured strategy. See contract_asof_config for why the
    column is configuration rather than a constant: for a premium bordereau it
    is policy inception, for claims it is date of loss, and choosing wrong
    produces plausible-looking wrong money rather than an error.
    """
    from contract_upload_services import contract_asof_config as _cfg
    sheets = (landing_data or {}).get("sheets") or {}
    columns, values = [], []
    for sh in sheets.values():
        if isinstance(sh, dict):
            columns.extend(sh.get("columns") or [])
    field = _cfg.governing_date_field(columns, *cfg_sources)
    if not field:
        return None, _cfg.describe(None, None)
    for sh in sheets.values():
        for row in (sh.get("rows") or []) if isinstance(sh, dict) else []:
            if isinstance(row, dict) and field in row:
                values.append(row[field])
    strategy = _cfg.setting("date_strategy", _cfg.DATE_STRATEGY, *cfg_sources)
    governing = _cfg.pick_date(values, strategy)
    return governing, _cfg.describe(field, governing, strategy)


# Per-row scoping helpers live in contract_upload_services.contract_asof —
# they are pure functions over dicts, and keeping them out of this module lets
# them be tested without importing the route layer (and with it the LLM client,
# the rule library and a live database).
from contract_upload_services.contract_asof import (          # noqa: E402
    AsofCfg as _AsofCfg,
    contract_windows as _asof_contract_windows,
    row_dates_from_blocks as _asof_row_dates,
    filter_exceptions_by_window as _asof_filter_exceptions,
)


def _apply_asof_contracts(s, landing_data, pipe, fmt, sheet_contracts, eff_contract_id):
    """Feature 7 §7.2 — re-point this run's contracts at the versions IN FORCE
    on the file's transaction dates, instead of whichever version is pinned.

    The pin (pipeline_contract / direct_format.contract_id) says WHICH contract
    governs this setup. That stays correct and untouched. This asks the second
    question §7 adds — which VERSION of it applied on these dates — and swaps
    only the version, keeping the pin's choice of contract lineage.

    Returns (sheet_contracts, eff_contract_id, note). Every failure path returns
    the inputs unchanged, so a run can never break because as-of resolution was
    unavailable or misconfigured.
    """
    from contract_upload_services import contract_asof as _asof
    from contract_upload_services import contract_asof_config as _cfg

    if not _cfg.enabled(pipe, fmt):
        return sheet_contracts, eff_contract_id, None

    governing, note = _asof_governing_date(landing_data, pipe, fmt)
    if governing is None:
        return sheet_contracts, eff_contract_id, note

    on_unresolved = _cfg.setting("on_unresolved", _cfg.ON_UNRESOLVED, pipe, fmt)

    def _swap(cid):
        """Pinned version → the sibling in force on `governing`."""
        if not cid:
            return cid, None
        found = _asof.resolve_sibling_as_of(s, int(cid), governing)
        if found and int(found) != int(cid):
            return int(found), f"contract {cid} → {found}"
        if found is None and on_unresolved == "skip":
            # No version covers this date. 'skip' drops the contract so the run
            # is not silently validated by rules that never applied; 'pin' (the
            # default) keeps it, preserving pre-Feature-7 behaviour.
            return None, f"contract {cid} → none (no version covers {governing})"
        return cid, None

    swaps = []
    new_sheets = {}
    for k, v in (sheet_contracts or {}).items():
        nv, msg = _swap(v)
        new_sheets[k] = nv
        if msg:
            swaps.append(f"sheet '{k}': {msg}")
    new_eff, msg = _swap(eff_contract_id)
    if msg:
        swaps.append(f"fallback: {msg}")

    # ── Per-row scoping (§7.1 "resolve EACH transaction") ────────────────
    # `governing` above is ONE date for the whole run, so a file straddling a
    # renewal resolves entirely to one version and the rows on the other side
    # are judged by rules that never applied to them. Collect every version the
    # file's dates actually touch; each one's rules then run, and
    # _asof_filter_exceptions discards the ones that fired outside their own
    # window. Off (`row_scoped: false`) keeps the single-version behaviour.
    extra_ids = []
    if _cfg.setting("row_scoped", "true", pipe, fmt) not in ("false", "0", "no"):
        anchor = new_eff or eff_contract_id
        lineage = _asof.lineage_of(s, int(anchor)) if anchor else None
        if lineage is not None:
            seen = {int(c) for c in list((new_sheets or {}).values()) + [new_eff] if c}
            for row_date in sorted({d for d in _row_dates_of(landing_data, pipe, fmt)}):
                cid = _asof.resolve_as_of(s, lineage, row_date)
                if cid and int(cid) not in seen:
                    seen.add(int(cid))
                    extra_ids.append(int(cid))
            if extra_ids:
                swaps.append(f"row-scoped: also governing {extra_ids}")

    note = note + ("; " + "; ".join(swaps) if swaps else "; no version change")
    return new_sheets, new_eff, note, extra_ids


def _row_dates_of(landing_data, *cfg_sources):
    """Every distinct governing date in the landing record — the set of dates
    the file's rows actually carry, so only the versions genuinely touched are
    pulled in (a file inside one window stays a single-contract run)."""
    from contract_upload_services import contract_asof_config as _cfg
    sheets = (landing_data or {}).get("sheets") or {}
    dates = set()
    for sh in sheets.values():
        if not isinstance(sh, dict):
            continue
        field = _cfg.governing_date_field(sh.get("columns") or [], *cfg_sources)
        if not field:
            continue
        for row in sh.get("rows") or []:
            d = _cfg.coerce_date(row.get(field)) if isinstance(row, dict) else None
            if d is not None:
                dates.add(d)
    return dates


def _pipeline_ready(s, p: Pipeline) -> tuple[bool, str]:
    """A pipeline can be activated once it has an input template, an output
    template, and at least one contract. Permissive: individual output sheets
    may stay unbound (they just aren't contract-validated). Returns
    (ready, reason) — reason is user-facing when not ready."""
    if not p.input_format_id:
        return False, "Add an input template before activating."
    if not p.output_template_id:
        return False, "Add an output template before activating."
    if not _pipeline_contracts(s, p.id):
        return False, "Add at least one contract before activating."
    return True, ""


def _contract_reference_documents(c) -> dict:
    """The reference-document record written on a contract at upload time:
    {"external": [{document_name, version_or_date, ...}], "provided": [filename]}.

    `external` is what the contract STILL defers rule content to — documents it
    names that were never supplied — so a setup screen can explain why those
    clauses produced no rule. Returns empty lists for contracts written before
    this was recorded (the key is simply absent), so callers need no special case.

    The stored list is re-filtered on the way out. It is written once at upload
    time, so a contract uploaded before a gate was tightened keeps whatever was
    accepted then — and what this screen names is what someone is asked to go
    and find. Filtering on READ makes the correction retroactive without
    rewriting a single row or asking for a rebuild.
    """
    extracted = getattr(c, "extracted", None)
    if isinstance(extracted, str):
        try:
            extracted = json.loads(extracted)
        except (ValueError, TypeError):
            extracted = None
    refs = (extracted or {}).get("reference_documents") if isinstance(extracted, dict) else None
    if not isinstance(refs, dict):
        return {"external": [], "provided": []}
    external = [r for r in (refs.get("external") or []) if isinstance(r, dict)]
    provided = [n for n in (refs.get("provided") or []) if isinstance(n, str)]
    try:
        from contract_upload_services.validation_rule_generator import (
            filter_external_references)
        external = filter_external_references(external)
    except Exception:                                     # noqa: BLE001
        # Showing the stored list beats breaking the screen.
        log.exception("could not re-filter stored reference documents")
    return {"external": external, "provided": provided}


def _derive_reference_documents(s, contract_id):
    """Re-derive a contract's deferred external documents from its STORED clauses.

    The reference-document record is written at upload time, so a setup built
    before that existed carries nothing. Rather than show such a setup a blank
    "None" — indistinguishable from a contract that genuinely defers to nothing
    — run the same deterministic detector the upload pipeline uses over the
    clause text already in the database. Same function, same rules, no model
    call. Clauses carrying an inlined "[Context from …]" block are skipped: that
    block only exists when the referenced document WAS supplied and used.

    Read-only and best-effort: any failure yields [] rather than breaking the
    screen. Only called for a SINGLE setup being opened, never for a list.
    """
    try:
        from contract_upload_services.validation_rule_generator import (
            detect_deferred_external_references)
        rows = s.execute(
            text("SELECT text, page_number FROM clauses_extracted "
                 "WHERE contract_id = :cid"), {"cid": contract_id}).mappings().all()
        return detect_deferred_external_references(
            [{"text": r["text"], "page": r["page_number"]} for r in rows
             if "[Context from" not in (r["text"] or "")])
    except Exception:                                     # noqa: BLE001
        log.exception("could not derive reference documents for contract %s",
                      contract_id)
        return []


def _pipeline_to_dict(s, p: Pipeline, derive_refs: bool = False) -> dict:
    ready, reason = _pipeline_ready(s, p)
    # Supplement is configured on the input template (DirectFormat); surface a
    # flag so the run screen can note that supplementary data is auto-captured.
    has_supplement = False
    _df = s.get(DirectFormat, p.input_format_id) if p.input_format_id else None
    if _df:
        has_supplement = bool((_df.supplement or {}).get("enabled"))
    # Names for the carrier/program/templates this setup belongs to — resolved
    # generically by id (works for any tenant/carrier/program, nothing hardcoded)
    # so a setups list can read "which carrier + program" without the caller
    # having to look each id up separately.
    _carrier = s.get(Party, p.carrier_party_id) if p.carrier_party_id else None
    _program = s.get(Program, p.program_id) if p.program_id else None
    _broker = s.get(Party, p.broker_party_id) if p.broker_party_id else None
    _out_tpl = s.get(ExportTemplate, p.output_template_id) if p.output_template_id else None
    _pcs = _pipeline_contracts(s, p.id)
    _contracts_by_id = {}
    _contract_ids = [pc.contract_id for pc in _pcs]
    if _contract_ids:
        _contracts_by_id = {
            c.id: c for c in s.query(Contract).filter(Contract.id.in_(_contract_ids)).all()
        }
    # Reference document(s) each contract of this setup defers rule content to.
    # Read off the contract rows already loaded above, so this costs no extra
    # query. `external` = named by the contract but never supplied — the setup
    # was built WITHOUT them, so the clauses that depend on them produced no
    # rule; `provided` = the reference files that were attached.
    _ref_docs = []
    for pc in _pcs:
        c = _contracts_by_id.get(pc.contract_id)
        if c is None:
            continue
        refs = _contract_reference_documents(c)
        if derive_refs and not refs["external"] and not refs["provided"]:
            refs["external"] = _derive_reference_documents(s, c.id)
        if refs["external"] or refs["provided"]:
            _ref_docs.append({
                "contract_id": c.id, "filename": c.filename,
                # Projected down to what identifies the document. The stored
                # record also carries every clause that cited it verbatim, which
                # would be by far the largest thing in this response and is not
                # what a setup summary shows.
                "external": [{"document_name": r.get("document_name"),
                              "version_or_date": r.get("version_or_date"),
                              "page": (r.get("pages") or [None])[0]}
                             for r in refs["external"]],
                "provided": refs["provided"],
            })
    return {
        "id": p.id, "tenant_id": p.tenant_id, "name": p.name,
        "carrier_party_id": p.carrier_party_id,
        "carrier_name": _carrier.legal_name if _carrier else None,
        "program_id": p.program_id,
        "program_name": _program.name if _program else None,
        "broker_party_id": p.broker_party_id,
        "broker_name": _broker.legal_name if _broker else None,
        "input_format_id": p.input_format_id,
        "input_format_name": _df.name if _df else None,
        "output_template_id": p.output_template_id,
        "output_template_name": _out_tpl.name if _out_tpl else None,
        "status": p.status, "has_supplement": has_supplement,
        "contracts": [
            {"contract_id": pc.contract_id, "sheet_key": pc.sheet_key,
             "position": pc.position,
             "filename": _contracts_by_id[pc.contract_id].filename
                         if pc.contract_id in _contracts_by_id else None}
            for pc in _pcs
        ],
        "reference_documents": _ref_docs,
        "ready": ready, "ready_reason": reason,
        "created_at": _iso_utc(p.created_at),
        "modified_at": _iso_utc(p.modified_at),
    }


def _activate_pipeline(s, p: Pipeline) -> None:
    """Enforce completeness, then make this pipeline live — superseding the live
    ones it replaces (same carrier, program and broker, overlapping contracts). Raises HTTP 400 with
    a user-facing reason when the pipeline isn't complete.

    Transition mirror: also flip the input DirectFormat's ``approved`` flag so
    ``bordereau_ready`` (app_routes.py) and the DirectSetup auto-load keep
    working until they are re-pointed at Pipeline (plan Step 7)."""
    ready, reason = _pipeline_ready(s, p)
    if not ready:
        raise HTTPException(400, reason)
    # Supersede the live setups this one REPLACES: same (carrier, program,
    # broker), AND covering any of the same contracts. Including the broker
    # means one programme can run a different input layout per broker; including
    # the contracts means one broker can run a different BDX template per
    # contract — a setup built for contract B no longer switches off the one
    # built for contract A. A setup listing no contracts (made before contracts
    # were attached) covers every contract, so it still collides with every
    # other one exactly as before, and a pre-broker setup (broker NULL) still
    # only ever collides with another NULL.
    from setup_scope import contract_ids_of
    mine = contract_ids_of(s, p.id)
    peers = s.query(Pipeline).filter(
        Pipeline.tenant_id == p.tenant_id,
        Pipeline.carrier_party_id == p.carrier_party_id,
        Pipeline.program_id == p.program_id,
        (Pipeline.broker_party_id.is_(None) if p.broker_party_id is None
         else Pipeline.broker_party_id == p.broker_party_id),
        Pipeline.id != p.id,
        Pipeline.status == "active",
    ).all()
    for peer in peers:
        theirs = contract_ids_of(s, peer.id)
        if not mine or not theirs or (mine & theirs):
            peer.status = "superseded"
    p.status = "active"
    p.modified_at = datetime.utcnow()

    # Mirror onto the input DirectFormat (transition-only): approve this one,
    # and withdraw approval from every other format of the (carrier, program)
    # EXCEPT those still behind a setup that stays live beside this one.
    if p.input_format_id and p.carrier_party_id is not None and p.program_id is not None:
        s.flush()
        still_live = {fid for (fid,) in s.query(Pipeline.input_format_id).filter(
            Pipeline.tenant_id == p.tenant_id,
            Pipeline.carrier_party_id == p.carrier_party_id,
            Pipeline.program_id == p.program_id,
            Pipeline.status == "active",
            Pipeline.id != p.id,
        ).all() if fid}
        still_live.add(p.input_format_id)
        s.query(DirectFormat).filter(
            DirectFormat.tenant_id == p.tenant_id,
            DirectFormat.carrier_party_id == p.carrier_party_id,
            DirectFormat.program_id == p.program_id,
            DirectFormat.id.notin_(still_live),
        ).update({DirectFormat.approved: 0}, synchronize_session=False)
        df = s.get(DirectFormat, p.input_format_id)
        if df:
            df.approved = 1


def _queue_datamodel_mapping(tenant_id: Optional[int], format_id: Optional[int],
                             actor: Optional[str]) -> Optional[int]:
    """DATA LANE, raised at ACTIVATION rather than after the first run.

    Activation is the point a setup is declared production-ready, so that is the
    earliest honest moment to ask an admin for the input→data-model mapping —
    doing it here means the tenant's FIRST real bordereau flows straight into the
    warehouse instead of sitting `pending` until the queue is worked.

    Deliberately conservative — returns None (queues nothing) when the format is
    already mapped (including via the sibling-fingerprint inheritance in
    `/direct/setup`) or when there is no landing sample to learn from, since
    `/admin/mapping-tasks/{id}/propose` needs one. Either way the existing
    post-run trigger in `_render_landing` still covers the format, and
    `_ensure_admin_task` keeps one open task per (tenant, format), so the two
    entry points can never double-queue.

    Runs in its OWN session, after the activation transaction has committed, and
    never raises: the data lane must not be able to fail a user's activation."""
    if not format_id:
        return None
    try:
        with SessionLocal() as s:
            df = s.get(DirectFormat, format_id)
            if df is None or df.datamodel_mapped:
                return None
            rec = (s.query(LandingRecord)
                   .filter(LandingRecord.format_id == df.id)
                   .order_by(LandingRecord.id.desc()).first())
            if rec is None or not rec.data:
                return None
            # Read the ids off the ORM instances BEFORE committing: commit expires
            # every attribute, so `rec.id` past this point would re-query — and
            # once the session closes it raises DetachedInstanceError instead.
            landing_id = rec.id
            task_id = _ensure_admin_task(s, tenant_id, df.id, df.fingerprint,
                                         landing_id, actor)
            s.commit()
        log.info("setup activated for format %s with no data-model mapping — "
                 "raised admin mapping task %s (landing %s)",
                 format_id, task_id, landing_id)
        return task_id
    except Exception:  # noqa: BLE001
        log.exception("could not raise the data-model mapping task for format %s "
                      "on activation (activation itself is unaffected)", format_id)
        return None


def _pending_mapping_task(format_id: Optional[int]) -> Optional[dict]:
    """The data-mapping work still outstanding for this input format, or None.

    This is the SAME condition the Data Mapping Queue screen shows a row for:
    the format is not yet mapped to the data model AND it has a task in an open
    state. Returns None the moment either is untrue — a format already mapped
    (or whose task has been done/dismissed) has nothing for an admin to act on.

    Read-only and best-effort: any failure returns None, which suppresses a
    notification rather than raising into the caller's request."""
    if not format_id:
        return None
    try:
        with SessionLocal() as s:
            df = s.get(DirectFormat, format_id)
            # datamodel_mapped is the authoritative "mapping is done" flag — the
            # same one _queue_datamodel_mapping refuses to re-queue on.
            if df is None or df.datamodel_mapped:
                return None
            task = (s.query(AdminMappingTask)
                    .filter(AdminMappingTask.format_id == format_id,
                            AdminMappingTask.status.in_(_OPEN_STATES))
                    .order_by(AdminMappingTask.id.desc()).first())
            if task is None:
                return None
            detail = task.detail if isinstance(task.detail, dict) else {}
            return {"task_id": task.id, "status": task.status,
                    "format_name": df.name,
                    # Mirrors the queue's status chip: a task the AI has already
                    # drafted a mapping for reads "Mapping Drafted", not
                    # "Needs Mapping".
                    "drafted": bool(detail.get("proposed_mapper_id"))}
    except Exception:  # noqa: BLE001
        log.exception("could not read the mapping state for format %s", format_id)
        return None


def _informative_facts(facts: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Drop rows that are just earlier rows glued together, and empty ones.

    A setup is normally auto-named "<carrier> — <program>", so listing it under
    its own carrier and program prints the same words three times. A row is
    dropped only when removing the values ALREADY shown leaves nothing but
    punctuation — i.e. it is a composite of them and adds no new information.

    Deliberately not a token-subset test: that would delete a genuinely distinct
    value whose every word happens to appear above (a program literally named
    "2" under "Carrier 2"). Substring removal keeps such a row, because "2" does
    not contain "carrier 2"."""
    kept: list[tuple[str, Any]] = []
    shown: list[str] = []
    for label, value in facts:
        text = str(value or "").strip()
        if not text:
            continue
        # Compare on letters/digits only, so "—", case and spacing don't matter.
        probe = re.sub(r"[^0-9a-z]+", " ", text.lower()).strip()
        remainder = probe
        for prior in shown:
            remainder = remainder.replace(prior, " ")
        if probe and not re.search(r"[0-9a-z]", remainder):
            continue
        shown.append(probe)
        kept.append((label, value))
    return kept


def _notify_setup_activated(setup: dict, actor: Optional[str],
                            actor_name: Optional[str] = None) -> None:
    """Ask Kavachio staff to map a newly-activated setup's input format —
    emailed to every kavachio_admin and recorded in their in-app feed.

    SENT ONLY WHEN THERE IS WORK TO DO. Activation is just the trigger; the
    notification's purpose is the Data Mapping Queue item it points at, so if
    the format is already mapped (or its task is resolved) nothing is sent and
    nothing is recorded — see `_pending_mapping_task`. A broker re-activating a
    setup whose format Kavachio mapped months ago therefore stays silent.

    Everything shown is read off the setup dict `_pipeline_to_dict` already
    built (names resolved by id, so this works for any tenant/carrier/program),
    plus the broker's own name. Runs AFTER the activation transaction has
    committed and never raises: notification delivery must not be able to fail
    or slow a user's activation — `notify_platform_admins` swallows its own
    errors and sends the mail on a background thread."""
    try:
        from notifications import notify_platform_admins

        pending = _pending_mapping_task(setup.get("input_format_id"))
        if pending is None:
            log.info("setup %s activated with its input format already mapped "
                     "(or no open mapping task) — no admin notification raised",
                     setup.get("id"))
            return

        tenant_id = setup.get("tenant_id")
        broker_name = broker_code = None
        if tenant_id:
            with SessionLocal() as s:
                t = s.get(Tenant, tenant_id)
                if t:
                    broker_name = t.legal_name or t.tenant_name
                    broker_code = t.tenant_name

        carrier = setup.get("carrier_name")
        program = setup.get("program_name")
        # "Carrier · Program" with whichever parts we actually have — a setup can
        # legitimately be missing either.
        scope = " · ".join(p for p in (carrier, program) if p)
        # Who acted, in the words a person would use. Falls back to the email
        # when the account has no name on it — never to a bare id.
        actor_label = (actor_name or "").strip() or actor or "A user"

        title = "New setup needs Kavachio mapping" + (f" — {broker_name}" if broker_name else "")
        body = (
            f"{actor_label} activated a bordereau setup"
            + (f" for {scope}" if scope else "")
            + (f" at {broker_name}" if broker_name else "")
            + ". Its input format is not mapped to the Kavachio data model yet, "
              "so bordereaux processed against it cannot flow into the warehouse "
              "automatically."
        )
        facts = _informative_facts([
            ("Broker", broker_name),
            ("Carrier", carrier),
            ("Program", program),
            ("Setup", setup.get("name")),
            ("Activated by", actor_label),
        ])
        notify_platform_admins(
            "setup_mapping_required", title,
            body=body, facts=facts, tenant_id=tenant_id, actor=actor,
            target=f"pipeline:{setup.get('id')}",
            details={"pipeline_id": setup.get("id"),
                     "carrier_party_id": setup.get("carrier_party_id"),
                     "program_id": setup.get("program_id"),
                     "broker_code": broker_code,
                     "mapping_task_id": pending["task_id"],
                     "format_id": setup.get("input_format_id")},
            # The whole point of the message: land the admin on the queue where
            # "Set Up Kavachio Mapping" lives. That screen is platform-admin
            # scoped, unlike the tenant-scoped setup screens.
            link_path="/admin/mapping-tasks",
            link_label="Set Up Kavachio Mapping",
            # Says what to DO, in the queue's own wording so the mail and the
            # screen agree. The button repeats only the short call to action, so
            # this stays a sentence rather than echoing it.
            action=("A mapping has already been drafted for this format — open "
                    "the Data Mapping Queue to review and approve it."
                    if pending["drafted"] else
                    "Open the Data Mapping Queue and run Set Up Kavachio Mapping "
                    "for this format, so its bordereaux load into the data model."),
            subject="Kavachio — new setup needs data mapping"
                    + (f" ({broker_name})" if broker_name else ""),
            # How a BATCH of these reads when the admin signs in and several are
            # waiting: "3 setups need Kavachio mapping".
            label="setup needs Kavachio mapping",
            label_plural="setups need Kavachio mapping",
        )
    except Exception:  # noqa: BLE001
        log.exception("could not raise the setup-mapping notification for "
                      "pipeline %s (activation itself is unaffected)", setup.get("id"))


@router.post("/direct/format/{format_id}/supplement")
async def direct_format_supplement(
    format_id: int,
    file: Optional[UploadFile] = File(default=None),
    clear: bool = Form(default=False),
    principal: Principal = Depends(current_principal),
):
    """Attach (or clear) the setup's supplementary data file. Uploaded ONCE here
    on the Setup page, alongside the input/output templates — NOT per run. The
    file is parsed now and stored with the format; every run captures its sheets
    alongside the BDX (no policy-number join)."""
    with SessionLocal() as s:
        f = s.get(DirectFormat, format_id)
        if not f:
            raise HTTPException(404, "format not found")
        assert_tenant_owns(principal, f.tenant_id)
        if clear or file is None:
            f.supplement = {"enabled": False}
        else:
            data = await file.read()
            supp_sheets = await run_in_threadpool(read_excel_all_sheets, data, 0)
            if not supp_sheets:
                raise HTTPException(400, "supplement workbook has no readable sheets")
            supp_landing = await run_in_threadpool(dl.build_landing_record, supp_sheets)
            f.supplement = {"enabled": True, "filename": file.filename,
                            "landing": supp_landing}
        f.modified_at = datetime.utcnow()
        s.commit()
        s.refresh(f)
        try:
            from audit import log_activity, actor_email
            log_activity(f.tenant_id, actor_email(principal.user_id),
                         "supplement_uploaded", target=f"format:{format_id}",
                         details={"format_id": format_id,
                                  "filename": file.filename if file is not None else None})
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "supplement": _supplement_summary(f.supplement)}


def _principal_email(principal) -> Optional[str]:
    """Resolve the acting user's email from a Principal, for the audit `actor`.
    Best-effort — never raises into the request."""
    try:
        from audit import actor_email
        return actor_email(principal.user_id) if principal is not None else None
    except Exception:
        return None


def _principal_name(principal) -> Optional[str]:
    """The acting user's display name, for anything a PERSON reads.

    The email address stays the actor of record on audit and notification rows
    (it is stable and unique); this is only for presentation, so a notification
    reads "Priya Sharma activated…" rather than an address. None when the
    account has no name — callers fall back to the email rather than inventing
    one. Best-effort; never raises into the request."""
    try:
        if principal is None or not principal.user_id:
            return None
        from db import AppUser
        with SessionLocal() as s:
            u = s.get(AppUser, principal.user_id)
            return ((u.full_name or "").strip() or None) if u else None
    except Exception:  # noqa: BLE001
        return None


def _scope_template_conflict(s, tid: int, scope: dict, pipe, fmt) -> Optional[str]:
    """Is the setup about to run the one this scope actually agreed on?

    Returns a user-facing reason to refuse, or None to proceed.

    The two ways this goes wrong are worth separating, because the fix differs:
    nothing agreed for the scope at all (make a template), versus something
    agreed that the running setup was not built against (make a setup for this
    scope). Both otherwise end in a file that looks delivered and carries no
    rows, which is the failure mode this exists to prevent.
    """
    from output_template_routes import _resolve as _resolve_output_template
    agreed, level = _resolve_output_template(
        s, tid, scope.get("carrier_party_id"), scope.get("program_id"),
        scope.get("broker_party_id"), scope.get("contract_id"))
    if agreed is None:
        return ("no output BDX template is configured for this carrier, "
                "programme, broker and contract — create one on the Bordereau "
                "Setup page first")
    running = (pipe.output_template_id if pipe else fmt.output_template_id)
    if running == agreed.id:
        return None
    # Different VERSIONS of one template share a name and a layout lineage; the
    # setup's mapping was learned against the version it was built with, and
    # that version is what it must keep using.
    running_row = s.get(ExportTemplate, running) if running else None
    if running_row and running_row.name == agreed.name:
        return None
    return (
        f"this bordereau is scoped to a {level} whose output template is "
        f"'{agreed.name}', but the setup that runs here was built against "
        f"'{running_row.name if running_row else 'a different template'}'. "
        f"A setup's mapping is learned against one output template, so running "
        f"it against another would produce a file with the right column "
        f"headings and no data. Build a Bordereau Setup for this scope first.")


def _mapping_review(decisions: Optional[dict]) -> dict:
    """A short summary of the mapping ladder's verdict, for the setup screen.

    Three numbers and two lists. `unresolved_required` is the one that matters:
    those columns will be EMPTY in the delivered file, and the plan is explicit
    that they must not be silently generated as if all were well.
    """
    decisions = decisions or {}
    auto = review = 0
    unresolved: list[dict] = []
    ambiguous: list[dict] = []
    for sheet, rows in decisions.items():
        for d in rows or []:
            if d.get("status") in ("AUTO_MAPPED", "MANUALLY_CONFIRMED"):
                auto += 1
                continue
            review += 1
            entry = {"sheet": sheet, "field": d.get("display_name"),
                     "field_key": d.get("field_key"),
                     "confidence": d.get("confidence"),
                     "reason": d.get("reason"),
                     "candidates": [c.get("source") for c in (d.get("candidates") or [])][:4]}
            if d.get("required"):
                unresolved.append(entry)
            else:
                ambiguous.append(entry)
    return {
        "checked": auto + review,
        "auto_mapped": auto,
        "needs_review": review,
        # Required and unmapped — blocks a clean delivery.
        "unresolved_required": unresolved,
        # Optional and unmapped — worth a look, not a blocker.
        "unmapped_optional": ambiguous,
        "threshold": _semantic_threshold(),
    }


def _semantic_threshold() -> float:
    from semantic_mapping import min_confidence
    return min_confidence()


def _output_sample_layout(s, template_id: Optional[int]) -> Optional[dict]:
    """The sample output BDX configured for a template, if one was supplied.

    The sample is a REFERENCE ARTIFACT, not the template — the recipient sends
    "here is what the file should look like", and we keep it beside the template
    to check ourselves against. It is stored as a ReferenceDocument (kind
    ``output_sample``) whose ``extracted`` already holds the parsed column list,
    so checking a generated file costs a dictionary comparison rather than
    re-reading a workbook on every run.

    None when no sample was configured — which is normal, not a failure.
    """
    if not template_id:
        return None
    try:
        rows = (s.query(ReferenceDocument)
                .filter(ReferenceDocument.kind == "output_sample")
                .order_by(ReferenceDocument.id.desc()).limit(50).all())
        for r in rows:
            ex = r.extracted or {}
            if ex.get("template_id") == template_id and ex.get("sheets"):
                return {"sheets": ex["sheets"]}
    except Exception as e:  # noqa: BLE001 — a missing sample never blocks a run
        log.warning("output sample lookup skipped: %s", e)
    return None


async def _render_landing(
    landing_id: int, contract_id: Optional[int], filename: Optional[str],
    actor: Optional[str], extra_consts: dict, auto_ingest: bool = False,
    reuse_export_id: Optional[int] = None, pipeline_id: Optional[int] = None,
    check_only: bool = False, scope: Optional[dict] = None,
) -> dict:
    """Shared core: project a landing record into the output BDX, validate it
    against the contract rules, persist the downloadable file, and either raise a
    one-time admin task (format not mapped yet) or — once the format IS mapped —
    auto-ingest the data into the data model in the background. Used by both
    /direct/render (setup preview) and /direct/run (ops data upload).

    ``reuse_export_id`` re-renders IN PLACE (updates that OutputExport row so the
    id/header stays stable across Re-generate); omit it to create a new export.

    ``check_only`` is the broker pre-submission self-check (V-5): run the full
    validation and produce the (downloadable, highlighted) output file, but treat
    it as a throwaway — do NOT link it to the landing (so it stays out of run
    history) and do NOT raise an admin mapping task. Combine with
    ``auto_ingest=False`` so nothing reaches the data model."""
    with SessionLocal() as s:
        rec = s.get(LandingRecord, landing_id)
        if not rec:
            raise HTTPException(404, "landing record not found")
        fmt = s.get(DirectFormat, rec.format_id) if rec.format_id else None
        if not fmt or not fmt.column_mapping:
            raise HTTPException(400, "format has no confirmed column mapping yet")
        # The Input Template (fmt) always supplies the input layout (routing,
        # column_mapping, datamodel state). The Output Template + the governing
        # contracts come from the PIPELINE when running against one; otherwise
        # (the /direct/render setup-preview path) they come from fmt, unchanged.
        pipe = s.get(Pipeline, pipeline_id) if pipeline_id else None
        if pipe:
            eff_output_template_id = pipe.output_template_id
            _pcs = _pipeline_contracts(s, pipe.id)
            sheet_contracts = {pc.sheet_key: pc.contract_id
                               for pc in _pcs if pc.sheet_key}
            fallback_contract_id = next(
                (pc.contract_id for pc in _pcs if not pc.sheet_key), None)
        else:
            eff_output_template_id = fmt.output_template_id
            sheet_contracts = fmt.sheet_contracts or {}
            fallback_contract_id = fmt.contract_id
        # The template here is always the SETUP's own — see this module's
        # _scope_template_conflict for why a run can never be pointed at a
        # different one. The scope has already selected WHICH setup runs.
        tpl = s.get(ExportTemplate, eff_output_template_id)
        if not tpl:
            raise HTTPException(404, "output template not found")
        structure = _load_structure(tpl)
        template_blob = storage.resolve_bytes(tpl.template_blob_ref, tpl.template_blob)
        template_name = tpl.name
        from output_serializers import normalize_format as _norm_fmt
        output_format = _norm_fmt(getattr(tpl, "output_format", None))
        tenant_id = rec.tenant_id
        eff_contract_id = contract_id or fallback_contract_id

        # ── Feature 7 §7.2 — Prior Period Files ──────────────────────────
        # The pin above answers "which contract governs this setup". For a late
        # or corrected file that is not the whole question: the version pinned
        # today may not be the version that applied when the transactions
        # happened. Swap each pinned contract for the sibling IN FORCE on the
        # file's own dates, keeping the pin's choice of contract lineage.
        #
        # Fail-open: on any error the run keeps the contracts the pin gave it,
        # so this can never be the reason a bordereau fails to process.
        asof_note, asof_extra_ids = None, []
        try:
            sheet_contracts, eff_contract_id, asof_note, asof_extra_ids = (
                _apply_asof_contracts(
                    s, rec.data, pipe, fmt, sheet_contracts, eff_contract_id))
            if asof_note:
                log.info(f"[AsOf] {asof_note}")
        except Exception as _asof_exc:  # noqa: BLE001 — fail-open by design
            log.info(f"[AsOf] skipped ({_asof_exc}) — run keeps its pinned contracts")

        # Per-schedule contracts: each output sheet can have its OWN contract.
        # Governing set = the per-sheet contracts ∪ the fallback contract. Every
        # governing contract's rules are validated (each only fires on the sheets
        # its compiled SQL references), and their constants are merged.
        governing_ids = _governing_ids(sheet_contracts, eff_contract_id,
                                       asof_extra_ids)
        base_consts = {}
        for cid in governing_ids:
            base_consts.update(_contract_constants(s, cid))
        constants_dict = {**base_consts, **extra_consts}
        # Per-output-sheet governing-contract summary for the UI — so the run
        # result can show "Schedule A → Contract-A.pdf" up front, without drilling
        # into individual exceptions. A contract PINNED to a sheet governs ONLY
        # that sheet; an unmapped sheet falls back to the format's contract ONLY
        # when that contract isn't itself pinned elsewhere (otherwise the sheet
        # has no governing contract and isn't contract-validated).
        asof_windows = {}
        asof_cfg = [_AsofCfg(x) for x in (pipe, fmt) if x is not None]
        try:
            if asof_extra_ids:
                asof_windows = _asof_contract_windows(s, governing_ids)
        except Exception:      # noqa: BLE001 — fail-open: no filter, no harm
            asof_windows = {}
        # BOTH labels, because a contract may carry either. One written in
        # Kavachio is typed and has a name and no file at all; one that arrived
        # as a document has a filename. Sending only the filename left the run
        # result calling a named contract "Contract #4019" — the one place the
        # operator checks that the file was measured against what they chose.
        _gov_rows = (s.query(Contract).filter(Contract.id.in_(governing_ids)).all()
                     if governing_ids else [])
        _fname = {c.id: c.filename for c in _gov_rows}
        _cname = {c.id: c.name for c in _gov_rows}
        _mapped = {str(k).strip(): int(v)
                   for k, v in sheet_contracts.items() if v}
        _pinned = set(_mapped.values())
        governing_contracts = []
        for _sh in _output_sheet_names(structure):
            _key = str(_sh).strip()
            if _key in _mapped:
                _cid, _is_fallback = _mapped[_key], False
            elif eff_contract_id and eff_contract_id not in _pinned:
                _cid, _is_fallback = eff_contract_id, True
            else:
                _cid, _is_fallback = None, False  # no governing contract
            governing_contracts.append({
                "sheet": _sh,
                "contract_id": _cid,
                "contract_name": _cname.get(_cid) if _cid else None,
                "contract_filename": _fname.get(_cid) if _cid else None,
                "fallback": _is_fallback,
            })
        routing = fmt.sheet_routing or dl.propose_sheet_routing(
            list((rec.data or {}).get("sheets", {})), _output_sheet_names(structure))
        column_mapping = fmt.column_mapping
        # Overlay any saved Fix/Approve corrections (raw capture stays untouched).
        landing_data = _apply_landing_corrections(s, landing_id, rec.data)
        # Corrections with no writable input cell are applied after projection.
        output_corrections = _load_output_corrections(s, landing_id)
        landing_fp = rec.fingerprint
        datamodel_mapped = bool(fmt.datamodel_mapped)
        format_id = fmt.id
        out_template_id = eff_output_template_id
        out_template_version = tpl.version or 1
        # Parsed once when the sample was configured, so comparing costs a
        # dictionary diff here rather than re-reading a workbook every run.
        sample_layout = _output_sample_layout(s, tpl.id)

    # Pure projection (offload heavy work from the event loop).
    routed = dl.apply_routing(landing_data, routing)
    projected = dl.project_to_output(routed, column_mapping, constants_dict)
    # Summary-total detection MUST see the UNCORRECTED projection: the input
    # file's totals are still self-consistent here, which is what proves a
    # summary cell is a column total. After a Fix, the stale static total no
    # longer matches its column — exactly the case the detected relationship
    # repairs at render time (see dr.inject_summary_formulas below).
    summary_totals = dr.detect_summary_totals(structure, projected)
    # Must run AFTER projection: these columns are computed, so an override applied
    # before this point would just be recomputed away. Runs before the validation
    # blocks are built so the corrected value is what gets re-validated and
    # rendered — otherwise a fixed cell would still report its old exception.
    projected = _apply_output_corrections(projected, output_corrections)
    blocks = dr.to_validation_blocks(structure, projected)

    # Contract-rule validation (non-blocking), reusing the DuckDB engine.
    # Validation is non-blocking and reuses the DuckDB engine. Contract rules run
    # against ALL governing contracts at once (per-schedule contracts each check
    # their own schedule); the deterministic date/amount TYPE checks run
    # regardless (they need only the output template's column types).
    exceptions: list[dict] = []
    try:
        rules: list[dict] = []
        if governing_ids:
            with CanonicalSession() as cs:
                rule_rows = cs.execute(
                    text("SELECT rule_id, contract_id, rule_engine, rule_name, "
                         "severity, canonical_target, rule_spec, error_message "
                         "FROM validation_rule WHERE contract_id = ANY(:cids) "
                         "AND rule_status != 'disabled'"),
                    {"cids": governing_ids}).mappings().all()
                rules = [dict(r) for r in rule_rows]

        # A rule is "schedule-scoped" when its clause explicitly names the
        # schedules it applies to (IR params scope_sheets/sheets, e.g.
        # "between Schedule G, H, I, J"). Such a rule's compiled SQL is already
        # restricted to just those sheets, so it must be kept wherever it
        # fires — the per-schedule isolation below only reins in UNSCOPED rules.
        def _rule_is_scoped(rule: dict) -> bool:
            spec = rule.get("rule_spec")
            if isinstance(spec, str):
                try:
                    spec = json.loads(spec)
                except Exception:
                    return False
            params = (((spec or {}).get("ir") or {}).get("params")) or {}
            return bool(params.get("scope_sheets") or params.get("sheets"))
        rule_scoped = {r.get("rule_id"): _rule_is_scoped(r) for r in rules}

        schema_cols = {
            sh.get("sheet_name", ""): [c.get("column_name")
                                       for c in (sh.get("columns") or [])
                                       if c.get("column_name")]
            for sh in structure.get("sheets", [])}
        column_types = _column_types_from_structure(structure)
        if rules or column_types:
            from duckdb_validation import run_validation
            dv = run_validation(blocks, rules, template_id=eff_output_template_id,
                                schema_cols=schema_cols, column_types=column_types)
            exceptions = dv.get("exceptions", [])

            # ── Feature 7 §7.1, per row ──────────────────────────────────
            # Every version the file spans has just had its rules run over
            # EVERY row, so a row is currently judged by versions that never
            # governed it. Keep only the exceptions whose contract was in force
            # on that row's own date. Filtering after the run rather than
            # scoping each rule's SQL means the compiler and the engine are
            # untouched — the multi-contract machinery that already serves
            # per-schedule contracts does the work.
            if asof_windows:
                try:
                    _rd = _asof_row_dates(blocks, *asof_cfg)
                    exceptions, _dropped = _asof_filter_exceptions(
                        exceptions, _rd, asof_windows)
                    if _dropped:
                        log.info(f"[AsOf] row-scoped: dropped {_dropped} exception(s) "
                                 f"raised by a version that did not govern the row")
                except Exception as _fx:  # noqa: BLE001 — fail-open: keep them all
                    log.info(f"[AsOf] row scoping skipped ({_fx})")

            # Label each row-level exception with the offending policy's number so
            # the review UI shows a real policy id instead of "Dataset-level".
            from duckdb_validation import label_exceptions_with_policy
            label_exceptions_with_policy(exceptions, structure, blocks)
            # Per-schedule isolation: unscoped rules fan out to every sheet that
            # carries their columns (rule_compiler.compile_ir) — NOT to the contract
            # they came from. So on a multi-schedule BDX whose schedules share the
            # same column layout (e.g. RiskSmith Sch A/B/C), an unscoped rule from
            # Schedule H's contract also fires on the other schedules. When the user
            # has mapped sheets to contracts, keep an UNSCOPED rule's exceptions only
            # on the sheet(s) its own contract governs. A SCOPED rule (its clause
            # names the schedules, e.g. "G, H, I, J") is left alone — its SQL is
            # already limited to those schedules. Type-check exceptions carry no
            # contract_id, so they are never dropped. No-op unless sheet_contracts
            # is set (single-contract runs unaffected).
            if sheet_contracts:
                sheet_owner = {str(sh).strip(): int(cid)
                               for sh, cid in sheet_contracts.items() if cid}
                pinned = set(sheet_owner.values())   # contracts tied to a sheet

                def _in_scope(e: dict) -> bool:
                    # Clause names its schedules → trust it (already SQL-limited).
                    if rule_scoped.get(e.get("rule_id")):
                        return True
                    ec = e.get("contract_id")
                    owner = sheet_owner.get(str(e.get("sheet") or "").strip())
                    if owner is not None:
                        # A mapped sheet is validated ONLY by its own contract.
                        return ec is None or int(ec) == owner
                    # An unmapped sheet is validated only by contracts that aren't
                    # pinned to some OTHER sheet — i.e. a real fallback contract.
                    # A pinned contract (e.g. Schedule H's) never leaks onto sheets
                    # it wasn't mapped to.
                    return ec is None or int(ec) not in pinned

                before = len(exceptions)
                exceptions = [e for e in exceptions if _in_scope(e)]
                dropped = before - len(exceptions)
                if dropped:
                    log.info("per-schedule scoping dropped %d out-of-scope "
                             "exception(s) across %d mapped sheet(s)",
                             dropped, len(sheet_owner))
    except Exception as e:  # noqa: BLE001 — never block delivery on validation
        log.warning("direct render validation skipped: %s", e)

    from output_serializers import (
        serialize as _serialize_output, output_extension as _output_ext,
        content_type_for_filename as _ct_for, ensure_extension as _ensure_ext,
        sheets_from_projected as _sheets_from_projected,
    )
    n_sheets = len(structure.get("sheets") or [])
    if output_format == "xlsx":
        output_bytes = await run_in_threadpool(
            dr.render_output, structure, projected, template_blob)
        # Cell highlighting is Excel-only; CSV/XML/JSON carry exceptions in the
        # OutputExport record instead.
        if exceptions:
            try:
                from exporter import highlight_exceptions
                output_bytes = highlight_exceptions(output_bytes, structure, exceptions)
            except Exception as e:  # noqa: BLE001
                log.warning("highlight skipped: %s", e)
        # LAST step on purpose: highlighting re-saves the workbook via openpyxl,
        # which would drop injected formulas/cached values. Patches each detected
        # totals cell with =SUM(...) + a cached value recomputed from the
        # corrected rows — Excel recalculates live, the viewers (data_only=True)
        # show the corrected value only.
        output_bytes = dr.inject_summary_formulas(
            output_bytes, structure, projected, summary_totals,
            template_bytes=template_blob)
    else:
        output_bytes = await run_in_threadpool(
            _serialize_output, _sheets_from_projected(structure, projected), output_format)

    # Template names read well on screen and badly as filenames — see
    # output_serializers.safe_filename for what it strips and why.
    from output_serializers import safe_filename as _safe_name
    raw = _safe_name(filename or template_name or "export")
    # Force the extension to match the template's output format (multi-sheet CSV
    # becomes a .zip bundle).
    fname = _ensure_ext(raw, _output_ext(output_format, n_sheets))

    # Persist the generated output to blob storage (Azure/Azurite) when enabled;
    # otherwise keep the bytes inline in `blob` (legacy behaviour).
    exp_ref, exp_bytes = await run_in_threadpool(
        storage.store_or_keep, "exports", tenant_id, fname, output_bytes,
        _ct_for(fname))

    sev_crit, sev_warn, sev_info = exception_severity_counts(exceptions)
    # Does the file we just wrote match the sample the recipient supplied?
    # Advisory only (plan section 15): a sample is optional, and a difference is
    # something for a person to look at, never a reason to withhold a delivery.
    sample_report = None
    if sample_layout:
        try:
            from output_template_validation import compare_with_sample
            sample_report = compare_with_sample(
                _sheets_from_projected(structure, projected), sample_layout)
        except Exception as e:  # noqa: BLE001
            log.warning("sample comparison skipped: %s", e)

    with SessionLocal() as s:
        out = s.get(OutputExport, reuse_export_id) if reuse_export_id else None
        if out is None:
            out = OutputExport(
                tenant_id=tenant_id, template_id=out_template_id,
                template_name=template_name, filename=fname,
                source_upload_id=None, policy_ids=None, generated_by=actor,
                policy_count=sum(len(v) for v in projected.values()),
                exception_count=len(exceptions), exceptions=exceptions,
                critical_count=sev_crit, warning_count=sev_warn,
                info_count=sev_info,
                status="has_exceptions" if exceptions else "clean",
                blob=exp_bytes, blob_ref=exp_ref,
                # What this file was made from and for. template_version is the
                # one that has to be right: a later edit forks the template, and
                # this download must keep pointing at the layout it was actually
                # written with.
                template_version=out_template_version,
                output_format=output_format,
                pipeline_id=pipeline_id,
                carrier_party_id=(scope or {}).get("carrier_party_id"),
                program_id=(scope or {}).get("program_id"),
                broker_party_id=(scope or {}).get("broker_party_id"),
                contract_id=(scope or {}).get("contract_id") or eff_contract_id,
                sample_comparison=sample_report)
            s.add(out)
        else:
            # Re-render in place — same export id/header, refreshed file + exceptions.
            out.template_name = template_name
            out.filename = fname
            out.policy_count = sum(len(v) for v in projected.values())
            out.exception_count = len(exceptions)
            out.exceptions = exceptions
            out.critical_count = sev_crit
            out.warning_count = sev_warn
            out.info_count = sev_info
            out.status = "has_exceptions" if exceptions else "clean"
            out.blob = exp_bytes
            out.blob_ref = exp_ref
            out.generated_by = actor or out.generated_by
            out.template_version = out_template_version
            out.output_format = output_format
            out.sample_comparison = sample_report
        s.add(ActivityEvent(
            tenant_id=tenant_id, actor=actor,
            action="direct_output_checked" if check_only else "direct_output_generated",
            target=f"direct:{template_name}",
            details={"filename": fname, "rows": out.policy_count,
                     "exceptions": len(exceptions)}))
        s.commit()
        s.refresh(out)
        export_id = out.id

        # Link the uploaded file (landing) to the output it produced, so the run
        # history can show "uploaded X → generated Y (N exceptions)". A pre-submission
        # self-check is a throwaway — leave it UNLINKED so /direct/runs (which
        # requires output_export_id) excludes it from the broker's run history.
        if not check_only:
            lr = s.get(LandingRecord, landing_id)
            if lr is not None:
                lr.output_export_id = export_id
                s.commit()
                # Group 3: producing a BDX for a program satisfies that program's next
                # outstanding deadline → mark the period received (green "on time").
                # Sits INSIDE `not check_only` on purpose: a pre-submission self-check
                # is a look, not a submission, and must not tick off a deadline.
                # Best-effort: the calendar is a side-feature and must never break
                # delivery, so any failure here is swallowed.
                try:
                    fmt = s.get(DirectFormat, lr.format_id) if lr.format_id else None
                    if fmt is not None and fmt.program_id is not None:
                        from submission_calendar_service import mark_received
                        # WHICH period, and WHOSE. The uploaded file's name is
                        # the only statement of the period we have here, and it
                        # is a better one than "whatever is oldest and open" —
                        # a July file sent in September satisfies July. When the
                        # name says nothing, mark_received falls back to the
                        # oldest open period and records that it guessed.
                        if mark_received(
                                s, fmt.program_id, export_id=export_id,
                                broker_party_id=out.broker_party_id,
                                source_filename=lr.source_filename) is not None:
                            s.commit()
                except Exception as _e:  # noqa: BLE001
                    log.warning("submission-calendar mark_received failed: %s", _e)
                    s.rollback()

        # DATA LANE trigger: if this format isn't mapped to the data model yet,
        # raise (or append to) a one-time admin task. Off the delivery path. A
        # self-check must not create admin tasks — it's a look, not a submission.
        admin_task_id = None
        if not datamodel_mapped and not check_only:
            admin_task_id = _ensure_admin_task(s, tenant_id, format_id,
                                               landing_fp, landing_id, actor)
        s.commit()

    # Once the format IS data-model-mapped, every real run flows straight into the
    # data model — in a background thread so delivery never waits on it.
    datamodel_queued = False
    if auto_ingest and datamodel_mapped:
        threading.Thread(target=_ingest_landing_background,
                         args=(landing_id, actor), daemon=True).start()
        datamodel_queued = True

    return {
        "export_id": export_id,
        "filename": fname,
        "row_count": sum(len(v) for v in projected.values()),
        "exception_count": len(exceptions),
        "exceptions": exceptions,
        "status": "has_exceptions" if exceptions else "clean",
        "datamodel_mapped": datamodel_mapped,
        "datamodel_queued": datamodel_queued,
        "admin_task_id": admin_task_id,
        "governing_contracts": governing_contracts,
        "preview": {k: v[:20] for k, v in projected.items()},
        "check_only": check_only,
    }


@router.post("/direct/render")
async def direct_render(
    landing_id: int = Form(...),
    contract_id: Optional[int] = Form(default=None),
    filename: Optional[str] = Form(default=None),
    actor: Optional[str] = Form(default=None),
    constants: Optional[str] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    """SETUP preview: render the output for the just-mapped input template."""
    with SessionLocal() as s:
        lr = s.get(LandingRecord, landing_id)
        if not lr:
            raise HTTPException(404, "landing record not found")
        assert_tenant_owns(principal, lr.tenant_id)
    extra: dict = {}
    if constants:
        try:
            extra = json.loads(constants) or {}
        except (ValueError, TypeError):
            extra = {}
    return await _render_landing(landing_id, contract_id, filename,
                                 actor or _principal_email(principal), extra)


class RerenderRequest(BaseModel):
    actor: Optional[str] = None


@router.post("/export/downloads/{export_id}/rerender")
async def rerender_export(export_id: int, body: Optional[RerenderRequest] = None,
                          principal: Principal = Depends(current_principal)):
    """Re-generate a DIRECT-LANE export's output BDX with saved Fix/Approve
    corrections applied. Produces a NEW output_exports (the landing's corrections
    persist across renders, since they're keyed by landing, not export)."""
    with SessionLocal() as s:
        lr = s.execute(
            text("SELECT id, tenant_id FROM landing_record WHERE output_export_id = :e "
                 "ORDER BY id DESC LIMIT 1"), {"e": export_id},
        ).fetchone()
        landing_id = lr[0] if lr else None
        if landing_id is None:
            raise HTTPException(404, "no direct-lane landing record for this export")
        # Guarded on the EXPORT, not on the landing's tenant: the export is what
        # carries the broker this run was made for, and a broker seat has no
        # tenant for the old comparison to match.
        from carrier_scope import assert_can_read_export
        _exp = s.get(OutputExport, export_id)
        if _exp is None:
            raise HTTPException(404, "export not found")
        assert_can_read_export(s, principal, _exp)
    # Re-render IN PLACE so the export id/header stays stable across Re-generate.
    return await _render_landing(int(landing_id), None, None,
                                 (body.actor if body else None) or _principal_email(principal), {},
                                 auto_ingest=False, reuse_export_id=export_id)


def _contract_clauses_by_field(contract_id: Optional[int],
                               field_names: list[str]) -> dict[str, list[dict]]:
    """Index a contract's validation clauses by the OUTPUT field they govern, so
    the UI can show the clause that 'follows' whichever output field is chosen.
    A clause is matched to a field when the field name appears in the rule's
    spec/target (the same vocabulary contract rules are written in)."""
    out: dict[str, list[dict]] = {}
    if not contract_id or not field_names:
        return out
    # A multi-sheet BDX template repeats the SAME column on every sheet, so
    # `field_names` arrives with each output field duplicated once per sheet
    # (e.g. "Insured State" ×10). Dedupe before matching, else every clause is
    # appended once per sheet and the UI shows the same rule N times.
    seen_field: set[str] = set()
    field_names = [f for f in field_names
                   if f and not (f in seen_field or seen_field.add(f))]
    # Per-field signatures already shown, so the same clause never renders twice
    # under one field — collapses both the ×N sheet repetition and distinct rules
    # that quote the identical clause paragraph (the UI shows only the text).
    seen_sig: dict[str, set] = {}
    try:
        with CanonicalSession() as cs:
            rows = cs.execute(
                text("SELECT rule_id, rule_name, severity, canonical_target, "
                     "rule_spec, error_message, source_verbatim_text, "
                     "source_page_number FROM validation_rule "
                     "WHERE contract_id=:c AND rule_status != 'disabled'"),
                {"c": contract_id}).mappings().all()
    except Exception as e:  # noqa: BLE001
        log.warning("contract clause lookup failed: %s", e)
        return out
    for r in rows:
        # The output field(s) the rule EXPLICITLY targets (canonical_target) — the
        # high-confidence link. A field that appears here is an exact clause match.
        target = r.get("canonical_target")
        if isinstance(target, str):
            try:
                target = json.loads(target)
            except (ValueError, TypeError):
                target = {}
        target = target or {}
        # Schedule scope: a clause that NAMES its schedules ("between Schedule
        # G, H, I, J") is compiled to exactly those sheets. Carry that through so
        # the display can show the clause on the sheets it truly applies to,
        # while unscoped clauses stay confined to their contract's sheet(s).
        spec = r.get("rule_spec")
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except (ValueError, TypeError):
                spec = None
        spec = spec if isinstance(spec, dict) else {}
        _params = ((spec.get("ir") or {}).get("params")) or {}
        scoped = bool(_params.get("scope_sheets") or _params.get("sheets"))
        sql_sheets = ([s.strip() for s in
                       re.findall(r'FROM\s+"([^"]+)"', spec.get("compiled_sql") or "")]
                      if scoped else [])
        targeted = set()
        if target.get("output_field"):
            targeted.add(str(target["output_field"]).strip().lower())
        for f in (target.get("output_fields") or []):
            if f:
                targeted.add(str(f).strip().lower())
        # Columns the rule REFERENCES beyond its primary target: the IR's param
        # values (other_field, scope, group_by, …). Regex/operator params are
        # skipped — they are values, not column names.
        referenced = set(targeted)
        for _v in _params.values():
            for _item in (_v if isinstance(_v, list) else [_v]):
                if not isinstance(_item, str):
                    continue
                _s = _item.strip()
                if len(_s) > 1 and not _RULE_PARAM_NOISE.search(_s):
                    referenced.add(_s.lower())
        # A "related" link is now a real similarity score against those column
        # names, kept only at/above _RELATED_MIN. It used to be a substring test
        # over a JSON dump of the whole rule — which included free prose, so any
        # column whose name is an ordinary word attached to unrelated rules
        # ("TIV not NULL — Total Insured Value…" surfaced under an "Insured"
        # column). Comparing column-to-column instead of column-to-prose is what
        # makes the threshold meaningful.
        clause_text = (r.get("source_verbatim_text") or r.get("error_message")
                       or r.get("rule_name"))
        for fld in field_names:
            if not fld:
                continue
            fl = fld.strip().lower()
            if fl in targeted:
                match, score = "exact", 1.0
            else:
                best = max((SequenceMatcher(None, fl, ref).ratio()
                            for ref in referenced), default=0.0)
                if best < _RELATED_MIN:
                    continue
                match, score = "related", round(best, 2)
            sig = (match, (clause_text or "").strip(),
                   (r.get("severity") or ""))
            if sig in seen_sig.setdefault(fld, set()):
                continue
            seen_sig[fld].add(sig)
            out.setdefault(fld, []).append({
                "rule_id": r.get("rule_id"), "rule_name": r.get("rule_name"),
                "severity": r.get("severity"), "text": clause_text,
                "page": r.get("source_page_number"),
                "match": match, "score": score,
                "scoped": scoped, "sql_sheets": sql_sheets,
            })
    return out


def _unbound_setup_contract_id(program_id: Optional[int],
                               output_template_id: Optional[int]) -> Optional[int]:
    """The contract a setup should read when it carries NO binding of its own.

    A setup is scoped to one (carrier, program), and contract upload keeps
    exactly one ACTIVE contract per program for a given output template, so that
    contract IS the one the build would have bound — the binding is a cache of a
    fact the server already owns, not an independent choice.

    This matters because the binding is written from a single value the browser
    reads out of the upload response, and that response can be lost in transit
    (extraction outlives an ingress request cap; the pipeline task survives and
    persists the contract, the answer does not). A setup left unbound that way
    shows ZERO rules while its contract sits in the database with all of them —
    silently, because "no contract bound" and "contract has no rules" look
    identical on screen. Resolving it here means the rules appear anyway.

    Deliberately narrow: consulted ONLY when the setup has no binding at all
    (no fallback contract AND no per-sheet pins), so it can never override or
    leak past a binding somebody actually made.
    """
    if not program_id or not output_template_id:
        return None
    try:
        with SessionLocal() as s:
            # Currency is no longer stored: every approved version stays
            # `active` and the calendar says which is current, so this asks for
            # the version in force TODAY rather than the newest id.
            from contract_upload_services.contract_asof import current_for_template
            cid = current_for_template(s, output_template_id)
            if cid is None:
                return None
            # The template lookup is programme-agnostic; keep the original
            # programme guard so a setup can never adopt another one's contract.
            owner = s.query(Contract.program_id).filter(Contract.id == cid).scalar()
            return cid if owner == program_id else None
    except Exception as e:  # noqa: BLE001 — a failed lookup must not break the page
        log.warning("unbound-setup contract lookup failed: %s", e)
        return None


def _attach_clauses(fields: list[dict], contract_id: Optional[int],
                    sheet_contracts: Optional[dict]) -> None:
    """Attach each (sheet, field) row's contract clauses IN PLACE, honouring the
    per-sheet contract governance:

    - No sheet↔contract mapping saved → legacy: the single contract's clauses
      show under its field on EVERY sheet.
    - Mapping saved → an UNSCOPED clause shows only on the sheet(s) its own
      contract governs (a pinned contract never leaks onto other sheets); a
      SCOPED clause (names its schedules, e.g. "G, H, I, J") shows on exactly
      the sheets its compiled SQL targets.
    """
    names = [f["field"] for f in fields]
    mapped = {str(k).strip(): int(v)
              for k, v in (sheet_contracts or {}).items() if v}
    if not mapped:
        clauses = _contract_clauses_by_field(contract_id, names)
        for f in fields:
            f["clauses"] = clauses.get(f["field"], [])
        return
    pinned = set(mapped.values())
    default_cid = contract_id if (contract_id and contract_id not in pinned) else None
    gov_cids: list[int] = []
    for cid in list(mapped.values()) + ([default_cid] if default_cid else []):
        if cid not in gov_cids:
            gov_cids.append(cid)
    by_cid = {cid: _contract_clauses_by_field(cid, names) for cid in gov_cids}
    for f in fields:
        sheet_key = str(f.get("sheet") or "").strip()
        own = mapped.get(sheet_key, default_cid)
        out: list[dict] = []
        for cid in gov_cids:
            for c in by_cid[cid].get(f["field"], []):
                if c.get("scoped"):
                    if sheet_key in (c.get("sql_sheets") or []):
                        out.append(c)
                elif cid == own:
                    out.append(c)
        f["clauses"] = out


@router.get("/direct/output-fields")
def direct_output_fields(template_id: int, contract_id: Optional[int] = None,
                         format_id: Optional[int] = None,
                         principal: Principal = Depends(current_principal)):
    """List the output template's columns (the searchable picker) with the
    contract clause bound to each — so changing the output field carries its
    clause along. Pass `format_id` to honour the setup's sheet↔contract mapping:
    each sheet's fields then only show clauses from the contract that governs
    that sheet (scoped clauses show on the schedules they name)."""
    sheet_contracts = None
    program_id = None
    with SessionLocal() as s:
        tpl = s.get(ExportTemplate, template_id)
        if not tpl:
            raise HTTPException(404, "output template not found")
        structure = _load_structure(tpl)
        if format_id:
            fmt = s.get(DirectFormat, format_id)
            if fmt:
                assert_tenant_owns(principal, fmt.tenant_id)
                sheet_contracts = fmt.sheet_contracts
                contract_id = contract_id or fmt.contract_id
                program_id = fmt.program_id
    # Setup carries no contract binding at all — fall back to the program's
    # active contract for this template (see _unbound_setup_contract_id).
    if not contract_id and not (sheet_contracts or {}):
        contract_id = _unbound_setup_contract_id(program_id, template_id)
    fields: list[dict] = []
    for sh in structure.get("sheets", []):
        if is_reference_sheet(sh):
            continue          # reference/lookup tab — no rule fields shown for it
        for c in sorted(sh.get("columns", []), key=lambda x: x.get("column_index", 0)):
            name = c.get("column_name")
            if name:
                fields.append({"sheet": sh.get("sheet_name", ""), "field": name})
    _attach_clauses(fields, contract_id, sheet_contracts)
    return {"template_id": template_id, "contract_id": contract_id, "fields": fields}


@router.get("/direct/format/{format_id}/editor")
def direct_format_editor(format_id: int,
                         principal: Principal = Depends(current_principal)):
    """Rebuild the full Setup editor view for an EXISTING setup: its saved routing
    + column mapping, the output template's fields (with contract clauses), and the
    input columns/sheets from its latest landing record — so a saved setup can be
    reopened, reviewed and edited exactly like a fresh upload."""
    with SessionLocal() as s:
        f = s.get(DirectFormat, format_id)
        if not f:
            raise HTTPException(404, "format not found")
        assert_tenant_owns(principal, f.tenant_id)
        tpl = s.get(ExportTemplate, f.output_template_id) if f.output_template_id else None
        structure = _load_structure(tpl) if tpl else {"sheets": []}
        output_sheets = _output_sheet_names(structure)
        rec = (s.query(LandingRecord)
               .filter(LandingRecord.format_id == format_id)
               .order_by(LandingRecord.id.desc()).first())
        input_sheets: list[str] = []
        input_columns: dict[str, list[str]] = {}
        landing_id = None
        if rec and rec.data:
            for name, sheet in (rec.data.get("sheets") or {}).items():
                input_sheets.append(name)
                input_columns[name] = sheet.get("columns") or []
            landing_id = rec.id
        routing = f.sheet_routing or dl.propose_sheet_routing(input_sheets, output_sheets)
        column_mapping = f.column_mapping or {}
        candidates = f.candidates or {}
        # NULL on a setup built before the mapper recorded its reasoning; the
        # review then shows nothing rather than claiming everything was fine.
        mapping_decisions = f.mapping_decisions or {}
        contract_id = f.contract_id
        template_id = f.output_template_id
        sheet_contracts = f.sheet_contracts
        program_id = f.program_id

    # Setup carries no contract binding at all — fall back to the program's
    # active contract for this template (see _unbound_setup_contract_id).
    if not contract_id and not (sheet_contracts or {}):
        contract_id = _unbound_setup_contract_id(program_id, template_id)

    fields: list[dict] = []
    for sh in structure.get("sheets", []):
        if is_reference_sheet(sh):
            continue          # reference/lookup tab — no rule fields shown for it
        for c in sorted(sh.get("columns", []), key=lambda x: x.get("column_index", 0)):
            nm = c.get("column_name")
            if nm:
                fields.append({"sheet": sh.get("sheet_name", ""), "field": nm})
    _attach_clauses(fields, contract_id, sheet_contracts)

    return {
        "format_id": format_id, "landing_id": landing_id, "known_format": True,
        "template_id": template_id, "contract_id": contract_id,
        "input_sheets": input_sheets, "input_columns": input_columns,
        "output_sheets": output_sheets, "sheet_routing": routing,
        "column_mapping": column_mapping, "candidates": candidates,
        "mapping_review": _mapping_review(mapping_decisions),
        "fields": fields,
    }


@router.get("/direct/setup")
def direct_setup_get(mga: str, carrier_party_id: Optional[int] = None,
                     program_id: Optional[int] = None,
                     principal: Principal = Depends(current_principal)):
    """List the direct-lane setup(s) for a carrier + program (the active approved
    one is the binding used by /direct/run)."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        q = s.query(DirectFormat).filter(DirectFormat.tenant_id == tid)
        if carrier_party_id is not None:
            q = q.filter(DirectFormat.carrier_party_id == carrier_party_id)
        if program_id is not None:
            q = q.filter(DirectFormat.program_id == program_id)
        return [_format_to_dict(f) for f in q.order_by(DirectFormat.id.desc()).all()]


# ---- Pipeline CRUD ---------------------------------------------------------

def _replace_pipeline_contracts(s, pipe: Pipeline, contracts: list) -> None:
    """Replace a pipeline's contract set. De-dupes by (contract_id, sheet_key)
    and assigns positions in order."""
    s.query(PipelineContract).filter(
        PipelineContract.pipeline_id == pipe.id).delete()
    seen: set = set()
    pos = 0
    for c in contracts:
        key = (c.contract_id, c.sheet_key)
        if key in seen:
            continue
        seen.add(key)
        s.add(PipelineContract(
            tenant_id=pipe.tenant_id, pipeline_id=pipe.id,
            contract_id=c.contract_id, sheet_key=c.sheet_key, position=pos))
        pos += 1


@router.post("/pipelines")
def pipeline_create(body: PipelineCreate, mga: Optional[str] = None,
                    principal: Principal = Depends(current_principal)):
    """Create a draft pipeline (input template + output template + contracts).
    Activate it separately via POST /pipelines/{id}/activate."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        pipe = Pipeline(
            tenant_id=tid, name=body.name,
            carrier_party_id=body.carrier_party_id, program_id=body.program_id,
            broker_party_id=body.broker_party_id,
            input_format_id=body.input_format_id,
            output_template_id=body.output_template_id, status="draft")
        s.add(pipe)
        s.flush()
        _replace_pipeline_contracts(s, pipe, body.contracts)
        s.commit()
        s.refresh(pipe)
        return _pipeline_to_dict(s, pipe)


@router.get("/pipelines")
def pipeline_list(
    mga: str,
    carrier_party_id: Optional[int] = None,
    program_id: Optional[int] = None,
    broker_party_id: Optional[int] = None,
    # Narrow to the runs made against ONE contract. A bordereau is validated
    # against a contract's rules, so this is the scope the broker's own history
    # is read at — a broker with two contracts on one programme is answering to
    # two different sets of rules and should not see them mixed.
    contract_id: Optional[int] = None,
    q: Optional[str] = None,
    status: Optional[str] = None,
    page: Optional[int] = Query(None, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=200),
    with_facets: bool = False,
    principal: Principal = Depends(current_principal),
):
    """List pipelines for a carrier + program (the active one is what
    /direct/run executes against). Pagination is opt-in — DirectSetup.tsx and
    DirectRun.tsx call this scoped to one carrier+program (page omitted),
    expecting every match in the small, unpaginated result; BordereauSetups.tsx
    ("Bordereau Setups") passes page for a true server-paged list."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        query = s.query(Pipeline).filter(Pipeline.tenant_id == tid)
        if carrier_party_id is not None:
            query = query.filter(Pipeline.carrier_party_id == carrier_party_id)
        if program_id is not None:
            query = query.filter(Pipeline.program_id == program_id)
        if broker_party_id is not None:
            # A broker's own setup, plus any programme-wide setup that also
            # applies to them — asking for one broker must not hide the setup
            # that covers everybody.
            query = query.filter(or_(Pipeline.broker_party_id == broker_party_id,
                                     Pipeline.broker_party_id.is_(None)))
        if status:
            query = query.filter(Pipeline.status == status)
        # Setups whose program has been deactivated are hidden everywhere the
        # setup list is consumed (All Setups, Bordereau setup, Process bordereau)
        # — done in SQL (not a post-fetch Python filter) so it composes with
        # true pagination instead of shrinking an already-sliced page.
        inactive_program_ids = (s.query(Program.id)
                                .filter(Program.tenant_id == tid, Program.status == "inactive"))
        query = query.filter(or_(Pipeline.program_id.is_(None),
                                  ~Pipeline.program_id.in_(inactive_program_ids)))
        if q and q.strip():
            ql = f"%{q.strip().lower()}%"
            query = (query
                     .outerjoin(Party, Pipeline.carrier_party_id == Party.id)
                     .outerjoin(Program, Pipeline.program_id == Program.id)
                     .filter(or_(
                         func.lower(func.coalesce(Party.legal_name, "")).like(ql),
                         func.lower(func.coalesce(Program.name, "")).like(ql),
                     )))

        total = query.order_by(None).count()
        # Newest first. Id descending already achieved this, but by accident of
        # insertion order rather than by saying so — created_at is the fact the
        # list is meant to be in, with id as the tiebreak.
        ordered = query.order_by(Pipeline.created_at.desc().nullslast(),
                                 Pipeline.id.desc())
        if page is not None:
            size = page_size or 10
            ordered = ordered.offset((page - 1) * size).limit(size)
        items = [_pipeline_to_dict(s, p) for p in ordered.all()]
        if page is not None:
            out = {"items": items, "total": int(total), "page": page,
                   "page_size": page_size or 10}
            if with_facets:
                # Distinct carrier/program pairs across every setup this tenant
                # has (NOT just the current page), so the filter dropdowns list
                # every option. Returned here so the list screen doesn't have to
                # fetch the whole unpaginated table a second time just to derive
                # them — one round-trip instead of two, and a few rows instead
                # of the entire result set.
                facet_rows = (s.query(Pipeline.carrier_party_id, Party.legal_name,
                                      Pipeline.program_id, Program.name)
                              .outerjoin(Party, Pipeline.carrier_party_id == Party.id)
                              .outerjoin(Program, Pipeline.program_id == Program.id)
                              .filter(Pipeline.tenant_id == tid)
                              .filter(or_(Pipeline.program_id.is_(None),
                                          ~Pipeline.program_id.in_(inactive_program_ids)))
                              .distinct().all())
                carriers, programs, pairs = {}, {}, []
                for cid, cname, pid, pname in facet_rows:
                    if cid is not None and cname:
                        carriers[cid] = cname
                    if pid is not None and pname:
                        programs[pid] = pname
                    if cid is not None and pid is not None:
                        pairs.append({"carrier_party_id": cid, "program_id": pid})
                out["facets"] = {
                    "carriers": [{"id": i, "name": n} for i, n in
                                 sorted(carriers.items(), key=lambda kv: kv[1].lower())],
                    "programs": [{"id": i, "name": n} for i, n in
                                 sorted(programs.items(), key=lambda kv: kv[1].lower())],
                    # Lets the UI keep narrowing each dropdown by the other's pick.
                    "pairs": pairs,
                }
            return out
        return items


@router.get("/pipelines/{pipeline_id}")
def pipeline_get(pipeline_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        p = s.get(Pipeline, pipeline_id)
        if not p:
            raise HTTPException(404, "pipeline not found")
        assert_tenant_owns(principal, p.tenant_id)
        # derive_refs: this is ONE setup being opened, so it can afford to
        # re-derive the reference documents of a setup built before they were
        # recorded. The list endpoint deliberately does not.
        return _pipeline_to_dict(s, p, derive_refs=True)


@router.put("/pipelines/{pipeline_id}")
def pipeline_update(pipeline_id: int, body: PipelineUpdate,
                    principal: Principal = Depends(current_principal)):
    """Edit a pipeline's templates / contracts. Does NOT change status — use the
    activate endpoint for that."""
    with SessionLocal() as s:
        p = s.get(Pipeline, pipeline_id)
        if not p:
            raise HTTPException(404, "pipeline not found")
        assert_tenant_owns(principal, p.tenant_id)
        if body.name is not None:
            p.name = body.name
        if body.broker_party_id is not None:
            p.broker_party_id = body.broker_party_id
        if body.input_format_id is not None:
            p.input_format_id = body.input_format_id
        if body.output_template_id is not None:
            p.output_template_id = body.output_template_id
        if body.contracts is not None:
            _replace_pipeline_contracts(s, p, body.contracts)
        p.modified_at = datetime.utcnow()
        s.commit()
        s.refresh(p)
        return _pipeline_to_dict(s, p)


@router.post("/pipelines/{pipeline_id}/activate")
def pipeline_activate(pipeline_id: int,
                      principal: Principal = Depends(current_principal)):
    """Activate a pipeline (requires input template + output template + >=1
    contract). Supersedes the live pipelines it replaces — same carrier, program
    and broker, covering any of the same contracts (see _activate_pipeline).

    Also raises the admin data-model mapping task for the input format, when it
    doesn't have one yet — see `_queue_datamodel_mapping`, and notifies Kavachio
    platform admins — see `_notify_setup_activated`. Both run after the commit
    below and swallow their own errors, so neither can affect activation."""
    with SessionLocal() as s:
        p = s.get(Pipeline, pipeline_id)
        if not p:
            raise HTTPException(404, "pipeline not found")
        assert_tenant_owns(principal, p.tenant_id)
        # Re-clicking Activate on the setup that is ALREADY live is a no-op, so
        # it must not raise a second notification — only a real draft/superseded
        # → active transition is "a setup was activated".
        became_active = (p.status or "").strip().lower() != "active"
        _activate_pipeline(s, p)
        s.commit()
        s.refresh(p)
        result = _pipeline_to_dict(s, p)
        tenant_id, format_id = p.tenant_id, p.input_format_id

    actor = _principal_email(principal)
    # Raise the mapping task FIRST: the notification is about that task, so it
    # has to exist before we look for it.
    _queue_datamodel_mapping(tenant_id, format_id, actor)
    if became_active:
        _notify_setup_activated(result, actor, _principal_name(principal))
    return result


@router.delete("/pipelines/{pipeline_id}")
def pipeline_delete(pipeline_id: int,
                    principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        p = s.get(Pipeline, pipeline_id)
        if not p:
            raise HTTPException(404, "pipeline not found")
        assert_tenant_owns(principal, p.tenant_id)
        s.query(PipelineContract).filter(
            PipelineContract.pipeline_id == p.id).delete()
        s.query(MissingBdxColumn).filter(
            MissingBdxColumn.pipeline_id == p.id).delete()
        s.delete(p)
        s.commit()
        return {"deleted": pipeline_id}


# ---- Missing BDX columns (contract vs. bordereau gap) -----------------------
# The check itself lives in missing_columns.py; these two routes are the read
# path (every visit to a setup) and the write path (once, after a setup is
# built). Kept separate on purpose: the read NEVER calls a model.

def _pipeline_for_principal(s, pipeline_id: int, principal: Principal) -> Pipeline:
    p = s.get(Pipeline, pipeline_id)
    if not p:
        raise HTTPException(404, "pipeline not found")
    assert_tenant_owns(principal, p.tenant_id)
    return p


@router.get("/pipelines/{pipeline_id}/missing-columns")
def pipeline_missing_columns(pipeline_id: int,
                             principal: Principal = Depends(current_principal)):
    """The stored NOTE for a setup: the columns its contract expects that its
    bordereau doesn't provide, minus any whose output field is already mapped.
    No contract parsing and no model call — so the setup screen can call it on
    every load, and re-call it after a mapping change to see the entry go away.

    `analyzed` is false when this setup has never been checked (e.g. it was built
    before this check existed); the caller can then POST .../analyze once."""
    with SessionLocal() as s:
        _pipeline_for_principal(s, pipeline_id, principal)
    return mc.get_for_pipeline(pipeline_id)


@router.post("/pipelines/{pipeline_id}/missing-columns/analyze")
async def pipeline_missing_columns_analyze(
    pipeline_id: int, force: bool = False,
    principal: Principal = Depends(current_principal),
):
    """Run the contract-vs-bordereau check and store the result.

    Idempotent: an already-checked setup returns its stored snapshot without
    spending a model call (`force=true` re-checks, e.g. after a rebuild). Never
    fails the caller — when the check can't run (no contract, no sample file, no
    model configured) it returns the stored snapshot with `skipped_reason` set,
    so the setup flow that calls this continues exactly as before."""
    with SessionLocal() as s:
        _pipeline_for_principal(s, pipeline_id, principal)
    # Blocking (SQL + one model call) — off the event loop.
    return await run_in_threadpool(mc.analyze_pipeline, pipeline_id, force)


@router.get("/direct/runs")
def direct_runs(
    mga: str,
    carrier_party_id: Optional[int] = None,
    carrier_ids: Optional[str] = None,  # comma-separated — RecentRuns.tsx's multi-select
    program_id: Optional[int] = None,
    # Narrow to ONE broker's own submissions. The carrier never sends this (it
    # wants the whole programme); the broker's nested route always does, because
    # two brokers on the same programme share a setup and would otherwise read
    # each other's run history off the same DirectFormat.
    broker_party_id: Optional[int] = None,
    # Narrow to the runs made against ONE contract. A bordereau is validated
    # against a contract's rules, so this is the scope the broker's own history
    # is read at — a broker with two contracts on one programme is answering to
    # two different sets of rules and should not see them mixed.
    contract_id: Optional[int] = None,
    q: Optional[str] = None,
    result: Optional[str] = None,       # "clean" | "exceptions"
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: Optional[int] = Query(None, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=200),
    limit: int = 20,
    principal: Principal = Depends(current_principal),
):
    """Direct-lane runs (uploaded file → generated output), newest first, so the
    Direct page shows what was uploaded. Only landing records that actually
    produced an output are returned (output_export_id set) — setup samples are
    excluded. Scope to a carrier + program when given, else the whole tenant.

    Pagination is opt-in: pass `page` for TRUE server-side paging (search/
    carrier/result/date filters applied in SQL, only one page returned, plus
    the matching total — {"items", "total", "page", "page_size"}). Without
    `page`, behaves as before: a plain list capped at min(limit, 100) — this
    old shape silently dropped anything past the 100th most-recent run with
    no way to reach it, which is exactly what true pagination fixes."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        if tid is None:
            return {"items": [], "total": 0, "page": page, "page_size": page_size} if page is not None else []

        # Select ONLY the columns this listing renders — never whole entities.
        # Each of these three tables carries multi-MB payload columns that the
        # list has no use for: OutputExport.exceptions (the full exception list,
        # up to ~1.5 MB per run) and OutputExport.blob (the generated xlsx —
        # 145 MB across the table), LandingRecord.data (the parsed input rows,
        # 159 MB), plus five JSON config columns on DirectFormat. Loading the
        # entities pulled all of that over the wire and through JSON decoding
        # just to read `exception_count` and a few strings, so a page of 10 runs
        # cost 0.3–1.4 s locally (and far worse against a remote DB) and got
        # slower the more exceptions a run had. Column-only: ~3 ms, flat across
        # pages. `total` below counts over the same joins without a row body.
        query = (s.query(
                     LandingRecord.id, LandingRecord.source_filename,
                     LandingRecord.row_count, LandingRecord.created_at,
                     LandingRecord.datamodel_status,
                     DirectFormat.carrier_party_id, DirectFormat.program_id,
                     OutputExport.id, OutputExport.filename,
                     OutputExport.exception_count, OutputExport.status,
                     Party.legal_name, Program.name)
                 .join(DirectFormat, LandingRecord.format_id == DirectFormat.id)
                 .join(OutputExport, LandingRecord.output_export_id == OutputExport.id)
                 .outerjoin(Party, DirectFormat.carrier_party_id == Party.id)
                 .outerjoin(Program, DirectFormat.program_id == Program.id)
                 .filter(LandingRecord.tenant_id == tid))
        if carrier_party_id is not None:
            query = query.filter(DirectFormat.carrier_party_id == carrier_party_id)
        ids = [int(x) for x in (carrier_ids or "").split(",") if x.strip().lstrip("-").isdigit()]
        if ids:
            query = query.filter(DirectFormat.carrier_party_id.in_(ids))
        if program_id is not None:
            query = query.filter(DirectFormat.program_id == program_id)
        # Filtered on the EXPORT, not the format: the format is the shared
        # setup, while output_exports.broker_party_id is the scope the run was
        # actually made for (stamped by _render_landing from run_scope).
        if broker_party_id is not None:
            query = query.filter(OutputExport.broker_party_id == broker_party_id)
        if contract_id is not None:
            query = query.filter(OutputExport.contract_id == contract_id)
        if result == "clean":
            query = query.filter(OutputExport.status == "clean")
        elif result == "exceptions":
            query = query.filter(OutputExport.status != "clean", OutputExport.exception_count > 0)
        if q and q.strip():
            ql = f"%{q.strip().lower()}%"
            query = query.filter(or_(
                func.lower(func.coalesce(LandingRecord.source_filename, "")).like(ql),
                func.lower(func.coalesce(OutputExport.filename, "")).like(ql),
                func.lower(func.coalesce(Party.legal_name, "")).like(ql),
                func.lower(func.coalesce(Program.name, "")).like(ql),
            ))
        df_, dt_ = _parse_client_dt(date_from), _parse_client_dt(date_to)
        if df_:
            query = query.filter(LandingRecord.created_at >= df_)
        if dt_:
            query = query.filter(LandingRecord.created_at <= dt_)

        if page is not None:
            # Reuse the same joins + filters, but swap the select list for a
            # bare count so no row body is fetched at all — `.count()` would
            # wrap the whole select in a subquery instead.
            total = query.order_by(None).with_entities(func.count(LandingRecord.id)).scalar() or 0
            size = page_size or 20
            rows = (query.order_by(LandingRecord.id.desc())
                    .offset((page - 1) * size).limit(size).all())
        else:
            rows = query.order_by(LandingRecord.id.desc()).limit(min(limit, 100)).all()

        items = [{
            "landing_id": landing_id,
            "source_filename": source_filename,
            "row_count": row_count or 0,
            "created_at": _iso_utc(created_at),
            "carrier_party_id": carrier_party_id_,
            "program_id": program_id_,
            "carrier_name": carrier_name,
            "program_name": program_name,
            "export_id": export_id,
            "filename": filename,
            "exception_count": exception_count or 0,
            "status": status_ or "clean",
            "datamodel_status": datamodel_status,
        } for (landing_id, source_filename, row_count, created_at, datamodel_status,
               carrier_party_id_, program_id_, export_id, filename,
               exception_count, status_, carrier_name, program_name) in rows]

        if page is not None:
            return {"items": items, "total": int(total), "page": page, "page_size": page_size or 20}
        return items


def _run_contract_for_render(chosen: Optional[int], pipeline_id: Optional[int],
                             legacy_fallback: Optional[int]) -> Optional[int]:
    """Which contract a run hands the renderer, in one place.

    THE CHOICE ON THE SCREEN WINS. Named on the run, the contract is what the
    file is measured against — it replaces the setup's own sheet-less pin, which
    is the contract the setup happened to be built on rather than the one this
    bordereau was written under. That pin used to win unconditionally, so a
    broker's second contract could be chosen on screen and never reach the
    validation at all.

    Named nothing, this is byte-for-byte what it always did: with a pipeline the
    pipeline's own contracts govern (None, so the renderer reads them off it),
    and on the legacy no-pipeline path the format's contract stays the fallback.
    """
    if chosen:
        return chosen
    return None if pipeline_id else legacy_fallback


def _governing_ids(sheet_contracts: dict, eff_contract_id: Optional[int],
                   asof_extra_ids=None) -> list:
    """Every contract whose rules run on this bordereau, in order, deduplicated.

    Per-schedule pins FIRST and always: a setup that gives each output sheet its
    own contract is answering a different question from "which contract is this
    file under", and one answer there must never wipe out several here. Each
    such rule only fires on the sheets its compiled SQL names, so they coexist.
    """
    out: list = []
    for cid in ([int(v) for v in sheet_contracts.values() if v]
                + ([eff_contract_id] if eff_contract_id else [])
                + list(asof_extra_ids or [])):
        if cid not in out:
            out.append(cid)
    return out


def _assert_run_contract(s, tid: int, program_id: int,
                         broker_party_id: Optional[int],
                         contract_id: int) -> Contract:
    """The contract this run says its bordereau is written under, checked.

    It arrives from a form field, and a form field is a request — not a fact.
    Left unchecked it would let a run be pointed at any contract id in the
    database, and the terms of somebody else's binder would then be the terms
    this bordereau was measured against. So the same rule the picker is built
    from is applied again here, on the server:

      · the contract is on THIS programme, and
      · it belongs to the broker this run named, or to no broker at all — a
        carrier-held contract predates the broker level and governs the whole
        programme.

    Refused with a plain 400 rather than silently ignored: a run that quietly
    measured the file against a different contract than the screen said is the
    failure this whole change exists to prevent.
    """
    c = s.get(Contract, contract_id)
    prog = s.get(Program, program_id)
    if c is None or prog is None or prog.tenant_id != tid or c.tenant_id != tid:
        raise HTTPException(400, "that contract is not on this programme")
    if c.program_id != program_id:
        raise HTTPException(
            400, "that contract belongs to a different programme, so its terms "
                 "do not govern this bordereau")
    if c.broker_party_id is not None and c.broker_party_id != broker_party_id:
        raise HTTPException(
            400, "that contract belongs to a different broker. A contract "
                 "belongs to one programme and one broker — pick the broker it "
                 "is held by, or choose one of this broker's own.")
    return c


@router.post("/direct/run")
async def direct_run(
    mga: str = Form(...),
    carrier_party_id: int = Form(...),
    program_id: int = Form(...),
    file: UploadFile = File(...),
    filename: Optional[str] = Form(default=None),
    actor: Optional[str] = Form(default=None),
    skip_rows: int = Form(default=0),
    check_only: bool = Form(default=False),
    # The broker and contract this bordereau is FOR. Both optional: a setup made
    # before the broker level existed sends neither and behaves exactly as it
    # always has. When they ARE sent, they pick the output template the four
    # levels agreed on — see plan sections 18/19.
    #
    # `contract_id` also decides WHOSE TERMS the file is measured against. A
    # broker with two live contracts has two different sets of terms, and which
    # of them governs a given bordereau is a question only the person holding
    # the file can answer. It used to be dropped the moment a setup was running
    # — the setup's own pinned contract governed every run — so a bordereau
    # written under the second contract was silently checked against the first.
    broker_party_id: Optional[int] = Form(default=None),
    contract_id: Optional[int] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    """DATA step: ops uploads a real data file for a carrier + program. Uses the
    active setup (DirectFormat) for that pair — no mapping review needed. If the
    setup has a supplementary data file, its sheets are captured alongside the
    BDX automatically (no per-run upload).

    ``check_only`` is the broker pre-submission self-check (V-5): run every
    validation and return the full row/field fix-list WITHOUT ingesting to the
    data model, creating an admin task, or recording a run — a dry run to answer
    "will this pass before I send it?"."""
    file_bytes = await file.read()
    sheets_dict = await run_in_threadpool(read_excel_all_sheets, file_bytes, skip_rows)
    if not sheets_dict:
        raise HTTPException(400, "workbook has no readable sheets")

    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        if contract_id:
            _assert_run_contract(s, tid, program_id, broker_party_id, contract_id)
        # Resolve the active PIPELINE for this carrier+program — that's the
        # config a run executes against. The pipeline's Input Template (a
        # DirectFormat) supplies the input layout below.
        # This broker's own setup first, then the programme-wide one — and when
        # the bordereau names its contract, the setup built for THAT contract.
        # A broker with two contracts on two templates has two live setups;
        # setup_scope decides between them, the same answer every screen shows.
        from setup_scope import live_setup_for
        pipe = live_setup_for(s, tid, carrier_party_id, program_id,
                              broker_party_id, contract_id)
        if pipe and pipe.input_format_id:
            fmt = s.get(DirectFormat, pipe.input_format_id)
        else:
            # Temporary rollout fallback: no pipeline yet → use the legacy
            # approved-DirectFormat so an in-flight setup keeps running until it
            # is backfilled/activated as a pipeline. Remove after soak (plan Step 7).
            pipe = None
            fmt = (s.query(DirectFormat)
                   .filter(DirectFormat.tenant_id == tid,
                           DirectFormat.carrier_party_id == carrier_party_id,
                           DirectFormat.program_id == program_id,
                           DirectFormat.approved == 1)
                   .order_by(DirectFormat.id.desc()).first())
        if not fmt:
            raise HTTPException(
                400, "no active pipeline for this carrier + program — activate "
                     "one on the Setup page first")

        # Multi-table DATA sheets would be processed as ONE table each, silently
        # misreading everything under the second header — refuse the run
        # outright (still above the heartbeat stream: a real 400). Scope: the
        # sheets this setup's ROUTING actually reads as data; summary/reference
        # tabs riding along in the workbook are ignored. No saved routing yet →
        # every non-spec sheet counts.
        _scope = None
        _routing = fmt.sheet_routing or {}
        _routed = {str(src.get("input_sheet")).strip()
                   for r in (_routing.get("routes") or [])
                   for src in (r.get("sources") or []) if src.get("input_sheet")}
        if _routed:
            _scope = [n for n in sheets_dict.keys()
                      if str(n).strip() in _routed]
        else:
            _specs = await run_in_threadpool(spec_sheet_names, file_bytes)
            _scope = [k for k in sheets_dict.keys() if k not in _specs]
        multi_tables = await run_in_threadpool(
            detect_multiple_tables, file_bytes, skip_rows, _scope)
        if multi_tables:
            # Name-blind summary/reference tabs: same classifier, final word.
            multi_tables = await run_in_threadpool(
                filter_findings_to_data_sheets, multi_tables,
                {n: list(map(str, df.columns)) for n, df in sheets_dict.items()})
        if multi_tables:
            raise HTTPException(400, _multi_table_error(multi_tables))
        # Restrict to the input sheets the setup actually maps — extra sheets in
        # the ops file are ignored, so the fingerprint stays comparable to setup.
        wanted = _routing_input_sheets(fmt.sheet_routing)
        if wanted:
            filtered = {k: v for k, v in sheets_dict.items() if k in wanted}
            if filtered:
                sheets_dict = filtered
        landing = await run_in_threadpool(dl.build_landing_record, sheets_dict)

        # Supplement: capture the setup's stored supplementary sheets alongside the
        # BDX (no policy-number join — a supplement file just carries extra data).
        # Uploaded once on the Setup page; nothing to upload here.
        supp_stats = None
        supp_cfg = fmt.supplement or {}
        if supp_cfg.get("enabled") and supp_cfg.get("landing"):
            supp_stats = dl.attach_supplement(landing, supp_cfg["landing"])

        fp = signature_hash(signature_multi(sheets_dict))
        drift = bool(fmt.fingerprint and fmt.fingerprint != fp)
        eff_contract_id = contract_id or fmt.contract_id

        # A scope was named, so the output template must be the one agreed for
        # it. Refuse rather than fall back onto an unrelated template — the
        # screen offers to create one (plan section 19).
        run_scope = ({"carrier_party_id": carrier_party_id,
                      "program_id": program_id,
                      "broker_party_id": broker_party_id,
                      "contract_id": contract_id}
                     if (broker_party_id or contract_id) else None)

        # The setup that will run, and the template agreed for the scope. When
        # they disagree, refuse: the mapping below belongs to the setup's
        # template, so writing into the other one yields headings with nothing
        # under them.
        if run_scope is not None:
            conflict = _scope_template_conflict(s, tid, run_scope, pipe, fmt)
            if conflict:
                raise HTTPException(400, conflict)

        rec = LandingRecord(
            tenant_id=tid, format_id=fmt.id, source_filename=file.filename,
            fingerprint=fp, data=landing, row_count=landing["row_count"],
            datamodel_status="pending")
        s.add(rec)
        s.commit()
        s.refresh(rec)
        landing_id = rec.id
        pipeline_id = pipe.id if pipe else None

    # Validation + output generation can exceed the Azure ingress ~4-min idle
    # timeout, so run it under a heartbeat stream (whitespace bytes keep the
    # connection alive; the JSON result is the final chunk). Everything above
    # this line still returns real 4xx (no sheets / no active pipeline); a
    # failure past here arrives in the body and the frontend interceptor
    # rethrows it. With a pipeline, governing contracts come from it (pass
    # contract_id=None); on the fallback path keep the legacy fallback contract.
    async def _render():
        governing = _run_contract_for_render(contract_id, pipeline_id,
                                             eff_contract_id)
        result = await _render_landing(
            landing_id, governing,
            filename, actor or mga, {}, auto_ingest=not check_only,
            pipeline_id=pipeline_id, check_only=check_only, scope=run_scope)
        result["format_drift"] = drift
        if supp_stats is not None:
            result["supplement"] = supp_stats
        return result

    return heartbeat_stream_response(_render())


# ---- DATA LANE (admin) -----------------------------------------------------

def _ensure_admin_task(s, tenant_id, format_id, fingerprint, landing_id, actor) -> int:
    """One open task per (tenant, format). Append the landing id so it gets
    backfilled into the data model once an admin approves the mapping."""
    task = (s.query(AdminMappingTask)
            .filter(AdminMappingTask.format_id == format_id,
                    AdminMappingTask.status.in_(("open", "in_progress")))
            .order_by(AdminMappingTask.id.desc())
            .first())
    if task is None:
        task = AdminMappingTask(
            tenant_id=tenant_id, format_id=format_id, fingerprint=fingerprint,
            status="open", title="Map new input format to the data model",
            detail={"fingerprint": fingerprint}, landing_record_ids=[landing_id],
            created_by=actor)
        s.add(task)
        s.flush()
        return task.id
    ids = list(task.landing_record_ids or [])
    if landing_id not in ids:
        ids.append(landing_id)
        task.landing_record_ids = ids
    return task.id


_OPEN_STATES = ("open", "in_progress")
_RESOLVED_STATES = ("done", "dismissed")


@router.get("/admin/mapping-tasks")
def admin_tasks_list(
    mga: Optional[str] = None,
    status: Optional[str] = None,
    tab: Optional[str] = None,
    q: Optional[str] = None,
    broker: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=200),
    _p: Principal = Depends(require_role("kavachio_admin")),
):
    """Cross-tenant data-mapping queue, TRUE server-side paginated. Search (`q`
    over format name / title / fingerprint), `tab` (open|resolved), and date
    range are applied in SQL; only one page of rows is returned.

    Returns {"items", "total", "page", "page_size", "open_count",
    "resolved_count"} — the two counts honour `q`/date so the tab labels track
    the current search, and drive the Open/Resolved tab badges."""
    with SessionLocal() as s:
        # Left-join DirectFormat so the search can match the resolved format name
        # (not just the task's own title/fingerprint).
        base = (s.query(AdminMappingTask)
                .outerjoin(DirectFormat, DirectFormat.id == AdminMappingTask.format_id))

        # Cross-tenant Kavachio ops queue: only filter by tenant when a REAL mga
        # is supplied. Sentinel/placeholder values (e.g. the frontend's "default"
        # fallback for a platform admin with no tenant) mean "all tenants" — this
        # is what previously left the queue empty for platform admins.
        scope_tid = None
        if mga and mga.lower() not in ("default", "null", "undefined", "all", ""):
            scope_tid = _tenant_id(s, mga)
            base = base.filter(AdminMappingTask.tenant_id == scope_tid)

        # Options for the Broker dropdown: every tenant that owns a task in
        # scope. Deliberately NOT narrowed by q/broker/date — otherwise picking
        # a broker would collapse the menu to that one entry and you could
        # never switch back without clearing the filter.
        bq = (s.query(AdminMappingTask.tenant_id, Tenant.legal_name, Tenant.tenant_name)
              .join(Tenant, Tenant.id == AdminMappingTask.tenant_id)
              .distinct())
        if scope_tid is not None:
            bq = bq.filter(AdminMappingTask.tenant_id == scope_tid)
        brokers = sorted(
            [{"id": tid, "name": (legal or (tname or "").title() or f"Tenant #{tid}")}
             for tid, legal, tname in bq.all()],
            key=lambda b: b["name"].lower())

        if broker and broker.strip().isdigit():
            base = base.filter(AdminMappingTask.tenant_id == int(broker))
        if q and q.strip():
            like = f"%{q.strip().lower()}%"
            base = base.filter(or_(
                func.lower(func.coalesce(DirectFormat.name, "")).like(like),
                func.lower(func.coalesce(AdminMappingTask.title, "")).like(like),
                func.lower(func.coalesce(AdminMappingTask.fingerprint, "")).like(like)))
        df, dt_ = _parse_client_dt(date_from), _parse_client_dt(date_to)
        if df:
            base = base.filter(AdminMappingTask.created_at >= df)
        if dt_:
            base = base.filter(AdminMappingTask.created_at <= dt_)

        # Tab/status counts share the search+broker+date filters above.
        open_count = base.filter(AdminMappingTask.status.in_(_OPEN_STATES)).count()
        resolved_count = base.filter(AdminMappingTask.status.in_(_RESOLVED_STATES)).count()

        q_page = base
        if status:
            q_page = q_page.filter(AdminMappingTask.status == status)
        elif tab == "resolved":
            q_page = q_page.filter(AdminMappingTask.status.in_(_RESOLVED_STATES))
        elif tab == "open":
            q_page = q_page.filter(AdminMappingTask.status.in_(_OPEN_STATES))

        total = q_page.order_by(None).count()
        rows = (q_page.order_by(AdminMappingTask.id.desc())
                .offset((page - 1) * page_size).limit(page_size).all())

        fmt_ids = {t.format_id for t in rows if t.format_id}
        fmt_names = ({f.id: f.name for f in
                     s.query(DirectFormat).filter(DirectFormat.id.in_(fmt_ids))}
                    if fmt_ids else {})
        ten_ids = {t.tenant_id for t in rows if t.tenant_id}
        ten_names = ({t.id: (t.legal_name or (t.tenant_name or "").title())
                      for t in s.query(Tenant).filter(Tenant.id.in_(ten_ids))}
                     if ten_ids else {})
        items = []
        for t in rows:
            detail = t.detail if isinstance(t.detail, dict) else {}
            items.append({
                "id": t.id, "tenant_id": t.tenant_id, "format_id": t.format_id,
                "format_name": fmt_names.get(t.format_id),
                "tenant_name": ten_names.get(t.tenant_id, "—"),
                "fingerprint": t.fingerprint, "status": t.status, "title": t.title,
                "detail": t.detail,
                "proposed_mapper_id": detail.get("proposed_mapper_id"),
                "landing_record_ids": t.landing_record_ids or [],
                "created_by": t.created_by, "resolved_by": t.resolved_by,
                "created_at": _iso_utc(t.created_at),
            })
        return {"items": items, "total": int(total),
                "page": page, "page_size": page_size,
                "open_count": int(open_count), "resolved_count": int(resolved_count),
                "brokers": brokers}


@router.post("/admin/mapping-tasks/{task_id}/propose")
async def admin_task_propose(task_id: int,
                             _p: Principal = Depends(require_role("kavachio_admin"))):
    """AI-map the format's input fields → the 850-field data model (with confidence
    scoring), persist it as a Mapper, link it to the task, and return its id — so
    the admin reviews/edits it in the standard mapper UI before approving. Same
    engine and scoring as the normal upload→mapper flow.

    The Gemini call runs for minutes on an uncached format, and a request that
    sends nothing for that long is severed by the ingress idle timeout — the
    browser then reports a bare "Network Error" even though the mapping was
    created server-side. So the slow half streams under a heartbeat, exactly
    like /direct/upload. Everything that must surface as a real 4xx is checked
    BEFORE the stream opens (once it starts the status is locked at 200)."""
    with SessionLocal() as s:
        task = s.get(AdminMappingTask, task_id)
        if not task:
            raise HTTPException(404, "task not found")
        # Already proposed — hand the same mapper back instead of running the AI
        # again. Without this, a client that gave up on a slow first call (or a
        # double click) creates a second Mapper and orphans the first, since the
        # task can only point at one.
        detail0 = task.detail if isinstance(task.detail, dict) else {}
        prior = detail0.get("proposed_mapper_id")
        if prior and s.get(Mapper, prior) is not None:
            return {"task_id": task_id, "mapper_id": prior,
                    "reused": True, "stats": None}
        # Learn from a landing sample: prefer the task's pending landings, else the
        # newest landing captured for this format.
        rec = None
        for lid in (task.landing_record_ids or []):
            r = s.get(LandingRecord, lid)
            if r and r.data:
                rec = r
                break
        if rec is None and task.format_id:
            rec = (s.query(LandingRecord)
                   .filter(LandingRecord.format_id == task.format_id)
                   .order_by(LandingRecord.id.desc()).first())
        if rec is None or not rec.data:
            raise HTTPException(400, "no landing sample to learn the mapping from")
        tenant_id = task.tenant_id
        fmt = s.get(DirectFormat, task.format_id) if task.format_id else None
        fmt_id = fmt.id if fmt else None
        fmt_name = (fmt.name if fmt and fmt.name else None) or f"Format #{task.format_id}"
        # Capture the tenant Setup's input→output column map (set on Bordereau
        # Setup, saved as DirectFormat.column_mapping) while the session is open
        # (fmt detaches after the block closes).
        column_mapping = (fmt.column_mapping if fmt else None) or {}
        source_filename = rec.source_filename
        landing_data = rec.data

    # Reconstruct DataFrames from the faithful landing JSON. Off the event loop
    # (pandas blocks) but still ahead of the stream, so an unreadable sample is
    # reported as a real 400 rather than buried in a 200 body.
    def _frames() -> dict[str, "pd.DataFrame"]:
        out: dict[str, "pd.DataFrame"] = {}
        for name, sheet in (landing_data.get("sheets") or {}).items():
            cols = sheet.get("columns") or []
            rows = sheet.get("rows") or []
            out[name] = (pd.DataFrame(rows, columns=cols) if rows
                         else pd.DataFrame(columns=cols))
        return out

    sheets_dict = await run_in_threadpool(_frames)
    if not sheets_dict:
        raise HTTPException(400, "landing sample has no readable sheets")

    def _propose() -> dict:
        # Same AI mapping engine (+ scoring) as the normal upload flow — and the
        # minutes-long step the heartbeat above exists to cover.
        result = generate_mapping_multi(sheets_dict)
        sig = signature_multi(sheets_dict)

        # Tenant "output column" per input column. The Bordereau Setup renames raw
        # input columns to the tenant's output fields via `copy` rules, stored as
        # column_mapping = {output_sheet: {output_field: {kind:"copy", source:<input_col>}}}.
        # Invert those (input col → output field), then re-qualify with the landing
        # sheet so keys line up with the mapper's "Sheet :: Column" source keys.
        # Only `copy` rules map 1:1; const/transform have no single source → skipped.
        out_by_input: dict[str, str] = {}
        for col_rules in (column_mapping or {}).values():
            if not isinstance(col_rules, dict):
                continue
            for out_field, rule in col_rules.items():
                if isinstance(rule, dict) and rule.get("kind") == "copy" and rule.get("source"):
                    out_by_input.setdefault(str(rule["source"]), out_field)
        output_by_source: dict[str, str] = {}
        if out_by_input:
            for sheet_name, df in sheets_dict.items():
                for col in df.columns:
                    bare = str(col)
                    if bare in out_by_input:
                        output_by_source[qualify(str(sheet_name), bare)] = out_by_input[bare]

        with SessionLocal() as s:
            m = Mapper(
                tenant_id=tenant_id, name=f"{fmt_name} → data model",
                version=1, is_active=0, approved=0, signature=sig,
                spec=result.get("spec") or {},
                spec_by_sheet=result.get("spec_by_sheet") or {},
                candidates=result.get("candidates_by_source") or {},
                samples=result.get("samples") or {},
                output_by_source=output_by_source,
                source_filename=source_filename,
                selected_sheets=list(sheets_dict.keys()))
            s.add(m)
            s.flush()
            mapper_id = m.id
            task = s.get(AdminMappingTask, task_id)
            detail = dict(task.detail) if isinstance(task.detail, dict) else {}
            detail["proposed_mapper_id"] = mapper_id
            task.detail = detail
            task.status = "in_progress"
            s.commit()

        try:
            from audit import log_activity, actor_email
            log_activity(tenant_id, None, "datamodel_mapping_proposed",
                         target=f"task:{task_id}",
                         details={"task_id": task_id, "mapper_id": mapper_id,
                                  "format_id": fmt_id,
                                  "stats": {
                                      "successful": len(result.get("successful") or []),
                                      "likely": len(result.get("likely") or []),
                                      "unsuccessful": len(result.get("unsuccessful") or []),
                                  }})
        except Exception:  # noqa: BLE001
            pass

        return {
            "task_id": task_id, "mapper_id": mapper_id,
            "stats": {
                "successful": len(result.get("successful") or []),
                "likely": len(result.get("likely") or []),
                "unsuccessful": len(result.get("unsuccessful") or []),
            },
        }

    return heartbeat_stream_response(run_in_threadpool(_propose))


class TaskResolveBody(BaseModel):
    # The admin's input→canonical Mapper (mappers.id) for this format. Its
    # spec_by_sheet is used to backfill the pending landing records.
    mapper_id: Optional[int] = None
    action: str = "approve"          # approve | dismiss
    resolved_by: Optional[str] = None


@router.post("/admin/mapping-tasks/{task_id}/resolve")
def admin_task_resolve(task_id: int, body: TaskResolveBody,
                       background_tasks: BackgroundTasks,
                       _p: Principal = Depends(require_role("kavachio_admin"))):
    """Approve: mark the format data-model-mapped and SCHEDULE the backfill of
    every pending landing record into the canonical warehouse — the row-loading
    runs in the BACKGROUND so the caller returns immediately. Dismiss: just
    close the task."""
    with SessionLocal() as s:
        task = s.get(AdminMappingTask, task_id)
        if not task:
            raise HTTPException(404, "task not found")
        if body.action == "dismiss":
            task.status = "dismissed"
            task.resolved_by = body.resolved_by
            task.resolved_at = datetime.utcnow()
            s.commit()
            try:
                from audit import log_activity, actor_email
                log_activity(task.tenant_id, body.resolved_by, "mapping_task_resolved",
                             target=f"task:{task_id}",
                             details={"task_id": task_id, "action": body.action,
                                      "mapper_id": body.mapper_id, "queued": 0})
            except Exception:  # noqa: BLE001
                pass
            return {"id": task.id, "status": task.status, "queued": 0}

        if body.mapper_id is None:
            raise HTTPException(400, "mapper_id required to approve")
        mapper = s.get(Mapper, body.mapper_id)
        if not mapper or not mapper.spec_by_sheet:
            raise HTTPException(400, "mapper not found or has no spec_by_sheet")

        # Flag the format data-model-mapped so BOTH this backfill and all future
        # uploads of this format auto-ingest into the warehouse.
        fmt = s.get(DirectFormat, task.format_id) if task.format_id else None
        if fmt:
            fmt.datamodel_mapped = True
            fmt.datamodel_mapper_id = body.mapper_id
            fmt.modified_at = datetime.utcnow()

        landing_ids = list(task.landing_record_ids or [])
        # Mark resolved now; the actual row-loading happens in the background
        # below so the admin isn't blocked on ingestion.
        task.status = "done"
        task.resolved_by = body.resolved_by
        task.resolved_at = datetime.utcnow()
        s.commit()

    # Load each pending file into the warehouse OFF the response path.
    # _ingest_landing_background is idempotent (skips already-loaded landings)
    # and self-contained (re-reads the approved mapper from the format), so it
    # runs safely after the response is returned.
    for lid in landing_ids:
        background_tasks.add_task(_ingest_landing_background, lid, body.resolved_by)

    try:
        from audit import log_activity, actor_email
        log_activity(None, body.resolved_by, "mapping_task_resolved",
                     target=f"task:{task_id}",
                     details={"task_id": task_id, "action": body.action,
                              "mapper_id": body.mapper_id,
                              "queued": len(landing_ids)})
    except Exception:  # noqa: BLE001
        pass

    return {"id": task_id, "status": "done", "queued": len(landing_ids)}


def _ingest_landing_background(landing_id: int, actor: Optional[str] = None) -> None:
    """Fire-and-forget: load the approved data-model mapper for this landing's
    format and backfill the landing into the canonical warehouse. Runs off the
    delivery path so the user never waits on data-model loading. Safe to call
    repeatedly — _backfill_landing is idempotent (skips already-loaded landings).

    `actor` (the user who triggered the load) is recorded on the ingest-outcome
    audit event, since this runs after the response so the request middleware
    can't capture the result."""
    try:
        with SessionLocal() as s:
            rec = s.get(LandingRecord, landing_id)
            if not rec or rec.datamodel_status == "loaded":
                return
            fmt = s.get(DirectFormat, rec.format_id) if rec.format_id else None
            if not fmt or not fmt.datamodel_mapped or not fmt.datamodel_mapper_id:
                return
            mapper = s.get(Mapper, fmt.datamodel_mapper_id)
            if not mapper or not mapper.spec_by_sheet:
                log.warning("auto-ingest: format %s mapped but mapper %s has no spec",
                            fmt.id, fmt.datamodel_mapper_id)
                return
            spec_by_sheet = mapper.spec_by_sheet
            mga = _tenant_name(s, rec.tenant_id)
        loaded = _backfill_landing(landing_id, spec_by_sheet, mga, actor)
        log.info("auto-ingested landing %s into data model (%s record(s))",
                 landing_id, loaded)
    except Exception as e:  # noqa: BLE001 — background work must never crash
        log.warning("auto data-model ingest failed for landing %s: %s", landing_id, e)


def _backfill_landing(landing_id: int, spec_by_sheet: dict, mga: Optional[str],
                      actor: Optional[str] = None) -> int:
    """Reconstruct DataFrames from a landing record and load them into the
    canonical warehouse using the admin's input→canonical mapper spec."""
    with SessionLocal() as s:
        rec = s.get(LandingRecord, landing_id)
        if not rec or rec.datamodel_status == "loaded":
            return 0
        data = rec.data or {}
        tenant_mga = mga or _tenant_name(s, rec.tenant_id)
        rec_tenant_id = rec.tenant_id

    sheets_dict: dict[str, pd.DataFrame] = {}
    for name, sheet in (data.get("sheets") or {}).items():
        cols = sheet.get("columns") or []
        rows = sheet.get("rows") or []
        sheets_dict[name] = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)

    per_sheet = apply_spec_multi(sheets_dict, spec_by_sheet)
    records = [r for recs in per_sheet.values() for r in recs]
    if not records:
        with SessionLocal() as s:
            r = s.get(LandingRecord, landing_id)
            r.datamodel_status = "loaded"
            s.commit()
        return 0

    loaded = 0
    with CanonicalSession() as cs:
        # Each record ingests inside its own SAVEPOINT so a single failing INSERT
        # (a duplicate invoice, a bad value, a constraint hit) rolls back only that
        # record instead of aborting the whole Postgres transaction — which
        # otherwise makes every following record fail with InFailedSqlTransaction.
        # Good rows still commit.
        for record in records:
            try:
                with cs.begin_nested():
                    ingest_record(cs, tenant_mga, record)
                loaded += 1
            except Exception as e:  # noqa: BLE001
                log.warning("ingest_record failed during backfill: %s", e)
        cs.commit()

    with SessionLocal() as s:
        r = s.get(LandingRecord, landing_id)
        # Only mark loaded when at least one record actually persisted; a fully
        # failed load stays 'pending' so it can be retried, rather than being
        # silently stuck as 'loaded' with zero rows.
        if loaded > 0:
            r.datamodel_status = "loaded"
        s.commit()

    # Ingest OUTCOME audit (the trigger request was already logged by the
    # middleware; this records the result the background load produced).
    try:
        from audit import log_activity
        log_activity(rec_tenant_id, actor, "datamodel.ingest",
                     target=f"landing:{landing_id}",
                     details={"loaded": loaded, "failed": len(records) - loaded,
                              "total": len(records)})
    except Exception:  # noqa: BLE001 — auditing must never break ingestion
        pass
    return loaded


@router.get("/pipelines/{pipeline_id}/bordereau-template")
def pipeline_bordereau_template(pipeline_id: int,
                                principal: Principal = Depends(current_principal)):
    """The blank bordereau this setup reads — its Input Template's sheets and
    column headings, nothing under them. What to fill in before Process
    Bordereau; bordereau_template says why it is the input layout, not the
    output template."""
    from fastapi import Response
    import bordereau_template as bt
    with SessionLocal() as s:
        p = s.get(Pipeline, pipeline_id)
        if not p:
            raise HTTPException(404, "setup not found")
        assert_tenant_owns(principal, p.tenant_id)
        try:
            data, name = bt.setup_input_template(s, p)
        except bt.TemplateUnavailable as e:
            raise HTTPException(404, str(e))
    return Response(content=data, media_type=bt.XLSX, headers=bt.attachment(name))
