"""Plan task 23.4 — the reconciliation as people read it: the "Previous
Submission" tab of the exceptions screen and the CSV (version_diff_sheet.py).
Pure: no database."""
from __future__ import annotations

import csv
import io

from openpyxl import Workbook

import export_results as xr
import exporter
import version_diff as vd
import version_diff_sheet as vsh
from test_version_diff import BASE, COLS, compare, land, r, setup


def _book(*sheets) -> bytes:
    wb = Workbook()
    wb.active.title = sheets[0] if sheets else "BDX"
    for name in sheets[1:]:
        wb.create_sheet(name)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _result():
    """P1 unchanged, P2's premium restated, P3 gone, a new row with no policy
    number, and a new endorsement on P2."""
    cur = [BASE[0], r("P2", "New Business", "2025-08-03", 2500), r("", "New", "2025-08-09", 5),
           r("P2", "Endorsement", "2025-08-20", 500)]
    res = compare(land(*BASE), land(*cur))
    res["this"], res["accepted"] = {"version": 3}, {"version": 2, "status_text": "Delivered"}
    return res


def _sheet(scr: dict, name: str = "BDX") -> dict:
    return next(s for s in scr["sheets"] if s["sheet"] == name)


def test_the_counts_are_the_tasks_four_outcomes_and_the_rest():
    scr = vsh.screen(_result())
    assert scr["state"] == "compared" and scr["message"] is None
    assert [(o["word"], o["count"]) for o in scr["outcomes"]] == [
        ("New", 2), ("Changed", 0), ("Disappeared", 1), ("Restated", 1), ("Unchanged", 1)]
    assert scr["total"] == 5
    words = {o["word"] for o in scr["outcomes"]}
    assert not words & {"Requires Review", "Not compared", "Matched", "Missing", "Unexpected"}


def test_each_count_says_what_it_means_in_plain_words():
    scr = vsh.screen(_result())
    meaning = {o["code"]: o["meaning"] for o in scr["outcomes"]}
    assert meaning[vd.RESTATED] == "The same row as in version 2, but an amount differs."
    assert meaning[vd.DISAPPEARED] == "In version 2, not in this file."
    assert scr["all_meaning"] == ("Every row of this file, plus the rows that disappeared "
                                  "since version 2.")
    legend = {o["code"]: o["legend"] for o in scr["outcomes"]}
    assert legend[vd.NEW] == "not in version 2" and legend[vd.RESTATED] == "amount differs"


def test_about_says_which_versions_and_how_rows_match():
    res = _result()
    assert vsh.screen(res)["about"] == [
        "Version 3 compared with version 2, the last version sent to the carrier.",
        "Rows match when their policy number, transaction type, transaction effective date "
        "and reported period are the same."]
    res["this"]["received_at"] = "2026-10-16T14:30:00+05:30"
    res["accepted"]["received_at"] = "2026-10-13T10:00:00+05:30"
    assert vsh.screen(res)["about"][0] == (
        "Version 3 (received 16 Oct 2026) compared with version 2, the last version sent to "
        "the carrier (received 13 Oct 2026).")


def test_every_row_in_file_order_with_the_missing_row_put_back_where_it_was():
    sh = _sheet(vsh.screen(_result()))
    assert sh["columns"] == COLS
    got = [(e["outcome"], e["row"], e["accepted_row"], e["cells"][0], e["gone"])
           for e in sh["entries"]]
    assert got == [(vd.UNCHANGED, 1, 1, "P1", False), (vd.RESTATED, 2, 2, "P2", False),
                   (vd.DISAPPEARED, None, 3, "P3", True), (vd.NEW, 3, None, "", False),
                   (vd.NEW, 4, None, "P2", False)]
    assert sh["counts"] == {vd.UNCHANGED: 1, vd.RESTATED: 1, vd.DISAPPEARED: 1, vd.NEW: 2}


def test_several_missing_rows_go_back_after_the_row_before_them():
    acc = [r(f"P{i}", "New Business", "2025-08-01", 100 * i) for i in range(1, 7)]
    cur = [acc[0], acc[3], acc[5]]
    res = compare(land(*acc), land(*cur))
    res["accepted"] = {"version": 1}
    sh = _sheet(vsh.screen(res))
    assert [(e["cells"][0], e["outcome"]) for e in sh["entries"]] == [
        ("P1", vd.UNCHANGED), ("P2", vd.DISAPPEARED), ("P3", vd.DISAPPEARED),
        ("P4", vd.UNCHANGED), ("P5", vd.DISAPPEARED), ("P6", vd.UNCHANGED)]


def test_a_changed_value_carries_the_value_it_had():
    sh = _sheet(vsh.screen(_result()))
    restated = sh["entries"][1]
    gross, name = COLS.index("Gross Premium"), COLS.index("Insured Name")
    assert restated["cells"][gross] == "2,500.00"
    assert restated["was"][str(gross)] == "2,000.00"
    assert str(name) not in restated["was"]
    assert "silent restatement" in restated["note"]
    assert "What changed: Gross Premium: 2,000.00 → 2,500.00" in restated["note"]
    assert sh["entries"][0]["note"] == "" and sh["entries"][0]["was"] == {}


def test_cells_read_the_way_people_write_them():
    sh = _sheet(vsh.screen(_result()))
    first = sh["entries"][0]["cells"]
    assert first[COLS.index("Gross Premium")] == "1,000.00"
    assert first[COLS.index("Commission %")] == "10"            # a rate is not an amount
    assert first[COLS.index("Transaction Effective Date")] == "01 Aug 2025"
    show = vsh.view_tables(_result())[0]["show"]
    assert show("Gross Premium", "1234.5") == "1,234.50"
    assert show("Policy Number", 951001.0) == "951001"           # an id, not an amount
    assert show("Insured Name", None) == show("Insured Name", "  ") == ""


def test_only_columns_the_setup_maps_to_a_date_field_read_as_dates():
    res = _result()
    assert res["view"]["BDX"]["date_columns"] == ["Transaction Effective Date"]
    res["view"]["BDX"]["date_columns"] = []
    sh = _sheet(vsh.screen(res))
    assert sh["entries"][0]["cells"][COLS.index("Transaction Effective Date")] == "2025-08-01"


def test_a_missing_row_shows_what_it_was():
    sh = _sheet(vsh.screen(_result()))
    gone = sh["entries"][2]
    assert gone["gone"] and gone["cells"][COLS.index("Gross Premium")] == "3,000.00"
    assert gone["note"].startswith("Was in the accepted version, is not in this file.")


def test_no_colour_is_one_the_grids_use_for_exceptions():
    used = {c.lstrip("#").upper() for st in vsh.STYLE.values() for c in st.values() if c}
    assert used and not used & {exporter._INVALID_FILL_RGB, exporter._WARNING_FILL_RGB}
    assert set(vsh.STYLE) == set(vsh.ORDER) == set(vd.OUTCOMES)


def test_nothing_moved_shows_every_row_as_unchanged():
    res = compare(land(*BASE), land(*BASE))
    res["accepted"] = {"version": 2}
    scr = vsh.screen(res)
    assert [o["count"] for o in scr["outcomes"]] == [0, 0, 0, 0, 3]
    assert [e["outcome"] for e in _sheet(scr)["entries"]] == [vd.UNCHANGED] * 3


def test_totals_are_mentioned_only_when_the_rows_do_not_explain_them():
    res = _result()
    assert vsh.screen(res)["warnings"] == []
    res["summary"]["money"]["Gross Premium"]["control_gap"] = 150.0
    assert vsh.screen(res)["warnings"] == [
        "Check the totals: Gross Premium is 4,005.00 in this file and 6,000.00 in version 2, "
        "and +150.00 of that difference is not explained by the rows."]


def test_a_file_that_could_not_be_compared_says_why_and_what_to_map():
    mapping = {**setup()["canonical_of"],
               "Transaction Effective Date": "policy_effective_date"}
    res = compare(land(*BASE), land(*BASE), accepted_setup=setup(canonical_of=mapping),
                  current_setup=setup(canonical_of=mapping))
    res["accepted"] = {"version": 2}
    scr = vsh.screen(res)
    assert scr["state"] == "not_compared"
    assert "the setup maps no column to transaction effective date" in scr["message"]
    assert "Map it in Bordereau Setup" in scr["message"]
    assert scr["about"] == [] and _sheet(scr)["entries"] == []
    assert "Map it in Bordereau Setup" in _sheet(scr)["message"]


def test_nothing_to_compare_has_only_its_reason():
    res = vd._empty("no_accepted_version", "Nothing to compare yet.")
    scr = vsh.screen(res)
    assert scr["message"] == "Nothing to compare yet." and scr["sheets"] == []
    assert scr["total"] == 0


def test_the_results_counting_skips_an_old_outputs_sheet():
    grid = [{"sheet": "BDX", "rows": [["Policy"], ["P1"]]},
            {"sheet": vsh.SHEET_NAME, "rows": [[vsh.TITLE], ["…"]]}]
    sheets, _cols = xr.grid_to_sheets(grid)
    assert list(sheets) == ["BDX"]
    assert vsh.TITLE in xr.REPORT_TITLES


def test_csv_has_one_line_per_changed_value():
    res = _result()
    lines = list(csv.reader(io.StringIO(vsh.to_csv(res))))
    assert lines[0][0] == vsh.TITLE
    assert lines[1] == list(vsh.LINE_COLUMNS)
    fields = vsh.money_fields(res)
    assert len(lines) == 2 + sum(len(vsh.value_lines(x, fields)) for x in res["rows"])
    # the task's order: new, changed, disappeared, restated
    assert list(dict.fromkeys(x[0] for x in lines[2:])) == ["New", "Disappeared", "Restated"]
    first = next(x for x in lines if x[0] == "Restated")
    assert first[:9] == ["Restated", "P2", "Acme", "2", "2", "Gross Premium", "2000.0",
                         "2500.0", "500.0"]
    assert any(x[0] == "New" and x[9].endswith("a genuine new transaction (endorsement).")
               for x in lines)


def test_amounts_are_the_mapped_money_columns_without_rates():
    fields = {"Gross Premium": "premium_transaction_total_gross_written_premium_amount",
              "Commission Rate": "premium_transaction_commission_percent",
              "Commission Amount": "premium_transaction_commission_amount",
              "Net Premium to London": "premium_transaction_net_premium_amount"}
    assert vsh._amounts(list(fields), fields) == ["Gross Premium", "Commission Amount",
                                                  "Net Premium to London"]
