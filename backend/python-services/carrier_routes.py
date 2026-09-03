"""The carrier-centric API: carrier → programme → broker → contract → policy.

These are the CANONICAL routes. They spell out the workflow the platform
actually implements:

    1. the carrier owns the lifecycle — programmes, brokers, contracts, setup
    2. the broker signs in and sees only what the carrier assigned it
    3. the broker uploads a contract
    4. the carrier reviews the extraction and approves or rejects it
    5. once approved, the broker completes bordereau setup and supplies files
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
def broker_contracts_list(approved_only: bool = Query(default=False),
                          scope: CarrierScope = Depends(broker_scope)):
    """Contracts this broker holds on this programme.

    The flat /programs/{id}/contracts returns EVERY broker's contracts; here the
    broker is part of the address, so the shared handler narrows to theirs.

    That narrowing deliberately also returns the programme's CARRIER-HELD
    contracts (broker_party_id NULL): they predate the broker level and still
    govern the programme, so hiding them would make an existing setup look
    empty. The filtering is left to program_contracts_list rather than repeated
    here, so the two can never disagree about that rule.

    `approved_only` drops anything still waiting on the carrier — only a live
    contract can have an output template built on it.
    """
    return _app.program_contracts_list(
        program_id=scope.program_id,
        broker_party_id=scope.broker_party_id,
        approved_only=approved_only,
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
    """Step 3 of the flow — the broker uploads a contract for this programme.

    A contract a BROKER uploads waits for its carrier; one the CARRIER uploads
    is live immediately. That decision is made from who submitted it (the DB
    trigger trg_set_contract_approval), never from anything the client says.
    """
    return await _app.program_contract_upload(
        program_id=scope.program_id, file=file,
        output_template_id=output_template_id, schedule_key=schedule_key,
        reference_files=reference_files, continue_anyway=continue_anyway,
        resume_token=resume_token, enable_reference_halt=enable_reference_halt,
        upload_token=upload_token, principal=scope.acting,
    )


@router.get(_T)
def contract_detail(scope: CarrierScope = Depends(contract_scope)):
    return _app.program_contract_detail(program_id=scope.program_id,
                                        contract_id=scope.contract_id,
                                        principal=scope.acting)


# ── 4. carrier review: approve / reject ─────────────────────────────────────

@router.post(_T + "/approve")
def contract_approve(body: _hier.ApprovalDecision = _hier.ApprovalDecision(),
                     scope: CarrierScope = Depends(contract_scope),
                     _guard: Principal = Depends(require_role("carrier_admin"))):
    return _hier.contract_approve(contract_id=scope.contract_id, body=body,
                                  principal=scope.acting)


@router.post(_T + "/reject")
def contract_reject(body: _hier.ApprovalDecision = _hier.ApprovalDecision(),
                    scope: CarrierScope = Depends(contract_scope),
                    _guard: Principal = Depends(require_role("carrier_admin"))):
    return _hier.contract_reject(contract_id=scope.contract_id, body=body,
                                 principal=scope.acting)


@router.get(_T + "/approvals")
def contract_approval_history(scope: CarrierScope = Depends(contract_scope)):
    """Every decision ever made on this contract — the contract row holds the
    CURRENT state, this holds how it got there."""
    return _hier.contract_approval_history(contract_id=scope.contract_id,
                                           principal=scope.acting)


@router.get(_C + "/approvals")
def carrier_approvals_queue(scope: CarrierScope = Depends(carrier_scope)):
    """What is waiting on THIS carrier — step 4's inbox."""
    return _hier.approvals_queue(principal=scope.acting)


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
