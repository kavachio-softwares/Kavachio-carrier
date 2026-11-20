"""
audit_package.py
────────────────
Task 50.1 — the audit package: for a chosen policy or period, one file with
every check that ran, the contract wording and page each came from, every
result (passed, failed, could not run), every error found and every decision
made on it. Excel (one sheet each) or PDF (one section each), with the Column
Guide.

Read only from what is already kept (check_record's check_run / check_result /
check_error, validation_rule and clauses_extracted, exception_decision_log):
nothing is stored. Checks are named as they were when they ran. Which VERSION
of a contract's check ran comes from Rule History (rule_history.py, 48.2):
the version in place when the check last ran in the period, and — when it has
changed since — its settings then, as Rule History kept them. The contract
wording is the clause's, which a rule change does not alter.

WHO: the same reach as the Audit Logs tabs (audit_history.reach) — a carrier
its own files, a broker only the files it sent, Kavachio everything or one
carrier (?mga=).
"""
from __future__ import annotations

import datetime as dt
import io
import re
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, text

import audit_history as ah

MAX_ROWS = 20_000          # per sheet, as the Audit Logs download (audit_feed.MAX_EXPORT_ROWS)
PDF_ROWS = 1_500           # per section of the PDF; the Excel file has every row
POLICY_EXPORTS = 300       # files read through when one policy is asked for
RUN_KINDS = ("run", "rerun")
RESULT_WORDS = {"passed": "Passed", "failed": "Failed", "not_run": "Could not run"}
DECISION_WORDS = {"approve": "Accepted as it is", "fix": "Value corrected", "dismiss": "Dismissed",
                  "reject": "Rejected"}
SEVERITY = {"critical": "Critical", "warning": "Warning", "info": "Info"}


def _window(month: Optional[str], since: Optional[str], until: Optional[str]) -> tuple[dt.datetime, dt.datetime, str]:
    """The period asked for: a month ('2026-10') or from–to days. Neither:
    the last 24 months (a policy asked for on its own)."""
    if month:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise HTTPException(400, "Pick a month.")
        a = dt.date(int(month[:4]), int(month[5:]), 1)
        b = (a.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return dt.datetime.combine(a, dt.time()), dt.datetime.combine(b, dt.time()), f"{a:%B %Y}"
    a, b = ah._day(since), ah._day(until)
    if a and b and b < a:
        raise HTTPException(400, "The to date must be on or after the from date.")
    if not a and not b:
        b = dt.datetime.utcnow().date()
        a = b - dt.timedelta(days=730)
        words = "the last 24 months"
    else:
        a = a or dt.date(2000, 1, 1)
        b = b or dt.datetime.utcnow().date()
        words = f"{a:%d %b %Y} – {b:%d %b %Y}"
    return dt.datetime.combine(a, dt.time()), dt.datetime.combine(b, dt.time()) + dt.timedelta(days=1), words


def _too_many(n: int, what: str) -> None:
    if n > MAX_ROWS:
        raise HTTPException(400, f"This package would have more than {MAX_ROWS:,} {what}. "
                                 "Pick a shorter period or one programme.")


def build(s, principal, *, policy: Optional[str] = None, month: Optional[str] = None,
          since: Optional[str] = None, until: Optional[str] = None, programme: Optional[int] = None,
          broker: Optional[int] = None, mga: Optional[str] = None, dates: Optional[str] = None) -> dict:
    """Everything the file says, as sheets of rows. `dates`: the Date filter as
    the screen named it ("Last 30 days"), so the file says what was picked."""
    from db import CheckError, CheckResult, CheckRun, Contract, ExceptionDecisionLog, OutputExport
    policy = (policy or "").strip() or None
    if not policy and not (month or since or until):
        raise HTTPException(400, "Pick a policy or a period.")
    r = ah.reach(s, principal, mga)
    lo, hi, when = _window(month, since, until)

    q = r.filter(s.query(CheckRun), CheckRun.tenant_id, CheckRun.broker_party_id).filter(
        CheckRun.kind.in_(RUN_KINDS), CheckRun.created_at >= lo, CheckRun.created_at < hi)
    if programme:
        q = q.filter(CheckRun.program_id == programme)
    if broker:
        q = q.filter(CheckRun.broker_party_id == broker)
    runs = q.order_by(CheckRun.created_at.asc()).all()

    if policy:
        runs = _runs_with_policy(s, principal, runs, policy)
    _too_many(len(runs), "files")
    run_ids = [x.id for x in runs]
    by_run = {x.id: x for x in runs}
    names = ah._names(s, {x.program_id for x in runs}, {x.broker_party_id for x in runs},
                      {x.tenant_id for x in runs})

    results = (s.query(CheckResult).filter(CheckResult.run_id.in_(run_ids)).order_by(CheckResult.run_id, CheckResult.id)
               .all() if run_ids else [])
    eq = s.query(CheckError).filter(CheckError.run_id.in_(run_ids)) if run_ids else None
    if eq is not None and policy:
        eq = eq.filter(CheckError.policy_number == policy)
    errors = eq.order_by(CheckError.run_id, CheckError.id).limit(MAX_ROWS + 1).all() if eq is not None else []
    _too_many(len(results), "check results")
    _too_many(len(errors), "errors")

    # the checks that ran, with where their words come from
    rule_ids = sorted({x.rule_id for x in results if x.rule_id})
    rules = _rules(s, rule_ids)
    contracts = {c.id: c.name or c.filename or f"Contract #{c.id}" for c in
                 s.query(Contract).filter(Contract.id.in_({v["contract_id"] for v in rules.values() if v["contract_id"]}
                                                          | {x.contract_id for x in runs if x.contract_id} or {0}))}

    def file_name(run) -> str:
        return run.filename or f"File #{run.export_id or run.id}"

    failed_for_policy = {(e.run_id, e.check_key) for e in errors} if policy else set()
    checks: dict[str, dict] = {}
    result_rows = []
    for x in results:
        run = by_run[x.run_id]
        outcome = x.outcome
        if policy and outcome != "not_run":
            outcome = "failed" if (x.run_id, x.check_key) in failed_for_policy else "passed"
        c = checks.setdefault(x.check_key, {"name": x.name or x.check_key, "rule": rules.get(x.rule_id),
                                            "rule_id": x.rule_id, "last_ran": None,
                                            "severity": x.severity, "runs": 0, "passed": 0, "failed": 0, "not_run": 0})
        if run.created_at and (c["last_ran"] is None or run.created_at > c["last_ran"]):
            c["last_ran"] = run.created_at
        c["runs"] += 1
        c[outcome] += 1
        result_rows.append([
            file_name(run), names["programme"].get(run.program_id) or "—", names["party"].get(run.broker_party_id) or "—",
            ah.af_period(run.period) or "—", run.version_no or 1, run.created_at.strftime("%d %b %Y %H:%M") if run.created_at else "—",
            x.name or x.check_key, RESULT_WORDS.get(outcome, outcome),
            (sum(1 for e in errors if e.run_id == x.run_id and e.check_key == x.check_key) if policy else x.error_count or 0),
            x.message or ""])

    ran = versions_that_ran(s, {c["rule_id"]: c["last_ran"] for c in checks.values() if c["rule_id"]},
                            {x.contract_id for x in runs if x.contract_id}
                            | {v["contract_id"] for v in rules.values() if v["contract_id"]})
    check_rows = []
    for key, c in sorted(checks.items(), key=lambda kv: kv[1]["name"].lower()):
        rule = c["rule"] or {}
        v = ran.get(c["rule_id"]) or {}
        builtin = key.startswith("type:")
        check_rows.append([
            c["name"], SEVERITY.get((c["severity"] or "").lower(), c["severity"] or "—"),
            contracts.get(rule.get("contract_id")) or ("Column type check" if builtin else "—"),
            rule.get("wording") or ("Built in: the column must hold this kind of value." if builtin else "—"),
            rule.get("page") or "—",
            "Built in" if builtin else v.get("version") or "—",
            "—" if builtin else v.get("since") or "Not on record",
            v.get("then") or "", c["runs"], c["passed"], c["failed"], c["not_run"]])

    error_rows = [[file_name(by_run[e.run_id]), checks.get(e.check_key, {}).get("name") or e.check_key or "—",
                   e.policy_number or "—", e.sheet or "—", e.row or "—", e.field or "—", e.actual_value or "",
                   SEVERITY.get((e.severity or "").lower(), e.severity or "—"), e.message or "",
                   e.first_found_at.strftime("%d %b %Y") if e.first_found_at else "—"] for e in errors]

    export_ids = {x.export_id for x in runs if x.export_id}
    dq = (s.query(ExceptionDecisionLog).filter(ExceptionDecisionLog.export_id.in_(export_ids))
          if export_ids else None)
    if dq is not None and policy:
        dq = dq.filter(ExceptionDecisionLog.policy_number == policy)
    decisions = dq.order_by(ExceptionDecisionLog.decided_at.asc()).limit(MAX_ROWS + 1).all() if dq is not None else []
    _too_many(len(decisions), "decisions")
    people = ah.People(s, r.v)
    files_by_export = {x.export_id: file_name(x) for x in runs if x.export_id}
    if export_ids:
        files_by_export.update({o.id: o.filename for o in s.query(OutputExport).filter(OutputExport.id.in_(export_ids))
                                if o.id not in files_by_export})
    decision_rows = [[d.decided_at.strftime("%d %b %Y %H:%M") if d.decided_at else "—",
                      files_by_export.get(d.export_id) or "—", d.policy_number or "—", d.field or "—",
                      d.old_value or "", d.new_value or "", DECISION_WORDS.get(d.kind, d.kind),
                      people.name(d.decided_by_user_id, "—"), d.reason or ""] for d in decisions]

    passed = sum(c["passed"] for c in checks.values())
    failed = sum(c["failed"] for c in checks.values())
    not_run = sum(c["not_run"] for c in checks.values())
    carrier = ", ".join(sorted({v for v in names["carrier"].values() if v})) or "—"
    programme_name = (names["programme"].get(programme) or ah._names(s, [programme], ())["programme"].get(programme)
                      or f"Programme #{programme}") if programme else None
    broker_name = (ah._names(s, (), [broker])["party"].get(broker) or f"Broker #{broker}") if broker else None
    period = when.replace(" – ", " to ")
    dates = (dates or "").strip()[:40]
    dates_words = (f"the {dates[0].lower()}{dates[1:]} ({period})" if dates.lower().startswith("last")
                   else period)
    # The same sentence the download popup showed, so the file says what it holds.
    lead = (f"Every check, result, error and decision from {dates_words}, for "
            f"{programme_name or 'all programmes'} and {broker_name or 'all brokers'}"
            f"{f', policy {policy}' if policy else ''}.")
    summary = [
        ["Dates", f"{dates} ({period})" if dates and dates.lower().startswith("last") else period],
        ["Programme", programme_name or "All programmes"],
        ["Broker", broker_name or "All brokers"],
        *([["Policy", policy]] if policy else []),
        ["Carrier", carrier],
        ["Files checked", len(runs)],
        ["Checks run", len(results)],
        ["Passed", passed], ["Failed", failed], ["Could not run", not_run],
        ["Errors found", len(errors)],
        ["Decisions made", len(decisions)],
        ["Made on", dt.datetime.utcnow().strftime("%d %b %Y %H:%M") + " (UTC)"],
        ["Made by", people.name(principal.user_id, "—")],
    ]
    title = f"Audit package — policy {policy}" if policy else f"Audit package — {when}"
    return {"title": title, "lead": lead, "summary": summary,
            "sheets": [
                ("Checks that ran", ["Check", "Severity", "Contract", "Contract wording", "Page", "Version that ran",
                                     "Changed since", "Check as it ran", "Times run", "Passed", "Failed",
                                     "Could not run"], check_rows),
                ("Results", ["File", "Programme", "Broker", "Reporting period", "Version", "Checked on", "Check",
                             "Result", "Errors", "Why"], result_rows),
                ("Errors found", ["File", "Check", "Policy", "Sheet", "Row", "Field", "Value found", "Severity",
                                  "Message", "First found"], error_rows),
                ("Decisions", ["Date and time", "File", "Policy", "Field", "Old value", "New value", "Decision",
                               "By", "Reason"], decision_rows),
            ]}


def _when(x: dt.datetime) -> str:
    return x.strftime("%d %b %Y")


def _as_it_was(view: dict) -> str:
    """A kept version of a check (rule_history.view), in one cell."""
    import json
    parts = []
    for label in ("How strict", "Status", "Field checked", "Message"):
        if view.get(label) not in (None, ""):
            parts.append(f"{label}: {view[label]}")
    if view.get("Settings") not in (None, "", {}):
        parts.append("Settings: " + json.dumps(view["Settings"], default=str, ensure_ascii=False))
    return "; ".join(parts)[:3000]


def versions_that_ran(s, last_ran: dict[int, dt.datetime], contract_ids: set) -> dict[int, dict]:
    """For each rule that ran (rule_id → when it last ran in the period): the
    version then, whether and when it changed after, and — if it did — the
    check as it was, from Rule History's kept copy.

    Versions are counted as Rule History counts them (rule_history._rows): a
    rule's first recorded event is version 1 if it created the rule, else
    version 2, version 1 being what was in place before history began. A rule
    re-saved since is found by its old rule_id in the history rows, so one
    whose row was deleted and written again still gets its version."""
    from db import ActivityEvent
    import change_history as ch
    import rule_history as rh
    if not last_ran:
        return {}
    ids = list(last_ran)
    keys: dict[int, str] = {}
    for x in s.execute(text("""SELECT rule_id, contract_id, rule_name, canonical_target
                                 FROM validation_rule WHERE rule_id = ANY(:ids)"""), {"ids": ids}).mappings():
        keys[x["rule_id"]] = rh.key_of(dict(x))
    events: dict[str, list] = {}
    cids = {c for c in contract_ids if c}
    if cids:
        from sqlalchemy import or_
        q = (s.query(ActivityEvent.target, ActivityEvent.created_at, ActivityEvent.details)
             .filter(ActivityEvent.action == ch.ACTION,
                     or_(*[ActivityEvent.target.like(f"contract_rule:{c}:%") for c in sorted(cids)]))
             .order_by(ActivityEvent.created_at.asc(), ActivityEvent.id.asc()))
        for target, at, det in q:
            det = det if isinstance(det, dict) else {}
            events.setdefault(target, []).append((at, det))
            if det.get("entity_id") in last_ran and det["entity_id"] not in keys:
                keys[det["entity_id"]] = target
    began = s.query(func.min(ActivityEvent.created_at)).filter(ActivityEvent.action == ch.ACTION).scalar()
    out = {}
    for rid, at in last_ran.items():
        evs = events.get(keys.get(rid) or "", [])
        if not evs:
            out[rid] = {"version": "As today", "since": (
                "No change recorded" if began and at and at >= began else
                f"No change recorded since {_when(began)}, when rule history began" if began else
                "Not on record")}
            continue
        first = 0 if evs[0][1].get("event") == "created" else 1
        before = [e for e in evs if at and e[0] <= at]
        after = [e for e in evs if not at or e[0] > at]
        version = first + len(before) if before or first else None
        row = {"version": f"Version {version}" if version else "—"}
        if not after:
            row["since"] = "No"
        else:
            last = after[-1]
            n = len(after)
            row["since"] = (f"Yes — {n} change{'' if n == 1 else 's'}, last on {_when(last[0])} "
                            f"(now version {first + len(evs)}; "
                            f"{ch.EVENT_WORDS.get(last[1].get('event'), last[1].get('event'))})")
            kept = ((after[0][1].get("before") or {}).get("rule"))
            if kept:
                row["then"] = _as_it_was(kept)
        out[rid] = row
    return out


def _rules(s, rule_ids: list[int]) -> dict[int, dict]:
    """Each rule's contract and the words it came from: the clause it was read
    from (title, text, page) or the wording kept on the rule itself."""
    if not rule_ids:
        return {}
    rows = s.execute(text("""
        SELECT r.rule_id, r.contract_id, r.source_verbatim_text, r.source_page_number,
               c.title AS clause_title, c.text AS clause_text, c.page_number AS clause_page
          FROM validation_rule r
          LEFT JOIN clauses_extracted c ON c.clause_id = r.source_clause_id
         WHERE r.rule_id = ANY(:ids)"""), {"ids": rule_ids}).mappings().all()
    out = {}
    for x in rows:
        words = (x["source_verbatim_text"] or x["clause_text"] or "").strip()
        if words.startswith("[Generic rule]"):
            words = "Standard check (not from the contract): " + words[len("[Generic rule]"):].strip()
        if x["clause_title"] and words and not words.startswith(x["clause_title"]):
            words = f"{x['clause_title']}: {words}"
        page = x["source_page_number"] or x["clause_page"]
        out[x["rule_id"]] = {"contract_id": x["contract_id"], "wording": words[:3000] or None,
                             "page": f"Page {page}" if page else None}
    return out


def _runs_with_policy(s, principal, runs: list, policy: str) -> list:
    """The runs whose file holds this policy: errors on it, or its rows in the
    processed file (read by heading, policy_search)."""
    from db import CheckError
    import policy_search as ps
    want = ps.policy_key(policy)
    hit = {e[0] for e in s.query(CheckError.run_id).filter(
        CheckError.run_id.in_([x.id for x in runs] or [0]), CheckError.policy_number == policy)}
    rest = [x for x in runs if x.id not in hit and x.export_id][-POLICY_EXPORTS:]
    if rest:
        try:
            seat = ps.seat_of(s, principal)
            exports = ps._exports(s, seat, ids={x.export_id for x in rest})
            structures = ps._structures(s, {o.template_id for o in exports})
            holding = set()
            for o in exports:
                if any(ps.policy_key(ln["policy"]) == want for ln in ps._lines_of(s, o, structures.get(o.template_id))):
                    holding.add(o.id)
            hit |= {x.id for x in rest if x.export_id in holding}
        except HTTPException:
            pass
    return [x for x in runs if x.id in hit]


# ── the file ───────────────────────────────────────────────────────────────
def render(pkg: dict, file_format: str) -> tuple[str, bytes, str]:
    name = re.sub(r"\s+", " ", re.sub(r"[^\w\- ]+", " ", pkg["title"].replace("—", "-"))).strip()
    if file_format == "excel":
        return _excel(pkg, name)
    if file_format == "pdf":
        return _pdf(pkg, name)
    raise HTTPException(400, "Pick Excel or PDF.")


def _excel(pkg: dict, name: str):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    import export_glossary as gl
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append([pkg["title"]])
    ws["A1"].font = Font(bold=True, size=14)
    if pkg.get("lead"):
        ws.append([pkg["lead"]])
        ws["A2"].font = Font(italic=True)
    ws.append([])
    for k, v in pkg["summary"]:
        ws.append([k, v])
        ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 22, 60
    widths = {"Check as it ran": 60, "Changed since": 40, "Contract wording": 80, "Why": 50, "Message": 50, "Reason": 40, "File": 36, "Check": 36}
    for title, header, rows in pkg["sheets"]:
        sh = wb.create_sheet(title)
        sh.append(header)
        for c in sh[1]:
            c.font, c.fill = Font(bold=True), PatternFill("solid", fgColor="F3F4F7")
        if not rows:
            sh.append(["Nothing to show."])
        for r in rows:
            sh.append(r)
        for i, h in enumerate(header):
            col = sh.cell(row=1, column=i + 1).column_letter
            sh.column_dimensions[col].width = widths.get(h, max(12, len(h) + 4))
            if widths.get(h, 0) >= 40:
                for cell in sh[col][1:]:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
        sh.freeze_panes = "A2"
    try:
        gl.xlsx_sheet(wb, skip=("Summary",))
    except Exception:  # noqa: BLE001 — the guide never costs the package
        pass
    buf = io.BytesIO()
    wb.save(buf)
    return f"{name}.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _pdf(pkg: dict, name: str):
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    import export_glossary as gl
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=10 * mm, bottomMargin=10 * mm, title=pkg["title"], author="Kavachio")
    ss = getSampleStyleSheet()
    cell = ss["Normal"].clone("cell", fontSize=7, leading=8.5)
    head = ss["Normal"].clone("head", fontSize=7, leading=8.5, fontName="Helvetica-Bold")
    style = TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F7")),
                        ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#E5E8EE")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP")])

    def table(header, rows, shares=None):
        n = len(header)
        shares = shares or [1 / n] * n
        data = [[Paragraph(escape(str(h)), head) for h in header]] + \
               [[Paragraph(escape(str(v)), cell) for v in r] for r in rows]
        t = Table(data, colWidths=[doc.width * x for x in shares], repeatRows=1)
        t.setStyle(style)
        return t

    story = [Paragraph(escape(pkg["title"]), ss["Title"].clone("t", alignment=0, fontSize=16)),
             *([Paragraph(escape(pkg["lead"]), ss["Normal"])] if pkg.get("lead") else []), Spacer(1, 6),
             Paragraph("Summary", ss["Heading2"]),
             table(["", ""], [[k, v] for k, v in pkg["summary"]], [.25, .75])]
    shares = {"Checks that ran": [.11, .05, .09, .22, .04, .06, .11, .14, .045, .045, .045, .045]}
    for title, header, rows in pkg["sheets"]:
        story += [PageBreak(), Paragraph(escape(title), ss["Heading2"])]
        if len(rows) > PDF_ROWS:
            story.append(Paragraph(f"Showing the first {PDF_ROWS:,} of {len(rows):,}. "
                                   "Download the Excel file to see them all.", ss["Normal"]))
            rows = rows[:PDF_ROWS]
        story.append(table(header, rows, shares.get(title)) if rows
                     else Paragraph("Nothing to show.", ss["Normal"]))
    try:
        guide = gl.pdf_rows(story)
        if len(guide) > 1:
            story += [PageBreak(), Paragraph(gl.TITLE, ss["Heading2"]), table(guide[0], guide[1:], [.2, .6, .2])]
    except Exception:  # noqa: BLE001
        pass
    doc.build(story)
    return f"{name}.pdf", buf.getvalue(), "application/pdf"
