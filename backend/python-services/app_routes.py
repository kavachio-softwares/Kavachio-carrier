"""CRUD routes backing the Kavachio wireframes (S-01, S-02, S-03/S-03a,
S-05, S-12, S-22). Validation-rule extraction is intentionally omitted.

Mounted on the FastAPI app by main.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, date
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import String, and_, desc, func, or_, select, text

from db import (
    ActivityEvent, AdminMappingTask, AppUser, Contract, DirectFormat, ExportTemplate,
    GenericRuleSpecification, Mapper, OutputExport, Party, PartyContact, Program,
    ProgramBroker,
    SessionLocal, Tenant, Upload, SheetBinding, ReferenceDocument, SubmissionSchedule,
    ExpectedSubmission,
)
from sqlalchemy import exists
from sqlalchemy.exc import IntegrityError

from auth_deps import require_role, current_principal, Principal

router = APIRouter()

from contract_upload_services.contract_extraction_service import (
    ContractExtractionService
)
contract_service = ContractExtractionService()
import asyncio
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


def _as_utc(dt: Any) -> Optional[datetime]:
    """Normalize any stored timestamp to an AWARE UTC datetime.

    Needed because the ORM and the database disagree about tz-awareness, and
    which side you get depends on the column:

      • Several models declare `Column(DateTime)` (naive) while the underlying
        Postgres column is actually `timestamp with time zone` — program.created_at,
        party.created_at and contract.created_at are all timestamptz. psycopg2
        hands those back AWARE, in the session's timezone (e.g. +05:30).
      • Genuinely naive columns exist too, and by this codebase's convention
        always hold UTC (the datetime.utcnow() writers).

    Comparing the two kinds directly raises
    "can't compare offset-naive and offset-aware datetimes", so every timestamp
    comparison must funnel through here first. Naive input is read as UTC;
    aware input is converted to UTC, preserving the instant.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_utc(dt: Any) -> Optional[str]:
    """Serialize a stored timestamp as an explicit-UTC ISO string.

    Without the marker JS Date() parses the string as local time. Display
    timestamps only — never business dates, which are compared and re-parsed
    downstream.
    """
    utc = _as_utc(dt)
    if utc is None:
        return None
    return utc.isoformat().replace("+00:00", "Z")


def _parse_client_dt(v: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp sent by the frontend (e.g. a local day-bound from
    localDayStart/localDayEnd, serialized via Date.toISOString()) into a naive
    UTC datetime, so it can be compared against the naive-UTC `created_at`
    columns. Returns None on anything unparseable (filter is simply skipped)."""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


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
                    # Every sample the template parser captured. The LLM-facing
                    # list above stays at 3 (prompt size), but the DETERMINISTIC
                    # arithmetic checks in the verify gate use all of them: two
                    # candidate base columns can be identical over the first rows
                    # and differ on the next, and that is exactly what decides
                    # which one a "share = base × rate" identity belongs to.
                    "samples_all": col.get("samples") or [],
                    # …and the rows that TELL THIS SHEET'S COLUMNS APART, aligned
                    # so position i is the same row in every column (see
                    # exporter._grounding_row_positions). Read only by those
                    # arithmetic checks, which have to compare columns row by row;
                    # absent on a template parsed before they were captured, where
                    # they fall back to `samples_all` as before.
                    "row_samples": col.get("row_samples") or [],
                    # Data-dictionary enrichment (present when the template ships a
                    # spec sheet) — meaning + allowed values for better mapping.
                    "description": col.get("description"),
                    "allowed_values": col.get("allowed_values") or [],
                    "field_format": col.get("field_format"),
                    "required": col.get("required"),
                    # Per-column derivation note captured from the row above the
                    # header (e.g. "Payable due X = Gross Premium − Commission …").
                    # Read ONLY by the formula deriver, never by the mapping LLM.
                    "formula": col.get("formula"),
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
        text("SELECT tenant_id FROM tenant WHERE tenant_code=:m LIMIT 1"),
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
        text("SELECT tenant_code FROM tenant WHERE tenant_id=:t LIMIT 1"),
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


def _actor(principal) -> Optional[str]:
    """Resolve the acting user's email from a Principal, for the `actor` field of
    audit rows. Best-effort — never raises into the request."""
    try:
        from audit import actor_email
        return actor_email(principal.user_id) if principal is not None else None
    except Exception:
        return None


# ---- S-01 Login (mock) ----------------------------------------------------

class LoginBody(BaseModel):
    email: str
    password: str


# ---- User-defined extra fields -------------------------------------------

class ExtraFieldBody(BaseModel):
    key: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    data_type: Optional[str] = "string"
    shared: Optional[bool] = False


@router.get("/extra-fields")
def extra_fields_list(mga: str, p: Principal = Depends(current_principal)):
    """Return every extra-field definition visible to this tenant:
    own (from tenant.internal_codes.extras) + any shared definitions from
    other tenants. Used by the Mapping page and the Output Template
    canonical-field dropdown."""
    from db import CanonicalSession
    from extras import list_definitions
    with CanonicalSession() as cs:
        tid = resolve_tenant_id(cs, p, mga)
        return list_definitions(cs, tid, include_shared=True)


@router.post("/extra-fields")
def extra_fields_create(mga: str, body: ExtraFieldBody,
                        p: Principal = Depends(current_principal)):
    from db import CanonicalSession
    from extras import upsert_definition, normalise_key
    with CanonicalSession() as cs:
        tid = resolve_tenant_id(cs, p, mga)
        result = upsert_definition(cs, tid, normalise_key(body.key),
                                   body.model_dump())
        cs.commit()
        _log(mga, _actor(p), "extra_field_saved", target=normalise_key(body.key))
        return result


@router.post("/extra-fields/{key}/adopt")
def extra_fields_adopt(mga: str, key: str, p: Principal = Depends(current_principal)):
    from db import CanonicalSession
    from extras import adopt_definition
    with CanonicalSession() as cs:
        tid = resolve_tenant_id(cs, p, mga)
        res = adopt_definition(cs, tid, key)
        if res is None:
            raise HTTPException(404, "no shared extra-field with that key")
        cs.commit()
        _log(mga, _actor(p), "extra_field_adopted", target=key)
        return res


@router.get("/onboarding/status")
def onboarding_status(mga: str, p: Principal = Depends(current_principal)):
    """Drive the first-login wizard, which walks the hierarchy in order:
      1. Organization (legal name + tenant_type + currency) — MANDATORY.
      2. Programme (at least one) — MANDATORY. A broker's reach IS its
         program_broker rows, so a programme has to exist before step 3 has
         anything to attach a broker to.
      3. Broker (at least one, on a programme) — MANDATORY.
    Bordereau Setup is deliberately NOT a step: it needs a live contract, which
    is two moves further on (the broker uploads one, the carrier approves it).
    It has its own permanent home in the sidebar.
    `needs_onboarding` is true until steps 1-3 are done (or the tenant admin
    explicitly dismissed the wizard)."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, p, mga)
        tc = s.query(Tenant).filter(Tenant.id == tid).first() if tid else None
        # Tenant is considered configured once legal_name, tenant_type and
        # currency are all set. (The previous check that rejected
        # legal_name == mga was too aggressive — a real "Aurenity" tenant
        # IS named "Aurenity", so saving never flipped the flag.)
        tenant_ready = bool(
            tc and (tc.legal_name or "").strip()
            and tc.tenant_type and tc.currency
        )
        # Onboarding asks "did the user set these up in the app", so count only
        # app-created rows (is_app_managed) / real ingests (mapper_id), not the
        # BDX-ingested canonical rows.
        # The broker step ticks for any PRODUCER the carrier added — an MGA, MGU
        # or TPA counts the same as a broker. Still narrowed to those types, so
        # an unrelated party (a reinsurer, say) cannot mark the step complete
        # while the wizard lists nothing.
        has_party = bool(tid) and (s.query(exists().where(
            and_(Party.tenant_id == tid, Party.is_app_managed.is_(True),
                 func.cast(Party.party_type, String).in_(PRODUCER_PARTY_TYPES)))).scalar() or False)
        has_contract = bool(tid) and (s.query(exists().where(
            and_(Contract.tenant_id == tid, Contract.is_app_managed.is_(True)))).scalar() or False)
        has_program = bool(tid) and (s.query(exists().where(
            and_(Program.tenant_id == tid, Program.is_app_managed.is_(True)))).scalar() or False)
        has_upload = bool(tid) and (s.query(exists().where(
            and_(Upload.tenant_id == tid, Upload.mapper_id.isnot(None)))).scalar() or False)
        # Treat "BDX configured" as: either the user fully ingested a file OR
        # they approved a mapping spec (= format setup done). The onboarding
        # step is about getting the first BDX through the AI mapping flow.
        has_mapper = bool(tid) and (
            s.query(exists().where(Mapper.tenant_id == tid)).scalar() or False
        )
        # A configured Bordereau Setup = an APPROVED direct-lane format. It bundles
        # input + output + contract in one place, so it satisfies the format-setup
        # step on its own (and implies a contract + a carrier party were created).
        bordereau_ready = bool(tid) and (s.query(exists().where(
            and_(DirectFormat.tenant_id == tid, DirectFormat.approved == 1))).scalar()
            or False)
        bdx_ready = bool(has_upload or has_mapper or bordereau_ready)
        onboarding_skipped = bool(tc and tc.onboarding_skipped)
        # Mandatory: Organization + Programme — the two things the carrier
        # owns outright. A broker is a RELATIONSHIP with another firm, so it is
        # added from the Brokers screen when one exists; requiring it here only
        # made carriers invent a placeholder to escape the wizard. Bordereau
        # Setup is excluded for the same reason it always was: it needs a live
        # contract, and has its own screen.
        mandatory_done = tenant_ready and has_program
        return {
            "tenant_ready": tenant_ready,
            # Step 2 of the wizard. Named alongside the older has_program so a
            # frontend on either version reads the same fact.
            "programs_ready": bool(has_program),
            "parties_ready": bool(has_party),
            "contract_ready": bool(has_contract),
            "bordereau_ready": bordereau_ready,
            "bdx_ready": bdx_ready,
            # Backward-compat with previous keys
            "has_program": bool(has_program),
            "has_contract": bool(has_contract),
            "has_upload": bool(has_upload),
            "onboarding_skipped": onboarding_skipped,
            "needs_onboarding": not (mandatory_done or onboarding_skipped),
        }


@router.post("/onboarding/skip")
def onboarding_skip(mga: str, p: Principal = Depends(require_role("tenant_admin"))):
    """Persist that the tenant admin dismissed the first-login onboarding wizard
    ("Skip for now"), so /onboarding/status stops sending them back to /welcome
    on future logins even if Bordereau Setup is still unfinished."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, p, mga)
        tc = s.query(Tenant).filter(Tenant.id == tid).first() if tid else None
        if not tc:
            raise HTTPException(404, "tenant not found")
        tc.onboarding_skipped = True
        s.commit()
        return {"ok": True}


# --- Login brute-force throttle (per-IP and per-email, sliding window) -------
# In-memory + process-local: fine for the POC / single worker. Swap for Redis
# when running multiple workers so the window is shared.
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


@router.post("/auth/login")
def auth_login(body: LoginBody, request: Request):
    """Authenticate user and issue a short-lived access token + a 7-day refresh
    token (MULTITENANCY_AUTH_CONCEPT.md §5.1). The client stores both and sends
    the access token as `Authorization: Bearer <jwt>` on every call.

    Unknown email and wrong password return the SAME 401 (no user enumeration).
    Auto-provision-on-login was removed — creating users is an explicit admin
    action via POST /users."""
    from auth_utils import hash_password, verify_password, _is_bcrypt
    from auth_tokens import mint_access_token, mint_refresh_token
    from auth_deps import normalize_role
    email = (body.email or "").strip().lower()
    # Throttle brute force before touching the DB.
    _client_ip = request.client.host if request.client else "unknown"
    _ua = request.headers.get("user-agent")
    _rate_limit_login(f"ip:{_client_ip}", f"email:{email}")
    from audit import log_auth
    with SessionLocal() as s:
        u = s.query(AppUser).filter(AppUser.email == email).first()
        if not u or not u.password or not verify_password(body.password, u.password):
            log_auth("login_failed", actor=email, ok=False, ip=_client_ip,
                     user_agent=_ua, user_id=(u.id if u else None),
                     details={"reason": "invalid_credentials"})
            raise HTTPException(401, "invalid credentials")
        if (u.status or "active") != "active":
            log_auth("login_failed", actor=email, ok=False, ip=_client_ip,
                     user_agent=_ua, user_id=u.id, tenant_id=u.tenant_id,
                     details={"reason": "disabled"})
            raise HTTPException(403, "user disabled")
        # Auto-upgrade legacy plain-text password to bcrypt on successful login.
        if not _is_bcrypt(u.password):
            u.password = hash_password(body.password)
            s.commit()
        # Record the successful sign-in time (shown on the Users & Roles screen).
        u.last_login_at = datetime.utcnow()
        s.commit()
        log_auth("login_success", actor=u.email, ok=True, ip=_client_ip,
                 user_agent=_ua, user_id=u.id, tenant_id=u.tenant_id,
                 details={"role": normalize_role(u.role), "full_name": u.full_name})
        # `mga` is the tenant_name the frontend still keys requests on.
        t = s.query(Tenant).filter(Tenant.id == u.tenant_id).first() if u.tenant_id else None
        role = normalize_role(u.role)
        return {
            "access_token":  mint_access_token(u.id, u.tenant_id, role),
            "refresh_token": mint_refresh_token(u.id),
            "token_type":    "bearer",
            "id": u.id, "email": u.email, "full_name": u.full_name,
            "tenant_id": u.tenant_id,
            "role": role, "mga": t.tenant_name if t else None,
        }


class RefreshBody(BaseModel):
    refresh_token: str


@router.post("/auth/refresh")
def auth_refresh(body: RefreshBody, request: Request):
    """Mint a fresh access token from a valid refresh token (§5.3). Role and
    tenant are re-read from the DB so mid-session changes take effect. The
    refresh token is NOT rotated: it keeps its original 7-day expiry, so the
    session ends 7 days after login and the client must then re-login."""
    from auth_tokens import decode_refresh_token, mint_access_token
    from auth_deps import normalize_role
    from jose import JWTError
    try:
        claims = decode_refresh_token(body.refresh_token)
    except JWTError:
        raise HTTPException(401, "invalid or expired refresh token")
    with SessionLocal() as s:
        u = s.get(AppUser, int(claims["sub"]))
        if not u or (u.status or "active") != "active":
            raise HTTPException(401, "user not found or disabled")
        # Audit the refresh, but never let it gate the token mint: this endpoint
        # sits in front of user-facing requests, and an audit INSERT+commit is
        # the most expensive thing in an otherwise cheap token mint. A failed
        # write must not turn a valid refresh into a 500 and log the user out.
        try:
            from audit import log_auth
            log_auth("token_refresh", actor=u.email, ok=True,
                     ip=(request.client.host if request.client else None),
                     user_agent=request.headers.get("user-agent"),
                     user_id=u.id, tenant_id=u.tenant_id,
                     details={"email": u.email, "method": "refresh_token"})
        except Exception:                                   # noqa: BLE001
            pass
        return {
            "access_token": mint_access_token(u.id, u.tenant_id, normalize_role(u.role)),
            "token_type":   "bearer",
        }


class LogoutBody(BaseModel):
    refresh_token: Optional[str] = None


@router.post("/auth/logout")
def auth_logout(request: Request, body: Optional[LogoutBody] = None):
    """Stateless logout. Tokens are self-contained (no server-side session), so
    the client simply discards them. Kept as an endpoint so revocable refresh
    records can be wired in later without a frontend change.

    Resolves the acting user for the audit row from the Bearer access token; if
    the client already dropped it, falls back to the refresh token in the body,
    so the logout record carries the same user context (user_id / tenant_id /
    actor / user_agent) as the login record."""
    from audit import log_auth, actor_from_token, actor_email
    _ua = request.headers.get("user-agent")
    _uid, _tid = actor_from_token(request.headers.get("authorization", ""))
    if _uid is None and body and body.refresh_token:
        try:
            from auth_tokens import decode_refresh_token
            claims = decode_refresh_token(body.refresh_token)
            _uid = int(claims["sub"])
            with SessionLocal() as s:
                _u = s.get(AppUser, _uid)
                _tid = _u.tenant_id if _u else None
        except Exception:
            pass
    log_auth("logout", actor=actor_email(_uid), ok=True,
             ip=(request.client.host if request.client else None),
             user_agent=_ua, user_id=_uid, tenant_id=_tid,
             details={"method": "user_initiated"})
    return {"ok": True}


# ---- Password reset ------------------------------------------------------

class ForgotBody(BaseModel):
    email: str


class ResetBody(BaseModel):
    token: str
    password: str


@router.post("/auth/forgot")
def auth_forgot(body: ForgotBody, request: Request):
    """Start a password reset: generate a 30-minute token, email the user a
    reset link. Always returns 200 regardless of whether the email matches an
    account (no user enumeration)."""
    import os
    import secrets
    email = (body.email or "").strip().lower()
    _uid = _tid = None
    with SessionLocal() as s:
        u = s.query(AppUser).filter(AppUser.email == email).first()
        if u:
            _uid, _tid = u.id, u.tenant_id
            token = secrets.token_urlsafe(32)
            u.reset_token = token
            u.reset_token_expires = datetime.now(timezone.utc) + timedelta(minutes=30)
            s.commit()
            base = os.getenv("APP_BASE_URL", "http://localhost:5173").rstrip("/")
            link = f"{base}/reset?token={token}"
            name = (u.full_name or "").strip()
            try:
                from email_utils import send_email, reset_email_html
                send_email(
                    email, "Reset your Kavachio password",
                    reset_email_html(link, name),
                    text=(f"Hi {name}, " if name else "")
                    + f"reset your Kavachio password (expires in 30 minutes): {link}",
                )
            except Exception as e:
                import logging
                logging.getLogger("bdx.email").warning(
                    "password-reset email to %s failed: %s", email, e)
    from audit import log_auth
    log_auth("forgot_request", actor=email, ok=True,
             ip=(request.client.host if request.client else None),
             user_agent=request.headers.get("user-agent"),
             user_id=_uid, tenant_id=_tid, details={"matched": _uid is not None})
    return {"ok": True}


@router.get("/auth/reset/validate")
def auth_reset_validate(token: str):
    """Check whether a reset token is still valid (exists + not expired) WITHOUT
    consuming it — so the reset page can show an 'expired' message on load
    instead of the set-password form.

    Also reports which flow issued the token — 'invite' (first-time onboarding,
    the account's real status is still "invited") vs 'reset' (an existing
    active account requested a password reset) — so the frontend can tailor
    its copy without guessing from the URL or token shape."""
    with SessionLocal() as s:
        u = s.query(AppUser).filter(AppUser.reset_token == token).first() if token else None
        exp = u.reset_token_expires if u else None
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        valid = bool(u and exp and exp >= datetime.now(timezone.utc))
        mode = "invite" if (valid and u.status == "invited") else "reset"
        return {"valid": valid, "mode": mode}


@router.post("/auth/reset")
def auth_reset(body: ResetBody, request: Request):
    """Complete a password reset: validate the token (exists + not expired),
    set the new bcrypt password, and clear the token."""
    import re
    from auth_utils import hash_password
    pw = body.password or ""
    if not (len(pw) >= 8 and re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw)
            and re.search(r"\d", pw) and re.search(r"[^A-Za-z0-9]", pw)):
        raise HTTPException(
            400,
            "Password must be at least 8 characters and include an uppercase letter, "
            "a lowercase letter, a number, and a special character.",
        )
    with SessionLocal() as s:
        u = s.query(AppUser).filter(AppUser.reset_token == body.token).first()
        exp = u.reset_token_expires if u else None
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if not u or exp is None or exp < datetime.now(timezone.utc):
            raise HTTPException(400, "This reset link is invalid or has expired.")
        u.password = hash_password(body.password)
        u.reset_token = None
        u.reset_token_expires = None
        u.status = "active"
        s.commit()
        _log(_tenant_name(s, u.tenant_id), u.email, "password_reset", target=str(u.id),
             details={"email": u.email, "method": "reset_token"})
        from audit import log_auth
        log_auth("password_reset", actor=u.email, ok=True,
                 ip=(request.client.host if request.client else None),
                 user_agent=request.headers.get("user-agent"),
                 user_id=u.id, tenant_id=u.tenant_id,
                 details={"email": u.email, "method": "reset_token"})
        return {"ok": True}


# ---- User invites (tokened "complete onboarding" link — reuses the reset page, in invite mode) ----

def _tenant_display(s, tenant_id) -> Optional[str]:
    """Human org name for invite emails: legal_name, else the tenant code."""
    if not tenant_id:
        return None
    t = s.query(Tenant).filter(Tenant.id == tenant_id).first()
    return (t.legal_name or t.tenant_name) if t else None


def _make_invite_link(user, days: int = 7) -> str:
    """Stamp a fresh invite token on the user (status → invited) and return the
    tokened set-password URL (same /reset page as password reset). The caller
    is responsible for committing the session."""
    import os
    import secrets
    token = secrets.token_urlsafe(32)
    user.reset_token = token
    user.reset_token_expires = datetime.now(timezone.utc) + timedelta(days=days)
    user.status = "invited"
    base = os.getenv("APP_BASE_URL", "http://localhost:5173").rstrip("/")
    return f"{base}/reset?token={token}"


def _send_invite_email(email: str, link: str, name: Optional[str],
                       org: Optional[str] = None) -> None:
    """Best-effort invite email — never raises into the request."""
    try:
        from email_utils import send_email, invite_email_html
        nm = (name or "").strip()
        send_email(
            email, "You're invited to Kavachio",
            invite_email_html(link, nm, org),
            text=(f"Hi {nm}, " if nm else "")
            + f"you've been invited to Kavachio. Set up your password and Complete your onboarding (expires in 7 days): {link}",
        )
    except Exception as e:
        import logging
        logging.getLogger("bdx.email").warning("invite email to %s failed: %s", email, e)


# ---- Self-service: change password (signed-in user) ----------------------

class ChangePasswordBody(BaseModel):
    user_id: int
    current_password: str
    new_password: str


@router.post("/auth/change-password")
def auth_change_password(body: ChangePasswordBody, request: Request):
    """Change the signed-in user's own password.

    Verifies the current password, enforces the same policy as /auth/reset,
    and stores the new bcrypt hash. (No auth token in this POC — the caller
    passes its own user_id; the current-password check is what authorizes it.)
    """
    import re
    from auth_utils import hash_password, verify_password
    new = body.new_password or ""
    if not (len(new) >= 8 and re.search(r"[A-Z]", new) and re.search(r"[a-z]", new)
            and re.search(r"\d", new) and re.search(r"[^A-Za-z0-9]", new)):
        raise HTTPException(
            400,
            "Password must be at least 8 characters and include an uppercase letter, "
            "a lowercase letter, a number, and a special character.",
        )
    with SessionLocal() as s:
        u = s.get(AppUser, body.user_id)
        if not u:
            raise HTTPException(404, "user not found")
        # A user with a password set must prove they know the current one.
        if u.password and not verify_password(body.current_password, u.password):
            raise HTTPException(400, "Your current password is incorrect.")
        u.password = hash_password(body.new_password)
        s.commit()
        _log(_tenant_name(s, u.tenant_id), u.email, "password_changed", target=str(u.id),
             details={"email": u.email, "method": "self_service"})
        from audit import log_auth
        log_auth("password_changed", actor=u.email, ok=True,
                 ip=(request.client.host if request.client else None),
                 user_agent=request.headers.get("user-agent"),
                 user_id=u.id, tenant_id=u.tenant_id,
                 details={"email": u.email, "method": "self_service"})
        return {"ok": True}


# ---- S-02 Tenant Configuration -------------------------------------------

class TenantBody(BaseModel):
    legal_name: Optional[str] = None
    tenant_type: Optional[str] = None
    address: Optional[dict] = None
    currency: Optional[str] = None
    logo: Optional[str] = None
    internal_codes: Optional[dict] = None


def _tenant_dict(t: Tenant) -> dict:
    # `mga` in the response is the tenant_name the frontend keys on. `name`/
    # `code`/`is_active` mirror the Tenants-list row shape so the single-tenant
    # fetch (TenantDetail) can render the header without pulling the whole list.
    return {"id": t.id, "mga": t.tenant_name, "legal_name": t.legal_name,
            "name": t.legal_name or (t.tenant_name or "").title(),
            "code": t.tenant_name, "is_active": bool(t.is_active),
            "tenant_type": t.tenant_type, "address": t.address,
            "currency": t.currency, "logo": t.logo,
            "internal_codes": t.internal_codes,
            "created_at": _iso_utc(t.created_at),
            "modified_at": _iso_utc(t.modified_at)}


def _get_or_create_tenant(s, mga: str) -> Tenant:
    from ingester import _ensure_tenant
    tid = _ensure_tenant(s, mga)
    return s.query(Tenant).filter(Tenant.id == tid).first()


@router.get("/tenants")
def tenants_list(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=200),
    q: Optional[str] = None,
    tenant_type: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    _p: Principal = Depends(require_role("kavachio_admin")),
):
    """List tenants for the platform-admin "Tenants" screen, TRUE server-side:
    search / type / status / date filters are applied in SQL and only one page
    of rows is returned, with the matching total. Platform-admin only.

    Returns {"items", "total", "page", "page_size"}. The "status" filter mirrors
    the UI's derived status: inactive (is_active=0), invited (active + has a
    pending invite), active (active + no pending invite)."""
    with SessionLocal() as s:
        # Per-tenant pending-invite and active-user counts drive the derived
        # "Invited" status. A broker is "invited" ONLY while nobody has signed
        # in yet (pending invites AND zero active users); once even one user is
        # active, further invites no longer flip it out of "active". One grouped
        # subquery each, left-joined, instead of per-row COUNTs.
        pend_sq = (
            s.query(AppUser.tenant_id.label("tid"),
                    func.count(AppUser.id).label("pending"))
            .filter(AppUser.role != "kavachio_admin",
                    AppUser.status.in_(("invited", "pending")))
            .group_by(AppUser.tenant_id).subquery())
        pending_col = func.coalesce(pend_sq.c.pending, 0)
        active_sq = (
            s.query(AppUser.tenant_id.label("tid"),
                    func.count(AppUser.id).label("actives"))
            .filter(AppUser.role != "kavachio_admin",
                    AppUser.status == "active")
            .group_by(AppUser.tenant_id).subquery())
        active_col = func.coalesce(active_sq.c.actives, 0)

        query = (s.query(Tenant, pending_col.label("pending"), active_col.label("actives"))
                 .outerjoin(pend_sq, pend_sq.c.tid == Tenant.id)
                 .outerjoin(active_sq, active_sq.c.tid == Tenant.id))

        if q and q.strip():
            like = f"%{q.strip().lower()}%"
            query = query.filter(or_(
                func.lower(func.coalesce(Tenant.legal_name, "")).like(like),
                func.lower(func.coalesce(Tenant.tenant_name, "")).like(like)))
        if tenant_type:
            query = query.filter(Tenant.tenant_type == tenant_type)
        if status == "inactive":
            query = query.filter(Tenant.is_active.is_(False))
        elif status == "invited":
            query = query.filter(Tenant.is_active.is_(True),
                                 pending_col > 0, active_col == 0)
        elif status == "active":
            query = query.filter(Tenant.is_active.is_(True),
                                 or_(active_col > 0, pending_col == 0))
        df, dt_ = _parse_client_dt(date_from), _parse_client_dt(date_to)
        if df:
            query = query.filter(Tenant.created_at >= df)
        if dt_:
            query = query.filter(Tenant.created_at <= dt_)

        total = query.order_by(None).count()
        rows = (query.order_by(Tenant.tenant_name)
                .offset((page - 1) * page_size).limit(page_size).all())

        items = []
        for t, pending, actives in rows:
            # kavachio_admin is a cross-tenant platform role, not a member of
            # this org — never counted as one of the tenant's own users, even
            # when a legacy row happens to carry this tenant's tenant_id.
            users = s.query(func.count(AppUser.id)).filter(
                AppUser.tenant_id == t.id,
                AppUser.role != "kavachio_admin").scalar() or 0
            setups = s.query(func.count(DirectFormat.id)).filter(
                DirectFormat.tenant_id == t.id, DirectFormat.approved == 1).scalar() or 0
            # What the carrier has built for itself. Kavachio creates neither —
            # they are the proof that its own admin got started.
            programmes = s.query(func.count(Program.id)).filter(
                Program.tenant_id == t.id).scalar() or 0
            brokers = s.query(func.count(Party.id)).filter(
                Party.tenant_id == t.id,
                func.cast(Party.party_type, String) == "broker").scalar() or 0
            items.append({
                "mga": t.tenant_name,
                "name": t.legal_name or (t.tenant_name or "").title(),
                "code": t.tenant_name,
                "tenant_type": t.tenant_type,
                "users": int(users),
                "setups": int(setups),
                "programmes": int(programmes),
                "brokers": int(brokers),
                "is_active": bool(t.is_active),
                "pending_invites": int(pending or 0),
                "active_users": int(actives or 0),
                "created_at": _iso_utc(t.created_at),
            })
        return {"items": items, "total": int(total),
                "page": page, "page_size": page_size}


class NewTenantBody(BaseModel):
    name: str
    tenant_type: Optional[str] = None
    currency: Optional[str] = None
    is_active: Optional[bool] = True
    admin_name: Optional[str] = None
    admin_email: Optional[str] = None


# Carriers and reinsurers aren't provisioned as their own platform tenant —
# they're onboarded as Party directory entries under an MGA/MGU/broker/TPA's
# tenant instead (see Parties/AddParty). Mirrors the Add Tenant screen's
# dropdown options (frontend/src/pages/AddTenant.tsx); only enforced at
# creation — existing tenants provisioned before this restriction are
# untouched.
# Kavachio sets up carriers, and only carriers. A broker is not a tenant at
# all — it is a party a carrier adds on its own programmes, so it can never
# be provisioned from here.
NEW_TENANT_TYPES = {"carrier"}
NEW_TENANT_CURRENCIES = {"USD", "GBP", "EUR", "CAD", "AUD"}


@router.post("/tenants")
def tenants_create(body: NewTenantBody,
                   _p: Principal = Depends(require_role("kavachio_admin"))):
    """Provision a new tenant (Kavachio "Add tenant" screen) and, optionally,
    invite its first admin. The account code (tenant_name) is auto-slugged from
    the organization name. Platform-admin only."""
    import re
    from ingester import _ensure_tenant
    if body.tenant_type and body.tenant_type not in NEW_TENANT_TYPES:
        raise HTTPException(422, f"tenant_type must be one of {sorted(NEW_TENANT_TYPES)}")
    if body.currency and body.currency not in NEW_TENANT_CURRENCIES:
        raise HTTPException(422, f"currency must be one of {sorted(NEW_TENANT_CURRENCIES)}")
    slug = re.sub(r"[^a-z0-9]+", "-", (body.name or "").strip().lower()).strip("-") or "tenant"
    with SessionLocal() as s:
        if s.query(Tenant).filter(Tenant.tenant_name == slug).first():
            raise HTTPException(409, "a tenant with a similar name already exists")
        # Emails are unique platform-wide (enforced the same way in POST
        # /users) — checked before creating anything, so a taken admin email
        # never leaves behind a broker with no admin invited.
        if body.admin_email and s.query(AppUser).filter(
                AppUser.email == body.admin_email.strip().lower()).first():
            raise HTTPException(409, "This email is already in use — not able to create again")
        # _ensure_tenant supplies the canonical NOT NULL defaults.
        tid = _ensure_tenant(s, slug)
        s.flush()
        t = s.query(Tenant).filter(Tenant.id == tid).first()
        t.legal_name = body.name.strip()
        if body.tenant_type:
            t.tenant_type = body.tenant_type
        if body.currency:
            t.currency = body.currency
        t.is_active = bool(body.is_active if body.is_active is not None else True)
        admin_user = None
        if body.admin_email:
            admin_user = AppUser(
                email=body.admin_email.strip().lower(),
                full_name=body.admin_name or body.admin_email.split("@")[0].title(),
                # 'carrier_admin', not the legacy 'admin' — the column only
                # accepts the four role names (chk_app_user_role).
                role="carrier_admin", status="invited", tenant_id=tid,
                # Kavachio is inviting this person, and every login has to say
                # who let it in (trg_enforce_invitation_chain). This is the one
                # place a carrier's FIRST admin is created, so the platform
                # admin doing it is the answer.
                invited_by_user_id=_p.user_id)
            s.add(admin_user)
        # Invite the first admin with a tokened set-password link.
        invite_link = _make_invite_link(admin_user) if admin_user else None
        s.commit(); s.refresh(t)
        if admin_user and invite_link:
            _send_invite_email(admin_user.email, invite_link, admin_user.full_name,
                               t.legal_name or t.tenant_name)
        _log(slug, _actor(_p), "tenant_created", target=slug,
             details={"name": t.legal_name, "tenant_id": t.id})
        return _tenant_dict(t)


@router.post("/tenants/{mga}/resend-invite")
def tenant_resend_invite(mga: str,
                         _p: Principal = Depends(require_role("kavachio_admin"))):
    """Re-issue this broker's onboarding invite (Brokers list -> "Resend Link").

    Without this, a broker whose invite mail was deleted / lost / expired before
    they onboarded had NO way back in: they have no password to reset, so
    "Forgot password" can't help them either, and the Kavachio admin could only
    delete and re-create the org. Every user of this tenant still sitting on a
    pending invite gets a FRESH token + set-password email; stamping a new token
    invalidates the previous link, so an old copy can't be used afterwards.

    Platform-admin only — it is driven from the platform-admin Brokers screen.
    (Per-USER resend lives at POST /users/{user_id}/resend-invite.)
    """
    with SessionLocal() as s:
        tid = _get_tenant_id(s, mga)
        if tid is None:
            raise HTTPException(404, "broker not found")
        # kavachio_admin is a cross-tenant platform role, never one of this
        # org's own members — same exclusion the /tenants list counts use, so
        # the link the UI shows and the users we mail are the same set.
        pending = (s.query(AppUser)
                   .filter(AppUser.tenant_id == tid,
                           AppUser.role != "kavachio_admin",
                           AppUser.status.in_(("invited", "pending")))
                   .order_by(AppUser.id).all())
        if not pending:
            raise HTTPException(409, "this broker has no pending invite to resend")
        org = _tenant_display(s, tid)
        # Stamp every fresh token and commit ONCE, then send. _send_invite_email
        # is best-effort and never raises, so sending after the commit can't
        # leave a user holding a token that was rolled back.
        recipients = [(u.email, u.full_name, _make_invite_link(u)) for u in pending]
        s.commit()
        for email, name, link in recipients:
            _send_invite_email(email, link, name, org)
        emails = [e for e, _n, _l in recipients]
        _log(mga, _actor(_p), "invite_resent", target=mga,
             details={"emails": emails, "count": len(emails), "scope": "tenant"})
        return {"ok": True, "sent": len(emails), "emails": emails}


@router.get("/tenants/{mga}")
def tenant_get(mga: str, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        t = _get_or_create_tenant(s, mga)
        # A tenant_admin can only read their OWN tenant; kavachio_admin any.
        # (Tenant maps its PK column `tenant_id` to the ORM attr `.id`.)
        assert_tenant_owns(principal, t.id)
        s.commit(); s.refresh(t)
        return _tenant_dict(t)


@router.put("/tenants/{mga}")
def tenant_update(mga: str, body: TenantBody,
                  principal: Principal = Depends(require_role("tenant_admin"))):
    with SessionLocal() as s:
        t = _get_or_create_tenant(s, mga)
        # Edits are limited to the caller's own tenant; kavachio_admin any.
        # (Tenant maps its PK column `tenant_id` to the ORM attr `.id`.)
        assert_tenant_owns(principal, t.id)
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(t, k, v)
        s.commit(); s.refresh(t)
        _log(mga, _actor(principal), "tenant_updated", target=mga,
             details={"changed": sorted(body.model_dump(exclude_unset=True).keys())})
        return _tenant_dict(t)


# ---- S-03 Party Directory + S-03a Party Configuration --------------------

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


# The organisations a carrier can put on a programme and receive business from.
# Named once here because three places have to agree on it: this module's
# onboarding check, hierarchy_routes._assert_broker, and the wizard's Type
# dropdown. `program_broker.broker_party_id` keeps its column name for history;
# "broker" there means "the producer on this programme", whichever of these it is.
PRODUCER_PARTY_TYPES = ("mga", "mgu", "broker", "tpa")


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


@router.get("/my-carrier-party")
def my_carrier_party(mga: str, p: Principal = Depends(current_principal)):
    """The carrier party that IS this tenant.

    In the carrier-centric model a tenant no longer picks which carrier it is
    writing for — it IS the carrier. Screens that still have to store a
    carrier_party_id (Bordereau Setup, pipelines, fingerprints) ask here instead
    of showing a dropdown. Find-or-create, keyed on the stable natural id
    `carrier::<tenant_id>`, so a tenant has exactly one of these forever and a
    renamed organisation never spawns a second.
    """
    from ingester import _ensure_carrier_party
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, p, mga)
        party_id = _ensure_carrier_party(s, tid)
        if party_id is None:
            raise HTTPException(500, "could not resolve this carrier's own party record")
        s.commit()
        party = s.get(Party, party_id)
        tenant = s.query(Tenant).filter(Tenant.id == tid).first()
        # The placeholder is created as "Tenant <id> Carrier". Once the org has
        # a real legal name, show that instead — same row, better label.
        wanted = (tenant.legal_name or "").strip() if tenant else ""
        if wanted and party is not None and party.legal_name != wanted:
            party.legal_name = wanted
            s.commit()
        return {"id": party_id,
                "legal_name": (party.legal_name if party else None) or mga}


@router.get("/parties")
def parties_list(
    mga: str,
    q: Optional[str] = None,
    party_type: Optional[str] = None,
    scope: Optional[str] = None,
    include_inactive: bool = False,
    is_active: Optional[bool] = None,
    page: Optional[int] = Query(None, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=200),
    p: Principal = Depends(current_principal),
):
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, p, mga)
        # Directory shows app-created parties only (+ shared globals); BDX-ingested
        # canonical parties are excluded via is_app_managed (was the old mga filter).
        query = s.query(Party).filter(or_(
            and_(Party.tenant_id == tid, Party.is_app_managed.is_(True)),
            Party.scope == "global"))
        if is_active is not None:
            # An explicit status pick (the directory's Status filter) — narrow to
            # exactly that value, server-side, so it composes correctly with
            # true pagination (unlike the old client-side post-filter, which
            # filtered an already-sliced page and could show fewer rows than
            # the page size with no indication more matches existed elsewhere).
            query = query.filter(Party.is_active.is_(True), Party.is_active.is_(None)) \
                if is_active else query.filter(Party.is_active.is_(False))
        elif not include_inactive:
            # Selectors must never offer a deactivated party, so active-only is the
            # default; the directory opts in to list (and re-activate) them. NULL
            # predates the column default and still counts as active.
            query = query.filter(or_(Party.is_active.is_(True),
                                     Party.is_active.is_(None)))
        if q:
            ql = f"%{q.lower()}%"
            query = query.filter(or_(
                func.lower(Party.legal_name).like(ql),
                func.lower(Party.dba_name).like(ql),
            ))
        if party_type:
            query = query.filter(Party.party_type == party_type)
        if scope:
            query = query.filter(Party.scope == scope)

        total = query.order_by(None).count()
        from_global = query.filter(Party.scope == "global").order_by(None).count()

        ordered = query.order_by(Party.legal_name)
        # Pagination is opt-in: selector dropdowns all over the app call this
        # endpoint expecting every match (no page param) — only slice when the
        # caller explicitly asks for a page, so those callers don't silently
        # start getting truncated.
        if page is not None:
            size = page_size or 10
            ordered = ordered.offset((page - 1) * size).limit(size)
        rows = [_party_dict(p, mga if p.scope != "global" else None) for p in ordered.all()]
        return {"total": total, "from_global": from_global, "items": rows,
                "page": page, "page_size": page_size}


@router.post("/parties")
def parties_create(mga: str, body: PartyBody,
                   principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        # Tenant comes from the trusted token, never the client. (For a logged-in
        # user the tenant row already exists, so no _ensure_tenant create is
        # needed; the party_scope_tenant_chk constraint is satisfied.)
        tenant_id = resolve_tenant_id(s, principal, mga)
        p = Party(tenant_id=tenant_id,
                  **body.model_dump(exclude_unset=True))
        s.add(p); s.commit(); s.refresh(p)
        _log(mga, _actor(principal), "party_created", target=str(p.id),
             details={"legal_name": p.legal_name, "party_type": p.party_type, "scope": p.scope})
        return _party_dict(p, mga)


@router.get("/parties/{party_id}")
def parties_get(party_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        p = s.get(Party, party_id)
        if not p:
            raise HTTPException(404, "party not found")
        assert_tenant_owns(principal, p.tenant_id)
        return _party_dict(p, _tenant_name(s, p.tenant_id))


@router.put("/parties/{party_id}")
def parties_update(party_id: int, body: PartyBody,
                   principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        p = s.get(Party, party_id)
        if not p:
            raise HTTPException(404, "party not found")
        assert_tenant_owns(principal, p.tenant_id)
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(p, k, v)
        s.commit(); s.refresh(p)
        mga = _tenant_name(s, p.tenant_id)
        _log(mga, _actor(principal), "party_updated", target=str(p.id),
             details={"legal_name": p.legal_name, "party_type": p.party_type,
                      "changed": sorted(body.model_dump(exclude_unset=True).keys())})
        return _party_dict(p, mga)


class PartyContactBody(BaseModel):
    full_name: str
    title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


@router.get("/parties/{party_id}/contacts")
def party_contacts_list(party_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        party = s.get(Party, party_id)
        if not party:
            raise HTTPException(404, "party not found")
        assert_tenant_owns(principal, party.tenant_id)
        rows = s.query(PartyContact).filter(PartyContact.party_id == party_id).all()
        return [{"id": c.id, "full_name": c.full_name, "title": c.title,
                 "email": c.email, "phone": c.phone} for c in rows]


@router.post("/parties/{party_id}/contacts")
def party_contacts_create(party_id: int, body: PartyContactBody,
                          principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        p = s.get(Party, party_id)
        if not p:
            raise HTTPException(404, "party not found")
        assert_tenant_owns(principal, p.tenant_id)
        c = PartyContact(party_id=party_id, tenant_id=p.tenant_id, **body.model_dump())
        s.add(c); s.commit(); s.refresh(c)
        try:
            from audit import log_activity, actor_email
            log_activity(p.tenant_id, actor_email(principal.user_id), "party_contact_added",
                         target=f"party:{party_id}",
                         details={
                             "party_id": party_id,
                             "contact_id": c.id,
                             "full_name": c.full_name,
                             "email": c.email,
                             "title": c.title,
                         })
        except Exception:  # noqa: BLE001
            pass
        return {"id": c.id, "full_name": c.full_name, "title": c.title,
                "email": c.email, "phone": c.phone}


@router.delete("/parties/{party_id}/contacts/{contact_id}")
def party_contacts_delete(party_id: int, contact_id: int,
                          principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        c = s.get(PartyContact, contact_id)
        if not c or c.party_id != party_id:
            raise HTTPException(404, "contact not found")
        assert_tenant_owns(principal, c.tenant_id)
        _c_tenant_id = c.tenant_id
        s.delete(c); s.commit()
        try:
            from audit import log_activity, actor_email
            log_activity(_c_tenant_id, actor_email(principal.user_id), "party_contact_removed",
                         target=f"party:{party_id}",
                         details={
                             "party_id": party_id,
                             "contact_id": contact_id,
                         })
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True}


@router.get("/parties/{party_id}/programs")
def party_programs_list(party_id: int, principal: Principal = Depends(current_principal)):
    """Return programs linked to this party, each with their contracts list."""
    with SessionLocal() as s:
        party = s.get(Party, party_id)
        if not party:
            raise HTTPException(404, "party not found")
        assert_tenant_owns(principal, party.tenant_id)
        programs = s.query(Program).filter(Program.party_id == party_id).order_by(Program.name).all()
        result = []
        for p in programs:
            contracts = s.query(Contract).filter(Contract.program_id == p.id).all()
            pd = _program_dict(p, _tenant_name(s, p.tenant_id))
            pd["contracts"] = [
                {"id": c.id, "filename": c.filename, "status": c.status,
                 "created_at": _iso_utc(c.created_at)}
                for c in contracts
            ]
            result.append(pd)
        return result


# ---- S-05 Program & Contract Setup ---------------------------------------

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
    # canonical_program_id: Optional[int] = None


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
            # "canonical_program_id": p.canonical_program_id}


@router.get("/programs")
def programs_list(mga: str, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        return [_program_dict(p, mga) for p in
                s.query(Program).filter(Program.tenant_id == tid,
                                        Program.is_app_managed.is_(True))
                .order_by(Program.name).all()]


@router.post("/programs")
def programs_create(mga: str, body: ProgramBody,
                    principal: Principal = Depends(current_principal)):
    if not body.name:
        raise HTTPException(400, "name required")
    with SessionLocal() as s:
        p = Program(tenant_id=resolve_tenant_id(s, principal, mga),
                    **body.model_dump(exclude_unset=True))
        s.add(p); s.commit(); s.refresh(p)
        _log(mga, _actor(principal), "program_created", target=str(p.id),
             details={"name": p.name})
        return _program_dict(p, mga)


@router.get("/programs/{program_id}")
def programs_get(program_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        p = s.get(Program, program_id)
        if not p:
            raise HTTPException(404, "program not found")
        assert_tenant_owns(principal, p.tenant_id)
        return _program_dict(p, _tenant_name(s, p.tenant_id))


@router.put("/programs/{program_id}")
def programs_update(program_id: int, body: ProgramBody,
                    principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        p = s.get(Program, program_id)
        if not p:
            raise HTTPException(404, "program not found")
        assert_tenant_owns(principal, p.tenant_id)
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(p, k, v)
        s.commit(); s.refresh(p)
        mga = _tenant_name(s, p.tenant_id)
        _log(mga, _actor(principal), "program_updated", target=str(p.id),
             details={"name": p.name, "changed": sorted(body.model_dump(exclude_unset=True).keys())})
        return _program_dict(p, mga)


# ---- Group 3: submission calendar (own-deadline reminders) ------------------

class ScheduleBody(BaseModel):
    # All optional — a PUT only sets what it sends (override → contract fallback).
    # No `grace_days`: there is no grace period. An older client still sending it
    # is ignored rather than rejected (pydantic drops unknown fields), which is
    # the right outcome — the field no longer has any effect to honour.
    frequency_override: Optional[str] = None       # 'weekly'|'monthly'|'quarterly'
    anchor_date_override: Optional[date] = None
    due_day_of_month: Optional[int] = None         # monthly/quarterly deadline
    due_offset_days: Optional[int] = None          # weekly deadline
    soon_window_days: Optional[int] = None
    contract_id: Optional[int] = None


def _schedule_dict(sched: Optional[SubmissionSchedule], resolved, reason=None,
                   contract_frequency=None, contract_anchor=None) -> dict:
    """Serialise a schedule + whether it resolves. `resolved` is a ResolvedSchedule
    or None; when None the UI shows the 'set it up' prompt (reason says what's missing).
    contract_frequency/anchor tell the UI what the contract supplies, so 'Save' can be
    enabled when the schedule would resolve even without a manual override."""
    base = {
        "exists": sched is not None,
        "resolved": resolved is not None,
        "reason": reason,
        "contract_frequency": contract_frequency,
        "contract_anchor": contract_anchor.isoformat()
            if hasattr(contract_anchor, "isoformat") else contract_anchor,
    }
    if sched is not None:
        base.update({
            "program_id": sched.program_id,
            "contract_id": sched.contract_id,
            "frequency_override": sched.frequency_override,
            "anchor_date_override": sched.anchor_date_override.isoformat()
                if sched.anchor_date_override else None,
            "due_day_of_month": sched.due_day_of_month,
            "due_offset_days": sched.due_offset_days,
            "soon_window_days": sched.soon_window_days,
        })
    if resolved is not None:
        base.update({
            "effective_frequency": resolved.frequency,
            "effective_anchor": resolved.anchor.isoformat(),
        })
    return base


@router.get("/programs/{program_id}/schedule")
def program_schedule_get(program_id: int, principal: Principal = Depends(current_principal)):
    """The program's submission schedule + whether it resolves into a calendar.

    C-5: when the contract supplies BOTH a frequency and a start date, the
    calendar is built here on first view rather than waiting for someone to press
    Save — that is what "auto-build" means. Every override column is left NULL,
    so C-6 still wins whenever ops set one, and a corrected contract still flows
    through. When either half is missing nothing is written and the UI shows its
    "No calendar yet" prompt instead of guessed deadlines.
    """
    from submission_calendar_service import (
        resolve_for_schedule, _unresolved_reason, program_contract_basis,
        materialize_schedule,
    )
    from submission_calendar import _norm_freq
    with SessionLocal() as s:
        p = s.get(Program, program_id)
        if not p:
            raise HTTPException(404, "program not found")
        assert_tenant_owns(principal, p.tenant_id)
        c_freq, c_anchor = program_contract_basis(s, program_id)
        sched = (s.query(SubmissionSchedule)
                 .filter(SubmissionSchedule.program_id == program_id).first())
        if sched is None:
            # `_norm_freq` (not truthiness) decides "usable": it is the same check
            # resolve_schedule() applies, so a frequency it cannot build from —
            # 'annual', say — falls through to the prompt rather than creating a
            # schedule row that could never resolve.
            if not (_norm_freq(c_freq) and c_anchor):
                return _schedule_dict(None, None, reason="not_set",
                                      contract_frequency=c_freq, contract_anchor=c_anchor)
            sched = SubmissionSchedule(tenant_id=p.tenant_id, program_id=program_id)
            s.add(sched)
            try:
                s.flush()
            except IntegrityError:
                # Two first views at once; program_id is unique. Reuse whichever
                # row won rather than failing the read.
                s.rollback()
                sched = (s.query(SubmissionSchedule)
                         .filter(SubmissionSchedule.program_id == program_id).first())
            if sched is not None:
                materialize_schedule(s, sched)
                s.commit(); s.refresh(sched)
            if sched is None:
                return _schedule_dict(None, None, reason="not_set",
                                      contract_frequency=c_freq, contract_anchor=c_anchor)
        resolved, program, contract = resolve_for_schedule(s, sched)
        reason = None if resolved else _unresolved_reason(sched, program, contract)
        return _schedule_dict(sched, resolved, reason,
                              contract_frequency=c_freq, contract_anchor=c_anchor)


@router.put("/programs/{program_id}/schedule")
def program_schedule_put(program_id: int, body: ScheduleBody,
                         principal: Principal = Depends(current_principal)):
    """Create/update the schedule (C-6/C-8 override), then (re)build the calendar."""
    from submission_calendar_service import (
        resolve_for_schedule, materialize_schedule, _unresolved_reason,
        program_contract_basis,
    )
    with SessionLocal() as s:
        p = s.get(Program, program_id)
        if not p:
            raise HTTPException(404, "program not found")
        assert_tenant_owns(principal, p.tenant_id)
        sched = (s.query(SubmissionSchedule)
                 .filter(SubmissionSchedule.program_id == program_id).first())
        if sched is None:
            sched = SubmissionSchedule(tenant_id=p.tenant_id, program_id=program_id)
            s.add(sched); s.flush()
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(sched, k, v)
        result = materialize_schedule(s, sched)
        s.commit(); s.refresh(sched)
        resolved, program, contract = resolve_for_schedule(s, sched)
        reason = None if resolved else _unresolved_reason(sched, program, contract)
        c_freq, c_anchor = program_contract_basis(s, program_id)
        mga = _tenant_name(s, p.tenant_id)
        _log(mga, _actor(principal), "submission_schedule_updated", target=str(program_id),
             details={"resolved": result.get("resolved"), "count": result.get("count")})
        return {"schedule": _schedule_dict(sched, resolved, reason,
                                           contract_frequency=c_freq, contract_anchor=c_anchor),
                "calendar": result}


@router.get("/calendar")
def calendar_list(mga: Optional[str] = None, program_id: Optional[int] = None,
                  principal: Principal = Depends(current_principal)):
    """All expected submissions for the tenant (optionally one program), status
    recomputed fresh against today — drives the My Calendar page and Home tile.

    Also runs a lazy overdue sweep so the bell reminder appears for anyone using the
    app even without an external daily cron (idempotent — fires each reminder once)."""
    from submission_calendar_service import calendar_rows, sweep_overdue
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        try:
            sweep_overdue(s, tenant_id=tid)
            s.commit()
        except Exception as _e:   # never let the reminder path break the view
            s.rollback()
        rows = calendar_rows(s, tid, program_id=program_id)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return {"rows": rows, "counts": counts}


@router.post("/calendar/sweep")
def calendar_sweep(mga: Optional[str] = None,
                   principal: Principal = Depends(current_principal)):
    """Run the deadline sweep now — raise the due-soon / due-today / overdue bell
    reminders. Meant for a daily cron/uptime-ping; also callable manually."""
    from submission_calendar_service import sweep_overdue
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        result = sweep_overdue(s, tenant_id=tid)
        s.commit()
        return result


# @router.post("/programs/{program_id}/contracts")
# async def program_contract_upload(
#     program_id: int,
#     file: UploadFile = File(...),
# ):
#     """Upload a contract. AI extraction is stubbed — fills with placeholder
#     metadata so the UI flow is functional. Validation-rule extraction is
#     intentionally NOT performed (out of scope for this milestone)."""
#     with SessionLocal() as s:
#         prog = s.get(Program, program_id)
#         if not prog:
#             raise HTTPException(404, "program not found")
#         data = await file.read()
#         extracted = _stub_contract_extract(file.filename or "contract")
#         c = Contract(
#             program_id=program_id, mga=prog.mga,
#             filename=file.filename or "contract",
#             status="active", extracted=extracted, blob=data,
#         )
#         s.add(c)
#         # Patch program with extracted fields where empty.
#         for k, v in extracted.items():
#             if hasattr(prog, k) and not getattr(prog, k):
#                 setattr(prog, k, v)
#         if not prog.source_contract_file:
#             prog.source_contract_file = file.filename
#         s.commit(); s.refresh(c)
#         _log(prog.mga, None, "contract_uploaded", target=str(c.id),
#              details={"program_id": program_id, "filename": c.filename})
#         return {"id": c.id, "filename": c.filename, "status": c.status,
#                 "extracted": c.extracted, "program": _program_dict(prog)}

@router.post("/programs/{program_id}/contracts")
async def program_contract_upload(
    program_id: int,
    file: UploadFile = File(...),
    output_template_id: Optional[int] = Form(default=None),
    # WHICH BROKER this contract is with. A contract is a (programme x broker)
    # pair, and the hierarchy lists contracts UNDER the broker that holds them
    # (hierarchy_routes: `c.broker_party_id == party.id`). Without this the row
    # is created with a NULL broker and appears under nobody — the contract
    # exists, the setup exists, and the programme still looks empty.
    # Optional: a carrier-held contract that predates the broker level, and any
    # caller that does not know the broker, still works exactly as before.
    broker_party_id: Optional[int] = Form(default=None),
    schedule_key: Optional[str] = Form(default=None),
    reference_files: Optional[list[UploadFile]] = File(default=None),
    continue_anyway: bool = Form(default=False),
    resume_token: Optional[str] = Form(default=None),
    enable_reference_halt: bool = Form(default=False),
    upload_token: Optional[str] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    """Upload a contract PDF, optionally linked to an Output Template.

    WITH a template, the whole pipeline runs: contract fields are mapped to that
    template's field names by the LLM — not to the canonical data model directly
    (Contract Fields → Output Template Fields → Data Model Fields) — and the
    clauses become compiled validation rules.

    WITHOUT one, the contract is still read and its CLAUSES are still saved; it
    simply stops before rule generation, because a rule is written against an
    output template's columns and there are none to write against. The clauses
    that carry a rule are recorded as awaiting a template rather than discarded.
    This is what lets a contract be added on its own — from a broker's page,
    before any bordereau work exists — and picked up by a setup later.

    Only one contract can be Active per Output Template at any time.

    `broker_party_id` says WHICH broker holds this contract with the carrier. A
    contract is always (programme × broker), so a caller that knows the broker —
    the nested carrier route, and the Add Contract flow on the broker's own page
    — passes it here and the row is filed under that broker instead of landing
    as a carrier-held contract nobody's page can show. It also narrows what this
    upload supersedes: replacing one broker's contract must not retire another
    broker's on the same programme. Optional, because the setup builder uploads
    at (carrier, programme) scope and those contracts genuinely have no broker.

    Reference documents: when the contract DEFERS rule content to an external
    document ("Excluded Classes: per the Purchasing Guidelines on file"), the
    pipeline halts after extraction and returns {status:"references_required",
    external_references, resume_token} so the UI can ask the user to upload that
    document (sent back in `reference_files`) or proceed via `continue_anyway`.

    Response is a chunked heartbeat stream: validation errors still return real
    4xx statuses, but once extraction starts the endpoint streams whitespace
    every ~20s (so the ingress idle timeout never fires) and ends with one JSON
    payload. Pipeline failures therefore arrive as HTTP 200 with body
    {success:false, error:true, status_code, detail} — clients must check it.
    """

    if not file.filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # -------------------------------------------------
    # RESOLVE OUTPUT TEMPLATE + FETCH ITS FIELDS
    # Must exist; returns 400 if not found.
    # -------------------------------------------------

    with SessionLocal() as s:
        # Both the program and the output template must belong to the caller's
        # tenant — derived from the token, not the client.
        prog = s.get(Program, program_id)
        if not prog:
            raise HTTPException(404, "program not found")
        assert_tenant_owns(principal, prog.tenant_id)
        # Optional. A named template must exist and be this tenant's; NO named
        # template is a valid state, not an error — see the docstring.
        tmpl = s.get(ExportTemplate, output_template_id) if output_template_id else None
        if output_template_id and not tmpl:
            raise HTTPException(
                status_code=400,
                detail=f"Output Template {output_template_id} not found. "
                       "Create or select an Output Template before uploading a contract.",
            )
        if tmpl:
            assert_tenant_owns(principal, tmpl.tenant_id)
        # A named broker must actually be ON this programme. program_broker is
        # the gate that says the pair may produce at all, so filing a contract
        # under a pair the carrier never created is refused here rather than
        # written and discovered later by a screen that cannot explain it.
        if broker_party_id is not None:
            link = (s.query(ProgramBroker)
                    .filter(ProgramBroker.program_id == program_id,
                            ProgramBroker.broker_party_id == broker_party_id)
                    .first())
            if link is None:
                raise HTTPException(
                    status_code=400,
                    detail="That broker is not on this programme, so they cannot "
                           "hold a contract on it. Put them on the programme first.")
            if link.status != "active":
                raise HTTPException(
                    status_code=400,
                    detail="That broker has been taken off this programme, so no "
                           "new contract can be filed under them.")
        # Use the shared builder so the data-dictionary enrichment (description,
        # allowed_values, format, required) reaches the LLM mapper — building the
        # list inline here previously dropped it. Empty with no template, which
        # is what stops the pipeline after the clauses.
        template_fields = _template_fields_from_structure(tmpl.structure) if tmpl else []

    if template_fields:
        print(
            f"[Contract] Template-aware extraction: "
            f"output_template_id={output_template_id}, "
            f"{len(template_fields)} template field(s)"
        )
    else:
        print("[Contract] No output template — reading clauses only; "
              "no rules will be generated.")

    # -------------------------------------------------
    # SAVE TEMP FILE
    # -------------------------------------------------

    suffix = os.path.splitext(file.filename)[1]

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        contents = await file.read()
        tmp.write(contents)
        temp_file_path = tmp.name

    # -------------------------------------------------
    # SYNCHRONOUS FAST PATH
    # Everything that should fail with a real HTTP status (4xx) runs BEFORE
    # the response starts streaming — once the heartbeat stream below begins,
    # the status code is locked at 200 and errors can only travel in the body.
    # -------------------------------------------------
    try:
        # -------------------------------------------------
        # VALIDATE CONTRACT UPLOAD
        # -------------------------------------------------
        from contract_upload_services.upload_file_validator import (
            validate_uploaded_contract,
            UploadContractValidationError,
        )

        try:
            validate_uploaded_contract(
                file=file,
                file_bytes=contents,
                temp_file_path=temp_file_path,
            )
        except UploadContractValidationError as ve:
            raise HTTPException(status_code=400, detail=ve.to_detail())
        except HTTPException:
            raise

        # -------------------------------------------------
        # REFERENCE DOCUMENTS + HALT GATE
        # When the contract DEFERS rule content to an external document (e.g.
        # "Excluded Classes: per the Purchasing Guidelines on file"), pause after
        # extraction and ask the user to upload that document so the deferred
        # clauses resolve into concrete rules. Don't halt when the user already
        # attached reference doc(s) this request, or chose "Continue Anyway".
        #
        # Read here (not inside the pipeline task): Starlette closes the
        # multipart temp files when this endpoint function returns, so the
        # uploads must be consumed before the streaming response starts.
        # -------------------------------------------------
        reference_documents = await _extract_reference_documents(
            reference_files, program_id=program_id, tenant_id=principal.tenant_id)
        has_reference_files = bool(reference_documents)
        if has_reference_files:
            print(
                f"[Contract] {len(reference_documents)} reference document(s) provided: "
                f"{[rd['name'] for rd in reference_documents]}"
            )
        # Only halt for callers that can HANDLE the references_required response
        # (DirectSetup opts in via enable_reference_halt). Other callers — e.g.
        # the Programs "upload new version" flow — keep the non-halting behaviour:
        # a contract that defers to an external doc just proceeds (deferred clauses
        # won't become rules), exactly as before.
        halt = enable_reference_halt and not continue_anyway and not has_reference_files
    except Exception:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        raise

    # Plain values captured for the pipeline task — the UploadFile and request
    # objects are owned by Starlette and closed once this function returns.
    contract_filename = file.filename
    contract_content_type = file.content_type
    tenant_id = principal.tenant_id

    async def _pipeline() -> dict:
        """Heavy extraction pipeline (LLM calls + persistence).

        Runs as a background task while the endpoint streams heartbeat bytes,
        so the Azure Container Apps ingress idle timeout (~4 min) never fires.
        Returns the exact dict this endpoint used to return synchronously.
        """
        # -------------------------------------------------
        # EXACT RE-UPLOAD REUSE (no LLM)
        # Fingerprint the document + template; if an identical upload already
        # exists for this program, reuse its contract + rules and skip every
        # LLM call. This is what makes re-uploading the same contract
        # deterministic instead of regenerating a fresh rule set each time.
        # -------------------------------------------------
        from contract_upload_services.contract_versioning import (
            compute_content_fingerprint,
            compute_entity_fingerprint,
        )
        from contract_upload_services.document_extractors import extract_document_data
        from contract_upload_services.prompt_builder import build_llm_context

        # Parse the PDF to text (non-LLM) so the fingerprint is over the
        # NORMALIZED document content, not the raw bytes — a re-export of the
        # same contract still matches. Parsing is cheap relative to the LLM, so
        # the reuse short-circuit below still runs before any model call.
        parsed_doc = await run_in_threadpool(extract_document_data, temp_file_path)
        document_text = build_llm_context(parsed_doc)

        content_fp = compute_content_fingerprint(
            document_text, template_fields, output_template_id
        )
        entity_fp = compute_entity_fingerprint(program_id, contract_filename)

        # ── Regeneration stability (regen_reconcile.py) ──────────────────
        # L1/L2/L2b identity ladder, pre-LLM and TENANT-wide: is this upload a
        # version of a contract we already know? A hit does NOT skip generation
        # (unlike the exact-reuse short-circuit) — it makes the persister
        # reconcile the fresh rules against the prior contract's, so settled
        # decisions carry forward and changes become review proposals.
        regen_identity = None
        prior_contract = None
        try:
            from contract_upload_services.regen_reconcile import (
                identity_payload, find_prior_contract,
            )
            regen_identity = identity_payload(contents, document_text)
            from db import canonical_engine as _regen_ce
            from sqlalchemy import text as _regen_text
            with _regen_ce.connect() as _regen_conn:
                _tid = _regen_conn.execute(
                    _regen_text("SELECT program_tenant_id FROM program WHERE program_id = :pid"),
                    {"pid": program_id},
                ).scalar()
                prior_contract = find_prior_contract(
                    _regen_conn, _tid, content_fp=content_fp,
                    file_sha=(regen_identity or {}).get("file_sha256"),
                    doc_sha=(regen_identity or {}).get("doc_sha256"))
            if prior_contract:
                print(f"[Regen] prior version found: contract "
                      f"{prior_contract['contract_id']} "
                      f"(match {prior_contract['level']}) — reconcile will run "
                      f"at persist time.")
        except Exception as _regen_exc:  # noqa: BLE001 — fail-open
            print(f"[Regen] identity ladder skipped: {_regen_exc}")

        # Contract-only re-upload ("Upload new version") must ALWAYS regenerate
        # rules — it is intentionally NOT skippable. Only the combined
        # contract + output-template flow (/setup) is allowed to skip via the
        # identical-upload reuse short-circuit. So never reuse here.
        reusable = None
        if reusable:
            cid = reusable["contract_id"]
            print(
                f"[Contract] Identical re-upload detected "
                f"(content_fingerprint={content_fp[:12]}…) — reusing "
                f"contract_id={cid}; skipping all LLM calls."
            )
            program_obj = None
            contract_obj = None
            with SessionLocal() as s:
                prog = s.get(Program, program_id)
                if prog:
                    program_obj = _program_dict(prog)
                c = s.get(Contract, cid)
                if c:
                    contract_obj = {
                        "id":                 c.id,
                        "filename":           c.filename,
                        "status":             c.status,
                        "extracted":          c.extracted,
                        "output_template_id": c.output_template_id,
                        "created_at":         _iso_utc(c.created_at),
                    }
            return {
                "success": True,
                "reused": True,
                "program": program_obj,
                "id":        (contract_obj or {}).get("id"),
                "filename":  (contract_obj or {}).get("filename", contract_filename),
                "status":    (contract_obj or {}).get("status"),
                "extracted": (contract_obj or {}).get("extracted"),
                "contract":  contract_obj,
                "persisted": {"contract_id": cid, "reused": True},
                "persist_warning": None,
                "extraction_output": None,
            }

        # -------------------------------------------------
        # PROCESS CONTRACT
        # LLM maps contract clauses → Output Template fields
        # (not the canonical data model)
        # -------------------------------------------------

        extraction_output = await run_in_threadpool(
            contract_service.process_contract,
            temp_file_path,
            template_fields=template_fields if template_fields else None,
            halt_on_external_references=halt,
            resume_token=resume_token if continue_anyway else None,
            reference_documents=reference_documents or None,
            tenant_id=principal.tenant_id,
        )

        # Pipeline paused — contract references external document(s) not provided.
        # Nothing is persisted; return the referenced names so the UI can prompt
        # the user to upload them (resend as reference_files) or continue anyway.
        if isinstance(extraction_output, dict) and extraction_output.get("halted_for_references"):
            halted_refs = extraction_output.get("external_references", [])
            print(
                f"[Contract] HALTED for {len(halted_refs)} external "
                f"reference(s) — awaiting user (upload reference / continue anyway)."
            )
            return {
                "status": "references_required",
                "external_references": halted_refs,
                "resume_token": extraction_output.get("resume_token"),
                "id": None,
                "contract": None,
            }
        # print(f"[Contract] Extraction output: {extraction_output}")
        # -------------------------------------------------
        # PERSIST TO POSTGRES
        # output_template_id stored on the contract row.
        # -------------------------------------------------

        persist_result = None
        persist_warning = None

        try:
            from contract_upload_services.db_persister import persist_pipeline_output

            persist_result = persist_pipeline_output(
                extraction_output,
                program_id,
                contract_filename,
                output_template_id=output_template_id,
                content_fingerprint=content_fp,
                entity_fingerprint=entity_fp,
                identity=regen_identity,
                prior_contract=prior_contract,
                # Correlation id from the caller that made this upload. Recorded
                # so that caller can still identify THIS contract if the response
                # never reaches it — extraction can outlive the request, and the
                # id is otherwise only ever delivered in that one reply.
                upload_token=upload_token,
            )
            print(f"[persist_result] {persist_result}")

            # Stamp the broker onto the row persistence just created. Done here
            # rather than inside db_persister so the extraction pipeline keeps
            # one job; the id was already checked against this programme above.
            _cid = (persist_result or {}).get("contract_id")
            if _cid and broker_party_id:
                with SessionLocal() as _s:
                    _c = _s.get(Contract, _cid)
                    if _c is not None:
                        _c.broker_party_id = broker_party_id
                        _s.commit()

        except Exception as persist_exc:
            persist_warning = f"persistence failed: {persist_exc}"
            print(f"[Persist] {persist_warning}")

        # A failed persist means no contract row was created — surface it as an
        # error instead of returning a misleading success with id=null.
        if not (persist_result or {}).get("contract_id"):
            raise HTTPException(
                status_code=500,
                detail=persist_warning or "Contract was processed but could not be saved.",
            )


        # -------------------------------------------------
        # Persist the raw contract file to blob storage (Azure/Azurite) when
        # enabled. Historically the uploaded file was discarded after
        # extraction; with blob storage on we now retain it and record the
        # pointer on the contract row (blob_ref) below. Non-fatal on failure.
        # -------------------------------------------------
        contract_blob_ref = None
        if storage.is_azure():
            try:
                contract_blob_ref = storage.build_key(
                    "contracts", tenant_id, contract_filename)
                await run_in_threadpool(
                    storage.put_bytes, contract_blob_ref, contents,
                    contract_content_type or "application/octet-stream",
                )
            except Exception as blob_exc:                # noqa: BLE001 — best-effort
                contract_blob_ref = None
                print(f"[Contract] blob persist failed (non-fatal): {blob_exc}")

        # -------------------------------------------------
        # Auto-activate within Program scope.
        # Only ONE Active contract is allowed per Program at a time. Uploading a
        # new version supersedes every other contract in the program (including
        # ones linked to an earlier output template version).
        # -------------------------------------------------

        program_obj = None
        contract_obj = None

        with SessionLocal() as s:
            prog = s.get(Program, program_id)
            if prog:
                program_obj = _program_dict(prog)

            cid = (persist_result or {}).get("contract_id")
            if cid:
                # Supersede scope: when a schedule_key is given, only replace the
                # PRIOR contract for that SAME schedule — so one program keeps many
                # active contracts (one per schedule). With no schedule_key we keep
                # the legacy behaviour of superseding every contract in the program.
                sib_q = s.query(Contract).filter(
                    Contract.program_id == program_id,
                    Contract.id != cid,
                    Contract.status.notin_(["failed", "drafted", "extracting"]),
                )
                if schedule_key is not None:
                    sib_q = sib_q.filter(Contract.schedule_key == schedule_key)
                # A contract is (programme × broker): replacing what THIS broker
                # holds must leave every other broker's contract on the same
                # programme alone. With no broker named the scope is the whole
                # programme, exactly as before.
                if broker_party_id is not None:
                    sib_q = sib_q.filter(Contract.broker_party_id == broker_party_id)
                for sib in sib_q.all():
                    sib.status = "superseded"
                new_c = s.get(Contract, cid)
                if new_c:
                    new_c.status = "active"
                    if contract_blob_ref:
                        new_c.blob_ref = contract_blob_ref
                    if schedule_key is not None:
                        new_c.schedule_key = schedule_key
                    # The persister writes the contract at (tenant, programme)
                    # scope — it has no notion of the broker level. Filing it
                    # under the broker is what makes it reachable from their
                    # page, from the approvals queue, and from a broker-scoped
                    # setup; without it every contract reads as carrier-held.
                    if broker_party_id is not None:
                        new_c.broker_party_id = broker_party_id
                    # A program is "active" once it has an active contract.
                    if prog:
                        prog.status = "active"
                s.commit()
                if prog:
                    program_obj = _program_dict(prog)

                c = s.get(Contract, cid)
                if c:
                    contract_obj = {
                        "id":                 c.id,
                        "filename":           c.filename,
                        "status":             c.status,
                        "extracted":          c.extracted,
                        "output_template_id": c.output_template_id,
                        "schedule_key":       c.schedule_key,
                        "broker_party_id":    c.broker_party_id,
                        "approval_status":    c.approval_status,
                        "created_at":         _iso_utc(c.created_at),
                    }

        try:
            from audit import log_activity, actor_email
            log_activity(tenant_id, actor_email(principal.user_id), "contract_uploaded",
                         target=f"program:{program_id}",
                         details={
                             "program_id": program_id,
                             "output_template_id": output_template_id,
                             "broker_party_id": broker_party_id,
                             "contract_id": (persist_result or {}).get("contract_id"),
                             "filename": contract_filename,
                             "status": (contract_obj or {}).get("status"),
                             "rule_count": (persist_result or {}).get("counts", {}).get("validation_rule"),
                         })
        except Exception:  # noqa: BLE001
            pass
        return {
            "success": True,
            "program": program_obj,
            "id":        (contract_obj or {}).get("id"),
            "filename":  (contract_obj or {}).get("filename", contract_filename),
            "status":    (contract_obj or {}).get("status"),
            "extracted": (contract_obj or {}).get("extracted"),
            "contract":  contract_obj,
            "persisted": persist_result,
            "persist_warning": persist_warning,
            "extraction_output": extraction_output,
        }

    # -------------------------------------------------
    # HEARTBEAT STREAM
    # The Azure Container Apps ingress kills connections idle for ~4 minutes,
    # and extraction routinely takes longer. Run the pipeline as a task and
    # stream a whitespace byte every HEARTBEAT_SECS to reset the idle timer;
    # the real JSON payload is the final chunk. Leading whitespace is ignored
    # by JSON parsers, so the accumulated body is still one valid JSON doc.
    # -------------------------------------------------
    task = asyncio.create_task(_pipeline())

    def _on_done(t: asyncio.Task) -> None:
        # Mark any exception as retrieved (the client may disconnect before
        # the stream reads task.result()), then clean up the temp file — only
        # safe here, once the pipeline can no longer be reading it.
        if not t.cancelled():
            t.exception()
        try:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
        except OSError:
            pass

    task.add_done_callback(_on_done)

    HEARTBEAT_SECS = 20

    async def _heartbeat_stream():
        yield b" "  # flush headers + first byte immediately
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECS)
            if done:
                break
            yield b" "
        try:
            result = task.result()
        except Exception as e:  # noqa: BLE001
            print(f"\n[ERROR] Contract Processing Failed")
            print("Error::", str(e))
            # The 200 status is already on the wire — report the failure in
            # the body; the frontend interceptor converts it back into a
            # thrown error with the same {detail} shape as an HTTPException.
            status_code = e.status_code if isinstance(e, HTTPException) else 500
            detail = e.detail if isinstance(e, HTTPException) else str(e)
            result = {
                "success": False,
                "error": True,
                "status_code": status_code,
                "detail": detail,
            }
        yield json.dumps(jsonable_encoder(result)).encode()

    return StreamingResponse(
        _heartbeat_stream(),
        media_type="application/json",
        # Ask intermediaries to pass chunks through unbuffered.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/programs/{program_id}/setup")
async def program_setup(
    program_id: int,
    contract_file: UploadFile = File(...),
    template_file: Optional[UploadFile] = File(default=None),
    template_name: Optional[str] = Form(default=None),
    output_template_id: Optional[int] = Form(default=None),
    output_format: str = Form(default="xlsx"),
    continue_anyway: bool = Form(default=False),
    resume_token: Optional[str] = Form(default=None),
    reference_files: Optional[list[UploadFile]] = File(default=None),
    principal: Principal = Depends(current_principal),
):
    """Combined output-template + contract upload in one request.

    New architecture:
      1. Output Template is created/resolved first (it is the semantic bridge).
      2. Contract is then processed with the Output Template fields as context,
         so the LLM maps contract clauses → Output Template fields
         (not to the canonical data model directly).
      3. Contract is activated within Output Template scope.

    Either supply `template_file` (create a new Output Template) or
    `output_template_id` (use an existing one). If both are given, the
    new template takes precedence. At least one must be provided.

    Returns:
      { contract: {...}, template: {...} | None, program: {...} }
    """

    # ── 1. Resolve or create the Output Template first ───────────────────────
    template_result = None
    resolved_template_id = output_template_id

    if template_file and template_file.filename:
        template_bytes = await template_file.read()
        try:
            from exporter import parse_template, propose_template_mapping

            with SessionLocal() as s:
                prog = s.get(Program, program_id)
                if not prog:
                    raise HTTPException(404, "program not found")
                assert_tenant_owns(principal, prog.tenant_id)
                tid_code = prog.tenant_id

                carrier_party_id = prog.party_id if prog else None
                carrier_name = None
                if carrier_party_id:
                    cp = s.get(Party, carrier_party_id)
                    carrier_name = cp.legal_name if cp else None

                structure = await run_in_threadpool(parse_template, template_bytes, filename=template_file.filename)
                if structure.get("sheets"):
                    await run_in_threadpool(propose_template_mapping, structure)
                    tname = (template_name or "").strip() or \
                        template_file.filename.rsplit(".", 1)[0]

                    # Version number only — read that column rather than every
                    # sibling row (each carries the `structure` JSON).
                    versions = [v for (v,) in s.query(ExportTemplate.version).filter(
                        ExportTemplate.tenant_id == tid_code,
                        ExportTemplate.name == tname,
                    )]
                    version = (max((v or 1) for v in versions) + 1) if versions else 1
                    is_active = 0 if versions else 1

                    tmpl_ref, tmpl_bytes = await run_in_threadpool(
                        storage.store_or_keep, "templates", tid_code,
                        template_file.filename, template_bytes,
                    )
                    from output_serializers import normalize_format as _norm_fmt
                    t = ExportTemplate(
                        tenant_id=tid_code, name=tname,
                        version=version, is_active=is_active,
                        carrier=carrier_name,
                        carrier_party_id=carrier_party_id,
                        structure=structure,
                        template_blob=tmpl_bytes,
                        template_blob_ref=tmpl_ref,
                        approved=0,
                        output_format=_norm_fmt(output_format),
                    )
                    s.add(t)
                    s.commit()
                    s.refresh(t)
                    resolved_template_id = t.id
                    template_result = {
                        "id": t.id, "name": t.name, "version": t.version,
                        "sheets": [sh["sheet_name"] for sh in (structure.get("sheets") or [])],
                    }
        except Exception as te:
            template_result = {"error": str(te)}

    if not resolved_template_id:
        raise HTTPException(
            status_code=400,
            detail="Provide either template_file (to create a new Output Template) "
                   "or output_template_id (to use an existing one). "
                   "A contract cannot be created without an Output Template.",
        )

    # ── 2. Fetch Output Template fields for template-aware LLM extraction ─────
    template_fields: list = []
    with SessionLocal() as s:
        tmpl = s.get(ExportTemplate, resolved_template_id)
        if not tmpl:
            raise HTTPException(404, f"Output Template {resolved_template_id} not found")
        # Shared builder → carries data-dictionary enrichment (description,
        # allowed_values, format, required) through to the LLM mapper.
        template_fields = _template_fields_from_structure(tmpl.structure)

    # ── 3. Process the contract with template-aware LLM mapping ───────────────
    contract_result = None
    halted_external_refs = None

    suffix = os.path.splitext(contract_file.filename or "contract.pdf")[1]
    contract_bytes = await contract_file.read()

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(contract_bytes)
        tmp_path = tmp.name

    try:
        from contract_upload_services.upload_file_validator import (
            validate_uploaded_contract, UploadContractValidationError,
        )
        try:
            validate_uploaded_contract(
                file=contract_file, file_bytes=contract_bytes,
                temp_file_path=tmp_path,
            )
        except UploadContractValidationError as ve:
            raise HTTPException(status_code=400, detail=ve.to_detail())

        # ── Exact re-upload reuse (no LLM) — mirror of the /contracts route ──
        # Fingerprint the document + template; if an identical upload already
        # exists for this program, reuse its contract + rules and skip every LLM
        # call. Without this, /setup re-inserts a fresh contract + full clause
        # set on every upload. Skipped on a halted-extraction resume (that path
        # reuses the cached extraction, not a prior contract).
        from contract_upload_services.contract_versioning import (
            compute_content_fingerprint,
            compute_entity_fingerprint,
            find_reusable_contract,
        )
        from contract_upload_services.document_extractors import extract_document_data
        from contract_upload_services.prompt_builder import build_llm_context

        content_fp = None
        entity_fp = None
        regen_identity = None
        prior_contract = None
        if not (continue_anyway or resume_token):
            parsed_doc = await run_in_threadpool(extract_document_data, tmp_path)
            document_text = build_llm_context(parsed_doc)
            content_fp = compute_content_fingerprint(
                document_text, template_fields, resolved_template_id
            )
            entity_fp = compute_entity_fingerprint(program_id, contract_file.filename)

            # ── Regeneration stability (regen_reconcile.py) ──────────────────
            # L1/L2/L2b identity ladder, pre-LLM and TENANT-wide: is this upload a
            # version of a contract we already know? A hit does NOT skip generation
            # (unlike the exact-reuse short-circuit) — it makes the persister
            # reconcile the fresh rules against the prior contract's, so settled
            # decisions carry forward and changes become review proposals.
            regen_identity = None
            prior_contract = None
            try:
                from contract_upload_services.regen_reconcile import (
                    identity_payload, find_prior_contract,
                )
                regen_identity = identity_payload(contract_bytes, document_text)
                from db import canonical_engine as _regen_ce
                from sqlalchemy import text as _regen_text
                with _regen_ce.connect() as _regen_conn:
                    _tid = _regen_conn.execute(
                        _regen_text("SELECT program_tenant_id FROM program WHERE program_id = :pid"),
                        {"pid": program_id},
                    ).scalar()
                    prior_contract = find_prior_contract(
                        _regen_conn, _tid, content_fp=content_fp,
                        file_sha=(regen_identity or {}).get("file_sha256"),
                        doc_sha=(regen_identity or {}).get("doc_sha256"))
                if prior_contract:
                    print(f"[Regen] prior version found: contract "
                          f"{prior_contract['contract_id']} "
                          f"(match {prior_contract['level']}) — reconcile will run "
                          f"at persist time.")
            except Exception as _regen_exc:  # noqa: BLE001 — fail-open
                print(f"[Regen] identity ladder skipped: {_regen_exc}")
            # Set KAVACHIO_DISABLE_CONTRACT_REUSE=1 to force a full re-run (skip the
            # identical-upload short-circuit) — useful when testing pipeline changes.
            reusable = (None if os.getenv("KAVACHIO_DISABLE_CONTRACT_REUSE")
                        else find_reusable_contract(program_id, content_fp))
            if reusable:
                cid = reusable["contract_id"]
                print(
                    f"[Setup] Identical re-upload detected "
                    f"(content_fingerprint={content_fp[:12]}…) — reusing "
                    f"contract_id={cid}; skipping all LLM calls."
                )
                with SessionLocal() as s:
                    # Re-point the reused contract to the CURRENT output template
                    # and make it active, so the template page
                    # (GET /export/template/{id}/contract-mapping — which finds the
                    # active contract WHERE output_template_id = template_id) locates
                    # it and shows its rules. Reuse only fires when the template
                    # field-set matches, so re-pointing is safe — the rules
                    # reference the same fields.
                    c = s.get(Contract, cid)
                    if c:
                        c.output_template_id = resolved_template_id
                        c.status = "active"
                        for sib in s.query(Contract).filter(
                            Contract.program_id == program_id,
                            Contract.id != cid,
                            Contract.status.notin_(["failed", "drafted", "extracting"]),
                        ).all():
                            sib.status = "superseded"
                        # A program is "active" once it has an active contract.
                        prog = s.get(Program, program_id)
                        if prog:
                            prog.status = "active"
                        s.commit()
                    prog = s.get(Program, program_id)
                    program_obj = _program_dict(prog) if prog else None
                    c = s.get(Contract, cid)
                    contract_result = {
                        "id": c.id, "filename": c.filename, "status": c.status,
                        "extracted": c.extracted,
                        "output_template_id": c.output_template_id,
                        "created_at": _iso_utc(c.created_at),
                    } if c else None
                try:
                    from audit import log_activity, actor_email
                    log_activity(prog.tenant_id if prog else None,
                                 actor_email(principal.user_id), "bordereau_setup_completed",
                                 target=f"program:{program_id}",
                                 details={
                                     "program_id": program_id,
                                     "template_id": resolved_template_id,
                                     "contract_id": (contract_result or {}).get("id"),
                                     "template_name": template_name,
                                     "reused": True,
                                 })
                except Exception:  # noqa: BLE001
                    pass
                return {
                    "status": "ok",
                    "reused": True,
                    "contract": contract_result,
                    "template": template_result,
                    "program": program_obj,
                }

        # ── Reference documents: extract their text (same as the contract) so
        # the extraction LLM can resolve clauses that defer to them. ──────────
        reference_documents = await _extract_reference_documents(
            reference_files, program_id=program_id, tenant_id=principal.tenant_id)
        has_reference_files = bool(reference_documents)
        if has_reference_files:
            print(
                f"[Setup] {len(reference_documents)} reference document(s) provided: "
                f"{[rd['name'] for rd in reference_documents]}"
            )

        # LLM maps contract clauses → Output Template fields.
        # HALT GATE: when the contract DEFERS rule content to an external document
        # (e.g. "Authorized / Targeted / Excluded Classes of Business: per the
        # Facultative Purchasing Guidelines on file with the Company"), pause and
        # ask the user to upload that document so the deferred clauses can be
        # resolved into concrete, checkable rules instead of being silently
        # dropped. Do NOT halt when:
        #   • the user already uploaded reference doc(s) on THIS request
        #     (has_reference_files) — they are fed to the extractor and used; or
        #   • the user chose "Continue Anyway" (continue_anyway) — proceed with the
        #     contract text alone, resuming from the cached extraction.
        halt = not continue_anyway and not has_reference_files
        extraction_output = await run_in_threadpool(
            contract_service.process_contract,
            tmp_path,
            template_fields=template_fields if template_fields else None,
            halt_on_external_references=halt,
            resume_token=resume_token if continue_anyway else None,
            reference_documents=reference_documents or None,
            tenant_id=principal.tenant_id,
        )

        # Pipeline paused: return the referenced document names for the popup.
        # Nothing is persisted; the user uploads the reference doc or retries
        # this endpoint with continue_anyway=true.
        if isinstance(extraction_output, dict) and extraction_output.get("halted_for_references"):
            halted_external_refs = extraction_output.get("external_references", [])
            print(
                f"[Setup] HALTED for {len(halted_external_refs)} external "
                f"reference(s) — awaiting user (upload reference / continue anyway)."
            )
        else:
            from contract_upload_services.db_persister import persist_pipeline_output
            persist_result = None
            try:
                persist_result = persist_pipeline_output(
                    extraction_output, program_id, contract_file.filename,
                    output_template_id=resolved_template_id,
                    content_fingerprint=content_fp,
                    entity_fingerprint=entity_fp,
                    identity=regen_identity,
                    prior_contract=prior_contract,
                )
            except Exception as pe:
                print(f"[Persist] ERROR (non-fatal): {pe}")

            with SessionLocal() as s:
                cid = (persist_result or {}).get("contract_id")
                if cid:
                    # One active contract per Program — supersede all others
                    siblings = s.query(Contract).filter(
                        Contract.program_id == program_id,
                        Contract.id != cid,
                        Contract.status.notin_(["failed", "drafted", "extracting"]),
                    ).all()
                    for sib in siblings:
                        sib.status = "superseded"
                    new_c = s.get(Contract, cid)
                    if new_c:
                        new_c.status = "active"
                        # A program is "active" once it has an active contract.
                        prog = s.get(Program, program_id)
                        if prog:
                            prog.status = "active"
                    s.commit()
                    c = s.get(Contract, cid)
                    contract_result = {
                        "id": c.id, "filename": c.filename, "status": c.status,
                        "extracted": c.extracted,
                        "output_template_id": c.output_template_id,
                        "created_at": _iso_utc(c.created_at),
                    } if c else None

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    with SessionLocal() as s:
        prog = s.get(Program, program_id)
        program_obj = _program_dict(prog) if prog else None

    # Halted on external references — prompt the user (no contract persisted yet).
    if halted_external_refs is not None:
        return {
            "status": "references_required",
            "external_references": halted_external_refs,
            "resume_token": (extraction_output or {}).get("resume_token"),
            "contract": None,
            "template": template_result,
            "program": program_obj,
        }

    try:
        from audit import log_activity, actor_email
        log_activity(prog.tenant_id if prog else None,
                     actor_email(principal.user_id), "bordereau_setup_completed",
                     target=f"program:{program_id}",
                     details={
                         "program_id": program_id,
                         "template_id": resolved_template_id,
                         "contract_id": (contract_result or {}).get("id"),
                         "template_name": template_name,
                     })
    except Exception:  # noqa: BLE001
        pass
    return {
        "status": "ok",
        "contract": contract_result,
        "template": template_result,
        "program": program_obj,
    }


@router.get("/programs/{program_id}/contracts")
def program_contracts_list(program_id: int,
                           broker_party_id: Optional[int] = None,
                           approved_only: bool = False,
                           principal: Principal = Depends(current_principal)):
    """Contracts on a programme.

    `broker_party_id` narrows to one broker's contracts. It deliberately also
    returns the programme's CARRIER-HELD contracts (broker_party_id NULL) —
    those were written before the broker level existed and still govern the
    programme, so hiding them would make an existing setup look empty.

    `approved_only` drops anything still waiting on the carrier: only a live
    contract can have an output template built on it.

    Both default off, so an existing caller gets exactly what it always did.
    """
    with SessionLocal() as s:
        prog = s.get(Program, program_id)
        if not prog:
            raise HTTPException(404, "program not found")
        assert_tenant_owns(principal, prog.tenant_id)
        q = s.query(Contract).filter(Contract.program_id == program_id)
        if broker_party_id is not None:
            q = q.filter(or_(Contract.broker_party_id == broker_party_id,
                             Contract.broker_party_id.is_(None)))
        if approved_only:
            q = q.filter(func.coalesce(Contract.approval_status, "approved")
                         == "approved")
        rows = q.order_by(Contract.id.desc()).all()
        # Clause count per contract = rows in clauses_extracted (defensive: the
        # table may be absent on minimal DBs).
        counts: dict[int, int] = {}
        try:
            if rows:
                ids = [c.id for c in rows]
                res = s.execute(text(
                    "SELECT contract_id, COUNT(*) AS n FROM clauses_extracted "
                    "WHERE contract_id = ANY(:ids) GROUP BY contract_id"),
                    {"ids": ids}).mappings().all()
                counts = {r["contract_id"]: r["n"] for r in res}
        except Exception:
            counts = {}
        # `upload_token` is surfaced as a field of its own so a caller can ask
        # "which of these is the contract MY upload created?" without knowing
        # where it is stored. Null for every contract written before this
        # existed, and for any upload that didn't send one.
        from contract_upload_services.db_persister import extracted_upload_token
        # broker_party_id / approval_status / the term are read-side additions:
        # the columns already existed on the row, they were simply never
        # returned. Every existing key is unchanged.
        broker_names = {}
        broker_ids = {c.broker_party_id for c in rows if c.broker_party_id}
        if broker_ids:
            broker_names = {p.id: p.legal_name for p in
                            s.query(Party).filter(Party.id.in_(broker_ids)).all()}
        return [{"id": c.id, "filename": c.filename, "status": c.status,
                 "extracted": c.extracted,
                 "upload_token": extracted_upload_token(c.extracted),
                 "clause_count": counts.get(c.id, 0),
                 "output_template_id": c.output_template_id,
                 "schedule_key": c.schedule_key,
                 "broker_party_id": c.broker_party_id,
                 "broker_name": broker_names.get(c.broker_party_id),
                 "approval_status": c.approval_status or "approved",
                 "inception_dt": c.inception_dt.isoformat() if c.inception_dt else None,
                 "expiry_dt": c.expiry_dt.isoformat() if c.expiry_dt else None,
                 "created_at": _iso_utc(c.created_at)}
                for c in rows]


# =========================================================
# Phase 2 — Sheet bindings (which sheet → which schedule/contract/output)
# Keyed by mapper (the BDX format). Saved once, reused each upload, overridable.
# =========================================================

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


@router.get("/mappers/{mapper_id}/sheet-bindings")
def get_sheet_bindings(mapper_id: int,
                       principal: Principal = Depends(current_principal)):
    """Return saved sheet bindings for a BDX format; sheets without a saved
    binding get a name-matched PROPOSAL the user can confirm."""
    with SessionLocal() as s:
        mp = s.get(Mapper, mapper_id)
        if not mp:
            raise HTTPException(404, "mapper not found")
        assert_tenant_owns(principal, mp.tenant_id)
        saved = {b.sheet_name: b
                 for b in s.query(SheetBinding).filter(SheetBinding.mapper_id == mapper_id).all()}
        out = []
        for sh in _mapper_sheet_names(mp):
            if sh in saved:
                out.append(_sb_binding_dict(saved[sh]))
            else:
                sched, role = _sb_propose_schedule(sh)
                out.append({
                    "sheet_name": sh, "role": role, "schedule_key": sched,
                    "contract_id": None, "output_template_id": None,
                    "depends_on": [], "reference_doc_ids": [],
                    "approved": False, "proposed": True,
                })
        # Include any saved bindings for sheets not in the mapper's current list.
        for sh, b in saved.items():
            if sh not in {o["sheet_name"] for o in out}:
                out.append(_sb_binding_dict(b))
        return {"mapper_id": mapper_id, "bindings": out}


@router.put("/mappers/{mapper_id}/sheet-bindings")
def put_sheet_bindings(mapper_id: int, body: dict = Body(...),
                       principal: Principal = Depends(current_principal)):
    """Upsert the saved sheet bindings for a BDX format."""
    items = body.get("bindings") or []
    with SessionLocal() as s:
        mp = s.get(Mapper, mapper_id)
        if not mp:
            raise HTTPException(404, "mapper not found")
        assert_tenant_owns(principal, mp.tenant_id)
        existing = {b.sheet_name: b
                    for b in s.query(SheetBinding).filter(SheetBinding.mapper_id == mapper_id).all()}
        for it in items:
            sh = it.get("sheet_name")
            if not sh:
                continue
            b = existing.get(sh) or SheetBinding(mapper_id=mapper_id, sheet_name=sh)
            b.role = it.get("role") or "schedule"
            b.schedule_key = it.get("schedule_key")
            b.contract_id = it.get("contract_id")
            b.output_template_id = it.get("output_template_id")
            b.depends_on = it.get("depends_on") or []
            b.reference_doc_ids = it.get("reference_doc_ids") or []
            b.approved = 1 if it.get("approved") else 0
            if b.id is None:
                s.add(b)
        s.commit()
        rows = s.query(SheetBinding).filter(SheetBinding.mapper_id == mapper_id).all()
        try:
            from audit import log_activity, actor_email
            log_activity(mp.tenant_id, actor_email(principal.user_id),
                         "sheet_bindings_saved", target=f"mapper:{mapper_id}",
                         details={"mapper_id": mapper_id, "sheet_count": len(rows)})
        except Exception:  # noqa: BLE001
            pass
        return {"mapper_id": mapper_id, "bindings": [_sb_binding_dict(b) for b in rows]}


@router.get("/programs/{program_id}/contracts/{contract_id}")
def program_contract_detail(program_id: int, contract_id: int,
                            principal: Principal = Depends(current_principal)):
    """Return a contract detail payload for the UI.

    Includes the contract, linked Output Template, extracted commercial terms,
    field mappings inferred from generated rules, and all validation rules.
    """
    with SessionLocal() as s:
        contract = s.get(Contract, contract_id)
        if not contract or contract.program_id != program_id:
            raise HTTPException(404, "contract not found for this program")
        assert_tenant_owns(principal, contract.tenant_id)

        template = None
        template_fields: list[dict] = []
        if contract.output_template_id:
            tmpl = s.get(ExportTemplate, contract.output_template_id)
            if tmpl:
                template_fields = _template_fields_from_structure(tmpl.structure)
                template = {
                    "id": tmpl.id,
                    "name": tmpl.name,
                    "version": tmpl.version,
                    "is_active": bool(tmpl.is_active),
                    "approved": bool(tmpl.approved),
                    "fields": template_fields,
                }

        term_rows = s.execute(
            text("""
                SELECT term_id,
                       term_type            AS term_category,
                       term_definition,
                       term_source_reference AS extracted_from_clause_ref,
                       NULL                 AS extraction_confidence
                FROM contract_term
                WHERE term_contract_id = :cid
                ORDER BY term_id
            """),
            {"cid": contract_id},
        ).mappings().all()

        clause_rows = s.execute(
            text("""
                SELECT clause_id, clause_type, title, text, page_number,
                       section_header, classified_engine, classified_rule_types,
                       classification_confidence, generated_rule_count
                FROM clauses_extracted
                WHERE contract_id = :cid
                ORDER BY clause_id
            """),
            {"cid": contract_id},
        ).mappings().all()
        clauses_by_id = {row["clause_id"]: dict(row) for row in clause_rows}

        rule_rows = s.execute(
            text("""
                SELECT rule_id AS validation_rule_id, rule_engine, rule_name,
                       rule_description, validation_stage, severity,
                       canonical_target, rule_spec, error_message,
                       source_clause_id, source_verbatim_text,
                       source_page_number, generation_confidence, rule_status
                FROM validation_rule
                WHERE contract_id = :cid AND rule_status <> 'disabled'
                ORDER BY rule_id
            """),
            {"cid": contract_id},
        ).mappings().all()

        rules = []
        mappings_by_key: dict[tuple[str, str], dict] = {}

        for row in rule_rows:
            rule = dict(row)
            rule["canonical_target"] = _json_value(rule.get("canonical_target")) or {}
            rule["rule_spec"] = _json_value(rule.get("rule_spec")) or {}
            # Surface the current output-field mapping + whether it's a retargetable
            # IR rule, so the UI can offer "change field" / "remove" per rule.
            _ofields = _output_fields_from_rule(rule)
            rule["output_field"] = _ofields[0] if _ofields else None
            rule["output_fields"] = _ofields
            _spec = rule["rule_spec"]
            rule["rule_kind"] = _spec.get("kind") if isinstance(_spec, dict) else None
            # Surface the tolerance band on cross_field_math (formula) rules so the
            # UI can offer a ±% editor (PUT .../rules/{id}/tolerance). Other rule
            # templates carry no band, so these stay null and the UI hides it.
            _ir = _spec.get("ir") if isinstance(_spec, dict) else None
            if isinstance(_ir, dict):
                _params = _ir.get("params") or {}
                rule["rule_template"] = _ir.get("template")
                if _ir.get("template") == "cross_field_math":
                    rule["tolerance_pct"] = _params.get("tolerance_pct")
                    rule["reject_pct"] = _params.get("reject_pct")
                else:
                    # Value-matching rules (allowed/prohibited lists, conditional
                    # values) carry the accepted SURFACE SPELLINGS of each value.
                    # Surfacing them is what lets the setup screen show the list and
                    # offer "add a spelling" (POST .../rules/{id}/variation-values).
                    # Wrapped: a malformed rule_spec must never break the page.
                    try:
                        from contract_upload_services.rule_editor import (
                            variation_context, removable_variations)
                        _vctx = variation_context(_spec)
                    except Exception:
                        _vctx = None
                    if _vctx:
                        rule["enum_field"] = _vctx["field"]
                        rule["enum_values"] = _vctx["base"]
                        rule["variation_values"] = _vctx["existing"]
                        # Which of those the admin may take back. Derived on the
                        # server from the SAME rule the delete route enforces, so
                        # the UI can never offer a removal that would be refused —
                        # a value the CONTRACT names is not the admin's to drop.
                        try:
                            rule["removable_variations"] = removable_variations(_spec)
                        except Exception:
                            rule["removable_variations"] = []
                        # The rule accepts MORE than the list above: the compiler
                        # also bakes in every spelling the shared vocabulary knows
                        # for these values (e.g. USA / US / U.S.A. for "United
                        # States of America"). Those are invisible in rule_spec,
                        # so without this the UI shows half the truth and an admin
                        # reasonably tries to add a spelling that already works.
                        try:
                            from contract_upload_services import vocabulary as _vocab
                            _seen = {_vocab._norm(v) for v in
                                     (_vctx["existing"] + _vctx["base"])}
                            _from_vocab = []
                            for _b in _vctx["base"]:
                                for _syn in _vocab.synonyms_for_value(_b, _vctx["field"]):
                                    _k = _vocab._norm(_syn)
                                    if _k and _k not in _seen:
                                        _seen.add(_k)
                                        _from_vocab.append(_syn)
                            rule["vocabulary_values"] = _from_vocab
                        except Exception:
                            rule["vocabulary_values"] = []
            clause = clauses_by_id.get(rule.get("source_clause_id"))
            if clause:
                rule["source_clause"] = {
                    "id": clause.get("clause_id"),
                    "title": clause.get("title"),
                    "text": clause.get("text"),
                    "page_number": clause.get("page_number"),
                    "section_header": clause.get("section_header"),
                }
            rules.append(rule)

            contract_field = (
                (clause or {}).get("title")
                or rule.get("source_verbatim_text")
                or rule.get("rule_name")
                or "Contract clause"
            )
            for output_field in _output_fields_from_rule(rule):
                key = (str(contract_field), output_field)
                if key not in mappings_by_key:
                    mappings_by_key[key] = {
                        "contract_field": contract_field,
                        "contract_clause_id": rule.get("source_clause_id"),
                        "contract_clause_text": (clause or {}).get("text") or rule.get("source_verbatim_text"),
                        "output_field": output_field,
                        "rule_names": [],
                    }
                mappings_by_key[key]["rule_names"].append(rule.get("rule_name"))

        terms = [
            {
                "id": row["term_id"],
                "category": row["term_category"],
                "value": _json_value(row["term_definition"]),
                "source_text": row["extracted_from_clause_ref"],
                "confidence": row["extraction_confidence"],
            }
            for row in term_rows
        ]

        # Non-validatable clauses (review = needs data the BDX lacks; control =
        # governance / obligations). Table may not exist on older DBs — tolerate.
        clause_routing: list[dict] = []
        try:
            routing_rows = s.execute(
                text("""
                    SELECT clause_id, bucket, rule_name, clause_text,
                           source_page, reason
                    FROM contract_clause_routing
                    WHERE contract_id = :cid
                    ORDER BY bucket, routing_id
                """),
                {"cid": contract_id},
            ).mappings().all()
            clause_routing = [dict(r) for r in routing_rows]
        except Exception:
            clause_routing = []

        return {
            "contract": {
                "id": contract.id,
                "program_id": contract.program_id,
                "filename": contract.filename,
                "status": contract.status,
                "output_template_id": contract.output_template_id,
                "extracted": contract.extracted,
                "template_field_mappings": contract.template_field_mappings,
                "created_at": _iso_utc(contract.created_at),
            },
            "output_template": template,
            "terms": terms,
            "clauses": [dict(row) for row in clause_rows],
            "field_mappings": list(mappings_by_key.values()),
            "rules": rules,
            "clause_routing": clause_routing,
        }


# ---- Edit a contract's validation rules (retarget output field / remove) -----

class RuleRetargetBody(BaseModel):
    new_field: str
    old_field: Optional[str] = None
    mga: Optional[str] = None
    actor: Optional[str] = None


class RuleVariationBody(BaseModel):
    # SURFACE VARIATIONS of a value the contract already names — e.g. "Palms
    # Spec." for "Palms Specialty Insurance Company, Inc.". Append-only: there is
    # no removal counterpart, because the same list also holds the contract's own
    # authorized values (see rule_editor.add_variation_value).
    #
    # Send `variations` (several at once — they are judged in ONE model call) or
    # `spelling` (one). Both are accepted so an older client keeps working.
    variations: Optional[list[str]] = None
    spelling: Optional[str] = None
    mga: Optional[str] = None
    actor: Optional[str] = None

    def candidates(self) -> list[str]:
        raw = self.variations if self.variations is not None else (
            [self.spelling] if self.spelling is not None else [])
        return [str(v).strip() for v in raw if str(v or "").strip()]


class RuleToleranceBody(BaseModel):
    # Both percents (1.0 == 1%). tolerance_pct = the compliant band (within it a
    # reported amount matches its formula); reject_pct, when set ABOVE it, makes the
    # band beyond reject a hard violation (rule severity) and the middle a soft
    # warning ("flag vs auto-reject"). Send clear_reject to drop the reject band and
    # revert to a single threshold. Omitting a field leaves it unchanged.
    tolerance_pct: Optional[float] = None
    reject_pct: Optional[float] = None
    clear_reject: Optional[bool] = False
    mga: Optional[str] = None
    actor: Optional[str] = None


def _load_rule_for_contract(s, contract_id: int, rule_id: int) -> dict:
    row = s.execute(
        text("""SELECT rule_id, rule_name, rule_spec, canonical_target,
                       error_message, rule_status, source_clause_id,
                       source_verbatim_text, source_page_number
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


@router.put("/programs/{program_id}/contracts/{contract_id}/rules/{rule_id}/output-field")
def rule_retarget_output_field(program_id: int, contract_id: int, rule_id: int,
                               body: RuleRetargetBody,
                               principal: Principal = Depends(current_principal)):
    """Change the OUTPUT FIELD an IR validation rule is mapped to.

    Deterministically re-derives the rule's IR, compiled SQL and canonical_target
    from the new field (no LLM), so validation, formulas and the clause→field
    display all follow. Soft cache (`rule_sql`) is cleared so it recompiles clean.
    """
    from contract_upload_services.output_schema import build_output_schema
    from contract_upload_services.rule_editor import retarget_ir_rule, RetargetError

    if not body.new_field or not body.new_field.strip():
        raise HTTPException(400, "new_field is required")

    with SessionLocal() as s:
        contract = s.get(Contract, contract_id)
        if not contract or contract.program_id != program_id:
            raise HTTPException(404, "contract not found for this program")
        assert_tenant_owns(principal, contract.tenant_id)
        if not contract.output_template_id:
            raise HTTPException(400, "this contract has no output template to map fields against")
        tmpl = s.get(ExportTemplate, contract.output_template_id)
        if not tmpl:
            raise HTTPException(404, "output template not found")
        schema = build_output_schema(_template_fields_from_structure(tmpl.structure))
        tenant_id = contract.tenant_id

        row = _load_rule_for_contract(s, contract_id, rule_id)
        if row["rule_status"] == "disabled":
            raise HTTPException(400, "this rule was removed; restore it before retargeting")
        rule_spec = _json_value(row["rule_spec"]) or {}
        canonical_target = _json_value(row["canonical_target"]) or {}
        old_fields = _output_fields_from_rule(
            {"canonical_target": canonical_target, "rule_spec": rule_spec})
        old_field = body.old_field or (old_fields[0] if old_fields else None)

        try:
            new_spec, new_target, new_error, resolved_old, resolved_new = retarget_ir_rule(
                rule_spec, canonical_target, row.get("error_message"),
                old_field, body.new_field, schema)
        except RetargetError as e:
            raise HTTPException(400, str(e))

        s.execute(
            text("""UPDATE validation_rule
                       SET rule_spec = CAST(:rs AS JSONB),
                           canonical_target = CAST(:ct AS JSONB),
                           error_message = :em,
                           updated_at = now(), updated_by = :actor
                     WHERE rule_id = :rid"""),
            {"rs": json.dumps(new_spec, default=str),
             "ct": json.dumps(new_target, default=str),
             "em": new_error, "actor": body.actor or "user", "rid": rule_id},
        )
        s.commit()
        # Drop the stale compiled-SQL cache row (own tx — see _purge_rule_sql).
        _purge_rule_sql(s, rule_id)
        tenant_mga = body.mga or _tenant_name(s, tenant_id)

    _log(tenant_mga, body.actor, "rule.output_field_changed",
         target=f"rule:{rule_id}",
         details={"contract_id": contract_id, "program_id": program_id,
                  "rule_name": row["rule_name"],
                  "old_field": resolved_old, "new_field": resolved_new})
    return {"ok": True, "rule_id": rule_id,
            "output_field": new_target.get("output_field"),
            "output_fields": new_target.get("output_fields")}


@router.put("/programs/{program_id}/contracts/{contract_id}/rules/{rule_id}/tolerance")
def rule_update_tolerance(program_id: int, contract_id: int, rule_id: int,
                          body: RuleToleranceBody,
                          principal: Principal = Depends(current_principal)):
    """Set the ±% tolerance band(s) on a cross_field_math (formula) rule.

    Deterministically rewrites the rule's IR params and recompiles its SQL (no
    LLM) so the DuckDB runtime picks up the new band on the next run. Soft cache
    (`rule_sql`) is cleared so it recompiles clean — same contract as retargeting.
    """
    from contract_upload_services.output_schema import build_output_schema
    from contract_upload_services.rule_editor import patch_tolerance, RuleEditError

    updates: dict = {}
    if body.tolerance_pct is not None:
        updates["tolerance_pct"] = body.tolerance_pct
    if body.clear_reject:
        updates["reject_pct"] = None
    elif body.reject_pct is not None:
        updates["reject_pct"] = body.reject_pct
    if not updates:
        raise HTTPException(
            400, "supply tolerance_pct and/or reject_pct (or clear_reject)")

    with SessionLocal() as s:
        contract = s.get(Contract, contract_id)
        if not contract or contract.program_id != program_id:
            raise HTTPException(404, "contract not found for this program")
        assert_tenant_owns(principal, contract.tenant_id)
        if not contract.output_template_id:
            raise HTTPException(400, "this contract has no output template to map fields against")
        tmpl = s.get(ExportTemplate, contract.output_template_id)
        if not tmpl:
            raise HTTPException(404, "output template not found")
        schema = build_output_schema(_template_fields_from_structure(tmpl.structure))
        tenant_id = contract.tenant_id

        row = _load_rule_for_contract(s, contract_id, rule_id)
        if row["rule_status"] == "disabled":
            raise HTTPException(400, "this rule was removed; restore it before editing")
        rule_spec = _json_value(row["rule_spec"]) or {}

        try:
            new_spec, applied = patch_tolerance(rule_spec, schema, updates)
        except RuleEditError as e:
            raise HTTPException(400, str(e))

        s.execute(
            text("""UPDATE validation_rule
                       SET rule_spec = CAST(:rs AS JSONB),
                           updated_at = now(), updated_by = :actor
                     WHERE rule_id = :rid"""),
            {"rs": json.dumps(new_spec, default=str),
             "actor": body.actor or "user", "rid": rule_id},
        )
        s.commit()
        # Drop the stale compiled-SQL cache row (own tx — see _purge_rule_sql).
        _purge_rule_sql(s, rule_id)
        tenant_mga = body.mga or _tenant_name(s, tenant_id)

    new_params = (new_spec.get("ir") or {}).get("params", {})
    _log(tenant_mga, body.actor, "rule.tolerance_changed",
         target=f"rule:{rule_id}",
         details={"contract_id": contract_id, "program_id": program_id,
                  "rule_name": row["rule_name"], "applied": applied})
    return {"ok": True, "rule_id": rule_id,
            "tolerance_pct": new_params.get("tolerance_pct"),
            "reject_pct": new_params.get("reject_pct")}


@router.post("/programs/{program_id}/contracts/{contract_id}/rules/{rule_id}/variation-values")
def rule_add_variation_value(program_id: int, contract_id: int, rule_id: int,
                             body: RuleVariationBody,
                             principal: Principal = Depends(require_role("tenant_admin"))):
    """Teach a value-matching rule another SPELLING of a value the contract names.

    The bordereau spells a carrier a dozen ways; the contract spells it once. When
    a rule flags rows that are really compliant (or misses rows that are not), the
    fix is usually one missing spelling. This lets a tenant_admin add it from the
    setup screen instead of re-uploading the contract.

    Nothing is taken on trust:

      * deterministic gates first (already covered? traces back to a value the
        contract actually names? specific enough to identify ONE value?) — these
        are the same tests contract generation applies, so an accepted spelling
        survives the next regeneration instead of silently vanishing;
      * then a small model, which may only VETO. It can never admit a spelling the
        deterministic gates rejected, and an unreachable model fails CLOSED.

    A REJECTION IS A 200, not an error — it is a normal, expected answer carrying
    the reason and the contract's own words. 4xx is reserved for a wrong rule type
    (400), the wrong role (403), or a rule/contract that is not yours (404).

    SEVERAL AT ONCE: send `variations` and the whole batch is judged in ONE model
    call — the deterministic gates run per candidate (they are free), and only the
    survivors are sent to the model together. Each acceptance widens the set the
    next candidate is judged against, so a batch behaves exactly like adding them
    one at a time. Every submitted line gets its own entry in `results`.

    On acceptance: the variations are appended, the rule's SQL is recompiled from
    the finished IR (never string-patched — the runtime executes
    rule_spec.compiled_sql verbatim with no check that it still agrees with the IR
    beside it), the row is written ONCE, and each variation is recorded in the
    SHARED vocabulary so every contract processed afterwards already understands it.
    """
    from contract_upload_services.output_schema import build_output_schema
    from contract_upload_services.rule_editor import (
        add_variation_value, variation_context, RuleEditError)
    from contract_upload_services import variation_admit, vocabulary

    candidates = body.candidates()
    if not candidates:
        raise HTTPException(400, "supply at least one variation to add")
    if len(candidates) > 25:
        raise HTTPException(400, "add at most 25 variations at a time")

    with SessionLocal() as s:
        contract = s.get(Contract, contract_id)
        if not contract or contract.program_id != program_id:
            raise HTTPException(404, "contract not found for this program")
        assert_tenant_owns(principal, contract.tenant_id)
        if not contract.output_template_id:
            raise HTTPException(400, "this contract has no output template to map fields against")
        tmpl = s.get(ExportTemplate, contract.output_template_id)
        if not tmpl:
            raise HTTPException(404, "output template not found")
        schema = build_output_schema(_template_fields_from_structure(tmpl.structure))
        tenant_id = contract.tenant_id

        row = _load_rule_for_contract(s, contract_id, rule_id)
        if row["rule_status"] == "disabled":
            raise HTTPException(400, "this rule was removed; restore it before editing")
        rule_spec = _json_value(row["rule_spec"]) or {}

        ctx = variation_context(rule_spec)
        if ctx is None:
            raise HTTPException(
                400, "Spellings apply only to rules that match a value against the "
                     "contract — an allowed or prohibited list, or a conditional value.")

        # The clause the rule came from: what the admin is shown when a spelling is
        # refused, so the refusal cites the contract rather than the checker.
        clause_text = row.get("source_verbatim_text")
        clause_page = row.get("source_page_number")
        if not clause_text and row.get("source_clause_id"):
            crow = s.execute(
                text("SELECT text, page_number FROM clauses_extracted WHERE clause_id = :cid"),
                {"cid": row["source_clause_id"]},
            ).mappings().first()
            if crow:
                clause_text = crow.get("text")
                clause_page = clause_page or crow.get("page_number")

        # Every candidate goes through the deterministic gates on its own; only the
        # survivors reach the model, and they go together in ONE request.
        verdicts = variation_admit.check_many(
            candidates, template=ctx["template"], field=ctx["field"],
            base=ctx["base"], existing=ctx["existing"], clause_text=clause_text)

        accepted = [v for v in verdicts if v["accepted"]]
        after = list(ctx["existing"])

        if accepted:
            # Apply them in order, each on the result of the last, so the final
            # spec carries all of them and the SQL is compiled from the finished
            # IR — one write, not one per variation.
            working = rule_spec
            applied = []
            for v in accepted:
                try:
                    working, after, _c = add_variation_value(
                        working, schema, v["spelling"])
                    applied.append(v)
                except RuleEditError as e:
                    # One bad variation must not lose the good ones already applied.
                    v["accepted"] = False
                    v["reason_code"] = "not_applied"
                    v["reason"] = str(e)
            accepted = applied

            if accepted:
                s.execute(
                    text("""UPDATE validation_rule
                               SET rule_spec = CAST(:rs AS JSONB),
                                   updated_at = now(), updated_by = :actor
                             WHERE rule_id = :rid"""),
                    {"rs": json.dumps(working, default=str),
                     "actor": body.actor or "user", "rid": rule_id},
                )
                s.commit()
                # Drop the stale compiled-SQL cache row (own tx — see _purge_rule_sql).
                _purge_rule_sql(s, rule_id)

        tenant_mga = body.mga or _tenant_name(s, tenant_id)

    # Record each accepted variation in the SHARED vocabulary so the next contract
    # — for this tenant or any other — is generated already knowing it. Never
    # fatal: the rule edit above is committed and correct regardless.
    vocab_written = 0
    for v in accepted:
        try:
            vocab = vocabulary.add_admin_synonym(
                v.get("matched_value") or (ctx["base"][0] if ctx["base"] else None),
                v["spelling"], field=ctx["field"],
                created_by=getattr(principal, "user_id", None),
                source_rule_id=rule_id, source_tenant_id=tenant_id)
            v["vocabulary_written"] = bool(vocab)
            vocab_written += bool(vocab)
        except Exception as exc:
            v["vocabulary_written"] = False
            print(f"[variation] vocabulary write skipped for rule {rule_id}: "
                  f"{type(exc).__name__}: {str(exc)[:200]}")
    if vocab_written:
        try:
            vocabulary.reload_vocab()
        except Exception:
            pass

    if accepted:
        _log(tenant_mga, body.actor, "rule.variation_value_added",
             target=f"rule:{rule_id}",
             details={"contract_id": contract_id, "program_id": program_id,
                      "rule_name": row["rule_name"],
                      "variations": [v["spelling"] for v in accepted],
                      "refused": [v["spelling"] for v in verdicts if not v["accepted"]],
                      "template": ctx["template"], "field": ctx["field"],
                      "vocabulary_written": vocab_written})

    return {
        "rule_id": rule_id,
        "accepted": bool(accepted),
        "accepted_count": len(accepted),
        "refused_count": len(verdicts) - len(accepted),
        "variation_values": after,
        "sql_changed": bool(accepted),
        "vocabulary_written": bool(vocab_written),
        "clause_page": clause_page,
        # One entry per line the admin submitted, in their order.
        "results": [{
            "spelling": v["spelling"],
            "accepted": v["accepted"],
            "reason_code": v["reason_code"],
            "reason": v["reason"],
            "clause_quote": v.get("clause_quote"),
            "matched_value": v.get("matched_value"),
            "vocabulary_written": v.get("vocabulary_written", False),
        } for v in verdicts],
    }


class RuleVariationRemoveBody(BaseModel):
    # `variations` is the real shape — the UI stages several ✕ clicks and commits
    # them together. `variation` is the single-item form, kept because it is the
    # obvious way to call this endpoint by hand.
    variations: Optional[List[str]] = None
    variation: Optional[str] = None
    actor: Optional[str] = None
    mga: Optional[str] = None

    def candidates(self) -> List[str]:
        raw = self.variations if self.variations is not None else (
            [self.variation] if self.variation is not None else [])
        return [str(v).strip() for v in raw if str(v or "").strip()]


@router.post("/programs/{program_id}/contracts/{contract_id}/rules/{rule_id}/variation-values/remove")
def rule_remove_variation_value(program_id: int, contract_id: int, rule_id: int,
                                body: RuleVariationRemoveBody,
                                principal: Principal = Depends(require_role("tenant_admin"))):
    """Take back spellings a tenant_admin previously taught this rule.

    The undo half of rule_add_variation_value, and intentionally not its mirror
    image. Adding widens what a rule accepts, so it is checked by six gates and a
    model. Removing only narrows it back toward what the contract says, so it needs
    no checker at all — but it does need one hard guard, enforced in
    rule_editor.remove_variation_value: A VALUE THE CONTRACT ITSELF NAMES CANNOT BE
    REMOVED. Those values are seeded into the same list, and they are not the
    admin's to drop; that attempt is reported against that one spelling.

    SEVERAL AT ONCE, for the same reason the add route batches: each removal
    recompiles the rule's SQL from the finished IR, so committing five ✕ clicks
    together is one recompile and one row write instead of five of each. They are
    applied in order onto one working spec, and one bad entry never discards the
    good ones — it comes back refused in `results` while the rest still commit.

    A REFUSED REMOVAL IS A 200, matching the add route: it is a normal answer
    carrying a reason. 4xx stays reserved for the wrong rule type (400), the wrong
    role (403), or a rule that is not yours (404).

    POST rather than DELETE because the spellings travel in the body — free text
    that can carry slashes, dots and spaces, which a proxy may normalize if it is
    encoded into a path segment.

    Two honest caveats are computed and RETURNED rather than hidden, because in
    both cases the rule may keep matching a spelling after it disappears from the
    list, and an admin who is not told that will believe the removal failed:

      still_matched_by_dictionary — the SHARED vocabulary canonicalizes the removed
          spelling onto the same token as a contract value, so the compiled SQL
          matches it regardless of this list (rule_compiler._enum_match_rows).
      vocabulary_removed — whether the shared-dictionary entry this rule itself
          added was taken back too. False when migration 17 is absent (provenance
          cannot be proven, so nothing global is touched) or when the entry came
          from somewhere other than this rule.
    """
    from contract_upload_services.output_schema import build_output_schema
    from contract_upload_services.rule_editor import (
        remove_variation_value, variation_context, RuleEditError)
    from contract_upload_services import vocabulary

    spellings = body.candidates()
    if not spellings:
        raise HTTPException(400, "supply at least one variation to remove")
    if len(spellings) > 50:
        raise HTTPException(400, "remove at most 50 variations at a time")

    with SessionLocal() as s:
        contract = s.get(Contract, contract_id)
        if not contract or contract.program_id != program_id:
            raise HTTPException(404, "contract not found for this program")
        assert_tenant_owns(principal, contract.tenant_id)
        if not contract.output_template_id:
            raise HTTPException(400, "this contract has no output template to map fields against")
        tmpl = s.get(ExportTemplate, contract.output_template_id)
        if not tmpl:
            raise HTTPException(404, "output template not found")
        schema = build_output_schema(_template_fields_from_structure(tmpl.structure))
        tenant_id = contract.tenant_id

        row = _load_rule_for_contract(s, contract_id, rule_id)
        if row["rule_status"] == "disabled":
            raise HTTPException(400, "this rule was removed; restore it before editing")
        rule_spec = _json_value(row["rule_spec"]) or {}

        ctx = variation_context(rule_spec)
        if ctx is None:
            raise HTTPException(
                400, "Spellings apply only to rules that match a value against the "
                     "contract — an allowed or prohibited list, or a conditional value.")

        # Apply them in order onto ONE working spec, so the SQL is compiled from the
        # finished IR and the row is written once. A refusal drops that one spelling
        # and leaves the rest of the batch intact.
        working = rule_spec
        after = list(ctx["existing"])
        results, removed = [], []
        for sp in spellings:
            try:
                working, after, ctx = remove_variation_value(working, schema, sp)
                removed.append(sp)
                results.append({"spelling": sp, "removed": True,
                                "reason_code": "removed", "reason": None})
            except RuleEditError as e:
                results.append({"spelling": sp, "removed": False,
                                "reason_code": "not_removed", "reason": str(e)})

        if removed:
            s.execute(
                text("""UPDATE validation_rule
                           SET rule_spec = CAST(:rs AS JSONB),
                               updated_at = now(), updated_by = :actor
                         WHERE rule_id = :rid"""),
                {"rs": json.dumps(working, default=str),
                 "actor": body.actor or "user", "rid": rule_id},
            )
            s.commit()
            _purge_rule_sql(s, rule_id)
        tenant_mga = body.mga or _tenant_name(s, tenant_id)

    # Undo the shared-dictionary writes this same rule made, if it made any. Scoped
    # to source='admin' AND source_rule_id=<this rule>, so a seeded term or another
    # rule's addition can never be reached from here. Never fatal.
    vocab_removed = 0
    for sp in removed:
        for b in ctx["base"]:
            try:
                if vocabulary.remove_admin_synonym(b, sp, field=ctx["field"],
                                                   source_rule_id=rule_id):
                    vocab_removed += 1
                    break
            except Exception as exc:
                print(f"[variation] vocabulary removal skipped for rule {rule_id}: "
                      f"{type(exc).__name__}: {str(exc)[:200]}")
    if vocab_removed:
        try:
            vocabulary.reload_vocab()
        except Exception:
            pass

    # Does the rule STILL match any of them? The compiler canonicalizes both the
    # rule value and the cell through the shared vocabulary, so a spelling the
    # dictionary collapses onto a contract value keeps matching after removal.
    still_matched = {}
    for sp in removed:
        try:
            tok = vocabulary.canonical_token(sp, ctx["field"])
            for b in ctx["base"]:
                if tok and vocabulary.canonical_token(b, ctx["field"]) == tok:
                    still_matched[sp] = b
                    break
        except Exception:
            pass
    for r in results:
        r["still_matched_by_dictionary"] = still_matched.get(r["spelling"])

    if removed:
        _log(tenant_mga, body.actor, "rule.variation_value_removed",
             target=f"rule:{rule_id}",
             details={"contract_id": contract_id, "program_id": program_id,
                      "rule_name": row["rule_name"], "variations": removed,
                      "refused": [r["spelling"] for r in results if not r["removed"]],
                      "template": ctx["template"], "field": ctx["field"],
                      "vocabulary_removed": vocab_removed,
                      "still_matched_by_dictionary": still_matched})

    return {
        "rule_id": rule_id,
        "removed": removed,
        "removed_count": len(removed),
        "refused_count": len(results) - len(removed),
        "variation_values": after,
        "sql_changed": bool(removed),
        "vocabulary_removed": vocab_removed,
        "still_matched_by_dictionary": still_matched,
        # One entry per spelling submitted, in the caller's order.
        "results": results,
    }


# ---- Resolve a review-queue clause by assigning it an output field ----------

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


@router.post("/programs/{program_id}/contracts/{contract_id}/clause-routing/{clause_id}/resolve")
def resolve_clause_routing(program_id: int, contract_id: int, clause_id: int,
                           body: ClauseResolveBody,
                           principal: Principal = Depends(current_principal)):
    """Generate a validation rule for an `in_review` (unmapped) clause by binding
    it to a user-chosen Output-Template field.

    Re-runs the real generation pipeline (intent extraction → IR mapping forced
    onto the chosen field → verify/compile) for this one clause, then persists
    the rule(s), updates the clause status to 'rules_generated' and removes it
    from the review queue. If the field genuinely cannot represent the clause,
    nothing is written and the verifier's reason is returned.
    """
    from contract_upload_services.output_schema import build_output_schema
    from contract_upload_services.manual_rule_resolution import (
        generate_rules_for_clause_field,
    )
    from contract_upload_services import db_persister

    # Accept a single field or a list (rule spanning several columns). The first
    # entry is the primary; the rest scope/condition the rule.
    raw_fields = [f for f in (body.output_fields or []) if f and f.strip()]
    if not raw_fields and body.output_field and body.output_field.strip():
        raw_fields = [body.output_field.strip()]
    if not raw_fields:
        raise HTTPException(400, "output_field (or output_fields) is required")

    with SessionLocal() as s:
        contract = s.get(Contract, contract_id)
        if not contract or contract.program_id != program_id:
            raise HTTPException(404, "contract not found for this program")
        assert_tenant_owns(principal, contract.tenant_id)
        if not contract.output_template_id:
            raise HTTPException(400, "this contract has no output template to map fields against")
        tmpl = s.get(ExportTemplate, contract.output_template_id)
        if not tmpl:
            raise HTTPException(404, "output template not found")

        template_fields = _template_fields_from_structure(tmpl.structure)
        schema = build_output_schema(template_fields)

        # Resolve every pick to an exact template column (tolerant match), keeping
        # order and dropping duplicates. The first becomes the primary.
        resolved_fields: list[str] = []
        for raw in raw_fields:
            rf = schema.resolve_field(raw.strip())
            if not rf:
                raise HTTPException(
                    400, f"'{raw}' is not a field in this contract's "
                         f"output template")
            if rf not in resolved_fields:
                resolved_fields.append(rf)
        resolved = resolved_fields[0]
        extra_fields = resolved_fields[1:]
        chosen_field = next(
            (f for f in template_fields if f.get("name") == resolved), None)
        if chosen_field is None:
            raise HTTPException(400, "selected output field could not be loaded")

        clause = s.execute(
            text("""SELECT clause_id, contract_id, clause_type, title, text,
                           page_number, section_header, rule_generation_status
                    FROM clauses_extracted
                    WHERE clause_id = :clid AND contract_id = :cid"""),
            {"clid": clause_id, "cid": contract_id},
        ).mappings().first()
        if not clause:
            raise HTTPException(404, "clause not found for this contract")

        tenant_id = contract.tenant_id
        output_template_id = contract.output_template_id
        tenant_mga = body.mga or _tenant_name(s, tenant_id)

    clause_dict = {
        "clause_id":      clause["clause_id"],
        "contract_id":    clause["contract_id"],
        "clause_type":    clause["clause_type"],
        "title":          clause["title"],
        "text":           clause["text"],
        "page_number":    clause["page_number"],
        "section_header": clause["section_header"],
    }
    contract_ctx = {"tenant_id": tenant_id, "contract_id": contract_id,
                    "program_id": program_id}

    # Heavy step (LLM calls + DuckDB verify). FastAPI runs this sync route in a
    # worker thread, so blocking here is fine.
    validation_rules, review_queue, control_register = (
        generate_rules_for_clause_field(
            clause_dict, chosen_field, template_fields, schema, contract_ctx,
            note=body.note, extra_field_names=extra_fields)
    )

    if not validation_rules:
        # Could not bind to the chosen field — surface why (clause stays in_review).
        reason = None
        for bucket in (review_queue, control_register):
            for it in bucket:
                if isinstance(it, dict) and it.get("reason"):
                    reason = it["reason"]
                    break
            if reason:
                break
        return {
            "ok": False,
            "clause_id": clause_id,
            "output_field": resolved,
            "output_fields": resolved_fields,
            "status": "in_review",
            "reason": reason or
                      f"The clause could not be expressed against '{resolved}'.",
        }

    created = db_persister.persist_resolved_rules(
        contract_id=contract_id,
        program_id=program_id,
        tenant_id=tenant_id,
        db_clause_id=clause_id,
        output_template_id=output_template_id,
        validation_rules=validation_rules,
        actor=body.actor or "user",
    )

    _log(tenant_mga, body.actor, "clause.resolved_to_field",
         target=f"clause:{clause_id}",
         details={"contract_id": contract_id, "program_id": program_id,
                  "output_field": resolved,
                  "output_fields": resolved_fields,
                  "note": (body.note or "").strip() or None,
                  "rule_ids": [c["rule_id"] for c in created],
                  "rule_names": [c["rule_name"] for c in created]})

    return {
        "ok": True,
        "clause_id": clause_id,
        "output_field": resolved,
        "output_fields": resolved_fields,
        "status": "rules_generated",
        "note": (body.note or "").strip() or None,
        "created_rules": created,
    }


@router.delete("/programs/{program_id}/contracts/{contract_id}/rules/{rule_id}")
def rule_delete(program_id: int, contract_id: int, rule_id: int,
                mga: Optional[str] = None, actor: Optional[str] = None,
                principal: Principal = Depends(current_principal)):
    """Soft-delete a validation rule (rule_status='disabled').

    Excluded from validation in every lane and hidden from the contract view,
    but the row + exception history are preserved and it is recoverable.
    """
    with SessionLocal() as s:
        contract = s.get(Contract, contract_id)
        if not contract or contract.program_id != program_id:
            raise HTTPException(404, "contract not found for this program")
        assert_tenant_owns(principal, contract.tenant_id)
        tenant_id = contract.tenant_id
        row = _load_rule_for_contract(s, contract_id, rule_id)
        changed = row["rule_status"] != "disabled"
        if changed:
            s.execute(
                text("""UPDATE validation_rule
                           SET rule_status = 'disabled', updated_at = now(), updated_by = :actor
                         WHERE rule_id = :rid"""),
                {"actor": actor or "user", "rid": rule_id})
            s.commit()
            # Drop the compiled-SQL cache row (own tx — see _purge_rule_sql).
            _purge_rule_sql(s, rule_id)
        tenant_mga = mga or _tenant_name(s, tenant_id)
        ofields = _output_fields_from_rule(
            {"canonical_target": _json_value(row["canonical_target"]) or {}})

    # Only audit a real state transition (an idempotent re-delete is a no-op).
    if changed:
        _log(tenant_mga, actor, "rule.disabled", target=f"rule:{rule_id}",
             details={"contract_id": contract_id, "program_id": program_id,
                      "rule_name": row["rule_name"],
                      "output_field": ofields[0] if ofields else None})
    return {"ok": True, "rule_id": rule_id, "already_disabled": not changed}


@router.post("/programs/{program_id}/contracts/{contract_id}/activate")
def program_contract_activate(program_id: int, contract_id: int,
                              principal: Principal = Depends(current_principal)):
    """Make one contract the active one for its Program (schedule-scoped).

    Supersedes the other contract(s) for the SAME schedule only, so a program can
    keep one active contract per schedule. A contract with no schedule_key (legacy)
    still supersedes every other contract in the program.
    Failed/drafted/extracting contracts must be re-uploaded before activation.
    """
    with SessionLocal() as s:
        target = s.get(Contract, contract_id)
        if not target or target.program_id != program_id:
            raise HTTPException(404, "contract not found for this program")
        assert_tenant_owns(principal, target.tenant_id)
        if target.status in ("failed", "drafted", "extracting"):
            raise HTTPException(400, f"cannot activate a contract with status '{target.status}'")

        # One active contract per (program, schedule) — supersede same-schedule
        # siblings. Legacy (schedule_key IS NULL) supersedes all, as before.
        sib_q = s.query(Contract).filter(
            Contract.program_id == program_id,
            Contract.id != contract_id,
        )
        if target.schedule_key is not None:
            sib_q = sib_q.filter(Contract.schedule_key == target.schedule_key)

        for sib in sib_q.all():
            if sib.status not in ("failed", "drafted", "extracting"):
                sib.status = "superseded"
        target.status = "active"
        # A program is "active" once it has an active contract.
        prog = s.get(Program, program_id)
        if prog:
            prog.status = "active"
        s.commit()
        _log(_tenant_name(s, target.tenant_id) or "", _actor(principal), "contract_activated",
             target=str(contract_id),
             details={"program_id": program_id, "output_template_id": target.output_template_id})
        # Return the full updated list for this program
        rows = s.query(Contract).filter(Contract.program_id == program_id)\
                .order_by(Contract.id.desc()).all()
        return [{"id": c.id, "filename": c.filename, "status": c.status,
                 "extracted": c.extracted,
                 "output_template_id": c.output_template_id,
                 "schedule_key": c.schedule_key,
                 "created_at": _iso_utc(c.created_at)}
                for c in rows]


# def _stub_contract_extract(filename: str) -> dict:
#     """Placeholder for AI extraction so the UI can demonstrate the pattern."""
#     base = filename.rsplit(".", 1)[0]
#     return {
#         "name": base.replace("_", " ").title(),
#         "lead_carrier": "Pinnacle Insurance Co.",
#         "admin_party": "",
#         "bdx_frequency": "monthly",
#         "business_segment": "Commercial Lines",
#         "product_line": "General Liability",
#         "distribution_channel": "wholesale",
#         "territory": "US",
#         "commercial_terms": {"commission_pct": 15, "profit_share": True},
#     }


# ---- S-12 Operations Home — dashboard stats + activity --------------------

@router.get("/dashboard/stats")
def dashboard_stats(mga: str, principal: Principal = Depends(current_principal)):
    """Aggregates for the home dashboard."""
    today = datetime.utcnow().date()
    week_ago = datetime.utcnow() - timedelta(days=7)
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        # `upload` is overloaded (§2a): count only real ingest rows (mapper_id set),
        # not the canonical lineage shadow rows — matches the /uploads list.
        uploads_today = s.query(Upload).filter(
            Upload.tenant_id == tid,
            Upload.mapper_id.isnot(None),
            func.date(Upload.ingested_at) == today,
        ).count()
        total_uploads = s.query(Upload).filter(
            Upload.tenant_id == tid, Upload.mapper_id.isnot(None)).count()
        programs = s.query(Program).filter(Program.tenant_id == tid,
                                           Program.is_app_managed.is_(True),
                                           Program.status == "active").count()
        # "Active setups" = activated output templates (a carrier+program config
        # that's been activated to generate BDX), plus the carriers they span.
        active_setups = s.query(ExportTemplate).filter(
            ExportTemplate.tenant_id == tid, ExportTemplate.is_active == 1).count()
        active_setup_carriers = s.query(
            func.count(func.distinct(ExportTemplate.carrier_party_id))
        ).filter(
            ExportTemplate.tenant_id == tid, ExportTemplate.is_active == 1,
            ExportTemplate.carrier_party_id.isnot(None),
        ).scalar() or 0
        parties = s.query(Party).filter(
            or_(and_(Party.tenant_id == tid, Party.is_app_managed.is_(True)),
                Party.scope == "global")
        ).count()

        # A "run" is a generated output (OutputExport), which carries the
        # validation result — matches the dashboard's "Recent runs" table.
        # "Runs this week" tile + a 7-day daily series (oldest → newest) for the spark.
        week_rows = s.query(
            func.date(OutputExport.created_at), func.count(OutputExport.id)
        ).filter(
            OutputExport.tenant_id == tid,
            OutputExport.created_at >= week_ago,
        ).group_by(func.date(OutputExport.created_at)).all()
        by_date = {str(d): c for d, c in week_rows}
        runs_by_day = [by_date.get(str(today - timedelta(days=i)), 0)
                       for i in range(6, -1, -1)]
        runs_this_week = sum(runs_by_day)

        # "Exceptions to review" tile — real exception totals across generated
        # outputs (replaces the previous hardcoded 0). Counts every flagged
        # exception; per-exception resolution state is not yet subtracted.
        exc_sum, exc_runs = s.query(
            func.coalesce(func.sum(OutputExport.exception_count), 0),
            func.count(OutputExport.id),
        ).filter(
            OutputExport.tenant_id == tid,
            OutputExport.status == "has_exceptions",
        ).one()

        # "Mapping tasks" tile (Kavachio admin) — open items in the data-model queue.
        mapping_tasks_open = s.query(AdminMappingTask).filter(
            AdminMappingTask.tenant_id == tid,
            AdminMappingTask.status.in_(("open", "in_progress")),
        ).count()

        # "Avg Turnaround" tile (tenant/operator) — average wall-clock time from a
        # file's upload to its validated output, over the last 30 days. Combines
        # BOTH validation pipelines, since a tenant's uploads can go through
        # either (or both):
        #   1. Uploads screen  → upload.ingested_at → validation_run.completed_at
        #   2. Process Bordereau (direct run) → landing_record.created_at →
        #      output_exports.created_at (via landing_record.output_export_id)
        # upload.ingested_at is naive (datetime.utcnow(), no tz) while
        # validation_run.completed_at is timestamptz; diffing them directly lets
        # Postgres cast the naive side using the session's timezone, silently
        # skewing the result — `AT TIME ZONE 'UTC'` marks it UTC first so the
        # subtraction is correct everywhere. landing_record/output_exports are
        # both naive already, so no cast is needed there.
        #
        # Two data-integrity traps found while validating this against real
        # rows, both fixed below:
        #   - An upload can be RE-validated many times (found one with 38 runs
        #     spanning 5 days); diffing every completed run against the same
        #     original ingested_at turned a normal turnaround into "5 days" —
        #     only the upload's FIRST completed validation is a real "upload →
        #     validated" measurement, so re-validations are excluded.
        #   - "Fix & re-run" reassigns landing_record.output_export_id to a
        #     freshly-generated export, so a landing re-rendered long after its
        #     own created_at (found real gaps of 19-44 hours) looked like an
        #     enormous turnaround. Process Bordereau is synchronous, so
        #     anything over an hour there is a reused landing, not real
        #     latency — capped out.
        avg_turnaround_min = s.execute(
            text("""
                WITH first_validation AS (
                    SELECT bdx_upload_id, MIN(completed_at) AS completed_at
                    FROM validation_run
                    WHERE tenant_id = :tid AND status = 'completed' AND completed_at IS NOT NULL
                    GROUP BY bdx_upload_id
                ),
                durations AS (
                    SELECT EXTRACT(EPOCH FROM (fv.completed_at - (u.ingested_at AT TIME ZONE 'UTC'))) / 60.0 AS mins
                    FROM first_validation fv
                    JOIN upload u ON u.upload_id = fv.bdx_upload_id
                    WHERE fv.completed_at > (u.ingested_at AT TIME ZONE 'UTC')
                      AND fv.completed_at >= NOW() - INTERVAL '30 days'
                    UNION ALL
                    SELECT EXTRACT(EPOCH FROM (oe.created_at - lr.created_at)) / 60.0 AS mins
                    FROM landing_record lr
                    JOIN output_exports oe ON oe.id = lr.output_export_id
                    WHERE lr.tenant_id = :tid
                      AND lr.output_export_id IS NOT NULL
                      AND oe.created_at > lr.created_at
                      AND oe.created_at - lr.created_at <= INTERVAL '60 minutes'
                      AND oe.created_at >= NOW() - INTERVAL '30 days'
                )
                SELECT AVG(mins) FROM durations
            """),
            {"tid": tid},
        ).scalar()

        return {
            "uploads_today": uploads_today,
            "uploads_total": total_uploads,
            "open_bdx_cycles": programs,
            "active_setups": active_setups,             # "Active setups" tile
            "active_setup_carriers": int(active_setup_carriers),
            "parties_in_directory": parties,
            "pending_exceptions": int(exc_sum or 0),
            "exception_runs": int(exc_runs or 0),
            "ai_cache_hit_rate": None,
            "runs_this_week": runs_this_week,
            "runs_by_day": runs_by_day,
            "mapping_tasks_open": mapping_tasks_open,
            "avg_turnaround_min": round(avg_turnaround_min, 1) if avg_turnaround_min is not None else None,
        }


# ---- Program Management — the CARRIER-scoped oversight dashboard ----------
# Answers a different question from /dashboard/stats. That one is operational
# ("what did I process today" — runs, exceptions, turnaround). This one is
# oversight of ONE carrier's program book: how big it is, how it's spread, what
# is coming due, and what has already slipped.
#
# Built entirely on columns that already exist — no migration. Where the source
# data genuinely isn't there (UW audits), the field returns None so the screen
# can render a dash and say "not tracked" instead of a fabricated 0, which would
# read as "oversight is clean" when it actually means "we don't know".

# A program's term end drives both the "under review" count and the timing
# chart. Buckets are days-from-today, evaluated in order (first match wins).
_REVIEW_BUCKETS = [
    ("overdue",    "Overdue",     lambda d: d < 0),
    ("this_week",  "This week",   lambda d: d <= 7),
    ("this_month", "This month",  lambda d: d <= 30),
    ("30_60",      "30–60d",      lambda d: d <= 60),
    ("60_90",      "60–90d",      lambda d: True),
]


# Stages that mean "currently writing business". Onboarding / In Setup are not
# live yet; Runoff's term has already ended. This is what the Active Programs
# card and the segment chart count — deliberately NOT `_is_live_program()` on its
# own, which cannot see that a term has lapsed (see below).
_WRITING_STAGES = ("Active", "Under Review")


def _bucket_for(days: int) -> str:
    for key, _label, test in _REVIEW_BUCKETS:
        if test(days):
            return key
    return "60_90"


def _is_live_program(program, contract) -> bool:
    """Is this program actually writing business right now?

    NOT simply `status_ops == 'active'`. Two things measured against the dev
    database make that check wrong on real rows:

      1. The column drifts. 96 programs there carry an ACTIVE contract while
         their own status_ops still says 'draft' — the program is promoted when
         a contract is activated, but rows predating that behaviour were never
         caught up (which is exactly what backfill_program_status.py exists to
         repair, and it clearly hasn't been run everywhere). Trusting the column
         alone would drop those programs out of the KPI count, the segment chart
         AND the review count, silently understating the book.
      2. It is not case-normalised — both 'active' and 'Active' exist.

    So liveness is derived from the two signals together: the program says it is
    active, or it holds an activated contract.

    LIMIT, and why callers must not use this alone as "active": it reads
    contract.STATUS, never contract.expiry_dt, and nothing flips a contract to
    inactive when its term lapses. A program whose term ended years ago still
    answers True here. That is correct for "this row is live, not a draft" — the
    question the stage ladder asks — but wrong for "is this writing business",
    which the spec defines as active AND NOT in runoff. For that, test the
    resolved stage against _WRITING_STAGES.
    """
    if (program.status or "").strip().lower() == "active":
        return True
    return contract is not None and (contract.status or "").strip().lower() == "active"


def _current_contract(rows: list) -> Optional[Any]:
    """The contract that speaks for a program right now.

    A program can hold several contracts (versions, or one per schedule_key).
    An `active` one wins; otherwise the most recently created row, so a program
    still mid-setup still reports a stage instead of vanishing from the donut.
    """
    if not rows:
        return None
    active = [c for c in rows if (c.status or "").strip().lower() == "active"]
    pool = active or rows
    return max(pool, key=lambda c: (c.id or 0))


@router.get("/program-management/carriers")
def program_management_carriers(
    mga: str,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    principal: Principal = Depends(current_principal),
):
    """The carrier picker's list — one page of carriers, each with the size of
    its program book.

    A dedicated endpoint rather than reusing /parties for two reasons:
      • /parties is called by selector dropdowns all over the app that expect
        EVERY match, so it can't be made to page by default; and its payload has
        no program counts.
      • Counting here means the picker can show what actually distinguishes one
        carrier from another (how many programs, how many are late) instead of
        a grid of identical cards.

    Sort order is triage order: carriers with overdue bordereaux first, then by
    size of book, then by name. Someone opening this screen is looking for what
    needs attention, and a carrier with one late program matters more than a
    quiet carrier with twenty. Empty carriers sort last either way.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)

        # Count of this tenant's programs for each party, as a correlated
        # subquery so it can both drive the sort AND be paged in one query.
        prog_count = (
            select(func.count(Program.id))
            .where(Program.party_id == Party.id,
                   Program.tenant_id == tid,
                   Program.is_app_managed.is_(True))
            .correlate(Party)
            .scalar_subquery()
        )

        # Overdue bordereaux per carrier, as a correlated subquery for the same
        # reason as prog_count: it has to drive the ORDER BY, and a sort key
        # computed after LIMIT/OFFSET would only order within a page.
        overdue_count = (
            select(func.count(ExpectedSubmission.id))
            .select_from(ExpectedSubmission)
            .join(Program, Program.id == ExpectedSubmission.program_id)
            .where(Program.party_id == Party.id,
                   Program.tenant_id == tid,
                   ExpectedSubmission.status.in_(("overdue", "late")))
            .correlate(Party)
            .scalar_subquery()
        )

        # Same visibility rule as the directory: this tenant's own app-managed
        # parties plus shared globals. Deactivated carriers are left out — you
        # cannot meaningfully oversee a book through a retired party.
        base = s.query(Party, prog_count.label("programs"),
                       overdue_count.label("overdue")).filter(
            or_(and_(Party.tenant_id == tid, Party.is_app_managed.is_(True)),
                Party.scope == "global"),
            or_(Party.is_active.is_(True), Party.is_active.is_(None)),
        )
        if q:
            ql = f"%{q.lower()}%"
            base = base.filter(or_(
                func.lower(Party.legal_name).like(ql),
                func.lower(Party.dba_name).like(ql),
            ))

        total = base.order_by(None).count()
        rows = (base.order_by(desc("overdue"), desc("programs"), Party.legal_name)
                    .offset((page - 1) * page_size).limit(page_size).all())

        return {
            "items": [{
                "id": p.id,
                "legal_name": p.legal_name,
                "dba_name": p.dba_name,
                "party_type": p.party_type,
                "programs": int(c or 0),
                "overdue_bordereaux": int(od or 0),
            } for p, c, od in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }


@router.get("/program-management/stats")
def program_management_stats(
    mga: str,
    carrier_party_id: int,
    horizon_days: int = Query(90, ge=7, le=365),
    principal: Principal = Depends(current_principal),
):
    """Oversight aggregates for ONE carrier's program book.

    `carrier_party_id` is required by design — the screen is per-carrier, so
    there is no "all carriers" shape to fall back to. The tenant is taken from
    the token (resolve_tenant_id), never from `mga`, so a carrier id belonging
    to another tenant simply resolves to zero programs.
    """
    from business_segment import classify as classify_segment
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    cutoff_30d = now_utc - timedelta(days=30)   # aware, so it compares with _as_utc()
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)

        # The carrier itself — scoped exactly like the directory: this tenant's
        # own app-managed parties plus the shared globals.
        carrier = s.query(Party).filter(
            Party.id == carrier_party_id,
            or_(and_(Party.tenant_id == tid, Party.is_app_managed.is_(True)),
                Party.scope == "global"),
        ).first()
        if carrier is None:
            raise HTTPException(404, "carrier not found")

        programs = s.query(Program).filter(
            Program.tenant_id == tid,
            Program.is_app_managed.is_(True),
            Program.party_id == carrier_party_id,
        ).all()
        pids = [p.id for p in programs]

        # Contracts carry the term (inception_dt / expiry_dt). One query for the
        # whole book, grouped in Python — the per-carrier row count is small and
        # it keeps the "which contract counts" rule in one readable place.
        by_program: dict[int, list] = {}
        if pids:
            for c in s.query(Contract).filter(
                Contract.tenant_id == tid, Contract.program_id.in_(pids),
            ).all():
                by_program.setdefault(c.program_id, []).append(c)

        # Overdue bordereaux, straight off the submission calendar. `status` is
        # recomputed from the due date whenever the calendar is rebuilt, so
        # 'overdue' is genuinely past-due and not-yet-received. 'due_today' is
        # deliberately NOT counted — the deadline has not passed yet.
        #
        # 'late' is the retired pre-grace-removal spelling of 'overdue'. init_db
        # folds those rows over, but RLS deployments skip that migration, so it
        # stays in the filter rather than silently dropping their backlog.
        overdue_by_program: dict[int, int] = {}
        schedules_configured = 0
        if pids:
            for prog_id, cnt in s.query(
                ExpectedSubmission.program_id, func.count(ExpectedSubmission.id),
            ).filter(
                ExpectedSubmission.tenant_id == tid,
                ExpectedSubmission.program_id.in_(pids),
                ExpectedSubmission.status.in_(("overdue", "late")),
            ).group_by(ExpectedSubmission.program_id).all():
                overdue_by_program[prog_id] = int(cnt)
            schedules_configured = s.query(SubmissionSchedule).filter(
                SubmissionSchedule.tenant_id == tid,
                SubmissionSchedule.program_id.in_(pids),
            ).count()

        stage_counts: dict[str, int] = {}
        # segment key (lowercased) -> {"total": n, "spellings": {as-typed: n}}
        segment_counts: dict[str, dict] = {}
        bucket_counts = {key: 0 for key, _l, _t in _REVIEW_BUCKETS}
        active_programs = 0
        new_active_30d = 0
        under_review = 0
        rows_out = []

        for p in programs:
            contract = _current_contract(by_program.get(p.id, []))
            term_end = contract.expiry_dt if contract is not None else None
            days = (term_end - today).days if term_end else None

            is_active = _is_live_program(p, contract)
            contract_status = (contract.status or "").strip().lower() if contract is not None else None

            # Stage ladder — derived from real columns, first match wins.
            # Deliberately NOT the document's five-value vocabulary: that would
            # need a status enum the DB doesn't have. These are the same
            # lifecycle positions expressed in the signals we actually store.
            if not is_active:
                stage = "Onboarding" if contract is None else "In Setup"
            elif contract is None:
                stage = "Onboarding"                       # program, no contract yet
            elif contract_status in ("drafted", "extracting"):
                stage = "In Setup"
            elif days is None:
                stage = "Active"                           # live, term unknown
            elif days < 0:
                stage = "Runoff"                           # term ended, still reporting
            elif days <= horizon_days:
                stage = "Under Review"                     # continuation decision due
            else:
                stage = "Active"
            stage_counts[stage] = stage_counts.get(stage, 0) + 1

            # Writing business = the resolved STAGE, so the card and the donut can
            # no longer contradict each other. `is_active` alone counted a program
            # whose term lapsed years ago, which the spec excludes ("active but not
            # in runoff").
            if stage in _WRITING_STAGES:
                active_programs += 1
                # Segment labels come from the data itself (program.business_segment),
                # never a baked-in list — a tenant's own segment names show as typed.
                # Grouped case-insensitively because the real column holds both
                # 'Casualty' and 'CASUALTY'; splitting those into two bars would
                # halve a segment's apparent size. The label rendered is the
                # spelling that occurs most often, so the chart still reads in the
                # tenant's own words.
                # DISPLAY-ONLY: the stored column is free text (contract
                # extraction writes whole class-of-business clauses into it), so
                # the chart is grouped by the classified segment rather than the
                # raw sentence. Nothing is written back — see business_segment.py.
                seg = classify_segment(p.business_segment) or "Unspecified"
                key = seg.lower()
                bucket = segment_counts.setdefault(key, {"total": 0, "spellings": {}})
                bucket["total"] += 1
                bucket["spellings"][seg] = bucket["spellings"].get(seg, 0) + 1
                created = _as_utc(p.created_at)
                if created and created >= cutoff_30d:
                    new_active_30d += 1

            # "Under review" = term ends inside the look-ahead window, or has
            # already passed with the program still live. Same population the
            # timing chart buckets, so the card and the chart can never disagree.
            #
            # Deliberately still keyed on `is_active`, NOT _WRITING_STAGES: a
            # program in runoff has an OVERDUE continuation decision, and the spec
            # wants those counted ("coming due or already overdue"). Narrowing this
            # to writing-business would silently empty the card and the red Overdue
            # bar. The two cards answer different questions and so use different
            # populations.
            if is_active and days is not None and days <= horizon_days:
                under_review += 1
                bucket_counts[_bucket_for(days)] += 1

            rows_out.append({
                "program_id": p.id,
                "program_name": p.name,
                # Lets the UI re-count "newly added" over any window the reader
                # picks without another round trip — the whole book is in this
                # payload already, so the range filter is a client-side slice.
                "created_at": _as_utc(p.created_at).isoformat() if p.created_at else None,
                # Raw value kept as-is (the UI shows it as the cell tooltip, so the
                # contract's own wording stays visible); `segment_label` is what the
                # Segment column renders.
                "business_segment": p.business_segment,
                "segment_label": classify_segment(p.business_segment) or "Unspecified",
                "product_line": p.product_line,
                "status": p.status,
                "stage": stage,
                "bdx_frequency": p.bdx_frequency,
                "inception_dt": contract.inception_dt.isoformat()
                    if contract is not None and contract.inception_dt else None,
                "term_end": term_end.isoformat() if term_end else None,
                "days_to_term_end": days,
                "contract_id": contract.id if contract is not None else None,
                "contract_status": contract.status if contract is not None else None,
                "overdue_bordereaux": overdue_by_program.get(p.id, 0),
            })

        # Most-urgent first, then the rest of the book. Programs with no term end
        # sort last rather than jumping the queue on a None.
        rows_out.sort(key=lambda r: (r["days_to_term_end"] is None,
                                     r["days_to_term_end"] if r["days_to_term_end"] is not None else 0))

        return {
            "carrier": {
                "id": carrier.id,
                "legal_name": carrier.legal_name,
                "dba_name": carrier.dba_name,
                "party_type": carrier.party_type,
            },
            "horizon_days": horizon_days,
            "as_of": today.isoformat(),
            # --- the four KPI cards ---
            "active_programs": active_programs,
            "new_active_30d": new_active_30d,
            "programs_under_review": under_review,
            "overdue_bordereaux": {
                "programs": len(overdue_by_program),
                "submissions": sum(overdue_by_program.values()),
            },
            # No UW-audit entity exists. None (not 0) so the screen renders "—".
            "overdue_audits": None,
            # --- the three charts ---
            "programs_per_stage": [{"label": k, "value": v} for k, v in
                                   sorted(stage_counts.items(), key=lambda kv: -kv[1])],
            "active_per_segment": [
                {"label": max(b["spellings"].items(), key=lambda kv: kv[1])[0], "value": b["total"]}
                for _k, b in sorted(segment_counts.items(), key=lambda kv: -kv[1]["total"])],
            "review_buckets": [{"key": k, "label": lbl, "value": bucket_counts[k]}
                               for k, lbl, _t in _REVIEW_BUCKETS],
            # --- the per-program table ---
            "programs": rows_out,
            "total_programs": len(programs),
            # Lets the UI distinguish "nothing overdue" from "no calendar built",
            # which look identical if you only send a 0.
            "calendar_configured": schedules_configured > 0,
        }


@router.get("/activity")
def activity_list(mga: str, limit: int = 25, actions: Optional[str] = None,
                  principal: Principal = Depends(current_principal)):
    """Recent activity, newest first.

    `actions` (comma-separated) narrows to specific event types BEFORE the limit
    is applied. The notification surfaces need this: they show deadline
    reminders, but the feed carries every activity row this tenant produces —
    uploads, logins, contract uploads — so a plain "newest 25" spends almost all
    of its budget on rows the caller then throws away. On a real tenant that
    left 19 of 93 reminders reachable, and each new reminder silently pushed an
    older one out of view. Filtering first makes the limit mean what the caller
    intended.
    """
    wanted = [a.strip() for a in (actions or "").split(",") if a.strip()]
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        q = s.query(ActivityEvent).filter(ActivityEvent.tenant_id == tid)
        if wanted:
            q = q.filter(ActivityEvent.action.in_(wanted))
        rows = (
            q.order_by(desc(ActivityEvent.created_at))
            .limit(max(1, min(limit, 500))).all()
        )
        return [{"id": e.id, "actor": e.actor, "action": e.action,
                 "target": e.target, "details": e.details,
                 "created_at": _iso_utc(e.created_at)}
                for e in rows]


# ---- Platform (cross-tenant) dashboard — kavachio_admin only --------------
# Everything above is tenant-scoped (forces an `mga`). The platform dashboard
# aggregates ACROSS all tenants for the Kavachio admin. No tenant filter is
# applied; access is gated to the kavachio_admin role server-side.

def _norm_role(r: Optional[str]) -> str:
    r = (r or "").lower()
    if r == "kavachio_admin":
        return "kavachio_admin"
    if r in ("tenant_admin", "admin"):
        return "tenant_admin"
    return "tenant_user"        # ops / read_only / tenant_user / anything else


def _iso_ts(dt: Any) -> Optional[str]:
    """Self-contained ISO-UTC serializer (stored timestamps are naive UTC).
    Avoids depending on a helper that may not be re-exported in every build."""
    if dt is None:
        return None
    s = dt.isoformat()
    return s if (s.endswith("Z") or "+" in s) else s + "Z"


# ---- Platform-admin notifications ----------------------------------------
# Cross-tenant events Kavachio staff need to know about (today: a broker taking
# a Bordereau Setup live). The WRITE side lives at each event's own call site,
# via notifications.notify_platform_admins — these two endpoints are only the
# read side, so adding a new notifiable event never touches this file.

class NotificationsReadBody(BaseModel):
    # The newest notification timestamp the client actually rendered. Marking
    # read UP TO that point, rather than "now", means anything that arrives
    # while the admin is looking at the list stays unread instead of being
    # silently cleared. Omitted → mark everything up to now.
    upto: Optional[str] = None


@router.get("/admin/notifications")
def admin_notifications(
    limit: int = Query(20, ge=1, le=200),
    kind: Optional[str] = None,
    unread_only: bool = False,
    principal: Principal = Depends(require_role("kavachio_admin")),
):
    """This platform admin's notification feed: {items, unread, seen_at,
    latest_at}. `unread` counts EVERY unread notification, not just the ones on
    this page, so the count stays true when `limit` caps the list."""
    from notifications import feed_for_user
    return feed_for_user(principal.user_id, limit=limit, kind=kind,
                         unread_only=unread_only)


@router.post("/admin/notifications/read")
def admin_notifications_read(
    body: NotificationsReadBody = Body(default_factory=NotificationsReadBody),
    principal: Principal = Depends(require_role("kavachio_admin")),
):
    """Move this admin's read watermark forward (per-user, so one admin reading
    their feed never clears anyone else's). Returns the new {unread, seen_at}."""
    from notifications import mark_read
    return mark_read(principal.user_id, _parse_client_dt(body.upto))


# Date-range presets for the dashboard filter → number of days to look back.
def _range_days(rng: str) -> int:
    rng = (rng or "30d").lower()
    fixed = {"1d": 1, "today": 1, "7d": 7, "week": 7,
             "30d": 30, "month": 30, "90d": 90, "quarter": 90,
             "12m": 365, "year": 365, "all": 730}
    if rng in fixed:
        return fixed[rng]
    if rng == "ytd":                       # 1 Jan of the current year → today
        now = datetime.utcnow()
        return max(1, (now.date() - datetime(now.year, 1, 1).date()).days + 1)
    return 30


@router.get("/dashboard/platform")
def platform_dashboard(window: str = Query("30d", alias="range"),
                       _p: Principal = Depends(require_role("kavachio_admin"))):
    """Cross-tenant platform overview for the Kavachio admin dashboard.
    Aggregates every tenant (no tenant filter). Platform-admin only.

    `range` selects the time window that drives every time-based metric
    (runs, series, severity, per-tenant volume, mapping resolutions):
    1d / 7d / 30d (default) / 90d / ytd / 12m / all. Point-in-time counts
    (tenants, users, setups, programs) are NOT windowed."""
    days = _range_days(window)
    now = datetime.utcnow()
    today = now.date()
    dstart = now - timedelta(days=days)          # window start
    dprev = now - timedelta(days=2 * days)       # prior window start (for the delta)
    with SessionLocal() as s:
        # --- Tenants (point-in-time) ---
        # Derived status mirrors the /tenants list EXACTLY so the two screens
        # agree: a broker is "invited" only while nobody has signed in yet
        # (pending invites AND zero active users); once any user is active it's
        # "active", even with outstanding invites. active + invited + inactive
        # == total.
        tenants = s.query(Tenant).all()
        tenant_total = len(tenants)
        tname = {t.id: (t.legal_name or (t.tenant_name or "").title()) for t in tenants}
        tcode = {t.id: t.tenant_name for t in tenants}
        pending_tids = {tid for (tid,) in s.query(func.distinct(AppUser.tenant_id)).filter(
            AppUser.role != "kavachio_admin",
            AppUser.status.in_(("invited", "pending"))).all()}
        active_user_tids = {tid for (tid,) in s.query(func.distinct(AppUser.tenant_id)).filter(
            AppUser.role != "kavachio_admin",
            AppUser.status == "active").all()}
        tenant_inactive = sum(1 for t in tenants if not t.is_active)
        tenant_invited = sum(1 for t in tenants if t.is_active
                             and t.id in pending_tids and t.id not in active_user_tids)
        tenant_active = tenant_total - tenant_invited - tenant_inactive

        # --- Users (point-in-time; total, pending, by normalized role) ---
        urows = s.query(AppUser.role, AppUser.status, AppUser.tenant_id).all()
        users_total = len(urows)
        pending_invites = sum(1 for _r, st, _t in urows if (st or "") in ("invited", "pending"))
        by_role = {"tenant_user": 0, "tenant_admin": 0, "kavachio_admin": 0}
        users_by_tenant: dict = {}
        for r, _st, tid in urows:
            by_role[_norm_role(r)] += 1
            users_by_tenant[tid] = users_by_tenant.get(tid, 0) + 1

        # --- Setups & programs (point-in-time) ---
        # "Active setups" = APPROVED direct-lane bordereau formats (one per
        # program+carrier). "Programs" = programs whose ops-status is 'active'
        # (case-insensitive — the column has mixed 'active'/'Active' values).
        # The two differ because most programs sit in 'draft' and only a subset
        # is live, while a setup is counted the moment its format is approved.
        active_setups = s.query(func.count(DirectFormat.id)).filter(
            DirectFormat.approved == 1).scalar() or 0
        setup_tenants = s.query(func.count(func.distinct(DirectFormat.tenant_id))).filter(
            DirectFormat.approved == 1).scalar() or 0
        setups_by_tenant = dict(s.query(DirectFormat.tenant_id, func.count(DirectFormat.id))
                                .filter(DirectFormat.approved == 1)
                                .group_by(DirectFormat.tenant_id).all())
        programs_active = s.query(func.count(Program.id)).filter(
            Program.is_app_managed.is_(True),
            func.lower(Program.status) == "active").scalar() or 0

        # --- Runs in window vs prior equal window (for the delta) ---
        runs_win = s.query(func.count(OutputExport.id)).filter(
            OutputExport.created_at >= dstart).scalar() or 0
        runs_prev = s.query(func.count(OutputExport.id)).filter(
            OutputExport.created_at >= dprev, OutputExport.created_at < dstart).scalar() or 0
        runs_delta_pct = None
        if runs_prev:
            runs_delta_pct = round((runs_win - runs_prev) * 100.0 / runs_prev)

        # --- Daily series over the window: total + runs-with-exceptions ---
        tot_rows = {str(d): c for d, c in s.query(
            func.date(OutputExport.created_at), func.count(OutputExport.id))
            .filter(OutputExport.created_at >= dstart)
            .group_by(func.date(OutputExport.created_at)).all()}
        exc_rows = {str(d): c for d, c in s.query(
            func.date(OutputExport.created_at), func.count(OutputExport.id))
            .filter(OutputExport.created_at >= dstart, OutputExport.status == "has_exceptions")
            .group_by(func.date(OutputExport.created_at)).all()}
        series = []
        for i in range(days - 1, -1, -1):
            k = str(today - timedelta(days=i))
            series.append({"date": k,
                           "total": int(tot_rows.get(k, 0) or 0),
                           "exceptions": int(exc_rows.get(k, 0) or 0)})

        # --- Exceptions by severity (true window totals) ---
        # Summed from the denormalized per-run counts (written at export time,
        # backfilled by init_db) so the donut covers the WHOLE selected range —
        # not the old recent-25-runs snapshot, which showed identical numbers
        # for 7d and 30d whenever the newest 25 exception runs were recent.
        sev_crit, sev_warn, sev_info = s.query(
            func.coalesce(func.sum(OutputExport.critical_count), 0),
            func.coalesce(func.sum(OutputExport.warning_count), 0),
            func.coalesce(func.sum(OutputExport.info_count), 0),
        ).filter(OutputExport.created_at >= dstart,
                 OutputExport.status == "has_exceptions").one()
        sev = {"critical": int(sev_crit), "warning": int(sev_warn),
               "info": int(sev_info)}

        # --- Runs per tenant (window) + clean rate ---
        total_by_tenant = dict(s.query(OutputExport.tenant_id, func.count(OutputExport.id))
                               .filter(OutputExport.created_at >= dstart)
                               .group_by(OutputExport.tenant_id).all())
        exc_by_tenant = dict(s.query(OutputExport.tenant_id, func.count(OutputExport.id))
                             .filter(OutputExport.created_at >= dstart,
                                     OutputExport.status == "has_exceptions")
                             .group_by(OutputExport.tenant_id).all())
        runs_win_total = sum(int(v or 0) for v in total_by_tenant.values())
        exc_win_total = sum(int(v or 0) for v in exc_by_tenant.values())
        clean_rate = (round((runs_win_total - exc_win_total) * 100.0 / runs_win_total)
                      if runs_win_total else None)

        top_tenants = sorted(
            ([{"name": tname.get(tid, "—"), "code": tcode.get(tid, ""),
               "runs": int(c or 0)} for tid, c in total_by_tenant.items()]),
            key=lambda r: r["runs"], reverse=True)[:6]

        # --- Per-tenant overview table (all tenants, busiest first) ---
        pending_by_tenant = dict(s.query(AppUser.tenant_id, func.count(AppUser.id))
                                 .filter(AppUser.status.in_(("invited", "pending")))
                                 .group_by(AppUser.tenant_id).all())
        table = []
        for t in tenants:
            runs = int(total_by_tenant.get(t.id, 0) or 0)
            exc = int(exc_by_tenant.get(t.id, 0) or 0)
            if not t.is_active:
                status = "inactive"
            elif int(pending_by_tenant.get(t.id, 0) or 0) > 0:
                status = "invited"
            else:
                status = "active"
            table.append({
                "code": t.tenant_name,
                "name": t.legal_name or (t.tenant_name or "").title(),
                "users": int(users_by_tenant.get(t.id, 0) or 0),
                "setups": int(setups_by_tenant.get(t.id, 0) or 0),
                "runs": runs,
                "clean_pct": (round((runs - exc) * 100.0 / runs) if runs else None),
                "status": status,
            })
        table.sort(key=lambda r: (r["runs"], r["users"]), reverse=True)

        # --- Data-model mapping queue (Kavachio ops workload) ---
        q_open = s.query(func.count(AdminMappingTask.id)).filter(
            AdminMappingTask.status == "open").scalar() or 0
        q_prog = s.query(func.count(AdminMappingTask.id)).filter(
            AdminMappingTask.status == "in_progress").scalar() or 0
        q_done = s.query(func.count(AdminMappingTask.id)).filter(
            AdminMappingTask.status == "done",
            AdminMappingTask.resolved_at.isnot(None),
            AdminMappingTask.resolved_at >= dstart).scalar() or 0
        q_dismissed = s.query(func.count(AdminMappingTask.id)).filter(
            AdminMappingTask.status == "dismissed").scalar() or 0
        done_rows = s.query(AdminMappingTask.created_at, AdminMappingTask.resolved_at).filter(
            AdminMappingTask.status == "done",
            AdminMappingTask.resolved_at.isnot(None),
            AdminMappingTask.resolved_at >= dstart).all()
        hrs = [(r.resolved_at - r.created_at).total_seconds() / 3600.0
               for r in done_rows if r.created_at and r.resolved_at]
        avg_turnaround = round(sum(hrs) / len(hrs), 1) if hrs else None

        return {
            "range": window, "range_days": days,
            "tenants": {"total": tenant_total, "active": tenant_active,
                        "invited": int(tenant_invited),
                        "inactive": int(tenant_inactive)},
            "users": {"total": users_total, "pending_invites": pending_invites,
                      "by_role": by_role},
            "setups": {"active": int(active_setups), "tenants": int(setup_tenants)},
            "programs_active": int(programs_active),
            "runs": {"window_count": int(runs_win), "prev_count": int(runs_prev),
                     "delta_pct": runs_delta_pct, "clean_rate": clean_rate},
            "runs_series": series,
            "exceptions_by_severity": sev,
            "top_tenants": top_tenants,
            "tenants_table": table,
            "mapping_queue": {"open": int(q_open), "in_progress": int(q_prog),
                              "resolved_window": int(q_done), "dismissed": int(q_dismissed),
                              "avg_turnaround_hours": avg_turnaround},
        }


@router.get("/dashboard/platform/activity")
def platform_activity(limit: int = 12,
                      _p: Principal = Depends(require_role("kavachio_admin"))):
    """Cross-tenant recent-activity feed for the platform dashboard."""
    with SessionLocal() as s:
        names = {t.id: (t.legal_name or (t.tenant_name or "").title())
                 for t in s.query(Tenant).all()}
        rows = (s.query(ActivityEvent)
                .order_by(desc(ActivityEvent.created_at))
                .limit(max(1, min(limit, 50))).all())
        return [{"id": e.id, "tenant": names.get(e.tenant_id, "—"),
                 "actor": e.actor, "action": e.action, "target": e.target,
                 "details": e.details, "created_at": _iso_ts(e.created_at)}
                for e in rows]


# ---- S-22 User Management ------------------------------------------------

class UserBody(BaseModel):
    email: str
    full_name: str
    role: Optional[str] = "ops"
    status: Optional[str] = "active"
    password: Optional[str] = None
    # Required when role is broker_admin, ignored otherwise. A broker seat
    # belongs to a BROKER, not to the carrier doing the inviting — the same
    # broker produces for several carriers, so it cannot be pinned to one.
    broker_party_id: Optional[int] = None


def _user_dict(u: AppUser, mga: Optional[str] = None) -> dict:
    from auth_deps import normalize_role
    return {"id": u.id, "mga": mga, "email": u.email,
            "full_name": u.full_name, "role": normalize_role(u.role), "status": u.status,
            "tenant_id": u.tenant_id,
            "last_login_at": _iso_utc(u.last_login_at)}


@router.get("/users")
def users_list(
    mga: str,
    q: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    page: Optional[int] = Query(None, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=200),
    principal: Principal = Depends(current_principal),
):
    from auth_deps import _ROLE_ALIASES
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)

        # Brokers are invited from this screen too, so their people belong on
        # it. Which brokers count is not "who created the party" — it is every
        # broker on one of this carrier's programmes, plus any this carrier
        # added to its own directory. Same rule the Brokers directory used.
        broker_ids = {r[0] for r in s.query(ProgramBroker.broker_party_id)
                      .filter(ProgramBroker.tenant_id == tid).all()}
        broker_ids |= {r[0] for r in s.query(Party.id).filter(
            Party.tenant_id == tid,
            func.cast(Party.party_type, String).in_(PRODUCER_PARTY_TYPES)).all()}

        # kavachio_admin is a cross-tenant platform role, not a member of this
        # tenant's org — never surfaced on a tenant's own Users screen.
        base = s.query(AppUser).filter(
            or_(AppUser.tenant_id == tid,
                AppUser.broker_party_id.in_(broker_ids) if broker_ids else False),
            AppUser.role != "kavachio_admin")

        # Tenant-WIDE admin count, independent of filters/paging — the "can't
        # remove the last admin" rule needs the true total, not just whatever
        # happens to be on the current page.
        admin_raw = [raw for raw, norm in _ROLE_ALIASES.items() if norm == "carrier_admin"]
        total_admins = base.filter(AppUser.role.in_(admin_raw)).count()

        query = base
        if q and q.strip():
            ql = f"%{q.strip().lower()}%"
            query = query.filter(or_(
                func.lower(AppUser.full_name).like(ql),
                func.lower(AppUser.email).like(ql)))
        if role:
            # `role` arrives normalized ("tenant_admin"/"tenant_user"); the
            # column stores legacy raw values too ("admin"/"ops"/...), so match
            # every raw value that normalizes to the requested one.
            raw_values = [raw for raw, norm in _ROLE_ALIASES.items() if norm == role]
            query = query.filter(AppUser.role.in_(raw_values or [role]))
        if status == "invited":
            query = query.filter(AppUser.status.in_(("invited", "pending")))
        elif status == "active":
            query = query.filter(AppUser.status == "active")
        elif status == "inactive":
            query = query.filter(AppUser.status.notin_(("active", "invited", "pending")))

        total = query.order_by(None).count()
        ordered = query.order_by(AppUser.email)
        # Pagination is opt-in (page omitted → every match, unpaginated) so any
        # caller besides Users.tsx/TenantDetail.tsx keeps working unchanged.
        if page is not None:
            size = page_size or 10
            ordered = ordered.offset((page - 1) * size).limit(size)
        rows = ordered.all()
        # "Carrier Admin" and "Broker Admin" both appear on this list now, so a
        # row has to say which organisation it is an admin OF.
        names = {p.id: p.legal_name for p in s.query(Party).filter(
            Party.id.in_({u.broker_party_id for u in rows if u.broker_party_id} or {-1})).all()}
        carrier_name = _tenant_display(s, tid) or mga
        items = []
        for u in rows:
            d = _user_dict(u, mga)
            d["broker_party_id"] = u.broker_party_id
            d["org_name"] = names.get(u.broker_party_id, "—") if u.broker_party_id else carrier_name
            d["org_kind"] = "broker" if u.broker_party_id else "carrier"
            items.append(d)
        return {"items": items, "total": total, "total_admins": int(total_admins),
                "page": page, "page_size": page_size}


@router.get("/admin/users")
def admin_users_list(
    q: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    page: Optional[int] = Query(None, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=200),
    _p: Principal = Depends(require_role("kavachio_admin")),
):
    """Every login on the platform, whoever they work for.

    Kavachio creates exactly ONE of these — a carrier's first admin. That admin
    invites their own colleagues and their brokers, and each broker adds its own
    operators. This list exists so that when someone calls, the platform can say
    who they are and whether they can get in. It is read-only on purpose: none
    of these accounts is Kavachio's to change."""
    from auth_deps import _ROLE_ALIASES, normalize_role
    with SessionLocal() as s:
        # A user belongs to a carrier (tenant_id) or to a broker
        # (broker_party_id), never both — the chk_app_user_scope constraint
        # enforces it — so the two outer joins can never double a row.
        query = (s.query(AppUser, Tenant.legal_name, Tenant.tenant_name, Party.legal_name)
                 .outerjoin(Tenant, Tenant.id == AppUser.tenant_id)
                 .outerjoin(Party, Party.id == AppUser.broker_party_id))
        if q and q.strip():
            ql = f"%{q.strip().lower()}%"
            query = query.filter(or_(
                func.lower(AppUser.full_name).like(ql),
                func.lower(AppUser.email).like(ql)))
        if role:
            raw = [r for r, n in _ROLE_ALIASES.items() if n == role]
            query = query.filter(AppUser.role.in_(raw or [role]))
        if status == "invited":
            query = query.filter(AppUser.status.in_(("invited", "pending")))
        elif status == "active":
            query = query.filter(AppUser.status == "active")
        elif status == "inactive":
            query = query.filter(AppUser.status.notin_(("active", "invited", "pending")))

        total = query.order_by(None).count()
        ordered = query.order_by(AppUser.email)
        if page is not None:
            size = page_size or 10
            ordered = ordered.offset((page - 1) * size).limit(size)

        items = []
        for u, t_legal, t_name, b_legal in ordered.all():
            r = normalize_role(u.role)
            if r == "kavachio_admin":
                org, kind = "Kavachio", "kavachio"
            elif b_legal:
                org, kind = b_legal, "broker"
            else:
                org, kind = (t_legal or t_name or "—"), "carrier"
            items.append({
                "id": u.id, "full_name": u.full_name, "email": u.email,
                "role": r, "status": u.status,
                "org_name": org, "org_kind": kind,
                "tenant_name": t_name, "broker_party_id": u.broker_party_id,
                "created_at": _iso_utc(u.created_at),
                "last_login_at": _iso_utc(u.last_login_at),
            })

        # Headline counts, over EVERY user rather than the current page.
        def _n(*roles):
            raw = [r for r, n in _ROLE_ALIASES.items() if n in roles]
            return s.query(func.count(AppUser.id)).filter(
                AppUser.role.in_(raw or list(roles))).scalar() or 0
        counts = {
            "total":          s.query(func.count(AppUser.id)).scalar() or 0,
            "kavachio":       _n("kavachio_admin"),
            "carrier_users":  _n("carrier_admin"),
            "broker_users":   _n("broker_admin", "operator"),
            "operators":      _n("operator"),
            "never_signed_in": s.query(func.count(AppUser.id)).filter(
                AppUser.last_login_at.is_(None)).scalar() or 0,
            # How many ORGANISATIONS those people are spread across — "4 carrier
            # users" reads very differently across one carrier than across four.
            "carriers": s.query(func.count(func.distinct(AppUser.tenant_id))).filter(
                AppUser.tenant_id.isnot(None)).scalar() or 0,
            "brokers": s.query(func.count(func.distinct(AppUser.broker_party_id))).filter(
                AppUser.broker_party_id.isnot(None)).scalar() or 0,
        }
        return {"items": items, "total": int(total), "counts": counts,
                "page": page, "page_size": page_size}


def _assert_manages_user(s, principal: Principal, u: AppUser) -> None:
    """Guard the by-id user routes now that BROKER people appear on the
    carrier's Users screen too.

    A carrier owns its own staff outright. It also created each broker's FIRST
    admin, so it may resend or withdraw that. An OPERATOR is not its to touch —
    that seat belongs to the broker, and only the broker's own admin manages it.

    404 rather than 403 for anything outside the carrier's reach: an id must
    not reveal whether it exists somewhere else on the platform.
    """
    if principal.is_platform_admin:
        return
    if u.tenant_id is not None:
        if u.tenant_id != principal.tenant_id:
            raise HTTPException(404, "user not found")
        return
    if not u.broker_party_id:
        raise HTTPException(404, "user not found")
    reachable = (s.query(ProgramBroker)
                  .filter(ProgramBroker.broker_party_id == u.broker_party_id,
                          ProgramBroker.tenant_id == principal.tenant_id).first())
    if not reachable:
        party = s.query(Party).filter(Party.id == u.broker_party_id).first()
        if not party or party.tenant_id != principal.tenant_id:
            raise HTTPException(404, "user not found")
    if u.role == "operator":
        raise HTTPException(
            403, "Operators belong to the broker. Their own admin adds and removes them.")


@router.post("/users")
def users_create(mga: str, body: UserBody,
                 principal: Principal = Depends(require_role("tenant_admin"))):
    """Invite someone: a colleague at this carrier, or a broker's first admin.

    Three things the database insists on, which this endpoint used to get wrong
    and fail with a 500 rather than a message:

      * the role must be one of the four — a stored 'admin'/'ops' fails
        chk_app_user_role, so the legacy value is normalized first;
      * every login must record who invited it (trg_enforce_invitation_chain),
        so invited_by_user_id is the signed-in admin;
      * a broker seat belongs to a broker and to NO carrier
        (chk_app_user_scope), so tenant_id and broker_party_id swap over.

    Operators are deliberately refused. They belong to the broker, one level
    down — their own admin creates them.
    """
    from auth_utils import hash_password
    from auth_deps import normalize_role

    role = normalize_role(body.role)
    if role == "operator":
        raise HTTPException(
            400, "Operators are added by the broker's own admin, not by you. "
                 "Invite their Broker Admin and they take it from there.")
    if role == "kavachio_admin":
        raise HTTPException(403, "Kavachio staff accounts are not created here.")

    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        if s.query(AppUser).filter(AppUser.email == body.email.strip().lower()).first():
            raise HTTPException(409, "Email already exists")

        # Where this person lands. Exactly one of the two is ever set.
        broker_id = None
        if role == "broker_admin":
            if not body.broker_party_id:
                raise HTTPException(400, "Choose which broker this admin belongs to.")
            # Must be a broker this carrier actually holds — otherwise a carrier
            # could seat an admin at someone else's broker.
            party = s.query(Party).filter(Party.id == body.broker_party_id).first()
            # 404, not 403, on anything that is not this carrier's broker — a
            # wrong id must not reveal whether it exists somewhere else.
            if not party or str(party.party_type) not in PRODUCER_PARTY_TYPES:
                raise HTTPException(404, "broker not found")
            on_a_programme = (s.query(ProgramBroker)
                               .filter(ProgramBroker.broker_party_id == party.id,
                                       ProgramBroker.tenant_id == tid).first())
            if not on_a_programme and party.tenant_id != tid:
                raise HTTPException(404, "broker not found")
            broker_id, tid = party.id, None

        # No password supplied → this is an INVITE: create the user as "invited"
        # and email a tokened set-password link (same page as password reset).
        invited = not body.password
        hashed = hash_password(body.password) if body.password else None
        u = AppUser(email=body.email.strip().lower(),
                    full_name=body.full_name, role=role,
                    status="invited" if invited else (body.status or "active"),
                    password=hashed,
                    tenant_id=tid, broker_party_id=broker_id,
                    # Who let this person in. Without it the database refuses
                    # the row outright.
                    invited_by_user_id=principal.user_id)
        s.add(u)
        link = _make_invite_link(u) if invited else None
        s.commit(); s.refresh(u)
        org = _tenant_display(s, u.tenant_id) if u.tenant_id else (
            s.query(Party).filter(Party.id == broker_id).first().legal_name if broker_id else None)
        if invited and link:
            _send_invite_email(u.email, link, u.full_name, org)
        _log(mga, _actor(principal), "user_invited" if invited else "user_created", target=str(u.id),
             details={"email": u.email, "full_name": u.full_name, "role": u.role,
                      "status": u.status, "broker_party_id": broker_id})
        return _user_dict(u, mga)


@router.post("/users/{user_id}/resend-invite")
def users_resend_invite(user_id: int,
                        principal: Principal = Depends(require_role("tenant_admin"))):
    """Re-issue an invite: fresh token + set-password email. Tenant-admin only."""
    with SessionLocal() as s:
        u = s.get(AppUser, user_id)
        if not u:
            raise HTTPException(404, "user not found")
        _assert_manages_user(s, principal, u)
        if u.status not in ("invited", "pending"):
            raise HTTPException(409, "this user has already accepted their invite")
        link = _make_invite_link(u)
        s.commit()
        _send_invite_email(u.email, link, u.full_name, _tenant_display(s, u.tenant_id))
        _log(_tenant_name(s, u.tenant_id), _actor(principal), "invite_resent", target=str(u.id),
             details={"email": u.email})
        return {"ok": True}


@router.put("/users/{user_id}")
def users_update(user_id: int, body: UserBody,
                 principal: Principal = Depends(require_role("tenant_admin"))):
    from auth_utils import hash_password
    with SessionLocal() as s:
        u = s.get(AppUser, user_id)
        if not u:
            raise HTTPException(404, "user not found")
        _assert_manages_user(s, principal, u)
        u.full_name = body.full_name
        if body.role:
            # Same reason as on create: the column only accepts the four names,
            # so a legacy 'admin'/'ops' from an older client is mapped, not stored.
            from auth_deps import normalize_role
            u.role = normalize_role(body.role)
        if body.status:
            u.status = body.status
        if body.password:
            u.password = hash_password(body.password)
        s.commit(); s.refresh(u)
        try:
            from audit import log_activity, actor_email
            log_activity(u.tenant_id, actor_email(principal.user_id), "user_updated",
                         target=str(user_id),
                         details={
                             "email": u.email,
                             "role": u.role,
                             "status": u.status,
                             "changed": sorted(body.model_dump(exclude_unset=True).keys()),
                         })
        except Exception:  # noqa: BLE001
            pass
        return _user_dict(u, _tenant_name(s, u.tenant_id))


@router.delete("/users/{user_id}")
def users_delete(user_id: int,
                 principal: Principal = Depends(require_role("tenant_admin"))):
    from auth_deps import normalize_role, db_role_values
    with SessionLocal() as s:
        u = s.get(AppUser, user_id)
        if not u:
            raise HTTPException(404, "user not found")
        _assert_manages_user(s, principal, u)

        # Guards the UI already shows, enforced here too — the screen is not
        # the authority, and a bookmarked request bypasses it entirely.
        if u.id == principal.user_id:
            raise HTTPException(409, "You cannot remove your own account.")
        if normalize_role(u.role) == "carrier_admin" and u.tenant_id:
            others = (s.query(func.count(AppUser.id))
                       .filter(AppUser.tenant_id == u.tenant_id,
                               AppUser.role.in_(db_role_values("carrier_admin")),
                               AppUser.id != u.id).scalar() or 0)
            if others == 0:
                raise HTTPException(
                    409, "This is the only admin — add another before removing this one.")

        _u_tenant_id, _u_email, _u_role = u.tenant_id, u.email, u.role

        # Someone who has invited people, approved a contract or assigned a
        # broker is referenced from other rows. Deleting them would either fail
        # or erase the answer to "who did this?" — so they are SUSPENDED
        # instead. Access goes immediately either way; the record survives.
        # Same principle as taking a broker off a programme that has contracts.
        invited = (s.query(func.count(AppUser.id))
                    .filter(AppUser.invited_by_user_id == u.id).scalar() or 0)
        suspended = False
        if invited:
            u.status = "suspended"; suspended = True; s.commit()
        else:
            try:
                s.delete(u); s.commit()
            except IntegrityError:
                # Referenced from somewhere else (a contract they submitted, a
                # broker they assigned). Same answer, without enumerating every
                # foreign key that might ever point here.
                s.rollback()
                u = s.get(AppUser, user_id)
                u.status = "suspended"; suspended = True; s.commit()
        try:
            from audit import log_activity, actor_email
            log_activity(_u_tenant_id, actor_email(principal.user_id), "user_deleted",
                         target=f"user:{user_id}",
                         details={
                             "email": _u_email,
                             "role": _u_role,
                         })
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "suspended": suspended,
                "message": (f"{_u_email} can no longer sign in. Their account is kept "
                            "because other records name them.") if suspended else None}


class ProfileBody(BaseModel):
    full_name: str


@router.put("/users/{user_id}/profile")
def users_update_profile(user_id: int, body: ProfileBody):
    """Self-service profile update for the signed-in user. Deliberately narrow:
    it updates only the display name — never role/status/email — so a user
    editing their own profile can't change their access (unlike the admin-only
    PUT /users/{user_id})."""
    with SessionLocal() as s:
        u = s.get(AppUser, user_id)
        if not u:
            raise HTTPException(404, "user not found")
        name = (body.full_name or "").strip()
        if not name:
            raise HTTPException(400, "Full name is required.")
        u.full_name = name
        s.commit(); s.refresh(u)
        mga = _tenant_name(s, u.tenant_id)
        _log(mga, u.email, "profile_updated", target=str(u.id),
             details={"full_name": u.full_name})
        return _user_dict(u, mga)


# =====================================================================
# RULE LIBRARY — managed BDX validation rules (S-generic)
# ---------------------------------------------------------------------
# Two scopes on one table (generic_rule_specification), keyed on tenant_id:
#   • GLOBAL rule  (tenant_id IS NULL) — managed by kavachio_admin, applied to
#     every tenant's uploads.
#   • TENANT rule  (tenant_id = <id>)  — managed by that tenant's tenant_admin,
#     applied only to that tenant's uploads, invisible to other tenants.
#
# The owning scope is decided SERVER-SIDE from the caller's token, never from
# request input: a kavachio_admin always writes globals, a tenant_admin always
# writes their own tenant's rules. Cross-scope reads/writes are 404 (IDOR-safe).
# =====================================================================

from contract_upload_services.generic_rule_library import (
    supported_classes as _rule_supported_classes,
    is_supported_class as _rule_is_supported_class,
    label_for_class as _rule_label_for_class,
    SUPPORTED_SEVERITIES as _RULE_SEVERITIES,
)


def _rule_dict(r: GenericRuleSpecification) -> dict:
    return {
        "id": r.id,
        "rule_name": r.rule_name,
        "class_name": r.class_name,
        "class_label": _rule_label_for_class(r.class_name),
        "severity": r.severity,
        "validation_logic": r.validation_logic,
        "is_active": bool(r.is_active),
        "scope": "global" if r.tenant_id is None else "tenant",
        "tenant_id": r.tenant_id,
        "created_at": _iso_utc(r.created_at),
        "updated_at": _iso_utc(r.updated_at),
    }


def _rule_manageable_or_404(principal: Principal, r: GenericRuleSpecification) -> None:
    """A rule is manageable only within the caller's own scope: kavachio_admin
    manages GLOBAL rules (tenant_id IS NULL); a tenant_admin manages only their
    OWN tenant's rules. Anything else is 404 so ids can't be probed across
    scopes."""
    if principal.is_platform_admin:
        if r.tenant_id is not None:
            raise HTTPException(404, "not found")
    else:
        if r.tenant_id != principal.tenant_id:
            raise HTTPException(404, "not found")


class RuleBody(BaseModel):
    rule_name: str
    class_name: str
    severity: str = "Major"
    validation_logic: Optional[str] = None
    is_active: bool = True


def _validate_rule_body(body: RuleBody) -> tuple[str, str, str, Optional[str]]:
    name = (body.rule_name or "").strip()
    if not name:
        raise HTTPException(400, "Rule name is required.")
    if not _rule_is_supported_class(body.class_name):
        raise HTTPException(400, f"Unsupported rule type '{body.class_name}'.")
    severity = (body.severity or "Major").strip().title()
    if severity not in _RULE_SEVERITIES:
        raise HTTPException(400, f"Severity must be one of {_RULE_SEVERITIES}.")
    logic = (body.validation_logic or "").strip() or None
    return name, body.class_name, severity, logic


@router.get("/rule-library/classes")
def rule_library_classes(principal: Principal = Depends(require_role("tenant_admin"))):
    """The rule-type catalogue driving the create/edit dropdown. A rule only
    runs if its class_name is one of these, so the form offers exactly these."""
    return {"classes": _rule_supported_classes(), "severities": _RULE_SEVERITIES}


@router.get("/rule-library")
def rule_library_list(principal: Principal = Depends(require_role("tenant_admin"))):
    """Rules in the caller's scope. kavachio_admin sees the platform's GLOBAL
    rules; a tenant_admin sees only their own tenant's rules (globals are hidden
    from the tenant screen). Includes disabled rules so they can be re-enabled."""
    with SessionLocal() as s:
        q = s.query(GenericRuleSpecification)
        if principal.is_platform_admin:
            q = q.filter(GenericRuleSpecification.tenant_id.is_(None))
        else:
            q = q.filter(GenericRuleSpecification.tenant_id == principal.tenant_id)
        rows = q.order_by(GenericRuleSpecification.id.desc()).all()
        return {"items": [_rule_dict(r) for r in rows], "total": len(rows)}


@router.post("/rule-library")
def rule_library_create(body: RuleBody,
                        principal: Principal = Depends(require_role("tenant_admin"))):
    """Create a rule. Scope is forced from the token: kavachio_admin → global
    (tenant_id NULL); tenant_admin → their own tenant."""
    name, class_name, severity, logic = _validate_rule_body(body)
    owner_tenant = None if principal.is_platform_admin else principal.tenant_id
    if not principal.is_platform_admin and owner_tenant is None:
        raise HTTPException(403, "no tenant bound to this user")
    with SessionLocal() as s:
        r = GenericRuleSpecification(
            rule_name=name, class_name=class_name, severity=severity,
            validation_logic=logic, is_generic=True, is_active=bool(body.is_active),
            tenant_id=owner_tenant, created_by=principal.user_id,
        )
        s.add(r); s.commit(); s.refresh(r)
        mga = _tenant_name(s, owner_tenant)
        _log(mga, _actor(principal), "rule_created", target=str(r.id),
             details={"rule_name": name, "class_name": class_name, "scope": _rule_dict(r)["scope"]})
        return _rule_dict(r)


@router.put("/rule-library/{rule_id}")
def rule_library_update(rule_id: int, body: RuleBody,
                        principal: Principal = Depends(require_role("tenant_admin"))):
    """Edit a rule in the caller's own scope. Scope/tenant is immutable."""
    name, class_name, severity, logic = _validate_rule_body(body)
    with SessionLocal() as s:
        r = s.get(GenericRuleSpecification, rule_id)
        if not r:
            raise HTTPException(404, "not found")
        _rule_manageable_or_404(principal, r)
        r.rule_name = name; r.class_name = class_name; r.severity = severity
        r.validation_logic = logic; r.is_active = bool(body.is_active)
        s.commit(); s.refresh(r)
        mga = _tenant_name(s, r.tenant_id)
        _log(mga, _actor(principal), "rule_updated", target=str(r.id),
             details={"rule_name": name, "is_active": r.is_active})
        return _rule_dict(r)


class RuleToggleBody(BaseModel):
    is_active: bool


@router.patch("/rule-library/{rule_id}")
def rule_library_toggle(rule_id: int, body: RuleToggleBody,
                        principal: Principal = Depends(require_role("tenant_admin"))):
    """Enable/disable a rule (soft on/off) without editing its content."""
    with SessionLocal() as s:
        r = s.get(GenericRuleSpecification, rule_id)
        if not r:
            raise HTTPException(404, "not found")
        _rule_manageable_or_404(principal, r)
        r.is_active = bool(body.is_active)
        s.commit(); s.refresh(r)
        mga = _tenant_name(s, r.tenant_id)
        _log(mga, _actor(principal),
             "rule_enabled" if r.is_active else "rule_disabled", target=str(r.id),
             details={"rule_name": r.rule_name})
        return _rule_dict(r)


@router.delete("/rule-library/{rule_id}")
def rule_library_delete(rule_id: int,
                        principal: Principal = Depends(require_role("tenant_admin"))):
    """Delete a rule in the caller's own scope."""
    with SessionLocal() as s:
        r = s.get(GenericRuleSpecification, rule_id)
        if not r:
            raise HTTPException(404, "not found")
        _rule_manageable_or_404(principal, r)
        tid, name = r.tenant_id, r.rule_name
        s.delete(r); s.commit()
        mga = _tenant_name(s, tid)
        _log(mga, _actor(principal), "rule_deleted", target=str(rule_id),
             details={"rule_name": name})
        return {"ok": True, "id": rule_id}
