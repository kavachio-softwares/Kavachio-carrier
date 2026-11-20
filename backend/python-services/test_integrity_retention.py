"""Record integrity (record_integrity.py, 52.1) and retention (retention.py,
52.2). Pure: an in-memory sqlite database, nothing shared. Migration 50's
guard is Postgres-only and was tried on a local copy.

Run with:  DATABASE_URL=sqlite:// .venv/bin/python -m pytest test_integrity_retention.py
"""
import datetime as dt

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import record_integrity as ri
import retention as rt
from db import ActivityEvent, AppUser, AuthAudit, ExceptionDecisionLog, Tenant

NOW = dt.datetime(2026, 11, 19, 12)


@pytest.fixture
def s(monkeypatch):
    eng = create_engine("sqlite://")
    for m in (Tenant, AppUser, ActivityEvent, AuthAudit, ExceptionDecisionLog):
        m.__table__.create(eng)
    ri.IntegrityBase.metadata.create_all(eng)
    S = sessionmaker(bind=eng)()
    S.add_all([Tenant(id=7, tenant_name="ins", legal_name="Insurer Ltd", address={"country": "India"}),
               Tenant(id=8, tenant_name="oth", legal_name="Other Ltd"),
               AppUser(id=1, email="c@x.test", full_name="Dana", role="carrier_admin", tenant_id=7),
               AppUser(id=2, email="b@x.test", full_name="Bo", role="broker_admin", broker_party_id=50)])
    old, new = NOW - dt.timedelta(days=400), NOW - dt.timedelta(days=10)
    S.add_all([ActivityEvent(id=1, tenant_id=7, action="a", created_at=old, details={"x": 1}),
               ActivityEvent(id=2, tenant_id=7, action="b", created_at=old, actor_broker_party_id=50),
               ActivityEvent(id=3, tenant_id=7, action="c", created_at=new),
               ActivityEvent(id=4, tenant_id=8, action="d", created_at=old),
               ExceptionDecisionLog(id=1, tenant_id=7, lane="direct", kind="fix", broker_party_id=51, decided_at=old)])
    S.commit()
    import audit_feed as af
    monkeypatch.setattr(af, "brokers_of_carrier", lambda s, tid: {50, 51} if tid == 7 else set())
    yield S
    S.close()


# ── 52.1 ───────────────────────────────────────────────────────────────────
def test_a_fingerprint_ignores_order_and_empty_columns():
    assert ri.fingerprint({"a": 1, "b": None, "c": {"y": 2, "x": 1}}) == ri.fingerprint({"c": {"x": 1, "y": 2}, "a": 1})
    assert ri.fingerprint({"a": 1}) != ri.fingerprint({"a": 2})


def test_sealing_is_once_and_the_check_finds_a_change_and_a_removal(s):
    assert ri.seal_new(s, NOW) == 5
    assert ri.seal_new(s, NOW) == 0
    r = ri.check(s, 7, by="Dana", now=NOW)
    assert r["result"] == "All unchanged" and r["checked"] == 4 and r["chain_ok"]
    s.execute(text("UPDATE activity_events SET actor = 'someone' WHERE id = 1"))
    s.execute(text("DELETE FROM exception_decision_log WHERE id = 1"))
    s.commit()
    r = ri.check(s, 7, by="Dana", now=NOW)
    assert r["result"] == "Problem found"
    assert sorted((p["type"], p["result"]) for p in r["problems"]) == [("Activity log", "Changed"),
                                                                       ("Exception decisions", "Missing")]
    assert ri.check(s, 8, now=NOW)["problems_total"] == 0                 # the other carrier's records are fine
    last = s.query(ActivityEvent).filter_by(action="integrity_checked").order_by(ActivityEvent.id.desc()).first()
    assert ri.summary(s, 8)["last_check"]["result"] == last.details["result"] == "All unchanged"


def test_a_rewritten_fingerprint_breaks_the_chain(s):
    ri.seal_new(s, NOW)
    s.execute(text("UPDATE audit_seal SET seal_hash = :h WHERE seal_id = 2"), {"h": "f" * 64})
    s.commit()
    r = ri.check(s, None, now=NOW)
    assert not r["chain_ok"] and r["broken_at"] == 2 and r["result"] == "Problem found"


def test_a_record_still_being_written_is_not_sealed(s):
    s.add(ActivityEvent(id=9, tenant_id=7, action="fresh", created_at=NOW - dt.timedelta(minutes=1)))
    s.commit()
    ri.seal_new(s, NOW)
    assert s.query(ri.Seal).filter_by(table="activity_events", record_id=9).count() == 0


# ── 52.2 ───────────────────────────────────────────────────────────────────
def test_the_country_period_wins_over_everywhere(s):
    rt.save_rule(s, 1, "activity", "", 120, None)
    rt.save_rule(s, 1, "activity", "india", 12, None)
    rules = s.query(rt.Rule).all()
    assert rt.months_for(rules, "activity", "India") == (12, "india")
    assert rt.months_for(rules, "activity", "France") == (120, "")
    assert rt.months_for(rules, "sign_in", "India") == (None, "")
    with pytest.raises(HTTPException):
        rt.save_rule(s, 1, "activity", "", 0, None)


def test_nothing_is_deleted_until_a_period_is_set(s):
    assert rt.run_due(s, NOW) == 0
    assert s.query(ActivityEvent).count() == 4


def test_a_run_deletes_what_is_past_and_marks_seals_gone(s):
    ri.seal_new(s, NOW)
    rt.save_rule(s, 1, "activity", "India", 12, None)
    s.commit()
    assert rt.run_due(s, NOW) == 2                       # carrier 7's two old ones; carrier 8 has no India period
    assert sorted(i for (i,) in s.query(ActivityEvent.id)) == [3, 4]
    log = rt.deletions(s, 7)["items"]
    assert [(x["type"], x["deleted"], x["country"], x["months"]) for x in log] == [("Activity log", 2, "India", 12)]
    r = ri.check(s, 7, now=NOW)
    assert r["result"] == "All unchanged" and sum(k["gone"] for k in r["kinds"]) == 2


def test_a_hold_keeps_records_and_is_logged(s):
    rt.save_rule(s, 1, "activity", "", 12, None)
    rt.save_rule(s, 1, "decisions", "", 12, None)
    h = rt.place_hold(s, 7, 1, "Court order 12/2026", None)
    s.commit()
    assert rt.run_due(s, NOW) == 1                       # only carrier 8's
    assert [(x["deleted"], x["held"]) for x in rt.deletions(s, 7)["items"]] == [(0, 1), (0, 2)]
    with pytest.raises(HTTPException):
        rt.lift_hold(s, 7, h.id, 1, " ")
    rt.lift_hold(s, 7, h.id, 1, "Case closed")
    # a hold on broker 50 only keeps what its people did
    rt.place_hold(s, 7, 1, "Broker dispute", 50)
    s.commit()
    assert rt.run_due(s, NOW + dt.timedelta(minutes=1)) == 2      # activity 1 and the decision for broker 51
    assert sorted(i for (i,) in s.query(ActivityEvent.id).filter(ActivityEvent.tenant_id == 7)) == [2, 3]
    with pytest.raises(HTTPException):
        rt.place_hold(s, 7, 1, "x", 99)                  # not this carrier's broker


def test_the_preview_says_what_the_next_run_would_delete(s):
    rt.save_rule(s, 1, "activity", "", 12, None)
    rt.place_hold(s, 7, 1, "Broker dispute", 50)
    s.commit()
    row = next(k for k in rt.overview(s, 7, NOW)["kinds"] if k["key"] == "activity")
    assert (row["months"], row["applies"], row["next_count"], row["held"]) == (12, "Everywhere", 1, 1)
