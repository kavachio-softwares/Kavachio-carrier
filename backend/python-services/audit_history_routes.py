"""The Audit Logs tabs beyond the trail (audit_history.py): its tiles,
Downloads & Views, Partner Submission History, Contract History and File
Versions. Every seat may call them; what differs is whose rows come back
(audit_history.reach / audit_feed.scope_for), the same rule as /audit/logs."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

import audit_history as ah
from audit_routes import _filters
from auth_deps import Principal, current_principal
from db import SessionLocal

router = APIRouter(prefix="/audit/history", tags=["audit"])


@router.get("/summary")
def trail_summary(days: Optional[int] = None,
                  since: Optional[str] = Query(default=None, alias="from"),
                  until: Optional[str] = Query(default=None, alias="to"),
                  actor: Optional[str] = None, action: Optional[str] = None, q: Optional[str] = None,
                  record: Optional[str] = None, verb: Optional[str] = None,
                  programme: Optional[int] = None, status: Optional[str] = None, tz: int = 0,
                  principal: Principal = Depends(current_principal)):
    import audit_one
    with SessionLocal() as s:
        f = audit_one.narrow(s, _filters(days, since, until, actor, None, q, record), action or "", verb or "")
        return ah.summary(s, principal, f, programme=programme, status=status or "", tz=tz)


@router.get("/access")
def downloads_and_views(kind: str = "", page: int = 1, page_size: int = 50,
                        days: Optional[int] = None,
                        since: Optional[str] = Query(default=None, alias="from"),
                        until: Optional[str] = Query(default=None, alias="to"),
                        actor: Optional[str] = None, q: Optional[str] = None,
                        record: Optional[str] = None,
                        principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return ah.access(s, principal, _filters(days, since, until, actor, "all", q, record),
                         kind, page, page_size)


@router.get("/submissions")
def partner_submissions(mga: Optional[str] = None, broker: Optional[int] = None,
                        programme: Optional[int] = None, period: Optional[str] = None,
                        since: Optional[str] = Query(default=None, alias="from"),
                        until: Optional[str] = Query(default=None, alias="to"),
                        status: Optional[str] = None, page: int = 1, page_size: int = 50,
                        principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return ah.submissions(s, principal, mga=mga, broker=broker, programme=programme, period=period,
                              since=since, until=until, status=status, page=page, page_size=page_size)


@router.get("/submissions/{expected_id}")
def partner_submission_timeline(expected_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return ah.submission_timeline(s, principal, expected_id)


@router.get("/contracts")
def contract_history(mga: Optional[str] = None, broker: Optional[int] = None,
                     programme: Optional[int] = None, contract: Optional[int] = None,
                     status: Optional[str] = None,
                     since: Optional[str] = Query(default=None, alias="from"),
                     until: Optional[str] = Query(default=None, alias="to"),
                     page: int = 1, page_size: int = 50,
                     principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return ah.contracts(s, principal, mga=mga, broker=broker, programme=programme, contract=contract,
                            status=status, since=since, until=until, page=page, page_size=page_size)


@router.get("/contracts/{contract_id}")
def contract_history_timeline(contract_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return ah.contract_timeline(s, principal, contract_id)


@router.get("/treaties")
def treaty_history(mga: Optional[str] = None, reinsurer: Optional[int] = None,
                   programme: Optional[int] = None, treaty: Optional[int] = None,
                   status: Optional[str] = None,
                   since: Optional[str] = Query(default=None, alias="from"),
                   until: Optional[str] = Query(default=None, alias="to"),
                   page: int = 1, page_size: int = 50,
                   principal: Principal = Depends(current_principal)):
    import treaty_history as th
    with SessionLocal() as s:
        return th.history(s, principal, mga=mga, reinsurer=reinsurer, programme=programme, treaty=treaty,
                          status=status, since=since, until=until, page=page, page_size=page_size)


@router.get("/treaties/{treaty_id}")
def treaty_history_timeline(treaty_id: int, mga: Optional[str] = None,
                            principal: Principal = Depends(current_principal)):
    import treaty_history as th
    with SessionLocal() as s:
        return th.timeline(s, principal, treaty_id, mga)


@router.get("/versions")
def file_versions(mga: Optional[str] = None, broker: Optional[int] = None,
                  programme: Optional[int] = None, period: Optional[str] = None,
                  page: int = 1, page_size: int = 50,
                  principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return ah.versions(s, principal, mga=mga, broker=broker, programme=programme, period=period,
                           page=page, page_size=page_size)


@router.get("/choices")
def history_choices(mga: Optional[str] = None, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return ah.choices(s, principal, mga)


@router.get("/partners")
def partner_monitoring(mga: Optional[str] = None, broker: Optional[int] = None, programme: Optional[int] = None,
                       risk: Optional[str] = None, type: Optional[str] = None, page: int = 1, page_size: int = 50,
                       principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return ah.partners(s, principal, mga=mga, broker=broker, programme=programme, risk=risk, kind=type,
                           page=page, page_size=page_size)


@router.get("/partners/{broker_id}")
def partner_profile(broker_id: int, mga: Optional[str] = None, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return ah.partner_profile(s, principal, broker_id, mga)


@router.get("/restores")
def setup_restores(mga: Optional[str] = None, page: int = 1, page_size: int = 50,
                   principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return ah.restores(s, principal, mga=mga, page=page, page_size=page_size)


@router.get("/package")
def audit_package(policy: Optional[str] = None, month: Optional[str] = None,
                  since: Optional[str] = Query(default=None, alias="from"),
                  until: Optional[str] = Query(default=None, alias="to"),
                  programme: Optional[int] = None, broker: Optional[int] = None,
                  format: str = "excel", mga: Optional[str] = None, dates: Optional[str] = None,
                  principal: Principal = Depends(current_principal)):
    """One file for a policy or a period: the checks that ran, their contract
    wording and page, every result, error and decision (audit_package.py)."""
    import audit_package as ap
    from fastapi.responses import Response
    with SessionLocal() as s:
        pkg = ap.build(s, principal, policy=policy, month=month, since=since, until=until,
                       programme=programme, broker=broker, mga=mga, dates=dates)
    name, data, media = ap.render(pkg, format)
    return Response(content=data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
