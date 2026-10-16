"""
treaty_types.py
───────────────
WHAT a reinsurance treaty is made of, per type — Phase 26.1.

An insurer ↔ reinsurer contract (contract_types.CONTRACT_TYPES
["insurer_reinsurer"]) says WHO the carrier cedes to and for how long. This
says HOW: the treaty type, the numbers that type is defined by, the layers it
is built from, and which reinsurers hold what share of each layer.

THE FIVE TYPES, AND WHY THEY ARE SHAPED DIFFERENTLY

  quota_share   proportional. A fixed percentage of every risk is ceded. One
                layer; what matters is the cession %.
  surplus       proportional. The carrier keeps a fixed amount per risk (the
                line) and cedes the surplus above it, up to N lines. One layer.
  xol           non-proportional. Losses above a retention are paid up to a
                limit. Usually several layers stacked on top of each other —
                which is the only type where "layers" is plural in practice.
  stop_loss     non-proportional, on the whole year. Pays once the loss ratio
                (or an aggregate amount) passes an attachment point. One layer.
  facultative   one risk, placed on its own. One layer, one policy reference.

EVERY TYPE HAS LAYERS, even the ones with one. Shares hang off a layer, so
modelling a quota share as "a treaty with one layer" means the reinsurer panel
works the same way on all five and nothing downstream has to ask which type it
is looking at before it can say who holds what.

THE FORM IS BUILT FROM THIS FILE (GET /treaty-types), for the same reason the
contract form is built from contract_types: a field cannot be mandatory on the
server and optional on the screen when there is only one list.

Deterministic throughout — no model call. Same structure, same summary, same
wording, every time.
"""
from __future__ import annotations

from typing import Any

# ── field vocabulary ─────────────────────────────────────────────────────────
# kind:   money | pct | int | text | select
# when:   show (and require) this field only when another treaty-level field
#         has a given value — stop loss is measured either as a loss ratio or
#         as an amount, and asking for both would be asking for a contradiction.
TREATY_FIELDS: dict[str, dict[str, Any]] = {
    # ── shared by every type ──
    "currency": {"label": "Treaty currency", "kind": "text", "example": "USD",
                 "hint": "The currency every amount on this treaty is in."},
    "basis": {"label": "Attachment basis", "kind": "select",
              "options": [{"value": "risks_attaching", "label": "Risks attaching"},
                          {"value": "losses_occurring", "label": "Losses occurring"}],
              "hint": "Whether cover follows the policies written in the "
                      "period, or the losses that happen in it."},
    "reinsurance_broker": {
        "label": "Reinsurance broker", "kind": "text", "example": "Gallagher Re",
        "hint": "Who placed it, if anyone. They are not on Kavachio either — "
                "this is a name for the record."},
    "placement_reference": {
        "label": "Placement reference", "kind": "text", "example": "GRE-2027-0418",
        "hint": "The slip or placement number the market knows it by."},

    # ── quota share ──
    "cession_pct": {"label": "Cession", "kind": "pct", "example": "40",
                    "hint": "The share of every risk passed to the reinsurers."},
    "treaty_limit": {"label": "Limit per risk", "kind": "money",
                     "hint": "The most the treaty pays on any one risk."},
    "event_limit": {"label": "Event limit", "kind": "money",
                    "hint": "The most it pays for all risks in one event."},
    "ceding_commission_pct": {
        "label": "Ceding commission", "kind": "pct", "example": "30",
        "hint": "Paid back to you by the reinsurers on the premium ceded."},
    "profit_commission_pct": {
        "label": "Profit commission", "kind": "pct",
        "hint": "Your share of the reinsurers' profit on the treaty."},

    # ── surplus ──
    "retention_line": {"label": "Retention (one line)", "kind": "money",
                       "hint": "What you keep on any one risk."},
    "lines": {"label": "Number of lines", "kind": "int", "example": "9",
              "hint": "How many times the retention the treaty can take."},

    # ── excess of loss ──
    "cover_basis": {"label": "Cover basis", "kind": "select",
                    "options": [{"value": "per_risk", "label": "Per risk"},
                                {"value": "per_event", "label": "Per event"}],
                    "hint": "Whether each layer responds to one risk's loss "
                            "or to everything one event causes."},

    # ── stop loss ──
    "measure": {"label": "Measured as", "kind": "select",
                "options": [{"value": "loss_ratio", "label": "Loss ratio"},
                            {"value": "amount", "label": "Aggregate amount"}],
                "hint": "What the attachment point is expressed in."},
    "subject_premium": {"label": "Subject premium", "kind": "money",
                        "hint": "The year's premium the loss ratio is taken on."},

    # ── facultative ──
    "risk_reference": {"label": "Policy or risk reference", "kind": "text",
                       "example": "POL-88213",
                       "hint": "The one risk this certificate covers."},
    "insured_name": {"label": "Insured", "kind": "text",
                     "hint": "Who the risk belongs to."},
    "sum_insured": {"label": "Sum insured", "kind": "money",
                    "hint": "The full value of the risk, before any is ceded."},
    "share_ceded_pct": {"label": "Share ceded", "kind": "pct", "example": "50",
                        "hint": "How much of the risk the reinsurers take."},
    "premium": {"label": "Reinsurance premium", "kind": "money",
                "hint": "What is paid for the ceded share."},
}

LAYER_FIELDS: dict[str, dict[str, Any]] = {
    "retention": {"label": "Retention", "kind": "money",
                  "hint": "Losses below this stay with you (the layer "
                          "attaches here)."},
    "limit": {"label": "Limit", "kind": "money",
              "hint": "The most this layer pays above its retention."},
    "reinstatements": {"label": "Reinstatements", "kind": "int",
                       "hint": "How many times the limit can be restored "
                               "after a loss."},
    "reinstatement_premium_pct": {"label": "Reinstatement premium",
                                  "kind": "pct",
                                  "hint": "Charged, as a % of the original "
                                          "premium, to restore the limit."},
    "rate_on_line_pct": {"label": "Rate on line", "kind": "pct",
                         "hint": "Premium as a percentage of the limit."},
    "deposit_premium": {"label": "Deposit premium", "kind": "money",
                        "hint": "Paid up front, adjusted at the end of the year."},
    "attachment_pct": {"label": "Attaches at (loss ratio)", "kind": "pct",
                       "when": {"field": "measure", "equals": "loss_ratio"},
                       "example": "75",
                       "hint": "The loss ratio at which the cover starts."},
    "limit_pct": {"label": "Cover (loss ratio points)", "kind": "pct",
                  "when": {"field": "measure", "equals": "loss_ratio"},
                  "example": "30",
                  "hint": "How many loss-ratio points above the attachment "
                          "are paid."},
    "attachment_amount": {"label": "Attaches at (amount)", "kind": "money",
                          "when": {"field": "measure", "equals": "amount"},
                          "hint": "The aggregate loss at which cover starts."},
    "limit_amount": {"label": "Cover (amount)", "kind": "money",
                     "when": {"field": "measure", "equals": "amount"},
                     "hint": "The most paid above the attachment."},
}

COMMON_TREATY = ["currency", "basis", "reinsurance_broker", "placement_reference"]

TREATY_TYPES: dict[str, dict[str, Any]] = {
    "quota_share": {
        "key": "quota_share", "label": "Quota Share", "short": "QS",
        "proportional": True,
        "blurb": "A fixed share of every risk is ceded.",
        "fields": ["cession_pct", "treaty_limit", "event_limit",
                   "ceding_commission_pct", "profit_commission_pct"],
        "required": ["currency", "cession_pct"],
        "layer_fields": [], "layer_required": [], "multi_layer": False,
        "default_layer": "Quota share",
    },
    "surplus": {
        "key": "surplus", "label": "Surplus", "short": "Surplus",
        "proportional": True,
        "blurb": "You keep a fixed line per risk and cede what is above it.",
        "fields": ["retention_line", "lines", "event_limit",
                   "ceding_commission_pct"],
        "required": ["currency", "retention_line", "lines"],
        "layer_fields": [], "layer_required": [], "multi_layer": False,
        "default_layer": "Surplus",
    },
    "xol": {
        "key": "xol", "label": "Excess of Loss", "short": "XOL",
        "proportional": False,
        "blurb": "Losses above a retention are paid up to a limit, layer by layer.",
        "fields": ["cover_basis"],
        "required": ["currency", "cover_basis"],
        "layer_fields": ["retention", "limit", "reinstatements",
                         "reinstatement_premium_pct", "rate_on_line_pct",
                         "deposit_premium"],
        "layer_required": ["retention", "limit"],
        "multi_layer": True,
        "default_layer": "Layer 1",
    },
    "stop_loss": {
        "key": "stop_loss", "label": "Stop Loss", "short": "Stop loss",
        "proportional": False,
        "blurb": "Pays once the year's losses pass an attachment point.",
        "fields": ["measure", "subject_premium"],
        "required": ["currency", "measure"],
        "layer_fields": ["attachment_pct", "limit_pct", "attachment_amount",
                         "limit_amount", "rate_on_line_pct"],
        "layer_required": ["attachment_pct", "limit_pct", "attachment_amount",
                           "limit_amount"],
        "multi_layer": False,
        "default_layer": "Stop loss",
    },
    "facultative": {
        "key": "facultative", "label": "Facultative", "short": "Fac",
        "proportional": True,
        "blurb": "One risk, placed on its own.",
        "fields": ["risk_reference", "insured_name", "sum_insured",
                   "share_ceded_pct", "premium", "ceding_commission_pct"],
        "required": ["currency", "risk_reference", "sum_insured",
                     "share_ceded_pct"],
        "layer_fields": [], "layer_required": [], "multi_layer": False,
        "default_layer": "Facultative",
    },
}

# A share is a percentage of a LAYER, so a layer is fully placed at 100.
FULL = 100.0
# Floating point: 33.33 + 33.33 + 33.34 must read as placed, not 99.99999.
_EPS = 0.005


class TreatyError(ValueError):
    """A structure that cannot be saved, with the fields to point at."""

    def __init__(self, message: str, errors: dict[str, str] | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or {}

    def to_detail(self) -> dict:
        return {"message": self.message, "errors": self.errors}


# ── the spec the form is built from ──────────────────────────────────────────
def _field_out(name: str, f: dict, required: bool) -> dict:
    out = {"name": name, "label": f["label"], "kind": f["kind"],
           "hint": f.get("hint"), "example": f.get("example"),
           "required": required}
    if f.get("options"):
        out["options"] = f["options"]
    if f.get("when"):
        out["when"] = f["when"]
    return out


def describe() -> list[dict]:
    out = []
    for t in TREATY_TYPES.values():
        names = COMMON_TREATY + t["fields"]
        out.append({
            "key": t["key"], "label": t["label"], "short": t["short"],
            "blurb": t["blurb"], "proportional": t["proportional"],
            "multi_layer": t["multi_layer"],
            "default_layer": t["default_layer"],
            "fields": [_field_out(n, TREATY_FIELDS[n], n in t["required"])
                       for n in names],
            "layer_fields": [_field_out(n, LAYER_FIELDS[n],
                                        n in t["layer_required"])
                             for n in t["layer_fields"]],
        })
    return out


def label(treaty_type: str | None) -> str | None:
    t = TREATY_TYPES.get(treaty_type or "")
    return t["label"] if t else None


# ── cleaning ─────────────────────────────────────────────────────────────────
def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _num(v: Any) -> float | None:
    if _blank(v):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _applies(f: dict, terms: dict) -> bool:
    w = f.get("when")
    return not w or terms.get(w["field"]) == w["equals"]


def _clean_value(name: str, f: dict, raw: Any, errors: dict, path: str) -> Any:
    if _blank(raw):
        return None
    kind = f["kind"]
    if kind in ("money", "pct"):
        n = _num(raw)
        if n is None:
            errors[path] = f"{f['label']} has to be a number."
            return None
        if n < 0:
            errors[path] = f"{f['label']} cannot be negative."
            return None
        if kind == "pct" and n > 100 and name not in ("attachment_pct",
                                                      "limit_pct"):
            # A loss ratio can pass 100 — that is the whole point of a stop
            # loss attaching at 110%. Every other percentage here is a share.
            errors[path] = f"{f['label']} cannot be more than 100%."
            return None
        return n
    if kind == "int":
        n = _num(raw)
        if n is None or n != int(n) or n < 0:
            errors[path] = f"{f['label']} has to be a whole number."
            return None
        return int(n)
    if kind == "select":
        allowed = {o["value"] for o in f.get("options") or []}
        if raw not in allowed:
            errors[path] = f"{f['label']} is not one of the choices."
            return None
        return raw
    s = str(raw).strip()
    if name == "currency":
        s = s.upper()
        if len(s) != 3 or not s.isalpha():
            errors[path] = "Use a three-letter currency code, like USD."
            return None
    return s


def validate(structure: dict, *, reinsurer_ids: set[int] | None = None,
             strict: bool = True) -> dict:
    """Check a structure and return it cleaned.

    `reinsurer_ids` is the set of party ids the caller may name on a panel
    (the carrier's own reinsurers). Passed in rather than looked up here so this
    module stays free of the database.

    Raises TreatyError naming every field at fault, with dotted paths the form
    can attach to the right box: `terms.cession_pct`, `layers.1.limit`,
    `layers.0.shares.2.share_pct`.

    `strict=False` is for the live wording preview, which runs on every
    keystroke of a half-filled form: it returns whatever is already sound and
    drops the rest instead of refusing, so the clauses follow the typing.
    """
    structure = structure or {}
    ttype = structure.get("treaty_type")
    t = TREATY_TYPES.get(ttype or "")
    if not t:
        if not strict:
            return {"treaty_type": None, "terms": {}, "layers": []}
        raise TreatyError("Pick a treaty type.", {"treaty_type": "required"})

    errors: dict[str, str] = {}
    raw_terms = structure.get("terms") or {}
    terms: dict[str, Any] = {}
    for name in COMMON_TREATY + t["fields"]:
        f = TREATY_FIELDS[name]
        v = _clean_value(name, f, raw_terms.get(name), errors, f"terms.{name}")
        if v is not None:
            terms[name] = v
    for name in t["required"]:
        if name not in terms and f"terms.{name}" not in errors:
            errors[f"terms.{name}"] = f"{TREATY_FIELDS[name]['label']} is required."

    raw_layers = [lay for lay in (structure.get("layers") or [])
                  if isinstance(lay, dict)]
    if not raw_layers:
        raw_layers = [{"name": t["default_layer"]}]
    if not t["multi_layer"] and len(raw_layers) > 1:
        errors["layers"] = f"A {t['label']} treaty has a single layer."
        raw_layers = raw_layers[:1]

    layers: list[dict] = []
    for i, raw in enumerate(raw_layers):
        lay_terms: dict[str, Any] = {}
        raw_lt = raw.get("terms") or {}
        for name in t["layer_fields"]:
            f = LAYER_FIELDS[name]
            if not _applies(f, terms):
                continue
            v = _clean_value(name, f, raw_lt.get(name), errors,
                             f"layers.{i}.{name}")
            if v is not None:
                lay_terms[name] = v
        for name in t["layer_required"]:
            f = LAYER_FIELDS[name]
            if not _applies(f, terms):
                continue
            if name not in lay_terms and f"layers.{i}.{name}" not in errors:
                errors[f"layers.{i}.{name}"] = f"{f['label']} is required."
        if ("limit" in lay_terms and lay_terms["limit"] == 0):
            errors[f"layers.{i}.limit"] = "A layer with no limit pays nothing."

        shares: list[dict] = []
        seen: set[int] = set()
        leads = 0
        for j, sh in enumerate(raw.get("shares") or []):
            if not isinstance(sh, dict):
                continue
            path = f"layers.{i}.shares.{j}"
            pid = sh.get("reinsurer_party_id")
            try:
                pid = int(pid) if pid not in (None, "") else None
            except (TypeError, ValueError):
                pid = None
            if pid is None:
                errors[f"{path}.reinsurer_party_id"] = "Pick a reinsurer."
                continue
            if reinsurer_ids is not None and pid not in reinsurer_ids:
                errors[f"{path}.reinsurer_party_id"] = (
                    "That organisation is not one of your reinsurers.")
                continue
            if pid in seen:
                errors[f"{path}.reinsurer_party_id"] = (
                    "This reinsurer is already on this layer — give them one "
                    "line with their whole share.")
                continue
            seen.add(pid)
            pct = _num(sh.get("share_pct"))
            if pct is None or pct <= 0:
                errors[f"{path}.share_pct"] = "A share has to be more than 0%."
                continue
            if pct > FULL + _EPS:
                errors[f"{path}.share_pct"] = "A share cannot be more than 100%."
                continue
            lead = bool(sh.get("is_lead"))
            leads += lead
            shares.append({
                "reinsurer_party_id": pid, "share_pct": round(pct, 4),
                "is_lead": lead,
                "slip_reference": (str(sh.get("slip_reference") or "").strip()
                                   or None),
            })
        placed = sum(s["share_pct"] for s in shares)
        if placed > FULL + _EPS:
            errors[f"layers.{i}.shares"] = (
                f"The shares on this layer add up to {placed:g}%. A layer "
                f"cannot be placed beyond 100%.")
        if leads > 1:
            errors[f"layers.{i}.shares"] = "Only one reinsurer can lead a layer."
        name = str(raw.get("name") or "").strip() or (
            f"Layer {i + 1}" if t["multi_layer"] else t["default_layer"])
        layers.append({"name": name, "terms": lay_terms, "shares": shares})

    if errors and strict:
        n = len(errors)
        raise TreatyError(
            "The treaty structure has "
            + ("a problem" if n == 1 else f"{n} problems")
            + " to fix before it can be saved.", errors)

    return {"treaty_type": ttype, "terms": terms, "layers": layers}


# ── reading it back ──────────────────────────────────────────────────────────
def money(v: Any, currency: str | None = None) -> str:
    n = _num(v)
    if n is None:
        return "—"
    if n >= 1_000_000 and n % 100_000 == 0:
        body = f"{n / 1_000_000:g}m"
    elif n >= 1_000 and n % 1_000 == 0 and n < 1_000_000:
        body = f"{n / 1_000:g}k"
    else:
        body = f"{n:,.0f}" if n == int(n) else f"{n:,.2f}"
    return f"{currency} {body}".strip() if currency else body


def full_money(v: Any, currency: str | None = None) -> str:
    """The long form, for the wording — a contract does not say "5m"."""
    n = _num(v)
    if n is None:
        return "—"
    body = f"{n:,.0f}" if n == int(n) else f"{n:,.2f}"
    return f"{currency} {body}".strip() if currency else body


def pct(v: Any) -> str:
    n = _num(v)
    return "—" if n is None else f"{n:g}%"


def layer_facts(structure: dict | None, layer: dict) -> list[dict]:
    """One layer's own numbers, in words, for the panel table and the
    reinsurer's view. Only fields that apply to this type and were filled."""
    t = TREATY_TYPES.get((structure or {}).get("treaty_type") or "")
    if not t:
        return []
    terms = (structure or {}).get("terms") or {}
    cur = terms.get("currency")
    lt = layer.get("terms") or {}
    out = []
    for name in t["layer_fields"]:
        f = LAYER_FIELDS[name]
        v = lt.get(name)
        if v is None or not _applies(f, terms):
            continue
        val = (money(v, cur) if f["kind"] == "money"
               else pct(v) if f["kind"] == "pct" else str(v))
        out.append({"label": f["label"], "value": val})
    return out


def fingerprint(structure: dict | None) -> str:
    """A short hash of what a reinsurer is agreeing to: the type, the terms,
    and every layer's terms and panel. Database ids and display names are left
    out, so re-saving the same structure gives the same fingerprint, and any
    real change — a share, a limit, a reinsurer — gives a different one."""
    import hashlib
    import json
    st = structure or {}

    def num(v):
        n = _num(v)
        return round(n, 4) if n is not None else v

    shaped = {
        "t": st.get("treaty_type"),
        "terms": {k: num(v) for k, v in sorted((st.get("terms") or {}).items())},
        "layers": [{
            "n": lay.get("name"),
            "terms": {k: num(v) for k, v in sorted((lay.get("terms") or {}).items())},
            "shares": sorted(
                [[int(sh.get("reinsurer_party_id") or 0),
                  num(sh.get("share_pct")), bool(sh.get("is_lead"))]
                 for sh in lay.get("shares") or []]),
        } for lay in st.get("layers") or []],
    }
    raw = json.dumps(shaped, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def lead_of(structure: dict | None) -> int | None:
    """The reinsurer starred as lead on the FIRST layer — who the treaty is
    with. On a treaty the panel names the counterparty, so the contract's
    counterparty follows this rather than being picked separately."""
    layers = (structure or {}).get("layers") or []
    if not layers:
        return None
    for sh in layers[0].get("shares") or []:
        if sh.get("is_lead") and sh.get("reinsurer_party_id") not in (None, ""):
            try:
                return int(sh["reinsurer_party_id"])
            except (TypeError, ValueError):
                return None
    return None


def placed_pct(layer: dict) -> float:
    return round(sum(float(s.get("share_pct") or 0)
                     for s in layer.get("shares") or []), 4)


def summarise(structure: dict | None) -> dict | None:
    """What the screens show: a one-line headline, the key facts, how placed
    it is, and either a TOWER (non-proportional: bands stacked by amount) or a
    SPLIT (proportional: ceded against retained). Never both — a quota share
    drawn as a tower would be a picture of something it is not.
    """
    if not structure or structure.get("treaty_type") not in TREATY_TYPES:
        return None
    t = TREATY_TYPES[structure["treaty_type"]]
    terms = structure.get("terms") or {}
    layers = structure.get("layers") or []
    cur = terms.get("currency")
    facts: list[dict] = []
    tower = None
    split = None
    warnings: list[str] = []

    def fact(lbl: str, val: str | None):
        if val and val != "—":
            facts.append({"label": lbl, "value": val})

    key = t["key"]
    if key == "quota_share":
        ced = _num(terms.get("cession_pct"))
        headline = (f"{pct(ced)} quota share" if ced is not None
                    else "Quota share")
        if ced is not None:
            split = {"ceded_pct": ced, "retained_pct": round(FULL - ced, 4)}
        fact("Cession", pct(ced))
        fact("Retained", pct(FULL - ced) if ced is not None else None)
        fact("Limit per risk", money(terms.get("treaty_limit"), cur))
        fact("Event limit", money(terms.get("event_limit"), cur))
        fact("Ceding commission", pct(terms.get("ceding_commission_pct")))
        fact("Profit commission", pct(terms.get("profit_commission_pct")))
    elif key == "surplus":
        line = _num(terms.get("retention_line"))
        n = terms.get("lines")
        cap = line * n if line is not None and n else None
        headline = (f"{n}-line surplus on {money(line, cur)}"
                    if line is not None and n else "Surplus")
        fact("Retention line", money(line, cur))
        fact("Lines", str(n) if n else None)
        fact("Treaty capacity", money(cap, cur))
        fact("Max per risk", money(line + cap, cur)
             if line is not None and cap is not None else None)
        fact("Ceding commission", pct(terms.get("ceding_commission_pct")))
        if line is not None and cap:
            tower = {"unit": "money", "currency": cur, "bands": [
                {"label": "Your retention", "kind": "retention",
                 "from": 0, "to": line, "placed_pct": None},
                {"label": f"{n} lines ceded", "kind": "layer",
                 "from": line, "to": line + cap, "layer": 0,
                 "placed_pct": placed_pct(layers[0]) if layers else 0},
            ]}
    elif key == "xol":
        basis = {"per_risk": "per risk", "per_event": "per event"}.get(
            terms.get("cover_basis"), "")
        bands = []
        ordered = sorted(
            [{**lay, "_i": i} for i, lay in enumerate(layers)
             if _num((lay.get("terms") or {}).get("retention")) is not None],
            key=lambda lay: _num(lay["terms"]["retention"]))
        if ordered:
            first = _num(ordered[0]["terms"]["retention"])
            bands.append({"label": "Your retention", "kind": "retention",
                          "from": 0, "to": first, "placed_pct": None})
        prev_top = None
        for lay in ordered:
            r = _num(lay["terms"]["retention"])
            lim = _num(lay["terms"].get("limit")) or 0
            if prev_top is not None and r > prev_top + _EPS:
                warnings.append(
                    f"There is a gap between {money(prev_top, cur)} and "
                    f"{money(r, cur)}: no layer pays losses in that band.")
            elif prev_top is not None and r < prev_top - _EPS:
                warnings.append(
                    f"{lay['name']} starts at {money(r, cur)}, below the top "
                    f"of the layer under it ({money(prev_top, cur)}), so the "
                    f"two overlap.")
            bands.append({"label": f"{lay['name']}: {money(lim, cur)} xs "
                                   f"{money(r, cur)}",
                          "kind": "layer", "from": r, "to": r + lim,
                          "layer": lay["_i"], "placed_pct": placed_pct(lay)})
            prev_top = max(prev_top or 0, r + lim)
        if bands:
            tower = {"unit": "money", "currency": cur, "bands": bands}
        if ordered:
            bottom = _num(ordered[0]["terms"]["retention"])
            headline = (f"{money(prev_top - bottom, cur)} xs "
                        f"{money(bottom, cur)}"
                        + (f" {basis}" if basis else "")
                        + (f" in {len(ordered)} layers" if len(ordered) > 1
                           else ""))
        else:
            headline = "Excess of loss"
        fact("Cover basis", basis.capitalize() if basis else None)
        fact("Layers", str(len(layers)))
        fact("Top of programme", money(prev_top, cur) if prev_top else None)
    elif key == "stop_loss":
        lt = (layers[0].get("terms") if layers else {}) or {}
        if terms.get("measure") == "loss_ratio":
            a, lim = _num(lt.get("attachment_pct")), _num(lt.get("limit_pct"))
            headline = (f"{pct(lim)} xs {pct(a)} loss ratio"
                        if a is not None and lim is not None else "Stop loss")
            fact("Attaches at", f"{pct(a)} loss ratio" if a is not None else None)
            fact("Cover", f"{lim:g} loss-ratio points" if lim is not None else None)
            fact("Subject premium", money(terms.get("subject_premium"), cur))
            if a is not None and lim is not None:
                tower = {"unit": "pct", "currency": None, "bands": [
                    {"label": "Your retention", "kind": "retention",
                     "from": 0, "to": a, "placed_pct": None},
                    {"label": f"{pct(lim)} xs {pct(a)}", "kind": "layer",
                     "from": a, "to": a + lim, "layer": 0,
                     "placed_pct": placed_pct(layers[0])},
                ]}
        else:
            a = _num(lt.get("attachment_amount"))
            lim = _num(lt.get("limit_amount"))
            headline = (f"{money(lim, cur)} xs {money(a, cur)} aggregate"
                        if a is not None and lim is not None else "Stop loss")
            fact("Attaches at", money(a, cur))
            fact("Cover", money(lim, cur))
            if a is not None and lim is not None:
                tower = {"unit": "money", "currency": cur, "bands": [
                    {"label": "Your retention", "kind": "retention",
                     "from": 0, "to": a, "placed_pct": None},
                    {"label": f"{money(lim, cur)} xs {money(a, cur)}",
                     "kind": "layer", "from": a, "to": a + lim, "layer": 0,
                     "placed_pct": placed_pct(layers[0])},
                ]}
        fact("Rate on line", pct(lt.get("rate_on_line_pct")))
    else:  # facultative
        sh = _num(terms.get("share_ceded_pct"))
        si = terms.get("sum_insured")
        headline = (f"{pct(sh)} of {money(si, cur)} on "
                    f"{terms.get('risk_reference')}"
                    if sh is not None and si else "Facultative")
        if sh is not None:
            split = {"ceded_pct": sh, "retained_pct": round(FULL - sh, 4)}
        fact("Risk", terms.get("risk_reference"))
        fact("Insured", terms.get("insured_name"))
        fact("Sum insured", money(si, cur))
        fact("Share ceded", pct(sh))
        fact("Premium", money(terms.get("premium"), cur))
        fact("Ceding commission", pct(terms.get("ceding_commission_pct")))

    if terms.get("basis"):
        fact("Basis", {"risks_attaching": "Risks attaching",
                       "losses_occurring": "Losses occurring"}
             .get(terms["basis"]))
    fact("Reinsurance broker", terms.get("reinsurance_broker"))
    fact("Placement reference", terms.get("placement_reference"))

    per_layer = [{"name": lay.get("name"), "placed_pct": placed_pct(lay)}
                 for lay in layers]
    overall = (round(min(p["placed_pct"] for p in per_layer), 4)
               if per_layer else 0.0)
    for p in per_layer:
        if p["placed_pct"] < FULL - _EPS:
            warnings.append(f"{p['name']} is {p['placed_pct']:g}% placed — "
                            f"{FULL - p['placed_pct']:g}% has no reinsurer.")
    return {
        "treaty_type": key, "type_label": t["label"], "type_short": t["short"],
        "proportional": t["proportional"],
        "currency": cur, "headline": headline, "facts": facts,
        "tower": tower, "split": split,
        "layers_placed": per_layer,
        # The least-placed layer, because "placed" is only as true as the
        # weakest layer: a programme 100% placed on layer one and 40% on layer
        # two is not placed.
        "placed_pct": overall,
        "fully_placed": bool(per_layer) and overall >= FULL - _EPS,
        "warnings": warnings,
    }


# ── wording ──────────────────────────────────────────────────────────────────
# Sections this module owns inside a contract's wording. Every one of them is
# rewritten whenever the structure changes, so the wording and the structure
# cannot say different things — the same promise the token system makes for
# agreed limits, kept here by regeneration rather than by tokens, because a
# layer table is not a sentence with a blank in it.
SECTION_PREFIX = "treaty_"
SECTION_ORIGIN = "from your treaty structure"


def build_sections(structure: dict | None) -> list[dict]:
    """The treaty's clauses, numbered from 1; contract_wording.renumber puts
    them in their real place once they are merged.

    REINSURERS ARE NOT NAMED IN THE WORDING. The panel is confidential between
    markets: each reinsurer may see its own line and nobody else's, and a
    wording that listed them all would undo that the moment it was shared.
    It says the panel exists and where it is recorded.

    NOR ARE THE PROGRAMMES. Which programmes a treaty protects can change after
    it is signed (26.2 — a treaty is attached to a new programme mid-term), and
    a signed wording cannot follow that. The link lives on the record.
    """
    if not structure or structure.get("treaty_type") not in TREATY_TYPES:
        return []
    t = TREATY_TYPES[structure["treaty_type"]]
    terms = structure.get("terms") or {}
    layers = structure.get("layers") or []
    cur = terms.get("currency")
    m = lambda v: full_money(v, cur)  # noqa: E731
    out: list[dict] = []

    def add(key: str, title: str, lines: list[str]):
        kept = [ln for ln in lines if ln]
        if not kept:
            return
        n = len(out) + 1
        out.append({"key": SECTION_PREFIX + key, "title": title,
                    "body": "\n".join(f"{n}.{i}  {ln}"
                                      for i, ln in enumerate(kept, start=1)),
                    "origin": SECTION_ORIGIN, "locked": False})

    basis = {"risks_attaching": "on a risks-attaching basis",
             "losses_occurring": "on a losses-occurring basis"}.get(
        terms.get("basis"), "")
    add("nature", "Nature of this treaty", [
        f"This is a {t['label']} reinsurance treaty"
        + (f", written {basis}" if basis else "") + ".",
        f"All amounts in this treaty are in {cur}." if cur else "",
    ])

    key = t["key"]
    if key == "quota_share":
        add("cession", "Cession and retention", [
            f"The Reinsured cedes {pct(terms.get('cession_pct'))} of every "
            f"risk falling within this treaty and retains the remaining "
            f"{pct(FULL - (_num(terms.get('cession_pct')) or 0))} for its own "
            f"account.",
            f"The Reinsurers' liability on any one risk shall not exceed "
            f"{m(terms.get('treaty_limit'))}."
            if terms.get("treaty_limit") is not None else "",
            f"Their liability for all risks arising from any one event shall "
            f"not exceed {m(terms.get('event_limit'))}."
            if terms.get("event_limit") is not None else "",
        ])
    elif key == "surplus":
        line = _num(terms.get("retention_line")) or 0
        n = terms.get("lines") or 0
        add("cession", "Retention and surplus", [
            f"The Reinsured retains {m(line)} on any one risk (one line).",
            f"The Reinsurers accept the surplus above that retention, up to "
            f"{n} lines, being {m(line * n)} on any one risk.",
            f"Their liability for all risks arising from any one event shall "
            f"not exceed {m(terms.get('event_limit'))}."
            if terms.get("event_limit") is not None else "",
        ])
    elif key == "xol":
        basis_txt = {"per_risk": "any one risk",
                     "per_event": "any one event"}.get(
            terms.get("cover_basis"), "any one loss")
        lines = []
        for lay in layers:
            lt = lay.get("terms") or {}
            ln = (f"{lay.get('name')}: the Reinsurers pay the ultimate net "
                  f"loss in excess of {m(lt.get('retention'))} arising from "
                  f"{basis_txt}, up to a further {m(lt.get('limit'))}.")
            if lt.get("reinstatements") is not None:
                ln += (f" The limit may be reinstated {lt['reinstatements']} "
                       f"time{'s' if lt['reinstatements'] != 1 else ''}"
                       + (f" at {pct(lt.get('reinstatement_premium_pct'))} "
                          f"additional premium"
                          if lt.get("reinstatement_premium_pct") is not None
                          else "") + ".")
            lines.append(ln)
        add("layers", "Layers of cover", lines)
        prem = [f"{lay.get('name')}: rate on line "
                f"{pct((lay.get('terms') or {}).get('rate_on_line_pct'))}"
                + (f", deposit premium "
                   f"{m((lay.get('terms') or {}).get('deposit_premium'))}"
                   if (lay.get("terms") or {}).get("deposit_premium")
                   is not None else "") + "."
                for lay in layers
                if (lay.get("terms") or {}).get("rate_on_line_pct") is not None]
        add("premium", "Reinsurance premium", prem)
    elif key == "stop_loss":
        lt = (layers[0].get("terms") if layers else {}) or {}
        if terms.get("measure") == "loss_ratio":
            add("cover", "Stop loss cover", [
                f"The Reinsurers pay losses for the treaty year once the "
                f"Reinsured's loss ratio exceeds {pct(lt.get('attachment_pct'))}, "
                f"up to a further {_num(lt.get('limit_pct')) or 0:g} "
                f"loss-ratio points.",
                f"The loss ratio is taken on a subject premium of "
                f"{m(terms.get('subject_premium'))}."
                if terms.get("subject_premium") is not None else "",
            ])
        else:
            add("cover", "Stop loss cover", [
                f"The Reinsurers pay the Reinsured's aggregate net losses for "
                f"the treaty year in excess of "
                f"{m(lt.get('attachment_amount'))}, up to a further "
                f"{m(lt.get('limit_amount'))}.",
            ])
    else:
        add("risk", "The risk reinsured", [
            f"This certificate reinsures risk {terms.get('risk_reference')}"
            + (f", the insured being {terms.get('insured_name')}"
               if terms.get("insured_name") else "") + ".",
            f"The sum insured is {m(terms.get('sum_insured'))}, of which the "
            f"Reinsurers accept {pct(terms.get('share_ceded_pct'))}.",
            f"The reinsurance premium is {m(terms.get('premium'))}."
            if terms.get("premium") is not None else "",
        ])

    add("commission", "Commission", [
        f"The Reinsurers allow the Reinsured a ceding commission of "
        f"{pct(terms.get('ceding_commission_pct'))} on premium ceded."
        if terms.get("ceding_commission_pct") is not None else "",
        f"A profit commission of {pct(terms.get('profit_commission_pct'))} of "
        f"the Reinsurers' profit on this treaty is payable to the Reinsured."
        if terms.get("profit_commission_pct") is not None else "",
    ])

    add("panel", "Subscribing reinsurers", [
        "Each Reinsurer is liable only for its own share of each layer, as "
        "stated in its signed line, and not for the share of any other "
        "Reinsurer.",
        "The subscribing Reinsurers and their shares are recorded in the "
        "placement schedule held by the Reinsured"
        + (f" under reference {terms.get('placement_reference')}"
           if terms.get("placement_reference") else "") + ".",
        f"This treaty was placed through {terms.get('reinsurance_broker')}."
        if terms.get("reinsurance_broker") else "",
    ])
    return out


def merge_sections(sections: list[dict] | None,
                   treaty_sections: list[dict]) -> list[dict]:
    """The wording with this treaty's clauses in it, replacing any earlier ones.

    Placed straight after the parties section, where a reader expects to learn
    what is being reinsured. Everything the carrier wrote is left as it is; only
    sections this module owns are taken out and put back.
    """
    import contract_wording as cw
    kept = [sec for sec in (sections or [])
            if isinstance(sec, dict)
            and not str(sec.get("key") or "").startswith(SECTION_PREFIX)]
    at = next((i + 1 for i, sec in enumerate(kept)
               if sec.get("key") == "parties"), 0)
    merged = kept[:at] + list(treaty_sections) + kept[at:]
    return cw.renumber(merged)
