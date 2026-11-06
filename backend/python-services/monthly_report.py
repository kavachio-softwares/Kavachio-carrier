"""MOPR 42 — the monthly pack (42.1) and its comparisons (42.2).

One month's timeliness, volumes, data quality and fixes, beside last month and
the same month last year, the last six months, and one line per broker and
programme. Read entirely from what the other three reports already count —
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
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Optional

TREND_MONTHS = 6


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

def _timeliness(s, tenant_id: int, months: set[str], *, program_id, broker_party_id,
                today: date) -> tuple[dict, dict]:
    """Per month, and per (broker, programme) in each month: the files due."""
    from check_record import month_bounds
    from db import ExpectedSubmission
    from submission_calendar import derive_status
    from submission_case import OPEN

    first = month_bounds(min(months))[0].date()
    last = month_bounds(max(months))[1].date()
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

    def _blank():
        return {"due": 0, "due_so_far": 0, "on_time": 0, "late": 0, "overdue": 0,
                "never": 0, "cases_open": 0, "_late_days": []}

    per_month: dict[str, dict] = defaultdict(_blank)
    per_group: dict[str, dict] = defaultdict(lambda: defaultdict(_blank))
    for e in q.all():
        k = _month_of(e.due_date)
        if k not in months:
            continue
        status = derive_status(e.due_date, today, 0, e.received_at, e.closes_on)
        for t in (per_month[k], per_group[k][(e.broker_party_id, e.program_id)]):
            t["due"] += 1
            if status in ("scheduled", "due_soon", "due_today"):
                continue
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
            if e.case_state in OPEN:
                t["cases_open"] += 1

    def _done(t: dict) -> dict:
        late_days = t.pop("_late_days")
        t["on_time_pct"] = _pct(t["on_time"], t["due_so_far"])
        t["avg_days_late"] = _avg(late_days)
        return t

    return ({k: _done(per_month[k]) for k in months},
            {k: {g: _done(t) for g, t in groups.items()} for k, groups in per_group.items()})


# --- MOPR 40: what came in, and how clean -------------------------------------------

def _quality(s, tenant_id: int, month: str, *, program_id, broker_party_id) -> tuple[dict, dict, list]:
    import check_record
    start, end = check_record.month_bounds(month)
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


# --- the pack ---------------------------------------------------------------------

def pack(s, tenant_id: int, month: str, *, program_id: Optional[int] = None,
         broker_party_id: Optional[int] = None, now: Optional[datetime] = None) -> dict:
    """The month, last month, the same month last year and the last six months,
    each measured the same way, plus the month by broker and programme."""
    import check_record
    import fix_performance as fp
    now = now or datetime.utcnow()
    last_month, last_year = shift(month, -1), shift(month, -12)
    trend = [shift(month, -i) for i in range(TREND_MONTHS)]
    months = sorted(set(trend) | {last_year})

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
            "has_data": bool(t["due"] or volumes["files"] or fixes["fixed"] or fixes["waiting"]),
            # Still running: its numbers will move until it ends.
            "complete": end <= now,
        }
        if k == month:
            quality_by = by_bp

    # One line per broker and programme for the month itself.
    start, end = check_record.month_bounds(month)
    rows: dict[tuple, dict] = {}

    def _row(b, p):
        return rows.setdefault((b, p), {"broker_party_id": b, "program_id": p,
                                        "due_so_far": 0, "on_time": 0, "on_time_pct": None,
                                        "files": 0, "clean_first_time": 0,
                                        "clean_first_time_pct": None, "errors": 0,
                                        "avg_fix_days": None, "waiting_past_target": 0})
    for (b, p), t in (timely_by.get(month) or {}).items():
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
    by_bp = sorted(rows.values(), key=lambda r: (-r["waiting_past_target"], -r["errors"],
                                                 -(r["due_so_far"] - r["on_time"])))

    return {
        "month": month, "last_month": last_month, "last_year": last_year,
        "target_days": target, "as_of": now.date().isoformat(),
        "months": measured, "trend": trend, "by_broker_programme": by_bp,
    }


def name_rows(s, out: dict) -> dict:
    """Put the broker's and programme's names on each line of the pack — the
    same words on screen, in the file and in the email."""
    from db import Party, Program
    rows = out["by_broker_programme"]
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
    return out
