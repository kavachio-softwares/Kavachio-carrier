"""
programme_report.py
───────────────────
The Programme Management Report: a sub-screen of the Executive Report. The
Executive Report shows the programmes in charts; this lists every programme
and broker for the same day, week, month or custom dates, and opens the
detail of one programme's broker under its row.

  management   one row per programme and broker: line of business, reporting
               period, files expected / received / missing, errors, premium,
               and a status — Attention when a file is missing or late or
               there are critical errors, else OK
  detail       one programme's broker: the files processed (submission
               history), files expected against received, the validation
               result, the checks that failed most, premium against the period
               before and last year, the contract, and missing or late files
  file         the management rows as a PDF or Excel file

NOTHING RE-COUNTED
  Files, errors and premium are the Executive Report's own readers narrowed
  to the programme and broker (executive_report's `program_id` and
  `broker_party_id`); files due are the Bordereau Calendar's own rule
  (submission_calendar.derive_status). A pair's premium is the Financial
  Summary's reading of the files (its _build rows, added by its own _add), in
  the currency the Financial Summary puts first.

Reading never writes; every function takes the caller's session.
"""
from __future__ import annotations

import datetime as dt
import io
from typing import Optional

from fastapi import HTTPException

import executive_report as er

ATTENTION, OK = "Attention", "OK"
SUBMISSIONS_SHOWN = 50

# derive_status → the words on screen.
OWED_WORDS = {"on_time": "Received", "received_late": "Arrived late", "overdue": "Missing",
              "never_arrived": "Never arrived", "due_today": "Due today",
              "due_soon": "Not due yet", "scheduled": "Not due yet"}


def _owed(s, tenant_id: int, w: dict, program_id: Optional[int] = None,
          broker_party_id: Optional[int] = None) -> list[dict]:
    """Every file due in the period, as it stood at the end of its last day."""
    from submission_calendar import derive_status
    out = []
    for e in er._expected(s, tenant_id, w["start"], w["end"], program_id, broker_party_id):
        got = e.received_at if e.received_at and e.received_at <= w["as_at"] else None
        st = derive_status(e.due_date, w["as_at"], 0, got, e.closes_on)
        out.append({"program_id": e.program_id, "broker_party_id": e.broker_party_id,
                    "month": e.period, "due_on": e.due_date.isoformat(),
                    "arrived_on": got.isoformat() if got else None, "state": st,
                    "status": OWED_WORDS.get(st, st),
                    # Due by now: the ones a broker owed (the calendar's rule, as _due_tally).
                    "due": not (st in ("scheduled", "due_soon") or (st == "due_today" and not got)),
                    "days_late": max(((got or w["as_at"]) - e.due_date).days, 0)
                    if st in ("received_late", "overdue", "never_arrived") else 0})
    return out


def _first_currency(rows: list) -> Optional[str]:
    """The currency the Financial Summary shows first: stated before "not
    stated" and "in review", then the most rows."""
    import financial_summary as fs
    n: dict[str, int] = {}
    for _o, _c, fig in rows:
        k = fs._currency_key(fig)
        n[k] = n.get(k, 0) + 1
    order = sorted(n, key=lambda k: (k in (fs.NOT_STATED, fs.IN_REVIEW), -n[k], k))
    return order[0] if order else None


def _pair_premium(s, tenant_id: int, w: dict) -> tuple[Optional[str], dict]:
    """Gross premium per (programme, broker): by bordereau month as the
    Financial Summary reads it, or, for a day, in the files processed that day
    (the Executive Report's daily money)."""
    import financial_summary as fs
    if w["cadence"] == "day":
        got = er._processed_money(s, tenant_id, w["start"], w["end"])
        real = {k: g for k, g in got["groups"].items() if k not in (fs.NOT_STATED, fs.IN_REVIEW)}
        pick = real or got["groups"]
        if not pick:
            return None, {}
        cur = max(pick, key=lambda k: (pick[k]["gross"] or 0, pick[k]["rows"]))
        return cur, dict(pick[cur]["pair"])
    from auth_deps import Principal
    me = Principal(user_id=0, tenant_id=tenant_id, role="carrier_admin")
    rows = fs._build(s, me, basis="month", period=w["money_month"], program_id=None,
                     broker_party_id=None, carrier_id=None, open_problems="include")["rows"]["current"]
    cur = _first_currency(rows)
    acc: dict = {}
    for o, _cells, fig in rows:
        if fs._currency_key(fig) == cur:
            fs._add(acc.setdefault((o.program_id, o.broker_party_id), fs._blank()), fig)
    return cur, {k: fs._close(a)["gross_premium"]["value"] for k, a in acc.items()}


def _with_broker(c) -> bool:
    """A contract with a broker — not a treaty, whose other side is a
    reinsurer (contract_types: its counterparty_party_type)."""
    from contract_types import CONTRACT_TYPES
    return (CONTRACT_TYPES.get((c.contract_type or "").strip()) or {}).get(
        "counterparty_party_type", "broker") == "broker"


def _type_label(key: Optional[str]) -> Optional[str]:
    from contract_types import CONTRACT_TYPES
    return (CONTRACT_TYPES.get((key or "").strip()) or {}).get("label") or key


def _line_of_business(p, contracts: list) -> Optional[str]:
    """The programme's product line; else its contract's class of business."""
    for v in (p.product_line, p.business_segment, p.annual_statement_lob,
              *(c.class_of_business for c in contracts)):
        if v and str(v).strip():
            return str(v).strip()
    return None


def _month_label(key: str) -> str:
    try:
        return dt.date.fromisoformat(key + "-01").strftime("%b %Y")
    except ValueError:
        return key


def management(s, tenant_id: int, *, cadence: str = "month", period: Optional[str] = None,
               today: Optional[dt.date] = None) -> dict:
    """Every programme and broker for the period."""
    import check_record as cr
    import program_stage as ps_
    from db import Program
    today = today or dt.datetime.utcnow().date()
    w = er.window(cadence, period, today)
    stages = {p.id: (p, st, cs) for p, st, cs in er.stage_of(s, tenant_id, w["as_at"])}
    pairs: dict[tuple, dict] = {}

    def pair(pid, bid) -> dict:
        return pairs.setdefault((pid, bid), {
            "program_id": pid, "broker_party_id": bid, "months": set(),
            "expected": 0, "received": 0, "missing": 0, "late": 0,
            "files": 0, "errors": 0, "critical": 0, "warning": 0, "info": 0})
    # Each broker with a contract on the programme, whether or not it sent anything.
    for p, _st, contracts in stages.values():
        for c in contracts:
            if c.broker_party_id and _with_broker(c):
                pair(p.id, c.broker_party_id)
    for o in _owed(s, tenant_id, w):
        if not o["due"]:
            continue
        r = pair(o["program_id"], o["broker_party_id"])
        r["months"].add(o["month"])
        r["expected"] += 1
        r["received"] += o["arrived_on"] is not None
        r["missing"] += o["state"] in ("overdue", "never_arrived")
        r["late"] += o["state"] == "received_late"
    q = cr.summary(s, tenant_id, er._at(w["start"]), er._at(w["end"]))
    for x in q["by_broker_programme"]:
        if x["program_id"] is None:
            continue
        r = pair(x["program_id"], x["broker_party_id"])
        for k in ("files", "errors", "critical", "warning", "info"):
            r[k] += x[k]
    currency, premium = _pair_premium(s, tenant_id, w)
    for (pid, bid) in premium:
        if pid is not None:
            pair(pid, bid)

    missing_progs = {pid for pid, _b in pairs if pid not in stages}
    if missing_progs:
        for p in s.query(Program).filter(Program.id.in_(missing_progs)):
            stages[p.id] = (p, None, [])
    brokers = er._names(s, (), [b for _p, b in pairs])["broker"]
    rows = []
    for (pid, bid), r in pairs.items():
        p, stage, contracts = stages.get(pid, (None, None, []))
        mine = [c for c in contracts if c.broker_party_id == bid and _with_broker(c)]
        months = sorted(r.pop("months"))
        rows.append({**r, "programme": (p.name if p else None) or f"#{pid}",
                     "broker": brokers.get(bid) or ("No broker" if bid is None else f"#{bid}"),
                     "stage": stage, "line_of_business": _line_of_business(p, mine) if p else None,
                     "reporting_period": ", ".join(_month_label(m) for m in months) if months
                     else (_month_label(w["money_month"]) if cadence != "day" else None),
                     "premium": premium.get((pid, bid)),
                     "status": ATTENTION if (r["missing"] or r["late"] or r["critical"]) else OK})
    rows.sort(key=lambda r: (r["programme"].lower(), r["broker"].lower()))
    return {**er.head(w, today), "money_month": None if cadence == "day" else w["money_month"],
            "currency": currency,
            "totals": {"programmes": len(stages),
                       "writing": sum(1 for _p, st, _c in stages.values() if st in ps_.WRITING_STAGES),
                       "expected": sum(r["expected"] for r in rows),
                       "received": sum(r["received"] for r in rows),
                       "missing": sum(r["missing"] for r in rows),
                       "errors": sum(r["errors"] for r in rows),
                       "premium": round(sum(r["premium"] or 0 for r in rows), 2) if currency else None},
            "rows": rows}


def detail(s, tenant_id: int, program_id: int, broker_party_id: int, *, cadence: str = "month",
           period: Optional[str] = None, today: Optional[dt.date] = None) -> dict:
    """One programme's broker for the period — what opens under its row."""
    import check_record as cr
    import program_stage as ps_
    from db import Contract, Program
    prog = s.get(Program, program_id)
    if prog is None or prog.tenant_id != tenant_id:
        raise HTTPException(404, "That programme is not one of yours.")
    today = today or dt.datetime.utcnow().date()
    w = er.window(cadence, period, today)
    scope = {"program_id": program_id, "broker_party_id": broker_party_id}
    money, ops, timeliness = er.core(s, tenant_id, w, today, **scope)
    runs = cr.first_runs(cr.runs_in(s, tenant_id, er._at(w["start"]), er._at(w["end"]), **scope))
    q = cr.summary(s, tenant_id, er._at(w["start"]), er._at(w["end"]), **scope)
    pair = next(iter(q["by_broker_programme"]), {})
    owed = _owed(s, tenant_id, w, **scope)
    contracts = s.query(Contract).filter(Contract.tenant_id == tenant_id,
                                         Contract.program_id == program_id,
                                         Contract.broker_party_id == broker_party_id).all()
    c = ps_.current_contract([x for x in contracts if _with_broker(x)])
    caps = {x["contract_id"]: x for x in
            er._contracts(s, tenant_id, w["start"], w["end"], w["as_at"], cadence == "day", **scope)["caps"]}
    broker = er._names(s, (), [broker_party_id])["broker"].get(broker_party_id)
    mine = sorted([x for x in contracts if _with_broker(x)],
                  key=lambda x: (x.id != (c.id if c else None), -(x.id or 0)))
    treaties = _treaties(s, tenant_id, program_id)
    return {
        **er.head(w, today),
        "programme": {"id": prog.id, "name": prog.name or f"#{prog.id}"},
        "broker": {"id": broker_party_id, "name": broker or f"#{broker_party_id}"},
        "money": {k: money.get(k) for k in ("month", "basis", "currency", "gross_premium",
                                            "commission", "earned_premium")},
        "operations": ops, "timeliness": timeliness,
        "submissions": [{"at": f.created_at.isoformat() if f.created_at else None,
                         "filename": f.filename, "month": f.period, "status": f.status,
                         "errors": f.errors_total or 0, "critical": f.critical_count or 0,
                         "rows": (f.rows_checked if f.rows_checked is not None else f.rows_out) or 0}
                        for f in sorted(runs, key=lambda f: f.created_at or dt.datetime.min,
                                        reverse=True)[:SUBMISSIONS_SHOWN]],
        "files_total": len(runs),
        "owed": {"expected": sum(1 for o in owed if o["due"]),
                 "received": sum(1 for o in owed if o["due"] and o["arrived_on"]),
                 "list": sorted(owed, key=lambda o: o["due_on"], reverse=True)},
        "validation": {"rows": pair.get("rows", 0), "rows_with_errors": min(pair.get("rows_with_errors", 0),
                                                                             pair.get("rows", 0)),
                       "errors": pair.get("errors", 0), "critical": pair.get("critical", 0),
                       "warning": pair.get("warning", 0), "info": pair.get("info", 0),
                       "top_errors": er._quality(s, tenant_id, w["start"], w["end"], **scope)["top_errors"]},
        # The current contract (Overview, Premium cap) — and every contract
        # this broker holds on the programme, the current one first.
        "contract": None if c is None else _contract_row(c, caps, current=True),
        "contracts": [_contract_row(x, caps, current=x is c) for x in mine],
        # The treaties protecting the programme: they cover every broker's business on it.
        "treaties": treaties,
    }


def _contract_row(c, caps: dict, current: bool = False) -> dict:
    cap = caps.get(c.id)
    return {
        "id": c.id, "name": c.name or c.filename or f"Contract #{c.id}",
        "type": _type_label(c.contract_type), "status": c.lifecycle or c.status,
        "starts": c.inception_dt.isoformat() if c.inception_dt else None,
        "ends": c.expiry_dt.isoformat() if c.expiry_dt else None,
        "umr": c.umr, "class_of_business": c.class_of_business,
        "year_of_account": c.year_of_account, "current": current,
        "cap": cap and {k: cap[k] for k in ("currency", "cap", "written", "used_pct", "flag")}}


def _treaties(s, tenant_id: int, program_id: int) -> list[dict]:
    """The programme's treaties (programme_rules.treaties: its home and any it
    covers, not ended) with the reinsurers on each (treaty_routes' panel)."""
    import programme_rules as prr
    out = []
    for t in prr.treaties(s, tenant_id, program_id):
        try:
            ids = sorted(prr.panel(s, t))
        except Exception:                                   # a panel that cannot be read: no names
            ids = []
        names = er._names(s, (), ids)["broker"]                  # party names, whatever the side
        out.append({**_contract_row(t, {}), "reinsurers": [names.get(i) or f"#{i}" for i in ids]})
    return out


# ── the file ───────────────────────────────────────────────────────────────
COLUMNS = ["Programme", "Broker", "Line of business", "Reporting period", "Expected files",
           "Received files", "Missing files", "Errors", "Premium", "Status"]


def table(out: dict) -> list[list]:
    cur = out["currency"]
    money = lambda v: "" if v is None else (f"{cur} {v:,.0f}" if cur else f"{v:,.0f}")  # noqa: E731
    return [COLUMNS] + [[r["programme"], r["broker"], r["line_of_business"] or "", r["reporting_period"] or "",
                         r["expected"], r["received"], r["missing"], r["errors"], money(r["premium"]),
                         r["status"]] for r in out["rows"]]


def render(out: dict, file_format: str, carrier: str) -> tuple[str, bytes, str]:
    """The management rows as a PDF (A4 landscape) or an Excel sheet."""
    import executive_delivery as ed
    title = f"Programme Management Report — {ed.period_title(out['cadence'], out['period'])}"
    sub = f"{carrier} · numbers as at {dt.date.fromisoformat(out['as_of']):%d %B %Y}"
    rows = table(out)
    name = f"Programme Management Report {out['period'].replace('..', ' to ')}"
    if file_format == "excel":
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        wb = Workbook()
        ws = wb.active
        ws.title = "Programmes"
        ws.append([title])
        ws["A1"].font = Font(bold=True, size=14)
        ws.append([sub])
        ws.append([])
        for r in rows:
            ws.append(r)
        for cell in ws[4]:
            cell.font, cell.fill = Font(bold=True), PatternFill("solid", fgColor="F3F4F7")
        for i, wd in enumerate((28, 26, 20, 18, 10, 10, 10, 8, 16, 11)):
            ws.column_dimensions[chr(65 + i)].width = wd
        ws.freeze_panes = "A5"
        buf = io.BytesIO()
        wb.save(buf)
        return (f"{name}.xlsx", buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title=title, author="Kavachio")
    ss = getSampleStyleSheet()
    cell = ss["Normal"].clone("cell", fontSize=8, leading=10)
    shares = (.16, .15, .12, .11, .07, .07, .07, .06, .11, .08)
    t = Table([[Paragraph(escape(str(v)), cell) for v in r] for r in rows],
              colWidths=[doc.width * x for x in shares], repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F7F8FB")),
                           ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E8EE")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    doc.build([Paragraph(escape(title), ss["Title"].clone("t", alignment=0, fontSize=16)),
               Paragraph(escape(sub), ss["Normal"]), Spacer(1, 8), t])
    return f"{name}.pdf", buf.getvalue(), "application/pdf"
