"""Plan task 43.1 — Policy Search (policy_search.py): who may search, how a
policy's rows across files become one line, and the filters. Pure: the files
are fed in directly, no database."""
from __future__ import annotations

import datetime as dt
import io
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

import ceded_engine as ce
import exception_tally
import policy_search as ps
from auth_deps import Principal

CARRIER = Principal(user_id=1, tenant_id=7, role="carrier_admin")
BROKER = Principal(user_id=2, tenant_id=None, role="broker_admin", broker_party_id=50)
KAVACHIO = Principal(user_id=3, tenant_id=None, role="kavachio_admin")

STRUCTURE = {"sheets": [{"sheet_name": "BDX", "data_start_row": 1, "columns": [
    {"column_index": 0, "column_name": "Policy No", "canonical_field": "policy_number"},
    {"column_index": 1, "column_name": "Insured", "canonical_field": "policyholder_legal_name"},
    {"column_index": 2, "column_name": "State", "canonical_field": "risk_location_subdivision"},
    {"column_index": 3, "column_name": "Inception", "canonical_field": "policy_effective_date"},
    {"column_index": 4, "column_name": "Expiry", "canonical_field": "policy_expiration_date"},
    {"column_index": 5, "column_name": "Transaction", "canonical_field": "premium_transaction_type"},
    {"column_index": 6, "column_name": "Gross Premium",
     "canonical_field": "premium_transaction_gross_premium_amount"},
    {"column_index": 7, "column_name": "Currency",
     "canonical_field": "premium_transaction_original_currency"},
    {"column_index": 8, "column_name": "Notes"},
]}]}


def row(policy, insured="Acme Ltd", state="TX", start=dt.datetime(2026, 1, 1),
        end=dt.datetime(2027, 1, 1), txn="New", premium=100, currency="USD"):
    return [policy, insured, state, start, end, txn, premium, currency, "n/a"]


def export(eid, period, rows, *, tenant=7, program=11, broker=50):
    return SimpleNamespace(id=eid, tenant_id=tenant, program_id=program,
                           broker_party_id=broker, contract_id=900, template_id=1,
                           filename=f"file-{eid}.xlsx", blob_ref=None,
                           blob_md5=f"md5-{eid}", created_at=dt.datetime(2026, 11, 1),
                           _period=period, _rows=rows)


def rows_of(rows):
    """What ceded_engine.read_export_rows makes of a header + these rows."""
    return ce._rows_from_table([["h"] * 9] + rows, STRUCTURE["sheets"][0])


@pytest.fixture
def files(monkeypatch):
    """Serve `files.exports` to the search instead of the database."""
    box = SimpleNamespace(exports=[], asked=[], reads=0)
    ps._REMEMBERED.clear()

    def _exports(s, seat, carrier_id=None):
        box.asked.append((seat.kind, carrier_id))
        return [o for o in box.exports
                if (seat.kind == "carrier" and o.tenant_id == seat.tenant_id)
                or (seat.kind == "broker" and o.broker_party_id == seat.broker_party_id
                    and (carrier_id is None or o.tenant_id == carrier_id))]

    def _raw_rows(s, o, structure):
        box.reads += 1
        return rows_of(o._rows)

    monkeypatch.setattr(ps, "_exports", _exports)
    monkeypatch.setattr(ps, "_raw_rows", _raw_rows)
    monkeypatch.setattr(ps, "_structures", lambda s, ids: {1: STRUCTURE})
    monkeypatch.setattr(ps, "_names", lambda s, ex: {
        "programme": {11: "Fleet", 12: "Cargo"}, "broker": {50: "Wani", 51: "Other"},
        "insurer": {7: "Mahi", 8: "Northgate"}})
    monkeypatch.setattr(exception_tally, "export_versions", lambda s, ids: {
        o.id: {"reporting_period": o._period} for o in box.exports if o.id in set(ids)})
    real_seat = ps.seat_of

    def _seat(s, p):
        if p.is_broker:
            return ps.Seat("broker", broker_party_id=p.broker_party_id, program_ids={11, 12})
        return real_seat(s, p)
    monkeypatch.setattr(ps, "seat_of", _seat)
    return box


# ── who may search ──────────────────────────────────────────────────────────
def test_kavachio_is_refused():
    with pytest.raises(HTTPException) as e:
        ps.seat_of(None, KAVACHIO)
    assert e.value.status_code == 403


def test_carrier_seat_is_its_own_tenant():
    assert ps.seat_of(None, CARRIER) == ps.Seat("carrier", tenant_id=7)
    with pytest.raises(HTTPException):
        ps.seat_of(None, Principal(user_id=1, tenant_id=None, role="carrier_admin"))


def test_carrier_never_sees_another_carriers_policies(files):
    files.exports = [export(1, "2026-10", [row("P-1")]),
                     export(2, "2026-10", [row("P-2")], tenant=8)]
    r = ps.search(None, CARRIER)
    assert [i["policy"] for i in r["items"]] == ["P-1"]


def test_broker_sees_only_its_own_and_follows_the_carrier_switch(files):
    files.exports = [export(1, "2026-10", [row("P-1")]),
                     export(2, "2026-10", [row("P-2")], tenant=8),
                     export(3, "2026-10", [row("P-3")], broker=51)]
    r = ps.search(None, BROKER)
    assert sorted(i["policy"] for i in r["items"]) == ["P-1", "P-2"]
    assert [c["name"] for c in r["filters"]["insurers"]] == ["Mahi", "Northgate"]
    assert r["filters"]["brokers"] == []
    # Another broker's id in the query changes nothing for a broker.
    assert ps.search(None, BROKER, broker_party_id=51)["total"] == 2
    assert [i["policy"] for i in ps.search(None, BROKER, carrier_id=8)["items"]] == ["P-2"]
    # A carrier seat ignores carrier_id: it is always its own company.
    ps.search(None, CARRIER, carrier_id=8)
    assert files.asked[-1] == ("carrier", None)


# ── one line per policy ─────────────────────────────────────────────────────
def test_rows_across_files_become_one_policy_with_the_latest_values(files):
    files.exports = [
        export(1, "2026-09", [row("P-1", insured="Acme", txn="New", premium=100)]),
        export(2, "2026-10", [row("p-1 ", insured=None, txn="Endorsement", premium=25),
                              row("P-9")]),
    ]
    r = ps.search(None, CARRIER)
    assert r["total"] == 2 and r["files"] == 2
    p1 = next(i for i in r["items"] if i["policy"].upper().strip() == "P-1")
    assert p1["rows"] == 2 and p1["files"] == 2
    assert p1["transaction"] == "Endorsement"            # the latest row speaks
    assert p1["insured"] == "Acme"                       # an earlier row fills a blank
    assert p1["premium"] == 125.0 and p1["currency"] == "USD"
    assert (p1["first_period"], p1["last_period"]) == ("2026-09", "2026-10")
    assert (p1["start"], p1["end"]) == ("2026-01-01", "2027-01-01")


def test_premium_in_two_currencies_is_not_added_up(files):
    files.exports = [export(1, "2026-09", [row("P-1", currency="USD"),
                                           row("P-1", currency="GBP")])]
    p = ps.search(None, CARRIER)["items"][0]
    assert p["premium"] is None and p["currency"] is None


def test_same_number_under_another_broker_or_programme_stays_separate(files):
    files.exports = [export(1, "2026-10", [row("P-1")]),
                     export(2, "2026-10", [row("P-1")], broker=51),
                     export(3, "2026-10", [row("P-1")], program=12)]
    assert ps.search(None, CARRIER)["total"] == 3


def test_rows_with_no_policy_number_are_not_policies(files):
    files.exports = [export(1, "2026-10", [row("P-1"), row(None, insured="Total")])]
    assert ps.search(None, CARRIER)["total"] == 1


def test_a_date_in_a_state_column_is_not_a_state():
    r = {"policy_number": "P", "risk_location_subdivision": dt.datetime(2026, 11, 2, 9),
         "policyholder_subdivision": "CA"}
    assert ps._line(r, 1)["state"] == "CA"


# ── filters and paging ──────────────────────────────────────────────────────
@pytest.fixture
def book(files):
    files.exports = [
        export(1, "2026-08", [row("A-1", insured="Cedar Ridge", state="TX",
                                  start=dt.datetime(2026, 8, 1), end=dt.datetime(2027, 8, 1))]),
        export(2, "2026-09", [row("A-2", insured="Falcon Crest", state="IL",
                                  start=dt.datetime(2026, 9, 3), end=dt.datetime(2027, 9, 3))],
               program=12),
        export(3, "2026-10", [row("A-3", insured="Harbourview", state="tx",
                                  start=dt.datetime(2026, 10, 6), end=dt.datetime(2027, 10, 6))],
               broker=51),
    ]
    return files


def _policies(**kw):
    return [i["policy"] for i in ps.search(None, CARRIER, **kw)["items"]]


def test_newest_bordereau_month_first(book):
    assert _policies() == ["A-3", "A-2", "A-1"]


def test_search_by_policy_number_or_insured(book):
    assert _policies(q="a-2") == ["A-2"]
    assert _policies(q="  harbour ") == ["A-3"]


def test_filter_by_programme_broker_and_state(book):
    assert _policies(program_id=12) == ["A-2"]
    assert _policies(broker_party_id=51) == ["A-3"]
    assert _policies(state="TX") == ["A-3", "A-1"]           # case does not matter


def test_filter_by_dates(book):
    assert _policies(date_field="start", date_from="2026-09-01") == ["A-3", "A-2"]
    assert _policies(date_field="end", date_to="2027-08-31") == ["A-1"]
    assert _policies(date_field="month", date_from="2026-09-15", date_to="2026-09-20") == ["A-2"]
    with pytest.raises(HTTPException):
        ps.search(None, CARRIER, date_from="31/12/2026")
    with pytest.raises(HTTPException):
        ps.search(None, CARRIER, date_field="issued")


def test_choices_come_from_the_whole_set_and_total_counts_every_match(book):
    r = ps.search(None, CARRIER, program_id=12, page_size=1)
    assert [c["name"] for c in r["filters"]["programmes"]] == ["Cargo", "Fleet"]
    assert r["filters"]["states"] == ["IL", "TX"]            # "tx" and "TX" are one state
    page2 = ps.search(None, CARRIER, page=2, page_size=2)
    assert page2["total"] == 3 and [i["policy"] for i in page2["items"]] == ["A-1"]


# ── remembering a file ──────────────────────────────────────────────────────
def test_a_file_is_read_once_until_it_changes(files):
    files.exports = [export(1, "2026-10", [row("P-1")])]
    ps.search(None, CARRIER)
    ps.search(None, CARRIER, q="p")
    assert files.reads == 1
    files.exports[0].blob_md5 = "md5-rerendered"
    ps.search(None, CARRIER)
    assert files.reads == 2


# ── one policy's rows ───────────────────────────────────────────────────────
def test_detail_lists_every_row_with_its_columns(files, monkeypatch):
    class _Q:
        def filter(self, *a): return self
        def __iter__(self): return iter([SimpleNamespace(id=900, name="Fleet 2026", filename=None)])
    monkeypatch.setattr(ps, "_names", lambda s, ex: {
        "programme": {11: "Fleet"}, "broker": {50: "Wani"}, "insurer": {7: "Mahi"}})
    files.exports = [export(1, "2026-09", [row("P-1", premium=100)]),
                     export(2, "2026-10", [row("P-1", txn="Endorsement", premium=25), row("P-2")])]
    s = SimpleNamespace(query=lambda *a: _Q())
    d = ps.detail(s, CARRIER, insurer_id=7, program_id=11, broker_party_id=50, policy="p-1")
    assert [(r["period"], r["transaction"]) for r in d["rows"]] == [
        ("2026-10", "Endorsement"), ("2026-09", "New")]
    assert d["rows"][0]["contract"] == "Fleet 2026"
    assert {"name": "Notes", "value": "n/a"} in d["rows"][0]["columns"]
    with pytest.raises(HTTPException) as e:
        ps.detail(s, CARRIER, insurer_id=8, program_id=11, broker_party_id=50, policy="P-1")
    assert e.value.status_code == 404


def test_reads_a_real_workbook_through_the_template():
    wb = Workbook()
    ws = wb.active
    ws.title = "BDX"
    ws.append(["Policy No", "Insured", "State", "Inception", "Expiry", "Transaction",
               "Gross Premium", "Currency", "Notes"])
    ws.append(row("P-1"))
    ws.append(row(None, insured="Total", premium=100))
    buf = io.BytesIO()
    wb.save(buf)
    rows, notes = ce.read_export_rows(STRUCTURE, buf.getvalue(), "out.xlsx")
    lines = [ln for i, r in enumerate(rows, 1) if (ln := ps._line(r, i))]
    assert notes == [] and len(lines) == 1
    assert lines[0]["policy"] == "P-1" and lines[0]["start"] == "2026-01-01"
    assert lines[0]["premium"] == 100.0 and lines[0]["state"] == "TX"
