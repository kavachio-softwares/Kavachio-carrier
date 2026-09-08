"""
contract_asof.py
────────────────
Feature 7 — Prior Period Files. Answers ONE question:

    "Which contract version was in force on <date>?"

§7.1 resolves each transaction to the version in force on its transaction date;
§7.2 routes late and corrected files to that version rather than the current one.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
─────────────────────────────────────────
It never filters on `status_ops` or `is_current_version`. That looks like an
omission and is the entire point: the version governing a 2024 transaction is,
in 2026, a SUPERSEDED row — status 'active' and is_current_version TRUE both
belong to the version in force TODAY, which is the one §7.2 forbids using.
A `WHERE status_ops = 'active'` here would return the exact opposite of what is
wanted, and would do it silently.

The word "active" is overloaded, so this module avoids it entirely and says
"in force on <date>" everywhere instead.

SAFETY
──────
Everything here is fail-open. Missing columns (migration 20_1 not applied),
missing dates, or no version covering the date all return None, and every caller
treats None as "fall back to the behaviour that existed before this module".
Resolution is additionally gated behind CONTRACT_ASOF_ENABLED, which defaults
OFF — so importing and calling this module changes nothing until it is switched
on deliberately.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any, NamedTuple, Optional

from sqlalchemy import text


# ── Feature gate ────────────────────────────────────────────────────────────
# Default OFF. With a single contract live, as-of resolution and "latest wins"
# return the same row, so switching it on is provably a no-op — but that is a
# claim to be VERIFIED (scripts/verify_contract_asof.py --compare) rather than
# assumed, and a flag is what makes verifying it possible.
def asof_enabled() -> bool:
    return os.getenv("CONTRACT_ASOF_ENABLED", "").strip().lower() in (
        "1", "true", "on", "yes",
    )


class Lineage(NamedTuple):
    """The three columns that identify one logical contract across its versions.

    Contracts separated by any of these are DIFFERENT contracts that may
    legitimately be in force at the same time (Schedule A and Schedule B both
    live, two brokers on one programme). Contracts sharing all three are
    versions of the SAME contract and must never overlap in time — that is the
    invariant 20_2 enforces.
    """
    program_id: Optional[int]
    schedule_key: Optional[str]
    broker_party_id: Optional[int]


# Null-safe equality. `IS NOT DISTINCT FROM` is the natural spelling but is
# PostgreSQL-only, and the test suite runs on SQLite; this form is portable and
# means the same thing. It matters because schedule_key is NULL on every legacy
# contract, i.e. NULL is the common case rather than an edge case.
def _null_safe_eq(col: str, param: str) -> str:
    return f"({col} = :{param} OR ({col} IS NULL AND :{param} IS NULL))"


_LINEAGE_WHERE = " AND ".join((
    _null_safe_eq("contract_program_id", "pid"),
    _null_safe_eq("schedule_key", "sk"),
    _null_safe_eq("contract_broker_party_id", "bk"),
))


# Every column this module reads. schedule_key and contract_broker_party_id are
# included deliberately: they come from earlier migrations that a deployment can
# have skipped, and a query naming a column that does not exist is exactly the
# failure this check exists to avoid.
_REQUIRED_COLUMNS = frozenset((
    "contract_effective_from", "contract_effective_to", "contract_version_label",
    "schedule_key", "contract_broker_party_id", "contract_program_id",
))


def _columns_present(conn) -> bool:
    """True when migration 20_1 has been applied.

    Uses SQLAlchemy reflection rather than probing with a SELECT. That is not a
    style preference — it is the whole point of this function:

    In PostgreSQL a failed statement aborts the ENTIRE transaction, and catching
    the exception in Python does not heal it. Every subsequent statement fails
    with InFailedSqlTransaction. So a probe of the form "SELECT the new column
    and see if it raises" poisons the caller's transaction on the one path it
    was written to handle — a database without the migration — and takes the
    caller's own work down with it. Reflection asks the catalog instead and
    cannot fail that way.

    Cached on the engine: resolution runs per transaction row, and a bordereau
    has a lot of rows.
    """
    # Callers hand us either a Connection (the contract-upload path) or a
    # Session (the validation paths). A Session exposes neither .engine nor a
    # reflectable inspector, so resolve its bind before asking — without this
    # the feature silently reports "columns absent" on every validation call and
    # as-of resolution never fires.
    bind = conn
    if not hasattr(bind, "engine") and hasattr(bind, "get_bind"):
        try:
            bind = bind.get_bind()
        except Exception:
            pass
    engine = getattr(bind, "engine", bind if hasattr(bind, "dialect") else None)
    cached = getattr(engine, "_kv_asof_columns", None) if engine is not None else None
    if cached is not None:
        return cached
    try:
        from sqlalchemy import inspect as _sa_inspect
        cols = {c["name"] for c in _sa_inspect(bind).get_columns("contract")}
        present = _REQUIRED_COLUMNS <= cols
    except Exception:
        present = False
    if engine is not None:
        try:
            engine._kv_asof_columns = present
        except Exception:
            pass
    return present


def lineage_of(conn, contract_id: int) -> Optional[Lineage]:
    """The lineage a known contract belongs to — the entry point for callers
    that have a contract_id (from a template binding, or from the file's
    original upload) and need its whole version timeline.

    Guarded by _columns_present because it names schedule_key, which predates
    this feature and may be absent."""
    if not _columns_present(conn):
        return None
    row = conn.execute(
        text("SELECT contract_program_id, schedule_key, contract_broker_party_id "
             "FROM contract WHERE contract_id = :cid"),
        {"cid": contract_id},
    ).first()
    return Lineage(row[0], row[1], row[2]) if row else None


def resolve_as_of(conn, lineage: Lineage, on_date: date) -> Optional[int]:
    """§7.1 — the contract_id of the version in force on `on_date`, or None.

    None means NO version covers that date: a transaction dated before the
    contract incepted, or falling in a gap between versions. The caller MUST
    route that row to exceptions. It must NOT fall back to the current version,
    which would be the silent wrong-rules answer §7 exists to prevent — so this
    returns None rather than a best guess, and the distinction between "no
    version" and "the current version" stays visible to the caller.

    No ORDER BY and no LIMIT: constraint contract_no_overlap (migration 20_2)
    guarantees at most one version matches. If this ever raises MultipleResults,
    that constraint is missing or was installed over pre-existing overlaps —
    which is a data problem to fix, not a query to loosen.
    """
    if on_date is None or not _columns_present(conn):
        return None
    return conn.execute(
        text(f"""
            SELECT contract_id
            FROM   contract
            WHERE  {_LINEAGE_WHERE}
              AND  contract_effective_from IS NOT NULL
              AND  contract_effective_from <= :d
              AND  (contract_effective_to IS NULL OR contract_effective_to > :d)
        """),
        {"pid": lineage.program_id, "sk": lineage.schedule_key,
         "bk": lineage.broker_party_id, "d": on_date},
    ).scalar()


def resolve_sibling_as_of(conn, contract_id: int, on_date: date) -> Optional[int]:
    """The version of `contract_id`'s own contract that was in force on `on_date`.

    The shape every call site actually wants: it already holds whichever version
    the old "latest wins" lookup produced, and needs the one that governs this
    row's date instead. Returns None when as-of resolution cannot answer, so the
    caller keeps the contract it already had.
    """
    if not asof_enabled() or on_date is None:
        return None
    lineage = lineage_of(conn, contract_id)
    if lineage is None:
        return None
    return resolve_as_of(conn, lineage, on_date)


def timeline(conn, lineage: Lineage) -> list[dict[str, Any]]:
    """Every version of one contract, oldest first — for the verification
    script and for the UI badge that has to tell an operator WHICH version
    produced the exceptions they are looking at."""
    if not _columns_present(conn):
        return []
    rows = conn.execute(
        text(f"""
            SELECT contract_id, contract_version_label, filename,
                   contract_effective_from, contract_effective_to,
                   status_ops, is_current_version
            FROM   contract
            WHERE  {_LINEAGE_WHERE}
            ORDER BY contract_effective_from NULLS FIRST, contract_id
        """),
        {"pid": lineage.program_id, "sk": lineage.schedule_key,
         "bk": lineage.broker_party_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def stamp_effective_dates(conn, contract_id: int, lineage: Lineage,
                          effective_from: date, effective_to: Optional[date],
                          version_label: Optional[str] = None) -> bool:
    """Record a newly inserted contract's business-effective term, and close the
    version it supersedes AT THAT SAME DATE.

    THE SECOND HALF IS THE POINT. db_persister already retires the prior version
    with `valid_until = now()`, which says "we stopped believing this today".
    That is system time and it is not what §7.1 asks. Closing the prior version
    at `effective_from` says "this stopped applying on 01-Apr-2024" — business
    time — and it is what makes a BACKDATED endorsement land correctly: uploaded
    in June, effective from April, and from that moment a 15-May transaction
    resolves to the new version even though the old one was the current version
    on 15-May.

    Called inside the caller's transaction and returns False rather than raising
    when the columns are absent, so a contract upload can never fail because
    effective dating is unavailable.
    """
    if not _columns_present(conn) or effective_from is None:
        return False

    # Close the open-ended predecessor(s) in the SAME lineage. Scoped to
    # contract_id <> the new row so re-running cannot close the row it just
    # opened, and to open-ended rows only so already-closed history is immutable.
    conn.execute(
        text(f"""
            UPDATE contract
            SET    contract_effective_to = :eff_from
            WHERE  {_LINEAGE_WHERE}
              AND  contract_id <> :cid
              AND  contract_effective_from IS NOT NULL
              AND  contract_effective_from < :eff_from
              AND  contract_effective_to IS NULL
        """),
        {"pid": lineage.program_id, "sk": lineage.schedule_key,
         "bk": lineage.broker_party_id, "cid": contract_id, "eff_from": effective_from},
    )

    conn.execute(
        text("""
            UPDATE contract
            SET    contract_effective_from = :eff_from,
                   contract_effective_to   = :eff_to,
                   contract_version_label  = COALESCE(:label, contract_version_label)
            WHERE  contract_id = :cid
        """),
        {"cid": contract_id, "eff_from": effective_from,
         "eff_to": effective_to, "label": version_label},
    )
    return True


def find_overlaps(conn) -> list[dict[str, Any]]:
    """Pairs of versions of the same contract whose date ranges intersect.

    Run before installing 20_2 — each pair is a date on which "the version in
    force" has two answers, so §7.1 is unsatisfiable for that period until a
    human decides which one is right.
    """
    if not _columns_present(conn):
        return []
    rows = conn.execute(text("""
        SELECT a.contract_id AS a_id, a.filename AS a_file,
               a.contract_effective_from AS a_from, a.contract_effective_to AS a_to,
               b.contract_id AS b_id, b.filename AS b_file,
               b.contract_effective_from AS b_from, b.contract_effective_to AS b_to,
               a.contract_program_id AS program_id
        FROM   contract a
        JOIN   contract b
          ON   a.contract_id < b.contract_id
         AND   a.contract_program_id = b.contract_program_id
         AND   (a.schedule_key = b.schedule_key
                OR (a.schedule_key IS NULL AND b.schedule_key IS NULL))
         AND   (a.contract_broker_party_id = b.contract_broker_party_id
                OR (a.contract_broker_party_id IS NULL
                    AND b.contract_broker_party_id IS NULL))
        WHERE  a.contract_effective_from IS NOT NULL
          AND  b.contract_effective_from IS NOT NULL
          AND  a.contract_effective_from < COALESCE(b.contract_effective_to, '2999-12-31')
          AND  b.contract_effective_from < COALESCE(a.contract_effective_to, '2999-12-31')
        ORDER BY a.contract_program_id, a.contract_effective_from
    """)).mappings().all()
    return [dict(r) for r in rows]


# ==========================================================================
# Per-row scoping — §7.1 "resolve EACH transaction"
# ==========================================================================

class AsofCfg:
    """Detached snapshot of one config source (pipeline or setup).

    The pipeline/setup rows are read inside `with SessionLocal()`, and the
    per-row filter runs after that session closes — touching them there risks a
    DetachedInstanceError. Copying the two fields the config layer reads keeps
    the later code independent of session lifetime.
    """
    __slots__ = ("governing_date_field", "asof_config")

    def __init__(self, src):
        self.governing_date_field = getattr(src, "governing_date_field", None)
        self.asof_config = getattr(src, "asof_config", None)


def contract_windows(conn, contract_ids):
    """{contract_id: (effective_from, effective_to)} for the versions in play.

    effective_to is EXCLUSIVE and may be None (open-ended), matching
    contract_asof.resolve_as_of — the two must agree or a row could be admitted
    by one and rejected by the other.
    """
    ids = [int(c) for c in contract_ids if c]
    if not ids:
        return {}
    rows = conn.execute(text(
        "SELECT contract_id, contract_effective_from, contract_effective_to "
        "FROM contract WHERE contract_id = ANY(:ids)"), {"ids": ids}).all()
    return {r[0]: (r[1], r[2]) for r in rows if r[1] is not None}


def row_dates_from_blocks(blocks, *cfg_sources):
    """{(sheet, __rowid): date} — the governing date of every validated row.

    Keyed on the DuckDB `__rowid`, which is the 1-based position of the record
    within its sheet's block. Exceptions carry that same id, so this maps an
    exception back to the date that decides which contract version governs it.
    Built from `blocks` (what the engine actually validated) rather than from
    the landing record, so the indices cannot drift apart.
    """
    from contract_upload_services import contract_asof_config as _cfg
    out = {}
    for blk in blocks or []:
        sheet = blk.get("sheet")
        records = blk.get("records") or []
        if not records:
            continue
        field = _cfg.governing_date_field(list(records[0].keys()), *cfg_sources)
        if not field:
            continue
        for i, rec in enumerate(records, start=1):     # 1-based, like __rowid
            d = _cfg.coerce_date(rec.get(field))
            if d is not None:
                out[(sheet, i)] = d
    return out


def filter_exceptions_by_window(exceptions, row_dates, windows):
    """§7.1 "resolve EACH TRANSACTION" — drop every exception raised by a
    contract version that did not govern that row's date.

    Why filter here rather than scope each rule's SQL: the engine already runs
    several governing contracts over the whole file and merges their exceptions
    (that is how per-schedule contracts work). Feeding it every version in the
    lineage therefore needs no compiler change — each version's rules simply
    also fire on rows they do not govern, and those are the ones removed here.
    The result is per-row resolution with the run structure untouched.

    Only exceptions from contracts in `windows` are considered. Anything else —
    global rules, type checks, contracts with no effective dating — is left
    alone, so this can only ever remove a violation of a rule that provably did
    not apply to that row.
    """
    if not windows or not row_dates:
        return exceptions, 0
    kept, dropped = [], 0
    for exc in exceptions or []:
        cid = exc.get("contract_id")
        win = windows.get(int(cid)) if cid else None
        if win is None:
            kept.append(exc)
            continue
        d = row_dates.get((exc.get("sheet"), exc.get("row")))
        if d is None:
            # No governing date for this row — keep it. Silently dropping an
            # exception because a date failed to parse would hide a real breach.
            kept.append(exc)
            continue
        start, end = win
        if start <= d and (end is None or d < end):
            kept.append(exc)
        else:
            dropped += 1
    return kept, dropped
