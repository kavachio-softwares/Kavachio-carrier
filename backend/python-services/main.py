"""FastAPI app exposing the BDX onboarding + ingestion flow."""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Optional

from pathlib import Path

from dotenv import load_dotenv

# Load .env BEFORE importing db (db.py reads DATABASE_URL and creates the
# engine at import time).
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

log = logging.getLogger("bdx.main")
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel
from sqlalchemy import case, or_, func, select, text
from sqlalchemy.orm import load_only

from data_model import DATA_MODEL
from assembler import fetch_policies
from db import (
    ActivityEvent, BDXRecord, CanonicalSession, Contract, ExportTemplate, Mapper,Program,
    OutputExport, SessionLocal, Upload, UploadPolicy, init_db, Party,
    SheetBinding, UploadSheetContract, exception_severity_counts,
)
from exporter import generate_workbook, build_output_records, parse_template, propose_template_mapping
from app_routes import router as app_router, resolve_tenant_id, assert_tenant_owns, _iso_utc
from auth_deps import current_principal, Principal
from validation_routes import router as validation_router
from direct_routes import router as direct_router
from ingester import _ensure_canonical_upload, _ensure_tenant, ingest_record
from mapper import (
    apply_spec_multi,
    generate_mapping_multi,
    read_excel_all_sheets,
    signature_multi,
)
import storage  # blob storage abstraction (Azure/Azurite; DB-blob fallback)

init_db()

app = FastAPI(title="BDX Mapper", version="0.3.0")

# CORS: lock to known frontends. Override in prod via CORS_ORIGINS (comma-
# separated); defaults to the local dev origins. Never ship "*".
import os as _os
_cors_origins = [o.strip() for o in _os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
).split(",") if o.strip()]

# Password-reset & invite emails link to APP_BASE_URL, so that origin must be
# able to call the API (e.g. /auth/reset/validate) from the emailed page. Always
# allow it — this keeps CORS in sync with wherever the emails point, so the
# reset/invite page never hits a cross-origin block.
_app_base = (_os.getenv("APP_BASE_URL", "") or "").strip().rstrip("/")
if _app_base and _app_base not in _cors_origins:
    _cors_origins.append(_app_base)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # Cache preflight (OPTIONS) responses for an hour so the browser doesn't
    # re-preflight every cross-origin call — without this the network tab shows
    # 2 entries (OPTIONS + real request) for nearly every api call on the
    # deployed site. Chrome caps this at 2h, Firefox at 24h.
    max_age=3600,
)

app.include_router(app_router)
app.include_router(validation_router)
app.include_router(direct_router)

# C-9 — daily background sweep: create overdue / due-soon reminder events even
# when nobody opens the calendar. In-process (asyncio), idempotent, off the event
# loop; opt-out via SWEEP_SCHEDULER_ENABLED=0. See sweep_scheduler.py.
import sweep_scheduler  # noqa: E402
sweep_scheduler.start(app)


# --- DB audit middleware ---------------------------------------------------
# Records every mutating request (POST/PUT/PATCH/DELETE) as an activity_events
# row with the REAL actor (resolved from the Bearer token), and every download /
# source-data read as an access_log row. /auth/* is skipped here and handled by
# auth_audit; mutations that already self-log via app_routes._log() are skipped
# to avoid duplicates. Fully fail-safe: auditing never breaks or blocks the
# request (DB write runs off the event loop and swallows errors).
import audit as _audit  # noqa: E402
import asyncio as _asyncio  # noqa: E402
from starlette.concurrency import run_in_threadpool as _run_in_threadpool  # noqa: E402


async def _audit_activity(auth_header, method, path, status, ip):
    """Look up the actor + write the activity row. Runs OFF the request's
    critical path (see below) — the email lookup + insert are 2 extra DB
    round-trips that must not add latency to every write."""
    try:
        uid, tid = _audit.actor_from_token(auth_header)
        email = _audit.actor_email(uid)
        await _run_in_threadpool(
            _audit.log_activity, tid, email,
            _audit.friendly_action(method, path), path,
            {"status": status, "ip": ip, "method": method},
        )
    except Exception:  # noqa: BLE001 — auditing must never break anything
        pass


async def _audit_access(auth_header, path, ip):
    try:
        uid, tid = _audit.actor_from_token(auth_header)
        email = _audit.actor_email(uid)
        await _run_in_threadpool(
            _audit.log_access, email, path, "download", ip, uid, tid,
        )
    except Exception:  # noqa: BLE001
        pass


@app.middleware("http")
async def _db_audit_middleware(request, call_next):
    response = await call_next(request)
    try:
        method = request.method
        path = request.url.path
        status = getattr(response, "status_code", 200)
        if status < 400 and not path.startswith("/auth"):
            ip = request.client.host if request.client else None
            auth_header = request.headers.get("authorization", "")
            # Fire-and-forget: schedule the audit writes so the response returns
            # to the client immediately. Previously these were awaited here,
            # adding the email-lookup + insert round-trips to EVERY mutating
            # request's latency (writes felt slow against a remote DB).
            if method in ("POST", "PUT", "PATCH", "DELETE") and not _audit.is_self_logged(method, path):
                _asyncio.ensure_future(_audit_activity(auth_header, method, path, status, ip))
            elif method == "GET" and _audit.is_access_path(path):
                _asyncio.ensure_future(_audit_access(auth_header, path, ip))
    except Exception:  # noqa: BLE001 — auditing must never break the request
        pass
    return response


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


@app.get("/data-model")
def get_data_model(principal: Principal = Depends(current_principal)):
    return DATA_MODEL


# --- Workbook inspection (sheet picker) -----------------------------------

@app.post("/bdx/sheets")
async def bdx_sheets(
    file: UploadFile = File(...),
    skip_rows: int = Form(default=0),
    principal: Principal = Depends(current_principal),
):
    """Return the list of sheets in a workbook so the UI can ask the user
    which ones to process. Returned shape:
        { "sheets": [ {"name": "...", "rows": int, "columns": int,
                       "headers": [first 8 column names]} ] }
    """
    file_bytes = await file.read()
    try:
        sheets = await run_in_threadpool(read_excel_all_sheets, file_bytes, skip_rows)
    except Exception as e:
        raise HTTPException(400, f"could not parse uploaded file: {e}")
    if not sheets:
        raise HTTPException(400, "workbook has no readable sheets")
    out = []
    for name, df in sheets.items():
        out.append({
            "name": name,
            "rows": int(df.shape[0]),
            "columns": int(df.shape[1]),
            "headers": [str(c) for c in list(df.columns)[:8]],
        })
    return {"sheets": out}


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


# --- Template versioning helpers -------------------------------------------
# A "template" is the set of rows sharing (mga, name); each row is a version.
# `is_active` marks the single version used at runtime. Mappers without a name
# fall back to grouping by their column signature so legacy rows still work.

def _mapper_sibling_ids(session, m: Mapper) -> list[int]:
    """Ids of the rows that are versions of the same template as `m`.

    Reads (id, name, signature) only — flipping an `is_active` flag never needs
    the rest of the row, and every mapper row carries the spec/candidates JSON.
    """
    if m.name:
        return [i for (i,) in session.query(Mapper.id)
                .filter(Mapper.tenant_id == m.tenant_id, Mapper.name == m.name)]
    # Legacy unnamed rows group by column signature (a JSON compare).
    return [i for (i, sig) in session.query(Mapper.id, Mapper.signature)
            .filter(Mapper.tenant_id == m.tenant_id,
                    or_(Mapper.name.is_(None), Mapper.name == ""))
            if sig == m.signature]


def _activate_mapper(session, m: Mapper) -> None:
    """Make `m` the active version; deactivate its siblings."""
    ids = _mapper_sibling_ids(session, m)
    if not ids:
        return
    # One UPDATE instead of loading each sibling and flipping it in Python.
    # synchronize_session="fetch" keeps any already-loaded instances in step.
    (session.query(Mapper).filter(Mapper.id.in_(ids))
     .update({Mapper.is_active: case((Mapper.id == m.id, 1), else_=0)},
             synchronize_session="fetch"))


def _activate_template(session, t: ExportTemplate) -> None:
    (session.query(ExportTemplate)
     .filter(ExportTemplate.tenant_id == t.tenant_id, ExportTemplate.name == t.name)
     .update({ExportTemplate.is_active: case((ExportTemplate.id == t.id, 1), else_=0)},
             synchronize_session="fetch"))


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


# --- Onboarding: generate proposed mapping from a sample BDX ---------------

@app.post("/mapper/generate")
async def mapper_generate(
    mga: str = Form(...),
    file: UploadFile = File(...),
    name: Optional[str] = Form(default=None),
    carrier: Optional[str] = Form(default=None),
    contract: Optional[str] = Form(default=None),
    party_id: Optional[int] = Form(default=None),
    skip_rows: int = Form(default=0),
    sheets: Optional[str] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    """Generate a proposed header→canonical mapping AND save it for the MGA.

    `sheets` is an optional comma-separated list of sheet names to include;
    omit to use every sheet in the workbook.
    """
    file_bytes = await file.read()

    sheets_dict = await run_in_threadpool(read_excel_all_sheets, file_bytes, skip_rows)
    if not sheets_dict:
        raise HTTPException(400, "workbook has no readable sheets")
    sheets = _filter_sheets(sheets_dict, sheets)
    sig = signature_multi(sheets)

    # FORMAT-LEVEL CACHE: if ANY tenant has already produced a mapper with
    # the exact same column signature, clone its spec instead of calling
    # Gemini at all. Approved mappers win over drafts. Result: zero LLM
    # calls for a workbook whose layout we've seen before.
    cloned = _try_clone_existing_mapper(sig)
    cloned = None
    if cloned is not None:
        result = cloned
        log.info("Mapper format match — cloned spec from existing mapper "
                 "#%s (skipped Gemini entirely)",
                 cloned.get("_cloned_from_mapper_id"))
        # Re-derive sample dict from the current workbook so the response
        # carries fresh sample values even though the spec was cloned.
        from mapper import qualify, MAX_SAMPLES
        samples: dict[str, list[str]] = {}
        for sheet_name, df in sheets.items():
            for col in df.columns:
                q = qualify(str(sheet_name), str(col))
                samples[q] = df[col].dropna().astype(str).head(MAX_SAMPLES).tolist()
        result["samples"] = samples
        result["sheets"] = list(sheets.keys())
    else:
        result = await run_in_threadpool(generate_mapping_multi, sheets)
    source_columns = list(result["samples"].keys())  # 'sheet :: column'
    sample_rows = {
        name: df.head(5).where(lambda d: d.notna(), "").astype(str).to_dict(orient="records")
        for name, df in sheets.items()
    }
    sheets_meta = list(sheets.keys())

    # Persist the canonical fingerprint first (cross-tenant home), then
    # the operational mappers row referencing it.
    fingerprint_id: Optional[int] = None
    try:
        from fingerprint import upsert as fingerprint_upsert
        with CanonicalSession() as cs:
            fingerprint_id = fingerprint_upsert(
                cs,
                mga=mga,
                signature_tokens=sig,
                canonical_mapping=result.get("spec_by_sheet") or {},
            )
            cs.commit()
    except Exception as e:
        log.warning("canonical fingerprint upsert failed: %s", e)

    name = (name or "").strip() or None
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        # Versioning: a new row under an existing (tenant, name) becomes the next
        # version; the first version of a brand-new template auto-activates.
        # Only the highest existing version number is needed here, so read that
        # column alone rather than materialising every sibling row.
        if name:
            versions = [v for (v,) in s.query(Mapper.version)
                        .filter(Mapper.tenant_id == tid, Mapper.name == name)]
        else:
            # Unnamed (legacy) rows group by column signature instead, which is
            # a JSON compare — still done in Python, but over two columns.
            versions = [v for (v, sig_) in
                        s.query(Mapper.version, Mapper.signature)
                        .filter(Mapper.tenant_id == tid,
                                or_(Mapper.name.is_(None), Mapper.name == ""))
                        if sig_ == sig]
        version = (max((v or 1) for v in versions) + 1) if versions else 1
        is_active = 0 if versions else 1

        # Persist the workbook to blob storage (Azure/Azurite) when enabled;
        # otherwise keep the bytes inline in source_blob (legacy behaviour).
        mapper_blob_ref, mapper_blob_bytes = await run_in_threadpool(
            storage.store_or_keep, "mappers", tid, file.filename, file_bytes,
        )

        m = Mapper(
            tenant_id=tid, name=name, version=version, is_active=is_active,
            carrier=carrier, contract=contract, party_id=party_id,
            signature=sig,
            spec=result["spec"],
            spec_by_sheet=result["spec_by_sheet"],
            candidates=result.get("candidates_by_source") or {},
            samples=result.get("samples") or {},
            approved=0,
            source_filename=file.filename,
            source_blob=mapper_blob_bytes,
            source_blob_ref=mapper_blob_ref,
            selected_sheets=sheets_meta,
            fingerprint_id=fingerprint_id,
        )
        s.add(m)
        s.commit()
        s.refresh(m)
        mapper_id = m.id
        mapper_version = m.version
        mapper_active = bool(m.is_active)

    try:
        from audit import log_activity, actor_email
        log_activity(tid, actor_email(principal.user_id), "input_mapper_generated",
                     target=f"mapper:{mapper_id}",
                     details={"mapper_id": mapper_id, "name": name,
                              "version": mapper_version,
                              "source_filename": file.filename,
                              "sheets": sheets_meta})
    except Exception:  # noqa: BLE001
        pass

    return {
        "mapper_id": mapper_id,
        "name": name,
        "version": mapper_version,
        "is_active": mapper_active,
        "mga": mga, "carrier": carrier, "contract": contract, "party_id": party_id,
        "approved": False,
        "signature": sig,
        "sheets": sheets_meta,
        "source_columns": source_columns,
        "skip_rows": skip_rows,
        "spec": result["spec"],
        "spec_by_sheet": result["spec_by_sheet"],
        "candidates": result.get("candidates_by_source") or {},
        "categories": {
            "successful":   result["successful"],
            "likely":       result["likely"],
            "unsuccessful": result["unsuccessful"],
        },
        "canonical_unmapped": result["canonical_unmapped"],
        "samples": result["samples"],
        "sample_rows": sample_rows,
    }


# --- Update / approve mapper (MGA correction loop) -------------------------

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
        text("SELECT tenant_id FROM tenant WHERE tenant_name=:m LIMIT 1"),
        {"m": mga}).fetchone()
    return row[0] if row else None


def _tenant_name(session, tenant_id: Optional[int]) -> Optional[str]:
    """Resolve a tenant_id back to its mga code (tenant_name) for API responses.

    Memoised per session: list endpoints serialize many rows of the same tenant
    and were re-running this lookup once per row.
    """
    if not tenant_id:
        return None
    cache = getattr(session, "_tenant_name_cache", None)
    if cache is None:
        cache = session._tenant_name_cache = {}
    if tenant_id not in cache:
        row = session.execute(
            text("SELECT tenant_name FROM tenant WHERE tenant_id=:t LIMIT 1"),
            {"t": tenant_id}).fetchone()
        cache[tenant_id] = row[0] if row else None
    return cache[tenant_id]


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


@app.put("/mapper/{mapper_id}")
def mapper_update(mapper_id: int, body: UpdateMapperBody,
                  principal: Principal = Depends(current_principal)):
    """MGA submits corrected mappings (and optionally approves)."""
    if body.spec is None and body.spec_by_sheet is None and body.approved is None \
       and body.carrier is None and body.contract is None:
        raise HTTPException(400, "no fields to update")
    with SessionLocal() as s:
        m = s.get(Mapper, mapper_id)
        if not m:
            raise HTTPException(404, "mapper not found")
        assert_tenant_owns(principal, m.tenant_id)
        if body.spec is not None:
            m.spec = body.spec
        if body.spec_by_sheet is not None:
            m.spec_by_sheet = body.spec_by_sheet
            # Push every user-approved mapping into the cross-tenant cache
            # so future uploads of similar columns skip the LLM. User
            # corrections outrank the LLM via source="user".
            from mapper import sample_fingerprint, cache_store, SHEET_SEP
            # No raw samples on the saved mapper, so the cache row is keyed
            # by sheet+column only (empty fingerprint). That's enough to win
            # tier-2 lookups for the same column name on later uploads.
            for mapping in (m.spec_by_sheet or {}).values():
                for canonical, src_val in mapping.items():
                    srcs = src_val if isinstance(src_val, list) else [src_val]
                    for src in srcs:
                        s_name, _, col = src.partition(SHEET_SEP)
                        try:
                            cache_store(s, s_name, col,
                                        sample_fingerprint([]),
                                        canonical,
                                        confidence=1.0, source="user")
                        except Exception as e:
                            log.warning("cache_store(user) failed for %s → %s: %s",
                                        src, canonical, e)
        if body.approved is not None:
            m.approved = 1 if body.approved else 0
            # Approving a version makes it the live one for its template.
            if body.approved:
                _activate_mapper(s, m)
        if body.carrier is not None:
            m.carrier = body.carrier
        if body.contract is not None:
            m.contract = body.contract
        s.commit()
        s.refresh(m)

        # Mirror the approved spec into the canonical fingerprint so
        # future cross-tenant lookups serve the user-corrected mapping.
        if body.spec_by_sheet is not None:
            try:
                from fingerprint import upsert as fingerprint_upsert
                with CanonicalSession() as cs:
                    new_fp_id = fingerprint_upsert(
                        cs, mga=_tenant_name(s, m.tenant_id),
                        signature_tokens=m.signature or [],
                        canonical_mapping=m.spec_by_sheet or {},
                    )
                    cs.commit()
                if new_fp_id and new_fp_id != m.fingerprint_id:
                    m.fingerprint_id = new_fp_id
                    s.commit()
            except Exception as e:
                log.warning("fingerprint refresh on PUT failed: %s", e)
        try:
            from audit import log_activity, actor_email
            log_activity(m.tenant_id, actor_email(principal.user_id), "input_mapper_updated",
                         target=f"mapper:{mapper_id}",
                         details={"mapper_id": mapper_id, "version": m.version,
                                  "approved": bool(m.approved),
                                  "changed": [f for f in ("spec", "spec_by_sheet",
                                              "approved", "carrier", "contract")
                                              if getattr(body, f) is not None]})
        except Exception:  # noqa: BLE001
            pass
        return _mapper_to_dict(m, mga=_tenant_name(s, m.tenant_id))


@app.get("/mapper")
def mapper_list(mga: Optional[str] = None,
                principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        return [_mapper_to_dict(m, hsb, mga=mga) for m, hsb in _mapper_rows_no_blob(s, tid)]


@app.get("/mapper/templates")
def mapper_templates(mga: Optional[str] = None,
                     principal: Principal = Depends(current_principal)):
    """Input mappers grouped into templates with their versions, newest first."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        return _group_versions([_mapper_to_dict(m, hsb, mga=mga) for m, hsb in _mapper_rows_no_blob(s, tid)])


@app.post("/mapper/{mapper_id}/activate")
def mapper_activate(mapper_id: int,
                    principal: Principal = Depends(current_principal)):
    """Make this mapper version the active one for its template."""
    with SessionLocal() as s:
        m = s.get(Mapper, mapper_id)
        if not m:
            raise HTTPException(404, "mapper not found")
        assert_tenant_owns(principal, m.tenant_id)
        _activate_mapper(s, m)
        s.commit()
        s.refresh(m)
        try:
            from audit import log_activity, actor_email
            log_activity(m.tenant_id, actor_email(principal.user_id),
                         "input_mapper_activated", target=f"mapper:{mapper_id}",
                         details={"mapper_id": mapper_id, "version": m.version})
        except Exception:  # noqa: BLE001
            pass
        return _mapper_to_dict(m)


@app.get("/mapper/{mapper_id}")
def mapper_get(mapper_id: int,
               principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        m = s.get(Mapper, mapper_id)
        if not m:
            raise HTTPException(404, "mapper not found")
        assert_tenant_owns(principal, m.tenant_id)
        return _mapper_to_dict(m)


@app.get("/mapper/{mapper_id}/file")
def mapper_file(mapper_id: int,
                principal: Principal = Depends(current_principal)):
    """Download the original BDX workbook that was uploaded when this
    mapper was generated."""
    with SessionLocal() as s:
        m = s.get(Mapper, mapper_id)
        if not m:
            raise HTTPException(404, "mapper not found")
        assert_tenant_owns(principal, m.tenant_id)
        data = storage.resolve_bytes(m.source_blob_ref, m.source_blob)
        if not data:
            raise HTTPException(404, "no original file stored for this mapper")
        fname = m.source_filename or f"mapper_{mapper_id}.xlsx"
        return Response(
            content=data,
            media_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if fname.lower().endswith(".xlsx")
                else "application/octet-stream"
            ),
            headers={"Content-Disposition": _content_disposition(fname)},
        )


# --- Ingestion: actual BDX uploads -----------------------------------------

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
    # Two phases: rank on the four columns the decision needs, then load ONLY
    # the winner. Ranking every tenant mapper as a full entity meant reading
    # each one's spec/candidates/samples JSON just to throw all but one away.
    rows = session.query(
        Mapper.id, Mapper.signature, Mapper.is_active, Mapper.approved,
    ).filter(Mapper.tenant_id == tenant_id).all()

    # Collect all mappers whose signature is exactly or a subset of the upload.
    # Subset match: mapper covers fewer sheets than the upload — the extra
    # sheets were excluded when the mapper was trained (e.g. Summary/Check).
    candidates = [
        (mid, msig == sig, is_active, approved)   # (id, is_exact, …)
        for (mid, msig, is_active, approved) in rows
        if msig and set(msig) <= sig_set
    ]
    if not candidates:
        return None

    # Sort: active first, then exact-match preferred, then approved, then newest.
    candidates.sort(
        key=lambda t: ((t[2] or 0), int(t[1]), (t[3] or 0), t[0]),
        reverse=True,
    )
    return session.get(Mapper, candidates[0][0])



def _resolve_spec_by_sheet(m: Mapper) -> dict[str, dict[str, Any]]:
    """Use per-sheet spec when present; otherwise fall back to the flat spec
    by inferring the sheet from each source's 'Sheet :: Column' prefix."""
    if m.spec_by_sheet:
        return m.spec_by_sheet
    from mapper import SHEET_SEP
    out: dict[str, dict[str, Any]] = {}
    for canonical, src in (m.spec or {}).items():
        srcs = src if isinstance(src, list) else [src]
        for s in srcs:
            if SHEET_SEP in s:
                sheet = s.split(SHEET_SEP, 1)[0]
                out.setdefault(sheet, {})[canonical] = src
    return out


@app.post("/bdx/preview")
async def bdx_preview(
    mga: str = Form(...),
    file: UploadFile = File(...),
    skip_rows: int = Form(default=0),
    sheets: Optional[str] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    try:
        _raw = await file.read()
        sheets_dict = await run_in_threadpool(read_excel_all_sheets, _raw, skip_rows)
    except Exception as e:
        raise HTTPException(400, f"could not parse uploaded file: {e}")
    sheets_dict = _filter_sheets(sheets_dict, sheets)
    sig = signature_multi(sheets_dict)
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        m = _find_mapper(s, tid, sig)
        if not m:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "no_matching_mapper",
                    "message": "File format does not match any saved mapper for this MGA. "
                               "Create or update a mapping via /mapper/generate.",
                    "signature": sig,
                },
            )
        per_sheet = apply_spec_multi(sheets_dict, _resolve_spec_by_sheet(m))
        preview = {sheet: rows[:50] for sheet, rows in per_sheet.items()}
        counts = {sheet: len(rows) for sheet, rows in per_sheet.items()}
        return {
            "mapper_id": m.id,
            "sheets": list(sheets_dict.keys()),
            "rows": preview,
            "counts": counts,
            "total": sum(counts.values()),
        }


@app.post("/bdx/upload")
async def bdx_upload(
    mga: str = Form(...),
    file: UploadFile = File(...),
    skip_rows: int = Form(default=0),
    sheets: Optional[str] = Form(default=None),  # comma-separated sheet names from UI
    party_id: Optional[int] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    file_bytes = await file.read()
    try:
        sheets_dict = await run_in_threadpool(read_excel_all_sheets, file_bytes, skip_rows)
    except Exception as e:
        raise HTTPException(400, f"could not parse uploaded file: {e}")
    # Honour the user's sheet selection — filters BEFORE signature so the
    # signature matches the mapper that was trained on the same sheet set.
    sheets_dict = _filter_sheets(sheets_dict, sheets)
    sig = signature_multi(sheets_dict)
    with SessionLocal() as s:
        # Authoritative tenant from the trusted token (client `mga` ignored for
        # regular users). Used to scope the mapper lookup AND stamp all new rows.
        tid = resolve_tenant_id(s, principal, mga)
        auth_mga = _tenant_name(s, tid) or mga
        m = _find_mapper(s, tid, sig)
        if not m:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "no_matching_mapper",
                    "message": "File format does not match any saved mapper for this MGA.",
                    "signature": sig,
                },
            )

        per_sheet = apply_spec_multi(sheets_dict, _resolve_spec_by_sheet(m))
        ingested_by_sheet = {sh: len(rows) for sh, rows in per_sheet.items()}

        # Phase 2 — resolve each sheet's schedule from its saved binding. Guarded
        # so an un-migrated DB (no bdx_sheet_binding table) behaves exactly as
        # before: no bindings → schedule=None everywhere → single-scope merge.
        sheet_bindings = _sheet_bindings_for_mapper(s, m.id)

        def _skip_sheet(sh: str) -> bool:
            b = sheet_bindings.get(sh)
            return bool(b) and (b.get("role") in ("ignore", "summary", "check"))

        # Flatten across sheets (skipping non-policy sheets when bound) and merge
        # by (schedule, policy_number) so different schedules never collapse.
        flat, scopes = [], []
        for sh, rows in per_sheet.items():
            if _skip_sheet(sh):
                continue
            sched = (sheet_bindings.get(sh) or {}).get("schedule_key")
            for r in rows:
                flat.append(r)
                scopes.append(sched)
        merged = _merge_records(flat, scopes if any(scopes) else None)

        # The upload table now requires tenant_id — stamp it from the TRUSTED
        # token, never from the client-supplied `mga`.
        tenant_id_for_upload = tid

        # Persist the original workbook to blob storage (Azure/Azurite) when
        # enabled; otherwise keep it inline in source_blob (legacy behaviour).
        upload_blob_ref, upload_blob_bytes = await run_in_threadpool(
            storage.store_or_keep, "uploads", tenant_id_for_upload,
            file.filename, file_bytes,
        )

        upload = Upload(
            mapper_id=m.id, source_file=file.filename,
            sheets=list(sheets_dict.keys()),
            counts_by_sheet=ingested_by_sheet,
            total_rows=sum(ingested_by_sheet.values()),
            source_blob=upload_blob_bytes,
            source_blob_ref=upload_blob_ref,
            tenant_id=tenant_id_for_upload,
            party_id=party_id,
        )
        s.add(upload)
        s.flush()

        # Keep the legacy raw store too (handy for debugging) — one BDXRecord
        # per source row, tagged with the upload.
        for sheet_name, rows in per_sheet.items():
            for r in rows:
                s.add(BDXRecord(
                    upload_id=upload.id, tenant_id=tenant_id_for_upload, mapper_id=m.id,
                    source_file=file.filename, sheet_name=sheet_name, payload=r,
                ))

        # Canonical relational write — runs on the REMOTE Postgres database
        # in its own session/transaction.
        from datetime import datetime as _dt
        now = _dt.utcnow()
        policy_ids: list[int] = []
        ingest_errors: list[dict] = []
        with CanonicalSession() as cs:
            tenant_id = _ensure_tenant(cs, auth_mga)
            canonical_upload_id = _ensure_canonical_upload(
                cs, tenant_id, file.filename or "upload",
                now.year, now.month,
            )
            # Ingest each record inside its own SAVEPOINT so a single bad row
            # (malformed value, unexpected shape, constraint violation) is
            # logged and skipped instead of aborting the entire upload with a
            # 500. Good rows still commit.
            for idx, rec in enumerate(merged):
                try:
                    with cs.begin_nested():
                        pid = ingest_record(cs, auth_mga, rec,
                                            canonical_upload_id=canonical_upload_id)
                    if pid is not None:
                        policy_ids.append(pid)
                except Exception as ingest_exc:
                    polno = (rec.get("policy") or {}).get("policy_number")
                    log.warning(
                        "ingest_record failed for row %d (policy_number=%s): %s",
                        idx, polno, ingest_exc,
                    )
                    ingest_errors.append({
                        "row": idx, "policy_number": polno,
                        "error": str(ingest_exc),
                    })
            cs.commit()

        # Map upload → canonical policies in the local DB.
        for pid in policy_ids:
            s.add(UploadPolicy(upload_id=upload.id, policy_id=pid))

        # Phase 2 — per-upload lineage: record what each ingested sheet was bound
        # to (schedule / contract / output template). Guarded so an un-migrated DB
        # simply skips it.
        try:
            for sh in per_sheet.keys():
                b = sheet_bindings.get(sh)
                if not b:
                    continue
                s.add(UploadSheetContract(
                    upload_id=upload.id, sheet_name=sh,
                    schedule_key=b.get("schedule_key"),
                    contract_id=b.get("contract_id"),
                    output_template_id=b.get("output_template_id"),
                    was_override=False,
                ))
        except Exception as _lin_exc:
            log.warning("upload_sheet_contract lineage skipped: %s", _lin_exc)

        s.commit()
        try:
            from audit import log_activity, actor_email
            log_activity(upload.tenant_id, actor_email(principal.user_id), "bdx_uploaded",
                         target=f"upload:{upload.id}",
                         details={"upload_id": upload.id,
                                  "source_file": upload.source_file,
                                  "policies": len(policy_ids),
                                  "total_source_rows": upload.total_rows,
                                  "rows_skipped": len(ingest_errors)})
        except Exception:  # noqa: BLE001
            pass
        return {
            "upload_id": upload.id,
            "mapper_id": m.id,
            "ingested_by_sheet": ingested_by_sheet,
            "policies_loaded": len(policy_ids),
            "total_source_rows": upload.total_rows,
            "rows_skipped": len(ingest_errors),
            "ingest_errors": ingest_errors[:20],
        }


def _upload_to_dict(u, has_blob: bool = False, mga: Optional[str] = None) -> dict:
    return {
        "id": u.id, "mga": mga, "tenant_id": getattr(u, "tenant_id", None),
        "mapper_id": u.mapper_id,
        "source_file": u.source_file, "sheets": u.sheets,
        "counts_by_sheet": u.counts_by_sheet, "total_rows": u.total_rows,
        "has_source_blob": has_blob,
        "ingested_at": _iso_utc(u.ingested_at),
    }


@app.get("/uploads")
def uploads_list(mga: Optional[str] = None, limit: int = 50,
                 principal: Principal = Depends(current_principal)):
    """List uploads without loading blob bytes — blob presence checked via IS NOT NULL."""
    with SessionLocal() as s:
        # Select only the lightweight columns; check blob presence with SQL IS NOT NULL
        # so we never transfer the actual file bytes across the network.
        ut = Upload.__table__
        cols = [
            ut.c.upload_id, ut.c.tenant_id, ut.c.mapper_id, ut.c.source_file,
            ut.c.sheets, ut.c.counts_by_sheet, ut.c.total_rows, ut.c.ingested_at,
            case(
                (ut.c.source_blob.isnot(None) | ut.c.source_blob_ref.isnot(None), True),
                else_=False,
            ).label("has_source_blob"),
        ]
        tid = resolve_tenant_id(s, principal, mga)
        # The `upload` table is overloaded (§2a): each ingest writes an ops row
        # (mapper_id/source_file/total_rows set) AND a canonical lineage row
        # (those NULL, filename/num_rows set instead). The list must show only
        # the ops rows — the old `mga = :mga` filter did this implicitly because
        # canonical rows have mga NULL. Now that mga is gone we filter on
        # `mapper_id IS NOT NULL`, which partitions the two row types exactly.
        ops_row = ut.c.mapper_id.isnot(None)
        base = select(*cols).where(ops_row)
        q = s.execute(
            base.where(ut.c.tenant_id == tid)
            .order_by(ut.c.upload_id.desc()).limit(limit)
        )
        rows = q.mappings().all()
        return [
            {
                "id": r["upload_id"], "mga": mga, "tenant_id": r["tenant_id"],
                "mapper_id": r["mapper_id"],
                "source_file": r["source_file"], "sheets": r["sheets"],
                "counts_by_sheet": r["counts_by_sheet"], "total_rows": r["total_rows"],
                "has_source_blob": bool(r["has_source_blob"]),
                "ingested_at": _iso_utc(r["ingested_at"]),
            }
            for r in rows
        ]


@app.get("/uploads/{upload_id}")
def uploads_get(upload_id: int,
                principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        u = s.get(Upload, upload_id)
        if not u:
            raise HTTPException(404, "upload not found")
        assert_tenant_owns(principal, u.tenant_id)
        return _upload_to_dict(u, mga=_tenant_name(s, u.tenant_id))


@app.get("/uploads/{upload_id}/file")
def uploads_file(upload_id: int,
                 principal: Principal = Depends(current_principal)):
    """Download the exact original file the user ingested for this upload."""
    with SessionLocal() as s:
        u = s.get(Upload, upload_id)
        if not u:
            raise HTTPException(404, "upload not found")
        assert_tenant_owns(principal, u.tenant_id)
        data = storage.resolve_bytes(u.source_blob_ref, u.source_blob)
        if not data:
            raise HTTPException(404, "no original file stored for this upload")
        fname = u.source_file or f"upload_{upload_id}.xlsx"
        low = fname.lower()
        if low.endswith(".xlsx"):
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif low.endswith(".csv"):
            media = "text/csv"
        elif low.endswith(".xml"):
            media = "application/xml"
        else:
            media = "application/octet-stream"
        return Response(
            content=data,
            media_type=media,
            headers={"Content-Disposition": _content_disposition(fname)},
        )


@app.get("/uploads/{upload_id}/source-rows")
def uploads_source_rows(upload_id: int, policy_numbers: str = "",
                        principal: Principal = Depends(current_principal)):
    """Return the original rows from the uploaded Excel file for the given
    policy numbers (comma-separated).  Uses the mapper's spec_by_sheet to
    detect which column is the policy-number field.

    Response shape:
      {found: [{policy_number, sheet, row_number, row_data: {col: val}}],
       not_found: [<policy numbers with no matching row>]}
    """
    with SessionLocal() as s:
        u = s.get(Upload, upload_id)
        if not u:
            raise HTTPException(404, "upload not found")
        assert_tenant_owns(principal, u.tenant_id)
        # Resolve bytes inside the session so they survive the session close below.
        source_bytes = storage.resolve_bytes(u.source_blob_ref, u.source_blob)
        if not source_bytes:
            raise HTTPException(404, "no original file stored for this upload")

        mapper = s.get(Mapper, u.mapper_id) if u.mapper_id else None
        spec: dict = (mapper.spec_by_sheet or {}) if mapper else {}

    targets = {p.strip() for p in policy_numbers.split(",") if p.strip()}

    # Parse the source workbook (skip_rows not stored; try 0 first).
    try:
        sheets_dict = read_excel_all_sheets(source_bytes, 0)
    except Exception as e:
        raise HTTPException(400, f"could not parse source file: {e}")

    # Build a lookup: canonical_field → list of (sheet, bare_col_name)
    # spec_by_sheet shape: {sheet: {canonical_field: "Sheet :: Column" | ["S :: C", ...]}}
    SHEET_SEP = " :: "
    def _bare(src: str) -> tuple[str, str]:
        """'Sheet :: Column' → (sheet, column).  Plain 'Column' → ('', column)."""
        if SHEET_SEP in src:
            sh, col = src.split(SHEET_SEP, 1)
            return sh.strip(), col.strip()
        return "", src.strip()

    # Find which source column represents the policy number
    pn_cols: dict[str, str] = {}   # sheet → column_name
    for sheet, mapping in spec.items():
        for canonical, src_val in mapping.items():
            if "policy_number" in canonical.lower():
                srcs = src_val if isinstance(src_val, list) else [src_val]
                for sv in srcs:
                    sh, col = _bare(str(sv))
                    if sh == sheet or not sh:
                        pn_cols[sheet] = col
                        break

    # Fallback: auto-detect policy-number column by header keywords
    PN_KEYWORDS = ["policy ref", "policy no", "policy number", "pol ref", "pol no"]
    for sheet, df in sheets_dict.items():
        if sheet not in pn_cols:
            for col in df.columns:
                if any(kw in str(col).lower() for kw in PN_KEYWORDS):
                    pn_cols[sheet] = str(col)
                    break

    found: list[dict] = []
    found_pns: set[str] = set()

    for sheet_name, df in sheets_dict.items():
        pn_col = pn_cols.get(sheet_name)
        if not pn_col or pn_col not in df.columns:
            continue
        for idx, row in df.iterrows():
            pn = str(row[pn_col]).strip()
            if not targets or pn in targets:
                found_pns.add(pn)
                # Strip NaN / empty values to keep payload lean
                row_data = {
                    str(k): str(v)
                    for k, v in row.items()
                    if str(v).strip() not in ("", "nan", "None", "NaT", "<NA>")
                }
                found.append({
                    "policy_number": pn,
                    "sheet": sheet_name,
                    "row_number": int(idx) + 2,   # 1-based row number including header
                    "row_data": row_data,
                })

    return {
        "found": found,
        "not_found": sorted(targets - found_pns),
    }


# Tables that exist 1:1 with a policy — merge scalar (later non-null wins).
_SCALAR_TABLES = {
    "policy", "program", "contract", "tenant",
    "parametric_coverage_detail",
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


@app.get("/dwh")
def dwh_list(
    upload_id: Optional[int] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    principal: Principal = Depends(current_principal),
):
    """Fetch canonical data straight from the relational warehouse.

    Each item is one policy reassembled by joining the canonical tables:
        policy (scalar) ← program (scalar parent)
        + insured_location[] + building[] + coverage[] + premium_transaction[]
        + claim[] + party_role_in_policy[] + …

    Filter by `upload_id` to scope to a specific /bdx/upload call. When
    `upload_id` is supplied, ALL policies for that upload are returned.
    """
    # Look up the canonical policy_ids in the LOCAL ops DB (or in Postgres if
    # no upload_id was supplied).
    if upload_id is not None:
        with SessionLocal() as s:
            # Scope to the caller's tenant: the upload must belong to them (platform
            # admin bypasses) before we hand back its canonical policies.
            u = s.get(Upload, upload_id)
            if not u:
                raise HTTPException(404, "upload not found")
            assert_tenant_owns(principal, u.tenant_id)
            rows = s.execute(
                select(UploadPolicy.policy_id)
                .where(UploadPolicy.upload_id == upload_id)
                .order_by(UploadPolicy.id.asc())
            ).fetchall()
            policy_ids = [r[0] for r in rows]
    else:
        # No upload_id → list recent canonical policies, SCOPED to the caller's
        # tenant (platform admin sees all tenants). The canonical policy table
        # carries tenant_id, so we can filter directly.
        from canonical import CANONICAL_TABLES
        pol = CANONICAL_TABLES["policy"]
        cap = limit if limit is not None else 100
        with CanonicalSession() as cs:
            q = select(pol.c.policy_id).order_by(pol.c.policy_id.desc())
            if not principal.is_platform_admin:
                q = q.where(pol.c.tenant_id == principal.tenant_id)
            rows = cs.execute(q.offset(offset).limit(cap)).fetchall()
            policy_ids = [r[0] for r in rows]

    # Reassemble policies from REMOTE Postgres.
    with CanonicalSession() as cs:
        results = fetch_policies(cs, policy_ids)
    if upload_id is not None and limit is not None:
        end = offset + limit
        results = results[offset:end]
    elif upload_id is not None:
        results = results[offset:]
    return results


# --- Export: user-defined output BDX templates -----------------------------

def _active_contract_id_for_template(session, template_id: int) -> Optional[int]:
    """Resolve the active contract linked to an output template via the new
    hierarchy (Contract.output_template_id == template_id, status='active')."""
    # Only the id is wanted — select that column, not the whole contract row
    # (which carries the extracted-clauses and field-mapping JSON).
    return (
        session.query(Contract.id)
        .filter(Contract.output_template_id == template_id,
                Contract.status == "active")
        .order_by(Contract.id.desc())
        .limit(1)
        .scalar()
    )


def _contract_id_for_template(session, t: ExportTemplate) -> Optional[int]:
    """Best contract id to show for a template. Prefer the *active* linked
    contract; else the latest contract linked to this template regardless of
    status (e.g. drafted/superseded — so it isn't shown as null while a contract
    clearly exists); else the legacy ExportTemplate.contract_id column."""
    active = _active_contract_id_for_template(session, t.id)
    if active:
        return active
    latest = (
        session.query(Contract.id)
        .filter(Contract.output_template_id == t.id)
        .order_by(Contract.id.desc())
        .limit(1)
        .scalar()
    )
    if latest:
        return latest
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


def _structure_summaries(session, tenant_id) -> dict[int, dict]:
    """{template_id: structure-without-per-sheet-`columns`} for one tenant, in
    a single query.

    A template `structure` is the whole output layout, and its per-sheet
    `columns` lists are ~99% of the bytes (half a MB for an 11-sheet template).
    The listing endpoints only render sheet names, so `columns` is dropped in
    SQL — the rows never leave the database. Everything else is preserved
    verbatim, so callers reading `structure.sheets[].sheet_name` (or any other
    sheet-level key) see exactly what they saw before.
    """
    rows = session.execute(text("""
        SELECT id,
               COALESCE(
                 (structure::jsonb - 'sheets') || jsonb_build_object('sheets', COALESCE((
                     SELECT jsonb_agg(sh - 'columns' ORDER BY ord)
                     FROM jsonb_array_elements(structure::jsonb -> 'sheets')
                          WITH ORDINALITY AS x(sh, ord)
                 ), '[]'::jsonb)),
                 '{}'::jsonb
               ) AS summary
        FROM export_templates
        WHERE tenant_id = :tid AND structure IS NOT NULL
    """), {"tid": tenant_id})
    return {r[0]: r[1] for r in rows}


_UNSET = object()


def _template_to_dict(t: ExportTemplate, session=None, structure=_UNSET) -> dict:
    """Pass `structure=` to serialize a pre-fetched (e.g. summarized) structure
    instead of reading `t.structure`, which is a load-on-access column."""
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
        "output_format": getattr(t, "output_format", None) or "xlsx",
        "structure": t.structure if structure is _UNSET else structure,
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


@app.get("/export/template/{template_id}/fields")
def export_template_fields(template_id: int,
                           principal: Principal = Depends(current_principal)):
    """Return the flat field list for an output template.
    Used by the contract upload UI and extraction service to scope LLM
    rule generation to the output template's field vocabulary."""
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        return {
            "template_id": t.id,
            "name": t.name,
            "fields": _extract_template_fields(t.structure or {}),
        }


@app.get("/export/template/{template_id}/contract-mapping")
def export_template_contract_mapping(template_id: int,
                                     principal: Principal = Depends(current_principal)):
    """Return the active contract for this output template plus its validation
    rules grouped by output template field name.

    Shape:
      {
        contract: { id, filename, status } | null,
        field_rules: {
          "<output_field_name>": [
            { rule_id, rule_engine, rule_name, rule_description, severity,
              source_clause, error_message, rule_spec }
          ]
        }
      }
    """
    with SessionLocal() as s:
        # Find the active contract whose output_template_id == template_id
        active_contract = (
            s.query(Contract)
            .filter(
                Contract.output_template_id == template_id,
                Contract.status == "active",
            )
            .order_by(Contract.id.desc())
            .first()
        )
        if not active_contract:
            return {"contract": None, "field_rules": {}}
        assert_tenant_owns(principal, active_contract.tenant_id)

        contract_info = {
            "id": active_contract.id,
            "filename": active_contract.filename,
            "status": active_contract.status,
            "output_template_id": active_contract.output_template_id,
        }
        contract_id = active_contract.id

    # Load validation rules from the canonical DB, grouped by output field name
    field_rules: dict = {}
    try:
        with CanonicalSession() as cs:
            rows = cs.execute(
                text(
                    "SELECT rule_id, rule_engine, rule_name, rule_description, "
                    "severity, canonical_target, rule_spec, error_message, "
                    "source_verbatim_text, generation_confidence "
                    "FROM validation_rule "
                    "WHERE contract_id = :cid AND rule_status != 'disabled' "
                    "ORDER BY severity, rule_name"
                ),
                {"cid": contract_id},
            ).mappings().all()

            import json as _json

            for row in rows:
                # Resolve the output field name from canonical_target
                target = row["canonical_target"]
                if isinstance(target, str):
                    try:
                        target = _json.loads(target)
                    except Exception:
                        target = {}
                spec = row["rule_spec"]
                if isinstance(spec, str):
                    try:
                        spec = _json.loads(spec)
                    except Exception:
                        spec = {}

                field_name = (
                    (target or {}).get("output_field")
                    or (target or {}).get("field")
                    or (spec or {}).get("field")
                    or "__unresolved__"
                )

                entry = {
                    "rule_id": row["rule_id"],
                    "rule_engine": row["rule_engine"],
                    "rule_name": row["rule_name"],
                    "rule_description": row["rule_description"],
                    "severity": row["severity"],
                    "source_clause": row["source_verbatim_text"],
                    "error_message": row["error_message"],
                    "rule_spec": spec,
                    "generation_confidence": float(row["generation_confidence"] or 0),
                }
                field_rules.setdefault(field_name, []).append(entry)
    except Exception:
        field_rules = {}

    return {"contract": contract_info, "field_rules": field_rules}


@app.post("/export/template/{template_id}/build-rules")
def export_template_build_rules(template_id: int,
                                principal: Principal = Depends(current_principal)):
    """Pre-compile the active contract's rules into DuckDB SQL (LLM + guard +
    retry), without needing data. Warms the cache and tells the UI which rules
    can run and which "cannot be processed" — before the user generates output.
    """
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        structure = t.structure or {}
        active = (
            s.query(Contract)
            .filter(Contract.output_template_id == template_id,
                    Contract.status == "active")
            .order_by(Contract.id.desc())
            .first()
        )
        if not active:
            return {"contract": None, "rules_ok": 0, "unprocessable": [],
                    "message": "No active contract linked to this template."}
        contract_info = {"id": active.id, "filename": active.filename}

    # Rules from the canonical DB
    with CanonicalSession() as cs:
        rule_rows = cs.execute(
            text(
                "SELECT rule_id, rule_engine, rule_name, rule_description, "
                "severity, canonical_target, rule_spec, error_message "
                "FROM validation_rule "
                "WHERE contract_id = :cid AND rule_status != 'disabled'"
            ),
            {"cid": active.id},
        ).mappings().all()
        rules = [dict(r) for r in rule_rows]

    # Empty records per sheet + full column set from the template, so SQL can be
    # compiled and dry-run without any BDX data.
    schema_cols, empty_records = {}, []
    for sh in (structure.get("sheets") or []):
        sname = sh.get("sheet_name", "")
        cols = [c.get("column_name") for c in (sh.get("columns") or [])
                if c.get("column_name")]
        schema_cols[sname] = cols
        empty_records.append({"sheet": sname, "records": []})

    from duckdb_validation import run_validation as _duck_validate
    dv = _duck_validate(
        empty_records, rules, contract=contract_info,
        template_id=template_id, schema_cols=schema_cols,
    )
    try:
        _audit.log_activity(
            t.tenant_id, _audit.actor_email(principal.user_id),
            "contract.rules_built", target=f"template:{template_id}",
            details={"contract_id": contract_info["id"],
                     "rules_total": dv["stats"]["rules_total"],
                     "rules_ok": dv["stats"]["rules_ok"]})
    except Exception:  # noqa: BLE001 — auditing must never break the response
        pass
    return {
        "contract": contract_info,
        "rules_total": dv["stats"]["rules_total"],
        "rules_ok": dv["stats"]["rules_ok"],
        "unprocessable": dv["unprocessable"],
    }


@app.post("/export/template/generate")
async def export_template_generate(
    mga: str = Form(...),
    name: str = Form(...),
    file: UploadFile = File(...),
    carrier_party_id: Optional[int] = Form(default=None),
    contract_id: Optional[int] = Form(default=None),
    sheets: Optional[list[str]] = Form(default=None),
    output_format: str = Form(default="xlsx"),
    principal: Principal = Depends(current_principal),
):
    """Upload a sample output BDX. Returns a draft template (with LLM-proposed
    canonical_field per column + row_strategy per sheet) for the user to review.

    `output_format` (xlsx | csv | xml | json) sets the file format every export
    of this template produces; xlsx preserves the sample workbook's styling.

    `sheets` restricts the template to the sheets the user chose; the generated
    output will then contain only those sheets."""
    file_bytes = await file.read()
    structure = await run_in_threadpool(parse_template, file_bytes, filename=file.filename)
    if not structure["sheets"]:
        raise HTTPException(400, "workbook has no readable sheets")
    if sheets:
        keep = set(sheets)
        structure["sheets"] = [sh for sh in structure["sheets"] if sh.get("sheet_name") in keep]
        if not structure["sheets"]:
            raise HTTPException(400, "none of the selected output sheets were found in the workbook")
    await run_in_threadpool(propose_template_mapping, structure)

    # A DATA sheet stacking SEVERAL tables would be parsed as ONE table and
    # corrupt the template structure the setup is built on — refuse it, same as
    # the input-side capture (/direct/upload) and processing (/direct/run).
    # Runs AFTER the sheet-role classification above so reference/lookup tabs
    # (and the spec sheets parse_template already dropped) are exempt: those
    # tabs are freeform by design, only data sheets are read as tables.
    from mapper import detect_multiple_tables
    from direct_routes import _multi_table_error
    from exporter import is_reference_sheet
    _data_sheets = [sh.get("sheet_name") for sh in structure["sheets"]
                    if not is_reference_sheet(sh)]
    _multi = await run_in_threadpool(
        detect_multiple_tables, file_bytes, 0, _data_sheets)
    if _multi:
        raise HTTPException(400, _multi_table_error(_multi))

    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        # Versioning: a new sample under an existing (tenant, name) is the next
        # version; the first version of a brand-new template auto-activates.
        # Only the highest existing version number is needed — read that column
        # alone instead of every sibling row (each carries a `structure` JSON).
        versions = [v for (v,) in s.query(ExportTemplate.version)
                    .filter(ExportTemplate.tenant_id == tid,
                            ExportTemplate.name == name)]
        version = (max((v or 1) for v in versions) + 1) if versions else 1
        is_active = 0 if versions else 1

        # Resolve carrier name for display if a party id was supplied.
        carrier_name = None
        if carrier_party_id is not None:
            p = s.get(Party, carrier_party_id)
            if p:
                carrier_name = p.legal_name

        tmpl_ref, tmpl_bytes = await run_in_threadpool(
            storage.store_or_keep, "templates", tid, file.filename, file_bytes,
        )
        from output_serializers import normalize_format as _norm_fmt
        t = ExportTemplate(
            tenant_id=tid, name=name, version=version, is_active=is_active,
            carrier=carrier_name,
            carrier_party_id=carrier_party_id, contract_id=contract_id,
            structure=structure, template_blob=tmpl_bytes,
            template_blob_ref=tmpl_ref, approved=0,
            output_format=_norm_fmt(output_format),
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        try:
            from audit import log_activity, actor_email
            log_activity(t.tenant_id, actor_email(principal.user_id),
                         "output_template_generated", target=f"template:{t.name}",
                         details={"template_id": t.id, "name": t.name,
                                  "version": t.version, "carrier_party_id": t.carrier_party_id})
        except Exception:  # noqa: BLE001 — auditing must never break the response
            pass
        return _template_to_dict(t, s)


class UpdateExportTemplateBody(BaseModel):
    structure: Optional[dict[str, Any]] = None
    name: Optional[str] = None
    carrier: Optional[str] = None
    carrier_party_id: Optional[int] = None
    contract_id: Optional[int] = None
    approved: Optional[bool] = None
    output_format: Optional[str] = None


@app.put("/export/template/{template_id}")
def export_template_update(template_id: int, body: UpdateExportTemplateBody,
                           principal: Principal = Depends(current_principal)):
    """User submits corrected structure (column→canonical_field, row_strategy,
    static_value, transform) and optionally approves."""
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        if body.structure is not None:
            t.structure = body.structure
        if body.name is not None:
            t.name = body.name
        if body.carrier is not None:
            t.carrier = body.carrier
        if body.carrier_party_id is not None:
            t.carrier_party_id = body.carrier_party_id
        if body.contract_id is not None:
            t.contract_id = body.contract_id
        if body.output_format is not None:
            from output_serializers import normalize_format as _norm_fmt
            t.output_format = _norm_fmt(body.output_format)
        if body.approved is not None:
            t.approved = 1 if body.approved else 0
            # Activating (Save & activate) makes this the live output version.
            if body.approved:
                _activate_template(s, t)
        s.commit()
        s.refresh(t)
        try:
            from audit import log_activity, actor_email
            log_activity(t.tenant_id, actor_email(principal.user_id),
                         "output_template_updated", target=f"template:{t.name}",
                         details={"template_id": t.id, "name": t.name,
                                  "approved": bool(t.approved),
                                  "changed": sorted(body.model_dump(exclude_unset=True).keys())})
        except Exception:  # noqa: BLE001 — auditing must never break the response
            pass
        return _template_to_dict(t, s)


@app.get("/export/template")
def export_template_list(mga: Optional[str] = None,
                         principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        q = s.query(ExportTemplate).filter(ExportTemplate.tenant_id == tid)
        summaries = _structure_summaries(s, tid)
        return [_template_to_dict(t, s, structure=summaries.get(t.id))
                for t in q.order_by(ExportTemplate.id.desc()).all()]


@app.get("/export/templates")
def export_templates_grouped(mga: Optional[str] = None,
                             principal: Principal = Depends(current_principal)):
    """Output templates grouped into versions, newest first."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        q = s.query(ExportTemplate).filter(ExportTemplate.tenant_id == tid)
        summaries = _structure_summaries(s, tid)
        return _group_versions([_template_to_dict(t, s, structure=summaries.get(t.id))
                                for t in q.all()])


@app.post("/export/template/{template_id}/activate")
def export_template_activate(template_id: int,
                             principal: Principal = Depends(current_principal)):
    """Make this template version the active one used to generate output."""
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        _activate_template(s, t)
        s.commit()
        s.refresh(t)
        try:
            from audit import log_activity, actor_email
            log_activity(t.tenant_id, actor_email(principal.user_id),
                         "output_template_activated", target=f"template:{template_id}",
                         details={"template_id": template_id, "name": t.name})
        except Exception:  # noqa: BLE001
            pass
        return _template_to_dict(t, s)


@app.get("/export/template/{template_id}")
def export_template_get(template_id: int,
                        principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        return _template_to_dict(t, s)


@app.post("/export/template/{template_id}/refresh")
def export_template_refresh(template_id: int,
                            principal: Principal = Depends(current_principal)):
    """Re-run the AI mapping proposer against the template's stored sample
    workbook, populating the per-column `candidates` list (and the top
    canonical_field assignment). Used to retrofit templates created before
    the ranked-candidates feature shipped — no re-upload needed.

    Robustness:
      - User overrides (transform, static_value) are always preserved.
      - Existing canonical_field / confidence / candidates are preserved
        for any column the LLM doesn't update (e.g. when Gemini errors out
        or returns a partial response), so a 503 doesn't wipe your spec.
    """
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        tpl_bytes = storage.resolve_bytes(t.template_blob_ref, t.template_blob)
        if not tpl_bytes:
            raise HTTPException(
                400,
                "this template has no stored sample workbook; "
                "re-upload via /export/template/generate to refresh",
            )

        old_struct = t.structure or {}
        new_struct = parse_template(tpl_bytes)

        # Index everything from the existing template so we can fall back
        # gracefully if the LLM call fails partway through.
        existing: dict[tuple[str, int], dict] = {}
        for sh in (old_struct.get("sheets") or []):
            for col in (sh.get("columns") or []):
                existing[(sh.get("sheet_name"), col.get("column_index"))] = col
        old_row_strategy = {
            sh.get("sheet_name"): sh.get("row_strategy")
            for sh in (old_struct.get("sheets") or [])
        }

        propose_template_mapping(new_struct)

        any_llm_hit = False
        for sh in new_struct["sheets"]:
            # Restore row_strategy if the LLM didn't set one but we had one.
            if (not sh.get("row_strategy") or sh.get("row_strategy") == "policy") \
                    and old_row_strategy.get(sh["sheet_name"]):
                sh["row_strategy"] = old_row_strategy[sh["sheet_name"]]

            for col in sh["columns"]:
                key = (sh["sheet_name"], col["column_index"])
                prev = existing.get(key) or {}

                # 1. Always preserve user-set transform / static_value.
                if prev.get("transform") is not None:
                    col["transform"] = prev["transform"]
                if prev.get("static_value") is not None:
                    col["static_value"] = prev["static_value"]

                # 2. If the LLM populated this column, use that.
                #    Otherwise fall back to whatever was there before.
                if col.get("candidates"):
                    any_llm_hit = True
                else:
                    if prev.get("canonical_field"):
                        col["canonical_field"] = prev["canonical_field"]
                        col["confidence"] = prev.get("confidence") or 0.0
                    if prev.get("candidates"):
                        col["candidates"] = prev["candidates"]

        t.structure = new_struct
        s.commit()
        s.refresh(t)

        if not any_llm_hit:
            # LLM call failed end-to-end. Tell the caller so the UI can
            # surface "service unavailable, try again later" instead of
            # silently swapping to a half-empty spec.
            raise HTTPException(
                503,
                "AI mapping service is unavailable right now. Your existing "
                "mappings have been preserved — please try again in a minute.",
            )
        try:
            from audit import log_activity, actor_email
            log_activity(t.tenant_id, actor_email(principal.user_id),
                         "output_template_refreshed", target=f"template:{template_id}",
                         details={"template_id": template_id,
                                  "sheets": len(new_struct.get("sheets") or [])})
        except Exception:  # noqa: BLE001
            pass
        return _template_to_dict(t, s)


def _output_to_grid(blob: bytes, filename: str, max_rows: int = 500,
                    with_marks: bool = False,
                    header_row_by_sheet: dict | None = None) -> list[dict]:
    """Read a generated output file back into per-sheet grids for in-site viewing.

    xlsx keeps its rich (styled, mark-aware) path; CSV/ZIP/XML/JSON are parsed
    back into the same ``[{sheet, rows:[[...]]}]`` shape (no validation marks —
    only the highlighted xlsx carries those).

    `header_row_by_sheet` ({sheet name: 0-based row index}) overrides which row
    is the header for a template whose header isn't on row 1 — e.g. it reused a
    source workbook with a leading annotation row. Defaults to row 1 for every
    sheet not named, so every other export is unaffected."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xls")):
        return _xlsx_to_grid(blob, max_rows=max_rows, with_marks=with_marks,
                             header_row_by_sheet=header_row_by_sheet)
    try:
        if name.endswith(".zip"):
            return _zip_csv_to_grid(blob, max_rows)
        if name.endswith(".csv"):
            return _csv_to_grid(blob, max_rows, sheet="Sheet1")
        if name.endswith(".json"):
            return _json_to_grid(blob, max_rows)
        if name.endswith(".xml"):
            return _xml_to_grid(blob, max_rows)
    except Exception as e:  # noqa: BLE001 — viewing must never 500
        log.warning("could not build grid for %s: %s", filename, e)
        return []
    # Unknown extension: best effort as xlsx (legacy blobs), else empty.
    try:
        return _xlsx_to_grid(blob, max_rows=max_rows, with_marks=with_marks,
                             header_row_by_sheet=header_row_by_sheet)
    except Exception:
        return []


def _rows_to_grid(header: list, records: list[dict], max_rows: int) -> list[list]:
    grid: list[list] = [[str(h) for h in header]]
    for rec in records:
        grid.append(["" if rec.get(h) is None else rec.get(h) for h in header])
        if len(grid) >= max_rows:
            break
    return grid


def _csv_to_grid(blob: bytes, max_rows: int, sheet: str) -> list[dict]:
    import csv as _csv
    import io as _io
    text = blob.decode("utf-8", errors="replace")
    rows: list[list] = []
    for r in _csv.reader(_io.StringIO(text)):
        rows.append(r)
        if len(rows) >= max_rows:
            break
    return [{"sheet": sheet, "rows": rows}]


def _zip_csv_to_grid(blob: bytes, max_rows: int) -> list[dict]:
    import io as _io
    import zipfile as _zip
    out: list[dict] = []
    with _zip.ZipFile(_io.BytesIO(blob)) as zf:
        for member in zf.namelist():
            if not member.lower().endswith(".csv"):
                continue
            sheet = member[:-4]
            out.extend(_csv_to_grid(zf.read(member), max_rows, sheet=sheet))
    return out


def _json_to_grid(blob: bytes, max_rows: int) -> list[dict]:
    import json as _json
    obj = _json.loads(blob.decode("utf-8", errors="replace"))
    if isinstance(obj, list):
        obj = {"Sheet1": obj}
    out: list[dict] = []
    for sheet, records in (obj or {}).items():
        records = records or []
        header: list = []
        for rec in records:
            for k in rec.keys():
                if k not in header:
                    header.append(k)
        out.append({"sheet": str(sheet), "rows": _rows_to_grid(header, records, max_rows)})
    return out


def _xml_to_grid(blob: bytes, max_rows: int) -> list[dict]:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(blob.decode("utf-8", errors="replace"))
    out: list[dict] = []
    sheets = root.findall("sheet") or [root]
    for sh in sheets:
        header: list = []
        records: list[dict] = []
        for row in sh.findall("row"):
            rec: dict = {}
            for cell in row.findall("cell"):
                key = cell.get("name") or cell.tag
                rec[key] = cell.text or ""
                if key not in header:
                    header.append(key)
            records.append(rec)
        out.append({"sheet": sh.get("name") or "Sheet1",
                    "rows": _rows_to_grid(header, records, max_rows)})
    return out


def _flag_fill_rgbs() -> tuple:
    """(critical, warning) highlight RGBs, resolved from the exporter that paints
    them so the in-site grids and the downloaded workbook always agree."""
    try:
        from exporter import _INVALID_FILL_RGB as _inv, _WARNING_FILL_RGB as _warn
    except Exception:
        _inv, _warn = "FFC7CE", "FFE0B2"
    return _inv.upper(), _warn.upper()


def _cell_flag_kind(cell, inv: str, warn_rgb: str):
    """'crit' / 'warn' for a cell painted in a flag colour, else None."""
    fill = getattr(cell, "fill", None)
    ftype = (getattr(fill, "patternType", None)
             or getattr(fill, "fill_type", None)) if fill else None
    if ftype != "solid":
        return None
    rgb = getattr(getattr(fill, "fgColor", None), "rgb", None)
    rgb = rgb.upper() if isinstance(rgb, str) else ""
    if rgb.endswith(warn_rgb):
        return "warn"
    return "crit" if rgb.endswith(inv) else None


def _xlsx_to_grid(blob: bytes, max_rows: int = 500, with_marks: bool = False,
                  header_row_by_sheet: dict | None = None) -> list[dict]:
    """Read a generated xlsx back into a per-sheet cell grid for in-site viewing.

    When ``with_marks`` is set, also surface the light-red "failed validation"
    cells the downloaded workbook already carries (the Excel Light Red Fill that
    ``exporter.highlight_exceptions`` paints) so the in-site preview can show the
    SAME highlighting as the download — read straight from the stored, already-
    highlighted blob, with no re-validation. Per sheet it adds:
      * ``marks``: 0-based ``[row, col]`` pairs into ``rows`` for flagged cells;
      * ``warn_marks``: the subset of those painted the non-critical (light
        orange) colour, so the grid can tint them exactly as the file does;
      * ``notes``: ``{r, c, text}`` for that cell's validation comment, if any.
    The default (values-only) path is unchanged so existing callers are untouched.

    ``header_row_by_sheet`` ({sheet name: 0-based row index}) skips any rows
    BEFORE a sheet's real header (pre-header noise, e.g. a leading annotation
    row a reused template inherited from its source file) so ``rows[0]`` is
    always the true header, matching what every caller already assumes.
    Defaults to row 0 for an unnamed sheet, so every other export is unaffected.
    """
    import io
    import openpyxl
    out: list[dict] = []
    if not with_marks:
        wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                skip = (header_row_by_sheet or {}).get(ws.title, 0)
                rows: list[list] = []
                for ri, r in enumerate(ws.iter_rows(values_only=True)):
                    if ri < skip:
                        continue
                    rows.append(["" if c is None else c for c in r])
                    if len(rows) >= max_rows:
                        break
                out.append({"sheet": ws.title, "rows": rows})
        finally:
            wb.close()
        return out
    # Styled read: per-cell fills/comments aren't available in read_only mode, so
    # load fully. Match ONLY the specific flag RGBs (not any solid fill), so a
    # template's own coloured headers are never mistaken for a validation flag.
    inv, warn_rgb = _flag_fill_rgbs()
    wb = openpyxl.load_workbook(io.BytesIO(blob), data_only=True)
    try:
        for ws in wb.worksheets:
            skip = (header_row_by_sheet or {}).get(ws.title, 0)
            rows: list[list] = []
            marks: list[list] = []
            warn_marks: list[list] = []
            notes: list[dict] = []
            kept = 0
            for ri, row in enumerate(ws.iter_rows()):
                if ri < skip:
                    continue
                if kept >= max_rows:
                    break
                kept += 1
                gi = ri - skip
                vals: list = []
                for ci, cell in enumerate(row):
                    vals.append("" if cell.value is None else cell.value)
                    kind = _cell_flag_kind(cell, inv, warn_rgb)
                    if kind:
                        marks.append([gi, ci])
                        if kind == "warn":
                            warn_marks.append([gi, ci])
                        cmt = getattr(cell, "comment", None)
                        if cmt is not None and getattr(cmt, "text", None):
                            notes.append({"r": gi, "c": ci, "text": str(cmt.text)})
                rows.append(vals)
            out.append({"sheet": ws.title, "rows": rows, "marks": marks,
                        "warn_marks": warn_marks, "notes": notes})
    finally:
        wb.close()
    return out


def _xlsx_to_grid_page(blob: bytes, sheet_name: str | None = None, offset: int = 0,
                        limit: int | None = None, row_indices: set | None = None,
                        with_marks: bool = False,
                        header_row_by_sheet: dict | None = None) -> list[dict]:
    """Windowed read for the BDX Review infinite-scroll grid.

    Every sheet reports its header and `total_rows` (cheap — first row plus the
    sheet's own dimensions, no per-cell scan). Only the ONE sheet named
    `sheet_name` (or the workbook's first sheet, if that name doesn't match)
    additionally gets real data:
      * `marks`/`notes` for the WHOLE sheet — Approve-all and the pending/
        resolved counts operate over every row, not just the ones currently on
        screen, so these always cover the full sheet regardless of the window
        requested (same fill-color scan `_xlsx_to_grid` already pays for
        `marks=1`, just without also collecting every row's values);
      * `rows`: the header plus ONLY the requested data-row window — either a
        contiguous `[offset, offset + limit)` range, or the exact rows named in
        `row_indices` (1-based, header excluded) — with `row_gis` giving each
        entry's data row number so the caller can place it correctly even
        when the window isn't contiguous.

    `header_row_by_sheet` ({sheet name: 0-based row index}) names which physical
    row is a sheet's real header (default 0), skipping pre-header noise rows;
    windows/`row_gis`/marks stay in DATA row numbers (1 = first row after the
    header) exactly as before, so existing callers see identical semantics.
    """
    import io
    import openpyxl
    inv = warn_rgb = None
    if with_marks:
        inv, warn_rgb = _flag_fill_rgbs()

    wb = openpyxl.load_workbook(io.BytesIO(blob), data_only=True)
    try:
        names = [ws.title for ws in wb.worksheets]
        target = sheet_name if sheet_name in names else (names[0] if names else None)
        want = (lambda gi: gi in row_indices) if row_indices is not None else \
               (lambda gi: offset < gi <= offset + (limit or 0))

        out: list[dict] = []
        for ws in wb.worksheets:
            hdr_ri = (header_row_by_sheet or {}).get(ws.title, 0)
            total_rows = max(0, (ws.max_row or 0) - 1 - hdr_ri)

            if ws.title != target:
                first = next(ws.iter_rows(min_row=hdr_ri + 1, max_row=hdr_ri + 1), None)
                header = ["" if c.value is None else c.value for c in first] if first else []
                out.append({"sheet": ws.title, "rows": [header], "marks": [],
                            "warn_marks": [], "notes": [], "total_rows": total_rows})
                continue

            header: list = []
            rows_by_gi: dict[int, list] = {}
            marks: list[list] = []
            warn_marks: list[list] = []
            notes: list[dict] = []
            for ri, row in enumerate(ws.iter_rows()):
                if ri < hdr_ri:
                    continue          # pre-header noise row — not header, not data
                gi = ri - hdr_ri      # 0 = header, 1.. = data row number
                need_vals = gi == 0 or want(gi)
                vals = [] if need_vals else None
                for ci, cell in enumerate(row):
                    if vals is not None:
                        vals.append("" if cell.value is None else cell.value)
                    if with_marks:
                        kind = _cell_flag_kind(cell, inv, warn_rgb)
                        if kind:
                            marks.append([gi, ci])
                            if kind == "warn":
                                warn_marks.append([gi, ci])
                            cmt = getattr(cell, "comment", None)
                            if cmt is not None and getattr(cmt, "text", None):
                                notes.append({"r": gi, "c": ci, "text": str(cmt.text)})
                if gi == 0:
                    header = vals or []
                elif vals is not None:
                    rows_by_gi[gi] = vals

            order = sorted(rows_by_gi.keys())
            out.append({
                "sheet": ws.title,
                "rows": [header] + [rows_by_gi[gi] for gi in order],
                "row_gis": order,
                "marks": marks, "warn_marks": warn_marks,
                "notes": notes, "total_rows": total_rows,
            })
        return out
    finally:
        wb.close()


# Rows one streamed request may deliver, and rows per NDJSON line within it.
#
# The window is what keeps a big sheet affordable. Streaming a whole sheet down
# one response sounds like chunking but isn't bounded by anything: a 10,368-row
# x 77-column sheet is ~9.5 MB of JSON, and the client must encode, parse and
# re-render every byte of it before the tab settles — while only ~60 rows are
# ever on screen. So a request carries the window the grid is about to SHOW
# (plus a little prefetch) and the grid comes back for more as the user scrolls.
# Cost then tracks what is looked at, not how big the sheet happens to be.
#
# The chunk size is the second, smaller step: it splits that window into a few
# lines so the page still fills in visibly instead of landing in one lump. Chunk
# COUNT is what has to stay bounded — every chunk costs a JSON encode, a write
# and a client re-render — which is why this is a flat size against a bounded
# window rather than a fraction of the whole sheet.
_STREAM_PAGE_ROWS = 600
_STREAM_CHUNK_ROWS = 200
_STREAM_MAX_PAGE_ROWS = 5000


def _json_default(o):
    """JSON fallback for the cell values openpyxl hands back.

    `jsonable_encoder` handles these too, but it walks every value through a
    recursive type dispatch: on a 260x77 chunk that is ~20k dispatches, and it
    measured 3x the cost of `json.dumps` with this hook (0.93s vs 0.35s over a
    sheet's worth of chunks) for an identical result. The hook is only consulted
    for values `json` can't already write, so the common str/int/float path
    doesn't pay for it at all.
    """
    if isinstance(o, (datetime, date, time)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", "replace")
    return str(o)


def _export_file_row(s, export_id: int):
    """Load an export row with only the columns needed to SERVE its file.

    `output_exports.exceptions` is a JSON document that grows with the run: on a
    large bordereau it reaches tens of megabytes, and a plain `s.get()` fetches
    AND parses the whole of it even when the caller only wants the xlsx bytes.
    That cost is paid before the endpoint can emit anything — which on the
    streamed grid meant several seconds of dead air per sheet, repeated for
    every tab the user opened, entirely on data none of these endpoints read.

    Columns are limited rather than deferred so this stays a single round trip.
    Nothing else changes: the return value is still an OutputExport, and the
    unloaded attributes would still load on demand if a caller ever touched one.
    """
    return s.get(OutputExport, export_id, options=[load_only(
        OutputExport.tenant_id,
        OutputExport.filename,
        OutputExport.blob,
        OutputExport.blob_ref,
        OutputExport.template_id,
    )])


def _export_meta_row(s, export_id: int):
    """An export row WITHOUT its bytes — for callers that may not need them."""
    return s.get(OutputExport, export_id, options=[load_only(
        OutputExport.tenant_id,
        OutputExport.filename,
        OutputExport.blob_ref,
        OutputExport.template_id,
    )])


def _export_blob_digest(s, export_id: int) -> "str | None":
    """The stored xlsx's content digest, computed BY THE DATABASE.

    Same value as `grid_cache.fingerprint()` over the bytes, for a few hundred
    microseconds of network instead of several megabytes — which is what lets a
    request that will hit the parse cache avoid fetching the file at all. None
    when the bytes aren't in the column (blob storage, or no file), leaving the
    caller to fall back to digesting what it fetches.
    """
    try:
        return s.execute(
            select(func.md5(OutputExport.blob)).where(OutputExport.id == export_id)
        ).scalar()
    except Exception:  # noqa: BLE001 — any dialect that can't do this just pays the fetch
        return None


def _export_blob_bytes(export_id: int) -> "bytes | None":
    """Fetch an export's bytes in a session of their own — called lazily, from
    inside a streaming response, after the request's own session has closed."""
    with SessionLocal() as s:
        r = s.get(OutputExport, export_id, options=[load_only(
            OutputExport.blob, OutputExport.blob_ref)])
        return storage.resolve_bytes(r.blob_ref, r.blob) if r else None


def _export_data_sheet_names(s, rec) -> set:
    """Normalized names of an export's DATA sheets — the ones the setup classified
    role='data' (the validated policy sheets), as opposed to the template's static
    spec / instruction sheets that also physically live in the generated file.
    Lets the in-site preview point at the actual data instead of a documentation
    sheet. Best-effort: an empty set (→ caller leaves every sheet as-is) whenever
    there's no structure to classify from, so nothing regresses for legacy files.
    """
    def _n(x):
        return " ".join(str(x).split()).strip().lower()
    try:
        # `structure` only: the row also carries the template workbook itself
        # (several MB), and loading that to read a list of sheet names costs
        # more than everything else this endpoint does.
        tpl = s.get(ExportTemplate, rec.template_id,
                    options=[load_only(ExportTemplate.structure)]) if rec.template_id else None
        st = (getattr(tpl, "structure", None) or {}) if tpl else {}
        out: set = set()
        for sh in st.get("sheets") or []:
            role = sh.get("sheet_role")
            gen = sh.get("rule_generatable")
            if role == "data" or (role != "reference" and gen):
                nm = sh.get("sheet_name")
                if nm:
                    out.add(_n(nm))
        return out
    except Exception:
        return set()


def _export_header_rows(s, rec) -> dict:
    """{sheet name: 0-based header row} for an export's template, listing ONLY
    sheets whose header is NOT on the file's first row (a template that reused a
    source workbook with a leading annotation/blank row keeps its header where
    the source had it, e.g. row 2). The grid readers treat an unlisted sheet as
    header-on-row-1, so the common case costs nothing and legacy exports are
    unaffected. Best-effort: {} on any failure, restoring today's behavior."""
    try:
        tpl = s.get(ExportTemplate, rec.template_id,
                    options=[load_only(ExportTemplate.structure)]) if rec.template_id else None
        st = (getattr(tpl, "structure", None) or {}) if tpl else {}
        out: dict = {}
        for sh in st.get("sheets") or []:
            nm = sh.get("sheet_name")
            hr = sh.get("header_row")
            if nm and isinstance(hr, int) and hr > 0:
                out[str(nm).strip()] = hr
        return out
    except Exception:
        return {}


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
                text("SELECT rule_id, rule_spec, generation_confidence, "
                     "source_verbatim_text, source_page_number, rule_description "
                     "FROM validation_rule WHERE rule_id = ANY(:ids)"),
                {"ids": rule_ids},
            ).mappings().all()
        spec_by_id = {row["rule_id"]: row["rule_spec"] for row in rows}
        # Stored description — the fallback wording for legacy (pre-IR) rules,
        # which have no template for the explainer to read.
        desc_by_id = {row["rule_id"]: row["rule_description"] for row in rows}
        # Rule-generation confidence (0..1) so the review screen can show it under
        # the recommendation, same as the upload path.
        conf_by_id = {
            row["rule_id"]: (float(row["generation_confidence"])
                             if row["generation_confidence"] is not None else None)
            for row in rows
        }
        # The contract clause the rule was generated from — shown under the reason
        # in the rule header. Stored output exceptions don't carry it, so attach
        # from the rule (source_verbatim_text / source_page_number), matching the
        # upload path.
        clause_by_id = {
            row["rule_id"]: (row["source_verbatim_text"], row["source_page_number"])
            for row in rows
        }
    except Exception:
        return excs
    out = []
    for e in excs:
        if not isinstance(e, dict):
            out.append(e)
            continue
        e = dict(e)
        spec = spec_by_id.get(e.get("rule_id"))
        if e.get("confidence") is None:
            e["confidence"] = conf_by_id.get(e.get("rule_id"))
        if e.get("contract_clause_text") in (None, ""):
            _clause_text, _clause_page = clause_by_id.get(e.get("rule_id"), (None, None))
            if _clause_text:
                e["contract_clause_text"] = _clause_text
                if e.get("contract_clause_page") in (None, ""):
                    e["contract_clause_page"] = _clause_page
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
        # Format rules recommend a SHAPE, not a value — send one example of that
        # shape so the screen never has to show the reviewer a raw regex.
        if not e.get("recommendation_example"):
            try:
                ex = _example_from_ir(spec)
            except Exception:
                ex = None
            if ex:
                e["recommendation_example"] = ex.get("example")
                e["recommendation_format"] = ex.get("format")
        # Plain-English explanation of the rule, derived from the IR that
        # actually ran. Attached at READ time, so exports generated before this
        # existed gain it without being re-generated. Runs last so it can read
        # the clause text/page attached above.
        if not e.get("explanation"):
            try:
                from contract_upload_services.rule_explainer import explain_rule
                exp = explain_rule(
                    rule_spec=spec,
                    source_verbatim_text=e.get("contract_clause_text"),
                    source_page_number=e.get("contract_clause_page"),
                    contract_filename=e.get("contract_filename"),
                    rule_description=desc_by_id.get(e.get("rule_id")),
                )
                if exp:
                    e["explanation"] = exp
            except Exception:
                pass
        # A row flagged only because its cell is not a number is a different
        # failure from the rule's own check, so it is re-titled to say so —
        # otherwise it reads under a heading, an explanation and a recommended
        # value that all describe a check that never ran on it. Last, so it
        # overrides the rule-derived wording attached above.
        try:
            from contract_upload_services.rule_explainer import (
                apply_numeric_format_identity)
            apply_numeric_format_identity(e, spec)
        except Exception:
            pass
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


# kind (landing_correction) → validation_exception-style status, so the frontend
# tally + decision-restore logic works identically for both lanes.
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
            return ", ".join(str(x) for x in n["enum"])
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
    # Legacy AJV: same "schema" unwrap as _expected_from_ajv_schema (the enum may
    # live under rule_spec.schema rather than rule_spec itself) — top-level enum,
    # or the first property with an enum.
    sch = rule_spec.get("schema") if isinstance(rule_spec.get("schema"), dict) else rule_spec
    enum = sch.get("enum")
    if isinstance(enum, list) and enum:
        return [str(x) for x in enum]
    props = sch.get("properties")
    if isinstance(props, dict):
        for pv in props.values():
            if isinstance(pv, dict) and isinstance(pv.get("enum"), list) and pv["enum"]:
                return [str(x) for x in pv["enum"]]
    return None


def _ir_of(rule_spec) -> tuple:
    """(template, params) of a rule_spec, however it arrives (dict or JSON text).
    (None, {}) when the spec carries no IR — the legacy AJV shape."""
    import json as _json
    if isinstance(rule_spec, str):
        try:
            rule_spec = _json.loads(rule_spec)
        except Exception:
            return None, {}
    if not isinstance(rule_spec, dict):
        return None, {}
    ir = rule_spec.get("ir") or {}
    params = ir.get("params")
    return ir.get("template"), (params if isinstance(params, dict) else {})


def _example_from_ir(rule_spec) -> Optional[dict]:
    """An EXAMPLE for a rule that constrains the SHAPE of a value, not its content.

    A format rule ("must be 4 digits") has no recommended value to offer, so the
    review screen used to fall back to the rule's own machine wording — the raw
    regex, `matches ^\\d{4}$`. That is unreadable, and worse, it was treated as a
    value: Approve wrote it into the cell and Fix pre-filled it. This returns

        {"example": "1234", "format": "4 digits"}

    — one value of the right shape (built from the pattern and verified against
    it, never invented) plus the shape in words. The example is for DISPLAY only;
    the reviewer still supplies the real value with Fix. None when the rule's
    expectation is already a value (enums, bounds, dates) or when no example can
    be built with certainty."""
    template, params = _ir_of(rule_spec)
    if not template:
        return None
    try:
        from contract_upload_services.rule_explainer import format_hint
        hint = format_hint(template, params)
    except Exception:
        return None
    if not hint or hint.get("exact"):
        return None                      # an exact value is a recommendation, not an example
    if not (hint.get("example") or hint.get("format")):
        return None
    return {"example": hint.get("example"), "format": hint.get("format")}


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
        return f"{join(p.get('allowed'))}" if p.get("allowed") else None
    if tmpl == "value_not_in_set":
        return f"not: {join(p.get('excluded'))}" if p.get("excluded") else None
    if tmpl == "max_limit":
        return f"<= {num(p.get('max'))}" if p.get("max") is not None else None
    if tmpl == "min_limit":
        return f">= {num(p.get('min'))}" if p.get("min") is not None else None
    if tmpl == "range_check":
        lo, hi = p.get("min"), p.get("max")
        if lo is not None and hi is not None:
            if lo == hi:
                return num(lo)
            return f"between {num(lo)} and {num(hi)}"
        if hi is not None:
            return f"<= {num(hi)}"
        if lo is not None:
            return f">= {num(lo)}"
        return None
    if tmpl == "pattern_check":
        pat = p.get("pattern")
        if not pat:
            return None
        # A pattern that accepts exactly ONE string ("^441105$") names a value,
        # not a format — hand back the value so it can be recommended and
        # approved like any other. Everything else stays the machine form: it is
        # what marks the recommendation as a FORMAT (see _example_from_ir), and
        # the reviewer is shown the example instead.
        try:
            from contract_upload_services.rule_explainer import format_hint
            exact = (format_hint(tmpl, p) or {}).get("exact")
        except Exception:
            exact = None
        return exact or f"matches {pat}"
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


@app.post("/export/validate")
def export_validate(
    template_id: int = Form(...),
    upload_id: Optional[int] = Form(default=None),
    policy_ids: Optional[str] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    """Pre-generation dry run for the Outputs page.

    Resolves the output rows an export WOULD write and runs the SAME contract-rule
    validation that /export/generate runs (the DuckDB engine, which compiles each
    rule — old AJV *or* new ir_v1 — to SQL and executes it). Builds NO file and
    creates no downloadable export, BUT persists a validation_run + validation_exception
    rows for the upload so the "Modify data here" editor and the exceptions review can
    read them back via /api/validate/upload. The frontend calls this before downloading
    so it can show the "Validation exceptions found" popup. Returns exception counts
    plus a violations list already shaped for the popup (see frontend CustomViolation).
    """
    with SessionLocal() as s:
        tgt = _resolve_export_target(s, template_id, upload_id, policy_ids, principal)

    structure = tgt["structure"]
    ids = tgt["ids"]
    active_contract_id = tgt["active_contract_id"]

    empty = {"exception_count": 0, "critical": 0, "warning": 0,
             "violations": [], "unprocessable_rules": []}
    if not ids or active_contract_id is None:
        return empty

    # Active-contract rules (everything except disabled), same as generate Pass B.
    with CanonicalSession() as cs:
        rule_rows = cs.execute(
            text(
                "SELECT rule_id, rule_engine, rule_name, rule_description, "
                "severity, canonical_target, rule_spec, error_message, "
                "source_verbatim_text, source_page_number "
                "FROM validation_rule "
                "WHERE contract_id = :cid AND rule_status != 'disabled'"
            ),
            {"cid": active_contract_id},
        ).mappings().all()
        contract_validation_rules = [dict(r) for r in rule_rows]

    if not contract_validation_rules:
        return empty

    with CanonicalSession() as cs:
        policies = fetch_policies(cs, ids)

    records = build_output_records(structure, policies)
    schema_cols = {
        sh.get("sheet_name", ""): [
            c.get("column_name") for c in (sh.get("columns") or [])
            if c.get("column_name")
        ]
        for sh in (structure.get("sheets") or [])
    }

    exceptions: list[dict] = []
    unprocessable: list[dict] = []
    try:
        from duckdb_validation import run_validation as _duck_validate
        _dv = _duck_validate(
            records,
            contract_validation_rules,
            contract=tgt["active_contract_info"],
            template_id=template_id,
            schema_cols=schema_cols,
        )
        exceptions = _dv["exceptions"]
        unprocessable = _dv["unprocessable"]
        print(f"[DuckDB validate-only] {_dv['stats']}")
    except Exception as dv_exc:  # never let validation crash block the user
        import traceback as _tb
        _tb.print_exc()
        print(f"[DuckDB validate-only] ERROR — skipped: {dv_exc}")
        return empty

    # Shape each DuckDB exception into the frontend popup's CustomViolation.
    violations = [
        {
            "ruleId": e.get("rule_id"),
            "ruleName": e.get("rule_name") or e.get("code"),
            "severity": (e.get("severity") or "warning").lower(),
            "rowIndex": e.get("row"),
            "field": e.get("field") or e.get("column"),
            "actualValue": e.get("actual_value"),
            "expectedValue": None,
            "operator": None,
            "message": e.get("message") or e.get("reason"),
            "recordIdentifier": (
                {"policy_number": e.get("policy_number")}
                if e.get("policy_number") else None
            ),
            "affectedRecords": None,
            "violationDetail": None,
        }
        for e in exceptions
    ]
    critical = sum(1 for v in violations if v["severity"] == "critical")
    warning = sum(1 for v in violations if v["severity"] in ("warning", "warn"))

    # Persist a validation_run + validation_exception rows for this upload so the
    # "Modify data here" editor and the exceptions review (which read back via
    # /api/validate/upload) have data to work with. field_path is stored as the
    # OUTPUT column name — save_fields/resolve_field map it to the canonical cell at
    # save time. Best-effort: persistence must never break the popup response.
    if upload_id is not None and exceptions:
        try:
            from types import SimpleNamespace
            from validation_routes import _persist
            # policy_number -> policy_id (+ a tenant_id) for this upload's policies.
            with CanonicalSession() as cs:
                prows = cs.execute(
                    text("SELECT policy_id, policy_number, tenant_id FROM policy "
                         "WHERE policy_id = ANY(:ids) AND is_current_version IS NOT FALSE"),
                    {"ids": ids},
                ).mappings().all()
            pn_to_pid = {str(r["policy_number"]): r["policy_id"]
                         for r in prows if r["policy_number"] is not None}
            tenant_id = next((r["tenant_id"] for r in prows
                              if r["tenant_id"] is not None), None)

            # Fallback (sheet, __rowid) -> policy_id for rules whose compiled SQL
            # doesn't emit policy_number (e.g. value-in-set/range row checks): rebuild
            # rows one policy at a time, in the same order DuckDB enumerates __rowid
            # (1-based per sheet), so each output row maps back to its policy.
            rowid_to_pid: dict = {}
            sheet_counts: dict = {}
            for p in policies:
                pid = (p.get("policy") or {}).get("policy_id")
                for block in build_output_records(structure, [p]):
                    sh = block.get("sheet")
                    for _ in (block.get("records") or []):
                        i = sheet_counts.get(sh, 0) + 1
                        sheet_counts[sh] = i
                        rowid_to_pid[(sh, i)] = pid

            def _pid_for(e):
                pn = e.get("policy_number")
                if pn is not None and str(pn) in pn_to_pid:
                    return pn_to_pid[str(pn)]
                sh, rw = e.get("sheet"), e.get("row")
                if sh is not None and rw is not None:
                    try:
                        return rowid_to_pid.get((sh, int(rw)))
                    except (TypeError, ValueError):
                        return None
                return None

            # rule_id -> rule_spec, for deriving the "Expected:" constraint hint.
            rule_spec_by_id = {r.get("rule_id"): r.get("rule_spec")
                               for r in contract_validation_rules}
            items = [
                {
                    "tenant_id": tenant_id,
                    "rule_id": e.get("rule_id"),
                    "source_entity": "policy",
                    "source_entity_id": _pid_for(e),
                    "severity": (e.get("severity") or "warning").lower(),
                    "field_path": e.get("field") or e.get("column"),
                    "expected_value": _expected_from_ir(
                        rule_spec_by_id.get(e.get("rule_id"))),
                    "actual_value": e.get("actual_value"),
                    "status": "open",
                }
                for e in exceptions
            ]
            result_for_persist = {
                "exceptions": {"total": len(items), "critical": critical,
                               "warning": warning,
                               "info": len(items) - critical - warning,
                               "items": items},
                "rules": {"total": len(contract_validation_rules), "custom": 0, "ajv": 0},
                "engines": {},
                "recordCount": sum(len(b.get("records") or []) for b in records),
                "proceedToCanonical": True,
            }
            body_shim = SimpleNamespace(uploadId=upload_id, stage="output")
            with SessionLocal() as s:
                _persist(s, body_shim, tenant_id, active_contract_id, result_for_persist)
                s.commit()
        except Exception as persist_exc:  # never block the popup
            import traceback as _tb
            _tb.print_exc()
            print(f"[validate-only persist] skipped: {persist_exc}")

    return {
        "exception_count": len(violations),
        "critical": critical,
        "warning": warning,
        "violations": violations,
        "unprocessable_rules": unprocessable,
    }


@app.post("/export/generate")
def export_generate(
    template_id: int = Form(...),
    upload_id: Optional[int] = Form(default=None),
    policy_ids: Optional[str] = Form(default=None),
    filename: Optional[str] = Form(default=None),
    actor: Optional[str] = Form(default=None),
    skip_validation: bool = Form(default=False),
    principal: Principal = Depends(current_principal),
):
    """Render an xlsx using the approved template + canonical data, validate it,
    and persist it as a downloadable record. Returns JSON metadata (the bytes
    are fetched separately via /export/downloads/{id}/file).

    Provide EITHER `upload_id` (export all policies from a /bdx/upload call)
    OR `policy_ids` (comma-separated canonical policy_ids).
    """
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        structure = t.structure
        template_blob = storage.resolve_bytes(t.template_blob_ref, t.template_blob)
        template_name = t.name
        template_tid = t.tenant_id
        from output_serializers import normalize_format as _norm_fmt
        output_format = _norm_fmt(getattr(t, "output_format", None))

        # Find the active contract for this Output Template (new hierarchy).
        # The ops DB Contract row carries output_template_id and status.
        active_contract = (
            s.query(Contract)
            .filter(
                Contract.output_template_id == template_id,
                Contract.status == "active",
            )
            .order_by(Contract.id.desc())
            .first()
        )
        active_contract_id = active_contract.id if active_contract else None
        active_contract_info = (
            {"id": active_contract.id, "filename": active_contract.filename}
            if active_contract else None
        )

        # Resolve target policy_ids.
        ids: list[int] = []
        program_id: Optional[int] = None
        if upload_id is not None:
            rows = s.execute(
                select(UploadPolicy.policy_id)
                .where(UploadPolicy.upload_id == upload_id)
                .order_by(UploadPolicy.id.asc())
            ).fetchall()
            ids = [r[0] for r in rows]
            
            # Resolve canonical program_id via upload.tenant_id → programs table
            upload_obj = s.get(Upload, upload_id)
            if upload_obj:
                prog = (
                    s.query(Program)
                    .filter(Program.tenant_id == upload_obj.tenant_id)
                    .order_by(Program.id.desc())
                    .first()
                )
                # ops + canonical share the same `program` table, so the
                # program's id IS the canonical program_id.
                program_id = prog.id if prog else None

        elif policy_ids:
            try:
                ids = [int(x.strip()) for x in policy_ids.split(",") if x.strip()]
            except ValueError:
                raise HTTPException(400, "policy_ids must be comma-separated integers")
            # No single program when arbitrary policy_ids are supplied
            program_id = None

        else:
            raise HTTPException(400, "provide upload_id or policy_ids")

    # Load validation rules from the canonical DB for the active contract.
    contract_validation_rules: list[dict] = []
    if active_contract_id is not None:
        with CanonicalSession() as cs:
            rule_rows = cs.execute(
                text(
                    "SELECT rule_id, rule_engine, rule_name, rule_description, "
                    "severity, canonical_target, rule_spec, error_message, "
                    "source_verbatim_text, source_page_number "
                    "FROM validation_rule "
                    "WHERE contract_id = :cid AND rule_status != 'disabled'"
                ),
                {"cid": active_contract_id},
            ).mappings().all()
            contract_validation_rules = [dict(r) for r in rule_rows]

    with CanonicalSession() as cs:
        policies = fetch_policies(cs, ids)

    # ── Pass A: program-level BDX validation (blocking) ──────────────
    # Validates the raw policy rows against program rules and blocks the export
    # on critical violations. Best-effort: if the validator module/program is
    # unavailable it logs and continues (never silently blocks export).
    validation_summary = None
    if not skip_validation and program_id is not None:
        try:
            from bdx_validator import validate_bdx
        except ImportError:
            # Pass A uses an OPTIONAL legacy program-level validator that is not
            # bundled in this deployment. Skip it quietly — Pass B (the DuckDB
            # contract-rule validation below) is the active validator.
            validate_bdx = None
        if validate_bdx is not None:
            try:
                bdx_rows = [p if isinstance(p, dict) else dict(p) for p in policies]
                report = validate_bdx(program_id=program_id, bdx_rows=bdx_rows)
                validation_summary = report.summary()
                print(f"[Validation] complete → {validation_summary}")
                if not report.passed:
                    return JSONResponse(
                        status_code=422,
                        content={
                            "success":   False,
                            "blocked":   True,
                            "reason":    "BDX failed validation — critical violations found.",
                            "validation": validation_summary,
                        },
                    )
            except Exception as val_exc:
                print(f"[Validation] warning — could not run: {val_exc}")

    # ── Pass B: DuckDB contract-rule validation (non-blocking) ──
    # Resolve the output values, load them into an in-memory DuckDB, and run the
    # AI-compiled SQL for each active-contract rule. Compiled SQL is cached per
    # rule; a rule that cannot be compiled to a safe, runnable query (after one
    # retry) is reported as "cannot be processed" rather than silently dropped.
    records = build_output_records(structure, policies)
    # Full column set per sheet from the TEMPLATE (not the data) so the SQL
    # schema is complete and its hash is stable across runs.
    schema_cols = {
        sh.get("sheet_name", ""): [
            c.get("column_name") for c in (sh.get("columns") or [])
            if c.get("column_name")
        ]
        for sh in (structure.get("sheets") or [])
    }
    unprocessable: list[dict] = []
    try:
        from duckdb_validation import run_validation as _duck_validate
        _dv = _duck_validate(
            records,
            contract_validation_rules or [],
            contract=active_contract_info,
            template_id=template_id,
            schema_cols=schema_cols,
        )
        exceptions = _dv["exceptions"]
        unprocessable = _dv["unprocessable"]
        # Label each row-level exception with the offending policy's number (the
        # compiled SQL for value-set/range rules doesn't emit one), so the review
        # UI shows a real policy id instead of "Dataset-level".
        from duckdb_validation import label_exceptions_with_policy
        label_exceptions_with_policy(exceptions, structure, records)
        print(f"[DuckDB validation] {_dv['stats']}")
        # A rule reaches `unprocessable` here only when its created query is
        # missing or FAILS TO RUN against the data — i.e. a clause that was
        # supposed to validate but didn't. Surface each one as a highlighted
        # "not validated" notice so the user knows the clause did not work
        # (rather than silently assuming it passed).
        for u in unprocessable:
            exceptions.append({
                "severity": "warning",
                "code": u.get("rule_name") or "not_validated",
                "sheet": None, "row": None, "column": None, "field": None,
                "rule_id": u.get("rule_id"),
                "rule_name": u.get("rule_name"),
                "reason": u.get("message"),
                "message": f"This clause was NOT validated: {u.get('message')}",
                "error_class": "not_validated",
            })
    except Exception as dv_exc:
        # Never let validation failure block the export.
        import traceback as _tb
        _tb.print_exc()
        print(f"[DuckDB validation] ERROR — skipped: {dv_exc}")
        exceptions = []

    from output_serializers import (
        serialize as _serialize_output, output_extension as _output_ext,
        content_type_for_filename as _ct_for, ensure_extension as _ensure_ext,
        sheets_from_blocks as _sheets_from_blocks,
    )
    n_sheets = len(structure.get("sheets") or [])
    if output_format == "xlsx":
        output_bytes = generate_workbook(structure, policies, template_bytes=template_blob)
        # "Generate anyway": when the output still has validation exceptions, paint
        # each offending cell light-red with an explanatory comment so the problems
        # are visible in the downloaded file. No-op (and never raises) when clean.
        # (Cell highlighting is Excel-only; CSV/XML/JSON carry exceptions in the
        # OutputExport record instead.)
        if exceptions:
            from exporter import highlight_exceptions
            output_bytes = highlight_exceptions(output_bytes, structure, exceptions)
    else:
        output_bytes = _serialize_output(
            _sheets_from_blocks(structure, records), output_format)
    raw_name = filename or f"{(template_name or 'export').replace(' ', '_')}"
    # Force the extension to match the template's output format (a multi-sheet
    # CSV becomes a .zip bundle), regardless of what the user typed.
    fname = _ensure_ext(raw_name, _output_ext(output_format, n_sheets))

    # Persist the generated output to blob storage (Azure/Azurite) when
    # enabled; otherwise keep the bytes inline in `blob` (legacy behaviour).
    export_blob_ref, export_blob_bytes = storage.store_or_keep(
        "exports", template_tid, fname, output_bytes,
        content_type=_ct_for(fname),
    )

    sev_crit, sev_warn, sev_info = exception_severity_counts(exceptions)
    with SessionLocal() as s:
        rec = OutputExport(
            tenant_id=template_tid, template_id=template_id, template_name=template_name,
            filename=fname, source_upload_id=upload_id, policy_ids=ids,
            generated_by=actor, policy_count=len(policies),
            exception_count=len(exceptions), exceptions=exceptions,
            critical_count=sev_crit, warning_count=sev_warn, info_count=sev_info,
            status="has_exceptions" if exceptions else "clean",
            blob=export_blob_bytes,
            blob_ref=export_blob_ref,
        )
        s.add(rec)
        s.add(ActivityEvent(
            tenant_id=template_tid, actor=actor, action="output_generated",
            target=f"export:{template_name}",
            details={"filename": fname, "policies": len(policies),
                     "exceptions": len(exceptions)},
        ))
        s.commit()
        s.refresh(rec)
        out = _export_to_dict(rec, with_exceptions=True, mga=_tenant_name(s, rec.tenant_id))
        out["unprocessable_rules"] = unprocessable
        return out


@app.get("/export/downloads")
def export_downloads_list(mga: Optional[str] = None, limit: int = 50,
                          offset: int = 0,
                          principal: Principal = Depends(current_principal)):
    """Generated-output history, newest first. `offset` supports pagination
    (e.g. the dashboard's Recent runs). Still returns a plain list."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        q = s.query(OutputExport).filter(OutputExport.tenant_id == tid)
        return [_export_to_dict(r, mga=mga or _tenant_name(s, r.tenant_id))
                for r in q.order_by(OutputExport.id.desc())
                          .offset(max(0, offset)).limit(limit).all()]


# NOTE: declared before /export/downloads/{export_id} so "count" isn't parsed
# as an export id.
@app.get("/export/downloads/count")
def export_downloads_count(mga: Optional[str] = None,
                           principal: Principal = Depends(current_principal)):
    """Total number of generated outputs — for `page X of N` pagination."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        q = s.query(OutputExport).filter(OutputExport.tenant_id == tid)
        return {"total": q.count()}


@app.get("/export/downloads/{export_id}")
def export_download_get(export_id: int,
                        principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        r = s.get(OutputExport, export_id)
        if not r:
            raise HTTPException(404, "export not found")
        assert_tenant_owns(principal, r.tenant_id)
        return _export_to_dict(r, with_exceptions=True, mga=_tenant_name(s, r.tenant_id))


@app.get("/export/downloads/{export_id}/file")
def export_download_file(export_id: int,
                         principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        r = _export_file_row(s, export_id)
        if not r:
            raise HTTPException(404, "export file not found")
        assert_tenant_owns(principal, r.tenant_id)
        data = storage.resolve_bytes(r.blob_ref, r.blob)
        if not data:
            raise HTTPException(404, "export file not found")
        from output_serializers import content_type_for_filename
        return Response(
            content=data,
            media_type=content_type_for_filename(r.filename),
            headers={"Content-Disposition": _content_disposition(r.filename)},
        )


@app.get("/export/downloads/{export_id}/data")
def export_download_data(export_id: int,
                         full: bool = False,
                         marks: bool = False,
                         sheet: str | None = None,
                         offset: int = 0,
                         limit: int | None = None,
                         row_indices: str | None = None,
                         principal: Principal = Depends(current_principal)):
    """The rendered rows of the generated file, for in-site viewing.

    `marks=1` also returns the light-red failed-validation cells (so the preview
    highlights exactly what the downloaded file does). `full=1` lifts the row cap
    from the preview head to the whole output ("see all rows"). Both default off,
    so callers that just want the preview grid are unaffected.

    Passing `sheet` together with either `limit` (page a contiguous row window
    starting at `offset`) or `row_indices` (a comma-separated list of specific
    1-based row numbers, header excluded) switches to windowed mode: every sheet
    still reports its header and total row count, but only `sheet` gets data
    rows, and its marks/notes are trimmed to the rows returned.

    That trimming matters more than it sounds. The marks and notes come back
    whole-sheet from the cache, and on a heavily flagged export the notes — each
    one a full failure message — are ~8 MB against ~9 KB for a single row. A
    caller asking for one row was being handed the entire sheet's annotations to
    filter down to eight of them. Whole-sheet marks are still available where
    they are genuinely needed: `/data/stream` sends them once per sheet on its
    opening line, before any row, which is what the review grid uses to drive
    its highlighting and its Exceptions filter.
    """
    with SessionLocal() as s:
        r = _export_file_row(s, export_id)
        if not r:
            raise HTTPException(404, "export file not found")
        assert_tenant_owns(principal, r.tenant_id)
        data = storage.resolve_bytes(r.blob_ref, r.blob)
        if not data:
            raise HTTPException(404, "export file not found")
        cap = 20000 if full else 500
        # Which physical row is each sheet's real header (from the export's own
        # template) — only populated for a header NOT on row 1, so the common
        # case is an empty dict and behaves exactly as before.
        header_rows = _export_header_rows(s, r)
        if limit is not None or row_indices is not None:
            if offset < 0 or (limit is not None and limit <= 0):
                raise HTTPException(400, "invalid offset/limit")
            idx_set = None
            if row_indices is not None:
                try:
                    idx_set = {int(x) for x in row_indices.split(",") if x.strip() != ""}
                except ValueError:
                    raise HTTPException(400, "invalid row_indices")
            name = (r.filename or "").lower()
            if name.endswith((".xlsx", ".xls")):
                # Parse-once / serve-many: the styled workbook open costs the
                # whole file regardless of window size, so chunked scrolling
                # would otherwise re-pay it on every page. grid_cache keys on
                # the blob's content, so an in-place re-render invalidates
                # itself. Falls back to the direct read if anything goes wrong,
                # so a cache problem can never break viewing.
                try:
                    from grid_cache import grid_page as _grid_page
                    sheets = _grid_page(data, sheet_name=sheet, offset=offset, limit=limit,
                                        row_indices=idx_set, with_marks=marks,
                                        header_row_by_sheet=header_rows)
                except Exception as e:  # noqa: BLE001
                    log.warning("grid cache unavailable for export %s (%s); "
                                "falling back to direct read", export_id, e)
                    sheets = _xlsx_to_grid_page(data, sheet_name=sheet, offset=offset, limit=limit,
                                                row_indices=idx_set, with_marks=marks,
                                                header_row_by_sheet=header_rows)
                # Trim the annotations to the rows actually returned. Rebuilt
                # rather than mutated in place: for the cached path these dicts
                # hold references straight into the shared grid_cache entry.
                for i, sh in enumerate(sheets):
                    gis = sh.get("row_gis")
                    if gis is None:
                        continue
                    keep = set(gis)
                    sheets[i] = dict(
                        sh,
                        marks=[m for m in (sh.get("marks") or ()) if m[0] in keep],
                        warn_marks=[m for m in (sh.get("warn_marks") or ()) if m[0] in keep],
                        notes=[n for n in (sh.get("notes") or ()) if n["r"] in keep],
                    )
            else:
                # Non-xlsx outputs carry no cell-level marks to page around —
                # pagination only pays off for the highlighted xlsx grid, so
                # fall back to the full grid for CSV/ZIP/XML/JSON rather than
                # guessing a window that has nothing to highlight anyway.
                sheets = _output_to_grid(data, r.filename, max_rows=cap, with_marks=marks,
                                         header_row_by_sheet=header_rows)
        else:
            sheets = _output_to_grid(data, r.filename, max_rows=cap, with_marks=marks,
                                     header_row_by_sheet=header_rows)
        # Flag which sheets hold the actual policy DATA (as opposed to the
        # template's static spec/instruction sheets that also live in the file),
        # so the preview can point straight at the data the user sees on download
        # instead of landing on a documentation sheet. Uses the setup's own
        # role='data' classification — no name/heuristic guessing.
        data_names = _export_data_sheet_names(s, r)
        if data_names:
            for sh in sheets:
                sh["is_data"] = " ".join(str(sh["sheet"]).split()).strip().lower() in data_names
        return {"filename": r.filename, "row_cap": cap, "sheets": sheets}


@app.get("/export/downloads/{export_id}/data/stream")
def export_download_data_stream(export_id: int,
                                marks: bool = False,
                                sheet: str | None = None,
                                chunk: int | None = None,
                                delay_ms: int = 0,
                                offset: int = 0,
                                max_rows: int | None = None,
                                row_gis: str | None = None,
                                meta: bool = True,
                                principal: Principal = Depends(current_principal)):
    """One WINDOW of the grid, pushed down a single connection in pieces.

    Where `/data` answers a window in one lump, this holds the response open and
    writes it out as newline-delimited JSON, so the browser paints rows while
    the rest of the window is still arriving.

    Crucially the window is BOUNDED (`_STREAM_PAGE_ROWS`). An earlier version
    streamed the whole sheet in one response, which is not what chunked delivery
    is for: a 10k-row x 77-column sheet is ~9.5 MB of JSON that the client has
    to parse and fold into state in its entirety before the tab settles, all to
    show the ~60 rows that fit on screen. The grid now asks for the rows it is
    about to display and comes back for the next window as the user scrolls, so
    the cost of opening a sheet is the same whether it has 400 rows or 400,000.

    Which rows:
      · `offset` + `max_rows` — a contiguous run, `offset` being the count of
        data rows to skip (header excluded), capped at `_STREAM_PAGE_ROWS`;
      · `row_gis` — an explicit comma-separated list of 1-based data rows, for
        the Exceptions filter, whose rows are scattered through the sheet.

    Line 1 carries what the grid needs before any row appears:
        {"type":"meta", "filename":…, "target":…, "chunk":N, "window":N,
         "sheets":[{sheet, rows:[header], total_rows, is_data, marks, notes}]}
    then one line per chunk of the target sheet:
        {"type":"rows", "sheet":…, "row_gis":[…], "rows":[[…], …], "notes":[…]}
    and finally:
        {"type":"done", "sheet":…, "total_rows":N, "delivered":[…]}

    `meta=0` drops the `sheets` payload from line 1 — a continuation request
    already has the headers and the whole-sheet marks, and on a heavily flagged
    export those marks are ~0.8 MB that would otherwise be re-sent on every
    scroll. `marks` therefore only needs to ride the FIRST request per sheet,
    where it arrives before any row so the highlighting and the pending/resolved
    counts are right from the first paint. `notes` (the cell comments holding
    each failure's text) travel with the chunk carrying their row — they are the
    bulk of the payload (14.5 MB against 0.7 MB of marks on a real export) and
    are only read when a cell's popover opens.

    `done` reports `delivered`, the rows this response actually produced, so the
    client can tell "not fetched yet" from "this row does not exist" and stop
    asking for a gap it can never fill.

    Once the first byte is out the status is locked at 200, so a later failure
    arrives as {"type":"error", "detail":…} rather than an HTTP error (the same
    trade-off `streaming.py` documents).

    `chunk` splits the window into lines; `delay_ms` paces them apart to make
    the delivery observable. Both default sensibly and neither is needed in
    normal use.
    """
    import json as _json
    import time as _time
    from fastapi.responses import StreamingResponse as _Stream

    # Resolve everything that can legitimately 4xx BEFORE the stream opens,
    # while a real status code can still be sent.
    pinned_chunk = max(1, min(int(chunk), 5000)) if chunk is not None else None
    delay_ms = max(0, min(int(delay_ms or 0), 5000))
    offset = max(0, int(offset or 0))
    window_rows = _STREAM_PAGE_ROWS if max_rows is None else int(max_rows)
    window_rows = max(1, min(window_rows, _STREAM_MAX_PAGE_ROWS))
    wanted_gis: "list[int] | None" = None
    if row_gis is not None:
        try:
            wanted_gis = sorted({int(x) for x in row_gis.split(",") if x.strip()})
        except ValueError:
            raise HTTPException(400, "invalid row_gis")
        wanted_gis = wanted_gis[:_STREAM_MAX_PAGE_ROWS]
    with SessionLocal() as s:
        # Deliberately NOT the bytes: viewing a sheet the server has already
        # parsed shouldn't move the file across the wire again, and every tab
        # the user opens is another one of these requests. The digest is enough
        # to find the cached parse; the bytes are fetched below only if it isn't
        # there.
        r = _export_meta_row(s, export_id)
        if not r:
            raise HTTPException(404, "export file not found")
        assert_tenant_owns(principal, r.tenant_id)
        digest = _export_blob_digest(s, export_id)
        if not digest and not r.blob_ref:
            raise HTTPException(404, "export file not found")
        filename = r.filename
        data_names = _export_data_sheet_names(s, r)
        header_rows = _export_header_rows(s, r)

    if not (filename or "").lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "streaming is only available for xlsx output")

    def _gen():
        try:
            from grid_cache import fingerprint as _fp
            from grid_cache import open_grid_for as _open_grid_for

            # ONE handle for the whole response: the workbook is resolved (and,
            # first time, parsed) once here, and every chunk below is a slice of
            # that same parse. Resolving per chunk instead would re-digest the
            # entire file thousands of times over a large sheet.
            def _load():
                b = _export_blob_bytes(export_id)
                if not b:
                    raise HTTPException(404, "export file not found")
                return b

            # A blob-storage export can't be digested by the database, so it
            # falls back to fetching first and digesting locally — correct
            # either way, just without the saving.
            key = digest or _fp(_load())
            # ALWAYS resolved with marks, whatever this request asked to be
            # sent. `grid_cache` keys entries on `with_marks`, so alternating
            # the flag would build — and hold — a second, near-identical parse
            # of the same workbook: measured at a 5.2s re-parse on the first
            # continuation request, i.e. every scroll past the opening window.
            # `marks` below therefore governs serialisation only; the parse is
            # shared by every request for these bytes.
            grid = _open_grid_for(key, _load, with_marks=True,
                                  header_row_by_sheet=header_rows)

            # limit=0 asks for no row window: headers, dimensions and the
            # whole-sheet marks only.
            head = grid.page(sheet_name=sheet, limit=0)
            if data_names:
                for sh in head:
                    sh["is_data"] = " ".join(str(sh["sheet"]).split()).strip().lower() in data_names
            # The one sheet carrying row_gis is the one being streamed.
            target = next((sh for sh in head if sh.get("row_gis") is not None), None)
            target_name = target["sheet"] if target else (head[0]["sheet"] if head else None)
            total = int(target.get("total_rows") or 0) if target else 0

            # The rows THIS request is responsible for — never more than one
            # bounded window, whatever the sheet's size.
            if wanted_gis is not None:
                window = [gi for gi in wanted_gis if 1 <= gi <= total]
            else:
                window = list(range(offset + 1, min(offset + window_rows, total) + 1))
            chunk_rows = pinned_chunk or _STREAM_CHUNK_ROWS

            # ONE slice of the parsed workbook for the whole window, then split
            # locally into chunks. Asking `grid.page` per chunk instead would,
            # for a workbook too large to keep row values resident, re-open the
            # xlsx from the blob on every chunk — the exact per-chunk whole-file
            # cost `grid_cache` exists to remove.
            rows_by_gi: dict = {}
            if window:
                page = grid.page(sheet_name=target_name, row_indices=window)
                sh = next((x for x in page if x["sheet"] == target_name), None)
                gis = (sh or {}).get("row_gis") or []
                vals = ((sh or {}).get("rows") or [])[1:]
                rows_by_gi = dict(zip(gis, vals))

            # `marks` stays whole-sheet on the opening line — it is a compact
            # list of [row, col] pairs, and the Exceptions filter and its count
            # have to be right before any row shows up. The cell COMMENTS are a
            # different matter: they carry the full failure text, so on a sheet
            # with tens of thousands of flagged cells they dwarf everything else
            # (14.5 MB against 0.7 MB of marks on a real export). A comment is
            # only ever read when its cell's popover opens, and a cell can only
            # be clicked once its row is on screen, so each one travels with the
            # chunk that carries its row. Nothing is lost, only deferred.
            notes_by_row: dict = {}
            for n in (target.get("notes") or ()) if target else ():
                notes_by_row.setdefault(n["r"], []).append(n)

            open_line = {"type": "meta", "filename": filename, "target": target_name,
                         "chunk": chunk_rows, "window": len(window),
                         "total_rows": total}
            if meta:
                # Rebuilt rather than mutated — these dicts hold references
                # straight into the shared cache entry, which must not be
                # edited in place. Only the target sheet has marks/notes to
                # strip; the others already came back without them.
                open_line["sheets"] = [
                    dict(sh, notes=[], marks=(sh["marks"] if marks else []),
                         warn_marks=(sh.get("warn_marks", []) if marks else []))
                    if sh is target else sh
                    for sh in head
                ]
            yield (_json.dumps(open_line, default=_json_default) + "\n").encode()

            delivered: list = []
            for i in range(0, len(window), chunk_rows):
                gis = [gi for gi in window[i:i + chunk_rows] if gi in rows_by_gi]
                if not gis:
                    # A row the sheet declares but that yielded no values. Skip
                    # it rather than ending the stream: stopping here would
                    # silently drop every row after the gap. It stays out of
                    # `delivered`, which is how the client learns not to keep
                    # asking for it.
                    continue
                if delay_ms:
                    # Sync generators are iterated in a threadpool, so this
                    # paces the stream without blocking the event loop.
                    _time.sleep(delay_ms / 1000.0)
                yield (_json.dumps({
                    "type": "rows", "sheet": target_name,
                    "row_gis": gis, "rows": [rows_by_gi[gi] for gi in gis],
                    "notes": [n for gi in gis for n in notes_by_row.get(gi, ())],
                }, default=_json_default) + "\n").encode()
                delivered.extend(gis)

            # Only the SHORTFALL, not the whole window: rows the request asked
            # for and could not produce. Normally empty, and echoing all 600
            # requested numbers back just to have the client diff them would add
            # several KB to every scroll for an answer the server already has.
            got = set(delivered)
            yield (_json.dumps({
                "type": "done", "sheet": target_name, "total_rows": total,
                "absent": [gi for gi in window if gi not in got],
            }) + "\n").encode()
        except Exception as e:  # noqa: BLE001 — status is already 200 by now
            log.exception("BDX stream failed for export %s", export_id)
            yield (_json.dumps({"type": "error", "detail": str(e)}) + "\n").encode()

    return _Stream(
        _gen(),
        media_type="application/x-ndjson",
        # Same pass-through hints streaming.py uses, so nothing between here and
        # the browser holds chunks back waiting for the response to finish.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
