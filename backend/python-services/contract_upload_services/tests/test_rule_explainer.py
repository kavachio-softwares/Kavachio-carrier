"""Every rule must explain itself, in insurance English, without exception.

The exception screens now lead with `rule_explainer.explain_rule`, so a template
whose branch is missing (or whose sentence is wrong) shows a reviewer nothing
actionable. These tests pin three things:

  1. COVERAGE — every template in the catalog produces a requirement sentence.
     A new template added to rule_templates without a branch here fails the
     build rather than shipping a blank card.
  2. FIDELITY — the sentence matches what rule_compiler actually compiles, in
     the directions that are easy to invert (op is the COMPLIANT relation, so
     date_relation '<=' means "on or before", never "after").
  3. NO LEAKS — no warehouse column name, raw regex or Python repr reaches the
     reviewer.

Run:  python contract_upload_services/tests/test_rule_explainer.py
(pytest is not installed in this venv; every test file carries its own runner.)
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from contract_upload_services.rule_explainer import (   # noqa: E402
    describe_requirement, explain_rule, humanize_columns, _describe_pattern,
)

# One representative params set per template — the SHAPES mirror live rules, the
# NAMES are invented. Nothing here is known to rule_explainer: it reads the
# template and params it is handed, so these are inputs to assert against, not a
# vocabulary the production code matches on. (test_every_catalog_template_is_
# explained walks the real catalog, so a new template still fails the build.)
SAMPLES = {
    "required_field":      {"field": "Insured City"},
    "conditional_required": {"condition": {"field": "Event Type", "op": "=",
                                           "value": "Endorsement"},
                             "required_field": "Gross Written Premium"},
    "value_in_set":        {"field": "Carrier Name",
                            # Ends in "." on purpose — it pins the "INC.." double-stop fix.
                            "allowed": ["Northwind Specialty Insurance Co., Inc."]},
    "value_not_in_set":    {"field": "Class of Business",
                            "excluded": ["Nuclear Energy", "Offshore Risks"]},
    "max_limit":           {"field": "Occurrence Limit", "max": 2000000},
    "min_limit":           {"field": "Gross Written Premium", "min": 0},
    "range_check":         {"field": "Commission %", "min": 0.1, "max": 0.3},
    "pattern_check":       {"field": "Insured Zip", "pattern": r"^\d{5}(-\d{4})?$"},
    "date_relation":       {"field": "Transaction Effective Date", "op": "<=",
                            "other_field": "Policy Expiration Date"},
    "date_bound":          {"field": "Policy Effective Date", "op": ">=",
                            "date": "2026-04-01"},
    "period_duration":     {"start_field": "Policy Effective Date",
                            "end_field": "Policy Expiration Date",
                            "unit": "month", "min": 12, "max": 18},
    "aggregate_cap":       {"aggregation": "distinct_count", "field": "Policy Effective Date",
                            "group_by": ["Full Policy Number"], "max": 1},
    "uniqueness":          {"fields": ["Policy Number", "Transaction Type"]},
    "cross_field_math":    {"result_field": "Net Premium", "left_field": "GWP",
                            "operator": "-", "right_field": "Commission"},
    "cross_field_compare": {"field": "Total Paid", "op": "<=",
                            "other_field": "Total Incurred"},
    "cross_field_or_value": {"field": "Fronting Fee", "op": ">=", "other_field": "GWP",
                             "operator": "*", "factor": 0.125, "value": 15000,
                             "bound": "greater"},
    "conditional_value":   {"condition": {"field": "Insured State", "op": "!=",
                                          "value": "CA"},
                            "field": "Carrier Name", "op": "=",
                            "value": "Northwind Specialty"},
    "conditional_all":     {"conditions": [{"field": "Paper", "op": "=", "value": "Specialty"},
                                           {"field": "State", "op": "=", "value": "CA"}],
                            "field": "Referral Indicator", "op": "=", "value": "Yes"},
    "zip_state_consistency": {"zip_field": "Insured Zip", "state_field": "Insured State"},
    "state_validity":      {"state_field": "Insured State"},
    "currency_country_consistency": {"currency_field": "Currency",
                                     "country_field": "Country"},
}


def test_every_catalog_template_is_explained():
    """No template may ship without a plain-English branch."""
    from contract_upload_services.rule_ir import TEMPLATE_CATALOG
    missing_sample, missing_text = [], []
    for name in sorted(TEMPLATE_CATALOG):
        params = SAMPLES.get(name)
        if params is None:
            missing_sample.append(name)
            continue
        if not describe_requirement(name, params):
            missing_text.append(name)
    assert not missing_sample, (
        f"templates with no test sample (add one, and a branch in "
        f"rule_explainer if it lacks one): {missing_sample}")
    assert not missing_text, f"templates with no requirement sentence: {missing_text}"


def test_comparison_directions_are_not_inverted():
    """`op` is the COMPLIANT relation — the most invertible thing here."""
    s = describe_requirement("date_relation", SAMPLES["date_relation"])
    assert "on or before" in s, s
    assert "after" not in s.replace("on or before", ""), s

    s = describe_requirement("date_bound", SAMPLES["date_bound"])
    assert "on or after" in s and "April 1, 2026" in s, s

    s = describe_requirement("cross_field_compare", SAMPLES["cross_field_compare"])
    assert "no more than" in s and "Total Incurred" in s, s

    # max_limit is a ceiling, min_limit a floor — never the other way round.
    assert "not be more than" in describe_requirement("max_limit", SAMPLES["max_limit"])
    # min 0 is the "cannot be negative" rule, and should say so.
    assert "not be negative" in describe_requirement("min_limit", SAMPLES["min_limit"])


def test_invariant_reads_as_must_not_change():
    """aggregate_cap/distinct_count/max=1 is an INVARIANT, not a count."""
    s = describe_requirement("aggregate_cap", SAMPLES["aggregate_cap"])
    assert "same on every row" in s and "Full Policy Number" in s, s
    assert "distinct" not in s.lower(), s


def test_null_condition_is_presence_not_python_none():
    """A condition value of None means populated/blank, never the word 'None'."""
    s = describe_requirement("conditional_value", {
        "condition": {"field": "Reinsurance Paper", "op": "!=", "value": None},
        "field": "Referral Indicator", "op": "=", "value": "Yes"})
    assert "is filled in" in s, s
    assert "None" not in s, s


def test_patterns_read_as_english_not_regex():
    cases = [
        (r"^\d{4}$", None, "4 digits"),
        (r"^(\d{5}|441105)$", None, "5 digits or \u201c441105\u201d"),
        # A PREFIX match must not read as an exact match.
        (r"(?i)^(primary|excess)\b", None,
         "start with \u201cprimary\u201d or \u201cexcess\u201d (upper or lower case)"),
        # A bare 5 digits is as likely a SIC / NAIC / class code as a ZIP, so it
        # is never named — regardless of what the column happens to be called.
        (r"^\d{5}$", "SIC Code", "5 digits"),
        (r"^\d{5}$", "Insured Zip", "5 digits"),
        (r"^\d{5}(-\d{4})?$", "Insured Zip",
         "a 5-digit ZIP code, optionally with a 4-digit extension"),
        (r"(^\d{5}(-\d{4})?$)|(^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$)", "Insured Zip",
         "a 5-digit ZIP code, optionally with a 4-digit extension or "
         "a UK-style postcode"),
        # A quantified group must not collapse to one repetition.
        (r"^(\d{2}){3}$", None, "(2 digits) repeated 3 times"),
    ]
    for pat, field, want in cases:
        got = _describe_pattern(pat, field)
        assert got == want, f"{pat!r} field={field!r}\n  want: {want}\n  got : {got}"


def test_regex_constructs_we_cannot_read_are_refused_not_guessed():
    """A confident INVERSION is far worse than declining to explain.

    Each of these was previously rendered as its own opposite: `[^0-9]` as
    "digit", `(?!TEST)` as the literal "!TEST", `\\1` as the digit "1".
    """
    for pat in (r"^[^0-9]{3}$", r"^[^A-Z]+$", r"^(?!TEST)\w+$",
                r"^(?=.*\d)\w+$", r"^(\w+)-\1$"):
        assert _describe_pattern(pat) is None, f"{pat!r} should not be described"


def test_pattern_sentence_is_grammatical_when_unanchored():
    """An unanchored pattern is a VERB phrase \u2014 "must be start with" is not English."""
    s = describe_requirement("pattern_check", {
        "field": "Coverage Description", "pattern": r"(?i)^(primary|excess)\b"})
    assert s.startswith("Coverage Description must start with"), s
    s2 = describe_requirement("pattern_check", {"field": "Class Code",
                                                "pattern": r"^\d{4}$"})
    assert s2 == "Class Code must be 4 digits.", s2


def test_nothing_leaks_to_the_reviewer():
    """No requirement may contain a raw regex, a Python repr or a placeholder."""
    bad = re.compile(r"\\d|\{\d+\}|\[A-Z|\(\?i\)|\{\{|None\b")
    for name, params in SAMPLES.items():
        s = describe_requirement(name, params)
        if not s:
            continue
        assert not bad.search(s), f"{name}: leaked syntax in {s!r}"


def test_humanize_columns_only_touches_snake_case():
    assert humanize_columns("must not fall after pol_exp_dt or tran_exp_dt") == \
        "must not fall after pol exp dt or tran exp dt"
    # Ordinary prose, hyphens and single words are untouched.
    for s in ("The policy expiration date must be later.",
              "A well-known carrier.", "Policy Effective Date"):
        assert humanize_columns(s) == s, s


def test_origin_is_classified_and_markers_stripped():
    """A library rule is not a contract clause, and its marker never shows."""
    spec = {"ir": {"template": "required_field", "params": {"field": "Insured City"},
                   "rule_source": "generic_library"}}
    e = explain_rule(rule_spec=spec,
                     source_verbatim_text="[Generic rule] Insured City Must Not Be "
                                          "Null \u2014 The insured's city must be populated.")
    assert e["origin"] == "standard"
    assert e["origin_label"] == "Kavachio standard check"
    assert "[Generic rule]" not in (e.get("source_text") or "")
    assert e["source_text"] == "The insured's city must be populated."

    e2 = explain_rule(rule_spec={"ir": {"template": "required_field",
                                        "params": {"field": "X"}}},
                      source_verbatim_text="The Company shall...",
                      contract_filename="treaty.pdf", source_page_number=3)
    assert e2["origin"] == "contract"
    assert e2["origin_label"] == "Contract clause \u00b7 treaty.pdf \u00b7 p.3"


def test_referral_reads_as_a_referral_not_a_breach():
    """The referral framing lives on the chip and the action, never on `problem`.

    `problem` must keep describing the rows the SQL ACTUALLY flags. An earlier
    version replaced it with "these rows match a condition that must be
    referred", which for a referral compiled as value_in_set(AK, HI) named the
    exact opposite row set from the one listed underneath it — one card, two
    contradictory sentences.
    """
    spec = {"referral": True,
            "ir": {"template": "value_in_set",
                   "params": {"field": "Risk State", "allowed": ["AK", "HI"]}}}
    e = explain_rule(rule_spec=spec)
    assert e.get("is_referral") is True
    assert e.get("kind_label") == "Referral trigger"
    assert "Refer these policies" in e["how_to_fix"]
    # The problem describes the flagged rows, exactly as for a compliance rule.
    assert e["problem"] == "These rows have a Risk State that is not on the allowed list."


def test_scope_is_only_claimed_when_the_compiler_applies_it():
    """Eight builders ignore params['scope']; saying otherwise narrows the rule."""
    scoped = {"field": "Gross Written Premium", "max": 170000000,
              "aggregation": "sum", "group_by": ["Program Name"],
              "scope": {"Program Name": "PN0052"}}
    # aggregate_cap compiles no WHERE at all — must NOT claim a restriction.
    assert "applies_to" not in explain_rule(
        rule_spec={"ir": {"template": "aggregate_cap", "params": scoped}})
    # max_limit does apply it.
    e = explain_rule(rule_spec={"ir": {"template": "max_limit", "params": scoped}})
    assert e.get("applies_to") == "Applies only to rows where Program Name is PN0052."


def test_excluded_scope_is_not_read_as_an_inclusion():
    """`{allowed, excluded}` compiles to NOT IN — the opposite of "is"."""
    e = explain_rule(rule_spec={"ir": {"template": "max_limit", "params": {
        "field": "Limit", "max": 5,
        "scope": {"Coverage": {"allowed": ["GL"], "excluded": ["WC"]}}}}})
    assert e["applies_to"] == "Applies only to rows where Coverage is not GL."


def test_unusable_scope_shapes_are_silent_not_leaked():
    """A stringified scope is not a filter — and never a Python repr on screen.

    76 stored rules carry a scope value like "{'op': '!=', 'value': 'CA'}" as a
    STRING. rule_compiler does not parse it either — it emits
    `LOWER(TRIM(col)) = '{''op'': ...}'`, which no cell can equal — so there is
    no restriction to describe.
    """
    for bad in ("{'op': '!=', 'value': 'CA'}", "{'!=': 'CA'}",
                "['California', 'Florida']"):
        e = explain_rule(rule_spec={"ir": {"template": "max_limit", "params": {
            "field": "Limit", "max": 5, "scope": {"Insured State": bad}}}})
        assert "applies_to" not in e, e.get("applies_to")


def test_partially_describable_scope_is_dropped_whole():
    """Scope filters are ANDed — describing only some states a WIDER rule."""
    e = explain_rule(rule_spec={"ir": {"template": "max_limit", "params": {
        "field": "Limit", "max": 5,
        "scope": {"Coverage": "CGL", "Insured State": "{'op': '=', 'value': 'TX'}"}}}})
    assert "applies_to" not in e, e.get("applies_to")


def test_conditional_keeps_the_column_name_intact():
    """The target column is an identifier the reviewer must find in the file."""
    s = describe_requirement("conditional_value", {
        "condition": {"field": "Legal Entity", "op": "=", "value": "Specialty"},
        "field": "Broker Referral Indicator", "op": "=", "value": "Yes"})
    assert "Broker Referral Indicator" in s, s
    assert "broker Referral Indicator" not in s, s


def test_duration_units_are_singular_for_one():
    s = describe_requirement("period_duration", {
        "start_field": "A", "end_field": "B", "unit": "month", "min": 1})
    assert s.endswith("at least 1 month."), s


def test_an_exception_with_no_rule_gets_no_invented_explanation():
    """rule_id NULL (structural type checks) must keep its own message.

    Returning provenance + a generic how-to-fix for these replaced the
    exception's real reason with a "Contract clause" chip it never came from.
    """
    assert explain_rule(rule_spec=None, source_verbatim_text=None,
                        rule_description=None) == {}


def test_explain_rule_never_raises():
    for junk in (None, "", "not json", 123, {"ir": None}, {"ir": {"template": "nope"}},
                 {"ir": {"template": "value_in_set", "params": None}},
                 {"ir": {"template": "value_in_set", "params": {"allowed": [None]}}}):
        assert isinstance(explain_rule(rule_spec=junk), dict)


if __name__ == "__main__":
    import traceback
    fails = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception:
                fails += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print("ALL PASSED" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
