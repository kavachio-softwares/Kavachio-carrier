"""Task 36 — Partner Performance (partner_performance.py).

What is held: the periods; that on time and clean first time are exactly the
Executive Report scorecard's numbers; the one new rule (clean AND on time) —
late and missing files fail it, a file with no check recorded is left out and
counted, a later correction or re-run never repaints the first file; one row
per broker + programme + contract; problems found, put right and still open;
file type as stored; the filters (one broker keeps its rank among all), and
a picked value staying offered; the
ranking (one file is enough) and that best and worst never share a broker; the
Excel file; and that only the carrier seat may read it.

Its own SQLite database in memory: no shared database, no `main`."""
from __future__ import annotations

import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-placeholder")

import pytest
from fastapi import HTTPException

import partner_performance as pp

TODAY = dt.date(2026, 11, 12)
D = dt.date
A, B, C = 50, 51, 52            # brokers
FLEET, CARGO = 1, 2             # programmes


# ── the period ──────────────────────────────────────────────────────────────
def test_a_month_is_the_executive_reports_month():
    import executive_report as er
    w, x = pp.window("month", "2026-10", TODAY), er.window("month", "2026-10", TODAY)
    assert (w["start"], w["end"], w["as_at"], w["previous"]) == (x["start"], x["end"], x["as_at"], x["previous"])
    assert pp.window("month", None, TODAY)["period"] == "2026-10"


def test_a_quarter_runs_three_months_against_the_one_before():
    w = pp.window("quarter", "2026-Q1", TODAY)
    assert (w["start"], w["end"], w["complete"]) == (D(2026, 1, 1), D(2026, 4, 1), True)
    assert w["previous"] == {"period": "2025-Q4", "start": D(2025, 10, 1), "end": D(2026, 1, 1)}
    assert w["as_at"] == D(2026, 3, 31)


def test_the_running_quarter_and_year_count_to_today():
    q = pp.window("quarter", None, TODAY)
    assert (q["period"], q["start"], q["complete"], q["as_at"]) == ("2026-Q4", D(2026, 10, 1), False, TODAY)
    y = pp.window("year", None, TODAY)
    assert (y["period"], y["start"], y["end"], y["as_at"]) == ("2026", D(2026, 1, 1), D(2027, 1, 1), TODAY)
    assert y["previous"]["period"] == "2025"


@pytest.mark.parametrize("cadence,period", [("week", None), ("quarter", "2026-Q5"), ("quarter", "2026-10"),
                                            ("year", "26"), ("year", "2027"), ("month", "2026-13x")])
def test_a_wrong_or_future_period_is_refused(cadence, period):
    with pytest.raises(HTTPException) as e:
        pp.window(cadence, period, TODAY)
    assert e.value.status_code == 400


# ── a small book ────────────────────────────────────────────────────────────
#  October 2026, as at 31 Oct:
#   Alpha (A), Fleet, contract 900 "Property":
#     a1 due 10 Oct, came 9 Oct  by email, first run clean      → clean on time
#     a2 due 12 Oct, came 12 Oct, first run has errors; a later
#        corrected version (export 105) is clean                → not clean (first file counts)
#     a3 due 14 Oct, came 20 Oct (late), clean                  → fails: late
#     a4 due 16 Oct, never came                                 → fails: missing
#     a5 due 18 Oct, came on time, no check recorded            → NOT CHECKED (counts as not passed)
#     a6 due 25 Nov — not due yet in October's window at all
#   Beta (B), Cargo, contract 901 (no class of business), portal:
#     b1..b3 on time and clean; b3's first run clean but a
#        RE-RUN later had errors                                → still clean
#     b4 due 20 Oct, came on time, released from a hold         → not clean
#   Gamma (C), Fleet: c1, c2 on time and clean — too few to rank
#  September: Alpha a0 on time and clean (one file).
def _book():
    from sqlalchemy import create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import sessionmaker

    @compiles(JSONB, "sqlite")
    def _as_json(type_, compiler, **kw):  # noqa: ARG001
        return "JSON"
    import db
    from intake_models import FileArrival, IntakeRoute
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(eng, tables=[
        db.Program.__table__, db.Party.__table__, db.ExpectedSubmission.__table__,
        db.SubmissionSchedule.__table__, db.Contract.__table__, db.OutputExport.__table__,
        db.CheckRun.__table__, db.CheckResult.__table__, FileArrival.__table__, IntakeRoute.__table__])
    s = sessionmaker(bind=eng)()
    s.add_all([db.Program(id=FLEET, tenant_id=7, name="Fleet", is_app_managed=True),
               db.Program(id=CARGO, tenant_id=7, name="Cargo", is_app_managed=True),
               db.Party(id=A, party_type="broker", legal_name="Alpha Brokers"),
               db.Party(id=B, party_type="broker", legal_name="Beta Brokers"),
               db.Party(id=C, party_type="broker", legal_name="Gamma Brokers"),
               db.Contract(id=900, tenant_id=7, program_id=FLEET, broker_party_id=A,
                           class_of_business=" Property "),
               db.Contract(id=901, tenant_id=7, program_id=CARGO, broker_party_id=B,
                           class_of_business="  "),
               db.SubmissionSchedule(id=1, tenant_id=7, program_id=FLEET, contract_id=900),
               db.SubmissionSchedule(id=2, tenant_id=7, program_id=CARGO, contract_id=901)])
    s.add(IntakeRoute(id=1, tenant_id=7, broker_party_id=B, channel="upload", address="beta"))
    # (key, broker, programme, schedule, due, came, export, contract)
    rows = [("a0", A, FLEET, 1, D(2026, 9, 10), D(2026, 9, 9), 100, 900),
            ("a1", A, FLEET, 1, D(2026, 10, 10), D(2026, 10, 9), 101, 900),
            ("a2", A, FLEET, 1, D(2026, 10, 12), D(2026, 10, 12), 102, 900),
            ("a3", A, FLEET, 1, D(2026, 10, 14), D(2026, 10, 20), 103, 900),
            ("a4", A, FLEET, 1, D(2026, 10, 16), None, None, None),
            ("a5", A, FLEET, 1, D(2026, 10, 18), D(2026, 10, 18), 104, 900),
            ("a6", A, FLEET, 1, D(2026, 11, 25), None, None, None),
            ("b1", B, CARGO, 2, D(2026, 10, 5), D(2026, 10, 1), 201, 901),
            ("b2", B, CARGO, 2, D(2026, 10, 6), D(2026, 10, 2), 202, 901),
            ("b3", B, CARGO, 2, D(2026, 10, 7), D(2026, 10, 3), 203, 901),
            ("b4", B, CARGO, 2, D(2026, 10, 20), D(2026, 10, 20), 204, 901),
            ("c1", C, FLEET, 1, D(2026, 10, 8), D(2026, 10, 8), 301, 900),
            ("c2", C, FLEET, 1, D(2026, 10, 9), D(2026, 10, 9), 302, 900)]
    for i, (k, broker, prog, sched, due, came, export, contract) in enumerate(rows, 1):
        s.add(db.ExpectedSubmission(id=i, tenant_id=7, program_id=prog, broker_party_id=broker,
                                    schedule_id=sched, period=k, period_start=due, period_end=due,
                                    due_date=due, closes_on=due + dt.timedelta(days=30),
                                    received_at=came, received_export_id=export))
        if export:
            s.add(db.OutputExport(id=export, tenant_id=7, filename=f"{export}.xlsx", program_id=prog, broker_party_id=broker,
                                  contract_id=contract))
    s.add(db.OutputExport(id=105, tenant_id=7, filename="105.xlsx", program_id=FLEET, broker_party_id=A, contract_id=900))
    run = 0

    def check(export, status, on, kind="run", touch=None, broker=None, prog=None, contract=None):
        nonlocal run
        run += 1
        o = s.get(db.OutputExport, export)
        s.add(db.CheckRun(id=run, tenant_id=7, lane="direct", kind=kind, status=status, export_id=export,
                          program_id=prog or o.program_id, broker_party_id=broker or o.broker_party_id,
                          contract_id=contract, human_touch=touch or [], created_at=on))
    s.flush()
    t = lambda d, h=9: dt.datetime(d.year, d.month, d.day, h)  # noqa: E731
    check(100, "clean", t(D(2026, 9, 9)))
    check(101, "clean", t(D(2026, 10, 9)))
    check(102, "has_exceptions", t(D(2026, 10, 12)))
    check(105, "clean", t(D(2026, 10, 22)), touch=["corrected_version"])
    check(103, "clean", t(D(2026, 10, 20)))
    check(201, "clean", t(D(2026, 10, 1)))
    check(202, "clean", t(D(2026, 10, 2)))
    check(203, "clean", t(D(2026, 10, 3)))
    check(203, "has_exceptions", t(D(2026, 10, 4)), kind="rerun")
    check(204, "clean", t(D(2026, 10, 20)), touch=["released_from_hold"])
    check(301, "clean", t(D(2026, 10, 8)))
    check(302, "clean", t(D(2026, 10, 9)))
    s.add(FileArrival(id=1, tenant_id=7, filename="a1.xlsx", outcome="accepted", channel="email",
                      run_export_id=101, received_at=t(D(2026, 10, 9))))
    for n, e in enumerate((201, 202, 203, 204), 2):
        s.add(FileArrival(id=n, tenant_id=7, filename=f"{e}.xlsx", outcome="accepted", route_id=1,
                          run_export_id=e, received_at=t(D(2026, 10, 1))))
    s.commit()
    return s


# Problems (exception_tally is main's; here, a stand-in by export id):
#   a2's first file (102) had 10 exceptions; its live version (105) has 1 open.
#   b4's file (204) had 2, both still open.
TALLY = {102: {"exceptions": 10, "open": 10}, 105: {"exceptions": 1, "open": 1},
         204: {"exceptions": 2, "open": 2}}


@pytest.fixture
def book(monkeypatch):
    monkeypatch.setattr(pp, "_tally_export",
                        lambda r: TALLY.get(r.id, {"exceptions": 0, "open": 0}))
    monkeypatch.setattr(pp, "_live_versions", lambda s, tid: {(A, FLEET, 900, "a2"): 105})
    return _book()


def _page(s, **k):
    k.setdefault("cadence", "month")
    k.setdefault("period", "2026-10")
    return pp.page(s, 7, today=TODAY, **k)


def _row(out, name):
    return next(r for r in out["rows"] if r["broker"] == name)


# ── the one new rule ────────────────────────────────────────────────────────
def test_clean_and_on_time_file_by_file(book):
    exp, _ = pp.files_of(book, 7, D(2026, 10, 1), D(2026, 11, 1), D(2026, 10, 31))
    by = {e["id"]: e for e in exp}
    # a1 clean on time; a2 on time but its FIRST file had errors (the clean
    # correction does not repaint it); a3 late; a4 missing; a5 not checked.
    assert [by[i]["clean_on_time"] for i in (2, 3, 4, 5, 6)] == [True, False, False, False, None]
    assert 7 not in by                                   # a6 not due in October
    # b3: its re-run had errors, but the first run is the file as it came.
    assert by[10]["clean_on_time"] is True
    # b4: released from a hold — not straight through.
    assert by[11]["clean_on_time"] is False


def test_alpha_every_share_is_out_of_its_five_files_due(book):
    a = _row(_page(book), "Alpha Brokers")
    assert (a["due"], a["received"], a["on_time"], a["late_or_missing"]) == (5, 4, 3, 2)
    # late or missing, split: one came after its due date, one never came
    assert (a["late"], a["missing"]) == (1, 1)
    assert a["on_time"] + a["late"] + a["missing"] == a["due"]
    assert (a["scored"], a["clean_on_time"], a["not_checked"]) == (4, 1, 1)
    # ONE BASE: on time, late and on-time-and-passed are out of the 5 due;
    # passed first time is out of the 4 that came (a1 and a3 passed).
    assert (a["on_time_pct"], a["late_pct"], a["clean_on_time_pct"]) == (60.0, 20.0, 20.0)
    assert (a["clean_first_time"], a["checked"], a["not_checked_came"]) == (2, 3, 1)
    assert a["clean_first_time_pct"] == 50.0


def test_not_checked_files_are_named_and_count_as_not_passed(book):
    out = _page(book)
    s = out["summary"]
    # 11 October files due across the book; a5 came on time with no check.
    assert (s["due"], s["not_checked"], s["scored"]) == (11, 1, 10)
    assert s["clean_on_time"] == 6 and s["clean_on_time_pct"] == 54.5


def test_exceptions_on_the_file_as_it_came_mean_it_did_not_pass(book, monkeypatch):
    # a5 (export 104) has no check run, but 3 exceptions were found on it.
    monkeypatch.setattr(pp, "_tally_export", lambda r: {**TALLY, 104: {"exceptions": 3, "open": 3}}
                        .get(r.id, {"exceptions": 0, "open": 0}))
    a = _row(_page(book), "Alpha Brokers")
    assert (a["not_checked"], a["scored"], a["clean_on_time"]) == (0, 5, 1)
    assert (a["checked"], a["not_checked_came"], a["clean_first_time"]) == (4, 0, 2)


def test_a_period_with_nothing_due_has_no_share_not_zero(book):
    out = _page(book, cadence="quarter", period="2026-Q1")
    assert out["summary"]["clean_on_time_pct"] is None and out["summary"]["on_time_pct"] is None
    assert out["rows"] == [] and out["best"] == [] and out["worst"] == []


# ── nothing re-counted ──────────────────────────────────────────────────────
def test_on_time_is_the_executive_reports_own(book):
    import check_record as cr
    import executive_report as er
    w = er.window("month", "2026-10", TODAY)
    due = er._due_tally(book, 7, w["start"], w["end"], w["as_at"])["broker"]
    stp = {r["id"]: r["straight_through_pct"]
           for r in cr.summary(book, 7, er._at(w["start"]), er._at(w["end"]))["by_broker"]}
    out = _page(book)
    for r in out["rows"]:                      # one contract each in this book
        d = due[r["broker_id"]]
        assert (r["due"], r["late_or_missing"]) == (d["due"], d["late_or_missing"])
        assert r["on_time_pct"] == round(100.0 * d["on_time"] / d["due"], 1)
    # Passed first time is no longer the check-run share (ONE BASE): it is out
    # of the files due that came, re-runs and corrections never counted.
    assert stp  # the check-run figure still exists for the Executive Report


def test_the_change_is_against_the_month_before(book):
    a = _row(_page(book), "Alpha Brokers")
    assert a["prev_clean_on_time_pct"] == 100.0 and a["change_pts"] == -80.0
    assert _row(_page(book), "Beta Brokers")["change_pts"] is None     # nothing in September


# ── one row per broker + programme + contract ───────────────────────────────
def test_a_row_is_one_broker_programme_and_contract(book):
    import db
    # Alpha also owes a Cargo file, which never came. Its contract is Alpha's
    # own Cargo contract covering the month — not Beta's (901), and not a draft
    # that never went in force.
    book.add_all([db.Contract(id=902, tenant_id=7, program_id=CARGO, broker_party_id=A,
                              name="Cargo 2026", lifecycle="superseded",
                              inception_dt=D(2026, 1, 1), expiry_dt=D(2026, 12, 31)),
                  db.Contract(id=903, tenant_id=7, program_id=CARGO, broker_party_id=A,
                              name="Cargo draft", lifecycle="draft",
                              inception_dt=D(2026, 10, 1), expiry_dt=D(2027, 9, 30)),
                  db.Contract(id=904, tenant_id=7, program_id=CARGO, broker_party_id=A,
                              name="Cargo 2025", lifecycle="expired",
                              inception_dt=D(2025, 1, 1), expiry_dt=D(2025, 12, 31)),
                  db.ExpectedSubmission(id=99, tenant_id=7, program_id=CARGO, broker_party_id=A,
                                        schedule_id=2, period="a9", period_start=D(2026, 10, 11),
                                        period_end=D(2026, 10, 11), due_date=D(2026, 10, 11),
                                        closes_on=D(2026, 11, 11))])
    book.commit()
    out = _page(book)
    alpha = [r for r in out["rows"] if r["broker"] == "Alpha Brokers"]
    assert [(r["programme"], r["contract"], r["due"]) for r in alpha] == [
        ("Cargo", "Cargo 2026", 1), ("Fleet", "Contract #900", 5)]
    assert len({r["rank"] for r in alpha}) == 1            # the broker's rank on each
    # the rows always add up to the totals
    for k in ("due", "received", "on_time", "late_or_missing", "late", "missing", "scored",
              "clean_on_time"):
        assert sum(r[k] for r in out["rows"]) == out["summary"][k], k


def test_a_file_that_came_after_the_period_keeps_its_own_contract(book):
    import db
    # a4 was due 16 Oct and came on 5 Nov: missing as October closed, but the
    # file names its contract.
    book.add_all([db.Contract(id=905, tenant_id=7, program_id=FLEET, broker_party_id=A, name="Fleet late"),
                  db.OutputExport(id=106, tenant_id=7, filename="106.xlsx", program_id=FLEET,
                                  broker_party_id=A, contract_id=905)])
    book.query(db.ExpectedSubmission).filter_by(period="a4").update(
        {"received_at": D(2026, 11, 5), "received_export_id": 106})
    book.commit()
    row = next(r for r in _page(book)["rows"] if r["contract"] == "Fleet late")
    assert (row["due"], row["received"], row["missing"]) == (1, 0, 1)


def test_a_broker_with_no_contract_on_the_programme_says_so(book):
    import db
    # Gamma owes a Cargo file and has no Cargo contract (901 is Beta's).
    book.add(db.ExpectedSubmission(id=98, tenant_id=7, program_id=CARGO, broker_party_id=C,
                                   schedule_id=2, period="c9", period_start=D(2026, 10, 11),
                                   period_end=D(2026, 10, 11), due_date=D(2026, 10, 11),
                                   closes_on=D(2026, 11, 11)))
    book.commit()
    gamma = [r for r in _page(book)["rows"] if r["broker"] == "Gamma Brokers"]
    assert ("Cargo", "No contract") in [(r["programme"], r["contract"]) for r in gamma]


def test_no_change_is_not_claimed_when_nothing_came_either_time(book):
    import db
    # Gamma's Cargo file never came in September nor in October: 0% against
    # 0%, but there is no change to speak of.
    for i, (k, due) in enumerate((("c7", D(2026, 9, 11)), ("c8", D(2026, 10, 11))), start=96):
        book.add(db.ExpectedSubmission(id=i, tenant_id=7, program_id=CARGO, broker_party_id=C,
                                       schedule_id=2, period=k, period_start=due, period_end=due,
                                       due_date=due, closes_on=due + dt.timedelta(days=30)))
    book.commit()
    row = next(r for r in _page(book)["rows"] if r["broker"] == "Gamma Brokers" and r["programme"] == "Cargo")
    assert (row["received"], row["prev_received"], row["change_pts"]) == (0, 0, None)
    # a row where a file did come still shows its change
    assert _row(_page(book), "Alpha Brokers")["change_pts"] == -80.0


def test_rows_follow_the_broker_ranking(book):
    assert [r["broker"] for r in _page(book)["rows"]] == ["Gamma Brokers", "Beta Brokers", "Alpha Brokers"]


# ── problems found, put right, still open ───────────────────────────────────
def test_problems_put_right_count_from_the_first_file_to_the_live_one(book):
    a = _row(_page(book), "Alpha Brokers")
    # a2: 10 found on the file as it came, 1 still open on its live version.
    assert (a["problems_found"], a["problems_fixed"], a["problems_open"]) == (10, 9, 1)
    assert (a["files_with_problems"], a["fixed_pct"]) == (1, 90.0)
    b = _row(_page(book), "Beta Brokers")
    assert (b["problems_found"], b["problems_fixed"], b["files_with_problems"], b["fixed_pct"]) == (2, 0, 1, 0.0)
    g = _row(_page(book), "Gamma Brokers")
    assert (g["problems_found"], g["fixed_pct"]) == (0, None)    # nothing to put right
    assert _page(book)["summary"]["problems_fixed"] == 9


def test_problems_are_only_counted_on_files_that_came(book):
    exp, _ = pp.files_of(book, 7, D(2026, 10, 1), D(2026, 11, 1), D(2026, 10, 31), problems=True)
    a4 = next(e for e in exp if e["id"] == 5)
    assert (a4["found"], a4["open"], a4["fixed"]) == (None, None, None)


# ── file type, as stored ────────────────────────────────────────────────────
def test_file_type_is_the_way_in_unknown_is_not_recorded(book):
    out = _page(book)
    assert _row(out, "Alpha Brokers")["file_types"] == ["Email", "Not recorded"]
    assert _row(out, "Beta Brokers")["file_types"] == ["Portal"]
    assert [o["label"] for o in out["options"]["file_types"]] == [
        "Portal", "Email", "SFTP", "API", "Not recorded", "Not received"]
    assert "deal_types" not in out["options"]


# ── the filters ─────────────────────────────────────────────────────────────
def test_filters_narrow_every_number(book):
    out = _page(book, file_type="email")
    assert [r["broker"] for r in out["rows"]] == ["Alpha Brokers"]
    assert (out["summary"]["due"], out["summary"]["clean_on_time_pct"]) == (1, 100.0)
    assert {r["broker"] for r in _page(book, programme=FLEET)["rows"]} == {"Alpha Brokers", "Gamma Brokers"}
    assert _page(book, programme=CARGO, file_type="email")["summary"]["due"] == 0


def test_a_picked_value_stays_offered_in_a_period_without_it(book):
    # September has no Cargo files and nothing by Secure Link.
    out = _page(book, period="2026-09", programme=CARGO, file_type="secure_link")
    assert {"value": CARGO, "label": "Cargo"} in out["options"]["programmes"]
    assert {"value": "secure_link", "label": "Secure Link"} in out["options"]["file_types"]
    assert out["filters"] == {"programme": CARGO, "file_type": "secure_link", "broker": None}


# ── one broker picked ───────────────────────────────────────────────────────
def test_one_broker_keeps_its_rank_among_all_brokers(book):
    out = _page(book, broker=A)
    assert {r["broker"] for r in out["rows"]} == {"Alpha Brokers"}
    assert [r["rank"] for r in out["rows"]] == [3]                   # 3rd of all three
    assert out["picked"] == {"broker_id": A, "name": "Alpha Brokers", "rank": 3, "of": 3,
                             "clean_on_time_pct": 20.0, "clean_now_pct": 25.0}
    assert out["best"] == [] and out["worst"] == []                  # the line replaces them
    assert (out["summary"]["due"], out["summary"]["clean_on_time_pct"]) == (5, 20.0)
    assert out["filters"]["broker"] == A and out["ranked_brokers"] == 3


def test_no_broker_picked_has_best_and_worst_and_no_picked(book):
    out = _page(book)
    assert out["picked"] is None and out["best"]


def test_the_broker_list_offers_every_broker_and_keeps_the_picked_one(book):
    out = _page(book, broker=A)
    assert [o["label"] for o in out["options"]["brokers"]] == ["Alpha Brokers", "Beta Brokers", "Gamma Brokers"]
    sept = _page(book, period="2026-09", broker=B)                   # Beta sent nothing in September
    assert {"value": B, "label": "Beta Brokers"} in sept["options"]["brokers"]
    assert sept["picked"]["rank"] is None and sept["rows"] == []
    assert sept["picked"]["name"] == "Beta Brokers"


def test_a_broker_with_the_programme_filter_is_ranked_within_that_programme(book):
    out = _page(book, broker=A, programme=FLEET)                     # Fleet: Gamma and Alpha
    assert (out["picked"]["rank"], out["picked"]["of"]) == (2, 2)


# ── the ranking ─────────────────────────────────────────────────────────────
def test_one_file_with_a_result_is_enough_to_be_ranked(book):
    out = _page(book)
    ranks = {r["broker"]: r["rank"] for r in out["rows"]}
    # Gamma has two files, both clean on time: ranked, and first.
    assert ranks == {"Gamma Brokers": 1, "Beta Brokers": 2, "Alpha Brokers": 3}
    assert out["ranked_brokers"] == 3 and out["min_files"] == 1


def test_best_and_worst_never_name_the_same_broker(book):
    out = _page(book)
    assert [r["name"] for r in out["best"]] == ["Gamma Brokers", "Beta Brokers"]
    assert [r["name"] for r in out["worst"]] == ["Alpha Brokers"]


def _r(name, pct, on=50.0, scored=5):
    return {"name": name, "clean_now_pct": pct, "on_time_pct": on, "scored_now": scored,
            "due": scored, "received": scored, "files": scored}


def test_ties_go_to_on_time_then_most_files():
    ranked = pp.rank([_r("x", 50, 60), _r("y", 50, 80), _r("z", 50, 80, 9), _r("one", 100, 100, 1),
                      _r("none", None, 100, 0)])
    assert [(r["name"], r["rank"]) for r in ranked] == [
        ("one", 1), ("z", 2), ("y", 3), ("x", 4), ("none", None)]


@pytest.mark.parametrize("n,best,worst", [(1, 1, 0), (3, 2, 1), (7, 4, 3), (10, 5, 5), (14, 5, 5)])
def test_best_and_worst_take_up_to_five_each(n, best, worst):
    ranked = pp.rank([_r(f"b{i:02d}", 90 - i) for i in range(n)])
    b, w = pp.best_and_worst(ranked)
    assert (len(b), len(w)) == (best, worst)
    assert not {r["name"] for r in b} & {r["name"] for r in w}
    if w:
        assert w[0]["name"] == f"b{n - 1:02d}"            # the very worst first


def test_nobody_at_zero_is_best_and_nobody_at_100_is_worst():
    b, w = pp.best_and_worst(pp.rank([_r("z1", 0), _r("z2", 0), _r("z3", 0)]))
    assert b == [] and [r["name"] for r in w] == ["z3", "z2", "z1"]
    b, w = pp.best_and_worst(pp.rank([_r("p1", 100), _r("p2", 100), _r("h", 50)]))
    assert [r["name"] for r in b] == ["p1", "p2"] and [r["name"] for r in w] == ["h"]


# ── the Excel file ──────────────────────────────────────────────────────────
def test_the_excel_file_is_the_table(book):
    import io
    from openpyxl import load_workbook
    out = _page(book)
    ws = load_workbook(io.BytesIO(pp.workbook(out))).active
    head = [c.value for c in ws[1]]
    assert head[:6] == ["Rank", "Broker", "Programme", "Contract", "Files due", "Files received"]
    assert "Exceptions resolved" in head and not any(h.startswith("Change since") for h in head)
    assert ws.max_row == 1 + len(out["rows"])
    alpha = next(r for r in ws.iter_rows(min_row=2, values_only=True) if r[1] == "Alpha Brokers")
    assert alpha[0] == 3 and alpha[head.index("Resolved %")] == 0.9
    assert "Late or missing" not in head
    assert (alpha[head.index("Late")], alpha[head.index("Missing")]) == (1, 1)


# ── carrier only ────────────────────────────────────────────────────────────
def test_only_the_carrier_seat_may_read_it(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import auth_deps
    import partner_performance_routes as routes
    monkeypatch.setattr(routes, "resolve_tenant_id", lambda s, p, mga=None: 7)
    seen = {}
    monkeypatch.setattr(routes.pp, "page", lambda s, tid, **k: seen.update(k) or {
        "ok": tid, "period": "2026-10", "rows": [], "cadence": "month"})
    monkeypatch.setattr(routes.pp, "workbook", lambda d: b"xlsx")
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    for role, code in (("broker_admin", 403), ("operator", 403), ("carrier_admin", 200)):
        app.dependency_overrides[auth_deps.current_principal] = (
            lambda role=role: auth_deps.Principal(user_id=1, tenant_id=7, role=role))
        assert client.get("/reports/partner-performance").status_code == code, role
        f = client.get("/reports/partner-performance/file", params={"broker_id": 50})
        assert f.status_code == code, role
        if code == 200:
            assert f.headers["content-disposition"] == 'attachment; filename="Broker Performance 2026-10.xlsx"'
            assert seen["broker"] == 50


# ── clean now: resolving every exception counts (20 Nov 2026) ───────────────
@pytest.mark.parametrize("on_time, clean, found, open_, expect", [
    (True, True, 0, 0, True),        # passed its first check
    (True, False, 10, 0, True),      # every exception resolved since
    (True, None, 24, 0, True),       # no check run recorded, but all resolved
    (True, False, 10, 1, False),     # one still unresolved
    (True, None, 24, 24, False),
    (False, True, 0, 0, False),      # late (or missing) fails, however clean
    (True, False, 0, 0, False),      # failed first check for another reason
    (True, None, None, None, None),  # nothing known: not checked
])
def test_clean_now_file_by_file(on_time, clean, found, open_, expect):
    e = {"on_time": on_time, "clean": clean, "found": found, "open": open_}
    assert pp.clean_now(e) is expect


def test_a_file_cleaned_after_it_came_lifts_the_rank_score(book, monkeypatch):
    # a2 came on time with 10 exceptions; its live version now has none open.
    monkeypatch.setattr(pp, "_tally_export", lambda r: {**TALLY, 105: {"exceptions": 1, "open": 0}}
                        .get(r.id, {"exceptions": 0, "open": 0}))
    a = next(r for r in _page(book)["rows"] if r["broker"] == "Alpha Brokers")
    assert (a["clean_on_time"], a["scored"]) == (1, 4)       # first time: unchanged
    assert (a["clean_now"], a["scored_now"], a["clean_now_pct"]) == (2, 4, 50.0)
