"""ITD running total — the database side of itd.py.

A FEED is tenant + programme + broker (0 when the setup has none) + stream.
Its running total is `bdx_sent_transaction`: one row per transaction the feed
has delivered, with its latest fingerprint and money. Each file's row results
are `bdx_row_comparison` rows.

Two steps, on purpose:

  1. stage()  — when the run lands the file (direct_routes._prepare_run): the
                comparison's results are written as PENDING events. The
                running total is not touched.
  2. apply()  — once the run has produced its output (_render_prepared): the
                pending events move the running total forward and become
                APPLIED.

A run that fails between the two leaves pending events nobody applies, so a
rerun compares against the same running total again — nothing is lost and
nothing is counted twice. apply() re-checks every position it moves (the
fingerprint it expects must still be there), so two files of one feed run at
the same moment cannot both add the same transaction: the second gets
CONFLICT on those rows and the file says so.

The setup's settings are pipeline.itd_config — not mapped on the Pipeline
model, read and written here, the same way rule_scope.py handles
pipeline.rule_scope, so a database without migration 38 keeps working with
ITD off.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import and_, case, func, select, text

import itd

log = logging.getLogger("kavachio.itd")

PENDING = "pending"
APPLIED = "applied"
CONFLICT = "conflict"
CHECK_ONLY = "check_only"
VOID = "void"


# ── the setup's settings ────────────────────────────────────────────────────

def config_column_present(s) -> bool:
    from rule_scope import _column_present
    return _column_present(s, "pipeline", "itd_config")


def tables_present(s) -> bool:
    from rule_scope import _column_present
    return (_column_present(s, SENT_TABLE, "sent_stream")
            and _column_present(s, COMPARISON_TABLE, "comparison_status"))


def read_config(s, pipeline_id: Optional[int]) -> Optional[dict]:
    """A setup's ITD settings, cleaned (itd.normalize_config). None when it has
    none, when there is no such setup, or when the column is not there yet."""
    if not pipeline_id or not config_column_present(s):
        return None
    raw = s.execute(text("SELECT itd_config FROM pipeline WHERE id = :id"),
                    {"id": int(pipeline_id)}).scalar()
    return itd.normalize_config(raw)


def write_config(s, pipeline_id: int, raw: Any) -> Optional[dict]:
    """Store a setup's ITD settings (cleaned; an empty object clears them).
    The caller commits. LookupError when the column is not there, so a choice
    somebody made is refused out loud rather than dropped."""
    if not config_column_present(s):
        raise LookupError("pipeline.itd_config is not available on this database")
    cfg = itd.normalize_config(raw) if raw else None
    dialect = s.get_bind().dialect.name
    cast = "CAST(:v AS JSONB)" if dialect == "postgresql" else ":v"
    s.execute(text(f"UPDATE pipeline SET itd_config = {cast} WHERE id = :id"),
              {"v": json.dumps(cfg) if cfg is not None else None, "id": int(pipeline_id)})
    return cfg


def setup_layout(s, pipe) -> list[tuple[str, list[str]]]:
    """The setup's input sheets and headings (the same source its blank
    bordereau download uses). Empty when the sample is no longer on file."""
    try:
        import bordereau_template as bt
        from db import DirectFormat
        fmt = s.get(DirectFormat, pipe.input_format_id) if pipe.input_format_id else None
        if fmt is None:
            return []
        rec = bt.sample_landing(s, fmt)
        return bt.input_layout((rec.data or {}).get("sheets") if rec else None,
                               fmt.sheet_routing) or []
    except Exception:  # noqa: BLE001 — the layout is a convenience
        log.info("no input layout for setup %s", getattr(pipe, "id", None), exc_info=True)
        return []


def auto_fill(s, pipe, *, refill: bool = False,
              layout: Optional[list] = None) -> Optional[dict]:
    """Switch ITD on for a setup from its own input sheets (itd.auto_config),
    inside the caller's transaction. Returns what is stored afterwards — None
    when the sheets have no transaction id + reported period (ITD stays off).

    For a new setup (no settings yet). refill=True is for a setup whose input
    template was swapped: only settings that are still the automatic ones are
    redone — a person's choice is never overwritten, and a setup that had no
    settings stays as it is. Never raises: ITD must not be the reason a setup
    cannot be saved."""
    try:
        if not (config_column_present(s) and tables_present(s)):
            return None
        current = read_config(s, pipe.id)
        if refill:
            if current is None or not current.get("auto_filled"):
                return current
        elif current is not None:
            return current
        cfg = itd.auto_config(setup_layout(s, pipe) if layout is None else layout)
        if cfg is None and current is None:
            return None
        with s.begin_nested():
            return write_config(s, pipe.id, cfg)
    except Exception:  # noqa: BLE001
        log.warning("ITD settings could not be filled in for setup %s",
                    getattr(pipe, "id", None), exc_info=True)
        return None


# ── tables ──────────────────────────────────────────────────────────────────

# ── the tables' names ───────────────────────────────────────────────────────
# First called bordereau_position (columns position_*) and
# bordereau_position_event (event_*); renamed so they say what they hold.
# rename_old_tables() moves a database that still has the old names across —
# db.init_db runs it before any table is created, and
# migrations/39_itd_table_names.sql does the same by hand.
SENT_TABLE = "bdx_sent_transaction"        # every transaction already sent, once each
COMPARISON_TABLE = "bdx_row_comparison"    # every row of every file, and its result
OLD_TABLES = {"bordereau_position": SENT_TABLE, "bordereau_position_event": COMPARISON_TABLE}
_OLD_INDEXES = {
    "uq_bordereau_position_feed_key": "uq_bdx_sent_transaction_feed_key",
    "ix_bordereau_position_feed": "ix_bdx_sent_transaction_feed",
    "ix_bordereau_position_event_arrival": "ix_bdx_row_comparison_arrival",
    "ix_bordereau_position_event_landing": "ix_bdx_row_comparison_landing",
    "ix_bordereau_position_event_position": "ix_bdx_row_comparison_sent",
    "ix_bordereau_position_event_feed": "ix_bdx_row_comparison_feed",
    "bordereau_position_position_id_seq": "bdx_sent_transaction_sent_id_seq",
    "bordereau_position_event_event_id_seq": "bdx_row_comparison_comparison_id_seq",
}


def renamed_column(table: str, column: str) -> Optional[str]:
    """A column's new name in a renamed table; None when it keeps its name."""
    if table == SENT_TABLE and column.startswith("position_"):
        return "sent_" + column[len("position_"):]
    if table == COMPARISON_TABLE and column.startswith("event_"):
        if column == "event_position_id":
            return "comparison_sent_id"
        return "comparison_" + column[len("event_"):]
    return None


def rename_old_tables(conn) -> list[str]:
    """Rename the two tables, their columns, indexes and sequences on a
    database that still has the old names. The rows stay where they are.
    Safe to run again: what is already renamed is left alone, and a table is
    never renamed onto one that exists. Returns what it renamed."""
    from sqlalchemy import inspect
    done: list[str] = []
    for old, new in OLD_TABLES.items():
        insp = inspect(conn)
        if insp.has_table(old) and not insp.has_table(new):
            conn.exec_driver_sql(f'ALTER TABLE "{old}" RENAME TO "{new}"')
            done.append(f"{old} -> {new}")
            insp = inspect(conn)
        elif insp.has_table(old):
            log.error("both %s and %s exist; %s left as it is — the ITD history is "
                      "split and ITD files are held until it is repaired "
                      "(migrations/40_itd_table_names_repair.sql)", old, new, old)
        if not insp.has_table(new):
            continue
        cols = [c["name"] for c in insp.get_columns(new)]
        for c in cols:
            to = renamed_column(new, c)
            if to and to not in cols:
                conn.exec_driver_sql(f'ALTER TABLE "{new}" RENAME COLUMN "{c}" TO "{to}"')
                done.append(f"{new}.{c} -> {to}")
    if done and conn.dialect.name == "postgresql":
        # Indexes and sequences keep their names through a table rename.
        for old, new in _OLD_INDEXES.items():
            there = conn.exec_driver_sql(
                "SELECT to_regclass(%(o)s) IS NOT NULL AND to_regclass(%(n)s) IS NULL",
                {"o": old, "n": new}).scalar()
            if there:
                kind = "SEQUENCE" if old.endswith("_seq") else "INDEX"
                conn.exec_driver_sql(f'ALTER {kind} "{old}" RENAME TO "{new}"')
    return done


def old_tables_left(bind) -> list[str]:
    """The old ITD table names still in the database — the rename has not
    happened, so the feed's history is not where the app reads it. By
    reflection on the engine, never by a probe in the caller's transaction
    (see rule_scope._column_present). `bind` may be a Session, a Connection
    or an Engine."""
    from sqlalchemy import inspect
    if hasattr(bind, "get_bind"):
        bind = bind.get_bind()
    insp = inspect(bind)
    return [old for old in OLD_TABLES if insp.has_table(old)]


def tables_to_hold_back(conn) -> set[str]:
    """New ITD table names create_all must NOT build: their old table is still
    there and the rename did not run. Building them would put an empty second
    pair beside the history — the split migration 40 had to repair."""
    from sqlalchemy import inspect
    insp = inspect(conn)
    return {new for old, new in OLD_TABLES.items()
            if insp.has_table(old) and not insp.has_table(new)}


def _t(name: str):
    from canonical import CANONICAL_TABLES
    return CANONICAL_TABLES[name]


def _pos():
    return _t(SENT_TABLE)


def _ev():
    return _t(COMPARISON_TABLE)


def feed_of(program_id: Optional[int], broker_party_id: Optional[int]) -> tuple[int, int]:
    return int(program_id or 0), int(broker_party_id or 0)


def load_positions(s, tenant_id: int, program_id: int, broker_id: int,
                   streams: list[str]) -> dict:
    """{(stream, row_key): position} for a feed — what itd.compare reads."""
    if not streams:
        return {}
    p = _pos()
    rows = s.execute(select(p).where(and_(
        p.c.tenant_id == tenant_id,
        p.c.sent_program_id == program_id,
        p.c.sent_broker_party_id == broker_id,
        p.c.sent_stream.in_(streams),
        func.coalesce(p.c.sent_status, "active") != "void",
    ))).mappings().all()
    return {(r["sent_stream"], r["sent_row_key"]): {
        "position_id": r["sent_id"], "hash": r["sent_row_hash"],
        "money": r["sent_money"] or {}, "values": r["sent_values"] or {},
        "status": r["sent_status"] or "active",
        "adjust_count": r["sent_adjust_count"] or 0,
        "key_source": r["sent_key_source"], "source_ref": r["sent_source_ref"],
    } for r in rows}


def last_itd_period(s, tenant_id: int, program_id: int, broker_id: int) -> Optional[str]:
    """The latest 'as at' period of an ITD file this feed has applied."""
    e = _ev()
    return s.execute(select(func.max(e.c.comparison_period)).where(and_(
        e.c.tenant_id == tenant_id,
        e.c.comparison_program_id == program_id,
        e.c.comparison_broker_party_id == broker_id,
        e.c.comparison_load_type == itd.ITD,
        e.c.comparison_status == APPLIED,
    ))).scalar()


# ── step 1: stage ───────────────────────────────────────────────────────────

def stage(s, *, tenant_id: int, program_id: int, broker_id: int,
          arrival_id: Optional[int], landing_id: int, load_type: str,
          period: Optional[str], results: list[dict], check_only: bool) -> int:
    """Write a comparison's results as events. PENDING for a real run;
    CHECK_ONLY for a broker's self-check, which never moves the running total.
    The caller commits. Returns how many events were written."""
    e = _ev()
    status = CHECK_ONLY if check_only else PENDING
    now = datetime.utcnow()
    rows = []
    for r in results:
        rows.append({
            "tenant_id": tenant_id, "comparison_program_id": program_id,
            "comparison_broker_party_id": broker_id, "comparison_file_arrival_id": arrival_id,
            "comparison_landing_id": landing_id, "comparison_sent_id": r.get("position_id"),
            "comparison_stream": r.get("stream"), "comparison_sheet": r.get("sheet"),
            "comparison_row_no": r.get("row_no"), "comparison_row_key": r.get("row_key"),
            "comparison_source_ref": r.get("source_ref"), "comparison_result": r["result"],
            "comparison_load_type": load_type, "comparison_status": status,
            "comparison_period": period, "comparison_old_hash": r.get("old_hash"),
            "comparison_new_hash": r.get("new_hash"), "comparison_old_money": r.get("old_money"),
            "comparison_new_money": r.get("new_money"), "comparison_money_delta": r.get("delta"),
            "comparison_changed_fields": r.get("changed_fields"),
            "comparison_adjust_no": r.get("adjust_no"),
            "comparison_reverses_key": r.get("reverses_key"),
            "comparison_reverses_ref": r.get("reverses_ref"),
            "comparison_old_values": r.get("old_values"),
            "comparison_new_values": r.get("new_values"),
            "comparison_row_period": r.get("row_period"),
            "comparison_note": _note(r),
            "created_at": now, "modified_at": now,
        })
    if rows:
        s.execute(e.insert(), rows)
    return len(rows)


def _missing_key_note(r: dict) -> str:
    """'Skipped: no Risksmith Invoice Number, and Risksmith Policy Number is
    blank.' — names the setup's own columns that left the row without an id."""
    keys = r.get("blank_key_columns") or []
    fallback = r.get("blank_fallback_columns") or []
    if not keys and not fallback:
        return "Skipped: no transaction id, and the backup columns are not all filled in."
    parts = []
    if keys:
        parts.append("no " + " / ".join(keys))
    if fallback:
        parts.append(", ".join(fallback) + (" is" if len(fallback) == 1 else " are") + " blank")
    return "Skipped: " + ", and ".join(parts) + "."


# Notes saved before "Held back" was renamed "Skipped" — read in today's words.
_OLD_NOTES = {
    "Held back: the transaction id and the fallback columns are empty.":
        "Skipped: no transaction id, and the backup columns are not all filled in.",
    "Held back: the same transaction appears earlier in this file.":
        "Skipped: this transaction is already earlier in the file.",
}


def _note(r: dict) -> Optional[str]:
    res = r["result"]
    if res == itd.MISSING_KEY:
        return _missing_key_note(r)
    if res == itd.DUPLICATE:
        if r.get("duplicate_of_sheet"):
            return (f"Also on sheet {r['duplicate_of_sheet']} of this file — counted "
                    f"once, from there.")
        return "Skipped: this transaction is already earlier in the file."
    if res == itd.DROPPED:
        return "In earlier files, missing from this one — a person decides."
    if res == itd.REVERSAL:
        if r.get("reverses_key"):
            return f"Reversal of {r.get('reverses_ref')}."
        return (f"Reversal of {r.get('reverses_ref')}, which is neither in this "
                f"file nor delivered before.")
    if res == itd.NEW and r.get("identical_rows"):
        n = int(r["identical_rows"])
        if n == 2:
            return ("Identical to 1 other row in this file (same policy, type, date, "
                    "period and amounts) — please confirm both are real transactions.")
        return (f"Identical to {n - 1} other rows in this file (same policy, type, "
                f"date, period and amounts) — please confirm all {n} are real "
                f"transactions.")
    return None


# ── step 2: apply ───────────────────────────────────────────────────────────

def apply(s, landing_id: int, *, arrival_id: Optional[int] = None,
          on_changed: str = "adjust") -> dict:
    """Move the running total forward from a run's pending events. Idempotent:
    a run whose events are already applied applies nothing. The caller
    commits. Returns {"applied": n, "conflicts": n}."""
    p, e = _pos(), _ev()
    pending = s.execute(select(e).where(and_(
        e.c.comparison_landing_id == landing_id, e.c.comparison_status == PENDING))
        .order_by(e.c.comparison_id)).mappings().all()
    applied = conflicts = 0
    now = datetime.utcnow()
    for ev in pending:
        ok, position_id = _apply_one(s, p, ev, arrival_id=arrival_id,
                                     on_changed=on_changed, now=now)
        values = {"comparison_status": APPLIED if ok else CONFLICT, "modified_at": now}
        if position_id and not ev["comparison_sent_id"]:
            values["comparison_sent_id"] = position_id
        if not ok:
            values["comparison_note"] = ("Another file for this feed changed this "
                                         "transaction while this one was running — "
                                         "run this file again.")
            conflicts += 1
        else:
            applied += 1
        s.execute(e.update().where(e.c.comparison_id == ev["comparison_id"]).values(**values))
    return {"applied": applied, "conflicts": conflicts}


def _find(s, p, ev) -> Optional[dict]:
    return s.execute(select(p).where(and_(
        p.c.tenant_id == ev["tenant_id"],
        p.c.sent_program_id == ev["comparison_program_id"],
        p.c.sent_broker_party_id == ev["comparison_broker_party_id"],
        p.c.sent_stream == ev["comparison_stream"],
        p.c.sent_row_key == ev["comparison_row_key"],
        func.coalesce(p.c.sent_status, "active").notin_(("void", "removed")),
    ))).mappings().first()


def _apply_one(s, p, ev, *, arrival_id, on_changed, now) -> tuple[bool, Optional[int]]:
    res = ev["comparison_result"]
    if res in (itd.MISSING_KEY, itd.DUPLICATE):
        return True, None                       # nothing to move
    cur = _find(s, p, ev)
    seen = {"sent_last_seen_at": now, "sent_last_period": ev["comparison_period"],
            "sent_last_arrival_id": arrival_id, "modified_at": now}
    if res in itd.SENT_AS_IS:
        if cur is not None:
            # Already there: this very run applied before (a retry), or another
            # file of the feed added it meanwhile.
            same = cur["sent_row_hash"] == ev["comparison_new_hash"]
            return same, cur["sent_id"]
        pid = s.execute(p.insert().values(
            tenant_id=ev["tenant_id"], sent_program_id=ev["comparison_program_id"],
            sent_broker_party_id=ev["comparison_broker_party_id"],
            sent_stream=ev["comparison_stream"], sent_row_key=ev["comparison_row_key"],
            sent_row_hash=ev["comparison_new_hash"],
            sent_key_source=("txn" if (ev["comparison_row_key"] or "").startswith("txn:")
                                 else "composite"),
            sent_source_ref=ev["comparison_source_ref"],
            sent_money=ev["comparison_new_money"] or {},
            sent_values=ev["comparison_new_values"] or {},
            sent_adjust_count=0, sent_status="active",
            sent_reverses_key=ev.get("comparison_reverses_key"),
            sent_first_arrival_id=arrival_id, created_at=now, **seen,
        ).returning(p.c.sent_id)).scalar()
        return True, pid
    if cur is None:
        return False, None
    if res == itd.UNCHANGED:
        if cur["sent_row_hash"] != ev["comparison_new_hash"]:
            return False, cur["sent_id"]
        s.execute(p.update().where(p.c.sent_id == cur["sent_id"])
                  .values(sent_status="active", **seen))
        return True, cur["sent_id"]
    if res in (itd.CHANGED, itd.DETAILS_CHANGED):
        if cur["sent_row_hash"] != ev["comparison_old_hash"]:
            return False, cur["sent_id"]
        upd = {"sent_row_hash": ev["comparison_new_hash"],
               "sent_money": ev["comparison_new_money"] or {},
               "sent_values": ev["comparison_new_values"] or {},
               "sent_status": "active", **seen}
        if (res == itd.CHANGED and ev["comparison_load_type"] == itd.ITD
                and on_changed == "adjust" and ev["comparison_adjust_no"]):
            upd["sent_adjust_count"] = ev["comparison_adjust_no"]
        s.execute(p.update().where(p.c.sent_id == cur["sent_id"]).values(**upd))
        return True, cur["sent_id"]
    if res == itd.DROPPED:
        if cur["sent_row_hash"] != ev["comparison_old_hash"]:
            return False, cur["sent_id"]
        if (cur["sent_status"] or "active") == "active":
            s.execute(p.update().where(p.c.sent_id == cur["sent_id"])
                      .values(sent_status="dropped", modified_at=now))
        return True, cur["sent_id"]
    return True, cur["sent_id"]


# ── withdrawing a file ──────────────────────────────────────────────────────

class CannotWithdraw(Exception):
    pass


def withdraw(s, arrival_id: int) -> int:
    """Undo what a file did to its feed's running total — for a file that is
    discarded after it ran. Only the LATEST file to touch those transactions
    can be withdrawn: a later file has already been compared against this
    one's values. The caller commits. Returns how many events were undone."""
    p, e = _pos(), _ev()
    mine = s.execute(select(e).where(and_(
        e.c.comparison_file_arrival_id == arrival_id, e.c.comparison_status == APPLIED))
        .order_by(e.c.comparison_id.desc())).mappings().all()
    if not mine:
        # Nothing moved yet — but results still waiting must never be applied
        # later (itd_run.apply_completed_pending would) for a withdrawn file.
        s.execute(e.update().where(and_(e.c.comparison_file_arrival_id == arrival_id,
                                        e.c.comparison_status == PENDING))
                  .values(comparison_status=VOID, modified_at=datetime.utcnow()))
        return 0
    first_id = min(ev["comparison_id"] for ev in mine)
    touched = {ev["comparison_sent_id"] for ev in mine if ev["comparison_sent_id"]}
    if touched:
        later = s.execute(select(func.count()).select_from(e).where(and_(
            e.c.comparison_sent_id.in_(touched), e.c.comparison_id > first_id,
            e.c.comparison_status == APPLIED,
            func.coalesce(e.c.comparison_file_arrival_id, 0) != arrival_id,
        ))).scalar()
        if later:
            raise CannotWithdraw(
                "A later file for this feed has already been compared against this "
                "one, so withdrawing it would leave the running total wrong. "
                "Withdraw the later file first.")
    now = datetime.utcnow()
    for ev in mine:
        pid, res = ev["comparison_sent_id"], ev["comparison_result"]
        if not pid:
            continue
        if res in itd.SENT_AS_IS:
            s.execute(p.update().where(p.c.sent_id == pid)
                      .values(sent_status="void", modified_at=now))
        elif res in (itd.CHANGED, itd.DETAILS_CHANGED):
            upd = {"sent_row_hash": ev["comparison_old_hash"],
                   "sent_money": ev["comparison_old_money"] or {},
                   "sent_values": ev["comparison_old_values"] or {},
                   "modified_at": now}
            if res == itd.CHANGED and ev["comparison_adjust_no"]:
                upd["sent_adjust_count"] = int(ev["comparison_adjust_no"]) - 1
            s.execute(p.update().where(p.c.sent_id == pid).values(**upd))
        elif res == itd.DROPPED:
            s.execute(p.update().where(and_(p.c.sent_id == pid,
                                            p.c.sent_status == "dropped"))
                      .values(sent_status="active", modified_at=now))
    s.execute(e.update().where(and_(e.c.comparison_file_arrival_id == arrival_id,
                                    e.c.comparison_status.in_((APPLIED, PENDING))))
              .values(comparison_status=VOID, modified_at=now))
    return len(mine)


def has_effect(s, arrival_id: int) -> bool:
    """Has this file moved (or is it waiting to move) its feed's running total?"""
    e = _ev()
    return bool(s.execute(select(func.count()).select_from(e).where(and_(
        e.c.comparison_file_arrival_id == arrival_id,
        e.c.comparison_status.in_((APPLIED, PENDING))))).scalar())


# ── reading, for the screens ────────────────────────────────────────────────

_ROW_FIELDS = tuple("comparison_" + f for f in (
    "id", "sheet", "row_no", "source_ref", "row_key", "result", "status", "load_type",
    "period", "old_money", "new_money", "money_delta", "changed_fields", "adjust_no",
    "note", "sent_id", "stream", "reverses_key", "reverses_ref", "row_period"))


def file_rows(s, tenant_id: int, *, arrival_id: Optional[int] = None,
              landing_id: Optional[int] = None, result: Optional[str] = None,
              cls: Optional[str] = None, limit: int = 200, offset: int = 0) -> dict:
    """A file's row-by-row comparison, newest run first. `result` narrows to
    one result, `cls` to one of the client's classes (itd.CLASSES). Every row
    carries its class; `class_counts` counts the file's rows by class, and
    `month_counts` the rows in the file by the month they are reported in:
    "current" (the month the file is as at), "earlier" (a month the file
    repeats), "unknown" (no month read — a file compared before the month
    was recorded)."""
    e = _ev()
    cond = [e.c.tenant_id == tenant_id]
    if arrival_id is not None:
        cond.append(e.c.comparison_file_arrival_id == arrival_id)
    if landing_id is not None:
        cond.append(e.c.comparison_landing_id == landing_id)
    if arrival_id is not None and landing_id is None:
        # The latest run of the file only — a rerun has its own landing.
        last = s.execute(select(func.max(e.c.comparison_landing_id)).where(and_(*cond))).scalar()
        if last is None:
            return {"rows": [], "total": 0, "counts": {},
                    "class_counts": {c: 0 for c in itd.CLASSES},
                    "month_counts": {"current": 0, "earlier": 0, "unknown": 0}}
        cond.append(e.c.comparison_landing_id == last)
    counts = dict(s.execute(select(e.c.comparison_result, func.count())
                            .where(and_(*cond)).group_by(e.c.comparison_result)).all())
    class_counts = {c: sum(counts.get(r, 0) for r in itd.results_in(c)) for c in itd.CLASSES}
    month = case((e.c.comparison_row_period.is_(None), "unknown"),
                 (e.c.comparison_row_period == e.c.comparison_period, "current"),
                 else_="earlier").label("month")
    month_counts = {"current": 0, "earlier": 0, "unknown": 0}
    month_counts.update(dict(s.execute(
        select(month, func.count()).where(and_(*cond, e.c.comparison_result != itd.DROPPED))
        .group_by(month)).all()))
    if result:
        cond.append(e.c.comparison_result == result)
    if cls:
        cond.append(e.c.comparison_result.in_(itd.results_in(cls)))
    total = s.execute(select(func.count()).select_from(e).where(and_(*cond))).scalar()
    rows = s.execute(select(*[e.c[f] for f in _ROW_FIELDS]).where(and_(*cond))
                     .order_by(e.c.comparison_sheet, e.c.comparison_row_no, e.c.comparison_id)
                     .limit(max(1, min(int(limit), 1000))).offset(max(0, int(offset)))
                     ).mappings().all()
    out = [{k.replace("comparison_", "", 1): v for k, v in r.items()} for r in rows]
    for r in out:
        # The screens' name for the sent transaction a row belongs to.
        r["position_id"] = r.pop("sent_id", None)
        r["class"] = itd.CLASS_OF.get(r["result"])
        r["note"] = _OLD_NOTES.get(r.get("note"), r.get("note"))
    return {"rows": out, "total": total, "counts": counts, "class_counts": class_counts,
            "month_counts": month_counts}


def feed_positions(s, tenant_id: int, program_id: int, broker_id: int,
                   status: Optional[str] = None, limit: int = 200,
                   offset: int = 0) -> dict:
    """A feed's running total, for the screens."""
    p = _pos()
    cond = [p.c.tenant_id == tenant_id, p.c.sent_program_id == program_id,
            p.c.sent_broker_party_id == broker_id]
    counts = dict(s.execute(select(func.coalesce(p.c.sent_status, "active"), func.count())
                            .where(and_(*cond))
                            .group_by(func.coalesce(p.c.sent_status, "active"))).all())
    if status:
        cond.append(func.coalesce(p.c.sent_status, "active") == status)
    total = s.execute(select(func.count()).select_from(p).where(and_(*cond))).scalar()
    rows = s.execute(select(
        p.c.sent_id, p.c.sent_stream, p.c.sent_source_ref,
        p.c.sent_status, p.c.sent_money, p.c.sent_adjust_count,
        p.c.sent_first_arrival_id, p.c.sent_last_arrival_id,
        p.c.sent_last_period, p.c.sent_last_seen_at,
    ).where(and_(*cond)).order_by(p.c.sent_stream, p.c.sent_source_ref)
        .limit(max(1, min(int(limit), 1000))).offset(max(0, int(offset)))).mappings().all()
    return {"rows": [{k.replace("sent_", "", 1): v for k, v in r.items()} for r in rows],
            "total": total, "counts": counts}


DROPPED_DECISIONS = {
    # The broker took it out on purpose: it stops being listed as dropped and
    # a later ITD carrying it again is a NEW transaction.
    "removed": "removed",
    # Still a live transaction (the broker left it off by mistake): back to
    # active, so the next ITD that carries it compares as usual.
    "keep": "active",
}


def decide_dropped(s, tenant_id: int, position_id: int, decision: str,
                   who: Optional[str] = None) -> dict:
    """A person's decision on a transaction an ITD stopped carrying. The
    caller commits. Raises LookupError / ValueError for the routes to word."""
    target = DROPPED_DECISIONS.get((decision or "").strip().lower())
    if target is None:
        raise ValueError("decision must be 'removed' or 'keep'")
    p, e = _pos(), _ev()
    cur = s.execute(select(p).where(and_(p.c.sent_id == position_id,
                                         p.c.tenant_id == tenant_id))).mappings().first()
    if cur is None:
        raise LookupError("no such transaction")
    if (cur["sent_status"] or "active") != "dropped":
        raise ValueError("this transaction is not waiting for a decision")
    now = datetime.utcnow()
    s.execute(p.update().where(p.c.sent_id == position_id)
              .values(sent_status=target, modified_at=now))
    s.execute(e.insert().values(
        tenant_id=tenant_id, comparison_sent_id=position_id,
        comparison_program_id=cur["sent_program_id"],
        comparison_broker_party_id=cur["sent_broker_party_id"],
        comparison_stream=cur["sent_stream"], comparison_row_key=cur["sent_row_key"],
        comparison_source_ref=cur["sent_source_ref"], comparison_result=itd.DROPPED,
        comparison_status="dropped_decided",
        comparison_note=f"Decision: {decision} ({who or 'unknown'}).",
        created_at=now, modified_at=now))
    return {"position_id": position_id, "status": target}
