"""Load the US ZIP/state reference into Postgres.

The zip↔state and state-validity checks used to read a Parquet file bundled in
the repo. This moves that reference INTO the database so it is centrally owned,
editable with SQL, and identical for every deployment reading the same DB.

Source of truth for the initial load is still the bundled Parquet (or its source
workbook); after that the table is authoritative and can be updated in place.

Idempotent — safe to re-run; it replaces the table's contents in one transaction.

    python scripts/load_uszips_to_db.py            # load from bundled parquet
    python scripts/load_uszips_to_db.py --verify   # just report what's in the DB
"""
import argparse
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from sqlalchemy import create_engine, text  # noqa: E402

from contract_upload_services.uszips_reference import (  # noqa: E402
    USZIPS_PARQUET, USZIPS_DB_TABLE, build_parquet,
)

DDL = f"""
CREATE TABLE IF NOT EXISTS {USZIPS_DB_TABLE} (
    zip        varchar(16) NOT NULL,
    state_id   varchar(8)  NOT NULL,
    state_name varchar(64) NOT NULL
)
"""
# zip is the natural lookup key for the zip↔state rule; state_id/state_name for
# the state-validity rule's DISTINCT scan.
INDEXES = [
    f"CREATE INDEX IF NOT EXISTS uszip_reference_zip_idx ON {USZIPS_DB_TABLE} (zip)",
    f"CREATE INDEX IF NOT EXISTS uszip_reference_state_idx ON {USZIPS_DB_TABLE} (state_id)",
]


def read_parquet_rows():
    """Read the bundled Parquet as all-VARCHAR rows (zip stays zero-padded)."""
    import duckdb
    if not os.path.exists(USZIPS_PARQUET):
        build_parquet()
    if not os.path.exists(USZIPS_PARQUET):
        sys.exit(f"ABORT: no source parquet at {USZIPS_PARQUET} and it could not be rebuilt.")
    path = USZIPS_PARQUET.replace("'", "''")
    return duckdb.connect().execute(
        "SELECT CAST(zip AS VARCHAR), CAST(state_id AS VARCHAR), "
        f"CAST(state_name AS VARCHAR) FROM read_parquet('{path}')"
    ).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="report DB contents, load nothing")
    ap.add_argument("--url", help="target DB (defaults to DATABASE_URL from .env). "
                                  "Use this to load a second environment, e.g. Azure, "
                                  "without editing .env.")
    args = ap.parse_args()

    url = args.url or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("ABORT: no --url given and DATABASE_URL is not set.")
    eng = create_engine(url, future=True)
    # Say which server is being written to — the whole point of --url is that it
    # may NOT be the one .env points at.
    with eng.connect() as c:
        who = c.execute(text("SELECT current_database(), inet_server_addr()::text")).first()
    print(f"target: db={who[0]!r} host={who[1]!r}")

    if not args.verify:
        rows = read_parquet_rows()
        print(f"read {len(rows)} rows from parquet")
        if not rows:
            sys.exit("ABORT: parquet produced 0 rows; refusing to wipe the table.")

        with eng.begin() as c:
            c.execute(text(DDL))
            for ddl in INDEXES:
                c.execute(text(ddl))
            c.execute(text(f"TRUNCATE {USZIPS_DB_TABLE}"))
            # COPY is dramatically faster than 33k INSERTs and keeps the whole
            # replace inside the surrounding transaction.
            raw = c.connection.driver_connection   # underlying psycopg2 connection
            buf = io.StringIO()
            for zip_, sid, sname in rows:
                # No delimiters/newlines occur in this reference data; guard anyway.
                buf.write("\t".join(str(v or "").replace("\t", " ").replace("\n", " ")
                                    for v in (zip_, sid, sname)) + "\n")
            buf.seek(0)
            with raw.cursor() as cur:
                cur.copy_expert(
                    f"COPY {USZIPS_DB_TABLE} (zip, state_id, state_name) FROM STDIN", buf)
        print("loaded into", USZIPS_DB_TABLE)

    with eng.connect() as c:
        n = c.execute(text(f"SELECT count(*) FROM {USZIPS_DB_TABLE}")).scalar()
        states = c.execute(text(f"SELECT count(DISTINCT state_id) FROM {USZIPS_DB_TABLE}")).scalar()
        print(f"\n{USZIPS_DB_TABLE}: {n} rows, {states} distinct states")
        print("sample:")
        for r in c.execute(text(
                f"SELECT zip, state_id, state_name FROM {USZIPS_DB_TABLE} ORDER BY zip LIMIT 5")):
            print("   ", tuple(r))


if __name__ == "__main__":
    main()
