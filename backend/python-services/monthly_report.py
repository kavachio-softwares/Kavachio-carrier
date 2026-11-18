"""MOPR 42 — the monthly pack (42.1) and its comparisons (42.2).

One month's timeliness, volumes, data quality and fixes, beside last month and
the same month last year, and one line per broker and programme. Read entirely from what the other three reports already count —
nothing new is stored:

  TIMELINESS  expected_submission (MOPR 39): the files DUE in the month, by
              due date, and how each stands today.
  VOLUMES,
  QUALITY     check_record.summary (MOPR 40): the files CHECKED in the month.
  FIXES       fix_performance.problems (MOPR 41): the problems FIXED in the
              month, and what was still waiting on its last day — worked out
              from when each problem was found and fixed, so a past month reads
              the same today as it did then. The current month counts to now.

A month before the records began has nothing in it and says so (`has_data`):
"vs last year" reads "not available" for the first year rather than as zero.

A DAY, A WEEK OR DATES PICKED (`pack_for`) — the same switch as the Executive
Report, measured the same way over the period's own dates: the files due in
it, the files checked in it, the problems fixed in it and what was still
waiting at its end. A month is `pack` exactly as before. See `window` for what
each period is compared with.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional


def shift(month: str, n: int) -> str:
    """'2026-10' shifted by n months: shift('2026-10', -12) → '2025-10'."""
    y, m = (int(x) for x in month.split("-")[:2])
    i = y * 12 + (m - 1) + n
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def _pct(n: int, d: int) -> Optional[float]:
    return round(100.0 * n / d, 1) if d else None


def _avg(xs: list[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 1) if xs else None


def _month_of(d) -> str:
    return f"{d.year:04d}-{d.month:02d}"


# --- MOPR 39: was each file on time -----------------------------------------------

def _due_rows(s, tenant_id: int, first: date, last: date, *, program_id,
              broker_party_id) -> list:
    """The files due in [first, last)."""
    from db import ExpectedSubmission
    q = (s.query(ExpectedSubmission)
         .filter(ExpectedSubmission.tenant_id == tenant_id,
                 # Nobody owes a file on a programme with no broker.
                 ExpectedSubmission.broker_party_id.isnot(None),
                 ExpectedSubmission.due_date >= first,
                 ExpectedSubmission.due_date < last))
    if program_id:
        q = q.filter(ExpectedSubmission.program_id == program_id)
    if broker_party_id:
        q = q.filter(ExpectedSubmission.broker_party_id == broker_party_id)
    return q.all()


def _due_blank() -> dict:
    return {"due": 0, "due_so_far": 0, "on_time": 0, "late": 0, "overdue": 0,
            "never": 0, "cases_open": 0, "_late_days": []}


def _due_add(t: dict, e, status: str, open_states) -> None:
    """Count one file due into a tally, by how it stands."""
    t["due"] += 1
    if status in ("scheduled", "due_soon", "due_today"):
        return
    t["due_so_far"] += 1
    if status == "on_time":
        t["on_time"] += 1
    elif status == "received_late":
        t["late"] += 1
        t["_late_days"].append((e.received_at - e.due_date).days)
    elif status == "overdue":
        t["overdue"] += 1
    elif status == "never_arrived":
        t["never"] += 1
    if e.case_state in open_states:
        t["cases_open"] += 1


def _due_done(t: dict) -> dict:
    late_days = t.pop("_late_days")
    t["on_time_pct"] = _pct(t["on_time"], t["due_so_far"])
    t["avg_days_late"] = _avg(late_days)
    return t


def _timeliness(s, tenant_id: int, months: set[str], *, program_id, broker_party_id,
                today: date) -> tuple[dict, dict]:
    """Per month, and per (broker, programme) in each month: the files due.
    (The Executive Report reads this too — keep its arguments and its result.)"""
    from check_record import month_bounds
    from submission_calendar import derive_status
    from submission_case import OPEN

    first = month_bounds(min(months))[0].date()
    last = month_bounds(max(months))[1].date()
    per_month: dict[str, dict] = defaultdict(_due_blank)
    per_group: dict[str, dict] = defaultdict(lambda: defaultdict(_due_blank))
    for e in _due_rows(s, tenant_id, first, last, program_id=program_id,
                       broker_party_id=broker_party_id):
        k = _month_of(e.due_date)
        if k not in months:
            continue
        status = derive_status(e.due_date, today, 0, e.received_at, e.closes_on)
        for t in (per_month[k], per_group[k][(e.broker_party_id, e.program_id)]):
            _due_add(t, e, status, OPEN)

    return ({k: _due_done(per_month[k]) for k in months},
            {k: {g: _due_done(t) for g, t in groups.items()} for k, groups in per_group.items()})


def _timeliness_between(s, tenant_id: int, first: date, last: date, *, program_id,
                        broker_party_id, today: date) -> tuple[dict, dict]:
    """The files due in [first, last) — a day, a week or the dates picked — and
    per (broker, programme): counted exactly as a month's are."""
    from submission_calendar import derive_status
    from submission_case import OPEN

    total = _due_blank()
    groups: dict[tuple, dict] = defaultdict(_due_blank)
    for e in _due_rows(s, tenant_id, first, last, program_id=program_id,
                       broker_party_id=broker_party_id):
        status = derive_status(e.due_date, today, 0, e.received_at, e.closes_on)
        for t in (total, groups[(e.broker_party_id, e.program_id)]):
            _due_add(t, e, status, OPEN)
    return _due_done(total), {g: _due_done(t) for g, t in groups.items()}


# --- MOPR 40: what came in, and how clean -------------------------------------------

def _quality(s, tenant_id: int, month: str, *, program_id, broker_party_id) -> tuple[dict, dict, list]:
    import check_record
    start, end = check_record.month_bounds(month)
    return _quality_between(s, tenant_id, start, end, program_id=program_id,
                            broker_party_id=broker_party_id)


def _quality_between(s, tenant_id: int, start: datetime, end: datetime, *, program_id,
                     broker_party_id) -> tuple[dict, dict, list]:
    """The files first checked in [start, end): what came in and how clean."""
    import check_record
    out = check_record.summary(s, tenant_id, start, end, program_id=program_id,
                               broker_party_id=broker_party_id)
    v, q, h = out["volumes"], out["quality"], out["no_human_help"]
    by_type = {g["key"]: g for g in v["transaction_types"]}
    count = "policies" if v["policies_known"] else "rows"
    volumes = {"files": v["files"], "rows": v["rows"], "counted_by": count,
               **{k: (by_type.get(k) or {}).get(count, 0) for k in ("new", "renewed", "cancelled")}}
    top = q["top_failing_checks"][0]["check"] if q["top_failing_checks"] else None
    quality = {"errors": q["errors"], "critical": q["critical"],
               "rows_with_errors_pct": (round(100.0 - h["rows_pct"], 1)
                                        if h["rows_pct"] is not None else None),
               "clean_first_time": h["files"], "clean_first_time_pct": h["files_pct"],
               "top_check": top}
    return volumes, quality, out["by_broker_programme"]


# --- MOPR 41: how fast problems were fixed -------------------------------------------

def _fixes(probs: list[dict], start: datetime, end: datetime, target: int,
           now: datetime) -> dict:
    """Fixed in [start, end); and what was waiting at its end (or now, for the
    month still running) — found by then, not yet fixed by then."""
    from fix_performance import _days
    fixed = [p for p in probs if p["fixed_at"] and start <= p["fixed_at"] < end]
    took = [_days(p["fixed_at"] - p["found_at"]) for p in fixed if p["found_at"]]
    at = min(end, now)
    waiting = [p for p in probs if p["found_at"] and p["found_at"] < at
               and (p["fixed_at"] is None or p["fixed_at"] >= at)]
    within = sum(1 for d in took if d <= target)
    return {"fixed": len(fixed), "avg_days": _avg(took),
            "within_target": within, "within_target_pct": _pct(within, len(took)),
            "waiting": len(waiting),
            "waiting_past_target": sum(1 for p in waiting if _days(at - p["found_at"]) > target)}


def _by_broker_programme(timely_by: dict, quality_by: list, probs: list[dict],
                         start: datetime, end: datetime, target: int, now: datetime) -> list:
    """One line per broker and programme for one period: its files due, its
    files checked and its fixes — the three reports joined."""
    rows: dict[tuple, dict] = {}

    def _row(b, p):
        return rows.setdefault((b, p), {"broker_party_id": b, "program_id": p,
                                        "due_so_far": 0, "on_time": 0, "on_time_pct": None,
                                        "files": 0, "clean_first_time": 0,
                                        "clean_first_time_pct": None, "errors": 0,
                                        "avg_fix_days": None, "waiting_past_target": 0})
    for (b, p), t in timely_by.items():
        r = _row(b, p)
        r.update(due_so_far=t["due_so_far"], on_time=t["on_time"], on_time_pct=t["on_time_pct"])
    for q in quality_by:
        r = _row(q["broker_party_id"], q["program_id"])
        r.update(files=q["files"], clean_first_time=q["straight_through"],
                 clean_first_time_pct=q["straight_through_pct"], errors=q["errors"])
    by_group: dict[tuple, list] = defaultdict(list)
    for p in probs:
        by_group[(p["broker_party_id"], p["program_id"])].append(p)
    for key, ps in by_group.items():
        f = _fixes(ps, start, end, target, now)
        if f["fixed"] or f["waiting"] or key in rows:
            r = _row(*key)
            r.update(avg_fix_days=f["avg_days"], waiting_past_target=f["waiting_past_target"])
    return sorted(rows.values(), key=lambda r: (-r["waiting_past_target"], -r["errors"],
                                                -(r["due_so_far"] - r["on_time"])))


# --- when the records began ---------------------------------------------------------

def records_began(s, tenant_id: int, today: date) -> date:
    """The first day this carrier's files went through the system: its first
    file checked, or its first file received on the calendar, whichever came
    first — today when neither has happened yet. The whole carrier's, whatever
    programme or broker is picked: a broker with nothing yet sent nothing.

    A period that ended before this day has no records (`has_data` False, read
    "Not available"): the calendar has due dates going back to each contract's
    start, but files sent before the carrier used the system never came through
    it, and the check and fix records only begin with it. Counting those
    periods would show zeros, or files "never arrived", that are not true."""
    from sqlalchemy import func
    import check_record as cr
    from db import CheckRun, ExpectedSubmission
    checked = (s.query(func.min(CheckRun.created_at))
               .filter(CheckRun.tenant_id == tenant_id,
                       CheckRun.lane.in_((cr.INTAKE, cr.DIRECT, cr.CANONICAL))).scalar())
    received = (s.query(func.min(ExpectedSubmission.received_at))
                .filter(ExpectedSubmission.tenant_id == tenant_id).scalar())
    days = [d.date() if isinstance(d, datetime) else d for d in (checked, received) if d]
    return min(days) if days else today


# --- the pack ---------------------------------------------------------------------

def pack(s, tenant_id: int, month: str, *, program_id: Optional[int] = None,
         broker_party_id: Optional[int] = None, now: Optional[datetime] = None) -> dict:
    """The month, last month and the same month last year, each measured the
    same way, plus the month by broker and programme. A month that ended before
    the records began (records_began) has no data."""
    import check_record
    import fix_performance as fp
    now = now or datetime.utcnow()
    last_month, last_year = shift(month, -1), shift(month, -12)
    months = sorted({month, last_month, last_year})
    began = records_began(s, tenant_id, now.date())

    target = fp.target_days(s, tenant_id)
    timely, timely_by = _timeliness(s, tenant_id, set(months), program_id=program_id,
                                    broker_party_id=broker_party_id, today=now.date())
    probs = fp.problems(s, tenant_id, program_id=program_id, broker_party_id=broker_party_id)

    measured: dict[str, dict] = {}
    quality_by: list = []
    for k in months:
        start, end = check_record.month_bounds(k)
        volumes, quality, by_bp = _quality(s, tenant_id, k, program_id=program_id,
                                           broker_party_id=broker_party_id)
        fixes = _fixes(probs, start, end, target, now)
        t = timely[k]
        measured[k] = {
            "timeliness": t, "volumes": volumes, "quality": quality, "fixes": fixes,
            # Records for it: it ends after they began. A real 0 after that reads 0.
            "has_data": end.date() > began,
            # Still running: its numbers will move until it ends.
            "complete": end <= now,
        }
        if k == month:
            quality_by = by_bp

    # One line per broker and programme for the month itself.
    start, end = check_record.month_bounds(month)
    by_bp = _by_broker_programme(timely_by.get(month) or {}, quality_by, probs,
                                 start, end, target, now)

    return {
        "month": month, "last_month": last_month, "last_year": last_year,
        "target_days": target, "as_of": now.date().isoformat(),
        "records_began": began.isoformat(),
        "months": measured, "by_broker_programme": by_bp,
    }


def name_rows(s, out: dict) -> dict:
    """Put the broker's and programme's names on each line of the pack — the
    same words on screen, in the file and in the email — and on the lines of
    the errors still waiting when the pack carries them (add_file_detail), with
    who has to act: the broker, or the carrier for a file no broker sent."""
    from db import Party, Program
    waiting = out.get("waiting") or {}
    rows = [*out["by_broker_programme"], *(waiting.get("by_owner") or []),
            *(waiting.get("groups") or [])]
    broker_ids = {r["broker_party_id"] for r in rows} - {None}
    prog_ids = {r["program_id"] for r in rows} - {None}
    brokers = dict(s.query(Party.id, Party.legal_name)
                   .filter(Party.id.in_(broker_ids)).all()) if broker_ids else {}
    progs = dict(s.query(Program.id, Program.name)
                 .filter(Program.id.in_(prog_ids)).all()) if prog_ids else {}
    for r in rows:
        b, p = r["broker_party_id"], r["program_id"]
        r["broker_name"] = brokers.get(b) or ("No broker" if not b else f"Broker {b}")
        r["programme_name"] = progs.get(p) or ("No programme" if not p else f"Programme {p}")
        r["owner"] = (brokers.get(b) or f"Broker {b}") if b else "Carrier"
    return out


def add_file_detail(s, tenant_id: int, out: dict, *, program_id: Optional[int] = None,
                    broker_party_id: Optional[int] = None,
                    now: Optional[datetime] = None) -> dict:
    """What the PDF and Excel carry beyond the screen's Overview: the checks
    that failed most in the period (40.1, the Data Quality tab's list) and the
    errors still waiting as at `now` — how old, and who has to act (41.3, the
    Exception Resolution tab's numbers). Only the files need it, so the screen
    never waits for it. Call before name_rows."""
    import check_record
    import fix_performance as fp
    now = now or datetime.utcnow()
    span = (out.get("spans") or {}).get(out["month"])
    if span:
        start = _at(date.fromisoformat(span["from"]))
        end = _at(date.fromisoformat(span["to"])) + timedelta(days=1)
    else:
        start, end = check_record.month_bounds(out["month"])
    q = check_record.summary(s, tenant_id, start, end, program_id=program_id,
                             broker_party_id=broker_party_id)["quality"]
    out["top_checks"] = {"errors": q["errors"], "checks": q["top_failing_checks"]}
    probs = fp.problems(s, tenant_id, program_id=program_id, broker_party_id=broker_party_id)
    out["waiting"] = fp.open_now(probs, out["target_days"], now)
    return out


# --- a day, a week, a month or dates picked ---------------------------------------

CADENCES = ("day", "week", "month", "custom")


def _at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def window(cadence: str, period: Optional[str], today: date) -> dict:
    """The dates a report covers, the period it is compared with, and — a month
    or a week — the same period last year. Each is {period, start, end}, `end`
    the day after the last.

    The Executive Report's own window (executive_report.window), so both reports
    cover and compare the same dates for the same pick: a day beside the day
    before, a week (Monday to Sunday) beside the week before, a custom range
    beside as many days just before it. Two rules are the Monthly Report's own:
      * a MONTH with none picked is this month, as the report has always opened;
      * only a MONTH or a WEEK has a same period last year (a week: the Monday
        52 weeks back, so it is the same weekdays). A single day a year ago, or
        an arbitrary range, means little."""
    import executive_report as er
    from fastapi import HTTPException
    if cadence == "month" and not period:
        period = _month_of(today)
    w = er.window(cadence, period, today)
    start, end = w["start"], w["end"]
    if start > today:
        raise HTTPException(400, "Pick a date that has already begun.")
    previous = w["previous"]
    last_year = None
    if cadence == "month":
        from check_record import month_bounds
        k = shift(w["period"], -12)
        a, b = month_bounds(k)
        last_year = {"period": k, "start": a.date(), "end": b.date()}
    elif cadence == "week":
        a = start - timedelta(weeks=52)
        last_year = {"period": a.isoformat(), "start": a, "end": a + timedelta(days=7)}
    return {"cadence": cadence, "period": w["period"], "start": start, "end": end,
            "previous": previous, "last_year": last_year}


def period_title(cadence: str, period: str) -> str:
    """'October 2026', 'Week of 9 Nov 2026', '17 Nov 2026', '1 – 18 Nov 2026' —
    the Executive Report's words for the same dates."""
    import executive_delivery as ed
    return ed.period_title(cadence, period)


def spans(w: dict) -> dict:
    """{period: {from, to, title}} for the window's periods — `to` the last day."""
    out = {}
    for x in (w, w["previous"], w["last_year"]):
        if x:
            out[x["period"]] = {"from": x["start"].isoformat(),
                                "to": (x["end"] - timedelta(days=1)).isoformat(),
                                "title": period_title(w["cadence"], x["period"])}
    return out


def _pack_between(s, tenant_id: int, w: dict, *, program_id, broker_party_id,
                  now: datetime) -> dict:
    """The pack for a day, a week or dates picked: each period measured as a
    month is, over its own dates. Same shape as `pack` — `month`, `last_month`
    and `last_year` name the period, the one before it and the one a year back
    (None for a day or dates picked)."""
    import fix_performance as fp
    target = fp.target_days(s, tenant_id)
    probs = fp.problems(s, tenant_id, program_id=program_id, broker_party_id=broker_party_id)
    began = records_began(s, tenant_id, now.date())

    measured: dict[str, dict] = {}
    timely_by: dict = {}
    quality_by: list = []
    for x in (w, w["previous"], w["last_year"]):
        if x is None:
            continue
        start, end = _at(x["start"]), _at(x["end"])
        t, t_by = _timeliness_between(s, tenant_id, x["start"], x["end"], program_id=program_id,
                                      broker_party_id=broker_party_id, today=now.date())
        volumes, quality, q_by = _quality_between(s, tenant_id, start, end,
                                                  program_id=program_id,
                                                  broker_party_id=broker_party_id)
        fixes = _fixes(probs, start, end, target, now)
        measured[x["period"]] = {
            "timeliness": t, "volumes": volumes, "quality": quality, "fixes": fixes,
            # Records for it: it ends after they began (records_began).
            "has_data": x["end"] > began,
            "complete": end <= now,
        }
        if x is w:
            timely_by, quality_by = t_by, q_by

    by_bp = _by_broker_programme(timely_by, quality_by, probs, _at(w["start"]), _at(w["end"]),
                                 target, now)
    return {
        "month": w["period"], "last_month": w["previous"]["period"],
        "last_year": w["last_year"]["period"] if w["last_year"] else None,
        "target_days": target, "as_of": now.date().isoformat(),
        "records_began": began.isoformat(),
        "months": measured, "by_broker_programme": by_bp,
    }


def pack_for(s, tenant_id: int, cadence: str = "month", period: Optional[str] = None, *,
             program_id: Optional[int] = None, broker_party_id: Optional[int] = None,
             now: Optional[datetime] = None) -> dict:
    """The pack for a day, a week, a month or dates picked. A month is `pack`
    exactly as it always was; anything else is `_pack_between`. Either way the
    pack also says its `cadence`, `period` and `spans` (each period's dates and
    title), so the screen, the files and the email can name them."""
    now = now or datetime.utcnow()
    w = window(cadence, period, now.date())
    if cadence == "month":
        out = pack(s, tenant_id, w["period"], program_id=program_id,
                   broker_party_id=broker_party_id, now=now)
    else:
        out = _pack_between(s, tenant_id, w, program_id=program_id,
                            broker_party_id=broker_party_id, now=now)
    out.update(cadence=cadence, period=w["period"], spans=spans(w))
    return out
