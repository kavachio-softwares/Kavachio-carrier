"""Row-level non-data detection for a sheet's parsed records.

A BDX sheet's DATA rows (the transactions rules validate) are often mixed with
rows that are not really data at all: a "TX Total" subtotal, a "Grand Total"
row, a re-printed header row, a blank spacer. Nothing upstream filters these
out — `direct_lane.build_landing_record` only drops a row that is 100% blank —
so a sparse-but-non-empty summary row reaches validation like any other row,
and a `required_field` rule flags every blank cell on it as a false exception.

This is the ROW-level analog of `exporter.classify_sheet_roles`, which already
does the same job at the SHEET level (data/reference/summary tabs).

By design, every check here decides a row's category from THAT ROW'S OWN
cells only — how many of its own cells are blank, whether any of its own
cells is summary/footer wording, whether its own cells echo the column
headers. Nothing compares a row against other rows' values (no per-column
median, no cross-row model) — a row's classification never depends on what
any OTHER row in the sheet contains. Feature extraction is vectorized with
pandas (not a per-row Python loop) since a BDX upload routinely runs into the
hundreds of thousands of rows.

How blank a row is gets measured against the columns the setup can actually
fill (`measure_cols`), not the template's full width. A 169-column template
whose setup maps 27 writes nothing into the other 142 on ANY row, so counting
them made every real transaction row read as 85% empty and excluded it. The
measured set is fixed per sheet by the setup, never taken from other rows'
values, so a row is still judged from its own cells.

Entry point: classify_sheet_rows(records, cols, measure_cols=None).
"""
from __future__ import annotations

import re
from collections import Counter

import pandas as pd

# Whole-cell (after trim + casefold) summary/footer text, or a SHORT label
# ending in one ("TX Total", "Grand Total", "Casualty Subtotal") — the group
# prefix is deliberately unconstrained (state/LOB/program code) so this stays
# generic, not tied to any specific column or value vocabulary. The keyword
# must be at the END of the cell, so a genuine data value like "Total Wine
# Distribution Center" (keyword NOT at the end) never matches.
_SUMMARY_KEYWORDS = re.compile(
    r"^(\S+\s+){0,3}(grand\s+)?(total|subtotal|sub[\s-]total|summary|count)s?$"
    r"|^page\s+\d+\s+of\s+\d+$"
    r"|^continued\.{0,3}$",
    re.IGNORECASE,
)

# A row that is at least this blank is treated as non-data outright.
_BLANK_PCT_THRESHOLD = 0.7
# A row this blank whose every filled cell is a bare number (no text at all) is
# an aggregation/totals row even with NO summary wording — a real BDX
# transaction row always carries some text (an insured name, a city, a state, a
# policy id), while an unlabelled totals row holds nothing but sums in the
# amount columns. Deliberately lower than _BLANK_PCT_THRESHOLD (this signal has
# the all-numeric evidence on top of blankness), but high enough that a dense
# all-numeric table row (rare, but conceivable) is never touched.
_NUMERIC_ONLY_BLANK_PCT = 0.5


def _to_numeric(s: pd.Series) -> pd.Series:
    """Best-effort numeric parse of a column's cells (currency/percent/grouping
    punctuation stripped). NaN where a cell isn't a bare number — dates,
    datetimes and any text stay non-numeric on purpose."""
    cleaned = (s.astype(str).str.strip()
               .str.replace(",", "", regex=False)
               .str.replace("$", "", regex=False)
               .str.replace("%", "", regex=False))
    return pd.to_numeric(cleaned, errors="coerce")


def _compute_features(df: pd.DataFrame, cols: list[str],
                      measure_cols: list[str] | None = None) -> pd.DataFrame:
    """One vectorized pass over the whole sheet → a per-row feature frame.
    No per-row Python loop — this is what keeps it fast at lakhs-of-rows scale.
    Every feature is computed from each row's OWN cells (plus the fixed column
    headers) — never from other rows' values.

    The filled/blank share and the numeric-only test read `measure_cols` (the
    columns the setup fills; all of `cols` when not given). Wording and header
    echoes are looked for in every column — a blank cell is neutral there."""
    text = df.astype(str)
    stripped = text.apply(lambda c: c.str.strip())
    blank = df.isna() | stripped.eq("") | stripped.apply(lambda c: c.str.lower().eq("none"))

    measure = [c for c in dict.fromkeys(measure_cols or []) if c in df.columns] or list(cols)
    n_cols = len(measure) or 1
    filled_count = (~blank[measure]).sum(axis=1)
    blank_pct = blank[measure].sum(axis=1) / n_cols

    # Text-pattern hit: any non-blank cell matches a summary/footer keyword.
    kw_hit = pd.DataFrame(False, index=df.index, columns=cols)
    for c in cols:
        vals = stripped[c].where(~blank[c], "")
        kw_hit[c] = vals.str.match(_SUMMARY_KEYWORDS, na=False)
    text_hit = kw_hit.any(axis=1)

    # Header-repeat: every non-blank cell equals its own column's header text
    # (fixed metadata, not another row's data), with at least one non-blank
    # cell (an all-blank row is BLANK, not this).
    header_match = pd.DataFrame(True, index=df.index, columns=cols)
    for c in cols:
        col_header = str(c).strip().casefold()
        header_match[c] = blank[c] | (stripped[c].str.casefold() == col_header)
    header_repeat = header_match.all(axis=1) & (~blank).any(axis=1)

    # Numeric-only: every filled cell in the row parses as a bare number —
    # no name, no date, no code, no label anywhere. The signature of an
    # unlabelled totals row (see _NUMERIC_ONLY_BLANK_PCT).
    numeric = pd.DataFrame(False, index=df.index, columns=measure)
    for c in measure:
        numeric[c] = _to_numeric(df[c]).notna()
    numeric_count = (numeric & ~blank[measure]).sum(axis=1)
    numeric_only = (filled_count > 0) & (numeric_count == filled_count)

    return pd.DataFrame({
        "filled_count": filled_count,
        "blank_pct": blank_pct,
        "text_hit": text_hit,
        "header_repeat": header_repeat,
        "numeric_only": numeric_only,
    })


def _classify(feat: pd.DataFrame) -> dict[int, dict]:
    """Deterministic, per-row checks — each decided from that row's own
    feature values only, no other row's data involved."""
    classified: dict[int, dict] = {}
    for i, row in feat.iterrows():
        if row["filled_count"] == 0:
            classified[i] = {"category": "BLANK", "reason": "row is entirely empty"}
            continue
        if row["header_repeat"]:
            classified[i] = {"category": "HEADER_REPEAT",
                              "reason": "every filled cell matches its column header"}
            continue
        very_blank = row["blank_pct"] >= _BLANK_PCT_THRESHOLD
        if row["text_hit"]:
            reason = "contains summary/total wording"
            if very_blank:
                reason += f" and {round(row['blank_pct'] * 100)}% of cells are empty"
            classified[i] = {"category": "SUMMARY", "reason": reason}
            continue
        # Unlabelled totals row: nothing but numbers, and mostly empty. Catches
        # the summary rows that carry NO wording at all (no "Total", no label —
        # just sums sitting in the amount columns).
        if row["numeric_only"] and row["blank_pct"] >= _NUMERIC_ONLY_BLANK_PCT:
            classified[i] = {
                "category": "SUMMARY",
                "reason": (f"every filled cell is a bare number (no text/date "
                           f"anywhere) and {round(row['blank_pct'] * 100)}% of "
                           f"cells are empty — an unlabelled totals row"),
            }
            continue
        if very_blank:
            classified[i] = {"category": "BLANK",
                              "reason": f"{round(row['blank_pct'] * 100)}% of cells are empty"}
    return classified


def classify_sheet_rows(records: list[dict], cols: list[str],
                        measure_cols: list[str] | None = None,
                        ) -> tuple[list[dict], list[dict]]:
    """Classify each record as DATA or non-data (BLANK/SUMMARY/HEADER_REPEAT).
    Returns (data_records, excluded) — excluded entries carry {"position",
    "category", "reason", "row"} for audit; data_records is the filtered list,
    in original order, ready to load exactly as before.

    `measure_cols` are the columns the setup fills (see _compute_features).
    None or empty, or none of them in `cols`, measures every column, exactly as
    before it existed."""
    if not records or not cols:
        return records, []

    df = pd.DataFrame(records, columns=cols)
    feat = _compute_features(df, cols, measure_cols)
    classified = _classify(feat)

    data_records: list[dict] = []
    excluded: list[dict] = []
    for i, rec in enumerate(records):
        hit = classified.get(i)
        if hit is None:
            data_records.append(rec)
        else:
            excluded.append({"position": i + 1, "category": hit["category"],
                              "reason": hit["reason"], "row": rec})
    return data_records, excluded


def excluded_summary(excluded: list[dict]) -> dict:
    """{category: count} — for a one-line audit log."""
    return dict(Counter(e["category"] for e in excluded))
