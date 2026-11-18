"""MOPR 42 — the monthly pack (monthly_report.py), and the grouped open list
it sends people to (fix_performance.summary).

Run with:  DATABASE_URL=sqlite:// .venv/bin/python -m pytest test_monthly_report.py
"""
from __future__ import annotations

from datetime import date, datetime

import fix_performance as fp
import monthly_report as mr

NOW = datetime(2026, 10, 20, 12, 0)


def _mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(
        eng, tables=[db.CheckRun.__table__, db.CheckResult.__table__,
                     db.CheckError.__table__, db.ExceptionDecisionLog.__table__,
                     db.QualitySetting.__table__, db.ExpectedSubmission.__table__])
    return sessionmaker(bind=eng)()


def _due(s, due, *, received=None, closes=None, case=None, broker=8, program=7):
    import db
    s.add(db.ExpectedSubmission(tenant_id=1, program_id=program, broker_party_id=broker,
                                period=f"{due.year}-{due.month:02d}", period_start=due,
                                period_end=due, due_date=due, received_at=received,
                                closes_on=closes, case_state=case))
    s.flush()


def _file(s, *, at, export_id, status="has_exceptions", errors=0, critical=0, rows=10,
          rows_with_errors=0, types=None, chain=None, broker=8, program=7):
    import db
    r = db.CheckRun(tenant_id=1, lane="direct", kind="run", status=status, export_id=export_id,
                    chain_key=chain, broker_party_id=broker, program_id=program,
                    filename=f"file-{export_id}.xlsx", created_at=at, errors_total=errors,
                    critical_count=critical, rows_checked=rows, rows_with_errors=rows_with_errors,
                    transaction_types=types)
    s.add(r)
    s.flush()
    return r


def _problem(s, run, *, fpr, found, rule_id=1, name="Premium must be more than 0",
             severity="critical", row=1):
    import db
    s.add(db.CheckResult(run_id=run.id, tenant_id=1, check_key=f"rule:{rule_id}",
                         kind="contract_rule", rule_id=rule_id, name=name,
                         severity=severity, outcome="failed", error_count=1))
    s.add(db.CheckError(run_id=run.id, tenant_id=1, lane=run.lane, export_id=run.export_id,
                        program_id=run.program_id, broker_party_id=run.broker_party_id,
                        chain_key=run.chain_key, check_key=f"rule:{rule_id}", rule_id=rule_id,
                        code=name, severity=severity, sheet="Premium", row=row,
                        field="Premium", policy_number=f"P{row}", message="bad",
                        fingerprint=fpr, first_found_at=found, created_at=run.created_at))
    s.flush()


def _fixed(s, *, at, export_id, row=1):
    import db
    s.add(db.ExceptionDecisionLog(tenant_id=1, export_id=export_id, lane="direct", kind="fix",
                                  sheet="Premium", row=row, field="Premium",
                                  decided_by_user_id=5, decided_at=at))
    s.flush()


def test_months_shift_across_a_year():
    assert mr.shift("2026-01", -1) == "2025-12"
    assert mr.shift("2026-10", -12) == "2025-10"
    assert mr.shift("2026-12", 1) == "2027-01"


def test_timeliness_counts_the_files_due_in_the_month_by_how_they_stand():
    from submission_case import DELINQUENT
    s = _mem_session()
    # One period per programme: a broker owes one file a period on each.
    _due(s, date(2026, 10, 5), received=date(2026, 10, 5), program=1)         # on time
    _due(s, date(2026, 10, 5), received=date(2026, 10, 8), program=2)         # 3 days late
    _due(s, date(2026, 10, 10), case=DELINQUENT, program=3)                   # still waited for
    _due(s, date(2026, 10, 1), closes=date(2026, 10, 15), program=4)          # never arrived
    _due(s, date(2026, 10, 25), program=5)                                    # not due yet
    _due(s, date(2026, 10, 5), broker=None, program=6)                        # nobody owes it
    _due(s, date(2026, 9, 5), received=date(2026, 9, 5), program=1)           # last month
    s.commit()

    out = mr.pack(s, 1, "2026-10", now=NOW)
    t = out["months"]["2026-10"]["timeliness"]
    assert (t["due"], t["due_so_far"]) == (5, 4)
    assert (t["on_time"], t["late"], t["overdue"], t["never"]) == (1, 1, 1, 1)
    assert t["on_time_pct"] == 25.0 and t["avg_days_late"] == 3.0
    assert t["cases_open"] == 1
    assert out["months"]["2026-09"]["timeliness"]["on_time"] == 1


def test_volumes_and_quality_come_from_the_files_checked_in_the_month():
    s = _mem_session()
    _file(s, at=datetime(2026, 10, 2), export_id=1, status="clean",
          types={"new": {"rows": 3, "policies": 3}})
    _file(s, at=datetime(2026, 10, 3), export_id=2, errors=4, critical=1,
          rows_with_errors=2, types={"renewal": {"rows": 7, "policies": 6}})
    s.commit()

    m = mr.pack(s, 1, "2026-10", now=NOW)["months"]["2026-10"]
    assert (m["volumes"]["files"], m["volumes"]["rows"]) == (2, 20)
    assert m["volumes"]["counted_by"] == "policies"
    assert (m["volumes"]["new"], m["volumes"]["renewed"], m["volumes"]["cancelled"]) == (3, 6, 0)
    q = m["quality"]
    assert (q["errors"], q["critical"]) == (4, 1)
    assert (q["clean_first_time"], q["clean_first_time_pct"]) == (1, 50.0)
    assert q["rows_with_errors_pct"] == 10.0


def test_waiting_is_counted_as_it_stood_on_the_last_day_of_the_month():
    s = _mem_session()
    run = _file(s, at=datetime(2026, 9, 20), export_id=10, chain="sub:S1", errors=1)
    _problem(s, run, fpr="a", found=datetime(2026, 9, 20))
    _fixed(s, at=datetime(2026, 10, 3), export_id=10)
    run2 = _file(s, at=datetime(2026, 10, 18), export_id=11, chain="sub:S2", errors=1)
    _problem(s, run2, fpr="b", found=datetime(2026, 10, 18))
    s.commit()

    out = mr.pack(s, 1, "2026-10", now=NOW)
    sep, octo = out["months"]["2026-09"]["fixes"], out["months"]["2026-10"]["fixes"]
    # At the end of September the first problem was 11 days old, past the 5-day target.
    assert (sep["fixed"], sep["waiting"], sep["waiting_past_target"]) == (0, 1, 1)
    # October fixed it after 13 days; the second, found on the 18th, waits to now.
    assert (octo["fixed"], octo["avg_days"], octo["within_target_pct"]) == (1, 13.0, 0.0)
    assert (octo["waiting"], octo["waiting_past_target"]) == (1, 0)


def test_a_month_before_the_records_began_has_no_data_and_a_running_month_is_not_complete():
    s = _mem_session()
    _file(s, at=datetime(2026, 10, 2), export_id=1, status="clean")
    s.commit()
    out = mr.pack(s, 1, "2026-10", now=NOW)
    assert out["last_month"] == "2026-09" and out["last_year"] == "2025-10"
    # Only the three periods compared are worked out — no six-month trend.
    assert set(out["months"]) == {"2026-10", "2026-09", "2025-10"} and "trend" not in out
    assert out["months"]["2026-10"]["has_data"] is True
    assert out["months"]["2025-10"]["has_data"] is False
    assert out["months"]["2026-10"]["complete"] is False
    assert out["months"]["2026-09"]["complete"] is True


def test_one_line_per_broker_and_programme_joins_all_three_reports():
    s = _mem_session()
    _due(s, date(2026, 10, 5), received=date(2026, 10, 5))
    run = _file(s, at=datetime(2026, 10, 6), export_id=10, chain="sub:S1", errors=1)
    _problem(s, run, fpr="a", found=datetime(2026, 10, 6))
    _file(s, at=datetime(2026, 10, 7), export_id=20, status="clean", broker=9)
    s.commit()

    rows = {(r["broker_party_id"], r["program_id"]): r
            for r in mr.pack(s, 1, "2026-10", now=NOW)["by_broker_programme"]}
    assert set(rows) == {(8, 7), (9, 7)}
    a = rows[(8, 7)]
    assert (a["on_time"], a["due_so_far"], a["files"], a["errors"]) == (1, 1, 1, 1)
    # Found on the 6th, still open on the 20th: past the 5-day target.
    assert a["waiting_past_target"] == 1
    assert rows[(9, 7)]["clean_first_time"] == 1
    # The broker with work waiting past target comes first.
    assert mr.pack(s, 1, "2026-10", now=NOW)["by_broker_programme"][0]["broker_party_id"] == 8


def test_the_open_list_groups_by_file_and_check_and_severity_narrows_only_the_list():
    s = _mem_session()
    run = _file(s, at=datetime(2026, 10, 10), export_id=10, chain="sub:S1", errors=3)
    _problem(s, run, fpr="a", found=datetime(2026, 10, 10), row=1)
    _problem(s, run, fpr="b", found=datetime(2026, 10, 12), row=2)
    _problem(s, run, fpr="c", found=datetime(2026, 10, 18), row=3, rule_id=2,
             name="State is valid", severity="warning")
    s.commit()

    o = fp.summary(s, 1, datetime(2026, 10, 1), datetime(2026, 11, 1), now=NOW)["open"]
    assert o["groups_total"] == 2
    g = o["groups"][0]
    assert (g["check"], g["problems"], g["past_target"], g["severity"]) == (
        "Premium must be more than 0", 2, 2, "critical")
    assert g["oldest_days"] == 10.5

    o = fp.summary(s, 1, datetime(2026, 10, 1), datetime(2026, 11, 1), now=NOW,
                   severity="warning")["open"]
    assert [g["check"] for g in o["groups"]] == ["State is valid"]
    assert o["total"] == 1
    # The counts above the list stay the whole picture.
    assert o["problems"] == 3


# --- a day, a week or dates picked (pack_for) ------------------------------------

def test_a_period_covers_the_executive_reports_dates_with_the_monthly_reports_comparisons():
    import pytest
    from fastapi import HTTPException
    today = date(2026, 11, 18)                                   # a Wednesday
    w = mr.window("week", "2026-11-12", today)                   # any day of the week
    assert (w["period"], w["start"], w["end"]) == ("2026-11-09", date(2026, 11, 9), date(2026, 11, 16))
    assert w["previous"]["period"] == "2026-11-02"
    # The same week last year: the Monday 52 weeks back, so the same weekdays.
    assert w["last_year"] == {"period": "2025-11-10", "start": date(2025, 11, 10),
                              "end": date(2025, 11, 17)}
    d = mr.window("day", "2026-11-17", today)
    # A day beside the day before, as on the Executive Report, and no last year.
    assert d["previous"]["period"] == "2026-11-16" and d["last_year"] is None
    m = mr.window("month", None, today)
    assert (m["period"], m["previous"]["period"], m["last_year"]["period"]) == (
        "2026-11", "2026-10", "2025-11")
    c = mr.window("custom", "2026-11-01..2026-11-10", today)
    assert c["previous"]["period"] == "2026-10-22..2026-10-31" and c["last_year"] is None
    for cadence, period in (("day", "2026-11-19"), ("week", "2026-11-23"), ("custom", "2026-11-10..2026-11-19")):
        with pytest.raises(HTTPException):
            mr.window(cadence, period, today)


def test_a_week_counts_only_what_was_due_checked_and_fixed_in_its_own_dates():
    s = _mem_session()
    _due(s, date(2026, 10, 14), received=date(2026, 10, 14))     # this week, on time
    _due(s, date(2026, 10, 5), received=date(2026, 10, 9), program=9)   # the week before, late
    run = _file(s, at=datetime(2026, 10, 13), export_id=1, chain="sub:1", errors=2, critical=1)
    _problem(s, run, fpr="a", found=datetime(2026, 10, 13))
    _fixed(s, at=datetime(2026, 10, 15), export_id=1)
    before = _file(s, at=datetime(2026, 10, 6), export_id=2, chain="sub:2", errors=4)
    _problem(s, before, fpr="b", found=datetime(2026, 10, 6))
    s.commit()

    p = mr.pack_for(s, 1, "week", "2026-10-14", now=NOW)
    assert (p["cadence"], p["month"], p["last_month"], p["last_year"]) == (
        "week", "2026-10-12", "2026-10-05", "2025-10-13")
    this, prev = p["months"]["2026-10-12"], p["months"]["2026-10-05"]
    assert (this["timeliness"]["due"], this["timeliness"]["on_time"], this["timeliness"]["late"]) == (1, 1, 0)
    assert (prev["timeliness"]["due"], prev["timeliness"]["late"]) == (1, 1)
    assert (this["volumes"]["files"], this["quality"]["errors"]) == (1, 2)
    assert (prev["volumes"]["files"], prev["quality"]["errors"]) == (1, 4)
    # Fixed in the week; still waiting at its end: the other week's problem.
    assert (this["fixes"]["fixed"], this["fixes"]["waiting"]) == (1, 1)
    assert (prev["fixes"]["fixed"], prev["fixes"]["waiting"]) == (0, 1)
    assert this["complete"] is True and p["months"]["2025-10-13"]["has_data"] is False
    assert "trend" not in p
    assert p["spans"]["2026-10-12"] == {"from": "2026-10-12", "to": "2026-10-18",
                                        "title": "Week of 12 Oct 2026"}
    assert [r["files"] for r in p["by_broker_programme"]] == [1]


def test_a_day_is_compared_with_the_day_before():
    s = _mem_session()
    _file(s, at=datetime(2026, 10, 20, 9), export_id=1, errors=1)   # today
    _file(s, at=datetime(2026, 10, 19, 9), export_id=2, errors=9)   # yesterday
    _file(s, at=datetime(2026, 10, 13, 9), export_id=3, errors=5)   # a week ago: not compared
    s.commit()
    p = mr.pack_for(s, 1, "day", "2026-10-20", now=NOW)
    assert (p["month"], p["last_month"], p["last_year"]) == ("2026-10-20", "2026-10-19", None)
    assert p["months"]["2026-10-20"]["quality"]["errors"] == 1
    assert p["months"]["2026-10-19"]["quality"]["errors"] == 9
    assert p["months"]["2026-10-20"]["complete"] is False          # today, so far
    assert set(p["months"]) == {"2026-10-20", "2026-10-19"}


def test_dates_picked_are_compared_with_as_many_days_just_before():
    s = _mem_session()
    _file(s, at=datetime(2026, 10, 3), export_id=1, errors=2)
    _file(s, at=datetime(2026, 9, 25), export_id=2, errors=7)
    s.commit()
    p = mr.pack_for(s, 1, "custom", "2026-10-01..2026-10-10", now=NOW)
    assert (p["last_month"], p["last_year"]) == ("2026-09-21..2026-09-30", None)
    assert p["months"][p["month"]]["quality"]["errors"] == 2
    assert p["months"][p["last_month"]]["quality"]["errors"] == 7
    assert p["spans"][p["month"]]["title"] == "1 – 10 Oct 2026"


def test_a_month_through_pack_for_is_the_monthly_pack_as_it_always_was():
    s = _mem_session()
    _due(s, date(2026, 10, 5), received=date(2026, 10, 7))
    _file(s, at=datetime(2026, 10, 6), export_id=1, errors=3)
    s.commit()
    old = mr.pack(s, 1, "2026-10", now=NOW)
    new = mr.pack_for(s, 1, "month", "2026-10", now=NOW)
    assert {k: v for k, v in new.items() if k not in ("cadence", "period", "spans")} == old
    assert (new["cadence"], new["period"]) == ("month", "2026-10")
    assert new["spans"]["2025-10"]["title"] == "October 2025"
    # No month picked: this month, as the report has always opened.
    assert mr.pack_for(s, 1, "month", None, now=NOW)["month"] == "2026-10"


def test_a_period_before_the_records_began_is_not_available_and_one_after_reads_real_zeros():
    s = _mem_session()
    # The calendar goes back to each contract's start, but files only came
    # through the system from October 2026.
    _due(s, date(2025, 10, 10), program=9)
    _due(s, date(2026, 9, 10), program=9)
    _file(s, at=datetime(2026, 10, 2), export_id=1)
    s.commit()
    out = mr.pack(s, 1, "2026-10", now=NOW)
    assert out["records_began"] == "2026-10-02"
    assert out["months"]["2025-10"]["has_data"] is False      # due dates, but no records
    assert out["months"]["2026-09"]["has_data"] is False
    assert out["months"]["2026-10"]["has_data"] is True
    # November: nothing checked yet, but the records ran — a real 0, not "Not available".
    nov = mr.pack(s, 1, "2026-11", now=datetime(2026, 11, 20))["months"]["2026-11"]
    assert nov["has_data"] is True and nov["volumes"]["files"] == 0
    # A file received earlier on the calendar moves the start back.
    _due(s, date(2026, 8, 5), received=date(2026, 8, 20))
    s.commit()
    assert mr.records_began(s, 1, NOW.date()) == date(2026, 8, 20)
    assert mr.pack(s, 1, "2026-10", now=NOW)["months"]["2026-09"]["has_data"] is True
