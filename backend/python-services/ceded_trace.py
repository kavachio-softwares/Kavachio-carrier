"""
Tracing a reinsurance statement's figures back to the stored terms (27.3).

A statement is worked out with the terms IN FORCE on its period's first day
(treaty_basis.structure_at). This keeps a record of exactly those terms on the
statement — each one's value, the version it came from, the date it applies
from and where it was agreed — together with each reinsurer's share. With it,
a statement still says what it was worked out with after the terms change.

Each line carries its own sum in words ("how", ceded_engine.compute), so the
two together trace every figure: the line shows the arithmetic, the record
shows which term version each rate came from.

The record is kept inside the statement's totals, under "_basis" (the same
name structure_at gives it), so no new column is needed. `money_totals` and
`basis_of` are the only ways in and out — nothing else reads it.
"""
from __future__ import annotations

import copy
from typing import Any, Optional

import treaty_types as tt

KEY = "_basis"

SOURCE = {
    "structure": "As agreed on the treaty",
    "manual": "Changed on the Calculation basis",
    "wording": "From the signed wording",
    "endorsement": "By endorsement",
}


def value_text(kind: Optional[str], value: Any, currency: Optional[str] = None) -> str:
    """A term's value in words, as a statement shows it."""
    if kind == "pct":
        return tt.pct(value)
    if kind == "money":
        return tt.full_money(value, currency)
    if kind == "sliding":
        return tt._scale_fact(value) or "—"
    if kind == "profit":
        return tt._profit_fact(value) or "—"
    return "—" if value in (None, "") else f"{value}"


def snapshot(s, st_at: dict, currency: Optional[str] = None) -> Optional[dict]:
    """The record of what a statement was worked out with: every dated term in
    force for its period, and each layer's reinsurers and their shares."""
    from db import ContractDocument, Party
    basis = (st_at or {}).get("_basis")
    if not basis:
        return None
    docs = {d.id: d.filename for d in s.query(ContractDocument).filter(
        ContractDocument.id.in_({u.get("document_id") for u in basis["terms"]
                                 if u.get("document_id")} or {0}))}
    terms = []
    for u in basis["terms"]:
        src = SOURCE.get(u.get("source"), "")
        if u.get("source") == "endorsement" and docs.get(u.get("document_id")):
            src = f"{src}: {docs[u['document_id']]}"
        terms.append({
            "term": u["term"], "layer": u["layer"], "id": u["id"],
            "label": u.get("label") or u["term"], "layer_name": u.get("layer_name"),
            "kind": u.get("kind"), "value": u["value"],
            "value_text": value_text(u.get("kind"), u["value"], currency),
            "effective_from": u.get("effective_from"), "source": u.get("source"),
            "source_text": src,
        })
    layers = st_at.get("layers") or []
    pids = {sh.get("reinsurer_party_id") for lay in layers
            for sh in lay.get("shares") or [] if sh.get("reinsurer_party_id")}
    names = {p.id: p.legal_name for p in s.query(Party).filter(Party.id.in_(pids or {0}))}
    return {
        "as_of": basis.get("as_of"),
        "treaty_type": st_at.get("treaty_type"),
        "type_label": tt.label(st_at.get("treaty_type")),
        "terms": terms,
        "layers": [{"index": i, "name": lay.get("name"),
                    "shares": [{"reinsurer_party_id": sh.get("reinsurer_party_id"),
                                "reinsurer": names.get(sh.get("reinsurer_party_id")),
                                "share_pct": sh.get("share_pct"),
                                "is_lead": bool(sh.get("is_lead"))}
                               for sh in lay.get("shares") or []]}
                   for i, lay in enumerate(layers)],
    }


def with_basis(totals: Optional[dict], snap: Optional[dict]) -> dict:
    """The totals to store on a statement, carrying its record."""
    out = dict(totals or {})
    if snap:
        out[KEY] = snap
    return out


def money_totals(totals: Optional[dict]) -> dict:
    """A statement's totals without the record — what every screen sums."""
    return {k: v for k, v in (totals or {}).items() if k != KEY}


def basis_of(totals: Optional[dict]) -> Optional[dict]:
    """The record kept on a statement; None for one made before 27.3."""
    b = (totals or {}).get(KEY)
    return b if isinstance(b, dict) else None


def for_reinsurer(snap: Optional[dict], party_id: int) -> Optional[dict]:
    """The record as one reinsurer sees it: the terms, and its own shares only."""
    if not snap:
        return None
    out = copy.deepcopy(snap)
    out["layers"] = [{**lay, "shares": [sh for sh in lay["shares"]
                                         if sh.get("reinsurer_party_id") == party_id]}
                     for lay in out.get("layers") or []]
    out["layers"] = [lay for lay in out["layers"] if lay["shares"]]
    return out


def changes(snap: Optional[dict], now: Optional[dict]) -> list[dict]:
    """Each term whose version in force for the period differs now from the
    one the statement used: what it was, what it is, and from when."""
    if not snap or not now:
        return []
    was = {(t["term"], t["layer"]): t for t in snap.get("terms") or []}
    out = []
    for t in now.get("terms") or []:
        old = was.pop((t["term"], t["layer"]), None)
        if old and old.get("id") == t.get("id") and old.get("value") == t.get("value"):
            continue
        out.append({"label": t["label"], "layer_name": t.get("layer_name"),
                    "was": old["value_text"] if old else "not set",
                    "now": t["value_text"], "effective_from": t.get("effective_from"),
                    "source_text": t.get("source_text")})
    for old in was.values():
        out.append({"label": old["label"], "layer_name": old.get("layer_name"),
                    "was": old["value_text"], "now": "not set",
                    "effective_from": None, "source_text": None})
    return out


def sheet_rows(snap: Optional[dict]) -> list[tuple]:
    """The record as rows for a workbook's "Terms used" sheet."""
    if not snap:
        return [("Terms used were not recorded for this statement.",)]
    rows: list[tuple] = [
        (f"Worked out with the terms in force on {snap.get('as_of')}",), (),
        ("Term", "Layer", "Value", "Applies from", "Where it was agreed")]
    for t in snap.get("terms") or []:
        rows.append((t["label"], t.get("layer_name") or "", t["value_text"],
                     t.get("effective_from") or "", t.get("source_text") or ""))
    rows += [(), ("Reinsurer", "Layer", "Share", "Lead")]
    for lay in snap.get("layers") or []:
        for sh in lay["shares"]:
            rows.append((sh.get("reinsurer") or "", lay.get("name") or "",
                         tt.pct(sh.get("share_pct")), "Lead" if sh.get("is_lead") else ""))
    return rows
