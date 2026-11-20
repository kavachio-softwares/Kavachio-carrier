"""
partner_performance.py
──────────────────────
Task 36 — Partner Performance: how often each broker sends a clean file on
time — one row per broker, programme and contract — and the best and worst
brokers across the carrier's book.

ONE BASE: THE FILES DUE (20 Nov 2026). Every card and every share counts the
  same files — the expected files due in the period — so the numbers add up:
  on time + late + not received = files due; passed checks first time is out
  of the files due that came; on time and passed checks is out of the files
  due. (Until 20 Nov "passed checks first time" counted every check run in the
  period, re-uploads included, and "on time and passed checks" left out the
  files with no check on record — three different totals on one screen.)

  on time            The Executive Report scorecard's rule
                     (executive_report._due_tally), file by file: the expected
                     files due in the period (expected_submission, by due
                     date), each judged by submission_calendar.derive_status as
                     at the period's last day. Same files, same denominator.
  late               came in, but after its due date — out of the files due.
  clean first time   of the files due that came: the file as it came (the
                     calendar's received_export_id) passed its first check
                     straight through (check_record.straight_through). A file
                     with no check on record but with exceptions found on it as
                     it came did NOT pass (the check history only starts 20 Oct
                     2026, so older arrivals have exceptions but no check row).
  clean and on time  The ONE new rule. An expected file counts when it came by
                     its due date AND the file that came — the calendar's first
                     arrival (received_export_id; a later correction never
                     repaints it) — passed its first check straight through.
                     A file that came late or not at all fails it, whatever its
                     checks said. Out of the files due. A file that came on
                     time with no check and no exceptions on record is NOT
                     CHECKED: counted on its own so the screen can say so.

CONTRACT = the contract the arrived file was run against (the check run's own,
  else output_exports.contract_id) — also for a file that came only after the
  period ended. For a file that has not come at all: the broker's own contract
  on that programme whose term covers the file's period, leaving out contracts
  that never went in force (contract_types.LIFECYCLE before "active"); the one
  in force first, then the newest. None = the broker has no such contract.

PROBLEMS (exception_tally — the count Exception Triage and the Broker
  Performance card show, never re-counted). Per file that came:
    found     the exceptions on the file as it first came (received_export_id);
    open      the exceptions still not put right on its LIVE version
              (exception_tally.current_export_ids: the newest version of that
              broker + programme + contract + month);
    fixed     found − open, never below 0 — "9 of 10 put right".
  A file whose live version still has open exceptions "still has problems".

FILE TYPE = how the file came in — submission_calendar_service._ways_in, the
  calendar's own "via …": upload (the portal), email, sftp, api, and the
  secure link for corrections. A file with no arrival record says nothing about
  its way in → "Not recorded"; a file that never came → "Not received". With a
  file type picked, a file that never came has no way in, so it is left out.

CLEAN NOW (what Best / Worst and the rank use, 20 Nov 2026) — a file that
  came ON TIME and is clean today: it passed its first check, OR every
  exception found on it has since been resolved (open = 0 on its live
  version). Late or missing fails it; a file still with unresolved exceptions
  fails it. A file with no check recorded and no exceptions known is not
  checked, as above.

RANKING
  Brokers, by clean now and on time, then on time, then most files — among ALL the
  brokers in the period (with the programme and file type picked): picking one
  broker narrows what is shown, never the field it is ranked in. Every broker
  with at least MIN_FILES (one) file whose result is known is ranked; a broker
  whose files are all not checked has no share and is listed unranked. Best and worst never name the same broker, nobody at 0% is a
  best performer and nobody at 100% a worst one.

PERIODS  month 'YYYY-MM' (the Executive Report's own window), quarter
  'YYYY-Qn', year 'YYYY' — each against the one just before, worked out as at
  its last day (or today, while it is running).

Every function takes the caller's session; none opens one. Reading never writes.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any, Callable, Optional

CADENCES = ("month", "quarter", "year")
MIN_FILES = 1
TOP = 5

# File type for a file whose way in was not recorded, and for one that never came.
NOT_RECORDED = "not_recorded"
NOT_RECEIVED = "not_received"

# The four ways in the task names, always offered; anything else stored is
# offered only when a file in the period came that way.
CHANNELS = (("upload", "Portal"), ("email", "Email"), ("sftp", "SFTP"), ("api", "API"))
CHANNEL_LABEL = {**dict(CHANNELS), "secure_link": "Secure Link", "fix_validate": "Fix & Validate",
                 "cloud_folder": "Cloud Folder", NOT_RECORDED: "Not recorded",
                 NOT_RECEIVED: "Not received"}

_QUARTER = re.compile(r"(\d{4})-Q([1-4])")
_YEAR = re.compile(r"\d{4}")


# ── the period ──────────────────────────────────────────────────────────────
def _q_start(y: int, q: int) -> dt.date:
    return dt.date(y, 3 * (q - 1) + 1, 1)


def _q_shift(y: int, q: int, n: int) -> tuple[int, int]:
    i = y * 4 + (q - 1) + n
    return i // 4, i % 4 + 1


def window(cadence: str, period: Optional[str], today: dt.date) -> dict:
    """The dates covered and the ones they are compared with.

    month    'YYYY-MM' — executive_report.window's month; none = last complete.
    quarter  'YYYY-Qn'; none = the quarter running today.
    year     'YYYY';    none = the year running today.
    `as_at` is the last day worked out: the period's last day, or today."""
    from fastapi import HTTPException
    if cadence not in CADENCES:
        raise HTTPException(400, f"period type must be one of {', '.join(CADENCES)}")
    if cadence == "month":
        import executive_report as er
        w = er.window("month", period, today)
        out = {k: w[k] for k in ("cadence", "period", "start", "end", "previous", "complete", "as_at")}
    elif cadence == "quarter":
        if period and not _QUARTER.fullmatch(period):
            raise HTTPException(400, "period must be YYYY-Qn for a quarter, e.g. 2026-Q3")
        if period:
            m = _QUARTER.fullmatch(period)
            y, q = int(m.group(1)), int(m.group(2))
        else:
            y, q = today.year, (today.month - 1) // 3 + 1
        ny, nq = _q_shift(y, q, 1)
        py, pq = _q_shift(y, q, -1)
        start, end = _q_start(y, q), _q_start(ny, nq)
        out = {"cadence": "quarter", "period": f"{y}-Q{q}", "start": start, "end": end,
               "previous": {"period": f"{py}-Q{pq}", "start": _q_start(py, pq), "end": start}}
    else:
        if period and not _YEAR.fullmatch(period):
            raise HTTPException(400, "period must be YYYY for a year")
        y = int(period) if period else today.year
        start, end = dt.date(y, 1, 1), dt.date(y + 1, 1, 1)
        out = {"cadence": "year", "period": str(y), "start": start, "end": end,
               "previous": {"period": str(y - 1), "start": dt.date(y - 1, 1, 1), "end": start}}
    if out["start"] > today:
        raise HTTPException(400, "That period has not started yet.")
    out.setdefault("complete", out["end"] <= today)
    out.setdefault("as_at", min(out["end"] - dt.timedelta(days=1), today))
    return out


def _at(d: dt.date) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day)


def _pct(n: int, d: int) -> Optional[float]:
    return round(100.0 * n / d, 1) if d else None


def _points(now: Optional[float], then: Optional[float]) -> Optional[float]:
    return None if now is None or then is None else round(now - then, 1)


# ── the files ───────────────────────────────────────────────────────────────
def _expected(s, tenant_id: int, start: dt.date, end: dt.date, as_at: dt.date) -> list[dict]:
    """Each expected file due in [start, end) that counts as at the end of
    `as_at` — executive_report._due_tally's rule exactly: not yet due (or due
    today and not in yet) is not counted; on time, or late / not come."""
    from db import ExpectedSubmission
    from submission_calendar import derive_status
    out = []
    for e in (s.query(ExpectedSubmission)
              .filter(ExpectedSubmission.tenant_id == tenant_id,
                      ExpectedSubmission.broker_party_id.isnot(None),
                      ExpectedSubmission.due_date >= start, ExpectedSubmission.due_date < end)):
        got = e.received_at if e.received_at and e.received_at <= as_at else None
        st = derive_status(e.due_date, as_at, 0, got, e.closes_on)
        if st in ("scheduled", "due_soon") or (st == "due_today" and not got):
            continue
        out.append({"id": e.id, "broker": e.broker_party_id, "programme": e.program_id,
                    "schedule_id": e.schedule_id, "period": e.period, "received": got is not None,
                    "export_id": e.received_export_id if got else None,
                    # The file, even if it came only after `as_at` — for its contract.
                    "file_export_id": e.received_export_id,
                    "period_start": e.period_start, "period_end": e.period_end,
                    "on_time": st == "on_time" or (st == "due_today" and got is not None),
                    "late_or_missing": st in ("received_late", "overdue", "never_arrived"),
                    # The two halves of it: came after the due date / not come yet.
                    "late": st == "received_late",
                    "missing": st in ("overdue", "never_arrived")})
    return out


def _first_checks(s, tenant_id: int, export_ids: set, before: dt.datetime) -> dict:
    """{export id: its first processing run recorded before `before`} — the
    file as it arrived (check_record.first_runs' rule: lanes direct and
    canonical, kind run, oldest first; a re-generate is a `rerun`)."""
    import check_record as cr
    from db import CheckRun
    ids = {i for i in export_ids if i}
    if not ids:
        return {}
    first: dict = {}
    for r in (s.query(CheckRun)
              .filter(CheckRun.tenant_id == tenant_id, CheckRun.export_id.in_(ids),
                      CheckRun.lane.in_((cr.DIRECT, cr.CANONICAL)), CheckRun.kind == cr.RUN,
                      CheckRun.created_at < before)
              .order_by(CheckRun.id.asc())):
        first.setdefault(r.export_id, r)
    return first


def _channels(s, export_ids: set) -> dict:
    """{export id: how it came in} — the calendar's own reading."""
    from submission_calendar_service import _ways_in
    ids = {i for i in export_ids if i}
    return _ways_in(s, ids) if ids else {}


def _broker_contracts(s, tenant_id: int, expected: list) -> dict:
    """{(broker, programme): [contracts]} — each broker's own contracts on the
    programmes it owes files on, leaving out any that never went in force."""
    from contract_types import LIFECYCLE
    from db import Contract
    before_force = LIFECYCLE[:LIFECYCLE.index("active")]
    pairs = {(e["broker"], e["programme"]) for e in expected}
    if not pairs:
        return {}
    out: dict = {}
    for c in (s.query(Contract)
              .filter(Contract.tenant_id == tenant_id,
                      Contract.broker_party_id.in_({b for b, _ in pairs}),
                      Contract.program_id.in_({p for _, p in pairs}))):
        if c.lifecycle in before_force or (c.broker_party_id, c.program_id) not in pairs:
            continue
        out.setdefault((c.broker_party_id, c.program_id), []).append(c)
    return out


def _covering(contracts: list, start, end) -> Optional[int]:
    """The contract whose term covers [start, end]: in force first, then the
    newest. An open start or end date covers everything that side."""
    from contract_types import LIFECYCLE
    live = LIFECYCLE[LIFECYCLE.index("active")]
    fits = [c for c in contracts
            if (c.inception_dt is None or end is None or c.inception_dt <= end)
            and (c.expiry_dt is None or start is None or c.expiry_dt >= start)]
    if not fits:
        return None
    best = max(fits, key=lambda c: (c.lifecycle == live, c.inception_dt or dt.date.min, c.id))
    return best.id


def _export_contracts(s, export_ids: set) -> dict:
    from db import OutputExport
    ids = {i for i in export_ids if i}
    return ({i: c for i, c in s.query(OutputExport.id, OutputExport.contract_id)
             .filter(OutputExport.id.in_(ids)).all()} if ids else {})


def contract_names(s, contract_ids: set) -> dict:
    """{contract id: its name} — the Executive Report's label for a contract."""
    from db import Contract
    ids = {i for i in contract_ids if i}
    return ({c.id: (c.name or c.filename or f"Contract #{c.id}")
             for c in s.query(Contract).filter(Contract.id.in_(ids)).all()} if ids else {})


# ── problems on the files that came ─────────────────────────────────────────
def _live_versions(s, tenant_id: int) -> dict:
    """{(broker, programme, contract, month): the live export} — the version of
    each submission that stands now (exception_tally.current_export_ids), and
    the same without the contract for a file whose contract is not known."""
    from db import OutputExport
    from exception_tally import current_export_ids, export_versions
    live = current_export_ids(s, tenant_id=tenant_id, include_replaced=True)
    if not live:
        return {}
    ver = export_versions(s, live)
    out: dict = {}
    for eid, bid, pid, cid in (s.query(OutputExport.id, OutputExport.broker_party_id,
                                       OutputExport.program_id, OutputExport.contract_id)
                               .filter(OutputExport.id.in_(live)).all()):
        v = ver.get(eid) or {}
        month = v.get("reporting_period")
        if not month:
            continue
        for key in ((bid, pid, v.get("contract_id") or cid, month), (bid, pid, None, month)):
            out[key] = max(out.get(key, eid), eid)
    return out


def _tally_export(r) -> dict:
    """exception_tally.export_tally — one file's exceptions and how many are
    still open, matched to its decisions as Exception Triage matches them."""
    from exception_tally import export_tally
    return export_tally(r)


def _problems(s, tenant_id: int, expected: list) -> None:
    """Sets `found`, `open` and `fixed` on each expected file that came (None
    on one that did not)."""
    from db import OutputExport
    came = [e for e in expected if e["received"] and e["export_id"]]
    for e in expected:
        e["found"] = e["open"] = e["fixed"] = None
    if not came:
        return
    live = _live_versions(s, tenant_id)
    now = {}
    for e in came:
        now[e["id"]] = (live.get((e["broker"], e["programme"], e["contract"], e["period"]))
                        or live.get((e["broker"], e["programme"], None, e["period"]))
                        or e["export_id"])
    tallies: dict = {}
    for eid in {e["export_id"] for e in came} | set(now.values()):
        r = s.get(OutputExport, eid)
        if r is None:
            continue
        tallies[eid] = _tally_export(r)
        s.expunge(r)          # the exceptions column runs to megabytes
    for e in came:
        first, current = tallies.get(e["export_id"]), tallies.get(now[e["id"]])
        if first is None or current is None:
            continue
        e["found"] = first["exceptions"]
        e["open"] = current["open"]
        e["fixed"] = max(0, e["found"] - e["open"])


def files_of(s, tenant_id: int, start: dt.date, end: dt.date, as_at: dt.date,
             problems: bool = False) -> tuple[list, list]:
    """(expected files, processed files) for one period, each carrying its
    broker, programme, contract and file type.

    expected  _expected's, plus `clean` (True / False, None = no check
              recorded or nothing came), `clean_on_time` (True / False /
              None = came on time but not checked) and, with `problems`, the
              exceptions found / still open / put right (_problems).
    processed check_record's files of the period — each processed file's first
              run in [start, end) — with `clean` = straight_through."""
    import check_record as cr
    expected = _expected(s, tenant_id, start, end, as_at)
    runs = cr.first_runs(cr.runs_in(s, tenant_id, _at(start), _at(end)))
    first = _first_checks(s, tenant_id, {e["export_id"] for e in expected},
                          _at(as_at + dt.timedelta(days=1)))
    exports = {e["export_id"] for e in expected} | {r.export_id for r in runs}
    exports.discard(None)
    way = _channels(s, exports)
    ex_contract = _export_contracts(s, exports | {e["file_export_id"] for e in expected})
    own = _broker_contracts(s, tenant_id, expected)

    for e in expected:
        run = first.get(e["export_id"]) if e["export_id"] else None
        e["clean"] = cr.straight_through(run) if run is not None else None
        e["contract"] = ((run.contract_id if run is not None else None)
                         or ex_contract.get(e["file_export_id"])
                         or _covering(own.get((e["broker"], e["programme"]), []),
                                      e["period_start"], e["period_end"]))
        e["file_type"] = (NOT_RECEIVED if not e["received"]
                          else way.get(e["export_id"]) or NOT_RECORDED)
    if problems:
        _problems(s, tenant_id, expected)
    for e in expected:
        # No check on record, but exceptions were found on the file as it
        # came: it did not pass first time.
        if e["clean"] is None and e["received"] and e.get("found"):
            e["clean"] = False
        e["clean_on_time"] = e["clean"] if e["on_time"] else False   # None = on time, not checked
    processed = [{"broker": r.broker_party_id, "programme": r.program_id,
                  "contract": r.contract_id or ex_contract.get(r.export_id),
                  "file_type": (way.get(r.export_id) if r.export_id else None) or NOT_RECORDED,
                  "clean": cr.straight_through(r)} for r in runs]
    return expected, processed


# ── counting ────────────────────────────────────────────────────────────────
_COUNTS = ("due", "received", "on_time", "late_or_missing", "late", "missing", "scored", "clean_on_time",
           "not_checked", "checked", "not_checked_came", "files", "clean_first_time", "problems_found", "problems_open",
           "problems_fixed", "files_with_problems", "scored_now", "clean_now")


def clean_now(e: dict) -> Optional[bool]:
    """CLEAN NOW for one expected file (see the module notes): True / False,
    or None when nothing is known about its checks."""
    if not e["on_time"]:
        return False                      # late, or not come at all
    if e["clean"] is True:
        return True                       # passed its first check
    if e.get("found"):
        return e["open"] == 0             # every exception since resolved
    return e["clean"]                     # False, or None = not checked


def _blank() -> dict:
    return {**{k: 0 for k in _COUNTS}, "ways": set()}


def _add_expected(t: dict, e: dict) -> None:
    t["due"] += 1
    t["received"] += e["received"]
    t["on_time"] += e["on_time"]
    t["late_or_missing"] += e["late_or_missing"]
    t["late"] += e["late"]
    t["missing"] += e["missing"]
    if e["clean_on_time"] is None:
        t["not_checked"] += 1
    else:
        t["scored"] += 1
        t["clean_on_time"] += e["clean_on_time"]
    now = clean_now(e)
    if now is not None:
        t["scored_now"] += 1
        t["clean_now"] += now
    if e["received"]:
        t["ways"].add(e["file_type"])
        # Passed checks first time: out of the files due that came.
        if e["clean"] is None:
            t["not_checked_came"] += 1
        else:
            t["checked"] += 1
            t["clean_first_time"] += e["clean"]
    if e.get("found") is not None:
        t["problems_found"] += e["found"]
        t["problems_open"] += e["open"]
        t["problems_fixed"] += e["fixed"]
        t["files_with_problems"] += e["open"] > 0


def _add_processed(t: dict, f: dict) -> None:
    """Check runs in the period — kept as a count only; no card or share uses
    them (see ONE BASE)."""
    t["files"] += 1


def _rates(t: dict) -> dict:
    return {**t, "on_time_pct": _pct(t["on_time"], t["due"]),
            "late_pct": _pct(t["late"], t["due"]),
            "clean_first_time_pct": _pct(t["clean_first_time"], t["received"]),
            "clean_on_time_pct": _pct(t["clean_on_time"], t["due"]),
            "clean_now_pct": _pct(t["clean_now"], t["scored_now"]),
            "fixed_pct": _pct(t["problems_fixed"], t["problems_found"])}


def tally(expected: list, processed: list, key: Optional[Callable[[dict], Any]] = None) -> dict:
    """{key: counts and shares} — or the one total when `key` is None."""
    rows: dict = {}
    k = key or (lambda _x: None)
    for e in expected:
        _add_expected(rows.setdefault(k(e), _blank()), e)
    for f in processed:
        _add_processed(rows.setdefault(k(f), _blank()), f)
    return {i: _rates(t) for i, t in rows.items()}


def _keep(x: dict, programme: Optional[int], file_type: Optional[str],
          broker: Optional[int] = None) -> bool:
    if broker is not None and x["broker"] != broker:
        return False
    if programme is not None and x["programme"] != programme:
        return False
    if file_type is not None and x["file_type"] != file_type:
        return False
    return True


def rank(rows: list[dict]) -> list[dict]:
    """Ranked rows first — most on time and clean now (CLEAN NOW), then most
    on time, then most files — then the rows with no known result to rank on,
    most files due first. `rank` is 1.. or None."""
    ok = [r for r in rows if r["scored_now"] >= MIN_FILES]
    rest = [r for r in rows if r["scored_now"] < MIN_FILES]
    ok.sort(key=lambda r: (-r["clean_now_pct"], -(r["on_time_pct"] or 0),
                           -r["scored_now"], r["name"].lower()))
    rest.sort(key=lambda r: (-r["due"], -r["received"], r["name"].lower()))
    for i, r in enumerate(ok, 1):
        r["rank"] = i
    for r in rest:
        r["rank"] = None
    return ok + rest


def best_and_worst(ranked: list[dict]) -> tuple[list, list]:
    """Up to TOP each, from the ranked rows. With fewer than 2×TOP ranked, the
    top half (rounded up) can be best and the rest worst, so nobody is in both.
    Nobody at 0% is called a best performer and nobody at 100% a worst one —
    when every broker is at 0%, there is no best. Worst lists the very worst
    first."""
    ok = [r for r in ranked if r["rank"] is not None]
    best = [r for r in ok[:min(TOP, math.ceil(len(ok) / 2))] if r["clean_now_pct"] > 0]
    named = {id(r) for r in best}
    worst = [r for r in reversed(ok)
             if id(r) not in named and r["clean_now_pct"] < 100][:TOP]
    return best, worst


# ── the page ────────────────────────────────────────────────────────────────
_SHARES = ("on_time_pct", "late_pct", "clean_first_time_pct", "clean_on_time_pct")


def _figures(now: dict, then: Optional[dict]) -> dict:
    """A row's counts and shares, with the period before and the change."""
    then = then or {}
    return {
        **{k: now[k] for k in _COUNTS}, **{k: now[k] for k in _SHARES}, "fixed_pct": now["fixed_pct"],
        "clean_now_pct": now["clean_now_pct"],
        "file_types": [CHANNEL_LABEL.get(w, w) for w in sorted(now["ways"], key=_way_order)],
        **{f"prev_{k}": then.get(k) for k in _SHARES},
        "prev_received": then.get("received"),
        # Nothing came in either period: 0% against 0% is not "no change".
        "change_pts": (None if not now["received"] and not then.get("received")
                       else _points(now["clean_on_time_pct"], then.get("clean_on_time_pct"))),
    }


def _way_order(w: str) -> tuple:
    order = [k for k, _ in CHANNELS]
    return (order.index(w) if w in order else len(order), w)


def _broker_row(bid: int, now: dict, then: Optional[dict], names: dict) -> dict:
    return {"key": bid, "name": names["broker"].get(bid) or f"#{bid}", "broker_id": bid,
            **_figures(now, then)}


def _options(s, expected: list, processed: list, names: dict, programme: Optional[int],
             file_type: Optional[str], broker: Optional[int]) -> dict:
    """What each filter can be set to in this period: the brokers and
    programmes its files carry and the ways in — the task's four always. A
    value already picked is always offered, so the filter never hides behind
    "All" in another period."""
    progs = {x["programme"] for x in expected + processed if x["programme"]}
    if programme:
        progs.add(programme)
    brokers = {x["broker"] for x in expected + processed if x["broker"]}
    if broker:
        brokers.add(broker)
    missing_p = progs - set(names["programme"])
    missing_b = brokers - set(names["broker"])
    if missing_p or missing_b:
        import executive_report as er
        more = er._names(s, missing_p, missing_b)
        names["programme"].update(more["programme"])
        names["broker"].update(more["broker"])
    seen = {x["file_type"] for x in expected + processed}
    if file_type:
        seen.add(file_type)
    ways = [k for k, _ in CHANNELS] + sorted(seen - {k for k, _ in CHANNELS}
                                            - {NOT_RECORDED, NOT_RECEIVED})
    ways += [k for k in (NOT_RECORDED, NOT_RECEIVED) if k in seen]
    return {
        "brokers": sorted(({"value": b, "label": names["broker"].get(b) or f"#{b}"} for b in brokers),
                          key=lambda o: o["label"].lower()),
        "programmes": sorted(({"value": p, "label": names["programme"].get(p) or f"#{p}"} for p in progs),
                             key=lambda o: o["label"].lower()),
        "file_types": [{"value": k, "label": CHANNEL_LABEL.get(k, k)} for k in ways],
    }


def page(s, tenant_id: int, *, cadence: str = "month", period: Optional[str] = None,
         programme: Optional[int] = None, file_type: Optional[str] = None,
         broker: Optional[int] = None, today: Optional[dt.date] = None) -> dict:
    """Everything the Partner Performance screen shows, as plain data: the
    totals, the best and worst brokers, and one row per broker + programme +
    contract, each carrying its broker's rank.

    With a `broker` picked, the totals and rows are that broker's, it keeps its
    rank among all the brokers, and `picked` says where it stands in place of
    the best and worst lists (which would only name it)."""
    import executive_report as er
    today = today or dt.datetime.utcnow().date()
    w = window(cadence, period, today)
    p = w["previous"]
    exp_now, proc_now = files_of(s, tenant_id, w["start"], w["end"], w["as_at"], problems=True)
    # The period before is judged the same way (exceptions included), so the
    # change compares like with like.
    exp_then, proc_then = files_of(s, tenant_id, p["start"], p["end"],
                                   p["end"] - dt.timedelta(days=1), problems=True)
    # The field every broker is ranked in: the programme and file type picked…
    field = lambda x: _keep(x, programme, file_type)  # noqa: E731
    a_now, a_f_now = [x for x in exp_now if field(x)], [x for x in proc_now if field(x)]
    a_then, a_f_then = [x for x in exp_then if field(x)], [x for x in proc_then if field(x)]
    # …and what is shown: that, narrowed to the broker picked.
    keep = lambda x: broker is None or x["broker"] == broker  # noqa: E731
    e_now, f_now = [x for x in a_now if keep(x)], [x for x in a_f_now if keep(x)]
    e_then, f_then = [x for x in a_then if keep(x)], [x for x in a_f_then if keep(x)]

    brokers = {x["broker"] for x in a_now + a_f_now + a_then + a_f_then if x["broker"]}
    if broker:
        brokers.add(broker)                     # named even in a period it sent nothing
    progs = {x["programme"] for x in e_now + f_now if x["programme"]}
    names = er._names(s, progs, brokers)
    contracts = contract_names(s, {x["contract"] for x in e_now + f_now})

    # ONE BASE: every number counts the files due; check runs only feed the
    # filter options.
    total_now = tally(e_now, []).get(None) or _rates(_blank())
    total_then = tally(e_then, []).get(None) or _rates(_blank())
    summary = {
        **{k: total_now[k] for k in _COUNTS}, **{k: total_now[k] for k in _SHARES},
        "fixed_pct": total_now["fixed_pct"],
        "previous": {k: total_then[k] for k in _SHARES},
        "change_pts": {k: _points(total_now[k], total_then[k]) for k in _SHARES},
    }

    by_broker = lambda x: x["broker"]  # noqa: E731
    then_b = tally(a_then, [], by_broker)
    now_b = tally(a_now, [], by_broker)
    now_b.pop(None, None)                       # a processed file with no broker
    ranked = rank([_broker_row(b, v, then_b.get(b), names) for b, v in now_b.items()])
    ranked_count = sum(1 for r in ranked if r["rank"] is not None)
    if broker is None:
        best, worst, picked = *best_and_worst(ranked), None
    else:
        best, worst = [], []
        mine = next((r for r in ranked if r["broker_id"] == broker), None)
        picked = {"broker_id": broker, "name": names["broker"].get(broker) or f"#{broker}",
                  "rank": mine["rank"] if mine else None, "of": ranked_count,
                  "clean_on_time_pct": mine["clean_on_time_pct"] if mine else None,
                  "clean_now_pct": mine["clean_now_pct"] if mine else None}
    place = {r["broker_id"]: r["rank"] for r in ranked}
    order = {r["broker_id"]: i for i, r in enumerate(ranked)}

    by_contract = lambda x: (x["broker"], x["programme"], x["contract"])  # noqa: E731
    then_c = tally(e_then, [], by_contract)
    rows = []
    for key, v in tally(e_now, [], by_contract).items():
        bid, pid, cid = key
        if bid is None:
            continue
        rows.append({"key": f"{bid}-{pid}-{cid}", "rank": place.get(bid),
                     "broker_id": bid, "broker": names["broker"].get(bid) or f"#{bid}",
                     "programme_id": pid, "programme": names["programme"].get(pid) or "—",
                     "contract_id": cid, "contract": contracts.get(cid) or "No contract",
                     **_figures(v, then_c.get(key))})
    rows.sort(key=lambda r: (order.get(r["broker_id"], len(order)), r["programme"].lower(),
                             r["contract"].lower()))
    return {
        "cadence": w["cadence"], "period": w["period"],
        "from": w["start"].isoformat(), "to": (w["end"] - dt.timedelta(days=1)).isoformat(),
        "previous": {"period": p["period"], "from": p["start"].isoformat(),
                     "to": (p["end"] - dt.timedelta(days=1)).isoformat()},
        "complete": w["complete"], "as_at": w["as_at"].isoformat(),
        "filters": {"programme": programme, "file_type": file_type, "broker": broker},
        "options": _options(s, exp_now, proc_now, names, programme, file_type, broker),
        "min_files": MIN_FILES,
        "summary": summary,
        "best": best, "worst": worst, "picked": picked,
        "ranked_brokers": ranked_count,
        "rows": rows,
    }


# ── the download ────────────────────────────────────────────────────────────
def _cell_pct(v: Optional[float]):
    return None if v is None else v / 100.0


def workbook(data: dict) -> bytes:
    """The ranked table as an Excel file — the screen's own columns."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    cols = [
        ("Rank", 8, lambda r: r["rank"] if r["rank"] is not None else "Not ranked"),
        ("Broker", 26, lambda r: r["broker"]),
        ("Programme", 26, lambda r: r["programme"]),
        ("Contract", 30, lambda r: r["contract"]),
        ("Files due", 11, lambda r: r["due"]),
        ("Files received", 14, lambda r: r["received"]),
        ("On time", 10, lambda r: r["on_time"]),
        ("On time %", 11, lambda r: _cell_pct(r["on_time_pct"])),
        ("Late", 9, lambda r: r["late"]),
        ("Late %", 9, lambda r: _cell_pct(r["late_pct"])),
        ("Missing", 10, lambda r: r["missing"]),
        ("Received and passed checks first time", 18, lambda r: r["clean_first_time"]),
        ("Received and passed checks first time %", 18, lambda r: _cell_pct(r["clean_first_time_pct"])),
        ("On time and passed checks", 16, lambda r: r["clean_on_time"]),
        ("On time and passed checks %", 16, lambda r: _cell_pct(r["clean_on_time_pct"])),
        ("Not checked", 12, lambda r: r["not_checked"]),
        ("On time and clean now", 14, lambda r: r["clean_now"]),
        ("On time and clean now %", 14, lambda r: _cell_pct(r["clean_now_pct"])),
        ("Files with unresolved exceptions", 15, lambda r: r["files_with_problems"]),
        ("Unresolved exceptions", 14, lambda r: r["problems_open"]),
        ("Exceptions found", 14, lambda r: r["problems_found"]),
        ("Exceptions resolved", 14, lambda r: r["problems_fixed"]),
        ("Resolved %", 11, lambda r: _cell_pct(r["fixed_pct"])),
        ("How it came in", 22, lambda r: ", ".join(r["file_types"]) or "Not received"),
    ]
    wb = Workbook()
    ws = wb.active
    ws.title = "Broker Performance"
    ws.append([c[0] for c in cols])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F3A5F")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for r in data["rows"]:
        ws.append([c[2](r) for c in cols])
    for i, (label, width, _f) in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
        if label.endswith("%"):
            for (cell,) in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                cell.number_format = "0.0%"
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    # 45.2 — what every column means, in plain English.
    import export_glossary as gl
    gl.xlsx_sheet(wb)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
