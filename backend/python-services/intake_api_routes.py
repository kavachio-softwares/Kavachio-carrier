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
    partner hard-codes one. (One definition: intake_service.programme_ref,
    which email and SFTP subjects and file names are matched against too.)"""
    return svc.programme_ref(prog)


def _broker_programmes(s, tenant_id: int, broker_party_id) -> list[Program]:
    return svc.broker_programmes(s, tenant_id, broker_party_id)


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
    # The short code (PRG-7K3QMA) names a programme as well as its ref does.
    by_code = {svc._plain(f): x.id for x in options
               for f in svc._code_forms(svc.programme_code(x))}

    declared_id = None
    if declared_ref:
        declared_id = (by_ref.get(declared_ref.strip().lower())
                       or by_code.get(svc._plain(declared_ref)))
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


def reporting_periods(s, tenant_id: int, program_id: Optional[int],
                      broker_party_id) -> list[str]:
    """The reporting periods a sender may name — the same list the broker's
    Process Bordereau picker offers (intake_service.reporting_periods)."""
    return svc.reporting_periods(s, tenant_id, program_id, broker_party_id)


def resolve_contract(s, p: IntakePrincipal, program_id: int,
                     declared_ref: Optional[str]) -> Optional[int]:
    """Which of the programme's contracts the file is written under — the
    question Process Bordereau asks a person.

      one contract            → that one; a different declared one is a 400
      several + declared      → the declared one, if it is one of them
      several, none declared  → 400 that LISTS the valid refs
      none                    → None: the file is held for the carrier until a
                                contract is agreed, as it always has been
    """
    options = svc.live_contracts(s, p.tenant_id, program_id, p.broker_party_id)
    by_ref = {svc.contract_ref(c): c.id for c in options}
    if declared_ref:
        want = svc._plain(declared_ref)
        hit = [c.id for c in options
               if want in (svc._plain(svc.contract_ref(c)), svc._plain(c.name),
                           svc._plain(svc.contract_label(c)),
                           *(svc._plain(f) for f in svc._code_forms(svc.contract_code(c))))]
        if len(hit) != 1:
            raise _err(400, "unknown_contract",
                       f"There is no contract called '{declared_ref}' for this "
                       "sender on that programme.", choices=sorted(by_ref))
        return hit[0]
    if len(options) > 1:
        raise _err(400, "contract_required",
                   "This programme has more than one contract for this sender, so "
                   "each file has to say which one it is written under. Send "
                   "contract_ref with the file.", choices=sorted(by_ref))
    return options[0].id if options else None


def _receipt(s, arrival: FileArrival, base: str, replayed: bool = False) -> dict:
    """The reply to one file: did it get in, and what was it filed as. Short
    on purpose — no links, and one reference only (this file's), which every
    endpoint and the `replaces` field also accept.

    Broker and programme come back as NAMES on purpose: if someone pastes the
    wrong key into the wrong script they see the wrong broker in the reply on
    the very first night, instead of you finding out three months later that
    files were filed against the wrong programme."""
    from db import Contract
    route = s.get(IntakeRoute, arrival.route_id) if arrival.route_id else None
    broker = (s.get(Party, arrival.matched_broker_party_id)
              if arrival.matched_broker_party_id else None)
    # The programme the FILE was recorded against — the route's pin, or the one
    # the sender named on a key that covers several.
    prog_id = (getattr(route, "program_id", None) if route else None) or arrival.program_id
    prog = s.get(Program, prog_id) if prog_id else None
    con = (s.query(Contract.id, Contract.name, Contract.filename)
           .filter(Contract.id == arrival.contract_id).first()
           if getattr(arrival, "contract_id", None) else None)
    out = {
        "reference": arrival.public_ref,
        "status": _EXTERNAL.get(arrival.outcome, arrival.outcome),
    }
    if arrival.turned_away_reason:
        out["message"] = arrival.turned_away_reason
    out.update({
        "received_at": arrival.received_at.isoformat() if arrival.received_at else None,
        "file": arrival.filename,
        # What the file was recorded as — the three things Process Bordereau
        # asks a person to pick. A wrong one shows up on the first night.
        "broker": broker.legal_name if broker else None,
        "programme": prog.name if prog else None,
        "contract": svc.contract_label(con) if con else None,
        "period": arrival.reporting_period,
    })
    if replayed:
        out["replayed"] = True
    # Where this programme + contract + period stands as a whole: its current
    # version may not be this file (a held copy, or a later correction).
    try:
        import submission_service as subs
        th, ver = subs.thread_for_arrival(s, arrival.id)
        if th is not None and ver is not None:
            out["version"] = ver.no
            summary = {"current_version": th.current.no, "status": th.status}
            # Only once the newest file has been checked: while it is still
            # processing, the count would be the PREVIOUS version's.
            if th.status not in ("processing", "received"):
                summary["open_exceptions"] = subs.progress(s, th)["remaining"]
            out["period_summary"] = summary
    except Exception:  # noqa: BLE001 — the receipt never fails on it
        log.warning("no submission block for %s", arrival.public_ref, exc_info=True)
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
    # Which of the programme's contracts the file is written under — the
    # Contract that Process Bordereau asks a person to pick. Needed only when
    # the sender holds more than one on the programme; GET /v1/whoami lists
    # them. Verified like program_ref, never trusted.
    contract_ref: Optional[str] = Form(default=None),
    # The reporting period the file is FOR, e.g. "2026-07" — one of the
    # labels GET /v1/whoami lists. REQUIRED: a file that does not say which
    # period it is for was filed under the oldest period still open, which is
    # a guess — and a wrong guess marks the wrong month as delivered. Kept
    # Optional in the signature so a missing one gets this API's own error
    # (period_required, with the valid choices) rather than a bare 422.
    period: Optional[str] = Form(default=None),
    # A correction: the reference of the file it replaces (or of its
    # submission). Rarely needed — a file for the same programme, contract and
    # period is matched to it anyway.
    replaces: Optional[str] = Form(default=None),
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
        #
        # A key is the SENDER's, so it is looked up on this key's way in (its
        # route, which belongs to one broker) — the same scope the receipt
        # lookup below uses. Scoped to the carrier alone, two brokers who both
        # name their keys after the period ("bdx-2026-07") would collide: the
        # second one's file refused, or handed the first one's receipt.
        if idempotency_key:
            prior = (s.query(FileArrival)
                     .filter(FileArrival.tenant_id == p.tenant_id,
                             FileArrival.route_id == p.route_id,
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
        contract_id = resolve_contract(s, p, program_id, contract_ref)

        # A stated period must be one the carrier's calendar expects — the same
        # rule Process Bordereau's picker enforces. Refused rather than guessed:
        # a wrong period marks the wrong month as delivered.
        period = (period or "").strip() or None
        allowed = reporting_periods(s, p.tenant_id, program_id, p.broker_party_id)
        if not period:
            raise _err(400, "period_required",
                       "Say which reporting period this file is for: send period "
                       "with the file, e.g. period=" + (allowed[0] if allowed else "2026-09")
                       + ".", choices=allowed)
        if period:
            if allowed and period not in allowed:
                raise _err(400, "unknown_period",
                           f"'{period}' is not a reporting period this programme "
                           "expects from you. Send one of the listed periods.",
                           choices=allowed)

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
        # Inside the try: land_file FLUSHES the row, so the database's
        # idempotency rule fires there, before the commit is ever reached.
        try:
            arrival = svc.land_file(
                s, tenant_id=p.tenant_id, filename=fname, file_bytes=data,
                route=route, claimed_sender=f"api:{p.credential_id}",
                idempotency_key=idempotency_key, blob_ref=blob_ref,
                period=period, replaces=replaces,
                program_id=program_id, contract_id=contract_id)
            s.commit()
        except IntegrityError:
            # Two identical POSTs racing each other — the DB arbitrated, so
            # re-read whichever won and hand back its receipt.
            s.rollback()
            if not idempotency_key:
                raise              # not a key race: nothing to hand back
            prior = (s.query(FileArrival)
                     .filter(FileArrival.tenant_id == p.tenant_id,
                             FileArrival.route_id == p.route_id,
                             FileArrival.idempotency_key == idempotency_key).first())
            if prior is None:
                if (s.query(FileArrival.id)
                        .filter(FileArrival.tenant_id == p.tenant_id,
                                FileArrival.idempotency_key == idempotency_key)
                        .first()):
                    # The key clashed with ANOTHER way in's: this database
                    # still has the per-carrier rule that migration 23
                    # replaces. Refused like a reused key, without naming the
                    # other submission.
                    raise _err(409, "idempotency_key_reused",
                               "That Idempotency-Key is already in use. "
                               "Generate a new one for each submission.")
                raise
            return JSONResponse(status_code=200,
                                content=_receipt(s, prior, base, replayed=True))
        s.refresh(arrival)

        body = _receipt(s, arrival, base)
        # Which period the file was recorded against, so a wrong one shows up
        # on the first night rather than at month-end.
        body["period"] = getattr(arrival, "reporting_period", None) or period
        if arrival.outcome == "turned_away":
            # A refused file still has a row and a reference — nothing is
            # silently dropped, and the sender can quote it back at you.
            raise _err(422, "turned_away", arrival.turned_away_reason or
                       "The file was rejected.", reference=arrival.public_ref,
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
        # The same short reply as the upload; the month's history and its
        # exceptions are at the submission's URL.
        return _receipt(s, arrival, _base_url(request))


def _own_submission(s, reference: str, p: IntakePrincipal):
    """A submission this key's BROKER sent to this key's carrier — whichever
    channel each version came through."""
    import submission_service as subs
    th = subs.load(s, subs.thread_ref(s, reference))
    if th is None or th.tenant_id != p.tenant_id \
            or th.broker_party_id != p.broker_party_id:
        raise _err(404, "not_found", "No submission with that reference.")
    return th


@router.get("/submissions/{reference}")
def get_submission(reference: str, p: IntakePrincipal = Depends(current_intake_principal)):
    """Where a submission stands: status, versions, progress, deadline."""
    import submission_service as subs
    with SessionLocal() as s:
        return subs.status_json(s, _own_submission(s, reference, p))


@router.get("/submissions/{reference}/exceptions")
def get_submission_exceptions(reference: str, format: str = "json",
                              p: IntakePrincipal = Depends(current_intake_principal)):
    """Every exception on the current version, located in the broker's own
    file: sheet, row, column, current value, expected value, what to fix.
    Only the broker answers exceptions, so the broker's own key sees them.
    `format=csv` for a spreadsheet."""
    import submission_service as subs
    with SessionLocal() as s:
        th = _own_submission(s, reference, p)
        doc = subs.status_json(s, th, include_exceptions=True)
        if format == "csv":
            from fastapi.responses import Response
            body = subs.report_csv(th.ref, doc["version"], doc.get("exceptions") or [])
            return Response(body, media_type="text/csv", headers={
                "Content-Disposition":
                    f'attachment; filename="{th.ref}-v{doc["version"]}-exceptions.csv"'})
        return {"reference": th.ref, "version": doc["version"],
                "status": doc["status"], "progress": doc["progress"],
                "exceptions": doc.get("exceptions") or []}


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
        # What a file sent with this key can be for: each programme it may
        # name, the contracts under it and the periods open to it — the three
        # pickers of Process Bordereau, as data.
        shown = [prog] if prog is not None else options
        programmes = [{
            "ref": _programme_ref(x),
            "code": svc.programme_code(x),
            "name": x.name,
            "contracts": [{"ref": svc.contract_ref(c), "code": svc.contract_code(c),
                           "name": c.name or c.filename}
                          for c in svc.live_contracts(s, p.tenant_id, x.id,
                                                      p.broker_party_id)],
            "reporting_periods": reporting_periods(s, p.tenant_id, x.id,
                                                   p.broker_party_id),
        } for x in shown]
        return {
            "carrier": (tenant.legal_name or tenant.tenant_name) if tenant else None,
            "broker": broker.legal_name if broker else None,
            "programme": prog.name if prog else None,
            "programme_pinned": p.program_id is not None,
            "programmes": programmes,
            # When the key is NOT pinned these are the refs a caller may send.
            "programme_options": sorted(_programme_ref(x) for x in options),
            # The values `period` may take on POST /v1/bordereaux.
            "reporting_periods": reporting_periods(
                s, p.tenant_id,
                p.program_id or (options[0].id if len(options) == 1 else None),
                p.broker_party_id),
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
