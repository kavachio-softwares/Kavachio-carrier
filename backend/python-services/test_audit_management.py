"""Audit Management (audit_management.py, 53.1 / 53.2), Partner Monitoring's
risk level (audit_history.risk_of, 54.1) and the audit package's period
(audit_package._window, 50.1). Pure: an in-memory sqlite database, nothing shared.

Run with:  DATABASE_URL=sqlite:// .venv/bin/python -m pytest test_audit_management.py
"""
import datetime as dt

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import audit_feed as af
import audit_history as ah
import audit_management as am
import audit_package as ap
from db import AppUser, Contract, Party, Tenant

TODAY = dt.date(2026, 11, 19)


@pytest.fixture
def s(monkeypatch):
    eng = create_engine("sqlite://")
    for model in (Tenant, Party, Contract, AppUser):
        model.__table__.create(eng)
    am.AuditBase.metadata.create_all(eng)
    S = sessionmaker(bind=eng)()
    S.add_all([Party(id=50, party_type="broker", legal_name="Acme"),
               Party(id=51, party_type="broker", legal_name="Global"),
               Contract(id=9, tenant_id=7, broker_party_id=50, name="Acme binder"),
               Contract(id=10, tenant_id=7, broker_party_id=51, name="Global binder"),
               Tenant(id=7, tenant_name="ins", legal_name="Insurer Ltd"),
               AppUser(id=1, email="c@x.test", full_name="Dana", role="carrier_admin", tenant_id=7),
               AppUser(id=251, email="b@x.test", full_name="Bo", role="broker_admin", broker_party_id=50)])
    S.commit()
    monkeypatch.setattr(af, "brokers_of_carrier", lambda s, tid: {50, 51} if tid == 7 else set())
    monkeypatch.setattr(am, "check_file", lambda name, data: None)
    import storage
    monkeypatch.setattr(storage, "store_or_keep", lambda cat, tid, name, data, ct=None: (None, data))
    yield S
    S.close()


def _audit(s, **k):
    body = {"name": "Annual audit 2026", "type": "annual", "broker_party_id": 50, "start_date": "2026-12-01",
            "due_date": "2026-12-31", **k}
    a = am.create_audit(s, 7, 1, body)
    s.commit()
    return a


def test_the_form_says_what_is_missing_in_words(s):
    with pytest.raises(HTTPException) as e:
        am.create_audit(s, 7, 1, {"name": " ", "type": "nope", "broker_party_id": 99})
    assert e.value.detail["errors"] == {"name": "Audit name is needed.", "type": "Pick the type of audit.",
                                        "broker_party_id": "Pick one of your brokers."}


def test_a_contract_must_be_that_brokers_and_dates_in_order(s):
    with pytest.raises(HTTPException) as e:
        _audit(s, contract_id=10, due_date="2026-11-01")
    assert set(e.value.detail["errors"]) == {"contract_id", "due_date"}
    assert _audit(s, contract_id=9).contract_id == 9


def test_planned_until_it_starts_or_gets_a_finding(s):
    a = _audit(s)
    assert am.shown_status(a, False, TODAY) == "planned"
    am.add_finding(s, a, 1, {"title": "Files late", "severity": "high"})
    s.commit()
    assert a.status == "in_progress" and am.shown_status(a, True, TODAY) == "in_progress"


def test_a_finding_closes_only_with_how_it_was_fixed_and_the_audit_after(s):
    a = _audit(s)
    f = am.add_finding(s, a, 1, {"title": "Files late", "severity": "critical", "due_date": "2026-11-01"})
    s.commit()
    assert am.overdue(f.due_date, f.status, TODAY)
    with pytest.raises(HTTPException) as e:
        am.close_audit(s, a, 1, None)
    assert "Close its 1 open finding first." == e.value.detail
    with pytest.raises(HTTPException):
        am.set_status(s, a, f, 1, "closed", "  ")
    am.set_status(s, a, f, 1, "closed", "Checked November")
    am.close_audit(s, a, 1, "Done")
    s.commit()
    assert (f.status, f.resolution, a.status) == ("closed", "Checked November", "closed")
    # Reopening a finding reopens its audit.
    am.set_status(s, a, f, 1, "open", None)
    assert a.status == "in_progress" and f.closed_at is None
    kinds = [e.kind for e in s.query(am.FindingEvent).order_by(am.FindingEvent.id)]
    assert kinds == ["created", "closed", "reopened"]


def test_a_broker_reply_moves_an_open_finding_on_and_names_kavachio(s):
    a = _audit(s)
    f = am.add_finding(s, a, 1, {"title": "Files late", "severity": "medium"})
    s.commit()
    am.reply(s, f, 251, "broker", "Fixed from November", "proof.csv", b"a,b\n1,2\n")
    s.commit()
    assert f.status == "in_progress"
    out = am.broker_findings(s, 50)
    steps = out["findings"][0]["history"]
    assert [x["title"] for x in steps] == ["Finding raised", "Evidence added", "Status changed"]
    assert steps[0]["by"] == "Insurer Ltd" and steps[1]["by"] == "Bo" and steps[2]["by"] == "Kavachio · Automatic"
    assert steps[1]["file"] == "proof.csv"
    assert am.evidence_bytes(s, f, [e for e in steps if e["file"]][0]["id"]) == ("proof.csv", b"a,b\n1,2\n")


def test_a_broker_never_reaches_another_brokers_finding(s):
    a = _audit(s)
    f = am.add_finding(s, a, 1, {"title": "x", "severity": "low"})
    s.commit()
    assert am.broker_findings(s, 51)["findings"] == []
    with pytest.raises(HTTPException) as e:
        am.broker_finding(s, 51, f.id)
    assert e.value.status_code == 404


def test_open_findings_feed_partner_monitoring(s, monkeypatch):
    a = _audit(s)
    am.add_finding(s, a, 1, {"title": "x", "severity": "critical"})
    am.add_finding(s, a, 1, {"title": "y", "severity": "low"})
    s.commit()
    assert am.open_findings_by_broker(s, 7) == {50: {"open": 2, "critical": 1}}


@pytest.mark.parametrize("late,missing,open_,crit,level", [
    (0, 0, 0, 0, "Low"), (1, 0, 0, 0, "Medium"), (0, 0, 1, 0, "Medium"), (2, 0, 0, 0, "Medium"),
    (3, 0, 0, 0, "High"), (0, 1, 0, 0, "High"), (0, 0, 1, 1, "High"),
])
def test_risk_level_follows_the_rule(late, missing, open_, crit, level):
    got, why = ah.risk_of(late, missing, open_, crit)
    assert got == level and why


def test_risk_reasons_read_as_words():
    assert ah.risk_of(2, 1, 3, 1)[1] == ["1 open critical finding", "1 missing file", "2 late files",
                                         "2 open findings"]
    assert ah.risk_of(0, 0, 0, 0)[1] == ["Nothing late, missing or open"]


def test_the_package_period():
    a, b, words = ap._window("2026-02", None, None)
    assert (a.date(), b.date(), words) == (dt.date(2026, 2, 1), dt.date(2026, 3, 1), "February 2026")
    a, b, _ = ap._window(None, "2026-03-01", "2026-03-07")
    assert (a.date(), b.date()) == (dt.date(2026, 3, 1), dt.date(2026, 3, 8))     # both days included
    with pytest.raises(HTTPException):
        ap._window(None, "2026-03-07", "2026-03-01")
    with pytest.raises(HTTPException):
        ap._window("March", None, None)


# ── 53.2 emails, reminders and the carrier's card (audit_alerts.py) ────────
import audit_alerts as al  # noqa: E402
from db import ActivityEvent  # noqa: E402


@pytest.fixture
def mail(s, monkeypatch):
    ActivityEvent.__table__.create(s.get_bind())
    sent = []
    monkeypatch.setattr(al, "_broker_people", lambda s, bid: [{"email": "b@x.test", "name": "Bo"}] if bid == 50 else [])
    import notifications
    monkeypatch.setattr(notifications, "carrier_admin_recipients", lambda tid: [{"email": "c@x.test", "name": "Dana"}])
    monkeypatch.setattr(notifications, "notify_people", lambda people, title, **k: sent.append((people, title, k)))
    return sent


def test_which_reminder_a_finding_is_owed():
    f = am.Finding(status="open", due_date=dt.date(2026, 11, 22))
    assert al.reminder_due(f, dt.date(2026, 11, 18)) is None
    assert al.reminder_due(f, dt.date(2026, 11, 19)) == "due_soon"
    assert al.reminder_due(f, dt.date(2026, 11, 23)) == "overdue"
    f.status = "closed"
    assert al.reminder_due(f, dt.date(2026, 11, 23)) is None


def test_each_reminder_goes_once_and_is_on_the_trail(s, mail):
    a = _audit(s)
    am.add_finding(s, a, 1, {"title": "Files late", "severity": "high", "due_date": "2026-11-21"})
    s.commit()
    assert al.send_reminders(s, TODAY) == 1
    assert al.send_reminders(s, TODAY) == 0                      # not twice
    assert mail[0][2]["subject"] == "Kavachio - Audit finding due in 2 days: Files late"
    assert al.send_reminders(s, dt.date(2026, 11, 22)) == 1      # then overdue, carrier copied
    assert mail[1][2]["subject"] == "Kavachio - Audit finding overdue: Files late" and mail[1][2]["cc"] == ["c@x.test"]
    said = [e.details["reminder"] for e in s.query(ActivityEvent).filter_by(action="audit_finding_reminder")]
    assert said == ["due soon", "overdue"]


def test_no_contact_means_no_reminder_recorded(s, mail):
    a = _audit(s, broker_party_id=51, contract_id=None)
    am.add_finding(s, a, 1, {"title": "x", "severity": "low", "due_date": "2026-11-20"})
    s.commit()
    assert al.send_reminders(s, TODAY) == 0 and not mail


def test_the_carrier_card_lists_replies_and_overdue(s, mail):
    a = _audit(s)
    f = am.add_finding(s, a, 1, {"title": "Files late", "severity": "high", "due_date": "2026-11-01"})
    g = am.add_finding(s, a, 1, {"title": "Wrong totals", "severity": "low"})
    s.commit()
    am.reply(s, g, 251, "broker", "Fixed", None, None)
    s.commit()
    out = al.waiting_for_carrier(s, 7, TODAY)
    assert [r["title"] for r in out["replied"]] == ["Wrong totals"]
    assert [r["title"] for r in out["overdue"]] == ["Files late"]
    am.reply(s, g, 1, "carrier", "Please send the file", None, None)
    s.commit()
    assert al.waiting_for_carrier(s, 7, TODAY)["replied"] == []    # the carrier answered last


def test_raising_and_replying_tell_the_other_side(s, mail):
    a = _audit(s)
    f = am.add_finding(s, a, 1, {"title": "Files late", "severity": "high"})
    s.commit()
    al.finding_raised(s, a, f, 1)
    al.broker_replied(s, a, f, "Done", "proof.csv")
    al.carrier_replied(s, a, f, "Thanks, one more file please", 1)
    to = [(p[0]["email"], k["subject"]) for p, _, k in mail]
    assert to == [("b@x.test", "Kavachio - New audit finding: Files late"),
                  ("c@x.test", "Kavachio - Acme replied to an audit finding"),
                  ("b@x.test", "Kavachio - Insurer Ltd replied about an audit finding")]
    assert mail[0][2]["cc"] == ["c@x.test"] and mail[1][2]["link_path"] == f"/audit?tab=audits&audit={a.id}"


# ── 50.1: which version of a check ran (audit_package.versions_that_ran) ──
def test_the_version_that_ran_comes_from_rule_history(s, monkeypatch):
    from sqlalchemy import text as sql
    ActivityEvent.__table__.create(s.get_bind())
    s.execute(sql("CREATE TABLE validation_rule (rule_id INTEGER, contract_id INTEGER, rule_name TEXT, "
                  "canonical_target TEXT)"))
    s.execute(sql("INSERT INTO validation_rule VALUES (5, 9, 'Premium positive', '{\"output_field\": \"Premium\"}')"))
    key = "contract_rule:9:Premium positive:Premium"
    at = lambda d: dt.datetime(2026, 11, d, 12)  # noqa: E731
    s.add_all([
        ActivityEvent(action="record_changed", target=key, created_at=at(10),
                      details={"event": "changed", "entity_id": 3, "before": {"rule": {"How strict": "warning"}}}),
        ActivityEvent(action="record_changed", target=key, created_at=at(20),
                      details={"event": "changed", "entity_id": 5,
                               "before": {"rule": {"How strict": "critical", "Message": "Premium must be above 0"}}}),
    ])
    s.commit()
    monkeypatch.setattr(ap, "text", lambda q: sql(q.replace("= ANY(:ids)", "IN (5, 4)")))
    # rule 5 ran on the 15th: version 2 then (1 before history, the change on the 10th made 2), changed once since
    out = ap.versions_that_ran(s, {5: at(15), 4: at(15)}, {9})
    assert out[5]["version"] == "Version 2"
    assert out[5]["since"].startswith("Yes — 1 change, last on 20 Nov 2026 (now version 3")
    assert out[5]["then"] == "How strict: critical; Message: Premium must be above 0"
    # rule 4 is in no history row and not in validation_rule: nothing to say beyond "as today"
    assert out[4]["version"] == "As today"
    # after the last change: no change since
    assert ap.versions_that_ran(s, {5: at(21)}, {9})[5]["since"] == "No"


def test_the_placeholder_carrier_code_means_every_carrier():
    assert ah.chosen_mga("default") is None and ah.chosen_mga(" ") is None and ah.chosen_mga(None) is None
    assert ah.chosen_mga("ceded-4b868f6e") == "ceded-4b868f6e"


def test_kavachio_reads_every_carriers_audits(s):
    a = _audit(s)
    assert [x["id"] for x in am.list_audits(s, None)["audits"]] == [a.id]
    assert am.get_audit(s, None, a.id) is a
    with pytest.raises(HTTPException):
        am.get_audit(s, 8, a.id)                       # another carrier still cannot
