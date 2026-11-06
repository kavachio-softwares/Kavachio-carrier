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
    assert wb.sheetnames == ["Key numbers", "By broker and programme", "Last 6 months"]
    assert wb["Key numbers"]["A1"].value == "Monthly Report — October 2026"


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
