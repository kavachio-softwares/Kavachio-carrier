"""Single-sheet "complete details" report for a generated output export.

Reads everything from Postgres for one output_exports row (the run you just did
in the app) and flattens it into ONE Excel sheet:

    Clause  ->  Rule  ->  Compiled SQL (query)  ->  Sheet/Column mapping  ->  SQL output (exception)

Row types in the single sheet:
    Exception          one row per validation exception (the SQL output)
    Rule (0 exc)       a rule that fired no exception (clause + rule + query + mapping)
    Clause (no rule)   a contract clause that produced no rule

Nothing is re-run: rules/clauses come from validation_rule + clauses_extracted,
the compiled query from rule_spec.compiled_sql, the column mapping from the
output template, and the SQL output from output_exports.exceptions.

Usage:
    cd backend/python-services
    source venv/bin/activate
    python -m scripts.generate_exception_report                 # latest export with exceptions
    python -m scripts.generate_exception_report --export-id 141
    python -m scripts.generate_exception_report --out /path/report.xlsx
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_BACKEND_DIR, ".env"))
except Exception:
    pass

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import text

from db import canonical_engine

_REPO_ROOT = os.path.dirname(os.path.dirname(_BACKEND_DIR))  # .../kavachio
_DEMO = os.path.join(_REPO_ROOT, "Demo")

COLUMNS = [
    "#", "Clause ID", "Page", "Clause Type", "Clause",
    "Rule(s)", "Template(s)", "SQL Query(ies)", "Mapped Sheet · Column(s)",
    "Rows Impacted", "Sample Impacted Row", "Error Shown to User",
]
_WRAP = {"Clause", "SQL Query(ies)", "Sample Impacted Row", "Error Shown to User"}

# Second worksheet — EVERY clause in the contract, with its routing outcome.
ALL_CLAUSE_COLUMNS = [
    "#", "Clause ID", "Page", "Clause Type", "Title", "Routing", "# Rules", "Clause Text",
]
_ALL_WRAP = {"Title", "Clause Text"}
_ALL_WIDTHS = {"#": 5, "Clause ID": 9, "Page": 6, "Clause Type": 16, "Title": 42,
               "Routing": 18, "# Rules": 8, "Clause Text": 90}

# Map the stored rule_generation_status to a plain-language routing label.
_ROUTING_LABEL = {
    "rules_generated": "Rule generated",
    "in_review":       "Review queue",
    "not_rule_bearing": "Control register",
    "pending":         "Pending",
    "failed":          "Failed",
}


def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def fetch(export_id, contract_id=None):
    with canonical_engine.connect() as c:
        if contract_id is not None:
            # CONTRACT MODE — target a specific contract directly. Use its latest
            # output export (if any) for exceptions; otherwise report the rules +
            # clauses + compiled SQL with no exceptions (nothing has been run).
            crow = c.execute(text(
                "SELECT filename, output_template_id FROM contract WHERE contract_id = :c"),
                {"c": contract_id}).mappings().first()
            if not crow:
                raise SystemExit(f"contract {contract_id} not found")
            template_id = crow["output_template_id"]
            filename = crow["filename"] or f"contract_{contract_id}"
            exp = c.execute(text(
                "SELECT id, filename, template_id, exceptions FROM output_exports "
                "WHERE source_upload_id IN ("
                "  SELECT DISTINCT up.upload_id FROM upload_policy up "
                "  JOIN policy p ON p.policy_id = up.policy_id "
                "  WHERE p.contract_id = :c) "
                "ORDER BY id DESC LIMIT 1"), {"c": contract_id}).mappings().first()
            if exp:
                export_id = exp["id"]
                exceptions = _j(exp["exceptions"]) or []
                filename = exp["filename"] or filename
                template_id = exp["template_id"] or template_id
            else:
                export_id = None
                exceptions = []
        else:
            if export_id is None:
                export_id = c.execute(text(
                    "SELECT id FROM output_exports WHERE exception_count > 0 "
                    "ORDER BY id DESC LIMIT 1")).scalar()
            exp = c.execute(text(
                "SELECT id, filename, template_id, source_upload_id, exception_count, exceptions "
                "FROM output_exports WHERE id = :i"), {"i": export_id}).mappings().first()
            if not exp:
                raise SystemExit(f"output_exports id {export_id} not found")
            exceptions = _j(exp["exceptions"]) or []
            filename = exp["filename"]
            template_id = exp["template_id"]

            # contract id: from the exceptions (they carry it) — fall back to the upload's policies
            contract_id = next((e.get("contract_id") for e in exceptions if e.get("contract_id")), None)
            if contract_id is None:
                contract_id = c.execute(text(
                    "SELECT contract_id FROM policy p JOIN upload_policy up ON up.policy_id=p.policy_id "
                    "WHERE up.upload_id=:u AND contract_id IS NOT NULL LIMIT 1"),
                    {"u": exp["source_upload_id"]}).scalar()

        clauses = {r["clause_id"]: dict(r) for r in c.execute(text(
            "SELECT clause_id, clause_type, title, text, page_number, section_header, "
            "rule_generation_status FROM clauses_extracted WHERE contract_id = :c"),
            {"c": contract_id}).mappings()}

        rules = {}
        for r in c.execute(text(
            "SELECT rule_id, rule_name, severity, canonical_target, rule_spec, error_message, "
            "source_clause_id, source_verbatim_text, source_page_number "
            "FROM validation_rule WHERE contract_id = :c ORDER BY rule_id"), {"c": contract_id}):
            m = dict(r._mapping)
            spec = _j(m.get("rule_spec")) or {}
            ir = spec.get("ir") or {}
            m["compiled_sql"] = spec.get("compiled_sql") or ""
            m["template"] = ir.get("template")
            m["canonical_target"] = _j(m.get("canonical_target")) or {}
            rules[m["rule_id"]] = m

        # field -> sheet from the output template
        field_to_sheet = {}
        st = _j(c.execute(text("SELECT structure FROM export_templates WHERE id=:t"),
                          {"t": template_id}).scalar()) or {}
        for sh in st.get("sheets", []):
            sheet = sh.get("sheet_name", "")
            for col in sh.get("columns", []):
                name = col.get("column_name") or col.get("header")
                if name:
                    field_to_sheet.setdefault(name, sheet)

    return dict(export_id=export_id, filename=filename, contract_id=contract_id,
                exceptions=exceptions, clauses=clauses, rules=rules, field_to_sheet=field_to_sheet)


def fetch_live(template_id, upload_id, contract_id=None, limit=0):
    """Compute results LIVE — resolve the active contract for the template, build
    the upload's output records, run the DuckDB validation against the CURRENT
    persisted rules, and return the same shape fetch() does. Use when no export
    has been validated yet (e.g. right after regenerating rules). limit>0 samples
    the first N policies (the full set can be thousands of rows)."""
    from db import SessionLocal
    from assembler import fetch_policies
    from exporter import build_output_records
    from duckdb_validation import run_validation

    with canonical_engine.connect() as c:
        st = _j(c.execute(text("SELECT structure FROM export_templates WHERE id=:t"),
                          {"t": template_id}).scalar()) or {}
        if contract_id is None:
            contract_id = c.execute(text(
                "SELECT contract_id FROM contract WHERE output_template_id=:t "
                "AND status_ops='active' ORDER BY contract_id DESC LIMIT 1"),
                {"t": template_id}).scalar()
        if not contract_id:
            raise SystemExit(f"no active contract for template {template_id}")
        fn = c.execute(text("SELECT filename FROM contract WHERE contract_id=:c"),
                       {"c": contract_id}).scalar()
        clauses = {r["clause_id"]: dict(r) for r in c.execute(text(
            "SELECT clause_id, clause_type, title, text, page_number, section_header, "
            "rule_generation_status FROM clauses_extracted WHERE contract_id=:c"),
            {"c": contract_id}).mappings()}
        rules = {}
        for r in c.execute(text(
            "SELECT rule_id, rule_name, severity, canonical_target, rule_spec, error_message, "
            "source_clause_id, source_verbatim_text, source_page_number "
            "FROM validation_rule WHERE contract_id=:c AND rule_status!='disabled' ORDER BY rule_id"),
            {"c": contract_id}):
            m = dict(r._mapping)
            spec = _j(m.get("rule_spec")) or {}
            m["compiled_sql"] = spec.get("compiled_sql") or ""
            m["template"] = (spec.get("ir") or {}).get("template")
            m["canonical_target"] = _j(m.get("canonical_target")) or {}
            rules[m["rule_id"]] = m
        field_to_sheet, schema_cols = {}, {}
        for sh in st.get("sheets", []):
            sheet = sh.get("sheet_name", "")
            cols = []
            for col in sh.get("columns", []):
                name = col.get("column_name") or col.get("header")
                if name:
                    field_to_sheet.setdefault(name, sheet)
                    cols.append(name)
            schema_cols[sheet] = cols
        pids = [row[0] for row in c.execute(text(
            "SELECT policy_id FROM upload_policy WHERE upload_id=:u ORDER BY policy_id"),
            {"u": upload_id}).fetchall()]
    if limit and limit > 0:
        pids = pids[:limit]

    with SessionLocal() as s:
        policies = fetch_policies(s, pids)
    by_sheet = {}
    for p in policies:
        pid = (p.get("policy") or {}).get("policy_id")
        for block in build_output_records(st, [p]):
            recs = by_sheet.setdefault(block["sheet"], [])
            for rec in block["records"]:
                row = dict(rec); row["policy_id"] = pid; recs.append(row)
    records_by_sheet = [{"sheet": sh, "records": r} for sh, r in by_sheet.items()]

    dv = run_validation(records_by_sheet, list(rules.values()),
                        contract={"id": contract_id, "filename": fn},
                        template_id=template_id, schema_cols=schema_cols)
    return dict(export_id=f"live-u{upload_id}", filename=fn, contract_id=contract_id,
                exceptions=dv.get("exceptions") or [], clauses=clauses, rules=rules,
                field_to_sheet=field_to_sheet, _policies=len(pids),
                _unprocessable=dv.get("unprocessable") or [])


def _mapped(rule, field_to_sheet):
    ct = rule.get("canonical_target") or {}
    fields = ct.get("output_fields") or ([ct.get("output_field")] if ct.get("output_field") else [])
    fields = [f for f in fields if f]
    sheets = sorted({field_to_sheet.get(f, "") for f in fields} - {""})
    return ", ".join(sheets), ", ".join(fields)


def build_rows(data):
    """ONE row per clause (all clauses, in sequence). Each row aggregates the
    clause's rule(s), their SQL, the count of impacted rows, ONE sample row, and
    the user-facing error — never more than one row per clause."""
    rules, clauses, f2s = data["rules"], data["clauses"], data["field_to_sheet"]
    # Distinguish "rule ran and flagged nothing" from "no export/validation run".
    no_export = data.get("export_id") is None

    # group rules by their source clause, and exceptions by rule
    rules_by_clause = {}
    for r in rules.values():
        rules_by_clause.setdefault(r.get("source_clause_id"), []).append(r)
    exc_by_rule = {}
    for e in data["exceptions"]:
        exc_by_rule.setdefault(e.get("rule_id"), []).append(e)

    rows = []
    for seq, cid in enumerate(sorted(clauses), start=1):
        cl = clauses[cid]
        crules = rules_by_clause.get(cid, [])

        rule_names = "\n".join(r.get("rule_name") or "" for r in crules)
        templates = "\n".join(str(r.get("template") or "") for r in crules)
        sqls = "\n\n--------------------\n\n".join(
            (r.get("compiled_sql") or "").strip() for r in crules if r.get("compiled_sql"))
        # mapped sheet · columns across this clause's rules
        maps = []
        for r in crules:
            sheet, cols = _mapped(r, f2s)
            if cols:
                maps.append(f"{sheet}: {cols}" if sheet else cols)
        mapped = "\n".join(maps)

        # exceptions for ALL of this clause's rules
        cexc = [e for r in crules for e in exc_by_rule.get(r["rule_id"], [])]
        impacted_rows = sorted({e.get("row") for e in cexc if e.get("row") is not None})
        n_impacted = len(impacted_rows) or len(cexc)

        sample = ""
        error_shown = ""
        if cexc:
            s = cexc[0]
            loc = f"Row {s.get('row')}"
            if s.get("policy_number"):
                loc += f" (policy {s.get('policy_number')})"
            sample = f"{loc} · {s.get('column') or s.get('field')} = {s.get('actual_value')}"
            error_shown = s.get("message") or s.get("reason") or ""
        elif crules:
            # rule(s) exist but produced no exception
            error_shown = (
                "(not validated — no export run for this contract yet)"
                if no_export
                else "(no rows impacted — rule passed on this upload)"
            )

        rows.append({
            "#": seq,
            "Clause ID": cid,
            "Page": cl.get("page_number"),
            "Clause Type": cl.get("clause_type"),
            "Clause": (cl.get("title") and f"{cl.get('title')} — ") or "",  # prefix title if any
            "Rule(s)": rule_names,
            "Template(s)": templates,
            "SQL Query(ies)": sqls,
            "Mapped Sheet · Column(s)": mapped,
            "Rows Impacted": n_impacted if crules else "",
            "Sample Impacted Row": sample,
            "Error Shown to User": error_shown,
        })
        # put the full clause text into the Clause cell (title prefix + verbatim text)
        rows[-1]["Clause"] = (rows[-1]["Clause"] or "") + (cl.get("text") or "")
    return rows


def build_all_clauses_rows(data):
    """ONE row per clause in the contract — the full clause inventory with its
    routing outcome (rule generated / review / control), independent of whether a
    rule or exception exists. Guarantees EVERY extracted clause appears."""
    clauses = data["clauses"]
    rules_by_clause = {}
    for r in data["rules"].values():
        rules_by_clause.setdefault(r.get("source_clause_id"), []).append(r)

    rows = []
    for seq, cid in enumerate(sorted(clauses), start=1):
        cl = clauses[cid]
        status = cl.get("rule_generation_status")
        rows.append({
            "#": seq,
            "Clause ID": cid,
            "Page": cl.get("page_number"),
            "Clause Type": cl.get("clause_type"),
            "Title": cl.get("title") or "",
            "Routing": _ROUTING_LABEL.get(status, status or "—"),
            "# Rules": len(rules_by_clause.get(cid, [])),
            "Clause Text": cl.get("text") or "",
        })
    return rows


def _write_sheet(ws, columns, rows, widths, wrap):
    fill = PatternFill("solid", fgColor="1F4E78")
    font = Font(bold=True, color="FFFFFF")
    for c, h in enumerate(columns, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.fill = fill; cell.font = font
        cell.alignment = Alignment(vertical="top", wrap_text=True)
    for r, row in enumerate(rows, 2):
        for c, h in enumerate(columns, 1):
            cell = ws.cell(row=r, column=c, value=row.get(h))
            cell.alignment = Alignment(wrap_text=h in wrap, vertical="top")
    for c, h in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(c)].width = widths.get(h, max(12, min(len(h) + 4, 22)))
    ws.freeze_panes = "A2"


def write_xlsx(rows, out_path, title, all_clause_rows=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "Complete Details"
    widths = {"#": 5, "Clause ID": 9, "Page": 6, "Clause Type": 14, "Clause": 60,
              "Rule(s)": 26, "Template(s)": 16, "SQL Query(ies)": 80,
              "Mapped Sheet · Column(s)": 28, "Rows Impacted": 13,
              "Sample Impacted Row": 40, "Error Shown to User": 50}
    _write_sheet(ws, COLUMNS, rows, widths, _WRAP)

    # Second sheet — the complete clause inventory (every clause in the contract).
    if all_clause_rows is not None:
        ws2 = wb.create_sheet("All Clauses")
        _write_sheet(ws2, ALL_CLAUSE_COLUMNS, all_clause_rows, _ALL_WIDTHS, _ALL_WRAP)

    wb.save(out_path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Single-sheet complete-details report for an output export")
    ap.add_argument("--export-id", type=int, default=None, help="output_exports.id (default: latest with exceptions)")
    ap.add_argument("--contract-id", type=int, default=None,
                    help="report for a specific contract (uses its latest export for exceptions, "
                         "else rules/clauses with no exceptions); with --live, overrides the active contract")
    ap.add_argument("--out", default=None, help="output xlsx path")
    ap.add_argument("--live", action="store_true",
                    help="compute results live from current rules + upload data (no persisted export needed)")
    ap.add_argument("--template-id", type=int, help="[--live] output template id")
    ap.add_argument("--upload-id", type=int, help="[--live] BDX upload id to validate")
    ap.add_argument("--limit", type=int, default=0, help="[--live] sample first N policies (0 = all)")
    args = ap.parse_args(argv)

    data = fetch(args.export_id, contract_id=args.contract_id)
    rows = build_rows(data)
    all_clause_rows = build_all_clauses_rows(data)
    os.makedirs(_DEMO, exist_ok=True)
    default_name = (f"Exception_report_export{data['export_id']}.xlsx"
                    if data["export_id"] is not None
                    else f"Exception_report_contract{data['contract_id']}.xlsx")
    out = args.out or os.path.join(_DEMO, default_name)
    write_xlsx(rows, out, data["filename"], all_clause_rows=all_clause_rows)

    with_rule = sum(1 for r in rows if r["Rule(s)"])
    with_impact = sum(1 for r in rows if isinstance(r["Rows Impacted"], int) and r["Rows Impacted"] > 0)
    total_impacted = sum(r["Rows Impacted"] for r in rows if isinstance(r["Rows Impacted"], int))
    exp_label = data["export_id"] if data["export_id"] is not None else "(none — no export run)"
    print(f"export #{exp_label}  contract {data['contract_id']}  ({data['filename']})")
    print(f"  {len(rows)} rows (one per clause) | {with_rule} clauses have rules | "
          f"{with_impact} clauses flagged data | {total_impacted} impacted-row hits across {len(data['exceptions'])} exceptions")
    print(f"  'All Clauses' sheet: {len(all_clause_rows)} clause(s)")
    print(f"\n✓ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
