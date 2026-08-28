"""
test_same_subject_compare_guard.py
──────────────────────────────────
Tests for the SAME-SUBJECT guard: a plain paid-vs-incurred check must never weigh
a rolled-up TOTAL against a single COMPONENT of that total.

The case that motivated them: the global library rule "Closed Claims Must Have No
Outstanding Reserves" (incurred <= paid, on closed claims) was mapped onto a
bordereau that splits claim money per component — med_/ind_/loss_/dcc_/aoe_ ×
paid/resv/incurred — plus ONE rolled-up column, `total_incured`. With no
"total paid" column to bind, the mapper elected `loss_paid`, and the rule shipped
as

    total_incured <= loss_paid

On the real BDX that flagged 8 closed claims whose reserves were ALL zero: their
only fault was carrying defence costs (`dcc_paid`), which the total includes and
`loss_paid` excludes. The bucket-consistent binding the mapper produces on other
runs of the SAME template — `loss_incurred <= loss_paid` — flags none of them.

Asserted here: `generic_rule_library._guard_same_subject_compares`, the
deterministic backstop that unbinds such a rule (→ review queue, with a reason)
instead of shipping it. The Call-3 prompt carries the same axis (SAME SUBJECT,
SAME LEVEL); this is what catches the run where the model — or its RELAXED retry —
binds the mismatched pair anyway.

No DB and no LLM: the guard is pure.

Run standalone:  python contract_upload_services/tests/test_same_subject_compare_guard.py
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

from contract_upload_services.generic_rule_library import (   # noqa: E402
    _guard_same_subject_compares,
)


def _mapped(library_rule_id, ir):
    """One post-Call-3 entry for the library rule whose id is `library_rule_id`
    (the synthetic clause_id is the NEGATED library id — see _build_intents)."""
    return [{"clause": {"clause_id": -library_rule_id}, "candidates": [ir]}]


_RULES = [
    {"id": 3,  "class_name": "ClaimOverpayment"},
    {"id": 25, "class_name": "OutstandingOnClosed"},
    {"id": 47, "class_name": "PalmsPctShare"},
    {"id": 14, "class_name": "NotNull"},
    {"id": 99, "class_name": "CompareFields"},      # tenant-authored building block
]


def _compare(field, other, **extra):
    params = {"field": field, "op": "<=", "other_field": other}
    params.update(extra)
    return {"template": "cross_field_compare", "params": params}


# ── the failure this pins ────────────────────────────────────────────────────

def test_rolled_up_total_against_one_component_is_unbound_for_review():
    ir = _compare("total_incured", "loss_paid")
    ir["params"]["scope"] = {"open_closed": "Closed"}
    mapped = _mapped(25, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 1
    assert ir["template"] is None
    assert "total_incured" in ir["reason"] and "loss_paid" in ir["reason"]
    # params are kept so the reviewer can see what was proposed
    assert ir["params"]["field"] == "total_incured"
    assert ir["params"]["scope"] == {"open_closed": "Closed"}


def test_the_same_mismatch_on_the_overpayment_rule_also_fires():
    """The mirror-image binding (part <= whole) is just as mis-bound — it happens
    to pass on today's data, which is exactly why it never gets noticed."""
    ir = _compare("loss_paid", "total_incured")
    mapped = _mapped(3, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 1
    assert ir["template"] is None


def test_two_unrelated_columns_are_unbound():
    """Same defect, further gone: a bordereau with no claim columns at all had the
    rule bound to a premium column and a unit count."""
    ir = _compare("Highest Value Unit", "Commission Amount")
    mapped = _mapped(25, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 1
    assert ir["template"] is None


# ── every correctly-bound pair survives ──────────────────────────────────────

def test_same_component_measures_are_left_alone():
    for left, right in (("loss_incurred", "loss_paid"),
                        ("loss_paid", "loss_incurred"),
                        ("dcc_incurred", "dcc_paid"),
                        ("total_incurred", "total_paid"),
                        ("total_paid", "total_incurred"),
                        ("PaidLossAmount", "IncurredLossAmount"),
                        ("Total Paid Amount", "Incurred Loss Amount USD"),
                        ("(2) Loss Payments During Period",
                         "(4) Incurred During Period ( 2 + 3 )")):
        ir = _compare(left, right)
        mapped = _mapped(25, ir)
        assert _guard_same_subject_compares(mapped, _RULES) == 0, (left, right)
        assert ir["template"] == "cross_field_compare", (left, right)
        assert "reason" not in ir, (left, right)


def test_camelcase_names_share_their_subject():
    """The shared tokenizer splits camelCase, so PaidLossAmount / IncurredLossAmount
    are seen to share 'loss' + 'amount' rather than being two opaque words."""
    ir = _compare("PaidLossAmount", "IncurredLossAmount")
    assert _guard_same_subject_compares(_mapped(3, ir), _RULES) == 0
    assert ir["template"] == "cross_field_compare"


# ── scope: only the classes that compare two measures of ONE amount ──────────

def test_a_share_ordering_rule_is_never_touched():
    """PalmsPctShare deliberately compares two DIFFERENT parties' shares, so the
    operands are not supposed to name one amount."""
    ir = _compare("First Reinsurance Participation %", "Palms Part of Limit %")
    mapped = _mapped(47, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 0
    assert ir["template"] == "cross_field_compare"


def test_a_tenant_authored_compare_is_never_touched():
    """The author picked BOTH columns, so the pair IS the rule's intent."""
    ir = _compare("Deductible", "Occurrence Limit")
    mapped = _mapped(99, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 0
    assert ir["template"] == "cross_field_compare"


def test_other_library_classes_are_never_touched():
    ir = {"template": "required_field", "params": {"field": "loss_paid"}}
    mapped = _mapped(14, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 0
    assert ir["template"] == "required_field"


# ── shape guards ─────────────────────────────────────────────────────────────

def test_a_scaled_comparison_is_never_touched():
    """"A >= P% of B" compares two deliberately different quantities."""
    ir = _compare("Fronting Fee", "Gross Written Premium",
                  operator="*", factor=0.125)
    mapped = _mapped(3, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 0
    assert ir["template"] == "cross_field_compare"


def test_a_non_compare_template_is_never_touched():
    ir = {"template": "cross_field_math",
          "params": {"result_field": "total_incured", "left_field": "loss_paid",
                     "operator": "+", "right_field": "loss_resv"}}
    mapped = _mapped(3, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 0
    assert ir["template"] == "cross_field_math"


def test_already_unmapped_rule_is_not_double_counted():
    ir = {"template": None, "params": {}, "reason": "no column"}
    mapped = _mapped(25, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 0
    assert ir["reason"] == "no column"


def test_a_missing_operand_is_left_to_the_field_gate():
    ir = {"template": "cross_field_compare",
          "params": {"field": "total_incured", "op": "<="}}
    mapped = _mapped(25, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 0


def test_guard_no_ops_on_empty_input():
    assert _guard_same_subject_compares([], _RULES) == 0
    assert _guard_same_subject_compares(None, _RULES) == 0


# ── the unbound rule must reach a human, not the bin ─────────────────────────

def test_unbound_rule_reaches_the_review_queue_with_the_reason():
    from contract_upload_services.rule_normalizer import verify_and_build_ir_rule
    ir = _compare("total_incured", "loss_paid")
    ir["rule_name"] = "Closed Claims Must Have No Outstanding Reserves"
    assert _guard_same_subject_compares(_mapped(25, ir), _RULES) == 1
    out = verify_and_build_ir_rule(ir, {"clause_id": -25}, {}, None)
    assert out.get("route") == "review"
    assert "loss_paid" in (out.get("reason") or "")


# ── the closed-claim condition is part of the rule, not a detail ─────────────

def test_outstanding_on_closed_states_its_closed_scope():
    """An OPEN claim is supposed to carry a reserve, so the same comparison run
    over every row flags the whole open inventory. The class carries the condition
    in plain language for the mapper to bind to this programme's own status
    column — it is not left to be noticed in the rule's prose."""
    from contract_upload_services.generic_rule_library import _INTENT_BY_CLASS
    entry = _INTENT_BY_CLASS["OutstandingOnClosed"]
    assert len(entry) == 3, "no scope on the closed-claims rule"
    assert "closed" in entry[2].lower()


def test_closed_scope_reaches_the_mapper_as_the_intents_scope():
    from contract_upload_services.generic_rule_library import _build_intents
    rules = [{"id": 25, "rule_name": "Closed Claims Must Have No Outstanding Reserves",
              "severity": "Major", "class_name": "OutstandingOnClosed",
              "validation_logic": "once the claim is closed, the total incurred "
                                  "amount must not exceed the total paid amount"}]
    _clauses, intent_clfs = _build_intents(rules)
    intent = intent_clfs[0]["intents"][0]
    assert intent["operator"] == "cross_field_compare"
    assert "closed" in (intent["scope"] or "").lower()


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
