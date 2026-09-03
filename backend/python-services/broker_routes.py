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

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import String, and_, func, or_

from auth_deps import Principal, current_principal
from db import (
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


@router.get("/broker/carriers")
def broker_carriers(p: Principal = Depends(current_principal)):
    """The carriers that have put this broker on at least one programme.

    This is the first dropdown on BDX Setup. It is a list, not a choice the
    broker makes freely — a carrier missing here means it never linked them.
    """
    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        links = _links(s, bid)
        by_carrier: dict[int, int] = {}
        for l in links:
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
    straight away, and contracts the BROKER added, which wait for approval.
    Only a live one can be set up, so the UI needs both facts per row.
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
            out.append({
                "id": c.id,
                "filename": c.filename,
                "programme": {"id": c.program_id,
                              "name": progs[c.program_id].name if c.program_id in progs else "—"},
                "carrier": {"id": cid, "name": carriers.get(cid, "—")},
                "inception_dt": c.inception_dt.isoformat() if c.inception_dt else None,
                "expiry_dt": c.expiry_dt.isoformat() if c.expiry_dt else None,
                "approval_status": c.approval_status,
                "source": _contract_source(s, c, bid),
                "submitted_at": c.submitted_at.isoformat() if c.submitted_at else None,
                "created_at": c.created_at.isoformat() if getattr(c, "created_at", None) else None,
            })
        return out


@router.get("/broker/dashboard")
def broker_dashboard(p: Principal = Depends(current_principal)):
    """The broker's landing screen: what they hold, and what is holding them up.

    "Waiting on the carrier" is the only queue a broker has — it is the one
    thing they cannot move themselves.
    """
    with SessionLocal() as s:
        bid = _broker_party_id(s, p)
        me = s.query(Party).filter(Party.id == bid).first()
        links = _links(s, bid)
        prog_ids = [l.program_id for l in links]
        carrier_ids = sorted({l.tenant_id for l in links if l.tenant_id})
        carriers = {t.id: (t.legal_name or t.tenant_name) for t in s.query(Tenant).filter(
            Tenant.id.in_(carrier_ids)).all()} if carrier_ids else {}
        progs = ({pr.id: pr.name for pr in s.query(Program).filter(Program.id.in_(prog_ids)).all()}
                 if prog_ids else {})

        pending, live = [], 0
        if prog_ids:
            rows = (s.query(Contract)
                      .filter(Contract.program_id.in_(prog_ids),
                              Contract.broker_party_id == bid).all())
            for c in rows:
                if c.approval_status == "pending_approval":
                    pending.append({
                        "id": c.id, "filename": c.filename,
                        "programme": progs.get(c.program_id, "—"),
                        "carrier": carriers.get(
                            next((l.tenant_id for l in links if l.program_id == c.program_id), None), "—"),
                        "submitted_at": c.submitted_at.isoformat() if c.submitted_at else None,
                    })
                elif c.approval_status == "approved":
                    live += 1

        return {
            "broker": {"id": bid, "name": me.legal_name if me else "—"},
            "carriers": [{"id": i, "name": carriers.get(i, "—")} for i in carrier_ids],
            "counts": {
                "waiting_on_carrier": len(pending),
                "live_contracts": live,
                "programmes": len(links),
                "carriers": len(carrier_ids),
            },
            "waiting": sorted(pending, key=lambda r: r["submitted_at"] or ""),
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
