"""Shared helpers extracted from the monolith's app_routes.py (endpoints removed)."""
from __future__ import annotations

__all__ = [
    'APIRouter',
    'ActivityEvent',
    'AdminMappingTask',
    'Any',
    'AppUser',
    'BaseModel',
    'Body',
    'ChangePasswordBody',
    'ClauseResolveBody',
    'Contract',
    'ContractExtractionService',
    'Depends',
    'DirectFormat',
    'ExportTemplate',
    'ExtraFieldBody',
    'File',
    'ForgotBody',
    'Form',
    'HTTPException',
    'LoginBody',
    'Mapper',
    'NewTenantBody',
    'Optional',
    'OutputExport',
    'Party',
    'PartyBody',
    'PartyContact',
    'PartyContactBody',
    'Principal',
    'ProfileBody',
    'Program',
    'ProgramBody',
    'RefreshBody',
    'Request',
    'ResetBody',
    'RuleRetargetBody',
    'SessionLocal',
    'SheetBinding',
    'Tenant',
    'TenantBody',
    'Upload',
    'UploadFile',
    'UserBody',
    '_LOGIN_ATTEMPTS',
    '_LOGIN_MAX',
    '_LOGIN_WINDOW',
    '_deque',
    '_extract_reference_documents',
    '_get_or_create_tenant',
    '_get_tenant_id',
    '_iso_utc',
    '_json_value',
    '_load_rule_for_contract',
    '_log',
    '_mapper_sheet_names',
    '_output_fields_from_rule',
    '_party_dict',
    '_program_dict',
    '_purge_rule_sql',
    '_rate_limit_login',
    '_re_sb',
    '_sb_binding_dict',
    '_sb_propose_schedule',
    '_template_fields_from_structure',
    '_tenant_dict',
    '_tenant_name',
    '_user_dict',
    'and_',
    'annotations',
    'assert_tenant_owns',
    'contract_service',
    'current_principal',
    'datetime',
    'desc',
    'exists',
    'func',
    'json',
    'or_',
    'os',
    'require_role',
    'resolve_tenant_id',
    'run_in_threadpool',
    'tempfile',
    'text',
    'timedelta',
    'timezone',
]

"""CRUD routes backing the Kavachio wireframes (S-01, S-02, S-03/S-03a,
S-05, S-12, S-22). Validation-rule extraction is intentionally omitted.

Mounted on the FastAPI app by main.py.
"""

from datetime import datetime, timedelta, timezone

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile

from fastapi.concurrency import run_in_threadpool

from pydantic import BaseModel

from sqlalchemy import and_, desc, func, or_, text

from db import (
    ActivityEvent, AdminMappingTask, AppUser, Contract, DirectFormat, ExportTemplate,
    Mapper, OutputExport, Party, PartyContact, Program, SessionLocal, Tenant, Upload,
    SheetBinding, ReferenceDocument,
)

from sqlalchemy import exists

from auth_deps import require_role, current_principal, Principal

from contract_upload_services.contract_extraction_service import (
    ContractExtractionService
)

contract_service = ContractExtractionService()

import os,json

import tempfile
import storage  # blob storage abstraction (Azure/Azurite; DB-blob fallback)

def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value

def _iso_utc(dt: Any) -> Optional[str]:
    """Serialize a stored timestamp as an explicit-UTC ISO string.

    The columns are naive but always hold UTC (see the datetime.utcnow()
    writers); without the marker JS Date() parses the string as local time.
    Display timestamps only — never business dates, which are compared and
    re-parsed downstream.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def _template_fields_from_structure(structure: Optional[dict]) -> list[dict]:
    from exporter import is_reference_sheet
    fields: list[dict] = []
    for sheet in (structure or {}).get("sheets", []):
        # Reference / lookup tabs (classified at template-parse time from their
        # headers) are not validation targets — skip them entirely so no rule can
        # bind to their columns (see exporter.classify_sheet_roles).
        if is_reference_sheet(sheet):
            continue
        sheet_name = sheet.get("sheet_name", "")
        for col in sheet.get("columns", []):
            name = col.get("column_name") or col.get("header") or ""
            if name:
                fields.append({
                    "name": name,
                    "sheet": sheet_name,
                    "canonical_field": col.get("canonical_field"),
                    "confidence": col.get("confidence"),
                    "samples": (col.get("samples") or [])[:3],
                    # Data-dictionary enrichment (present when the template ships a
                    # spec sheet) — meaning + allowed values for better mapping.
                    "description": col.get("description"),
                    "allowed_values": col.get("allowed_values") or [],
                    "field_format": col.get("field_format"),
                    "required": col.get("required"),
                })
    return fields

def _output_fields_from_rule(rule: dict) -> list[str]:
    fields: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value not in fields:
            fields.append(value)

    target = _json_value(rule.get("canonical_target")) or {}
    if isinstance(target, dict):
        add(target.get("output_field"))
        for key in ("output_fields", "fields"):
            value = target.get(key)
            if isinstance(value, list):
                for item in value:
                    add(item)

    spec = _json_value(rule.get("rule_spec")) or {}
    if isinstance(spec, dict):
        add(spec.get("field"))
        for key in ("fields", "group_by"):
            value = spec.get(key)
            if isinstance(value, list):
                for item in value:
                    add(item)

    return fields

async def _extract_reference_documents(reference_files, program_id=None,
                                       tenant_id=None) -> list[dict]:
    """Extract plain text from each uploaded reference document.

    Uses the same extractor as the contract (extract_document_data). Returns a
    list of {"name", "text"} dicts ready to feed into the extraction LLM.
    """
    if not reference_files:
        return []

    from contract_upload_services.document_extractors import extract_document_data
    from contract_upload_services.prompt_builder import build_llm_context

    out: list[dict] = []
    for rf in reference_files:
        if not rf or not rf.filename:
            continue
        rbytes = await rf.read()
        rsuffix = os.path.splitext(rf.filename)[1] or ".pdf"
        with tempfile.NamedTemporaryFile(delete=False, suffix=rsuffix) as rtmp:
            rtmp.write(rbytes)
            rtmp_path = rtmp.name
        try:
            rdata = await run_in_threadpool(extract_document_data, rtmp_path)
            rtype = rdata.get("type")
            ref_extra = {}
            if rtype == "pdf":
                rtext = build_llm_context(rdata)
            elif rtype in ("docx", "doc"):
                # Give the extraction LLM clean [TABLE] grids — this path used to
                # hand it a raw json.dumps blob, which it rebuilt into malformed
                # pipe tables in the clause text. Keep the STRUCTURED table data
                # alongside (`data`) so build_reference_group_members can still
                # read the grids directly (the text is no longer a JSON string).
                # PDF / excel / csv reference handling is deliberately unchanged.
                rtext = build_llm_context(rdata)
                ref_extra["data"] = rdata.get("data")
            else:
                rd = rdata.get("data")
                rtext = rd if isinstance(rd, str) else json.dumps(rd, default=str)
            out.append({"name": rf.filename, "text": rtext or "", **ref_extra})

            # Persist the reference file to blob storage (Azure/Azurite) and
            # record a ReferenceDocument row pointing at it. Historically these
            # files were extracted then discarded. Non-fatal on failure.
            if storage.is_azure() and program_id is not None:
                try:
                    rk = storage.build_key("references", tenant_id, rf.filename)
                    await run_in_threadpool(
                        storage.put_bytes, rk, rbytes,
                        rf.content_type or "application/octet-stream")
                    with SessionLocal() as s:
                        s.add(ReferenceDocument(
                            tenant_id=tenant_id, program_id=program_id,
                            filename=rf.filename, kind="reference",
                            blob_ref=rk, extracted={"text": rtext or ""},
                        ))
                        s.commit()
                except Exception as se:  # noqa: BLE001 — best-effort
                    print(f"[Setup] reference blob persist failed for {rf.filename!r}: {se}")
        except Exception as re:
            print(f"[Setup] reference extraction failed for {rf.filename!r}: {re}")
        finally:
            if os.path.exists(rtmp_path):
                os.remove(rtmp_path)
    return out

def _get_tenant_id(session, mga: str) -> Optional[int]:
    """Look up the canonical tenant_id for an mga code, or None if not found."""
    from sqlalchemy import text
    row = session.execute(
        text("SELECT tenant_id FROM tenant WHERE tenant_name=:m LIMIT 1"),
        {"m": mga}).fetchone()
    return row[0] if row else None

def resolve_tenant_id(session, principal: Principal, mga: Optional[str] = None) -> int:
    """Authoritative tenant_id for a request, derived from the TRUSTED token —
    the migration bridge that replaces trusting the client-supplied `mga`
    (MULTITENANCY_AUTH_CONCEPT.md §6.6, run-in-parallel).

    - Regular users (tenant_user / tenant_admin) are pinned to their token's
      tenant. `mga` is IGNORED for them, so it can no longer be used to reach
      another tenant's data — the #1 multi-tenant leak.
    - Platform admins (kavachio_admin, tenant_id is None) may act on any tenant,
      selected via `mga`.
    """
    if principal.is_platform_admin:
        tid = _get_tenant_id(session, mga) if mga else None
        if tid is None:
            raise HTTPException(400, "platform admin must select a tenant (mga)")
        return tid
    if principal.tenant_id is None:
        raise HTTPException(403, "no tenant bound to this user")
    return principal.tenant_id

def assert_tenant_owns(principal: Principal, tenant_id: Optional[int]) -> None:
    """Guard a by-id (IDOR-prone) route: the fetched row must belong to the
    caller's tenant. Platform admins bypass. Raises 404 (not 403) so a user
    can't probe which ids exist in other tenants."""
    if principal.is_platform_admin:
        return
    if tenant_id != principal.tenant_id:
        raise HTTPException(404, "not found")

def _tenant_name(session, tenant_id: Optional[int]) -> Optional[str]:
    """Reverse of _get_tenant_id: tenant_id -> the legacy mga code (tenant_name).
    Used to keep the `mga` field in API responses the frontend still reads."""
    if not tenant_id:
        return None
    from sqlalchemy import text
    row = session.execute(
        text("SELECT tenant_name FROM tenant WHERE tenant_id=:t LIMIT 1"),
        {"t": tenant_id}).fetchone()
    return row[0] if row else None

def _log(mga: str, actor: Optional[str], action: str, target: Optional[str] = None,
         details: Optional[dict] = None) -> None:
    # `mga` is still the inbound caller key; resolve it to the tenant_id the
    # row is actually stored under.
    with SessionLocal() as s:
        s.add(ActivityEvent(tenant_id=_get_tenant_id(s, mga), actor=actor,
                            action=action, target=target, details=details or {}))
        s.commit()

class LoginBody(BaseModel):
    email: str
    password: str

class ExtraFieldBody(BaseModel):
    key: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    data_type: Optional[str] = "string"
    shared: Optional[bool] = False

from collections import deque as _deque

_LOGIN_ATTEMPTS: dict = {}

_LOGIN_MAX = 10           # attempts per key

_LOGIN_WINDOW = 300       # seconds (5 minutes)

def _rate_limit_login(*keys) -> None:
    """Raise 429 if any key exceeded _LOGIN_MAX attempts in _LOGIN_WINDOW."""
    import time
    now = time.time()
    for k in keys:
        dq = _LOGIN_ATTEMPTS.setdefault(k, _deque())
        while dq and now - dq[0] > _LOGIN_WINDOW:
            dq.popleft()
        if len(dq) >= _LOGIN_MAX:
            raise HTTPException(429, "too many login attempts; try again later")
    for k in keys:
        _LOGIN_ATTEMPTS[k].append(now)

class RefreshBody(BaseModel):
    refresh_token: str

class ForgotBody(BaseModel):
    email: str

class ResetBody(BaseModel):
    token: str
    password: str

class ChangePasswordBody(BaseModel):
    user_id: int
    current_password: str
    new_password: str

class TenantBody(BaseModel):
    legal_name: Optional[str] = None
    tenant_type: Optional[str] = None
    address: Optional[dict] = None
    currency: Optional[str] = None
    internal_codes: Optional[dict] = None

def _tenant_dict(t: Tenant) -> dict:
    # `mga` in the response is the tenant_name the frontend keys on.
    return {"id": t.id, "mga": t.tenant_name, "legal_name": t.legal_name,
            "tenant_type": t.tenant_type, "address": t.address,
            "currency": t.currency,
            "internal_codes": t.internal_codes}

def _get_or_create_tenant(s, mga: str) -> Tenant:
    from ingester import _ensure_tenant
    tid = _ensure_tenant(s, mga)
    return s.query(Tenant).filter(Tenant.id == tid).first()

class NewTenantBody(BaseModel):
    name: str
    tenant_type: Optional[str] = None
    currency: Optional[str] = None
    is_active: Optional[bool] = True
    admin_name: Optional[str] = None
    admin_email: Optional[str] = None

class PartyBody(BaseModel):
    party_type: str
    legal_name: str
    scope: Optional[str] = "tenant"
    dba_name: Optional[str] = None
    tax_id: Optional[str] = None
    naics_code: Optional[str] = None
    am_best_rating: Optional[str] = None
    domicile_country: Optional[str] = None
    primary_jurisdiction: Optional[str] = None
    is_active: Optional[bool] = True
    addresses: Optional[list] = None
    notes: Optional[str] = None

def _party_dict(p: Party, mga: Optional[str] = None) -> dict:
    return {
        "id": p.id, "mga": mga, "scope": p.scope, "party_type": p.party_type,
        "legal_name": p.legal_name, "dba_name": p.dba_name, "tax_id": p.tax_id,
        "naics_code": p.naics_code, "am_best_rating": p.am_best_rating,
        "domicile_country": p.domicile_country,
        "primary_jurisdiction": p.primary_jurisdiction,
        "is_active": bool(p.is_active), "addresses": p.addresses or [],
        "notes": p.notes,
    }

class PartyContactBody(BaseModel):
    full_name: str
    title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None

class ProgramBody(BaseModel):
    name: Optional[str] = None
    party_id: Optional[int] = None
    lead_carrier: Optional[str] = None
    admin_party: Optional[str] = None
    bdx_frequency: Optional[str] = None
    business_segment: Optional[str] = None
    product_line: Optional[str] = None
    distribution_channel: Optional[str] = None
    territory: Optional[str] = None
    commercial_terms: Optional[dict] = None
    status: Optional[str] = None

def _program_dict(p: Program, mga: Optional[str] = None) -> dict:
    return {"id": p.id, "mga": mga, "name": p.name,
            "party_id": p.party_id,
            "lead_carrier": p.lead_carrier, "admin_party": p.admin_party,
            "bdx_frequency": p.bdx_frequency,
            "business_segment": p.business_segment,
            "product_line": p.product_line,
            "distribution_channel": p.distribution_channel,
            "territory": p.territory, "commercial_terms": p.commercial_terms,
            "status": p.status, "source_contract_file": p.source_contract_file}

import re as _re_sb

def _sb_propose_schedule(sheet_name: str):
    """Name-match a sheet to a schedule + role. 'Palms Sch A Current BDX' →
    ('Schedule A', 'schedule'); 'Summary'/'Check' → their roles."""
    low = (sheet_name or "").lower()
    if "summary" in low:
        return None, "summary"
    if low.strip() in ("check", "checks") or low.startswith("check"):
        return None, "check"
    m = _re_sb.search(r"\bsch(?:edule)?\s*([a-z])\b", low)
    if m:
        return f"Schedule {m.group(1).upper()}", "schedule"
    return None, "schedule"

def _sb_binding_dict(b: SheetBinding, proposed=False):
    return {
        "sheet_name": b.sheet_name, "role": b.role,
        "schedule_key": b.schedule_key, "contract_id": b.contract_id,
        "output_template_id": b.output_template_id,
        "depends_on": b.depends_on or [], "reference_doc_ids": b.reference_doc_ids or [],
        "approved": bool(b.approved), "proposed": proposed,
    }

def _mapper_sheet_names(mp: Mapper) -> list:
    if mp is None:
        return []
    if getattr(mp, "selected_sheets", None):
        return list(mp.selected_sheets)
    spec = getattr(mp, "spec_by_sheet", None) or {}
    if spec:
        return list(spec.keys())
    return []

class RuleRetargetBody(BaseModel):
    new_field: str
    old_field: Optional[str] = None
    mga: Optional[str] = None
    actor: Optional[str] = None

def _load_rule_for_contract(s, contract_id: int, rule_id: int) -> dict:
    row = s.execute(
        text("""SELECT rule_id, rule_name, rule_spec, canonical_target,
                       error_message, rule_status
                FROM validation_rule WHERE rule_id = :rid AND contract_id = :cid"""),
        {"rid": rule_id, "cid": contract_id},
    ).mappings().first()
    if not row:
        raise HTTPException(404, "rule not found for this contract")
    return dict(row)

def _purge_rule_sql(s, rule_id: int) -> None:
    """Drop the rule's compiled-SQL cache row in its OWN transaction. On Postgres
    a failed statement aborts the surrounding transaction, so this must NOT share
    the transaction that already committed the rule change — otherwise a missing
    table / lock error would silently roll the rule change back."""
    try:
        s.execute(text("DELETE FROM rule_sql WHERE rule_id = :rid"), {"rid": rule_id})
        s.commit()
    except Exception:
        s.rollback()

class ClauseResolveBody(BaseModel):
    # Primary column the rule targets. `output_fields` (plural) may carry the FULL
    # set when the rule spans several columns; the first is the primary. Either may
    # be sent — output_fields wins when both are present.
    output_field: Optional[str] = None
    output_fields: Optional[List[str]] = None
    actor: Optional[str] = None
    mga: Optional[str] = None
    # Single free-text note: the overall rule LOGIC to enforce for this field/clause
    # (plus any reasoning). Drives generation AND is stored for reference.
    note: Optional[str] = None

class UserBody(BaseModel):
    email: str
    full_name: str
    role: Optional[str] = "ops"
    status: Optional[str] = "active"
    password: Optional[str] = None

def _user_dict(u: AppUser, mga: Optional[str] = None) -> dict:
    return {"id": u.id, "mga": mga, "email": u.email,
            "full_name": u.full_name, "role": u.role, "status": u.status,
            "tenant_id": u.tenant_id,
            "last_login_at": _iso_utc(u.last_login_at)}

class ProfileBody(BaseModel):
    full_name: str
