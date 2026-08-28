"""Group 3 — DB glue for the submission calendar.

Keeps the DB-touching logic out of the pure `submission_calendar` module: it feeds
that module the resolved inputs (frequency from Program.bdx_frequency, anchor from
the contract or a manual override) and persists / reads the ExpectedSubmission rows.

Reused by the schedule/calendar endpoints now, and by the daily late-flip job later.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Optional

from db import (
    Program, Contract, SubmissionSchedule, ExpectedSubmission, ActivityEvent,
)
from submission_calendar import (
    resolve_schedule, generate_expected, derive_status, ResolvedSchedule, _add_months,
    DEFAULT_DUE_OFFSET_DAYS, DEFAULT_GRACE_DAYS, DEFAULT_SOON_WINDOW_DAYS,
)

# Keys we look for inside Contract.extracted for a contract inception/effective date.
_INCEPTION_KEYS = (
    "inception_dt", "contract_inception_dt", "inception_date",
    "effective_date", "effective_dt", "inception",
)


def _parse_date(v) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def _contract_inception(contract: Optional[Contract]) -> Optional[date]:
    """Best-effort inception date from the AI-extracted contract metadata. The app
    Contract has no dedicated column, so we dig the usual keys out of `extracted`."""
    if contract is None:
        return None
    extracted = contract.extracted or {}
    if isinstance(extracted, dict):
        for key in _INCEPTION_KEYS:
            d = _parse_date(extracted.get(key))
            if d:
                return d
    return None


def _find_contract(session, schedule: SubmissionSchedule,
                   program: Optional[Program]) -> Optional[Contract]:
    if schedule.contract_id:
        return session.get(Contract, schedule.contract_id)
    if program is None:
        return None
    return (session.query(Contract)
            .filter(Contract.program_id == program.id)
            .order_by(Contract.id.desc()).first())


def program_contract_basis(session, program_id: int):
    """The contract-derived defaults for a program: (frequency, inception_date).
    Lets the UI know whether the schedule would resolve from the contract alone —
    so 'Save' can be enabled even before the user sets any override."""
    p = session.get(Program, program_id)
    if p is None:
        return (None, None)
    contract = (session.query(Contract)
                .filter(Contract.program_id == program_id)
                .order_by(Contract.id.desc()).first())
    return (p.bdx_frequency, _contract_inception(contract))


def resolve_for_schedule(session, schedule: SubmissionSchedule):
    """Resolve (frequency, anchor) for a schedule: manual override → contract → None.
    Returns (ResolvedSchedule|None, program, contract)."""
    program = session.get(Program, schedule.program_id)
    contract = _find_contract(session, schedule, program)
    resolved = resolve_schedule(
        frequency_override=schedule.frequency_override,
        anchor_override=schedule.anchor_date_override,
        contract_frequency=(program.bdx_frequency if program else None),
        contract_inception=_contract_inception(contract),
        due_offset_days=schedule.due_offset_days if schedule.due_offset_days is not None
            else DEFAULT_DUE_OFFSET_DAYS,
        grace_days=schedule.grace_days if schedule.grace_days is not None
            else DEFAULT_GRACE_DAYS,
        soon_window_days=schedule.soon_window_days if schedule.soon_window_days is not None
            else DEFAULT_SOON_WINDOW_DAYS,
    )
    return resolved, program, contract


def materialize_schedule(session, schedule: SubmissionSchedule,
                         today: Optional[date] = None, horizon_months: int = 12) -> dict:
    """Resolve → generate → upsert ExpectedSubmission rows for this program.

    Idempotent: existing rows are updated in place (their received_at / received_export_id
    are preserved), so re-running never loses a "received" mark. Produces nothing when
    the schedule is unresolved. Caller commits.
    """
    today = today or datetime.utcnow().date()
    resolved, program, contract = resolve_for_schedule(session, schedule)
    if resolved is None:
        return {"resolved": False, "count": 0,
                "reason": _unresolved_reason(schedule, program, contract)}

    end_y, end_m = _add_months(today.year, today.month, horizon_months)
    end = date(end_y, end_m, monthrange(end_y, end_m)[1])
    rows = generate_expected(resolved, resolved.anchor, end)

    existing = {e.period: e for e in session.query(ExpectedSubmission)
                .filter(ExpectedSubmission.program_id == schedule.program_id).all()}
    for r in rows:
        e = existing.get(r["period"])
        if e is None:
            e = ExpectedSubmission(
                tenant_id=schedule.tenant_id, program_id=schedule.program_id,
                period=r["period"])
            session.add(e)
        e.schedule_id = schedule.id
        e.period_start = r["period_start"]
        e.period_end = r["period_end"]
        e.due_date = r["due_date"]
        e.status = derive_status(r["due_date"], today, resolved.grace_days,
                                 resolved.soon_window_days, e.received_at)
    session.flush()
    return {"resolved": True, "count": len(rows),
            "frequency": resolved.frequency, "anchor": resolved.anchor.isoformat()}


def _unresolved_reason(schedule, program, contract) -> str:
    freq = schedule.frequency_override or (program.bdx_frequency if program else None)
    anchor = schedule.anchor_date_override or _contract_inception(contract)
    if not freq and not anchor:
        return "need_frequency_and_start"
    if not freq:
        return "need_frequency"
    return "need_start_date"


def mark_received(session, program_id: int, received_on: Optional[date] = None,
                  export_id: Optional[int] = None, period: Optional[str] = None) -> Optional[dict]:
    """Mark an expected submission as received when a BDX is produced for a program.

    Which period it satisfies: the explicit `period` if given (e.g. from a BDX
    reporting-period column), otherwise the **oldest still-unreceived period that
    has already ended** — the outstanding obligation a broker is catching up on.
    Idempotent-ish: never overwrites a period that's already marked received.

    Returns the matched row summary, or None when there's no open period to satisfy
    (no calendar for this program, or nothing due yet). Never raises for "no match".
    """
    received_on = received_on or datetime.utcnow().date()

    if period is not None:
        target = (session.query(ExpectedSubmission)
                  .filter(ExpectedSubmission.program_id == program_id,
                          ExpectedSubmission.period == period).first())
    else:
        target = (session.query(ExpectedSubmission)
                  .filter(ExpectedSubmission.program_id == program_id,
                          ExpectedSubmission.received_at.is_(None),
                          ExpectedSubmission.period_end <= received_on)
                  .order_by(ExpectedSubmission.period_end.asc()).first())
    if target is None:
        return None
    if target.received_at is not None and period is None:
        return None   # already satisfied — don't double-count

    target.received_at = received_on
    target.received_export_id = export_id
    target.status = derive_status(target.due_date, received_on,
                                  received_on=received_on)
    session.flush()
    return {"period": target.period, "status": target.status,
            "due_date": target.due_date.isoformat(),
            "received_at": received_on.isoformat()}


def sweep_overdue(session, today: Optional[date] = None,
                  tenant_id: Optional[int] = None) -> dict:
    """Flip unreceived expected submissions past their grace window to 'late' and
    raise ONE in-app reminder (ActivityEvent → the bell) per newly-late period.

    Idempotent: rows already marked 'late' are skipped, so the reminder fires once,
    not on every sweep. Self-directed only — it reminds the broker's own team, never
    contacts or escalates to the carrier. Safe to run daily (a cron/ping) or lazily
    when the calendar is viewed. Caller commits.
    """
    today = today or datetime.utcnow().date()
    sched_q = session.query(SubmissionSchedule)
    if tenant_id is not None:
        sched_q = sched_q.filter(SubmissionSchedule.tenant_id == tenant_id)
    schedules = {s.program_id: s for s in sched_q.all()}

    # All unreceived rows; the per-row flags (overdue_notified / due_soon_notified)
    # make each reminder fire exactly once, so scanning already-notified rows is
    # cheap and safe.
    q = session.query(ExpectedSubmission).filter(
        ExpectedSubmission.received_at.is_(None))
    if tenant_id is not None:
        q = q.filter(ExpectedSubmission.tenant_id == tenant_id)

    newly, newly_soon = [], []
    for e in q.all():
        sch = schedules.get(e.program_id)
        grace = sch.grace_days if sch and sch.grace_days is not None else DEFAULT_GRACE_DAYS
        soon = sch.soon_window_days if sch and sch.soon_window_days is not None else DEFAULT_SOON_WINDOW_DAYS
        if today > e.due_date + timedelta(days=grace):
            # Past grace → late. One overdue reminder.
            if not e.overdue_notified:
                e.status = "late"
                e.overdue_notified = True
                session.add(ActivityEvent(
                    tenant_id=e.tenant_id, actor="system",
                    action="submission_overdue",
                    target=f"program:{e.program_id}",
                    details={"program_id": e.program_id, "period": e.period,
                             "due_date": e.due_date.isoformat()}))
                newly.append({"program_id": e.program_id, "period": e.period,
                              "due_date": e.due_date.isoformat()})
        elif e.due_date - timedelta(days=soon) <= today <= e.due_date:
            # C-9: inside the soon window and not yet due → remind ONCE, BEFORE late.
            if not e.due_soon_notified:
                e.due_soon_notified = True
                session.add(ActivityEvent(
                    tenant_id=e.tenant_id, actor="system",
                    action="submission_due_soon",
                    target=f"program:{e.program_id}",
                    details={"program_id": e.program_id, "period": e.period,
                             "due_date": e.due_date.isoformat()}))
                newly_soon.append({"program_id": e.program_id, "period": e.period,
                                   "due_date": e.due_date.isoformat()})
    session.flush()
    return {"newly_late": len(newly), "rows": newly,
            "newly_due_soon": len(newly_soon), "due_soon": newly_soon}


def calendar_rows(session, tenant_id: Optional[int], today: Optional[date] = None,
                  program_id: Optional[int] = None) -> list[dict]:
    """The calendar for a tenant (optionally one program). Status is recomputed
    fresh against `today` using each program's schedule knobs, so the displayed
    state is always current without waiting on the daily job."""
    today = today or datetime.utcnow().date()
    schedules = {s.program_id: s for s in session.query(SubmissionSchedule)
                 .filter(SubmissionSchedule.tenant_id == tenant_id).all()}
    q = session.query(ExpectedSubmission).filter(ExpectedSubmission.tenant_id == tenant_id)
    if program_id is not None:
        q = q.filter(ExpectedSubmission.program_id == program_id)

    out = []
    for e in q.order_by(ExpectedSubmission.due_date.asc()).all():
        sch = schedules.get(e.program_id)
        grace = sch.grace_days if sch and sch.grace_days is not None else DEFAULT_GRACE_DAYS
        soon = sch.soon_window_days if sch and sch.soon_window_days is not None else DEFAULT_SOON_WINDOW_DAYS
        status = derive_status(e.due_date, today, grace, soon, e.received_at)
        out.append({
            "id": e.id,
            "program_id": e.program_id,
            "period": e.period,
            "period_start": e.period_start.isoformat() if e.period_start else None,
            "period_end": e.period_end.isoformat() if e.period_end else None,
            "due_date": e.due_date.isoformat() if e.due_date else None,
            "status": status,
            "received_at": e.received_at.isoformat() if e.received_at else None,
            "received_export_id": e.received_export_id,
        })
    return out
