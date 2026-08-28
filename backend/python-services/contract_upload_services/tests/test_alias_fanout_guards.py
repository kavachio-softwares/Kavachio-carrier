"""A rule must never be validated against a column it was not written for.

THE FAILURE THIS PINS
─────────────────────
`OutputSchema.field_aliases` fans a rule out to sheets that carry the same
logical column under a different header ("POL_NO" here, "Policy number" there).
It decided "same column" from `canonical_field` alone — and real templates use
that tag as a bucket: one Starstone template parks 15 unrelated claim amounts
under `commission_amount` and three programme columns under `program_name`.

So the standard check "Paid Loss Amount Must Not Exceed Incurred Loss Amount"
(total_paid <= total_incurred, on the claims sheet) was ALSO compiled against
the premium sheet, with BOTH of its fields rewritten to the single column that
shared the tag there. The arm compared that column with itself — and its
companion type check flagged every non-numeric cell of it. The reviewer saw

    Paid Loss Amount Must Not Exceed Incurred Loss Amount
    What this rule checks: total_paid must be no more than total_incurred
    Reason: Program ID must be a number, but found BB2.

— a valid programme code reported as a loss-figure breach.

Three independent guards, tested here:
  1. a canonical tag that names >1 column on ANY sheet cannot identify a column
  2. two columns holding different KINDS of value are not the same column
  3. an arm that folds two of a rule's own fields onto one column cannot express
     the rule, so it is never compiled
  4. and, for rules already compiled with the old behaviour, the collapsed arm
     is recognised and dropped from the cached query at validation time

Run:  python contract_upload_services/tests/test_alias_fanout_guards.py
(pytest is not installed in this venv; every test file carries its own runner.)
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from contract_upload_services.output_schema import OutputSchema      # noqa: E402
from contract_upload_services.rule_compiler import (                 # noqa: E402
    CompileError, collapsed_sheets_in_sql, compile_ir, drop_sheet_arms,
    is_not_numeric_reason, not_numeric_reason,
)

PAID_VS_INCURRED = {
    "template": "cross_field_compare",
    "params": {"field": "total_paid", "op": "<=", "other_field": "total_incurred"},
}


def _f(name, sheet, canon=None, samples=()):
    return {"name": name, "sheet": sheet, "canonical_field": canon,
            "samples": list(samples)[:3], "samples_all": list(samples)}


def _arms(sql):
    """(sheet, field, reason) per compiled UNION arm."""
    out = []
    for arm in sql.split("\nUNION ALL\n"):
        m = re.match(r"SELECT __rowid AS row_id, '([^']*)' AS sheet, "
                     r"'([^']*)' AS field, '([^']*)'", arm)
        if m:
            out.append((m.group(1), m.group(2), m.group(3)))
    return out


# ── guard 1: a bucket tag is not a column identity ───────────────────────────

def test_bucket_canonical_tag_makes_no_aliases():
    """The reported shape: one tag over many claim amounts on one sheet, and over
    one unrelated column on another."""
    fields = [
        _f("total_paid", "Claims", "commission_amount", ["100", "250"]),
        _f("total_incurred", "Claims", "commission_amount", ["400", "250"]),
        _f("expense_paid", "Claims", "commission_amount", ["10", "20"]),
        _f("Gross Commission %", "As of date", "commission_amount", ["0.2"]),
    ]
    assert OutputSchema(fields).field_aliases == {}


def test_one_to_one_tag_still_aliases():
    """The feature's own case must keep working: the same column, two spellings,
    exactly one of each per sheet."""
    fields = [
        _f("POL_NO", "Premium", "policy_number", ["P-1", "P-2"]),
        _f("pol_no", "Claims", "policy_number", ["P-3", "P-4"]),
    ]
    aliases = OutputSchema(fields).field_aliases
    assert aliases == {"POL_NO": {"Claims": "pol_no"},
                       "pol_no": {"Premium": "POL_NO"}}


# ── guard 2: different kind of value → different column ──────────────────────

def test_value_kind_mismatch_blocks_the_alias():
    fields = [
        _f("total_paid", "Claims", "x_amount", ["1200.50", "8000"]),
        _f("Program ID", "Premium", "x_amount", ["BB2", "BB2"]),
    ]
    assert OutputSchema(fields).field_aliases == {}


def test_value_kind_agreement_allows_the_alias():
    fields = [
        _f("total_paid", "Claims", "x_amount", ["1200.50", "8000"]),
        _f("Paid Amount", "Premium", "x_amount", ["990", "12,000"]),
    ]
    assert OutputSchema(fields).field_aliases == {
        "total_paid": {"Premium": "Paid Amount"},
        "Paid Amount": {"Claims": "total_paid"},
    }


def test_missing_samples_fail_open():
    """No sample data is no evidence — the alias a template already depends on
    must not disappear just because its columns were empty at parse time."""
    fields = [
        _f("total_paid", "Claims", "x_amount"),
        _f("Paid Amount", "Premium", "x_amount"),
    ]
    assert OutputSchema(fields).field_aliases == {
        "total_paid": {"Premium": "Paid Amount"},
        "Paid Amount": {"Claims": "total_paid"},
    }


# ── guard 3: an arm cannot fold two fields onto one column ───────────────────

def test_compile_skips_a_collapsing_alias_sheet():
    aliases = {"total_paid": {"Premium": "Program ID"},
               "total_incurred": {"Premium": "Program ID"}}
    f2s = {"total_paid": ["Claims", "Premium"], "total_incurred": ["Claims", "Premium"]}
    sql = compile_ir(PAID_VS_INCURRED, f2s, "Claims", aliases=aliases)
    sheets = {sheet for sheet, _, _ in _arms(sql)}
    assert sheets == {"Claims"}, sheets
    assert "Program ID" not in sql


def test_compile_keeps_a_sound_alias_sheet():
    aliases = {"total_paid": {"Sched B": "paid_amt"},
               "total_incurred": {"Sched B": "incurred_amt"}}
    f2s = {"total_paid": ["Claims", "Sched B"], "total_incurred": ["Claims", "Sched B"]}
    sql = compile_ir(PAID_VS_INCURRED, f2s, "Claims", aliases=aliases)
    assert {sheet for sheet, _, _ in _arms(sql)} == {"Claims", "Sched B"}
    assert "paid_amt must be <= incurred_amt" in sql


def test_compile_refuses_when_every_sheet_collapses():
    """Nothing sound to compile is a rule for the review queue, not a rule that
    quietly validates the wrong column."""
    aliases = {"total_paid": {"Premium": "Program ID"},
               "total_incurred": {"Premium": "Program ID"}}
    f2s = {"total_paid": ["Premium"], "total_incurred": ["Premium"]}
    try:
        compile_ir(PAID_VS_INCURRED, f2s, "Premium", aliases=aliases)
    except CompileError:
        return
    raise AssertionError("expected CompileError")


def test_single_field_rule_is_unaffected():
    """One field cannot collapse against itself — the guard must not touch the
    ordinary single-column rules, which are most of the estate."""
    ir = {"template": "max_limit", "params": {"field": "Limit", "max": 1000}}
    sql = compile_ir(ir, {"Limit": ["A", "B"]}, "A")
    assert {sheet for sheet, _, _ in _arms(sql)} == {"A", "B"}


# ── guard 4: rules already compiled the old way self-heal ────────────────────

def _legacy_sql():
    """What compile_ir produced for the reported rule BEFORE the guard."""
    from contract_upload_services.rule_compiler import _BUILDERS
    from contract_upload_services.rule_ir import remap_ir_fields
    bad = {"total_paid": {"Premium": "Program ID"},
           "total_incurred": {"Premium": "Program ID"}}
    parts = [_BUILDERS["cross_field_compare"]("Claims", PAID_VS_INCURRED["params"])]
    local = remap_ir_fields(PAID_VS_INCURRED,
                            lambda n: (bad.get(n) or {}).get("Premium"))
    parts.append(_BUILDERS["cross_field_compare"]("Premium", local["params"]))
    return "\nUNION ALL\n".join(parts)


def test_collapsed_arm_is_detected_and_dropped():
    sql = _legacy_sql()
    assert "Program ID must be a number" in sql          # the reported symptom
    collapsed = collapsed_sheets_in_sql(PAID_VS_INCURRED, sql)
    assert collapsed == ["Premium"], collapsed
    pruned = drop_sheet_arms(sql, collapsed)
    assert pruned and "Program ID" not in pruned
    assert {sheet for sheet, _, _ in _arms(pruned)} == {"Claims"}


def test_sound_sql_is_left_alone():
    aliases = {"total_paid": {"Sched B": "paid_amt"},
               "total_incurred": {"Sched B": "incurred_amt"}}
    f2s = {"total_paid": ["Claims", "Sched B"], "total_incurred": ["Claims", "Sched B"]}
    sql = compile_ir(PAID_VS_INCURRED, f2s, "Claims", aliases=aliases)
    assert collapsed_sheets_in_sql(PAID_VS_INCURRED, sql) == []
    assert drop_sheet_arms(sql, []) is None


def test_single_field_rule_never_looks_collapsed():
    ir = {"template": "max_limit", "params": {"field": "Limit", "max": 1000}}
    sql = compile_ir(ir, {"Limit": ["A"]}, "A")
    assert collapsed_sheets_in_sql(ir, sql) == []


def test_collapse_detection_never_raises():
    for junk_ir in (None, {}, {"template": "nope"},
                    {"template": "cross_field_compare", "params": None}):
        assert collapsed_sheets_in_sql(junk_ir, "SELECT 1") == []
    assert collapsed_sheets_in_sql(PAID_VS_INCURRED, "") == []


# ── the reason sentence has ONE definition ───────────────────────────────────

def test_not_numeric_reason_round_trips():
    """The SQL writes this sentence and the read path recognises it; they must be
    the same sentence or the re-titling silently stops working."""
    sql = compile_ir(PAID_VS_INCURRED,
                     {"total_paid": ["Claims"], "total_incurred": ["Claims"]}, "Claims")
    head = not_numeric_reason("total_paid", "\x00").split("\x00")[0]
    assert head in sql
    assert is_not_numeric_reason(
        not_numeric_reason("Program ID", "BB2"), "Program ID", "BB2")
    assert not is_not_numeric_reason("something else", "Program ID", "BB2")
    assert not is_not_numeric_reason(None, "Program ID", "BB2")


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
