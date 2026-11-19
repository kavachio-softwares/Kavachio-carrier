"""
A MASTER treaty: one treaty, one shared wording, placed with several
reinsurers — each writing its own participation share of each layer, and each
SIGNING for itself.

  Lines        each reinsurer's share of each layer (the treaty structure).
  Signatures   one per reinsurer (TreatyPartySignature): how it signed — on its
               private link, in the signing round (the lead), or on paper and
               recorded by the carrier — the document evidence it rests on, its
               lines as signed and the version of the shared wording.
  Placement    worked out per layer, never stored:
                 written  unplaced · partially placed · fully placed · over-placed
                 signed   unsigned · partially signed · fully signed
               A line changed after it was signed is flagged: the signature is
               for the line as it was.

The lead reinsurer's signature is also the treaty's counterparty signature
(contract_signature) — the one that, with the carrier's, puts it in force. The
other reinsurers' signatures bind their own lines; a treaty can go in force
before every line is signed, and shows as partially signed until it is.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

TOLERANCE = 0.005          # a share of 33.333 + 33.333 + 33.334 is 100%

WRITTEN_LABEL = {"unplaced": "Unplaced", "partially_placed": "Partially placed",
                 "fully_placed": "Fully placed", "over_placed": "Over-placed"}
SIGNED_LABEL = {"unsigned": "Unsigned", "partially_signed": "Partially signed",
                "fully_signed": "Fully signed"}
METHOD_LABEL = {"link": "Signed on their private link",
                "esign": "Signed in Kavachio's signing round",
                "recorded": "Signed on paper — recorded by you"}


def _tr():
    import treaty_routes
    return treaty_routes


def _cr():
    import contract_routes
    return contract_routes


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def written_status(pct: float) -> str:
    if pct <= TOLERANCE:
        return "unplaced"
    if pct < 100 - TOLERANCE:
        return "partially_placed"
    if pct <= 100 + TOLERANCE:
        return "fully_placed"
    return "over_placed"


def signed_status(signed: float, written: float) -> str:
    if signed <= TOLERANCE:
        return "unsigned"
    if written > TOLERANCE and signed >= written - TOLERANCE:
        return "fully_signed"
    return "partially_signed"


def lines_of(st: Optional[dict], party_id: int) -> dict:
    """{layer index: share %} — a reinsurer's lines, as it signs them."""
    out = {}
    for i, lay in enumerate((st or {}).get("layers") or []):
        for sh in lay.get("shares") or []:
            if sh.get("reinsurer_party_id") == party_id:
                out[str(i)] = round(_num(sh.get("share_pct")), 6)
    return out


def live_signatures(s, c) -> dict[int, object]:
    """Each reinsurer's live signature, by party."""
    from db import TreatyPartySignature
    rows = (s.query(TreatyPartySignature)
            .filter(TreatyPartySignature.contract_id == c.id,
                    TreatyPartySignature.withdrawn_at.is_(None)).all())
    return {r.party_id: r for r in rows}


def _lead_from_contract(s, c) -> Optional[dict]:
    """The lead's signature when it was given as the treaty's counterparty
    signature (the signing round, or before signatures were kept per party)."""
    sig = next((g for g in _cr()._signatures(s, c.id) if g.side == "counterparty"), None)
    if sig is None:
        return None
    method = {"link": "link", "recorded": "recorded"}.get(sig.method, "esign")
    doc = None
    if method == "recorded":
        doc = getattr(sig, "document_id", None)
    return {"signer_name": sig.signer_name, "signer_title": sig.signer_title,
            "signer_email": sig.signer_email, "method": method,
            "signed_at": sig.signed_at, "document_id": doc, "lines": None,
            "id": None}


def _sig_json(s, c, row, st, party_id, docs: dict) -> Optional[dict]:
    if row is None:
        return None
    get = (lambda k: row.get(k)) if isinstance(row, dict) else (lambda k: getattr(row, k, None))
    now_lines = lines_of(st, party_id)
    signed_lines = get("lines")
    doc_id = get("document_id")
    d = docs.get(doc_id) if doc_id else None
    return {
        "id": get("id"), "signer_name": get("signer_name"), "signer_title": get("signer_title"),
        "signer_email": get("signer_email"), "method": get("method"),
        "method_label": METHOD_LABEL.get(get("method"), get("method")),
        "signed_at": _cr()._iso_utc(get("signed_at")) if get("signed_at") else None,
        "evidence": ({"document_id": d.id, "filename": d.filename, "kind": d.kind,
                      "has_file": bool(d.blob_ref or d.blob is not None)} if d else None),
        # Signed for a line that has since changed: its signature is for the old one.
        "stale": signed_lines is not None and signed_lines != now_lines,
        "lines_signed": signed_lines,
    }


def placement(s, c) -> dict:
    """The treaty's placement, layer by layer and reinsurer by reinsurer."""
    from db import ContractDocument, Party
    st = _tr().load_structure(s, c.id, names=False)
    sigs = live_signatures(s, c)
    lead_sig = None
    if c.broker_party_id and c.broker_party_id not in sigs:
        lead_sig = _lead_from_contract(s, c)
    pids = {sh.get("reinsurer_party_id") for lay in (st or {}).get("layers") or []
            for sh in lay.get("shares") or []} - {None}
    names = {p.id: p.legal_name for p in s.query(Party).filter(Party.id.in_(pids or {0}))}
    doc_ids = {getattr(r, "document_id", None) for r in sigs.values()} | \
              ({lead_sig["document_id"]} if lead_sig and lead_sig.get("document_id") else set())
    docs = {d.id: d for d in s.query(ContractDocument)
            .filter(ContractDocument.id.in_({x for x in doc_ids if x} or {0}))}
    evidence = {}
    for d in s.query(ContractDocument).filter(
            ContractDocument.contract_id == c.id, ContractDocument.party_id.isnot(None),
            ContractDocument.is_active.is_(True)):
        evidence.setdefault(d.party_id, []).append(
            {"document_id": d.id, "filename": d.filename, "kind": d.kind})

    def sig_for(pid):
        if pid in sigs:
            return _sig_json(s, c, sigs[pid], st, pid, docs)
        if pid == c.broker_party_id and lead_sig:
            return _sig_json(s, c, lead_sig, st, pid, docs)
        return None

    layers = []
    for i, lay in enumerate((st or {}).get("layers") or []):
        lines = []
        for sh in lay.get("shares") or []:
            pid = sh.get("reinsurer_party_id")
            sig = sig_for(pid) if pid else None
            lines.append({"party_id": pid, "name": names.get(pid) or sh.get("reinsurer_name"),
                          "share_pct": _num(sh.get("share_pct")), "is_lead": bool(sh.get("is_lead")),
                          "slip_reference": sh.get("slip_reference"),
                          "signature": sig,
                          "status": "signed" if sig and not sig["stale"]
                                    else "line_changed" if sig else "awaiting"})
        written = sum(x["share_pct"] for x in lines)
        signed = sum(x["share_pct"] for x in lines if x["status"] == "signed")
        ws, ss = written_status(written), signed_status(signed, written)
        layers.append({"index": i, "name": lay.get("name"),
                       "written_pct": round(written, 4), "signed_pct": round(signed, 4),
                       "retained_pct": round(max(0.0, 100 - written), 4),
                       "written_status": ws, "written_label": WRITTEN_LABEL[ws],
                       "signed_status": ss, "signed_label": SIGNED_LABEL[ss],
                       "lines": lines})

    # The treaty as a whole: its weakest layer decides.
    order_w = ["unplaced", "partially_placed", "over_placed", "fully_placed"]
    order_s = ["unsigned", "partially_signed", "fully_signed"]
    ws = min((l_["written_status"] for l_ in layers), key=order_w.index, default="unplaced")
    if any(l_["written_status"] == "over_placed" for l_ in layers):
        ws = "over_placed"
    ss = min((l_["signed_status"] for l_ in layers), key=order_s.index, default="unsigned")
    parties = sorted(pids, key=lambda x: (names.get(x) or "").lower())
    signed_parties = [pid for pid in parties
                      if (lambda sg: sg and not sg["stale"])(sig_for(pid))]
    import treaty_wording
    return {
        "has_structure": st is not None,
        "written_status": ws, "written_label": WRITTEN_LABEL[ws],
        "signed_status": ss, "signed_label": SIGNED_LABEL[ss],
        "parties": len(parties), "parties_signed": len(signed_parties),
        "lead_party_id": c.broker_party_id,
        # The one wording every reinsurer signs.
        "wording": treaty_wording.signed_wording(s, c),
        "layers": layers,
        "evidence": {str(k): v for k, v in evidence.items()},
    }


def summary(s, c) -> dict:
    """The few facts the treaty's header and lists carry."""
    p = placement(s, c)
    return {k: p[k] for k in ("written_status", "written_label", "signed_status",
                              "signed_label", "parties", "parties_signed")}


def record(s, c, party_id: int, *, signer_name: str, signer_title: Optional[str],
           signer_email: Optional[str], method: str, document_id: Optional[int] = None,
           link_id: Optional[int] = None, ip: Optional[str] = None,
           note: Optional[str] = None, user_id: Optional[int] = None):
    """A reinsurer's signature for its own lines, under the shared wording."""
    from db import TreatyPartySignature
    import treaty_wording
    st = _tr().load_structure(s, c.id, names=False)
    w = treaty_wording.signed_wording(s, c)
    row = TreatyPartySignature(
        tenant_id=c.tenant_id, contract_id=c.id, party_id=party_id,
        signer_name=signer_name, signer_title=signer_title, signer_email=signer_email,
        method=method, document_id=document_id,
        wording_document_id=(w or {}).get("document_id"),
        lines=lines_of(st, party_id), link_id=link_id, ip=ip, note=note,
        recorded_by_user_id=user_id, signed_at=_now())
    s.add(row)
    s.flush()
    return row


def withdraw(s, c, party_id: int, user_id: Optional[int]) -> bool:
    row = live_signatures(s, c).get(party_id)
    if row is None:
        return False
    row.withdrawn_at = _now()
    row.withdrawn_by_user_id = user_id
    return True


def signing_block(s, c, party_id: int) -> Optional[str]:
    """Why this reinsurer cannot sign its lines now, or None. The lead signs
    as the treaty's counterparty (contract_routes.counterparty_signing_block);
    every other reinsurer once the terms are settled and the carrier has signed."""
    cr = _cr()
    st = _tr().load_structure(s, c.id, names=False)
    if not lines_of(st, party_id):
        return "it has no line on this treaty"
    if party_id in live_signatures(s, c):
        return "it has already signed its lines"
    if party_id == c.broker_party_id:
        return cr.counterparty_signing_block(s, c)
    state = cr._effective_lifecycle(c)
    if state in ("in_review", "changes_requested", "pending"):
        return "its terms are still being settled"
    if state in ("terminated", "superseded"):
        return f"it is {state}"
    if "carrier" in cr._unsigned_sides(cr._signatures(s, c.id)):
        return "the carrier has not signed it yet — the carrier signs first"
    return None
