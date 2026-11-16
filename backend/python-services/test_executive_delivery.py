"""Executive Reporting 38.2 — sending the Executive Report (executive_delivery.py).

What is held: it goes to the logins ticked AND the outside addresses typed,
each once; a bad address is named, not silently dropped; one failed mailbox
does not stop the rest; the daily job sends each week or month once; a sent
report keeps the numbers it went out with; both files render.

Run with:  DATABASE_URL=sqlite:// .venv/bin/python -m pytest test_executive_delivery.py
No mail leaves: every send is given a recording `mailer` instead of SMTP.
"""
from __future__ import annotations

import io
from datetime import date, datetime

import pytest

import executive_delivery as ed
import executive_report as er
import report_delivery as rd
from test_report_delivery import Mailer, _user

NOW = datetime(2026, 11, 12, 6, 0)          # a Thursday


def _session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(
        eng, tables=[db.ExecutiveReportSetting.__table__, db.ExecutiveReportSend.__table__,
                     db.AppUser.__table__, db.ActivityEvent.__table__])
    return sessionmaker(bind=eng)()


def _pack(cadence="month", period="2026-10"):
    w = er.window(cadence, period, NOW.date())
    ops = {"files": 12, "rows": 340, "errors": 9, "critical": 2, "clean_first_time": 9,
           "clean_first_time_pct": 75.0}
    return {
        "cadence": cadence, "period": w["period"], "from": w["start"].isoformat(),
        "to": (w["end"] - __import__("datetime").timedelta(days=1)).isoformat(),
        "previous": {"period": w["previous"]["period"]}, "complete": True,
        "as_of": NOW.date().isoformat(), "as_at": w["as_at"].isoformat(),
        "money": {"month": w["money_month"], "currency": "USD", "files": 3, "other_currencies": 0,
                  "gross_premium": {"current": 125000.0, "previous": 100000.0, "change": 25.0},
                  "commission": {"current": 12500.0, "previous": None, "change": None},
                  "top_programmes": [{"id": 1, "name": "Fleet & Cargo", "gross_premium": 125000.0}],
                  "top_brokers": [{"id": 5, "name": "CRC", "gross_premium": 125000.0}]},
        "operations": {**ops, "previous": {**ops, "files": 10, "clean_first_time_pct": 70.0},
                       "change": {}},
        "timeliness": {"on_time_pct": 80.0, "previous_on_time_pct": 90.0},
        "problems": {"open": 7, "overdue": 3, "escalations": 1, "by_type": [],
                     "escalation_list": [{"type_label": "Dispute", "title": "July file",
                                          "owner": "Carrier", "age_days": 4,
                                          "due_on": "2026-11-01", "overdue": True}]},
        "portfolio": {"total": 6, "active": 4, "runoff": 1, "not_live": 1, "by_stage": []},
        "trend": [{"month": "2026-10", "files": 12, "errors": 9, "clean_first_time_pct": 75.0}],
        "reinsurance": {"due": 2, "sent_on_time": 1, "sent_late": 0,
                        "not_sent": 1, "overdue": 1, "statements": [
            {"treaty": "Cat XL", "period": "2026-09", "due_on": "2026-10-30", "status": "Not sent",
             "sent_on": None, "overdue": True},
            {"treaty": "QS", "period": "2026-09", "due_on": "2026-10-30", "status": "Sent on time",
             "sent_on": "2026-10-29", "overdue": False}]},
    }


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr(rd, "carrier_name", lambda s, tid: "Test Carrier")
    monkeypatch.setattr(er, "pack", lambda s, tid, cadence="month", period=None, today=None:
                        _pack(cadence, period))


# ── names, numbers, files ───────────────────────────────────────────────────
def test_period_titles_read_as_words():
    assert ed.period_title("month", "2026-10") == "October 2026"
    assert ed.period_title("week", "2026-11-02") == "Week of 2 Nov 2026"


def test_the_headline_says_each_number_and_its_change():
    h = dict(ed.headline(_pack()))
    assert h["Gross premium"] == "USD 125,000 (+25% vs last month)"
    assert h["Commission"] == "USD 12,500"
    assert dict(ed.headline({**_pack(), "money": {**_pack()["money"], "commission": {"current": None, "previous": 5.0, "change": None}}}))["Commission"] == "Not reported"
    assert h["Files processed"] == "12 (+20% vs last month)"
    assert h["On time"] == "80% (-10 pts vs last month)"
    assert h["Open problems"] == "7 (3 overdue)" and h["Escalations"] == "1"


def test_a_weekly_headline_says_premium_is_the_month_so_far():
    h = dict(ed.headline(_pack("week", "2026-11-02")))
    assert "Gross premium so far" in h and "vs last week" in h["Files processed"]


def test_both_files_render_every_section():
    from openpyxl import load_workbook
    pdf, xlsx = ed.files(_pack(), "both", "Smith & Co")
    assert pdf[0] == "Executive Report 2026-10.pdf" and pdf[1][:4] == b"%PDF"
    wb = load_workbook(io.BytesIO(xlsx[1]))
    assert wb.sheetnames == ["Headline", "Top programmes by premium", "Top brokers by premium",
                             "Programmes", "Errors by month", "Needs attention",
                             "Reinsurance statements due"]
    assert wb["Reinsurance statements due"]["D3"].value == "Sent on time (2026-10-29)"
    assert wb["Needs attention"]["A2"].value == "Dispute: July file"
    assert wb["Headline"]["A3"].value == ("Open problems, escalations, programmes and reinsurance "
                                          "statements are as they stood at the end of 31 Oct 2026.")


def test_a_weekly_file_says_premium_is_the_month_so_far():
    assert "Premium and on time are for November 2026 so far." in ed.scope_note(_pack("week", "2026-11-02"))
    assert "so far" not in ed.scope_note(_pack())
    assert "for October 2026 so far" in ed.scope_note(_pack("custom", "2026-10-01..2026-10-15"))


def test_day_and_custom_titles_and_comparisons():
    assert ed.period_title("day", "2026-11-11") == "11 Nov 2026"
    assert ed.period_title("custom", "2026-10-01..2026-10-15") == "1 – 15 Oct 2026"
    assert ed.period_title("custom", "2026-09-28..2026-10-04") == "28 Sep – 4 Oct 2026"
    assert ed.period_title("custom", "2025-12-20..2026-01-05") == "20 Dec 2025 – 5 Jan 2026"
    assert "vs the day before" in dict(ed.headline(_pack("day", "2026-11-11")))["Files processed"]
    assert "vs the period before" in dict(ed.headline(_pack("custom", "2026-10-01..2026-10-15")))["Files processed"]
    assert ed.files(_pack("custom", "2026-10-01..2026-10-15"), "pdf", "C")[0][0] == \
        "Executive Report 2026-10-01 to 2026-10-15.pdf"


def test_the_reinsurance_line_names_only_what_happened():
    assert ed.reinsurance_line({"due": 3, "sent_late": 1, "not_sent": 0}) == "3 due · 1 sent late"


def test_a_daily_report_is_due_every_day_for_yesterday():
    cfg = {"cadence": "day", "week_day": 0, "send_day": 3}
    assert ed.due_now(cfg, date(2026, 11, 15)) == ("day", "2026-11-14")      # a Sunday too
    assert ed.next_send(cfg, date(2026, 11, 12), set())["period"] == "2026-11-11"
    assert ed.next_send(cfg, date(2026, 11, 12), {"day:2026-11-11"})["send_on"] == "2026-11-13"


def test_custom_dates_cannot_be_scheduled():
    s = _session()
    with pytest.raises(ValueError):
        ed.save_settings(s, 1, {"cadence": "custom"}, None)


# ── who ─────────────────────────────────────────────────────────────────────
def test_typed_addresses_are_split_trimmed_and_kept_once():
    assert ed.clean_emails("ceo@board.com, CFO@board.com\nceo@BOARD.com; x@y.org") == [
        "ceo@board.com", "CFO@board.com", "x@y.org"]


def test_an_address_that_is_not_one_is_named():
    with pytest.raises(ValueError) as e:
        ed.clean_emails(["ceo@board.com", "not-an-email"])
    assert "not-an-email" in str(e.value)


def test_it_goes_to_the_logins_ticked_and_the_outside_addresses_each_once():
    s = _session()
    _user(s, 1, name="Dana")
    _user(s, 2, name="Lee")
    _user(s, 3, role="broker_admin")                     # never the broker
    to = ed.recipients(s, 1, [1], ["ceo@board.com", "u1@carrier.test"])
    assert [(p["user_id"], p["email"]) for p in to] == [(1, "u1@carrier.test"), (None, "ceo@board.com")]
    assert {p["email"] for p in ed.recipients(s, 1, None, [])} == {"u1@carrier.test", "u2@carrier.test"}


# ── sending ─────────────────────────────────────────────────────────────────
def test_send_now_emails_everyone_the_file_and_keeps_the_numbers_as_sent():
    from db import ExecutiveReportSend
    s = _session()
    _user(s, 1, name="Dana")
    m = Mailer()
    r = ed.send(s, 1, cadence="month", period="2026-10", kind="now", user_id=1,
                user_ids=[1], emails=["ceo@board.com"], file_format="pdf", now=NOW, mailer=m)
    assert r["status"] == "sent" and r["delivered"] == 2
    assert [x["to"] for x in m.sent] == ["u1@carrier.test", "ceo@board.com"]
    assert m.sent[0]["subject"] == "Kavachio - Executive Report, October 2026"
    assert m.sent[0]["attachments"][0][0] == "Executive Report 2026-10.pdf"
    row = s.get(ExecutiveReportSend, r["id"])
    assert row.pack["money"]["gross_premium"]["current"] == 125000.0
    assert [x["email"] for x in ed.history(s, 1)[0]["recipients"]] == ["u1@carrier.test", "ceo@board.com"]


def test_a_week_is_named_in_the_subject():
    s = _session()
    _user(s, 1)
    m = Mailer()
    ed.send(s, 1, cadence="week", period="2026-11-04", kind="now", user_ids=[1], now=NOW, mailer=m)
    assert m.sent[0]["subject"] == "Kavachio - Executive Report, week of 2 Nov 2026"


def test_one_bad_mailbox_does_not_stop_the_others():
    s = _session()
    _user(s, 1)
    r = ed.send(s, 1, cadence="month", period="2026-10", kind="now", user_ids=[1],
                emails=["ceo@board.com"], now=NOW, mailer=Mailer(fail_for="u1@carrier.test"))
    assert (r["status"], r["delivered"], r["failed"]) == ("partly", 1, 1)


def test_nobody_to_send_to_is_said_plainly():
    s = _session()
    with pytest.raises(ValueError) as e:
        ed.send(s, 1, cadence="month", period="2026-10", kind="now", user_ids=[], emails=[],
                now=NOW, mailer=Mailer())
    assert str(e.value) == ed.NO_ONE


# ── the schedule ────────────────────────────────────────────────────────────
def test_until_saved_it_is_not_sent_automatically():
    s = _session()
    assert ed.settings(s, 1)["auto_send"] is False
    assert ed.run_due(s, now=NOW, mailer=Mailer()) == 0


def test_the_schedule_is_checked():
    s = _session()
    for bad in ({"cadence": "fortnight"}, {"week_day": 6}, {"send_day": 9}, {"file_format": "csv"},
                {"emails": ["nope"]}, {"auto_send": True, "user_ids": [], "emails": []}):
        with pytest.raises(ValueError):
            ed.save_settings(s, 1, bad, None)


def test_a_weekly_report_is_due_from_its_weekday_until_the_week_ends():
    cfg = {"cadence": "week", "week_day": 2, "send_day": 3}
    assert ed.due_now(cfg, date(2026, 11, 10)) is None                    # Tuesday
    assert ed.due_now(cfg, date(2026, 11, 11)) == ("week", "2026-11-02")  # Wednesday
    assert ed.due_now(cfg, date(2026, 11, 15)) == ("week", "2026-11-02")  # Sunday


def test_a_monthly_report_is_due_on_its_working_day():
    cfg = {"cadence": "month", "week_day": 0, "send_day": 3}
    assert ed.due_now(cfg, date(2026, 11, 3)) is None
    assert ed.due_now(cfg, date(2026, 11, 4)) == ("month", "2026-10")


def test_the_daily_job_sends_each_week_once_to_the_saved_people():
    from db import ActivityEvent
    s = _session()
    _user(s, 1)
    ed.save_settings(s, 1, {"auto_send": True, "cadence": "week", "week_day": 0,
                            "user_ids": [1], "emails": ["ceo@board.com"]}, 1)
    s.commit()
    m = Mailer()
    assert ed.run_due(s, now=NOW, mailer=m) == 1
    assert ed.run_due(s, now=NOW, mailer=m) == 0                 # already gone
    assert [x["to"] for x in m.sent] == ["u1@carrier.test", "ceo@board.com"]
    assert s.query(ActivityEvent).filter_by(action="executive_report_sent").count() == 1
    assert ed.next_send(ed.settings(s, 1), NOW.date(), ed.sent_keys(s, 1))["send_on"] == "2026-11-16"


def test_clear_all_hides_the_list_but_keeps_every_send():
    from db import ExecutiveReportSend
    s = _session()
    _user(s, 1)
    ed.save_settings(s, 1, {"auto_send": True, "cadence": "week", "week_day": 0,
                            "user_ids": [1]}, 1)
    s.commit()
    m = Mailer()
    assert ed.run_due(s, now=NOW, mailer=m) == 1
    ed.send(s, 1, cadence="month", period="2026-10", kind="now", user_id=1,
            user_ids=[1], emails=[], file_format="pdf", now=NOW, mailer=m)
    ed.send(s, 2, cadence="month", period="2026-10", kind="now", user_id=1,
            user_ids=[], emails=["x@other.test"], file_format="pdf", now=NOW, mailer=m)
    s.commit()
    assert len(ed.history(s, 1)) == 2
    assert ed.clear_history(s, 1, now=NOW) == 2
    s.commit()
    assert ed.history(s, 1) == []                                 # gone from the list
    assert s.query(ExecutiveReportSend).filter_by(tenant_id=1).count() == 2   # not deleted
    assert len(ed.history(s, 2)) == 1                             # another carrier's untouched
    assert ed.run_due(s, now=NOW, mailer=m) == 0                  # the week is still counted as sent
    assert ed.clear_history(s, 1) == 0                            # nothing left to clear
    ed.send(s, 1, cadence="month", period="2026-11", kind="now", user_id=1,
            user_ids=[1], emails=[], file_format="pdf", now=NOW, mailer=m)
    s.commit()
    assert [h["period"] for h in ed.history(s, 1)] == ["2026-11"]  # new sends show again


class _Html:
    """Keeps each email's HTML, text and attachment names."""
    def __init__(self):
        self.sent = {}

    def __call__(self, to, subject, html, text=None, account="", attachments=None,
                 sender_name=None, reply_to=None, **_):
        self.sent[to] = {"html": html, "text": text, "files": [a[0] for a in attachments or []],
                         "from": sender_name, "reply_to": reply_to}


def test_the_email_is_the_whole_report_in_the_format_picked(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://app.kavachio.test")
    s = _session()
    _user(s, 1, name="Dana")
    m = _Html()
    ed.send(s, 1, cadence="month", period="2026-10", kind="now", user_id=1, user_ids=[1],
            emails=["ceo@board.com"], file_format="excel", now=NOW, mailer=m)
    out, login = m.sent["ceo@board.com"], m.sent["u1@carrier.test"]
    assert out["files"] == ["Executive Report 2026-10.xlsx"]
    assert "attached as an Excel file: Executive Report 2026-10.xlsx" in out["html"]
    # Everything is in the email itself, so no login is needed to read it.
    for heading in ("Top programmes by premium", "Programmes", "Needs attention", "Reinsurance statements due"):
        assert heading in out["html"]
    assert "Fleet &amp; Cargo" in out["html"] and "Cat XL" in out["html"]
    # Only a company login is offered Kavachio; an outside address gets no link at all.
    assert "Open in Kavachio" in login["html"] and "app.kavachio.test/executive-report" in login["html"]
    assert "Open in Kavachio" not in out["html"] and "app.kavachio.test" not in out["html"] + (out["text"] or "")
    assert "Report</span>" in out["html"] and "Action needed" not in out["html"]


def test_both_formats_are_named(monkeypatch):
    assert ed.attached_line([("a.pdf", b"", ""), ("a.xlsx", b"", "")]) == \
        "The full report is attached as a PDF: a.pdf and an Excel file: a.xlsx."


def test_it_comes_from_the_carrier_and_replies_go_to_its_person():
    s = _session()
    _user(s, 1, name="Vaishnavi")
    m = _Html()
    ed.send(s, 1, cadence="month", period="2026-10", kind="now", user_id=1, user_ids=[],
            emails=["ceo@board.com"], now=NOW, mailer=m)
    assert m.sent["ceo@board.com"]["from"] == "Test Carrier via Kavachio"
    assert m.sent["ceo@board.com"]["reply_to"] == "u1@carrier.test"
    assert "Sent by Vaishnavi from Test Carrier on Kavachio." in m.sent["ceo@board.com"]["html"]


def test_an_automatic_send_replies_to_whoever_set_the_schedule():
    s = _session()
    _user(s, 1)
    _user(s, 2, name="Lee")
    ed.save_settings(s, 1, {"auto_send": True, "cadence": "week", "week_day": 0,
                            "user_ids": [1], "emails": ["ceo@board.com"]}, 2)
    s.commit()
    m = _Html()
    ed.run_due(s, now=NOW, mailer=m)
    assert {v["reply_to"] for v in m.sent.values()} == {"u2@carrier.test"}
    assert "Sent automatically from Test Carrier on Kavachio." in m.sent["ceo@board.com"]["html"]


def test_the_from_line_names_the_carrier_but_keeps_kavachios_address(monkeypatch):
    import smtplib
    import email_utils
    caught = []

    class _SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, *a): pass
        def send_message(self, msg): caught.append(msg)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _SMTP)
    monkeypatch.delenv("MAIL_ALLOWED_RECIPIENTS", raising=False)
    monkeypatch.setenv("NOTIFY_SMTP_USER", "notifications@kavachio.test")
    monkeypatch.setenv("NOTIFY_SMTP_PASS", "x")
    email_utils.send_email("ceo@board.com", "s", "<p>x</p>", account="NOTIFY",
                           sender_name="Accel via Kavachio", reply_to="vaishnavi@accel.com")
    assert caught[0]["From"] == "Accel via Kavachio <notifications@kavachio.test>"
    assert caught[0]["Reply-To"] == "vaishnavi@accel.com"


def test_the_errors_table_is_cut_the_way_the_period_was():
    days = [{"unit": "day", "label": "2 Nov", "from": "2026-11-02T00:00:00", "to": "2026-11-03T00:00:00",
             "files": 4, "errors": 1, "clean_first_time_pct": 75.0}]
    title, rows = ed.trend_section(days)
    assert title == "Errors by day" and rows[0][0] == "Day" and rows[1][:3] == ["2 Nov", "4", "1"]
    hours = [dict(days[0], unit="hour", label="09:00")]
    assert ed.trend_section(hours)[0] == "Errors by hour"
    # A report sent before keeps reading as it was sent.
    assert ed.trend_section([{"day": "2026-11-02", "files": 1, "errors": 0,
                              "clean_first_time_pct": None}])[1][1][0] == "2 Nov 2026"


def _full_pack():
    p = _pack()
    p["quality"] = {"clean": 9, "with_errors": 2, "not_checked": 1,
                    "top_errors": [{"check": "Premium is a number", "files": 2, "errors": 7, "severity": "critical"}],
                    "by_broker": [{"id": 5, "name": "CRC", "files": 12, "errors": 9, "clean_first_time_pct": 75.0}],
                    "by_programme": []}
    p["late"] = {"late": 1, "missing": 1, "never": 0, "total": 2, "list": [
        {"broker": "CRC", "programme": "Fleet & Cargo", "month": "2026-09", "due_on": "2026-10-15",
         "arrived_on": "2026-10-20", "status": "Arrived late", "days_late": 5},
        {"broker": "CRC", "programme": "Fleet & Cargo", "month": "2026-Q3", "due_on": "2026-10-20",
         "arrived_on": None, "status": "Not yet", "days_late": 11}]}
    p["contracts"] = {"bound": 3, "with_files": 2, "without_files": 1,
                      "idle": [{"contract_id": 2, "contract": "Marine 2026", "broker": "CRC", "programme": "Fleet & Cargo"}],
                      "capped": 1, "no_cap": 2, "over_cap": 0, "near_cap": 1,
                      "caps": [{"contract_id": 1, "contract": "Fleet 2026", "broker": "CRC", "programme": "Fleet & Cargo",
                                "currency": "USD", "cap": 1000000.0, "written": 900000.0, "used_pct": 90.0,
                                "flag": "Near cap", "other_currency_rows": 0}]}
    return p


def test_the_new_sections_reach_both_files_and_the_email():
    from openpyxl import load_workbook
    p = _full_pack()
    _pdf, xlsx = ed.files(p, "both", "Smith & Co")
    wb = load_workbook(io.BytesIO(xlsx[1]))
    assert wb.sheetnames == ["Headline", "Files by status", "Top programmes by premium", "Top brokers by premium",
                             "Programmes", "Errors by month", "Most common errors", "Data quality by broker",
                             "Data quality by programme", "Late submissions", "Contracts sending files",
                             "Premium cap used", "Needs attention", "Reinsurance statements due"]
    assert [c.value for c in wb["Files by status"]["B"]][1:] == ["9", "2", "1"]
    late = wb["Late submissions"]
    assert [c.value for c in late[2]] == ["CRC", "Fleet & Cargo", "September 2026", "2026-10-15", "2026-10-20", "5"]
    assert late["C3"].value == "2026-Q3" and late["E3"].value == "Not yet"
    assert wb["Data quality by programme"]["A2"].value == "No files processed"
    assert [c.value for c in wb["Premium cap used"][2]] == ["Fleet 2026", "CRC", "USD 1,000,000", "USD 900,000", "90%", "Near cap"]
    assert wb["Premium cap used"]["A3"].value == "2 contracts have no premium cap set"
    assert wb["Contracts sending files"]["A2"].value == "Marine 2026"
    html = ed.sections_html(p)
    assert "Late submissions — 1 arrived late · 1 still missing" in html
    assert "Contracts sending files — 2 of 3" in html and "Most common errors" in html


def test_the_scorecards_replace_data_quality_in_the_files():
    from openpyxl import load_workbook
    p = _full_pack()
    p["scorecard"] = {
        "broker": {"total": 12, "rows": [{"id": 5, "name": "CRC", "premium": 125000.0, "files": 12, "errors": 9,
                                          "clean_first_time_pct": 60.0, "prev_clean_first_time_pct": 75.0,
                                          "due": 4, "on_time_pct": 75.0, "prev_on_time_pct": 50.0,
                                          "late_or_missing": 1}]},
        "programme": {"total": 0, "rows": []}}
    wb = load_workbook(io.BytesIO(ed.files(p, "excel", "Smith & Co")[0][1]))
    assert "Data quality by broker" not in wb.sheetnames
    sh = wb["Broker scorecard"]
    assert [c.value for c in sh[1]] == ["Broker", "Premium", "Files", "On time", "Clean first time", "Late or missing"]
    # Clean first time fell, so it says from what; on time rose, so nothing is added.
    assert [c.value for c in sh[2]] == ["CRC", "USD 125,000", "12", "75%", "60% (down from 75%)", "1"]
    assert wb["Programme scorecard"]["A2"].value == "No programme had files, premium or files due"
    assert "Broker scorecard — 1 of 12" in ed.sections_html(p)
