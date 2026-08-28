"""
test_numeric_reader.py
──────────────────────
`rule_compiler.numeric_expr` is the one place the platform decides whether a cell
holds an AMOUNT. Both the rule engine (`_num`, used by every numeric comparison
and formula) and the type-check pass (`duckdb_validation._typecheck_num_expr`)
read numbers through it, so the two can never disagree about the same cell.

What went wrong: a bordereau exported from Excel writes a negative amount in the
Accounting/Currency format — "($4,380.00)" — and the reader only stripped ',',
'$' and ' '. The leftover parentheses failed the cast, so a perfectly valid
premium was reported as "expects a numeric amount … but found ($4,380.00)", and
every numeric rule on that column silently skipped the row.

The check must stay sharp in the other direction too: "N/A", "#N/A", "(abc)" and
"TBD" are genuinely not amounts and must still be reported.

Run standalone:  python contract_upload_services/tests/test_numeric_reader.py
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

from contract_upload_services.rule_compiler import numeric_expr   # noqa: E402


def _read(values):
    """Each value as the platform reads it: a float, or None when unreadable."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t(v VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?)", [[v] for v in values])
    rows = dict(con.execute(f'SELECT v, {numeric_expr("v")} FROM t').fetchall())
    con.close()
    return rows


def test_accounting_negative_is_a_negative_amount():
    got = _read(["($4,380.00)", "($1,810.00)", "(4380)", "(0)"])
    assert got["($4,380.00)"] == -4380.00, got
    assert got["($1,810.00)"] == -1810.00, got
    assert got["(4380)"] == -4380.0, got
    assert got["(0)"] == 0, got


def test_ordinary_decorated_amounts_still_read():
    got = _read(["$4,380.00", "1,250.00", "$3,000", "15%", "-4380", "-$100",
                 "$-100", " 1 000.50 ", " 1000", "0"])
    assert got["$4,380.00"] == 4380.0, got
    assert got["1,250.00"] == 1250.0, got
    assert got["$3,000"] == 3000.0, got
    assert got["15%"] == 15.0, got
    assert got["-4380"] == -4380.0, got
    assert got["-$100"] == -100.0 and got["$-100"] == -100.0, got
    assert got[" 1 000.50 "] == 1000.5, got
    assert got["0"] == 0, got


def test_unicode_spaces_are_read_as_thousands_separators():
    # Excel writes the thousands separator as a non-breaking / narrow space in
    # several locales, and the SQL engine's own \s is ASCII-only — so these need
    # the Unicode SEPARATOR category to be readable at all.
    got = _read(["1 000", "1 000", "1 000"])
    assert got["1 000"] == 1000.0, got   # non-breaking space
    assert got["1 000"] == 1000.0, got   # narrow non-breaking space
    assert got["1 000"] == 1000.0, got   # thin space


def test_currency_is_read_by_unicode_category_not_a_list():
    # A currency the code has never been told about must still work.
    got = _read(["£100", "€1.234", "₹1,00,000", "¥500", "₪75", "₦20"])
    assert got["£100"] == 100.0, got
    assert got["€1.234"] == 1.234, got
    assert got["₹1,00,000"] == 100000.0, got
    assert got["¥500"] == 500.0, got
    assert got["₪75"] == 75.0, got
    assert got["₦20"] == 20.0, got


def test_values_that_are_not_amounts_are_still_reported():
    bad = ["N/A", "#N/A", "TBD", "abc", "(abc)", "", "   ", "()", "--5", "1-2-3"]
    got = _read(bad)
    still_flagged = [v for v in bad if got[v] is None]
    assert still_flagged == bad, f"stopped flagging: {set(bad) - set(still_flagged)}"


def test_the_type_check_reads_numbers_the_same_way_as_the_rules():
    """One reader, so a SINGLE amount can never be "a valid amount" to one and
    "malformed" to the other.

    Asserted on the VALUES both expressions produce rather than on the SQL text:
    the type check adds its own tolerance on top of this reader (a layer/range
    stated as two amounts — see test_amount_typecheck_pairs.py), exactly as the
    date check tolerates a period stated as two dates. That tolerance may only
    ADD readable cells; every cell the rules can read must still read the same."""
    sys.path.insert(0, os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")))
    from duckdb_validation import _typecheck_num_expr
    values = ["($4,380.00)", "$4,380.00", "1,250.00", "15%", "-4380", "0",
              " 1 000.50 ", "£100", "N/A", "#N/A", "abc", "(abc)", "1-2-3", "--5"]
    con = duckdb.connect()
    con.execute("CREATE TABLE t(v VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?)", [[v] for v in values])
    rows = con.execute(
        f'SELECT v, {numeric_expr("v")}, {_typecheck_num_expr("v")} FROM t'
    ).fetchall()
    con.close()
    disagree = [(v, a, b) for v, a, b in rows if a != b]
    assert not disagree, f"rules and type check disagree on {disagree}"


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
