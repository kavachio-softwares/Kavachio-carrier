"""Feature 10.2 — /v1/*, the machine-to-machine way in.

ONE endpoint takes the file. Carrier, programme and broker come from the API
key, so a broker's overnight job is a single line:

    curl -X POST https://api.kavachio.app/v1/bordereaux \
         -H "X-API-Key: kv_live_…" -F file=@Halstead_CA_Jul26.xlsx

This module is deliberately thin. It checks the key, stores the bytes and hands
the file to `intake_service.land_file` — the SAME function the SFTP poller
calls, running the SAME six checks and writing the SAME `file_arrival` row. That
is what makes "all routes feed one pipeline" true rather than aspirational: a
file that arrives by machine and one that arrives by SFTP are indistinguishable
by the time anyone looks at them.
"""
from __future__ import annotations

import logging
from typing import Optional

import storage
from fastapi import (
    APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile,
)
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

import intake_service as svc
from db import Contract, Party, Program, ProgramBroker, SessionLocal, Tenant
from intake_auth import IntakePrincipal, current_intake_principal, mask
from intake_models import FileArrival, IntakeCredential, IntakeRoute

log = logging.getLogger("kavachio.intake.api")
router = APIRouter(prefix="/v1", tags=["intake-api"])

MAX_BYTES = 200 * 1024 * 1024      # matches the 200 MB the design promises

# Internal outcomes are accepted | held | turned_away. Partners see plain words,
# so adding an internal state later breaks nobody's cron job.
_EXTERNAL = {"accepted": "accepted", "held": "held", "turned_away": "rejected"}


def _err(http: int, code: str, message: str, **extra):
    body = {"error": code, "message": message}
    body.update(extra)
    return HTTPException(http, body)


async def _read_capped(upload: UploadFile, cap: int) -> bytes:
    """Read in 1 MB chunks so an oversized file is refused AT the cap. A plain
    `await upload.read()` pulls the whole body into memory first, so a 2 GB POST
    is an OOM before any size check runs. nginx catches most of those, but this
    must not depend on the gateway."""
    chunks, total = [], 0
    while True:
        chunk = await upload.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise _err(413, "file_too_large",
                       f"Files must be {cap >> 20} MB or smaller.")
        chunks.append(chunk)
    return b"".join(chunks)


def _programme_ref(prog: Program) -> str:
    """The carrier's own code where there is one, else a slug of the name.
    Surrogate ids are deliberately not exposed: they leak row counts, invite
    enumeration, and become an unbreakable public contract the first time a
    partner hard-codes one."""
    ref = getattr(prog, "program_ref", None)
    if ref:
        return str(ref)
    return "-".join((prog.name or f"programme-{prog.id}").lower().split())


def _broker_programmes(s, tenant_id: int, broker_party_id) -> list[Program]:
    if not broker_party_id:
        return []
    return (s.query(Program)
            .join(ProgramBroker, ProgramBroker.program_id == Program.id)
            .filter(ProgramBroker.broker_party_id == broker_party_id,
                    ProgramBroker.status == "active",
                    Program.tenant_id == tenant_id)
            .all())


def resolve_programme(s, p: IntakePrincipal, declared_ref: Optional[str]):
    """Carrier and broker are already fixed by the key. Only the programme can
    need resolving, and a declared one is VERIFIED, never trusted.

      route pinned            → use it; a disagreeing declaration is a 409
      broker-wide + declared  → use the declared ref
      broker-wide, one option → infer it
      broker-wide, several    → 400 that LISTS the valid refs, so a partner can
                                fix their own cron file at 02:00
    """
    options = _broker_programmes(s, p.tenant_id, p.broker_party_id)
    by_ref = {_programme_ref(x): x.id for x in options}

    declared_id = None
    if declared_ref:
        declared_id = by_ref.get(declared_ref.strip().lower())
        if declared_id is None:
            raise _err(400, "unknown_programme",
                       f"There is no programme called '{declared_ref}' for this "
                       "sender.", choices=sorted(by_ref))

    if p.program_id is not None:
        # Declared-and-verified: catches a wrongly wired cron job on night one,
        # instead of after three months of misfiled bordereaux.
        if declared_id is not None and declared_id != p.program_id:
            raise _err(409, "programme_mismatch",
                       "The programme you named is not the one this key is for. "
                       "Check which key that job is using.")
        return p.program_id
    if declared_id is not None:
        return declared_id
    if len(options) == 1:
        return options[0].id
    raise _err(400, "programme_required",
               "This key covers more than one programme, so each file has to say "
               "which one it is for. Send program_ref with the file.",
               choices=sorted(by_ref))


def _receipt(s, arrival: FileArrival, base: str, replayed: bool = False) -> dict:
    """Carrier, broker and programme come back as NAMES on purpose: if someone
    pastes the wrong key into the wrong script they see the wrong broker in the
    reply on the very first night, instead of you finding out three months later
    that files were filed against the wrong programme."""
    route = s.get(IntakeRoute, arrival.route_id) if arrival.route_id else None
    tenant = s.get(Tenant, arrival.tenant_id)
    broker = (s.get(Party, arrival.matched_broker_party_id)
              if arrival.matched_broker_party_id else None)
    prog = (s.get(Program, route.program_id)
            if route and getattr(route, "program_id", None) else None)
    out = {
        "reference": arrival.public_ref,
        "status": _EXTERNAL.get(arrival.outcome, arrival.outcome),
        "received_at": arrival.received_at.isoformat() if arrival.received_at else None,
        "file": {"name": arrival.filename, "bytes": arrival.file_size_bytes,
                 "sha256": arrival.file_hash_sha256},
        "carrier": (tenant.legal_name or tenant.tenant_name) if tenant else None,
        "broker": broker.legal_name if broker else None,
        "programme": prog.name if prog else None,
        "status_url": f"{base}/v1/bordereaux/{arrival.public_ref}",
    }
    if arrival.turned_away_reason:
        out["message"] = arrival.turned_away_reason
    if replayed:
        out["replayed"] = True
    return out


def _base_url(request: Request) -> str:
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("host", "")
    return f"{proto}://{host}" if host else ""


@router.post("/bordereaux", status_code=202)
async def receive_bordereau(
    request: Request,
    file: UploadFile = File(...),
    # Optional and VERIFIED against the key, never trusted — it exists to
    # disagree. There is deliberately no broker or carrier field: a
    # client-supplied broker would let anyone with any key file as anyone else.
    program_ref: Optional[str] = Form(default=None),
    period: Optional[str] = Form(default=None),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    p: IntakePrincipal = Depends(current_intake_principal),
):
    data = await _read_capped(file, MAX_BYTES)
    fname = file.filename or "bordereau"
    if not data:
        raise _err(400, "empty_body", "No file was attached to the request.")

    base = _base_url(request)

    with SessionLocal() as s:
        # A retry after a timeout is the NORMAL case for a cron job: hand back
        # the original receipt rather than loading the same month twice.
        if idempotency_key:
            prior = (s.query(FileArrival)
                     .filter(FileArrival.tenant_id == p.tenant_id,
                             FileArrival.idempotency_key == idempotency_key)
                     .first())
            if prior is not None:
                import hashlib
                if prior.file_hash_sha256 != hashlib.sha256(data).hexdigest():
                    raise _err(409, "idempotency_key_reused",
                               "That Idempotency-Key was already used for a "
                               "different file. Generate a new one for each "
                               "submission.", reference=prior.public_ref)
                return JSONResponse(status_code=200,
                                    content=_receipt(s, prior, base, replayed=True))

        route = s.get(IntakeRoute, p.route_id)
        program_id = resolve_programme(s, p, program_ref)
        # Re-checked every request, even for a pinned key: a broker taken off a
        # programme keeps their key until someone revokes it, and the link table
        # is the authority, not the credential.
        if not (s.query(ProgramBroker)
                .filter(ProgramBroker.program_id == program_id,
                        ProgramBroker.broker_party_id == p.broker_party_id,
                        ProgramBroker.status == "active").first()):
            raise _err(403, "broker_not_on_programme",
                       "This sender is not currently set up on that programme. "
                       "Contact the carrier.")

        # Store BEFORE the checks. When a broker rings about the file turned
        # away at 02:00, "we have the bytes and here is which check failed" is a
        # two-minute conversation; "we rejected something" is a two-day one.
        try:
            blob_ref, _ = storage.store_or_keep("intake", p.tenant_id, fname, data)
        except Exception as exc:                                # noqa: BLE001
            # Never accept what we cannot keep: a 202 we cannot honour is worse
            # than an outage, because the sender stops trying.
            log.exception("intake storage failed")
            raise _err(503, "intake_unavailable",
                       "We cannot store files right now. Try again shortly.",
                       retryable=True) from exc

        # The SAME function the SFTP poller calls. Same six checks, same row.
        arrival = svc.land_file(
            s, tenant_id=p.tenant_id, filename=fname, file_bytes=data,
            route=route, claimed_sender=f"api:{p.credential_id}",
            idempotency_key=idempotency_key, blob_ref=blob_ref)
        try:
            s.commit()
        except IntegrityError:
            # Two identical POSTs racing each other — the DB arbitrated, so
            # re-read whichever won and hand back its receipt.
            s.rollback()
            prior = (s.query(FileArrival)
                     .filter(FileArrival.tenant_id == p.tenant_id,
                             FileArrival.idempotency_key == idempotency_key).first())
            if prior is None:
                raise
            return JSONResponse(status_code=200,
                                content=_receipt(s, prior, base, replayed=True))
        s.refresh(arrival)

        body = _receipt(s, arrival, base)
        if arrival.outcome == "turned_away":
            # A refused file still has a row and a reference — nothing is
            # silently dropped, and the sender can quote it back at you.
            raise _err(422, "turned_away", arrival.turned_away_reason or
                       "The file was turned away.", reference=arrival.public_ref,
                       checks=body.get("checks"))
        return body


@router.get("/bordereaux/{reference}")
def get_bordereau(reference: str, request: Request,
                  p: IntakePrincipal = Depends(current_intake_principal)):
    with SessionLocal() as s:
        arrival = (s.query(FileArrival)
                   .filter(FileArrival.public_ref == reference,
                           FileArrival.tenant_id == p.tenant_id,
                           FileArrival.route_id == p.route_id).first())
        if arrival is None:
            raise _err(404, "not_found", "No submission with that reference.")
        out = _receipt(s, arrival, _base_url(request))
        # How many rows were loaded, but never WHAT was flagged: exception
        # detail is the carrier's and goes out through their review flow, not to
        # whoever holds an API key.
        out["result"] = {"upload_id": arrival.bdx_upload_id}
        return out


@router.get("/bordereaux")
def list_bordereaux(request: Request, limit: int = 50,
                    p: IntakePrincipal = Depends(current_intake_principal)):
    limit = max(1, min(limit, 200))
    with SessionLocal() as s:
        rows = (s.query(FileArrival)
                .filter(FileArrival.tenant_id == p.tenant_id,
                        FileArrival.route_id == p.route_id)
                .order_by(FileArrival.id.desc()).limit(limit).all())
        base = _base_url(request)
        return {"count": len(rows),
                "submissions": [_receipt(s, r, base) for r in rows]}


@router.get("/whoami")
def whoami(p: IntakePrincipal = Depends(current_intake_principal)):
    """Confirms a key works and shows what it is scoped to. This is what a
    partner runs first, and what 10.4 credential testing checks."""
    with SessionLocal() as s:
        route = s.get(IntakeRoute, p.route_id)
        cred = s.get(IntakeCredential, p.credential_id)
        tenant = s.get(Tenant, p.tenant_id)
        broker = s.get(Party, p.broker_party_id) if p.broker_party_id else None
        prog = s.get(Program, p.program_id) if p.program_id else None
        options = _broker_programmes(s, p.tenant_id, p.broker_party_id)
        return {
            "carrier": (tenant.legal_name or tenant.tenant_name) if tenant else None,
            "broker": broker.legal_name if broker else None,
            "programme": prog.name if prog else None,
            "programme_pinned": p.program_id is not None,
            # When the key is NOT pinned these are the refs a caller may send.
            "programme_options": sorted(_programme_ref(x) for x in options),
            "channel": route.channel if route else "api",
            "enabled": bool(route.is_enabled) if route else False,
            "key": mask(cred.key_prefix, cred.last4) if cred else None,
            "label": cred.label if cred else None,
            "last_used": cred.last_used_at.isoformat() if cred and cred.last_used_at else None,
        }


@router.get("/health")
def health():
    """No key needed, so a partner's own monitoring can watch us."""
    return {"service": "kavachio-intake", "status": "ok"}
