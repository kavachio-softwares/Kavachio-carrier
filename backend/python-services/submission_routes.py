"""Carrier-side controls for the broker exception loop.

  GET  /submissions/by-arrival/{arrival_id}   the file panel: reference, version,
                                              progress, and whether the broker
                                              was told (each message, its outcome)
  POST /submissions/{reference}/deliver       accept the file as it is — the
                                              carrier's move when the deadline
                                              rule kept a file on hold
  POST /submissions/{reference}/notify-again  send the broker the result again
  GET/PUT /programs/{program_id}/delivery-rule   what holds a file, and the deadline
                                                 (program.delivery_rule)
  GET/PUT /intake/routes/{route_id}/contacts     "notify these emails" on a channel
                                                 (intake_route.notify_emails)

The carrier still cannot answer an exception: only the broker can
(carrier_scope.assert_can_amend). Accepting a whole file at the deadline is a
delivery decision, not an amendment, and it is recorded with what was open.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import submission_service as svc
from app_routes import assert_tenant_owns
from auth_deps import Principal, current_principal, require_role

router = APIRouter(tags=["submissions"])

# Statuses where the broker still has something to do.
WAITING_ON_BROKER = ("with_exceptions", "held_at_deadline", "failed")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def notice_summary(items: list[dict]) -> Optional[dict]:
    """One line for the panel: was the broker told about the newest result?"""
    mail = [n for n in items if n["channel"] == "email"
            and n["event"] != "carrier_deadline"]
    if not mail:
        return None
    newest = mail[0]
    same = [n for n in mail if n["version"] == newest["version"]
            and n["event"] == newest["event"]]
    return {"event": newest["event"], "version": newest["version"],
            "sent": sum(1 for n in same if n["status"] == "sent"),
            "failed": sum(1 for n in same if n["status"] == "failed"),
            "queued": 0,
            "skipped": sum(1 for n in same if n["status"] == "skipped"),
            "recipients": [n["recipient"] for n in same],
            "at": newest["at"]}


def _thread_by_ref(s, ref: str, principal: Principal):
    th = svc.load(s, svc.find_reference(ref))
    if th is None:
        raise HTTPException(404, "not found")
    assert_tenant_owns(principal, th.tenant_id)
    return th


@router.get("/submissions/by-arrival/{arrival_id}")
def by_arrival(arrival_id: int, principal: Principal = Depends(current_principal)):
    from db import SessionLocal
    from intake_models import FileArrival
    with SessionLocal() as s:
        a = s.get(FileArrival, arrival_id)
        if a is None:
            raise HTTPException(404, "not found")
        assert_tenant_owns(principal, a.tenant_id)
        th, ver = svc.thread_for_arrival(s, arrival_id)
        if th is None or ver is None:
            return {"submission": None}
        doc = svc.status_json(s, th)
        items = svc.notices(s, th.ref)
        doc["this_file_version"] = ver.no
        # Each file version's own row, so the panel can open an earlier one —
        # Files Received lists a submission once, as its latest file.
        file_of = {f.version_no: f.id for f in th.files}
        for v in doc["versions"]:
            v["arrival_id"] = file_of.get(v["version"]) if v.get("is_file") else None
        doc["notifications"] = items
        doc["broker_notified"] = notice_summary(items)
        # The carrier's two moves, offered only when they can do something:
        # remind a broker who still owes an answer, and decide a file the
        # deadline rule left on hold.
        doc["can_notify"] = th.status in WAITING_ON_BROKER
        doc["can_deliver"] = th.status == "held_at_deadline"
        return {"submission": doc}


class DeliverBody(BaseModel):
    note: Optional[str] = None


@router.post("/submissions/{reference}/deliver")
def deliver_now(reference: str, body: DeliverBody = DeliverBody(),
                principal: Principal = Depends(require_role("carrier_admin"))):
    from db import AppUser, SessionLocal
    with SessionLocal() as s:
        th = _thread_by_ref(s, reference, principal)
        if th.status != "held_at_deadline":
            raise HTTPException(409, "A file can be accepted as it is once its "
                                     "correction deadline has passed.")
        u = s.get(AppUser, principal.user_id)
        res = svc.deliver(s, th, actor=(u.email if u else "carrier"), flagged=True)
        s.commit()
        return {"ok": True, **res}


@router.post("/submissions/{reference}/notify-again")
def notify_again(reference: str,
                 principal: Principal = Depends(require_role("carrier_admin"))):
    from db import SessionLocal
    with SessionLocal() as s:
        th = _thread_by_ref(s, reference, principal)
        if th.status not in WAITING_ON_BROKER:
            raise HTTPException(409, "The broker has nothing to act on for this file.")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        svc._queue_result_email(s, th, th.current, None, event="result",
                                suffix=f":again:{stamp}")
        s.commit()
        return {"ok": True}


# ── the programme's delivery rule (program.delivery_rule) ──────────────────

class RuleBody(BaseModel):
    hold_severities: Optional[list[str]] = None
    rule_overrides: Optional[dict] = None
    correction_days: Optional[int] = None
    deadline_action: Optional[str] = None


def _program(s, program_id: int, principal: Principal):
    from db import Program
    p = s.get(Program, program_id)
    if p is None:
        raise HTTPException(404, "not found")
    assert_tenant_owns(principal, p.tenant_id)
    return p


@router.get("/programs/{program_id}/delivery-rule")
def get_rule(program_id: int, principal: Principal = Depends(current_principal)):
    from db import SessionLocal
    with SessionLocal() as s:
        p = _program(s, program_id, principal)
        return {**svc.rule_for(s, program_id), "is_default": not p.delivery_rule,
                "severities": list(svc.SEVERITIES),
                "deadline_actions": list(svc.DEADLINE_ACTIONS)}


@router.put("/programs/{program_id}/delivery-rule")
def put_rule(program_id: int, body: RuleBody,
             principal: Principal = Depends(require_role("carrier_admin"))):
    from db import SessionLocal
    with SessionLocal() as s:
        p = _program(s, program_id, principal)
        rule = dict(p.delivery_rule or {})
        if body.hold_severities is not None:
            bad = [x for x in body.hold_severities if x not in svc.SEVERITIES]
            if bad:
                raise HTTPException(400, f"unknown severity: {', '.join(bad)}")
            rule["hold_severities"] = body.hold_severities
        if body.rule_overrides is not None:
            if any(v not in ("hold", "pass") for v in body.rule_overrides.values()):
                raise HTTPException(400, "a rule override is 'hold' or 'pass'")
            rule["rule_overrides"] = {str(k): v for k, v in body.rule_overrides.items()}
        if body.correction_days is not None:
            if not 1 <= body.correction_days <= 60:
                raise HTTPException(400, "correction days must be 1–60")
            rule["correction_days"] = body.correction_days
        if body.deadline_action is not None:
            if body.deadline_action not in svc.DEADLINE_ACTIONS:
                raise HTTPException(400, "deadline action must be deliver_flagged "
                                         "or keep_on_hold")
            rule["deadline_action"] = body.deadline_action
        p.delivery_rule = rule
        from audit import log_activity
        log_activity(p.tenant_id, None, "bdx_delivery_rule_updated",
                     target=f"program:{program_id}",
                     details={k: v for k, v in body.model_dump().items() if v is not None},
                     principal=principal)
        s.commit()
        return {**svc.rule_for(s, program_id), "is_default": False}


# ── "notify these emails" on a channel (intake_route.notify_emails) ────────

class ContactsBody(BaseModel):
    emails: list[str]


def _route(s, route_id: int, principal: Principal):
    from intake_models import IntakeRoute
    r = s.get(IntakeRoute, route_id)
    if r is None:
        raise HTTPException(404, "not found")
    assert_tenant_owns(principal, r.tenant_id)
    return r


@router.get("/intake/routes/{route_id}/contacts")
def get_contacts(route_id: int, principal: Principal = Depends(current_principal)):
    from db import SessionLocal
    with SessionLocal() as s:
        return {"emails": svc.route_contacts(_route(s, route_id, principal))}


@router.put("/intake/routes/{route_id}/contacts")
def put_contacts(route_id: int, body: ContactsBody,
                 principal: Principal = Depends(require_role("carrier_admin"))):
    from db import SessionLocal
    emails, seen = [], set()
    for e in body.emails:
        e = (e or "").strip()
        if not e:
            continue
        if not _EMAIL.match(e):
            raise HTTPException(400, f"'{e}' is not an email address")
        if e.lower() not in seen:
            seen.add(e.lower())
            emails.append(e)
    if len(emails) > 10:
        raise HTTPException(400, "at most 10 addresses per channel")
    with SessionLocal() as s:
        r = _route(s, route_id, principal)
        r.notify_emails = emails
        from audit import log_activity
        log_activity(r.tenant_id, None, "intake_route_contacts_updated",
                     target=f"route:{route_id}", details={"emails": emails},
                     principal=principal)
        s.commit()
        return {"emails": emails}
