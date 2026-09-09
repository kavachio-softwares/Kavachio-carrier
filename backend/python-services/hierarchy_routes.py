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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, String

from db import (
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
    # A broker is visible to a carrier either because the carrier created it
    # (tenant_id matches) or because it sits on one of the carrier's programmes.
    if party.tenant_id != tenant_id:
        on_a_programme = (
            session.query(ProgramBroker.id)
            .filter(ProgramBroker.broker_party_id == party_id,
                    ProgramBroker.tenant_id == tenant_id)
            .first()
        )
        if not on_a_programme:
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
        unassigned = (
            s.query(Party)
            .filter(Party.tenant_id == tid,
                    # party_type is the enum party_type_e — its labels are
                    # already lowercase, so cast to text rather than lower().
                    func.cast(Party.party_type, String) == "broker",
                    ~Party.id.in_([b for b in by_broker] or [-1]))
            .all()
        )
        for party in unassigned:
            by_broker[party.id] = {**_broker_dict(party), "programmes": [],
                                   "contract_count": 0, "user_count": 0}

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
        return sorted(by_broker.values(), key=lambda b: (b["legal_name"] or "").lower())


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
    from app_routes import _make_invite_link, _send_invite_email

    name = (body.legal_name or "").strip()
    if not name:
        raise HTTPException(400, "Give the broker a name.")
    if body.party_type not in PRODUCER_PARTY_TYPES:
        raise HTTPException(422, f"party_type must be one of {sorted(PRODUCER_PARTY_TYPES)}")
    email = (body.admin_email or "").strip().lower()

    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal)

        # Checked BEFORE anything is created, so a taken email never leaves
        # behind a broker with no admin invited.
        if email and s.query(AppUser).filter(AppUser.email == email).first():
            raise HTTPException(409, "That email is already in use.")
        if s.query(Party).filter(Party.tenant_id == tid,
                                 func.lower(Party.legal_name) == name.lower()).first():
            raise HTTPException(409, f"You already work with a broker called {name}.")
        if body.program_id:
            _assert_programme(s, body.program_id, principal, tid)

        party = Party(tenant_id=tid, party_type=body.party_type, legal_name=name,
                      scope="tenant", is_app_managed=True, is_active=True)
        s.add(party); s.flush()

        if body.program_id:
            s.add(ProgramBroker(tenant_id=tid, program_id=body.program_id,
                                broker_party_id=party.id, status="active",
                                assigned_by_user_id=principal.user_id))

        admin, link = None, None
        if email:
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

        s.commit(); s.refresh(party)
        if admin and link:
            _send_invite_email(admin.email, link, admin.full_name, party.legal_name)

        d = _broker_dict(party)
        d.update({"admin_invited": bool(admin),
                  "admin_email": admin.email if admin else None,
                  "program_id": body.program_id})
        return d


@router.get("/brokers/{broker_party_id}")
def broker_detail(broker_party_id: int, principal: Principal = Depends(current_principal)):
    """One broker: its programmes with this carrier, its contracts, its people.

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
        contracts = (
            s.query(Contract)
            .filter(Contract.broker_party_id == broker_party_id,
                    Contract.tenant_id == tid)
            .order_by(Contract.created_at.desc())
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
            "contracts": [
                # `name` as well as `filename`: a contract WRITTEN in Kavachio
                # has no file, so this list showed it as "Contract 1459" beside
                # uploads that showed their own names. It has a name — the one
                # the carrier typed — and it is what everything else calls it.
                {"id": c.id, "name": c.name, "filename": c.filename,
                 "program_id": c.program_id,
                 # Whether it is app-managed decides where its name should lead:
                 # a written contract's home is its own record, not the page
                 # that reads clauses out of an uploaded document.
                 "is_app_managed": bool(c.is_app_managed),
                 "status": c.status,
                 "inception_dt": str(c.inception_dt) if c.inception_dt else None,
                 "expiry_dt": str(c.expiry_dt) if c.expiry_dt else None,
                 "created_at": _iso_utc(c.created_at)}
                for c in contracts
            ],
            # Their people, but never their password/reset columns.
            "users": [
                {"id": u.id, "full_name": u.full_name, "email": u.email,
                 "role": u.role, "status": u.status,
                 "accepted_at": _iso_utc(u.accepted_at)}
                for u in users
            ],
        }


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
            .order_by(Program.name)
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
