"""Executive Reporting 38.2 — the Executive Report's pack (executive_report.py).

What is held: the dates a weekly and a monthly report cover and compare with,
which open problems are escalations, and that every section is the count its
own screen makes (each source is replaced here by a stand-in that records how
it was asked).

Pure: no database, no `main`."""
from __future__ import annotations

import datetime as dt
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-placeholder")

import pytest
from fastapi import HTTPException

import executive_report as er

TODAY = dt.date(2026, 11, 12)          # a Thursday


# ── the window ──────────────────────────────────────────────────────────────
def test_a_monthly_report_defaults_to_the_last_complete_month():
    w = er.window("month", None, TODAY)
    assert (w["period"], w["start"], w["end"]) == ("2026-10", dt.date(2026, 10, 1), dt.date(2026, 11, 1))
    assert w["previous"]["period"] == "2026-09" and w["complete"] is True
    assert w["money_month"] == "2026-10"


def test_a_monthly_report_compares_across_the_year_end():
    w = er.window("month", "2027-01", TODAY)
    assert w["previous"] == {"period": "2026-12", "start": dt.date(2026, 12, 1),
                             "end": dt.date(2027, 1, 1)}


def test_the_month_still_running_is_not_complete():
    assert er.window("month", "2026-11", TODAY)["complete"] is False


def test_a_weekly_report_runs_monday_to_sunday():
    w = er.window("week", "2026-11-05", TODAY)          # a Thursday
    assert (w["start"], w["end"]) == (dt.date(2026, 11, 2), dt.date(2026, 11, 9))
    assert w["previous"]["start"] == dt.date(2026, 10, 26)


def test_a_weekly_report_defaults_to_the_last_complete_week():
    w = er.window("week", None, TODAY)
    assert (w["start"], w["end"], w["complete"]) == (dt.date(2026, 11, 2), dt.date(2026, 11, 9), True)


def test_a_week_reads_premium_for_the_month_it_ends_in():
    w = er.window("week", "2026-10-29", TODAY)          # 26 Oct – 1 Nov
    assert w["money_month"] == "2026-11"


def test_a_daily_report_is_today_against_yesterday():
    w = er.window("day", None, TODAY)
    assert (w["period"], w["start"], w["end"]) == ("2026-11-12", TODAY, dt.date(2026, 11, 13))
    assert w["previous"]["period"] == "2026-11-11" and w["complete"] is False
    # The daily schedule still sends a whole past day.
    assert er.window("day", "2026-11-11", TODAY)["complete"] is True


def test_custom_dates_are_compared_with_as_many_days_just_before():
    w = er.window("custom", "2026-10-01..2026-10-15", TODAY)
    assert (w["start"], w["end"], w["as_at"]) == (dt.date(2026, 10, 1), dt.date(2026, 10, 16), dt.date(2026, 10, 15))
    assert w["previous"] == {"period": "2026-09-16..2026-09-30", "start": dt.date(2026, 9, 16),
                             "end": dt.date(2026, 10, 1)}
    assert w["money_month"] == "2026-10" and w["complete"] is True
    assert er.window("custom", "2026-11-01..2026-11-12", TODAY)["complete"] is False


def test_custom_dates_can_be_as_long_as_the_user_wants():
    w = er.window("custom", "2023-01-01..2026-11-11", TODAY)     # almost four years
    assert w["previous"]["start"] == dt.date(2019, 2, 20) and w["as_at"] == dt.date(2026, 11, 11)


@pytest.mark.parametrize("period", [None, "2026-10-01", "2026-10-15..2026-10-01",
                                    "2026-11-01..2026-11-13"])
def test_custom_dates_must_make_sense(period):
    with pytest.raises(HTTPException) as e:
        er.window("custom", period, TODAY)
    assert e.value.status_code == 400


def test_the_sections_are_worked_out_at_the_periods_last_day():
    assert er.window("month", "2026-09", TODAY)["as_at"] == dt.date(2026, 9, 30)
    assert er.window("week", "2026-11-04", TODAY)["as_at"] == dt.date(2026, 11, 8)
    assert er.window("month", "2026-11", TODAY)["as_at"] == TODAY             # still running


@pytest.mark.parametrize("cadence,period", [("fortnight", None), ("month", "2026-10-01"),
                                            ("week", "2026-10"), ("day", "2026-11")])
def test_a_wrong_window_is_refused(cadence, period):
    with pytest.raises(HTTPException) as e:
        er.window(cadence, period, TODAY)
    assert e.value.status_code == 400


def test_a_change_is_points_for_a_rate_and_percent_for_a_count():
    assert er._change(55.0, 50.0, points=True) == 5.0
    assert er._change(120, 100) == 20.0
    assert er._change(5, 0) is None and er._change(None, 3) is None


# ── the pack ────────────────────────────────────────────────────────────────
@pytest.fixture
def sources(monkeypatch):
    """Every section's source, recording what it was asked for."""
    asked = SimpleNamespace(ops=[], timeliness=[], money=[], trend=None)

    def ops(s, tid, a, b):
        asked.ops.append((a, b))
        return {"files": 10 if a == dt.date(2026, 10, 1) else 8, "rows": 100,
                "errors": 4 if a == dt.date(2026, 10, 1) else 5, "critical": 1,
                "clean_first_time": 6, "clean_first_time_pct": 60.0 if a == dt.date(2026, 10, 1) else 50.0}

    def tl(s, tid, month, today):
        asked.timeliness.append(month)
        return {"month": month, "due": 5, "due_so_far": 5, "on_time": 4,
                "on_time_pct": 80.0 if month == "2026-10" else 75.0,
                "late": 1, "overdue": 0, "never": 0}

    def money(s, tid, month):
        asked.money.append(month)
        return {"month": month}

    def trend(s, tid, a, b):
        asked.trend = (a, b)
        return []
    monkeypatch.setattr(er, "_operations", ops)
    monkeypatch.setattr(er, "_timeliness", tl)
    monkeypatch.setattr(er, "_money", money)
    monkeypatch.setattr(er, "_trend", trend)
    asked.at = {}
    monkeypatch.setattr(er, "_problems", lambda s, tid, today, as_at=None: asked.at.update(problems=as_at) or {"open": 0})
    monkeypatch.setattr(er, "_portfolio", lambda s, tid, as_at: asked.at.update(portfolio=as_at) or {"total": 0})
    monkeypatch.setattr(er, "_reinsurance", lambda s, tid, today, w=None: asked.at.update(reinsurance=w and w["as_at"]) or {"due": 0})
    asked.more = {}
    monkeypatch.setattr(er, "_quality", lambda s, tid, a, b: asked.more.update(quality=(a, b)) or {})
    monkeypatch.setattr(er, "_late", lambda s, tid, a, b, at: asked.more.update(late=(a, b, at)) or {})
    monkeypatch.setattr(er, "_contracts", lambda s, tid, a, b, at, daily: asked.more.update(contracts=(a, b, at, daily)) or {})
    monkeypatch.setattr(er, "_scorecard", lambda s, tid, w, prem: {})
    return asked


def test_a_monthly_pack_compares_the_month_with_the_one_before(sources):
    p = er.pack(None, 7, cadence="month", period="2026-10", today=TODAY)
    assert sources.ops == [(dt.date(2026, 10, 1), dt.date(2026, 11, 1)),
                           (dt.date(2026, 9, 1), dt.date(2026, 10, 1))]
    assert p["operations"]["change"] == {"files": 25.0, "errors": -20.0,
                                         "clean_first_time_pct": 10.0}
    assert sources.timeliness == ["2026-10", "2026-09"]
    assert p["timeliness"]["change"] == 5.0
    assert (p["from"], p["to"]) == ("2026-10-01", "2026-10-31")
    assert sources.money == ["2026-10"]
    assert sources.trend == (dt.date(2026, 10, 1), dt.date(2026, 11, 1))
    end = dt.date(2026, 10, 31)
    assert sources.at == {"problems": end, "portfolio": end, "reinsurance": end}
    # The new sections follow the month too.
    a, b = dt.date(2026, 10, 1), dt.date(2026, 11, 1)
    assert sources.more == {"quality": (a, b), "late": (a, b, end), "contracts": (a, b, end, False)}


def test_a_custom_pack_is_worked_out_at_its_to_date(sources):
    er.pack(None, 7, cadence="custom", period="2026-10-01..2026-10-15", today=TODAY)
    end = dt.date(2026, 10, 15)
    assert sources.at == {"problems": end, "portfolio": end, "reinsurance": end}


def test_a_weekly_pack_counts_the_week_and_the_month_so_far(sources):
    p = er.pack(None, 7, cadence="week", period="2026-11-04", today=TODAY)
    assert sources.ops[0] == (dt.date(2026, 11, 2), dt.date(2026, 11, 9))
    assert sources.ops[1] == (dt.date(2026, 10, 26), dt.date(2026, 11, 2))
    assert sources.money == ["2026-11"] and sources.timeliness[0] == "2026-11"
    assert (p["from"], p["to"]) == ("2026-11-02", "2026-11-08")


# ── escalations ─────────────────────────────────────────────────────────────
def _problem(kind, status="", overdue=False, age=1, owner="carrier"):
    return {"type": kind, "type_label": kind, "status": status, "title": f"{kind} {status}",
            "detail": None, "owner": owner, "started_on": TODAY - dt.timedelta(days=age),
            "due_on": TODAY + dt.timedelta(days=-1 if overdue else 3),
            "program_id": 1, "broker_party_id": None, "link": None, "key": f"{kind}{age}"}


def test_escalations_are_delinquent_files_disputes_and_anything_overdue(monkeypatch):
    import fix_performance as fp
    import open_problems as op
    items = [_problem(op.LATE_FILE, "Delinquent", age=9),
             _problem(op.LATE_FILE, "Overdue", age=2),             # not yet escalated
             _problem(op.DISPUTE, "In dispute", age=4),
             _problem(op.CHECK, "Open", overdue=True, age=12),
             _problem(op.CHECK, "Open", age=1)]
    monkeypatch.setattr(op, "collect", lambda s, tid, target: items)
    monkeypatch.setattr(fp, "target_days", lambda s, tid: 5)
    out = er._problems(None, 7, TODAY)          # no as_at: now
    assert out["open"] == 5 and out["escalations"] == 3
    assert [r["title"] for r in out["escalation_list"]] == [
        "check Open", "late_file Delinquent", "dispute In dispute"]          # oldest first
    assert out["escalation_list"][0]["owner"] == "Carrier"


# ── programme stages (program_stage.py, shared with Program Management) ──────
@pytest.mark.parametrize("prog_status,contract,stage", [
    ("draft", None, "Onboarding"),
    ("draft", {"status": "drafted", "expiry": None}, "In Setup"),
    ("active", None, "Onboarding"),
    ("active", {"status": "extracting", "expiry": 400}, "In Setup"),
    ("active", {"status": "active", "expiry": None}, "Active"),
    ("active", {"status": "active", "expiry": -1}, "Runoff"),
    ("active", {"status": "active", "expiry": 30}, "Under Review"),
    ("active", {"status": "active", "expiry": 400}, "Active"),
    ("draft", {"status": "active", "expiry": -10}, "Runoff"),      # live by its contract
])
def test_a_programme_stage_follows_its_contract(prog_status, contract, stage):
    import program_stage as ar
    c = None if contract is None else SimpleNamespace(
        status=contract["status"],
        expiry_dt=None if contract["expiry"] is None else TODAY + dt.timedelta(days=contract["expiry"]))
    assert ar.program_stage(SimpleNamespace(status=prog_status), c, TODAY) == stage


# ── Needs attention as at a past day (executive_report._open_at) ──────────────
# One timeline, read back at the end of different days. Fix target: 5 days.
#
#   L1  late file, due 30 Sep: Overdue from 1 Oct, Delinquent from 11 Oct,
#       received 20 Oct (written by the daily check a day late: 12 Oct).
#   D1  late file, due 5 Oct: Overdue 6 Oct, disputed 8 Oct (reminder 12 Oct),
#       dispute accepted 25 Oct.
#   E1  late file, due 10 Oct: Overdue 11 Oct, more time until 31 Oct on 12 Oct,
#       the extra time ran out — Overdue again from 1 Nov.
#   N1  no broker, due 15 Oct, never received, no case.
#   H1  held file: arrived 3 Oct, released 9 Oct.
#   C1  failed check found 2 Oct, fixed 7 Oct.  C2 found 20 Oct, not fixed.
def _timeline():
    from sqlalchemy import create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")       # file_arrival has a Postgres JSONB column
    def _as_json(type_, compiler, **kw):  # noqa: ARG001
        return "JSON"
    from sqlalchemy.orm import sessionmaker
    import db
    from intake_models import FileArrival
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(eng, tables=[db.Program.__table__, db.Party.__table__, db.ExpectedSubmission.__table__,
                                             db.SubmissionCaseEvent.__table__, FileArrival.__table__])
    s = sessionmaker(bind=eng)()
    d = dt.date
    s.add(db.Program(id=1, tenant_id=7, name="Fleet", is_app_managed=True))
    rows = {"L1": (d(2026, 9, 30), 50, d(2026, 10, 20)), "D1": (d(2026, 10, 5), 50, None),
            "E1": (d(2026, 10, 10), 50, None), "N1": (d(2026, 10, 15), None, None)}
    ids = {}
    for i, (k, (due, broker, got)) in enumerate(rows.items(), 1):
        s.add(db.ExpectedSubmission(id=i, tenant_id=7, program_id=1, broker_party_id=broker,
                                    period=k, period_start=due, period_end=due, due_date=due,
                                    received_at=got, dispute_raised_on=d(2026, 10, 8) if k == "D1" else None))
        ids[k] = i
    ev = [("L1", None, "overdue", d(2026, 10, 1), d(2026, 10, 1), None),
          ("L1", "overdue", "delinquent", d(2026, 10, 11), d(2026, 10, 12), None),
          ("L1", "delinquent", "resolved", d(2026, 10, 20), d(2026, 10, 20), None),
          ("D1", None, "overdue", d(2026, 10, 6), d(2026, 10, 6), None),
          ("D1", "overdue", "in_dispute", d(2026, 10, 8), d(2026, 10, 8), None),
          ("D1", "in_dispute", "in_dispute", d(2026, 10, 12), d(2026, 10, 12), None),
          ("D1", "in_dispute", "resolved", d(2026, 10, 25), d(2026, 10, 25), None),
          ("E1", None, "overdue", d(2026, 10, 11), d(2026, 10, 11), None),
          ("E1", "overdue", "extended", d(2026, 10, 12), d(2026, 10, 12), {"until": "2026-10-31"}),
          ("E1", "extended", "overdue", d(2026, 11, 1), d(2026, 11, 2), None)]
    for n, (k, frm, to, on, written, det) in enumerate(ev, 1):
        s.add(db.SubmissionCaseEvent(id=n, tenant_id=7, expected_id=ids[k], program_id=1, kind="x",
                                     from_state=frm, to_state=to, effective_on=on, details=det,
                                     created_at=dt.datetime(written.year, written.month, written.day, 6)))
    s.add(FileArrival(id=1, tenant_id=7, filename="held.xlsx", outcome="held",
                      received_at=dt.datetime(2026, 10, 3, 9), resolved_at=dt.datetime(2026, 10, 9, 9),
                      resolution="released"))
    s.commit()
    return s


@pytest.fixture
def timeline(monkeypatch):
    import fix_performance as fp
    monkeypatch.setattr(fp, "target_days", lambda s, tid: 5)
    monkeypatch.setattr(fp, "problems", lambda s, tid, **k: [
        {"found_at": dt.datetime(2026, 10, 2, 8), "fixed_at": dt.datetime(2026, 10, 7, 8),
         "check": "C1", "broker_party_id": 50, "program_id": 1, "export_id": 1},
        {"found_at": dt.datetime(2026, 10, 20, 8), "fixed_at": None,
         "check": "C2", "broker_party_id": 50, "program_id": 1, "export_id": 2}])
    return _timeline()


def _attention(s, as_at):
    out = er._problems(s, 7, dt.date(2026, 12, 1), as_at)
    return ({p["title"] for p in out["escalation_list"]}, out["open"])


def test_needs_attention_at_the_end_of_5_oct(timeline):
    # L1 is overdue; H1 held (due 8 Oct) and C1 (due 7 Oct) open but not yet late.
    assert _attention(timeline, dt.date(2026, 10, 5)) == ({"Bordereau for L1 not received"}, 3)


def test_needs_attention_at_the_end_of_11_oct_reads_the_day_a_move_counts_from(timeline):
    # Delinquent counts from 11 Oct although the daily check wrote it on the 12th.
    # E1 was due 10 Oct and nothing came, so it is late too; C1 was fixed and
    # H1 released before the 11th.
    esc, n = _attention(timeline, dt.date(2026, 10, 11))
    assert esc == {"Bordereau for L1 not received", "Late mark disputed for D1",
                   "Bordereau for E1 not received"}
    assert n == 3
    out = er._problems(timeline, 7, dt.date(2026, 12, 1), dt.date(2026, 10, 11))
    l1 = next(p for p in out["escalation_list"] if p["title"].endswith("L1 not received"))
    assert l1["status"] == "Delinquent"


def test_needs_attention_at_the_end_of_15_oct(timeline):
    # L1 delinquent; D1 disputed since 8 Oct; E1 has more time until 31 Oct (not
    # an escalation); N1, with no broker, became overdue — none on the 15th yet.
    esc, n = _attention(timeline, dt.date(2026, 10, 15))
    assert esc == {"Bordereau for L1 not received", "Late mark disputed for D1"}
    assert n == 3                                            # L1, D1, E1


def test_needs_attention_at_the_end_of_october(timeline):
    # L1 received, D1 accepted; E1's extra time runs to the 31st (not late yet);
    # N1 overdue since 15 Oct; C2 found 20 Oct, past its 25 Oct target.
    esc, n = _attention(timeline, dt.date(2026, 10, 31))
    assert esc == {"Bordereau for N1 not received", "C2"}
    assert n == 3                                            # E1, N1, C2


def test_needs_attention_after_the_extra_time_ran_out(timeline):
    esc, n = _attention(timeline, dt.date(2026, 11, 5))
    assert esc == {"Bordereau for E1 not received", "Bordereau for N1 not received", "C2"}
    assert n == 3


def test_each_filter_reads_needs_attention_at_its_own_last_day(timeline, monkeypatch):
    """Daily, Weekly, Monthly and Custom each ask for the end of their own last day."""
    seen = []
    real = er._problems
    monkeypatch.setattr(er, "_problems", lambda s, tid, today, as_at=None: seen.append(as_at) or real(s, tid, today, as_at))
    for name, stub in (("_operations", {"files": 0, "rows": 0, "errors": 0, "critical": 0,
                                         "clean_first_time": 0, "clean_first_time_pct": None}),
                       ("_timeliness", {"on_time_pct": None}), ("_money", {}), ("_trend", []),
                       ("_portfolio", {}), ("_reinsurance", {}), ("_quality", {}),
                       ("_late", {}), ("_contracts", {}), ("_scorecard", {})):
        monkeypatch.setattr(er, name, lambda *a, _v=stub, **k: dict(_v) if isinstance(_v, dict) else list(_v))
    today = dt.date(2026, 12, 1)
    got = {
        "week": er.pack(timeline, 7, cadence="week", period="2026-10-14", today=today),
        "month": er.pack(timeline, 7, cadence="month", period="2026-10", today=today),
        "custom": er.pack(timeline, 7, cadence="custom", period="2026-10-01..2026-11-05", today=today),
    }
    assert seen == [dt.date(2026, 10, 18), dt.date(2026, 10, 31), dt.date(2026, 11, 5)]
    assert {k: v["problems"]["escalations"] for k, v in got.items()} == {"week": 3, "month": 2, "custom": 3}


# ── Daily: only what happened that day (executive_report._new_problems) ─────
def _day(s, d):
    return er._new_problems(s, 7, d, d + dt.timedelta(days=1), dt.date(2026, 12, 1))


def test_a_day_counts_only_the_problems_that_started_that_day(timeline):
    out = _day(timeline, dt.date(2026, 10, 1))          # L1 became late; nothing else began
    assert (out["open"], out["escalations"]) == (1, 1)
    l1 = out["escalation_list"][0]
    assert l1["title"] == "Bordereau for L1 not received" and l1["still_open"] is False
    assert l1["status"] == "Received since"


def test_a_day_with_a_dispute_raised(timeline):
    out = _day(timeline, dt.date(2026, 10, 8))
    assert (out["open"], out["escalations"]) == (1, 1)
    assert out["escalation_list"][0]["title"] == "Late mark disputed for D1"


def test_a_reminder_is_not_a_new_escalation(timeline):
    # 12 Oct: the waiting dispute's reminder, and E1's extension — neither is new.
    assert _day(timeline, dt.date(2026, 10, 12))["escalations"] == 0


def test_a_day_counts_what_became_an_escalation_that_day(timeline):
    out = _day(timeline, dt.date(2026, 10, 11))         # L1 Delinquent; E1 became late
    assert {x["title"] for x in out["escalation_list"]} == {
        "Bordereau for L1 not received", "Bordereau for E1 not received"}
    assert out["open"] == 1                              # only E1 started that day


def test_a_check_found_and_fixed_on_their_own_days(timeline):
    assert (_day(timeline, dt.date(2026, 10, 2))["open"], _day(timeline, dt.date(2026, 10, 7))["fixed"]) == (1, 1)
    # C2, found 20 Oct and never fixed, becomes late on the 26th (target 5 days).
    out = _day(timeline, dt.date(2026, 10, 26))
    assert [x["title"] for x in out["escalation_list"]] == ["C2"] and out["escalation_list"][0]["still_open"]


def test_a_held_file_released_after_its_due_date_was_still_escalated_that_day(timeline):
    out = _day(timeline, dt.date(2026, 10, 9))          # due 8 Oct, released on the 9th
    assert [x["title"] for x in out["escalation_list"]] == ["held.xlsx"]
    assert out["escalation_list"][0]["status"] == "Released since"


def test_a_quiet_day_has_nothing(timeline):
    assert (lambda o: (o["open"], o["escalations"], o["fixed"]))(_day(timeline, dt.date(2026, 10, 14))) == (0, 0, 0)


def test_on_time_for_a_day_counts_only_files_due_that_day(timeline):
    # 5 Oct: only D1 was due (not received) — 1 due, 0 on time. 30 Sep: L1, received late.
    d = er._due_in(timeline, 7, dt.date(2026, 10, 5), dt.date(2026, 10, 6), dt.date(2026, 12, 1))
    assert (d["due"], d["due_so_far"], d["on_time"], d["overdue"]) == (1, 1, 0, 1)
    d = er._due_in(timeline, 7, dt.date(2026, 9, 30), dt.date(2026, 10, 1), dt.date(2026, 12, 1))
    assert (d["due"], d["late"], d["on_time_pct"]) == (1, 1, 0.0)
    # 15 Oct: N1 is due, but no broker owes it — not counted, as on the Monthly Report.
    assert er._due_in(timeline, 7, dt.date(2026, 10, 15), dt.date(2026, 10, 16), dt.date(2026, 12, 1))["due"] == 0


def test_daily_premium_is_from_the_files_processed_that_day(monkeypatch):
    """Not by reporting month: a September bordereau processed on 12 Nov counts
    on 12 Nov; a file processed the day before counts the day before."""
    import check_record as cr
    import financial_summary as fs
    import policy_search as ps
    from test_financial_summary import COLUMNS, export, full_row, structure, xlsx
    heads = [h for h, _f in COLUMNS]
    files = [export(1, "2026-09", [full_row("P1", gross=1000, comm=100),
                                   full_row("P2", gross=500, comm=50, cur="GBP")]),
             export(2, "2026-11", [full_row("P3", gross=200, comm=20)], program=12, broker=51),
             export(3, "2026-11", [full_row("P4", gross=999)])]
    when = {1: dt.datetime(2026, 11, 12, 9), 2: dt.datetime(2026, 11, 12, 15), 3: dt.datetime(2026, 11, 11, 9)}
    runs = [SimpleNamespace(id=i, export_id=i, lane=cr.DIRECT, kind=cr.RUN, created_at=when[i]) for i in when]
    fs._REMEMBERED.clear()
    monkeypatch.setattr(cr, "runs_in", lambda s, tid, a, b, **k: [r for r in runs if a <= r.created_at < b])
    monkeypatch.setattr(ps, "_exports", lambda s, seat, carrier_id=None, ids=None: [o for o in files if o.id in ids])
    monkeypatch.setattr(ps, "_structures", lambda s, ids: {1: structure()})
    monkeypatch.setattr(ps, "_names", lambda s, ex: {"programme": {11: "Fleet", 12: "Cargo"},
                                                     "broker": {50: "Wani", 51: "Other"}, "insurer": {}})
    monkeypatch.setattr(fs, "_latest_templates", lambda s, ids: {i: i for i in ids})
    monkeypatch.setattr(fs, "_bytes", lambda s, o: xlsx(heads, o._rows))
    m = er._money_processed(None, 7, er.window("day", "2026-11-12", dt.date(2026, 11, 12)))
    assert (m["currency"], m["files"], m["other_currencies"]) == ("USD", 2, 1)
    assert m["gross_premium"] == {"current": 1200, "previous": 999, "change": 20.1}
    assert m["commission"]["current"] == 120
    assert [(x["name"], x["gross_premium"]) for x in m["top_programmes"]] == [("Fleet", 1000), ("Cargo", 200)]


# ── the errors chart follows the period ────────────────────────────────────
def test_the_errors_chart_is_cut_to_the_period():
    d = dt.date
    unit, p = er.trend_buckets(d(2026, 11, 12), d(2026, 11, 13))
    assert unit == "hour" and len(p) == 24 and p[0][2] == "00:00" and p[-1][2] == "23:00"
    assert p[-1][1] == dt.datetime(2026, 11, 13)
    unit, p = er.trend_buckets(d(2026, 11, 2), d(2026, 11, 9))
    assert unit == "day" and [x[2] for x in p] == ["2 Nov", "3 Nov", "4 Nov", "5 Nov", "6 Nov", "7 Nov", "8 Nov"]
    unit, p = er.trend_buckets(d(2026, 10, 1), d(2026, 11, 1))
    assert unit == "day" and len(p) == 31
    # 3 Sep (a Thursday) to 20 Oct: weeks Monday to Sunday, cut at both ends.
    unit, p = er.trend_buckets(d(2026, 9, 3), d(2026, 10, 21))
    assert unit == "week" and p[0][2] == "3 – 6 Sep" and p[-1][2] == "19 – 20 Oct"
    assert p[4][2] == "28 Sep – 4 Oct"
    assert p[0][0] == dt.datetime(2026, 9, 3) and p[-1][1] == dt.datetime(2026, 10, 21)
    # 15 Feb to 11 Nov: months, the first and last cut at the dates picked.
    unit, p = er.trend_buckets(d(2026, 2, 15), d(2026, 11, 12))
    assert unit == "month" and [x[2] for x in p][:2] == ["Feb 2026", "Mar 2026"] and len(p) == 10
    assert p[0][0] == dt.datetime(2026, 2, 15) and p[-1] == (dt.datetime(2026, 11, 1), dt.datetime(2026, 11, 12), "Nov 2026")


def test_the_chart_reads_each_piece_of_the_period(monkeypatch):
    import check_record as cr
    asked = []
    monkeypatch.setattr(cr, "summary", lambda s, tid, a, b: asked.append((a, b)) or {
        "volumes": {"files": 2}, "quality": {"errors": 3}, "no_human_help": {"files_pct": 50.0}})
    out = er._trend(None, 7, dt.date(2026, 11, 2), dt.date(2026, 11, 9))
    assert len(out) == 7 and asked[0] == (dt.datetime(2026, 11, 2), dt.datetime(2026, 11, 3))
    assert out[0] == {"unit": "day", "label": "2 Nov", "from": "2026-11-02T00:00:00",
                      "to": "2026-11-03T00:00:00", "files": 2, "errors": 3, "clean_first_time_pct": 50.0}


# ── files by status, most common errors, data quality (executive_report._quality) ──
def test_quality_counts_files_by_status_and_the_checks_that_failed_most(monkeypatch):
    import check_record as cr
    asked = []
    monkeypatch.setattr(cr, "summary", lambda s_, tid, a, b: asked.append((a, b)) or {
        "quality": {"files_clean": 6, "files_with_errors": 3, "files_not_validated": 1,
                    "top_failing_checks": [{"check": f"C{i}", "files": 1, "errors": 9 - i,
                                            "severity": "critical"} for i in range(7)]}})
    q = er._quality(None, 7, dt.date(2026, 10, 1), dt.date(2026, 11, 1))
    assert asked == [(dt.datetime(2026, 10, 1), dt.datetime(2026, 11, 1))]
    assert (q["clean"], q["with_errors"], q["not_checked"]) == (6, 3, 1)
    assert [x["check"] for x in q["top_errors"]] == ["C0", "C1", "C2", "C3", "C4"]


# ── the scorecard (executive_report._scorecard) ─────────────────────────────
def test_the_scorecard_puts_the_broker_with_most_late_files_first(timeline, monkeypatch):
    import check_record as cr
    import db
    timeline.add_all([db.Party(id=50, party_type="broker", legal_name="Alpha Brokers"),
                      db.Party(id=51, party_type="broker", legal_name="Beta Brokers")])
    timeline.commit()

    def summary(s_, tid, a, b):
        october = a == dt.datetime(2026, 10, 1)
        return {"by_broker": [{"id": 50, "files": 4, "errors": 2, "straight_through_pct": 50.0 if october else 75.0},
                              {"id": 51, "files": 6, "errors": 9, "straight_through_pct": 20.0}],
                "by_programme": [{"id": 1, "files": 10, "errors": 11, "straight_through_pct": 30.0}]}
    monkeypatch.setattr(cr, "summary", summary)
    w = er.window("month", "2026-10", dt.date(2026, 11, 12))
    out = er._scorecard(timeline, 7, w, {"broker": {50: 1000.0, 52: 0}, "programme": {1: 1000.0}})
    b = out["broker"]
    # Alpha owes D1 and E1 (due in October, never came); Beta was late with nothing.
    assert [r["name"] for r in b["rows"]] == ["Alpha Brokers", "Beta Brokers"] and b["total"] == 2
    alpha = b["rows"][0]
    assert (alpha["premium"], alpha["files"], alpha["due"], alpha["late_or_missing"]) == (1000.0, 4, 2, 2)
    assert (alpha["on_time_pct"], alpha["clean_first_time_pct"], alpha["prev_clean_first_time_pct"]) == (0.0, 50.0, 75.0)
    # September: L1 was due on 30 Sep itself, so at that day's end it was not late
    # yet — nothing was due so far, and there is no on-time share to compare with.
    assert alpha["prev_on_time_pct"] is None
    assert b["rows"][1]["due"] == 0 and b["rows"][1]["on_time_pct"] is None
    assert out["programme"]["rows"][0]["name"] == "Fleet" and out["programme"]["rows"][0]["late_or_missing"] == 2


# ── late submissions (executive_report._late) ──────────────────────────────
def test_late_lists_files_due_in_the_period_that_came_late_or_not_at_all(timeline):
    # October, at the end of the 31st: D1 (due 5 Oct) and E1 (due 10 Oct) never
    # came; L1 was due in September; N1 has no broker, so nobody owes it.
    out = er._late(timeline, 7, dt.date(2026, 10, 1), dt.date(2026, 11, 1), dt.date(2026, 10, 31))
    assert (out["late"], out["missing"], out["never"], out["total"]) == (0, 2, 0, 2)
    assert [(x["month"], x["status"], x["days_late"], x["arrived_on"]) for x in out["list"]] == [
        ("E1", "Not yet", 21, None), ("D1", "Not yet", 26, None)]
    assert out["list"][0]["programme"] == "Fleet" and out["list"][0]["broker"] == "#50"


def test_a_file_that_came_after_the_period_ended_is_still_missing_at_its_end(timeline):
    # L1 was due 30 Sep and came 20 Oct: at the end of September it was missing,
    # read again at the end of October it came 20 days late.
    sep = (dt.date(2026, 9, 1), dt.date(2026, 10, 1))
    at_sep = er._late(timeline, 7, *sep, dt.date(2026, 9, 30))
    assert at_sep["total"] == 0                     # due that very day: not late yet
    at_oct = er._late(timeline, 7, *sep, dt.date(2026, 10, 31))
    assert [(x["status"], x["days_late"], x["arrived_on"]) for x in at_oct["list"]] == [
        ("Arrived late", 20, "2026-10-20")]


# ── contract utilization (executive_report._contracts, _written) ──────────────
def _contracts_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(eng, tables=[db.Program.__table__, db.Party.__table__,
                                             db.Contract.__table__])
    s = sessionmaker(bind=eng)()
    d = dt.date
    s.add_all([db.Program(id=1, tenant_id=7, name="Fleet", is_app_managed=True),
               db.Party(id=50, party_type="broker", legal_name="Alpha Brokers")])
    for cid, name, status, cap, inc, exp in (
            (1, "Over", "active", 1000, d(2026, 1, 1), d(2026, 12, 31)),
            (2, "Near", "Active", 1000, d(2026, 1, 1), d(2026, 12, 31)),
            (3, "Fine", "active", 1000, d(2026, 1, 1), d(2026, 12, 31)),
            (4, "No cap", "active", None, d(2026, 1, 1), d(2026, 12, 31)),
            (5, "Draft", "drafted", 1000, d(2026, 1, 1), d(2026, 12, 31)),
            (6, "Ended", "active", 1000, d(2025, 1, 1), d(2025, 12, 31)),
            (7, "Later", "active", 1000, d(2027, 1, 1), d(2027, 12, 31))):
        s.add(db.Contract(id=cid, tenant_id=7, program_id=1, broker_party_id=50, name=name,
                          status=status, premium_cap_amount=cap, premium_cap_currency="USD",
                          inception_dt=inc, expiry_dt=exp, is_app_managed=True,
                          created_at=dt.datetime(2025, 12, 1)))
    s.commit()
    return s


def test_contract_utilization_both_ways(monkeypatch):
    s = _contracts_db()
    monkeypatch.setattr(er, "_files_by_contract", lambda s_, tid, a, b: {1: 2, 2: 1})
    asked = []

    def written(s_, tid, contracts, as_at):
        asked.append(([c.id for c in contracts], as_at))
        return {1: {"written": 1200.0, "files": 2, "other_currency_rows": 1},
                2: {"written": 850.0, "files": 1, "other_currency_rows": 0},
                3: {"written": 100.0, "files": 0, "other_currency_rows": 0}}
    monkeypatch.setattr(er, "_written", written)
    out = er._contracts(s, 7, dt.date(2026, 10, 1), dt.date(2026, 11, 1), dt.date(2026, 10, 31), False)
    # Bound in October: 1–4. Draft, ended and not-yet-started contracts are not.
    assert (out["bound"], out["with_files"], out["without_files"]) == (4, 2, 2)
    assert [x["contract"] for x in out["idle"]] == ["Fine", "No cap"]
    assert asked == [([1, 2, 3], dt.date(2026, 10, 31))]
    assert [(x["contract"], x["used_pct"], x["flag"]) for x in out["caps"]] == [
        ("Over", 120.0, "Over cap"), ("Near", 85.0, "Near cap"), ("Fine", 10.0, None)]
    assert (out["over_cap"], out["near_cap"], out["no_cap"]) == (1, 1, 1)
    assert out["caps"][0]["broker"] == "Alpha Brokers" and out["caps"][0]["other_currency_rows"] == 1


def test_a_daily_report_lists_the_caps_of_contracts_that_sent_files_that_day(monkeypatch):
    s = _contracts_db()
    monkeypatch.setattr(er, "_files_by_contract", lambda s_, tid, a, b: {2: 1})
    monkeypatch.setattr(er, "_written", lambda s_, tid, cs, at: {
        c.id: {"written": 500.0, "files": 1, "other_currency_rows": 0} for c in cs})
    day = dt.date(2026, 11, 12)
    out = er._contracts(s, 7, day, day + dt.timedelta(days=1), day, True)
    assert [x["contract"] for x in out["caps"]] == ["Near"] and out["with_files"] == 1


def test_premium_written_counts_each_file_once_in_the_contracts_term_and_currency(monkeypatch):
    import financial_figures as ff
    import financial_summary as fs
    import policy_search as ps
    import exception_tally
    c = SimpleNamespace(id=1, inception_dt=dt.date(2026, 1, 1), expiry_dt=dt.date(2026, 12, 31),
                        premium_cap_currency="usd")
    ex = lambda i, md5, made=dt.datetime(2026, 6, 1): SimpleNamespace(  # noqa: E731
        id=i, contract_id=1, template_id=9, blob_md5=md5, blob_ref=None, created_at=made)
    exports = [ex(1, "a"), ex(2, "a"),                 # the same content twice: one file
               ex(3, "b"),                             # reports on 2025: before the term
               ex(4, "c"),                             # no reporting month: counted
               ex(5, "d", dt.datetime(2026, 11, 2))]   # processed after the day asked
    monkeypatch.setattr(ps, "_exports", lambda s_, seat, *a, **k: exports)
    monkeypatch.setattr(exception_tally, "export_versions", lambda s_, ids: {
        1: {"reporting_period": "2026-03"}, 2: {"reporting_period": "2026-03"},
        3: {"reporting_period": "2025-11"}})
    monkeypatch.setattr(fs, "_latest_templates", lambda s_, ids: {})
    monkeypatch.setattr(ps, "_structures", lambda s_, ids: {})
    rows = {2: [{"g": 300, "c": "USD"}, {"g": 50, "c": "EUR"}, {"g": None, "c": "USD"}],
            4: [{"g": 200, "c": "USD"}], 3: [{"g": 999, "c": "USD"}], 5: [{"g": 999, "c": "USD"}]}
    monkeypatch.setattr(fs, "_read", lambda s_, o, st, t: SimpleNamespace(evidence={}, rows=rows.get(o.id, [])))
    monkeypatch.setattr(ff, "pool", lambda evs: {})
    monkeypatch.setattr(ff, "convention", lambda e: None)
    monkeypatch.setattr(ff, "figures", lambda cells, conv: {
        "gross_premium": {"value": cells["g"]}, "currency": {"status": ff.REPORTED, "value": cells["c"]}})
    out = er._written(None, 7, [c], dt.date(2026, 10, 31))
    assert out == {1: {"written": 500.0, "files": 2, "other_currency_rows": 1}}
