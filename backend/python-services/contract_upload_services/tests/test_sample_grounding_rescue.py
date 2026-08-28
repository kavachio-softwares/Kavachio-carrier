"""
test_sample_grounding_rescue.py
───────────────────────────────
A value-set rule must never be dropped because the contract's own value is absent
from today's SAMPLE data.

THE FAILURE THIS PINS
─────────────────────
The allowed / excluded values of a value_in_set / value_not_in_set rule come from
the CONTRACT. The sample rows shown to Call 3 are a handful of illustrative cells
from ONE bordereau, so a value the contract authorises or forbids very often is
not among them — and a row that carries an unauthorised value is precisely what
the rule exists to catch. Refusing on those grounds drops the check *and* keeps
the violation invisible.

Across one 621-contract corpus, 228 rules in 62 programmes were refused exactly
that way — "Authorized Classes of Business" the most of all, e.g.

    The allowed value 'Commercial General Liability US Surplus Lines Policies'
    is not found in the sample values for 'Risk Class'.
    Excluded value 'Puerto Rico' for 'Domicile State' cannot be grounded from
    sample data.

In 157 of them the refusal NAMED the column it had rejected: the mapper had
already decided where the rule belongs, then talked itself out of emitting it.

Asserted here: `stage_b_synthesizer._sample_refusal_field`, which recognises that
refusal and hands back the column the model itself named, so the intent can be
re-mapped with the field FORCED instead of dropping to review.

No DB and no LLM: the helper is pure.

Run standalone:  python contract_upload_services/tests/test_sample_grounding_rescue.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except ImportError:
    pass
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")

from contract_upload_services.stage_b_synthesizer import (   # noqa: E402
    _sample_refusal_field,
)

_FIELDS = [{"name": n} for n in (
    "Risk Class", "Domicile State", "Detailed Coverage", "Writing Company",
    "Carrier Entity", "Policy Effective Date", "Gross Written Premium")]


def _unmapped(reason):
    return {"template": None, "params": {}, "reason": reason}


_ENUM = {"operator": "in_set"}          # a value-set intent
_LIMIT = {"operator": "max"}            # anything else


# ── the real refusals from the corpus are all recognised ─────────────────────

def test_real_refusals_return_the_column_the_model_named():
    cases = [
        ("The allowed value 'Commercial General Liability US Surplus Lines "
         "Policies' is not found in the sample values for 'Risk Class'.",
         "Risk Class"),
        ("One or more allowed values for 'Risk Class' cannot be grounded from "
         "sample data: 'Administrator Underwriting Guidelines'.", "Risk Class"),
        ("Excluded value 'Puerto Rico' for 'Domicile State' cannot be grounded "
         "from sample data.", "Domicile State"),
        ("Excluded value 'Nightclubs' not found in sample values for "
         "'Risk Class'.", "Risk Class"),
        ("Value 'Palms Cayman' for 'Carrier Entity' not found in sample data.",
         "Carrier Entity"),
        ("The 'Detailed Coverage' field (samples: ['Excess Liability']) does not "
         "contain the specific coverage types mentioned.", "Detailed Coverage"),
    ]
    for reason, expected in cases:
        got = _sample_refusal_field(_unmapped(reason), _ENUM, _FIELDS)
        assert got == expected, f"{reason!r} → {got!r}, expected {expected!r}"


def test_column_match_is_case_and_space_tolerant():
    got = _sample_refusal_field(
        _unmapped("value 'X' is not in the sample values for 'risk class'"),
        _ENUM, _FIELDS)
    assert got == "Risk Class"


def test_curly_quotes_are_handled():
    got = _sample_refusal_field(
        _unmapped("the allowed value ‘X’ is not found in the sample "
                  "values for ‘Risk Class’."), _ENUM, _FIELDS)
    assert got == "Risk Class"


# ── refusals that must NOT be rescued ────────────────────────────────────────

def test_a_meaning_refusal_is_left_in_review():
    """'No column can hold this kind of value' is a valid refusal — the rescue
    must not override it."""
    for reason in (
        "No field in the catalog represents the 'producing general agent' or 'GA'.",
        "No suitable field found to represent 'peril / cause of loss'. These are "
        "qualitative risk characteristics.",
        "The subject describes a qualitative risk characteristic not represented "
        "as a discrete value in any available bordereau column.",
    ):
        assert _sample_refusal_field(_unmapped(reason), _ENUM, _FIELDS) is None, reason


def test_a_sample_refusal_naming_no_real_column_is_left_in_review():
    got = _sample_refusal_field(
        _unmapped("the allowed value 'X' is not found in the sample values for "
                  "'Underwriter Name'."), _ENUM, _FIELDS)
    assert got is None


def test_a_rule_that_mapped_is_never_touched():
    ir = {"template": "value_in_set",
          "params": {"field": "Risk Class", "allowed": ["X"]},
          "reason": "not found in the sample values for 'Risk Class'"}
    assert _sample_refusal_field(ir, _ENUM, _FIELDS) is None


def test_missing_or_empty_input_is_safe():
    assert _sample_refusal_field(None, _ENUM, _FIELDS) is None
    assert _sample_refusal_field({}, _ENUM, _FIELDS) is None
    assert _sample_refusal_field(_unmapped(""), _ENUM, _FIELDS) is None
    assert _sample_refusal_field(_unmapped("not in sample values for 'Risk Class'"),
                                 _ENUM, []) is None
    assert _sample_refusal_field(_unmapped("not in sample values for 'Risk Class'"),
                                 _ENUM, None) is None
    assert _sample_refusal_field(_unmapped("x"), None, _FIELDS) is None


def test_only_a_value_set_intent_is_rescued():
    """A limit / date / formula intent does not turn on the "values come from the
    contract" argument, so its refusal keeps its review verdict."""
    reason = "the allowed value 'X' is not found in the sample values for 'Risk Class'."
    assert _sample_refusal_field(_unmapped(reason), _ENUM, _FIELDS) == "Risk Class"
    for other in ({"operator": "max"}, {"operator": "date_bound"},
                  {"operator": "cross_field_math"}, {}):
        assert _sample_refusal_field(_unmapped(reason), other, _FIELDS) is None, other


def test_a_meaning_refusal_that_merely_mentions_samples_stays_in_review():
    """Real corpus refusal: the column genuinely cannot hold the value, and the
    sentence happens to mention samples too. MEANING wins."""
    reason = ("No suitable field found for 'facultative reinsurance status'. The "
              "'Carrier Entity' field indicates something else and its sample "
              "values do not match.")
    assert _sample_refusal_field(_unmapped(reason), _ENUM, _FIELDS) is None


def test_a_quoted_value_outside_a_column_slot_is_not_taken_as_the_column():
    """'Writing Company' here is a VALUE the refusal quotes, not the column it
    rejected — nothing sits in a for/of/field slot, so nothing is rescued."""
    reason = ("The provided values 'Writing Company' and 'Carrier Entity' are not "
              "present in the sample data.")
    assert _sample_refusal_field(_unmapped(reason), _ENUM, _FIELDS) is None


def test_the_value_is_never_mistaken_for_the_column():
    """The phrasings put the VALUE first and the COLUMN last, and a value can
    itself be a real column name in some other template — the LAST quoted name
    that is a real column of THIS template wins."""
    got = _sample_refusal_field(
        _unmapped("the allowed value 'Writing Company' is not found in the "
                  "sample values for 'Risk Class'."), _ENUM, _FIELDS)
    assert got == "Risk Class"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'OK'} — {failures} failure(s)")
    sys.exit(1 if failures else 0)
