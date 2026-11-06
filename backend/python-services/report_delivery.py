"""MOPR 42.2 — delivering the Monthly Report.

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
DEFAULTS = {"auto_send": False, "send_day": 3, "file_format": "pdf", "delivery": "attach"}
# How long after its working day a missed automatic send may still go out.
CATCH_UP_DAYS = 7
SUBJECT_PREFIX = "Kavachio - "
REPORT_FOOTER = (
    "You're receiving this as your carrier's login on Kavachio. When and how it "
    "is sent is set under Monthly Report › Send & Schedule.")
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
    return {"auto_send": bool(row.auto_send), "send_day": row.send_day,
            "file_format": row.file_format, "delivery": row.delivery, "saved": True}


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
    auto = bool(body.get("auto_send", before["auto_send"]))
    if auto and not people(s, tenant_id):
        raise ValueError(NO_ONE)

    row = (s.query(MonthlyReportSetting)
           .filter(MonthlyReportSetting.tenant_id == tenant_id).first())
    if row is None:
        row = MonthlyReportSetting(tenant_id=tenant_id)
        s.add(row)
    row.auto_send, row.send_day, row.file_format, row.delivery = auto, send_day, file_format, delivery
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


def measures(pack: dict) -> list[tuple[str, str, list[dict]]]:
    """The Key numbers, grouped — the same rows and words as the Overview tab."""
    cur = pack["months"].get(pack["month"]) or {}
    unit = " (rows)" if (cur.get("volumes") or {}).get("counted_by") == "rows" else " policies"
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
            row("Files due", lambda m: T(m)["due"], lambda m: _num(T(m)["due"]), "count", "none", "count"),
            row("Arrived on time", lambda m: T(m)["on_time_pct"],
                lambda m: _of(T(m)["on_time"], T(m)["due_so_far"], T(m)["on_time_pct"]), "points", "up", "pct"),
            row("Arrived late", lambda m: T(m)["late"], lambda m: _num(T(m)["late"]), "count", "down", "count"),
            row("Overdue", lambda m: T(m)["overdue"], lambda m: _num(T(m)["overdue"]), "count", "down", "count"),
            row("Never arrived", lambda m: T(m)["never"], lambda m: _num(T(m)["never"]), "count", "down", "count"),
            row("Average days late", lambda m: T(m)["avg_days_late"], lambda m: _took(T(m)["avg_days_late"]),
                "time", "down", "days"),
            row("Late-file cases still open", lambda m: T(m)["cases_open"], lambda m: _num(T(m)["cases_open"]),
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
            row("Waiting to be fixed at month end", lambda m: F(m)["waiting"],
                lambda m: _num(F(m)["waiting"]), "share", "down", "count"),
            row("Waiting longer than target at month end", lambda m: F(m)["waiting_past_target"],
                lambda m: _num(F(m)["waiting_past_target"]), "count", "down", "count"),
        ]),
    ]


def headline(pack: dict) -> list[tuple[str, str]]:
    """The four numbers at the top of the Overview, for the email."""
    m = pack["months"].get(pack["month"]) or {}
    if not m.get("has_data"):
        return [("This month", "Nothing was due, checked or fixed")]
    t, q, f = m["timeliness"], m["quality"], m["fixes"]
    return [
        ("Files on time", _of(t["on_time"], t["due_so_far"], t["on_time_pct"])),
        ("Files clean first time", _of(q["clean_first_time"], m["volumes"]["files"],
                                       q["clean_first_time_pct"])),
        ("Errors found", f"{_num(q['errors'])} ({_num(q['critical'])} critical)"),
        ("Average fix time", _took(f["avg_days"])),
        ("Waiting longer than target at month end", _num(f["waiting_past_target"])),
    ]


def _columns(pack: dict) -> list[tuple[str, str, Optional[str]]]:
    """(heading, month key, compared-with key): the month, last month, last year."""
    return [(fmt_month(pack["month"]), pack["month"], None),
            (fmt_month(pack["last_month"]), pack["last_month"], "last month"),
            (fmt_month(pack["last_year"]), pack["last_year"], "last year")]


# --- the files -------------------------------------------------------------------

_TONE = {"ok": "#0A6E4C", "crit": "#A31F31", "mut": "#566071"}


def render_pdf(pack: dict, carrier: str, scope: str = "") -> bytes:
    """The pack as an A4 landscape PDF — the Overview tab on paper."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    def para(text, style):
        # reportlab reads markup; a name like "Smith & Co" is text.
        return Paragraph(escape(str(text)), style)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Monthly Report — {fmt_month(pack['month'])}", author="Kavachio")
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
        return t

    m = pack["months"].get(pack["month"]) or {}
    story = [
        para("Kavachio", ParagraphStyle("brand", parent=sub, textColor=teal, fontName="Helvetica-Bold")),
        para(f"Monthly Report — {fmt_month(pack['month'])}", h1),
        para(" · ".join(x for x in (carrier, scope) if x)
                  + f" · numbers as at {datetime.fromisoformat(pack['as_of']).strftime('%d %B %Y')}"
                  + ("" if m.get("complete", True) else " · the month was still running"), sub),
        Spacer(1, 6),
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

    story.append(para("By broker and programme", h2))
    rows = [["Broker", "Programme", "Files on time", "Clean first time", "Errors",
             "Average fix time", "Waiting longer than target"]]
    for b in pack["by_broker_programme"]:
        rows.append([para(b.get("broker_name") or "—", cell),
                     para(b.get("programme_name") or "—", cell),
                     _of(b["on_time"], b["due_so_far"], b["on_time_pct"]),
                     _of(b["clean_first_time"], b["files"], b["clean_first_time_pct"]),
                     _num(b["errors"]), _took(b["avg_fix_days"]), _num(b["waiting_past_target"])])
    if len(rows) == 1:
        rows.append(["Nothing this month", "", "", "", "", "", ""])
    story.append(table(rows, [doc.width * w for w in (.2, .2, .13, .13, .09, .11, .14)], left=2))

    story.append(para("Last 6 months", h2))
    rows = [["Month", "Files on time", "Clean first time", "Errors found", "Average fix time",
             "Waiting longer than target at month end"]]
    for k in pack["trend"]:
        x = pack["months"].get(k) or {}
        if not x.get("has_data"):
            rows.append([fmt_month(k) + " · no records", "—", "—", "—", "—", "—"])
            continue
        rows.append([fmt_month(k), _pct(x["timeliness"]["on_time_pct"]),
                     _pct(x["quality"]["clean_first_time_pct"]), _num(x["quality"]["errors"]),
                     _took(x["fixes"]["avg_days"]), _num(x["fixes"]["waiting_past_target"])])
    story.append(table(rows, [doc.width * w for w in (.22, .14, .14, .14, .14, .22)]))

    def _footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#8B93A2"))
        canvas.drawString(d.leftMargin, 8 * mm, f"Kavachio · Monthly Report — {fmt_month(pack['month'])}")
        canvas.drawRightString(d.leftMargin + d.width, 8 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def render_xlsx(pack: dict, carrier: str, scope: str = "") -> bytes:
    """The pack as an Excel workbook — the same report, with numbers you can
    work with: counts as numbers, shares as percentages, times in days."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

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

    put(ws, 1, 1, f"Monthly Report — {fmt_month(pack['month'])}", font=Font(bold=True, size=14))
    put(ws, 2, 1, " · ".join(x for x in (carrier, scope) if x)
        + f" · numbers as at {pack['as_of']}")
    cols = _columns(pack)
    m = pack["months"].get(pack["month"]) or {}
    header = ["Measure"]
    for name, _k, vs in cols:
        header.append(name)
        if vs:
            header.append(f"vs {vs}")
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
    ws.column_dimensions["A"].width = 42
    for col in "BCDEF":
        ws.column_dimensions[col].width = 18

    wb2 = wb.create_sheet("By broker and programme")
    heads = ["Broker", "Programme", "Files due so far", "Arrived on time", "On time %",
             "Files checked", "Clean first time", "Clean first time %", "Errors",
             "Average fix time (days)", "Waiting longer than target"]
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
    for col, w in zip("ABCDEFGHIJK", (28, 28, 14, 14, 11, 13, 15, 16, 10, 20, 24)):
        wb2.column_dimensions[col].width = w

    wb3 = wb.create_sheet("Last 6 months")
    heads = ["Month", "Files on time %", "Clean first time %", "Errors found",
             "Average fix time (days)", "Waiting longer than target at month end"]
    for c, h in enumerate(heads, 1):
        put(wb3, 1, c, h, font=bold, fill=grey)
    for i, k in enumerate(pack["trend"], 2):
        x = pack["months"].get(k) or {}
        put(wb3, i, 1, fmt_month(k))
        if not x.get("has_data"):
            put(wb3, i, 2, "No records")
            continue
        t, q, f = x["timeliness"], x["quality"], x["fixes"]
        put(wb3, i, 2, t["on_time_pct"] / 100.0 if t["on_time_pct"] is not None else None, "0%")
        put(wb3, i, 3, q["clean_first_time_pct"] / 100.0 if q["clean_first_time_pct"] is not None else None, "0%")
        put(wb3, i, 4, q["errors"], "#,##0")
        put(wb3, i, 5, f["avg_days"], "0.0")
        put(wb3, i, 6, f["waiting_past_target"], "#,##0")
    for col, w in zip("ABCDEF", (20, 16, 18, 14, 22, 38)):
        wb3.column_dimensions[col].width = w

    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for x in row:
                x.alignment = Alignment(vertical="center")
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def files(pack: dict, file_format: str, carrier: str,
          scope: str = "") -> list[tuple[str, bytes, str]]:
    """(filename, data, mime) for each file the format asks for."""
    name = f"Monthly Report {pack['month']}"
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


def build_pack(s, tenant_id: int, month: str, now: Optional[datetime] = None) -> dict:
    import monthly_report as mr
    return mr.name_rows(s, mr.pack(s, tenant_id, month, now=now))


def _status(delivered: int, failed: int) -> str:
    if delivered and not failed:
        return "sent"
    return "partly" if delivered else "failed"


def send(s, tenant_id: int, month: str, *, kind: str, user_id: Optional[int] = None,
         now: Optional[datetime] = None,
         mailer: Optional[Callable[..., None]] = None) -> Optional[dict]:
    """Build, email and record one report. `kind`: scheduled | now.

    It goes to the carrier's logins (people). A scheduled send claims its month
    first and returns None when it was already sent. Commits."""
    from sqlalchemy.exc import IntegrityError
    from db import MonthlyReportSend
    from notifications import NOTIFY_MAIL_ACCOUNT, _app_link, _email_text, notification_email_html
    if mailer is None:
        from email_utils import send_email as mailer
    now = now or datetime.utcnow()
    cfg = settings(s, tenant_id)
    to = people(s, tenant_id)
    if not to:
        raise ValueError(NO_ONE)

    row = MonthlyReportSend(tenant_id=tenant_id, month=month, kind=kind,
                            scheduled_key=f"{tenant_id}:{month}" if kind == "scheduled" else None,
                            file_format=cfg["file_format"], delivery=cfg["delivery"],
                            sent_by_user_id=user_id, created_at=now)
    s.add(row)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        return None   # this month's automatic send has already gone

    carrier = carrier_name(s, tenant_id)
    # The carrier's name as it was, so a file downloaded again later reads the same.
    options = {"carrier": carrier}
    link = _app_link(f"/monthly-report?month={month}")
    # A link is only a link when the app's address is configured; otherwise
    # the file goes along so the email is never empty-handed.
    attach = cfg["delivery"] == "attach" or not link
    try:
        pack = build_pack(s, tenant_id, month, now=now)
        attachments = files(pack, cfg["file_format"], carrier) if attach else []
    except Exception:
        # Nothing has gone out: drop the claim so the next run can try again.
        s.rollback()
        s.delete(s.merge(row))
        s.commit()
        raise
    title = f"Monthly Report — {fmt_month(month)}"
    subject = SUBJECT_PREFIX + f"Monthly Report · {fmt_month(month)}"
    body = (f"The {carrier} Monthly Report for {fmt_month(month)} is attached."
            if attach else
            f"The {carrier} Monthly Report for {fmt_month(month)} is ready. Sign in to open it.")
    facts = headline(pack)

    results = []
    for p in to:
        try:
            mailer(p["email"], subject,
                   notification_email_html(title, body, facts, link, "Open the Monthly Report",
                                           greeting_name=p.get("name"), footer=REPORT_FOOTER),
                   text=_email_text(title, body, facts, link), account=NOTIFY_MAIL_ACCOUNT,
                   attachments=attachments or None)
            results.append({**{k: p.get(k) for k in ("user_id", "email", "name")},
                            "delivered": True, "error": None})
        except Exception as e:  # noqa: BLE001 — one bad address must not stop the rest
            log.warning("monthly report %s to %s failed: %s", month, p["email"], e)
            results.append({**{k: p.get(k) for k in ("user_id", "email", "name")},
                            "delivered": False, "error": str(e)[:300]})

    row.recipients = results
    row.delivered = sum(1 for r in results if r["delivered"])
    row.failed = len(results) - row.delivered
    row.pack, row.options = pack, options
    s.commit()
    return {"id": row.id, "month": month, "kind": kind, "delivered": row.delivered,
            "failed": row.failed, "status": _status(row.delivered, row.failed),
            "recipients": [{k: r[k] for k in ("name", "email", "delivered")} for r in results]}


def history(s, tenant_id: int, limit: int = 24) -> list[dict]:
    """The reports sent, newest first."""
    from db import MonthlyReportSend
    rows = (s.query(MonthlyReportSend).filter(MonthlyReportSend.tenant_id == tenant_id)
            .order_by(MonthlyReportSend.created_at.desc(), MonthlyReportSend.id.desc())
            .limit(limit).all())
    return [{"id": r.id, "month": r.month, "kind": r.kind,
             "sent_at": r.created_at.isoformat() if r.created_at else None,
             "file_format": r.file_format, "delivery": r.delivery,
             "people": len(r.recipients or []), "delivered": r.delivered, "failed": r.failed,
             "status": _status(r.delivered, r.failed) if r.pack is not None else "failed",
             "has_file": r.pack is not None} for r in rows]


def sent_months(s, tenant_id: int) -> set[str]:
    """The months whose automatic send has gone."""
    from db import MonthlyReportSend
    return {m for (m,) in s.query(MonthlyReportSend.month)
            .filter(MonthlyReportSend.tenant_id == tenant_id,
                    MonthlyReportSend.kind == "scheduled").all()}


def run_due(s, now: Optional[datetime] = None,
            mailer: Optional[Callable[..., None]] = None) -> int:
    """The daily job: send every carrier's report that is due today. Returns
    how many went out. One carrier's failure never stops the others."""
    from db import ActivityEvent, MonthlyReportSetting
    now = now or datetime.utcnow()
    sent = 0
    for cfg in (s.query(MonthlyReportSetting)
                .filter(MonthlyReportSetting.auto_send.is_(True)).all()):
        month = due_now(cfg.send_day, now.date())
        if month is None:
            continue
        tid = cfg.tenant_id
        try:
            r = send(s, tid, month, kind="scheduled", now=now, mailer=mailer)
        except Exception:  # noqa: BLE001
            log.exception("monthly report: carrier %s, %s failed", tid, month)
            s.rollback()
            continue
        if r is None:
            continue
        s.add(ActivityEvent(tenant_id=tid, actor="system", action="monthly_report_sent",
                            target=f"monthly_report:{month}",
                            details={"month": month, "kind": "scheduled",
                                     "delivered": r["delivered"], "failed": r["failed"]}))
        s.commit()
        sent += 1
    return sent
