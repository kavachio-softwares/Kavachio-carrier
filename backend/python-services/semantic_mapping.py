"""Which incoming column feeds which output field — and how sure we are.

A bordereau rarely uses the words the output template asks for. The template
wants "Commission"; the file says "CM". Matching those is not string equality,
and it is not a guess either — it is a ladder of increasingly weak evidence,
where every rung records HOW it decided so the answer can be argued with later.

    1. an explicit mapping somebody already configured   MANUAL
    2. the same name                                      EXACT
    3. the same name once normalised                      NORMALIZED
    4. a known alias ("Commission" ~ "CM")                ALIAS
    5. a semantic match the model proposed                SEMANTIC
    6. nothing good enough                                REVIEW_REQUIRED

The rungs are ordered by how much they can be trusted, and a weaker rung NEVER
overrides a stronger one — an explicitly configured mapping in particular is
final, which is what stops a re-run quietly moving a column somebody had already
fixed by hand.

TWO THINGS GATE AN AUTOMATIC MAPPING, not one.

  Confidence. A semantic match is accepted only above the configured threshold
  (99% by default). Below it the pairing is not wrong — it is unproven, so it
  goes to review with its score attached rather than silently into the file.

  Compatibility. A name can look right and the data be wrong: a "CM" column
  holding "Commercial" is not a commission, whatever the name suggests. So the
  sample values are checked against what the field is supposed to hold, and a
  mismatch demotes the match no matter how confident the name comparison was.

The model only ever PROPOSES. Everything that decides — the threshold, the type
check, the ambiguity rule, the ordering — is deterministic and lives here, so an
AI answer can never by itself put a column into a delivered file.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Optional

import field_aliases as fa

log = logging.getLogger("bdx.semantic_mapping")

# --- how a mapping was decided (plan section 7) ------------------------------
EXACT, NORMALIZED, ALIAS, SEMANTIC, MANUAL = (
    "EXACT", "NORMALIZED", "ALIAS", "SEMANTIC", "MANUAL")

AUTO_MAPPED, REVIEW_REQUIRED, MANUALLY_CONFIRMED, REJECTED = (
    "AUTO_MAPPED", "REVIEW_REQUIRED", "MANUALLY_CONFIRMED", "REJECTED")

MAPPING_VERSION = "1"


def min_confidence() -> float:
    """The bar a SEMANTIC match must clear to be accepted without a human.

    Configurable rather than hard-coded (plan section 2). Deliberately high: a
    wrong column that looks plausible is worse than a blank one, because a blank
    is noticed and a wrong number is not.
    """
    raw = (os.getenv("KAVACHIO_SEMANTIC_MIN_CONFIDENCE") or "").strip()
    try:
        v = float(raw) if raw else 0.99
    except ValueError:
        v = 0.99
    # A percentage is the natural way to write this; accept either form.
    if v > 1.0:
        v = v / 100.0
    return min(max(v, 0.0), 1.0)


def _norm(s: Any) -> str:
    t = str(s or "").lower().replace("%", " percent ").replace("$", " dollar ")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


# --- what the samples actually hold (plan section 6) -------------------------

_MONEY_CHARS = "$£€¥,() "
_DATE_PAT = re.compile(
    r"^\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})")


def value_kind(samples: list) -> str:
    """number | date | bool | text | unknown — read off the values themselves.

    Only the values decide. A column called "Commission Amount" full of the word
    "Commercial" is text, and this is the function that says so.
    """
    vals = [str(v).strip() for v in (samples or [])
            if v is not None and str(v).strip() != ""]
    if not vals:
        return "unknown"
    n_num = n_date = n_bool = 0
    for v in vals:
        low = v.lower()
        if low in ("true", "false", "yes", "no", "y", "n"):
            n_bool += 1
            continue
        if _DATE_PAT.match(v):
            n_date += 1
            continue
        stripped = v
        for ch in _MONEY_CHARS:
            stripped = stripped.replace(ch, "")
        stripped = stripped.rstrip("%").lstrip("+-")
        if stripped and re.fullmatch(r"\d*\.?\d*", stripped) and any(
                c.isdigit() for c in stripped):
            n_num += 1
    total = len(vals)
    # A clear majority, not a single lucky row.
    if n_date / total >= 0.8:
        return "date"
    if n_num / total >= 0.8:
        return "number"
    if n_bool / total >= 0.8:
        return "bool"
    return "text"


# The output field's declared type -> the value kinds that can feed it.
_ACCEPTS: dict[str, set[str]] = {
    "int": {"number", "unknown"},
    "decimal": {"number", "unknown"},
    "date": {"date", "unknown"},
    "datetime": {"date", "unknown"},
    "bool": {"bool", "text", "number", "unknown"},
    # Text takes anything — a reference or a code is legitimately a number.
    "string": {"number", "date", "bool", "text", "unknown"},
    "json": {"number", "date", "bool", "text", "unknown"},
}


def compatible(data_type: Optional[str], samples: list) -> tuple[bool, str]:
    """Can this column's values feed a field of this type? (ok, why-not)."""
    kind = value_kind(samples)
    if kind == "unknown":
        # No samples is not evidence against — it is no evidence at all.
        return True, ""
    accepts = _ACCEPTS.get((data_type or "string").lower(), _ACCEPTS["string"])
    if kind in accepts:
        return True, ""
    return False, (f"the column holds {kind} values and the field expects "
                   f"{data_type or 'string'}")


# --- one field's answer ------------------------------------------------------

@dataclass
class Decision:
    """How one output field got its source — and how sure that is.

    Written to the setup so it can be shown, argued with and audited. The field
    KEY is what everything hangs off, never the display name: renaming a column
    in the editor must not move its data (plan section 4).
    """
    field_key: str
    display_name: str
    source: Optional[str] = None
    method: Optional[str] = None
    confidence: float = 0.0
    status: str = REVIEW_REQUIRED
    reason: str = ""
    # Everything that was considered, best first — this is what the review UI
    # shows when it asks a person to choose (plan section 8).
    candidates: list[dict] = dc_field(default_factory=list)
    required: bool = False
    data_type: Optional[str] = None
    version: str = MAPPING_VERSION
    alias_version: str = fa.ALIAS_VERSION

    @property
    def mapped(self) -> bool:
        return self.status in (AUTO_MAPPED, MANUALLY_CONFIRMED) and bool(self.source)

    def to_dict(self) -> dict:
        return {
            "field_key": self.field_key, "display_name": self.display_name,
            "source": self.source, "method": self.method,
            "confidence": round(self.confidence, 4), "status": self.status,
            "reason": self.reason, "candidates": self.candidates[:5],
            "required": self.required, "data_type": self.data_type,
            "version": self.version, "alias_version": self.alias_version,
        }


def resolve_field(
    out_field: dict,
    input_cols: list[str],
    samples: dict[str, list],
    *,
    existing_source: Optional[str] = None,
    semantic: Optional[dict] = None,
    threshold: Optional[float] = None,
) -> Decision:
    """Walk the ladder for ONE output field.

    `out_field`       {"field_key","display_name","column_name","data_type","required"}
    `existing_source` a mapping already configured for this field — rung 1, final
    `semantic`        the model's proposal for this field, {"source","confidence"}
    """
    bar = min_confidence() if threshold is None else threshold
    key = out_field.get("field_key") or _norm(out_field.get("column_name"))
    label = (out_field.get("display_name") or out_field.get("column_name") or key)
    # Matching uses the ORIGINAL heading, not the display name: renaming a column
    # must not change where its data comes from (plan section 4).
    match_on = out_field.get("column_name") or label
    d = Decision(field_key=key, display_name=label,
                 required=bool(out_field.get("required")),
                 data_type=out_field.get("data_type"))

    by_exact = {str(c).strip().lower(): c for c in reversed(input_cols)}
    by_norm: dict[str, str] = {}
    for c in reversed(input_cols):
        by_norm[_norm(c)] = c

    def _ok(src: str) -> tuple[bool, str]:
        return compatible(d.data_type, samples.get(src, []))

    # 1 — somebody configured this already. Final: a later re-run must not undo
    #     a mapping a person fixed by hand.
    if existing_source and existing_source in input_cols:
        d.source, d.method, d.confidence = existing_source, MANUAL, 1.0
        d.status = MANUALLY_CONFIRMED
        d.reason = "configured for this setup already"
        d.candidates = [{"source": existing_source, "confidence": 1.0, "method": MANUAL}]
        return d

    considered: list[dict] = []

    # 2 — the same name.
    hit = by_exact.get(str(match_on).strip().lower())
    if hit:
        considered.append({"source": hit, "confidence": 1.0, "method": EXACT})

    # 3 — the same name once normalised ("Policy No" / "policy_no").
    hit = by_norm.get(_norm(match_on))
    if hit and not any(c["source"] == hit for c in considered):
        considered.append({"source": hit, "confidence": 0.995, "method": NORMALIZED})

    # 4 — a known alias. Every input column that could mean this field, so an
    #     ambiguous file is SEEN as ambiguous rather than resolved by luck.
    alias_hits = [c for c in input_cols
                  if fa.share_an_alias(match_on, c)
                  and not any(x["source"] == c for x in considered)]
    for c in alias_hits:
        considered.append({"source": c, "confidence": 0.99, "method": ALIAS})

    # 5 — what the model proposed, if anything.
    if semantic and semantic.get("source") in input_cols:
        src = semantic["source"]
        conf = float(semantic.get("confidence") or 0.0)
        if not any(x["source"] == src for x in considered):
            considered.append({"source": src, "confidence": conf, "method": SEMANTIC})

    # Compatibility is applied to every candidate BEFORE ranking, so a
    # well-named column full of the wrong kind of data cannot win (section 6).
    for c in considered:
        ok, why = _ok(c["source"])
        c["compatible"] = ok
        if not ok:
            c["reason"] = why
            # Not zero: the candidate is still worth showing to a reviewer,
            # it just must not be picked automatically.
            c["confidence"] = min(c["confidence"], 0.5)

    considered.sort(key=lambda c: (c["compatible"], c["confidence"]), reverse=True)
    d.candidates = considered
    if not considered:
        d.status = REVIEW_REQUIRED
        d.reason = "no column in this file looks like this field"
        return d

    best = considered[0]
    if not best["compatible"]:
        d.status = REVIEW_REQUIRED
        d.method, d.source, d.confidence = best["method"], None, best["confidence"]
        d.reason = (f"'{best['source']}' matches the name but {best.get('reason', '')}")
        return d

    # Ambiguity: two candidates that BOTH clear the bar are not a decision
    # (section 8). An exact name match is exempt — it is not ambiguous for a
    # column to be called exactly what it is.
    qualifying = [c for c in considered
                  if c["compatible"] and c["confidence"] >= bar]
    if best["method"] != EXACT and len(qualifying) > 1:
        d.status = REVIEW_REQUIRED
        d.method, d.confidence = best["method"], best["confidence"]
        d.reason = ("more than one column could be this field: "
                    + ", ".join(f"{c['source']} ({c['confidence']:.0%})"
                                for c in qualifying[:4]))
        return d

    if best["confidence"] >= bar:
        d.source, d.method, d.confidence = (
            best["source"], best["method"], best["confidence"])
        d.status = AUTO_MAPPED
        d.reason = {
            EXACT: "the file uses the same column name",
            NORMALIZED: "the same name once punctuation and case are ignored",
            ALIAS: f"'{best['source']}' is a known way of writing this field",
            SEMANTIC: f"read as this field with {best['confidence']:.1%} confidence",
        }.get(best["method"], "")
        return d

    d.status = REVIEW_REQUIRED
    d.method, d.confidence = best["method"], best["confidence"]
    d.reason = (f"the closest column is '{best['source']}' at "
                f"{best['confidence']:.0%}, under the {bar:.0%} needed to accept "
                f"it without a person looking")
    return d


def resolve_sheet(
    out_fields: list[dict],
    input_cols: list[str],
    samples: dict[str, list],
    *,
    existing: Optional[dict[str, str]] = None,
    semantic: Optional[dict[str, dict]] = None,
    threshold: Optional[float] = None,
) -> list[Decision]:
    """Every output field of one sheet, in the template's own order."""
    existing = existing or {}
    semantic = semantic or {}
    out: list[Decision] = []
    for f in out_fields:
        col = f.get("column_name") or f.get("display_name") or ""
        out.append(resolve_field(
            f, input_cols, samples,
            existing_source=existing.get(col) or existing.get(f.get("field_key") or ""),
            semantic=semantic.get(col), threshold=threshold))
    return out


def unresolved_required(decisions: list[Decision]) -> list[Decision]:
    """Required fields with nowhere to get their value from (section 10).

    Reported rather than left blank: a mandatory column silently full of nothing
    is the one failure that looks like a successful delivery.
    """
    return [d for d in decisions if d.required and not d.mapped]
