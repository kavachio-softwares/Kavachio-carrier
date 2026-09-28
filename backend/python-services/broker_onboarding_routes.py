"""
Broker onboarding approval — the carrier admin's say on WHO the carrier works
with, taken before the broker is told anything at all.

WHY THIS EXISTS
---------------
Putting a broker on a programme is the act that lets them produce, and a
carrier USER could do it outright. Two things happened the instant they
clicked: the broker organisation and its admin login were created, and an email
went out inviting that person in. By the time the carrier admin could have had
an opinion there was nothing left to decide — an invitation cannot be un-sent.

So the ask now comes first. A carrier user's broker waits here; nothing is
created and nothing is mailed until the carrier admin answers. Reject it and
the broker never learns they were considered, because there was never anything
to learn about.

A carrier ADMIN adding a broker does not pass through here at all. Their own
act IS the approval — there is nobody left to ask — and their path is
byte-for-byte what it was before this file existed.

TWO GATES, IN A ROW, ANSWERING DIFFERENT QUESTIONS
--------------------------------------------------
    this one            WHO do we work with?     → releases the invitation
    Bordereau Setup     WHAT may they send us?   → releases the programme

The setup gate (migration 27) is untouched. Approving a broker here writes
their programme link at `pending_approval`, exactly as the carrier user's own
add always did, and it is still the approved Bordereau Setup that puts it live
(direct_routes._activate_links_for). So an approved broker gains a
relationship and an invitation — not a live programme. The broker still sees
nothing of the programme, its contracts or its BDX template until the setup
built on top is approved.

A REQUEST HOLDS AN INTENTION, NOT A THING
-----------------------------------------
No party, no app_user, no carrier_broker row, no program_broker row and no
invitation exists while a request waits. That is what makes "a rejected request
can never mail anybody" a fact about the data rather than a promise about the
code. Approving one calls the ORDINARY onboarding in hierarchy_routes —
_do_broker_onboarding and _do_programme_link, the same functions the carrier
admin's own buttons call — so the flow after approval is not reimplemented
here and cannot drift from it.

WHAT A REQUEST NEVER REVEALS
----------------------------
Whether the email belongs to a broker who already works with another carrier is
decided at APPROVAL, by check_broker_onboarding, which is the same code and the
same words as before. The request row keeps only what the carrier user typed.
The rule from migration 19 — a carrier never learns whose broker this is —
therefore survives the extra step intact, and the refusals a carrier user used
to see the moment they typed an address they still see then, because that
function is also run when the request is raised.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func

from db import (
    AppUser, BrokerOnboardingRequest, Party, Program, ProgramBroker, SessionLocal,
)
from auth_deps import Principal, require_role
from carrier_scope import LINK_PENDING, assert_can_invite_brokers, is_carrier_admin_seat
from app_routes import _carrier_seat, _iso_utc, resolve_tenant_id

router = APIRouter()

PENDING = "pending"


# =============================================================================
#  Raising one — called from hierarchy_routes, where the two buttons live
# =============================================================================
#
# These are not endpoints. `POST /brokers` and `POST /programs/{id}/brokers`
# keep their URLs and keep answering both seats, because the intent a carrier
# user is expressing is the same one the admin expresses — "this broker belongs
# with us" — and which of the two it becomes is the server's business, not the
# screen's. One button, as with pipeline_activate.
#
# Neither commits a session it was not given. Both are handed the open session
# from the route and commit it, because the request row is the whole of what
# happens and there is nothing to keep it company.


def _notify_admin(tid: int, principal: Principal, what: str,
                  prog_name: Optional[str], email: Optional[str]) -> None:
    """Tell the carrier admin a broker is waiting on them.

    Email only, for the same reason the setup submission is email only: the
    in-app PlatformNotification feed is Kavachio's own and is read cross-tenant
    by platform admins, so a row there would be invisible to the one person
    this is for. Their in-app equivalent is the queue this file serves, plus
    the activity row the bell reads.

    Never raises. A request must not fail to be recorded because mail did not
    go out — the queue is the record, and the mail is a courtesy on top of it.
    """
    try:
        from notifications import (CARRIER_ADMIN_FOOTER,
                                   carrier_admin_recipients, notify_people)
        who = _actor_name(principal)
        facts = [("Broker", what),
                 ("Programme", prog_name or "No programme yet"),
                 ("Asked by", who)]
        if email:
            facts.append(("Their admin", email))
        notify_people(
            carrier_admin_recipients(tid),
            f"{who} wants to bring a broker on board",
            body=("Nothing has been sent to them. They will not hear from "
                  "Kavachio at all unless you approve this."),
            facts=facts,
            link_path="/brokers/requests",
            link_label="Review it",
            action="Approve or turn it down",
            subject=f"Approval needed: {what}",
            footer=CARRIER_ADMIN_FOOTER)
    except Exception:  # noqa: BLE001
        pass


def _guard_duplicate(s, tid: int, program_id: Optional[int],
                     party_id: Optional[int], email: Optional[str]) -> None:
    """Refuse a second ask about the same broker while the first is unanswered.

    Facts about THIS carrier's own book, so they can be stated plainly — and
    they have to be: without this, a carrier user who clicked twice, or two
    colleagues who both wanted the same broker, would leave the admin two
    identical rows and no way to tell which one to answer.

    Matched on the EMAIL rather than the programme for an invitation, because
    the thing being asked for is the broker — a second request naming a second
    programme still wants the same person mailed once.
    """
    q = (s.query(BrokerOnboardingRequest)
         .filter(BrokerOnboardingRequest.tenant_id == tid,
                 BrokerOnboardingRequest.status == PENDING))
    if party_id is not None:
        row = q.filter(BrokerOnboardingRequest.broker_party_id == party_id,
                       BrokerOnboardingRequest.program_id == program_id).first()
        if row:
            raise HTTPException(409, {
                "message": "Somebody here has already asked your carrier admin "
                           "to put that broker on this programme. It is "
                           "waiting on them.",
                "errors": {"broker_party_id": "already requested"}})
        return
    row = q.filter(func.lower(BrokerOnboardingRequest.email)
                   == (email or "").lower()).first()
    if row:
        raise HTTPException(409, {
            "message": f"Somebody here has already asked your carrier admin to "
                       f"bring {email} on board. It is waiting on them.",
            "errors": {"admin_email": "already requested"}})


def raise_request_for_existing_broker(s, tid: int, program_id: int, party: Party,
                                      principal: Principal) -> dict:
    """A carrier user wants a broker ALREADY IN THE DIRECTORY on a programme.

    No email is ever sent for one of these, before or after approval — the
    broker is on board already, and this only decides a programme. It still
    needs the admin's answer, because the programme is what lets them produce.

    The "already on this programme" refusal is checked here as well as in
    _do_programme_link, so a carrier user is told now rather than having their
    admin discover it.
    """
    existing = (s.query(ProgramBroker)
                .filter(ProgramBroker.program_id == program_id,
                        ProgramBroker.broker_party_id == party.id)
                .first())
    if existing and existing.status in ("active", LINK_PENDING):
        raise HTTPException(409, f"{party.legal_name} is already on this programme")
    _guard_duplicate(s, tid, program_id, party.id, None)

    req = BrokerOnboardingRequest(
        tenant_id=tid, program_id=program_id, broker_party_id=party.id,
        status=PENDING, requested_by_user_id=principal.user_id,
        requested_at=dt.datetime.now(dt.timezone.utc))
    s.add(req)
    s.flush()
    prog = s.get(Program, program_id) if program_id else None
    prog_name = getattr(prog, "name", None)
    rid, name = req.id, party.legal_name
    _log_request(s, tid, principal, req, "broker_request_submitted",
                 broker_name=name, program_name=prog_name)
    s.commit()

    _notify_admin(tid, principal, name, prog_name, None)
    return {
        "ok": True, "pending": True, "request_id": rid,
        "reactivated": False, "link_id": None, "status": PENDING,
        "message": f"{name} has gone to your carrier admin to approve. "
                   f"Nothing is sent to the broker until they do.",
    }


def raise_request_to_invite(s, tid: int, body, name: str, email: str,
                            principal: Principal) -> dict:
    """A carrier user wants to INVITE a broker — new to us, or new to the
    platform entirely.

    The refusals run now (check_broker_onboarding, called by the route before
    us in the admin's case and here in the user's), so the carrier user learns
    about a taken address or a duplicate organisation name at the moment they
    type it, exactly as they did before. They run AGAIN at approval, because a
    colleague may have invited the same address in the meantime.

    Nothing is created. Not the party, not the login, not the invitation, and
    above all not the email.
    """
    from hierarchy_routes import check_broker_onboarding
    check_broker_onboarding(s, tid, name, email)
    _guard_duplicate(s, tid, body.program_id, None, email)

    req = BrokerOnboardingRequest(
        tenant_id=tid, program_id=body.program_id, broker_party_id=None,
        email=email, org_name=name, party_type=body.party_type,
        admin_name=(body.admin_name or "").strip() or None,
        status=PENDING, requested_by_user_id=principal.user_id,
        requested_at=dt.datetime.now(dt.timezone.utc))
    s.add(req)
    s.flush()
    prog = s.get(Program, body.program_id) if body.program_id else None
    prog_name = getattr(prog, "name", None)
    rid = req.id
    _log_request(s, tid, principal, req, "broker_request_submitted",
                 broker_name=name, program_name=prog_name)
    s.commit()

    _notify_admin(tid, principal, name, prog_name, email)
    # `invited` is FALSE and says so. The old response said "Invitation sent to
    # …", and repeating that here would be the one lie this whole feature
    # exists to stop telling.
    return {
        "ok": True, "pending": True, "invited": False, "email": email,
        "request_id": rid,
        "message": f"{name} has gone to your carrier admin to approve. "
                   f"Nothing is sent to {email} until they do.",
    }


# =============================================================================
#  Audit
# =============================================================================
#
# POST /brokers and POST /programs/{id}/brokers are in audit._SELF_LOGGED, so
# the middleware writes nothing for either and these are the only records. They
# have to be: the same POST /brokers is an onboarding from the carrier admin
# and a REQUEST from a carrier user, and a path-keyed name would put "added a
# broker" in the trail on a day nobody added one.

def _actor_email(principal) -> Optional[str]:
    try:
        from audit import actor_email
        return actor_email(principal.user_id) if principal is not None else None
    except Exception:  # noqa: BLE001
        return None


def _actor_name(principal) -> str:
    """Who to name in something a PERSON reads. The email stays the actor of
    record on audit rows; this is presentation only."""
    try:
        if principal is not None and principal.user_id:
            with SessionLocal() as s:
                u = s.get(AppUser, principal.user_id)
                nm = ((u.full_name or "").strip() if u else "") or None
                if nm:
                    return nm
    except Exception:  # noqa: BLE001
        pass
    return _actor_email(principal) or "A colleague"


def _request_details(req: BrokerOnboardingRequest, broker_name: Optional[str],
                     program_name: Optional[str],
                     note: Optional[str] = None) -> dict:
    d = {
        "request_id": req.id,
        # The bell needs a name to render, and it must be the ORGANISATION —
        # never the broker admin's address, which would put a person's email in
        # a feed their own colleagues read.
        "name": broker_name or req.org_name or f"Request {req.id}",
        "program_id": req.program_id,
        "program_name": program_name,
        "broker_name": broker_name or req.org_name,
        "invite": req.broker_party_id is None,
    }
    if note:
        d["note"] = note
    return d


def _log_request(s, tid: int, principal, req: BrokerOnboardingRequest,
                 action: str, *, broker_name: Optional[str] = None,
                 program_name: Optional[str] = None,
                 note: Optional[str] = None) -> None:
    """One activity row for a request or a decision on one.

    Best-effort, like every other audit call in this codebase: a broker must
    not fail to be requested because the trail could not be written."""
    try:
        from audit import log_activity
        log_activity(tid, _actor_email(principal), action,
                     target=f"broker-onboarding-request:{req.id}",
                     details=_request_details(req, broker_name, program_name, note),
                     principal=principal)
    except Exception:  # noqa: BLE001
        pass


# =============================================================================
#  The queue
# =============================================================================

def _row(req: BrokerOnboardingRequest, progs: dict, parties: dict,
         people: dict) -> dict:
    party = parties.get(req.broker_party_id) if req.broker_party_id else None
    asked_by = people.get(req.requested_by_user_id)
    decided_by = people.get(req.decided_by_user_id) if req.decided_by_user_id else None
    return {
        "id": req.id,
        "status": req.status,
        # Which of the two shapes this is. The screen needs it: an invitation
        # shows the address it will go to, a directory broker has none.
        "kind": "invite" if req.broker_party_id is None else "existing",
        "program_id": req.program_id,
        "programme": progs.get(req.program_id) if req.program_id else None,
        "broker_party_id": req.broker_party_id,
        # The organisation. For an existing broker that is their real name; for
        # an invitation it is what the carrier user typed, which is all anyone
        # here knows about them yet.
        "broker_name": (party.legal_name if party else None) or req.org_name,
        "party_type": (party.party_type if party else None) or req.party_type,
        "onboarding_status": getattr(party, "onboarding_status", None),
        # Who would be mailed, and under what name. None for an existing
        # broker — nothing is ever sent for one of those.
        "admin_email": req.email,
        "admin_name": req.admin_name,
        "requested_by": asked_by,
        "requested_at": _iso_utc(req.requested_at or req.created_at),
        "decided_by": decided_by,
        "decided_at": _iso_utc(req.decided_at),
        "reason": req.reason,
    }


@router.get("/broker-onboarding-requests")
def broker_onboarding_requests(
        status: Optional[str] = Query(None),
        page: Optional[int] = Query(None, ge=1),
        page_size: Optional[int] = Query(None, ge=1, le=200),
        principal: Principal = Depends(require_role("carrier_admin"))):
    """The queue. What it contains depends on the seat, as everywhere else here.

      carrier admin   every request at this carrier — they answer them all
      carrier user    their own, so they can see what they asked for and read
                      why something was turned down

    A carrier user is shown this rather than refused it because the rejection
    reason has to reach them, and the screen they raised the request from is
    not where they will be when the answer comes.

    `status` filters. On top of the four real states it takes one derived
    value, `answered` — everything that is not waiting. The screen draws the
    waiting ones and the answered ones as two tables, and each pages on its
    own; without this the second table would have to be cut out of a page of
    the first, which is the one thing server paging cannot do.

    Pagination is OPT-IN: omit `page` and it returns everything, newest first,
    exactly as before. `pending` is always the WHOLE queue's waiting count —
    not the page's and not the filter's — because it is what the dashboard
    tile and this screen's heading both state.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)
        base = (s.query(BrokerOnboardingRequest)
                .filter(BrokerOnboardingRequest.tenant_id == tid))
        if _carrier_seat(s, principal) == "user":
            base = base.filter(
                BrokerOnboardingRequest.requested_by_user_id == principal.user_id)
        # Counted off the SEAT'S whole queue, before any status filter and
        # before the page is cut.
        pending_total = base.filter(
            BrokerOnboardingRequest.status == PENDING).count()

        q = base
        want = (status or "").strip().lower()
        if want == "answered":
            q = q.filter(BrokerOnboardingRequest.status != PENDING)
        elif want:
            q = q.filter(BrokerOnboardingRequest.status == want)

        total = q.order_by(None).count()
        ordered = q.order_by(BrokerOnboardingRequest.id.desc())
        if page is not None:
            size = page_size or 10
            ordered = ordered.offset((page - 1) * size).limit(size)
        rows = ordered.all()
        if not rows:
            return {"items": [], "pending": pending_total, "total": total}

        progs = {p.id: p.name for p in s.query(Program).filter(
            Program.id.in_([r.program_id for r in rows if r.program_id] or [-1])).all()}
        parties = {p.id: p for p in s.query(Party).filter(
            Party.id.in_([r.broker_party_id for r in rows
                          if r.broker_party_id] or [-1])).all()}
        ids = {r.requested_by_user_id for r in rows} | {
            r.decided_by_user_id for r in rows if r.decided_by_user_id}
        people = {
            u.id: {"id": u.id, "full_name": u.full_name, "email": u.email}
            for u in s.query(AppUser).filter(AppUser.id.in_(ids or {-1})).all()}
        items = [_row(r, progs, parties, people) for r in rows]
        return {"items": items, "pending": pending_total, "total": total}


# =============================================================================
#  Deciding
# =============================================================================

def _open_request(s, request_id: int, principal: Principal
                  ) -> BrokerOnboardingRequest:
    """The request this carrier admin may decide, or the right refusal.

    404 BEFORE 403, deliberately: which requests exist at another carrier is
    not something a 403 should confirm to somebody who cannot see them. Within
    the carrier the order flips — a carrier user is entitled to know the
    request exists and is simply not theirs to answer, and "no such request"
    would send them looking for a bug.
    """
    # Kavachio can read who a carrier works with; deciding it is the carrier's.
    # The same rule as sending the invitation (assert_can_invite_brokers) —
    # approving one IS sending it. First, because it refuses a seat that is not
    # a carrier at all, so it gives nothing away about this carrier's requests.
    assert_can_invite_brokers(principal)
    req = s.get(BrokerOnboardingRequest, request_id)
    tid = resolve_tenant_id(s, principal)
    if not req or req.tenant_id != tid:
        raise HTTPException(404, "request not found")
    if not is_carrier_admin_seat(s, principal):
        raise HTTPException(
            403, "Only your organisation's carrier admin can decide on a "
                 "broker onboarding request.")
    if req.status != PENDING:
        raise HTTPException(409, {
            "message": f"That request was already {req.status}.",
            "errors": {"request": req.status}})
    return req


def _notify_requester(requested_by: Optional[int], principal: Principal,
                      broker_name: str, *, approved: bool,
                      reason: Optional[str] = None) -> None:
    """Tell the colleague who asked what was decided. Never raises."""
    try:
        from notifications import (CARRIER_USER_FOOTER, notify_people,
                                   user_recipients)
        who = _actor_name(principal)
        facts = [("Broker", broker_name), ("Decided by", who)]
        if reason:
            facts.append(("Reason", reason))
        notify_people(
            user_recipients(requested_by),
            (f"{who} approved {broker_name}" if approved
             else f"{who} turned down {broker_name}"),
            body=("They have been invited. You can carry on with their "
                  "contract and bordereau setup."
                  if approved else
                  "Nothing was sent to them, so nobody outside your "
                  "organisation knows this was asked."),
            facts=facts,
            link_path="/brokers/requests",
            link_label="Open the request",
            subject=(f"Approved: {broker_name}" if approved
                     else f"Turned down: {broker_name}"),
            footer=CARRIER_USER_FOOTER)
    except Exception:  # noqa: BLE001
        pass


class DecisionBody(BaseModel):
    reason: Optional[str] = None


@router.post("/broker-onboarding-requests/{request_id}/approve")
def broker_onboarding_approve(request_id: int,
                              principal: Principal = Depends(
                                  require_role("carrier_admin"))):
    """Approve it: onboard the broker and send the invitation, now.

    THIS IS WHERE THE EXISTING FLOW RESUMES, and it resumes by calling the
    existing functions — _do_broker_onboarding and _do_programme_link in
    hierarchy_routes, the same two the carrier admin's own buttons call. Not a
    copy of them. Whatever those do, an approved request does.

    Everything is re-checked against the world as it is NOW, not as it was when
    the request was raised: the programme may have been archived, the broker
    deactivated, the email claimed by somebody else. A week-old request is a
    question, not a reservation.

    THE LINK IS WRITTEN `pending_approval`, NOT LIVE. This gate decides who the
    carrier works with; the Bordereau Setup gate decides what they may send,
    and it is untouched. Recorded under the REQUESTER's id, not the approving
    admin's — they are the reason it exists, and _activate_links_for cares only
    that it is waiting.
    """
    from hierarchy_routes import (NewBrokerBody, _assert_broker,
                                  _assert_programme, _do_broker_onboarding,
                                  _do_programme_link)
    send_mail = None
    with SessionLocal() as s:
        req = _open_request(s, request_id, principal)
        tid = req.tenant_id
        prog = (_assert_programme(s, req.program_id, principal, tid)
                if req.program_id else None)

        if req.broker_party_id is not None:
            # A broker out of the directory: only the programme link is wanted,
            # and no mail is sent for one of these, now or ever.
            party = _assert_broker(s, req.broker_party_id, tid)
            broker_name = party.legal_name
            _do_programme_link(s, tid, req.program_id, party,
                               req.requested_by_user_id, LINK_PENDING)
        else:
            body = NewBrokerBody(
                legal_name=req.org_name or "",
                party_type=req.party_type or "broker",
                admin_name=req.admin_name,
                admin_email=req.email,
                program_id=req.program_id)
            broker_name = req.org_name or (req.email or f"Request {req.id}")
            _result, send_mail, ours = _do_broker_onboarding(
                s, tid, body, (req.org_name or "").strip(),
                (req.email or "").strip().lower(), req.requested_by_user_id)
            # A broker WE just created can go on the programme at once — the
            # carrier user needs it there to build the contract, and that is
            # what happened before this gate existed. One who already had a
            # login has agreed to nothing yet, so their link waits for their
            # own Accept (broker_routes._accept_invitation), which the
            # invitation's program_id drives exactly as it does for the admin.
            if ours and req.program_id:
                _do_programme_link(s, tid, req.program_id,
                                   s.get(Party, ours),
                                   req.requested_by_user_id, LINK_PENDING)

        req.status = "approved"
        req.decided_by_user_id = principal.user_id
        req.decided_at = dt.datetime.now(dt.timezone.utc)
        _log_request(s, tid, principal, req, "broker_request_approved",
                     broker_name=broker_name,
                     program_name=getattr(prog, "name", None))
        s.commit()
        out = _row(req, {req.program_id: getattr(prog, "name", None)}, {}, {})
        out["broker_name"] = broker_name
        requested_by = req.requested_by_user_id

    # After the commit, and only then. An invitation the broker has been told
    # about and the database has not is the one combination with no way back.
    if send_mail:
        send_mail()
    _notify_requester(requested_by, principal, broker_name, approved=True)
    return out


@router.post("/broker-onboarding-requests/{request_id}/reject")
def broker_onboarding_reject(request_id: int,
                             body: DecisionBody = DecisionBody(),
                             principal: Principal = Depends(
                                 require_role("carrier_admin"))):
    """Turn it down. A reason is REQUIRED.

    A request returned with nothing said about it is one its author cannot act
    on, and they would have to come and ask — which is the conversation this
    screen exists to save. The reason goes to them by mail and stands on the
    row for as long as it is kept.

    NOTHING IS UNDONE, because nothing was done: no organisation, no login, no
    relationship, no programme link and no email. The broker never learns they
    were considered. Re-asking later writes a new request rather than reopening
    this one, so the refusal and its reason stay on the record.
    """
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(400, {
            "message": "Say why you are turning this down — whoever asked has "
                       "to know what to do next.",
            "errors": {"reason": "required"}})
    with SessionLocal() as s:
        req = _open_request(s, request_id, principal)
        tid = req.tenant_id
        party = (s.get(Party, req.broker_party_id)
                 if req.broker_party_id else None)
        broker_name = ((party.legal_name if party else None) or req.org_name
                       or f"Request {req.id}")
        prog = s.get(Program, req.program_id) if req.program_id else None
        req.status = "rejected"
        req.reason = reason
        req.decided_by_user_id = principal.user_id
        req.decided_at = dt.datetime.now(dt.timezone.utc)
        _log_request(s, tid, principal, req, "broker_request_rejected",
                     broker_name=broker_name,
                     program_name=getattr(prog, "name", None), note=reason)
        s.commit()
        out = _row(req, {req.program_id: getattr(prog, "name", None)}, {}, {})
        out["broker_name"] = broker_name
        requested_by = req.requested_by_user_id

    _notify_requester(requested_by, principal, broker_name, approved=False,
                      reason=reason)
    return out


@router.delete("/broker-onboarding-requests/{request_id}")
def broker_onboarding_withdraw(request_id: int,
                               principal: Principal = Depends(
                                   require_role("carrier_admin"))):
    """Take the question back.

    The way out of a dead end that would otherwise need the admin: a carrier
    user who asked for the wrong broker, or asked twice, cannot raise the right
    request while the wrong one holds the duplicate guard. Their own only — the
    carrier admin answers a request rather than withdrawing it, and has Reject
    with a reason for that.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)
        req = s.get(BrokerOnboardingRequest, request_id)
        if not req or req.tenant_id != tid:
            raise HTTPException(404, "request not found")
        if req.requested_by_user_id != principal.user_id:
            raise HTTPException(
                403, "Only the person who raised a request can withdraw it. "
                     "Turn it down with a reason instead.")
        if req.status != PENDING:
            raise HTTPException(409, {
                "message": f"That request was already {req.status}, so there is "
                           f"nothing to withdraw.",
                "errors": {"request": req.status}})
        party = (s.get(Party, req.broker_party_id)
                 if req.broker_party_id else None)
        broker_name = ((party.legal_name if party else None) or req.org_name
                       or f"Request {req.id}")
        req.status = "withdrawn"
        req.decided_by_user_id = principal.user_id
        req.decided_at = dt.datetime.now(dt.timezone.utc)
        _log_request(s, tid, principal, req, "broker_request_withdrawn",
                     broker_name=broker_name)
        s.commit()
        return {"ok": True,
                "message": f"Your request for {broker_name} was withdrawn."}
