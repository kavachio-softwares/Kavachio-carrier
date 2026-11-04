"""Policy Search (Phase 2, 43.1) — the API behind the Policies screen.

  GET /policies          one page of the policies in the processed bordereaux,
                         with the choices the screen's filters offer
  GET /policies/detail   every bordereau row behind one of them

For the carrier and broker seats only; policy_search.seat_of decides whose
policies come back and refuses everyone else (Kavachio included).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

import policy_search
from auth_deps import Principal, current_principal
from db import SessionLocal

router = APIRouter(prefix="/policies", tags=["policies"])


@router.get("")
def policies_search(
    q: Optional[str] = None,
    program_id: Optional[int] = None,
    broker_party_id: Optional[int] = None,
    carrier_id: Optional[int] = None,
    state: Optional[str] = None,
    date_field: str = "start",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    principal: Principal = Depends(current_principal),
):
    """Search and filter the policies themselves — by policy number or insured
    (`q`), programme, broker (carrier seat), state, and a date range on the
    cover start, cover end or bordereau month (`date_field`). A broker's
    `carrier_id` narrows to one carrier, as the sidebar's carrier switch does
    on every broker screen."""
    with SessionLocal() as s:
        return policy_search.search(
            s, principal, q=q, program_id=program_id,
            broker_party_id=broker_party_id, carrier_id=carrier_id, state=state,
            date_field=date_field, date_from=date_from, date_to=date_to,
            page=page, page_size=page_size)


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
