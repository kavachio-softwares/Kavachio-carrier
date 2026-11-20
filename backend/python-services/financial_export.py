"""Financial Reporting (task 35) — the screen as an Excel file.

  workbook(summary, rows, seat)  →  .xlsx bytes

Built only from what the screen itself is served — financial_summary.summary()
and financial_summary.rows() with the same period and filters — so the file and
the screen always agree. Nothing is worked out here except how a number is
written. Sheets: Summary (every figure per currency, against the period before
and the same period last year), By programme, By broker (carrier only),
Bordereau rows (the rows the screen's tab lists: none with a figure needing
review) and the Column Guide.

Amounts in different currencies are never added together: each currency has
its own lines, as on screen.
"""
from __future__ import annotations

import calendar
import io
import re
from typing import Optional

# The screen's own names for each figure (pages/FinancialSummary.tsx METRICS),
# in the order financial_summary.METRICS lists them.
TITLES = {
    "gross_premium": "Gross premium",
    "total_gwp": "Total gross written premium (100%)",
    "commission_amount": "Commission",
    "commission_pct": "Commission rate",
    "brokerage_amount": "Brokerage",
    "brokerage_pct": "Brokerage rate",
    "fees": "Fees",
    "taxes": "Taxes",
    "net_premium": "Net premium (bordereau)",
    "earned_premium": "Earned premium",
}
# The figures the By programme / By broker tables show, as on screen.
PART_METRICS = ("gross_premium", "total_gwp", "commission_amount", "net_premium")
_STATUS = {"calculated": "Worked out", "needs_review": "Needs a check"}

_MONEY_FMT = "#,##0.00"
_PCT_FMT = "0.00%"
_CHANGE_FMT = "+0.0%;-0.0%;0.0%"
_PTS_FMT = '+0.00" pts";-0.00" pts";0.00" pts"'


def period_name(p: Optional[str]) -> str:
    """"2026-10" → "Oct 2026", "2026-Q4" → "Q4 2026", "2026" stays."""
    if not p:
        return "—"
    m = re.fullmatch(r"(\d{4})-(\d{2})", p)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{calendar.month_abbr[int(m.group(2))]} {m.group(1)}"
    q = re.fullmatch(r"(\d{4})-Q([1-4])", p, re.I)
    if q:
        return f"Q{q.group(2)} {q.group(1)}"
    return p


def rows_line(counts: dict) -> str:
    """The screen's "Sent in 2 policy rows · Missing in 1 policy row"."""
    def n_rows(n: int) -> str:
        return f"{n} policy row{'' if n == 1 else 's'}"
    out = []
    if counts.get("reported"):
        out.append(f"Sent in {n_rows(counts['reported'])}")
    if counts.get("calculated"):
        out.append(f"Worked out for {n_rows(counts['calculated'])}")
    if counts.get("needs_review"):
        out.append(f"Needs a check in {n_rows(counts['needs_review'])}")
    if counts.get("not_reported"):
        out.append(f"Missing in {n_rows(counts['not_reported'])}")
    return " · ".join(out)


def _currency(group: dict) -> str:
    return group.get("label") or group.get("currency") or ""


def _value(t: dict, window: str):
    """A figure's value as Excel should hold it: a rate as a fraction (shown
    as %), an amount as is, nothing as an empty cell."""
    v = (t.get(window) or {}).get("value")
    if v is None:
        return None
    return v / 100.0 if t.get("unit") == "percent" else v


def _change(t: dict, against: str):
    c = (t.get("change") or {}).get(against)
    if c is None:
        return None
    return c if t.get("unit") == "percent" else c / 100.0


def workbook(summary: dict, rows: list[dict], seat: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1F3A5F")
    wins = summary.get("windows") or {}
    cur = period_name((wins.get("current") or {}).get("period"))
    prev = period_name((wins.get("previous") or {}).get("period"))
    last = period_name((wins.get("last_year") or {}).get("period"))
    show_last_year = summary.get("basis") != "year"
    groups = summary.get("groups") or []

    def heading(ws, labels: list[str], widths: list[int]) -> None:
        ws.append(labels)
        for cell in ws[ws.max_row]:
            cell.font, cell.fill = head_font, head_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def fmt(cell, unit: str) -> None:
        cell.number_format = _PCT_FMT if unit == "percent" else _MONEY_FMT

    wb = Workbook()

    # ── Summary ──────────────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Summary"
    ws.append([f"Financial Reporting — {cur}"])
    ws["A1"].font = Font(bold=True, size=13)
    files = (wins.get("current") or {}).get("files") or 0
    n_rows = (wins.get("current") or {}).get("rows") or 0
    line = (f"{cur}: {files} file{'' if files == 1 else 's'}, "
            f"{n_rows} policy row{'' if n_rows == 1 else 's'}. Compared with {prev}"
            + (f" and {last}." if show_last_year else "."))
    ws.append([line])
    ws.append([])
    labels = ["Currency", "Figure", cur, prev, "Change on previous period"]
    widths = [16, 34, 18, 18, 14]
    if show_last_year:
        labels += [last, "Change on same period last year"]
        widths += [18, 16]
    labels.append("Policy rows behind it")
    widths.append(48)
    heading(ws, labels, widths)
    for g in groups:
        for key, title in TITLES.items():
            t = (g.get("totals") or {}).get(key)
            if not t:
                continue
            vals = [_currency(g), title, _value(t, "current"), _value(t, "previous"),
                    _change(t, "previous")]
            if show_last_year:
                vals += [_value(t, "last_year"), _change(t, "last_year")]
            vals.append(rows_line((t.get("current") or {}).get("counts") or {}))
            ws.append(vals)
            r = ws.max_row
            for col in (3, 4) + ((6,) if show_last_year else ()):
                fmt(ws.cell(r, col), t.get("unit"))
            for col in (5,) + ((7,) if show_last_year else ()):
                ws.cell(r, col).number_format = _PTS_FMT if t.get("unit") == "percent" else _CHANGE_FMT
    ws.freeze_panes = "C5"

    # ── By programme / By broker ─────────────────────────────────────────
    parts = [("By programme", "Programme", "by_programme")]
    if seat == "carrier":
        parts.append(("By broker", "Broker", "by_broker"))
    for title, who, key in parts:
        sh = wb.create_sheet(title)
        heading(sh, ["Currency", who] + [TITLES[k] for k in PART_METRICS]
                + ["Gross premium change on previous period"],
                [16, 30] + [20] * len(PART_METRICS) + [18])
        for g in groups:
            for p in g.get(key) or []:
                tots = p.get("totals") or {}
                sh.append([_currency(g), p.get("name")]
                          + [_value(tots.get(k) or {}, "current") for k in PART_METRICS]
                          + [_change(tots.get("gross_premium") or {}, "previous")])
                r = sh.max_row
                for i in range(len(PART_METRICS)):
                    sh.cell(r, 3 + i).number_format = _MONEY_FMT
                sh.cell(r, 3 + len(PART_METRICS)).number_format = _CHANGE_FMT
        sh.freeze_panes = "C2"

    # ── Bordereau rows ───────────────────────────────────────────────────
    sh = wb.create_sheet("Bordereau rows")
    cols = ["Policy number", "File", "Row", "Programme"]
    widths = [20, 34, 7, 24]
    if seat == "carrier":
        cols.append("Broker")
        widths.append(24)
    cols += ["Period", "Transaction", "Cover start", "Cover end", "Currency"]
    widths += [12, 16, 13, 13, 11]
    cols += [TITLES[k] for k in TITLES]
    widths += [18] * len(TITLES)
    cols.append("Notes")
    widths.append(60)
    heading(sh, cols, widths)
    first_figure = cols.index(TITLES["gross_premium"]) + 1
    for r in rows:
        figs = r.get("figures") or {}
        vals = [r.get("policy"), r.get("filename"), r.get("row"), r.get("programme")]
        if seat == "carrier":
            vals.append(r.get("broker"))
        vals += [period_name(r.get("period")), (r.get("transaction") or {}).get("sent"),
                 (r.get("cover_start") or {}).get("value"), (r.get("cover_end") or {}).get("value"),
                 (r.get("currency") or {}).get("value")]
        notes = []
        for k, title in TITLES.items():
            f = figs.get(k) or {}
            v = f.get("value")
            vals.append(None if v is None else (v / 100.0 if k.endswith("_pct") else v))
            label = _STATUS.get(f.get("status"))
            if label:
                notes.append(f"{title}: {label}" + (f" — {f['how']}" if f.get("how") else ""))
        vals.append("; ".join(notes) or None)
        sh.append(vals)
        n = sh.max_row
        for i, k in enumerate(TITLES):
            sh.cell(n, first_figure + i).number_format = _PCT_FMT if k.endswith("_pct") else _MONEY_FMT
    sh.freeze_panes = "B2"
    if rows:
        sh.auto_filter.ref = sh.dimensions

    # 45.2 — what every column means, in plain English.
    import export_glossary as gl
    gl.xlsx_sheet(wb)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
