"""Plan task 23.4 — the reconciliation in the BDX output: the "Previous
submission" sheet and the CSV (version_diff_sheet.py). Pure: no database."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from openpyxl import Workbook, load_workbook

import export_results as xr
import version_diff as vd
import version_diff_sheet as vsh
from test_version_diff import BASE, COLS, compare, land, r, setup

BOXES = ["All", "New", "Changed", "Disappeared", "Restated"]


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


def _sheet(res=None, about=None):
    res = res or _result()
    out = vsh.add_sheet(_book("BDX", "Results"), res, about=about or vsh.about_for(res))
    return load_workbook(io.BytesIO(out))


def _cells(ws) -> list[str]:
    return [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]


def _boxes(ws) -> tuple[list, list]:
    rows = [[c.value for c in row] for row in ws.iter_rows()]
    i = next(i for i, x in enumerate(rows) if x[:len(BOXES)] == BOXES)
    return rows[i][:len(BOXES)], rows[i + 1][:len(BOXES)]


def _view(ws) -> tuple[int, list]:
    """(header row number, data rows as cell lists) of the bordereau view."""
    rows = list(ws.iter_rows())
    head = next(i for i, x in enumerate(rows) if [c.value for c in x[:2]] == ["Outcome",
                                                                            "Row in this file"])
    body = []
    for x in rows[head + 1:]:
        if not any(c.value is not None for c in x):
            break
        body.append(x)
    return head + 1, body


def _rgb(c) -> str:
    return (c.fill.fgColor.rgb or "")[-6:].upper() if c.fill and c.fill.fill_type == "solid" else ""


def test_counts_on_top_then_the_bordereau():
    wb = _sheet()
    assert wb.sheetnames == ["BDX", "Results", "Previous submission"]
    ws = wb["Previous submission"]
    cells = _cells(ws)
    assert cells[0] == vsh.TITLE
    assert ("It is compared with version 2, the last version that was sent to the carrier."
            in cells)
    assert ("A row counts as the same row in both versions when its policy number, "
            "transaction type, transaction effective date and reported period are the same."
            in cells)
    labels, counts = _boxes(ws)
    assert counts == [5, 2, 0, 1, 1]
    # only the task's four outcomes: nothing to check by hand, nothing left uncompared
    assert not any(w in c for c in cells for w in ("Requires Review", "Not compared",
                                                    "Matched", "Missing", "Unexpected"))
    # what each colour means, in plain sentences, for the outcomes in the table
    assert vsh.LEGEND_TITLE in cells
    assert "A new row: it was not in version 2." in cells
    assert ("This row was in version 2 but is not in this file. It is shown crossed out, "
            "where it used to be.") in cells
    assert ("The same row as in version 2, but an amount is different. The changed amounts "
            "are highlighted: point at one to see what it was in version 2.") in cells
    assert not any(c.startswith("The same row as in version 2, but some details") for c in cells)
    # no summary sentences: the counts are in the boxes, the changes in the table
    assert not any(c.startswith(("Changed rows", "Gross Premium:", "Compared with"))
                   or "Every difference is explained" in c for c in cells)


def test_every_row_in_file_order_with_the_missing_row_put_back_where_it_was():
    ws = _sheet()["Previous submission"]
    head, body = _view(ws)
    heads = [c.value for c in ws[head]]
    assert heads == ["Outcome", "Row in this file", "Row in version 2", *COLS]
    got = [(x[0].value, x[1].value, x[2].value, x[3].value) for x in body]
    assert got == [("Unchanged", 1, 1, "P1"), ("Restated", 2, 2, "P2"), ("Disappeared", None, 3, "P3"),
                   ("New", 3, None, None), ("New", 4, None, "P2")]


def test_a_changed_value_is_highlighted_with_the_accepted_value_in_its_note():
    ws = _sheet()["Previous submission"]
    head, body = _view(ws)
    heads = [c.value for c in ws[head]]
    changed = body[1]
    gross = changed[heads.index("Gross Premium")]
    assert gross.value == 2500 and gross.font.b
    assert _rgb(gross) == vsh._CELL_TINT[vd.RESTATED]
    assert gross.comment.text == "Version 2: 2,000.00"
    insured = changed[heads.index("Insured Name")]
    assert _rgb(insured) == "" and insured.comment is None


def test_what_happened_is_a_note_on_the_outcome_not_a_column():
    ws = _sheet()["Previous submission"]
    head, body = _view(ws)
    assert "What happened" not in [c.value for c in ws[head]]
    matched, changed = body[0], body[1]
    assert matched[0].comment is None
    note = changed[0].comment.text
    assert "silent restatement" in note
    assert "What changed: Gross Premium: 2,000.00 → 2,500.00" in note


def test_new_rows_are_tinted_and_missing_rows_struck_through():
    ws = _sheet()["Previous submission"]
    _head, body = _view(ws)
    missing, new = body[2], body[4]
    assert all(c.font.strike for c in missing[3:])
    assert _rgb(missing[4]) == vsh._TINT[vd.DISAPPEARED][0]
    assert _rgb(new[4]) == vsh._TINT[vd.NEW][0]
    assert new[0].comment.text == ("New row, transaction type 'Endorsement': a genuine new "
                                   "transaction (endorsement).")


def test_no_cell_uses_the_colours_the_app_reads_as_exceptions():
    # exporter._INVALID_FILL_RGB / _WARNING_FILL_RGB, as main._flag_fill_rgbs reads them
    ws = _sheet()["Previous submission"]
    used = {_rgb(c) for row in ws.iter_rows() for c in row} - {""}
    assert used and not used & {"FFC7CE", "FFE0B2"}


def test_nothing_is_frozen_and_the_bordereau_has_filter_arrows():
    ws = _sheet()["Previous submission"]
    assert ws.freeze_panes is None
    head, body = _view(ws)
    assert ws.auto_filter.ref == f"A{head}:{ws.cell(row=head, column=3 + len(COLS)).column_letter}" \
                                 f"{head + len(body)}"


def test_wrapped_text_gets_a_tall_enough_row():
    assert vsh._lines_needed("Net Premium to London in Original Currency", 28) == 2
    assert vsh._lines_needed("Summit Transit Services LLC", 26) == 1
    assert vsh._lines_needed("From changed rows", 13, bold=True) == 3


def test_dates_the_setup_maps_to_a_date_field_are_shown_as_dates():
    from datetime import date
    ws = _sheet()["Previous submission"]
    head, body = _view(ws)
    col = [c.value for c in ws[head]].index("Transaction Effective Date")
    assert body[0][col].value.date() == date(2025, 8, 1)
    assert body[0][col].number_format == "DD MMM YYYY"


def test_column_widths_follow_the_content():
    rows = [["Outcome", "Field"],
            ["Requires Review", 1234567.5]]
    w = vsh.column_widths([(rows, set())])
    # left-aligned text gets room for its indent, so "Outcome" / "Requires
    # Review" never break mid-word; amounts are right-aligned, no indent
    assert w[0] == round(len("Requires Review") * 1.2) + 2 + vsh._INDENT
    assert vsh.column_widths([([["Outcome"]], set())])[0] >= round(len("Outcome") * 1.15) + 3
    assert vsh.column_widths([([["Commission Rate"], [12.0]], set())])[0] >= len("Commission") + 3
    assert w[1] == len("1,234,567.50") + 2
    names = [["Amount"], ["Net Premium to London in Original Currency"]]
    longest_word = max(len(w) for w in names[1][0].split())
    assert vsh.column_widths([(names, {0})])[0] == max(vsh._MIN_WIDTH,
                                                       round(longest_word * 1.2) + 2 + vsh._INDENT)
    assert vsh.column_widths([(names, set())])[0] == vsh._MAX_WIDTH


def test_amounts_are_the_mapped_money_columns_without_rates():
    fields = {"Gross Premium": "premium_transaction_total_gross_written_premium_amount",
              "Commission Rate": "premium_transaction_commission_percent",
              "Commission Amount": "premium_transaction_commission_amount",
              "Net Premium to London": "premium_transaction_net_premium_amount"}
    assert vsh._amounts(list(fields), fields) == ["Gross Premium", "Commission Amount",
                                                  "Net Premium to London"]


def test_checked_is_shown_in_the_versions_own_time_zone():
    res = _result()
    res["this"]["received_at"] = "2026-10-16T14:30:00+05:30"
    about = vsh.about_for(res, checked=datetime(2026, 10, 16, 9, 4, tzinfo=timezone.utc))
    assert about["This file"] == "This file is version 3 of this submission, received 16 Oct 2026."
    assert vsh._about_lines(about)[-1] == "Checked on 16 Oct 2026 at 14:34 (UTC+05:30)."
    plain = vsh.about_for(_result(), checked=datetime(2026, 10, 16, 9, 4, tzinfo=timezone.utc))
    assert vsh._about_lines(plain)[-1] == "Checked on 16 Oct 2026 at 09:04 (UTC)."
    assert vsh._about_lines(plain)[0] == "This file is version 3 of this submission."


def test_nothing_moved_shows_every_row_as_unchanged():
    res = compare(land(*BASE), land(*BASE))
    res["accepted"] = {"version": 2}
    ws = _sheet(res)["Previous submission"]
    assert _boxes(ws)[1] == [3, 0, 0, 0, 0]
    assert vsh.LEGEND_TITLE not in _cells(ws)          # no colours in the table to explain
    _head, body = _view(ws)
    assert [x[0].value for x in body] == ["Unchanged"] * 3


def test_the_colour_guide_lists_only_the_colours_the_table_uses():
    cur = [BASE[0], BASE[1], r("P3", "Endorsement", "2025-08-20", 500)]
    res = compare(land(*BASE[:2]), land(*cur))
    res["accepted"] = {"version": 4}
    cells = _cells(_sheet(res)["Previous submission"])
    assert "A new row: it was not in version 4." in cells
    assert not any(c.startswith("This row was in version 4") for c in cells)       # no missing row
    assert not any(c.startswith("Some values in this row") for c in cells)         # nothing changed


def test_totals_are_mentioned_only_when_the_rows_do_not_explain_them():
    res = _result()
    assert vsh.in_short(res) == []
    gross = next(c for c in res["summary"]["money"] if c == "Gross Premium")
    res["summary"]["money"][gross]["control_gap"] = 150.0
    lines = vsh.in_short(res)
    assert lines == ["Please check the totals: Gross Premium is 4,005.00 in this file and "
                     "6,000.00 in version 2, and +150.00 of that difference is not explained "
                     "by the rows below."]
    assert lines[0] in _cells(_sheet(res)["Previous submission"])


def test_a_file_that_could_not_be_compared_says_why_and_what_to_map():
    mapping = {**setup()["canonical_of"],
               "Transaction Effective Date": "policy_effective_date"}
    res = compare(land(*BASE), land(*BASE), accepted_setup=setup(canonical_of=mapping),
                  current_setup=setup(canonical_of=mapping))
    res["accepted"] = {"version": 2}
    cells = _cells(_sheet(res)["Previous submission"])
    assert any("the setup maps no column to transaction effective date" in c
               and "Map it in Bordereau Setup" in c for c in cells)
    assert "All" not in cells and "Outcome" not in cells


def test_a_clashing_name_gets_a_number():
    out = vsh.add_sheet(_book("Previous submission"), _result())
    assert load_workbook(io.BytesIO(out)).sheetnames[-1] == "Previous submission 2"


def test_a_broken_workbook_comes_back_unchanged():
    assert vsh.add_sheet(b"not a workbook", _result()) == b"not a workbook"


def test_the_results_counting_skips_the_sheet():
    out = vsh.add_sheet(_book("BDX"), _result())
    wb = load_workbook(io.BytesIO(out))
    grid = [{"sheet": ws.title, "rows": [[c.value for c in row] for row in ws.iter_rows()]}
            for ws in wb.worksheets]
    grid[0]["rows"] = [["Policy"], ["P1"]]
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


def test_a_very_long_file_is_capped_on_the_sheet_but_not_in_the_csv(monkeypatch):
    monkeypatch.setenv("PREVIOUS_SUBMISSION_MAX_LINES", "2")
    assert vsh.max_lines() == 2
    res = _result()
    ws = _sheet(res)["Previous submission"]
    assert any("Only the first 2 rows are shown here" in c for c in _cells(ws))
    _head, body = _view(ws)
    assert len(body) == 2
    fields = vsh.money_fields(res)
    assert len(vsh.to_csv(res).strip().splitlines()) == 2 + sum(
        len(vsh.value_lines(x, fields)) for x in res["rows"])
