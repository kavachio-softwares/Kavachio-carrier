"""Policy Search (Phase 2, 43) — the API behind the Policies screen.

  GET  /policies             one page of the policies in the processed
                             bordereaux, with the choices the screen's filters
                             offer — or only the ones behind a number on a
                             report (`from`, 38.1)
  GET  /policies/detail      every bordereau row behind one of them
  GET  /policies/file        every policy the same filters select, as an
                             Excel file (43.2)
  GET  /policies/recipients  whose logins the list can be emailed to
  POST /policies/send        email that file to the logins ticked and the
                             addresses typed (43.2)

For the carrier and broker seats only; policy_search.seat_of decides whose
policies come back and refuses everyone else (Kavachio included). The file and
the email each write their own named Audit Logs row — who took how many
policies out, with which filters, to whom (43.3, policy_export.record).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

import drill_down
import policy_export as pe
import policy_search
from auth_deps import Principal, current_principal
from db import SessionLocal

router = APIRouter(prefix="/policies", tags=["policies"])


def _filters(
    q: Optional[str] = None,
    program_id: Optional[int] = None,
    broker_party_id: Optional[int] = None,
    carrier_id: Optional[int] = None,
    state: Optional[str] = None,
    date_field: str = "start",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    # ── the number clicked (drill_down.resolve) ──
    from_: Optional[str] = Query(None, alias="from"),
    label: Optional[str] = None,
    basis: str = "month",
    period: Optional[str] = None,
    window: str = "current",
    metric: Optional[str] = None,
    currency: Optional[str] = None,
    open_problems: str = "include",
    month: Optional[str] = None,
    measure: Optional[str] = None,
    dates: Optional[str] = None,
    check: Optional[str] = None,
) -> dict:
    """The screen's filters and the number clicked — ONE set, read the same
    way by the list, the file and the email, so what goes out is what was on
    screen."""
    return {"q": q, "program_id": program_id, "broker_party_id": broker_party_id,
            "carrier_id": carrier_id, "state": state, "date_field": date_field,
            "date_from": date_from, "date_to": date_to, "from": from_, "label": label,
            "basis": basis, "period": period, "window": window, "metric": metric,
            "currency": currency, "open_problems": open_problems, "month": month,
            "measure": measure, "dates": dates, "check": check}


def _select(s, principal: Principal, f: dict, page: int = 1,
            page_size: Optional[int] = 25) -> dict:
    """The search for `f`: one page, or every match when `page_size` is None."""
    drill = drill_down.resolve(
        s, principal, source=f["from"], label=f["label"], basis=f["basis"],
        period=f["period"], window=f["window"], metric=f["metric"],
        currency=f["currency"], open_problems=f["open_problems"], month=f["month"],
        measure=f["measure"], program_id=f["program_id"],
        broker_party_id=f["broker_party_id"], carrier_id=f["carrier_id"],
        dates=f["dates"], check=f["check"])
    return policy_search.search(
        s, principal, q=f["q"], program_id=f["program_id"],
        broker_party_id=f["broker_party_id"], carrier_id=f["carrier_id"], state=f["state"],
        date_field=f["date_field"], date_from=f["date_from"], date_to=f["date_to"],
        page=page, page_size=page_size, drill=drill)


@router.get("")
def policies_search(
    f: dict = Depends(_filters),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    principal: Principal = Depends(current_principal),
):
    """Search and filter the policies themselves — by policy number or insured
    (`q`), programme, broker (carrier seat), state, and a date range on the
    cover start, cover end or bordereau month (`date_field`). A broker's
    `carrier_id` narrows to one carrier, as the sidebar's carrier switch does
    on every broker screen.

    `from` names a number clicked on a report — `financial` (Financial
    Summary: basis, period, window, metric, currency, open_problems) or
    `monthly` (Monthly Report: month, measure; the Executive Report's any
    `dates` instead of a month, and `check` for one check's errors) — and keeps only the policies
    behind it; programme and broker narrow it exactly as they narrowed the
    number."""
    with SessionLocal() as s:
        return _select(s, principal, f, page, page_size)


@router.get("/detail")
def policy_detail(
    policy: str,
    insurer_id: int,
    program_id: Optional[int] = None,
    broker_party_id: Optional[int] = None,
    carrier_id: Optional[int] = None,
    principal: Principal = Depends(current_principal),
):
    """One policy of the search: every row of it in every current bordereau,
    newest month first, each with all of its columns."""
    with SessionLocal() as s:
        return policy_search.detail(
            s, principal, insurer_id=insurer_id, program_id=program_id,
            broker_party_id=broker_party_id, policy=policy, carrier_id=carrier_id)


@router.get("/file")
def policies_file(f: dict = Depends(_filters),
                  principal: Principal = Depends(current_principal)):
    """Every policy the filters select — all pages — as an Excel file, and a
    "Downloaded the policy list" row in the Audit Logs."""
    with SessionLocal() as s:
        result = _select(s, principal, f, page_size=None)
        try:
            name, data = pe.export(s, principal, result, f)
        except ValueError as e:
            raise HTTPException(400, str(e))
        s.commit()
    return Response(content=data, media_type=pe.XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/recipients")
def policies_recipients(principal: Principal = Depends(current_principal)):
    """Who the list can be emailed to: the seat's own logins (any other
    address is typed in)."""
    with SessionLocal() as s:
        return {"logins": pe.logins(s, policy_search.seat_of(s, principal))}


class SendBody(BaseModel):
    user_ids: Optional[list[int]] = None       # own logins ticked; None = all
    emails: Optional[list[str]] = None         # other addresses


@router.post("/send")
def policies_send(body: SendBody, f: dict = Depends(_filters),
                  principal: Principal = Depends(current_principal)):
    """Email every policy the filters select, as an Excel file. The filters
    come in the query, exactly as GET /policies takes them."""
    with SessionLocal() as s:
        result = _select(s, principal, f, page_size=None)
        try:
            r = pe.send(s, principal, result, f, user_ids=body.user_ids, emails=body.emails)
        except ValueError as e:
            raise HTTPException(400, str(e))
        s.commit()
        return r
