"""
test_in_period_date_guard.py
────────────────────────────
Tests for the DATE-ROLE guard: a check that places a date INSIDE the policy
period must never be bound to a transaction PROCESSING (booked / keyed) date
column.

The case that motivated them: the global library rule "Transaction Effective
Date Must Be Valid" was mapped onto a bordereau's "Policy Transaction Date" —
the date the transaction was put on the books, not the date it took effect on
the risk. On one real program that flagged 26 cancellations whose only fault was
being recorded a few days after the (restated) policy expiry, while the column
that actually carries the transaction's effective date violated nothing.

Two pieces are asserted here:
  * `output_schema.is_processing_date_column` — the ONE role test, shared by the
    code that WANTS that column (backdating is measured inception → processing
    date) and the code that must keep it OUT of an in-period bound;
  * `generic_rule_library._guard_in_period_date_bounds` — the deterministic
    backstop that unbinds such a rule (→ review queue) instead of shipping it.

No DB and no LLM: both are pure.

Run standalone:  python contract_upload_services/tests/test_in_period_date_guard.py
(or under pytest — each case asserts independently).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

# The guard is pure, but its module pulls in the service's DB-backed constants,
# so the usual service environment has to be present — the same .env load the
# other tests in this directory do. Nothing here reads or writes application data.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except ImportError:
    pass
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")

from contract_upload_services.output_schema import (     # noqa: E402
    is_processing_date_column,
)
from contract_upload_services.generic_rule_library import (   # noqa: E402
    _guard_in_period_date_bounds,
)


# ── the role test ────────────────────────────────────────────────────────────

def test_processing_date_columns_are_recognised():
    for name in ("Policy Transaction Date", "Transaction Date",
                 "TRANSACTION_DATE", "transaction date", "Txn Transaction Date"):
        assert is_processing_date_column(name), name


def test_effective_and_expiry_transaction_dates_are_not_processing_dates():
    for name in ("Transaction Effective Date", "TransactionEffectiveDate",
                 "Effective Date of Transaction US", "Transaction Expiration Date",
                 "Transaction Expiry Date"):
        assert not is_processing_date_column(name), name


def test_unrelated_columns_are_not_processing_dates():
    for name in ("Policy Effective Date", "Policy Expiration Date",
                 "Endorsement Cancellation Change Date", "Insured Name",
                 "Transaction Type", "Booking Month", "", None):
        assert not is_processing_date_column(name), name


# ── the backstop ─────────────────────────────────────────────────────────────

def _mapped(class_rule_id, ir):
    """One post-Call-3 entry for the library rule whose id is `class_rule_id`
    (the synthetic clause_id is the NEGATED library id — see _build_intents)."""
    return [{"clause": {"clause_id": -class_rule_id}, "candidates": [ir]}]


_RULES = [
    {"id": 39, "class_name": "TransactionEffectiveDate"},
    {"id": 31, "class_name": "PolicyPeriod"},
    {"id": 14, "class_name": "NotNull"},
]


def test_in_period_bound_on_a_processing_date_is_unbound_for_review():
    ir = {"template": "date_relation",
          "params": {"field": "Policy Transaction Date", "op": "<=",
                     "other_field": "Policy Expiration Date"}}
    mapped = _mapped(39, ir)
    assert _guard_in_period_date_bounds(mapped, _RULES) == 1
    assert ir["template"] is None
    assert "Policy Transaction Date" in ir["reason"]
    # the params are kept so the reviewer can see what was proposed
    assert ir["params"]["field"] == "Policy Transaction Date"


def test_correctly_bound_effective_date_is_left_alone():
    for field in ("Endorsement Cancellation Change Date",
                  "Transaction Effective Date", "Premium Effective Date"):
        ir = {"template": "date_relation",
              "params": {"field": field, "op": "<=",
                         "other_field": "Policy Expiration Date"}}
        mapped = _mapped(39, ir)
        assert _guard_in_period_date_bounds(mapped, _RULES) == 0, field
        assert ir["template"] == "date_relation", field
        assert "reason" not in ir, field


def test_processing_date_on_the_other_side_of_the_relation_also_fires():
    ir = {"template": "date_relation",
          "params": {"field": "Policy Effective Date", "op": "<=",
                     "other_field": "Policy Transaction Date"}}
    mapped = _mapped(39, ir)
    assert _guard_in_period_date_bounds(mapped, _RULES) == 1
    assert ir["template"] is None


def test_policy_period_duration_on_a_processing_date_fires():
    ir = {"template": "period_duration",
          "params": {"start_field": "Policy Effective Date",
                     "end_field": "Policy Transaction Date",
                     "unit": "month", "min": 12, "max": 12}}
    mapped = _mapped(31, ir)
    assert _guard_in_period_date_bounds(mapped, _RULES) == 1
    assert ir["template"] is None


def test_policy_period_on_its_own_dates_is_left_alone():
    ir = {"template": "period_duration",
          "params": {"start_field": "Policy Effective Date",
                     "end_field": "Policy Expiration Date",
                     "unit": "month", "min": 12, "max": 12}}
    mapped = _mapped(31, ir)
    assert _guard_in_period_date_bounds(mapped, _RULES) == 0
    assert ir["template"] == "period_duration"


def test_other_library_classes_are_never_touched():
    """A required-field rule on the processing date column is perfectly valid —
    the guard is scoped to the classes that assert an IN-PERIOD date."""
    ir = {"template": "required_field",
          "params": {"field": "Policy Transaction Date"}}
    mapped = _mapped(14, ir)
    assert _guard_in_period_date_bounds(mapped, _RULES) == 0
    assert ir["template"] == "required_field"


def test_already_unmapped_rule_is_not_double_counted():
    ir = {"template": None, "params": {}, "reason": "no field"}
    mapped = _mapped(39, ir)
    assert _guard_in_period_date_bounds(mapped, _RULES) == 0
    assert ir["reason"] == "no field"


def test_guard_no_ops_on_empty_input():
    assert _guard_in_period_date_bounds([], _RULES) == 0
    assert _guard_in_period_date_bounds(None, _RULES) == 0


def test_several_unmapped_rules_all_survive_the_dedup():
    """Every unmapped rule must reach review. They all key to (None, None) in the
    (template, column) dedup, so the second and later ones used to look like
    duplicates of the first and were dropped — losing review items."""
    import contract_upload_services.generic_rule_library as grl

    rules = [{"id": 39, "rule_name": "A", "severity": "Major",
              "class_name": "TransactionEffectiveDate", "validation_logic": "x"},
             {"id": 31, "rule_name": "B", "severity": "Major",
              "class_name": "PolicyPeriod", "validation_logic": "y"}]
    unmapped = [
        {"clause": {"clause_id": -39},
         "candidates": [{"template": None, "params": {}, "reason": "no column"}]},
        {"clause": {"clause_id": -31},
         "candidates": [{"template": None, "params": {}, "reason": "no column"}]},
    ]
    orig_load, orig_build, orig_map = (grl.load_generic_rules, grl._build_intents, None)
    import contract_upload_services.stage_b_synthesizer as sb
    orig_map = sb.map_intents_to_ir
    try:
        grl.load_generic_rules = lambda tid=None: rules
        sb.map_intents_to_ir = lambda *a, **k: unmapped
        entries = grl.derive_generic_library_entries([], [{"name": "Any Column"}])
    finally:
        grl.load_generic_rules, grl._build_intents = orig_load, orig_build
        sb.map_intents_to_ir = orig_map
    kept = sum(len(e.get("candidates") or []) for e in entries)
    assert kept == 2, f"only {kept} of 2 unmapped rules survived to review"


def test_unbound_rule_reaches_the_review_queue_not_the_bin():
    """The verify gate must turn the guard's verdict into a REVIEW route carrying
    the guard's reason — the rule is re-mappable by a human, never silently lost."""
    from contract_upload_services.rule_normalizer import verify_and_build_ir_rule
    ir = {"template": "date_relation",
          "params": {"field": "Policy Transaction Date", "op": "<=",
                     "other_field": "Policy Expiration Date"},
          "rule_name": "Transaction Effective Date Must Be Valid"}
    _guard_in_period_date_bounds(_mapped(39, ir), _RULES)
    out = verify_and_build_ir_rule(ir, {"clause_id": -39}, {}, None)
    assert out.get("route") == "review", out
    assert "RECORDED" in out.get("reason", ""), out


# ── the shared definition still serves its original caller ───────────────────

def test_backdating_fixer_still_finds_the_processing_date_column():
    """fix_backdating_period_fields WANTS the processing date (backdating =
    inception → the date it was booked). It now asks the shared role test, so
    this pins that the refactor kept its behaviour."""
    from contract_upload_services.validation_rule_generator import (
        fix_backdating_period_fields,
    )
    fields = [{"name": n} for n in ("Policy Effective Date", "Policy Expiration Date",
                                    "Transaction Effective Date",
                                    "Policy Transaction Date")]
    synth = [{"clause": {"text": "Backdating of coverage beyond 5 days requires "
                                 "prior approval."},
              "candidates": [{"template": "period_duration",
                              "rule_name": "Backdating limit",
                              "params": {"start_field": "Policy Effective Date",
                                         "end_field": "Transaction Effective Date",
                                         "unit": "day", "min": 5}}]}]
    assert fix_backdating_period_fields(synth, fields) == 1
    p = synth[0]["candidates"][0]["params"]
    assert p["start_field"] == "Policy Effective Date"
    assert p["end_field"] == "Policy Transaction Date"
    assert p["max"] == 5 and "min" not in p


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print("\nall tests passed" if not failures else f"\n{failures} FAILED")
    sys.exit(1 if failures else 0)
