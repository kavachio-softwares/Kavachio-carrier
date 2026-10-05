"""The secure correction link — one lightweight page for ONE submission.

Not a portal and not a login. The link in the broker's email opens a page that
shows that one submission's exceptions in the broker's own terms. After a
one-time code is emailed to the same address, the broker can:

  * correct a value          → saved as a correction (kind 'fix')
  * keep a value as it is    → saved as an answer with a reason (kind 'approve')
  * Submit                   → the corrections become a NEW version, checked
                               again; delivery follows the programme's rule

Answers go through the SAME function the portal's exception screen uses
(validation_routes._decide_direct_lane → landing_correction), and Submit runs
the SAME render as the portal's Fix & Validate — into a new export, so the
version already sent and the original upload are never edited. Nothing here
touches approved policy data: auto_ingest stays off.

There is deliberately no upload, no list of other files and no settings: a
corrected FILE comes back through the broker's usual API, email or SFTP
channel quoting the reference.

Security, with nothing stored for the link itself (submission_service):
the link is signed with the app secret and expires in 14 days or as soon as
the submission is delivered; the code is derived from the link and the time
(good for 5–10 minutes); 5 wrong codes in 15 minutes lock the link for 15
minutes; at most one code a minute and ten per link; a verified session is
signed and lasts 30 minutes. Codes sent and wrong tries are activity_events
rows, which is what the limits count. Every refusal of the link itself says
the same "not valid" so a token cannot be probed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

import submission_service as svc

log = logging.getLogger("kavachio.fixlink")
router = APIRouter(prefix="/fix-link", tags=["fix-link"])

CODE_MINUTES = 10
SESSION_MINUTES = 30
MAX_TRIES = 5
LOCK_MINUTES = 15
CODE_COOLDOWN_S = 60
MAX_CODES = 10


def _gone() -> HTTPException:
    return HTTPException(404, "This link is not valid or has expired. Ask the "
                              "carrier for a new one, or send a corrected file "
                              "through your usual channel.")


# Where a link still OPENS. While a submitted version is being checked, and
# once a file is delivered, the page shows where it stands; anything that
# changes the file needs an open submission (_open).
_VIEWABLE = svc.OPEN_STATUSES + ("processing", "delivered", "delivered_flagged")


def _link(s, token: str) -> tuple[dict, "svc.Thread"]:
    doc = svc.read_link(token)
    th = svc.load(s, doc["r"]) if doc else None
    if th is None or th.status not in _VIEWABLE:
        raise _gone()
    return doc, th


def _open(th) -> None:
    if th.status not in svc.OPEN_STATUSES:
        raise HTTPException(409, "This file is not waiting for your review.")


def _session(s, token: str, session: Optional[str]) -> tuple[dict, "svc.Thread"]:
    doc, th = _link(s, token)
    if not svc.read_session(token, session):
        raise HTTPException(401, "Your session has ended. Ask for a new code.")
    return doc, th


def _audit(s, th, doc: dict, token: str, action: str, details: dict) -> None:
    from db import ActivityEvent
    s.add(ActivityEvent(
        tenant_id=th.tenant_id, actor=f"broker:{th.broker_party_id}",
        action=action, target=f"submission:{th.ref}",
        details={"reference": th.ref, "via": "secure link", "email": doc["e"],
                 "link": svc.link_id(token), **details},
        actor_broker_party_id=th.broker_party_id))


def _events(s, th, token: str, action: str, minutes: Optional[int] = None) -> list:
    """created_at of this link's `action` rows, newest first (UTC, naive —
    activity_events stamps datetime.utcnow)."""
    sql = ("SELECT created_at FROM activity_events WHERE target = :t "
           "AND action = :a AND CAST(details AS jsonb) ->> 'link' = :l")
    args = {"t": f"submission:{th.ref}", "a": action, "l": svc.link_id(token)}
    if minutes is not None:
        sql += " AND created_at >= :since"
        args["since"] = datetime.utcnow() - timedelta(minutes=minutes)
    return [r[0] for r in s.execute(text(sql + " ORDER BY created_at DESC"), args)]


def _locked(s, th, token: str) -> bool:
    return len(_events(s, th, token, "bdx_fix_link_code_wrong", LOCK_MINUTES)) >= MAX_TRIES


@router.get("/{token}")
def open_link(token: str):
    """What the page shows before the code: who it is for, nothing sensitive."""
    from db import SessionLocal
    from esign_otp import masked_email
    with SessionLocal() as s:
        doc, th = _link(s, token)
        names = svc._names(s, th)
        return {"reference": th.ref, "file": th.file_name, "programme": names["programme"],
                "carrier": names["carrier"], "broker": names["broker"],
                "period": th.period, "status": th.status,
                "status_text": svc.STATUS_WORDS.get(th.status, th.status),
                "email": masked_email(doc["e"]),
                "locked": _locked(s, th, token)}


@router.post("/{token}/code")
def send_code(token: str):
    from db import SessionLocal
    from esign_otp import masked_email
    with SessionLocal() as s:
        doc, th = _link(s, token)
        if _locked(s, th, token):
            raise HTTPException(429, "Too many wrong codes. Try again in 15 minutes.")
        sent = _events(s, th, token, "bdx_fix_link_code_sent")
        if sent and (datetime.utcnow() - sent[0]).total_seconds() < CODE_COOLDOWN_S:
            raise HTTPException(429, "A code was sent less than a minute ago.")
        if len(sent) >= MAX_CODES:
            raise HTTPException(429, "Too many codes for this link. Ask the carrier "
                                     "for a new link.")
        import time
        code = svc.code_for(token, int(time.time() // svc.CODE_WINDOW_S))
        # Sent straight away: the person is waiting on the page.
        try:
            from email_utils import send_email
            from html import escape
            send_email(doc["e"], f"Your verification code: {code}",
                       f"<p>Use this code to open the secure review for "
                       f"<b>{escape(th.file_name)}</b>:</p>"
                       f"<p style='font-size:26px;letter-spacing:6px;margin:16px 0'><b>{code}</b></p>"
                       f"<p style='color:#6b7280;font-size:13px'>Valid for {CODE_MINUTES} minutes. "
                       f"If you did not request it, you can ignore this email.</p>",
                       text=f"Your verification code: {code} (valid {CODE_MINUTES} minutes) "
                            f"for {th.file_name}.", account="NOTIFY")
        except Exception as exc:  # noqa: BLE001
            log.warning("code email for %s failed: %s", th.ref, exc)
            raise HTTPException(502, "We could not send the code just now. "
                                     "Please try again in a minute.")
        _audit(s, th, doc, token, "bdx_fix_link_code_sent", {})
        s.commit()
        return {"sent_to": masked_email(doc["e"]), "valid_minutes": CODE_MINUTES}


class CodeBody(BaseModel):
    code: str


@router.post("/{token}/verify")
def verify_code(token: str, body: CodeBody):
    from db import SessionLocal
    with SessionLocal() as s:
        doc, th = _link(s, token)
        if _locked(s, th, token):
            raise HTTPException(429, "Too many wrong codes. Try again in 15 minutes.")
        if not svc.check_code(token, body.code):
            _audit(s, th, doc, token, "bdx_fix_link_code_wrong", {})
            s.commit()
            raise HTTPException(400, "That code is not right, or it has expired.")
        _audit(s, th, doc, token, "bdx_fix_link_opened", {})
        s.commit()
        return {"session": svc.mint_session(token, SESSION_MINUTES),
                "valid_minutes": SESSION_MINUTES}


@router.get("/{token}/submission")
def get_submission(token: str, x_fix_session: Optional[str] = Header(default=None)):
    from db import SessionLocal
    with SessionLocal() as s:
        _, th = _session(s, token, x_fix_session)
        doc = svc.status_json(s, th, include_exceptions=True)
        doc["can_submit"] = th.status in svc.OPEN_STATUSES
        doc["drafts"] = _draft_count(s, th)
        return doc


def _draft_count(s, th) -> int:
    """Answers saved on the link since the current version was checked — what
    Submit will use. Read from the link's own audit rows (UTC, naive)."""
    from db import ActivityEvent, OutputExport
    out = s.get(OutputExport, th.current_export_id) if th.current_export_id else None
    since = out.created_at if out is not None else None
    q = (s.query(ActivityEvent)
         .filter(ActivityEvent.action == "bdx_fix_link_answers",
                 ActivityEvent.target == f"submission:{th.ref}"))
    if since is not None:
        q = q.filter(ActivityEvent.created_at >= since)
    return sum(int((ev.details or {}).get("saved") or 0) for ev in q.all())


class Answer(BaseModel):
    key: str                       # sheet|row|field of the exception
    action: str                    # correct | keep
    value: Optional[str] = None
    reason: Optional[str] = None


class AnswersBody(BaseModel):
    answers: list[Answer]


@router.post("/{token}/answers")
def save_answers(token: str, body: AnswersBody,
                 x_fix_session: Optional[str] = Header(default=None)):
    """Save answers. Nothing changes in any file until Submit makes a new version."""
    from db import OutputExport, SessionLocal
    from validation_routes import (ExportDecideRequest, ExportDecisionItem,
                                   _decide_direct_lane)
    with SessionLocal() as s:
        doc, th = _session(s, token, x_fix_session)
        _open(th)
        out = s.get(OutputExport, th.current_export_id) if th.current_export_id else None
        landing_id = th.current.landing_id
        if out is None or landing_id is None:
            raise HTTPException(409, "This file cannot be corrected here. Send a "
                                     "corrected file through your usual channel.")
        by_key = {svc.exception_key(e): e for e in svc._decorated(out)}
        items, problems = [], []
        for a in body.answers:
            e = by_key.get(a.key)
            if e is None:
                problems.append({"key": a.key, "reason": "not an exception on this version"})
                continue
            act = (a.action or "").lower()
            if act == "correct":
                if not (a.value or "").strip():
                    problems.append({"key": a.key, "reason": "a corrected value is needed"})
                    continue
                kind = "fix"
            elif act == "keep":
                if not (a.reason or "").strip():
                    problems.append({"key": a.key,
                                     "reason": "say why the value is right as it is"})
                    continue
                kind = "approve"
            else:
                problems.append({"key": a.key, "reason": f"unknown action '{a.action}'"})
                continue
            items.append(ExportDecisionItem(
                rule_id=e.get("rule_id"), policy_number=e.get("policy_number"),
                field=svc._field_of(e), kind=kind,
                value=(a.value or "").strip() if kind == "fix" else None,
                reason=((a.reason or "").strip() or None),
                actual_value=None if e.get("actual_value") is None
                else str(e.get("actual_value")),
                sheet=e.get("sheet"), row=int(e["row"]) if e.get("row") is not None else None))
        res = {"updated": 0, "skipped": []}
        if items:
            # The portal's own decide code, acting for the broker company with
            # the person's email as the decider (there is no login here).
            who = {"decided_by_user_id": None, "decided_by_role": "broker_admin",
                   "decided_by_broker_party_id": th.broker_party_id}
            res = _decide_direct_lane(
                s, int(landing_id), ExportDecideRequest(decisions=items), None,
                exp={"id": out.id, "tenant_id": out.tenant_id,
                     "program_id": out.program_id, "broker_party_id": out.broker_party_id},
                who=who, decided_by=f"link:{doc['e']}")
            _audit(s, th, doc, token, "bdx_fix_link_answers",
                   {"saved": res.get("updated"), "version": th.current.no})
            s.commit()
        return {"saved": res.get("updated", 0),
                "skipped": problems + list(res.get("skipped") or [])}


# ── the portal's own BDX review, served through the link ────────────────────
#
# The page shows the same "View BDX" grid and rule cards as the portal's
# exception screen (BdxInlineReview / ExceptionCards). These three endpoints are
# the portal's own handlers for the CURRENT version's export, called as the
# broker company the link was sent for — the same read rule a broker seat gets
# (carrier_scope.assert_can_read_export) and the same decide code. Only that
# one export is reachable: the id never comes from the request.

def _as_broker(th):
    """The broker company this link speaks for. No user: user_id 0 is no one."""
    from auth_deps import Principal
    return Principal(user_id=0, tenant_id=None, role="broker_admin",
                     broker_party_id=th.broker_party_id)


@router.get("/{token}/export")
def get_export(token: str, x_fix_session: Optional[str] = Header(default=None)):
    """The current version's export with its exceptions — what the portal's
    GET /export/downloads/{id} returns to the broker."""
    from db import SessionLocal
    import main as m
    with SessionLocal() as s:
        _, th = _session(s, token, x_fix_session)
        export_id = th.current_export_id
    if export_id is None:
        raise HTTPException(409, "This version has no checked file yet.")
    return m.export_download_get(export_id, principal=_as_broker(th))


@router.get("/{token}/data/stream")
def stream_rows(token: str, marks: bool = False, sheet: Optional[str] = None,
                chunk: Optional[int] = None, delay_ms: int = 0, offset: int = 0,
                max_rows: Optional[int] = None, row_gis: Optional[str] = None,
                meta: bool = True, x_fix_session: Optional[str] = Header(default=None)):
    """The grid's rows, exactly as GET /export/downloads/{id}/data/stream."""
    from db import SessionLocal
    import main as m
    with SessionLocal() as s:
        _, th = _session(s, token, x_fix_session)
        export_id = th.current_export_id
    if export_id is None:
        raise HTTPException(409, "This version has no checked file yet.")
    return m.export_download_data_stream(
        export_id, marks=marks, sheet=sheet, chunk=chunk, delay_ms=delay_ms,
        offset=offset, max_rows=max_rows, row_gis=row_gis, meta=meta,
        principal=_as_broker(th))


@router.post("/{token}/decide")
def decide(token: str, body: dict,
           x_fix_session: Optional[str] = Header(default=None)):
    """Approve / Fix / Dismiss from the grid or a rule — the portal's decide
    code for a direct-lane export. Nothing changes in any file until Submit
    makes the next version."""
    from db import OutputExport, SessionLocal
    from validation_routes import ExportDecideRequest, _decide_direct_lane
    try:
        req = ExportDecideRequest(**body)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Those decisions could not be read.")
    bad = [d.kind for d in req.decisions
           if (d.kind or "").lower() not in ("approve", "fix", "dismiss")]
    if bad:
        raise HTTPException(400, "A decision is Approve, Fix or Dismiss.")
    with SessionLocal() as s:
        doc, th = _session(s, token, x_fix_session)
        _open(th)
        out = s.get(OutputExport, th.current_export_id) if th.current_export_id else None
        landing_id = th.current.landing_id
        if out is None or landing_id is None:
            raise HTTPException(409, "This file cannot be corrected here. Send a "
                                     "corrected file through your usual channel.")
        who = {"decided_by_user_id": None, "decided_by_role": "broker_admin",
               "decided_by_broker_party_id": th.broker_party_id}
        res = _decide_direct_lane(
            s, int(landing_id), req, None,
            exp={"id": out.id, "tenant_id": out.tenant_id,
                 "program_id": out.program_id, "broker_party_id": out.broker_party_id},
            who=who, decided_by=f"link:{doc['e']}")
        _audit(s, th, doc, token, "bdx_fix_link_answers",
               {"saved": res.get("updated"), "version": th.current.no})
        s.commit()
        return {"ok": True, **res}


def _last(s, th, action: str):
    from db import ActivityEvent
    from sqlalchemy import func
    return (s.query(func.max(ActivityEvent.created_at))
            .filter(ActivityEvent.action == action,
                    ActivityEvent.target == f"submission:{th.ref}").scalar())


@router.post("/{token}/validate")
async def validate(token: str, x_fix_session: Optional[str] = Header(default=None)):
    """Check the saved decisions against every rule before they become a
    version — the render's own self-check, kept nowhere."""
    from db import SessionLocal
    with SessionLocal() as s:
        doc, th = _session(s, token, x_fix_session)
        _open(th)
        if _draft_count(s, th) == 0:
            raise HTTPException(400, "Resolve at least one exception first.")
        ref, email = th.ref, doc["e"]
    try:
        return await svc.validate_corrections(ref, actor=f"Secure link ({email})")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/{token}/submit")
async def submit(token: str, x_fix_session: Optional[str] = Header(default=None)):
    """Make the next version from the saved answers and check it again."""
    from db import SessionLocal
    with SessionLocal() as s:
        doc, th = _session(s, token, x_fix_session)
        _open(th)
        if _draft_count(s, th) == 0:
            raise HTTPException(400, "Resolve at least one exception first.")
        # Validated, and nothing decided since: what is submitted is what was checked.
        checked, answered = _last(s, th, "bdx_fix_link_validated"), _last(s, th, "bdx_fix_link_answers")
        if checked is None or (answered is not None and answered > checked):
            raise HTTPException(409, "Validate your changes before submitting.")
        ref, email = th.ref, doc["e"]
        _audit(s, th, doc, token, "bdx_fix_link_submitted", {"from_version": th.current.no})
        s.commit()
    # Sent like a file on the broker's own channel: intake checks, Files
    # Received, the auto-run, the result email. The page follows it until done.
    from starlette.concurrency import run_in_threadpool
    try:
        out = await run_in_threadpool(svc.submit_corrections, ref, email)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    out["can_submit"] = False
    out["drafts"] = 0
    return out
