"""
A treaty's CALCULATION BASIS, held with the dates it applies from (27.1, 27.2).

What every ceded figure is worked out from — and, because a treaty's terms move
during its life ("cession 40% from 1 January, 35% from 1 July"; a layer's limit
raised mid-year; a commission renegotiated), each term is held as a SCHEDULE:
the value agreed on the treaty's structure applies from inception, and each
dated change takes over from its own date.

  27.1  cession %, a surplus's retention line and lines, each layer's
        attachment (retention / loss-ratio / amount) and limit.
  27.2  ceding commission %; PROFIT commission and SLIDING-SCALE commission,
        each a small structure of its own — their exact figures are the
        treaty's (EXT), so they are entered, never assumed:
          profit commission  {rate_pct, expense_pct, deficit_years}
          sliding scale      {provisional_pct, min_pct, max_pct,
                              method: "linear" | "step",
                              points: [{loss_ratio, rate}]}

A statement is worked out with the terms IN FORCE on its period's first day
(structure_at). Each entry has an id, a date and a source, so a figure can be
traced back to the exact term version it used (27.3).
"""
from __future__ import annotations

import copy
import datetime as dt
from typing import Any, Optional

import treaty_types as tt

# The dated terms, per treaty type: (term, label, kind, level).
#   kind   pct · money · int · profit · sliding
#   level  "treaty" (one value) or "layer" (one per layer)
BASIS: dict[str, list[tuple[str, str, str, str]]] = {
    "quota_share": [
        ("cession_pct", "Cession", "pct", "treaty"),
        ("ceding_commission_pct", "Ceding commission", "pct", "treaty"),
        ("sliding_scale", "Sliding-scale commission", "sliding", "treaty"),
        ("profit_commission", "Profit commission", "profit", "treaty"),
    ],
    "surplus": [
        ("retention_line", "Retention (one line)", "money", "treaty"),
        ("lines", "Number of lines", "int", "treaty"),
        ("ceding_commission_pct", "Ceding commission", "pct", "treaty"),
        ("sliding_scale", "Sliding-scale commission", "sliding", "treaty"),
        ("profit_commission", "Profit commission", "profit", "treaty"),
    ],
    "xol": [
        ("retention", "Attachment (retention)", "money", "layer"),
        ("limit", "Limit", "money", "layer"),
    ],
    "stop_loss": [
        ("attachment_pct", "Attaches at (loss ratio)", "pct", "layer"),
        ("limit_pct", "Cover (loss ratio points)", "pct", "layer"),
        ("attachment_amount", "Attaches at (amount)", "money", "layer"),
        ("limit_amount", "Cover (amount)", "money", "layer"),
    ],
    "facultative": [
        ("share_ceded_pct", "Share ceded", "pct", "treaty"),
        ("ceding_commission_pct", "Ceding commission", "pct", "treaty"),
    ],
}
SOURCES = ("structure", "manual", "wording", "endorsement")


def spec(treaty_type: Optional[str]) -> list[dict]:
    return [{"term": k, "label": lbl, "kind": kind, "level": lvl}
            for k, lbl, kind, lvl in BASIS.get(treaty_type or "", [])]


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _date(v) -> Optional[dt.date]:
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


# ── checking an entry ───────────────────────────────────────────────────────
class BasisError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def clean_value(kind: str, raw: Any) -> Any:
    """A term's value, checked for its kind. Raises BasisError."""
    if kind in ("pct", "money", "int"):
        n = _num(raw)
        if n is None:
            raise BasisError({"value": "Enter a number."})
        if n < 0:
            raise BasisError({"value": "It cannot be negative."})
        if kind == "pct" and n > 100:
            raise BasisError({"value": "A percentage is at most 100."})
        if kind == "int":
            if n != int(n):
                raise BasisError({"value": "A whole number."})
            n = int(n)
        return n
    if not isinstance(raw, dict):
        raise BasisError({"value": "Enter the commission's terms."})
    if kind == "profit":
        rate, exp = _num(raw.get("rate_pct")), _num(raw.get("expense_pct"))
        years = _num(raw.get("deficit_years"))
        err = {}
        if rate is None or not 0 < rate <= 100:
            err["rate_pct"] = "The profit commission rate, 0–100%."
        if exp is not None and not 0 <= exp <= 100:
            err["expense_pct"] = "The expense allowance, 0–100% of premium."
        if years is not None and (years < 0 or years != int(years)):
            err["deficit_years"] = "Years a deficit is carried forward — a whole number."
        if err:
            raise BasisError(err)
        return {"rate_pct": rate, "expense_pct": exp or 0.0,
                "deficit_years": int(years or 0)}
    if kind == "sliding":
        prov = _num(raw.get("provisional_pct"))
        method = raw.get("method") if raw.get("method") in ("linear", "step") else "linear"
        pts = []
        raw_pts = raw.get("points")
        # A list of {loss_ratio, rate}; anything else is not a scale.
        for p in (raw_pts if isinstance(raw_pts, list) else []):
            if not isinstance(p, dict):
                continue
            lr, rate = _num(p.get("loss_ratio")), _num(p.get("rate"))
            if lr is None or rate is None:
                continue
            pts.append({"loss_ratio": lr, "rate": rate})
        pts.sort(key=lambda p: p["loss_ratio"])
        err = {}
        if prov is None or not 0 <= prov <= 100:
            err["provisional_pct"] = "The provisional commission charged on each line, 0–100%."
        if len(pts) < 2:
            err["points"] = "At least two points on the scale: a loss ratio and its commission."
        elif len({p["loss_ratio"] for p in pts}) != len(pts):
            err["points"] = "Each loss ratio on the scale once."
        elif any(not 0 <= p["rate"] <= 100 for p in pts):
            err["points"] = "Each commission rate 0–100%."
        if err:
            raise BasisError(err)
        rates = [p["rate"] for p in pts]
        return {"provisional_pct": prov, "method": method, "points": pts,
                "min_pct": min(rates), "max_pct": max(rates)}
    raise BasisError({"term": "Unknown term."})


# ── the commission formulas ─────────────────────────────────────────────────
def sliding_rate(scale: dict, loss_ratio: float) -> float:
    """The commission rate the scale gives at `loss_ratio` (%). Below the first
    point its rate applies, above the last its rate; between points, linear
    or stepped as the scale says."""
    pts = scale["points"]
    if loss_ratio <= pts[0]["loss_ratio"]:
        return pts[0]["rate"]
    if loss_ratio >= pts[-1]["loss_ratio"]:
        return pts[-1]["rate"]
    for a, b in zip(pts, pts[1:]):
        if a["loss_ratio"] <= loss_ratio <= b["loss_ratio"]:
            if scale.get("method") == "step":
                return a["rate"] if loss_ratio < b["loss_ratio"] else b["rate"]
            span = b["loss_ratio"] - a["loss_ratio"]
            return round(a["rate"] + (b["rate"] - a["rate"]) * (loss_ratio - a["loss_ratio"]) / span, 6)
    return pts[-1]["rate"]


def sliding_adjustment(scale: dict, *, premium: float, losses: float) -> dict:
    """What the scale makes the commission on `premium` ceded with `losses`
    incurred: the rate it earns, against the provisional rate charged."""
    lr = (losses / premium * 100) if premium else 0.0
    rate = sliding_rate(scale, lr)
    earned = premium * rate / 100
    charged = premium * scale["provisional_pct"] / 100
    return {"loss_ratio": round(lr, 4), "rate_pct": rate,
            "commission": round(earned, 2), "provisional": round(charged, 2),
            "adjustment": round(earned - charged, 2)}


def profit_commission(pc: dict, *, premium: float, ceding_commission: float,
                      losses: float, deficit_brought_forward: float = 0.0) -> dict:
    """Profit commission on one treaty year: the reinsurers' profit — premium,
    less ceding commission, losses, their expense allowance and any deficit
    brought forward — times the rate. A loss leaves nothing, and is carried
    forward if the treaty says so."""
    expenses = premium * pc["expense_pct"] / 100
    profit = premium - ceding_commission - losses - expenses - deficit_brought_forward
    return {"premium": round(premium, 2), "ceding_commission": round(ceding_commission, 2),
            "losses": round(losses, 2), "expenses": round(expenses, 2),
            "deficit_brought_forward": round(deficit_brought_forward, 2),
            "profit": round(profit, 2),
            "commission": round(max(0.0, profit) * pc["rate_pct"] / 100, 2),
            "deficit_carried_forward": round(-profit, 2)
            if profit < 0 and pc.get("deficit_years") else 0.0}


# ── the schedule ────────────────────────────────────────────────────────────
def _entries(s, c) -> list:
    from db import TreatyTerm
    return (s.query(TreatyTerm)
            .filter(TreatyTerm.contract_id == c.id, TreatyTerm.removed_at.is_(None))
            .order_by(TreatyTerm.effective_from, TreatyTerm.id).all())


def _base_value(st: dict, term: str, layer: Optional[int]):
    if layer is None:
        terms = st.get("terms") or {}
        if term == "profit_commission" and not terms.get(term) \
                and _num(terms.get("profit_commission_pct")):
            # Agreed as a rate only, before the full terms were held.
            return {"rate_pct": _num(terms["profit_commission_pct"]),
                    "expense_pct": 0.0, "deficit_years": 0}
        return terms.get(term)
    lays = st.get("layers") or []
    return (lays[layer].get("terms") or {}).get(term) if layer < len(lays) else None


def schedule(s, c, st: Optional[dict] = None) -> dict:
    """Every dated term, each with its versions: the structure's value from
    inception, then each change from its date."""
    import treaty_routes
    st = st if st is not None else treaty_routes.load_structure(s, c.id, names=False)
    if not st:
        return {"treaty_type": None, "terms": [], "inception": None}
    rows = _entries(s, c)
    inception = c.inception_dt.isoformat() if c.inception_dt else None
    out = []
    import treaty_types as tt
    for sp in spec(st.get("treaty_type")):
        # A term that only applies under one setting is left out under the
        # other: a stop loss measured as a loss ratio attaches at a % (its
        # amount pair is not used, nor worked out), and one measured as an
        # amount attaches at the amount entered.
        f = (tt.LAYER_FIELDS if sp["level"] == "layer" else tt.TREATY_FIELDS).get(sp["term"]) or {}
        w = f.get("when")
        if w and (st.get("terms") or {}).get(w["field"]) != w["equals"]:
            continue
        for li in ([None] if sp["level"] == "treaty"
                   else list(range(len(st.get("layers") or [])))):
            base = _base_value(st, sp["term"], li)
            versions = []
            if base not in (None, ""):
                versions.append({"id": None, "effective_from": inception, "value": base,
                                 "source": "structure", "note": "As agreed on the treaty's structure",
                                 "entered_by": None})
            for r in rows:
                if r.term == sp["term"] and r.layer_index == li:
                    versions.append({"id": r.id, "effective_from": r.effective_from.isoformat(),
                                     "value": r.value, "source": r.source, "note": r.note,
                                     "document_id": r.document_id,
                                     "entered_by": r.created_by_name,
                                     "entered_at": r.created_at.isoformat() if r.created_at else None})
            versions.sort(key=lambda v: v["effective_from"] or "")
            out.append({**sp, "layer": li,
                        "layer_name": (st["layers"][li]["name"] if li is not None else None),
                        "versions": versions})
    return {"treaty_type": st.get("treaty_type"), "inception": inception,
            "expiry": c.expiry_dt.isoformat() if c.expiry_dt else None, "terms": out}


def in_force(sched: dict, on: dt.date) -> dict:
    """{(term, layer): version} — the version of each term in force on `on`."""
    out = {}
    for t in sched["terms"]:
        live = [v for v in t["versions"]
                if v["effective_from"] is None or _date(v["effective_from"]) <= on]
        if live:
            out[(t["term"], t["layer"])] = live[-1]
    return out


def structure_at(s, c, st: dict, period: str) -> dict:
    """The structure with every dated term set to its value IN FORCE on the
    period's first day — what a statement for `period` is worked out with.
    The terms used, and the version of each, are kept on it (`_basis`) so a
    figure can be traced to them."""
    import ceded_engine as ce
    if not st or not st.get("treaty_type"):
        return st
    bounds = ce.period_bounds(period)
    on = bounds[0] if bounds else dt.date.today()
    sched = schedule(s, c, st)
    live = in_force(sched, on)
    out = copy.deepcopy(st)
    used = []
    for (term, layer), v in live.items():
        value = v["value"]
        if term == "sliding_scale":
            # The provisional rate is what each line is charged; the scale
            # adjusts it on the year's loss ratio.
            out.setdefault("terms", {})["ceding_commission_pct"] = value.get("provisional_pct")
            out["terms"]["sliding_scale"] = value
        elif term == "profit_commission":
            out.setdefault("terms", {})["profit_commission"] = value
            out["terms"]["profit_commission_pct"] = value.get("rate_pct")
        elif layer is None:
            out.setdefault("terms", {})[term] = value
        elif layer < len(out.get("layers") or []):
            out["layers"][layer].setdefault("terms", {})[term] = value
        used.append({"term": term, "layer": layer, "id": v["id"],
                     "effective_from": v["effective_from"], "source": v["source"],
                     "value": value})
    out["_basis"] = {"as_of": on.isoformat(), "terms": used}
    return out


def add(s, c, *, term: str, layer: Optional[int], value: Any, effective_from: Any,
        note: Optional[str], source: str, user_id: Optional[int], who: Optional[str],
        document_id: Optional[int] = None):
    """A dated change to one term. Raises BasisError."""
    import treaty_routes
    from db import TreatyTerm
    st = treaty_routes.load_structure(s, c.id, names=False)
    if not st:
        raise BasisError({"term": "Set out the treaty's structure first."})
    sp = next((x for x in spec(st.get("treaty_type")) if x["term"] == term), None)
    if sp is None:
        raise BasisError({"term": f"A {tt.label(st.get('treaty_type'))} treaty has no such term."})
    if sp["level"] == "layer":
        if layer is None or not 0 <= layer < len(st.get("layers") or []):
            raise BasisError({"layer": "Choose the layer."})
    else:
        layer = None
    on = _date(effective_from)
    if on is None:
        raise BasisError({"effective_from": "The date it applies from."})
    if c.inception_dt and on < c.inception_dt:
        raise BasisError({"effective_from": "It cannot apply before the treaty incepts."})
    if c.expiry_dt and on > c.expiry_dt:
        raise BasisError({"effective_from": "It cannot apply after the treaty expires."})
    clean = clean_value(sp["kind"], value)
    if (s.query(TreatyTerm).filter(TreatyTerm.contract_id == c.id, TreatyTerm.term == term,
                                   TreatyTerm.layer_index.is_(None) if layer is None
                                   else TreatyTerm.layer_index == layer,
                                   TreatyTerm.effective_from == on,
                                   TreatyTerm.removed_at.is_(None)).first()):
        raise BasisError({"effective_from": "There is already a change on that date — "
                                            "remove it first."})
    row = TreatyTerm(tenant_id=c.tenant_id, contract_id=c.id, term=term, layer_index=layer,
                     value=clean, effective_from=on, note=(note or "").strip() or None,
                     source=source if source in SOURCES else "manual",
                     document_id=document_id,
                     created_by_user_id=user_id, created_by_name=who)
    s.add(row)
    s.flush()
    return row


def remove(s, c, entry_id: int, user_id: Optional[int]) -> bool:
    from db import TreatyTerm
    r = s.get(TreatyTerm, entry_id)
    if r is None or r.contract_id != c.id or r.removed_at is not None:
        return False
    r.removed_at = dt.datetime.now(dt.timezone.utc)
    r.removed_by_user_id = user_id
    return True



# ── changed by an endorsement ───────────────────────────────────────────────
ENDORSABLE = ("pct", "money", "int", "profit", "sliding")


def _show(kind: str, v, currency: Optional[str] = None) -> str:
    if v in (None, "") or v == {}:
        return ""
    if kind in ("profit", "sliding"):
        f = tt._profit_fact(v) if kind == "profit" else tt._scale_fact(v)
        return f or ""
    if kind == "pct":
        return f"{float(v):g}%"
    if kind == "money":
        return f"{(currency + ' ') if currency else ''}{float(v):,.0f}"
    return f"{v}"


def endorsable(s, c, on: Optional[dt.date] = None) -> list[dict]:
    """The terms an endorsement can change, each with its value in force on
    `on` (the endorsement's effective date, else today)."""
    import treaty_routes
    st = treaty_routes.load_structure(s, c.id, names=False)
    if not st:
        return []
    sched = schedule(s, c, st)
    live = in_force(sched, on or dt.date.today())
    return [{"term": t["term"], "layer": t["layer"], "label": t["label"],
             "layer_name": t["layer_name"], "kind": t["kind"],
             "value": (live.get((t["term"], t["layer"])) or {}).get("value")}
            for t in sched["terms"] if t["kind"] in ENDORSABLE]


def endorsement_changes(s, c, items: list, effective_from: Any) -> list[dict]:
    """An endorsement's changes to the calculation basis, in the shape of its
    other changes (contract_wording.derive_endorsement_changes) — each from
    the value in force on its date to the new one. Raises BasisError."""
    import treaty_routes
    on = _date(effective_from)
    terms = {(t["term"], t["layer"]): t for t in endorsable(s, c, on)}
    st = treaty_routes.load_structure(s, c.id, names=False) or {}
    cur = ((st.get("terms") or {}).get("currency") or "").upper() or None
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        layer = it.get("layer")
        t = terms.get((it.get("term"), layer if layer is None else int(layer)))
        if t is None:
            raise BasisError({"basis": f"{it.get('term')} is not a term this treaty's "
                                       f"endorsement can change."})
        if it.get("value") in (None, ""):
            continue
        new = clean_value(t["kind"], it["value"])
        old = t["value"]
        if old is not None and (old == new if t["kind"] in ("profit", "sliding")
                                else float(old) == float(new)):
            continue
        label = t["label"] + (f" — {t['layer_name']}" if t["layer_name"] else "")
        out.append({"key": f"basis:{t['term']}:{'' if t['layer'] is None else t['layer']}",
                    "question": label, "kind": "added" if old is None else "amended",
                    "from": _show(t["kind"], old, cur), "to": _show(t["kind"], new, cur),
                    "severity_from": None, "severity_to": None,
                    "check_before": None, "check_after": None,
                    # What the calculation basis takes from it.
                    "basis": {"term": t["term"], "layer": t["layer"], "value": new}})
    return out
