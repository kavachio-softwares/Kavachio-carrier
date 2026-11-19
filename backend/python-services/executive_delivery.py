"""Executive Reporting 38.2 — delivering the Executive Report.

The pack (executive_report.pack) as a PDF or an Excel file, downloaded, or
emailed — by hand ("Send") or by itself each week or month ("Schedule").

  FROM    "<Carrier> via Kavachio", replies to the carrier person who sent it
          (automatic sends: whoever last saved the schedule). The address is
          Kavachio's own mailbox — a carrier's mail password is never asked for.
  WHO     the carrier's own logins it ticks, AND any other email address it
          types: leadership reads the report without logging in (decision of
          12 Nov 2026 — the Monthly Report still goes to logins only). Outside
          addresses still pass through email_utils' MAIL_ALLOWED_RECIPIENTS
          guard, which a development machine keeps narrow.
  WHEN    weekly: last week's report (Monday to Sunday), on the weekday chosen
          or at the next run that week if the app was down that day. Monthly:
          last month's, on the Nth working day (report_delivery.working_day),
          up to report_delivery.CATCH_UP_DAYS late. Once only: the send row's
          unique scheduled_key. Automatic sending is OFF until turned on.
  WHAT    the numbers go out exactly as they are kept (executive_report_send
          .pack), so a file downloaded again later is the file people received.

Formatting, the email template and the mail account are the Monthly Report's
(report_delivery, notifications) — one look for every report Kavachio sends.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import executive_report as er
import report_delivery as rd

log = logging.getLogger("executive_delivery")

FORMATS = ("pdf", "excel", "both")
WEEK_DAYS = (0, 1, 2, 3, 4)                 # Monday … Friday
SEND_DAYS = rd.SEND_DAYS
MAX_EMAILS = 50
DEFAULTS = {"auto_send": False, "cadence": "month", "week_day": 0, "send_day": 3,
            "file_format": "pdf", "user_ids": None, "emails": []}
_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

NO_ONE = "Choose at least one person to send the report to."


# ── names and numbers ───────────────────────────────────────────────────────
SCHEDULE_CADENCES = ("day", "week", "month")       # Custom dates are picked by hand


def period_title(cadence: str, period: str) -> str:
    """'October 2026', 'Week of 2 Nov 2026', '11 Nov 2026' or '1 – 15 Oct 2026'."""
    if cadence == "month":
        return rd.fmt_month(period)
    if cadence == "custom":
        return _span(*period.split(".."))
    d = date.fromisoformat(period)
    if cadence == "day":
        return f"{d.day} {d:%b %Y}"
    return f"Week of {d.day} {d:%b %Y}"


def _span(a: str, b: str) -> str:
    """'1 – 15 Oct 2026', '28 Sep – 4 Oct 2026', '20 Dec 2025 – 5 Jan 2026'; one day '3 Oct 2026'."""
    x, y = date.fromisoformat(a), date.fromisoformat(b)
    tail = f"{y.day} {y:%b %Y}"
    if x == y:
        return tail
    if x.year != y.year:
        return f"{x.day} {x:%b %Y} – {tail}"
    return f"{x.day}{'' if x.month == y.month else f' {x:%b}'} – {tail}"


def _in_sentence(cadence: str, title: str) -> str:
    """The title as it reads mid-sentence: 'week of 2 Nov 2026'."""
    return title[0].lower() + title[1:] if cadence == "week" else title


def _money(v, currency: Optional[str]) -> str:
    if v is None:
        return "—"
    text = f"{v:,.0f}"
    return text if currency in (None, "none", "review") else f"{currency} {text}"


def _vs(cadence: str) -> str:
    return {"day": "vs the day before", "week": "vs last week",
            "custom": "vs the period before"}.get(cadence, "vs last month")


def headline(pack: dict) -> list[tuple[str, str]]:
    """The headline numbers, each with its change — the email's facts and the
    top of both files. Earned premium only on packs that carry it (a report
    sent before it was added is re-made exactly as it went out)."""
    m, o, t, p = pack["money"], pack["operations"], pack["timeliness"], pack.get("problems")
    vs = _vs(pack["cadence"])

    def ch(now, then, kind, better):
        text, _tone = rd._delta(now, then, kind, better)
        return "" if text == "—" else f" ({text} {vs})"

    daily = pack["cadence"] == "day"
    # Premium shorter than a month is the month so far, against the month before.
    money_vs = "vs the day before" if daily else "vs last month"

    def money_line(fig):
        if not fig or fig["current"] is None:
            return "Not reported"
        changes = []
        for then, against in ((fig["previous"], money_vs), (fig.get("last_year"), "vs last year")):
            text, _tone = rd._delta(fig["current"], then, "share", "none")
            if text != "—":
                changes.append(f"{text} {against}")
        return _money(fig["current"], m["currency"]) + (f" ({' · '.join(changes)})" if changes else "")
    so_far = " so far" if pack["cadence"] not in ("month", "day") else ""
    earned = [(f"Earned premium{so_far}", money_line(m["earned_premium"]))] if "earned_premium" in m else []
    out = [
        (f"Gross premium{so_far}", money_line(m.get("gross_premium"))),
        (f"Commission{so_far}", money_line(m.get("commission"))),
        *earned,
        ("Files processed", rd._num(o["files"]) + ch(o["files"], o["previous"]["files"], "share", "none")),
        ("On time", rd._pct(t["on_time_pct"])
         + ch(t["on_time_pct"], t["previous_on_time_pct"], "points", "up")),
        ("Clean first time", rd._pct(o["clean_first_time_pct"])
         + ch(o["clean_first_time_pct"], o["previous"]["clean_first_time_pct"], "points", "up")),
    ]
    if p is None:          # one programme's report (programme_report) has no problems section
        return out
    return out + [
        ("New problems", f"{rd._num(p['open'])} ({rd._num(p.get('fixed', 0))} fixed)") if daily
        else ("Open problems", f"{rd._num(p['open'])} ({rd._num(p['overdue'])} overdue)"),
        ("New escalations" if daily else "Escalations", rd._num(p["escalations"])),
    ]


# ── the files ───────────────────────────────────────────────────────────────
# ── laying a table out so nothing overlaps ─────────────────────────────────
# Tables whose columns keep set widths, as on screen, however long the text:
# heading → (PDF share of the page width per column, Excel width per column).
SET_WIDTHS = {
    "Premium cap used": ((.28, .22, .16, .16, .08, .10), (40, 30, 18, 18, 8, 10)),
}


def _set_widths(heading: str):
    return SET_WIDTHS.get(heading.split(" — ")[0])


SHORT_COLUMN = 90                     # points: a column this narrow stays on one line
_NUMBER = re.compile(r"^(—|-|[A-Z]{3} )?-?[\d,]+(\.\d+)?%?$|^—$|^\d+ days?$")


def _message_row(row: list) -> bool:
    """'No files were late' and the like: one message, the other cells empty."""
    return len(row) > 1 and bool(str(row[0]).strip()) and all(str(v) == "" for v in row[1:])


def _numeric_cols(rows: list[list]) -> set[int]:
    """The columns holding only numbers, amounts, percentages or '—' (never
    the first, which names the row): they are right-aligned."""
    body = [r for r in rows[1:] if not _message_row(r)]
    out = set()
    for c in range(1, len(rows[0])):
        vals = [str(r[c]).strip() for r in body if c < len(r) and str(r[c]).strip()]
        if vals and all(_NUMBER.match(v) for v in vals):
            out.add(c)
    return out


def _col_widths(rows: list[list], nums: set[int], total: float, pad: float) -> list[float]:
    """Column widths that fit `total` points: each number column as wide as
    its widest entry; the text columns share the rest by how much they hold,
    none narrower than its longest word, so wrapping never cuts a word."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    n = len(rows[0])
    body = [r for r in rows[1:] if not _message_row(r)]

    def w(text, bold=False):
        return stringWidth(str(text), "Helvetica-Bold" if bold else "Helvetica", 8 if bold else 8.5)
    natural, least = [], []
    for c in range(n):
        cells = [str(r[c]) for r in body if c < len(r)]
        words = [x for v in cells + [str(rows[0][c])] for x in v.split()] or [""]
        # +4: a paragraph wraps a hair before its exact width.
        natural.append(max([w(rows[0][c], True)] + [w(v) for v in cells]) + pad + 4)
        least.append(max([w(x) for x in words] + [w(x, True) for x in str(rows[0][c]).split()]) + pad + 4)
        if natural[-1] <= SHORT_COLUMN:      # 'Over cap', 'September 2026': never split
            least[-1] = natural[-1]
    fixed = {c: natural[c] for c in nums}
    text = [c for c in range(n) if c not in nums]
    room = total - sum(fixed.values())
    if not text:
        return [natural[c] * total / sum(natural) for c in range(n)]
    want = sum(natural[c] for c in text)
    if want <= room:
        # Everything fits on one line: the spare room goes to the text columns.
        out = {c: natural[c] + (room - want) * natural[c] / want for c in text}
    else:
        out = {c: max(least[c], room * natural[c] / want) for c in text}
        over = sum(out.values()) - room
        if over > 0:                     # the minimums took too much: take it back from the widest
            for c in sorted(text, key=lambda c: -out[c]):
                give = min(over, out[c] - least[c])
                out[c] -= give
                over -= give
            if over > 0:                 # still too wide: shrink the number columns to fit
                scale = (total - sum(out.values())) / sum(fixed.values())
                fixed = {c: v * scale for c, v in fixed.items()}
    out.update(fixed)
    return [out[c] for c in range(n)]


def render_pdf(pack: dict, carrier: str) -> bytes:
    """The report as an A4 portrait PDF — what the screen shows, on paper."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    title = f"Executive Report — {period_title(pack['cadence'], pack['period'])}"
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm, title=title, author="Kavachio")
    ss = getSampleStyleSheet()
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontName="Helvetica", fontSize=9.5,
                         textColor=colors.HexColor("#566071"))
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=18,
                        alignment=0, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=12,
                        spaceBefore=10, spaceAfter=4)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontName="Helvetica", fontSize=8.5, leading=10.5)
    teal, line = colors.HexColor("#077282"), colors.HexColor("#E5E8EE")

    def para(text, style=cell):
        return Paragraph(escape(str(text)), style)

    head = ParagraphStyle("head", parent=cell, fontName="Helvetica-Bold", fontSize=8, leading=10,
                          textColor=colors.HexColor("#566071"))
    right = {s: ParagraphStyle(s.name + "_r", parent=s, alignment=2) for s in (cell, head)}
    pad = 12                                          # left + right padding of a cell

    def table(rows, widths=None):
        """Every cell a paragraph, so long text wraps inside its own column;
        number columns right-aligned; a row with one message spans the table."""
        nums = _numeric_cols(rows)
        widths = widths or _col_widths(rows, nums, doc.width, pad)
        body = []
        spans = []
        for r, row in enumerate(rows):
            if r and _message_row(row):
                spans.append(("SPAN", (0, r), (-1, r)))
                body.append([para(row[0])] + [""] * (len(row) - 1))
                continue
            st = head if r == 0 else cell
            body.append([para(v, right[st] if c in nums else st) for c, v in enumerate(row)])
        t = Table(body, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F7F8FB")),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, line),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), pad / 2), ("RIGHTPADDING", (0, 0), (-1, -1), pad / 2),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            *spans,
        ]))
        return t

    story = [
        para("Kavachio", ParagraphStyle("brand", parent=sub, textColor=teal, fontName="Helvetica-Bold")),
        para(title, h1),
        para(f"{carrier} · {_range(pack)} · numbers as at "
             f"{date.fromisoformat(pack['as_of']):%d %B %Y}"
             + ("" if pack["complete"] else " · still running"), sub),
        para(scope_note(pack), sub),
        Spacer(1, 6),
        table([["Headline", "This period"]] + [list(x) for x in headline(pack)],
              [doc.width * .35, doc.width * .65]),
    ]
    for heading, rows in sections(pack):
        # A heading never sits alone at the foot of a page: with less than
        # room for it and a few rows, the section starts on the next page.
        story.append(CondPageBreak(40 * mm))
        story.append(para(heading, h2))
        fixed = _set_widths(heading)
        story.append(table(rows, [doc.width * w for w in fixed[0]] if fixed else None))

    # 45.2 — the Column Guide, read from the tables above: only the columns
    # this PDF really has, each in plain English.
    import export_glossary as gl
    gl_rows = gl.pdf_rows(story)
    story.append(PageBreak())          # the guide always starts on a fresh page
    story.append(para(gl.TITLE, h2))
    story.append(para(gl.INTRO, sub))
    story.append(Spacer(1, 4))
    story.append(table(gl_rows, [doc.width * w for w in (.24, .52, .24)]))

    def _footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#8B93A2"))
        canvas.drawString(d.leftMargin, 8 * mm, f"Kavachio · {title}")
        canvas.drawRightString(d.leftMargin + d.width, 8 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def scope_note(pack: dict) -> str:
    """Said once at the top of the files: the day the position sections are
    worked out for, and (shorter than a month) the month premium is read for."""
    day = date.fromisoformat(pack["as_at"])
    if pack["cadence"] == "day":
        return (f"Only what happened on {day.day} {day:%b %Y}: premium in the bordereaux "
                f"processed that day, files due that day, problems that started or became "
                f"escalations that day, and programmes added, gone live or into run-off that day.")
    note = (f"Open problems, escalations, programmes and reinsurance statements are as they "
            f"stood at the end of {day.day} {day:%b %Y}.")
    if pack["cadence"] != "month":
        note += (f" Premium and on time are for {rd.fmt_month(pack['money']['month'])} so far.")
    return note


def _range(pack: dict) -> str:
    a, b = date.fromisoformat(pack["from"]), date.fromisoformat(pack["to"])
    return f"{a.day} {a:%b} – {b.day} {b:%b %Y}"


TREND_TITLES = {"hour": ("Errors by hour", "Hour"), "day": ("Errors by day", "Day"),
                "week": ("Errors by week", "Week"), "month": ("Errors by month", "Month")}


def _trend_label(t: dict) -> tuple[str, str]:
    """(unit, label) of one point. Reports sent before the chart followed the
    period kept 'month' or 'day' and no unit, and still read the same."""
    if t.get("unit"):
        return t["unit"], t["label"]
    if t.get("day"):
        return "day", _span(t["day"], t["day"])
    return "month", rd.fmt_month(t["month"])


def trend_section(trend: list[dict]) -> tuple[str, list[list]]:
    """The errors table, cut the way the period was (executive_report.trend_buckets)."""
    unit = _trend_label(trend[0])[0] if trend else "day"
    title, col = TREND_TITLES[unit]
    return (title, [[col, "Files", "Errors", "Clean first time"]]
            + [[_trend_label(t)[1], rd._num(t["files"]), rd._num(t["errors"]),
                rd._pct(t["clean_first_time_pct"])] for t in trend])


HISTORY_TITLES = {"day": ("Error history — last 14 days", "Day"),
                  "week": ("Error history — last 12 weeks", "Week of"),
                  "month": ("Error history — last 12 months", "Month")}


def health_sections(pack: dict) -> list[tuple[str, list[list]]]:
    """37 — errors by programme (split by severity) and 37.2's error history.
    Absent from reports sent before they existed."""
    out = []
    ebp = pack.get("errors_by_programme")
    if ebp is not None:
        more = f" — {len(ebp['rows'])} of {ebp['total']}" if ebp["total"] > len(ebp["rows"]) else ""
        out.append((f"Errors by programme{more}", [["Programme", "Errors", "Critical", "Warning", "Info"]]
                    + ([[x["name"], rd._num(x["errors"]), rd._num(x["critical"]),
                         rd._num(x["warning"]), rd._num(x["info"])] for x in ebp["rows"]]
                       or [["No errors found", "", "", "", ""]])))
    hist = pack.get("history")
    if hist:
        title, col = HISTORY_TITLES[hist[0]["unit"]]
        out.append((title, [[col, "Files", "Errors", "Clean first time"]]
                    + [[t["label"], rd._num(t["files"]), rd._num(t["errors"]),
                        rd._pct(t["clean_first_time_pct"])] for t in hist]))
    return out


def _month_of(key: Optional[str]) -> str:
    """'2026-10' as 'October 2026'; a quarter or week key as it is."""
    return rd.fmt_month(key) if key and re.fullmatch(r"\d{4}-\d{2}", key) else (key or "—")


def _moved(now, then) -> str:
    """' (down from 80%)' when a share got worse than the period before; the
    files have no colour to show it with. Nothing when it held or improved."""
    if now is None or then is None or now >= then - 0.05:
        return ""
    return f" (down from {rd._pct(then)})"


def _pct_or_dash(v) -> str:
    return "—" if v is None else rd._pct(v)


def status_section(pack: dict) -> Optional[tuple[str, list[list]]]:
    """Files by status: each file as it first arrived. None on a report sent
    before the section existed."""
    q = pack.get("quality")
    if not q:
        return None
    return ("Files by status", [["Status", "Files"], ["Clean", rd._num(q["clean"])],
                                ["With errors", rd._num(q["with_errors"])],
                                ["Not checked", rd._num(q["not_checked"])]])


def more_sections(pack: dict) -> list[tuple[str, list[list]]]:
    """Most common errors, data quality by broker and by programme, late
    submissions and contract utilization — absent from reports sent before
    they existed, which keep reading as they were sent."""
    q, lt, ct = pack.get("quality"), pack.get("late"), pack.get("contracts")
    daily = pack["cadence"] == "day"
    out = []
    if q:
        out.append(("Most common errors", [["Check", "Files", "Errors", "Severity"]]
                    + ([[x["check"], rd._num(x["files"]), rd._num(x["errors"]),
                         (x["severity"] or "—").capitalize()] for x in q["top_errors"]]
                       or [["No errors found", "", "", ""]])))
        # Reports sent before the scorecard kept quality by broker and by programme.
        for kind in ("broker", "programme") if "scorecard" not in pack else ():
            out.append((f"Data quality by {kind}", [[kind.capitalize(), "Files", "Errors", "Clean first time"]]
                        + ([[x["name"], rd._num(x["files"]), rd._num(x["errors"]),
                             _pct_or_dash(x["clean_first_time_pct"])] for x in q.get(f"by_{kind}", [])]
                           or [["No files processed", "", "", ""]])))
    sc = pack.get("scorecard")
    if sc:
        cur = pack["money"].get("currency")
        for kind in ("broker", "programme"):
            part = sc[kind]
            shown = (f" — {len(part['rows'])} of {part['total']}"
                     if part["total"] > len(part["rows"]) else "")
            out.append((f"{kind.capitalize()} scorecard{shown}",
                        [[kind.capitalize(), "Premium", "Files", "On time", "Clean first time", "Late or missing"]]
                        + ([[x["name"], _money(x["premium"], cur), rd._num(x["files"]),
                             _pct_or_dash(x["on_time_pct"]) + _moved(x["on_time_pct"], x["prev_on_time_pct"]),
                             _pct_or_dash(x["clean_first_time_pct"])
                             + _moved(x["clean_first_time_pct"], x["prev_clean_first_time_pct"]),
                             rd._num(x["late_or_missing"])] for x in part["rows"]]
                           or [[f"No {kind} had files, premium or files due", "", "", "", "", ""]])))
    if lt:
        lt = {**lt, "list": lt["list"][:er.LATE_SHOWN]}
        line = " · ".join(x for x in (f"{lt['late']} arrived late" if lt["late"] else "",
                                      f"{lt['missing']} still missing" if lt["missing"] else "",
                                      f"{lt['never']} never arrived" if lt["never"] else "") if x) \
            or "none"
        out.append((f"Late submissions — {line}",
                    [["Broker", "Programme", "Month", "Due", "Arrived", "Days late"]]
                    + ([[x["broker"], x["programme"], _month_of(x["month"]), x["due_on"],
                         x["arrived_on"] or x["status"], rd._num(x["days_late"])] for x in lt["list"]]
                       or [["No files were late", "", "", "", "", ""]])))
    if ct:
        out.append((f"Contracts sending files — {ct['with_files']} of {ct['bound']}",
                    [["Contract with no files " + ("that day" if daily else "in this period"), "Broker", "Programme"]]
                    + ([[x["contract"], x["broker"], x["programme"]] for x in ct["idle"]]
                       or [["Every bound contract sent files", "", ""]])))
        out.append(("Premium cap used",
                    [["Contract", "Broker", "Premium cap", "Written so far", "Used", ""]]
                    + ([[x["contract"], x["broker"], _money(x["cap"], x["currency"]),
                         _money(x["written"], x["currency"]), _pct_or_dash(x["used_pct"]), x["flag"] or ""]
                        for x in ct["caps"]]
                       or [["No contract " + ("that sent files that day " if daily else "")
                            + "has a premium cap set", "", "", "", "", ""]])
                    + ([[f"{ct['no_cap']} contract{' has' if ct['no_cap'] == 1 else 's have'} no premium cap set",
                         "", "", "", "", ""]] if ct["no_cap"] else [])))
    return out


def sections(pack: dict) -> list[tuple[str, list[list]]]:
    """Every section below the headline as (heading, [header, *rows]) — the
    PDF's tables and the Excel sheets read the same rows."""
    m, pf, p, re_ = pack["money"], pack["portfolio"], pack["problems"], pack["reinsurance"]
    cur = m["currency"]
    out = [x for x in (status_section(pack),) if x]
    daily = pack["cadence"] == "day"
    so_far = (" (processed that day)" if daily
              else " (month so far)" if pack["cadence"] != "month" else "")
    out.append((f"Top programmes by premium{so_far}",
                [["Programme", "Gross premium"]]
                + ([[x["name"], _money(x["gross_premium"], cur)] for x in m["top_programmes"]]
                   or [["No premium reported", ""]])))
    out.append((f"Top brokers by premium{so_far}",
                [["Broker", "Gross premium"]]
                + ([[x["name"], _money(x["gross_premium"], cur)] for x in m["top_brokers"]]
                   or [["No premium reported", ""]])))
    if daily:
        out.append(("Programmes", [["Programmes that day", "Count"], ["New", pf["new"]],
                                   ["Went live", pf["went_live"]],
                                   ["Went into run-off", pf["into_runoff"]],
                                   ["Programmes in all", pf["total"]]]))
        out.append(trend_section(pack["trend"]))
        out.extend(health_sections(pack))
        out.extend(more_sections(pack))
        out.append((f"Needs attention — {p['escalations']} new escalation{'' if p['escalations'] == 1 else 's'}",
                    [["Problem", "Who must act", "Escalated because", "Now"]]
                    + ([[f"{x['type_label']}: {x['title']}", x["owner"] or "—",
                         "Disputed" if x["type"] == "dispute"
                         else "Delinquent" if x["status"] in ("Delinquent",) else f"Past its due date ({x['due_on']})",
                         "Still open" if x["still_open"] else x["status"]] for x in p["escalation_list"][:er.ESCALATIONS_SHOWN]]
                       or [["Nothing became an escalation that day", "", "", ""]])))
    else:
        out.append(("Programmes", [["Programmes", "Count"], ["Active", pf["active"]],
                                   ["Run-off", pf["runoff"]], ["Not live yet", pf["not_live"]],
                                   ["Total", pf["total"]]]))
        out.append(trend_section(pack["trend"]))
        out.extend(health_sections(pack))
        out.extend(more_sections(pack))
        out.append((f"Needs attention — {p['escalations']} escalation{'' if p['escalations'] == 1 else 's'}",
                    [["Problem", "Who must act", "Open for", "Due"]]
                    + ([[f"{x['type_label']}: {x['title']}", x["owner"] or "—",
                         rd._num(x["age_days"]) + " days" if x["age_days"] is not None else "—",
                         x["due_on"] or "—"] for x in p["escalation_list"][:er.ESCALATIONS_SHOWN]]
                       or [["Nothing needs escalating", "", "", ""]])))
    out.append((f"Reinsurance statements due — {reinsurance_line(re_)}",
                [["Treaty", "Period", "Due", "Status"]]
                + ([[x["treaty"], x["period"], x["due_on"] or "—",
                     x["status"] + (f" ({x['sent_on']})" if x.get("sent_on") else "")]
                    for x in re_["statements"][:25]]
                   or [["No statements were due", "", "", ""]])))
    return out


def render_xlsx(pack: dict, carrier: str) -> bytes:
    """The report as an Excel workbook: the headline, then one sheet per section."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    bold, grey = Font(bold=True), PatternFill("solid", fgColor="F3F4F7")
    wrap = Alignment(wrap_text=True, vertical="top")
    wrap_right = Alignment(wrap_text=True, vertical="top", horizontal="right")
    wb = Workbook()
    ws = wb.active
    ws.title = "Headline"
    ws.cell(row=1, column=1, value=f"Executive Report — {period_title(pack['cadence'], pack['period'])}"
            ).font = Font(bold=True, size=14)
    ws.cell(row=2, column=1, value=f"{carrier} · {_range(pack)} · numbers as at {pack['as_of']}")
    ws.cell(row=3, column=1, value=scope_note(pack))
    for r, (k, v) in enumerate(headline(pack), 5):
        ws.cell(row=r, column=1, value=k).font = bold
        ws.cell(row=r, column=2, value=v).alignment = wrap
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 60
    used = set()
    for heading, rows in sections(pack):
        name = re.sub(r"[\[\]:*?/\\]", "", heading.split(" — ")[0])[:31]
        while name in used:
            name = name[:29] + "_2"
        used.add(name)
        sh = wb.create_sheet(name)
        nums = _numeric_cols(rows)
        for r, row in enumerate(rows, 1):
            for c, v in enumerate(row, 1):
                x = sh.cell(row=r, column=c, value=v)
                # Long text wraps inside its own column instead of running into the next.
                x.alignment = wrap_right if (c - 1) in nums else wrap
                if r == 1:
                    x.font, x.fill = bold, grey
        fixed = _set_widths(heading)
        if fixed:
            for c, wd in enumerate(fixed[1]):
                sh.column_dimensions[get_column_letter(c + 1)].width = wd
        # Each column as wide as what it holds (in characters), within limits.
        for c in (range(len(rows[0])) if not fixed else ()):
            longest = max(len(str(row[c])) for row in rows
                          if c < len(row) and (row is rows[0] or not _message_row(row)))
            sh.column_dimensions[get_column_letter(c + 1)].width = min(max(longest + 2, 10),
                                                                        60 if c == 0 else 45)
        sh.freeze_panes = "A2"
    # 45.2 — what every word in this file means, in plain English.
    import export_glossary as gl
    gl.xlsx_sheet(wb)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


_FORMAT_WORDS = {"pdf": "a PDF", "xlsx": "an Excel file"}


def attached_line(attachments: list[tuple[str, bytes, str]]) -> str:
    """'The full report is attached as a PDF: Executive Report 2026-10.pdf.'"""
    parts = [f"{_FORMAT_WORDS.get(n.rsplit('.', 1)[-1], 'a file')}: {n}" for n, _d, _m in attachments]
    if not parts:
        return ""
    return "The full report is attached as " + " and ".join(parts) + "."


def sections_html(pack: dict) -> str:
    """The report below the headline, for the email body: what the period
    counts, then every section as a small table — tables and inline styles
    only, as the email template is (Outlook strips the rest)."""
    from html import escape
    cell = ("padding:7px 10px;font-size:12.5px;color:#0E1320;border-top:1px solid #EDEFF4;"
            "vertical-align:top;word-break:break-word")
    head = ("padding:7px 10px;font-size:11px;color:#8B93A2;font-weight:600;letter-spacing:.3px;"
            "text-transform:uppercase;background:#F7F8FB")
    out = [f'<p style="font-size:12.5px;color:#566071;line-height:1.55;margin:18px 0 0">'
           f'{escape(scope_note(pack))}</p>']
    for heading, rows in sections(pack):
        nums = _numeric_cols(rows)
        side = lambda c: "text-align:right;white-space:nowrap" if c in nums else "text-align:left"  # noqa: E731
        th = "".join(f'<th style="{head};{side(c)}">{escape(str(h))}</th>' for c, h in enumerate(rows[0]))
        trs = "".join(
            "<tr>" + (f'<td colspan="{len(r)}" style="{cell}">{escape(str(r[0]))}</td>' if _message_row(r)
                      else "".join(f'<td style="{cell};{side(c)}">{escape(str(v))}</td>'
                                   for c, v in enumerate(r))) + "</tr>"
            for r in rows[1:])
        out.append(
            f'<div style="font-size:14px;font-weight:700;color:#0E1320;margin:22px 0 8px">{escape(heading)}</div>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="width:100%;border-collapse:collapse;border:1px solid #E5E8EE;border-radius:8px">'
            f'<tr>{th}</tr>{trs}</table>')
    return "".join(out)


def files(pack: dict, file_format: str, carrier: str) -> list[tuple[str, bytes, str]]:
    """(filename, data, mime) for each file the format asks for."""
    name = f"Executive Report {pack['period'].replace('..', ' to ')}"
    out = []
    if file_format in ("pdf", "both"):
        out.append((f"{name}.pdf", render_pdf(pack, carrier), rd.PDF_MIME))
    if file_format in ("excel", "both"):
        out.append((f"{name}.xlsx", render_xlsx(pack, carrier), rd.XLSX_MIME))
    return out


# ── who ─────────────────────────────────────────────────────────────────────
def clean_emails(raw) -> list[str]:
    """Typed addresses, one per line or separated by commas: trimmed, each once
    (ignoring case), in the order typed. ValueError names any that are not an
    email address."""
    if isinstance(raw, str):
        raw = re.split(r"[\s,;]+", raw)
    out, seen, bad = [], set(), []
    for e in raw or []:
        e = str(e).strip()
        if not e:
            continue
        if not _EMAIL.match(e):
            bad.append(e)
        elif e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    if bad:
        raise ValueError("These are not email addresses: " + ", ".join(bad[:5]))
    if len(out) > MAX_EMAILS:
        raise ValueError(f"Add at most {MAX_EMAILS} other email addresses.")
    return out


def recipients(s, tenant_id: Optional[int], user_ids: Optional[list], emails: Optional[list],
               logins: Optional[list[dict]] = None) -> list[dict]:
    """The people a report goes to: the ticked logins (None = every login) and
    the other addresses, each address once. `logins` is whose logins may be
    ticked — the carrier's (report_delivery.people) unless the caller says
    otherwise; the Policies list (policy_export) passes a broker's own."""
    if logins is None:
        logins = rd.people(s, tenant_id)
    if user_ids is not None:
        want = {int(i) for i in user_ids}
        logins = [p for p in logins if p["user_id"] in want]
    out, seen = [], set()
    for p in logins:
        if p["email"].lower() not in seen:
            seen.add(p["email"].lower())
            out.append({"user_id": p["user_id"], "email": p["email"], "name": p["name"]})
    for e in clean_emails(emails or []):
        if e.lower() not in seen:
            seen.add(e.lower())
            out.append({"user_id": None, "email": e, "name": None})
    return out


# ── the schedule ────────────────────────────────────────────────────────────
def settings(s, tenant_id: int) -> dict:
    from db import ExecutiveReportSetting
    row = (s.query(ExecutiveReportSetting)
           .filter(ExecutiveReportSetting.tenant_id == tenant_id).first())
    if row is None:
        return {**DEFAULTS, "saved": False}
    return {"auto_send": bool(row.auto_send), "cadence": row.cadence, "week_day": row.week_day,
            "send_day": row.send_day, "file_format": row.file_format,
            "user_ids": row.user_ids, "emails": row.emails or [], "saved": True}


def reinsurance_line(re_: dict) -> str:
    """'3 due · 1 sent late · 1 not sent' — the parts that are not zero."""
    parts = [f"{re_['due']} due"]
    if re_["sent_late"]:
        parts.append(f"{re_['sent_late']} sent late")
    if re_["not_sent"]:
        parts.append(f"{re_['not_sent']} not sent")
    return " · ".join(parts)


def save_settings(s, tenant_id: int, body: dict, user_id: Optional[int]) -> tuple[dict, dict]:
    """Check and save the schedule. Returns (before, after). Caller commits."""
    from db import ExecutiveReportSetting
    before = settings(s, tenant_id)
    cadence = str(body.get("cadence", before["cadence"]))
    if cadence not in SCHEDULE_CADENCES:
        raise ValueError("Choose daily, weekly or monthly.")
    week_day = int(body.get("week_day", before["week_day"]))
    if week_day not in WEEK_DAYS:
        raise ValueError("Pick a weekday from Monday to Friday.")
    send_day = int(body.get("send_day", before["send_day"]))
    if send_day not in SEND_DAYS:
        raise ValueError("Pick one of the working days offered.")
    file_format = str(body.get("file_format", before["file_format"]))
    if file_format not in FORMATS:
        raise ValueError("The file must be PDF, Excel or both.")
    user_ids = body.get("user_ids", before["user_ids"])
    user_ids = None if user_ids is None else sorted({int(i) for i in user_ids})
    emails = clean_emails(body.get("emails", before["emails"]))
    auto = bool(body.get("auto_send", before["auto_send"]))
    if auto and not recipients(s, tenant_id, user_ids, emails):
        raise ValueError(NO_ONE)
    row = (s.query(ExecutiveReportSetting)
           .filter(ExecutiveReportSetting.tenant_id == tenant_id).first())
    if row is None:
        row = ExecutiveReportSetting(tenant_id=tenant_id)
        s.add(row)
    row.auto_send, row.cadence, row.week_day, row.send_day = auto, cadence, week_day, send_day
    row.file_format, row.user_ids, row.emails = file_format, user_ids, emails
    row.updated_by_user_id, row.updated_at = user_id, datetime.utcnow()
    s.flush()
    return before, settings(s, tenant_id)


def due_now(cfg: dict, today: date) -> Optional[tuple[str, str]]:
    """(cadence, period) of the automatic send due today, or None.

    daily    yesterday's report, every day.
    weekly   last week's report, from its weekday until the week ends.
    monthly  last month's, from its working day until CATCH_UP_DAYS after."""
    if cfg["cadence"] == "day":
        return "day", (today - timedelta(days=1)).isoformat()
    if cfg["cadence"] == "week":
        if today.weekday() < cfg["week_day"]:
            return None
        monday = today - timedelta(days=today.weekday())
        return "week", (monday - timedelta(days=7)).isoformat()
    month = rd.due_now(cfg["send_day"], today)
    return ("month", month) if month else None


def next_send(cfg: dict, today: date, sent: set[str]) -> dict:
    """When the next automatic send happens and what it reports on."""
    if cfg["cadence"] == "day":
        y = (today - timedelta(days=1)).isoformat()
        on, period = (today, y) if f"day:{y}" not in sent else (today + timedelta(days=1), today.isoformat())
        return {"send_on": on.isoformat(), "period": period, "cadence": "day",
                "title": period_title("day", period)}
    if cfg["cadence"] == "week":
        monday = today - timedelta(days=today.weekday())
        last = (monday - timedelta(days=7)).isoformat()
        if f"week:{last}" not in sent:
            on = max(today, monday + timedelta(days=cfg["week_day"]))
            return {"send_on": on.isoformat(), "period": last, "cadence": "week",
                    "title": period_title("week", last)}
        nxt = monday + timedelta(days=7 + cfg["week_day"])
        return {"send_on": nxt.isoformat(), "period": monday.isoformat(), "cadence": "week",
                "title": period_title("week", monday.isoformat())}
    months = {k.split(":", 1)[1] for k in sent if k.startswith("month:")}
    n = rd.next_send(cfg["send_day"], today, months)
    return {"send_on": n["send_on"], "period": n["month"], "cadence": "month",
            "title": period_title("month", n["month"])}


# ── sending ─────────────────────────────────────────────────────────────────
def _status(delivered: int, failed: int) -> str:
    if delivered and not failed:
        return "sent"
    return "partly" if delivered else "failed"


def _person(s, user_id: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    """(name, email) of a login, or (None, None)."""
    if not user_id:
        return None, None
    from db import AppUser
    u = s.get(AppUser, user_id)
    if u is None:
        return None, None
    email = (u.email or "").strip() or None
    return ((u.full_name or "").strip() or email), email


def _sender(s, user_id: Optional[int]) -> Optional[str]:
    return _person(s, user_id)[0]


def reply_to_for(s, tenant_id: int, user_id: Optional[int]) -> Optional[str]:
    """Where a reply goes: the carrier person who sent it — or, for an
    automatic send, the one who last saved the schedule."""
    if not user_id:
        from db import ExecutiveReportSetting
        row = (s.query(ExecutiveReportSetting)
               .filter(ExecutiveReportSetting.tenant_id == tenant_id).first())
        user_id = row.updated_by_user_id if row else None
    return _person(s, user_id)[1]


def send(s, tenant_id: int, *, cadence: str, period: Optional[str], kind: str,
         user_id: Optional[int] = None, user_ids: Optional[list] = None,
         emails: Optional[list] = None, file_format: Optional[str] = None,
         now: Optional[datetime] = None,
         mailer: Optional[Callable[..., None]] = None) -> Optional[dict]:
    """Build, email and record one report. `kind`: scheduled | now.

    A scheduled send uses the saved schedule's recipients and claims its
    period first, returning None when it was already sent. Commits."""
    from sqlalchemy.exc import IntegrityError
    from db import ExecutiveReportSend
    from notifications import NOTIFY_MAIL_ACCOUNT, _app_link, _email_text, notification_email_html
    if mailer is None:
        from email_utils import send_email as mailer
    now = now or datetime.utcnow()
    cfg = settings(s, tenant_id)
    if kind == "scheduled":
        user_ids, emails, file_format = cfg["user_ids"], cfg["emails"], cfg["file_format"]
    file_format = file_format or cfg["file_format"]
    if file_format not in FORMATS:
        raise ValueError("The file must be PDF, Excel or both.")
    to = recipients(s, tenant_id, user_ids, emails)
    if not to:
        raise ValueError(NO_ONE)
    w = er.window(cadence, period, now.date())

    row = ExecutiveReportSend(
        tenant_id=tenant_id, cadence=cadence, period=w["period"], kind=kind,
        scheduled_key=f"{tenant_id}:{cadence}:{w['period']}" if kind == "scheduled" else None,
        file_format=file_format, sent_by_user_id=user_id, created_at=now)
    s.add(row)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        return None      # this period's automatic send has already gone

    carrier = rd.carrier_name(s, tenant_id)
    try:
        pack = er.pack(s, tenant_id, cadence=cadence, period=w["period"], today=now.date())
        attachments = files(pack, file_format, carrier)
    except Exception:
        # Nothing has gone out: drop the claim so the next run can try again.
        s.rollback()
        s.delete(s.merge(row))
        s.commit()
        raise
    when = period_title(cadence, w["period"])
    title = f"Executive Report — {when}"
    subject = rd.SUBJECT_PREFIX + f"Executive Report, {_in_sentence(cadence, when)}"
    body = (f"{carrier}'s Executive Report for {_in_sentence(cadence, when)}. "
            + attached_line(attachments)
            + " The numbers are also below, so you can read it without signing in.")
    sender = _sender(s, user_id)
    # From the carrier, not from "Kavachio": its name on the email, and a reply
    # goes to its own person. The address stays Kavachio's authorised mailbox,
    # which is what lets the email arrive at all (no carrier mail password).
    from_name = f"{carrier} via Kavachio" if carrier else None
    reply_to = reply_to_for(s, tenant_id, user_id)
    footer = (f"Sent by {sender} from {carrier} on Kavachio." if sender
              else f"Sent automatically from {carrier} on Kavachio.")
    facts = headline(pack)
    # The carrier's own logins can open the report in Kavachio; nobody else can.
    link = _app_link(f"/executive-report?cadence={cadence}&period={w['period']}")
    tables = sections_html(pack)
    note = scope_note(pack)

    results = []
    for p in to:
        # A company login gets "Open in Kavachio"; an outside address does not
        # (it has no login) — it reads the report in the email and attachment.
        their_link = link if p.get("user_id") else None
        try:
            mailer(p["email"], subject,
                   notification_email_html(title, body, facts, their_link, "Open in Kavachio",
                                           greeting_name=p.get("name"), footer=footer,
                                           extra_html=tables, badge="Report"),
                   text=_email_text(title, f"{body} {note}", facts, their_link),
                   account=NOTIFY_MAIL_ACCOUNT, attachments=attachments,
                   sender_name=from_name, reply_to=reply_to)
            results.append({**p, "delivered": True, "error": None})
        except Exception as e:  # noqa: BLE001 — one bad address must not stop the rest
            log.warning("executive report %s to %s failed: %s", w["period"], p["email"], e)
            results.append({**p, "delivered": False, "error": str(e)[:300]})
    row.recipients = results
    row.delivered = sum(1 for r in results if r["delivered"])
    row.failed = len(results) - row.delivered
    row.pack, row.options = pack, {"carrier": carrier}
    s.commit()
    return {"id": row.id, "cadence": cadence, "period": w["period"], "title": when,
            "kind": kind, "delivered": row.delivered, "failed": row.failed,
            "status": _status(row.delivered, row.failed),
            "recipients": [{k: r[k] for k in ("name", "email", "delivered")} for r in results]}


def history(s, tenant_id: int, limit: int = 30) -> list[dict]:
    """The reports sent, newest first, with who they went to — leaving out the
    ones cleared from the list (`hidden_at`)."""
    from db import AppUser, ExecutiveReportSend
    rows = (s.query(ExecutiveReportSend).filter(ExecutiveReportSend.tenant_id == tenant_id,
                                                ExecutiveReportSend.hidden_at.is_(None))
            .order_by(ExecutiveReportSend.created_at.desc(), ExecutiveReportSend.id.desc())
            .limit(limit).all())
    senders = {u.id: ((u.full_name or "").strip() or u.email) for u in s.query(AppUser).filter(
        AppUser.id.in_({r.sent_by_user_id for r in rows if r.sent_by_user_id} or {0}))}
    return [{"id": r.id, "cadence": r.cadence, "period": r.period,
             "title": period_title(r.cadence, r.period), "kind": r.kind,
             "sent_at": r.created_at.isoformat() + "Z" if r.created_at else None,
             "sent_by": senders.get(r.sent_by_user_id) if r.sent_by_user_id else None,
             "file_format": r.file_format,
             "recipients": [{k: x.get(k) for k in ("name", "email", "delivered")}
                            for x in (r.recipients or [])],
             "delivered": r.delivered, "failed": r.failed,
             "status": _status(r.delivered, r.failed) if r.pack is not None else "failed",
             "has_file": r.pack is not None} for r in rows]


def clear_history(s, tenant_id: int, now: Optional[datetime] = None) -> int:
    """Hide every report on the Sent reports list. Nothing is deleted: the rows,
    who they went to and the numbers stay, the sent file still downloads, and
    sent_keys still counts them. Returns how many were hidden; the caller commits."""
    from db import ExecutiveReportSend
    return (s.query(ExecutiveReportSend)
            .filter(ExecutiveReportSend.tenant_id == tenant_id,
                    ExecutiveReportSend.hidden_at.is_(None))
            .update({ExecutiveReportSend.hidden_at: now or datetime.utcnow()},
                    synchronize_session=False))


def sent_keys(s, tenant_id: int) -> set[str]:
    """'<cadence>:<period>' of every automatic send that has gone (cleared from
    the list or not)."""
    from db import ExecutiveReportSend
    return {f"{c}:{p}" for c, p in s.query(ExecutiveReportSend.cadence, ExecutiveReportSend.period)
            .filter(ExecutiveReportSend.tenant_id == tenant_id,
                    ExecutiveReportSend.kind == "scheduled").all()}


def run_due(s, now: Optional[datetime] = None,
            mailer: Optional[Callable[..., None]] = None) -> int:
    """The daily job: send every carrier's report that is due today. Returns
    how many went out. One carrier's failure never stops the others."""
    from db import ActivityEvent, ExecutiveReportSetting
    now = now or datetime.utcnow()
    sent = 0
    for row in (s.query(ExecutiveReportSetting)
                .filter(ExecutiveReportSetting.auto_send.is_(True)).all()):
        tid = row.tenant_id
        due = due_now(settings(s, tid), now.date())
        if due is None:
            continue
        cadence, period = due
        try:
            r = send(s, tid, cadence=cadence, period=period, kind="scheduled", now=now, mailer=mailer)
        except Exception:  # noqa: BLE001
            log.exception("executive report: carrier %s, %s %s failed", tid, cadence, period)
            s.rollback()
            continue
        if r is None:
            continue
        s.add(ActivityEvent(tenant_id=tid, actor="system", action="executive_report_sent",
                            target=f"executive_report:{cadence}:{period}",
                            details={"cadence": cadence, "period": period, "kind": "scheduled",
                                     "delivered": r["delivered"], "failed": r["failed"]}))
        s.commit()
        sent += 1
    return sent
