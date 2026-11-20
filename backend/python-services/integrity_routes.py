"""Integrity & Retention routes (record_integrity.py 52.1, retention.py 52.2).

  carrier / Kavachio  GET  /audit/integrity                 seals so far and the last check
                      POST /audit/integrity/check           work every fingerprint out again, now
                      GET  /audit/retention                 periods that apply, what the next run deletes, holds
                      GET  /audit/retention/deletions       what retention deleted
                      POST /audit/retention/holds           place a legal hold (the carrier, or one broker)
                      POST /audit/retention/holds/{id}/lift lift it, with a note
  Kavachio            PUT  /audit/retention/rules           set a period (a kind of record, a country or everywhere)
                      DELETE /audit/retention/rules/{id}    remove one

A carrier sees and holds only its own records; Kavachio sees every carrier, or
one with ?mga=, and holds need a carrier picked. Brokers have none of this.
Every write names itself in the audit log (audit._SELF_LOGGED).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import audit_history as ah
import record_integrity as ri
import retention as rt
from app_routes import _actor, resolve_tenant_id
from auth_deps import Principal, require_role
from carrier_scope import is_carrier_admin_seat
from db import ActivityEvent, SessionLocal

router = APIRouter(prefix="/audit", tags=["integrity"])

_reader = require_role("carrier_admin")
_kavachio = require_role("kavachio_admin")


class RuleIn(BaseModel):
    record_type: str
    country: Optional[str] = None
    months: Optional[int] = None
    note: Optional[str] = None


class HoldIn(BaseModel):
    reason: Optional[str] = None
    broker_party_id: Optional[int] = None


class LiftIn(BaseModel):
    note: Optional[str] = None


def _scope(s, principal: Principal, mga: Optional[str]) -> Optional[int]:
    """The carrier asked about; None for Kavachio looking at every carrier."""
    mga = ah.chosen_mga(mga)
    if principal.is_platform_admin and not mga:
        return None
    return resolve_tenant_id(s, principal, mga)


def _log(s, tid: Optional[int], principal: Principal, action: str, target: str, **details):
    from audit import actor_columns
    s.add(ActivityEvent(tenant_id=tid, actor=_actor(principal), action=action, target=target,
                        details=details, **actor_columns(principal)))


def _holder(s, principal: Principal, mga: Optional[str]) -> int:
    """Who may place or lift a hold: the carrier admin on its own records,
    Kavachio on a carrier it picked."""
    mga = ah.chosen_mga(mga)
    if principal.is_platform_admin:
        if not mga:
            raise HTTPException(400, "Pick a carrier first.")
        return resolve_tenant_id(s, principal, mga)
    if not is_carrier_admin_seat(s, principal):
        raise HTTPException(403, "Only the carrier can place or lift a legal hold.")
    return resolve_tenant_id(s, principal, mga)


@router.get("/integrity")
def integrity(mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    with SessionLocal() as s:
        return ri.summary(s, _scope(s, principal, mga))


@router.post("/integrity/check")
def integrity_check(mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    with SessionLocal() as s:
        return ri.check(s, _scope(s, principal, mga), by=_actor(principal), by_user_id=principal.user_id)


@router.get("/retention")
def retention(mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    with SessionLocal() as s:
        tid = _scope(s, principal, mga)
        out = rt.overview(s, tid)
        if out.get("ready"):
            out["holds"] = rt.holds(s, tid)
            out["can_set_periods"] = principal.is_platform_admin
            out["can_hold"] = (bool(ah.chosen_mga(mga)) if principal.is_platform_admin else is_carrier_admin_seat(s, principal))
            import audit_feed as af
            from db import Party
            ids = af.brokers_of_carrier(s, tid) if tid else set()
            out["brokers"] = sorted(({"id": i, "name": n} for i, n in
                                     s.query(Party.id, Party.legal_name).filter(Party.id.in_(ids or {0}))),
                                    key=lambda x: (x["name"] or "").lower())
        return out


@router.get("/retention/deletions")
def retention_deletions(mga: Optional[str] = None, page: int = 1, page_size: int = 50,
                        principal: Principal = Depends(_reader)):
    with SessionLocal() as s:
        if ri.missing_tables(s):
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        return rt.deletions(s, _scope(s, principal, mga), page, min(max(page_size, 1), 200))


@router.put("/retention/rules")
def save_rule(body: RuleIn, principal: Principal = Depends(_kavachio)):
    with SessionLocal() as s:
        ri.require_tables(s)
        r = rt.save_rule(s, principal.user_id, body.record_type, body.country, body.months, body.note)
        _log(s, None, principal, "retention_rule_saved", f"retention_rule:{r.id}",
             type=rt.RECORD_TYPES[r.record_type]["label"], country=r.country or "Everywhere",
             months=r.months or "Kept for good", reason=r.note)
        s.commit()
        return rt.overview(s, None)


@router.delete("/retention/rules/{rule_id}")
def remove_rule(rule_id: int, principal: Principal = Depends(_kavachio)):
    with SessionLocal() as s:
        ri.require_tables(s)
        r = rt.remove_rule(s, rule_id)
        _log(s, None, principal, "retention_rule_removed", f"retention_rule:{rule_id}",
             type=rt.RECORD_TYPES.get(r.record_type, {}).get("label"), country=r.country or "Everywhere")
        s.commit()
        return rt.overview(s, None)


@router.post("/retention/holds")
def place_hold(body: HoldIn, mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    with SessionLocal() as s:
        ri.require_tables(s)
        tid = _holder(s, principal, mga)
        h = rt.place_hold(s, tid, principal.user_id, body.reason, body.broker_party_id)
        _log(s, tid, principal, "legal_hold_placed", f"legal_hold:{h.id}", reason=h.reason,
             **({"broker_party_id": h.broker_party_id} if h.broker_party_id else {}))
        s.commit()
        return {"holds": rt.holds(s, tid)}


@router.post("/retention/holds/{hold_id}/lift")
def lift_hold(hold_id: int, body: LiftIn, mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    with SessionLocal() as s:
        ri.require_tables(s)
        tid = _holder(s, principal, mga)
        h = rt.lift_hold(s, tid, hold_id, principal.user_id, body.note)
        _log(s, tid, principal, "legal_hold_lifted", f"legal_hold:{h.id}", reason=h.release_note)
        s.commit()
        return {"holds": rt.holds(s, tid)}
