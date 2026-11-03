"""
When Renew is offered: expired, or active within the renewal window
(RENEWAL_WINDOW_DAYS, default 30), and never twice. One rule — renewal_gate —
read by the button (actions.renew) and enforced by POST /renew. No database.
"""
import datetime as dt
import types

import pytest

import contract_routes as cr

EXPIRY = dt.date(2027, 6, 30)


@pytest.mark.parametrize("today, allowed, opens_on", [
    (EXPIRY - dt.timedelta(days=31), False, EXPIRY - dt.timedelta(days=30)),  # day before window
    (EXPIRY - dt.timedelta(days=30), True, None),                             # first day of window
    (EXPIRY - dt.timedelta(days=1), True, None),
    (EXPIRY, True, None),                                                     # expiry day: still active
    (dt.date(2026, 10, 14), False, dt.date(2027, 5, 31)),                     # just made active
])
def test_active_contract_window_boundaries(today, allowed, opens_on):
    g = cr.renewal_gate("active", EXPIRY, today, 30)
    assert g["allowed"] is allowed
    assert g["opens_on"] == opens_on
    assert g["reason"] == (None if allowed else "too_early")


def test_window_length_follows_the_setting():
    today = EXPIRY - dt.timedelta(days=45)
    assert cr.renewal_gate("active", EXPIRY, today, 30)["allowed"] is False
    assert cr.renewal_gate("active", EXPIRY, today, 60)["allowed"] is True


@pytest.mark.parametrize("state", ["expired", "terminated"])
def test_expired_and_terminated_are_renewable(state):
    # Terminated BEFORE its expiry too: the window is only for active contracts.
    for today in (EXPIRY - dt.timedelta(days=200), EXPIRY + dt.timedelta(days=1)):
        g = cr.renewal_gate(state, EXPIRY, today, 30)
        assert g == {"allowed": True, "reason": None, "opens_on": None}


def test_active_without_expiry_is_renewable():
    assert cr.renewal_gate("active", None, dt.date(2026, 1, 1), 30)["allowed"] is True


@pytest.mark.parametrize("state", ["superseded", "draft", "in_review",
                                   "agreed", "signed", "pending"])
def test_never_renewable_states(state):
    g = cr.renewal_gate(state, EXPIRY, EXPIRY + dt.timedelta(days=5), 30)
    assert g["allowed"] is False and g["reason"] == "not_renewable"


@pytest.mark.parametrize("state", ["expired", "active", "terminated", "superseded"])
def test_existing_renewal_blocks_a_second(state):
    succ = types.SimpleNamespace(id=99)
    g = cr.renewal_gate(state, EXPIRY, EXPIRY - dt.timedelta(days=1), 30, succ)
    assert g["allowed"] is False and g["reason"] == "already_renewed"


@pytest.mark.parametrize("raw, days", [(None, 30), ("30", 30), ("45", 45), (" 7 ", 7),
                                       ("0", 0), ("-5", 0), ("abc", 30), ("", 30)])
def test_window_setting_parsing(monkeypatch, raw, days):
    if raw is None:
        monkeypatch.delenv("RENEWAL_WINDOW_DAYS", raising=False)
    else:
        monkeypatch.setenv("RENEWAL_WINDOW_DAYS", raw)
    assert cr._renewal_window_days() == days


def _contract(**kw):
    base = dict(id=1, lifecycle="active", status="active", renews_contract_id=None,
                expiry_dt=EXPIRY, lifecycle_effective_date=None, program_id=7,
                broker_party_id=9, tenant_id=3, contract_type="insurer_broker",
                wording_sections=None, blob_ref=None, blob=None,
                commercial_terms=None, filename="c.pdf", extracted=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_actions_renew_follows_the_gate():
    from auth_deps import Principal
    carrier = Principal(user_id=1, tenant_id=3, role="carrier_admin")
    broker = Principal(user_id=2, tenant_id=None, role="broker_admin")
    c = _contract()
    assert cr._allowed_actions(c, [], carrier, [], renewable=True)["renew"] is True
    assert cr._allowed_actions(c, [], carrier, [], renewable=False)["renew"] is False
    # A broker never renews, whatever the gate says.
    assert cr._allowed_actions(c, [], broker, [], renewable=True)["renew"] is False
    # Without the gate's answer: terminated is offered, superseded is not.
    t = _contract(lifecycle="terminated")
    assert cr._allowed_actions(t, [], carrier, [])["renew"] is True
    sup = _contract(lifecycle="superseded")
    assert cr._allowed_actions(sup, [], carrier, [])["renew"] is False
