"""MOPR 42.2 — delivering the Operational Report (report_delivery.py).

Run with:  DATABASE_URL=sqlite:// .venv/bin/python -m pytest test_report_delivery.py

No mail leaves: every send is given a recording `mailer` instead of SMTP.
"""
from __future__ import annotations

import io
from datetime import date, datetime

import pytest

import report_delivery as rd

NOW = datetime(2026, 11, 4, 6, 0)          # Wednesday — the 3rd working day of November


def _mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(
        eng, tables=[db.CheckRun.__table__, db.CheckResult.__table__,
                     db.CheckError.__table__, db.ExceptionDecisionLog.__table__,
                     db.QualitySetting.__table__, db.ExpectedSubmission.__table__,
                     db.MonthlyReportSetting.__table__, db.MonthlyReportSend.__table__,
                     db.AppUser.__table__, db.ActivityEvent.__table__,
                     db.Party.__table__, db.Program.__table__])
    return sessionmaker(bind=eng)()


def _user(s, uid, *, tenant=1, role="carrier_admin", status="active", name=None):
    import db
    s.add(db.AppUser(id=uid, tenant_id=tenant, email=f"u{uid}@carrier.test",
                     full_name=name or f"User {uid}", role=role, status=status))
    s.flush()


class Mailer:
    """Records each email instead of sending it; can be told to fail for one address."""
    def __init__(self, fail_for: str | None = None):
        self.sent, self.fail_for = [], fail_for

    def __call__(self, to, subject, html, text=None, account="", attachments=None, **_):
        if to == self.fail_for:
            raise RuntimeError("mailbox full")
        self.sent.append({"to": to, "subject": subject, "attachments": attachments or []})


@pytest.fixture(autouse=True)
def _no_carrier_lookup(monkeypatch):
    # The carrier's name comes from the tenant table, which SQLite cannot build
    # alongside the rest; the name itself is not what these tests are about.
    monkeypatch.setattr(rd, "carrier_name", lambda s, tid: "Test Carrier")


# --- when ---------------------------------------------------------------------

def test_the_nth_working_day_skips_weekends():
    assert rd.working_day(2026, 11, 3) == date(2026, 11, 4)    # 1 Nov is a Sunday
    assert rd.working_day(2026, 8, 1) == date(2026, 8, 3)      # 1 Aug is a Saturday


def test_last_months_report_is_due_from_its_day_for_a_week():
    assert rd.due_now(3, date(2026, 11, 3)) is None
    assert rd.due_now(3, date(2026, 11, 4)) == "2026-10"
    assert rd.due_now(3, date(2026, 11, 11)) == "2026-10"
    assert rd.due_now(3, date(2026, 11, 12)) is None


def test_the_next_send_moves_on_once_the_month_has_gone():
    assert rd.next_send(3, date(2026, 11, 2), set()) == {"send_on": "2026-11-04", "month": "2026-10"}
    assert rd.next_send(3, date(2026, 11, 5), {"2026-10"}) == {"send_on": "2026-12-03", "month": "2026-11"}


# --- who, and how -------------------------------------------------------------

def test_until_saved_it_is_not_sent_automatically():
    s = _mem_session()
    cfg = rd.settings(s, 1)
    assert cfg["auto_send"] is False and cfg["saved"] is False
    assert (cfg["file_format"], cfg["delivery"], cfg["send_day"]) == ("pdf", "attach", 3)


def test_it_goes_to_the_carriers_own_logins_only():
    s = _mem_session()
    _user(s, 1, role="tenant_admin")          # a legacy name for the Carrier seat
    _user(s, 2, status="inactive")
    _user(s, 3, tenant=2)                     # another carrier
    _user(s, 4, role="kavachio_admin")        # Kavachio staff, even on this carrier
    assert [p["user_id"] for p in rd.people(s, 1)] == [1]


def test_settings_are_checked():
    s = _mem_session()
    with pytest.raises(ValueError, match="no one to send"):
        rd.save_settings(s, 1, {"auto_send": True}, user_id=1)      # no login yet
    _user(s, 1)
    with pytest.raises(ValueError):
        rd.save_settings(s, 1, {"file_format": "word"}, user_id=1)
    with pytest.raises(ValueError):
        rd.save_settings(s, 1, {"send_day": 7}, user_id=1)
    before, after = rd.save_settings(s, 1, {"auto_send": True, "file_format": "both"}, user_id=1)
    assert before["auto_send"] is False and after["auto_send"] is True and after["saved"] is True


# --- sending -----------------------------------------------------------------------

def test_send_now_emails_each_person_the_file_and_keeps_the_numbers_as_sent():
    s = _mem_session()
    _user(s, 1)
    _user(s, 2)
    rd.save_settings(s, 1, {"file_format": "both"}, user_id=1)
    s.commit()
    mail = Mailer()
    r = rd.send(s, 1, "2026-10", kind="now", user_id=1, now=NOW, mailer=mail)
    assert (r["delivered"], r["failed"], r["status"]) == (2, 0, "sent")
    assert [m["to"] for m in mail.sent] == ["u1@carrier.test", "u2@carrier.test"]
    names = [a[0] for a in mail.sent[0]["attachments"]]
    assert names == ["Operational Report 2026-10.pdf", "Operational Report 2026-10.xlsx"]
    assert mail.sent[0]["attachments"][0][1].startswith(b"%PDF")
    assert "October 2026" in mail.sent[0]["subject"]
    h = rd.history(s, 1)
    assert h[0]["month"] == "2026-10" and h[0]["has_file"] is True and h[0]["people"] == 2


def test_send_now_goes_to_who_is_picked_in_the_format_picked(monkeypatch):
    import notifications
    # Send & Schedule says "a link, as PDF"; the popup picks Excel, so it goes along.
    monkeypatch.setattr(notifications, "_app_link", lambda path: f"https://app.test{path}")
    s = _mem_session()
    _user(s, 1)
    _user(s, 2)
    _user(s, 3, tenant=2)                      # another carrier's login
    rd.save_settings(s, 1, {"delivery": "link", "file_format": "pdf"}, user_id=1)
    s.commit()
    mail = Mailer()
    # The logins ticked (never another carrier's) and other addresses typed,
    # each address once — as the Executive Report's popup sends.
    r = rd.send(s, 1, "2026-10", kind="now", now=NOW, mailer=mail, user_ids=[2, 3],
                emails=["cfo@carrier.test", "U2@carrier.test"], file_format="excel")
    assert [m["to"] for m in mail.sent] == ["u2@carrier.test", "cfo@carrier.test"]
    assert r["delivered"] == 2
    assert [a[0] for a in mail.sent[0]["attachments"]] == ["Operational Report 2026-10.xlsx"]
    assert rd.history(s, 1)[0]["people"] == 2
    with pytest.raises(ValueError, match="Tick a login or type an email address"):
        rd.send(s, 1, "2026-10", kind="now", now=NOW, mailer=mail, user_ids=[3])
    with pytest.raises(ValueError, match="not email addresses: nobody"):
        rd.send(s, 1, "2026-10", kind="now", now=NOW, mailer=mail, user_ids=[], emails=["nobody"])
    with pytest.raises(ValueError, match="PDF, Excel or both"):
        rd.send(s, 1, "2026-10", kind="now", now=NOW, mailer=mail, file_format="word")


def test_reports_sent_can_be_paged_through_newest_first():
    s = _mem_session()
    _user(s, 1)
    s.commit()
    for m in ("2026-07", "2026-08", "2026-09", "2026-10"):
        rd.send(s, 1, m, kind="now", user_id=1, now=NOW, mailer=Mailer())
    assert rd.count_sends(s, 1) == 4
    assert [h["month"] for h in rd.history(s, 1, limit=3)] == ["2026-10", "2026-09", "2026-08"]
    assert [h["month"] for h in rd.history(s, 1, limit=3, offset=3)] == ["2026-07"]
    assert rd.count_sends(s, 2) == 0


def test_the_sent_list_says_who_and_clears_without_losing_a_send():
    s = _mem_session()
    _user(s, 1, name="Asha Rao")
    s.commit()
    rd.send(s, 1, "2026-09", kind="now", user_id=1, now=NOW, mailer=Mailer(),
            emails=["cfo@carrier.test"])
    rd.run_due(s, now=datetime(2026, 11, 4, 9, 1), mailer=Mailer())      # off: nothing
    rd.save_settings(s, 1, {"auto_send": True}, user_id=1)
    rd.run_due(s, now=datetime(2026, 11, 4, 9, 1), mailer=Mailer())      # October, by itself
    s.commit()
    h = rd.history(s, 1)
    assert [(x["month"], x["kind"], x["sent_by"]) for x in h] == [
        ("2026-10", "scheduled", None), ("2026-09", "now", "Asha Rao")]
    assert h[1]["recipients"] == [
        {"name": "Asha Rao", "email": "u1@carrier.test", "delivered": True},
        {"name": None, "email": "cfo@carrier.test", "delivered": True}]
    assert h[0]["sent_at"].endswith("Z")
    # Cleared: the list is empty, but October still counts as sent.
    assert rd.clear_history(s, 1, now=datetime(2026, 11, 4, 10, 0)) == 2
    s.commit()
    assert rd.history(s, 1) == [] and rd.count_sends(s, 1) == 0
    assert rd.run_due(s, now=datetime(2026, 11, 4, 11, 1), mailer=Mailer()) == 0
    # Saving the schedule keeps it cleared; a later send shows again.
    rd.save_settings(s, 1, {"send_hour": 8}, user_id=1)
    rd.send(s, 1, "2026-08", kind="now", user_id=1, now=datetime(2026, 11, 5, 9, 0), mailer=Mailer())
    s.commit()
    assert [x["month"] for x in rd.history(s, 1)] == ["2026-08"]
    assert rd.clear_history(s, 2) == 0       # another carrier: nothing to clear


def test_one_bad_address_does_not_stop_the_others():
    s = _mem_session()
    _user(s, 1)
    _user(s, 2)
    s.commit()
    r = rd.send(s, 1, "2026-10", kind="now", now=NOW, mailer=Mailer(fail_for="u1@carrier.test"))
    assert (r["delivered"], r["failed"], r["status"]) == (1, 1, "partly")


def test_kavachio_staff_never_get_it():
    s = _mem_session()
    _user(s, 1)
    _user(s, 4, role="kavachio_admin")
    mail = Mailer()
    rd.send(s, 1, "2026-10", kind="now", user_id=4, now=NOW, mailer=mail)
    assert [m["to"] for m in mail.sent] == ["u1@carrier.test"]


def test_a_link_sends_no_file_when_the_app_address_is_known(monkeypatch):
    import notifications
    monkeypatch.setattr(notifications, "_app_link", lambda path: f"https://app.test{path}")
    s = _mem_session()
    _user(s, 1)
    rd.save_settings(s, 1, {"delivery": "link"}, user_id=1)
    s.commit()
    mail = Mailer()
    rd.send(s, 1, "2026-10", kind="now", now=NOW, mailer=mail)
    assert mail.sent[0]["attachments"] == []


def test_the_daily_job_sends_each_month_once():
    s = _mem_session()
    _user(s, 1)
    rd.save_settings(s, 1, {"auto_send": True}, user_id=1)
    s.commit()
    mail = Mailer()
    assert rd.run_due(s, now=datetime(2026, 11, 3, 10), mailer=mail) == 0    # not its day yet
    assert rd.run_due(s, now=NOW, mailer=mail) == 0                          # its day, before 09:00 UK
    assert rd.run_due(s, now=datetime(2026, 11, 4, 9, 1), mailer=mail) == 1
    assert rd.run_due(s, now=datetime(2026, 11, 4, 10, 1), mailer=mail) == 0  # already gone
    assert len(mail.sent) == 1
    assert rd.sent_months(s, 1) == {"2026-10"}
    import db
    ev = s.query(db.ActivityEvent).filter(db.ActivityEvent.action == "monthly_report_sent").one()
    assert ev.actor == "system" and ev.details["kind"] == "scheduled"


def test_the_carriers_clock_is_uk_india_or_utc():
    # UK: GMT in winter, BST (UTC+1) from the last Sunday of March to the last of October.
    assert rd.local_time(datetime(2026, 11, 4, 6), "Europe/London") == datetime(2026, 11, 4, 6)
    assert rd.local_time(datetime(2026, 7, 1, 8), "Europe/London") == datetime(2026, 7, 1, 9)
    assert rd.local_time(datetime(2026, 3, 29, 0, 59), "Europe/London").hour == 0
    assert rd.local_time(datetime(2026, 3, 29, 1, 0), "Europe/London").hour == 2
    assert rd.local_time(datetime(2026, 10, 25, 0, 59), "Europe/London").hour == 1
    assert rd.local_time(datetime(2026, 10, 25, 1, 0), "Europe/London").hour == 1
    assert rd.local_time(datetime(2026, 11, 4, 6), "Asia/Kolkata") == datetime(2026, 11, 4, 11, 30)
    assert rd.local_time(datetime(2026, 11, 4, 6), "UTC") == datetime(2026, 11, 4, 6)


def test_how_often_when_and_to_whom_are_saved_and_checked():
    s = _mem_session()
    _user(s, 1)
    _user(s, 2)
    cfg = rd.settings(s, 1)
    assert (cfg["cadence"], cfg["week_day"], cfg["send_hour"], cfg["time_zone"]) == (
        "month", 0, 9, "Europe/London")
    assert (cfg["user_ids"], cfg["emails"]) == (None, [])
    for bad in ({"cadence": "custom"}, {"week_day": 5}, {"send_hour": 24},
                {"time_zone": "Mars"}, {"emails": ["not an address"]}):
        with pytest.raises(ValueError):
            rd.save_settings(s, 1, bad, user_id=1)
    with pytest.raises(ValueError, match="Tick a login or type an email address"):
        rd.save_settings(s, 1, {"auto_send": True, "user_ids": []}, user_id=1)
    _, after = rd.save_settings(s, 1, {"auto_send": True, "cadence": "week", "week_day": 2,
                                       "send_hour": 8, "time_zone": "Asia/Kolkata",
                                       "user_ids": [2], "emails": ["cfo@carrier.test"]}, user_id=1)
    assert {k: after[k] for k in ("cadence", "week_day", "send_hour", "time_zone",
                                  "user_ids", "emails")} == {
        "cadence": "week", "week_day": 2, "send_hour": 8, "time_zone": "Asia/Kolkata",
        "user_ids": [2], "emails": ["cfo@carrier.test"]}
    # Only typed addresses is enough.
    rd.save_settings(s, 1, {"user_ids": [], "emails": ["cfo@carrier.test"]}, user_id=1)


def test_a_week_goes_from_its_weekday_at_the_hour_picked_to_whom_it_was_picked():
    s = _mem_session()
    _user(s, 1)
    _user(s, 2)
    # Wednesdays at 09:00 UK time, to login 2 and one other address.
    rd.save_settings(s, 1, {"auto_send": True, "cadence": "week", "week_day": 2, "send_hour": 9,
                            "user_ids": [2], "emails": ["cfo@carrier.test"]}, user_id=1)
    s.commit()
    cfg = rd.settings(s, 1)
    assert rd.schedule_due(cfg, datetime(2026, 11, 17, 10)) is None          # Tuesday
    assert rd.schedule_due(cfg, datetime(2026, 11, 18, 8, 30)) is None       # Wednesday, 08:30
    assert rd.schedule_due(cfg, datetime(2026, 11, 18, 9, 1)) == ("week", "2026-11-09")
    # Missed on Wednesday (the app was down): Thursday's first run sends it.
    assert rd.schedule_due(cfg, datetime(2026, 11, 19, 3)) == ("week", "2026-11-09")
    nxt = rd.next_run(cfg, datetime(2026, 11, 17, 10), set())
    assert (nxt["send_on"], nxt["period"], nxt["cadence"], nxt["send_hour"]) == (
        "2026-11-18", "2026-11-09", "week", 9)
    mail = Mailer()
    assert rd.run_due(s, now=datetime(2026, 11, 18, 9, 1), mailer=mail) == 1
    assert rd.run_due(s, now=datetime(2026, 11, 18, 10, 1), mailer=mail) == 0   # gone already
    assert [m["to"] for m in mail.sent] == ["u2@carrier.test", "cfo@carrier.test"]
    assert mail.sent[0]["attachments"][0][0] == "Operational Report week of 2026-11-09.pdf"
    assert rd.sent_keys(s, 1) == {"week:2026-11-09"}
    assert rd.next_run(cfg, datetime(2026, 11, 18, 10), rd.sent_keys(s, 1))["send_on"] == "2026-11-25"
    h = rd.history(s, 1)[0]
    assert (h["kind"], h["cadence"], h["people"]) == ("scheduled", "week", 2)


def test_an_address_typed_in_gets_the_file_even_when_logins_get_a_link(monkeypatch):
    import notifications
    monkeypatch.setattr(notifications, "_app_link", lambda path: f"https://app.test{path}")
    s = _mem_session()
    _user(s, 1)
    rd.save_settings(s, 1, {"delivery": "link", "emails": ["cfo@carrier.test"]}, user_id=1)
    s.commit()
    mail = Mailer()
    cfg = rd.settings(s, 1)
    rd.send(s, 1, "2026-10", kind="scheduled", now=NOW, mailer=mail,
            user_ids=cfg["user_ids"], emails=cfg["emails"])
    got = {m["to"]: [a[0] for a in m["attachments"]] for m in mail.sent}
    # The login signs in to open it; the outside address has no login.
    assert got == {"u1@carrier.test": [], "cfo@carrier.test": ["Operational Report 2026-10.pdf"]}


def test_a_day_goes_every_day_on_the_carriers_clock():
    s = _mem_session()
    _user(s, 1)
    rd.save_settings(s, 1, {"auto_send": True, "cadence": "day", "send_hour": 7,
                            "time_zone": "Asia/Kolkata"}, user_id=1)
    s.commit()
    cfg = rd.settings(s, 1)
    # 01:00 UTC is 06:30 in India: not yet; 01:31 UTC is 07:01.
    assert rd.schedule_due(cfg, datetime(2026, 11, 18, 1, 0)) is None
    assert rd.schedule_due(cfg, datetime(2026, 11, 18, 1, 31)) == ("day", "2026-11-17")
    mail = Mailer()
    assert rd.run_due(s, now=datetime(2026, 11, 18, 1, 31), mailer=mail) == 1
    assert rd.run_due(s, now=datetime(2026, 11, 19, 1, 31), mailer=mail) == 1   # the next day's
    assert rd.sent_keys(s, 1) == {"day:2026-11-17", "day:2026-11-18"}


def test_nobody_to_send_to_is_said_plainly():
    s = _mem_session()
    with pytest.raises(ValueError, match="no one to send"):
        rd.send(s, 1, "2026-10", kind="now", now=NOW, mailer=Mailer())


# --- the files -------------------------------------------------------------------

def test_the_excel_file_has_one_sheet_per_part():
    from openpyxl import load_workbook
    s = _mem_session()
    pack = rd.build_pack(s, 1, "2026-10", now=NOW)
    wb = load_workbook(io.BytesIO(rd.render_xlsx(pack, "Test Carrier")))
    assert wb.sheetnames == ["Key numbers", "By broker and programme", "Checks that failed most",
                             "Unresolved problems", "Unresolved problems list", "Column Guide"]
    assert wb["Key numbers"]["A1"].value == "Operational Report — October 2026"
    # The title is the box's first line, centred across the table; under it
    # the period in words and the dates it compares, then the table. Every
    # line of every table has a border; the notes under it have none.
    key = wb["Key numbers"]
    merged = [str(r) for r in key.merged_cells.ranges]
    assert {"A1:F1", "A2:F2", "A3:F3"} <= set(merged)
    assert key["A1"].alignment.horizontal == "center"
    assert key["A3"].value.startswith("Covers 1 – 31 Oct 2026, compared with 1 – 30 Sep 2026")
    assert key["A4"].value == "Measure" and key["A5"].value == "Timeliness"
    end = next(c.row for c in key["A"] if c.value == "Unresolved for more than 5 days at month end")
    assert key["A1"].border.top.style == "medium" and key["A4"].border.left.style == "medium"
    assert key.cell(row=end, column=6).border.bottom.style == "medium"
    assert key.cell(row=end + 2, column=1).value == "Green is better, red is worse, grey is only more or less work."
    assert key.cell(row=end + 2, column=1).border.left.style is None
    assert wb["Unresolved problems"]["A3"].border.left.style == "medium"
    # Only the colour key under the table: no notes, no six months.
    assert key.cell(row=end + 3, column=1).value is None


def test_a_full_month_renders_in_both_files_with_its_comparisons():
    from openpyxl import load_workbook
    import test_monthly_report as tm
    s = _mem_session()
    for month, errors in ((9, 6), (10, 3)):
        tm._due(s, date(2026, month, 5), received=date(2026, month, 5 + (month == 9) * 3))
        run = tm._file(s, at=datetime(2026, month, 6), export_id=month, chain=f"sub:{month}",
                       errors=errors, critical=1, types={"new": {"rows": 4, "policies": 4}})
        tm._problem(s, run, fpr=f"p{month}", found=datetime(2026, month, 6))
    tm._fixed(s, at=datetime(2026, 10, 8), export_id=10)
    s.commit()
    pack = rd.build_pack(s, 1, "2026-10", now=NOW)
    # A name the PDF must print as text, not read as markup.
    pack["by_broker_programme"][0]["broker_name"] = "Smith & Co <Re>"
    pdf = rd.render_pdf(pack, "Test Carrier", "MOPR Program 1")
    assert pdf.startswith(b"%PDF") and len(pdf) > 2000
    ws = load_workbook(io.BytesIO(rd.render_xlsx(pack, "Test Carrier")))["Key numbers"]
    rows = {r[0]: r for r in ws.iter_rows(min_row=5, values_only=True) if r[0]}
    # October 1 of 1 on time; September's file was 3 days late.
    assert rows["Files received on time"][1] == 1.0 and rows["Files received on time"][2] == 0.0
    assert rows["Files received on time"][3] == "+100 pts"
    assert rows["Errors found"][1:4] == (3, 6, "-50%")
    assert rows["Files received on time"][4] == "Not available"      # October 2025: before the records


def test_changes_read_the_way_the_screen_reads_them():
    assert rd._delta(90.0, 85.0, "points", "up") == ("+5 pts", "ok")
    assert rd._delta(5120, 7430, "share", "down") == ("-31%", "ok")
    assert rd._delta(40, 38, "share", "none") == ("+5%", "mut")
    assert rd._delta(0.9, 1.8, "time", "down") == ("-22 h", "ok")
    assert rd._delta(2, 2, "count", "down") == ("No change", "mut")


# --- a day, a week or dates picked --------------------------------------------------

def test_a_week_renders_in_both_files_named_for_its_dates():
    from openpyxl import load_workbook
    s = _mem_session()
    pack = rd.build_pack(s, 1, "2026-10-26", now=NOW, cadence="week")
    wb = load_workbook(io.BytesIO(rd.render_xlsx(pack, "Test Carrier")))
    assert wb.sheetnames == ["Key numbers", "By broker and programme", "Checks that failed most",
                             "Unresolved problems", "Unresolved problems list", "Column Guide"]
    ws = wb["Key numbers"]
    assert ws["A1"].value == "Operational Report — Week of 26 Oct 2026"
    assert ws["A3"].value == "Covers 26 Oct – 1 Nov 2026, compared with 19 – 25 Oct 2026 and 27 Oct – 2 Nov 2025."
    assert [c.value for c in ws[4]][:6] == ["Measure", "Week of 26 Oct 2026", "Week of 19 Oct 2026",
                                            "vs last week", "Week of 27 Oct 2025", "vs last year"]
    labels = [r[0] for r in ws.iter_rows(min_row=5, values_only=True) if r[0]]
    assert "Still unresolved at the end of the week" in labels
    assert rd.render_pdf(pack, "Test Carrier").startswith(b"%PDF")
    assert [n for n, _d, _m in rd.files(pack, "both", "Test Carrier")] == [
        "Operational Report week of 2026-10-26.pdf", "Operational Report week of 2026-10-26.xlsx"]


def test_a_day_has_no_last_year_column():
    from openpyxl import load_workbook
    s = _mem_session()
    pack = rd.build_pack(s, 1, "2026-11-03", now=NOW, cadence="day")
    ws = load_workbook(io.BytesIO(rd.render_xlsx(pack, "Test Carrier")))["Key numbers"]
    header = [c.value for c in ws[4] if c.value]
    assert header == ["Measure", "3 Nov 2026", "2 Nov 2026", "vs the day before"]
    assert ws["A3"].value == "Covers Tuesday 3 November 2026, compared with the day before, Monday 2 November 2026."
    assert rd.headline(pack)[0] == ("This day", "Nothing was due, checked or fixed")


def test_send_now_can_send_a_week_and_the_history_names_it():
    s = _mem_session()
    _user(s, 1)
    s.commit()
    mail = Mailer()
    r = rd.send(s, 1, "2026-10-26", kind="now", user_id=1, now=NOW, mailer=mail, cadence="week")
    assert (r["cadence"], r["title"], r["status"]) == ("week", "Week of 26 Oct 2026", "sent")
    assert "Week of 26 Oct 2026" in mail.sent[0]["subject"]
    assert mail.sent[0]["attachments"][0][0] == "Operational Report week of 2026-10-26.pdf"
    rd.send(s, 1, "2026-10", kind="now", user_id=1, now=NOW, mailer=mail)
    h = rd.history(s, 1)
    assert [(x["month"], x["cadence"], x["title"]) for x in h] == [
        ("2026-10", "month", "October 2026"), ("2026-10-26", "week", "Week of 26 Oct 2026")]
    # Only the automatic send counts as a month's send.
    assert rd.sent_months(s, 1) == set()


# --- what only the files carry -----------------------------------------------------

def test_the_files_carry_the_checks_that_failed_and_the_errors_still_waiting():
    from openpyxl import load_workbook
    import test_monthly_report as tm
    s = _mem_session()
    run = tm._file(s, at=datetime(2026, 10, 6), export_id=10, chain="sub:10", errors=2, critical=1)
    tm._problem(s, run, fpr="a", found=datetime(2026, 10, 6), row=1)
    tm._problem(s, run, fpr="b", found=datetime(2026, 11, 2), row=2, rule_id=2,
                name="State is valid", severity="warning")
    s.commit()
    pack = rd.build_pack(s, 1, "2026-10", now=NOW)
    assert [c["check"] for c in pack["top_checks"]["checks"]] == [
        "Premium must be more than 0", "State is valid"]
    w = pack["waiting"]
    # As at 4 Nov: one found 6 Oct (29 days, past the 5-day target), one found 2 Nov.
    assert (w["problems"], w["past_target"]) == (2, 1)
    assert [b["problems"] for b in w["by_age"]] == [1, 0, 1, 0]
    assert (w["by_owner"][0]["owner"], w["by_owner"][0]["open"], w["by_owner"][0]["past_target"]) == (
        "Broker 8", 2, 1)

    wb = load_workbook(io.BytesIO(rd.render_xlsx(pack, "Test Carrier")))
    # The broker sheet says it in the Key numbers' words, the target included.
    bp = [c.value for c in wb["By broker and programme"][1]]
    assert (bp[2], bp[3], bp[10]) == ("Files expected so far", "Files received on time",
                                      "Unresolved for more than 5 days")
    checks = wb["Checks that failed most"]
    # Counts only — no share columns, as on screen.
    assert [c.value for c in checks[1]] == ["#", "Check", "Severity", "Files", "Errors"]
    assert (checks["B2"].value, checks["E2"].value) == ("Premium must be more than 0", 1)
    waiting = wb["Unresolved problems"]
    assert waiting["A1"].value.startswith("As at 4 Nov 2026: 2 unresolved, 1 unresolved for more than 5 days")
    assert [waiting.cell(row=r, column=1).value for r in range(4, 8)] == [
        "0–2 days", "3–7 days", "8–30 days", "Over 30 days"]
    assert [c.value for c in waiting[3]][:3] == ["Unresolved for", "Open problems", "Past the target"]
    assert waiting["A10"].value == "Broker 8"
    listed = wb["Unresolved problems list"]
    # The longest waiting first, one row per file and check.
    assert (listed["A2"].value, listed["D2"].value, listed["G2"].value) == (
        "Broker 8", "Premium must be more than 0", 1)
    assert listed.max_row == 3
    pdf = rd.render_pdf(pack, "Test Carrier")
    assert pdf.startswith(b"%PDF") and len(pdf) > 3000


def test_the_files_say_the_period_in_words_and_which_dates_it_compares():
    s = _mem_session()
    today = rd.build_pack(s, 1, "2026-11-04", now=NOW, cadence="day")
    assert rd.covers(today) == "Today so far, Wednesday 4 November 2026, compared with yesterday."
    # Nothing has gone through the system yet, so its records begin today:
    # today reads its real (empty) numbers, an earlier month has none.
    assert rd.glance(today).startswith("No files were due on 4 Nov 2026.")
    assert "still running" in rd.glance(today)
    october = rd.build_pack(s, 1, "2026-10", now=NOW)
    assert rd.glance(october) == ("There are no records in October 2026: "
                                  "this report counts from 4 Nov 2026.")
