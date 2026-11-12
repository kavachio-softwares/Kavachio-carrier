"""ceded_engine.read_export_rows finds each column by its HEADING in the file
(ceded_engine.at_file_positions), not by the template's column_index.

The writer leaves empty columns out and older files predate later template
edits, so the template position can point at another column's cells — 14 of
611 current files on 5 Nov 2026 were read wrongly that way (templates 123, 202,
209, 210, 226, 335, 336). These tests rebuild each of those templates' real
file layouts (fixtures/ceded_reader_templates.json: the layout and the header
row of one real file — headings only, no data) and check every tagged column
reads its own cells; and that a file laid out exactly as its template reads
exactly as before. `_rows_from_table`, the reinsurance module's own row logic,
is not changed and is used as the reference. Pure: no database."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from openpyxl import Workbook

import ceded_engine as ce

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ceded_reader_templates.json").read_text())
AFFECTED = FIXTURE["templates"]


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, table in sheets.items():
        ws = wb.create_sheet(name)
        for row in table:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _value(heading, n: int) -> str:
    """A cell value that says which column it came from."""
    return f"{' '.join(str(heading).split())}#{n}"


def _expected(sh: dict, headers: list) -> dict:
    """What each field must read: the value under its column's OWN heading
    (the first tagged column, in template order, whose heading the file has)."""
    norm = lambda v: " ".join(str(v).split()).lower()
    seen: dict[str, int] = {}
    position: dict[tuple, int] = {}
    for i, h in enumerate(headers):
        if h is not None and str(h).strip():
            k = norm(h)
            position[(k, seen.get(k, 0))] = i
            seen[k] = seen.get(k, 0) + 1
    used: dict[str, int] = {}
    where = {}
    for idx, c in sorted(enumerate(sh["columns"]), key=lambda t: int(t[1].get("column_index") or 0)):
        names = [norm(x) for x in dict.fromkeys([c.get("display_name") or c.get("column_name"), c.get("column_name")]) if x]
        hit = next((n for n in names if (n, used.get(n, 0)) in position), None)
        if hit is not None:
            where[idx] = position[(hit, used.get(hit, 0))]
            used[hit] = used.get(hit, 0) + 1
    out = {}
    for idx, c in enumerate(sh["columns"]):
        f = c.get("canonical_field")
        if f and idx in where and f not in out:
            out[f] = _value(headers[where[idx]], where[idx])
    return out


@pytest.mark.parametrize("tpl", AFFECTED, ids=[f"template-{t['template_id']}" for t in AFFECTED])
def test_every_tagged_column_reads_its_own_cells_in_the_real_layout(tpl):
    structure = tpl["structure"]
    tables = {}
    for sh in structure["sheets"]:
        headers = tpl["file_headers"].get(sh["sheet_name"])
        if headers is None:
            continue
        start = int(sh.get("data_start_row") or 1)
        table = [[None] * len(headers) for _ in range(start)]
        table[int(sh.get("header_row") or 0)] = list(headers)
        table.append([_value(h, i) if h is not None and str(h).strip() else None for i, h in enumerate(headers)])
        tables[sh["sheet_name"]] = table
    rows, notes = ce.read_export_rows(structure, _xlsx(tables), "f.xlsx")
    assert rows, notes
    sh = next(s for s in structure["sheets"] if s["sheet_name"] in tables)
    want = _expected(sh, tpl["file_headers"][sh["sheet_name"]])
    got = {f: rows[0].get(f) for f in want}
    assert got == want


@pytest.mark.parametrize("tpl", AFFECTED, ids=[f"template-{t['template_id']}" for t in AFFECTED])
def test_the_old_position_read_was_wrong_for_these_layouts(tpl):
    """Guard on the fixture itself: read by position, each real layout put at
    least one field on another column's cells — so the test above can fail."""
    structure = tpl["structure"]
    sh = next(s for s in structure["sheets"] if s["sheet_name"] in tpl["file_headers"])
    headers = tpl["file_headers"][sh["sheet_name"]]
    start = int(sh.get("data_start_row") or 1)
    table = [[None] * len(headers) for _ in range(start)]
    table[int(sh.get("header_row") or 0)] = list(headers)
    table.append([_value(h, i) if h is not None and str(h).strip() else None for i, h in enumerate(headers)])
    old = ce._rows_from_table(table, sh)[0]
    want = _expected(sh, headers)
    assert any(old.get(f) != v for f, v in want.items())


@pytest.mark.parametrize("tpl", AFFECTED, ids=[f"template-{t['template_id']}" for t in AFFECTED])
def test_a_file_laid_out_as_its_template_reads_exactly_as_before(tpl):
    """No columns dropped, none moved: the new read equals the reinsurance
    module's own row logic on the template as it stands."""
    sh = tpl["structure"]["sheets"][0]
    width = max(int(c.get("column_index") or 0) for c in sh["columns"]) + 1
    headers = [None] * width
    for c in sh["columns"]:
        headers[int(c.get("column_index") or 0)] = c.get("display_name") or c.get("column_name")
    start = int(sh.get("data_start_row") or 1)
    table = [[None] * width for _ in range(start)]
    table[int(sh.get("header_row") or 0)] = headers
    table.append([_value(h, i) if h else None for i, h in enumerate(headers)])
    rows, _notes = ce.read_export_rows({"sheets": [sh]}, _xlsx({sh["sheet_name"]: table}), "f.xlsx")
    assert rows == ce._rows_from_table(table, sh)


# ── the rules of the lookup ────────────────────────────────────────────────
SHEET = {"sheet_name": "BDX", "data_start_row": 1, "columns": [
    {"column_index": 0, "column_name": "Policy No", "canonical_field": "policy_number"},
    {"column_index": 1, "column_name": "Notes"},
    {"column_index": 2, "column_name": "Gross Premium",
     "canonical_field": "premium_transaction_gross_premium_amount"},
    {"column_index": 3, "column_name": "Currency",
     "canonical_field": "premium_transaction_original_currency"},
]}


def test_a_dropped_empty_column_no_longer_shifts_the_rest():
    data = _xlsx({"BDX": [["Policy No", "Gross Premium", "Currency"], ["P1", 100, "USD"]]})
    rows, _ = ce.read_export_rows({"sheets": [SHEET]}, data, "f.xlsx")
    assert rows[0]["premium_transaction_gross_premium_amount"] == 100
    assert rows[0]["premium_transaction_original_currency"] == "USD"


def test_a_renamed_column_is_found_by_the_name_the_file_shows():
    sh = {**SHEET, "columns": [dict(c) for c in SHEET["columns"]]}
    sh["columns"][2]["display_name"] = "GWP (our share)"
    data = _xlsx({"BDX": [["Policy No", "Notes", "GWP (our share)", "Currency"], ["P1", "x", 100, "USD"]]})
    rows, _ = ce.read_export_rows({"sheets": [sh]}, data, "f.xlsx")
    assert rows[0]["premium_transaction_gross_premium_amount"] == 100
    assert rows[0]["_col"]["Gross Premium"] == 100       # still keyed by the column's own name


def test_a_repeated_heading_is_shared_out_in_order():
    sh = {"sheet_name": "BDX", "data_start_row": 1, "columns": [
        {"column_index": 0, "column_name": "Policy No", "canonical_field": "policy_number"},
        {"column_index": 1, "column_name": "Premium", "canonical_field": "premium_transaction_gross_premium_amount"},
        {"column_index": 2, "column_name": "Premium", "canonical_field": "premium_transaction_net_premium_amount"},
    ]}
    data = _xlsx({"BDX": [["Policy No", "Premium", "Premium"], ["P1", 100, 90]]})
    rows, _ = ce.read_export_rows({"sheets": [sh]}, data, "f.xlsx")
    assert rows[0]["premium_transaction_gross_premium_amount"] == 100
    assert rows[0]["premium_transaction_net_premium_amount"] == 90


def test_a_file_whose_headings_match_nothing_is_read_by_position_as_before():
    table = [["a", "b", "c", "d"], ["P1", "x", 100, "USD"]]
    rows, _ = ce.read_export_rows({"sheets": [SHEET]}, _xlsx({"BDX": table}), "f.xlsx")
    assert rows == ce._rows_from_table(table, SHEET)


def test_a_csv_is_read_by_heading_too():
    data = b"Policy No,Gross Premium,Currency\nP1,100,USD\n"
    rows, _ = ce.read_export_rows({"sheets": [SHEET]}, data, "f.csv")
    assert rows[0]["premium_transaction_gross_premium_amount"] == "100"
    assert rows[0]["premium_transaction_original_currency"] == "USD"


def test_the_column_order_that_decides_a_shared_field_is_kept():
    """Two columns carry one field: the template's first one still wins."""
    sh = {"sheet_name": "BDX", "data_start_row": 1, "columns": [
        {"column_index": 0, "column_name": "Policy No", "canonical_field": "policy_number"},
        {"column_index": 2, "column_name": "Inception Prem", "canonical_field": "premium_transaction_gross_premium_amount"},
        {"column_index": 1, "column_name": "Gross Premium", "canonical_field": "premium_transaction_gross_premium_amount"},
    ]}
    data = _xlsx({"BDX": [["Policy No", "Gross Premium", "Inception Prem"], ["P1", 100, 250]]})
    rows, _ = ce.read_export_rows({"sheets": [sh]}, data, "f.xlsx")
    assert rows[0]["premium_transaction_gross_premium_amount"] == \
        ce._rows_from_table([["Policy No", "Gross Premium", "Inception Prem"], ["P1", 100, 250]], sh)[0][
            "premium_transaction_gross_premium_amount"]
