"""Plan task 43.2 / 43.3 — the policy list taken out of Kavachio (policy_export.py).

What is held: the Excel file is the list on screen, every page, in the screen's
columns, with what was picked written down; the email goes to the logins
ticked (only the seat's own) and the addresses typed, each once, and one
failed mailbox does not stop the rest; each download and send leaves a named
Audit Logs row — and a broker's row names no programme or carrier, because
every carrier the broker works for reads it.

Pure: in-memory SQLite and a recording mailer — no mail leaves, no shared DB.
Run with:  DATABASE_URL=sqlite:// .venv/bin/python -m pytest test_policy_export.py
"""
from __future__ import annotations

import datetime as dt
import io
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from openpyxl import load_workbook

import audit
import audit_feed
import policy_export as pe
import policy_search as ps
import report_delivery as rd
from auth_deps import Principal
from test_policy_search import export, files, row  # noqa: F401 — `files` is a fixture

NOW = dt.datetime(2026, 11, 18, 9, 30)
CARRIER = Principal(user_id=1, tenant_id=7, role="carrier_admin")
BROKER = Principal(user_id=2, tenant_id=None, role="broker_admin", broker_party_id=50)
FILE = "Policies 2026-11-18.xlsx"


def line(policy, **kw):
    """One policy as policy_search.search returns it."""
    return {"policy": policy, "insured": "Acme Ltd", "state": "TX", "start": "2026-01-01",
            "end": "2027-01-01", "transaction": "New", "premium": 1250.5, "currency": "USD",
            "first_period": "2026-09", "last_period": "2026-10", "rows": 2, "files": 2,
            "insurer_id": 7, "insurer": "Mahi", "program_id": 11, "programme": "Fleet",
            "broker_party_id": 50, "broker": "Wani", **kw}


def result(seat="carrier", items=None, drill=None):
    items = ([line("P-1"), line("P-2", start="not a date", premium=None, currency=None)]
             if items is None else items)
    return {"items": items, "total": len(items), "page": 1, "page_size": None, "seat": seat,
            "files": 3, "drill": drill,
            "filters": {"programmes": [{"id": 11, "name": "Fleet"}],
                        "brokers": [{"id": 50, "name": "Wani"}],
                        "insurers": [{"id": 7, "name": "Mahi"}], "states": ["TX"]}}


def filters(**kw):
    """policy_routes._filters with nothing picked, then `kw`."""
    f = {k: None for k in ("q", "program_id", "broker_party_id", "carrier_id", "state",
                           "date_from", "date_to", "from", "label", "period", "metric",
                           "currency", "month", "measure")}
    f.update(date_field="start", basis="month", window="current", open_problems="include")
    f.update(kw)
    return f


class Mailer:
    """Records each email, text and headers included; can fail for one address."""
    def __init__(self, fail_for=None):
        self.sent, self.fail_for = [], fail_for

    def __call__(self, to, subject, html, text=None, account="", attachments=None, **kw):
        if to == self.fail_for:
            raise RuntimeError("mailbox full")
        self.sent.append({"to": to, "subject": subject, "text": text,
                          "attachments": attachments or [], **kw})


@pytest.fixture
def s(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(eng, tables=[db.AppUser.__table__, db.ActivityEvent.__table__])
    sess = sessionmaker(bind=eng)()
    for uid, tenant, broker, email, name, role in [
            (1, 7, None, "ana@carrier.test", "Ana", "carrier_admin"),
            (3, 7, None, "raj@carrier.test", "Raj", "carrier_admin"),
            (4, 8, None, "zed@othercarrier.test", "Zed", "carrier_admin"),
            (2, None, 50, "bo@broker.test", "Bo", "broker_admin"),
            (5, None, 51, "cy@otherbroker.test", "Cy", "broker_admin")]:
        sess.add(db.AppUser(id=uid, tenant_id=tenant, broker_party_id=broker, email=email,
                            full_name=name, role=role, status="active"))
    sess.flush()
    # Names come from tables SQLite cannot build here; they are not what is tested.
    monkeypatch.setattr(rd, "carrier_name", lambda s, tid: "Mahi Insurance")
    monkeypatch.setattr(pe, "org_name",
                        lambda s, seat: "Mahi Insurance" if seat.kind == "carrier" else "Wani Brokers")
    monkeypatch.setattr(audit, "actor_for",
                        lambda p: f"broker:{p.broker_party_id}" if p.is_broker else "ana@carrier.test")
    monkeypatch.setattr(audit, "actor_columns", lambda p=None, user_id=None: {
        "actor_user_id": p.user_id, "actor_role": p.role,
        "actor_broker_party_id": p.broker_party_id})
    real_seat = ps.seat_of
    monkeypatch.setattr(ps, "seat_of", lambda s, p: (
        ps.Seat("broker", broker_party_id=50, program_ids={11}) if p.is_broker else real_seat(s, p)))
    return sess


def _events(s):
    import db
    return s.query(db.ActivityEvent).all()


def _sheet(data: bytes, name: str):
    return load_workbook(io.BytesIO(data))[name]


# ── the file ────────────────────────────────────────────────────────────────
def test_the_file_is_every_policy_in_the_screens_columns():
    ws = _sheet(pe.workbook(result(), filters(), org="Mahi Insurance", by="Ana", at=NOW), "Policies")
    head = [c.value for c in ws[1]]
    assert head[:5] == ["Policy number", "Insured", "Programme", "Broker", "State"]
    assert "Carrier" not in head
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    col = head.index
    assert [r[0] for r in rows] == ["P-1", "P-2"]
    assert rows[0][col("Cover start")] == dt.datetime(2026, 1, 1)     # a real date: sorts in Excel
    assert rows[1][col("Cover start")] == "not a date"                # the file's own text otherwise
    assert rows[0][col("Premium")] == 1250.5 and rows[1][col("Premium")] is None
    assert rows[0][col("Latest bordereau")] == "October 2026"
    assert ws.freeze_panes == "B2" and ws.auto_filter.ref


def test_a_brokers_file_names_the_carrier_not_the_broker():
    head = [c.value for c in _sheet(pe.workbook(result("broker"), filters(), org="Wani Brokers",
                                                 by="Bo", at=NOW), "Policies")[1]]
    assert "Carrier" in head and "Broker" not in head


def test_the_file_says_what_was_picked_by_whom_and_when():
    f = filters(q="acme", program_id=11, broker_party_id=50, state="TX",
                date_from="2026-01-01", date_to="2026-03-31")
    info = dict(_sheet(pe.workbook(result(), f, org="Mahi Insurance", by="Ana", at=NOW),
                       "About this list").iter_rows(values_only=True))
    assert info["Policies"] == "2 policies"
    assert (info["From"], info["Taken out by"], info["On"]) == ("Mahi Insurance", "Ana",
                                                                  "18 Nov 2026, 09:30 UTC")
    assert info["Read from"] == "3 processed bordereaux, each as its latest version"
    assert (info["Search"], info["Programme"], info["Broker"], info["State"]) == ("acme", "Fleet", "Wani", "TX")
    assert info["Cover start"] == "1 Jan 2026 – 31 Mar 2026"
    every = dict(_sheet(pe.workbook(result(), filters(), org="M", by=None, at=NOW),
                        "About this list").iter_rows(values_only=True))
    assert every["Filters"] == "None — every policy"


def test_a_number_clicked_on_a_report_is_named_but_not_on_a_brokers_record():
    r = result(drill={"source": "monthly", "label": "New policies", "period_name": "October 2026",
                      "files": 3, "rows": 9})
    assert ("Behind", "New policies · October 2026") in pe.picked(r, filters())
    assert ("Behind", "a number on a report · October 2026") in pe.picked(r, filters(), names=False)


def test_every_page_comes_back_when_no_page_size_is_given(files):
    files.exports = [export(1, "2026-10", [row(f"P-{i:02d}") for i in range(30)])]
    everything = ps.search(None, CARRIER, page_size=None)
    assert everything["total"] == 30 and len(everything["items"]) == 30
    assert len(ps.search(None, CARRIER)["items"]) == 25                # the screen's page is unchanged


# ── the record (43.3) ───────────────────────────────────────────────────────
def test_a_download_is_recorded_with_its_filters(s):
    name, data = pe.export(s, CARRIER, result(), filters(program_id=11, state="TX"), now=NOW)
    assert name == FILE and data[:2] == b"PK"                          # an xlsx (zip) file
    (e,) = _events(s)
    assert (e.action, e.tenant_id, e.target, e.actor_user_id) == ("policies_exported", 7, "policies", 1)
    assert e.details == {"policies": 2, "programme": "Fleet", "state": "TX", "filename": FILE}
    assert audit_feed._detail_words(e.details) == "2 policies · programme Fleet · state TX"
    assert audit_feed.ACTION_WORDS["policies_exported"] == "Downloaded the policy list"
    assert "policies_exported" in audit_feed.actions_for_group("downloads")
    assert "activity" in audit_feed.categories_for_group("downloads")


def test_a_brokers_record_names_no_programme_or_carrier(s):
    pe.export(s, BROKER, result("broker"), filters(program_id=11, carrier_id=7, q="acme"), now=NOW)
    (e,) = _events(s)
    # Every carrier the broker works for reads this row, so only what the broker typed.
    assert e.details == {"policies": 2, "search": "acme", "filename": FILE}
    assert (e.tenant_id, e.actor_broker_party_id) == (7, 50)           # the carrier it had picked


def test_an_empty_list_cannot_be_taken_out(s):
    with pytest.raises(ValueError, match="no policies"):
        pe.export(s, CARRIER, result(items=[]), filters(), now=NOW)
    with pytest.raises(ValueError, match="no policies"):
        pe.send(s, CARRIER, result(items=[]), filters(), user_ids=[1], emails=[], now=NOW, mailer=Mailer())
    assert _events(s) == []


# ── the email ───────────────────────────────────────────────────────────────
def test_send_goes_to_the_logins_ticked_and_the_addresses_typed_each_once(s):
    m = Mailer()
    r = pe.send(s, CARRIER, result(), filters(), user_ids=[3],
                emails=["cfo@board.test", "RAJ@carrier.test"], now=NOW, mailer=m)
    assert [x["to"] for x in m.sent] == ["raj@carrier.test", "cfo@board.test"]
    assert (r["policies"], r["delivered"], r["failed"]) == (2, 2, 0)
    name, data, mime = m.sent[0]["attachments"][0]
    assert (name, mime) == (FILE, pe.XLSX)
    assert _sheet(data, "Policies").max_row == 3                       # header + both policies
    assert m.sent[0]["subject"] == "Kavachio - Policy list, 2 policies"
    # From the carrier, and a reply goes to the person who sent it.
    assert m.sent[0]["sender_name"] == "Mahi Insurance via Kavachio"
    assert m.sent[0]["reply_to"] == "ana@carrier.test"
    (e,) = _events(s)
    assert e.action == "policies_sent"
    assert e.details["to"] == ["raj@carrier.test", "cfo@board.test"] and "failed" not in e.details


def test_only_your_own_logins_can_be_ticked(s):
    assert [p["user_id"] for p in pe.logins(s, ps.Seat("carrier", tenant_id=7))] == [1, 3]
    assert [p["user_id"] for p in pe.logins(s, ps.Seat("broker", broker_party_id=50))] == [2]
    # Another carrier's login and two brokers' are not this carrier's to tick.
    with pytest.raises(ValueError, match="Choose at least one"):
        pe.send(s, CARRIER, result(), filters(), user_ids=[4, 2, 5], emails=[], now=NOW, mailer=Mailer())
    m = Mailer()
    pe.send(s, BROKER, result("broker"), filters(), user_ids=None, emails=[], now=NOW, mailer=m)
    assert [x["to"] for x in m.sent] == ["bo@broker.test"]             # None = every own login


def test_one_failed_mailbox_does_not_stop_the_rest(s):
    r = pe.send(s, CARRIER, result(), filters(), user_ids=[1, 3], emails=[], now=NOW,
                mailer=Mailer(fail_for="ana@carrier.test"))
    assert (r["delivered"], r["failed"]) == (1, 1)
    assert [(x["email"], x["delivered"]) for x in r["recipients"]] == [
        ("ana@carrier.test", False), ("raj@carrier.test", True)]
    (e,) = _events(s)
    assert e.details["failed"] == 1


def test_a_bad_address_is_named_and_nothing_goes(s):
    m = Mailer()
    with pytest.raises(ValueError, match="not-an-email"):
        pe.send(s, CARRIER, result(), filters(), user_ids=[1], emails=["not-an-email"], now=NOW, mailer=m)
    assert m.sent == [] and _events(s) == []


def test_only_a_login_gets_the_link_back_to_the_same_list(s, monkeypatch):
    import notifications
    monkeypatch.setattr(notifications, "_app_link", lambda path: "https://app.test" + path)
    m = Mailer()
    pe.send(s, CARRIER, result(), filters(program_id=11, q="acme"), user_ids=[1],
            emails=["cfo@board.test"], now=NOW, mailer=m)
    login, outside = m.sent
    assert "https://app.test/policies?q=acme&program=11" in login["text"]
    assert "https://app.test" not in outside["text"]
