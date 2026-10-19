"""Plan task 23.4 — "Download BDX" rebuilds the "Previous submission" sheet of
an output made earlier (version_diff_store.refresh_download and
version_diff_sheet.add_sheet(replace=True)). Pure: no database."""
from __future__ import annotations

import io
import re
import zipfile

import pytest
from openpyxl import Workbook, load_workbook

import version_diff_sheet as vsh
import version_diff_store as vds
from test_version_diff_sheet import _book, _result


def _old_output(own_sheet: bool = False) -> bytes:
    """An output as stored: BDX + Results + an old-layout reconciliation sheet,
    and a totals formula whose result was written in after the last save
    (as direct_render.inject_summary_formulas does)."""
    wb = Workbook()
    wb.active.title = "BDX"
    wb["BDX"].append(["Policy", "Gross"])
    wb["BDX"].append(["P1", 10])
    wb["BDX"].append(["P2", 20])
    wb["BDX"]["B4"] = "=SUM(B2:B3)"
    wb.create_sheet("Results")
    if own_sheet:
        wb.create_sheet(vsh.SHEET_NAME)["A1"] = "The template's own notes"
    old = wb.create_sheet(vsh.SHEET_NAME + (" 2" if own_sheet else ""))
    old["A1"], old["A2"] = vsh.TITLE, "Rows matched on: Policy number + Transaction type"
    buf = io.BytesIO()
    wb.save(buf)
    z = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
        for item in z.infolist():
            data = z.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = re.sub(rb"(<f>SUM\(B2:B3\)</f>)<v\s*(?:/>|></v>)", rb"\1<v>30</v>", data)
                assert b"<v>30</v>" in data
            zo.writestr(item, data)
    return out.getvalue()


def _saved_result(xlsx: bytes, sheet: str, cell: str):
    return load_workbook(io.BytesIO(xlsx), data_only=True)[sheet][cell].value


def test_the_old_sheet_is_replaced_not_added_to():
    out = vsh.add_sheet(_old_output(), _result(), about=vsh.about_for(_result()), replace=True)
    wb = load_workbook(io.BytesIO(out))
    assert wb.sheetnames == ["BDX", "Results", vsh.SHEET_NAME]
    cells = [c.value for row in wb[vsh.SHEET_NAME].iter_rows() for c in row if c.value]
    assert not any(str(c).startswith("Rows matched on:") for c in cells)   # old layout gone
    assert vsh.LEGEND_TITLE in cells


def test_a_templates_own_sheet_of_that_name_is_kept():
    out = vsh.add_sheet(_old_output(own_sheet=True), _result(), replace=True)
    wb = load_workbook(io.BytesIO(out))
    assert wb[vsh.SHEET_NAME]["A1"].value == "The template's own notes"
    assert [n for n in wb.sheetnames if n.startswith(vsh.SHEET_NAME)] == [
        vsh.SHEET_NAME, vsh.SHEET_NAME + " 2"]
    assert wb[vsh.SHEET_NAME + " 2"]["A1"].value == vsh.TITLE


def test_saved_formula_results_survive_the_rebuild():
    old = _old_output()
    assert _saved_result(old, "BDX", "B4") == 30
    out = vsh.add_sheet(old, _result(), replace=True)
    assert _saved_result(out, "BDX", "B4") == 30
    assert load_workbook(io.BytesIO(out))["BDX"]["B4"].value == "=SUM(B2:B3)"


def test_has_sheet_reads_the_zip():
    assert vsh.has_sheet(_old_output())
    assert not vsh.has_sheet(_book("BDX", "Results"))
    assert not vsh.has_sheet(b"not a workbook")


@pytest.fixture
def compared(monkeypatch):
    calls = []

    def fake(s, *, export_id=None, **_kw):
        calls.append(export_id)
        return _result()
    monkeypatch.setattr(vds, "reconcile", fake)
    monkeypatch.delenv("PREVIOUS_SUBMISSION_SHEET", raising=False)
    return calls


def test_a_download_gets_the_sheet_rebuilt(compared):
    out = vds.refresh_download(None, 128, _old_output(), "x.xlsx")
    assert compared == [128]
    assert vsh.LEGEND_TITLE in [c.value for row in load_workbook(io.BytesIO(out))[
        vsh.SHEET_NAME].iter_rows() for c in row]


def test_the_stored_file_is_served_as_is_when_there_is_nothing_to_rebuild(compared, monkeypatch):
    plain = _book("BDX", "Results")
    assert vds.refresh_download(None, 1, plain, "x.xlsx") is plain           # never had the sheet
    old = _old_output()
    assert vds.refresh_download(None, 1, old, "x.csv") is old                # not a workbook
    monkeypatch.setenv("PREVIOUS_SUBMISSION_SHEET", "0")
    assert vds.refresh_download(None, 1, old, "x.xlsx") is old               # switched off
    assert compared == []


def test_a_failure_serves_the_stored_file(monkeypatch):
    def broken(s, **_kw):
        raise RuntimeError("database gone")
    monkeypatch.setattr(vds, "reconcile", broken)
    old = _old_output()
    assert vds.refresh_download(None, 1, old, "x.xlsx") is old


def test_an_output_that_is_no_longer_comparable_keeps_its_sheet(monkeypatch):
    monkeypatch.setattr(vds, "reconcile", lambda s, **_kw: {"state": "no_accepted_version"})
    old = _old_output()
    assert vds.refresh_download(None, 1, old, "x.xlsx") is old


def test_the_sheet_is_stamped_with_its_layout_and_state():
    out = vsh.add_sheet(_book("BDX"), _result())
    assert vsh.stamp_of(out) == f"{vsh.LAYOUT}|compared" and vsh.is_current(out)
    assert vsh.stamp_of(_old_output()) is None and not vsh.is_current(_old_output())
    again = vsh.add_sheet(out, _result(), replace=True)
    assert load_workbook(io.BytesIO(again)).custom_doc_props.names.count("Kavachio previous submission") == 1


def test_a_current_compared_sheet_is_served_as_stored(compared):
    current = vsh.add_sheet(_book("BDX"), _result())
    assert vds.refresh_download(None, 7, current, "x.xlsx") is current
    assert compared == []


def test_a_sheet_that_was_not_compared_is_rebuilt(compared):
    res = _result()
    res["state"] = "not_compared"
    stale = vsh.add_sheet(_book("BDX"), res)
    assert vsh.stamp_of(stale) == f"{vsh.LAYOUT}|not_compared"
    out = vds.refresh_download(None, 7, stale, "x.xlsx")
    assert compared == [7] and vsh.is_current(out)
