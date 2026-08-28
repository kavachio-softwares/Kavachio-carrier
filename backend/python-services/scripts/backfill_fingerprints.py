"""One-shot: write every kavachio_ops.mappers row into the canonical
column_mapping_fingerprint table and link the two via mappers.fingerprint_id.

Usage:
    cd backend
    source venv/bin/activate
    python -m scripts.backfill_fingerprints           # all unlinked rows
    python -m scripts.backfill_fingerprints --force    # re-upsert ALL rows
"""
from __future__ import annotations

import argparse
import os
import sys

# Make the backend package importable when run as a module from any cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import SessionLocal, CanonicalSession, Mapper, init_db
from fingerprint import upsert as fingerprint_upsert


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--force", action="store_true",
                   help="re-upsert mappers that already have a fingerprint_id")
    args = p.parse_args()

    init_db()
    written = 0
    skipped = 0
    failed = 0

    from sqlalchemy import text as _text

    def _tenant_name(session, tenant_id):
        if not tenant_id:
            return None
        r = session.execute(
            _text("SELECT tenant_name FROM tenant WHERE tenant_id=:t LIMIT 1"),
            {"t": tenant_id}).fetchone()
        return r[0] if r else None

    with SessionLocal() as ops, CanonicalSession() as cs:
        q = ops.query(Mapper).order_by(Mapper.id.asc())
        if not args.force:
            q = q.filter(Mapper.fingerprint_id.is_(None))
        rows = q.all()
        print(f"Found {len(rows)} mapper(s) to backfill "
              f"(force={args.force}).")
        for m in rows:
            try:
                fp_id = fingerprint_upsert(
                    cs,
                    mga=_tenant_name(ops, m.tenant_id),
                    signature_tokens=m.signature or [],
                    canonical_mapping=m.spec_by_sheet or {},
                )
                cs.commit()
                if fp_id is None:
                    failed += 1
                    print(f"  mapper#{m.id}: fingerprint_upsert returned None")
                    continue
                if m.fingerprint_id != fp_id:
                    m.fingerprint_id = fp_id
                    ops.commit()
                    written += 1
                    print(f"  mapper#{m.id} → fingerprint#{fp_id}")
                else:
                    skipped += 1
            except Exception as e:
                cs.rollback(); ops.rollback()
                failed += 1
                print(f"  mapper#{m.id}: FAILED — {e}")

    print()
    print(f"Done. linked={written} already_linked={skipped} failed={failed}")


if __name__ == "__main__":
    main()
