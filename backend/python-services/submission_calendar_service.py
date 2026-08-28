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

from sqlalchemy import and_, or_

from db import (
    Program, Contract, SubmissionSchedule, ExpectedSubmission, ActivityEvent,
)
from submission_calendar import (
    resolve_schedule, generate_expected, derive_status, ResolvedSchedule, _add_months,
    DEFAULT_DUE_OFFSET_DAYS, DEFAULT_SOON_WINDOW_DAYS,
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


def _unwrap(v):
    """Unwrap one extracted field.

    Contract extraction stores every metadata field as an envelope —
    {"value": ..., "source_text": ..., "page": ..., "confidence": ...} — so the
    date is never the value itself. Passing the envelope straight to
    _parse_date() yields None for a field that is actually populated.
    """
    if isinstance(v, dict) and "value" in v:
        return v.get("value")
    return v


def _contract_inception(contract: Optional[Contract]) -> Optional[date]:
    """The contract's inception date — the anchor a calendar is built from (C-5).

    Reads the real `inception_dt` column first; the extraction payload is only a
    fallback for rows written before that column was populated.

    In the payload the program fields sit one level down, under
    "program_metadata", and each is wrapped in the {value, source_text, page,
    confidence} envelope. Both have to be unwound: looking only at the top level
    of `extracted` finds nothing even when the date is present and confident,
    which is why this used to return None for every contract in the database.
    """
    if contract is None:
        return None
    d = _parse_date(getattr(contract, "inception_dt", None))
    if d:
        return d
    extracted = contract.extracted or {}
    if not isinstance(extracted, dict):
        return None
    scopes = [extracted]
    nested = extracted.get("program_metadata")
    if isinstance(nested, dict):
        scopes.append(nested)
    for scope in scopes:
        for key in _INCEPTION_KEYS:
            d = _parse_date(_unwrap(scope.get(key)))
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
        # No default here: NULL must stay NULL so due_date_for falls back to the
        # offset, which is how a schedule saved before this field existed keeps
        # producing exactly the dates it always did.
        due_day_of_month=schedule.due_day_of_month,
        due_offset_days=schedule.due_offset_days if schedule.due_offset_days is not None
            else DEFAULT_DUE_OFFSET_DAYS,
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
        e.status = derive_status(r["due_date"], today,
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
    """Raise the three deadline reminders for unreceived expected submissions.

    One reminder per moment, each fired at most once per period (ActivityEvent →
    the bell):

      * due_soon  — some days ahead, so there is still time to act;
      * due_today — on the due date itself;
      * overdue   — the date has passed and nothing arrived.

    There is no grace period: the day after the due date the row is overdue. See
    submission_calendar.derive_status.

    A row can legitimately ring more than one bell over its life, and a sweep
    that first sees a row long after its due date rings only `overdue` — the
    earlier moments have passed and warning about them now would be noise. Each
    bell is guarded by its own *_notified flag rather than by `status` (which is
    recomputed on every read), so a re-materialize never re-rings anything.

    Self-directed only — it reminds the broker's own team, never contacts or
    escalates to the carrier. Safe to run daily (a cron/ping) or lazily when the
    calendar is viewed. Caller commits.
    """
    today = today or datetime.utcnow().date()
    sched_q = session.query(SubmissionSchedule)
    if tenant_id is not None:
        sched_q = sched_q.filter(SubmissionSchedule.tenant_id == tenant_id)
    schedules = {s.program_id: s for s in sched_q.all()}

    # All unreceived rows; the per-row flags make each reminder fire exactly
    # once, so scanning already-notified rows is cheap and safe.
    q = session.query(ExpectedSubmission).filter(
        ExpectedSubmission.received_at.is_(None))
    if tenant_id is not None:
        q = q.filter(ExpectedSubmission.tenant_id == tenant_id)

    newly, newly_today, newly_soon = [], [], []

    def ring(e, action: str, bucket: list):
        session.add(ActivityEvent(
            tenant_id=e.tenant_id, actor="system", action=action,
            target=f"program:{e.program_id}",
            details={"program_id": e.program_id, "period": e.period,
                     "due_date": e.due_date.isoformat()}))
        bucket.append({"program_id": e.program_id, "period": e.period,
                       "due_date": e.due_date.isoformat()})

    for e in q.all():
        sch = schedules.get(e.program_id)
        soon = sch.soon_window_days if sch and sch.soon_window_days is not None else DEFAULT_SOON_WINDOW_DAYS
        if today > e.due_date:
            if not e.overdue_notified:
                e.status = "overdue"
                e.overdue_notified = True
                ring(e, "submission_overdue", newly)
        elif today == e.due_date:
            if not e.due_today_notified:
                e.status = "due_today"
                e.due_today_notified = True
                ring(e, "submission_due_today", newly_today)
        elif e.due_date - timedelta(days=soon) <= today:
            # Inside the warning window and not yet due → remind ONCE, early.
            if not e.due_soon_notified:
                e.due_soon_notified = True
                ring(e, "submission_due_soon", newly_soon)
    session.flush()
    # `newly_late` keeps its name: the scheduler logs it and the tests read it.
    return {"newly_late": len(newly), "rows": newly,
            "newly_due_today": len(newly_today), "due_today": newly_today,
            "newly_due_soon": len(newly_soon), "due_soon": newly_soon}


def pending_email_rows(session, tenant_id: Optional[int] = None) -> list[dict]:
    """Periods whose bell has fired but whose EMAIL has not been sent yet.

    Read-only on purpose. `sweep_overdue()` is called from three places, one of
    them a calendar PAGE VIEW, so sending mail from inside it would mail the
    admins every time somebody looks at the calendar. Instead the sweep only
    ever sets flags, and the SCHEDULER asks this question afterwards and decides
    to send — which also keeps sweep_overdue free of I/O and easy to test.

    Because the *_emailed flags are separate from the *_notified ones, it does
    not matter which sweep rang the bell: the next scheduled run still finds the
    row here. Returns one dict per (period, kind), newest deadline last, with the
    program name resolved for the digest.
    """
    q = (session.query(ExpectedSubmission, Program.name)
         .outerjoin(Program, Program.id == ExpectedSubmission.program_id)
         .filter(or_(
             and_(ExpectedSubmission.overdue_notified.is_(True),
                  ExpectedSubmission.overdue_emailed.isnot(True)),
             and_(ExpectedSubmission.due_today_notified.is_(True),
                  ExpectedSubmission.due_today_emailed.isnot(True)),
             and_(ExpectedSubmission.due_soon_notified.is_(True),
                  ExpectedSubmission.due_soon_emailed.isnot(True)))))
    if tenant_id is not None:
        q = q.filter(ExpectedSubmission.tenant_id == tenant_id)

    out: list[dict] = []
    for e, program_name in q.order_by(ExpectedSubmission.due_date.asc()).all():
        # A row can legitimately owe SEVERAL mails — it passed through the
        # warning window, then its due date, then went overdue before any
        # scheduled run. Emit one entry per kind so the digest reports (and
        # clears) each independently.
        for kind, notified, emailed in (
            ("overdue", e.overdue_notified, e.overdue_emailed),
            ("due_today", e.due_today_notified, e.due_today_emailed),
            ("due_soon", e.due_soon_notified, e.due_soon_emailed),
        ):
            if notified and not emailed:
                out.append({
                    "id": e.id, "kind": kind,
                    "tenant_id": e.tenant_id, "program_id": e.program_id,
                    "program_name": program_name or f"Program {e.program_id}",
                    "period": e.period,
                    "due_date": e.due_date.isoformat() if e.due_date else None,
                })
    return out


def mark_emailed(session, rows: list[dict]) -> int:
    """Flip the *_emailed flag for rows whose digest was sent successfully.

    Called ONLY after a send returns without raising, so a mail outage leaves the
    flags untouched and the next scheduled run retries instead of silently
    swallowing the reminder. Caller commits.
    """
    by_id: dict[int, set] = {}
    for r in rows:
        by_id.setdefault(r["id"], set()).add(r["kind"])
    if not by_id:
        return 0
    for e in (session.query(ExpectedSubmission)
              .filter(ExpectedSubmission.id.in_(by_id)).all()):
        kinds = by_id.get(e.id) or set()
        if "overdue" in kinds:
            e.overdue_emailed = True
        if "due_today" in kinds:
            e.due_today_emailed = True
        if "due_soon" in kinds:
            e.due_soon_emailed = True
    session.flush()
    return len(by_id)


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
        soon = sch.soon_window_days if sch and sch.soon_window_days is not None else DEFAULT_SOON_WINDOW_DAYS
        status = derive_status(e.due_date, today, soon, e.received_at)
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
