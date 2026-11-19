"""Plan task 23.1 — the stable row key (row_key.py). Pure: no database.

Which column is which comes from the setup only (its column mapping and its
ITD period setting) — never from a column's heading."""
from __future__ import annotations

from datetime import date, datetime

import row_key as rk

COLS = ["Policy Number", "Transaction Type", "Transaction Effective Date",
        "Reporting Period", "Gross Premium", "Insured Name"]
MAPPING = {"Policy Number": "policy_number", "Transaction Type": "premium_transaction_type",
           "Transaction Effective Date": "premium_transaction_effective_date",
           "Insured Name": "policyholder_legal_name",
           "Gross Premium": "premium_transaction_gross_premium_amount"}


def _cols(**over):
    res = rk.resolve_key_columns(COLS, MAPPING, period_column="Reporting Period")
    return {**res["columns"], **over}


def test_the_key_is_the_four_parts_and_never_the_amount():
    res = rk.resolve_key_columns(COLS, MAPPING, period_column="Reporting Period")
    assert res["columns"] == {"policy": "Policy Number", "type": "Transaction Type",
                              "date": "Transaction Effective Date",
                              "period": "Reporting Period"}
    assert res["found_by"] == {"period": "setting", "policy": "mapping", "type": "mapping",
                               "date": "mapping"}
    assert "Gross Premium" not in res["columns"].values()


def test_a_heading_alone_never_names_a_key_column():
    """No word lists: without the setup's mapping nothing is found, however
    obvious the heading looks."""
    res = rk.resolve_key_columns(COLS)
    assert res["columns"] == {"policy": None, "type": None, "date": None, "period": None}
    # the reported period comes only from the setup's ITD setting
    assert rk.resolve_key_columns(COLS, MAPPING)["columns"]["period"] is None


def test_the_mapping_decides_whatever_the_headings_say():
    cols = ["Pol Ref", "Kind", "Eff", "Bdx Month", "Policy Number"]
    res = rk.resolve_key_columns(
        cols, canonical_of={"Pol Ref": "policy_number", "Kind": "premium_transaction_type",
                            "Eff": "premium_transaction_effective_date"},
        period_column="Bdx Month")
    assert res["columns"] == {"policy": "Pol Ref", "type": "Kind", "date": "Eff",
                              "period": "Bdx Month"}
    # a legacy data-model name in a mapping is read as its current field
    res = rk.resolve_key_columns(["Eff"], canonical_of={"Eff": "transaction_eff_dt"})
    assert res["columns"]["date"] == "Eff"


def test_a_policy_effective_date_is_not_the_transaction_effective_date():
    res = rk.resolve_key_columns(["Policy No", "Effective Date of Transaction"],
                                 canonical_of={"Policy No": "policy_number",
                                               "Effective Date of Transaction":
                                               "policy_effective_date"})
    assert res["columns"]["date"] is None


def test_values_are_cleaned_with_the_systems_own_rules():
    assert rk.clean_text("  POL-001 ") == rk.clean_text("pol-001")
    assert rk.clean_text("00123") == rk.clean_text("123")           # itd.norm_value
    assert rk.clean_text(98000.0) == "98000"
    for v in ("2025-08-15", "2025-08-15T00:00:00", "20250815", "15/08/2025",
              45884, datetime(2025, 8, 15, 10, 30), date(2025, 8, 15)):
        assert rk.clean_date(v) == "2025-08-15", v
    # d/m/y the way the setup's date rule reads it, unless the rule names a format
    assert rk.clean_date("08/05/2025") == "2025-05-08"
    assert rk.clean_date("08/05/2025", "%m/%d/%Y") == "2025-08-05"
    # a value no rule can read keeps its cleaned text, so equal values still match
    assert rk.clean_date("Aug 15th") == rk.clean_date(" aug 15TH ")
    assert rk.clean_period("2025-08-31") == rk.clean_period("2025-08") == "2025-08"
    for blank in (None, "", "  ", float("nan")):
        assert rk.clean_text(blank) is None and rk.clean_date(blank) is None


def test_policy_number_and_effective_date_are_mandatory():
    cols = _cols()
    row = {"Policy Number": "P1", "Transaction Type": "New",
           "Transaction Effective Date": "2025-08-01", "Reporting Period": "2025-08"}
    v = rk.row_values(row, cols, rk.PARTS)
    assert rk.missing_mandatory(v) == []
    assert rk.missing_mandatory(rk.row_values({**row, "Policy Number": ""}, cols, rk.PARTS)) == ["policy"]
    # what ingest treats as "no policy number" is no policy number here too
    from ingester import _POLICY_NUMBER_PLACEHOLDERS
    for blank in _POLICY_NUMBER_PLACEHOLDERS:
        v = rk.row_values({**row, "Policy Number": f" {blank.upper()} "}, cols, rk.PARTS)
        assert rk.missing_mandatory(v) == ["policy"], blank
    assert rk.missing_mandatory(rk.row_values({**row, "Transaction Effective Date": None},
                                              cols, rk.PARTS)) == ["date"]
    # the optional parts may be blank
    assert rk.missing_mandatory(rk.row_values({**row, "Transaction Type": "",
                                               "Reporting Period": ""}, cols, rk.PARTS)) == []


def test_the_setups_date_format_is_used_for_the_key():
    cols = _cols()
    row = {"Policy Number": "P1", "Transaction Effective Date": "08/01/2025"}
    us = rk.row_values(row, cols, rk.PARTS, {"Transaction Effective Date": "%m/%d/%Y"})
    assert us["date"] == "2025-08-01"
    assert rk.row_values(row, cols, rk.PARTS)["date"] == "2025-01-08"


def test_parts_used_are_the_ones_both_versions_have():
    full = _cols()
    no_type = _cols(type=None)
    assert rk.parts_for(full, full) == ["policy", "type", "date", "period"]
    assert rk.parts_for(full, no_type) == ["policy", "date", "period"]
    assert rk.parts_for(full, _cols(policy=None)) is None          # sheet not compared
    assert rk.parts_for(_cols(date=None), full) is None


def test_the_key_and_its_text():
    v = {"policy": "pol-1", "type": "new", "date": "2025-08-01", "period": None}
    assert rk.key_of(v, ["policy", "type", "date", "period"]) == ("pol-1", "new", "2025-08-01", "")
    assert rk.key_text(v, ["policy", "date", "period"]) == "pol-1 · 2025-08-01 · (blank)"


def test_a_repeated_heading_line_is_recognised():
    cols = _cols()
    assert rk.is_heading_repeat({"Policy Number": "Policy Number"}, cols)
    assert not rk.is_heading_repeat({"Policy Number": "P1"}, cols)


def test_joined_up_headings_match_across_versions():
    assert rk.heading("TransactionEffectiveDate") == "transaction effective date"
    assert rk.heading("NJ Transaction No") == "nj transaction no"


def test_the_insured_name_column_comes_from_the_mapping_only():
    assert rk.name_column(COLS, MAPPING) == "Insured Name"
    assert rk.name_column(COLS) is None                              # no heading guess
    assert rk.name_column(["Account", "Insured"],
                          canonical_of={"Account": "policyholder_legal_name"}) == "Account"
    assert rk.name_column(["Insured"], {"Insured": "insured_legal_name"}) == "Insured"  # legacy
    assert rk.name_column(["Insured"], {"Insured": "policyholder_legal_name"},
                          taken=["Insured"]) is None
