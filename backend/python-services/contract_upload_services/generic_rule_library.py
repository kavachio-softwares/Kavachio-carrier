"""Generic (contract-INDEPENDENT) validation rule library.

`generic_rule_specification` holds Kavachio's standard BDX checks — the ones that
are true for every program regardless of what the contract says (policy number
present, zip format valid, paid <= incurred, …). They are NOT extracted from a
contract, so they never pass through Call 1 (clause extraction) or Call 2 (intent
extraction): the stored row IS the intent.

They enter the pipeline the same way the `derive_*` helpers in
validation_rule_generator do — as extra `synth_outputs` entries appended after
Call 3 and before the verify gate — so everything downstream treats them
identically: verification, compilation, review-queue routing, rule-level dedup
against contract rules, and display.

The one thing that is NOT deterministic is the COLUMN. These rules are GENERIC:
"the insured's postal code must be valid" is a CONCEPT, and every program's Output
Template names that concept differently ("Insured Postal Code", "Insured Zip",
"Risk Postcode"). The table therefore stores no column name at all — binding the
concept to a real column is exactly what the Call-3 mapper already does (it is
instructed to bind by MEANING and value kind, never by a lexical name match), so
we reuse it unchanged.

Five columns carry a rule: `class_name` decides the operator (via _INTENT_BY_CLASS
below — an unrecognised class generates NOTHING), `rule_name` is the concept the
mapper binds, `validation_logic` is the logic it reads, `severity` grades it, and
`id` gives it a stable handle. `is_generic` is the on/off switch.

Entry point: derive_generic_library_entries(synth_outputs, template_fields).
"""
from __future__ import annotations

import re
from copy import deepcopy

# Deterministic regex for the PolicyTypePattern rule. Policy/coverage type must
# START WITH "Primary" or "Excess" (case-insensitive, word boundary), so
# "Primary Casualty", "Excess Casualty", "Primary Property", "Excess Property"
# all pass while "Umbrella"/"Excessive" fail. RE2 syntax (DuckDB regexp_matches).
# Forced in code (see _force_policy_type_pattern) rather than left to the mapper:
# "Primary or Excess" reads like an enum, so the mapper otherwise emits a strict
# value_in_set that fuzzy-flags every compound value ("Excess Casualty" ~0.89).
_POLICY_TYPE_PATTERN = r"(?i)^(primary|excess)\b"

# Severity vocabulary differs between the library (business wording:
# Critical/Major/Minor) and the intent schema the mapper consumes
# (critical/warning/info).
#
# The library deliberately uses only TWO of the three downstream tiers: every
# baseline check that the business graded Critical OR Major is a hard failure,
# and Minor drops to warning. Nothing here emits 'info' — a standard BDX check is
# either worth fixing or worth flagging, never merely noted. (Contract-derived
# rules still use all three; the LLM grades those per clause.)
_SEVERITY = {"Critical": "critical", "Major": "critical", "Minor": "warning"}

# class_name → (intent operator, intent value).
#
# `operator` MUST be one of prompt_builder._INTENT_OPERATORS — the mapper rejects
# anything else. `value` is only set where the library row carries a concrete
# literal; otherwise it stays None and the mapper works from rule_description
# (e.g. the zip/NAICS/SIC patterns, which are stated in prose in validation_logic).
#
# Kept in CODE rather than as columns on generic_rule_specification so the table
# stays the plain business-facing catalogue it was created as.
_INTENT_BY_CLASS = {
    "NotNull":                          ("required",             None),
    # "0" and "-" are placeholders a person types into a cell they can't fill,
    # NOT values. They pass every presence check (a non-empty string), so only a
    # not_in_set can reject them. Applies to every rule of this class — the
    # Program Administrator rule wants the same treatment.
    "NoNullOrUnknown":                  ("not_in_set",           ["", "Unknown", "N/A", "NA", "None",
                                                                 "0", "-", "--", "TBD"]),
    "PatternCheck":                     ("pattern",              None),
    "ZipCodeFormat":                    ("pattern",              None),
    "StateCode":                        ("in_set",               None),
    "CurrencyCode":                     ("in_set",               None),
    "AccidentDate":                     ("date_bound",           None),
    "ReportedDate":                     ("date_relation",        None),
    "ClosedDate":                       ("date_relation",        None),
    "TransactionEffectiveDate":         ("date_relation",        None),
    "PolicyPeriod":                     ("duration_range",       None),
    # "The effective date must not CHANGE across a policy's transactions" is an
    # INVARIANT (one distinct value per policy), NOT uniqueness. A bordereau lists
    # many rows per policy — endorsements, instalments, unearned-premium movements
    # — that legitimately repeat the same policy number and the same effective
    # date, so a `unique` rule flags every ordinary multi-transaction policy and
    # never finds the real defect. `invariant` carries the grouping entity in its
    # value ("per"), which the mapper binds to the policy-identifier column.
    "PolicyEffectiveDateChanged":       ("invariant",
                                         {"per": "the policy (its policy number / "
                                                 "identifier)"}),
    "PolicyTypeAndExcessOf":            ("conditional_required", None),
    "NoEndorsementWithoutPolicyPremium": ("conditional_required", None),
    # A cancellation, rejection, return/reversal, reinstatement, or endorsement
    # legitimately carries a NEGATIVE premium (it offsets/adjusts a prior
    # transaction). Those five kinds are the ONLY exemption — every other
    # transaction type (new business, renewal, audit, instalment, rewrite,
    # anything else) must have premium >= 0. Scoped the same way as
    # PolicyTypePattern below: a plain-language row filter the mapper binds to
    # this program's own transaction-type column and its real values, so it
    # never depends on a hard-coded column/value name.
    "TotalPremiumNotNegative":          ("min",                  0,
                                         "excluding ONLY rows whose transaction "
                                         "type is a cancellation, rejection, "
                                         "return/reversal, reinstatement, or "
                                         "endorsement of a prior transaction — "
                                         "those legitimately carry a negative "
                                         "premium; every OTHER row, whatever its "
                                         "transaction type, must have premium "
                                         ">= 0"),
    "NegativeLossBuckets":              ("min",                  0),
    "ClaimOverpayment":                 ("cross_field_compare",  None),
    # "No outstanding reserve ONCE THE CLAIM IS CLOSED" — the closed condition is
    # what the check IS, not a detail of it: an OPEN claim is SUPPOSED to carry a
    # reserve, so the same comparison run over every row flags the whole open
    # inventory. The condition was left for the mapper to notice in the rule's
    # prose and it usually did; stating it as a scope makes it deterministic,
    # the same way TotalPremiumNotNegative states its own carve-out. Plain
    # language, so the mapper binds it to whatever column and value THIS
    # programme uses for claim status.
    "OutstandingOnClosed":              ("cross_field_compare",  None,
                                         "only claims whose status is closed "
                                         "(an open claim is expected to still "
                                         "carry a reserve)"),
    "CededPremiumAmount":               ("cross_field_math",     None),
    "NetPremium":                       ("cross_field_math",     None),
    # Participation / share logic (own limit = layer-share × layer limit; and the
    # layer/MGA share ordering). Field names in the source were program-specific
    # (palms_*); the stored rule_name/validation_logic are reworded to the generic
    # concept so the mapper binds them for every program, not only Palms.
    "PalmsLimit":                       ("cross_field_math",     None),
    "PalmsPctShare":                    ("cross_field_compare",  None),
    # Policy type must START WITH "Primary" or "Excess" — a PREFIX match, not an
    # exact enum. Real BDX values carry a line-of-business qualifier
    # ("Primary Casualty", "Excess Casualty", "Primary Property"), so an exact
    # in_set(["Primary","Excess"]) would wrongly reject every one of them. Uses
    # `pattern` (regex described in validation_logic, like the zip/NAICS rules)
    # and a scope so it applies to property-insurance rows only. Purpose-specific
    # class, so the scope lives with it.
    "PolicyTypePattern":                ("pattern", None,
                                         "only property-insurance policies "
                                         "(line of business / product is property)"),

    # --- Generic building blocks (what users pick when authoring a rule) -------
    # These are the reusable, operator-level types offered in the create/edit
    # dropdown. The specific names above are kept so the seeded/global library
    # rules still validate and generate, but new user rules use these generic
    # classes. Each maps to the same operator as its specific cousins.
    "Required":            ("required",             None),
    "Pattern":             ("pattern",              None),
    "ValueInSet":          ("in_set",               None),
    "NonNegative":         ("min",                  0),
    "CompareFields":       ("cross_field_compare",  None),
    "FieldFormula":        ("cross_field_math",     None),
    "Unique":              ("unique",               None),
    "MustNotChange":       ("invariant",            None),
    "ConditionalRequired": ("conditional_required", None),
    "DateCheck":           ("date_relation",        None),
}

# Human-facing catalogue for the rule-management UI. A rule only produces a real
# check if its class_name is a key of _INTENT_BY_CLASS (anything else generates
# NOTHING), so the create/edit form offers exactly these choices — no free-form
# class_name, no silent no-op rules. Labels are the dropdown text; `operator` is
# informational. Kept here so the UI and the generator share one source of truth.
_CLASS_LABELS = {
    "NotNull":                           "Required — value must be present",
    "NoNullOrUnknown":                   "Not null / not 'Unknown' / 'N/A'",
    "PatternCheck":                      "Must match a text pattern",
    "ZipCodeFormat":                     "Valid ZIP / postal code format",
    "StateCode":                         "Valid state / province code",
    "CurrencyCode":                      "Valid ISO currency code",
    "AccidentDate":                      "Accident date within bounds",
    "ReportedDate":                      "Reported date vs another date",
    "ClosedDate":                        "Closed date vs another date",
    "TransactionEffectiveDate":          "Transaction effective date relation",
    "PolicyPeriod":                      "Policy period duration in range",
    "PolicyEffectiveDateChanged":        "Policy effective date must not change "
                                         "across a policy's rows",
    "PolicyTypeAndExcessOf":             "Conditionally required (policy type / excess-of)",
    "NoEndorsementWithoutPolicyPremium": "Endorsement requires a policy premium",
    "TotalPremiumNotNegative":           "Total premium must not be negative",
    "NegativeLossBuckets":               "Loss buckets must not be negative",
    "ClaimOverpayment":                  "Claim overpayment (paid ≤ incurred)",
    "OutstandingOnClosed":               "No outstanding amount on a closed claim",
    "CededPremiumAmount":                "Ceded premium equation",
    "NetPremium":                        "Net premium equation",
}

# The GENERIC building blocks offered in the create/edit dropdown — reusable,
# operator-level rule types (not the specific named library checks above). Each
# has a plain-English label and a one-line hint so a non-technical user can pick
# the right one. `class_name` is what gets stored; it is a key of _INTENT_BY_CLASS.
_GENERIC_BLOCKS = [
    {"class_name": "Required",            "label": "Required — must have a value",
     "hint": "The field can't be empty."},
    {"class_name": "NoNullOrUnknown",     "label": "Not empty / not 'Unknown'",
     "hint": "Rejects blank, 'Unknown', 'N/A', etc."},
    {"class_name": "Pattern",             "label": "Text pattern",
     "hint": "Value must match a format you describe (e.g. a 5-digit ZIP)."},
    {"class_name": "ValueInSet",          "label": "Value from an allowed list",
     "hint": "Value must be one of a set (e.g. valid state codes)."},
    {"class_name": "NonNegative",         "label": "Number can't be negative",
     "hint": "The amount must be 0 or more."},
    {"class_name": "CompareFields",       "label": "Compare two fields",
     "hint": "One field vs another (e.g. Paid must be ≤ Incurred)."},
    {"class_name": "FieldFormula",        "label": "Field formula",
     "hint": "A field must equal a formula (e.g. Net = Gross − Commission)."},
    {"class_name": "Unique",              "label": "Must be unique",
     "hint": "No duplicate values across rows."},
    {"class_name": "MustNotChange",       "label": "Must not change within a policy",
     "hint": "The value has to stay the same on every row of the same policy "
             "(e.g. the effective date across its transactions)."},
    {"class_name": "ConditionalRequired", "label": "Required only in some cases",
     "hint": "Required when another field has a certain value."},
    {"class_name": "DateCheck",           "label": "Date check",
     "hint": "A date compared to another date (e.g. Reported ≥ Accident)."},
]

# Friendly label for ANY class_name — generic block or specific/legacy — used to
# display existing rules (the seeded global library uses the specific names).
_ALL_LABELS = {**_CLASS_LABELS, **{b["class_name"]: b["label"] for b in _GENERIC_BLOCKS}}

# Severities the UI offers (business wording the library uses; see _SEVERITY).
SUPPORTED_SEVERITIES = ["Critical", "Major", "Minor"]


def supported_classes():
    """The rule-type catalogue for the create/edit dropdown — the GENERIC
    building blocks, each with a label and a one-line hint. `operator` is
    included for reference."""
    return [
        {"class_name": b["class_name"], "label": b["label"],
         "hint": b["hint"], "operator": _INTENT_BY_CLASS[b["class_name"]][0]}
        for b in _GENERIC_BLOCKS
    ]


def label_for_class(class_name):
    """Friendly display label for a stored class_name (generic or legacy)."""
    return _ALL_LABELS.get(class_name, class_name)


def is_supported_class(class_name):
    return class_name in _INTENT_BY_CLASS


def load_generic_rules(tenant_id=None):
    """Active rule-library rows in scope for this upload, schema-agnostic read.

    Loads GLOBAL rules (tenant_id IS NULL — Kavachio's platform baseline that
    applies to every tenant) PLUS, when tenant_id is given, that tenant's OWN
    active rules. A tenant never sees another tenant's rules. Only is_active
    rows are returned so a disabled rule stops firing without being deleted.

    Returns [] (and prints, rather than raising) when the table is absent, so a
    deployment that has not run the migration still generates contract rules.
    """
    from sqlalchemy.sql import text
    from db import canonical_engine

    try:
        with canonical_engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, rule_name, severity, class_name, validation_logic, tenant_id
                FROM generic_rule_specification
                WHERE is_generic = TRUE
                  AND is_active = TRUE
                  AND (tenant_id IS NULL OR tenant_id = :tid)
                ORDER BY tenant_id NULLS FIRST, id
            """), {"tid": tenant_id}).mappings().all()
    except Exception as exc:
        print(f"[Generic] generic_rule_specification unavailable ({exc}); "
              f"skipping the generic rule library.")
        return []
    return [dict(r) for r in rows]


def _build_intents(rules):
    """Shape library rows as Call-3 input: (clauses, intent_clfs), index-aligned.

    ONE synthetic clause per rule (not one clause carrying 41 intents) so an
    unmappable rule lands in the review queue pointing at a SPECIFIC library rule
    rather than an anonymous batch. clause_id is the NEGATED library id, which can
    never collide with a real extracted clause_id.
    """
    clauses, intent_clfs, skipped = [], [], []
    for r in rules:
        entry = _INTENT_BY_CLASS.get(r["class_name"])
        if not entry:
            # An unknown class has no operator we can hand the mapper. Surface it
            # instead of silently generating nothing.
            skipped.append(r["rule_name"])
            continue
        # entry is (operator, value) or (operator, value, scope). Scope is a
        # plain-language row filter the mapper binds to a column (e.g. apply the
        # rule only to property-insurance policies); most rules have none.
        operator, value = entry[0], entry[1]
        scope = entry[2] if len(entry) > 2 else None
        clause_id = -int(r["id"])
        clauses.append({
            "clause_id":   clause_id,
            "title":       r["rule_name"],
            "text":        f"[Generic rule] {r['rule_name']} — {r['validation_logic']}",
            "clause_type": "generic_library",
            "page_number": None,
            "generic_rule_id": r["id"],
        })
        intent_clfs.append({
            "clause_id":       clause_id,
            "is_rule_bearing": True,
            "reasoning":       "Kavachio standard BDX rule library (not contract-derived).",
            "intents": [{
                # The CONCEPT, never a column name. These rules are generic: "the
                # insured's postal code must be valid" is the rule; whatever one
                # source system called that column is an artifact of that system.
                # The mapper is instructed to bind by MEANING against THIS program's
                # columns, so it gets the concept (rule_name) plus the full logic
                # (rule_description, which carries the equation for cross-field
                # rules) and nothing that would bias it toward a lexical match.
                "subject":          r["rule_name"],
                "operator":         operator,
                "value":            value,
                "scope":            scope,
                "severity":         _SEVERITY.get(r["severity"], "warning"),
                "is_referral":      False,
                "rule_name":        r["rule_name"],
                "rule_description": r["validation_logic"],
                # The message a reviewer reads when the check fires. It used to be
                # "<rule name>: check failed.", which says nothing they did not
                # already know from the rule's title — 1,119 stored rules still
                # carry that placeholder. The library row already states the
                # requirement in business English, so use it.
                "error_message":    r["validation_logic"],
            }],
        })
    if skipped:
        print(f"[Generic] {len(skipped)} library rule(s) have no intent mapping "
              f"for their class_name and were skipped: {skipped}")
    return clauses, intent_clfs


def _existing_template_field_keys(synth_outputs):
    """(template, field) pairs already covered by contract-derived candidates."""
    seen = set()
    for entry in (synth_outputs or []):
        for ir in (entry.get("candidates") or []):
            field = (ir.get("params") or {}).get("field")
            if ir.get("template") and field:
                seen.add((ir["template"], str(field).strip().lower()))
    return seen


# The auto-derived data-quality rules ("[Derived rule] …" entries from
# validation_rule_generator) and the generic library overlap by construction:
# both emit a baseline check per column, phrased differently ("Insured Zip Code
# must be a valid postal code for Insured State" vs "Insured ZIP Code Must Be
# Valid"). When both land on the SAME column the reviewer sees two
# near-identical rules. The library rule wins: it is the tenant-editable,
# business-facing catalogue entry.
#
# NOTHING here is template-specific: the derived rules are generated from
# whatever columns the template happens to have, so the set of templates/params
# involved is open-ended. Each rule's VALIDATED column is read from the
# template catalog's own declarative field list (`field_refs` — the first
# field ref IS the validated column; the same `bound_fields[0]` convention
# canonical_target.output_field is built on everywhere else). Any template the
# deriver or the library uses now or later is covered automatically.


def _primary_column(ir) -> str | None:
    """The column a rule validates, normalized — field_refs()[0], the same
    first-field convention the pipeline uses for canonical_target."""
    from contract_upload_services.rule_ir import field_refs
    refs = field_refs(ir)
    return refs[0].strip().lower() if refs and isinstance(refs[0], str) else None


# Params the DERIVER works out from THIS contract and THIS template that the
# library rule cannot carry: a catalogue entry is written once for every tenant,
# so it knows neither which country the contract pins its risks to nor which
# column of this bordereau states the country. When the library rule wins on a
# column, these travel from the derived rule it replaced — otherwise a UK
# contract's state check silently reverts to "valid in any country we hold data
# for" and passes rows the contract forbids.
_DISPATCH_PARAMS = ("countries", "country_field")


def _carry_dispatch_params(derived_ir, generic_irs) -> int:
    """Copy the derived rule's reference-DISPATCH params onto the library rule(s)
    replacing it, for the SAME template only (a different check on the same column
    — a pattern_check on a state code, say — takes no country dispatch, because it
    validates the value's shape rather than looking it up in a country's
    reference). Never overwrites a param the library rule already sets."""
    dparams = (derived_ir.get("params") or {})
    moved = 0
    for gir in generic_irs:
        if gir.get("template") != derived_ir.get("template"):
            continue
        gparams = gir.setdefault("params", {})
        for key in _DISPATCH_PARAMS:
            if key in dparams and key not in gparams:
                gparams[key] = dparams[key]
                moved += 1
    return moved


def drop_derived_duplicates(synth_outputs, generic_entries):
    """Remove auto-derived data-quality candidates whose validated column a
    generic library rule also validates — so only the generic rule ships.

    Mutates `synth_outputs` in place (entries emptied of all candidates are
    removed) and returns the number of derived candidates dropped. Only entries
    whose clause text starts with "[Derived rule]" are eligible — contract
    clauses and "[Derived formula]" arithmetic are never touched.
    """
    generic_by_col = {}
    for entry in (generic_entries or []):
        for ir in (entry.get("candidates") or []):
            col = _primary_column(ir)
            if col:
                generic_by_col.setdefault(col, []).append(ir)
    if not generic_by_col:
        return 0

    dropped = carried = 0
    for entry in list(synth_outputs or []):
        text = ((entry.get("clause") or {}).get("text") or "")
        if not text.startswith("[Derived rule]"):
            continue
        keep = []
        for ir in (entry.get("candidates") or []):
            twins = generic_by_col.get(_primary_column(ir))
            if twins:
                dropped += 1
                carried += _carry_dispatch_params(ir, twins)
                continue
            keep.append(ir)
        if keep:
            entry["candidates"] = keep
        else:
            synth_outputs.remove(entry)
    if dropped:
        print(f"[Generic] dropped {dropped} derived data-quality rule(s) already "
              f"covered by a library rule on the same column (library wins"
              + (f"; carried {carried} reference-dispatch param(s) onto the "
                 f"surviving rule)." if carried else ")."))
    return dropped


# Templates that resolve a value against a COUNTRY's reference vocabulary and can
# say nothing useful without knowing which country that is. `state_validity` is
# the whole list today: with no country its probe is the UNION of every country we
# hold data for, so a perfectly good region of a country we do NOT cover is
# reported as invalid, and the check cannot tell that apart from a typo. The ZIP
# check deliberately is NOT here — its per-country shape guard skips a code that
# belongs to none of them, so it stays valid with no country bound.
_COUNTRY_DEPENDENT_TEMPLATES = ("state_validity",)


def drop_uncountried_reference_rules(entries) -> int:
    """Remove auto-generated candidates that look a value up in a country's
    reference vocabulary without knowing the country — neither the contract nor a
    BDX country column named one, so `_carry_dispatch_params` had nothing to hand
    over either.

    Applied to the LIBRARY entries for the same reason the deriver refuses to emit
    them: the library entry is a baseline data-quality check auto-bound to whatever
    column this bordereau happens to have, so an unanswerable version of it is
    noise, not coverage. Contract-clause rules are not passed here — an explicit
    contract requirement is the reviewer's to keep. Mutates `entries` in place and
    returns the number of candidates removed."""
    removed = 0
    for entry in list(entries or []):
        keep = []
        for ir in (entry.get("candidates") or []):
            params = ir.get("params") or {}
            if (ir.get("template") in _COUNTRY_DEPENDENT_TEMPLATES
                    and not any(params.get(k) for k in _DISPATCH_PARAMS)):
                removed += 1
                continue
            keep.append(ir)
        if keep:
            entry["candidates"] = keep
        else:
            entries.remove(entry)
    if removed:
        print(f"[Generic] dropped {removed} reference-lookup rule(s) with no "
              f"country to validate against (no country in the contract and none "
              f"in the bordereau).")
    return removed


def _force_policy_type_pattern(mapped, rules):
    """Rewrite the PolicyTypePattern rule's IR to a deterministic prefix regex.

    Scoped strictly to rules whose class_name == 'PolicyTypePattern' (matched by
    clause_id → rule); every other rule is left exactly as the mapper produced it.
    Keeps the column the mapper chose (params['field']) and any scope it bound
    (params['scope'] — the property-insurance condition); only the template and
    match expression are overridden:
      template → 'pattern_check'
      params['pattern'] → _POLICY_TYPE_PATTERN
    dropping any 'values'/'value' left over from an enum template. Mutates in place.
    """
    cls_by_clause = {-int(r["id"]): r.get("class_name") for r in rules}
    forced = 0
    for entry in (mapped or []):
        cid = (entry.get("clause") or {}).get("clause_id")
        if cls_by_clause.get(cid) != "PolicyTypePattern":
            continue
        for ir in (entry.get("candidates") or []):
            params = ir.get("params") or {}
            if not params.get("field"):
                continue  # nothing bound to force onto → leave for review
            ir["template"] = "pattern_check"
            params["pattern"] = _POLICY_TYPE_PATTERN
            params.pop("values", None)
            params.pop("value", None)
            ir["params"] = params
            forced += 1
    if forced:
        print(f"[Generic] forced {forced} policy-type rule(s) to a deterministic "
              f"prefix pattern ({_POLICY_TYPE_PATTERN}).")


# Library classes whose check places a date INSIDE the policy period — the
# transaction's own effective date must not fall after the policy expires, and the
# policy period is measured between the policy's own dates. Both are only
# meaningful on dates that take effect ON THE RISK (see
# output_schema.is_processing_date_column). Scoped by class_name, the same
# structural handle _force_policy_type_pattern uses: every other library rule,
# and every tenant-authored rule, is untouched.
_IN_PERIOD_DATE_CLASSES = {"TransactionEffectiveDate", "PolicyPeriod"}

# The params each template uses to name a DATE column, so the guard inspects the
# right keys per template instead of guessing.
_DATE_PARAM_KEYS = ("field", "other_field", "start_field", "end_field")


def _guard_in_period_date_bounds(mapped, rules):
    """Unbind an in-period date rule the mapper hung on a PROCESSING date column.

    A booked / keyed / processed transaction date follows the REPORTING calendar,
    not the risk: a cancellation, audit or reversal is recorded AFTER the policy
    has expired and a renewal is keyed BEFORE it incepts. Bounding such a column
    by the policy period therefore flags ordinary bookkeeping — on one real
    program it flagged 26 cancellations whose only fault was being booked the day
    after the (restated) expiry — while finding no genuine defect.

    Call 3 is told this (see the DATE ROLE guard in
    prompt_builder.build_ir_mapping_prompt_batch); this is the deterministic
    backstop for the run where it picks the processing column anyway. Scoped to
    the library classes that assert an in-period date, matched by the SAME role
    test that validation_rule_generator.fix_backdating_period_fields uses to find
    that column — so the two can never disagree about which column it is.

    The rule is NOT silently dropped: clearing the template routes it to the
    review queue with a reason (verify_and_build_ir_rule), where a human can
    point it at the right column. Mutates `mapped` in place; returns the count.
    """
    from contract_upload_services.output_schema import is_processing_date_column

    cls_by_clause = {-int(r["id"]): r.get("class_name") for r in rules}
    unbound = 0
    for entry in (mapped or []):
        cid = (entry.get("clause") or {}).get("clause_id")
        if cls_by_clause.get(cid) not in _IN_PERIOD_DATE_CLASSES:
            continue
        for ir in (entry.get("candidates") or []):
            if not ir.get("template"):
                continue
            params = ir.get("params") or {}
            hit = next((params[k] for k in _DATE_PARAM_KEYS
                        if isinstance(params.get(k), str)
                        and is_processing_date_column(params[k])), None)
            if not hit:
                continue
            ir["template"] = None
            ir["reason"] = (
                f"{hit!r} reports when the transaction was RECORDED (a booked / "
                f"processed date), not when it took effect on the risk, so it is "
                f"not bounded by the policy period — a cancellation or audit is "
                f"booked after expiry and a renewal before inception. Select the "
                f"column that carries the transaction's EFFECTIVE date."
            )
            unbound += 1
    if unbound:
        print(f"[Generic] unbound {unbound} in-period date rule(s) mapped onto a "
              f"transaction PROCESSING date column → review queue.")
    return unbound


# The exact library rule(s) this guard governs — matched by rule_name, NOT by
# class_name ('NotNull' is shared by ~1,100 rules). Both the terse and the
# reworded titles of the SAME standard check are listed, lower-cased for a
# trim/case-insensitive compare. Every other rule is left exactly as the mapper
# bound it.
_CONTRACT_ID_RULE_NAMES = {
    "contract identifier must not be null",
    "contract not null",
}


def _pin_contract_identifier_to_effective_date(mapped, rules, template_fields):
    """Force the 'Contract Identifier Must Not Be Null' rule onto POLICY EFFECTIVE DATE.

    The rule resolves each policy to its contract BY the policy effective date,
    but its NAME says 'Contract Identifier', and the Call-3 mapper binds the field
    from the intent SUBJECT (= rule_name) — so it lands on the nearest identifier
    column (Program Name / Program Number). We deliberately keep the reviewer-facing
    rule_name unchanged, so this deterministic backstop re-points the BOUND column
    after mapping.

    Scoped by rule_name (never by the shared 'NotNull' class), so no other rule is
    touched. If the template has no policy effective-date column, the rule is routed
    to review (template cleared) rather than guessed — mirroring
    _guard_in_period_date_bounds. Mutates `mapped` in place; returns the count.
    """
    name_by_clause = {
        -int(r["id"]): (r.get("rule_name") or "").strip().lower()
        for r in rules
    }

    import re

    # An effective-date column may be spelled out ("Policy Effective Date") or
    # abbreviated ("Policy Eff Date", "Pol Eff Dt", "pol_eff_dt"). Match on TOKENS
    # (split on spaces / underscores / punctuation) rather than substrings, so an
    # abbreviation is caught while a longer word that merely CONTAINS the letters
    # (e.g. "coefficient", "effort") is not. A column qualifies when it carries an
    # effective-token AND a date-token.
    _EFF_TOKENS = {"effective", "eff", "inception"}
    # "inception" columns usually carry no separate date-token ("PolicyInception"),
    # so it satisfies BOTH the effective- and the date-token requirement.
    _DATE_TOKENS = {"date", "dt", "dte", "inception"}
    _POLICY_TOKENS = {"policy", "pol"}

    def _toks(name: str) -> set:
        # Split camelCase BEFORE lowering — "PolicyInception" is policy+inception,
        # not one opaque token. Without this, a template whose policy date column
        # is camelCase has no policy-token match and the FALLBACK picks whatever
        # other effective-date column exists (measured: 'Reins Eff Date', which
        # put a required-field check on the reinsurance date). Same failure
        # family as the camelCase BDX-header fix in the state/zip pipeline.
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
        return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t}

    def _is_effective_date(name: str) -> bool:
        n = (name or "").lower()
        # Never the record-keeping / other-scope / end-of-period date columns.
        if any(bad in n for bad in ("transaction", "program", "reporting",
                                    "booked", "entered", "accounting", "as of",
                                    "as-of", "expir", "termination")):
            return False
        t = _toks(name)
        return bool(t & _EFF_TOKENS) and bool(t & _DATE_TOKENS)

    names = [f.get("name") for f in (template_fields or []) if f.get("name")]
    # Prefer a POLICY effective-date column (has a "policy"/"pol" token); fall
    # back to any effective-date column.
    eff = next((n for n in names
                if _is_effective_date(n) and (_toks(n) & _POLICY_TOKENS)), None)
    if not eff:
        eff = next((n for n in names if _is_effective_date(n)), None)

    pinned = 0
    for entry in (mapped or []):
        cid = (entry.get("clause") or {}).get("clause_id")
        if name_by_clause.get(cid) not in _CONTRACT_ID_RULE_NAMES:
            continue
        for ir in (entry.get("candidates") or []):
            if not ir.get("template"):
                continue        # already unmapped → leave for review, don't fabricate
            params = ir.get("params") or {}
            if eff:
                if params.get("field") != eff:
                    params["field"] = eff
                    ir["params"] = params
                    # The mapper's reason described ITS binding; this is now a
                    # different one. Rewrite it so the rule doesn't contradict
                    # itself (the reason/binding gate pauses self-contradictory
                    # mappings) and the audit trail states what actually happened.
                    ir["reason"] = (
                        f"Deterministically pinned to '{eff}': the check "
                        f"resolves a policy to its contract by the policy "
                        f"effective date (see rule description), regardless of "
                        f"what the rule NAME suggested to the mapper."
                    )
                    pinned += 1
            else:
                ir["template"] = None
                ir["reason"] = (
                    "Resolves a policy to its contract by the policy effective "
                    "date, but this Output Template has no policy effective-date "
                    "column to bind the check to."
                )
                pinned += 1
    if pinned:
        print(f"[Generic] pinned {pinned} 'Contract Identifier Must Not Be Null' "
              f"binding(s) to {eff or 'review (no effective-date column)'}.")
    return pinned


# Library classes whose check compares TWO MEASURES OF ONE AMOUNT — what has been
# PAID against what has been INCURRED on the SAME claim. Both operands therefore
# name the same underlying amount and differ only in the measure, which is the only
# reading under which the comparison means anything.
#
# Deliberately NOT listed: a class that compares two DIFFERENT subjects on purpose
# (PalmsPctShare weighs one party's share against another party's), and every
# tenant-authored building block (CompareFields) — there the author picked BOTH
# columns, so the pair is the rule's stated intent and is never second-guessed.
#
# The value is WHICH of the two measures the class requires to be the smaller one,
# i.e. the side of the `<=`. It is the same kind of per-class reading of a library
# row that _INTENT_BY_CLASS already holds (that one says which OPERATOR the class
# means; this says which way round it points), and it is used only by
# _rebind_unbound_measure_compares, and only when the mapper declined to propose
# anything at all — whenever there IS a proposal, its direction is what survives.
_SAME_SUBJECT_COMPARE_CLASSES = {
    "ClaimOverpayment":    "paid",       # paid ≤ incurred
    "OutstandingOnClosed": "incurred",   # incurred ≤ paid, once the claim is closed
}


def _guard_same_subject_compares(mapped, rules):
    """Unbind a paid-vs-incurred library rule whose two operands do not report the
    SAME amount.

    A bordereau routinely splits claim money into COMPONENT columns — a paid /
    reserve / incurred trio per component (indemnity, medical, defence and other
    expense …) — and adds a rolled-up total beside them. Nothing tells the mapper
    that a column named for ONE component cannot stand in for the total, so it
    binds the closest-looking "paid" column and the check ends up weighing a
    rolled-up total against a single part of it. Every row whose OTHER components
    are non-zero then fails a check it does not breach: on one real programme this
    flagged 8 closed claims whose reserves were all zero, purely because the
    defence-cost part of what had been paid sits in its own column.

    Call 3 is told this (see SAME SUBJECT, SAME LEVEL in
    prompt_builder.build_ir_mapping_prompt_batch); this is the deterministic
    backstop for the run where it binds the mismatched pair anyway — including via
    the RELAXED retry, which exists to push a declined intent onto the closest
    field and would otherwise re-introduce exactly this pair.

    The test is the operands' own NAMES, split with the tokenizer every
    column-role test in the codebase shares: two measures of the same amount always
    keep that amount in BOTH names ("loss_paid"/"loss_incurred",
    "total_paid"/"total_incurred", "PaidLossAmount"/"IncurredLossAmount"), so a
    pair with NOT ONE token in common is reporting two different things. Nothing
    about any programme, carrier or column is named here — the shared subject is
    whatever THIS template's own two columns happen to have in common.

    A SCALED comparison ("A >= P% of B") is skipped: there the two columns are
    meant to be different quantities. The rule is NOT dropped either — clearing the
    template routes it to the review queue with a reason
    (verify_and_build_ir_rule), where a human can point it at the pair this
    programme really reports. Mutates `mapped` in place; returns the count.
    """
    from contract_upload_services.uszips_reference import column_tokens

    cls_by_clause = {-int(r["id"]): r.get("class_name") for r in rules}
    unbound = 0
    for entry in (mapped or []):
        cid = (entry.get("clause") or {}).get("clause_id")
        if cls_by_clause.get(cid) not in _SAME_SUBJECT_COMPARE_CLASSES:
            continue
        for ir in (entry.get("candidates") or []):
            if ir.get("template") != "cross_field_compare":
                continue
            params = ir.get("params") or {}
            left, right = params.get("field"), params.get("other_field")
            if not (isinstance(left, str) and isinstance(right, str)):
                continue
            if params.get("operator") or params.get("factor"):
                continue          # scaled compare — different quantities on purpose
            if set(column_tokens(left)) & set(column_tokens(right)):
                continue          # they name the same amount → keep the binding
            ir["template"] = None
            ir["reason"] = (
                f"{left!r} and {right!r} have no part of their names in common, so "
                f"they do not report the same amount: this check weighs what has "
                f"been PAID against what has been INCURRED on the SAME claim, and a "
                f"total that rolls up several components cannot be compared with one "
                f"component on its own (every row whose other components are "
                f"non-zero would fail without breaching anything). Select the paid "
                f"column and the incurred column that cover the SAME amounts."
            )
            unbound += 1
    if unbound:
        print(f"[Generic] unbound {unbound} paid-vs-incurred rule(s) whose two "
              f"operands do not report the same amount → review queue.")
    return unbound


# The two MEASURES one claim amount is reported in: what has already been PAID out,
# and what has been INCURRED in total. The library's paid-vs-incurred checks are
# ABOUT these two measures, so the words are the rule's own vocabulary — not any
# programme's column names, and nothing here names a column, carrier or MGA.
#
# Matched as whole NAME TOKENS (the tokenizer every column-role test shares), and
# the incurred side by STEM because bordereaux spell it every way there is —
# "incurred", "incured", "incur". A column naming BOTH measures is ambiguous and
# counts as neither.
_PAID_TOKENS = {"paid", "payment", "payments"}
_INCURRED_STEM = "incur"


def _toks(name):
    """The shared column tokenizer, imported where it is used (the reference-table
    module is not pulled in at import time anywhere else in this file either)."""
    from contract_upload_services.uszips_reference import column_tokens
    return column_tokens(name)


def _measure_role(name):
    """'paid' / 'incurred' when a column NAME reports one of the two measures a
    claim amount is stated in, else None. Token-based, so "Paid Loss Amount",
    "loss_paid" and "PaidLossAmount" all read as the paid measure while a word that
    merely CONTAINS one ("Unpaid", "Repayment") does not."""
    toks = set(_toks(name))
    inc = any(t.startswith(_INCURRED_STEM) for t in toks)
    paid = bool(_PAID_TOKENS & toks)
    if inc == paid:
        return None       # neither measure, or both → not a clean operand
    return "incurred" if inc else "paid"


def _measure_subject(name):
    """The AMOUNT a measure column reports: its name tokens MINUS the measure word.
    'med_paid' and 'med_incurred' both reduce to ('med',), and 'PaidLossAmount' /
    'IncurredLossAmount' both to ('amount', 'loss') — which is exactly what makes
    each pair two measures of ONE amount."""
    return tuple(sorted(
        t for t in _toks(name)
        if not (t.startswith(_INCURRED_STEM) or t in _PAID_TOKENS)))


def _reports_an_amount(field):
    """False only when a column HAS sample values and NOT ONE of them is a number —
    a "Paid Date" or a paid-status column carries a measure word but can never be an
    operand of an arithmetic comparison. No samples → unknown → allowed through, the
    same benefit of the doubt every other numeric gate here gives."""
    samples = (field or {}).get("samples") or []
    if not samples:
        return True
    for s in samples:
        try:
            float(re.sub(r"[,$%\s]", "", str(s)))
            return True
        except (TypeError, ValueError):
            continue
    return False


def _measure_pairs(template_fields):
    """Every amount THIS bordereau reports in BOTH measures, keyed by subject:
    {subject: {'paid': column, 'incurred': column}}.

    A subject reported by more than one column of the SAME measure is skipped —
    which of them the check should use is not ours to guess."""
    by_subject = {}
    for f in (template_fields or []):
        name = f.get("name")
        role = _measure_role(name) if name else None
        if not role or not _reports_an_amount(f):
            continue
        by_subject.setdefault(_measure_subject(name), {}) \
                  .setdefault(role, []).append(name)
    return {
        subject: {"paid": roles["paid"][0], "incurred": roles["incurred"][0]}
        for subject, roles in by_subject.items()
        if len(roles.get("paid") or []) == 1 and len(roles.get("incurred") or []) == 1
    }


_OP_WORDS = {"<=": "must not exceed", "<": "must be less than",
             ">=": "must be at least", ">": "must be greater than",
             "=": "must equal", "!=": "must differ from"}


def _rebind_unbound_measure_compares(mapped, rules, template_fields):
    """Re-bind an UNBOUND paid-vs-incurred library rule to the pairs of columns this
    bordereau actually reports — one check per amount — instead of shipping nothing.

    A bordereau that splits claim money per component (indemnity, medical, defence,
    other expense …) reports NO single "total paid" column, so there is no one pair
    for the mapper to bind: it picks the closest-looking paid column, the
    same-subject guard above correctly refuses the mismatched pair, and the standard
    check then disappears from the contract entirely. It should not: the SAME
    requirement holds for every amount the bordereau states in both measures, and
    per component it is both exactly expressible and strictly stronger (each
    component's paid ≤ its incurred implies the totals do too).

    So the columns are re-derived from the OUTPUT TEMPLATE's own structure —
    columns whose names differ only in the measure word report the same amount —
    and one rule is emitted per such pair. Everything ELSE about the rule is
    inherited from the mapper's own proposal: which measure sits on each side, the
    comparison operator, the severity, and any row scope it bound (the closed-claim
    condition, say).

    The mapper's other answer to the same template is to decline outright ("no
    total_paid field to compare against total_incured") — no proposal, nothing to
    inherit. The direction then comes from the library CLASS, which states which
    measure has to be the smaller one (_SAME_SUBJECT_COMPARE_CLASSES), with `<=`.
    A class whose check only holds on a SUBSET of rows is NOT re-bound that way: its
    _INTENT_BY_CLASS entry carries a plain-language scope, and without the mapper
    having bound that scope to a column the rule would run over every row and flag
    the ones it was never about (an OPEN claim is supposed to carry a reserve). Those
    stay in the review queue, where the guard put them.

    Scoped to _SAME_SUBJECT_COMPARE_CLASSES and to candidates that are ALREADY
    unbound, so a rule the mapper bound correctly is never touched. Mutates `mapped`
    in place; returns the number of rules re-bound.
    """
    pairs = _measure_pairs(template_fields)
    if not pairs:
        return 0

    cls_by_clause = {-int(r["id"]): r.get("class_name") for r in rules}
    rebound = 0
    for entry in (mapped or []):
        cid = (entry.get("clause") or {}).get("clause_id")
        cls = cls_by_clause.get(cid)
        if cls not in _SAME_SUBJECT_COMPARE_CLASSES:
            continue
        candidates = []
        for ir in (entry.get("candidates") or []):
            if ir.get("template") is not None:
                candidates.append(ir)
                continue
            params = ir.get("params") or {}
            op = params.get("op")
            left_role = _measure_role(params.get("field"))
            right_role = _measure_role(params.get("other_field"))
            if op not in _OP_WORDS or not left_role or not right_role \
                    or left_role == right_role:
                # No proposal to inherit from. Fall back to the class's own reading
                # — but only where the check holds for EVERY row (see docstring).
                if len(_INTENT_BY_CLASS.get(cls) or ()) > 2:
                    candidates.append(ir)
                    continue
                left_role = _SAME_SUBJECT_COMPARE_CLASSES[cls]
                right_role = "incurred" if left_role == "paid" else "paid"
                op = "<="
                # Only the operands are dropped — anything else the mapper managed
                # to bind (a tolerance, say) is still the mapper's, and is kept.
                params = {k: v for k, v in params.items()
                          if k not in ("field", "other_field")}
            base_name = ir.get("rule_name") or ""
            for _subject, cols in sorted(pairs.items()):
                left, right = cols[left_role], cols[right_role]
                requirement = f"{left} {_OP_WORDS[op]} {right}"
                bound = dict(ir)
                bound.pop("reason", None)
                bound["template"] = "cross_field_compare"
                # deepcopy: a scope the mapper bound (the closed-claim condition) is
                # nested, and each of these rules is normalized/pruned on its own.
                bound["params"] = {**deepcopy(params), "op": op,
                                   "field": left, "other_field": right}
                bound["rule_name"] = f"{base_name} — {left} vs {right}".strip(" —")
                bound["rule_description"] = (
                    f"{ir.get('rule_description') or base_name} "
                    f"Checked on each amount the bordereau reports in both "
                    f"measures: {requirement}.").strip()
                bound["error_message"] = f"{requirement}."
                candidates.append(bound)
                rebound += 1
        entry["candidates"] = candidates
    if rebound:
        print(f"[Generic] re-bound {rebound} paid-vs-incurred check(s) onto the "
              f"{len(pairs)} amount(s) this bordereau reports in both measures "
              f"(no single rolled-up pair to bind).")
    return rebound


def is_generic_entry(entry) -> bool:
    """True when a mapper entry came from the rule library rather than the contract.

    _build_intents keys every library clause to the NEGATED library row id, and a
    real extracted clause_id is always positive, so the sign is an exact
    discriminator — no name matching, no heuristics. Used to pull the library half
    back out of a mapper call that carried both (see build_generic_intents).
    """
    return ((entry.get("clause") or {}).get("clause_id") or 0) < 0


def build_generic_intents(tenant_id=None, template_fields=None):
    """Library rules shaped as Call-3 mapper input — the FIRST half of the bind.

    Split out of derive_generic_library_entries so the library intents can ride the
    SAME map_intents_to_ir call as the contract intents instead of paying for a
    second one. Returns (clauses, intent_clfs, rules), all index-aligned, or three
    empties when the template is unknown or the library is empty/unseeded.

    The caller concatenates these onto the contract intents, makes ONE mapper call,
    splits the result back with is_generic_entry(), and passes the library half to
    finish_generic_entries() at the point in the pipeline where the dedup belongs.
    """
    if template_fields is not None and not template_fields:
        return [], [], []

    rules = load_generic_rules(tenant_id)
    if not rules:
        return [], [], []

    # Tenant rules first so they win the intra-batch dedup against a global rule
    # on the same column (see finish_generic_entries); globals keep their order.
    rules.sort(key=lambda r: (r.get("tenant_id") is None, r["id"]))

    clauses, intent_clfs = _build_intents(rules)
    if not clauses:
        return [], [], []
    return clauses, intent_clfs, rules


def finish_generic_entries(mapped, rules, synth_outputs, template_fields):
    """Deterministic guards + dedup over already-mapped library rules — SECOND half.

    Everything derive_generic_library_entries did AFTER its mapper call, unchanged.
    Kept separate from build_generic_intents because this half is position-sensitive
    in a way the mapper call is not: the dedup below must run AFTER every derived-rule
    injector has populated synth_outputs, or a library rule stops deduping against
    them. Moving the AI call earlier is safe; moving this earlier is not.

    `mapped` must contain library entries only — pass the is_generic_entry() half of
    a merged mapper result, never the whole thing.
    """
    # Deterministic template override — SCOPED to the PolicyTypePattern class only,
    # so no other rule is affected. The mapper still binds the COLUMN (which output
    # field is the policy/coverage type, and the property scope), but the template
    # and regex are forced here so the check can never regress to a strict enum
    # (which fuzzy-flags "Excess Casualty" et al. as a near-miss).
    _force_policy_type_pattern(mapped, rules)

    # Deterministic backstop for the DATE ROLE guard in the Call-3 prompt: an
    # in-period date check bound to a transaction PROCESSING (booked/keyed) date
    # column flags ordinary bookkeeping, so it is unbound and routed to review
    # instead of shipping. Scoped by class_name; no other rule is affected.
    _guard_in_period_date_bounds(mapped, rules)

    # Deterministic re-point for the 'Contract Identifier Must Not Be Null' rule:
    # its check keys on the policy effective date, but its NAME makes the mapper
    # bind it to a program/contract identifier column. Force it onto the policy
    # effective-date column. Scoped by rule_name; no other rule is affected.
    _pin_contract_identifier_to_effective_date(mapped, rules, template_fields)

    # Deterministic backstop for the SAME SUBJECT, SAME LEVEL guard in the Call-3
    # prompt: a paid-vs-incurred check whose two operands report DIFFERENT amounts
    # (a rolled-up total against one of its components) flags every row whose other
    # components are non-zero, so it is unbound and routed to review instead of
    # shipping. Scoped by class_name; no other rule is affected.
    _guard_same_subject_compares(mapped, rules)

    # …and, for the bordereau that has no single rolled-up pair to bind BECAUSE it
    # reports claim money per component, re-bind that same check onto the pairs of
    # columns it DOES report — one check per amount — rather than let a standard
    # check vanish from the contract. Runs on the UNBOUND rules only, so a correct
    # binding (here or from the mapper) is never disturbed. See
    # _rebind_unbound_measure_compares.
    _rebind_unbound_measure_compares(mapped, rules, template_fields)

    already = _existing_template_field_keys(synth_outputs)
    seen_in_batch = set()   # tenant-vs-global precedence within this library batch
    entries, dropped_dupe = [], 0
    for entry in (mapped or []):
        keep = []
        for ir in (entry.get("candidates") or []):
            field = (ir.get("params") or {}).get("field")
            key = (ir.get("template"), str(field).strip().lower() if field else None)
            # An UNMAPPED rule (no template) has no (template, column) identity to
            # be a duplicate OF — every one of them keys to (None, None), so a
            # second unmapped rule looked like a dupe of the first and was dropped
            # instead of reaching the review queue. Dedup only what is bound.
            if ir.get("template"):
                if key in already or key in seen_in_batch:
                    dropped_dupe += 1
                    continue
                seen_in_batch.add(key)
            # Mark provenance on the IR so the review UI and audit trail can tell a
            # library rule from a contract rule. verify_and_build_ir_rule copies the
            # IR onto the persisted row verbatim, so this survives to the DB.
            ir["rule_source"] = "generic_library"
            keep.append(ir)
        if keep:
            entry["candidates"] = keep
            entries.append(entry)

    bound = sum(len(e.get("candidates") or []) for e in entries)
    # len(mapped), not len(clauses): the mapper returns one entry per library clause
    # it was given, so this counts the same population the old code counted.
    unbound = len(mapped or []) - bound - dropped_dupe
    print(f"[Generic] {bound} rule(s) bound to a column, {dropped_dupe} already "
          f"covered by a contract rule, {unbound} unbound → review queue.")
    return entries


def derive_generic_library_entries(synth_outputs, template_fields, tenant_id=None):
    """Bind the in-scope rule library to THIS program's Output Template.

    Loads the platform's GLOBAL rules plus (when tenant_id is given) the current
    tenant's OWN rules — never another tenant's — so a tenant's rules apply to
    exactly its own uploads. Mirrors the derive_* helpers: returns a list of
    synth_outputs entries ({clause, engine, candidates}) for the caller to extend
    onto synth_outputs. Returns [] when the template is unknown or the library is
    empty/unseeded.

    Deduped against candidates the contract pipeline already produced on the same
    (template, column), so a contract clause restating a baseline check does not
    yield two rules. Tenant rules are bound FIRST, so when a tenant rule and a
    global rule land on the same column the tenant's overrides the global (only
    one fires). The normalizer's rule-level dedup is the third net.

    SELF-CONTAINED PATH: makes its own mapper call. The main pipeline no longer
    takes this route — it merges the library intents into the contract mapper call
    and then calls build_generic_intents/finish_generic_entries directly, saving one
    AI call. This wrapper is kept because it is the whole bind behind one name, for
    callers that have no contract intents to merge with and for tests.
    """
    if not template_fields:
        return []

    clauses, intent_clfs, rules = build_generic_intents(tenant_id, template_fields)
    if not clauses:
        return []

    # Same Call-3 mapper the contract path uses — no new prompt, no new engine.
    from contract_upload_services.stage_b_synthesizer import map_intents_to_ir
    print(f"\n[Generic] binding {len(clauses)} library rule(s) to "
          f"{len(template_fields)} output-template field(s) "
          f"(tenant_id={tenant_id}).")
    mapped = map_intents_to_ir(clauses, intent_clfs, template_fields=template_fields)
    return finish_generic_entries(mapped, rules, synth_outputs, template_fields)
