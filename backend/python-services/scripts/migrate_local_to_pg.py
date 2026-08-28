"""One-shot migration of the local ops store from SQLite → PostgreSQL.

Usage (from the backend/ directory, with the venv active):

    # 1. Make sure the target Postgres database exists and is reachable:
    #    createdb -h <host> -U <user> kavachio_ops
    #
    # 2. Run the migration. It reads from bdx.db (or $SOURCE_DB) and writes
    #    to whatever $DATABASE_URL points at (defaults to Postgres).
    python -m scripts.migrate_local_to_pg

    # Optional overrides:
    SOURCE_DB=sqlite:///bdx.db \
    DATABASE_URL=postgresql+psycopg2://<user>:<password>@<host>:5432/kavachio_ops \
        python -m scripts.migrate_local_to_pg

The script is idempotent for empty target tables. If the target already has
rows in a given table, the script SKIPS that table (it does not merge or
overwrite). Pass --truncate to wipe target tables first.
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

# Import the app's metadata so we get exactly the same schema on the target.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db as appdb  # noqa: E402


def _seq_for(table_name: str, pk_col: str) -> str:
    """Postgres serial-sequence name created by SQLAlchemy."""
    return f"{table_name}_{pk_col}_seq"


def migrate(source_url: str, target_engine, truncate: bool) -> None:
    src_engine = create_engine(source_url, future=True)
    SrcSession = sessionmaker(bind=src_engine, autoflush=False, future=True)

    # Use the app's Base.metadata — that's the ops schema.
    md = appdb.Base.metadata

    # Make sure target schema exists.
    md.create_all(target_engine)

    # Ordered by FK dependency so child tables get inserted after their parent.
    order = md.sorted_tables

    src = SrcSession()
    tgt = sessionmaker(bind=target_engine, autoflush=False, future=True)()

    try:
        for t in order:
            name = t.name
            # Count rows on both sides.
            src_count = src.execute(select(t)).rowcount
            tgt_existing = tgt.execute(select(t)).rowcount
            # SQLAlchemy rowcount isn't reliable for SELECTs on all dialects;
            # fall back to a COUNT(*).
            src_count = src.execute(
                text(f'SELECT COUNT(*) FROM "{name}"')
            ).scalar() or 0
            tgt_existing = tgt.execute(
                text(f'SELECT COUNT(*) FROM "{name}"')
            ).scalar() or 0

            if src_count == 0:
                print(f"  {name:25} source empty — skipping")
                continue

            if tgt_existing and not truncate:
                print(f"  {name:25} target has {tgt_existing} rows — skipping "
                      "(pass --truncate to overwrite)")
                continue

            if tgt_existing and truncate:
                # CASCADE so children can be wiped without manual ordering.
                if target_engine.dialect.name == "postgresql":
                    tgt.execute(text(f'TRUNCATE TABLE "{name}" RESTART IDENTITY CASCADE'))
                else:
                    tgt.execute(text(f'DELETE FROM "{name}"'))
                tgt.commit()

            rows = [dict(r._mapping) for r in src.execute(select(t)).all()]
            if not rows:
                continue
            tgt.execute(t.insert(), rows)
            tgt.commit()

            # On Postgres, re-sync the identity sequence to MAX(pk) so future
            # inserts don't collide with the migrated ids.
            if target_engine.dialect.name == "postgresql":
                pk_cols = [c.name for c in t.primary_key.columns]
                if len(pk_cols) == 1:
                    seq = _seq_for(name, pk_cols[0])
                    try:
                        tgt.execute(text(
                            f"SELECT setval(:seq, COALESCE((SELECT MAX({pk_cols[0]}) "
                            f'FROM "{name}"), 1))'
                        ), {"seq": seq})
                        tgt.commit()
                    except Exception as e:
                        # Older schemas may not have a serial sequence; skip.
                        print(f"     (seq sync skipped for {name}: {e})")

            print(f"  {name:25} copied {len(rows)} row(s)")
    finally:
        src.close()
        tgt.close()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source",
                   default=os.getenv("SOURCE_DB", "sqlite:///bdx.db"),
                   help="SQLAlchemy URL of the SQLite source. "
                        "Defaults to sqlite:///bdx.db.")
    p.add_argument("--target",
                   default=os.getenv("DATABASE_URL", appdb.DATABASE_URL),
                   help="SQLAlchemy URL of the Postgres target. Defaults to "
                        "DATABASE_URL / the app's configured ops DB.")
    p.add_argument("--truncate", action="store_true",
                   help="TRUNCATE each target table before inserting "
                        "(overwrite existing rows).")
    args = p.parse_args()

    print(f"Source : {args.source}")
    print(f"Target : {args.target}")
    print(f"Mode   : {'TRUNCATE + COPY' if args.truncate else 'COPY only (skip non-empty)'}")
    print()

    tgt_engine = create_engine(args.target, future=True, pool_pre_ping=True)
    migrate(args.source, tgt_engine, args.truncate)
    print("\nDone.")


if __name__ == "__main__":
    main()
