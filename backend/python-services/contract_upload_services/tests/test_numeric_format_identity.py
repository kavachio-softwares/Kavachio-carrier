"""A row flagged for not being a number must say so — under its own name.

Every numeric rule compiles a companion check beside its comparison: a cell
holding text cannot be compared, so it is flagged in its own right rather than
passing silently (rule_compiler._not_numeric_select). Those rows used to be
shown under the RULE's identity, which described a check that never ran on them:

    heading         Paid Loss Amount Must Not Exceed Incurred Loss Amount
    what it checks  total_paid must be no more than total_incurred
    what's wrong    total_paid does not agree with the figures it is derived from
    recommendation  <= total_incurred                      ← Approve would write this
    reason          Program ID must be a number, but found BB2.

Only the last line was about the row. These tests pin the re-titling that fixes
it, on BOTH read paths — the output screen (which keeps the row's own reason) and
the upload screen (which stores only the rule, the column and the value).

Run:  python contract_upload_services/tests/test_numeric_format_identity.py
(pytest is not installed in this venv; every test file carries its own runner.)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from contract_upload_services.rule_compiler import not_numeric_reason   # noqa: E402
from contract_upload_services.rule_explainer import (                   # noqa: E402
    apply_numeric_format_identity, classify_numeric_format,
    explain_numeric_format, numeric_format_rule_name,
)

SPEC = {"kind": "ir_v1", "ir": {
    "template": "cross_field_compare",
    "rule_source": "generic_library",
    "params": {"field": "total_paid", "op": "<=", "other_field": "total_incurred"},
}}


def _output_row(field="Program ID", actual="BB2"):
    """An exception as the output (per-download) screen has it — with the row's
    own reason."""
    return {
        "rule_id": 2471, "rule_name": "Paid Loss Amount Must Not Exceed Incurred Loss Amount",
        "field": field, "column": field, "actual_value": actual,
        "reason": not_numeric_reason(field, actual),
        "recommendation": "<= total_incurred", "expected_value": "<= total_incurred",
        "explanation": {"requirement": "total_paid must be no more than total_incurred."},
    }


def _upload_row(field="total_paid", actual="n/a"):
    """The same exception as the upload screen has it — no per-row reason."""
    return {
        "rule_id": 2471, "rule_name": "Paid Loss Amount Must Not Exceed Incurred Loss Amount",
        "field_path": field, "actual_value": actual,
        "recommendation": "<= total_incurred", "expected_value": "<= total_incurred",
        "explanation": {"requirement": "total_paid must be no more than total_incurred."},
    }


# ── recognising the row ──────────────────────────────────────────────────────

def test_output_row_recognised_by_its_own_reason():
    assert classify_numeric_format(field="Program ID", actual_value="BB2",
                                   reason=not_numeric_reason("Program ID", "BB2"))


def test_output_row_with_the_rules_own_reason_is_not_one():
    assert not classify_numeric_format(
        field="total_paid", actual_value="900",
        reason="total_paid must be <= total_incurred")


def test_upload_row_recognised_from_rule_and_value():
    assert classify_numeric_format(rule_spec=SPEC, field="total_paid",
                                   actual_value="n/a")


def test_a_real_comparison_breach_is_not_one():
    """A numeric value that simply breaks the rule keeps the rule's identity."""
    assert not classify_numeric_format(rule_spec=SPEC, field="total_paid",
                                       actual_value="9000")


def test_numbers_the_compiler_accepts_are_numbers_here_too():
    """The SQL cast tolerates separators, currency, percent and accounting
    negatives; misjudging any of them would re-title a genuine breach."""
    for ok in ("1200.50", " 8,000 ", "$1,250", "(500)", "12%", "-3", "0"):
        assert not classify_numeric_format(rule_spec=SPEC, field="total_paid",
                                           actual_value=ok), ok
    for bad in ("BB2", "n/a", "TBC", "1,2.3.4", "twelve"):
        assert classify_numeric_format(rule_spec=SPEC, field="total_paid",
                                       actual_value=bad), bad


def test_blank_is_left_to_required_field():
    for blank in ("", "   ", None):
        assert not classify_numeric_format(rule_spec=SPEC, field="total_paid",
                                           actual_value=blank)


def test_only_the_operands_are_judged():
    """A numeric rule's scope/grouping columns hold ordinary text and must never
    be re-titled as failing a numeric cast."""
    spec = {"ir": {"template": "max_limit",
                   "params": {"field": "Limit", "max": 100,
                              "scope": {"Program Name": "BB2"}}}}
    assert not classify_numeric_format(rule_spec=spec, field="Program Name",
                                       actual_value="BB2")
    assert classify_numeric_format(rule_spec=spec, field="Limit",
                                   actual_value="BB2")


def test_non_numeric_templates_are_never_re_titled():
    spec = {"ir": {"template": "value_in_set",
                   "params": {"field": "LOB", "allowed": ["GL"]}}}
    assert not classify_numeric_format(rule_spec=spec, field="LOB",
                                       actual_value="Property")


# ── re-titling the row ───────────────────────────────────────────────────────

def test_output_row_is_re_titled():
    e = _output_row()
    assert apply_numeric_format_identity(e, SPEC) is True
    assert e["rule_name"] == "Program ID Must Be A Number"
    assert e["check_kind"] == "numeric_format"
    assert e["root_cause"] == "type_mismatch"
    # every sentence now names the column the reviewer must fix …
    for key in ("requirement", "problem", "how_to_fix"):
        assert "Program ID" in e["explanation"][key], key
    # … and none of them claims the rule's own comparison failed
    assert "total_incurred" not in " ".join(e["explanation"].values())
    # the rule that could not run is still named, so the reviewer can see it
    assert "Paid Loss Amount" in e["explanation"]["problem"]


def test_the_wrong_recommendation_is_withdrawn():
    """"<= total_incurred" is not what this cell needs, and Approve would have
    written it into the sheet."""
    e = _output_row()
    apply_numeric_format_identity(e, SPEC)
    assert e["recommendation"] is None
    assert e["expected_value"] is None
    assert e["recommendation_options"] is None


def test_upload_row_is_re_titled_identically():
    e = _upload_row()
    assert apply_numeric_format_identity(e, SPEC) is True
    assert e["rule_name"] == "total_paid Must Be A Number"
    assert e["check_kind"] == "numeric_format"
    assert e["recommendation"] is None


def test_an_ordinary_violation_keeps_the_rules_identity():
    e = _upload_row(actual="9000")
    assert apply_numeric_format_identity(e, SPEC) is False
    assert e["rule_name"] == "Paid Loss Amount Must Not Exceed Incurred Loss Amount"
    assert e["recommendation"] == "<= total_incurred"
    assert "check_kind" not in e


def test_explanation_stands_on_its_own_without_a_rule_name():
    exp = explain_numeric_format("Program ID")
    assert exp["requirement"] == "Program ID must hold a number."
    assert exp["problem"] and exp["how_to_fix"]
    assert exp["origin_label"] and exp["origin_note"]


def test_name_is_derived_not_looked_up():
    for col in ("Program ID", "loss_paid", "(2) Loss Payments"):
        assert numeric_format_rule_name(col).startswith(col)


def test_never_raises():
    for junk in (None, "", 123, [], {"rule_id": 1}):
        try:
            apply_numeric_format_identity(junk, SPEC)
        except Exception as exc:
            raise AssertionError(f"raised on {junk!r}: {exc}")
    e = _output_row()
    assert apply_numeric_format_identity(e, "not json") in (True, False)


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
