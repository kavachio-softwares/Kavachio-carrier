"""Route handlers owned by validation-service. Extracted from: validation_routes.py."""
from __future__ import annotations
from fastapi import APIRouter, Body
from common_validation_routes import *

router = APIRouter()


@router.post("/internal/validate", tags=["_internal"])
def _internal_validate(payload: dict = Body(...)):
    """Internal boundary: other services POST serialized records+rules here instead
    of importing the DuckDB validation engine. Returns run_validation's result."""
    from duckdb_validation import run_validation
    return run_validation(
        payload.get("records_by_sheet"),
        payload.get("rules") or [],
        contract=payload.get("contract"),
        template_id=payload.get("template_id"),
        schema_cols=payload.get("schema_cols"),
        column_types=payload.get("column_types"),
    )


@router.post("/internal/label-exceptions", tags=["_internal"])
def _internal_label_exceptions(payload: dict = Body(...)):
    """Internal boundary for label_exceptions_with_policy. The engine mutates the
    exceptions list in place; over HTTP we return the labelled list."""
    from duckdb_validation import label_exceptions_with_policy
    exceptions = payload.get("exceptions") or []
    label_exceptions_with_policy(exceptions, payload.get("structure") or {},
                                 payload.get("blocks") or [])
    return exceptions


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
        if _up_tenant is not None:
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
        from common_main import _expected_from_ir, _options_from_ir
    except Exception:
        _expected_from_ir = None
        _options_from_ir = None

    exceptions_out = []
    for _r in exc:
        e = dict(_r)
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
    return {"ok": True, "updated": updated, "skipped": skipped}


@router.post("/export/downloads/{export_id}/decide")
def decide_export_exceptions(export_id: int, body: ExportDecideRequest,
                             principal: Principal = Depends(current_principal)):
    """Persist Approve/Fix/Dismiss/Reject for a download's output-stage exceptions.

    Idempotent: re-deciding the same (rule, policy, field) updates its row instead
    of adding another. Returns {ok, updated, skipped, writeback}."""
    with SessionLocal() as s:
        exp = s.execute(
            text("SELECT id, tenant_id, template_id, source_upload_id, policy_ids "
                 "FROM output_exports WHERE id = :id"), {"id": export_id},
        ).mappings().first()
        if not exp:
            raise HTTPException(404, "export not found")
        # Ownership guard (by-id / IDOR-prone): the export (its owning MGA tenant)
        # must belong to the caller's tenant; platform admin bypasses.
        assert_tenant_owns(principal, exp["tenant_id"])
        # Direct-lane export? (output built from a landing_record, not canonical)
        landing = s.execute(
            text("SELECT id FROM landing_record WHERE output_export_id = :id "
                 "ORDER BY id DESC LIMIT 1"), {"id": export_id},
        ).scalar()
        if landing is not None:
            return _decide_direct_lane(s, int(landing), body)
        template_id, upload_id = exp["template_id"], exp["source_upload_id"]

        # policy_number -> {policy_id, tenant_id}, scoped to THIS export's policies
        # (the export tenant is the MGA, not the policies' source tenant).
        from common_main import resolve_export_policy_numbers
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

    return {"ok": True, "updated": updated, "skipped": skipped,
            "writeback": writeback}
