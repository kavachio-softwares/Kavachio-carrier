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
VALID_ROLES = ("tenant_user", "tenant_admin", "kavachio_admin")

# Map legacy DB values (ops/admin/read_only) onto the 3-role model. Tokens
# always carry a normalized value so the rest of the code sees one vocabulary.
_ROLE_ALIASES = {
    "ops":            "tenant_user",
    "admin":          "tenant_admin",
    "read_only":      "tenant_user",
    "tenant_user":    "tenant_user",
    "tenant_admin":   "tenant_admin",
    "kavachio_admin": "kavachio_admin",
}


def normalize_role(raw: str | None) -> str:
    """Coerce any stored/legacy role string to one of VALID_ROLES.
    Unknown values fall back to the least-privileged role."""
    return _ROLE_ALIASES.get((raw or "").strip(), "tenant_user")


def db_role_values(role: str) -> tuple[str, ...]:
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
    tenant_id: int | None      # None ONLY for kavachio_admin (cross-tenant)
    role: str

    @property
    def is_platform_admin(self) -> bool:
        return self.role == "kavachio_admin"


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
    )


def require_role(*allowed: str):
    """RBAC guard factory. kavachio_admin is always allowed (superset).

        @router.post("/users")
        def create(p: Principal = Depends(require_role("tenant_admin"))): ...
    """
    def _dep(p: Principal = Depends(current_principal)) -> Principal:
        if not (p.is_platform_admin or p.role in allowed):
            raise HTTPException(403, "insufficient role")
        return p
    return _dep
