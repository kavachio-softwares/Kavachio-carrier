"""The rule library belongs to the carrier ADMIN, not to everyone at a carrier.

A rule here is not work on one file: it is a standing instruction applied to
every bordereau the carrier validates, on every programme and for every
broker. So it sits with the organisation's owner, the same person who signs a
contract — not with everyone holding the `carrier_admin` DB role, which is
both carrier seats.

That distinction cannot come from the token: both seats carry the same role.
It comes from `tenant.owner_user_id`, which is what `carrier_seat` reads and
what `require_carrier_admin` refuses on.

Self-contained: the session is a stub, so no database is touched.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi import HTTPException

import carrier_scope
from auth_deps import Principal

OWNER_ID = 11
OTHER_ID = 22
TENANT_ID = 7

CARRIER_ADMIN = Principal(user_id=OWNER_ID, tenant_id=TENANT_ID, role="carrier_admin")
CARRIER_USER = Principal(user_id=OTHER_ID, tenant_id=TENANT_ID, role="carrier_admin")
PLATFORM = Principal(user_id=1, tenant_id=None, role="kavachio_admin")


class _Tenant:
    def __init__(self, owner):
        self.id = TENANT_ID
        self.owner_user_id = owner


class _StubSession:
    """Just enough of a session for carrier_seat: one tenant, one owner."""

    def __init__(self, owner):
        self._tenant = _Tenant(owner)

    def query(self, *_a, **_kw):
        return self

    def filter(self, *_a, **_kw):
        return self

    def first(self):
        return self._tenant

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def with_owner(monkeypatch):
    """Point carrier_scope at a stub tenant whose owner we choose."""
    def _install(owner):
        monkeypatch.setattr(carrier_scope, "SessionLocal",
                            lambda: _StubSession(owner))
    return _install


def _call(principal):
    """Run the dependency's body the way FastAPI would, having already
    satisfied the require_role half it composes on top of."""
    return carrier_scope.require_carrier_admin("change the rule library")(principal)


def test_the_carrier_admin_may_change_the_library(with_owner):
    with_owner(OWNER_ID)
    assert _call(CARRIER_ADMIN) is CARRIER_ADMIN


def test_a_carrier_user_may_not(with_owner):
    with_owner(OWNER_ID)
    with pytest.raises(HTTPException) as e:
        _call(CARRIER_USER)
    assert e.value.status_code == 403


def test_the_refusal_names_the_act_and_who_may_do_it(with_owner):
    """403s on this screen are read by someone who believes they are an admin,
    because both seats are called carrier_admin everywhere else."""
    with_owner(OWNER_ID)
    with pytest.raises(HTTPException) as e:
        _call(CARRIER_USER)
    msg = str(e.value.detail)
    assert "only the carrier can" in msg.lower()
    assert "rule library" in msg


def test_kavachio_staff_keep_the_platform_wide_library(with_owner):
    """They manage the GLOBAL rules and created the organisation; the seat
    rule is about the two carrier seats, not about them."""
    with_owner(OWNER_ID)
    assert _call(PLATFORM) is PLATFORM


def test_a_carrier_with_no_owner_recorded_fails_OPEN(with_owner):
    """Migration 18 could not name an owner for every legacy organisation.
    Refusing everyone there would lock a whole company out of its own rules,
    and it would contradict the UI, which shows all of them the admin's
    buttons for the same reason (carrier_seat answers "both")."""
    with_owner(None)
    assert _call(CARRIER_USER) is CARRIER_USER
