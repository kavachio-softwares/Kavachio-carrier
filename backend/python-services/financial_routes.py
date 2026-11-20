"""Financial Summary & Trends (Phase 2, task 35) — the API behind the screen.

  GET /reports/financial-summary        totals per currency, overall, by
                                        programme and by broker, for a period,
                                        the period before and the same period
                                        last year
  GET /reports/financial-summary/rows   the bordereau rows behind those totals,
                                        each figure with its label
  GET /reports/financial-summary/file   the same screen as an Excel file
                                        (financial_export.py)
  GET /reports/financial-summary/add-together-fields
                                        the data-model fields whose columns
                                        the output template page offers to add
                                        together (the money figures)

For the carrier and broker seats only; policy_search.seat_of decides whose
files are read and refuses everyone else (Kavachio included), as on the
Policies screen. The field list is not data and is served to any signed-in
seat.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

import financial_export
import financial_figures
import financial_summary
from auth_deps import Principal, current_principal
from db import SessionLocal

router = APIRouter(prefix="/reports/financial-summary", tags=["reports"])


@router.get("/add-together-fields")
def financial_add_together_fields(principal: Principal = Depends(current_principal)):
    """Served rather than restated in the browser: the money figures, as
    data-model field ids."""
    return {"fields": [financial_figures.FIELDS[k][0] for k in financial_figures.MONEY]}


@router.get("")
def financial_summary_view(
    basis: str = "month",
    period: Optional[str] = None,
    program_id: Optional[int] = None,
    broker_party_id: Optional[int] = None,
    carrier_id: Optional[int] = None,
    open_problems: str = "include",
    principal: Principal = Depends(current_principal),
):
    """`basis` is month, quarter or year; `period` a label of that basis
    ("2027-03", "2027-Q1", "2027"), the latest with files when left out.
    `open_problems=exclude` leaves out files that still have open problems.
    A broker's `carrier_id` narrows to one carrier, as the sidebar's carrier
    switch does on every broker screen."""
    with SessionLocal() as s:
        return financial_summary.summary(
            s, principal, basis=basis, period=period, program_id=program_id,
            broker_party_id=broker_party_id, carrier_id=carrier_id,
            open_problems=open_problems)


@router.get("/rows")
def financial_summary_rows(
    basis: str = "month",
    period: Optional[str] = None,
    window: str = "current",
    program_id: Optional[int] = None,
    broker_party_id: Optional[int] = None,
    carrier_id: Optional[int] = None,
    open_problems: str = "include",
    currency: Optional[str] = None,
    status: Optional[str] = None,
    transaction: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(current_principal),
):
    """The rows behind one window's totals. `currency` is a currency code, or
    "none" / "review" for the rows whose currency is not stated / unclear;
    `status` keeps the rows where any figure is reported, calculated,
    needs_review or not_reported; `transaction` the rows of one transaction
    type as sent, or "none" for the rows that state none."""
    with SessionLocal() as s:
        return financial_summary.rows(
            s, principal, basis=basis, period=period, window=window,
            program_id=program_id, broker_party_id=broker_party_id,
            carrier_id=carrier_id, open_problems=open_problems,
            currency=currency, status=status, transaction=transaction,
            page=page, page_size=page_size)


# Every row behind a period goes in the file; an Excel sheet holds about a
# million, far more than one period's bordereaux.
_FILE_ROWS = 1_000_000


@router.get("/file")
def financial_summary_file(
    basis: str = "month",
    period: Optional[str] = None,
    program_id: Optional[int] = None,
    broker_party_id: Optional[int] = None,
    carrier_id: Optional[int] = None,
    open_problems: str = "include",
    principal: Principal = Depends(current_principal),
):
    """The screen, with the same period and filters, as an Excel file: the
    totals, by programme, by broker (carrier only), every bordereau row behind
    them and the Column Guide. Read through the same two functions the screen
    is served by, so the seat checks are theirs."""
    args = dict(basis=basis, period=period, program_id=program_id,
                broker_party_id=broker_party_id, carrier_id=carrier_id,
                open_problems=open_problems)
    with SessionLocal() as s:
        data = financial_summary.summary(s, principal, **args)
        rows = (financial_summary.rows(s, principal, window="current",
                                       page_size=_FILE_ROWS, **args)["items"]
                if data.get("period") else [])
    name = f"Financial Report {financial_export.period_name(data.get('period'))}.xlsx"
    return Response(
        content=financial_export.workbook(data, rows, data.get("seat") or ""),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
