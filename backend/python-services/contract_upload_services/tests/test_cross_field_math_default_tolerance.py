"""
test_cross_field_math_default_tolerance.py
──────────────────────────────────────────
A cross_field_math rule bound without `tolerance_pct` (the generic library binds
operands only) used to compile to a 0% band — "> 0.0 * ABS(...)" — so every
one-cent rounding difference between a reported amount and its formula was a
violation. Pinned here, by running the compiled SQL in an in-memory DuckDB:

  * no tolerance_pct   → the fleet default band (same value the generator stamps
                         on derived formulas), plus a one-unit floor;
  * tolerance_pct = 0  → no percent band, but the one-unit floor still holds;
  * explicit tolerance → respected as before;
  * the floor follows the rule's comparison precision (`decimals`).

Pure — see _offline.py (no DB) — and no model calls.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _offline                                                  # noqa: E402,F401

import duckdb                                                    # noqa: E402

from contract_upload_services import rule_compiler as rc         # noqa: E402
from contract_upload_services import validation_rule_generator as vrg  # noqa: E402


# result = left - right, e.g. a net amount = gross - deduction. Synthetic names.
_BASE = {"result_field": "Net", "left_field": "Gross", "operator": "-",
         "right_field": "Deduction"}


def _flagged(params, rows):
    """rows: (rowid, gross, deduction, net) → set of flagged row ids."""
    con = duckdb.connect(":memory:")
    con.execute('CREATE TABLE "S" (__rowid INTEGER, "Gross" VARCHAR, '
                '"Deduction" VARCHAR, "Net" VARCHAR)')
    con.executemany('INSERT INTO "S" VALUES (?,?,?,?)', rows)
    sql = rc._b_cross_field_math("S", params)
    return {r[0] for r in con.execute(sql).fetchall()}


def test_missing_tolerance_ignores_cent_rounding_but_flags_real_gap():
    rows = [
        (0, "1000.00", "235.00", "765.00"),   # exact
        (1, "1000.00", "235.00", "765.01"),   # +0.01
        (2, "1000.00", "235.00", "764.99"),   # -0.01
        (3, "1000.00", "235.00", "803.25"),   # +5%
        (4, "1000.00", "235.00", "726.75"),   # -5%
        (5, "0.75", "0.25", "0.51"),          # +0.01 on a tiny amount (1% < a cent)
    ]
    assert _flagged(dict(_BASE), rows) == {3, 4}


def test_missing_tolerance_compiles_like_the_generator_default():
    """The compiler's fallback must be the generator's own default, not a copy
    that can drift."""
    implicit = rc._b_cross_field_math("S", dict(_BASE))
    explicit = rc._b_cross_field_math(
        "S", dict(_BASE, tolerance_pct=vrg.DEFAULT_CROSS_FIELD_TOLERANCE_PCT))
    assert implicit == explicit
    assert "> 0.0 * ABS(" not in implicit


def test_blank_tolerance_is_treated_as_missing():
    rows = [(0, "1000", "235", "770")]        # 0.65% off → inside the default 1%
    assert _flagged(dict(_BASE, tolerance_pct=""), rows) == set()
    assert _flagged(dict(_BASE, tolerance_pct=None), rows) == set()


def test_explicit_zero_keeps_only_the_one_cent_floor():
    params = dict(_BASE, tolerance_pct=0)
    rows = [
        (0, "1000.00", "235.00", "765.01"),   # one cent → floor, not flagged
        (1, "1000.00", "235.00", "765.02"),   # two cents → no % band, flagged
        (2, "1000.00", "235.00", "770.00"),   # 0.65% → flagged (0 means 0%)
    ]
    assert _flagged(params, rows) == {1, 2}


def test_explicit_tolerance_is_respected():
    params = dict(_BASE, tolerance_pct=2.0)
    rows = [
        (0, "1000", "235", "780"),            # ~1.96% → inside 2%
        (1, "1000", "235", "790"),            # ~3.27% → outside
    ]
    assert _flagged(params, rows) == {1}


def test_floor_follows_rule_precision():
    params = dict(_BASE, tolerance_pct=0, decimals=0)
    rows = [
        (0, "1000", "235", "766"),            # one whole unit → floor
        (1, "1000", "235", "767"),            # two units → flagged
    ]
    assert _flagged(params, rows) == {1}


def test_banded_rule_still_zones_past_the_floor():
    params = dict(_BASE, tolerance_pct=1.0, reject_pct=5.0)
    con = duckdb.connect(":memory:")
    con.execute('CREATE TABLE "S" (__rowid INTEGER, "Gross" VARCHAR, '
                '"Deduction" VARCHAR, "Net" VARCHAR)')
    con.executemany('INSERT INTO "S" VALUES (?,?,?,?)', [
        (0, "1000", "235", "765.01"),         # floor → compliant
        (1, "1000", "235", "788"),            # 3% → warning
        (2, "1000", "235", "900"),            # 17.6% → violation
    ])
    sql = rc._b_cross_field_math("S", params)
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    got = {r[cols.index("row_id")]: r[cols.index("zone")] for r in cur.fetchall()}
    assert got == {1: "warning", 2: "violation"}
