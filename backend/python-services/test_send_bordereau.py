"""Tests for the reporting-period / send-to-carrier machinery behind Process
Bordereau — submission_calendar.send_status and submission_calendar_service.
broker_bordereau_rows / send_bordereau / carrier_contacts.

There is no separate "Send Bordereau" screen or route any more — sending is
now automatic, called from direct_routes._render_landing right after
mark_received satisfies a period on a broker's own run (no HTTP layer of its
own to test here; see test_process_bordereau_auto_sends below, which calls
mark_received then send_bordereau in that same sequence). send_bordereau()
itself is UNCHANGED by that move — it is the exact function the removed
screen's Send button used to call, just called from a different place.

Same in-memory-SQLite pattern as test_submission_calendar.py's
`_mem_session()`, extended with the tables this feature actually touches
(OutputExport, for the attachment). No network, no real Postgres — this is
what stood in for the local/disposable DB the task asked for, given this
session has no credentials for either the shared DB or a local Postgres
instance (see the implementation summary).
"""
from datetime import date
from unittest.mock import patch

import pytest

from submission_calendar import send_status


# audit.log_activity() deliberately opens its OWN SessionLocal() rather than
# taking the caller's session (see audit.py) — reasonable in production, but
# it means these tests' in-memory sqlite session does NOT contain it: an
# unmocked run reaches whatever DATABASE_URL actually resolves to. Autouse so
# every test in this file is covered, not just the ones that happen to
# remember it. (This is not theoretical — an early run of this suite, before
# this fixture existed, wrote 5 rows to the real shared activity_events table;
# see the implementation summary.)
@pytest.fixture(autouse=True)
def _no_real_audit_writes():
    with patch("audit.log_activity") as m:
        yield m


# ---------------------------------------------------------------------------
# send_status — pure logic, no DB. Every scenario the approval asked for.
# ---------------------------------------------------------------------------

DUE = date(2026, 9, 10)


def test_send_status_not_due_yet():
    assert send_status(DUE, date(2026, 9, 1)) == "not_due"


def test_send_status_not_arrived_when_overdue_and_nothing_processed():
    assert send_status(DUE, date(2026, 9, 15)) == "not_arrived"


def test_send_status_not_arrived_on_the_due_date_itself_with_nothing_processed():
    # No grace, same rule as derive_status: the due date itself with nothing
    # processed already counts as not arrived, not "still due today".
    assert send_status(DUE, DUE) == "not_arrived"


def test_send_status_ready_to_send_once_processed_but_not_sent():
    assert send_status(DUE, date(2026, 9, 20),
                       processed_on=date(2026, 9, 8)) == "ready_to_send"


def test_send_status_ready_to_send_even_if_processed_late():
    # Processing late does not change the bucket — "not arrived" must never
    # mean "processed but unsent"; only sent/unsent decides on_time vs late.
    assert send_status(DUE, date(2026, 9, 20),
                       processed_on=date(2026, 9, 14)) == "ready_to_send"


def test_send_status_on_time_when_sent_on_or_before_due_date():
    assert send_status(DUE, date(2026, 9, 20),
                       processed_on=date(2026, 9, 8),
                       sent_on=date(2026, 9, 9)) == "on_time"
    assert send_status(DUE, date(2026, 9, 20),
                       processed_on=date(2026, 9, 8),
                       sent_on=DUE) == "on_time"          # sent ON the due date


def test_send_status_late_when_sent_after_due_date():
    assert send_status(DUE, date(2026, 9, 20),
                       processed_on=date(2026, 9, 8),
                       sent_on=date(2026, 9, 12)) == "late"


def test_send_status_late_even_when_processed_before_due_date():
    # Processing early does not rescue a late SEND — the two acts are judged
    # independently, which is the whole point of keeping them separate.
    assert send_status(DUE, date(2026, 9, 20),
                       processed_on=date(2026, 9, 1),
                       sent_on=date(2026, 9, 11)) == "late"


# ---------------------------------------------------------------------------
# Service layer — in-memory SQLite, same shape as test_submission_calendar's
# _mem_session but with OutputExport added for the attachment.
# ---------------------------------------------------------------------------

def _mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(
        eng, tables=[db.Program.__table__, db.Contract.__table__,
                     db.SubmissionSchedule.__table__, db.ExpectedSubmission.__table__,
                     db.ActivityEvent.__table__, db.ProgramBroker.__table__,
                     db.SubmissionVersion.__table__, db.Party.__table__,
                     db.AppUser.__table__, db.Tenant.__table__,
                     db.OutputExport.__table__])
    return sessionmaker(bind=eng)()


def _seed(s, *, tenant_id=1, program_name="Acme", broker_id=100,
         today=date(2026, 9, 1), horizon_months=2):
    import db, submission_calendar_service as svc
    s.add(db.Tenant(id=tenant_id, tenant_name=f"T{tenant_id}",
                    legal_name=f"Carrier {tenant_id}"))
    p = db.Program(tenant_id=tenant_id, name=program_name, bdx_frequency="monthly")
    s.add(p); s.flush()
    c = db.Contract(program_id=p.id, tenant_id=tenant_id,
                    extracted={"inception_dt": "2026-01-01"})
    s.add(c); s.flush()
    sched = db.SubmissionSchedule(tenant_id=tenant_id, program_id=p.id)
    s.add(sched); s.flush()
    svc.materialize_schedule(s, sched, today=today, horizon_months=horizon_months)
    # Adopt every generated row onto one broker, exactly like a real
    # ProgramBroker attach would (materialize_schedule leaves them
    # unattributed until a broker exists on the programme).
    import db as _db
    s.add(_db.ProgramBroker(tenant_id=tenant_id, program_id=p.id,
                            broker_party_id=broker_id, status="active"))
    for e in s.query(_db.ExpectedSubmission).filter(
            _db.ExpectedSubmission.program_id == p.id).all():
        e.broker_party_id = broker_id
    if s.get(_db.Party, broker_id) is None:   # a broker can appear via _seed twice
        s.add(_db.Party(id=broker_id, legal_name="Test Broker Co", party_type="broker"))
    s.commit()
    return p, c


def _add_carrier_admin(s, tenant_id, email="carrier.admin@example.com"):
    import db
    s.add(db.AppUser(tenant_id=tenant_id, email=email, full_name="Carrier Admin",
                     role="carrier_admin", status="active"))
    s.commit()


def _add_broker_admin(s, broker_id, email="broker.admin@example.com"):
    import db
    s.add(db.AppUser(broker_party_id=broker_id, email=email, full_name="Broker Admin",
                     role="broker_admin", status="active"))
    s.commit()


def _process(s, expected_id, *, received_on, export_bytes=b"fake-xlsx-bytes",
            exception_count=0):
    """Stand in for Process Bordereau: an OutputExport + a SubmissionVersion,
    exactly what mark_received leaves behind, without going through the whole
    file-validation pipeline this test has no reason to exercise."""
    import db
    e = s.get(db.ExpectedSubmission, expected_id)
    out = db.OutputExport(tenant_id=e.tenant_id, filename=f"{e.period}.xlsx",
                          blob=export_bytes, exception_count=exception_count,
                          status="has_exceptions" if exception_count else "clean")
    s.add(out); s.flush()
    e.received_at = received_on
    e.received_export_id = out.id
    e.version_count = (e.version_count or 0) + 1
    e.latest_received_at = received_on
    s.add(db.SubmissionVersion(
        tenant_id=e.tenant_id, expected_id=e.id, program_id=e.program_id,
        broker_party_id=e.broker_party_id, period=e.period,
        version_no=e.version_count, kind="original",
        received_at=received_on, received_export_id=out.id))
    s.commit()
    return out


def test_broker_bordereau_rows_covers_not_due_and_not_arrived():
    import submission_calendar_service as svc
    s = _mem_session()
    p, c = _seed(s, today=date(2026, 9, 1))
    rows = svc.broker_bordereau_rows(s, 100, program_ids=[p.id], today=date(2026, 9, 1))
    statuses = {r["period"]: r["status"] for r in rows}
    # July's due date (~10 Aug) has passed with nothing processed; August's
    # (~10 Sep) has not arrived yet — anchor 2026-01-01, default due_day 10.
    assert statuses["2026-07"] == "not_arrived"
    assert statuses["2026-08"] == "not_due"


def test_period_picker_query_excludes_future_periods():
    # carrier_routes.contract_bordereau_periods and direct_routes.
    # direct_periods both cannot be imported here (same RULE_CLASS_LIBRARY-at-
    # import-time issue as broker_routes — see the implementation summary),
    # so this exercises their actual filter — `period_end <= today` — directly
    # against the same ExpectedSubmission rows, rather than the route wrapper.
    import db
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1), horizon_months=6)
    today = date(2026, 9, 1)

    all_periods = {e.period for e in s.query(db.ExpectedSubmission)
                  .filter(db.ExpectedSubmission.program_id == p.id).all()}
    assert "2026-11" in all_periods   # a genuinely future period exists to exclude

    listed = (s.query(db.ExpectedSubmission)
             .filter(db.ExpectedSubmission.program_id == p.id,
                     db.ExpectedSubmission.period_end <= today)
             .all())
    listed_periods = {e.period for e in listed}

    assert "2026-11" not in listed_periods       # future — excluded
    assert "2026-07" in listed_periods           # already ended — listed
    # The period covering "today" itself (August, ending 2026-08-31) has not
    # ended yet on 2026-09-01... wait: today IS 2026-09-01, so August (ending
    # 2026-08-31) HAS ended and is listed; September (ending 2026-09-30) has
    # not and is excluded — the boundary this filter actually exists for.
    assert "2026-08" in listed_periods
    assert "2026-09" not in listed_periods


def test_ready_to_send_appears_once_processed():
    s = _mem_session()
    p, c = _seed(s, today=date(2026, 9, 1))
    import submission_calendar_service as svc
    e = s.query(__import__("db").ExpectedSubmission).filter_by(
        program_id=p.id, period="2026-07").first()
    _process(s, e.id, received_on=date(2026, 8, 20))
    rows = svc.broker_bordereau_rows(s, 100, program_ids=[p.id], today=date(2026, 9, 1))
    row = next(r for r in rows if r["period"] == "2026-07")
    assert row["status"] == "ready_to_send"


@patch("email_utils.send_email")
def test_send_bordereau_on_time(mock_send, monkeypatch):
    monkeypatch.setenv("BORDEREAU_AUTO_SEND_EMAIL", "true")
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, c = _seed(s, today=date(2026, 9, 1))
    _add_carrier_admin(s, 1)
    _add_broker_admin(s, 100)
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    due = e.due_date
    _process(s, e.id, received_on=due)
    out = svc.send_bordereau(s, e.id, 100, actor_email="broker.user@example.com",
                             released_on=due)
    s.commit()
    assert out["mail_sent"] is True
    assert out["to"] == ["carrier.admin@example.com"]
    assert out["cc"] == ["broker.admin@example.com"]
    mock_send.assert_called_once()
    kwargs = mock_send.call_args.kwargs
    assert kwargs["account"] == "NOTIFY"
    assert kwargs["attachments"][0][1] == b"fake-xlsx-bytes"

    s.expire_all()
    rows = svc.broker_bordereau_rows(s, 100, program_ids=[p.id], today=due)
    row = next(r for r in rows if r["period"] == "2026-07")
    assert row["status"] == "on_time"


@patch("email_utils.send_email")
def test_send_bordereau_late(mock_send):
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, c = _seed(s, today=date(2026, 9, 1))
    _add_carrier_admin(s, 1)
    _add_broker_admin(s, 100)
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    _process(s, e.id, received_on=e.due_date)

    # record_release directly, at a date past the due date, so this proves the
    # STATUS math rather than racing the real wall-clock date.
    from datetime import timedelta
    late_date = e.due_date + timedelta(days=3)
    svc.record_release(s, e.id, released_on=late_date, released_to="x",
                       released_by="y")
    s.commit()
    s.expire_all()
    rows = svc.broker_bordereau_rows(s, 100, program_ids=[p.id], today=late_date)
    row = next(r for r in rows if r["period"] == "2026-07")
    assert row["status"] == "late"


def test_broker_isolation_send_refuses_another_brokers_period():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, c = _seed(s, today=date(2026, 9, 1), broker_id=100)
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    _process(s, e.id, received_on=e.due_date)
    try:
        svc.send_bordereau(s, e.id, 200, actor_email="intruder@example.com")
        assert False, "a different broker must not be able to send this period"
    except ValueError as ex:
        assert str(ex) == "not_found"


def test_broker_isolation_board_never_returns_another_brokers_rows():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p1, _ = _seed(s, tenant_id=1, program_name="Acme", broker_id=100)
    p2, _ = _seed(s, tenant_id=2, program_name="Globex", broker_id=200)
    rows_100 = svc.broker_bordereau_rows(s, 100, program_ids=[p1.id, p2.id])
    rows_200 = svc.broker_bordereau_rows(s, 200, program_ids=[p1.id, p2.id])
    assert all(r["carrier_id"] == 1 for r in rows_100)
    assert all(r["carrier_id"] == 2 for r in rows_200)
    assert {r["program_id"] for r in rows_100} == {p1.id}
    assert {r["program_id"] for r in rows_200} == {p2.id}


def test_multiple_carriers_and_programmes_in_one_broker_board():
    import submission_calendar_service as svc
    s = _mem_session()
    p1, _ = _seed(s, tenant_id=1, program_name="Acme", broker_id=100)
    p2, _ = _seed(s, tenant_id=2, program_name="Globex", broker_id=100)
    rows = svc.broker_bordereau_rows(s, 100, program_ids=[p1.id, p2.id])
    assert {r["carrier_id"] for r in rows} == {1, 2}
    assert {r["program_name"] for r in rows} == {"Acme", "Globex"}


def test_row_carries_export_id_and_exception_count_for_a_clean_file():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    out = _process(s, e.id, received_on=e.due_date, exception_count=0)
    rows = svc.broker_bordereau_rows(s, 100, program_ids=[p.id])
    row = next(r for r in rows if r["period"] == "2026-07")
    assert row["export_id"] == out.id
    assert row["exception_count"] == 0


def test_row_carries_exception_count_for_a_flagged_file():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    out = _process(s, e.id, received_on=e.due_date, exception_count=7)
    rows = svc.broker_bordereau_rows(s, 100, program_ids=[p.id])
    row = next(r for r in rows if r["period"] == "2026-07")
    assert row["export_id"] == out.id
    assert row["exception_count"] == 7


def test_export_id_and_exception_count_are_none_before_anything_is_processed():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    rows = svc.broker_bordereau_rows(s, 100, program_ids=[p.id])
    row = next(r for r in rows if r["period"] == "2026-07")
    assert row["export_id"] is None
    assert row["exception_count"] == 0


@patch("email_utils.send_email")
def test_send_is_not_blocked_by_exceptions(mock_send, monkeypatch):
    # The broker's own call — a carrier decides what to do about exceptions,
    # never the send action itself. This is the guard against ever adding a
    # server-side block here by accident.
    monkeypatch.setenv("BORDEREAU_AUTO_SEND_EMAIL", "true")
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    _add_carrier_admin(s, 1)
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    _process(s, e.id, received_on=e.due_date, exception_count=40)
    out = svc.send_bordereau(s, e.id, 100, actor_email="a@example.com",
                             released_on=e.due_date)
    assert out["mail_sent"] is True


def test_pagination_matches_the_unpaged_list():
    # Same rows, same order, just handed back a page at a time — this is the
    # property that actually matters: paging must never lose or duplicate a
    # row relative to the full list.
    import submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1), horizon_months=6)
    whole = svc.broker_bordereau_rows(s, 100, program_ids=[p.id])
    assert len(whole) >= 5   # horizon_months=6 generates enough to page over

    size = 3
    pages: list[dict] = []
    total = None
    page_no = 1
    while True:
        items, tot = svc.broker_bordereau_rows(
            s, 100, program_ids=[p.id], page=page_no, page_size=size)
        total = tot
        if not items:
            break
        pages.extend(items)
        page_no += 1

    assert total == len(whole)
    # SQL orders by (due_date, id) — the old Python sort's program-name
    # tiebreak never mattered here since there is only one programme, so the
    # two orderings agree and the ids line up one for one.
    assert [r["expected_id"] for r in pages] == [r["expected_id"] for r in whole]


def test_pagination_page_past_the_end_is_empty_not_an_error():
    import submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    items, total = svc.broker_bordereau_rows(
        s, 100, program_ids=[p.id], page=999, page_size=10)
    assert items == []
    assert total > 0   # the count is still the REAL total, not zero


@patch("email_utils.send_email")
def test_process_bordereau_auto_sends(mock_send, monkeypatch):
    monkeypatch.setenv("BORDEREAU_AUTO_SEND_EMAIL", "true")
    # Mirrors direct_routes._render_landing's actual new sequence, line for
    # line: mark_received() (what Process Bordereau already did), immediately
    # followed by send_bordereau() using mark_received's own expected_id —
    # no separate screen, no separate click, in one request/response.
    import db
    from submission_calendar_service import mark_received, send_bordereau

    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    _add_carrier_admin(s, 1)
    _add_broker_admin(s, 100)

    out = db.OutputExport(tenant_id=1, filename="2026-07.xlsx",
                          blob=b"the-actual-bytes", exception_count=3,
                          status="has_exceptions", broker_party_id=100)
    s.add(out); s.flush()

    due = s.query(db.ExpectedSubmission).filter_by(
        program_id=p.id, period="2026-07").first().due_date
    # received_on pinned to the due date itself — this test proves the
    # SEQUENCE (mark_received then send_bordereau, in one flow), not the
    # on-time/late boundary, which send_status's own tests already cover.
    received = mark_received(s, p.id, export_id=out.id, received_on=due,
                             broker_party_id=100, period="2026-07")
    s.commit()
    assert received is not None
    assert received["status"] == "on_time"

    result = send_bordereau(s, received["expected_id"], 100,
                            actor_email="broker:100", actor_user_id=7,
                            released_on=s.get(db.ExpectedSubmission,
                                              received["expected_id"]).due_date)
    s.commit()

    assert result["mail_sent"] is True
    assert result["to"] == ["carrier.admin@example.com"]
    assert result["cc"] == ["broker.admin@example.com"]
    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["attachments"][0][1] == b"the-actual-bytes"

    e = s.get(db.ExpectedSubmission, received["expected_id"])
    assert e.received_at is not None     # processing recorded
    assert e.released_at is not None     # AND sending, in the same flow
    assert e.released_count == 1


def test_not_processed_refuses_send():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    try:
        svc.send_bordereau(s, e.id, 100, actor_email="x@example.com")
        assert False, "nothing has been processed — there is nothing to send"
    except ValueError as ex:
        assert str(ex) == "not_processed"


@patch("email_utils.send_email")
def test_resend_updates_timestamp_without_a_second_release_count(mock_send, monkeypatch):
    monkeypatch.setenv("BORDEREAU_AUTO_SEND_EMAIL", "true")
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    _add_carrier_admin(s, 1)
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    _process(s, e.id, received_on=e.due_date)

    svc.send_bordereau(s, e.id, 100, actor_email="a@example.com")
    s.commit()
    first_count = s.get(db.ExpectedSubmission, e.id).released_count
    assert first_count == 1

    svc.send_bordereau(s, e.id, 100, actor_email="a@example.com")
    s.commit()
    second_count = s.get(db.ExpectedSubmission, e.id).released_count
    assert second_count == 1          # re-send, not a second release
    assert mock_send.call_count == 2  # but it DID mail again


@patch("email_utils.send_email")
def test_audit_log_entry_written_on_send(mock_send, _no_real_audit_writes, monkeypatch):
    # log_activity() itself opens a fresh SessionLocal() (see audit.py), so
    # this asserts on the CALL — the fixture is what keeps it off the shared
    # DB and gives us something to inspect. What matters here is that
    # send_bordereau asks for exactly one "bordereau_sent" row naming this
    # period, on the CARRIER's tenant — for an email that was really sent.
    monkeypatch.setenv("BORDEREAU_AUTO_SEND_EMAIL", "true")
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    _add_carrier_admin(s, 1)
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    _process(s, e.id, received_on=e.due_date)
    svc.send_bordereau(s, e.id, 100, actor_email="a@example.com")
    s.commit()
    _no_real_audit_writes.assert_called_once()
    args, kwargs = _no_real_audit_writes.call_args
    assert args[0] == 1                    # tenant_id — the CARRIER's, not the broker's
    assert args[2] == "bordereau_sent"      # action
    assert kwargs["details"]["period"] == "2026-07"


@patch("email_utils.send_email")
def test_outbound_email_disabled_by_default(mock_send, monkeypatch):
    # No BORDEREAU_AUTO_SEND_EMAIL set at all — the actual state right now,
    # on request: the SMTP call never fires, but the period still moves to
    # sent/on-time-or-late, the release is still recorded, and mail_error
    # says plainly that it was disabled rather than that it failed.
    monkeypatch.delenv("BORDEREAU_AUTO_SEND_EMAIL", raising=False)
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    _add_carrier_admin(s, 1)
    _add_broker_admin(s, 100)
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    _process(s, e.id, received_on=e.due_date)
    out = svc.send_bordereau(s, e.id, 100, actor_email="a@example.com",
                             released_on=e.due_date)
    s.commit()
    mock_send.assert_not_called()
    assert out["mail_sent"] is False
    assert out["mail_error"] == "outbound email is temporarily disabled"
    # Still recorded as sent — this is a paused EMAIL, not a paused send.
    fresh = s.get(db.ExpectedSubmission, e.id)
    assert fresh.released_at is not None
    assert fresh.released_count == 1


@patch("email_utils.send_email")
def test_no_audit_row_when_outbound_email_is_switched_off(mock_send, _no_real_audit_writes,
                                                          monkeypatch):
    # Switched off on purpose, so no email was even attempted — the audit
    # trail says nothing (6 Oct 2026). The release is still recorded.
    monkeypatch.delenv("BORDEREAU_AUTO_SEND_EMAIL", raising=False)
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    _add_carrier_admin(s, 1)
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    _process(s, e.id, received_on=e.due_date)
    svc.send_bordereau(s, e.id, 100, actor_email="a@example.com")
    s.commit()
    _no_real_audit_writes.assert_not_called()
    assert s.get(db.ExpectedSubmission, e.id).released_count == 1


@patch("email_utils.send_email")
def test_setting_the_flag_explicitly_false_also_disables(mock_send, monkeypatch):
    monkeypatch.setenv("BORDEREAU_AUTO_SEND_EMAIL", "false")
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    _add_carrier_admin(s, 1)
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    _process(s, e.id, received_on=e.due_date)
    out = svc.send_bordereau(s, e.id, 100, actor_email="a@example.com",
                             released_on=e.due_date)
    mock_send.assert_not_called()
    assert out["mail_sent"] is False


def test_no_carrier_contact_refuses_send_without_recording_anything():
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    # deliberately no carrier_admin user seeded
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    _process(s, e.id, received_on=e.due_date)
    try:
        svc.send_bordereau(s, e.id, 100, actor_email="a@example.com")
        assert False
    except ValueError as ex:
        assert str(ex) == "no_carrier_contact"
    assert s.get(db.ExpectedSubmission, e.id).released_count == 0


def test_processed_and_received_at_are_independent_of_sending():
    # The guardrail the approval was most explicit about: processing must
    # never be conflated with sending.
    import db, submission_calendar_service as svc
    s = _mem_session()
    p, _ = _seed(s, today=date(2026, 9, 1))
    _add_carrier_admin(s, 1)
    e = s.query(db.ExpectedSubmission).filter_by(program_id=p.id, period="2026-07").first()
    processed_on = e.due_date
    _process(s, e.id, received_on=processed_on)
    assert s.get(db.ExpectedSubmission, e.id).released_at is None  # not sent yet
    with patch("email_utils.send_email"):
        svc.send_bordereau(s, e.id, 100, actor_email="a@example.com")
    s.commit()
    fresh = s.get(db.ExpectedSubmission, e.id)
    assert fresh.received_at == processed_on   # untouched by the send
    assert fresh.released_at is not None        # the send, recorded separately
