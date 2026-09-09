"""The carrier-centric API: carrier → programme → broker → contract → policy.

These are the CANONICAL routes. They spell out the workflow the platform
actually implements:

    1. the carrier owns the lifecycle — programmes, brokers, contracts, setup
    2. the broker signs in and sees only what the carrier assigned it
    3. the carrier raises the contract and sends its terms to the broker
    4. the broker reads the terms and either agrees them or asks for changes
    5. both sides sign, and the contract goes in force
    6. the broker submits bordereaux and reads its own validation results
    7. the broker fixes what validation flagged

Every path segment is verified against the one above it by carrier_scope, so
authorization is structural rather than a check each handler has to remember:
a contract is only reachable through the broker that holds it, on the programme
it belongs to, under the carrier that owns the programme.

The flat routes these replace still exist and still work — they are kept as
deprecated aliases so nothing breaks mid-migration (see DEPRECATED_ALIASES in
main.py). New callers should use the paths here.

Handlers are DELEGATED to, never duplicated: the nested route resolves and
checks the chain, then calls the same function the flat route calls. There is
one implementation of every behaviour.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile

import app_routes as _app
import hierarchy_routes as _hier
from auth_deps import Principal, current_principal, require_role
from carrier_scope import (
    CarrierScope, broker_party_id_of, carrier_ids_for_broker, carrier_scope,
    contract_scope, program_scope, broker_scope, policy_scope,
)
from db import SessionLocal, Tenant

_NOT_FOUND_FILE = "not found"

router = APIRouter(tags=["carrier"])

_C = "/carriers/{carrier_id}"
_P = _C + "/programs/{program_id}"
_B = _P + "/brokers/{broker_party_id}"
_T = _B + "/contracts/{contract_id}"


# ── 0. carriers ─────────────────────────────────────────────────────────────
# The entry point of the chain. For a carrier seat this is a list of one; for a
# broker it is the carriers that put them on a programme; for the platform admin
# it is every carrier. This is what replaces the old `?mga=` selector.

@router.get("/carriers")
def carriers_list(p: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        if p.is_platform_admin:
            rows = s.query(Tenant).all()
        elif p.is_broker:
            bid = broker_party_id_of(s, p)
            ids = carrier_ids_for_broker(s, bid) if bid else set()
            rows = s.query(Tenant).filter(Tenant.id.in_(ids)).all() if ids else []
        else:
            rows = s.query(Tenant).filter(Tenant.id == p.tenant_id).all() \
                if p.tenant_id else []
        return [{"id": t.id, "code": t.tenant_name,
                 "name": t.legal_name or t.tenant_name}
                for t in sorted(rows, key=lambda t: (t.legal_name or t.tenant_name or "").lower())]


@router.get(_C)
def carrier_get(scope: CarrierScope = Depends(carrier_scope)):
    with SessionLocal() as s:
        t = s.query(Tenant).filter(Tenant.id == scope.carrier_id).first()
        if t is None:
            raise HTTPException(404, "not found")
        return {"id": t.id, "code": t.tenant_name,
                "name": t.legal_name or t.tenant_name}


# ── 1. programmes (carrier owns them) ───────────────────────────────────────

@router.get(_C + "/programs")
def programs_list(scope: CarrierScope = Depends(carrier_scope)):
    return _app.programs_list(mga=scope.mga, principal=scope.acting)


@router.post(_C + "/programs")
def programs_create(body: _app.ProgramBody,
                    scope: CarrierScope = Depends(carrier_scope)):
    return _app.programs_create(mga=scope.mga, body=body, principal=scope.acting)


@router.get(_P)
def program_get(scope: CarrierScope = Depends(program_scope)):
    return _app.programs_get(program_id=scope.program_id, principal=scope.acting)


@router.put(_P)
def program_update(body: _app.ProgramBody,
                   scope: CarrierScope = Depends(program_scope)):
    return _app.programs_update(program_id=scope.program_id, body=body,
                                principal=scope.acting)


@router.get(_P + "/schedule")
def program_schedule_get(scope: CarrierScope = Depends(program_scope)):
    return _app.program_schedule_get(program_id=scope.program_id,
                                     principal=scope.acting)


@router.put(_P + "/schedule")
def program_schedule_put(body: _app.ScheduleBody,
                         scope: CarrierScope = Depends(program_scope)):
    return _app.program_schedule_put(program_id=scope.program_id, body=body,
                                     principal=scope.acting)


# ── 2. brokers on a programme (the carrier's grant) ─────────────────────────
# `program_broker` IS the permission: a broker with no row here cannot hold a
# contract on the programme, and removing the row stops new work without
# deleting history.

@router.get(_P + "/brokers")
def programme_brokers(scope: CarrierScope = Depends(program_scope)):
    return _hier.programme_brokers(program_id=scope.program_id,
                                   principal=scope.acting)


@router.post(_P + "/brokers")
def programme_broker_add(body: _hier.BrokerAssignBody,
                         scope: CarrierScope = Depends(program_scope),
                         _guard: Principal = Depends(require_role("carrier_admin"))):
    return _hier.programme_broker_add(program_id=scope.program_id, body=body,
                                      principal=scope.acting)


@router.delete(_P + "/brokers/{broker_party_id}")
def programme_broker_remove(scope: CarrierScope = Depends(broker_scope),
                            _guard: Principal = Depends(require_role("carrier_admin"))):
    return _hier.programme_broker_remove(program_id=scope.program_id,
                                         broker_party_id=scope.broker_party_id,
                                         principal=scope.acting)


# ── 3. contracts (a contract is programme × broker) ─────────────────────────

@router.get(_B + "/contracts")
def broker_contracts_list(
                          scope: CarrierScope = Depends(broker_scope)):
    """Contracts this broker holds on this programme.

    The flat /programs/{id}/contracts returns EVERY broker's contracts; here the
    broker is part of the address, so the shared handler narrows to theirs.

    That narrowing deliberately also returns the programme's CARRIER-HELD
    contracts (broker_party_id NULL): they predate the broker level and still
    govern the programme, so hiding them would make an existing setup look
    empty. The filtering is left to program_contracts_list rather than repeated
    here, so the two can never disagree about that rule.

    It once took an `approved_only` flag, back when a contract could be waiting
    on the carrier's approval. Nothing waits any more — that gate is gone.
    """
    return _app.program_contracts_list(
        program_id=scope.program_id,
        broker_party_id=scope.broker_party_id,
        principal=scope.acting,
    )


@router.post(_B + "/contracts")
async def broker_contract_upload(
    file: UploadFile = File(...),
    output_template_id: int = Form(...),
    schedule_key: Optional[str] = Form(default=None),
    reference_files: Optional[list[UploadFile]] = File(default=None),
    continue_anyway: bool = Form(default=False),
    resume_token: Optional[str] = Form(default=None),
    enable_reference_halt: bool = Form(default=False),
    upload_token: Optional[str] = Form(default=None),
    scope: CarrierScope = Depends(broker_scope),
):
    """Upload a contract for this programme, filed under this broker.

    This was "the broker uploads a contract and waits for the carrier to
    approve it". There is no approval gate any more, and no broker-side upload
    screen: a contract is the carrier's, and the broker in the address says who
    it is WITH, not who brought it.
    """
    return await _app.program_contract_upload(
        program_id=scope.program_id, file=file,
        output_template_id=output_template_id, schedule_key=schedule_key,
        # The broker is part of the address here, so the contract is filed
        # under them rather than landing as a carrier-held one.
        broker_party_id=scope.broker_party_id,
        reference_files=reference_files, continue_anyway=continue_anyway,
        resume_token=resume_token, enable_reference_halt=enable_reference_halt,
        upload_token=upload_token, principal=scope.acting,
    )


@router.get(_T)
def contract_detail(scope: CarrierScope = Depends(contract_scope)):
    return _app.program_contract_detail(program_id=scope.program_id,
                                        contract_id=scope.contract_id,
                                        principal=scope.acting)


# ── 4. the contract's thread ───────────────────────────────────────────────

@router.get(_T + "/approvals")
def contract_approval_history(scope: CarrierScope = Depends(contract_scope)):
    """How this contract got to where it is — the negotiation thread: terms sent
    out, changes asked for, terms agreed."""
    return _hier.contract_approval_history(contract_id=scope.contract_id,
                                           principal=scope.acting)


# ── 5-7. bordereau: setup, submission, validation ───────────────────────────

@router.post(_B + "/setup")
async def broker_bordereau_setup(
    contract_file: UploadFile = File(...),
    template_file: Optional[UploadFile] = File(default=None),
    template_name: Optional[str] = Form(default=None),
    output_template_id: Optional[int] = Form(default=None),
    output_format: str = Form(default="xlsx"),
    continue_anyway: bool = Form(default=False),
    resume_token: Optional[str] = Form(default=None),
    reference_files: Optional[list[UploadFile]] = File(default=None),
    scope: CarrierScope = Depends(broker_scope),
):
    """Step 5 — bordereau setup for this broker on this programme.

    Combined output-template + contract upload: the Output Template is resolved
    first (it is the semantic bridge), then the contract is processed with that
    template's fields as context. Sits under the broker because the setup is
    per (programme × broker), which is exactly what a contract is.
    """
    return await _app.program_setup(
        program_id=scope.program_id, contract_file=contract_file,
        template_file=template_file, template_name=template_name,
        output_template_id=output_template_id, output_format=output_format,
        continue_anyway=continue_anyway, resume_token=resume_token,
        reference_files=reference_files, principal=scope.acting,
    )


@router.get(_T + "/policies")
def contract_policies(limit: int = Query(default=50, le=500),
                      offset: int = Query(default=0, ge=0),
                      scope: CarrierScope = Depends(contract_scope)):
    """The policies written under this contract — the last link of the chain.

    Reads the canonical warehouse directly (policy is not an ops ORM entity)
    and returns only the CURRENT SCD-2 version of each row.
    """
    from sqlalchemy import select, func as _f
    from canonical import CANONICAL_TABLES
    from db import CanonicalSession
    t = CANONICAL_TABLES["policy"]

    def _current(stmt):
        if "is_current_version" in t.c:
            stmt = stmt.where(t.c.is_current_version.isnot(False))
        return stmt

    with CanonicalSession() as cs:
        total = cs.execute(_current(
            select(_f.count()).select_from(t)
            .where(t.c.policy_contract_id == scope.contract_id))).scalar() or 0
        rows = cs.execute(_current(
            select(t).where(t.c.policy_contract_id == scope.contract_id)
            .order_by(t.c.policy_id.desc()).limit(limit).offset(offset))
        ).mappings().all()
    return {
        "total": total, "limit": limit, "offset": offset,
        "items": [{k: v for k, v in dict(r).items() if v is not None} for r in rows],
    }


@router.get(_T + "/policies/{policy_id}")
def contract_policy_detail(scope: CarrierScope = Depends(policy_scope)):
    """One fully-assembled policy: scalar parents plus child collections."""
    from assembler import fetch_policy
    from db import CanonicalSession
    with CanonicalSession() as cs:
        return fetch_policy(cs, scope.policy_id)


# ── 6. the bordereau run — under the CONTRACT ───────────────────────────────
# Steps 6 and 7 of the workflow at the top of this file: the broker submits a
# bordereau and reads its own validation results.
#
# THE RUN HANGS OFF THE CONTRACT, not off the programme or the broker. A
# bordereau is validated against a contract's rules, so the contract is what
# decides whether a file passes — and a broker with two contracts on one
# programme is answering to two different sets of rules. Naming the programme
# alone would leave "which rules?" unanswered and mix both contracts' history
# into one list. The chain already says this: contract sits between broker and
# policy, and a run is what turns a file into policies.
#
# The flat /direct/* lane cannot serve a broker at all. Every one of those
# handlers calls resolve_tenant_id(), which reads principal.tenant_id — and a
# broker seat carries no tenant of its own (auth_deps.Principal), so it answers
# "no tenant bound to this user". That is correct for a flat route: nothing in
# `?mga=carrier` proves the broker may act for that carrier. Under the chain it
# IS proved, one path segment at a time, so scope.acting can state the carrier
# and delegate to the very same handler.
#
# SETUP IS NOT HERE ON PURPOSE. Building the setup — the input template, the
# column mapping, the rules — stays the carrier's: it is what defines a valid
# file. The broker RUNS against what the carrier built.

def _carrier_party_id(s, carrier_id: int) -> int:
    """The party row that IS this carrier.

    Resolved here rather than asked of the caller. The carrier's own screens
    fetch it from /my-carrier-party, but a broker has no business knowing a
    carrier's internal party ids — and letting it send one would be a way to aim
    a run at another carrier's setup. Same find-or-create, keyed on
    `carrier::<tenant_id>`, so this is the same row that screen returns.
    """
    from ingester import _ensure_carrier_party
    pid = _ensure_carrier_party(s, carrier_id)
    if pid is None:
        raise HTTPException(500, "could not resolve this carrier's own party record")
    s.commit()
    return pid


@router.get(_T + "/bordereau")
def contract_bordereau_status(scope: CarrierScope = Depends(contract_scope)):
    """Can a bordereau be run against this contract yet, and against what.

    What has to be true is that a SETUP exists — the (programme × broker) setup
    the carrier builds, which says how to read the spreadsheet. That is not
    something the broker can fix themselves, which is exactly why the answer is
    a reason and not an upload box that fails on submit.

    It also used to require the contract to have cleared the carrier's approval
    gate. There is no gate any more, so the only remaining question is the
    setup.
    """
    from db import DirectFormat, ExportTemplate, Pipeline
    with SessionLocal() as s:
        cpid = _carrier_party_id(s, scope.carrier_id)

        # Mirrors direct_run's resolution order exactly, so what this endpoint
        # calls ready is what will actually be used. The setup is per
        # (carrier, programme, broker) — the CONTRACT supplies the rules, not
        # the layout, which is why one setup can serve several contracts.
        def _pipe(broker: Optional[int], active_only: bool):
            q = (s.query(Pipeline)
                 .filter(Pipeline.tenant_id == scope.carrier_id,
                         Pipeline.carrier_party_id == cpid,
                         Pipeline.program_id == scope.program_id))
            if active_only:
                q = q.filter(Pipeline.status == "active")
            q = q.filter(Pipeline.broker_party_id == broker if broker is not None
                         else Pipeline.broker_party_id.is_(None))
            return q.order_by(Pipeline.id.desc()).first()

        def _payload(pipe: Pipeline, held_by: str):
            tpl = (s.get(ExportTemplate, pipe.output_template_id)
                   if pipe.output_template_id else None)
            return {
                "id": pipe.id,
                "name": pipe.name,
                "status": pipe.status,
                # Whose setup this is. A broker running on the programme-wide
                # setup should know that is what happened — it explains why the
                # expected columns are not the ones discussed for their book.
                "held_by": held_by,
                "output_template": ({"id": tpl.id, "name": tpl.name} if tpl else None),
            }

        pipe, held_by = _pipe(scope.broker_party_id, True), "broker"
        if pipe is None:
            pipe, held_by = _pipe(None, True), "programme"
        if pipe is not None:
            return {"ready": True, "reason": None, "setup": _payload(pipe, held_by)}

        # The pre-pipeline fallback direct_run still honours: an approved
        # DirectFormat with no pipeline built around it yet.
        fmt = (s.query(DirectFormat)
               .filter(DirectFormat.tenant_id == scope.carrier_id,
                       DirectFormat.carrier_party_id == cpid,
                       DirectFormat.program_id == scope.program_id,
                       DirectFormat.approved == 1)
               .order_by(DirectFormat.id.desc()).first())
        if fmt is not None:
            return {"ready": True, "reason": None, "setup": {
                "id": None, "name": fmt.name, "status": "active",
                "held_by": "programme", "output_template": None}}

        # NOT READY — and the two reasons are not the same thing.
        #
        # A setup that exists but was never ACTIVATED is the common case, and
        # reporting it as "no setup has been built" is both wrong and useless:
        # it sends the carrier off to build a second one when the first is
        # sitting there a click away from live. So look again without the status
        # filter and say which of the two it is.
        draft, held_by = _pipe(scope.broker_party_id, False), "broker"
        if draft is None:
            draft, held_by = _pipe(None, False), "programme"
        if draft is not None:
            return {
                "ready": False,
                "reason": ("Your carrier has built the bordereau setup for this "
                           "programme but has not made it live yet. Nothing can "
                           "be submitted against a setup that is still a draft — "
                           "ask them to activate it."),
                "setup": _payload(draft, held_by),
            }

        return {
            "ready": False,
            "reason": ("The carrier has not built the bordereau setup for this "
                       "programme yet. Until they do there is nothing to "
                       "validate your file against."),
            "setup": None,
        }


@router.post(_T + "/runs")
async def contract_bordereau_run(
    file: UploadFile = File(...),
    filename: Optional[str] = Form(default=None),
    skip_rows: int = Form(default=0),
    # The pre-submission self-check (workflow step 7): run every validation and
    # return the fix-list WITHOUT ingesting or recording a run. This is the
    # whole point of giving the broker the lane — they find out what is wrong
    # before the carrier does, not after.
    check_only: bool = Form(default=False),
    scope: CarrierScope = Depends(contract_scope),
):
    """Step 6 — submit a bordereau against this contract."""
    import direct_routes as _direct
    with SessionLocal() as s:
        cpid = _carrier_party_id(s, scope.carrier_id)
    return await _direct.direct_run(
        mga=scope.mga,
        carrier_party_id=cpid,
        program_id=scope.program_id,
        file=file,
        filename=filename,
        # Who ran it, for the audit trail — attributable to the broker rather
        # than to a tenant code that means nothing on their side.
        actor=f"broker:{scope.broker_party_id}",
        skip_rows=skip_rows,
        check_only=check_only,
        broker_party_id=scope.broker_party_id,
        contract_id=scope.contract_id,
        principal=scope.acting,
    )


@router.get(_T + "/runs")
def contract_bordereau_runs(limit: int = Query(default=20, le=100),
                            scope: CarrierScope = Depends(contract_scope)):
    """What has been submitted against this contract, newest first."""
    import direct_routes as _direct
    with SessionLocal() as s:
        cpid = _carrier_party_id(s, scope.carrier_id)
    # page/page_size are passed EXPLICITLY. Their declared defaults are fastapi
    # Query(...) objects, which only become None when FastAPI resolves the
    # request — calling the handler directly leaves the Query instance in place,
    # and `if page is not None` would take the paginated branch with a Query
    # object where an int belongs.
    #
    # Both broker AND contract are passed. Filtering on the setup alone would
    # leak: two brokers on one programme share a DirectFormat, so each would
    # read the other's filenames, row counts and exception counts.
    return _direct.direct_runs(
        mga=scope.mga,
        carrier_party_id=cpid,
        program_id=scope.program_id,
        broker_party_id=scope.broker_party_id,
        contract_id=scope.contract_id,
        page=None,
        page_size=None,
        limit=limit,
        principal=scope.acting,
    )


def _scoped_export(s, scope: CarrierScope, export_id: int):
    """An export row, checked to belong to THIS point in the chain.

    Not assert_tenant_owns(), which is all /export/downloads/{id}/* does: for a
    broker acting on this carrier that would pass for every export the carrier
    holds, including the runs of the other brokers on the same programme. The
    export carries its own denormalised carrier/programme/broker/contract — the
    scope the run was actually made for — so that is what is compared.

    404 (not 403) on every miss, for the same reason as the rest of the chain:
    a 403 would confirm the id exists.
    """
    from db import OutputExport
    row = s.get(OutputExport, export_id)
    if (row is None
            or row.tenant_id != scope.carrier_id
            or row.program_id != scope.program_id
            or row.broker_party_id != scope.broker_party_id
            or row.contract_id != scope.contract_id):
        raise HTTPException(404, _NOT_FOUND_FILE)
    return row


@router.get(_T + "/runs/{export_id}/data")
def contract_bordereau_data(export_id: int,
                            full: bool = False,
                            marks: bool = False,
                            sheet: Optional[str] = None,
                            offset: int = 0,
                            limit: Optional[int] = None,
                            row_indices: Optional[str] = None,
                            scope: CarrierScope = Depends(contract_scope)):
    """The rendered rows of one of this contract's runs, for in-site viewing.

    What the output preview and "See All Rows" read. Same payload as
    /export/downloads/{id}/data — `marks=1` returns the failed-validation cells
    so the preview highlights exactly what the downloaded file does, `full=1`
    lifts the preview row cap — but reachable by the broker that produced the
    run, which the flat route is not (it resolves the tenant from the token, and
    a broker seat has none).
    """
    import main as _main
    with SessionLocal() as s:
        _scoped_export(s, scope, export_id)
    # Scope is proven above; the delegated handler re-checks tenant ownership
    # against the pinned principal, which is the carrier this request was just
    # authorized for.
    return _main.export_download_data(
        export_id=export_id, full=full, marks=marks, sheet=sheet,
        offset=offset, limit=limit, row_indices=row_indices,
        principal=scope.acting,
    )


@router.get(_T + "/runs/{export_id}/file")
def contract_bordereau_download(export_id: int,
                                scope: CarrierScope = Depends(contract_scope)):
    """Download the output one of this contract's runs produced.

    NOT a delegation to /export/downloads/{id}/file. That route's only guard is
    assert_tenant_owns(), which for a broker acting on this carrier would pass
    for EVERY export the carrier holds — including the runs of the other brokers
    on the same programme. The scope is checked against the export's own
    denormalised carrier/programme/broker/contract instead, which is the scope
    the run was actually made for.
    """
    from fastapi import Response
    import storage
    from output_serializers import content_type_for_filename

    with SessionLocal() as s:
        row = _scoped_export(s, scope, export_id)
        data = storage.resolve_bytes(row.blob_ref, row.blob)
        if not data:
            raise HTTPException(404, _NOT_FOUND_FILE)
        name = row.filename or f"bordereau-{export_id}.xlsx"
        return Response(
            content=data,
            media_type=content_type_for_filename(name),
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
