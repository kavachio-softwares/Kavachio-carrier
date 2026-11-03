from db import canonical_engine
from sqlalchemy.sql import text 
FIELD_TYPE_MAPPING = {
    0: "text",
    1: "button",
    2: "checkbox",
    3: "combobox",
    4: "listbox",
    5: "radio",
    6: "signature",
}


# =========================================================
# CANONICAL DATA MODEL — used by Stage B synthesis prompts + normalization
# Sourced directly from the warehouse data model's BDX fields
# (data_model.BDX_FIELDS) so it always tracks data_model.py rather than being
# hand-maintained. Shape: { table_name: [column, ...] }.
# =========================================================

from data_model import DATA_MODEL as _DATA_MODEL, BDX_FIELDS as _BDX_FIELDS


def _build_canonical_schema() -> dict:
    """Group the BDX-source canonical fields by table → ordered unique columns."""
    schema: dict[str, list[str]] = {}
    for _key in _BDX_FIELDS:
        _meta = _DATA_MODEL.get(_key) or {}
        _t, _c = _meta.get("table"), _meta.get("column")
        if _t and _c and _c not in schema.setdefault(_t, []):
            schema[_t].append(_c)
    return schema


def _build_canonical_field_meta() -> dict:
    """Per-field type + description, so the synthesis prompt can show the LLM
    what each column means (not just its name). Shape:
        { table: { column: {"type": str, "description": str} } }
    Same source + ordering as CANONICAL_SCHEMA (data_model.BDX_FIELDS)."""
    meta: dict[str, dict] = {}
    for _key in _BDX_FIELDS:
        _m = _DATA_MODEL.get(_key) or {}
        _t, _c = _m.get("table"), _m.get("column")
        if not (_t and _c):
            continue
        _tbl = meta.setdefault(_t, {})
        if _c not in _tbl:
            _tbl[_c] = {
                "type": _m.get("type"),
                "description": (_m.get("description") or "").strip(),
            }
    return meta


# {table: [column, ...]} — used by the normalizer's existence checks.
CANONICAL_SCHEMA = _build_canonical_schema()
# {table: {column: {type, description}}} — used to enrich the synthesis prompt.
CANONICAL_FIELD_META = _build_canonical_field_meta()

# =========================================================
# RULE CLASS LIBRARY — registry of rule types the engine
# knows how to run (AJV-bound + custom-bound).
# Stage A may only emit rule_types from this catalog;
# normalizer validates rule_spec against spec_schema.
# =========================================================
# Fetch RULE_CLASS_LIBRARY from the database
def fetch_rule_class_library():
    """
    Fetch the RULE_CLASS_LIBRARY data from the database using canonical_engine.
    """
    try:
        # Use canonical_engine to connect to the database
        with canonical_engine.connect() as connection:
            # Wrap the query in text()
            query = text("""
                SELECT name, display_name, description, rule_engine, default_severity, default_stage, availability
                FROM rule_class_library;
            """)
            result = connection.execute(query)

            # Fetch all rows
            rows = result.fetchall()

            # Convert rows to the required format
            rule_class_library = [
                {
                    "name": row[0],
                    "display_name": row[1],
                    "description": row[2],
                    "rule_engine": row[3],
                    "default_severity": row[4],
                    "default_stage": row[5],
                    "availability": row[6],
                }
                for row in rows
            ]

            return rule_class_library

    except Exception as e:
        print(f"Error fetching RULE_CLASS_LIBRARY: {e}")
        return []

# Fetch RULE_CLASS_LIBRARY from the database
RULE_CLASS_LIBRARY = fetch_rule_class_library()
# print("Fetched RULE_CLASS_LIBRARY from database:", RULE_CLASS_LIBRARY)
if not RULE_CLASS_LIBRARY:
    raise ValueError("Failed to load RULE_CLASS_LIBRARY from the database.")



# =========================================================
# Default auto-trust threshold: confidence ≥ threshold →
# rule_status='active'; below → 'needs_review'.
# =========================================================

DEFAULT_RULE_AUTO_TRUST_THRESHOLD = 0.85


# =========================================================
# Section header detection patterns for Pipeline 1.2
# Matched in priority order against page text lines.
# =========================================================

SECTION_HEADER_PATTERNS = [
    r"^\s*SECTION\s+\d+(?:\.\d+)?(?:\s*[—\-:]\s*[A-Z][A-Z0-9 \-/&,]+)?\s*$",
    r"^\s*ARTICLE\s+[IVXLC0-9]+(?:\s*[—\-:]\s*[A-Z][A-Z0-9 \-/&,]+)?\s*$",
    r"^\s*\d+(?:\([a-z]\))?\.\s+[A-Z][A-Za-z0-9 \-/&,]+\s*$",
    r"^\s*[A-Z][A-Z0-9 \-/&,]{4,}\s*$"  # all-caps heading fallback
]


# =========================================================
# Contract upload validation (FILE_TOO_LARGE / encrypted / length gate, etc.)
# =========================================================

MAX_CONTRACT_UPLOAD_MB = 50
MAX_CONTRACT_UPLOAD_BYTES = MAX_CONTRACT_UPLOAD_MB * 1024 * 1024

# THE LENGTH FLOOR IS COUNTED IN WORDS, not bytes.
#
# It used to be a 10 KB minimum file size, which judged the FILE rather than the
# contract: a PDF writer that compresses well produces a real three-page program
# schedule in under 9 KB, and that was refused as "too small to be a binding
# authority contract" before a word of it was read. Words are what makes a
# contract long enough to have terms in it, so words are what is counted — read
# from the same extracted text the rest of the pipeline reads.
MIN_CONTRACT_WORDS = 100

CONTRACT_UPLOAD_ERROR_MESSAGES = {
    "FILE_TOO_LARGE":
        "This file exceeds the 50 MB limit. Please split or compress the document and try again.",
    # Formatted with {words} and {min_words} — the count is part of the message
    # so "too short" says HOW short, and the minimum is never restated here.
    "CONTRACT_TOO_SHORT":
        "Your contract is too short to process. A contract needs at least {min_words} words, and this one has {words}.",
    "INVALID_FORMAT":
        "We accept PDF and Word (.docx) documents. The file you uploaded appears to be a different format.",
    "ENCRYPTED_PDF":
        "This PDF is password-protected. Please remove the password and re-upload.",
    "CORRUPT_FILE":
        "We couldn't read this file. It may be corrupted. Try saving it again or contact support.",
    "INSUFFICIENT_TEXT":
        "This document doesn't have enough readable text. If it's a scanned document, please OCR it before uploading.",
}

