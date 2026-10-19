"""
row_key.py
──────────
The stable row key of plan task 23.1 (Previous Submission Reconciliation):
how one transaction is recognised in two versions of the same submission.

The key (decided 16 Oct 2026, from the client's Open Questions Register):

    Policy Number + Transaction Type + Transaction Effective Date
    + Bordereau Reported Period

  - Policy Number and Transaction Effective Date are MANDATORY on every row.
    A row missing either is not compared at all — it is reported as
    "Not compared" and never classified.
  - Transaction Type and Reported Period are used when they are there
    ("go with whatever we have"): a sheet without one of those columns is
    keyed on the parts both versions have; a blank cell is handled by the
    reduced-key retry in version_diff.
  - The amount is never part of the key. It is compared separately, after
    two rows are paired (version_diff).

Which column holds which part comes ONLY from the setup (decided 16 Oct
2026: no heading word lists): its column mapping (input column → data-model
field) and, for the reported period, which has no data-model field, the
period column named in the setup's ITD settings. A part the setup does not
name is missing; a sheet missing a mandatory part is not compared, and says
which part to map in Bordereau Setup.

Values are cleaned with the system's own rules, so formatting never breaks
a match: ids and words as itd.norm_value spells them, dates as the setup
reads them (direct_lane._date_reformat, with the setup's input format when
its rule has one), reported periods by month (itd.period_of).

No database and no web imports, so it can be tested on its own.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable, Optional

import itd
from data_model import resolve_field

POLICY, TYPE, DATE, PERIOD = "policy", "type", "date", "period"
PARTS = (POLICY, TYPE, DATE, PERIOD)
MANDATORY = (POLICY, DATE)
OPTIONAL = (TYPE, PERIOD)
PART_WORDS = {
    POLICY: "Policy number",
    TYPE: "Transaction type",
    DATE: "Transaction effective date",
    PERIOD: "Reported period",
}

# The data-model field each key part is mapped to in a setup (data_model.py).
# The reported period has none: it comes from the setup's ITD settings.
CANONICAL = {
    POLICY: "policy_number",
    TYPE: "premium_transaction_type",
    DATE: "premium_transaction_effective_date",
    PERIOD: None,
}
# The insured's name, shown beside the key so people can find a row — never
# part of the key.
NAME_FIELD = "policyholder_legal_name"

_ISO = "%Y-%m-%d"


def heading(name: Any) -> str:
    """A column heading as plain lower-case words, for matching one column
    across two versions: 'Policy No.' → 'policy no',
    'TransactionEffectiveDate' → 'transaction effective date'."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(name or ""))
    return " ".join(re.sub(r"[^0-9a-z%]+", " ", s.lower()).split())


def clean_text(v: Any) -> Optional[str]:
    """An id or a word, spelt one way (itd.norm_value — the ITD key rule)."""
    return itd.norm_value(v)


def clean_policy(v: Any) -> Optional[str]:
    """A policy number, or None when there is none — including the values
    ingest itself treats as no policy number (ingester._POLICY_NUMBER_PLACEHOLDERS,
    e.g. 'N/A', '-', 'TBD')."""
    from ingester import _POLICY_NUMBER_PLACEHOLDERS
    text = itd.norm_value(v)
    if text is None or str(v).strip().casefold() in _POLICY_NUMBER_PLACEHOLDERS:
        return None
    return text


def clean_date(v: Any, from_fmt: Optional[str] = None) -> Optional[str]:
    """A date-like cell as 'YYYY-MM-DD', read the way the setup reads it:
    a real date, an Excel serial number (mapper's rule), or text parsed by
    the setup's date rule (direct_lane._date_reformat: the rule's own input
    format when it names one, else the system's list of input formats). A
    cell that is not blank but cannot be read as a date keeps its cleaned
    text, so the same odd value in both versions still matches."""
    if itd._blank(v):
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        from mapper import _excel_serial_to_date
        d = _excel_serial_to_date(v)
        if d is not None:
            return (d.date() if isinstance(d, datetime) else d).isoformat()
    from direct_lane import _date_reformat
    got = _date_reformat(str(v).strip(), from_fmt, _ISO)
    try:
        return datetime.strptime(str(got), _ISO).date().isoformat()
    except (TypeError, ValueError):
        return itd.norm_value(v)


def clean_period(v: Any, from_fmt: Optional[str] = None) -> Optional[str]:
    """The reported period as 'YYYY-MM', compared by month (itd.period_of,
    or the setup's date format when its rule names one); a value neither
    can read keeps its cleaned text."""
    if itd._blank(v):
        return None
    if from_fmt:
        d = clean_date(v, from_fmt)
        if d and re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            return d[:7]
    return itd.period_of(v) or itd.norm_value(v)


CLEAN = {POLICY: clean_policy, TYPE: clean_text, DATE: clean_date, PERIOD: clean_period}


# ── which column holds which part ───────────────────────────────────────────

def _field(f: Any) -> Optional[str]:
    return resolve_field(str(f).strip()) if f else None


def resolve_key_columns(columns: Iterable[str], canonical_of: Optional[dict] = None,
                        period_column: Optional[str] = None) -> dict:
    """{part: column or None} for one sheet, from the setup only: the period
    column named in its ITD settings, and its column mapping (`canonical_of`:
    input column → data-model field) for the other parts. A column is used
    for one part only.

    Returns {"columns": {part: col|None}, "found_by": {part: 'setting'|'mapping'}}."""
    cols = [c for c in columns or [] if c is not None and str(c).strip()]
    canon = {c: _field(f) for c, f in (canonical_of or {}).items()}
    chosen: dict[str, Optional[str]] = {p: None for p in PARTS}
    found_by: dict[str, str] = {}
    used: set[str] = set()

    def take(part: str, col: str, how: str) -> None:
        chosen[part] = col
        found_by[part] = how
        used.add(col)

    if period_column:
        want = heading(period_column)
        col = next((c for c in cols if heading(c) == want), None)
        if col:
            take(PERIOD, col, "setting")
    for part in PARTS:
        if chosen[part] or not CANONICAL[part]:
            continue
        col = next((c for c in cols if c not in used and canon.get(c) == CANONICAL[part]), None)
        if col:
            take(part, col, "mapping")
    return {"columns": chosen, "found_by": found_by}


def name_column(columns: Iterable[str], canonical_of: Optional[dict] = None,
                taken: Iterable[str] = ()) -> Optional[str]:
    """The column the setup maps to the insured's name (display only), or None."""
    skip = set(taken or ())
    canon = {c: _field(f) for c, f in (canonical_of or {}).items()}
    return next((c for c in columns or [] if c not in skip and canon.get(c) == NAME_FIELD),
                None)


def parts_for(cols_a: dict, cols_b: dict) -> Optional[list[str]]:
    """The key parts BOTH versions of a sheet have a column for, in key order —
    "go with whatever we have" at column level. None when either version has
    no policy-number or no effective-date column: then the sheet is not
    compared at all."""
    a, b = cols_a or {}, cols_b or {}
    if any(not a.get(p) or not b.get(p) for p in MANDATORY):
        return None
    return [p for p in PARTS if a.get(p) and b.get(p)]


# ── one row's key ───────────────────────────────────────────────────────────

def row_values(row: dict, cols: dict, parts: Iterable[str],
               formats: Optional[dict] = None) -> dict[str, Optional[str]]:
    """The cleaned value of each key part on one row (None when blank).
    `formats` = {input column: date format} from the setup's date rules."""
    formats = formats or {}
    out = {}
    for p in parts:
        col = cols.get(p)
        if not col:
            out[p] = None
        elif p in (DATE, PERIOD):
            out[p] = CLEAN[p](row.get(col), formats.get(col))
        else:
            out[p] = CLEAN[p](row.get(col))
    return out


def missing_mandatory(values: dict) -> list[str]:
    """The mandatory parts a row lacks — any → the row is not compared."""
    return [p for p in MANDATORY if not values.get(p)]


def key_of(values: dict, parts: Iterable[str]) -> tuple:
    """The key itself: the cleaned parts in key order. A blank optional part
    is part of the key as '' (the reduced-key retry looks past it)."""
    return tuple(values.get(p) or "" for p in parts)


def key_text(values: dict, parts: Iterable[str]) -> str:
    """The key for a person: 'pol-001 · new · 2025-08-15 · 2025-08'."""
    return " · ".join(values.get(p) or "(blank)" for p in parts)


def is_heading_repeat(row: dict, cols: dict) -> bool:
    """A repeated header line: the policy cell holds the policy column's own
    name. Such a line is skipped, never listed."""
    col = cols.get(POLICY)
    return bool(col) and heading(row.get(col)) == heading(col)
