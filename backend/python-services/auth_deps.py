"""
auth_deps.py
────────────
The single request-time resolver (§5a / §6.3 of MULTITENANCY_AUTH_CONCEPT.md).

`current_principal` turns a Bearer JWT into a trusted `Principal`
(user_id + tenant_id + role) derived from the token's signed claims — never
from anything the client can freely set. `require_role` builds RBAC guards on
top of it.

Roles (tenant_reviewer intentionally dropped — not used in this app):
  tenant_user    — "Operator": run/map/export
  tenant_admin   — manage users + config within one tenant
  kavachio_admin — platform admin, cross-tenant (tenant_id is None)
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from jose import JWTError

from auth_tokens import decode_access_token

# --- role vocabulary --------------------------------------------------------
# FOUR seats, named after what they are:
#   kavachio_admin  the platform. Owns no book, sees across carriers.
#   carrier_admin   runs a CARRIER. Owns programmes, brokers, contracts.
#   broker_admin    runs a BROKER organisation. Belongs to a broker party, not
#                   to a carrier — the same broker produces for several.
#   operator        a seat a broker admin creates. Processes files and clears
#                   exceptions; it is a SEAT, not an organisation, and inherits
#                   its broker's reach entirely.
VALID_ROLES = ("kavachio_admin", "carrier_admin", "broker_admin", "operator")

# Map legacy DB values (ops/admin/read_only) onto the role model. Tokens
# always carry a normalized value so the rest of the code sees one vocabulary.
# Every legacy spelling lands on one of the four. `tenant_admin` and
# `tenant_user` are the MGA-era names — a tenant IS a carrier, so both mean
# carrier_admin now; `broker_operator` was always just an operator. Keeping the
# aliases means old tokens, old rows and old require_role() call sites all
# still resolve instead of silently 403-ing.
_ROLE_ALIASES = {
    "kavachio_admin":  "kavachio_admin",
    "carrier_admin":   "carrier_admin",
    "broker_admin":    "broker_admin",
    "operator":        "operator",
    # legacy
    "tenant_admin":    "carrier_admin",
    "admin":           "carrier_admin",
    "tenant_user":     "carrier_admin",
    "ops":             "carrier_admin",
    "read_only":       "carrier_admin",
    "broker_operator": "operator",
}

# The two seats that belong to a broker party rather than to a carrier.
BROKER_ROLES = ("broker_admin", "operator")


def normalize_role(raw: str | None) -> str:
    """Coerce any stored/legacy role string to one of VALID_ROLES.

    An unknown value falls back to `operator` — the least-privileged seat —
    so a typo or a role from a future version can never be mistaken for admin.
    """
    return _ROLE_ALIASES.get((raw or "").strip(), "operator")


def db_role_values(role: str) -> tuple[str, ...]:  # noqa: D401
    """Every raw value that could be STORED in app_user.role and normalizes to
    `role` — the inverse of normalize_role().

    Lets a query filter on role in SQL (``AppUser.role.in_(db_role_values(...))``)
    instead of loading every user and normalizing in Python, without any call
    site restating the legacy aliases. Derived from _ROLE_ALIASES, so adding an
    alias there is picked up everywhere."""
    return tuple(raw for raw, norm in _ROLE_ALIASES.items() if norm == role)


@dataclass(frozen=True)
class Principal:
    user_id: int
    # None for kavachio_admin (cross-tenant) and for broker users, who have no
    # carrier of their own — for those the API must resolve which carrier is
    # being worked on and check it against v_broker_tenant_access first.
    tenant_id: int | None
    role: str
    broker_party_id: int | None = None

    @property
    def is_platform_admin(self) -> bool:
        return self.role == "kavachio_admin"

    @property
    def is_broker(self) -> bool:
        """A broker seat. Carries broker_party_id instead of tenant_id."""
        return self.role in BROKER_ROLES


def current_principal(authorization: str = Header(default="")) -> Principal:
    """FastAPI dependency: validate the Bearer access token and return the
    caller's Principal. Raises 401 on a missing/invalid/expired token."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    try:
        claims = decode_access_token(authorization[7:].strip())
    except JWTError:
        raise HTTPException(401, "invalid or expired token")
    return Principal(
        user_id=int(claims["sub"]),
        tenant_id=claims.get("tenant_id"),
        role=normalize_role(claims.get("role")),
        broker_party_id=claims.get("broker_party_id"),
    )


def require_role(*allowed: str):
    """RBAC guard factory. kavachio_admin is always allowed (superset).

        @router.post("/users")
        def create(p: Principal = Depends(require_role("tenant_admin"))): ...
    """
    # The names are normalized here too, so the ~30 existing call sites that
    # still say require_role("tenant_admin") keep working against the renamed
    # vocabulary instead of failing closed on every request.
    wanted = {normalize_role(a) for a in allowed}

    def _dep(p: Principal = Depends(current_principal)) -> Principal:
        if not (p.is_platform_admin or p.role in wanted):
            raise HTTPException(403, "insufficient role")
        return p
    return _dep
