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
    assert out["trend"] == ["2026-10", "2026-09", "2026-08", "2026-07", "2026-06", "2026-05"]
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
