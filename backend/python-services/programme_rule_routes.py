"""Programme Rules — the API (programme_rules.py has the rules themselves).

  Carrier (any carrier login reads; the carrier admin writes)
  GET    /programs/{id}/rules                    the programme's rules, the fields
                                                 and contracts a rule can use
  POST   /programs/{id}/rules                    add a draft
  PUT    /programs/{id}/rules/{rule}             edit (a sent rule becomes a draft
                                                 of its next version)
  POST   /programs/{id}/rules/{rule}/send        send for approval
  POST   /programs/{id}/rules/{rule}/retire      stop checking with it
  DELETE /programs/{id}/rules/{rule}             delete a draft never sent

  Broker (only rules sent to its own organisation)
  GET    /broker/programme-rules                 the rules waiting for it, and answered
  POST   /broker/programme-rules/{rule}/approve
  POST   /broker/programme-rules/{rule}/request-changes

  Reinsurer — no login: on its treaty link, after the code (treaty_routes)
  GET    /treaty-view/{token}/programme-rules
  POST   /treaty-view/{token}/programme-rules/{rule}/approve
  POST   /treaty-view/{token}/programme-rules/{rule}/request-changes

Every write logs its own named row in the Audit Logs (audit._SELF_LOGGED).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

import programme_rules as pr
from app_routes import _actor, resolve_tenant_id
from auth_deps import Principal, require_role
from carrier_scope import broker_party_id_of, require_carrier_admin
from db import ActivityEvent, Contract, Party, Program, SessionLocal

log = logging.getLogger("kavachio.programme_rules")

router = APIRouter(tags=["programme-rules"])
public_router = APIRouter(prefix="/treaty-view", tags=["programme-rules"])

_reader = require_role("carrier_admin")
_broker = require_role("broker_admin")


class RuleIn(BaseModel):
    name: str = ""
    description: Optional[str] = None
    check: str = ""
    operand: Optional[object] = None
    severity: str = "warning"
    broker_scope: str = "none"
    broker_contract_ids: Optional[list[int]] = None
    treaty_scope: str = "none"
    treaty_contract_ids: Optional[list[int]] = None
    approval_from: str = ""


class AnswerIn(BaseModel):
    note: Optional[str] = None
    signer_name: Optional[str] = None


def _log(s, tid: int, actor: str, action: str, rule: pr.ProgrammeRule, principal=None, **extra):
    from audit import actor_columns
    s.add(ActivityEvent(tenant_id=tid, actor=actor, action=action,
                        target=f"programme_rule:{rule.id}",
                        details={"rule_id": rule.id, "name": rule.name, "program_id": rule.program_id,
                                 "version": rule.version, **extra},
                        **(actor_columns(principal) if principal is not None else {})))


def _programme(s, principal: Principal, mga: Optional[str], program_id: int,
               *, need_tables: bool = True) -> tuple[int, Program]:
    tid = resolve_tenant_id(s, principal, mga)
    prog = s.get(Program, program_id)
    if prog is None or prog.tenant_id != tid:
        raise HTTPException(404, "That programme is not one of yours.")
    if need_tables:
        pr.require_tables(s)
    return tid, prog


def _rule(s, prog: Program, rule_id: int) -> pr.ProgrammeRule:
    r = s.get(pr.ProgrammeRule, rule_id)
    if r is None or r.program_id != prog.id or r.tenant_id != prog.tenant_id:
        raise HTTPException(404, "That rule is not on this programme.")
    return r


def _page(s, tid: int, prog: Program) -> dict:
    rules = (s.query(pr.ProgrammeRule).filter(pr.ProgrammeRule.program_id == prog.id,
                                              pr.ProgrammeRule.tenant_id == tid)
             .order_by(pr.ProgrammeRule.id.desc()).all())
    return {"programme": {"id": prog.id, "name": prog.name},
            "rules": [pr.view(s, r) for r in rules],
            "checks": pr.checks(), "contracts": pr.contract_choices(s, tid, prog.id)}


# ── carrier ────────────────────────────────────────────────────────────────
@router.get("/programme-rules/waiting")
def waiting_rules(mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    """The carrier's sent rules not yet approved by everyone they were sent to
    (the dashboard's "Programme rules waiting for approval")."""
    with SessionLocal() as s:
        if pr.missing_tables(s):
            return {"rules": [], "waiting": 0, "changes_requested": 0}
        tid = resolve_tenant_id(s, principal, mga)
        sent = (s.query(pr.ProgrammeRule).filter(pr.ProgrammeRule.tenant_id == tid,
                                                 pr.ProgrammeRule.status == pr.SENT)
                .order_by(pr.ProgrammeRule.sent_at.desc(), pr.ProgrammeRule.id.desc()).all())
        progs = {p.id: p.name for p in s.query(Program).filter(
            Program.id.in_({r.program_id for r in sent} or {0}))}
        rows = []
        for r in sent:
            v = pr.view(s, r)
            if v["status_line"] == "Active":
                continue
            rows.append({"id": r.id, "program_id": r.program_id,
                         "programme": progs.get(r.program_id) or f"#{r.program_id}",
                         "name": v["name"], "sentence": v["sentence"], "status_line": v["status_line"],
                         "changes_requested": any(a["status"] == pr.CHANGES for a in v["approvals"]),
                         "sent_at": v["sent_at"]})
        return {"rules": rows, "waiting": len(rows),
                "changes_requested": sum(1 for x in rows if x["changes_requested"])}


@router.get("/programs/{program_id}/rules")
def list_rules(program_id: int, mga: Optional[str] = None, principal: Principal = Depends(_reader)):
    with SessionLocal() as s:
        tid, prog = _programme(s, principal, mga, program_id, need_tables=False)
        if pr.missing_tables(s):
            # The tab says so in words instead of failing.
            return {"ready": False, "message": pr.NOT_SET_UP,
                    "programme": {"id": prog.id, "name": prog.name}, "rules": [], "checks": [],
                    "contracts": {"broker": [], "treaty": []}}
        return {"ready": True, **_page(s, tid, prog)}


@router.post("/programs/{program_id}/rules")
def add_rule(program_id: int, body: RuleIn, mga: Optional[str] = None,
             principal: Principal = Depends(require_carrier_admin("add a programme rule"))):
    with SessionLocal() as s:
        tid, prog = _programme(s, principal, mga, program_id)
        r = pr.create(s, tid, prog.id, body.model_dump(), principal.user_id)
        _log(s, tid, _actor(principal), "programme_rule_created", r, principal)
        s.commit()
        return _page(s, tid, prog)


@router.put("/programs/{program_id}/rules/{rule_id}")
def edit_rule(program_id: int, rule_id: int, body: RuleIn, mga: Optional[str] = None,
              principal: Principal = Depends(require_carrier_admin("change a programme rule"))):
    with SessionLocal() as s:
        tid, prog = _programme(s, principal, mga, program_id)
        r = pr.update(s, _rule(s, prog, rule_id), body.model_dump())
        _log(s, tid, _actor(principal), "programme_rule_updated", r, principal)
        s.commit()
        return _page(s, tid, prog)


@router.post("/programs/{program_id}/rules/{rule_id}/send")
def send_rule(program_id: int, rule_id: int, mga: Optional[str] = None,
              principal: Principal = Depends(require_carrier_admin("send a programme rule"))):
    with SessionLocal() as s:
        tid, prog = _programme(s, principal, mga, program_id)
        r = _rule(s, prog, rule_id)
        asked = pr.send(s, r)
        _log(s, tid, _actor(principal), "programme_rule_sent", r, principal,
             asked=[{"kind": k, "party_id": p} for k, p in asked])
        s.commit()
        emailed = _email_reinsurers(s, r, [p for k, p in asked if k == pr.REINSURER], principal)
        out = _page(s, tid, prog)
        out["sent"] = {"brokers": sum(1 for k, _p in asked if k == pr.BROKER),
                       "reinsurers": sum(1 for k, _p in asked if k == pr.REINSURER),
                       "emails": emailed}
        return out


@router.post("/programs/{program_id}/rules/{rule_id}/retire")
def retire_rule(program_id: int, rule_id: int, mga: Optional[str] = None,
                principal: Principal = Depends(require_carrier_admin("retire a programme rule"))):
    with SessionLocal() as s:
        tid, prog = _programme(s, principal, mga, program_id)
        r = _rule(s, prog, rule_id)
        pr.retire(r)
        _log(s, tid, _actor(principal), "programme_rule_retired", r, principal)
        s.commit()
        return _page(s, tid, prog)


@router.delete("/programs/{program_id}/rules/{rule_id}")
def delete_rule(program_id: int, rule_id: int, mga: Optional[str] = None,
                principal: Principal = Depends(require_carrier_admin("delete a programme rule"))):
    with SessionLocal() as s:
        tid, prog = _programme(s, principal, mga, program_id)
        r = _rule(s, prog, rule_id)
        if r.sent_at is not None:
            raise HTTPException(409, "This rule was sent, so it is kept. Retire it instead.")
        _log(s, tid, _actor(principal), "programme_rule_deleted", r, principal)
        s.delete(r)
        s.commit()
        return _page(s, tid, prog)


# ── reinsurers: an email with their treaty link ────────────────────────────
def _rules_email(*, link: str, name: str, carrier: str, reinsurer: str, programme: str,
                 rule: str, what: str, expires: str, otp: str) -> str:
    from esign_email import _code_block, _esc, _facts, _shell
    return _shell(
        f"{carrier} asks you to approve a programme rule",
        f"""<p style="font-size:14px;color:#0E1320;line-height:1.6;margin:0 0 8px">
              Hi {_esc(name)},</p>
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 18px">
              {_esc(carrier)} has set a rule for every treaty on <b>{_esc(programme)}</b>
              and asks {_esc(reinsurer)} to approve it. Open the link, enter the code
              below, and approve it or ask for changes.</p>
            {_facts([("Rule", rule), ("Checks", what), ("Programme", programme), ("Sent by", carrier)])}
            <p style="font-size:14px;color:#566071;line-height:1.6;margin:0 0 4px">
              It checks your reinsurance statements only once you approve.</p>
            {_code_block(otp)}""",
        link=link, cta="Review the rule",
        footer=(f"This link is yours alone and works until {_esc(expires)}. Opening it asks "
                f"for the code above every time you start a new browser session."))


def _email_reinsurers(s, rule: pr.ProgrammeRule, party_ids: list[int], principal: Principal) -> int:
    """A link to every person at each reinsurer newly asked, on one treaty of
    the rule's that the reinsurer is on. Never fails the send."""
    if not party_ids:
        return 0
    import treaty_email
    import treaty_routes as tr
    from db import PartyContact
    sent = 0
    try:
        prog = s.get(Program, rule.program_id)
        treaties = pr.covered(rule, pr.treaties(s, rule.tenant_id, rule.program_id), pr.REINSURER)
        for pid in party_ids:
            c = next((t for t in treaties if pid in pr.panel(s, t)), None)
            party = s.get(Party, pid)
            if c is None:
                continue
            for contact in (s.query(PartyContact).filter(PartyContact.party_id == pid)
                            .order_by(PartyContact.id)):
                if not (contact.email or "").strip():
                    continue
                link, token, code = tr._open_link(s, c, contact, principal)
                s.commit()
                carrier = tr._carrier_name(s, c)
                html = _rules_email(link=tr._view_link(token), name=link.name, carrier=carrier,
                                    reinsurer=getattr(party, "legal_name", "") or "",
                                    programme=getattr(prog, "name", "") or "the programme",
                                    rule=rule.name, what=pr.sentence(rule),
                                    expires=f"{tr._aware(link.expires_at):%d %b %Y}", otp=code)
                if treaty_email.safe_send(
                        link.email, f"{carrier} asks you to approve a programme rule", html,
                        text=(f"{carrier} asks you to approve a programme rule on Kavachio. Open "
                              f"{tr._view_link(token)}, enter the code {code}, and approve it "
                              f"or ask for changes there.")):
                    sent += 1
    except Exception as e:  # noqa: BLE001 — the rule is sent; the email can go again
        s.rollback()
        log.warning("programme rule %s: could not email the reinsurers: %s", rule.id, e)
    return sent


# ── broker ─────────────────────────────────────────────────────────────────
def _broker_view(s, rule: pr.ProgrammeRule, row: pr.ProgrammeRuleApproval, carriers: dict) -> dict:
    prog = s.get(Program, rule.program_id)
    mine = [c for c in pr.covered(rule, pr.broker_contracts(s, rule.tenant_id, rule.program_id), pr.BROKER)
            if c.broker_party_id == row.party_id]
    return {"id": rule.id, "name": rule.name, "description": rule.description,
            "sentence": pr.sentence(rule), "severity": rule.severity, "version": rule.version,
            "programme": getattr(prog, "name", None), "carrier": carriers.get(rule.tenant_id),
            "contracts": [c.name or c.filename or f"Contract #{c.id}" for c in mine],
            "sent_at": rule.sent_at.isoformat() + "Z" if rule.sent_at else None,
            "status": row.status, "note": row.note,
            "at": row.acted_at.isoformat() + "Z" if row.acted_at else None}


def _on_programmes(s, bid: int) -> set[int]:
    """The programmes the carrier has this broker on, now (program_broker)."""
    from sqlalchemy import func
    from db import ProgramBroker
    return {r[0] for r in s.query(ProgramBroker.program_id)
            .filter(ProgramBroker.broker_party_id == bid,
                    func.coalesce(ProgramBroker.status, "active") == "active")}


@router.get("/broker/programme-rules")
def broker_rules(principal: Principal = Depends(_broker)):
    from db import Tenant
    with SessionLocal() as s:
        if pr.missing_tables(s):
            return {"rules": [], "waiting": 0}
        bid = broker_party_id_of(s, principal)
        if not bid:
            raise HTTPException(403, "Your login is not linked to a broker.")
        # Only rules sent to this broker, on programmes it is still on.
        progs = _on_programmes(s, bid)
        rows = [(r, a) for r, a in pr.asked_of(s, pr.BROKER, bid) if r.program_id in progs]
        carriers = {t.id: (getattr(t, "legal_name", None) or getattr(t, "tenant_name", None))
                    for t in s.query(Tenant).filter(Tenant.id.in_({r.tenant_id for r, _a in rows} or {0}))}
        items = [_broker_view(s, r, a, carriers) for r, a in rows]
        return {"rules": items, "waiting": sum(1 for x in items if x["status"] != pr.APPROVED)}


def _broker_answer(rule_id: int, status: str, body: AnswerIn, principal: Principal) -> dict:
    with SessionLocal() as s:
        pr.require_tables(s)
        bid = broker_party_id_of(s, principal)
        if not bid:
            raise HTTPException(403, "Your login is not linked to a broker.")
        r = s.get(pr.ProgrammeRule, rule_id)
        if r is None:
            raise HTTPException(404, "This rule was not sent to you.")
        from carrier_scope import assert_broker_on_programme
        assert_broker_on_programme(s, bid, r.program_id)
        a = pr.answer(s, r, pr.BROKER, bid, status, note=body.note, user_id=principal.user_id,
                      name=getattr(s.get(Party, bid), "legal_name", None))
        _log(s, r.tenant_id, _actor(principal),
             "programme_rule_approved" if status == pr.APPROVED else "programme_rule_changes_requested",
             r, principal, by="broker", party_id=bid, note=a.note)
        s.commit()
    return broker_rules(principal)


@router.post("/broker/programme-rules/{rule_id}/approve")
def broker_approve(rule_id: int, body: AnswerIn = AnswerIn(), principal: Principal = Depends(_broker)):
    return _broker_answer(rule_id, pr.APPROVED, body, principal)


@router.post("/broker/programme-rules/{rule_id}/request-changes")
def broker_request_changes(rule_id: int, body: AnswerIn, principal: Principal = Depends(_broker)):
    return _broker_answer(rule_id, pr.CHANGES, body, principal)


# ── reinsurer, on its treaty link ──────────────────────────────────────────
def _reinsurer_view(s, link) -> dict:
    rows = pr.asked_of(s, pr.REINSURER, link.party_id, {link.tenant_id})
    out = []
    for r, a in rows:
        prog = s.get(Program, r.program_id)
        mine = [c.name or f"Treaty #{c.id}" for c in
                pr.covered(r, pr.treaties(s, r.tenant_id, r.program_id), pr.REINSURER)
                if link.party_id in pr.panel(s, c)]
        out.append({"id": r.id, "name": r.name, "description": r.description,
                    "sentence": pr.sentence(r), "severity": r.severity,
                    "programme": getattr(prog, "name", None), "treaties": mine,
                    "status": a.status, "note": a.note, "by": a.acted_by_name,
                    "at": a.acted_at.isoformat() + "Z" if a.acted_at else None})
    return {"rules": out}


@public_router.get("/{token}/programme-rules")
def reinsurer_rules(token: str, session: str = Query("", alias="session"),
                    x_treaty_session: str = Header(default="")):
    import treaty_routes as tr
    with SessionLocal() as s:
        if pr.missing_tables(s):
            return {"rules": []}
        link, _c = tr._require_open(s, token, tr._session_value(x_treaty_session, session))
        return _reinsurer_view(s, link)


def _reinsurer_answer(token: str, session: str, rule_id: int, status: str, body: AnswerIn) -> dict:
    import treaty_routes as tr
    with SessionLocal() as s:
        pr.require_tables(s)
        link, _c = tr._require_open(s, token, session)
        r = s.get(pr.ProgrammeRule, rule_id)
        if r is None or r.tenant_id != link.tenant_id:
            raise HTTPException(404, "This rule was not sent to you.")
        name = (body.signer_name or "").strip() or link.name
        if status == pr.APPROVED and not (body.signer_name or "").strip():
            raise HTTPException(400, "Type your name to approve.")
        a = pr.answer(s, r, pr.REINSURER, link.party_id, status, note=body.note, name=name)
        _log(s, r.tenant_id, f"{name} <{link.email}>",
             "programme_rule_approved" if status == pr.APPROVED else "programme_rule_changes_requested",
             r, by="reinsurer", party_id=link.party_id, note=a.note)
        s.commit()
        return _reinsurer_view(s, link)


@public_router.post("/{token}/programme-rules/{rule_id}/approve")
def reinsurer_approve(token: str, rule_id: int, body: AnswerIn,
                      session: str = Query("", alias="session"), x_treaty_session: str = Header(default="")):
    import treaty_routes as tr
    return _reinsurer_answer(token, tr._session_value(x_treaty_session, session), rule_id, pr.APPROVED, body)


@public_router.post("/{token}/programme-rules/{rule_id}/request-changes")
def reinsurer_request_changes(token: str, rule_id: int, body: AnswerIn,
                              session: str = Query("", alias="session"),
                              x_treaty_session: str = Header(default="")):
    import treaty_routes as tr
    return _reinsurer_answer(token, tr._session_value(x_treaty_session, session), rule_id, pr.CHANGES, body)
