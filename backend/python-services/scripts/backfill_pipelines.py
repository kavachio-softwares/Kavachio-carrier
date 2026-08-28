"""One-shot: seed a Pipeline for every currently-approved DirectFormat ("setup")
so /direct/run keeps resolving the same config once it switches to pipeline-based
resolution. Idempotent — safe to re-run (guards on existing input_format_id).

Run AFTER applying docs/migrations/10_pipeline.sql.

Usage:
    cd backend/python-services
    source .venv/bin/activate
    python -m scripts.backfill_pipelines

Under RLS it scopes to each tenant in turn (the app role can only INSERT rows
whose tenant_id matches the session scope). With RLS off it runs once, unscoped.
"""
from __future__ import annotations

import os
import sys

# Make the backend package importable when run as a module from any cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from db import (
    SessionLocal, RLS_ENABLED, set_current_tenant, backfill_pipelines,
)


def main() -> None:
    total = 0
    if not RLS_ENABLED:
        with SessionLocal() as s:
            total = backfill_pipelines(s)
        print(f"[backfill_pipelines] created {total} pipeline(s) (RLS off).")
        return

    # `tenant` is RLS-exempt (migration 08), so we can enumerate tenants even
    # under a scoped app-role connection, then backfill each under its scope.
    with SessionLocal() as s:
        tenant_ids = [r[0] for r in s.execute(text("SELECT tenant_id FROM tenant")).all()]
    for tid in tenant_ids:
        set_current_tenant(tid)
        try:
            with SessionLocal() as s:
                n = backfill_pipelines(s)
            total += n
            if n:
                print(f"[backfill_pipelines] tenant {tid}: created {n} pipeline(s).")
        finally:
            set_current_tenant(None)
    print(f"[backfill_pipelines] done — created {total} pipeline(s) across "
          f"{len(tenant_ids)} tenant(s).")


if __name__ == "__main__":
    main()
