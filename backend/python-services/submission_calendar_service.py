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
    SubmissionVersion, ProgramBroker, Party, AppUser,
)
from submission_calendar import (
    resolve_schedule, generate_expected, derive_status, ResolvedSchedule, _add_months,
    period_for_date, parse_period_hint, horizon_months_for,
    DEFAULT_DUE_OFFSET_DAYS, DEFAULT_SOON_WINDOW_DAYS,
)

# Keys we look for inside Contract.extracted for a contract inception/effective date.
_INCEPTION_KEYS = (
    "inception_dt", "contract_inception_dt", "inception_date",
    "effective_date", "effective_dt", "inception",
)

# The other end of the term. Same shapes, same envelope-unwrapping as inception.
_EXPIRY_KEYS = (
    "expiry_dt", "contract_expiry_dt", "expiry_date", "expiration_date",
    "expiration_dt", "term_end", "expiry", "expiration",
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


def _contract_expiry(contract: Optional[Contract]) -> Optional[date]:
    """When the contract stops — the far end of the calendar.

    Mirrors _contract_inception exactly: the real `expiry_dt` column first, the
    extraction payload only as a fallback for rows written before that column
    was populated.
    """
    if contract is None:
        return None
    d = _parse_date(getattr(contract, "expiry_dt", None))
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
        for key in _EXPIRY_KEYS:
            d = _parse_date(_unwrap(scope.get(key)))
            if d:
                return d
    return None


def program_cover_end(session, program_id: int) -> Optional[date]:
    """The last date this programme is under contract — the LATEST expiry across
    every contract on it, not just the newest row.

    Renewals are why it is a max rather than "the current contract". A programme
    can hold an expired 2023 contract and a live 2025 one; taking either the
    newest id or the first row found would bound the calendar by whichever
    happened to be picked. Taking the furthest date means renewing a contract
    extends the calendar, and letting one lapse shortens it, which is the
    behaviour the dates are actually describing.

    None when NO contract on the programme states an expiry — the calendar then
    falls back to its rolling horizon rather than refusing to build.
    """
    ends = [
        _contract_expiry(c)
        for c in session.query(Contract).filter(
            Contract.program_id == program_id).all()
    ]
    ends = [d for d in ends if d is not None]
    return max(ends) if ends else None


def program_cover_ends(session, program_ids) -> dict:
    """{program_id: latest expiry} for many programmes in ONE query.

    Same rule as program_cover_end, batched: the board asks this for every
    programme it lists, and a query per row is a query per row.
    """
    ids = {i for i in program_ids if i is not None}
    if not ids:
        return {}
    out: dict = {}
    for c in session.query(Contract).filter(Contract.program_id.in_(ids)).all():
        d = _contract_expiry(c)
        if d is None:
            continue
        cur = out.get(c.program_id)
        if cur is None or d > cur:
            out[c.program_id] = d
    return out


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


def active_broker_ids(session, program_id: int) -> list[int]:
    """The brokers currently allowed to report on this programme, id order.

    Order matters: the FIRST id is the one that adopts any pre-broker calendar
    rows (see materialize_schedule), so it has to be stable across runs rather
    than whatever the database hands back.
    """
    rows = (session.query(ProgramBroker.broker_party_id)
            .filter(ProgramBroker.program_id == program_id,
                    or_(ProgramBroker.status.is_(None),
                        ProgramBroker.status != "inactive"))
            .all())
    seen, out = set(), []
    for (bid,) in rows:
        if bid is not None and bid not in seen:
            seen.add(bid)
            out.append(bid)
    return sorted(out)


def materialize_schedule(session, schedule: SubmissionSchedule,
                         today: Optional[date] = None,
                         horizon_months: Optional[int] = None) -> dict:
    """Resolve → generate → upsert ExpectedSubmission rows for this program.

    ONE ROW PER (BROKER, PERIOD). A programme is reported by several brokers and
    each owes its own file, so the calendar fans the periods out across the
    programme's active brokers. A programme with no broker on it yet keeps a
    single unattributed row per period (broker_party_id NULL) — the schedule
    still exists and can still be edited, there is simply nobody to be late.

    Idempotent: existing rows are updated in place (their received_at,
    received_export_id and version history are preserved), so re-running never
    loses a "received" mark. Produces nothing when the schedule is unresolved.
    Caller commits.
    """
    today = today or datetime.utcnow().date()
    resolved, program, contract = resolve_for_schedule(session, schedule)
    if resolved is None:
        return {"resolved": False, "count": 0,
                "reason": _unresolved_reason(schedule, program, contract)}

    # HOW FAR AHEAD TO BUILD. Two bounds, and the nearer one wins.
    #
    # The rolling horizon exists only so generation stops somewhere; it is
    # measured from TODAY and is otherwise arbitrary. It is frequency-aware
    # because twelve months is four quarters but only one year, and a yearly
    # programme showing a single row looks broken.
    if horizon_months is None:
        horizon_months = horizon_months_for(resolved.frequency)
    end_y, end_m = _add_months(today.year, today.month, horizon_months)
    end = date(end_y, end_m, monthrange(end_y, end_m)[1])

    # THE CONTRACT'S OWN END is the real bound, and it is the point of this.
    # Past its expiry the broker owes nothing under that agreement, so a period
    # generated beyond it asserts an obligation that does not exist. The rolling
    # horizon used to run a full year past a contract that had already ended.
    #
    # A period is kept only if it STARTS before the expiry: a contract ending
    # 1 Oct 2026 covers September 2026 in full and October not at all.
    #
    # Guarded on cover_end > anchor. A contract whose expiry predates its own
    # inception is bad data, and honouring it would empty the calendar — worse,
    # the stale-period sweep below would then delete every unfiled period.
    cover_end = program_cover_end(session, schedule.program_id)
    bounded_by = "horizon"
    if cover_end is not None and cover_end > resolved.anchor:
        contract_end = cover_end - timedelta(days=1)
        if contract_end < end:
            end = contract_end
            bounded_by = "contract"

    rows = generate_expected(resolved, resolved.anchor, end)

    brokers = active_broker_ids(session, schedule.program_id)
    existing_rows = (session.query(ExpectedSubmission)
                     .filter(ExpectedSubmission.program_id == schedule.program_id)
                     .all())
    by_key = {(e.broker_party_id, e.period): e for e in existing_rows}

    # ADOPTION. Every row written before brokers were part of the key has
    # broker_party_id NULL. Re-keying them onto the programme's first broker
    # keeps their arrival history attached to a real obligation; creating fresh
    # broker rows beside them would strand that history on a row nobody reads
    # and show the same period twice.
    if brokers:
        primary = brokers[0]
        for e in list(existing_rows):
            if e.broker_party_id is None and (primary, e.period) not in by_key:
                by_key.pop((None, e.period), None)
                e.broker_party_id = primary
                by_key[(primary, e.period)] = e

    targets: list[Optional[int]] = list(brokers) if brokers else [None]
    written = 0
    for bid in targets:
        for r in rows:
            e = by_key.get((bid, r["period"]))
            if e is None:
                e = ExpectedSubmission(
                    tenant_id=schedule.tenant_id, program_id=schedule.program_id,
                    broker_party_id=bid, period=r["period"])
                session.add(e)
                by_key[(bid, r["period"])] = e
            e.schedule_id = schedule.id
            e.period_start = r["period_start"]
            e.period_end = r["period_end"]
            e.due_date = r["due_date"]
            e.status = derive_status(r["due_date"], today,
                                     resolved.soon_window_days, e.received_at)
            written += 1

    # WHAT NO LONGER BELONGS. Two ways a row can be left over:
    #
    #   * the frequency changed, so "2026" and "2026-H1" are not periods this
    #     programme has any more — switching yearly to monthly used to leave the
    #     yearly rows sitting in the calendar as obligations nobody owed;
    #   * a broker was taken off the programme, so it stops owing FUTURE files.
    #
    # Neither ever removes a period with an ARRIVAL against it. A file that was
    # actually sent is history, and history does not stop being true because the
    # reporting frequency changed afterwards.
    wanted = {r["period"] for r in rows}
    for e in list(existing_rows):
        if e.received_at is not None or (e.version_count or 0):
            continue                       # something was submitted — keep it
        stale_period = e.period not in wanted
        dropped_broker = (brokers and e.broker_party_id is not None
                          and e.broker_party_id not in brokers
                          and e.period_start is not None and e.period_start > today)
        if stale_period or dropped_broker:
            session.delete(e)

    session.flush()
    return {"resolved": True, "count": written, "periods": len(rows),
            "brokers": len(brokers),
            "frequency": resolved.frequency, "anchor": resolved.anchor.isoformat(),
            # Which bound stopped it, so a caller can say "the calendar ends here
            # because the contract does" rather than leaving it unexplained.
            "bounded_by": bounded_by,
            "covers_until": cover_end.isoformat() if cover_end else None,
            "last_period_end": end.isoformat()}


def heal_calendars(session, tenant_id: Optional[int] = None,
                   today: Optional[date] = None) -> dict:
    """Re-materialize calendars that were built under rules that have since changed.

    FIRST CASE — a calendar with no broker on its rows.

    materialize_schedule() is the only thing that attributes a period to a
    broker, and it runs just twice: when a schedule is first created, and when
    somebody saves it. So every calendar generated before the broker became part
    of the key keeps broker_party_id NULL indefinitely — the board reads those
    rows as "no broker on it yet", shows "Nothing is owed" against programmes
    that plainly do owe something, and counts zero in every tile. Nothing is
    wrong with the data; it simply predates the column.

    This heals it lazily, on view, the same way sweep_overdue already runs from
    the calendar page. It only touches a programme where BOTH are true —
    unattributed rows exist AND the programme has active brokers — so it is two
    indexed lookups per schedule and no writes at all once a tenant is healed.

    A programme with no brokers is left exactly as it is: its unattributed row
    is the correct answer there, not a leftover.

    SECOND CASE — a calendar that runs past its contract. Deadlines used to be
    generated on a rolling twelve-month horizon that ignored the contract term,
    so existing calendars assert obligations for periods after the agreement
    ended. Those only shrink when materialize_schedule runs again, and once a
    programme is attributed nothing else makes it run — so an over-long calendar
    would sit there until somebody happened to re-save the schedule.

    Both checks are cheap: an indexed lookup per schedule, and no writes at all
    once a tenant is settled. Caller commits.
    """
    today = today or datetime.utcnow().date()
    q = session.query(SubmissionSchedule)
    if tenant_id is not None:
        q = q.filter(SubmissionSchedule.tenant_id == tenant_id)

    healed = []
    for sch in q.all():
        reason = None

        unattributed = (session.query(ExpectedSubmission.id)
                        .filter(ExpectedSubmission.program_id == sch.program_id,
                                ExpectedSubmission.broker_party_id.is_(None))
                        .first())
        # Only worth attributing when there is somebody to attribute it TO.
        if unattributed is not None and active_broker_ids(session, sch.program_id):
            reason = "unattributed"

        if reason is None:
            cover_end = program_cover_ends(
                session, [sch.program_id]).get(sch.program_id)
            if cover_end is not None:
                # Any period that STARTS on or after the expiry should not exist.
                overrun = (session.query(ExpectedSubmission.id)
                           .filter(ExpectedSubmission.program_id == sch.program_id,
                                   ExpectedSubmission.period_start >= cover_end)
                           .first())
                if overrun is not None:
                    reason = "past_contract_end"

        if reason is None:
            continue
        result = materialize_schedule(session, sch, today=today)
        if result.get("resolved"):
            healed.append({"program_id": sch.program_id,
                           "reason": reason,
                           "rows": result.get("count"),
                           "brokers": result.get("brokers"),
                           "bounded_by": result.get("bounded_by")})
    session.flush()
    return {"healed": len(healed), "programs": healed}


def _unresolved_reason(schedule, program, contract) -> str:
    freq = schedule.frequency_override or (program.bdx_frequency if program else None)
    anchor = schedule.anchor_date_override or _contract_inception(contract)
    if not freq and not anchor:
        return "need_frequency_and_start"
    if not freq:
        return "need_frequency"
    return "need_start_date"


def resolve_period_label(session, program_id: int, *,
                         explicit: Optional[str] = None,
                         covering_date: Optional[date] = None,
                         source_filename: Optional[str] = None
                         ) -> tuple[Optional[str], str]:
    """Which period a file is FOR, and how confidently we know it.

    Returns (label, source) where source is one of:

      explicit  — the caller stated the period. Always trusted.
      date      — the caller gave a date the file covers; the schedule turns it
                  into the period containing it.
      filename  — read out of the file name ("SpectrumBDX_2026-07.xlsx").
      unknown   — nothing said. The caller falls back to its own rule.

    The point of the middle two is that a LATE file belongs to its own period.
    July's bordereau arriving in September is July's, and the only way to know
    that without asking is to read the period off the file rather than off the
    clock. When nothing can be read, we say so instead of guessing — the caller
    then uses "the oldest period still open", which is a reasonable default but
    is a guess, and the difference gets recorded on the version row.
    """
    if explicit:
        return explicit, "explicit"
    sched = (session.query(SubmissionSchedule)
             .filter(SubmissionSchedule.program_id == program_id).first())
    resolved = resolve_for_schedule(session, sched)[0] if sched is not None else None
    if resolved is None:
        return None, "unknown"
    if covering_date is not None:
        label = period_for_date(resolved, covering_date)
        if label:
            return label, "date"
    hinted = parse_period_hint(source_filename)
    if hinted is not None:
        label = period_for_date(resolved, hinted)
        if label:
            return label, "filename"
    return None, "unknown"


def _pick_expected(session, program_id: int, *, period: Optional[str],
                   broker_party_id: Optional[int],
                   received_on: date) -> Optional[ExpectedSubmission]:
    """The calendar row a file satisfies.

    With a period: that period's row for this broker, falling back to the
    programme's unattributed row when the broker has none (a file can arrive
    from a broker before anybody has put them on the programme).

    Without a period: the OLDEST still-unreceived period that has already
    ended — the outstanding obligation somebody is catching up on. This is the
    guess; resolve_period_label() exists to avoid needing it.
    """
    q = session.query(ExpectedSubmission).filter(
        ExpectedSubmission.program_id == program_id)
    if period is not None:
        rows = q.filter(ExpectedSubmission.period == period).all()
        if broker_party_id is not None:
            for e in rows:
                if e.broker_party_id == broker_party_id:
                    return e
        for e in rows:
            if e.broker_party_id is None:
                return e
        return rows[0] if rows and broker_party_id is None else None
    if broker_party_id is not None:
        q = q.filter(or_(ExpectedSubmission.broker_party_id == broker_party_id,
                         ExpectedSubmission.broker_party_id.is_(None)))
    return (q.filter(ExpectedSubmission.received_at.is_(None),
                     ExpectedSubmission.period_end <= received_on)
            .order_by(ExpectedSubmission.period_end.asc()).first())


def mark_received(session, program_id: int, received_on: Optional[date] = None,
                  export_id: Optional[int] = None, period: Optional[str] = None,
                  broker_party_id: Optional[int] = None,
                  source_filename: Optional[str] = None,
                  covering_date: Optional[date] = None,
                  period_source: Optional[str] = None) -> Optional[dict]:
    """Record a bordereau arriving for a period — as a new VERSION of it.

    Sending the same period twice is normal: the original goes in on the due
    date, something is found wrong, and the period is sent again. So this
    APPENDS rather than overwrites.

      * version 1 is the `original`. It, and only it, sets `received_at` and
        therefore decides on_time vs received_late. A correction three weeks
        later must never repaint a missed deadline green.
      * version 2+ is a `corrected`. It moves `latest_received_at` and the
        version count, and leaves the deadline verdict exactly where it was.

    Which period it satisfies is worked out by resolve_period_label() — from an
    explicit period, a date the file covers, or the file name — and only falls
    back to "the oldest period still open" when none of those can answer. How it
    was decided is stored on the version row, because "the file said July" and
    "we assumed July" are different levels of confidence.

    Returns the version summary, or None when there is nothing to satisfy (no
    calendar for this programme, or nothing due yet) or when this exact export
    has already been recorded. Never raises for "no match".
    """
    received_on = received_on or datetime.utcnow().date()

    label, source = resolve_period_label(
        session, program_id, explicit=period, covering_date=covering_date,
        source_filename=source_filename)
    if label is None:
        source = "oldest_open"
    target = _pick_expected(session, program_id, period=label,
                            broker_party_id=broker_party_id,
                            received_on=received_on)
    if target is None:
        return None

    # Re-rendering an export in place must not look like a correction. The same
    # generated file arriving twice is one submission, not two.
    if export_id is not None:
        dupe = (session.query(SubmissionVersion)
                .filter(SubmissionVersion.expected_id == target.id,
                        SubmissionVersion.received_export_id == export_id).first())
        if dupe is not None:
            return None

    last = (session.query(SubmissionVersion)
            .filter(SubmissionVersion.expected_id == target.id)
            .order_by(SubmissionVersion.version_no.desc()).first())
    version_no = (last.version_no if last else 0) + 1
    kind = "original" if version_no == 1 else "corrected"

    session.add(SubmissionVersion(
        tenant_id=target.tenant_id, expected_id=target.id,
        program_id=target.program_id, broker_party_id=target.broker_party_id,
        period=target.period, version_no=version_no, kind=kind,
        received_at=received_on, received_export_id=export_id,
        source_filename=source_filename,
        period_source=period_source or source))

    target.version_count = version_no
    target.latest_received_at = received_on
    if version_no == 1:
        # The deadline verdict is set here, once, by the file that met it or
        # missed it. Later versions deliberately leave it alone.
        target.received_at = received_on
        target.received_export_id = export_id
        target.status = derive_status(target.due_date, received_on,
                                      received_on=received_on)
    session.flush()
    return {"expected_id": target.id, "period": target.period,
            "broker_party_id": target.broker_party_id,
            "version_no": version_no, "kind": kind,
            "status": target.status, "period_source": period_source or source,
            "due_date": target.due_date.isoformat() if target.due_date else None,
            "received_at": received_on.isoformat()}


def record_release(session, expected_id: int, *, released_on: Optional[date] = None,
                   released_to: Optional[str] = None, released_by: Optional[str] = None,
                   release_ref: Optional[str] = None, version_no: Optional[int] = None,
                   note: Optional[str] = None) -> Optional[dict]:
    """Record that a period's file was SENT ONWARD to its recipient.

    Producing a file and sending it are two different acts — a bordereau can be
    generated on the 3rd and only reach the reinsurer on the 6th — so the
    calendar records them separately. That is what makes "did Munich Re get
    July, and when?" a question with a straight answer.

    The release attaches to a VERSION, not to the period, because the answer to
    "which file did they get?" has to survive a later correction. Defaults to the
    newest version. Returns None when the period has no submission yet: there is
    nothing to send on. Caller commits.
    """
    released_on = released_on or datetime.utcnow().date()
    target = session.get(ExpectedSubmission, expected_id)
    if target is None:
        return None
    q = session.query(SubmissionVersion).filter(
        SubmissionVersion.expected_id == expected_id)
    v = (q.filter(SubmissionVersion.version_no == version_no).first()
         if version_no is not None
         else q.order_by(SubmissionVersion.version_no.desc()).first())
    if v is None:
        return None

    first_release = v.released_at is None
    v.released_at = released_on
    v.released_to = released_to
    v.released_by = released_by
    v.release_ref = release_ref
    if note:
        v.note = note
    target.released_at = released_on
    if first_release:
        target.released_count = (target.released_count or 0) + 1
    session.flush()
    return {"expected_id": target.id, "period": target.period,
            "version_no": v.version_no, "kind": v.kind,
            "released_at": released_on.isoformat(), "released_to": released_to}


def record_chase(session, expected_ids: list[int], *, actor: Optional[str] = None,
                 note: Optional[str] = None,
                 today: Optional[date] = None) -> dict:
    """Note that somebody was chased for a late file.

    Writes an activity event and stamps the row, so the screen can say "chased
    2 days ago" rather than offering a button whose effect nobody can see. It
    does NOT send the broker an email — that would be a message leaving the
    building, and nothing here is wired to send one. What it does is make the
    chase a recorded act instead of a private one.

    Only rows that are actually late AND belong to a broker are chased; anything
    else is ignored rather than refused. The broker check matters: a period with
    no broker on it has nobody to chase, and the screen already says "Nothing is
    owed" for those rows — stamping one would record a conversation that cannot
    have happened. The tenant-wide path applies the same rule, so passing ids by
    hand no longer reaches rows the bulk button would skip. Caller commits.
    """
    today = today or datetime.utcnow().date()
    chased = []
    for e in (session.query(ExpectedSubmission)
              .filter(ExpectedSubmission.id.in_(expected_ids or [])).all()):
        if e.broker_party_id is None:
            continue
        if e.received_at is not None or e.due_date is None or e.due_date >= today:
            continue
        e.chased_at = today
        e.chase_count = (e.chase_count or 0) + 1
        details = {"program_id": e.program_id, "period": e.period,
                   "broker_party_id": e.broker_party_id,
                   "due_date": e.due_date.isoformat(),
                   "days_over": (today - e.due_date).days,
                   "chase_count": e.chase_count}
        # What the operator actually said, kept with the chase. Without it the
        # record shows that somebody was chased but not what they were told,
        # which is the half that matters if it is ever disputed.
        if note:
            details["note"] = note
        session.add(ActivityEvent(
            tenant_id=e.tenant_id, actor=actor or "system", action="submission_chased",
            target=f"program:{e.program_id}", details=details))
        chased.append({"expected_id": e.id, "period": e.period,
                       "program_id": e.program_id,
                       "broker_party_id": e.broker_party_id,
                       "days_over": (today - e.due_date).days})
    session.flush()
    return {"chased": len(chased), "rows": chased}


def submission_versions(session, expected_id: int) -> list[dict]:
    """Every file ever submitted for one period, oldest first.

    The original is kept exactly as it was even after a correction replaces it
    in practice, because replacing it would quietly rewrite history — anybody
    looking back later would see numbers that never actually went out.
    """
    out = []
    for v in (session.query(SubmissionVersion)
              .filter(SubmissionVersion.expected_id == expected_id)
              .order_by(SubmissionVersion.version_no.asc()).all()):
        out.append({
            "id": v.id, "version_no": v.version_no, "kind": v.kind,
            "received_at": v.received_at.isoformat() if v.received_at else None,
            "received_export_id": v.received_export_id,
            "source_filename": v.source_filename,
            "period_source": v.period_source,
            "released_at": v.released_at.isoformat() if v.released_at else None,
            "released_to": v.released_to, "released_by": v.released_by,
            "release_ref": v.release_ref, "note": v.note,
        })
    return out


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


def _party_names(session, ids) -> dict:
    """{party_id: display name} for the ids given. One query, missing ids simply
    absent — a deactivated broker must not blank out the row it is on."""
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {p.id: p.legal_name for p in
            session.query(Party).filter(Party.id.in_(ids)).all()}


def _latest_release_state(session, expected_ids) -> dict:
    """{expected_id: True/False} — has the NEWEST version been sent onward?

    Needed because `released_at` on the period is the LAST release, which can
    predate the newest file: send July, find something wrong, resend it, and the
    period still reads "sent 16 Aug" while the correction sits unsent. That is
    precisely the thing this screen exists to catch, so it gets its own answer
    rather than being inferred from a date.
    """
    ids = [i for i in expected_ids if i is not None]
    if not ids:
        return {}
    newest: dict = {}
    for v in (session.query(SubmissionVersion)
              .filter(SubmissionVersion.expected_id.in_(ids))
              .order_by(SubmissionVersion.version_no.asc()).all()):
        newest[v.expected_id] = v.released_at is not None
    return newest


# Who at a broker would be told a file is late. There is no contact record for a
# broker anywhere in the platform — party_contact exists but nothing populates it —
# so the only real answer is the broker's own user accounts. An admin is preferred
# over an operator: chasing is a management conversation, not a task assignment.
_CONTACT_ROLE_RANK = {"broker_admin": 0, "operator": 1}

# An INVITED user counts. They are the address the carrier chose when they set the
# broker up; they simply have not signed in yet. Dropping them made the dialog
# say "nobody on record" for a broker that has a perfectly good contact, which is
# the one thing it must not get wrong. They rank below an active user and are
# labelled, so the operator can see the difference. Anything else — disabled,
# removed — is excluded outright.
_CONTACT_STATUSES = ("active", "invited")
_CONTACT_STATUS_RANK = {"active": 0, "invited": 1}


def broker_contacts(session, broker_ids) -> dict:
    """{broker_party_id: [{name, email, role, status}, ...]} — best contact first.

    One query for every broker on the screen rather than one per row. Brokers with
    no usable account come back absent, not empty-string: "we do not know who to
    tell" is a different answer from "nobody", and the screen has to be able to
    say so.
    """
    ids = {i for i in broker_ids if i is not None}
    if not ids:
        return {}
    out: dict = {}
    rows = (session.query(AppUser)
            .filter(AppUser.broker_party_id.in_(ids),
                    or_(AppUser.status.is_(None),
                        AppUser.status.in_(_CONTACT_STATUSES)))
            .all())
    for u in rows:
        out.setdefault(u.broker_party_id, []).append({
            "name": u.full_name or u.email,
            "email": u.email,
            "role": u.role,
            "status": u.status or "active",
        })
    for bucket in out.values():
        bucket.sort(key=lambda c: (_CONTACT_STATUS_RANK.get(c.get("status"), 9),
                                   _CONTACT_ROLE_RANK.get(c.get("role"), 9),
                                   (c.get("name") or "").lower()))
    return out


def _version_label(e) -> str:
    """How the calendar names a period's version, in the words the screen uses."""
    n = e.version_count or 0
    if n == 0:
        return "—"
    if n == 1:
        return "First version"
    if n == 2:
        return "Corrected once"
    return f"Corrected {n - 1} times"


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

    rows = q.order_by(ExpectedSubmission.due_date.asc()).all()
    names = _party_names(session, [e.broker_party_id for e in rows])
    latest_sent = _latest_release_state(session, [e.id for e in rows])

    out = []
    for e in rows:
        sch = schedules.get(e.program_id)
        soon = sch.soon_window_days if sch and sch.soon_window_days is not None else DEFAULT_SOON_WINDOW_DAYS
        status = derive_status(e.due_date, today, soon, e.received_at)
        out.append({
            "id": e.id,
            "program_id": e.program_id,
            "broker_party_id": e.broker_party_id,
            "broker_name": names.get(e.broker_party_id),
            "period": e.period,
            "period_start": e.period_start.isoformat() if e.period_start else None,
            "period_end": e.period_end.isoformat() if e.period_end else None,
            "due_date": e.due_date.isoformat() if e.due_date else None,
            "status": status,
            "received_at": e.received_at.isoformat() if e.received_at else None,
            "received_export_id": e.received_export_id,
            # Requirement 17.2 — the released and corrected halves.
            "version_count": e.version_count or 0,
            "version_label": _version_label(e),
            "latest_received_at": e.latest_received_at.isoformat()
                if e.latest_received_at else None,
            "released_at": e.released_at.isoformat() if e.released_at else None,
            "released_count": e.released_count or 0,
            "latest_version_released": latest_sent.get(e.id, False),
            "chased_at": e.chased_at.isoformat() if e.chased_at else None,
            "chase_count": e.chase_count or 0,
        })
    return out


# --- the carrier's Bordereau Calendar ---------------------------------------
# One screen answering three questions in the order they get asked: what is owed
# to me this month, what turned up, and what did I send on. The board is keyed on
# the DUE MONTH rather than on the reporting period, because programmes on
# different frequencies have to appear on the same page — a monthly programme's
# July file and a quarterly programme's Q2 file are both due in August, and
# "August" is the only heading both of them belong under.

_FREQ_LABEL = {
    "weekly": "Every week", "monthly": "Every month",
    "quarterly": "Every three months", "half_yearly": "Every six months",
    "yearly": "Once a year",
}

_ORDINAL_SUFFIX = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{_ORDINAL_SUFFIX.get(n % 10, 'th')}"


def _due_rule_text(resolved) -> str:
    """The deadline in words — "the 15th of the following month"."""
    if resolved is None:
        return "not set"
    if resolved.frequency == "weekly" or resolved.due_day_of_month is None:
        return f"{resolved.due_offset_days} days after the period ends"
    day = _ordinal(resolved.due_day_of_month)
    if resolved.frequency == "monthly":
        return f"{day} of the following month"
    if resolved.frequency == "quarterly":
        return f"{day} of the month after the quarter ends"
    if resolved.frequency == "half_yearly":
        return f"{day} of the month after the half-year ends"
    return f"{day} of the month after the year ends"


def _month_key(d: Optional[date]) -> Optional[str]:
    return f"{d.year:04d}-{d.month:02d}" if d else None


def calendar_board(session, tenant_id: Optional[int], *, month: Optional[str] = None,
                   today: Optional[date] = None) -> dict:
    """The carrier's Bordereau Calendar for one due-month.

    Returns the rows (one per programme x broker x period due that month), the
    five headline counts above them, the list of months that have anything in
    them, and how often each programme reports.

    A programme with NO BROKER on it shows as owing nothing rather than as
    overdue. There is nobody to be late, and an overdue row against no-one is a
    number that cannot be acted on — it stays in the list so the programme reads
    as idle instead of being forgotten.
    """
    today = today or datetime.utcnow().date()
    programs = {p.id: p for p in session.query(Program)
                .filter(Program.tenant_id == tenant_id).all()}
    schedules = {s.program_id: s for s in session.query(SubmissionSchedule)
                 .filter(SubmissionSchedule.tenant_id == tenant_id).all()}
    all_rows = (session.query(ExpectedSubmission)
                .filter(ExpectedSubmission.tenant_id == tenant_id)
                .order_by(ExpectedSubmission.due_date.asc()).all())

    months = sorted({_month_key(e.due_date) for e in all_rows if e.due_date},
                    reverse=True)
    if month not in months:
        # Default to the month being worked on: the current one if it has
        # anything in it, otherwise the most recent month that does — so the
        # screen opens on real work rather than on an empty heading.
        month = _month_key(today) if _month_key(today) in months else (
            months[0] if months else _month_key(today))

    names = _party_names(session, [e.broker_party_id for e in all_rows])
    contacts = broker_contacts(session, [e.broker_party_id for e in all_rows])
    latest_sent = _latest_release_state(
        session, [e.id for e in all_rows if _month_key(e.due_date) == month])
    rows, counts = [], {"due": 0, "on_time": 0, "late": 0, "never": 0,
                        "released": 0, "unsent_correction": 0}
    programmes_in_month = set()

    for e in all_rows:
        if _month_key(e.due_date) != month:
            continue
        sch = schedules.get(e.program_id)
        soon = sch.soon_window_days if sch and sch.soon_window_days is not None else DEFAULT_SOON_WINDOW_DAYS
        status = derive_status(e.due_date, today, soon, e.received_at)
        unassigned = e.broker_party_id is None
        days_over = ((today - e.due_date).days
                     if e.received_at is None and e.due_date and today > e.due_date
                     else None)
        days_late = ((e.received_at - e.due_date).days
                     if e.received_at and e.due_date and e.received_at > e.due_date
                     else None)
        prog = programs.get(e.program_id)
        rows.append({
            "id": e.id,
            "program_id": e.program_id,
            "program_name": prog.name if prog else f"Programme {e.program_id}",
            "broker_party_id": e.broker_party_id,
            "broker_name": names.get(e.broker_party_id),
            "unassigned": unassigned,
            "period": e.period,
            "due_date": e.due_date.isoformat() if e.due_date else None,
            "received_at": e.received_at.isoformat() if e.received_at else None,
            "latest_received_at": e.latest_received_at.isoformat()
                if e.latest_received_at else None,
            "status": status,
            "days_over": days_over,
            "days_late": days_late,
            "released_at": e.released_at.isoformat() if e.released_at else None,
            "released_count": e.released_count or 0,
            # Whether the file they would get TODAY has actually gone. False on
            # a period that was corrected after it was sent — the recipient is
            # holding a version we have since superseded.
            "latest_version_released": latest_sent.get(e.id, False),
            "version_count": e.version_count or 0,
            "version_label": _version_label(e),
            "chased_at": e.chased_at.isoformat() if e.chased_at else None,
            "chase_count": e.chase_count or 0,
            # Who would be told, if this row were chased. Empty when the broker
            # has no user account — the screen says so rather than pretending.
            "contacts": contacts.get(e.broker_party_id, []),
        })
        # Nobody owes anything on a programme with no broker, so it is counted
        # nowhere — including in "due this month".
        if unassigned:
            continue
        programmes_in_month.add(e.program_id)
        counts["due"] += 1
        if status == "on_time":
            counts["on_time"] += 1
        elif status == "received_late":
            counts["late"] += 1
        elif status == "overdue":
            counts["never"] += 1
        if (e.released_count or 0) > 0:
            counts["released"] += 1
        # Corrected, sent once, and the correction never went out.
        if (e.version_count or 0) > 1 and (e.released_count or 0) > 0 \
                and not latest_sent.get(e.id, False):
            counts["unsent_correction"] += 1

    # How often each programme reports — the answer that fills everything above.
    cover_ends = program_cover_ends(session, list(programs.keys()))
    schedule_rows = []
    for pid, p in sorted(programs.items(), key=lambda kv: (kv[1].name or "").lower()):
        sch = schedules.get(pid)
        resolved = resolve_for_schedule(session, sch)[0] if sch is not None else None
        nxt = (session.query(ExpectedSubmission)
               .filter(ExpectedSubmission.program_id == pid,
                       ExpectedSubmission.received_at.is_(None),
                       ExpectedSubmission.due_date >= today)
               .order_by(ExpectedSubmission.due_date.asc()).first())
        schedule_rows.append({
            "program_id": pid,
            "program_name": p.name,
            "frequency": resolved.frequency if resolved else None,
            "frequency_label": (_FREQ_LABEL.get(resolved.frequency, resolved.frequency)
                                if resolved else "Not set"),
            "due_rule": _due_rule_text(resolved),
            "next_due": nxt.due_date.isoformat() if nxt and nxt.due_date else None,
            "broker_count": len(active_broker_ids(session, pid)),
            # When the contract stops. This is what bounds the calendar, so the
            # screen can say why the deadlines end where they do instead of the
            # list simply running out.
            "covers_until": (cover_ends.get(pid).isoformat()
                             if cover_ends.get(pid) else None),
        })

    return {
        "month": month,
        "months": months,
        "rows": rows,
        "counts": counts,
        "programme_count": len(programmes_in_month),
        "schedules": schedule_rows,
    }
