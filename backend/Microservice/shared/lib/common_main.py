"""Shared helpers extracted from the monolith's main.py (endpoints removed)."""
from __future__ import annotations

__all__ = [
    'ActivityEvent',
    'Any',
    'BDXRecord',
    'BaseModel',
    'CORSMiddleware',
    'CanonicalSession',
    'Contract',
    'DATA_MODEL',
    'Depends',
    'ExportTemplate',
    'FastAPI',
    'File',
    'Form',
    'HTTPException',
    'JSONResponse',
    'Mapper',
    'Optional',
    'OutputExport',
    'Party',
    'Path',
    'Principal',
    'Program',
    'Response',
    'SessionLocal',
    'SheetBinding',
    'UpdateExportTemplateBody',
    'UpdateMapperBody',
    'Upload',
    'UploadFile',
    'UploadPolicy',
    'UploadSheetContract',
    '_KIND_STATUS',
    '_SCALAR_TABLES',
    '_activate_mapper',
    '_activate_template',
    '_active_contract_id_for_template',
    '_attach_decisions',
    '_attach_direct_lane_decisions',
    '_attach_recommendations',
    '_carrier_for_template',
    '_check_field_constraints',
    '_content_disposition',
    '_contract_id_for_template',
    '_cors_origins',
    '_evaluate_ajv_rule',
    '_expected_from_ajv_schema',
    '_expected_from_ir',
    '_export_to_dict',
    '_extract_template_fields',
    '_field_of',
    '_filter_sheets',
    '_find_mapper',
    '_get_tenant_id',
    '_group_versions',
    '_ir_template',
    '_iso_utc',
    '_mapper_rows_no_blob',
    '_mapper_siblings',
    '_mapper_to_dict',
    '_merge_into_collection',
    '_merge_records',
    '_merge_scalar',
    '_options_from_ir',
    '_os',
    '_resolve_export_target',
    '_resolve_spec_by_sheet',
    '_sheet_bindings_for_mapper',
    '_template_to_dict',
    '_tenant_name',
    '_to_number',
    '_try_clone_existing_mapper',
    '_upload_to_dict',
    '_validate_records',
    '_xlsx_to_grid',
    'annotations',
    'apply_spec_multi',
    'assert_tenant_owns',
    'case',
    'current_principal',
    'fetch_policies',
    'init_db',
    'load_dotenv',
    'log',
    'logging',
    'read_excel_all_sheets',
    'resolve_export_policy_numbers',
    'resolve_tenant_id',
    'run_in_threadpool',
    'select',
    'signature_multi',
    'text',
]

"""FastAPI app exposing the BDX onboarding + ingestion flow."""

import logging

from typing import Any, Optional

from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("bdx.main")

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile

from fastapi.concurrency import run_in_threadpool

from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import Response, JSONResponse

from pydantic import BaseModel

from sqlalchemy import case, select, text

from data_model import DATA_MODEL

from assembler import fetch_policies

from db import (
    ActivityEvent, BDXRecord, CanonicalSession, Contract, ExportTemplate, Mapper,Program,
    OutputExport, SessionLocal, Upload, UploadPolicy, init_db, Party,
    SheetBinding, UploadSheetContract,
)


from common_app_routes import resolve_tenant_id, assert_tenant_owns, _iso_utc

from auth_deps import current_principal, Principal




from mapping_utils import (
    apply_spec_multi,
    read_excel_all_sheets,
    signature_multi,
)

import os as _os

_cors_origins = [o.strip() for o in _os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://192.168.2.21:5173",
).split(",") if o.strip()]

def _content_disposition(filename: Optional[str], default: str = "download.xlsx") -> str:
    """Build an RFC 5987-safe Content-Disposition value. HTTP headers must be
    latin-1 encodable, so a filename with non-ASCII characters (e.g. an em dash
    from a setup name) would otherwise crash the response with a 500. We emit an
    ASCII fallback plus a UTF-8 `filename*` that modern browsers prefer."""
    import re
    from urllib.parse import quote
    name = filename or default
    ascii_fallback = re.sub(r"[^\x20-\x7e]", "_", name).replace('"', "'") or default
    return (f"attachment; filename=\"{ascii_fallback}\"; "
            f"filename*=UTF-8''{quote(name)}")

def _filter_sheets(sheets: dict, selected_csv: Optional[str]) -> dict:
    """If the caller supplied a comma-separated list of sheet names, keep
    only those (case-insensitive). Empty/None means 'all sheets'."""
    if not selected_csv:
        return sheets
    wanted = {s.strip().lower() for s in selected_csv.split(",") if s.strip()}
    if not wanted:
        return sheets
    kept = {name: df for name, df in sheets.items()
            if str(name).strip().lower() in wanted}
    if not kept:
        raise HTTPException(400, "none of the selected sheets were found")
    return kept

def _try_clone_existing_mapper(sig: list[str]) -> Optional[dict[str, Any]]:
    """Return a `result`-shaped dict cloned from an existing mapping.

    Lookup tiers:
      1. canonical `column_mapping_fingerprint` (cross-tenant) — primary.
      2. legacy `mappers` table — fallback for rows not yet linked to a
         fingerprint (pre-migration).

    Returns None when no match — caller falls back to the LLM.
    """
    # Tier 1 — canonical fingerprint (cross-tenant by SHA-256 of signature).
    try:
        from fingerprint import find_by_hash
        with CanonicalSession() as cs:
            hit = find_by_hash(cs, sig, cross_tenant=True)
            hit = False
        if hit:
            fp_id = hit["fingerprint_id"]
            spec_by_sheet = hit["canonical_mapping"] or {}

            # The canonical table stores the spec but NOT the per-source-
            # column ranked candidates. Pick those up from the most recent
            # `mappers` row already linked to this fingerprint — that's
            # what the UI uses for the confidence pills.
            sibling_candidates: dict = {}
            sibling_spec: dict = {}
            with SessionLocal() as s:
                sibling = (
                    s.query(Mapper)
                    .filter(Mapper.fingerprint_id == fp_id)
                    .order_by(Mapper.approved.desc(), Mapper.id.desc())
                    .first()
                )
                if sibling:
                    sibling_candidates = sibling.candidates or {}
                    sibling_spec = sibling.spec or {}

            log.info("Format cache hit on canonical fingerprint #%s "
                     "(hit_count=%d, candidates from sibling=%d)",
                     fp_id, hit["hit_count"], len(sibling_candidates))
            return {
                "spec": sibling_spec,
                "spec_by_sheet": spec_by_sheet,
                "candidates_by_source": sibling_candidates,
                "successful": [], "likely": [], "unsuccessful": [],
                "canonical_unmapped": [],
                "samples": {}, "sheets": [],
                "_cloned_from_fingerprint_id": fp_id,
            }
    except Exception as e:
        log.warning("canonical fingerprint lookup failed (%s); falling "
                    "back to legacy mappers table", e)

    # Tier 2 — legacy `mappers` table (rows without fingerprint_id).
    with SessionLocal() as s:
        candidates = (
            s.query(Mapper)
            .order_by(Mapper.approved.desc(), Mapper.id.desc())
            .all()
        )
        for m in candidates:
            if m.signature == sig:
                return {
                    "spec": m.spec or {},
                    "spec_by_sheet": m.spec_by_sheet or {},
                    "candidates_by_source": m.candidates or {},
                    "successful": [], "likely": [], "unsuccessful": [],
                    "canonical_unmapped": [],
                    "samples": {}, "sheets": [],
                    "_cloned_from_mapper_id": m.id,
                }
    return None

def _mapper_siblings(session, m: Mapper) -> list[Mapper]:
    rows = session.query(Mapper).filter(Mapper.tenant_id == m.tenant_id).all()
    if m.name:
        return [r for r in rows if r.name == m.name]
    return [r for r in rows if not r.name and r.signature == m.signature]

def _activate_mapper(session, m: Mapper) -> None:
    """Make `m` the active version; deactivate its siblings."""
    for sib in _mapper_siblings(session, m):
        sib.is_active = 1 if sib.id == m.id else 0

def _activate_template(session, t: ExportTemplate) -> None:
    sibs = session.query(ExportTemplate).filter(
        ExportTemplate.tenant_id == t.tenant_id, ExportTemplate.name == t.name).all()
    for sib in sibs:
        sib.is_active = 1 if sib.id == t.id else 0

def _group_versions(items: list[dict]) -> list[dict]:
    """Group serialized rows into templates by `name`, newest version first.
    Resolves the active version (explicit is_active, else approved-then-newest)."""
    groups: dict[str, list[dict]] = {}
    for it in items:
        key = it.get("name") or f"(unnamed) #{it['id']}"
        groups.setdefault(key, []).append(it)
    out = []
    for name, versions in groups.items():
        versions.sort(key=lambda x: x.get("version") or 1, reverse=True)
        active = next((v for v in versions if v.get("is_active")), None)
        if active is None:
            active = sorted(
                versions,
                key=lambda x: (bool(x.get("approved")), x.get("version") or 1),
            )[-1]
        out.append({"name": name, "active_id": active["id"], "versions": versions})
    out.sort(key=lambda g: g["name"].lower())
    return out

class UpdateMapperBody(BaseModel):
    # Either a flat spec, a per-sheet spec, or both. If only the flat spec is
    # provided we mirror it under sheet_by_sheet (the source string already
    # carries the sheet prefix).
    spec: Optional[dict[str, Any]] = None
    spec_by_sheet: Optional[dict[str, dict[str, Any]]] = None
    approved: Optional[bool] = None
    carrier: Optional[str] = None
    contract: Optional[str] = None

def _get_tenant_id(session, mga: str) -> Optional[int]:
    """Resolve an mga code (tenant_name) to its tenant_id, or None."""
    row = session.execute(
        text("SELECT tenant_id FROM tenant WHERE tenant_code=:m LIMIT 1"),
        {"m": mga}).fetchone()
    return row[0] if row else None

def _tenant_name(session, tenant_id: Optional[int]) -> Optional[str]:
    """Resolve a tenant_id back to its mga code (tenant_name) for API responses."""
    if not tenant_id:
        return None
    row = session.execute(
        text("SELECT tenant_code FROM tenant WHERE tenant_id=:t LIMIT 1"),
        {"t": tenant_id}).fetchone()
    return row[0] if row else None

def _mapper_to_dict(m, has_source_blob: bool = False, mga: Optional[str] = None) -> dict:
    return {
        "id": m.id, "mga": mga, "carrier": m.carrier, "contract": m.contract,
        "tenant_id": getattr(m, "tenant_id", None),
        "party_id": getattr(m, "party_id", None),
        "name": m.name, "version": m.version or 1, "is_active": bool(m.is_active),
        "approved": bool(m.approved), "signature": m.signature,
        "spec": m.spec, "spec_by_sheet": m.spec_by_sheet,
        "candidates": m.candidates or {},
        "samples": getattr(m, "samples", None) or {},
        "output_by_source": getattr(m, "output_by_source", None) or {},
        "source_filename": m.source_filename,
        "selected_sheets": m.selected_sheets,
        "has_source_blob": has_source_blob,
        "fingerprint_id": m.fingerprint_id,
        "created_at": _iso_utc(m.created_at),
    }

def _mapper_rows_no_blob(session, tenant_id: Optional[int] = None):
    """Query mapper rows excluding the blob column, with has_source_blob flag."""
    mt = Mapper.__table__
    blob_flag = case(
        (mt.c.source_blob.isnot(None) | mt.c.source_blob_ref.isnot(None), True),
        else_=False,
    ).label("has_source_blob")
    non_blob_cols = [c for c in mt.c if c.name != "source_blob"] + [blob_flag]
    q = select(*non_blob_cols)
    if tenant_id is not None:
        q = q.where(mt.c.tenant_id == tenant_id)
    rows = session.execute(q).mappings().all()
    # Re-hydrate as lightweight objects the existing _mapper_to_dict can read
    result = []
    for r in rows:
        class _M:
            pass
        obj = _M()
        for k, v in r.items():
            setattr(obj, k, v)
        setattr(obj, "id", r["id"])
        result.append((obj, bool(r["has_source_blob"])))
    return result

def _find_mapper(session, tenant_id: Optional[int], sig: list[str]) -> Optional[Mapper]:
    """Return the mapper for this MGA whose signature best matches.

    Match strategy (best first):
    1. Exact signature match → prefer active, then approved, then newest.
    2. Subset match: mapper's signature is a subset of the upload's signature
       (user uploaded extra sheets the mapper doesn't cover) → same priority.
    Subset matching lets the active version win even when the user uploads a
    file with extra/summary sheets that were excluded when the mapper was created.
    """
    sig_set = set(sig)
    all_mappers = session.query(Mapper).filter(
        Mapper.tenant_id == tenant_id).all()

    # Collect all mappers whose signature is exactly or a subset of the upload.
    # Subset match: mapper covers fewer sheets than the upload — the extra
    # sheets were excluded when the mapper was trained (e.g. Summary/Check).
    candidates = [
        (m, m.signature == sig)          # (mapper, is_exact)
        for m in all_mappers
        if m.signature and set(m.signature) <= sig_set
    ]
    if not candidates:
        return None

    # Sort: active first, then exact-match preferred, then approved, then newest.
    candidates.sort(
        key=lambda t: ((t[0].is_active or 0), int(t[1]), (t[0].approved or 0), t[0].id),
        reverse=True,
    )
    return candidates[0][0]

def _resolve_spec_by_sheet(m: Mapper) -> dict[str, dict[str, Any]]:
    """Use per-sheet spec when present; otherwise fall back to the flat spec
    by inferring the sheet from each source's 'Sheet :: Column' prefix."""
    if m.spec_by_sheet:
        return m.spec_by_sheet
    from mapping_utils import SHEET_SEP
    out: dict[str, dict[str, Any]] = {}
    for canonical, src in (m.spec or {}).items():
        srcs = src if isinstance(src, list) else [src]
        for s in srcs:
            if SHEET_SEP in s:
                sheet = s.split(SHEET_SEP, 1)[0]
                out.setdefault(sheet, {})[canonical] = src
    return out

def _upload_to_dict(u, has_blob: bool = False, mga: Optional[str] = None) -> dict:
    return {
        "id": u.id, "mga": mga, "tenant_id": getattr(u, "tenant_id", None),
        "mapper_id": u.mapper_id,
        "source_file": u.source_file, "sheets": u.sheets,
        "counts_by_sheet": u.counts_by_sheet, "total_rows": u.total_rows,
        "has_source_blob": has_blob,
        "ingested_at": _iso_utc(u.ingested_at),
    }

_SCALAR_TABLES = {
    "policy", "policyholder", "program", "contract", "tenant",
    # `extras` isn't a canonical table — it's a {entity: {key: value}} dict
    # produced by mapper.apply_spec_multi when `_xf:*` entries are in the
    # spec. Treat it as scalar so merging across sheets preserves the dict
    # shape (later non-null wins per key) instead of wrapping it in a list.
    "extras",
}

def _merge_scalar(into: dict, new: dict) -> dict:
    """Right-biased merge ignoring blanks."""
    out = dict(into) if into else {}
    for k, v in (new or {}).items():
        if v in (None, "", [], {}):
            continue
        out[k] = v
    return out

def _sheet_bindings_for_mapper(s, mapper_id) -> dict:
    """Return {sheet_name: {role, schedule_key, contract_id, output_template_id}}
    for a mapper's saved sheet bindings. Guarded: an un-migrated DB (no
    bdx_sheet_binding table) returns {} so the caller falls back to today's
    single-scope behaviour."""
    try:
        rows = s.query(SheetBinding).filter(SheetBinding.mapper_id == mapper_id).all()
    except Exception:
        return {}
    return {
        b.sheet_name: {
            "role": b.role,
            "schedule_key": b.schedule_key,
            "contract_id": b.contract_id,
            "output_template_id": b.output_template_id,
        }
        for b in rows
    }

def _merge_records(records: list[dict], scope_keys: list | None = None) -> list[dict]:
    """Group canonical rows by policy.policy_number and merge them.

    Scalar tables (policy, program, contract…) are right-biased merged.
    Every other table is treated as a collection: each row's table dict
    becomes one entry in an array, de-duplicated by exact content.
    Rows lacking a policy_number form their own bucket (keyed by claim_number
    if present; otherwise kept individually).

    Phase 2 — schedule-scoped merge: when `scope_keys` is given (one per record,
    the record's schedule), the merge key becomes (scope, policy_number) so two
    schedules that share a policy_number never collapse into one policy. Sheets
    (and supplement sheets) WITHIN the same schedule still merge by policy_number.
    With no scope_keys, behaviour is unchanged (single global scope).
    """
    buckets: dict = {}
    order: list = []  # preserve insertion order
    anon = 0

    for i, rec in enumerate(records):
        if not rec:
            continue
        pol = rec.get("policy") or {}
        clm = rec.get("claim") or {}
        polkey = pol.get("policy_number") or clm.get("claim_number")
        if not polkey:
            anon += 1
            polkey = f"__anon_{anon}"
        scope = scope_keys[i] if scope_keys is not None else None
        key = (scope, polkey)

        if key not in buckets:
            buckets[key] = {}
            order.append(key)
        target = buckets[key]

        for table, payload in rec.items():
            if not payload:
                continue
            if table in _SCALAR_TABLES:
                target[table] = _merge_scalar(target.get(table), payload)
            else:
                _merge_into_collection(target.setdefault(table, []), payload)
    return [buckets[k] for k in order]

def _merge_into_collection(arr: list[dict], payload: dict) -> None:
    """Add `payload` to a collection of sub-records.

    If any existing entry has no conflicting non-null values, merge into that
    entry (right-biased, non-blank wins). Otherwise append as a new entry.
    This collapses partial canonical payloads coming from different sheets
    (e.g. POL Data contributes `hazard_category` for coverage; PREM/UNT Data
    contributes the rest) into ONE row per logical entity, while keeping
    genuinely-different sub-records (multiple insured locations) separate.
    """
    for i, existing in enumerate(arr):
        if existing == payload:
            return  # exact dup
        conflicts = False
        for k in existing.keys() & payload.keys():
            a, b = existing.get(k), payload.get(k)
            if a in (None, "") or b in (None, ""):
                continue
            if a != b:
                conflicts = True
                break
        if not conflicts:
            merged = dict(existing)
            for k, v in payload.items():
                if v in (None, ""):
                    continue
                merged[k] = v
            arr[i] = merged
            return
    arr.append(dict(payload))

def _active_contract_id_for_template(session, template_id: int) -> Optional[int]:
    """Resolve the active contract linked to an output template via the new
    hierarchy (Contract.output_template_id == template_id, status='active')."""
    c = (
        session.query(Contract)
        .filter(Contract.output_template_id == template_id,
                Contract.status == "active")
        .order_by(Contract.id.desc())
        .first()
    )
    return c.id if c else None

def _contract_id_for_template(session, t: ExportTemplate) -> Optional[int]:
    """Best contract id to show for a template. Prefer the *active* linked
    contract; else the latest contract linked to this template regardless of
    status (e.g. drafted/superseded — so it isn't shown as null while a contract
    clearly exists); else the legacy ExportTemplate.contract_id column."""
    active = _active_contract_id_for_template(session, t.id)
    if active:
        return active
    latest = (
        session.query(Contract)
        .filter(Contract.output_template_id == t.id)
        .order_by(Contract.id.desc())
        .first()
    )
    if latest:
        return latest.id
    return getattr(t, "contract_id", None)

def _carrier_for_template(session, t: ExportTemplate, contract_id: Optional[int]):
    """Return (carrier_party_id, carrier_name) for display.

    The carrier is often captured on the program/contract rather than stamped on
    the ExportTemplate row (and is never carried onto new template versions), so
    the row's own carrier_party_id is frequently null. Derive it generically:
      1. the row's own values, if set;
      2. else the linked contract's program's carrier party
         (template -> contract -> program.party_id -> party.legal_name);
      3. else a sibling version of the same (mga, name) that has a carrier.
    """
    cpid = getattr(t, "carrier_party_id", None)
    cname = t.carrier

    def _name(pid):
        p = session.get(Party, pid) if pid else None
        return p.legal_name if p else None

    # 1. Explicit on the row.
    if cpid:
        return cpid, (cname or _name(cpid))

    # 2. Derive via the linked contract -> program -> carrier party.
    if contract_id:
        c = session.get(Contract, contract_id)
        if c and getattr(c, "program_id", None):
            prog = session.get(Program, c.program_id)
            if prog and getattr(prog, "party_id", None):
                return prog.party_id, (_name(prog.party_id) or cname)

    # 3. Fall back to a sibling version that already has a carrier set.
    sib = (
        session.query(ExportTemplate)
        .filter(ExportTemplate.tenant_id == t.tenant_id, ExportTemplate.name == t.name,
                ExportTemplate.carrier_party_id.isnot(None))
        .order_by(ExportTemplate.version.desc())
        .first()
    )
    if sib and sib.carrier_party_id:
        return sib.carrier_party_id, (sib.carrier or _name(sib.carrier_party_id))

    return cpid, cname

def _template_to_dict(t: ExportTemplate, session=None) -> dict:
    # Carrier + contract are often held on the program/contract hierarchy rather
    # than on the template row itself, so derive them (read-only) when missing.
    contract_id = getattr(t, "contract_id", None)
    carrier = t.carrier
    carrier_party_id = getattr(t, "carrier_party_id", None)
    if session is not None:
        contract_id = _contract_id_for_template(session, t)
        carrier_party_id, carrier = _carrier_for_template(session, t, contract_id)
    return {
        "id": t.id,
        "mga": _tenant_name(session, t.tenant_id) if session is not None else None,
        "tenant_id": t.tenant_id,
        "name": t.name,
        "carrier": carrier,
        "carrier_party_id": carrier_party_id,
        "contract_id": contract_id,
        "version": t.version or 1,
        "is_active": bool(t.is_active),
        "approved": bool(t.approved),
        "structure": t.structure,
    }

def _extract_template_fields(structure: dict) -> list[dict]:
    """Return a flat list of {name, canonical_field, sheet} from a template structure.
    Used to give the LLM the Output Template field vocabulary when extracting
    contract rules (Contract → Output Template hierarchy)."""
    from exporter import is_reference_sheet
    fields = []
    for sheet in (structure.get("sheets") or []):
        # Reference / lookup tabs are not rule targets — keep their columns out of
        # the extraction vocabulary so rules are never generated for them.
        if is_reference_sheet(sheet):
            continue
        sheet_name = sheet.get("sheet_name", "")
        for col in (sheet.get("columns") or []):
            col_name = col.get("column_name") or col.get("header") or ""
            if col_name:
                fields.append({
                    "name": col_name,
                    "sheet": sheet_name,
                    "canonical_field": col.get("canonical_field"),
                    "samples": col.get("samples", [])[:3],
                })
    return fields

class UpdateExportTemplateBody(BaseModel):
    structure: Optional[dict[str, Any]] = None
    name: Optional[str] = None
    carrier: Optional[str] = None
    carrier_party_id: Optional[int] = None
    contract_id: Optional[int] = None
    approved: Optional[bool] = None

def _xlsx_to_grid(blob: bytes, max_rows: int = 500) -> list[dict]:
    """Read a generated xlsx back into a per-sheet cell grid for in-site viewing."""
    import io
    import openpyxl
    out: list[dict] = []
    wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows: list[list] = []
            for r in ws.iter_rows(values_only=True):
                rows.append(["" if c is None else c for c in r])
                if len(rows) >= max_rows:
                    break
            out.append({"sheet": ws.title, "rows": rows})
    finally:
        wb.close()
    return out

def _to_number(v):
    """Best-effort numeric coercion for range checks. Returns None if not numeric."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().replace(",", "").replace("$", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None

def _check_field_constraints(field: str, value, c: dict) -> list[str]:
    """Evaluate a single field's JSON-Schema constraints against `value`.
    Returns a list of human-readable violation details (empty = OK)."""
    out: list[str] = []
    s = str(value).strip() if value is not None else ""
    if s == "":
        return out  # emptiness is handled by `required`, not by constraints

    enum_vals = c.get("enum")
    if enum_vals:
        allowed = [str(v) for v in enum_vals]
        if s not in allowed:
            out.append(f"'{s}' not in allowed values: {allowed}")

    pattern = c.get("pattern")
    if pattern:
        import re as _re
        try:
            if not _re.search(pattern, s):
                out.append(f"'{s}' does not match pattern {pattern}")
        except Exception:
            pass

    # Numeric bounds
    num = _to_number(value)
    for key, op, label in (
        ("maximum", lambda n, b: n <= b, "exceeds maximum"),
        ("minimum", lambda n, b: n >= b, "below minimum"),
        ("exclusiveMaximum", lambda n, b: n < b, "not below exclusive maximum"),
        ("exclusiveMinimum", lambda n, b: n > b, "not above exclusive minimum"),
    ):
        bound = c.get(key)
        if bound is not None and num is not None and not op(num, bound):
            out.append(f"{num} {label} {bound}")
    return out

def _evaluate_ajv_rule(record: dict, spec: dict, target: dict) -> list[tuple[str, str]]:
    """Evaluate one AJV/JSON-Schema rule against a single output record
    (a {output_field: value} dict). Returns list of (field, detail) violations.

    Supports the common shapes our synthesizer emits:
      - {"required": ["Field"]}
      - {"properties": {"Field": {type/enum/pattern/min/max}}, "required": [...]}
      - flat {"field": "X", "enum"/"pattern"/...: ...}
    Conditional shapes (allOf/if/then) are skipped (no false positives).
    """
    violations: list[tuple[str, str]] = []
    if spec.get("allOf") or spec.get("if") or spec.get("anyOf") or spec.get("oneOf"):
        return violations  # conditional logic not evaluated here

    props = spec.get("properties") if isinstance(spec.get("properties"), dict) else {}

    # required: list of field names that must be present & non-empty
    req = spec.get("required")
    req_fields = req if isinstance(req, list) else []
    for f in req_fields:
        v = record.get(f)
        if v is None or str(v).strip() == "":
            violations.append((f, "value is required"))

    # per-field constraints from `properties`
    for f, constraints in props.items():
        if not isinstance(constraints, dict):
            continue
        for detail in _check_field_constraints(f, record.get(f), constraints):
            violations.append((f, detail))

    # flat fallback (single field keyed by target.output_field / spec.field)
    flat_field = target.get("output_field") or spec.get("field")
    if flat_field and flat_field not in props:
        flat_constraints = {
            k: spec[k] for k in
            ("enum", "pattern", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
            if k in spec
        }
        if flat_constraints:
            for detail in _check_field_constraints(flat_field, record.get(flat_field), flat_constraints):
                violations.append((flat_field, detail))

    return violations

def _validate_records(structure: dict, records_by_sheet: list[dict],
                      contract_rules: list | None = None,
                      max_exc: int = 200) -> list[dict]:
    """Validate the resolved output values BEFORE rendering xlsx.

    Two passes (mirrors the previous grid-based `_validate_output`, but on the
    real typed values rather than a re-read workbook):
      1. Completeness — every column mapped to a canonical field must carry a
         value in every data row.
      2. Contract rules — AJV/JSON-Schema rules from the active contract,
         evaluated per row against Output Template field names.
    """
    import json as _json
    exceptions: list[dict] = []
    sheets_meta = {s.get("sheet_name"): s for s in (structure.get("sheets") or [])}

    for block in records_by_sheet:
        sheet = block["sheet"]
        meta = sheets_meta.get(sheet)
        if not meta:
            continue
        mapped = [c for c in (meta.get("columns") or []) if c.get("canonical_field")]

        for idx, rec in enumerate(block["records"]):
            # Pass 1: completeness
            for c in mapped:
                name = c.get("column_name")
                val = rec.get(name)
                if val is None or str(val).strip() == "":
                    exceptions.append({
                        "severity": "warning",
                        "code": "missing_value",
                        "sheet": sheet,
                        "row": idx + 1,
                        "column": name,
                        "field": c.get("canonical_field"),
                        "message": f"Expected value for '{c.get('canonical_field')}' is empty",
                    })
                    if len(exceptions) >= max_exc:
                        return exceptions

            # Pass 2: contract rules
            for rule in (contract_rules or []):
                if rule.get("rule_engine", "ajv") != "ajv":
                    continue
                spec = rule.get("rule_spec") or {}
                if isinstance(spec, str):
                    try:
                        spec = _json.loads(spec)
                    except Exception:
                        spec = {}
                target = rule.get("canonical_target") or {}
                if isinstance(target, str):
                    try:
                        target = _json.loads(target)
                    except Exception:
                        target = {}

                for field_name, detail in _evaluate_ajv_rule(rec, spec, target):
                    exceptions.append({
                        "severity": rule.get("severity") or "warning",
                        "code": rule.get("rule_name") or "contract_rule",
                        "sheet": sheet,
                        "row": idx + 1,
                        "column": field_name,
                        "field": field_name,
                        "message": rule.get("error_message") or detail,
                        "rule_id": rule.get("rule_id"),
                    })
                    if len(exceptions) >= max_exc:
                        return exceptions

    return exceptions

def _attach_recommendations(excs: list) -> list:
    """Enrich stored output-stage exceptions with a rule-derived recommendation.

    The exceptions persisted on an OutputExport carry only actual_value — the
    "Expected/Recommended" value is not stored. To keep the exception-review
    screen consistent with the upload path, derive it here at read time from the
    rule's rule_spec (rule_id -> validation_rule.rule_spec -> _expected_from_ir).
    Returns a NEW list of shallow-copied dicts so the ORM's JSONB attribute is
    never mutated in place. Best-effort: any failure leaves exceptions untouched.
    """
    if not excs:
        return excs
    rule_ids = sorted({e["rule_id"] for e in excs
                       if isinstance(e, dict) and e.get("rule_id")})
    if not rule_ids:
        return excs
    try:
        with CanonicalSession() as cs:
            rows = cs.execute(
                text("SELECT rule_id, rule_spec FROM validation_rule "
                     "WHERE rule_id = ANY(:ids)"),
                {"ids": rule_ids},
            ).mappings().all()
        spec_by_id = {row["rule_id"]: row["rule_spec"] for row in rows}
    except Exception:
        return excs
    out = []
    for e in excs:
        if not isinstance(e, dict):
            out.append(e)
            continue
        e = dict(e)
        spec = spec_by_id.get(e.get("rule_id"))
        if e.get("recommendation") in (None, ""):
            try:
                rec = _expected_from_ir(spec)
            except Exception:
                rec = None
            if rec not in (None, ""):
                e["recommendation"] = rec
                if e.get("expected_value") in (None, ""):
                    e["expected_value"] = rec
        # Structured enum options (comma-safe) so the UI never splits "one of: …".
        if not e.get("recommendation_options"):
            try:
                opts = _options_from_ir(spec)
            except Exception:
                opts = None
            if opts:
                e["recommendation_options"] = opts
        out.append(e)
    return out

def resolve_export_policy_numbers(s, *, upload_id, policy_ids, policy_numbers) -> dict:
    """Map policy_number -> {'policy_id', 'tenant_id'} for one export's policies.

    An export's tenant (the MGA) is NOT the policies' tenant (the carrier/source),
    and policy_number is only unique WITHIN a source — not globally. So scope to
    the export's own policy set when known (its policy_ids, else the source
    upload's policies); otherwise fall back to a global current-version lookup and
    accept only policy_numbers that resolve UNAMBIGUOUSLY (exactly one row) —
    ambiguous ones are dropped rather than risk writing to the wrong policy."""
    pns = sorted({str(p).strip() for p in policy_numbers if str(p or "").strip()})
    if not pns:
        return {}
    scope_ids = None
    if policy_ids:
        scope_ids = [int(x) for x in policy_ids]
    elif upload_id:
        rows = s.execute(
            text("SELECT policy_id FROM upload_policy WHERE upload_id = :u"),
            {"u": upload_id},
        ).fetchall()
        scope_ids = [r[0] for r in rows] or None
    if scope_ids:
        rows = s.execute(
            text("SELECT policy_id, policy_number, tenant_id FROM policy "
                 "WHERE policy_id = ANY(:ids) AND policy_number = ANY(:pns) "
                 "AND is_current_version IS NOT FALSE"),
            {"ids": scope_ids, "pns": pns},
        ).mappings().all()
        return {str(r["policy_number"]): {"policy_id": r["policy_id"],
                                          "tenant_id": r["tenant_id"]} for r in rows}
    # Unscoped fallback — accept only unambiguous (single current-version) matches.
    rows = s.execute(
        text("SELECT policy_id, policy_number, tenant_id FROM policy "
             "WHERE policy_number = ANY(:pns) AND is_current_version IS NOT FALSE"),
        {"pns": pns},
    ).mappings().all()
    seen: dict = {}
    for r in rows:
        pn = str(r["policy_number"])
        seen[pn] = None if pn in seen else {"policy_id": r["policy_id"],
                                            "tenant_id": r["tenant_id"]}
    return {k: v for k, v in seen.items() if v}

_KIND_STATUS = {"approve": "approved", "fix": "fixed",
                "dismiss": "dismissed", "reject": "rejected"}

def _field_of(e: dict):
    """The output field key, matching the frontend's `x.column ?? x.field`."""
    c = e.get("column")
    return c if c is not None else e.get("field")

def _attach_direct_lane_decisions(excs: list, landing_id: int) -> list:
    """Attach decisions for a DIRECT-LANE export from landing_correction, keyed by
    (output_sheet, output_row, output_field) — no canonical policy involved."""
    try:
        with SessionLocal() as s:
            rows = s.execute(
                text("SELECT output_sheet, output_row, output_field, kind, reason, "
                     "new_value FROM landing_correction WHERE landing_id = :l"),
                {"l": landing_id},
            ).mappings().all()
    except Exception:
        return excs
    by_key = {(x["output_sheet"], x["output_row"], x["output_field"]): x for x in rows}
    if not by_key:
        return excs

    def _row_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    out = []
    for e in excs:
        if not isinstance(e, dict):
            out.append(e)
            continue
        hit = by_key.get((e.get("sheet"), _row_int(e.get("row")), _field_of(e)))
        if hit:
            e = dict(e)
            e["status"] = _KIND_STATUS.get(hit["kind"], "resolved")
            e["resolution_note"] = hit["reason"]
        out.append(e)
    return out

def _attach_decisions(excs: list, r: OutputExport) -> list:
    """Attach any saved decision (status + resolution_note) to stored output-stage
    exceptions, so the review screen shows the reviewer's Approve/Fix/Dismiss/Reject
    state across refreshes.

    Direct-lane exports (output projected from a landing_record) resolve decisions
    from landing_correction by (sheet, row, field); canonical-lane exports match
    validation_exception by (rule_id, policy_id, field_path)."""
    if not excs:
        return excs
    # Direct lane?
    try:
        with SessionLocal() as s:
            landing_id = s.execute(
                text("SELECT id FROM landing_record WHERE output_export_id = :e "
                     "ORDER BY id DESC LIMIT 1"), {"e": r.id},
            ).scalar()
    except Exception:
        landing_id = None
    if landing_id is not None:
        return _attach_direct_lane_decisions(excs, int(landing_id))

    pns = [e["policy_number"] for e in excs
           if isinstance(e, dict) and e.get("policy_number")]
    if not pns:
        return excs
    try:
        with CanonicalSession() as cs:
            pn_map = resolve_export_policy_numbers(
                cs, upload_id=r.source_upload_id, policy_ids=r.policy_ids,
                policy_numbers=pns)
            pids = sorted({v["policy_id"] for v in pn_map.values()})
            if not pids:
                return excs
            drows = cs.execute(
                text("SELECT exception_id, rule_id, source_entity_id, field_path, "
                     "status, resolution_note FROM validation_exception "
                     "WHERE source_entity_id = ANY(:pids) ORDER BY exception_id"),
                {"pids": pids},
            ).mappings().all()
    except Exception:
        return excs
    by_key = {(x["rule_id"], x["source_entity_id"], x["field_path"]): x
              for x in drows}
    out = []
    for e in excs:
        if not isinstance(e, dict):
            out.append(e)
            continue
        hit = pn_map.get(str(e.get("policy_number") or "").strip())
        pid = hit["policy_id"] if hit else None
        row = by_key.get((e.get("rule_id"), pid, e.get("field") or e.get("column")))
        if row:
            e = dict(e)
            e["exception_id"] = row["exception_id"]
            e["status"] = row["status"]
            e["resolution_note"] = row["resolution_note"]
        out.append(e)
    return out

def _export_to_dict(r: OutputExport, with_exceptions: bool = False,
                    mga: Optional[str] = None) -> dict:
    d = {
        "id": r.id, "mga": mga, "tenant_id": r.tenant_id, "template_id": r.template_id,
        "template_name": r.template_name, "filename": r.filename,
        "source_upload_id": r.source_upload_id,
        "generated_by": r.generated_by,
        "policy_count": r.policy_count or 0,
        "exception_count": r.exception_count or 0,
        "status": r.status or "clean",
        "created_at": _iso_utc(r.created_at),
    }
    if with_exceptions:
        excs = _attach_recommendations(r.exceptions or [])
        d["exceptions"] = _attach_decisions(excs, r)
    return d

def _ir_template(rule_spec):
    """The IR template name of a rule_spec (ir_v1 only), else None."""
    import json as _json
    if isinstance(rule_spec, str):
        try:
            rule_spec = _json.loads(rule_spec)
        except Exception:
            return None
    if not isinstance(rule_spec, dict):
        return None
    return (rule_spec.get("ir") or {}).get("template")

def _expected_from_ajv_schema(rule_spec) -> Optional[str]:
    """Expected-value hint for a LEGACY (non-ir) AJV JSON-Schema rule_spec — mirrors
    the old JS engine's expectedFromAjvError. Best-effort: returns the first concrete
    constraint found at the top level or on any property."""
    import json as _json
    if isinstance(rule_spec, str):
        try:
            rule_spec = _json.loads(rule_spec)
        except Exception:
            return None
    if not isinstance(rule_spec, dict):
        return None
    sch = rule_spec.get("schema") if isinstance(rule_spec.get("schema"), dict) else rule_spec

    def _num(v):
        try:
            f = float(v)
            return f"{int(f):,}" if f == int(f) else f"{f:,}"
        except (TypeError, ValueError):
            return str(v)

    nodes = [sch]
    props = sch.get("properties")
    if isinstance(props, dict):
        nodes += [v for v in props.values() if isinstance(v, dict)]
    for n in nodes:
        if "const" in n:
            return str(n["const"])
        if isinstance(n.get("enum"), list) and n["enum"]:
            return "one of: " + ", ".join(str(x) for x in n["enum"])
        if n.get("maximum") is not None:
            return f"<= {_num(n['maximum'])}"
        if n.get("exclusiveMaximum") is not None:
            return f"< {_num(n['exclusiveMaximum'])}"
        if n.get("minimum") is not None:
            return f">= {_num(n['minimum'])}"
        if n.get("exclusiveMinimum") is not None:
            return f"> {_num(n['exclusiveMinimum'])}"
        if n.get("formatMinimum") is not None:
            return f"on/after {n['formatMinimum']}"
        if n.get("formatMaximum") is not None:
            return f"on/before {n['formatMaximum']}"
        if n.get("multipleOf") is not None:
            return f"multiple of {_num(n['multipleOf'])}"
    return None

def _options_from_ir(rule_spec) -> Optional[list]:
    """The raw list of allowed values for an enum rule (`value_in_set`, or a
    legacy AJV `enum`). The UI renders each as its own choice from THIS list —
    never by splitting the comma-joined "one of: …" string, because a value can
    itself contain a comma (e.g. "Palms Insurance Company, Limited"). Returns
    None for non-enum rules."""
    import json as _json
    if isinstance(rule_spec, str):
        try:
            rule_spec = _json.loads(rule_spec)
        except Exception:
            return None
    if not isinstance(rule_spec, dict):
        return None
    ir = rule_spec.get("ir") or {}
    if ir.get("template") == "value_in_set":
        allowed = (ir.get("params") or {}).get("allowed")
        return [str(x) for x in allowed] if isinstance(allowed, list) and allowed else None
    # Legacy AJV: top-level enum, or the first property with an enum.
    enum = rule_spec.get("enum")
    if isinstance(enum, list) and enum:
        return [str(x) for x in enum]
    props = rule_spec.get("properties")
    if isinstance(props, dict):
        for pv in props.values():
            if isinstance(pv, dict) and isinstance(pv.get("enum"), list) and pv["enum"]:
                return [str(x) for x in pv["enum"]]
    return None

def _expected_from_ir(rule_spec) -> Optional[str]:
    """Human-readable description of the constraint a contract rule enforces — the
    "Expected:" hint shown in the Modify-here editor. Derived deterministically
    from the rule's IR template + params, covering the whole TEMPLATE_CATALOG
    (rule_ir.py). Mirrors the old JS engine's expectedValue style ("<= N", "one
    of: ..."). Falls back to the legacy AJV JSON-Schema for non-ir specs. Returns
    None when no concise constraint applies."""
    import json as _json
    if isinstance(rule_spec, str):
        try:
            rule_spec = _json.loads(rule_spec)
        except Exception:
            return None
    if not isinstance(rule_spec, dict):
        return None
    ir = rule_spec.get("ir") or {}
    tmpl = ir.get("template")
    p = ir.get("params") or {}
    if not tmpl or not isinstance(p, dict):
        # Legacy (non-ir) AJV/custom rule_spec — derive from the JSON Schema.
        return _expected_from_ajv_schema(rule_spec)

    def num(v):
        try:
            f = float(v)
            return f"{int(f):,}" if f == int(f) else f"{f:,}"
        except (TypeError, ValueError):
            return str(v)

    def join(vals):
        if isinstance(vals, (list, tuple)):
            return ", ".join(str(x) for x in vals)
        return str(vals)

    if tmpl == "required_field":
        return "Required"
    if tmpl == "conditional_required":
        cond = p.get("condition")
        return f"Required when {cond}" if cond else "Required (conditional)"
    if tmpl == "value_in_set":
        return f"one of: {join(p.get('allowed'))}" if p.get("allowed") else None
    if tmpl == "value_not_in_set":
        return f"not: {join(p.get('excluded'))}" if p.get("excluded") else None
    if tmpl == "max_limit":
        return f"<= {num(p.get('max'))}" if p.get("max") is not None else None
    if tmpl == "min_limit":
        return f">= {num(p.get('min'))}" if p.get("min") is not None else None
    if tmpl == "range_check":
        lo, hi = p.get("min"), p.get("max")
        if lo is not None and hi is not None:
            return f"between {num(lo)} and {num(hi)}"
        if hi is not None:
            return f"<= {num(hi)}"
        if lo is not None:
            return f">= {num(lo)}"
        return None
    if tmpl == "pattern_check":
        pat = p.get("pattern")
        return f"matches {pat}" if pat else None
    if tmpl == "date_bound":
        # Field vs a FIXED calendar date, e.g. ">= 2025-10-01".
        op, d = p.get("op"), p.get("date")
        if d is None:
            return None
        return f"{op} {d}".strip() if op else str(d)
    if tmpl == "date_relation":
        op, other = p.get("op"), p.get("other_field")
        return (f"{op} {other}".strip()) if (op or other) else None
    if tmpl == "period_duration":
        op, val, unit = p.get("op"), p.get("value"), p.get("unit")
        if op is not None and val is not None and unit:
            return f"{op} {num(val)} {unit}(s)"
        return None
    if tmpl == "aggregate_cap":
        agg = p.get("aggregation") or "sum"
        if p.get("max") is not None:
            return f"{agg} <= {num(p.get('max'))}"
        if p.get("min") is not None:
            return f"{agg} >= {num(p.get('min'))}"
        return None
    if tmpl == "uniqueness":
        flds = p.get("fields")
        return f"unique: {join(flds)}" if flds else "unique"
    if tmpl == "cross_field_compare":
        op, other = p.get("op"), p.get("other_field")
        if op and other:
            operator, factor = p.get("operator"), p.get("factor")
            expr = (f"{other} {operator} {factor}"
                    if operator and factor is not None else other)
            return f"{op} {expr}"
        return None
    if tmpl == "conditional_value":
        # The value alone is the actionable expected (the triggering condition is
        # already shown in the rule's error message / contract clause). Keeping the
        # recommendation as just "{op} {value}" lets Approve write it back cleanly.
        op, val = p.get("op"), p.get("value")
        if op and val is not None:
            return f"{op} {val}"
        return None
    # cross_field_math and anything unrecognised: no concise expected value.
    return None

def _resolve_export_target(s, template_id, upload_id, policy_ids, principal=None):
    """Resolve everything an export needs from a template + source selection.

    Read-only mirror of the resolution block in /export/generate, so the
    pre-generation dry run (/export/validate) can run the SAME validation without
    building or persisting a file. Returns the template structure, the active
    linked contract, and the target canonical policy_ids.
    """
    t = s.get(ExportTemplate, template_id)
    if not t:
        raise HTTPException(404, "template not found")
    if principal is not None:
        assert_tenant_owns(principal, t.tenant_id)

    # Active contract linked to this output template (new hierarchy).
    active_contract = (
        s.query(Contract)
        .filter(Contract.output_template_id == template_id,
                Contract.status == "active")
        .order_by(Contract.id.desc())
        .first()
    )
    active_contract_id = active_contract.id if active_contract else None
    active_contract_info = (
        {"id": active_contract.id, "filename": active_contract.filename}
        if active_contract else None
    )

    ids: list[int] = []
    program_id: Optional[int] = None
    if upload_id is not None:
        rows = s.execute(
            select(UploadPolicy.policy_id)
            .where(UploadPolicy.upload_id == upload_id)
            .order_by(UploadPolicy.id.asc())
        ).fetchall()
        ids = [r[0] for r in rows]
        upload_obj = s.get(Upload, upload_id)
        if upload_obj:
            prog = (
                s.query(Program)
                .filter(Program.tenant_id == upload_obj.tenant_id)
                .order_by(Program.id.desc())
                .first()
            )
            program_id = prog.id if prog else None
    elif policy_ids:
        try:
            ids = [int(x.strip()) for x in policy_ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "policy_ids must be comma-separated integers")
    else:
        raise HTTPException(400, "provide upload_id or policy_ids")

    return {
        "structure": t.structure,
        "active_contract_id": active_contract_id,
        "active_contract_info": active_contract_info,
        "ids": ids,
        "program_id": program_id,
    }
