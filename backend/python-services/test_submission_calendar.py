"""Tests for the pure submission-calendar logic (Group 3)."""
from datetime import date, datetime

import submission_calendar as sc
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
                     db.ActivityEvent.__table__,
                     # The calendar is now per (programme x broker) and keeps a
                     # version chain per period, so the service reads these too.
                     db.ProgramBroker.__table__, db.SubmissionVersion.__table__,
                     db.Party.__table__,
                     # The board resolves who to chase from the broker's own
                     # user accounts — there is no contact record anywhere else.
                     db.AppUser.__table__])
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


# ---- half-yearly and yearly (requirement 17.1) ------------------------------

def test_half_yearly_aliases_resolve():
    # These are the spellings the programme forms have been storing all along.
    for spelling in ("semi-annual", "Semi-Annual", "semiannual", "half-yearly",
                     "biannual", "six-monthly"):
        r = sc.resolve_schedule(frequency_override=spelling,
                                anchor_override=date(2026, 3, 15))
        assert r is not None and r.frequency == "half_yearly", spelling


def test_yearly_aliases_resolve():
    for spelling in ("annual", "Annually", "yearly", "YEAR"):
        r = sc.resolve_schedule(frequency_override=spelling,
                                anchor_override=date(2026, 3, 15))
        assert r is not None and r.frequency == "yearly", spelling


def test_half_yearly_periods_align_to_calendar_halves():
    # Anchored mid-H1, so the first period is the whole of H1 — not six months
    # counted from March. Halves snap to the calendar exactly as quarters do.
    r = sc.resolve_schedule(frequency_override="half_yearly",
                            anchor_override=date(2026, 3, 15), due_day_of_month=10)
    rows = sc.generate_expected(r, date(2026, 1, 1), date(2027, 6, 30))
    assert [x["period"] for x in rows] == ["2026-H1", "2026-H2", "2027-H1"]
    assert rows[0]["period_start"] == date(2026, 1, 1)
    assert rows[0]["period_end"] == date(2026, 6, 30)
    assert rows[0]["due_date"] == date(2026, 7, 10)
    assert rows[1]["period_end"] == date(2026, 12, 31)
    assert rows[1]["due_date"] == date(2027, 1, 10)


def test_yearly_periods_are_whole_calendar_years():
    r = sc.resolve_schedule(frequency_override="annual",
                            anchor_override=date(2026, 9, 1), due_day_of_month=15)
    rows = sc.generate_expected(r, date(2026, 1, 1), date(2028, 12, 31))
    assert [x["period"] for x in rows] == ["2026", "2027", "2028"]
    assert rows[0]["period_start"] == date(2026, 1, 1)
    assert rows[0]["period_end"] == date(2026, 12, 31)
    # A year ending 31 Dec is due on 15 Jan — the month AFTER the period.
    assert rows[0]["due_date"] == date(2027, 1, 15)


def test_horizon_scales_with_the_period_length():
    # Twelve months is four quarters but only one year, so a yearly programme
    # needs a longer horizon to show a comparable number of rows.
    assert sc.horizon_months_for("monthly") == 12
    assert sc.horizon_months_for("quarterly") == 12
    assert sc.horizon_months_for("semi-annual") == 24
    assert sc.horizon_months_for("annual") == 36
    assert sc.horizon_months_for(None) == 12


# ---- which period a file is FOR (requirement 17.2) --------------------------

def test_period_for_date_names_the_period_containing_a_date():
    monthly = sc.resolve_schedule(frequency_override="monthly",
                                  anchor_override=date(2026, 1, 1))
    quarterly = sc.resolve_schedule(frequency_override="quarterly",
                                    anchor_override=date(2026, 1, 1))
    half = sc.resolve_schedule(frequency_override="half_yearly",
                               anchor_override=date(2026, 1, 1))
    yearly = sc.resolve_schedule(frequency_override="yearly",
                                 anchor_override=date(2026, 1, 1))
    d = date(2026, 7, 20)
    # The SAME date, read under four different frequencies.
    assert sc.period_for_date(monthly, d) == "2026-07"
    assert sc.period_for_date(quarterly, d) == "2026-Q3"
    assert sc.period_for_date(half, d) == "2026-H2"
    assert sc.period_for_date(yearly, d) == "2026"


def test_period_for_date_is_none_before_the_calendar_starts():
    r = sc.resolve_schedule(frequency_override="monthly",
                            anchor_override=date(2026, 6, 1))
    assert sc.period_for_date(r, date(2026, 3, 4)) is None
    assert sc.period_for_date(None, date(2026, 7, 4)) is None


def test_parse_period_hint_reads_the_shapes_brokers_actually_use():
    cases = {
        "SpectrumBDX_2026-07.xlsx": date(2026, 7, 1),
        "corvin July2026 bdx.xlsx": date(2026, 7, 1),
        "bdx_2026Q3.csv": date(2026, 7, 1),
        "Halstead-202607.xlsx": date(2026, 7, 1),
        "risk_2026-H2.xlsx": date(2026, 7, 1),
        "claims_07-2026.xlsx": date(2026, 7, 1),
    }
    for name, expected in cases.items():
        assert sc.parse_period_hint(name) == expected, name


def test_parse_period_hint_returns_none_rather_than_guessing():
    # A wrong period is worse than no period: it marks the wrong month
    # delivered. Anything unreadable must fall through to the caller's rule.
    for name in ("bordereau.xlsx", "", None, "final_v2.xlsx", "Q5-2026.xlsx"):
        assert sc.parse_period_hint(name) is None, name


# ---- one obligation per (programme x broker) -------------------------------

def _seed_with_brokers(s, n=2, freq="monthly"):
    import db, submission_calendar_service as svc
    p = db.Program(tenant_id=1, name="Acme", bdx_frequency=freq); s.add(p); s.flush()
    c = db.Contract(program_id=p.id, tenant_id=1,
                    extracted={"inception_dt": "2026-01-01"})
    s.add(c); s.flush()
    brokers = []
    for i in range(n):
        b = db.Party(tenant_id=1, party_type="broker", legal_name=f"Broker {i+1}")
        s.add(b); s.flush()
        s.add(db.ProgramBroker(tenant_id=1, program_id=p.id, broker_party_id=b.id))
        brokers.append(b)
    s.flush()
    sched = db.SubmissionSchedule(tenant_id=1, program_id=p.id); s.add(sched); s.flush()
    svc.materialize_schedule(s, sched, today=date(2026, 2, 6), horizon_months=3)
    s.commit()
    return p, brokers, sched


def test_materialize_fans_periods_out_across_brokers():
    import db
    s = _mem_session()
    p, brokers, _ = _seed_with_brokers(s, n=3)
    rows = s.query(db.ExpectedSubmission).all()
    periods = {r.period for r in rows}
    # Every broker owes every period, and nobody owes it twice.
    assert len(rows) == len(periods) * 3
    for b in brokers:
        assert {r.period for r in rows if r.broker_party_id == b.id} == periods


def test_a_programme_with_no_broker_keeps_one_unattributed_row():
    import db
    s = _mem_session()
    p = _seed_monthly(s)
    rows = s.query(db.ExpectedSubmission).all()
    assert rows and all(r.broker_party_id is None for r in rows)


def test_a_first_broker_adopts_the_pre_broker_rows():
    # The arrival history on a pre-broker row must survive being attributed —
    # creating fresh broker rows beside it would strand it and show the period
    # twice.
    import db, submission_calendar_service as svc
    s = _mem_session()
    p = _seed_monthly(s)
    svc.mark_received(s, p.id, received_on=date(2026, 2, 8), export_id=42)
    s.commit()
    before = s.query(db.ExpectedSubmission).filter(
        db.ExpectedSubmission.received_at.isnot(None)).one()
    period, keep_id = before.period, before.id

    b = db.Party(tenant_id=1, party_type="broker", legal_name="Corvin")
    s.add(b); s.flush()
    s.add(db.ProgramBroker(tenant_id=1, program_id=p.id, broker_party_id=b.id))
    sched = s.query(db.SubmissionSchedule).one()
    svc.materialize_schedule(s, sched, today=date(2026, 2, 6), horizon_months=3)
    s.commit()

    same_period = s.query(db.ExpectedSubmission).filter(
        db.ExpectedSubmission.period == period).all()
    assert len(same_period) == 1                    # not duplicated
    assert same_period[0].id == keep_id             # the same row, re-keyed
    assert same_period[0].broker_party_id == b.id
    assert same_period[0].received_at == date(2026, 2, 8)   # history intact


# ---- versions: original, corrected, released -------------------------------

def test_a_second_file_for_a_period_is_a_corrected_version():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p = _seed_monthly(s)
    first = svc.mark_received(s, p.id, received_on=date(2026, 2, 8), export_id=1)
    assert first["version_no"] == 1 and first["kind"] == "original"
    again = svc.mark_received(s, p.id, received_on=date(2026, 3, 2),
                              export_id=2, period=first["period"])
    assert again["version_no"] == 2 and again["kind"] == "corrected"
    e = s.get(db.ExpectedSubmission, first["expected_id"])
    assert e.version_count == 2
    assert svc._version_label(e) == "Corrected once"


def test_a_correction_never_repaints_a_missed_deadline():
    # The verdict belongs to the file that met or missed the date. A correction
    # sent three weeks later must not turn "late" into "on time".
    import db, submission_calendar_service as svc
    s = _mem_session()
    p = _seed_monthly(s)
    late = svc.mark_received(s, p.id, received_on=date(2026, 2, 20), export_id=1)
    assert late["status"] == "received_late"
    svc.mark_received(s, p.id, received_on=date(2026, 3, 1), export_id=2,
                      period=late["period"])
    e = s.get(db.ExpectedSubmission, late["expected_id"])
    assert e.status == "received_late"
    assert e.received_at == date(2026, 2, 20)      # pinned to version 1
    assert e.latest_received_at == date(2026, 3, 1)  # but we know about the newer one


def test_the_original_survives_a_correction():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p = _seed_monthly(s)
    r = svc.mark_received(s, p.id, received_on=date(2026, 2, 8), export_id=1)
    svc.mark_received(s, p.id, received_on=date(2026, 3, 2), export_id=2,
                      period=r["period"])
    chain = svc.submission_versions(s, r["expected_id"])
    assert [v["version_no"] for v in chain] == [1, 2]
    assert [v["kind"] for v in chain] == ["original", "corrected"]
    assert chain[0]["received_at"] == "2026-02-08"   # untouched


def test_re_rendering_the_same_export_is_not_a_correction():
    import submission_calendar_service as svc
    s = _mem_session()
    p = _seed_monthly(s)
    r = svc.mark_received(s, p.id, received_on=date(2026, 2, 8), export_id=7)
    assert r is not None
    again = svc.mark_received(s, p.id, received_on=date(2026, 2, 9), export_id=7,
                              period=r["period"])
    assert again is None
    assert len(svc.submission_versions(s, r["expected_id"])) == 1


def test_a_late_file_belongs_to_its_own_period_not_the_month_it_arrived():
    import submission_calendar_service as svc
    s = _mem_session()
    p = _seed_monthly(s)   # monthly from 2026-01, three months materialized
    # January's file, sent in March. Read off the filename, it satisfies
    # January — not whichever period happens to be open in March.
    r = svc.mark_received(s, p.id, received_on=date(2026, 3, 20), export_id=9,
                          source_filename="acme_bdx_2026-01.xlsx")
    assert r["period"] == "2026-01"
    assert r["period_source"] == "filename"
    assert r["status"] == "received_late"


def test_without_a_readable_period_it_falls_back_and_says_so():
    import submission_calendar_service as svc
    s = _mem_session()
    p = _seed_monthly(s)
    r = svc.mark_received(s, p.id, received_on=date(2026, 2, 8), export_id=9,
                          source_filename="bordereau.xlsx")
    assert r["period_source"] == "oldest_open"


def test_release_is_recorded_against_the_version_that_was_sent():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p = _seed_monthly(s)
    r = svc.mark_received(s, p.id, received_on=date(2026, 2, 8), export_id=1)
    out = svc.record_release(s, r["expected_id"], released_on=date(2026, 2, 11),
                             released_to="Munich Re", released_by="ops@carrier.com")
    assert out["version_no"] == 1 and out["released_to"] == "Munich Re"
    e = s.get(db.ExpectedSubmission, r["expected_id"])
    assert e.released_at == date(2026, 2, 11) and e.released_count == 1
    # A correction is then sent on separately — the reinsurer got two files, and
    # which one they got when has to stay answerable.
    svc.mark_received(s, p.id, received_on=date(2026, 3, 1), export_id=2,
                      period=r["period"])
    svc.record_release(s, r["expected_id"], released_on=date(2026, 3, 3),
                       released_to="Munich Re")
    chain = svc.submission_versions(s, r["expected_id"])
    assert chain[0]["released_at"] == "2026-02-11"
    assert chain[1]["released_at"] == "2026-03-03"
    assert s.get(db.ExpectedSubmission, r["expected_id"]).released_count == 2


def test_nothing_can_be_sent_on_before_it_arrives():
    import db, submission_calendar_service as svc
    s = _mem_session()
    _seed_monthly(s)
    e = s.query(db.ExpectedSubmission).first()
    assert svc.record_release(s, e.id, released_on=date(2026, 2, 11)) is None


# ---- chasing ---------------------------------------------------------------

def test_chase_stamps_only_the_rows_that_are_actually_late():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _brokers, _sched = _seed_with_brokers(s, n=1)
    rows = s.query(db.ExpectedSubmission).order_by(
        db.ExpectedSubmission.due_date.asc()).all()
    today = date(2026, 3, 20)
    late = [e for e in rows if e.due_date < today]
    future = [e for e in rows if e.due_date >= today]
    res = svc.record_chase(s, [e.id for e in rows], actor="ops@carrier.com",
                           today=today)
    assert res["chased"] == len(late)
    assert all(e.chase_count == 1 for e in late)
    assert all((e.chase_count or 0) == 0 for e in future)
    assert s.query(db.ActivityEvent).filter(
        db.ActivityEvent.action == "submission_chased").count() == len(late)


# ---- the carrier's board ---------------------------------------------------

def test_board_groups_by_due_month_and_counts_only_real_obligations():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, brokers, sched = _seed_with_brokers(s, n=2)
    # January's file is due 10 Feb for both brokers. One sends on time, the
    # other never does.
    svc.mark_received(s, p.id, received_on=date(2026, 2, 5), export_id=1,
                      period="2026-01", broker_party_id=brokers[0].id)
    s.commit()
    board = svc.calendar_board(s, 1, month="2026-02", today=date(2026, 3, 1))
    assert board["month"] == "2026-02"
    assert board["counts"]["due"] == 2
    assert board["counts"]["on_time"] == 1
    assert board["counts"]["never"] == 1
    assert board["schedules"][0]["frequency"] == "monthly"
    assert board["schedules"][0]["due_rule"] == "10th of the following month"


def test_board_shows_a_programme_with_no_broker_as_owing_nothing():
    import submission_calendar_service as svc
    s = _mem_session()
    _seed_monthly(s)
    board = svc.calendar_board(s, 1, month="2026-02", today=date(2026, 3, 1))
    assert board["rows"] and all(r["unassigned"] for r in board["rows"])
    # Nobody can be late when there is nobody to be late.
    assert board["counts"] == {"due": 0, "on_time": 0, "late": 0,
                               "never": 0, "released": 0, "unsent_correction": 0}


def test_changing_the_frequency_clears_the_old_periods():
    # Switching yearly to monthly used to leave "2026" sitting in the calendar
    # as an obligation nobody owed under the new frequency.
    import db, submission_calendar_service as svc
    s = _mem_session()
    p = db.Program(tenant_id=1, name="Cat layer", bdx_frequency="annual")
    s.add(p); s.flush()
    s.add(db.Contract(program_id=p.id, tenant_id=1, inception_dt=date(2026, 1, 1)))
    s.flush()
    sched = db.SubmissionSchedule(tenant_id=1, program_id=p.id); s.add(sched); s.flush()
    svc.materialize_schedule(s, sched, today=date(2026, 6, 1))
    s.commit()
    assert all(len(e.period) == 4 for e in s.query(db.ExpectedSubmission).all())

    sched.frequency_override = "monthly"
    svc.materialize_schedule(s, sched, today=date(2026, 6, 1))
    s.commit()
    periods = {e.period for e in s.query(db.ExpectedSubmission).all()}
    assert "2026" not in periods
    assert "2026-06" in periods


def test_a_period_that_was_filed_survives_a_frequency_change():
    # History does not stop being true because the reporting frequency changed
    # afterwards, so a period with an arrival is never swept up.
    import db, submission_calendar_service as svc
    s = _mem_session()
    p = _seed_monthly(s)
    r = svc.mark_received(s, p.id, received_on=date(2026, 2, 8), export_id=1)
    sched = s.query(db.SubmissionSchedule).one()
    sched.frequency_override = "quarterly"
    svc.materialize_schedule(s, sched, today=date(2026, 2, 6), horizon_months=6)
    s.commit()
    kept = s.query(db.ExpectedSubmission).filter(
        db.ExpectedSubmission.period == r["period"]).one()
    assert kept.received_at == date(2026, 2, 8)


def test_a_correction_sent_after_a_release_shows_as_not_yet_sent():
    # The recipient is holding a version we have since superseded. The period's
    # released_at still has a date on it, so the flag is the only thing that can
    # say the newest file has not gone.
    import submission_calendar_service as svc
    s = _mem_session()
    p = _seed_monthly(s)
    r = svc.mark_received(s, p.id, received_on=date(2026, 2, 8), export_id=1)
    svc.record_release(s, r["expected_id"], released_on=date(2026, 2, 11),
                       released_to="Munich Re")
    svc.mark_received(s, p.id, received_on=date(2026, 3, 1), export_id=2,
                      period=r["period"])
    s.commit()
    rows = {x["id"]: x for x in svc.calendar_rows(s, 1, today=date(2026, 3, 5))}
    row = rows[r["expected_id"]]
    assert row["released_at"] == "2026-02-11"       # something WAS sent
    assert row["latest_version_released"] is False  # but not the newest file


def test_a_pre_broker_calendar_heals_itself_on_view():
    # The exact state a live database is in after upgrading: rows exist, brokers
    # exist, but nothing attributed them to each other — so the board read
    # "nothing is owed" against a programme that plainly owed something.
    import db, submission_calendar_service as svc
    s = _mem_session()
    p = _seed_monthly(s)                       # builds NULL-broker rows
    assert all(e.broker_party_id is None
               for e in s.query(db.ExpectedSubmission).all())

    for nm in ("CRC Insurisk", "Halstead & Co"):
        b = db.Party(tenant_id=1, party_type="broker", legal_name=nm)
        s.add(b); s.flush()
        s.add(db.ProgramBroker(tenant_id=1, program_id=p.id, broker_party_id=b.id))
    s.commit()

    # Before healing the board says nobody owes anything.
    board = svc.calendar_board(s, 1, month="2026-02", today=date(2026, 3, 1))
    assert board["counts"]["due"] == 0
    assert all(r["unassigned"] for r in board["rows"])

    res = svc.heal_calendars(s, tenant_id=1, today=date(2026, 3, 1))
    s.commit()
    assert res["healed"] == 1

    board = svc.calendar_board(s, 1, month="2026-02", today=date(2026, 3, 1))
    assert board["counts"]["due"] == 2                     # both brokers owe it
    assert not any(r["unassigned"] for r in board["rows"])


def test_healing_is_a_no_op_once_done_and_when_there_are_no_brokers():
    import submission_calendar_service as svc
    s = _mem_session()
    _seed_monthly(s)                            # no brokers on this programme
    # Nothing to attribute it TO — the unattributed row is the right answer.
    assert svc.heal_calendars(s, tenant_id=1)["healed"] == 0

    s2 = _mem_session()
    _seed_with_brokers(s2, n=2)                 # already fanned out at creation
    assert svc.heal_calendars(s2, tenant_id=1)["healed"] == 0


def test_the_board_says_who_would_be_chased():
    # There is no contact record for a broker anywhere in the platform, so the
    # only real answer is the broker's own user accounts — admin first.
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, brokers, _ = _seed_with_brokers(s, n=2)
    s.add(db.AppUser(email="ops@corvin.com", full_name="Ines Duarte",
                     role="operator", broker_party_id=brokers[0].id))
    s.add(db.AppUser(email="boss@corvin.com", full_name="Ana Reis",
                     role="broker_admin", broker_party_id=brokers[0].id))
    s.commit()

    board = svc.calendar_board(s, 1, month="2026-02", today=date(2026, 3, 1))
    by_broker = {r["broker_party_id"]: r for r in board["rows"]}
    # The admin is offered first: chasing is a management conversation.
    assert [c["email"] for c in by_broker[brokers[0].id]["contacts"]] \
        == ["boss@corvin.com", "ops@corvin.com"]
    # A broker with no account at all comes back empty, so the screen can say
    # "we do not know who to tell" rather than inventing somebody.
    assert by_broker[brokers[1].id]["contacts"] == []


def test_a_chase_keeps_what_the_operator_said():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _brokers, _sched = _seed_with_brokers(s, n=1)
    rows = s.query(db.ExpectedSubmission).all()
    svc.record_chase(s, [e.id for e in rows], actor="ops@carrier.com",
                     note="we need this before month end", today=date(2026, 3, 20))
    s.commit()
    ev = s.query(db.ActivityEvent).filter(
        db.ActivityEvent.action == "submission_chased").first()
    assert ev.details["note"] == "we need this before month end"


def test_a_chase_with_nothing_to_add_stores_no_note():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _brokers, _sched = _seed_with_brokers(s, n=1)
    rows = s.query(db.ExpectedSubmission).all()
    svc.record_chase(s, [e.id for e in rows], actor="ops@carrier.com",
                     today=date(2026, 3, 20))
    s.commit()
    ev = s.query(db.ActivityEvent).filter(
        db.ActivityEvent.action == "submission_chased").first()
    assert "note" not in ev.details


def test_an_invited_contact_still_counts_but_is_labelled():
    # They are the address the carrier chose when they set the broker up; they
    # just have not signed in. Dropping them made the dialog say "nobody on
    # record" for a broker that has a perfectly good contact.
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, brokers, _ = _seed_with_brokers(s, n=1)
    s.add(db.AppUser(email="invited@corvin.com", full_name="Not Yet",
                     role="broker_admin", status="invited",
                     broker_party_id=brokers[0].id))
    s.add(db.AppUser(email="live@corvin.com", full_name="Signed In",
                     role="broker_admin", status="active",
                     broker_party_id=brokers[0].id))
    s.add(db.AppUser(email="gone@corvin.com", full_name="Removed",
                     role="broker_admin", status="disabled",
                     broker_party_id=brokers[0].id))
    s.commit()
    got = svc.broker_contacts(s, [brokers[0].id])[brokers[0].id]
    # Active first, invited second, disabled not at all.
    assert [c["email"] for c in got] == ["live@corvin.com", "invited@corvin.com"]
    assert [c["status"] for c in got] == ["active", "invited"]


def test_a_period_with_no_broker_cannot_be_chased():
    # The screen calls these rows "Nothing is owed" — there is nobody to chase.
    # Stamping one would record a conversation that cannot have happened.
    import db, submission_calendar_service as svc
    s = _mem_session()
    _seed_monthly(s)                       # no brokers on this programme
    rows = s.query(db.ExpectedSubmission).all()
    res = svc.record_chase(s, [e.id for e in rows], actor="ops@carrier.com",
                           today=date(2026, 3, 20))
    assert res["chased"] == 0
    assert s.query(db.ActivityEvent).filter(
        db.ActivityEvent.action == "submission_chased").count() == 0


# ---- the calendar stops where the contract does ----------------------------

def _seed_with_term(s, inception, expiries, freq="monthly"):
    """A programme whose contract(s) state a term. `expiries` may hold several,
    which is what a renewal looks like."""
    import db, submission_calendar_service as svc
    p = db.Program(tenant_id=1, name="Termed", bdx_frequency=freq)
    s.add(p); s.flush()
    for exp in expiries:
        s.add(db.Contract(program_id=p.id, tenant_id=1,
                          inception_dt=inception, expiry_dt=exp))
    s.flush()
    sched = db.SubmissionSchedule(tenant_id=1, program_id=p.id, due_day_of_month=10)
    s.add(sched); s.flush()
    return p, sched


def test_generation_stops_at_the_contract_expiry():
    # The rolling horizon would have run to Feb 2027. The contract ends in July
    # 2026, and past that the broker owes nothing under it.
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, sched = _seed_with_term(s, date(2026, 1, 1), [date(2026, 7, 1)])
    res = svc.materialize_schedule(s, sched, today=date(2026, 2, 6))
    s.commit()
    assert res["bounded_by"] == "contract"
    assert res["covers_until"] == "2026-07-01"
    periods = sorted({e.period for e in s.query(db.ExpectedSubmission).all()})
    # A period is kept only if it STARTS before the expiry: a contract ending
    # 1 Jul covers June in full and July not at all.
    assert periods == ["2026-01", "2026-02", "2026-03",
                       "2026-04", "2026-05", "2026-06"]
    last = s.query(db.ExpectedSubmission).filter(
        db.ExpectedSubmission.period == "2026-06").one()
    assert last.due_date == date(2026, 7, 10)


def test_a_renewal_extends_the_calendar():
    # Renewals are why the bound is the LATEST expiry across every contract on
    # the programme, not whichever row happens to be newest by id.
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, sched = _seed_with_term(s, date(2026, 1, 1),
                               [date(2026, 7, 1), date(2027, 1, 1)])
    res = svc.materialize_schedule(s, sched, today=date(2026, 2, 6))
    s.commit()
    assert res["covers_until"] == "2027-01-01"
    periods = sorted({e.period for e in s.query(db.ExpectedSubmission).all()})
    assert periods[-1] == "2026-12"
    assert len(periods) == 12


def test_no_expiry_falls_back_to_the_rolling_horizon():
    # _seed_monthly's contract states an inception and no expiry at all.
    import db, submission_calendar_service as svc
    s = _mem_session()
    p = _seed_monthly(s)
    sched = s.query(db.SubmissionSchedule).one()
    res = svc.materialize_schedule(s, sched, today=date(2026, 2, 6))
    s.commit()
    assert res["bounded_by"] == "horizon"
    assert res["covers_until"] is None


def test_an_expiry_beyond_the_horizon_does_not_extend_generation():
    # The horizon still caps a very long contract — otherwise a ten-year treaty
    # would materialise a hundred and twenty periods nobody asked for.
    import submission_calendar_service as svc
    s = _mem_session()
    p, sched = _seed_with_term(s, date(2026, 1, 1), [date(2036, 1, 1)])
    res = svc.materialize_schedule(s, sched, today=date(2026, 2, 6))
    s.commit()
    assert res["bounded_by"] == "horizon"
    assert res["covers_until"] == "2036-01-01"      # known, just not binding


def test_an_expiry_before_its_own_inception_is_ignored():
    # Bad data. Honouring it would empty the calendar — and the stale-period
    # sweep would then delete every unfiled period along with it.
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, sched = _seed_with_term(s, date(2026, 1, 1), [date(2025, 1, 1)])
    res = svc.materialize_schedule(s, sched, today=date(2026, 2, 6))
    s.commit()
    assert res["bounded_by"] == "horizon"
    assert s.query(db.ExpectedSubmission).count() > 0


def test_shrinking_to_the_contract_never_deletes_a_filed_period():
    # A file that was actually sent is history. It does not stop being true
    # because the calendar was later bounded more tightly.
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, sched = _seed_with_term(s, date(2026, 1, 1), [date(2027, 1, 1)])
    svc.materialize_schedule(s, sched, today=date(2026, 2, 6))
    s.commit()
    got = svc.mark_received(s, p.id, received_on=date(2026, 11, 5),
                            export_id=1, period="2026-10")
    assert got is not None
    s.commit()

    # The contract turns out to end in July instead — 2026-10 is now outside it.
    for csub in s.query(db.Contract).all():
        csub.expiry_dt = date(2026, 7, 1)
    svc.materialize_schedule(s, sched, today=date(2026, 2, 6))
    s.commit()
    kept = s.query(db.ExpectedSubmission).filter(
        db.ExpectedSubmission.period == "2026-10").one()
    assert kept.received_at == date(2026, 11, 5)


def test_an_over_long_calendar_shrinks_itself_on_view():
    # The state every existing database is in: deadlines generated on the old
    # rolling horizon, running past a contract term the calendar did not yet
    # know about. Nothing re-runs materialize once a programme is attributed, so
    # without this the phantom periods would sit there until somebody happened
    # to re-save the schedule.
    import db, submission_calendar_service as svc
    s = _mem_session()
    # Built while the contract stated no end date at all — so the rolling
    # horizon applied, exactly as it did before this bound existed.
    p, sched = _seed_with_term(s, date(2026, 1, 1), [None])
    svc.materialize_schedule(s, sched, today=date(2026, 2, 6))
    s.commit()
    periods = sorted({e.period for e in s.query(db.ExpectedSubmission).all()})
    assert periods[-1] == "2027-02"

    # The term is filled in later — a re-extraction, or somebody correcting it.
    s.query(db.Contract).one().expiry_dt = date(2026, 7, 1)
    s.commit()

    res = svc.heal_calendars(s, tenant_id=1, today=date(2026, 2, 6))
    s.commit()
    assert res["healed"] == 1
    assert res["programs"][0]["reason"] == "past_contract_end"
    assert res["programs"][0]["bounded_by"] == "contract"
    periods = sorted({e.period for e in s.query(db.ExpectedSubmission).all()})
    assert periods[-1] == "2026-06"

    # And it settles: a second pass finds nothing left to do.
    assert svc.heal_calendars(s, tenant_id=1, today=date(2026, 2, 6))["healed"] == 0
