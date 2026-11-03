"""
Dated checks: how a mid-term change splits one bordereau between two versions
of a rule (Kavachio_Mid_Term_Change_Plan.md §6.1, §8).

A mid-term change never overwrites a rule. When it goes live, the rule it
changes is CLOSED (effective_to = the change's effective date, exclusive) and
the new version is OPENED (effective_from = that date, source_amendment_id =
the change). Each bordereau row is then checked only by the version in force
on that row's transaction effective date — the column the setup names in
pipeline.governing_date_field.

A rule is DATED when a change touched it:
  * effective_to set           → it stops applying on that date;
  * source_amendment_id set    → it starts applying on its effective_from.
Every other rule is undated and runs over every row, exactly as before.

The trap this module exists to avoid: validation_rule.effective_from defaults
to CURRENT_DATE, so every rule written before mid-term changes existed carries
its CREATION date there. Reading that as a start date would silently stop
checking every row dated before the rule was made. The start bound therefore
counts only on a rule a change opened (source_amendment_id set).

Pure functions, no database — duckdb_validation and the callers ask these.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable, Optional

Window = tuple  # (Optional[date] from — inclusive, Optional[date] to — exclusive)

# What a row with no readable transaction date is told, per dated check (D2).
UNDATED_KIND = "undated_rows"
# A dated check whose setup names no transaction date column at all.
NO_DATE_COLUMN_KIND = "no_date_column"


def as_date(value) -> Optional[dt.date]:
    """A date from a date, a datetime or 'YYYY-MM-DD…'; None otherwise."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def window_of(rule: dict) -> Optional[Window]:
    """(from, to) for a dated rule, None for an undated one — see the module
    docstring for why effective_from alone never makes a rule dated."""
    opened = rule.get("source_amendment_id") is not None
    start = as_date(rule.get("effective_from")) if opened else None
    end = as_date(rule.get("effective_to"))
    if start is None and end is None:
        return None
    return (start, end)


def covers(window: Optional[Window], day: Optional[dt.date]) -> bool:
    """Whether a rule with this window applies on `day` (from ≤ day < to).
    An undated rule applies every day; a dated one never applies to no day."""
    if window is None:
        return True
    if day is None:
        return False
    start, end = window
    return (start is None or day >= start) and (end is None or day < end)


def _fmt(d: dt.date) -> str:
    return f"{d.day} {d:%b %Y}"


def last_day(window: Window) -> Optional[dt.date]:
    """The last day a window covers — the day before its exclusive end."""
    end = window[1]
    return end - dt.timedelta(days=1) if end else None


def label(window: Optional[Window]) -> Optional[str]:
    """How a dated check is named beside its title: 'until 15 Aug 2026',
    'from 16 Aug 2026', or '16 Aug 2026 – 30 Sep 2026'. None when undated."""
    if window is None:
        return None
    start, _ = window
    last = last_day(window)
    if start and last:
        return f"{_fmt(start)} – {_fmt(last)}"
    if start:
        return f"from {_fmt(start)}"
    return f"until {_fmt(last)}"


def total_version_for(rules: Iterable[dict], day: Optional[dt.date]) -> list[dict]:
    """D4: of the dated TOTAL checks passed in, the ones in force on `day` (the
    last day of the reporting period). A total compares the whole file against
    one limit, so splitting its rows would let a breach through; it runs
    unwindowed, as the version in force when the period closed. With no day
    known, none can be chosen and the caller reports them as not checked."""
    if day is None:
        return []
    return [r for r in rules if covers(window_of(r), day)]


def window_predicate(date_expr: str, window: Window) -> str:
    """The SQL condition a row must meet to be checked by a rule with this
    window. `date_expr` is the compiler's tolerant VARCHAR→DATE expression for
    the transaction date column, so a row is dated exactly as a rule reads a
    date. A row whose date cannot be read meets no window."""
    start, end = window
    parts = [f"{date_expr} IS NOT NULL"]
    if start is not None:
        parts.append(f"{date_expr} >= DATE '{start.isoformat()}'")
    if end is not None:
        parts.append(f"{date_expr} < DATE '{end.isoformat()}'")
    return " AND ".join(parts)


def date_column_from_structure(structure: dict, canonical: str =
                               "premium_transaction_effective_date") -> Optional[str]:
    """The output column the template maps to the transaction effective date —
    what the setup step pre-fills, and what a run with no saved choice falls
    back to. None when no column (or more than one name) carries the tag:
    guessing between two columns is exactly the mistake a dated check can't
    afford, so the carrier picks."""
    names = []
    for sh in (structure or {}).get("sheets") or []:
        for c in sh.get("columns") or []:
            if (c.get("canonical_field") or "") == canonical and c.get("column_name"):
                if c["column_name"] not in names:
                    names.append(c["column_name"])
    return names[0] if len(names) == 1 else None
