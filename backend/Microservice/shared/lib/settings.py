"""
settings.py
───────────
Central auth/token configuration. Read once at import; sourced from env so
secrets never live in code or git (§9 of MULTITENANCY_AUTH_CONCEPT.md).

Locally, put JWT_SECRET in a `.env`. In AWS, inject it from Secrets Manager /
SSM (SecureString) as the `JWT_SECRET` env var.
"""
import os
import logging

_log = logging.getLogger("kavachio.auth")

# Dev fallback so the app still boots without a configured secret. This is NOT
# safe for any shared/prod environment — a warning is emitted so it can't slip
# by unnoticed.
_DEV_SECRET = "dev-insecure-change-me"


class Settings:
    JWT_SECRET       = os.getenv("JWT_SECRET", _DEV_SECRET)
    JWT_ALG          = os.getenv("JWT_ALG", "HS256")
    JWT_ISSUER       = os.getenv("JWT_ISSUER", "kavachio-auth")
    ACCESS_TTL_MIN   = int(os.getenv("ACCESS_TTL_MIN", "60"))      # short-lived
    REFRESH_TTL_DAYS = int(os.getenv("REFRESH_TTL_DAYS", "7"))     # 7-day session
    # Bump to force-logout everyone after a key rotation (checked in decode).
    TOKEN_VER        = int(os.getenv("JWT_TOKEN_VER", "1"))


SETTINGS = Settings()

if SETTINGS.JWT_SECRET == _DEV_SECRET:
    _log.warning(
        "JWT_SECRET is unset — using an insecure development secret. "
        "Set JWT_SECRET in the environment before deploying."
    )
