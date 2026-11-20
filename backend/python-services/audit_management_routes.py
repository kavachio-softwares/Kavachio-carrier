"""Audit Management routes (audit_management.py, tasks 53.1 / 53.2).

  carrier   GET  /audits                               the list, its tiles and form choices
            POST /audits                               a new audit
            GET  /audits/waiting                       the dashboard card: replies to look at, overdue findings
            GET  /audits/{id}                          one audit with its findings and their history
            PUT  /audits/{id}                          change it
            POST /audits/{id}/close                    close it (every finding closed first)
            POST /audits/{id}/findings                 a new finding
            PUT  /audits/{id}/findings/{f}             change a finding
            POST /audits/{id}/findings/{f}/status      In progress / Closed (how it was fixed) / Open again
            POST /audits/{id}/findings/{f}/reply       a note, with or without a file
            GET  /audits/{id}/findings/{f}/files/{e}   a file on a finding
  broker    GET  /broker/audit-findings                the findings about this broker
            POST /broker/audit-findings/{f}/reply      a reply, with or without evidence
            GET  /broker/audit-findings/{f}/files/{e}  a file on one of its findings

Kavachio may read (with ?mga=) and changes nothing. Every write names itself
in the audit log (audit._SELF_LOGGED).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

import audit_alerts
import audit_management as am
from app_routes import _actor, resolve_tenant_id
from auth_deps import Principal, require_role
from carrier_scope import broker_party_id_of, require_carrier_admin
from db import ActivityEvent, SessionLocal

router = APIRouter(tags=["audits"])

_reader = require_role("carrier_admin")
_broker = require_role("broker_admin")


class AuditIn(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    broker_party_id: Optional[int] = None
    contract_id: Optional[int] = None
    scope: Optional[str] = None
    assigned_to: Optional[str] = None
    start_date: Optional[str] = None
    due_date: Optional[str] = None


class FindingIn(BaseModel):
    title: Optional[str] = None
    severity: Optional[str] = None
    action: Optional[str] = None
    owner: Optional[str] = None
    due_date: Optional[str] = None


class StatusIn(BaseModel):
    status: str
    note: Optional[str] = None


class NoteIn(BaseModel):
    note: Optional[str] = None


def _log(s, tid: int, principal: Principal, action: str, a: am.Audit, f: Optional[am.Finding] = None, **extra):
    from audit import actor_columns
    s.add(ActivityEvent(
        tenant_id=tid, actor=_actor(principal), action=action,
        target=f"audit_finding:{f.id}" if f else f"audit:{a.id}",
        details={"audit_id": a.id, "audit": am.audit_ref(a), "name": a.name,
                 **({"finding_id": f.id, "finding": f.title[:120]} if f else {}), **extra},
        **actor_columns(principal)))


def _tenant(s, principal: Principal, mga: Optional[str]) -> int:
    am.require_tables(s)
    return resolve_tenant_id(s, principal, mga)


def _reading(s, principal: Principal, mga: Optional[str]) -> Optional[int]:
    """The carrier whose audits are read; None for Kavachio with no carrier
    picked — every carrier's, read only (writes go through _tenant)."""
    import audit_history as ah
    am.require_tables(s)
    mga = ah.chosen_mga(mga)
    if principal.is_platform_admin and not mga:
        return None
    return resolve_tenant_id(s, principal, mga)


def _carrier_form(s, tid: int) -> dict:
    """The brokers and their contracts the form may offer."""
    import audit_feed as af
    from db import Contract, Party
    brokers = af.brokers_of_carrier(s, tid)
    names = dict(s.query(Party.id, Party.legal_name).filter(Party.id.in_(brokers or {0})))
    contracts = s.query(Contract).filter(Contract.tenant_id == tid, Contract.broker_party_id.in_(brokers or {0})).all()
    return {"brokers": sorted(({"id": b, "name": names.get(b) or f"#{b}"} for b in brokers), key=lambda x: x["name"].lower()),
            "contracts": sorted(({"id": c.id, "broker_id": c.broker_party_id,
                                  "name": c.name or c.filename or f"Contract #{c.id}"} for c in contracts),
                                key=lambda x: x["name"].lower())}


# ── the carrier ────────────────────────────────────────────────────────────
@router.get("/audits")
def list_audits(mga: Optional[str] = None, broker: Optional[int] = None, status: Optional[str] = None,
                since: Optional[str] = None, until: Optional[str] = None,
                principal: Principal = Depends(_reader)):
    import datetime as dt
    with SessionLocal() as s:
        if am.missing_tables(s):
            return {"ready": False, "message": am.NOT_SET_UP, "audits": [], "counts": {}, "types": [],
                    "severities": [], "form": {"brokers": [], "contracts": []}}
        tid = _reading(s, principal, mga)
        day = lambda v: dt.date.fromisoformat(v[:10]) if v else None  # noqa: E731
        out = am.list_audits(s, tid, broker=broker, status=status, since=day(since), until=day(until))
        out["form"] = _carrier_form(s, tid) if tid is not None else {"brokers": [], "contracts": []}
        return out


@router.post("/audits")
def create_audit(body: AuditIn, mga: Optional[str] = None,
                 principal: Principal = Depends(require_carrier_admin("record an audit"))):
    with SessionLocal() as s:
        tid = _tenant(s, principal, mga)
        a = am.create_audit(s, tid, principal.user_id, body.model_dump())
        _log(s, tid, principal, "audit_created", a)
        s.commit()
        return am.one_audit(s, a)


@router.get("/audits/waiting")
def waiting(mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    """The carrier dashboard's card: findings the broker answered, and overdue ones."""
    with SessionLocal() as s:
        if am.missing_tables(s):
            return {"ready": False, "replied": [], "overdue": []}
        return audit_alerts.waiting_for_carrier(s, _reading(s, principal, mga))


@router.get("/audits/{audit_id}")
def get_audit(audit_id: int, mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    with SessionLocal() as s:
        return am.one_audit(s, am.get_audit(s, _reading(s, principal, mga), audit_id))


@router.put("/audits/{audit_id}")
def edit_audit(audit_id: int, body: AuditIn, mga: Optional[str] = None,
               principal: Principal = Depends(require_carrier_admin("change an audit"))):
    with SessionLocal() as s:
        tid = _tenant(s, principal, mga)
        a = am.update_audit(s, am.get_audit(s, tid, audit_id), body.model_dump())
        _log(s, tid, principal, "audit_updated", a)
        s.commit()
        return am.one_audit(s, a)


@router.post("/audits/{audit_id}/close")
def close_audit(audit_id: int, body: NoteIn = NoteIn(), mga: Optional[str] = None,
                principal: Principal = Depends(require_carrier_admin("close an audit"))):
    with SessionLocal() as s:
        tid = _tenant(s, principal, mga)
        a = am.close_audit(s, am.get_audit(s, tid, audit_id), principal.user_id, body.note)
        _log(s, tid, principal, "audit_closed", a, note=body.note)
        s.commit()
        return am.one_audit(s, a)


@router.post("/audits/{audit_id}/findings")
def add_finding(audit_id: int, body: FindingIn, mga: Optional[str] = None,
                principal: Principal = Depends(require_carrier_admin("add a finding"))):
    with SessionLocal() as s:
        tid = _tenant(s, principal, mga)
        a = am.get_audit(s, tid, audit_id)
        f = am.add_finding(s, a, principal.user_id, body.model_dump())
        _log(s, tid, principal, "audit_finding_created", a, f, severity=f.severity)
        s.commit()
        audit_alerts.finding_raised(s, a, f, principal.user_id)
        return am.one_audit(s, a)


@router.put("/audits/{audit_id}/findings/{finding_id}")
def edit_finding(audit_id: int, finding_id: int, body: FindingIn, mga: Optional[str] = None,
                 principal: Principal = Depends(require_carrier_admin("change a finding"))):
    with SessionLocal() as s:
        tid = _tenant(s, principal, mga)
        a = am.get_audit(s, tid, audit_id)
        f = am.update_finding(s, am.get_finding(s, a, finding_id), principal.user_id, body.model_dump())
        _log(s, tid, principal, "audit_finding_updated", a, f)
        s.commit()
        return am.one_audit(s, a)


@router.post("/audits/{audit_id}/findings/{finding_id}/status")
def finding_status(audit_id: int, finding_id: int, body: StatusIn, mga: Optional[str] = None,
                   principal: Principal = Depends(require_carrier_admin("change a finding"))):
    with SessionLocal() as s:
        tid = _tenant(s, principal, mga)
        a = am.get_audit(s, tid, audit_id)
        f = am.get_finding(s, a, finding_id)
        before = f.status
        am.set_status(s, a, f, principal.user_id, body.status, body.note)
        action = ("audit_finding_closed" if f.status == "closed" else
                  "audit_finding_reopened" if before == "closed" else "audit_finding_status")
        _log(s, tid, principal, action, a, f, status=am.FINDING_STATUS[f.status], note=body.note)
        s.commit()
        return am.one_audit(s, a)


@router.post("/audits/{audit_id}/findings/{finding_id}/reply")
async def carrier_reply(audit_id: int, finding_id: int, note: Optional[str] = Form(None),
                        file: Optional[UploadFile] = File(None), mga: Optional[str] = None,
                        principal: Principal = Depends(require_carrier_admin("reply on a finding"))):
    data = await file.read() if file else None
    with SessionLocal() as s:
        tid = _tenant(s, principal, mga)
        a = am.get_audit(s, tid, audit_id)
        f = am.reply(s, am.get_finding(s, a, finding_id), principal.user_id, "carrier", note,
                     file.filename if file else None, data)
        _log(s, tid, principal, "audit_finding_evidence" if data else "audit_finding_replied", a, f,
             **({"file": file.filename} if file else {}))
        s.commit()
        audit_alerts.carrier_replied(s, a, f, note, principal.user_id)
        return am.one_audit(s, a)


def _file(name: str, data: bytes) -> Response:
    safe = name.replace('"', "")
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@router.get("/audits/{audit_id}/findings/{finding_id}/files/{event_id}")
def carrier_file(audit_id: int, finding_id: int, event_id: int, mga: Optional[str] = None,
                 principal: Principal = Depends(_reader)):
    with SessionLocal() as s:
        a = am.get_audit(s, _reading(s, principal, mga), audit_id)
        return _file(*am.evidence_bytes(s, am.get_finding(s, a, finding_id), event_id))


# ── the broker ─────────────────────────────────────────────────────────────
def _bid(s, principal: Principal) -> int:
    bid = broker_party_id_of(s, principal)
    if not bid:
        raise HTTPException(403, "Your login is not linked to a broker.")
    return bid


@router.get("/broker/audit-findings")
def broker_findings(principal: Principal = Depends(_broker)):
    with SessionLocal() as s:
        if am.missing_tables(s):
            return {"ready": False, "findings": [], "open": 0}
        return am.broker_findings(s, _bid(s, principal))


@router.post("/broker/audit-findings/{finding_id}/reply")
async def broker_reply(finding_id: int, note: Optional[str] = Form(None),
                       file: Optional[UploadFile] = File(None), principal: Principal = Depends(_broker)):
    data = await file.read() if file else None
    with SessionLocal() as s:
        am.require_tables(s)
        bid = _bid(s, principal)
        f = am.broker_finding(s, bid, finding_id)
        am.reply(s, f, principal.user_id, "broker", note, file.filename if file else None, data)
        a = s.get(am.Audit, f.audit_id)
        _log(s, f.tenant_id, principal, "audit_finding_evidence" if data else "audit_finding_replied", a, f,
             **({"file": file.filename} if file else {}))
        s.commit()
        audit_alerts.broker_replied(s, a, f, note, file.filename if file else None)
        return am.broker_findings(s, bid)


@router.get("/broker/audit-findings/{finding_id}/files/{event_id}")
def broker_file(finding_id: int, event_id: int, principal: Principal = Depends(_broker)):
    with SessionLocal() as s:
        am.require_tables(s)
        f = am.broker_finding(s, _bid(s, principal), finding_id)
        return _file(*am.evidence_bytes(s, f, event_id))
