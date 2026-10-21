"""
A treaty's rules — what a reinsurance bordereau is checked against, the way a
broker's bordereau is checked against its contract.

WHERE THEY COME FROM
  terms     worked out from the treaty's own structure, no model call: the
            period it covers (by its attachment basis), its currency, the data
            its type needs, its limit per risk or capacity. They follow the
            treaty — change a term and the rule changes with it.
  wording   read from the treaty's wording by the model (extract()): excluded
            classes and territories, the territory it covers, the most it
            takes on any one risk — each with the clause it came from.

Each rule is on or off and Critical or Warning, set on the setup's Treaty
Rules step. Critical stops a statement being sent until it is dealt with;
Warning is shown and sent anyway.

check() runs them over the rows a statement is read from (canonical fields,
before anything is ceded) and reports each rule that failed, how many rows,
and a few of them. Reading never writes; ceded_routes saves.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any, Callable, Optional

import ceded_engine as ce

log = logging.getLogger("kavachio.treaty.rules")

KINDS = ("within_term", "currency", "required", "max", "excluded", "allowed",
         "duplicate")
SEVERITIES = ("critical", "warning")

# What a rule's subject is read from, in the order the fields are tried.
SUBJECT_FIELDS: dict[str, tuple[str, ...]] = {
    "inception": ("policy_effective_date",),
    "loss_date": ("claim_loss_date",),
    "currency": ce.CURRENCY,
    "premium": ce.PREMIUM,
    "sum_insured": ce.SUM_INSURED,
    "claims": ce.CLAIM_INCURRED + ce.CLAIM_PAID + ce.CLAIM_RESERVE,
    "policy": ce.POLICY,
    "territory": ("risk_location_country", "policy_risk_country",
                  "risk_location_subdivision", "policy_risk_subdivision"),
    "class_of_business": ("contract_class_of_business", "coverage_code",
                          "coverage_type"),
}
SUBJECT_LABEL = {
    "inception": "policy inception", "loss_date": "date of loss",
    "currency": "currency", "premium": "premium", "sum_insured": "sum insured",
    "claims": "claim amount", "policy": "policy number",
    "territory": "territory", "class_of_business": "class of business",
}


def _rule(rid: str, label: str, kind: str, subject: str, *, severity: str,
          source: str = "terms", clause: Optional[str] = None, **params) -> dict:
    return {"id": rid, "label": label, "kind": kind, "subject": subject,
            "params": params, "severity": severity, "enabled": True,
            "source": source, "clause": clause}


def _money(v: float) -> str:
    return f"{v:,.0f}"


# ── from the limits agreed on the treaty ────────────────────────────────────
def from_limits(limits: Optional[dict], currency: Optional[str] = None) -> list[dict]:
    """The treaty's agreed limits (contract_types.TREATY_LIMITS) as rules on
    its reinsurance bordereau, at the severity set on each limit."""
    import contract_types as ct
    out = []
    for key, entry in ct.treaty_limits(limits).items():
        rule = ct.TREATY_LIMITS[key].get("rule")
        if not rule:
            continue
        kind, subject = rule
        v, sev = entry["value"], _severity(entry.get("severity"), "warning")
        q = ct.TREATY_LIMITS[key]["question"]
        clause = f"Agreed limit — {q}: " + (f"{(currency + ' ') if currency else ''}{_money(ce.num(v))}"
                                             if kind == "max" else str(v))
        if kind == "max":
            n = ce.num(v)
            if n is None:
                continue
            what = ("referred to the lead reinsurer above" if key == "referral_threshold"
                    else "at most")
            out.append(_rule(f"limit_{key}",
                             f"Sum insured {what} {(currency + ' ') if currency else ''}{_money(n)}",
                             kind, subject, severity=sev, clause=clause, value=n))
        else:
            vals = [x.strip() for x in re.split(r";|,| and ", str(v)) if x.strip()]
            if not vals:
                continue
            noun = SUBJECT_LABEL[subject]
            label = (f"{noun[:1].upper()}{noun[1:]} is one of: {', '.join(vals)}" if kind == "allowed"
                     else f"{noun[:1].upper()}{noun[1:]} is not {', '.join(vals)}")
            out.append(_rule(f"limit_{key}", label, kind, subject, severity=sev,
                             clause=clause, values=vals))
    return out


# ── from the treaty's terms ─────────────────────────────────────────────────
def derive(st: dict, inception: Optional[dt.date],
           expiry: Optional[dt.date], limits: Optional[dict] = None) -> list[dict]:
    """The rules a treaty's structure implies — and its agreed limits, when
    given. Same structure and limits, same rules."""
    ttype = st.get("treaty_type")
    terms = st.get("terms") or {}
    cur = (terms.get("currency") or "").strip().upper()
    out: list[dict] = []

    if inception and expiry:
        losses = terms.get("basis") == "losses_occurring" or ttype in ("xol", "stop_loss")
        subject = "loss_date" if losses else "inception"
        out.append(_rule(
            "term", f"{'Loss' if losses else 'Policy inception'} within the treaty "
            f"period ({inception:%d %b %Y} – {expiry:%d %b %Y})",
            "within_term", subject, severity="critical",
            start=inception.isoformat(), end=expiry.isoformat()))
    if cur:
        out.append(_rule("currency", f"Written in the treaty currency ({cur})",
                         "currency", "currency", severity="critical", value=cur))

    need = {"quota_share": ["premium"], "surplus": ["premium", "sum_insured"],
            "facultative": ["premium"], "xol": ["claims"], "stop_loss": ["claims"]}
    for subj in need.get(ttype, []):
        out.append(_rule(f"required_{subj}", f"Every row has a {SUBJECT_LABEL[subj]}",
                         "required", subj, severity="critical"))

    if ttype == "quota_share":
        lim, pct = ce.num(terms.get("treaty_limit")), ce.num(terms.get("cession_pct"))
        if lim and pct:
            cap = lim / (pct / 100)
            out.append(_rule(
                "limit_per_risk", f"Sum insured within the limit per risk "
                f"({cur} {_money(lim)} ceded = {cur} {_money(cap)} sum insured)",
                "max", "sum_insured", severity="warning", value=round(cap, 2)))
    if ttype == "surplus":
        line, n = ce.num(terms.get("retention_line")), ce.num(terms.get("lines"))
        if line and n:
            cap = line * (n + 1)
            out.append(_rule(
                "capacity", f"Sum insured within the treaty's capacity "
                f"({cur} {_money(cap)}: retention + {n:g} lines)",
                "max", "sum_insured", severity="warning", value=round(cap, 2)))
    if ttype in ("quota_share", "surplus"):
        out.append(_rule("duplicate", "Each policy appears once in the period",
                         "duplicate", "policy", severity="warning"))
    return out + from_limits(limits, cur or None)


def merge(saved: Optional[list], derived: list[dict]) -> list[dict]:
    """The rules as they stand: the terms rules worked out afresh (so they
    follow the treaty) keeping the switch and severity the carrier set; the
    wording rules as saved."""
    by_id = {r.get("id"): r for r in saved or []}
    out = []
    for r in derived:
        prev = by_id.get(r["id"])
        if prev:
            r = {**r, "enabled": bool(prev.get("enabled", True)),
                 "severity": _severity(prev.get("severity"), r["severity"])}
        out.append(r)
    out += [r for r in saved or [] if r.get("source") == "wording"]
    return out


def clean(rules: list[dict]) -> list[dict]:
    """A rule list from the screen, kept to what check() understands."""
    out = []
    for r in rules or []:
        if r.get("kind") not in KINDS or r.get("subject") not in SUBJECT_FIELDS:
            continue
        out.append({
            "id": str(r.get("id") or "")[:60], "label": str(r.get("label") or "")[:240],
            "kind": r["kind"], "subject": r["subject"],
            "params": r.get("params") if isinstance(r.get("params"), dict) else {},
            "severity": _severity(r.get("severity"), "warning"),
            "enabled": bool(r.get("enabled", True)),
            "source": "wording" if r.get("source") == "wording" else "terms",
            "clause": (str(r["clause"])[:600] if r.get("clause") else None)})
    return out


# ── from the treaty's wording ───────────────────────────────────────────────
PROMPT = """You read a REINSURANCE TREATY's wording and list the conditions a
row of the ceded business must meet. Only conditions the wording states — never
guess, never restate commercial terms like cession % or commission.

Return ONLY this JSON:
{"rules": [
  {"kind": "excluded" | "allowed" | "max",
   "subject": "territory" | "class_of_business" | "sum_insured",
   "values": ["for excluded/allowed: the items, as written"],
   "value": number or null,        // for max: the amount
   "label": "a short plain sentence, e.g. 'Marine cargo is excluded'",
   "clause": "the sentence of the wording it comes from, quoted"}
]}

Use "excluded" for business the treaty does not cover (excluded classes,
excluded territories), "allowed" for the territory or classes it is limited to,
and "max" for the most it takes on any one risk (as a sum insured). Return
{"rules": []} when the wording states none.

WORDING:
"""


def extract(text: str, *, call: Optional[Callable[[str], str]] = None) -> list[dict]:
    """The wording's conditions as rules. `call` is injectable for tests."""
    if not (text or "").strip():
        return []
    if call is None:
        from contract_upload_services.gemini_service import call_gemini

        def call(prompt: str) -> str:
            return call_gemini(prompt, label="TREATY_RULES", temperature=0, seed=7,
                               thinking_budget=2048, max_output_tokens=4096)
    raw = call(PROMPT + text[:60000])
    try:
        got = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (ValueError, TypeError):
        log.warning("treaty rules: model returned no JSON")
        return []
    out = []
    for i, r in enumerate(got.get("rules") or []):
        if not isinstance(r, dict):
            continue
        kind, subj = r.get("kind"), r.get("subject")
        if kind not in ("excluded", "allowed", "max") or subj not in SUBJECT_FIELDS:
            continue
        params: dict[str, Any] = {}
        if kind == "max":
            v = ce.num(r.get("value"))
            if v is None:
                continue
            params["value"] = v
        else:
            vals = [str(x).strip() for x in r.get("values") or [] if str(x).strip()]
            if not vals:
                continue
            params["values"] = vals
        label = str(r.get("label") or "").strip() or (
            f"{SUBJECT_LABEL[subj].capitalize()} {kind}: " + ", ".join(params.get("values", [])))
        out.append(_rule(f"wording_{i + 1}_{subj}_{kind}", label, kind, subj,
                         severity="critical" if kind != "max" else "warning",
                         source="wording", clause=(str(r.get("clause") or "")[:600] or None),
                         **params))
    return out


# ── running them ────────────────────────────────────────────────────────────
def _date(v: Any) -> Optional[dt.date]:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "").strip().lower())


def _hits(value: str, items: list[str]) -> bool:
    v = _norm(value)
    return any(v == _norm(x) or (len(_norm(x)) > 3 and _norm(x) in v) for x in items)


# Every row a rule fails is kept, to be decided one by one — up to this many.
MAX_ITEMS = 2000


def check(rules: list[dict], rows: list[dict]) -> list[dict]:
    """Every enabled rule that some row breaks: [{id, label, severity, source,
    clause, rows, examples: [{policy, value, why}], items: [{key, row, policy,
    value, why}]}]. `row` is the row's place in `rows`; `key` names the
    exception across rebuilds (rule, policy, value — and which repeat of them).
    A rule whose subject no row carries is skipped — said as `untested`, not
    as a pass."""
    out = []
    seen: dict[str, int] = {}
    for r in rules or []:
        if not r.get("enabled", True):
            continue
        fields = SUBJECT_FIELDS.get(r.get("subject"), ())
        p = r.get("params") or {}
        bad: list[dict] = []
        carried = False
        if r["kind"] == "duplicate":
            seen.clear()
        for i, row in enumerate(rows):
            v = ce.first(row, fields)
            pol = ce._s(ce.first(row, ce.POLICY))
            if r["kind"] == "required":
                carried = True
                if v is None or (ce.num(v) is None and r["subject"] != "policy"):
                    bad.append({"row": i, "policy": pol, "value": None,
                                "why": f"no {SUBJECT_LABEL[r['subject']]}"})
                continue
            if v is None:
                continue
            carried = True
            why = None
            if r["kind"] == "within_term":
                d = _date(v)
                lo, hi = _date(p.get("start")), _date(p.get("end"))
                if d and lo and hi and not (lo <= d <= hi):
                    why = f"{d:%d %b %Y} is outside the treaty period"
            elif r["kind"] == "currency":
                if str(v).strip().upper() != str(p.get("value") or "").upper():
                    why = f"in {str(v).strip().upper()}"
            elif r["kind"] == "max":
                n, lim = ce.num(v), ce.num(p.get("value"))
                if n is not None and lim is not None and n > lim:
                    why = f"{_money(n)} is above {_money(lim)}"
            elif r["kind"] == "excluded":
                if _hits(str(v), p.get("values") or []):
                    why = f"{v} is excluded"
            elif r["kind"] == "allowed":
                if not _hits(str(v), p.get("values") or []):
                    why = f"{v} is not covered"
            elif r["kind"] == "duplicate":
                key = _norm(v)
                seen[key] = seen.get(key, 0) + 1
                if seen[key] == 2:
                    why = "appears more than once"
            if why:
                bad.append({"row": i, "policy": pol, "value": ce._s(v), "why": why})
        if bad:
            n: dict[tuple, int] = {}
            for b in bad:
                k = (b["policy"] or "", b["value"] or "")
                n[k] = n.get(k, 0) + 1
                b["key"] = "|".join((r["id"], *k, str(n[k])))
            reco, options = recommend(r)
            out.append({"id": r["id"], "label": r["label"], "severity": r["severity"],
                        "source": r.get("source"), "clause": r.get("clause"),
                        "kind": r["kind"], "subject": r["subject"],
                        "field": SUBJECT_LABEL.get(r["subject"]),
                        "recommendation": reco, "options": options,
                        "rows": len(bad),
                        "examples": [{k: b[k] for k in ("policy", "value", "why")}
                                     for b in bad[:5]],
                        "items": bad[:MAX_ITEMS]})
        elif not carried and rows:
            out.append({"id": r["id"], "label": r["label"], "severity": "untested",
                        "source": r.get("source"), "clause": r.get("clause"),
                        "rows": 0, "examples": [],
                        "why": f"no row carries a {SUBJECT_LABEL[r['subject']]}"})
    return out


# The broker's decisions: Approve (the recommended value), Fix (a value
# entered), Dismiss (kept as it is). "accept" is Dismiss as first named;
# "exclude" (left out of the statement) is still honoured where saved.
DECISIONS = ("approve", "fix", "dismiss", "accept", "exclude")
STATUS = {"approve": "approved", "fix": "fixed", "dismiss": "dismissed",
          "accept": "dismissed", "exclude": "excluded"}
NUMERIC = {"premium", "sum_insured", "claims"}


def recommend(r: dict) -> tuple[Optional[str], list[str]]:
    """The value a row breaking `r` should hold, when the rule names one —
    (one value, or the choices). Approve writes it; without one, Fix."""
    p = r.get("params") or {}
    if r["kind"] == "max" and p.get("value") is not None:
        return _plain(p["value"]), []
    if r["kind"] == "currency" and p.get("value"):
        return str(p["value"]).upper(), []
    if r["kind"] == "allowed":
        vals = [str(v) for v in p.get("values") or [] if str(v).strip()]
        return (vals[0], []) if len(vals) == 1 else (None, vals)
    return None, []


def _plain(v: Any) -> str:
    n = ce.num(v)
    if n is None:
        return str(v)
    return str(int(n)) if float(n).is_integer() else str(n)


def decide(checks: list[dict], decisions: dict[str, dict],
           refail: Optional[set] = None) -> tuple[set[int], dict[int, dict]]:
    """Mark every exception with what was decided on it, by its key, and
    count each check's open / approved / fixed / dismissed / excluded rows.
    `refail`: (rule id, row) pairs whose approved or fixed value was checked
    again and still breaks the rule — open again, said so.
    Returns the rows to leave out, and the values to write: {row: {subject: value}}."""
    refail = refail or set()
    out = {it["row"] for c in checks for it in c.get("items") or []
           if (decisions.get(it.get("key")) or {}).get("kind") == "exclude"}
    fixes: dict[int, dict] = {}
    for c in checks:
        tally = {"open": 0, "approved": 0, "fixed": 0, "dismissed": 0, "excluded": 0}
        for it in c.get("items") or []:
            for k in ("reason", "decided_by", "decided_at", "new_value", "still_fails"):
                it.pop(k, None)
            d = decisions.get(it.get("key"))
            if d:
                it["status"] = STATUS.get(d["kind"], "open")
                it.update({"reason": d.get("reason"), "decided_by": d.get("by"),
                           "decided_at": d.get("at")})
                if d["kind"] in ("approve", "fix"):
                    it["new_value"] = d.get("value")
                    if d.get("value") not in (None, ""):
                        fixes.setdefault(it["row"], {})[c.get("subject")] = d["value"]
                    if (c["id"], it["row"]) in refail:
                        it["status"] = "open"
                        it["still_fails"] = True
            elif it["row"] in out:
                it["status"] = "excluded"
                it["reason"] = "Left out under another rule"
            else:
                it["status"] = "open"
            tally[it["status"]] += 1
        if c.get("items") is not None:
            # Rows past MAX_ITEMS are never shown, so they stay open.
            tally["open"] += max(0, c.get("rows", 0) - len(c["items"]))
            c.update(tally)
    return out, fixes


def apply_fixes(rows: list[dict], fixes: dict[int, dict]) -> list[dict]:
    """The rows with each approved or fixed value written in — to the field
    the rule reads and to the column it came from, so a column copied into
    the reinsurer's file shows it too."""
    out = list(rows)
    for i, subj in fixes.items():
        if i >= len(out):
            continue
        row = {**out[i], "_col": dict(out[i].get("_col") or {})}
        for subject, value in subj.items():
            fields = SUBJECT_FIELDS.get(subject) or ()
            if not fields:
                continue
            key = next((k for k in fields if row.get(k) not in (None, "")), fields[0])
            v = ce.num(value) if subject in NUMERIC and ce.num(value) is not None else value
            row[key] = v
            col = (row.get("_from") or {}).get(key)
            if col:
                row["_col"][col] = v
        out[i] = row
    return out


def critical(checks: Optional[list]) -> list[dict]:
    """The critical rules some row still breaks — undecided. A check from
    before decisions (no `open` count) counts whole."""
    # "blocking" is the name it had before — statements checked then still count.
    return [c for c in checks or [] if c.get("severity") in ("critical", "blocking")
            and c.get("open", c.get("rows", 1)) > 0]


def _severity(v: Any, default: str) -> str:
    if v == "blocking":
        return "critical"
    return v if v in SEVERITIES else default
