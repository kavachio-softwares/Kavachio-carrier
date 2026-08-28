"""
test_empty_date_cell.py
───────────────────────
A date column's EMPTY cells must not be reported as malformed dates.

THE FAILURE THIS PINS
─────────────────────
On the Proverity PL bordereau (upload 507) the review screen showed

    Type check — date · Retro Date · 13 affected policies
    "Retro Date" expects a valid date (e.g. 2026-01-31), but found "00:00:00".

Those 13 policies simply have no retro date. A spreadsheet stores a date as a
number of days, so a cell holding no date holds zero — and a zero rendered
through a time format comes out as "00:00:00", midnight of a day that was never
there. It is the same absence as the 58 blank cells in the very same column,
just written down: there is no date in it to be malformed, and nothing a
reviewer could correct by reading it.

WHAT IS ASSERTED
────────────────
1. `is_empty_date_cell` recognises the midnight spellings a spreadsheet emits
   (24-hour and 12-hour, with or without seconds) and NOTHING else — a real time
   of day ("09:30:00") lost a date it once had and is still a defect.
2. The type-check pass skips those cells in a DATE column, exactly as it skips a
   blank, and still reports a genuinely broken date in the same column.
3. THE SCOPE THE FIX MUST KEEP: the same text in an AMOUNT column is still
   reported. The Proverity file has one of those too — "Rate Change" holds
   "00:00:00" on two rows and is checked as an amount — and it must keep warning.
4. The classification guards treat an empty date cell as NO EVIDENCE either way,
   so a column whose sampled rows happen to have no date keeps its date check for
   the rows that do.

No LLM, no DB: DuckDB in-memory only.

Run standalone:  python contract_upload_services/tests/test_empty_date_cell.py
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

import duckdb                                                   # noqa: E402

from duckdb_validation import (                                 # noqa: E402
    is_empty_date_cell, parses_as_date, run_type_checks,
)
from direct_routes import (                                     # noqa: E402
    _samples_all_fail_date, _samples_all_parse_date,
)


# What a spreadsheet writes when a date cell holds nothing …
EMPTY = ["00:00:00", "0:00:00", "00:00", "0:00", "00:00:00.000",
         "12:00:00 AM", "12:00 AM", "12:00:00 am", " 00:00:00 "]
# … and what it does NOT: a real time of day, a real date, anything else.
NOT_EMPTY = ["09:30:00", "00:00:01", "00:01", "23:59:59", "12:00:00 PM",
             "2026-01-31", "4/18/2019", "4/18/2019 12:00:00 AM", "TBD", "0",
             "1899-12-29 23:14:38.400000"]


def test_only_a_zero_time_of_day_is_an_empty_date():
    for v in EMPTY:
        assert is_empty_date_cell(v), f"{v!r} should read as an empty date cell"
    for v in NOT_EMPTY:
        assert not is_empty_date_cell(v), f"{v!r} must NOT read as an empty cell"
    assert not is_empty_date_cell(None)
    assert not is_empty_date_cell("")


def test_an_empty_date_cell_is_still_not_a_date():
    """The parser is unchanged — the cell is SKIPPED as an absence, never
    accepted as a valid date (which would let it satisfy a date rule)."""
    for v in EMPTY:
        assert not parses_as_date(v), v


# ── the type-check pass ──────────────────────────────────────────────────────

def _run(rows, kinds):
    """rows = [{column: value}], kinds = {column: 'date'|'number'}."""
    cols = list(kinds)
    con = duckdb.connect()
    coldefs = ", ".join(f'"{c}" VARCHAR' for c in cols)
    con.execute(f'CREATE TABLE "Sheet1" (__rowid INTEGER, {coldefs})')
    con.executemany(
        f'INSERT INTO "Sheet1" VALUES ({", ".join(["?"] * (len(cols) + 1))})',
        [[i] + [r.get(c) for c in cols] for i, r in enumerate(rows, 1)])
    try:
        return run_type_checks(con, {"Sheet1": {"columns": cols}},
                               {"Sheet1": kinds})
    finally:
        con.close()


def test_a_date_column_no_longer_flags_its_empty_cells():
    rows = [{"Retro Date": "1995-01-01 00:00:00"},
            {"Retro Date": None},
            {"Retro Date": "00:00:00"},
            {"Retro Date": "00:00:00"},
            {"Retro Date": "4/18/2019 12:00:00 AM"}]
    assert _run(rows, {"Retro Date": "date"}) == []


def test_a_broken_date_in_the_same_column_is_still_reported():
    rows = [{"Retro Date": "00:00:00"},
            {"Retro Date": "TBD"},
            {"Retro Date": "09:30:00"},
            {"Retro Date": "2026-01-31"}]
    got = sorted(e["actual_value"] for e in _run(rows, {"Retro Date": "date"}))
    assert got == ["09:30:00", "TBD"], got


def test_an_amount_column_still_flags_the_same_text():
    """The allowance is for DATE columns only — nothing else changes."""
    rows = [{"Rate Change": "00:00:00"},
            {"Rate Change": "00:00:00"},
            {"Rate Change": "0.15"}]
    got = [e["actual_value"] for e in _run(rows, {"Rate Change": "number"})]
    assert got == ["00:00:00", "00:00:00"], got


def test_the_two_columns_side_by_side():
    """Both kinds in one pass: the date column is quiet, the amount column is
    not — the exact shape of the bordereau that motivated this."""
    rows = [{"Retro Date": "00:00:00", "Rate Change": "00:00:00"},
            {"Retro Date": "2026-01-31", "Rate Change": "0.15"}]
    got = [(e["column"], e["actual_value"])
           for e in _run(rows, {"Retro Date": "date", "Rate Change": "number"})]
    assert got == [("Rate Change", "00:00:00")], got


# ── the classification guards ────────────────────────────────────────────────

def test_empty_cells_never_prove_a_date_column_wrong():
    """A column sampled only on rows that have no date keeps its date check —
    the empty cells say nothing about it either way."""
    assert not _samples_all_fail_date(["00:00:00", "00:00:00"])
    assert not _samples_all_parse_date(["00:00:00", "00:00:00"])
    # a column of genuine non-dates still withdraws the check, as before
    assert _samples_all_fail_date(["5000", "3000000"])
    # …and one of real dates still proves itself, empty cells among them
    assert _samples_all_parse_date(["2026-01-31", "00:00:00", "4/18/2019"])
    assert not _samples_all_fail_date(["2026-01-31", "00:00:00"])


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
