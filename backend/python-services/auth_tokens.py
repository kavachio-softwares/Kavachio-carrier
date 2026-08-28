"""
auth_tokens.py
──────────────
Mint and verify the two JWTs (§4 of MULTITENANCY_AUTH_CONCEPT.md):

  access token  — short-lived (ACCESS_TTL_MIN), carries sub/tenant_id/role.
                  Sent on every API call as `Authorization: Bearer <jwt>`.
  refresh token — long-lived (REFRESH_TTL_DAYS = 7), carries only `sub`.
                  Used at /auth/refresh to mint a fresh access token without
                  re-login. When it expires the user is logged out.

Both are signed with the same HS256 secret and carry a `type` claim so an
access token can never be used where a refresh token is expected (and vice
versa), plus a `ver` claim for forced rotation.
"""
from datetime import datetime, timedelta, timezone

from jose import jwt, JWTError  # python-jose[cryptography]

from settings import SETTINGS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mint_access_token(user_id: int, tenant_id: "int | None", role: str) -> str:
    now = _now()
    payload = {
        "sub":       str(user_id),        # JWT spec: subject is a string
        "tenant_id": tenant_id,           # None for kavachio_admin (cross-tenant)
        "role":      role,
        "type":      "access",
        "iat":       now,
        "exp":       now + timedelta(minutes=SETTINGS.ACCESS_TTL_MIN),
        "iss":       SETTINGS.JWT_ISSUER,
        "ver":       SETTINGS.TOKEN_VER,
    }
    return jwt.encode(payload, SETTINGS.JWT_SECRET, algorithm=SETTINGS.JWT_ALG)


def mint_refresh_token(user_id: int) -> str:
    now = _now()
    payload = {
        "sub":  str(user_id),
        "type": "refresh",
        "iat":  now,
        "exp":  now + timedelta(days=SETTINGS.REFRESH_TTL_DAYS),
        "iss":  SETTINGS.JWT_ISSUER,
        "ver":  SETTINGS.TOKEN_VER,
    }
    return jwt.encode(payload, SETTINGS.JWT_SECRET, algorithm=SETTINGS.JWT_ALG)


def _decode(token: str, expected_type: str) -> dict:
    """Verify signature + issuer + expiry, then check the token type and
    schema version. Raises jose.JWTError on any failure."""
    claims = jwt.decode(
        token,
        SETTINGS.JWT_SECRET,
        algorithms=[SETTINGS.JWT_ALG],
        issuer=SETTINGS.JWT_ISSUER,
    )  # raises on bad sig / expiry / issuer
    if claims.get("type") != expected_type:
        raise JWTError(f"expected {expected_type} token")
    if claims.get("ver") != SETTINGS.TOKEN_VER:
        raise JWTError("stale token version")
    return claims


def decode_access_token(token: str) -> dict:
    return _decode(token, "access")


def decode_refresh_token(token: str) -> dict:
    return _decode(token, "refresh")
