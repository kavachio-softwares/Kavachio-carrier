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

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

FREQUENCIES = {"weekly", "monthly", "quarterly", "half_yearly", "yearly"}

# Accept the handful of spellings a contract / user might use.
#
# The stored tokens are the five above; everything else here is an INBOUND
# spelling. The half-yearly and yearly aliases matter more than the others,
# because programmes were being created with "semi-annual" / "annual" /
# "annually" long before this module could build a calendar from them — those
# rows exist in the database today and have to keep resolving without a data
# migration.
_FREQ_ALIASES = {
    "week": "weekly", "weekly": "weekly",
    "month": "monthly", "monthly": "monthly",
    "quarter": "quarterly", "quarterly": "quarterly",
    # half-yearly, and the many ways a contract writes it
    "half_yearly": "half_yearly", "half-yearly": "half_yearly",
    "half yearly": "half_yearly", "halfyearly": "half_yearly",
    "half_year": "half_yearly", "half-year": "half_yearly",
    "semi_annual": "half_yearly", "semi-annual": "half_yearly",
    "semi annual": "half_yearly", "semiannual": "half_yearly",
    "semi-annually": "half_yearly", "semiannually": "half_yearly",
    "biannual": "half_yearly", "bi-annual": "half_yearly",
    "six_monthly": "half_yearly", "six-monthly": "half_yearly",
    # yearly
    "yearly": "yearly", "year": "yearly",
    "annual": "yearly", "annually": "yearly",
}

# Defaults for the tuning knobs — sensible out of the box, overridable per program.
DEFAULT_DUE_DAY_OF_MONTH = 10  # monthly/quarterly: due on this day of the next month
DEFAULT_DUE_OFFSET_DAYS = 10   # weekly only: due this many days after the period ends
DEFAULT_SOON_WINDOW_DAYS = 5   # how early the "due soon" warning starts

# How far ahead a calendar is built, per frequency. A flat twelve months was
# right while every schedule was monthly or quarterly; on a YEARLY programme it
# produces one or two rows and the screen looks broken. The number of PERIODS a
# reader can see ahead is what should stay roughly constant, not the number of
# months, so the horizon scales with the period length.
_HORIZON_MONTHS = {
    "weekly": 12, "monthly": 12, "quarterly": 12,
    "half_yearly": 24, "yearly": 36,
}
DEFAULT_HORIZON_MONTHS = 12


def horizon_months_for(frequency: Optional[str]) -> int:
    """Months of calendar to build ahead of today for this frequency."""
    return _HORIZON_MONTHS.get(_norm_freq(frequency) or "", DEFAULT_HORIZON_MONTHS)

# HOW A DUE DATE IS SET, and why it depends on the frequency.
#
# Monthly, quarterly, half-yearly and yearly periods all end on the LAST DAY OF
# A CALENDAR MONTH (see _period_bounds), so the natural way to state their
# deadline is the one brokers actually use: "the bordereau is due on the 10th".
# That is `due_day_of_month` — day N of the month FOLLOWING the period. A yearly
# programme ending 31 Dec is therefore due on 10 Jan, which is how a treaty
# actually reads.
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
    if frequency == "half_yearly":
        # First month of the anchor's calendar half (Jan or Jul), then step i halves.
        first_month_of_anchor_h = 1 if anchor.month <= 6 else 7
        y, m = _add_months(anchor.year, first_month_of_anchor_h, i * 6)
        h = 1 if m <= 6 else 2
        start = date(y, m, 1)
        ly, lm = _add_months(y, m, 5)          # last month of this half
        end = date(ly, lm, monthrange(ly, lm)[1])
        return f"{y:04d}-H{h}", start, end
    if frequency == "yearly":
        y = anchor.year + i
        return f"{y:04d}", date(y, 1, 1), date(y, 12, 31)
    if frequency == "weekly":
        start = anchor + timedelta(days=7 * i)
        end = start + timedelta(days=6)
        iso = start.isocalendar()
        return f"{iso[0]:04d}-W{iso[1]:02d}", start, end
    raise ValueError(f"unknown frequency: {frequency!r}")


def period_index_for(frequency: str, anchor: date, d: date) -> int:
    """Which period number (0-based, from the anchor) contains `d`.

    Negative when `d` falls before the calendar starts. Computed arithmetically
    rather than by walking periods, so answering "which period is this file
    for?" costs the same whether the anchor is last month or ten years ago.
    """
    if frequency == "weekly":
        return (d - anchor).days // 7
    if frequency == "monthly":
        return (d.year * 12 + d.month - 1) - (anchor.year * 12 + anchor.month - 1)
    if frequency == "quarterly":
        return (d.year * 4 + (d.month - 1) // 3) - (anchor.year * 4 + (anchor.month - 1) // 3)
    if frequency == "half_yearly":
        return (d.year * 2 + (d.month - 1) // 6) - (anchor.year * 2 + (anchor.month - 1) // 6)
    if frequency == "yearly":
        return d.year - anchor.year
    raise ValueError(f"unknown frequency: {frequency!r}")


def period_for_date(resolved: Optional[ResolvedSchedule], d: date) -> Optional[str]:
    """The label of the period that `d` falls inside — e.g. 2026-07, 2026-Q3,
    2026-H2, 2026, 2026-W28.

    This is what makes a LATE file belong to its own period rather than to the
    period it turned up in. A July bordereau that arrives in September is still
    July's: pass the date the file COVERS (not the date it arrived) and this
    names the row it satisfies.

    None when there is no schedule, or when `d` is before the calendar starts.
    """
    if resolved is None:
        return None
    i = period_index_for(resolved.frequency, resolved.anchor, d)
    if i < 0:
        return None
    return _period_bounds(resolved.frequency, resolved.anchor, i)[0]


def due_date_for(resolved: ResolvedSchedule, period_end: date) -> date:
    """When the bordereau for a period ending `period_end` has to be in.

    Any calendar-aligned frequency with a day-of-month set: day N of the month AFTER the
    period. February is the case that matters — a schedule set to the 31st is
    due on the 28th (or 29th) there, because clamping to the month's real last
    day is the only reading of "the 31st" that is always a date. It never spills
    into March, which is exactly the drift the old offset had.

    Everything else (weekly, whose periods end on arbitrary dates, or a schedule
    saved before day-of-month existed) falls back to the offset.
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

    NOTE a period CAN begin before the anchor. Every frequency except weekly
    snaps to whole calendar units (see _period_bounds), so an anchor of 15 Oct
    produces 2025-10 running 1–31 Oct, an anchor of 20 Nov produces 2025-Q4
    running 1 Oct–31 Dec, and an anchor of 3 Sep produces 2025-H2 running
    1 Jul–31 Dec and 2025 running 1 Jan–31 Dec. Only weekly periods start on the
    anchor itself. The anchor selects which period is FIRST; it does not clip
    that period.
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


_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Ordered most-specific first: "2026-Q3" must not be read as the bare year 2026.
# Each pattern yields a date INSIDE the period the text names — never the period
# label itself, because the label depends on the programme's frequency and this
# function does not know it. period_for_date() does that translation.
_PERIOD_PATTERNS: list[tuple[str, str]] = [
    (r"(?<!\d)(20\d{2})[-_ ]?q([1-4])(?!\d)", "yq"),      # 2026-Q3, 2026Q3
    (r"(?<!\d)q([1-4])[-_ ]?(20\d{2})(?!\d)", "qy"),      # Q3-2026, Q3 2026
    (r"(?<!\d)(20\d{2})[-_ ]?h([12])(?!\d)", "yh"),       # 2026-H2
    (r"(?<!\d)h([12])[-_ ]?(20\d{2})(?!\d)", "hy"),       # H2-2026
    (r"(?<!\d)(20\d{2})[-_](0[1-9]|1[0-2])(?!\d)", "ym"), # 2026-07, 2026_07
    (r"(?<!\d)(0[1-9]|1[0-2])[-_](20\d{2})(?!\d)", "my"), # 07-2026
    (r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?!\d)", "ym"),     # 202607
    (r"([a-z]{3,9})[-_ ]?(20\d{2})(?!\d)", "ny"),          # July2026, Jul-2026
    (r"(?<!\d)(20\d{2})[-_ ]?([a-z]{3,9})", "yn"),         # 2026-July
    (r"(?<!\d)(20\d{2})(?!\d)", "y"),                     # bare 2026 — last resort
]

# A quarter or half marker that did NOT validate — "Q5", "H3". Its presence
# means the name was TRYING to state a period and got it wrong, so the bare-year
# fallback must not quietly read "Q5-2026" as the year 2026 and mark January
# delivered. Better to admit we cannot tell.
_SPOILED_MARKER = re.compile(r"(?<![a-z])[qh]\s*\d")


def parse_period_hint(text: Optional[str]) -> Optional[date]:
    """Read a reporting period out of free text — usually a filename.

    Returns a date that falls INSIDE the period the text names, which
    period_for_date() then turns into the right label for the programme's own
    frequency. So "SpectrumBDX_2026-07.xlsx" gives 1 Jul 2026, which is 2026-07
    on a monthly programme and 2026-Q3 on a quarterly one — the same file, read
    correctly by both.

    Deliberately conservative: it recognises the shapes brokers actually name
    files with and returns None for anything else, so an unreadable name falls
    back to the caller's own rule instead of guessing a period wrong. A wrong
    period is worse than no period — it marks the wrong month delivered.
    """
    if not text:
        return None
    low = str(text).lower()
    for pattern, kind in _PERIOD_PATTERNS:
        m = re.search(pattern, low)
        if not m:
            continue
        try:
            if kind == "yq":
                return date(int(m.group(1)), (int(m.group(2)) - 1) * 3 + 1, 1)
            if kind == "qy":
                return date(int(m.group(2)), (int(m.group(1)) - 1) * 3 + 1, 1)
            if kind == "yh":
                return date(int(m.group(1)), 1 if m.group(2) == "1" else 7, 1)
            if kind == "hy":
                return date(int(m.group(2)), 1 if m.group(1) == "1" else 7, 1)
            if kind == "ym":
                return date(int(m.group(1)), int(m.group(2)), 1)
            if kind == "my":
                return date(int(m.group(2)), int(m.group(1)), 1)
            if kind in ("ny", "yn"):
                name, year = (m.group(1), m.group(2)) if kind == "ny" else (m.group(2), m.group(1))
                month = _MONTH_NAMES.get(name)
                if month is None:
                    continue        # a word that is not a month — keep looking
                return date(int(year), month, 1)
            if kind == "y":
                if _SPOILED_MARKER.search(low):
                    return None
                return date(int(m.group(1)), 1, 1)
        except ValueError:
            continue
    return None


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
