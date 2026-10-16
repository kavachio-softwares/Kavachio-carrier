"""
Phase A — which version owns a day (D1), gaps (D2), and the draft-renewal block
(D8). renewal_chain.term_conflicts is the one rule behind the Renew form, POST
/renew, the draft edit and a renewal going in force. No database.
"""
import datetime as dt
import types

import pytest
from fastapi import HTTPException

import contract_routes as cr
import renewal_chain as rc

D = dt.date


def v(id=1, inception=None, expiry=None, terminated=None, lifecycle="active", name=None):
    return types.SimpleNamespace(id=id, inception_dt=inception, expiry_dt=expiry,
                                 terminated_date=terminated, lifecycle=lifecycle,
                                 name=name or f"V{id}")


def codes(found):
    return [(f["code"], f["severity"], f["days"]) for f in found]


# ── D1: who owns the boundary day ──────────────────────────────────────────

def test_handover_day_goes_to_the_new_version():
    # Version 1: 1 Oct 2025 – 1 Oct 2026 ("12:01 a.m. on both dates").
    v1 = v(1, D(2025, 10, 1), D(2026, 10, 1))
    v2 = v(2, D(2026, 10, 1), D(2027, 9, 30))
    assert rc.term_conflicts(v2.inception_dt, v2.expiry_dt, prev=v1) == []
    assert rc.last_day(v1, v2) == D(2026, 9, 30)      # 1 Oct is Version 2's
    assert rc.last_day(v2) == D(2027, 9, 30)          # inclusive last day


def test_adjacent_typed_terms_keep_their_last_day():
    v1 = v(1, D(2026, 1, 1), D(2026, 12, 31))
    v2 = v(2, D(2027, 1, 1), D(2027, 12, 31))
    assert rc.term_conflicts(v2.inception_dt, v2.expiry_dt, prev=v1) == []
    assert rc.last_day(v1, v2) == D(2026, 12, 31)     # 31 Dec stays Version 1's


def test_later_handover_moves_the_last_day():
    v2 = v(2, D(2026, 10, 1), D(2027, 9, 30))
    assert rc.last_day(v2, v(3, D(2027, 10, 1))) == D(2027, 9, 30)   # adjacent
    assert rc.last_day(v2, v(3, D(2027, 9, 30))) == D(2027, 9, 29)   # handover


@pytest.mark.parametrize("start, shared", [
    (D(2026, 9, 30), 2),     # one day before the handover: 30 Sep + 1 Oct
    (D(2026, 6, 1), 123),
])
def test_overlap_is_refused_with_the_shared_days(start, shared):
    v1 = v(1, D(2025, 10, 1), D(2026, 10, 1))
    found = rc.term_conflicts(start, D(2027, 9, 30), prev=v1)
    assert codes(found) == [("overlaps_previous", "block", shared)]
    assert "Start on 1 Oct 2026" in found[0]["message"]


def test_version2_version3_case_is_an_8_day_overlap():
    v2 = v(7732, D(2026, 10, 1), D(2026, 10, 13))
    found = rc.term_conflicts(D(2026, 10, 6), D(2027, 10, 5), prev=v2)
    assert codes(found) == [("overlaps_previous", "block", 8)]   # 6–13 Oct


def test_open_ended_previous_ends_the_day_before_the_successor():
    v1 = v(1, D(2025, 1, 1), None)
    assert rc.term_conflicts(D(2026, 3, 1), D(2027, 2, 28), prev=v1) == []
    assert rc.last_day(v1, v(2, D(2026, 3, 1))) == D(2026, 2, 28)
    assert rc.last_day(v1) is None


def test_terminated_previous_ends_on_its_termination_date():
    v1 = v(1, D(2026, 5, 15), D(2027, 5, 14), terminated=D(2026, 10, 14),
           lifecycle="terminated")
    assert rc.term_end(v1) == D(2026, 10, 14)
    assert rc.term_conflicts(D(2026, 10, 14), D(2027, 10, 13), prev=v1) == []  # handover
    assert rc.term_conflicts(D(2026, 10, 15), D(2027, 10, 14), prev=v1) == []  # adjacent
    assert codes(rc.term_conflicts(D(2026, 10, 13), D(2027, 10, 12), prev=v1)) == [
        ("overlaps_previous", "block", 2)]


def test_terminated_after_expiry_keeps_the_expiry():
    v1 = v(1, D(2026, 1, 1), D(2026, 12, 31), terminated=D(2027, 2, 1))
    assert rc.term_end(v1) == D(2026, 12, 31)


def test_expiry_before_inception():
    assert codes(rc.term_conflicts(D(2027, 1, 2), D(2027, 1, 1))) == [
        ("expiry_before_inception", "block", None)]


# ── D2: a gap warns, never blocks ──────────────────────────────────────────

def test_gap_after_previous_is_a_warning_with_its_length():
    v1 = v(1, D(2026, 1, 1), D(2026, 12, 31))
    found = rc.term_conflicts(D(2027, 1, 5), D(2027, 12, 31), prev=v1)
    assert codes(found) == [("gap_after_previous", "warn", 4)]     # 1–4 Jan
    assert rc.blocking(found) == [] and len(rc.warnings(found)) == 1


def test_gap_before_next():
    nxt = v(3, D(2027, 2, 1), lifecycle="active")
    assert codes(rc.term_conflicts(D(2026, 1, 1), D(2026, 12, 31), next_version=nxt)) == [
        ("gap_before_next", "warn", 31)]


# ── D8 and the next version ────────────────────────────────────────────────

def test_overlapping_a_draft_renewal_is_blocked_until_the_draft_is_corrected():
    draft = v(9, D(2026, 12, 1), D(2027, 11, 30), lifecycle="draft")
    found = rc.term_conflicts(D(2026, 1, 1), D(2026, 12, 31), next_version=draft)
    assert codes(found) == [("overlaps_draft_renewal", "block", 31)]
    assert "Correct the draft's dates first" in found[0]["message"]


def test_overlapping_an_active_next_version():
    nxt = v(9, D(2026, 12, 1), lifecycle="active")
    assert codes(rc.term_conflicts(D(2026, 1, 1), D(2026, 12, 31), next_version=nxt)) == [
        ("overlaps_next", "block", 31)]


def test_handover_into_the_next_version_is_fine():
    nxt = v(9, D(2026, 12, 31), lifecycle="draft")
    assert rc.term_conflicts(D(2026, 1, 1), D(2026, 12, 31), next_version=nxt) == []


def test_a_version_that_would_own_no_days():
    nxt = v(9, D(2026, 6, 1))
    found = rc.term_conflicts(D(2026, 6, 1), D(2026, 6, 1), next_version=nxt)
    assert ("no_days", "block", None) in codes(found)


# ── the suggested start, and the switch ────────────────────────────────────

@pytest.mark.parametrize("prev, want", [
    (v(1, D(2025, 10, 1), D(2026, 10, 1)), D(2026, 10, 1)),    # handover style
    (v(1, D(2026, 1, 1), D(2026, 12, 31)), D(2027, 1, 1)),     # inclusive style
    (v(1, D(2026, 10, 6), D(2027, 10, 5)), D(2027, 10, 6)),
    (v(1, D(2026, 5, 15), D(2027, 5, 14), terminated=D(2026, 10, 14)), D(2026, 10, 15)),
    (v(1, D(2026, 1, 1), None), None),
])
def test_suggested_inception(prev, want):
    assert rc.suggested_inception(prev) == want


@pytest.mark.parametrize("raw, on", [(None, True), ("true", True), ("false", False),
                                     ("0", False), ("off", False)])
def test_switch(monkeypatch, raw, on):
    if raw is None:
        monkeypatch.delenv("RENEWAL_OVERLAP_CHECK", raising=False)
    else:
        monkeypatch.setenv("RENEWAL_OVERLAP_CHECK", raw)
    assert rc.overlap_check_enabled() is on


# ── the server's use of it ─────────────────────────────────────────────────

def test_check_term_refuses_overlap_and_asks_for_gaps(monkeypatch):
    monkeypatch.delenv("RENEWAL_OVERLAP_CHECK", raising=False)
    v1 = v(1, D(2026, 1, 1), D(2026, 12, 31))
    with pytest.raises(HTTPException) as e:
        cr._check_term(D(2026, 12, 1), D(2027, 11, 30), prev=v1, next_version=None,
                       confirmed=True)                 # confirming never lifts a block
    assert e.value.status_code == 409 and "needs_confirm" not in e.value.detail
    with pytest.raises(HTTPException) as e:
        cr._check_term(D(2027, 1, 10), D(2027, 12, 31), prev=v1, next_version=None,
                       confirmed=False)
    assert e.value.detail["needs_confirm"] is True
    gaps = cr._check_term(D(2027, 1, 10), D(2027, 12, 31), prev=v1, next_version=None,
                          confirmed=True)
    assert [g["days"] for g in gaps] == [9]
    assert cr._check_term(D(2027, 1, 1), D(2027, 12, 31), prev=v1, next_version=None,
                          confirmed=False) == []


def test_check_term_switched_off(monkeypatch):
    monkeypatch.setenv("RENEWAL_OVERLAP_CHECK", "false")
    v1 = v(1, D(2026, 1, 1), D(2026, 12, 31))
    assert cr._check_term(D(2026, 6, 1), D(2027, 5, 31), prev=v1, next_version=None,
                          confirmed=False) == []


def test_going_live_over_the_previous_version_is_refused(monkeypatch):
    monkeypatch.delenv("RENEWAL_OVERLAP_CHECK", raising=False)
    prev = v(1, D(2026, 1, 1), D(2026, 12, 31))
    new = v(2, D(2026, 12, 1), D(2027, 11, 30))
    new.renews_contract_id = 1

    class S:
        def get(self, _m, pk):
            return prev if pk == 1 else None
    import sqlalchemy.orm as orm
    monkeypatch.setattr(orm, "object_session", lambda obj: S())
    with pytest.raises(HTTPException) as e:
        cr._check_live_term(new)
    assert e.value.status_code == 409 and "cannot go in force" in e.value.detail["message"]
    new.inception_dt = D(2027, 1, 1)
    cr._check_live_term(new)                               # adjacent: goes ahead
