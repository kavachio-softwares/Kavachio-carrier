"""The Programme Management Report (programme_report.py): every programme and
broker for the period, one pair in detail, and the file."""
import datetime as dt
from types import SimpleNamespace as NS

import pytest
from fastapi import HTTPException

import check_record as cr
import executive_delivery as ed
import executive_report as er
import programme_report as pr

TODAY = dt.date(2026, 11, 18)


def _prog(i, name, line=None):
    return NS(id=i, name=name, product_line=line, business_segment=None, annual_statement_lob=None)


def _owed(pid, bid, due, got=None, month="2026-10"):
    return NS(program_id=pid, broker_party_id=bid, period=month, due_date=due, received_at=got, closes_on=None)


@pytest.fixture
def book(monkeypatch):
    progs = [(_prog(1, "Motor Plus", "Motor"), "Active",
              [NS(broker_party_id=50, class_of_business=None, contract_type="insurer_broker"),
               NS(broker_party_id=51, class_of_business=None, contract_type=None),
               # A treaty's other side is a reinsurer, never a broker row.
               NS(broker_party_id=60, class_of_business=None, contract_type="insurer_reinsurer")]),
             (_prog(2, "Health Secure"), "Active", [NS(broker_party_id=52, class_of_business="Health", contract_type="insurer_broker")]),
             (_prog(3, "Marine"), "In Setup", [])]
    monkeypatch.setattr(er, "stage_of", lambda s, tid, at: progs)
    d = dt.date
    owed = [_owed(1, 50, d(2026, 10, 5), d(2026, 10, 4)),        # on time
            _owed(1, 50, d(2026, 10, 20)),                       # missing
            _owed(1, 51, d(2026, 10, 10), d(2026, 10, 12)),      # late
            _owed(2, 52, d(2026, 10, 15), d(2026, 10, 15))]      # on time
    monkeypatch.setattr(er, "_expected", lambda s, tid, a, b, pid, bid=None: owed)
    monkeypatch.setattr(cr, "summary", lambda s, tid, a, b, **k: {"by_broker_programme": [
        {"program_id": 1, "broker_party_id": 50, "files": 1, "errors": 6, "critical": 0, "warning": 6, "info": 0},
        {"program_id": 2, "broker_party_id": 52, "files": 1, "errors": 3, "critical": 2, "warning": 1, "info": 0},
        {"program_id": None, "broker_party_id": 53, "files": 1, "errors": 9, "critical": 9, "warning": 0, "info": 0}]})
    monkeypatch.setattr(pr, "_pair_premium", lambda s, tid, w: ("USD", {(1, 50): 900.0, (2, 52): 1200.0}))
    monkeypatch.setattr(er, "_names", lambda s, p, b: {"programme": {}, "broker": {50: "Acme", 51: "Global", 52: "Future"}})


def test_one_row_per_programme_and_broker(book):
    out = pr.management(None, 7, cadence="month", period="2026-10", today=TODAY)
    got = [(r["programme"], r["broker"], r["line_of_business"], r["expected"], r["received"], r["missing"],
            r["errors"], r["premium"], r["status"]) for r in out["rows"]]
    assert got == [
        ("Health Secure", "Future", "Health", 1, 1, 0, 3, 1200.0, "Attention"),   # critical errors
        ("Motor Plus", "Acme", "Motor", 2, 1, 1, 6, 900.0, "Attention"),          # a file missing
        ("Motor Plus", "Global", "Motor", 1, 1, 0, 0, None, "Attention"),         # a file late
    ]
    assert out["rows"][1]["reporting_period"] == "Oct 2026"
    # A programme with no broker yet is counted, not listed; files with no programme are left out.
    assert out["totals"] == {"programmes": 3, "writing": 2, "expected": 4, "received": 3, "missing": 1,
                             "errors": 9, "premium": 2100.0}
    assert (out["currency"], out["money_month"], out["from"], out["to"]) == ("USD", "2026-10", "2026-10-01", "2026-10-31")


def test_a_pair_with_nothing_wrong_is_ok(book, monkeypatch):
    monkeypatch.setattr(er, "_expected", lambda s, tid, a, b, pid, bid=None: [
        _owed(1, 50, dt.date(2026, 10, 5), dt.date(2026, 10, 4))])
    out = pr.management(None, 7, cadence="month", period="2026-10", today=TODAY)
    acme = next(r for r in out["rows"] if r["broker"] == "Acme")
    assert acme["status"] == "OK"


def test_files_not_due_yet_are_not_expected(book, monkeypatch):
    # Week of 16 Nov, as at the 18th: a file due on the 20th is not owed yet.
    monkeypatch.setattr(er, "_expected", lambda s, tid, a, b, pid, bid=None: [
        _owed(1, 50, dt.date(2026, 11, 20), month="2026-10")])
    out = pr.management(None, 7, cadence="week", period="2026-11-16", today=TODAY)
    acme = next(r for r in out["rows"] if r["broker"] == "Acme")
    assert (acme["expected"], acme["missing"], acme["reporting_period"]) == (0, 0, "Nov 2026")


def test_another_carriers_programme_is_not_found():
    s = NS(get=lambda model, i: NS(id=i, name="Theirs", tenant_id=8))
    with pytest.raises(HTTPException) as e:
        pr.detail(s, 7, 1, 50, today=TODAY)
    assert e.value.status_code == 404


def test_the_first_currency_is_the_financial_summarys():
    fig = lambda c: {"currency": {"value": c}}  # noqa: E731
    import financial_summary as fs
    rows = [(None, None, f) for f in (fig("GBP"), fig("USD"), fig("USD"))]
    keys = {"GBP": "GBP", "USD": "USD"}
    orig = fs._currency_key
    try:
        fs._currency_key = lambda f: keys[f["currency"]["value"]]
        assert pr._first_currency(rows) == "USD"
    finally:
        fs._currency_key = orig
    assert pr._first_currency([]) is None


def test_the_file_lists_every_row(book):
    out = pr.management(None, 7, cadence="month", period="2026-10", today=TODAY)
    rows = pr.table(out)
    assert rows[0] == pr.COLUMNS and len(rows) == 4
    assert rows[1][8] == "USD 1,200" and rows[3][8] == ""
    name, data, mime = pr.render(out, "excel", "Insurer")
    assert name == "Programme Management Report 2026-10.xlsx" and data[:2] == b"PK"
    name, data, mime = pr.render(out, "pdf", "Insurer")
    assert name.endswith(".pdf") and data[:4] == b"%PDF" and mime == "application/pdf"


def test_a_programmes_headline_has_no_problem_lines():
    fig = {"current": 100.0, "previous": None, "change": None}
    pack = {"cadence": "month", "money": {"currency": "USD", "gross_premium": fig, "commission": fig},
            "operations": {"files": 3, "clean_first_time_pct": 50.0,
                           "previous": {"files": 2, "clean_first_time_pct": 40.0}},
            "timeliness": {"on_time_pct": None, "previous_on_time_pct": None}}
    labels = [k for k, _v in ed.headline(pack)]
    assert labels == ["Gross premium", "Commission", "Files processed", "On time", "Clean first time"]


def _c(i, name, kind="insurer_broker"):
    return NS(id=i, name=name, filename=None, contract_type=kind, lifecycle="Bound", status="active",
              inception_dt=dt.date(2026, 1, 1), expiry_dt=dt.date(2026, 12, 31), umr=f"B{i}",
              class_of_business="Motor", year_of_account=2026)


def test_treaties_list_their_reinsurers(monkeypatch):
    import programme_rules as prr
    monkeypatch.setattr(prr, "treaties", lambda s, tid, pid: [_c(9, "QS 2026", "insurer_reinsurer"),
                                                              _c(10, "XoL 2026", "insurer_reinsurer")])
    monkeypatch.setattr(prr, "panel", lambda s, t: {70, 71} if t.id == 9 else set())
    monkeypatch.setattr(er, "_names", lambda s, p, b: {"programme": {}, "broker": {70: "Re One", 71: "Re Two"}})
    out = pr._treaties(None, 7, 1)
    assert [(t["name"], t["reinsurers"], t["umr"]) for t in out] == [
        ("QS 2026", ["Re One", "Re Two"], "B9"), ("XoL 2026", [], "B10")]


def test_a_contract_row_carries_its_cap_and_whether_it_is_current():
    caps = {3: {"currency": "USD", "cap": 1000, "written": 500, "used_pct": 50, "flag": None, "extra": 1}}
    row = pr._contract_row(_c(3, "Motor 2026"), caps, current=True)
    assert row["current"] and row["cap"] == {"currency": "USD", "cap": 1000, "written": 500, "used_pct": 50, "flag": None}
    assert pr._contract_row(_c(4, "Motor 2025"), caps)["cap"] is None
