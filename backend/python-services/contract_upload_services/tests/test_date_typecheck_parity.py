"""
test_date_typecheck_parity.py
─────────────────────────────
Tests for the deterministic DATE type-check and the guard that withdraws it.

The case that motivated them: five columns of one bordereau were reported as
"expects a valid date … but found 5000 / 3000000 / 3500000", and a sixth —
a genuine date column — as "but found 4/18/2019 12:00:00 AM". Two distinct
defects:

  * a column typed 'date' by the setup's canonical mapping but holding plain
    AMOUNTS kept its date check, because the only guards recognised money that
    was unmistakably formatted ("$500", "1,234.56") — a bare "5000" is neither
    money-shaped nor a year, so every row warned;
  * an Excel date column exported as text ("4/18/2019 12:00:00 AM") failed the
    parser, which knew the date spellings but not a trailing time of day, so
    every row of a perfectly valid column warned.

Both are fixed by asking ONE question with ONE parser: would the check itself
accept this value? `duckdb_validation.parses_as_date` is the Python twin of the
SQL the check runs, and the two MUST agree — a twin that drifts either silently
withdraws checks that work or keeps checks that cannot pass. The parity case
below is what holds them together.

No LLM. DuckDB in-memory only; no DB writes.

Run standalone:  python contract_upload_services/tests/test_date_typecheck_parity.py
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

import duckdb                                              # noqa: E402

from duckdb_validation import (                             # noqa: E402
    _typecheck_date_expr, parses_as_date,
)
from direct_routes import _samples_all_fail_date            # noqa: E402


# Every spelling the check must accept, and every value it must reject.
ACCEPT = [
    "2026-01-31", "4/18/2019", "04/18/2019", "20260131", "2026/01/31",
    "18-Apr-2019", "18 Apr 2019", "Apr 18, 2019",
    # …the same, carrying a time of day (an Excel date column read as text)
    "4/18/2019 12:00:00 AM", "5/1/2022 12:00:00 AM", "4/18/2019 13:45",
    "2019-04-18T00:00:00", "18-Apr-2019 00:00:00",
    # …and a period stated as two dates
    "7/1/2023 - 6/30/2024", "7/1/2023 to 6/30/2024",
]
REJECT = [
    "5000", "0", "3000000", "5000000", "3500000", "45678",
    "1245075",          # 7 digits: must NOT read as the compact date 1245-07-05
    "2023", "202301", "$1,234.56", "1,234.56", "Not Selected", "", "N/A",
    "-500.25", "7/1/2023 - garbage",
]


def _sql_accepts(values):
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (v VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?)", [[v] for v in values])
    rows = con.execute(f'SELECT v, {_typecheck_date_expr("v")} FROM t').fetchall()
    con.close()
    return {v: parsed is not None for v, parsed in rows}


def test_sql_check_accepts_every_supported_spelling():
    got = _sql_accepts(ACCEPT)
    bad = [v for v in ACCEPT if not got[v]]
    assert not bad, f"date check rejected valid dates: {bad}"


def test_sql_check_rejects_non_dates():
    got = _sql_accepts(REJECT)
    bad = [v for v in REJECT if got[v]]
    assert not bad, f"date check accepted non-dates: {bad}"


def test_python_twin_matches_the_sql_exactly():
    """The twin decides whether a column keeps its date check; if it drifts from
    the SQL, the guard withdraws checks that would have worked (or keeps ones
    that cannot pass)."""
    values = ACCEPT + REJECT
    sql = _sql_accepts(values)
    bad = [(v, sql[v], parses_as_date(v)) for v in values
           if sql[v] != parses_as_date(v)]
    assert not bad, f"SQL/Python disagree on {bad}"


def test_amount_column_typed_as_date_loses_the_check():
    for samples in (["5000", "0"], ["3000000", "Not Selected"],
                    ["3000000", "5000000"], ["3500000", "1245075"]):
        assert _samples_all_fail_date(samples), samples


def test_real_date_column_keeps_the_check():
    for samples in (["4/18/2019 12:00:00 AM", "5/1/2022 12:00:00 AM"],
                    ["2026-07-31", "2026-07-31"],
                    # one unreadable cell among readable ones is a DATA defect,
                    # not a mis-typed column — the check must stay to report it
                    ["4/18/2019 12:00:00 AM", "Policy Inception", "40258"]):
        assert not _samples_all_fail_date(samples), samples


def test_column_with_no_samples_keeps_its_classification():
    """Absence of data is never a mismatch — an empty column stays as typed."""
    assert not _samples_all_fail_date([])
    assert not _samples_all_fail_date(None)
    assert not _samples_all_fail_date(["", "  "])


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
