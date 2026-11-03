"""The two things the PLATFORM seat may not do, however far require_role()
lets it in: invite a carrier's broker, and open a contract record.

Both were closed only by accident before — every one of those endpoints ends
up at resolve_tenant_id(), which 400s a platform admin for naming no tenant.
That is a missing parameter, not a rule, and it would open the day anyone
added a tenant picker for platform staff. These tests pin the rule itself.

Self-contained: the guards read the token's role and nothing else, so nothing
here touches a database or any other test's fixtures.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi import HTTPException

from auth_deps import Principal
from carrier_scope import assert_can_invite_brokers, assert_can_open_contract

PLATFORM = Principal(user_id=1, tenant_id=None, role="kavachio_admin")
# Both carrier seats hold the carrier_admin DB role; only the owner is the
# Carrier Admin on screen, and that difference is not this guard's business.
CARRIER = Principal(user_id=2, tenant_id=7, role="carrier_admin")
BROKER_ADMIN = Principal(user_id=3, tenant_id=None, role="broker_admin",
                         broker_party_id=42)
OPERATOR = Principal(user_id=4, tenant_id=None, role="operator",
                     broker_party_id=42)

GUARDS = [
    pytest.param(assert_can_invite_brokers, id="invite-brokers"),
    pytest.param(assert_can_open_contract, id="open-contract"),
]

# A broker seat never reaches these endpoints at all — require_role("carrier_admin")
# turns them away first — so neither guard says anything about them, and neither
# must start refusing a seat it was never asked about.
PASSES_THROUGH = [
    pytest.param(CARRIER, id="carrier"),
    pytest.param(BROKER_ADMIN, id="broker-admin"),
    pytest.param(OPERATOR, id="operator"),
]


@pytest.mark.parametrize("guard", GUARDS)
def test_platform_seat_is_refused(guard):
    with pytest.raises(HTTPException) as e:
        guard(PLATFORM)
    assert e.value.status_code == 403


@pytest.mark.parametrize("guard", GUARDS)
@pytest.mark.parametrize("who", PASSES_THROUGH)
def test_every_other_seat_passes_through(guard, who):
    guard(who)


@pytest.mark.parametrize("guard", GUARDS)
def test_the_refusal_says_who_is_refused_and_why(guard):
    """A 403 a carrier support desk cannot read is a bug report waiting to be
    filed. The message has to name Kavachio as the one refused, and say what
    the rule is rather than only that there is one."""
    with pytest.raises(HTTPException) as e:
        guard(PLATFORM)
    msg = str(e.value.detail)
    assert "Kavachio" in msg, f"message does not say who is refused: {msg!r}"
    assert len(msg) >= 40, f"message does not say why: {msg!r}"
