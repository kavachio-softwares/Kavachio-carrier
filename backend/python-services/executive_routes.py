"""Executive Reporting 38.2 — the API behind the Executive Report screen.

  GET  /reports/executive                    the report for one week or month
  GET  /reports/executive/file               it as a PDF or Excel file
  POST /reports/executive/send               email it now to the people chosen
  GET  /reports/executive/delivery           the schedule, who can receive it,
                                             when it next goes, what was sent
  PUT  /reports/executive/delivery           save the schedule
  POST /reports/executive/sends/clear        hide every sent report from the
                                             list (nothing is deleted)
  GET  /reports/executive/sends/{id}/file    a sent report, exactly as sent
  GET  /reports/executive/money-trend        six months of premium, for the tiles
  GET  /reports/executive/programmes         Programme Management Report: every
                                             programme and broker for the period
                                             (programme_report)
  GET  /reports/executive/programmes/file    it as a PDF or Excel file
  GET  /reports/executive/programmes/{p}/brokers/{b}
                                             one programme's broker, in detail

The carrier's own report: any carrier login reads and downloads it; sending
it and changing the schedule are the carrier's (require_carrier_admin), as on
the Monthly Report. Sends and schedule changes write their own named rows in
the Audit Logs (audit._SELF_LOGGED).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

import executive_delivery as ed
import executive_report as er
import programme_report as pr
import report_delivery as rd
from app_routes import _actor, resolve_tenant_id
from auth_deps import Principal, require_role
from carrier_scope import require_carrier_admin
from db import ActivityEvent, SessionLocal

router = APIRouter(prefix="/reports/executive", tags=["reports"])

_reader = require_role("carrier_admin")


def _file(name: str, data: bytes, mime: str) -> Response:
    return Response(content=data, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _period(cadence: str, period: Optional[str], date_from: Optional[str],
            date_to: Optional[str]) -> Optional[str]:
    """Custom dates come as `date_from` and `date_to` (YYYY-MM-DD); the other
    reports take `period`. Inside, a custom period is kept as 'from..to'."""
    if cadence != "custom":
        return period
    if not date_from or not date_to:
        raise HTTPException(400, "Pick a from date and a to date.")
    return f"{date_from}..{date_to}"


def _fmt(format: str) -> str:
    return "excel" if format in ("excel", "xlsx") else "pdf"


@router.get("")
def executive_report(cadence: str = "month", period: Optional[str] = None,
                     date_from: Optional[str] = None, date_to: Optional[str] = None,
                     mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    """The Executive Report for one day (`period` YYYY-MM-DD), one week
    (`period` any day of it), one month (`period` YYYY-MM) — none = the last
    complete one — or custom dates (`date_from`, `date_to`, YYYY-MM-DD)."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        out = er.pack(s, tid, cadence=cadence, period=_period(cadence, period, date_from, date_to))
        out["title"] = ed.period_title(out["cadence"], out["period"])
        out["headline"] = [{"label": k, "value": v} for k, v in ed.headline(out)]
        return out


@router.get("/file")
def executive_report_file(cadence: str = "month", period: Optional[str] = None,
                          date_from: Optional[str] = None, date_to: Optional[str] = None,
                          format: str = "pdf", mga: Optional[str] = None,
                          principal: Principal = Depends(_reader)):
    """The report as on screen, as a PDF or Excel file."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        pack = er.pack(s, tid, cadence=cadence, period=_period(cadence, period, date_from, date_to))
        return _file(*ed.files(pack, _fmt(format), rd.carrier_name(s, tid))[0])


@router.get("/money-trend")
def executive_money_trend(cadence: str = "month", period: Optional[str] = None,
                          date_from: Optional[str] = None, date_to: Optional[str] = None,
                          mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    """The last six months of premium for the money tiles' trend lines —
    asked for after the report, so the report never waits on it."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        return er.money_trend(s, tid, cadence, _period(cadence, period, date_from, date_to))


@router.get("/programmes")
def programme_management(cadence: str = "month", period: Optional[str] = None,
                         date_from: Optional[str] = None, date_to: Optional[str] = None,
                         mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    """Every programme and broker for the period the Executive Report covers."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        out = pr.management(s, tid, cadence=cadence, period=_period(cadence, period, date_from, date_to))
        out["title"] = ed.period_title(out["cadence"], out["period"])
        return out


@router.get("/programmes/file")
def programme_management_file(cadence: str = "month", period: Optional[str] = None,
                              date_from: Optional[str] = None, date_to: Optional[str] = None,
                              format: str = "excel", mga: Optional[str] = None,
                              principal: Principal = Depends(_reader)):
    """The Programme Management Report as a PDF or Excel file."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        out = pr.management(s, tid, cadence=cadence, period=_period(cadence, period, date_from, date_to))
        return _file(*pr.render(out, _fmt(format), rd.carrier_name(s, tid)))


@router.get("/programmes/{program_id}/brokers/{broker_party_id}")
def programme_broker_detail(program_id: int, broker_party_id: int, cadence: str = "month",
                            period: Optional[str] = None, date_from: Optional[str] = None,
                            date_to: Optional[str] = None, mga: Optional[str] = None,
                            principal: Principal = Depends(_reader)):
    """One programme's broker for the period: what opens under its row."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        return pr.detail(s, tid, program_id, broker_party_id, cadence=cadence,
                         period=_period(cadence, period, date_from, date_to))


class SendBody(BaseModel):
    cadence: str = "month"
    period: Optional[str] = None
    date_from: Optional[str] = None            # custom dates, YYYY-MM-DD
    date_to: Optional[str] = None
    user_ids: Optional[list[int]] = None       # the carrier's logins ticked; None = all
    emails: Optional[list[str]] = None         # other addresses
    file_format: Optional[str] = None


@router.post("/send")
def send_executive_report(body: SendBody, mga: Optional[str] = None,
                          principal: Principal = Depends(require_carrier_admin("send the executive report"))):
    """Email the report now to the logins ticked and the addresses typed."""
    from audit import actor_columns
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        try:
            r = ed.send(s, tid, cadence=body.cadence,
                        period=_period(body.cadence, body.period, body.date_from, body.date_to), kind="now",
                        user_id=principal.user_id, user_ids=body.user_ids,
                        emails=body.emails or [], file_format=body.file_format)
        except ValueError as e:
            raise HTTPException(400, str(e))
        s.add(ActivityEvent(tenant_id=tid, actor=_actor(principal), action="executive_report_sent",
                            target=f"executive_report:{r['cadence']}:{r['period']}",
                            details={"cadence": r["cadence"], "period": r["period"], "kind": "now",
                                     "to": [x["email"] for x in r["recipients"]],
                                     "delivered": r["delivered"], "failed": r["failed"]},
                            **actor_columns(principal)))
        s.commit()
        return r


def _delivery_view(s, tid: int) -> dict:
    cfg = ed.settings(s, tid)
    nxt = (ed.next_send(cfg, datetime.utcnow().date(), ed.sent_keys(s, tid))
           if cfg["auto_send"] else None)
    return {"settings": cfg, "logins": rd.people(s, tid), "next": nxt,
            "sends": ed.history(s, tid), "send_days": list(ed.SEND_DAYS),
            "week_days": list(ed.WEEK_DAYS)}


@router.get("/delivery")
def executive_delivery(mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    with SessionLocal() as s:
        return _delivery_view(s, resolve_tenant_id(s, principal, mga))


class DeliveryBody(BaseModel):
    auto_send: Optional[bool] = None
    cadence: Optional[str] = None
    week_day: Optional[int] = None
    send_day: Optional[int] = None
    file_format: Optional[str] = None
    user_ids: Optional[list[int]] = None
    emails: Optional[list[str]] = None


@router.put("/delivery")
def save_executive_delivery(body: DeliveryBody, mga: Optional[str] = None,
                            principal: Principal = Depends(require_carrier_admin("change how the executive report is sent"))):
    """Save the schedule. Each change is kept in the Audit Logs with the old
    and new value."""
    from audit import actor_columns
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        try:
            before, after = ed.save_settings(s, tid, body.model_dump(exclude_unset=True),
                                             principal.user_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        changes = {k: {"old": before.get(k), "new": v} for k, v in after.items()
                   if k != "saved" and before.get(k) != v}
        if changes:
            s.add(ActivityEvent(tenant_id=tid, actor=_actor(principal),
                                action="executive_report_settings_updated",
                                target="executive_report:settings", details={"changes": changes},
                                **actor_columns(principal)))
        s.commit()
        return _delivery_view(s, tid)


@router.post("/sends/clear")
def clear_executive_sends(mga: Optional[str] = None,
                          principal: Principal = Depends(require_carrier_admin("clear the sent reports list"))):
    """Clear the Sent reports list. The sends are hidden from it, not deleted:
    their records and files are kept, and the schedule never sends one twice."""
    from audit import actor_columns
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        n = ed.clear_history(s, tid)
        if n:
            s.add(ActivityEvent(tenant_id=tid, actor=_actor(principal),
                                action="executive_report_sends_cleared",
                                target="executive_report:sends", details={"cleared": n},
                                **actor_columns(principal)))
        s.commit()
        return _delivery_view(s, tid)


@router.get("/sends/{send_id}/file")
def executive_sent_file(send_id: int, format: str = "pdf", mga: Optional[str] = None,
                        principal: Principal = Depends(_reader)):
    """A report as it was sent: the numbers kept with it, so the file is the
    one people received."""
    from db import ExecutiveReportSend
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        row = (s.query(ExecutiveReportSend)
               .filter(ExecutiveReportSend.id == send_id, ExecutiveReportSend.tenant_id == tid).first())
        if row is None or row.pack is None:
            raise HTTPException(404, "That report was not sent, so there is no file to download.")
        return _file(*ed.files(row.pack, _fmt(format), (row.options or {}).get("carrier", ""))[0])
