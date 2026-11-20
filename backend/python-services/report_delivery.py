"""MOPR 42.2 — delivering the Operational Report.

The pack (monthly_report.pack) as a PDF or an Excel file, emailed to the
carrier — by itself on the Nth working day after each month ends, or by hand
("Send now"). Always the whole report, beside last month and last year.

  WHO     the carrier itself: every active Carrier login on this tenant (today
          one per carrier). Never a broker, never Kavachio staff, and no
          address outside the app: the report carries the carrier's whole book,
          and who may receive it outside the app is the client's call, not yet
          made.
  WHEN    the daily job (sweep_scheduler) sends last month's report on the Nth
          Monday-to-Friday of this month, or at its next run within a week if
          the app was down that day. Once only: the send row's unique
          scheduled_key makes a second run, or a second app instance, a no-op.
          Automatic sending is OFF until the carrier turns it on.
  WHAT    the numbers go out exactly as they are kept (monthly_report_send.pack),
          so a file downloaded again later is the file people received.
  PERIOD  a month, or — downloaded or sent by hand — a day, a week or dates
          picked (monthly_report.pack_for): `month` on the send row is then that
          period and `options.cadence` says which kind. The automatic send stays
          monthly.
"""
from __future__ import annotations

import io
import logging
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from monthly_report import shift

log = logging.getLogger("report_delivery")

FORMATS = ("pdf", "excel", "both")
DELIVERIES = ("attach", "link")
SEND_DAYS = (1, 2, 3, 4, 5, 10)
# The automatic send: every day, week or month. Dates picked are a one-off,
# so they are for Send now only.
CADENCES = ("day", "week", "month")
WEEK_DAYS = (0, 1, 2, 3, 4)                 # Monday … Friday
# The clocks a carrier can send by (local_time).
TIME_ZONES = {"Europe/London": "UK time", "Asia/Kolkata": "India time", "UTC": "UTC"}
DEFAULTS = {"auto_send": False, "send_day": 3, "file_format": "pdf", "delivery": "attach",
            "cadence": "month", "week_day": 0, "send_hour": 9, "time_zone": "Europe/London",
            # Who: the logins ticked (None = every login) and other addresses.
            "user_ids": None, "emails": []}
# How long after its working day a missed automatic send may still go out.
CATCH_UP_DAYS = 7
SUBJECT_PREFIX = "Kavachio - "
REPORT_FOOTER = (
    "You're receiving this as your carrier's login on Kavachio. When and how it "
    "is sent is set under Operational Report › Send & Schedule.")
PDF_MIME = "application/pdf"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --- when ---------------------------------------------------------------------

def working_day(year: int, month: int, n: int) -> date:
    """The nth Monday-to-Friday of a month: working_day(2026, 11, 3) → Wed 4 Nov."""
    d, seen = date(year, month, 1), 0
    while True:
        if d.weekday() < 5:
            seen += 1
            if seen == n:
                return d
        d += timedelta(days=1)


def _key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def due_now(send_day: int, today: date) -> Optional[str]:
    """The month whose automatic send is due today, or None: last month's
    report, from its working day this month until CATCH_UP_DAYS after."""
    on = working_day(today.year, today.month, send_day)
    if on <= today <= on + timedelta(days=CATCH_UP_DAYS):
        return shift(_key(today), -1)
    return None


def next_send(send_day: int, today: date, sent_months: set[str]) -> dict:
    """When the next automatic send happens, and which month it reports on."""
    on = working_day(today.year, today.month, send_day)
    month = shift(_key(today), -1)
    if month not in sent_months and today <= on + timedelta(days=CATCH_UP_DAYS):
        return {"send_on": max(on, today).isoformat(), "month": month}
    nxt = shift(_key(today), 1)
    y, m = (int(x) for x in nxt.split("-"))
    return {"send_on": working_day(y, m, send_day).isoformat(), "month": _key(today)}


def _last_sunday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def local_time(now: datetime, time_zone: str) -> datetime:
    """`now` (UTC, as stored) on the carrier's clock. UK clocks go forward at
    01:00 UTC on the last Sunday of March and back at 01:00 UTC on the last
    Sunday of October; India is UTC+5:30 all year. Worked out here rather than
    from the system's time-zone files, which a slim server image may not have."""
    if time_zone == "Asia/Kolkata":
        return now + timedelta(hours=5, minutes=30)
    if time_zone == "Europe/London":
        y = now.year
        summer = (datetime.combine(_last_sunday(y, 3), datetime.min.time()) + timedelta(hours=1)
                  <= now < datetime.combine(_last_sunday(y, 10), datetime.min.time()) + timedelta(hours=1))
        return now + timedelta(hours=1) if summer else now
    return now


def _first_day(cfg: dict, today: date) -> date:
    """The first day the send now due may go: today for a day; the weekday
    picked for a week; the working day picked for a month."""
    if cfg["cadence"] == "week":
        return today - timedelta(days=today.weekday()) + timedelta(days=cfg["week_day"])
    if cfg["cadence"] == "month":
        return working_day(today.year, today.month, cfg["send_day"])
    return today


def schedule_due(cfg: dict, now: datetime) -> Optional[tuple[str, str]]:
    """(cadence, period) of the automatic send due at `now`, or None. When, as
    the Executive Report's schedule (executive_delivery.due_now): yesterday's
    report every day; last week's from the weekday picked to the week's end;
    last month's from the working day picked to CATCH_UP_DAYS after — on the
    carrier's clock, from the hour picked. A send missed on its first day (the
    app was down) goes at the next run after it, whatever the hour."""
    import executive_delivery as xd
    local = local_time(now, cfg["time_zone"])
    due = xd.due_now(cfg, local.date())
    if due is None:
        return None
    if local.hour >= cfg["send_hour"] or local.date() > _first_day(cfg, local.date()):
        return due
    return None


def next_run(cfg: dict, now: datetime, sent: set[str]) -> dict:
    """When the next automatic send goes, on the carrier's clock, and what it
    reports on (executive_delivery.next_send). `month` is the period, as the
    screen has always read it."""
    import executive_delivery as xd
    n = xd.next_send(cfg, local_time(now, cfg["time_zone"]).date(), sent)
    return {"send_on": n["send_on"], "month": n["period"], "period": n["period"],
            "cadence": n["cadence"], "title": n["title"],
            "send_hour": cfg["send_hour"], "time_zone": cfg["time_zone"]}


# --- who, and how -------------------------------------------------------------

def people(s, tenant_id: int) -> list[dict]:
    """Who receives the report: the carrier's own active Carrier logins.
    By seat as well as tenant: a Kavachio admin's row can carry a carrier's
    tenant_id too (Users & Roles hides it the same way), and is not the carrier's."""
    from sqlalchemy import func
    from auth_deps import db_role_values
    from db import AppUser
    rows = (s.query(AppUser.id, AppUser.email, AppUser.full_name, AppUser.role)
            .filter(AppUser.tenant_id == tenant_id,
                    AppUser.role.in_(db_role_values("carrier_admin")),
                    func.lower(func.coalesce(AppUser.status, "active")) == "active")
            .order_by(AppUser.full_name).all())
    return [{"user_id": i, "email": e.strip(), "name": (n or "").strip() or None, "role": r}
            for i, e, n, r in rows if (e or "").strip()]


NO_ONE = "This carrier has no active login with an email, so there is no one to send the report to."


def settings(s, tenant_id: int) -> dict:
    from db import MonthlyReportSetting
    row = (s.query(MonthlyReportSetting)
           .filter(MonthlyReportSetting.tenant_id == tenant_id).first())
    if row is None:
        return {**DEFAULTS, "saved": False}
    # How often, when and to whom are kept in `sections` (JSON, unused since
    # 5 Nov 2026), so no column had to be added on every database. Anything
    # else found there — an older shape — reads as the defaults.
    sec = row.sections if isinstance(row.sections, dict) else {}
    sch = sec.get("schedule") if isinstance(sec.get("schedule"), dict) else {}
    out = {"auto_send": bool(row.auto_send), "send_day": row.send_day,
           "file_format": row.file_format, "delivery": row.delivery}
    for k in ("cadence", "week_day", "send_hour", "time_zone"):
        out[k] = sch.get(k, DEFAULTS[k])
    out["user_ids"] = sec.get("user_ids") if isinstance(sec.get("user_ids"), list) else None
    out["emails"] = sec.get("emails") if isinstance(sec.get("emails"), list) else []
    return {**out, "saved": True}


def save_settings(s, tenant_id: int, body: dict, user_id: Optional[int]) -> tuple[dict, dict]:
    """Check and save Send & Schedule. Returns (before, after). Caller commits."""
    from db import MonthlyReportSetting
    before = settings(s, tenant_id)
    send_day = int(body.get("send_day", before["send_day"]))
    if send_day not in SEND_DAYS:
        raise ValueError("Pick one of the working days offered.")
    file_format = str(body.get("file_format", before["file_format"]))
    if file_format not in FORMATS:
        raise ValueError("The file must be PDF, Excel or both.")
    delivery = str(body.get("delivery", before["delivery"]))
    if delivery not in DELIVERIES:
        raise ValueError("Choose to attach the file or send a link.")
    cadence = str(body.get("cadence", before["cadence"]))
    if cadence not in CADENCES:
        raise ValueError("Pick every day, every week or every month.")
    week_day = int(body.get("week_day", before["week_day"]))
    if week_day not in WEEK_DAYS:
        raise ValueError("Pick a weekday, Monday to Friday.")
    send_hour = int(body.get("send_hour", before["send_hour"]))
    if not 0 <= send_hour <= 23:
        raise ValueError("Pick a time of day.")
    time_zone = str(body.get("time_zone", before["time_zone"]))
    if time_zone not in TIME_ZONES:
        raise ValueError("Pick UK time, India time or UTC.")
    import executive_delivery as xd
    raw_ids = body.get("user_ids", before["user_ids"])
    user_ids = None if raw_ids is None else sorted({int(i) for i in raw_ids})
    emails = xd.clean_emails(body.get("emails", before["emails"]))   # names a bad address
    auto = bool(body.get("auto_send", before["auto_send"]))
    if auto and not xd.recipients(s, tenant_id, user_ids, emails):
        raise ValueError(NO_ONE if not people(s, tenant_id) and not emails
                         else "Tick a login or type an email address to send the report to.")

    row = (s.query(MonthlyReportSetting)
           .filter(MonthlyReportSetting.tenant_id == tenant_id).first())
    if row is None:
        row = MonthlyReportSetting(tenant_id=tenant_id)
        s.add(row)
    row.auto_send, row.send_day, row.file_format, row.delivery = auto, send_day, file_format, delivery
    # Keeps what else is there (when the Sent reports list was cleared).
    sec = dict(row.sections) if isinstance(row.sections, dict) else {}
    row.sections = {**sec, "schedule": {"cadence": cadence, "week_day": week_day,
                                        "send_hour": send_hour, "time_zone": time_zone},
                    "user_ids": user_ids, "emails": emails}
    row.updated_by_user_id = user_id
    row.updated_at = datetime.utcnow()
    s.flush()
    return before, settings(s, tenant_id)


# --- the numbers, written the way the screen writes them -----------------------

def _r(x: float) -> int:
    """Half up, as the screen rounds (Python's round() goes to even)."""
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def _num(n) -> str:
    if n is None:
        return "—"
    return f"{n:,}" if isinstance(n, int) else f"{n:,.1f}".rstrip("0").rstrip(".")


def _pct(n) -> str:
    return "—" if n is None else f"{_r(n)}%"


def _took(d) -> str:
    if d is None:
        return "—"
    if d < 1:
        return f"{max(1, _r(d * 24))} h"
    r = round(d * 10) / 10
    return f"{int(r) if r == int(r) else r} {'day' if r == 1 else 'days'}"


def _of(n, of, p) -> str:
    return f"{_num(n)} of {_num(of)} ({_pct(p)})" if of else "—"


def fmt_month(key: str) -> str:
    y, m = (int(x) for x in key.split("-")[:2])
    return date(y, m, 1).strftime("%B %Y")


def _delta(now, then, kind: str, better: str) -> tuple[str, str]:
    """(text, tone) — tone ok | crit | mut, as the screen's pill."""
    if now is None or then is None:
        return "—", "mut"
    d = now - then
    if abs(d) < 0.05:
        return "No change", "mut"
    sign = "+" if d > 0 else "-"
    if kind == "share":
        if then == 0:
            return ("New" if d > 0 else "No change"), "mut"
        size = f"{_r(abs(100 * d / then))}%"
    elif kind == "points":
        size = f"{_r(abs(d))} pts"
    elif kind == "time":
        size = _took(abs(d))
    else:
        size = _num(round(abs(d) * 10) / 10 if isinstance(d, float) else abs(d))
    tone = "mut" if better == "none" else ("ok" if (d > 0) == (better == "up") else "crit")
    return f"{sign}{size}", tone


# A pack for a day, a week or dates picked (monthly_report.pack_for) says so in
# `cadence`; a month's pack — and every pack kept before it existed — has none.
VS_WORDS = {"month": "last month", "week": "last week", "day": "the day before",
            "custom": "the period before"}
END_WORDS = {"month": "month end", "week": "the end of the week", "day": "the end of the day",
             "custom": "the end of the period"}
WHAT_WORDS = {"month": "month", "week": "week", "day": "day", "custom": "period"}


def cadence_of(pack: dict) -> str:
    return pack.get("cadence") or "month"


def title_of(pack: dict, key: Optional[str] = None) -> str:
    """The words for one of the pack's periods (default: its own) —
    'October 2026', 'Week of 9 Nov 2026', '17 Nov 2026', '1 – 18 Nov 2026'."""
    key = key or pack["month"]
    span = (pack.get("spans") or {}).get(key) or {}
    return span.get("title") or fmt_month(key)


def measures(pack: dict) -> list[tuple[str, str, list[dict]]]:
    """The Key numbers, grouped — the same rows and words as the Overview tab."""
    cur = pack["months"].get(pack["month"]) or {}
    unit = " (rows)" if (cur.get("volumes") or {}).get("counted_by") == "rows" else " policies"
    end = END_WORDS[cadence_of(pack)]
    T = lambda m: m["timeliness"]  # noqa: E731
    V = lambda m: m["volumes"]     # noqa: E731
    Q = lambda m: m["quality"]     # noqa: E731
    F = lambda m: m["fixes"]       # noqa: E731

    def row(label, value, show, kind, better, xl):
        return {"label": label, "value": value, "show": show, "kind": kind,
                "better": better, "xl": xl}
    days = f"{pack['target_days']} day{'' if pack['target_days'] == 1 else 's'}"
    return [
        ("timeliness", "Timeliness", [
            row("Files expected", lambda m: T(m)["due"], lambda m: _num(T(m)["due"]), "count", "none", "count"),
            row("Files received on time", lambda m: T(m)["on_time_pct"],
                lambda m: _of(T(m)["on_time"], T(m)["due_so_far"], T(m)["on_time_pct"]), "points", "up", "pct"),
            row("Files received late", lambda m: T(m)["late"], lambda m: _num(T(m)["late"]), "count", "down", "count"),
            row("Overdue", lambda m: T(m)["overdue"], lambda m: _num(T(m)["overdue"]), "count", "down", "count"),
            row("Files never received", lambda m: T(m)["never"], lambda m: _num(T(m)["never"]), "count", "down", "count"),
            row("Average delay", lambda m: T(m)["avg_days_late"], lambda m: _took(T(m)["avg_days_late"]),
                "time", "down", "days"),
            row("Unresolved late-file cases", lambda m: T(m)["cases_open"], lambda m: _num(T(m)["cases_open"]),
                "count", "down", "count"),
        ]),
        ("volumes", "Volumes", [
            row("Files handled", lambda m: V(m)["files"], lambda m: _num(V(m)["files"]), "share", "none", "count"),
            row("Rows handled", lambda m: V(m)["rows"], lambda m: _num(V(m)["rows"]), "share", "none", "count"),
            row(f"New{unit}", lambda m: V(m)["new"], lambda m: _num(V(m)["new"]), "share", "none", "count"),
            row(f"Renewed{unit}", lambda m: V(m)["renewed"], lambda m: _num(V(m)["renewed"]), "share", "none", "count"),
            row(f"Cancelled{unit}", lambda m: V(m)["cancelled"], lambda m: _num(V(m)["cancelled"]),
                "share", "none", "count"),
        ]),
        ("quality", "Data quality", [
            row("Errors found", lambda m: Q(m)["errors"], lambda m: _num(Q(m)["errors"]), "share", "down", "count"),
            row("Critical errors", lambda m: Q(m)["critical"], lambda m: _num(Q(m)["critical"]), "share", "down", "count"),
            row("Rows with errors", lambda m: Q(m)["rows_with_errors_pct"],
                lambda m: _pct(Q(m)["rows_with_errors_pct"]), "points", "down", "pct"),
            row("Files clean first time", lambda m: Q(m)["clean_first_time_pct"],
                lambda m: _of(Q(m)["clean_first_time"], V(m)["files"], Q(m)["clean_first_time_pct"]),
                "points", "up", "pct"),
            row("Most common problem", lambda m: None, lambda m: Q(m)["top_check"] or "—", "count", "none", "text"),
        ]),
        ("fixes", "Fixes", [
            row("Problems fixed", lambda m: F(m)["fixed"], lambda m: _num(F(m)["fixed"]), "share", "none", "count"),
            row("Average fix time", lambda m: F(m)["avg_days"], lambda m: _took(F(m)["avg_days"]),
                "time", "down", "days"),
            row(f"Fixed within target ({days})", lambda m: F(m)["within_target_pct"],
                lambda m: _of(F(m)["within_target"], F(m)["fixed"], F(m)["within_target_pct"]),
                "points", "up", "pct"),
            row(f"Still unresolved at {end}", lambda m: F(m)["waiting"],
                lambda m: _num(F(m)["waiting"]), "share", "down", "count"),
            row(f"Unresolved for more than {days} at {end}", lambda m: F(m)["waiting_past_target"],
                lambda m: _num(F(m)["waiting_past_target"]), "count", "down", "count"),
        ]),
    ]


def _days_word(pack: dict) -> str:
    n = pack["target_days"]
    return f"{n} day{'' if n == 1 else 's'}"


def headline(pack: dict) -> list[tuple[str, str]]:
    """The four numbers at the top of the Overview, for the email."""
    m = pack["months"].get(pack["month"]) or {}
    c = cadence_of(pack)
    if not m.get("has_data"):
        return [(f"This {WHAT_WORDS[c]}", "Nothing was due, checked or fixed")]
    t, q, f = m["timeliness"], m["quality"], m["fixes"]
    return [
        ("Files on time", _of(t["on_time"], t["due_so_far"], t["on_time_pct"])),
        ("Files clean first time", _of(q["clean_first_time"], m["volumes"]["files"],
                                       q["clean_first_time_pct"])),
        ("Errors found", f"{_num(q['errors'])} ({_num(q['critical'])} critical)"),
        ("Average fix time", _took(f["avg_days"])),
        (f"Unresolved for more than {_days_word(pack)} at {END_WORDS[c]}", _num(f["waiting_past_target"])),
    ]


def _columns(pack: dict) -> list[tuple[str, str, Optional[str]]]:
    """(heading, period key, compared-with words): the period, the one before,
    and — a month or a week — the same one last year."""
    cols = [(title_of(pack), pack["month"], None),
            (title_of(pack, pack["last_month"]), pack["last_month"], VS_WORDS[cadence_of(pack)])]
    if pack.get("last_year"):
        cols.append((title_of(pack, pack["last_year"]), pack["last_year"], "last year"))
    return cols


# --- the words around the numbers (both files) -----------------------------------

SEV_WORDS = {"critical": "Critical", "warning": "Warning", "info": "Info"}


def _plural(n: int, word: str) -> str:
    return f"{_num(n)} {word if n == 1 else word + 's'}"


def _fmt_day(d: date) -> str:
    """'Wednesday 18 November 2026'."""
    return f"{d:%A} {d.day} {d:%B %Y}"


def _short_day(iso: str) -> str:
    """'18 Nov 2026'."""
    d = date.fromisoformat(iso[:10])
    return f"{d.day} {d:%b %Y}"


def _age(days) -> str:
    if days is None:
        return "—"
    return "under 1 day" if days < 1 else _plural(int(days), "day")


def _range_of(pack: dict, key: str) -> tuple[date, date]:
    """The first and last day of one of the pack's periods: its span, or — a
    month's pack kept before spans existed — the month's own days."""
    span = (pack.get("spans") or {}).get(key)
    if span:
        return date.fromisoformat(span["from"]), date.fromisoformat(span["to"])
    y, m = (int(x) for x in key.split("-")[:2])
    return date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def _in_period(cadence: str, title: str) -> str:
    """'in October 2026', 'on 17 Nov 2026', 'in the week of 9 Nov 2026'."""
    if cadence == "day":
        return f"on {title}"
    if cadence == "week":
        return f"in the {title[0].lower()}{title[1:]}"
    return f"in {title}"


def covers(pack: dict, running: bool = True) -> str:
    """'Covers 1 – 31 Oct 2026, compared with 1 – 30 Sep 2026 and 1 – 31 Oct
    2025.' — the line beside the screen's period picker. `running` False
    leaves out "Still running…" where the glance above already says it."""
    import monthly_report as mr
    c = cadence_of(pack)
    m = pack["months"].get(pack["month"]) or {}
    if c == "day":
        day = date.fromisoformat(pack["month"])
        if pack["month"] == pack.get("as_of"):
            return f"Today so far, {_fmt_day(day)}, compared with yesterday."
        return f"Covers {_fmt_day(day)}, compared with the day before, {_fmt_day(day - timedelta(days=1))}."

    def span(key):
        a, b = _range_of(pack, key)
        return mr.period_title("custom", f"{a.isoformat()}..{b.isoformat()}")
    vs = span(pack["last_month"]) + (f" and {span(pack['last_year'])}" if pack.get("last_year") else "")
    return (f"Covers {span(pack['month'])}, compared with {vs}."
            + ("" if m.get("complete", True) or not running
               else " Still running, so the numbers count to today."))


def glance(pack: dict, filtered: bool = False) -> str:
    """The period in a few sentences — the screen's "at a glance" box."""
    c = cadence_of(pack)
    m = pack["months"].get(pack["month"]) or {}
    p = pack["months"].get(pack["last_month"]) or {}
    when = _in_period(c, title_of(pack))
    sel = " for this selection" if filtered else ""
    if not m.get("has_data"):
        began = pack.get("records_began")
        since = f"{_short_day(began)}" if began else "when the records began"
        return f"There are no records {when}{sel}: this report counts from {since}."
    before = fmt_month(pack["last_month"]).split(" ")[0] if c == "month" else VS_WORDS[c]
    t, q, f = m["timeliness"], m["quality"], m["fixes"]
    parts = []
    if t["due_so_far"] > 0:
        was = (f", {before}: {_pct(p['timeliness']['on_time_pct'])}"
               if p.get("has_data") and p["timeliness"]["on_time_pct"] is not None else "")
        parts.append(f"{_num(t['on_time'])} of {_plural(t['due_so_far'], 'file')} arrived on time "
                     f"({_pct(t['on_time_pct'])}{was}).")
    elif c != "month" and t["due"] == 0:
        parts.append(f"No files were due {when}.")
    if m["volumes"]["files"] > 0:
        if q["errors"] == 0:
            parts.append("No errors were found.")
        else:
            found = f"{_plural(q['errors'], 'error')} {'was' if q['errors'] == 1 else 'were'} found"
            then = p["quality"]["errors"] if p.get("has_data") else 0
            ch = _r(100 * (q["errors"] - then) / then) if then > 0 else None
            if ch is None:
                parts.append(f"{found}.")
            elif ch == 0:
                parts.append(f"{found}, the same as {before}.")
            else:
                parts.append(f"{found} — {abs(ch)}% {'fewer' if ch < 0 else 'more'} than {before}.")
    if f["fixed"] > 0:
        within = "all" if f["within_target_pct"] == 100 else _pct(f["within_target_pct"])
        parts.append(f"Problems were fixed in {_took(f['avg_days'])} on average, "
                     f"{within} within the {pack['target_days']}-day target.")
    if t["never"] > 0:
        parts.append(f"{_plural(t['never'], 'file')} never arrived.")
    if not parts:
        parts.append(f"Nothing was due, checked or fixed {when}{sel}.")
    if not m.get("complete", True):
        parts.append(f"The {WHAT_WORDS[c]} is still running, so these numbers count to today.")
    return " ".join(parts)


# How to read the colours of the "vs" columns — under the Key numbers table.
COLOUR_KEY = "Green is better, red is worse, grey is only more or less work."


# --- the files -------------------------------------------------------------------

_TONE = {"ok": "#0A6E4C", "crit": "#A31F31", "mut": "#566071"}


def render_pdf(pack: dict, carrier: str, scope: str = "") -> bytes:
    """The pack as an A4 landscape PDF — the Overview tab on paper."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    def para(text, style):
        # reportlab reads markup; a name like "Smith & Co" is text.
        return Paragraph(escape(str(text)), style)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Operational Report — {title_of(pack)}", author="Kavachio")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=18,
                        alignment=0, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontName="Helvetica", fontSize=9.5,
                         textColor=colors.HexColor("#566071"))
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=12,
                        spaceBefore=10, spaceAfter=4)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontName="Helvetica", fontSize=8.5, leading=10.5)
    # Words that can run long (a check's name) wrap inside their own column.
    cell_r = ParagraphStyle("cellr", parent=cell, alignment=2)
    head_l = ParagraphStyle("headl", parent=cell, fontName="Helvetica-Bold", fontSize=8,
                            textColor=colors.HexColor("#566071"))
    body = ParagraphStyle("body", parent=ss["Normal"], fontName="Helvetica", fontSize=10, leading=14)
    small = ParagraphStyle("small", parent=sub, fontSize=8, leading=11)
    teal, line = colors.HexColor("#077282"), colors.HexColor("#E5E8EE")

    def table(rows, widths, group_rows=(), tones=(), left=1):
        """`left` = how many columns, from the first, read left-aligned."""
        t = Table(rows, colWidths=widths, repeatRows=1)
        style = [
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#566071")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F7F8FB")),
            ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
            ("ALIGN", (left, 0), (-1, -1), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, line),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        for i in group_rows:
            style += [("SPAN", (0, i), (-1, i)), ("FONT", (0, i), (-1, i), "Helvetica-Bold", 9),
                      ("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F3F4F7")),
                      ("ALIGN", (0, i), (-1, i), "LEFT")]
        for (r, c, tone) in tones:
            style.append(("TEXTCOLOR", (c, r), (c, r), colors.HexColor(_TONE[tone])))
        t.setStyle(TableStyle(style))
        t.hAlign = "LEFT"   # a narrow table lines up with the headings, not the middle
        return t

    m = pack["months"].get(pack["month"]) or {}
    what = WHAT_WORDS[cadence_of(pack)]
    story = [
        para("Kavachio", ParagraphStyle("brand", parent=sub, textColor=teal, fontName="Helvetica-Bold")),
        para(f"Operational Report — {title_of(pack)}", h1),
        para(" · ".join(x for x in (carrier, scope) if x)
                  + f" · numbers as at {datetime.fromisoformat(pack['as_of']).strftime('%d %B %Y')}", sub),
        Spacer(1, 8),
        # The period in a few sentences (it says when the period is still
        # running), and exactly which dates it compares.
        para(glance(pack, filtered=bool(scope)), body),
        Spacer(1, 2),
        para(covers(pack, running=False), sub),
        Spacer(1, 8),
    ]
    head = headline(pack)
    story.append(table([[para(k, head_l) for k, _ in head], [para(v, cell) for _, v in head]],
                       [doc.width / len(head)] * len(head), left=len(head)))

    cols = _columns(pack)
    groups = measures(pack)
    if groups:
        story.append(para("Key numbers", h2))
        header = ["Measure"]
        for name, _k, vs in cols:
            header.append(name)
            if vs:
                header.append(f"vs {vs}")
        rows, group_rows, tones = [header], [], []
        for _key, title, items in groups:
            group_rows.append(len(rows))
            rows.append([title] + [""] * (len(header) - 1))
            for it in items:
                line_ = [para(it["label"], cell)]
                for _name, k, vs in cols:
                    mm_ = pack["months"].get(k) or {}
                    shown = it["show"](mm_) if mm_.get("has_data") else "Not available"
                    line_.append(para(shown, cell_r) if it["xl"] == "text" else shown)
                    if vs:
                        if m.get("has_data") and mm_.get("has_data") and it["value"](m) is not None:
                            text, tone = _delta(it["value"](m), it["value"](mm_), it["kind"], it["better"])
                        else:
                            text, tone = "—", "mut"
                        tones.append((len(rows), len(line_), tone))
                        line_.append(text)
                rows.append(line_)
        first = doc.width * 0.30
        story.append(table(rows, [first] + [(doc.width - first) / (len(header) - 1)] * (len(header) - 1),
                           group_rows, tones))
        story.append(para(COLOUR_KEY, small))

    story.append(para("By broker and programme", h2))
    rows = [["Broker", "Programme", "Files received on time", "Clean first time", "Errors",
             "Average fix time", f"Unresolved for more than {_days_word(pack)}"]]
    for b in pack["by_broker_programme"]:
        rows.append([para(b.get("broker_name") or "—", cell),
                     para(b.get("programme_name") or "—", cell),
                     _of(b["on_time"], b["due_so_far"], b["on_time_pct"]),
                     _of(b["clean_first_time"], b["files"], b["clean_first_time_pct"]),
                     _num(b["errors"]), _took(b["avg_fix_days"]), _num(b["waiting_past_target"])])
    if len(rows) == 1:
        rows.append([f"Nothing this {what}", "", "", "", "", "", ""])
    story.append(table(rows, [doc.width * w for w in (.18, .18, .13, .11, .08, .11, .21)], left=2))

    # 40.1 — what the checks found most (the Data Quality tab's list, top 5).
    top = pack.get("top_checks")
    if top is not None:
        story.append(para("Checks that failed most", h2))
        rows = [["#", "Check", "Severity", "Files", "Errors"]]
        for i, ch in enumerate(top["checks"][:5], 1):
            rows.append([str(i), para(ch["check"] or "—", cell),
                         SEV_WORDS.get(ch["severity"] or "", "—"), _num(ch["files"]),
                         _num(ch["errors"])])
        if len(rows) == 1:
            rows.append(["", f"No check failed this {what}.", "", "", ""])
        story.append(table(rows, [doc.width * w for w in (.05, .55, .14, .13, .13)], left=2))

    # 41.3 — every error still open, how old, and who has to act (the
    # Exception Resolution tab's numbers, as at the day the file was made).
    w = pack.get("waiting")
    if w is not None:
        rows = [["Unresolved for", "Open problems"]]
        tones = []
        for b in w["by_age"]:
            late = b["past_target"] and b["problems"] > 0
            rows.append([b["label"] + (" · past target" if late else ""), _num(b["problems"])])
            if late:
                tones.append((len(rows) - 1, 0, "crit"))
        # The heading, the counts and the age table stay on one page.
        story.append(KeepTogether([
            para(f"Unresolved problems, as at {_short_day(w['as_at'])}", h2),
            para(f"{_num(w['problems'])} unresolved · {_num(w['past_target'])} unresolved for more than "
                 f"{_days_word(pack)} · oldest problem {_age(w['oldest_days'])}.", sub),
            Spacer(1, 4),
            table(rows, [doc.width * x for x in (.24, .1)], tones=tones),
        ]))
        story.append(Spacer(1, 8))
        shown = w["by_owner"][:10]
        rows = [["Owner", "Programme", "Unresolved", f"Over {_days_word(pack)}", "Oldest problem"]]
        tones = []
        for g in shown:
            rows.append([para(g.get("owner") or "—", cell), para(g.get("programme_name") or "—", cell),
                         _num(g["open"]), _num(g["past_target"]), _age(g["oldest_days"])])
            if g["past_target"]:
                tones.append((len(rows) - 1, 3, "crit"))
        if len(rows) == 1:
            rows.append(["Nothing is waiting to be fixed.", "", "", "", ""])
        story.append(table(rows, [doc.width * x for x in (.28, .28, .12, .16, .16)],
                           tones=tones, left=2))
        if len(w["by_owner"]) > len(shown):
            story.append(para(f"…and {len(w['by_owner']) - len(shown)} more — every one is in "
                              "the Excel file.", small))

    # 45.2 — the Column Guide, read from the tables above: only the columns
    # this PDF really has, each in plain English.
    import export_glossary as gl
    gl_rows = gl.pdf_rows(story, target=_days_word(pack))
    story.append(PageBreak())          # the guide always starts on a fresh page
    story.append(KeepTogether([para(gl.TITLE, h2), para(gl.INTRO, small), Spacer(1, 4)]))
    story.append(table([[para(c, head_l if i == 0 else cell) for c in r] for i, r in enumerate(gl_rows)],
                       [doc.width * w for w in (.22, .56, .22)], left=3))

    def _footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#8B93A2"))
        canvas.drawString(d.leftMargin, 8 * mm, f"Kavachio · Operational Report — {title_of(pack)}")
        canvas.drawRightString(d.leftMargin + d.width, 8 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def render_xlsx(pack: dict, carrier: str, scope: str = "") -> bytes:
    """The pack as an Excel workbook — the same report, with numbers you can
    work with: counts as numbers, shares as percentages, times in days."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Key numbers"
    bold, grey = Font(bold=True), PatternFill("solid", fgColor="F3F4F7")
    tone_font = {t: Font(color=c.lstrip("#")) for t, c in _TONE.items()}

    def put(sheet, r, c, v, fmt=None, font=None, fill=None):
        x = sheet.cell(row=r, column=c, value=v)
        if fmt:
            x.number_format = fmt
        if font:
            x.font = font
        if fill:
            x.fill = fill
        return x

    def xl(it, mm_):
        """(value, number format) for one measure in one month."""
        if not mm_.get("has_data"):
            return "Not available", None
        v = it["value"](mm_)
        if it["xl"] == "text" or v is None:
            return it["show"](mm_), None
        if it["xl"] == "pct":
            return v / 100.0, "0%"
        if it["xl"] == "days":
            return v, '0.0 "days"'
        return v, "#,##0"

    cols = _columns(pack)
    m = pack["months"].get(pack["month"]) or {}
    header = ["Measure"]
    for name, _k, vs in cols:
        header.append(name)
        if vs:
            header.append(f"vs {vs}")
    width = len(header)
    tables: list[tuple] = []   # (sheet, first row, last row, last column) to box

    def banner(sheet, r, text, cols, *, font=None, height=None, center=False):
        """One line across a table's width: a title, or a sentence that wraps."""
        x = put(sheet, r, 1, text, font=font)
        sheet.merge_cells(start_row=r, start_column=1, end_row=r, end_column=cols)
        x.alignment = Alignment(horizontal="center" if center else "left", wrap_text=True)
        if height:
            sheet.row_dimensions[r].height = height

    # The title sits on top of the table, centred across it, as the box's
    # first line; under it the period in a few sentences and the dates it
    # compares. A file narrowed to a programme or broker says which.
    banner(ws, 1, f"Operational Report — {title_of(pack)}" + (f" · {scope}" if scope else ""), width,
           font=Font(bold=True, size=14), height=26, center=True)
    said = glance(pack, filtered=bool(scope))
    banner(ws, 2, said, width, height=15 * (1 + len(said) // 120))
    banner(ws, 3, covers(pack, running=False), width, font=Font(color="566071"), height=18)
    r = 4
    for c, h in enumerate(header, 1):
        put(ws, r, c, h, font=bold, fill=grey)
    for _key, title, items in measures(pack):
        r += 1
        put(ws, r, 1, title, font=bold)
        for it in items:
            r += 1
            put(ws, r, 1, it["label"])
            c = 1
            for _name, k, vs in cols:
                c += 1
                mm_ = pack["months"].get(k) or {}
                v, fmt = xl(it, mm_)
                put(ws, r, c, v, fmt)
                if vs:
                    c += 1
                    if m.get("has_data") and mm_.get("has_data") and it["value"](m) is not None:
                        text, tone = _delta(it["value"](m), it["value"](mm_), it["kind"], it["better"])
                    else:
                        text, tone = "—", "mut"
                    put(ws, r, c, text, font=tone_font[tone])
    tables.append((ws, 1, r, width))
    # How to read the colours, under the table and outside its box.
    banner(ws, r + 2, COLOUR_KEY, width, font=Font(size=9, color="566071"), height=13)
    ws.column_dimensions["A"].width = 42
    for col in "BCDEF":
        ws.column_dimensions[col].width = 18

    wb2 = wb.create_sheet("By broker and programme")
    heads = ["Broker", "Programme", "Files expected so far", "Files received on time", "On time %",
             "Files checked", "Clean first time", "Clean first time %", "Errors",
             "Average fix time (days)", f"Unresolved for more than {_days_word(pack)}"]
    for c, h in enumerate(heads, 1):
        put(wb2, 1, c, h, font=bold, fill=grey)
    for i, b in enumerate(pack["by_broker_programme"], 2):
        vals = [b.get("broker_name"), b.get("programme_name"), b["due_so_far"], b["on_time"],
                (b["on_time_pct"] / 100.0) if b["on_time_pct"] is not None else None,
                b["files"], b["clean_first_time"],
                (b["clean_first_time_pct"] / 100.0) if b["clean_first_time_pct"] is not None else None,
                b["errors"], b["avg_fix_days"], b["waiting_past_target"]]
        for c, v in enumerate(vals, 1):
            put(wb2, i, c, v, "0%" if c in (5, 8) else ("0.0" if c == 10 else None))
    for col, w in zip("ABCDEFGHIJK", (28, 28, 22, 23, 11, 13, 15, 16, 10, 20, 33)):
        wb2.column_dimensions[col].width = w
    tables.append((wb2, 1, wb2.max_row, len(heads)))

    # 40.1 — the checks that failed most in the period (top 10).
    top = pack.get("top_checks")
    if top is not None:
        sh = wb.create_sheet("Checks that failed most")
        heads = ["#", "Check", "Severity", "Files", "Errors"]
        for c, h in enumerate(heads, 1):
            put(sh, 1, c, h, font=bold, fill=grey)
        for i, ch in enumerate(top["checks"], 2):
            put(sh, i, 1, i - 1)
            put(sh, i, 2, ch["check"])
            put(sh, i, 3, SEV_WORDS.get(ch["severity"] or "", None))
            put(sh, i, 4, ch["files"], "#,##0")
            put(sh, i, 5, ch["errors"], "#,##0")
        if not top["checks"]:
            put(sh, 2, 2, f"No check failed this {WHAT_WORDS[cadence_of(pack)]}.")
        for col, w in zip("ABCDE", (6, 60, 12, 10, 10)):
            sh.column_dimensions[col].width = w
        tables.append((sh, 1, sh.max_row, len(heads)))

    # 41.3 — every error still open as at the day the file was made: how old,
    # and who has to act; then the whole list, one row per file and check.
    wt = pack.get("waiting")
    if wt is not None:
        sh = wb.create_sheet("Unresolved problems")
        banner(sh, 1, f"As at {_short_day(wt['as_at'])}: {_num(wt['problems'])} unresolved, "
                      f"{_num(wt['past_target'])} unresolved for more than {_days_word(pack)}, "
                      f"oldest problem {_age(wt['oldest_days'])}.", 5, font=bold, height=20)
        tables.append((sh, 1, 1, 5))
        heads = ["Unresolved for", "Open problems", "Past the target"]
        for c, h in enumerate(heads, 1):
            put(sh, 3, c, h, font=bold, fill=grey)
        for i, b in enumerate(wt["by_age"], 4):
            put(sh, i, 1, b["label"])
            put(sh, i, 2, b["problems"], "#,##0")
            put(sh, i, 3, "Yes" if b["past_target"] else "No",
                font=tone_font["crit"] if b["past_target"] and b["problems"] else None)
        tables.append((sh, 3, 3 + len(wt["by_age"]), len(heads)))
        first = 5 + len(wt["by_age"])
        heads = ["Owner", "Programme", "Unresolved", f"Over {_days_word(pack)}", "Oldest problem (days)"]
        for c, h in enumerate(heads, 1):
            put(sh, first, c, h, font=bold, fill=grey)
        for i, g in enumerate(wt["by_owner"], first + 1):
            put(sh, i, 1, g.get("owner"))
            put(sh, i, 2, g.get("programme_name"))
            put(sh, i, 3, g["open"], "#,##0")
            put(sh, i, 4, g["past_target"], "#,##0", font=tone_font["crit"] if g["past_target"] else None)
            put(sh, i, 5, g["oldest_days"], "0.0")
        if not wt["by_owner"]:
            put(sh, first + 1, 1, "Nothing is waiting to be fixed.")
        tables.append((sh, first, sh.max_row, len(heads)))
        for col, w in zip("ABCDE", (34, 34, 12, 20, 24)):
            sh.column_dimensions[col].width = w

        sh = wb.create_sheet("Unresolved problems list")
        heads = ["Owner", "Programme", "Unresolved for (days)", "Problem", "Severity",
                 "Count", f"Over {_days_word(pack)}", "File", "Period"]
        for c, h in enumerate(heads, 1):
            put(sh, 1, c, h, font=bold, fill=grey)
        for i, g in enumerate(wt["groups"], 2):
            vals = [g.get("owner"), g.get("programme_name"), g["oldest_days"], g["check"],
                    SEV_WORDS.get(g["severity"] or "", None), g["problems"], g["past_target"],
                    g["file"], g["period"]]
            for c, v in enumerate(vals, 1):
                put(sh, i, c, v, "0.0" if c == 3 else ("#,##0" if c in (6, 7) else None))
        if not wt["groups"]:
            put(sh, 2, 1, "Nothing is waiting to be fixed.")
        for col, w in zip("ABCDEFGHI", (30, 30, 18, 48, 12, 10, 18, 40, 12)):
            sh.column_dimensions[col].width = w
        tables.append((sh, 1, sh.max_row, len(heads)))

    # 45.2 — the Column Guide, read from the sheets above.
    import export_glossary as gl
    gl.xlsx_sheet(wb, target=_days_word(pack))

    # Every cell of each table — the Key numbers title lines too — in a bold
    # (medium) mid-grey box: clearly a border, but not black. Notes under a
    # table stay outside it.
    line = Side(style="medium", color="8B93A2")
    box = Border(left=line, right=line, top=line, bottom=line)
    for sheet, r1, r2, c2 in tables:
        for row in sheet.iter_rows(min_row=r1, max_row=r2, max_col=c2):
            for x in row:
                x.border = box

    # Middle of the row, keeping a line's own centring and wrapping.
    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for x in row:
                x.alignment = Alignment(horizontal=x.alignment.horizontal, vertical="center",
                                        wrap_text=x.alignment.wrap_text)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def files(pack: dict, file_format: str, carrier: str,
          scope: str = "") -> list[tuple[str, bytes, str]]:
    """(filename, data, mime) for each file the format asks for."""
    c = cadence_of(pack)
    name = (f"Operational Report {pack['month']}" if c in ("month", "day")
            else f"Operational Report week of {pack['month']}" if c == "week"
            else f"Operational Report {pack['month'].replace('..', ' to ')}")
    out = []
    if file_format in ("pdf", "both"):
        out.append((f"{name}.pdf", render_pdf(pack, carrier, scope), PDF_MIME))
    if file_format in ("excel", "both"):
        out.append((f"{name}.xlsx", render_xlsx(pack, carrier, scope), XLSX_MIME))
    return out


# --- sending -----------------------------------------------------------------------

def carrier_name(s, tenant_id: int) -> str:
    from db import Tenant
    t = s.query(Tenant).filter(Tenant.id == tenant_id).first()
    return (t.legal_name or (t.tenant_name or "").title() or f"Carrier {tenant_id}") if t else ""


def build_pack(s, tenant_id: int, month: str, now: Optional[datetime] = None,
               cadence: str = "month") -> dict:
    """The whole carrier's pack, as the files carry it: the period (`month` is
    a month, or the day, week or dates picked — monthly_report.pack_for) and
    what only the files show (add_file_detail)."""
    import monthly_report as mr
    now = now or datetime.utcnow()
    pack = mr.pack_for(s, tenant_id, cadence, month, now=now)
    mr.add_file_detail(s, tenant_id, pack, now=now)
    return mr.name_rows(s, pack)


def _status(delivered: int, failed: int) -> str:
    if delivered and not failed:
        return "sent"
    return "partly" if delivered else "failed"


def send(s, tenant_id: int, month: str, *, kind: str, user_id: Optional[int] = None,
         now: Optional[datetime] = None,
         mailer: Optional[Callable[..., None]] = None, cadence: str = "month",
         user_ids: Optional[list] = None, emails: Optional[list] = None,
         file_format: Optional[str] = None) -> Optional[dict]:
    """Build, email and record one report. `kind`: scheduled | now.

    It goes to the carrier's logins (people). A scheduled send claims its month
    first and returns None when it was already sent. Commits.
    `cadence` day | week | custom sends that period instead (by hand only —
    the automatic send is monthly); `month` is then the period, as the screen
    names it ('2026-11-17', the week's Monday, or 'from..to').

    Send now's popup — the Executive Report's — picks who gets it: the logins
    ticked (`user_ids`, the carrier's own only: anyone else is ignored) and
    other addresses typed (`emails`), counted and checked as the Executive
    Report does (executive_delivery.recipients); and the format
    (`file_format`), and then the file goes along whatever Send & Schedule
    says. The automatic send still goes to every login."""
    from sqlalchemy.exc import IntegrityError
    from db import MonthlyReportSend
    from notifications import NOTIFY_MAIL_ACCOUNT, _app_link, _email_text, notification_email_html
    if mailer is None:
        from email_utils import send_email as mailer
    now = now or datetime.utcnow()
    cfg = settings(s, tenant_id)
    if user_ids is None and not emails:
        to = people(s, tenant_id)
        if not to:
            raise ValueError(NO_ONE)
    else:
        import executive_delivery as xd
        # Names any typed address that is not one (ValueError).
        to = xd.recipients(s, tenant_id, user_ids, emails)
        if not to:
            raise ValueError("Tick a login or type an email address to send the report to.")
    if file_format is not None and file_format not in FORMATS:
        raise ValueError("The file must be PDF, Excel or both.")
    fmt = file_format or cfg["file_format"]

    monthly = cadence == "month"
    row = MonthlyReportSend(tenant_id=tenant_id, month=month, kind=kind,
                            # A month keeps the key it always had; a day or a
                            # week says which, so neither takes the other's.
                            scheduled_key=((f"{tenant_id}:{month}" if monthly
                                            else f"{tenant_id}:{cadence}:{month}")
                                           if kind == "scheduled" else None),
                            file_format=fmt, delivery="attach" if file_format else cfg["delivery"],
                            sent_by_user_id=user_id, created_at=now,
                            # Which kind of period `month` is, from the start,
                            # so even a send that failed can be named.
                            options=None if monthly else {"cadence": cadence})
    s.add(row)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        return None   # this month's automatic send has already gone

    carrier = carrier_name(s, tenant_id)
    # The carrier's name as it was, so a file downloaded again later reads the same.
    options = {"carrier": carrier} if monthly else {"carrier": carrier, "cadence": cadence}
    link = _app_link(f"/monthly-report?month={month}" if monthly
                     else f"/monthly-report?cadence={cadence}&period={month}")
    # A link is only a link when the app's address is configured; otherwise
    # the file goes along so the email is never empty-handed.
    attach = bool(file_format) or cfg["delivery"] == "attach" or not link
    # An address typed in has no login to sign in with: it always gets the
    # file, never the link (as the Executive Report sends it).
    outside = any(not p.get("user_id") for p in to)
    try:
        pack = build_pack(s, tenant_id, month, now=now, cadence=cadence)
        attachments = files(pack, fmt, carrier) if attach or outside else []
    except Exception:
        # Nothing has gone out: drop the claim so the next run can try again.
        s.rollback()
        s.delete(s.merge(row))
        s.commit()
        raise
    named = title_of(pack)
    title = f"Operational Report — {named}"
    subject = SUBJECT_PREFIX + f"Operational Report · {named}"
    attached = f"The {carrier} Operational Report for {named} is attached."
    facts = headline(pack)

    results = []
    for p in to:
        mine = bool(p.get("user_id"))
        their_link = link if mine else None
        if mine and not attach:
            body = f"The {carrier} Operational Report for {named} is ready. Sign in to open it."
        else:
            body = attached
        footer = REPORT_FOOTER if mine else f"Sent from {carrier} on Kavachio."
        try:
            mailer(p["email"], subject,
                   notification_email_html(title, body, facts, their_link, "Open the Operational Report",
                                           greeting_name=p.get("name"), footer=footer),
                   text=_email_text(title, body, facts, their_link), account=NOTIFY_MAIL_ACCOUNT,
                   attachments=(attachments if attach or not mine else None) or None)
            results.append({**{k: p.get(k) for k in ("user_id", "email", "name")},
                            "delivered": True, "error": None})
        except Exception as e:  # noqa: BLE001 — one bad address must not stop the rest
            log.warning("operational report %s to %s failed: %s", month, p["email"], e)
            results.append({**{k: p.get(k) for k in ("user_id", "email", "name")},
                            "delivered": False, "error": str(e)[:300]})

    row.recipients = results
    row.delivered = sum(1 for r in results if r["delivered"])
    row.failed = len(results) - row.delivered
    row.pack, row.options = pack, options
    s.commit()
    return {"id": row.id, "month": month, "kind": kind, "cadence": cadence, "title": named,
            "delivered": row.delivered,
            "failed": row.failed, "status": _status(row.delivered, row.failed),
            "recipients": [{k: r[k] for k in ("name", "email", "delivered")} for r in results]}


def _sent_cadence(r) -> str:
    """Which kind of period a sent report covered: a month unless it says so."""
    return (r.options or {}).get("cadence") or "month"


def _sent_title(r) -> str:
    """A sent report's period in words, from the numbers kept with it — or, for
    a send that failed before they were kept, from the period itself."""
    if r.pack:
        return title_of(r.pack)
    c = _sent_cadence(r)
    if c == "month":
        return fmt_month(r.month)
    import monthly_report as mr
    try:
        return mr.period_title(c, r.month)
    except (ValueError, TypeError):
        return r.month


def _cleared_at(s, tenant_id: int) -> Optional[datetime]:
    """When the Sent reports list was last cleared (kept in the settings row's
    `sections`, so no column was added), or None."""
    from db import MonthlyReportSetting
    row = (s.query(MonthlyReportSetting)
           .filter(MonthlyReportSetting.tenant_id == tenant_id).first())
    sec = row.sections if row is not None and isinstance(row.sections, dict) else {}
    try:
        return datetime.fromisoformat(sec["sent_cleared_at"]) if sec.get("sent_cleared_at") else None
    except (TypeError, ValueError):
        return None


def _listed(s, tenant_id: int):
    """The sends the Sent reports list shows: all but those cleared from it."""
    from db import MonthlyReportSend
    q = s.query(MonthlyReportSend).filter(MonthlyReportSend.tenant_id == tenant_id)
    cleared = _cleared_at(s, tenant_id)
    return q.filter(MonthlyReportSend.created_at > cleared) if cleared else q


def history(s, tenant_id: int, limit: int = 24, offset: int = 0) -> list[dict]:
    """The reports sent, newest first, with who they went to and who sent them
    — `limit` of them from `offset` (count_sends), leaving out the ones
    cleared from the list (clear_history)."""
    from db import AppUser, MonthlyReportSend
    rows = (_listed(s, tenant_id)
            .order_by(MonthlyReportSend.created_at.desc(), MonthlyReportSend.id.desc())
            .offset(max(offset, 0)).limit(limit).all())
    senders = {u.id: ((u.full_name or "").strip() or u.email) for u in s.query(AppUser).filter(
        AppUser.id.in_({r.sent_by_user_id for r in rows if r.sent_by_user_id} or {0}))}
    return [{"id": r.id, "month": r.month, "kind": r.kind,
             "cadence": _sent_cadence(r), "title": _sent_title(r),
             "sent_at": r.created_at.isoformat() + "Z" if r.created_at else None,
             "sent_by": senders.get(r.sent_by_user_id) if r.sent_by_user_id else None,
             "file_format": r.file_format, "delivery": r.delivery,
             "people": len(r.recipients or []),
             "recipients": [{k: x.get(k) for k in ("name", "email", "delivered")}
                            for x in (r.recipients or [])],
             "delivered": r.delivered, "failed": r.failed,
             "status": _status(r.delivered, r.failed) if r.pack is not None else "failed",
             "has_file": r.pack is not None} for r in rows]


def count_sends(s, tenant_id: int) -> int:
    """How many reports the Sent reports list holds, for its pages."""
    return _listed(s, tenant_id).count()


def clear_history(s, tenant_id: int, now: Optional[datetime] = None) -> int:
    """Clear the Sent reports list, as the Executive Report's does. Nothing is
    deleted: the sends, who they went to and their files are kept, and
    sent_keys still counts them, so the schedule never sends one twice.
    Returns how many were hidden; the caller commits."""
    from db import MonthlyReportSetting
    n = count_sends(s, tenant_id)
    if not n:
        return 0
    row = (s.query(MonthlyReportSetting)
           .filter(MonthlyReportSetting.tenant_id == tenant_id).first())
    if row is None:
        row = MonthlyReportSetting(tenant_id=tenant_id)
        s.add(row)
    sec = dict(row.sections) if isinstance(row.sections, dict) else {}
    row.sections = {**sec, "sent_cleared_at": (now or datetime.utcnow()).isoformat()}
    s.flush()
    return n


def sent_months(s, tenant_id: int) -> set[str]:
    """The months whose automatic send has gone."""
    from db import MonthlyReportSend
    return {m for (m,) in s.query(MonthlyReportSend.month)
            .filter(MonthlyReportSend.tenant_id == tenant_id,
                    MonthlyReportSend.kind == "scheduled").all()}


def sent_keys(s, tenant_id: int) -> set[str]:
    """'<cadence>:<period>' of every automatic send that has gone."""
    from db import MonthlyReportSend
    return {f"{(o or {}).get('cadence') or 'month'}:{m}"
            for m, o in s.query(MonthlyReportSend.month, MonthlyReportSend.options)
            .filter(MonthlyReportSend.tenant_id == tenant_id,
                    MonthlyReportSend.kind == "scheduled").all()}


def run_due(s, now: Optional[datetime] = None,
            mailer: Optional[Callable[..., None]] = None) -> int:
    """The hourly job: send every carrier's report that is due now — every
    day, week or month, from the hour it picked on its own clock — to the
    logins it ticked and the addresses it typed. Returns how many went out.
    One carrier's failure never stops the others."""
    from db import ActivityEvent, MonthlyReportSetting
    now = now or datetime.utcnow()
    sent = 0
    for row in (s.query(MonthlyReportSetting)
                .filter(MonthlyReportSetting.auto_send.is_(True)).all()):
        tid = row.tenant_id
        cfg = settings(s, tid)
        due = schedule_due(cfg, now)
        if due is None or f"{due[0]}:{due[1]}" in sent_keys(s, tid):
            continue
        cadence, period = due
        try:
            r = send(s, tid, period, kind="scheduled", now=now, mailer=mailer, cadence=cadence,
                     user_ids=cfg["user_ids"], emails=cfg["emails"])
        except Exception:  # noqa: BLE001
            log.exception("operational report: carrier %s, %s %s failed", tid, cadence, period)
            s.rollback()
            continue
        if r is None:
            continue
        details = {"month": period, "kind": "scheduled",
                   "delivered": r["delivered"], "failed": r["failed"]}
        if cadence != "month":
            details.update(cadence=cadence, period=r["title"])
        s.add(ActivityEvent(tenant_id=tid, actor="system", action="monthly_report_sent",
                            target=f"monthly_report:{period}", details=details))
        s.commit()
        sent += 1
    return sent
