"""A run's status follows from what its validation actually checked (no DB, no
network).

WHAT WENT WRONG. Three runs were stored as 'clean' with nothing checked: two
because validation crashed and the render path carried on with an empty list,
one because the uploaded file's sheets matched none of the setup's, so no row
reached the output. `validation_outcome` decides the status from the inputs to
the run instead, and these tests pin that table, the two entries it adds, and the
small setup-derived inputs it relies on (which columns a setup fills, how many
rows the upload carried).

Also here, because they share the "measure only what the setup fills" idea:
the summary-row test the renderer styles rows with, and the rule that decides
which of a stored, unapproved mapping may be handed back as already decided.
direct_routes reads the database on import, so that one function is read out of
its source and run on its own.

    python -m pytest test_validation_outcome.py
"""
from __future__ import annotations

import ast
import os
import types

# exporter builds a Gemini client on import; no call is made here.
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")

import pytest

import validation_outcome as vo

_HERE = os.path.dirname(os.path.abspath(__file__))


# ── outcome table ─────────────────────────────────────────────────────────────
def _stats(total, validated):
    return {"rows_total": total, "rows_validated": validated,
            "rows_excluded": total - validated}


_ROW_EXC = {"severity": "critical", "row": 1, "field": "x", "error_class": "data_violation"}
_NOT_CHECKED = vo.not_checked_entry([{"rule_id": 1, "rule_name": "r", "message": "m",
                                      "columns": ["c"], "kind": "not_filled"}])


@pytest.mark.parametrize("kw,status,reason_has", [
    # routing matched nothing: the file had rows, the output has none
    (dict(input_rows=10, projected_rows=0, stats=_stats(0, 0)), vo.NOT_VALIDATED, "reached the output"),
    (dict(input_rows=10, projected_rows=0, stats=None), vo.NOT_VALIDATED, "sheets do not match"),
    # validation raised
    (dict(input_rows=5, projected_rows=5, error=ValueError("boom")), vo.NOT_VALIDATED, "ValueError: boom"),
    # rows projected, none checked
    (dict(input_rows=5, projected_rows=5, stats=_stats(5, 0)), vo.NOT_VALIDATED, "set aside"),
    (dict(input_rows=5, projected_rows=5, stats=_stats(0, 0)), vo.NOT_VALIDATED, "reached the checks"),
    # checked
    (dict(input_rows=5, projected_rows=5, stats=_stats(5, 5)), vo.CLEAN, None),
    (dict(input_rows=5, projected_rows=5, stats=_stats(5, 4), exceptions=[_ROW_EXC]),
     vo.HAS_EXCEPTIONS, None),
    # the not-checked summary alone never makes a run has_exceptions
    (dict(input_rows=5, projected_rows=5, stats=_stats(5, 5), exceptions=[_NOT_CHECKED]),
     vo.CLEAN, None),
    # an empty upload is not a failure
    (dict(input_rows=0, projected_rows=0, stats=_stats(0, 0)), vo.CLEAN, None),
    # nothing to check with (no rules, no typed columns): stats None
    (dict(input_rows=5, projected_rows=5, stats=None), vo.CLEAN, None),
    # routing known: none of the setup's sheets in the file → mismatch
    (dict(input_rows=10, routed_rows=None, projected_rows=0), vo.NOT_VALIDATED, "sheets do not match"),
    # the setup's sheets carry rows, its filter let none through
    (dict(input_rows=10, routed_rows=4, projected_rows=0), vo.NOT_VALIDATED, "passed its sheet filter"),
    # the setup's sheets are there but empty; rows sit on a tab it does not read
    (dict(input_rows=10, routed_rows=0, projected_rows=0), vo.CLEAN, None),
])
def test_outcome_table(kw, status, reason_has):
    got_status, reason = vo.outcome(**kw)
    assert got_status == status
    if reason_has is None:
        assert reason is None
    else:
        assert reason_has in reason


def test_routing_miss_outranks_a_crash():
    status, reason = vo.outcome(input_rows=3, projected_rows=0, error=RuntimeError("x"))
    assert status == vo.NOT_VALIDATED and "reached the output" in reason


# ── entries ───────────────────────────────────────────────────────────────────
def test_not_validated_entry_shape():
    e = vo.not_validated_entry("the checks stopped")
    # the shape main.py has always used for its per-rule not-validated notices
    for key in ("severity", "code", "sheet", "row", "column", "field", "rule_id",
                "rule_name", "reason", "message", "error_class"):
        assert key in e
    assert (e["severity"], e["error_class"], e["row"], e["rule_id"]) == \
        ("critical", "not_validated", None, None)
    assert "NOT validated" in e["message"] and "the checks stopped" in e["message"]


def test_not_checked_entry_groups_every_rule_once():
    unprocessable = [
        {"rule_id": 1, "rule_name": "A", "message": "does not fill: x", "columns": ["x"]},
        {"rule_id": 2, "rule_name": "B", "message": "no column for: y, x", "columns": ["y", "x"]},
        {"rule_id": 3, "rule_name": "C", "message": "query failed"},
    ]
    e = vo.not_checked_entry(unprocessable)
    assert (e["severity"], e["error_class"], e["row"]) == ("info", "not_checked", None)
    assert e["columns"] == ["x", "y"]
    assert [r["rule_id"] for r in e["rules"]] == [1, 2, 3]
    assert e["message"].startswith("3 checks could not be run")
    assert e["reason"].splitlines() == ["A: does not fill: x", "B: no column for: y, x",
                                        "C: query failed"]
    assert vo.not_checked_entry([]) is None and vo.not_checked_entry(None) is None


def test_not_checked_entry_lists_partly_run_rules_separately():
    partial = [{"rule_id": 4, "rule_name": "D", "columns": ["x"], "sheets": ["B"],
                "message": "Checked only on A — not on B, where …"}]
    e = vo.not_checked_entry([], partial)
    assert e["rules"] == [] and e["columns"] == []
    assert [p["sheets"] for p in e["partial"]] == [["B"]]
    assert e["message"] == "1 check ran on only some sheets, not on B."
    assert e["reason"] == "D: Checked only on A — not on B, where …"
    both = vo.not_checked_entry([{"rule_id": 1, "rule_name": "A", "message": "m",
                                  "columns": ["y"]}], partial)
    assert both["message"].startswith("1 check could not be run") and "; 1 check ran" in both["message"]
    assert vo.not_checked_entry(None, []) is None


def test_not_checked_message_caps_the_column_list():
    cols = [f"k{i}" for i in range(20)]
    e = vo.not_checked_entry([{"rule_id": 1, "rule_name": "A", "message": "m", "columns": cols}])
    assert "and 8 more" in e["message"] and len(e["columns"]) == 20


def test_countable_leaves_out_only_the_not_checked_summary():
    nv = vo.not_validated_entry("r")
    assert vo.countable([_ROW_EXC, _NOT_CHECKED, nv]) == [_ROW_EXC, nv]


# ── what a setup fills ────────────────────────────────────────────────────────
def test_filled_cols_from_mapping_reads_the_rules_not_the_data():
    mapping = {"Out": {
        "copied": {"kind": "copy", "source": "In A"},
        "copied_with_default": {"kind": "copy", "source": None, "default": "UNK"},
        "no_source": {"kind": "copy", "source": None},
        "constant": {"kind": "const", "value": "GBP"},
        "blank_constant": {"kind": "const", "value": ""},
        "from_contract": {"kind": "const", "value": "@contract:limit"},
        "tab_name": {"kind": "source_sheet"},
        "computed": {"kind": "transform", "op": "mul", "operands": []},
        "unknown_kind": {"kind": "lookup"},
        "not_a_rule": "x",
    }}
    assert vo.filled_cols_from_mapping(mapping) == {"Out": [
        "copied", "copied_with_default", "constant", "from_contract", "tab_name", "computed"]}
    assert vo.filled_cols_from_mapping(None) == {}


def test_filled_cols_from_structure_and_active_schema():
    structure = {"sheets": [{"sheet_name": "S", "columns": [
        {"column_name": "canon", "canonical_field": "policy_number"},
        {"column_name": "static", "static_value": 0},
        {"column_name": "nothing"},
        {"column_name": "off", "canonical_field": "premium", "active": False},
        {"column_name": None, "canonical_field": "x"},
    ]}]}
    assert vo.filled_cols_from_structure(structure) == {"S": ["canon", "static"]}
    assert vo.active_schema_cols(structure) == {"S": ["canon", "static", "nothing"]}
    assert vo.template_schema_cols(structure) == {"S": ["canon", "static", "nothing", "off"]}
    assert vo.inactive_cols(structure) == {"S": ["off"]}
    assert vo.inactive_cols({"sheets": [{"sheet_name": "T", "columns": [{"column_name": "a"}]}]}) == {}


def test_input_row_count_leaves_out_the_setup_supplement():
    landing = {"sheets": {
        "Data": {"rows": [{}, {}, {}]},
        "Extra": {"rows": [{}, {}]},
        "Extra (supp 2)": {"rows": [{}]},
    }}
    supplement = {"enabled": True, "landing": {"sheets": {"Extra": {"rows": [{}]}}}}
    assert vo.input_row_count(landing, supplement) == 3
    assert vo.input_row_count(landing, {"enabled": False, **{"landing": supplement["landing"]}}) == 6
    assert vo.input_row_count(landing, None) == 6
    assert vo.input_row_count(None, None) == 0


def test_routed_row_count_reads_only_the_routed_sheets():
    landing = {"sheets": {
        "Risks ": {"rows": [{}, {}, {}]},
        "Notes": {"rows": [{}, {}]},
        "Empty": {"rows": []},
        "Ref (supp 1)": {"rows": [{}]},
    }}
    supplement = {"enabled": True, "landing": {"sheets": {"Ref": {"rows": [{}]}}}}

    def routing(*names):
        return {"routes": [{"output_sheet": "Out", "sources": [{"input_sheet": n} for n in names]}]}

    # matched the way apply_routing matches (case / spacing), counted once
    assert vo.routed_row_count(landing, supplement, routing("risks", "RISKS")) == 3
    # a routed sheet that is there but empty is 0, not "missing"
    assert vo.routed_row_count(landing, supplement, routing("Empty")) == 0
    # none of the routed sheets in the file — nor a supplement sheet — is None
    assert vo.routed_row_count(landing, supplement, routing("Other", "Ref (supp 1)")) is None
    assert vo.routed_row_count(landing, supplement, None) is None


# ── the renderer's summary-row test, measured over the mapped columns ─────────
def test_non_data_row_values_measures_the_mapped_columns():
    import exporter
    names = [f"c{i}" for i in range(40)]
    mapped = names[:10]
    row = {n: None for n in names}
    for n in mapped[:6]:
        row[n] = "1234"                                   # six bare numbers
    vals = [row[n] for n in names]
    # over 40 columns six numbers read as a sparse totals row…
    assert exporter._is_non_data_row_values(vals) is True
    # …over the 10 the setup fills they are a dense data row.
    assert exporter._is_non_data_row_values(vals, names, mapped) is False
    # wording is still found anywhere, and the default is unchanged
    worded = list(vals)
    worded[30] = "Grand Total"
    assert exporter._is_non_data_row_values(worded, names, mapped) is True
    assert exporter._is_non_data_row_values(vals, names, None) is True
    assert exporter._is_non_data_row_values(vals, names, ["unknown"]) is True


def test_projected_columns_are_the_keys_the_rows_carry():
    import direct_render
    rows = [{"a": 1, "b": None}, {"b": 2, "c": 3}]
    assert direct_render._projected_columns(rows) == ["a", "b", "c"]
    assert direct_render._projected_columns([]) == []


# ── which stored mapping may be handed back as already decided ────────────────
def _confirmed_mapping_fn():
    """direct_routes._confirmed_mapping, read out of its source (the module
    queries the database on import) and bound to the DB-free direct_mapper."""
    import direct_mapper
    path = os.path.join(_HERE, "direct_routes.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    [fn] = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_confirmed_mapping"]
    ns = {"dm": direct_mapper, "Optional": __import__("typing").Optional}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns["_confirmed_mapping"], direct_mapper


def test_confirmed_mapping_drops_unreviewed_proposals_only():
    fn, dm = _confirmed_mapping_fn()
    structure = {"sheets": [{"sheet_name": "Out", "columns": [
        {"column_name": "Kept As Proposed"}, {"column_name": "Changed By Person"},
        {"column_name": "Added By Person"}, {"column_name": "Confirmed Before"},
        {"column_name": "Constant"},
    ]}]}
    key = {f["column_name"]: f["field_key"] for f in dm._output_fields_for_sheet(structure, "Out")}
    mapping = {"Out": {
        "Kept As Proposed": {"kind": "copy", "source": "in1"},
        "Changed By Person": {"kind": "copy", "source": "in_person"},
        "Added By Person": {"kind": "copy", "source": "in3"},
        "Confirmed Before": {"kind": "copy", "source": "in4"},
        "Constant": {"kind": "const", "value": "X"},
    }}
    decisions = {"Out": [
        {"field_key": key["Kept As Proposed"], "source": "in1", "method": "SEMANTIC", "status": "AUTO_MAPPED"},
        {"field_key": key["Changed By Person"], "source": "in2", "method": "ALIAS", "status": "AUTO_MAPPED"},
        {"field_key": key["Added By Person"], "source": None, "method": None, "status": "UNMAPPED"},
        {"field_key": key["Confirmed Before"], "source": "in4", "method": dm.sm.MANUAL,
         "status": "MANUALLY_CONFIRMED"},
        {"field_key": key["Constant"], "source": None, "method": None, "status": "NOT_FROM_INPUT"},
    ]}
    got = fn(mapping, decisions, False, structure)
    assert set(got["Out"]) == {"Changed By Person", "Added By Person", "Confirmed Before", "Constant"}
    # approved: everything stands; nothing stored: nothing to pass
    assert fn(mapping, decisions, True, structure) == mapping
    assert fn(None, decisions, False, structure) is None
    # no decisions were ever recorded: nothing distinguishes a proposal, keep all
    assert fn(mapping, {}, False, structure) == mapping
