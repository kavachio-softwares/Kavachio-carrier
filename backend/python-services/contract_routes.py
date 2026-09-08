"""
The contract as a RECORD — raise it, fill it in, get it approved, keep it.

Everything that already existed treated a contract as the FILE it came from:
upload a PDF, extract clauses, generate rules. That is the right shape for rule
generation and the wrong shape for the thing itself. A contract is agreed before
anyone has a signed PDF, it is identified by terms rather than by a filename, it
picks up endorsements while it runs, and it ends — on its expiry date, on notice,
or by being renewed into a successor. None of that could be said.

So this file adds the record and its life, and leaves extraction exactly where
it was. The two meet in one place: `POST /contracts/{id}/generate-rules` feeds
the contract's ACTIVE documents through the existing pipeline and writes the
result back onto this contract instead of minting a second one.

Three status axes, deliberately not collapsed into one:

    approval_status   what the CARRIER decided. The gate — a broker's contract
                      cannot be used until it says approved. Untouched by this
                      file except through the approve/reject routes that already
                      existed in hierarchy_routes.
    lifecycle         where the CONTRACT is: draft → pending → active →
                      expired / terminated / superseded. New here.
    status_ops        what the extraction pipeline did with the file.

They answer different questions and a contract routinely differs on all three —
approved, extracted, and not yet in force because its term starts in January.

WHO MAY DO WHAT
    A carrier admin owns the book: they raise contracts that are live on
    arrival, and they decide on the ones brokers bring.
    A broker raises a contract as a DRAFT, submits it, and waits. The DB trigger
    trg_set_contract_approval — not this file — is what marks a broker's
    contract pending, so a client can never assert "approved".
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import tempfile
from typing import Any, Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import String, func, or_

import contract_types as ct
import storage
from auth_deps import Principal, current_principal, resolve_broker_party_id
from app_routes import _iso_utc, assert_tenant_owns, resolve_tenant_id
from db import (
    Contract, ContractApproval, ContractDocument, ExportTemplate,
    ContractSignature, Party, Program, ProgramBroker, SessionLocal,
)

router = APIRouter()

# The kinds of document a contract can hold. See ContractDocument for what each
# one MEANS — the difference between a reference and an endorsement is the whole
# reason this is not a single blob column.
DOC_KINDS = ("contract", "reference", "endorsement")


# =============================================================================
#  Access
# =============================================================================

def _program_access(s, p: Principal, program_id: int) -> tuple[Program, int]:
    """The programme, plus the tenant that owns it — or 404.

    A carrier reaches its own programmes. A BROKER has no tenant of its own, so
    it reaches a programme only through program_broker: the same row that says
    the pair may produce at all. Anything else is 404 rather than 403, so an id
    that exists in another carrier's book is indistinguishable from one that
    does not exist.
    """
    prog = s.get(Program, program_id)
    if not prog:
        raise HTTPException(404, "programme not found")

    if p.is_broker:
        # Resolved, never read off the token — see resolve_broker_party_id.
        bid = resolve_broker_party_id(s, p)
        if bid is None:
            raise HTTPException(403, "no broker bound to this user")
        link = (s.query(ProgramBroker)
                .filter(ProgramBroker.program_id == program_id,
                        ProgramBroker.broker_party_id == bid)
                .first())
        if link is None:
            raise HTTPException(404, "programme not found")
        if link.status != "active":
            raise HTTPException(
                403, "you have been taken off this programme, so no new "
                     "contract can be raised on it")
        return prog, prog.tenant_id

    assert_tenant_owns(p, prog.tenant_id)
    return prog, prog.tenant_id


def _contract_access(s, p: Principal, contract_id: int) -> Contract:
    """Fetch a contract the caller is allowed to see."""
    c = s.get(Contract, contract_id)
    if not c:
        raise HTTPException(404, "contract not found")
    if p.is_broker:
        # A broker sees only its OWN contracts — a shared programme carries
        # other brokers' contracts and none of them are this broker's business.
        bid = resolve_broker_party_id(s, p)
        if bid is None:
            raise HTTPException(403, "no broker bound to this user")
        if c.broker_party_id != bid:
            raise HTTPException(404, "contract not found")
        if c.program_id:
            _program_access(s, p, c.program_id)
        return c
    assert_tenant_owns(p, c.tenant_id)
    return c


def _require_carrier(p: Principal) -> None:
    if p.is_broker:
        raise HTTPException(
            403, "only the carrier can do this — you own the submission, "
                 "they own the decision")


# =============================================================================
#  Reading a contract
# =============================================================================

def _today() -> dt.date:
    return dt.date.today()


def _effective_lifecycle(c: Contract) -> str:
    """What state the contract is ACTUALLY in, now.

    `expired` is derived rather than stored, because it happens by the calendar
    passing rather than by anyone doing anything. A nightly job that stamped it
    would be one more thing to be down when someone asks whether a contract is
    in force; the expiry date is already on the row and answers it exactly.

    Every other state is a decision somebody made, so those ARE stored.
    """
    state = (c.lifecycle or "").strip()
    if not state:
        # Contracts that predate this flow. They were uploaded, so they were
        # meant to be in force — but only say so while the term holds.
        state = "active" if (c.status or "") == "active" else "draft"
    if state == "active" and c.expiry_dt and c.expiry_dt < _today():
        return "expired"
    return state


def _external_references(c: Contract) -> list[dict]:
    """Documents this contract's wording DEFERS to, as extraction found them.

    Stored by the persister under extracted.reference_documents.external — a
    clause saying "excluded classes per the Guidelines on file" cannot become a
    rule until those guidelines are supplied.
    """
    ext = (c.extracted or {})
    refs = ((ext.get("reference_documents") or {}).get("external")) or []
    return [r for r in refs if isinstance(r, dict)]


def _missing_references(c: Contract, docs: list[ContractDocument]) -> list[str]:
    """Named documents the wording needs that nobody has supplied yet.

    This is the list that makes reference documents MANDATORY: while it is
    non-empty the contract cannot be submitted or activated, because some of its
    clauses are known to be unenforceable — the wording says a limit exists and
    nothing on file says what it is.

    Matched loosely, in both directions. The contract names a document in prose
    ("the Purchasing Guidelines on file") and the file is named by whoever saved
    it ("purchasing_guidelines_v4_FINAL.pdf"); requiring those to match exactly
    would make the gate impossible to clear.
    """
    supplied = [d for d in docs if d.kind == "reference" and d.is_active]
    if not supplied:
        return [n for n in (
            (r.get("document_name") or "").strip() for r in _external_references(c)
        ) if n]

    def covers(doc: ContractDocument, named: str) -> bool:
        if (doc.satisfies_reference or "").strip().lower() == named.strip().lower():
            return True
        a = (doc.filename or "").lower()
        b = named.lower()
        if not a or not b:
            return False
        return a[:12] in b or b[:12] in a.replace(".pdf", "").replace(".docx", "")

    missing = []
    for r in _external_references(c):
        named = (r.get("document_name") or "").strip()
        if named and not any(covers(d, named) for d in supplied):
            missing.append(named)
    return missing


def _doc_dict(d: ContractDocument) -> dict:
    return {
        "id": d.id,
        "kind": d.kind,
        "filename": d.filename,
        "satisfies_reference": d.satisfies_reference,
        "effective_from": str(d.effective_from) if d.effective_from else None,
        # The signed copy, as opposed to a draft. Reported from outside — see
        # the upload route.
        "is_executed_copy": bool(d.is_executed_copy),
        "is_active": bool(d.is_active),
        "created_at": _iso_utc(d.created_at),
        # Whether the bytes are still reachable. A row whose blob was cleaned up
        # cannot be re-read, and a screen offering "download" for it lies.
        "has_file": bool(d.blob_ref or d.blob),
    }


def _record(s, c: Contract, *, with_docs: bool = True,
            p: Principal | None = None) -> dict:
    """The contract, as the screens need it.

    Deliberately fat: a contract screen that had to make five calls to say what
    state a contract is in would show four different half-states while they
    landed.
    """
    prog = s.get(Program, c.program_id) if c.program_id else None
    counterparty = s.get(Party, c.broker_party_id) if c.broker_party_id else None
    tmpl = (s.get(ExportTemplate, c.output_template_id)
            if c.output_template_id else None)
    docs = []
    if with_docs:
        docs = (s.query(ContractDocument)
                .filter(ContractDocument.contract_id == c.id)
                .order_by(ContractDocument.created_at.desc())
                .all())

    ctype = (c.contract_type or "")
    known_type = ctype in ct.CONTRACT_TYPES
    missing = _missing_references(c, docs) if with_docs else []
    active_docs = [d for d in docs if d.is_active]
    sigs = _signatures(s, c.id)
    has_wording = (bool((c.wording_sections or {}).get("sections"))
                   or any(d.kind == "contract" and d.is_active for d in active_docs)
                   or bool(c.blob_ref or c.blob))

    return {
        "id": c.id,
        "name": c.name or c.filename or f"Contract {c.id}",
        # The coded type, or None when this row predates coded types — the UI
        # says "not set" and offers to set it, rather than showing a document
        # type ("Program Schedule G") in a field that now means something else.
        "contract_type": ctype if known_type else None,
        "contract_type_label": (ct.CONTRACT_TYPES[ctype]["label"]
                                if known_type else (ctype or None)),
        "filename": c.filename,
        "programme": {"id": prog.id, "name": prog.name} if prog else None,
        "counterparty": ({
            "id": counterparty.id,
            "name": counterparty.legal_name,
            "party_type": counterparty.party_type,
        } if counterparty else None),
        "output_template": ({"id": tmpl.id, "name": tmpl.name,
                             "version": tmpl.version} if tmpl else None),
        "schedule_key": c.schedule_key,

        # ── the record ──
        "umr": c.umr,
        "risk_code": c.risk_code,
        "section_number": c.section_number,
        "class_of_business": c.class_of_business,
        "year_of_account": c.year_of_account,
        "earnings_pattern": c.earnings_pattern,
        "inception_dt": str(c.inception_dt) if c.inception_dt else None,
        "expiry_dt": str(c.expiry_dt) if c.expiry_dt else None,
        "executed_date": str(c.executed_date) if c.executed_date else None,
        "notice_period_days": c.notice_period_days,
        "premium_cap_amount": (float(c.premium_cap_amount)
                               if c.premium_cap_amount is not None else None),
        "premium_cap_currency": c.premium_cap_currency,

        # ── where it is ──
        "lifecycle": _effective_lifecycle(c),
        "lifecycle_stored": c.lifecycle,
        "lifecycle_effective_date": (str(c.lifecycle_effective_date)
                                     if c.lifecycle_effective_date else None),
        "approval_status": c.approval_status,
        "status_ops": c.status,
        "terminated_date": str(c.terminated_date) if c.terminated_date else None,
        "termination_reason": c.termination_reason,
        "renews_contract_id": c.renews_contract_id,
        "submitted_at": _iso_utc(c.submitted_at),
        "approved_at": _iso_utc(c.approved_at),
        "created_at": _iso_utc(c.created_at),

        # ── documents ──
        "documents": [_doc_dict(d) for d in docs] if with_docs else None,
        # Does this contract HAVE a wording — not "is there a file". A contract
        # written here has one: its sections, structured, on this record and
        # typeset on its page. Tying this to a blob was what made an authored
        # contract look wording-less and pushed the app into generating a
        # document just so the answer would come out true.
        "has_wording": has_wording,
        "endorsement_count": sum(1 for d in active_docs if d.kind == "endorsement"),
        # The authored contract, where there is one. NULL on uploads — that is
        # the fact, not an omission.
        "agreed_limits": c.commercial_terms,
        "wording_sections": (c.wording_sections or {}).get("sections"),
        "signers": (c.wording_sections or {}).get("signers"),
        # WHO ACTUALLY SIGNED, as opposed to who was named to. The gate that
        # puts a contract in force reads these, so the screen shows the same
        # rows the server decides on.
        "signatures": [
            {"id": sg.id, "side": sg.side, "signer_name": sg.signer_name,
             "signer_title": sg.signer_title, "signer_email": sg.signer_email,
             "method": sg.method, "by_user_id": sg.by_user_id,
             "signed_at": sg.signed_at.isoformat() if sg.signed_at else None,
             "document_id": sg.document_id, "note": sg.note}
            for sg in sigs],
        "unsigned_sides": _unsigned_sides(sigs),
        # The gate. Non-empty means some clauses cannot become rules, so the
        # contract may not be submitted or activated until it is cleared.
        "missing_references": missing,
        "actions": _allowed_actions(c, missing, p, _unsigned_sides(sigs),
                                    has_wording),
        # Whose move it is — "carrier", "broker", or null when nobody is
        # waiting. Both sides read this, so neither has to infer it from a
        # state name written for the other one.
        "whose_turn": _whose_turn(c, _unsigned_sides(sigs)),
        # The last thing the broker asked to change, if the contract is sitting
        # with the carrier because of it. Put on the record itself so the
        # carrier's screen can show the request beside the terms without a
        # second call.
        "open_change_request": _open_change_request(s, c),
    }


def _open_change_request(s, c: Contract) -> Optional[dict]:
    """The change request that is currently unanswered, or None.

    Only meaningful while the contract is back with the carrier: once the terms
    go out again, the request has been answered — by revising, or by re-sending
    unchanged — and it belongs to the thread rather than to the top of the page.
    """
    if _effective_lifecycle(c) != "changes_requested":
        return None
    row = (s.query(ContractApproval)
           .filter(ContractApproval.contract_id == c.id,
                   ContractApproval.action == "changes_requested")
           .order_by(ContractApproval.acted_at.desc())
           .first())
    if row is None:
        return None
    return {
        "note": row.note,
        "proposed_changes": row.proposed_changes or [],
        "acted_at": _iso_utc(row.acted_at),
    }


def _allowed_actions(c: Contract, missing: list[str],
                     p: Principal | None = None,
                     unsigned: list[str] | None = None,
                     has_wording: bool = True) -> dict[str, bool]:
    """What may be done to this contract right now, by THIS caller.

    Computed on the server because the rules are the server's: a screen that
    worked them out itself would eventually offer a button the API refuses, and
    the user would have no way to tell which of the two was wrong.

    Role matters here in a way it did not before. During a negotiation the two
    sides can do different things at the same moment — while terms are out for
    review the broker can accept or push back and the carrier can only wait, and
    when they come back the reverse is true. One shared answer would have to be
    the union of both, which is a screen full of buttons that 403.
    """
    state = _effective_lifecycle(c)
    blocked = bool(missing)
    is_broker = bool(p and p.is_broker)
    is_carrier = bool(p and not p.is_broker)
    unsigned = ["carrier", "counterparty"] if unsigned is None else unsigned
    my_side = "counterparty" if is_broker else "carrier"

    return {
        # The carrier revises while the ball is with it — including after the
        # broker has asked for changes, which is the whole point of asking.
        "edit": (state in ("draft", "pending")
                 or (state == "changes_requested" and is_carrier)),
        "submit": state == "draft" and not blocked and is_broker,
        "approve": c.approval_status == "pending_approval" and state == "pending",

        # ── the negotiation ──
        # Carrier sends its terms out. Only for a contract it owns and only to a
        # BROKER: a reinsurer has no seat in this app, so there is nobody on the
        # other side to review it.
        "send_for_review": (
            is_carrier and not blocked
            and state in ("draft", "changes_requested", "agreed")
            and c.contract_type == "insurer_broker"
            and c.broker_party_id is not None),
        # Broker's two answers. Never both sides' — this is their turn.
        "request_changes": is_broker and state in ("in_review", "agreed"),
        "accept_terms": is_broker and state == "in_review" and not blocked,
        # Sign and return it. The broker's last act — after this the contract is
        # the carrier's to place and put in force.
        "submit_signed": is_broker and state == "agreed",

        # Signing is what puts a contract in force, so this is only ever the
        # tidy-up path for a contract both sides signed while something else
        # was in the way. It never stands in for a signature.
        "activate": (state in ("signed",) and not unsigned
                     and c.approval_status == "approved" and not blocked
                     and is_carrier),

        # ── signing ──
        # Your own side, once the terms have stopped moving and there is a
        # wording to sign. A broker signs for the counterparty; the carrier
        # signs for itself.
        # `has_wording` matters: there is nothing to sign without one, and the
        # API says so. A button that 400s is worse than no button.
        "sign": (state in ("draft", "agreed", "signed")
                 and my_side in unsigned
                 and has_wording and bool(p)),
        # The carrier entering a signature made on paper or through a provider.
        # The only way a reinsurance contract is ever signed on both sides,
        # since a reinsurer has no seat here.
        "record_signature": (is_carrier and state in ("draft", "agreed", "signed")
                             and "counterparty" in unsigned and has_wording),
        "terminate": state in ("active", "expired") and is_carrier,
        "renew": state in ("active", "expired", "terminated") and is_carrier,
        "upload_documents": state not in ("terminated", "superseded"),
        "generate_rules": (state not in ("terminated", "superseded")
                           and not blocked),
    }


def _whose_turn(c: Contract, unsigned: list[str] | None = None) -> Optional[str]:
    """Who the contract is waiting on — "carrier", "broker", or nobody.

    A negotiation that does not say whose move it is becomes two people each
    assuming the other is looking at it.

    ONCE THE TERMS ARE SETTLED THE STATE ALONE CANNOT ANSWER THIS. `agreed`
    means "waiting on a signature", and after the broker gives theirs it is
    still `agreed` — but the move is now the carrier's. Reading the state
    alone would leave the contract sitting in the broker's queue on the
    strength of a signature they had already given, which is precisely the
    thing this field exists to prevent. So once signing has started, the turn
    belongs to whoever has not signed.
    """
    state = _effective_lifecycle(c)
    if unsigned is not None and state in ("agreed", "signed"):
        if "counterparty" in unsigned:
            return "broker"
        if "carrier" in unsigned:
            return "carrier"
        return "carrier"        # both signed, waiting to be put in force
    if state in ct.WITH_BROKER:
        return "broker"
    if state == "pending":
        return "carrier"
    if state in ct.WITH_CARRIER and state != "draft":
        return "carrier"
    return None


# =============================================================================
#  The type spec  —  what the form is built from
# =============================================================================

@router.get("/contract-types")
def contract_types(_p: Principal = Depends(current_principal)):
    """The two contract types and the fields each one cannot be saved without.

    Served rather than duplicated in the frontend so the form and the validator
    cannot disagree. Adding a mandatory field is a one-line change in
    contract_types.py and both sides pick it up.
    """
    return {"types": ct.public_spec(), "default": ct.DEFAULT_TYPE,
            "lifecycle": list(ct.LIFECYCLE),
            # The limits you agreed — each a question, an answer, and what
            # happens when a file breaks it. Served, not restated client-side.
            "agreed_limits": ct.agreed_limits_spec(),
            "limit_groups": ct.limit_groups_spec(),
            "severities": list(ct.SEVERITIES)}


@router.get("/counterparties")
def counterparties(party_type: str = Query(..., description="broker | reinsurer"),
                   program_id: Optional[int] = Query(None),
                   p: Principal = Depends(current_principal)):
    """Who this carrier may write a contract WITH, for the type being raised.

    For a broker the list is narrowed to the programme, because a broker that is
    not on the programme cannot hold a contract on it — offering them and
    refusing on save would be a worse way to say the same thing. A reinsurer has
    no such gate: it does not produce business into the programme.
    """
    with SessionLocal() as s:
        tid = (p.tenant_id if not p.is_platform_admin
               else (s.get(Program, program_id).tenant_id if program_id else None))
        q = (s.query(Party)
             .filter(func.cast(Party.party_type, String) == party_type,
                     Party.is_active.is_(True)))
        if tid is not None:
            # Global parties (scope='global') are shared; tenant ones are not.
            q = q.filter(or_(Party.tenant_id == tid, Party.tenant_id.is_(None)))

        if party_type == "broker" and program_id:
            on_programme = {
                l.broker_party_id for l in s.query(ProgramBroker).filter(
                    ProgramBroker.program_id == program_id,
                    ProgramBroker.status == "active").all()
            }
            q = q.filter(Party.id.in_(on_programme or {-1}))

        return [{"id": r.id, "name": r.legal_name, "party_type": r.party_type}
                for r in q.order_by(Party.legal_name).all()]


# =============================================================================
#  List
# =============================================================================

@router.get("/contracts")
def list_contracts(
    program_id: Optional[int] = Query(None),
    counterparty_id: Optional[int] = Query(None),
    contract_type: Optional[str] = Query(None),
    lifecycle: Optional[str] = Query(None),
    approval_status: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="match on name, UMR or filename"),
    p: Principal = Depends(current_principal),
):
    """Every contract the caller can see, across all their programmes.

    The carrier-wide view the app never had: contracts could only be reached one
    broker or one programme at a time, which is the wrong shape for "what is
    expiring", "what is waiting on me" and "where is that contract".
    """
    with SessionLocal() as s:
        query = s.query(Contract)
        if p.is_broker:
            query = query.filter(Contract.broker_party_id == p.broker_party_id)
        else:
            tid = resolve_tenant_id(s, p)
            query = query.filter(Contract.tenant_id == tid)

        if program_id:
            query = query.filter(Contract.program_id == program_id)
        if counterparty_id:
            query = query.filter(Contract.broker_party_id == counterparty_id)
        if contract_type:
            query = query.filter(Contract.contract_type == contract_type)
        if approval_status:
            query = query.filter(Contract.approval_status == approval_status)
        if q:
            like = f"%{q.strip()}%"
            query = query.filter(or_(Contract.name.ilike(like),
                                     Contract.umr.ilike(like),
                                     Contract.filename.ilike(like)))

        rows = query.order_by(Contract.id.desc()).all()

        # Lifecycle is filtered in Python, not SQL, because `expired` is derived
        # from the expiry date rather than stored — see _effective_lifecycle.
        out = [_record(s, c, with_docs=False, p=p) for c in rows]
        if lifecycle:
            out = [r for r in out if r["lifecycle"] == lifecycle]
        return out


@router.get("/contracts/{contract_id}")
def get_contract(contract_id: int, p: Principal = Depends(current_principal)):
    """One contract, in full — the record, its documents and what may be done."""
    with SessionLocal() as s:
        return _record(s, _contract_access(s, p, contract_id), p=p)


# =============================================================================
#  Create / edit
# =============================================================================

class ContractIn(BaseModel):
    """A contract as a person describes it.

    Every field is optional HERE and the mandatory ones are enforced by
    contract_types.validate() instead, so that "what this type requires" is
    stated exactly once — in contract_types.py — rather than half here in a
    Pydantic model and half there.
    """
    program_id: int
    contract_type: str
    name: Optional[str] = None
    counterparty_party_id: Optional[int] = None
    schedule_key: Optional[str] = None
    output_template_id: Optional[int] = None
    inception_dt: Optional[str] = None
    expiry_dt: Optional[str] = None
    umr: Optional[str] = None
    risk_code: Optional[str] = None
    section_number: Optional[str] = None
    class_of_business: Optional[str] = None
    year_of_account: Optional[str] = None
    earnings_pattern: Optional[str] = None
    executed_date: Optional[str] = None
    notice_period_days: Optional[int] = None
    premium_cap_amount: Optional[float] = None
    premium_cap_currency: Optional[str] = None
    # The authored contract. `commercial_terms` is the deal's numbers
    # (contract_types.COMMERCIAL_TERMS); `wording_sections` is the document as
    # the carrier left it in the Wording step; `signature_layout` moves the
    # blocks on the signature page Kavachio adds. All optional — a contract
    # raised without them is simply one with no authored wording yet.
    agreed_limits: Optional[dict] = None
    wording_sections: Optional[list] = None
    signature_layout: Optional[dict] = None
    # Who signs, named at step 4. Stored with the wording rather than in a
    # column of their own: they belong to this authored document, and the
    # signature screen is the only thing that reads them. Kavachio does not
    # send anything to them — see the signature screen, which says so first.
    signers: Optional[list] = None
    # The contract this one renews, when it is a renewal. A renewal raised here
    # is a NEW contract that points back — last year's terms have to keep
    # meaning what they meant while bordereaux were checked against them, so
    # nothing about the old one is touched.
    renews_contract_id: Optional[int] = None
    # Start a negotiation instead of putting the contract straight in force.
    # Default false, so the carrier's long-standing "what I raise is live on
    # arrival" behaviour is exactly what it was — this is a choice the carrier
    # makes when there is something to agree, not a new hoop for when there
    # isn't. Ignored for a broker, whose contract goes to the carrier either way.
    send_for_review: bool = False
    # What the contract should BE when it is created. The boolean above only
    # ever offered two of the three, and left no way to park a half-written
    # contract — which a four-step authoring flow needs more than most screens,
    # since abandoning it otherwise throws the whole thing away.
    #
    #   "draft"   written down, not live, nothing checked, still editable
    #   "review"  out to the broker to agree
    #   "live"    in force from now
    #
    # `send_for_review` is still honoured when this is absent, so nothing that
    # called the old shape changes behaviour.
    create_as: Optional[str] = None


def _bad_fields(e: ct.ContractTypeError):
    return HTTPException(400, e.to_detail())


def _as_date(v: Any) -> Optional[dt.date]:
    if v in (None, ""):
        return None
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def _check_counterparty(s, p: Principal, spec: dict, program_id: int,
                        party_id: Optional[int], tenant_id: int) -> None:
    """The counterparty must be the RIGHT KIND of organisation for the type.

    An insurer↔broker contract filed against a reinsurer would sit under a party
    whose page cannot show it, and every screen downstream joins contracts to
    parties through this one column. Checked on the way in, where it can still
    be explained, rather than discovered later by a screen that renders nothing.
    """
    if party_id is None:
        return
    party = s.get(Party, party_id)
    if party is None:
        raise HTTPException(400, {"message": "That organisation does not exist.",
                                  "errors": {"counterparty_party_id": "not found"}})
    if party.tenant_id not in (None, tenant_id):
        raise HTTPException(404, "contract not found")

    want = spec["counterparty_party_type"]
    if (party.party_type or "") != want:
        raise HTTPException(400, {
            "message": f"A {spec['label']} contract has to be with a {want}, "
                       f"and {party.legal_name} is a "
                       f"{party.party_type or 'party of no stated type'}.",
            "errors": {"counterparty_party_id": f"must be a {want}"},
        })

    if spec["counterparty_must_be_on_programme"]:
        link = (s.query(ProgramBroker)
                .filter(ProgramBroker.program_id == program_id,
                        ProgramBroker.broker_party_id == party_id)
                .first())
        if link is None:
            raise HTTPException(400, {
                "message": f"{party.legal_name} is not on this programme, so "
                           f"they cannot hold a contract on it. Put them on "
                           f"the programme first.",
                "errors": {"counterparty_party_id": "not on this programme"},
            })
        if link.status != "active":
            raise HTTPException(400, {
                "message": f"{party.legal_name} has been taken off this "
                           f"programme, so no new contract can be filed "
                           f"under them.",
                "errors": {"counterparty_party_id": "taken off this programme"},
            })


@router.post("/contracts")
def create_contract(body: ContractIn, p: Principal = Depends(current_principal)):
    """Raise a contract from its TERMS — no document required.

    This is the part that did not exist. A contract could only be created by
    uploading a PDF, which meant it could not be recorded until someone had the
    executed wording — even though the terms are agreed well before that, and
    the programme, the broker and the dates are all known.

    Without a document there are no clauses and therefore no rules; the contract
    is a record and nothing downstream can be produced against it until the
    wording is attached and read. That is a real state, and saying it plainly is
    better than refusing to record the contract at all.

    Who creates it decides what happens next:
      * a CARRIER owns the book, so its contract is live on arrival;
      * a BROKER gets a draft to finish and submit, and the DB trigger — not
        this code — marks it pending the carrier's decision.
    """
    with SessionLocal() as s:
        prog, tenant_id = _program_access(s, p, body.program_id)

        try:
            spec = ct.spec(body.contract_type)
        except ct.ContractTypeError as e:
            raise _bad_fields(e)

        payload = body.model_dump(exclude_none=False)
        if p.is_broker:
            # Checked BEFORE the field validation, so a broker attempting a
            # reinsurance contract is told the real reason rather than being
            # sent to fill in a year of account it may never have.
            if spec["counterparty_party_type"] != "broker":
                raise HTTPException(
                    403, "a broker can only raise its own insurer ↔ broker "
                         "contracts; a reinsurance contract is the carrier's "
                         "to raise")
            # A broker's contract is always WITH that broker. Substituted before
            # validation rather than after, so the broker is never asked to name
            # a counterparty it is not allowed to choose — and whatever it did
            # send is discarded, so it cannot file under someone else.
            payload["counterparty_party_id"] = resolve_broker_party_id(s, p)

        try:
            clean = ct.validate(body.contract_type, payload)
        except ct.ContractTypeError as e:
            raise _bad_fields(e)

        counterparty_id = clean.get("counterparty_party_id")
        _check_counterparty(s, p, spec, body.program_id, counterparty_id, tenant_id)

        if body.output_template_id:
            tmpl = s.get(ExportTemplate, body.output_template_id)
            if not tmpl:
                raise HTTPException(400, "that output template does not exist")
            assert_tenant_owns(p, tmpl.tenant_id)

        c = Contract(
            program_id=body.program_id,
            tenant_id=tenant_id,
            broker_party_id=counterparty_id,
            contract_type=body.contract_type,
            output_template_id=body.output_template_id,
            schedule_key=clean.get("schedule_key"),
            is_app_managed=True,
            # No file yet. `status_ops` describes what the EXTRACTION pipeline
            # did, and it has not run — "drafted" is the honest value, and the
            # contract's own state lives in `lifecycle`.
            status="drafted",
        )
        # `umr` is deliberately absent — see contract_types.
        for field in ("name", "risk_code", "section_number",
                      "class_of_business", "year_of_account", "earnings_pattern",
                      "premium_cap_currency"):
            setattr(c, field, clean.get(field))
        c.inception_dt = _as_date(clean.get("inception_dt"))
        c.expiry_dt = _as_date(clean.get("expiry_dt"))
        c.executed_date = _as_date(clean.get("executed_date"))
        c.notice_period_days = clean.get("notice_period_days")
        c.premium_cap_amount = clean.get("premium_cap_amount")
        c.commercial_terms = ct.clean_agreed_limits(body.agreed_limits) or None
        if body.renews_contract_id is not None:
            # Must be a contract the caller can actually see, and one nobody has
            # renewed already — two renewals of the same contract would leave
            # two successors claiming the same year.
            prior = _contract_access(s, p, body.renews_contract_id)
            existing = (s.query(Contract)
                        .filter(Contract.renews_contract_id == prior.id).first())
            if existing:
                raise HTTPException(409, {
                    "message": f"That contract has already been renewed — see "
                               f"contract {existing.id}.",
                    "errors": {"renews_contract_id": "already renewed"}})
            c.renews_contract_id = prior.id
        if body.wording_sections:
            c.wording_sections = {
                "sections": [sec for sec in body.wording_sections
                             if isinstance(sec, dict) and (sec.get("body") or "").strip()],
                "signature_layout": body.signature_layout or {},
                "signers": [sg for sg in (body.signers or [])
                            if isinstance(sg, dict) and (sg.get("name") or "").strip()],
            }

        if p.is_broker:
            # Setting the submitter is what makes the DB trigger
            # trg_set_contract_approval mark this pending_approval — from the
            # submitter's STORED role, so a client can never assert "approved".
            #
            # The status is ALSO set here, from the role in the signed token.
            # Not redundancy for its own sake: the trigger reads app_user, and
            # if that lookup ever failed to identify the seat as a broker's, the
            # row would fall through to its 'approved' default — and a
            # half-finished draft would read as live to everything downstream
            # that filters on approved_only. The trigger still runs and still
            # has the last word; this only makes the failure mode safe.
            c.submitted_by_user_id = p.user_id
            c.approval_status = "pending_approval"
            c.lifecycle = "draft"
        else:
            # The carrier owns the book — there is nobody left to approve it.
            c.approved_by_user_id = p.user_id
            # Two ways to finish, and neither is "live". A contract goes in
            # force because both sides SIGNED it — see sign_contract — so
            # creating one already active would be asserting a signing that
            # never happened, in the one place nobody could later point at.
            mode = body.create_as or ("review" if body.send_for_review else "draft")
            if mode == "live":
                raise HTTPException(400, {
                    "message": "a contract cannot be created in force. Both "
                               "sides sign it, and the second signature is "
                               "what puts it in force.",
                    "errors": {"create_as": "not allowed"}})
            if mode not in ("draft", "review"):
                raise HTTPException(400, {
                    "message": f"“{mode}” is not a way to create a contract. "
                               f"Use draft or review.",
                    "errors": {"create_as": "unknown"}})
            if mode == "review":
                # These are PROPOSED terms. They go out to the broker and the
                # contract is not in force until the broker has agreed them and
                # the carrier has put it in force.
                if spec["counterparty_party_type"] != "broker":
                    raise HTTPException(
                        400, "only an insurer ↔ broker contract can go out for "
                             "review — a reinsurer has no seat in Kavachio, so "
                             "there is nobody on the other side to read it")
                c.lifecycle = "in_review"
            elif mode == "draft":
                # Written down and nothing more: not live, nothing checked,
                # and the terms still editable. The carrier's own draft needs
                # no approval — approval_status stays approved, because the
                # gate is about whose contract it is, not whether it is
                # finished. What makes it not-live is the lifecycle.
                c.lifecycle = "draft"
        c.lifecycle_effective_date = c.inception_dt or _today()

        s.add(c)
        s.flush()
        if c.lifecycle == "in_review":
            # The thread starts at the moment the terms go out, so the broker's
            # answer has something to answer.
            s.add(ContractApproval(
                tenant_id=c.tenant_id, contract_id=c.id,
                action="sent_for_review", acted_by_user_id=p.user_id,
                acted_at=dt.datetime.now(dt.timezone.utc), note=None))
        s.commit()
        s.refresh(c)

        # NO document is produced here. A contract written in Kavachio IS its
        # sections and its terms — both structured, both on the record, both
        # readable on its page. Composing a .docx at birth meant the app
        # generated a file, then read the file back to recover clauses it had
        # just written: a round trip that could only lose fidelity, and that
        # existed only because the first version modelled an authored contract
        # as an uploaded one.
        #
        # What the file was for, and what replaced it:
        #   · reading it      → the wording is typeset on the contract's page
        #   · its rules       → derived from the agreed limits directly, which
        #                       is deterministic where extraction was not
        #   · signing it      → the executed copy is attached at signing, and
        #                       that is a document from the real world rather
        #                       than one Kavachio invented
        return _record(s, c, p=p)


class WordingPreviewIn(BaseModel):
    """Everything the builder knows BEFORE the contract exists."""
    contract_type: Optional[str] = None
    values: dict = {}
    agreed_limits: dict = {}
    sections: Optional[list] = None
    carrier_name: Optional[str] = None
    counterparty_name: Optional[str] = None
    programme_name: Optional[str] = None
    signature_layout: Optional[dict] = None
    # Who signs, so a draft circulated for comment already carries the names
    # on its signature page rather than two blank lines.
    signers: Optional[list] = None


@router.post("/contract-wording/preview")
def wording_preview(body: WordingPreviewIn,
                    _p: Principal = Depends(current_principal)):
    """Steps 2 and 3, computed statelessly.

    Nothing is written: the builder calls this as the terms change and gets the
    tokenised sections (or keeps its own edited ones), the resolved text, the
    checks with their severities, the worth-a-look list, and the page estimate.

    Sections come back BOTH tokenised and rendered. The editor needs the tokens
    to keep chips live; the reader needs the words. Sending only one would make
    the other side guess.
    """
    import contract_wording as cw

    limits = ct.clean_agreed_limits(body.agreed_limits)
    try:
        type_label = ct.spec(body.contract_type or ct.DEFAULT_TYPE)["label"]
    except ct.ContractTypeError:
        type_label = None

    sections = [sec for sec in (body.sections or [])
                if isinstance(sec, dict) and (sec.get("body") or "").strip()]
    if not sections:
        sections = cw.build_sections(values=body.values, limits=limits,
                                     type_label=type_label)

    tokens = cw.token_values(
        values=body.values, limits=limits,
        carrier_name=body.carrier_name,
        counterparty_name=body.counterparty_name,
        programme_name=body.programme_name)

    checks, warnings = cw.derive_checks(body.values, limits)
    rendered = [{**sec,
                 "rendered": cw.render(sec.get("body") or "", tokens),
                 "tokens": cw.used_tokens(sec.get("body") or "")}
                for sec in sections]

    return {
        "sections": rendered,
        "tokens": tokens,
        "checks": checks,
        "warnings": warnings,
        # Sections that quote no term produce no check. Not a fault — see
        # contract_wording.uncheckable_sections.
        "uncheckable": cw.uncheckable_sections(sections),
        "pages": cw.estimate_pages(sections),
    }


@router.post("/contract-wording/draft")
def wording_draft(body: WordingPreviewIn,
                  _p: Principal = Depends(current_principal)):
    """Download a draft — the composed PDF, before anything is saved.

    "Nothing has been sent yet" has to stay true while someone takes the draft
    to a colleague, so this writes no row anywhere: the same composer the saved
    contract uses, run on what the builder holds right now.
    """
    import contract_wording as cw

    sections = [sec for sec in (body.sections or [])
                if isinstance(sec, dict) and (sec.get("body") or "").strip()]
    if not sections:
        raise HTTPException(400, "there are no sections to compose — write or "
                                 "generate the wording first")
    limits = ct.clean_agreed_limits(body.agreed_limits)
    try:
        type_label = ct.spec(body.contract_type or ct.DEFAULT_TYPE)["label"]
    except ct.ContractTypeError:
        type_label = None
    tokens = cw.token_values(
        values=body.values, limits=limits,
        carrier_name=body.carrier_name,
        counterparty_name=body.counterparty_name,
        programme_name=body.programme_name)
    # The same whole-contract shape the saved record downloads as — schedule
    # then wording — so the draft somebody circulates and the final file are
    # recognisably the same document.
    data = cw.compose_pdf(
        name=body.values.get("name") or "Contract draft",
        carrier_name=body.carrier_name,
        counterparty_name=body.counterparty_name,
        sections=sections, tokens=tokens,
        schedule=cw.schedule_rows(values=body.values, limits=limits,
                                  type_label=type_label,
                                  programme_name=body.programme_name),
        signers=body.signers, signature_layout=body.signature_layout)
    fname = f"{(body.values.get('name') or 'contract-draft').strip()} (draft).pdf"
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": _content_disposition(fname)})


@router.patch("/contracts/{contract_id}")
def update_contract(contract_id: int, body: dict,
                    p: Principal = Depends(current_principal)):
    """Correct a contract that is still a draft or still awaiting a decision.

    Only those two states. Editing an ACTIVE contract's terms would silently
    change what every bordereau already produced against it was checked for —
    a contract that is in force is changed by an endorsement, which is a
    document with a date on it, not a quiet field edit.
    """
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        state = _effective_lifecycle(c)
        # `changes_requested` is editable BY THE CARRIER and nobody else: the
        # broker asked for a change and this is the carrier answering it. Left
        # out, the negotiation would have a step 3 and no step 4.
        editable = state in ("draft", "pending") or (
            state == "changes_requested" and not p.is_broker)
        if state == "in_review" and not p.is_broker:
            raise HTTPException(
                409,
                "these terms are out with the broker. Changing them underneath "
                "a review would mean they agree to something they never read — "
                "wait for their answer, or ask them to send it back.")
        if not editable:
            raise HTTPException(
                409,
                f"this contract is {state}, so its terms cannot be edited. "
                f"A contract in force is changed by an endorsement, so that "
                f"what it said when a bordereau was checked against it stays "
                f"on the record.")

        new_type = body.get("contract_type") or c.contract_type
        try:
            spec = ct.spec(new_type)
            # partial: an edit may send one field. What it DOES send still has
            # to satisfy the type — a required field may be corrected, never
            # blanked.
            clean = ct.validate(new_type, body, partial=True)
        except ct.ContractTypeError as e:
            raise _bad_fields(e)

        if "counterparty_party_id" in clean:
            if p.is_broker:
                raise HTTPException(403, "a broker's contract is always with "
                                         "that broker")
            _check_counterparty(s, p, spec, c.program_id,
                                clean["counterparty_party_id"], c.tenant_id)
            c.broker_party_id = clean["counterparty_party_id"]

        # Changing type re-validates the WHOLE record against the new type: the
        # fields it now requires may never have been asked for.
        if new_type != c.contract_type:
            merged = {f: getattr(c, ct.FIELDS[f]["attr"]) for f in ct.field_names(new_type)
                      if f != "counterparty_party_id"}
            merged["counterparty_party_id"] = c.broker_party_id
            merged.update(clean)
            try:
                ct.validate(new_type, merged)
            except ct.ContractTypeError as e:
                raise _bad_fields(e)
            c.contract_type = new_type

        for name, value in clean.items():
            if name == "counterparty_party_id":
                continue
            attr = ct.FIELDS[name]["attr"]
            kind = ct.FIELDS[name]["kind"]
            setattr(c, attr, _as_date(value) if kind == "date" else value)

        # The SUBSTANCE of the contract, not just its identity. A draft is a
        # contract nobody has agreed to yet, so everything about it has to be
        # changeable — editing its name but not its commission would be a
        # strange half-draft. The same is true while the broker has asked for
        # changes: what they asked to change is almost always a limit.
        #
        # Both are absent-means-unchanged, so a screen saving one field does not
        # wipe the other.
        if "agreed_limits" in body:
            c.commercial_terms = ct.clean_agreed_limits(body["agreed_limits"]) or None
        if "wording_sections" in body:
            sections = [sec for sec in (body["wording_sections"] or [])
                        if isinstance(sec, dict) and (sec.get("body") or "").strip()]
            existing = dict(c.wording_sections or {})
            existing["sections"] = sections
            c.wording_sections = existing or None

        # A SIGNATURE IS ON A VERSION. Changing what was signed deletes the
        # signatures on it — anything else leaves a name attached to words it
        # was never under, which is worse than no signature at all. Only ever
        # reachable before the contract is in force, since edit is refused
        # after that.
        if ("agreed_limits" in body or "wording_sections" in body):
            gone = (s.query(ContractSignature)
                    .filter(ContractSignature.contract_id == c.id)
                    .delete(synchronize_session=False))
            if gone and _effective_lifecycle(c) == "signed":
                c.lifecycle = "agreed" if c.broker_party_id else "draft"

        # Who signs, and how the blocks are laid out on the signature page.
        # Editable for the same reason the wording is: while a contract is a
        # draft nothing about it has been agreed, and a named signer who has
        # left the company is not a thing to be corrected by endorsement.
        #
        # CARRIER ONLY. Naming the signatories is part of authoring the
        # contract — it is what the signature page of the document is built
        # from — and the broker's part in signing is to sign. Letting them
        # rewrite the list would let one party decide who is competent to bind
        # the other, which is not a thing either side gets to do alone.
        if ("signers" in body or "signature_layout" in body) and p.is_broker:
            raise HTTPException(
                403,
                "who signs is part of the contract the carrier writes. You "
                "sign it — open the signature page and give your signature "
                "there.")
        if "signers" in body:
            existing = dict(c.wording_sections or {})
            existing["signers"] = [
                sg for sg in (body["signers"] or [])
                if isinstance(sg, dict) and (sg.get("name") or "").strip()]
            c.wording_sections = existing or None
        if "signature_layout" in body:
            existing = dict(c.wording_sections or {})
            existing["signature_layout"] = body["signature_layout"] or {}
            c.wording_sections = existing or None

        s.commit()
        s.refresh(c)
        return _record(s, c, p=p)


# =============================================================================
#  Lifecycle
# =============================================================================

def _move(c: Contract, to: str) -> None:
    """Apply a lifecycle transition, or refuse it with the reason.

    The legal moves live in contract_types.LIFECYCLE_TRANSITIONS so the rules
    are readable in one place instead of spread across the handlers.
    """
    frm = _effective_lifecycle(c)
    if to == frm:
        return
    allowed = ct.LIFECYCLE_TRANSITIONS.get(frm, ())
    if to not in allowed:
        raise HTTPException(
            409, f"a {frm} contract cannot become {to}"
                 + (f" — only {', '.join(allowed)}." if allowed
                    else ", because that is where its life ended."))
    c.lifecycle = to
    c.lifecycle_effective_date = _today()


class Note(BaseModel):
    note: Optional[str] = None


@router.post("/contracts/{contract_id}/submit")
def submit_contract(contract_id: int, body: Note = Note(),
                    p: Principal = Depends(current_principal)):
    """Send a draft to the carrier for a decision.

    Refused while the wording defers to a document nobody has supplied: those
    clauses are known to produce no rule, so the carrier would be approving a
    contract whose terms are partly unreadable.
    """
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        docs = s.query(ContractDocument).filter(
            ContractDocument.contract_id == c.id).all()
        missing = _missing_references(c, docs)
        if missing:
            raise HTTPException(400, {
                "message": "This contract defers to document(s) that have not "
                           "been supplied, so some of its clauses cannot be "
                           "checked. Attach them before submitting.",
                "errors": {"documents": ", ".join(missing)},
            })

        try:
            ct.validate(c.contract_type or "", {
                f: getattr(c, ct.FIELDS[f]["attr"])
                for f in ct.field_names(c.contract_type or "")
                if f != "counterparty_party_id"
            } | {"counterparty_party_id": c.broker_party_id})
        except ct.ContractTypeError as e:
            raise _bad_fields(e)

        _move(c, "pending")
        now = dt.datetime.now(dt.timezone.utc)
        c.submitted_at = now
        c.submitted_by_user_id = p.user_id
        # The trigger only fires on INSERT, so a contract submitted after it was
        # drafted has its gate set here — from the submitter's ROLE, never from
        # anything the client said.
        if p.is_broker:
            c.approval_status = "pending_approval"

        s.add(ContractApproval(
            tenant_id=c.tenant_id, contract_id=c.id, action="submitted",
            acted_by_user_id=p.user_id, acted_at=now, note=body.note))
        s.commit()
        s.refresh(c)
        return _record(s, c, p=p)


# =============================================================================
#  Negotiation  —  the carrier proposes, the broker answers
# =============================================================================
#
#   1. Carrier writes the terms                     (create, or edit a draft)
#   2. Carrier sends them out                       send-for-review  → in_review
#   3. Broker reads them and either
#        pushes back                                request-changes  → changes_requested
#        or agrees                                  accept-terms     → agreed
#   4. Carrier revises and re-sends                 edit + send-for-review
#      …steps 2–4 repeat for as long as it takes…
#   5. Terms agreed → signature → in force          activate
#
# Nobody "approves" anything here, and that is the point. The approve/reject
# gate answers "may this broker's contract into my book?", which the carrier
# decides alone. This answers "do we both agree these terms?", which neither
# side can decide alone — so it needed its own states and its own words rather
# than being bent into the existing one.

class ReviewRequest(BaseModel):
    note: Optional[str] = None


@router.post("/contracts/{contract_id}/send-for-review")
def send_for_review(contract_id: int, body: ReviewRequest = ReviewRequest(),
                    p: Principal = Depends(current_principal)):
    """Send the terms to the broker and wait for their answer.

    Used both to open a negotiation and to re-open one after revising in answer
    to a change request — they are the same act, so they are the same endpoint.
    """
    _require_carrier(p)
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        docs = s.query(ContractDocument).filter(
            ContractDocument.contract_id == c.id).all()
        missing = _missing_references(c, docs)
        if missing:
            raise HTTPException(400, {
                "message": "This contract defers to document(s) that have not "
                           "been supplied. Sending it out now would ask the "
                           "broker to agree to terms neither of you can read.",
                "errors": {"documents": ", ".join(missing)}})

        if c.contract_type != "insurer_broker":
            raise HTTPException(
                409, "only an insurer ↔ broker contract can go out for review — "
                     "a reinsurer has no seat in Kavachio, so there is nobody "
                     "on the other side to read it")
        if c.broker_party_id is None:
            raise HTTPException(
                409, "this contract has no broker on it, so there is nobody to "
                     "send it to")

        try:
            ct.validate(c.contract_type or "", {
                f: getattr(c, ct.FIELDS[f]["attr"])
                for f in ct.field_names(c.contract_type or "")
                if f != "counterparty_party_id"
            } | {"counterparty_party_id": c.broker_party_id})
        except ct.ContractTypeError as e:
            raise _bad_fields(e)

        _move(c, "in_review")
        now = dt.datetime.now(dt.timezone.utc)
        s.add(ContractApproval(
            tenant_id=c.tenant_id, contract_id=c.id, action="sent_for_review",
            acted_by_user_id=p.user_id, acted_at=now, note=body.note))
        s.commit()
        s.refresh(c)
        return _record(s, c, p=p)


class ProposedChange(BaseModel):
    """One term the broker wants changed.

    `current` is what it said when they looked, kept so the carrier can tell a
    request that is still about the live value from one that was overtaken by an
    edit in the meantime.
    """
    field: str
    current: Optional[str] = None
    proposed: Optional[str] = None
    comment: Optional[str] = None


class ChangeRequest(BaseModel):
    note: str
    changes: list[ProposedChange] = []


@router.post("/contracts/{contract_id}/request-changes")
def request_changes(contract_id: int, body: ChangeRequest,
                    p: Principal = Depends(current_principal)):
    """Push back on the terms. The broker's half of the negotiation.

    A note is required. "Rejected" with no reason is a wall rather than a
    negotiation, and the carrier cannot answer what it cannot read.

    Named fields are optional but are what make this useful: they let the
    carrier see the request beside the current terms and apply it in one move,
    instead of reading prose and retyping.
    """
    if not p.is_broker:
        raise HTTPException(
            403, "the broker answers a review — the carrier revises and "
                 "re-sends instead")
    if not (body.note or "").strip():
        raise HTTPException(400, {
            "message": "Say what needs to change. The carrier can only answer "
                       "what it can read.",
            "errors": {"note": "required"}})

    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)

        # A request naming a field nobody can change is a dead end, so it is
        # refused here rather than discovered when the carrier tries to apply it.
        #
        # BOTH vocabularies count. A broker pushes back on the commission rate
        # or the per-risk limit — the agreed limits — far more often than on the
        # contract's name. Checking only the identity fields made the negotiation
        # able to argue about everything except the terms.
        known = (set(ct.field_names(c.contract_type or ct.DEFAULT_TYPE))
                 | set(ct.AGREED_LIMITS))
        unknown = [ch.field for ch in body.changes if ch.field not in known]
        if unknown:
            raise HTTPException(400, {
                "message": f"This contract has no term called "
                           f"{', '.join(unknown)}.",
                "errors": {"changes": ", ".join(unknown)}})

        _move(c, "changes_requested")
        now = dt.datetime.now(dt.timezone.utc)
        s.add(ContractApproval(
            tenant_id=c.tenant_id, contract_id=c.id, action="changes_requested",
            acted_by_user_id=p.user_id, acted_at=now, note=body.note.strip(),
            proposed_changes=[ch.model_dump() for ch in body.changes] or None))
        s.commit()
        s.refresh(c)
        return _record(s, c, p=p)


@router.post("/contracts/{contract_id}/accept-terms")
def accept_terms(contract_id: int, body: ReviewRequest = ReviewRequest(),
                 p: Principal = Depends(current_principal)):
    """The broker agrees the terms. Step 4 of the flow.

    This is NOT the signature and it is not the contract going live. It records
    that both sides have settled what the contract says; signing it and putting
    it in force are separate acts, done separately, because they are.
    """
    if not p.is_broker:
        raise HTTPException(
            403, "the broker is the one who agrees to the terms — the carrier "
                 "wrote them")
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        docs = s.query(ContractDocument).filter(
            ContractDocument.contract_id == c.id).all()
        missing = _missing_references(c, docs)
        if missing:
            raise HTTPException(400, {
                "message": "This contract defers to document(s) nobody has "
                           "supplied, so part of what you would be agreeing to "
                           "cannot be read. Ask the carrier for them first.",
                "errors": {"documents": ", ".join(missing)}})

        _move(c, "agreed")
        now = dt.datetime.now(dt.timezone.utc)
        s.add(ContractApproval(
            tenant_id=c.tenant_id, contract_id=c.id, action="terms_agreed",
            acted_by_user_id=p.user_id, acted_at=now, note=body.note))
        s.commit()
        s.refresh(c)
        return _record(s, c, p=p)


class SignedSubmission(BaseModel):
    """Which attachment is the signed copy, when it was signed, and by whom."""
    document_id: Optional[int] = None
    executed_date: Optional[str] = None
    note: Optional[str] = None
    # Who signed for the broker. Optional only so an existing caller does not
    # break; without it the signature is attributed to the broker organisation
    # rather than a person, which is worth avoiding.
    signer_name: Optional[str] = None
    signer_title: Optional[str] = None


@router.post("/contracts/{contract_id}/submit-signed")
def submit_signed(contract_id: int, body: SignedSubmission = SignedSubmission(),
                  p: Principal = Depends(current_principal)):
    """The broker signs and returns the contract to the carrier. Step 5.

    Kavachio does not witness the signing — it records that it happened, which
    is a different and much more honest claim. Two facts land here: the date it
    was executed, and WHICH attachment is the signed copy rather than a draft.
    Both already exist in the data model (contract.executed_date and
    contract_document.is_executed_copy) and neither had anything writing them.

    What happens next is the carrier's PLACEMENT, which Kavachio does not do
    yet. Until it does, the carrier goes from here straight to putting the
    contract in force. That is why `signed` is its own state rather than being
    folded into `active`: the gap where placement belongs stays visible, and
    adding it later means adding one transition rather than unpicking this one.
    """
    if not p.is_broker:
        raise HTTPException(
            403, "the broker signs and returns the contract — the carrier "
                 "receives it")
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)

        doc = None
        if body.document_id is not None:
            doc = s.get(ContractDocument, body.document_id)
            if not doc or doc.contract_id != c.id:
                raise HTTPException(404, "that document is not on this contract")
            if doc.kind != "contract":
                raise HTTPException(
                    400, "the signed copy is the WORDING, not a reference or an "
                         "endorsement")
        else:
            # Default to the live wording, which is what was agreed.
            doc = (s.query(ContractDocument)
                   .filter(ContractDocument.contract_id == c.id,
                           ContractDocument.kind == "contract",
                           ContractDocument.is_active.is_(True))
                   .order_by(ContractDocument.created_at.desc())
                   .first())
        # No document is required. An authored contract has no Kavachio-made
        # file to sign — the wording lives on its page — so what comes back is
        # the executed copy from the real world, attached here or separately.
        # Refusing to record a signature because no file was produced would be
        # refusing the fact for want of the paperwork.
        if doc is not None:
            doc.is_executed_copy = True
        c.executed_date = _as_date(body.executed_date) or _today()
        now = dt.datetime.now(dt.timezone.utc)

        # A SIGNATURE ROW, not just a state change. This endpoint and /sign are
        # two doors into the same fact, and if only one of them wrote the row
        # the gate on going live would read differently depending on which
        # button the broker happened to press.
        counterparty = (s.get(Party, c.broker_party_id)
                        if c.broker_party_id else None)
        existing = _signatures(s, c.id)
        if "counterparty" not in _signed_sides(existing):
            s.add(ContractSignature(
                tenant_id=c.tenant_id, contract_id=c.id, side="counterparty",
                signer_name=(body.signer_name or "").strip()
                            or getattr(counterparty, "legal_name", None)
                            or "the broker",
                signer_title=(body.signer_title or "").strip() or None,
                method="typed", by_user_id=p.user_id, signed_at=now,
                document_id=doc.id if doc is not None else None,
                note=body.note))
            s.flush()

        if _effective_lifecycle(c) != "signed":
            _move(c, "signed")
        # The carrier still has to sign. Going in force is the second
        # signature's job, whichever side gives it — see sign_contract.
        sigs = _signatures(s, c.id)
        if not _unsigned_sides(sigs) and c.approval_status == "approved":
            docs_now = s.query(ContractDocument).filter(
                ContractDocument.contract_id == c.id).all()
            if not _missing_references(c, docs_now):
                _move(c, "active")

        s.add(ContractApproval(
            tenant_id=c.tenant_id, contract_id=c.id, action="signed_submitted",
            acted_by_user_id=p.user_id, acted_at=now, note=body.note))
        s.commit()
        s.refresh(c)
        return _record(s, c, p=p)


# =============================================================================
#  Endorsements  —  amending a contract that is already running
# =============================================================================
#
# A mid-term change is not a new contract and must not become one. The contract
# keeps running, keeps its id, keeps every bordereau already checked against
# it; what changes is some of its terms, from a stated date. So this produces a
# DOCUMENT that says what moved, attaches it alongside the wording (both stay
# active — see ContractDocument), and updates the contract's live terms so the
# checks follow.
#
# The rules are NOT regenerated here. Re-reading a contract is a long job and
# discards the rules currently in force, so it stays the explicit action it
# already is — the response says the rules are now stale and the contract's own
# page has the button.

class EndorsementIn(BaseModel):
    """The changed terms, and when they take effect."""
    agreed_limits: dict = {}
    effective_from: Optional[str] = None
    note: Optional[str] = None
    # Sections as the builder left them, when the carrier has edited the
    # generated wording. Absent means "use what you generate".
    sections: Optional[list] = None


def _endorsement_context(s, c: Contract, body: EndorsementIn):
    """What both the preview and the create need, worked out once."""
    import contract_wording as cw

    old = c.commercial_terms or {}
    new = ct.clean_agreed_limits(body.agreed_limits)
    changes = cw.derive_endorsement_changes(old, new)
    number = 1 + (s.query(ContractDocument)
                  .filter(ContractDocument.contract_id == c.id,
                          ContractDocument.kind == "endorsement")
                  .count())
    sections = [sec for sec in (body.sections or [])
                if isinstance(sec, dict) and (sec.get("body") or "").strip()]
    if not sections:
        sections = cw.build_endorsement_sections(
            contract_name=c.name or c.filename, number=number,
            effective_from=body.effective_from, changes=changes,
            note=body.note)
    return old, new, changes, number, sections


@router.post("/contracts/{contract_id}/endorsement/preview")
def endorsement_preview(contract_id: int, body: EndorsementIn,
                        p: Principal = Depends(current_principal)):
    """What this change would say, and which checks would move.

    Writes nothing. The before/after on every check is the part that matters:
    the carrier is changing what passes on a contract that already has files
    running through it, and needs to see which rows that turns.
    """
    _require_carrier(p)
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        old, new, changes, number, sections = _endorsement_context(s, c, body)
        return {
            "number": number,
            "changes": changes,
            "sections": sections,
            "current_limits": old,
            "contract": {"id": c.id, "name": c.name or c.filename,
                         "inception_dt": str(c.inception_dt) if c.inception_dt else None,
                         "expiry_dt": str(c.expiry_dt) if c.expiry_dt else None},
        }


@router.post("/contracts/{contract_id}/endorsement")
def endorsement_create(contract_id: int, body: EndorsementIn,
                       p: Principal = Depends(current_principal)):
    """Endorse the contract: compose the document, attach it, move the terms.

    Refused when nothing actually changed — an endorsement that amends nothing
    is a document nobody can act on, and it would still count against the
    numbering as if something had happened.
    """
    _require_carrier(p)
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        state = _effective_lifecycle(c)
        if state not in ("active", "expired"):
            raise HTTPException(
                409, f"this contract is {state}. An endorsement changes a "
                     f"contract that is running — a draft is changed by "
                     f"editing its terms, and one that has ended is not "
                     f"changed at all.")

        old, new, changes, number, sections = _endorsement_context(s, c, body)
        if not changes:
            raise HTTPException(400, {
                "message": "Nothing has changed, so there is nothing to "
                           "endorse. Change a term first.",
                "errors": {"agreed_limits": "no change"}})

        import contract_wording as cw
        counterparty = (s.get(Party, c.broker_party_id)
                        if c.broker_party_id else None)
        from db import Tenant
        carrier = s.get(Tenant, c.tenant_id) if c.tenant_id else None
        carrier_name = (getattr(carrier, "legal_name", None)
                        or getattr(carrier, "tenant_name", None))

        data = cw.compose_endorsement_pdf(
            contract_name=c.name or c.filename, number=number,
            carrier_name=carrier_name,
            counterparty_name=getattr(counterparty, "legal_name", None),
            effective_from=body.effective_from, sections=sections)
        filename = f"Endorsement {number} — {(c.name or 'contract').strip()}.pdf"

        # Stored, unlike the wording. See compose_endorsement_pdf: an
        # endorsement records a change that happened on a date, so it must not
        # be regenerated later from terms that have moved on since.
        blob_ref, blob = storage.store_or_keep(
            "contract-documents", c.tenant_id, filename, data, "application/pdf")

        # Attached ALONGSIDE the wording, not replacing it. Both are active and
        # rule generation reads the pair, with the endorsed value winning.
        doc = ContractDocument(
            tenant_id=c.tenant_id, contract_id=c.id, kind="endorsement",
            filename=filename, version=f"endorsement-{number}",
            blob_ref=blob_ref, blob=blob,
            fingerprint=hashlib.sha256(data).hexdigest(),
            effective_from=_as_date(body.effective_from),
            is_active=True, uploaded_by_user_id=p.user_id,
            extracted={"changes": changes, "note": body.note,
                       "endorsement_number": number},
        )
        s.add(doc)

        # The contract's live terms move to the endorsed values. What they WERE
        # is not lost — the endorsement document records every from/to, which is
        # exactly the history somebody asks for later.
        c.commercial_terms = new or None
        # Keep the authored wording's own record of its sections in step with
        # the terms, so re-composing the contract quotes the endorsed numbers.
        s.commit()
        s.refresh(c)

        rec = _record(s, c, p=p)
        rec["endorsement"] = {
            "number": number, "document_id": doc.id, "changes": changes,
            "effective_from": body.effective_from,
        }
        # Attaching an endorsement changes what the rules SHOULD be, and the
        # rules on file were read without it.
        rec["rules_stale"] = True
        return rec


# =============================================================================
#  Signing — what actually puts a contract in force
# =============================================================================
#
# A contract is in force because both sides signed it. That sentence is the
# whole design: there is no "make it live" that skips it, creation cannot
# produce an active contract, and activate() refuses while a side is missing.
#
# Kavachio does not witness a signing. It records that one happened. The two
# claims are kept apart by `method` — see db.ContractSignature — because the
# difference matters to anybody who later asks what this row is evidence of.


def _signatures(s, contract_id: int) -> list[ContractSignature]:
    return (s.query(ContractSignature)
            .filter(ContractSignature.contract_id == contract_id)
            .order_by(ContractSignature.signed_at.asc()).all())


def _signed_sides(sigs) -> set[str]:
    return {sg.side for sg in sigs}


def _unsigned_sides(sigs) -> list[str]:
    return [side for side in ("carrier", "counterparty")
            if side not in _signed_sides(sigs)]


class SignatureIn(BaseModel):
    """One signature. `side` is only accepted from a carrier recording the
    other party's — everybody else signs for their own side and cannot say
    otherwise."""
    signer_name: Optional[str] = None
    signer_title: Optional[str] = None
    signer_email: Optional[str] = None
    side: Optional[str] = None
    # True when the carrier is recording a signature made OUTSIDE Kavachio —
    # on paper, or through a provider. Never attributed to the signatory.
    recorded: bool = False
    document_id: Optional[int] = None
    note: Optional[str] = None


@router.post("/contracts/{contract_id}/sign")
def sign_contract(contract_id: int, body: SignatureIn = SignatureIn(),
                  p: Principal = Depends(current_principal)):
    """Sign a contract, or record a signature made elsewhere.

    WHEN. Only once the terms have stopped moving. A contract still out for
    review, or with a change request unanswered, is a proposal — signing it
    would be signing something the other side is still arguing with.

    WHOSE SIDE. A broker signs for the counterparty and a carrier for the
    carrier; neither can sign for the other by asking. The one exception is a
    carrier RECORDING the counterparty's signature, which is explicit
    (`recorded`), stored as such, and attributed to the person who recorded it
    rather than to the signatory. Without it a reinsurance contract could never
    go live, because a reinsurer has no seat in Kavachio to sign from.

    WHAT IT DOES. The second side's signature normally puts the contract in
    force — that is the point of signing. If something else is still in the way
    (an unapproved contract, a document the wording asked for and nobody
    supplied) it stops at `signed` and says which, rather than half-activating.
    """
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        state = _effective_lifecycle(c)

        if state in ("in_review", "changes_requested", "pending"):
            raise HTTPException(
                409,
                "these terms are still being settled, so there is nothing "
                "final to sign. Agree them first — signing a moving target is "
                "how two sides end up holding different contracts.")
        if state in ("active", "expired", "terminated", "superseded"):
            raise HTTPException(
                409, f"this contract is {state} and is not waiting on a "
                     f"signature.")

        sections = ((c.wording_sections or {}).get("sections")
                    if isinstance(c.wording_sections, dict) else None)
        has_wording = bool(sections) or bool(
            s.query(ContractDocument).filter(
                ContractDocument.contract_id == c.id,
                ContractDocument.kind == "contract",
                ContractDocument.is_active.is_(True)).first())
        if not has_wording:
            raise HTTPException(
                400, "there is nothing to sign yet — this contract has no "
                     "wording. Write it or attach it first.")

        # ── whose side ──
        if p.is_broker:
            if body.recorded:
                raise HTTPException(
                    403, "only the carrier records a signature made outside "
                         "Kavachio")
            side = "counterparty"
        elif body.recorded:
            side = body.side or "counterparty"
            if side not in ("carrier", "counterparty"):
                raise HTTPException(400, "a signature is for the carrier or "
                                         "the counterparty")
        else:
            if body.side and body.side != "carrier":
                raise HTTPException(
                    403,
                    "you can sign for the carrier. To enter a signature the "
                    "other side made on paper or through a provider, record it "
                    "as theirs — it is stored as a recorded signature, not as "
                    "one made here.")
            side = "carrier"

        name = (body.signer_name or "").strip()
        if not name:
            raise HTTPException(400, {
                "message": "a signature needs the name of the person signing.",
                "errors": {"signer_name": "required"}})

        existing = _signatures(s, c.id)
        if any(sg.side == side and (sg.signer_name or "").lower() == name.lower()
               for sg in existing):
            raise HTTPException(
                409, f"{name} has already signed for the "
                     f"{'carrier' if side == 'carrier' else 'counterparty'}.")

        doc_id = None
        if body.document_id is not None:
            doc = s.get(ContractDocument, body.document_id)
            if not doc or doc.contract_id != c.id:
                raise HTTPException(404, "that document is not on this contract")
            doc.is_executed_copy = True
            doc_id = doc.id

        s.add(ContractSignature(
            tenant_id=c.tenant_id, contract_id=c.id, side=side,
            signer_name=name,
            signer_title=(body.signer_title or "").strip() or None,
            signer_email=(body.signer_email or "").strip() or None,
            method="recorded" if body.recorded else "typed",
            by_user_id=p.user_id,
            signed_at=dt.datetime.now(dt.timezone.utc),
            document_id=doc_id, note=(body.note or "").strip() or None))
        s.flush()

        sigs = _signatures(s, c.id)
        both = not _unsigned_sides(sigs)
        blocked_by = None
        if both:
            c.executed_date = c.executed_date or _today()
            docs = s.query(ContractDocument).filter(
                ContractDocument.contract_id == c.id).all()
            missing = _missing_references(c, docs)
            if c.approval_status != "approved":
                blocked_by = ("it has not been approved yet")
            elif missing:
                blocked_by = ("it defers to document(s) nobody has supplied: "
                              + ", ".join(missing))
            if blocked_by:
                # Signed by both, but something else is genuinely in the way.
                # Recorded as signed and said plainly, rather than activated
                # with a hole in it or refused after the signature was given.
                if _effective_lifecycle(c) != "signed":
                    _move(c, "signed")
            else:
                if _effective_lifecycle(c) != "signed":
                    _move(c, "signed")
                _move(c, "active")

        s.commit()
        s.refresh(c)
        rec = _record(s, c, p=p)
        rec["signature_note"] = (
            f"Signed for the {'carrier' if side == 'carrier' else 'counterparty'}"
            f" by {name}."
            + ("" if not both else
               (f" Both sides have now signed, but it cannot go in force while "
                f"{blocked_by}." if blocked_by
                else " Both sides have signed — the contract is in force."))
            + ("" if both else
               f" Waiting on the {_unsigned_sides(sigs)[0]}.")) 
        return rec


@router.delete("/contracts/{contract_id}/sign/{signature_id}")
def unsign_contract(contract_id: int, signature_id: int,
                    p: Principal = Depends(current_principal)):
    """Withdraw a signature that was given in error.

    Only before the contract is in force. Once it is running, a signature is
    part of why — removing it would leave an active contract nobody signed, and
    the way back from there is to terminate it, not to quietly edit the record.
    """
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        if _effective_lifecycle(c) in ("active", "expired", "terminated",
                                       "superseded"):
            raise HTTPException(
                409, "this contract is already in force on the strength of "
                     "these signatures. Terminate it instead — an active "
                     "contract nobody signed is not a state worth having.")
        sg = s.get(ContractSignature, signature_id)
        if not sg or sg.contract_id != c.id:
            raise HTTPException(404, "that signature is not on this contract")
        if p.is_broker and sg.side != "counterparty":
            raise HTTPException(403, "that is not your signature to withdraw")
        s.delete(sg)
        if _effective_lifecycle(c) == "signed":
            c.lifecycle = "agreed" if c.broker_party_id else "draft"
        s.commit()
        s.refresh(c)
        return _record(s, c, p=p)


@router.post("/contracts/{contract_id}/activate")
def activate_contract(contract_id: int, p: Principal = Depends(current_principal)):
    """Put a signed, approved contract in force.

    Separate from approval because they are separate facts: a contract can be
    approved in November and incept in January. Only the carrier does this — a
    broker cannot make its own contract live.

    IT WILL USUALLY HAVE HAPPENED ALREADY. The second signature puts a contract
    in force on its own, so this is the path for the case where something else
    was in the way at that moment — approval outstanding, a referenced document
    missing — and has since been cleared. What it will not do is stand in for
    a signature.
    """
    _require_carrier(p)
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)

        # Signed by both sides, or it does not go in force. This is the gate
        # the whole signing flow exists to hold, so it is checked before
        # anything else — an unsigned contract is not "nearly ready", it is a
        # proposal.
        unsigned = _unsigned_sides(_signatures(s, c.id))
        if unsigned:
            who = " and ".join("the carrier" if u == "carrier"
                               else "the counterparty" for u in unsigned)
            raise HTTPException(409, {
                "message": f"this contract has not been signed by {who}, so it "
                           f"cannot be put in force. A contract goes live "
                           f"because both sides signed it.",
                "errors": {"signatures": ", ".join(unsigned)}})

        if c.approval_status != "approved":
            raise HTTPException(
                409, "this contract has not been approved, so it cannot be put "
                     "in force")
        docs = s.query(ContractDocument).filter(
            ContractDocument.contract_id == c.id).all()
        missing = _missing_references(c, docs)
        if missing:
            raise HTTPException(400, {
                "message": "This contract defers to document(s) that have not "
                           "been supplied. Attach them before putting it in "
                           "force — until then some of its clauses cannot be "
                           "checked.",
                "errors": {"documents": ", ".join(missing)},
            })
        _move(c, "active")
        s.commit()
        s.refresh(c)
        return _record(s, c, p=p)


class Termination(BaseModel):
    reason: str
    terminated_date: Optional[str] = None


@router.post("/contracts/{contract_id}/terminate")
def terminate_contract(contract_id: int, body: Termination,
                       p: Principal = Depends(current_principal)):
    """End a contract early, with the reason on the record.

    A termination without a reason is not a decision anyone can act on later,
    so the reason is required. Notice periods are reported rather than enforced:
    the contract says how much notice is needed, and whether it was actually
    given is a fact about the world that this system does not witness.
    """
    _require_carrier(p)
    if not (body.reason or "").strip():
        raise HTTPException(400, {
            "message": "A termination needs a reason — it is the only thing "
                       "that explains this contract to whoever reads it next.",
            "errors": {"reason": "required"}})

    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        when = _as_date(body.terminated_date) or _today()
        _move(c, "terminated")
        c.terminated_date = when
        c.lifecycle_effective_date = when
        c.termination_reason = body.reason.strip()
        c.status = "superseded"

        notice = c.notice_period_days
        short_notice = None
        if notice and c.expiry_dt:
            days = (c.expiry_dt - when).days
            if days < notice:
                short_notice = (
                    f"This contract asks for {notice} days' notice and there "
                    f"are {max(days, 0)} left before expiry. Recorded as "
                    f"terminated anyway — whether notice was actually served "
                    f"is not something this system can see.")

        s.commit()
        s.refresh(c)
        out = _record(s, c, p=p)
        out["warning"] = short_notice
        return out


class Renewal(BaseModel):
    """What is allowed to CHANGE at renewal. Everything else carries over."""
    inception_dt: str
    expiry_dt: str
    name: Optional[str] = None
    premium_cap_amount: Optional[float] = None
    notice_period_days: Optional[int] = None
    year_of_account: Optional[str] = None
    schedule_key: Optional[str] = None


@router.post("/contracts/{contract_id}/renew")
def renew_contract(contract_id: int, body: Renewal,
                   p: Principal = Depends(current_principal)):
    """Renew into a SUCCESSOR contract, and point it back at this one.

    A renewal is deliberately a new row rather than new dates on the old one.
    Last year's contract has to keep meaning exactly what it meant while
    bordereaux were being checked against it — moving its term would silently
    re-date every decision already made under it.

    The successor starts as a draft with no documents: last year's wording is
    not this year's, and copying it forward would produce rules from a document
    that does not govern the new term.
    """
    _require_carrier(p)
    with SessionLocal() as s:
        old = _contract_access(s, p, contract_id)
        state = _effective_lifecycle(old)
        if state not in ("active", "expired", "terminated"):
            raise HTTPException(409, f"a {state} contract cannot be renewed yet")

        existing = (s.query(Contract)
                    .filter(Contract.renews_contract_id == old.id).first())
        if existing:
            raise HTTPException(
                409, f"this contract has already been renewed — see contract "
                     f"{existing.id}.")

        new = Contract(
            program_id=old.program_id,
            tenant_id=old.tenant_id,
            broker_party_id=old.broker_party_id,
            contract_type=old.contract_type,
            output_template_id=old.output_template_id,
            schedule_key=body.schedule_key or old.schedule_key,
            is_app_managed=True,
            status="drafted",
            renews_contract_id=old.id,
            lifecycle="draft",
        )
        # Carried forward: the terms that identify the contract rather than the
        # term it runs for.
        for field in ("umr", "risk_code", "section_number", "class_of_business",
                      "earnings_pattern", "premium_cap_currency"):
            setattr(new, field, getattr(old, field))
        new.name = body.name or f"{old.name or old.filename or 'Contract'} — renewal"
        new.inception_dt = _as_date(body.inception_dt)
        new.expiry_dt = _as_date(body.expiry_dt)
        new.year_of_account = body.year_of_account or old.year_of_account
        new.notice_period_days = (body.notice_period_days
                                  if body.notice_period_days is not None
                                  else old.notice_period_days)
        new.premium_cap_amount = (body.premium_cap_amount
                                  if body.premium_cap_amount is not None
                                  else old.premium_cap_amount)
        new.lifecycle_effective_date = _today()

        try:
            ct.validate(new.contract_type or ct.DEFAULT_TYPE, {
                f: getattr(new, ct.FIELDS[f]["attr"])
                for f in ct.field_names(new.contract_type or ct.DEFAULT_TYPE)
                if f != "counterparty_party_id"
            } | {"counterparty_party_id": new.broker_party_id})
        except ct.ContractTypeError as e:
            raise _bad_fields(e)

        s.add(new)
        s.flush()

        # The old one is superseded only once it has actually run its course.
        # Renewing an ACTIVE contract early must not switch it off — both are
        # live until the term rolls over.
        if state == "expired":
            _move(old, "superseded")
            old.status = "superseded"

        s.commit()
        s.refresh(new)
        return _record(s, new, p=p)


# =============================================================================
#  Documents
# =============================================================================

@router.get("/contracts/{contract_id}/documents")
def list_documents(contract_id: int, p: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        docs = (s.query(ContractDocument)
                .filter(ContractDocument.contract_id == c.id)
                .order_by(ContractDocument.created_at.desc()).all())
        return {
            "documents": [_doc_dict(d) for d in docs],
            "missing_references": _missing_references(c, docs),
            # What the wording ASKED for, whether or not it has been supplied —
            # so the screen can show the request next to the answer.
            "external_references": _external_references(c),
        }


@router.post("/contracts/{contract_id}/documents")
async def upload_document(
    contract_id: int,
    file: UploadFile = File(...),
    kind: str = Form(default="contract"),
    satisfies_reference: Optional[str] = Form(default=None),
    effective_from: Optional[str] = Form(default=None),
    # Whether this attachment is the SIGNED copy rather than a draft. Kavachio
    # does not witness the signing, so this is a fact reported from outside —
    # which is exactly why it is a flag on the document and not a status the
    # app derives.
    is_executed_copy: bool = Form(default=False),
    p: Principal = Depends(current_principal),
):
    """Attach a document to a contract.

    Three kinds, and the difference matters more than it looks:

      contract     the wording. One active at a time — attaching a new one
                   retires the previous, because a contract has one wording.
      reference    a document the wording defers to. Several may be active; each
                   one clears part of the mandatory-references gate.
      endorsement  a change agreed after the fact. Several may be active, and
                   they stay active ALONGSIDE the wording — that pair is what
                   rule generation reads.

    Uploading does not itself re-read the contract: attaching a file and
    discarding the rules currently in force are different decisions, and the
    second one costs a long pipeline run. Call generate-rules when ready.
    """
    if kind not in DOC_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(DOC_KINDS)}")
    if not file.filename:
        raise HTTPException(400, "the file has no name")

    data = await file.read()
    if not data:
        raise HTTPException(400, "that file is empty")

    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        state = _effective_lifecycle(c)
        if state in ("terminated", "superseded"):
            raise HTTPException(
                409, f"this contract is {state}, so nothing more can be "
                     f"attached to it")

        blob_ref, blob = await run_in_threadpool(
            storage.store_or_keep, "contract-documents", c.tenant_id,
            file.filename, data, file.content_type)

        if kind == "contract":
            # A contract has ONE wording. The previous is retired rather than
            # deleted, so the rules it produced stay explainable.
            (s.query(ContractDocument)
             .filter(ContractDocument.contract_id == c.id,
                     ContractDocument.kind == "contract",
                     ContractDocument.is_active.is_(True))
             .update({"is_active": False}, synchronize_session=False))

        doc = ContractDocument(
            tenant_id=c.tenant_id, contract_id=c.id, kind=kind,
            filename=file.filename,
            satisfies_reference=(satisfies_reference or None),
            blob_ref=blob_ref, blob=blob,
            fingerprint=hashlib.sha256(data).hexdigest(),
            effective_from=_as_date(effective_from),
            is_executed_copy=is_executed_copy or None,
            is_active=True, uploaded_by_user_id=p.user_id,
        )
        s.add(doc)

        # Keep the contract row's own filename pointing at the live wording —
        # every existing screen reads it, and they should not all have to learn
        # about contract_document to show a name.
        if kind == "contract":
            c.filename = file.filename
            if not c.name:
                c.name = file.filename

        s.commit()
        s.refresh(doc)
        docs = s.query(ContractDocument).filter(
            ContractDocument.contract_id == c.id).all()
        return {
            "document": _doc_dict(doc),
            "missing_references": _missing_references(c, docs),
            # Attaching a wording or an endorsement changes what the rules
            # SHOULD be, and the rules on file were generated without it.
            "rules_stale": kind in ("contract", "endorsement"),
        }


@router.delete("/contracts/{contract_id}/documents/{document_id}")
def deactivate_document(contract_id: int, document_id: int,
                        p: Principal = Depends(current_principal)):
    """Retire a document — never delete it.

    The rules currently in force were generated from these documents. Deleting
    one would leave rules nobody can explain, so it is deactivated: out of the
    next generation, still on the record for the last one.
    """
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        doc = s.get(ContractDocument, document_id)
        if not doc or doc.contract_id != c.id:
            raise HTTPException(404, "document not found")
        doc.is_active = False
        s.commit()
        docs = s.query(ContractDocument).filter(
            ContractDocument.contract_id == c.id).all()
        return {"document": _doc_dict(doc),
                "missing_references": _missing_references(c, docs),
                "rules_stale": doc.kind in ("contract", "endorsement")}


@router.get("/contracts/{contract_id}/documents/{document_id}/download")
def download_document(contract_id: int, document_id: int,
                      p: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        doc = s.get(ContractDocument, document_id)
        if not doc or doc.contract_id != c.id:
            raise HTTPException(404, "document not found")
        data = storage.resolve_bytes(doc.blob_ref, doc.blob)
        if data is None:
            raise HTTPException(
                404, "the file for this document is no longer stored")
        return Response(
            content=data, media_type=_media_type(doc.filename),
            headers={"Content-Disposition":
                     _content_disposition(doc.filename or "document")})


@router.get("/contracts/{contract_id}/contract.pdf")
def download_contract_pdf(contract_id: int,
                          p: Principal = Depends(current_principal)):
    """The WHOLE contract as a PDF, composed on the spot.

    Not a document row, deliberately. A contract in Kavachio is its terms and
    the wording written from them, both held as data and read on the screen,
    where they are live — change a limit and the schedule, the sentence quoting
    it and the check behind it all move together. This endpoint exists for the
    one thing a screen cannot do: be sent, printed or signed.

    What comes back is the contract entire — schedule first, then the clauses,
    then a signature page — and it is a DIFFERENT artefact from what is on
    screen: fixed, paginated, and correct only as at the moment it was asked
    for. Composed fresh each time rather than stored, because a stored copy is
    a second version of the contract that stops following its terms.
    """
    import contract_wording as cw
    from db import Tenant

    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        sections = ((c.wording_sections or {}).get("sections")
                    if isinstance(c.wording_sections, dict)
                    else c.wording_sections) or []
        limits = ct.clean_agreed_limits(c.commercial_terms or {})
        if not sections and not limits:
            raise HTTPException(
                404,
                "there is nothing to compose yet — this contract has no terms "
                "and no wording written in Kavachio. If its wording was "
                "uploaded, download the document itself.")

        counterparty = (s.get(Party, c.broker_party_id)
                        if c.broker_party_id else None)
        carrier = s.get(Tenant, c.tenant_id) if c.tenant_id else None
        carrier_name = (getattr(carrier, "legal_name", None)
                        or getattr(carrier, "tenant_name", None))
        programme = s.get(Program, c.program_id) if c.program_id else None
        try:
            type_label = ct.spec(c.contract_type)["label"] if c.contract_type else None
        except ct.ContractTypeError:
            type_label = None

        values = {
            "name": c.name, "inception_dt": c.inception_dt,
            "expiry_dt": c.expiry_dt, "class_of_business": c.class_of_business,
            "schedule_key": c.schedule_key, "risk_code": c.risk_code,
            "section_number": c.section_number,
            "year_of_account": c.year_of_account,
            "notice_period_days": c.notice_period_days,
        }
        programme_name = getattr(programme, "name", None)
        tokens = cw.token_values(
            values=values, limits=limits, carrier_name=carrier_name,
            counterparty_name=getattr(counterparty, "legal_name", None),
            programme_name=programme_name)

        data = cw.compose_pdf(
            name=c.name or "Contract", carrier_name=carrier_name,
            counterparty_name=getattr(counterparty, "legal_name", None),
            sections=sections, tokens=tokens,
            schedule=cw.schedule_rows(values=values, limits=limits,
                                      type_label=type_label,
                                      programme_name=programme_name),
            signers=(c.wording_sections or {}).get("signers")
                    if isinstance(c.wording_sections, dict) else None,
            signature_layout=(c.wording_sections or {}).get("signature_layout")
                             if isinstance(c.wording_sections, dict) else None)

        # The state is in the filename because this file outlives the screen it
        # came from: a draft that reaches somebody's inbox should say so.
        state = _effective_lifecycle(c)
        suffix = "" if state in ("active", "signed") else f" ({state})"
        fname = f"{(c.name or 'contract').strip()}{suffix}.pdf"
        return Response(content=data, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 _content_disposition(fname)})


# =============================================================================
#  Rule generation from the ACTIVE documents
# =============================================================================

def _media_type(filename: str | None) -> str:
    """The document's real type, from its name.

    Everything used to go out as application/octet-stream, which is safe and
    useless: a browser cannot show a PDF it has been told is a byte stream, so
    "view it in the page" was impossible for the one format that can be shown.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument"
                 ".wordprocessingml.document",
        ".doc": "application/msword",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".xlsx": "application/vnd.openxmlformats-officedocument"
                 ".spreadsheetml.sheet",
    }.get(ext, "application/octet-stream")


def _content_disposition(filename: str) -> str:
    """An attachment header that survives a non-ASCII filename.

    Headers are latin-1 on the wire, and contract names carry em-dashes the
    moment someone types one. RFC 5987: an ASCII fallback in `filename`, the
    real name UTF-8-encoded in `filename*` — every current browser prefers the
    second.
    """
    from urllib.parse import quote
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "'")
    return (f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename)}")


def _doc_bytes(d: ContractDocument) -> Optional[bytes]:
    return storage.resolve_bytes(d.blob_ref, d.blob)


def _parse_to_text(data: bytes, filename: str) -> str:
    """Parse one attachment to the plain text the extraction prompt wants."""
    from contract_upload_services.document_extractors import extract_document_data
    from contract_upload_services.prompt_builder import build_llm_context

    suffix = os.path.splitext(filename or "")[1] or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        parsed = extract_document_data(path)
        if parsed.get("type") in ("pdf", "docx", "doc"):
            return build_llm_context(parsed)
        raw = parsed.get("data")
        return raw if isinstance(raw, str) else json.dumps(raw, default=str)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class MapToTemplateIn(BaseModel):
    output_template_id: int


@router.post("/contracts/{contract_id}/map-to-template")
def map_to_template(contract_id: int, body: MapToTemplateIn,
                    p: Principal = Depends(current_principal)):
    """Bind this contract to a bordereau template and build its checks.

    THE MAPPING STEP. A contract is read for its clauses when it arrives and
    nothing more, because a rule is written against a bordereau column and
    until a template is chosen there are no columns. This is the later moment
    the BDX setup reaches when it says which template this contract produces
    into — and it is where the terms become checks.

    For a contract WRITTEN here the mapping is a translation: each agreed limit
    already carries its comparison and the severity the carrier chose, so the
    rules come out deterministically with no model call. For an uploaded PDF
    the clauses had to be interpreted first, and that is generate-rules' job,
    not this one.

    A limit whose column is missing from the template produces no rule and is
    returned in `unmapped`. Binding it to the nearest-looking column would fail
    rows for the wrong reason, which is worse than not checking at all.
    """
    _require_carrier(p)
    import contract_rules as cr
    from app_routes import _template_fields_from_structure

    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        tmpl = s.get(ExportTemplate, body.output_template_id)
        if not tmpl:
            raise HTTPException(400, "that output template does not exist")
        assert_tenant_owns(p, tmpl.tenant_id)

        limits = c.commercial_terms or {}
        if not limits:
            raise HTTPException(409, {
                "message": "This contract has no agreed limits to build checks "
                           "from. A contract read out of a PDF gets its rules "
                           "from its clauses instead — use Re-read rules.",
                "errors": {"agreed_limits": "none"}})

        fields = _template_fields_from_structure(tmpl.structure)
        rules, unmapped = cr.map_limits_to_template(limits, fields)

        from db import engine as _engine
        with _engine.begin() as conn:
            written = cr.write_rules(
                conn, contract_id=c.id, program_id=c.program_id,
                tenant_id=c.tenant_id, rules=rules,
                template_id=body.output_template_id)

        c.output_template_id = body.output_template_id
        s.commit()
        s.refresh(c)

        rec = _record(s, c, p=p)
        rec["mapping"] = {
            "output_template": {"id": tmpl.id, "name": tmpl.name},
            "rules_written": written,
            "rules": rules,
            # Named, not hidden: these are things the contract says that this
            # template cannot measure, and the setup has to show them before
            # anybody treats the checks as complete.
            "unmapped": unmapped,
        }
        return rec


@router.post("/contracts/{contract_id}/generate-rules")
async def generate_rules(contract_id: int,
                         output_template_id: Optional[int] = Form(default=None),
                         p: Principal = Depends(current_principal)):
    """Re-read this contract from its ACTIVE documents and rebuild its rules.

    This is where the three document kinds finally mean something:

      * the WORDING is the document being read;
      * REFERENCES resolve clauses that deferred their content elsewhere, so
        "excluded classes per the Guidelines" becomes a rule with the actual
        list in it;
      * ENDORSEMENTS amend clauses that were already complete, and are fed in
        separately for exactly that reason — a limit that was raised from $5m to
        $7m must produce ONE rule, not two contradictory ones.

    It replaces the rules currently in force for this contract (see
    db_persister's existing_contract_id path), which is why it is an explicit
    action and never a side effect of attaching a file.

    Streams whitespace while it runs: a long contract is 75+ sequential model
    calls and the ingress will drop an idle connection long before that ends.
    Failures therefore arrive as HTTP 200 with {success:false, error:true} in
    the body — the frontend interceptor turns that back into a thrown error.
    """
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        if _effective_lifecycle(c) in ("terminated", "superseded"):
            raise HTTPException(409, "this contract has ended — its rules are "
                                     "history and are not regenerated")

        docs = (s.query(ContractDocument)
                .filter(ContractDocument.contract_id == c.id,
                        ContractDocument.is_active.is_(True)).all())
        missing = _missing_references(c, docs)
        if missing:
            raise HTTPException(400, {
                "message": "This contract defers to document(s) that have not "
                           "been supplied. Rules generated now would silently "
                           "omit whatever those documents contain.",
                "errors": {"documents": ", ".join(missing)}})

        wording = next((d for d in docs if d.kind == "contract"), None)
        wording_bytes = _doc_bytes(wording) if wording else None
        if wording_bytes is None:
            # Fall back to the contract row's own blob: contracts created by the
            # original upload flow have their wording there and no
            # contract_document row at all.
            wording_bytes = storage.resolve_bytes(c.blob_ref, c.blob)
            wording_name = c.filename
        else:
            wording_name = wording.filename

        if not wording_bytes:
            if (c.wording_sections or {}).get("sections"):
                raise HTTPException(409, {
                    "message": "This contract was written here, so there is "
                               "nothing to re-read. Its checks come from the "
                               "terms you agreed, not from reading a document "
                               "back — change a term and they change with it.",
                    "errors": {"wording": "authored, not extracted"}})
            raise HTTPException(
                400, "there is no contract wording attached, so there is "
                     "nothing to read rules from. Attach the contract document "
                     "first.")

        template_id = output_template_id or c.output_template_id
        program_id = c.program_id
        tenant_id = c.tenant_id
        refs = [(d.filename, _doc_bytes(d)) for d in docs if d.kind == "reference"]
        ends = [(d.filename, _doc_bytes(d),
                 str(d.effective_from) if d.effective_from else None)
                for d in docs if d.kind == "endorsement"]

        tmpl = s.get(ExportTemplate, template_id) if template_id else None
        if template_id and not tmpl:
            raise HTTPException(400, "that output template does not exist")
        if tmpl:
            assert_tenant_owns(p, tmpl.tenant_id)
        from app_routes import _template_fields_from_structure
        template_fields = _template_fields_from_structure(tmpl.structure) if tmpl else []

    suffix = os.path.splitext(wording_name or "")[1] or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(wording_bytes)
        wording_path = tmp.name

    async def _pipeline() -> dict:
        from contract_upload_services.contract_extraction_service import (
            ContractExtractionService,
        )
        from contract_upload_services.contract_versioning import (
            compute_content_fingerprint, compute_entity_fingerprint,
        )
        from contract_upload_services.db_persister import persist_pipeline_output
        from contract_upload_services.document_extractors import extract_document_data
        from contract_upload_services.prompt_builder import build_llm_context

        reference_documents = []
        for name, data in refs:
            if data:
                reference_documents.append(
                    {"name": name, "text": await run_in_threadpool(
                        _parse_to_text, data, name)})
        endorsements = []
        for name, data, eff in ends:
            if data:
                endorsements.append(
                    {"name": name, "effective_from": eff,
                     "text": await run_in_threadpool(_parse_to_text, data, name)})

        parsed = await run_in_threadpool(extract_document_data, wording_path)
        document_text = build_llm_context(parsed)
        content_fp = compute_content_fingerprint(
            document_text, template_fields, template_id)
        entity_fp = compute_entity_fingerprint(program_id, wording_name)

        service = ContractExtractionService()
        extraction_output = await run_in_threadpool(
            service.process_contract,
            wording_path,
            template_fields=template_fields if template_fields else None,
            reference_documents=reference_documents or None,
            endorsements=endorsements or None,
            tenant_id=tenant_id,
        )

        persist_result = await run_in_threadpool(
            persist_pipeline_output,
            extraction_output, program_id, wording_name,
            output_template_id=template_id,
            content_fingerprint=content_fp,
            entity_fingerprint=entity_fp,
            # Write INTO this contract. Without it the pipeline would create a
            # second contract row and leave the one the user is looking at with
            # no rules at all.
            existing_contract_id=contract_id,
        )

        with SessionLocal() as s2:
            c2 = s2.get(Contract, contract_id)
            if c2 is not None and template_id:
                c2.output_template_id = template_id
                s2.commit()
            record = _record(s2, s2.get(Contract, contract_id), p=p)

        return {
            "success": True,
            "contract": record,
            "counts": (persist_result or {}).get("counts"),
            "endorsements_applied": [e["name"] for e in endorsements],
            "references_applied": [r["name"] for r in reference_documents],
        }

    task = asyncio.create_task(_pipeline())

    def _on_done(t: asyncio.Task) -> None:
        if not t.cancelled():
            t.exception()
        try:
            os.remove(wording_path)
        except OSError:
            pass

    task.add_done_callback(_on_done)

    async def _stream():
        yield b" "
        while True:
            done, _ = await asyncio.wait({task}, timeout=20)
            if done:
                break
            yield b" "
        try:
            result = task.result()
        except Exception as e:  # noqa: BLE001
            print(f"[Contract] rule generation failed: {e}")
            result = {
                "success": False, "error": True,
                "status_code": e.status_code if isinstance(e, HTTPException) else 500,
                "detail": e.detail if isinstance(e, HTTPException) else str(e),
            }
        yield json.dumps(jsonable_encoder(result)).encode()

    return StreamingResponse(
        _stream(), media_type="application/json",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
