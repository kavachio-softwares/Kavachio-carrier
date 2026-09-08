"""Validation orchestration.

Flow:  Frontend -> POST /api/validate (here)
       -> store validation_run + validation_exception -> return result.

The legacy JS validation service (global/custom/ajv engines on port 4000) has
been REMOVED. Those rule types carry no compiled SQL, so they are not evaluated
here — /api/validate records a CLEAN run (zero exceptions) and proceeds. Contract
`ir_v1` rules are validated separately by the DuckDB engine (see /export/validate
in main.py). `_skip_validation` is the no-op stand-in for the old JS call.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from db import SessionLocal
from app_routes import resolve_tenant_id, assert_tenant_owns
from auth_deps import current_principal, Principal

log = logging.getLogger("bdx.validation")
router = APIRouter()


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


# @router.post("/api/validate")
# def validate(body: ValidateBody):
#     engines = body.engines or ["global", "custom","ajv"]
#     stage = body.stage or "input"

#     # 1. DB: the upload's policies + the rules for the given tenant + contract.
#     #    tenantId / contractId come from the payload (they select WHICH rules
#     #    apply — upload-wide/global + tenant + contract); if omitted they're
#     #    derived from the upload's policies as a fallback.
#     with SessionLocal() as s:
#         records = _fetch_policies(s, body.uploadId)
#         print("-----records----",records)
#         # The frontend typically sends only uploadId; derive tenant + contract
#         # from the upload's policies (first non-null of each) when not given.
#         tenant_id = body.tenantId
#         print("-----tenant_id from body----",tenant_id)                                                             
#         contract_id = body.contractId
#         for r in records:
#             if contract_id is None and r.get("contract_id") is not None:
#                 contract_id = r.get("contract_id")
#             if tenant_id is not None and contract_id is not None:
#                 break
#         rules = _fetch_rules(s, tenant_id, contract_id, stage)
#         print("-----rules----",rules)

#     # 2. One stateless JS call — all engines, all the scoped rules.
#     try:
#         result = _call_js(records, rules, engines, tenant_id, contract_id)
#     except Exception as e:  # noqa: BLE001
#         log.exception("JS validation call failed")
#         raise HTTPException(502, f"validation service error: {e}")

#     # 3. Persist run + exceptions (input stage, best-effort)
#     run_id = None
#     persisted = False
#     persist_error = None
#     # Persist BOTH input- and output-stage runs so the exceptions survive a
#     # reload (the Output Delivery screen reads them back via /api/validate/upload).
#     if tenant_id is not None:
#         with SessionLocal() as s:
#             try:
#                 run_id = _persist(s, body, tenant_id, contract_id, result)
#                 s.commit()
#                 persisted = True
#             except Exception as e:  # noqa: BLE001
#                 s.rollback()
#                 persist_error = str(e)
#                 log.warning("validation persistence failed: %s", e)

#     # 4. Return result to the frontend
#     return {
#         "success": True,
#         "uploadId": body.uploadId,
#         "contractId": contract_id,
#         "tenantId": tenant_id,
#         "stage": stage,
#         "runId": run_id,
#         "persisted": persisted,
#         "persistError": persist_error,
#         "recordCount": result.get("recordCount", 0),
#         "rules": result.get("rules", {}),
#         "engines": result.get("engines", {}),
#         "exceptions": {
#             k: v for k, v in (result.get("exceptions", {}) or {}).items() if k != "items"
#         },
#         "proceedToCanonical": result.get("proceedToCanonical", True),
#     }

def _as_of_or(s, contract_id: Optional[int], on_date) -> Optional[int]:
    """Feature 7 §7.2 — swap a "latest wins" contract for the version that was
    IN FORCE on `on_date`.

    Wrapped around the existing resolvers rather than folded into them: their
    job is "which contract governs this template", which is unchanged and still
    correct. This adds the one thing §7 needs on top — "and which VERSION of it
    governs this date" — without touching a line of the logic that finds the
    contract in the first place.

    Returns `contract_id` untouched whenever as-of resolution cannot improve on
    it (flag off, no date, migration 20_1 not applied, or no sibling version
    covering the date), so every caller's pre-Feature-7 behaviour is preserved
    exactly.
    """
    if contract_id is None or on_date is None:
        return contract_id
    try:
        from contract_upload_services import contract_asof as _asof
        return _asof.resolve_sibling_as_of(s, contract_id, on_date) or contract_id
    except Exception:  # noqa: BLE001 — fail-open: never break validation over this
        return contract_id


def _resolve_contract_for_validation(s, template_id: int, tenant_id,
                                     on_date=None) -> Optional[int]:
    """Find the contract whose rules govern an output template — then, when a
    transaction date is supplied, the VERSION of it in force on that date
    (Feature 7 §7.1). `on_date=None` keeps the historical behaviour exactly."""
    return _as_of_or(s, _resolve_contract_latest(s, template_id, tenant_id), on_date)


def _resolve_contract_latest(s, template_id: int, tenant_id) -> Optional[int]:
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


@router.post("/api/validate")
def validate(body: ValidateBody, principal: Principal = Depends(current_principal)):
    engines = body.engines or ["global", "custom", "ajv"]
    stage = body.stage or "input"

    # 1. Fetch the upload's policies, then derive tenant + contract FROM THE
    #    POLICIES — they are the source of truth for what data is being
    #    validated, so the rules get scoped correctly.
    #    NOTE: body.tenantId is intentionally NOT used; the frontend sends the
    #    app user id, not the canonical tenant_id, which broke rule scoping
    #    (only global rules matched).
    with SessionLocal() as s:
        records = _fetch_policies(s, body.uploadId)
        print("-----BDX records----", records)
        # tenant_id from policies (authoritative)
        tenant_id = next(
            (r.get("tenant_id") for r in records if r.get("tenant_id") is not None),
            None,
        )
        # Ownership guard: the upload's data must belong to the caller's tenant
        # (platform admin bypasses). Blocks validating another tenant's upload.
        if tenant_id is not None:
            assert_tenant_owns(principal, tenant_id)
        # contract_id: honour an explicit body value, else derive from policies
        contract_id = body.contractId
        print("-----contract_id----", contract_id)
        if contract_id is None:
            contract_id = next(
                (r.get("contract_id") for r in records if r.get("contract_id") is not None),
                None,
            )
        # Still none (e.g. a new output template not linked to a contract, and the
        # policies carry no contract_id): resolve the governing contract from the
        # template, so the output stage runs the CONTRACT rules and surfaces
        # violations (otherwise only global rules run → no popup).
        if contract_id is None and stage == "output" and body.templateId:
            contract_id = _resolve_contract_for_validation(s, int(body.templateId), tenant_id)
            print("-----resolved contract via template/columns----", contract_id)

        print("-----resolved tenant_id / contract_id----", tenant_id, contract_id)
        rules = _fetch_rules(s, tenant_id, contract_id, stage)
        print(
            "-----rules fetched (by engine)----",
            {e: sum(1 for x in rules if x.get("rule_engine") == e)
             for e in ("ajv", "custom", "global")},
        )

        # The records to validate. For the OUTPUT stage we validate the resolved
        # output rows (keyed by the template's column names, the same vocabulary
        # the contract rules use) instead of the raw policy row — otherwise every
        # rule on a coverage/premium field silently passes. Falls back to the
        # policy rows if no template is given or the build fails.
        validation_records = records
        if stage == "output" and body.templateId:
            try:
                out_records = _fetch_output_records(s, body.uploadId, int(body.templateId))
                if out_records:
                    validation_records = out_records
                    print(f"-----validating {len(out_records)} OUTPUT rows "
                          f"(template {body.templateId})-----")
                else:
                    log.warning("output-stage validation: no output rows built for "
                                "upload %s template %s; using policy rows",
                                body.uploadId, body.templateId)
            except Exception as e:  # noqa: BLE001
                log.warning("output-record build failed (%s); using policy rows", e)

    # 2. JS engine removed — record a clean run and proceed (no evaluation).
    result = _skip_validation(validation_records, rules, engines, tenant_id, contract_id)

    # 3. Persist run + exceptions (best-effort; both input and output stages so
    #    they survive a reload via /api/validate/upload).
    run_id = None
    persisted = False
    persist_error = None
    if tenant_id is not None:
        with SessionLocal() as s:
            try:
                run_id = _persist(s, body, tenant_id, contract_id, result)
                s.commit()
                persisted = True
            except Exception as e:  # noqa: BLE001
                s.rollback()
                persist_error = str(e)
                log.warning("validation persistence failed: %s", e)

    # Audit the validation run + its exception count (the middleware skips this
    # path since it can't read the response body where the counts live).
    try:
        from audit import log_activity, actor_email
        _exc = result.get("exceptions") or {}
        _items = _exc.get("items")
        _exc_count = len(_items) if isinstance(_items, list) else _exc.get("total", 0)
        log_activity(tenant_id, actor_email(principal.user_id), "validation.run",
                     target=f"upload:{body.uploadId}",
                     details={"stage": stage, "contract_id": contract_id,
                              "run_id": run_id, "records": result.get("recordCount", 0),
                              "exceptions": _exc_count})
    except Exception:  # noqa: BLE001 — auditing must never break the response
        pass
    # 4. Return result to the frontend
    return {
        "success": True,
        "uploadId": body.uploadId,
        "contractId": contract_id,
        "tenantId": tenant_id,
        "stage": stage,
        "runId": run_id,
        "persisted": persisted,
        "persistError": persist_error,
        "recordCount": result.get("recordCount", 0),
        "rules": result.get("rules", {}),
        "engines": result.get("engines", {}),
        "exceptions": {
            k: v for k, v in (result.get("exceptions", {}) or {}).items() if k != "items"
        },
        "proceedToCanonical": result.get("proceedToCanonical", True),
    }

# ---------------------------------------------------------------------------
# SCD-2 "Modify here": resolve a flagged output column to a canonical cell,
# re-validate the proposed value, and RETURN the SQL that versions it.
# This endpoint NEVER writes to the database — the operator runs the returned
# SQL by hand (product decision).
# ---------------------------------------------------------------------------

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


def _resolve_contract_id(s, body, policy_id, on_date=None) -> Optional[int]:
    """Find the contract whose rules govern this edit (so re-validation uses the
    SAME rule set the original output validation did).

    Feature 7: with `on_date` supplied, the result is narrowed to the version in
    force on that date. Note the ordering — an explicit body.contractId still
    wins outright, because an operator naming a contract version has said which
    one they mean and must not be second-guessed by a date lookup."""
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
    # Fall back to the contract that owns this output template. This is the
    # "latest wins" lookup Feature 7 exists to correct, so it is the one that
    # gets the as-of wrap; the exceptionId path above does NOT, because the
    # contract that produced an exception is already the right version to
    # re-validate against, by construction.
    cid = s.execute(
        text("SELECT contract_id FROM contract WHERE output_template_id = :t "
             "ORDER BY contract_id DESC LIMIT 1"),
        {"t": body.templateId},
    ).scalar()
    if cid is not None:
        return _as_of_or(s, cid, on_date)
    # Last resort: the policy's own contract_id (often NULL).
    return _as_of_or(s, s.execute(
        text("SELECT policy_contract_id FROM policy WHERE policy_id = :p"), {"p": policy_id}
    ).scalar(), on_date)


def _scope_for_policy(s, policy_id: int):
    row = s.execute(
        text("SELECT tenant_id, policy_contract_id AS contract_id "
             "FROM policy WHERE policy_id = :p"),
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


@router.post("/api/canonical/field/preview")
def preview_field_edit(body: FieldEditBody,
                       principal: Principal = Depends(current_principal)):
    """Resolve + re-validate a 'Modify here' edit and return the SCD-2 SQL.

    Writes nothing. The response tells the UI whether the field is editable,
    whether the new value would introduce a new exception, and (when clear)
    the exact SQL transaction to run."""
    from scd2_sql import (
        resolve_field, pick_target_row, find_target_in_upload, coerce_value,
        build_scd2_sql, tables_touched, physical_columns, missing_scd_columns,
        pk_is_identity_always,
    )

    with SessionLocal() as s:
        policy_id = body.policyId
        actual_value = body.actualValue
        # Enrich from the stored exception when the client only sent its id.
        if body.exceptionId is not None and (policy_id is None or actual_value is None):
            erow = s.execute(
                text("SELECT source_entity_id, actual_value FROM validation_exception "
                     "WHERE exception_id = :e"),
                {"e": body.exceptionId},
            ).mappings().first()
            if erow:
                if policy_id is None:
                    policy_id = erow["source_entity_id"]
                if actual_value is None:
                    actual_value = erow["actual_value"]

        res = resolve_field(s, body.templateId, body.fieldPath)
        if not res.get("editable"):
            return {"ok": False, "editable": False, "reason": res.get("reason")}

        table, column, transform = res["table"], res["column"], res.get("transform")

        # The one-time DDL (scripts/scd2_modify_here.sql) must have run first —
        # otherwise the generated SQL would reference columns that don't exist.
        missing = missing_scd_columns(s, table)
        if missing:
            return {"ok": False, "editable": True,
                    "reason": f"SCD-2 columns missing on '{table}' ({', '.join(missing)}). "
                              "Run scripts/scd2_modify_here.sql once, then retry."}

        # Locate the exact row to version. Per-row exceptions carry a policy_id;
        # aggregate/grouped exceptions (e.g. the Aggregate Limit cap) carry none,
        # so find the row in the upload whose value matches the flagged value.
        if policy_id is not None:
            target = pick_target_row(s, table, int(policy_id), column, transform, actual_value)
        else:
            target = find_target_in_upload(
                s, body.uploadId, table, column, transform, actual_value)
        if not target.get("found"):
            return {"ok": False, "editable": True, "reason": target.get("reason")}

        resolved_pid = int(target.get("policy_id") or policy_id)
        # Ownership guard: the policy being edited must belong to the caller's
        # tenant (platform admin bypasses) — this route can WRITE canonical SCD-2.
        _owner_tenant, _ = _scope_for_policy(s, resolved_pid)
        assert_tenant_owns(principal, _owner_tenant)
        new_typed = coerce_value(table, column, body.newValue)
        contract_id = _resolve_contract_id(s, body, resolved_pid)

        verdict = _revalidate_edit(
            s, body.uploadId, body.templateId, resolved_pid,
            body.fieldPath, table, target["target_id"], column,
            new_typed, res["is_scalar"], contract_id_override=contract_id,
        )
        if verdict.get("introduced"):
            return {
                "ok": False, "editable": True,
                "reason": "this value would introduce a new validation exception",
                "introducedExceptions": verdict["introduced"],
                "currentValue": _jsonable(target.get("current_value")),
            }

        sql = build_scd2_sql(
            table=table, pk_col=target["pk_col"], target_id=target["target_id"],
            edits={column: new_typed},
            exception_ids=([body.exceptionId] if body.exceptionId is not None else []),
            physical_cols=physical_columns(s, table),
            id_identity=pk_is_identity_always(s, table),
        )

        # "Save" — execute the SCD-2 transaction now (re-validation already passed).
        applied = False
        apply_error = None
        if body.apply:
            from db import engine
            raw = engine.raw_connection()
            try:
                raw.autocommit = True            # the SQL is its own BEGIN..COMMIT
                raw.cursor().execute(sql)
                applied = True
            except Exception as e:  # noqa: BLE001
                apply_error = str(e)
            finally:
                raw.close()
            if not applied:
                return {"ok": False, "editable": True,
                        "reason": f"save failed: {apply_error}", "sql": sql}

        return {
            "ok": True, "editable": True, "applied": applied,
            "table": table, "column": column, "pkCol": target["pk_col"],
            "targetId": target["target_id"],
            "currentValue": _jsonable(target.get("current_value")),
            "newValue": _jsonable(new_typed),
            "ambiguous": target.get("ambiguous", False),
            "targetStillViolates": verdict.get("targetStillViolates", False),
            "revalidationSkipped": bool(verdict.get("error")),
            "revalidationNote": verdict.get("error"),
            "tablesTouched": tables_touched(table, body.exceptionId),
            "sql": sql,
        }


# ---------------------------------------------------------------------------
# Batch "Save": one Save button → re-validate ALL edits together → write ONE
# new version per record (several field edits on the same row become a single
# new row, not several).
# ---------------------------------------------------------------------------

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


@router.post("/api/canonical/fields/save")
def save_fields(body: FieldsSaveBody,
                principal: Principal = Depends(current_principal)):
    """Re-validate ALL the entered values together, then (apply=True) write one
    new SCD-2 version per affected record — several edits on the same row become a
    SINGLE new version. Returns per-edit status; writes nothing if any edit would
    introduce a new validation exception."""
    from scd2_sql import (
        resolve_field, pick_target_row, find_target_in_upload, coerce_value,
        build_scd2_sql, physical_columns, missing_scd_columns, pk_is_identity_always,
    )
    from collections import defaultdict

    with SessionLocal() as s:
        results = []        # one per input edit (for UI feedback)
        resolved = []       # the editable, resolved ones
        for idx, ed in enumerate(body.edits):
            row = {"index": idx, "fieldPath": ed.fieldPath, "exceptionId": ed.exceptionId}
            if ed.newValue is None or str(ed.newValue).strip() == "":
                row.update(ok=False, reason="no new value entered")
                results.append(row); continue
            policy_id, actual_value = ed.policyId, ed.actualValue
            if ed.exceptionId is not None and (policy_id is None or actual_value is None):
                erow = s.execute(
                    text("SELECT source_entity_id, actual_value FROM validation_exception "
                         "WHERE exception_id = :e"), {"e": ed.exceptionId},
                ).mappings().first()
                if erow:
                    policy_id = policy_id if policy_id is not None else erow["source_entity_id"]
                    actual_value = actual_value if actual_value is not None else erow["actual_value"]

            res = resolve_field(s, body.templateId, ed.fieldPath)
            if not res.get("editable"):
                row.update(ok=False, editable=False, reason=res.get("reason"))
                results.append(row); continue
            table, column, transform = res["table"], res["column"], res.get("transform")
            if missing_scd_columns(s, table):
                row.update(ok=False, reason=f"SCD-2 columns missing on '{table}' — run the migration")
                results.append(row); continue
            if policy_id is not None:
                target = pick_target_row(s, table, int(policy_id), column, transform, actual_value)
            else:
                target = find_target_in_upload(s, body.uploadId, table, column, transform, actual_value)
            if not target.get("found"):
                row.update(ok=False, editable=True, reason=target.get("reason"))
                results.append(row); continue
            new_typed = coerce_value(table, column, ed.newValue)
            r = {"index": idx, "fieldPath": ed.fieldPath, "exceptionId": ed.exceptionId,
                 "table": table, "column": column, "is_scalar": res["is_scalar"],
                 "target_id": target["target_id"], "pk_col": target["pk_col"],
                 "policy_id": int(target.get("policy_id") or policy_id), "new_typed": new_typed,
                 "ambiguous": target.get("ambiguous", False)}
            resolved.append(r)
            row.update(ok=True, editable=True, table=table, column=column,
                       targetId=target["target_id"], ambiguous=r["ambiguous"])
            results.append(row)

        if not resolved:
            return {"ok": False, "applied": False, "results": results,
                    "reason": "nothing editable to save"}

        # Ownership guard: the record(s) being versioned must belong to the
        # caller's tenant (platform admin bypasses) — this route WRITES canonical
        # SCD-2. All edits in a batch share the upload, so the first resolved
        # policy's tenant is authoritative.
        _owner_tenant, _ = _scope_for_policy(s, resolved[0]["policy_id"])
        assert_tenant_owns(principal, _owner_tenant)

        # Re-validate ALL edits together (the user asked: validate again on save).
        sample_pid = resolved[0]["policy_id"]
        contract_id = body.contractId
        if contract_id is None and resolved[0]["exceptionId"] is not None:
            contract_id = s.execute(
                text("SELECT vr.contract_id FROM validation_exception e "
                     "JOIN validation_rule vr ON vr.rule_id = e.rule_id WHERE e.exception_id = :e"),
                {"e": resolved[0]["exceptionId"]},
            ).scalar()
        if contract_id is None:
            tenant_id = next((p.get("tenant_id") for p in _fetch_policies(s, body.uploadId)
                              if p.get("tenant_id") is not None), None)
            contract_id = _resolve_contract_for_validation(s, int(body.templateId), tenant_id)

        verdict = _revalidate_edits(s, body.uploadId, body.templateId, resolved, contract_id)
        if verdict.get("introduced"):
            return {"ok": False, "applied": False, "results": results,
                    "introducedExceptions": verdict["introduced"],
                    "reason": "these values would introduce new validation exception(s) — nothing saved"}

        # Group by record (table, id) → ONE new version per record.
        groups: dict = defaultdict(lambda: {"edits": {}, "exception_ids": []})
        for r in resolved:
            g = groups[(r["table"], r["target_id"])]
            g["table"], g["target_id"], g["pk_col"] = r["table"], r["target_id"], r["pk_col"]
            g["edits"][r["column"]] = r["new_typed"]
            if r["exceptionId"] is not None:
                g["exception_ids"].append(r["exceptionId"])

        sqls = [
            build_scd2_sql(
                table=g["table"], pk_col=g["pk_col"], target_id=g["target_id"],
                edits=g["edits"], exception_ids=g["exception_ids"],
                physical_cols=physical_columns(s, g["table"]),
                id_identity=pk_is_identity_always(s, g["table"]),
            )
            for g in groups.values()
        ]

        applied = False
        if body.apply:
            from db import engine
            raw = engine.raw_connection()
            try:
                cur = raw.cursor()
                for sql in sqls:  # all records in ONE transaction (all-or-nothing)
                    inner = "\n".join(l for l in sql.splitlines()
                                      if l.strip() not in ("BEGIN;", "COMMIT;"))
                    cur.execute(inner)
                raw.commit()
                applied = True
            except Exception as e:  # noqa: BLE001
                raw.rollback()
                return {"ok": False, "applied": False, "results": results,
                        "reason": f"save failed: {e}", "sql": "\n\n".join(sqls)}
            finally:
                raw.close()

        try:
            from audit import log_activity, actor_email
            log_activity(_owner_tenant, actor_email(principal.user_id), "canonical_fields_saved",
                         target=f"upload:{body.uploadId}",
                         details={"uploadId": body.uploadId, "templateId": body.templateId,
                                  "recordsVersioned": len(groups), "edits": len(body.edits)})
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "applied": applied, "recordsVersioned": len(groups),
                "results": results, "sql": "\n\n".join(sqls)}


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


@router.get("/api/validate/upload/{upload_id}")
def upload_exceptions(upload_id: int,
                      principal: Principal = Depends(current_principal)):
    """Latest stored validation run + exceptions for an upload (read-only)."""
    with SessionLocal() as s:
        # Ownership guard (by-id / IDOR-prone): the Upload must belong to the
        # caller's tenant (platform admin bypasses). upload.tenant_id is the
        # owning tenant. Only asserted when the upload exists, so a missing id
        # still falls through to the "not validated" response below.
        _up_tenant = s.execute(
            text("SELECT tenant_id FROM upload WHERE upload_id = :u"),
            {"u": upload_id},
        ).scalar()
        if _up_tenant is None:
            raise HTTPException(404, "upload not found")
        assert_tenant_owns(principal, _up_tenant)

        run = s.execute(
            text(
                """
                SELECT run_id, tenant_id, bdx_upload_id, contract_id, validation_stage,
                       rules_evaluated, ajv_rules_count, custom_rules_count,
                       violations_count, critical_count, warning_count, info_count,
                       rows_validated, proceeded_to_canonical, status,
                       started_at, completed_at, duration_ms
                FROM validation_run
                WHERE bdx_upload_id = :u
                ORDER BY run_id DESC
                LIMIT 1
                """
            ),
            {"u": upload_id},
        ).mappings().first()

        if not run:
            return {"success": True, "uploadId": upload_id, "validated": False,
                    "run": None, "exceptions": []}

        exc = s.execute(
            text(
                """
                SELECT e.exception_id, e.rule_id, e.source_entity, e.source_entity_id,
                       e.severity, e.field_path, e.expected_value, e.actual_value,
                       e.status, e.resolution_note, e.created_at,
                       p.policy_number, p.external_policy_number, p.certificate_number,
                       -- rule enrichment
                       vr.rule_name,
                       vr.error_message,
                       vr.rule_spec,
                       vr.rule_description,
                       vr.generation_confidence,
                       vr.source_verbatim_text  AS contract_clause_text,
                       vr.source_page_number    AS contract_clause_page,
                       vr.contract_id           AS rule_contract_id,
                       -- contract filename for download
                       c.filename               AS contract_filename
                FROM validation_exception e
                LEFT JOIN policy p
                  ON e.source_entity = 'policy' AND p.policy_id = e.source_entity_id
                  -- SCD-2 makes policy_id non-unique (one row per version); without
                  -- this filter the join multiplies every exception per version.
                  AND p.is_current_version IS NOT FALSE
                LEFT JOIN validation_rule vr
                  ON e.rule_id = vr.rule_id
                LEFT JOIN contract c
                  ON vr.contract_id = c.contract_id
                WHERE e.validation_run_id = :r
                ORDER BY e.severity DESC, e.rule_id NULLS LAST, e.source_entity_id NULLS FIRST
                """
            ),
            {"r": run["run_id"]},
        ).mappings().all()

        # Fetch upload metadata + mapper spec (for source column reverse-mapping)
        upload_info = s.execute(
            text("""
                SELECT u.source_file, u.tenant_id, t.tenant_name AS mga, u.mapper_id,
                      u.source_blob IS NOT NULL AS has_source_blob,
                      m.spec_by_sheet, m.source_filename AS mapper_filename
                FROM upload u
                LEFT JOIN mappers m ON m.id = u.mapper_id
                LEFT JOIN tenant t ON t.tenant_id = u.tenant_id
                WHERE u.upload_id = :u
            """),
            {"u": upload_id},
        ).mappings().first()

        # Output template the run's contract is bound to — lets the decision table
        # write Fix/Approve values back via /api/canonical/fields/save (needs a
        # templateId). Derived from contract.output_template_id; may be None.
        output_template_id = None
        _ctid = run["contract_id"] if run else None
        if _ctid:
            _ct = s.execute(
                text("SELECT output_template_id FROM contract WHERE contract_id = :c"),
                {"c": _ctid},
            ).mappings().first()
            output_template_id = _ct["output_template_id"] if _ct else None

    mapper_spec = upload_info["spec_by_sheet"] if upload_info else None

    # --- Additive, best-effort classification (root_cause / review_reason) -----
    # Tags each exception so the UI can distinguish a genuine data violation from
    # a SYSTEM gap (target field unmapped / rule has no threshold). This is purely
    # additive: it only adds two keys, never alters existing fields, never changes
    # which exceptions fire/persist, and is fully wrapped so it can never raise.
    #
    # Conservative on purpose:
    #   * mapping_gap is flagged ONLY when the field is unmapped AND the actual
    #     value is empty (the false-critical pattern, e.g. "100% policy Limit").
    #     A populated actual means the field DID resolve to data, so it is not a
    #     mapping gap (it may still be a rule/scope issue — left as data_violation).
    #   * rule_incomplete is flagged when the rule produced no expected value.
    try:
        from contract_upload_services.expected_value_resolver import check_mapping_coverage
    except Exception:
        check_mapping_coverage = None

    # Comprehensive recommendation derivation for contract (ir_v1) + legacy AJV
    # rule specs — covers the IR template catalog, which the simple
    # _derive_recommendation parser does not. Lazy import to avoid a circular
    # import with main.py (which mounts this router).
    try:
        from main import _expected_from_ir, _options_from_ir, _example_from_ir
    except Exception:
        _expected_from_ir = None
        _options_from_ir = None
        _example_from_ir = None

    exceptions_out = []
    for _r in exc:
        e = dict(_r)
        # Rule-generation confidence (0..1) — how sure the AI was when it created
        # this rule from the contract. Surfaced under the recommendation so a
        # reviewer can weigh a low-confidence rule before approving. Decimal from
        # PG → float for JSON.
        _conf = e.pop("generation_confidence", None)
        e["confidence"] = float(_conf) if _conf is not None else None
        cause, reason = "data_violation", None
        try:
            field = e.get("field_path")
            actual = e.get("actual_value")
            expected = e.get("expected_value")
            empty_actual = actual is None or str(actual).strip() == ""
            empty_expected = expected is None or str(expected).strip() == ""
            if field and mapper_spec and check_mapping_coverage is not None and empty_actual:
                cov = check_mapping_coverage(field, mapper_spec)
                if not cov.get("ok"):
                    cause, reason = "mapping_gap", cov.get("reason")
            if reason is None and empty_expected:
                cause, reason = "rule_incomplete", "NO_EXPECTED"
        except Exception:
            cause, reason = "data_violation", None
        e["root_cause"] = cause          # data_violation | mapping_gap | rule_incomplete
        e["review_reason"] = reason      # REVIEW_REASONS key or None
        # Recommendation derived from expected_value / rule_spec (rule_spec is not
        # exposed in the response — popped here after deriving).
        rule_spec = e.pop("rule_spec", None)
        rec = _derive_recommendation(e.get("expected_value"), rule_spec)
        if not rec and _expected_from_ir is not None:
            try:
                rec = _expected_from_ir(rule_spec)
            except Exception:
                rec = None
        e["recommendation"] = rec
        # Structured enum options (comma-safe) so the UI can render each allowed
        # value as its own choice without splitting the "one of: …" string.
        if _options_from_ir is not None:
            try:
                opts = _options_from_ir(rule_spec)
            except Exception:
                opts = None
            if opts:
                e["recommendation_options"] = opts
        # A format rule constrains the SHAPE of the value, so it has no value to
        # recommend — send one example of that shape (and the shape in words)
        # rather than leaving the screen to display the rule's raw pattern.
        if _example_from_ir is not None:
            try:
                ex = _example_from_ir(rule_spec)
            except Exception:
                ex = None
            if ex:
                e["recommendation_example"] = ex.get("example")
                e["recommendation_format"] = ex.get("format")
        # Plain-English explanation of the rule, derived from the IR that
        # actually compiled and ran — so it can never drift from the check the
        # way the stored prose has. rule_description is popped alongside
        # rule_spec: it is only the legacy fallback wording, not for display.
        _desc = e.pop("rule_description", None)
        try:
            from contract_upload_services.rule_explainer import explain_rule
            exp = explain_rule(
                rule_spec=rule_spec,
                source_verbatim_text=e.get("contract_clause_text"),
                source_page_number=e.get("contract_clause_page"),
                contract_filename=e.get("contract_filename"),
                rule_description=_desc,
            )
            if exp:
                e["explanation"] = exp
        except Exception:
            pass
        # Same re-titling as the output screen (one shared implementation): a row
        # flagged only because its cell is not a number gets the heading,
        # explanation and how-to-fix of THAT problem, not of the rule whose
        # comparison could not run on it.
        try:
            from contract_upload_services.rule_explainer import (
                apply_numeric_format_identity)
            apply_numeric_format_identity(e, rule_spec)
        except Exception:
            pass
        exceptions_out.append(e)

    return {
        "success": True,
        "uploadId": upload_id,
        "validated": True,
        "run": dict(run),
        "exceptions": exceptions_out,
        "source_file": upload_info["source_file"] if upload_info else None,
        "tenant_id": upload_info["tenant_id"] if upload_info else None,
        "mga": upload_info["mga"] if upload_info else None,
        "mapper_id": upload_info["mapper_id"] if upload_info else None,
        "has_source_blob": bool(upload_info["has_source_blob"]) if upload_info else False,
        # spec_by_sheet: {sheet: {canonical_field: "Sheet :: Column"}}
        # Frontend uses this to reverse-map exception.field_path → source sheet+column
        "mapper_spec": mapper_spec,
        # Output template bound to the contract — used for Fix/Approve write-back.
        "output_template_id": output_template_id,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Persist exception decisions (Screen B "Save decisions").
#
# Matches the frontend ExceptionDecisionTable's Decision shape exactly:
#   { kind: approve | fix | dismiss | reject, value?, reason? }  keyed by exception_id.
#
# Reuses existing validation_exception columns — NO schema migration:
#   status (free Text)        ← the decision
#   resolution_note (Text)    ← the reason / fixed value
#   resolved_by_user_id, resolved_at, modified_at
#
# This only records the reviewer's decision on the exception; it does NOT write
# the corrected value back into the canonical data — that is a separate flow
# (/api/canonical/field/preview + /fields/save, the SCD-2 inline edit).
# ─────────────────────────────────────────────────────────────────────────────
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


@router.post("/api/validate/exceptions/decide")
def decide_exceptions(body: DecideRequest,
                      principal: Principal = Depends(current_principal)):
    """Persist Approve/Fix/Dismiss/Reject decisions onto validation_exception rows.

    Returns {ok, updated, skipped}. Unknown kinds are skipped (never raises)."""
    # Tenant scoping (by-id / IDOR-prone): a regular user may only decide
    # exceptions belonging to their own tenant; the UPDATE is pinned to the
    # token tenant so another tenant's exception_id simply matches no row (it is
    # reported as "not found", never mutated). Platform admins (tid=None) skip
    # the filter and may act on any tenant.
    tid_filter = None if principal.is_platform_admin else principal.tenant_id
    updated, skipped = 0, []
    with SessionLocal() as s:
        for d in body.decisions:
            status = _DECISION_STATUS.get((d.kind or "").lower())
            if not status:
                skipped.append({"exception_id": d.exception_id, "reason": f"unknown kind '{d.kind}'"})
                continue
            res = s.execute(
                text(
                    """
                    UPDATE validation_exception
                       SET status              = :status,
                           resolution_note     = :note,
                           resolved_by_user_id = :user,
                           resolved_at         = now(),
                           modified_at         = now()
                     WHERE exception_id = :eid
                       AND (:tid IS NULL OR tenant_id = :tid)
                    """
                ),
                {
                    "status": status,
                    "note": _decision_note(d.kind, d.value, d.reason),
                    "user": body.user_id,
                    "eid": d.exception_id,
                    "tid": tid_filter,
                },
            )
            if res.rowcount:
                updated += res.rowcount
            else:
                skipped.append({"exception_id": d.exception_id, "reason": "not found"})
        s.commit()
    try:
        from audit import log_activity, actor_email
        log_activity(principal.tenant_id, actor_email(principal.user_id), "exception_decided",
                     target=f"exceptions:{updated}",
                     details={"updated": updated, "skipped": len(skipped),
                              "kinds": sorted({(d.kind or '').lower() for d in body.decisions})})
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "updated": updated, "skipped": skipped}


# ── OUTPUT-stage (per-download) decisions ────────────────────────────────────
# Output exceptions live only inside output_exports.exceptions (no backing
# validation_exception rows, no source_upload_id), so the generic /decide +
# /fields/save path — which is keyed on a real exception_id — can't target them.
# This endpoint materialises a validation_exception row per decided
# (rule_id, policy, field), keyed by policy_number resolved against the canonical
# `policy` table, records the decision, and (Fix/Approve with a concrete value)
# writes it back to canonical via the shared SCD-2 save path.
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


@router.post("/export/downloads/{export_id}/decide")
def decide_export_exceptions(export_id: int, body: ExportDecideRequest,
                             principal: Principal = Depends(current_principal)):
    """Persist Approve/Fix/Dismiss/Reject for a download's output-stage exceptions.

    Idempotent: re-deciding the same (rule, policy, field) updates its row instead
    of adding another. Returns {ok, updated, skipped, writeback}."""
    with SessionLocal() as s:
        exp = s.execute(
            text("SELECT id, tenant_id, template_id, source_upload_id, policy_ids, "
                 "program_id, broker_party_id "
                 "FROM output_exports WHERE id = :id"), {"id": export_id},
        ).mappings().first()
        if not exp:
            raise HTTPException(404, "export not found")
        # Ownership guard (by-id / IDOR-prone). Not assert_tenant_owns: a BROKER
        # seat carries no tenant, and the broker that produced a run decides the
        # exceptions on its own file. The export's denormalised
        # program_id/broker_party_id are what answer that — see
        # carrier_scope.assert_can_read_export. Platform admin bypasses.
        from types import SimpleNamespace
        from carrier_scope import assert_can_read_export
        assert_can_read_export(s, principal, SimpleNamespace(
            tenant_id=exp["tenant_id"], program_id=exp["program_id"],
            broker_party_id=exp["broker_party_id"]))
        # Direct-lane export? (output built from a landing_record, not canonical)
        landing = s.execute(
            text("SELECT id FROM landing_record WHERE output_export_id = :id "
                 "ORDER BY id DESC LIMIT 1"), {"id": export_id},
        ).scalar()
        if landing is not None:
            _res = _decide_direct_lane(s, int(landing), body)
            try:
                from audit import log_activity, actor_email
                log_activity(exp["tenant_id"], actor_email(principal.user_id), "exception_decided",
                             target=f"export:{export_id}",
                             details={"updated": _res.get("updated"), "skipped": len(_res.get("skipped") or []), "lane": "direct"})
            except Exception:
                pass
            return _res
        template_id, upload_id = exp["template_id"], exp["source_upload_id"]

        # policy_number -> {policy_id, tenant_id}, scoped to THIS export's policies
        # (the export tenant is the MGA, not the policies' source tenant).
        from main import resolve_export_policy_numbers
        pn_map = resolve_export_policy_numbers(
            s, upload_id=upload_id, policy_ids=exp["policy_ids"],
            policy_numbers=[d.policy_number for d in body.decisions])

        updated, skipped, edits = 0, [], []
        for d in body.decisions:
            status = _DECISION_STATUS.get((d.kind or "").lower())
            if not status:
                skipped.append({"policy_number": d.policy_number,
                                "reason": f"unknown kind '{d.kind}'"})
                continue
            hit = pn_map.get((d.policy_number or "").strip())
            if not hit:
                # No unambiguous canonical policy → can't persist or match on reload.
                skipped.append({"policy_number": d.policy_number,
                                "reason": "policy could not be resolved"})
                continue
            pid, row_tenant = hit["policy_id"], hit["tenant_id"]
            note = _decision_note(d.kind, d.value, d.reason)
            # Find-or-create the backing exception row (rule + policy + field).
            eid = None
            if d.rule_id is not None and pid is not None:
                eid = s.execute(
                    text("SELECT exception_id FROM validation_exception "
                         "WHERE rule_id = :r AND source_entity_id = :p "
                         "AND field_path IS NOT DISTINCT FROM :f "
                         "ORDER BY exception_id DESC LIMIT 1"),
                    {"r": d.rule_id, "p": pid, "f": d.field},
                ).scalar()
            if eid is None:
                eid = s.execute(
                    text("INSERT INTO validation_exception "
                         "(tenant_id, rule_id, source_entity, source_entity_id, "
                         " severity, field_path, actual_value, status, "
                         " resolution_note, resolved_by_user_id, resolved_at, "
                         " created_at, modified_at) "
                         "VALUES (:t, :r, 'policy', :p, 'warning', :f, :av, :st, "
                         " :note, :u, now(), now(), now()) RETURNING exception_id"),
                    {"t": row_tenant, "r": d.rule_id, "p": pid, "f": d.field,
                     "av": d.actual_value, "st": status, "note": note,
                     "u": body.user_id},
                ).scalar()
            else:
                s.execute(
                    text("UPDATE validation_exception SET status = :st, "
                         "resolution_note = :note, resolved_by_user_id = :u, "
                         "resolved_at = now(), modified_at = now() "
                         "WHERE exception_id = :e"),
                    {"st": status, "note": note, "u": body.user_id, "e": eid},
                )
            updated += 1
            # Fix/Approve with a concrete value → queue a canonical write-back.
            if (d.kind or "").lower() in ("fix", "approve") \
                    and (d.value or "").strip() and pid is not None and d.field:
                edits.append(FieldEditItem(
                    fieldPath=d.field, newValue=(d.value or "").strip(),
                    policyId=pid, exceptionId=eid, actualValue=d.actual_value))
        s.commit()

    # Write concrete values back through the shared SCD-2 save path (own session).
    writeback = None
    if body.apply and edits and template_id is not None:
        # Internal (non-HTTP) call — forward the authenticated principal so the
        # shared SCD-2 save path keeps its tenant ownership guard.
        writeback = save_fields(FieldsSaveBody(
            uploadId=int(upload_id or 0), templateId=int(template_id),
            contractId=None, edits=edits, apply=True), principal)

    try:
        from audit import log_activity, actor_email
        log_activity(exp["tenant_id"], actor_email(principal.user_id), "exception_decided",
                     target=f"export:{export_id}",
                     details={"updated": updated, "skipped": len(skipped), "lane": "canonical"})
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "updated": updated, "skipped": skipped,
            "writeback": writeback}
