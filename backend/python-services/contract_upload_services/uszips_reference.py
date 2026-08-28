"""
uszips_reference.py
───────────────────
Full US ZIP-code → state reference table for the `zip_state_consistency` and
`state_validity` validation checks, loaded into DuckDB as a REAL table (`uszips`)
that the rules JOIN against row-by-row — instead of an inline ZIP3-prefix
approximation or a literal list of state codes.

The reference data ships as a small bundled Parquet file
(`data/uszips.parquet`, ~60 KB, 33 782 rows: zip, state_id, state_name) built
once from the source `uszips.xlsx`. It is PUBLIC, universal postal reference
data — the same category as the `_US_TERRITORIES` / `_COUNTRY_ALIASES` tables
already embedded in rule_normalizer.py — NOT contract / carrier / MGA-specific.
Nothing is written to Postgres; the table lives only in the ephemeral
validation DuckDB, created fresh per run.

Contract:
  * `zip`        — the canonical 5-digit ZIP as VARCHAR, zero-padded
                   ('00601', '85009'). Zero padding survives Excel's habit of
                   dropping leading zeros ('06390' → 6390).
  * `state_id`   — 2-letter USPS code, UPPER ('PR', 'AZ').
  * `state_name` — full state/territory name, UPPER ('PUERTO RICO', 'ARIZONA').

Because a ZIP maps to exactly ONE state, the rule can flag a row whose ZIP is a
known US ZIP but is assigned to a DIFFERENT state than the one reported on the
same row. Unknown / foreign / blank ZIPs are never flagged (they simply don't
match any `uszips` row), keeping the check conservative.

The same table doubles as the authoritative list of US states: its `state_id` /
`state_name` columns are the DISTINCT set of every USPS code and full state name
(50 states + DC + territories), which is what `state_validity` probes so that no
state vocabulary is ever hardcoded in SQL.

Loading MUST happen while DuckDB external access is still enabled (the reader
touches a file); `duckdb_validation.build_connection` calls `load_reference_tables`
BEFORE it locks the sandbox with `SET enable_external_access=false`. The path is
a fixed code constant, never AI-influenced, so this widens no attack surface.
"""

import os
import re

# Bundled Parquet reference (built once from uszips.xlsx — see scripts/build note
# in git history). Resolved relative to THIS module so it works regardless of the
# process working directory.
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
USZIPS_PARQUET = os.path.join(_DATA_DIR, "uszips.parquet")

# The DuckDB table name the compiled zip rule references. Kept as a module
# constant so the compiler and the loader can never drift apart.
USZIPS_TABLE = "uszips"

# The POSTGRES table that now owns this reference data (loaded by
# scripts/load_uszips_to_db.py). The DuckDB table above is still called `uszips`
# so every already-compiled rule keeps working untouched — only the SOURCE of
# its rows moved from the bundled Parquet into the database.
USZIPS_DB_TABLE = "uszip_reference"

# Reference data is effectively static, and a validation run would otherwise pull
# ~34k rows over the network every time. Cache the fetched rows for the life of
# the process; `refresh_db_cache()` drops it if the table is ever updated in place.
_db_rows_cache = None


def refresh_db_cache():
    """Forget cached reference rows so the next run re-reads them from Postgres."""
    global _db_rows_cache
    _db_rows_cache = None


def fetch_rows_from_db():
    """Return [(zip, state_id, state_name)] from Postgres, or None if unavailable.

    Never raises: any failure (table not yet created, DB unreachable) returns None
    so `load_reference_tables` can fall back to the bundled Parquet rather than
    failing the whole validation run.
    """
    global _db_rows_cache
    if _db_rows_cache is not None:
        return _db_rows_cache
    try:
        from sqlalchemy import text
        from db import engine
        with engine.connect() as c:
            rows = c.execute(text(
                f"SELECT CAST(zip AS VARCHAR), CAST(state_id AS VARCHAR), "
                f"CAST(state_name AS VARCHAR) FROM {USZIPS_DB_TABLE}")).fetchall()
        if not rows:
            print(f"[uszips] {USZIPS_DB_TABLE} is empty; falling back to parquet.")
            return None
        _db_rows_cache = [tuple(r) for r in rows]
        return _db_rows_cache
    except Exception as exc:
        print(f"[uszips] could not read {USZIPS_DB_TABLE} ({exc}); falling back to parquet.")
        return None

# Column-name tokens that mark a ZIP/postal column. A zip_state_consistency rule
# can only exist when the template carries such a column, so the reference table
# is loaded ONLY when one is present — most validations skip the load entirely.
# "postcode" is the everyday spelling outside North America and reaches the same
# check; it is matched on the SQUASHED name (see `is_postal_column`) so "Post
# Code", "PostCode" and "Postcode" are one case, not three.
_ZIP_TOKENS = ("zip", "postal", "postcode")

# Column-name tokens that mark a US STATE column. `state_validity` rules make the
# reference table necessary for a STATE column too — including a BDX that carries
# a state column but no ZIP column at all, which the zip gate alone would skip
# (leaving the state rule to query a table that was never created).
_STATE_TOKENS = frozenset(("state", "states"))

# camelCase / PascalCase boundary: a lower-or-digit followed by an upper
# ("InsuredState" → Insured|State, "Zip5Code" → Zip5|Code), and the tail of an
# acronym run that starts a new word ("UMRState" → UMR|State). Splitting on
# non-alphanumerics ALONE is not enough: Lloyd's-style BDX headers carry no
# separators at all, and "InsuredState" would otherwise be the single opaque
# token "insuredstate" — invisible to every token test below.
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def column_tokens(name) -> list:
    """A column NAME split into whole lowercase words — the one tokenizer every
    column-role test in the codebase shares.

    "Insured State" / "state_code" / "Risk-State" / "InsuredState" all yield
    ['insured', 'state']-shaped lists, while the words that merely CONTAIN
    "state" — "Real Estate", "Statement Date", "Interstate" — do not, which is
    the whole reason these tests are token-based rather than substring-based.
    Generic word splitting only: no column name, carrier or MGA-specific value
    is named here."""
    return re.findall(r"[a-z0-9]+", _CAMEL_SPLIT.sub(" ", str(name or "")).lower())


def squashed_name(name) -> str:
    """The column name as one lowercase alphanumeric run ("Post Code" →
    "postcode"), for the few tests that must ignore word boundaries because the
    same term is written both as one word and as two."""
    return "".join(column_tokens(name))


def is_postal_column(name) -> bool:
    """True when a column NAME denotes a ZIP / postal-code column.

    Matched on the squashed name so every spelling of the same term collapses to
    one case — "Insured Zip Code", "InsuredZipCode", "Postal Code", "PostCode"
    and "Postcode" are all postal columns. Shared by the rule generator (which
    emits the `zip_state_consistency` rules) and the load gate below, so the
    emit side and the load side can never disagree."""
    sq = squashed_name(name)
    return any(tok in sq for tok in _ZIP_TOKENS)


def _sheet_column_names(records_by_sheet, schema_cols):
    """Every column name across the loaded schema (template columns first, then
    any keys present in the data), plus the set of (stripped, lowered) sheet
    names — used for the zip-present gate and the table-name collision guard."""
    cols, sheets = [], set()
    for sh, cs in (schema_cols or {}).items():
        sheets.add(str(sh).strip().lower())
        cols.extend(cs or [])
    for block in (records_by_sheet or []):
        sheets.add(str(block.get("sheet") or "").strip().lower())
        for rec in (block.get("records") or []):
            cols.extend(rec.keys())
    return cols, sheets


def has_zip_column(records_by_sheet, schema_cols) -> bool:
    """True when any loaded column looks like a ZIP/postal column — one of the two
    cases in which a rule (and hence the reference table) is needed."""
    cols, _ = _sheet_column_names(records_by_sheet, schema_cols)
    return any(is_postal_column(c) for c in cols)


def is_state_column(name) -> bool:
    """True when a column NAME denotes a US state column.

    Matched on whole NAME TOKENS rather than a substring, because "state" sits
    inside ordinary insurance words that are not states at all — "Real Estate",
    "Statement Date", "Interstate" — and a substring test would emit a state rule
    against every one of them. `column_tokens` splits on non-alphanumerics AND on
    camelCase humps, so "state_code", "Risk-State" and the separator-less
    "InsuredState" all match, which neither a `\\bstate\\b` regex ("_" is a regex
    word character) nor a plain non-alphanumeric split would do. Only the generic
    word "state" is consulted: no column name, carrier or MGA-specific value is
    named here.

    Shared by the rule generator (which emits one `state_validity` rule per such
    column) and the load gate below, so the two can never drift apart — the drift
    that would otherwise leave a generated rule querying a missing table."""
    return bool(_STATE_TOKENS.intersection(column_tokens(name)))


def has_state_column(records_by_sheet, schema_cols) -> bool:
    """True when any loaded column is a US STATE column — the other case in which
    the reference table is needed (for `state_validity`)."""
    cols, _ = _sheet_column_names(records_by_sheet, schema_cols)
    return any(is_state_column(c) for c in cols)


# --- Self-heal: rebuild the bundled Parquet from its SOURCE when it is missing ---
# The Parquet is a pre-built cache of `uszips.xlsx`. If it is deleted (e.g. an
# untracked data/ dir wiped by `git clean`), regenerate it from the source workbook
# on demand so the zip check keeps working without a manual step. Best-effort — a
# missing source or a read-only filesystem just falls back to the existing
# "skip → not-validated" behaviour, so a rebuild attempt can never break a run.
_REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
# Candidate locations for the source workbook (a plain .xlsx, or a directory that
# wraps one — the repo currently ships `uszips.xlsx/` as a folder). Resolved by
# _find_source_xlsx; nothing here is contract/MGA-specific.
_SOURCE_XLSX_CANDIDATES = (
    os.path.join(_REPO_ROOT, "uszips.xlsx"),
    os.path.join(_DATA_DIR, "uszips.xlsx"),
)


def _find_source_xlsx():
    """First readable .xlsx among the candidates (descending into a directory that
    wraps the workbook). Returns a path or None."""
    for cand in _SOURCE_XLSX_CANDIDATES:
        if os.path.isfile(cand):
            return cand
        if os.path.isdir(cand):
            inner = os.path.join(cand, "uszips.xlsx")
            if os.path.isfile(inner):
                return inner
            for f in sorted(os.listdir(cand)):
                if f.lower().endswith(".xlsx") and os.path.isfile(os.path.join(cand, f)):
                    return os.path.join(cand, f)
    return None


def build_parquet(dest: str = USZIPS_PARQUET) -> bool:
    """(Re)build the uszips Parquet from the source workbook, applying the SAME
    normalization the compiled rule joins on: 5-digit zero-padded ZIP, UPPER state
    code and UPPER state name. Writes ATOMICALLY (temp file + os.replace) so a
    concurrent validation run never reads a half-written file. Returns True on
    success, False if the source is unavailable or the write fails. No values are
    hardcoded — every row comes from the source workbook. Also usable as a
    deterministic build step (see scripts/build_reference_data.py)."""
    src = _find_source_xlsx()
    if not src:
        # Parquet missing AND no source workbook shipped (e.g. a packaged /
        # site-packages deploy where uszips.xlsx isn't alongside the code). We
        # can't self-heal here — degrade gracefully to "not-validated", but LOG it
        # so the silent zip-check degradation is visible to operators (commit the
        # parquet, or ship uszips.xlsx + run scripts/build_reference_data at deploy).
        print(f"[uszips] cannot auto-rebuild: source workbook not found "
              f"(looked in {list(_SOURCE_XLSX_CANDIDATES)}); "
              f"zip checks will report not-validated.")
        return False
    tmp = None
    try:
        from openpyxl import load_workbook   # lazy: only when a rebuild is needed
        import duckdb
        import uuid
        wb = load_workbook(src, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [str(h).strip().lower() if h is not None else "" for h in next(it)]
        iz, iid, inm = (header.index("zip"), header.index("state_id"),
                        header.index("state_name"))
        rows, seen = [], set()
        for r in it:
            z = r[iz] if iz < len(r) else None
            sid = r[iid] if iid < len(r) else None
            snm = r[inm] if inm < len(r) else None
            if z is None or sid is None:
                continue
            digits = "".join(ch for ch in str(z) if ch.isdigit())
            if not digits:
                continue
            key = (digits[:5].zfill(5), str(sid).strip().upper(),
                   str(snm).strip().upper() if snm is not None else "")
            if key in seen:
                continue
            seen.add(key)
            rows.append(key)
        wb.close()
        if not rows:
            return False
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # Unique per CALL (pid + uuid), so two concurrent rebuilds — even two
        # threads in one process — never share a temp file; os.replace is atomic.
        tmp = f"{dest}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        con = duckdb.connect(":memory:")
        try:
            con.execute("CREATE TABLE _u(zip VARCHAR, state_id VARCHAR, "
                        "state_name VARCHAR)")
            con.executemany("INSERT INTO _u VALUES (?,?,?)", rows)
            con.execute(f"COPY _u TO '{tmp.replace(chr(39), chr(39) * 2)}' "
                        f"(FORMAT PARQUET)")
        finally:
            con.close()
        os.replace(tmp, dest)
        print(f"[uszips] rebuilt reference parquet from {src} ({len(rows)} rows)")
        return True
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[uszips] auto-rebuild failed ({exc}); "
              f"zip checks will report not-validated.")
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def load_reference_tables(con, records_by_sheet, schema_cols) -> list:
    """Create the `uszips` reference table in `con`, when the loaded schema has a
    ZIP column OR a STATE column and no sheet already claims the table name.

    Rows come from the POSTGRES table `uszip_reference` (see
    scripts/load_uszips_to_db.py). The bundled Parquet is retained only as a
    fallback for when the DB table is missing/empty/unreachable, so a deployment
    that hasn't been loaded yet keeps validating instead of failing.

    Idempotent and best-effort: any failure is swallowed and returns [] — a
    zip/state rule then simply reports "not validated" rather than crashing the
    whole run. Returns the list of reference table names created (for logging).
    MUST be called before the sandbox lock.
    """
    created = []
    try:
        # EITHER gate: a BDX with a state column but no ZIP column still needs the
        # table, because `state_validity` reads state_id/state_name from it.
        if not (has_zip_column(records_by_sheet, schema_cols)
                or has_state_column(records_by_sheet, schema_cols)):
            return created
        _, sheets = _sheet_column_names(records_by_sheet, schema_cols)
        if USZIPS_TABLE.lower() in sheets:
            # A real sheet already owns this name (pathological); don't clobber it.
            return created

        # --- primary source: the database ---
        rows = fetch_rows_from_db()
        if rows:
            # Register a frame and CREATE TABLE AS, rather than executemany over
            # ~34k rows: the vectorised path is ~250x faster (0.02s vs 5s), which
            # matters because this runs on every validation.
            import pandas as pd
            df = pd.DataFrame(rows, columns=["zip", "state_id", "state_name"],
                              dtype="string")
            con.register("_uszips_src", df)
            try:
                # all-VARCHAR keeps zip a zero-padded string so '00601' compares
                # correctly against a BDX cell.
                con.execute(
                    f"CREATE TABLE {USZIPS_TABLE} AS SELECT "
                    f"CAST(zip AS VARCHAR) AS zip, "
                    f"CAST(state_id AS VARCHAR) AS state_id, "
                    f"CAST(state_name AS VARCHAR) AS state_name FROM _uszips_src")
            finally:
                con.unregister("_uszips_src")
            created.append(USZIPS_TABLE)
            print(f"[uszips] reference loaded from DB table {USZIPS_DB_TABLE} "
                  f"({len(rows)} rows).")
            return created

        # --- fallback: the bundled Parquet ---
        if not os.path.exists(USZIPS_PARQUET):
            build_parquet()          # self-heal a deleted bundle (best-effort)
        if not os.path.exists(USZIPS_PARQUET):
            return created
        # read_parquet needs external access, which is still enabled here. The
        # path is a trusted code constant. all-VARCHAR keeps zip a zero-padded
        # string so '00601' compares correctly.
        path = USZIPS_PARQUET.replace("'", "''")
        con.execute(
            f'CREATE TABLE {USZIPS_TABLE} AS '
            f"SELECT CAST(zip AS VARCHAR) AS zip, "
            f"CAST(state_id AS VARCHAR) AS state_id, "
            f"CAST(state_name AS VARCHAR) AS state_name "
            f"FROM read_parquet('{path}')"
        )
        created.append(USZIPS_TABLE)
        print("[uszips] reference loaded from bundled parquet (DB table unavailable).")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[uszips] reference table not loaded ({exc}); "
              f"zip checks will report not-validated.")
    return created
