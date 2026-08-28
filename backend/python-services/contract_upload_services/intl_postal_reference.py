"""
intl_postal_reference.py
────────────────────────
CANADA and UNITED KINGDOM postal-code → region reference data, the international
counterpart of `uszips_reference.py`. Together the two modules let the
`zip_state_consistency` and `state_validity` checks dispatch PER COUNTRY: a US row
is validated against the US reference exactly as before, a Canadian row against the
CA reference, a British row against the GB reference.

Same category as `uszips_reference.py` / `country_currency_reference.py`: PUBLIC,
universal postal reference data (sourced from the GeoNames-derived
`zipcodes.ca.csv` / `zipcodes.gb.csv` shipped in `data/`) — NOT contract, carrier
or MGA-specific. Nothing is written to Postgres; the tables live only in the
ephemeral validation DuckDB, created fresh per run.

WHAT THE SOURCE DATA ACTUALLY IS  (profiled from the CSVs, not assumed)
──────────────────────────────────────────────────────────────────────
CA — 1 657 rows. `zipcode` is the 3-character FORWARD SORTATION AREA (FSA), the
     first half of a postal code: 'T0A' of 'T0A 1A0'. 13 provinces/territories in
     `state`/`state_code`, both always populated, and no FSA spans two provinces.
     Its `province` column holds CITIES ('Abbotsford', 'Ajax'), NOT provinces — so
     it is deliberately NOT used as a region alias here.
GB — 27 450 rows over 3 002 distinct `zipcode` values, which are OUTWARD CODES
     (the part before the space: 'SW1A' of 'SW1A 1AA'), 2–4 characters. `state` is
     the constituent country (England / Scotland / Wales / Northern Ireland) and
     `province` is the COUNTY ('Berkshire', 'Greater Manchester') — a UK BDX
     "state/county" column usually carries the county, so BOTH are accepted as
     region aliases. 104 rows (the Crown Dependencies — Isle of Man, Guernsey,
     Jersey) have a blank `state`; their `province` carries the dependency name, so
     they survive via the county alias. One row carries an ONS code
     ('L93000001') in `state_code`; non-alphabetic state codes are dropped.

WHY A COMPOSITE KEY
───────────────────
Both tables key on `country || ':' || value` so the compiled rule can do SINGLE-
EQUALITY anti-joins (hash-joinable), exactly like `_b_zip_state_consistency`
already does for the US — instead of an OR over three countries, which would force
a nested scan over the reference for every BDX row.

Contract:
  * `postal_reference(key, state_alias)` — key 'US:90001' | 'CA:T0A' | 'GB:SW1A';
    state_alias is the UPPER region code, region name, or (GB) county. One row per
    accepted spelling, so a match is one equality.
  * `postal_state(key)` — DISTINCT 'US:CA' | 'CA:ON' | 'GB:BERKSHIRE'; the
    authoritative region vocabulary per country, so no state list is ever written
    into SQL.
  * `postal_country(alias, country)` — every recognized COUNTRY spelling → the
    alpha-2 of a country we hold postal data for. Lets a row's country column pick
    the slice to validate against.

Verified disambiguation (why "match any supported country" is safe when a BDX has
no country column): US/CA/GB region codes and names have ZERO overlap with each
other, and no CA or GB postal code is all-digits — so a US ZIP can never be read as
a CA/GB code. Only 3 literal code collisions exist (CA FSA vs GB outward: E1W, N1C,
N1P) and the region value disambiguates those.

Loading MUST happen while DuckDB external access is still enabled (the reader
touches files); `duckdb_validation.build_connection` calls `load_reference_tables`
BEFORE it locks the sandbox with `SET enable_external_access=false`. Every path is
a fixed code constant, never AI-influenced, so this widens no attack surface.
"""

import os
import re as _re

# The countries this module holds postal data for, and where each one's rows come
# from. US is NOT listed here: its rows are copied from the `uszips` table that
# `uszips_reference.load_reference_tables` already created, so there is exactly one
# US source of truth and the existing (already-compiled) US rules stay untouched.
SUPPORTED_COUNTRIES = ("US", "CA", "GB")

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Bundled Parquet cache (built from the CSVs below by build_parquet).
INTL_POSTAL_PARQUET = os.path.join(_DATA_DIR, "intl_postal.parquet")

# DuckDB table names the compiled rules reference. Kept as module constants so the
# compiler and the loader can never drift apart.
POSTAL_TABLE = "postal_reference"      # (key, state_alias)
POSTAL_STATE_TABLE = "postal_state"    # (key)
POSTAL_COUNTRY_TABLE = "postal_country"  # (alias, country)

# Source CSVs, and which of their columns carry a REGION alias for that country.
# The per-country column choice is dictated by what the data actually holds (see
# the module docstring): CA's `province` column is cities, so only GB reads it.
_SOURCES = (
    {"country": "CA", "csv": os.path.join(_DATA_DIR, "zipcodes.ca.csv"),
     "alias_cols": ("state_code", "state")},
    {"country": "GB", "csv": os.path.join(_DATA_DIR, "zipcodes.gb.csv"),
     "alias_cols": ("state_code", "state", "province")},
)

# Non-ISO country spellings that the ISO reference (country_currency_reference)
# cannot supply. 'UK' / 'GREAT BRITAIN' are the everyday names for GB and appear
# constantly in real BDX country columns; the constituent-country names
# (ENGLAND / SCOTLAND / …) are read from the GB CSV itself, not listed here.
_EXTRA_COUNTRY_ALIASES = {"UK": "GB", "GREAT BRITAIN": "GB"}


def _norm_code(value: str) -> str:
    """Postal code → the canonical reference form: alphanumerics only, UPPER.
    'T0A 1A0' → 'T0A1A0', 'sw1a 1aa' → 'SW1A1AA'."""
    return "".join(ch for ch in str(value or "") if ch.isalnum()).upper()


def _canonical_code(country: str, value: str) -> str:
    """The reference key for a raw source value, per country's code granularity.

    CA — the 3-char FSA (a couple of source rows carry a full postal code).
    GB — the outward code. A GB postcode is 5–7 alphanumerics and its INWARD code
         is always exactly 3 ('SW1A1AA' → 'SW1A'), while an outward code on its own
         is 2–4 — so the length cleanly decides which form the value is in, with no
         ambiguity. THE SAME expression is used by the compiler on the BDX cell.
    """
    a = _norm_code(value)
    if not a:
        return ""
    if country == "CA":
        return a[:3]
    if country == "GB":
        return a[:-3] if len(a) >= 5 else a
    return a


def build_parquet(dest: str = INTL_POSTAL_PARQUET) -> bool:
    """(Re)build the intl_postal Parquet from the bundled CSVs, applying the SAME
    normalization the compiled rule joins on: canonical code per country, UPPER
    region alias. Emits one row per (country, code, region alias) so a match is a
    single equality. Writes ATOMICALLY (temp file + os.replace) so a concurrent
    validation run never reads a half-written file. Returns True on success, False
    if a source is unavailable or the write fails. No values are hardcoded — every
    row comes from the source CSVs. Also usable as a deterministic build step (see
    scripts/build_reference_data.py)."""
    tmp = None
    try:
        import csv
        import duckdb
        import uuid
        rows, seen = [], set()
        for src in _SOURCES:
            path, country = src["csv"], src["country"]
            if not os.path.isfile(path):
                print(f"[intl_postal] source CSV missing: {path}")
                return False
            with open(path, newline="", encoding="utf-8-sig") as fh:
                for rec in csv.DictReader(fh):
                    code = _canonical_code(country, rec.get("zipcode"))
                    if not code:
                        continue
                    for col in src["alias_cols"]:
                        alias = str(rec.get(col) or "").strip().upper()
                        # A region CODE column must be alphabetic — this drops the
                        # single ONS identifier ('L93000001') sitting in the GB
                        # state_code column without naming that value anywhere.
                        if not alias or (col.endswith("_code") and not alias.isalpha()):
                            continue
                        key = (country, code, alias)
                        if key in seen:
                            continue
                        seen.add(key)
                        rows.append(key)
        if not rows:
            return False
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # Unique per CALL (pid + uuid), so two concurrent rebuilds — even two
        # threads in one process — never share a temp file; os.replace is atomic.
        tmp = f"{dest}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        con = duckdb.connect(":memory:")
        try:
            con.execute("CREATE TABLE _p(country VARCHAR, code VARCHAR, "
                        "state_alias VARCHAR)")
            con.executemany("INSERT INTO _p VALUES (?,?,?)", rows)
            con.execute(f"COPY _p TO '{tmp.replace(chr(39), chr(39) * 2)}' "
                        f"(FORMAT PARQUET)")
        finally:
            con.close()
        os.replace(tmp, dest)
        print(f"[intl_postal] rebuilt reference parquet ({len(rows)} rows)")
        return True
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[intl_postal] auto-rebuild failed ({exc}); "
              f"CA/GB postal checks will report not-validated.")
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def _country_alias_rows():
    """[(alias, country)] — every recognized spelling of a supported country.

    Sourced from the ISO reference already in the codebase (alpha-2, alpha-3, name,
    official name via `country_currency_reference.COUNTRY_CURRENCY`) plus the GB
    constituent-country names read out of the GB CSV's `state` column, plus the two
    everyday non-ISO names for GB. No country vocabulary is invented here."""
    out, seen = [], set()

    def add(alias, country):
        alias = str(alias or "").strip().upper()
        if alias and (alias, country) not in seen:
            seen.add((alias, country))
            out.append((alias, country))

    try:
        from contract_upload_services.country_currency_reference import COUNTRY_CURRENCY
    except Exception:                                   # pragma: no cover
        COUNTRY_CURRENCY = {}
    for a2 in SUPPORTED_COUNTRIES:
        entry = COUNTRY_CURRENCY.get(a2)
        add(a2, a2)
        if entry:
            alpha3, name, official = entry[0], entry[1], entry[2]
            for spelling in (alpha3, name, official):
                add(spelling, a2)
    for alias, country in _EXTRA_COUNTRY_ALIASES.items():
        add(alias, country)
    # GB's constituent countries, taken from the reference data itself.
    src = next((s for s in _SOURCES if s["country"] == "GB"), None)
    if src and os.path.isfile(src["csv"]):
        try:
            import csv
            with open(src["csv"], newline="", encoding="utf-8-sig") as fh:
                for rec in csv.DictReader(fh):
                    add(rec.get("state"), "GB")
        except Exception:                               # pragma: no cover
            pass
    return out


def _norm_alias(value) -> str:
    """A country spelling reduced to its comparable form: UPPER, punctuation to
    single spaces ('U.K.' → 'U K', 'Great  Britain' → 'GREAT BRITAIN'). Applied to
    BOTH sides of every lookup below so the two can never disagree."""
    return _re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


# Alias → alpha-2 maps, built once per process. `_postal_aliases` covers only the
# countries we hold POSTAL data for; `_iso_aliases` covers every ISO country, so a
# caller can tell "a country we cannot postal-check" (France) apart from "not a
# country at all" (Worldwide) — a distinction that decides whether a postal rule is
# narrowed, left row-dispatched, or not emitted.
_postal_aliases = None
_iso_aliases = None


def _index_alias(index: dict, alias, country, squash: bool = True) -> None:
    """Register one spelling, optionally also under its space-free form so an
    abbreviation written with punctuation ('U.K.' → 'U K' → 'UK') still resolves.
    First writer wins, so a real alias is never shadowed by a squashed collision.

    `squash` is off for the FULL ISO index: squashing there turns placeholder text
    into a country ('N/A' → 'NA' → Namibia). The handful of punctuated
    abbreviations that matter in practice are abbreviations of the countries we
    hold postal data for, and those are indexed with squashing on."""
    key = _norm_alias(alias)
    if not key:
        return
    index.setdefault(key, country)
    if squash:
        index.setdefault(key.replace(" ", ""), country)


def country_alias_map() -> dict:
    """{normalized alias: alpha-2} for the countries we hold postal data for."""
    global _postal_aliases
    if _postal_aliases is None:
        _postal_aliases = {}
        for alias, country in _country_alias_rows():
            _index_alias(_postal_aliases, alias, country)
    return _postal_aliases


def resolve_country_code(value):
    """Any spelling of a country → its ISO alpha-2, else None when the value names
    no country at all.

    Recognizes the postal aliases first (alpha-2/alpha-3/name/official name of the
    supported countries, plus GB's constituent countries and the everyday 'UK'),
    then falls back to the full ISO reference already in the codebase. Returns the
    code for an unsupported country too — `postal_country_code` is the caller that
    cares about the difference. No country vocabulary is invented here."""
    key = _norm_alias(value)
    if not key:
        return None
    postal = country_alias_map()
    hit = postal.get(key) or postal.get(key.replace(" ", ""))
    if hit:
        return hit
    global _iso_aliases
    if _iso_aliases is None:
        try:
            from contract_upload_services.country_currency_reference import (
                COUNTRY_CURRENCY)
        except Exception:                                   # pragma: no cover
            COUNTRY_CURRENCY = {}
        _iso_aliases = {}
        for a2, entry in COUNTRY_CURRENCY.items():
            alpha3, name, official = entry[0], entry[1], entry[2]
            for spelling in (a2, alpha3, name, official):
                _index_alias(_iso_aliases, spelling, a2, squash=False)
    return _iso_aliases.get(key)


def country_display_name(code) -> str:
    """Alpha-2 → the country's everyday name in title case ('GB' → 'United
    Kingdom'), for rule text a reviewer reads. Falls back to the code itself."""
    try:
        from contract_upload_services.country_currency_reference import (
            COUNTRY_CURRENCY)
    except Exception:                                       # pragma: no cover
        COUNTRY_CURRENCY = {}
    entry = COUNTRY_CURRENCY.get(str(code or "").strip().upper())
    return str(entry[1]).title() if entry and entry[1] else str(code or "")


def postal_country_code(value):
    """Any spelling of a country → its alpha-2 IF we hold postal reference data for
    it, else None. The generation-time counterpart of the `postal_country` lookup
    the compiled SQL does per row."""
    code = resolve_country_code(value)
    return code if code in SUPPORTED_COUNTRIES else None


def load_reference_tables(con, records_by_sheet, schema_cols) -> list:
    """Create `postal_reference`, `postal_state` and `postal_country` in `con`,
    when the loaded schema has a ZIP/postal column OR a STATE column.

    The gate is imported from `uszips_reference` — ONE definition of "this BDX has
    a postal/state column", so a generated rule is never left querying a table that
    was not created. Must run AFTER `uszips_reference.load_reference_tables` (the
    US slice is copied from the `uszips` table it creates) and BEFORE the sandbox
    lock.

    Idempotent and best-effort: any failure is swallowed and returns [] — a postal
    rule then simply reports "not validated" rather than crashing the whole run.
    Returns the list of reference table names created (for logging).
    """
    created = []
    try:
        from contract_upload_services.uszips_reference import (
            has_zip_column, has_state_column, USZIPS_TABLE, _sheet_column_names)
        if not (has_zip_column(records_by_sheet, schema_cols)
                or has_state_column(records_by_sheet, schema_cols)):
            return created
        _, sheets = _sheet_column_names(records_by_sheet, schema_cols)
        if {POSTAL_TABLE, POSTAL_STATE_TABLE, POSTAL_COUNTRY_TABLE} & sheets:
            # A real sheet already owns one of these names (pathological); don't
            # clobber it — and don't half-build, or a rule would query a table that
            # is missing its partner.
            return created

        if not os.path.exists(INTL_POSTAL_PARQUET):
            build_parquet()          # self-heal a deleted bundle (best-effort)
        if not os.path.exists(INTL_POSTAL_PARQUET):
            return created

        # read_parquet needs external access, which is still enabled here. The path
        # is a trusted code constant. all-VARCHAR keeps codes exact strings so a
        # zero-padded '00601' compares correctly against a BDX cell.
        path = INTL_POSTAL_PARQUET.replace("'", "''")
        con.execute(
            f'CREATE TABLE {POSTAL_TABLE} AS '
            f"SELECT CAST(country AS VARCHAR) || ':' || CAST(code AS VARCHAR) AS key, "
            f"CAST(state_alias AS VARCHAR) AS state_alias "
            f"FROM read_parquet('{path}')")
        created.append(POSTAL_TABLE)

        # The US slice comes from `uszips` — one source of truth for US data, and
        # the existing US-only rules keep using that table untouched. Absent (its
        # own load failed) just means US rows report not-validated, as before.
        us_loaded = con.execute(
            "SELECT COUNT(*) FROM duckdb_tables() WHERE lower(table_name) = ?",
            [USZIPS_TABLE.lower()]).fetchone()[0]
        if us_loaded:
            con.execute(
                f"INSERT INTO {POSTAL_TABLE} "
                f"SELECT 'US:' || u.zip, u.state_id FROM {USZIPS_TABLE} u "
                f"WHERE u.state_id IS NOT NULL AND u.state_id <> '' "
                f"UNION "
                f"SELECT 'US:' || u.zip, u.state_name FROM {USZIPS_TABLE} u "
                f"WHERE u.state_name IS NOT NULL AND u.state_name <> ''")

        # Region vocabulary per country, derived from the pairs above — so a region
        # added or renamed in the source data flows through with no code change.
        con.execute(
            f"CREATE TABLE {POSTAL_STATE_TABLE} AS SELECT DISTINCT "
            f"split_part(key, ':', 1) || ':' || state_alias AS key "
            f"FROM {POSTAL_TABLE}")
        created.append(POSTAL_STATE_TABLE)

        alias_rows = _country_alias_rows()
        con.execute(f"CREATE TABLE {POSTAL_COUNTRY_TABLE}"
                    f"(alias VARCHAR, country VARCHAR)")
        if alias_rows:
            con.executemany(
                f"INSERT INTO {POSTAL_COUNTRY_TABLE} VALUES (?,?)", alias_rows)
        created.append(POSTAL_COUNTRY_TABLE)

        n = con.execute(f"SELECT COUNT(*) FROM {POSTAL_TABLE}").fetchone()[0]
        print(f"[intl_postal] reference loaded ({n} code/region pairs across "
              f"{'US+' if us_loaded else ''}CA+GB, {len(alias_rows)} country aliases).")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[intl_postal] reference tables not loaded ({exc}); "
              f"CA/GB postal checks will report not-validated.")
    return created
