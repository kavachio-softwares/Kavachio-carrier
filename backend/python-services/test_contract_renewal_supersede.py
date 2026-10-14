"""
Renewal: the contract it replaces is superseded when the renewal goes IN FORCE,
and a renewal is re-checked against today's programme and broker before it goes
out. No database: the session and the parties are stood in for.
"""
import datetime as dt
import types

import pytest
from fastapi import HTTPException

import contract_routes as cr


def _contract(**kw):
    base = dict(id=1, lifecycle="active", status="active", renews_contract_id=None,
                expiry_dt=None, lifecycle_effective_date=None, program_id=7,
                broker_party_id=9, tenant_id=3, contract_type="insurer_broker")
    base.update(kw)
    return types.SimpleNamespace(**base)


class _Sess:
    def __init__(self, *rows):
        self.rows = {r.id: r for r in rows}

    def get(self, _model, pk):
        return self.rows.get(pk)


@pytest.fixture
def session(monkeypatch):
    holder = {}
    import sqlalchemy.orm as orm
    monkeypatch.setattr(orm, "object_session", lambda obj: holder.get("s"))
    return holder


@pytest.mark.parametrize("old_expiry", [None, dt.date(2020, 1, 1)])
def test_renewal_going_active_supersedes_old(session, old_expiry):
    old = _contract(id=1, expiry_dt=old_expiry)          # active, or expired
    new = _contract(id=2, lifecycle="signed", status="drafted", renews_contract_id=1)
    session["s"] = _Sess(old, new)
    cr._move(new, "active")
    assert new.lifecycle == "active"
    assert new.status == "active"          # now governs files
    assert old.lifecycle == "superseded" and old.status == "superseded"


def test_not_active_yet_leaves_old_alone(session):
    old = _contract(id=1)
    new = _contract(id=2, lifecycle="agreed", status="drafted", renews_contract_id=1)
    session["s"] = _Sess(old, new)
    cr._move(new, "signed")
    assert old.lifecycle == "active"
    assert new.status == "drafted"         # not in force: does not govern


def test_terminated_predecessor_is_not_resurrected(session):
    old = _contract(id=1, lifecycle="terminated", status="superseded")
    new = _contract(id=2, lifecycle="signed", status="drafted", renews_contract_id=1)
    session["s"] = _Sess(old, new)
    cr._move(new, "active")
    assert old.lifecycle == "terminated"


def test_ordinary_contract_going_active_touches_nothing(session):
    new = _contract(id=2, lifecycle="signed", status="drafted")
    session["s"] = _Sess(new)
    cr._move(new, "active")
    assert new.lifecycle == "active"
    assert new.status == "drafted"         # Part 1 is renewals only


def test_recheck_ignores_non_renewals(monkeypatch):
    monkeypatch.setattr(cr, "_check_counterparty",
                        lambda *a, **k: pytest.fail("should not be called"))
    cr._recheck_renewal_parties(None, None, _contract())


def test_recheck_refuses_removed_broker(monkeypatch):
    monkeypatch.setattr(cr, "_program_access", lambda *a: None)

    def boom(*a, **k):
        raise HTTPException(400, "taken off this programme")
    monkeypatch.setattr(cr, "_check_counterparty", boom)
    with pytest.raises(HTTPException):
        cr._recheck_renewal_parties(None, None, _contract(renews_contract_id=1))
