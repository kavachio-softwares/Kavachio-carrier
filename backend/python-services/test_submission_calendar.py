"""Tests for the pure submission-calendar logic (Group 3)."""
from datetime import date, datetime

from submission_calendar import (
    resolve_schedule, generate_expected, derive_status, due_date_for,
    ResolvedSchedule,
)


# ---- resolver: override → contract → nothing --------------------------------

def test_resolve_both_from_contract():
    r = resolve_schedule(contract_frequency="Monthly",
                         contract_inception=date(2026, 1, 1))
    assert r is not None
    assert r.frequency == "monthly"
    assert r.anchor == date(2026, 1, 1)


def test_resolve_override_wins_over_contract():
    r = resolve_schedule(frequency_override="quarterly",
                         anchor_override=date(2026, 2, 15),
                         contract_frequency="monthly",
                         contract_inception=date(2026, 1, 1))
    assert r.frequency == "quarterly"
    assert r.anchor == date(2026, 2, 15)


def test_resolve_missing_frequency_returns_none():
    assert resolve_schedule(contract_inception=date(2026, 1, 1)) is None


def test_resolve_missing_anchor_returns_none():
    assert resolve_schedule(contract_frequency="monthly") is None


def test_resolve_missing_both_returns_none():
    assert resolve_schedule() is None


def test_resolve_bad_frequency_returns_none():
    assert resolve_schedule(contract_frequency="fortnightly",
                            contract_inception=date(2026, 1, 1)) is None


def test_resolve_coerces_datetime_anchor():
    r = resolve_schedule(contract_frequency="monthly",
                         contract_inception=datetime(2026, 1, 1, 9, 30))
    assert r.anchor == date(2026, 1, 1)


# ---- period generation + due dates ------------------------------------------

def test_monthly_periods_and_due_dates():
    r = resolve_schedule(contract_frequency="monthly",
                         contract_inception=date(2026, 1, 1),
                         due_offset_days=10)
    rows = generate_expected(r, date(2026, 1, 1), date(2026, 3, 31))
    assert [x["period"] for x in rows] == ["2026-01", "2026-02", "2026-03"]
    jan = rows[0]
    assert jan["period_start"] == date(2026, 1, 1)
    assert jan["period_end"] == date(2026, 1, 31)
    assert jan["due_date"] == date(2026, 2, 10)      # period_end + 10
    # February end-of-month handled (28 days in 2026)
    assert rows[1]["period_end"] == date(2026, 2, 28)
    assert rows[1]["due_date"] == date(2026, 3, 10)


def test_quarterly_periods_align_to_calendar_quarters():
    r = resolve_schedule(contract_frequency="quarterly",
                         contract_inception=date(2026, 2, 15),  # mid-Q1
                         due_offset_days=15)
    rows = generate_expected(r, date(2026, 1, 1), date(2026, 12, 31))
    assert [x["period"] for x in rows] == ["2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"]
    q1 = rows[0]
    assert q1["period_start"] == date(2026, 1, 1)
    assert q1["period_end"] == date(2026, 3, 31)
    assert q1["due_date"] == date(2026, 4, 15)


def test_weekly_periods():
    r = resolve_schedule(contract_frequency="weekly",
                         contract_inception=date(2026, 1, 5),   # a Monday
                         due_offset_days=2)
    rows = generate_expected(r, date(2026, 1, 5), date(2026, 1, 25))
    assert rows[0]["period_start"] == date(2026, 1, 5)
    assert rows[0]["period_end"] == date(2026, 1, 11)
    assert rows[0]["due_date"] == date(2026, 1, 13)
    assert len(rows) == 3


def test_no_periods_before_anchor():
    r = resolve_schedule(contract_frequency="monthly",
                         contract_inception=date(2026, 6, 1))
    # window starts before the contract; nothing owed before June
    rows = generate_expected(r, date(2026, 1, 1), date(2026, 7, 31))
    assert [x["period"] for x in rows] == ["2026-06", "2026-07"]


def test_generate_none_returns_empty():
    assert generate_expected(None, date(2026, 1, 1), date(2026, 12, 31)) == []


def test_generate_empty_window_returns_empty():
    r = ResolvedSchedule("monthly", date(2026, 1, 1))
    assert generate_expected(r, date(2026, 3, 1), date(2026, 1, 1)) == []


# ---- due date as a DAY OF MONTH ---------------------------------------------

def test_day_of_month_sets_the_deadline_for_monthly():
    r = resolve_schedule(contract_frequency="monthly",
                         contract_inception=date(2026, 1, 1),
                         due_day_of_month=15)
    rows = generate_expected(r, date(2026, 1, 1), date(2026, 3, 31))
    # January's file is due the 15th of February, and so on — the same date
    # every period, which is the whole point of a fixed day.
    assert rows[0]["due_date"] == date(2026, 2, 15)
    assert rows[1]["due_date"] == date(2026, 3, 15)
    assert rows[2]["due_date"] == date(2026, 4, 15)


def test_day_of_month_clamps_to_a_short_month():
    # "The 31st" has to mean something in February. It clamps to the real last
    # day and does NOT spill into March.
    r = resolve_schedule(contract_frequency="monthly",
                         contract_inception=date(2026, 1, 1),
                         due_day_of_month=31)
    assert due_date_for(r, date(2026, 1, 31)) == date(2026, 2, 28)   # 2026: not a leap year
    assert due_date_for(r, date(2026, 3, 31)) == date(2026, 4, 30)   # April has 30
    assert due_date_for(r, date(2026, 4, 30)) == date(2026, 5, 31)   # May has 31


def test_day_of_month_clamps_in_a_leap_february():
    r = resolve_schedule(contract_frequency="monthly",
                         contract_inception=date(2028, 1, 1),
                         due_day_of_month=30)
    assert due_date_for(r, date(2028, 1, 31)) == date(2028, 2, 29)


def test_day_of_month_applies_to_quarterly():
    r = resolve_schedule(contract_frequency="quarterly",
                         contract_inception=date(2026, 1, 1),
                         due_day_of_month=20)
    rows = generate_expected(r, date(2026, 1, 1), date(2026, 12, 31))
    assert rows[0]["due_date"] == date(2026, 4, 20)    # Q1 ends 31 Mar
    assert rows[1]["due_date"] == date(2026, 7, 20)    # Q2 ends 30 Jun


def test_weekly_ignores_day_of_month_and_uses_the_offset():
    # Weekly periods end on arbitrary dates, so a day-of-month cannot express
    # their deadline — the offset stays in charge even when a day is set.
    r = resolve_schedule(contract_frequency="weekly",
                         contract_inception=date(2026, 1, 5),   # a Monday
                         due_day_of_month=15, due_offset_days=2)
    rows = generate_expected(r, date(2026, 1, 5), date(2026, 1, 25))
    assert rows[0]["period_end"] == date(2026, 1, 11)
    assert rows[0]["due_date"] == date(2026, 1, 13)    # period_end + 2


def test_no_day_of_month_falls_back_to_the_offset():
    # A schedule saved before day-of-month existed must keep producing exactly
    # the dates it always did.
    r = resolve_schedule(contract_frequency="monthly",
                         contract_inception=date(2026, 1, 1),
                         due_offset_days=10)
    assert r.due_day_of_month is None
    assert due_date_for(r, date(2026, 1, 31)) == date(2026, 2, 10)


def test_offset_10_and_day_10_agree_for_monthly():
    # The backfill leans on this: for monthly/quarterly an offset of <= 28 and
    # the same number as a day-of-month are the SAME DATE, because periods end
    # on a month's last day. This is what makes the migration lossless.
    off = resolve_schedule(contract_frequency="monthly",
                           contract_inception=date(2026, 1, 1), due_offset_days=10)
    dom = resolve_schedule(contract_frequency="monthly",
                           contract_inception=date(2026, 1, 1), due_day_of_month=10)
    win = (date(2026, 1, 1), date(2026, 12, 31))
    assert ([r["due_date"] for r in generate_expected(off, *win)]
            == [r["due_date"] for r in generate_expected(dom, *win)])


def test_day_of_month_is_clamped_to_a_real_day_number():
    r = resolve_schedule(contract_frequency="monthly",
                         contract_inception=date(2026, 1, 1), due_day_of_month=99)
    assert r.due_day_of_month == 31          # never reaches date() as 99
    assert due_date_for(r, date(2026, 1, 31)) == date(2026, 2, 28)


# ---- status: the color boundaries -------------------------------------------
# due 2026-02-10, soon window 5 → amber from 02-05, "due today" ON 02-10,
# overdue from 02-11. There is no grace period.

DUE = date(2026, 2, 10)


def test_status_scheduled_before_window():
    assert derive_status(DUE, date(2026, 2, 4), soon_window_days=5) == "scheduled"


def test_status_due_soon_at_window_edge():
    assert derive_status(DUE, date(2026, 2, 5), soon_window_days=5) == "due_soon"


def test_status_due_soon_day_before_due():
    assert derive_status(DUE, date(2026, 2, 9), soon_window_days=5) == "due_soon"


def test_status_due_today_on_the_due_date():
    # The due date is its own state — it used to be swallowed by 'due_soon', so
    # the one day the file had to go out looked no different from four days out.
    assert derive_status(DUE, DUE, soon_window_days=5) == "due_today"


def test_status_overdue_the_day_after_due():
    # No grace: the deadline is missed the moment the date passes.
    assert derive_status(DUE, date(2026, 2, 11), soon_window_days=5) == "overdue"


def test_status_stays_overdue_however_long_it_slips():
    # Nothing escalates past 'overdue' — the retired 'late' was the same state
    # under a second name.
    assert derive_status(DUE, date(2026, 2, 14), soon_window_days=5) == "overdue"
    assert derive_status(DUE, date(2026, 9, 1), soon_window_days=5) == "overdue"


def test_status_on_time_when_received_before_due():
    assert derive_status(DUE, date(2026, 2, 20), received_on=date(2026, 2, 8)) == "on_time"


def test_status_received_late_when_received_after_due():
    assert derive_status(DUE, date(2026, 2, 20), received_on=date(2026, 2, 12)) == "received_late"


def test_status_received_on_due_date_is_on_time():
    assert derive_status(DUE, date(2026, 2, 20), received_on=DUE) == "on_time"


# ---- received detection (service layer, in-memory DB) -----------------------

def _mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(
        eng, tables=[db.Program.__table__, db.Contract.__table__,
                     db.SubmissionSchedule.__table__, db.ExpectedSubmission.__table__,
                     db.ActivityEvent.__table__])
    return sessionmaker(bind=eng)()


def _seed_monthly(s):
    import db, submission_calendar_service as svc
    p = db.Program(tenant_id=1, name="Acme", bdx_frequency="monthly"); s.add(p); s.flush()
    db.Contract(program_id=p.id, tenant_id=1, extracted={"inception_dt": "2026-01-01"})  # noqa
    c = db.Contract(program_id=p.id, tenant_id=1, extracted={"inception_dt": "2026-01-01"})
    s.add(c); s.flush()
    sched = db.SubmissionSchedule(tenant_id=1, program_id=p.id); s.add(sched); s.flush()
    svc.materialize_schedule(s, sched, today=date(2026, 2, 6), horizon_months=3)
    s.commit()
    return p


def test_mark_received_picks_oldest_ended_period():
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed_monthly(s)
    # A file produced 2026-02-08 satisfies the January period (ended 01-31).
    res = svc.mark_received(s, p.id, received_on=date(2026, 2, 8), export_id=42)
    assert res["period"] == "2026-01"
    assert res["status"] == "on_time"          # received 02-08 <= due 02-10


def test_mark_received_late_when_after_due():
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed_monthly(s)
    res = svc.mark_received(s, p.id, received_on=date(2026, 2, 15), export_id=7)
    assert res["period"] == "2026-01"
    assert res["status"] == "received_late"    # 02-15 > due 02-10


def test_mark_received_advances_to_next_period():
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed_monthly(s)
    svc.mark_received(s, p.id, received_on=date(2026, 2, 8))     # Jan
    res = svc.mark_received(s, p.id, received_on=date(2026, 3, 9))  # Feb (ended 02-28)
    assert res["period"] == "2026-02"


def test_mark_received_none_when_no_ended_period():
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed_monthly(s)
    # Before any period has ended → nothing to satisfy.
    assert svc.mark_received(s, p.id, received_on=date(2026, 1, 15)) is None


def test_mark_received_explicit_period():
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed_monthly(s)
    res = svc.mark_received(s, p.id, received_on=date(2026, 3, 1), period="2026-02")
    assert res["period"] == "2026-02"


# ---- overdue sweep + bell reminder ------------------------------------------

def _bells(s, program_id):
    import db
    return (s.query(db.ActivityEvent)
            .filter(db.ActivityEvent.action == "submission_overdue",
                    db.ActivityEvent.target == f"program:{program_id}").all())


def test_sweep_flips_past_due_to_overdue_and_rings_bell():
    import submission_calendar_service as svc, db
    s = _mem_session(); p = _seed_monthly(s)
    # Jan due 02-10 → overdue from 02-11 (no grace). Sweep on 02-20.
    res = svc.sweep_overdue(s, today=date(2026, 2, 20), tenant_id=1); s.commit()
    assert res["newly_late"] == 1
    assert res["rows"][0]["period"] == "2026-01"
    assert len(_bells(s, p.id)) == 1
    jan = s.query(db.ExpectedSubmission).filter_by(period="2026-01").first()
    assert jan.status == "overdue"


def test_sweep_is_overdue_the_very_next_day():
    # The boundary grace used to blur: 02-11 is one day past due and already
    # overdue, not "still in grace".
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed_monthly(s)
    res = svc.sweep_overdue(s, today=date(2026, 2, 11), tenant_id=1); s.commit()
    assert res["newly_late"] == 1
    assert len(_bells(s, p.id)) == 1


def test_sweep_is_idempotent_bell_fires_once():
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed_monthly(s)
    svc.sweep_overdue(s, today=date(2026, 2, 20), tenant_id=1); s.commit()
    second = svc.sweep_overdue(s, today=date(2026, 2, 21), tenant_id=1); s.commit()
    assert second["newly_late"] == 0           # already overdue — not re-flagged
    assert len(_bells(s, p.id)) == 1           # bell rang once, not twice


def test_sweep_ignores_received_periods():
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed_monthly(s)
    svc.mark_received(s, p.id, received_on=date(2026, 2, 8)); s.commit()  # Jan received
    # On 02-12, Jan is received; Feb (due 03-10) not yet due → nothing overdue.
    res = svc.sweep_overdue(s, today=date(2026, 2, 12), tenant_id=1); s.commit()
    assert res["newly_late"] == 0
    assert len(_bells(s, p.id)) == 0


def test_sweep_fires_even_if_materialize_ran_after_due():
    # Regression: materialize on a past-due date stamps status='overdue'; the
    # bell must still fire (guarded by overdue_notified, not status).
    import submission_calendar_service as svc, db
    s = _mem_session()
    p = db.Program(tenant_id=1, name="Acme", bdx_frequency="monthly"); s.add(p); s.flush()
    s.add(db.Contract(program_id=p.id, tenant_id=1,
                      extracted={"inception_dt": "2026-01-01"})); s.flush()
    sched = db.SubmissionSchedule(tenant_id=1, program_id=p.id); s.add(sched); s.flush()
    svc.materialize_schedule(s, sched, today=date(2026, 2, 20)); s.commit()  # Jan already 'overdue'
    res = svc.sweep_overdue(s, today=date(2026, 2, 20), tenant_id=1); s.commit()
    assert res["newly_late"] == 1
    assert len(_bells(s, p.id)) == 1


def test_bell_not_re_rung_after_rematerialize():
    import submission_calendar_service as svc, db
    s = _mem_session(); p = _seed_monthly(s)
    svc.sweep_overdue(s, today=date(2026, 2, 20), tenant_id=1); s.commit()
    # Re-materialize (e.g. schedule edited) must NOT reset the fired reminder.
    sched = s.query(db.SubmissionSchedule).filter_by(program_id=p.id).first()
    svc.materialize_schedule(s, sched, today=date(2026, 2, 21)); s.commit()
    again = svc.sweep_overdue(s, today=date(2026, 2, 21), tenant_id=1); s.commit()
    assert again["newly_late"] == 0
    assert len(_bells(s, p.id)) == 1


# ---- the three deadline reminders: before, on the day, after ----------------

def _soon_bells(s, program_id):
    import db
    return (s.query(db.ActivityEvent)
            .filter(db.ActivityEvent.action == "submission_due_soon",
                    db.ActivityEvent.target == f"program:{program_id}").all())


def _today_bells(s, program_id):
    import db
    return (s.query(db.ActivityEvent)
            .filter(db.ActivityEvent.action == "submission_due_today",
                    db.ActivityEvent.target == f"program:{program_id}").all())


def test_sweep_fires_due_soon_before_due():
    # Jan due 02-10, soon window 5 → [02-05, 02-09]. Sweep on 02-06 (in window,
    # before due, unreceived) rings ONE "due soon" reminder and nothing else.
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed_monthly(s)
    res = svc.sweep_overdue(s, today=date(2026, 2, 6), tenant_id=1); s.commit()
    assert res["newly_due_soon"] == 1
    assert res["newly_due_today"] == 0
    assert res["newly_late"] == 0
    assert len(_soon_bells(s, p.id)) == 1
    assert len(_bells(s, p.id)) == 0


def test_sweep_due_soon_idempotent():
    # The due-soon reminder fires once, not on every sweep inside the window.
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed_monthly(s)
    svc.sweep_overdue(s, today=date(2026, 2, 6), tenant_id=1); s.commit()
    second = svc.sweep_overdue(s, today=date(2026, 2, 7), tenant_id=1); s.commit()
    assert second["newly_due_soon"] == 0
    assert len(_soon_bells(s, p.id)) == 1


def test_sweep_fires_due_today_on_the_due_date():
    # 02-10 IS the deadline: its own bell, not "due soon" and not "overdue".
    import submission_calendar_service as svc, db
    s = _mem_session(); p = _seed_monthly(s)
    res = svc.sweep_overdue(s, today=date(2026, 2, 10), tenant_id=1); s.commit()
    assert res["newly_due_today"] == 1
    assert res["newly_due_soon"] == 0
    assert res["newly_late"] == 0
    assert len(_today_bells(s, p.id)) == 1
    jan = s.query(db.ExpectedSubmission).filter_by(period="2026-01").first()
    assert jan.status == "due_today"


def test_sweep_due_today_idempotent():
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed_monthly(s)
    svc.sweep_overdue(s, today=date(2026, 2, 10), tenant_id=1); s.commit()
    # Same day, second sweep (a page view triggers one) — the bell stays at one.
    second = svc.sweep_overdue(s, today=date(2026, 2, 10), tenant_id=1); s.commit()
    assert second["newly_due_today"] == 0
    assert len(_today_bells(s, p.id)) == 1


def test_all_three_reminders_fire_across_the_deadline():
    # Walk one period through the whole ladder: each moment rings exactly once,
    # and the earlier bells are not re-rung by the later sweeps.
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed_monthly(s)
    svc.sweep_overdue(s, today=date(2026, 2, 6), tenant_id=1); s.commit()   # soon
    svc.sweep_overdue(s, today=date(2026, 2, 10), tenant_id=1); s.commit()  # today
    svc.sweep_overdue(s, today=date(2026, 2, 11), tenant_id=1); s.commit()  # overdue
    assert len(_soon_bells(s, p.id)) == 1
    assert len(_today_bells(s, p.id)) == 1
    assert len(_bells(s, p.id)) == 1


def test_sweep_seen_late_rings_only_overdue():
    # A calendar first swept long after the due date must NOT retro-ring the
    # due-soon and due-today reminders — those moments have passed.
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed_monthly(s)
    res = svc.sweep_overdue(s, today=date(2026, 2, 25), tenant_id=1); s.commit()
    assert res["newly_late"] == 1
    assert res["newly_due_today"] == 0
    assert res["newly_due_soon"] == 0
    assert len(_soon_bells(s, p.id)) == 0
    assert len(_today_bells(s, p.id)) == 0
