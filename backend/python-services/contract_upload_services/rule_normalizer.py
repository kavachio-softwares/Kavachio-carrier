"""
rule_normalizer.py
──────────────────
Deterministic Pipeline 2 — Step 3 (Normalization).

IR pipeline: each IR candidate is verified (validate → vocab-normalize →
field-existence → compile → guard + dry-run → smoke) and routed to exactly one
destination (validation_rule / review_queue / control_register) — see
normalize_ir_outputs below.

Also exports the legacy helpers used by validation_rule_generator:
parse_llm_json, deduplicate_rules, merge_reference_lists,
normalize_lookup_lists.
"""

import os
import re
import json
import hashlib
import datetime

from contract_upload_services.constants import RULE_CLASS_LIBRARY


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
    """Tolerant JSON parser — strips fences and trims whitespace.

    strict=False on purpose: the model routinely emits a literal newline or tab
    inside a string value (clause text copied verbatim from the contract), which
    stdlib json rejects as an invalid control character. Failing here throws away a
    COMPLETE extraction and forces a whole re-run in page chunks, so we accept the
    raw control characters — they are harmless in the parsed value.
    """

    if isinstance(text, (dict, list)):
        return text

    if not text:
        return {}

    s = text.strip()
    s = re.sub(r"```json\s*", "", s)
    s = re.sub(r"```", "", s)
    s = s.strip()

    try:
        return json.loads(s, strict=False)
    except Exception:
        # json-repair handles a missing brace / trailing comma; a truncated answer
        # still raises, which is what the caller's chunked fallback is for.
        import json_repair
        recovered = json_repair.loads(s)
        if not recovered:
            raise
        return recovered


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


# USPS two-letter codes for the 50 US states + DC — canonical name → code.
# Universal public postal reference data, the same category as _US_TERRITORIES /
# _COUNTRY_ALIASES above (see uszips_reference.py's docstring). Used to seed a
# state's abbreviation alongside its full name (and the full name when the
# contract wrote the code): a contract says "Alaska" but the BDX reports "AK",
# and the generic variation filter drops a 2-letter code as an untraceable
# token ("ak" is not a word of, nor a substring of, "alaska") — so, exactly
# like the territory codes, these are re-seeded AFTER that filter.
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


def _seed_state_abbreviations(ir):
    """Seed the USPS code for every US state named in an enum rule's value set
    (and the full name when the contract wrote the code), so a BDX reporting
    "AK" still matches a rule whose contract says "Alaska". Mirrors
    _seed_territory_abbreviations: MUST run AFTER normalize_variation_values,
    whose trace-back filter drops 2-letter codes as foreign tokens. Applies to
    BOTH enum templates — for value_not_in_set the code widens the CATCH set;
    for value_in_set it widens the PASS set (an allowed state's own code must
    not be flagged as a deviation). Only exact state names/codes seed anything,
    so a non-geographic value on a state column adds nothing. Mutates ir."""
    if ir.get("template") not in ("value_in_set", "value_not_in_set"):
        return ir
    params = ir.get("params") or {}
    field = params.get("field")
    if not isinstance(field, str) or "state" not in field.lower():
        return ir
    base = params.get("allowed") if ir.get("template") == "value_in_set" \
        else params.get("excluded")
    vv = list(params.get("variation_values") or [])
    seen = {str(x).strip().lower() for x in vv}

    def _add(form):
        if form.strip().lower() not in seen:
            seen.add(form.strip().lower())
            vv.append(form)

    for v in base or []:
        if not isinstance(v, str):
            continue
        k = v.strip().lower()
        if k in _STATE_NAME_BY_LOWER:           # full name → seed its code
            _add(_US_STATE_ABBR[_STATE_NAME_BY_LOWER[k]])
        elif k in _STATE_CODE_TO_NAME:          # code → seed the full name
            _add(_STATE_CODE_TO_NAME[k])
    params["variation_values"] = vv
    ir["params"] = params
    return ir


def _repair_tiny_state_allowlist(ir, clause):
    """Deterministic repair for a "home state within <country> … including DC …
    excluding <territories>" clause that collapsed to a near-empty value_in_set
    instead of the intended territory EXCLUSION. When the unmappable country
    literal ("United States of America") is stripped (see
    _country_in_state_enum), the mapper sometimes keeps only a leftover
    fragment (e.g. a bare "District of Columbia") as if THAT were the entire
    requirement — an allow-list this small on a state-domain column would flag
    almost every real US state, the opposite of the clause's intent.

    Detected structurally, not by a contract-specific value list: value_in_set
    on a "state" field whose `allowed` set names FEWER than half of the real
    US states (using the existing, contract-agnostic _US_STATE_ABBR reference
    — the same table _seed_state_abbreviations already uses), AND the clause's
    own raw text names a territory-exclusion pattern (the same _TERRITORY_
    TRIGGER regex _expand_us_territory_exclusion already keys on). A
    deliberately narrow, legitimate state list (e.g. a 3-state regional
    program) never trips this: its clause text has no territory/possession
    language at all, so the guard is a no-op for it regardless of how few
    states it names. Rebuilds the rule as the canonical _US_TERRITORIES
    exclusion — the same shape a correctly-extracted run already produces.
    Mutates and returns ir; leaves it unchanged when there is no textual basis
    to repair it (the existing _country_in_state_enum guard still routes an
    unrepaired, country-literal-bearing case to review)."""
    if ir.get("template") != "value_in_set":
        return ir
    params = ir.get("params") or {}
    field = params.get("field")
    if not isinstance(field, str) or "state" not in field.lower():
        return ir
    allowed = [v for v in (params.get("allowed") or []) if isinstance(v, str)]
    if not allowed:
        return ir
    real_state_forms = {n.lower() for n in _US_STATE_ABBR} | \
        {c.lower() for c in _US_STATE_ABBR.values()}
    matched = sum(1 for v in allowed if v.strip().lower() in real_state_forms)
    if matched >= len(_US_STATE_ABBR) / 2:
        return ir                                   # already a broad, real list
    text = str(clause.get("text") or "")
    if not _TERRITORY_TRIGGER.search(text):
        return ir                                   # no textual basis to repair
    params["excluded"] = list(_US_TERRITORIES.keys())
    params.pop("allowed", None)
    ir["template"] = "value_not_in_set"
    ir["params"] = params
    print(f"[TERRITORY-REPAIR] {ir.get('rule_name')!r}: allow-list {allowed!r} "
          f"on state-domain field {field!r} named only {matched} real US "
          f"state(s) while the clause's own text names a territory exclusion "
          f"— rebuilt as the canonical US-territory exclusion.")
    return ir


_REFERRAL_TRIGGER_RE = re.compile(
    r"requir\w*\s+referral|referral\s+to\s+(?:the\s+)?company|"
    r"subject\s+to\s+referral|prior\s+approval", re.I)


def _force_referral_on_carveout_requirement(ir, clause):
    """Deterministic classification backstop for the "utilize <value> for all
    policies EXCEPT state S; any deviation requires Referral to the Company"
    pattern. Clause classification is a single non-deterministic LLM pass —
    when it misses this referral trigger, the rule ships as a hard block
    (no referral, whatever severity the mapper guessed) for the general
    (non-carved-out) direction instead of the contract's actual consequence
    (a referral, per this codebase's own convention: severity 'warning').

    Scoped NARROWLY to only the general "except state S" requirement
    direction, via the SAME _carveout_state test _synthesize_carveout_
    prohibition already uses to identify that direction — so this can never
    touch the complementary carve-out/prohibition direction (state S itself),
    which is a separate requirement the clause does not say is a referral.
    No-op if is_referral is already set, or if the clause's own text has no
    referral-trigger language. Mutates and returns ir."""
    if ir.get("is_referral"):
        return ir
    params = ir.get("params") or {}
    state_col, S = _carveout_state(params)
    if not state_col or not S:
        return ir
    text = str(clause.get("text") or "")
    if not _REFERRAL_TRIGGER_RE.search(text):
        return ir
    ir["is_referral"] = True
    ir["severity"] = "warning"
    print(f"[REFERRAL-FIX] {ir.get('rule_name')!r}: clause text requires "
          f"Referral to the Company on deviation, but classification missed "
          f"is_referral — forced is_referral=True, severity='warning'.")
    return ir


# Wording that marks the FLAGGED event as a DEPARTURE from a stated baseline
# value ("any deviation from the required X paper", "paper other than X").
# Used by the polarity guard below.
_REQUIRED_DEVIATION_RE = re.compile(
    r"deviat|other\s+than|differs?\s+from|instead\s+of", re.I)


def _fix_inverted_required_value(ir):
    """Deterministic polarity guard for the REQUIRED-value clause pattern
    ("Administrator to utilize X paper for all policies …; any deviation
    requires referral"). The model sometimes emits this as value_not_in_set
    with excluded=[X] — which flags every row MATCHING the required value (the
    compliant ones) and passes every actual deviation. The contradiction is
    detectable from the rule's own artifacts: its description/error text says
    the flagged event is a DEVIATION FROM a value that sits in the excluded
    set. When both signals are present, flip the rule to value_in_set with
    allowed=[those values] — the flagged event becomes NOT matching, which is
    what the rule's own description says it reports. variation_values carry
    over unchanged (they are spellings of the same values). Mutates ir."""
    if ir.get("template") != "value_not_in_set":
        return ir
    params = ir.get("params") or {}
    excluded = [v for v in (params.get("excluded") or []) if isinstance(v, str)]
    if not excluded:
        return ir
    text = " ".join(str(ir.get(k) or "") for k in
                    ("rule_description", "error_message", "rule_name", "reason"))
    if not _REQUIRED_DEVIATION_RE.search(text):
        return ir
    tnorm = re.sub(r"[^a-z0-9]", "", text.lower())
    named = [v for v in excluded
             if re.sub(r"[^a-z0-9]", "", v.lower()) in tnorm]
    if not named:
        return ir
    params["allowed"] = params.pop("excluded")
    ir["params"] = params
    ir["template"] = "value_in_set"
    print(f"[POLARITY-FIX] {ir.get('rule_name')!r}: rule text describes the "
          f"flagged event as a DEVIATION FROM {named!r} but the template "
          f"flagged rows MATCHING them — flipped value_not_in_set(excluded) "
          f"to value_in_set(allowed).")
    return ir


def _expand_state_scope_values(ir):
    """Widen a state-column scope's scalar (in)equality value to BOTH standard
    spellings — full name AND USPS code ("California" → ["California", "CA"]) —
    so the scope holds regardless of which form the BDX reports. Without this a
    scope compiled against one spelling silently stops scoping when the data
    uses the other (e.g. `state != 'california'` never excludes rows that spell
    it 'CA'). The compiler renders a list value as IN / NOT IN, preserving the
    operator's semantics. Only exact state names/codes are touched; any other
    scope value passes through unchanged. Mutates ir."""
    params = ir.get("params") or {}

    def _both_forms(v):
        if not isinstance(v, str):
            return None
        k = v.strip().lower()
        if k in _STATE_NAME_BY_LOWER:
            name = _STATE_NAME_BY_LOWER[k]
            return [name, _US_STATE_ABBR[name]]
        if k in _STATE_CODE_TO_NAME:
            name = _STATE_CODE_TO_NAME[k]
            return [name, _US_STATE_ABBR[name]]
        return None

    scope = params.get("scope")
    if isinstance(scope, dict):
        for fld, cond in scope.items():
            if fld == "any_of" or "state" not in str(fld).lower():
                continue
            if isinstance(cond, dict) \
                    and str(cond.get("op", "=")).strip().lower() in ("=", "==", "!=", "<>") \
                    and "date" not in cond:
                both = _both_forms(cond.get("value"))
                if both:
                    cond["value"] = both
            elif isinstance(cond, str):
                both = _both_forms(cond)
                if both:
                    scope[fld] = both

    # The conditional templates carry their trigger under `condition` (one dict)
    # or `conditions` (a list of dicts) rather than `scope` — widen those the
    # same way ("Insured State = CA" must also hold for a BDX that spells out
    # "California"). _cmp_bool renders a list value as IN / NOT IN.
    conds = []
    if isinstance(params.get("condition"), dict):
        conds.append(params["condition"])
    if isinstance(params.get("conditions"), list):
        conds.extend(c for c in params["conditions"] if isinstance(c, dict))
    for cond in conds:
        fld = cond.get("field")
        if not isinstance(fld, str) or "state" not in fld.lower():
            continue
        if str(cond.get("op", "=")).strip().lower() in ("=", "==", "!=", "<>"):
            both = _both_forms(cond.get("value"))
            if both:
                cond["value"] = both
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


def _is_container_geography_exclusion(ir):
    """True when a scope-dropped value_not_in_set is a "home state within
    <country> excluding <sub-regions>" carve-out: the mapper attached a COUNTRY
    scope ("only when country is United States …") that the template can't bind
    (no country column), leaving a bare state-column exclusion whose excluded set
    is US territories/possessions. The country scope is a redundant CONTAINER —
    a row in an excluded territory is non-compliant regardless of the (absent)
    country cell — so the exclusion holds for every row and the scope can be
    dropped safely. Narrow by design (state column + territory-triggered excluded
    values) so genuine ROW-level scopes (per-reinsurer, per-class, per-entity)
    are never relaxed. Reuses the existing _TERRITORY_TRIGGER reference pattern —
    no contract/carrier/state literals."""
    if ir.get("template") != "value_not_in_set":
        return False
    params = ir.get("params") or {}
    field = params.get("field")
    if not isinstance(field, str) or "state" not in field.lower():
        return False
    excluded = params.get("excluded") or []
    return any(isinstance(v, str) and _TERRITORY_TRIGGER.search(v) for v in excluded)


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


def _sing_toks(s) -> set:
    """Lower-cased word tokens with a crude plural fold ('managers'→'manager'),
    so a referring phrase matches its concept across singular/plural."""
    return {t[:-1] if t.endswith("s") and len(t) > 3 else t
            for t in re.findall(r"[a-z0-9]+", str(s or "").lower())}


def _prune_reference_enum_values(ir):
    """ALWAYS-ON guard: an enum 'value' that merely RESTATES the concept is a
    cross-reference, not a literal cell value — no data cell will ever hold it.

    Two shapes, both seen in production:
      • the referring phrase — allowed ["the Program Managers"] on an
        'Authorized Program Manager' rule: article-led, and every remaining
        word is drawn from the rule/field concept itself;
      • the field name restated — allowed ["New/Renewal"] on the "New/Renewal"
        column, compiling to "New/Renewal must be New/Renewal" (flags all rows).

    Such entries are pruned from allowed/excluded AND variation_values. If
    nothing literal remains, returns a reason string (route to review — the
    contract defines the members elsewhere; the model must resolve them);
    returns None when the rule is fine. Deliberately narrow: a real value that
    happens to start with an article ("The Hartford") survives, because its
    remaining words are NOT the rule's own concept words."""
    template = ir.get("template")
    if template not in ("value_in_set", "value_not_in_set"):
        return None
    params = ir.get("params") or {}
    key = "allowed" if template == "value_in_set" else "excluded"
    vals = params.get(key)
    if not isinstance(vals, list) or not vals:
        return None
    field = params.get("field") or ""
    concept = _sing_toks(ir.get("rule_name")) | _sing_toks(field)

    def is_reference(v) -> bool:
        toks = re.findall(r"[a-z0-9]+", str(v or "").lower())
        if not toks:
            return False
        if _sing_toks(v) == _sing_toks(field):          # field name restated
            return True
        if toks[0] in ("the", "all", "any") and len(toks) > 1:
            rest = {t[:-1] if t.endswith("s") and len(t) > 3 else t for t in toks[1:]}
            return rest <= concept                       # "the <concept>s"
        return False

    refs = [v for v in vals if is_reference(v)]
    if not refs:
        return None
    keep = [v for v in vals if v not in refs]
    if not keep:
        return (f"{key} values {refs!r} only reference the rule's own concept "
                f"(a defined term), not literal cell values — the contract "
                f"names the actual members elsewhere; resolve them")
    params[key] = keep
    vv = params.get("variation_values")
    if isinstance(vv, list):
        params["variation_values"] = [v for v in vv if not is_reference(v)]
    ir["params"] = params
    return None


def _stringset_on_numeric(ir, output_schema):
    """Always-on deterministic guard for the broken-numeric-query bug: a
    value_in_set / value_not_in_set / value_equals (STRING set-match) rule
    whose literal value is a FRACTIONAL number and whose bound column is numeric
    is a wrong-TEMPLATE choice — a numeric equality/limit (e.g. "Commission rate =
    23.5%" → 0.235) got compiled as a string set-match against '0.235', which
    flags every row that writes the same number any other way. Route to review so it is re-issued as a numeric rule.
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


def _flip_value_in_set_referral(ir):
    """A referral TRIGGER that lists the values which REQUIRE referral (e.g.
    "any policy written in Alaska/Hawaii", "any Sabal Specialty paper policy",
    "Nightclubs") is a POSITIVE-membership signal: the listed values are the ones
    to surface. Emitted as `value_in_set` (an AUTHORIZED / allow-list) it means the
    exact opposite — "the column must be one of these" — so it flags the COMPLEMENT
    (every row that ISN'T a trigger value) and lets the real trigger rows pass.

    When such a referral can't be bound to a referral-indicator column (none in the
    template, or the trigger has >1 value so it won't reduce to a single condition),
    flip it to `value_not_in_set` so it flags the rows that MATCH the trigger — the
    referral zone. Only `value_in_set` is flipped; max/min/period_duration already
    flag their deviation zone, and `value_not_in_set` is already correct polarity."""
    if not isinstance(ir, dict) or ir.get("template") != "value_in_set":
        return ir
    p = dict(ir.get("params") or {})
    allowed = p.get("allowed")
    if not isinstance(allowed, list) or not allowed:
        return ir
    p["excluded"] = allowed
    p.pop("allowed", None)
    out = dict(ir)
    out["template"] = "value_not_in_set"
    out["params"] = p
    return out


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


def _expand_state_values(values):
    """[S] -> [S plus its USPS-code/full-name counterpart], de-duplicated. Uses
    the universal 50-state+DC table applied symmetrically — no per-state casing."""
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
    S' requirement — reads the 'state != S' predicate from scope / condition /
    conditions, whichever spelling the template uses. (None, None) if there is no
    state carve-out."""
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
    """Bidirectional routing backstop (the deterministic half of the paper fix).

    A clause "use E for all policies EXCEPT state S; any deviation requires
    referral" on a field that has >=2 authorised values — the contract's OWN
    enumeration, e.g. its "Authorized Writing Companies" list — is a ROUTING
    rule: S is sent to a DIFFERENT authorised value, so S must NOT be E. The
    extractor emits only the requirement (A) and, because the prompt treats
    "except <state>" as a pure exemption, usually omits the complementary
    prohibition (B). Synthesize B deterministically so the carve-out side does
    not depend on the model emitting it.

    Generic — no hardcoding: the routing test and the value E both come from the
    contract's own rules; only the universal state name<->code table is used
    (symmetrically, like the uszips reference). Idempotent: a clause+field that
    already carries a valid complementary B is left alone."""
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
                             default_sheet=output_schema.primary_sheet,
                             aliases=getattr(output_schema, "field_aliases", None))
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
        if params.get("left_complement") or params.get("right_complement"):
            # `base * (1 - rate)` is not linear in the coefficient map below (the
            # rate appears both inside and outside a constant term), so this
            # function cannot canonicalise it. Never merge it — a key it cannot
            # model is a key it could collide with something it can.
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
                             default_sheet=output_schema.primary_sheet,
                             aliases=getattr(output_schema, "field_aliases", None))
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


def _sample_numbers(output_schema, field):
    """A field's sample values as floats, in order, with non-numeric entries kept
    as None so several fields' samples stay ROW-ALIGNED. Understands the money
    spellings a bordereau uses: thousands separators, a currency symbol, a percent
    sign, and accounting negatives '(1,234.00)' → -1234.0."""
    return _to_numbers(output_schema.samples_for_grounding(field))


def _sheet_sample_numbers(output_schema, sheet, field):
    """The same, for a column read on ONE NAMED SHEET.

    Samples are row-aligned WITHIN a sheet — position i is the same row of that
    sheet in every one of its columns. Across sheets they are not aligned at all
    (different tables, different row counts, different orders), so any check that
    reads several columns together has to read them all from the same sheet."""
    for store in ("row_samples", "samples_all", "samples"):
        vals = ((getattr(output_schema, store, None) or {}).get(sheet) or {}).get(field)
        if vals:
            return _to_numbers(vals)
    return []


def _to_numbers(values):
    out = []
    for v in (values or []):
        s = str(v).strip()
        neg = s.startswith("(") and s.endswith(")")
        if neg:
            s = s[1:-1]
        s = re.sub(r"[,$£€%\s]", "", s)
        try:
            n = float(s)
        except (TypeError, ValueError):
            out.append(None)
            continue
        out.append(-n if neg else n)
    return out


def _reproduces(target, base, factor, tol=0.01):
    """True when `base × factor` reproduces `target` on every row-aligned sample
    pair. Needs at least 2 usable pairs — one pair is too easily a coincidence (and
    a column of zeros would match anything). Tolerance mirrors the compiler's own
    default for an '=' cross-field comparison, scaled for large magnitudes so
    float noise on a million-dollar figure isn't read as a mismatch."""
    pairs = [(t, b) for t, b in zip(target, base)
             if t is not None and b is not None]
    if len(pairs) < 2:
        return False
    if all(b == 0 for _, b in pairs):
        return False
    return all(abs(t - b * factor) <= max(tol, abs(b * factor) * 1e-6)
               for t, b in pairs)


# How far a rescale of a multiplicative constant may reach, as a power of ten.
# A percent/fraction slip is ÷100; a per-mille or a stray decimal point are the
# neighbouring exponents. Bounded so the search can only ever move the DECIMAL
# POINT, never invent an unrelated constant.
_FACTOR_SCALE_EXPONENTS = (2, -2, 1, -1, 3, -3, 4, -4)


def _fmt_factor(v):
    """Render a rescaled constant the way the rest of the pipeline writes one —
    shortest exact decimal, no exponent for the magnitudes a rate lives at."""
    return f"{v:.12g}"


def _restate_factor(text, old, new):
    """Rewrite the constant inside a rule's human-readable sentence after the
    factor was rescaled, so what the reviewer reads matches what the SQL checks.

    Only the LAST number token that equals the old factor is replaced: the
    constant always sits at the END of these sentences ("… equals <base> * 5.0"),
    while an earlier match is part of a COLUMN NAME ("Palms (5%) equals …") and
    must survive untouched."""
    if not isinstance(text, str) or not text:
        return text
    spans = [m.span() for m in re.finditer(r"\d+(?:\.\d+)?", text)
             if _as_float(m.group(0)) == old]
    if not spans:
        return text
    s, e = spans[-1]
    return text[:s] + _fmt_factor(new) + text[e:]


def _as_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _ground_multiplicative_factor(ir, output_schema):
    """Repair a DECIMAL-SCALE slip in a "field = other_field × factor" identity.

    The constant in such a rule arrives from several places — a contract's
    headline percent, a formula note carried on the output template, an inferred
    formula — and every one of those routes can lose the percent-to-fraction step
    on the way, so "5%" lands as 5 instead of 0.05. The rule then demands 100× the
    right number and flags the entire book.

    The sample rows settle it: when the stated factor does NOT reproduce the
    target column's row-aligned samples but the SAME base column does at
    factor / 10**k, the constant was written at the wrong scale and is rescaled.

    Deliberately limited to moving the DECIMAL POINT. A mismatch of any other
    shape is left exactly as written — that is a rule the data disagrees with,
    which is precisely the defect the rule exists to report, and silently refitting
    the constant to the data would erase it. Purely arithmetic: no column-name
    vocabulary, nothing specific to a program or an MGA.
    """
    if not isinstance(ir, dict) or ir.get("template") != "cross_field_compare":
        return ir
    p = ir.get("params") or {}
    if p.get("op") != "=" or p.get("operator") != "*":
        return ir
    field, other = p.get("field"), p.get("other_field")
    factor = _as_float(p.get("factor"))
    if factor in (None, 0) or not (isinstance(field, str) and isinstance(other, str)):
        return ir

    target = _sample_numbers(output_schema, field)
    base = _sample_numbers(output_schema, other)
    if len([t for t in target if t is not None]) < 2:
        return ir                       # nothing to ground the constant on
    if _reproduces(target, base, factor):
        return ir                       # the constant already checks out

    for k in _FACTOR_SCALE_EXPONENTS:
        cand = factor / (10.0 ** k)
        if not _reproduces(target, base, cand):
            continue
        out = dict(ir)
        out["params"] = {**p, "factor": cand}
        for key in ("rule_name", "rule_description", "error_message"):
            if out.get(key):
                out[key] = _restate_factor(out[key], factor, cand)
        print(f"[verify] {field!r} = {other!r} × {factor} does not hold on the "
              f"sample rows; × {_fmt_factor(cand)} does — rescaled the constant "
              f"(a rate written at the wrong decimal scale).")
        return out
    return ir


def _ground_multiplicative_base(ir, output_schema):
    """Point a "field = other_field × factor" identity at the base column the DATA
    actually supports.

    A share/fee identity ("Palms (5%) = <the ceded amount> × 0.05") is only correct
    if `other_field` really is the base the share was struck on. The mapper picks
    that column from names and meanings, and a bordereau often carries several
    plausible candidates — a gross amount, a deduction, and the net of the two.
    Their SAMPLES settle it: the base is whichever column reproduces the target's
    own sample values when multiplied by the factor.

    Acts only when the chosen `other_field` is PROVABLY wrong on the samples and a
    candidate column on the same sheet reproduces them — so a correct rule is
    never disturbed. When SEVERAL candidates reproduce them the arithmetic has
    already accepted them all (a bordereau repeats one amount under more than one
    heading), so any of them is an equally correct check and the one whose NAME is
    closest to the column the mapper reached for is used — see _closest_named.
    Purely arithmetic: no column-name vocabulary, nothing about this program.
    """
    if not isinstance(ir, dict) or ir.get("template") != "cross_field_compare":
        return ir
    p = ir.get("params") or {}
    if p.get("op") != "=" or p.get("operator") != "*":
        return ir
    field, other = p.get("field"), p.get("other_field")
    try:
        factor = float(p.get("factor"))
    except (TypeError, ValueError):
        return ir
    if not (isinstance(field, str) and isinstance(other, str)) or factor == 0:
        return ir

    target = _sample_numbers(output_schema, field)
    if len([t for t in target if t is not None]) < 2:
        return ir                       # nothing to ground the choice on
    if _reproduces(target, _sample_numbers(output_schema, other), factor):
        return ir                       # the mapper's base already checks out

    sheets = set(output_schema.field_to_sheets.get(field) or [])
    matches = [c for c in output_schema.field_names
               if c not in (field, other)
               and sheets & set(output_schema.field_to_sheets.get(c) or [])
               and _reproduces(target, _sample_numbers(output_schema, c), factor)]
    if not matches:
        return ir                       # nothing the data supports — leave it alone

    pick = _closest_named(other, matches)
    out = dict(ir)
    out["params"] = {**p, "other_field": pick}
    _restate_field(out, other, pick)
    print(f"[verify] {field!r} = {other!r} × {factor} does not hold on the sample "
          f"rows; {pick!r} does — repointed the base column."
          + (f" ({len(matches)} columns fit; {pick!r} is the one whose name is "
             f"closest to {other!r})" if len(matches) > 1 else ""))
    return out


def _name_tokens(name):
    """A column name's word tokens, camelCase-aware — the same split every
    column-role test in the codebase shares."""
    from contract_upload_services.uszips_reference import column_tokens
    return set(column_tokens(name or ""))


def _closest_named(incumbent, candidates):
    """Of several columns the DATA cannot tell apart, the one whose NAME is
    closest to the column originally chosen.

    Reached only after arithmetic has already accepted every candidate: they
    reproduce the identity on the same rows, so each is an equally correct check
    and there is no data left to choose with. What remains is that a bordereau
    reports one quantity under several headings — a collected figure, the same
    figure inclusive of a charge, the same figure less that charge — and the
    heading the mapper picked names the quantity it MEANT. The nearest heading to
    it is therefore the one it was reaching for, and the ones that share none of
    its words are a different report of the same number.

    Deterministic: most shared word tokens, then fewest unshared ones, then the
    name itself — no vocabulary and nothing about any programme."""
    if not candidates:
        return None
    t = _name_tokens(incumbent)
    return sorted(candidates,
                  key=lambda c: (-len(t & _name_tokens(c)),
                                 len(t ^ _name_tokens(c)), c))[0]


def _restate_field(ir, old, new):
    """Rewrite a column name inside a rule's human-readable sentences after the
    rule was repointed at a different column, so what the reviewer reads matches
    what the SQL checks. Left alone when the old name does not appear exactly
    once (the sentence then does not have one unambiguous slot to rewrite, and
    the params — which the SQL is compiled from — are already correct)."""
    if not (isinstance(old, str) and isinstance(new, str)) or old == new:
        return
    for key in ("rule_name", "rule_description", "error_message"):
        text = ir.get(key)
        if isinstance(text, str) and text.count(old) == 1:
            ir[key] = text.replace(old, new)


# Machine-written clause text — the deriver's own restatement of a rule it
# generated, not a contract's words. Only these are rewritten when a rule is
# repointed at another column; a real clause is quoted verbatim and is never
# touched (see _restate_derived_clause_text).
_DERIVED_CLAUSE_PREFIXES = ("[Derived formula]", "[Derived rule]", "[Generic rule]")


def _restate_derived_clause_text(clause, before, after):
    """A copy of `clause` whose MACHINE-WRITTEN text names the columns the rule
    ended up bound to, when a grounding step above repointed one.

    "Where this comes from" quotes this text, so a derived rule that was
    repointed would otherwise keep telling the reviewer it checks the column the
    data disagreed with. Contract clauses are quoted verbatim and never
    rewritten. Never mutates the caller's dict."""
    if not isinstance(clause, dict):
        return clause
    text = clause.get("text")
    if not isinstance(text, str) or not text.startswith(_DERIVED_CLAUSE_PREFIXES):
        return clause
    out = text
    for key, old in (before or {}).items():
        new = (after or {}).get(key)
        if (isinstance(old, str) and isinstance(new, str) and old != new
                and out.count(old) == 1):
            out = out.replace(old, new)
    return clause if out == text else {**clause, "text": out}


# Rows of row-aligned sample data an arithmetic identity must be judged on before
# a rule is repointed at another column. Three is the floor at which a column
# reproducing the identity on every one of them is evidence rather than
# coincidence; templates parsed with row-aligned samples carry more.
_MIN_GROUNDING_ROWS = int(os.getenv("KAVACHIO_MIN_GROUNDING_ROWS", "3"))


def _aligned_rows(output_schema, sheet, fields):
    """The rows of ONE SHEET on which EVERY one of `fields` reports a number, as
    tuples in that field order. Samples are row-aligned within a sheet (see
    exporter._build_columns), so position i is the same row in each column; a row
    where any of them is blank or non-numeric is dropped, exactly as the compiled
    rule skips it."""
    cols = [_sheet_sample_numbers(output_schema, sheet, f) for f in fields]
    if not cols or any(not c for c in cols):
        return []
    n = min(len(c) for c in cols)
    rows = [tuple(c[i] for c in cols) for i in range(n)]
    return [r for r in rows if all(v is not None for v in r)]


def _math_holds(output_schema, sheet, result_field, params):
    """(holds, rows) for a "result = left <op> right" identity measured on the
    row-aligned samples — `holds` is True only when EVERY usable row satisfies it.

    Mirrors what the compiled rule will do (rule_compiler._b_cross_field_math):
    percent-stored operands scaled, both sides rounded to the reporting precision,
    the deviation compared against the rule's own tolerance band, and a division
    by zero skipped rather than failed."""
    from contract_upload_services.rule_compiler import NUMERIC_MATCH_DECIMALS
    op = params.get("operator")
    left_f, right_f = params.get("left_field"), params.get("right_field")
    rows = _aligned_rows(output_schema, sheet, [result_field, left_f, right_f])
    if not rows:
        return False, 0
    try:
        dec = int(params.get("decimals")) if params.get("decimals") is not None \
            else NUMERIC_MATCH_DECIMALS
        tol = float(params.get("tolerance_pct") or 0) / 100.0
    except (TypeError, ValueError):
        return False, 0
    used = 0
    holds = True
    for res, left, right in rows:
        if params.get("left_is_percent"):
            left = left / 100.0
        if params.get("right_is_percent"):
            right = right / 100.0
        # Mirrors _b_cross_field_math: complement applied after percent scaling.
        if params.get("left_complement"):
            left = 1.0 - left
        if params.get("right_complement"):
            right = 1.0 - right
        if op == "+":
            expected = left + right
        elif op == "-":
            expected = left - right
        elif op == "*":
            expected = left * right
        elif op == "/":
            if right == 0:
                continue                    # skipped by the compiled rule too
            expected = left / right
        else:
            return False, 0
        used += 1
        exp_r = round(expected, dec)
        dev = round(abs(round(res, dec) - exp_r), dec)
        if dev > tol * abs(exp_r):
            holds = False
    return (holds and used > 0), used


def _ground_cross_field_math_operands(ir, output_schema):
    """Point a "result = left <op> right" identity at the OPERAND columns the DATA
    actually supports.

    A bordereau reports one quantity under several headings — a collected figure,
    the same figure inclusive of a charge, the same figure less that charge — and
    they carry identical values on every policy that did not buy the charge. So
    whoever writes the formula, whether a template author, a contract clause or
    the formula inference, is choosing between columns that look interchangeable
    and often are not: bind the identity to the wrong one and the rule flags
    exactly the handful of rows where the two part company — the rows that are
    correct — while the real defect it was written for goes unreported.

    The file settles it. When the stated identity provably FAILS on the sampled
    rows and swapping ONE operand for another column of the same sheet makes it
    hold on every one of them, that column is the one the formula meant.

    Deliberately narrow, so a correct rule is never disturbed:
      • the incumbent must be PROVABLY wrong on the data (an identity that holds
        is left exactly as written — a rule the data disagrees with on a few rows
        is the defect the rule exists to report, and refitting it to the data
        would erase it);
      • the replacement must hold on EVERY usable row and on no fewer rows than
        the incumbent was judged on, so a mostly-empty column cannot win by
        having little to disagree with;
      • only ONE operand may be repairable. If either could be swapped to make
        the identity hold, the data does not say which one was mis-picked, and a
        guess is worse than the rule as written.
    The three columns must also share a SHEET, since that is the only scale on
    which their samples are the same rows — and a formula whose operands live on
    different sheets does not compile into a per-row check anyway.
    Purely arithmetic: no column-name vocabulary, nothing about any programme.
    """
    if not isinstance(ir, dict) or ir.get("template") != "cross_field_math":
        return ir
    p = ir.get("params") or {}
    res, left, right = (p.get("result_field"), p.get("left_field"),
                        p.get("right_field"))
    if p.get("operator") not in ("+", "-", "*", "/"):
        return ir
    if not all(isinstance(f, str) and f for f in (res, left, right)):
        return ir

    def _sheets(f):
        return set(output_schema.field_to_sheets.get(f) or [])

    common = _sheets(res) & _sheets(left) & _sheets(right)
    if not common:
        return ir               # not one row-set — nothing to compare row-wise
    sheet = sorted(common)[0]

    holds, rows = _math_holds(output_schema, sheet, res, p)
    if holds or rows < _MIN_GROUNDING_ROWS:
        return ir               # already checks out, or too little to judge on

    siblings = [c for c in output_schema.field_names
                if c not in (res, left, right) and sheet in _sheets(c)]

    repairs = {}
    for slot in ("left_field", "right_field"):
        fits = []
        for cand in siblings:
            trial = {**p, slot: cand}
            ok, n = _math_holds(output_schema, sheet, res, trial)
            if ok and n >= rows:
                fits.append(cand)
        if fits:
            repairs[slot] = fits
    if len(repairs) != 1:
        return ir               # nothing fits, or both operands could — ambiguous

    slot, fits = next(iter(repairs.items()))
    incumbent = p.get(slot)
    pick = _closest_named(incumbent, fits)
    out = dict(ir)
    out["params"] = {**p, slot: pick}
    _restate_field(out, incumbent, pick)
    print(f"[verify] {res!r} = {left!r} {p['operator']} {right!r} does not hold on "
          f"{rows} sample row(s); replacing {incumbent!r} with {pick!r} does — "
          f"repointed the formula's operand."
          + (f" ({len(fits)} columns fit; {pick!r} is the one whose name is "
             f"closest to {incumbent!r})" if len(fits) > 1 else ""))
    return out


# An exact-equality constant this many times larger (or smaller) than every sampled
# value of its column cannot be a per-row requirement. Deliberately generous: a
# genuine limit/rate rule sits within the same order of magnitude as its column.
_CONSTANT_MAGNITUDE_RATIO = float(os.getenv("KAVACHIO_CONSTANT_MAGNITUDE_RATIO", "100"))


def _constant_magnitude_mismatch(ir, output_schema):
    """Reason string when an EXACT-equality rule demands a constant that the bound
    column's own sample values put orders of magnitude out of reach; None otherwise.

    Contracts state figures that describe the WHOLE agreement — a layer limit, a
    participant's share of that layer, a programme aggregate. Bound to a column
    that reports a PER-ROW amount, "every row must equal $1,250,000" flags 100% of
    rows and can never detect the defect it was written for. The signal is
    arithmetic and needs no vocabulary: every sampled value of the column is at
    least `_CONSTANT_MAGNITUDE_RATIO`× away from the constant.

    Narrow on purpose — exact equality only (a limit is a ceiling and legitimately
    sits above the data), at least two numeric samples, and a 100× margin — so a
    valid rule whose column merely happens not to show the contract's value in
    three sample rows is untouched. This is the last net under the mapping
    instructions, not a substitute for them.
    """
    if not isinstance(ir, dict) or ir.get("template") != "range_check":
        return None
    p = ir.get("params") or {}
    field = p.get("field")
    try:
        lo, hi = float(p.get("min")), float(p.get("max"))
    except (TypeError, ValueError):
        return None
    if lo != hi or lo == 0 or not isinstance(field, str):
        return None

    nums = [n for n in _sample_numbers(output_schema, field) if n is not None]
    if len(nums) < 2:
        return None
    biggest = max(abs(n) for n in nums)
    if biggest == 0:
        return None
    ratio = abs(lo) / biggest
    if ratio < _CONSTANT_MAGNITUDE_RATIO and ratio > 1 / _CONSTANT_MAGNITUDE_RATIO:
        return None
    fmt = lambda n: f"{n:,.2f}".rstrip("0").rstrip(".")
    shown = ", ".join(fmt(n) for n in nums[:3])
    return (f"requires every row of {field!r} to equal {fmt(lo)}, but that column's "
            f"sample values are {shown} — {ratio:.0f}× apart, so no row can ever "
            f"satisfy it. A contract-level figure (a layer limit, a share of it) "
            f"is not a per-row value; the per-row check is usually the PROPORTION "
            f"it came from")


def _fix_invariant_ir(ir):
    """Repair an INVARIANT intent the mapper collapsed into a UNIQUENESS rule.

    "<value> must not CHANGE across one entity's rows" and "<value> must be UNIQUE
    across rows" read alike in English but are opposite checks, and `uniqueness` is
    the wrong one for a bordereau:

        uniqueness(["Policy Number", "Policy Eff Dt"])
            GROUP BY policy, eff_dt HAVING COUNT(*) > 1
            → flags a policy that merely HAS more than one row. A bordereau lists
              many rows per policy (endorsements, instalments, unearned-premium
              movements) all carrying the SAME date, so this flags every ordinary
              multi-transaction policy and never finds a moved date.

        invariant(field=eff_dt, per=policy)
            GROUP BY policy HAVING COUNT(DISTINCT eff_dt) > 1
            → flags exactly the policies whose date really does differ between
              rows, and only those.

    The repair is mechanical: same two columns, re-shaped into the aggregate_cap
    the intent asked for (no new template, no new compiler code). Roles come from
    the uniqueness key ORDER the mapper emits — the entity key first, the value
    that must hold still last — which is how the composite key is specified in the
    mapping prompt. With fewer than two columns there is nothing to group by, so
    the rule is left alone for the normal gates to handle.

    Fires only on an IR whose intent operator was `invariant` (stamped in
    stage_b_synthesizer), so a genuine "must be unique" rule is never touched.
    """
    if not isinstance(ir, dict) or ir.get("_intent_operator") != "invariant":
        return ir
    if ir.get("template") != "uniqueness":
        return ir            # already mapped to aggregate_cap — nothing to repair
    fields = [f for f in ((ir.get("params") or {}).get("fields") or []) if f]
    if len(fields) < 2:
        return ir
    *key, subject = fields
    out = dict(ir)
    out["template"] = "aggregate_cap"
    out["params"] = {"aggregation": "distinct_count", "field": subject,
                     "group_by": key, "max": 1}
    print(f"[verify] invariant intent was mapped to uniqueness{fields} — rewrote to "
          f"one distinct {subject!r} per {', '.join(key)} (a uniqueness rule here "
          f"would flag every policy with more than one row).")
    return out


def _referral_to_conditional(ir, output_schema):
    """Rewrite a referral intent into conditional_value(trigger → indicator='Yes'):
    flag rows where the trigger is met but the policy was NOT referred. When the
    trigger can't be bound to an indicator column, a positive-membership
    `value_in_set` trigger is flipped to `value_not_in_set` (see
    _flip_value_in_set_referral) so it still flags the trigger rows instead of the
    complement; other templates are left unchanged."""
    if not isinstance(ir, dict):
        return ir
    indicator, referred = _find_referral_indicator(output_schema)
    if not indicator:
        return _flip_value_in_set_referral(ir)
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
        # Couldn't reduce the trigger to a single indicator condition (e.g. a
        # multi-value set trigger like AK/HI). Still fix the polarity of a
        # value_in_set trigger so it flags the trigger rows, not the complement.
        return _flip_value_in_set_referral(ir)
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
    the spellings the rule carries — i.e. adding it changes no row.

    The compiled query matches a cell to a value when the two are EQUAL after
    normalization, so that is the question here too. It used to be asked with the
    runtime's old similarity score, which answered "already matched" for any
    spelling that merely RESEMBLED one on the rule — and a spelling suppressed on
    that basis was one the rule then never matched, because the runtime it was
    mirroring no longer scores anything."""
    nc = _vv_norm(candidate)
    return bool(nc) and any(nc == _vv_norm(a) for a in accepted)


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
                             group_members=None, variation_memo=None):
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

    # 1b-invariant) "must not change across an entity's rows" is a per-group
    # invariant, not row uniqueness. If the mapper collapsed it into `uniqueness`
    # the rule would flag every policy that simply has more than one transaction
    # row — repair it structurally rather than depend on the LLM picking the right
    # template.
    ir = _fix_invariant_ir(ir)

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
        elif _is_container_geography_exclusion(ir):
            # CONTAINER-GEOGRAPHY RESCUE: the dropped scope is a country the
            # template can't bind, but the rule already excludes finer-level US
            # territories on a state column — valid for every row regardless of
            # the country cell. Keep the exclusion instead of losing the rule;
            # the scope is already absent from params, so just fall through.
            print(f"[verify] dropped redundant container scope "
                  f"({scope_txt[:60]}…) — kept finer-level territory exclusion on "
                  f"{(ir.get('params') or {}).get('field')!r}.")
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

    # 1d) ground a "field = other_field × factor" identity in the sample rows.
    # First the CONSTANT: a rate that lost its percent-to-fraction step (5 for 5%)
    # checks 100× the intended value and flags every row — the samples show which
    # decimal scale the identity actually holds at. Then the BASE: when the
    # mapper's base column provably does not reproduce the target's samples and
    # exactly one other column does, use that one. A bordereau often offers a gross
    # amount, a deduction and their net as candidate bases; only the data says
    # which one the share was struck on. Constant first, so a correct base is not
    # abandoned merely because the scale it was paired with was wrong.
    # …and the same question for a "result = left <op> right" formula: a bordereau
    # reports one amount under several headings that agree until the row where
    # they don't, so an identity can be bound to a column the file itself
    # disagrees with. When the stated one provably fails on the sample rows and
    # swapping ONE operand makes it hold on all of them, use that column. See
    # _ground_cross_field_math_operands.
    _params_before = dict(ir.get("params") or {})
    ir = _ground_multiplicative_factor(ir, output_schema)
    ir = _ground_multiplicative_base(ir, output_schema)
    ir = _ground_cross_field_math_operands(ir, output_schema)
    # A DERIVED rule's clause text is the deriver's own restatement of it, and
    # "where this comes from" quotes it — so it names whatever column the rule
    # ended up bound to. A contract clause is quoted verbatim and never rewritten.
    clause = _restate_derived_clause_text(clause, _params_before,
                                          ir.get("params") or {})

    # 1e) a contract-level figure bound to a per-row column. An exact-equality whose
    # constant is orders of magnitude away from every sampled value of its column
    # can only ever flag 100% of rows — route it to review rather than ship it.
    _magnitude_reason = _constant_magnitude_mismatch(ir, output_schema)
    if _magnitude_reason:
        return {"route": "review", "reason": _magnitude_reason}

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
                             default_sheet=output_schema.primary_sheet,
                             aliases=getattr(output_schema, "field_aliases", None))
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

        # 3a-bis-2) POLARITY guard: a REQUIRED-value clause ("utilize X paper for
        #     all policies; any deviation requires referral") sometimes arrives
        #     inverted as value_not_in_set(excluded=[X]) — flagging the compliant
        #     rows and passing the deviations. Detected from the rule's own
        #     description and flipped to value_in_set(allowed=[X]). Runs BEFORE
        #     the enum steps below so they all see the final polarity.
        _fix_inverted_required_value(ir)

        # 3a-bis-2½) REFERENCE-VALUE guard: enum "values" that only restate the
        #     rule's own concept ("the Program Managers", the field name itself)
        #     are cross-references to a defined term, not literal cell values —
        #     prune them; if nothing literal remains, the members are defined
        #     elsewhere in the contract and the rule needs re-resolution.
        _ref_reason = _prune_reference_enum_values(ir)
        if _ref_reason:
            return {"route": "review", "reason": _ref_reason}

        # 3a-bis-3) REFERRAL-CLASSIFICATION backstop: the general "except state
        #     S" requirement direction of a routing clause sometimes loses its
        #     is_referral flag to a single non-deterministic classification
        #     pass, shipping as a hard block instead of the contract's actual
        #     "any deviation requires Referral to the Company" consequence.
        #     Re-derive it from the clause's own text; scoped so it can never
        #     touch the complementary carve-out direction (see docstring).
        _force_referral_on_carveout_requirement(ir, clause)

        # 3a-quater) TERRITORY-ALLOWLIST repair: a "home state within <country>
        #     … excluding <territories>" clause that collapsed to a near-empty
        #     value_in_set (e.g. just "DC") instead of the intended exclusion —
        #     rebuild it as the canonical territory exclusion before the
        #     expansion step below, which then also seeds its abbreviations.
        _repair_tiny_state_allowlist(ir, clause)

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
                ir["params"] = topup_variation_values(
                    _enum_tmpl, ir["params"], memo=variation_memo)
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
        # 3c-bis) Same for US STATE abbreviations on a state column ("Alaska" →
        #     also match "AK") — the BDX's canonical spelling is often the USPS
        #     code, which the generic filter above would drop as untraceable.
        if _enum_tmpl in ("value_in_set", "value_not_in_set"):
            _seed_state_abbreviations(ir)

        # 3d) A state-column SCOPE value widens to BOTH spellings ("California" →
        #     ["California", "CA"]) so the scope keeps scoping whichever form the
        #     BDX reports. Any template — scopes appear on most of them.
        _expand_state_scope_values(ir)

        # 4) compile_ir — deterministic IR → DuckDB SELECT. Pass the field→ALL-sheets
        #    map so a rule fans out (UNION) to every sheet that has its column(s).
        try:
            sql = compile_ir(ir, output_schema.field_to_sheets,
                             aliases=getattr(output_schema, "field_aliases", None))
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
    # Alias SPELLINGS the rule also runs on: compile fans each rule out to
    # sheets that carry the same concept under a different column name
    # (OutputSchema.field_aliases — canonical/name/value/LLM evidence). Surface
    # those names in canonical_target.output_fields too, or every
    # rules-per-column view (Bordereau Setup detail, the field picker's clause
    # list) shows the rule mapped ONLY to the primary spelling while it in
    # fact validates "Policy No" / "Account Reference #" columns as well.
    _fa = getattr(output_schema, "field_aliases", {}) or {}
    alias_spellings: list = []
    for _bf in bound_fields:
        for _other in (_fa.get(_bf) or {}).values():
            if _other not in bound_fields and _other not in alias_spellings:
                alias_spellings.append(_other)

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
                                  "output_fields": bound_fields + alias_spellings,
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
        # Provenance: 'generic_library' for a rule seeded from
        # generic_rule_specification (applies to every program, not read from this
        # contract), 'contract' for everything the clause pipeline produced.
        # Set on the IR by generic_rule_library.derive_generic_library_entries.
        "rule_source":           ir.get("rule_source") or "contract",
        "ir":                    ir,
        "template":              template,
        "vocab_version":         VOCAB_VERSION,
        "catalog_version":       CATALOG_VERSION,
        "error_message":         error_message,
        "source_clause_id":      clause.get("clause_id"),
        "source_verbatim_text":  clause.get("text"),
        "source_page_number":    clause.get("page_number") or clause.get("page"),
        # Where the clause sat and what it was tagged as. Not persisted (the
        # INSERT names its columns explicitly) — these are read by the in-memory
        # consolidation passes below to tell which clauses are items of ONE
        # enumerated list. See _cc_list_key.
        "source_section_header": clause.get("section_header"),
        "source_clause_type":    clause.get("clause_type"),
        "generation_confidence": ir.get("confidence"),
        # IR rules that pass the verify gate are trusted and go live immediately:
        # rule_status='active'. (The canonical CHECK constraint allows
        # active|needs_review|superseded|disabled|failed_compilation; 'active'
        # is the default so rules are not parked in a review state.)
        "rule_status":           "needs_review" if unmapped else "active",
        "created_by":            "ai_generator_ir_v1",
    }


def _cc_recompile(r, new_ir, output_schema):
    """Write a rewritten IR back onto a rule, recompiling its SQL.

    `r["ir"]` and `r["rule_spec"]["ir"]` are the SAME object at build time, and
    the RUNTIME reads `rule_spec["compiled_sql"]` — so updating only the
    top-level keys would persist and EXECUTE the pre-merge query while the UI
    showed the merged IR. Both homes are written, exactly as
    _unflip_required_value_referrals does. Returns False (leaving the rule
    untouched) if the merged IR will not compile.
    """
    # Imported here like every other compile site in this module — it is not a
    # module-level name, and a broad `except` around the call would swallow the
    # resulting NameError and skip every merge in silence.
    from contract_upload_services.rule_compiler import compile_ir, CompileError
    try:
        sql = compile_ir(new_ir, output_schema.field_to_sheets,
                         default_sheet=output_schema.primary_sheet,
                         aliases=getattr(output_schema, "field_aliases", None))
    except CompileError:
        return False
    r["ir"] = new_ir
    r["template"] = new_ir.get("template")
    r["compiled_sql"] = sql
    spec = r.get("rule_spec")
    if isinstance(spec, dict):
        spec["ir"] = new_ir
        spec["compiled_sql"] = sql
    return True


def _cc_norm_text(s):
    """Clause text flattened for prefix comparison (whitespace is not meaning)."""
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _cc_text_minus_own_values(rules, i):
    """A rule's clause text with the VALUES THAT RULE ENFORCES removed.

    What is left is the requirement's WORDING, stripped of the particular value
    this copy of it carries — so two clauses that state the same requirement
    about different values reduce to (nearly) the same string."""
    t = _cc_norm_text(rules[i].get("source_verbatim_text"))
    if not t:
        return ""
    params = (rules[i].get("ir") or {}).get("params") or {}
    vals = list(params.get("allowed") or []) + list(params.get("variation_values") or [])
    for v in sorted((str(v).strip().lower() for v in vals if str(v).strip()),
                    key=len, reverse=True):
        t = t.replace(v, "")
    return re.sub(r"\s+", " ", t).strip()


# How alike two value-stripped clauses must read to count as ONE requirement
# restated. Well above what unrelated clauses on a column score, and below the
# near-identity a restatement produces (a differing heading, "Class:" vs
# "Class of Business:", still scores ~0.94).
_CC_SAME_REQUIREMENT_RATIO = 0.90


def _cc_same_requirement(rules, i, j) -> bool:
    """True when two clauses state the SAME requirement with DIFFERENT values.

    The prefix test below catches a list that extraction SPLIT (every sibling
    re-quotes the lead-in, so they share a long head). It cannot catch the other
    shape: one requirement RESTATED elsewhere in the document — a risk-details
    page and a signing page each naming the binder the business was accepted
    under, where the two clauses differ at the START (their headings) and agree
    everywhere after it. Comparing them with their OWN enforced values removed
    is what makes that visible, and it is the same evidence in both cases: the
    wording, read off the contract, never a list of phrases known in advance.

    Why they must be one rule: the column holds ONE value per row, so two
    single-value requirements enforced separately can never both pass — each
    rejects precisely the rows the other accepts.
    """
    import difflib
    a, b = _cc_text_minus_own_values(rules, i), _cc_text_minus_own_values(rules, j)
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= _CC_SAME_REQUIREMENT_RATIO


def _cc_group_by_lead_in(rules, idxs):
    """Partition rule indices by the LIST LEAD-IN their clauses quote.

    When extraction splits an enumerated list it emits one clause per item, each
    re-quoting the list's lead-in before its own item — so siblings share a long
    common prefix and unrelated clauses share essentially none. The lead-in is
    therefore discovered from the text itself: nothing about its wording is known
    in advance, which is what keeps this working across every contract rather
    than the ones someone thought to enumerate.

    Two clauses are siblings when their common prefix covers at least half of the
    shorter text — comfortably above the handful of characters two unrelated
    clauses share by chance, and comfortably below the near-identical prefixes a
    split list produces. Returns groups in input order, singletons included.
    """
    groups = []                       # [(lead_in_prefix, [idx, ...])]
    for i in idxs:
        t = _cc_norm_text(rules[i].get("source_verbatim_text"))
        if not t:
            groups.append((None, [i]))
            continue
        for gi, (prefix, members) in enumerate(groups):
            if prefix is None:
                continue
            common = os.path.commonprefix([prefix, t])
            if len(common) >= max(1, min(len(prefix), len(t)) // 2):
                groups[gi] = (common, members + [i])
                break
            # …or the same requirement RESTATED with a different value, which
            # shares no long head to be found by the prefix test above.
            if _cc_same_requirement(rules, members[0], i):
                groups[gi] = (prefix, members + [i])
                break
        else:
            groups.append((t, [i]))
    return [members for _prefix, members in groups]


def _cc_is_referral(r):
    """True when a built rule is a referral trigger, checking every home.

    verify_and_build_ir_rule writes `is_referral` top-level and `referral` inside
    rule_spec; the IR may also carry `is_referral`. A referral is not a
    compliance requirement, so two referral triggers on one column are not a
    contradiction and must never be consolidated.
    """
    if r.get("is_referral") is True:
        return True
    spec = r.get("rule_spec")
    if isinstance(spec, dict):
        if spec.get("referral") is True:
            return True
        if (spec.get("ir") or {}).get("is_referral") is True:
            return True
    return (r.get("ir") or {}).get("is_referral") is True


# An ISO calendar date. date_bound stores 'YYYY-MM-DD', and only that form may be
# ordered with min()/max() on the raw string — a '01/04/2026' mixed in would sort
# by its leading digits and invert the choice of bound.
_CC_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _canonical_scope_value(v):
    """One scope value reduced to its MEANING, so different ENCODINGS of the
    same row filter compare equal: a plain string, ["X"], and
    {"allowed": ["X"], "variation_values": [...]} all say "rows where the
    column is X" — the mapper emits all three shapes for one requirement, and
    comparing them verbatim made two copies of the same rule look different
    (variation_values only widen SPELLING matching, never the requirement)."""
    if isinstance(v, dict):
        if isinstance(v.get("allowed"), (list, tuple, set)):
            return {"in": sorted(str(x).strip().lower() for x in v["allowed"])}
        if isinstance(v.get("excluded"), (list, tuple, set)):
            return {"not_in": sorted(str(x).strip().lower() for x in v["excluded"])}
        return {k: _canonical_scope_value(val)
                for k, val in sorted(v.items()) if k != "variation_values"}
    if isinstance(v, (list, tuple, set)):
        return {"in": sorted(str(x).strip().lower() for x in v)}
    return {"in": [str(v).strip().lower()]}


def _dedup_canonical_params(params):
    """Params reduced to the rule's MEANING for duplicate detection: every
    variation_values list stripped (spelling tolerance, not semantics), the
    scope canonicalized (see _canonical_scope_value), everything else kept."""
    out = {}
    for k, v in (params or {}).items():
        if k == "variation_values":
            continue
        if k == "scope" and isinstance(v, dict):
            out[k] = {sk: _canonical_scope_value(sv) for sk, sv in sorted(v.items())}
        else:
            out[k] = v
    return out


def _cc_scope_key(params):
    """A rule's row scope, normalised for equality. Rules whose scopes DIFFER are
    not talking about the same rows, so they are never consolidated. Compared by
    MEANING (see _canonical_scope_value): the same filter written as a plain
    value on one rule and an allowed-list-with-variations on another must not
    block consolidation of two copies of one requirement."""
    sc = (params or {}).get("scope")
    if isinstance(sc, dict):
        sc = {k: _canonical_scope_value(v) for k, v in sorted(sc.items())}
    sheets = (params or {}).get("scope_sheets")
    if isinstance(sheets, (list, tuple, set)):
        sheets = sorted(str(s).strip().lower() for s in sheets)
    return json.dumps({"scope": sc, "sheets": sheets}, sort_keys=True, default=str)


def _gate_reason_binding_disagreement(rules, output_schema):
    """Pause a rule whose mapping explanation names a DIFFERENT column than the
    one it actually bound.

    The defect this catches (validated on a real regression): a library rule
    whose ir.reason said "Mapped 'Contract Identifier' to 'Policy No' …" while
    params.field — and justification.mapped_field with it — held 'Reins Eff
    Date', a date column. The structural type check can't object (required_field
    is type-agnostic), so the internal contradiction is the only signal.

    Deterministic and vocabulary-free: quoted strings in ir.reason count ONLY
    when they exactly match a template field name, so prose like "'auto' is a
    type of 'unit'" contributes nothing. The rule is paused only when the
    reason's field names and the bound field names are BOTH non-empty and share
    no member — agreement on any one field passes (multi-field reasons routinely
    quote several bound columns).
    """
    known = {str(f.get("name")) for f in
             (getattr(output_schema, "template_fields", None) or [])
             if f.get("name")}
    if not known:
        return rules
    for r in rules:
        if not isinstance(r, dict) or r.get("rule_status") != "active":
            continue
        ir = r.get("ir") or {}
        reason = str(ir.get("reason") or "")
        if not reason:
            continue
        quoted_fields = {q for q in re.findall(r"'([^']+)'", reason) if q in known}
        if not quoted_fields:
            continue
        p = ir.get("params") or {}
        bound = {v for k, v in p.items()
                 if isinstance(v, str) and v in known}
        cond = p.get("condition")
        if isinstance(cond, dict) and cond.get("field") in known:
            bound.add(cond["field"])
        bound.update(a for a in (ir.get("field_aliases") or [])
                     if isinstance(a, str) and a in known)
        if bound and not (quoted_fields & bound):
            _cc_downgrade(
                r, "needs_review",
                f"The mapping's own explanation names "
                f"{sorted(quoted_fields)} but the rule is bound to "
                f"{sorted(bound)} — the binding contradicts its reasoning; "
                f"confirm the intended column.")
    return rules


def _gate_untraceable_referral_flagsets(rules):
    """Pause a referral whose trigger values cannot be traced to the contract.

    Referral polarity is the one failure with no downstream signal: a flipped
    trigger set ("flag 'NO'" instead of "flag 'Yes'") is a well-formed rule that
    fires on exactly the wrong rows. _unflip_required_value_referrals catches
    the flips that contradict a requirement rule; this catches the rest by
    provenance: every value a referral FLAGS should be traceable to the clause
    that created it (the clause text, or the bound column's own name — "(Y/N)"
    headers name their codes). A flag set with NO traceable member means the
    orientation was invented by the mapper, not read from the contract — that
    is precisely how the inverted facultative referral was born, so it goes to
    review for a human to confirm the direction ONCE (carry-forward then
    remembers the confirmed orientation on every future regeneration).

    Values with any trace survive untouched: "Alaska"/"Hawaii" appear in their
    clause, authorised-paper names appear in theirs — those referrals never see
    this gate. Disable with KAVACHIO_REFERRAL_TRACE_GATE=0.
    """
    if os.getenv("KAVACHIO_REFERRAL_TRACE_GATE", "1") == "0":
        return rules

    def _norm(t):
        return re.sub(r"[^a-z0-9 ]", " ", str(t or "").lower())

    for r in rules:
        if not isinstance(r, dict) or not r.get("is_referral"):
            continue
        if r.get("rule_status") != "active":
            continue
        ir = r.get("ir") or {}
        if ir.get("template") not in ("value_in_set", "value_not_in_set"):
            continue
        p = ir.get("params") or {}
        flags = [v for v in (p.get("excluded") or p.get("allowed") or [])
                 if isinstance(v, str) and v.strip()]
        if not flags:
            continue
        universe = " ".join(_norm(t) for t in (
            r.get("source_verbatim_text"), p.get("field")))
        tokens = set(universe.split())
        traced = False
        for v in flags:
            nv = _norm(v).strip()
            if nv and (nv in universe or
                       any(t in tokens for t in nv.split() if len(t) >= 2)):
                traced = True
                break
        if not traced:
            _cc_downgrade(
                r, "needs_review",
                f"None of the values this referral flags "
                f"({flags[:6]}) appear in the clause it was read from — the "
                f"trigger direction cannot be verified from the contract. "
                f"Confirm whether these are the rows that require referral.")
    return rules



class _NameOnlySchema:
    """Minimal stand-in for OutputSchema carrying only `template_fields`.

    The two provenance gates need nothing but the set of valid column NAMES, so
    a caller that has names (the persist side, which holds
    metadata.template_field_names) can run them without rebuilding a full
    OutputSchema — which would need the template's samples and data dictionary.
    """

    def __init__(self, field_names):
        self.template_fields = [{"name": n} for n in (field_names or []) if n]


def apply_provenance_gates(rules, field_names=None, output_schema=None):
    """Run the deterministic provenance gates over ANY rule list.

    Exposed because these gates must also police rules this module did not
    generate: on a regeneration, rules carried forward from a prior version of
    the contract bypass the whole normalizer, so without this a defect that was
    admitted once would be re-admitted forever (see regen_reconcile).

    Rules are mutated in place (paused via _cc_downgrade) and the list is
    returned. Rules whose dicts came from the DB carry their IR under
    `rule_spec.ir` rather than a top-level `ir`, so both shapes are accepted.
    """
    schema = output_schema or _NameOnlySchema(field_names)
    # DB-shaped rows keep the IR inside rule_spec; give the gates the top-level
    # `ir`/`is_referral` view they expect, without copying the rules.
    shimmed = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        if "ir" not in r:
            spec = r.get("rule_spec")
            if isinstance(spec, dict) and isinstance(spec.get("ir"), dict):
                r["ir"] = spec["ir"]
        if "is_referral" not in r:
            spec = r.get("rule_spec") if isinstance(r.get("rule_spec"), dict) else {}
            ct = r.get("canonical_target") if isinstance(r.get("canonical_target"), dict) else {}
            r["is_referral"] = bool(spec.get("referral") or ct.get("is_referral"))
        shimmed.append(r)
    _gate_reason_binding_disagreement(shimmed, schema)
    _gate_untraceable_referral_flagsets(shimmed)
    return rules


def gate_failures(rule, field_names=None, output_schema=None):
    """Which gates a rule FAILS, without mutating it. Returns a list of reasons.

    Used to compare a carried rule against its freshly generated twin: a defect
    the old rule carries and the new one does not is the one case where the new
    answer should win. Works on a shallow copy so the caller's rule is untouched.
    """
    probe = dict(rule)
    probe["rule_status"] = "active"
    probe["rule_description"] = ""
    probe["rule_spec"] = dict(probe.get("rule_spec") or {})
    apply_provenance_gates([probe], field_names, output_schema)
    if probe.get("rule_status") == "active":
        return []
    return [(probe.get("rule_description") or "").strip()]


def _cc_downgrade(r, status, why):
    """Stop enforcing a rule, recording WHY somewhere a human will actually see.

    The engine only runs `rule_status='active'` (validation_routes.py), while the
    contract's rule list shows everything except 'disabled' — so a downgraded rule
    stays visible and re-enableable instead of vanishing. There is no column for a
    per-rule note, so the reason goes on `rule_description` (rendered by the
    Contract Detail / Bordereau Setup / Output Template screens) and, structured,
    inside `rule_spec` (persisted verbatim as JSONB).
    """
    r["rule_status"] = status
    prefix = "Not enforced" if status == "superseded" else "Paused for review"
    desc = (r.get("rule_description") or "").strip()
    r["rule_description"] = f"[{prefix}] {why}" + (f" Original rule: {desc}" if desc else "")
    spec = r.get("rule_spec")
    if isinstance(spec, dict):
        spec["consolidation"] = {"status": status, "reason": why}


# Ordinal date_bound operators, and which end of the range is the WEAKEST bound.
# `op` is the COMPLIANT relation, so for ">=" the most permissive bound is the
# EARLIEST date and for "<=" it is the LATEST. "=" / "!=" have no ordering and
# are never consolidated.
_CC_DATE_OPS = {">=": min, ">": min, "<=": max, "<": max}


# Any number written in prose — thousands separators optional, decimals optional.
_CC_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def _cc_fmt_num(v):
    """A rule's numeric value as a reviewer would write it — no trailing zeros on
    a whole number, full precision otherwise (25.0005 must not read as 25)."""
    f = float(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _cc_fixed_value(params):
    """The single value a range_check pins its column to (min == max), else None.

    A fixed value is the one numeric shape that can CONTRADICT another: two
    ranges may narrow each other, but two different exact values cannot both be
    satisfied by one cell."""
    if not isinstance(params, dict):
        return None
    lo, hi = params.get("min"), params.get("max")
    if lo is None or hi is None:
        return None
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return None
    return lo if lo == hi else None


# Templates that put a NUMERIC ceiling / floor on a column. They are reconciled
# as one family (not per template) because one item of a limits list can compile
# to a per-row max_limit while its sibling compiles to a per-policy aggregate_cap
# — split by template they would never be compared, which is the whole point.
_CC_BOUND_TEMPLATES = ("max_limit", "min_limit", "range_check", "aggregate_cap")


def _cc_numeric_bound(template, params):
    """The ONE-SIDED numeric bound a rule puts on its column, as (direction, value).

    Only one-sided bounds take part. A two-sided band ("between 1 and 9") narrows
    rather than contradicts, and a fixed value (min == max) is the range_check
    branch's business — both return None here.

    aggregate_cap joins only for aggregation="sum": a distinct_count cap is a
    consistency invariant ("this must not change across the policy's rows"), not
    a money limit, and has no business being weighed against one.
    """
    if not isinstance(params, dict) or template not in _CC_BOUND_TEMPLATES:
        return None
    if template == "aggregate_cap" and str(params.get("aggregation") or "sum").lower() != "sum":
        return None
    lo, hi = params.get("min"), params.get("max")
    if (lo is None) == (hi is None):          # neither bound, or a band / fixed value
        return None
    try:
        return ("max", float(hi)) if lo is None else ("min", float(lo))
    except (TypeError, ValueError):
        return None


def _cc_bound_subject(template, params):
    """WHAT a numeric bound measures: one entity's figure, or the whole book.

    A per-policy ceiling and a portfolio total are different requirements even
    when a contract lists them side by side ("per policy $1M; in the aggregate
    $100M"), so they are never reconciled against each other. Everything that
    resolves to one entity's value — a per-row limit, or a sum grouped by a
    policy/occurrence key — is one subject; an ungrouped sum across every row is
    the other.
    """
    if template == "aggregate_cap" and not [g for g in (params.get("group_by") or []) if g]:
        return "book"
    return "entity"


def _cc_value_is_verbatim(rules, i, value) -> bool:
    """True when the rule's value is WRITTEN in its own clause, rather than worked
    out from it.

    "ORDER HEREON: 45.4500% of 55.000% of 100.00%" yields 25.0005 only by
    multiplying — a reading of the sentence, not a quotation of it — while
    "a Quota Share … 45.4500% of the Reinsured's line" states its value outright.
    When two clauses pin one column to different values, the quoted one is the
    better-evidenced reading, so it is the one left enforced.

    Percent SCALE is not evidence either way: a rule may hold 45.45 or 0.4545 for
    the same "45.4500%", so both scalings count as quoted. Comparison is numeric,
    never string equality, so 45.45 matches "45.4500"."""
    text = str(rules[i].get("source_verbatim_text") or "")
    if not text:
        return False
    try:
        wanted = {round(float(value), 6), round(float(value) * 100, 6),
                  round(float(value) / 100, 6)}
    except (TypeError, ValueError):
        return False
    for m in _CC_NUMBER.finditer(text):
        try:
            if round(float(m.group(0).replace(",", "")), 6) in wanted:
                return True
        except ValueError:
            continue
    return False


# A MONEY amount immediately followed by a parenthesised percentage — "USD500,000
# (100%)", "$1,000,000 (50% share)". The percentage there states the BASIS the
# amount is expressed on (the whole-account level), so it is a property of that
# figure, not a value any column must equal. Matched by SHAPE — a currency marker,
# a number, then the bracket — so no currency list or phrase is written in.
_CC_AMOUNT_BASIS = re.compile(
    r"(?:[$£€¥]|\b[A-Z]{3}\s?)\s?\d[\d,]*(?:\.\d+)?\s*\(\s*(\d[\d,]*(?:\.\d+)?)\s*%")


def _cc_value_is_amount_basis(rules, i, value) -> bool:
    """True when the rule's value appears in its clause ONLY as the basis of a
    monetary amount.

    "CASH LOSS: USD500,000 (100%)" sets a cash-call threshold of half a million
    at the 100% level; reading the 100 as "this column must equal 100" turns a
    threshold into a share requirement and flags every row. If the same number
    also appears somewhere else in the clause, the qualifier tells us nothing and
    this returns False."""
    text = str(rules[i].get("source_verbatim_text") or "")
    if not text:
        return False
    try:
        wanted = {round(float(value), 6), round(float(value) * 100, 6),
                  round(float(value) / 100, 6)}
    except (TypeError, ValueError):
        return False

    def _hits(numbers):
        out = 0
        for raw in numbers:
            try:
                if round(float(str(raw).replace(",", "")), 6) in wanted:
                    out += 1
            except ValueError:
                continue
        return out

    as_basis = _hits(m.group(1) for m in _CC_AMOUNT_BASIS.finditer(text))
    if not as_basis:
        return False
    return as_basis == _hits(m.group(0) for m in _CC_NUMBER.finditer(text))


def _cc_document_order(rules, i):
    """Where a rule's clause sits in the document — page first, then the order it
    was extracted in. Used ONLY to break a tie between two equally-evidenced
    readings: the first statement of a term is the operative one, and the later
    mentions are the schedules and signing pages that restate it."""
    page = rules[i].get("source_page_number")
    cid = rules[i].get("source_clause_id")
    return (page if isinstance(page, int) else 10 ** 9,
            cid if isinstance(cid, int) else 10 ** 9,
            i)


# For a numeric bound, which end is the WEAKEST — the one no compliant row can
# fail. A ceiling is weakest at its HIGHEST value, a floor at its LOWEST.
_CC_BOUND_PICK = {"max": max, "min": min}


def _cc_list_key(r):
    """The enumerated list a rule's clause is an item of, or None if it can't be told.

    Extraction is asked to emit one clause per list item and to put the list's
    lead-in heading in `section_header`. It does not reliably inline that lead-in
    into each item's own text, so `_cc_group_by_lead_in`'s shared-prefix test
    cannot always see the list: on the K&B contract the eleven sub-limits arrive
    as bare "<Coverage>: $X aggregate;" strings sharing no head at all.

    What the items DO share, read straight off the extraction and never guessed,
    is the heading they sit under, the page they were read from, and the
    clause_type they were tagged with. The clause_type is what keeps the list's
    own lead-in sentence and the neighbouring liability cap OUT of the group —
    all three sit under that same heading on that same page, but extraction typed
    them 'limit' and 'aggregate_cap' against the items' 'sublimit'.
    """
    header = _cc_norm_text(r.get("source_section_header"))
    if not header:
        return None
    return (header, r.get("source_page_number"), r.get("source_clause_type"))


def _cc_group_by_list(rules, idxs):
    """Partition rule indices into the enumerated lists their clauses came from.

    Clauses whose extraction recorded a section heading are grouped by it
    (_cc_list_key); the rest fall back to the shared-lead-in prefix test, which is
    the only evidence available when there is no heading. Groups come back in
    input order, singletons included."""
    keyed, unkeyed = {}, []
    for i in idxs:
        key = _cc_list_key(rules[i])
        if key is None:
            unkeyed.append(i)
        else:
            keyed.setdefault(key, []).append(i)
    return list(keyed.values()) + _cc_group_by_lead_in(rules, unkeyed)


def _cc_resolve_competing_bounds(rules, by_bound):
    """Reconcile several ceilings (or floors) that one enumerated list put on ONE
    column. Returns how many rules stopped being enforced.

    A contract's limits section is typically a list of SUB-LIMITS, one per named
    coverage — "1. Professional Liability: $3,000,000 aggregate … 6. Policy
    Aggregate: $10,000,000 … 10. Damage to Residents Property: $20,000
    aggregate". A bordereau, though, carries ONE limit column for the policy, not
    a column per coverage, so every item of that list is mapped onto the same
    column and the column ends up carrying eleven different ceilings. Only one of
    them can be the limit that column actually reports; the other ten flag rows
    that breach a sub-limit governing a coverage the column never held. On the
    K&B bordereau the $20,000 residents-property sub-limit landed on "Primary
    Coverage In Aggregate" and flagged all 25 policies, whose $3,000,000 is
    perfectly within the $10,000,000 policy aggregate that column does report.

    So the WEAKEST bound stays enforced and the rest are PAUSED with the conflict
    written into the rule — the same stance, and the same reason, as the
    date_bound branch: the widest bound is the only one no compliant row can
    fail, so the check still catches a genuine breach of the section without any
    row being flagged by a sub-limit that does not govern its column. Nothing is
    merged (that would assert a limit no clause states) and nothing is dropped
    (the mis-binding must stay visible and re-enableable).

    GATES, in addition to the caller's identical-scope / non-referral keying:
      * one SUBJECT only — a per-policy ceiling is never weighed against a
        portfolio total (see _cc_bound_subject);
      * one DIRECTION only — a floor never cancels a ceiling;
      * siblings of ONE list only (_cc_group_by_list), so two unrelated clauses
        that happen to bound the same column are left alone;
      * >= 2 clauses stating >= 2 DIFFERENT values — one limit restated is no
        conflict.
    """
    downgraded = 0
    for (_col, _scope, direction, _subject), idxs in by_bound.items():
        if len(idxs) < 2:
            continue
        for group in _cc_group_by_list(rules, idxs):
            if len(group) < 2:
                continue
            if len({rules[i].get("source_clause_id") for i in group}) < 2:
                continue
            col = (((rules[group[0]].get("ir") or {}).get("params") or {})
                   .get("field") or _col)
            valued = []
            for i in group:
                ir = rules[i].get("ir") or {}
                bound = _cc_numeric_bound(ir.get("template"), ir.get("params") or {})
                if bound is not None:
                    valued.append((bound[1], i))
            if len({v for v, _ in valued}) < 2:
                continue                    # one limit restated — not a conflict
            keep_val = _CC_BOUND_PICK[direction](v for v, _ in valued)
            all_vals = ", ".join(_cc_fmt_num(v) for v in sorted({v for v, _ in valued}))
            word = "ceiling" if direction == "max" else "floor"
            for v, i in valued:
                if v == keep_val:
                    continue
                _cc_downgrade(
                    rules[i], "needs_review",
                    f"This contract states more than one {word} for {col} in a "
                    f"single list ({all_vals}), and the column reports one "
                    f"figure — enforced together they flag rows that only "
                    f"breach a limit governing something this column never "
                    f"carried. {_cc_fmt_num(keep_val)} is enforced because it "
                    f"is the widest, the one bound no compliant row can fail; "
                    f"confirm which item of the list this column reports and "
                    f"re-enable the right limit.")
                downgraded += 1
            print(f"  [cross-clause] {col!r}: {len(valued)} {word}s from one list "
                  f"({all_vals}) — enforcing {_cc_fmt_num(keep_val)}, "
                  f"{len(valued) - 1} sent to review")
    return downgraded


def _consolidate_cross_clause_column_rules(rules, output_schema):
    """Reconcile rules from DIFFERENT clauses that constrain the SAME column.

    A reinsurance contract is often a BUNDLE — several agreements and programme
    schedules in one PDF. Each names its own authorised carriers and its own
    inception date, and each becomes its own clause and its own rule. Enforced
    together against one bordereau they do not narrow the data, they CONTRADICT:

      * two `value_in_set` rules on Carrier Name, one allowing the Transverse
        companies and one allowing Palms, mean "must be in A" AND "must be in B"
        — the INTERSECTION, which is empty, so every row fails both rules. On
        contract 811 that was 1453 + 1453 flagged rows and two near-identical
        exception cards for one real requirement.
      * three `date_bound` rules on Policy Effective Date (>= Jan 1, >= Feb 1,
        >= Apr 1) each flag whichever policies predate their own section.

    In both cases ONE rule stays enforced and the rest are PAUSED
    (rule_status='needs_review') with the reason recorded — never merged into a
    new rule, and never dropped:
      value_in_set → only when the allow-lists are pairwise DISJOINT (overlapping
        lists are refinements of one requirement, which the per-clause passes
        already own). The widest list stays enforced.
      date_bound   → the MOST PERMISSIVE bound stays enforced, so no policy is
        flagged by a section that does not govern it.

    Why pause rather than union the allow-lists: a union asserts something no
    clause says. On contract 418 twelve "Approved Reinsurer: X" rules are bound
    to the writing-company column alongside the real "Authorized Writing
    Companies" rule; unioning them would silently declare every reinsurer an
    acceptable writing company and the mis-binding would never be found. Pausing
    collapses the duplicate cards just as well, states the contradiction in the
    rule's own description, and leaves the defect visible.

    Nothing is routed to review_queue: that persists a routing row with no rule,
    no SQL and no rule_id, which would throw the paused rules away entirely.
    Paused rules are skipped by the engine but still listed and re-enableable.

    SAFETY GATES — this is the only pass that merges across clauses, and the
    per-clause passes above rely on that never happening carelessly:
      * identical row scope (and sheet scope) only. Two per-schedule allow-lists
        that are already scoped to different schedules are complementary, not
        contradictory — widening them into one global list would be a bug.
      * >= 2 rules on the column, same template, non-referral only.
      * date_bound consolidation additionally requires identical ORDINAL ops.
      * numeric bounds are the one family keyed ACROSS templates, and carry their
        own gates (same direction, same subject, one list) — see
        _cc_resolve_competing_bounds.
    Mutates survivors in place, returns the list with nothing removed (losers are
    downgraded, never dropped).
    """
    by_col, by_bound = {}, {}
    for i, r in enumerate(rules):
        # verify_and_build_ir_rule writes the flag as `is_referral` at the top
        # level and as rule_spec['referral'] — there is no top-level 'referral'
        # key, so guarding on one would silently never fire and referral triggers
        # would be consolidated like compliance rules. Both homes are checked.
        if not isinstance(r, dict) or _cc_is_referral(r):
            continue
        ir = r.get("ir") or {}
        tmpl = ir.get("template")
        params = ir.get("params") or {}
        field = params.get("field")
        if not field:
            continue
        col_key = str(field).strip().lower()
        # Competing numeric ceilings / floors on one column — their own family,
        # keyed ACROSS templates and by what the bound measures (see below).
        bound = _cc_numeric_bound(tmpl, params)
        if bound is not None:
            by_bound.setdefault(
                (col_key, _cc_scope_key(params), bound[0],
                 _cc_bound_subject(tmpl, params)), []).append(i)
        if tmpl not in ("value_in_set", "date_bound", "range_check"):
            continue
        # Only the FIXED-value shape of range_check takes part: two exact values
        # exclude each other, two ranges do not.
        if tmpl == "range_check" and _cc_fixed_value(params) is None:
            continue
        key = (tmpl, col_key, _cc_scope_key(params))
        by_col.setdefault(key, []).append(i)

    downgraded = _cc_resolve_competing_bounds(rules, by_bound)
    merged_lists = 0
    for (tmpl, field, _scope), idxs in by_col.items():
        if len(idxs) < 2:
            continue
        # Only ACROSS clauses — several rules from one clause are already the
        # per-clause passes' business.
        clause_ids = {rules[i].get("source_clause_id") for i in idxs}
        if len(clause_ids) < 2:
            continue

        if tmpl == "value_in_set":
            # ONE enumerated list that extraction deliberately SPLIT into a clause
            # per item (see prompt_builder: each split clause re-quotes the list's
            # lead-in, then its own item). Those siblings are one requirement —
            # "the reinsurer must be one of these twelve" — and enforcing them
            # separately makes eleven of the twelve fire on every row.
            #
            # Siblings are recognised by the shared lead-in they all quote, which
            # is read from the clause text itself; nothing about the wording is
            # known in advance. Clauses that do NOT share a lead-in are different
            # statements and are never merged — on contract 418 that keeps the
            # twelve "Approved Reinsurer" clauses apart from the "Writing
            # Companies" clause bound to the same column.
            for group in _cc_group_by_lead_in(rules, idxs):
                if len(group) < 2:
                    continue
                if len({rules[i].get("source_clause_id") for i in group}) < 2:
                    continue
                survivor = rules[group[0]]
                s_ir = survivor.get("ir") or {}
                s_params = dict(s_ir.get("params") or {})
                col = s_params.get("field") or field
                allowed = list(s_params.get("allowed") or [])
                variations = list(s_params.get("variation_values") or [])
                seen_a = {str(v).strip().lower() for v in allowed}
                seen_v = {str(v).strip().lower() for v in variations}
                for i in group[1:]:
                    p = (rules[i].get("ir") or {}).get("params") or {}
                    for v in (p.get("allowed") or []):
                        if str(v).strip() and str(v).strip().lower() not in seen_a:
                            seen_a.add(str(v).strip().lower())
                            allowed.append(v)
                    # variation_values feed the SAME compiled match list as
                    # `allowed`; carrying only `allowed` across would make the
                    # merged rule flag rows its own siblings used to pass.
                    for v in (p.get("variation_values") or []):
                        if str(v).strip() and str(v).strip().lower() not in seen_v:
                            seen_v.add(str(v).strip().lower())
                            variations.append(v)
                if len(allowed) <= len(s_params.get("allowed") or []):
                    continue
                s_params["allowed"] = allowed
                if variations:
                    s_params["variation_values"] = variations
                new_ir = dict(s_ir)
                new_ir["params"] = s_params
                if not _cc_recompile(survivor, new_ir, output_schema):
                    continue
                for i in group[1:]:
                    _cc_downgrade(
                        rules[i], "superseded",
                        f"This clause is one item of a single list in the "
                        f"contract; every item is now checked by "
                        f"{survivor.get('rule_name')!r}, which accepts all "
                        f"{len(allowed)} permitted {col} values. Enforced one "
                        f"item at a time, each rule rejected the other items.")
                    downgraded += 1
                merged_lists += 1
                print(f"  [cross-clause] {col!r}: {len(group)} clauses quoting one "
                      f"list lead-in merged into {survivor.get('rule_name')!r} "
                      f"— {len(allowed)} permitted value(s)")
            continue

        if tmpl == "range_check":
            # Two clauses pinning ONE column to two different exact values. A cell
            # holds one value, so each rule rejects precisely the rows the other
            # accepts and every row is flagged by one of them — on the Brit
            # fronting contract "OrderPct must be exactly 45.45" (the treaty
            # detail) ran alongside "OrderPct must be exactly 25.0005" (the order
            # hereon, multiplied out), and the reviewer was shown two cards with
            # contradictory recommended values for the same cell.
            #
            # One reading stays enforced, the rest are paused with the conflict
            # spelled out — the same stance the two branches above take, and for
            # the same reason: merging would assert a value no clause states, and
            # dropping would hide the mis-binding that caused it.
            col = (((rules[idxs[0]].get("ir") or {}).get("params") or {})
                   .get("field") or field)
            valued = []
            for i in idxs:
                v = _cc_fixed_value((rules[i].get("ir") or {}).get("params") or {})
                if v is not None:
                    valued.append((v, i))
            if len({v for v, _ in valued} ) < 2:
                continue                        # same value restated — no conflict
            # Quoted values beat computed ones; between equals, the earliest
            # statement in the document wins.
            # Evidence ladder, weakest reading demoted first:
            #   1. a percentage that only qualifies a money amount is not a value
            #      any column must equal;
            #   2. a figure the clause STATES beats one worked out from it;
            #   3. equally-evidenced readings — the first statement in the
            #      document is the operative one.
            keep_val, keep_i = min(valued, key=lambda vi: (
                _cc_value_is_amount_basis(rules, vi[1], vi[0]),
                not _cc_value_is_verbatim(rules, vi[1], vi[0]),
                _cc_document_order(rules, vi[1])))
            all_vals = ", ".join(_cc_fmt_num(v)
                                 for v in sorted({v for v, _ in valued}))
            if any(_cc_value_is_amount_basis(rules, i, v)
                   for v, i in valued if i != keep_i):
                why_kept = ("the other figure only states the basis of a money "
                            "amount, not a value this column must equal")
            elif _cc_value_is_verbatim(rules, keep_i, keep_val):
                why_kept = "its clause states that figure outright"
            else:
                why_kept = "its clause is the first statement of the term"
            for v, i in valued:
                if i == keep_i:
                    continue
                _cc_downgrade(
                    rules[i], "needs_review",
                    f"This contract states more than one exact value for {col} "
                    f"({all_vals}), and a cell can only hold one — enforced "
                    f"together, every row fails one of them. "
                    f"{_cc_fmt_num(keep_val)} is enforced because {why_kept}; "
                    f"confirm which section governs this bordereau column and "
                    f"re-enable the right value.")
                downgraded += 1
            print(f"  [cross-clause] {col!r}: {len(valued)} exact values from "
                  f"{len(clause_ids)} clauses ({all_vals}) — enforcing "
                  f"{_cc_fmt_num(keep_val)}, {len(valued) - 1} sent to review")
            continue

        # date_bound — keep the weakest bound enforced, flag the rest for review.
        col = (((rules[idxs[0]].get("ir") or {}).get("params") or {})
               .get("field") or field)
        ops = {((rules[i].get("ir") or {}).get("params") or {}).get("op")
               for i in idxs}
        if len(ops) != 1:
            continue
        op = ops.pop()
        pick = _CC_DATE_OPS.get(op)
        if pick is None:
            continue                           # "=" / "!=" have no permissive end
        dated = [(str(((rules[i].get("ir") or {}).get("params") or {}).get("date")), i)
                 for i in idxs]
        # min()/max() here order the raw strings, which is only equivalent to
        # ordering the dates while every value is ISO 'YYYY-MM-DD'. A single
        # '01/04/2026' would sort by its leading digits and invert the choice of
        # the most permissive bound, so anything else is left alone.
        if not all(_CC_ISO_DATE.match(d) for d, _ in dated):
            continue
        keep_date = pick(d for d, _ in dated)
        if len({d for d, _ in dated}) < 2:
            continue                           # same bound restated — not a conflict
        all_dates = ", ".join(sorted({dd for dd, _ in dated}))
        for d, i in dated:
            if d == keep_date:
                continue
            _cc_downgrade(
                rules[i], "needs_review",
                f"This contract sets more than one '{op}' bound on {col} "
                f"({all_dates}) — they come from different sections and cannot "
                f"all apply to the same policy. {keep_date} is enforced because "
                f"it accepts every section's policies; confirm which section "
                f"governs this bordereau and re-enable the right bound.")
            downgraded += 1
        print(f"  [cross-clause] {col!r}: {len(idxs)} '{op}' date bounds from "
              f"{len(clause_ids)} clauses — enforcing {keep_date}, "
              f"{len(idxs) - 1} sent to review")

    if merged_lists or downgraded:
        print(f"[Pipeline 2.5-IR] cross-clause consolidation: "
              f"{merged_lists} split list(s) merged, "
              f"{downgraded} rule(s) no longer enforced separately")
    return rules


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

    # Ask for EVERY enum rule's missing spellings in ONE call, up front, instead of
    # one call per rule inside the loop below (13-14 on a typical contract). Only
    # the network call moves — each rule still consumes its answer at exactly the
    # point it does today (step 3b-a), because the ordering there is load-bearing:
    # the top-up must land between the deterministic seeder and the vocabulary /
    # abbreviation seeding that follows. Returns None if the batch fails, in which
    # case every rule falls back to its own call — i.e. the previous behaviour.
    try:
        from contract_upload_services.variation_topup import prefill_variation_topups
        _variation_memo = prefill_variation_topups(synth_outputs)
    except Exception as _exc:
        print(f"[VARIATION-TOPUP] prefill skipped: {_exc}")
        _variation_memo = None

    # Semantic alias merge — the final MEANING layer over the mechanical ones
    # OutputSchema built (canonical concept, normalized name, shared id
    # values). The Call-3 mapping call ALREADY judged, per bound field, which
    # column on each differently-spelled sheet means the same thing (its
    # per-result "field_aliases" — no separate LLM call). Harvest those
    # proposals here and merge them behind the same deterministic guards the
    # mechanical layers use. Runs BEFORE the loop so every compile in this
    # pass (and every recompile downstream) fans out with the full map.
    try:
        from contract_upload_services.alias_llm import merge_field_aliases
        _proposals: dict = {}
        for _e in synth_outputs:
            for _ir in (_e.get("candidates") or []):
                if not isinstance(_ir, dict):
                    continue
                _al = _ir.get("field_aliases")
                if isinstance(_al, dict):        # legacy {sheet: column} shape
                    _names = [v for v in _al.values() if v]
                elif isinstance(_al, (list, tuple)):
                    _names = [v for v in _al if v]
                else:
                    continue
                if not _names:
                    continue
                _p = _ir.get("params") or {}
                _f = _p.get("field") or _p.get("result_field")
                if not _f:
                    continue
                _bucket = _proposals.setdefault(str(_f), [])
                for _n in _names:
                    if str(_n) not in _bucket:
                        _bucket.append(str(_n))
        if _proposals:
            merge_field_aliases(output_schema, _proposals,
                                label="Pipeline 2.5-AliasMerge")
    except Exception as _exc:  # noqa: BLE001 — advisory; rules must still build
        print(f"[Pipeline 2.5-AliasMerge] skipped ({_exc})")

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
                    group_members=group_members, variation_memo=_variation_memo,
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
    # Two provenance gates (see each function): a binding that contradicts
    # its own mapping reason, and a referral whose trigger set traces to
    # nothing in its clause. Both PAUSE the rule (needs_review) — never
    # guess a correction.
    validation_rules = _gate_reason_binding_disagreement(validation_rules, output_schema)
    validation_rules = _gate_untraceable_referral_flagsets(validation_rules)

    # One clause, one requirement: drop the carve-out restated as a prohibition
    # and the duplicate emitted in referral form. Runs after the polarity fix so
    # both copies are already the right way round before they are compared.
    validation_rules = _consolidate_clause_duplicates(validation_rules)
    # Bidirectional routing backstop: for a "use E except state S" mandate on a
    # field with >=2 authorised values, synthesize the carve-out prohibition
    # (S must NOT be E) the model was told not to emit. Runs after consolidation
    # so it sees the deduped requirement and any model-emitted B.
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
    # Signature by MEANING, not bytes: an IR rule's identity is its template +
    # canonical params (variation_values stripped, scope encodings unified —
    # see _dedup_canonical_params). Compiled SQL differs between two copies of
    # one requirement whenever their variation spellings differ, so SQL is the
    # signature only for rules with no IR. When duplicates collide, the copy
    # with the RICHER params (more variation spellings) survives, so dedup
    # never costs spelling tolerance.
    sig_index, deduped, dropped_dupes = {}, [], 0
    for r in validation_rules:
        _ir = r.get("ir") or {}
        if _ir.get("template"):
            sig = ("ir", _ir.get("template"), json.dumps(
                _dedup_canonical_params(_ir.get("params")),
                sort_keys=True, default=str))
        elif r.get("compiled_sql"):
            sig = ("sql", re.sub(r"\s+", " ", r["compiled_sql"]).strip().lower())
        else:
            sig = ("obj", id(r))
        if sig in sig_index:
            dropped_dupes += 1
            kept_i = sig_index[sig]
            kept_p = json.dumps(((deduped[kept_i].get("ir") or {}).get("params")) or {},
                                default=str)
            new_p = json.dumps((_ir.get("params")) or {}, default=str)
            if len(new_p) > len(kept_p):
                deduped[kept_i] = r          # richer variations win
            continue
        sig_index[sig] = len(deduped)
        deduped.append(r)
    validation_rules = deduped
    if dropped_dupes:
        print(f"[Pipeline 2.5-IR] dropped {dropped_dupes} duplicate rule(s) "
              f"(same requirement — identical canonical IR / SQL)")

    # One identity, one rule. The dedup above only catches BYTE-identical rules;
    # an arithmetic identity solved for a different column is a different rule
    # that flags exactly the same rows (see the helper's docstring).
    validation_rules = _consolidate_equivalent_formula_rules(validation_rules)

    # Territory carve-out: keep only the exclusion when a state column carries both
    # an inclusion and an exclusion from the same clause (see helper).
    validation_rules = _drop_paired_state_inclusions(validation_rules)

    # Cross-clause contradictions on ONE column. Everything above is keyed on
    # source_clause_id; this pass is the only one that looks ACROSS clauses, for
    # the case a multi-programme contract bundle creates (see the docstring).
    validation_rules = _consolidate_cross_clause_column_rules(
        validation_rules, output_schema)

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
