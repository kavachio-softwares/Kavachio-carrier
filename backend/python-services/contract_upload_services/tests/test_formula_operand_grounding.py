"""
test_formula_operand_grounding.py
─────────────────────────────────
A derived formula must be bound to the column the BORDEREAU agrees with, not to
whichever of several look-alike columns was named first.

THE FAILURE THIS PINS
─────────────────────
A bordereau reports one amount under several headings. On the Obsidian Platinum
Construction programme:

    Gross Written Collected | Gross Written Premium (Including TRIA) | … (Less TRIA)
              10,000        |            10,000                     |     10,000
               7,500        |             7,500                     |      7,500
             207,000        |           207,000                     |    207,000

Identical — because none of those policies bought terrorism cover. They part
company on the 37th row, and on 6 rows out of 424 in total. The formula
inference, which sees three sample values, therefore had no way to tell them
apart and wrote

    Earned Premium = Gross Written Premium (Including TRIA) − Unearned Premium

The file says otherwise: earned + unearned reconciles to the LESS-TRIA premium on
every one of its rows. So the shipped rule flagged exactly the 6 policies that
were right (RULE-3147, "6 affected policies") and could never have found a real
one. Three sibling rules on the same programme — the unearned identity, the quota
share and the TPA fee — were bound to the same wrong column for the same reason.

WHAT IS ASSERTED
────────────────
1. exporter._grounding_row_positions — the template parser captures, on top of
   the head rows, the earliest row that TELLS TWO IDENTICAL COLUMNS APART. That
   row is the entire evidence; without it every candidate fits.
2. rule_normalizer._ground_cross_field_math_operands — an identity that provably
   fails on those rows, and holds when ONE operand is swapped for another column
   of the same sheet, is repointed at that column. Everything else is left
   exactly as written: a formula that already holds, one that no column can
   rescue (a genuine data defect — the rule's whole purpose), one where either
   operand could be swapped, one with too little data, and the result column,
   which is never second-guessed.
3. _closest_named — when several columns are data-EQUIVALENT (a bordereau
   repeating one amount), the one whose name is closest to the column the mapper
   reached for is used, deterministically.

No DB and no LLM: every helper here is pure.

Run standalone:  python contract_upload_services/tests/test_formula_operand_grounding.py
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

import pandas as pd                                                 # noqa: E402

from contract_upload_services.output_schema import build_output_schema  # noqa: E402
from contract_upload_services.rule_normalizer import (               # noqa: E402
    _closest_named,
    _ground_cross_field_math_operands,
    _ground_multiplicative_base,
    _restate_derived_clause_text,
)
import exporter                                                      # noqa: E402


# ── the real Obsidian shape: three premium columns, identical until row 36 ────

_COLLECTED = "Gross Written Collected"
_INCL = "Gross Written Premium (Including TRIA)"
_LESS = "Gross Written Premium (Less TRIA)"
_EARNED = "Earned Premium"
_UNEARNED = "Unearned Premium"

# 5 head rows where the three premium columns agree, then the row where the
# policy bought terrorism cover and they do not.
_ROWS = [
    # collected,  incl,   less,   earned,    unearned
    (10000.0,   10000.0, 10000.0,    27.32,   9972.68),
    (7500.0,     7500.0,  7500.0,    61.48,   7438.52),
    (207000.0, 207000.0, 207000.0, 9049.18, 197950.82),
    (26718.0,   26718.0, 26718.0,   657.00,  26061.00),
    (8450.0,     8450.0,  8450.0,   507.92,   7942.08),
    (-2299.0,      50.0, -2299.0, -1250.00,  -1049.00),
]
_ORDER = [_COLLECTED, _INCL, _LESS, _EARNED, _UNEARNED]


def _fields(rows=_ORDER and _ROWS, order=_ORDER, sheet="Sheet1", depth=None):
    """Template fields the way _template_fields_from_structure builds them."""
    out = []
    for i, name in enumerate(order):
        vals = [f"{r[i]}" for r in rows][:depth] if depth else [f"{r[i]}" for r in rows]
        out.append({"name": name, "sheet": sheet,
                    "samples": vals[:3], "samples_all": vals[:5],
                    "row_samples": vals})
    return out


def _math(result, left, op, right, sheet_note=""):
    return {
        "template": "cross_field_math",
        "params": {"result_field": result, "left_field": left, "operator": op,
                   "right_field": right, "tolerance_pct": 1.0},
        "rule_name": f"{result} equals {left} {op} {right}",
        "rule_description": f"{result} must equal {left} {op} {right}{sheet_note}.",
        "error_message": f"{result} does not match {left} {op} {right}.",
    }


# ── 1. the parser captures the row that tells the look-alike columns apart ────

def test_the_splitting_row_is_captured():
    data = pd.DataFrame([list(r) for r in _ROWS] + [[1.0] * 5] * 30)
    rows = exporter._grounding_row_positions(data)
    assert rows[:5] == [0, 1, 2, 3, 4], f"head rows must come first, got {rows}"
    assert 5 in rows, f"the row that splits the premium columns is missing: {rows}"


def test_columns_that_never_agree_add_no_rows():
    """Nothing to disambiguate → the head rows are the whole sample."""
    data = pd.DataFrame({"a": range(50), "b": range(100, 150), "c": range(200, 250)})
    assert exporter._grounding_row_positions(data) == [0, 1, 2, 3, 4]


def test_the_row_set_is_bounded():
    """Every column identical to every other until its own late row — still capped."""
    n = 400
    cols = {}
    for c in range(30):
        col = [0.0] * n
        col[10 + c] = float(c + 1)          # each column splits off on its own row
        cols[f"c{c}"] = col
    rows = exporter._grounding_row_positions(pd.DataFrame(cols))
    assert len(rows) <= exporter.MAX_GROUNDING_ROWS
    assert rows == sorted(set(rows)), "rows must be ordered and unique"


def test_text_and_empty_columns_never_drive_the_choice():
    data = pd.DataFrame({"name": [f"policy {i}" for i in range(40)],
                         "note": [None] * 40})
    assert exporter._grounding_row_positions(data) == [0, 1, 2, 3, 4]


def test_a_short_sheet_is_safe():
    assert exporter._grounding_row_positions(pd.DataFrame()) == []
    assert exporter._grounding_row_positions(pd.DataFrame({"a": [1.0, 2.0]})) == [0, 1]


# ── 2. the identity is repointed at the column the data supports ──────────────

def test_the_wrong_premium_column_is_repointed():
    schema = build_output_schema(_fields())
    out = _ground_cross_field_math_operands(
        _math(_EARNED, _INCL, "-", _UNEARNED), schema)
    assert out["params"]["left_field"] == _LESS, out["params"]
    assert out["params"]["result_field"] == _EARNED
    assert out["params"]["right_field"] == _UNEARNED
    # what the reviewer reads must match what the SQL checks
    assert _LESS in out["rule_name"] and _INCL not in out["rule_name"]
    assert _INCL not in out["rule_description"]
    assert _INCL not in out["error_message"]


def test_a_formula_that_already_holds_is_untouched():
    schema = build_output_schema(_fields())
    ir = _math(_EARNED, _LESS, "-", _UNEARNED)
    assert _ground_cross_field_math_operands(ir, schema)["params"] == ir["params"]


def test_a_genuine_data_defect_is_still_reported():
    """The identity fails and NO column rescues it — that is the defect the rule
    exists to find, and refitting it to the data would erase it."""
    rows = [(a, b, c, d + (100.0 if i == 5 else 0.0), e)
            for i, (a, b, c, d, e) in enumerate(_ROWS)]
    # every premium column now disagrees with earned+unearned on the last row
    rows[5] = (-2299.0, 50.0, -2299.0, -1150.0, -1049.0)
    schema = build_output_schema(_fields(rows=rows))
    ir = _math(_EARNED, _LESS, "-", _UNEARNED)
    assert _ground_cross_field_math_operands(ir, schema)["params"] == ir["params"]


def test_an_ambiguous_repair_is_refused():
    """Either operand could be swapped to make it hold → the data does not say
    which one was mis-picked, so nothing is changed."""
    order = ["Sum", "A", "B", "A alt", "B alt"]
    #  Sum,   A,     B,   A alt, B alt      A alt / B alt copy their twin until…
    rows = [(11.0,  1.0, 10.0,   1.0, 10.0),
            (13.0,  2.0, 11.0,   2.0, 11.0),
            (15.0,  3.0, 12.0,   3.0, 12.0),
            (17.0,  4.0, 13.0,   4.0, 13.0),
            (19.0,  5.0, 14.0,   5.0, 14.0),
            # …the last row, where BOTH alternatives absorb the same difference:
            # Sum = A alt + B = 115, and Sum = A + B alt = 115, equally.
            (115.0, 6.0, 15.0, 100.0, 109.0)]
    schema = build_output_schema(_fields(rows=rows, order=order))
    # the ambiguity is real: each swap ON ITS OWN rescues the identity
    for slot, alt in (("left_field", "A alt"), ("right_field", "B alt")):
        ir = _math("Sum", "A", "+", "B")
        ir["params"][slot] = alt
        assert _ground_cross_field_math_operands(ir, schema)["params"] == ir["params"], \
            f"{slot}={alt} should already hold on this data"
    ir = _math("Sum", "A", "+", "B")
    got = _ground_cross_field_math_operands(ir, schema)
    assert got["params"] == ir["params"], got["params"]


def test_the_result_column_is_never_second_guessed():
    """Only the INPUTS may be repointed: swapping the validated column would
    silence the check on the column the rule is about."""
    schema = build_output_schema(_fields())
    out = _ground_cross_field_math_operands(
        _math(_EARNED, _INCL, "-", _UNEARNED), schema)
    assert out["params"]["result_field"] == _EARNED


def test_too_few_rows_decides_nothing():
    schema = build_output_schema(_fields(depth=2))
    ir = _math(_EARNED, _INCL, "-", _UNEARNED)
    assert _ground_cross_field_math_operands(ir, schema)["params"] == ir["params"]


def test_columns_on_different_sheets_are_not_compared():
    """Row 3 of one sheet is not row 3 of another, so there is nothing to compare
    row-wise — and such a formula does not compile into a per-row check anyway."""
    fields = _fields()
    for f in fields:
        if f["name"] == _UNEARNED:
            f["sheet"] = "Other"
    schema = build_output_schema(fields)
    ir = _math(_EARNED, _INCL, "-", _UNEARNED)
    assert _ground_cross_field_math_operands(ir, schema)["params"] == ir["params"]


def test_a_template_parsed_before_the_aligned_rows_still_works():
    """An older template carries only head samples. The check falls back to them,
    finds the premium columns indistinguishable there, and changes nothing —
    the behaviour that shipped before, not a crash."""
    fields = [{k: v for k, v in f.items() if k != "row_samples"}
              for f in _fields()]
    schema = build_output_schema(fields)
    ir = _math(_EARNED, _INCL, "-", _UNEARNED)
    assert _ground_cross_field_math_operands(ir, schema)["params"] == ir["params"]


def test_a_column_empty_on_the_aligned_rows_keeps_its_head_samples():
    """A sparsely-populated column is blank on the rows chosen to separate the
    others; its head samples are still real values and are still read."""
    fields = _fields()
    for f in fields:
        if f["name"] == _COLLECTED:
            f["row_samples"] = ["", "", "", "", "", ""]
    schema = build_output_schema(fields)
    assert schema.samples_for_grounding(_COLLECTED) == fields[0]["samples_all"]


def test_non_formula_rules_are_ignored():
    schema = build_output_schema(_fields())
    for ir in ({"template": "range_check", "params": {"field": _EARNED, "min": 0}},
               {"template": "cross_field_math", "params": {}},
               {"template": "cross_field_math",
                "params": {"result_field": _EARNED, "left_field": _INCL,
                           "operator": "^", "right_field": _UNEARNED}},
               {}, None):
        assert _ground_cross_field_math_operands(ir, schema) is ir


# ── 3. choosing between columns the data cannot tell apart ───────────────────

def test_the_closest_named_equivalent_column_wins():
    """`Gross Written Collected` reconciles just as well as `… (Less TRIA)` — both
    are correct checks. The one that shares the mapper's own words is used."""
    assert _closest_named(_INCL, [_COLLECTED, _LESS]) == _LESS
    assert _closest_named(_INCL, [_LESS, _COLLECTED]) == _LESS   # order-independent
    assert _closest_named(_INCL, [_COLLECTED]) == _COLLECTED
    assert _closest_named(_INCL, []) is None


def test_a_share_identity_picks_the_closest_equivalent_base_too():
    """Same question for "field = other × factor" (the quota share and the TPA
    fee on this programme), which had been left alone whenever more than one
    column reproduced the target."""
    order = [_COLLECTED, _INCL, _LESS, "QS Premium", _UNEARNED]
    rows = [r[:3] + (round(r[2] * 0.9, 2), r[4]) for r in _ROWS]
    schema = build_output_schema(_fields(rows=rows, order=order))
    ir = {"template": "cross_field_compare",
          "params": {"field": "QS Premium", "op": "=", "other_field": _INCL,
                     "operator": "*", "factor": 0.9, "tolerance": 0.01},
          "rule_name": f"QS Premium equals {_INCL} * 0.9"}
    out = _ground_multiplicative_base(ir, schema)
    assert out["params"]["other_field"] == _LESS, out["params"]
    assert _INCL not in out["rule_name"]


def test_a_share_identity_that_holds_is_untouched():
    order = [_COLLECTED, _INCL, _LESS, "QS Premium", _UNEARNED]
    rows = [r[:3] + (round(r[2] * 0.9, 2), r[4]) for r in _ROWS]
    schema = build_output_schema(_fields(rows=rows, order=order))
    ir = {"template": "cross_field_compare",
          "params": {"field": "QS Premium", "op": "=", "other_field": _LESS,
                     "operator": "*", "factor": 0.9, "tolerance": 0.01}}
    assert _ground_multiplicative_base(ir, schema)["params"] == ir["params"]


# ── 4. "where this comes from" follows the rule ──────────────────────────────

def test_a_derived_clause_text_is_restated():
    clause = {"text": f"[Derived formula] {_EARNED} equals {_INCL} - {_UNEARNED}"}
    out = _restate_derived_clause_text(
        clause, {"left_field": _INCL}, {"left_field": _LESS})
    assert out["text"] == f"[Derived formula] {_EARNED} equals {_LESS} - {_UNEARNED}"
    assert clause["text"].endswith(f"{_INCL} - {_UNEARNED}"), "must not mutate"


def test_a_contract_clause_is_never_rewritten():
    """A clause is the contract's own words, quoted — not ours to edit."""
    clause = {"text": f"The {_INCL} shall be reported monthly."}
    out = _restate_derived_clause_text(
        clause, {"left_field": _INCL}, {"left_field": _LESS})
    assert out is clause


def test_an_unchanged_rule_leaves_its_clause_alone():
    clause = {"text": f"[Derived formula] {_EARNED} equals {_LESS} - {_UNEARNED}"}
    assert _restate_derived_clause_text(
        clause, {"left_field": _LESS}, {"left_field": _LESS}) is clause
    assert _restate_derived_clause_text(None, {}, {}) is None


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
