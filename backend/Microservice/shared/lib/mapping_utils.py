"""Pure, stateless mapping utilities (Excel IO, qualify, signatures, glom spec
application, header cache). Shared library - safe for any service to import."""
from __future__ import annotations

__all__ = [
    'Any',
    'CANONICAL_FIELDS',
    'DATA_MODEL',
    'JOIN_KEY_FIELDS',
    'MAPPABLE_FIELDS',
    'MAX_SAMPLES',
    'Path',
    'ROLE_PREFIXED_BASES',
    'SHEET_SEP',
    '_CACHE_CONFIDENCE_COL_FP',
    '_CACHE_CONFIDENCE_COL_ONLY',
    '_CACHE_CONFIDENCE_EXACT',
    '_CURRENCY_LIKE',
    '_DATE_COL_RE',
    '_EXPLICIT_DATE_RE',
    '_MEASURE_COL_RE',
    '_EXCEL_EPOCH',
    '_ISO_DATE',
    '_PLACEHOLDER_HINTS',
    '_UNNAMED_PAT',
    '_US_STATE',
    '_YYYYMMDD',
    '_YYYY_MM',
    '_apply_local',
    '_build_df',
    '_coerce_serial_date_columns',
    '_detect_header_row',
    '_dt',
    '_excel_serial_to_date',
    '_heuristic_pin',
    '_is_blank',
    '_lenient_json_loads',
    '_looks_like_template_row',
    '_norm',
    '_sample_kind',
    '_td',
    'apply_spec_multi',
    'cache_lookup',
    'cache_store',
    'glom',
    'group_spec_by_table',
    'io',
    'json',
    'log',
    'logging',
    'os',
    'pd',
    'qualify',
    're',
    'read_excel',
    'read_excel_all_sheets',
    'sample_fingerprint',
    'signature_multi',
    'time',
    'unqualify',
]

"""Mapping engine — LLM-driven with high-accuracy improvements.

Key changes versus v1:
  1. **Sample values are sent to the LLM.** Previously banned; in practice they
     are essential for disambiguating headers like LOB (text) vs LOB Code (code),
     PolicyType (NB/RB) vs policy_type, NonRenewStatus (R) → new_or_renewal, etc.
  2. **Per-sheet specs.** The spec is now `{sheet_name: {canonical: source}}`,
     letting join-key fields (policy_number, program_name) be mapped from every
     sheet that carries them instead of just one. apply_spec consumes this shape.
  3. **Role-prefixed party fields.** Canonical fields ending in `_INSURED` /
     `_AGENCY` / `_CARRIER` are recognized so the LLM can map both the insured's
     address AND the agency's address to party_address without collision.
  4. **policy_attributes catch-all.** Unmappable but clearly-attribute-shaped
     columns (UserDefined1, NJ Transaction Number, CustomerNumber, etc.) are
     auto-routed to policy_attributes with attribute_key = source column name.
  5. **Composite-source hints.** The prompt now tells the model that fields like
     legal_name can be composed from multiple source columns
     (first_name + ' ' + last_name) and to declare composition explicitly.
  6. **Pre-pass heuristics.** Obvious patterns (RecordType, RecordID, dates with
     YYYYMMDD format, state codes, currency-shaped values) get seeded before the
     LLM call; the LLM is told these are pinned so it doesn't second-guess them.
"""

import io

import json

import logging

import os

import re

import time

from typing import Any

import pandas as pd

from glom import Path, glom

from data_model import DATA_MODEL, group_spec_by_table

MAPPABLE_FIELDS = [
    k for k, v in DATA_MODEL.items() if v.get("source") in ("bdx", "fk_resolve")
]

CANONICAL_FIELDS = set(MAPPABLE_FIELDS)

JOIN_KEY_FIELDS = {
    "policy_number",
    "program_name",
    "tenant_name",
    "policy_umr",
}

ROLE_PREFIXED_BASES = {
    "party_address_address_line1",
    "party_address_address_line2",
    "party_address_city",
    "party_address_state_code",
    "party_address_zip_code",
    "party_address_country",
    "legal_name",
    "license_number",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger("bdx.mapper")

SHEET_SEP = " :: "

MAX_SAMPLES = 5

_CACHE_CONFIDENCE_EXACT = 0.99      # sheet + column + sample fingerprint

_CACHE_CONFIDENCE_COL_FP = 0.95     # column + sample fingerprint

_CACHE_CONFIDENCE_COL_ONLY = 0.85   # column only (majority vote)

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())

def sample_fingerprint(samples: list[str]) -> str:
    """A short stable hash of the first MAX_SAMPLES values. Lets us notice
    "same column shape" even on different files. Empty samples → empty fp
    so column-only fall-through still works."""
    import hashlib
    vals = [_norm(v) for v in (samples or [])[:MAX_SAMPLES]]
    vals = [v for v in vals if v]
    if not vals:
        return ""
    return hashlib.sha1("|".join(vals).encode("utf-8")).hexdigest()[:12]

def cache_lookup(session, sheet: str, column: str,
                 fp: str) -> tuple[str, float] | None:
    """Tiered lookup. Returns (canonical_field, confidence_0_to_1) or None."""
    # Import here so this module stays import-light for tests.
    from db import ColumnMappingCache
    sheet_n = _norm(sheet)
    column_n = _norm(column)

    # Tier 1 — exact sheet+col+fp. Highest signal.
    if fp:
        row = (session.query(ColumnMappingCache)
               .filter(ColumnMappingCache.sheet_norm == sheet_n,
                       ColumnMappingCache.column_norm == column_n,
                       ColumnMappingCache.sample_fingerprint == fp)
               .order_by(ColumnMappingCache.hit_count.desc(),
                         ColumnMappingCache.last_seen_at.desc())
               .first())
        if row:
            return row.canonical_field, _CACHE_CONFIDENCE_EXACT

        # Tier 2 — same column header & samples, sheet differs.
        row = (session.query(ColumnMappingCache)
               .filter(ColumnMappingCache.column_norm == column_n,
                       ColumnMappingCache.sample_fingerprint == fp)
               .order_by(ColumnMappingCache.hit_count.desc(),
                         ColumnMappingCache.last_seen_at.desc())
               .first())
        if row:
            return row.canonical_field, _CACHE_CONFIDENCE_COL_FP

    # Tier 3 — column name only. Majority vote: pick the canonical that's
    # been recorded most often for this header across the whole org.
    from sqlalchemy import func
    voted = (session.query(ColumnMappingCache.canonical_field,
                            func.sum(ColumnMappingCache.hit_count).label("votes"))
             .filter(ColumnMappingCache.column_norm == column_n)
             .group_by(ColumnMappingCache.canonical_field)
             .order_by(func.sum(ColumnMappingCache.hit_count).desc())
             .first())
    if voted:
        return voted[0], _CACHE_CONFIDENCE_COL_ONLY
    return None

def cache_store(session, sheet: str, column: str, fp: str,
                canonical: str, confidence: float, source: str = "llm") -> None:
    """Upsert one cache row. Same (sheet, column, fp, canonical) bumps hit_count."""
    from db import ColumnMappingCache
    sheet_n = _norm(sheet)
    column_n = _norm(column)
    row = (session.query(ColumnMappingCache)
           .filter(ColumnMappingCache.sheet_norm == sheet_n,
                   ColumnMappingCache.column_norm == column_n,
                   ColumnMappingCache.sample_fingerprint == fp,
                   ColumnMappingCache.canonical_field == canonical)
           .first())
    if row:
        row.hit_count = (row.hit_count or 1) + 1
        # User corrections outrank the LLM — keep the highest "source"
        # priority we've seen.
        if source == "user":
            row.source = "user"
        return
    session.add(ColumnMappingCache(
        sheet_norm=sheet_n,
        column_norm=column_n,
        sample_fingerprint=fp or None,
        canonical_field=canonical,
        confidence=int(round(max(0.0, min(1.0, confidence)) * 100)),
        source=source,
    ))

_UNNAMED_PAT = re.compile(r"^Unnamed:\s*\d+$")

def _detect_header_row(raw: pd.DataFrame, max_scan: int = 20) -> int:
    """Return the 0-based row index most likely to contain column headers.

    Primary pass: rows whose every non-empty cell is a text label (zero
    numeric values). Among those, pick the one with the most text cells.

    Fallback: if every scanned row contains at least one numeric value
    (e.g. a totals-only sheet), fall back to highest text-cell count
    regardless of numerics.
    """
    best_idx, best_score = 0, -1   # primary: text-only rows
    fall_idx, fall_score = 0, -1   # fallback: any row

    for i, row in raw.head(max_scan).iterrows():
        non_empty = [v for v in row if pd.notna(v) and str(v).strip()]
        text_count = sum(
            1 for v in non_empty
            if not str(v).strip().lstrip("-").replace(".", "", 1).isnumeric()
        )
        num_count = len(non_empty) - text_count

        if text_count > fall_score:
            fall_score, fall_idx = text_count, int(i)

        if num_count == 0 and text_count > best_score:
            best_score, best_idx = text_count, int(i)

    return best_idx if best_score > 0 else fall_idx

from datetime import datetime as _dt, timedelta as _td

_EXCEL_EPOCH = _dt(1899, 12, 30)

_DATE_COL_RE = re.compile(
    r"(?:^|[^a-z])(date|inception|expiry|expiration|effective|renewal|maturity)(?:[^a-z]|$)",
    re.I)

# An explicit "date" token (e.g. "Transaction Date", "Value Date") is an
# unambiguous date column. The other words above ("inception", "effective",
# "renewal", ...) are also used as ADJECTIVES modifying a non-date noun —
# "Inception Prem", "Renewal Premium", "Effective Rate" hold money, not dates.
_EXPLICIT_DATE_RE = re.compile(r"(?:^|[^a-z])date(?:[^a-z]|$)", re.I)

# Monetary / measure nouns. A column carrying one of these is an AMOUNT column,
# so a date-adjective in its name must not trigger serial→date coercion (which
# would rewrite a premium such as 75250 as the date 2106-01-09). Generic list —
# no per-tenant / per-contract values.
_MEASURE_COL_RE = re.compile(
    r"(?:^|[^a-z])(prem|premium|amount|amt|fee|fees|commission|comm|rate|tax|"
    r"taxes|levy|levies|cost|charge|charges|limit|deductible|value|sum|balance|"
    r"total|price|revenue|brokerage|surcharge|discount|tiv)(?:[^a-z]|$)",
    re.I)


def _is_date_named_col(name: str) -> bool:
    """True when the column NAME denotes a date column. An explicit "date" token
    always wins; otherwise a date-adjective word counts only when the column is
    not also a monetary/measure column."""
    if not _DATE_COL_RE.search(name):
        return False
    if _EXPLICIT_DATE_RE.search(name):
        return True
    return not _MEASURE_COL_RE.search(name)


def _excel_serial_to_date(v):
    """Excel day-serial → date/datetime, or None if v isn't a plausible serial."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not (20000 <= f <= 80000):   # ~1954 .. ~2119 — skips ids/amounts/bare years
        return None
    d = _EXCEL_EPOCH + _td(days=f)
    return d.date() if (d.hour, d.minute, d.second) == (0, 0, 0) else d

def _coerce_serial_date_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Convert Excel serial numbers to real dates in date-named columns. Leaves
    every other value (real dates, text, non-date numbers) untouched. Columns
    that merely pair a date-adjective with a money noun ("Inception Prem") are
    treated as amounts and left alone (see _is_date_named_col)."""
    for col in df.columns:
        if _is_date_named_col(str(col)):
            df[col] = df[col].map(lambda v: _excel_serial_to_date(v) or v)
    return df

def _build_df(raw: pd.DataFrame, hdr: int) -> pd.DataFrame:
    """Slice raw at `hdr`, assign clean deduplicated column names, drop blank columns.

    Blank / Unnamed header columns are dropped BEFORE deduplication so that
    spacer columns never leak into the mapping as '_1', '_2', etc.
    Real duplicate headers (e.g. ten 'Total Taxes and Levies' columns) still
    get '_1' … '_N' suffixes so each can be addressed individually.
    """
    raw_cols = [str(v).strip() if pd.notna(v) else "" for v in raw.iloc[hdr]]
    # Keep only columns whose header is non-empty and not an auto-Unnamed label.
    valid_idx = [i for i, c in enumerate(raw_cols)
                 if c and not _UNNAMED_PAT.match(c)]
    df = raw.iloc[hdr + 1:, valid_idx].copy()
    valid_cols = [raw_cols[i] for i in valid_idx]
    # Deduplicate real column names.
    seen: dict[str, int] = {}
    deduped: list[str] = []
    for c in valid_cols:
        if c in seen:
            seen[c] += 1
            deduped.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            deduped.append(c)
    df.columns = deduped
    return _coerce_serial_date_columns(df.reset_index(drop=True))

def read_excel(file_bytes: bytes, sheet_name=0, skip_rows: int = 0) -> pd.DataFrame:
    """Read a single-sheet payload. Supports XLSX/XLS, CSV and simple XML.

    Tries Excel first, falls back to CSV, then XML. Preserves caller's
    expectation of a DataFrame with header row parsed (header=0).
    """
    # Try Excel (xlsx / xls)
    try:
        df = pd.read_excel(
            io.BytesIO(file_bytes),
            sheet_name=sheet_name if sheet_name is not None else 0,
            skiprows=skip_rows or 0,
        )
        log.info("Excel parsed: %d rows × %d columns", len(df), len(df.columns))
        return df
    except Exception as exc_excel:
        log.debug("Not an Excel file: %s", exc_excel)

    # Try CSV (autodetect delimiter)
    try:
        text = file_bytes.decode("utf-8", errors="replace")
        df = pd.read_csv(io.StringIO(text), sep=None, engine="python",
                         header=0, skiprows=skip_rows or 0)
        log.info("CSV parsed: %d rows × %d columns", len(df), len(df.columns))
        return df
    except Exception as exc_csv:
        log.debug("Not a CSV file: %s", exc_csv)

    # Try XML — produce a flat table if possible
    try:
        df = pd.read_xml(io.BytesIO(file_bytes))
        log.info("XML parsed: %d rows × %d columns", len(df), len(df.columns))
        return df
    except Exception as exc_xml:
        log.debug("Not an XML file: %s", exc_xml)

    # If we reach here, nothing could parse the bytes
    raise ValueError("unsupported or corrupted workbook: not xlsx/csv/xml")

def read_excel_all_sheets(file_bytes: bytes, skip_rows: int = 0) -> dict[str, pd.DataFrame]:
    """Return a dict of sheet_name -> DataFrame for Excel/CSV/XML payloads.

    - Excel: auto-detects the real header row within the first 20 rows (or
      uses `skip_rows` when the caller provides an explicit value), then drops
      blank/spacer columns before building the DataFrame.
    - CSV: single sheet named 'Sheet1'; blank columns dropped.
    - XML: single sheet named 'Sheet1'.
    """
    # Try Excel first (multi-sheet).
    try:
        raw_sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, header=None)
        from exporter import hidden_sheet_names
        hidden = hidden_sheet_names(file_bytes)
        result: dict[str, pd.DataFrame] = {}
        for name, raw in raw_sheets.items():
            if name in hidden:          # skip deliberately-hidden tabs
                continue
            if raw.empty:
                continue
            # Honour explicit skip_rows; otherwise auto-detect within 20 rows.
            hdr = skip_rows if skip_rows else _detect_header_row(raw)
            if hdr >= len(raw):
                continue
            df = _build_df(raw, hdr)
            if len(df.columns):
                result[name] = df
                log.info("  sheet %r: header at row %d, %d rows × %d columns",
                         name, hdr, len(df), len(df.columns))
        if result:
            return result
    except Exception as exc_excel:
        log.debug("Not an Excel workbook (or multi-sheet parse failed): %s", exc_excel)

    # Not Excel — try CSV.
    try:
        text = file_bytes.decode("utf-8", errors="replace")
        df = pd.read_csv(io.StringIO(text), sep=None, engine="python", header=None,
                         skiprows=skip_rows or 0)
        if not df.empty:
            hdr = _detect_header_row(df) if not skip_rows else skip_rows
            df = _build_df(df, hdr)
        log.info("CSV parsed as single sheet: %d rows × %d columns", len(df), len(df.columns))
        return {"Sheet1": df}
    except Exception as exc_csv:
        log.debug("Not a CSV file: %s", exc_csv)

    # Try XML.
    try:
        df = pd.read_xml(io.BytesIO(file_bytes))
        log.info("XML parsed as single sheet: %d rows × %d columns", len(df), len(df.columns))
        return {"Sheet1": df}
    except Exception as exc_xml:
        log.debug("Not an XML file: %s", exc_xml)

    raise ValueError("unsupported or corrupted workbook: not xlsx/csv/xml")

def qualify(sheet: str, column: str) -> str:
    return f"{sheet}{SHEET_SEP}{column}"

def unqualify(qualified: str) -> tuple[str, str]:
    sheet, _, col = qualified.partition(SHEET_SEP)
    return sheet, col

def signature_multi(sheets: dict[str, pd.DataFrame]) -> list[str]:
    toks: list[str] = []
    for sheet, df in sheets.items():
        for c in df.columns:
            toks.append(f"{str(sheet).strip().lower()}{SHEET_SEP}{str(c).strip().lower()}")
    return sorted(toks)

_YYYYMMDD = re.compile(r"^\d{8}$")

_YYYY_MM = re.compile(r"^\d{6}$")

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")

_US_STATE = re.compile(r"^[A-Z]{2}$")

_CURRENCY_LIKE = re.compile(r"^-?\d{1,3}(?:[,]\d{3})*(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")

def _sample_kind(samples: list[str]) -> str:
    """Crude type classifier from a column's sample values."""
    cleaned = [s.strip() for s in samples if s and str(s).strip()]
    if not cleaned:
        return "empty"
    if all(_YYYYMMDD.match(s) for s in cleaned):
        return "date_yyyymmdd"
    if all(_YYYY_MM.match(s) for s in cleaned):
        return "yyyymm"
    if all(_ISO_DATE.match(s) for s in cleaned):
        return "iso_date"
    if all(_US_STATE.match(s) for s in cleaned):
        return "state_code"
    if all(_CURRENCY_LIKE.match(s.replace("$", "")) for s in cleaned):
        return "numeric"
    if all(s.upper() in ("Y", "N", "YES", "NO", "TRUE", "FALSE") for s in cleaned):
        return "boolean"
    if all(len(s) <= 4 and s.isupper() for s in cleaned):
        return "code"
    return "text"

def _heuristic_pin(header: str, samples: list[str]) -> str | None:
    """Return a canonical field if a header is mechanically obvious.

    Delegates the bulk of the work to the shared rule set in `exporter`
    (`_HEURISTIC_RULES`) so INPUT mapping and OUTPUT template mapping agree on
    the canonical key for common columns (policy/insured/carrier/dates/limits/
    premium/location). Only the column name is passed — never the sheet prefix —
    so a sheet called e.g. "Premium" can't pin every column as a premium field.
    """
    h = header.lower()
    kind = _sample_kind([str(s) for s in samples])
    # System discriminator columns are intentionally skipped.
    if h.endswith(":: recordtype") or h.endswith(":: recordid"):
        return None
    # AccountingYRMO format is yyyymm
    if "accountingyrmo" in h and kind == "yyyymm":
        return "premium_transaction_accounting_period"
    # Shared rich rules (column name only, restricted to mappable fields).
    col = header.split(SHEET_SEP)[-1]
    try:
        from exporter import _heuristic_pin as _rich_pin
        canon = _rich_pin(col, [str(s) for s in samples])
    except Exception:
        canon = None
    if canon and canon in CANONICAL_FIELDS:
        return canon
    return None

def _lenient_json_loads(text: str) -> dict | None:
    """Parse a Gemini response that *should* be JSON but might be wrapped in
    stray prose, code fences, an unterminated tail, or have a stray token in
    the middle. Uses `json-repair` as the primary recovery path so a single
    bad comma/brace doesn't force a chunked retry.
    Returns None only if nothing parseable can be recovered.
    """
    if not text:
        return None
    text = text.strip()
    # Strip ```json … ``` fencing.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    # Locate the JSON object: from the first '{' through the matching '}'.
    start = text.find("{")
    if start < 0:
        return None
    text = text[start:]

    # Try once as-is.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # json-repair handles truncation, missing commas, stray quotes,
    # unbalanced braces, etc. Vast majority of real-world Gemini quirks
    # are recovered here in one shot.
    try:
        from json_repair import repair_json
        repaired = repair_json(text, return_objects=True)
        if isinstance(repaired, dict) and repaired:
            return repaired
    except Exception:
        pass

    # Walk forward and find every prefix that's a complete top-level object
    # by counting braces while respecting string quoting / escaping. We keep
    # the longest balanced prefix and try that.
    depth = 0
    in_str = False
    escape = False
    last_balanced_end = -1
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                last_balanced_end = i
    if last_balanced_end > 0:
        try:
            return json.loads(text[: last_balanced_end + 1])
        except json.JSONDecodeError:
            pass

    # As a last resort: take the unterminated tail and close it manually by
    # popping any trailing partial entry and balancing the braces. This keeps
    # the entries we DO have when Gemini just stops mid-stream.
    trimmed = text
    # Cut after the last `]` that follows a key/value (i.e. a fully-closed
    # source-column entry).
    cut = trimmed.rfind("]")
    if cut > 0:
        trimmed = trimmed[: cut + 1]
        # Strip trailing comma if any, then close the outer object.
        trimmed = trimmed.rstrip().rstrip(",")
        # Count net unclosed braces and pad them.
        depth = 0
        in_str = False
        escape = False
        for ch in trimmed:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        if depth > 0:
            trimmed = trimmed + ("}" * depth)
        try:
            return json.loads(trimmed)
        except json.JSONDecodeError:
            return None
    return None

def apply_spec_multi(
    sheets: dict[str, pd.DataFrame],
    spec_by_sheet: dict[str, dict[str, Any]],
) -> dict[str, list[dict]]:
    """Apply per-sheet spec to each sheet's rows, producing DB-shaped records.

    Returns {sheet_name: [{table_name: {db_column: value, ...}, ...}, ...]}.
    """
    results: dict[str, list[dict]] = {}
    for sheet_name, df in sheets.items():
        sheet_spec = spec_by_sheet.get(sheet_name, {})
        if not sheet_spec:
            results[sheet_name] = []
            continue
        # Strip the "Sheet :: " prefix from spec values since the per-sheet df
        # has bare column names.
        local_spec: dict[str, str] = {}
        for canonical, src in sheet_spec.items():
            srcs = src if isinstance(src, list) else [src]
            local_cols = []
            for s in srcs:
                if SHEET_SEP in s:
                    sh, col = s.split(SHEET_SEP, 1)
                    if sh != sheet_name:
                        continue
                    local_cols.append(col)
                else:
                    local_cols.append(s)
            if not local_cols:
                continue
            local_spec[canonical] = local_cols[0] if len(local_cols) == 1 else local_cols
        results[sheet_name] = _apply_local(df, local_spec)
    return results

def _is_blank(v: Any) -> bool:
    """True for None, NaN, empty string, and whitespace-only strings."""
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(v, str) and not v.strip():
        return True
    return False

def _apply_local(df: pd.DataFrame, spec: dict[str, Any]) -> list[dict]:
    """Apply a sheet-local spec to a DataFrame. Handles composite sources.

    Cleanup rules so the warehouse stays compact:
      - Coerce NaN → None, dates → ISO strings.
      - Drop columns whose value is blank (None / NaN / empty / whitespace).
      - Drop table dicts that become empty after that.
      - Drop rows that produce NO usable values (template / blank rows).

    Extra-field passthrough: entries whose canonical key starts with `_xf:`
    are collected into a separate `extras` dict on the record. The ingester
    decides which entity table that dict belongs on (policy / claim / …).
    """
    # Split out the _xf:* entries first so the rest of the pipeline doesn't
    # have to worry about non-canonical keys. xf_spec is a list of
    # (entity, key, source) tuples — entity defaults to 'policy'.
    from extras import is_extras_key, parse_xf_key
    xf_spec: list[tuple[str, str, Any]] = []
    rest: dict = {}
    for k, v in spec.items():
        if is_extras_key(k):
            ent, key = parse_xf_key(k)
            xf_spec.append((ent, key, v))
        else:
            rest[k] = v

    by_table = group_spec_by_table({k: v for k, v in rest.items()
                                    if not isinstance(v, list)})
    composites = {k: v for k, v in rest.items() if isinstance(v, list)}

    records = df.to_dict(orient="records")
    out: list[dict] = []
    for row in records:
        record: dict[str, dict] = {}
        for table, col_to_src in by_table.items():
            present = {col: src for col, src in col_to_src.items() if src in row}
            if not present:
                continue
            glom_spec = {col: Path(src) for col, src in present.items()}
            mapped = glom(row, glom_spec)
            cleaned: dict[str, Any] = {}
            for k, v in mapped.items():
                if _is_blank(v):
                    continue
                if hasattr(v, "isoformat"):
                    v = v.isoformat()
                elif isinstance(v, float) and v.is_integer():
                    # pandas reads numeric-looking columns (zip_code, license_number)
                    # as floats — collapse 95060.0 → "95060" so they don't drift
                    # apart from the same value coming through as text from another sheet.
                    v = str(int(v))
                cleaned[k] = v
            if cleaned:
                record[table] = cleaned
        # Composites: join non-empty values with a space
        for canonical, srcs in composites.items():
            values = [str(row.get(s, "")).strip() for s in srcs
                      if not _is_blank(row.get(s))]
            if values:
                model_entry = DATA_MODEL.get(canonical)
                if model_entry:
                    record.setdefault(model_entry["table"], {})[model_entry["column"]] = \
                        " ".join(values).strip()

        # Extra fields (_xf:*) — grouped by target entity so the ingester
        # can write each group to its entity's `extras` JSONB column.
        # Final shape on the record:
        #   record["extras"] = {
        #       "policy": {"tria_premium": 12500.00, …},
        #       "claim":  {"tpa_ref": "RC-2026", …},
        #   }
        if xf_spec:
            extras_by_entity: dict[str, dict[str, Any]] = {}
            for entity, key, src in xf_spec:
                srcs = src if isinstance(src, list) else [src]
                vals = [row.get(s) for s in srcs if not _is_blank(row.get(s))]
                if not vals:
                    continue
                if len(vals) == 1:
                    v = vals[0]
                    if hasattr(v, "isoformat"):
                        v = v.isoformat()
                    elif isinstance(v, float) and v.is_integer():
                        v = str(int(v))
                else:
                    v = " ".join(str(x).strip() for x in vals)
                extras_by_entity.setdefault(entity, {})[key] = v
            if extras_by_entity:
                record["extras"] = extras_by_entity

        if not record:
            continue  # row produced zero real values
        if _looks_like_template_row(record):
            continue  # spreadsheet description / instructions row
        out.append(record)
    return out

_PLACEHOLDER_HINTS = (
    "see ", "always", "enter ", "your assigned", "to be listed",
    "submits the surplus", "your ", "license number of",
    "address information", "always include", "please use this",
    "we will", "we ", "to you",
)

def _looks_like_template_row(record: dict[str, dict]) -> bool:
    """Detect xlsx description/instructions rows masquerading as data.

    A real bordereau row MUST have at least one of:
      - policy.policy_number containing a digit
      - policy.policy_effective_date containing a digit
      - claim.claim_number containing a digit
      - any numeric (int/float) value anywhere in the record
    Anything else is treated as a footer/template/junk row.
    """
    pol = record.get("policy") or {}
    clm = record.get("claim") or {}

    def has_digit(v: Any) -> bool:
        return isinstance(v, str) and any(ch.isdigit() for ch in v)

    if has_digit(pol.get("policy_number")) or has_digit(pol.get("policy_effective_date")):
        return False
    if has_digit(clm.get("claim_number")):
        return False
    # any int/float anywhere is a strong signal of real data
    for table in record.values():
        for v in table.values():
            if isinstance(v, (int, float)) and not (isinstance(v, bool)):
                return False
    # No digit-bearing identifier anywhere → junk
    return True
