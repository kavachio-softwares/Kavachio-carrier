"""
test_amount_typecheck_pairs.py
──────────────────────────────
Tests for the AMOUNT type-check's handling of a column that writes its amounts as
a COMPOSITE — two amounts joined by a connector.

The case that motivated them: a "Layer" column of one bordereau holds excess-of
notation — "5000000 xs 45000000", "3000000 xs 2000000" — beside plain "0" rows.
Those cells are amount data (5m excess of 45m), but the check read them with the
single-amount reader, so all 122 layer rows were reported as '"Layer" expects a
numeric amount (digits, optional . , $ %), but found "5000000 xs 45000000"'.

The shape alone cannot settle it: a street address ("5625 CR 7410", "12750 Merit
Drive Suite 1000") splits into amount · connector · amount exactly as a layer
does, and in a column typed as an amount an address IS worth reporting. So the
question is asked of the COLUMN (`_amount_notation_values`): one connector used
by the majority of its unreadable values is a notation; a scatter of different
connectors is prose that happens to contain numbers. Nothing about how a layer or
an address is spelled is written into the code — the connector comes from the
data, and both halves are read by the platform's own number reader.

No LLM. DuckDB in-memory only; no DB writes.

Run standalone:  python contract_upload_services/tests/test_amount_typecheck_pairs.py
(or under pytest — each case asserts independently).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

# The compiler's catalog is DB-backed, so the service environment has to be
# present — load the service .env the same way main.py and scripts/ do. The SQL
# below runs in an in-memory DuckDB; no application data is read or written.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except ImportError:
    pass
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")

import duckdb                                                    # noqa: E402

from duckdb_validation import (                                  # noqa: E402
    _amount_notation_values, run_type_checks,
)

# The layer column that was reported as 122 malformed amounts, and the plain
# zeros it sits beside.
LAYERS = ["5000000 xs 45000000", "5000000 xs 5000000", "3000000 xs 2000000",
          "5000000 xs 10000000", "2000000 xs 3000000", "5,000,000 xs 5,000,000",
          "$5,000,000 xs $45,000,000"]
# Real addresses from bordereaux in the same workbook set — the shape a per-cell
# rule cannot tell apart from a layer.
ADDRESSES = ["5625 CR 7410", "12750 Merit Drive Suite 1000", "318 US 1",
             "20333 HWY 249", "55 E. Jackson Blvd Floor 14", "2730 N Hwy 360",
             "105 Fieldcrest Ave Ste 200", "88 Pine St FL 6"]


def _check(values):
    """The exceptions the amount type-check raises for a column holding these."""
    con = duckdb.connect()
    con.execute('CREATE TABLE "Sheet1" (__rowid BIGINT, "Amount" VARCHAR)')
    con.executemany('INSERT INTO "Sheet1" VALUES (?,?)',
                    [[i, v] for i, v in enumerate(values, 1)])
    exc = run_type_checks(con, {"Sheet1": {"columns": ["Amount"]}},
                          {"Sheet1": {"Amount": "number"}})
    con.close()
    return [e["actual_value"] for e in exc]


def _notation(values):
    con = duckdb.connect()
    try:
        return _amount_notation_values(con, values)
    finally:
        con.close()


def test_a_layer_column_raises_no_exceptions():
    """The reported bug, end to end through the pass that produced it."""
    assert _check(["0"] * 20 + LAYERS * 3) == []


def test_a_band_column_raises_no_exceptions():
    """Same notation, a different connector — discovered, not listed."""
    assert _check(["1000 - 2000", "1000 - 5000", "2,500 - 7,500"]) == []
    assert _check(["1000 to 2000", "1000 to 5000", "2500 to 7500"]) == []
    assert _check(["5000000 part of 10000000",
                   "2000000 part of 10000000",
                   "1000000 part of 5000000"]) == []


def test_addresses_in_an_amount_column_are_still_reported():
    """Identical SHAPE to a layer, but no one notation runs through the column —
    so every one of them stays a violation."""
    assert sorted(_check(ADDRESSES)) == sorted(ADDRESSES)


def test_junk_is_still_reported_beside_a_recognised_notation():
    """A column may hold a notation AND genuinely broken cells; only the notation
    is amount data."""
    junk = ["N/A", "not an amount", "5000000 xs abc"]
    assert sorted(_check(LAYERS * 3 + junk)) == sorted(junk)


def test_a_few_pair_shaped_cells_do_not_speak_for_a_column_of_junk():
    """The case a majority-of-SPLITTABLE rule got wrong on real data: an address
    column whose 46 unreadable cells include exactly two Texas farm roads. `fm`
    wins 2 out of the 2 values that split, but 2 out of 46 is not how the column
    writes amounts — both stay reported."""
    farm_roads = ["6220 FM 2920", "545 FM 1488"]
    others = ["10208 Hawthorne Place Drive", "10333 Clay Road",
              "104-248 S. Mulrennan Rd", "N/A", "TBD", "unit 4B"]
    assert _notation(farm_roads + others) == set()
    assert sorted(_check(farm_roads + others)) == sorted(farm_roads + others)


def test_plain_amount_columns_are_untouched():
    """Nothing about the ordinary path changes: readable amounts raise nothing,
    unreadable ones are still reported."""
    assert _check(["0", "1,250.00", "$3,000", "15%", "-4380", "($4,380.00)",
                   " 1 000.50 ", "£100"]) == []
    assert sorted(_check(["1,250.00", "N/A", "#N/A", "abc", "1-2-3", "--5"])) == \
        sorted(["N/A", "#N/A", "abc", "1-2-3", "--5"])


def test_a_minority_notation_is_not_the_columns_way_of_writing_amounts():
    """One layer-shaped cell among many unrelated unreadable ones proves nothing
    about the column, so it is left reported."""
    assert _notation(["5000000 xs 45000000"] + ADDRESSES) == set()


def test_the_notation_is_discovered_not_listed():
    """A connector the code has never been told about works the same way."""
    vals = ["5000000 ~~ 45000000", "3000000 ~~ 2000000", "1000000 ~~ 500000"]
    assert _notation(vals) == set(vals)


def test_halves_must_both_be_amounts():
    """The connector is only half the question — what it joins must be amounts,
    so a period stated as two dates is still the wrong type for the column."""
    assert _notation(["7/1/2023 - 6/30/2024", "1/1/2024 - 12/31/2024"]) == set()
    assert _notation(["5000000 xs abc", "3000000 xs abc"]) == set()


def test_no_values_no_notation():
    assert _notation([]) == set()
    assert _notation(["", "   ", None]) == set()


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
