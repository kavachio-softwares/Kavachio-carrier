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
                  principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return ah.summary(s, principal, _filters(days, since, until, actor, action, q))


@router.get("/access")
def downloads_and_views(kind: str = "", page: int = 1, page_size: int = 50,
                        days: Optional[int] = None,
                        since: Optional[str] = Query(default=None, alias="from"),
                        until: Optional[str] = Query(default=None, alias="to"),
                        actor: Optional[str] = None, q: Optional[str] = None,
                        principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        return ah.access(s, principal, _filters(days, since, until, actor, "all", q), kind, page, page_size)


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
