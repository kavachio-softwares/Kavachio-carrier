"""Shared helpers extracted from the monolith's validation_routes.py (endpoints removed)."""
from __future__ import annotations

__all__ = [
    'APIRouter',
    'Any',
    'BaseModel',
    'DecideRequest',
    'Decimal',
    'DecisionItem',
    'Depends',
    'ExportDecideRequest',
    'ExportDecisionItem',
    'FieldEditBody',
    'FieldEditItem',
    'FieldsSaveBody',
    'HTTPException',
    'Optional',
    'Principal',
    'SessionLocal',
    'ValidateBody',
    '_DECISION_STATUS',
    '_apply_canonical_edit',
    '_build_tagged_records',
    '_decide_direct_lane',
    '_decision_note',
    '_derive_recommendation',
    '_fetch_output_records',
    '_fetch_policies',
    '_fetch_rules',
    '_jsonable',
    '_persist',
    '_resolve_contract_for_validation',
    '_resolve_contract_id',
    '_revalidate_edit',
    '_revalidate_edits',
    '_row_to_payload',
    '_scope_for_policy',
    '_skip_validation',
    '_violation_keys',
    'annotations',
    'assert_tenant_owns',
    'current_principal',
    'date',
    'datetime',
    'json',
    'log',
    'logging',
    'resolve_tenant_id',
    'text',
]

"""Validation orchestration.

Flow:  Frontend -> POST /api/validate (here)
       -> store validation_run + validation_exception -> return result.

The legacy JS validation service (global/custom/ajv engines on port 4000) has
been REMOVED. Those rule types carry no compiled SQL, so they are not evaluated
here — /api/validate records a CLEAN run (zero exceptions) and proceeds. Contract
`ir_v1` rules are validated separately by the DuckDB engine (see /export/validate
in main.py). `_skip_validation` is the no-op stand-in for the old JS call.
"""

import json

import logging

from datetime import date, datetime

from decimal import Decimal

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException

from pydantic import BaseModel

from sqlalchemy import text

from db import SessionLocal

from common_app_routes import resolve_tenant_id, assert_tenant_owns

from auth_deps import current_principal, Principal

log = logging.getLogger("bdx.validation")

class ValidateBody(BaseModel):
    uploadId: int
    contractId: Optional[int] = None
    tenantId: Optional[int] = None
    engines: Optional[list[str]] = None
    stage: str = "input"
    # Output template to validate against. Required for stage="output": contract
    # rules are written in terms of the OUTPUT column names (e.g.
    # "Aggregate Limit (USD)"), so the validator must see the resolved output
    # rows — not the raw policy table row.
    templateId: Optional[int] = None

def _jsonable(value: Any) -> Any:
    """Make a DB value JSON-serializable for the JS service."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value

def _row_to_payload(row: dict) -> dict:
    return {k: _jsonable(v) for k, v in row.items()}

def _fetch_policies(s, upload_id: int) -> list[dict]:
    pid_rows = s.execute(
        text("SELECT policy_id FROM upload_policy WHERE upload_id = :u ORDER BY policy_id"),
        {"u": upload_id},
    ).fetchall()
    policy_ids = [r[0] for r in pid_rows]
    if not policy_ids:
        return []
    rows = s.execute(
        text("SELECT * FROM policy WHERE policy_id = ANY(:ids) ORDER BY policy_id"),
        {"ids": policy_ids},
    ).mappings().all()
    return [dict(r) for r in rows]

def _fetch_rules(s, tenant_id, contract_id, stage: str) -> list[dict]:
    print("------tenant_id----",tenant_id)
    rows = s.execute(
        text(
            """
            SELECT vr.rule_id, vr.rule_engine, vr.rule_name, vr.severity,
                   vr.canonical_target, vr.rule_spec, vr.error_message,
                   vr.source_clause_id, vr.source_verbatim_text, vr.source_page_number,
                   rcl.name AS rule_class_name
            FROM validation_rule vr
            LEFT JOIN rule_class_library rcl USING (rule_class_id)
            WHERE vr.rule_status = 'active'
              AND (vr.tenant_id IS NULL OR vr.tenant_id = :tid)
              AND (vr.contract_id IS NULL OR vr.contract_id = :cid)
            ORDER BY (vr.contract_id IS NOT NULL) DESC,
                     (vr.tenant_id   IS NOT NULL) DESC,
                     vr.rule_id
            """
        ),
        {"stage": stage, "tid": tenant_id, "cid": contract_id},
    ).mappings().all()
    return [dict(r) for r in rows]

def _fetch_output_records(s, upload_id: int, template_id: int) -> list[dict]:
    """Build the resolved OUTPUT rows that an export would write, keyed by the
    output template's column names (e.g. "Aggregate Limit (USD)").

    Contract rules target output-template field names with values that live
    across the canonical model (coverage/premium/location/... — not just the
    `policy` row). Validating the raw policy row therefore silently no-ops every
    rule on a child-table field (the property is simply absent). Building the
    same records the exporter writes makes range/formula/aggregate rules see
    their fields. Each record is tagged with `policy_id` for traceability.
    """
    import json as _json
    from assembler import fetch_policies
    from exporter import build_output_records

    pid_rows = s.execute(
        text("SELECT policy_id FROM upload_policy WHERE upload_id = :u ORDER BY policy_id"),
        {"u": upload_id},
    ).fetchall()
    policy_ids = [r[0] for r in pid_rows]
    if not policy_ids:
        return []

    structure = s.execute(
        text("SELECT structure FROM export_templates WHERE id = :t"),
        {"t": template_id},
    ).scalar()
    if not structure:
        return []
    if isinstance(structure, str):
        structure = _json.loads(structure)

    policies = fetch_policies(s, policy_ids)
    records: list[dict] = []
    for p in policies:
        pid = (p.get("policy") or {}).get("policy_id")
        # Build one policy at a time so every emitted output row can be tagged
        # back to its policy_id (build_output_records flattens across policies).
        for block in build_output_records(structure, [p]):
            for rec in block["records"]:
                row = dict(rec)
                row["policy_id"] = pid
                records.append(row)
    return records

def _skip_validation(records: list[dict], rules: list[dict], engines=None,
                     tenant_id=None, contract_id=None) -> dict:
    """No-op stand-in for the removed JS validation service.

    The legacy global/custom/ajv engines ran in Node and those rules carry no
    compiled SQL, so they are not evaluated. We return a CLEAN result (zero
    exceptions) in the same shape the JS service used, so callers proceed without
    Node. Contract `ir_v1` rules are validated by the DuckDB engine elsewhere.
    """
    if rules:
        log.info("validation: skipping %d %s rule(s) — JS engine removed, not evaluated",
                 len(rules), "/".join(engines) if engines else "global/custom/ajv")
    return {
        "recordCount": len(records or []),
        "rules": {"total": 0, "custom": 0},
        "engines": {},
        "exceptions": {"total": 0, "critical": 0, "warning": 0, "info": 0, "items": []},
        "proceedToCanonical": True,
    }

def _persist(s, body, tenant_id, contract_id, result) -> Optional[int]:
    """Write validation_run + validation_exception. Best-effort; returns run_id."""
    exc = result.get("exceptions", {}) or {}
    rules = result.get("rules", {}) or {}
    engines = result.get("engines", {}) or {}
    custom = engines.get("custom") or {}
    ajv = engines.get("ajv") or {}
    items = exc.get("items", []) or []

    run_id = s.execute(
        text(
            """
            INSERT INTO validation_run
              (tenant_id, bdx_upload_id, contract_id, validation_stage,
               rules_evaluated, ajv_rules_count, custom_rules_count,
               violations_count, critical_count, warning_count, info_count,
               rows_validated, proceeded_to_canonical, status,
               started_at, completed_at, duration_ms, created_by, created_at)
            VALUES
              (:tid, :bu, :cid, :stage, :re, :ajv, :cust, :vc, :cc, :wc, :ic,
               :rv, :ptc, 'completed', now(), now(), 0, 'python_orchestrator', now())
            RETURNING run_id
            """
        ),
        {
            "tid": tenant_id,
            "bu": body.uploadId,
            "cid": contract_id,
            "stage": body.stage,
            "re": rules.get("total", 0),
            "ajv": ajv.get("rulesEvaluated", ajv.get("skipped", 0)),
            "cust": rules.get("custom", 0),
            "vc": exc.get("total", 0),
            "cc": exc.get("critical", 0),
            "wc": exc.get("warning", 0),
            "ic": exc.get("info", 0),
            "rv": result.get("recordCount", 0),
            "ptc": result.get("proceedToCanonical", True),
        },
    ).scalar()

    for it in items:
        s.execute(
            text(
                """
                INSERT INTO validation_exception
                  (tenant_id, validation_run_id, rule_id, source_entity,
                   source_entity_id, severity, field_path, expected_value,
                   actual_value, status)
                VALUES
                  (:tid, :run, :rule, :se, :sid, :sev, :fp, :exp, :act, :st)
                """
            ),
            {
                "tid": it.get("tenant_id"),
                "run": run_id,
                "rule": it.get("rule_id"),
                "se": it.get("source_entity"),
                "sid": it.get("source_entity_id"),
                "sev": it.get("severity"),
                "fp": it.get("field_path"),
                "exp": it.get("expected_value"),
                "act": it.get("actual_value"),
                "st": it.get("status", "open"),
            },
        )
    return run_id

def _resolve_contract_for_validation(s, template_id: int, tenant_id) -> Optional[int]:
    """Find the contract whose rules govern an output template, so the output
    stage runs the CONTRACT rules (not just global ones) even when the chosen
    template isn't directly linked to a contract — which otherwise means no
    exceptions and no popup.

    Order: (1) contract linked to this template (active, then latest);
           (2) the active-rule contract whose rules reference the most of this
               template's output columns (scoped to the tenant)."""
    import json as _json
    if not template_id:
        return None
    # Latest contract directly linked to this output template (status column on the
    # ops contract table is 'status_ops'; we don't filter on it — latest wins).
    cid = s.execute(
        text("SELECT contract_id FROM contract WHERE output_template_id = :t "
             "ORDER BY contract_id DESC LIMIT 1"),
        {"t": template_id},
    ).scalar()
    if cid:
        return cid
    # Column-overlap fallback.
    struct = s.execute(
        text("SELECT structure FROM export_templates WHERE id = :t"), {"t": template_id}
    ).scalar()
    if not struct:
        return None
    if isinstance(struct, str):
        struct = _json.loads(struct)
    cols = [(c.get("column_name") or "").strip()
            for sh in struct.get("sheets", []) for c in sh.get("columns", [])
            if (c.get("column_name") or "").strip()]
    if not cols:
        return None
    rows = s.execute(
        text("SELECT contract_id, rule_spec, canonical_target FROM validation_rule "
             "WHERE rule_status = 'active' AND contract_id IS NOT NULL "
             "AND (tenant_id IS NULL OR tenant_id = :tid)"),
        {"tid": tenant_id},
    ).mappings().all()
    from collections import defaultdict
    # Count DISTINCT template columns each contract's rules reference (not a raw
    # sum, which would just favour whichever contract has the most rules).
    cols_by_contract: dict = defaultdict(set)
    for r in rows:
        blob = _json.dumps([r["rule_spec"], r["canonical_target"]], default=str)
        for col in cols:
            if col in blob:
                cols_by_contract[r["contract_id"]].add(col)
    threshold = min(3, len(cols))
    # Among contracts that confidently match this template's columns, take the
    # MOST RECENT — the contract the user most likely just set up for it.
    strong = [cid for cid, cs in cols_by_contract.items() if len(cs) >= threshold]
    if strong:
        return max(strong)
    any_overlap = [cid for cid, cs in cols_by_contract.items() if cs]
    return max(any_overlap) if any_overlap else None

class FieldEditBody(BaseModel):
    uploadId: int
    templateId: int
    fieldPath: str
    newValue: Any
    policyId: Optional[int] = None
    exceptionId: Optional[int] = None
    actualValue: Optional[Any] = None
    ruleId: Optional[int] = None
    # Contract whose rules to re-validate against. The UI sends it; if omitted we
    # derive it from the exception's rule or the output template, because the
    # policy row's own contract_id is often NULL for these uploads.
    contractId: Optional[int] = None
    # When true ("Save"), the backend EXECUTES the SCD-2 SQL itself (after
    # re-validation passes) instead of only returning it for manual execution.
    apply: bool = False

def _resolve_contract_id(s, body, policy_id) -> Optional[int]:
    """Find the contract whose rules govern this edit (so re-validation uses the
    SAME rule set the original output validation did)."""
    if body.contractId is not None:
        return body.contractId
    if body.exceptionId is not None:
        cid = s.execute(
            text("SELECT vr.contract_id FROM validation_exception e "
                 "JOIN validation_rule vr ON vr.rule_id = e.rule_id "
                 "WHERE e.exception_id = :e"),
            {"e": body.exceptionId},
        ).scalar()
        if cid is not None:
            return cid
    # Fall back to the contract that owns this output template.
    cid = s.execute(
        text("SELECT contract_id FROM contract WHERE output_template_id = :t "
             "ORDER BY contract_id DESC LIMIT 1"),
        {"t": body.templateId},
    ).scalar()
    if cid is not None:
        return cid
    # Last resort: the policy's own contract_id (often NULL).
    return s.execute(
        text("SELECT contract_id FROM policy WHERE policy_id = :p"), {"p": policy_id}
    ).scalar()

def _scope_for_policy(s, policy_id: int):
    row = s.execute(
        text("SELECT tenant_id, contract_id FROM policy WHERE policy_id = :p"),
        {"p": policy_id},
    ).mappings().first()
    if not row:
        return None, None
    return row["tenant_id"], row["contract_id"]

def _violation_keys(result: dict) -> set:
    items = (result.get("exceptions", {}) or {}).get("items", []) or []
    return {(it.get("rule_id"), it.get("source_entity_id"), it.get("field_path"))
            for it in items}

def _build_tagged_records(structure: dict, policies: list[dict]) -> list[dict]:
    """Flatten assembled policies into output rows tagged with policy_id (mirrors
    `_fetch_output_records`, but from in-memory policy dicts so we can substitute
    a proposed edit before rendering)."""
    from exporter import build_output_records
    records: list[dict] = []
    for p in policies:
        pid = (p.get("policy") or {}).get("policy_id")
        for block in build_output_records(structure, [p]):
            for rec in block["records"]:
                row = dict(rec)
                row["policy_id"] = pid
                records.append(row)
    return records

def _apply_canonical_edit(policies: list[dict], policy_id: int, table: str,
                          target_id: int, column: str, new_raw: Any,
                          is_scalar: bool) -> bool:
    """Apply the proposed value to the EXACT target row in the assembled policy
    dicts (by primary key for child tables — no value-matching guesswork). Returns
    True iff a row was actually changed. new_raw is the pre-transform value;
    build_output_records applies the column transform exactly as a real export."""
    from canonical import pk_column
    pk = pk_column(table)
    for p in policies:
        if (p.get("policy") or {}).get("policy_id") != policy_id:
            continue
        if is_scalar or table == "policy":
            if isinstance(p.get("policy"), dict):
                p["policy"][column] = new_raw
                return True
            return False
        for row in p.get(table) or []:
            if row.get(pk) == target_id:
                row[column] = new_raw
                return True
        return False
    return False

def _revalidate_edit(s, upload_id: int, template_id: int, policy_id: int,
                     field_path: str, table: str, target_id: int, column: str,
                     new_raw: Any, is_scalar: bool,
                     contract_id_override: Optional[int] = None) -> dict:
    """Re-run the contract rules with the proposed value substituted into the
    EXACT target row, and report any NEW violation the edit would create.

    Operates on assembled policy dicts (not flattened rows) so a single child row
    is targeted by primary key. Best-effort: on any failure returns {error: ...}
    and the caller proceeds without blocking (revalidationSkipped).
    """
    import copy
    import json as _json

    structure = s.execute(
        text("SELECT structure FROM export_templates WHERE id = :t"),
        {"t": template_id},
    ).scalar()
    if not structure:
        return {"error": "output template has no structure"}
    if isinstance(structure, str):
        structure = _json.loads(structure)

    pid_rows = s.execute(
        text("SELECT policy_id FROM upload_policy WHERE upload_id = :u ORDER BY policy_id"),
        {"u": upload_id},
    ).fetchall()
    policy_ids = [r[0] for r in pid_rows]
    if not policy_ids:
        return {"introduced": [], "targetStillViolates": False}

    tenant_id, derived_contract = _scope_for_policy(s, policy_id)
    contract_id = contract_id_override if contract_id_override is not None else derived_contract
    rules = _fetch_rules(s, tenant_id, contract_id, "output")
    if not rules:
        return {"introduced": [], "targetStillViolates": False}

    try:
        from assembler import fetch_policies
        policies = fetch_policies(s, policy_ids)
        base_records = _build_tagged_records(structure, policies)
        edited = copy.deepcopy(policies)
        applied = _apply_canonical_edit(
            edited, policy_id, table, target_id, column, new_raw, is_scalar)
        if not applied:
            return {"error": "could not locate the target row to re-validate"}
        mod_records = _build_tagged_records(structure, edited)
        # JS engine removed: both sides are clean, so no edit is ever flagged as
        # introducing a custom/ajv violation here.
        base = _skip_validation(base_records, rules, ["custom", "ajv"], tenant_id, contract_id)
        after = _skip_validation(mod_records, rules, ["custom", "ajv"], tenant_id, contract_id)
    except Exception as e:  # noqa: BLE001
        return {"error": f"validation service error: {e}"}

    base_keys = _violation_keys(base)
    after_items = (after.get("exceptions", {}) or {}).get("items", []) or []
    introduced = [
        {"rule_id": it.get("rule_id"), "field_path": it.get("field_path"),
         "severity": it.get("severity"), "actual_value": it.get("actual_value"),
         "expected_value": it.get("expected_value")}
        for it in after_items
        if (it.get("rule_id"), it.get("source_entity_id"), it.get("field_path")) not in base_keys
    ]
    target_still = any(
        it.get("source_entity_id") == policy_id and it.get("field_path") == field_path
        for it in after_items
    )
    return {"introduced": introduced, "targetStillViolates": target_still}

class FieldEditItem(BaseModel):
    fieldPath: str
    newValue: Any
    policyId: Optional[int] = None
    exceptionId: Optional[int] = None
    actualValue: Optional[Any] = None

class FieldsSaveBody(BaseModel):
    uploadId: int
    templateId: int
    contractId: Optional[int] = None
    edits: list[FieldEditItem]
    apply: bool = True

def _revalidate_edits(s, upload_id, template_id, resolved_edits, contract_id):
    """Re-run the contract rules with ALL the proposed edits applied at once and
    report any NEW violation they collectively introduce. resolved_edits carry
    policy_id/table/target_id/column/new_typed/is_scalar."""
    import copy, json as _json
    structure = s.execute(
        text("SELECT structure FROM export_templates WHERE id = :t"), {"t": template_id},
    ).scalar()
    if not structure:
        return {"error": "output template has no structure"}
    if isinstance(structure, str):
        structure = _json.loads(structure)
    pid_rows = s.execute(
        text("SELECT policy_id FROM upload_policy WHERE upload_id = :u ORDER BY policy_id"),
        {"u": upload_id},
    ).fetchall()
    policy_ids = [r[0] for r in pid_rows]
    if not policy_ids:
        return {"introduced": []}
    tenant_id = next((r.get("tenant_id") for r in _fetch_policies(s, upload_id)
                      if r.get("tenant_id") is not None), None)
    rules = _fetch_rules(s, tenant_id, contract_id, "output")
    if not rules:
        return {"introduced": []}
    try:
        from assembler import fetch_policies
        policies = fetch_policies(s, policy_ids)
        base_records = _build_tagged_records(structure, policies)
        edited = copy.deepcopy(policies)
        for r in resolved_edits:
            _apply_canonical_edit(edited, r["policy_id"], r["table"], r["target_id"],
                                  r["column"], r["new_typed"], r["is_scalar"])
        mod_records = _build_tagged_records(structure, edited)
        # JS engine removed: both sides are clean, so no edit is ever flagged as
        # introducing a custom/ajv violation here.
        base = _skip_validation(base_records, rules, ["custom", "ajv"], tenant_id, contract_id)
        after = _skip_validation(mod_records, rules, ["custom", "ajv"], tenant_id, contract_id)
    except Exception as e:  # noqa: BLE001
        return {"error": f"validation service error: {e}"}
    base_keys = _violation_keys(base)
    after_items = (after.get("exceptions", {}) or {}).get("items", []) or []
    introduced = [
        {"rule_id": it.get("rule_id"), "field_path": it.get("field_path"),
         "severity": it.get("severity"), "actual_value": it.get("actual_value"),
         "expected_value": it.get("expected_value")}
        for it in after_items
        if (it.get("rule_id"), it.get("source_entity_id"), it.get("field_path")) not in base_keys
    ]
    return {"introduced": introduced}

def _derive_recommendation(expected, rule_spec):
    """Best-effort human recommendation for a rule's target field.

    Fallback chain (the concrete value, never invented):
      1. expected_value (+ operator)        — what the engine already computed
      2. value / min / max / enum / schema  — parsed from the rule_spec
      3. None                               — no concrete value (UI shows a hint)
    """
    op = ""
    if isinstance(rule_spec, str):
        try:
            rule_spec = json.loads(rule_spec)
        except Exception:
            rule_spec = None
    if isinstance(rule_spec, dict):
        op = str(rule_spec.get("operator") or "")

    # 1. concrete expected value already on the exception
    if expected is not None and str(expected).strip() != "":
        return f"{op} {expected}".strip()

    if not isinstance(rule_spec, dict):
        return None

    # 2a. scalar bound
    for k in ("value", "max_value", "maximum", "min_value", "minimum"):
        v = rule_spec.get(k)
        if v is not None:
            sym = op or ("≤" if "max" in k else "≥" if "min" in k else "")
            return f"{sym} {v}".strip()

    # 2b. enum
    enum = rule_spec.get("enum")
    if isinstance(enum, list) and enum:
        shown = ", ".join(str(x) for x in enum[:8])
        return shown + (" …" if len(enum) > 8 else "")

    # 2c. nested JSON-Schema (AJV) properties
    props = rule_spec.get("properties")
    if isinstance(props, dict):
        for pv in props.values():
            if isinstance(pv, dict):
                if pv.get("maximum") is not None:
                    return f"≤ {pv['maximum']}"
                if pv.get("minimum") is not None:
                    return f"≥ {pv['minimum']}"
                pe = pv.get("enum")
                if isinstance(pe, list) and pe:
                    return "one of: " + ", ".join(str(x) for x in pe[:8])
    return None

_DECISION_STATUS = {
    "approve": "approved",
    "fix":     "fixed",
    "dismiss": "dismissed",
    "reject":  "rejected",
}

class DecisionItem(BaseModel):
    exception_id: int
    kind: str                      # approve | fix | dismiss | reject
    value: Optional[str] = None    # the corrected value (fix)
    reason: Optional[str] = None   # reviewer reason / note

class DecideRequest(BaseModel):
    decisions: list[DecisionItem]
    user_id: Optional[int] = None

def _decision_note(kind: str, value: Optional[str], reason: Optional[str]) -> str:
    """Human-readable resolution_note for each decision kind."""
    reason = (reason or "").strip()
    if kind == "approve":
        if value not in (None, ""):
            base = f"Approved with value: {value}"
            return f"{base} — {reason}" if reason else base
        return reason or "Approved — matches recommendation"
    if kind == "fix":
        base = f"Fixed with user value: {value}" if value not in (None, "") else "Fixed"
        return f"{base} — {reason}" if reason else base
    if kind == "dismiss":
        return reason or "Dismissed — kept as-is"
    if kind == "reject":
        return reason or "Rejected — excluded from output"
    return reason

class ExportDecisionItem(BaseModel):
    rule_id: Optional[int] = None
    policy_number: Optional[str] = None
    field: Optional[str] = None
    kind: str                       # approve | fix | dismiss | reject
    value: Optional[str] = None     # corrected value (fix) / resolved value (approve)
    reason: Optional[str] = None
    actual_value: Optional[str] = None
    sheet: Optional[str] = None     # output sheet (direct-lane decision key)
    row: Optional[int] = None       # 1-based output row (direct-lane decision key)

class ExportDecideRequest(BaseModel):
    decisions: list[ExportDecisionItem]
    user_id: Optional[int] = None
    apply: bool = True

def _decide_direct_lane(s, landing_id: int, body: ExportDecideRequest) -> dict:
    """Persist decisions for a DIRECT-LANE export (output projected from
    landing_record.data, not canonical). Each decision is upserted into
    landing_correction keyed by (landing_id, sheet, row, field); Fix/Approve with
    a value also resolves + stores the source-cell override (applied at render;
    raw landing.data is never mutated). No canonical write-back."""
    import direct_lane as dl

    rec = s.execute(
        text("SELECT id, tenant_id, format_id, data FROM landing_record WHERE id = :i"),
        {"i": landing_id},
    ).mappings().first()
    if not rec:
        raise HTTPException(404, "landing record not found")
    fmt = None
    if rec["format_id"]:
        fmt = s.execute(
            text("SELECT sheet_routing, column_mapping FROM direct_format WHERE id = :f"),
            {"f": rec["format_id"]},
        ).mappings().first()

    def _as_dict(v):
        return v if isinstance(v, dict) else (json.loads(v) if v else {})

    routing = _as_dict(fmt and fmt["sheet_routing"])
    column_mapping = _as_dict(fmt and fmt["column_mapping"])
    landing_data = _as_dict(rec["data"])
    tenant_id = rec["tenant_id"]
    decided_by = str(body.user_id) if body.user_id is not None else None

    updated, skipped = 0, []
    for d in body.decisions:
        kind = (d.kind or "").lower()
        if kind not in _DECISION_STATUS:
            skipped.append({"field": d.field, "reason": f"unknown kind '{d.kind}'"})
            continue
        if not d.sheet or d.row is None or not d.field:
            skipped.append({"field": d.field,
                            "reason": "missing sheet/row/field coordinates"})
            continue
        in_sheet = in_idx = source_col = old_val = new_val = None
        if kind in ("fix", "approve") and (d.value or "").strip():
            new_val = (d.value or "").strip()
            r = dl.resolve_landing_cell(
                landing_data, routing, column_mapping,
                d.sheet, d.row, d.field, expect_value=d.actual_value)
            if r.get("ok"):
                in_sheet, in_idx = r["input_sheet"], r["input_row_index"]
                source_col = r["source_column"]
                old_val = None if r.get("current_value") is None else str(r["current_value"])
            else:
                # No single writable input cell — the column is computed (const,
                # arithmetic transform, source_sheet) or unmapped. Store the fix
                # anyway, keyed by the output coordinates it already carries, and
                # leave the input coords NULL: the renderer applies it as an
                # output-level override after projection. Dropping it here is what
                # made a fix silently disappear from the re-rendered BDX.
                old_val = None if d.actual_value is None else str(d.actual_value)
        note = _decision_note(d.kind, d.value, d.reason)
        s.execute(
            text(
                "INSERT INTO landing_correction "
                "(tenant_id, landing_id, output_sheet, output_row, output_field, "
                " rule_id, policy_number, kind, reason, input_sheet, input_row_index, "
                " source_column, old_value, new_value, decided_by, decided_at) "
                "VALUES (:t,:lid,:sh,:rw,:fld,:rid,:pn,:kind,:reason,:ish,:iidx,:scol,"
                " :old,:new,:by, now()) "
                "ON CONFLICT (landing_id, output_sheet, output_row, output_field) "
                "DO UPDATE SET kind=:kind, reason=:reason, input_sheet=:ish, "
                " input_row_index=:iidx, source_column=:scol, old_value=:old, "
                " new_value=:new, rule_id=:rid, policy_number=:pn, decided_by=:by, "
                " decided_at=now()"
            ),
            {"t": tenant_id, "lid": landing_id, "sh": d.sheet, "rw": d.row,
             "fld": d.field, "rid": d.rule_id, "pn": d.policy_number, "kind": kind,
             "reason": note, "ish": in_sheet, "iidx": in_idx, "scol": source_col,
             "old": old_val, "new": new_val, "by": decided_by},
        )
        updated += 1
    s.commit()
    return {"ok": True, "updated": updated, "skipped": skipped,
            "writeback": None, "lane": "direct"}
