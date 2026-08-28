"""
rule_normalizer.py
──────────────────
Deterministic Pipeline 2 — Step 3 (Normalization).

For each Stage B candidate:
  1. (AJV)   try to compile the json_schema — drop if malformed
  2.         look up canonical_target.table in CANONICAL_SCHEMA
  3.         verify referenced fields exist on that table
  4.         map rule_type → rule_class_library entry, enforce engine consistency
  5.         compare confidence to the auto-trust threshold:
                ≥ threshold → rule_status='active'
                <  threshold → rule_status='needs_review'

Outputs a validation_rule dict (the row that would be persisted) or
{"drop": True, "reason": ...} when the candidate must be discarded.

Also still exports the legacy helpers used by validation_rule_generator:
parse_llm_json, deduplicate_rules, merge_reference_lists,
normalize_lookup_lists.
"""

import os
import re
import json
import hashlib
import datetime

from contract_upload_services.constants import (
    CANONICAL_SCHEMA,
    RULE_CLASS_LIBRARY,
    DEFAULT_RULE_AUTO_TRUST_THRESHOLD
)


# Mapping is COLUMN-based by default: a rule binds to an Output-Template column by
# NAME/meaning, not to the sampled data values. So the data-grounding guards
# (numeric/date type inferred from samples, percentage-scale, flags-all-sample-
# rows) are OFF by default — they were rejecting valid rules whose contract value
# simply wasn't in the 3 sample rows. Set KAVACHIO_DATA_GROUNDING=1 to re-enable.
_DATA_GROUNDING = os.getenv("KAVACHIO_DATA_GROUNDING", "0") == "1"

# When the model is confident but the Output Template has NO matching column, we
# still GENERATE the rule (rather than send it to review) so it's recorded — but
# it has no compiled SQL (there's no column to query), so it's a non-executable
# "recorded" rule until the column is added. Tune the bar via env.
_GENERATE_CONF_THRESHOLD = float(os.getenv("KAVACHIO_GENERATE_CONFIDENCE", "0.8"))

# Weak-rule floor: route an otherwise-valid rule to review when the model's
# generation confidence is below this. Default 0.0 (off) so it never regresses
# existing behaviour — raise it (e.g. 0.4) to make weak rules require a human.
_REJECT_CONF_FLOOR = float(os.getenv("KAVACHIO_REJECT_CONFIDENCE", "0.0"))


# =========================================================
# LLM JSON HELPERS
# =========================================================

def parse_llm_json(text):
    """Tolerant JSON parser — strips fences and trims whitespace."""

    if isinstance(text, (dict, list)):
        return text

    if not text:
        return {}

    s = text.strip()
    s = re.sub(r"```json\s*", "", s)
    s = re.sub(r"```", "", s)

    return json.loads(s.strip())


# =========================================================
# Legacy class_name rule helpers
# =========================================================

def fingerprint_rule(rule):

    key = {
        "contract_id": rule.get("contract_id"),
        "class_name":  rule.get("class_name"),
        "section":     rule.get("section"),
        "params":      rule.get("params", {})
    }

    return hashlib.md5(
        json.dumps(key, sort_keys=True, default=str).encode()
    ).hexdigest()


def deduplicate_rules(all_rules):

    seen = set()
    result = []

    for rule in all_rules:

        fp = fingerprint_rule(rule)

        if fp not in seen:
            seen.add(fp)
            result.append(rule)

    return result


def merge_reference_lists(base, llm_additions):

    merged = dict(base)

    for key, values in (llm_additions or {}).items():

        if key not in merged:
            merged[key] = values

        elif isinstance(values, list) and values:

            existing = set(merged[key])

            merged[key] = merged[key] + [
                v for v in values
                if v not in existing
            ]

    return merged


def normalize_lookup_lists(contract_rules, reference_lists):

    for rule in contract_rules:

        params = rule.get("params", {})

        if "lookup_list" not in params:
            continue

        lookup = params["lookup_list"]

        if isinstance(lookup, str):

            key = re.sub(
                r"[^a-z0-9]+",
                "_",
                lookup.strip().lower()
            ).strip("_")

            params["lookup_list"] = key

            if key not in reference_lists:
                reference_lists[key] = []

        elif isinstance(lookup, list):

            rule_name = rule.get("rule_name", "rule")

            key = re.sub(
                r"[^a-z0-9]+",
                "_",
                rule_name.strip().lower()
            ).strip("_") + "_values"

            reference_lists[key] = lookup
            params["lookup_list"] = key

        else:
            del params["lookup_list"]

    return contract_rules, reference_lists


# =========================================================
# NEW: AJV / Custom rule candidate normalization
# =========================================================

_RULE_CLASS_BY_NAME = {r["name"]: r for r in RULE_CLASS_LIBRARY}


def _extract_field_refs_from_schema(schema):
    """
    Walk a JSON Schema fragment and collect every top-level field name
    used under `properties` or referenced inside `required`.
    """

    fields = set()

    def walk(node):

        if isinstance(node, dict):

            if "properties" in node and isinstance(node["properties"], dict):
                for k in node["properties"].keys():
                    fields.add(k)

            if "required" in node and isinstance(node["required"], list):
                for k in node["required"]:
                    if isinstance(k, str):
                        fields.add(k)

            for v in node.values():
                walk(v)

        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)

    return fields


def _try_compile_json_schema(schema):
    """
    Best-effort AJV-compile check. Uses `jsonschema` if available; otherwise
    falls back to a structural sanity check that the top-level object is a
    dict and any 'enum' / 'required' fields are the right shape.
    """

    if not isinstance(schema, dict):
        return False, "json_schema is not an object"

    try:
        from jsonschema import Draft7Validator
        Draft7Validator.check_schema(schema)
        return True, None

    except ImportError:
        # Fallback structural check
        if "type" in schema and schema["type"] not in {
            "object", "array", "string", "number",
            "integer", "boolean", "null"
        }:
            return False, f"invalid json_schema type: {schema['type']}"
        return True, None

    except Exception as exc:
        return False, f"JSON Schema compile failed: {exc}"


def _resolve_rule_class(rule_type, expected_engine):
    """Look up rule_type in rule_class_library; enforce engine consistency."""

    if not rule_type:
        return None, "rule_type missing"

    cls = _RULE_CLASS_BY_NAME.get(rule_type)

    if not cls:
        return None, f"unknown rule_type: {rule_type}"

    if cls["rule_engine"] != expected_engine:
        return None, (
            f"rule_type '{rule_type}' uses engine "
            f"'{cls['rule_engine']}', expected '{expected_engine}'"
        )

    return cls, None


def _rule_status_from_confidence(confidence, threshold):
    """confidence ≥ threshold → 'active'; below → 'needs_review'."""

    try:
        c = float(confidence or 0)
    except (TypeError, ValueError):
        c = 0.0

    return "active" if c >= threshold else "needs_review"


# Floor/cap wording read from the CONTRACT text (clause text + section header) —
# never from the LLM's own rule_name/spec, which is circular (a misread "limit of
# $X" gets named "… Minimum", so trusting the name would lock in the error).
_MIN_SIGNALS = ("at least", "minimum", "no less than", "not less than",
                "no fewer than", "or more", "or greater", "greater than or equal")
_MAX_SIGNALS = ("maximum", "up to", "not to exceed", "shall not exceed",
                "no more than", "at most", "or less", "cannot exceed",
                "not exceed")


def _is_numeric_schema(node) -> bool:
    t = (node or {}).get("type")
    return t in ("number", "integer") or (isinstance(t, list) and
                                          ("number" in t or "integer" in t))


def _fix_limit_semantics(schema, clause, error_message):
    """Re-derive the operator for a numeric LIMIT from the contract's own wording.

    A coverage limit in an underwriting-authority schedule is normally a CAP — e.g.
    "Maximum Limits by Product: per occurrence limit of $2,000,000" means
    <= 2,000,000. But the extracted clause snippet usually DROPS that "Maximum
    Limits" heading, leaving an ambiguous "per occurrence limit of $X", so the LLM
    guesses inconsistently (we've seen the same Aurenity clause become const,
    minimum, AND maximum across contracts). Genuine floors, by contrast, say so in
    the text ("limits of at least $5,000,000", "minimum amount of $10,000,000").

    So, for a top-level numeric property carrying exactly one of const/minimum/
    maximum, set the operator from the contract wording:
      * explicit 'at least / minimum / no less than …'  -> minimum
      * explicit 'maximum / up to / not to exceed …'    -> maximum
      * otherwise, a Limit/Aggregate field              -> maximum (authority cap)
      * otherwise                                       -> leave unchanged
    Range rules (minimum AND maximum), string/enum const, and if/then conditional
    blocks are left untouched.
    """
    props = (schema or {}).get("properties")
    if not isinstance(props, dict):
        return schema, error_message
    # Contract-sourced context only (text + section header). Deliberately exclude
    # rule_name/title which can echo the LLM's own (possibly wrong) interpretation.
    clause = clause or {}
    ctx = " ".join(str(clause.get(k) or "")
                   for k in ("text", "section_header")).lower()
    is_min = any(s in ctx for s in _MIN_SIGNALS)
    is_max = any(s in ctx for s in _MAX_SIGNALS)
    new_op = new_field = new_val = None
    for field, node in props.items():
        if not isinstance(node, dict) or not _is_numeric_schema(node):
            continue
        present = [k for k in ("const", "minimum", "maximum") if k in node]
        if len(present) != 1:
            continue                # nothing to fix, or a range -> leave alone
        cur = present[0]
        fl = field.lower()
        if is_min and not is_max:
            target = "minimum"
        elif is_max and not is_min:
            target = "maximum"
        elif "limit" in fl or "aggregate" in fl:
            target = "maximum"      # authority-schedule limit -> cap
        else:
            continue                # genuine exact value -> keep as-is
        val = node.pop(cur)
        node[target] = val
        node.setdefault("type", "number")
        if target != cur:
            new_op, new_field, new_val = target, field, val
    if new_op:
        verb = "must be at least" if new_op == "minimum" else "must not exceed"
        error_message = f"{new_field} {verb} {new_val}."
    return schema, error_message


def normalize_ajv_rule(
    candidate,
    clause,
    classification,
    contract_ctx,
    threshold=DEFAULT_RULE_AUTO_TRUST_THRESHOLD
):
    """
    Normalize a Stage B-AJV candidate into a validation_rule dict.
    Returns either the validation_rule dict or {"drop": True, "reason": ...}.
    """

    json_schema = candidate.get("json_schema") or {}

    # 1) AJV compile check
    ok, reason = _try_compile_json_schema(json_schema)
    if not ok:
        return {"drop": True, "reason": reason}

    # 1b) Limit semantics: a coverage "limit of $X" is normally a cap, not an exact
    # value or a floor. Re-derive const/minimum/maximum from the contract's wording
    # (Limit/Aggregate fields with no wording default to a maximum cap). Genuine
    # floors keep `minimum` because their text says "at least/minimum". Also yields
    # a clearer message when the operator changed.
    json_schema, candidate_error_message = _fix_limit_semantics(
        json_schema, clause, candidate.get("error_message"))

    target = candidate.get("canonical_target") or {}
    template_aware = bool(target.get("output_field"))

    if template_aware:
        # Template-aware mode: canonical_target has output_field (Output Template
        # column name) instead of table/column. Skip DB schema checks — the LLM
        # was instructed to use output template field names, not canonical names.
        pass
    else:
        # 2) Canonical table check
        table = target.get("table")

        if not table or table not in CANONICAL_SCHEMA:
            return {
                "drop": True,
                "reason": f"unknown canonical table: {table!r}"
            }

        # 3) Canonical field check
        referenced = _extract_field_refs_from_schema(json_schema)
        allowed = set(CANONICAL_SCHEMA[table])

        bad = [f for f in referenced if f not in allowed]
        if bad:
            return {
                "drop": True,
                "reason": (
                    f"fields not in canonical schema '{table}': {bad}"
                )
            }

    # 4) rule_class_library mapping
    rule_types = classification.get("rule_types") or []
    rule_type = rule_types[0] if rule_types else None

    rule_class, err = _resolve_rule_class(rule_type, "ajv")
    if err:
        return {"drop": True, "reason": err}

    # 5) Threshold → status
    status = _rule_status_from_confidence(
        candidate.get("confidence"), threshold
    )

    return {
        "tenant_id":             contract_ctx.get("tenant_id"),
        "contract_id":           contract_ctx.get("contract_id"),
        "program_id":            contract_ctx.get("program_id"),
        "rule_engine":           "ajv",
        "rule_class":            rule_class["name"],
        "rule_class_display":    rule_class["display_name"],
        "rule_name":             candidate.get("rule_name"),
        "rule_description":      candidate.get("rule_description"),
        "validation_stage":      candidate.get("stage") or rule_class["default_stage"],
        "severity":              candidate.get("severity") or rule_class["default_severity"],
        "canonical_target":      target,
        "rule_spec":             json_schema,
        "error_message":         candidate_error_message,
        "source_clause_id":      clause.get("clause_id"),
        "source_verbatim_text":  clause.get("text"),
        "source_page_number":    clause.get("page_number") or clause.get("page"),
        "generation_confidence": candidate.get("confidence"),
        "rule_status":           status,
        "created_by":            "ai_generator_v1"
    }


def normalize_custom_rule(
    candidate,
    clause,
    classification,
    contract_ctx,
    threshold=DEFAULT_RULE_AUTO_TRUST_THRESHOLD
):
    """
    Normalize a Stage B-Custom candidate into a validation_rule dict.
    """

    rule_spec = candidate.get("rule_spec") or {}

    if not isinstance(rule_spec, dict):
        return {"drop": True, "reason": "rule_spec is not an object"}

    rule_type = (
        candidate.get("rule_type")
        or rule_spec.get("rule_type")
        or (classification.get("rule_types") or [None])[0]
    )

    rule_class, err = _resolve_rule_class(rule_type, "custom")
    if err:
        return {"drop": True, "reason": err}

    target = candidate.get("canonical_target") or {}
    template_aware = bool(target.get("output_field"))

    if not template_aware:
        table = target.get("table")

        if not table or table not in CANONICAL_SCHEMA:
            return {
                "drop": True,
                "reason": f"unknown canonical table: {table!r}"
            }
    else:
        table = None  # template-aware — no canonical table to check

    # Per-type structural checks (skip field-level checks in template-aware mode)
    if not template_aware and rule_type == "aggregate_limit":

        field = rule_spec.get("field")
        if field and field not in CANONICAL_SCHEMA[table]:
            return {
                "drop": True,
                "reason": f"aggregate_limit.field '{field}' not in '{table}'"
            }

        for g in rule_spec.get("group_by", []) or []:
            if g not in CANONICAL_SCHEMA[table]:
                return {
                    "drop": True,
                    "reason": f"group_by '{g}' not in '{table}'"
                }

        if (
            rule_spec.get("max_value") is None
            and rule_spec.get("min_value") is None
        ):
            return {
                "drop": True,
                "reason": "aggregate_limit needs at least one of max_value/min_value"
            }

    elif not template_aware and rule_type == "uniqueness":

        for f in rule_spec.get("fields", []) or []:
            if f not in CANONICAL_SCHEMA[table]:
                return {
                    "drop": True,
                    "reason": f"uniqueness.fields '{f}' not in '{table}'"
                }

    elif rule_type == "cross_field_math":

        if not rule_spec.get("formula"):
            return {"drop": True, "reason": "cross_field_math.formula missing"}

    elif not template_aware and rule_type == "referential_check":

        child_table  = rule_spec.get("child_table")
        parent_table = rule_spec.get("parent_table")

        if (
            child_table  not in CANONICAL_SCHEMA
            or parent_table not in CANONICAL_SCHEMA
        ):
            return {
                "drop": True,
                "reason": "referential_check tables not in canonical schema"
            }

    status = _rule_status_from_confidence(
        candidate.get("confidence"), threshold
    )

    return {
        "tenant_id":             contract_ctx.get("tenant_id"),
        "contract_id":           contract_ctx.get("contract_id"),
        "program_id":            contract_ctx.get("program_id"),
        "rule_engine":           "custom",
        "rule_class":            rule_class["name"],
        "rule_class_display":    rule_class["display_name"],
        "rule_name":             candidate.get("rule_name"),
        "rule_description":      candidate.get("rule_description"),
        "validation_stage":      candidate.get("stage") or rule_class["default_stage"],
        "severity":              candidate.get("severity") or rule_class["default_severity"],
        "canonical_target":      target,
        "rule_spec":             rule_spec,
        "error_message":         candidate.get("error_message"),
        "source_clause_id":      clause.get("clause_id"),
        "source_verbatim_text":  clause.get("text"),
        "source_page_number":    clause.get("page_number") or clause.get("page"),
        "generation_confidence": candidate.get("confidence"),
        "rule_status":           status,
        "created_by":            "ai_generator_v1"
    }


def normalize_stage_b_outputs(
    synth_outputs,
    contract_ctx,
    threshold=DEFAULT_RULE_AUTO_TRUST_THRESHOLD
):
    """
    Walk the output of stage_b_synthesizer.synthesize_rules and produce:
      - validation_rules: persistable validation_rule rows
      - dropped:          [{reason, clause_id, candidate}]
    """

    validation_rules = []
    dropped = []

    for entry in synth_outputs:

        clause = entry["clause"]
        classification = entry["classification"]
        engine = entry.get("engine")
        candidates = entry.get("candidates", [])

        for cand in candidates:

            if engine == "ajv":
                norm = normalize_ajv_rule(
                    cand, clause, classification, contract_ctx, threshold
                )
            elif engine == "custom":
                norm = normalize_custom_rule(
                    cand, clause, classification, contract_ctx, threshold
                )
            else:
                norm = {"drop": True, "reason": f"unknown engine: {engine}"}

            if isinstance(norm, dict) and norm.get("drop"):
                dropped.append({
                    "clause_id": clause.get("clause_id"),
                    "reason":    norm["reason"],
                    "candidate": cand
                })
                continue

            validation_rules.append(norm)

    return validation_rules, dropped


# =========================================================
# NEW (IR pipeline): verify gate + routing
# Each IR candidate is verified (validate → vocab-normalize → field-existence →
# compile → guard + dry-run → smoke) and routed to exactly one destination:
#   verified        → validation_rule (rule_status='proposed')
#   unmappable/fail → review_queue
#   non_validatable → control_register  (non-rule-bearing clauses)
# Polarity is carried by the template name, so there is no operator to invert.
# =========================================================


def _smoke_connection(output_schema):
    """Build a tiny DuckDB from the Output-Template sample rows for the verify
    gate's guard + dry-run + smoke checks. Returns (con, tables) or (None, None)
    when DuckDB / samples are unavailable (smoke is then skipped, not failed)."""
    try:
        from duckdb_validation import build_connection
        con, tables = build_connection(
            output_schema.sample_records(),
            schema_cols=output_schema.sheets_for_smoke(),
            label="smoke",   # creation-time verify, not a validation run
        )
        return con, tables
    except Exception as exc:
        print(f"[Pipeline 2.5-IR] smoke connection unavailable ({exc}); "
              f"compile-only verification.")
        return None, None


# Which param fields a template numerically/temporally compares — used to catch
# a rule bound to the WRONG output field (e.g. a max_limit on a text column).
_TEMPLATE_NUMERIC_FIELDS = {
    "max_limit": ["field"],
    "min_limit": ["field"],
    "range_check": ["field"],
    "aggregate_cap": ["field"],          # only when aggregation == 'sum'
    "cross_field_math": ["result_field", "left_field", "right_field"],
}
_TEMPLATE_DATE_FIELDS = {
    "date_relation": ["field", "other_field"],
    "period_duration": ["start_field", "end_field"],
    "date_bound": ["field"],
}


def _vals_any_numeric(vals):
    for v in vals:
        s = re.sub(r"[,$%\s]", "", str(v))
        try:
            float(s)
            return True
        except (TypeError, ValueError):
            continue
    return False


def _vals_any_date(vals):
    for v in vals:
        s = str(v).strip()
        if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", s) or \
           re.match(r"^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", s):
            return True
    return False


def _check_field_types(ir, output_schema):
    """Return a reason string if a numeric/date rule is bound to a field whose
    sample values clearly aren't numeric/date (a mis-binding), else None. Skips
    fields with no samples (can't tell)."""
    template = ir.get("template")
    params = ir.get("params") or {}

    num_keys = _TEMPLATE_NUMERIC_FIELDS.get(template, [])
    if template == "aggregate_cap" and str(params.get("aggregation", "")).lower() != "sum":
        num_keys = []  # count / distinct_count don't need a numeric field
    for k in num_keys:
        f = params.get(k)
        if isinstance(f, str) and f:
            sm = output_schema.samples_for(f)
            if sm and not _vals_any_numeric(sm):
                return (f"field {f!r} is not numeric (samples: {sm}) — likely the "
                        f"wrong field for {template}")

    for k in _TEMPLATE_DATE_FIELDS.get(template, []):
        f = params.get(k)
        if isinstance(f, str) and f:
            sm = output_schema.samples_for(f)
            if sm and not _vals_any_date(sm):
                return (f"field {f!r} is not a date (samples: {sm}) — likely the "
                        f"wrong field for {template}")
    return None


# Threshold params per numeric template — used to catch a percentage written as
# 100 when the field actually stores a 0-1 fraction (a no-op rule otherwise).
_TEMPLATE_THRESHOLD_KEYS = {
    "max_limit": ["max"], "min_limit": ["min"], "range_check": ["min", "max"],
}


def _check_numeric_scale(ir, output_schema):
    """Return a reason if a numeric threshold doesn't match the field's sample
    SCALE — specifically a 0-1 fraction field given a threshold > 1 (e.g. '100%'
    written as 100 against a 'Part of Limit %' column whose values are 0.2, 0.08).
    Such a rule never fires. Else None."""
    template = ir.get("template")
    params = ir.get("params") or {}
    keys = _TEMPLATE_THRESHOLD_KEYS.get(template, [])
    field = params.get("field")
    if not keys or not isinstance(field, str):
        return None
    sm = output_schema.samples_for(field)
    nums = []
    for v in sm:
        try:
            nums.append(float(re.sub(r"[,$%\s]", "", str(v))))
        except (TypeError, ValueError):
            continue
    if not nums or max(nums) > 1.0 or not any(n > 0 for n in nums):
        return None  # not a fraction field (or no samples) — can't tell
    for k in keys:
        try:
            t = float(params.get(k))
        except (TypeError, ValueError):
            continue
        if t > 1.0:
            return (f"field {field!r} holds fractions (samples {sm}) but {k}="
                    f"{params.get(k)} — looks like a percentage; use {t / 100} not "
                    f"{params.get(k)}")
    return None


def _autocorrect_numeric_scale(ir, output_schema):
    """Deterministically rescale a percentage threshold written as 10/100 when
    the bound column's SAMPLE values prove it stores fractions (0–1). E.g. a
    "Percentage of Total Layer — up to 100%" rule compiled as max=100 against a
    "Part of Limit %" column whose values are 0.2, 0.08 never fires; we rewrite
    max=1.0. Returns (possibly-new ir, note|None).

    This is a *correction*, not a rejection (unlike _check_numeric_scale), and is
    always applied because the fraction signal is high-confidence: it only fires
    when every sample value is in (0, 1]. It is a no-op for percent-as-integer
    columns (e.g. Commission % stored as 25) and for money columns."""
    template = ir.get("template")
    params = ir.get("params") or {}
    keys = _TEMPLATE_THRESHOLD_KEYS.get(template, [])
    field = params.get("field")
    if not keys or not isinstance(field, str):
        return ir, None

    sm = output_schema.samples_for(field)
    nums = []
    for v in sm:
        try:
            nums.append(float(re.sub(r"[,$%\s]", "", str(v))))
        except (TypeError, ValueError):
            continue
    if not nums or not any(n > 0 for n in nums):
        return ir, None

    # Match the rule value to the COLUMN's scale, in EITHER direction, so a rate
    # works whether the BDX stores it as a fraction (0.235) or as a percent (23.5):
    #   - PROVEN fraction column (all samples in (0, 1]) → divide a >1 value by 100.
    #   - PROVEN percent column (all samples in (1, 100]) → multiply a 0<v<=1 value
    #     by 100. The (1,100] bound excludes money/limit columns (thousands/
    #     millions), so only true percentage columns are rescaled upward.
    is_fraction_col = max(nums) <= 1.0
    is_percent_col = min(nums) > 1.0 and max(nums) <= 100.0
    if not (is_fraction_col or is_percent_col):
        return ir, None

    new_params = dict(params)
    changed = []
    for k in keys:
        try:
            t = float(new_params.get(k))
        except (TypeError, ValueError):
            continue
        if is_fraction_col and t > 1.0:
            new_params[k] = t / 100.0
            changed.append(f"{k}: {params.get(k)} → {new_params[k]} (→fraction)")
        elif is_percent_col and 0 < t <= 1.0:
            new_params[k] = t * 100.0
            changed.append(f"{k}: {params.get(k)} → {new_params[k]} (→percent)")
    if not changed:
        return ir, None

    new_ir = dict(ir)
    new_ir["params"] = new_params
    return new_ir, (f"rescaled percentage to fraction for {field!r} "
                    f"(samples {sm}): {', '.join(changed)}")


def _numeric_field_on_text(ir, output_schema):
    """Always-on deterministic guard: a numeric-threshold rule (max/min/range/
    sum-aggregate/math) bound to a column whose SAMPLE values are clearly
    non-numeric (text) is a wrong-column binding — return a reason so the verify
    gate routes it to review instead of shipping a rule that errors or matches
    nothing. Numeric templates ONLY: date templates are skipped on purpose,
    because BDX dates are stored as Excel serials that don't look like dates and
    would false-trigger. Skips columns with no samples (can't tell)."""
    template = ir.get("template")
    params = ir.get("params") or {}
    keys = _TEMPLATE_NUMERIC_FIELDS.get(template, [])
    if template == "aggregate_cap" and str(params.get("aggregation", "")).lower() != "sum":
        keys = []
    for k in keys:
        f = params.get(k)
        if isinstance(f, str) and f:
            sm = output_schema.samples_for(f)
            if sm and not _vals_any_numeric(sm):
                return (f"numeric rule bound to non-numeric column {f!r} "
                        f"(samples {sm}) — likely the wrong column")
    return None


# Country / nation names (normalized, punctuation+spaces stripped) that must
# never appear as an ALLOWED value in a STATE-level column.
_COUNTRY_ALIASES = {
    "unitedstatesofamerica", "unitedstates", "usa", "us", "usofa", "america",
    "unitedstatesamerica",
}


def _country_in_state_enum(ir, output_schema):
    """Always-on deterministic guard. A value_in_set INCLUSION on a STATE-level
    column whose allowed values include a COUNTRY name (e.g. "United States of
    America") is meaningless — a state cell never equals a country, so the rule is
    dead. This happens for "home state within <country> … excluding <sub-regions>"
    when the template has only state columns: the country inclusion should be
    DROPPED and only the paired exclusion (value_not_in_set of the sub-regions)
    kept. Return a reason so the verify gate routes the inclusion to review; the
    exclusion rule is a separate candidate and survives on its own."""
    if ir.get("template") != "value_in_set":
        return None
    params = ir.get("params") or {}
    field = params.get("field")
    if not isinstance(field, str) or "state" not in field.lower():
        return None
    allowed = params.get("allowed") or []
    for v in allowed:
        if isinstance(v, str) and re.sub(r"[^a-z0-9]", "", v.lower()) in _COUNTRY_ALIASES:
            return (f"inclusion lists a COUNTRY value ({v!r}) in the state-level "
                    f"column {field!r}; dropping the inclusion — the paired "
                    f"exclusion rule still applies")
    return None


# US territories & possessions — canonical name → surface variations (incl.
# abbreviations). A "home state excluding US territories/possessions" clause must
# list them ALL so a BDX row reporting any of them is caught, not just the ones
# the contract happened to name.
_US_TERRITORIES = {
    "Puerto Rico": ["PR"],
    "US Virgin Islands": ["USVI", "VI", "U.S. Virgin Islands", "Virgin Islands"],
    "Guam": ["GU"],
    "American Samoa": ["AS"],
    "Northern Mariana Islands": ["CNMI", "MP",
                                 "Commonwealth of the Northern Mariana Islands"],
    "US Minor Outlying Islands": ["UM", "United States Minor Outlying Islands"],
}
_TERRITORY_TRIGGER = re.compile(
    r"puerto\s*rico|virgin\s*islands|\bguam\b|american\s*samoa|mariana|"
    r"territor|possession|\busvi\b|\bcnmi\b", re.I)


def _expand_us_territory_exclusion(ir):
    """When a value_not_in_set on a STATE/territory column excludes any US
    territory/possession — or a generic "US Territories"/"Possessions" token —
    replace the excluded set with the FULL canonical list of US territories &
    possessions and seed their abbreviations as variation_values, so every
    reported territory is caught. Any non-territory excluded value is kept.
    Mutates and returns ir."""
    if ir.get("template") != "value_not_in_set":
        return ir
    params = ir.get("params") or {}
    field = params.get("field")
    if not isinstance(field, str) or "state" not in field.lower():
        return ir
    excluded = params.get("excluded") or []
    if not any(isinstance(v, str) and _TERRITORY_TRIGGER.search(v) for v in excluded):
        return ir
    extras = [v for v in excluded
              if isinstance(v, str) and not _TERRITORY_TRIGGER.search(v)]
    new_excluded, seen = [], set()
    for v in extras + list(_US_TERRITORIES.keys()):
        k = v.strip().lower()
        if k and k not in seen:
            seen.add(k)
            new_excluded.append(v)
    params["excluded"] = new_excluded
    ir["params"] = params
    return ir


def _seed_territory_abbreviations(ir):
    """Re-append the FULL set of US-territory names + abbreviations to a territory
    exclusion's variation_values. Runs AFTER the generic variation filter, which
    otherwise drops short codes (PR, GU, AS, MP, UM, USVI, CNMI) as "too generic"
    — but for a state/territory column those short codes ARE the BDX's canonical
    spelling, so they must stay in the match set. Only fires when the excluded set
    is (or includes) the canonical US-territory list. Mutates ir."""
    if ir.get("template") != "value_not_in_set":
        return ir
    params = ir.get("params") or {}
    field = params.get("field")
    if not isinstance(field, str) or "state" not in field.lower():
        return ir
    excluded = params.get("excluded") or []
    canon = {n.lower() for n in _US_TERRITORIES}
    if not any(isinstance(v, str) and v.strip().lower() in canon for v in excluded):
        return ir
    vv = list(params.get("variation_values") or [])
    for name, abbrs in _US_TERRITORIES.items():
        for form in [name] + abbrs:
            if form not in vv:
                vv.append(form)
    params["variation_values"] = vv
    ir["params"] = params
    return ir


def _drop_paired_state_inclusions(rules):
    """Territory carve-out cleanup. "Home state within <country> … excluding
    <sub-regions>" yields BOTH a value_in_set (the country / an incidental value
    like DC) and a value_not_in_set (the excluded territories) on the SAME state
    column from the SAME clause. The inclusion is redundant and HARMFUL — a
    value_in_set of just {DC} would flag every non-DC row — so keep ONLY the
    exclusion. Drops the value_in_set of such a same-field, same-clause pair on a
    STATE column. (Non-geographic allowed/excluded pairs — e.g. classes of
    business — are untouched: both remain meaningful.)"""
    excl_keys = set()
    for r in rules:
        ir = r.get("ir") or {}
        if ir.get("template") == "value_not_in_set":
            fld = (ir.get("params") or {}).get("field") or ""
            if "state" in fld.lower():
                excl_keys.add((r.get("source_clause_id"), fld))
    out = []
    for r in rules:
        ir = r.get("ir") or {}
        if ir.get("template") == "value_in_set":
            fld = (ir.get("params") or {}).get("field") or ""
            if "state" in fld.lower() and (r.get("source_clause_id"), fld) in excl_keys:
                print(f"  [drop] inclusion {r.get('rule_name')!r}: territory "
                      f"carve-out keeps only the exclusion on {fld!r}")
                continue
        out.append(r)
    return out


def _enum_set_mismatch(ir, output_schema):
    """Always-on (but narrow) deterministic guard for the wrong-column enum bind
    the prompt warns about (e.g. a 'US Surplus Lines' class value mapped onto a
    Primary/Excess column). Fires ONLY for a value-set/equals rule whose bound
    column's samples form a CLEAR BINARY enum (exactly 2 distinct sample values,
    like Primary/Excess or Yes/No) that NONE of the rule's literals match. Kept to
    binary columns on purpose: open columns (state, class) routinely sample down
    to 3-4 values, so a wider net would wrongly reject valid rules like a single
    'Alaska' check. Returns a reason (→ review) or None.

    EXCLUSION rules (value_not_in_set) are deliberately NOT checked here: an
    exclusion's literals (e.g. excluded classes "Cannabis", "Mobile Homes") are
    SUPPOSED to be absent from compliant sample data — that absence is the rule's
    whole purpose, not a wrong-column signal. Including not_in_set here
    false-rejected legitimate exclusion rules (the values never appear because the
    samples are clean), and a genuinely wrong-bound exclusion only ever yields a
    harmless no-op (it can never match), so there is nothing to catch. Only
    INCLUSION (value_in_set) and EXACT-MATCH (value_equals) rules — whose literals
    SHOULD appear in the column vocabulary — are guarded."""
    if ir.get("template") not in ("value_in_set", "value_equals"):
        return None
    from contract_upload_services.rule_ir import field_refs
    refs = field_refs(ir)
    if not refs:
        return None
    field = refs[0]
    sm = output_schema.samples_for(field)
    distinct = {str(s).strip().lower() for s in (sm or []) if str(s).strip()}
    if len(distinct) != 2:           # only a clear binary enum is reliable at N≈3 samples
        return None
    params = ir.get("params") or {}
    vals = []
    for v in params.values():
        if isinstance(v, str):
            vals.append(v)
        elif isinstance(v, list):
            vals.extend(str(x) for x in v if isinstance(x, (str, int, float)))
    fl = field.strip().lower()
    vals = [v.strip().lower() for v in vals if isinstance(v, str) and v.strip() and v.strip().lower() != fl]
    if not vals:
        return None

    def _match(a, b):
        return a == b or a in b or b in a

    if any(_match(rv, dv) for rv in vals for dv in distinct):
        return None  # at least one rule value is in the column's vocabulary — OK
    return (f"enum rule values {vals} are not in binary column {field!r}'s value "
            f"set {sorted(distinct)} — likely the wrong column")


def _stringset_on_numeric(ir, output_schema):
    """Always-on deterministic guard for the broken-numeric-query bug: a
    value_in_set / value_not_in_set / value_equals (fuzzy STRING set-match) rule
    whose literal value is a FRACTIONAL number and whose bound column is numeric
    is a wrong-TEMPLATE choice — a numeric equality/limit (e.g. "Commission rate =
    23.5%" → 0.235) got compiled as a jaro_winkler match against '0.235', which
    flags every row. Route to review so it is re-issued as a numeric rule.
    Limited to FRACTIONAL values on purpose so integer code-sets (Division '10',
    '20') — which legitimately use a value-set — are NOT touched. Returns a reason
    (→ review) or None."""
    if ir.get("template") not in ("value_in_set", "value_not_in_set", "value_equals"):
        return None
    from contract_upload_services.rule_ir import field_refs
    refs = field_refs(ir)
    if not refs:
        return None
    field = refs[0]
    params = ir.get("params") or {}
    vals = params.get("allowed")
    if vals is None:
        vals = params.get("values", params.get("value"))
    if not isinstance(vals, list):
        vals = [vals]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None

    def _is_fractional(x):
        try:
            f = float(str(x).strip().rstrip("%"))
        except (TypeError, ValueError):
            return False
        return f != int(f)

    if not any(_is_fractional(v) for v in vals):
        return None  # integer / non-numeric values — a value-set may be correct
    sm = output_schema.samples_for(field)
    if not sm or not _vals_any_numeric(sm):
        return None  # column isn't numeric — a different problem (or genuine enum)
    return (f"value-set rule on numeric column {field!r} with fractional value(s) "
            f"{vals} — should be a numeric equals/limit, not a fuzzy string match")


def _resolve_rule_class_for_template(template):
    """Best-effort rule_class lookup for the template's rule_type (for the
    rule_class / rule_class_display columns). Never blocks a rule."""
    from contract_upload_services.rule_ir import TEMPLATE_CATALOG
    spec = TEMPLATE_CATALOG.get(template) or {}
    rt = spec.get("rule_type")
    cls = _RULE_CLASS_BY_NAME.get(rt) or {}
    return rt, cls


# Param-key aliases the model occasionally emits instead of the canonical name.
# Left silent, they'd be ignored by the compiler — turning e.g. a SCOPED limit
# into an unscoped one that flags every row.
_PARAM_ALIASES = {
    "row_scope": "scope", "rowscope": "scope", "filter": "scope",
    "where": "scope", "allowed_values": "allowed", "excluded_values": "excluded",
    "group": "group_by",
}


def _normalize_param_aliases(ir):
    """Rename known param aliases (e.g. row_scope→scope) to the canonical keys the
    compiler expects. Never overwrites a canonical key the model already set."""
    if not isinstance(ir, dict):
        return ir
    params = ir.get("params")
    if not isinstance(params, dict):
        return ir
    new = dict(params)
    for alias, canon in _PARAM_ALIASES.items():
        if alias in new and canon not in new:
            new[canon] = new.pop(alias)
    out = dict(ir)
    out["params"] = new
    return out


# Values a referral-indicator column uses to mean "this policy WAS referred".
_REFERRED_TOKENS = {"yes", "y", "true", "referred", "1"}


def _find_referral_indicator(output_schema):
    """Return (field_name, referred_value) for the template's referral-indicator
    column, or (None, None). Prefers a name containing 'referral' + indicator/flag;
    falls back to any field whose name contains 'referral'."""
    names = list(getattr(output_schema, "field_names", []) or [])
    pick = None
    for n in names:
        nl = n.lower()
        if "referral" in nl and ("indicator" in nl or "flag" in nl or "status" in nl):
            pick = n
            break
    if not pick:
        pick = next((n for n in names if "referral" in n.lower()), None)
    if not pick:
        return None, None
    referred = "Yes"
    try:
        for v in (output_schema.allowed_values_for(pick) or []):
            if str(v).strip().lower() in _REFERRED_TOKENS:
                referred = v
                break
    except Exception:
        pass
    return pick, referred


def _trigger_condition_from_ir(ir):
    """Express the model's trigger rule as ONE {field, op, value} condition (the
    state under which a referral is required). Returns None when it can't be
    reduced to a single condition (left for the model's plain rule / review)."""
    t = ir.get("template")
    p = ir.get("params") or {}
    f = p.get("field")
    # A referral trigger is the zone the contract flags: "over X", "at least /
    # present (>= X)", or "equals V". max → exceeds (> max); min → at-least/present
    # (>= min, e.g. "any net retention" with min≈0); set membership → equals.
    if t == "max_limit" and f and p.get("max") is not None:
        return {"field": f, "op": ">", "value": p["max"]}
    if t == "min_limit" and f and p.get("min") is not None:
        return {"field": f, "op": ">=", "value": p["min"]}
    if t in ("value_in_set", "value_not_in_set"):
        vals = p.get("allowed") or p.get("excluded") or []
        if f and len(vals) == 1:
            return {"field": f, "op": "=", "value": vals[0]}
    if t == "conditional_value" and isinstance(p.get("condition"), dict):
        return p["condition"]
    return None


# Logical negation of a comparison operator — used to turn an "except <X>"
# exemption (the compliant zone) into the DEVIATION zone that needs referral.
_NEGATE_OP = {"=": "!=", "!=": "=", "<>": "=",
              ">": "<=", "<=": ">", "<": ">=", ">=": "<"}


def _req_norm(v):
    return " ".join(str(v).split()).strip().lower()


def _pred(field, op, value):
    return {"field": _req_norm(field), "op": str(op or "=").strip() or "=",
            "value": _req_norm(value)}


def _disjoint(a, b):
    """True when two predicates on the SAME column can never hold for one row.

    Only the two cases that are certain: `= v` against `!= v`, and `= v1` against
    `= v2`. Anything else (ranges, different columns, unknown ops) is treated as
    possibly-overlapping, so the caller stays conservative.
    """
    if not a or not b or a["field"] != b["field"]:
        return False
    eq = {"=", "=="}
    ne = {"!=", "<>"}
    if a["op"] in eq and b["op"] in ne:
        return a["value"] == b["value"]
    if a["op"] in ne and b["op"] in eq:
        return a["value"] == b["value"]
    if a["op"] in eq and b["op"] in eq:
        return a["value"] != b["value"]
    return False


def _scope_preds(params):
    """The scope of a set rule as a list of predicates. `scope` is either
    {col: value} (an equality) or {col: {op, value}}."""
    out = []
    scope = params.get("scope")
    if not isinstance(scope, dict):
        return out
    for col, cond in scope.items():
        if isinstance(cond, dict):
            out.append(_pred(col, cond.get("op"), cond.get("value")))
        else:
            out.append(_pred(col, "=", cond))
    return out


def _required_values_by_field(rules):
    """field -> {required value -> list of gates under which it is required}.

    Only NON-referral rules count: a referral is a "please review this" signal,
    while these are the contract's hard statements of what a compliant row looks
    like. A gate of None means "required unconditionally".
    """
    req = {}

    def add(field, value, gate):
        if field is None or value is None:
            return
        req.setdefault(_req_norm(field), {}).setdefault(_req_norm(value), []).append(gate)

    for r in rules:
        if not isinstance(r, dict) or r.get("is_referral"):
            continue
        ir = r.get("ir") or {}
        p = ir.get("params") or {}
        t = ir.get("template")
        if t == "value_in_set":
            # A SINGLE-value allow-list is "this column must be <v>". A multi-value
            # one is a DOMAIN ("must be one of Yes/No", "one of these carriers") and
            # says nothing about which member a given row must carry, so it is not
            # evidence that a referral naming one member has the wrong polarity.
            allowed = p.get("allowed") or []
            if len(allowed) == 1:
                gates = _scope_preds(p) or [None]
                for g in gates:
                    add(p.get("field"), allowed[0], g)
        elif t in ("conditional_value", "conditional_all"):
            if str(p.get("op") or "").strip() == "=":
                conds = ([p["condition"]] if isinstance(p.get("condition"), dict)
                         else list(p.get("conditions") or []))
                gates = [_pred(c.get("field"), c.get("op"), c.get("value"))
                         for c in conds if isinstance(c, dict) and c.get("field")]
                for g in (gates or [None]):
                    add(p.get("field"), p.get("value"), g)
    return req


def _flat_norm(v):
    """A scope/allowed value as a normalised SET — the extractor writes either a
    scalar ("California") or a list of surface forms (["California","CA"])."""
    if isinstance(v, (list, tuple, set)):
        return frozenset(_req_norm(x) for x in v if x is not None)
    return frozenset([_req_norm(v)])


def _scope_shape(params):
    """{restriction column -> (op, value-set)} for a rule's scope.

    A set rule (`value_in_set`/`value_not_in_set`) carries its restriction in
    `scope`; a `conditional_value` rule carries the SAME idea in `condition`
    ({field, op, value}). Both reduce to the identical shape here so one
    requirement written either way compares equal."""
    out = {}
    scope = params.get("scope")
    if isinstance(scope, dict):
        for col, cond in scope.items():
            key = _req_norm(col)
            if isinstance(cond, dict):
                # Three spellings are in circulation for the same idea, so all
                # three must reduce to the same (op, values) shape or a
                # comparison silently fails to match:  {"op":"!=","value":X} ·
                # {"excluded":[X]} (Call-3's current output) · {"allowed":[X]}.
                if cond.get("excluded") is not None:
                    out[key] = ("!=", _flat_norm(cond["excluded"]))
                elif cond.get("allowed") is not None:
                    out[key] = ("=", _flat_norm(cond["allowed"]))
                else:
                    out[key] = (str(cond.get("op") or "=").strip(), _flat_norm(cond.get("value")))
            else:
                out[key] = ("=", _flat_norm(cond))
    # conditional_value carries one `condition`; conditional_all/any carry a
    # `conditions` list. Fold every predicate into the same shape — but SKIP a
    # predicate on the target field itself: for a referral that names the
    # required value, "<target> != <value>" is only the deviation restated, not
    # a scoping restriction, and would otherwise make the shape differ from the
    # plain requirement.
    target = _req_norm(params["field"]) if params.get("field") is not None else None
    preds = []
    if isinstance(params.get("condition"), dict):
        preds.append(params["condition"])
    if isinstance(params.get("conditions"), list):
        preds.extend(c for c in params["conditions"] if isinstance(c, dict))
    for c in preds:
        ccol = c.get("field") or c.get("col")
        if ccol is None:
            continue
        key = _req_norm(ccol)
        if key == target:
            continue
        out[key] = (str(c.get("op") or "=").strip(), _flat_norm(c.get("value")))
    return out


_OPPOSITE = {"=": {"!=", "<>"}, "==": {"!=", "<>"}, "!=": {"=", "=="}, "<>": {"=", "=="}}


def _scopes_complementary(a, b):
    """b selects exactly the zone a EXCLUDES (same columns, same values, flipped
    operator) — i.e. b is the carve-out a was written to exempt."""
    if not a or not b or set(a) != set(b):
        return False
    flipped = False
    for col, (op_a, val_a) in a.items():
        op_b, val_b = b[col]
        if val_a != val_b:
            return False
        if op_b in _OPPOSITE.get(op_a, set()):
            flipped = True
        elif op_a != op_b:
            return False
    return flipped


def _scopes_same_shape(a, b):
    """Same restriction expressed on possibly DIFFERENT columns — the extractor
    binding one clause's "home state" to `Risk State` in one pass and `Insured
    State` in another still means the same thing."""
    if len(a) != len(b):
        return False
    return sorted(a.values(), key=repr) == sorted(b.values(), key=repr)


def _entity_values(params):
    """(the values the rule names, those values PLUS their surface variations).
    Two rules name the same real-world entity when each one's values appear in
    the other's value+variation vocabulary — that is how "Demoshield Specialty"
    and "Demoshield Specialty Insurance Company, Inc." are recognised as one
    carrier without any hard-coded name list."""
    raw = params.get("allowed") or params.get("excluded")
    if not raw and params.get("value") is not None:
        # conditional_value names its required value in `value` (scalar or list),
        # not in allowed/excluded.
        v = params.get("value")
        raw = list(v) if isinstance(v, (list, tuple, set)) else [v]
    prim = frozenset(_req_norm(v) for v in (raw or []) if v is not None)
    var = frozenset(_req_norm(v) for v in (params.get("variation_values") or []) if v is not None)
    return prim, prim | var


def _same_entity(a, b):
    return bool(a["prim"] and b["prim"]
                and a["prim"] <= b["full"] and b["prim"] <= a["full"])


# Universal US state name<->USPS-code reference (50 states + DC), applied
# symmetrically to whichever state a clause names — no per-state special-casing.
_US_STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "District of Columbia": "DC",
}
_STATE_NAME_BY_LOWER = {n.lower(): n for n in _US_STATE_ABBR}
_STATE_CODE_TO_NAME = {c.lower(): n for n, c in _US_STATE_ABBR.items()}


def _expand_state_values(values):
    """[S] -> [S plus its USPS-code/full-name counterpart], de-duplicated."""
    out = []
    for v in values or []:
        if v is None:
            continue
        out.append(v)
        k = str(v).strip().lower()
        if k in _STATE_CODE_TO_NAME:
            out.append(_STATE_CODE_TO_NAME[k])
        elif k in _STATE_NAME_BY_LOWER:
            out.append(_US_STATE_ABBR[_STATE_NAME_BY_LOWER[k]])
    seen, res = set(), []
    for v in out:
        k = str(v).strip().lower()
        if k not in seen:
            seen.add(k)
            res.append(v)
    return res


def _carveout_state(params):
    """(state_column, [carved-out state value(s)]) for a 'field == E EXCEPT state
    S' requirement, reading the 'state != S' predicate from scope / condition /
    conditions. (None, None) if there is no state carve-out."""
    target = _req_norm(params.get("field")) if params.get("field") is not None else None
    scope = params.get("scope")
    if isinstance(scope, dict):
        for col, cond in scope.items():
            if "state" not in str(col).lower():
                continue
            if isinstance(cond, dict):
                if cond.get("excluded") is not None:
                    return col, list(cond["excluded"])
                if str(cond.get("op") or "").strip() in ("!=", "<>") and cond.get("value") is not None:
                    v = cond["value"]
                    return col, (list(v) if isinstance(v, (list, tuple, set)) else [v])
    preds = []
    if isinstance(params.get("condition"), dict):
        preds.append(params["condition"])
    if isinstance(params.get("conditions"), list):
        preds.extend(c for c in params["conditions"] if isinstance(c, dict))
    for c in preds:
        col = c.get("field") or c.get("col")
        if col is None or _req_norm(col) == target or "state" not in str(col).lower():
            continue
        if str(c.get("op") or "").strip() in ("!=", "<>") and c.get("value") is not None:
            v = c["value"]
            return col, (list(v) if isinstance(v, (list, tuple, set)) else [v])
    return None, None


def _synthesize_carveout_prohibition(rules, output_schema):
    """Bidirectional routing backstop. "use E for all policies EXCEPT state S; any
    deviation requires referral" on a field with >=2 authorised values (the
    contract's OWN enumeration) is a ROUTING rule: S must NOT be E. The extractor
    emits only the requirement (A); synthesize the complementary prohibition (B)
    deterministically. Generic (E and the routing test come from the contract's
    own rules; only the universal state table is used). Idempotent."""
    import copy as _copy
    from contract_upload_services.rule_compiler import compile_ir

    authorized = {}
    for r in rules:
        ir = r.get("ir") or {}
        if ir.get("template") in ("value_in_set", "conditional_value", "conditional_all"):
            prim, _ = _entity_values(ir.get("params") or {})
            fld = (ir.get("params") or {}).get("field")
            if fld and prim:
                authorized.setdefault(_req_norm(fld), set()).update(prim)

    by_cf = {}
    for r in rules:
        p = (r.get("ir") or {}).get("params") or {}
        f, cid = p.get("field"), r.get("source_clause_id")
        if f is not None and cid is not None:
            by_cf.setdefault((cid, _req_norm(f)), []).append(r)

    added = []
    for r in rules:
        ir = r.get("ir") or {}
        if ir.get("template") not in ("value_in_set", "conditional_value", "conditional_all"):
            continue
        p = ir.get("params") or {}
        f, cid = p.get("field"), r.get("source_clause_id")
        if f is None or cid is None:
            continue
        if len(authorized.get(_req_norm(f), set())) < 2:          # ROUTING test
            continue
        state_col, S = _carveout_state(p)
        if not state_col or not S:
            continue
        prim, full = _entity_values(p)
        if not prim:
            continue
        a_scope = _scope_shape(p)
        already = False
        for o in by_cf.get((cid, _req_norm(f)), []):
            if o is r:
                continue
            oir = o.get("ir") or {}
            # The B-direction (carve-out prohibition) can already be present in
            # ANY of these shapes — Call-3 sometimes writes it as value_not_in_set
            # (the canonical form this function itself synthesizes), but just as
            # often as a conditional_value/conditional_all whose condition is the
            # carve-out state and whose target negates the required value (e.g.
            # "when Insured State = CA, Carrier != Demoshield Specialty"). A
            # literal template-name check misses that second shape entirely and
            # synthesizes a REDUNDANT, conflicting-severity duplicate of a rule
            # that already exists. Compare by SHAPE (entity + complementary
            # scope), never by template name.
            if oir.get("template") not in (
                    "value_not_in_set", "conditional_value", "conditional_all"):
                continue
            op_prim, op_full = _entity_values(oir.get("params") or {})
            if _same_entity({"prim": prim, "full": full}, {"prim": op_prim, "full": op_full}) \
                    and _scopes_complementary(a_scope, _scope_shape(oir.get("params") or {})):
                already = True
                break
        if already:
            continue
        raw = list(p.get("allowed") or [])
        if p.get("value") is not None:
            raw += p["value"] if isinstance(p["value"], (list, tuple)) else [p["value"]]
        raw += list(p.get("variation_values") or [])
        forms = list(dict.fromkeys(v for v in raw if v is not None))
        forms = [v for v in forms if len(str(v).split()) >= 2] or forms
        if not forms:
            continue
        s_disp = " / ".join(str(x) for x in S)
        b_ir = {
            "template": "value_not_in_set", "is_referral": bool(ir.get("is_referral")),
            "severity": ir.get("severity") or r.get("severity") or "warning",
            "confidence": ir.get("confidence", 1.0),
            "params": {"field": f, "scope": {state_col: {"op": "=", "value": _expand_state_values(S)}},
                       "excluded": forms, "variation_values": forms},
            "rule_name": f"{f} carve-out prohibition ({s_disp})",
            "rule_description": (f"For a {s_disp} home-state policy, {f} must NOT be the value required for "
                                 f"all other policies (it is routed to a different authorised value); any "
                                 f"deviation requires referral."),
            "error_message": f"Referral required: {f} must not be the mandated value for a {s_disp} policy.",
        }
        try:
            sql = compile_ir(b_ir, output_schema.field_to_sheets,
                             default_sheet=output_schema.primary_sheet)
        except Exception as exc:
            print(f"  [synthesize-B] clause {cid}: compile failed ({exc})")
            continue
        b = _copy.deepcopy(r)
        b["ir"] = b_ir
        b["template"] = "value_not_in_set"
        b["rule_name"] = b_ir["rule_name"]
        b["rule_description"] = b_ir["rule_description"]
        b["error_message"] = b_ir["error_message"]
        b["severity"] = b_ir["severity"]
        b["is_referral"] = bool(ir.get("is_referral"))
        b["compiled_sql"] = sql
        rs = dict(b.get("rule_spec") or {})
        rs["ir"] = b_ir
        rs["compiled_sql"] = sql
        b["rule_spec"] = rs
        added.append(b)
        by_cf.setdefault((cid, _req_norm(f)), []).append(b)
        print(f"  [synthesize-B] clause {cid}: {f} must NOT be mandated value for {s_disp} "
              f"(>=2 authorised values) — synthesized complementary prohibition")
    if added:
        print(f"  [synthesize-B] added {len(added)} carve-out prohibition rule(s)")
    return rules + added


def _consolidate_clause_duplicates(rules):
    """Collapse the DUPLICATE rules one clause emits for a SINGLE direction.

    A clause of the form "use <value> for all policies EXCEPT <carve-out>; any
    deviation requires referral" is a BIDIRECTIONAL routing rule when there are
    several authorised values (e.g. several writing companies):
        A  outside the carve-out, the column MUST be <value>
        B  inside  the carve-out, the column must NOT be <value>
           (the carve-out is routed to a DIFFERENT authorised value)
    BOTH directions are real deviations that require referral, so BOTH A and B
    are KEPT. An earlier version dropped B as an "invented" prohibition on the
    theory that "except X exempts, it does not prohibit" — that was wrong for a
    routing clause: with a substitute value available, a carve-out row that sits
    on <value> IS a deviation (verified against the Demoshield/Palms paper
    clause, where a California policy on Demoshield Specialty paper must flag).

    What this DOES collapse is the A direction emitted MORE THAN ONCE — Call-3
    spells the same requirement several ways at the same time:

        A1 value_in_set      (col in [<value>],  scope: state != <carve-out>)
        A2 conditional_value (col == <value> when state != <carve-out>)
        A3 conditional_all   (… whose extra "col != <value>" condition just
                              restates the deviation)

    These are the SAME requirement and are matched by SHAPE (entity +
    restriction, ignoring any predicate on the target column), never by template
    name. The survivor is the referral form when the clause's consequence is a
    referral (that is what `is_referral` records), so the severity the clause
    actually calls for is the one that is kept. The B direction (value_not_in_set
    on the carve-out) is never a "duplicate" of A and is left untouched.

    Everything is keyed on source_clause_id, so rules from DIFFERENT clauses are
    never merged, and a clause that legitimately yields several distinct rules
    (different columns, or different values) is untouched.
    """
    groups = {}
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            continue
        ir = r.get("ir") or {}
        if ir.get("template") not in ("value_in_set", "value_not_in_set",
                                       "conditional_value", "conditional_all"):
            continue
        p = ir.get("params") or {}
        field, cid = p.get("field"), r.get("source_clause_id")
        if field is None or cid is None:
            continue
        prim, full = _entity_values(p)
        groups.setdefault((cid, _req_norm(field)), []).append({
            "i": i, "tmpl": ir.get("template"), "prim": prim, "full": full,
            "scope": _scope_shape(p), "ref": bool(ir.get("is_referral")),
            "name": r.get("rule_name"),
        })

    drop = {}
    for (cid, field), items in groups.items():
        if len(items) < 2:
            continue
        # The carve-out prohibition (value_not_in_set on the carve-out) is the B
        # direction of a bidirectional routing clause, NOT a duplicate — keep it.
        # Only the A direction, emitted more than once, is collapsed below.
        reqs = [x for x in items if x["tmpl"] in ("value_in_set", "conditional_value", "conditional_all") and x["i"] not in drop]
        for n, a in enumerate(reqs):
            if a["i"] in drop:
                continue
            for b in reqs[n + 1:]:
                if b["i"] in drop:
                    continue
                if not (_same_entity(a, b) and _scopes_same_shape(a["scope"], b["scope"])):
                    continue
                # Keep the referral form when the clause's consequence IS a
                # referral; otherwise keep the earlier rule. Deterministic either way.
                loser = (b if (a["ref"] or not b["ref"]) else a)
                keeper = a if loser is b else b
                drop[loser["i"]] = (f"clause {cid}: same requirement as "
                                    f"{keeper['name']!r} on the same column")

    if not drop:
        return rules
    for i, why in sorted(drop.items()):
        print(f"  [clause-merge] dropped {rules[i].get('rule_name')!r} — {why}")
    return [r for i, r in enumerate(rules) if i not in drop]


def _consolidate_cross_field_referral_duplicates(rules):
    """Collapse a referral TRIGGER restated on a DIFFERENT column.

    `_consolidate_clause_duplicates` only compares rules within the SAME
    (source_clause_id, field) group, so it never sees this shape: extraction
    chunks one contract paragraph differently run-to-run, splitting a single
    referral sentence (e.g. "All SUPER Specialty policies require referral to
    the Company") into its OWN isolated clause. With no surrounding "paper"
    context in that isolated clause, the mapper can bind it to a plausible but
    WRONG column (e.g. a "Division" field) instead of the column the SAME
    concept is already bound to elsewhere (e.g. "Carrier") — producing two
    live rules for one real-world trigger, each on a different field.

    Scoped narrowly to avoid false merges: only `value_not_in_set` referral
    triggers (is_referral=True) with NO scope (an unconditional trigger — a
    scoped one is far less likely to be an accidental duplicate) are compared.
    Two are the same trigger when one's excluded/variation vocabulary and the
    other's overlap by a HIGH fraction of the SHORTER phrase's own words (the
    same word-ratio test variation_traces_to_base uses elsewhere in this
    file) — e.g. "SUPER Specialty paper" vs "SUPER Specialty" — AND the
    shorter phrase is at least 2 words, so a single generic/ambiguous word
    can never trigger a merge on its own. Keeps the higher-confidence rule.
    Generic: nothing about "SUPER Specialty" or any other name is hardcoded —
    only the shape (referral trigger, unconditional, high word-overlap) is."""
    def _phrase_words(r):
        ir = r.get("ir") or {}
        p = ir.get("params") or {}
        vals = list(p.get("excluded") or []) + list(p.get("variation_values") or [])
        return {str(v).strip().lower() for v in vals if str(v).strip()}

    candidates = []
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            continue
        ir = r.get("ir") or {}
        if ir.get("template") != "value_not_in_set" or not ir.get("is_referral"):
            continue
        p = ir.get("params") or {}
        if p.get("scope") or p.get("condition") or p.get("conditions"):
            continue                                # scoped — not this shape
        words = _phrase_words(r)
        if not words:
            continue
        candidates.append({"i": i, "words": words, "conf": ir.get("confidence") or 0,
                            "name": r.get("rule_name")})

    drop = {}
    for n, a in enumerate(candidates):
        if a["i"] in drop:
            continue
        for b in candidates[n + 1:]:
            if b["i"] in drop:
                continue
            best = 0.0
            for pa in a["words"]:
                wa = set(re.findall(r"[a-z0-9]+", pa))
                if len(wa) < 2:
                    continue
                for pb in b["words"]:
                    wb = set(re.findall(r"[a-z0-9]+", pb))
                    if len(wb) < 2:
                        continue
                    ratio = len(wa & wb) / min(len(wa), len(wb))
                    best = max(best, ratio)
            if best < 1.0:
                continue                             # require a full-overlap match
            loser = a if a["conf"] < b["conf"] else b
            keeper = b if loser is a else a
            drop[loser["i"]] = (f"same referral trigger as {keeper['name']!r} "
                                 f"restated on a different field")

    if not drop:
        return rules
    for i, why in sorted(drop.items()):
        print(f"  [cross-field-merge] dropped {rules[i].get('rule_name')!r} — {why}")
    return [r for i, r in enumerate(rules) if i not in drop]


_SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}


def _formula_identity_key(ir):
    """Canonical fingerprint of a row-level arithmetic EQUALITY, built so that the
    SAME equation solved for a DIFFERENT variable produces the SAME key.

    An identity between money columns has as many equivalent spellings as it has
    variables. "Earned = Collected − Unearned", "Unearned = Collected − Earned"
    and "Collected = Earned + Unearned" are ONE equation: a row that breaks it
    breaks all three, so the reviewer gets three exceptions for one discrepancy.
    Nothing upstream catches this — the `covered` guard in the formula derivers
    only blocks a second formula for the SAME result column, and each
    rearrangement isolates a different column, so all of them sail through.

    Each rule is reduced to a coefficient map over its columns plus a constant:

        add space (sum / difference):     Σ coef·col  =  const
        mul space (product / quotient):   Π col^coef  =  const

    `result = left OP right` is moved onto one side, then EVERY coefficient (and
    the constant) is divided by the coefficient of the alphabetically-first
    column. That division cancels the arbitrary choice of which variable was
    isolated — and the sign flip that rearranging introduces with it — so all
    spellings of one equation land on one key. A percent-stored operand
    (`left_is_percent` / `right_is_percent`) contributes its ÷100 to the
    coefficient (add space) or to the constant (mul space), so a scaled formula
    is never confused with its unscaled twin.

    Returns None — meaning "never merge this rule" — for anything that is not a
    plain field-to-field equality: an inequality is a threshold, not an identity;
    a non-positive multiplicative constant has no well-defined root; and a
    template this function does not model is left alone. Scope and referral flag
    are part of the key, so two equations that hold under different conditions
    stay separate.
    """
    template = ir.get("template")
    params = ir.get("params") or {}
    terms, const, space = {}, 0.0, None

    def _add(field, coef):
        terms[_req_norm(field)] = terms.get(_req_norm(field), 0.0) + coef

    def _pct(flag):
        return 0.01 if params.get(flag) else 1.0

    if template == "cross_field_math":
        res = params.get("result_field")
        left = params.get("left_field")
        right = params.get("right_field")
        op = params.get("operator")
        if not all(isinstance(x, str) and x.strip() for x in (res, left, right)):
            return None
        if op in ("+", "-"):
            # result = left ± right  →  result − left ∓ right = 0
            space = "add"
            _add(res, 1.0)
            _add(left, -_pct("left_is_percent"))
            _add(right, (-1.0 if op == "+" else 1.0) * _pct("right_is_percent"))
        elif op in ("*", "/"):
            # result = left {×,÷} right  →  result · left⁻¹ · right∓¹ = 100^e,
            # where e collects the ÷100 of each percent-stored operand.
            space = "mul"
            _add(res, 1.0)
            _add(left, -1.0)
            _add(right, -1.0 if op == "*" else 1.0)
            a = 1 if params.get("left_is_percent") else 0
            b = 1 if params.get("right_is_percent") else 0
            const = 100.0 ** (-(a + b) if op == "*" else (b - a))
        else:
            return None

    elif template == "cross_field_compare":
        # Only the DEFINITIONAL form is an identity; ">=", "<=" … are thresholds.
        if str(params.get("op") or "").strip() != "=":
            return None
        field = params.get("field")
        other = params.get("other_field")
        if not all(isinstance(x, str) and x.strip() for x in (field, other)):
            return None
        operator = params.get("operator")
        try:
            factor = float(params.get("factor", 1)) if operator else 1.0
        except (TypeError, ValueError):
            return None
        if operator in (None, "", "*", "/"):
            # field = other [{×,÷} factor]  →  field · other⁻¹ = factor
            space = "mul"
            _add(field, 1.0)
            _add(other, -1.0)
            if operator == "/":
                if factor == 0:
                    return None
                factor = 1.0 / factor
            const = factor
        elif operator in ("+", "-"):
            # field = other ± factor  →  field − other = ±factor
            space = "add"
            _add(field, 1.0)
            _add(other, -1.0)
            const = factor if operator == "+" else -factor
        else:
            return None
    else:
        return None

    terms = {f: c for f, c in terms.items() if abs(c) > 1e-12}
    if not terms:
        return None
    if space == "mul" and const <= 0:
        return None                     # no real root — leave the rule alone

    # Divide through by the anchor column's coefficient: this is what makes the
    # key independent of which variable the rule happened to solve for.
    def _r(x):
        # round() can hand back -0.0, which prints as a different key than 0.0
        # even though it compares equal — normalise it away so a logged key reads
        # the same every time.
        return round(x, 9) or 0.0

    anchor = min(terms)
    scale = terms[anchor]
    norm = tuple(sorted((f, _r(c / scale)) for f, c in terms.items()))
    const = _r(const ** (1.0 / scale) if space == "mul" else const / scale)

    return (space, norm, const,
            tuple(sorted(_scope_shape(params).items())),
            bool(ir.get("is_referral")))


def _consolidate_equivalent_formula_rules(rules):
    """Keep ONE rule per arithmetic identity, however many ways it was written.

    Cross-field formula rules reach this point from several independent sources —
    the Call-3 clause mapper, a per-column formula annotation on the output
    template, the AI formula inference for templates that carry no annotation
    row, and the generic library. Each source is deduped by RESULT COLUMN, which
    is blind to the fact that "A = B − C", "C = B − A" and "B = A + C" are the
    same equation. When more than one of them survives, every row that breaks the
    identity is reported once per spelling: three exceptions, one real problem,
    and an exception count inflated by the number of variables.

    Rules are grouped by `_formula_identity_key` (see there for the canonical
    form; it returns None for anything that must never be merged). Within a
    group the survivor is the strongest statement of the identity — highest
    severity first, then highest confidence, then the earliest rule, so the
    outcome is deterministic and a `critical` check is never dropped in favour of
    a `warning`. Generic by construction: the key is pure algebra over whatever
    columns the rule names, so nothing about premiums, or any column or program,
    is assumed."""
    groups = {}
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            continue
        key = _formula_identity_key(r.get("ir") or {})
        if key is not None:
            groups.setdefault(key, []).append(i)

    def _strength(i):
        ir = rules[i].get("ir") or {}
        sev = str(ir.get("severity") or rules[i].get("severity") or "").strip().lower()
        try:
            conf = float(ir.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        return (_SEVERITY_RANK.get(sev, 0), conf, -i)

    drop = {}
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        keeper = max(idxs, key=_strength)
        for i in idxs:
            if i != keeper:
                drop[i] = (f"same equation as {rules[keeper].get('rule_name')!r}, "
                           f"solved for a different column")

    if not drop:
        return rules
    for i, why in sorted(drop.items()):
        print(f"  [formula-merge] dropped {rules[i].get('rule_name')!r} — {why}")
    return [r for i, r in enumerate(rules) if i not in drop]


def _unflip_required_value_referrals(rules, output_schema):
    """Undo a referral polarity flip that contradicts the contract's own requirements.

    `_flip_value_in_set_referral` turns a referral's `value_in_set` into a
    `value_not_in_set` because a referral trigger usually LISTS the values that
    demand referral ("any policy written in AK/HI") — flagging rows that MATCH is
    then right. But a "deviation" referral is the mirror image: "use <paper> for
    every policy except <state>; any deviation requires referral" names the value a
    row must CARRY, so the same flip inverts it and the rule flags every COMPLIANT
    row while the deviating ones pass.

    The two are told apart by the contract itself, not by any word list: if every
    value the referral now EXCLUDES on a column is a value another (non-referral)
    rule REQUIRES on that same column, the pair is a flat contradiction — one rule
    says the column must be X, the other says it must not be. The requirement is
    the contract's own hard statement, so the referral is the one with the wrong
    polarity; flip it back to `value_in_set`, where the engine flags exactly the
    rows that deviate from the required value.

    Exact (whitespace/case-normalised) value matching only — a referral naming a
    DIFFERENT value on the same column (e.g. a second paper type that always needs
    referral) shares no value with the requirement and is left untouched.
    """
    from contract_upload_services.rule_compiler import compile_ir

    required = _required_values_by_field(rules)
    if not required:
        return rules
    for r in rules:
        if not isinstance(r, dict) or not r.get("is_referral"):
            continue
        ir = r.get("ir") or {}
        if ir.get("template") != "value_not_in_set":
            continue
        p = ir.get("params") or {}
        excluded = p.get("excluded")
        field = p.get("field")
        if not field or not isinstance(excluded, list) or not excluded:
            continue
        needed = required.get(_req_norm(field))
        if not needed:
            continue
        # Every excluded value must be one the contract REQUIRES on this column,
        # under a gate that can actually co-occur with this referral's own scope.
        # A requirement gated on "state != CA" and a referral scoped to "state =
        # CA" cover disjoint zones — they never contradict, so that referral is
        # saying something different and is left exactly as it is.
        scope = _scope_preds(p)
        def _contradicts(v):
            gates = needed.get(_req_norm(v))
            if not gates:
                return False
            return any(g is None or not any(_disjoint(g, sp) for sp in scope)
                       for g in gates)
        if not all(_contradicts(v) for v in excluded):
            continue

        new_ir = dict(ir)
        np = dict(p)
        np["allowed"] = list(excluded)
        np.pop("excluded", None)
        new_ir["template"] = "value_in_set"
        new_ir["params"] = np
        try:
            sql = compile_ir(new_ir, output_schema.field_to_sheets,
                             default_sheet=output_schema.primary_sheet)
        except Exception:      # noqa: BLE001 - leave the rule exactly as it was
            continue
        r["ir"] = new_ir
        r["template"] = "value_in_set"
        r["compiled_sql"] = sql
        spec = r.get("rule_spec")
        if isinstance(spec, dict):
            spec["ir"] = new_ir
            spec["compiled_sql"] = sql
        print(f"  [polarity] {r.get('rule_name')!r}: referral excluded the value "
              f"{field!r} is REQUIRED to carry - flipped back to value_in_set")
    return rules


def _cmp_key(d):
    """(field, op, value) of a comparison, normalised for comparison."""
    if not isinstance(d, dict):
        return None
    f, o, v = d.get("field"), d.get("op"), d.get("value")
    if f is None or o is None:
        return None
    return (str(f).strip().lower(), str(o).strip(), str(v).strip().lower())


def _fix_self_negating_conditional(ir):
    """Repair a conditional whose TARGET restates one of its own CONDITIONS.

    A conditional means "when the condition(s) hold, the target must hold", and a
    row is flagged when the conditions hold but the target FAILS. So if the target
    is literally one of the conditions, the rule asks for `C AND NOT C` — it can
    never describe a real violation, and with fuzzy value matching it flags the
    COMPLIANT rows instead of the deviating ones.

    That shape has one cause: the model described the DEVIATION (the zone that
    breaches the clause) where the template wants the REQUIREMENT. Seen on
    "utilise <paper> for all policies except <exemption>; any deviation requires
    referral", emitted as
        conditions [Carrier != <paper>, State != <exempt>] -> Carrier != <paper>
    which says "when the carrier is already wrong, the carrier must be wrong".

    The repair is mechanical and needs no vocabulary: drop the restated condition
    and negate the target, turning the deviation back into the requirement —
        conditions [State != <exempt>] -> Carrier = <paper>
    i.e. "outside the exemption, the carrier MUST BE <paper>", which flags exactly
    the rows the clause calls a deviation.

    Only ever touches a rule that is provably broken by its own structure, so a
    rule that expresses a genuine condition->target pair is left untouched.
    """
    if not isinstance(ir, dict):
        return ir
    if ir.get("template") not in ("conditional_value", "conditional_all"):
        return ir
    p = ir.get("params")
    if not isinstance(p, dict):
        return ir
    tgt = _cmp_key(p)
    if not tgt or tgt[1] not in _NEGATE_OP:
        return ir            # nothing to compare, or an op we can't invert

    single = isinstance(p.get("condition"), dict)
    conds = [p["condition"]] if single else list(p.get("conditions") or [])
    if not any(_cmp_key(c) == tgt for c in conds):
        return ir            # target and conditions differ — a normal rule

    kept = [c for c in conds if _cmp_key(c) != tgt]
    out = dict(ir)
    np = dict(p)
    np["op"] = _NEGATE_OP[tgt[1]]
    if kept:
        # Other drivers survive, so the conditional still has something to gate on.
        if single:
            np.pop("condition", None)
            np["conditions"] = kept
            out["template"] = "conditional_all"
        else:
            np["conditions"] = kept
    # No other driver (the only condition WAS the target): keep the condition so
    # the rule stays a well-formed conditional rather than silently widening into
    # an unconditional check on every row.
    out["params"] = np
    return out


def _referral_to_conditional(ir, output_schema):
    """Rewrite a referral intent into conditional_value(trigger → indicator='Yes'):
    flag rows where the trigger is met but the policy was NOT referred. Leaves the
    IR unchanged when there is no indicator column or the trigger can't be reduced
    to a single condition."""
    if not isinstance(ir, dict):
        return ir
    indicator, referred = _find_referral_indicator(output_schema)
    if not indicator:
        return ir
    # already a conditional on the indicator → nothing to do
    if ir.get("template") == "conditional_value" and (ir.get("params") or {}).get("field") == indicator:
        return ir

    # TWO-COLUMN "except" referral. The model expressed a paper-placement rule as
    # conditional_value(condition = the EXEMPTION → target = the required value)
    # and flagged it is_referral — e.g. "use Specialty paper for all policies
    # EXCEPT home state CA; any deviation requires Referral":
    #     condition {Insured State != CA}, field=Legal Entity, op='=', value='Specialty'.
    # The deviation that needs referral is a row in the EXEMPTED zone that still
    # carries the required value (a CA policy written on Specialty paper), i.e.
    #   target holds  AND  NOT(condition)  →  referral required.
    # Rebuild as conditional_all([target, negated-exemption] → indicator='Yes') so
    # BOTH drivers are checked in ONE rule (never split into =CA / !=CA halves).
    if ir.get("template") == "conditional_value":
        p = ir.get("params") or {}
        cond = p.get("condition")
        tgt_field = p.get("field")
        if (isinstance(cond, dict) and cond.get("field") and tgt_field
                and tgt_field != indicator and cond.get("field") != indicator
                and cond.get("op") in _NEGATE_OP):
            out = {
                "template": "conditional_all",
                "params": {
                    "conditions": [
                        {"field": tgt_field, "op": p.get("op", "="),
                         "value": p.get("value")},
                        {"field": cond["field"],
                         "op": _NEGATE_OP[cond.get("op", "=")],
                         "value": cond.get("value")},
                    ],
                    "field": indicator, "op": "=", "value": referred,
                },
            }
            for k in ("rule_name", "rule_description", "error_message", "severity",
                      "is_referral", "confidence", "reason", "citation", "stage",
                      "polarity"):
                if k in ir:
                    out[k] = ir[k]
            return out

    cond = _trigger_condition_from_ir(ir)
    if not cond or not cond.get("field") or cond.get("field") == indicator:
        return ir
    out = {
        "template": "conditional_value",
        "params": {
            "condition": cond,
            "field": indicator,
            "op": "=",
            "value": referred,   # violation = trigger holds AND indicator != referred
        },
    }
    for k in ("rule_name", "rule_description", "error_message", "severity",
              "is_referral", "confidence", "reason", "citation", "stage", "polarity"):
        if k in ir:
            out[k] = ir[k]
    return out


def _vv_words(s):
    import re as _re
    return _re.findall(r"[a-z0-9]+", str(s).lower())


def _vv_norm(s):
    import re as _re
    return _re.sub(r"[^a-z0-9]", "", str(s).lower())


def variation_traces_to_base(v, base):
    """True when variation `v` can be a genuine surface form of one of the
    authorized values in `base` — i.e. it shares at least one WORD with some
    authorized value, or is a substring/superstring of one (glued spellings).

    A variation that shares NO word with ANY authorized value and has no
    substring relationship to one names a DIFFERENT thing than anything the
    contract authorized. In practice this is a token harvested from the output
    template's COLUMN HEADER rather than from the contract — e.g. "Cayman" from a
    column named "Legal Entity (Specialty vs Cayman Paper)" when the contract's
    authorized companies are "Palms Insurance Company, Limited" / "Palms Specialty
    Insurance Company, Inc." "Cayman" is nowhere in either company name, so as a
    variation it would let a non-authorized value silently pass the check.

    NOTHING is hardcoded: the acceptable words are derived entirely from this
    contract's own authorized values.

    NOTE: kept deliberately permissive (any single shared word suffices) — this
    is called from the ENUM path (normalize_variation_values) where `base` is
    often a whole authorized SET (e.g. a 6-item territory-exclusion list, or a
    dozen approved reinsurers) and a real, legitimate variation can summarise
    the set via one connecting word (e.g. "US Territories and Possessions") or
    share only a common word with the ONE member it is a surface form of
    ("Berkley Re America" / "Berkely RE"). A stricter, word-overlap-ratio test
    was tried and reliably kills a cross-entity contamination like "Palms
    Specialty Insurance Company, Inc." bleeding into a "Demoshield Specialty"
    rule, but it also silently drops ~250 currently-correct variations of this
    permissive kind across the wider rule set (verified against every stored
    variation_values/allowed/excluded pair in validation_rule) — an
    unacceptable regression for a fix scoped to one bug. The conditional_value/
    conditional_all TARGET path (see filter_conditional_variations) has exactly
    ONE authorized value, where that ambiguity cannot arise the same way, and
    carries the stricter check instead — see _conditional_target_traces_to_value."""
    vw = set(_vv_words(v))
    if not vw:
        return False
    nv = _vv_norm(v)
    base_words = set()
    base_norms = []
    for b in base:
        base_words |= set(_vv_words(b))
        nb = _vv_norm(b)
        if nb:
            base_norms.append(nb)
    if vw & base_words:
        return True
    if any(nv and (nv in nb or nb in nv) for nb in base_norms):
        return True
    # An INITIALISM shares no word and no substring with the value it stands for,
    # so the two tests above cannot see it — yet "SSIC" for "SiriusPoint Specialty
    # Insurance Corporation" is exactly how a bordereau writes a carrier. Whether
    # the letters line up is arithmetic, not judgement, so it belongs here rather
    # than in a model. One match only: an initialism that fits two authorized
    # values cannot identify either (see acronym_matches).
    return len(acronym_matches(v, base)) == 1


# Two letters is not an identifier — "SA" is the initials of half the companies in
# any book of business. Three is the shortest initialism worth trusting.
_MIN_ACRONYM_LEN = int(os.getenv("KAVACHIO_MIN_ACRONYM_LEN", "3"))


def acronym_forms(value):
    """The initialisms a person would plausibly build from `value`: the initials of
    every word, plus every LEADING run of those initials at least
    _MIN_ACRONYM_LEN long.

    The prefixes matter because an initialism routinely drops the legal-form tail —
    "SiriusPoint Specialty Insurance Corporation" is written SSIC and also SSI.

    Nothing here knows what a legal form IS. Naming the droppable words
    (corporation/company/ltd/…) would be a baked-in vocabulary, and this platform
    serves MGAs whose entity names, languages and suffixes it has not seen. Taking
    leading initials instead is purely structural: every form returned is derived
    from the contract's own value and nothing else.
    """
    words = [w for w in _vv_words(value) if w]
    if len(words) < 2:
        return set()
    full = "".join(w[0] for w in words)
    return {full[:n] for n in range(_MIN_ACRONYM_LEN, len(full) + 1)}


def acronym_matches(v, base):
    """Which values in `base` the string `v` is exactly the initialism of.

    Returns a list on purpose: MORE THAN ONE match is the dangerous case, not the
    useful one. "SIC" against both "Specialty Insurance Corporation" and "Surety
    Insurance Company" identifies neither, and admitting it would leave the rule
    unable to tell the two apart. Callers must treat len() > 1 as a refusal.
    """
    nv = _vv_norm(v)
    if len(nv) < _MIN_ACRONYM_LEN:
        return []
    return [b for b in base if nv in acronym_forms(b)]


def _conditional_target_traces_to_value(v, val):
    """Stricter trace-to-base test for a conditional_value/conditional_all
    TARGET's variation_values, where there is exactly ONE authorized value —
    unlike variation_traces_to_base (used for the enum templates' whole
    authorized SET), which must stay permissive to tolerate a shared generic/
    connecting word across many entries. With only one target value, a real
    surface variation should share its LEADING word with the value (a company
    name variation preserves its own brand/head word — "Demoshield Insurance"
    or "Demoshield Inc." for "Demoshield Specialty" — even if a different
    trailing/legal-form word follows it), or that leading word should appear
    somewhere else in the value (a short abbreviation like "Specialty" for
    "Palms Specialty Insurance Company, Inc." — the SAME worked example
    normalize_variation_values already documents). A variation whose leading
    word is foreign to the value entirely — e.g. "Palms Specialty Insurance
    Company, Inc." for a rule whose actual value is "Demoshield Specialty
    paper" — names a genuinely different, unrelated company and is dropped,
    even though it shares the generic word "Specialty". Verified against every
    stored conditional_value/conditional_all rule in validation_rule: this
    change affects ONLY the known "Palms Specialty" contamination, nothing
    else. Falls back to the substring/superstring check for glued spellings."""
    words = _vv_words(v)
    if not words:
        return False
    base_words = set(_vv_words(val))
    if words[0] in base_words:
        return True
    nv, nb = _vv_norm(v), _vv_norm(val)
    return bool(nv and nb and (nv in nb or nb in nv))


# ── STRUCTURAL VARIATION SEEDING ───────────────────────────────────────────
# The prompt asks the model for AT LEAST 3 surface spellings of EVERY authorized
# value (prompt_builder, "VARIATION VALUES"), but nothing enforced it: whenever
# the model returned the key empty, returned it for only ONE value of a set, or
# returned it in the {value: [spellings]} MAP shape, the rule shipped with the
# contract's own wording and nothing else — an exact-match-only check that misses
# every bordereau spelling the contract did not use. Measured on the live rule set
# before this fix: 1642 of 3661 enum rules (45%) carried ZERO spellings beyond
# their own values.
#
# So the floor is now enforced deterministically, from the value's OWN words —
# never from a baked-in vocabulary of company/suffix words, because this platform
# serves MGAs whose names, languages and legal forms it has not seen. Every
# candidate has to clear the SAME gates an AI-proposed spelling clears
# (variation_traces_to_base + the over-generic tests), plus two of its own: it must
# be unambiguous across the value set, and it must not be a form the compiled query
# ALREADY matches (a near-duplicate adds nothing but noise — see prompt_builder).
_MIN_VARIATIONS_PER_VALUE = int(os.getenv("KAVACHIO_MIN_VARIATIONS_PER_VALUE", "3"))

# Name-like values only. A value that is a whole clause ("injury, sickness, disease
# … upon exhaustion of its limit of liability") has no surface spellings worth
# deriving — dropping its last word yields another sentence, not a spelling a BDX
# would hold — and would bloat the compiled VALUES list for nothing.
_VARIATION_MAX_WORDS = int(os.getenv("KAVACHIO_VARIATION_MAX_WORDS", "8"))

# Mirrors the compiled query's matcher (rule_compiler._ENUM_MATCH_THRESHOLD), so
# "does the rule already match this?" is answered with the runtime's own metric.
_VARIATION_MATCH_THRESHOLD = float(os.getenv("KAVACHIO_ENUM_MATCH_THRESHOLD", "0.90"))

_VV_JW_CON = None


def _vv_similarity(a, b):
    """Similarity between two normalized values using the SAME function the
    compiled rule uses — DuckDB's jaro_winkler_similarity (an in-process library,
    already a dependency of the validation runtime). difflib disagrees with it
    badly on real carrier names, and this decides whether a derived spelling adds
    anything, so it must not be approximated. Falls back to difflib only when
    DuckDB cannot be loaded at all."""
    global _VV_JW_CON
    try:
        if _VV_JW_CON is None:
            import duckdb
            _VV_JW_CON = duckdb.connect()
        return float(_VV_JW_CON.execute(
            "SELECT jaro_winkler_similarity(?, ?)", [a, b]).fetchone()[0])
    except Exception:
        import difflib as _difflib
        return _difflib.SequenceMatcher(None, a, b).ratio()


def flatten_variation_values(vv):
    """`variation_values` as a flat list of spellings, accepting BOTH shapes the
    model is shown: a flat list, and the {value: [spellings]} map used in the
    `any_of` scope example (rule_compiler._flatten_variations accepts both too).

    This used to be `list(vv) if isinstance(vv, list) else []` — so a map-shaped
    answer, which the prompt's own example invites, was silently discarded and the
    rule kept only the contract's own values."""
    if isinstance(vv, dict):
        out = []
        for lst in vv.values():
            out.extend(lst if isinstance(lst, list) else [lst])
        return out
    if isinstance(vv, list):
        return list(vv)
    return []


# A word this short carries no meaning on its own — it is a connector ("and", "of",
# "or", "&") or a legal-form stub ("Inc", "Ltd", "LLC"). A DERIVED multi-word form
# containing one is a fragment, not a spelling: cutting "Boiler and Machinery
# coverage" down to "Boiler and", or "United States of America" to "of America",
# produces text no bordereau ever holds. The contract's OWN values are never judged
# by this — only forms this module invents. Purely structural: no word list.
_MIN_CONTENT_WORD_LEN = int(os.getenv("KAVACHIO_MIN_CONTENT_WORD_LEN", "4"))

# Shortest initialism this module will INVENT (the model's own proposals keep the
# lower, deliberate floor in acronym_forms). See structural_variation_forms.
_MIN_SEEDED_ACRONYM_LEN = int(os.getenv("KAVACHIO_MIN_SEEDED_ACRONYM_LEN", "4"))


def _cased_words(value):
    """The value's words, ORIGINAL casing kept and surrounding punctuation trimmed.
    Splitting on whitespace (not on every non-alphanumeric) keeps "Stand-alone" and
    "U.S." whole — splitting inside them yields fragments like "alone cyber"."""
    out = []
    for tok in str(value).split():
        w = tok.strip(".,;:()[]{}\"'“”‘’")
        if w:
            out.append(w)
    return out


def _is_fragment(form):
    """True when a DERIVED multi-word form contains a word too short to carry
    meaning — the structural signature of a cut made mid-phrase."""
    words = _cased_words(form)
    return len(words) > 1 and any(len(w) < _MIN_CONTENT_WORD_LEN for w in words)


def _looks_like_proper_name(value):
    """True when every alphabetic word of `value` is capitalised (Title Case or ALL
    CAPS) — the orthography of a NAMED entity ("Volante International Limited", "MS
    TRANSVERSE INSURANCE COMPANY") as opposed to a described thing ("financial
    guaranty business", "Stand-alone cyber").

    This gates the two forms that only make sense for a name: an initialism, and a
    bare distinguishing word. Nobody writes "FGB" for "Financial guaranty business".
    It is an orthographic test, not a vocabulary — it never asks what a word MEANS."""
    words = [w for w in _cased_words(value) if any(c.isalpha() for c in w)]
    if len(words) < 2:
        return False
    return all(w[0].isupper() for w in words)


def structural_variation_forms(value, base):
    """Candidate surface spellings of `value`, derived from ITS OWN text and ordered
    most-specific-first. Nothing here knows what a company, a legal form or an
    insurance word IS — every form is a cut of the contract's own wording:

      1. PUNCTUATION TRIM — drop a trailing parenthetical or comma segment
         ("Palms Insurance Company, Limited" → "Palms Insurance Company"). The
         contract's own punctuation says where the name ends.
      2. SUFFIX-DROP ladder — drop trailing words one at a time ("Palms Specialty
         Insurance Company Inc" → "… Insurance Company" → "… Insurance" → "Palms
         Specialty"). A bordereau shortens a name from the RIGHT, by dropping the
         legal form. There is deliberately NO head-drop ladder: dropping leading
         words removes the word that IDENTIFIES the entity, and on a single-value
         rule (where no word is "shared" and the ambiguity guard has nothing to bite
         on) it would seed a generic "Insurance Company Limited" that any carrier
         would satisfy — a silent hole in an allow-list.
      3. INITIALISM — the initials of the words, plus their leading runs
         (acronym_forms), for "SSIC"-style reporting. NAMES only.
    There is deliberately NO bare-single-word form here, even though a bare
    distinguishing word ("Specialty") is a legitimate variation the prompt asks the
    model for. Structure cannot tell a distinguishing word from an ordinary one: on
    a single-value rule the "shared across 2+ values" guard has nothing to compare
    against, so cutting "Palms Insurance Company, Limited" down to one word yields
    "Insurance" — which every carrier on earth satisfies. That form is left to the
    two paths that can actually ground it: the model (which knows the entity) and
    variation_reconcile (which sees the column's real values).

    Order matters: the caller takes the first N that survive its gates, so the
    fullest, most identifying forms are preferred over the barest ones."""
    words = _cased_words(value)
    if len(words) < 2 or len(words) > _VARIATION_MAX_WORDS:
        return []                                   # nothing to derive / a clause, not a name
    if str(value).count(",") >= 2:
        return []                                   # a comma LIST of things, not one name

    forms = []

    raw = str(value).strip()
    for cut in (raw.split("(")[0], raw.split(",")[0]):   # 1. punctuation trims
        cut = cut.strip().rstrip(",;:-–—")
        if cut and len(_cased_words(cut)) >= 2:
            forms.append(cut)

    # 2. Suffix-drop, ≥2 words — over the HEAD SEGMENT only (the text before the
    #    first comma or bracket). Cutting across the contract's own punctuation
    #    splices unrelated halves together: "Owners, Landlords and Tenants
    #    Liability" would yield "Owners Landlords", and "Special Automobile
    #    Policies (private passenger automobiles…)" would yield "Special Automobile
    #    Policies private". The punctuation marks where the name ends — respect it.
    head = _cased_words(re.split(r"[(,;]", raw)[0])
    for n in range(len(head) - 1, 1, -1):
        forms.append(" ".join(head[:n]))

    named = _looks_like_proper_name(value)
    if named:
        # 3. The FULL initialism only, and only when it is long enough to mean one
        #    thing. A 3-letter initialism this module INVENTS is a coin flip in an
        #    insurance column — "TPL" reads as Third Party Liability, "DOL" as Date
        #    of Loss — and seeding it into an allow-list would let an unrelated cell
        #    pass. Short initialisms are still accepted when the MODEL proposes one
        #    (it is repeating a form it has actually seen) or an admin types it:
        #    that path runs through acronym_matches, which keeps its own floor.
        # A REPEATED word means the text is a concatenation, not a name — "Everest
        # RE Everest Reinsurance Company" is two spellings of one carrier glued
        # together by extraction, and its initials ("ERERC") stand for nothing.
        lowered = [w.lower() for w in words]
        if len(set(lowered)) == len(lowered):
            full = "".join(w[0] for w in words).upper()
            if len(full) >= _MIN_SEEDED_ACRONYM_LEN:
                forms.append(full)

    seen, out = set(), []
    for f in forms:
        k = _vv_norm(f)
        if not k or k == _vv_norm(value) or k in seen or _is_fragment(f):
            continue
        seen.add(k)
        out.append(f)
    return out


def _vv_already_matched(candidate, accepted):
    """True when the compiled query would ALREADY match `candidate` through one of
    the spellings the rule carries — i.e. adding it changes no row. Uses the
    runtime's own threshold and metric, so this is the real question, not a proxy."""
    nc = _vv_norm(candidate)
    return any(_vv_similarity(nc, _vv_norm(a)) >= _VARIATION_MATCH_THRESHOLD
               for a in accepted if _vv_norm(a))


def seed_structural_variations(base, kept, minimum=None):
    """Top `kept` up to `minimum` spellings PER authorized value, using forms
    derived from that value's own words. Returns the new list (order-stable:
    everything already kept stays, in place, and derived forms are appended).

    A value is topped up only when the rule does not already carry enough spellings
    that trace to IT — a set where the model answered well for one carrier and not
    at all for the next (the common failure) is filled in for the second carrier
    only. Every derived form must:
      • survive the same over-generic tests an AI spelling survives
        (variation_is_over_generic: no_trace / ambiguous_stem / legal_form_token /
        short_prefix), so a bare "Palms" or a bare "Limited" can never be seeded;
      • be unambiguous — not equally close to a DIFFERENT authorized value, which
        would leave the rule unable to tell the two apart;
      • add something — a form the query already matches is skipped rather than
        padded in (the prompt's own instruction: never filler).

    Consequence of those gates: a short value ("Claims Made") may still end up with
    fewer than `minimum`. That is correct and deliberate — the alternative is
    inventing a spelling, and a wrong spelling silently disables the check."""
    minimum = _MIN_VARIATIONS_PER_VALUE if minimum is None else minimum
    base = [b for b in (base or []) if str(b).strip()]
    if minimum <= 0 or not base:
        return list(kept)

    out = list(kept)
    for value in base:
        # Spellings this rule already has for THIS value (its own wording aside).
        # Attribution uses the STRICT single-value test, not the permissive
        # set-level one: "Palms Specialty" must not be counted as coverage for
        # "Demoshield Specialty" just because both carry the word "Specialty",
        # which is exactly how a value ends up silently starved.
        own = [v for v in out
               if _vv_norm(v) != _vv_norm(value)
               and variation_traces_to_value(v, value, base)]
        need = minimum - len(own)
        if need <= 0:
            continue
        accepted = [value] + own
        added = []
        for cand in structural_variation_forms(value, base):
            if need <= 0:
                break
            if any(_vv_norm(cand) == _vv_norm(x) for x in out):
                continue                            # already carried
            is_generic, _reason = variation_is_over_generic(cand, base)
            if is_generic:
                continue                            # same bar an AI spelling clears
            other = [b for b in base if _vv_norm(b) != _vv_norm(value)]
            if other and _vv_already_matched(cand, other):
                continue                            # would confuse two authorized values
            if _vv_already_matched(cand, accepted):
                continue                            # the query already matches it
            out.append(cand)
            accepted.append(cand)
            added.append(cand)
            need -= 1
        if added:
            print(f"[VARIATION-SEED] value={value!r} | had {len(own)} of {minimum} "
                  f"→ SEEDED {added}")
    return out


def variation_traces_to_value(v, value, base=None):
    """Does `v` name THIS ONE authorized value? — the per-value question, as
    opposed to variation_traces_to_base's per-SET question ("does it name anything
    the contract authorized?", deliberately permissive and therefore useless for
    deciding WHICH value a spelling covers).

    Two ways to qualify: the strict surface test
    (_conditional_target_traces_to_value — keeps the value's own leading word, or
    is a glued form of it), or being the initialism of exactly this value and no
    other ("VCL" for "Volante Canada Limited"), which shares no word with it at all
    and so is invisible to every surface test."""
    if _conditional_target_traces_to_value(v, value):
        return True
    hits = acronym_matches(v, list(base) if base else [value])
    return len(hits) == 1 and _vv_norm(hits[0]) == _vv_norm(value)


def normalize_variation_values(template, params):
    """Build params['variation_values'] for an enum rule: the rule's authorized
    values (allowed for value_in_set, excluded for value_not_in_set) PLUS any AI
    surface variations, deduped and order-stable, with OVER-GENERIC variations
    dropped.

    A variation is over-generic when ALL of its words are shared across 2+
    authorized values (an ambiguous common stem like "Palms" / "Palms Insurance"
    when both companies start with "Palms"), when it is a bare short prefix of an
    authorized value, or when it is a bare single word that sits in the legal-form
    (trailing) position of a company name — e.g. bare "Limited"/"Inc" from
    "Palms Insurance Company, Limited" — since a corporate-form suffix on its own
    identifies no specific entity. A variation is also dropped when it does NOT
    trace back to ANY authorized value (shares no word and no substring with any of
    them) — such a value is a foreign token, typically harvested from the output
    template's COLUMN HEADER (e.g. "Cayman" from "Legal Entity (Specialty vs Cayman
    Paper)"), and names no authorized entity. The distinguishing words ("Specialty")
    and any multi-word short-form ("Palms Limited") are kept. NOTHING is hardcoded:
    the trailing tokens and the acceptable words are derived from this contract's
    own authorized values. Such fragments identify no single entity and would let a
    wrong value pass. The authorized values themselves are always kept. Mutates and
    returns `params`."""
    import re as _re
    from collections import Counter as _Counter

    base = list(params.get("allowed") or []) if template == "value_in_set" \
        else list(params.get("excluded") or [])
    variations = flatten_variation_values(params.get("variation_values"))

    def _w(s):
        return _re.findall(r"[a-z0-9]+", str(s).lower())

    def _n(s):
        return _re.sub(r"[^a-z0-9]", "", str(s).lower())

    base_norm = {_n(b) for b in base}
    base_tok = [_n(b) for b in base]
    wc = _Counter()
    for b in base:
        for wd in set(_w(b)):
            wc[wd] += 1
    common = {w for w, n in wc.items() if n >= 2}   # words shared by 2+ authorized values
    # Trailing token of each MULTI-word authorized value = its legal-form/suffix
    # position (e.g. "Limited"/"Inc" in "Palms Insurance Company, Limited").
    # Self-derived from the data — no hardcoded suffix list.
    tail_tokens = {bw[-1] for b in base if len(bw := _w(b)) >= 2}

    def _is_generic(v):
        nv = _n(v)
        if not nv or nv in base_norm:
            return False                            # authorized values are never dropped
        vw = _w(v)
        if not variation_traces_to_base(v, base):
            return True                             # foreign token (e.g. column-header word) -> not this entity
        if vw and all(w in common for w in vw):
            return True                             # only shared words -> ambiguous stem
        if len(vw) == 1 and vw[0] in tail_tokens:
            return True                             # bare legal-form/trailing token of a name
        if len(nv) <= 5 and any(bt.startswith(nv) and bt != nv for bt in base_tok):
            return True                             # bare short prefix of an authorized value
        return False

    seen, merged, dropped = set(), [], []
    for v in base + variations:                     # base first so it's always kept
        k = str(v).strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        if _is_generic(v):
            dropped.append(v)
        else:
            merged.append(v)
    # The model's answer is a FLOOR, not the last word: whatever it left out (or
    # left out for only some of the values) is derived from the values themselves,
    # so an enum rule is never born matching only the contract's own wording.
    seeded = seed_structural_variations(base, merged)
    params["variation_values"] = seeded

    # SCENARIO 1 LOG — variations from the AI at contract + output-template upload.
    print(f"[VARIATION-GEN] field={params.get('field')!r} | AI proposed "
          f"{len(variations)} variation(s) → KEPT {len(merged)}"
          + (f" → SEEDED to {len(seeded)}" if len(seeded) != len(merged) else "")
          + f": {seeded}"
          + (f" | DROPPED as too-generic: {dropped}" if dropped else ""))
    return params


def variation_is_over_generic(v, base):
    """(is_generic, reason_code) — would `v` be dropped by the over-generic filter
    inside normalize_variation_values, and WHICH of its tests fired?

    normalize_variation_values._is_generic answers only yes/no, and it does so
    inside a closure over values computed for a whole rule at build time. This is
    the same four tests, standalone and self-explaining, for the ONE case where a
    person needs to be told why their spelling was refused (the tenant_admin
    "add a spelling" popup — see contract_upload_services/variation_admit.py).

    Reason codes: 'no_trace' | 'ambiguous_stem' | 'legal_form_token' |
    'short_prefix'; (False, None) when the spelling is acceptable — including
    when it IS one of the authorized values, which are never dropped.

    Deliberately a SEPARATE implementation rather than a refactor of
    _is_generic: that function sits in the hot generation path, and the
    regressions its docstring documents (2084-2099) were expensive enough that
    the small duplication is the cheaper risk. Any change to the tests below
    must be made in both places.
    """
    import re as _re
    from collections import Counter as _Counter

    base = list(base or [])

    def _w(s):
        return _re.findall(r"[a-z0-9]+", str(s).lower())

    def _n(s):
        return _re.sub(r"[^a-z0-9]", "", str(s).lower())

    nv = _n(v)
    base_norm = {_n(b) for b in base}
    if not nv or nv in base_norm:
        return False, None                  # authorized values are never dropped

    vw = _w(v)
    if not variation_traces_to_base(v, base):
        return True, "no_trace"             # names nothing this contract authorized

    wc = _Counter()
    for b in base:
        for wd in set(_w(b)):
            wc[wd] += 1
    common = {w for w, n in wc.items() if n >= 2}
    if vw and all(w in common for w in vw):
        return True, "ambiguous_stem"       # only words shared by 2+ values -> identifies none

    tail_tokens = {bw[-1] for b in base if len(bw := _w(b)) >= 2}
    if len(vw) == 1 and vw[0] in tail_tokens:
        return True, "legal_form_token"     # bare "Limited"/"Inc" — every company has one

    base_tok = [_n(b) for b in base]
    if len(nv) <= 5 and any(bt.startswith(nv) and bt != nv for bt in base_tok):
        return True, "short_prefix"         # bare truncation of a longer authorized value

    return False, None


def _apply_known_vocabulary_spellings(ir):
    """Add any surface spellings the SHARED vocabulary already knows for this
    rule's own values to its `variation_values`. Mutates `ir`.

    The loop this closes: a tenant_admin corrects one rule on one contract; the
    spelling goes into the global vocabulary; every rule generated afterwards that
    names the same value is born already understanding it. Nobody has to make the
    same correction twice, and the matching gets better the more the product is
    used.

    A vocabulary entry is a CANDIDATE, never an override — each one still has to
    pass the same trace-to-base and over-generic tests the AI's own variations
    passed, against THIS rule's values. So a spelling learned for one carrier can
    never widen a different carrier's rule just because both rows sit in the same
    table.

    Silent no-op when the vocabulary is unreachable: generation must not fail
    because a nice-to-have lookup did.
    """
    try:
        from contract_upload_services import vocabulary
    except Exception:
        return

    params = ir.get("params") or {}
    template = ir.get("template")
    field = params.get("field")

    if template == "value_in_set":
        base = list(params.get("allowed") or [])
    elif template == "value_not_in_set":
        base = list(params.get("excluded") or [])
    else:
        base = [params["value"]] if params.get("value") is not None else []
    if not base:
        return

    existing = params.get("variation_values")
    if isinstance(existing, dict):
        return              # keyed conditional map — leave its shape alone
    existing = list(existing) if isinstance(existing, list) else []
    have = {_vv_norm(v) for v in [*base, *existing]}

    added = []
    for value in base:
        try:
            known = vocabulary.synonyms_for_value(value, field)
        except Exception:
            return
        for spelling in known:
            key = _vv_norm(spelling)
            if not key or key in have:
                continue
            # No trace/over-generic re-test here, deliberately. Those tests exist
            # to catch an AI INVENTING a variation at generation time. A vocabulary
            # entry is not invented: it is recorded against THIS value by name
            # (synonyms_for_value looks it up BY the value), after a tenant_admin
            # proposed it and the model confirmed it. Re-running a text test over
            # it would throw away exactly the cases a text test cannot judge —
            # initialisms like "PICL" for "Palms Insurance Company, Limited" —
            # and the admin's decision would silently vanish on the next upload.
            # The canonical binding IS the safety: a variation recorded for one
            # carrier can only ever be offered to a rule naming that same carrier.
            have.add(key)
            existing.append(spelling)
            added.append(spelling)

    if added:
        params["variation_values"] = existing
        ir["params"] = params
        print(f"[VARIATION-VOCAB] field={field!r} | reused {len(added)} "
              f"previously-learned spelling(s): {added}")


def filter_conditional_variations(params):
    """Hygiene for a conditional_value/conditional_all rule's target
    `variation_values` (surface spellings of the enforced value the compiler folds
    into the target's IN/NOT-IN set). Drop any variation that does NOT trace back
    to the target `value` — a foreign token (e.g. a word harvested from the output
    column header, "Cayman" from "Legal Entity (Specialty vs Cayman Paper)", OR a
    different real company that happens to share one generic descriptor word with
    this contract's own value, e.g. "Palms Specialty Insurance Company, Inc." for a
    target of "Demoshield Specialty paper") would otherwise widen the accept-set
    and let a genuinely non-compliant cell equal it, hiding the violation.

    Uses _conditional_target_traces_to_value rather than the enum path's
    variation_traces_to_base: a conditional target has exactly ONE authorized
    value (never a whole set), so a real surface variation must share ITS
    leading/brand word — a bare stem like "Specialty" for "Palms Specialty
    Insurance Company, Inc." still passes (that leading word IS a word of the
    value), but a foreign company name that only brushes one trailing/generic
    word against the value does not. Mutates and returns `params`."""
    vv = params.get("variation_values")
    if not isinstance(vv, list) or not vv:
        return params
    val = params.get("value")
    if val is None or str(val).strip() == "":
        return params                               # no target value to trace to
    kept, dropped = [], []
    for v in vv:
        (kept if _conditional_target_traces_to_value(v, val) else dropped).append(v)
    params["variation_values"] = kept
    if dropped:
        print(f"[COND-VARIATION] field={params.get('field')!r} target={val!r} | "
              f"DROPPED foreign (no trace to target value): {dropped} | KEPT {kept}")
    return params


def _gm_norm(s):
    """Normalize a value for group-name matching: lowercase, alphanumeric only —
    same shape the compiled SQL uses to compare values."""
    import re as _re
    return _re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def build_reference_group_members(reference_documents):
    """Derive a generic GROUP → [member, …] map from any reference document's
    TABLES, so a value-set rule whose values are category/GROUP names can be
    expanded to also carry every specific member listed under that group.

    Fully data-driven (NO hardcoded vocabulary): for each table row we take the
    first cell as the group and the cell with the most comma-separated items as
    its member list. A row only contributes when that member cell actually holds a
    LIST (has a comma) — this naturally selects category→members tables (e.g.
    Occupancy Group → Occupancy Description) and skips header rows and
    single-value tables (e.g. an approved-reinsurer "Company → Paper" table).

    `reference_documents` is the list passed to extraction: [{"name","text"}],
    where docx `text` is a JSON dump of [{"text","tables"}]. Returns
    {normalized_group: (display_group, [members])}. Empty when nothing parses.
    """
    gm = {}
    if not reference_documents:
        return gm

    def _collect_tables(obj, out):
        if isinstance(obj, dict):
            t = obj.get("tables")
            if isinstance(t, list):
                out.extend(t)
            for v in obj.values():
                _collect_tables(v, out)
        elif isinstance(obj, list):
            for v in obj:
                _collect_tables(v, out)

    for rd in reference_documents:
        tables = []
        # DOCX/DOC references now carry their structured tables directly in
        # `data` (their `text` is clean [TABLE] markdown, not a JSON dump), so
        # parse that when present — no JSON round-trip. Everything else keeps the
        # original behaviour: json.loads the `text` (older DOCX refs stored as a
        # JSON dump still parse), and a non-JSON string (a PDF's markdown context)
        # is treated as having no structured tables. PDF handling is unchanged.
        data = (rd or {}).get("data")
        if data is not None:
            _collect_tables(data, tables)
        else:
            text = (rd or {}).get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                parsed = json.loads(text)
            except Exception:
                continue  # plain-text reference (e.g. PDF) — no structured tables
            _collect_tables(parsed, tables)
        for table in tables:
            if not isinstance(table, list):
                continue
            for row in table:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                cells = [str(c).strip() for c in row]
                group = cells[0]
                if not group:
                    continue
                # the member cell = the non-first cell holding the longest LIST
                member_cell = max(cells[1:], key=lambda c: c.count(","))
                if member_cell.count(",") < 1:
                    continue  # not a list → not a category→members row
                members = [m.strip() for m in member_cell.split(",") if m.strip()]
                # Skip a comma cell that is really a single formatted NUMBER
                # ("10,000,000" → ['10','000','000']) — a member list must contain
                # at least one alphabetic token.
                if not any(any(ch.isalpha() for ch in m) for m in members):
                    continue
                if members:
                    gm[_gm_norm(group)] = (group, members)
    return gm


def _expand_grouped_values(ir, group_members):
    """For a value_in_set / value_not_in_set rule whose values include a GROUP
    name present in `group_members`, append that group's specific members to the
    value list (keeping the group itself). Order-stable, deduped. Mutates and
    returns `ir`. No-op when there's no map or no template match."""
    if not group_members:
        return ir
    tmpl = ir.get("template")
    key = "allowed" if tmpl == "value_in_set" else (
        "excluded" if tmpl == "value_not_in_set" else None)
    if not key:
        return ir
    params = ir.get("params") or {}
    vals = params.get(key)
    if not isinstance(vals, list) or not vals:
        return ir

    out, seen = [], set()
    def _add(v):
        n = _gm_norm(v)
        if n and n not in seen:
            seen.add(n)
            out.append(v)

    expanded_any = False
    for v in vals:
        _add(v)
        hit = group_members.get(_gm_norm(v))
        if hit:
            for m in hit[1]:
                before = len(out)
                _add(m)
                if len(out) > before:
                    expanded_any = True

    if expanded_any:
        params[key] = out
        ir["params"] = params
    return ir


def verify_and_build_ir_rule(ir, clause, contract_ctx, output_schema, con=None,
                             group_members=None):
    """Run the verify gate on one IR and return either a validation_rule dict
    or {"route": "review"|"control", "reason": ...}.

    Gate order (fail-fast): vocab-normalize → validate_ir (incl. field-existence)
    → compile_ir → guard_sql + dry_run (+ light smoke) → build rule.
    """
    from contract_upload_services.rule_ir import validate_ir, TEMPLATE_CATALOG
    from contract_upload_services.rule_compiler import compile_ir, CompileError
    from contract_upload_services.vocabulary import (
        normalize_ir_literals, VOCAB_VERSION,
    )
    from contract_upload_services.rule_ir import CATALOG_VERSION

    if not isinstance(ir, dict):
        return {"route": "review", "reason": "IR is not an object"}

    # The model returns template:null when nothing fits — route, never force.
    if ir.get("template") in (None, "", "null"):
        return {"route": "review",
                "reason": ir.get("reason") or "no template selected by extractor"}

    # 0) normalize param key aliases the model sometimes emits (e.g. `row_scope`
    # instead of `scope`) — otherwise the compiler silently ignores them and a
    # SCOPED rule becomes an unscoped one that flags every row.
    ir = _normalize_param_aliases(ir)

    # 1) vocabulary-normalize literals (USA→US, etc.) before any check.
    ir = normalize_ir_literals(ir)

    # 1b) snap field names to the EXACT output-template names (tolerate
    # case/spacing/punctuation from the extractor) so a rule isn't lost over
    # 'occurrence limit' vs 'Occurrence Limit'.
    from contract_upload_services.rule_ir import remap_ir_fields
    ir = remap_ir_fields(ir, output_schema.resolve_field)

    # 1b-referral) A referral intent (is_referral) means "this trigger requires a
    # referral to the Company". If the output template has a REFERRAL-INDICATOR
    # column, the check is: trigger met AND the policy was NOT referred. Rewrite
    # the model's (often plain) trigger rule into a conditional_value on that
    # indicator — deterministic, so it does not depend on the LLM choosing the
    # conditional itself.
    if ir.get("is_referral"):
        ir = _referral_to_conditional(ir, output_schema)

    # 1b-selfneg) Repair a conditional whose target restates one of its own
    # conditions — the model gave the deviation where the requirement belongs, so
    # the rule can only ever flag compliant rows. Structural, so it leaves every
    # well-formed rule alone.
    ir = _fix_self_negating_conditional(ir)

    # 1b-scope) The clause named a row-filter (a per-entity / per-subset scope) but
    # the mapper produced a rule WITHOUT one — it dropped the filter, so the rule
    # would apply over-broadly to EVERY row (e.g. a per-reinsurer "limit ≤ $Z"
    # emitted as a bare cap because there is no reinsurer column). Route to review
    # rather than ship the over-broad rule. (A referral was already rewritten to a
    # conditional above, so this only catches genuinely dropped scopes.)
    if ir.get("_scope_dropped") and not (ir.get("params") or {}).get("scope") \
            and ir.get("template") != "conditional_value":
        # MULTI-SHEET RESCUE: on a multi-schedule BDX the schedules are SHEETS,
        # not row values — a clause scoped to named schedules ("between Schedule
        # G, H, I and J") needs no scope COLUMN. When the dropped scope's
        # schedule keys match this template's sheet names, convert it to a SHEET
        # scope (params.scope_sheets): compile_ir then restricts the rule to
        # exactly those sheets, and an aggregate template unions their rows into
        # one TRUE cross-schedule total. Only genuinely row-level scopes (per-
        # reinsurer, per-class …) still route to review below.
        from contract_upload_services.rule_compiler import _sched_key, _scope_toks
        scope_txt = str(ir["_scope_dropped"])
        pieces = [p.strip() for p in
                  re.split(r"\s*(?:,|/|&|\band\b|\bor\b|\bbetween\b|\bacross\b)\s*",
                           scope_txt, flags=re.I) if p and p.strip()]
        named = {k for k in (_sched_key(_scope_toks(p)) for p in pieces) if k}
        sheet_keys = {k for k in
                      (_sched_key(_scope_toks(s))
                       for s in (output_schema.fields_by_sheet or {}))
                      if k}
        if named and named & sheet_keys:
            ir.setdefault("params", {})["scope_sheets"] = pieces
            print(f"[verify] scope '{scope_txt[:60]}…' matched schedule sheet(s) "
                  f"{sorted(named & sheet_keys)} — compiled as a sheet scope.")
        else:
            return {"route": "review",
                    "reason": f"clause applies only to specific rows "
                              f"({ir['_scope_dropped']}) but no column identifies them "
                              f"— rule not generated to avoid an over-broad match"}

    # 1c) rescale a percentage threshold (e.g. "100%" emitted as 100) to a
    # fraction when the bound column's SAMPLES prove it stores 0–1 values —
    # otherwise the compiled rule is a silent no-op. High-confidence and
    # non-destructive (fires only when every sample is in (0,1]), so always on.
    ir, _scale_note = _autocorrect_numeric_scale(ir, output_schema)

    # 2) validate_ir — template known, params well-typed, field-existence.
    #    A rule that references a column NOT present in the Output Template cannot
    #    be validated against the BDX, so we do NOT generate it — it routes to
    #    review for a human to map/add the column. (Recording it as a
    #    non-executable "unmapped" rule is intentionally DISABLED.) `unmapped`
    #    stays False, so the unmapped branch below is never taken.
    ok, reason = validate_ir(ir, output_schema.field_names)
    unmapped = False
    unmapped_reason = None
    if not ok:
        # A pure COLUMN gap — the rule is well-formed but references a field the
        # Output Template doesn't have (yet) — is RECORDED as a non-executable
        # 'unmapped' rule (rule_status='needs_review') so the UI surfaces it as a
        # pending rule to map, and the runtime tells the user which column to add,
        # instead of dropping the rule entirely. Other failures (no template,
        # malformed params) still route to review.
        if reason and "not in output template" in reason:
            unmapped = True
            unmapped_reason = reason
        else:
            return {"route": "review", "reason": reason}

    sql = None
    if unmapped:
        # The column isn't in the template, but we still GENERATE runnable SQL —
        # targeting the primary data sheet — so the rule executes at validation
        # time IF the BDX actually has that column (the runtime checks first).
        # Skip the template dry-run: the column isn't in the smoke schema.
        try:
            sql = compile_ir(ir, output_schema.field_to_sheets,
                             default_sheet=output_schema.primary_sheet)
        except CompileError:
            sql = None  # truly uncompilable (no sheet at all) — recorded only
    else:
        # 3) Data-grounding checks (OFF by default — mapping is COLUMN-based, not
        # data-based). When KAVACHIO_DATA_GROUNDING=1, also reject a numeric/date
        # rule bound to a clearly-text column, or a percentage written as 100 on a
        # 0-1 fraction column. By default we trust the mapping and skip these.
        if _DATA_GROUNDING:
            type_reason = _check_field_types(ir, output_schema)
            if type_reason:
                return {"route": "review", "reason": type_reason}
            scale_reason = _check_numeric_scale(ir, output_schema)
            if scale_reason:
                return {"route": "review", "reason": scale_reason}

        # 3b) ALWAYS-ON kind guard: never SILENTLY ship a numeric-threshold rule
        # bound to a clearly-text column (a wrong-column binding the LLM mapper can
        # still make). Route it to review instead. Numeric templates only — date
        # templates are skipped (BDX dates are Excel serials). This is the
        # deterministic backstop behind the prompt's value-kind matching.
        kind_reason = _numeric_field_on_text(ir, output_schema)
        if kind_reason:
            return {"route": "review", "reason": kind_reason}

        # ALWAYS-ON geographic-level guard: never keep a value_in_set that lists a
        # COUNTRY as an allowed value in a STATE column (dead rule). Drop the
        # inclusion; the paired exclusion (PR / US territories) still applies.
        geo_reason = _country_in_state_enum(ir, output_schema)
        if geo_reason:
            return {"route": "review", "reason": geo_reason}

        # DATA-GROUNDING enum guard (OFF by default — gated like the type/scale
        # guards above). An in_set/equals rule whose values don't intersect a
        # column's SAMPLE value set MIGHT be a wrong-column bind (e.g. a class value
        # on a Primary/Excess column) — but it is just as likely a legitimate rule
        # whose concrete contract/reference values simply aren't among the 2-3
        # stored samples (the binary-sample heuristic cannot tell the two apart, and
        # it false-rejected e.g. authorized classes of business sourced from the
        # Facultative Guidelines because the column's samples were coverage-types).
        # Mapping binds columns by NAME/meaning (Call 3), so we TRUST the contract/
        # reference values and STILL create the rule — its job is to flag rows that
        # deviate from the contract. Set KAVACHIO_DATA_GROUNDING=1 to re-enable this
        # sample-based reject.
        if _DATA_GROUNDING:
            enum_reason = _enum_set_mismatch(ir, output_schema)
            if enum_reason:
                return {"route": "review", "reason": enum_reason}

        # ALWAYS-ON numeric-template guard: a fractional numeric equality compiled
        # as a fuzzy string value-set (e.g. Commission Rate = 0.235) flags every
        # row — route it to review to be re-issued as a numeric rule.
        numset_reason = _stringset_on_numeric(ir, output_schema)
        if numset_reason:
            return {"route": "review", "reason": numset_reason}

        # 3a-bis) GROUP → MEMBERS expansion (deterministic, data-driven). When the
        #     rule's value set carries category/GROUP names that a reference
        #     document defines as a group of specific members (e.g. an "Occupancy
        #     Group" whose "Occupancy Description" lists the individual occupancies),
        #     expand the value set to include EVERY listed member alongside the
        #     group. This runs BEFORE variation-value seeding + compile so the
        #     members reach both the VALUES list and the SQL. The map is derived
        #     from the reference doc's own tables — nothing hardcoded — so a BDX row
        #     reporting a specific occupancy (e.g. "Hospitals") matches its group's
        #     authorized/excluded set, not just the group label.
        _expand_grouped_values(ir, group_members)

        # 3a-ter) Expand a "home state excluding US territories/possessions"
        #     exclusion to the FULL canonical territory list + abbreviations, so a
        #     row reporting any territory (not just the one named) is caught.
        _expand_us_territory_exclusion(ir)

        # 3b) Seed variation_values for enum rules so the compiled VALUES list is
        #     never empty even if the model omitted the key, and always carries the
        #     contract's OWN values. variation_values = the rule's specified values
        #     (allowed for in_set, excluded for not_in_set) + any AI surface
        #     variations, deduped and order-stable. We deliberately base it on the
        #     SPECIFIED values only (never mix allowed/excluded) so a value the
        #     contract forbids can never slip into the "passes" set.
        _enum_tmpl = ir.get("template")
        if _enum_tmpl in ("value_in_set", "value_not_in_set"):
            ir["params"] = normalize_variation_values(_enum_tmpl, ir.get("params") or {})
            # 3b-a) …and where neither the model's big generation call nor the
            #     structural seeder could reach the per-value floor, ask the model
            #     the ONE narrow question it answers well ("how else is this
            #     written?"). Only fires for the values still short, never widens a
            #     rule past the same gates every other spelling clears, and fails
            #     open — see variation_topup.
            try:
                from contract_upload_services.variation_topup import (
                    topup_variation_values)
                ir["params"] = topup_variation_values(_enum_tmpl, ir["params"])
            except Exception as _exc:
                print(f"[VARIATION-TOPUP] skipped: {_exc}")
        # 3b-bis) The conditional templates fold their target's variation_values
        #     into an IN/NOT-IN accept-set too (rule_compiler._cmp_bool). Run the
        #     same trace-to-base hygiene so a foreign token can't silently widen
        #     the target and hide a violation. Kept out of the compiler because
        #     rule_compiler must not import rule_normalizer (circular).
        elif _enum_tmpl in ("conditional_value", "conditional_all"):
            ir["params"] = filter_conditional_variations(ir.get("params") or {})

        # 3b-ter) Fold in spellings the SHARED VOCABULARY has already learned for
        #     these same values — the ones tenant_admins corrected by hand on
        #     earlier contracts (app_routes.rule_add_variation_value →
        #     vocabulary.add_admin_synonym). This is what stops the same correction
        #     being needed on every new contract: a spelling taught once is known
        #     to every rule generated afterwards.
        #
        #     Ordering is load-bearing. It runs AFTER the filters above (which
        #     would drop short forms) and BEFORE compile_ir, and every candidate
        #     still has to pass the SAME trace/over-generic tests the AI's own
        #     variations passed — a vocabulary entry widens nothing on its own.
        if _enum_tmpl in ("value_in_set", "value_not_in_set",
                          "conditional_value", "conditional_all"):
            _apply_known_vocabulary_spellings(ir)

        # 3c) Re-seed US-territory names + abbreviations AFTER the generic filter,
        #     which drops short codes (PR/GU/AS/MP/UM/USVI/CNMI) as "too generic".
        #     For a territory column those codes ARE the BDX's real spelling.
        if _enum_tmpl == "value_not_in_set":
            _seed_territory_abbreviations(ir)

        # 4) compile_ir — deterministic IR → DuckDB SELECT. Pass the field→ALL-sheets
        #    map so a rule fans out (UNION) to every sheet that has its column(s).
        try:
            sql = compile_ir(ir, output_schema.field_to_sheets)
        except CompileError as exc:
            return {"route": "review", "reason": f"compile failed: {exc}"}

    # 5) guard + dry-run (+ light smoke) against the sample schema, when available.
    #    Skipped for unmapped rules (their column isn't in the template schema).
    if not unmapped and sql is not None and con is not None:
        try:
            from duckdb_validation import guard_sql, dry_run
            good, cleaned = guard_sql(sql)
            if not good:
                return {"route": "review", "reason": f"guard rejected: {cleaned}"}
            ran, err = dry_run(con, cleaned)
            if not ran:
                return {"route": "review", "reason": f"dry-run failed: {err}"}

            # False-positive guard: the template's sample rows are *compliant*
            # example data, so a correct row-level rule should flag NONE (or few)
            # of them. A rule that flags EVERY sample row almost always has the
            # wrong operator (a max limit compiled as "!= X") or enum values that
            # don't match the data's vocabulary ("General Contractor" vs
            # "construction"). Route those to review instead of shipping a rule
            # that fires on every policy.
            _FP_TEMPLATES = {
                "value_in_set", "value_not_in_set", "max_limit", "min_limit",
                "range_check", "period_duration", "date_relation", "date_bound",
            }
            if _DATA_GROUNDING and ir.get("template") in _FP_TEMPLATES:
                try:
                    from contract_upload_services.rule_ir import field_refs as _fr
                    refs = _fr(ir)
                    # the rule fans out across all sheets carrying the field, so
                    # the sample-row denominator must span those same sheets.
                    sheets = output_schema.field_to_sheets.get(refs[0]) if refs else None
                    if sheets:
                        total = sum(
                            con.execute(f'SELECT COUNT(*) FROM "{s}"').fetchone()[0]
                            for s in sheets)
                        if total >= 2:
                            flagged = con.execute(
                                f"SELECT COUNT(*) FROM ({cleaned}) AS _q"
                            ).fetchone()[0]
                            if flagged >= total:
                                return {"route": "review",
                                        "reason": f"flags all {total} sample rows — "
                                                  f"likely wrong operator or values "
                                                  f"(false-positive rule)"}
                except Exception:
                    pass  # counting is best-effort; never block a rule on it
        except Exception as exc:
            return {"route": "review", "reason": f"verification error: {exc}"}

    # Weak-rule floor (opt-in via KAVACHIO_REJECT_CONFIDENCE): a mapped rule below
    # the floor needs a human. Skipped for unmapped 'recorded' rules.
    if not unmapped and _REJECT_CONF_FLOOR > 0:
        conf = float(ir.get("confidence") or 0)
        if conf < _REJECT_CONF_FLOOR:
            return {"route": "review",
                    "reason": f"generation confidence {conf:.2f} below floor "
                              f"{_REJECT_CONF_FLOOR:.2f}"}

    template = ir["template"]
    rule_type, rule_class = _resolve_rule_class_for_template(template)
    spec = TEMPLATE_CATALOG.get(template) or {}

    # Field(s) the rule binds to, for canonical_target.
    from contract_upload_services.rule_ir import field_refs
    bound_fields = field_refs(ir)

    # Referral rules (Root C): a referral trigger is a real, checkable rule, but
    # its consequence is "refer to Company", not a hard violation — surface that
    # in severity + error_message + rule_spec so the UI shows a referral.
    is_referral = bool(ir.get("is_referral"))
    severity = ir.get("severity") or rule_class.get("default_severity")
    if is_referral and not severity:
        severity = "warning"
    error_message = ir.get("error_message")
    if is_referral and error_message and "referral" not in error_message.lower():
        error_message = f"Referral to Company required: {error_message}"

    # Justification artifact (Root D) — deterministic, no LLM. Ties the rule back
    # to the contract language and mapping it came from, for the review UI / audit.
    justification = {
        "contract_text": (clause.get("text") or "")[:500],
        "interpreted_requirement": ir.get("rule_description") or ir.get("rule_name"),
        "mapped_field": bound_fields[0] if bound_fields else None,
        "operator": template,
        "confidence": ir.get("confidence"),
        "is_referral": is_referral,
    }

    return {
        "tenant_id":             contract_ctx.get("tenant_id"),
        "contract_id":           contract_ctx.get("contract_id"),
        "program_id":            contract_ctx.get("program_id"),
        "rule_engine":           spec.get("engine") or "ir",
        "rule_class":            rule_class.get("name") or rule_type,
        "rule_class_display":    rule_class.get("display_name") or rule_type,
        "rule_name":             ir.get("rule_name"),
        "rule_description":      ir.get("rule_description"),
        "validation_stage":      ir.get("stage") or rule_class.get("default_stage"),
        "severity":              severity,
        "canonical_target":      {"output_field": bound_fields[0] if bound_fields else None,
                                  "output_fields": bound_fields,
                                  "unmapped": unmapped,
                                  "is_referral": is_referral},
        # rule_spec carries the IR + compiled SQL; the runtime short-circuits on
        # kind=='ir_v1' (no LLM). compiled_sql is also surfaced top-level.
        # An 'unmapped' rule is recorded but NOT executable (no column to query)
        # — compiled_sql is None and the runtime flags it until the column exists.
        # 'referral'/'justification' are extra keys the runtime ignores (Roots C/D).
        "rule_spec":             {"kind": "ir_v1", "ir": ir, "compiled_sql": sql,
                                  "executable": not unmapped,
                                  "unmapped_reason": unmapped_reason,
                                  "referral": is_referral,
                                  "justification": justification},
        "compiled_sql":          sql,
        "is_executable":         not unmapped,
        "is_referral":           is_referral,
        "ir":                    ir,
        "template":              template,
        "vocab_version":         VOCAB_VERSION,
        "catalog_version":       CATALOG_VERSION,
        "error_message":         error_message,
        "source_clause_id":      clause.get("clause_id"),
        "source_verbatim_text":  clause.get("text"),
        "source_page_number":    clause.get("page_number") or clause.get("page"),
        "generation_confidence": ir.get("confidence"),
        # IR rules that pass the verify gate are trusted and go live immediately:
        # rule_status='active'. (The canonical CHECK constraint allows
        # active|needs_review|superseded|disabled|failed_compilation; 'active'
        # is the default so rules are not parked in a review state.)
        "rule_status":           "needs_review" if unmapped else "active",
        "created_by":            "ai_generator_ir_v1",
    }


def normalize_ir_outputs(synth_outputs, contract_ctx, output_schema,
                         group_members=None):
    """Walk synthesize_rules_ir output and route every clause to exactly one of:
        validation_rules  — verified, persistable rule rows (rule_status='proposed')
        review_queue      — rule-bearing but unmappable / failed verification
        control_register  — non-rule-bearing clauses (obligations / governance)

    Nothing is dropped silently.
    """
    validation_rules, review_queue, control_register = [], [], []

    total_candidates = sum(len(e.get("candidates") or []) for e in synth_outputs)
    field_names_dbg = sorted(getattr(output_schema, "field_names", set()))
    print(f"\n[Pipeline 2.5-IR] verifying {total_candidates} extracted candidate(s) "
          f"against {len(field_names_dbg)} output-template field(s): {field_names_dbg}")

    con, _tables = _smoke_connection(output_schema)
    try:
        for entry in synth_outputs:
            clause = entry["clause"]
            engine = entry.get("engine")
            candidates = entry.get("candidates") or []
            cid = clause.get("clause_id")

            # Non-rule-bearing → control register. Persist the model's actual
            # per-clause reasoning (why it isn't a BDX rule) instead of a fixed
            # boilerplate string, so "explain why no rule" is answered per clause.
            if engine is None:
                clf = entry.get("classification") or {}
                control_register.append({
                    "clause_id": cid,
                    "clause_text": clause.get("text"),
                    "source_page": clause.get("page_number") or clause.get("page"),
                    "reason": clf.get("reasoning")
                              or "non-rule-bearing clause (obligation / governance)",
                })
                continue

            # Rule-bearing but the extractor produced nothing → review.
            if not candidates:
                print(f"  [review] clause {cid}: extractor returned no rule")
                review_queue.append({
                    "clause_id": cid,
                    "clause_text": clause.get("text"),
                    "reason": "extractor returned no rule for a rule-bearing clause",
                })
                continue

            for ir in candidates:
                result = verify_and_build_ir_rule(
                    ir, clause, contract_ctx, output_schema, con=con,
                    group_members=group_members,
                )
                if isinstance(result, dict) and result.get("route"):
                    target = (review_queue if result["route"] == "review"
                              else control_register)
                    print(f"  [{result['route']}] clause {cid} "
                          f"(template={ir.get('template')!r}): {result.get('reason')}")
                    target.append({
                        "clause_id": cid,
                        "clause_text": clause.get("text"),
                        "reason": result.get("reason"),
                        "ir": ir,
                    })
                    continue
                print(f"  [ok]     clause {cid}: {result.get('template')} "
                      f"→ {result.get('rule_name')}")
                validation_rules.append(result)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass

    # Referral polarity vs the contract's own requirements. A "deviation requires
    # referral" clause names the REQUIRED value, so the generic referral flip
    # (trigger values -> flag the rows that match) inverts it. Only correctable
    # once every rule is built, because the evidence is a sibling rule requiring
    # that same value on that same column.
    validation_rules = _unflip_required_value_referrals(validation_rules, output_schema)

    # One clause, one requirement: drop the carve-out restated as a prohibition
    # and the duplicate emitted in referral form. Runs after the polarity fix so
    # both copies are already the right way round before they are compared.
    validation_rules = _consolidate_clause_duplicates(validation_rules)
    # Bidirectional routing backstop: synthesize the carve-out prohibition
    # (state S must NOT be the mandated value) for "use E except S" clauses on a
    # field with >=2 authorised values — the rule the prompt tells the model not
    # to emit. Runs after consolidation.
    validation_rules = _synthesize_carveout_prohibition(validation_rules, output_schema)

    # Cross-field referral-trigger dedup: a single referral sentence chunked
    # into its OWN clause by extraction can get bound to a different (wrong)
    # column than the one the same trigger is already bound to elsewhere —
    # two live rules for one real-world trigger. Unlike the pass above, this
    # compares ACROSS clause_id/field boundaries (see docstring).
    validation_rules = _consolidate_cross_field_referral_duplicates(validation_rules)

    # Rule-level dedup. Clause-text dedup (Pipeline 1) only catches identical
    # clause bodies; two DIFFERENTLY-worded clauses can still compile to the SAME
    # rule, and the own-share guard can re-point two limits onto the same
    # column+value. Collapse exact duplicates on the compiled SQL (deterministic),
    # falling back to the IR (template+params) for not-yet-executable rules.
    seen_sig, deduped, dropped_dupes = set(), [], 0
    for r in validation_rules:
        sql = r.get("compiled_sql")
        if sql:
            sig = ("sql", re.sub(r"\s+", " ", sql).strip().lower())
        else:
            _ir = r.get("ir") or {}
            sig = ("ir", json.dumps(
                {"t": _ir.get("template"), "p": _ir.get("params")},
                sort_keys=True, default=str))
        if sig in seen_sig:
            dropped_dupes += 1
            continue
        seen_sig.add(sig)
        deduped.append(r)
    validation_rules = deduped
    if dropped_dupes:
        print(f"[Pipeline 2.5-IR] dropped {dropped_dupes} duplicate rule(s) "
              f"(identical compiled SQL / IR)")

    # One identity, one rule. The dedup above only catches BYTE-identical rules;
    # an arithmetic identity solved for a different column is a different rule
    # that flags exactly the same rows (see the helper's docstring).
    validation_rules = _consolidate_equivalent_formula_rules(validation_rules)

    # Territory carve-out: keep only the exclusion when a state column carries both
    # an inclusion and an exclusion from the same clause (see helper).
    validation_rules = _drop_paired_state_inclusions(validation_rules)

    print(f"[Pipeline 2.5-IR] verified={len(validation_rules)} "
          f"review={len(review_queue)} control={len(control_register)}")
    return validation_rules, review_queue, control_register


# =========================================================
# Normalization output persistence
# =========================================================

def save_normalization_output(
    validation_rules,
    dropped,
    output_dir,
    contract_id="contract",
    file_base=None
):
    """
    Save Pipeline 2.5 normalization output to a JSON file.

    File written (inside `output_dir`):
      <file_base>_normalization.json

    Payload:
      {
        "stage": "2.5 — Normalization",
        "contract_id": ...,
        "generated_at": ISO timestamp,
        "summary": {total, active, needs_review, ajv, custom, dropped},
        "validation_rules": [...],
        "dropped_candidates": [...]
      }

    Returns: the written file path.
    """

    os.makedirs(output_dir, exist_ok=True)

    if not file_base:
        file_base = contract_id or "contract"

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    summary = {
        "total":        len(validation_rules),
        "active":       sum(1 for r in validation_rules if r.get("rule_status") == "active"),
        "needs_review": sum(1 for r in validation_rules if r.get("rule_status") == "needs_review"),
        "ajv":          sum(1 for r in validation_rules if r.get("rule_engine") == "ajv"),
        "custom":       sum(1 for r in validation_rules if r.get("rule_engine") == "custom"),
        "dropped":      len(dropped)
    }

    payload = {
        "stage":              "2.5 — Normalization",
        "contract_id":        contract_id,
        "generated_at":       now,
        "summary":            summary,
        "validation_rules":   validation_rules,
        "dropped_candidates": dropped
    }

    path = os.path.join(output_dir, f"{file_base}_normalization.json")

    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    print(
        f"[Pipeline 2.5] saved normalization output → {path} "
        f"(active={summary['active']}, needs_review={summary['needs_review']}, "
        f"dropped={summary['dropped']})"
    )

    return path
