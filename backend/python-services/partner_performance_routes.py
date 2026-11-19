"""Task 36 — Partner Performance (partner_performance.py).

  GET /reports/partner-performance        one period's broker scores, filtered,
                                          with the best and worst brokers
  GET /reports/partner-performance/file   the same table as an Excel file

Carrier only, as the Executive Report it sits beside (executive_routes): a
broker seat is refused by the role check. Reading never writes.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import Response

import partner_performance as pp
from auth_deps import Principal, require_role
from db import SessionLocal

router = APIRouter(prefix="/reports/partner-performance", tags=["reports"])

_reader = require_role("carrier_admin")


def resolve_tenant_id(s, principal: Principal, mga: Optional[str] = None) -> int:
    """app_routes.resolve_tenant_id — imported when asked, because importing
    app_routes reads the rule library from the database."""
    from app_routes import resolve_tenant_id as resolve
    return resolve(s, principal, mga)


def _page(principal, mga, cadence, period, program_id, file_type, broker_id) -> dict:
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        return pp.page(s, tid, cadence=cadence, period=period, programme=program_id,
                       file_type=file_type or None, broker=broker_id)


@router.get("")
def partner_performance(cadence: str = "month", period: Optional[str] = None,
                        program_id: Optional[int] = None, file_type: Optional[str] = None,
                        broker_id: Optional[int] = None, mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    """One month (`period` YYYY-MM; none = the last complete one), quarter
    (YYYY-Qn) or year (YYYY; none = the one running). `file_type` a way in
    (upload, email, sftp, api, …); `broker_id` one broker, ranked among all."""
    return _page(principal, mga, cadence, period, program_id, file_type, broker_id)


@router.get("/file")
def partner_performance_file(cadence: str = "month", period: Optional[str] = None,
                             program_id: Optional[int] = None, file_type: Optional[str] = None,
                             broker_id: Optional[int] = None, mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    """The ranked table, as on screen with the same filters, as an Excel file."""
    data = _page(principal, mga, cadence, period, program_id, file_type, broker_id)
    name = f"Partner Performance {data['period']}.xlsx"
    return Response(
        content=pp.workbook(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
