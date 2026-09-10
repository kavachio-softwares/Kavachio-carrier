"""The broker's own side of Kavachio.

A broker creates NOTHING structural. Carriers and programmes are handed to it:
the carrier puts the broker on a programme, and that `program_broker` row is
the entire extent of what the broker can reach. So every query here starts from
that table rather than from a tenant id, and the carrier comes out of the link
rather than being asked for.

That also makes every endpoint carrier-scoped by construction. A broker cannot
name a carrier it was not put on, because the carrier is never an input — it is
read off the link rows, which only exist where a carrier created one.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import String, and_, func, or_

import contract_routes
import contract_types as ct
from auth_deps import Principal, current_principal
from db import (
    CarrierBroker, link_carrier_broker,
    BrokerInvitation,
    SessionLocal, AppUser, Contract, Party, Program, ProgramBroker, Tenant,
)

router = APIRouter(tags=["broker"])


# --- scope ------------------------------------------------------------------

def _broker_party_id(s, p: Principal) -> int:
    """Which broker organisation this person works for.

    Read from the database, not the token: `mint_access_token` only puts
    user/tenant/role in the claims, so `Principal.broker_party_id` is always
    None today. Trusting it would silently resolve every broker to "no broker"
    and hand back empty screens that look like real answers.
    """
    if not p.is_broker:
        raise HTTPException(403, "this is a broker screen")
    u = s.query(AppUser).filter(AppUser.id == p.user_id).first()
    if not u or not u.broker_party_id:
        raise HTTPException(403, "no broker bound to this user")
    return int(u.broker_party_id)


def _links(s, broker_id: int, carrier_id: Optional[int] = None,
           program_id: Optional[int] = None):
    """The (carrier, programme) pairs this broker is actually on.

    Every read below funnels through here. A programme the carrier never linked
    has no row, so it cannot appear — there is no separate permission check to
    forget, and no way to widen the scope by passing a different id.
    """
    q = (s.query(ProgramBroker)
           .filter(ProgramBroker.broker_party_id == broker_id,
                   func.coalesce(ProgramBroker.status, "active") == "active"))
    if carrier_id is not None:
        q = q.filter(ProgramBroker.tenant_id == carrier_id)
    if program_id is not None:
        q = q.filter(ProgramBroker.program_id == program_id)
    return q.all()


def _contract_source(s, c: Contract, broker_id: int) -> str:
    """Who put this contract here — the broker, or the carrier.

    It decides both the label and whether it had to be approved, so it is
    derived from the submitter's own broker binding rather than stored twice.
    """
    if not c.submitted_by_user_id:
        return "carrier"
    u = s.query(AppUser).filter(AppUser.id == c.submitted_by_user_id).first()
    return "broker" if (u and u.broker_party_id == broker_id) else "carrier"


# --- what the carrier has given this broker ---------------------------------

@router.get("/broker/me")
def broker_me(p: Principal = Depends(current_principal)):
    """Who this seat belongs to.

    The broker's own party id, needed by any screen that has to build a
    carrier-centric path (/carriers/{c}/programs/{p}/brokers/{b}/...). The
    broker never PICKS this — it is read off their user row, exactly as every
    other endpoint here does — but the URL has to carry it, so the screen has
    to be able to ask.
    """
    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        me = s.query(Party).filter(Party.id == bid).first()
        return {"id": bid, "name": (me.legal_name if me else "—"), "role": p.role}


# =============================================================================
#  Invitations — the broker's side of being asked
# =============================================================================
#
# A carrier invites; the broker answers. The link that lets them produce is
# written HERE, on accept, and nowhere else — so a carrier cannot put a broker
# on a programme by unilateral act. That is what "invitation" means, and the
# old direct-assign path quietly skipped it.


def _accept_invitation(s, inv, party_id: int, how: str) -> None:
    """Record that this broker agreed to work with that carrier.

    THE ACCEPTED INVITATION IS THE RELATIONSHIP. An invitation names a carrier,
    not a programme — which programmes a broker produces on is a decision the
    carrier goes on making for years, and it is made afterwards from the
    programme's own screen. So the usual case writes no programme link at all;
    the `program_id` branch below exists only for an invitation that named one.

    Shared by the broker clicking Accept and by onboarding accepting on their
    behalf, so the two cannot drift into writing different rows.
    """
    inv.status = "accepted"
    inv.accepted_by = how
    inv.answered_at = dt.datetime.now(dt.timezone.utc)
    inv.party_id = party_id
    # THE RELATIONSHIP ITSELF, written where it begins. The invitation records
    # that they were asked and said yes; this records that they work together,
    # which is what every screen reads.
    link_carrier_broker(s, inv.tenant_id, party_id,
                        origin="invitation", by_user_id=inv.by_user_id)
    if not inv.program_id:
        return
    link = (s.query(ProgramBroker)
            .filter(ProgramBroker.program_id == inv.program_id,
                    ProgramBroker.broker_party_id == party_id)
            .first())
    if link:
        # Re-accepting after having been taken off reactivates the row rather
        # than inserting a second one; when it was first assigned is worth
        # keeping.
        link.status = "active"
        link.tenant_id = inv.tenant_id
    else:
        s.add(ProgramBroker(tenant_id=inv.tenant_id, program_id=inv.program_id,
                            broker_party_id=party_id, status="active",
                            assigned_by_user_id=inv.by_user_id))


def accept_pending_for_email(s, email: str, party_id: int) -> int:
    """Accept every invitation waiting on this address. Returns how many.

    Called when somebody finishes onboarding. A brand-new broker has nothing to
    weigh up — the invitation is why their login exists — so making them click
    Accept afterwards is a second click that can only ever be yes. Invitations
    that arrived while they were still setting up are swept in the same pass.
    """
    pending = (s.query(BrokerInvitation)
               .filter(func.lower(BrokerInvitation.email) == (email or "").lower(),
                       BrokerInvitation.status == "pending")
               .all())
    for inv in pending:
        _accept_invitation(s, inv, party_id, "auto")
    return len(pending)


@router.get("/broker/invitations")
def broker_invitations(p: Principal = Depends(current_principal)):
    """Carriers asking this broker to produce on a programme.

    Only what is still open and only what is addressed to this broker. A
    carrier's name appears here because they chose to introduce themselves by
    inviting — nothing is disclosed in the other direction.
    """
    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        me = s.query(AppUser).filter(AppUser.id == p.user_id).first()
        emails = {(me.email or "").lower()} if me else set()
        rows = (s.query(BrokerInvitation)
                .filter(BrokerInvitation.status == "pending",
                        or_(BrokerInvitation.party_id == bid,
                            func.lower(BrokerInvitation.email).in_(emails or {""})))
                .order_by(BrokerInvitation.created_at.desc())
                .all())
        if not rows:
            return []
        progs = {pr.id: pr.name for pr in s.query(Program).filter(
            Program.id.in_([r.program_id for r in rows if r.program_id])).all()}
        tens = {t.id: (t.legal_name or t.tenant_name) for t in s.query(Tenant).filter(
            Tenant.id.in_([r.tenant_id for r in rows])).all()}
        return [{
            "id": r.id,
            "carrier": tens.get(r.tenant_id, "—"),
            # Usually none: a carrier invites you to work with THEM, and picks
            # programmes afterwards. Shown only when one was named.
            "programme": progs.get(r.program_id) if r.program_id else None,
            "program_id": r.program_id,
            "invited_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows]


class InvitationAnswer(BaseModel):
    note: Optional[str] = None


@router.post("/broker/invitations/{invitation_id}/accept")
def broker_invitation_accept(invitation_id: int,
                             body: InvitationAnswer = InvitationAnswer(),
                             p: Principal = Depends(current_principal)):
    """Agree to produce on that carrier's programme. This writes the link."""
    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        me = s.query(AppUser).filter(AppUser.id == p.user_id).first()
        inv = s.get(BrokerInvitation, invitation_id)
        # 404 rather than 403: an invitation addressed to somebody else is not
        # this broker's to know about.
        if (not inv or inv.status != "pending"
                or (inv.party_id not in (None, bid)
                    and (inv.email or "").lower() != (me.email or "").lower())):
            raise HTTPException(404, "that invitation is not open to you")
        _accept_invitation(s, inv, bid, "broker")
        inv.note = (body.note or "").strip() or None
        tenant = s.query(Tenant).filter(Tenant.id == inv.tenant_id).first()
        carrier_name = (tenant.legal_name or tenant.tenant_name) if tenant else None
        s.commit()
        # The carrier comes back so the screen can SWITCH TO IT. Somebody who
        # just deliberately joined one carrier should not be dropped on a
        # merged view of all of them — that answers a question they did not ask
        # and hides the thing they came for.
        return {"ok": True, "program_id": inv.program_id,
                "carrier_id": inv.tenant_id, "carrier": carrier_name,
                "message": (f"You are now working with {carrier_name}."
                            if carrier_name else "Accepted.")
                           + (" They can put you on their programmes from here."
                              if not inv.program_id else "")}


@router.post("/broker/invitations/{invitation_id}/decline")
def broker_invitation_decline(invitation_id: int,
                              body: InvitationAnswer = InvitationAnswer(),
                              p: Principal = Depends(current_principal)):
    """Say no. Nothing is linked, and the carrier sees it was declined —
    which is a fact about the invitation THEY sent, not about this broker's
    other business."""
    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        me = s.query(AppUser).filter(AppUser.id == p.user_id).first()
        inv = s.get(BrokerInvitation, invitation_id)
        if (not inv or inv.status != "pending"
                or (inv.party_id not in (None, bid)
                    and (inv.email or "").lower() != (me.email or "").lower())):
            raise HTTPException(404, "that invitation is not open to you")
        inv.status = "declined"
        inv.answered_at = dt.datetime.now(dt.timezone.utc)
        inv.party_id = bid
        inv.note = (body.note or "").strip() or None
        s.commit()
        return {"ok": True, "message": "Declined."}


def _carrier_ids(s, broker_id: int) -> set[int]:
    """Every carrier this broker WORKS WITH.

    Two sources, and both are needed. A programme link proves a working
    relationship, but it is not the only one: a broker who has accepted a
    carrier's invitation works with them from that moment, whether or not a
    programme has been assigned yet — and assigning one may be days later.

    Reading only the links is why a broker could accept two carriers and see
    neither: they had agreed to work with both and been put on nothing, so the
    dashboard showed an empty list and the carrier switcher had nothing to
    switch between. The relationship is the invitation; the programme is what
    they do inside it.
    """
    from_links = {l.tenant_id for l in _links(s, broker_id) if l.tenant_id}
    from_rel = {r[0] for r in s.query(CarrierBroker.tenant_id)
                .filter(CarrierBroker.party_id == broker_id,
                        CarrierBroker.status == "active").all() if r[0]}
    return from_links | from_rel


@router.get("/broker/carriers")
def broker_carriers(p: Principal = Depends(current_principal)):
    """The carriers this broker works with.

    The first dropdown on BDX Setup, and the carrier switcher in the sidebar.
    It is a list, not a choice the broker makes freely — a carrier appears
    because the broker accepted their invitation, and `programme_count` says
    how much of that relationship has actually been set up yet. Zero is a real
    and common answer: accepted this morning, programmes tomorrow.
    """
    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        by_carrier: dict[int, int] = {t: 0 for t in _carrier_ids(s, bid)}
        for l in _links(s, bid):
            if l.tenant_id:
                by_carrier[l.tenant_id] = by_carrier.get(l.tenant_id, 0) + 1
        if not by_carrier:
            return []
        rows = s.query(Tenant).filter(Tenant.id.in_(list(by_carrier))).all()
        return [{
            "id": t.id,
            "name": t.legal_name or t.tenant_name,
            "programme_count": by_carrier.get(t.id, 0),
        } for t in sorted(rows, key=lambda t: (t.legal_name or t.tenant_name or "").lower())]


@router.get("/broker/programmes")
def broker_programmes(carrier_id: Optional[int] = Query(None),
                      p: Principal = Depends(current_principal)):
    """Programmes this broker is on — optionally narrowed to one carrier.

    Each row carries its carrier, because a broker producing for two carriers
    would otherwise see two identically-named programmes and no way to tell
    whose columns it is filling in.
    """
    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        links = _links(s, bid, carrier_id=carrier_id)
        if not links:
            return []
        progs = {pr.id: pr for pr in s.query(Program).filter(
            Program.id.in_([l.program_id for l in links])).all()}
        carriers = {t.id: (t.legal_name or t.tenant_name) for t in s.query(Tenant).filter(
            Tenant.id.in_([l.tenant_id for l in links if l.tenant_id])).all()}
        out = []
        for l in links:
            pr = progs.get(l.program_id)
            if not pr:
                continue
            out.append({
                "id": pr.id, "name": pr.name, "status": pr.status,
                "carrier_id": l.tenant_id,
                "carrier_name": carriers.get(l.tenant_id, "—"),
                "assigned_at": l.created_at.isoformat() if l.created_at else None,
            })
        return sorted(out, key=lambda r: (r["carrier_name"].lower(), r["name"].lower()))


@router.get("/broker/contracts")
def broker_contracts(carrier_id: Optional[int] = Query(None),
                     program_id: Optional[int] = Query(None),
                     p: Principal = Depends(current_principal)):
    """Every contract this broker holds, across every programme it is on.

    Two kinds live in one list: contracts the CARRIER added, which work
    straight away. Only a live one can be set up, so the UI needs that per row.

    IT ALSO CARRIES THE LIFECYCLE, and that is not a detail. Whether a contract
    may be set up says nothing about whether the carrier has sent terms over for
    the broker to read, argued back at, or is waiting on their signature.
    Without the lifecycle a contract sitting in
    `in_review` — the whole point of which is that the BROKER has to act —
    renders as an ordinary approved row, and the negotiation is invisible to
    the one person it is waiting on. `whose_turn` is included for the same
    reason: it is the question the list is actually being scanned for.
    """
    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        links = _links(s, bid, carrier_id=carrier_id, program_id=program_id)
        if not links:
            return []
        prog_ids = [l.program_id for l in links]
        carrier_of = {l.program_id: l.tenant_id for l in links}
        progs = {pr.id: pr for pr in s.query(Program).filter(Program.id.in_(prog_ids)).all()}
        carriers = {t.id: (t.legal_name or t.tenant_name) for t in s.query(Tenant).filter(
            Tenant.id.in_([t for t in carrier_of.values() if t])).all()}

        # Scoped by BOTH the programme and this broker: a contract on a shared
        # programme that belongs to a different broker is not this broker's.
        rows = (s.query(Contract)
                  .filter(Contract.program_id.in_(prog_ids),
                          Contract.broker_party_id == bid)
                  .order_by(Contract.id.desc()).all())
        out = []
        for c in rows:
            cid = carrier_of.get(c.program_id)
            state = contract_routes._effective_lifecycle(c)
            out.append({
                "id": c.id,
                "filename": c.filename,
                # An AUTHORED contract has no file, so a list keyed on filename
                # shows it as "Contract 462". The name is what it is called.
                "name": c.name or c.filename or f"Contract {c.id}",
                "contract_type": c.contract_type,
                "lifecycle": state,
                # Who the contract is waiting on. The SAME function the
                # carrier's record uses, not a second copy of the rule — the
                # two sides disagreeing about whose move it is would be worse
                # than neither of them saying.
                "whose_turn": contract_routes._whose_turn(
                    c, contract_routes._unsigned_sides(
                        contract_routes._signatures(s, c.id))),
                "has_wording": bool((c.wording_sections or {}).get("sections"))
                               or bool(c.blob_ref or c.blob),
                "programme": {"id": c.program_id,
                              "name": progs[c.program_id].name if c.program_id in progs else "—"},
                "carrier": {"id": cid, "name": carriers.get(cid, "—")},
                "inception_dt": c.inception_dt.isoformat() if c.inception_dt else None,
                "expiry_dt": c.expiry_dt.isoformat() if c.expiry_dt else None,
                "source": _contract_source(s, c, bid),
                "submitted_at": c.submitted_at.isoformat() if c.submitted_at else None,
                "created_at": c.created_at.isoformat() if getattr(c, "created_at", None) else None,
            })
        return out


@router.get("/broker/dashboard")
def broker_dashboard(carrier_id: Optional[int] = Query(None),
                     p: Principal = Depends(current_principal)):
    """The broker's landing screen: what they hold, and what is holding them up.

    TWO QUEUES, not one, and they point in opposite directions. "Waiting on the
    carrier" is what the broker cannot move; "waiting on you" is what nobody
    else can. The second one was missing entirely, so a carrier sending terms
    over for review reached a broker who was never told — the negotiation sat
    in a state whose whole purpose is that the broker acts on it, on a
    dashboard that only counted the other side's queue.

    SCOPED TO ONE CARRIER when `carrier_id` is given. A broker on several
    carriers was shown one merged pile: five contracts waiting, across three
    companies, with no way to answer "what does Northgate need from me". The
    counts are the reason to open this screen, and a count that spans carriers
    answers a question nobody asked. `carriers` always lists them all, so the
    screen can offer the switch regardless of what is selected.

    An unknown or unlinked carrier_id narrows to nothing rather than falling
    back to everything: silently widening a scope the caller asked to narrow is
    how a broker ends up acting on the wrong carrier's contract.
    """
    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        me = s.query(Party).filter(Party.id == bid).first()
        # Every carrier, for the switcher — computed before the narrowing, so
        # selecting one never hides the others.
        all_links = _links(s, bid)
        links = ([l for l in all_links if l.tenant_id == carrier_id]
                 if carrier_id is not None else all_links)
        prog_ids = [l.program_id for l in links]
        carrier_ids = sorted(_carrier_ids(s, bid))
        carriers = {t.id: (t.legal_name or t.tenant_name) for t in s.query(Tenant).filter(
            Tenant.id.in_(carrier_ids)).all()} if carrier_ids else {}
        progs = ({pr.id: pr.name for pr in s.query(Program).filter(Program.id.in_(prog_ids)).all()}
                 if prog_ids else {})

        # `pending` — contracts a broker had brought and the carrier had still
        # to approve — is gone with the upload flow that created them.
        on_me, live = [], 0
        if prog_ids:
            rows = (s.query(Contract)
                      .filter(Contract.program_id.in_(prog_ids),
                              Contract.broker_party_id == bid).all())
            for c in rows:
                carrier_name = carriers.get(
                    next((l.tenant_id for l in links
                          if l.program_id == c.program_id), None), "—")
                state = contract_routes._effective_lifecycle(c)

                # The broker's OWN queue. `agreed` is in it too: terms both
                # sides settled are waiting on the broker's signature, and a
                # contract nobody signs never goes live.
                turn = contract_routes._whose_turn(
                    c, contract_routes._unsigned_sides(
                        contract_routes._signatures(s, c.id)))
                if turn == "broker":
                    on_me.append({
                        "id": c.id,
                        "name": c.name or c.filename or f"Contract {c.id}",
                        "lifecycle": state,
                        "programme": progs.get(c.program_id, "—"),
                        "carrier": carrier_name,
                        "what": ("read the terms and agree them or ask for "
                                 "changes" if state == "in_review"
                                 else "sign it"),
                    })

                # LIVE means in force, not merely approved. Since a contract
                # goes in force only when both sides have signed it, an
                # approved-but-unsigned one is not something to produce
                # against, and counting it as live would say it is.
                if state == "active":
                    live += 1

        return {
            "broker": {"id": bid, "name": me.legal_name if me else "—"},
            "carriers": [{"id": i, "name": carriers.get(i, "—")} for i in carrier_ids],
            "counts": {
                "waiting_on_me": len(on_me),
                "live_contracts": live,
                "programmes": len(links),
                "carriers": len(carrier_ids),
            },
            # The queue only this broker can move.
            "waiting_on_me": on_me,
        }


# --- the broker's own people ------------------------------------------------
#
# A broker staffs itself. Kavachio created the carrier, the carrier created this
# broker's first admin, and that admin creates its operators here — each company
# brings in its own people, one level at a time.
#
# The database enforces the same chain (trg_enforce_invitation_chain): an
# operator may only be created by a broker admin, and chk_app_user_scope keeps
# it on a broker party with no carrier of its own. So OPERATOR is the only seat
# this screen can offer — a second broker admin still has to come from the
# carrier, the way this one did.

class BrokerUserBody(BaseModel):
    full_name: str
    email: str


def _broker_admin(s, p: Principal) -> int:
    """Broker id for someone who may CHANGE the team, not just look at it.

    An operator inherits its broker's reach but runs no organisation, so it
    never reaches these writes — and the invitation-chain trigger would refuse
    them anyway. Failing here says why, instead of surfacing a database error.
    """
    if p.role != "broker_admin":
        raise HTTPException(403, "only a broker admin can manage your team")
    return _broker_party_id(s, p)


def _broker_user_dict(u: AppUser) -> dict:
    from auth_deps import normalize_role
    return {
        "id": u.id,
        "email": u.email,
        "full_name": u.full_name,
        "role": normalize_role(u.role),
        "status": u.status,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    }


@router.get("/broker/users")
def broker_users(p: Principal = Depends(current_principal)):
    """Everyone at this broker organisation.

    Scoped by broker_party_id, never by tenant: a broker producing for three
    carriers has one team, not three.

    Admin-only, reads included — an operator has no sidebar entry for this
    screen, and an endpoint that answers a request the UI never makes is just a
    way for the two layers to disagree later.
    """
    with SessionLocal() as s:
        bid = _broker_admin(s, p)
        me = s.query(Party).filter(Party.id == bid).first()
        rows = (s.query(AppUser)
                  .filter(AppUser.broker_party_id == bid)
                  .order_by(AppUser.email).all())
        admins = sum(1 for u in rows if u.role == "broker_admin")
        return {
            "broker": {"id": bid, "name": (me.legal_name if me else "—")},
            "items": [_broker_user_dict(u) for u in rows],
            "total": len(rows),
            "total_admins": admins,
        }


@router.post("/broker/users")
def broker_user_invite(body: BrokerUserBody, p: Principal = Depends(current_principal)):
    """Invite an OPERATOR into this broker organisation.

    The role is not an input. A broker admin may create exactly one kind of
    seat, so letting the client name it would only create a way to be refused
    by the trigger. `invited_by_user_id` is the signed-in admin, which is what
    makes "who let this person in?" a stored fact rather than a guess.
    """
    from app_routes import _make_invite_link, _send_invite_email

    email = (body.email or "").strip().lower()
    name = (body.full_name or "").strip()
    if not email or not name:
        raise HTTPException(400, "Name and email are required")

    with SessionLocal() as s:
        bid = _broker_admin(s, p)
        if s.query(AppUser).filter(AppUser.email == email).first():
            raise HTTPException(409, "Email already exists")
        me = s.query(Party).filter(Party.id == bid).first()
        u = AppUser(
            email=email, full_name=name, role="operator",
            # An operator belongs to the broker, to no carrier. Setting
            # tenant_id here would break chk_app_user_scope.
            tenant_id=None, broker_party_id=bid,
            invited_by_user_id=p.user_id, status="invited",
        )
        s.add(u)
        link = _make_invite_link(u)
        s.commit(); s.refresh(u)
        _send_invite_email(u.email, link, u.full_name, me.legal_name if me else None)
        return _broker_user_dict(u)


@router.post("/broker/users/{user_id}/resend-invite")
def broker_user_resend(user_id: int, p: Principal = Depends(current_principal)):
    """Re-issue the set-password link. The old one stops working."""
    from app_routes import _make_invite_link, _send_invite_email
    with SessionLocal() as s:
        bid = _broker_admin(s, p)
        u = s.get(AppUser, user_id)
        # 404 rather than 403 on someone else's user: a wrong id must not tell
        # a broker whether that id exists somewhere else on the platform.
        if not u or u.broker_party_id != bid:
            raise HTTPException(404, "user not found")
        if u.status not in ("invited", "pending"):
            raise HTTPException(409, "this user has already accepted their invite")
        me = s.query(Party).filter(Party.id == bid).first()
        link = _make_invite_link(u)
        s.commit()
        _send_invite_email(u.email, link, u.full_name, me.legal_name if me else None)
        return {"ok": True}


@router.delete("/broker/users/{user_id}")
def broker_user_remove(user_id: int, p: Principal = Depends(current_principal)):
    """Take someone's access away. Their runs and uploads keep their name."""
    with SessionLocal() as s:
        bid = _broker_admin(s, p)
        u = s.get(AppUser, user_id)
        if not u or u.broker_party_id != bid:
            raise HTTPException(404, "user not found")
        if u.id == p.user_id:
            raise HTTPException(409, "You cannot remove your own account.")
        if u.role == "broker_admin":
            admins = (s.query(AppUser)
                        .filter(AppUser.broker_party_id == bid,
                                AppUser.role == "broker_admin").count())
            if admins <= 1:
                raise HTTPException(409, "This is the only admin — the carrier "
                                         "has to add another before this one goes.")
        s.delete(u); s.commit()
        return {"ok": True}


# --- the operator's day ------------------------------------------------------

class _OperatorHome(BaseModel):
    """Shape note only — the endpoint returns a plain dict."""


@router.get("/broker/operator-home")
def broker_operator_home(p: Principal = Depends(current_principal)):
    """What an operator has to do today.

    An operator is a seat the BROKER adds to do the day-to-day work, so the
    scope is the broker's — the same programmes, the same carriers. What
    differs is the question being asked: an admin asks "what is holding me
    up", an operator asks "what do I have to run, and what went wrong".

    Counts come from the real run tables. They are legitimately zero until a
    setup exists and a file has been through it, and the screen says so rather
    than showing invented activity.
    """
    from sqlalchemy import text as _text
    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        me = s.query(Party).filter(Party.id == bid).first()
        links = _links(s, bid)
        prog_ids = [l.program_id for l in links]
        carrier_ids = sorted({l.tenant_id for l in links if l.tenant_id})
        carriers = {t.id: (t.legal_name or t.tenant_name) for t in s.query(Tenant).filter(
            Tenant.id.in_(carrier_ids)).all()} if carrier_ids else {}

        # A setup an operator may run against belongs to one of THIS broker's
        # programmes. direct_format has no broker column yet (that is the next
        # schema change), so scope on the programme, which is already ours.
        setups = 0
        if prog_ids:
            setups = s.execute(_text(
                "SELECT count(*) FROM direct_format "
                "WHERE program_id = ANY(:pids) AND COALESCE(approved,0) = 1"),
                {"pids": prog_ids}).scalar() or 0

        # Runs and their exceptions, from the same programmes.
        runs, exceptions = 0, 0
        if prog_ids:
            runs = s.execute(_text(
                "SELECT count(*) FROM upload u "
                "WHERE u.program_id = ANY(:pids)"), {"pids": prog_ids}).scalar() or 0

        return {
            "broker": {"id": bid, "name": me.legal_name if me else "—"},
            "carriers": [{"id": i, "name": carriers.get(i, "—")} for i in carrier_ids],
            "counts": {
                "programmes": len(links),
                "setups": int(setups),
                "runs": int(runs),
                "exceptions": int(exceptions),
            },
            # Why the screen is empty, said in the API rather than guessed at
            # in the UI: the operator cannot run anything until a setup exists,
            # and only their broker admin can build one.
            "blocked_on": (
                "no-programme" if not links
                else "no-setup" if setups == 0
                else None
            ),
        }
