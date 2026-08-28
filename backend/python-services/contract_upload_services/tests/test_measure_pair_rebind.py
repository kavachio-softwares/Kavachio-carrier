"""
test_measure_pair_rebind.py
───────────────────────────
Tests for the MEASURE-PAIR re-bind: when a bordereau reports claim money per
COMPONENT and therefore has no single rolled-up "total paid" column, the standard
paid-vs-incurred check must still ship — bound to the pairs of columns the
bordereau DOES report — instead of disappearing.

The case that motivated them: contract 106 (and 79 before it), a claims bordereau
whose money columns are  med_ / ind_ / loss_ / dcc_ / aoe_  ×  paid / resv /
incurred, plus ONE rolled-up `total_incured`. With no "total paid" column to bind,
the mapper elected `loss_paid` and proposed

    loss_paid <= total_incured

which weighs a rolled-up total against one component of it. `_guard_same_subject
_compares` correctly refuses that pair — and then contract 106 shipped with NO
paid-vs-incurred rule at all, while contract 79 (generated before the guard) had
shipped the wrong one.

Asserted here: `generic_rule_library._rebind_unbound_measure_compares`, which reads
the pairs off the output template's own structure (two columns whose names differ
only in the measure word report the same amount) and emits one check per pair,
inheriting the direction, operator, severity and scope from the mapper's own
proposal.

No DB and no LLM: the re-binder is pure.

Run standalone:  python contract_upload_services/tests/test_measure_pair_rebind.py
(or under pytest — each case asserts independently).
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

from contract_upload_services.generic_rule_library import (   # noqa: E402
    _guard_same_subject_compares,
    _measure_pairs,
    _measure_role,
    _rebind_unbound_measure_compares,
)

_RULES = [
    {"id": 3,  "class_name": "ClaimOverpayment"},
    {"id": 25, "class_name": "OutstandingOnClosed"},
    {"id": 47, "class_name": "PalmsPctShare"},
    {"id": 99, "class_name": "CompareFields"},      # tenant-authored building block
]

# The real contract-106 / 79 claims template: five components × three measures,
# one rolled-up incurred total, and NO total paid column.
_COMPONENT_TEMPLATE = [{"name": n, "samples": ["0", "1500.25", "0"]} for n in (
    "med_paid", "med_resv", "med_incurred",
    "ind_paid", "ind_resv", "ind_incurred",
    "loss_paid", "loss_resv", "loss_incurred",
    "dcc_paid", "dcc_resv", "dcc_incurred",
    "aoe_paid", "aoe_resv", "aoe_incurred",
    "total_incured",
)] + [{"name": "POL_NO", "samples": ["GL0001", "GL0002"]},
      {"name": "open_closed", "samples": ["Open", "Closed"]}]


def _mapped(library_rule_id, ir):
    """One post-Call-3 entry for the library rule whose id is `library_rule_id`
    (the synthetic clause_id is the NEGATED library id — see _build_intents)."""
    return [{"clause": {"clause_id": -library_rule_id}, "candidates": [ir]}]


def _compare(field, other, **extra):
    params = {"field": field, "op": "<=", "other_field": other}
    params.update(extra)
    return {"template": "cross_field_compare", "params": params,
            "rule_name": "Paid Loss Amount Must Not Exceed Incurred Loss Amount",
            "rule_description": "Amounts already paid on a claim cannot be "
                                "greater than the total incurred.",
            "severity": "critical"}


def _bound(entry):
    return [c for c in entry["candidates"] if c.get("template")]


# ── the failure this pins ────────────────────────────────────────────────────

def test_guard_rejection_is_rebound_onto_every_component_pair():
    """The exact contract-106 shape: the guard unbinds loss_paid <= total_incured,
    and the check comes back as one rule per amount reported in both measures."""
    ir = _compare("loss_paid", "total_incured")
    mapped = _mapped(3, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 1
    assert ir["template"] is None

    assert _rebind_unbound_measure_compares(mapped, _RULES, _COMPONENT_TEMPLATE) == 5
    got = {(c["params"]["field"], c["params"]["op"], c["params"]["other_field"])
           for c in _bound(mapped[0])}
    assert got == {
        ("aoe_paid",  "<=", "aoe_incurred"),
        ("dcc_paid",  "<=", "dcc_incurred"),
        ("ind_paid",  "<=", "ind_incurred"),
        ("loss_paid", "<=", "loss_incurred"),
        ("med_paid",  "<=", "med_incurred"),
    }
    # the mis-bound proposal is REPLACED, so it no longer reaches the review queue
    assert len(mapped[0]["candidates"]) == 5
    for c in _bound(mapped[0]):
        assert c["template"] == "cross_field_compare"
        assert "reason" not in c
        assert c["severity"] == "critical"


def test_rebound_rules_carry_distinct_names_and_messages():
    ir = _compare("loss_paid", "total_incured")
    mapped = _mapped(3, ir)
    _guard_same_subject_compares(mapped, _RULES)
    _rebind_unbound_measure_compares(mapped, _RULES, _COMPONENT_TEMPLATE)
    names = [c["rule_name"] for c in _bound(mapped[0])]
    assert len(set(names)) == 5, names
    for name in names:
        assert name.startswith("Paid Loss Amount Must Not Exceed "
                               "Incurred Loss Amount — ")
    one = next(c for c in _bound(mapped[0])
               if c["params"]["field"] == "dcc_paid")
    assert one["error_message"] == "dcc_paid must not exceed dcc_incurred."
    assert "dcc_paid must not exceed dcc_incurred" in one["rule_description"]


def test_direction_and_scope_are_inherited_not_re_decided():
    """The closed-claims rule reads incurred <= paid, scoped to closed claims. Both
    the ORDER of the two measures and the scope come from the mapper's proposal —
    the re-binder decides only which columns."""
    ir = _compare("total_incured", "loss_paid")
    ir["params"]["scope"] = {"open_closed": "Closed"}
    ir["rule_name"] = "Closed Claims Must Have No Outstanding Reserves"
    mapped = _mapped(25, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 1

    assert _rebind_unbound_measure_compares(mapped, _RULES, _COMPONENT_TEMPLATE) == 5
    for c in _bound(mapped[0]):
        assert c["params"]["field"].endswith("_incurred")
        assert c["params"]["other_field"].endswith("_paid")
        assert c["params"]["scope"] == {"open_closed": "Closed"}
    # each rule owns its scope — pruning one must not touch the others
    first = _bound(mapped[0])[0]
    first["params"]["scope"]["open_closed"] = "MUTATED"
    assert _bound(mapped[0])[1]["params"]["scope"] == {"open_closed": "Closed"}


# ── nothing that already works is disturbed ──────────────────────────────────

def test_a_correctly_bound_rule_is_never_touched():
    ir = _compare("loss_paid", "loss_incurred")
    mapped = _mapped(3, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 0
    assert _rebind_unbound_measure_compares(mapped, _RULES, _COMPONENT_TEMPLATE) == 0
    assert mapped[0]["candidates"] == [ir]


def test_a_template_with_one_clean_pair_yields_one_rule():
    """The ordinary bordereau: one total paid, one total incurred. Were the mapper
    ever to leave it unbound, the re-bind produces exactly the one rule."""
    template = [{"name": "total_paid_amt", "samples": ["12000"]},
                {"name": "total_incurr_amt", "samples": ["15000"]}]
    ir = _compare("Highest Value Unit", "total_incurr_amt")
    ir["template"] = None
    ir["params"]["field"] = "total_paid_amt"     # role-readable, wrong subject
    mapped = _mapped(3, ir)
    assert _rebind_unbound_measure_compares(mapped, _RULES, template) == 1
    c = _bound(mapped[0])[0]
    assert (c["params"]["field"], c["params"]["other_field"]) == (
        "total_paid_amt", "total_incurr_amt")


def test_other_library_classes_and_tenant_rules_are_never_touched():
    for rule_id in (47, 99):
        ir = _compare("loss_paid", "total_incured")
        ir["template"] = None
        mapped = _mapped(rule_id, ir)
        assert _rebind_unbound_measure_compares(
            mapped, _RULES, _COMPONENT_TEMPLATE) == 0
        assert mapped[0]["candidates"] == [ir]


# ── the mapper's OTHER answer: it declines outright ──────────────────────────

def test_a_declined_mapping_is_rebound_from_the_class_reading():
    """Told there is no total-paid column, the mapper's other answer is to propose
    nothing at all. The direction then comes from the library class — paid is the
    measure that must not exceed the other — and the check still ships."""
    ir = {"template": None, "params": {},
          "rule_name": "Paid Loss Amount Must Not Exceed Incurred Loss Amount",
          "reason": "No 'total_paid' field to compare against 'total_incured'.",
          "severity": "critical"}
    mapped = _mapped(3, ir)
    assert _rebind_unbound_measure_compares(mapped, _RULES, _COMPONENT_TEMPLATE) == 5
    for c in _bound(mapped[0]):
        assert c["params"]["op"] == "<="
        assert c["params"]["field"].endswith("_paid")
        assert c["params"]["other_field"].endswith("_incurred")
        assert "reason" not in c


def test_a_declined_SCOPED_rule_is_left_in_review():
    """The closed-claims check only holds once a claim is CLOSED. With no proposal
    there is no scope bound to a column, and an unscoped version would flag the
    whole open inventory — so it stays where the mapper left it."""
    ir = {"template": None, "params": {},
          "rule_name": "Closed Claims Must Have No Outstanding Reserves",
          "reason": "No 'total_paid' field to compare against 'total_incured'."}
    mapped = _mapped(25, ir)
    assert _rebind_unbound_measure_compares(mapped, _RULES, _COMPONENT_TEMPLATE) == 0
    assert mapped[0]["candidates"] == [ir]


def test_an_unreadable_proposal_falls_back_the_same_way():
    """The bordereau with no claim columns at all: the rule was bound to a premium
    column and a unit count, so there is no direction to INHERIT — but the class
    still knows which way the check points, and this template has real pairs."""
    ir = _compare("Highest Value Unit", "Commission Amount")
    mapped = _mapped(3, ir)
    assert _guard_same_subject_compares(mapped, _RULES) == 1
    assert _rebind_unbound_measure_compares(mapped, _RULES, _COMPONENT_TEMPLATE) == 5
    assert all(c["params"]["field"].endswith("_paid") for c in _bound(mapped[0]))

    scoped = _compare("Highest Value Unit", "Commission Amount")
    mapped = _mapped(25, scoped)
    assert _guard_same_subject_compares(mapped, _RULES) == 1
    assert _rebind_unbound_measure_compares(mapped, _RULES, _COMPONENT_TEMPLATE) == 0
    assert "reason" in mapped[0]["candidates"][0]


def test_two_operands_of_the_same_measure_fall_back_to_the_class():
    ir = _compare("loss_paid", "dcc_paid")
    ir["template"] = None
    mapped = _mapped(3, ir)
    assert _rebind_unbound_measure_compares(mapped, _RULES, _COMPONENT_TEMPLATE) == 5


def test_a_scoped_class_still_rebinds_when_the_mapper_bound_its_scope():
    """The complement of the case above: the proposal DOES carry the closed-claim
    condition, so the check can be re-bound and the scope travels with it."""
    ir = _compare("total_incured", "loss_paid")
    ir["params"]["scope"] = {"open_closed": "Closed"}
    mapped = _mapped(25, ir)
    _guard_same_subject_compares(mapped, _RULES)
    assert _rebind_unbound_measure_compares(mapped, _RULES, _COMPONENT_TEMPLATE) == 5


def test_a_template_with_no_measure_pair_at_all_changes_nothing():
    template = [{"name": "total_incured", "samples": ["15000"]},
                {"name": "Gross Written Premium", "samples": ["1000"]}]
    ir = _compare("loss_paid", "total_incured")
    ir["template"] = None
    mapped = _mapped(3, ir)
    assert _rebind_unbound_measure_compares(mapped, _RULES, template) == 0
    assert mapped[0]["candidates"] == [ir]


def test_rebinder_no_ops_on_empty_input():
    assert _rebind_unbound_measure_compares([], _RULES, _COMPONENT_TEMPLATE) == 0
    assert _rebind_unbound_measure_compares(None, _RULES, _COMPONENT_TEMPLATE) == 0
    assert _rebind_unbound_measure_compares(
        _mapped(3, _compare("a", "b")), _RULES, None) == 0


# ── how the pairs are read off the template ──────────────────────────────────

def test_component_pairs_are_read_off_the_template():
    pairs = _measure_pairs(_COMPONENT_TEMPLATE)
    assert set(pairs) == {("aoe",), ("dcc",), ("ind",), ("loss",), ("med",)}
    assert pairs[("med",)] == {"paid": "med_paid", "incurred": "med_incurred"}
    # the rolled-up incurred has no paid counterpart, so it forms no pair
    assert ("total",) not in pairs


def test_camelcase_and_spaced_names_pair_up():
    template = [{"name": "PaidLossAmount"}, {"name": "IncurredLossAmount"},
                {"name": "Total Paid Amount"}, {"name": "Total Incurred Amount"}]
    pairs = _measure_pairs(template)
    assert pairs[("amount", "loss")] == {"paid": "PaidLossAmount",
                                         "incurred": "IncurredLossAmount"}
    assert pairs[("amount", "total")] == {"paid": "Total Paid Amount",
                                          "incurred": "Total Incurred Amount"}


def test_a_misspelt_incurred_still_reads_as_the_incurred_measure():
    for spelling in ("total_incured", "TotalIncurredAmt", "incur_amt",
                     "Incurred Loss"):
        assert _measure_role(spelling) == "incurred", spelling


def test_a_word_that_merely_contains_a_measure_is_not_one():
    for name in ("Unpaid Balance", "Repayment Plan", "Prepaid Expense Ratio"):
        assert _measure_role(name) is None, name


def test_a_column_naming_both_measures_is_no_operand():
    assert _measure_role("paid_and_incurred_total") is None


def test_an_ambiguous_subject_is_skipped_rather_than_guessed():
    """Two paid columns for one subject — which one the check should use is not
    ours to decide."""
    template = [{"name": "loss_paid"}, {"name": "loss_payments"},
                {"name": "loss_incurred"}]
    assert _measure_pairs(template) == {}


def test_a_date_column_carrying_a_measure_word_is_not_an_amount():
    template = [{"name": "paid_date", "samples": ["2025-03-04", "2025-06-11"]},
                {"name": "incurred_date", "samples": ["2025-03-01"]},
                {"name": "med_paid", "samples": ["100"]},
                {"name": "med_incurred", "samples": ["150"]}]
    assert set(_measure_pairs(template)) == {("med",)}


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
