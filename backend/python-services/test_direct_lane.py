"""Standalone tests for the direct Input→Output engine (no DB / no network).

Run:  python test_direct_lane.py     (or pytest test_direct_lane.py)
"""
import pandas as pd

import direct_lane as dl
import direct_mapper as dm


def _sample_input_sheets():
    """3 tabs, identical 10 columns — the 'same columns on every sheet' case."""
    cols = ["Policy No", "Insured Name", "Inception", "Expiry", "Gross Premium",
            "Commission %", "Country", "Currency", "Sum Insured", "Status"]
    def rows(prefix):
        return pd.DataFrame([
            {"Policy No": f"{prefix}-1001", "Insured Name": "ACME Foods",
             "Inception": "20240101", "Expiry": "20241231", "Gross Premium": "10000",
             "Commission %": "15", "Country": "GB", "Currency": "USD",
             "Sum Insured": "500000", "Status": "Bound"},
            {"Policy No": f"{prefix}-1002", "Insured Name": "Globex",
             "Inception": "20240201", "Expiry": "20250131", "Gross Premium": "20000",
             "Commission %": "10", "Country": "US", "Currency": "USD",
             "Sum Insured": "750000", "Status": "Bound"},
        ], columns=cols)
    return {"Property": rows("PROP"), "Casualty": rows("CAS"), "Marine": rows("MAR")}


def _output_structure(sheet_names):
    """Minimal exporter.parse_template-shaped structure."""
    out_cols = ["Policy Reference", "Insured", "Risk Inception Date",
                "Gross Premium (USD)", "Commission Amount", "Net Premium",
                "UMR", "Line of Business"]
    return {"sheets": [{"sheet_name": s,
                        "columns": [{"column_name": c} for c in out_cols]}
                       for s in sheet_names]}


def _column_mapping_for(sheet):
    # Order matters: Commission Amount before Net Premium (Net references it).
    return {
        "Policy Reference":    {"kind": "copy", "source": "Policy No"},
        "Insured":             {"kind": "copy", "source": "Insured Name"},
        "Risk Inception Date": {"kind": "transform", "op": "date_reformat",
                                "source": "Inception", "from_fmt": "%Y%m%d",
                                "to_fmt": "%d/%m/%Y"},
        "Gross Premium (USD)": {"kind": "copy", "source": "Gross Premium"},
        "Commission Amount":   {"kind": "transform", "op": "mul",
                                "operands": [{"in": "Gross Premium"},
                                             {"in": "Commission %"},
                                             {"const": 0.01}]},
        "Net Premium":         {"kind": "transform", "op": "sub",
                                "operands": [{"in": "Gross Premium"},
                                             {"out": "Commission Amount"}]},
        "UMR":                 {"kind": "const", "value": "@contract:UMR"},
        "Line of Business":    {"kind": "const", "value": sheet},
    }


def test_landing_capture():
    landing = dl.build_landing_record(_sample_input_sheets())
    assert set(landing["sheets"]) == {"Property", "Casualty", "Marine"}
    assert landing["row_count"] == 6
    assert landing["sheets"]["Property"]["rows"][0]["Policy No"] == "PROP-1001"
    print("✓ landing capture: 3 sheets, 6 rows")


def test_pair_routing_by_name():
    landing = dl.build_landing_record(_sample_input_sheets())
    routing = dl.propose_sheet_routing(list(landing["sheets"]),
                                       ["Property", "Casualty", "Marine"])
    assert routing["mode"] == "pair" and routing["confidence"] == "high"
    routed = dl.apply_routing(landing, routing)
    assert len(routed["Property"]) == 2
    assert routed["Property"][0][dl.SOURCE_SHEET_KEY] == "Property"
    print("✓ pair routing by name (1→1) with source-sheet tag")


def test_heuristic_column_mapping():
    mapping, cands, unmatched = dm.heuristic_match(
        ["Policy No", "Insured Name", "Gross Premium"],
        ["Policy No", "Gross Premium (USD)", "UMR"])
    assert mapping["Policy No"] == {"kind": "copy", "source": "Policy No"}
    # fuzzy: "Gross Premium (USD)" ~ "Gross Premium"
    assert mapping["Gross Premium (USD)"]["source"] == "Gross Premium"
    assert "UMR" in unmatched  # no input source → user/contract fills it
    print("✓ heuristic column mapping (exact + fuzzy + unmatched)")


def test_projection_full():
    landing = dl.build_landing_record(_sample_input_sheets())
    routing = dl.propose_sheet_routing(list(landing["sheets"]),
                                       ["Property", "Casualty", "Marine"])
    routed = dl.apply_routing(landing, routing)
    mapping = {s: _column_mapping_for(s) for s in ["Property", "Casualty", "Marine"]}
    out = dl.project_to_output(routed, mapping, constants={"UMR": "B1234CARRIER2024"})

    r0 = out["Property"][0]
    assert r0["Policy Reference"] == "PROP-1001"
    assert r0["Risk Inception Date"] == "01/01/2024"
    assert r0["Commission Amount"] == 1500.0          # 10000 * 15%
    assert r0["Net Premium"] == 8500.0                # 10000 - 1500
    assert r0["UMR"] == "B1234CARRIER2024"            # contract constant
    assert r0["Line of Business"] == "Property"
    r1 = out["Property"][1]
    assert r1["Commission Amount"] == 2000.0          # 20000 * 10%
    assert r1["Net Premium"] == 18000.0
    print("✓ projection: copy + date_reformat + mul + sub + contract const + label")


def test_merge_many_to_one_with_label():
    """3 input tabs → 1 output tab, source tab preserved via source_sheet rule."""
    landing = dl.build_landing_record(_sample_input_sheets())
    routing = dl.propose_sheet_routing(list(landing["sheets"]), ["All Premium"])
    assert routing["mode"] == "merge"
    routed = dl.apply_routing(landing, routing)
    assert len(routed["All Premium"]) == 6
    mapping = {"All Premium": {"Policy Reference": {"kind": "copy", "source": "Policy No"},
                               "Line of Business": {"kind": "source_sheet"}}}
    out = dl.project_to_output(routed, mapping)
    lobs = {r["Line of Business"] for r in out["All Premium"]}
    assert lobs == {"Property", "Casualty", "Marine"}   # label column preserved
    print("✓ merge (3→1) with discriminator/label column")


def test_split_one_to_many_by_column():
    """1 input tab → many output tabs, routed by a discriminator filter."""
    df = pd.DataFrame([
        {"Policy No": "A-1", "LOB": "Property", "Gross Premium": "100"},
        {"Policy No": "A-2", "LOB": "Marine", "Gross Premium": "200"},
        {"Policy No": "A-3", "LOB": "Property", "Gross Premium": "300"},
    ])
    landing = dl.build_landing_record({"All Business": df})
    routing = {"version": 1, "mode": "split", "confidence": "low", "routes": [
        {"output_sheet": "Property", "sources": [{"input_sheet": "All Business"}],
         "filter": {"column": "LOB", "equals": "Property"}},
        {"output_sheet": "Marine", "sources": [{"input_sheet": "All Business"}],
         "filter": {"column": "LOB", "equals": "Marine"}},
    ]}
    routed = dl.apply_routing(landing, routing)
    assert len(routed["Property"]) == 2 and len(routed["Marine"]) == 1
    print("✓ split (1→many) by discriminator column")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} direct-lane tests passed.")


if __name__ == "__main__":
    main()
