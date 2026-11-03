"""Dated checks — a mid-term change's per-row split (no DB, no network).

The scenario these tests pin is the one the carrier will test on screen: a
contract whose "Commission Schedule" rule requires a Commission Rate of 23.5
gets a mid-term change from 16 Aug 2026 moving it to 25.5. Once live, the old
rule is closed (effective_to = 16 Aug, exclusive — it still covers 15 Aug) and
a new one opens from 16 Aug. Each row is then checked by the version in force
on its TRANSACTION date:

  · 14 and 15 Aug rows are checked against 23.5, 16 Aug onwards against 25.5;
  · a row whose rate is not a number is still told so — by its own version;
  · a row with no readable transaction date is checked by neither, and the run
    says which rows (D2); unchanged rules still run on it;
  · a rule NOT touched by a change runs on every row, even though its
    effective_from holds its creation date (the CURRENT_DATE default) — the
    trap that would otherwise stop checking every older row;
  · with no transaction date column, the dated versions are reported as not
    run rather than both firing on every row;
  · a dated TOTAL runs whole, as the version in force on the period's last
    day (D4);
  · exceptions carry the window they were judged in, for the screens.

The rule SQL is the shape the compiler produced for the real rule (range_check,
min = max, with its "must be a number" arm). rule_compiler loads its catalog
from the database on import, so — as in test_duckdb_validation_filled.py — it
is replaced by a stand-in carrying the REAL `_q`, `_date` and
`drop_sheet_arms` source read out of rule_compiler.py.

    python -m pytest test_rule_windows.py
"""
from __future__ import annotations

import ast
import datetime as dt
import os
import re
import sys
import types

import pytest

import duckdb_validation as dv
import rule_windows as rw
import validation_outcome as vo

_HERE = os.path.dirname(os.path.abspath(__file__))
D = dt.date


def _compiler_stub():
    path = os.path.join(_HERE, "contract_upload_services", "rule_compiler.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    want_fn = {"drop_sheet_arms", "_q", "_date"}
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in want_fn)
            or (isinstance(n, ast.ClassDef) and n.name == "CompileError")
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "_REFERENCE_TABLE_NAMES" for t in n.targets))]
    assert len(keep) == 5, "rule_compiler helpers moved"
    mod = types.ModuleType("contract_upload_services.rule_compiler")
    mod.__dict__["_re"] = re
    mod.__dict__["re"] = re
    exec(compile(ast.Module(body=keep, type_ignores=[]), path, "exec"), mod.__dict__)
    return mod


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    import contract_upload_services  # noqa: F401
    monkeypatch.setitem(sys.modules, "contract_upload_services.rule_compiler", _compiler_stub())
    recon = types.ModuleType("contract_upload_services.variation_reconcile")
    recon.reconcile = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "contract_upload_services.variation_reconcile", recon)
    monkeypatch.setattr(dv, "_save_cache", lambda *a, **k: None)
    monkeypatch.setenv("KAVACHIO_VARIATION_RECONCILE", "0")


# ── the compiler's range_check (min = max), as stored on the real rule ───────
SHEET, RATE, TXN, INCEPT = "Sheet 2", "Commission Rate", "TransactionDate", "PolicyInception"
POL = "PolicyNumber"
_NUM = (r"""(CASE WHEN regexp_replace(TRIM("{c}"), '[\s,%]|\p{{Z}}|\p{{Sc}}', '', 'g') LIKE '(%)' """
        r"""THEN -1 ELSE 1 END * TRY_CAST(CASE WHEN regexp_replace(TRIM("{c}"), """
        r"""'[\s,%]|\p{{Z}}|\p{{Sc}}', '', 'g') LIKE '(%)' THEN SUBSTR(regexp_replace(TRIM("{c}"), """
        r"""'[\s,%]|\p{{Z}}|\p{{Sc}}', '', 'g'), 2, LENGTH(regexp_replace(TRIM("{c}"), """
        r"""'[\s,%]|\p{{Z}}|\p{{Sc}}', '', 'g')) - 2) ELSE regexp_replace(TRIM("{c}"), """
        r"""'[\s,%]|\p{{Z}}|\p{{Sc}}', '', 'g') END AS DOUBLE))""")


def _equals_sql(value):
    n = _NUM.format(c=RATE)
    return (f"SELECT __rowid AS row_id, '{SHEET}' AS sheet, '{RATE}' AS field, "
            f"'{RATE} must equal {value}' AS reason, \"{RATE}\" AS actual_value "
            f"FROM \"{SHEET}\" WHERE {n} IS NOT NULL AND {n} <> {value}\n"
            f"UNION ALL\n"
            f"SELECT __rowid AS row_id, '{SHEET}' AS sheet, '{RATE}' AS field, "
            f"'{RATE} must be a number, but found ' || \"{RATE}\" || '.' AS reason, "
            f"\"{RATE}\" AS actual_value FROM \"{SHEET}\" WHERE {n} IS NULL AND "
            f"(\"{RATE}\" IS NOT NULL AND TRIM(\"{RATE}\") <> '')")


def _rule(rule_id, value, **dates):
    spec = {"compiled_sql": _equals_sql(value),
            "ir": {"template": "range_check",
                   "params": {"field": RATE, "min": value, "max": value}}}
    return {"rule_id": rule_id, "rule_name": "Commission Schedule", "severity": "critical",
            "rule_spec": spec, "compiled_sql": spec["compiled_sql"], **dates}


def _required_rule(rule_id, field, **dates):
    sql = (f"SELECT __rowid AS row_id, '{SHEET}' AS sheet, '{field}' AS field, "
           f"'{field} is required but empty' AS reason, \"{field}\" AS actual_value "
           f"FROM \"{SHEET}\" WHERE \"{field}\" IS NULL OR TRIM(\"{field}\") = ''")
    return {"rule_id": rule_id, "rule_name": f"{field} required", "severity": "warning",
            "rule_spec": {"compiled_sql": sql}, "compiled_sql": sql, **dates}


OLD = dict(effective_from=D(2026, 9, 3), effective_to=D(2026, 8, 16))   # closed
NEW = dict(effective_from=D(2026, 8, 16), source_amendment_id=1)          # opened


def _rows(*rows):
    """(transaction date, rate[, inception]) → projection rows."""
    out = []
    for i, r in enumerate(rows, start=1):
        txn, rate = r[0], r[1]
        inc = r[2] if len(r) > 2 else "2026-01-10"
        out.append({POL: f"POL-{i:03d}", TXN: txn, RATE: rate, INCEPT: inc})
    return [{"sheet": SHEET, "records": out}]


SCHEMA = {SHEET: [POL, TXN, RATE, INCEPT]}


def _run(blocks, rules, date_column=TXN, period_end=None):
    return dv.run_validation(blocks, rules, session=object(), schema_cols=SCHEMA,
                             date_column=date_column, period_end=period_end)


def _flags(out):
    """{row: [(rule_id, reason)]} for data violations."""
    got = {}
    for e in out["exceptions"]:
        got.setdefault(e["row"], []).append((e["rule_id"], e["reason"]))
    return got


# ── rule_windows, pure ────────────────────────────────────────────────────────
def test_creation_date_in_effective_from_does_not_date_a_rule():
    assert rw.window_of({"effective_from": D(2026, 9, 3)}) is None
    assert rw.window_of({"effective_from": "2026-09-03", "source_amendment_id": None}) is None


def test_windows_and_labels():
    old, new = rw.window_of(OLD), rw.window_of(NEW)
    assert old == (None, D(2026, 8, 16))
    assert new == (D(2026, 8, 16), None)
    # The boundary: 15 Aug is the old rule's, 16 Aug the new one's — no gap.
    assert rw.covers(old, D(2026, 8, 15)) and not rw.covers(new, D(2026, 8, 15))
    assert rw.covers(new, D(2026, 8, 16)) and not rw.covers(old, D(2026, 8, 16))
    assert not rw.covers(old, None) and rw.covers(None, None)
    assert rw.label(old) == "until 15 Aug 2026"
    assert rw.label(new) == "from 16 Aug 2026"
    assert rw.label((D(2026, 8, 16), D(2026, 10, 1))) == "16 Aug 2026 – 30 Sep 2026"


def test_date_column_from_structure_needs_exactly_one_tagged_column():
    s = {"sheets": [{"columns": [
        {"column_name": "TransactionDate", "canonical_field": "premium_transaction_effective_date"},
        {"column_name": "TransactionEffectiveDate", "canonical_field": None},
        {"column_name": "PolicyInception", "canonical_field": "policy_effective_date"}]}]}
    assert rw.date_column_from_structure(s) == "TransactionDate"
    s["sheets"][0]["columns"][1]["canonical_field"] = "premium_transaction_effective_date"
    assert rw.date_column_from_structure(s) is None      # two candidates: carrier picks
    assert rw.date_column_from_structure({}) is None


# ── the per-row split ─────────────────────────────────────────────────────────
def test_rows_split_at_16_august_by_transaction_date():
    blocks = _rows(("2026-08-14", "23.5"),   # 1 old, passes
                   ("2026-08-15", "23.5"),   # 2 old (boundary day), passes
                   ("2026-08-15", "25.5"),   # 3 old → flagged
                   ("2026-08-16", "25.5"),   # 4 new, passes
                   ("2026-08-16", "23.5"),   # 5 new → flagged
                   ("2026-12-31", "25.5"),   # 6 new, passes
                   ("2026-01-01", "23.5"))   # 7 old, passes
    out = _run(blocks, [_rule(4131, 23.5, **OLD), _rule(9001, 25.5, **NEW)])
    f = _flags(out)
    assert set(f) == {3, 5}
    assert f[3] == [(4131, "Commission Rate must equal 23.5")]
    assert f[5] == [(9001, "Commission Rate must equal 25.5")]
    assert out["stats"]["rules_ok"] == 2 and out["unprocessable"] == []


def test_split_uses_transaction_date_not_inception():
    # Policy incepted in January (old terms) but the transaction is dated
    # 20 Aug: the NEW rule applies.
    blocks = _rows(("2026-08-20", "23.5", "2026-01-05"))
    f = _flags(_run(blocks, [_rule(4131, 23.5, **OLD), _rule(9001, 25.5, **NEW)]))
    assert f == {1: [(9001, "Commission Rate must equal 25.5")]}


def test_must_be_a_number_is_kept_in_each_version():
    blocks = _rows(("2026-08-10", "abc"), ("2026-08-20", "n/a"))
    f = _flags(_run(blocks, [_rule(4131, 23.5, **OLD), _rule(9001, 25.5, **NEW)]))
    assert [r for r, _ in f[1]] == [4131] and "must be a number" in f[1][0][1]
    assert [r for r, _ in f[2]] == [9001] and "must be a number" in f[2][0][1]


def test_date_spellings_the_rules_read_are_read_here_too():
    blocks = _rows(("20260814", "25.5"),            # compact → old → flagged
                   ("08/20/2026", "23.5"),          # US slash → new → flagged
                   ("2026-08-16 00:00:00", "25.5"), # ISO datetime → new, passes
                   ("15-08-2026", "25.5"))          # day-first dash → old → flagged
    f = _flags(_run(blocks, [_rule(4131, 23.5, **OLD), _rule(9001, 25.5, **NEW)]))
    assert {k: [r for r, _ in v] for k, v in f.items()} == {1: [4131], 2: [9001], 4: [4131]}


def test_undated_rows_are_not_checked_by_changed_checks_only():
    blocks = _rows(("2026-08-20", "25.5"),
                   ("", "99"),             # no date: neither version may judge it
                   ("not a date", "99"))
    rules = [_rule(4131, 23.5, **OLD), _rule(9001, 25.5, **NEW),
             _required_rule(77, INCEPT, effective_from=D(2026, 9, 3))]
    blocks[0]["records"][1][INCEPT] = None   # unchanged rule still runs on row 2
    out = _run(blocks, rules)
    f = _flags(out)
    assert f == {2: [(77, f"{INCEPT} is required but empty")]}
    notes = [p for p in out["partially_checked"] if p.get("kind") == rw.UNDATED_KIND]
    assert len(notes) == 1                    # one note for both versions
    n = notes[0]
    assert n["rule_name"] == "Commission Schedule" and n["rows"] == {SHEET: [2, 3]}
    assert "until 15 Aug 2026" in n["message"] and "from 16 Aug 2026" in n["message"]
    # …and the grouped "Checks not run" line says so in plain words.
    entry = vo.not_checked_entry(out["unprocessable"], out["partially_checked"])
    assert "not run on rows with no transaction effective date" in entry["message"]
    assert entry["partial"][0]["rows"] == {SHEET: [2, 3]}


def test_untouched_rule_with_creation_date_checks_every_row():
    # effective_from = 3 Sep 2026 (the day the rule was written); rows dated
    # long before must still be checked.
    blocks = _rows(("2025-02-01", "23.5", None), ("2026-08-20", "23.5", None))
    out = _run(blocks, [_required_rule(77, INCEPT, effective_from=D(2026, 9, 3))])
    assert sorted(_flags(out)) == [1, 2]
    assert out["partially_checked"] == []


def test_no_date_column_means_not_run_never_double_flagged():
    blocks = _rows(("2026-08-20", "24"))
    out = _run(blocks, [_rule(4131, 23.5, **OLD), _rule(9001, 25.5, **NEW)],
               date_column=None)
    assert out["exceptions"] == []
    kinds = {u["rule_id"]: u.get("kind") for u in out["unprocessable"]}
    assert kinds == {4131: rw.NO_DATE_COLUMN_KIND, 9001: rw.NO_DATE_COLUMN_KIND}
    assert "Choose the column in Bordereau Setup" in out["unprocessable"][0]["message"]


def test_run_without_dated_rules_is_unchanged():
    blocks = _rows(("2026-08-20", "24"))
    plain = _rule(4131, 23.5)
    a = _run(blocks, [dict(plain)], date_column=None)
    b = _run(blocks, [dict(plain)], date_column=TXN)
    assert _flags(a) == _flags(b) == {1: [(4131, "Commission Rate must equal 23.5")]}
    assert "rule_window" not in a["exceptions"][0]


def test_exceptions_carry_their_window():
    blocks = _rows(("2026-08-10", "1"), ("2026-08-20", "1"))
    out = _run(blocks, [_rule(4131, 23.5, **OLD), _rule(9001, 25.5, **NEW)])
    by_row = {e["row"]: e for e in out["exceptions"]}
    assert by_row[1]["rule_window_label"] == "until 15 Aug 2026"
    assert by_row[1]["rule_window"] == {"from": None, "to": "2026-08-16"}
    assert by_row[2]["rule_window_label"] == "from 16 Aug 2026"
    assert by_row[1]["rule_name"] == by_row[2]["rule_name"] == "Commission Schedule"


# ── D4: a changed total ──────────────────────────────────────────────────────
def _total_rule(rule_id, cap, **dates):
    sql = (f"SELECT MIN(__rowid) AS row_id, '{SHEET}' AS sheet, '{RATE}' AS field, "
           f"'total over {cap}' AS reason, CAST(SUM(TRY_CAST(\"{RATE}\" AS DOUBLE)) AS VARCHAR) "
           f"AS actual_value FROM \"{SHEET}\" HAVING SUM(TRY_CAST(\"{RATE}\" AS DOUBLE)) > {cap}")
    spec = {"compiled_sql": sql, "ir": {"template": "aggregate_cap",
                                        "params": {"field": RATE, "max": cap}}}
    return {"rule_id": rule_id, "rule_name": "Total cap", "severity": "critical",
            "rule_spec": spec, "compiled_sql": sql, **dates}


def test_changed_total_runs_whole_file_as_version_in_force_at_period_end():
    # 30 + 30 = 60 over the whole file. Old cap 50, new cap 100 from 16 Aug.
    blocks = _rows(("2026-08-10", "30"), ("2026-08-20", "30"))
    rules = [_total_rule(1, 50, **OLD), _total_rule(2, 100, **NEW)]
    # August period ends 31 Aug → the new cap governs the whole file: no breach.
    out = _run(blocks, [dict(r) for r in rules], period_end=D(2026, 8, 31))
    assert out["exceptions"] == [] and out["stats"]["rules_ok"] == 1
    # A July file (period end 31 Jul) is judged on the old cap: breach.
    out = _run(blocks, [dict(r) for r in rules], period_end=D(2026, 7, 31))
    assert [e["rule_id"] for e in out["exceptions"]] == [1]
    assert out["exceptions"][0]["rule_window_label"] == "until 15 Aug 2026"


def test_changed_total_without_period_uses_latest_transaction_date():
    blocks = _rows(("2026-08-10", "30"), ("2026-08-20", "30"))
    rules = [_total_rule(1, 50, **OLD), _total_rule(2, 100, **NEW)]
    out = _run(blocks, rules)            # no period: last date 20 Aug → new cap
    assert out["exceptions"] == [] and out["stats"]["rules_ok"] == 1
