"""Add 'unknown' to the transaction_type_e enum.

WHY
───
`_TXN_TYPE_MAP.get(nor, "new")` used to default an unrecognised or blank BDX
transaction code to "new" — asserting NEW BUSINESS on a row we failed to
classify. That is a false statement, and an unrecoverable one: once written, a
defaulted row is byte-identical to a genuine new-business row.

It is not hypothetical. Before this change the canonical warehouse held

    premium_transaction rows typed 'new':        42 110
    ...of those, with a NEGATIVE premium:          8 455

New business is never negative, so those 8 455 are cancellations, reversals or
endorsements whose code did not map. Their original values are gone.

Palms BDX Ingestion BRD v1.2, Appendix 2 §2.8 requires the opposite:

    COALESCE(rt.prs_transaction_short_types, 'UNK')
    "Null transaction codes are not permitted in output. 'UNK' is the required
     default."

The point of their 'UNK' is that it stays VISIBLE — anyone can search for it and
find every affected row, unlike a silent default that looks like real data.

WHY 'unknown' AND NOT 'UNK'
──────────────────────────
'UNK' is Palms' shortcode vocabulary. We already translate that vocabulary at
ingest — NB→new, EN→endorsement, ENDT→endorsement (see _TXN_TYPE_MAP) — and the
enum is lowercase snake_case throughout (new, renewal, flat_cancellation).
'unknown' is the canonical form of their 'UNK'; translating it is exactly what
every other code in that map already does.

SCOPE
─────
Purely ADDITIVE. The nine existing values keep working, no row is touched, no
table is rewritten, and nothing in the codebase branches on the value (verified:
zero equality comparisons against transaction_type anywhere in the Python).

NOT REVERSIBLE. PostgreSQL has no `ALTER TYPE ... DROP VALUE`. Undoing this means
recreating transaction_type_e and rewriting both columns that use it
(policy.transaction_type, premium_transaction.transaction_type). That is the one
reason this is a script you run deliberately rather than a migration that rides
along with a deploy.

Idempotent via ADD VALUE IF NOT EXISTS, and run with AUTOCOMMIT because a value
added inside a transaction block cannot be used until that block commits.

USAGE
─────
    python -m scripts.add_unknown_transaction_type            # dry run
    python -m scripts.add_unknown_transaction_type --apply
    python -m scripts.add_unknown_transaction_type --sql      # for QA/prod
"""
from __future__ import annotations

import argparse
import os
import sys

_SVC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SVC_DIR)

# db.py reads DATABASE_URL at import time and only main.py loads the .env file,
# so a script run standalone would otherwise build the engine with None.
# override=False, so an already-exported DATABASE_URL still wins.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_SVC_DIR, ".env"), override=False)
except ImportError:
    pass

ENUM_TYPE = "transaction_type_e"
NEW_VALUE = "unknown"
DDL = f"ALTER TYPE {ENUM_TYPE} ADD VALUE IF NOT EXISTS '{NEW_VALUE}';"


def emit_sql() -> None:
    """Print the DDL so QA/prod can be done by whoever owns those DBs."""
    print(f"-- Add '{NEW_VALUE}' to {ENUM_TYPE}. Additive; NOT reversible.")
    print("-- Must run OUTSIDE a transaction block, or in one that commits")
    print("-- before the value is used.")
    print(DDL)


def _values(conn) -> list[str]:
    from sqlalchemy.sql import text
    return [r[0] for r in conn.execute(text("""
        SELECT e.enumlabel FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = :ty ORDER BY e.enumsortorder"""), {"ty": ENUM_TYPE}).all()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="run the ALTER TYPE (default is a dry run)")
    ap.add_argument("--sql", action="store_true",
                    help="print the DDL and exit (for QA/prod)")
    args = ap.parse_args()

    if args.sql:
        emit_sql()
        return 0

    from db import canonical_engine

    # AUTOCOMMIT: a value added inside a transaction block cannot be used until
    # the block commits, and ALTER TYPE ... ADD VALUE is not transactional on
    # older servers at all.
    with canonical_engine.connect().execution_options(
            isolation_level="AUTOCOMMIT") as conn:
        before = _values(conn)
        print(f"{ENUM_TYPE} currently allows ({len(before)}):")
        for v in before:
            print(f"   {v}")

        if NEW_VALUE in before:
            print(f"\n'{NEW_VALUE}' is already present — nothing to do.")
            return 0

        print(f"\nWILL ADD: '{NEW_VALUE}'")
        print("This is ADDITIVE (nothing existing changes) but NOT reversible.")

        if not args.apply:
            print("\nDry run — re-run with --apply to write.")
            return 0

        from sqlalchemy.sql import text
        conn.execute(text(DDL))
        after = _values(conn)
        print(f"\nDone. {ENUM_TYPE} now allows ({len(after)}): {', '.join(after)}")
        if NEW_VALUE not in after:
            print("WARNING: the value is still absent — the ALTER did not take.")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
