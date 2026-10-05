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
    ProgramBroker, ContractApproval, Pipeline,
)
from auth_deps import current_principal, require_role, Principal, resolve_broker_party_id
from carrier_scope import (
    LINK_PENDING, assert_can_invite_brokers, is_carrier_admin_seat,
    needs_carrier_approval,
)
from app_routes import (
    resolve_tenant_id, assert_tenant_owns, _iso_utc, PRODUCER_PARTY_TYPES,
    _carrier_seat, _my_broker_party_ids,
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


def _contract_settled(s, c: Contract) -> bool:
    """Is this contract finished enough to build a bordereau setup on?

    THE SAME RULE direct_routes._pipeline_ready enforces at the gate that
    actually matters — a setup activation. Re-derived here rather than
    imported wholesale (a lazy import avoids a module cycle; hierarchy_routes
    and direct_routes do not import each other today) so the Programmes wizard
    can say so BEFORE a carrier user builds a setup on a contract nobody has
    signed and discovers the refusal only when they try to activate it.

    A contract written here is settled once the broker has agreed the terms
    AND the carrier has signed — not once BOTH sides have, which is
    deliberate: see [[carrier-admin-one-gate]]. One that was uploaded is
    settled once the carrier admin has accepted it. NULL lifecycle (a contract
    older than this column) reads as settled, exactly as the setup gate does —
    it must not retroactively block setups that have run for years.
    """
    try:
        from direct_routes import _CONTRACT_UNSETTLED, _carrier_has_signed
    except Exception:  # noqa: BLE001 — never blocks the tree from rendering
        return True
    state = (c.lifecycle or "").strip().lower()
    if state in _CONTRACT_UNSETTLED:
        return False
    if state == "agreed" and not _carrier_has_signed(s, c):
        return False
    return True


def _contract_awaiting_admin(s, c: Contract) -> bool:
    """Is THIS contract sitting on the carrier admin's desk right now?

    THE ONE ANSWER, read off contract_routes._carrier_admin_turn — the same
    function the contract record's banner, the dashboard's "Waiting on You"
    tile and the notification bell all read. Re-deriving "is it the admin's
    move" a second time here is exactly how those three came to disagree with
    each other before [[carrier-admin-one-gate]]; this must not become a
    fourth copy.

    Lazy import for the same reason as _contract_settled: no module cycle
    exists, but importing at call time keeps it that way regardless of load
    order between the two route files.
    """
    try:
        from contract_routes import (_carrier_admin_turn, _signatures,
                                     _unsigned_sides)
        return _carrier_admin_turn(c, _unsigned_sides(_signatures(s, c.id)))
    except Exception:  # noqa: BLE001 — never blocks the tree from rendering
        return False


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
def programme_brokers(program_id: int, mga: Optional[str] = None,
                      principal: Principal = Depends(current_principal)):
    """Every broker on this programme, with how much each one holds.

    The counts are what make the screen answerable: "can I take this broker
    off?" is really "what happens to their contracts?".

    `mga` names the carrier for Kavachio staff (read-only, from a carrier's
    Programs & Contracts tab); resolve_tenant_id ignores it for everyone else.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
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


# Both endpoints below are in audit._SELF_LOGGED, because each one now means
# two different things depending on who calls it: POST /brokers is an
# onboarding from the carrier admin and a REQUEST from a carrier user, and the
# middleware keys on the path alone. A path-keyed name would put "added a
# broker" in the trail on a day nobody added one. So the carrier admin's own
# acts are logged here, and the carrier user's requests in
# broker_onboarding_routes — under different names, so neither reader sees the
# same event twice or the wrong one once.

def _log_broker_act(s, tid: int, principal: Principal, action: str,
                    details: dict) -> None:
    """Best-effort audit row. A broker must not fail to be onboarded because
    the trail could not be written."""
    try:
        from audit import log_activity, actor_email
        log_activity(tid, actor_email(principal.user_id), action,
                     target=details.get("target"), details=details,
                     principal=principal)
    except Exception:  # noqa: BLE001
        pass


def _log_broker_onboarded(s, tid: int, principal: Principal, result: dict,
                          program_id: Optional[int]) -> None:
    """The carrier admin brought a broker on board themselves."""
    prog = s.get(Program, program_id) if program_id else None
    _log_broker_act(s, tid, principal, "broker_added", {
        "target": f"broker:{result.get('email')}",
        "name": result.get("email"),
        "email": result.get("email"),
        "program_id": program_id,
        "program_name": getattr(prog, "name", None),
    })


def _log_broker_linked(s, tid: int, principal: Principal, program_id: int,
                       party: Party, result: dict) -> None:
    """The carrier admin put a broker on a programme themselves."""
    prog = s.get(Program, program_id) if program_id else None
    _log_broker_act(s, tid, principal, "broker_put_on_programme", {
        "target": f"party:{party.id}",
        "name": party.legal_name,
        "broker_name": party.legal_name,
        "broker_party_id": party.id,
        "program_id": program_id,
        "program_name": getattr(prog, "name", None),
        "link_status": result.get("status"),
        "reactivated": result.get("reactivated"),
    })


def _do_programme_link(s, tid: int, program_id: int, party: Party,
                       by_user_id: int, status: str) -> dict:
    """Write the programme→broker pair. THE ACT ITSELF, with no gate in it.

    Pulled out of the route because it now has two callers that must do exactly
    the same thing: the carrier admin adding a broker directly, and the carrier
    admin APPROVING a colleague's request to. Any difference between those two
    would be a difference nobody asked for.

    Re-assigning a broker that was previously removed REACTIVATES the existing
    row rather than inserting a second one — the pair is unique, and the
    history of when it was first assigned is worth keeping.

    Does NOT commit: the approval path writes this and the decision on the
    request together, and half of that landing would be worse than neither.
    """
    existing = (
        s.query(ProgramBroker)
        .filter(ProgramBroker.program_id == program_id,
                ProgramBroker.broker_party_id == party.id)
        .first()
    )
    if existing:
        if existing.status in ("active", LINK_PENDING):
            raise HTTPException(409, f"{party.legal_name} is already on this programme")
        existing.status = status
        existing.assigned_by_user_id = by_user_id
        return {"ok": True, "reactivated": True, "link_id": existing.id,
                "status": status}

    link = ProgramBroker(
        tenant_id=tid,
        program_id=program_id,
        broker_party_id=party.id,
        status=status,
        assigned_by_user_id=by_user_id,
    )
    s.add(link)
    # Producing on a programme is the strongest evidence there is that the
    # two work together, so it records the relationship as well. Idempotent
    # — the usual case is that it already exists.
    link_carrier_broker(s, tid, party.id, origin="programme",
                        by_user_id=by_user_id)
    s.flush()
    return {"ok": True, "reactivated": False, "link_id": link.id,
            "status": status}


@router.post("/programs/{program_id}/brokers")
def programme_broker_add(program_id: int, body: BrokerAssignBody,
                         principal: Principal = Depends(require_role("carrier_admin"))):
    """Put a broker on a programme. This is the act that lets them produce.

    WHAT THIS DOES DEPENDS ON WHO ASKS, and the server is what knows:

      carrier admin   the pair is written and the link goes live, exactly as
                      before. Their own act IS the approval.
      carrier user    a REQUEST is written and the carrier admin is asked.
                      Nothing is linked and nothing is sent until they answer.

    ONE endpoint and ONE button, for the same reason pipeline_activate is one:
    the intent being expressed is the same either way — "this broker belongs on
    this programme" — and which of the two it turns into is not something the
    screen should have to work out and could get wrong.

    TWO GATES NOW STAND IN A ROW, and they are not the same gate:

      this one    WHO do we work with?   — releases the invitation
      setup 27    WHAT may they send?    — releases the programme

    So an approved request still writes the link at `pending_approval` for a
    carrier user's broker, and the Bordereau Setup approval still releases it
    (direct_routes._activate_links_for), untouched. Approving a broker opens
    the relationship; it does not put the programme in the broker's sight.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)
        _assert_programme(s, program_id, principal, tid)
        party = _assert_broker(s, body.broker_party_id, tid)

        if needs_carrier_approval(s, principal):
            from broker_onboarding_routes import raise_request_for_existing_broker
            return raise_request_for_existing_broker(
                s, tid, program_id, party, principal)

        result = _do_programme_link(s, tid, program_id, party,
                                    principal.user_id, "active")
        _log_broker_linked(s, tid, principal, program_id, party, result)
        s.commit()
        return result


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
def broker_directory(q: Optional[str] = None,
                     page: Optional[int] = Query(None, ge=1),
                     page_size: Optional[int] = Query(None, ge=1, le=200),
                     mine: bool = Query(False),
                     mga: Optional[str] = None,
                     principal: Principal = Depends(current_principal)):
    """Every broker this carrier works with, and how far each one reaches.

    `mga` names the carrier for Kavachio staff (read-only, from a carrier's
    Programs & Contracts tab); resolve_tenant_id ignores it for everyone else.

    Sourced from program_broker, not from party.tenant_id: a broker the carrier
    did not create still belongs on this list the moment it is put on one of
    the carrier's programmes.

    Pagination is OPT-IN. Without `page` this returns the plain list exactly as
    it always has — the Programmes screen and Add Programme both read the whole
    directory to offer it in a picker. With `page` it returns
    {"items", "total", "page", "page_size", "stranded"}.

    `stranded` counts brokers on NO programme across the whole directory, not
    the page: the Brokers screen states it above the table as a fact about the
    book, and a count that shrank as you paged would be a different sentence.

    `mine` is the Party screen's view (and its dashboard tile's). A carrier
    USER's Party list is the broker companies they invited themselves — the
    same reach their Users & Roles and dashboard already have
    (app_routes._my_broker_party_ids). The carrier admin's reach is the whole
    company, so for them it changes nothing. The programme pickers leave it
    off: which broker goes on a programme is company business, and a
    colleague's broker must stay assignable.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)

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

        if mine and _carrier_seat(s, principal) == "user":
            reach = _my_broker_party_ids(s, principal, tid)
            by_broker = {pid: d for pid, d in by_broker.items() if pid in reach}

        # THE SAME BROKER READS DIFFERENTLY TO DIFFERENT CARRIERS, and that is
        # the point: one who accepted carrier A and has not answered carrier B
        # is active on A's screen and invited on B's. The status is the
        # relationship with THIS carrier, not a property of the broker.
        for pid, d in by_broker.items():
            inv = pending_by_party.get(pid)
            d["relationship"] = "invited" if inv else "active"
            # by_user_id: who sent it. Only that carrier user — or the carrier
            # admin, who oversees them all — may resend or withdraw it, so the
            # screen offers those links to them alone.
            d["invitation"] = ({"id": inv.id, "email": inv.email,
                                "invited_at": _iso_utc(inv.created_at),
                                "by_user_id": inv.by_user_id}
                               if inv else None)

        # An invitation with no party is not listed. Inviting a NEW broker
        # creates the organisation immediately, so the only way to have one is
        # an address belonging to somebody who is not a broker at all — which
        # can never be accepted, and does not belong in a directory of brokers.
        #
        # Newest first. A carrier scanning this list is almost always looking
        # for the one they just added — alphabetical put it wherever its name
        # happened to fall, which on a long list is nowhere near the top.
        # `created_at` is an ISO string here; a missing one sorts last rather
        # than crashing the comparison.
        rows = sorted(by_broker.values(),
                      key=lambda b: (b.get("created_at") or "",
                                     (b["legal_name"] or "").lower()),
                      reverse=True)

        # Searched over the SAME two fields the screen searched in the browser,
        # so moving the search to the server did not quietly change what counts
        # as a match.
        if q and q.strip():
            needle = q.strip().lower()
            rows = [b for b in rows
                    if needle in (b["legal_name"] or "").lower()
                    or needle in (b["dba_name"] or "").lower()]

        stranded = sum(1 for b in rows if not b["programmes"])
        total = len(rows)
        if page is not None:
            size = page_size or 10
            rows = rows[(page - 1) * size:(page - 1) * size + size]

        # Two COUNTs per broker, and the reason they are down here rather than
        # in the loop above: paged, they run for the ten brokers being read
        # instead of for every broker the carrier holds.
        for d in rows:
            pid = d["id"]
            d["contract_count"] = (
                s.query(func.count(Contract.id))
                .filter(Contract.tenant_id == tid, Contract.broker_party_id == pid)
                .scalar() or 0
            )
            # Admins only: how many people the broker has is the broker's own
            # business, so the carrier is never told (the screen no longer
            # shows a people column at all).
            from auth_deps import db_role_values
            d["user_count"] = (
                s.query(func.count(AppUser.id))
                .filter(AppUser.broker_party_id == pid,
                        AppUser.role.in_(db_role_values("broker_admin"))).scalar() or 0
            )

        if page is not None:
            return {"items": rows, "total": total, "page": page,
                    "page_size": page_size or 10, "stranded": stranded}
        return rows


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


def check_broker_onboarding(s, tid: int, name: str, email: str):
    """Everything that can REFUSE bringing this broker on, decided without
    creating anything. Returns the resolved (existing_user, existing_party,
    is_existing_broker) trio for whoever is going to act on it.

    Its own function because the refusals now have to happen TWICE, in two
    places, with the same words: once when a carrier user asks (so they hear
    "that email is already in use" at the moment they type it, rather than
    their carrier admin hearing it days later and having to relay it), and
    again when the request is approved (because a colleague may have invited
    the same address in between). Two copies of these four rules would drift,
    and the third one would be the one that leaked whose broker this is.

    IT STILL REFUSES IN THE SAME VOICE, which is the part that matters. "That
    email is already in use" says nothing about whether the address belongs to
    a broker, to a carrier's own staff, or to nobody at all — see the comments
    below. A carrier user running this early learns exactly what they learned
    before, and nothing more.
    """
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
    existing_user = (s.query(AppUser)
                     .filter(func.lower(AppUser.email) == email).first())
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

    if is_existing_broker:
        if existing_party.id in carrier_broker_ids(s, tid):
            # Their own book again — this one they can be told.
            raise HTTPException(409, {
                "message": "You already work with that broker.",
                "errors": {"admin_email": "already yours"}})
    elif not existing_user:
        # A party can only be created when the email is genuinely free, so the
        # organisation name only has to be unique in that one case. Where the
        # address is taken by somebody who is not a broker, nothing is created
        # and the name is never used — see the branch in _do_broker_onboarding.
        if s.query(Party).filter(Party.tenant_id == tid,
                                 func.lower(Party.legal_name) == name.lower()).first():
            raise HTTPException(409, {
                "message": f"You already work with a broker called {name}.",
                "errors": {"legal_name": "duplicate"}})

    return existing_user, existing_party, is_existing_broker


def _do_broker_onboarding(s, tid: int, body: NewBrokerBody, name: str,
                          email: str,
                          by_user_id: int) -> tuple[dict, callable, Optional[int]]:
    """Bring the broker on board: the organisation, its first admin and the
    invitation. THE ACT ITSELF, with no gate in it.

    Pulled out of the route verbatim, for the same reason as
    _do_programme_link: the carrier admin doing it directly and the carrier
    admin APPROVING a colleague's request must do the identical thing. This is
    the "existing broker-onboarding flow" that the approval sits in FRONT of —
    it is not re-implemented anywhere, and nothing in it changed.

    Returns (response, send_mail, our_party_id). It does NOT commit and it does
    NOT send: the caller commits, then calls send_mail(). Mail goes out after
    the commit because an invitation the broker has been told about and the
    database has not is the one combination there is no way back from.

    `our_party_id` is the organisation THIS carrier just created, and None in
    every other case. It is the only safe answer to "may a programme link be
    written for them right now?": a broker who already had a login has agreed
    to nothing yet, and their link is written when they accept
    (broker_routes._accept_invitation) — never by the carrier alone.
    """
    from app_routes import (_make_invite_link, _send_invite_email,
                            _send_carrier_invite_email)

    existing_user, existing_party, is_existing_broker = check_broker_onboarding(
        s, tid, name, email)

    admin, link, party = None, None, None
    if is_existing_broker:
        # They exist. Nothing is created — no organisation, no login, and
        # no programme link. The invitation waits on THEIR screen, and the
        # link appears when they accept it. A carrier can no longer put a
        # broker on a programme by unilateral act.
        party = existing_party
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
                by_user_id=by_user_id))
            return ({"ok": True, "invited": True, "email": email,
                     "message": f"Invitation sent to {email}."},
                    lambda: None, None)

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
            invited_by_user_id=by_user_id)
        s.add(admin)
        link = _make_invite_link(admin)
        # This carrier brought them on, so they work together from now —
        # the invitation that follows is accepted as onboarding completes,
        # but the relationship does not wait on that to be true.
        link_carrier_broker(s, tid, party.id, origin="onboarded",
                            by_user_id=by_user_id)

    invitation = BrokerInvitation(
        tenant_id=tid, program_id=body.program_id, email=email,
        party_id=party.id if party else None, org_name=name,
        status="pending", by_user_id=by_user_id)
    s.add(invitation)
    s.flush()

    # Everything the mail needs, read off the rows NOW, while the session is
    # open and they are certainly loaded. The thunk runs after the commit and
    # must not touch the session — a lazy load from a closed one is exactly the
    # sort of failure that would swallow the invitation.
    if admin and link:
        # New: hand them an account. "Complete onboarding."
        to, nm, org = admin.email, admin.full_name, party.legal_name

        def send_mail() -> None:
            _send_invite_email(to, link, nm, org)
    elif is_existing_broker:
        # Already has a login: ask them a question. "Join now" drops them on
        # the invitation screen, signed in as themselves — no password, no
        # expiry. Without this the invitation sat silently on a dashboard
        # they had no reason to open.
        me = s.query(Tenant).filter(Tenant.id == tid).first()
        join, nm = _join_link(invitation.id), (existing_user.full_name
                                              if existing_user else None)
        carrier = (me.legal_name or me.tenant_name) if me else None

        def send_mail() -> None:
            _send_carrier_invite_email(email, join, nm, carrier)
    else:
        def send_mail() -> None:
            return None

    # ONE response shape for both branches. A carrier comparing two
    # invitations must not be able to tell which broker already existed.
    return ({"ok": True, "invited": True, "email": email,
             "message": f"Invitation sent to {email}. They start working "
                        f"with you once they accept — put them on "
                        f"programmes after that."},
            send_mail, (party.id if admin is not None else None))


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

    WHO ASKS DECIDES WHAT HAPPENS, as on the programme endpoint above:

      carrier admin   the broker is onboarded and the invitation goes out, as
                      it always did. Their own act IS the approval.
      carrier user    a REQUEST is written and the carrier admin is asked. No
                      organisation, no login, no relationship and above all NO
                      EMAIL — the broker never learns they were considered
                      unless the answer is yes.

    The refusals still happen HERE either way, at the moment the address is
    typed (check_broker_onboarding). A carrier user finding out a week later,
    through their admin, that the email was taken is not an improvement on
    finding out immediately.

    The CARRIER's, not Kavachio's — see assert_can_invite_brokers. require_role
    passes a platform admin through every carrier gate, so without this the
    only thing stopping them was resolve_tenant_id's "select a tenant" 400.
    """
    assert_can_invite_brokers(principal)

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
                "message": "Give the broker's email — the invitation "
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

        if needs_carrier_approval(s, principal):
            from broker_onboarding_routes import raise_request_to_invite
            return raise_request_to_invite(s, tid, body, name, email, principal)

        result, send_mail, _ours = _do_broker_onboarding(
            s, tid, body, name, email, principal.user_id)
        _log_broker_onboarded(s, tid, principal, result, body.program_id)
        s.commit()

    send_mail()
    return result





@router.post("/broker-invitations/{invitation_id}/resend")
def broker_invitation_resend(invitation_id: int,
                             principal: Principal = Depends(require_role("carrier_admin"))):
    from app_routes import (_make_invite_link, _send_invite_email,
                            _send_carrier_invite_email)
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
    assert_can_invite_brokers(principal)
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)
        inv = s.get(BrokerInvitation, invitation_id)
        if not inv or inv.tenant_id != tid:
            raise HTTPException(404, "invitation not found")
        # A carrier user chases or calls off only the invitations they sent;
        # another carrier user's broker is not theirs. The carrier admin
        # oversees every invitation the company has out.
        if _carrier_seat(s, principal) == "user" and inv.by_user_id != principal.user_id:
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
    assert_can_invite_brokers(principal)
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)
        inv = s.get(BrokerInvitation, invitation_id)
        if not inv or inv.tenant_id != tid:
            raise HTTPException(404, "invitation not found")
        # A carrier user chases or calls off only the invitations they sent;
        # another carrier user's broker is not theirs. The carrier admin
        # oversees every invitation the company has out.
        if _carrier_seat(s, principal) == "user" and inv.by_user_id != principal.user_id:
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
        # The broker's ADMIN only. Its users (operators) belong to the broker
        # alone — the carrier never sees them, here or anywhere else.
        from auth_deps import db_role_values
        users = (
            s.query(AppUser)
            .filter(AppUser.broker_party_id == broker_party_id,
                    AppUser.role.in_(db_role_values("broker_admin")))
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
            # Their admin, but never their password/reset columns.
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
        from auth_deps import normalize_role
        _company: dict[int, Optional[str]] = {}

        def _acted_by(u):
            if u is None:
                return None
            # The carrier deals with the broker COMPANY and never sees its
            # broker users. A row written by one (possible before only the
            # broker admin could answer a review) is shown to a carrier seat
            # under the company's name, never the person's.
            if (not principal.is_broker and u.broker_party_id is not None
                    and normalize_role(u.role) == "operator"):
                bid = u.broker_party_id
                if bid not in _company:
                    party = s.get(Party, bid)
                    _company[bid] = party.legal_name if party else None
                return {"id": None, "full_name": _company[bid] or "The broker",
                        "email": None}
            return {"id": u.id, "full_name": u.full_name, "email": u.email}

        return [
            {"action": a.action, "note": a.note, "acted_at": _iso_utc(a.acted_at),
             "acted_by": _acted_by(u),
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

        # Bordereau setups, so the Programmes list can show how far each
        # programme has got (programme → brokers → contract → setup). A setup
        # is per (programme, broker); one with no broker predates the broker
        # level and still covers every broker on its programme.
        pipelines = (
            s.query(Pipeline.program_id, Pipeline.broker_party_id, Pipeline.status)
            .filter(Pipeline.program_id.in_(prog_ids),
                    Pipeline.status.in_(("active", "pending_approval", "draft")))
            .all()
        )
        # How far a programme has got. `pending_approval` sits between the two
        # it already knew about: further along than a draft nobody has finished,
        # not as far as one the carrier admin has released.
        setup_rank = {"active": 3, "pending_approval": 2, "draft": 1}
        setups: dict[tuple, str] = {}
        for prog_id, broker_id, status in pipelines:
            key = (prog_id, broker_id)
            if setup_rank.get(status, 0) > setup_rank.get(setups.get(key), 0):
                setups[key] = status

        def setup_status(prog_id: int, broker_id: int) -> Optional[str]:
            own, shared = setups.get((prog_id, broker_id)), setups.get((prog_id, None))
            return max((own, shared), key=lambda st: setup_rank.get(st, 0)) or None

        # BROKERS A CARRIER USER HAS ASKED FOR AND NOBODY HAS ANSWERED YET.
        #
        # Without these the Programmes screen says "add a broker" to somebody
        # who just did — there is no link, because a request is not a link, and
        # every reader below counts links. So they kept being told to do the
        # thing they were waiting on, and doing it again was refused.
        #
        # Reported separately from `brokers` rather than mixed in, because a
        # waiting request is NOT a broker on the programme: no contract can
        # hang off it and no setup can be built on it. The two must not be
        # added together anywhere.
        #
        # Scoped like the queue itself — a carrier user sees their own asks,
        # the carrier admin sees the company's.
        awaiting_by_prog: dict[int, list] = {}
        try:
            from db import BrokerOnboardingRequest
            aq = (s.query(BrokerOnboardingRequest, Party)
                  .outerjoin(Party,
                             Party.id == BrokerOnboardingRequest.broker_party_id)
                  .filter(BrokerOnboardingRequest.tenant_id == tid,
                          BrokerOnboardingRequest.status == "pending",
                          BrokerOnboardingRequest.program_id.in_(prog_ids)))
            if _carrier_seat(s, principal) == "user":
                aq = aq.filter(
                    BrokerOnboardingRequest.requested_by_user_id == principal.user_id)
            for req, party in aq.all():
                awaiting_by_prog.setdefault(req.program_id, []).append({
                    "request_id": req.id,
                    "broker_party_id": req.broker_party_id,
                    "legal_name": (party.legal_name if party else None)
                                  or req.org_name,
                })
        except Exception:  # noqa: BLE001 — the tree must render regardless
            awaiting_by_prog = {}

        by_prog: dict[int, list] = {}
        for link, party in links:
            by_prog.setdefault(link.program_id, []).append((link, party))

        # The short codes a broker writes in an email subject or file name
        # instead of a long name (intake_service.programme_code).
        from intake_service import contract_code, programme_code
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
                    # active | draft | None — the best setup this broker has here.
                    "setup_status": setup_status(p.id, party.id),
                    "contracts": [
                        {"id": c.id, "filename": c.filename, "status": c.status,
                         "code": contract_code(c),
                         # The business state (draft … active), which is what
                         # "is this contract live" means; `status` is extraction.
                         "lifecycle": c.lifecycle,
                         # Is THIS contract finished enough to build a setup on?
                         # Not the same question as "is it active" — an `agreed`
                         # contract the carrier has signed answers yes here
                         # while its lifecycle still reads `agreed`, because the
                         # broker's countersignature is not waited for. See
                         # _contract_settled.
                         "settled": _contract_settled(s, c),
                         # Is it the CARRIER ADMIN's move on this one? Lets the
                         # Programmes wizard put a "Review contract" action
                         # right where the admin is already looking, instead of
                         # a plain "Open the contract" that reads the same
                         # whether there is something to do or nothing at all.
                         "awaiting_carrier_admin": _contract_awaiting_admin(s, c)}
                        for c in sorted(bc, key=lambda c: c.id)
                    ],
                })
            tree.append({
                "id": p.id,
                "name": p.name,
                "code": programme_code(p),
                "status": p.status,
                # What kind of business it is, so the Programmes list can say
                # more than a name — the same two fields the create screen asks.
                "business_segment": p.business_segment,
                "product_line": p.product_line,
                "bdx_frequency": p.bdx_frequency,
                # Shown under the name on the Programmes list ("Created 2 Sep 2026").
                "created_at": _iso_utc(p.created_at),
                "broker_count": len(brokers),
                "contract_count": sum(len(b["contracts"]) for b in brokers),
                "brokers": brokers,
                # NOT part of broker_count, deliberately: nothing can be built
                # on one of these until it is approved.
                "brokers_awaiting": sorted(
                    awaiting_by_prog.get(p.id, []),
                    key=lambda b: (b["legal_name"] or "").lower()),
            })
        return {"tenant_id": tid, "programmes": tree}
