"""A premium NET OF a rate is `base × (1 − rate)`, and nothing could express it.

THE FAILURE THIS PINS
─────────────────────
On the MSIG/Amwins XS Casualty sidecar the bordereau reports, per policy:

    WRITTEN PREMIUM   COMPANY CEDE   NET CEDED     PALMS (DIRECT)
        -20,066          0.130       -17,457.42      -3,491.484
        -45,000          0.230       -34,650.00      -6,930.000
       -322,770          0.230      -248,532.90     -49,706.580

`NET CEDED` is the written premium less the ceding commission the Company keeps —
`WRITTEN PREMIUM × (1 − COMPANY CEDE)`, exact to the cent on all 94 rows — and
`PALMS (DIRECT)` is the subscribing reinsurer's 20% of it. The run produced a rule
for PALMS and none at all for NET CEDED, because three things lined up:

  * `cross_field_math` is `result = left OP right` and `cross_field_compare` is
    `field = other OP factor`. Neither carries a constant term, so `A × (1 − B)`
    had no expressible form — the one shape everyday quota-share arithmetic needs.
  * the annotation deriver parses exactly two operands and one operator, so the
    flat spelling `WP - WP * CEDE` (3 operands) was dropped by a bare `continue`;
  * and had the model written the parenthesised spelling instead, it was WORSE
    than dropped: `_resolve_formula_col("1 - COMPANY CEDE")` fuzzy-matched
    `COMPANY CEDE` on two shared tokens and silently threw the "1 −" away, giving
    `NET CEDED = WRITTEN PREMIUM × COMPANY CEDE` — a rule that flags 93 of 94 rows.

WHAT IS ASSERTED
────────────────
1. an operand phrase that carries its own arithmetic is REFUSED, not fuzzy-matched
   (the silent mis-parse), while ordinary punctuated headers still resolve;
2. "(1 - <col>)" survives tokenising, reaches the IR as `right_complement`, and
   compiles to `(1.0 - …)` — checked by running the SQL in DuckDB, the engine the
   runtime uses;
3. the value-driven deriver finds `base × (1 − rate)` and `base × k` from the
   NUMBERS alone, picks the rate the data supports rather than a look-alike that
   merely fits the tolerance band, and stays silent when nothing verifies;
4. a division-only annotation is no longer classed as prose;
5. a complement rule is never merged into a plain identity by the algebra dedup.

Nothing here knows what a premium is: the column names in the pure-unit tests are
arbitrary and the arithmetic does the work.

Run:  PYTHONPATH=. python contract_upload_services/tests/test_rate_complement_formulas.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")
# The value-driven deriver is OFF in production (see derive_verified_rate_formulas):
# across the whole template estate it proposes far more than anyone reviewed. The
# tests that exercise it opt in here; `test_the_pass_is_off_unless_asked_for`
# clears this again so the default itself is pinned.
os.environ.setdefault("KAVACHIO_VERIFIED_FORMULAS", "1")

from contract_upload_services import validation_rule_generator as V   # noqa: E402
from contract_upload_services.rule_compiler import compile_ir         # noqa: E402
from contract_upload_services.rule_normalizer import (                # noqa: E402
    _consolidate_equivalent_formula_rules, _formula_identity_key,
)

# ── the real sidecar shape, first six policies ───────────────────────────────
_COLS = ["WRITTEN PREMIUM", "COMMISSION PERCENT", "COMMISSION AMT",
         "NET PREMIUM", "COMPANY CEDE", "NET CEDED", "PALMS (DIRECT)"]
_ROWS = [
    (-20066.0,  0.125,  -2508.25,  -17557.75, 0.130, -17457.42,  -3491.484),
    (-45000.0,  0.225, -10125.00,  -34875.00, 0.230, -34650.00,  -6930.000),
    (-322770.0, 0.225, -72623.25, -250146.75, 0.230, -248532.90, -49706.580),
    (-90000.0,  0.225, -20250.00,  -69750.00, 0.230, -69300.00,  -13860.000),
    (-25000.0,  0.225,  -5625.00,  -19375.00, 0.230, -19250.00,  -3850.000),
    (-45000.0,  0.210,  -9450.00,  -35550.00, 0.215, -35325.00,  -7065.000),
]


def _fields(rows=_ROWS, cols=_COLS, sheet="Sheet1"):
    return [{"name": n, "sheet": sheet,
             "samples": [repr(r[i]) for r in rows][:3],
             "samples_all": [repr(r[i]) for r in rows][:5],
             "row_samples": [repr(r[i]) for r in rows]}
            for i, n in enumerate(cols)]


def _names(fields):
    return [f["name"] for f in fields]


def _pairs(fields):
    return [(n, V._norm_col(n)) for n in _names(fields)]


def _emitted(entries):
    return {e["candidates"][0]["rule_name"]: e["candidates"][0] for e in entries}


# ── 1. the silent mis-parse ──────────────────────────────────────────────────

def test_an_operand_that_is_itself_an_expression_is_refused():
    f = _fields()
    assert V._resolve_formula_col("1 - COMPANY CEDE", _names(f), _pairs(f)) is None
    assert V._resolve_formula_col("WRITTEN PREMIUM * COMPANY CEDE",
                                  _names(f), _pairs(f)) is None


def test_ordinary_column_names_still_resolve():
    """The guard must not cost the fuzzy match its day job."""
    cols = _COLS + ["CLAIMS MADE/OCCURRENCE", "SELF-INSURED RETENTION"]
    f = _fields(rows=[r + (1.0, 1.0) for r in _ROWS], cols=cols)
    n, p = _names(f), _pairs(f)
    assert V._resolve_formula_col("CLAIMS MADE/OCCURRENCE", n, p) == "CLAIMS MADE/OCCURRENCE"
    assert V._resolve_formula_col("SELF-INSURED RETENTION", n, p) == "SELF-INSURED RETENTION"
    assert V._resolve_formula_col("WRITTEN PREM", n, p) == "WRITTEN PREMIUM"   # abbreviation
    assert V._resolve_formula_col("NET CEDED", n, p) == "NET CEDED"


# ── 2. the complement round-trips: text → token → IR → SQL ───────────────────

def test_the_complement_tokenises_as_its_own_operand():
    f = _fields()
    toks, _ = V._tokenize_formula_expr(
        "WRITTEN PREMIUM * (1 - COMPANY CEDE)", _names(f), _pairs(f))
    assert toks == [("col", "WRITTEN PREMIUM"), ("op", "*"), ("colc", "COMPANY CEDE")]


def test_a_percent_stored_rate_keeps_its_scaling_inside_the_complement():
    f = _fields()
    toks, div100 = V._tokenize_formula_expr(
        "WRITTEN PREMIUM * (1 - COMPANY CEDE / 100)", _names(f), _pairs(f))
    assert toks[-1] == ("colc_pct", "COMPANY CEDE")
    assert div100 is False, "the inner /100 belongs to the rate, not the product"


def test_the_annotation_deriver_emits_the_complement_flag():
    fs = _fields()
    for f in fs:
        if f["name"] == "NET CEDED":
            f["formula"] = "WRITTEN PREMIUM * (1 - COMPANY CEDE)"
    ir = _emitted(V.derive_annotation_formula_entries([], fs))
    name = "NET CEDED equals WRITTEN PREMIUM * (1 - COMPANY CEDE)"
    assert name in ir, list(ir)
    p = ir[name]["params"]
    assert p["result_field"] == "NET CEDED"
    assert p["left_field"] == "WRITTEN PREMIUM"
    assert p["right_field"] == "COMPANY CEDE"
    assert p["right_complement"] is True
    assert not p.get("right_is_percent")


def _duckdb_flagged(ir, rows=_ROWS, cols=_COLS):
    """Rows the compiled rule flags — the runtime loads a BDX as text, so the
    fixture table is VARCHAR and the SQL does its own numeric parsing."""
    import duckdb
    con = duckdb.connect()
    con.execute('CREATE TABLE "Sheet1" (__rowid BIGINT, '
                + ", ".join(f'"{c}" VARCHAR' for c in cols) + ")")
    con.executemany(
        f'INSERT INTO "Sheet1" VALUES ({",".join("?" * (len(cols) + 1))})',
        [(i, *[repr(v) for v in r]) for i, r in enumerate(rows)])
    out = con.execute(
        compile_ir(ir, {c: "Sheet1" for c in cols}, default_sheet="Sheet1")
    ).fetchall()
    con.close()
    return {r[0] for r in out}


def test_the_compiled_complement_rule_clears_every_correct_row():
    fs = _fields()
    for f in fs:
        if f["name"] == "NET CEDED":
            f["formula"] = "WRITTEN PREMIUM * (1 - COMPANY CEDE)"
    ir = list(_emitted(V.derive_annotation_formula_entries([], fs)).values())[0]
    assert _duckdb_flagged(ir) == set()


def test_the_exception_message_names_the_formula_that_was_checked():
    """The reason a reviewer reads is the only place the rule explains itself. If
    it says "must equal <base> * <rate>" on a rule that tested the complement, the
    reviewer reconciles against a figure the rule never computed."""
    ir = {"template": "cross_field_math",
          "params": {"result_field": "NET CEDED", "left_field": "WRITTEN PREMIUM",
                     "operator": "*", "right_field": "COMPANY CEDE",
                     "right_complement": True, "tolerance_pct": 1.0}}
    sql = compile_ir(ir, {c: "Sheet1" for c in
                          ("NET CEDED", "WRITTEN PREMIUM", "COMPANY CEDE")},
                     default_sheet="Sheet1")
    assert "NET CEDED must equal WRITTEN PREMIUM * (1 - COMPANY CEDE)" in sql
    assert "must equal WRITTEN PREMIUM * COMPANY CEDE" not in sql


def test_the_compiled_complement_rule_still_catches_a_broken_row():
    """Dropping the complement is what this whole change is about — so the rule
    must fire when the reported figure is the UNCOMPLEMENTED product."""
    rows = list(_ROWS)
    wp, cede = rows[2][0], rows[2][4]
    rows[2] = rows[2][:5] + (round(wp * cede, 2),) + rows[2][6:]   # the wrong maths
    fs = _fields(rows=rows)
    for f in fs:
        if f["name"] == "NET CEDED":
            f["formula"] = "WRITTEN PREMIUM * (1 - COMPANY CEDE)"
    ir = list(_emitted(V.derive_annotation_formula_entries([], fs)).values())[0]
    assert _duckdb_flagged(ir, rows=rows) == {2}


# ── 2b. behaviour of the shipped rule: signs, percent storage, tolerance ─────

_NC = {"template": "cross_field_math",
       "params": {"result_field": "NET CEDED", "left_field": "WRITTEN PREMIUM",
                  "operator": "*", "right_field": "COMPANY CEDE",
                  "right_complement": True, "tolerance_pct": 1.0}}


def _run(rows, ir=_NC, cols=("WRITTEN PREMIUM", "COMPANY CEDE", "NET CEDED")):
    import duckdb
    con = duckdb.connect()
    con.execute('CREATE TABLE "Sheet1" (__rowid BIGINT, '
                + ", ".join(f'"{c}" VARCHAR' for c in cols) + ")")
    con.executemany(
        f'INSERT INTO "Sheet1" VALUES ({",".join("?" * (len(cols) + 1))})',
        [(i, *[str(v) for v in r]) for i, r in enumerate(rows)])
    out = con.execute(
        compile_ir(ir, {c: "Sheet1" for c in cols}, default_sheet="Sheet1")
    ).fetchall()
    con.close()
    return {r[0] for r in out}


def test_positive_values_reconcile():
    # 20,066 x (1 - 0.13) = 17,457.42
    assert _run([(20066, 0.13, 17457.42), (45000, 0.23, 34650.00)]) == set()


def test_negative_values_reconcile():
    """This whole bordereau is negative — return premium on incremental removals.
    A sign slip anywhere in the numeric parsing would flag the entire book."""
    assert _run([(-20066, 0.13, -17457.42), (-45000, 0.23, -34650.00),
                 (-322770, 0.23, -248532.90)]) == set()


def test_accounting_negatives_in_parentheses_are_understood():
    assert _run([("(20,066)", 0.13, "(17,457.42)")]) == set()


def test_a_sign_flip_on_the_result_is_caught():
    assert _run([(-20066, 0.13, 17457.42)]) == {0}


def test_a_percent_stored_rate_is_scaled_before_the_complement():
    """COMPANY CEDE held as 13, not 0.13 → (1 - 13/100), never (1 - 13)."""
    ir = {"template": "cross_field_math",
          "params": {**_NC["params"], "right_is_percent": True}}
    assert _run([(-20066, 13, -17457.42), (-45000, 23, -34650.00)], ir=ir) == set()
    # and the unscaled reading is genuinely different, so the flag means something
    assert _run([(-20066, 13, -17457.42)]) == {0}


def test_an_incorrect_net_ceded_is_flagged():
    # the uncomplemented product — the exact error the complement exists to catch
    assert _run([(-20066, 0.13, round(-20066 * 0.13, 2))]) == {0}


def test_a_rounding_difference_inside_the_band_passes():
    # 1% of 17,457.42 is ~174; a few cents must never raise an exception
    assert _run([(-20066, 0.13, -17457.40), (-20066, 0.13, -17457.44)]) == set()


def test_a_difference_beyond_the_band_is_flagged():
    assert _run([(-20066, 0.13, -17457.42 * 1.05)]) == {0}


def test_a_blank_or_non_numeric_row_does_not_reconcile_silently():
    """A missing operand is skipped by the identity check, but a value that is
    present and non-numeric is reported — it cannot be validated either way."""
    assert _run([(-20066, 0.13, "")]) == set()
    assert _run([(-20066, 0.13, "n/a")]) == {0}


# ── 3. the value-driven deriver ──────────────────────────────────────────────

def test_the_relationships_are_found_from_the_numbers_alone():
    """No formula annotation, no clause, no LLM — headers that match none of the
    name gates ("AMT" is not "amount", "COMPANY CEDE" carries no '%')."""
    got = _emitted(V.derive_verified_rate_formulas([], _fields()))
    assert "NET CEDED equals WRITTEN PREMIUM × (1 - COMPANY CEDE)" in got, list(got)
    assert "PALMS (DIRECT) equals NET CEDED * 0.2" in got, list(got)
    assert got["NET CEDED equals WRITTEN PREMIUM × (1 - COMPANY CEDE)"]["params"][
        "right_complement"] is True


def test_the_rate_is_chosen_exactly_not_merely_within_tolerance():
    """COMPANY CEDE (0.130) and COMMISSION PERCENT (0.125) sit half a percent
    apart, inside the 1% band the emitted rule runs with. Picking by the band
    would bind NET CEDED to the commission rate — a rule that reconciles to the
    wrong column and can never see a half-percent error."""
    got = _emitted(V.derive_verified_rate_formulas([], _fields()))
    ir = got["NET CEDED equals WRITTEN PREMIUM × (1 - COMPANY CEDE)"]
    assert ir["params"]["right_field"] == "COMPANY CEDE"


def test_a_column_already_defined_is_left_to_its_existing_rule():
    prior = [{"candidates": [{"template": "cross_field_math",
                              "params": {"result_field": "NET CEDED"}}]}]
    got = _emitted(V.derive_verified_rate_formulas(prior, _fields()))
    assert not any(k.startswith("NET CEDED") for k in got), list(got)


def test_nothing_is_emitted_when_the_numbers_support_nothing():
    rows = [(1000.0, 0.1, 3.0, 7.0, 0.2, 11.0, 13.0),
            (2000.0, 0.3, 5.0, 17.0, 0.4, 19.0, 23.0),
            (3000.0, 0.5, 29.0, 31.0, 0.6, 37.0, 41.0),
            (4000.0, 0.7, 43.0, 47.0, 0.8, 53.0, 59.0)]
    assert V.derive_verified_rate_formulas([], _fields(rows=rows)) == []


def test_a_constant_column_cannot_pin_a_formula():
    """Every row identical: any rate 'verifies'. Two distinct bases are required."""
    rows = [_ROWS[1]] * 5
    got = _emitted(V.derive_verified_rate_formulas([], _fields(rows=rows)))
    assert got == {}, list(got)


def test_too_few_rows_decide_nothing():
    got = _emitted(V.derive_verified_rate_formulas([], _fields(rows=_ROWS[:2])))
    assert got == {}, list(got)


def test_a_plain_copy_is_not_reported_as_a_ratio():
    """k = 1 is a definitional copy, derived elsewhere; restating it as `× 1`
    would turn every duplicated column into arithmetic."""
    cols = _COLS + ["NET CEDED COPY"]
    rows = [r + (r[5],) for r in _ROWS]
    got = _emitted(V.derive_verified_rate_formulas([], _fields(rows=rows, cols=cols)))
    assert not any(" * 1" in k or " * 1.0" in k for k in got), list(got)


def test_a_rate_column_is_never_the_RESULT_of_a_share():
    """A rate that happens to rise in step with a premium over the sampled rows is
    proportional to it — 0.2/0.4/0.6 against 1000/2000/3000 exactly so. Only an
    AMOUNT can be a share of a base, or the pass invents "the cede rate equals the
    premium × 0.0002"."""
    rows = [(1000.0, 0.1, 3.0, 7.0, 0.2, 11.0, 13.0),
            (2000.0, 0.3, 5.0, 17.0, 0.4, 19.0, 23.0),
            (3000.0, 0.5, 29.0, 31.0, 0.6, 37.0, 41.0),
            (4000.0, 0.7, 43.0, 47.0, 0.8, 53.0, 59.0)]
    got = _emitted(V.derive_verified_rate_formulas([], _fields(rows=rows)))
    assert not any(k.startswith("COMPANY CEDE") or k.startswith("COMMISSION PERCENT")
                   for k in got), list(got)


def test_columns_on_different_sheets_are_never_combined():
    fs = _fields()
    for f in fs:
        if f["name"] == "COMPANY CEDE":
            f["sheet"] = "Other"
    got = _emitted(V.derive_verified_rate_formulas([], fs))
    assert not any("COMPANY CEDE" in k for k in got), list(got)


def test_the_pass_is_off_unless_asked_for():
    """The default is what ships. With the flag unset the pass must propose
    NOTHING, even on data it can reconcile perfectly — a template estate is far
    wider than the sheets it was written against, and on that scale exact
    agreement over a few sampled rows is reached by coincidence (a date stored as
    an Excel serial reconciles against anything constant)."""
    saved = os.environ.pop("KAVACHIO_VERIFIED_FORMULAS", None)
    try:
        assert V.derive_verified_rate_formulas([], _fields()) == []
        for val in ("0", "", "true", "yes"):
            os.environ["KAVACHIO_VERIFIED_FORMULAS"] = val
            assert V.derive_verified_rate_formulas([], _fields()) == [], val
        os.environ["KAVACHIO_VERIFIED_FORMULAS"] = "1"
        assert V.derive_verified_rate_formulas([], _fields()), "explicit 1 must enable it"
    finally:
        if saved is None:
            os.environ.pop("KAVACHIO_VERIFIED_FORMULAS", None)
        else:
            os.environ["KAVACHIO_VERIFIED_FORMULAS"] = saved


# ── 4. the division gate ─────────────────────────────────────────────────────

def test_a_quotient_is_an_arithmetic_formula():
    assert V._looks_arithmetic_formula("PALMS (DIRECT) / 0.2")
    assert V._looks_arithmetic_formula("NET CEDED / 100")
    assert V._looks_arithmetic_formula("A / B")


def test_prose_and_plain_headers_are_still_not_formulas():
    for note in ("Validate zip code w/ state", "CLAIMS MADE/OCCURRENCE",
                 "Must be populated", ""):
        assert not V._looks_arithmetic_formula(note), note


# ── 5. the algebra dedup must not touch a complement ─────────────────────────

def test_a_complement_identity_is_never_merged():
    def _rule(params, name):
        ir = {"template": "cross_field_math", "params": params,
              "rule_name": name, "severity": "warning", "confidence": 1.0}
        return {"rule_name": name, "ir": ir, "severity": "warning"}

    comp = _rule({"result_field": "A", "left_field": "B", "operator": "*",
                  "right_field": "R", "right_complement": True,
                  "tolerance_pct": 1.0}, "A equals B * (1 - R)")
    plain = _rule({"result_field": "A", "left_field": "B", "operator": "*",
                   "right_field": "R", "tolerance_pct": 1.0}, "A equals B * R")
    assert _formula_identity_key(comp["ir"]) is None
    assert _formula_identity_key(plain["ir"]) is not None
    assert len(_consolidate_equivalent_formula_rules([comp, plain])) == 2


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'OK'} — {failures} failure(s)")
    sys.exit(1 if failures else 0)
