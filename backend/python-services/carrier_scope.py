"""The carrier-centric scope chain: carrier → program → broker → contract → policy.

Every nested route in carrier_routes.py resolves its path through here, one
link at a time. That is the point of the nesting: each segment is CHECKED
against the one above it, so a caller cannot reach a contract by knowing its id
— the contract must actually hang off the broker, on the programme, owned by
the carrier they are allowed to act for.

Who may name which carrier
──────────────────────────
  carrier_admin   exactly the carrier in their signed token. Anything else 404s.
  broker_admin /  a carrier that has put their broker organisation on at least
  operator        one programme (the `program_broker` row IS the grant), and
                  then only the programmes carrying that row.
  kavachio_admin  any carrier. This replaces the old `?mga=` selector.

Everything raises 404, never 403, on a scope miss: a 403 would confirm the row
exists, letting a caller enumerate other carriers' ids.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Path
from sqlalchemy import func, or_

from auth_deps import Principal, current_principal, require_role
from db import (
    AppUser, Contract, Program, ProgramBroker, SessionLocal, Tenant,
)

_NOT_FOUND = "not found"


# --- carrier ----------------------------------------------------------------

def broker_party_id_of(s, p: Principal) -> Optional[int]:
    """The broker organisation this person works for, read from the database.

    `mint_access_token` does not put broker_party_id in the claims, so
    Principal.broker_party_id is None in practice; trusting it would resolve
    every broker to "no broker" and hand back empty screens that look like
    real answers. Same reasoning as broker_routes._broker_party_id.
    """
    if not p.is_broker:
        return None
    u = s.query(AppUser).filter(AppUser.id == p.user_id).first()
    return int(u.broker_party_id) if u and u.broker_party_id else None


def carrier_ids_for_broker(s, broker_party_id: int) -> set[int]:
    """Carriers that have put this broker on at least one ACTIVE programme."""
    rows = (s.query(ProgramBroker.tenant_id)
              .filter(ProgramBroker.broker_party_id == broker_party_id,
                      func.coalesce(ProgramBroker.status, "active") == "active")
              .distinct().all())
    return {r[0] for r in rows if r[0] is not None}


def broker_programme_ids(s, p: Principal, carrier_id: int) -> set[int]:
    """The programmes of `carrier_id` that carry this broker seat's ACTIVE
    program_broker row — the only ones a broker may see there. Empty for a
    seat with no broker."""
    bid = broker_party_id_of(s, p)
    if bid is None:
        return set()
    # Joined through the programme (whose carrier is always set), not
    # program_broker.tenant_id, which legacy rows may lack — resolve_broker
    # accepts those too.
    rows = (s.query(ProgramBroker.program_id)
              .join(Program, Program.id == ProgramBroker.program_id)
              .filter(ProgramBroker.broker_party_id == bid,
                      Program.tenant_id == carrier_id,
                      func.coalesce(ProgramBroker.status, "active") == "active")
              .all())
    return {r[0] for r in rows}


def resolve_carrier(s, p: Principal, carrier_id: int) -> int:
    """Validate that `p` may act for `carrier_id`; return it as a tenant_id."""
    if p.is_platform_admin:
        if s.query(Tenant).filter(Tenant.id == carrier_id).first() is None:
            raise HTTPException(404, _NOT_FOUND)
        return carrier_id
    if p.is_broker:
        bid = broker_party_id_of(s, p)
        if bid is None or carrier_id not in carrier_ids_for_broker(s, bid):
            raise HTTPException(404, _NOT_FOUND)
        return carrier_id
    # carrier seat — pinned to the token, the path may not widen it
    if p.tenant_id is None or int(p.tenant_id) != int(carrier_id):
        raise HTTPException(404, _NOT_FOUND)
    return int(carrier_id)


# --- the two carrier seats --------------------------------------------------
#
# Everyone at a carrier holds the same `carrier_admin` DB role — the column
# takes four values, and a fifth would ripple through every check in the
# system. What separates the carrier ADMIN from a carrier USER is the
# organisation's owner pointer, and nothing else.
#
# The consequence is easy to miss: `require_role("carrier_admin")` admits BOTH
# seats. That is right for almost every route, and wrong for the few acts that
# are the admin's alone — signing a contract, and approving a setup a carrier
# user built. Those compose `require_carrier_admin` on top.
#
# app_routes._carrier_seat and _assert_is_carrier_admin are the same rule,
# kept where their callers are; both delegate here so that who the carrier
# admin is has ONE definition rather than three that can drift.

def carrier_seat(s, p: Principal) -> str:
    """Which carrier seat this is: "admin" (the organisation's owner), "user"
    (everyone else there), or "both".

    "both" covers Kavachio staff and an organisation with NO owner recorded —
    a legacy tenant migration 18 could not name one for. Treating that as
    "user" would lock every person in it out of their own company, so it opens
    rather than closes.
    """
    if p.is_platform_admin:
        return "both"
    t = (s.query(Tenant).filter(Tenant.id == p.tenant_id).first()
         if p.tenant_id else None)
    if t is None or t.owner_user_id is None:
        return "both"
    return "admin" if t.owner_user_id == p.user_id else "user"


def is_carrier_admin_seat(s, p: Principal) -> bool:
    """True for the carrier admin, and for a seat the rule cannot pin down."""
    return carrier_seat(s, p) in ("admin", "both")


def is_carrier_admin_user(s, tenant_id, user_id) -> bool:
    """The same question as is_carrier_admin_seat, asked about a USER ID rather
    than about whoever is signed in.

    Needed where the act is happening later than the decision to do it: an
    invitation is accepted days after it was sent, and what the carrier's own
    rule says about the link it writes depends on who sent it, not on the
    broker clicking Accept. There is no Principal to hand it by then — only
    `broker_invitation.by_user_id`.

    Fails OPEN on an unknown tenant or a tenant with no owner recorded, exactly
    as carrier_seat does: those organisations answer "both", every seat in them
    is treated as the admin, and this has to agree with that or the two rules
    would disagree about the same person.
    """
    if user_id is None:
        return True
    t = s.query(Tenant).filter(Tenant.id == tenant_id).first() if tenant_id else None
    if t is None or t.owner_user_id is None:
        return True
    return t.owner_user_id == user_id


def require_carrier_admin(what: str = "do this"):
    """Dependency factory for the acts that are the carrier ADMIN's alone.

    Composed with require_role rather than replacing it: the role check is what
    refuses a broker token, a platform-only seat and a signed-out caller, and
    this adds the seat on top of it.

    403, not 404. A carrier user is entitled to know the contract exists and is
    simply not theirs to sign — "no such contract" would send them looking for
    a bug. That is the opposite of the scope misses above, where a 404 is what
    stops a caller enumerating another carrier's ids.
    """
    def _dep(p: Principal = Depends(require_role("carrier_admin"))) -> Principal:
        with SessionLocal() as s:
            if not is_carrier_admin_seat(s, p):
                raise HTTPException(
                    403, f"Only your organisation's carrier admin can {what}.")
        return p
    return _dep



# --- a programme link that is still waiting -----------------------------------
#
# A carrier USER's programme→broker link is created `pending_approval` and is
# released when the Bordereau Setup built on it is approved (migration 27).
# While it waits it is REAL for the carrier — they are building the contract
# and the setup on top of it — and does not exist for the broker, which is the
# whole point of the gate.
#
# So the status check cannot be one condition for everybody. Every broker-side
# reader keeps asking for `active` alone; the carrier's own scope chain asks
# through here.

LINK_LIVE = "active"
LINK_PENDING = "pending_approval"


def link_live(p: Principal | None = None):
    """SQL condition for the programme links this caller may act through.

    The carrier sees its own pending links; a broker seat, and any caller we
    cannot identify, sees only live ones. Fails closed: an unknown caller gets
    the narrower answer.
    """
    live = func.coalesce(ProgramBroker.status, LINK_LIVE) == LINK_LIVE
    if p is not None and not p.is_broker:
        return or_(live, ProgramBroker.status == LINK_PENDING)
    return live


def link_is_live(link, p: Principal | None = None) -> bool:
    """The same rule as link_live(), for code holding the ROW rather than
    building a query.

    Both exist because both shapes are in use, and the rule has to be one
    thing. A carrier raising a contract under a broker whose link is still
    waiting for the carrier admin is doing exactly what the flow asks of them —
    refusing it there stops the chain in the middle, one step after it was
    started. A NULL status is a legacy row and has always meant live.
    """
    status = (getattr(link, "status", None) or LINK_LIVE)
    if status == LINK_LIVE:
        return True
    return status == LINK_PENDING and p is not None and not p.is_broker


# --- the resolved chain -----------------------------------------------------

@dataclass(frozen=True)
class CarrierScope:
    """A validated position in the hierarchy. Only the links the route named
    are populated; each one was checked against its parent."""
    principal: Principal
    carrier_id: int
    carrier_code: Optional[str] = None      # tenant_code — the legacy `mga`
    program_id: Optional[int] = None
    broker_party_id: Optional[int] = None
    contract_id: Optional[int] = None
    policy_id: Optional[int] = None

    @property
    def mga(self) -> Optional[str]:
        """The legacy `mga` string the older handlers still take."""
        return self.carrier_code

    @property
    def acting(self) -> Principal:
        """The principal to hand a delegated handler.

        A BROKER seat carries no tenant of its own — it works across carriers,
        and which carrier is meant comes from the path. The shared handlers
        predate that: they call assert_tenant_owns(), which compares the row's
        tenant against principal.tenant_id and 404s on None.

        So delegation passes a principal pinned to the carrier this request has
        ALREADY been authorized for, a few lines above, by resolve_carrier() +
        resolve_program() + resolve_broker(). This narrows nothing and widens
        nothing: it states the carrier the scope chain just proved, in the field
        the older handlers read it from. The role is left untouched, so every
        require_role() guard still applies exactly as before.
        """
        if self.principal.tenant_id == self.carrier_id:
            return self.principal
        return Principal(
            user_id=self.principal.user_id,
            tenant_id=self.carrier_id,
            role=self.principal.role,
            broker_party_id=self.principal.broker_party_id,
        )


def _carrier_code(s, carrier_id: int) -> Optional[str]:
    t = s.query(Tenant).filter(Tenant.id == carrier_id).first()
    return t.tenant_name if t else None


def resolve_program(s, carrier_id: int, program_id: int) -> Program:
    prog = s.query(Program).filter(Program.id == program_id).first()
    if prog is None or prog.tenant_id != carrier_id:
        raise HTTPException(404, _NOT_FOUND)
    return prog


def resolve_broker(s, p: Principal, carrier_id: int, program_id: int,
                   broker_party_id: int) -> ProgramBroker:
    """The broker must be ON this programme — `program_broker` is the grant.

    A broker seat may additionally only ever name ITSELF, so one broker cannot
    read another broker's contracts on a programme they happen to share.
    """
    if p.is_broker:
        own = broker_party_id_of(s, p)
        if own is None or int(own) != int(broker_party_id):
            raise HTTPException(404, _NOT_FOUND)
    link = (s.query(ProgramBroker)
              .filter(ProgramBroker.program_id == program_id,
                      ProgramBroker.broker_party_id == broker_party_id,
                      link_live(p))
              .first())
    if link is None or (link.tenant_id is not None and link.tenant_id != carrier_id):
        raise HTTPException(404, _NOT_FOUND)
    return link


def resolve_contract(s, p: Principal, carrier_id: int, program_id: int,
                     broker_party_id: int, contract_id: int) -> Contract:
    """A contract is (programme × broker); both must match the path."""
    c = s.query(Contract).filter(Contract.id == contract_id).first()
    if c is None or c.program_id != program_id:
        raise HTTPException(404, _NOT_FOUND)
    if c.tenant_id is not None and c.tenant_id != carrier_id:
        raise HTTPException(404, _NOT_FOUND)
    # A contract with no broker is CARRIER-HELD: written before the broker level
    # existed, and it still governs the programme. app_routes.program_contracts_list
    # returns those to a broker on the programme for exactly that reason, so
    # opening one by id must agree with the list it appears in.
    if c.broker_party_id is not None and int(c.broker_party_id) != int(broker_party_id):
        raise HTTPException(404, _NOT_FOUND)
    return c


def policy_sender_filter(t, scope: "CarrierScope"):
    """Which of a contract's policies this scope may read, by who SENT them.

    A contract does not say whose a policy is: one the carrier holds for the
    whole programme collects every broker's policies. The sender the loader
    records (canonical.POLICY_SUBMITTER_COL) does:
      broker seat   only the policies its own runs loaded. A policy with no
                    sender recorded is nobody's to show a broker.
      carrier seat  the policies of the broker the path names, plus those with
                    no sender recorded — the carrier owns them all anyway.
    Returns a where-clause, or None for "no narrowing".
    """
    from sqlalchemy import false, or_
    from canonical import POLICY_SUBMITTER_COL
    col = t.c.get(POLICY_SUBMITTER_COL)
    if scope.principal.is_broker:
        if col is None or scope.broker_party_id is None:
            return false()
        return col == scope.broker_party_id
    if col is None or scope.broker_party_id is None:
        return None
    return or_(col == scope.broker_party_id, col.is_(None))


def resolve_policy(cs, contract_id: int, policy_id: int,
                   scope: Optional["CarrierScope"] = None) -> dict:
    """A policy row, checked to hang off this contract — and, given the scope,
    to be one this scope may read (policy_sender_filter). Reads the canonical
    warehouse (policy is not an ops ORM entity)."""
    from sqlalchemy import select
    from canonical import CANONICAL_TABLES
    t = CANONICAL_TABLES["policy"]
    stmt = select(t).where(t.c.policy_id == policy_id)
    if "is_current_version" in t.c:
        stmt = stmt.where(t.c.is_current_version.isnot(False))
    if scope is not None:
        sender = policy_sender_filter(t, scope)
        if sender is not None:
            stmt = stmt.where(sender)
    row = cs.execute(stmt).mappings().first()
    if row is None or row.get("policy_contract_id") != contract_id:
        raise HTTPException(404, _NOT_FOUND)
    return dict(row)


# --- FastAPI dependencies ---------------------------------------------------
#
# One per depth, so a route declares exactly how deep it sits and gets the whole
# chain validated by declaring it. Each opens its own short session purely to
# check the links, then hands back plain ints — the handler opens its own
# session as before.

def carrier_scope(carrier_id: int = Path(..., ge=1),
                  p: Principal = Depends(current_principal)) -> CarrierScope:
    with SessionLocal() as s:
        cid = resolve_carrier(s, p, carrier_id)
        return CarrierScope(principal=p, carrier_id=cid,
                            carrier_code=_carrier_code(s, cid))


def program_scope(program_id: int = Path(..., ge=1),
                  scope: CarrierScope = Depends(carrier_scope)) -> CarrierScope:
    with SessionLocal() as s:
        resolve_program(s, scope.carrier_id, program_id)
        if scope.principal.is_broker:
            # the broker must be on this programme at all
            bid = broker_party_id_of(s, scope.principal)
            if bid is None or not (
                s.query(ProgramBroker)
                 .filter(ProgramBroker.program_id == program_id,
                         ProgramBroker.broker_party_id == bid,
                         func.coalesce(ProgramBroker.status, "active") == "active")
                 .first()):
                raise HTTPException(404, _NOT_FOUND)
    return CarrierScope(**{**scope.__dict__, "program_id": program_id})


def broker_scope(broker_party_id: int = Path(..., ge=1),
                 scope: CarrierScope = Depends(program_scope)) -> CarrierScope:
    with SessionLocal() as s:
        resolve_broker(s, scope.principal, scope.carrier_id,
                       scope.program_id, broker_party_id)
    return CarrierScope(**{**scope.__dict__, "broker_party_id": broker_party_id})


def contract_scope(contract_id: int = Path(..., ge=1),
                   scope: CarrierScope = Depends(broker_scope)) -> CarrierScope:
    with SessionLocal() as s:
        resolve_contract(s, scope.principal, scope.carrier_id, scope.program_id,
                         scope.broker_party_id, contract_id)
    return CarrierScope(**{**scope.__dict__, "contract_id": contract_id})


def policy_scope(policy_id: int = Path(..., ge=1),
                 scope: CarrierScope = Depends(contract_scope)) -> CarrierScope:
    from db import CanonicalSession
    with CanonicalSession() as cs:
        resolve_policy(cs, scope.contract_id, policy_id, scope)
    return CarrierScope(**{**scope.__dict__, "policy_id": policy_id})


# --- what the PLATFORM seat is not a party to --------------------------------
#
# require_role() lets a platform admin through every carrier gate — it is a
# superset of the carrier roles by design. That is right for oversight, and
# wrong for the two things below, which are not oversight: they are acts in a
# relationship between a carrier and a broker that Kavachio is not part of.
#
# Both were "closed" only by accident before: each endpoint ends up calling
# resolve_tenant_id(), which 400s a platform admin for not naming a tenant. A
# 400 about a missing parameter is not a rule — it reads as something the
# caller forgot rather than something they may not do, and it opens the moment
# anyone adds a tenant picker to the screen. These say it outright instead.

def assert_can_invite_brokers(principal: Principal) -> None:
    """Bringing a broker on board, and chasing or withdrawing that invitation,
    belongs to the CARRIER alone.

    An invitation says "come and work with us", and Kavachio is not the "us".
    The accepted invitation IS the carrier-broker relationship, so a platform
    admin creating one would be making a relationship on a carrier's behalf
    that the carrier never agreed to — and the broker would be told a carrier
    invited them when nobody there did.

    Kavachio's oversight of the same ground is unchanged: the carrier's page
    still lists its brokers, and the audit trail still names every invitation.
    Reading who works with whom is oversight; deciding it is not."""
    if principal.is_platform_admin:
        raise HTTPException(
            403, "Kavachio can see a carrier's brokers but cannot invite one. "
                 "An invitation comes from the carrier — only they can send, "
                 "chase or withdraw it.")


def assert_can_open_contract(principal: Principal) -> None:
    """Opening a CONTRACT RECORD — its terms, its documents, its signature
    page — is for the two organisations that hold it.

    A contract is an agreement between a carrier and a broker, and its terms
    are the commercial substance of that agreement. Kavachio runs the platform
    the agreement is administered on; it is not a party to it, and being able
    to read every carrier's rates and limits is not something running the
    platform requires.

    What oversight DOES need is left open on purpose: which contracts hang off
    which programme, how many there are and what state they are in, all of
    which the carrier's page still shows from the programme's contract list.
    That answers "is this carrier set up" without opening the agreement."""
    if principal.is_platform_admin:
        raise HTTPException(
            403, "Kavachio can see which contracts a programme has but cannot "
                 "open one. A contract is between the carrier and the broker.")


# --- reading one generated export, from either side --------------------------

def assert_can_amend(principal: Principal) -> None:
    """Changing a file's exceptions — deciding them (Approve / Fix / Dismiss),
    correcting a value, or running Fix & Validate — is the BROKER's work, and
    only the broker's.

    A bordereau is the broker's submission. What is flagged on it is a question
    put to the broker, and answering it is answering for their own file, so the
    two broker seats are the only ones that may write here. Everyone else reads:
    Kavachio staff open every file and amend none (a change made from the
    platform is an edit neither side made), and a carrier reads what was flagged
    on a file it received without correcting the submission on the sender's
    behalf. Reading is untouched for all of them — assert_can_read_export still
    lets each side see every file it is entitled to.

    Call it BEFORE any read-scope check, so the answer is the same 403 whatever
    the file — it is about who is asking, not which file."""
    if principal.is_broker:
        return
    if principal.is_platform_admin:
        raise HTTPException(
            403, "Kavachio can view exceptions but not change them. Only the "
                 "broker that sent a file can review, fix or re-validate it.")
    raise HTTPException(
        403, "You can view every exception on this file but not change them. "
             "Only the broker that sent it can approve, fix, dismiss or "
             "re-validate it.")


def assert_can_read_export(s, p: Principal, export_row) -> None:
    """May this principal see this generated output?

    assert_tenant_owns() cannot answer it. That guard compares the row's tenant
    against principal.tenant_id, and a BROKER seat has no tenant — so every
    /export/downloads/* route 404s for them, which is why a broker could run a
    bordereau and then not open its exceptions.

    Widening assert_tenant_owns itself would be wrong: it guards mappers,
    uploads, templates and contracts too, and "the broker owns it" is meaningless
    for most of those. So this is export-specific, and states the rule once:

      platform admin  any export
      carrier seat    an export belonging to their tenant (unchanged)
      broker seat     an export STAMPED with their broker_party_id, and only
                      while they are still on the programme it was run for —
                      output_exports carries carrier/programme/broker/contract
                      denormalised precisely so this is answerable without
                      walking back through a pipeline that may since have been
                      edited or deleted.

    A broker taken off a programme therefore loses access to those runs, which
    matches every other read on their side (broker_routes starts from the same
    program_broker rows).

    404, never 403, on a miss: a 403 confirms the id exists.
    """
    if p.is_platform_admin:
        return
    if not p.is_broker:
        if getattr(export_row, "tenant_id", None) != p.tenant_id:
            raise HTTPException(404, _NOT_FOUND)
        return

    own = broker_party_id_of(s, p)
    row_broker = getattr(export_row, "broker_party_id", None)
    if own is None or row_broker is None or int(row_broker) != int(own):
        raise HTTPException(404, _NOT_FOUND)

    # Still on the programme it was run for. An export predating the broker
    # level carries no programme; it also carries no broker, so it was already
    # refused above.
    prog = getattr(export_row, "program_id", None)
    if prog is None:
        raise HTTPException(404, _NOT_FOUND)
    link = (s.query(ProgramBroker)
              .filter(ProgramBroker.program_id == prog,
                      ProgramBroker.broker_party_id == own,
                      func.coalesce(ProgramBroker.status, "active") == "active")
              .first())
    if link is None:
        raise HTTPException(404, _NOT_FOUND)
