"""Password hashing utilities (bcrypt).

Correct flow
------------
1. Browser sends the plain-text password over HTTPS (TLS handles transport).
2. Backend calls hash_password() before storing — only the bcrypt hash is
   ever written to the database.
3. On login, verify_password() compares the submitted plain password against
   the stored bcrypt hash.  The plain password is never persisted.

Migration path for legacy plain-text rows
------------------------------------------
Rows that pre-date this change have the plain password stored as-is.
verify_password() detects this (no $2b$ prefix) and falls back to a plain
equality check.  The caller (auth_login) then immediately re-hashes and
saves the bcrypt version so the row is upgraded on first successful login.
"""
import bcrypt

_BCRYPT_PREFIXES = ("$2b$", "$2a$", "$2y$")


def _is_bcrypt(value: str) -> bool:
    return any(value.startswith(p) for p in _BCRYPT_PREFIXES)


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of the plain-text password (cost=12)."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, stored: str) -> bool:
    """Return True if *plain* matches *stored*.

    Handles two stored formats:
      1. bcrypt hash  — use bcrypt.checkpw (secure, constant-time)
      2. plain text   — direct equality (legacy rows; triggers rehash in caller)
    """
    if not stored:
        return False
    if _is_bcrypt(stored):
        try:
            return bcrypt.checkpw(plain.encode(), stored.encode())
        except Exception:
            return False
    # Legacy plain-text fallback — only reached for rows created before
    # this module existed.  The caller rehashes on success.
    return plain == stored
