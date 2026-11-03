"""The one-time code that unlocks a signing link, and the session it opens.

WHY THE LINK ALONE WAS NOT ENOUGH
---------------------------------
The emailed URL used to be the whole credential. URLs leak in ways a mailbox
does not: browser history, a pasted chat message, a screen share, a proxy log,
an address bar read over a shoulder — and most often a forwarded email, where
the sender only meant to ask a colleague what they thought of clause 4.2.

So there are two factors now: something emailed to you (the link) and something
you have to read out of that email and type (the code). It is honest about what
it does NOT do — the code travels in the same message, so it is no defence
against someone who has taken the mailbox itself. Sending the code out of band
is what defends against that, and nothing here cares how the code reached the
signer, so that is a delivery change and not a rewrite.

WHAT PROTECTS A SIX-DIGIT SECRET
--------------------------------
Not the hash. A million possibilities falls to a script in minutes if you let it
guess. The lockout does: five wrong answers and the link stops accepting codes
for fifteen minutes, right or wrong. That turns a one-in-a-million guess into an
attack measured in years, and one that is loud in the audit trail from the
first few tries.

Two further rules follow from the same thought:

  * A wrong code and an expired code answer the SAME way — "that code is not
    right" — because "expired" tells an attacker the link is real.
  * The code is compared with bcrypt, which is deliberately slow. On a
    six-digit secret that cost is a feature.

AFTER THE CODE
--------------
Typing it once is enough. Verification mints a short-lived session (a signed
JWT, 45 minutes) that the signing screen presents on every later call. It is
bound to the recipient AND to the link it was opened with, so a session cannot
be carried across to a different signer's link — and it is short enough that a
laptop left open in a coffee shop stops being a way in before the day is out.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from auth_utils import hash_password, verify_password
from settings import SETTINGS

log = logging.getLogger("bdx.esign.otp")

# Six digits: long enough to be worth locking out over, short enough to read off
# a phone screen and type without a second look at the email.
OTP_DIGITS = 6

# Wrong answers allowed before the link stops listening.
MAX_ATTEMPTS = int(os.getenv("ESIGN_OTP_MAX_ATTEMPTS", "5"))
LOCKOUT_MINUTES = int(os.getenv("ESIGN_OTP_LOCKOUT_MIN", "15"))

# How long a signer stays unlocked after typing the code. Long enough to read a
# contract properly; short enough that an unattended browser is not a way in.
SESSION_MINUTES = int(os.getenv("ESIGN_SESSION_MIN", "45"))

# A fresh code may be emailed at most this often, and this many times per link.
RESEND_COOLDOWN_SECONDS = int(os.getenv("ESIGN_OTP_RESEND_COOLDOWN", "60"))
MAX_SENDS = int(os.getenv("ESIGN_OTP_MAX_SENDS", "10"))

SESSION_TYPE = "esign_session"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_code() -> str:
    """A fresh code. `randbelow` is the CSPRNG — `random` is seeded from the
    clock and its output is predictable to anyone who knows roughly when the
    contract was sent."""
    return f"{secrets.randbelow(10 ** OTP_DIGITS):0{OTP_DIGITS}d}"


def hash_code(code: str) -> str:
    """bcrypt, the same as a password. Slow on purpose."""
    return hash_password(code)


def check_code(code: str, stored_hash: str | None) -> bool:
    if not stored_hash or not code:
        return False
    return verify_password(code.strip(), stored_hash)


def token_fingerprint(token: str) -> str:
    """A short, non-reversible stand-in for the link, so a session can name the
    link it was opened with WITHOUT carrying the link itself.

    The session is a JWT: readable by anyone holding it. Putting the signing
    token in its claims would mean a leaked session leaks the link too, which
    is the exact thing the code exists to stop."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


# --- the session a correct code opens ---------------------------------------
def mint_session(recipient_id: int, envelope_id: int, token: str) -> str:
    now = _now()
    return jwt.encode(
        {
            "sub": str(recipient_id),
            "env": envelope_id,
            # Bound to the link it was opened with: a session handed to another
            # signer's link is rejected rather than silently accepted.
            "lnk": token_fingerprint(token),
            "type": SESSION_TYPE,
            "iat": now,
            "exp": now + timedelta(minutes=SESSION_MINUTES),
            "iss": SETTINGS.JWT_ISSUER,
        },
        SETTINGS.JWT_SECRET,
        algorithm=SETTINGS.JWT_ALG,
    )


def read_session(session: str, token: str) -> int | None:
    """The recipient id this session unlocks, or None if it does not.

    None for every failure — expired, tampered, wrong link, wrong kind of token
    — because the caller's answer is the same in each case and telling them
    apart only helps somebody probing.
    """
    if not session:
        return None
    try:
        claims = jwt.decode(session, SETTINGS.JWT_SECRET,
                            algorithms=[SETTINGS.JWT_ALG],
                            issuer=SETTINGS.JWT_ISSUER)
    except JWTError:
        return None
    if claims.get("type") != SESSION_TYPE:
        return None
    if claims.get("lnk") != token_fingerprint(token):
        return None
    try:
        return int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        return None


# --- lockout bookkeeping ----------------------------------------------------
def is_locked(locked_until) -> bool:
    if locked_until is None:
        return False
    lu = locked_until if locked_until.tzinfo else locked_until.replace(tzinfo=timezone.utc)
    return lu > _now()


def lockout_seconds_left(locked_until) -> int:
    if not is_locked(locked_until):
        return 0
    lu = locked_until if locked_until.tzinfo else locked_until.replace(tzinfo=timezone.utc)
    return max(1, int((lu - _now()).total_seconds()))


def next_lockout() -> datetime:
    return _now() + timedelta(minutes=LOCKOUT_MINUTES)


def masked_email(email: str) -> str:
    """`m****o@crcinsurisk.com` — enough for a signer to recognise which inbox
    to look in, not enough to hand an address to somebody who did not have it."""
    name, _, domain = (email or "").partition("@")
    if not domain:
        return ""
    if len(name) <= 2:
        return f"{name[:1]}***@{domain}"
    return f"{name[0]}{'*' * min(len(name) - 2, 6)}{name[-1]}@{domain}"
