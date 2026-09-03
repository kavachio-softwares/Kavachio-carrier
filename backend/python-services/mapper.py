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
     address AND the agency's address land on role-specific tables
     (policyholder_* / ingested_party_*) without collision.
  4. **policy_attributes catch-all.** Unmappable but clearly-attribute-shaped
     columns (UserDefined1, NJ Transaction Number, CustomerNumber, etc.) are
     auto-routed to the entity's extras JSONB via the `_xf:` prefix.
  5. **Composite-source hints.** The prompt now tells the model that fields like
     legal_name can be composed from multiple source columns
     (first_name + ' ' + last_name) and to declare composition explicitly.
  6. **Pre-pass heuristics.** Obvious patterns (RecordType, RecordID, dates with
     YYYYMMDD format, state codes, currency-shaped values) get seeded before the
     LLM call; the LLM is told these are pinned so it doesn't second-guess them.
"""
from __future__ import annotations

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

# Restrict mapping candidates to fields BDX files can actually carry.
MAPPABLE_FIELDS = [
    k for k, v in DATA_MODEL.items() if v.get("source") in ("bdx", "fk_resolve")
]
CANONICAL_FIELDS = set(MAPPABLE_FIELDS)

# Fields that legitimately appear in multiple sheets as join keys.
# Spec keeps them per-sheet so each sheet's apply_spec finds its own column.
JOIN_KEY_FIELDS = {
    "policy_number",
    "program_name",
    "tenant_legal_name",
    "policy_umr",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bdx.mapper")

SHEET_SEP = " :: "
SUCCESS_THRESHOLD = 0.65
LIKELY_THRESHOLD = 0.45
MAX_SAMPLES = 5

# Cache lookup quality. Higher = stricter match → higher confidence boost.
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
    """Tiered lookup. Returns (canonical_field, confidence_0_to_1) or None.

    Cached rows written before the v4 model migration may still carry a v2
    canonical field name; those resolve through LEGACY_FIELD_MAP (fields the
    v4 model removed return None and the cache tier is skipped)."""
    # Import here so this module stays import-light for tests.
    from db import ColumnMappingCache
    from data_model import resolve_field
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
            cf = resolve_field(row.canonical_field)
            if cf:
                return cf, _CACHE_CONFIDENCE_EXACT

        # Tier 2 — same column header & samples, sheet differs.
        row = (session.query(ColumnMappingCache)
               .filter(ColumnMappingCache.column_norm == column_n,
                       ColumnMappingCache.sample_fingerprint == fp)
               .order_by(ColumnMappingCache.hit_count.desc(),
                         ColumnMappingCache.last_seen_at.desc())
               .first())
        if row:
            cf = resolve_field(row.canonical_field)
            if cf:
                return cf, _CACHE_CONFIDENCE_COL_FP

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
        cf = resolve_field(voted[0])
        if cf:
            return cf, _CACHE_CONFIDENCE_COL_ONLY
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


# ---- I/O -------------------------------------------------------------------

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


# Excel stores dates as day-serials (1899-12-30 epoch). A date cell whose number
# format openpyxl doesn't recognise comes through as the raw serial (e.g. 45931 →
# 2025-10-01). For columns whose NAME clearly denotes a date we convert those
# serials back to real dates so they don't surface as integers in the output BDX
# or silently break date validation (TRY_CAST AS DATE).
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


_XML_ATTR_TOKEN = re.compile(r'(?P<pre>[\s<])(?P<name>[^\s=<>/"\']+)(?P<eq>\s*=\s*)(?P<q>["\'])')
_XML_VALID_ENTITY = re.compile(r'&(?:amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);')


def _sanitize_xml_bytes(file_bytes: bytes) -> bytes:
    """Best-effort repair of "almost XML" defects seen in real BDX exports so
    they can still be parsed, instead of failing well-formedness outright:

      1. A bare '&' that isn't part of a valid entity/char reference (e.g. an
         insured name like "Marsh & McLennan" written without escaping).
      2. An attribute name that isn't a valid XML Name — most commonly a raw
         column header used verbatim as an XML attribute, like `1st Notice
         Date="..."` (Names cannot start with a digit) — prefixed with '_'.

    Only tried as a fallback AFTER a strict parse already failed; never
    changes bytes that were already well-formed.
    """
    text = file_bytes.decode("utf-8", errors="replace")
    text = re.sub(r'&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);)', '&amp;', text)

    def _fix_name(m: re.Match) -> str:
        name = m.group("name")
        if re.match(r'^[A-Za-z_:]', name):
            return m.group(0)
        return f'{m.group("pre")}_{name}{m.group("eq")}{m.group("q")}'

    text = _XML_ATTR_TOKEN.sub(_fix_name, text)
    return text.encode("utf-8")


def _read_xml_lenient(file_bytes: bytes) -> pd.DataFrame:
    """pd.read_xml(), retrying once against _sanitize_xml_bytes() if the raw
    bytes aren't well-formed XML. Raises whatever the first attempt raised
    when the sanitized retry also fails, so callers see the original cause."""
    try:
        return pd.read_xml(io.BytesIO(file_bytes))
    except Exception as exc_strict:
        try:
            return pd.read_xml(io.BytesIO(_sanitize_xml_bytes(file_bytes)))
        except Exception:
            raise exc_strict


def parse_workbook_xml_sheets(file_bytes: bytes) -> dict[str, pd.DataFrame] | None:
    """Parse the app's own XML export shape (see output_serializers._to_xml):

        <workbook><sheet name="..."><row><cell name="Col">value</cell>...
        </row>...</sheet>...</workbook>

    pandas.read_xml()'s default shallow xpath treats <sheet> itself as the
    record and never descends into <row>/<cell>, so a workbook this app
    exported as XML could never be read back in (garbage single-row frame,
    or a parse error on realistic data) — parse it directly instead. Returns
    None (never raises) when the root isn't <workbook>, so callers fall back
    to a generic pd.read_xml() for arbitrary customer XML.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(file_bytes)
    except ET.ParseError:
        try:
            root = ET.fromstring(_sanitize_xml_bytes(file_bytes))
        except ET.ParseError:
            return None
    if root.tag != "workbook":
        return None
    result: dict[str, pd.DataFrame] = {}
    for i, sheet_el in enumerate(root.findall("sheet")):
        name = sheet_el.get("name") or f"Sheet{i + 1}"
        records = [
            {(c.get("name") or ""): (c.text or "") for c in row_el.findall("cell")}
            for row_el in sheet_el.findall("row")
        ]
        if records:
            result[name] = pd.DataFrame(records)
    return result or None


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
        log.warning("Not an Excel file: %s", exc_excel)

    # Try JSON BEFORE CSV — a records list, or an object whose (first) value is
    # such a list (e.g. {"Sheet1": [ {...}, ... ]}). Tried first because the
    # permissive CSV parser below would otherwise mangle JSON text into garbage
    # columns. Non-JSON bytes raise in json.loads and fall through to CSV.
    try:
        df = _json_to_single_df(file_bytes)
        if df is not None:
            log.info("JSON parsed: %d rows × %d columns", len(df), len(df.columns))
            return df
    except Exception as exc_json:
        log.warning("Not a JSON file: %s", exc_json)

    # Try CSV (autodetect delimiter)
    try:
        text = file_bytes.decode("utf-8", errors="replace")
        df = pd.read_csv(io.StringIO(text), sep=None, engine="python",
                         header=0, skiprows=skip_rows or 0)
        log.info("CSV parsed: %d rows × %d columns", len(df), len(df.columns))
        return df
    except Exception as exc_csv:
        log.warning("Not a CSV file: %s", exc_csv)

    # Try XML — our own <workbook>/<sheet>/<row>/<cell> shape first, then a
    # generic flat pd.read_xml() for arbitrary customer XML.
    own_xml = parse_workbook_xml_sheets(file_bytes)
    if own_xml:
        names = list(own_xml.keys())
        if isinstance(sheet_name, str) and sheet_name in own_xml:
            df = own_xml[sheet_name]
        elif isinstance(sheet_name, int) and 0 <= sheet_name < len(names):
            df = own_xml[names[sheet_name]]
        else:
            df = own_xml[names[0]]
        log.info("XML (workbook) parsed: %d rows × %d columns", len(df), len(df.columns))
        return df
    try:
        df = _read_xml_lenient(file_bytes)
        log.info("XML parsed: %d rows × %d columns", len(df), len(df.columns))
        return df
    except Exception as exc_xml:
        log.warning("Not an XML file: %s", exc_xml)

    # If we reach here, nothing could parse the bytes
    raise ValueError("unsupported or corrupted workbook: not xlsx/csv/xml/json")


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
        log.warning("Not an Excel workbook (or multi-sheet parse failed): %s", exc_excel)

    # Not Excel — try JSON BEFORE CSV (the permissive CSV parser below would
    # otherwise turn JSON text into garbage columns). Supports either a flat list
    # of records (→ one "Sheet1") or an object mapping sheet names to lists of
    # records (→ one sheet per key), so a multi-sheet BDX can round-trip as JSON.
    # Non-JSON bytes raise in json.loads and fall through to CSV.
    try:
        sheets = _json_to_sheets(file_bytes)
        if sheets:
            for name, df in sheets.items():
                log.info("JSON sheet %r: %d rows × %d columns", name, len(df), len(df.columns))
            return sheets
    except Exception as exc_json:
        log.warning("Not a JSON file: %s", exc_json)

    # Not Excel/JSON — try CSV.
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
        log.warning("Not a CSV file: %s", exc_csv)

    # Try XML — our own <workbook>/<sheet>/<row>/<cell> shape first (this is
    # multi-sheet aware, unlike the generic fallback below), then a generic
    # flat pd.read_xml() for arbitrary customer XML.
    own_xml = parse_workbook_xml_sheets(file_bytes)
    if own_xml:
        for name, df in own_xml.items():
            log.info("XML (workbook) sheet %r: %d rows × %d columns", name, len(df), len(df.columns))
        return own_xml
    try:
        df = _read_xml_lenient(file_bytes)
        log.info("XML parsed as single sheet: %d rows × %d columns", len(df), len(df.columns))
        return {"Sheet1": df}
    except Exception as exc_xml:
        log.warning("Not an XML file: %s", exc_xml)

    raise ValueError("unsupported or corrupted workbook: not xlsx/csv/xml/json")


def _json_records_to_df(records: list) -> pd.DataFrame:
    """Build a DataFrame from a list of dict records (order-preserving columns)."""
    df = pd.DataFrame(records)
    return df.reset_index(drop=True)


def _json_to_single_df(file_bytes: bytes) -> pd.DataFrame | None:
    """Parse JSON bytes into a single flat DataFrame, or None if the shape isn't
    a records list (or an object whose first value is one)."""
    import json
    obj = json.loads(file_bytes.decode("utf-8", errors="replace"))
    if isinstance(obj, list):
        return _json_records_to_df(obj)
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, list):
                return _json_records_to_df(v)
        # A single record object → one-row table.
        return _json_records_to_df([obj])
    return None


def _json_to_sheets(file_bytes: bytes) -> dict[str, pd.DataFrame]:
    """Parse JSON bytes into {sheet_name -> DataFrame}.

    - top-level list of records         → {"Sheet1": df}
    - top-level {name: [records], ...}   → one sheet per list-valued key
    - top-level single record object     → {"Sheet1": df} (one row)
    """
    import json
    obj = json.loads(file_bytes.decode("utf-8", errors="replace"))
    if isinstance(obj, list):
        return {"Sheet1": _json_records_to_df(obj)}
    if isinstance(obj, dict):
        sheet_lists = {k: v for k, v in obj.items() if isinstance(v, list)}
        if sheet_lists:
            return {str(k): _json_records_to_df(v) for k, v in sheet_lists.items()}
        return {"Sheet1": _json_records_to_df([obj])}
    return {}


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


# ---- Heuristic pre-pass ----------------------------------------------------
# Detect obvious patterns before we ever call the LLM. Pin them in the prompt
# so the LLM doesn't waste tokens (or mistakes) re-deciding them.

_YYYYMMDD = re.compile(r"^\d{8}$")
_YYYY_MM = re.compile(r"^\d{6}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_US_STATE = re.compile(r"^[A-Z]{2}$")
# Includes a plain-integer alternative so un-grouped amounts (e.g. 1000000)
# classify as numeric instead of text.
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
    # AccountingYRMO format is yyyymm → the transaction's accounting date
    if "accountingyrmo" in h and kind == "yyyymm":
        return "premium_transaction_accounting_date"
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


# ---- LLM mapping -----------------------------------------------------------

# ---- top-N candidate scoring (UI ranked picker) ---------------------------

TOP_N_CANDIDATES = 10


def _build_candidates_prompt(
    headers: list[str],
    samples: dict[str, list[str]],
    pins: dict[str, str],
    mappable_model: dict,
) -> str:
    """Single unified prompt: per source column, return TOP_N_CANDIDATES
    ranked canonical fields with confidence + reason. Also carries the
    role-prefix / composite / attribute / pin hints that used to live on
    the per-canonical prompt — so the spec we derive from these candidates
    is as accurate as the previous two-call pipeline."""
    role_hint = (
        "\nROLE-SPECIFIC FIELDS: every column in the model names its own\n"
        "subject, so pick the field whose prefix matches WHO the column is\n"
        "about:\n"
        "  - Insured*/Policyholder* headers → policyholder_* fields\n"
        "    (policyholder_legal_name, policyholder_address_line1, …).\n"
        "  - Agency*/Broker*/Carrier*/Company* headers naming an organisation\n"
        "    read off the file → ingested_party_* fields\n"
        "    (ingested_party_legal_name, ingested_party_type, …).\n"
        "  - Location/premises columns → risk_location_* fields.\n"
        "Never silently drop agency/broker columns.\n"
    )
    composite_hint = (
        "\nCOMPOSITE FIELDS: policyholder_legal_name is often split across\n"
        "FirstName + LastName columns. List the canonical field as a\n"
        "HIGH-confidence candidate on BOTH source columns; the apply layer\n"
        "combines them.\n"
    )
    attributes_hint = (
        "\nNEVER invent canonical field keys. If no field in the provided\n"
        "data model is a reasonable match for a source column, return ONLY\n"
        "low-confidence (<0.30) candidates drawn from the existing model —\n"
        "do not fabricate keys such as `attribute_value_*`, `extra_*`, or\n"
        "anything else not in the canonical list.\n"
    )
    join_hint = (
        "\nJOIN KEYS (policy_number, program_name, tenant_legal_name,\n"
        "policy_umr): if the same column meaning appears in\n"
        "multiple sheets (POL/UNT/PRM), pick the same canonical key as the\n"
        "top candidate in each sheet — do NOT suffix.\n"
    )
    pins_hint = ""
    if pins:
        pins_hint = (
            "\nPINNED MAPPINGS (heuristic — must be the TOP candidate with\n"
            "confidence 1.0 for the listed source column):\n"
            + json.dumps({src: canon for canon, src in pins.items()}, indent=2)
            + "\n"
        )

    sample_block = json.dumps(
        {h: samples.get(h, [])[:MAX_SAMPLES] for h in headers},
        indent=2, default=str,
    )
    return (
        "You map insurance bordereaux (BDX) Excel COLUMN HEADERS to a\n"
        "canonical data model. Headers are qualified 'SheetName :: ColumnHeader'.\n"
        f"\nFor EACH source column, return the TOP {TOP_N_CANDIDATES} canonical\n"
        "fields ranked by goodness of fit. Use BOTH the header name and the\n"
        "sample values to decide.\n"
        "\nConfidence scale (0-1): >=0.85 unambiguous; 0.65-0.85 strong;\n"
        "0.45-0.65 likely; 0.20-0.45 weak; 0.0 no fit. Reserve high\n"
        f"confidence carefully. Always return exactly {TOP_N_CANDIDATES}\n"
        "items per column — pad with weak candidates if needed.\n"
        + role_hint + composite_hint + attributes_hint + join_hint + pins_hint
        + "\nUse canonical field KEYS exactly as they appear in the data model.\n"
        "Output MUST be COMPACT JSON — NO explanations, NO reason fields,\n"
        "NO whitespace between tokens. Two keys only per candidate object:\n"
        '`c` (canonical field key) and `s` (confidence score 0-1).\n'
        "\nReturn EXACTLY this shape, nothing else:\n"
        '  { "Sheet :: Column": [\n'
        '      {"c":"policy_number","s":0.95},\n'
        '      {"c":"policy_umr","s":0.42},\n'
        "      …\n"
        "    ],\n"
        "    … (one entry per source column) }\n\n"
        f"Canonical data model (BDX-mappable fields):\n{json.dumps(mappable_model)}\n\n"
        f"Source columns with sample values:\n{sample_block}\n"
    )


_MAX_OUTPUT_TOKENS = 65536  # Flash 2.5 ceiling
_FALLBACK_BATCH = 30        # only used if the single call truncates


def _is_acceptable_canonical(cf: str) -> bool:
    """Whitelist: canonical keys that actually exist in the data model. The
    attribute_value_* / extra_* catch-all family is INTENTIONALLY rejected —
    if the LLM can't find a real model field, the column stays unmapped for
    the user to handle.

    Legacy role-suffixed keys (`legal_name_insured`, …) from cached or stored
    mappings resolve through the v2→v4 LEGACY_FIELD_MAP after the suffix is
    stripped, so an old spec degrades to its base field instead of vanishing.
    """
    if cf in CANONICAL_FIELDS:
        return True
    from data_model import resolve_field
    base = re.sub(r"_(insured|agency|carrier)$", "", cf)
    return resolve_field(base) in CANONICAL_FIELDS


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


def _parse_candidates_response(
    raw: dict, valid_headers: set[str],
) -> dict[str, list[dict]]:
    """Accept the compact `{c, s}` shape AND the verbose
    `{canonical, confidence, reason}` shape for forward/backward compat."""
    out: dict[str, list[dict]] = {}
    for src, lst in (raw or {}).items():
        if src not in valid_headers or not isinstance(lst, list):
            continue
        cleaned: list[dict] = []
        seen: set[str] = set()
        for item in lst:
            if not isinstance(item, dict):
                continue
            cf = item.get("c") or item.get("canonical")
            if not isinstance(cf, str) or not _is_acceptable_canonical(cf):
                continue
            if cf in seen:
                continue
            seen.add(cf)
            raw_conf = item.get("s") if "s" in item else item.get("confidence", 0.0)
            try:
                conf = float(raw_conf or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            cleaned.append({
                "canonical": cf,
                "confidence": max(0.0, min(1.0, conf)),
                "reason": (item.get("reason") or "")[:140],
            })
            if len(cleaned) >= TOP_N_CANDIDATES:
                break
        if cleaned:
            cleaned.sort(key=lambda c: c["confidence"], reverse=True)
            out[src] = cleaned
    return out


def _gemini_candidates_call(
    client, headers: list[str], samples: dict[str, list[str]],
    pins: dict[str, str], mappable_model: dict,
) -> dict[str, list[dict]] | None:
    """One Gemini round trip. Returns parsed candidates dict, or None on
    failure / truncation (caller may retry in smaller chunks)."""
    prompt = _build_candidates_prompt(headers, samples, pins, mappable_model)
    log.info("Calling Gemini for top-%d candidates over %d headers (prompt=%d chars)…",
             TOP_N_CANDIDATES, len(headers), len(prompt))
    t0 = time.time()
    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "max_output_tokens": _MAX_OUTPUT_TOKENS,
            },
        )
    except Exception as e:
        log.error("Gemini candidates call failed: %s", e)
        return None

    # Diagnostics: finish_reason tells us if Gemini stopped early (MAX_TOKENS,
    # SAFETY, RECITATION, OTHER…) so a truncated JSON can be distinguished
    # from a model bug. usage_metadata gives token accounting.
    finish_reason: Any = "?"
    safety_ratings: Any = None
    cands = getattr(resp, "candidates", None) or []
    if cands:
        finish_reason = getattr(cands[0], "finish_reason", "?")
        safety_ratings = getattr(cands[0], "safety_ratings", None)
    usage = getattr(resp, "usage_metadata", None)

    text = (resp.text or "").strip()
    log.info(
        "Gemini responded in %.2fs (%d chars) finish=%s usage=%s",
        time.time() - t0, len(text), finish_reason, usage,
    )

    raw = _lenient_json_loads(text)
    if raw is None:
        # Persist the raw payload so we can post-mortem WHY recovery failed.
        try:
            import tempfile
            fd, dump = tempfile.mkstemp(
                prefix="mapper_unparseable_", suffix=".json", dir="/tmp",
            )
            with os.fdopen(fd, "w") as f:
                f.write(text)
            log.error(
                "Could not recover any JSON. finish=%s len=%d. Dumped raw "
                "response to %s. First 200 chars: %r",
                finish_reason, len(text), dump, text[:200],
            )
        except Exception:
            log.error(
                "Could not recover any JSON. finish=%s len=%d. First 200 "
                "chars: %r", finish_reason, len(text), text[:200],
            )
        if safety_ratings:
            log.error("Safety ratings on the failed response: %s", safety_ratings)
        return None

    parsed = _parse_candidates_response(raw, set(headers))
    log.info("Parsed %d/%d source columns with at least one candidate",
             len(parsed), len(headers))
    return parsed


def _call_llm_candidates(
    headers: list[str],
    samples: dict[str, list[str]],
    pins: dict[str, str] | None = None,
) -> dict[str, list[dict]]:
    """Ask Gemini for ranked top-N candidates per source column.

    Strategy: ONE call covers all source columns. Only if that response
    truncates or fails to parse do we retry in chunks of `_FALLBACK_BATCH`.
    Returns `{ "Sheet :: Column": [{canonical, confidence, reason}, ...] }`.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or not headers:
        if not api_key:
            log.warning("No GEMINI_API_KEY — returning empty candidates.")
        return {}

    from google import genai
    client = genai.Client(api_key=api_key)
    mappable_model = {k: DATA_MODEL[k] for k in MAPPABLE_FIELDS}

    parsed = _gemini_candidates_call(
        client, headers, samples, pins or {}, mappable_model,
    )
    if parsed is not None:
        log.info("Got candidates for %d/%d source columns (single call)",
                 len(parsed), len(headers))
        return parsed

    # Fallback: chunk only when the single call hits a problem (truncation,
    # parse failure, transient error). This is intentionally rare.
    log.warning("Single call failed/truncated — falling back to chunked calls of %d.",
                _FALLBACK_BATCH)
    out: dict[str, list[dict]] = {}
    for i in range(0, len(headers), _FALLBACK_BATCH):
        chunk = headers[i : i + _FALLBACK_BATCH]
        chunk_samples = {h: samples.get(h, []) for h in chunk}
        chunk_pins = {c: s for c, s in (pins or {}).items() if s in chunk}
        chunk_parsed = _gemini_candidates_call(
            client, chunk, chunk_samples, chunk_pins, mappable_model,
        )
        if chunk_parsed:
            out.update(chunk_parsed)
    log.info("Got candidates for %d/%d source columns (chunked)", len(out), len(headers))
    return out


def _derive_llm_mapping(
    candidates_by_source: dict[str, list[dict]],
) -> dict[str, dict[str, Any]]:
    """Invert per-source candidates into the canonical→source structure that
    `_bucketize` and `apply_spec_multi` already consume.

    Rules:
      - Each source's #1 candidate with confidence ≥ LIKELY_THRESHOLD becomes
        that source's vote.
      - If a join key (policy_number, program_name, …) is voted by sources in
        multiple sheets, emit per-sheet canonical keys with the `__<sheet>`
        suffix that `_call_llm` used to produce (e.g. `policy_number__unt`).
      - If two or more sources in the SAME sheet vote for the same canonical,
        treat them as a composite source (list); confidence = mean.
    """
    votes: dict[str, list[tuple[str, float]]] = {}
    for src, cands in (candidates_by_source or {}).items():
        if not cands:
            continue
        top = cands[0]
        canon = top.get("canonical")
        try:
            conf = float(top.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        if not canon or conf < LIKELY_THRESHOLD:
            continue
        votes.setdefault(canon, []).append((src, conf))

    out: dict[str, dict[str, Any]] = {}

    def _sheet_token(src: str) -> str:
        sheet = src.split(SHEET_SEP, 1)[0]
        return re.sub(r"[^a-z0-9]+", "_", sheet.lower()).strip("_") or "sheet"

    for canon, vlist in votes.items():
        sheets_seen = {v[0].split(SHEET_SEP, 1)[0] for v in vlist}

        if canon in JOIN_KEY_FIELDS and len(sheets_seen) > 1:
            # One entry per (canonical, sheet) so each sheet's apply_spec can
            # find its own column.
            for src, conf in vlist:
                tok = _sheet_token(src)
                # First sheet keeps the bare canonical key (matches legacy);
                # subsequent sheets get the suffix.
                if canon not in out:
                    out[canon] = {"source": src, "confidence": conf}
                else:
                    out[f"{canon}__{tok}"] = {"source": src, "confidence": conf}
            continue

        if len(vlist) > 1:
            srcs = [s for s, _ in vlist]
            avg = sum(c for _, c in vlist) / len(vlist)
            out[canon] = {"source": srcs, "confidence": avg}
        else:
            src, conf = vlist[0]
            out[canon] = {"source": src, "confidence": conf}

    return out


# ---- public mapping entry points -------------------------------------------

def _bucketize(headers: list[str], llm_result: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Convert LLM result into the canonical response shape, per-sheet aware."""
    # spec is now grouped per sheet, since one canonical field may have one
    # source per sheet (the join-key case).
    spec_by_sheet: dict[str, dict[str, Any]] = {}
    source_to_mappings: dict[str, list[dict[str, Any]]] = {}

    for raw_field, payload in llm_result.items():
        # Strip __sheet suffix and find which sheet it applies to.
        if "__" in raw_field:
            field, _, sheet_suffix = raw_field.partition("__")
        else:
            field = raw_field
            sheet_suffix = None

        src = payload["source"]
        conf = payload["confidence"]
        # Determine sheet(s) from the source string(s)
        srcs = src if isinstance(src, list) else [src]
        sheets = sorted({s.split(SHEET_SEP, 1)[0] for s in srcs})
        # Place into spec per-sheet
        for sheet in sheets:
            spec_by_sheet.setdefault(sheet, {})[field] = src
        # Track bindings for bucket display
        for one_src in srcs:
            source_to_mappings.setdefault(one_src, []).append(
                {"canonical": field, "score": round(conf, 3)}
            )

    successful, likely, unsuccessful = [], [], []
    for src in headers:
        bindings = source_to_mappings.get(src, [])
        if not bindings:
            unsuccessful.append({"source": src, "canonicals": [], "score": 0.0})
            continue
        bindings.sort(key=lambda b: -b["score"])
        best = bindings[0]
        entry = {
            "source": src,
            "canonical": best["canonical"],
            "canonicals": [b["canonical"] for b in bindings],
            "bindings": bindings,
            "score": best["score"],
        }
        if best["score"] >= SUCCESS_THRESHOLD:
            successful.append(entry)
        elif best["score"] >= LIKELY_THRESHOLD:
            likely.append(entry)
        else:
            unsuccessful.append(entry)

    flat_spec = {f: s for sheet_spec in spec_by_sheet.values() for f, s in sheet_spec.items()}
    canonical_unmapped = sorted(CANONICAL_FIELDS - set(flat_spec.keys()))

    return {
        "spec": flat_spec,                 # legacy flat view (one source per canonical)
        "spec_by_sheet": spec_by_sheet,    # NEW: per-sheet, supports join keys
        "successful": successful,
        "likely": likely,
        "unsuccessful": unsuccessful,
        "canonical_unmapped": canonical_unmapped,
    }


def generate_mapping_multi(sheets: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Map headers across ALL sheets in a single LLM call, per-sheet aware."""
    log.info("=== generate_mapping_multi: %d sheets ===", len(sheets))
    qualified_headers: list[str] = []
    samples: dict[str, list[str]] = {}
    for sheet_name, df in sheets.items():
        for col in df.columns:
            q = qualify(str(sheet_name), str(col))
            qualified_headers.append(q)
            vals = df[col].dropna().astype(str).head(MAX_SAMPLES).tolist()
            samples[q] = vals

    log.info("Total qualified headers: %d", len(qualified_headers))

    # Heuristic pre-pass — pins guaranteed mappings.
    pins: dict[str, str] = {}
    for h in qualified_headers:
        canonical = _heuristic_pin(h, samples.get(h, []))
        if canonical:
            pins[canonical] = h
    if pins:
        log.info("Pre-pinned %d obvious mappings via heuristics: %s", len(pins), list(pins))

    # Cross-tenant column cache pre-pass. Any source column that's already
    # been mapped (by anyone, any tenant) with similar samples is served
    # straight from cache. Only the cache MISSES go to the LLM.
    cached_candidates: dict[str, list[dict]] = {}
    cache_miss_headers: list[str] = []
    try:
        from db import SessionLocal
        with SessionLocal() as s:
            for h in qualified_headers:
                if h in pins.values():
                    # Heuristic pin always wins — still LLM-call it so the
                    # candidates list has alternatives for the UI picker.
                    cache_miss_headers.append(h)
                    continue
                sheet, _, col = h.partition(SHEET_SEP)
                fp = sample_fingerprint(samples.get(h) or [])
                hit = cache_lookup(s, sheet, col, fp)
                if hit:
                    canon, conf = hit
                    cached_candidates[h] = [{
                        "canonical": canon,
                        "confidence": conf,
                        "reason": "cache hit",
                    }]
                else:
                    cache_miss_headers.append(h)
    except Exception as e:
        log.warning("Cache lookup skipped: %s", e)
        cache_miss_headers = list(qualified_headers)

    log.info("Cache: %d/%d source columns served from cache (LLM will see %d)",
             len(cached_candidates), len(qualified_headers), len(cache_miss_headers))

    # ONE Gemini call covers only the cache-miss columns. If every column
    # is already cached, we skip the LLM entirely.
    if cache_miss_headers:
        miss_samples = {h: samples.get(h, []) for h in cache_miss_headers}
        miss_pins = {c: s for c, s in pins.items() if s in cache_miss_headers}
        llm_candidates = _call_llm_candidates(cache_miss_headers, miss_samples, miss_pins)
    else:
        log.info("All %d headers cache-hit — skipping LLM entirely.",
                 len(qualified_headers))
        llm_candidates = {}

    candidates_by_source: dict[str, list[dict]] = {**cached_candidates, **llm_candidates}
    # Inject heuristic pins as forced top candidates so they're always #1
    # even if the model ranked something else higher.
    for canonical, src in pins.items():
        existing = candidates_by_source.get(src, [])
        existing = [c for c in existing if c.get("canonical") != canonical]
        candidates_by_source[src] = (
            [{"canonical": canonical, "confidence": 1.0,
              "reason": "Heuristic-pinned"}] + existing
        )[:TOP_N_CANDIDATES]

    llm = _derive_llm_mapping(candidates_by_source)
    # Force-merge pins (defensive — derivation above should cover them).
    for canonical, src in pins.items():
        if canonical not in llm:
            llm[canonical] = {"source": src, "confidence": 1.0}

    out = _bucketize(qualified_headers, llm)

    # NOTE: the `attribute_value_*` policy_attributes catch-all was removed —
    # if a column has no real match in the canonical model we leave it
    # unmapped so the user explicitly handles it.

    out["samples"] = samples
    out["sheets"] = list(sheets.keys())
    out["candidates_by_source"] = candidates_by_source

    # Write fresh mappings back to the cache so the NEXT upload benefits.
    # Only write LLM-derived hits (cached_candidates already came from cache).
    try:
        from db import SessionLocal
        with SessionLocal() as s:
            for h, cands in llm_candidates.items():
                if not cands:
                    continue
                top = cands[0]
                if not top.get("canonical"):
                    continue
                if float(top.get("confidence", 0)) < LIKELY_THRESHOLD:
                    continue
                sheet, _, col = h.partition(SHEET_SEP)
                fp = sample_fingerprint(samples.get(h) or [])
                cache_store(s, sheet, col, fp,
                            top["canonical"], top["confidence"],
                            source="llm")
            s.commit()
    except Exception as e:
        log.warning("Cache write skipped: %s", e)

    log.info(
        "=== generate_mapping_multi done: success=%d likely=%d weak=%d ===",
        len(out["successful"]), len(out["likely"]), len(out["unsuccessful"]),
    )
    return out


# ---- spec application ------------------------------------------------------

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
        #       "policy": {"tria_premium": 12500.00, …},   # user extra field
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

# ─── Multi-table detection (one sheet, several stacked tables) ───────────────
#
# A BDX sheet sometimes stacks two DIFFERENT tables ("premium section" then a
# "claims section" with its own header). The reader takes ONE header per sheet,
# so everything after the second header is misread — its header row becomes a
# data row and its columns are forced into the first table's layout. Detected
# here so Bordereau Setup and Process Bordereau can WARN the user up front.
#
# A repeated band of the SAME header (grouped sheets restate their header
# mid-way) is NOT a second table and is deliberately not flagged.

def _mt_headerish_cell(v) -> bool:
    s = str(v).strip()
    if not s or len(s) > 48:
        return False
    try:
        float(s.replace(",", "").replace("$", "").replace("%", ""))
        return False
    except ValueError:
        pass
    return not re.match(r"^\d{4}-\d{2}-\d{2}", s)


def _mt_numericish_cell(v) -> bool:
    """A value shaped like DATA (number or date) rather than a column name."""
    s = str(v).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s) or re.match(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", s):
        return True
    try:
        float(s.replace(",", "").replace("$", "").replace("%", ""))
        return True
    except ValueError:
        return False


def _mt_empty(c) -> bool:
    """None, NaN, or whitespace-only — pandas raw grids carry NaN for blanks,
    and str(nan) == "nan" would otherwise count as a filled text cell."""
    if c is None:
        return True
    if isinstance(c, float) and c != c:      # NaN
        return True
    return not str(c).strip()


def _mt_row_kind(cells: list) -> str:
    """'blank' | 'title' (≤2 filled) | 'header' | 'data'.

    The discriminator between a header row and a data row is NUMBERS: a header
    is column NAMES — ≥3 distinct text cells and not a single numeric/date
    value — while a real BDX data row virtually always carries at least one
    amount, rate or date. Text-share alone cannot tell them apart (a data
    row's policy number, insured name and state are all header-ish text)."""
    filled = [c for c in cells if not _mt_empty(c)]
    if not filled:
        return "blank"
    if len(filled) <= 2:
        return "title"
    numericish = sum(1 for c in filled if _mt_numericish_cell(c))
    if (numericish == 0
            and len(filled) >= 3
            and all(_mt_headerish_cell(c) for c in filled)
            and len({str(c).strip().lower() for c in filled}) >= 3):
        return "header"
    return "data"


def _mt_norm_set(cells: list) -> frozenset:
    return frozenset(str(c).strip().lower() for c in cells if not _mt_empty(c))


def filter_findings_to_data_sheets(findings: list[dict],
                                   headers_by_sheet: dict) -> list[dict]:
    """Drop multi-table findings that sit on NON-DATA sheets, judged by the
    SAME sheet-role classifier the output-template flow uses
    (exporter.classify_sheet_roles) — from sheet names, column headers and how
    the tabs relate, with no particular name treated as special. This is the
    SOLE authority on summary/reference vs data: sheet NAMES are never trusted
    for that call, so "Reinsurance Summary All TY" and a name-blind "TY1
    Rollup" are judged the same way.

    Called ONLY when findings exist, so the AI is consulted exactly when a
    refusal is on the table — a clean workbook costs nothing. Fail-open to the
    UNFILTERED findings when the classifier is unavailable: the AI can only
    ever rescue a sheet, never condemn one.
    """
    if not findings:
        return findings
    try:
        import os
        from google import genai
        from exporter import classify_sheet_roles, is_reference_sheet
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return findings
        structure = {"sheets": [
            {"sheet_name": str(n),
             "columns": [{"column_name": str(c)} for c in (cols or []) if c is not None]}
            for n, cols in headers_by_sheet.items()]}
        client = genai.Client(api_key=api_key)
        classify_sheet_roles(client, structure)
        non_data = {sh["sheet_name"] for sh in structure["sheets"]
                    if is_reference_sheet(sh)}
        kept = [f for f in findings if f["sheet"] not in non_data]
        dropped = len(findings) - len(kept)
        if dropped:
            print(f"[multi-table] sheet-role classifier cleared {dropped} "
                  f"finding(s) on non-data sheet(s): "
                  f"{sorted({f['sheet'] for f in findings} & non_data)}")
        return kept
    except Exception as exc:  # noqa: BLE001 — classification must never block
        print(f"[multi-table] sheet-role classification unavailable ({exc}); "
              f"keeping name-based result")
        return findings


def detect_multiple_tables(file_bytes: bytes, skip_rows: int = 0,
                           only_sheets=None) -> list[dict]:
    """Sheets that appear to contain MORE than one table.

    Signature required for a second table — all of it, so the false-positive
    controls (repeated header bands, totals rows, footers) stay silent:
      • a header-like row BELOW the sheet's primary header,
      • that STARTS A NEW BLOCK — a blank row, short title row, or an
        unlabeled numeric totals row sits within the few rows above it (a
        header jammed directly between data rows is a pagination band
        restating the SAME table's header, and is not flagged),
      • with real data rows both before it and after it.
    The second table's header may be DIFFERENT (a claims section under a
    premium section) or IDENTICAL (two same-layout tables stacked, e.g.
    current + prior month) — both are flagged; `same_header` says which.

    Returns [{"sheet", "row" (1-based), "headers" (first few names)}] — one
    entry per extra table found; [] when every sheet is a single table.
    """
    try:
        raw = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, header=None)
    except Exception:
        try:
            df = pd.read_csv(io.BytesIO(file_bytes), header=None,
                             skip_blank_lines=False, dtype=object)
            raw = {"Sheet1": df}
        except Exception:
            return []

    allowed = ({str(n).strip().lower() for n in only_sheets}
               if only_sheets is not None else None)
    findings: list[dict] = []
    for sheet, df in raw.items():
        if allowed is not None and str(sheet).strip().lower() not in allowed:
            continue
        rows = df.values.tolist()
        if skip_rows:
            rows = rows[skip_rows:]
        kinds = [_mt_row_kind(r) for r in rows]
        try:
            h = next(i for i, k in enumerate(kinds[:20]) if k == "header")
        except StopIteration:
            continue
        primary = _mt_norm_set(rows[h])

        # Per-row separator test. Besides blank/title rows, an UNLABELED
        # all-numeric SPARSE row is a totals artifact (real BDX totals rows
        # carry only the summed amount columns — a fraction of the table's
        # width) and separates blocks the same way a blank line does. A real
        # data row fills most tracked columns and carries text (names, cities).
        def _is_sep(idx: int) -> bool:
            if kinds[idx] in ("blank", "title"):
                return True
            cells = [c for c in rows[idx] if not _mt_empty(c)]
            return (len(cells) >= 3
                    and all(_mt_numericish_cell(c) for c in cells)
                    and len(cells) < 0.6 * max(3, len(primary)))

        seen_data = False
        for i in range(h + 1, len(rows)):
            k = kinds[i]
            if k == "data":
                seen_data = True
                continue
            if k != "header":
                continue
            names = _mt_norm_set(rows[i])
            # Separation looks back a few rows so a totals row sitting between
            # the blank gap and the repeated header doesn't mask the break.
            sep = any(_is_sep(j) for j in range(max(0, i - 3), i))
            follows = any(kk == "data" for kk in kinds[i + 1:i + 6])
            if not (seen_data and sep and follows):
                continue                       # mid-data band — same table
            # A repeated IDENTICAL header that starts a NEW BLOCK is a second
            # table too (two same-layout tables stacked in one sheet, e.g.
            # current month + prior month) — only a band jammed directly
            # between data rows is pagination noise, and that fails `sep`.
            findings.append({
                "sheet": str(sheet),
                "row": (skip_rows or 0) + i + 1,
                "headers": [str(c).strip() for c in rows[i]
                            if not _mt_empty(c)][:6],
                "same_header": names == primary,
            })
            if names != primary:
                primary = names               # keep scanning for a 3rd table
    return findings
