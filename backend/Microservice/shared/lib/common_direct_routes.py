"""Shared helpers extracted from the monolith's direct_routes.py (endpoints removed)."""
from __future__ import annotations

__all__ = [
    'APIRouter',
    'ActivityEvent',
    'AdminMappingTask',
    'Any',
    'BackgroundTasks',
    'BaseModel',
    'CanonicalSession',
    'Contract',
    'Depends',
    'DirectFormat',
    'DirectFormatUpdate',
    'ExportTemplate',
    'File',
    'Form',
    'HTTPException',
    'LandingRecord',
    'Mapper',
    'Optional',
    'OutputExport',
    'Party',
    'Principal',
    'Program',
    'RerenderRequest',
    'SessionLocal',
    'TaskResolveBody',
    'UploadFile',
    '_DATE_TYPES',
    '_NUM_TYPES',
    '_apply_landing_corrections',
    '_apply_output_corrections',
    '_attach_clauses',
    '_backfill_landing',
    '_coerce_like',
    '_cols_and_samples',
    '_column_types_from_structure',
    '_contract_clauses_by_field',
    '_contract_constants',
    '_ensure_admin_task',
    '_ensure_tenant',
    '_format_to_dict',
    '_ingest_landing_background',
    '_iso_utc',
    '_load_output_corrections',
    '_load_structure',
    '_output_sheet_names',
    '_render_landing',
    '_routing_input_sheets',
    '_supplement_summary',
    '_tenant_id',
    '_tenant_name',
    'annotations',
    'apply_spec_multi',
    'assert_tenant_owns',
    'copy',
    'current_principal',
    'datetime',
    'dl',
    'dm',
    'dr',
    'ingest_record',
    'is_reference_sheet',
    'json',
    'log',
    'logging',
    'parse_template',
    'pd',
    'qualify',
    're',
    'read_excel_all_sheets',
    'require_role',
    'resolve_tenant_id',
    'run_in_threadpool',
    'signature_hash',
    'signature_multi',
    'spec_sheet_names',
    'text',
    'threading',
]

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

import copy

import json

import logging

import re

import threading

from datetime import datetime

from typing import Any, Optional

import pandas as pd

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile

from fastapi.concurrency import run_in_threadpool

from pydantic import BaseModel

from sqlalchemy import text

import storage  # blob storage abstraction (Azure/Azurite; DB-blob fallback)
import direct_lane as dl

import direct_mapper as dm

import direct_render as dr

from common_app_routes import _iso_utc

from db import (
    ActivityEvent, AdminMappingTask, CanonicalSession, Contract, DirectFormat,
    ExportTemplate, LandingRecord, Mapper, OutputExport, Party, Program,
    SessionLocal,
)

from exporter import parse_template, spec_sheet_names, is_reference_sheet

from fingerprint import signature_hash

from ingester import _ensure_tenant, ingest_record

from auth_deps import current_principal, require_role, Principal

from mapping_utils import (
    apply_spec_multi, qualify, read_excel_all_sheets,
    signature_multi,
)

log = logging.getLogger("bdx.direct")

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

def _load_output_corrections(s, landing_id: int) -> list:
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

def _apply_output_corrections(projected: dict, corrections: list) -> dict:
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
    row = s.execute(text("SELECT tenant_id FROM tenant WHERE tenant_name=:m LIMIT 1"),
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
    row = s.execute(text("SELECT tenant_name FROM tenant WHERE tenant_id=:t LIMIT 1"),
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
    STRIPPED sheet name to match the DuckDB table names."""
    try:
        from data_model import DATA_MODEL
    except Exception:
        DATA_MODEL = {}
    out: dict = {}
    for sh in (structure or {}).get("sheets", []) or []:
        sheet = str(sh.get("sheet_name", "")).strip()
        if not sheet:
            continue
        kinds: dict = {}
        for c in (sh.get("columns") or []):
            name = c.get("column_name")
            if not name:
                continue
            fmt = str(c.get("field_format") or "")
            cf = c.get("canonical_field")
            t = (DATA_MODEL.get(cf) or {}).get("type") if cf else None
            kind = "date" if t in _DATE_TYPES else "number" if t in _NUM_TYPES else None
            if kind is None:  # fall back to the template's declared format
                kind = _fmt_type_kind(fmt)
            # Whichever source typed it, a column whose sample values are bare
            # years is never date-checked: "2023" is a valid treaty year but not
            # a parsable date, so the check would flag every row.
            if kind == "date" and _samples_are_years(c.get("samples")):
                kind = None
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

async def _render_landing(
    landing_id: int, contract_id: Optional[int], filename: Optional[str],
    actor: Optional[str], extra_consts: dict, auto_ingest: bool = False,
    reuse_export_id: Optional[int] = None,
) -> dict:
    """Shared core: project a landing record into the output BDX, validate it
    against the contract rules, persist the downloadable file, and either raise a
    one-time admin task (format not mapped yet) or — once the format IS mapped —
    auto-ingest the data into the data model in the background. Used by both
    /direct/render (setup preview) and /direct/run (ops data upload).

    ``reuse_export_id`` re-renders IN PLACE (updates that OutputExport row so the
    id/header stays stable across Re-generate); omit it to create a new export."""
    with SessionLocal() as s:
        rec = s.get(LandingRecord, landing_id)
        if not rec:
            raise HTTPException(404, "landing record not found")
        fmt = s.get(DirectFormat, rec.format_id) if rec.format_id else None
        if not fmt or not fmt.column_mapping:
            raise HTTPException(400, "format has no confirmed column mapping yet")
        tpl = s.get(ExportTemplate, fmt.output_template_id)
        if not tpl:
            raise HTTPException(404, "output template not found")
        structure = _load_structure(tpl)
        template_blob = storage.resolve_bytes(tpl.template_blob_ref, tpl.template_blob)
        template_name = tpl.name
        tenant_id = rec.tenant_id
        eff_contract_id = contract_id or fmt.contract_id
        # Per-schedule contracts: each output sheet can have its OWN contract.
        # Governing set = the per-sheet contracts ∪ the fallback contract. Every
        # governing contract's rules are validated (each only fires on the sheets
        # its compiled SQL references), and their constants are merged.
        sheet_contracts = fmt.sheet_contracts or {}
        governing_ids = []
        for cid in ([int(v) for v in sheet_contracts.values() if v]
                    + ([eff_contract_id] if eff_contract_id else [])):
            if cid not in governing_ids:
                governing_ids.append(cid)
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
        _fname = {c.id: c.filename for c in
                  (s.query(Contract).filter(Contract.id.in_(governing_ids)).all()
                   if governing_ids else [])}
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
        out_template_id = fmt.output_template_id

    # Pure projection (offload heavy work from the event loop).
    routed = dl.apply_routing(landing_data, routing)
    projected = dl.project_to_output(routed, column_mapping, constants_dict)
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
            from clients import validation as _dvc
            dv = _dvc.run_validation(blocks, rules, template_id=fmt.output_template_id,
                                schema_cols=schema_cols, column_types=column_types)
            exceptions = dv.get("exceptions", [])
            # Label each row-level exception with the offending policy's number so
            # the review UI shows a real policy id instead of "Dataset-level".
            exceptions = _dvc.label_exceptions(exceptions, structure, blocks)
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

    xlsx = await run_in_threadpool(dr.render_output, structure, projected, template_blob)
    if exceptions:
        try:
            from exporter import highlight_exceptions
            xlsx = highlight_exceptions(xlsx, structure, exceptions)
        except Exception as e:  # noqa: BLE001
            log.warning("highlight skipped: %s", e)

    raw = filename or f"{(template_name or 'export').replace(' ', '_')}"
    fname = raw if raw.lower().endswith((".xlsx", ".xls", ".csv")) else raw + ".xlsx"

    # Persist the generated workbook to blob storage (Azure/Azurite) when enabled;
    # otherwise keep the bytes inline in `blob` (legacy behaviour).
    exp_ref, exp_bytes = await run_in_threadpool(
        storage.store_or_keep, "exports", tenant_id, fname, xlsx)

    with SessionLocal() as s:
        out = s.get(OutputExport, reuse_export_id) if reuse_export_id else None
        if out is None:
            out = OutputExport(
                tenant_id=tenant_id, template_id=out_template_id,
                template_name=template_name, filename=fname,
                source_upload_id=None, policy_ids=None, generated_by=actor,
                policy_count=sum(len(v) for v in projected.values()),
                exception_count=len(exceptions), exceptions=exceptions,
                status="has_exceptions" if exceptions else "clean",
                blob=exp_bytes, blob_ref=exp_ref)
            s.add(out)
        else:
            # Re-render in place — same export id/header, refreshed file + exceptions.
            out.template_name = template_name
            out.filename = fname
            out.policy_count = sum(len(v) for v in projected.values())
            out.exception_count = len(exceptions)
            out.exceptions = exceptions
            out.status = "has_exceptions" if exceptions else "clean"
            out.blob = exp_bytes
            out.blob_ref = exp_ref
            out.generated_by = actor or out.generated_by
        s.add(ActivityEvent(
            tenant_id=tenant_id, actor=actor, action="direct_output_generated",
            target=f"direct:{template_name}",
            details={"filename": fname, "rows": out.policy_count,
                     "exceptions": len(exceptions)}))
        s.commit()
        s.refresh(out)
        export_id = out.id

        # Link the uploaded file (landing) to the output it produced, so the run
        # history can show "uploaded X → generated Y (N exceptions)".
        lr = s.get(LandingRecord, landing_id)
        if lr is not None:
            lr.output_export_id = export_id
            s.commit()
            # Group 3: producing a BDX for a program satisfies that program's next
            # outstanding deadline → mark the period received. Best-effort: the
            # calendar must never break delivery.
            try:
                fmt = s.get(DirectFormat, lr.format_id) if lr.format_id else None
                if fmt is not None and fmt.program_id is not None:
                    from submission_calendar_service import mark_received
                    if mark_received(s, fmt.program_id, export_id=export_id) is not None:
                        s.commit()
            except Exception as _e:  # noqa: BLE001
                log.warning("submission-calendar mark_received failed: %s", _e)
                s.rollback()

        # DATA LANE trigger: if this format isn't mapped to the data model yet,
        # raise (or append to) a one-time admin task. Off the delivery path.
        admin_task_id = None
        if not datamodel_mapped:
            admin_task_id = _ensure_admin_task(s, tenant_id, format_id,
                                               landing_fp, landing_id, actor)
        s.commit()

    # Once the format IS data-model-mapped, every real run flows straight into the
    # data model — in a background thread so delivery never waits on it.
    datamodel_queued = False
    if auto_ingest and datamodel_mapped:
        threading.Thread(target=_ingest_landing_background,
                         args=(landing_id,), daemon=True).start()
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
    }

class RerenderRequest(BaseModel):
    actor: Optional[str] = None

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
        # Everything else the rule names — a weaker, substring ("related") link.
        blob = json.dumps([r.get("rule_spec"), r.get("error_message"),
                           r.get("rule_name")], default=str).lower()
        clause_text = (r.get("source_verbatim_text") or r.get("error_message")
                       or r.get("rule_name"))
        for fld in field_names:
            if not fld:
                continue
            fl = fld.strip().lower()
            if fl in targeted:
                match, score = "exact", 1.0
            elif fl in blob:
                match, score = "related", 0.6
            else:
                continue
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

class TaskResolveBody(BaseModel):
    # The admin's input→canonical Mapper (mappers.id) for this format. Its
    # spec_by_sheet is used to backfill the pending landing records.
    mapper_id: Optional[int] = None
    action: str = "approve"          # approve | dismiss
    resolved_by: Optional[str] = None

def _ingest_landing_background(landing_id: int) -> None:
    """Fire-and-forget: load the approved data-model mapper for this landing's
    format and backfill the landing into the canonical warehouse. Runs off the
    delivery path so the user never waits on data-model loading. Safe to call
    repeatedly — _backfill_landing is idempotent (skips already-loaded landings)."""
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
        loaded = _backfill_landing(landing_id, spec_by_sheet, mga)
        log.info("auto-ingested landing %s into data model (%s record(s))",
                 landing_id, loaded)
    except Exception as e:  # noqa: BLE001 — background work must never crash
        log.warning("auto data-model ingest failed for landing %s: %s", landing_id, e)

def _backfill_landing(landing_id: int, spec_by_sheet: dict, mga: Optional[str]) -> int:
    """Reconstruct DataFrames from a landing record and load them into the
    canonical warehouse using the admin's input→canonical mapper spec."""
    with SessionLocal() as s:
        rec = s.get(LandingRecord, landing_id)
        if not rec or rec.datamodel_status == "loaded":
            return 0
        data = rec.data or {}
        tenant_mga = mga or _tenant_name(s, rec.tenant_id)

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
    return loaded
