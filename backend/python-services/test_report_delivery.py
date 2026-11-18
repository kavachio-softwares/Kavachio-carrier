"""MOPR 42.2 — delivering the Monthly Report (report_delivery.py).

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
    assert names == ["Monthly Report 2026-10.pdf", "Monthly Report 2026-10.xlsx"]
    assert mail.sent[0]["attachments"][0][1].startswith(b"%PDF")
    assert "October 2026" in mail.sent[0]["subject"]
    h = rd.history(s, 1)
    assert h[0]["month"] == "2026-10" and h[0]["has_file"] is True and h[0]["people"] == 2


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
    assert rd.run_due(s, now=datetime(2026, 11, 3, 6), mailer=mail) == 0     # not its day yet
    assert rd.run_due(s, now=NOW, mailer=mail) == 1
    assert rd.run_due(s, now=NOW, mailer=mail) == 0                          # already gone
    assert len(mail.sent) == 1
    assert rd.sent_months(s, 1) == {"2026-10"}
    import db
    ev = s.query(db.ActivityEvent).filter(db.ActivityEvent.action == "monthly_report_sent").one()
    assert ev.actor == "system" and ev.details["kind"] == "scheduled"


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
                             "Errors still waiting", "Waiting list"]
    assert wb["Key numbers"]["A1"].value == "Monthly Report — October 2026"
    # The title is the box's first line, centred across the table; under it
    # the period in words and the dates it compares, then the table. Every
    # line of every table has a border; the notes under it have none.
    key = wb["Key numbers"]
    merged = [str(r) for r in key.merged_cells.ranges]
    assert {"A1:F1", "A2:F2", "A3:F3"} <= set(merged)
    assert key["A1"].alignment.horizontal == "center"
    assert key["A3"].value.startswith("Covers 1 – 31 Oct 2026, compared with 1 – 30 Sep 2026")
    assert key["A4"].value == "Measure" and key["A5"].value == "Timeliness"
    end = next(c.row for c in key["A"] if c.value == "Waiting longer than target at month end")
    assert key["A1"].border.top.style == "medium" and key["A4"].border.left.style == "medium"
    assert key.cell(row=end, column=6).border.bottom.style == "medium"
    assert key.cell(row=end + 2, column=1).value == "Green is better, red is worse, grey is only more or less work."
    assert key.cell(row=end + 2, column=1).border.left.style is None
    assert wb["Errors still waiting"]["A3"].border.left.style == "medium"
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
    assert rows["Arrived on time"][1] == 1.0 and rows["Arrived on time"][2] == 0.0
    assert rows["Arrived on time"][3] == "+100 pts"
    assert rows["Errors found"][1:4] == (3, 6, "-50%")
    assert rows["Arrived on time"][4] == "Not available"      # October 2025: before the records


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
                             "Errors still waiting", "Waiting list"]
    ws = wb["Key numbers"]
    assert ws["A1"].value == "Monthly Report — Week of 26 Oct 2026"
    assert ws["A3"].value == "Covers 26 Oct – 1 Nov 2026, compared with 19 – 25 Oct 2026 and 27 Oct – 2 Nov 2025."
    assert [c.value for c in ws[4]][:6] == ["Measure", "Week of 26 Oct 2026", "Week of 19 Oct 2026",
                                            "vs last week", "Week of 27 Oct 2025", "vs last year"]
    labels = [r[0] for r in ws.iter_rows(min_row=5, values_only=True) if r[0]]
    assert "Waiting to be fixed at the end of the week" in labels
    assert rd.render_pdf(pack, "Test Carrier").startswith(b"%PDF")
    assert [n for n, _d, _m in rd.files(pack, "both", "Test Carrier")] == [
        "Monthly Report week of 2026-10-26.pdf", "Monthly Report week of 2026-10-26.xlsx"]


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
    assert mail.sent[0]["attachments"][0][0] == "Monthly Report week of 2026-10-26.pdf"
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
    checks = wb["Checks that failed most"]
    assert (checks["B2"].value, checks["F2"].value) == ("Premium must be more than 0", 0.5)
    waiting = wb["Errors still waiting"]
    assert waiting["A1"].value.startswith("As at 4 Nov 2026: 2 waiting, 1 longer than the 5-day target")
    assert [waiting.cell(row=r, column=1).value for r in range(4, 8)] == [
        "0–2 days", "3–7 days", "8–30 days", "Over 30 days"]
    assert waiting["A10"].value == "Broker 8"
    listed = wb["Waiting list"]
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
