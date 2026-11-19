"""
executive_report.py
───────────────────
Executive Reporting 38.2: the Executive Report — a short read of a carrier's
whole book for one day, week or month, or any dates picked, to look at, download,
or email to anyone the carrier names.

ONE PACK, NOTHING RE-COUNTED
  Every number is taken from the code that counts it for its own screen, so
  the report and those screens always agree:

    money          financial_summary.summary  gross premium, commission, and
                                              the top programmes and brokers
    operations     check_record.summary       files processed, errors, files
                                              clean first time
    timeliness     monthly_report._timeliness files due and on time
    problems       open_problems              everything open, and the
                                              escalations among it
    portfolio      program_stage              programmes active, in run-off,
                                              not yet live, as at the last day
    trend          check_record.summary       errors and clean files inside
                                              the period: by hour for a day,
                                              by day up to 31 days, by week
                                              up to 26 weeks, else by month
    reinsurance    ceded_routes               statements due in the period and
                                              whether each was sent on time
    quality        check_record.summary       files clean / with errors / not
                                              checked, the checks that failed
                                              most, quality by broker and by
                                              programme, worst first
    late           submission_calendar        files due in the period that came
                                              late or had not come by its end
    scorecard      check_record.summary,      per broker and per programme:
                   submission_calendar,       premium, files, on time, clean
                   the money section          first time, late or missing, with
                                              the period before
    history        check_record.summary       37.2: errors and clean files over
                                              the 14 days / 12 weeks / 12 months
                                              up to the period's end
    errors_by_     check_record.summary       37: programmes with the most
    programme                                 errors, split by severity
    contracts      contract, Financial        contracts bound in the period that
                   Summary's reader           sent files and that sent none;
                                              premium written over each term
                                              against its premium cap

DAY, WEEK, MONTH OR CUSTOM
  day     one day (default TODAY, so far), against the day before — and ONLY
          what happened in it (see DAILY below)
  week    Monday to Sunday (default last week), against the week before
  month   one month (default last month), against the month before
  custom  any dates picked, 'YYYY-MM-DD..YYYY-MM-DD', as long as the user
          wants, against the same number of days just before
  Premium exists only by bordereau month, and files are due by the month, so
  for anything shorter than a month premium and timeliness are for the month
  the period ends in, so far.

AS AT THE PERIOD'S LAST DAY
  Programmes, Needs attention and Reinsurance follow the period picked: they
  are worked out as they stood at the end of its last day (`as_at`), from the
  records' own dates — when a problem was found and fixed, each late-file case
  move (submission_case_event), when a held file was released, when each
  statement was sent. A period running to today is as of now.
  Two limits. A contract's past STATUS is not kept, so a programme's stage is
  its contract's status today with its term judged against `as_at`. And when
  a contract started waiting for a signature is not kept, so signatures count
  only when the period runs to today.

DAILY: ONLY WHAT HAPPENED THAT DAY
  A daily report counts activity, not standing positions: premium in the
  bordereaux processed that day (not by reporting month), files due that day,
  problems that started that day, problems that became escalations that day,
  programmes that were added, went live or went into run-off that day, and
  errors hour by hour. Nothing from before the day is carried in.

ESCALATIONS
  Open problems that need someone to step in: a late file whose case is
  Delinquent, a dispute waiting for the carrier's decision, and anything open
  past its due date. Oldest first.

REINSURANCE STATEMENTS
  Read-only: the statements due in the period (their due date falls in it),
  each "Sent on time", "Sent late" or "Not sent" as at its end; Live lists
  every prepared statement not yet sent. Preparing the ones now due is the
  Reinsurance screen's job (ceded_routes.prepare_due writes), never a report's.

Every function takes the caller's session; none opens one. Reading never writes.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

CADENCES = ("day", "week", "month", "custom")
TOP = 5
ESCALATIONS_SHOWN = 25     # in the files
ESCALATIONS_KEPT = 500     # in the pack: the screen pages through them all

_MONTH = re.compile(r"\d{4}-\d{2}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


# ── the window ──────────────────────────────────────────────────────────────
def _month_key(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _shift_month(key: str, n: int) -> str:
    y, m = divmod(int(key[:4]) * 12 + int(key[5:7]) - 1 + n, 12)
    return f"{y:04d}-{m + 1:02d}"


def _month_start(key: str) -> dt.date:
    return dt.date(int(key[:4]), int(key[5:7]), 1)


def window(cadence: str, period: Optional[str], today: dt.date) -> dict:
    """The dates a report covers and the ones it is compared with.

    month   `period` 'YYYY-MM'; none = the last complete month.
    week    `period` any day of the week, 'YYYY-MM-DD'; the week runs Monday to
            Sunday; none = the last complete week.
    day     `period` 'YYYY-MM-DD'; none = yesterday.
    custom  `period` 'YYYY-MM-DD..YYYY-MM-DD', both days included.
    `money_month` is the bordereau month premium and timeliness are read for;
    `as_at` the last day the position sections are worked out for."""
    from fastapi import HTTPException
    if cadence not in CADENCES:
        raise HTTPException(400, f"cadence must be one of {', '.join(CADENCES)}")
    if cadence == "month":
        if period and not _MONTH.fullmatch(period):
            raise HTTPException(400, "period must be YYYY-MM for a monthly report")
        key = period or _shift_month(_month_key(today), -1)
        start, end = _month_start(key), _month_start(_shift_month(key, 1))
        prev = _shift_month(key, -1)
        return {"cadence": "month", "period": key, "start": start, "end": end,
                "previous": {"period": prev, "start": _month_start(prev), "end": start},
                "money_month": key, "complete": end <= today,
                "as_at": min(end - dt.timedelta(days=1), today)}
    if cadence == "custom":
        a, sep, b = (period or "").partition("..")
        if not (sep and _DAY.fullmatch(a) and _DAY.fullmatch(b)):
            raise HTTPException(400, "Pick a from date and a to date.")
        start, last = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
        if last < start:
            raise HTTPException(400, "The to date must be on or after the from date.")
        if last > today:
            raise HTTPException(400, "The to date cannot be after today.")
        days = (last - start).days + 1
        end = last + dt.timedelta(days=1)
        pstart = start - dt.timedelta(days=days)
        prev_last = start - dt.timedelta(days=1)
        return {"cadence": "custom", "period": f"{start.isoformat()}..{last.isoformat()}",
                "start": start, "end": end,
                "previous": {"period": f"{pstart.isoformat()}..{prev_last.isoformat()}",
                             "start": pstart, "end": start},
                "money_month": _month_key(last), "complete": last < today, "as_at": last}
    if cadence == "day":
        if period and not _DAY.fullmatch(period):
            raise HTTPException(400, "period must be YYYY-MM-DD for a daily report")
        day = dt.date.fromisoformat(period) if period else today
        before = day - dt.timedelta(days=1)
        return {"cadence": "day", "period": day.isoformat(), "start": day,
                "end": day + dt.timedelta(days=1),
                "previous": {"period": before.isoformat(), "start": before, "end": day},
                "money_month": _month_key(day), "complete": day < today,
                "as_at": min(day, today)}
    if period and not _DAY.fullmatch(period):
        raise HTTPException(400, "period must be YYYY-MM-DD for a weekly report")
    day = dt.date.fromisoformat(period) if period else today - dt.timedelta(days=7)
    start = day - dt.timedelta(days=day.weekday())
    end = start + dt.timedelta(days=7)
    pstart = start - dt.timedelta(days=7)
    return {"cadence": "week", "period": start.isoformat(), "start": start, "end": end,
            "previous": {"period": pstart.isoformat(), "start": pstart, "end": start},
            "money_month": _month_key(end - dt.timedelta(days=1)), "complete": end <= today,
            "as_at": min(end - dt.timedelta(days=1), today)}


def _scope(program_id: Optional[int], broker_party_id: Optional[int] = None) -> dict:
    """The Programme Management Report (programme_report) narrows each reader
    to one programme, or one broker on it; the whole book passes nothing."""
    return {**({"program_id": program_id} if program_id else {}),
            **({"broker_party_id": broker_party_id} if broker_party_id else {})}


def _at(d: dt.date) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day)


def _change(now: Optional[float], then: Optional[float], points: bool = False) -> Optional[float]:
    """Points for a percentage, percent for a count; None when there is
    nothing to compare with."""
    if now is None or then is None:
        return None
    if points:
        return round(now - then, 1)
    return round(100.0 * (now - then) / then, 1) if then else None


# ── the sections ────────────────────────────────────────────────────────────
def _money(s, tenant_id: int, month: str, program_id: Optional[int] = None,
           broker_party_id: Optional[int] = None) -> dict:
    """Gross premium and commission for the month, and its top programmes and
    brokers — the Financial Summary's own totals, in its biggest currency."""
    import financial_summary as fs
    from auth_deps import Principal
    me = Principal(user_id=0, tenant_id=tenant_id, role="carrier_admin")
    out = fs.summary(s, me, basis="month", period=month, program_id=program_id,
                     broker_party_id=broker_party_id)
    groups = [g for g in out["groups"] if g["currency"] not in (fs.NOT_STATED, fs.IN_REVIEW)] \
        or out["groups"]
    if not groups:
        return {"month": month, "currency": None, "files": 0, "other_currencies": 0,
                "gross_premium": None, "commission": None, "earned_premium": None,
                "top_programmes": [], "top_brokers": [], "premium_by": {"programme": {}, "broker": {}},
                "premium_before_by": {"programme": {}, "broker": {}}}
    g = groups[0]

    def _fig(t: dict) -> dict:
        return {"current": t["current"]["value"], "previous": t["previous"]["value"],
                "change": t["change"]["previous"],
                "last_year": t["last_year"]["value"], "change_last_year": t["change"]["last_year"]}

    def _top(parts: list) -> list[dict]:
        return [{"id": p["id"], "name": p["name"],
                 "gross_premium": p["totals"]["gross_premium"]["current"]["value"]}
                for p in parts if p["totals"]["gross_premium"]["current"]["value"]][:TOP]
    return {"month": month, "currency": g["currency"],
            "files": (out["windows"].get("current") or {}).get("files", 0),
            "other_currencies": len(out["groups"]) - 1,
            "gross_premium": _fig(g["totals"]["gross_premium"]),
            "commission": _fig(g["totals"]["commission_amount"]),
            "earned_premium": _fig(g["totals"]["earned_premium"]),
            "top_programmes": _top(g["by_programme"]),
            "top_brokers": _top(g["by_broker"]),
            # Every programme's and broker's premium, for the scorecard.
            "premium_by": {k: {p["id"]: p["totals"]["gross_premium"]["current"]["value"]
                               for p in g[f"by_{k}"] if p["id"] is not None}
                           for k in ("programme", "broker")},
            # And the month before, so a programme's report shows its change.
            "premium_before_by": {k: {p["id"]: p["totals"]["gross_premium"]["previous"]["value"]
                                      for p in g[f"by_{k}"] if p["id"] is not None}
                                  for k in ("programme", "broker")}}


def _operations(s, tenant_id: int, start: dt.date, end: dt.date,
                program_id: Optional[int] = None,
                broker_party_id: Optional[int] = None) -> dict:
    import check_record as cr
    out = cr.summary(s, tenant_id, _at(start), _at(end), **_scope(program_id, broker_party_id))
    v, q, h = out["volumes"], out["quality"], out["no_human_help"]
    return {"files": v["files"], "rows": v["rows"], "errors": q["errors"],
            "critical": q["critical"], "clean_first_time": h["files"],
            "clean_first_time_pct": h["files_pct"]}


def _timeliness(s, tenant_id: int, month: str, today: dt.date,
                program_id: Optional[int] = None,
                broker_party_id: Optional[int] = None) -> dict:
    import monthly_report as mr
    per, _groups = mr._timeliness(s, tenant_id, {month}, program_id=program_id,
                                  broker_party_id=broker_party_id, today=today)
    t = per[month]
    return {"month": month, "due": t["due"], "due_so_far": t["due_so_far"],
            "on_time": t["on_time"], "on_time_pct": t["on_time_pct"],
            "late": t["late"], "overdue": t["overdue"], "never": t["never"]}


def _open_at(s, tenant_id: int, as_at: dt.date, target: int) -> list[dict]:
    """The problems open at the end of `as_at`, from the records' own dates:
    the same kinds and words as open_problems, signatures left out (when one
    started waiting is not kept)."""
    import fix_performance as fp
    import open_problems as op
    import submission_case as sc
    from db import ExpectedSubmission, Program, SubmissionCaseEvent
    from intake_models import FileArrival
    end = _at(as_at + dt.timedelta(days=1))
    day = lambda v: v.date() if isinstance(v, dt.datetime) else v  # noqa: E731
    out = []
    for p in fp.problems(s, tenant_id):
        found, fixed = (v.replace(tzinfo=None) if isinstance(v, dt.datetime) else v
                        for v in (p.get("found_at"), p.get("fixed_at")))
        if isinstance(found, dt.date) and not isinstance(found, dt.datetime):
            found = _at(found)
        if isinstance(fixed, dt.date) and not isinstance(fixed, dt.datetime):
            fixed = _at(fixed)
        if found is None or found >= end or (fixed is not None and fixed < end):
            continue
        b = p.get("broker_party_id")
        out.append(op._item(op.CHECK, started=found, due=day(found) + dt.timedelta(days=target),
                            status="Waiting for a fix", owner=b or op.CARRIER,
                            program_id=p.get("program_id"), broker_party_id=b,
                            title=p.get("check") or p.get("message") or "Check failed",
                            link=f"/uploads/{p['export_id']}/exceptions" if p.get("export_id") else None,
                            key=f"check:{p.get('export_id')}:{p.get('check_key')}:{p.get('row')}"))
    rows = (s.query(ExpectedSubmission).join(Program, Program.id == ExpectedSubmission.program_id)
            .filter(Program.tenant_id == tenant_id, ExpectedSubmission.due_date < as_at).all())
    owed = [e for e in rows if e.received_at is None or e.received_at > as_at]
    # Each case's state at the end of `as_at`: its last move by the day the
    # move COUNTS from (effective_on) — the daily check often writes a move
    # after the day it took effect. A move to no state ("cleared") counts too.
    state: dict[int, tuple] = {}
    if owed:
        moves = []
        for ev in (s.query(SubmissionCaseEvent)
                   .filter(SubmissionCaseEvent.expected_id.in_([e.id for e in owed]))):
            on = ev.effective_on or day(ev.created_at)
            if on is not None and on <= as_at:
                moves.append((on, ev.created_at or dt.datetime.min, ev.id, ev))
        for *_k, ev in sorted(moves, key=lambda m: m[:3]):
            if ev.to_state is not None or ev.from_state is not None:
                state[ev.expected_id] = (ev.to_state, ev)
    for e in owed:
        st, ev = state.get(e.id, (None, None))
        if st == sc.RESOLVED:
            continue
        b = e.broker_party_id
        if st == sc.IN_DISPUTE:
            # A reminder while it waits is also an in_dispute row; the dispute
            # began when it was raised.
            raised = e.dispute_raised_on or ev.effective_on or day(ev.created_at)
            out.append(op._item(op.DISPUTE, started=raised, due=raised + dt.timedelta(days=target),
                                status="Waiting for your decision", owner=op.CARRIER,
                                program_id=e.program_id, broker_party_id=b,
                                title=f"Late mark disputed for {e.period}",
                                link="/bordereau-calendar", key=f"dispute:{e.id}"))
            continue
        until = (ev.details or {}).get("until") if st == sc.EXTENDED and ev else None
        out.append(op._item(op.LATE_FILE, started=e.due_date,
                            due=dt.date.fromisoformat(until) if until else e.due_date,
                            status=sc.STATE_WORDS.get(st, "Overdue"), owner=b or op.CARRIER,
                            program_id=e.program_id, broker_party_id=b,
                            title=f"Bordereau for {e.period} not received",
                            link="/bordereau-calendar", key=f"late:{e.id}"))
    for a in (s.query(FileArrival).filter(FileArrival.tenant_id == tenant_id,
                                           FileArrival.outcome == "held")):
        got = a.received_at
        if got is None or got.replace(tzinfo=None) >= end:
            continue
        if a.resolved_at is not None and a.resolved_at.replace(tzinfo=None) < end:
            continue
        out.append(op._item(op.HELD_FILE, started=got, due=day(got) + dt.timedelta(days=target),
                            status="Waiting to be released", owner=op.CARRIER,
                            program_id=a.program_id, broker_party_id=a.matched_broker_party_id,
                            title=a.filename, link="/files", key=f"held:{a.id}"))
    return out


def _problems(s, tenant_id: int, today: dt.date, as_at: Optional[dt.date] = None) -> dict:
    """Open problems and the escalations among them — now (Live, or a period
    still running), or as they stood at the end of `as_at`."""
    import fix_performance as fp
    import open_problems as op
    from db import Party
    target = fp.target_days(s, tenant_id)
    if as_at is None or as_at >= today:
        as_at = today
        items = op.collect(s, tenant_id, target=target)
    else:
        items = _open_at(s, tenant_id, as_at, target)
    report = op.build(items, today=as_at, page_size=1)       # sets age and overdue
    today = as_at
    delinquent = op._LATE_STATES["delinquent"]
    esc = [p for p in items
           if p["type"] == op.DISPUTE or p["overdue"]
           or (p["type"] == op.LATE_FILE and p["status"] == delinquent)]
    esc.sort(key=lambda p: (-(p["age_days"] or 0), p["due_on"] or today))
    owners = {p["owner"] for p in esc if p["owner"] not in (None, op.CARRIER)}
    names = dict(s.query(Party.id, Party.legal_name).filter(Party.id.in_(owners)).all()) \
        if owners else {}

    def _row(p: dict) -> dict:
        return {"type": p["type"], "type_label": p["type_label"], "status": p["status"],
                "title": p["title"], "detail": p["detail"],
                "owner": "Carrier" if p["owner"] == op.CARRIER else names.get(p["owner"]),
                "started_on": p["started_on"].isoformat() if p["started_on"] else None,
                "due_on": p["due_on"].isoformat() if p["due_on"] else None,
                "age_days": p["age_days"], "overdue": p["overdue"], "link": p["link"]}
    return {"open": report["total_open"], "overdue": report["total_overdue"],
            "by_type": [{"type": t["type"], "label": t["label"], "open": t["open"],
                         "overdue": t["overdue"]} for t in report["by_type"] if t["open"]],
            "escalations": len(esc),
            "escalation_list": [_row(p) for p in esc[:ESCALATIONS_KEPT]]}


def _portfolio(s, tenant_id: int, today: dt.date) -> dict:
    """Programmes by stage as at `today` (the period's last day): the ones
    that existed by then, each term judged against that day."""
    import program_stage as ps_
    stages: dict[str, int] = {}
    programs = stage_of(s, tenant_id, today)
    for _p, st, _c in programs:
        stages[st] = stages.get(st, 0) + 1
    return {"total": len(programs),
            "active": sum(stages.get(k, 0) for k in ps_.WRITING_STAGES),
            "runoff": stages.get("Runoff", 0),
            "not_live": stages.get("Onboarding", 0) + stages.get("In Setup", 0),
            "by_stage": [{"stage": k, "programmes": v}
                         for k, v in sorted(stages.items(), key=lambda kv: -kv[1])]}


def stage_of(s, tenant_id: int, today: dt.date) -> list[tuple]:
    """(programme, stage, its contracts) for every programme that existed by
    the end of `today`, each term judged against that day."""
    import program_stage as ps_
    from db import Contract, Program
    end = _at(today + dt.timedelta(days=1))
    programs = [p for p in s.query(Program).filter(Program.tenant_id == tenant_id,
                                                   Program.is_app_managed.is_(True))
                if p.created_at is None or p.created_at.replace(tzinfo=None) < end]
    by_program: dict[int, list] = {}
    if programs:
        for c in s.query(Contract).filter(Contract.tenant_id == tenant_id,
                                          Contract.program_id.in_([p.id for p in programs])):
            by_program.setdefault(c.program_id, []).append(c)
    return [(p, ps_.program_stage(p, ps_.current_contract(by_program.get(p.id, [])), today),
             by_program.get(p.id, [])) for p in programs]


TREND_BY_DAY_UP_TO = 31          # days
TREND_BY_WEEK_UP_TO = 26 * 7     # days


def trend_buckets(start: dt.date, end: dt.date) -> tuple[str, list[tuple]]:
    """How the period is cut for the chart, and the pieces as (from, to,
    label), `to` not included: by hour for one day, by day up to 31 days, by
    week (Monday to Sunday, cut at the period's ends) up to 26 weeks, else by
    month (cut the same way)."""
    days = (end - start).days
    if days <= 1:
        a = _at(start)
        return "hour", [(a + dt.timedelta(hours=h), a + dt.timedelta(hours=h + 1), f"{h:02d}:00")
                        for h in range(24)]
    if days <= TREND_BY_DAY_UP_TO:
        return "day", [(_at(d), _at(d + dt.timedelta(days=1)), f"{d.day} {d:%b}")
                       for d in (start + dt.timedelta(days=i) for i in range(days))]
    out, a = [], start
    if days <= TREND_BY_WEEK_UP_TO:
        while a < end:
            b = min(a + dt.timedelta(days=7 - a.weekday()), end)
            last = b - dt.timedelta(days=1)
            label = (f"{a.day} {a:%b}" if a == last else
                     f"{a.day}{'' if a.month == last.month else f' {a:%b}'} – {last.day} {last:%b}")
            out.append((_at(a), _at(b), label))
            a = b
        return "week", out
    while a < end:
        b = min(_month_start(_shift_month(_month_key(a), 1)), end)
        out.append((_at(a), _at(b), f"{a:%b %Y}"))
        a = b
    return "month", out


def _trend(s, tenant_id: int, start: dt.date, end: dt.date,
           program_id: Optional[int] = None,
           broker_party_id: Optional[int] = None) -> list[dict]:
    """Errors and clean files across the report's own period, piece by piece
    (trend_buckets)."""
    import check_record as cr
    unit, pieces = trend_buckets(start, end)
    out = []
    for a, b, label in pieces:
        q = cr.summary(s, tenant_id, a, b, **_scope(program_id, broker_party_id))
        out.append({"unit": unit, "label": label,
                    "from": a.isoformat(), "to": b.isoformat(),
                    "files": q["volumes"]["files"], "errors": q["quality"]["errors"],
                    "clean_first_time_pct": q["no_human_help"]["files_pct"]})
    return out


# 37.2 — how far back the error history looks, by the report's cadence.
HISTORY = {"day": ("day", 14), "week": ("week", 12), "month": ("month", 12), "custom": ("month", 12)}
HEALTH_SHOWN = 5


def history_buckets(cadence: str, end: dt.date) -> tuple[str, list[tuple]]:
    """The periods up to the report's last day, oldest first, as (from, to,
    label), `to` not included: the last 14 days for a daily report, the last 12
    weeks (Monday to Sunday) for a weekly one, else the last 12 months. The
    last piece is the report's own day, week or month."""
    unit, n = HISTORY.get(cadence, HISTORY["month"])
    last = end - dt.timedelta(days=1)
    if unit == "day":
        return unit, [(_at(d), _at(d + dt.timedelta(days=1)), f"{d.day} {d:%b}")
                      for d in (last - dt.timedelta(days=i) for i in range(n - 1, -1, -1))]
    if unit == "week":
        monday = last - dt.timedelta(days=last.weekday())
        out = []
        for i in range(n - 1, -1, -1):
            a = monday - dt.timedelta(weeks=i)
            out.append((_at(a), _at(a + dt.timedelta(days=7)), f"{a.day} {a:%b}"))
        return unit, out
    key = _month_key(last)
    out = []
    for i in range(n - 1, -1, -1):
        m = _shift_month(key, -i)
        a = _month_start(m)
        out.append((_at(a), _at(_month_start(_shift_month(m, 1))), f"{a:%b %Y}"))
    return unit, out


def _history(s, tenant_id: int, cadence: str, end: dt.date,
             program_id: Optional[int] = None,
             broker_party_id: Optional[int] = None) -> list[dict]:
    """37.2 — errors and clean files over the periods before the report's own
    (history_buckets), counted the way the in-period chart counts them."""
    import check_record as cr
    unit, pieces = history_buckets(cadence, end)
    out = []
    for a, b, label in pieces:
        q = cr.summary(s, tenant_id, a, b, **_scope(program_id, broker_party_id))
        out.append({"unit": unit, "label": label,
                    "from": a.isoformat(), "to": b.isoformat(),
                    "files": q["volumes"]["files"], "errors": q["quality"]["errors"],
                    "clean_first_time_pct": q["no_human_help"]["files_pct"]})
    return out


def _portfolio_of(s, tenant_id: int, w: dict, daily: bool) -> dict:
    """Programmes by stage as at the period's last day; a daily report adds
    that day's changes (new, went live, into run-off)."""
    stages = _portfolio(s, tenant_id, w["as_at"])
    if not daily:
        return stages
    return {**_programme_changes(s, tenant_id, w["start"], w["end"]),
            **{k: stages[k] for k in ("active", "runoff", "not_live", "by_stage") if k in stages}}


def _errors_by_programme(s, tenant_id: int, start: dt.date, end: dt.date) -> dict:
    """37 — the programmes with the most errors in the period, each split by
    severity (check_record.summary's own counts), most errors first."""
    import check_record as cr
    rows = [r for r in cr.summary(s, tenant_id, _at(start), _at(end))["by_programme"]
            if r["id"] is not None and r["errors"]]
    names = _names(s, [r["id"] for r in rows], [])["programme"]
    return {"total": len(rows),
            "rows": [{"id": r["id"], "name": names.get(r["id"]) or f"#{r['id']}",
                      "errors": r["errors"], "critical": r["critical"],
                      "warning": r["warning"], "info": r["info"]}
                     for r in rows[:HEALTH_SHOWN]]}


SENT_ON_TIME, SENT_LATE, NOT_SENT = "Sent on time", "Sent late", "Not sent"


def _reinsurance(s, tenant_id: int, today: dt.date, window_: dict) -> dict:
    """The statements whose due date falls in the period, each sent on time,
    sent late or not sent as at the period's end — the first version sent is
    the one that counts."""
    import ceded_routes as cdr
    from db import CededBordereau, Contract
    q = (s.query(CededBordereau, Contract)
         .join(Contract, Contract.id == CededBordereau.contract_id)
         .filter(CededBordereau.tenant_id == tenant_id))
    setups: dict[int, object] = {}
    by_key: dict[tuple, dict] = {}
    for cb, c in q.all():
        if c.id not in setups:
            setups[c.id] = cdr.setup_of(s, c)
        su = setups[c.id]
        due = cdr.due_date(cb.period, su.due_days if su is not None else None)
        if due is None or not window_["start"] <= due < window_["end"]:
            continue
        it = by_key.setdefault((c.id, cb.period), {
            "contract_id": c.id, "treaty": c.name or "Treaty", "period": cb.period,
            "due_on": due.isoformat() if due else None, "_sent": None})
        sent = cb.sent_at.replace(tzinfo=None) if cb.sent_at else None
        if sent and (it["_sent"] is None or sent < it["_sent"]):
            it["_sent"] = sent
    end = _at(window_["as_at"] + dt.timedelta(days=1))
    items = []
    for it in by_key.values():
        sent = it.pop("_sent")
        due = dt.date.fromisoformat(it["due_on"]) if it["due_on"] else None
        if sent is not None and sent < end:
            it["status"] = SENT_ON_TIME if due is None or sent.date() <= due else SENT_LATE
            it["sent_on"] = sent.date().isoformat()
        else:
            it["status"], it["sent_on"] = NOT_SENT, None
        it["overdue"] = it["status"] == NOT_SENT and bool(due and window_["as_at"] > due)
        items.append(it)
    items.sort(key=lambda i: (i["due_on"] is None, i["due_on"] or ""))
    count = lambda st: sum(1 for i in items if i["status"] == st)  # noqa: E731
    return {"due": len(items), "sent_on_time": count(SENT_ON_TIME),
            "sent_late": count(SENT_LATE), "not_sent": count(NOT_SENT),
            "overdue": sum(1 for i in items if i["overdue"]), "statements": items}


# ── the pack ────────────────────────────────────────────────────────────────
# ── daily: only what happened in the day ────────────────────────────────────


def _processed_money(s, tenant_id: int, start: dt.date, end: dt.date,
                     program_id: Optional[int] = None,
                     broker_party_id: Optional[int] = None) -> dict:
    """Premium and commission in the bordereaux first processed in [start,
    end), per currency, read the way the Financial Summary reads a file (its
    reader, the latest version of the file's template, its conventions) — by
    when the file was processed, not by the month it reports on."""
    import check_record as cr
    import financial_figures as ff
    import financial_summary as fs
    import policy_search as ps
    files = cr.first_runs(cr.runs_in(s, tenant_id, _at(start), _at(end),
                                       **_scope(program_id, broker_party_id)))
    ids = {f.export_id for f in files if f.export_id}
    out: dict = {"files": len(ids), "groups": {}, "names": {"programme": {}, "broker": {}}}
    if not ids:
        return out
    exports = ps._exports(s, ps.Seat("carrier", tenant_id=tenant_id), ids=ids)
    latest = fs._latest_templates(s, {o.template_id for o in exports})
    reading = lambda o: latest.get(o.template_id, o.template_id)  # noqa: E731
    structures = ps._structures(s, {reading(o) for o in exports})
    reads = {o.id: fs._read(s, o, structures.get(reading(o)), reading(o)) for o in exports}
    pooled: dict = {}
    for o in exports:
        pooled.setdefault(reading(o), []).append(reads[o.id].evidence)
    conv = {tid: {n: ff.convention(ff.pool(evs).get(n)) for n in ff.RATES}
            for tid, evs in pooled.items()}
    for o in exports:
        for cells in reads[o.id].rows:
            fig = ff.figures(cells, conv.get(reading(o), {}))
            g = out["groups"].setdefault(fs._currency_key(fig), {
                "rows": 0, "gross": None, "commission": None, "earned": None,
                "programme": {}, "broker": {}, "pair": {}})
            g["rows"] += 1
            gross = fig["gross_premium"]["value"]
            comm = fig["commission_amount"]["value"]
            earned = fig["earned_premium"]["value"]
            if earned is not None:
                g["earned"] = (g["earned"] or 0) + earned
            if gross is not None:
                g["gross"] = (g["gross"] or 0) + gross
                for part, key in (("programme", o.program_id), ("broker", o.broker_party_id)):
                    if key is not None:
                        g[part][key] = g[part].get(key, 0) + gross
                # Programme and broker together — the Programme Management Report's rows.
                pair = (o.program_id, o.broker_party_id)
                g["pair"][pair] = g["pair"].get(pair, 0) + gross
            if comm is not None:
                g["commission"] = (g["commission"] or 0) + comm
    out["names"] = ps._names(s, exports)
    return out


def _year_before(d: dt.date) -> dt.date:
    """The same day a year earlier (29 February → 28 February)."""
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def _money_processed(s, tenant_id: int, w: dict, program_id: Optional[int] = None,
                     broker_party_id: Optional[int] = None) -> dict:
    """The money section for a day: the bordereaux processed in it, in the
    biggest currency, against the day before and the same day a year earlier."""
    import financial_summary as fs
    pid = _scope(program_id, broker_party_id)
    now = _processed_money(s, tenant_id, w["start"], w["end"], **pid)
    before = _processed_money(s, tenant_id, w["previous"]["start"], w["previous"]["end"], **pid)
    year_ago = _processed_money(s, tenant_id, _year_before(w["start"]), _year_before(w["end"]), **pid)
    real = {k: g for k, g in now["groups"].items() if k not in (fs.NOT_STATED, fs.IN_REVIEW)}
    pick = real or now["groups"]
    blank = {"month": None, "basis": "processed", "currency": None, "files": now["files"],
             "other_currencies": 0, "gross_premium": None, "commission": None, "earned_premium": None,
             "top_programmes": [], "top_brokers": [], "premium_by": {"programme": {}, "broker": {}},
             "premium_before_by": {"programme": {}, "broker": {}}}
    if not pick:
        return blank
    cur = max(pick, key=lambda k: (pick[k]["gross"] or 0, pick[k]["rows"]))
    g, p = pick[cur], before["groups"].get(cur) or {}
    ly = year_ago["groups"].get(cur) or {}

    def fig(k: str) -> dict:
        c, b, y = g.get(k), p.get(k), ly.get(k)
        return {"current": c, "previous": b, "change": _change(c, b),
                "last_year": y, "change_last_year": _change(c, y)}

    def top(part: str) -> list[dict]:
        names = now["names"].get(part, {})
        return [{"id": i, "name": names.get(i) or f"#{i}", "gross_premium": v}
                for i, v in sorted(g[part].items(), key=lambda kv: -kv[1]) if v][:TOP]
    return {**blank, "currency": cur, "other_currencies": len(now["groups"]) - 1,
            "gross_premium": fig("gross"), "commission": fig("commission"),
            "earned_premium": fig("earned"),
            "top_programmes": top("programme"), "top_brokers": top("broker"),
            "premium_by": {k: dict(g[k]) for k in ("programme", "broker")},
            "premium_before_by": {k: dict(p.get(k) or {}) for k in ("programme", "broker")}}


def _expected(s, tenant_id: int, start: dt.date, end: dt.date, program_id: Optional[int],
              broker_party_id: Optional[int] = None):
    """The files owed (a broker named) with their due date in [start, end)."""
    from db import ExpectedSubmission
    q = s.query(ExpectedSubmission).filter(
        ExpectedSubmission.tenant_id == tenant_id,
        ExpectedSubmission.broker_party_id.isnot(None),
        ExpectedSubmission.due_date >= start, ExpectedSubmission.due_date < end)
    if program_id:
        q = q.filter(ExpectedSubmission.program_id == program_id)
    if broker_party_id:
        q = q.filter(ExpectedSubmission.broker_party_id == broker_party_id)
    return q.all()


def _due_in(s, tenant_id: int, start: dt.date, end: dt.date, today: dt.date,
            program_id: Optional[int] = None,
            broker_party_id: Optional[int] = None) -> dict:
    """Files whose due date falls in [start, end): how many, and how many came
    on time — the Monthly Report's own rule (submission_calendar.derive_status)."""
    from submission_calendar import derive_status
    due = so_far = on_time = late = overdue = 0
    for e in _expected(s, tenant_id, start, end, program_id, broker_party_id):
        due += 1
        st = derive_status(e.due_date, today, 0, e.received_at, e.closes_on)
        if st in ("scheduled", "due_soon", "due_today") and not (st == "due_today" and e.received_at):
            continue
        so_far += 1
        on_time += st == "on_time" or (st == "due_today" and bool(e.received_at))
        late += st == "received_late"
        overdue += st in ("overdue", "never_arrived")
    pct = round(100.0 * on_time / so_far, 1) if so_far else None
    return {"month": None, "due": due, "due_so_far": so_far, "on_time": on_time,
            "on_time_pct": pct, "late": late, "overdue": overdue, "never": 0}


def _new_problems(s, tenant_id: int, start: dt.date, end: dt.date, today: dt.date) -> dict:
    """What went wrong in [start, end): problems that STARTED in it (a failed
    check found, a file that became late, a dispute raised, a file held) and
    problems that BECAME ESCALATIONS in it (a late file turned Delinquent, a
    dispute raised, anything that passed its due date). Each escalation says
    whether it is still open now."""
    import fix_performance as fp
    import open_problems as op
    import submission_case as sc
    from db import ExpectedSubmission, Party, Program, SubmissionCaseEvent
    from intake_models import FileArrival
    target = fp.target_days(s, tenant_id)
    lo, hi = _at(start), _at(end)
    naive = lambda v: v.replace(tzinfo=None) if isinstance(v, dt.datetime) else (  # noqa: E731
        _at(v) if isinstance(v, dt.date) else v)
    inside = lambda d: d is not None and start <= d < end  # noqa: E731
    started = fixed = 0
    esc: list[dict] = []

    def _esc(kind, title, owner, began, due, became, still_open, status):
        esc.append({"type": kind, "type_label": op.TYPE_WORDS[kind], "status": status,
                    "title": title, "detail": None, "owner": owner,
                    "started_on": began.isoformat() if began else None,
                    "due_on": due.isoformat() if due else None,
                    "became_on": became.isoformat(), "still_open": still_open,
                    "age_days": (min(end - dt.timedelta(days=1), today) - began).days if began else None,
                    "overdue": True, "link": None})
    for p in fp.problems(s, tenant_id):
        found, fx = naive(p.get("found_at")), naive(p.get("fixed_at"))
        if found is None:
            continue
        if lo <= found < hi:
            started += 1
        if fx is not None and lo <= fx < hi:
            fixed += 1
        due = found.date() + dt.timedelta(days=target)
        late_from = due + dt.timedelta(days=1)
        if inside(late_from) and (fx is None or fx.date() > due):
            _esc(op.CHECK, p.get("check") or p.get("message") or "Check failed",
                 p.get("broker_party_id") or op.CARRIER, found.date(), due, late_from,
                 fx is None, "Waiting for a fix" if fx is None else "Fixed since")
    rows = (s.query(ExpectedSubmission).join(Program, Program.id == ExpectedSubmission.program_id)
            .filter(Program.tenant_id == tenant_id,
                    ExpectedSubmission.due_date >= start - dt.timedelta(days=1),
                    ExpectedSubmission.due_date < end - dt.timedelta(days=1)).all())
    for e in rows:                          # became late in the window
        late_from = e.due_date + dt.timedelta(days=1)
        if e.received_at is not None and e.received_at <= e.due_date:
            continue
        started += 1
        _esc(op.LATE_FILE, f"Bordereau for {e.period} not received", e.broker_party_id or op.CARRIER,
             e.due_date, e.due_date, late_from, e.received_at is None,
             "Overdue" if e.received_at is None else "Received since")
    by_id = {}
    for ev in (s.query(SubmissionCaseEvent)
               .filter(SubmissionCaseEvent.tenant_id == tenant_id,
                       SubmissionCaseEvent.to_state.in_((sc.DELINQUENT, sc.IN_DISPUTE)))):
        on = ev.effective_on or (ev.created_at.date() if ev.created_at else None)
        if not inside(on) or ev.from_state == ev.to_state:     # a reminder is not a move
            continue
        e = by_id.get(ev.expected_id) or s.get(ExpectedSubmission, ev.expected_id)
        by_id[ev.expected_id] = e
        if e is None:
            continue
        if ev.to_state == sc.IN_DISPUTE:
            started += 1
            _esc(op.DISPUTE, f"Late mark disputed for {e.period}", op.CARRIER, on, None, on,
                 e.case_state == sc.IN_DISPUTE, "Waiting for your decision"
                 if e.case_state == sc.IN_DISPUTE else "Decided since")
        else:
            _esc(op.LATE_FILE, f"Bordereau for {e.period} not received",
                 e.broker_party_id or op.CARRIER, e.due_date, e.due_date, on,
                 e.received_at is None, "Delinquent" if e.received_at is None else "Received since")
    for a in s.query(FileArrival).filter(FileArrival.tenant_id == tenant_id,
                                         FileArrival.outcome == "held"):
        got, done = naive(a.received_at), naive(a.resolved_at)
        if got is None:
            continue
        if lo <= got < hi:
            started += 1
        due = got.date() + dt.timedelta(days=target)
        if inside(due + dt.timedelta(days=1)) and (done is None or done.date() > due):
            _esc(op.HELD_FILE, a.filename, op.CARRIER, got.date(), due, due + dt.timedelta(days=1),
                 done is None, "Waiting to be released" if done is None else "Released since")
    owners = {x["owner"] for x in esc if x["owner"] not in (None, op.CARRIER)}
    names = dict(s.query(Party.id, Party.legal_name).filter(Party.id.in_(owners)).all()) if owners else {}
    for x in esc:
        x["owner"] = "Carrier" if x["owner"] == op.CARRIER else names.get(x["owner"])
    esc.sort(key=lambda x: (x["became_on"], x["title"]))
    return {"open": started, "overdue": sum(1 for x in esc if x["still_open"]),
            "fixed": fixed, "new": True, "by_type": [], "escalations": len(esc),
            "escalation_list": esc[:ESCALATIONS_KEPT]}


def _programme_changes(s, tenant_id: int, start: dt.date, end: dt.date) -> dict:
    """Programmes that were added, went live or went into run-off in [start,
    end), and how many there are at its end."""
    import program_stage as ps_
    from db import Contract, Program
    hi = _at(end)
    programs = [p for p in s.query(Program).filter(Program.tenant_id == tenant_id,
                                                   Program.is_app_managed.is_(True))
                if p.created_at is None or p.created_at.replace(tzinfo=None) < hi]
    by_program: dict[int, list] = {}
    if programs:
        for c in s.query(Contract).filter(Contract.tenant_id == tenant_id,
                                          Contract.program_id.in_([p.id for p in programs])):
            by_program.setdefault(c.program_id, []).append(c)
    new = live = runoff = 0
    for p in programs:
        if p.created_at is not None and p.created_at.replace(tzinfo=None) >= _at(start):
            new += 1
        c = ps_.current_contract(by_program.get(p.id, []))
        if c is None:
            continue
        went = c.lifecycle_effective_date
        if went and start <= went < end and (c.status or "").strip().lower() == "active":
            live += 1
        if c.expiry_dt and start <= c.expiry_dt + dt.timedelta(days=1) < end:
            runoff += 1
    return {"total": len(programs), "new": new, "went_live": live, "into_runoff": runoff,
            "active": 0, "runoff": 0, "not_live": 0, "changes": True, "by_stage": []}


# ── what came in, how clean, who was late, how much of each contract is used ──
QUALITY_SHOWN = 5
LATE_SHOWN = 10                 # in the files
LATE_KEPT = 200                 # in the pack: the scorecard opens each broker's own
SCORECARD_SHOWN = 10
CONTRACTS_SHOWN = 10
# "At 85% you get an email, before the line rather than after" — the premium
# cap's own promise (contract_types.premium_cap_total), so the report warns at
# the same point.
NEAR_CAP_PCT = 85.0


def _names(s, programme_ids, broker_ids) -> dict:
    from db import Party, Program
    progs = {i for i in programme_ids if i}
    brokers = {i for i in broker_ids if i}
    return {"programme": dict(s.query(Program.id, Program.name)
                              .filter(Program.id.in_(progs)).all()) if progs else {},
            "broker": dict(s.query(Party.id, Party.legal_name)
                           .filter(Party.id.in_(brokers)).all()) if brokers else {}}


def _quality(s, tenant_id: int, start: dt.date, end: dt.date,
             program_id: Optional[int] = None,
             broker_party_id: Optional[int] = None) -> dict:
    """The files of the period by how they first arrived (clean, with errors,
    not checked) and the checks that failed most — check_record.summary, the
    Monthly Report's own source. Quality by broker and by programme is the
    scorecard's (_scorecard)."""
    import check_record as cr
    q = cr.summary(s, tenant_id, _at(start), _at(end), **_scope(program_id, broker_party_id))["quality"]
    return {"clean": q["files_clean"], "with_errors": q["files_with_errors"],
            "not_checked": q["files_not_validated"],
            "top_errors": [{"check": t["check"], "key": t.get("key"), "files": t["files"], "errors": t["errors"],
                            "severity": t["severity"]} for t in q["top_failing_checks"][:QUALITY_SHOWN]]}


def _late(s, tenant_id: int, start: dt.date, end: dt.date, as_at: dt.date,
          program_id: Optional[int] = None,
          broker_party_id: Optional[int] = None) -> dict:
    """Files due in [start, end) that came after their due date or had not come
    by the end of `as_at` — submission_calendar.derive_status, the Monthly
    Report's rule. Latest due first."""
    from submission_calendar import derive_status
    rows = []
    for e in _expected(s, tenant_id, start, end, program_id, broker_party_id):
        got = e.received_at if e.received_at and e.received_at <= as_at else None
        st = derive_status(e.due_date, as_at, 0, got, e.closes_on)
        if st not in ("received_late", "overdue", "never_arrived"):
            continue
        rows.append({"program_id": e.program_id, "broker_party_id": e.broker_party_id,
                     "month": e.period, "due_on": e.due_date.isoformat(),
                     "arrived_on": got.isoformat() if got else None,
                     "status": {"received_late": "Arrived late", "overdue": "Not yet",
                                "never_arrived": "Never arrived"}[st],
                     "days_late": ((got or as_at) - e.due_date).days})
    names = _names(s, [r["program_id"] for r in rows], [r["broker_party_id"] for r in rows])
    for r in rows:
        r["programme"] = names["programme"].get(r["program_id"]) or f"#{r['program_id']}"
        r["broker"] = names["broker"].get(r["broker_party_id"]) or f"#{r['broker_party_id']}"
    rows.sort(key=lambda r: (r["due_on"], r["days_late"]), reverse=True)
    count = lambda st: sum(1 for r in rows if r["status"] == st)  # noqa: E731
    return {"late": count("Arrived late"), "missing": count("Not yet"),
            "never": count("Never arrived"), "total": len(rows), "list": rows[:LATE_KEPT]}


def _due_tally(s, tenant_id: int, start: dt.date, end: dt.date, as_at: dt.date,
               program_id: Optional[int] = None,
               broker_party_id: Optional[int] = None) -> dict:
    """Files due in [start, end), per broker and per programme, as at the end
    of `as_at`: how many were due by then, how many came on time, and how many
    came late or have not come — submission_calendar.derive_status."""
    from submission_calendar import derive_status
    out: dict = {"broker": {}, "programme": {}}
    for e in _expected(s, tenant_id, start, end, program_id, broker_party_id):
        got = e.received_at if e.received_at and e.received_at <= as_at else None
        st = derive_status(e.due_date, as_at, 0, got, e.closes_on)
        if st in ("scheduled", "due_soon") or (st == "due_today" and not got):
            continue
        for kind, key in (("broker", e.broker_party_id), ("programme", e.program_id)):
            t = out[kind].setdefault(key, {"due": 0, "on_time": 0, "late_or_missing": 0})
            t["due"] += 1
            t["on_time"] += st == "on_time" or (st == "due_today" and bool(got))
            t["late_or_missing"] += st in ("received_late", "overdue", "never_arrived")
    return out


def _scorecard(s, tenant_id: int, w: dict, premium_by: dict,
               program_id: Optional[int] = None,
               broker_party_id: Optional[int] = None, shown: Optional[int] = SCORECARD_SHOWN) -> dict:
    """One row per broker and per programme for the period: premium, files,
    errors, on time, clean first time and files late or missing — with on
    time and clean first time for the period before, so a figure that got
    worse shows. Most late or missing first, then the lowest share clean
    first time. check_record.summary and the calendar, as the cards they
    replace."""
    import check_record as cr
    pid = _scope(program_id, broker_party_id)
    now = cr.summary(s, tenant_id, _at(w["start"]), _at(w["end"]), **pid)
    before = cr.summary(s, tenant_id, _at(w["previous"]["start"]), _at(w["previous"]["end"]), **pid)
    due = _due_tally(s, tenant_id, w["start"], w["end"], w["as_at"], **pid)
    due_before = _due_tally(s, tenant_id, w["previous"]["start"], w["previous"]["end"],
                            w["previous"]["end"] - dt.timedelta(days=1), **pid)
    pct = lambda n, d: round(100.0 * n / d, 1) if d else None  # noqa: E731
    out = {}
    for kind in ("broker", "programme"):
        files = {r["id"]: r for r in now[f"by_{kind}"] if r["id"] is not None}
        files_before = {r["id"]: r for r in before[f"by_{kind}"] if r["id"] is not None}
        money = premium_by.get(kind) or {}
        ids = set(files) | set(due[kind]) | {i for i, v in money.items() if v}
        names = _names(s, ids if kind == "programme" else (), ids if kind == "broker" else ())[kind]
        rows = []
        for i in ids:
            f, fb = files.get(i) or {}, files_before.get(i) or {}
            d, db_ = due[kind].get(i) or {}, due_before[kind].get(i) or {}
            rows.append({"id": i, "name": names.get(i) or f"#{i}", "premium": money.get(i),
                         "files": f.get("files", 0), "errors": f.get("errors", 0),
                         "critical": f.get("critical", 0), "warning": f.get("warning", 0),
                         "info": f.get("info", 0),
                         "clean_first_time_pct": f.get("straight_through_pct"),
                         "prev_clean_first_time_pct": fb.get("straight_through_pct"),
                         "due": d.get("due", 0), "on_time_pct": pct(d.get("on_time", 0), d.get("due", 0)),
                         "prev_on_time_pct": pct(db_.get("on_time", 0), db_.get("due", 0)),
                         "late_or_missing": d.get("late_or_missing", 0)})
        rows.sort(key=lambda r: (-r["late_or_missing"],
                                 r["clean_first_time_pct"] if r["clean_first_time_pct"] is not None else 101,
                                 r["name"].lower()))
        out[kind] = {"total": len(rows), "rows": rows[:shown]}
    return out


def _bound(s, tenant_id: int, start: dt.date, as_at: dt.date,
           program_id: Optional[int] = None,
           broker_party_id: Optional[int] = None) -> list:
    """The contracts bound (status active) by the end of `as_at` whose term
    reaches into [start, as_at]."""
    from db import Contract
    hi = _at(as_at + dt.timedelta(days=1))
    return [c for c in s.query(Contract).filter(Contract.tenant_id == tenant_id,
                                                Contract.is_app_managed.is_(True))
            if (c.status or "").strip().lower() == "active"
            and (not program_id or c.program_id == program_id)
            and (not broker_party_id or c.broker_party_id == broker_party_id)
            and (c.created_at is None or c.created_at.replace(tzinfo=None) < hi)
            and (c.inception_dt is None or c.inception_dt <= as_at)
            and (c.expiry_dt is None or c.expiry_dt >= start)]


def _contract_label(c, names: dict) -> dict:
    return {"contract_id": c.id, "contract": c.name or c.filename or f"Contract #{c.id}",
            "programme": names["programme"].get(c.program_id) or "—",
            "broker": names["broker"].get(c.broker_party_id) or "—"}


def _files_by_contract(s, tenant_id: int, start: dt.date, end: dt.date,
                       program_id: Optional[int] = None,
                       broker_party_id: Optional[int] = None) -> dict[int, int]:
    """Files first processed in [start, end), per contract — a run that names
    no contract takes its file's."""
    import check_record as cr
    from db import OutputExport
    files = cr.first_runs(cr.runs_in(s, tenant_id, _at(start), _at(end),
                                       **_scope(program_id, broker_party_id)))
    of_export = {}
    missing = {f.export_id for f in files if not f.contract_id and f.export_id}
    if missing:
        of_export = dict(s.query(OutputExport.id, OutputExport.contract_id)
                         .filter(OutputExport.id.in_(missing)).all())
    out: dict[int, int] = {}
    for f in files:
        cid = f.contract_id or of_export.get(f.export_id)
        if cid:
            out[cid] = out.get(cid, 0) + 1
    return out


def _written(s, tenant_id: int, contracts: list, as_at: dt.date) -> dict[int, dict]:
    """Gross premium written on each contract over its term up to the end of
    `as_at`: its current processed files (one per content, as the Financial
    Summary counts them), read the Financial Summary's way, in the contract's
    cap currency. Rows in another currency are counted apart."""
    import financial_figures as ff
    import financial_summary as fs
    import policy_search as ps
    import ceded_engine as ce
    from exception_tally import export_versions
    by_id = {c.id: c for c in contracts}
    hi = _at(as_at + dt.timedelta(days=1))
    exports = [o for o in ps._exports(s, ps.Seat("carrier", tenant_id=tenant_id))
               if o.contract_id in by_id
               and (o.created_at is None or o.created_at.replace(tzinfo=None) < hi)]
    newest: dict[tuple, int] = {}
    for o in exports:
        if (k := o.blob_md5 or o.blob_ref):
            newest[(o.contract_id, k)] = max(newest.get((o.contract_id, k), 0), o.id)
    exports = [o for o in exports if not (o.blob_md5 or o.blob_ref)
               or newest[(o.contract_id, o.blob_md5 or o.blob_ref)] == o.id]
    versions = export_versions(s, [o.id for o in exports]) if exports else {}

    def _in_term(o) -> bool:
        c, b = by_id[o.contract_id], ce.period_bounds(ps._period(o, versions) or "")
        if not b:
            return True        # no reporting month known: processed on it, so counted
        return ((c.inception_dt is None or b[1] >= c.inception_dt)
                and (c.expiry_dt is None or b[0] <= c.expiry_dt))
    exports = [o for o in exports if _in_term(o)]
    out = {c.id: {"written": 0.0, "files": 0, "other_currency_rows": 0} for c in contracts}
    if not exports:
        return out
    latest = fs._latest_templates(s, {o.template_id for o in exports})
    reading = lambda o: latest.get(o.template_id, o.template_id)  # noqa: E731
    structures = ps._structures(s, {reading(o) for o in exports})
    reads = {o.id: fs._read(s, o, structures.get(reading(o)), reading(o)) for o in exports}
    pooled: dict = {}
    for o in exports:
        pooled.setdefault(reading(o), []).append(reads[o.id].evidence)
    conv = {tid: {n: ff.convention(ff.pool(evs).get(n)) for n in ff.RATES}
            for tid, evs in pooled.items()}
    for o in exports:
        acc, cap_cur = out[o.contract_id], (by_id[o.contract_id].premium_cap_currency or "").upper()
        acc["files"] += 1
        for cells in reads[o.id].rows:
            fig = ff.figures(cells, conv.get(reading(o), {}))
            gross = fig["gross_premium"]["value"]
            if gross is None:
                continue
            cur = fs._currency_key(fig)
            if cap_cur and cur.upper() != cap_cur:
                acc["other_currency_rows"] += 1
                continue
            acc["written"] += gross
    return out


def _contracts(s, tenant_id: int, start: dt.date, end: dt.date, as_at: dt.date,
               daily: bool, program_id: Optional[int] = None,
               broker_party_id: Optional[int] = None) -> dict:
    """Contract utilization, both ways:
      use   — of the contracts bound in the period, which sent files in it and
              which sent none;
      cap   — premium written over each capped contract's term so far against
              its premium cap, fullest first; Near cap from NEAR_CAP_PCT, Over
              cap past 100%. A daily report lists only the contracts that sent
              files that day."""
    bound = _bound(s, tenant_id, start, as_at, **_scope(program_id, broker_party_id))
    sent = _files_by_contract(s, tenant_id, start, end, **_scope(program_id, broker_party_id))
    names = _names(s, [c.program_id for c in bound], [c.broker_party_id for c in bound])
    used = [c for c in bound if sent.get(c.id)]
    idle = sorted((c for c in bound if not sent.get(c.id)), key=lambda c: (c.name or "").lower())
    capped = [c for c in bound if c.premium_cap_amount]
    if daily:
        capped = [c for c in capped if sent.get(c.id)]
    written = _written(s, tenant_id, capped, as_at) if capped else {}
    caps = []
    for c in capped:
        cap, w_ = float(c.premium_cap_amount), written[c.id]
        pct = round(100.0 * w_["written"] / cap, 1) if cap else None
        caps.append({**_contract_label(c, names), "currency": c.premium_cap_currency,
                     "cap": cap, "written": round(w_["written"], 2), "used_pct": pct,
                     "flag": ("Over cap" if pct is not None and pct > 100 else
                              "Near cap" if pct is not None and pct >= NEAR_CAP_PCT else None),
                     "other_currency_rows": w_["other_currency_rows"]})
    caps.sort(key=lambda r: -(r["used_pct"] or 0))
    return {"bound": len(bound), "with_files": len(used), "without_files": len(idle),
            "idle": [_contract_label(c, names) for c in idle[:CONTRACTS_SHOWN]],
            "capped": len(caps), "no_cap": sum(1 for c in bound if not c.premium_cap_amount),
            "over_cap": sum(1 for r in caps if r["flag"] == "Over cap"),
            "near_cap": sum(1 for r in caps if r["flag"] == "Near cap"),
            "caps": caps[:CONTRACTS_SHOWN]}


def pack(s, tenant_id: int, *, cadence: str = "month", period: Optional[str] = None,
         today: Optional[dt.date] = None) -> dict:
    """The whole report for one day, week, month or custom dates, as plain data — what the
    screen shows, the files are drawn from, and a sent report keeps."""
    today = today or dt.datetime.utcnow().date()
    w = window(cadence, period, today)
    daily = cadence == "day"
    money, ops, t_now = core(s, tenant_id, w, today)
    return {
        **head(w, today),
        "money": money,
        "operations": ops,
        "timeliness": t_now,
        "problems": (_new_problems(s, tenant_id, w["start"], w["end"], today) if daily
                     else _problems(s, tenant_id, today, w["as_at"])),
        "portfolio": _portfolio_of(s, tenant_id, w, daily),
        "trend": _trend(s, tenant_id, w["start"], w["end"]),
        "history": _history(s, tenant_id, cadence, w["end"]),
        "errors_by_programme": _errors_by_programme(s, tenant_id, w["start"], w["end"]),
        "reinsurance": _reinsurance(s, tenant_id, today, w),
        "quality": _quality(s, tenant_id, w["start"], w["end"]),
        "scorecard": _scorecard(s, tenant_id, w, money.get("premium_by") or {}),
        "late": _late(s, tenant_id, w["start"], w["end"], w["as_at"]),
        "contracts": _contracts(s, tenant_id, w["start"], w["end"], w["as_at"], daily),
    }


TREND_MONTHS = 6


def money_trend(s, tenant_id: int, cadence: str, period: Optional[str],
                today: Optional[dt.date] = None) -> dict:
    """Gross premium, commission and earned premium for the TREND_MONTHS
    bordereau months up to the report's own — the small trend lines in the
    money tiles. The money section's own reader (_money), so each point is the
    tile's number for that month; each call gives a month and the one before,
    so half as many reads. A month in another currency is left out. A daily
    report's money is by the day's files, not by month: no trend."""
    today = today or dt.datetime.utcnow().date()
    w = window(cadence, period, today)
    if cadence == "day":
        return {"currency": None, "months": []}
    key = w["money_month"]
    got: dict[str, dict] = {}
    currency = None
    for back in range(0, TREND_MONTHS, 2):
        m = _money(s, tenant_id, _shift_month(key, -back))
        if currency is None:
            currency = m.get("currency")
        if m.get("currency") != currency:
            continue
        for month, which in ((_shift_month(key, -back), "current"), (_shift_month(key, -back - 1), "previous")):
            figs = {k: (m.get(f) or {}).get(which) for k, f in
                    (("gross", "gross_premium"), ("commission", "commission"), ("earned", "earned_premium"))}
            got.setdefault(month, {"month": month, **figs})
    months = [got[k] for k in sorted(got)][-TREND_MONTHS:]
    return {"currency": currency, "months": months}


def head(w: dict, today: dt.date) -> dict:
    """The period a report covers, as every report writes it."""
    return {
        "cadence": w["cadence"], "period": w["period"],
        "from": w["start"].isoformat(), "to": (w["end"] - dt.timedelta(days=1)).isoformat(),
        "previous": {"period": w["previous"]["period"],
                     "from": w["previous"]["start"].isoformat(),
                     "to": (w["previous"]["end"] - dt.timedelta(days=1)).isoformat()},
        "complete": w["complete"], "as_of": today.isoformat(),
        "as_at": w["as_at"].isoformat(),
        "updated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }


def core(s, tenant_id: int, w: dict, today: dt.date,
         program_id: Optional[int] = None,
         broker_party_id: Optional[int] = None) -> tuple[dict, dict, dict]:
    """Money, files and timeliness for the period against the one before —
    the whole book's, or one programme's (programme_report)."""
    pid = _scope(program_id, broker_party_id)
    now = _operations(s, tenant_id, w["start"], w["end"], **pid)
    before = _operations(s, tenant_id, w["previous"]["start"], w["previous"]["end"], **pid)
    ops = {**now, "previous": before,
           "change": {"files": _change(now["files"], before["files"]),
                      "errors": _change(now["errors"], before["errors"]),
                      "clean_first_time_pct": _change(now["clean_first_time_pct"],
                                                      before["clean_first_time_pct"], True)}}
    if w["cadence"] == "day":
        t_now = _due_in(s, tenant_id, w["start"], w["end"], today, **pid)
        t_before = _due_in(s, tenant_id, w["previous"]["start"], w["previous"]["end"], today, **pid)
    else:
        t_now = _timeliness(s, tenant_id, w["money_month"], today, **pid)
        t_before = _timeliness(s, tenant_id, _shift_month(w["money_month"], -1), today, **pid)
    t_now["previous_on_time_pct"] = t_before["on_time_pct"]
    t_now["change"] = _change(t_now["on_time_pct"], t_before["on_time_pct"], True)
    money = (_money_processed(s, tenant_id, w, **pid) if w["cadence"] == "day"
             else _money(s, tenant_id, w["money_month"], **pid))
    return money, ops, t_now
