"""Create (or reset) a kavachio_admin — the platform seat.

kavachio_admin is the only role that belongs to NOBODY: it carries neither a
tenant_id (it is not a carrier's user) nor a broker_party_id (not a broker's).
That is what makes it cross-tenant, and it is why this seat cannot be created
through the normal /users API — every route there writes a user INTO the
caller's own carrier or broker. So it is seeded here instead.

Idempotent: running it again on an existing email resets that account's
password, role and status rather than failing or creating a duplicate.

Usage:
    cd backend/python-services
    python -m scripts.create_kavachio_admin --email admin@kavachio.dev --password '...'

    # or non-interactively, e.g. in a provisioning step
    KAVACHIO_ADMIN_EMAIL=... KAVACHIO_ADMIN_PASSWORD=... python -m scripts.create_kavachio_admin
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# main.py loads the .env; this script does not import main, so load it here or
# DATABASE_URL is unset and the engine points nowhere.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:  # pragma: no cover — dotenv is a normal dependency
    pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default=os.getenv("KAVACHIO_ADMIN_EMAIL"))
    ap.add_argument("--password", default=os.getenv("KAVACHIO_ADMIN_PASSWORD"))
    ap.add_argument("--name", default=os.getenv("KAVACHIO_ADMIN_NAME", "Kavachio Admin"))
    args = ap.parse_args()

    if not args.email or not args.password:
        raise SystemExit("--email and --password are required "
                         "(or KAVACHIO_ADMIN_EMAIL / KAVACHIO_ADMIN_PASSWORD)")

    from auth_utils import hash_password
    from db import AppUser, SessionLocal, engine

    print(f"target: {engine.url.render_as_string(hide_password=True)}")

    email = args.email.strip().lower()
    with SessionLocal() as s:
        u = s.query(AppUser).filter(AppUser.email == email).first()
        action = "updated" if u else "created"
        if u is None:
            u = AppUser(email=email)
            s.add(u)
        u.full_name = args.name
        u.role = "kavachio_admin"
        u.status = "active"          # auth_login rejects any other status
        u.tenant_id = None           # platform seat — belongs to no carrier
        u.broker_party_id = None     # …and to no broker
        u.password = hash_password(args.password)   # bcrypt, never plain text
        s.commit()
        s.refresh(u)
        print(f"{action}: id={u.id}  {u.email}  role={u.role}  status={u.status}")

    # Prove the stored hash actually verifies, so a bad write cannot look like
    # a success and leave someone unable to log in.
    from auth_utils import verify_password
    with SessionLocal() as s:
        u = s.query(AppUser).filter(AppUser.email == email).first()
        ok = bool(u and verify_password(args.password, u.password))
        print(f"password verifies: {ok}")
        if not ok:
            raise SystemExit("password did not verify after write")


if __name__ == "__main__":
    main()
