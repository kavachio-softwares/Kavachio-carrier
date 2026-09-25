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
from exception_tally import current_export_ids, export_tally as _export_tally
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
        # Only the broker admin answers them (_open_invitation), so only the
        # broker admin is shown them — a list with buttons that always fail
        # would be worse than none.
        if p.role != "broker_admin":
            return []
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


def _open_invitation(s, p: Principal, invitation_id: int):
    """The pending invitation this broker may answer, and its broker id.

    Answering one decides who the whole COMPANY works with, so it is the broker
    admin's act — an operator works inside the relationships the admin agreed.

    Open to them means exactly what GET /broker/invitations lists: addressed to
    their broker, or to their own email. An invitation with no broker recorded
    (party_id NULL) is theirs only by email — before, it was open to ANY broker
    seat that guessed its id, which let one broker accept (or kill) an
    invitation meant for somebody else and land on that carrier's programme.

    404 rather than 403: an invitation addressed to somebody else is not this
    broker's to know about.
    """
    if p.role != "broker_admin":
        raise HTTPException(403, "only your broker admin can answer a carrier's invitation")
    bid = _broker_party_id(s, p)
    me = s.query(AppUser).filter(AppUser.id == p.user_id).first()
    inv = s.get(BrokerInvitation, invitation_id)
    mine_by_email = bool(me and me.email and inv and inv.email
                         and inv.email.lower() == me.email.lower())
    if (not inv or inv.status != "pending"
            or not (inv.party_id == bid or mine_by_email)):
        raise HTTPException(404, "that invitation is not open to you")
    return inv, bid


@router.post("/broker/invitations/{invitation_id}/accept")
def broker_invitation_accept(invitation_id: int,
                             body: InvitationAnswer = InvitationAnswer(),
                             p: Principal = Depends(current_principal)):
    """Agree to produce on that carrier's programme. This writes the link."""
    with SessionLocal() as s:
        inv, bid = _open_invitation(s, p, invitation_id)
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
        inv, bid = _open_invitation(s, p, invitation_id)
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
                     status: Optional[str] = Query(None),
                     page: Optional[int] = Query(None, ge=1),
                     page_size: Optional[int] = Query(None, ge=1, le=200),
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

    Pagination is OPT-IN — without `page` the plain list comes back exactly as
    before. `status` takes the screen's own vocabulary: "mine" means whose_turn
    is the broker, anything else is matched against the lifecycle. Both facts
    are derived per row rather than stored, so the filter runs HERE — and it
    runs BEFORE the page is cut, so the total counts what the filter kept
    rather than what the page happened to hold.
    """
    def _page(out: list):
        # Counted over EVERYTHING this broker holds — before the status filter,
        # and before the page is cut. The screen states it above the table as
        # the reason to open the page at all, so a number that shrank as you
        # paged would be answering a different question.
        waiting = sum(1 for r in out if r["whose_turn"] == "broker")
        if status:
            out = [r for r in out
                   if (r["whose_turn"] == "broker" if status == "mine"
                       else r["lifecycle"] == status)]
        if page is None:
            return out
        size = page_size or 10
        start = (page - 1) * size
        return {"items": out[start:start + size], "total": len(out),
                "page": page, "page_size": size, "waiting_on_me": waiting}

    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        links = _links(s, bid, carrier_id=carrier_id, program_id=program_id)
        if not links:
            return _page([])
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
        return _page(out)


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

        # Signatures on this broker's own contracts: signed by everyone
        # (completed), and waiting on this broker's signature. Terms still to
        # agree come before signing, so they are counted apart.
        signatures_completed = 0
        if prog_ids and rows:
            from db import EsignEnvelope
            signatures_completed = (s.query(func.count(EsignEnvelope.id))
                .filter(EsignEnvelope.contract_id.in_([c.id for c in rows]),
                        EsignEnvelope.status == "completed").scalar() or 0)
        terms_to_agree = sum(1 for w in on_me if w["lifecycle"] == "in_review")

        agency_exceptions = 0
        if prog_ids:
            from db import OutputExport
            import validation_outcome as vo
            agency_exceptions = s.query(func.coalesce(func.sum(OutputExport.exception_count), 0)).filter(
                OutputExport.broker_party_id == bid,
                OutputExport.program_id.in_(prog_ids),
                OutputExport.status == vo.HAS_EXCEPTIONS
            ).scalar() or 0

        # "Users" tile — this broker's OWN people: its admins and its users
        # (operators), invited ones included. Not narrowed by carrier: a broker
        # has one team whichever carrier it is producing for. Removed people
        # are deleted outright here, so every row still counts.
        user_rows = (s.query(AppUser.status, func.count(AppUser.id))
                       .filter(AppUser.broker_party_id == bid)
                       .group_by(AppUser.status).all())

        return {
            "broker": {"id": bid, "name": me.legal_name if me else "—"},
            "carriers": [{"id": i, "name": carriers.get(i, "—")} for i in carrier_ids],
            "counts": {
                "waiting_on_me": len(on_me),
                "live_contracts": live,
                "programmes": len(links),
                "carriers": len(carrier_ids),
                "users": sum(n for _st, n in user_rows),
                "users_invited": sum(n for st, n in user_rows
                                     if st in ("invited", "pending")),
                "agency_exceptions": int(agency_exceptions),
                "signatures_pending": len(on_me) - terms_to_agree,
                "signatures_completed": int(signatures_completed),
                "terms_to_agree": terms_to_agree,
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

        # Runs made FOR THIS BROKER — every one its team sent, whichever of
        # them sent it, and every one the CARRIER ran for it (a carrier's
        # Process Bordereau with this broker picked stamps the same column).
        # All of the broker's users see the same runs and the same exceptions.
        # Not every upload on the programme: programmes are shared, and
        # counting by programme alone showed other brokers' files.
        runs, exceptions, exception_runs = 0, 0, 0
        my_uploads_week, turnaround_sec = 0, None
        recent: list[dict] = []
        if prog_ids:
            from db import LandingRecord, OutputExport
            import validation_outcome as vo
            mine = s.query(OutputExport).filter(
                OutputExport.broker_party_id == bid,
                OutputExport.program_id.in_(prog_ids))
            runs = mine.count()

            # Files THIS person sent through Process Bordereau. Runs made
            # before the uploader was recorded have no user and are not
            # guessed at, so this counts from the day recording began.
            week_ago = dt.datetime.utcnow() - dt.timedelta(days=7)
            my_uploads_week = mine.filter(
                OutputExport.generated_by_user_id == p.user_id,
                OutputExport.created_at >= week_ago).count()

            # File received (landing written) to output ready (export written)
            # — the same measure the carrier's own turnaround tile uses, over
            # the broker's runs in the last 30 days.
            month_ago = dt.datetime.utcnow() - dt.timedelta(days=30)
            turnaround_sec = (s.query(func.avg(
                    func.extract("epoch", OutputExport.created_at)
                    - func.extract("epoch", LandingRecord.created_at)))
                .join(LandingRecord, LandingRecord.output_export_id == OutputExport.id)
                .filter(OutputExport.broker_party_id == bid,
                        OutputExport.program_id.in_(prog_ids),
                        OutputExport.created_at >= month_ago,
                        OutputExport.created_at >= LandingRecord.created_at)
                .scalar())
            # Counted the way the carrier's own "Exceptions to review" tile
            # counts them, so both sides read the same number for a run.
            exceptions, exception_runs = s.query(
                func.coalesce(func.sum(OutputExport.exception_count), 0),
                func.count(OutputExport.id),
            ).filter(OutputExport.broker_party_id == bid,
                     OutputExport.program_id.in_(prog_ids),
                     OutputExport.status == vo.HAS_EXCEPTIONS).one()
            recent = _run_rows(s, mine.order_by(OutputExport.id.desc()).limit(10).all())

        return {
            "broker": {"id": bid, "name": me.legal_name if me else "—"},
            "carriers": [{"id": i, "name": carriers.get(i, "—")} for i in carrier_ids],
            "counts": {
                "programmes": len(links),
                "setups": int(setups),
                "runs": int(runs),
                "exceptions": int(exceptions),
                "exception_runs": int(exception_runs),
            },
            "my_uploads_this_week": int(my_uploads_week),
            # Seconds; None when there is no run to measure in the last 30 days.
            "avg_turnaround_sec": (round(float(turnaround_sec), 1)
                                   if turnaround_sec is not None else None),
            # Newest first — the broker's own runs and the ones the carrier
            # ran for it, the same list for every one of the broker's users.
            "recent_runs": recent,
            # Why the screen is empty, said in the API rather than guessed at
            # in the UI: the operator cannot run anything until a setup exists,
            # and only their broker admin can build one.
            "blocked_on": (
                "no-programme" if not links
                else "no-setup" if setups == 0
                else None
            ),
        }


# --- the same activity, shaped for charts -----------------------------------


@router.get("/broker/insights")
def broker_insights(days: int = Query(30, ge=7, le=90),
                    p: Principal = Depends(current_principal)):
    """This broker's activity over a window, for the two dashboard charts.

    Both broker screens read this one endpoint. The admin asks "who is doing
    the work and which carrier is it for"; the user asks "how did our runs go".
    Same rows, different cuts — one query set, so the two screens cannot drift
    into quoting different numbers for the same week.

    EVERY COUNT IS BROKER-WIDE, NEVER PER PERSON. `output_exports` records the
    broker a run was made FOR (`broker_party_id`) and never the person who sent
    it: a broker-lane run stamps `generated_by = "broker:<id>"`, the company.
    So there is no honest per-user file count to return, and the screens say
    "your team" rather than implying a personal total. This is deliberate, not
    a gap to fill later — the carrier must not be able to see which of a
    broker's people did what.

    Two things ARE attributable to a person, and `by_person` carries both.
    A decision on an exception: `exception_decision_log` takes the decider from
    the login, and `decided_by_broker_party_id` is that person's own broker.
    And a file sent through Process Bordereau: `output_exports
    .generated_by_user_id` is stamped from the principal that ran it — never
    served to a carrier seat, which still sees only "broker:<id>". So each
    person's row carries the files they sent and the exceptions on them,
    still open or put right. It goes to the broker
    ADMIN only — the team roster is already admin-only, and a per-colleague
    league table is not an operator's business.

    Days with nothing in them are returned as zeros rather than skipped, so a
    quiet day reads as a quiet day instead of collapsing the axis.
    """
    from db import ExceptionDecisionLog, OutputExport
    import validation_outcome as vo

    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        links = _links(s, bid)
        prog_ids = [l.program_id for l in links]

        # utcnow, because that is what every writer here stores. The column is
        # timestamptz on a server in Asia/Kolkata, so comparing against a local
        # "now" would silently drop the last 5.5 hours of rows.
        today = dt.datetime.utcnow().date()
        day_list = [today - dt.timedelta(days=i) for i in range(days - 1, -1, -1)]
        since = dt.datetime.combine(day_list[0], dt.time.min)
        week_since = dt.datetime.utcnow() - dt.timedelta(days=7)

        runs: dict[str, dict] = {}
        resolved: dict[str, int] = {}
        by_carrier: list[dict] = []
        carriers_total = 0
        by_person: Optional[list[dict]] = None
        runs_week = 0

        if prog_ids:
            scope = and_(OutputExport.broker_party_id == bid,
                         OutputExport.program_id.in_(prog_ids))

            for d, status, n in (
                s.query(func.date(OutputExport.created_at),
                        OutputExport.status, func.count(OutputExport.id))
                 .filter(scope, OutputExport.created_at >= since)
                 .group_by(func.date(OutputExport.created_at),
                           OutputExport.status).all()
            ):
                cell = runs.setdefault(
                    str(d), {"clean": 0, "flagged": 0, "not_checked": 0})
                # Three outcomes, kept apart. A file nobody checked is not a
                # clean one — folding it into either colour would report work
                # that never happened.
                if status == vo.HAS_EXCEPTIONS:
                    cell["flagged"] += n
                elif status == vo.CLEAN:
                    cell["clean"] += n
                else:
                    cell["not_checked"] += n

            runs_week = (s.query(func.count(OutputExport.id))
                          .filter(scope, OutputExport.created_at >= week_since)
                          .scalar() or 0)

            # Which carrier the work was for — computed by _carrier_ranking below.
            by_carrier, carriers_total = _carrier_ranking(s, bid, prog_ids, links, since, limit=5)

        # Resolved-over-time is NOT gated on prog_ids: a decision is logged
        # against the broker who made it, and it stays true even after the
        # carrier takes that broker off the programme.
        for d, n in (
            s.query(func.date(ExceptionDecisionLog.decided_at),
                    func.count(ExceptionDecisionLog.id))
             .filter(ExceptionDecisionLog.decided_by_broker_party_id == bid,
                     ExceptionDecisionLog.decided_at >= since,
                     # "reject" settles an exception too — see _team_ranking.
                     ExceptionDecisionLog.kind.in_(
                         ("fix", "approve", "dismiss", "reject")))
             .group_by(func.date(ExceptionDecisionLog.decided_at)).all()
        ):
            resolved[str(d)] = resolved.get(str(d), 0) + n

        people_total = None
        if p.role == "broker_admin":
            # The top of the team only. A broker can have hundreds of people;
            # the dashboard draws a handful, and "View all" pages through the
            # rest from /broker/insights/people rather than shipping every
            # row on every load.
            # The admin's own row is IN it: they run bordereaux too now, so
            # leaving themselves out hid real work from the one card that
            # reports it (see _team_ranking).
            by_person, people_total = _team_ranking(s, bid, since, limit=5)

        return {
            "days": days,
            # Oldest → newest, every day present, each one labelled with its
            # own date so the screen never has to infer which day a bar is.
            "runs_by_day": [
                {"date": str(d),
                 **runs.get(str(d), {"clean": 0, "flagged": 0, "not_checked": 0}),
                 "resolved": resolved.get(str(d), 0)}
                for d in day_list
            ],
            "by_carrier": by_carrier,
            "carriers_total": carriers_total,
            "by_person": by_person,
            "people_total": people_total,
            "totals": {
                "runs_this_week": int(runs_week),
                "runs_in_window": sum(
                    c["clean"] + c["flagged"] + c["not_checked"]
                    for c in runs.values()),
                "resolved_in_window": sum(resolved.values()),
            },
        }


def _run_rows(s, exports) -> list[dict]:
    """The run rows both the dashboard and the run history show."""
    from app_routes import _iso_utc
    prog_names = ({pid: name for pid, name in s.query(Program.id, Program.name)
                   .filter(Program.id.in_({e.program_id for e in exports})).all()}
                  if exports else {})
    cids = {e.contract_id for e in exports if e.contract_id}
    contract_names = ({cid: (name or fname) for cid, name, fname in
                       s.query(Contract.id, Contract.name, Contract.filename)
                       .filter(Contract.id.in_(cids)).all()} if cids else {})
    return [{
        "export_id": e.id,
        "filename": e.filename,
        "programme": prog_names.get(e.program_id),
        "contract": contract_names.get(e.contract_id),
        "rows": e.policy_count,
        "exception_count": e.exception_count or 0,
        "status": e.status,
        # A run through the broker's own lane is recorded as the broker
        # company; anything else the carrier ran for them.
        "sent_by": ("broker" if (e.generated_by or "").startswith("broker:")
                    else "carrier"),
        "created_at": _iso_utc(e.created_at),
    } for e in exports]


def _uploader_work(s, bid: int, user_ids: list[int], since) -> dict[int, dict]:
    """Per person: the exceptions on the files THEY sent, open and put right.

    The only per-user file fact a broker has is `generated_by_user_id`, stamped
    on a run made through Process Bordereau (see the dashboard's own note on
    per-user attribution). Files run before that column existed, and files the
    CARRIER ran for the broker, carry no user — they are left out rather than
    attributed to a guess.

    COUNTED IN EXCEPTIONS, NOT ROWS OR CELLS. An exception sits on one CELL:
    a row of forty values with one bad date is one exception, not a bad row.
    Counting it as a row said "10 of 10 rows need review" about a file where
    thirty-nine values in forty were fine — true by its own definition and
    wrong to every reader. Counting the other way, against every cell checked,
    buries the same 14 exceptions in ~500 cells and draws a sliver nobody can
    see. So the bar is the WORK — how much of it is still open — and the size
    of what it came from is written beside it as files and rows.

    `rows_flagged` is kept for that sentence only ("14 exceptions across 10
    rows"), never as a bar: it is context for the count, not a share of the
    file.

    Decisions are matched to the file's exceptions AS THEY ARE NOW, notices
    left out, exactly as Exception Triage matches them, so a bar here always
    equals the screen it opens. Counting `exception_decision_log` rows instead
    would call a file fixed on the strength of decisions about a rule that is
    no longer an exception on it.
    """
    from db import OutputExport

    out = {uid: {"files": 0, "rows": 0, "rows_flagged": 0,
                 "exceptions": 0, "open": 0, "put_right": 0}
           for uid in user_ids}
    if not user_ids:
        return out
    live = current_export_ids(s, broker_party_id=bid)
    if not live:
        return out
    rows = (s.query(OutputExport.id, OutputExport.generated_by_user_id)
             .filter(OutputExport.broker_party_id == bid,
                     OutputExport.generated_by_user_id.in_(user_ids),
                     OutputExport.created_at >= since,
                     OutputExport.id.in_(live)).all())
    for eid, uid in rows:
        # One export at a time, then let it go: the exceptions column runs to
        # megabytes on a big bordereau and a busy person has many of them.
        r = s.get(OutputExport, eid)
        if r is None:
            continue
        tally = out[uid]
        t = _export_tally(r)
        tally["files"] += 1
        tally["rows"] += t["rows"]
        tally["rows_flagged"] += t["rows_flagged"]
        tally["exceptions"] += t["exceptions"]
        tally["open"] += t["open"]
        tally["put_right"] += t["put_right"]
        s.expunge(r)
    return out


def _team_ranking(s, bid: int, since, q: Optional[str] = None,
                  offset: int = 0, limit: int = 10,
                  exclude_user_id: Optional[int] = None):
    """The broker's whole team ranked by how much work they have brought in
    since `since` — files sent, then exceptions put right.

    RANKED ON FILES, NOT ON DECISIONS. A person who uploads a bordereau with
    fourteen exceptions on it and has fixed none of them is the most active
    person on the team and the one an admin most needs to see; ranking on
    decisions alone put them LAST and pushed them off a five-row card. So the
    order is files sent first, decisions as the tie-break, and the row itself
    says which of its rows still need review.

    Counted and ranked in the database, so the rank costs the same for five
    people as for five thousand. The rank is the TEAM rank, taken before any
    search, so someone found by name keeps their real place rather than
    becoming #1 of the results. Ties share a rank; everyone who has done
    nothing in the window shares the last one.

    `uploads` is attached only to the page being returned (see
    `_uploader_work`, which has to open each file's exception blob) — ranking
    stays in SQL, the detail is bought for the handful of people actually
    being drawn.

    `exclude_user_id` drops a user from the list. It is applied before ranking,
    so a colleague's rank is never shifted by the dropped row.

    IT IS NO LONGER USED TO DROP THE VIEWER. It was, on the reasoning that an
    admin is not someone they keep tabs on. That held while the admin could not
    send a file: their row was an empty one and hiding it lost nothing. Now that
    both broker seats run Process Bordereau, an admin who sends the month's
    bordereaux themselves is the person doing the work this screen exists to
    show — and excluding them answered "who on my team sent what" with a table
    that left out the largest sender, or with nothing at all at a broker where
    the admin is the only person. The parameter is kept because the exclusion is
    still the right call for any OTHER row a caller wants left out.
    """
    from auth_deps import normalize_role
    from db import ExceptionDecisionLog, OutputExport
    counts = (s.query(ExceptionDecisionLog.decided_by_user_id.label("uid"),
                      func.count(ExceptionDecisionLog.id).label("n"))
               .filter(ExceptionDecisionLog.decided_by_broker_party_id == bid,
                       ExceptionDecisionLog.decided_at >= since,
                       ExceptionDecisionLog.decided_by_user_id.isnot(None),
                       # Every kind that settles an exception, "reject" too —
                       # a file's own tally counts a rejected one as resolved,
                       # so leaving it out here made the two disagree.
                       ExceptionDecisionLog.kind.in_(
                           ("fix", "approve", "dismiss", "reject")))
               .group_by(ExceptionDecisionLog.decided_by_user_id)
               .subquery())
    # Files each person SENT in the window. Only runs made through Process
    # Bordereau carry a user; a run the carrier made for this broker does not,
    # and is not attributed to anyone.
    sent = (s.query(OutputExport.generated_by_user_id.label("uid"),
                    func.count(OutputExport.id).label("n"))
             .filter(OutputExport.broker_party_id == bid,
                     OutputExport.created_at >= since,
                     OutputExport.generated_by_user_id.isnot(None))
             .group_by(OutputExport.generated_by_user_id)
             .subquery())
    n = func.coalesce(counts.c.n, 0)
    f = func.coalesce(sent.c.n, 0)
    base = s.query(AppUser.id.label("id"),
                   AppUser.full_name.label("full_name"),
                   AppUser.email.label("email"),
                   AppUser.role.label("role"),
                   n.label("resolved"),
                   f.label("files"),
                   func.rank().over(order_by=(f.desc(), n.desc())).label("rank")) \
             .outerjoin(counts, counts.c.uid == AppUser.id) \
             .outerjoin(sent, sent.c.uid == AppUser.id) \
             .filter(AppUser.broker_party_id == bid)
    if exclude_user_id is not None:
        base = base.filter(AppUser.id != exclude_user_id)
    ranked = base.subquery()
    rows = s.query(ranked)
    if q and q.strip():
        like = f"%{q.strip()}%"
        rows = rows.filter(or_(ranked.c.full_name.ilike(like),
                               ranked.c.email.ilike(like)))
    total = rows.count()
    page = (rows.order_by(ranked.c.rank,
                          func.lower(func.coalesce(ranked.c.full_name, ranked.c.email)),
                          ranked.c.id)
                .offset(offset).limit(limit).all())
    work = _uploader_work(s, bid, [r.id for r in page], since)
    items = []
    for r in page:
        w = work.get(r.id, {})
        items.append({
            "id": r.id,
            "name": r.full_name or r.email or f"User {r.id}",
            "role": normalize_role(r.role),
            "resolved": int(r.resolved),
            "rank": int(r.rank),
            # The files this person sent, and the exceptions on them. `files`
            # comes from the same query the rank does, so the number and the
            # order can never disagree.
            "files": int(r.files),
            "uploads": {
                "rows": w.get("rows", 0),
                # Rows carrying at least one open exception — context for the
                # count ("14 across 10 rows"), never drawn as a share.
                "rows_flagged": w.get("rows_flagged", 0),
                "exceptions": w.get("exceptions", 0),
                "open": w.get("open", 0),
                "put_right": w.get("put_right", 0),
            },
        })
    return items, total


def _carrier_ranking(s, bid: int, prog_ids: list[int], links, since,
                     q: Optional[str] = None, offset: int = 0, limit: int = 10):
    """Every carrier this broker is linked to, ranked by files run since
    `since`. A carrier with no runs this window still appears at the bottom,
    the same way a quiet team member still appears in `_team_ranking` — a
    broker's carrier count is small enough that this is one pass in Python,
    not a query worth pushing into SQL.
    """
    from db import OutputExport
    carrier_ids = sorted({l.tenant_id for l in links if l.tenant_id})
    tally: dict[int, int] = {cid: 0 for cid in carrier_ids}
    if prog_ids:
        carrier_of = {l.program_id: l.tenant_id for l in links}
        scope = and_(OutputExport.broker_party_id == bid,
                     OutputExport.program_id.in_(prog_ids))
        for pid, n in (s.query(OutputExport.program_id, func.count(OutputExport.id))
                        .filter(scope, OutputExport.created_at >= since)
                        .group_by(OutputExport.program_id).all()):
            cid = carrier_of.get(pid)
            if cid in tally:
                tally[cid] += n
    names = {t.id: (t.legal_name or t.tenant_name)
             for t in s.query(Tenant).filter(Tenant.id.in_(carrier_ids or [0])).all()}
    ranked = sorted(
        ({"id": cid, "name": names.get(cid, "—"), "runs": n} for cid, n in tally.items()),
        key=lambda r: (-r["runs"], r["name"].lower()))
    rank, last = 0, None
    for i, r in enumerate(ranked):
        if r["runs"] != last:
            rank, last = i + 1, r["runs"]
        r["rank"] = rank
    if q and q.strip():
        needle = q.strip().lower()
        ranked = [r for r in ranked if needle in r["name"].lower()]
    total = len(ranked)
    return ranked[offset:offset + limit], total


@router.get("/broker/insights/carriers")
def broker_insights_carriers(days: int = Query(30, ge=7, le=90),
                             page: int = Query(1, ge=1),
                             page_size: int = Query(25, ge=1, le=100),
                             q: Optional[str] = Query(None, max_length=100),
                             p: Principal = Depends(current_principal)):
    """Every carrier this broker works with, ranked by files run, a page at a
    time, with a name search. Broker admin only, like the dashboard card it
    opens from.
    """
    with SessionLocal() as s:
        bid = _broker_admin(s, p)
        links = _links(s, bid)
        prog_ids = [l.program_id for l in links]
        today = dt.datetime.utcnow().date()
        since = dt.datetime.combine(today - dt.timedelta(days=days - 1), dt.time.min)
        items, total = _carrier_ranking(s, bid, prog_ids, links, since, q=q,
                                        offset=(page - 1) * page_size, limit=page_size)
        return {"items": items, "total": total}


@router.get("/broker/insights/people/{user_id}/decisions")
def broker_person_decisions(user_id: int, days: int = Query(30, ge=7, le=90),
                            page: int = Query(1, ge=1),
                            page_size: int = Query(25, ge=1, le=100),
                            p: Principal = Depends(current_principal)):
    """WHO resolved what on the files this person SENT — the receipt behind the
    "Put right" column above it.

    It used to list the decisions this person made themselves, anywhere. That
    answered a question nobody was asking and read as broken: Cleap's page said
    "nothing put right" beside a file showing 65 put right, because the 65 were
    the ADMIN's. What a broker admin wants from a person's page is the other
    cut — this is the work on THEIR files, whoever did it, with the decider
    named on every row.

    Scoped to THIS broker by the FILES, not by the decider: only exports
    stamped with this broker and sent by this user are looked at, so a user id
    from another broker matches no export and returns nothing. That also lets a
    decision made by a CARRIER person on one of these files appear — it is work
    on this file, and the name is written the way a broker seat may see it
    (their own people by name, the other side as its company: see
    `decision_log.Labeller`).

    A re-run moves the landing pointer to a new export, so decisions saved
    against the older one are matched by landing too — otherwise "Fix & Validate"
    would empty this list.
    """
    from db import ExceptionDecisionLog, LandingRecord, OutputExport
    with SessionLocal() as s:
        bid = _broker_admin(s, p)
        today = dt.datetime.utcnow().date()
        since = dt.datetime.combine(today - dt.timedelta(days=days - 1), dt.time.min)

        live = current_export_ids(s, broker_party_id=bid)
        mine = [i for (i,) in s.query(OutputExport.id)
                .filter(OutputExport.broker_party_id == bid,
                        OutputExport.generated_by_user_id == user_id,
                        OutputExport.created_at >= since,
                        OutputExport.id.in_(live or {-1})).all()]
        landings = [i for (i,) in s.query(LandingRecord.id)
                    .filter(LandingRecord.output_export_id.in_(mine or [0])).all()]
        q = (s.query(ExceptionDecisionLog)
              .filter(or_(ExceptionDecisionLog.export_id.in_(mine or [0]),
                          ExceptionDecisionLog.landing_id.in_(landings or [0])),
                      # Every kind that settles an exception, "reject" included:
                      # the file's own tally counts a rejected one as put right,
                      # so leaving it out here made the two disagree.
                      ExceptionDecisionLog.kind.in_(
                          ("fix", "approve", "dismiss", "reject"))))
        total = q.count()
        rows = (q.order_by(ExceptionDecisionLog.decided_at.desc())
                 .offset((page - 1) * page_size).limit(page_size).all())

        eids = {r.export_id for r in rows if r.export_id}
        exports = ({e.id: e for e in s.query(OutputExport)
                    .filter(OutputExport.id.in_(eids)).all()} if eids else {})
        pids = {e.program_id for e in exports.values() if e.program_id}
        prog_names = ({pid: name for pid, name in s.query(Program.id, Program.name)
                       .filter(Program.id.in_(pids)).all()} if pids else {})

        from app_routes import _iso_utc
        from decision_log import Labeller
        who = Labeller(s, p)

        def _row(r):
            exp = exports.get(r.export_id)
            return {
                "id": r.id,
                "kind": r.kind,
                "policy_number": r.policy_number,
                "sheet": r.sheet,
                "row": r.row,
                "field": r.field,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "reason": r.reason,
                "decided_at": _iso_utc(r.decided_at),
                "export_id": r.export_id,
                "filename": exp.filename if exp else None,
                "programme": prog_names.get(exp.program_id) if exp else None,
                # WHO resolved it, in the words this viewer may see.
                "decided_by": who.label(r.decided_by_user_id),
                "decided_by_user_id": r.decided_by_user_id,
            }

        # Same bid filter as the decisions query, for the same reason: a
        # user id from another broker gets no name here either.
        person = (s.query(AppUser)
                   .filter(AppUser.id == user_id, AppUser.broker_party_id == bid)
                   .first())
        return {
            "person": {"id": user_id,
                       "name": (person.full_name or person.email) if person else None},
            "items": [_row(r) for r in rows],
            "total": total,
        }


@router.get("/broker/insights/people/{user_id}/files")
def broker_person_files(user_id: int, days: int = Query(30, ge=7, le=90),
                        page: int = Query(1, ge=1),
                        page_size: int = Query(25, ge=1, le=100),
                        p: Principal = Depends(current_principal)):
    """WHICH files this person sent, and what is still open on each one.

    The Team Activity bar is one number for a person — "564 of 564 open" — and
    a broker admin cannot act on that. Those 564 came from four files on two
    different programmes, and a programme is the unit a broker admin actually
    reviews: the contract, the rules and the carrier all hang off it. So this
    returns the files themselves, each with its own counts and its own way in,
    plus the same totals grouped by programme. Sending an admin straight to the
    newest flagged file (what the card's link used to do) answered a question
    nobody asked.

    Counted by `_export_tally`, the same function behind the dashboard bar, so
    the file rows add up to the bar and each row equals the triage screen it
    opens.

    Broker admin only, and scoped to THIS broker: every export is filtered on
    `broker_party_id == bid`, so a user id belonging to another broker returns
    an empty list rather than that broker's files.
    """
    from db import OutputExport
    from app_routes import _iso_utc
    with SessionLocal() as s:
        bid = _broker_admin(s, p)
        today = dt.datetime.utcnow().date()
        since = dt.datetime.combine(today - dt.timedelta(days=days - 1), dt.time.min)

        person = (s.query(AppUser)
                   .filter(AppUser.id == user_id, AppUser.broker_party_id == bid)
                   .first())
        live = current_export_ids(s, broker_party_id=bid)
        q = (s.query(OutputExport.id)
              .filter(OutputExport.broker_party_id == bid,
                      OutputExport.generated_by_user_id == user_id,
                      OutputExport.created_at >= since,
                      OutputExport.id.in_(live or {-1}))
              .order_by(OutputExport.created_at.desc(), OutputExport.id.desc()))
        eids = [i for (i,) in q.all()]
        total = len(eids)

        # Which carrier each programme belongs to — the broker's own links, so
        # a programme it was taken off simply has no carrier name rather than
        # reaching into a carrier it can no longer see.
        links = _links(s, bid)
        carrier_of_prog = {l.program_id: l.tenant_id for l in links}
        cids = {c for c in carrier_of_prog.values() if c}
        carrier_names = ({t.id: (t.legal_name or t.tenant_name) for t in
                          s.query(Tenant).filter(Tenant.id.in_(cids)).all()}
                         if cids else {})
        pids = {pid for (pid,) in s.query(OutputExport.program_id)
                .filter(OutputExport.id.in_(eids or [0])).distinct().all() if pid}
        prog_names = ({pid: name for pid, name in s.query(Program.id, Program.name)
                       .filter(Program.id.in_(pids)).all()} if pids else {})

        # Every file is tallied (the programme summary has to cover all of
        # them), but only the page asked for is returned. A broker's files in a
        # 30-day window are tens, not thousands — the cost is one exception blob
        # per file, the same pass the dashboard already makes for its top five.
        items, by_prog = [], {}
        lo, hi = (page - 1) * page_size, (page - 1) * page_size + page_size
        for n, eid in enumerate(eids):
            r = s.get(OutputExport, eid)
            if r is None:
                continue
            t = _export_tally(r)
            cid = carrier_of_prog.get(r.program_id)
            g = by_prog.setdefault(r.program_id, {
                "id": r.program_id,
                "name": prog_names.get(r.program_id) or "No programme",
                "carrier": carrier_names.get(cid),
                "files": 0, "rows": 0, "exceptions": 0, "open": 0, "put_right": 0,
            })
            g["files"] += 1
            for k in ("rows", "exceptions", "open", "put_right"):
                g[k] += t[k]
            if lo <= n < hi:
                items.append({
                    "export_id": r.id,
                    "source_upload_id": r.source_upload_id,
                    "filename": r.filename,
                    "programme_id": r.program_id,
                    "programme": prog_names.get(r.program_id),
                    "carrier": carrier_names.get(cid),
                    "status": r.status,
                    "created_at": _iso_utc(r.created_at),
                    **t,
                })
            s.expunge(r)

        programmes = sorted(by_prog.values(),
                            key=lambda g: (-g["open"], -g["files"], g["name"].lower()))
        return {
            "person": {"id": user_id,
                       "name": (person.full_name or person.email) if person else None},
            "items": items,
            "total": total,
            "by_programme": programmes,
            "totals": {
                "files": total,
                **{k: sum(g[k] for g in programmes)
                   for k in ("rows", "exceptions", "open", "put_right")},
            },
        }


@router.get("/broker/insights/people")
def broker_insights_people(days: int = Query(30, ge=7, le=90),
                           page: int = Query(1, ge=1),
                           page_size: int = Query(25, ge=1, le=100),
                           q: Optional[str] = Query(None, max_length=100),
                           p: Principal = Depends(current_principal)):
    """Every person on the team, ranked, a page at a time, with a name search.

    Broker admin only, like the dashboard card it opens from.
    """
    with SessionLocal() as s:
        bid = _broker_admin(s, p)
        today = dt.datetime.utcnow().date()
        since = dt.datetime.combine(today - dt.timedelta(days=days - 1), dt.time.min)
        items, total = _team_ranking(s, bid, since, q=q,
                                     offset=(page - 1) * page_size, limit=page_size)
        return {"items": items, "total": total}


@router.get("/broker/runs")
def broker_runs(page: int = Query(1, ge=1),
                page_size: int = Query(20, ge=1, le=100),
                p: Principal = Depends(current_principal)):
    """Every run made for this broker, newest first, a page at a time.

    Same scope as the dashboard's recent runs: this broker's runs on the
    programmes it is still on, whether its own team or the carrier sent them.
    """
    from db import OutputExport
    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        prog_ids = [l.program_id for l in _links(s, bid)]
        if not prog_ids:
            return {"items": [], "total": 0}
        q = s.query(OutputExport).filter(OutputExport.broker_party_id == bid,
                                         OutputExport.program_id.in_(prog_ids))
        total = q.count()
        rows = (q.order_by(OutputExport.id.desc())
                 .offset((page - 1) * page_size).limit(page_size).all())
        return {"items": _run_rows(s, rows), "total": total}
