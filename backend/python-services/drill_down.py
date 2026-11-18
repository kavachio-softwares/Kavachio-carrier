"""
drill_down.py
─────────────
Executive Reporting 38.1: click a number on a report and see the policies
behind it.

A number names where it came from — which report, which window, which
measure, which programme or broker — and `resolve` works out, from the SAME
selection the report counted it from, exactly which bordereau rows were behind
it: {file id: the policy keys counted in it, or None for every row}. Policy
Search (policy_search.search, `drill`) then lists those policies and nothing
else. Nothing is counted a second way: the files come from the report's own
code (financial_summary._build, check_record.first_runs), never from a
re-written copy of its rules.

  financial   a Financial Summary figure — a window (this period, the one
              before, the same one last year), optionally one metric and one
              currency group. The rows behind it are the rows that window
              added up; with a metric, only the rows that gave that figure a
              value (reported or calculated).
  monthly     a Monthly Report number for one month — files and rows handled,
              new / renewed / cancelled policies, errors and critical errors,
              files clean first time. Counted from the files first checked in
              that month, as they first arrived.

What it cannot show it says: a row with no policy number is a row, not a
policy, so it is counted in `rows` but appears in no policy.

Every function takes the caller's session; none opens one. Reading never writes.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from fastapi import HTTPException

import policy_search as ps

SOURCES = ("financial", "monthly")

# Monthly Report numbers that have policies behind them.
MONTHLY = ("files", "rows", "new", "renewed", "cancelled", "errors", "critical", "clean")
MONTHLY_TXN = {"new": "new", "renewed": "renewed", "cancelled": "cancelled"}

_MONTH = re.compile(r"\d{4}-\d{2}")
_SPAN = re.compile(r"\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}")


def period_name(label: Optional[str]) -> str:
    """'2026-10' → 'October 2026', '2026-Q3' → 'Q3 2026', '2026' → '2026'."""
    p = (label or "").strip().upper()
    if re.fullmatch(r"\d{4}-\d{2}", p):
        return dt.date(int(p[:4]), int(p[5:]), 1).strftime("%B %Y")
    if re.fullmatch(r"\d{4}-Q[1-4]", p):
        return f"{p[5:]} {p[:4]}"
    return p


def _add(within: dict, export_id: Optional[int], key: Optional[str]) -> None:
    if export_id is None or key is None:
        return
    keys = within.setdefault(export_id, set())
    if keys is not None:
        keys.add(key)


def _financial(s, p, *, basis: str, period: Optional[str], window: str,
               metric: Optional[str], currency: Optional[str],
               program_id: Optional[int], broker_party_id: Optional[int],
               carrier_id: Optional[int], open_problems: str) -> dict:
    import financial_figures as ff
    import financial_summary as fs
    if window not in fs.WINDOWS:
        raise HTTPException(400, f"window must be one of {', '.join(fs.WINDOWS)}")
    if metric is not None and metric not in fs.METRICS:
        raise HTTPException(400, f"metric must be one of {', '.join(fs.METRICS)}")
    b = fs._build(s, p, basis=basis, period=period, program_id=program_id,
                  broker_party_id=broker_party_id, carrier_id=carrier_id,
                  open_problems=open_problems)
    gave = (ff.REPORTED, ff.CALCULATED)
    within: dict = {}
    rows = 0
    seen_files = set()
    for o, _cells, fig in b["rows"].get(window, []):
        if currency and fs._currency_key(fig) != currency:
            continue
        if metric and fig[metric]["status"] not in gave:
            continue
        rows += 1
        seen_files.add(o.id)
        number = fig.get("policy")
        _add(within, o.id, ps.policy_key(number) if number not in (None, "") else None)
    files = len(seen_files)
    return {"source": "financial", "period": b["wins"].get(window),
            "period_name": period_name(b["wins"].get(window)),
            "files": files, "rows": rows, "within": within}


def _monthly(s, p, *, month: Optional[str], measure: Optional[str],
             program_id: Optional[int], broker_party_id: Optional[int]) -> dict:
    import check_record as cr
    from db import CheckError
    if p.role != "carrier_admin" or p.tenant_id is None:
        raise HTTPException(403, "The Monthly Report is for carriers.")
    if measure not in MONTHLY:
        raise HTTPException(400, f"measure must be one of {', '.join(MONTHLY)}")
    if month and _SPAN.fullmatch(month):
        # A day, a week or dates picked on the Monthly Report: 'from..to',
        # both days included — the files first checked on those days.
        try:
            a, b = (dt.date.fromisoformat(x) for x in month.split(".."))
        except ValueError:
            raise HTTPException(400, "Pick real dates.")
        if b < a:
            raise HTTPException(400, "The to date must be on or after the from date.")
        start = dt.datetime(a.year, a.month, a.day)
        end = dt.datetime(b.year, b.month, b.day) + dt.timedelta(days=1)
        import monthly_report as mr
        name = mr.period_title("custom", month)
    elif month and _MONTH.fullmatch(month):
        start, end = cr.month_bounds(month)
        name = period_name(month)
    else:
        raise HTTPException(400, "month must be YYYY-MM")
    files = cr.first_runs(cr.runs_in(s, int(p.tenant_id), start, end, program_id=program_id,
                                     broker_party_id=broker_party_id))
    within: dict = {}
    if measure in ("files", "rows"):
        within = {f.export_id: None for f in files if f.export_id}
    elif measure == "clean":
        within = {f.export_id: None for f in files if f.export_id and cr.straight_through(f)}
    elif measure in ("errors", "critical"):
        by_run = {f.id: f.export_id for f in files if f.export_id}
        q = s.query(CheckError.run_id, CheckError.policy_number, CheckError.severity).filter(
            CheckError.run_id.in_(by_run or {0}))
        for run_id, number, sev in q:
            if measure == "critical" and cr._sev(sev) != "critical":
                continue
            if number not in (None, ""):
                _add(within, by_run[run_id], ps.policy_key(number))
    else:
        # New / renewed / cancelled: the rows of those files whose transaction
        # type falls in that group, read the way the check read it.
        want = MONTHLY_TXN[measure]
        seat = ps.seat_of(s, p)
        exports = ps._exports(s, seat, ids={f.export_id for f in files if f.export_id})
        structures = ps._structures(s, {o.template_id for o in exports})
        for o in exports:
            for ln in ps._lines_of(s, o, structures.get(o.template_id)):
                if cr._GROUP_OF.get(cr.txn_type(ln["transaction"])) == want:
                    _add(within, o.id, ps.policy_key(ln["policy"]))
    return {"source": "monthly", "period": month, "period_name": name,
            "files": len(within), "rows": None, "within": within}


def resolve(s, p, *, source: Optional[str], label: Optional[str] = None,
            basis: str = "month", period: Optional[str] = None,
            window: str = "current", metric: Optional[str] = None,
            currency: Optional[str] = None, open_problems: str = "include",
            month: Optional[str] = None, measure: Optional[str] = None,
            program_id: Optional[int] = None, broker_party_id: Optional[int] = None,
            carrier_id: Optional[int] = None) -> Optional[dict]:
    """The rows behind one number, for policy_search.search(drill=…); None
    when no number was clicked. `label` is what the screen called the number,
    shown back above the list."""
    if not source:
        return None
    if source not in SOURCES:
        raise HTTPException(400, f"from must be one of {', '.join(SOURCES)}")
    if source == "financial":
        out = _financial(s, p, basis=basis, period=period, window=window, metric=metric,
                         currency=currency, program_id=program_id,
                         broker_party_id=broker_party_id, carrier_id=carrier_id,
                         open_problems=open_problems)
    else:
        out = _monthly(s, p, month=month, measure=measure, program_id=program_id,
                       broker_party_id=broker_party_id)
    out["label"] = " ".join((label or "").split())[:80] or None
    return out
