"""Row classifier measured over the columns a setup fills (no DB, no network).

WHAT WENT WRONG. The blank-row test divided by the TEMPLATE's column count. A
wide standard template whose setup maps a small share of its columns writes
nothing into the rest on any row, so every real transaction row read as mostly
empty, was excluded as BLANK, and validation then crashed on an empty insert and
stored the file as clean.

These tests build their sheets programmatically — N template columns, K of them
filled by the setup, rows filling f of K — with no real column names, and pin:
a dense row is DATA however small K/N is; an empty or summary row is still
excluded; and with no measure the classifier behaves exactly as it did (the old
function is kept below as the oracle).

    python -m pytest test_row_classifier_measure.py
"""
from __future__ import annotations

import pandas as pd
import pytest

import row_classifier as rc


# ── oracle: the classifier as it was before `measure_cols` ────────────────────
def _old_compute_features(df, cols):
    text = df.astype(str)
    stripped = text.apply(lambda c: c.str.strip())
    blank = df.isna() | stripped.eq("") | stripped.apply(lambda c: c.str.lower().eq("none"))
    n_cols = len(cols) or 1
    filled_count = (~blank).sum(axis=1)
    blank_pct = blank.sum(axis=1) / n_cols
    kw_hit = pd.DataFrame(False, index=df.index, columns=cols)
    for c in cols:
        vals = stripped[c].where(~blank[c], "")
        kw_hit[c] = vals.str.match(rc._SUMMARY_KEYWORDS, na=False)
    text_hit = kw_hit.any(axis=1)
    header_match = pd.DataFrame(True, index=df.index, columns=cols)
    for c in cols:
        col_header = str(c).strip().casefold()
        header_match[c] = blank[c] | (stripped[c].str.casefold() == col_header)
    header_repeat = header_match.all(axis=1) & (~blank).any(axis=1)
    numeric = pd.DataFrame(False, index=df.index, columns=cols)
    for c in cols:
        numeric[c] = rc._to_numeric(df[c]).notna()
    numeric_count = (numeric & ~blank).sum(axis=1)
    numeric_only = (filled_count > 0) & (numeric_count == filled_count)
    return pd.DataFrame({"filled_count": filled_count, "blank_pct": blank_pct,
                         "text_hit": text_hit, "header_repeat": header_repeat,
                         "numeric_only": numeric_only})


def _old_classify_sheet_rows(records, cols):
    if not records or not cols:
        return records, []
    df = pd.DataFrame(records, columns=cols)
    classified = rc._classify(_old_compute_features(df, cols))
    data, excluded = [], []
    for i, rec in enumerate(records):
        hit = classified.get(i)
        if hit is None:
            data.append(rec)
        else:
            excluded.append({"position": i + 1, "category": hit["category"],
                             "reason": hit["reason"], "row": rec})
    return data, excluded


# ── fixtures ──────────────────────────────────────────────────────────────────
def _cols(n):
    return [f"col_{i:03d}" for i in range(n)]


def _row(measure, filled, *, text=True):
    """A record carrying only the setup's columns (what a projection writes),
    `filled` of them non-empty; text values unless text=False."""
    rec = {c: None for c in measure}
    for i, c in enumerate(measure[:filled]):
        rec[c] = f"value {i}" if text else str(1000 + i)
    return rec


def _categories(excluded, n):
    by_pos = {e["position"]: e["category"] for e in excluded}
    return [by_pos.get(i + 1, "DATA") for i in range(n)]


# ── measured over the filled columns ─────────────────────────────────────────
@pytest.mark.parametrize("n_template,k_measure,f_filled", [
    (169, 27, 25), (162, 34, 33), (150, 9, 8), (120, 60, 40), (40, 40, 30),
])
def test_dense_row_is_data_however_narrow_the_mapping(n_template, k_measure, f_filled):
    cols = _cols(n_template)
    measure = cols[:k_measure]
    recs = [_row(measure, f_filled) for _ in range(5)]
    data, excluded = rc.classify_sheet_rows(recs, cols, measure_cols=measure)
    assert excluded == [] and len(data) == 5
    # …while measuring the whole template (the old way) excludes it once the
    # template is much wider than the mapping.
    if 1 - f_filled / n_template >= rc._BLANK_PCT_THRESHOLD:
        _, old_excluded = _old_classify_sheet_rows(recs, cols)
        assert {e["category"] for e in old_excluded} == {"BLANK"}


@pytest.mark.parametrize("n_template,k_measure", [(169, 27), (150, 9), (30, 30)])
def test_empty_row_is_still_blank(n_template, k_measure):
    cols = _cols(n_template)
    measure = cols[:k_measure]
    recs = [_row(measure, k_measure), _row(measure, 0)]
    _, excluded = rc.classify_sheet_rows(recs, cols, measure_cols=measure)
    assert _categories(excluded, 2) == ["DATA", "BLANK"]


def test_mostly_empty_over_the_measure_is_blank():
    cols = _cols(100)
    measure = cols[:20]
    recs = [_row(measure, 18), _row(measure, 3)]           # 85% of the measure empty
    _, excluded = rc.classify_sheet_rows(recs, cols, measure_cols=measure)
    assert _categories(excluded, 2) == ["DATA", "BLANK"]


def test_summary_wording_is_summary_on_a_narrow_mapping():
    cols = _cols(169)
    measure = cols[:27]
    total = _row(measure, 0)
    total[measure[0]] = "TX Total"
    total[measure[5]] = "12,500.00"
    total[measure[6]] = "3,100.00"
    recs = [_row(measure, 25), total]
    _, excluded = rc.classify_sheet_rows(recs, cols, measure_cols=measure)
    assert _categories(excluded, 2) == ["DATA", "SUMMARY"]
    assert "summary/total wording" in excluded[0]["reason"]


def test_numbers_only_sparse_row_is_summary():
    cols = _cols(169)
    measure = cols[:27]
    recs = [_row(measure, 25), _row(measure, 3, text=False)]
    _, excluded = rc.classify_sheet_rows(recs, cols, measure_cols=measure)
    assert _categories(excluded, 2) == ["DATA", "SUMMARY"]
    assert "bare number" in excluded[0]["reason"]


def test_small_measure_pins_the_numeric_and_blank_bars():
    """K <= 10: a row carrying two of its columns is judged against those ten.
    A date and an amount is a sparse row (80% empty) → BLANK; two bare amounts
    and nothing else → SUMMARY; half the columns with text → DATA."""
    cols = _cols(150)
    measure = cols[:10]
    date_and_amount = _row(measure, 0)
    date_and_amount[measure[0]] = "2024-03-01"
    date_and_amount[measure[1]] = "1500"
    two_amounts = _row(measure, 2, text=False)
    half_text = _row(measure, 5)
    _, excluded = rc.classify_sheet_rows([date_and_amount, two_amounts, half_text],
                                         cols, measure_cols=measure)
    assert _categories(excluded, 3) == ["BLANK", "SUMMARY", "DATA"]


def test_header_echo_is_still_found_across_all_columns():
    cols = _cols(50)
    measure = cols[:5]
    echo = {c: c for c in measure}
    _, excluded = rc.classify_sheet_rows([_row(measure, 5), echo], cols, measure_cols=measure)
    assert _categories(excluded, 2) == ["DATA", "HEADER_REPEAT"]


def test_measure_names_outside_the_columns_are_ignored():
    cols = _cols(40)
    recs = [_row(cols[:30], 30), _row(cols[:30], 2)]
    base = rc.classify_sheet_rows(recs, cols)
    assert rc.classify_sheet_rows(recs, cols, measure_cols=["not_a_column"]) == base


# ── no measure: identical to the old classifier ───────────────────────────────
def _mixed_sheet(n):
    cols = _cols(n)
    recs = []
    for f in (n, n - 1, n // 2, n // 3, 3, 1, 0):
        recs.append(_row(cols, max(0, f)))
    recs.append(_row(cols, 2, text=False))
    total = _row(cols, 0)
    total[cols[0]] = "Grand Total"
    total[cols[-1]] = "999"
    recs.append(total)
    recs.append({c: c for c in cols})
    recs.append({**_row(cols, n // 2, text=False), cols[0]: "None"})
    return cols, recs


@pytest.mark.parametrize("n", [4, 10, 27, 87])
@pytest.mark.parametrize("measure", [None, []])
def test_no_measure_matches_the_old_classifier(n, measure):
    cols, recs = _mixed_sheet(n)
    assert rc.classify_sheet_rows(recs, cols, measure_cols=measure) == \
        _old_classify_sheet_rows(recs, cols)


@pytest.mark.parametrize("n", [10, 87])
def test_full_measure_matches_the_old_classifier(n):
    cols, recs = _mixed_sheet(n)
    assert rc.classify_sheet_rows(recs, cols, measure_cols=list(cols)) == \
        _old_classify_sheet_rows(recs, cols)
