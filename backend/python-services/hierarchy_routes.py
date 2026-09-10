"""
The carrier hierarchy — Phase 1.

    Kavachio  (the platform — owns no book)
       └── Carrier ................ tenant
             ├── Carrier Admin .... app_user (tenant_id set)
             └── Programme ........ program
                   └── Broker ..... party (party_type = 'broker')
                         ├── Broker Admin . app_user (broker_party_id set)
                         │     └── Operator  app_user (broker_party_id set)
                         └── Contract ..... contract
                               └── Policy ... policy

Three facts shape every endpoint in this file:

 1. ONE PROGRAMME HAS MANY BROKERS, and ONE BROKER IS ON MANY PROGRAMMES.
    The pair lives in `program_broker`, never as a column on either side.
    That table is also the GATE: no row, no contract on that programme.

 2. A BROKER IS A `party`, NOT A `tenant`, because the same broker produces for
    several carriers. Which is why a broker cannot be reached by tenant_id and
    a carrier can only ever see the brokers on its own programmes.

 3. THERE IS EXACTLY ONE APPROVAL: a contract a BROKER uploads waits for its
    carrier; a contract the CARRIER uploads is live immediately. Nothing else
    in the platform is ever approved. The decision itself is set by the DB
    trigger from *who submitted it* — a client can never assert "approved".
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, String

from db import (
    carrier_broker_ids, link_carrier_broker,
    Tenant,
    BrokerInvitation,
    SessionLocal, Party, Program, Contract, AppUser,
    ProgramBroker, ContractApproval,
)
from auth_deps import current_principal, require_role, Principal, resolve_broker_party_id
from app_routes import (
    resolve_tenant_id, assert_tenant_owns, _iso_utc, PRODUCER_PARTY_TYPES,
)

router = APIRouter()


# =============================================================================
#  Helpers
# =============================================================================

def _broker_dict(p: Party) -> dict:
    return {
        "id": p.id,
        "legal_name": p.legal_name,
        "dba_name": p.dba_name,
        "party_type": p.party_type,
        "is_active": p.is_active,
        # Has this broker come on board? Derived by the database from their
        # people, so it is a read here and never something the API decides.
        "onboarding_status": p.onboarding_status,
        "created_at": _iso_utc(p.created_at),
    }


# ── Feature gate: one broker, several carrier organisations ─────────────────
# Default OFF, so the behaviour is exactly what it was: an email that already
# belongs to a broker admin is refused, every carrier onboards its own brokers,
# and a broker holds one login for one carrier.
#
# Switched ON, the same address can be invited by a second carrier: the broker
# keeps the login they have, answers the invitation themselves, and works with
# both — which is the normal shape of the market, but a real change to who can
# see whom, so it is opt-in rather than assumed.
#
# Nothing else is gated. `carrier_broker` rows, the invitation table and the
# accept flow all behave the same either way; with the flag off a broker simply
# never accumulates a second carrier. That keeps ONE code path rather than two,
# so the flag cannot rot into a branch nobody exercises.
def multi_carrier_brokers_enabled() -> bool:
    import os
    return os.getenv("MULTI_CARRIER_BROKERS", "").strip().lower() in (
        "1", "true", "on", "yes",
    )


def _invited_broker_ids(session, tenant_id: int) -> set[int]:
    """Brokers this carrier works with. Now one lookup in `carrier_broker`.

    It used to be derived from `broker_invitation.status = 'accepted'` — a rule
    every query had to know, and an invitation is an EVENT while working
    together is a FACT that outlives it. Kept as a thin wrapper so the call
    sites read the same; the definition lives in db.carrier_broker_ids.
    """
    return carrier_broker_ids(session, tenant_id)


def _assert_broker(session, party_id: int, tenant_id: int) -> Party:
    """Fetch a party and prove it can produce on this carrier's programmes.

    "Broker" is the slot, not the party type: an MGA, MGU or TPA occupies it on
    exactly the same terms, which is why the test is PRODUCER_PARTY_TYPES rather
    than the single word. A reinsurer or another carrier is still refused.

    404 rather than 403 throughout: a carrier must not be able to probe which
    party ids exist in another carrier's directory.
    """
    party = session.get(Party, party_id)
    if not party:
        raise HTTPException(404, "broker not found")
    if (party.party_type or "").lower() not in PRODUCER_PARTY_TYPES:
        raise HTTPException(
            400,
            f"party {party_id} is a {party.party_type} — only "
            f"{', '.join(PRODUCER_PARTY_TYPES)} can be put on a programme")
    # A broker is visible to a carrier because the carrier created it
    # (tenant_id matches), because it sits on one of the carrier's programmes,
    # or because it ACCEPTED that carrier's invitation — which is the case for
    # every broker shared with another carrier, and the reason they can be put
    # on a programme at all.
    if party.tenant_id != tenant_id:
        reachable = (
            session.query(ProgramBroker.id)
            .filter(ProgramBroker.broker_party_id == party_id,
                    ProgramBroker.tenant_id == tenant_id)
            .first()
            or party_id in carrier_broker_ids(session, tenant_id)
        )
        if not reachable:
            raise HTTPException(404, "broker not found")
    return party


def _assert_programme(session, program_id: int, principal: Principal, tenant_id: int) -> Program:
    prog = session.get(Program, program_id)
    if not prog:
        raise HTTPException(404, "programme not found")
    assert_tenant_owns(principal, prog.tenant_id)
    return prog


# =============================================================================
#  BROKERS ON A PROGRAMME  —  the many-to-many, and the gate
# =============================================================================

class BrokerAssignBody(BaseModel):
    broker_party_id: int


@router.get("/programs/{program_id}/brokers")
def programme_brokers(program_id: int, principal: Principal = Depends(current_principal)):
    """Every broker on this programme, with how much each one holds.

    The counts are what make the screen answerable: "can I take this broker
    off?" is really "what happens to their contracts?".
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)
        _assert_programme(s, program_id, principal, tid)

        rows = (
            s.query(ProgramBroker, Party)
            .join(Party, Party.id == ProgramBroker.broker_party_id)
            .filter(ProgramBroker.program_id == program_id)
            .order_by(Party.legal_name)
            .all()
        )
        out = []
        for link, party in rows:
            contracts = (
                s.query(func.count(Contract.id))
                .filter(Contract.program_id == program_id,
                        Contract.broker_party_id == party.id)
                .scalar() or 0
            )
            # `approved` used to be counted apart from `contract_count`, which
            # included contracts still waiting on the carrier's decision. There
            # is no such wait any more — a contract exists or it does not — so
            # the two questions have one answer.
            d = _broker_dict(party)
            d.update({
                "link_id": link.id,
                "status": link.status,
                "assigned_at": _iso_utc(link.created_at),
                "contract_count": contracts,
                "approved_contract_count": contracts,
            })
            out.append(d)
        return out


@router.post("/programs/{program_id}/brokers")
def programme_broker_add(program_id: int, body: BrokerAssignBody,
                         principal: Principal = Depends(require_role("carrier_admin"))):
    """Put a broker on a programme. This is the act that lets them produce.

    Re-assigning a broker that was previously removed REACTIVATES the existing
    row rather than inserting a second one — the pair is unique, and the
    history of when it was first assigned is worth keeping.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)
        _assert_programme(s, program_id, principal, tid)
        party = _assert_broker(s, body.broker_party_id, tid)

        existing = (
            s.query(ProgramBroker)
            .filter(ProgramBroker.program_id == program_id,
                    ProgramBroker.broker_party_id == party.id)
            .first()
        )
        if existing:
            if existing.status == "active":
                raise HTTPException(409, f"{party.legal_name} is already on this programme")
            existing.status = "active"
            existing.assigned_by_user_id = principal.user_id
            s.commit()
            return {"ok": True, "reactivated": True, "link_id": existing.id}

        link = ProgramBroker(
            tenant_id=tid,
            program_id=program_id,
            broker_party_id=party.id,
            status="active",
            assigned_by_user_id=principal.user_id,
        )
        s.add(link)
        # Producing on a programme is the strongest evidence there is that the
        # two work together, so it records the relationship as well. Idempotent
        # — the usual case is that it already exists.
        link_carrier_broker(s, tid, party.id, origin="programme",
                            by_user_id=principal.user_id)
        s.commit()
        return {"ok": True, "reactivated": False, "link_id": link.id}


@router.delete("/programs/{program_id}/brokers/{broker_party_id}")
def programme_broker_remove(program_id: int, broker_party_id: int,
                            principal: Principal = Depends(require_role("carrier_admin"))):
    """Take a broker off a programme.

    A pair that already carries contracts is never deleted — it is marked
    inactive, so the contracts underneath keep their meaning and the history of
    the relationship survives. Only a pair that never produced anything is
    removed outright.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)
        _assert_programme(s, program_id, principal, tid)

        link = (
            s.query(ProgramBroker)
            .filter(ProgramBroker.program_id == program_id,
                    ProgramBroker.broker_party_id == broker_party_id)
            .first()
        )
        if not link:
            raise HTTPException(404, "that broker is not on this programme")

        contracts = (
            s.query(func.count(Contract.id))
            .filter(Contract.program_id == program_id,
                    Contract.broker_party_id == broker_party_id)
            .scalar() or 0
        )
        if contracts:
            link.status = "inactive"
            s.commit()
            return {"ok": True, "deactivated": True, "contract_count": contracts,
                    "message": f"Kept because {contracts} contract(s) sit under it. "
                               f"They stay readable; no new work can start."}
        s.delete(link)
        s.commit()
        return {"ok": True, "deactivated": False, "contract_count": 0}


# =============================================================================
#  THE CARRIER'S BROKER DIRECTORY
# =============================================================================

@router.get("/brokers")
def broker_directory(principal: Principal = Depends(current_principal)):
    """Every broker this carrier works with, and how far each one reaches.

    Sourced from program_broker, not from party.tenant_id: a broker the carrier
    did not create still belongs on this list the moment it is put on one of
    the carrier's programmes.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)

        links = (
            s.query(ProgramBroker, Program, Party)
            .join(Program, Program.id == ProgramBroker.program_id)
            .join(Party, Party.id == ProgramBroker.broker_party_id)
            .filter(ProgramBroker.tenant_id == tid)
            .all()
        )
        by_broker: dict[int, dict] = {}
        for link, prog, party in links:
            d = by_broker.setdefault(party.id, {**_broker_dict(party),
                                                "programmes": [], "contract_count": 0,
                                                "user_count": 0})
            d["programmes"].append({"id": prog.id, "name": prog.name, "status": link.status})

        # A broker the carrier created but has not yet put on any programme is
        # still in the directory — it is exactly the state the UI must show as
        # "not on a programme yet", not hide.
        # Brokers this carrier created, PLUS brokers who accepted its
        # invitation — a shared broker has no party row of this carrier's and
        # no programme link until one is added, so without the second half they
        # would be invisible to the carrier who just invited them.
        invited_ids = _invited_broker_ids(s, tid)
        # …AND brokers who have been invited but have not answered. Leaving
        # those out was a dead end: the carrier saw nothing, invited again, and
        # got "you have already invited them" from a screen that showed no such
        # invitation. An outstanding invitation is a thing that HAPPENED — it
        # belongs on the list, marked as unanswered.
        pending = (s.query(BrokerInvitation)
                   .filter(BrokerInvitation.tenant_id == tid,
                           BrokerInvitation.status == "pending")
                   .order_by(BrokerInvitation.created_at.desc())
                   .all())
        pending_by_party = {i.party_id: i for i in pending if i.party_id}

        unassigned = (
            s.query(Party)
            .filter(or_(Party.tenant_id == tid,
                        Party.id.in_((invited_ids | set(pending_by_party)) or {-1})),
                    # party_type is the enum party_type_e — its labels are
                    # already lowercase, so cast to text rather than lower().
                    func.cast(Party.party_type, String) == "broker",
                    ~Party.id.in_([b for b in by_broker] or [-1]))
            .all()
        )
        for party in unassigned:
            by_broker[party.id] = {**_broker_dict(party), "programmes": [],
                                   "contract_count": 0, "user_count": 0}

        # THE SAME BROKER READS DIFFERENTLY TO DIFFERENT CARRIERS, and that is
        # the point: one who accepted carrier A and has not answered carrier B
        # is active on A's screen and invited on B's. The status is the
        # relationship with THIS carrier, not a property of the broker.
        for pid, d in by_broker.items():
            inv = pending_by_party.get(pid)
            d["relationship"] = "invited" if inv else "active"
            d["invitation"] = ({"id": inv.id, "email": inv.email,
                                "invited_at": _iso_utc(inv.created_at)}
                               if inv else None)

        # An invitation with no party is not listed. Inviting a NEW broker
        # creates the organisation immediately, so the only way to have one is
        # an address belonging to somebody who is not a broker at all — which
        # can never be accepted, and does not belong in a directory of brokers.
        for pid, d in by_broker.items():
            d["contract_count"] = (
                s.query(func.count(Contract.id))
                .filter(Contract.tenant_id == tid, Contract.broker_party_id == pid)
                .scalar() or 0
            )
            d["user_count"] = (
                s.query(func.count(AppUser.id))
                .filter(AppUser.broker_party_id == pid).scalar() or 0
            )
        # Newest first. A carrier scanning this list is almost always looking
        # for the one they just added — alphabetical put it wherever its name
        # happened to fall, which on a long list is nowhere near the top.
        # `created_at` is an ISO string here; a missing one sorts last rather
        # than crashing the comparison.
        return sorted(by_broker.values(),
                      key=lambda b: (b.get("created_at") or "",
                                     (b["legal_name"] or "").lower()),
                      reverse=True)


class NewBrokerBody(BaseModel):
    legal_name: str
    party_type: str = "broker"
    # The broker's FIRST admin. Optional, but leaving it out is what produces a
    # broker nobody can sign in as — the screen says so.
    admin_name: Optional[str] = None
    admin_email: Optional[str] = None
    # Put them straight onto a programme. Optional, because a carrier may add a
    # broker to its directory before deciding which programme they belong on.
    program_id: Optional[int] = None


def _join_link(invitation_id: int) -> str:
    """Where "Join now" lands: the invitation itself, inside the app.

    Not a tokened link. The recipient already has a login, and the screen
    refuses any invitation not addressed to whoever is signed in — so the id in
    the URL opens nothing on its own, and a forwarded email is useless to
    anybody else.
    """
    import os
    base = os.getenv("APP_BASE_URL", "http://localhost:5173").rstrip("/")
    return f"{base}/invitations?id={invitation_id}"


# There is no "add an existing broker" endpoint, deliberately. It worked by
# telling the carrier that an address already belonged to a broker — which is a
# relationship between that broker and whichever carriers onboarded them, and
# not the next carrier's to learn by typing an address into a form. Inviting
# now covers both cases without the carrier ever finding out which one they are
# in: see broker_create, and BrokerInvitation.


@router.post("/brokers")
def broker_create(body: NewBrokerBody,
                  principal: Principal = Depends(require_role("carrier_admin"))):
    """Bring a broker on board: the organisation, its first admin, and
    optionally the programme it produces into — in ONE step.

    These three used to be three separate screens, and the middle one had
    nowhere to start from: inviting a broker admin needed a broker that only
    the Brokers screen could create, and the Brokers screen had no way to
    create one. So a carrier could not onboard a broker at all.

    They belong together anyway. A broker organisation with no admin is a name
    nobody can sign in as, and a broker on no programme cannot produce. Doing
    all three at once means what you end up with actually works.
    """
    from app_routes import (_make_invite_link, _send_invite_email,
                            _send_carrier_invite_email)

    name = (body.legal_name or "").strip()
    if not name:
        raise HTTPException(400, "Give the broker a name.")
    if body.party_type not in PRODUCER_PARTY_TYPES:
        raise HTTPException(422, f"party_type must be one of {sorted(PRODUCER_PARTY_TYPES)}")
    email = (body.admin_email or "").strip().lower()

    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)

        if not email:
            raise HTTPException(400, {
                "message": "Give the broker admin's email — the invitation "
                           "goes to a person, not to an organisation.",
                "errors": {"admin_email": "required"}})
        # NO PROGRAMME. A broker is invited to work with the CARRIER, not with
        # one of its programmes: which programmes they produce on is a decision
        # the carrier goes on making for years, and making the first one part
        # of onboarding forces it before either side knows the answer. So the
        # invitation says "come and work with us", the accepted invitation IS
        # the relationship, and programmes are added afterwards from the
        # programme's own screen — as many times as needed.
        if body.program_id:
            _assert_programme(s, body.program_id, principal, tid)

        # ── facts about THIS carrier's own book: safe to state plainly ──
        dup = (s.query(BrokerInvitation)
               .filter(BrokerInvitation.tenant_id == tid,
                       func.lower(BrokerInvitation.email) == email,
                       BrokerInvitation.status == "pending")
               .first())
        if dup:
            raise HTTPException(409, {
                "message": f"You have already invited {email}. They have not "
                           f"answered yet.",
                "errors": {"admin_email": "already invited"}})

        # ── facts about somebody ELSE's book: never stated, never implied ──
        #
        # From here the two cases diverge and the carrier is told NOTHING about
        # which one they are in. Whether this address already has a login is a
        # relationship between that person and whichever carriers onboarded
        # them; the next carrier does not get to discover it by typing an
        # address into a form. Both branches end at the same response.
        existing_user = s.query(AppUser).filter(AppUser.email == email).first()
        existing_party = (s.get(Party, existing_user.broker_party_id)
                          if existing_user and existing_user.broker_party_id else None)
        is_existing_broker = bool(
            existing_party
            and (existing_party.party_type or "").lower() in PRODUCER_PARTY_TYPES)

        if is_existing_broker and not multi_carrier_brokers_enabled():
            # The old behaviour, and the default. A broker belongs to the
            # carrier that onboarded them, so a second carrier cannot reach
            # them at all — the address is simply taken.
            raise HTTPException(409, {
                "message": "That email is already in use.",
                "errors": {"admin_email": "taken"}})

        admin, link, party = None, None, None
        if is_existing_broker:
            # They exist. Nothing is created — no organisation, no login, and
            # no programme link. The invitation waits on THEIR screen, and the
            # link appears when they accept it. A carrier can no longer put a
            # broker on a programme by unilateral act.
            party = existing_party
            already_ours = party.id in carrier_broker_ids(s, tid)
            if already_ours:
                # Their own book again — this one they can be told.
                raise HTTPException(409, {
                    "message": "You already work with that broker.",
                    "errors": {"admin_email": "already yours"}})
        else:
            # New to the platform, OR an address belonging to somebody who is
            # not a broker (a carrier's own staff, say). Both are handled the
            # same way on purpose: a party can only be created when the email
            # is genuinely free, and the difference between "new" and "taken by
            # a non-broker" is not the inviting carrier's business either.
            if existing_user:
                # The address cannot become a broker login. The invitation is
                # recorded and simply never accepted — indistinguishable from
                # one nobody got round to answering, which is the point.
                s.add(BrokerInvitation(
                    tenant_id=tid, program_id=body.program_id, email=email,
                    org_name=name, status="pending",
                    by_user_id=principal.user_id))
                s.commit()
                return {"ok": True, "invited": True, "email": email,
                        "message": f"Invitation sent to {email}."}

            if s.query(Party).filter(Party.tenant_id == tid,
                                     func.lower(Party.legal_name) == name.lower()).first():
                raise HTTPException(409, {
                    "message": f"You already work with a broker called {name}.",
                    "errors": {"legal_name": "duplicate"}})

            party = Party(tenant_id=tid, party_type=body.party_type,
                          legal_name=name, scope="tenant",
                          is_app_managed=True, is_active=True)
            s.add(party); s.flush()
            admin = AppUser(
                email=email,
                full_name=(body.admin_name or "").strip() or email.split("@")[0].title(),
                role="broker_admin", status="invited",
                # A broker seat belongs to the broker and to NO carrier — the
                # same broker produces for several (chk_app_user_scope).
                tenant_id=None, broker_party_id=party.id,
                invited_by_user_id=principal.user_id)
            s.add(admin)
            link = _make_invite_link(admin)
            # This carrier brought them on, so they work together from now —
            # the invitation that follows is accepted as onboarding completes,
            # but the relationship does not wait on that to be true.
            link_carrier_broker(s, tid, party.id, origin="onboarded",
                                by_user_id=principal.user_id)

        invitation = BrokerInvitation(
            tenant_id=tid, program_id=body.program_id, email=email,
            party_id=party.id if party else None, org_name=name,
            status="pending", by_user_id=principal.user_id)
        s.add(invitation)
        s.commit()

        if admin and link:
            # New: hand them an account. "Complete onboarding."
            s.refresh(party)
            _send_invite_email(admin.email, link, admin.full_name, party.legal_name)
        elif is_existing_broker:
            # Already has a login: ask them a question. "Join now" drops them on
            # the invitation screen, signed in as themselves — no password, no
            # expiry. Without this the invitation sat silently on a dashboard
            # they had no reason to open.
            s.refresh(invitation)
            me = s.query(Tenant).filter(Tenant.id == tid).first()
            _send_carrier_invite_email(
                email, _join_link(invitation.id),
                existing_user.full_name if existing_user else None,
                (me.legal_name or me.tenant_name) if me else None)

        # ONE response shape for both branches. A carrier comparing two
        # invitations must not be able to tell which broker already existed.
        return {"ok": True, "invited": True, "email": email,
                "message": f"Invitation sent to {email}. They start working "
                           f"with you once they accept — put them on "
                           f"programmes after that."}





@router.post("/broker-invitations/{invitation_id}/resend")
def broker_invitation_resend(invitation_id: int,
                             principal: Principal = Depends(require_role("carrier_admin"))):
    from app_routes import _make_invite_link, _send_invite_email
    """Send an outstanding invitation again.

    The way out of a dead end. Inviting twice is refused — one invitation per
    broker — so without this the only thing a carrier could do about an
    unanswered invitation was nothing.

    WHAT IT ACTUALLY DOES depends, again, on facts the carrier is not shown. A
    broker who has never signed in gets a fresh invite link by email, because
    the old one may have expired. One who already has a login gets nothing sent
    — the invitation is sitting on their own screen and always was, and mailing
    them about it would be this carrier telling them something about their own
    account. Both answer the same way.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)
        inv = s.get(BrokerInvitation, invitation_id)
        if not inv or inv.tenant_id != tid:
            raise HTTPException(404, "invitation not found")
        if inv.status != "pending":
            raise HTTPException(409, {
                "message": f"That invitation was already {inv.status}.",
                "errors": {"invitation": inv.status}})

        u = (s.query(AppUser)
             .filter(func.lower(AppUser.email) == (inv.email or "").lower())
             .first())
        if u and (u.status or "") == "invited" and u.broker_party_id:
            # Never signed in: the link is how they get in at all, and it may
            # have expired since.
            link = _make_invite_link(u)
            party = s.get(Party, u.broker_party_id)
            s.commit()
            _send_invite_email(u.email, link, u.full_name,
                               party.legal_name if party else "")
        else:
            # Already has a login: the same "Join now" they got the first time.
            me = s.query(Tenant).filter(Tenant.id == tid).first()
            s.commit()
            _send_carrier_invite_email(
                inv.email, _join_link(inv.id), u.full_name if u else None,
                (me.legal_name or me.tenant_name) if me else None)
        return {"ok": True,
                "message": f"Invitation to {inv.email} sent again."}


@router.delete("/broker-invitations/{invitation_id}")
def broker_invitation_revoke(invitation_id: int,
                             principal: Principal = Depends(require_role("carrier_admin"))):
    """Withdraw an invitation nobody has answered.

    Only a pending one. An accepted invitation is the relationship itself, and
    ending that is taking the broker off your programmes — a different act,
    with contracts underneath it.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)
        inv = s.get(BrokerInvitation, invitation_id)
        if not inv or inv.tenant_id != tid:
            raise HTTPException(404, "invitation not found")
        if inv.status != "pending":
            raise HTTPException(409, {
                "message": f"That invitation was already {inv.status}, so "
                           f"there is nothing to withdraw.",
                "errors": {"invitation": inv.status}})
        inv.status = "revoked"
        inv.answered_at = datetime.now(timezone.utc)
        s.commit()
        return {"ok": True, "message": f"Invitation to {inv.email} withdrawn."}


@router.get("/brokers/{broker_party_id}")
def broker_detail(broker_party_id: int, principal: Principal = Depends(current_principal)):
    """One broker: its programmes with this carrier, and its people.

    Deliberately scoped to THIS carrier. The same broker may produce far more
    business for someone else; none of that is this carrier's to see.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)
        party = _assert_broker(s, broker_party_id, tid)

        programmes = (
            s.query(ProgramBroker, Program)
            .join(Program, Program.id == ProgramBroker.program_id)
            .filter(ProgramBroker.broker_party_id == broker_party_id,
                    ProgramBroker.tenant_id == tid)
            .order_by(Program.name)
            .all()
        )
        users = (
            s.query(AppUser)
            .filter(AppUser.broker_party_id == broker_party_id)
            .order_by(AppUser.full_name)
            .all()
        )
        return {
            **_broker_dict(party),
            "programmes": [
                {"id": p.id, "name": p.name, "status": link.status,
                 "assigned_at": _iso_utc(link.created_at)}
                for link, p in programmes
            ],
            # Contracts are NOT here: a broker of any size has hundreds and
            # the screen shows ten. See broker_contracts below, which filters
            # and pages them in SQL.
            #
            # Their people, but never their password/reset columns.
            "users": [
                {"id": u.id, "full_name": u.full_name, "email": u.email,
                 "role": u.role, "status": u.status,
                 "accepted_at": _iso_utc(u.accepted_at)}
                for u in users
            ],
        }


def _broker_contract_dict(c: Contract) -> dict:
    return {
        # `name` as well as `filename`: a contract WRITTEN in Kavachio has no
        # file, so this list showed it as "Contract 1459" beside uploads that
        # showed their own names. It has a name — the one the carrier typed —
        # and it is what everything else calls it.
        "id": c.id, "name": c.name, "filename": c.filename,
        "program_id": c.program_id,
        # Whether it is app-managed decides where its name should lead: a
        # written contract's home is its own record, not the page that reads
        # clauses out of an uploaded document.
        "is_app_managed": bool(c.is_app_managed),
        "status": c.status,
        "inception_dt": str(c.inception_dt) if c.inception_dt else None,
        "expiry_dt": str(c.expiry_dt) if c.expiry_dt else None,
        "created_at": _iso_utc(c.created_at),
    }


@router.get("/brokers/{broker_party_id}/contracts")
def broker_contracts(broker_party_id: int,
                     q: Optional[str] = Query(None, description="match on name, UMR or filename"),
                     program_id: Optional[int] = Query(None),
                     limit: int = Query(10, ge=1, le=100),
                     offset: int = Query(0, ge=0),
                     principal: Principal = Depends(current_principal)):
    """One page of what this broker holds with this carrier.

    Split out of broker_detail so the filters and the LIMIT run in SQL. The
    page above it — programmes, people, the header — does not change as the
    table is searched or paged, so it is fetched once and left alone.

    `total` is the count AFTER filtering, which is what the pager counts
    pages from; an unfiltered total would offer pages that come back empty.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)
        # Same scope check as the detail page: a carrier can only ever see a
        # broker that is on one of its own programmes.
        _assert_broker(s, broker_party_id, tid)

        query = s.query(Contract).filter(
            Contract.broker_party_id == broker_party_id,
            Contract.tenant_id == tid)
        if program_id:
            query = query.filter(Contract.program_id == program_id)
        if q and q.strip():
            like = f"%{q.strip()}%"
            query = query.filter(or_(Contract.name.ilike(like),
                                     Contract.umr.ilike(like),
                                     Contract.filename.ilike(like)))

        total = query.count()
        # id as the tiebreak: two contracts added in the same second would
        # otherwise be free to swap places between pages, which shows one of
        # them twice and hides the other.
        rows = (query.order_by(Contract.created_at.desc(), Contract.id.desc())
                .limit(limit).offset(offset).all())
        return {"contracts": [_broker_contract_dict(c) for c in rows],
                "total": total}

# =============================================================================
#  THE NEGOTIATION THREAD
# =============================================================================
#
# The carrier's approve/reject gate used to live here: a broker brought a
# contract and it could not be used until the carrier said so. That gate is
# gone, along with the broker-side upload it existed to police — a contract is
# now raised by the carrier alone, so there was nobody left to approve it and
# nothing for the queue to hold.
#
# What remains is the part that was never about permission: the thread of what
# the two sides said to each other — sent for review, changes requested, terms
# agreed — which the contract record still reads.

@router.get("/contracts/{contract_id}/approvals")
def contract_approval_history(contract_id: int,
                              principal: Principal = Depends(current_principal)):
    """How this contract got to where it is — every decision, oldest first.

    THE BROKER READS THIS TOO. It is not an audit log for the carrier: it is
    the negotiation thread, carrying every change request and the terms it
    named. Scoping it to the owning tenant meant the one party who has to
    ANSWER a change request could not see it — they saw an empty trail on a
    contract they had themselves pushed back on.
    """
    with SessionLocal() as s:
        c = s.get(Contract, contract_id)
        if not c:
            raise HTTPException(404, "contract not found")
        if principal.is_broker:
            # Their own contracts only. A shared programme carries other
            # brokers' contracts and none of them are this broker's business.
            # Resolved from the database — the token carries no broker party.
            bid = resolve_broker_party_id(s, principal)
            if bid is None or c.broker_party_id != bid:
                raise HTTPException(404, "contract not found")
        else:
            assert_tenant_owns(principal, c.tenant_id)

        rows = (
            s.query(ContractApproval, AppUser)
            .outerjoin(AppUser, AppUser.id == ContractApproval.acted_by_user_id)
            .filter(ContractApproval.contract_id == contract_id)
            .order_by(ContractApproval.acted_at.asc())
            .all()
        )
        return [
            {"action": a.action, "note": a.note, "acted_at": _iso_utc(a.acted_at),
             "acted_by": {"id": u.id, "full_name": u.full_name, "email": u.email} if u else None,
             # The counter-proposal, when this row is one. Carried here because
             # this endpoint IS the negotiation thread: a change request without
             # the terms it named is half the story.
             "proposed_changes": a.proposed_changes or []}
            for a, u in rows
        ]


# =============================================================================
#  THE HIERARCHY ITSELF  —  one call the UI can draw the tree from
# =============================================================================

@router.get("/hierarchy")
def hierarchy(principal: Principal = Depends(current_principal)):
    """The whole carrier tree in one response: programmes → brokers → contracts.

    One call rather than N+1 from the client, because every screen in the
    Phase 1 figma draws some slice of this same shape.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)

        programmes = (
            s.query(Program)
            .filter(Program.tenant_id == tid)
            # Newest first — this is the list the Programmes screen renders.
            .order_by(Program.created_at.desc().nullslast(),
                      Program.id.desc())
            .all()
        )
        prog_ids = [p.id for p in programmes] or [-1]

        links = (
            s.query(ProgramBroker, Party)
            .join(Party, Party.id == ProgramBroker.broker_party_id)
            .filter(ProgramBroker.program_id.in_(prog_ids))
            .all()
        )
        contracts = (
            s.query(Contract)
            .filter(Contract.program_id.in_(prog_ids))
            .all()
        )

        by_prog: dict[int, list] = {}
        for link, party in links:
            by_prog.setdefault(link.program_id, []).append((link, party))

        tree = []
        for p in programmes:
            brokers = []
            for link, party in sorted(by_prog.get(p.id, []),
                                      key=lambda t: (t[1].legal_name or "").lower()):
                bc = [c for c in contracts
                      if c.program_id == p.id and c.broker_party_id == party.id]
                brokers.append({
                    "id": party.id,
                    "legal_name": party.legal_name,
                    "link_status": link.status,
                    "contracts": [
                        {"id": c.id, "filename": c.filename, "status": c.status}
                        for c in sorted(bc, key=lambda c: c.id)
                    ],
                })
            tree.append({
                "id": p.id,
                "name": p.name,
                "status": p.status,
                # What kind of business it is, so the Programmes list can say
                # more than a name — the same two fields the create screen asks.
                "business_segment": p.business_segment,
                "product_line": p.product_line,
                "bdx_frequency": p.bdx_frequency,
                "broker_count": len(brokers),
                "contract_count": sum(len(b["contracts"]) for b in brokers),
                "brokers": brokers,
            })
        return {"tenant_id": tid, "programmes": tree}
