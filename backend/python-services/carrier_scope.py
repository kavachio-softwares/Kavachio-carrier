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
from sqlalchemy import func

from auth_deps import Principal, current_principal
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
                      func.coalesce(ProgramBroker.status, "active") == "active")
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


def resolve_policy(cs, contract_id: int, policy_id: int) -> dict:
    """A policy row, checked to hang off this contract. Reads the canonical
    warehouse (policy is not an ops ORM entity)."""
    from sqlalchemy import select
    from canonical import CANONICAL_TABLES
    t = CANONICAL_TABLES["policy"]
    stmt = select(t).where(t.c.policy_id == policy_id)
    if "is_current_version" in t.c:
        stmt = stmt.where(t.c.is_current_version.isnot(False))
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
        resolve_policy(cs, scope.contract_id, policy_id)
    return CarrierScope(**{**scope.__dict__, "policy_id": policy_id})


# --- reading one generated export, from either side --------------------------

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
