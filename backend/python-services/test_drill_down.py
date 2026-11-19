"""Executive Reporting 38.1 — click a number, see the policies behind it
(drill_down.py, policy_search.search(drill=…)).

What is held: the policies listed are exactly the ones in the rows the number
was counted from — the same window, metric, currency, programme and broker on
the Financial Summary; the same files first checked in the month on the
Monthly Report — and a policy's figures on that list come from those rows only.

Pure: files are built in memory and fed in; no database, no `main`."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-placeholder")

import pytest
from fastapi import HTTPException

import ceded_engine as ce
import check_record as cr
import drill_down as dd
import exception_tally
import financial_summary as fs
import policy_search as ps
from auth_deps import Principal
from test_financial_summary import COLUMNS, export, full_row, structure, xlsx

CARRIER = Principal(user_id=1, tenant_id=7, role="carrier_admin")
BROKER = Principal(user_id=2, tenant_id=None, role="broker_admin", broker_party_id=50)


@pytest.fixture
def files(monkeypatch):
    """Both readers — the summary's and the search's — served the same files."""
    box = SimpleNamespace(exports=[], open={}, runs=[], errors=[])
    fs._REMEMBERED.clear()
    ps._REMEMBERED.clear()
    heads = [h for h, _f in COLUMNS]

    def _exports(s, seat, carrier_id=None, ids=None):
        return [o for o in box.exports
                if ((seat.kind == "carrier" and o.tenant_id == seat.tenant_id)
                    or (seat.kind == "broker" and o.broker_party_id == seat.broker_party_id))
                and (ids is None or o.id in ids)]

    def _seat_of(s, p):
        if p.role == "broker_admin":
            return ps.Seat("broker", broker_party_id=p.broker_party_id, program_ids={11, 12})
        return ps.Seat("carrier", tenant_id=p.tenant_id)

    monkeypatch.setattr(ps, "_exports", _exports)
    monkeypatch.setattr(ps, "seat_of", _seat_of)
    monkeypatch.setattr(ps, "_structures", lambda s, ids: {1: structure()})
    monkeypatch.setattr(ps, "_raw_rows", lambda s, o, st: ce.read_export_rows(
        st, xlsx(heads, o._rows), "f.xlsx")[0])
    monkeypatch.setattr(ps, "_names", lambda s, ex: {
        "programme": {11: "Fleet", 12: "Cargo"}, "broker": {50: "Wani", 51: "Other"},
        "insurer": {7: "Carrier Co"}})
    monkeypatch.setattr(exception_tally, "export_versions",
                        lambda s, ids: {o.id: {"reporting_period": o._period} for o in box.exports})
    monkeypatch.setattr(fs, "_latest_templates", lambda s, ids: {i: i for i in ids})
    monkeypatch.setattr(fs, "_bytes", lambda s, o: xlsx(heads, o._rows))
    monkeypatch.setattr(fs, "_open_counts", lambda s, ids: {i: box.open.get(i, 0) for i in ids})
    monkeypatch.setattr(cr, "runs_in", lambda s, tid, a, b, program_id=None, broker_party_id=None: [
        r for r in box.runs if a <= r.created_at < b
        and (program_id is None or r.program_id == program_id)])
    return box


def _search(p=CARRIER, s=None, **drill):
    d = dd.resolve(s, p, **drill)
    return ps.search(s, p, program_id=drill.get("program_id"),
                     broker_party_id=drill.get("broker_party_id"), drill=d)


def _policies(out) -> set:
    return {g["policy"] for g in out["items"]}


# ── nothing clicked ────────────────────────────────────────────────────────
def test_no_number_clicked_changes_nothing(files):
    files.exports = [export(1, "2027-03", [full_row("P1")])]
    assert dd.resolve(None, CARRIER, source=None) is None
    out = ps.search(None, CARRIER)
    assert out["drill"] is None and _policies(out) == {"P1"}


def test_an_unknown_report_is_refused(files):
    with pytest.raises(HTTPException) as e:
        dd.resolve(None, CARRIER, source="elsewhere")
    assert e.value.status_code == 400


# ── Financial Summary ──────────────────────────────────────────────────────
def test_a_window_lists_only_the_policies_it_added_up(files):
    files.exports = [export(1, "2027-03", [full_row("P1"), full_row("P2")]),
                     export(2, "2027-02", [full_row("P3")]),
                     export(3, "2026-03", [full_row("P4")])]
    assert _policies(_search(source="financial", period="2027-03")) == {"P1", "P2"}
    assert _policies(_search(source="financial", period="2027-03", window="previous")) == {"P3"}
    assert _policies(_search(source="financial", period="2027-03", window="last_year")) == {"P4"}


def test_a_figure_lists_only_the_rows_that_gave_it_a_value(files):
    """Commission behind the commission figure: P2 sent none and no rate to
    work it out from, so it is not behind that number — but it is behind gross."""
    files.exports = [export(1, "2027-03", [full_row("P1", comm=100),
                                           full_row("P2", pct=None, comm=None)])]
    assert _policies(_search(source="financial", period="2027-03",
                             metric="commission_amount")) == {"P1"}
    assert _policies(_search(source="financial", period="2027-03",
                             metric="gross_premium")) == {"P1", "P2"}


def test_a_currency_group_lists_only_its_own_currency(files):
    files.exports = [export(1, "2027-03", [full_row("P1", cur="USD"), full_row("P2", cur="GBP")])]
    assert _policies(_search(source="financial", period="2027-03", currency="GBP")) == {"P2"}


def test_a_programme_row_lists_only_that_programme(files):
    files.exports = [export(1, "2027-03", [full_row("P1")], program=11),
                     export(2, "2027-03", [full_row("P2")], program=12)]
    assert _policies(_search(source="financial", period="2027-03", program_id=12)) == {"P2"}


def test_a_policy_shows_the_premium_of_the_clicked_period_only(files):
    """P1 is in March and in February. Behind March's number it carries
    March's premium, not its total across both."""
    files.exports = [export(1, "2027-03", [full_row("P1", gross=1200)]),
                     export(2, "2027-02", [full_row("P1", gross=1000)])]
    out = _search(source="financial", period="2027-03")
    assert [(g["policy"], g["premium"], g["files"]) for g in out["items"]] == [("P1", 1200, 1)]
    assert ps.search(None, CARRIER)["items"][0]["premium"] == 2200


def test_the_list_says_what_it_is_behind(files):
    files.exports = [export(1, "2027-03", [full_row("P1"), full_row("P2")])]
    out = _search(source="financial", period="2027-03", label="Gross premium")
    assert out["drill"] == {"source": "financial", "period": "2027-03",
                            "period_name": "March 2027", "files": 1, "rows": 2,
                            "label": "Gross premium"}


def test_a_broker_sees_only_its_own_rows_behind_a_number(files):
    files.exports = [export(1, "2027-03", [full_row("P1")], broker=50),
                     export(2, "2027-03", [full_row("P2")], broker=51)]
    assert _policies(_search(BROKER, source="financial", period="2027-03")) == {"P1"}


# ── Monthly Report ─────────────────────────────────────────────────────────
def _run(rid, export_id, day="2027-03-05", status="clean", kind=cr.RUN, program=11):
    import datetime as dt
    return SimpleNamespace(id=rid, export_id=export_id, lane=cr.DIRECT, kind=kind,
                           status=status, human_touch=[], program_id=program,
                           created_at=dt.datetime.fromisoformat(day))


def test_files_handled_lists_the_policies_in_the_files_checked_that_month(files):
    files.exports = [export(1, "2027-02", [full_row("P1")]),
                     export(2, "2027-03", [full_row("P2")])]
    files.runs = [_run(10, 1, "2027-03-02"), _run(11, 1, "2027-03-09"),   # a re-run: one file
                  _run(12, 2, "2027-04-01")]                              # checked in April
    out = _search(source="monthly", month="2027-03", measure="files")
    assert _policies(out) == {"P1"} and out["drill"]["files"] == 1


def test_new_and_cancelled_list_their_own_transactions(files):
    files.exports = [export(1, "2027-03", [full_row("P1", txn="New"),
                                           full_row("P2", txn="Cancellation"),
                                           full_row("P3", txn="Renewal")])]
    files.runs = [_run(10, 1)]
    assert _policies(_search(source="monthly", month="2027-03", measure="new")) == {"P1"}
    assert _policies(_search(source="monthly", month="2027-03", measure="cancelled")) == {"P2"}
    assert _policies(_search(source="monthly", month="2027-03", measure="renewed")) == {"P3"}


def test_clean_first_time_lists_only_the_clean_files(files):
    files.exports = [export(1, "2027-03", [full_row("P1")]),
                     export(2, "2027-03", [full_row("P2")])]
    files.runs = [_run(10, 1, status="clean"), _run(11, 2, status="has_exceptions")]
    assert _policies(_search(source="monthly", month="2027-03", measure="clean")) == {"P1"}


class _Errors:
    """A session whose only query is check_error's (run, policy, severity)."""
    def __init__(self, rows):
        self.rows = rows

    def query(self, *cols):
        return self

    def filter(self, *a):
        return self

    def __iter__(self):
        return iter(self.rows)


def test_errors_list_the_policies_the_checks_named(files):
    files.exports = [export(1, "2027-03", [full_row("P1"), full_row("P2"), full_row("p 3")])]
    files.runs = [_run(10, 1, status="has_exceptions")]
    s = _Errors([(10, "P1", "critical"), (10, "P 3", "warning"), (10, None, "critical")])
    assert _policies(_search(s=s, source="monthly", month="2027-03",
                             measure="errors")) == {"P1", "p 3"}
    assert _policies(_search(s=s, source="monthly", month="2027-03",
                             measure="critical")) == {"P1"}


def test_the_monthly_report_is_the_carriers(files):
    with pytest.raises(HTTPException) as e:
        dd.resolve(None, BROKER, source="monthly", month="2027-03", measure="files")
    assert e.value.status_code == 403


@pytest.mark.parametrize("month,measure", [("March", "files"), ("2027-03", "on_time")])
def test_a_month_and_measure_must_be_real(files, month, measure):
    with pytest.raises(HTTPException) as e:
        dd.resolve(None, CARRIER, source="monthly", month=month, measure=measure)
    assert e.value.status_code == 400


def test_period_names_read_as_words():
    assert dd.period_name("2026-10") == "October 2026"
    assert dd.period_name("2026-Q3") == "Q3 2026"
    assert dd.period_name("2026") == "2026"


def test_a_week_or_dates_picked_list_the_files_checked_on_those_days(files):
    files.exports = [export(1, "2027-03", [full_row("P1")]),
                     export(2, "2027-03", [full_row("P2")])]
    files.runs = [_run(10, 1, "2027-03-02"), _run(11, 2, "2027-03-09")]
    out = _search(source="monthly", month="2027-03-08..2027-03-14", measure="files")
    assert _policies(out) == {"P2"} and out["drill"]["period_name"] == "8 – 14 Mar 2027"
    with pytest.raises(HTTPException) as e:
        dd.resolve(None, CARRIER, source="monthly", month="2027-03-14..2027-03-08", measure="files")
    assert e.value.status_code == 400


# ── the Executive Report: any dates, and one check's errors ─────────────────
def test_any_dates_list_the_files_first_checked_in_them(files):
    files.exports = [export(1, "2027-03", [full_row("P1")]),
                     export(2, "2027-03", [full_row("P2")])]
    files.runs = [_run(10, 1, "2027-03-02"), _run(11, 2, "2027-03-09")]
    out = _search(source="monthly", dates="2027-03-01..2027-03-07", measure="files")
    assert _policies(out) == {"P1"}
    assert (out["drill"]["period"], out["drill"]["period_name"]) == ("2027-03-01..2027-03-07", "1 – 7 Mar 2027")
    # Both days are included: a single day is "from it to itself".
    assert _policies(_search(source="monthly", dates="2027-03-09..2027-03-09", measure="files")) == {"P2"}


@pytest.mark.parametrize("dates", ["2027-03-01", "2027-03-07..2027-03-01", "March..April"])
def test_dates_must_be_real(files, dates):
    with pytest.raises(HTTPException) as e:
        dd.resolve(None, CARRIER, source="monthly", dates=dates, measure="files")
    assert e.value.status_code == 400


def test_one_checks_errors_narrow_to_that_check(files):
    files.exports = [export(1, "2027-03", [full_row("P1"), full_row("P2")])]
    files.runs = [_run(10, 1, status="has_exceptions")]

    class _Checked(_Errors):
        filters = 0

        def filter(self, *a):
            _Checked.filters += 1
            return self
    s = _Checked([(10, "P1", "critical")])
    _search(s=s, source="monthly", month="2027-03", measure="errors")
    plain = _Checked.filters
    _Checked.filters = 0
    assert _policies(_search(s=s, source="monthly", month="2027-03", measure="errors",
                             check="rule:7")) == {"P1"}
    assert _Checked.filters == plain + 1                        # the check's own filter


def test_dates_read_as_words():
    import datetime as _dt
    d = _dt.date
    assert dd.dates_name(d(2026, 11, 12), d(2026, 11, 12)) == "12 Nov 2026"
    assert dd.dates_name(d(2026, 11, 2), d(2026, 11, 8)) == "2 – 8 Nov 2026"
    assert dd.dates_name(d(2026, 10, 28), d(2026, 11, 3)) == "28 Oct – 3 Nov 2026"
    assert dd.dates_name(d(2025, 12, 29), d(2026, 1, 4)) == "29 Dec 2025 – 4 Jan 2026"
