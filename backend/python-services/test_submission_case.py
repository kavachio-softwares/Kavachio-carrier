"""Tests for MOPR 39.2 — the case on a missing bordereau (submission_case.py).

The fixture is one monthly programme with one broker: January's file is due
10 Feb, so with the default 15 days it is Overdue from 11 Feb and Delinquent
from 25 Feb.
"""
from datetime import date

import pytest

import submission_case as sc

BROKER = {"actor": "broker:1", "user_id": 50, "role": "broker_admin", "broker_party_id": None}
CARRIER = {"actor": "carrier@example.com", "user_id": 7, "role": "carrier_admin",
           "broker_party_id": None}


def _mem_session(url: str = "sqlite:///:memory:"):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db
    eng = create_engine(url)
    db.Base.metadata.create_all(
        eng, tables=[db.Program.__table__, db.Contract.__table__,
                     db.SubmissionSchedule.__table__, db.ExpectedSubmission.__table__,
                     db.ActivityEvent.__table__, db.ProgramBroker.__table__,
                     db.SubmissionVersion.__table__, db.Party.__table__,
                     db.AppUser.__table__, db.OutputExport.__table__,
                     db.LandingRecord.__table__, db.Pipeline.__table__,
                     db.SubmissionCaseEvent.__table__])
    return sessionmaker(bind=eng)()


def _seed(s, *, brokers=1, delinquent_after=None):
    import db, submission_calendar_service as svc
    p = db.Program(tenant_id=1, name="Acme", bdx_frequency="monthly"); s.add(p); s.flush()
    s.add(db.Contract(program_id=p.id, tenant_id=1, extracted={"inception_dt": "2026-01-01"}))
    for i in range(brokers):
        b = db.Party(tenant_id=1, party_type="broker", legal_name=f"Broker {i + 1}")
        s.add(b); s.flush()
        s.add(db.ProgramBroker(tenant_id=1, program_id=p.id, broker_party_id=b.id))
    sched = db.SubmissionSchedule(tenant_id=1, program_id=p.id,
                                  delinquent_after_days=delinquent_after)
    s.add(sched); s.flush()
    svc.materialize_schedule(s, sched, today=date(2026, 2, 6), horizon_months=3)
    s.commit()
    return p


def _jan(s):
    import db
    return s.query(db.ExpectedSubmission).filter_by(period="2026-01").first()


def _sweep(s, day):
    out = sc.sweep_cases(s, today=day, tenant_id=1)
    s.commit()
    return out


def _actions(s, action):
    import db
    return s.query(db.ActivityEvent).filter(db.ActivityEvent.action == action).all()


def _kinds(s, e):
    return [h["kind"] for h in sc.history(s, e.id)]


# ---- the automatic half -----------------------------------------------------

def test_no_case_until_the_day_after_the_due_date():
    s = _mem_session(); _seed(s)
    _sweep(s, date(2026, 2, 10))
    assert _jan(s).case_state is None
    _sweep(s, date(2026, 2, 11))
    e = _jan(s)
    assert e.case_state == sc.OVERDUE
    assert e.case_since == date(2026, 2, 11)
    assert _kinds(s, e) == ["opened"]


def test_delinquent_on_the_15th_day_late_and_only_once():
    s = _mem_session(); _seed(s)
    _sweep(s, date(2026, 2, 24))
    assert _jan(s).case_state == sc.OVERDUE
    _sweep(s, date(2026, 2, 25))
    e = _jan(s)
    assert e.case_state == sc.DELINQUENT
    assert e.case_since == date(2026, 2, 25)
    _sweep(s, date(2026, 2, 26))
    assert _kinds(s, e) == ["opened", "delinquent"]
    assert len(_actions(s, "submission_delinquent")) == 1


def test_delinquent_days_follow_the_programme_setting():
    s = _mem_session(); _seed(s, delinquent_after=5)
    _sweep(s, date(2026, 2, 14))
    assert _jan(s).case_state == sc.OVERDUE
    _sweep(s, date(2026, 2, 15))
    assert _jan(s).case_state == sc.DELINQUENT


def test_a_backlog_is_recorded_with_its_real_dates_but_rings_no_bell():
    # The first check after switching this on finds January long past both
    # moves: both are recorded on the days they really happened, quietly.
    s = _mem_session(); _seed(s)
    _sweep(s, date(2026, 4, 1))
    e = _jan(s)
    assert e.case_state == sc.DELINQUENT
    hist = sc.history(s, e.id)
    assert [(h["kind"], h["on"]) for h in hist] == [
        ("opened", "2026-02-11"), ("delinquent", "2026-02-25")]
    assert _actions(s, "submission_delinquent") == []


def test_a_period_no_broker_owes_never_gets_a_case():
    s = _mem_session(); _seed(s, brokers=0)
    _sweep(s, date(2026, 4, 1))
    assert _jan(s).case_state is None


def test_a_file_on_time_opens_no_case():
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed(s)
    svc.mark_received(s, p.id, received_on=date(2026, 2, 8), period="2026-01",
                      broker_party_id=_jan(s).broker_party_id)
    s.commit()
    _sweep(s, date(2026, 3, 1))
    assert _jan(s).case_state is None


def test_moving_the_due_date_past_today_clears_an_open_case():
    s = _mem_session(); _seed(s)
    _sweep(s, date(2026, 2, 12))
    e = _jan(s)
    e.due_date = date(2026, 2, 20)          # the schedule was changed
    s.commit()
    _sweep(s, date(2026, 2, 12))
    assert _jan(s).case_state is None
    assert _kinds(s, e)[-1] == "cleared"


def test_the_deadline_sweep_moves_cases_too():
    import submission_calendar_service as svc
    s = _mem_session(); _seed(s)
    out = svc.sweep_overdue(s, today=date(2026, 2, 25), tenant_id=1)
    s.commit()
    assert out["cases"] == {"opened": 1, "delinquent": 1}
    assert _jan(s).case_state == sc.DELINQUENT


# ---- disputes ---------------------------------------------------------------

def test_a_broker_dispute_pauses_the_case():
    s = _mem_session(); _seed(s)
    e = _jan(s)
    sc.raise_dispute(s, e, who=BROKER, reason="nil_return", note="No business in January",
                     today=date(2026, 2, 20))
    s.commit()
    assert e.case_state == sc.IN_DISPUTE
    _sweep(s, date(2026, 3, 1))              # would be Delinquent, but it is in dispute
    assert _jan(s).case_state == sc.IN_DISPUTE
    assert len(_actions(s, "submission_dispute_raised")) == 1
    # No "delinquent" — only the one reminder that the carrier has to decide.
    assert _kinds(s, e) == ["opened", "dispute_raised", "carrier_reminded"]


def test_the_carrier_is_reminded_once_after_five_days():
    s = _mem_session(); _seed(s)
    e = _jan(s)
    sc.raise_dispute(s, e, who=BROKER, reason="already_sent", note=None,
                     today=date(2026, 2, 20))
    s.commit()
    _sweep(s, date(2026, 2, 24))
    assert _actions(s, "submission_dispute_waiting") == []
    _sweep(s, date(2026, 2, 25))
    _sweep(s, date(2026, 2, 26))
    assert len(_actions(s, "submission_dispute_waiting")) == 1
    assert _jan(s).case_state == sc.IN_DISPUTE   # still the carrier's to decide


def test_accepting_a_dispute_resolves_it():
    s = _mem_session(); _seed(s)
    e = _jan(s)
    sc.raise_dispute(s, e, who=BROKER, reason="deadline_wrong", note="Due on the 25th",
                     today=date(2026, 2, 20))
    sc.decide(s, e, who=CARRIER, decision="accept", note="Contract says the 25th",
              today=date(2026, 2, 22))
    s.commit()
    assert e.case_state == sc.RESOLVED
    assert e.case_resolution == sc.ACCEPTED
    assert len(_actions(s, "submission_dispute_accepted")) == 1


def test_rejecting_sends_it_back_and_the_broker_cannot_dispute_again():
    s = _mem_session(); _seed(s)
    e = _jan(s)
    sc.raise_dispute(s, e, who=BROKER, reason="nil_return", note=None, today=date(2026, 2, 20))
    sc.decide(s, e, who=CARRIER, decision="reject", note="Policies were written",
              today=date(2026, 3, 1))        # 19 days late → straight back to Delinquent
    s.commit()
    assert e.case_state == sc.DELINQUENT
    assert e.dispute_locked is True
    with pytest.raises(sc.CaseError) as err:
        sc.raise_dispute(s, e, who=BROKER, reason="nil_return", note=None,
                         today=date(2026, 3, 2))
    assert err.value.code == "locked"
    # The carrier can still reopen it.
    sc.raise_dispute(s, e, who=CARRIER, reason="other", note="Broker phoned with new facts",
                     by_carrier=True, today=date(2026, 3, 2))
    assert e.case_state == sc.IN_DISPUTE
    assert len(_actions(s, "submission_dispute_entered")) == 1


def test_a_rejection_before_15_days_goes_back_to_overdue():
    s = _mem_session(); _seed(s)
    e = _jan(s)
    sc.raise_dispute(s, e, who=BROKER, reason="nil_return", note=None, today=date(2026, 2, 12))
    sc.decide(s, e, who=CARRIER, decision="reject", note="No", today=date(2026, 2, 14))
    assert e.case_state == sc.OVERDUE


# ---- waive and more time ----------------------------------------------------

def test_waiving_closes_the_case_but_the_file_is_still_missing():
    import submission_calendar_service as svc
    s = _mem_session(); _seed(s)
    e = _jan(s)
    sc.decide(s, e, who=CARRIER, decision="waive", note="Nil return confirmed",
              today=date(2026, 2, 20))
    s.commit()
    assert e.case_state == sc.RESOLVED and e.case_resolution == sc.WAIVED
    row = next(r for r in svc.calendar_board(s, 1, month="2026-02",
                                             today=date(2026, 2, 20))["rows"]
               if r["period"] == "2026-01")
    assert row["status"] == "overdue"                    # 39.1: no file came
    assert row["case"]["resolution"] == sc.WAIVED        # 39.2: nothing is owed


def test_the_day_count_stops_on_the_day_it_was_settled():
    import submission_calendar_service as svc
    s = _mem_session(); _seed(s)
    e = _jan(s)                                          # due 10 Feb
    assert sc.days_over(e, date(2026, 2, 20)) == 10      # still owed: counts on
    sc.raise_dispute(s, e, who=BROKER, reason="nil_return", note="Nothing written",
                     today=date(2026, 2, 20))
    sc.decide(s, e, who=CARRIER, decision="accept", note="Confirmed",
              today=date(2026, 2, 22))
    s.commit()
    # Settled 12 days over, and it stays 12 — a month on, and on the board.
    assert sc.days_over(e, date(2026, 3, 30)) == 12
    row = next(r for r in svc.calendar_board(s, 1, month="2026-02",
                                             today=date(2026, 3, 30))["rows"]
               if r["period"] == "2026-01")
    assert row["days_over"] == 12

    # How it closed is the event's own name — not repeated underneath it.
    accepted = [h for h in sc.history(s, e.id) if h["kind"] == "dispute_accepted"]
    assert accepted[0]["reason_label"] is None


def test_a_rejected_case_keeps_counting():
    s = _mem_session(); _seed(s)
    e = _jan(s)
    sc.raise_dispute(s, e, who=BROKER, reason="already_sent", note="Emailed it",
                     today=date(2026, 2, 20))
    sc.decide(s, e, who=CARRIER, decision="reject", note="Nothing came",
              today=date(2026, 2, 22))
    s.commit()
    assert sc.days_over(e, date(2026, 3, 30)) == 48      # still owed


def test_more_time_then_overdue_again_counted_from_the_new_date():
    s = _mem_session(); _seed(s)
    e = _jan(s)
    sc.decide(s, e, who=CARRIER, decision="extend", note="Agreed by phone",
              extend_to=date(2026, 3, 5), today=date(2026, 2, 20))
    s.commit()
    assert e.case_state == sc.EXTENDED and e.case_until == date(2026, 3, 5)
    _sweep(s, date(2026, 3, 5))
    assert _jan(s).case_state == sc.EXTENDED
    _sweep(s, date(2026, 3, 6))
    assert _jan(s).case_state == sc.OVERDUE
    assert _jan(s).case_since == date(2026, 3, 6)
    _sweep(s, date(2026, 3, 19))
    assert _jan(s).case_state == sc.OVERDUE
    _sweep(s, date(2026, 3, 20))                         # 5 Mar + 15 days
    assert _jan(s).case_state == sc.DELINQUENT


# ---- the file arriving ------------------------------------------------------

@pytest.mark.parametrize("setup", ["overdue", "delinquent", "in_dispute", "extended"])
def test_the_file_arriving_closes_any_open_case(setup):
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed(s)
    e = _jan(s)
    _sweep(s, date(2026, 2, 12) if setup != "delinquent" else date(2026, 2, 26))
    if setup == "in_dispute":
        sc.raise_dispute(s, e, who=BROKER, reason="already_sent", note=None,
                         today=date(2026, 2, 12))
    if setup == "extended":
        sc.decide(s, e, who=CARRIER, decision="extend", note="ok",
                  extend_to=date(2026, 3, 1), today=date(2026, 2, 12))
    s.commit()
    assert e.case_state == setup
    svc.mark_received(s, p.id, received_on=date(2026, 2, 27), period="2026-01",
                      broker_party_id=e.broker_party_id)
    s.commit()
    assert e.case_state == sc.RESOLVED
    assert e.case_resolution == sc.FILE_RECEIVED
    assert e.status == "received_late"                   # 39.1 still says late


# ---- what is refused --------------------------------------------------------

def test_what_each_move_refuses():
    s = _mem_session(); _seed(s)
    e = _jan(s)
    with pytest.raises(sc.CaseError) as err:               # not late yet
        sc.raise_dispute(s, e, who=BROKER, reason="nil_return", note=None,
                         today=date(2026, 2, 9))
    assert err.value.code == "not_disputable"
    with pytest.raises(sc.CaseError) as err:
        sc.raise_dispute(s, e, who=BROKER, reason="", note=None, today=date(2026, 2, 12))
    assert err.value.code == "bad_reason"
    with pytest.raises(sc.CaseError) as err:
        sc.raise_dispute(s, e, who=BROKER, reason="other", note="  ", today=date(2026, 2, 12))
    assert err.value.code == "note_required"
    with pytest.raises(sc.CaseError) as err:
        sc.decide(s, e, who=CARRIER, decision="waive", note="", today=date(2026, 2, 12))
    assert err.value.code == "note_required"
    with pytest.raises(sc.CaseError) as err:
        sc.decide(s, e, who=CARRIER, decision="accept", note="x", today=date(2026, 2, 12))
    assert err.value.code == "no_dispute"
    with pytest.raises(sc.CaseError) as err:
        sc.decide(s, e, who=CARRIER, decision="extend", note="x",
                  extend_to=date(2026, 2, 12), today=date(2026, 2, 12))
    assert err.value.code == "bad_date"


def test_a_received_file_cannot_be_disputed():
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed(s)
    e = _jan(s)
    svc.mark_received(s, p.id, received_on=date(2026, 2, 15), period="2026-01",
                      broker_party_id=e.broker_party_id)
    with pytest.raises(sc.CaseError) as err:
        sc.raise_dispute(s, e, who=BROKER, reason="nil_return", note=None,
                         today=date(2026, 2, 16))
    assert err.value.code == "received"


# ---- reopening a case the carrier closed (MOPR 39.3) -------------------------

def _accepted(s):
    """January (due 10 Feb): disputed 20 Feb, accepted 22 Feb."""
    e = _jan(s)
    sc.raise_dispute(s, e, who=BROKER, reason="nil_return", note="Nothing written",
                     today=date(2026, 2, 20))
    sc.decide(s, e, who=CARRIER, decision="accept", note="Confirmed",
              today=date(2026, 2, 22))
    s.commit()
    return e


def test_reopening_an_accepted_case_puts_the_file_back_on_the_books():
    s = _mem_session(); _seed(s)
    e = _accepted(s)
    sc.decide(s, e, who=CARRIER, decision="reopen",
              note="Policies were sold after all", today=date(2026, 3, 1))
    s.commit()
    assert e.case_state == sc.DELINQUENT                 # 19 days late, past 15
    assert e.case_since == date(2026, 3, 1)
    assert e.case_resolution is None
    assert sc.days_over(e, date(2026, 3, 5)) == 23       # counting again
    assert _kinds(s, e)[-1] == "case_reopened"
    assert len(_actions(s, "submission_case_reopened_by_carrier")) == 1
    assert _actions(s, "submission_case_reopened") == []  # not "extra time ran out"
    # Like a rejection: the broker cannot dispute it again; the carrier can.
    assert sc.allowed(e, side="broker", carrier_admin=False,
                      today=date(2026, 3, 1)) == {"raise_dispute": False}
    can = sc.allowed(e, side="carrier", carrier_admin=True, today=date(2026, 3, 1))
    assert can["raise_dispute"] and can["waive"] and not can["reopen"]


def test_reopening_soon_after_goes_back_to_overdue_then_delinquent_as_usual():
    s = _mem_session(); _seed(s)
    e = _jan(s)
    sc.decide(s, e, who=CARRIER, decision="waive", note="Nil return",
              today=date(2026, 2, 15))
    sc.decide(s, e, who=CARRIER, decision="reopen", note="Wrong programme",
              today=date(2026, 2, 20))                   # 10 days late
    s.commit()
    assert e.case_state == sc.OVERDUE
    _sweep(s, date(2026, 2, 25))                         # 15 days late
    assert e.case_state == sc.DELINQUENT


def test_what_reopen_refuses():
    import submission_calendar_service as svc
    s = _mem_session(); p = _seed(s)
    e = _jan(s)
    _sweep(s, date(2026, 2, 11))
    with pytest.raises(sc.CaseError) as err:            # still open
        sc.decide(s, e, who=CARRIER, decision="reopen", note="x",
                  today=date(2026, 2, 12))
    assert err.value.code == "not_reopenable"
    e = _accepted(s)
    with pytest.raises(sc.CaseError) as err:            # no note
        sc.decide(s, e, who=CARRIER, decision="reopen", note=" ",
                  today=date(2026, 3, 1))
    assert err.value.code == "note_required"
    # Closed by the file arriving: a real file came, nothing to reopen.
    svc.mark_received(s, p.id, received_on=date(2026, 3, 2), period="2026-01",
                      broker_party_id=e.broker_party_id)
    with pytest.raises(sc.CaseError) as err:
        sc.decide(s, e, who=CARRIER, decision="reopen", note="x",
                  today=date(2026, 3, 3))
    assert err.value.code == "received"
    assert not sc.allowed(e, side="carrier", carrier_admin=True,
                          today=date(2026, 3, 3))["reopen"]


def test_reopen_is_offered_only_on_a_case_the_carrier_closed():
    s = _mem_session(); _seed(s)
    e = _jan(s)
    _sweep(s, date(2026, 2, 11))
    assert not sc.allowed(e, side="carrier", carrier_admin=True,
                          today=date(2026, 2, 11))["reopen"]
    e = _accepted(s)
    assert sc.allowed(e, side="carrier", carrier_admin=True,
                      today=date(2026, 2, 23))["reopen"]
    assert sc.allowed(e, side="broker", carrier_admin=False,
                      today=date(2026, 2, 23)) == {"raise_dispute": False}


# ---- chasing, the board, and what each side may do ---------------------------

def test_reminders_skip_disputed_extended_and_resolved_files():
    import db, submission_calendar_service as svc
    s = _mem_session(); _seed(s, brokers=4)
    rows = s.query(db.ExpectedSubmission).filter_by(period="2026-01").order_by(
        db.ExpectedSubmission.broker_party_id).all()
    today = date(2026, 2, 20)
    sc.raise_dispute(s, rows[1], who=BROKER, reason="nil_return", note=None, today=today)
    sc.decide(s, rows[2], who=CARRIER, decision="extend", note="ok",
              extend_to=date(2026, 3, 1), today=today)
    sc.decide(s, rows[3], who=CARRIER, decision="waive", note="nil", today=today)
    s.commit()
    out = svc.record_chase(s, [r.id for r in rows], today=today)
    assert [r["expected_id"] for r in out["rows"]] == [rows[0].id]


def test_the_board_carries_the_case_and_its_counts():
    import submission_calendar_service as svc
    s = _mem_session(); _seed(s)
    _sweep(s, date(2026, 2, 26))
    board = svc.calendar_board(s, 1, month="2026-02", today=date(2026, 2, 26))
    row = next(r for r in board["rows"] if r["period"] == "2026-01")
    assert row["case"]["state"] == sc.DELINQUENT
    assert row["case"]["state_label"] == "Delinquent"
    assert board["case_counts"][sc.DELINQUENT] == 1


def test_who_may_do_what():
    s = _mem_session(); _seed(s)
    e = _jan(s)
    today = date(2026, 2, 12)
    # A late file the daily check has not reached yet already reads as Overdue.
    assert sc.allowed(e, side="broker", carrier_admin=False, today=today) == {
        "raise_dispute": True}
    assert sc.allowed(e, side="carrier", carrier_admin=False, today=today) == {}
    sc.raise_dispute(s, e, who=BROKER, reason="nil_return", note=None, today=today)
    can = sc.allowed(e, side="carrier", carrier_admin=True, today=today)
    assert can["accept"] and can["reject"] and can["waive"] and can["extend"]
    assert not can["raise_dispute"]
    assert sc.allowed(e, side="broker", carrier_admin=False, today=today) == {
        "raise_dispute": False}


def test_history_is_in_order_and_names_the_daily_check():
    s = _mem_session(); _seed(s)
    e = _jan(s)
    _sweep(s, date(2026, 2, 11))
    sc.raise_dispute(s, e, who=BROKER, reason="nil_return", note="Nothing written",
                     today=date(2026, 2, 13))
    s.commit()
    hist = sc.history(s, e.id, label=lambda uid: "Broker 1" if uid == 50 else None)
    assert [h["kind_label"] for h in hist] == ["Became overdue", "Dispute raised"]
    assert hist[0]["by"] == "Automatic"
    assert hist[1]["by"] == "Broker 1"
    assert hist[1]["reason_label"] == "No policies sold this period, so nothing to report"
    assert hist[1]["note"] == "Nothing written"


# --- sweeps that overlap ------------------------------------------------------

def test_a_sweep_that_finds_another_running_changes_nothing(monkeypatch):
    import db
    import submission_calendar_service as svc
    s = _mem_session(); _seed(s)
    monkeypatch.setattr(svc, "_take_sweep_turn", lambda session: False)
    out = svc.sweep_overdue(s, today=date(2026, 3, 1), tenant_id=1)
    s.commit()
    assert out["skipped"] is True
    assert s.query(db.SubmissionCaseEvent).count() == 0
    assert _jan(s).case_state is None


def test_a_sweep_reads_fresh_rows_not_a_copy_loaded_earlier(tmp_path):
    """The duplicate history: one session holds a row from before another
    sweep moved it on, then sweeps from that stale copy."""
    import db
    import submission_calendar_service as svc
    url = f"sqlite:///{tmp_path / 'cases.db'}"
    a = _mem_session(url); _seed(a)
    b = _mem_session(url)
    held = b.query(db.ExpectedSubmission).filter_by(period="2026-01").first()
    assert held.case_state is None                       # b loaded it first...

    svc.sweep_overdue(a, today=date(2026, 3, 1), tenant_id=1)   # ...a moves it on
    a.commit()
    svc.sweep_overdue(b, today=date(2026, 3, 1), tenant_id=1)
    b.commit()

    kinds = [r.kind for r in a.query(db.SubmissionCaseEvent)
             .filter_by(expected_id=held.id).order_by(db.SubmissionCaseEvent.id)]
    assert kinds == ["opened", "delinquent"]             # once each, not twice


def test_the_cleanup_keeps_the_first_of_each_repeat_and_every_person_move():
    import db
    from sqlalchemy import text
    s = _mem_session(); _seed(s)
    e = _jan(s)

    def add(kind, on, actor="system"):
        row = db.SubmissionCaseEvent(tenant_id=1, expected_id=e.id, kind=kind,
                                     effective_on=on, actor=actor)
        s.add(row); s.flush()
        return row.id

    first = [add("opened", date(2026, 2, 11)), add("delinquent", date(2026, 2, 25))]
    for _ in range(2):                                   # the copies
        add("opened", date(2026, 2, 11)); add("delinquent", date(2026, 2, 25))
    add("dispute_raised", date(2026, 3, 1), actor="broker:1")
    add("dispute_raised", date(2026, 3, 1), actor="broker:1")   # a person's: kept
    s.commit()

    s.execute(text(db.DEDUPE_CASE_EVENTS_SQL)); s.commit()
    left = s.query(db.SubmissionCaseEvent).filter_by(expected_id=e.id).all()
    assert sorted(r.id for r in left if r.actor == "system") == first
    assert sum(1 for r in left if r.actor == "broker:1") == 2

    s.execute(text(db.DEDUPE_CASE_EVENTS_SQL)); s.commit()   # a second run: nothing
    assert s.query(db.SubmissionCaseEvent).filter_by(expected_id=e.id).count() == 4


# --- who is mailed, copied and answered --------------------------------------

def _broker_users(s, e, *users):
    import db
    for email, role in users:
        s.add(db.AppUser(email=email, full_name=email.split("@")[0], role=role,
                         status="active", broker_party_id=e.broker_party_id))
    s.commit()


def _capture_mail(monkeypatch):
    import notifications
    sent = []
    monkeypatch.setattr(notifications, "notify_people",
                        lambda people, title, **kw: sent.append((people, title, kw)))
    monkeypatch.setattr(notifications, "carrier_admin_recipients",
                        lambda tenant_id: [{"email": "owner@carrier.test", "name": "Owner"}])
    return sent


def test_a_dispute_mail_copies_the_broker_admins_and_answers_the_raiser(monkeypatch):
    s = _mem_session(); _seed(s)
    e = _jan(s)
    _broker_users(s, e, ("ann@broker.test", "broker_admin"),
                  ("bob@broker.test", "broker_admin"),
                  ("op@broker.test", "operator"))
    sent = _capture_mail(monkeypatch)

    sc.notify_dispute_raised(s, e, "Acme", "Broker 1", raised_by="bob@broker.test")
    people, title, kw = sent[-1]
    assert [p["email"] for p in people] == ["owner@carrier.test"]
    assert title == "Broker 1 disputed a late bordereau"
    assert sorted(kw["cc"]) == ["ann@broker.test", "bob@broker.test"]
    assert kw["reply_to"] == "bob@broker.test"

    # Raised by one of the broker's own users: the carrier never sees those,
    # so neither the copy nor the reply names them — the admin answers.
    sc.notify_dispute_raised(s, e, "Acme", "Broker 1", raised_by="op@broker.test")
    _people, _title, kw = sent[-1]
    assert "op@broker.test" not in kw["cc"]
    assert kw["reply_to"] in ("ann@broker.test", "bob@broker.test")


def test_a_decision_mail_copies_the_carrier_admin_who_decided(monkeypatch):
    s = _mem_session(); _seed(s)
    e = _jan(s)
    _broker_users(s, e, ("ann@broker.test", "broker_admin"),
                  ("op@broker.test", "operator"))
    sent = _capture_mail(monkeypatch)

    sc.notify_decision(s, e, "reject", "Nothing received", "Acme", "Carrier Co",
                       decided_by="carrier@example.com")
    people, title, kw = sent[-1]
    assert [p["email"] for p in people] == ["ann@broker.test"]
    assert title == "Your dispute was not accepted — please send the file"
    assert kw["cc"] == ["carrier@example.com"]
    assert kw["reply_to"] == "carrier@example.com"

    sc.notify_decision(s, e, "reopen", "Policies were sold", "Acme", "Carrier Co",
                       decided_by="carrier@example.com")
    _people, title, kw = sent[-1]
    assert title == "Your late bordereau case was reopened — please send the file"
    assert ("Their note", "Policies were sold") in kw["facts"]


def test_the_copy_goes_out_once_and_survives_a_failed_send(monkeypatch):
    import email_utils
    import notifications
    calls = []

    def fake_send(to, subject, html, text=None, account="", cc=None, reply_to=None, **_):
        calls.append((to, cc, reply_to))
        if to == "a@x.test":
            raise RuntimeError("mailbox down")

    monkeypatch.setattr(email_utils, "send_email", fake_send)
    notifications._send_emails(
        [{"email": "a@x.test"}, {"email": "b@x.test"}, {"email": "c@x.test"}],
        "Title", None, [], None, "Open", "Subject",
        cc=["boss@x.test", "B@x.test", "boss@x.test"], reply_to="boss@x.test")
    # a failed, so b carries the copy; c does not get a second one. b is a
    # recipient already, so it is not copied on top; the duplicate is dropped.
    assert calls == [("a@x.test", ["boss@x.test"], "boss@x.test"),
                     ("b@x.test", ["boss@x.test"], "boss@x.test"),
                     ("c@x.test", None, "boss@x.test")]
