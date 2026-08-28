"""One arithmetic identity should produce one exception, not one per variable.

An equation between money columns can be written once per variable it contains.
A bordereau carrying Collected / Earned / Unearned Premium yields three formulas
that are the SAME equation:

    Earned    = Collected − Unearned
    Unearned  = Collected − Earned
    Collected = Earned + Unearned

Every row that breaks the identity breaks all three, so a live upload reported
the same six rows three times over — eighteen exceptions for six discrepancies.
The formula derivers each dedup by RESULT COLUMN, which cannot see that these
are rearrangements of one another, so all three shipped.

`_consolidate_equivalent_formula_rules` keys a rule on the ALGEBRA rather than
on its spelling. These tests pin the two properties that make that safe:

  * rearrangements of one equation collapse to one rule, and the survivor still
    flags exactly the rows the discarded spellings flagged (proved by running
    the compiled SQL in DuckDB, the engine the runtime uses);
  * equations that merely LOOK alike — a different constant, a different
    column, a percent-scaled operand, a different scope, an inequality — are
    left alone.

Nothing in the pass or in these tests knows what a premium is; the column names
below are arbitrary and the algebra is what does the work.

Run:  python contract_upload_services/tests/test_formula_identity_dedup.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from contract_upload_services.rule_normalizer import (   # noqa: E402
    _consolidate_equivalent_formula_rules, _formula_identity_key,
)


# ---------------------------------------------------------------- builders

def _rule(ir, name, severity="warning", confidence=1.0):
    ir = dict(ir, rule_name=name, severity=severity, confidence=confidence)
    return {"rule_name": name, "ir": ir, "template": ir["template"],
            "severity": severity, "rule_status": "active",
            "rule_spec": {"ir": ir}, "rule_description": name}


def _math(result, left, op, right, name=None, **kw):
    params = {"result_field": result, "left_field": left, "operator": op,
              "right_field": right, "tolerance_pct": 1.0}
    params.update({k: v for k, v in kw.items()
                   if k not in ("name", "severity", "confidence")})
    return _rule({"template": "cross_field_math", "params": params},
                 name or f"{result} equals {left} {op} {right}",
                 severity=kw.get("severity", "warning"),
                 confidence=kw.get("confidence", 1.0))


def _cmp(field, other, operator=None, factor=None, op="=", name=None, **kw):
    params = {"field": field, "op": op, "other_field": other, "tolerance": 0.01}
    if operator:
        params.update({"operator": operator, "factor": factor})
    params.update({k: v for k, v in kw.items()
                   if k not in ("name", "severity", "confidence")})
    tail = f" {operator} {factor}" if operator else ""
    return _rule({"template": "cross_field_compare", "params": params},
                 name or f"{field} {op} {other}{tail}",
                 severity=kw.get("severity", "warning"),
                 confidence=kw.get("confidence", 1.0))


def _names(rules):
    return [r["rule_name"] for r in rules]


# ------------------------------------------------- the reported bug, exactly

#: The three spellings the AI formula inference produced for one identity.
TRIO = [
    _math("Earned Premium", "Collected Premium", "-", "Unearned Premium"),
    _math("Unearned Premium", "Collected Premium", "-", "Earned Premium"),
    _math("Collected Premium", "Earned Premium", "+", "Unearned Premium"),
]


def test_one_equation_written_three_ways_becomes_one_rule():
    out = _consolidate_equivalent_formula_rules(list(TRIO))
    assert len(out) == 1, _names(out)
    # …and the survivor is the first, so the result does not depend on which
    # order the derivers happened to emit them in.
    assert _names(out) == [TRIO[0]["rule_name"]]


def test_every_rearrangement_shares_one_key():
    keys = {_formula_identity_key(r["ir"]) for r in TRIO}
    assert len(keys) == 1, keys
    assert None not in keys


def test_the_surviving_rule_still_flags_every_broken_row():
    """The whole point: dedup must remove REPORTS, never COVERAGE.

    The rows are the real numbers from the upload that raised this (Earned,
    Unearned, Collected). Six of them break the identity. Whichever spelling
    survives has to flag those six and only those six."""
    import duckdb
    from contract_upload_services.rule_compiler import compile_ir

    rows = [
        (1,   913.241095890411,   87.75890410958903,  1001.0),
        (2,  2236.30447761194,     6.695522388059544, 2243.0),
        (3,  1148.561194029851,    3.43880597014936,  1152.0),
        (4,  3376.693150684932,  586.3068493150681,   3963.0),
        (5, 10220.35068493151,   407.6493150684928,  10628.0),
        (6, 12486.0,               0.0,              12486.0),
        (7, -1148.561194029851,   -3.43880597014936, -1152.0),
        (8,  -349.0,               0.0,                 0.0),   # broken
        (9,    68.56164383561644, 932.4383561643835,    0.0),   # broken
        (10,  -63.82465753424658,  -0.1753424657534239, 0.0),   # broken
        (11,  998.2575342465753,    2.742465753424653, 1001.0),
        (12, -1261.183561643836,  -84.81643835616433, -1346.0),
        (13,    0.0,             11669.0,                0.0),  # broken
        (14, 10628.0,                0.0,            10628.0),
        (15, 12486.0,                0.0,            12486.0),
        (16,  201.0,                 0.0,                0.0),  # broken
        (17,  184.0,                 0.0,                0.0),  # broken
        (18, -1152.0,                0.0,            -1152.0),
        (19,  1152.0,                0.0,             1152.0),
        (20,  3713.276712328767,  249.7232876712328,  3963.0),
        (21,  2243.0,                0.0,             2243.0),
    ]
    broken = {8, 9, 10, 13, 16, 17}

    # The runtime loads a BDX as text and the compiled SQL does its own numeric
    # parsing, so the fixture table is VARCHAR too — same shape as production.
    con = duckdb.connect()
    con.execute('CREATE TABLE "Sheet1" (__rowid BIGINT, "Earned Premium" VARCHAR, '
                '"Unearned Premium" VARCHAR, "Collected Premium" VARCHAR)')
    con.executemany('INSERT INTO "Sheet1" VALUES (?, ?, ?, ?)',
                    [(r[0], repr(r[1]), repr(r[2]), repr(r[3])) for r in rows])
    field_to_sheets = {c: "Sheet1" for c in
                       ("Earned Premium", "Unearned Premium", "Collected Premium")}

    def _flagged(rule):
        sql = compile_ir(rule["ir"], field_to_sheets, default_sheet="Sheet1")
        return {r[0] for r in con.execute(sql).fetchall()}

    before = [_flagged(r) for r in TRIO]
    # All three spellings really do report the same rows — that is the bug.
    assert before[0] == before[1] == before[2] == broken, before

    out = _consolidate_equivalent_formula_rules(list(TRIO))
    assert _flagged(out[0]) == broken
    con.close()


# ------------------------------------------------ rearrangement, other shapes

def test_a_product_and_its_quotient_are_one_equation():
    rules = [_math("Amount", "Base", "*", "Rate"),
             _math("Base", "Amount", "/", "Rate")]
    assert len(_consolidate_equivalent_formula_rules(rules)) == 1


def test_a_scaled_copy_and_its_inverse_are_one_equation():
    # "A = B × 0.705" and "B = A ÷ 0.705" pin the same ratio.
    rules = [_cmp("A", "B", "*", 0.705), _cmp("B", "A", "/", 0.705)]
    assert len(_consolidate_equivalent_formula_rules(rules)) == 1


def test_a_plain_copy_matches_its_times_one_spelling():
    rules = [_cmp("A", "B"), _cmp("B", "A", "*", 1.0)]
    assert len(_consolidate_equivalent_formula_rules(rules)) == 1


def test_an_offset_and_its_mirror_are_one_equation():
    rules = [_cmp("A", "B", "+", 25.0), _cmp("B", "A", "-", 25.0)]
    assert len(_consolidate_equivalent_formula_rules(rules)) == 1


# ------------------------------------------------------ what must NOT collapse

def test_a_different_constant_is_a_different_equation():
    # Two live rules on one column with different participations: a genuine
    # conflict for another pass to resolve — never silently merged here.
    rules = [_cmp("Share", "Total", "*", 0.475), _cmp("Share", "Total", "*", 0.1)]
    assert len(_consolidate_equivalent_formula_rules(rules)) == 2


def test_two_columns_sharing_one_base_are_different_equations():
    rules = [_cmp("Written Share", "Collected", "*", 0.705),
             _cmp("Collected Share", "Collected", "*", 0.705)]
    assert len(_consolidate_equivalent_formula_rules(rules)) == 2


def test_a_percent_stored_operand_is_not_its_unscaled_twin():
    rules = [_math("Amount", "Base", "*", "Rate"),
             _math("Amount", "Base", "*", "Rate", right_is_percent=True,
                   name="Amount equals Base * Rate / 100")]
    assert len(_consolidate_equivalent_formula_rules(rules)) == 2


def test_an_inequality_is_never_merged_into_the_identity():
    # A ceiling and a definition on the same pair are different constraints.
    rules = [_cmp("A", "B", "*", 0.5), _cmp("A", "B", "*", 0.5, op="<=")]
    assert len(_consolidate_equivalent_formula_rules(rules)) == 2
    assert _formula_identity_key(rules[1]["ir"]) is None


def test_the_same_equation_under_different_scopes_stays_twice():
    a = _math("Net", "Gross", "-", "Fee")
    b = _math("Gross", "Net", "+", "Fee")
    b["ir"]["params"]["scope"] = {"State": {"op": "=", "value": "CA"}}
    out = _consolidate_equivalent_formula_rules([a, b])
    assert len(out) == 2, _names(out)


def test_a_referral_flavoured_identity_is_kept_apart():
    a = _math("Net", "Gross", "-", "Fee")
    b = _math("Gross", "Net", "+", "Fee")
    b["ir"]["is_referral"] = True
    assert len(_consolidate_equivalent_formula_rules([a, b])) == 2


def test_unrelated_rules_pass_through_untouched():
    others = [
        _rule({"template": "value_in_set",
               "params": {"field": "Carrier", "allowed": ["X"]}}, "enum"),
        _rule({"template": "required_field", "params": {"field": "Policy"}}, "req"),
        _rule({"template": "max_limit",
               "params": {"field": "Occ Limit", "max": 5000000}}, "cap"),
    ]
    out = _consolidate_equivalent_formula_rules(list(others))
    assert out == others


def test_a_lone_formula_is_returned_as_is():
    one = [_math("Net", "Gross", "-", "Fee")]
    assert _consolidate_equivalent_formula_rules(list(one)) == one


# -------------------------------------------------------- survivor selection

def test_the_stronger_severity_survives_a_merge():
    weak = _math("Net", "Gross", "-", "Fee", severity="warning")
    strong = _math("Gross", "Net", "+", "Fee", severity="critical")
    out = _consolidate_equivalent_formula_rules([weak, strong])
    assert _names(out) == [strong["rule_name"]]
    # …and order does not change the outcome.
    out = _consolidate_equivalent_formula_rules([strong, weak])
    assert _names(out) == [strong["rule_name"]]


def test_confidence_breaks_a_severity_tie():
    low = _math("Net", "Gross", "-", "Fee", confidence=0.4)
    high = _math("Gross", "Net", "+", "Fee", confidence=0.95)
    out = _consolidate_equivalent_formula_rules([low, high])
    assert _names(out) == [high["rule_name"]]


def test_a_malformed_formula_is_left_alone():
    # A non-string field reference must not raise here and must not be merged.
    bad = _math("Net", "Gross", "-", "Fee")
    bad["ir"]["params"]["right_field"] = 0.28
    assert _formula_identity_key(bad["ir"]) is None
    good = _math("Net", "Gross", "-", "Fee")
    assert len(_consolidate_equivalent_formula_rules([bad, good])) == 2


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}: {exc}")
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    sys.exit(1 if failures else 0)
