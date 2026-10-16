"""
Reinsurance treaties — Phase 26.

26.1  A treaty's structure: its type, its layers, and each reinsurer's share of
      each layer (treaty_types.py says what each type is made of).
26.2  The programmes a treaty protects, on the Phase 1 contract spine: a treaty
      IS an insurer ↔ reinsurer contract, and this hangs the structure and the
      programme links off that row rather than inventing a second record.

AND THE REINSURER'S VIEW OF IT. A reinsurer has no seat in Kavachio — the
contract code has always said so — so it cannot log in to see its line. The
carrier sends one named person at the reinsurer a private link instead. The
link opens nothing on its own: it asks for the one-time code that was emailed
to the same address (esign_otp, the lock the signing link already uses), and
then shows a read-only page with that reinsurer's OWN share and nobody else's.

TWO ROUTERS, AS IN esign_routes
  router         carrier-side, Bearer token, tenant-scoped.
  public_router  /treaty-view/…  NO authentication. The emailed token finds the
                 link; the code proves the reader holds the mailbox; a short
                 session carries them from there.

WHAT CUTS A LINK OFF (_link_state)
  revoked by the carrier · past its expiry · the treaty terminated or replaced
  by a renewal · the contact it was sent to deleted · the reinsurer no longer
  on the treaty. Each is said plainly on the page rather than shown as an error.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
from typing import Any, Optional

from fastapi import (
    APIRouter, Depends, File, Form, Header, HTTPException, Query, Request,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import or_

import esign_otp
import treaty_types as tt
from app_routes import _iso_utc, assert_tenant_owns, resolve_tenant_id
from auth_deps import Principal, current_principal
from db import (
    Contract, ContractDocument, Party, PartyContact, Program,
    ReinsurerAccessEvent, ReinsurerAccessLink, ReinsurerApproval,
    SessionLocal, Tenant, Treaty,
    TreatyLayer, TreatyProgramme, TreatyShare,
)
from settings import SETTINGS

log = logging.getLogger("kavachio.treaty")

router = APIRouter(tags=["reinsurance"])
public_router = APIRouter(prefix="/treaty-view", tags=["reinsurance (public)"])

REINSURER = "insurer_reinsurer"

# Where the treaty's terms may still move. Once both sides have settled them
# the structure is the contract, and changing it would be changing what was
# signed — the same line `actions.edit` draws for the rest of the terms.
EDITABLE_STATES = ("draft", "pending", "changes_requested")
# A treaty in either of these is over. Nothing new is attached to it, and every
# link to it stops opening.
CLOSED_STATES = ("terminated", "superseded")

# A link with no treaty expiry to follow — or one whose treaty has already run
# out — lasts this long from the moment it is sent.
FALLBACK_LINK_DAYS = 30

SESSION_TYPE = "treaty_view_session"


def _cr():
    """contract_routes, imported late: it imports this module's helpers for
    the record, and a top-level import each way would be a cycle."""
    import contract_routes
    return contract_routes


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(v: Any) -> Optional[dt.datetime]:
    if v is None:
        return None
    return v.replace(tzinfo=dt.timezone.utc) if v.tzinfo is None \
        else v.astimezone(dt.timezone.utc)


def _iso(v: Any) -> Optional[str]:
    d = _aware(v)
    return d.isoformat().replace("+00:00", "Z") if d else None


# =============================================================================
#  The structure, read and written
# =============================================================================

def load_structure(s, contract_id: int, *, names: bool = True) -> Optional[dict]:
    """The treaty as treaty_types reads it, with ids — or None if it has none."""
    tr = s.get(Treaty, contract_id)
    if tr is None:
        return None
    layers = (s.query(TreatyLayer)
              .filter(TreatyLayer.contract_id == contract_id)
              .order_by(TreatyLayer.position, TreatyLayer.id).all())
    shares = (s.query(TreatyShare)
              .filter(TreatyShare.contract_id == contract_id)
              .order_by(TreatyShare.id).all())
    party_names: dict[int, str] = {}
    if names and shares:
        ids = {sh.reinsurer_party_id for sh in shares}
        party_names = {p.id: p.legal_name for p in
                       s.query(Party).filter(Party.id.in_(ids)).all()}
    by_layer: dict[int, list[dict]] = {}
    for sh in shares:
        by_layer.setdefault(sh.layer_id, []).append({
            "id": sh.id,
            "reinsurer_party_id": sh.reinsurer_party_id,
            "reinsurer_name": party_names.get(sh.reinsurer_party_id),
            "share_pct": float(sh.share_pct),
            "is_lead": bool(sh.is_lead),
            "slip_reference": sh.slip_reference,
        })
    return {
        "treaty_type": tr.treaty_type,
        "terms": tr.terms or {},
        "layers": [{"id": lay.id, "name": lay.name, "terms": lay.terms or {},
                    "shares": by_layer.get(lay.id, [])} for lay in layers],
        "updated_at": _iso(tr.updated_at),
    }


def save_structure(s, c: Contract, clean: dict, user_id: Optional[int]) -> None:
    """Replace the treaty's structure with `clean` (treaty_types.validate's
    output). Layers and shares are rewritten whole: the form always sends the
    whole structure, and a merge would leave a deleted layer's shares behind."""
    tr = s.get(Treaty, c.id)
    if tr is None:
        tr = Treaty(contract_id=c.id, tenant_id=c.tenant_id)
        s.add(tr)
    tr.treaty_type = clean["treaty_type"]
    tr.terms = clean["terms"]
    tr.updated_by_user_id = user_id
    tr.updated_at = _now()
    s.query(TreatyShare).filter(TreatyShare.contract_id == c.id) \
        .delete(synchronize_session=False)
    s.query(TreatyLayer).filter(TreatyLayer.contract_id == c.id) \
        .delete(synchronize_session=False)
    s.flush()
    for pos, lay in enumerate(clean["layers"]):
        row = TreatyLayer(contract_id=c.id, tenant_id=c.tenant_id,
                          position=pos, name=lay["name"], terms=lay["terms"])
        s.add(row)
        s.flush()
        for sh in lay["shares"]:
            s.add(TreatyShare(
                layer_id=row.id, contract_id=c.id, tenant_id=c.tenant_id,
                reinsurer_party_id=sh["reinsurer_party_id"],
                share_pct=sh["share_pct"], is_lead=sh["is_lead"],
                slip_reference=sh["slip_reference"]))
    s.flush()


def copy_structure(s, old: Contract, new: Contract) -> None:
    """A renewal starts from last year's structure and programmes — the same
    reinsurers on the same layers is the ordinary renewal, and the carrier
    edits what moved rather than re-keying the panel."""
    st = load_structure(s, old.id, names=False)
    if st:
        save_structure(s, new, {
            "treaty_type": st["treaty_type"], "terms": st["terms"],
            "layers": [{"name": lay["name"], "terms": lay["terms"],
                        "shares": [{k: sh[k] for k in
                                    ("reinsurer_party_id", "share_pct",
                                     "is_lead", "slip_reference")}
                                   for sh in lay["shares"]]}
                       for lay in st["layers"]],
        }, None)
    set_programmes(s, new, [r.program_id for r in s.query(TreatyProgramme)
                            .filter(TreatyProgramme.contract_id == old.id)])


def reinsurer_ids(s, tenant_id: Optional[int]) -> set[int]:
    """The parties a carrier may put on a panel: its own reinsurers (and any
    shared directory ones), active or not — a share written before a reinsurer
    was deactivated must still save."""
    q = s.query(Party.id).filter(Party.party_type == "reinsurer").filter(
        or_(Party.tenant_id == tenant_id, Party.scope == "global"))
    return {r[0] for r in q.all()}


def _validate(s, c_or_tenant: Any, raw: dict, *, strict: bool = True) -> dict:
    tenant_id = (c_or_tenant.tenant_id if isinstance(c_or_tenant, Contract)
                 else c_or_tenant)
    try:
        return tt.validate(raw or {}, reinsurer_ids=reinsurer_ids(s, tenant_id),
                           strict=strict)
    except tt.TreatyError as e:
        raise HTTPException(400, e.to_detail())


def validate_for_create(s, tenant_id: int, raw: dict) -> dict:
    """For contract_routes.create_contract: the structure checked before the
    contract row is written, so a bad panel refuses the whole create."""
    return _validate(s, tenant_id, raw)


# ── programmes (26.2) ────────────────────────────────────────────────────────
def programmes(s, c: Contract) -> list[dict]:
    rows = (s.query(TreatyProgramme, Program)
            .join(Program, Program.id == TreatyProgramme.program_id)
            .filter(TreatyProgramme.contract_id == c.id)
            .order_by(Program.name).all())
    out = [{"id": p.id, "name": p.name, "is_home": p.id == c.program_id}
           for _tp, p in rows]
    # The home programme is always covered, row or no row — a treaty raised
    # before this table existed has no rows at all.
    if c.program_id and not any(o["is_home"] for o in out):
        home = s.get(Program, c.program_id)
        if home:
            out.insert(0, {"id": home.id, "name": home.name, "is_home": True})
    out.sort(key=lambda o: (not o["is_home"], (o["name"] or "").lower()))
    return out


def set_programmes(s, c: Contract, program_ids: list[int] | None) -> None:
    """The programmes this treaty protects: the home one, plus `program_ids`.
    Each must be the carrier's own."""
    wanted = {int(x) for x in (program_ids or []) if x not in (None, "")}
    if c.program_id:
        wanted.add(c.program_id)
    if wanted:
        found = {p.id: p for p in
                 s.query(Program).filter(Program.id.in_(wanted)).all()}
        for pid in wanted:
            prog = found.get(pid)
            if prog is None or prog.tenant_id != c.tenant_id:
                raise HTTPException(400, {
                    "message": "One of those programmes is not yours.",
                    "errors": {"program_ids": f"programme {pid} not found"}})
    have = {tp.program_id: tp for tp in s.query(TreatyProgramme)
            .filter(TreatyProgramme.contract_id == c.id).all()}
    for pid, tp in have.items():
        if pid not in wanted:
            s.delete(tp)
    for pid in wanted - set(have):
        s.add(TreatyProgramme(contract_id=c.id, program_id=pid,
                              tenant_id=c.tenant_id))
    s.flush()


def regenerate_wording(s, c: Contract, structure: Optional[dict]) -> bool:
    """Put the treaty's clauses into a WRITTEN wording, replacing the old ones.

    Only a wording written in Kavachio: an uploaded document is what it is.
    Returns whether anything was rewritten, so the caller can re-store the
    clauses the record lists."""
    ws = c.wording_sections if isinstance(c.wording_sections, dict) else None
    if not ws or not ws.get("sections"):
        return False
    merged = tt.merge_sections(ws["sections"], tt.build_sections(structure))
    c.wording_sections = {**ws, "sections": merged}
    _cr()._store_authored_clauses(s, c, merged)
    return True


def record_summary(s, c: Contract) -> Optional[dict]:
    """What the contract record and list carry about the treaty — enough for a
    header, a list column and the summary tab, without the panel itself."""
    if (c.contract_type or "") != REINSURER:
        return None
    st = load_structure(s, c.id, names=False)
    summ = tt.summarise(st) if st else None
    return {
        "has_structure": st is not None,
        "treaty_type": st["treaty_type"] if st else None,
        "type_label": summ["type_label"] if summ else None,
        "headline": summ["headline"] if summ else None,
        "placed_pct": summ["placed_pct"] if summ else None,
        "fully_placed": summ["fully_placed"] if summ else False,
        "programmes": programmes(s, c),
        "approvals": ({k: v for k, v in approvals(s, c, st).items()
                       if k in ("approved", "total", "changes_requested",
                                "all_approved")} if st else None),
    }


def _state(c: Contract) -> str:
    return _cr()._effective_lifecycle(c)


# ── reinsurers' approvals ───────────────────────────────────────────────────
def approvals(s, c: Contract, structure: Optional[dict] = None,
              names: Optional[dict] = None) -> dict:
    """Where every reinsurer on the panel stands on the treaty AS IT IS NOW.

    One row per reinsurer on the panel (and the lead, the contract's
    counterparty). Its latest answer counts only if it was given on the current
    structure; an answer to an earlier version is reported as `stale`.
    """
    st = structure if structure is not None else load_structure(s, c.id, names=False)
    fp = tt.fingerprint(st) if st else None
    order: list[int] = []
    if c.broker_party_id:
        order.append(c.broker_party_id)
    for lay in (st or {}).get("layers") or []:
        for sh in lay.get("shares") or []:
            pid = sh.get("reinsurer_party_id")
            if pid and pid not in order:
                order.append(pid)
    if names is None:
        names = {p.id: p.legal_name for p in
                 s.query(Party).filter(Party.id.in_(order or [0]))}
    latest: dict[int, ReinsurerApproval] = {}
    for row in (s.query(ReinsurerApproval)
                .filter(ReinsurerApproval.contract_id == c.id)
                .order_by(ReinsurerApproval.at.desc(),
                          ReinsurerApproval.id.desc())):
        latest.setdefault(row.party_id, row)
    parties = []
    for pid in order:
        row = latest.get(pid)
        if row is None:
            status = "pending"
        elif row.fingerprint != fp:
            status = "stale"
        else:
            status = row.status
        parties.append({
            "party_id": pid, "party_name": names.get(pid),
            "is_lead": pid == c.broker_party_id,
            "status": status,
            "last_status": row.status if row else None,
            "signer_name": row.signer_name if row else None,
            "signer_title": row.signer_title if row else None,
            "note": row.note if row else None,
            "at": _iso(row.at) if row else None,
        })
    approved = sum(1 for p_ in parties if p_["status"] == "approved")
    return {
        "fingerprint": fp,
        "parties": parties,
        "approved": approved,
        "total": len(parties),
        "changes_requested": sum(1 for p_ in parties
                                 if p_["status"] == "changes_requested"),
        "all_approved": bool(parties) and approved == len(parties),
    }


def _structure_editable(c: Contract, state: Optional[str] = None) -> bool:
    """May the structure still change? Only while the terms move.

    Once a treaty is signed — written here and signed, or uploaded already
    signed — its shares and terms are what the reinsurers put their names to,
    and they do not change on the record. A correction is a renewal or an
    endorsement, as for any signed contract. (Which programmes it protects is
    not a signed term, and stays changeable — see put_treaty_programmes.)
    """
    return (state or _state(c)) in EDITABLE_STATES


def _treaty_contract(s, p: Principal, contract_id: int) -> Contract:
    c = _cr()._contract_access(s, p, contract_id)
    if (c.contract_type or "") != REINSURER:
        raise HTTPException(
            404, "this is not a reinsurance contract, so it has no treaty "
                 "structure")
    return c


# =============================================================================
#  Carrier-side routes
# =============================================================================

@router.get("/treaty-types")
def treaty_types(_p: Principal = Depends(current_principal)):
    """What the treaty form is built from — see treaty_types.describe."""
    return {"types": tt.describe()}


class StructureIn(BaseModel):
    treaty_type: Optional[str] = None
    terms: dict = {}
    layers: list = []
    # Absent = leave the programmes as they are.
    program_ids: Optional[list[int]] = None


@router.post("/treaty/preview")
def treaty_preview(body: StructureIn, p: Principal = Depends(current_principal)):
    """The summary, tower and clauses for a structure that is still being
    typed. Nothing is written. Lenient: a half-filled form previews what it
    has rather than answering with a list of what it has not."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, p)
        clean = _validate(s, tid, body.model_dump(), strict=False)
        errors: dict = {}
        try:
            tt.validate(body.model_dump(), reinsurer_ids=reinsurer_ids(s, tid))
        except tt.TreatyError as e:
            errors = e.errors
        return {"summary": tt.summarise(clean) if clean["treaty_type"] else None,
                "sections": tt.build_sections(clean),
                "errors": errors}


def _treaty_payload(s, c: Contract, p: Principal) -> dict:
    st = load_structure(s, c.id)
    summ = tt.summarise(st) if st else None
    state = _state(c)
    is_carrier = not p.is_broker
    if st:
        for lay in st["layers"]:
            lay["facts"] = tt.layer_facts(st, lay)
            lay["placed_pct"] = tt.placed_pct(lay)
    return {
        "contract_id": c.id,
        "structure": st,
        "summary": summ,
        "programmes": programmes(s, c),
        "lead_reinsurer_id": c.broker_party_id,
        "can_edit": is_carrier and _structure_editable(c, state),
        "can_edit_programmes": is_carrier and state not in CLOSED_STATES,
        "edit_blocked_reason": (
            None if _structure_editable(c, state) else
            "The terms are settled, so the structure is fixed. Renew the "
            "treaty, or record an endorsement, to change it."
            if state not in CLOSED_STATES else
            f"This treaty is {state}."),
    }


@router.get("/contracts/{contract_id}/treaty")
def get_treaty(contract_id: int, p: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        c = _treaty_contract(s, p, contract_id)
        return _treaty_payload(s, c, p)


@router.put("/contracts/{contract_id}/treaty")
def put_treaty(contract_id: int, body: StructureIn,
               p: Principal = Depends(current_principal)):
    """Save the whole structure (and, if sent, the programmes covered).

    The structure is part of the terms, so it moves only while the terms do.
    A wording written in Kavachio has its treaty clauses rewritten in the same
    transaction — the document and the structure never say different things.
    """
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        c = _treaty_contract(s, p, contract_id)
        state = _state(c)
        if not _structure_editable(c, state):
            raise HTTPException(
                409, "the terms of this treaty are settled, so its structure "
                     "cannot change. Renew it, or record an endorsement.")
        clean = _validate(s, c, body.model_dump())
        save_structure(s, c, clean, p.user_id)
        # The starred lead on the first layer is who the treaty is with.
        lead = tt.lead_of(clean)
        if lead is not None and lead != c.broker_party_id:
            c.broker_party_id = lead
        if body.program_ids is not None:
            set_programmes(s, c, body.program_ids)
        else:
            set_programmes(s, c, [r.program_id for r in s.query(TreatyProgramme)
                                  .filter(TreatyProgramme.contract_id == c.id)])
        regenerate_wording(s, c, clean)
        s.commit()
        s.refresh(c)
        return _treaty_payload(s, c, p)


class ProgrammesIn(BaseModel):
    program_ids: list[int] = []


@router.put("/contracts/{contract_id}/treaty/programmes")
def put_treaty_programmes(contract_id: int, body: ProgrammesIn,
                          p: Principal = Depends(current_principal)):
    """Which programmes this treaty protects. Allowed for as long as the
    treaty runs — protecting one more programme mid-term is a placement
    decision, not a change to the signed wording (which does not list them)."""
    _cr()._require_carrier(p)
    with SessionLocal() as s:
        c = _treaty_contract(s, p, contract_id)
        if _state(c) in CLOSED_STATES:
            raise HTTPException(409, f"this treaty is {_state(c)}")
        set_programmes(s, c, body.program_ids)
        s.commit()
        return _treaty_payload(s, c, p)


def _programme_row(s, c: Contract, program_id: int) -> dict:
    summ = tt.summarise(load_structure(s, c.id, names=False))
    party = s.get(Party, c.broker_party_id) if c.broker_party_id else None
    return {
        "contract_id": c.id,
        "name": c.name or f"Treaty {c.id}",
        "lifecycle": _state(c),
        "lead_reinsurer": party.legal_name if party else None,
        "inception_dt": str(c.inception_dt) if c.inception_dt else None,
        "expiry_dt": str(c.expiry_dt) if c.expiry_dt else None,
        "is_home": c.program_id == program_id,
        "summary": summ,
    }


@router.get("/programs/{program_id}/treaties")
def programme_treaties(program_id: int, p: Principal = Depends(current_principal)):
    """The treaties protecting one programme, and the ones that could be
    attached to it."""
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        prog, tid = cr._program_access(s, p, program_id)
        linked = {r.contract_id for r in s.query(TreatyProgramme)
                  .filter(TreatyProgramme.program_id == program_id)}
        treaties = (s.query(Contract)
                    .filter(Contract.tenant_id == tid,
                            Contract.contract_type == REINSURER)
                    .order_by(Contract.id.desc()).all())
        mine = [c for c in treaties
                if c.id in linked or c.program_id == program_id]
        rows = [_programme_row(s, c, program_id) for c in mine]
        live = [r for r in rows if r["lifecycle"] not in CLOSED_STATES]
        attachable = [
            {"contract_id": c.id, "name": c.name or f"Treaty {c.id}",
             "type_label": tt.label(getattr(s.get(Treaty, c.id),
                                            "treaty_type", None)),
             "lifecycle": _state(c)}
            for c in treaties
            if c not in mine and _state(c) not in CLOSED_STATES]
        return {"programme": {"id": prog.id, "name": prog.name},
                "treaties": rows,
                "live_count": len(live),
                "attachable": attachable}


class AttachIn(BaseModel):
    contract_id: int


@router.post("/programs/{program_id}/treaties")
def attach_treaty(program_id: int, body: AttachIn,
                  p: Principal = Depends(current_principal)):
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        cr._program_access(s, p, program_id)
        c = _treaty_contract(s, p, body.contract_id)
        if _state(c) in CLOSED_STATES:
            raise HTTPException(409, f"that treaty is {_state(c)}, so it "
                                     f"protects nothing new")
        if not s.query(TreatyProgramme).filter(
                TreatyProgramme.contract_id == c.id,
                TreatyProgramme.program_id == program_id).first():
            s.add(TreatyProgramme(contract_id=c.id, program_id=program_id,
                                  tenant_id=c.tenant_id))
        s.commit()
    return programme_treaties(program_id, p)


@router.delete("/programs/{program_id}/treaties/{contract_id}")
def detach_treaty(program_id: int, contract_id: int,
                  p: Principal = Depends(current_principal)):
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        cr._program_access(s, p, program_id)
        c = _treaty_contract(s, p, contract_id)
        if c.program_id == program_id:
            raise HTTPException(
                409, "this is the programme the treaty was raised on, so it "
                     "always protects it")
        s.query(TreatyProgramme).filter(
            TreatyProgramme.contract_id == c.id,
            TreatyProgramme.program_id == program_id).delete()
        s.commit()
    return programme_treaties(program_id, p)


@router.get("/treaty-counts")
def treaty_counts(p: Principal = Depends(current_principal)):
    """Live treaties per programme, for the Programmes list — one call for the
    whole page rather than one per row."""
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, p)
        treaties = {c.id: c for c in s.query(Contract).filter(
            Contract.tenant_id == tid, Contract.contract_type == REINSURER)}
        counts: dict[int, int] = {}
        seen: set[tuple[int, int]] = set()
        pairs = [(tp.program_id, tp.contract_id) for tp in
                 s.query(TreatyProgramme).filter(
                     TreatyProgramme.contract_id.in_(list(treaties) or [0]))]
        pairs += [(c.program_id, c.id) for c in treaties.values() if c.program_id]
        for pid, cid in pairs:
            if (pid, cid) in seen or _state(treaties[cid]) in CLOSED_STATES:
                continue
            seen.add((pid, cid))
            counts[pid] = counts.get(pid, 0) + 1
        return {"counts": {str(k): v for k, v in counts.items()}}


@router.get("/reinsurers/{party_id}/treaties")
def reinsurer_treaties(party_id: int, p: Principal = Depends(current_principal)):
    """Every treaty this reinsurer is on — as lead (the contract's
    counterparty) or holding a share of any layer — with its lines."""
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        party = s.get(Party, party_id)
        if party is None:
            raise HTTPException(404, "party not found")
        tid = resolve_tenant_id(s, p)
        if party.tenant_id not in (None, tid):
            raise HTTPException(404, "party not found")
        ids = {r[0] for r in s.query(TreatyShare.contract_id)
               .filter(TreatyShare.reinsurer_party_id == party_id)}
        ids |= {r[0] for r in s.query(Contract.id).filter(
            Contract.contract_type == REINSURER,
            Contract.broker_party_id == party_id)}
        rows = (s.query(Contract).filter(Contract.id.in_(ids or {0}),
                                         Contract.tenant_id == tid)
                .order_by(Contract.id.desc()).all())
        out = []
        for c in rows:
            st = load_structure(s, c.id, names=False)
            lines = [{"layer": lay["name"], "share_pct": sh["share_pct"],
                      "is_lead": sh["is_lead"]}
                     for lay in (st or {}).get("layers", [])
                     for sh in lay["shares"]
                     if sh["reinsurer_party_id"] == party_id]
            summ = tt.summarise(st) if st else None
            out.append({
                "contract_id": c.id, "name": c.name or f"Treaty {c.id}",
                "lifecycle": _state(c),
                "is_lead_reinsurer": c.broker_party_id == party_id,
                "type_label": (summ or {}).get("type_label"),
                "headline": (summ or {}).get("headline"),
                "inception_dt": str(c.inception_dt) if c.inception_dt else None,
                "expiry_dt": str(c.expiry_dt) if c.expiry_dt else None,
                "lines": lines,
            })
        return {"treaties": out}


@router.post("/treaties/read")
async def read_signed_treaty(file: UploadFile = File(...),
                             p: Principal = Depends(current_principal)):
    """Read a signed treaty and say what it says — name, term, type, layers,
    and who holds what — for the upload form to be filled with. Nothing is
    saved: the carrier checks it, then uploads. See treaty_reader."""
    import treaty_reader
    cr = _cr()
    cr._require_carrier(p)
    data = await file.read()
    if not data or not file.filename:
        raise HTTPException(400, "attach the signed treaty first")
    try:
        text = await run_in_threadpool(cr._parse_to_text, data, file.filename)
    except Exception as e:  # noqa: BLE001 — an unreadable file is the user's to fix
        log.warning("treaty read: could not parse %s: %s", file.filename, e)
        raise HTTPException(400, "That file could not be read. Upload the "
                                 "treaty as a PDF or Word document.")
    if not (text or "").strip():
        raise HTTPException(400, "No text could be read from that file — it may "
                                 "be a scan without text. Fill the details in "
                                 "by hand.")
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, p)
        parties = [(pt.id, pt.legal_name) for pt in s.query(Party).filter(
            Party.party_type == "reinsurer",
            or_(Party.tenant_id == tid, Party.scope == "global"))]
    try:
        return await run_in_threadpool(treaty_reader.read_treaty, text, parties)
    except Exception as e:  # noqa: BLE001 — the form still works by hand
        log.warning("treaty read failed: %s", e)
        raise HTTPException(502, "The treaty could not be read just now. Try "
                                 "again, or fill the details in by hand.")


@router.post("/treaties/upload")
async def upload_signed_treaty(
    file: UploadFile = File(...),
    program_id: int = Form(...),
    name: str = Form(...),
    inception_dt: str = Form(...),
    expiry_dt: str = Form(...),
    year_of_account: str = Form(...),
    notice_period_days: int = Form(...),
    executed_date: Optional[str] = Form(default=None),
    # The structure, as JSON: treaty_types' shape. Its starred lead on the
    # first layer is who the treaty is with.
    treaty: str = Form(...),
    # Other programmes it protects, as a JSON list of ids.
    program_ids: str = Form(default="[]"),
    # What /treaties/read made of the document, kept on the contract so what
    # was read and what the carrier confirmed can be told apart later.
    reading: Optional[str] = Form(default=None),
    p: Principal = Depends(current_principal),
):
    """Bring in a treaty that was ALREADY SIGNED, outside Kavachio.

    Nothing to negotiate and nobody to sign: the reinsurers signed the slip on
    paper. So this is the uploaded road every contract already has, in one
    request — the contract is recorded with its structure, the signed copy is
    attached as the executed wording, and it is accepted (by the carrier admin,
    which puts it in force) or sent up to them (by a carrier user).

    The file is NOT read for rules. Rules are checks on a bordereau's columns,
    and a treaty is not checked against bordereaux.
    """
    import json
    cr = _cr()
    cr._require_carrier(p)
    data = await file.read()
    if not data or not file.filename:
        raise HTTPException(400, {"message": "Attach the signed treaty.",
                                  "errors": {"file": "required"}})
    try:
        structure = json.loads(treaty or "{}")
        extra = [int(x) for x in json.loads(program_ids or "[]")]
    except (ValueError, TypeError):
        raise HTTPException(400, "the treaty structure could not be read")
    lead = tt.lead_of(structure)
    if lead is None:
        raise HTTPException(400, {
            "message": "Star the lead reinsurer on the first layer — the "
                       "treaty is with them.",
            "errors": {"layers.0.shares": "no lead"}})

    body = cr.ContractIn(
        program_id=program_id, contract_type=REINSURER, name=name,
        counterparty_party_id=lead, inception_dt=inception_dt,
        expiry_dt=expiry_dt, year_of_account=year_of_account,
        notice_period_days=notice_period_days, executed_date=executed_date,
        treaty=structure, program_ids=extra, create_as="draft")
    # Every check the create flow makes — the type's required terms, the
    # reinsurer being a reinsurer, the panel adding up — made here too.
    rec = await run_in_threadpool(cr.create_contract, body, p)
    cid = rec["id"]

    import hashlib
    import storage
    with SessionLocal() as s:
        c = s.get(Contract, cid)
        blob_ref, blob = await run_in_threadpool(
            storage.store_or_keep, "contract-documents", c.tenant_id,
            file.filename, data, file.content_type)
        s.add(ContractDocument(
            tenant_id=c.tenant_id, contract_id=c.id, kind="contract",
            filename=file.filename, blob_ref=blob_ref, blob=blob,
            fingerprint=hashlib.sha256(data).hexdigest(),
            is_executed_copy=True, is_active=True,
            uploaded_by_user_id=p.user_id))
        c.filename = file.filename
        if reading:
            try:
                c.extracted = {**(c.extracted or {}),
                               "treaty_reading": json.loads(reading)}
            except ValueError:
                pass
        s.commit()
    # The uploaded road from here: the carrier admin's acceptance puts it in
    # force; a carrier user's sends it up to them.
    return await run_in_threadpool(
        cr.submit_for_approval, cid,
        cr.Note(note="Signed treaty uploaded."), p)


def _protected_bordereaux(s, tid: int, programme_ids: set[int]) -> dict:
    """This month's inward bordereaux on treaty-protected programmes.

    A ceded (reinsurance) bordereau is built FROM these: the business written
    on a protected programme is what is ceded. Kavachio does not produce the
    ceded file yet, so this counts the inward side only, with the same rules
    the File Submissions list uses — one row per submission (its latest
    version), by validation result.
    """
    from sqlalchemy import func as _f
    import validation_outcome as vo
    from db import DirectFormat, LandingRecord, OutputExport
    from exception_tally import current_export_ids
    start = dt.datetime.now(dt.timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    out = {"since": start.date().isoformat(), "total": 0, "clean": 0,
           "with_exceptions": 0, "not_validated": 0, "programmes": 0}
    if not programme_ids:
        return out
    prog = _f.coalesce(OutputExport.program_id, DirectFormat.program_id)
    rows = (s.query(OutputExport.status, OutputExport.exception_count, prog)
            .select_from(LandingRecord)
            .join(DirectFormat, LandingRecord.format_id == DirectFormat.id)
            .join(OutputExport, LandingRecord.output_export_id == OutputExport.id)
            .filter(LandingRecord.tenant_id == tid,
                    LandingRecord.created_at >= start,
                    prog.in_(programme_ids),
                    OutputExport.id.in_(current_export_ids(s, tenant_id=tid) or {-1}))
            .all())
    seen = set()
    for status, exc, pid in rows:
        out["total"] += 1
        seen.add(pid)
        if status == vo.NOT_VALIDATED:
            out["not_validated"] += 1
        elif status == vo.CLEAN or not exc:
            out["clean"] += 1
        else:
            out["with_exceptions"] += 1
    out["programmes"] = len(seen)
    return out


@router.get("/reinsurance/dashboard")
def reinsurance_dashboard(p: Principal = Depends(current_principal)):
    """The carrier's reinsurance at a glance, for the Home dashboard.

    Built only from what Kavachio actually holds — treaties, their placement,
    their signatures and the reinsurers' private links. Ceded bordereaux are
    not produced here yet, so nothing on this card counts them.
    """
    cr = _cr()
    cr._require_carrier(p)
    today = dt.date.today()
    soon = today + dt.timedelta(days=90)
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, p)
        rows = (s.query(Contract)
                .filter(Contract.tenant_id == tid,
                        Contract.contract_type == REINSURER)
                .order_by(Contract.id.desc()).all())
        party_names = {pt.id: pt.legal_name for pt in s.query(Party).filter(
            Party.id.in_({c.broker_party_id for c in rows if c.broker_party_id}
                         or {0}))}
        counts = {"active": 0, "settling": 0, "awaiting_signature": 0,
                  "under_placed": 0, "expiring_soon": 0, "no_structure": 0}
        actions = []
        expiring = []
        live_ids = []
        for c in rows:
            state = _state(c)
            if state in CLOSED_STATES or state == "expired":
                continue
            live_ids.append(c.id)
            st = load_structure(s, c.id, names=False)
            summ = tt.summarise(st) if st else None
            unsigned = cr._unsigned_sides(cr._signatures(s, c.id))
            row = {"contract_id": c.id, "name": c.name or f"Treaty {c.id}",
                   "lifecycle": state,
                   "lead_reinsurer": party_names.get(c.broker_party_id),
                   "type_label": (summ or {}).get("type_label"),
                   "headline": (summ or {}).get("headline")}

            def act(reason: str, tab: str, tone: str):
                actions.append({**row, "reason": reason, "tab": tab,
                                "tone": tone})

            if state == "active":
                counts["active"] += 1
                if c.expiry_dt and c.expiry_dt <= soon:
                    counts["expiring_soon"] += 1
                    renewed = s.query(Contract.id).filter(
                        Contract.renews_contract_id == c.id).first()
                    expiring.append({
                        "contract_id": c.id, "name": row["name"],
                        "lead_reinsurer": row["lead_reinsurer"],
                        "type_label": row["type_label"],
                        "expiry_dt": str(c.expiry_dt),
                        "days_left": (c.expiry_dt - today).days,
                        "renewal_id": renewed[0] if renewed else None})
                    act(f"Expires {c.expiry_dt:%d %b %Y} — renew or let it run off",
                        "summary", "warn")
            elif state in ("agreed", "signed"):
                counts["awaiting_signature"] += 1
                act("Waiting on the carrier's signature" if "carrier" in unsigned
                    else "Record the reinsurer's signature against the signed copy",
                    "summary", "warn")
            else:
                counts["settling"] += 1
            if not st:
                counts["no_structure"] += 1
                act("No treaty structure yet", "treaty", "crit")
            elif not summ["fully_placed"]:
                counts["under_placed"] += 1
                act(f"{summ['placed_pct']:g}% placed — a layer has no reinsurer "
                    f"for the rest", "treaty", "warn")

        links = (s.query(ReinsurerAccessLink)
                 .filter(ReinsurerAccessLink.contract_id.in_(live_ids or [0]))
                 .all())
        open_links = [lk for lk in links if _link_state(s, lk)[0] == "active"]
        week_ago = _now() - dt.timedelta(days=7)
        names = {c.id: (c.name or f"Treaty {c.id}") for c in rows}
        by_link = {lk.id: lk for lk in links}
        lp_names = {pt.id: pt.legal_name for pt in s.query(Party).filter(
            Party.id.in_({lk.party_id for lk in links} or {0}))}
        recent = (s.query(ReinsurerAccessEvent)
                  .filter(ReinsurerAccessEvent.tenant_id == tid,
                          ReinsurerAccessEvent.type.in_(
                              ("viewed", "downloaded", "locked", "sent")))
                  .order_by(ReinsurerAccessEvent.at.desc()).limit(6).all())
        progs = s.query(Program.id).filter(Program.tenant_id == tid,
                                           Program.is_app_managed.is_(True)).all()
        protected = {r.program_id for r in s.query(TreatyProgramme)
                     .filter(TreatyProgramme.contract_id.in_(live_ids or [0]))}
        protected |= {c.program_id for c in rows
                      if c.id in live_ids and c.program_id}
        order = {"crit": 0, "warn": 1}
        actions.sort(key=lambda a: order.get(a["tone"], 2))
        bdx = _protected_bordereaux(s, tid, protected)
        return {
            "live": len(live_ids),
            "counts": counts,
            "links": {
                "active": len(open_links),
                "opened": sum(1 for lk in open_links if lk.first_opened_at),
                "never_opened": sum(1 for lk in open_links
                                    if lk.sent_at and not lk.first_opened_at),
                "viewed_this_week": sum(
                    1 for lk in open_links
                    if lk.last_viewed_at and _aware(lk.last_viewed_at) >= week_ago),
            },
            "programmes": {"total": len(progs),
                           "protected": len(protected & {r[0] for r in progs})},
            # Bordereaux received this month on programmes a treaty protects —
            # the business a reinsurance bordereau will be built from.
            "bordereaux": bdx,
            # Treaties running out within 90 days, soonest first, and whether
            # a renewal has been raised for each — the dashboard's one list.
            "expiring": sorted(expiring, key=lambda e: e["days_left"]),
            "actions": actions[:8],
            "actions_total": len(actions),
            "recent_activity": [{
                "type": e.type, "at": _iso(e.at), "contract_id": e.contract_id,
                "treaty": names.get(e.contract_id),
                "who": by_link[e.link_id].name if e.link_id in by_link else None,
                "party": (lp_names.get(by_link[e.link_id].party_id)
                          if e.link_id in by_link else None),
            } for e in recent],
        }


# =============================================================================
#  The reinsurer's private link — carrier side
# =============================================================================

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _view_link(token: str) -> str:
    import os
    base = (os.getenv("APP_BASE_URL", "http://localhost:5173") or "").rstrip("/")
    return f"{base}/treaty-view?token={token}"


def _link_expiry(c: Contract) -> dt.datetime:
    """The link lasts as long as the treaty does. A treaty with no expiry, or
    one already past it, gets FALLBACK_LINK_DAYS from now."""
    if c.expiry_dt:
        end = dt.datetime.combine(c.expiry_dt, dt.time(23, 59, 59),
                                  tzinfo=dt.timezone.utc)
        if end > _now():
            return end
    return _now() + dt.timedelta(days=FALLBACK_LINK_DAYS)


def _panel_party_ids(s, c: Contract) -> set[int]:
    ids = {r[0] for r in s.query(TreatyShare.reinsurer_party_id)
           .filter(TreatyShare.contract_id == c.id)}
    if c.broker_party_id:
        ids.add(c.broker_party_id)
    return ids


def _link_state(s, link: ReinsurerAccessLink,
                c: Optional[Contract] = None) -> tuple[str, Optional[str], Optional[str]]:
    """(status, title, sentence) — status is `active` or why it is not.

    The title and sentence are what the reinsurer reads on the page in place
    of the treaty, so they say what happened rather than "invalid link"."""
    c = c or s.get(Contract, link.contract_id)
    carrier = _carrier_name(s, c)
    if link.revoked_at:
        return ("revoked", "This link has been withdrawn",
                f"{carrier} withdrew access to this treaty on "
                f"{_aware(link.revoked_at):%d %b %Y}. Ask them if you still "
                f"need to see it.")
    if c is None or _state(c) == "terminated":
        return ("closed", "This treaty has been terminated",
                f"{carrier} ended this treaty, so it can no longer be viewed "
                f"here.")
    if _state(c) == "superseded":
        return ("closed", "This treaty has been renewed",
                f"{carrier} replaced this treaty with a renewal. If you are on "
                f"the new one, they will send you a link to it.")
    if _aware(link.expires_at) < _now():
        return ("expired", "This link has expired",
                f"It was valid until {_aware(link.expires_at):%d %b %Y}. Ask "
                f"{carrier} for a new one.")
    if link.contact_id is not None and s.get(PartyContact, link.contact_id) is None:
        return ("closed", "You are no longer a contact for this treaty",
                f"{carrier} no longer lists you as a contact, so this link "
                f"has stopped working.")
    if link.party_id not in _panel_party_ids(s, c):
        return ("closed", "You are no longer on this treaty",
                f"Your organisation no longer holds a share of this treaty "
                f"with {carrier}.")
    return ("active", None, None)


def _carrier_name(s, c: Optional[Contract]) -> str:
    t = s.get(Tenant, c.tenant_id) if c and c.tenant_id else None
    return (getattr(t, "legal_name", None) or getattr(t, "tenant_name", None)
            or "The carrier")


def _event(s, link: ReinsurerAccessLink, type_: str, *,
           actor: Optional[str] = None, request: Request | None = None,
           detail: dict | None = None) -> None:
    ip = None
    agent = None
    if request is not None:
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        ip = fwd or (request.client.host if request.client else None)
        agent = request.headers.get("user-agent")
    s.add(ReinsurerAccessEvent(
        link_id=link.id, contract_id=link.contract_id, tenant_id=link.tenant_id,
        type=type_, actor=actor, ip=ip, agent=agent, detail=detail, at=_now()))


def _link_json(s, link: ReinsurerAccessLink, c: Contract,
               party_names: dict[int, str]) -> dict:
    status, title, _ = _link_state(s, link, c)
    locked = esign_otp.is_locked(link.otp_locked_until)
    return {
        "id": link.id, "party_id": link.party_id,
        "party_name": party_names.get(link.party_id),
        "contact_id": link.contact_id, "name": link.name, "email": link.email,
        "status": status, "status_title": title,
        "locked": locked,
        "sent_at": _iso(link.sent_at), "first_opened_at": _iso(link.first_opened_at),
        "last_viewed_at": _iso(link.last_viewed_at),
        "expires_at": _iso(link.expires_at), "revoked_at": _iso(link.revoked_at),
        # The last DAY it works, as a date. The timestamp is the end of that
        # day in UTC, which a browser east of Greenwich would show as the next
        # morning — "works until 1 Jan" for a treaty that ends on 31 Dec.
        "expires_on": _aware(link.expires_at).date().isoformat(),
    }


def _issue(s, link: ReinsurerAccessLink, c: Contract, p: Principal) -> tuple[str, str]:
    """A fresh token and code on `link`. Any earlier URL for it stops working —
    a resend is a new link, so a copy forwarded before it is dead."""
    token = secrets.token_urlsafe(32)
    code = esign_otp.generate_code()
    link.token_hash = _token_hash(token)
    link.expires_at = _link_expiry(c)
    link.revoked_at = None
    link.revoked_by_user_id = None
    link.otp_hash = esign_otp.hash_code(code)
    link.otp_expires = link.expires_at
    link.otp_attempts = 0
    link.otp_locked_until = None
    link.otp_sent_count = 1
    link.otp_last_sent_at = _now()
    link.sent_at = _now()
    link.sent_by_user_id = p.user_id
    return token, code


def _send_link_email(s, link: ReinsurerAccessLink, c: Contract,
                     token: str, code: str) -> bool:
    import treaty_email
    carrier = _carrier_name(s, c)
    party = s.get(Party, link.party_id)
    summ = tt.summarise(load_structure(s, c.id, names=False))
    term = (f"{c.inception_dt:%d %b %Y} to {c.expiry_dt:%d %b %Y}"
            if c.inception_dt and c.expiry_dt else "")
    html = treaty_email.share_email(
        link=_view_link(token), name=link.name, carrier=carrier,
        reinsurer=getattr(party, "legal_name", "") or "",
        treaty=c.name or f"Treaty {c.id}",
        type_label=(summ or {}).get("type_label") or "Reinsurance treaty",
        term=term, expires=f"{_aware(link.expires_at):%d %b %Y}", otp=code)
    return treaty_email.safe_send(
        link.email, f"{carrier} shared {c.name or 'a treaty'} with you", html,
        text=(f"{carrier} has shared a treaty with you on Kavachio. Open "
              f"{_view_link(token)} and enter the code {code}."))


@router.get("/contracts/{contract_id}/reinsurer-access")
def reinsurer_access(contract_id: int, p: Principal = Depends(current_principal)):
    """Who this treaty has been shared with, everything that happened on those
    links, and who it could be shared with next."""
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        c = _treaty_contract(s, p, contract_id)
        links = (s.query(ReinsurerAccessLink)
                 .filter(ReinsurerAccessLink.contract_id == c.id)
                 .order_by(ReinsurerAccessLink.id.desc()).all())
        panel = _panel_party_ids(s, c)
        parties = {pt.id: pt for pt in s.query(Party)
                   .filter(Party.id.in_(panel | {lk.party_id for lk in links}
                                        or {0})).all()}
        names = {k: v.legal_name for k, v in parties.items()}
        contacts = (s.query(PartyContact)
                    .filter(PartyContact.party_id.in_(panel or {0}))
                    .order_by(PartyContact.full_name).all())
        events = (s.query(ReinsurerAccessEvent)
                  .filter(ReinsurerAccessEvent.contract_id == c.id)
                  .order_by(ReinsurerAccessEvent.at.desc(),
                            ReinsurerAccessEvent.id.desc())
                  .limit(300).all())
        by_link = {lk.id: lk for lk in links}
        has_structure = s.get(Treaty, c.id) is not None
        state = _state(c)
        why_not = None
        if state in CLOSED_STATES:
            why_not = f"This treaty is {state}, so it cannot be shared."
        elif not has_structure:
            why_not = ("Set up the treaty structure first — the link shows the "
                       "reinsurer their share of it.")
        elif not cr._speaks_for_carrier(p):
            why_not = ("Only your carrier admin can share a treaty outside "
                       "Kavachio.")
        return {
            "approvals": approvals(s, c, names=names),
            "can_share": why_not is None,
            "why_not": why_not,
            "links": [_link_json(s, lk, c, names) for lk in links],
            "events": [{
                "id": e.id, "type": e.type, "at": _iso(e.at),
                "link_id": e.link_id,
                "who": (by_link[e.link_id].name if e.link_id in by_link else None),
                "party_name": (names.get(by_link[e.link_id].party_id)
                               if e.link_id in by_link else None),
                "actor": e.actor, "ip": e.ip, "detail": e.detail,
            } for e in events],
            "contacts": [{
                "party_id": pid, "party_name": names.get(pid),
                "is_lead_reinsurer": pid == c.broker_party_id,
                "contacts": [{"id": ct_.id, "name": ct_.full_name,
                              "title": ct_.title, "email": ct_.email}
                             for ct_ in contacts if ct_.party_id == pid],
            } for pid in sorted(panel, key=lambda x: (names.get(x) or "").lower())],
        }


class ShareIn(BaseModel):
    contact_ids: list[int]


@router.post("/contracts/{contract_id}/reinsurer-access")
def share_with_reinsurer(contract_id: int, body: ShareIn,
                         p: Principal = Depends(current_principal)):
    """Send each named contact their own private link.

    A contact who already has a link is sent a fresh one — the old URL stops
    working, so there is only ever one way in per person."""
    cr = _cr()
    cr._require_carrier(p)
    cr._assert_speaks_for_carrier(p, "share a treaty outside Kavachio")
    if not body.contact_ids:
        raise HTTPException(400, "pick at least one person to share it with")
    results = []
    with SessionLocal() as s:
        c = _treaty_contract(s, p, contract_id)
        if _state(c) in CLOSED_STATES:
            raise HTTPException(409, f"this treaty is {_state(c)}")
        if s.get(Treaty, c.id) is None:
            raise HTTPException(409, "set up the treaty structure before "
                                     "sharing it")
        panel = _panel_party_ids(s, c)
        sends = []
        for cid in dict.fromkeys(body.contact_ids):
            contact = s.get(PartyContact, cid)
            if contact is None or contact.party_id not in panel:
                raise HTTPException(400, "one of those people is not a contact "
                                         "at a reinsurer on this treaty")
            if not (contact.email or "").strip():
                raise HTTPException(400, f"{contact.full_name} has no email "
                                         f"address to send the link to")
            link = (s.query(ReinsurerAccessLink)
                    .filter(ReinsurerAccessLink.contract_id == c.id,
                            ReinsurerAccessLink.contact_id == contact.id)
                    .order_by(ReinsurerAccessLink.id.desc()).first())
            if link is None:
                link = ReinsurerAccessLink(
                    contract_id=c.id, tenant_id=c.tenant_id,
                    party_id=contact.party_id, contact_id=contact.id,
                    name=contact.full_name or contact.email,
                    email=contact.email.strip(),
                    token_hash=secrets.token_hex(16),  # replaced by _issue
                    expires_at=_link_expiry(c))
                s.add(link)
                s.flush()
            # The contact's details as they are NOW — a corrected email
            # address must be where the new link goes.
            link.name = contact.full_name or contact.email
            link.email = contact.email.strip()
            token, code = _issue(s, link, c, p)
            _event(s, link, "sent", actor=cr._principal_email_for(s, p))
            sends.append((link, token, code))
        s.commit()
        for link, token, code in sends:
            ok = _send_link_email(s, link, c, token, code)
            results.append({"link_id": link.id, "name": link.name,
                            "email": link.email, "emailed": ok})
    return {"sent": results, **reinsurer_access(contract_id, p)}


@router.post("/contracts/{contract_id}/reinsurer-access/{link_id}/resend")
def resend_link(contract_id: int, link_id: int,
                p: Principal = Depends(current_principal)):
    cr = _cr()
    cr._require_carrier(p)
    cr._assert_speaks_for_carrier(p, "share a treaty outside Kavachio")
    with SessionLocal() as s:
        c = _treaty_contract(s, p, contract_id)
        link = s.get(ReinsurerAccessLink, link_id)
        if link is None or link.contract_id != c.id:
            raise HTTPException(404, "link not found")
        if _state(c) in CLOSED_STATES:
            raise HTTPException(409, f"this treaty is {_state(c)}")
        if link.contact_id is not None and s.get(PartyContact, link.contact_id) is None:
            raise HTTPException(409, "that contact has been removed from the "
                                     "reinsurer, so there is nobody to send it to")
        if link.party_id not in _panel_party_ids(s, c):
            raise HTTPException(409, "that reinsurer no longer holds a share "
                                     "of this treaty")
        token, code = _issue(s, link, c, p)
        _event(s, link, "sent", actor=cr._principal_email_for(s, p),
               detail={"resend": True})
        s.commit()
        ok = _send_link_email(s, link, c, token, code)
    return {"emailed": ok, **reinsurer_access(contract_id, p)}


@router.post("/contracts/{contract_id}/reinsurer-access/{link_id}/revoke")
def revoke_link(contract_id: int, link_id: int,
                p: Principal = Depends(current_principal)):
    cr = _cr()
    cr._require_carrier(p)
    with SessionLocal() as s:
        c = _treaty_contract(s, p, contract_id)
        link = s.get(ReinsurerAccessLink, link_id)
        if link is None or link.contract_id != c.id:
            raise HTTPException(404, "link not found")
        if not link.revoked_at:
            link.revoked_at = _now()
            link.revoked_by_user_id = p.user_id
            _event(s, link, "revoked", actor=cr._principal_email_for(s, p))
            s.commit()
    return reinsurer_access(contract_id, p)


# =============================================================================
#  The reinsurer's private link — public side
# =============================================================================

def _mint_session(link_id: int, token: str) -> str:
    now = _now()
    return jwt.encode({
        "sub": str(link_id), "lnk": esign_otp.token_fingerprint(token),
        "type": SESSION_TYPE, "iat": now,
        "exp": now + dt.timedelta(minutes=esign_otp.SESSION_MINUTES),
        "iss": SETTINGS.JWT_ISSUER,
    }, SETTINGS.JWT_SECRET, algorithm=SETTINGS.JWT_ALG)


def _read_session(session: str, token: str) -> Optional[int]:
    """The link id this session unlocks, or None. Same rules as
    esign_otp.read_session — bound to the link it was opened with, and a
    signing session is not accepted here (different `type`)."""
    if not session:
        return None
    try:
        claims = jwt.decode(session, SETTINGS.JWT_SECRET,
                            algorithms=[SETTINGS.JWT_ALG],
                            issuer=SETTINGS.JWT_ISSUER)
    except JWTError:
        return None
    if claims.get("type") != SESSION_TYPE:
        return None
    if claims.get("lnk") != esign_otp.token_fingerprint(token):
        return None
    try:
        return int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        return None


_DEAD = ("This link is not valid. Check you opened the whole link from the "
         "email, or ask the sender for a new one.")


def _by_token(s, token: str) -> ReinsurerAccessLink:
    """Find the link. An unknown token is a plain 404 — nothing about it is
    worth telling a guesser. A KNOWN link that has been closed is answered by
    the caller with the reason, which only somebody holding it can see."""
    if not token or len(token) < 20:
        raise HTTPException(404, _DEAD)
    link = (s.query(ReinsurerAccessLink)
            .filter(ReinsurerAccessLink.token_hash == _token_hash(token)).first())
    if link is None:
        raise HTTPException(404, _DEAD)
    return link


def _closed_or_none(s, link: ReinsurerAccessLink, c: Contract) -> Optional[dict]:
    status, title, sentence = _link_state(s, link, c)
    if status == "active":
        return None
    return {"closed": True, "status": status, "title": title, "message": sentence}


def _require_open(s, token: str, session: str) -> tuple[ReinsurerAccessLink, Contract]:
    link = _by_token(s, token)
    c = s.get(Contract, link.contract_id)
    closed = _closed_or_none(s, link, c)
    if closed:
        raise HTTPException(410, closed["message"])
    if _read_session(session, token) != link.id:
        raise HTTPException(401, "Enter the code from your email to open this "
                                 "treaty.")
    return link, c


def _session_value(header: str, query: str) -> str:
    return (header or query or "").strip()


def _public_view(s, link: ReinsurerAccessLink, c: Contract) -> dict:
    """The treaty as ONE reinsurer may see it: the structure, every layer's
    total placement, and their own lines — never another reinsurer's name or
    share."""
    st = load_structure(s, c.id, names=False) or {"treaty_type": None,
                                                   "terms": {}, "layers": []}
    summ = tt.summarise(st) if st.get("treaty_type") else None
    party = s.get(Party, link.party_id)
    layers = []
    for i, lay in enumerate(st["layers"]):
        mine = next((sh for sh in lay["shares"]
                     if sh["reinsurer_party_id"] == link.party_id), None)
        layers.append({
            "index": i, "name": lay["name"],
            "facts": tt.layer_facts(st, lay),
            "placed_pct": tt.placed_pct(lay),
            "your_share": ({"share_pct": mine["share_pct"],
                            "is_lead": mine["is_lead"],
                            "slip_reference": mine["slip_reference"]}
                           if mine else None),
        })
    if summ:
        # Facts about the market are the carrier's to share, not the page's:
        # the reinsurance broker and placement reference stay, the panel does
        # not — summarise never names a reinsurer.
        summ = {k: v for k, v in summ.items() if k != "warnings"}
    docs = (s.query(ContractDocument)
            .filter(ContractDocument.contract_id == c.id,
                    ContractDocument.kind == "contract",
                    ContractDocument.is_active.is_(True))
            .order_by(ContractDocument.created_at.desc()).all())
    written = bool((c.wording_sections or {}).get("sections")) \
        if isinstance(c.wording_sections, dict) else False
    lifecycle = _state(c)
    return {
        "closed": False, "locked": False,
        "viewer": {"name": link.name, "email": link.email,
                   "organisation": getattr(party, "legal_name", None)},
        "carrier": _carrier_name(s, c),
        "treaty": {
            "name": c.name or f"Treaty {c.id}",
            "inception_dt": str(c.inception_dt) if c.inception_dt else None,
            "expiry_dt": str(c.expiry_dt) if c.expiry_dt else None,
            "year_of_account": c.year_of_account,
            "lifecycle": lifecycle,
            "programmes": [pr["name"] for pr in programmes(s, c)],
        },
        "summary": summ,
        "layers": layers,
        "documents": [{"id": d.id, "filename": d.filename,
                       "is_executed_copy": bool(d.is_executed_copy),
                       "uploaded_at": _iso_utc(d.created_at)}
                      for d in docs if d.blob_ref or d.blob],
        # A treaty written in Kavachio and not yet signed has no document row;
        # its wording is composed on demand, the same file the carrier sees.
        "wording_pdf": written and not docs,
        # This reinsurer's own answer, and whether it can give one now.
        "approval": _my_approval(s, c, link, st),
        "expires_at": _iso(link.expires_at),
        "expires_on": _aware(link.expires_at).date().isoformat(),
    }


def _my_approval(s, c: Contract, link: ReinsurerAccessLink,
                 st: Optional[dict]) -> dict:
    mine = next((p_ for p_ in approvals(s, c, st if st and st.get("treaty_type")
                                        else None)["parties"]
                 if p_["party_id"] == link.party_id), None)
    state = _state(c)
    has_structure = bool(st and st.get("treaty_type"))
    return {
        **(mine or {"status": "pending"}),
        # Answerable whenever there is a structure to answer about and the
        # treaty has not been ended or replaced. An EXPIRED treaty still is:
        # its lines are confirmed and queried long after the period ends, while
        # its claims run off.
        "can_answer": has_structure and state not in CLOSED_STATES,
        # A change can be asked for only while the terms can still change. A
        # SIGNED treaty is fixed — its reinsurers can approve it, not reopen it.
        "can_request_changes": has_structure and state in EDITABLE_STATES,
        # Why not, when not — said on the page instead of hiding the buttons.
        "why_not": (None if has_structure and state not in CLOSED_STATES
                    else "The treaty's structure has not been set out yet."
                    if not has_structure else
                    f"This treaty has been {state}, so it can no longer be "
                    f"approved."),
        # Once the terms are settled an approval confirms them; before, it is
        # what settles them.
        "settles_terms": state == "draft",
    }


class ApproveIn(BaseModel):
    signer_name: str
    signer_title: Optional[str] = None


class ChangeIn(BaseModel):
    note: str


def _answer(token: str, session: str, request: Request, status: str,
            *, signer_name: Optional[str] = None,
            signer_title: Optional[str] = None,
            note: Optional[str] = None) -> dict:
    cr = _cr()
    with SessionLocal() as s:
        link, c = _require_open(s, token, session)
        st = load_structure(s, c.id, names=False)
        if not st:
            raise HTTPException(409, "There is no treaty structure to approve yet.")
        state = _state(c)
        ip = None
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        ip = fwd or (request.client.host if request.client else None)
        s.add(ReinsurerApproval(
            contract_id=c.id, tenant_id=c.tenant_id, party_id=link.party_id,
            link_id=link.id, status=status, signer_name=signer_name,
            signer_title=signer_title, signer_email=link.email, note=note,
            fingerprint=tt.fingerprint(st), ip=ip, at=_now()))
        _event(s, link, status, actor=link.email, request=request,
               detail={"signer_name": signer_name, "note": note})
        s.flush()
        settled = False
        if status == "approved" and state == "draft" \
                and approvals(s, c, st)["all_approved"]:
            # Every reinsurer on the panel has approved THESE terms: they are
            # agreed, exactly as a broker's acceptance agrees a binder's. The
            # carrier signs next, and the reinsurers' signature is recorded
            # against the signed copy, as before.
            cr._move(c, "agreed")
            settled = True
        s.commit()
        party = s.get(Party, link.party_id)
        org = getattr(party, "legal_name", None)
        try:
            from audit import log_activity
            log_activity(c.tenant_id, link.email,
                         "treaty_approved_by_reinsurer" if status == "approved"
                         else "treaty_changes_requested_by_reinsurer",
                         target=f"contract:{c.id}",
                         details={"contract_id": c.id, "name": c.name,
                                  "reinsurer": org, "signer_name": signer_name,
                                  "note": note, "terms_agreed": settled})
        except Exception:  # noqa: BLE001 — the answer stands without the trail
            pass
        return {"ok": True, "terms_agreed": settled,
                "view": _public_view(s, link, c)}


@public_router.post("/{token}/approve")
def approve_treaty(token: str, body: ApproveIn, request: Request,
                   session: str = Query("", alias="session"),
                   x_treaty_session: str = Header(default="")):
    """A reinsurer approves its lines and the treaty's terms, on its private
    link. Its name is what it typed — the code it entered is what shows it
    holds the mailbox the link was sent to."""
    name = (body.signer_name or "").strip()
    if not name:
        raise HTTPException(400, "Type your name to approve.")
    return _answer(token, _session_value(x_treaty_session, session), request,
                   "approved", signer_name=name,
                   signer_title=(body.signer_title or "").strip() or None)


@public_router.post("/{token}/request-changes")
def request_treaty_changes(token: str, body: ChangeIn, request: Request,
                           session: str = Query("", alias="session"),
                           x_treaty_session: str = Header(default="")):
    note = (body.note or "").strip()
    if not note:
        raise HTTPException(400, "Say what should change.")
    with SessionLocal() as s:
        link = _by_token(s, token)
        who = link.name
        c = s.get(Contract, link.contract_id)
        if c is not None and _state(c) not in EDITABLE_STATES:
            raise HTTPException(
                409, "This treaty is signed, so its terms cannot change. You "
                     "can approve it.")
    return _answer(token, _session_value(x_treaty_session, session), request,
                   "changes_requested", signer_name=who, note=note)


@public_router.get("/{token}")
def open_treaty_view(token: str, request: Request,
                     session: str = Query("", alias="session"),
                     x_treaty_session: str = Header(default="")):
    with SessionLocal() as s:
        link = _by_token(s, token)
        c = s.get(Contract, link.contract_id)
        closed = _closed_or_none(s, link, c)
        if closed:
            _event(s, link, "refused", actor=link.email, request=request,
                   detail={"status": closed["status"]})
            s.commit()
            return closed
        if _read_session(_session_value(x_treaty_session, session), token) != link.id:
            locked = esign_otp.is_locked(link.otp_locked_until)
            return {
                "closed": False, "locked": True,
                "carrier": _carrier_name(s, c),
                "email_hint": esign_otp.masked_email(link.email),
                "attempts_left": (0 if locked else max(
                    0, esign_otp.MAX_ATTEMPTS - (link.otp_attempts or 0))),
                "lockout_seconds": esign_otp.lockout_seconds_left(
                    link.otp_locked_until),
                "can_resend": (link.otp_sent_count or 0) < esign_otp.MAX_SENDS,
            }
        view = _public_view(s, link, c)
        link.last_viewed_at = _now()
        link.first_opened_at = link.first_opened_at or link.last_viewed_at
        _event(s, link, "viewed", actor=link.email, request=request)
        s.commit()
        return view


class CodeIn(BaseModel):
    code: str


@public_router.post("/{token}/verify")
def verify_treaty_code(token: str, body: CodeIn, request: Request):
    """The code from the email, for a session. Wrong and expired codes answer
    the same way; five wrong locks the link for a while — esign_otp's rules."""
    with SessionLocal() as s:
        link = _by_token(s, token)
        c = s.get(Contract, link.contract_id)
        closed = _closed_or_none(s, link, c)
        if closed:
            raise HTTPException(410, closed["message"])
        if esign_otp.is_locked(link.otp_locked_until):
            raise HTTPException(
                429, "Too many wrong codes. Try again in "
                     f"{esign_otp.lockout_seconds_left(link.otp_locked_until) // 60 + 1}"
                     " minutes, or ask for a new code.")
        exp = _aware(link.otp_expires)
        if (exp is not None and exp < _now()) or not esign_otp.check_code(
                body.code or "", link.otp_hash):
            link.otp_attempts = (link.otp_attempts or 0) + 1
            left = esign_otp.MAX_ATTEMPTS - link.otp_attempts
            if left <= 0:
                link.otp_locked_until = esign_otp.next_lockout()
                link.otp_attempts = 0
            _event(s, link, "locked" if left <= 0 else "code_failed",
                   actor=link.email, request=request)
            s.commit()
            if left <= 0:
                raise HTTPException(
                    429, f"Too many wrong codes. This link is locked for "
                         f"{esign_otp.LOCKOUT_MINUTES} minutes.")
            raise HTTPException(400, f"That code is not right. {left} attempt"
                                     f"{'' if left == 1 else 's'} left.")
        link.otp_attempts = 0
        link.otp_locked_until = None
        link.otp_verified_at = _now()
        _event(s, link, "unlocked", actor=link.email, request=request)
        session = _mint_session(link.id, token)
        s.commit()
    return {"ok": True, "session": session,
            "expires_in": esign_otp.SESSION_MINUTES * 60}


@public_router.post("/{token}/resend-code")
def resend_treaty_code(token: str, request: Request):
    """A fresh code to the same address. Rate-limited like the signing link's:
    a cooldown, and a ceiling per link. The URL is unchanged."""
    import treaty_email
    with SessionLocal() as s:
        link = _by_token(s, token)
        c = s.get(Contract, link.contract_id)
        closed = _closed_or_none(s, link, c)
        if closed:
            raise HTTPException(410, closed["message"])
        last = _aware(link.otp_last_sent_at)
        if last and (_now() - last).total_seconds() < esign_otp.RESEND_COOLDOWN_SECONDS:
            wait = int(esign_otp.RESEND_COOLDOWN_SECONDS
                       - (_now() - last).total_seconds())
            raise HTTPException(429, f"A code was just sent. Wait {wait} seconds.")
        if (link.otp_sent_count or 0) >= esign_otp.MAX_SENDS:
            raise HTTPException(429, "Too many codes have been sent for this "
                                     "link. Ask the sender for a new one.")
        code = esign_otp.generate_code()
        link.otp_hash = esign_otp.hash_code(code)
        link.otp_expires = link.expires_at
        link.otp_attempts = 0
        link.otp_locked_until = None
        link.otp_sent_count = (link.otp_sent_count or 0) + 1
        link.otp_last_sent_at = _now()
        _event(s, link, "code_sent", actor=link.email, request=request)
        to, name, title = link.email, link.name, c.name or "your treaty"
        s.commit()
    ok = treaty_email.safe_send(
        to, f"Your code for {title}",
        treaty_email.code_email(name=name, treaty=title, otp=code),
        text=f"Your code for {title} is {code}.")
    return {"ok": ok, "sent_to": esign_otp.masked_email(to)}


@public_router.get("/{token}/documents/{document_id}")
def treaty_view_document(token: str, document_id: int, request: Request,
                         session: str = Query("", alias="session"),
                         x_treaty_session: str = Header(default="")):
    cr = _cr()
    with SessionLocal() as s:
        link, c = _require_open(s, token,
                                _session_value(x_treaty_session, session))
        d = s.get(ContractDocument, document_id)
        if (d is None or d.contract_id != c.id or d.kind != "contract"
                or not d.is_active):
            raise HTTPException(404, "document not found")
        data = cr._doc_bytes(d)
        if not data:
            raise HTTPException(404, "this document's file is no longer stored")
        _event(s, link, "downloaded", actor=link.email, request=request,
               detail={"document_id": d.id, "filename": d.filename})
        s.commit()
        name = d.filename or f"treaty-{c.id}"
    return Response(content=data, media_type=cr._media_type(name),
                    headers={"Content-Disposition": cr._content_disposition(name)})


@public_router.get("/{token}/wording.pdf")
def treaty_view_wording(token: str, request: Request,
                        session: str = Query("", alias="session"),
                        x_treaty_session: str = Header(default="")):
    """The wording written in Kavachio, composed — for a treaty whose signed
    copy is not attached yet. It names no reinsurer (treaty_types.build_sections),
    so it is safe to hand to any one of them."""
    cr = _cr()
    with SessionLocal() as s:
        link, c = _require_open(s, token,
                                _session_value(x_treaty_session, session))
        data = cr.compose_contract_pdf(s, c)
        _event(s, link, "downloaded", actor=link.email, request=request,
               detail={"wording_pdf": True})
        s.commit()
        name = f"{c.name or 'Treaty'} - wording.pdf"
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": cr._content_disposition(name)})
