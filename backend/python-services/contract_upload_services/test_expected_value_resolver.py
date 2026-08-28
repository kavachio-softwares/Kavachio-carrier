"""
Tests for expected_value_resolver — steps 2-3 (coverage check + gate).

Runnable two ways:
    python contract_upload_services/test_expected_value_resolver.py     # from python-services
    pytest  contract_upload_services/test_expected_value_resolver.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PYSVC = os.path.dirname(_HERE)                       # backend/python-services (for data_model)
sys.path[:0] = [_HERE, _PYSVC]

import expected_value_resolver as r  # noqa: E402


# A realistic mapper spec (spec_by_sheet shape). Note: "100% policy Limit" is
# NOT mapped — exactly the case that produces an empty-actual false critical.
MAPPER_SPEC = {
    "Carrier BDX": {
        "policy_number": "Carrier BDX :: Policy No",
        "net_premium":   "Carrier BDX :: Net Premium to Carrier (USD)",
        "currency_iso":  "Ccy",                      # flat (no sheet prefix)
    }
}
PRESENT_COLUMNS = ["Policy No", "Net Premium to Carrier (USD)", "Ccy"]


# ---- reverse lookup --------------------------------------------------------
def test_reverse_lookup_with_sheet():
    src = r._reverse_lookup("policy_number", MAPPER_SPEC)
    assert src == {"sheet": "Carrier BDX", "column": "Policy No"}

def test_reverse_lookup_flat_value():
    src = r._reverse_lookup("currency_iso", MAPPER_SPEC)
    assert src == {"sheet": None, "column": "Ccy"}

def test_reverse_lookup_bare_segment_match():
    # rule field_path may be dotted; should still match the bare canonical key
    src = r._reverse_lookup("policy.net_premium", MAPPER_SPEC)
    assert src and src["column"] == "Net Premium to Carrier (USD)"

def test_reverse_lookup_unmapped_returns_none():
    assert r._reverse_lookup("100% policy Limit", MAPPER_SPEC) is None


# ---- coverage check (STEP 2) ----------------------------------------------
def test_coverage_ok_when_mapped_and_present():
    cov = r.check_mapping_coverage("net_premium", MAPPER_SPEC, PRESENT_COLUMNS)
    assert cov["ok"] is True
    assert cov["source"]["column"] == "Net Premium to Carrier (USD)"

def test_coverage_missing_mapping():
    cov = r.check_mapping_coverage("100% policy Limit", MAPPER_SPEC, PRESENT_COLUMNS)
    assert cov["ok"] is False and cov["reason"] == "MISSING_MAPPING"

def test_coverage_missing_column():
    spec = {"Sheet1": {"some_field": "Sheet1 :: Not In File"}}
    cov = r.check_mapping_coverage("some_field", spec, PRESENT_COLUMNS)
    assert cov["ok"] is False and cov["reason"] == "MISSING_COLUMN"


# ---- the gate (STEP 3) -----------------------------------------------------
def test_gate_unmapped_field_goes_to_review_not_critical():
    """The '100% policy Limit' case: even with a real rule_spec value, an
    unmapped target must become needs_review/mapping_gap — never an active rule."""
    res = r.resolve_expected(
        "100% policy Limit",
        rule_spec={"operator": "<=", "value": 25000000, "confidence": 0.9},
        mapper_spec=MAPPER_SPEC,
        present_columns=PRESENT_COLUMNS,
    )
    assert res["status"] == "needs_review"
    assert res["root_cause"] == "mapping_gap"
    assert res["review_reason"] == "MISSING_MAPPING"

def test_gate_mapped_with_contract_value_is_active():
    res = r.resolve_expected(
        "net_premium",
        rule_spec={"operator": ">=", "value": 0, "confidence": 0.95},
        mapper_spec=MAPPER_SPEC,
        present_columns=PRESENT_COLUMNS,
    )
    assert res["status"] == "active"
    assert res["source"] == "contract"
    assert res["expected"] == 0

def test_gate_mapped_no_value_no_master_no_samples_is_review():
    res = r.resolve_expected(
        "net_premium",
        rule_spec=None,
        mapper_spec=MAPPER_SPEC,
        present_columns=PRESENT_COLUMNS,
    )
    assert res["status"] == "needs_review"
    assert res["review_reason"] == "NO_EXPECTED"

def test_gate_profiling_suggestion_when_mapped():
    res = r.resolve_expected(
        "net_premium",
        rule_spec=None,
        samples=["1000", "2,000", "$3000"],
        mapper_spec=MAPPER_SPEC,
        present_columns=PRESENT_COLUMNS,
    )
    assert res["status"] == "needs_review"
    assert res["source"] == "profiled"
    assert res["constraint"] == "range"
    assert res["expected"] == {"min": 1000.0, "max": 3000.0}


# ---- data type -------------------------------------------------------------
def test_infer_type():
    assert r.infer_type(["1", "2", "3"]) == "int"
    assert r.infer_type(["1.5", "2"]) == "decimal"
    assert r.infer_type(["2026-01-01", "2026-12-31"]) == "date"
    assert r.infer_type(["abc", "def"]) == "string"
    assert r.infer_type([]) == "string"

def test_resolve_data_type_falls_back_to_inference_for_unknown_field():
    assert r.resolve_data_type("100% policy Limit", ["1000", "2000"]) in ("int", "decimal")


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} tests passed")
