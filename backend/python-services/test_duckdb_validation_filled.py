"""DuckDB validation on sparsely-mapped templates (no DB, no network).

WHAT WENT WRONG. On a template far wider than its setup's mapping every row was
set aside as blank, `executemany` was handed an empty list and raised, and the
run was stored as clean with nothing checked. With that fixed, the rules that
read a column the setup never fills still ran on NULLs: a "must not be empty"
rule flagged every row about a column nobody can fill in triage, and the rest
passed having checked nothing.

These tests pin, with an in-memory DuckDB and hand-written rule queries shaped
like the compiler's (one SELECT per sheet, UNION ALL between arms):

  · every record excluded → no crash, the table exists empty, stats say 0 of N;
  · a rule on a column the setup does not fill → unprocessable, not N rows;
  · the same rule on a filled column → fires;
  · a two-sheet fan-out where one sheet fills the column keeps that arm, and
    records the sheet it skipped;
  · a column switched off on one sheet only drops that sheet's arm (the table
    keeps the column, so the rule never fails to bind), and switched off
    everywhere the rule says to switch it on;
  · type checks skip unfilled columns;
  · without `filled_cols` nothing about which rules run changes.

rule_compiler and variation_reconcile load their catalog from the database on
import, so both are replaced for the duration of each test: the reconcile step by
a no-op, the compiler by a stand-in carrying the REAL `drop_sheet_arms` source
read straight out of rule_compiler.py (so the arm cutting under test is the
production code, not a copy).

    python -m pytest test_duckdb_validation_filled.py
"""
from __future__ import annotations

import ast
import os
import re
import sys
import types

import pytest

import duckdb_validation as dv

_HERE = os.path.dirname(os.path.abspath(__file__))


def _compiler_stub():
    """A module with rule_compiler's real `drop_sheet_arms` (and the reference
    table names it reads), extracted from the source without importing it."""
    path = os.path.join(_HERE, "contract_upload_services", "rule_compiler.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "drop_sheet_arms")
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "_REFERENCE_TABLE_NAMES" for t in n.targets))]
    assert len(keep) == 2, "rule_compiler.drop_sheet_arms / _REFERENCE_TABLE_NAMES moved"
    mod = types.ModuleType("contract_upload_services.rule_compiler")
    mod.__dict__["_re"] = re
    exec(compile(ast.Module(body=keep, type_ignores=[]), path, "exec"), mod.__dict__)
    return mod


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    import contract_upload_services  # noqa: F401 — the package itself is plain
    monkeypatch.setitem(sys.modules, "contract_upload_services.rule_compiler", _compiler_stub())
    recon = types.ModuleType("contract_upload_services.variation_reconcile")
    recon.reconcile = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "contract_upload_services.variation_reconcile", recon)
    monkeypatch.setattr(dv, "_save_cache", lambda *a, **k: None)
    monkeypatch.setenv("KAVACHIO_VARIATION_RECONCILE", "0")


# ── helpers ───────────────────────────────────────────────────────────────────
def _cols(prefix, n):
    return [f"{prefix}_{i:03d}" for i in range(n)]


def _required(sheet, field):
    """The compiler's required_field shape."""
    return (f"SELECT __rowid AS row_id, '{sheet}' AS sheet, '{field}' AS field, "
            f"'{field} is required but empty' AS reason, \"{field}\" AS actual_value "
            f"FROM \"{sheet}\" WHERE \"{field}\" IS NULL OR TRIM(\"{field}\") = ''")


def _rule(rule_id, sql):
    return {"rule_id": rule_id, "rule_name": f"rule {rule_id}", "severity": "critical",
            "compiled_sql": sql}


def _records(filled, n_rows, empty=()):
    """Projection-shaped rows: only the setup's columns, all non-empty except
    the ones named in `empty`."""
    return [{c: (None if c in empty else f"v{r}-{i}") for i, c in enumerate(filled)}
            for r in range(n_rows)]


def _run(blocks, rules, schema, filled=None, types_=None, inactive=None):
    return dv.run_validation(blocks, rules, session=object(), schema_cols=schema,
                             column_types=types_, filled_cols=filled,
                             inactive_cols=inactive)


# ── crash + stats ─────────────────────────────────────────────────────────────
def test_every_record_excluded_does_not_crash():
    cols = _cols("c", 20)
    blocks = [{"sheet": "S", "records": [{c: None for c in cols} for _ in range(4)]}]
    con, tables = dv.build_connection(blocks, schema_cols={"S": cols})
    assert con.execute('SELECT COUNT(*) FROM "S"').fetchone()[0] == 0
    assert con.execute(_required("S", cols[0])).fetchall() == []
    t = tables["S"]
    assert (t["rows_total"], t["rows_loaded"], t["excluded"]) == (4, 0, {"BLANK": 4})


def test_run_validation_reports_rows_checked():
    cols = _cols("c", 20)
    blocks = [{"sheet": "S", "records": [{c: None for c in cols} for _ in range(3)]}]
    out = _run(blocks, [_rule(1, _required("S", cols[0]))], {"S": cols})
    st = out["stats"]
    assert (st["rows_total"], st["rows_validated"], st["rows_excluded"]) == (3, 0, 3)
    assert st["excluded"] == {"BLANK": 3}
    assert out["exceptions"] == []


def test_sparse_mapping_rows_are_loaded_by_the_record_key_fallback():
    """No filled_cols given: the keys the records carry are the measure, so a
    wide template no longer sets every row aside."""
    cols = _cols("c", 150)
    mapped = cols[:12]
    blocks = [{"sheet": "S", "records": _records(mapped, 6)}]
    out = _run(blocks, [], {"S": cols})
    assert out["stats"]["rows_validated"] == 6


def test_trailing_space_sheet_name_finds_its_filled_columns():
    cols = _cols("c", 30)
    mapped = cols[:10]
    blocks = [{"sheet": "S ", "records": _records(mapped, 3)}]
    _, tables = dv.build_connection(blocks, schema_cols={"S ": cols},
                                    filled_cols={"S ": mapped})
    assert tables["S"]["filled"] == mapped and tables["S"]["rows_loaded"] == 3


# ── rules on unfilled columns ─────────────────────────────────────────────────
def test_required_field_on_unfilled_column_is_not_checked():
    cols = _cols("c", 60)
    mapped = cols[:10]
    blocks = [{"sheet": "S", "records": _records(mapped, 5)}]
    unfilled = cols[40]
    out = _run(blocks, [_rule(7, _required("S", unfilled))], {"S": cols}, {"S": mapped})
    assert out["exceptions"] == []
    [u] = out["unprocessable"]
    assert u["rule_id"] == 7 and u["kind"] == "not_filled" and u["columns"] == [unfilled]
    assert "does not fill" in u["message"] and unfilled in u["message"]
    assert out["stats"]["rules_ok"] == 0 and out["stats"]["rules_unprocessable"] == 1


def test_required_field_on_filled_column_fires():
    cols = _cols("c", 60)
    mapped = cols[:10]
    blocks = [{"sheet": "S", "records": _records(mapped, 5, empty={mapped[3]})}]
    out = _run(blocks, [_rule(8, _required("S", mapped[3]))], {"S": cols}, {"S": mapped})
    assert out["unprocessable"] == []
    assert [e["row"] for e in out["exceptions"]] == [1, 2, 3, 4, 5]


def test_without_filled_cols_the_rule_runs_as_before():
    cols = _cols("c", 60)
    mapped = cols[:10]
    blocks = [{"sheet": "S", "records": _records(mapped, 5)}]
    out = _run(blocks, [_rule(9, _required("S", cols[40]))], {"S": cols}, None)
    assert out["unprocessable"] == [] and len(out["exceptions"]) == 5


def test_column_missing_from_the_template_keeps_its_own_message():
    cols = _cols("c", 10)
    blocks = [{"sheet": "S", "records": _records(cols, 2)}]
    out = _run(blocks, [_rule(3, _required("S", "not_in_template"))], {"S": cols}, {"S": cols})
    [u] = out["unprocessable"]
    assert "no column for" in u["message"] and "kind" not in u


def test_fan_out_keeps_the_arm_whose_sheet_fills_the_column():
    shared = "shared_col"
    a_cols = _cols("a", 30) + [shared]
    b_cols = _cols("b", 30) + [shared]
    blocks = [{"sheet": "A", "records": _records(a_cols[:5] + [shared], 3, empty={shared})},
              {"sheet": "B", "records": _records(b_cols[:5], 4)}]
    sql = _required("A", shared) + "\nUNION ALL\n" + _required("B", shared)
    out = _run(blocks, [_rule(11, sql)], {"A": a_cols, "B": b_cols},
               {"A": a_cols[:5] + [shared], "B": b_cols[:5]})
    assert out["unprocessable"] == []
    assert {(e["sheet"], e["row"]) for e in out["exceptions"]} == {("A", 1), ("A", 2), ("A", 3)}
    # the sheet it skipped is recorded, not only printed
    [p] = out["partially_checked"]
    assert (p["rule_id"], p["columns"], p["sheets"]) == (11, [shared], ["B"])
    assert "not on B" in p["message"] and "does not fill" in p["message"]
    assert out["stats"]["rules_ok"] == 1 and out["stats"]["rules_partial"] == 1


def test_column_switched_off_on_one_sheet_keeps_the_other_sheets_findings():
    """The mapping fills the column on both sheets, but the template switches it
    off on B. B's table still carries it (no binder error on B's arm), B's arm
    is dropped as unfilled, and A's findings stand."""
    cols = ["Policy", "Premium"]
    blocks = [{"sheet": "A", "records": [{"Policy": "P1", "Premium": None},
                                         {"Policy": "P2", "Premium": None}]},
              {"sheet": "B", "records": [{"Policy": "P3", "Premium": None}]}]
    sql = _required("A", "Premium") + "\nUNION ALL\n" + _required("B", "Premium")
    out = _run(blocks, [_rule(21, sql)], {"A": cols, "B": cols},
               {"A": cols, "B": cols}, inactive={"B": ["Premium"]})
    assert out["unprocessable"] == []
    assert {(e["sheet"], e["row"]) for e in out["exceptions"]} == {("A", 1), ("A", 2)}
    [p] = out["partially_checked"]
    assert p["sheets"] == ["B"] and "switches off: Premium" in p["message"]


def test_column_switched_off_everywhere_says_switch_it_on():
    cols = ["Policy", "Premium"]
    blocks = [{"sheet": "A", "records": [{"Policy": "P1", "Premium": "1"}]},
              {"sheet": "B", "records": [{"Policy": "P2", "Premium": "2"}]}]
    sql = _required("A", "Premium") + "\nUNION ALL\n" + _required("B", "Premium")
    out = _run(blocks, [_rule(22, sql)], {"A": cols, "B": cols},
               {"A": cols, "B": cols}, inactive={"A ": ["Premium"], "B": ["Premium"]})
    [u] = out["unprocessable"]
    assert u["kind"] == "not_filled" and u["columns"] == ["Premium"]
    assert u["message"] == ("Not checked — the output template switches off: Premium. "
                            "Switch it on in the output template to run this check.")
    assert out["partially_checked"] == []


def test_inactive_columns_do_nothing_without_filled_cols():
    cols = ["Policy", "Premium"]
    blocks = [{"sheet": "A", "records": [{"Policy": "P1", "Premium": None}]}]
    out = _run(blocks, [_rule(23, _required("A", "Premium"))], {"A": cols},
               None, inactive={"A": ["Premium"]})
    assert out["unprocessable"] == [] and len(out["exceptions"]) == 1


def test_fan_out_where_no_sheet_fills_the_column_is_not_checked():
    shared = "shared_col"
    a_cols = _cols("a", 30) + [shared]
    b_cols = _cols("b", 30) + [shared]
    blocks = [{"sheet": "A", "records": _records(a_cols[:5], 3)},
              {"sheet": "B", "records": _records(b_cols[:5], 4)}]
    sql = _required("A", shared) + "\nUNION ALL\n" + _required("B", shared)
    out = _run(blocks, [_rule(12, sql)], {"A": a_cols, "B": b_cols},
               {"A": a_cols[:5], "B": b_cols[:5]})
    assert out["exceptions"] == [] and [u["rule_id"] for u in out["unprocessable"]] == [12]


def test_aggregate_over_several_sheets_counts_a_column_filled_by_any():
    shared = "amount"
    a_cols = _cols("a", 10) + [shared]
    b_cols = _cols("b", 10) + [shared]
    blocks = [{"sheet": "A", "records": [{**r, shared: "5"} for r in _records(a_cols[:3], 2)]},
              {"sheet": "B", "records": _records(b_cols[:3], 2)}]
    sql = (f"SELECT NULL AS row_id, 'A' AS sheet, '{shared}' AS field, 'cap' AS reason, "
           f"SUM(TRY_CAST(\"{shared}\" AS DOUBLE)) AS actual_value "
           f"FROM (SELECT __rowid, \"{shared}\" FROM \"A\" UNION ALL "
           f"SELECT __rowid, \"{shared}\" FROM \"B\") AS _u "
           f"HAVING SUM(TRY_CAST(\"{shared}\" AS DOUBLE)) > 1")
    out = _run(blocks, [_rule(13, sql)], {"A": a_cols, "B": b_cols},
               {"A": a_cols[:3] + [shared], "B": b_cols[:3]})
    assert out["unprocessable"] == [] and len(out["exceptions"]) == 1


def test_type_checks_skip_unfilled_columns():
    cols = _cols("c", 30)
    mapped = cols[:5]
    recs = [{**r, mapped[0]: "not a date"} for r in _records(mapped, 3)]
    blocks = [{"sheet": "S", "records": recs}]
    types_ = {"S": {mapped[0]: "date", cols[20]: "date"}}
    out = _run(blocks, [], {"S": cols}, {"S": mapped}, types_)
    assert {(e["column"], e["row"]) for e in out["exceptions"]} == \
        {(mapped[0], 1), (mapped[0], 2), (mapped[0], 3)}


def test_unfilled_columns_helper_leaves_unknown_sheets_alone():
    tables = {"S": {"columns": ["x", "y"], "filled": None}}
    sql = _required("S", "y")
    assert dv._unfilled_columns(sql, tables) == ([], sql)
