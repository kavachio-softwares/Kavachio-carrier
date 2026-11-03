"""MOPR 41 — Exception Resolution Performance (fix_performance.py).

Run with:  DATABASE_URL=sqlite:// .venv/bin/python -m pytest test_fix_performance.py
"""
from __future__ import annotations

from datetime import datetime

import pytest

import fix_performance as fp

OCT = (datetime(2026, 10, 1), datetime(2026, 11, 1))
NOW = datetime(2026, 10, 20, 12, 0)


def _mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(
        eng, tables=[db.CheckRun.__table__, db.CheckResult.__table__,
                     db.CheckError.__table__, db.ExceptionDecisionLog.__table__,
                     db.QualitySetting.__table__])
    return sessionmaker(bind=eng)()


def _run(s, *, at, chain="sub:S1", kind="run", status="has_exceptions", export_id=10,
         broker=8, program=7, lane="direct", tenant=1):
    import db
    r = db.CheckRun(tenant_id=tenant, lane=lane, kind=kind, status=status, chain_key=chain,
                    export_id=export_id, broker_party_id=broker, program_id=program,
                    filename=f"file-{export_id}.xlsx", created_at=at)
    s.add(r)
    s.flush()
    return r


def _err(s, run, *, fpr, found, sheet="Premium", row=1, field="Premium", rule_id=1,
         policy="P1", severity="critical", name="Premium must be more than 0"):
    import db
    s.add(db.CheckResult(run_id=run.id, tenant_id=run.tenant_id, check_key=f"rule:{rule_id}",
                         kind="contract_rule", rule_id=rule_id, name=name,
                         severity=severity, outcome="failed", error_count=1))
    s.add(db.CheckError(run_id=run.id, tenant_id=run.tenant_id, lane=run.lane,
                        export_id=run.export_id, program_id=run.program_id,
                        broker_party_id=run.broker_party_id, chain_key=run.chain_key,
                        check_key=f"rule:{rule_id}", rule_id=rule_id, code=name,
                        severity=severity, sheet=sheet, row=row, field=field,
                        policy_number=policy, message="bad", fingerprint=fpr,
                        first_found_at=found, created_at=run.created_at))
    s.flush()


def _decide(s, *, at, kind="fix", export_id=10, lane="direct", sheet="Premium", row=1,
            field="Premium", rule_id=None, policy=None, user=5):
    import db
    s.add(db.ExceptionDecisionLog(tenant_id=1, export_id=export_id, lane=lane, kind=kind,
                                  sheet=sheet, row=row, field=field, rule_id=rule_id,
                                  policy_number=policy, decided_by_user_id=user,
                                  decided_at=at))
    s.flush()


def _one(s, **kw):
    ps = fp.problems(s, 1, **kw)
    assert len(ps) == 1
    return ps[0]


# --- when a problem counts as fixed --------------------------------------------

def test_a_decision_fixes_a_problem_from_when_it_was_found():
    s = _mem_session()
    r = _run(s, at=datetime(2026, 10, 1))
    _err(s, r, fpr="A", found=datetime(2026, 10, 1))
    _decide(s, at=datetime(2026, 10, 3), kind="approve")
    p = _one(s)
    assert (p["fixed_at"], p["how"], p["fixed_by_user_id"]) == (datetime(2026, 10, 3), "approve", 5)


def test_a_problem_gone_from_the_corrected_version_is_fixed_then():
    s = _mem_session()
    r1 = _run(s, at=datetime(2026, 10, 1))
    _err(s, r1, fpr="B", found=datetime(2026, 10, 1))
    _run(s, at=datetime(2026, 10, 4), export_id=11, status="clean")     # version 2
    p = _one(s)
    assert (p["fixed_at"], p["how"]) == (datetime(2026, 10, 4), fp.GONE)


def test_the_same_problem_in_two_versions_is_one_problem_found_in_the_first():
    s = _mem_session()
    r1 = _run(s, at=datetime(2026, 10, 1))
    _err(s, r1, fpr="C", found=datetime(2026, 10, 1))
    r2 = _run(s, at=datetime(2026, 10, 5), export_id=11)
    _err(s, r2, fpr="C", found=datetime(2026, 10, 1))
    p = _one(s)
    assert p["found_at"] == datetime(2026, 10, 1) and p["fixed_at"] is None
    assert p["export_id"] == 11            # shown as last seen


def test_a_later_check_that_ran_over_nothing_fixes_nothing():
    s = _mem_session()
    r1 = _run(s, at=datetime(2026, 10, 1))
    _err(s, r1, fpr="D", found=datetime(2026, 10, 1))
    _run(s, at=datetime(2026, 10, 4), export_id=11, status="not_validated")
    assert _one(s)["fixed_at"] is None


def test_a_self_check_is_not_a_problem_found():
    s = _mem_session()
    r = _run(s, at=datetime(2026, 10, 1), kind="self_check")
    _err(s, r, fpr="E", found=datetime(2026, 10, 1))
    assert fp.problems(s, 1) == []


def test_a_decision_from_before_the_problem_was_found_does_not_fix_it():
    s = _mem_session()
    r = _run(s, at=datetime(2026, 10, 5))
    _err(s, r, fpr="F", found=datetime(2026, 10, 5))
    _decide(s, at=datetime(2026, 10, 2))
    assert _one(s)["fixed_at"] is None


def test_a_canonical_decision_is_matched_by_rule_policy_and_field():
    s = _mem_session()
    r = _run(s, at=datetime(2026, 10, 1), lane="canonical", chain="export:20", export_id=20)
    _err(s, r, fpr="G", found=datetime(2026, 10, 1), rule_id=3, policy="P9", field="State",
         sheet=None, row=None)
    _decide(s, at=datetime(2026, 10, 2), lane="canonical", export_id=20, rule_id=3,
            policy="P9", field="State", sheet=None, row=None, kind="dismiss")
    assert _one(s)["how"] == "dismiss"


# --- the target ------------------------------------------------------------------

def test_the_target_is_five_days_until_the_carrier_changes_it():
    s = _mem_session()
    assert fp.target_days(s, 1) == 5
    assert fp.set_target_days(s, 1, 3, user_id=9) == (5, 3)
    assert fp.target_days(s, 1) == 3
    with pytest.raises(ValueError):
        fp.set_target_days(s, 1, 0)


# --- the month read back -----------------------------------------------------------

def _month(s):
    # Fixed by a decision in 2 days, within target.
    r1 = _run(s, at=datetime(2026, 10, 1), chain="sub:A", export_id=1, broker=8)
    _err(s, r1, fpr="a", found=datetime(2026, 10, 1))
    _decide(s, at=datetime(2026, 10, 3), export_id=1)
    # Gone in version 2 after 8 days — past the 5-day target.
    r2 = _run(s, at=datetime(2026, 10, 2), chain="sub:B", export_id=2, broker=9)
    _err(s, r2, fpr="b", found=datetime(2026, 10, 2))
    _run(s, at=datetime(2026, 10, 10), chain="sub:B", export_id=3, broker=9, status="clean")
    # Fixed in September: not this month's.
    r3 = _run(s, at=datetime(2026, 9, 1), chain="sub:C", export_id=4, broker=8)
    _err(s, r3, fpr="c", found=datetime(2026, 9, 1))
    _decide(s, at=datetime(2026, 9, 2), export_id=4)
    # Still open: 19 days old (past target) and 1 day old.
    r4 = _run(s, at=datetime(2026, 10, 1), chain="sub:D", export_id=5, broker=9)
    _err(s, r4, fpr="d", found=datetime(2026, 10, 1), rule_id=2, severity="warning",
         name="State is valid")
    r5 = _run(s, at=datetime(2026, 10, 19), chain="sub:E", export_id=6, broker=None)
    _err(s, r5, fpr="e", found=datetime(2026, 10, 19, 12, 0))
    s.commit()


def test_the_month_measures_fix_time_target_and_what_is_open():
    s = _mem_session()
    _month(s)
    out = fp.summary(s, 1, *OCT, now=NOW)
    f, o = out["fixed"], out["open"]
    assert f["problems"] == 2
    assert f["avg_days"] == 5.0 and f["median_days"] == 5.0     # 2 and 8 days
    assert (f["within_target"], f["within_target_pct"]) == (1, 50.0)
    assert {h["key"]: h["problems"] for h in f["by_how"]}["gone"] == 1
    assert (o["problems"], o["past_target"]) == (2, 1)
    assert o["oldest_days"] == 19.5
    assert {b["key"]: b["problems"] for b in o["by_age"]} == {
        "0_2": 1, "3_7": 0, "8_30": 1, "31_plus": 0}
    # Oldest first.
    assert [i["check"] for i in o["items"]] == ["State is valid", "Premium must be more than 0"]
    assert o["items"][0]["past_target"] is True


def test_by_broker_and_programme_puts_the_most_past_target_first():
    s = _mem_session()
    _month(s)
    rows = fp.summary(s, 1, *OCT, now=NOW)["by_broker_programme"]
    assert [r["broker_party_id"] for r in rows][0] == 9
    nine = rows[0]
    assert (nine["fixed"], nine["open"], nine["past_target"]) == (1, 1, 1)
    assert nine["avg_days"] == 8.0 and nine["within_target_pct"] == 0.0


def test_the_open_list_pages_and_a_new_target_changes_what_is_late():
    s = _mem_session()
    _month(s)
    out = fp.summary(s, 1, *OCT, now=NOW, page=2, page_size=1)
    assert out["open"]["total"] == 2 and len(out["open"]["items"]) == 1
    assert out["open"]["items"][0]["check"] == "Premium must be more than 0"
    fp.set_target_days(s, 1, 30)
    out = fp.summary(s, 1, *OCT, now=NOW)
    assert out["open"]["past_target"] == 0 and out["fixed"]["within_target"] == 2
