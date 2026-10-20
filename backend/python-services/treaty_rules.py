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


# ── from the treaty's terms ─────────────────────────────────────────────────
def derive(st: dict, inception: Optional[dt.date],
           expiry: Optional[dt.date]) -> list[dict]:
    """The rules a treaty's structure implies. Same structure, same rules."""
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
    return out


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


def check(rules: list[dict], rows: list[dict]) -> list[dict]:
    """Every enabled rule that some row breaks: [{id, label, severity, source,
    clause, rows, examples: [{policy, value, why}]}]. A rule whose subject no
    row carries is skipped — said as `untested`, not as a pass."""
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
        for row in rows:
            v = ce.first(row, fields)
            pol = ce._s(ce.first(row, ce.POLICY))
            if r["kind"] == "required":
                carried = True
                if v is None or (ce.num(v) is None and r["subject"] != "policy"):
                    bad.append({"policy": pol, "value": None,
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
                bad.append({"policy": pol, "value": ce._s(v), "why": why})
        if bad:
            out.append({"id": r["id"], "label": r["label"], "severity": r["severity"],
                        "source": r.get("source"), "clause": r.get("clause"),
                        "rows": len(bad), "examples": bad[:5]})
        elif not carried and rows:
            out.append({"id": r["id"], "label": r["label"], "severity": "untested",
                        "source": r.get("source"), "clause": r.get("clause"),
                        "rows": 0, "examples": [],
                        "why": f"no row carries a {SUBJECT_LABEL[r['subject']]}"})
    return out


def critical(checks: Optional[list]) -> list[dict]:
    # "blocking" is the name it had before — statements checked then still count.
    return [c for c in checks or [] if c.get("severity") in ("critical", "blocking")]


def _severity(v: Any, default: str) -> str:
    if v == "blocking":
        return "critical"
    return v if v in SEVERITIES else default
