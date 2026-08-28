"""
Expected-value & data-type resolution with a mapping-coverage gate.

A rule's "expected value" can come from several sources of decreasing reliability
(contract -> master data -> system defaults -> profiled -> none).  Before any of
that matters, though, the rule must actually be able to *reach* a value: its
target field has to be mapped to a source column that is present in the file.

When it is NOT — the field is unmapped, or the mapped column is missing/empty —
the rule cannot legitimately validate anything.  Firing a `critical` in that case
produces the "empty actual / empty expected" false exceptions we see today
(e.g. the `100% policy Limit` rule).  Instead we route those to a human-review
queue via `status='needs_review'` + a `review_reason`, with no master data and
no schema migration required.

Steps 2-3 of the plan live here:
  * check_mapping_coverage / _reverse_lookup  — the coverage check
  * resolve_expected                          — the gate (+ a minimal cascade)

The master-data lookup (`get_lookup`) is a stub returning [] until ref_code_* is
seeded; the cascade skips it gracefully today.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------- #
# Review reason codes — why a rule/field needs manual intervention.
# (Can be centralised into constants.py later; kept here so this module is
#  self-contained and unit-testable without importing the DB-backed constants.)
# --------------------------------------------------------------------------- #
REVIEW_REASONS = {
    "MISSING_MAPPING":  "Target field is not mapped to any source column",
    "MISSING_COLUMN":   "Mapped column is not present in the uploaded file",
    "EMPTY_VALUE":      "Mapped column is present but empty for all rows",
    "NO_EXPECTED":      "Rule has no expected value/threshold defined",
    "AMBIGUOUS_CLAUSE": "Contract clause is ambiguous — needs underwriter input",
}

SHEET_SEP = " :: "


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _norm(s: Any) -> str:
    """Loose key for comparing field/column names (case/space/punct insensitive)."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _split(field_path: str) -> tuple[Optional[str], str]:
    """'policy.tria_premium' -> ('policy', 'tria_premium'); 'x' -> (None, 'x')."""
    fp = str(field_path or "")
    if "." in fp:
        table, col = fp.split(".", 1)
        return table.strip(), col.strip()
    return None, fp.strip()


_NUM_CLEAN = re.compile(r"[,$%\s]")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}/\d{1,2}/\d{2,4}$")


def _is_int(v: Any) -> bool:
    s = _NUM_CLEAN.sub("", str(v))
    if s in ("", "-", "+"):
        return False
    try:
        return float(s).is_integer()
    except ValueError:
        return False


def _is_decimal(v: Any) -> bool:
    s = _NUM_CLEAN.sub("", str(v))
    if s in ("", "-", "+", "."):
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def _is_bool(v: Any) -> bool:
    return str(v).strip().lower() in {"true", "false", "0", "1", "yes", "no", "y", "n"}


def _is_date(v: Any) -> bool:
    return bool(_DATE_RE.match(str(v).strip()))


def _num(v: Any) -> float:
    return float(_NUM_CLEAN.sub("", str(v)))


# --------------------------------------------------------------------------- #
# data type — authoritative from the canonical model, inferred only as fallback
# --------------------------------------------------------------------------- #
_FIELD_TYPES: Optional[dict[str, str]] = None


def _field_types() -> dict[str, str]:
    """Build {normalised field name -> canonical type} from the data model once.

    Keys are stored both as the bare column and as 'table.column' so a rule's
    field_path matches either way. Built from data_model (no DB)."""
    global _FIELD_TYPES
    if _FIELD_TYPES is None:
        out: dict[str, str] = {}
        try:
            from data_model import DATA_MODEL  # pure dict, no DB
            for meta in DATA_MODEL.values():
                t, c, ty = meta.get("table"), meta.get("column"), meta.get("type")
                if c and ty:
                    out.setdefault(_norm(c), ty)
                    if t:
                        out.setdefault(_norm(f"{t}.{c}"), ty)
        except Exception:
            pass
        _FIELD_TYPES = out
    return _FIELD_TYPES


def infer_type(samples: Iterable[Any]) -> str:
    """Best-effort type from sample values (for unmapped / custom fields)."""
    vals = [s for s in (samples or []) if str(s).strip() != ""]
    if not vals:
        return "string"
    if all(_is_int(v) for v in vals):
        return "int"
    if all(_is_decimal(v) for v in vals):
        return "decimal"
    if all(_is_date(v) for v in vals):
        return "date"
    if all(_is_bool(v) for v in vals):
        return "bool"
    return "string"


def resolve_data_type(field_path: str, samples: Optional[Iterable[Any]] = None) -> str:
    """Canonical type when the field is in the data model; else inferred. Never unknown."""
    ft = _field_types()
    _, bare = _split(field_path)
    ty = ft.get(_norm(field_path)) or ft.get(_norm(bare))
    return ty if ty else infer_type(samples or [])


# --------------------------------------------------------------------------- #
# STEP 2 — mapping-coverage check
# --------------------------------------------------------------------------- #
def _iter_mappings(spec: Any) -> Iterable[tuple[str, Any]]:
    """Yield (canonical_field, source_value) from either shape:
       nested spec_by_sheet  {sheet: {canonical: src|[src,...]}}
       flat spec             {canonical: src|[src,...]}
    """
    if not isinstance(spec, dict):
        return
    for key, val in spec.items():
        if isinstance(val, dict):                 # nested: sheet -> {canonical: src}
            for canon, src in val.items():
                yield canon, src
        else:                                     # flat: canonical -> src
            yield key, val


def _reverse_lookup(field_path: str, spec: Any) -> Optional[dict]:
    """Find the source {sheet, column} a rule's target field is mapped from.
    Matches on full field_path or its bare last segment, both ways. None if unmapped."""
    _, bare = _split(field_path)
    targets = {_norm(field_path), _norm(bare)}
    for canon, srcval in _iter_mappings(spec):
        _, canon_bare = _split(canon)
        canon_keys = {_norm(canon), _norm(canon_bare)}
        if targets & canon_keys:
            srcs = srcval if isinstance(srcval, list) else [srcval]
            for s in srcs:
                s = str(s).strip()
                if not s:
                    continue
                if SHEET_SEP in s:
                    sheet, col = s.split(SHEET_SEP, 1)
                    return {"sheet": sheet.strip(), "column": col.strip()}
                return {"sheet": None, "column": s}
    return None


def check_mapping_coverage(
    field_path: str,
    mapper_spec: Any,
    present_columns: Optional[Iterable[str]] = None,
) -> dict:
    """Can the rule's target field actually be validated?

    Returns {"ok": True, "source": {...}} when the field is mapped (and, if
    present_columns is given, the column exists in the file). Otherwise
    {"ok": False, "reason": <REVIEW_REASONS key>, ...} for the review queue.
    """
    src = _reverse_lookup(field_path, mapper_spec)
    if not src or not src.get("column"):
        return {"ok": False, "reason": "MISSING_MAPPING", "field": field_path}
    if present_columns:
        present = {_norm(c) for c in present_columns}
        if _norm(src["column"]) not in present:
            return {"ok": False, "reason": "MISSING_COLUMN",
                    "field": field_path, "source": src}
    return {"ok": True, "source": src}


# --------------------------------------------------------------------------- #
# expected-value cascade pieces
# --------------------------------------------------------------------------- #
def _has_value(spec: Any) -> bool:
    if not isinstance(spec, dict):
        return False
    return any(spec.get(k) is not None
               for k in ("value", "min_value", "max_value", "minimum", "maximum", "enum", "formula"))


def _value_of(spec: dict) -> Any:
    for k in ("value", "max_value", "maximum", "min_value", "minimum", "enum", "formula"):
        if spec.get(k) is not None:
            return spec[k]
    return None


def _constraint_of(spec: dict) -> str:
    if spec.get("enum") is not None:
        return "enum"
    if any(spec.get(k) is not None for k in ("min_value", "max_value", "minimum", "maximum")):
        return "range"
    if spec.get("formula") is not None:
        return "formula"
    return "value"


def get_lookup(field_path: str, tenant_id: Optional[int] = None) -> list:
    """Master-data enum for a field (ref_code_value). Stub until ref_code_* is seeded."""
    return []


def profile_constraint(samples: Iterable[Any], data_type: str) -> Optional[dict]:
    """Suggest a constraint from observed data — a *suggestion only* (needs_review)."""
    vals = [v for v in (samples or []) if str(v).strip() != ""]
    if not vals:
        return None
    if data_type in ("int", "decimal"):
        nums = [_num(v) for v in vals if _is_decimal(v)]
        if nums:
            return {"kind": "range", "value": {"min": min(nums), "max": max(nums)}}
    elif data_type == "date":
        ds = sorted(str(v).strip() for v in vals if _is_date(v))
        if ds:
            return {"kind": "range", "value": {"min": ds[0], "max": ds[-1]}}
    else:  # string
        distinct = sorted({str(v).strip() for v in vals})
        if len(distinct) <= 20:                   # low cardinality -> candidate enum
            return {"kind": "enum", "value": distinct}
    return None


# --------------------------------------------------------------------------- #
# result builders
# --------------------------------------------------------------------------- #
def _result(data_type, expected, constraint, source, *, source_version, confidence,
            status, review_reason, root_cause) -> dict:
    return {
        "data_type": data_type,
        "expected": expected,
        "constraint": constraint,        # value | range | enum | format | presence | none
        "source": source,               # contract | master_data | system_default | profiled | none
        "source_version": source_version,
        "confidence": confidence,
        "status": status,               # active | needs_review
        "review_reason": review_reason, # REVIEW_REASONS key or None
        "root_cause": root_cause,       # data_violation | mapping_gap | rule_incomplete | clause_ambiguous
    }


def _ok(dt, expected, constraint, source, *, source_version=None, confidence=0.9) -> dict:
    return _result(dt, expected, constraint, source, source_version=source_version,
                   confidence=confidence, status="active",
                   review_reason=None, root_cause="data_violation")


def _needs_review(dt, expected, constraint, source, *, review_reason=None,
                  root_cause="rule_incomplete", confidence=0.0) -> dict:
    return _result(dt, expected, constraint, source, source_version=None,
                   confidence=confidence, status="needs_review",
                   review_reason=review_reason, root_cause=root_cause)


# --------------------------------------------------------------------------- #
# STEP 3 — the gate (mapping-coverage first, then the cascade)
# --------------------------------------------------------------------------- #
def resolve_expected(
    field_path: str,
    *,
    clause: Optional[str] = None,
    rule_spec: Optional[dict] = None,
    samples: Optional[Iterable[Any]] = None,
    mapper_spec: Any = None,
    present_columns: Optional[Iterable[str]] = None,
    tenant_id: Optional[int] = None,
) -> dict:
    """Resolve the expected value + data type for a rule's target field.

    Order:
      0. data type (always)
      1. MAPPING GATE — unmapped / missing column -> needs_review (mapping_gap)
      2. contract     — concrete value already in rule_spec
      3. master data  — ref_code enum (stub today)
      4. profiling    — suggestion from samples (needs_review)
      5. nothing      — type/presence only (needs_review, NO_EXPECTED)
    """
    dt = resolve_data_type(field_path, samples)

    # 1. mapping gate (only when a mapper_spec is supplied to check against)
    if mapper_spec is not None:
        cov = check_mapping_coverage(field_path, mapper_spec, present_columns)
        if not cov["ok"]:
            return _needs_review(dt, None, "presence", "none",
                                 review_reason=cov["reason"], root_cause="mapping_gap")

    # 2. contract
    if rule_spec and _has_value(rule_spec):
        conf = float(rule_spec.get("confidence", 0.9)) if isinstance(rule_spec, dict) else 0.9
        return _ok(dt, _value_of(rule_spec), _constraint_of(rule_spec), "contract", confidence=conf)

    # 3. master data (enum) — stub returns [] until ref_code_* seeded
    enum = get_lookup(field_path, tenant_id)
    if enum:
        return _ok(dt, enum, "enum", "master_data")

    # 4. profiling — suggestion only
    if samples:
        c = profile_constraint(samples, dt)
        if c:
            return _needs_review(dt, c["value"], c["kind"], "profiled", confidence=0.5)

    # 5. nothing definable
    return _needs_review(dt, None, "presence", "none", review_reason="NO_EXPECTED")
