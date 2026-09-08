"""Feature 7 — the configurable layer.

Covers the two things that decide a run's contract: WHICH column carries the
governing date, and HOW a file's many dates reduce to one.
"""
from __future__ import annotations

from datetime import date

import pytest

from contract_upload_services import contract_asof_config as cfg


class _Src:
    """Stands in for a Pipeline / DirectFormat row."""
    def __init__(self, field=None, conf=None):
        self.governing_date_field = field
        self.asof_config = conf


MOCKRISK_COLS = ["PolicyNumber", "InsuredName", "PolicyTermBeginDate",
                 "PolicyTermEndDate", "OccurrenceLimit"]


# ── which column governs ────────────────────────────────────────────────────

def test_explicit_config_beats_autodetection():
    """A configured column wins even when a 'better' candidate is present —
    the operator has said which date the contract actually keys on."""
    assert cfg.governing_date_field(
        MOCKRISK_COLS, _Src("PolicyTermEndDate")) == "PolicyTermEndDate"


def test_pipeline_overrides_setup():
    """Sources are consulted most-specific first: the pipeline is the thing
    that runs, so it wins over the setup it runs under."""
    assert cfg.governing_date_field(
        MOCKRISK_COLS, _Src("PolicyTermEndDate"), _Src("PolicyTermBeginDate")
    ) == "PolicyTermEndDate"


def test_autodetects_policy_inception_on_the_real_mockrisk_columns():
    assert cfg.governing_date_field(MOCKRISK_COLS) == "PolicyTermBeginDate"


def test_inception_is_preferred_over_transaction_date():
    """For a premium bordereau the contract in force when the risk ATTACHED
    governs it for its whole term — so inception outranks transaction date,
    even though both are present and both are plausible."""
    assert cfg.governing_date_field(
        ["TransactionDate", "TransactionEffectiveDate", "PolicyInception"]
    ) == "PolicyInception"


def test_matching_ignores_case_and_separators():
    assert cfg.governing_date_field(["policy term begin date"]) == "policy term begin date"
    assert cfg.governing_date_field(["POLICY_INCEPTION"]) == "POLICY_INCEPTION"


def test_no_date_column_returns_none_so_the_caller_keeps_its_pin():
    assert cfg.governing_date_field(["PolicyNumber", "Premium"]) is None


# ── which date governs ──────────────────────────────────────────────────────

SPAN = ["2025-10-01T00:00:00", "2025-11-25T00:00:00", "2026-06-01T00:00:00"]


def test_min_is_the_default_strategy():
    """A file straddling a version boundary resolves to the OLDER version, so
    no row is ever judged by rules that did not exist when it was written."""
    assert cfg.pick_date(SPAN) == date(2025, 10, 1)


@pytest.mark.parametrize("strategy,expected", [
    ("min",  date(2025, 10, 1)),
    ("max",  date(2026, 6, 1)),
    ("mode", date(2025, 10, 1)),
])
def test_strategies(strategy, expected):
    assert cfg.pick_date(SPAN, strategy) == expected


def test_mode_picks_the_most_common_not_the_earliest():
    vals = ["2026-01-01", "2025-05-05", "2026-01-01"]
    assert cfg.pick_date(vals, "mode") == date(2026, 1, 1)


def test_unparseable_cells_are_ignored_not_fatal():
    """One bad cell must not decide a run, nor block one."""
    assert cfg.pick_date(["not a date", "", None, "2025-10-01"]) == date(2025, 10, 1)


def test_no_parseable_dates_returns_none():
    assert cfg.pick_date(["n/a", "", None]) is None


@pytest.mark.parametrize("raw,expected", [
    ("2025-10-01T00:00:00", date(2025, 10, 1)),
    ("2025-10-01", date(2025, 10, 1)),
    ("01/10/2025", date(2025, 10, 1)),
    ("2025-10-01T00:00:00Z", date(2025, 10, 1)),
])
def test_coerce_date_formats(raw, expected):
    assert cfg.coerce_date(raw) == expected


# ── per-setup enable/disable ────────────────────────────────────────────────

def test_setup_config_can_disable_for_one_programme(monkeypatch):
    """Globally on, off for this setup — for a programme whose dates are known
    to be unreliable, without touching the others."""
    monkeypatch.setenv("CONTRACT_ASOF_ENABLED", "true")
    assert cfg.enabled(_Src(conf={"enabled": False})) is False


def test_setup_config_can_enable_while_global_is_off(monkeypatch):
    """And the reverse — pilot one programme before switching everyone on."""
    monkeypatch.delenv("CONTRACT_ASOF_ENABLED", raising=False)
    assert cfg.enabled(_Src(conf={"enabled": True})) is True


def test_falls_back_to_the_global_flag(monkeypatch):
    monkeypatch.setenv("CONTRACT_ASOF_ENABLED", "true")
    assert cfg.enabled(_Src(), _Src()) is True


def test_on_unresolved_default_is_pin():
    """Default keeps the pinned contract when no version covers the date —
    'skip' is more correct per §7 but changes output, so it is opt-in."""
    assert cfg.setting("on_unresolved", cfg.ON_UNRESOLVED, _Src()) == "pin"
    assert cfg.setting("on_unresolved", cfg.ON_UNRESOLVED,
                       _Src(conf={"on_unresolved": "skip"})) == "skip"


# ── per-row scoping (§7.1 "resolve EACH transaction") ───────────────────────
# The 4-row MIXED fixture: 2 rows in v1's window ($2M), 2 in v2's ($4M).
# Designed so the three possible behaviours give three different counts —
#   per-row = 2, min-strategy = 3, max-strategy = 1 —
# so a test cannot pass by accident.

from datetime import date as _date
from contract_upload_services import contract_asof as _dr

WINDOWS = {213: (_date(2025, 2, 15), _date(2026, 2, 15)),      # $2M
           214: (_date(2026, 2, 15), _date(2027, 2, 15))}      # $4M

MIXED_ROWS = [
    {"PolicyNumber": "TEST-2025-A", "PolicyTermBeginDate": "2025-10-01T00:00:00", "OccurrenceLimit": 3_000_000},
    {"PolicyNumber": "TEST-2025-B", "PolicyTermBeginDate": "2025-11-01T00:00:00", "OccurrenceLimit": 1_000_000},
    {"PolicyNumber": "TEST-2026-C", "PolicyTermBeginDate": "2026-06-01T00:00:00", "OccurrenceLimit": 3_000_000},
    {"PolicyNumber": "TEST-2026-D", "PolicyTermBeginDate": "2026-07-01T00:00:00", "OccurrenceLimit": 5_000_000},
]
BLOCKS = [{"sheet": "Sheet1", "records": MIXED_ROWS}]


def _raw_exceptions():
    """What the engine yields with BOTH versions' rules run over ALL rows."""
    out = []
    for cid, cap in ((213, 2_000_000), (214, 4_000_000)):
        for i, r in enumerate(MIXED_ROWS, start=1):
            if r["OccurrenceLimit"] > cap:
                out.append({"sheet": "Sheet1", "row": i, "contract_id": cid,
                            "policy_number": r["PolicyNumber"]})
    return out


def test_row_dates_are_keyed_by_duckdb_rowid():
    """__rowid is 1-based within the sheet's block — exceptions carry that same
    id, so an off-by-one here would silently mis-attribute every row."""
    rd = _dr.row_dates_from_blocks(BLOCKS)
    assert rd == {("Sheet1", 1): _date(2025, 10, 1), ("Sheet1", 2): _date(2025, 11, 1),
                  ("Sheet1", 3): _date(2026, 6, 1),  ("Sheet1", 4): _date(2026, 7, 1)}


def test_per_row_scoping_keeps_exactly_the_governing_exceptions():
    kept, dropped = _dr.filter_exceptions_by_window(
        _raw_exceptions(), _dr.row_dates_from_blocks(BLOCKS), WINDOWS)
    assert [(e["policy_number"], e["contract_id"]) for e in kept] == [
        ("TEST-2025-A", 213),   # 3M > 2M, and 213 governs Oct-2025
        ("TEST-2026-D", 214),   # 5M > 4M, and 214 governs Jul-2026
    ]
    assert dropped == 2


def test_it_drops_the_over_flag_a_single_contract_run_would_produce():
    """TEST-2026-C (3M) breaches v1's $2M but not v2's $4M, and v2 governs it.
    Resolving the whole run to v1 — what min-strategy does — flags it wrongly.
    This is the row the fix exists for."""
    kept, _ = _dr.filter_exceptions_by_window(
        _raw_exceptions(), _dr.row_dates_from_blocks(BLOCKS), WINDOWS)
    assert not [e for e in kept if e["policy_number"] == "TEST-2026-C"]


def test_it_drops_the_duplicate_when_both_versions_flag_one_row():
    """TEST-2026-D breaches both caps, so both versions raise it. Only the one
    that governs the row survives — otherwise every straddling file would
    double-count."""
    kept, _ = _dr.filter_exceptions_by_window(
        _raw_exceptions(), _dr.row_dates_from_blocks(BLOCKS), WINDOWS)
    d = [e for e in kept if e["policy_number"] == "TEST-2026-D"]
    assert len(d) == 1 and d[0]["contract_id"] == 214


def test_the_three_behaviours_give_three_different_counts():
    """Guards the fixture itself: if per-row, min and max ever agree, this
    suite would pass without proving anything."""
    per_row = len(_dr.filter_exceptions_by_window(
        _raw_exceptions(), _dr.row_dates_from_blocks(BLOCKS), WINDOWS)[0])
    as_min = sum(1 for r in MIXED_ROWS if r["OccurrenceLimit"] > 2_000_000)
    as_max = sum(1 for r in MIXED_ROWS if r["OccurrenceLimit"] > 4_000_000)
    assert (per_row, as_min, as_max) == (2, 3, 1)


def test_exceptions_from_undated_contracts_are_never_touched():
    """Global rules and contracts with no effective dating must pass through —
    the filter may only remove a rule that provably did not apply."""
    exc = [{"sheet": "Sheet1", "row": 1, "contract_id": None},
           {"sheet": "Sheet1", "row": 3, "contract_id": 999}]   # not in windows
    kept, dropped = _dr.filter_exceptions_by_window(
        exc, _dr.row_dates_from_blocks(BLOCKS), WINDOWS)
    assert len(kept) == 2 and dropped == 0


def test_a_row_with_no_parseable_date_keeps_its_exceptions():
    """Dropping an exception because a date failed to parse would hide a real
    breach, so the unknown case fails safe by keeping it."""
    exc = [{"sheet": "Sheet1", "row": 99, "contract_id": 213}]  # no date for row 99
    kept, dropped = _dr.filter_exceptions_by_window(
        exc, _dr.row_dates_from_blocks(BLOCKS), WINDOWS)
    assert len(kept) == 1 and dropped == 0


def test_no_windows_means_no_filtering():
    exc = _raw_exceptions()
    kept, dropped = _dr.filter_exceptions_by_window(exc, _dr.row_dates_from_blocks(BLOCKS), {})
    assert kept == exc and dropped == 0


def test_open_ended_window_admits_later_dates():
    kept, _ = _dr.filter_exceptions_by_window(
        [{"sheet": "Sheet1", "row": 4, "contract_id": 214}],
        _dr.row_dates_from_blocks(BLOCKS), {214: (_date(2026, 2, 15), None)})
    assert len(kept) == 1
