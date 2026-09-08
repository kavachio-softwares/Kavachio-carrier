"""Feature 7 — Prior Period Files.

Self-contained: builds a tiny in-memory `contract` table, so it runs without a
database and without touching any other test's fixtures.

The scenario throughout is one contract amended twice:

    v1  01-Jan-2024 → 01-Apr-2024   'Original'
    v2  01-Apr-2024 → 01-Jan-2025   'Endt 001'
    v3  01-Jan-2025 → open          'Renewal'   ← the CURRENT version

and the question §7 asks is: what governs a transaction dated 12-Mar-2024,
submitted in 2026?
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, text

from contract_upload_services import contract_asof as asof


DDL = """
CREATE TABLE contract (
    contract_id              INTEGER PRIMARY KEY,
    contract_program_id      INTEGER,
    schedule_key             TEXT,
    contract_broker_party_id INTEGER,
    filename                 TEXT,
    status_ops               TEXT,
    is_current_version       BOOLEAN,
    contract_effective_from  DATE,
    contract_effective_to    DATE,
    contract_version_label   TEXT
)
"""

# id, program, schedule, broker, file, status, current, from, to, label
ROWS = [
    (1, 7, None, None, "original.pdf", "superseded", False, "2024-01-01", "2024-04-01", "Original"),
    (2, 7, None, None, "endt001.pdf",  "superseded", False, "2024-04-01", "2025-01-01", "Endt 001"),
    (3, 7, None, None, "renewal.pdf",  "active",     True,  "2025-01-01", None,         "Renewal"),
]

LIN = asof.Lineage(7, None, None)


@pytest.fixture()
def conn():
    engine = create_engine("sqlite://")
    with engine.connect() as c:
        c.execute(text(DDL))
        for r in ROWS:
            c.execute(text(
                "INSERT INTO contract VALUES (:a,:b,:c,:d,:e,:f,:g,:h,:i,:j)"),
                dict(zip("abcdefghij", r)))
        yield c


# ── §7.1 resolve to the version in force on the transaction date ────────────

def test_prior_period_txn_resolves_to_the_old_version_not_the_current_one(conn):
    """The headline requirement. A March-2024 transaction gets v1, even though
    v3 is the version flagged current."""
    assert asof.resolve_as_of(conn, LIN, date(2024, 3, 12)) == 1


def test_resolver_ignores_status_and_is_current_version(conn):
    """The row it returns is 'superseded' and is_current_version=False.

    Guards the module's central trap: any WHERE on status_ops='active' or
    is_current_version=TRUE reintroduces exactly the bug §7.2 forbids, and would
    still pass a test that only asserted 'some contract came back'."""
    cid = asof.resolve_as_of(conn, LIN, date(2024, 3, 12))
    row = conn.execute(text("SELECT status_ops, is_current_version FROM contract "
                            "WHERE contract_id = :c"), {"c": cid}).first()
    assert row[0] == "superseded"
    assert not row[1]


@pytest.mark.parametrize("txn,expected", [
    (date(2024, 1, 1),  1),   # first day of v1
    (date(2024, 3, 31), 1),   # last day of v1
    (date(2024, 4, 1),  2),   # v2 starts — boundary belongs to the NEW version
    (date(2024, 12, 31), 2),  # last day of v2
    (date(2025, 1, 1),  3),   # v3 starts
    (date(2030, 6, 1),  3),   # open-ended upper bound
])
def test_boundaries_are_half_open(conn, txn, expected):
    """[from, to) — each boundary date matches exactly one version.

    Closed-closed ranges would make 01-Apr-2024 match both v1 and v2, and the
    resolver would then have to pick one arbitrarily."""
    assert asof.resolve_as_of(conn, LIN, txn) == expected


def test_date_before_inception_returns_none_not_the_current_version(conn):
    """No version covers 2023. The caller must route the row to exceptions —
    so the resolver must not 'helpfully' fall back to v3."""
    assert asof.resolve_as_of(conn, LIN, date(2023, 6, 1)) is None


def test_gap_between_versions_returns_none(conn):
    conn.execute(text("UPDATE contract SET contract_effective_to = '2024-03-01' "
                      "WHERE contract_id = 1"))
    assert asof.resolve_as_of(conn, LIN, date(2024, 3, 15)) is None


# ── lineage: what is and is not the same contract ───────────────────────────

def test_a_different_schedule_is_a_different_contract(conn):
    """Schedule B may be in force at the same time as Schedule A without either
    being a version of the other — this is the legitimate 'multiple active
    contracts' case, and it must not leak into A's timeline."""
    conn.execute(text(
        "INSERT INTO contract VALUES (4, 7, 'Schedule B', NULL, 'b.pdf', "
        "'active', 1, '2024-01-01', NULL, 'B Original')"))
    assert asof.resolve_as_of(conn, LIN, date(2024, 3, 12)) == 1
    assert asof.resolve_as_of(conn, asof.Lineage(7, "Schedule B", None),
                              date(2024, 3, 12)) == 4


def test_null_schedule_key_matches_null_not_everything(conn):
    """schedule_key is NULL on every legacy contract, so null-safe equality is
    the common path rather than an edge case."""
    assert asof.lineage_of(conn, 1) == LIN


# ── §7.2 routing, via the call-site wrapper ─────────────────────────────────

def test_sibling_lookup_is_a_noop_while_the_flag_is_off(conn, monkeypatch):
    """Deploy safety: with CONTRACT_ASOF_ENABLED unset, nothing changes."""
    monkeypatch.delenv("CONTRACT_ASOF_ENABLED", raising=False)
    assert asof.resolve_sibling_as_of(conn, 3, date(2024, 3, 12)) is None


def test_sibling_lookup_swaps_current_for_in_force_when_enabled(conn, monkeypatch):
    """§7.2 — a late/corrected file holding the CURRENT contract (3) is routed
    to the version that applied on the transaction date (1)."""
    monkeypatch.setenv("CONTRACT_ASOF_ENABLED", "true")
    assert asof.resolve_sibling_as_of(conn, 3, date(2024, 3, 12)) == 1


def test_correction_reprocessed_later_lands_on_the_same_version(conn, monkeypatch):
    """The acceptance criterion: the answer depends on the TRANSACTION date, not
    on when the file is processed. Reprocessing in 2026 what was processed in
    2024 gives the same contract version, so results reproduce."""
    monkeypatch.setenv("CONTRACT_ASOF_ENABLED", "true")
    txn = date(2024, 3, 12)
    assert asof.resolve_sibling_as_of(conn, 1, txn) == \
           asof.resolve_sibling_as_of(conn, 3, txn) == 1


# ── stamping: closing the predecessor on BUSINESS time ──────────────────────

def test_stamping_closes_the_predecessor_at_the_new_effective_date(conn):
    """A new version effective 01-Jul-2025 must close v3 on 01-Jul-2025 — not
    on today's date, which is what `valid_until = now()` records."""
    conn.execute(text(
        "INSERT INTO contract (contract_id, contract_program_id, filename) "
        "VALUES (5, 7, 'endt002.pdf')"))
    asof.stamp_effective_dates(conn, 5, LIN, date(2025, 7, 1), None, "Endt 002")

    closed = conn.execute(text("SELECT contract_effective_to FROM contract "
                               "WHERE contract_id = 3")).scalar()
    assert str(closed) == "2025-07-01"
    assert asof.resolve_as_of(conn, LIN, date(2025, 6, 30)) == 3
    assert asof.resolve_as_of(conn, LIN, date(2025, 7, 1)) == 5


def test_backdated_endorsement_rewrites_history_for_past_dates(conn):
    """Uploaded today, effective from 01-Jun-2025. From that moment a
    15-Jun-2025 transaction resolves to the new version — even though v3 was the
    current version throughout June. This is the case a system-time
    (valid_from/valid_until) lookup gets wrong, and the reason Feature 7 needed
    its own pair of columns."""
    conn.execute(text(
        "INSERT INTO contract (contract_id, contract_program_id, filename) "
        "VALUES (6, 7, 'backdated.pdf')"))
    assert asof.resolve_as_of(conn, LIN, date(2025, 6, 15)) == 3
    asof.stamp_effective_dates(conn, 6, LIN, date(2025, 6, 1), None, "Endt 002")
    assert asof.resolve_as_of(conn, LIN, date(2025, 6, 15)) == 6


def test_stamping_never_reopens_already_closed_history(conn):
    """v1 and v2 are already closed. A new version must not disturb them —
    closed history is immutable, or old periods stop reproducing."""
    conn.execute(text(
        "INSERT INTO contract (contract_id, contract_program_id, filename) "
        "VALUES (7, 7, 'new.pdf')"))
    asof.stamp_effective_dates(conn, 7, LIN, date(2025, 7, 1), None, "Endt 002")
    assert str(conn.execute(text("SELECT contract_effective_to FROM contract "
                                 "WHERE contract_id = 1")).scalar()) == "2024-04-01"
    assert str(conn.execute(text("SELECT contract_effective_to FROM contract "
                                 "WHERE contract_id = 2")).scalar()) == "2025-01-01"


# ── overlap detection ───────────────────────────────────────────────────────

def test_find_overlaps_is_clean_on_a_well_formed_timeline(conn):
    assert asof.find_overlaps(conn) == []


def test_find_overlaps_catches_two_versions_claiming_one_date(conn):
    conn.execute(text("UPDATE contract SET contract_effective_to = '2024-06-01' "
                      "WHERE contract_id = 1"))
    overlaps = asof.find_overlaps(conn)
    assert len(overlaps) == 1
    assert {overlaps[0]["a_id"], overlaps[0]["b_id"]} == {1, 2}


# ── fail-open ───────────────────────────────────────────────────────────────

def test_missing_columns_degrade_to_none_rather_than_raising():
    """Migration 20_1 not applied: every entry point returns empty and the
    callers keep their pre-Feature-7 behaviour."""
    engine = create_engine("sqlite://")
    with engine.connect() as c:
        c.execute(text("CREATE TABLE contract (contract_id INTEGER PRIMARY KEY, "
                       "contract_program_id INTEGER, schedule_key TEXT, "
                       "contract_broker_party_id INTEGER)"))
        assert asof.resolve_as_of(c, LIN, date(2024, 3, 12)) is None
        assert asof.timeline(c, LIN) == []
        assert asof.find_overlaps(c) == []
        assert asof.stamp_effective_dates(c, 1, LIN, date(2024, 1, 1), None) is False


def test_none_date_resolves_to_none(conn):
    assert asof.resolve_as_of(conn, LIN, None) is None
