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

Two status axes, deliberately not collapsed into one:

    lifecycle         where the CONTRACT is: draft → pending → active →
                      expired / terminated / superseded.
    status_ops        what the extraction pipeline did with the file.

They answer different questions and a contract routinely differs on both —
extracted, and not yet in force because its term starts in January.

WHO MAY DO WHAT
    A carrier admin owns the book and raises every contract in it.
    A broker is a PARTY to a contract, not its author: they read the terms sent
    to them, push back on them, agree them and sign — but they do not bring one.
    There was once a third path, where a broker uploaded a contract and the
    carrier approved it into the book; the upload and the approval gate that
    policed it were removed together, since neither is worth anything alone.
"""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import hashlib
import json
import logging
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
from sqlalchemy import String, false, func, or_
from sqlalchemy import text as sa_text

import contract_types as ct
import esign_pdf
import storage
from auth_deps import Principal, current_principal, resolve_broker_party_id
from carrier_scope import assert_can_open_contract
from app_routes import _iso_utc, assert_tenant_owns, resolve_tenant_id
from db import (
    AppUser, Contract, ContractApproval, ContractDocument, ExportTemplate,
    ContractSignature, Party, Program, ProgramBroker, SessionLocal,
)

log = logging.getLogger("kavachio.contracts")

router = APIRouter()

# The kinds of document a contract can hold. See ContractDocument for what each
# one MEANS — the difference between a reference and an endorsement is the whole
# reason this is not a single blob column.
DOC_KINDS = ("contract", "reference", "endorsement", "evidence")


def _doc_visible(d: ContractDocument, p: Principal | None) -> bool:
    """May this caller see this document at all? An `evidence` file (what a
    large date correction rests on, Phase B / B1) is the carrier's own record:
    never a broker's, and never served where the caller is not known. Applied
    on the SERVER — the record, the document list and the download — so a
    screen that forgot to hide it could still not show it."""
    if d.kind != "evidence":
        return True
    return p is not None and not p.is_broker


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
    """Fetch a contract the caller is allowed to see.

    Every read and every write in this module comes through here, so the
    platform seat is refused once, at the door, rather than on thirty
    endpoints — and a new contract endpoint is covered the day it is written.
    Asked BEFORE the row is fetched, so the answer cannot differ by contract:
    it is about who is asking (see assert_can_open_contract).
    """
    assert_can_open_contract(p)
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

    A document the carrier chose to go on WITHOUT ("Continue anyway" after
    creating a renewal; extracted.references_waived) is no longer missing: it
    was a decision, not an oversight. A re-read rewrites `extracted` and so
    asks again.
    """
    waived = {str(n).strip().lower()
              for n in ((c.extracted or {}).get("references_waived") or [])}
    supplied = [d for d in docs if d.kind == "reference" and d.is_active]
    if not supplied:
        return [n for n in (
            (r.get("document_name") or "").strip() for r in _external_references(c)
        ) if n and n.lower() not in waived]

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
        if (named and named.lower() not in waived
                and not any(covers(d, named) for d in supplied)):
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


def _store_authored_clauses(s, c: Contract, sections: list[dict] | None) -> int:
    """Write an authored wording's sections into `clauses_extracted`.

    A contract WRITTEN here had no clauses at all. Uploading one runs the
    extraction pipeline and lands rows in `clauses_extracted`; authoring one
    only ever stored `wording_sections` on the contract, so every screen and
    query downstream that asks "what does this contract say" — the clause list,
    the routing, the exception's source text — found nothing for it.

    There is nothing to EXTRACT here in the model sense: the clauses were typed,
    so the text is already the text. This just puts it where clauses live.

    STORED AS WORDS, NOT AS TOKENS. A section body holds `{{carrier_name}}` so
    the sentence can follow the term it quotes — but a clause row is READ, by
    people and by everything downstream, and "{{carrier_name}} may bind" is not
    a clause anybody can act on. The tokens are resolved on the way in. The
    body keeps its tokens on the contract, which is what makes the wording
    move when a term does; this is the settled text at the moment it was
    saved, and it is rewritten whenever the wording or the terms change.

    CLAUSES ONLY — NO RULES. `rule_generation_status` is left `pending`, the
    state that means "not looked at yet", because a rule is written against a
    bordereau template's COLUMNS and no template is chosen at authoring time.
    That mapping happens once during BDX setup, which is the only moment the
    columns are known. See contract_rules.map_limits_to_template.
    """
    import contract_wording as cw
    from sqlalchemy import text as _sql
    rows = [sec for sec in (sections or [])
            if isinstance(sec, dict) and (sec.get("body") or "").strip()]
    tokens = _wording_context(s, c)["tokens"]
    # Replace rather than append: the wording is saved whole, so the clauses
    # are whatever it now says. Appending would leave the deleted ones behind.
    s.execute(_sql("DELETE FROM clauses_extracted WHERE contract_id = :cid "
                   "AND created_by = 'contract_wording'"), {"cid": c.id})
    for i, sec in enumerate(rows, start=1):
        s.execute(_sql("""
            INSERT INTO clauses_extracted
                (contract_id, clause_type, title, text, section_header,
                 rule_generation_status, generated_rule_count,
                 extraction_confidence, created_by, updated_by)
            VALUES
                (:cid, 'other', :title, :text, :header,
                 'pending', 0, 1.0, 'contract_wording', 'contract_wording')
        """), {"cid": c.id, "title": sec.get("title"),
               "text": cw.render((sec.get("body") or ""), tokens).strip(),
               "header": f"{i}. {sec.get('title') or 'Section'}"})
    return len(rows)


def _wording_context(s, c: Contract) -> dict:
    """Everything a contract's wording needs to be read: its values, its limits,
    and what every token in it resolves to.

    Pulled out of the PDF composer so that the SCREEN and the DOCUMENT resolve a
    token the same way. They did not: the record page carried a second resolver
    written in TypeScript that knew about agreed limits and nothing else, so a
    clause naming the parties rendered as "carrier_name" and one naming the
    term as "from inception to expiry". A token means one thing, and this is
    where that one thing is worked out.
    """
    import contract_wording as cw
    from db import Tenant

    limits = ct.clean_agreed_limits(c.commercial_terms or {})
    counterparty = (s.get(Party, c.broker_party_id)
                    if c.broker_party_id else None)
    carrier = s.get(Tenant, c.tenant_id) if c.tenant_id else None
    programme = s.get(Program, c.program_id) if c.program_id else None
    carrier_name = (getattr(carrier, "legal_name", None)
                    or getattr(carrier, "tenant_name", None))
    counterparty_name = getattr(counterparty, "legal_name", None)
    programme_name = getattr(programme, "name", None)
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
    return {
        "values": values,
        "limits": limits,
        "tokens": cw.token_values(
            values=values, limits=limits, carrier_name=carrier_name,
            counterparty_name=counterparty_name,
            programme_name=programme_name),
        "carrier_name": carrier_name,
        "counterparty_name": counterparty_name,
        "programme_name": programme_name,
        "type_label": type_label,
    }


def _codes():
    """intake_service, imported late: it is heavy, and only its two short-code
    functions are wanted here."""
    import intake_service
    return intake_service


_CHAIN_LIMIT = 50


def _has_live_setup(s, c: Contract) -> bool:
    import intake_autorun
    return intake_autorun._live_pipeline(s, c.tenant_id, c.program_id,
                                         c.broker_party_id) is not None


def _has_clauses(s, contract_id: int) -> bool:
    from sqlalchemy import text as _sql
    return s.execute(_sql(
        "SELECT 1 FROM clauses_extracted WHERE contract_id = :c LIMIT 1"),
        {"c": contract_id}).first() is not None


def _renewal_chain(c, get, successor_of) -> list:
    """Every contract period linked to `c` by renewal, oldest first.

    Worked out from `renews_contract_id` alone: back to the first contract,
    then forward through each renewal. Nothing is stored for it. `get(id)` and
    `successor_of(id)` are passed in so the walk can be tested without a
    database. A loop or a broken link stops the walk rather than spinning.
    """
    seen = {c.id}
    first = c
    while first.renews_contract_id and len(seen) < _CHAIN_LIMIT:
        prev = get(first.renews_contract_id)
        if prev is None or prev.id in seen:
            break
        seen.add(prev.id)
        first = prev
    chain = [first]
    ids = {first.id}
    while len(chain) < _CHAIN_LIMIT:
        nxt = successor_of(chain[-1].id)
        if nxt is None or nxt.id in ids:
            break
        ids.add(nxt.id)
        chain.append(nxt)
    if c.id not in ids:
        # The walk forward took another branch; the history is at least
        # everything up to and including this contract.
        back = []
        cur = c
        while cur is not None and len(back) < _CHAIN_LIMIT:
            back.append(cur)
            if cur.id == first.id or not cur.renews_contract_id:
                break
            cur = get(cur.renews_contract_id)
        chain = list(reversed(back))
    return chain


_RENEWAL_WINDOW_DEFAULT = 30


def _renewal_window_days() -> int:
    """How many days before its expiry an ACTIVE contract may be renewed.

    RENEWAL_WINDOW_DAYS in the environment; 30 when unset or not a whole
    number. Renewing earlier set up next year's contract months ahead, from one
    stray click."""
    try:
        return max(0, int(os.getenv("RENEWAL_WINDOW_DAYS",
                                    str(_RENEWAL_WINDOW_DEFAULT)).strip()))
    except ValueError:
        return _RENEWAL_WINDOW_DEFAULT


def renewal_gate(state: str, expiry: Optional[dt.date], today: dt.date,
                 window_days: int, successor=None) -> dict:
    """May this contract be renewed now? The ONE rule, read by the Renew
    button (actions.renew) and enforced by POST /renew.

      * already renewed (a draft or live renewal exists) → no: the screen links
        to that renewal instead, and a second one is refused;
      * expired or terminated → yes (the term is over either way; a
        terminated contract stays terminated when its renewal goes live);
      * active → yes from `window_days` before expiry (inclusive), and on the
        expiry day itself; before that, no, and `opens_on` says when. An
        active contract with no expiry has no date to wait for → yes;
      * superseded, and anything not yet in force → no.

    Returns {allowed, reason, opens_on}; reason is None when allowed, else
    "already_renewed" | "too_early" | "not_renewable". Pure, so the boundary
    days can be tested without a database.
    """
    if successor is not None:
        return {"allowed": False, "reason": "already_renewed", "opens_on": None}
    if state in ("expired", "terminated"):
        return {"allowed": True, "reason": None, "opens_on": None}
    if state == "active":
        if expiry is None:
            return {"allowed": True, "reason": None, "opens_on": None}
        opens = expiry - dt.timedelta(days=window_days)
        if today >= opens:
            return {"allowed": True, "reason": None, "opens_on": None}
        return {"allowed": False, "reason": "too_early", "opens_on": opens}
    return {"allowed": False, "reason": "not_renewable", "opens_on": None}


def _successors(s, ids: list[int]) -> dict[int, Contract]:
    """{contract id: the renewal that renews it} for many contracts in one
    query — the first by id, the same one POST /renew finds."""
    if not ids:
        return {}
    out: dict[int, Contract] = {}
    for r in (s.query(Contract).filter(Contract.renews_contract_id.in_(ids))
              .order_by(Contract.id).all()):
        out.setdefault(r.renews_contract_id, r)
    return out


def _renewal_dict(s, c: Contract, p: Principal | None,
                  successors: dict[int, Contract] | None) -> dict:
    """What the Renew area of the screen shows, and whether Renew is allowed.

    `existing` (the renewal already made, with its state) and `opens_on` are
    for the carrier only: a broker never renews, and a draft renewal is not
    theirs to see."""
    succ = (successors.get(c.id) if successors is not None
            else _successors(s, [c.id]).get(c.id))
    window = _renewal_window_days()
    gate = renewal_gate(_effective_lifecycle(c), c.expiry_dt, _today(),
                        window, succ)
    carrier = bool(p and not p.is_broker)
    import renewal_chain as _rc
    suggested = _rc.suggested_inception(c)
    return {
        "allowed": gate["allowed"],
        "reason": gate["reason"],
        "opens_on": (str(gate["opens_on"]) if carrier and gate["opens_on"]
                     else None),
        "window_days": window,
        # Where the Renew form starts its inception (renewal_chain.
        # suggested_inception): the end date for a "1 Oct – 1 Oct" term (a
        # handover), else the day after.
        "suggested_inception": (str(suggested) if carrier and gate["allowed"]
                                and suggested else None),
        "existing": ({"id": succ.id,
                      "name": succ.name or succ.filename or f"Contract {succ.id}",
                      "lifecycle": _effective_lifecycle(succ)}
                     if carrier and succ is not None else None),
    }


def _period_conflicts(s, c: Contract,
                      successors: dict[int, Contract] | None = None) -> list[dict]:
    """This version's overlaps and gaps with the version it renews and the one
    that renews it (renewal_chain.term_conflicts). Reported, never fixed."""
    import renewal_chain as _rc
    prev = s.get(Contract, c.renews_contract_id) if c.renews_contract_id else None
    nxt = (successors.get(c.id) if successors is not None
           else _successors(s, [c.id]).get(c.id))
    if prev is None and nxt is None:
        return []
    return _rc.term_conflicts(c.inception_dt, c.expiry_dt, prev=prev,
                              next_version=nxt)


def _gap_already_confirmed(s, c: Contract, gap: dict) -> bool:
    """Has somebody already said this gap is intended (D2)? Read from the audit
    log: a contract_term_gap_confirmed row for either side of the gap, naming
    the other side and the same number of days. Dates moved since then make a
    different gap, which is asked about again."""
    from db import ActivityEvent
    other = gap.get("with_id")
    pairs = {(c.id, other), (other, c.id)}
    # A gap confirmed while correcting a contract's dates (Change period) is
    # recorded on that correction or undo row, as `gaps_confirmed`.
    rows = (s.query(ActivityEvent.target, ActivityEvent.details)
            .filter(ActivityEvent.action.in_(("contract_term_gap_confirmed",
                                              "contract_period_corrected",
                                              "contract_period_correction_undone")),
                    ActivityEvent.tenant_id == c.tenant_id,
                    ActivityEvent.target.in_([f"contract:{c.id}",
                                              f"contract:{other}"]))
            .all())
    for target, details in rows:
        try:
            owner = int(str(target).split(":", 1)[1])
        except (IndexError, ValueError):
            continue
        d = details or {}
        for g in (d.get("gaps") or []) + (d.get("gaps_confirmed") or []):
            if ((owner, g.get("with_id")) in pairs
                    and g.get("days") == gap.get("days")):
                return True
    return False


def _detail_text(detail) -> str:
    """An HTTPException's detail as one sentence."""
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail)


def _parties_reason(s, c: Contract, e: HTTPException) -> str:
    """_recheck_renewal_parties' refusal, said so a person can act on it. Its
    404s ("contract not found") are written not to reveal another carrier's
    records to an outsider — but this is the carrier's own contract naming its
    own broker, and "not found" on a page showing the contract explains
    nothing."""
    if e.status_code != 404:
        return _detail_text(e.detail)
    party = s.get(Party, c.broker_party_id) if c.broker_party_id else None
    if party is not None and not _carrier_works_with(s, c.broker_party_id,
                                                     party.tenant_id, c.tenant_id):
        return (f"its broker, {party.legal_name}, is recorded under another "
                f"organisation, not as one of this carrier's brokers. The "
                f"broker needs to be set up for this carrier first.")
    return "its programme is not open to this carrier any more."


def _renewal_activation(s, c: Contract, docs: list[ContractDocument],
                        missing: list[str], signed_copy_due: bool,
                        p: Principal | None,
                        successors: dict[int, Contract] | None = None
                        ) -> Optional[dict]:
    """Can this UPLOADED renewal be made active now — and if not, why not.

    Its "Make it active" stays on the page until it is (B5), so the page needs
    the same answer the server will give when it is pressed:
    _check_renewal_activation refuses on exactly these blockers. Nothing here
    is waived — the signed copy, the read, the references, the people it names
    and its dates are what an uploaded renewal needs to go in force. A gap is
    not a blocker; it is asked about once (D2) and then remembered.

    None when it does not apply: not a renewal, not on the uploaded road, past
    the point of activating, or a broker asking.
    """
    if (not c.renews_contract_id or p is None or p.is_broker
            or _effective_lifecycle(c) not in ("draft", "pending")
            or not (signed_copy_due or _is_uploaded(c))):
        return None
    blockers: list[dict] = []
    wording = [d for d in docs if d.is_active and d.kind == "contract"]
    if signed_copy_due:
        blockers.append({
            "code": "signed_copy",
            "message": "the signed renewal contract (PDF) is not attached yet."})
    elif wording and not any(d.is_executed_copy for d in wording):
        blockers.append({
            "code": "signed_copy_unmarked",
            "message": "the renewal PDF on file is not marked as the signed "
                       "copy. Attach the signed PDF again under Documents with "
                       "“This is the signed copy” ticked."})
    if wording and not _has_clauses(s, c.id):
        blockers.append({
            "code": "unread",
            "message": "the new contract has not been read yet, so there is "
                       "nothing to check files against."})
    if missing:
        blockers.append({
            "code": "references",
            "message": "it refers to documents that are not on file: "
                       + ", ".join(missing) + "."})
    try:
        _recheck_renewal_parties(s, p, c)
    except HTTPException as e:
        blockers.append({"code": "parties", "message": _parties_reason(s, c, e)})
    gaps: list[dict] = []
    import renewal_chain as _rc
    if _rc.overlap_check_enabled():
        conflicts = _period_conflicts(s, c, successors)
        blockers += [{"code": "dates", "message": b["message"],
                      "with_id": b["with_id"]} for b in _rc.blocking(conflicts)]
        gaps = [w for w in _rc.warnings(conflicts)
                if not _gap_already_confirmed(s, c, w)]
    return {"ready": not blockers, "blockers": blockers, "gaps": gaps}


def _check_renewal_activation(s, c: Contract, p: Principal, *,
                              confirmed: bool) -> list[dict]:
    """Refuse to put an uploaded renewal in force while _renewal_activation
    names anything in the way, and ask about a gap nobody has confirmed yet.
    Returns the gaps confirmed now, for the audit log after the commit."""
    docs = s.query(ContractDocument).filter(
        ContractDocument.contract_id == c.id).all()
    act = _renewal_activation(s, c, docs, _missing_references(c, docs),
                              _awaiting_signed_copy(s, c), p)
    if act is None:
        return []
    if act["blockers"]:
        raise HTTPException(409, {
            "message": ("This renewal cannot go in force yet: "
                        + act["blockers"][0]["message"]),
            "errors": {},
            "blockers": act["blockers"]})
    if act["gaps"] and not confirmed:
        g = act["gaps"][0]
        raise HTTPException(409, {
            "message": g["message"] + " Confirm the gap is intended to go ahead.",
            "errors": {},
            "conflicts": act["gaps"],
            "needs_confirm": True})
    return act["gaps"]


def _period_change_info(s, c: Contract, p: Principal | None) -> Optional[dict]:
    import period_change as pc
    if not pc.enabled() or p is None or p.is_broker:
        return None
    allowed = pc.allowed(_effective_lifecycle(c))
    return {"allowed": allowed, "can_change": any(allowed.values()),
            "large_days": pc.large_days(), "history": _period_history(s, c)}


def _renewal_history(s, c: Contract, p: Principal | None) -> list[dict]:
    """The contract's renewal periods, for the Renewal history card.

    Contract PERIODS only — the files attached to each one are document
    history, served separately under `documents`. Empty when the contract was
    never renewed and renews nothing. Versions the caller may not open are left
    out rather than shown as links that would refuse them; the numbering still
    counts them, so a version keeps the same number for everyone.
    """
    chain = _renewal_chain(
        c,
        lambda cid: s.get(Contract, cid),
        lambda cid: (s.query(Contract)
                     .filter(Contract.renews_contract_id == cid)
                     .order_by(Contract.id).first()))
    if len(chain) < 2:
        return []

    def visible(k: Contract) -> bool:
        if k.id == c.id:
            return True
        if p is None:
            return k.tenant_id == c.tenant_id
        try:
            _contract_access(s, p, k.id)
            return True
        except HTTPException:
            return False

    return [{
        "id": k.id,
        "version": i,
        "name": k.name or k.filename or f"Contract {k.id}",
        "inception_dt": str(k.inception_dt) if k.inception_dt else None,
        "expiry_dt": str(k.expiry_dt) if k.expiry_dt else None,
        "lifecycle": _effective_lifecycle(k),
        "is_current": k.id == c.id,
    } for i, k in enumerate(chain, start=1) if visible(k)]


def _record(s, c: Contract, *, with_docs: bool = True,
            p: Principal | None = None,
            rule_counts: dict[int, dict] | None = None,
            successors: dict[int, Contract] | None = None) -> dict:
    """The contract, as the screens need it.

    Deliberately fat: a contract screen that had to make five calls to say what
    state a contract is in would show four different half-states while they
    landed.

    `rule_counts` is each contract's rules counted by source (_rule_tallies),
    passed in rather than counted here so the LIST can answer it for every row
    in one query instead of one per row. Left out, this counts its own.
    `successors` (_successors) is the same idea for "has it been renewed".
    """
    prog = s.get(Program, c.program_id) if c.program_id else None
    counterparty = s.get(Party, c.broker_party_id) if c.broker_party_id else None
    tmpl = (s.get(ExportTemplate, c.output_template_id)
            if c.output_template_id else None)
    docs = []
    if with_docs:
        docs = [d for d in (s.query(ContractDocument)
                            .filter(ContractDocument.contract_id == c.id)
                            .order_by(ContractDocument.created_at.desc())
                            .all())
                if _doc_visible(d, p)]

    ctype = (c.contract_type or "")
    known_type = ctype in ct.CONTRACT_TYPES
    missing = _missing_references(c, docs) if with_docs else []
    active_docs = [d for d in docs if d.is_active]
    sigs = _signatures(s, c.id)

    import contract_wording as cw
    wording_sections = (c.wording_sections or {}).get("sections")
    unquoted: list[dict] = []
    # Only for the detail view. The LIST renders no wording, and resolving every
    # row's tokens would be three extra reads per contract to compose sentences
    # nobody is going to see.
    if with_docs and wording_sections:
        ctx = _wording_context(s, c)
        wording_sections = [
            {**sec, "rendered": cw.render(sec.get("body") or "", ctx["tokens"])}
            for sec in wording_sections if isinstance(sec, dict)]
        unquoted = [
            {"key": k,
             "question": ct.AGREED_LIMITS[k]["question"],
             "value": ctx["tokens"].get(k)}
            for k in cw.unquoted_terms(wording_sections, ctx["limits"])
            if k in ct.AGREED_LIMITS]

    has_wording = (bool((c.wording_sections or {}).get("sections"))
                   or any(d.kind == "contract" and d.is_active for d in active_docs)
                   or bool(c.blob_ref or c.blob))
    signed_copy_due = _awaiting_signed_copy(s, c)
    renewal = _renewal_dict(s, c, p, successors)

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
        # The short codes a broker writes in an email subject or file name
        # instead of the names (intake_service.programme_code).
        "code": _codes().contract_code(c),
        "programme": ({"id": prog.id, "name": prog.name,
                       "code": _codes().programme_code(prog)} if prog else None),
        "counterparty": ({
            "id": counterparty.id,
            "name": counterparty.legal_name,
            "party_type": counterparty.party_type,
        } if counterparty else None),
        "output_template": ({"id": tmpl.id, "name": tmpl.name,
                             "version": tmpl.version} if tmpl else None),
        "schedule_key": c.schedule_key,

        # ── the record ──
        # No `risk_code` / `section_number`: the form does not ask for them and
        # the record no longer shows them, so serving a column nothing reads
        # would just be rot. A contract that carries one from an upload or a
        # renewal still PRINTS it on its schedule — see
        # contract_wording.schedule_rows.
        "umr": c.umr,
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
        "status_ops": c.status,
        "terminated_date": str(c.terminated_date) if c.terminated_date else None,
        "termination_reason": c.termination_reason,
        "renews_contract_id": c.renews_contract_id,
        # Every period in this contract's renewal chain (see _renewal_history).
        # Only on the full record: the list view would pay a walk per row.
        "renewal_history": _renewal_history(s, c, p) if with_docs else None,
        # The Renew area: whether Renew is offered (see renewal_gate), the
        # renewal already made (linked instead of a second Renew), and the date
        # Renew opens for an active contract not yet in its window.
        "renewal": renewal,
        # This version against the ones either side of it (D1/D2): overlaps and
        # gaps, for the amber note. Carrier side only; full record only.
        "period_conflicts": (_period_conflicts(s, c, successors)
                             if with_docs and p is not None and not p.is_broker
                             else None),
        # An uploaded renewal not yet in force: ready to make active, or what
        # is in the way (B5). Carrier side only; full record only.
        "activation": (_renewal_activation(s, c, docs, missing, signed_copy_due,
                                           p, successors)
                       if with_docs else None),
        # Change period (Phase B): which dates this carrier may correct now,
        # and the corrections made (newest first, never the internal note).
        # Carrier side, full record, only while the switch is on.
        "period_change": _period_change_info(s, c, p) if with_docs else None,
        "submitted_at": _iso_utc(c.submitted_at),
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
        # Which of the two roads this contract is on — see _is_uploaded. The
        # screen used to decide this for itself from the two fields below, and
        # the two answers had already drifted: the server offers an accept
        # button on exactly the contracts this says are uploaded.
        "is_uploaded": _is_uploaded(c),
        # A renewal of an uploaded contract with no signed copy attached yet:
        # the screen asks for that copy instead of offering review or signing.
        "awaiting_signed_copy": signed_copy_due,
        # Has this contract any clauses yet? "Reflect new contract rules in BDX
        # setup" is only offered once it has — before that there is nothing to
        # turn into rules, whatever else the page says.
        "has_clauses": (_has_clauses(s, c.id) if with_docs else None),
        # A renewal's programme has a live Bordereau Setup to reflect its rules
        # in (the one auto-run uses). Without one the screen points at Setup.
        "has_live_setup": (_has_live_setup(s, c)
                           if with_docs and c.renews_contract_id else None),
        # A renewal whose signed PDF is attached but has not been read yet: no
        # clauses, so nothing to check files against until it is.
        "awaiting_read": (with_docs and bool(c.renews_contract_id)
                          and _effective_lifecycle(c) not in ("terminated", "superseded")
                          and any(d.kind == "contract" for d in active_docs)
                          and not _has_clauses(s, c.id)),
        # Each section BOTH ways: `body` keeps the tokens the editor turns into
        # chips, `rendered` is the same sentence with today's values in it. The
        # screen reads one and edits the other, and neither has to know how a
        # percentage or a money amount is written.
        "wording_sections": wording_sections,
        # Terms this contract is CHECKED on that its wording does not state.
        # Nearly always a clause that was edited with the figure typed in place
        # of the chip — after which the document keeps saying the old number
        # while the check follows the term. See contract_wording.unquoted_terms.
        "wording_unquoted": unquoted,
        # What was deleted from this wording on purpose. Served because the
        # editor re-reads through the preview, which writes a clause for any
        # agreed term the document does not state — without this, a clause
        # deleted last week would be back the next time somebody opened it.
        "wording_dropped": {
            "sections": list((c.wording_sections or {}).get("dropped_sections") or []),
            "terms": list((c.wording_sections or {}).get("dropped_terms") or []),
        },
        "signers": (c.wording_sections or {}).get("signers"),
        # The block this contract's signature page will ask for. Normalised on
        # the way out, so a screen reading it never has to know that older rows
        # hold {} — it gets the same shape whatever is stored, and the shape it
        # gets is the one the document will actually be drawn from.
        "signature_layout": esign_pdf.normalise_signature_layout(
            (c.wording_sections or {}).get("signature_layout")),
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
                                    has_wording, signed_copy_due,
                                    renewable=renewal["allowed"]),
        # Whose move it is — "carrier", "broker", or null when nobody is
        # waiting. Both sides read this, so neither has to infer it from a
        # state name written for the other one.
        "whose_turn": _whose_turn(c, _unsigned_sides(sigs)),
        # The last thing the broker asked to change, if the contract is sitting
        # with the carrier because of it. Put on the record itself so the
        # carrier's screen can show the request beside the terms without a
        # second call.
        "open_change_request": _open_change_request(s, c),
        # WHAT IS ACTUALLY CHECKED ON A FILE, which is not the same question as
        # what the contract says. A term becomes a check only once the contract
        # is bound to an output template, because a check is a comparison
        # against a bordereau COLUMN and until a template is chosen there are no
        # columns. So a contract can carry ten agreed limits and nought checks,
        # and that gap is invisible unless it is reported — which is how a
        # carrier ends up believing a file is being measured against terms
        # nothing has ever looked at. See _checks_summary.
        "checks": _checks_summary(s, c, rule_counts),
        # A reinsurance treaty's type, headline, placement and programmes
        # (treaty_routes.record_summary). NULL on an insurer ↔ broker contract.
        "treaty": _treaty_summary(s, c),
    }


def _treaty_summary(s, c: Contract) -> Optional[dict]:
    if (c.contract_type or "") != "insurer_reinsurer":
        return None
    import treaty_routes
    return treaty_routes.record_summary(s, c)


def _rule_tallies(s, contract_ids: list[int]) -> dict[int, dict]:
    """Each contract's rules, counted by where they came from — one query for
    any number of contracts, so the list costs what one record does.

    Disabled rules are left out: nothing runs them. A rule written from a typed
    term carries `rule_spec.source = 'contract_terms'` and the term's key in
    `rule_spec.limit` (contract_rules.write_rules). A Bordereau Setup rule
    carries neither — it quotes a clause through `source_clause_id`, or, as a
    standard check (derived, or from the rule library), quotes nothing.
    """
    if not contract_ids:
        return {}
    rows = s.execute(sa_text(
        "SELECT contract_id, COUNT(*) AS total, "
        "COUNT(*) FILTER (WHERE rule_spec->>'source' = 'contract_terms') "
        "  AS from_terms, "
        "COUNT(*) FILTER (WHERE rule_spec->>'source' IS DISTINCT FROM "
        "  'contract_terms' AND source_clause_id IS NOT NULL) AS from_clauses, "
        "ARRAY_AGG(DISTINCT rule_spec->>'limit') FILTER (WHERE "
        "  rule_spec->>'source' = 'contract_terms') AS term_keys "
        "FROM validation_rule WHERE contract_id = ANY(:ids) "
        "AND rule_status IS DISTINCT FROM 'disabled' "
        "GROUP BY contract_id"), {"ids": list(contract_ids)}).mappings().all()
    return {int(r["contract_id"]): {
                "total": int(r["total"]),
                "from_terms": int(r["from_terms"]),
                "from_clauses": int(r["from_clauses"]),
                "term_keys": {k for k in (r["term_keys"] or []) if k}}
            for r in rows}


def _checks_summary(s, c: Contract,
                    rule_counts: dict[int, dict] | None = None) -> dict:
    """How many of this contract's terms are actually checked, and how many could be.

    `checkable` counts the agreed limits that CAN become a check — a limit whose
    `check` is None is wording only, by design, and counting it would make every
    contract look half-bound forever. `rules` is what runs right now, split by
    where it came from.

    `terms_checked` is its own number, never `rules` read against `checkable`:
    the two count different things, and a Bordereau Setup's rules carry no term
    key, so crediting them to terms would be a guess. Reading one as the other
    is how a contract showed "102 of 9 · every checkable term is measured" with
    not one term checked.
    """
    limits = c.commercial_terms or {}
    checkable_keys = {
        k for k, entry in limits.items()
        if (ct.AGREED_LIMITS.get(k) or {}).get("check")
        and (entry or {}).get("value") not in (None, "")}
    checkable = len(checkable_keys)
    tally = ((_rule_tallies(s, [c.id]) if rule_counts is None else rule_counts)
             .get(c.id) or {})
    total = tally.get("total", 0)
    from_terms = tally.get("from_terms", 0)
    from_clauses = tally.get("from_clauses", 0)
    out = {
        "rules": total,
        "checkable": checkable,
        "terms_checked": len(checkable_keys & tally.get("term_keys", set())),
        "from_terms": from_terms,
        "from_clauses": from_clauses,
        "standard": total - from_terms - from_clauses,
        "output_template_id": c.output_template_id,
        # The one thing the screen cannot work out for itself: whether pressing
        # the button would do anything. Terms with nothing to check produce no
        # rules however many templates are bound.
        "bindable": checkable > 0,
        "output_template": None,
        "sheets": [],
    }
    # WHICH TEMPLATE, SAID OUT LOUD. Binding resolves the template by itself —
    # deliberately, so nobody picks one the runs do not use — and that made the
    # answer invisible: a carrier reporting on a Lloyd's US layout had never
    # been asked about the United States and had nowhere to see that they were.
    # The template is not the contract's to choose, but it IS the contract's to
    # show, so the wrong one is noticed here rather than in an exception report.
    #
    # Only on the single-record path: the list resolves nothing per row (see
    # `rule_counts`), and one template lookup per row is how a list turns into
    # a hundred round trips.
    if rule_counts is None and c.output_template_id:
        t = s.get(ExportTemplate, c.output_template_id)
        if t is not None:
            out["output_template"] = t.name
            out["sheets"] = [sh.get("sheet_name") for sh
                             in ((t.structure or {}).get("sheets") or [])
                             if sh.get("sheet_name")]
    return out


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


def _speaks_for_broker(p: Principal | None) -> bool:
    """May this seat commit its broker company to a contract?

    Agreeing terms, pushing back on them and signing are the broker ADMIN's.
    A broker user (operator) sends the files and sorts out their errors; the
    contract is the company's word, so the person who gives it is the one who
    runs the company's seat here.
    """
    return bool(p and p.is_broker and p.role == "broker_admin")


def _assert_speaks_for_broker(p: Principal, what: str) -> None:
    """Refuse a broker USER an act that commits the broker company."""
    if p.is_broker and not _speaks_for_broker(p):
        raise HTTPException(
            403, f"only your broker can {what} — broker users send the "
                 f"files, the broker agrees and signs the contract")


def _speaks_for_carrier(p: Principal | None) -> bool:
    """May this seat commit the CARRIER to a contract?

    The mirror of _speaks_for_broker on the other side of the table, and the
    same principle: a signature is the organisation's word, so the person who
    gives it is the one who runs the organisation's seat here. A carrier user
    raises a contract and takes it as far as being ready to sign; the carrier
    admin is who puts the company's name on it.

    Unlike the broker side this CANNOT be read off the token — everyone at a
    carrier holds the same `carrier_admin` role, and the two seats are told
    apart by the organisation's owner pointer. So it costs one query. Called
    once per contract screen, not in a loop.
    """
    if not p or p.is_broker:
        return False
    from carrier_scope import is_carrier_admin_seat
    with SessionLocal() as s:
        return is_carrier_admin_seat(s, p)


def _assert_speaks_for_carrier(p: Principal, what: str) -> None:
    """Refuse a carrier USER an act that commits the carrier."""
    if not p.is_broker and not _speaks_for_carrier(p):
        raise HTTPException(
            403, f"only your carrier can {what} — carrier users raise "
                 f"the contract, the carrier signs it")


def _is_uploaded(c: Contract) -> bool:
    """Was this contract's wording brought in as a document rather than written
    here?

    The two are governed differently and the difference is the whole of the
    uploaded path: an AUTHORED contract is a proposal — its terms are Kavachio's
    own structured fields, the broker reads them and agrees them, and both sides
    then sign inside the app. An UPLOADED one was executed on paper before it
    ever arrived; there is nothing left to negotiate and nobody left to sign, so
    what it needs is not a signature but somebody at the carrier saying "yes,
    this is ours" — which is the carrier admin's word, and is what
    /contracts/{id}/accept records.

    Read off the wording, not off a flag, because that is where the difference
    actually lives: an authored contract HAS wording_sections and an uploaded
    one does not. Agreed limits count as authorship too — a contract carrying
    commercial terms was built here even if its prose has not been written yet,
    which is the same test the record screen uses (ContractRecord.isUploaded),
    kept in step deliberately so the screen and the server cannot disagree about
    which of the two roads a contract is on.
    """
    if (c.wording_sections or {}).get("sections"):
        return False
    if isinstance(c.commercial_terms, dict) and c.commercial_terms:
        return False
    return bool(c.blob_ref or c.blob or c.filename)


def _awaiting_signed_copy(s, c: Contract) -> bool:
    """Is this the renewal of an UPLOADED contract, still waiting for its
    signed copy?

    Renew starts the successor with no documents, and a contract with no
    document reads as authored to _is_uploaded — so the renewal of a contract
    signed on paper was offered the review and in-app signing it never needs.
    Its road is the parent's: attach the signed renewal, then accept it. Once
    that copy is attached _is_uploaded says so itself and this stops applying;
    writing wording or terms here instead also ends it, as an authored choice.
    """
    if not c.renews_contract_id or _is_uploaded(c):
        return False
    if (c.wording_sections or {}).get("sections"):
        return False
    if isinstance(c.commercial_terms, dict) and c.commercial_terms:
        return False
    prior = s.get(Contract, c.renews_contract_id)
    return bool(prior and _is_uploaded(prior))


def _carrier_admin_turn(c: Contract, unsigned: list[str] | None = None) -> bool:
    """Is this contract waiting on the CARRIER ADMIN, specifically?

    _whose_turn answers "carrier or broker", which is the right question for the
    other side of the table and the wrong one inside the carrier: a contract the
    broker has just agreed is the carrier's move, but it is not the move of the
    carrier USER who raised it — the signature is the admin's alone
    (_assert_speaks_for_carrier). Counting those into the author's queue told
    them to do something the API would refuse, and leaving them out of the
    admin's told nobody at all.

    So this is the one function that says whose desk, and the dashboard tile,
    the record banner and the notifications all read it. Three states qualify:

      pending            an uploaded contract sent up for acceptance
      agreed / signed    the terms are settled and the carrier has not signed
                         — the moment the whole gate exists for

    Everything else on the carrier's side (a draft still being written, a change
    request to answer) is ordinary work its author can do, and is deliberately
    NOT here.
    """
    state = _effective_lifecycle(c)
    if state == "pending":
        return True
    if state in ("agreed", "signed"):
        return "carrier" in (unsigned if unsigned is not None
                             else ["carrier", "counterparty"])
    return False


def _allowed_actions(c: Contract, missing: list[str],
                     p: Principal | None = None,
                     unsigned: list[str] | None = None,
                     has_wording: bool = True,
                     signed_copy_due: bool = False,
                     renewable: bool | None = None) -> dict[str, bool]:
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
    # The broker side's answers and signature are its admin's alone.
    broker_voice = _speaks_for_broker(p)
    # …and the carrier's signature is its carrier admin's alone. Worked out
    # here rather than on the screen for the reason at the top of this
    # function: a carrier user shown a Sign button would be shown one the API
    # refuses, and they would have no way to tell which of the two was wrong.
    carrier_voice = _speaks_for_carrier(p)
    unsigned = ["carrier", "counterparty"] if unsigned is None else unsigned
    my_side = "counterparty" if is_broker else "carrier"

    return {
        # The carrier revises while the ball is with it — including after the
        # broker has asked for changes, which is the whole point of asking.
        "edit": (state in ("draft", "pending")
                 or (state == "changes_requested" and is_carrier)),

        # ── the negotiation ──
        # Carrier sends its terms out. Only for a contract it owns and only to a
        # BROKER: a reinsurer has no seat in this app, so there is nobody on the
        # other side to review it.
        #
        # NOT once the terms are agreed. `agreed` means the broker has read
        # these terms and said yes; there is nothing left to review, and the
        # next thing that happens to the contract is a signature. Offering it
        # there asked the broker to agree twice to the same thing — and since
        # `edit` stops at `agreed` too, the terms could not even have moved in
        # between. If they need to move, the broker asks for changes and the
        # contract comes back to `changes_requested`, which IS on this list.
        # NOT from `pending`. That state means one thing now — an uploaded
        # contract waiting to be accepted — and asking the broker to agree to a
        # document they have already signed is not a question. The admin's two
        # answers there are accept and send back; if one genuinely does need
        # the broker to look, sending it back puts it in `draft`, where this
        # button is offered exactly as it always was.
        # Not on the renewal of an uploaded contract — see _awaiting_signed_copy.
        # Never for an UPLOADED contract: it was signed on paper, so there are
        # no terms left to agree — the carrier accepts it (submit_for_approval)
        # and the broker is told by email when it goes in force.
        "send_for_review": (
            is_carrier and not blocked and not signed_copy_due
            and not _is_uploaded(c)
            and (state in ("draft", "changes_requested")
                 # An AUTHORED contract should never be in `pending` — one
                 # written here goes straight to the broker. But a row left
                 # there by an earlier version of this flow would otherwise
                 # have no way out at all, so the admin can still send it on.
                 or (state == "pending" and not _is_uploaded(c)
                     and carrier_voice))
            and c.contract_type == "insurer_broker"
            and c.broker_party_id is not None),
        # Skip the review and go straight to signing. The carrier is not
        # OBLIGED to hold a review — a renewal on last year's wording, or a
        # treaty whose counterparty has no seat here to read it, has nothing to
        # negotiate — and without this the only road to a signature ran through
        # a broker who had nothing to say. Offered alongside send_for_review and
        # never instead of it, so the default road stays the one that asks.
        #
        # Draft only. Once the terms have been out, skipping the answer to them
        # is not "no review was needed", it is ignoring one that was asked for.
        # DRAFT ONLY, because that is all the route accepts — a `pending`
        # contract is an uploaded one waiting to be accepted, and "skip the
        # review" is not the answer to it. Offering it there was a button that
        # 409'd with "a pending contract's terms have already been out".
        "skip_review": (is_carrier and not blocked and state == "draft"
                        and not signed_copy_due),
        # An uploaded contract's way up to the carrier admin. It has no terms to
        # negotiate and no broker to ask, so it does not go out for review — it
        # goes to the one person who can accept it on the carrier's behalf.
        "submit_for_approval": (is_carrier and not blocked
                                and state == "draft" and _is_uploaded(c)),
        # …and the admin's answer to it. Review and accept, in one act: there is
        # no signature to give on a contract that was executed before it got
        # here, and pretending otherwise would put a name in Kavachio under a
        # document signed somewhere else.
        # Not conditioned on _is_uploaded, although submit_for_approval is.
        # `pending` means "waiting on the carrier admin's word" and their word
        # has to be givable: tying the button to a test computed from the
        # wording meant that saving one agreed limit on a contract already
        # sitting there un-classified it, the button vanished, and the contract
        # was stranded in a state with no exit but send-back.
        "accept_contract": (is_carrier and carrier_voice
                            and state == "pending"),
        # Send a colleague's contract back to them. The carrier admin's answer
        # when it is not right: the work stays, the author is told why, and it
        # goes back to `draft` where they can change it and send it up again.
        "send_back": is_carrier and carrier_voice and state == "pending",
        # Said plainly so the screen does not have to infer it from a state
        # name written for somebody else: this contract is on the carrier
        # admin's desk and on nobody else's.
        "awaiting_carrier_admin": _carrier_admin_turn(c, unsigned),
        # Whether the person reading this IS that admin. The screen needs both:
        # one says the contract is waiting on the carrier admin, the other says
        # whether to show the buttons or the sentence explaining the wait.
        "carrier_admin_seat": carrier_voice,
        # Broker's two answers. Never both sides' — this is their turn.
        "request_changes": broker_voice and state in ("in_review", "agreed"),
        "accept_terms": broker_voice and state == "in_review" and not blocked,
        # Sign and return it. The broker's last act — after this the contract is
        # the carrier's to place and put in force. Not until the carrier has
        # signed, though: the broker signs a document that already carries the
        # other signature, which is the same order the signing round keeps.
        "submit_signed": (broker_voice and state == "agreed"
                          and "carrier" not in unsigned),

        # Signing is what puts a contract in force, so this is only ever the
        # tidy-up path for a contract both sides signed while something else
        # was in the way. It never stands in for a signature.
        "activate": (state in ("signed",) and not unsigned and not blocked
                     and is_carrier),

        # ── signing ──
        # Your own side, once the terms have stopped moving and there is a
        # wording to sign. A broker signs for the counterparty; the carrier
        # signs for itself.
        # `has_wording` matters: there is nothing to sign without one, and the
        # API says so. A button that 400s is worse than no button.
        #
        # And the carrier goes first — see _whose_turn. A broker offered a sign
        # button before the carrier has signed would be signing a document the
        # carrier has not, and the signing round would refuse it as out of turn
        # anyway.
        # NOT `pending`. That state now means one thing only — an uploaded
        # contract waiting to be accepted — and sign_contract refuses it
        # outright ("these terms are still being settled"). Offering the button
        # there was the reported "I can see it is waiting on me and I cannot
        # sign it": the screen said yes and the API said no, and nothing on
        # either told you which was wrong. An uploaded contract is accepted
        # (accept_contract), never signed.
        "sign": (state in ("draft", "agreed", "signed")
                 and my_side in unsigned
                 and (not is_broker or "carrier" not in unsigned)
                 and (not is_broker or broker_voice)
                 and (is_broker or carrier_voice)
                 and has_wording and bool(p)),
        # The carrier entering a signature made on paper or through a provider.
        # The only way a reinsurance contract is ever signed on both sides,
        # since a reinsurer has no seat here. Still a signing act, and still
        # the carrier's word, so it is the carrier admin's too.
        "record_signature": (is_carrier and carrier_voice
                             and state in ("draft", "agreed", "signed")
                             and "counterparty" in unsigned and has_wording),
        "terminate": state in ("active", "expired") and is_carrier,
        # renewal_gate: expired or terminated, or active within the renewal
        # window, and not already renewed. Never superseded. `renewable` is
        # that answer worked out by _record; without it, the state part alone.
        "renew": is_carrier and (renewable if renewable is not None
                                 else state in ("active", "expired", "terminated")),
        # The carrier's documents. A broker reviews and signs; it does not
        # attach or retire the carrier's wording or references.
        "upload_documents": state not in ("terminated", "superseded") and is_carrier,
        "generate_rules": (state not in ("terminated", "superseded")
                           and not blocked),
    }


def _whose_turn(c: Contract, unsigned: list[str] | None = None) -> Optional[str]:
    """Who the contract is waiting on — "carrier", "broker", or nobody.

    A negotiation that does not say whose move it is becomes two people each
    assuming the other is looking at it.

    ONCE THE TERMS ARE SETTLED THE STATE ALONE CANNOT ANSWER THIS. `agreed`
    means "waiting on a signature", and after one side gives theirs it is
    still `agreed` — but the move is now the other's. Reading the state alone
    would leave the contract sitting in somebody's queue on the strength of a
    signature they had already given, which is precisely the thing this field
    exists to prevent. So once signing has started, the turn belongs to
    whoever has not signed.

    THE CARRIER SIGNS FIRST. It wrote the terms and it holds the document, so
    the round starts on its side: the carrier signs, and only then is the
    broker asked. That order is not a preference — the broker is asked to sign
    a document that already carries the carrier's signature, and asking both
    at once is how two people sign two different versions of the same
    contract. It is why the carrier is named before the broker here, and the
    signing round enforces the same order for itself (esign_routes._turn_check).
    """
    state = _effective_lifecycle(c)
    if unsigned is not None and state in ("agreed", "signed"):
        if "carrier" in unsigned:
            return "carrier"
        if "counterparty" in unsigned:
            return "broker"
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
            # How long a term may be said to run for, and how a term is counted.
            # The form turns a chosen length into an expiry DATE — nothing about
            # a duration is stored, see contract_types.TERM_DURATION_MONTHS.
            "term": ct.term_spec(),
            # What a signature block may contain, and how the two may sit on
            # the page. Served for the same reason as everything else here: the
            # form offers exactly what the wording builder can draw and the
            # validator will accept, because all three read this one list.
            "signature_block": esign_pdf.signature_block_spec(),
            # How hard a check bites, with the words for it. Objects rather
            # than bare keys so the form, the summary badge and the sample
            # contract all name a severity the same way — see SEVERITY_VOCAB.
            "severities": [dict(sv) for sv in ct.SEVERITY_VOCAB]}


@router.get("/counterparties")
def counterparties(party_type: str = Query(..., description="broker | reinsurer"),
                   program_id: Optional[int] = Query(None),
                   p: Principal = Depends(current_principal)):
    """Who this carrier may write a contract WITH, for the type being raised.

    Carrier-side only. A broker seat carries no tenant, so every scope below
    would fall away and it could list the brokers on ANY carrier's programme;
    it never raises a contract, so it is simply refused.

    For a broker the list is narrowed to the programme, because a broker that is
    not on the programme cannot hold a contract on it — offering them and
    refusing on save would be a worse way to say the same thing. A reinsurer has
    no such gate: it does not produce business into the programme.
    """
    if p.is_broker:
        raise HTTPException(403, "insufficient role")
    with SessionLocal() as s:
        tid = (p.tenant_id if not p.is_platform_admin
               else (s.get(Program, program_id).tenant_id if program_id else None))
        q = (s.query(Party)
             .filter(func.cast(Party.party_type, String) == party_type,
                     Party.is_active.is_(True)))

        if party_type == "broker" and program_id:
            # BEING ON THE PROGRAMME IS THE AUTHORISATION — not owning the
            # party row. A broker shared with another carrier keeps the row of
            # whoever onboarded them, so filtering by `Party.tenant_id` as well
            # hid exactly the brokers a carrier had just put on a programme:
            # they were on it, and still could not be picked.
            #
            # The programme is checked to be THIS carrier's first, which is what
            # the ownership filter was incidentally providing.
            prog = s.get(Program, program_id)
            if not prog or (tid is not None and prog.tenant_id != tid):
                raise HTTPException(404, "programme not found")
            # A carrier USER's link waits for the carrier admin, but the
            # contract they are raising is the next step in building it — so
            # the carrier picks from its pending links too. link_live() makes
            # that the carrier's answer only; a broker still sees live ones.
            from carrier_scope import link_live
            on_programme = {
                l.broker_party_id for l in s.query(ProgramBroker).filter(
                    ProgramBroker.program_id == program_id,
                    link_live(p)).all()
            }
            q = q.filter(Party.id.in_(on_programme or {-1}))
        elif tid is not None:
            # No programme to gate on (a reinsurer, or a broker list asked for
            # without one), so the carrier's own directory is the scope.
            # Global parties (scope='global') are shared; tenant ones are not.
            q = q.filter(or_(Party.tenant_id == tid, Party.tenant_id.is_(None)))

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
    treaty_type: Optional[str] = Query(
        None, description="quota_share | surplus | xol | stop_loss | facultative"),
    lifecycle: Optional[str] = Query(None),
    waiting: Optional[str] = Query(
        None, description='"mine" — only contracts it is YOUR side\'s move on'),
    q: Optional[str] = Query(None, description="match on name, UMR or filename"),
    page: Optional[int] = Query(None, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=200),
    p: Principal = Depends(current_principal),
):
    """Every contract the caller can see, across all their programmes.

    The carrier-wide view the app never had: contracts could only be reached one
    broker or one programme at a time, which is the wrong shape for "what is
    expiring", "what is waiting on me" and "where is that contract".

    Pagination is OPT-IN. Without `page` this returns the plain list exactly as
    it always has — ContractNew's endorsement picker reads the whole thing and
    must keep working. With `page` it returns
    {"items", "total", "page", "page_size"} for the Contracts screen.

    `waiting="mine"` answers the question the screen is most often opened with,
    and which the carrier had no way to ask: what is on MY side's desk. The
    broker's own list has had it since it was written (`status=mine`); the
    carrier could only filter by lifecycle, and no lifecycle answers it — a
    contract sitting in `agreed` is the carrier's move or the broker's
    depending on who has already signed, which is exactly what _whose_turn
    exists to work out. It is the same function the record and both lists use,
    so a row counted here always matches the one the screen shows.
    """
    with SessionLocal() as s:
        query = s.query(Contract)
        if p.is_broker:
            # The broker's party from the DATABASE, never the token claim: the
            # claim is None in real requests, and `== None` renders as IS NULL
            # — every carrier-held contract on the platform instead of this
            # broker's own (see auth_deps.resolve_broker_party_id).
            bid = resolve_broker_party_id(s, p)
            query = query.filter(Contract.broker_party_id == bid if bid else false())
        else:
            tid = resolve_tenant_id(s, p)
            query = query.filter(Contract.tenant_id == tid)

        if program_id:
            query = query.filter(Contract.program_id == program_id)
        if counterparty_id:
            query = query.filter(Contract.broker_party_id == counterparty_id)
        if contract_type:
            query = query.filter(Contract.contract_type == contract_type)
        if treaty_type:
            from db import Treaty
            query = query.join(Treaty, Treaty.contract_id == Contract.id) \
                .filter(Treaty.treaty_type == treaty_type)
        if q:
            like = f"%{q.strip()}%"
            query = query.filter(or_(Contract.name.ilike(like),
                                     Contract.umr.ilike(like),
                                     Contract.filename.ilike(like)))

        size = page_size or 10

        # THE FAST PATH, and the only one that avoids building every record:
        # with no lifecycle filter the page can be cut in SQL, so a carrier with
        # ten thousand contracts pays for ten of them.
        #
        # Lifecycle cannot take this path. `expired` is DERIVED from the expiry
        # date rather than stored (see _effective_lifecycle), so it is knowable
        # only after _record has run — slicing first would hand back a page
        # holding however many of its ten rows happened to survive the filter,
        # and a total counting rows the filter would have dropped.
        # `waiting` joins `lifecycle` on the slow path for the same reason:
        # whose move it is is DERIVED per row (from the lifecycle and from who
        # has signed), so it is knowable only once _record has run. Slicing
        # first would page over rows the filter then dropped.
        if page is not None and not lifecycle and not waiting:
            total = query.order_by(None).count()
            rows = (query.order_by(Contract.id.desc())
                    .offset((page - 1) * size).limit(size).all())
            counts = _rule_tallies(s, [c.id for c in rows])
            succ = _successors(s, [c.id for c in rows])
            return {"items": [_record(s, c, with_docs=False, p=p, rule_counts=counts,
                                      successors=succ)
                              for c in rows],
                    "total": int(total), "page": page, "page_size": size}

        rows = query.order_by(Contract.id.desc()).all()

        # One query for every row's check count. Counting inside _record would
        # be one round trip per contract, which on a carrier with a few hundred
        # of them is the whole cost of the screen.
        counts = _rule_tallies(s, [c.id for c in rows])
        succ = _successors(s, [c.id for c in rows])
        out = [_record(s, c, with_docs=False, p=p, rule_counts=counts,
                       successors=succ)
               for c in rows]
        if lifecycle:
            out = [r for r in out if r["lifecycle"] == lifecycle]
        if waiting == "mine":
            my_side = "broker" if p.is_broker else "carrier"
            out = [r for r in out if r.get("whose_turn") == my_side]
            # "Mine" means MINE, and inside a carrier that is two queues rather
            # than one: the signature is the carrier admin's alone, so a
            # contract the broker has just agreed is on their desk and not on
            # the desk of the user who raised it. Filtered on the same flag the
            # record screen and the dashboard tile read, so the count on the
            # tile and the length of this list cannot disagree.
            if not p.is_broker and not _speaks_for_carrier(p):
                out = [r for r in out
                       if not (r.get("actions") or {}).get(
                           "awaiting_carrier_admin")]
        if page is not None:
            start = (page - 1) * size
            return {"items": out[start:start + size], "total": len(out),
                    "page": page, "page_size": size}
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
    # column of their own: they belong to this authored document. Naming them
    # sends them NOTHING — no route in this file emails anybody. The address is
    # kept for the signing round (esign_routes), which is started separately
    # from the contract's own signature page and is the only thing in the flow
    # that puts mail in an inbox.
    signers: Optional[list] = None
    # What the carrier deleted while writing it. A term with no clause is given
    # one on the way in (contract_wording.missing_clauses), so a clause removed
    # on purpose has to be named or it would arrive back in the saved contract.
    dropped_sections: Optional[list] = None
    dropped_terms: Optional[list] = None
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
    # A reinsurance treaty's structure (Phase 26.1) — its type, layers and
    # panel, in treaty_types' shape — and the programmes it protects besides
    # the one it is raised on (26.2). Ignored on an insurer ↔ broker contract.
    treaty: Optional[dict] = None
    program_ids: Optional[list[int]] = None


def _bad_fields(e: ct.ContractTypeError):
    return HTTPException(400, e.to_detail())


def _clean_signature_layout(raw: Any) -> dict:
    """The chosen block, checked and put in order — or a 400 saying which side
    is wrong. Refused on the way IN, while the carrier is still looking at the
    choice they made, rather than discovered when a document comes out with
    nowhere to sign."""
    try:
        return esign_pdf.normalise_signature_layout(raw, strict=True)
    except esign_pdf.SignatureLayoutError as e:
        raise HTTPException(400, {"message": e.message, "errors": e.errors})


def _as_date(v: Any) -> Optional[dt.date]:
    if v in (None, ""):
        return None
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def _carrier_works_with(s, party_id: int, party_tenant_id: Optional[int],
                        tenant_id: int) -> bool:
    """Is this organisation one of this carrier's? Its own (or a shared,
    tenant-less) party — or a broker it has an active relationship with.

    A broker works with several carriers (carrier_broker, 21–27 Sep); its party
    row stays under the carrier that first brought it in. Testing the row's
    tenant alone refused every later carrier's contract with that broker as
    "contract not found" (7758 and its predecessor 3813, 16 Oct 2026). One
    definition of "works with": db.carrier_broker_ids."""
    if party_tenant_id in (None, tenant_id):
        return True
    from db import carrier_broker_ids
    return party_id in carrier_broker_ids(s, tenant_id)


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
    if not _carrier_works_with(s, party_id, party.tenant_id, tenant_id):
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
        # A link still WAITING for the carrier admin is not "taken off" — it is
        # the step the carrier is in the middle of, and this contract is the
        # next one. link_is_live() lets the carrier through and still refuses a
        # broker, and an actually-removed link ('inactive') is refused for both.
        from carrier_scope import link_is_live
        if not link_is_live(link, p):
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
    # Raising one is the same act as holding one: a contract Kavachio could
    # create is a contract it could not then open (_contract_access), which is
    # not a state worth having.
    assert_can_open_contract(p)

    # Set when the caller sent a wording; stored as clauses after the commit.
    authored_sections: list[dict] | None = None
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

        # A treaty's counterparty IS the lead on its panel. Taken from there
        # when one is starred, so the contract and its panel cannot name two
        # different reinsurers as the one the treaty is with.
        if body.contract_type == "insurer_reinsurer" and body.treaty:
            import treaty_types as _tt
            lead = _tt.lead_of(body.treaty)
            if lead is not None:
                payload["counterparty_party_id"] = lead

        try:
            clean = ct.validate(body.contract_type, payload)
        except ct.ContractTypeError as e:
            raise _bad_fields(e)

        counterparty_id = clean.get("counterparty_party_id")
        _check_counterparty(s, p, spec, body.program_id, counterparty_id, tenant_id)

        # The treaty is checked BEFORE the contract row exists, so a panel that
        # adds up to 120% refuses the create rather than leaving a treaty with
        # no structure behind it.
        import treaty_routes as tr
        is_treaty = body.contract_type == tr.REINSURER
        treaty_clean = (tr.validate_for_create(s, tenant_id, body.treaty)
                        if is_treaty and body.treaty else None)

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
        # `umr`, `risk_code` and `section_number` are deliberately absent: the
        # form does not ask for them, nothing on the record shows them, and
        # `ContractIn` does not accept them either. See contract_types. A
        # renewal still carries them forward from the contract it succeeds,
        # which is where an uploaded one gets them.
        for field in ("name", "class_of_business", "year_of_account",
                      "earnings_pattern", "premium_cap_currency"):
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
            import contract_wording as cw
            # Same tie as on edit: a figure typed where a chip used to be is
            # bound back to the term it quotes, or the contract stops following
            # its own terms from the moment it is created.
            ctx = _wording_context(s, c)
            kept, _retied = cw.retie(
                [sec for sec in body.wording_sections
                 if isinstance(sec, dict) and (sec.get("body") or "").strip()],
                ctx["tokens"])
            # The last chance to catch a term with no sentence. The wizard tops
            # the wording up as it goes, but nothing stops a client sending
            # sections written before the terms were finished, and a contract
            # that does not state a term it is measured on is the failure this
            # whole file exists to prevent.
            kept, _added = cw.missing_clauses(
                kept, values=ctx["values"], limits=ctx["limits"],
                type_label=ctx["type_label"],
                dropped_sections=body.dropped_sections,
                dropped_terms=body.dropped_terms)
            if treaty_clean:
                # The clauses the preview wrote from the structure, rewritten
                # from the structure actually being saved — the two cannot
                # differ, whatever the client sent.
                import treaty_types as _tt
                kept = _tt.merge_sections(kept, _tt.build_sections(treaty_clean))
            authored_sections = kept
            c.wording_sections = {
                "sections": kept,
                "signature_layout": _clean_signature_layout(body.signature_layout),
                "signers": [sg for sg in (body.signers or [])
                            if isinstance(sg, dict) and (sg.get("name") or "").strip()],
            }

        # A BROKER NO LONGER RAISES CONTRACTS. The carrier owns the book, and
        # this used to be the other half of that: a broker could bring one, and
        # it sat behind an approval gate until the carrier let it in. Both went
        # together — with nobody bringing a contract there is nothing to
        # approve, and with no gate there is nothing to hold a broker's upload.
        # Refused here rather than only in the UI, because the endpoint is the
        # thing that decides.
        if p.is_broker:
            raise HTTPException(
                403, "a contract is raised by the carrier — a broker is a party "
                     "to one, not the author of it")
        # Two ways to finish, and neither is "live". A contract goes in
        # force because both sides SIGNED it — see sign_contract — so
        # creating one already active would be asserting a signing that
        # never happened, in the one place nobody could later point at.
        mode = body.create_as or ("review" if body.send_for_review else "draft")
        if mode == "live":
            raise HTTPException(400, {
                "message": "a contract cannot be created active. Both "
                           "sides sign it, and the second signature is "
                           "what makes it active.",
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
            # STRAIGHT TO THE BROKER, whoever raised it.
            #
            # There was a version of this that stopped here first, at
            # `pending`, so the carrier admin could read the terms before the
            # broker did. It was removed on purpose: it put the admin in the
            # conversation twice — once before the broker had said anything,
            # and again to sign — and the first of those two readings is the
            # one with the least information in it. Nobody has raised an
            # objection yet, so there is nothing to weigh.
            #
            # The admin's gate is now the SECOND one, where it earns its keep:
            # the broker agrees the terms (accept_terms), and the contract
            # stops on the admin's desk for review and signature before it can
            # go anywhere. One reading, of a settled document, by the person
            # whose signature it needs. See _carrier_admin_turn.
            c.lifecycle = "in_review"
            # Kept even though nothing gates on it any more: it is who to tell
            # when the broker answers, and who the contract goes back to if the
            # admin sends it back.
            c.submitted_by_user_id = p.user_id
            c.submitted_at = dt.datetime.now(dt.timezone.utc)
        elif mode == "draft":
            # Written down and nothing more: not live, nothing checked, and
            # the terms still editable. What makes it not-live is the
            # lifecycle, and nothing else.
            c.lifecycle = "draft"
        c.lifecycle_effective_date = c.inception_dt or _today()

        s.add(c)
        s.flush()
        if is_treaty:
            if treaty_clean:
                tr.save_structure(s, c, treaty_clean, p.user_id)
            tr.set_programmes(s, c, body.program_ids)
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
        #
        # CLAUSES YES, RULES NO. The wording that was just written is stored as
        # this contract's clauses, because that is what it says and every screen
        # downstream asks for it that way. No rule is written: a rule is a
        # comparison against a bordereau template's COLUMNS, and no template is
        # chosen at authoring time. Binding the terms to columns happens once,
        # during BDX setup, which is the only moment the columns are known.
        # After the commit above, so the contract has an id to hang them on —
        # and committed in its own right, or the rows would be discarded when
        # the session closes.
        if authored_sections:
            _store_authored_clauses(s, c, authored_sections)
            s.commit()
        _notify_contract_raised(s, c, p)
        return _record(s, c, p=p)



def _notify_contract_raised(s, c: Contract, p: Principal) -> None:
    """Tell the carrier admin that a colleague has raised a contract.

    ONLY a carrier USER's. The signature at the end is the carrier admin's
    alone (_assert_speaks_for_carrier), so a contract somebody else raises is
    work that will land on them — and one THEY raised is not, because they were
    the person doing it. Telling people about their own actions is how a
    notification turns into something they filter.

    NOT the same thing as sending a contract for review. That puts it in the
    broker's queue, emails nobody by design, and is the broker's turn; this is
    about whose desk it comes back to. The mail says which of the two has just
    happened rather than implying a signature is due today.

    Best-effort, like every notification: raising the contract has already been
    committed, and must not fail because mail did not go out.
    """
    try:
        if p.is_broker or _speaks_for_carrier(p):
            return
        from notifications import (CARRIER_ADMIN_FOOTER,
                                   carrier_admin_recipients, notify_people)
        state = _effective_lifecycle(c)
        if state == "draft":
            # Saved and nothing more. It has not been sent anywhere and nobody
            # is waiting on it — the old copy said "It is waiting for your
            # signature", which was simply untrue and taught the carrier admin
            # that these mails do not mean anything.
            return
        waiting_on_admin = state == "pending"
        with_broker = state in ("in_review", "changes_requested")
        who = _actor_name(s, p)
        name = c.name or c.filename or f"Contract {c.id}"
        prog = s.get(Program, c.program_id) if c.program_id else None
        party = s.get(Party, c.broker_party_id) if c.broker_party_id else None
        notify_people(
            carrier_admin_recipients(c.tenant_id),
            (f"{who} has raised a contract for you to review"
             if waiting_on_admin else
             f"{who} has raised a contract you will need to sign"),
            body=("Nothing has gone to the broker. Read the terms and either "
                  "send them out, settle them without asking, or send it back "
                  "for a change."
                  if waiting_on_admin else
                  "It is with the broker to read and agree first. It comes back "
                  "to you for signature once they have."
                  if with_broker else
                  "It is waiting for your signature — a contract becomes active "
                  "when both sides have signed it."),
            facts=[f for f in (
                ("Contract", name),
                ("With", getattr(party, "legal_name", None)),
                ("Programme", getattr(prog, "name", None)),
                ("Raised by", who),
                ("Now", "Waiting on you" if waiting_on_admin
                        else "With the broker" if with_broker
                        else "Waiting on you"),
            ) if f[1]],
            link_path=f"/contracts/{c.id}",
            link_label="Open the contract",
            action=("Review it" if waiting_on_admin
                    else None if with_broker else "Sign it"),
            subject=f"Contract to review: {name}" if waiting_on_admin
                    else f"Contract raised: {name}",
            footer=CARRIER_ADMIN_FOOTER)
    except Exception:  # noqa: BLE001
        pass


def _actor_name(s, p: Principal) -> str:
    """The acting person's display name, for something a PERSON reads. Falls
    back to the address, then to a neutral phrase — never to an empty name."""
    try:
        u = s.query(AppUser).filter(AppUser.id == p.user_id).first()
        return (u.full_name or u.email) if u else "A colleague"
    except Exception:  # noqa: BLE001
        return "A colleague"


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
    # What the carrier took OUT. A clause deleted on purpose must not come back
    # the next time the screen re-reads the wording — see
    # contract_wording.missing_clauses, which writes a sentence for any term
    # the document does not state and needs to be told which silences were
    # meant.
    dropped_sections: Optional[list] = None
    dropped_terms: Optional[list] = None
    # A treaty's structure, so its clauses are in the wording being previewed.
    treaty: Optional[dict] = None


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
    added: list[str] = []
    if not sections:
        sections = cw.build_sections(values=body.values, limits=limits,
                                     type_label=type_label)
    else:
        # There IS a wording, and it was written from the terms as they stood
        # at the time. A term agreed since then has no sentence in it, so it
        # gets one — and everything already written, edits included, is left
        # exactly as it is. Without this the contract goes out for signature
        # stating the terms it had on the day the wording was first opened.
        sections, added = cw.missing_clauses(
            sections, values=body.values, limits=limits, type_label=type_label,
            dropped_sections=body.dropped_sections,
            dropped_terms=body.dropped_terms)

    if body.treaty and (body.contract_type or "") == "insurer_reinsurer":
        # The treaty's clauses, from the structure as it stands on screen.
        # Lenient: a half-typed panel previews what it has.
        import treaty_types as tt
        treaty = tt.validate(body.treaty, strict=False)
        if treaty.get("treaty_type"):
            sections = tt.merge_sections(sections, tt.build_sections(treaty))

    tokens = cw.token_values(
        values=body.values, limits=limits,
        carrier_name=body.carrier_name,
        counterparty_name=body.counterparty_name,
        programme_name=body.programme_name)

    checks, warnings = cw.derive_checks(body.values, limits, sections)
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
        # Terms this call wrote a sentence for. Named rather than slipped in:
        # the wording is the carrier's, and text appearing in it is something
        # they are told about.
        "added": [{"key": k,
                   "question": ct.AGREED_LIMITS[k]["question"]}
                  for k in added if k in ct.AGREED_LIMITS],
    }


def _compose_from_body(body: WordingPreviewIn, *,
                       blank_signature_space: bool = False,
                       marks_out: list | None = None,
                       landings_out: dict | None = None) -> bytes:
    """The composed document for a contract that does not exist yet.

    "Nothing has been sent yet" has to stay true while somebody is still
    building one, so this writes no row anywhere: the same composer the saved
    contract uses, run on what the builder holds right now.

    Shared by the three things the create wizard can do with a document it has
    not saved — download it, count its pages, and draw one of them to drag a
    signature block onto. One composer, because a page somebody places a block
    on has to be the page that comes out of the printer; two would be two
    documents that agree until they do not.
    """
    import contract_wording as cw

    sections = _composable(body.sections)
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
        signers=body.signers, signature_layout=body.signature_layout,
        blank_signature_space=blank_signature_space, marks_out=marks_out,
        landings_out=landings_out)
    return data


@router.post("/contract-wording/draft")
def wording_draft(body: WordingPreviewIn,
                  _p: Principal = Depends(current_principal)):
    """Download a draft — the composed PDF, before anything is saved."""
    data = _compose_from_body(body)
    fname = f"{(body.values.get('name') or 'contract-draft').strip()} (draft).pdf"
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": _content_disposition(fname)})


# ── the pages of a contract that does not exist yet ─────────────────────────
#
# The saved record has /contracts/{id}/pages for this. The create wizard cannot
# use it, and used to have nothing: placing the blocks by hand was offered only
# after the contract was saved, so a carrier who wanted them somewhere
# particular had to create the contract, leave the flow, and go and move them.
# The document was composable from the builder's own state the whole time —
# that is what "Download the draft" does — so the only thing missing was
# serving it as PAGES rather than as a file.
#
# POST, not GET, for both: the body IS the contract. There is no id to put in a
# path because there is no row, and the terms are far too big for a query
# string.

@router.post("/contract-wording/pages")
def wording_pages(body: WordingPreviewIn,
                  _p: Principal = Depends(current_principal)):
    """How many pages the unsaved contract has, and how big each one is.

    The same answer /contracts/{id}/pages gives for a saved one, and for the
    same purpose: a block is stored as a page number and a point on that page,
    so the screen has to be showing the pages the PDF actually has.
    """
    marks: list = []
    landings: dict = {}
    data = _compose_from_body(body, blank_signature_space=True,
                              marks_out=marks, landings_out=landings)
    return {"marks": marks, "landings": landings,
            "pages": esign_pdf.page_count(data),
            "sizes": esign_pdf.page_sizes(data),
            # Where the wording is, so a block can be kept off it.
            "text": esign_pdf.text_regions(data),
            "clauses": _clause_list(_composable(body.sections))}


@router.post("/contract-wording/pages/{page_no}")
def wording_page(page_no: int, body: WordingPreviewIn,
                 scale: float = Query(2.0, ge=0.5, le=3.0),
                 _p: Principal = Depends(current_principal)):
    """One page of the unsaved contract, as a PNG.

    Composed fresh and never cached, exactly like the saved contract's pages:
    the wording is not a file, and a cached image is a picture of terms that
    may have moved since.
    """
    data = _compose_from_body(body, blank_signature_space=True)
    try:
        img = esign_pdf.render_page_png(data, page_no, scale)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return Response(content=img, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


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
        # A date edit is checked against the neighbouring versions further
        # down. Lock this row and theirs first — before anything on `c` is
        # changed, in the order a date correction takes them — and read them
        # again once held, so a correction committed meanwhile is seen.
        if isinstance(body, dict) and ({"inception_dt", "expiry_dt"} & set(body)):
            _period_neighbours(s, c, lock=True)
        state = _effective_lifecycle(c)
        # `changes_requested` is editable BY THE CARRIER and nobody else: the
        # broker asked for a change and this is the carrier answering it. Left
        # out, the negotiation would have a step 3 and no step 4.
        # `draft` and `pending` are both CARRIER-side states — a contract being
        # written, or one sitting on the carrier admin's desk — and a broker
        # has no business in either. Nothing puts a broker's contract there any
        # more (the path where a broker uploaded one was removed; see the note
        # at the top of this module), so this was a door left open onto a room
        # nobody uses: any broker seat, operators included, could rewrite a
        # carrier's terms while they were still being decided.
        editable = (not p.is_broker) and (
            state in ("draft", "pending", "changes_requested"))
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
                f"An active contract is changed by an endorsement, so that "
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

        dates_before = (c.inception_dt, c.expiry_dt)
        for name, value in clean.items():
            if name == "counterparty_party_id":
                continue
            attr = ct.FIELDS[name]["attr"]
            kind = ct.FIELDS[name]["kind"]
            setattr(c, attr, _as_date(value) if kind == "date" else value)

        # New dates next to the versions either side of this one (D1/D2/D8):
        # an overlap is refused, a gap needs body["confirm_gap"].
        term_gap: list[dict] = []
        if (c.inception_dt, c.expiry_dt) != dates_before:
            prev_v = s.get(Contract, c.renews_contract_id) if c.renews_contract_id else None
            next_v = _successors(s, [c.id]).get(c.id)
            if prev_v is not None or next_v is not None:
                term_gap = _check_term(c.inception_dt, c.expiry_dt, prev=prev_v,
                                       next_version=next_v,
                                       confirmed=bool(body.get("confirm_gap")))

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
        retied: list[str] = []
        added: list[str] = []
        if "wording_sections" in body:
            import contract_wording as cw
            sections = [sec for sec in (body["wording_sections"] or [])
                        if isinstance(sec, dict) and (sec.get("body") or "").strip()]
            # AFTER the limits above, so a request that changes a term and its
            # wording together ties the words to the new value, not the old one.
            #
            # Why at all: the editor shows every term as a chip you cannot type
            # by hand, but you can delete one and type the number it was
            # showing — and then the sentence stops moving when the term does.
            # That is how a contract ends up saying 13% while the check enforces
            # 14%. See contract_wording.retie.
            ctx = _wording_context(s, c)
            sections, retied = cw.retie(sections, ctx["tokens"])
            # And a term agreed since this wording was written gets the
            # sentence it never had. Same rule as the wizard: add only what the
            # document does not already say, and touch nothing else.
            #
            # What THIS save deleted stays deleted. The screen sends the
            # wording as it now stands, so the difference against what was
            # stored is the only record of the intention — and a clause whose
            # chip was replaced by prose belongs to the warning on the record,
            # not to a second sentence written beside it. Read AFTER the
            # re-tie, so a figure tied back still counts as quoted.
            _held = c.wording_sections if isinstance(c.wording_sections, dict) else {}
            _gone_sections, _gone_terms = cw.dropped_between(
                _held.get("sections"), sections)
            # Remembered ON THE CONTRACT, not just for this save. The editor
            # re-reads the wording through the preview every time it opens, and
            # a clause taken out on purpose must not be written back then.
            #
            # Self-healing in the other direction: anything the wording states
            # again falls off the list, so a term written back in — by a
            # rebuild, or by putting the chip back — starts being topped up
            # again like any other.
            _present = {s.get("key") for s in sections if isinstance(s, dict)}
            _quoted = cw.quoted_tokens(sections)
            _drop_sections = sorted((set(_held.get("dropped_sections") or [])
                                     | set(_gone_sections)) - _present)
            _drop_terms = sorted((set(_held.get("dropped_terms") or [])
                                  | set(_gone_terms)) - _quoted)
            sections, added = cw.missing_clauses(
                sections, values=ctx["values"], limits=ctx["limits"],
                type_label=ctx["type_label"],
                dropped_sections=_drop_sections, dropped_terms=_drop_terms)
            existing = dict(c.wording_sections or {})
            existing["sections"] = sections
            existing["dropped_sections"] = _drop_sections
            existing["dropped_terms"] = _drop_terms
            c.wording_sections = existing or None
            # The wording IS the clauses. See _store_authored_clauses — it
            # stores them and deliberately writes no rules.
            _store_authored_clauses(s, c, sections)

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
            existing["signature_layout"] = _clean_signature_layout(
                body["signature_layout"])
            c.wording_sections = existing or None

        s.commit()
        s.refresh(c)
        if term_gap:
            _log_gap_confirmed(s, c, p, term_gap)

        # A TERM THAT MOVED TAKES ITS CHECK WITH IT. Only for a contract whose
        # checks were already written: binding one that nobody has bound would
        # be this endpoint quietly deciding a contract should start being
        # measured, which is a decision with a button of its own. But leaving
        # rules that say 11% on a contract that now says 15% is the exact drift
        # the tokens, the re-tie and the chips all exist to prevent, and it is
        # the one place it could still happen silently.
        # Rules are never WRITTEN here — a rule is a comparison against a
        # bordereau template's columns, and those are chosen at BDX setup. But a
        # contract that has ALREADY been bound there keeps its checks in step:
        # the `rules > 0` guard means this corrects existing rules and creates
        # none, so a term corrected from 11% to 15% cannot leave a check behind
        # still measuring 11%.
        #
        # The CLAUSES are rewritten too. Their text is the wording with its
        # tokens resolved, so moving a limit moves the words — and a clause row
        # still quoting last week's number would be the exact drift the tokens
        # exist to prevent.
        rebound = None
        # Any rule at all, disabled ones included: `checks.rules` leaves those
        # out, and the question here is whether the contract was ever bound.
        if "agreed_limits" in body and s.execute(sa_text(
                "SELECT 1 FROM validation_rule WHERE contract_id = :cid LIMIT 1"),
                {"cid": c.id}).first() is not None:
            rebound = _auto_bind(s, c, p)
        if "agreed_limits" in body and "wording_sections" not in body:
            import contract_wording as cw2
            _sections = ((c.wording_sections or {}).get("sections")
                         if isinstance(c.wording_sections, dict) else None)
            if _sections:
                # A term moved (or arrived) without the wording being sent with
                # it — the terms panel saves limits alone. The sentences that
                # quote a term already follow it, because they hold tokens; a
                # term that is NEW has no sentence to follow, and this is where
                # it gets one.
                _ctx = _wording_context(s, c)
                _held = (c.wording_sections
                         if isinstance(c.wording_sections, dict) else {})
                _sections, _added = cw2.missing_clauses(
                    _sections, values=_ctx["values"], limits=_ctx["limits"],
                    type_label=_ctx["type_label"],
                    # What was deleted from this wording before now. Saving a
                    # TERM must not write back a clause somebody removed on
                    # purpose — the deletion is remembered on the contract.
                    dropped_sections=_held.get("dropped_sections"),
                    dropped_terms=_held.get("dropped_terms"))
                if _added:
                    _existing = dict(c.wording_sections or {})
                    _existing["sections"] = _sections
                    c.wording_sections = _existing
                    added.extend(_added)
                _store_authored_clauses(s, c, _sections)
                # The clause rows are written by raw SQL and the wording above
                # is an ORM change; neither is durable until this.
                s.commit()

        out = _record(s, c, p=p)
        # Named, not silent. It changed the text of a contract, and whoever
        # saved it is the only person who can say the tie was wrong.
        out["wording_retied"] = [
            ct.AGREED_LIMITS[k]["question"] for k in retied
            if k in ct.AGREED_LIMITS]
        # Same reason as the re-tie: text appeared in a contract, and the person
        # who saved it is the one who gets to say whether it belongs there.
        out["wording_added"] = [
            ct.AGREED_LIMITS[k]["question"] for k in dict.fromkeys(added)
            if k in ct.AGREED_LIMITS]
        if rebound:
            out["mapping"] = rebound
        return out


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
    if to == "active" and c.renews_contract_id:
        _check_live_term(c)
    c.lifecycle = to
    c.lifecycle_effective_date = _today()
    if to == "active" and c.renews_contract_id:
        _supersede_predecessor(c)
        _mark_renewal_live(c)


def _check_live_term(c: Contract) -> None:
    """The last line of defence (D1): a renewal does not go in force while its
    dates overlap the version it renews — whichever road brought it here.
    Gaps were confirmed when the dates were set and are not asked again."""
    import renewal_chain as _rc
    if not _rc.overlap_check_enabled():
        return
    from sqlalchemy.orm import object_session
    s = object_session(c)
    prev = s.get(Contract, c.renews_contract_id) if s is not None else None
    if prev is None:
        return
    blocks = _rc.blocking(_rc.term_conflicts(
        getattr(c, "inception_dt", None), getattr(c, "expiry_dt", None), prev=prev))
    if blocks:
        raise HTTPException(409, {
            "message": "This renewal cannot go in force yet: " + blocks[0]["message"],
            "errors": _term_field_errors(blocks),
            "conflicts": blocks})


def _supersede_predecessor(c: Contract) -> None:
    """A renewal has gone in force: the contract it renews is now history.

    Done here, on the renewal's own move to `active`, because every road into
    force (both signatures, the executed copy accepted, the signed PDF
    submitted, e-signing) passes through _move — so none of them can forget it.
    The old contract keeps its dates and terms; only its state changes.
    """
    from sqlalchemy.orm import object_session
    s = object_session(c)
    old = s.get(Contract, c.renews_contract_id) if s is not None else None
    if old is None or _effective_lifecycle(old) not in ("active", "expired"):
        return
    old.lifecycle = "superseded"
    old.lifecycle_effective_date = _today()
    old.status = "superseded"


def _mark_renewal_live(c: Contract) -> None:
    """A renewal has gone in force: let it govern files, and tell the broker.

    Contracts raised in Kavachio start with status_ops 'drafted', which every
    "live contract" lookup skips (contract_asof.NON_GOVERNING_STATUSES) — so an
    active renewal would never be offered to the broker or matched by intake.
    It becomes 'active' here, on the one move that means "in force", and
    nowhere earlier. Renewals only: other contracts are a separate change.

    The broker is emailed the new code and dates AFTER the commit, so a
    rolled-back activation never sends anything. Never raises.
    """
    if (c.status or "").strip().lower() in ("", "drafted"):
        c.status = "active"
    try:
        from sqlalchemy import event
        from sqlalchemy.orm import object_session
        s = object_session(c)
        if s is None or not c.broker_party_id:
            return
        sends = _renewal_email_thunks(s, c)
        if sends:
            def _after_commit(_session):
                for _addr, send in sends:
                    try:
                        send()
                    except Exception:  # noqa: BLE001
                        log.exception("renewal email to %s failed", _addr)
            event.listen(s, "after_commit", _after_commit, once=True)
    except Exception:  # noqa: BLE001
        log.exception("could not prepare the renewal email for contract %s", c.id)


def _renewal_email_thunks(s, c: Contract) -> list:
    """Everything the "your contract was renewed" email needs, read now."""
    from intake_service import contract_code
    from submission_calendar_service import broker_contacts
    from db import Tenant
    prog = s.get(Program, c.program_id) if c.program_id else None
    carrier = s.get(Tenant, c.tenant_id) if c.tenant_id else None
    carrier_name = ((carrier.legal_name or carrier.tenant_name) if carrier else None) \
        or "Your carrier"
    program_name = (prog.name if prog else None) or "your programme"
    old = s.get(Contract, c.renews_contract_id) if c.renews_contract_id else None
    code = contract_code(c)
    old_code = contract_code(old) if old is not None else None
    name = c.name or c.filename or f"Contract {c.id}"
    start = c.inception_dt.strftime("%d %b %Y") if c.inception_dt else "—"
    end = c.expiry_dt.strftime("%d %b %Y") if c.expiry_dt else "—"
    contacts = broker_contacts(s, [c.broker_party_id]).get(c.broker_party_id, [])

    def _sender(person: dict):
        def send() -> None:
            from email_utils import send_email, renewal_live_email_html
            send_email(
                person["email"], f"{carrier_name} renewed your contract on {program_name}",
                renewal_live_email_html(person.get("name"), carrier_name, program_name,
                                        name, code, start, end, old_code),
                text=(f"{carrier_name} has put the renewal of your contract on "
                      f"{program_name} in force. New contract code: {code}. Period: "
                      f"{start} to {end}. Late files for the previous period still go "
                      f"under the previous contract."),
                account="NOTIFY")
        return send

    return [(p_["email"], _sender(p_)) for p_ in contacts if p_.get("email")]


def _recheck_renewal_parties(s, p: Principal, c: Contract) -> None:
    """Before a renewal goes out, the people it names must still be valid.

    A year is long enough for a broker to be taken off the programme. The
    contract being renewed was fine when it was made; this one is a new
    agreement and is held to today's facts.
    """
    if not c.renews_contract_id:
        return
    if c.program_id:
        _program_access(s, p, c.program_id)
    if c.broker_party_id is not None:
        _check_counterparty(s, p, ct.spec(c.contract_type or ct.DEFAULT_TYPE),
                            c.program_id, c.broker_party_id, c.tenant_id)


class Note(BaseModel):
    note: Optional[str] = None


class AcceptNote(Note):
    """Accepting an uploaded contract. `confirm_gap`: for a renewal whose dates
    leave days that belong to no version — the person has seen and accepted
    the gap (D2)."""
    confirm_gap: bool = False


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
# Steps 2–4 can be skipped outright                 skip-review      → agreed
# when there is nothing to negotiate or nobody to ask. Same destination, and
# deliberately NOT the same endpoint: the history has to be able to say which
# of the two happened. See skip_review.
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
        # A contract WAITING on the carrier admin is theirs to decide.
        # From any other state this is a no-op, so the carrier user's
        # own revise-and-resend loop is untouched.
        if _effective_lifecycle(c) == "pending":
            _assert_speaks_for_carrier(p, "send a contract to the broker")
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
        if _is_uploaded(c):
            raise HTTPException(
                409, "this contract was uploaded already signed, so there are no "
                     "terms for the broker to agree — accept it to make it active")
        _recheck_renewal_parties(s, p, c)

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
        # WHO IS OWED THE ANSWER. Only create_contract used to set this, so a
        # contract saved as a draft and sent out later had nobody recorded
        # against it — and every notification addressed to "whoever raised it"
        # (sent back, accepted, signed) quietly reached no one. Set only when
        # it is still empty: the person who first put it out is the one
        # waiting, not whoever re-sent it after a change request.
        if not c.submitted_by_user_id:
            c.submitted_by_user_id = p.user_id
            c.submitted_at = now
        s.add(ContractApproval(
            tenant_id=c.tenant_id, contract_id=c.id, action="sent_for_review",
            acted_by_user_id=p.user_id, acted_at=now, note=body.note))
        s.commit()
        s.refresh(c)
        return _record(s, c, p=p)


@router.post("/contracts/{contract_id}/send-back")
def send_back_contract(contract_id: int, body: Note = Note(),
                       p: Principal = Depends(current_principal)):
    """Send a colleague's contract back to them, with a reason.

    The carrier admin's third answer, beside sending the terms out and settling
    them: this one is "not yet". It goes back to `draft` — where it came from —
    with everything still on it, so the author changes what was wrong and sends
    it up again rather than raising the whole contract a second time.

    A REASON IS REQUIRED. A contract returned with nothing said about it leaves
    its author to come and ask what was wrong, which is the conversation this
    step exists to save.
    """
    note = (body.note or "").strip()
    if not note:
        raise HTTPException(400, {
            "message": "Say why you are sending this back — whoever raised it "
                       "has to know what to change.",
            "errors": {"note": "required"}})
    _require_carrier(p)
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        if _effective_lifecycle(c) != "pending":
            raise HTTPException(
                409, "this contract is not waiting on you — it is "
                     f"{_effective_lifecycle(c)}.")
        _assert_speaks_for_carrier(p, "decide on a contract")
        _move(c, "draft")
        s.add(ContractApproval(
            tenant_id=c.tenant_id, contract_id=c.id, action="sent_back",
            acted_by_user_id=p.user_id,
            acted_at=dt.datetime.now(dt.timezone.utc), note=note))
        s.commit()
        s.refresh(c)
        author = c.submitted_by_user_id
        rec = _record(s, c, p=p)
        _log_contract_event(s, c, p, "contract_sent_back", note)
        _notify_contract_sent_back(s, c, p, note, author)
        return rec



def _log_contract_event(s, c: Contract, p: Principal, action: str,
                        note: Optional[str] = None) -> None:
    """One activity row for a contract decision, with the details the
    notification bell needs to render and link it.

    Written here rather than left to the middleware because the middleware
    records the PATH and nothing else: the bell cannot show "ffff · poly org"
    from "POST /contracts/123/send-back", and it cannot group the row under its
    programme either.

    WHETHER THIS IS THE ONLY ROW DEPENDS ON THE ENDPOINT, and both answers are
    deliberate. Where the act is the carrier's own — send-back, accept,
    submit-for-approval — the path is in audit._SELF_LOGGED and this is the
    whole record. Where the act is the BROKER's — accept-terms, request-changes
    — the middleware's row stays: it is the broker's trail, filed the way every
    other broker action is, and taking it away to avoid a second line would
    have cost them their own history. This row is the carrier's copy of the
    same event, under the carrier's tenant, carrying what the carrier's bell
    needs. Those two are given DIFFERENT action names for that reason, so
    neither reader sees the same thing twice.

    Best-effort: a contract must not fail to be decided because the trail could
    not be written."""
    try:
        from audit import log_activity
        prog = s.get(Program, c.program_id) if c.program_id else None
        party = s.get(Party, c.broker_party_id) if c.broker_party_id else None
        details = {
            "contract_id": c.id,
            "name": c.name or c.filename or f"Contract {c.id}",
            # The bell groups by programme, like every other notification in it.
            "program_id": c.program_id,
            "program_name": getattr(prog, "name", None),
            "broker_name": getattr(party, "legal_name", None),
            "lifecycle": _effective_lifecycle(c),
        }
        if note:
            details["note"] = note
        log_activity(c.tenant_id, _principal_email_for(s, p), action,
                     target=f"contract:{c.id}", details=details, principal=p)
    except Exception:  # noqa: BLE001
        pass


def _principal_email_for(s, p: Principal) -> Optional[str]:
    """The actor of record. Best-effort, never raises into the request."""
    try:
        from audit import actor_email
        return actor_email(p.user_id)
    except Exception:  # noqa: BLE001
        return None


def _notify_contract_sent_back(s, c: Contract, p: Principal, note: str,
                               author) -> None:
    """Tell whoever raised it what was wrong. Nobody to tell when the contract
    was never submitted by anyone, which is the normal case for one the carrier
    admin raised themselves."""
    if not author:
        return
    try:
        from notifications import (CARRIER_USER_FOOTER, notify_people,
                                   user_recipients)
        who = _actor_name(s, p)
        name = c.name or c.filename or f"Contract {c.id}"
        notify_people(
            user_recipients(author),
            f"{who} sent your contract back",
            body="The work is all still there. Make the change and send it up again.",
            facts=[("Contract", name), ("Sent back by", who), ("Reason", note)],
            link_path=f"/contracts/{c.id}",
            link_label="Open the contract",
            subject=f"Sent back: {name}",
            footer=CARRIER_USER_FOOTER)
    except Exception:  # noqa: BLE001
        pass


# =============================================================================
#  The uploaded road  —  review and accept, because there is nothing to sign
# =============================================================================
#
# An uploaded contract did not start here. It was negotiated somewhere else,
# executed on paper somewhere else, and arrives as a finished document. Sending
# it out for the broker's agreement would be asking them to agree to something
# they have already signed, and asking for signatures in Kavachio would put
# names under a document that was signed before the app ever saw it.
#
# What it needs instead is one act by one person: the carrier admin reading it
# and saying it is ours. That is `accept`, and these two endpoints are the road
# to it — a carrier user sends it up, the admin accepts it. An admin who
# uploaded it themselves skips the first step, exactly as they do on a
# bordereau setup (direct_routes.pipeline_activate): their own act IS the
# approval, and there is nobody left to ask.


def _accept_uploaded(s, c: Contract, p: Principal,
                     note: Optional[str] = None) -> Optional[str]:
    """Record the carrier's acceptance of an uploaded contract and put it in
    force. Returns what is still in the way, or None if it went live.

    ONE SIGNATURE, NOT TWO. The carrier admin's acceptance is recorded on the
    CARRIER side and nowhere else. It would have been convenient to stamp the
    counterparty's too — the record would be tidy and `activate` would unblock
    itself — but nobody here knows who signed for them or when, and inventing a
    name is exactly the kind of fact this app is supposed to refuse to make up.
    So the counterparty side stays unsigned, visibly, and the contract goes in
    force on the strength of the executed copy rather than on a signature
    Kavachio witnessed. Which is the truth of it.

    `active` is reached directly rather than through activate_contract, which
    requires both sides signed and would therefore refuse every uploaded
    contract there has ever been. Both moves it makes — pending → signed and
    signed → active — are ordinary transitions; see LIFECYCLE_TRANSITIONS.
    """
    _recheck_renewal_parties(s, p, c)
    now = dt.datetime.now(dt.timezone.utc)
    # Only if the carrier has not already put its name to this. A second row
    # would claim the company signed twice, which is not a thing that happened.
    if "carrier" in _unsigned_sides(_signatures(s, c.id)):
        s.add(ContractSignature(
            tenant_id=c.tenant_id, contract_id=c.id, side="carrier",
            signer_name=_actor_name(s, p), method="recorded",
            by_user_id=p.user_id, signed_at=now,
            note="Accepted the executed copy on file."))
    s.add(ContractApproval(
        tenant_id=c.tenant_id, contract_id=c.id, action="carrier_accepted",
        acted_by_user_id=p.user_id, acted_at=now, note=note))
    c.executed_date = c.executed_date or _today()
    if _effective_lifecycle(c) != "signed":
        _move(c, "signed")
    docs = s.query(ContractDocument).filter(
        ContractDocument.contract_id == c.id).all()
    missing = _missing_references(c, docs)
    if missing:
        # Accepted, and said plainly why it is not running: some of its clauses
        # defer to documents nobody has supplied, so they cannot be checked.
        # Recorded as accepted rather than refused after the fact — the admin's
        # decision stands, and what is missing is a separate thing to go and get.
        return "it defers to document(s) nobody has supplied: " + ", ".join(missing)
    _move(c, "active")
    return None


@router.post("/contracts/{contract_id}/submit-for-approval")
def submit_for_approval(contract_id: int, body: AcceptNote = AcceptNote(),
                        p: Principal = Depends(current_principal)):
    """Send an uploaded contract up to the carrier admin to be accepted.

    A carrier ADMIN pressing this accepts it there and then — see the note at
    the top of this section.
    """
    _require_carrier(p)
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        state = _effective_lifecycle(c)
        if state != "draft":
            raise HTTPException(
                409, f"only a draft goes up for approval — this one is {state}.")
        # With the approval flow off (the default) nobody waits: accepted now.
        from carrier_scope import carrier_approvals_enabled
        admin = _speaks_for_carrier(p) or not carrier_approvals_enabled()
        # An uploaded renewal is held to everything it needs before anything
        # is written, so a refusal names the missing piece (B5) — ahead of the
        # check below, which would call a renewal still waiting for its signed
        # copy "written here".
        gaps = (_check_renewal_activation(s, c, p, confirmed=body.confirm_gap)
                if admin else [])
        if not _is_uploaded(c):
            raise HTTPException(
                409, "this contract was written here, so it takes the ordinary "
                     "road: the broker agrees the terms, then your carrier "
                     "signs it.")
        blocked_by = None
        if admin:
            blocked_by = _accept_uploaded(s, c, p, body.note)
        else:
            _move(c, "pending")
            c.submitted_by_user_id = p.user_id
            c.submitted_at = dt.datetime.now(dt.timezone.utc)
            s.add(ContractApproval(
                tenant_id=c.tenant_id, contract_id=c.id, action="submitted",
                acted_by_user_id=p.user_id,
                acted_at=dt.datetime.now(dt.timezone.utc),
                note=body.note or "asked for it to be accepted"))
        s.commit()
        s.refresh(c)
        rec = _record(s, c, p=p)
        if gaps:
            _log_gap_confirmed(s, c, p, gaps)
        if admin:
            _log_contract_event(s, c, p, "contract_accepted", body.note)
        else:
            _log_contract_event(s, c, p, "contract_awaiting_review", body.note)
            _notify_contract_for_acceptance(s, c, p)
        if blocked_by:
            rec["signature_note"] = ("Accepted, but it cannot become active while "
                                     + blocked_by + ".")
        return rec


@router.post("/contracts/{contract_id}/accept")
def accept_contract(contract_id: int, body: AcceptNote = AcceptNote(),
                    p: Principal = Depends(current_principal)):
    """The carrier admin accepts an uploaded contract, and it goes in force."""
    _require_carrier(p)
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        state = _effective_lifecycle(c)
        if state != "pending":
            raise HTTPException(
                409, f"this contract is not waiting on you — it is {state}.")
        _assert_speaks_for_carrier(p, "accept a contract")
        gaps = _check_renewal_activation(s, c, p, confirmed=body.confirm_gap)
        author = c.submitted_by_user_id
        blocked_by = _accept_uploaded(s, c, p, body.note)
        s.commit()
        s.refresh(c)
        rec = _record(s, c, p=p)
        if gaps:
            _log_gap_confirmed(s, c, p, gaps)
        _log_contract_event(s, c, p, "contract_accepted", body.note)
        _notify_contract_accepted(s, c, p, author)
        rec["signature_note"] = (
            "Accepted, but it cannot become active while " + blocked_by + "."
            if blocked_by else
            "Accepted and active. Bordereau setup can be built on it now.")
        return rec


def _notify_contract_for_acceptance(s, c: Contract, p: Principal) -> None:
    """Tell the carrier admin an uploaded contract is waiting on them."""
    try:
        from notifications import (CARRIER_ADMIN_FOOTER,
                                   carrier_admin_recipients, notify_people)
        who = _actor_name(s, p)
        name = c.name or c.filename or f"Contract {c.id}"
        prog = s.get(Program, c.program_id) if c.program_id else None
        party = s.get(Party, c.broker_party_id) if c.broker_party_id else None
        notify_people(
            carrier_admin_recipients(c.tenant_id),
            f"{who} has sent you a contract to accept",
            body="It was signed before it got here, so there is nothing to "
                 "negotiate and nothing to sign. Read it and either accept it "
                 "— which makes it active — or send it back.",
            facts=[f for f in (
                ("Contract", name),
                ("With", getattr(party, "legal_name", None)),
                ("Programme", getattr(prog, "name", None)),
                ("Uploaded by", who),
            ) if f[1]],
            link_path=f"/contracts/{c.id}",
            link_label="Open the contract",
            action="Review and accept it",
            subject=f"Contract to accept: {name}",
            footer=CARRIER_ADMIN_FOOTER)
    except Exception:  # noqa: BLE001
        pass


def _notify_contract_signed(s, c: Contract, p: Principal, author) -> None:
    """Tell whoever raised it that the carrier admin has signed.

    In the bell only. It is news, not work — and the work it unblocks (the
    bordereau setup) is somewhere else entirely, so a mail would arrive saying
    "nothing to do here" and be right.
    """
    if not author or author == p.user_id:
        return
    try:
        from notifications import (CARRIER_USER_FOOTER, notify_people,
                                   user_recipients)
        who = _actor_name(s, p)
        name = c.name or c.filename or f"Contract {c.id}"
        notify_people(
            user_recipients(author),
            f"{who} has signed the contract you raised",
            body="You can build the bordereau setup on it now.",
            facts=[("Contract", name), ("Signed by", who)],
            link_path=f"/contracts/{c.id}",
            link_label="Open the contract",
            subject=f"Signed: {name}",
            footer=CARRIER_USER_FOOTER)
    except Exception:  # noqa: BLE001
        pass


def _notify_contract_accepted(s, c: Contract, p: Principal, author) -> None:
    """Tell whoever sent it up that it is accepted and they can build on it."""
    if not author:
        return
    try:
        from notifications import (CARRIER_USER_FOOTER, notify_people,
                                   user_recipients)
        who = _actor_name(s, p)
        name = c.name or c.filename or f"Contract {c.id}"
        notify_people(
            user_recipients(author),
            f"{who} accepted the contract you sent up",
            body="It is active. You can build the bordereau setup on it now.",
            facts=[("Contract", name), ("Accepted by", who)],
            link_path=f"/contracts/{c.id}",
            link_label="Open the contract",
            subject=f"Accepted: {name}",
            footer=CARRIER_USER_FOOTER)
    except Exception:  # noqa: BLE001
        pass


@router.post("/contracts/{contract_id}/skip-review")
def skip_review(contract_id: int, body: ReviewRequest = ReviewRequest(),
                p: Principal = Depends(current_principal)):
    """Settle the terms without sending them out, and go straight to signing.

    The review exists so the OTHER SIDE can object before anyone signs. There
    are contracts where there is nothing for them to object to — a renewal on
    last year's wording at last year's numbers — and contracts where there is
    nobody to ask: an insurer ↔ reinsurer treaty has a counterparty with no seat
    in Kavachio at all, so `send-for-review` refuses it outright and the signing
    round refuses a draft. Between the two, a treaty could not be signed in the
    app by any route. This is that route.

    It is the carrier agreeing the terms on its own, and it is written down as
    exactly that: a `review_skipped` row in the history, so a reader months
    later can tell a contract the broker agreed from one they were never asked
    about. Which is why this is a separate endpoint rather than a flag on
    send-for-review — those two acts leave the contract in the same state and
    mean completely different things.

    Everything send-for-review checks is checked here too, and for a stronger
    reason: nothing is going out for anyone to read, so this is the last point
    at which an incomplete contract can be stopped before it carries signatures.
    """
    _require_carrier(p)
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        # A contract WAITING on the carrier admin is theirs to decide.
        # From any other state this is a no-op, so the carrier user's
        # own revise-and-resend loop is untouched.
        if _effective_lifecycle(c) == "pending":
            _assert_speaks_for_carrier(p, "settle a contract's terms")
        docs = s.query(ContractDocument).filter(
            ContractDocument.contract_id == c.id).all()
        missing = _missing_references(c, docs)
        if missing:
            raise HTTPException(400, {
                "message": "This contract defers to document(s) that have not "
                           "been supplied. Signing it now would sign up to "
                           "terms nobody can read.",
                "errors": {"documents": ", ".join(missing)}})

        try:
            ct.validate(c.contract_type or "", {
                f: getattr(c, ct.FIELDS[f]["attr"])
                for f in ct.field_names(c.contract_type or "")
                if f != "counterparty_party_id"
            } | {"counterparty_party_id": c.broker_party_id})
        except ct.ContractTypeError as e:
            raise _bad_fields(e)

        # Draft only, and refused HERE rather than left to `_move` — which
        # returns quietly when a contract is already where it is being sent, so
        # a second press would have written a second `review_skipped` row and
        # reported success. A skip is a fact about a contract, not a setting,
        # and it happens once.
        state = _effective_lifecycle(c)
        if state != "draft":
            raise HTTPException(
                409, "the terms of this contract are already settled"
                     if state == "agreed" else
                     f"a {state} contract's terms have already been out — "
                     "there is no review left to skip")
        _move(c, "agreed")
        now = dt.datetime.now(dt.timezone.utc)
        if not c.submitted_by_user_id:
            c.submitted_by_user_id = p.user_id
            c.submitted_at = now
        s.add(ContractApproval(
            tenant_id=c.tenant_id, contract_id=c.id, action="review_skipped",
            acted_by_user_id=p.user_id, acted_at=now, note=body.note))
        s.commit()
        s.refresh(c)
        rec = _record(s, c, p=p)
        # THIS ENDS AT THE SAME PLACE accept_terms DOES — `agreed`, with the
        # carrier unsigned — so it puts the contract on the carrier admin's
        # desk just as surely, and it did it silently. A carrier user may skip
        # a review (there is nobody to ask on a treaty), and when they do, the
        # admin has to be told the same way the broker agreeing tells them.
        # Not when the admin did it themselves: they are standing on the page.
        if not _speaks_for_carrier(p):
            _log_contract_event(s, c, p, "contract_awaiting_signature",
                                body.note)
            _notify_terms_agreed(s, c, p, skipped=True)
        return rec


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
    # Optional at the schema level so a request made entirely of named terms
    # does not 422 before the handler can decide whether it says enough. What
    # counts as "enough" is one rule, and it lives in the handler.
    note: Optional[str] = None
    changes: list[ProposedChange] = []


@router.post("/contracts/{contract_id}/request-changes")
def request_changes(contract_id: int, body: ChangeRequest,
                    p: Principal = Depends(current_principal)):
    """Push back on the terms. The broker's half of the negotiation.

    SOMETHING has to be said. "Rejected" with no reason is a wall rather than a
    negotiation, and the carrier cannot answer what it cannot read.

    But prose is not the only way to say it. A named term — this field, this
    value, this reason — says what is wanted more precisely than a sentence
    does, and it is what lets the carrier apply the request in one move instead
    of reading prose and retyping. Requiring a note ON TOP of one made a broker
    who had filled the row exactly stare at a button that would not go, so
    either form is accepted and only saying nothing at all is refused.
    """
    if not p.is_broker:
        raise HTTPException(
            403, "the broker answers a review — the carrier revises and "
                 "re-sends instead")
    _assert_speaks_for_broker(p, "ask for changes to a contract")
    named = [ch for ch in body.changes
             if (ch.field or "").strip() and (ch.proposed or "").strip()]
    if not (body.note or "").strip() and not named:
        raise HTTPException(400, {
            "message": "Say what needs to change — either in words, or by "
                       "naming a term and what you want it to say. The carrier "
                       "can only answer what it can read.",
            "errors": {"note": "required unless you name a term"}})

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

        # Only while the contract is THEIRS. `signed -> changes_requested` is a
        # legal transition (it is how a signature is withdrawn from the
        # carrier's side), so without this a broker could pull a contract the
        # carrier admin had already signed back into the negotiation and the
        # signature would go with it.
        _state = _effective_lifecycle(c)
        if _state not in ("in_review", "agreed"):
            raise HTTPException(
                409, f"this contract is {_state}, so there is nothing in front "
                     f"of you to ask changes to.")
        _move(c, "changes_requested")
        now = dt.datetime.now(dt.timezone.utc)
        s.add(ContractApproval(
            tenant_id=c.tenant_id, contract_id=c.id, action="changes_requested",
            acted_by_user_id=p.user_id, acted_at=now, note=body.note.strip(),
            proposed_changes=[ch.model_dump() for ch in body.changes] or None))
        author = c.submitted_by_user_id
        s.commit()
        s.refresh(c)
        rec = _record(s, c, p=p)
        # The other half of the negotiation. The broker agreeing is told; the
        # broker pushing back was not, and it is the answer that actually needs
        # somebody to do something. It goes to whoever put the terms out —
        # revising them is their work, not the admin's, and the admin is not
        # needed again until there is something to sign.
        # A DIFFERENT NAME from the middleware's `contract_changes_requested`,
        # deliberately. That row is the broker's own trail and stays where it
        # is; this one is written under the CARRIER's tenant with the contract,
        # the broker and the reason on it, which is what the carrier's bell
        # needs to render and link. Same event, two readers, and giving them
        # one name would have made them one row that neither could use.
        _log_contract_event(s, c, p, "contract_pushed_back",
                            body.note.strip())
        _notify_changes_requested(s, c, p, author, body.note.strip())
        return rec


def _notify_changes_requested(s, c: Contract, p: Principal, author,
                              note: str) -> None:
    """Tell the carrier the broker has pushed back, and what they said."""
    try:
        from notifications import (CARRIER_ADMIN_FOOTER, CARRIER_USER_FOOTER,
                                   carrier_admin_recipients, notify_people,
                                   user_recipients)
        name = c.name or c.filename or f"Contract {c.id}"
        party = s.get(Party, c.broker_party_id) if c.broker_party_id else None
        broker = getattr(party, "legal_name", None) or "The broker"
        # Whoever put the terms out, if anyone is on the record; the carrier
        # admin otherwise, so a contract raised before this existed still
        # reaches a person rather than nobody.
        to = user_recipients(author) if author else []
        notify_people(
            to or carrier_admin_recipients(c.tenant_id),
            f"{broker} has asked for changes",
            body="They have read the terms and want something different. The "
                 "contract is back with you — change what they asked about and "
                 "send the revised terms out again.",
            facts=[f for f in (
                ("Contract", name),
                ("With", getattr(party, "legal_name", None)),
                ("What they said", note or None),
            ) if f[1]],
            link_path=f"/contracts/{c.id}",
            link_label="Open the contract",
            action="Read what they asked for",
            subject=f"Changes requested: {name}",
            footer=CARRIER_USER_FOOTER if to else CARRIER_ADMIN_FOOTER)
    except Exception:  # noqa: BLE001
        pass


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
    _assert_speaks_for_broker(p, "agree a contract's terms")
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        # ONLY TERMS THAT WERE PUT TO THEM. LIFECYCLE_TRANSITIONS allows
        # draft -> agreed (that is skip-review's road, and the carrier's own),
        # so without this a broker could agree a contract still being written
        # and land it on the carrier admin's desk for signature before its
        # author had finished it.
        _state = _effective_lifecycle(c)
        if _state != "in_review":
            raise HTTPException(
                409, "these terms are not out for your review — this contract "
                     f"is {_state}.")
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
        rec = _record(s, c, p=p)
        # THIS IS THE MOMENT THE CARRIER ADMIN'S GATE OPENS, and it is the only
        # one. Until now the contract was the broker's to read; from here it is
        # settled, it is back inside the carrier, and the next act on it —
        # signing — is the carrier admin's alone. Nothing else tells them: the
        # broker agreeing is an act on the broker's screen, and the carrier
        # side would otherwise only find out by going and looking.
        _log_contract_event(s, c, p, "contract_awaiting_signature", body.note)
        _notify_terms_agreed(s, c, p)
        return rec


def _notify_terms_agreed(s, c: Contract, p: Principal,
                         skipped: bool = False) -> None:
    """The broker has agreed. Mail the carrier admin, because it is now theirs.

    BY MAIL, not only in the app. This is the one step of the flow that stops
    dead until a named person acts — the carrier signs first, so the broker
    cannot move either — and a notice that waits for them to come back and look
    is a notice that can hold up both sides for a week.

    Whoever RAISED it is told too, but through the bell rather than here: the
    `contract_awaiting_signature` activity row carries no seat, so both seats
    see it (NotificationBell.NOTIFY). For them it is news, not work — nothing
    is theirs to do until the signature lands — and a second mail saying
    "nothing to do" is how people learn to ignore the first one.
    """
    try:
        from notifications import (CARRIER_ADMIN_FOOTER,
                                   carrier_admin_recipients, notify_people)
        name = c.name or c.filename or f"Contract {c.id}"
        prog = s.get(Program, c.program_id) if c.program_id else None
        party = s.get(Party, c.broker_party_id) if c.broker_party_id else None
        broker = getattr(party, "legal_name", None) or "The broker"
        who = _actor_name(s, p)
        notify_people(
            carrier_admin_recipients(c.tenant_id),
            (f"{who} settled these terms without a review — they need your "
             f"signature" if skipped else
             f"{broker} has agreed the terms — it needs your signature"),
            body=("The review was skipped, so the other side was never asked. "
                  "Read the terms before you sign: yours is the only reading "
                  "this contract will get."
                  if skipped else
                  "The terms are settled and nobody is waiting on the broker "
                  "any more. Read them and sign: a contract becomes active when "
                  "both sides have signed, and the carrier signs first."),
            facts=[f for f in (
                ("Contract", name),
                ("With", getattr(party, "legal_name", None)),
                ("Programme", getattr(prog, "name", None)),
                ("Settled by", who if skipped else None),
                ("Now", "Waiting on you to sign"),
            ) if f[1]],
            link_path=f"/contracts/{c.id}",
            link_label="Open the contract",
            action="Review and sign it",
            subject=(f"Ready to sign (no review held): {name}" if skipped
                     else f"Ready to sign: {name}"),
            footer=CARRIER_ADMIN_FOOTER)
    except Exception:  # noqa: BLE001
        pass


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
    _assert_speaks_for_broker(p, "sign a contract")
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        # THE SAME TWO RULES THE SCREEN SHOWS THIS BUTTON UNDER, enforced.
        # Terms that are still moving cannot be signed and returned, and the
        # CARRIER SIGNS FIRST — the broker is asked to sign a document that
        # already carries the carrier's signature, which is the whole reason
        # the contract stops on the carrier admin's desk at `agreed`. Without
        # these, that stop could be walked straight past from the broker's
        # side: submit-signed would carry the contract to `signed` with nobody
        # at the carrier having read it.
        _state = _effective_lifecycle(c)
        if _state != "agreed":
            raise HTTPException(
                409, "a contract is signed and returned once both sides have "
                     f"agreed its terms — this one is {_state}.")
        if "carrier" in _unsigned_sides(_signatures(s, c.id)):
            raise HTTPException(
                409, "the carrier has not signed this yet. It comes to you for "
                     "signature once they have, and you are emailed the moment "
                     "it does.")

        doc = None
        if body.document_id is not None:
            doc = s.get(ContractDocument, body.document_id)
            if not doc or doc.contract_id != c.id or not _doc_visible(doc, p):
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
        if not _unsigned_sides(sigs):
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


def _live_signing_round(s, contract_id: int):
    """The electronic signing round open on this contract, if there is one.

    Voided and declined rounds do not count — those ended without a signed
    contract, and what happens next is a fresh round or a typed signature,
    not either of them being reopened. Kept here rather than in esign_routes
    so the two places that ask (this module's guard and the round itself)
    cannot drift on what "open" means.
    """
    from db import EsignEnvelope
    return (s.query(EsignEnvelope)
            .filter(EsignEnvelope.contract_id == contract_id,
                    EsignEnvelope.status.in_(("sent", "in_progress", "completed")))
            .order_by(EsignEnvelope.created_at.desc())
            .first())


def _signed_envelope(s, contract_id: int):
    """The most recent COMPLETED e-signing round on this contract — everybody
    signed, and its current PDF carries every signature — or None."""
    from db import EsignEnvelope
    return (s.query(EsignEnvelope)
            .filter(EsignEnvelope.contract_id == contract_id,
                    EsignEnvelope.status == "completed")
            .order_by(EsignEnvelope.completed_at.desc().nullslast(),
                      EsignEnvelope.id.desc())
            .first())


def settle_signatures(s, c: Contract) -> tuple[list, bool, Optional[str]]:
    """After a signature: if both sides have now signed, put the contract in
    force — or stop at `signed` and say what is in the way. Shared by signing
    here and a reinsurer signing on its private link (treaty_routes).

    Returns (signatures, both signed, what blocks it or None)."""
    sigs = _signatures(s, c.id)
    both = not _unsigned_sides(sigs)
    blocked_by = None
    if both:
        c.executed_date = c.executed_date or _today()
        docs = s.query(ContractDocument).filter(
            ContractDocument.contract_id == c.id).all()
        missing = _missing_references(c, docs)
        if missing:
            blocked_by = ("it defers to document(s) nobody has supplied: "
                          + ", ".join(missing))
        # Signed by both, but something else genuinely in the way: recorded as
        # signed and said plainly, rather than activated with a hole in it or
        # refused after the signature was given.
        if _effective_lifecycle(c) != "signed":
            _move(c, "signed")
        if not blocked_by:
            _move(c, "active")
    return sigs, both, blocked_by


def counterparty_signing_block(s, c: Contract) -> Optional[str]:
    """Why the counterparty cannot sign this contract right now, or None.
    The same rules as sign_contract, for the reinsurer's signing link."""
    state = _effective_lifecycle(c)
    if state in ("in_review", "changes_requested", "pending"):
        return "its terms are still being settled"
    if state in ("active", "expired", "terminated", "superseded"):
        return f"it is {state}, so it is not waiting on a signature"
    sigs = _signatures(s, c.id)
    if "counterparty" not in _unsigned_sides(sigs):
        return "the reinsurer's side has already signed"
    if "carrier" in _unsigned_sides(sigs):
        return "the carrier has not signed it yet — the carrier signs first"
    return None


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

        # Counted exactly as _record counts it (see `has_wording` there),
        # blob included. The two had drifted: the record said an old contract
        # carrying only a blob_ref HAD a wording and offered Sign, and this
        # said it did not and answered 400. One of the two had to be wrong and
        # the user could not tell which.
        sections = ((c.wording_sections or {}).get("sections")
                    if isinstance(c.wording_sections, dict) else None)
        has_wording = bool(sections) or bool(c.blob_ref or c.blob) or bool(
            s.query(ContractDocument).filter(
                ContractDocument.contract_id == c.id,
                ContractDocument.kind == "contract",
                ContractDocument.is_active.is_(True)).first())
        if not has_wording:
            raise HTTPException(
                400, "there is nothing to sign yet — this contract has no "
                     "wording. Write it or attach it first.")

        # ── whose side ──
        # The carrier's signature — and one it RECORDS on the other side's
        # behalf, which is just as much the carrier's word — is the carrier
        # admin's to give. A no-op when the caller is a broker, whose own
        # equivalent is the line below.
        _assert_speaks_for_carrier(p, "sign a contract")
        if p.is_broker:
            _assert_speaks_for_broker(p, "sign a contract")
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

        # The carrier signs first — see _whose_turn. A broker signing ahead of
        # it would put their name under a document the carrier has not signed,
        # and the two sides would end up holding different things. `recorded`
        # is exempt: that is the carrier entering a signature made on paper,
        # where the order was somebody else's to keep.
        if (side == "counterparty" and not body.recorded
                and "carrier" in _unsigned_sides(existing)):
            raise HTTPException(
                409, "the carrier has not signed this yet. It comes to you for "
                     "signature once they have, and you are emailed the moment "
                     "it does.")

        # A signing round already open on this contract owns the signature.
        # Typing a name here as well would record the same fact twice, by two
        # different routes, one of them not on the document — and the round
        # would go on waiting for a signature the contract already claims to
        # have. Recording a paper signature stays allowed: that is a fact from
        # outside, and it is what a round cannot capture.
        if not body.recorded:
            live = _live_signing_round(s, c.id)
            if live is not None:
                raise HTTPException(
                    409, "this contract is out for electronic signature — sign "
                         "it there, so your signature lands on the document "
                         "itself. Open it from the Sign button.")

        # A treaty's counterparty signs OUTSIDE Kavachio — the reinsurer has no
        # seat here — so the only evidence of that signature is the signed copy
        # that came back. Recording it without one would leave a treaty in
        # force on the strength of a name typed into a box.
        if (body.recorded and side == "counterparty"
                and (c.contract_type or "") == "insurer_reinsurer"
                and body.document_id is None):
            raise HTTPException(400, {
                "message": "Attach the signed slip or wording the reinsurer "
                           "sent back, and pick it here. A reinsurer signs "
                           "outside Kavachio, so that document is the proof "
                           "of their signature.",
                "errors": {"document_id": "required for a treaty"}})

        doc_id = None
        if body.document_id is not None:
            doc = s.get(ContractDocument, body.document_id)
            # An evidence file is not found for a broker (B1), and no
            # reference, endorsement or evidence file can be the signed copy —
            # the same rule as submit_signed.
            if not doc or doc.contract_id != c.id or not _doc_visible(doc, p):
                raise HTTPException(404, "that document is not on this contract")
            if doc.kind != "contract":
                raise HTTPException(
                    400, "the signed copy is the WORDING, not a reference, an "
                         "endorsement or an evidence file")
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

        sigs, both, blocked_by = settle_signatures(s, c)

        author = c.submitted_by_user_id
        s.commit()
        s.refresh(c)
        rec = _record(s, c, p=p)
        # The carrier's signature is what releases the rest of the flow: until
        # it is given, a bordereau setup built on this contract cannot be
        # finished (direct_routes._pipeline_ready). Whoever raised it is waiting
        # on exactly this, and had no way to know it had happened.
        if side == "carrier":
            _log_contract_event(s, c, p, "contract_signed_off")
            _notify_contract_signed(s, c, p, author)
        rec["signature_note"] = (
            f"Signed for the {'carrier' if side == 'carrier' else 'counterparty'}"
            f" by {name}."
            + ("" if not both else
               (f" Both sides have now signed, but it cannot become active while "
                f"{blocked_by}." if blocked_by
                else " Both sides have signed — the contract is active."))
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
                409, "this contract is already active on the strength of "
                     "these signatures. Terminate it instead — an active "
                     "contract nobody signed is not a state worth having.")
        sg = s.get(ContractSignature, signature_id)
        if not sg or sg.contract_id != c.id:
            raise HTTPException(404, "that signature is not on this contract")
        if p.is_broker and sg.side != "counterparty":
            raise HTTPException(403, "that is not your signature to withdraw")
        # WHOEVER COULD GIVE IT IS WHO MAY TAKE IT BACK. Signing is the carrier
        # admin's alone, and withdrawing a signature is the same act in
        # reverse — it un-signs the company's name and, on an uploaded
        # contract, erases the acceptance that put it in force. Without this a
        # carrier user could undo their admin's decision, which is the one
        # thing this whole gate exists to prevent.
        _assert_speaks_for_carrier(p, "withdraw a signature")
        if p.is_broker:
            _assert_speaks_for_broker(p, "withdraw a signature")
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
                           f"cannot be made active. A contract becomes active "
                           f"because both sides signed it.",
                "errors": {"signatures": ", ".join(unsigned)}})

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


@router.post("/contracts/{contract_id}/continue-without-references")
def continue_without_references(contract_id: int,
                                p: Principal = Depends(current_principal)):
    """The carrier goes on without the document(s) the wording defers to.

    Asked right after a renewal's new PDF is read: upload them now, or continue
    anyway. Continuing records the names on the contract so the missing-
    reference gate stops blocking it; the clauses pointing at them still make
    no rules. Drafts only — a contract in force was cleared already.
    """
    _require_carrier(p)
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        if _effective_lifecycle(c) != "draft":
            raise HTTPException(409, "only a draft contract can go on without "
                                     "its referenced documents")
        docs = s.query(ContractDocument).filter(
            ContractDocument.contract_id == c.id).all()
        missing = _missing_references(c, docs)
        if missing:
            ext = dict(c.extracted or {})
            ext["references_waived"] = sorted(
                set(ext.get("references_waived") or []) | set(missing))
            c.extracted = ext
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
    # Only for a contract that never had one — an UPLOADED contract does not
    # (extraction does not fill it), yet the type requires it, so without
    # this its renewal could not be saved at all. One that has it carries it.
    class_of_business: Optional[str] = None
    # UPLOADED contracts only: no new signed PDF — the renewal keeps the old
    # contract's PDF and gets its own copy of the clauses, terms and checks
    # (see _copy_contract_rules). Off by default: a new PDF is the normal road.
    same_wording: bool = False
    # A gap between the renewed contract's end and this start is allowed, but
    # only once somebody has said so (renewal_chain.term_conflicts, D2).
    confirm_gap: bool = False


def _check_term(new_inception, new_expiry, *, prev, next_version,
                confirmed: bool) -> list[dict]:
    """Refuse dates that overlap a neighbouring version (D1), and a gap nobody
    confirmed (D2). Returns the confirmed gap warnings (to be recorded), or [].

    409 shapes the screens read: {"message", "conflicts"} for an overlap, and
    {"message", "conflicts", "needs_confirm": true} for an unconfirmed gap.
    RENEWAL_OVERLAP_CHECK=false skips all of it."""
    import renewal_chain as _rc
    if not _rc.overlap_check_enabled():
        return []
    found = _rc.term_conflicts(new_inception, new_expiry, prev=prev,
                               next_version=next_version)
    blocks = _rc.blocking(found)
    if blocks:
        raise HTTPException(409, {"message": blocks[0]["message"],
                                  "errors": _term_field_errors(blocks),
                                  "conflicts": found})
    gaps = _rc.warnings(found)
    if gaps and not confirmed:
        raise HTTPException(409, {"message": gaps[0]["message"]
                                  + " Confirm the gap to continue.",
                                  "errors": _term_field_errors(gaps),
                                  "conflicts": found, "needs_confirm": True})
    return gaps


# Which date field each conflict is about — so the screens (fieldErrors) show
# the server's sentence and mark the date, instead of "That could not be saved."
_TERM_FIELD = {"overlaps_previous": "inception_dt", "gap_after_previous": "inception_dt",
               "no_days": "inception_dt", "overlaps_next": "expiry_dt",
               "overlaps_draft_renewal": "expiry_dt", "gap_before_next": "expiry_dt",
               "expiry_before_inception": "expiry_dt"}


def _term_field_errors(conflicts: list[dict]) -> dict:
    out: dict = {}
    for c in conflicts:
        out.setdefault(_TERM_FIELD.get(c["code"], "inception_dt"), c["message"])
    return out


def _log_gap_confirmed(s, c: Contract, p: Principal, gaps: list[dict]) -> None:
    """Who accepted a gap between two versions, and how long it was (D2)."""
    try:
        from audit import log_activity
        log_activity(c.tenant_id, _principal_email_for(s, p),
                     "contract_term_gap_confirmed",
                     target=f"contract:{c.id}",
                     details={"contract_id": c.id,
                              "name": c.name or c.filename or f"Contract {c.id}",
                              "gaps": [{"code": g["code"], "days": g["days"],
                                        "with_id": g["with_id"],
                                        "message": g["message"]} for g in gaps]},
                     principal=p)
    except Exception:  # noqa: BLE001
        pass


def clauses_from_renewal_chain(s, contract: Contract) -> Optional[int]:
    """Give a renewal that has no clauses of its own a copy of the clauses of
    the nearest contract it renews that has some — its own new rows, so the
    rules written from them belong to the renewal. Returns that contract's
    id, or None when nothing up the chain was ever read.

    A renewal carries last year's terms; one made without "renew on the same
    wording" was given no document, and so nothing to write its rules from.
    Building a bordereau setup on it then failed outright ("no extracted
    clauses"). Last year's clauses are the terms it renewed on until a new
    wording is attached — and attaching one and re-reading it replaces them."""
    from sqlalchemy import text as _sql
    seen: set[int] = set()
    prev_id = contract.renews_contract_id
    source = None
    while prev_id and prev_id not in seen:
        seen.add(prev_id)
        n = s.execute(_sql(
            "SELECT COUNT(*) FROM clauses_extracted WHERE contract_id = :c "
            "AND COALESCE(created_by, '') <> 'contract_wording'"),
            {"c": prev_id}).scalar() or 0
        if n:
            source = prev_id
            break
        prev = s.get(Contract, prev_id)
        prev_id = prev.renews_contract_id if prev is not None else None
    if source is None:
        return None
    skip = {"clause_id", "contract_id", "created_at", "updated_at", "modified_at"}
    names = [n for n in s.execute(_sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'clauses_extracted' "
        "ORDER BY ordinal_position")).scalars().all() if n not in skip]
    quoted = ", ".join(f'"{n}"' for n in names)
    s.execute(_sql(
        f"INSERT INTO clauses_extracted ({quoted}, contract_id) "
        f"SELECT {quoted}, :new FROM clauses_extracted WHERE contract_id = :old "
        f"AND COALESCE(created_by, '') <> 'contract_wording' ORDER BY clause_id"),
        {"new": contract.id, "old": source})
    # Copied clauses start un-ruled: their rules are written for the renewal.
    if "rule_generation_status" in names:
        s.execute(_sql(
            "UPDATE clauses_extracted SET rule_generation_status = 'in_review', "
            "generated_rule_count = 0 WHERE contract_id = :c"), {"c": contract.id})
    return source


def _copy_contract_rules(s, old_id: int, new_id: int,
                         moves: Optional[dict] = None) -> dict:
    """Give the renewal its OWN copy of the old contract's clauses, checks,
    clause routing and extracted terms. Used by "renew on the same wording".

    New rows only — nothing on the old contract is read for write. Clause ids
    are generated by the database, so each copied clause gets a new id and the
    rows that point at clauses (a rule's source clause, a routing row) are
    re-pointed at the copy. Columns are read from the live table so a column
    added later is carried too. Timestamps start fresh.

    Clauses written from authored wording (`created_by='contract_wording'`) are
    left out: renew regenerates those from the copied wording itself.

    `moves` ({old term date: new term date}) re-dates each copied rule — its
    settings, compiled query, name and message — where a date EXACTLY equals
    the old term's start or end (renewal_chain.redate_rule). Any other date is
    left as written; the rule stays active and is flagged for review under
    rule_spec.renewal_review. A tolerance a person set by hand before
    tolerance_source existed is recorded against the OLD rule id, so the copy
    is marked tolerance_source='rule' to keep it.
    """
    import json as _json
    import renewal_chain as rc
    from contract_margin import rules_with_legacy_own_tolerance
    from sqlalchemy import text as _sql

    def cols(table: str, skip: set[str]) -> list[str]:
        names = s.execute(_sql(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t "
            "ORDER BY ordinal_position"), {"t": table}).scalars().all()
        return [n for n in names if n not in skip]

    def copy_row(table: str, pk: str, pk_val, overrides: dict, skip: set[str]):
        keep = [c for c in cols(table, skip | {pk}) if c not in overrides]
        names = keep + list(overrides)
        exprs = [f'"{c}"' for c in keep] + [f":ov_{c}" for c in overrides]
        params = {f"ov_{c}": v for c, v in overrides.items()}
        params["pk"] = pk_val
        quoted = ", ".join(f'"{n}"' for n in names)
        return s.execute(_sql(
            f"INSERT INTO {table} ({quoted}) "
            f"SELECT {', '.join(exprs)} FROM {table} WHERE \"{pk}\" = :pk "
            f"RETURNING \"{pk}\""), params).scalar()

    stamps = {"created_at", "updated_at", "modified_at"}
    clause_map: dict[int, int] = {}
    for cid in s.execute(_sql(
            "SELECT clause_id FROM clauses_extracted WHERE contract_id = :c "
            "AND COALESCE(created_by, '') <> 'contract_wording' "
            "ORDER BY clause_id"), {"c": old_id}).scalars():
        clause_map[cid] = copy_row("clauses_extracted", "clause_id", cid,
                                   {"contract_id": new_id}, stamps)

    rules, redated, review = 0, 0, []
    old_rules = s.execute(_sql(
        "SELECT rule_id, source_clause_id, rule_contract_id, rule_spec, "
        "rule_name, error_message FROM validation_rule WHERE contract_id = :c "
        "ORDER BY rule_id"), {"c": old_id}).all()
    legacy = rules_with_legacy_own_tolerance(s, [r[0] for r in old_rules])
    for rid, src, rcid, spec, rname, msg in old_rules:
        new_rid = copy_row("validation_rule", "rule_id", rid, {
            "contract_id": new_id,
            "source_clause_id": clause_map.get(src) if src is not None else None,
            "rule_contract_id": new_id if rcid == old_id else rcid,
        }, stamps)
        rules += 1
        spec_d = spec if isinstance(spec, dict) else (
            _json.loads(spec) if isinstance(spec, str) and spec.strip() else None)
        out = rc.redate_rule(spec_d, rname, msg, moves or {})
        new_spec = out["spec"] if isinstance(out["spec"], dict) else spec_d
        touched = out["changed"]
        if isinstance(new_spec, dict):
            if rid in legacy and new_spec.get("tolerance_source") is None:
                new_spec = {**new_spec, "tolerance_source": "rule"}
                touched = True
            if out["other_dates"]:
                new_spec = {**new_spec, "renewal_review": {
                    "other_dates": out["other_dates"],
                    "note": "Copied from the previous contract with dates that "
                            "are not its start or end; left as written."}}
                touched = True
        if touched:
            s.execute(_sql(
                "UPDATE validation_rule SET rule_spec = CAST(:sp AS jsonb), "
                "rule_name = :n, error_message = :m WHERE rule_id = :r"),
                {"sp": _json.dumps(new_spec) if new_spec is not None else None,
                 "n": out["name"], "m": out["message"], "r": new_rid})
        if out["changed"]:
            redated += 1
        if out["other_dates"]:
            review.append({"rule_id": new_rid, "rule_name": out["name"],
                           "dates": out["other_dates"]})

    routing = 0
    for rid, cl in s.execute(_sql(
            "SELECT routing_id, clause_id FROM contract_clause_routing "
            "WHERE contract_id = :c ORDER BY routing_id"), {"c": old_id}).all():
        copy_row("contract_clause_routing", "routing_id", rid, {
            "contract_id": new_id,
            "clause_id": clause_map.get(cl) if cl is not None else None,
        }, stamps)
        routing += 1

    terms = 0
    for tid in s.execute(_sql(
            "SELECT term_id FROM contract_term WHERE term_contract_id = :c "
            "ORDER BY term_id"), {"c": old_id}).scalars().all():
        copy_row("contract_term", "term_id", tid,
                 {"term_contract_id": new_id}, stamps)
        terms += 1

    return {"clauses": len(clause_map), "checks": rules,
            "clause_routing": routing, "terms": terms,
            "checks_redated": redated, "checks_to_review": review}


@router.post("/contracts/{contract_id}/renew")
def renew_contract(contract_id: int, body: Renewal,
                   p: Principal = Depends(current_principal)):
    """Renew into a SUCCESSOR contract, and point it back at this one.

    A renewal is deliberately a new row rather than new dates on the old one.
    Last year's contract has to keep meaning exactly what it meant while
    bordereaux were being checked against it — moving its term would silently
    re-date every decision already made under it.

    The successor starts as a DRAFT carrying last year's agreed terms and
    wording, with no documents. The carrier reviews and edits it, then sends it
    on the ordinary road (send-for-review to the broker; or, for an uploaded
    contract, the signed PDF and acceptance). Nothing goes out from here. The
    contract it replaces is superseded when this one goes in force, not now.
    """
    _require_carrier(p)
    with SessionLocal() as s:
        old = _contract_access(s, p, contract_id)
        # Locked, so two Renew clicks at once cannot both find "not renewed
        # yet" and make two renewals — and read again once held, so a date
        # correction committed while we waited is what the checks below see.
        (s.query(Contract).filter(Contract.id == old.id)
         .with_for_update().populate_existing().first())
        state = _effective_lifecycle(old)
        existing = (s.query(Contract)
                    .filter(Contract.renews_contract_id == old.id)
                    .order_by(Contract.id).first())
        # The same rule the Renew button is shown by (renewal_gate).
        gate = renewal_gate(state, old.expiry_dt, _today(),
                            _renewal_window_days(), existing)
        if gate["reason"] == "already_renewed":
            raise HTTPException(
                409, f"this contract has already been renewed — see contract "
                     f"{existing.id}.")
        if gate["reason"] == "too_early":
            raise HTTPException(
                409, f"renewal opens on {gate['opens_on']:%d %b %Y}, "
                     f"{_renewal_window_days()} days before this contract "
                     f"expires.")
        if not gate["allowed"]:
            raise HTTPException(409, f"a {state} contract cannot be renewed")
        # The new term next to the one it renews (D1/D2): an overlap is
        # refused, a gap needs confirm_gap. Nothing is created before this.
        gap = _check_term(_as_date(body.inception_dt), _as_date(body.expiry_dt),
                          prev=old, next_version=None,
                          confirmed=body.confirm_gap)

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
        new.class_of_business = (old.class_of_business
                                 or (body.class_of_business or "").strip() or None)
        new.notice_period_days = (body.notice_period_days
                                  if body.notice_period_days is not None
                                  else old.notice_period_days)
        new.premium_cap_amount = (body.premium_cap_amount
                                  if body.premium_cap_amount is not None
                                  else old.premium_cap_amount)
        new.lifecycle_effective_date = _today()
        # Last year's agreed terms and wording travel with it, for the carrier
        # to review and edit before anything goes to the broker. Documents do
        # not: a signed PDF belongs to the term it was signed for.
        if old.commercial_terms:
            new.commercial_terms = copy.deepcopy(old.commercial_terms)
        if old.wording_sections:
            new.wording_sections = copy.deepcopy(old.wording_sections)
        if body.same_wording:
            if not _is_uploaded(old):
                raise HTTPException(400, {
                    "message": "Only an uploaded contract can be renewed on its "
                               "previous PDF. A contract written here carries "
                               "its terms and wording over already.",
                    "errors": {"same_wording": "not an uploaded contract"}})
            # The same PDF, pointed at — nothing deletes a contract's file, so
            # two rows can share it. What the PDF said (its extracted values,
            # margin, referenced documents) comes too; its identity hashes do
            # not, or the renewal would read as "the same upload" to the
            # duplicate checks.
            new.filename = old.filename
            new.blob_ref = old.blob_ref
            if not old.blob_ref and old.blob:
                new.blob = old.blob
            if old.extracted:
                import renewal_chain as _rc
                ext = copy.deepcopy(old.extracted)
                if isinstance(ext, dict):
                    ext.pop("identity", None)
                    # Values read from the PDF that ARE the old term (written
                    # into output columns) follow the new term; nothing else.
                    ext = _rc.redate_json(ext, _rc.term_moves(
                        old.inception_dt, old.expiry_dt,
                        new.inception_dt, new.expiry_dt))
                new.extracted = ext
        _recheck_renewal_parties(s, p, new)

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
        if (new.contract_type or "") == "insurer_reinsurer":
            # Last year's structure and programmes come across; the carrier
            # edits what moved. See treaty_routes.copy_structure.
            import treaty_routes
            treaty_routes.copy_structure(s, old, new)
        if new.wording_sections:
            _store_authored_clauses(
                s, new, (new.wording_sections or {}).get("sections"))
        if body.same_wording:
            import renewal_chain as _rc
            copied = _copy_contract_rules(s, old.id, new.id, _rc.term_moves(
                old.inception_dt, old.expiry_dt, new.inception_dt, new.expiry_dt))
        else:
            copied = None

        # The old one stays as it is until the renewal goes in force; _move
        # supersedes it at that moment (see _supersede_predecessor). Until
        # then late files still meet the contract that governed the term.

        s.commit()
        s.refresh(new)
        if gap:
            _log_gap_confirmed(s, new, p, gap)
        out = _record(s, new, p=p)
        if copied is not None:
            out["copied_from_previous"] = copied
        return out


@router.get("/contracts/{contract_id}/renewal-check")
def renewal_check(contract_id: int, inception_dt: Optional[str] = None,
                  expiry_dt: Optional[str] = None,
                  p: Principal = Depends(current_principal)):
    """The Renew form asks this as the dates change: would a renewal of this
    contract with these dates overlap it (refused) or leave a gap (to
    confirm)? The same rule POST /renew applies. Writes nothing."""
    _require_carrier(p)
    import renewal_chain as _rc
    with SessionLocal() as s:
        old = _contract_access(s, p, contract_id)
        found = (_rc.term_conflicts(_as_date(inception_dt), _as_date(expiry_dt),
                                    prev=old)
                 if _rc.overlap_check_enabled() else [])
        sugg = _rc.suggested_inception(old)
        return {
            "conflicts": found,
            "blocked": bool(_rc.blocking(found)),
            "needs_confirm": bool(_rc.warnings(found)),
            "suggested_inception": str(sugg) if sugg else None,
            "previous_ends": (str(_rc.term_end(old)) if _rc.term_end(old) else None),
        }


@router.get("/renewal-conflicts")
def renewal_conflicts(p: Principal = Depends(current_principal)):
    """Every renewal whose dates overlap, or leave a gap with, the version it
    renews — a carrier's own, or every carrier's for Kavachio staff. A REPORT:
    nothing here changes a contract; a person resolves each one."""
    if p.is_broker:
        raise HTTPException(403, "brokers do not see this report")
    import renewal_chain as _rc
    with SessionLocal() as s:
        q = s.query(Contract).filter(Contract.renews_contract_id.isnot(None))
        if not getattr(p, "is_platform_admin", False):
            q = q.filter(Contract.tenant_id == p.tenant_id)
        rows = q.order_by(Contract.id).all()
        prevs = {c.id: c for c in s.query(Contract).filter(
            Contract.id.in_([r.renews_contract_id for r in rows])).all()} if rows else {}
        out = []
        for new in rows:
            old = prevs.get(new.renews_contract_id)
            if old is None:
                continue
            for f in _rc.term_conflicts(new.inception_dt, new.expiry_dt, prev=old):
                out.append({
                    "kind": "overlap" if f["severity"] == "block" else "gap",
                    "days": f["days"], "message": f["message"],
                    "previous": {"id": old.id, "name": old.name or old.filename,
                                 "lifecycle": _effective_lifecycle(old),
                                 "inception_dt": str(old.inception_dt) if old.inception_dt else None,
                                 "expiry_dt": str(old.expiry_dt) if old.expiry_dt else None},
                    "renewal": {"id": new.id, "name": new.name or new.filename,
                                "lifecycle": _effective_lifecycle(new),
                                "inception_dt": str(new.inception_dt) if new.inception_dt else None,
                                "expiry_dt": str(new.expiry_dt) if new.expiry_dt else None},
                    "tenant_id": new.tenant_id,
                })
        return out


def _rule_key(spec, output_template_id) -> tuple:
    """What a rule checks, independent of its id and wording: its template, the
    column(s) it reads and the output template it is written for."""
    import json as _json
    if isinstance(spec, str):
        try:
            spec = _json.loads(spec)
        except Exception:
            spec = None
    ir = (spec or {}).get("ir") if isinstance(spec, dict) else None
    ir = ir if isinstance(ir, dict) else {}
    params = ir.get("params") if isinstance(ir.get("params"), dict) else {}
    fields = params.get("fields") or params.get("field") or ""
    if isinstance(fields, list):
        fields = tuple(sorted(str(f) for f in fields))
    return (ir.get("template"), fields, output_template_id)


def _rule_rows(s, contract_id: int) -> list[dict]:
    from sqlalchemy import text as _sql
    return [dict(r._mapping) for r in s.execute(_sql(
        "SELECT rule_id, rule_name, rule_status, severity, rule_spec, "
        "output_template_id FROM validation_rule WHERE contract_id = :c "
        "ORDER BY rule_id"), {"c": contract_id}).all()]


def _params_of(spec) -> dict:
    import json as _json
    if isinstance(spec, str):
        try:
            spec = _json.loads(spec)
        except Exception:
            return {}
    ir = (spec or {}).get("ir") if isinstance(spec, dict) else None
    return dict((ir or {}).get("params") or {}) if isinstance(ir, dict) else {}


def _renewal_rule_changes(s, c: Contract) -> dict:
    """The renewal's rules against the contract it renews.

    Matched on what a rule checks (_rule_key), so a rule that was re-dated or
    regenerated with new wording still pairs with its predecessor. A key held by
    more than one rule on either side cannot be paired with certainty and is
    listed as uncertain for a person to compare.
    """
    old_rows = _rule_rows(s, c.renews_contract_id) if c.renews_contract_id else []
    new_rows = _rule_rows(s, c.id)
    from collections import defaultdict
    by_old, by_new = defaultdict(list), defaultdict(list)
    for r in old_rows:
        by_old[_rule_key(r["rule_spec"], r["output_template_id"])].append(r)
    for r in new_rows:
        by_new[_rule_key(r["rule_spec"], r["output_template_id"])].append(r)
    added, removed, changed, uncertain, same = [], [], [], [], 0
    for key in set(by_old) | set(by_new):
        o, n = by_old.get(key, []), by_new.get(key, [])
        if len(o) > 1 or len(n) > 1:
            uncertain.append({"check": key[0], "column": key[1],
                              "previous": [r["rule_name"] for r in o],
                              "renewal": [r["rule_name"] for r in n]})
        elif o and not n:
            removed.append({"rule_name": o[0]["rule_name"], "check": key[0],
                            "column": key[1]})
        elif n and not o:
            added.append({"rule_id": n[0]["rule_id"], "rule_name": n[0]["rule_name"],
                          "check": key[0], "column": key[1]})
        else:
            po = {k: v for k, v in _params_of(o[0]["rule_spec"]).items()}
            pn = {k: v for k, v in _params_of(n[0]["rule_spec"]).items()}
            diff = sorted(k for k in set(po) | set(pn) if po.get(k) != pn.get(k))
            if diff or o[0]["rule_status"] != n[0]["rule_status"]:
                changed.append({"rule_id": n[0]["rule_id"],
                                "rule_name": n[0]["rule_name"],
                                "previous_name": o[0]["rule_name"],
                                "settings": diff,
                                "previous_status": o[0]["rule_status"],
                                "status": n[0]["rule_status"]})
            else:
                same += 1
    review = []
    import json as _json
    for r in new_rows:
        sp = r["rule_spec"]
        if isinstance(sp, str):
            try:
                sp = _json.loads(sp)
            except Exception:
                sp = None
        flag = (sp or {}).get("renewal_review") if isinstance(sp, dict) else None
        if flag:
            review.append({"rule_id": r["rule_id"], "rule_name": r["rule_name"],
                           "dates": flag.get("other_dates") or []})
    return {"previous_contract_id": c.renews_contract_id,
            "previous_count": len(old_rows), "renewal_count": len(new_rows),
            "unchanged": same, "added": added, "removed": removed,
            "changed": changed, "uncertain": uncertain,
            "dates_to_review": review}


@router.get("/contracts/{contract_id}/renewal-rule-changes")
def renewal_rule_changes(contract_id: int, p: Principal = Depends(current_principal)):
    """Preview: how this renewal's rules differ from the previous contract's.
    Nothing here runs on a bordereau until the renewal is accepted and active."""
    _require_carrier(p)
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        if not c.renews_contract_id:
            raise HTTPException(400, "this contract is not a renewal")
        return _renewal_rule_changes(s, c)


@router.post("/contracts/{contract_id}/reflect-setup")
def reflect_setup(contract_id: int, p: Principal = Depends(current_principal)):
    """"Reflect new contract rules in BDX setup" — for a renewal read from its
    new PDF.

    Reuses the programme's live Bordereau Setup as it is: its input template,
    column mapping and output template are not touched. The renewal's clauses
    are turned into rules for that setup's output template by the existing
    clause → rule step (app_routes.contract_generate_rules — no PDF re-read,
    cached AI answers), which replaces the renewal's OWN rules only.

    Then, where a new rule checks exactly what one previous rule checked (one
    on each side), a previous rule a person had DISABLED stays disabled.
    Everything else — changed limits, a tolerance or column someone edited,
    rules that cannot be paired — is listed for review, never guessed.

    Allowed only while the renewal is not in force, so nothing it writes can
    reach a bordereau before the carrier accepts it.
    """
    _require_carrier(p)
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        if not c.renews_contract_id:
            raise HTTPException(400, "this contract is not a renewal")
        state = _effective_lifecycle(c)
        if state in ("active", "expired", "terminated", "superseded"):
            raise HTTPException(409, "the renewal is already in force — its rules "
                                     "are changed in Bordereau Setup now")
        if not _has_clauses(s, c.id):
            raise HTTPException(400, "read the signed PDF first — this renewal has "
                                     "no clauses to turn into rules yet")
        import intake_autorun
        pipe = intake_autorun._live_pipeline(s, c.tenant_id, c.program_id,
                                             c.broker_party_id)
        if pipe is None or not pipe.output_template_id:
            raise HTTPException(409, "this programme has no live Bordereau Setup to "
                                     "reflect the rules in — set one up first")
        program_id, template_id = c.program_id, pipe.output_template_id
        setup = {"pipeline_id": pipe.id, "name": pipe.name,
                 "output_template_id": template_id}

    import app_routes
    app_routes.contract_generate_rules(
        program_id, contract_id,
        app_routes.ContractGenerateRulesBody(output_template_id=template_id),
        principal=p)

    from sqlalchemy import text as _sql
    with SessionLocal() as s:
        c = s.get(Contract, contract_id)
        old_rows = _rule_rows(s, c.renews_contract_id)
        new_rows = _rule_rows(s, c.id)
        from collections import Counter
        ko = Counter(_rule_key(r["rule_spec"], r["output_template_id"]) for r in old_rows)
        kn = Counter(_rule_key(r["rule_spec"], r["output_template_id"]) for r in new_rows)
        disabled_old = {_rule_key(r["rule_spec"], r["output_template_id"])
                        for r in old_rows if r["rule_status"] == "disabled"}
        carried = []
        for r in new_rows:
            k = _rule_key(r["rule_spec"], r["output_template_id"])
            if k in disabled_old and ko[k] == 1 and kn[k] == 1 \
                    and r["rule_status"] != "disabled":
                s.execute(_sql("UPDATE validation_rule SET rule_status = 'disabled', "
                               "updated_at = now() WHERE rule_id = :r"),
                          {"r": r["rule_id"]})
                carried.append(r["rule_name"])
        # Margin is a carrier setting, not wording: the previous contract's
        # stays unless the renewal already has its own.
        old = s.get(Contract, c.renews_contract_id)
        from contract_margin import contract_margin_pct
        margin_note = None
        if old is not None:
            om, nm = contract_margin_pct(old.extracted), contract_margin_pct(c.extracted)
            if om and not nm:
                ext = dict(c.extracted or {}) if isinstance(c.extracted, dict) else {}
                ext["settings"] = {**(ext.get("settings") or {}), "margin_pct": om}
                c.extracted = ext
                margin_note = f"Margin {om:g}% carried from the previous contract."
            elif om and nm and om != nm:
                margin_note = (f"The renewal's margin ({nm:g}%) differs from the "
                               f"previous contract's ({om:g}%). Check it.")
        s.commit()
        out = _renewal_rule_changes(s, c)
    out.update({"setup": setup, "kept_disabled": carried, "margin_note": margin_note})
    return out


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
            "documents": [_doc_dict(d) for d in docs if _doc_visible(d, p)],
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
    _require_carrier(p)
    if kind not in DOC_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(DOC_KINDS)}")
    if not file.filename:
        raise HTTPException(400, "the file has no name")

    data = await file.read()
    if not data:
        raise HTTPException(400, "that file is empty")

    if kind == "evidence":
        # The document a large date correction rests on (Phase B). Part of
        # Change period, so off with it; never a reference, never a wording.
        import period_change as _pc
        if not _pc.enabled():
            raise HTTPException(400, "Evidence files belong to Change period, "
                                     "which is switched off.")
        if satisfies_reference:
            raise HTTPException(400, "An evidence file can't stand in for a "
                                     "reference document.")
        is_executed_copy = False
        effective_from = None

    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        state = _effective_lifecycle(c)
        # Evidence is the one thing still attached to a superseded or
        # terminated contract: its end date can be corrected (D3), and a large
        # correction needs it.
        if state in ("terminated", "superseded") and kind != "evidence":
            raise HTTPException(
                409, f"this contract is {state}, so nothing more can be "
                     f"attached to it")
        # ...and only where Change period can correct a date (D3): a draft's
        # dates are edited in its terms, with nothing to evidence.
        if kind == "evidence":
            import period_change as _pc
            if not any(_pc.allowed(state).values()):
                raise HTTPException(
                    409, f"Evidence supports a date correction, and this "
                         f"contract's dates are not corrected that way while it "
                         f"is {state.replace('_', ' ')}.")

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
            # A renewal is first named after the contract it renews
            # ("old.pdf — renewal"). Once its own new PDF is attached, that
            # name points at last year's file, so it takes the new file's
            # name — the way an uploaded contract is named. Drafts only, and
            # only while it still carries the automatic name.
            elif (c.renews_contract_id and _effective_lifecycle(c) == "draft"
                    and c.name.endswith(" — renewal")):
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

    Carrier only: a broker reviewing a contract must not be able to take the
    carrier's wording or references off it.
    """
    _require_carrier(p)
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        doc = s.get(ContractDocument, document_id)
        if not doc or doc.contract_id != c.id:
            raise HTTPException(404, "document not found")
        if doc.kind == "evidence" and _evidence_in_use(s, c, doc.id):
            raise HTTPException(409, "This evidence supports a recorded date "
                                     "correction, so it stays on the contract.")
        doc.is_active = False
        s.commit()
        docs = s.query(ContractDocument).filter(
            ContractDocument.contract_id == c.id).all()
        return {"document": _doc_dict(doc),
                "missing_references": _missing_references(c, docs),
                "rules_stale": doc.kind in ("contract", "endorsement")}


@router.get("/contracts/{contract_id}/original")
def download_original(contract_id: int,
                      p: Principal = Depends(current_principal)):
    """The file an UPLOADED contract was read from, as it arrived.

    What "use the same contract as the sibling broker" attaches to the other
    broker's upload. Kept only where blob storage is configured (see the upload
    route), so a 404 here is the normal answer on a machine without it — the
    screen then asks for the same file to be picked again rather than failing.
    """
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        data = storage.resolve_bytes(c.blob_ref, c.blob)
        if data is None:
            raise HTTPException(
                404, "the original file for this contract is not stored")
        return Response(
            content=data, media_type=_media_type(c.filename),
            headers={"Content-Disposition":
                     _content_disposition(c.filename or "contract")})


@router.get("/contracts/{contract_id}/documents/{document_id}/download")
def download_document(contract_id: int, document_id: int,
                      p: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        doc = s.get(ContractDocument, document_id)
        # Not found, rather than forbidden: a broker is not told an evidence
        # file exists.
        if not doc or doc.contract_id != c.id or not _doc_visible(doc, p):
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
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)

        # Once both sides have signed in Kavachio, THE contract is the copy they
        # signed — with their signatures on it — not a fresh composition that
        # has none. It is frozen as at signing, as an executed contract should
        # be; a later change goes through its own round.
        signed = _signed_envelope(s, c.id)
        if signed is not None:
            import esign_routes
            data = esign_routes._pdf_bytes(signed)
            fname = f"{(c.name or 'contract').strip()} (signed).pdf"
            return Response(content=data, media_type="application/pdf",
                            headers={"Content-Disposition":
                                     _content_disposition(fname)})

        data = compose_contract_pdf(s, c)

        # The state is in the filename because this file outlives the screen it
        # came from: a draft that reaches somebody's inbox should say so.
        state = _effective_lifecycle(c)
        suffix = "" if state in ("active", "signed") else f" ({state})"
        fname = f"{(c.name or 'contract').strip()}{suffix}.pdf"
        return Response(content=data, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 _content_disposition(fname)})


@router.get("/contracts/{contract_id}/pages")
def contract_pages(contract_id: int,
                   p: Principal = Depends(current_principal)):
    """How many pages the composed contract has, and how big each one is.

    What it is FOR: placing the signature blocks by hand. A block is stored as
    a page number and a point on that page, so the screen showing the document
    has to be showing the same pages the PDF has — and the width/height lets it
    reserve the right space before an image has loaded, so nothing a person has
    dragged jumps under them when it does.
    """
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        marks: list = []
        landings: dict = {}
        data = compose_contract_pdf(s, c, blank_signature_space=True,
                                    marks_out=marks, landings_out=landings)
        return {"marks": marks, "landings": landings,
                "pages": esign_pdf.page_count(data),
                "sizes": esign_pdf.page_sizes(data),
                # Where the wording is, so a block can be kept off it.
                "text": esign_pdf.text_regions(data),
                "clauses": _clause_list(_contract_sections(c))}


@router.get("/contracts/{contract_id}/pages/{page_no}")
def contract_page(contract_id: int, page_no: int,
                  scale: float = Query(2.0, ge=0.5, le=3.0),
                  p: Principal = Depends(current_principal)):
    """One page of the composed contract, as a PNG.

    Composed fresh, like the download and for the same reason: the wording is
    not a file, so there is no stored copy that could be shown instead — and a
    cached image is a picture of terms that may have moved since. It is not
    cached for the same reason.
    """
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        data = compose_contract_pdf(s, c, blank_signature_space=True)
    try:
        img = esign_pdf.render_page_png(data, page_no, scale)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return Response(content=img, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


def _clause_list(sections) -> list[dict]:
    """The clauses a signature block may be anchored to, numbered as the
    document numbers them.

    Served with the pages rather than restated on the screen, and numbered off
    the SAME list the composer typesets: `compose_pdf` numbers what it is
    given, from 1, so a screen counting its own copy would tie a block to
    clause 4 and get clause 5 the first time an empty section was dropped on
    the way in. Give this exactly what was composed — see `_composable`.
    """
    return [{"n": i,
             "title": str((sec.get("title") if isinstance(sec, dict) else None)
                          or f"Clause {i}")}
            for i, sec in enumerate(sections or [], start=1)]


def _composable(raw) -> list[dict]:
    """The sections that actually reach the page — the one filter, named once.

    A section with no body is dropped before composing, so it is not a clause,
    has no number, and must not be offered as one to anchor a block to.
    """
    return [sec for sec in (raw or [])
            if isinstance(sec, dict) and (sec.get("body") or "").strip()]


def _contract_sections(c: Contract) -> list:
    """This contract's wording sections, however the row happens to hold it."""
    return ((c.wording_sections or {}).get("sections")
            if isinstance(c.wording_sections, dict)
            else c.wording_sections) or []


class PagesIn(BaseModel):
    """A layout to compose the canvas with, instead of the saved one.

    Anchoring a block to a clause CHANGES the document — the clauses below it
    move down — so a screen that offered the choice against pages composed from
    the saved layout would show nothing happening. Sent, never stored: this
    writes no row, exactly like the GET twins it sits beside.
    """
    signature_layout: Optional[dict] = None


@router.post("/contracts/{contract_id}/pages")
def contract_pages_preview(contract_id: int, body: PagesIn,
                           p: Principal = Depends(current_principal)):
    """The pages of a contract as a layout WOULD compose it. See PagesIn."""
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        marks: list = []
        landings: dict = {}
        data = compose_contract_pdf(s, c, blank_signature_space=True,
                                    layout_override=body.signature_layout,
                                    marks_out=marks, landings_out=landings)
        return {"marks": marks, "landings": landings,
                "pages": esign_pdf.page_count(data),
                "sizes": esign_pdf.page_sizes(data),
                "text": esign_pdf.text_regions(data),
                "clauses": _clause_list(_contract_sections(c))}


@router.post("/contracts/{contract_id}/pages/{page_no}")
def contract_page_preview(contract_id: int, page_no: int, body: PagesIn,
                          scale: float = Query(2.0, ge=0.5, le=3.0),
                          p: Principal = Depends(current_principal)):
    """One page of a contract as a layout WOULD compose it. See PagesIn."""
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        data = compose_contract_pdf(s, c, blank_signature_space=True,
                                    layout_override=body.signature_layout)
    try:
        img = esign_pdf.render_page_png(data, page_no, scale)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return Response(content=img, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


def compose_contract_pdf(s, c: Contract, *,
                         anchors: dict[str, str] | None = None,
                         blank_signature_space: bool = False,
                         layout_override: dict | None = None,
                         marks_out: list | None = None,
                         landings_out: dict | None = None) -> bytes:
    """This contract's terms and wording as one PDF.

    Split out of the download endpoint because the SIGNING round needs the same
    file: an authored contract has no document row to send — its wording is
    held as sections and composed on demand — so if the two composed it
    separately the copy people signed could differ from the copy they read.
    One function, one document.

    `anchors` is the only difference between the two callers. Passed, each
    signature block is tagged with the party it belongs to and the round reads
    its boxes back out of the file; omitted, this is exactly the draft anybody
    can download. See contract_wording.compose_pdf.
    """
    import contract_wording as cw

    sections = _contract_sections(c)
    # The same context the RECORD reads, so a clause says the same thing on the
    # screen and in the file. It used to be worked out twice.
    ctx = _wording_context(s, c)
    limits = ctx["limits"]
    if not sections and not limits:
        raise HTTPException(
            404,
            "there is nothing to compose yet — this contract has no terms "
            "and no wording written in Kavachio. If its wording was "
            "uploaded, download the document itself.")

    return cw.compose_pdf(
        name=c.name or "Contract", carrier_name=ctx["carrier_name"],
        counterparty_name=ctx["counterparty_name"],
        sections=sections, tokens=ctx["tokens"],
        schedule=cw.schedule_rows(values=ctx["values"], limits=limits,
                                  type_label=ctx["type_label"],
                                  programme_name=ctx["programme_name"]),
        signers=(c.wording_sections or {}).get("signers")
                if isinstance(c.wording_sections, dict) else None,
        signature_layout=(
            layout_override if layout_override is not None
            else ((c.wording_sections or {}).get("signature_layout")
                  if isinstance(c.wording_sections, dict) else None)),
        anchors=anchors, blank_signature_space=blank_signature_space,
        marks_out=marks_out, landings_out=landings_out)


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
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        tmpl = s.get(ExportTemplate, body.output_template_id)
        if not tmpl:
            raise HTTPException(400, "that output template does not exist")
        assert_tenant_owns(p, tmpl.tenant_id)
        return _bind_checks(s, c, tmpl, p)


def _write_checks(s, c: Contract, tmpl: ExportTemplate) -> tuple[int, list, list]:
    """The mapping itself: terms → rules, written. Returns (written, rules,
    unmapped).

    Separate from the endpoint because three callers need exactly this and
    nothing else — the explicit bind, the create, and an edit that moves a term
    a check was already written from. One function, so a contract cannot end up
    with two different translations of the same terms.
    """
    import contract_rules as cr
    from app_routes import _template_fields_from_structure

    import rule_scope
    from db import engine as _engine

    terms = c.commercial_terms or {}
    fields = _template_fields_from_structure(tmpl.structure)
    rules, unmapped = cr.map_limits_to_template(terms, fields)

    rebind = True
    with _engine.begin() as conn:
        # Asked inside the write's own transaction, so a set added a moment
        # ago is seen. None added — every contract until one is: all its rules
        # replaced and the contract bound to `tmpl`, exactly as always.
        added = rule_scope.added_set_templates(conn, c.id)
        if not added:
            written = cr.write_rules(
                conn, contract_id=c.id, program_id=c.program_id,
                tenant_id=c.tenant_id, rules=rules, template_id=tmpl.id)
        else:
            # Rule sets for several templates (rule_scope.py). A term that
            # moved moves in EVERY set, each against its own template's
            # columns; no set is created for a template the contract has
            # none for, and the binding stays where it is.
            rebind = False
            bound = c.output_template_id
            own = s.get(ExportTemplate, bound) if bound is not None else None
            if own is not None:
                rules, unmapped = cr.map_limits_to_template(
                    terms, _template_fields_from_structure(own.structure))
                written = cr.write_rules(
                    conn, contract_id=c.id, program_id=c.program_id,
                    tenant_id=c.tenant_id, rules=rules, template_id=own.id,
                    set_scope={"family": sorted(
                        rule_scope.families(s, [own.id])[own.id]), "tag": None})
                tmpl = own
            else:
                written = 0
            for t in s.query(ExportTemplate).filter(ExportTemplate.id.in_(added)).all():
                t_rules, _ = cr.map_limits_to_template(
                    terms, _template_fields_from_structure(t.structure))
                cr.write_rules(
                    conn, contract_id=c.id, program_id=c.program_id,
                    tenant_id=c.tenant_id, rules=t_rules, template_id=t.id,
                    set_scope={"family": sorted(
                        rule_scope.families(s, [t.id])[t.id]), "tag": t.id})
    if rebind:
        c.output_template_id = tmpl.id
    s.commit()
    s.refresh(c)
    return written, rules, unmapped


def _resolve_template(s, c: Contract):
    """The output template this contract reports into, by the same ladder every
    run uses. None when the programme has none yet.

    Scoped by the CONTRACT'S tenant, not the caller's. The contract knows whose
    it is, and a broker raising one on a carrier's programme has no tenant of
    their own — asking the caller would make the answer depend on who pressed
    the button, and every run resolves it from the contract.
    """
    from ingester import _ensure_carrier_party
    from output_template_routes import _resolve

    tid = c.tenant_id
    carrier_party_id = _ensure_carrier_party(s, tid) if tid else None
    s.commit()
    return _resolve(s, tid, carrier_party_id, c.program_id,
                    c.broker_party_id, c.id)


def _auto_bind(s, c: Contract, p: Principal) -> Optional[dict]:
    """Bind the checks without being asked, and never at the cost of the thing
    that WAS asked.

    Two moments need this. A contract raised with ten limits on it should not
    have to be bound by hand before any of them is measured — that step was
    invisible, and a contract with no checks looks exactly like a contract with
    ten. And a term that MOVES has to take its check with it: a contract
    corrected from 11% to 15% whose rules still hold 11% is the drift this whole
    design exists to prevent, and it is silent.

    Swallows every failure. The contract is the thing being saved here; a
    programme with no bordereau template yet is a perfectly ordinary state, and
    refusing to save a contract over it would be absurd. The record reports how
    many checks there are, and the button is still on the screen.
    """
    if not (c.commercial_terms or {}):
        return None
    try:
        tmpl, level = _resolve_template(s, c)
        if tmpl is None or tmpl.tenant_id != c.tenant_id:
            return None
        written, _rules, unmapped = _write_checks(s, c, tmpl)
    except Exception:                       # never at the cost of the save
        log.warning("[contract] checks could not be bound for contract %s",
                    c.id, exc_info=True)
        s.rollback()
        return None
    return {"output_template": {"id": tmpl.id, "name": tmpl.name},
            "rules_written": written, "match_level": level,
            "unmapped": unmapped}


def _bind_checks(s, c: Contract, tmpl: ExportTemplate, p: Principal) -> dict:
    """`_write_checks`, plus the record and the report to show for it.

    What the two ENDPOINTS share — "map to this template" and "bind the
    checks" — so they answer with the same shape. The refusal belongs here
    rather than in _write_checks: writing nothing is a perfectly good outcome
    for the automatic path, and only a person who pressed a button needs to be
    told there was nothing to press it for.
    """
    if not (c.commercial_terms or {}):
        raise HTTPException(409, {
            "message": "This contract has no agreed limits to build checks "
                       "from. A contract read out of a PDF gets its rules "
                       "from its clauses instead — use Re-read rules.",
            "errors": {"agreed_limits": "none"}})

    written, rules, unmapped = _write_checks(s, c, tmpl)
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


@router.post("/contracts/{contract_id}/bind-checks")
def bind_checks(contract_id: int,
                p: Principal = Depends(current_principal)):
    """Bind this contract's terms to the checks that run on every bordereau.

    THE SAME THING THE BROKER PAGE DOES WHEN A CONTRACT IS UPLOADED, for a
    contract that was WRITTEN here instead. Uploading reads a document for its
    clauses and then writes rules from them; a contract written here already
    HAS its terms — each one carrying the comparison and the severity the
    carrier chose — so there is nothing to read and the rules follow by
    translation. What both flows were missing is the same step, and it is the
    reason a carrier could raise a contract with ten limits on it and find the
    contract screen reporting no rules at all: nothing had ever bound them.

    THE TEMPLATE IS RESOLVED, NOT ASKED FOR. A rule is a comparison against a
    bordereau column, so it needs the output template this contract reports
    into — and which template that is, is already decided by the scope ladder
    (contract → broker → programme). Asking the carrier to pick one here would
    be asking them to answer a question the app can answer, and to answer it
    differently from the way every run will.
    """
    _require_carrier(p)
    with SessionLocal() as s:
        c = _contract_access(s, p, contract_id)
        tmpl, level = _resolve_template(s, c)
        if tmpl is None:
            raise HTTPException(409, {
                "message": "There is no output template for this contract's "
                           "programme yet, and a check is a comparison against "
                           "a bordereau column — so there is nothing to write "
                           "the checks against. Build the Bordereau Setup for "
                           "this programme first; the terms are kept and can be "
                           "bound the moment one exists.",
                "errors": {"output_template": "none for this scope"}})
        assert_tenant_owns(p, tmpl.tenant_id)
        rec = _bind_checks(s, c, tmpl, p)
        # How specific the template match was, so the screen can say "this is
        # the programme's template, not this contract's" rather than implying
        # the carrier chose it.
        rec["mapping"]["match_level"] = level
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
        # A re-read replaces the contract's rules AND its clauses wholesale
        # (db_persister's existing_contract_id path). Rule sets added for other
        # output templates were written from those clauses and point at them,
        # so a re-read would take them with it — refused, and said so, rather
        # than discarding checks somebody set up. Never true of a contract that
        # has not had a set added (rule_scope.py).
        import rule_scope
        _added = rule_scope.added_set_templates(s, c.id)
        if _added:
            _names = [t.name for t in s.query(ExportTemplate)
                      .filter(ExportTemplate.id.in_(_added)).all()]
            raise HTTPException(409, {
                "message": "This contract also has rules set up for other "
                           "output templates (" + ", ".join(_names or map(str, _added))
                           + "). Re-reading replaces every rule the contract "
                           "has, and those would be lost with them, so it has "
                           "not been re-read.",
                "errors": {"rule_sets": ", ".join(map(str, _added))}})
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
            # The persister marks the row status_ops='active' as a side effect
            # of reading it. A renewal that is not in force must not govern
            # files (brokers' lists, intake, auto-run) — it becomes 'active'
            # only when it goes in force (_mark_renewal_live).
            if (c2 is not None and c2.renews_contract_id
                    and _effective_lifecycle(c2) not in ("active", "expired",
                                                         "superseded", "terminated")
                    and (c2.status or "").lower() == "active"):
                c2.status = "drafted"
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


# =============================================================================
#  Change period — Phase B: correcting a contract's dates
# =============================================================================
#
# A CORRECTION: the dates on file were wrong. Never a termination, never an
# endorsement, never the prior-period dates (B3). The rules are in
# period_change.py (D3 who may change which date, D4 large, B2 the typed code
# and its preview token, B4 when an undo is safe); this section reads the facts
# and writes the result. Switched off unless CONTRACT_PERIOD_CHANGE_ENABLED.
#
# What a correction writes, in ONE transaction: the two dates, the rules whose
# dates EXACTLY equal the old start or end (re-dated, with a snapshot of each
# rule before), the reporting calendar (refreshed; a period with files is never
# removed), a contract_approval row (the reason only — the broker reads it) and
# an activity_events row (everything, including the internal note). The broker
# email and, for a large one, the Kavachio notice go out after the commit.
# It never writes: terminated_date, termination_reason, lifecycle,
# lifecycle_effective_date, status_ops, contract_effective_from/_to, files,
# results, exceptions or deliveries.


class PeriodChange(BaseModel):
    inception_dt: str
    expiry_dt: str
    reason: Optional[str] = None
    internal_note: Optional[str] = None
    confirm_gap: bool = False
    evidence_document_id: Optional[int] = None
    confirm_text: Optional[str] = None
    preview_token: Optional[str] = None


class PeriodDates(BaseModel):
    inception_dt: str
    expiry_dt: str


class PeriodUndo(BaseModel):
    reason: Optional[str] = None
    internal_note: Optional[str] = None
    confirm_gap: bool = False
    confirm_text: Optional[str] = None
    preview_token: Optional[str] = None


def _period_dates(body) -> tuple:
    """The two dates asked for; a date that is not YYYY-MM-DD is refused
    plainly (400), not left to fail as a server error."""
    out, bad = [], {}
    for field in ("inception_dt", "expiry_dt"):
        try:
            out.append(_as_date(getattr(body, field)))
        except (TypeError, ValueError):
            out.append(None)
            bad[field] = "not a date"
    if bad:
        raise HTTPException(400, {"message": "Write the dates as YYYY-MM-DD.",
                                  "errors": bad})
    return out[0], out[1]


def _period_gate(s, p: Principal, contract_id: int) -> Contract:
    """Switched on, the carrier, its own contract. Kavachio is refused at the
    door by _contract_access (it never opens a contract record); a broker here."""
    import period_change as pc
    if not pc.enabled():
        raise HTTPException(404, "Change period is switched off.")
    _require_carrier(p)
    return _contract_access(s, p, contract_id)


def _period_neighbours(s, c: Contract, lock: bool = False):
    """(previous version, next version) of `c`. With `lock`, the three rows are
    locked and READ AGAIN once held: the versions were first loaded before the
    lock, and a correction committed to a neighbour while we waited must be
    what every check after this sees (populate_existing — a locking query does
    not refresh rows already in the session). A renewal created while we
    waited is picked up and locked too."""
    prev = s.get(Contract, c.renews_contract_id) if c.renews_contract_id else None
    nxt = _successors(s, [c.id]).get(c.id)
    if not lock:
        return prev, nxt
    locked: set = set()
    for _ in range(3):
        ids = {x.id for x in (c, prev, nxt) if x is not None}
        if ids <= locked:
            break
        (s.query(Contract).filter(Contract.id.in_(sorted(ids | locked)))
         .order_by(Contract.id).with_for_update().populate_existing().all())
        locked |= ids
        prev = s.get(Contract, c.renews_contract_id) if c.renews_contract_id else None
        nxt = _successors(s, [c.id]).get(c.id)
    return prev, nxt


def _nb_state(prev, nxt) -> dict:
    def one(v):
        return ([v.id, str(v.inception_dt) if v.inception_dt else None,
                 str(v.expiry_dt) if v.expiry_dt else None] if v is not None else None)
    return {"prev": one(prev), "next": one(nxt)}


def _period_rule_rows(s, contract_id: int, ids: Optional[list] = None) -> list:
    from sqlalchemy import text as _sql
    q = ("SELECT rule_id, rule_spec, rule_name, error_message, rule_description "
         "FROM validation_rule WHERE contract_id = :c")
    params: dict = {"c": contract_id}
    if ids is not None:
        if not ids:
            return []
        q += " AND rule_id = ANY(:ids)"
        params["ids"] = [int(i) for i in ids]
    rows = s.execute(_sql(q + " ORDER BY rule_id"), params).all()
    out = []
    for rid, spec, name, msg, desc in rows:
        if isinstance(spec, str) and spec.strip():
            try:
                spec = json.loads(spec)
            except ValueError:
                pass
        out.append((rid, spec, name, msg, desc))
    return out


def _period_rules(s, c: Contract, moves: dict) -> dict:
    """Which of this contract's rules a correction re-dates, and to what.

    Only a date EXACTLY equal to the old start or end moves
    (renewal_chain.redate_rule) — in the rule's settings and compiled query,
    its name, message and description. A rule holding any other date is listed
    for a person to look at and NOT changed. `source_verbatim_text` is the
    contract's own words and stays as quoted."""
    import period_change as pc
    import renewal_chain as rc
    redated, other = [], []
    # The contract's own start and end — now and as corrected — are not
    # "other" dates: those are the ones a correction moves (or leaves be).
    own = {str(d) for d in (c.inception_dt, c.expiry_dt) if d} \
        | {str(d) for pair in (moves or {}).items() for d in pair if d}
    for rid, spec, name, msg, desc in _period_rule_rows(s, c.id):
        spec_in = spec if isinstance(spec, (dict, list)) else None
        out = rc.redate_rule(spec_in, name, msg, moves or {})
        new_desc = rc._swap_text(desc, moves or {})
        new_spec = out["spec"] if spec_in is not None else spec
        if out["changed"] or new_desc != desc:
            redated.append({
                "rule_id": rid, "name": out["name"] or name,
                "before": {"rule_spec": spec, "rule_name": name,
                           "error_message": msg, "rule_description": desc},
                "after": {"rule_spec": new_spec, "rule_name": out["name"],
                          "error_message": out["message"], "rule_description": new_desc},
                "after_fp": pc.rule_fingerprint(new_spec, out["name"], out["message"],
                                                new_desc),
                "before_fp": pc.rule_fingerprint(spec, name, msg, desc)})
        dates = [d for d in out["other_dates"] if d not in own]
        if dates:
            other.append({"rule_id": rid, "name": name, "dates": dates})
    return {"redated": redated, "other_dates": other}


def _write_rule(s, rid: int, v: dict) -> None:
    from sqlalchemy import text as _sql
    spec = v.get("rule_spec")
    s.execute(_sql(
        "UPDATE validation_rule SET rule_spec = CAST(:sp AS jsonb), rule_name = :n, "
        "error_message = :m, rule_description = :d WHERE rule_id = :r"),
        {"sp": json.dumps(spec) if spec is not None else None, "n": v.get("rule_name"),
         "m": v.get("error_message"), "d": v.get("rule_description"), "r": rid})


def _period_files(s, c: Contract) -> list[dict]:
    """Files and results already handled under this contract, with the period
    each covers — what a date change can leave outside the term."""
    from sqlalchemy import text as _sql
    out = []
    for aid, name, period, ps, pe, outcome in s.execute(_sql(
            "SELECT arrival_id, filename, reporting_period, period_start, period_end, "
            "outcome FROM file_arrival WHERE contract_id = :c ORDER BY arrival_id"),
            {"c": c.id}).all():
        out.append({"kind": "file", "id": aid, "label": name, "period": period,
                    "start": ps, "end": pe, "outcome": outcome})
    for eid, name, ps, pe in s.execute(_sql(
            "SELECT id, COALESCE(filename, template_name), period_start, period_end "
            "FROM output_exports WHERE contract_id = :c ORDER BY id"), {"c": c.id}).all():
        out.append({"kind": "result", "id": eid, "label": name, "period": None,
                    "start": ps, "end": pe, "outcome": None})
    return out


def _review_items(files: list[dict], new_in, new_ex) -> list[dict]:
    """Of those, the ones whose period is not wholly inside [new_in, new_ex]."""
    import calendar as _cal
    import re
    import renewal_chain as rc
    items = []
    for f in files:
        ps, pe = _as_date(f["start"]), _as_date(f["end"])
        if ps is None and f.get("period"):
            ps = rc.period_start(f["period"])
            # A month ("2026-07") covers the whole month, not its first day.
            if (ps is not None and pe is None
                    and re.fullmatch(r"\d{4}-\d{2}", str(f["period"]).strip())):
                pe = ps.replace(day=_cal.monthrange(ps.year, ps.month)[1])
        if ps is None:
            continue
        pe = pe or ps
        if new_in and new_ex and (ps < new_in or pe > new_ex):
            why = ("outside the corrected term" if (pe < new_in or ps > new_ex)
                   else "partly outside the corrected term")
            items.append({"kind": f["kind"], "id": f["id"], "label": f["label"],
                          "period": f.get("period") or f"{ps} – {pe}",
                          "start": str(ps), "end": str(pe), "why": why})
    return items


def _period_marks(s, c: Contract) -> dict:
    """Fingerprints of everything an undo must not run over: files received
    and how they were handled, results, exceptions and decisions on them,
    submission versions and deliveries, policy rows. Scoped to this contract
    and its programme. An undo is refused if ANY of them differs from what the
    correction left (period_change.undo_blockers)."""
    from sqlalchemy import text as _sql
    q = {
        "arrivals": "SELECT md5(COALESCE(string_agg(concat_ws('|', arrival_id, outcome, "
                    "resolution, run_state, run_export_id, version_status, delivered_at, "
                    "contract_id, reporting_period), ',' ORDER BY arrival_id), '')) "
                    "FROM file_arrival WHERE contract_id = :c OR program_id = :p",
        "exports": "SELECT md5(COALESCE(string_agg(concat_ws('|', id, status, "
                   "release_status, released_at, version_status, exception_count, "
                   "signed_off_at), ',' ORDER BY id), '')) FROM output_exports "
                   "WHERE contract_id = :c OR program_id = :p",
        "exceptions": "SELECT md5(COALESCE(string_agg(concat_ws('|', e.exception_id, "
                      "e.status, e.exception_status, e.resolved_at), ',' ORDER BY "
                      "e.exception_id), '')) FROM validation_exception e JOIN "
                      "validation_run r ON r.run_id = e.validation_run_id "
                      "WHERE r.contract_id = :c",
        "decisions": "SELECT md5(COALESCE(string_agg(id::text, ',' ORDER BY id), '')) "
                     "FROM exception_decision_log WHERE program_id = :p",
        "versions": "SELECT md5(COALESCE(string_agg(concat_ws('|', id, received_at, "
                    "released_at, received_export_id), ',' ORDER BY id), '')) "
                    "FROM submission_version WHERE program_id = :p",
        "releases": "SELECT md5(COALESCE(string_agg(concat_ws('|', id, received_at, "
                    "version_count, released_count, released_at), ',' ORDER BY id), '')) "
                    "FROM expected_submission WHERE program_id = :p",
        "policies": "SELECT md5(COALESCE(string_agg(policy_id::text, ',' ORDER BY "
                    "policy_id), '')) FROM policy WHERE policy_contract_id = :c",
        # A file uploaded, or a run started, that has no arrival, result or
        # exception row yet — still activity an undo must not run over.
        "uploads": "SELECT md5(COALESCE(string_agg(concat_ws('|', upload_id, "
                   "upload_status, ingested_at), ',' ORDER BY upload_id), '')) "
                   "FROM upload WHERE upload_contract_id = :c OR program_id = :p",
        "bdx_uploads": "SELECT md5(COALESCE(string_agg(concat_ws('|', bdx_upload_id, "
                       "status), ',' ORDER BY bdx_upload_id), '')) FROM bdx_upload "
                       "WHERE contract_id = :c",
        "runs": "SELECT md5(COALESCE(string_agg(concat_ws('|', run_id, status, "
                "run_status, completed_at), ',' ORDER BY run_id), '')) "
                "FROM validation_run WHERE contract_id = :c",
    }
    out = {}
    for key, sql in q.items():
        try:
            with s.begin_nested():
                out[key] = s.execute(_sql(sql), {"c": c.id,
                                                 "p": c.program_id or -1}).scalar()
        except Exception:  # noqa: BLE001 — a table this database lacks
            out[key] = None
    return out


def _expected_snapshot(s, program_id: int) -> dict:
    """{id: the whole row} of a programme's reporting calendar."""
    from sqlalchemy import text as _sql
    return {int(i): json.loads(r) for i, r in s.execute(_sql(
        "SELECT id, row_to_json(e)::text FROM expected_submission e "
        "WHERE program_id = :p"), {"p": program_id}).all()}


# Worked out afresh from the dates and today; everything else on a removed
# period (its id, created_at, chase count, the notified / emailed flags) is
# history, and comes back exactly as it was.
_CALENDAR_COMPUTED = ("tenant_id", "schedule_id", "period_start", "period_end",
                      "due_date", "status")


def _restore_expected(s, program_id: int, removed: list) -> dict:
    """Put back the reporting periods a correction removed, as they were.

    Undo re-runs the calendar for the restored dates, which recreates those
    periods as NEW rows with fresh flags — so the next sweep would send their
    overdue reminders again. Each recreated row is swapped for the original
    (its id and history), with only the computed fields taken from the fresh
    one. A period the calendar did not recreate is left out and named."""
    from sqlalchemy import text as _sql
    restored, not_restored = [], []
    for snap in removed or []:
        fresh = s.execute(_sql(
            "SELECT row_to_json(e)::text FROM expected_submission e WHERE program_id = :p "
            "AND period = :per AND broker_party_id IS NOT DISTINCT FROM :b"),
            {"p": program_id, "per": snap.get("period"),
             "b": snap.get("broker_party_id")}).scalar()
        taken = s.execute(_sql("SELECT 1 FROM expected_submission WHERE id = :i"),
                          {"i": snap.get("id")}).scalar()
        if fresh is None or taken:
            not_restored.append(snap.get("period"))
            continue
        fresh = json.loads(fresh)
        if fresh.get("received_at") or (fresh.get("version_count") or 0):
            not_restored.append(snap.get("period"))
            continue
        row = {**snap, **{k: fresh.get(k) for k in _CALENDAR_COMPUTED}}
        s.execute(_sql("DELETE FROM expected_submission WHERE id = :i"), {"i": fresh["id"]})
        s.execute(_sql("INSERT INTO expected_submission SELECT * FROM "
                       "json_populate_record(NULL::expected_submission, CAST(:r AS json))"),
                  {"r": json.dumps(row)})
        restored.append(snap.get("period"))
    return {"restored": sorted(set(restored)), "not_restored": sorted(set(not_restored))}


def _period_calendar(s, c: Contract, dry: bool, restore: Optional[list] = None) -> dict:
    """Refresh the programme's reporting calendar for the dates now on `c`.
    Adds periods and removes unfiled ones only: every period with a file, a
    received mark, a version or a delivery is kept (materialize_schedule's own
    rule plus keep_periods). `dry` runs it inside a savepoint and rolls back —
    the preview's view of the effect, with nothing kept.

    For real, the whole rows of the periods it removes come back as
    `removed_rows` (kept in the correction's audit row, never sent to a
    screen), and `restore` — an undo — puts such rows back as they were."""
    from sqlalchemy import text as _sql
    from db import SubmissionSchedule, ExpectedSubmission
    import submission_calendar_service as cal
    if not c.program_id:
        return {"schedule": False}
    sched = (s.query(SubmissionSchedule)
             .filter(SubmissionSchedule.program_id == c.program_id).first())
    if sched is None:
        return {"schedule": False}
    keep = set(s.execute(_sql(
        "SELECT DISTINCT reporting_period FROM file_arrival WHERE program_id = :p "
        "AND reporting_period IS NOT NULL UNION SELECT DISTINCT period FROM "
        "submission_version WHERE program_id = :p"), {"p": c.program_id}).scalars())

    def rows():
        return {(e.broker_party_id, e.period) for e in s.query(ExpectedSubmission)
                .filter(ExpectedSubmission.program_id == c.program_id).all()}

    sp = s.begin_nested() if dry else None
    try:
        before = rows()
        snap = _expected_snapshot(s, c.program_id) if not dry else {}
        res = cal.materialize_schedule(s, sched, keep_periods=keep)
        after = rows()
    finally:
        if sp is not None:
            sp.rollback()
    added = sorted({p_ for _, p_ in after - before})
    removed = sorted({p_ for _, p_ in before - after})
    out = {"schedule": True, "added": added, "removed": removed,
           "kept_with_files": sorted(p_ for p_ in keep if p_),
           "covers_until": res.get("covers_until"),
           "bounded_by": res.get("bounded_by")}
    if not dry:
        now_ids = set(_expected_snapshot(s, c.program_id))
        out["removed_rows"] = [r for i, r in sorted(snap.items()) if i not in now_ids]
        if restore:
            out.update(_restore_expected(s, c.program_id, restore))
    return out


def _calendar_public(calendar: Optional[dict]) -> Optional[dict]:
    """The calendar effect as a screen reads it — without the stored rows."""
    if not isinstance(calendar, dict):
        return calendar
    return {k: v for k, v in calendar.items() if k != "removed_rows"}


def _new_blocking(new_conf: list, cur_conf: list, start_same: bool,
                  end_same: bool) -> tuple[list, list]:
    """(blocking conflicts the change CREATES, the ones already there that it
    leaves alone). An overlap that exists today on a side the change does not
    touch — a superseded contract's locked start overlapping the version before
    it, say — is not this change's to fix and must not stop it; it stays on the
    record (the overlap card) and is reported, not hidden."""
    import renewal_chain as rc
    untouched = {"overlaps_previous": start_same, "overlaps_next": end_same,
                 "overlaps_draft_renewal": end_same}
    existing = {(w["code"], w["with_id"]) for w in rc.blocking(cur_conf)}
    new, kept = [], []
    for w in rc.blocking(new_conf):
        if (w["code"], w["with_id"]) in existing and untouched.get(w["code"]):
            kept.append(w)
        else:
            new.append(w)
    return new, kept


def _period_plan(s, c: Contract, new_in, new_ex) -> dict:
    """Everything the preview shows and the save re-checks, for these dates."""
    import period_change as pc
    import renewal_chain as rc
    old_in, old_ex = c.inception_dt, c.expiry_dt
    lifecycle = _effective_lifecycle(c)
    term = c.terminated_date
    errors = pc.field_errors(lifecycle, old_in, old_ex, new_in, new_ex, term)
    prev, nxt = _period_neighbours(s, c)

    def eff(ex):
        # A terminated contract's days end at its termination (D1, §6b.2), so
        # correcting the written end date changes no ownership of days.
        return min(ex, term) if (term and ex and term < ex) else ex

    conflicts, gaps, blocking, existing = [], [], [], []
    if not errors:
        conflicts = rc.term_conflicts(new_in, eff(new_ex), prev=prev, next_version=nxt)
        current = rc.term_conflicts(old_in, eff(old_ex), prev=prev, next_version=nxt)
        before = {(w["code"], w["with_id"], w["days"]) for w in rc.warnings(current)}
        gaps = [w for w in rc.warnings(conflicts)
                if (w["code"], w["with_id"], w["days"]) not in before
                and not _gap_already_confirmed(s, c, w)]
        blocking, existing = _new_blocking(conflicts, current, new_in == old_in,
                                           new_ex == old_ex)
    big = pc.large(old_in, old_ex, new_in, new_ex)
    moves = rc.term_moves(old_in, old_ex, new_in, new_ex) if not errors else {}
    rules = _period_rules(s, c, moves)
    files = _period_files(s, c)
    stored = (c.lifecycle or "").strip()
    after = ("expired" if (stored == "active" and new_ex and new_ex < _today())
             else ("active" if stored == "active" else lifecycle))
    # What the preview showed rests on all of this: the dates, the versions
    # either side, the rules it re-dates, the contract's state and termination,
    # and any file, result or delivery activity — any of it changing before the
    # save means the person reviews the impact again (stale_preview).
    state = [str(old_in), str(old_ex), _nb_state(prev, nxt),
             sorted([r["rule_id"], r["before_fp"]] for r in rules["redated"]),
             lifecycle, str(term) if term else None, _period_marks(s, c)]
    return {
        "lifecycle": lifecycle, "lifecycle_after": after,
        "allowed": pc.allowed(lifecycle),
        "current": {"inception_dt": str(old_in) if old_in else None,
                    "expiry_dt": str(old_ex) if old_ex else None},
        "proposed": {"inception_dt": str(new_in) if new_in else None,
                     "expiry_dt": str(new_ex) if new_ex else None},
        "errors": errors,
        "conflicts": blocking, "existing_conflicts": existing, "gaps": gaps,
        "large": big, "moves": {str(a): str(b) for a, b in moves.items()},
        "rules": rules, "files": files,
        "review_items": _review_items(files, new_in, new_ex) if not errors else [],
        "termination": ({"date": str(term), "reason": c.termination_reason}
                        if term else None),
        "prev": prev, "next": nxt,
        "preview_token": pc.preview_token(c.id, new_in, new_ex, state),
    }


def _period_public(s, c: Contract, plan: dict, calendar: Optional[dict] = None) -> dict:
    """The preview as the carrier's screen reads it (no snapshots)."""
    import period_change as pc
    contacts = _period_broker_contacts(s, c)
    evidence = [{"id": d.id, "filename": d.filename, "created_at": _iso_utc(d.created_at)}
                for d in s.query(ContractDocument).filter(
                    ContractDocument.contract_id == c.id,
                    ContractDocument.kind == "evidence",
                    ContractDocument.is_active.is_(True))
                .order_by(ContractDocument.created_at.desc()).all()]
    large = plan["large"]["large"]
    return {
        "lifecycle": plan["lifecycle"], "lifecycle_after": plan["lifecycle_after"],
        "allowed": plan["allowed"], "current": plan["current"],
        "proposed": plan["proposed"], "errors": plan["errors"],
        "conflicts": plan["conflicts"], "gaps": plan["gaps"],
        "existing_conflicts": plan["existing_conflicts"],
        "large": plan["large"], "moves": plan["moves"],
        "rules": {"redated": [{"rule_id": r["rule_id"], "name": r["name"]}
                              for r in plan["rules"]["redated"]],
                  "other_dates": plan["rules"]["other_dates"]},
        "calendar": calendar, "review_items": plan["review_items"],
        "termination": plan["termination"],
        "broker_recipients": [x.get("name") or x.get("email") for x in contacts],
        "prior_period": "Unchanged: the prior-period dates are kept as they are.",
        "requires": {"reason": True, "evidence": large, "code": large,
                     "gap_confirmation": bool(plan["gaps"])},
        "endorsement_suggested": large,
        "code": _codes().contract_code(c) if large else None,
        "evidence_documents": evidence if large else [],
        "preview_token": plan["preview_token"],
        "ready": not plan["errors"] and not plan["conflicts"],
        "switch": pc.SWITCH,
    }


def _period_activity(s, c: Contract, p: Principal, action: str, details: dict) -> None:
    """The audit row, in the SAME transaction as the change it records — so a
    correction can never exist without the snapshot its undo needs."""
    from db import ActivityEvent
    from auth_deps import normalize_role
    s.add(ActivityEvent(
        tenant_id=c.tenant_id, actor=_principal_email_for(s, p), action=action,
        target=f"contract:{c.id}", details=details,
        actor_user_id=p.user_id, actor_role=normalize_role(p.role),
        actor_broker_party_id=None))


def _period_broker_contacts(s, c: Contract) -> list[dict]:
    """Who the broker email goes to — the broker's admins, and only while the
    broker is still on the programme: one taken off it (broker removal) is not
    told about the contract's dates any more. The preview names the same list."""
    from submission_calendar_service import broker_contacts
    from carrier_scope import link_is_live
    if not c.broker_party_id:
        return []
    if c.program_id:
        link = (s.query(ProgramBroker)
                .filter(ProgramBroker.program_id == c.program_id,
                        ProgramBroker.broker_party_id == c.broker_party_id).first())
        if link is None or not link_is_live(link):
            return []
    return broker_contacts(s, [c.broker_party_id]).get(c.broker_party_id, [])


def _period_email_thunks(s, c: Contract, kind: str, old: tuple, new: tuple,
                         reason: str) -> list:
    """The broker's email: the old and new dates and the REASON. Built only
    from those — the internal note and the evidence are never passed in."""
    from db import Tenant
    if not c.broker_party_id:
        return []
    prog = s.get(Program, c.program_id) if c.program_id else None
    carrier = s.get(Tenant, c.tenant_id) if c.tenant_id else None
    carrier_name = ((carrier.legal_name or carrier.tenant_name) if carrier else None) \
        or "Your carrier"
    program_name = (prog.name if prog else None) or "your programme"
    code = _codes().contract_code(c)
    name = c.name or c.filename or f"Contract {c.id}"

    def fmt(d):
        return d.strftime("%d %b %Y") if d else "—"

    period_old, period_new = (f"{fmt(old[0])} to {fmt(old[1])}",
                              f"{fmt(new[0])} to {fmt(new[1])}")
    contacts = _period_broker_contacts(s, c)

    def _sender(person: dict):
        def send() -> None:
            from email_utils import send_email, period_change_email_html
            send_email(
                person["email"],
                (f"{carrier_name} undid a correction to the dates of your contract "
                 f"on {program_name}" if kind == "undo" else
                 f"{carrier_name} corrected the dates of your contract on {program_name}"),
                period_change_email_html(person.get("name"), carrier_name, program_name,
                                         name, code, period_old, period_new, reason,
                                         undone=(kind == "undo")),
                text=(f"{carrier_name} has "
                      + ("undone a correction to" if kind == "undo" else "corrected")
                      + f" the dates of your contract {code} "
                      f"on {program_name}: {period_old} → {period_new}. Reason: {reason}"),
                account="NOTIFY")
        return send
    return [(x["email"], _sender(x)) for x in contacts if x.get("email")]


def _after_commit(s, thunks: list, what: str) -> None:
    from sqlalchemy import event
    if not thunks:
        return

    def run(_session):
        for addr, send in thunks:
            try:
                send()
            except Exception:  # noqa: BLE001
                log.exception("%s email to %s failed", what, addr)
    event.listen(s, "after_commit", run, once=True)


def _notify_large(c: Contract, p: Principal, s, title: str, facts: list) -> None:
    try:
        from notifications import notify_platform_admins
        # The link opens Kavachio's Audit Logs on "Contracts and rules" (a group
        # the Action type list offers), where the correction's row carries the
        # button to its evidence.
        notify_platform_admins(
            "contract_period_large_correction", title, tenant_id=c.tenant_id,
            actor=_principal_email_for(s, p), target=f"contract:{c.id}",
            facts=facts, label="large contract date correction",
            label_plural="large contract date corrections",
            link_path="/audit?action=contracts",
            link_label="Open in Audit Logs")
    except Exception:  # noqa: BLE001
        log.exception("Kavachio notice for the large correction of %s failed", c.id)


def _period_latest(s, c: Contract):
    """The latest Change period row for this contract (correction or undo)."""
    import period_change as pc
    from db import ActivityEvent
    return (s.query(ActivityEvent)
            .filter(ActivityEvent.target == f"contract:{c.id}",
                    ActivityEvent.tenant_id == c.tenant_id,
                    ActivityEvent.action.in_([pc.ACTION_CORRECTED, pc.ACTION_UNDONE]))
            .order_by(ActivityEvent.id.desc()).first())


def _period_history(s, c: Contract) -> list[dict]:
    """This contract's date corrections and undos, newest first — carrier side.
    The reason, the dates and the large flag; never the internal note."""
    import period_change as pc
    from db import ActivityEvent
    rows = (s.query(ActivityEvent)
            .filter(ActivityEvent.target == f"contract:{c.id}",
                    ActivityEvent.tenant_id == c.tenant_id,
                    ActivityEvent.action.in_([pc.ACTION_CORRECTED, pc.ACTION_UNDONE]))
            .order_by(ActivityEvent.id.desc()).all())
    out = []
    for r in rows:
        d = r.details or {}
        out.append({"ref": d.get("change_ref"),
                    "kind": "undo" if r.action == pc.ACTION_UNDONE else "correction",
                    "undoes": d.get("undoes"), "at": d.get("acted_at_utc"),
                    "old": d.get("old"), "new": d.get("new"),
                    "large": bool((d.get("large") or {}).get("large")),
                    "reason": d.get("reason"), "by": r.actor})
    return out


def _undo_facts(s, c: Contract, target, latest) -> tuple[dict, dict]:
    """(the correction as recorded, the world now) for period_change.undo_blockers.
    `target` is the correction an undo would reverse; `latest` the newest
    Change period row on the contract (the same row, unless an undo came after)."""
    import period_change as pc
    import renewal_chain as rc
    d = dict(target.details or {})
    change = {"ref": d.get("change_ref"),
              "new": [d.get("new", {}).get("inception_dt"), d.get("new", {}).get("expiry_dt")],
              "old": [d.get("old", {}).get("inception_dt"), d.get("old", {}).get("expiry_dt")],
              "lifecycle_after": d.get("lifecycle_stored_after"),
              "neighbours": d.get("neighbours"),
              "rules_after": {str(r["rule_id"]): r["after_fp"] for r in d.get("rules") or []},
              "marks": d.get("marks") or {}}
    ld = latest.details or {}
    prev, nxt = _period_neighbours(s, c)
    rows = _period_rule_rows(s, c.id, [r["rule_id"] for r in d.get("rules") or []])
    rules_now = {str(rid): pc.rule_fingerprint(spec, name, msg, desc)
                 for rid, spec, name, msg, desc in rows}
    old_in, old_ex = _as_date(change["old"][0]), _as_date(change["old"][1])
    term = c.terminated_date
    eff_ex = min(old_ex, term) if (term and old_ex and term < old_ex) else old_ex
    cur_ex = c.expiry_dt
    eff_now = min(cur_ex, term) if (term and cur_ex and term < cur_ex) else cur_ex
    now = {"latest_ref": ld.get("change_ref"),
           "latest_kind": "undo" if latest.action == pc.ACTION_UNDONE else "correction",
           "latest_undoes": ld.get("undoes"),
           "dates": [str(c.inception_dt) if c.inception_dt else None,
                     str(c.expiry_dt) if c.expiry_dt else None],
           "lifecycle": (c.lifecycle or "").strip(),
           "neighbours": _nb_state(prev, nxt),
           "rules": {k: rules_now.get(k) for k in change["rules_after"]},
           "marks": _period_marks(s, c),
           "conflicts": _new_blocking(
               rc.term_conflicts(old_in, eff_ex, prev=prev, next_version=nxt),
               rc.term_conflicts(c.inception_dt, eff_now, prev=prev, next_version=nxt),
               old_in == c.inception_dt, old_ex == c.expiry_dt)[0]}
    return change, now


def _undo_target(s, c: Contract):
    """(the latest correction, the latest Change period row). When an undo is
    the latest row it names the correction it undid; that one is returned so
    the refusal can say "already undone"."""
    import period_change as pc
    from db import ActivityEvent
    latest = _period_latest(s, c)
    if latest is None:
        return None, None
    if latest.action == pc.ACTION_CORRECTED:
        return latest, latest
    target = (s.query(ActivityEvent)
              .filter(ActivityEvent.target == f"contract:{c.id}",
                      ActivityEvent.tenant_id == c.tenant_id,
                      ActivityEvent.action == pc.ACTION_CORRECTED)
              .order_by(ActivityEvent.id.desc()).first())
    return target, latest


def _undo_plan(s, c: Contract) -> dict:
    import period_change as pc
    import renewal_chain as rc
    target, latest = _undo_target(s, c)
    if target is None:
        return {"available": False, "blockers": [{"code": "nothing",
                "message": "There is no date correction on this contract to undo."}]}
    change, now = _undo_facts(s, c, target, latest)
    blockers = pc.undo_blockers(change, now)
    d = target.details or {}
    old_in, old_ex = _as_date(d["old"]["inception_dt"]), _as_date(d["old"]["expiry_dt"])
    prev, nxt = _period_neighbours(s, c)
    term = c.terminated_date

    def eff(ex):
        return min(ex, term) if (term and ex and term < ex) else ex

    before = {(w["code"], w["with_id"], w["days"]) for w in rc.warnings(
        rc.term_conflicts(c.inception_dt, eff(c.expiry_dt), prev=prev, next_version=nxt))}
    gaps = [w for w in rc.warnings(rc.term_conflicts(old_in, eff(old_ex), prev=prev,
                                                     next_version=nxt))
            if (w["code"], w["with_id"], w["days"]) not in before
            and not _gap_already_confirmed(s, c, w)]
    large = bool((d.get("large") or {}).get("large"))
    state = [d.get("change_ref"), now["dates"], sorted(now["rules"].items()),
             now["marks"], now["neighbours"], now["latest_ref"]]
    return {"available": True, "target": target, "change_ref": d.get("change_ref"),
            "blockers": blockers, "gaps": gaps, "large": large,
            "restore": {"inception_dt": d["old"]["inception_dt"],
                        "expiry_dt": d["old"]["expiry_dt"]},
            "from": {"inception_dt": now["dates"][0], "expiry_dt": now["dates"][1]},
            "rules": len(d.get("rules") or []),
            "preview_token": pc.preview_token(c.id, old_in, old_ex, state)}


@router.post("/contracts/{contract_id}/period/preview")
def period_preview(contract_id: int, body: PeriodDates,
                   p: Principal = Depends(current_principal)):
    """What correcting these dates would do. Writes nothing: the calendar's
    effect is worked out inside a savepoint that is rolled back."""
    with SessionLocal() as s:
        c = _period_gate(s, p, contract_id)
        new_in, new_ex = _period_dates(body)
        plan = _period_plan(s, c, new_in, new_ex)
        calendar = None
        if not plan["errors"] and not plan["conflicts"]:
            sp = s.begin_nested()
            try:
                c.inception_dt, c.expiry_dt = new_in, new_ex
                s.flush()
                calendar = _period_calendar(s, c, dry=True)
            finally:
                sp.rollback()
        out = _period_public(s, c, plan, calendar)
        s.rollback()
        return out


@router.post("/contracts/{contract_id}/period")
def period_correct(contract_id: int, body: PeriodChange,
                   p: Principal = Depends(current_principal)):
    """Correct the dates (kind: correction). Every check is made again here,
    under a lock on this contract and its neighbours."""
    import period_change as pc
    import uuid as _uuid
    with SessionLocal() as s:
        c = _period_gate(s, p, contract_id)
        _period_neighbours(s, c, lock=True)
        s.refresh(c)
        new_in, new_ex = _period_dates(body)
        plan = _period_plan(s, c, new_in, new_ex)
        if plan["errors"]:
            raise HTTPException(400, {"message": next(iter(plan["errors"].values())),
                                      "errors": plan["errors"]})
        if plan["conflicts"]:
            raise HTTPException(409, {"message": plan["conflicts"][0]["message"],
                                      "errors": _term_field_errors(plan["conflicts"]),
                                      "conflicts": plan["conflicts"]})
        reason = (body.reason or "").strip()
        if not reason:
            raise HTTPException(400, {"message": "Give the reason for the correction — "
                                                 "the broker will see it.",
                                      "errors": {"reason": "required"}})
        if body.preview_token != plan["preview_token"]:
            raise HTTPException(409, {"message": "The impact has changed — review it again.",
                                      "errors": {"_": "preview out of date"},
                                      "stale_preview": True})
        if plan["gaps"] and not body.confirm_gap:
            raise HTTPException(409, {"message": plan["gaps"][0]["message"]
                                      + " Confirm the gap is intended to go ahead.",
                                      "errors": {}, "conflicts": plan["gaps"],
                                      "needs_confirm": True})
        large = plan["large"]
        evidence = None
        if large["large"]:
            evidence = (s.get(ContractDocument, body.evidence_document_id)
                        if body.evidence_document_id else None)
            if (evidence is None or evidence.contract_id != c.id
                    or evidence.kind != "evidence" or not evidence.is_active):
                raise HTTPException(400, {
                    "message": "A correction this large needs supporting evidence: "
                               "attach it as an evidence file and choose it.",
                    "errors": {"evidence_document_id": "required"}})
            code = _codes().contract_code(c)
            if not pc.code_matches(body.confirm_text, code):
                raise HTTPException(400, {
                    "message": f"Type the contract code {code} exactly to confirm.",
                    "errors": {"confirm_text": "does not match"}})

        old_in, old_ex = c.inception_dt, c.expiry_dt
        # Untouched by design (§6b.2, B3) — checked again in the tests.
        for r in plan["rules"]["redated"]:
            _write_rule(s, r["rule_id"], r["after"])
        c.inception_dt, c.expiry_dt = new_in, new_ex
        s.flush()
        calendar = _period_calendar(s, c, dry=False)
        s.flush()
        ref = _uuid.uuid4().hex[:12]
        now_utc = dt.datetime.now(dt.timezone.utc)
        changes = [{"field": f, "current": str(a) if a else None,
                    "proposed": str(b) if b else None, "ref": ref}
                   for f, a, b in (("inception_dt", old_in, new_in),
                                   ("expiry_dt", old_ex, new_ex)) if a != b]
        s.add(ContractApproval(
            tenant_id=c.tenant_id, contract_id=c.id, action=pc.APPROVAL_CORRECTED,
            acted_by_user_id=p.user_id, acted_at=now_utc, note=reason,
            proposed_changes=changes))
        prev, nxt = plan["prev"], plan["next"]
        _period_activity(s, c, p, pc.ACTION_CORRECTED, {
            "change_ref": ref, "kind": "correction", "contract_id": c.id,
            "name": c.name or c.filename or f"Contract {c.id}",
            "reason": reason, "internal_note": (body.internal_note or "").strip() or None,
            "old": plan["current"], "new": plan["proposed"],
            "lifecycle_before": plan["lifecycle"], "lifecycle_after": plan["lifecycle_after"],
            "lifecycle_stored_after": (c.lifecycle or "").strip(),
            "termination": plan["termination"],
            "large": large, "endorsement_suggested": large["large"],
            "evidence_document_id": evidence.id if evidence else None,
            "evidence_filename": evidence.filename if evidence else None,
            "gaps_confirmed": [{k: g[k] for k in ("code", "days", "with_id", "message")}
                               for g in plan["gaps"]],
            "rules": [{"rule_id": r["rule_id"], "name": r["name"], "before": r["before"],
                       "after_fp": r["after_fp"]} for r in plan["rules"]["redated"]],
            "rules_other_dates": plan["rules"]["other_dates"],
            "calendar": calendar, "review_items": plan["review_items"],
            "neighbours": _nb_state(prev, nxt),
            "marks": _period_marks(s, c),
            "prior_period": "unchanged",
            "acted_at_utc": now_utc.isoformat()})
        _after_commit(s, _period_email_thunks(s, c, "correction", (old_in, old_ex),
                                              (new_in, new_ex), reason), "period correction")
        s.commit()
        s.refresh(c)
        if large["large"]:
            _notify_large(c, p, s, "Large contract date correction", [
                ("Contract", f"{c.name or c.filename} ({_codes().contract_code(c)})"),
                ("From", f"{old_in} – {old_ex}"), ("To", f"{new_in} – {new_ex}"),
                ("Why large", " ".join(large["reasons"])), ("Reason", reason),
                ("Evidence", evidence.filename if evidence else "—")])
        rec = _record(s, c, p=p)
        rec["period_change_result"] = {"change_ref": ref, "rules_redated":
                                       len(plan["rules"]["redated"]),
                                       "calendar": _calendar_public(calendar),
                                       "review_items": len(plan["review_items"])}
        return rec


@router.post("/contracts/{contract_id}/period/undo/preview")
def period_undo_preview(contract_id: int, p: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        c = _period_gate(s, p, contract_id)
        u = _undo_plan(s, c)
        u.pop("target", None)
        if u.get("available") and u["large"]:
            u["code"] = _codes().contract_code(c)
        s.rollback()
        return u


@router.post("/contracts/{contract_id}/period/undo")
def period_undo(contract_id: int, body: PeriodUndo,
                p: Principal = Depends(current_principal)):
    """Reverse the LATEST correction — only while nothing since makes that
    unsafe or ambiguous (period_change.undo_blockers). A new, logged action:
    the correction's own rows are never edited or removed."""
    import period_change as pc
    import uuid as _uuid
    with SessionLocal() as s:
        c = _period_gate(s, p, contract_id)
        _period_neighbours(s, c, lock=True)
        s.refresh(c)
        u = _undo_plan(s, c)
        if not u.get("available") or u["blockers"]:
            b = u["blockers"]
            raise HTTPException(409, {"message": "This correction can't be undone: "
                                                 + b[0]["message"],
                                      "errors": {}, "blockers": b})
        reason = (body.reason or "").strip()
        if not reason:
            raise HTTPException(400, {"message": "Give the reason for undoing it — the "
                                                 "broker will see it.",
                                      "errors": {"reason": "required"}})
        if body.preview_token != u["preview_token"]:
            raise HTTPException(409, {"message": "The impact has changed — review it again.",
                                      "errors": {"_": "preview out of date"},
                                      "stale_preview": True})
        if u["gaps"] and not body.confirm_gap:
            raise HTTPException(409, {"message": u["gaps"][0]["message"]
                                      + " Confirm the gap is intended to go ahead.",
                                      "errors": {}, "conflicts": u["gaps"],
                                      "needs_confirm": True})
        code = _codes().contract_code(c)
        if u["large"] and not pc.code_matches(body.confirm_text, code):
            raise HTTPException(400, {
                "message": f"This undoes a large correction. Type the contract code "
                           f"{code} exactly to confirm.",
                "errors": {"confirm_text": "does not match"}})
        target = u["target"]
        d = target.details or {}
        cur_in, cur_ex = c.inception_dt, c.expiry_dt
        back_in, back_ex = _as_date(u["restore"]["inception_dt"]), _as_date(
            u["restore"]["expiry_dt"])
        restored = []
        for r in d.get("rules") or []:
            _write_rule(s, int(r["rule_id"]), r["before"])
            restored.append(int(r["rule_id"]))
        c.inception_dt, c.expiry_dt = back_in, back_ex
        s.flush()
        calendar = _period_calendar(s, c, dry=False,
                                    restore=(d.get("calendar") or {}).get("removed_rows"))
        s.flush()
        ref = _uuid.uuid4().hex[:12]
        now_utc = dt.datetime.now(dt.timezone.utc)
        changes = [{"field": f, "current": str(a) if a else None,
                    "proposed": str(b) if b else None, "ref": ref}
                   for f, a, b in (("inception_dt", cur_in, back_in),
                                   ("expiry_dt", cur_ex, back_ex)) if a != b]
        s.add(ContractApproval(
            tenant_id=c.tenant_id, contract_id=c.id, action=pc.APPROVAL_UNDONE,
            acted_by_user_id=p.user_id, acted_at=now_utc, note=reason,
            proposed_changes=changes))
        prev, nxt = _period_neighbours(s, c)
        _period_activity(s, c, p, pc.ACTION_UNDONE, {
            "change_ref": ref, "kind": "undo", "undoes": u["change_ref"],
            "contract_id": c.id, "name": c.name or c.filename or f"Contract {c.id}",
            "reason": reason, "internal_note": (body.internal_note or "").strip() or None,
            "old": u["from"], "new": u["restore"],
            "large": {"large": False}, "reverses_large": u["large"],
            "rules_restored": restored, "calendar": calendar,
            "gaps_confirmed": [{k: g[k] for k in ("code", "days", "with_id", "message")}
                               for g in u["gaps"]],
            "neighbours": _nb_state(prev, nxt),
            "acted_at_utc": now_utc.isoformat()})
        _after_commit(s, _period_email_thunks(s, c, "undo", (cur_in, cur_ex),
                                              (back_in, back_ex), reason), "period undo")
        s.commit()
        s.refresh(c)
        if u["large"]:
            _notify_large(c, p, s, "Large contract date correction undone", [
                ("Contract", f"{c.name or c.filename} ({code})"),
                ("From", f"{cur_in} – {cur_ex}"), ("Back to", f"{back_in} – {back_ex}"),
                ("Reason", reason)])
        rec = _record(s, c, p=p)
        rec["period_change_result"] = {"change_ref": ref, "undoes": u["change_ref"],
                                       "rules_restored": len(restored),
                                       "calendar": _calendar_public(calendar)}
        return rec


@router.get("/contracts/{contract_id}/period-review")
def period_review(contract_id: int, p: Principal = Depends(current_principal)):
    """Files and results whose period falls outside the contract's term now,
    and rules holding dates that are not its start or end. Read-only: what to
    do about each is Phase D."""
    import renewal_chain as rc
    with SessionLocal() as s:
        c = _period_gate(s, p, contract_id)
        files = _period_files(s, c)
        rules = _period_rules(s, c, {})
        return {"items": _review_items(files, c.inception_dt, c.expiry_dt),
                "rules_other_dates": rules["other_dates"],
                "term": {"inception_dt": str(c.inception_dt) if c.inception_dt else None,
                         "expiry_dt": str(c.expiry_dt) if c.expiry_dt else None}}


def _evidence_in_use(s, c: Contract, doc_id: int) -> bool:
    """Does a recorded correction of this contract rest on this evidence file?
    Then it is part of the audit trail and is not retired."""
    import period_change as pc
    from db import ActivityEvent
    rows = (s.query(ActivityEvent.details)
            .filter(ActivityEvent.action == pc.ACTION_CORRECTED,
                    ActivityEvent.target == f"contract:{c.id}",
                    ActivityEvent.tenant_id == c.tenant_id).all())
    return any(str((d or {}).get("evidence_document_id")) == str(doc_id) for (d,) in rows)


def _correction_evidence(s, event_id: int):
    """The evidence file a recorded LARGE correction relied on — reached only
    through that correction's own audit row, or None.

    So a file uploaded as evidence and never used for a correction stays the
    carrier's alone, and an id typed by hand reaches nothing: the row must be
    a correction, name this contract, belong to the contract's carrier, and
    point at an evidence document of that same contract."""
    import period_change as pc
    from db import ActivityEvent
    ev = s.get(ActivityEvent, event_id)
    if ev is None or ev.action != pc.ACTION_CORRECTED:
        return None
    d = ev.details if isinstance(ev.details, dict) else {}
    doc_id, cid = d.get("evidence_document_id"), d.get("contract_id")
    if (not doc_id or not cid or not (d.get("large") or {}).get("large")
            or ev.target != f"contract:{cid}"):
        return None
    c = s.get(Contract, int(cid))
    doc = s.get(ContractDocument, int(doc_id))
    if (c is None or doc is None or c.tenant_id != ev.tenant_id
            or doc.contract_id != c.id or doc.kind != "evidence"):
        return None
    return doc


@router.get("/admin/contract-period-evidence/{event_id}/file")
def period_evidence_for_kavachio(event_id: int,
                                 p: Principal = Depends(current_principal)):
    """Kavachio's one way to an evidence file (B1): the file behind a large
    date correction it was told about, opened from that correction's row in
    its Audit Logs.

    Deliberately NOT a door into the contract: assert_can_open_contract still
    refuses Kavachio the record, its terms and its other documents. The
    carrier opens its evidence from the contract's Documents; a broker never
    does. Not behind the switch: it is read-only oversight of corrections
    already made, and while the switch has never been on there are none."""
    if not p.is_platform_admin:
        raise HTTPException(403, "Only Kavachio admins open evidence here. The "
                                 "carrier opens it from the contract's Documents.")
    with SessionLocal() as s:
        doc = _correction_evidence(s, event_id)
        if doc is None:
            raise HTTPException(404, "evidence not found")
        data = storage.resolve_bytes(doc.blob_ref, doc.blob)
        if data is None:
            raise HTTPException(404, "the file for this document is no longer stored")
        return Response(
            content=data, media_type=_media_type(doc.filename),
            headers={"Content-Disposition":
                     _content_disposition(doc.filename or "evidence")})
