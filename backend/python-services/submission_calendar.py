"""Group 3 — "Never miss a deadline": pure submission-calendar logic.

This module is deliberately DB-free and side-effect-free so it is trivially
testable. It answers three questions:

  1. resolve_schedule()  — do we even have enough to build a calendar? The
     frequency + anchor come from the contract by default (Program.bdx_frequency,
     contract.inception_dt); manual overrides win (C-6). If neither resolves,
     it returns None and NO calendar is produced (we never guess a deadline).
  2. generate_expected() — walk the periods from the anchor by frequency and
     compute each period's due date (period_end + offset).
  3. derive_status()     — given a due date, today, the due-soon window and
     whether a file was received, return the plain status.

Nothing here touches the database; the service layer feeds these functions the
resolved inputs and persists the results.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

FREQUENCIES = {"weekly", "monthly", "quarterly"}

# Accept the handful of spellings a contract / user might use.
_FREQ_ALIASES = {
    "week": "weekly", "weekly": "weekly",
    "month": "monthly", "monthly": "monthly",
    "quarter": "quarterly", "quarterly": "quarterly",
}

# Defaults for the tuning knobs — sensible out of the box, overridable per program.
DEFAULT_DUE_DAY_OF_MONTH = 10  # monthly/quarterly: due on this day of the next month
DEFAULT_DUE_OFFSET_DAYS = 10   # weekly only: due this many days after the period ends
DEFAULT_SOON_WINDOW_DAYS = 5   # how early the "due soon" warning starts

# HOW A DUE DATE IS SET, and why it depends on the frequency.
#
# Monthly and quarterly periods always end on the LAST DAY OF A CALENDAR MONTH
# (see _period_bounds), so the natural way to state their deadline is the one
# brokers actually use: "the bordereau is due on the 10th". That is
# `due_day_of_month` — day N of the month FOLLOWING the period.
#
# It replaced a "days after the period ends" offset, which was the same thing
# said confusingly AND drifted: 31 Jan + 30 days is 2 March, skipping February
# entirely, so a "30 day" rule silently produced a different day-of-month every
# period. A fixed day cannot drift.
#
# Weekly periods are 7-day windows that end on arbitrary dates, several per
# month, so "day of month" is meaningless for them — they keep the offset.

# There is deliberately NO grace period. A deadline is either met or missed: the
# day after the due date the bordereau is overdue, full stop. The old grace_days
# knob split "missed" into Overdue (inside grace) and Late (past it) — two words
# for one state that nobody could tell apart on screen, and a setting that
# quietly moved the deadline the user had just set. The column survives in
# db.SubmissionSchedule so no migration is needed, but nothing reads it.


@dataclass(frozen=True)
class ResolvedSchedule:
    frequency: str
    anchor: date
    # Monthly/quarterly deadline. None falls back to due_offset_days, which is
    # how a schedule saved before this field existed still resolves.
    due_day_of_month: Optional[int] = None
    due_offset_days: int = DEFAULT_DUE_OFFSET_DAYS
    soon_window_days: int = DEFAULT_SOON_WINDOW_DAYS


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _norm_freq(value) -> Optional[str]:
    key = str(value or "").strip().lower()
    return _FREQ_ALIASES.get(key)


def resolve_schedule(
    *,
    frequency_override=None,
    anchor_override=None,
    contract_frequency=None,
    contract_inception=None,
    due_day_of_month=None,
    due_offset_days: int = DEFAULT_DUE_OFFSET_DAYS,
    soon_window_days: int = DEFAULT_SOON_WINDOW_DAYS,
) -> Optional[ResolvedSchedule]:
    """Resolve the effective (frequency, anchor) via override → contract → nothing.

    Returns None when either the frequency or the anchor cannot be resolved — the
    caller then builds no calendar and the UI prompts the user to set it up. We
    never fabricate a deadline from a partial basis.
    """
    freq = _norm_freq(frequency_override) or _norm_freq(contract_frequency)
    anchor = _as_date(anchor_override) or _as_date(contract_inception)
    if freq not in FREQUENCIES or anchor is None:
        return None
    # Clamped to a real day number here rather than at the point of use, so an
    # out-of-range value cannot reach date() and raise mid-generation.
    day = None if due_day_of_month is None else max(1, min(31, int(due_day_of_month)))
    return ResolvedSchedule(
        frequency=freq,
        anchor=anchor,
        due_day_of_month=day,
        due_offset_days=int(due_offset_days),
        soon_window_days=int(soon_window_days),
    )


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + n
    return total // 12, total % 12 + 1


def _period_bounds(frequency: str, anchor: date, i: int) -> tuple[str, date, date]:
    """Return (label, period_start, period_end) for the i-th period from the anchor.

    Monthly/quarterly periods align to the calendar (whole months / whole quarters);
    weekly periods are 7-day windows anchored to the anchor date itself.
    """
    if frequency == "monthly":
        y, m = _add_months(anchor.year, anchor.month, i)
        start = date(y, m, 1)
        end = date(y, m, monthrange(y, m)[1])
        return f"{y:04d}-{m:02d}", start, end
    if frequency == "quarterly":
        # First month of the anchor's calendar quarter, then step i quarters.
        first_month_of_anchor_q = ((anchor.month - 1) // 3) * 3 + 1
        y, m = _add_months(anchor.year, first_month_of_anchor_q, i * 3)
        q = (m - 1) // 3 + 1
        start = date(y, m, 1)
        ly, lm = _add_months(y, m, 2)          # last month of this quarter
        end = date(ly, lm, monthrange(ly, lm)[1])
        return f"{y:04d}-Q{q}", start, end
    if frequency == "weekly":
        start = anchor + timedelta(days=7 * i)
        end = start + timedelta(days=6)
        iso = start.isocalendar()
        return f"{iso[0]:04d}-W{iso[1]:02d}", start, end
    raise ValueError(f"unknown frequency: {frequency!r}")


def due_date_for(resolved: ResolvedSchedule, period_end: date) -> date:
    """When the bordereau for a period ending `period_end` has to be in.

    Monthly/quarterly with a day-of-month set: day N of the month AFTER the
    period. February is the case that matters — a schedule set to the 31st is
    due on the 28th (or 29th) there, because clamping to the month's real last
    day is the only reading of "the 31st" that is always a date. It never spills
    into March, which is exactly the drift the old offset had.

    Everything else (weekly, or a schedule saved before day-of-month existed)
    falls back to the offset.
    """
    if resolved.frequency == "weekly" or resolved.due_day_of_month is None:
        return period_end + timedelta(days=resolved.due_offset_days)
    # period_end is a month's last day, so +1 lands on the 1st of the next month.
    nxt = period_end + timedelta(days=1)
    last = monthrange(nxt.year, nxt.month)[1]
    return date(nxt.year, nxt.month, min(resolved.due_day_of_month, last))


def generate_expected(
    resolved: Optional[ResolvedSchedule],
    start: date,
    end: date,
) -> list[dict]:
    """Produce the expected-submission rows whose period intersects [start, end].

    Returns [] when `resolved` is None (nothing to build yet) or when the window
    is empty.

    NOTE a period CAN begin before the anchor. Monthly and quarterly periods snap
    to whole calendar months/quarters (see _period_bounds), so an anchor of
    15 Oct produces 2025-10 running 1–31 Oct, and an anchor of 20 Nov produces
    2025-Q4 running 1 Oct–31 Dec. Only weekly periods start on the anchor itself.
    The anchor selects which period is FIRST; it does not clip that period.
    """
    if resolved is None or start > end:
        return []
    rows: list[dict] = []
    i = 0
    while True:
        label, p_start, p_end = _period_bounds(resolved.frequency, resolved.anchor, i)
        if p_start > end:
            break
        if p_end >= start:
            rows.append({
                "period": label,
                "period_start": p_start,
                "period_end": p_end,
                "due_date": due_date_for(resolved, p_end),
            })
        i += 1
        if i > 10_000:   # safety valve against a bad frequency/anchor
            break
    return rows


def derive_status(
    due_date: date,
    today: date,
    soon_window_days: int = DEFAULT_SOON_WINDOW_DAYS,
    received_on: Optional[date] = None,
) -> str:
    """Plain status for one expected submission.

    received  → on_time (on/before due) or received_late.
    not yet   → scheduled → due_soon → due_today → overdue.

    Three moments matter to a broker chasing a file, and each gets its own state
    so each can raise its own reminder: a warning some days ahead, a prompt on
    the day itself, and a flag once the date has passed. The due date used to be
    swallowed by `due_soon`, so the one day the file actually had to go out
    looked no different from four days earlier.
    """
    if received_on is not None:
        return "on_time" if received_on <= due_date else "received_late"
    # "due soon" begins exactly soon_window_days before the due date, so anything
    # strictly earlier than that edge is still just scheduled.
    if today < due_date - timedelta(days=soon_window_days):
        return "scheduled"
    if today < due_date:
        return "due_soon"
    if today == due_date:
        return "due_today"
    return "overdue"
