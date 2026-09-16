"""
test_wide_template_rule_fixes.py
────────────────────────────────
Deterministic rule-generation defects that only show on WIDE standard templates
(many currency / country / state columns, no sample values, columns the user
switched off), each pinned with a synthetic field list — no real template:

  * currency ↔ country: a "Country Sub-division: State" column is not a country
    (US state codes collide with ISO country codes); a sample-less AMOUNT column
    that mentions "currency" gets no currency rule; each currency column is paired
    with at most ONE country column, never the cross product.
  * conditional_required / conditional_value whose condition VALUE is another
    column compiles to a column comparison — proven by running the SQL in DuckDB.
  * the backdating anchor finds a booking-date column by its canonical field.
  * _template_fields_from_structure leaves out switched-off columns by default.

Pure — see _offline.py (no DB) — and no model calls.

Run:  python -m pytest contract_upload_services/tests/test_wide_template_rule_fixes.py
"""
import ast
import os
import sys
import types
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))
import _offline                                                  # noqa: E402,F401

import duckdb                                                    # noqa: E402
import pytest                                                    # noqa: E402

from contract_upload_services import validation_rule_generator as vrg  # noqa: E402
from contract_upload_services import rule_compiler as rc         # noqa: E402
from contract_upload_services.output_schema import is_processing_date_column  # noqa: E402
from contract_upload_services.uszips_reference import is_state_column  # noqa: E402


def _f(name, data_type=None, canonical=None, samples=()):
    return {"name": name, "sheet": "S1", "data_type": data_type,
            "canonical_field": canonical, "samples": list(samples),
            "samples_all": list(samples)}


# A wide, sample-less standard sheet. Names are generic header shapes, not any
# customer's template.
WIDE = [
    _f("Original Currency", "string"),
    _f("Settlement Currency", "string"),
    _f("Sum Insured Currency (see code list)", "string"),
    _f("Net Premium in Original Currency", "decimal"),
    _f("Brokerage Amount (Settlement Currency)", None, "txn_brokerage_amount"),
    _f("Tax Amount in Settlement Currency"),                 # name is the only clue
    # "string" is the type an UNTYPED column gets, amounts included — not a code.
    _f("Net Premium to Market in Settlement Currency", "string"),
    _f("Brokerage Amount (Original Currency)", "string"),
    _f("Total Insured Value (Original Currency)", "string"),  # no amount word at all
    # An explicit code list names a code column, amount words or not.
    _f("Final Net Premium Settlement Currency (see code list)", "string"),
    _f("Broker Currency"),
    _f("Insured Country (see code list)", "string"),
    _f("Insured Country Sub-division: State, Province, Territory", "string"),
    _f("Location of Risk - Country", "string"),
    _f("Location of Risk - Country Sub-division: State, Province", "string"),
    _f("Tax Jurisdiction: Country, State, Province", "string"),
    _f("Broker Country", "string"),
    _f("Insured Zip Code", "string"),
    _f("Insured State", "string"),
]
AMOUNT_COLS = {"Net Premium in Original Currency",
               "Brokerage Amount (Settlement Currency)",
               "Tax Amount in Settlement Currency",
               "Net Premium to Market in Settlement Currency",
               "Brokerage Amount (Original Currency)",
               "Total Insured Value (Original Currency)"}


def _currency_rules(fields):
    out = []
    for e in vrg.derive_formula_entries([], fields):
        ir = e["candidates"][0]
        if ir["template"] == "currency_country_consistency":
            out.append(ir["params"])
    return out


def test_no_currency_rule_pairs_with_a_state_or_subdivision_column():
    rules = _currency_rules(WIDE)
    assert rules
    for p in rules:
        c = p["country_field"]
        assert not is_state_column(c) and "sub-division" not in c.lower(), p


def test_sample_less_amount_columns_get_no_currency_rule():
    cyc = {p["currency_field"] for p in _currency_rules(WIDE)}
    assert not (cyc & AMOUNT_COLS), cyc


def test_each_currency_column_has_at_most_one_country_pairing():
    from collections import Counter
    per = Counter(p["currency_field"] for p in _currency_rules(WIDE))
    assert per and max(per.values()) == 1, per
    # the code columns are all still checked
    assert {"Original Currency", "Settlement Currency",
            "Sum Insured Currency (see code list)", "Broker Currency",
            "Final Net Premium Settlement Currency (see code list)"} <= set(per)


def test_a_string_typed_amount_column_gets_no_currency_rule():
    # The reported shape: sample-less, untyped ("string"), no canonical field.
    fields = [_f("Net Premium to Market in Settlement Currency", "string"),
              _f("Settlement Currency", "string"),
              _f("Insured Country", "string")]
    assert _currency_rules(fields) == [
        {"currency_field": "Settlement Currency", "country_field": "Insured Country"}]


def test_a_code_type_is_evidence_even_when_the_type_says_currency():
    fields = [_f("Premium in Original Currency", "currency_code"),
              _f("Insured Country", "string")]
    assert [p["currency_field"] for p in _currency_rules(fields)] == [
        "Premium in Original Currency"]


def test_pairing_prefers_the_same_entity_then_the_risk_country():
    got = {p["currency_field"]: p["country_field"] for p in _currency_rules(WIDE)}
    assert got["Broker Currency"] == "Broker Country"
    assert got["Original Currency"] == "Insured Country (see code list)"


def test_the_choice_is_deterministic_across_runs():
    assert _currency_rules(WIDE) == _currency_rules(list(WIDE))


def test_samples_still_decide_when_present():
    fields = [_f("Premium Currency", "decimal", samples=["USD", "GBP"]),
              _f("Net Premium Currency", "string", samples=["1250.40"]),
              _f("Insured Country", "string")]
    cyc = {p["currency_field"] for p in _currency_rules(fields)}
    assert cyc == {"Premium Currency"}


def test_postal_dispatch_never_uses_a_subdivision_column_as_the_country():
    for e in vrg.derive_formula_entries([], WIDE):
        ir = e["candidates"][0]
        if ir["template"] in ("zip_state_consistency", "state_validity"):
            cf = ir["params"].get("country_field")
            assert cf is None or "sub-division" not in cf.lower(), ir["params"]


# ── conditional rules whose value is another column ─────────────────────────

def _run(sql, rows):
    con = duckdb.connect()
    con.execute('CREATE TABLE "S1" (__rowid INTEGER, "Original Currency" VARCHAR, '
                '"Settlement Currency" VARCHAR, "Rate of Exchange" VARCHAR)')
    con.executemany('INSERT INTO "S1" VALUES (?, ?, ?, ?)', rows)
    return sorted(r[0] for r in con.execute(sql).fetchall())


ROWS = [
    (1, "USD", "USD", None),     # same currency → no rate needed
    (2, "EUR", "USD", None),     # differs, no rate → violation
    (3, "EUR", "USD", "1.08"),   # differs, rate given → fine
    (4, "eur", "EUR ", None),    # same after case/space → no rate needed
    (5, "EUR", "", None),        # settlement blank → cannot tell → not flagged
]


def test_conditional_required_with_a_field_reference_compares_columns():
    params = {"condition": {"field": "Original Currency", "op": "!=",
                            "value": {"field": "Settlement Currency"}},
              "required_field": "Rate of Exchange"}
    sql = rc._b_conditional_required("S1", params)
    assert "{'field'" not in sql
    assert _run(sql, ROWS) == [2]


def test_compile_ir_counts_the_referenced_column():
    ir = {"template": "conditional_required",
          "params": {"condition": {"field": "Original Currency", "op": "!=",
                                   "value": {"field": "Settlement Currency"}},
                     "required_field": "Rate of Exchange"}}
    f2s = {"Original Currency": ["S1"], "Rate of Exchange": ["S1"],
           "Settlement Currency": ["S1"]}
    assert _run(rc.compile_ir(ir, f2s), ROWS) == [2]
    # the referenced column lives on no sheet with the others → not compilable
    with pytest.raises(rc.CompileError):
        rc.compile_ir(ir, dict(f2s, **{"Settlement Currency": ["S2"]}))


def test_conditional_value_with_a_field_reference_compares_columns():
    params = {"condition": {"field": "Rate of Exchange", "op": "=", "value": "1"},
              "field": "Original Currency", "op": "=",
              "value": {"field": "Settlement Currency"}}
    rows = [(1, "USD", "USD", "1"), (2, "EUR", "USD", "1"), (3, "EUR", "USD", "1.08")]
    sql = rc._b_conditional_value("S1", params)
    assert "{'field'" not in sql
    assert _run(sql, rows) == [2]


def test_literal_conditions_compile_exactly_as_before():
    params = {"condition": {"field": "Original Currency", "op": "!=", "value": "USD"},
              "required_field": "Rate of Exchange"}
    sql = rc._b_conditional_required("S1", params)
    assert "TRIM(\"Original Currency\") <> 'USD'" in sql
    assert _run(sql, ROWS) == [2, 4, 5]


def test_value_field_refs_finds_references_at_any_depth():
    params = {"conditions": [{"field": "A", "op": "=", "value": {"field": "B"}}],
              "value": {"field": "C"}, "condition": {"field": "D", "value": "x"}}
    assert rc.value_field_refs(params) == ["B", "C"]


def _run_scoped(sql, rows):
    con = duckdb.connect()
    con.execute('CREATE TABLE "S1" (__rowid INTEGER, "Amount" VARCHAR, '
                '"Original Currency" VARCHAR, "Settlement Currency" VARCHAR)')
    con.executemany('INSERT INTO "S1" VALUES (?, ?, ?, ?)', rows)
    return sorted(r[0] for r in con.execute(sql).fetchall())


SCOPED_ROWS = [
    (1, "500", "USD", "USD"),    # same currency → out of scope
    (2, "500", "EUR", "USD"),    # differs → in scope, over the cap → violation
    (3, "50", "EUR", "USD"),     # in scope, under the cap
    (4, "500", "eur", "EUR "),   # same after case/space → out of scope
    (5, "500", "EUR", ""),       # settlement blank → cannot tell → out of scope
]


def _scoped_ir(value=None, op="!="):
    return {"template": "max_limit",
            "params": {"field": "Amount", "max": 100,
                       "scope": {"Original Currency": {
                           "op": op, "value": value or {"field": "Settlement Currency"}}}}}


def test_a_scope_value_that_names_a_column_compares_the_two_cells():
    f2s = {"Amount": ["S1"], "Original Currency": ["S1"], "Settlement Currency": ["S1"]}
    sql = rc.compile_ir(_scoped_ir(), f2s)
    assert "{'field'" not in sql and "{''field''" not in sql
    assert _run_scoped(sql, SCOPED_ROWS) == [2]


def test_a_scope_column_reference_is_pruned_like_a_scope_column_not_required():
    # The compared column is on no sheet with the rule: the predicate is dropped
    # (as for any off-sheet scope column) and the rule still compiles.
    f2s = {"Amount": ["S1"], "Original Currency": ["S1"], "Settlement Currency": ["S2"]}
    sql = rc.compile_ir(_scoped_ir(), f2s)
    assert "Settlement Currency" not in sql
    assert rc.value_field_refs(_scoped_ir()["params"]) == []


def test_a_scope_column_reference_with_a_non_comparison_op_is_rejected():
    with pytest.raises(rc.CompileError):
        rc._scope_pred("Original Currency",
                       {"op": "contains", "value": {"field": "Settlement Currency"}})


def test_a_scope_column_reference_in_the_aliased_enum_query_is_qualified():
    pred = rc._scope_pred("Original Currency",
                          {"op": "=", "value": {"field": "Settlement Currency"}}, "t")
    assert 't."Original Currency"' in pred and 't."Settlement Currency"' in pred


# ── processing / booking date by canonical field ────────────────────────────

def test_processing_date_by_canonical_field():
    assert is_processing_date_column("Policy issuance date",
                                     "premium_transaction_booking_date")
    assert is_processing_date_column("Keyed On", "transaction_processing_date")
    assert not is_processing_date_column("Policy issuance date")          # name alone
    assert not is_processing_date_column("Effective Date of Transaction",
                                         "transaction_effective_date")
    assert not is_processing_date_column("Booking Month", "booking_month")


def _backdating_rule():
    return [{"clause": {"text": "Backdating of more than 5 days requires referral."},
             "candidates": [{"template": "period_duration", "rule_name": "Backdating",
                             "params": {"start_field": "Risk Inception Date",
                                        "end_field": "Effective Date of Transaction",
                                        "unit": "day", "max": 5}}]}]


def test_backdating_anchors_to_a_booking_date_found_by_canonical_field():
    synth = _backdating_rule()
    fields = [_f("Risk Inception Date", "date"),
              _f("Effective Date of Transaction", "date", "transaction_effective_date"),
              _f("Policy issuance date", "date", "premium_transaction_booking_date")]
    assert vrg.fix_backdating_period_fields(synth, fields) == 1
    p = synth[0]["candidates"][0]["params"]
    assert (p["start_field"], p["end_field"], p["max"]) == (
        "Risk Inception Date", "Policy issuance date", 5)


def test_a_name_matched_transaction_date_still_wins():
    synth = _backdating_rule()
    fields = [_f("Risk Inception Date", "date"),
              _f("Policy issuance date", "date", "premium_transaction_booking_date"),
              _f("Transaction Date", "date")]
    vrg.fix_backdating_period_fields(synth, fields)
    assert synth[0]["candidates"][0]["params"]["end_field"] == "Transaction Date"


# ── switched-off columns are not generation targets ─────────────────────────

def _template_fields_fn(monkeypatch):
    """app_routes._template_fields_from_structure, loaded on its own: importing
    the whole route module would start the application."""
    path = os.path.join(os.path.dirname(__file__), "..", "..", "app_routes.py")
    tree = ast.parse(open(path).read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "_template_fields_from_structure")
    stub = types.ModuleType("exporter")
    stub.is_reference_sheet = lambda sheet: bool(sheet.get("reference"))
    monkeypatch.setitem(sys.modules, "exporter", stub)
    ns = {"Optional": Optional}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns["_template_fields_from_structure"]


STRUCTURE = {"sheets": [
    {"sheet_name": "S1", "columns": [
        {"column_name": "On", "active": True, "data_type": "string"},
        {"column_name": "Off", "active": False},
        {"column_name": "Unflagged"},
    ]},
    {"sheet_name": "Lookup", "reference": True, "columns": [{"column_name": "Code"}]},
]}


def test_inactive_columns_are_skipped_by_default(monkeypatch):
    fn = _template_fields_fn(monkeypatch)
    assert [f["name"] for f in fn(STRUCTURE)] == ["On", "Unflagged"]
    assert fn(STRUCTURE)[0]["data_type"] == "string"


def test_include_inactive_returns_every_column(monkeypatch):
    fn = _template_fields_fn(monkeypatch)
    assert [f["name"] for f in fn(STRUCTURE, include_inactive=True)] == [
        "On", "Off", "Unflagged"]
