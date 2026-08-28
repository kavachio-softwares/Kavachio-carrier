"""Route handlers owned by export-service. Extracted from: direct_routes.py, main.py."""
from __future__ import annotations
from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool
from common_direct_routes import *
from common_main import *

import storage  # blob storage abstraction (Azure Blob with DB fallback)

from exporter import generate_workbook, build_output_records, parse_template, propose_template_mapping

router = APIRouter()


@router.get("/export/template/{template_id}/fields")
def export_template_fields(template_id: int,
                           principal: Principal = Depends(current_principal)):
    """Return the flat field list for an output template.
    Used by the contract upload UI and extraction service to scope LLM
    rule generation to the output template's field vocabulary."""
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        return {
            "template_id": t.id,
            "name": t.name,
            "fields": _extract_template_fields(t.structure or {}),
        }


@router.get("/export/template/{template_id}/contract-mapping")
def export_template_contract_mapping(template_id: int,
                                     principal: Principal = Depends(current_principal)):
    """Return the active contract for this output template plus its validation
    rules grouped by output template field name.

    Shape:
      {
        contract: { id, filename, status } | null,
        field_rules: {
          "<output_field_name>": [
            { rule_id, rule_engine, rule_name, rule_description, severity,
              source_clause, error_message, rule_spec }
          ]
        }
      }
    """
    with SessionLocal() as s:
        # Find the active contract whose output_template_id == template_id
        active_contract = (
            s.query(Contract)
            .filter(
                Contract.output_template_id == template_id,
                Contract.status == "active",
            )
            .order_by(Contract.id.desc())
            .first()
        )
        if not active_contract:
            return {"contract": None, "field_rules": {}}
        assert_tenant_owns(principal, active_contract.tenant_id)

        contract_info = {
            "id": active_contract.id,
            "filename": active_contract.filename,
            "status": active_contract.status,
            "output_template_id": active_contract.output_template_id,
        }
        contract_id = active_contract.id

    # Load validation rules from the canonical DB, grouped by output field name
    field_rules: dict = {}
    try:
        with CanonicalSession() as cs:
            rows = cs.execute(
                text(
                    "SELECT rule_id, rule_engine, rule_name, rule_description, "
                    "severity, canonical_target, rule_spec, error_message, "
                    "source_verbatim_text, generation_confidence "
                    "FROM validation_rule "
                    "WHERE contract_id = :cid AND rule_status != 'disabled' "
                    "ORDER BY severity, rule_name"
                ),
                {"cid": contract_id},
            ).mappings().all()

            import json as _json

            for row in rows:
                # Resolve the output field name from canonical_target
                target = row["canonical_target"]
                if isinstance(target, str):
                    try:
                        target = _json.loads(target)
                    except Exception:
                        target = {}
                spec = row["rule_spec"]
                if isinstance(spec, str):
                    try:
                        spec = _json.loads(spec)
                    except Exception:
                        spec = {}

                field_name = (
                    (target or {}).get("output_field")
                    or (target or {}).get("field")
                    or (spec or {}).get("field")
                    or "__unresolved__"
                )

                entry = {
                    "rule_id": row["rule_id"],
                    "rule_engine": row["rule_engine"],
                    "rule_name": row["rule_name"],
                    "rule_description": row["rule_description"],
                    "severity": row["severity"],
                    "source_clause": row["source_verbatim_text"],
                    "error_message": row["error_message"],
                    "rule_spec": spec,
                    "generation_confidence": float(row["generation_confidence"] or 0),
                }
                field_rules.setdefault(field_name, []).append(entry)
    except Exception:
        field_rules = {}

    return {"contract": contract_info, "field_rules": field_rules}


@router.post("/export/template/{template_id}/build-rules")
def export_template_build_rules(template_id: int,
                                principal: Principal = Depends(current_principal)):
    """Pre-compile the active contract's rules into DuckDB SQL (LLM + guard +
    retry), without needing data. Warms the cache and tells the UI which rules
    can run and which "cannot be processed" — before the user generates output.
    """
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        structure = t.structure or {}
        active = (
            s.query(Contract)
            .filter(Contract.output_template_id == template_id,
                    Contract.status == "active")
            .order_by(Contract.id.desc())
            .first()
        )
        if not active:
            return {"contract": None, "rules_ok": 0, "unprocessable": [],
                    "message": "No active contract linked to this template."}
        contract_info = {"id": active.id, "filename": active.filename}

    # Rules from the canonical DB
    with CanonicalSession() as cs:
        rule_rows = cs.execute(
            text(
                "SELECT rule_id, rule_engine, rule_name, rule_description, "
                "severity, canonical_target, rule_spec, error_message "
                "FROM validation_rule "
                "WHERE contract_id = :cid AND rule_status != 'disabled'"
            ),
            {"cid": active.id},
        ).mappings().all()
        rules = [dict(r) for r in rule_rows]

    # Empty records per sheet + full column set from the template, so SQL can be
    # compiled and dry-run without any BDX data.
    schema_cols, empty_records = {}, []
    for sh in (structure.get("sheets") or []):
        sname = sh.get("sheet_name", "")
        cols = [c.get("column_name") for c in (sh.get("columns") or [])
                if c.get("column_name")]
        schema_cols[sname] = cols
        empty_records.append({"sheet": sname, "records": []})

    from clients import validation as _dvc
    dv = _dvc.run_validation(
        empty_records, rules, contract=contract_info,
        template_id=template_id, schema_cols=schema_cols,
    )
    return {
        "contract": contract_info,
        "rules_total": dv["stats"]["rules_total"],
        "rules_ok": dv["stats"]["rules_ok"],
        "unprocessable": dv["unprocessable"],
    }


@router.post("/export/template/generate")
async def export_template_generate(
    mga: str = Form(...),
    name: str = Form(...),
    file: UploadFile = File(...),
    carrier_party_id: Optional[int] = Form(default=None),
    contract_id: Optional[int] = Form(default=None),
    sheets: Optional[list[str]] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    """Upload a sample output BDX. Returns a draft template (with LLM-proposed
    canonical_field per column + row_strategy per sheet) for the user to review.

    `sheets` restricts the template to the sheets the user chose; the generated
    output will then contain only those sheets."""
    file_bytes = await file.read()
    structure = await run_in_threadpool(parse_template, file_bytes, filename=file.filename)
    if not structure["sheets"]:
        raise HTTPException(400, "workbook has no readable sheets")
    if sheets:
        keep = set(sheets)
        structure["sheets"] = [sh for sh in structure["sheets"] if sh.get("sheet_name") in keep]
        if not structure["sheets"]:
            raise HTTPException(400, "none of the selected output sheets were found in the workbook")
    await run_in_threadpool(propose_template_mapping, structure)

    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        # Versioning: a new sample under an existing (tenant, name) is the next
        # version; the first version of a brand-new template auto-activates.
        siblings = (
            s.query(ExportTemplate)
            .filter(ExportTemplate.tenant_id == tid, ExportTemplate.name == name)
            .all()
        )
        version = (max((r.version or 1) for r in siblings) + 1) if siblings else 1
        is_active = 0 if siblings else 1

        # Resolve carrier name for display if a party id was supplied.
        carrier_name = None
        if carrier_party_id is not None:
            p = s.get(Party, carrier_party_id)
            if p:
                carrier_name = p.legal_name

        tmpl_ref, tmpl_bytes = await run_in_threadpool(
            storage.store_or_keep, "templates", tid, file.filename, file_bytes,
        )
        t = ExportTemplate(
            tenant_id=tid, name=name, version=version, is_active=is_active,
            carrier=carrier_name,
            carrier_party_id=carrier_party_id, contract_id=contract_id,
            structure=structure, template_blob=tmpl_bytes,
            template_blob_ref=tmpl_ref, approved=0,
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        return _template_to_dict(t, s)


@router.put("/export/template/{template_id}")
def export_template_update(template_id: int, body: UpdateExportTemplateBody,
                           principal: Principal = Depends(current_principal)):
    """User submits corrected structure (column→canonical_field, row_strategy,
    static_value, transform) and optionally approves."""
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        if body.structure is not None:
            t.structure = body.structure
        if body.name is not None:
            t.name = body.name
        if body.carrier is not None:
            t.carrier = body.carrier
        if body.carrier_party_id is not None:
            t.carrier_party_id = body.carrier_party_id
        if body.contract_id is not None:
            t.contract_id = body.contract_id
        if body.approved is not None:
            t.approved = 1 if body.approved else 0
            # Activating (Save & activate) makes this the live output version.
            if body.approved:
                _activate_template(s, t)
        s.commit()
        s.refresh(t)
        return _template_to_dict(t, s)


@router.get("/export/template")
def export_template_list(mga: Optional[str] = None,
                         principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        q = s.query(ExportTemplate).filter(ExportTemplate.tenant_id == tid)
        return [_template_to_dict(t, s) for t in q.order_by(ExportTemplate.id.desc()).all()]


@router.get("/export/templates")
def export_templates_grouped(mga: Optional[str] = None,
                             principal: Principal = Depends(current_principal)):
    """Output templates grouped into versions, newest first."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        q = s.query(ExportTemplate).filter(ExportTemplate.tenant_id == tid)
        return _group_versions([_template_to_dict(t, s) for t in q.all()])


@router.post("/export/template/{template_id}/activate")
def export_template_activate(template_id: int,
                             principal: Principal = Depends(current_principal)):
    """Make this template version the active one used to generate output."""
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        _activate_template(s, t)
        s.commit()
        s.refresh(t)
        return _template_to_dict(t, s)


@router.get("/export/template/{template_id}")
def export_template_get(template_id: int,
                        principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        return _template_to_dict(t, s)


@router.post("/export/template/{template_id}/refresh")
def export_template_refresh(template_id: int,
                            principal: Principal = Depends(current_principal)):
    """Re-run the AI mapping proposer against the template's stored sample
    workbook, populating the per-column `candidates` list (and the top
    canonical_field assignment). Used to retrofit templates created before
    the ranked-candidates feature shipped — no re-upload needed.

    Robustness:
      - User overrides (transform, static_value) are always preserved.
      - Existing canonical_field / confidence / candidates are preserved
        for any column the LLM doesn't update (e.g. when Gemini errors out
        or returns a partial response), so a 503 doesn't wipe your spec.
    """
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        tpl_bytes = storage.resolve_bytes(t.template_blob_ref, t.template_blob)
        if not tpl_bytes:
            raise HTTPException(
                400,
                "this template has no stored sample workbook; "
                "re-upload via /export/template/generate to refresh",
            )

        old_struct = t.structure or {}
        new_struct = parse_template(tpl_bytes)

        # Index everything from the existing template so we can fall back
        # gracefully if the LLM call fails partway through.
        existing: dict[tuple[str, int], dict] = {}
        for sh in (old_struct.get("sheets") or []):
            for col in (sh.get("columns") or []):
                existing[(sh.get("sheet_name"), col.get("column_index"))] = col
        old_row_strategy = {
            sh.get("sheet_name"): sh.get("row_strategy")
            for sh in (old_struct.get("sheets") or [])
        }

        propose_template_mapping(new_struct)

        any_llm_hit = False
        for sh in new_struct["sheets"]:
            # Restore row_strategy if the LLM didn't set one but we had one.
            if (not sh.get("row_strategy") or sh.get("row_strategy") == "policy") \
                    and old_row_strategy.get(sh["sheet_name"]):
                sh["row_strategy"] = old_row_strategy[sh["sheet_name"]]

            for col in sh["columns"]:
                key = (sh["sheet_name"], col["column_index"])
                prev = existing.get(key) or {}

                # 1. Always preserve user-set transform / static_value.
                if prev.get("transform") is not None:
                    col["transform"] = prev["transform"]
                if prev.get("static_value") is not None:
                    col["static_value"] = prev["static_value"]

                # 2. If the LLM populated this column, use that.
                #    Otherwise fall back to whatever was there before.
                if col.get("candidates"):
                    any_llm_hit = True
                else:
                    if prev.get("canonical_field"):
                        col["canonical_field"] = prev["canonical_field"]
                        col["confidence"] = prev.get("confidence") or 0.0
                    if prev.get("candidates"):
                        col["candidates"] = prev["candidates"]

        t.structure = new_struct
        s.commit()
        s.refresh(t)

        if not any_llm_hit:
            # LLM call failed end-to-end. Tell the caller so the UI can
            # surface "service unavailable, try again later" instead of
            # silently swapping to a half-empty spec.
            raise HTTPException(
                503,
                "AI mapping service is unavailable right now. Your existing "
                "mappings have been preserved — please try again in a minute.",
            )
        return _template_to_dict(t, s)


@router.post("/export/validate")
def export_validate(
    template_id: int = Form(...),
    upload_id: Optional[int] = Form(default=None),
    policy_ids: Optional[str] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    """Pre-generation dry run for the Outputs page.

    Resolves the output rows an export WOULD write and runs the SAME contract-rule
    validation that /export/generate runs (the DuckDB engine, which compiles each
    rule — old AJV *or* new ir_v1 — to SQL and executes it). Builds NO file and
    creates no downloadable export, BUT persists a validation_run + validation_exception
    rows for the upload so the "Modify data here" editor and the exceptions review can
    read them back via /api/validate/upload. The frontend calls this before downloading
    so it can show the "Validation exceptions found" popup. Returns exception counts
    plus a violations list already shaped for the popup (see frontend CustomViolation).
    """
    with SessionLocal() as s:
        tgt = _resolve_export_target(s, template_id, upload_id, policy_ids, principal)

    structure = tgt["structure"]
    ids = tgt["ids"]
    active_contract_id = tgt["active_contract_id"]

    empty = {"exception_count": 0, "critical": 0, "warning": 0,
             "violations": [], "unprocessable_rules": []}
    if not ids or active_contract_id is None:
        return empty

    # Active-contract rules (everything except disabled), same as generate Pass B.
    with CanonicalSession() as cs:
        rule_rows = cs.execute(
            text(
                "SELECT rule_id, rule_engine, rule_name, rule_description, "
                "severity, canonical_target, rule_spec, error_message, "
                "source_verbatim_text, source_page_number "
                "FROM validation_rule "
                "WHERE contract_id = :cid AND rule_status != 'disabled'"
            ),
            {"cid": active_contract_id},
        ).mappings().all()
        contract_validation_rules = [dict(r) for r in rule_rows]

    if not contract_validation_rules:
        return empty

    with CanonicalSession() as cs:
        policies = fetch_policies(cs, ids)

    records = build_output_records(structure, policies)
    schema_cols = {
        sh.get("sheet_name", ""): [
            c.get("column_name") for c in (sh.get("columns") or [])
            if c.get("column_name")
        ]
        for sh in (structure.get("sheets") or [])
    }

    exceptions: list[dict] = []
    unprocessable: list[dict] = []
    try:
        from clients import validation as _dvc
        _dv = _dvc.run_validation(
            records,
            contract_validation_rules,
            contract=tgt["active_contract_info"],
            template_id=template_id,
            schema_cols=schema_cols,
        )
        exceptions = _dv["exceptions"]
        unprocessable = _dv["unprocessable"]
        print(f"[DuckDB validate-only] {_dv['stats']}")
    except Exception as dv_exc:  # never let validation crash block the user
        import traceback as _tb
        _tb.print_exc()
        print(f"[DuckDB validate-only] ERROR — skipped: {dv_exc}")
        return empty

    # Shape each DuckDB exception into the frontend popup's CustomViolation.
    violations = [
        {
            "ruleId": e.get("rule_id"),
            "ruleName": e.get("rule_name") or e.get("code"),
            "severity": (e.get("severity") or "warning").lower(),
            "rowIndex": e.get("row"),
            "field": e.get("field") or e.get("column"),
            "actualValue": e.get("actual_value"),
            "expectedValue": None,
            "operator": None,
            "message": e.get("message") or e.get("reason"),
            "recordIdentifier": (
                {"policy_number": e.get("policy_number")}
                if e.get("policy_number") else None
            ),
            "affectedRecords": None,
            "violationDetail": None,
        }
        for e in exceptions
    ]
    critical = sum(1 for v in violations if v["severity"] == "critical")
    warning = sum(1 for v in violations if v["severity"] in ("warning", "warn"))

    # Persist a validation_run + validation_exception rows for this upload so the
    # "Modify data here" editor and the exceptions review (which read back via
    # /api/validate/upload) have data to work with. field_path is stored as the
    # OUTPUT column name — save_fields/resolve_field map it to the canonical cell at
    # save time. Best-effort: persistence must never break the popup response.
    if upload_id is not None and exceptions:
        try:
            from types import SimpleNamespace
            from common_validation_routes import _persist
            # policy_number -> policy_id (+ a tenant_id) for this upload's policies.
            with CanonicalSession() as cs:
                prows = cs.execute(
                    text("SELECT policy_id, policy_number, tenant_id FROM policy "
                         "WHERE policy_id = ANY(:ids) AND is_current_version IS NOT FALSE"),
                    {"ids": ids},
                ).mappings().all()
            pn_to_pid = {str(r["policy_number"]): r["policy_id"]
                         for r in prows if r["policy_number"] is not None}
            tenant_id = next((r["tenant_id"] for r in prows
                              if r["tenant_id"] is not None), None)

            # Fallback (sheet, __rowid) -> policy_id for rules whose compiled SQL
            # doesn't emit policy_number (e.g. value-in-set/range row checks): rebuild
            # rows one policy at a time, in the same order DuckDB enumerates __rowid
            # (1-based per sheet), so each output row maps back to its policy.
            rowid_to_pid: dict = {}
            sheet_counts: dict = {}
            for p in policies:
                pid = (p.get("policy") or {}).get("policy_id")
                for block in build_output_records(structure, [p]):
                    sh = block.get("sheet")
                    for _ in (block.get("records") or []):
                        i = sheet_counts.get(sh, 0) + 1
                        sheet_counts[sh] = i
                        rowid_to_pid[(sh, i)] = pid

            def _pid_for(e):
                pn = e.get("policy_number")
                if pn is not None and str(pn) in pn_to_pid:
                    return pn_to_pid[str(pn)]
                sh, rw = e.get("sheet"), e.get("row")
                if sh is not None and rw is not None:
                    try:
                        return rowid_to_pid.get((sh, int(rw)))
                    except (TypeError, ValueError):
                        return None
                return None

            # rule_id -> rule_spec, for deriving the "Expected:" constraint hint.
            rule_spec_by_id = {r.get("rule_id"): r.get("rule_spec")
                               for r in contract_validation_rules}
            items = [
                {
                    "tenant_id": tenant_id,
                    "rule_id": e.get("rule_id"),
                    "source_entity": "policy",
                    "source_entity_id": _pid_for(e),
                    "severity": (e.get("severity") or "warning").lower(),
                    "field_path": e.get("field") or e.get("column"),
                    "expected_value": _expected_from_ir(
                        rule_spec_by_id.get(e.get("rule_id"))),
                    "actual_value": e.get("actual_value"),
                    "status": "open",
                }
                for e in exceptions
            ]
            result_for_persist = {
                "exceptions": {"total": len(items), "critical": critical,
                               "warning": warning,
                               "info": len(items) - critical - warning,
                               "items": items},
                "rules": {"total": len(contract_validation_rules), "custom": 0, "ajv": 0},
                "engines": {},
                "recordCount": sum(len(b.get("records") or []) for b in records),
                "proceedToCanonical": True,
            }
            body_shim = SimpleNamespace(uploadId=upload_id, stage="output")
            with SessionLocal() as s:
                _persist(s, body_shim, tenant_id, active_contract_id, result_for_persist)
                s.commit()
        except Exception as persist_exc:  # never block the popup
            import traceback as _tb
            _tb.print_exc()
            print(f"[validate-only persist] skipped: {persist_exc}")

    return {
        "exception_count": len(violations),
        "critical": critical,
        "warning": warning,
        "violations": violations,
        "unprocessable_rules": unprocessable,
    }


@router.post("/export/generate")
def export_generate(
    template_id: int = Form(...),
    upload_id: Optional[int] = Form(default=None),
    policy_ids: Optional[str] = Form(default=None),
    filename: Optional[str] = Form(default=None),
    actor: Optional[str] = Form(default=None),
    skip_validation: bool = Form(default=False),
    principal: Principal = Depends(current_principal),
):
    """Render an xlsx using the approved template + canonical data, validate it,
    and persist it as a downloadable record. Returns JSON metadata (the bytes
    are fetched separately via /export/downloads/{id}/file).

    Provide EITHER `upload_id` (export all policies from a /bdx/upload call)
    OR `policy_ids` (comma-separated canonical policy_ids).
    """
    with SessionLocal() as s:
        t = s.get(ExportTemplate, template_id)
        if not t:
            raise HTTPException(404, "template not found")
        assert_tenant_owns(principal, t.tenant_id)
        structure = t.structure
        template_blob = storage.resolve_bytes(t.template_blob_ref, t.template_blob)
        template_name = t.name
        template_tid = t.tenant_id

        # Find the active contract for this Output Template (new hierarchy).
        # The ops DB Contract row carries output_template_id and status.
        active_contract = (
            s.query(Contract)
            .filter(
                Contract.output_template_id == template_id,
                Contract.status == "active",
            )
            .order_by(Contract.id.desc())
            .first()
        )
        active_contract_id = active_contract.id if active_contract else None
        active_contract_info = (
            {"id": active_contract.id, "filename": active_contract.filename}
            if active_contract else None
        )

        # Resolve target policy_ids.
        ids: list[int] = []
        program_id: Optional[int] = None
        if upload_id is not None:
            rows = s.execute(
                select(UploadPolicy.policy_id)
                .where(UploadPolicy.upload_id == upload_id)
                .order_by(UploadPolicy.id.asc())
            ).fetchall()
            ids = [r[0] for r in rows]
            
            # Resolve canonical program_id via upload.tenant_id → programs table
            upload_obj = s.get(Upload, upload_id)
            if upload_obj:
                prog = (
                    s.query(Program)
                    .filter(Program.tenant_id == upload_obj.tenant_id)
                    .order_by(Program.id.desc())
                    .first()
                )
                # ops + canonical share the same `program` table, so the
                # program's id IS the canonical program_id.
                program_id = prog.id if prog else None

        elif policy_ids:
            try:
                ids = [int(x.strip()) for x in policy_ids.split(",") if x.strip()]
            except ValueError:
                raise HTTPException(400, "policy_ids must be comma-separated integers")
            # No single program when arbitrary policy_ids are supplied
            program_id = None

        else:
            raise HTTPException(400, "provide upload_id or policy_ids")

    # Load validation rules from the canonical DB for the active contract.
    contract_validation_rules: list[dict] = []
    if active_contract_id is not None:
        with CanonicalSession() as cs:
            rule_rows = cs.execute(
                text(
                    "SELECT rule_id, rule_engine, rule_name, rule_description, "
                    "severity, canonical_target, rule_spec, error_message, "
                    "source_verbatim_text, source_page_number "
                    "FROM validation_rule "
                    "WHERE contract_id = :cid AND rule_status != 'disabled'"
                ),
                {"cid": active_contract_id},
            ).mappings().all()
            contract_validation_rules = [dict(r) for r in rule_rows]

    with CanonicalSession() as cs:
        policies = fetch_policies(cs, ids)

    # ── Pass A: program-level BDX validation (blocking) ──────────────
    # Validates the raw policy rows against program rules and blocks the export
    # on critical violations. Best-effort: if the validator module/program is
    # unavailable it logs and continues (never silently blocks export).
    validation_summary = None
    if not skip_validation and program_id is not None:
        try:
            from bdx_validator import validate_bdx
        except ImportError:
            # Pass A uses an OPTIONAL legacy program-level validator that is not
            # bundled in this deployment. Skip it quietly — Pass B (the DuckDB
            # contract-rule validation below) is the active validator.
            validate_bdx = None
        if validate_bdx is not None:
            try:
                bdx_rows = [p if isinstance(p, dict) else dict(p) for p in policies]
                report = validate_bdx(program_id=program_id, bdx_rows=bdx_rows)
                validation_summary = report.summary()
                print(f"[Validation] complete → {validation_summary}")
                if not report.passed:
                    return JSONResponse(
                        status_code=422,
                        content={
                            "success":   False,
                            "blocked":   True,
                            "reason":    "BDX failed validation — critical violations found.",
                            "validation": validation_summary,
                        },
                    )
            except Exception as val_exc:
                print(f"[Validation] warning — could not run: {val_exc}")

    # ── Pass B: DuckDB contract-rule validation (non-blocking) ──
    # Resolve the output values, load them into an in-memory DuckDB, and run the
    # AI-compiled SQL for each active-contract rule. Compiled SQL is cached per
    # rule; a rule that cannot be compiled to a safe, runnable query (after one
    # retry) is reported as "cannot be processed" rather than silently dropped.
    records = build_output_records(structure, policies)
    # Full column set per sheet from the TEMPLATE (not the data) so the SQL
    # schema is complete and its hash is stable across runs.
    schema_cols = {
        sh.get("sheet_name", ""): [
            c.get("column_name") for c in (sh.get("columns") or [])
            if c.get("column_name")
        ]
        for sh in (structure.get("sheets") or [])
    }
    unprocessable: list[dict] = []
    try:
        from clients import validation as _dvc
        _dv = _dvc.run_validation(
            records,
            contract_validation_rules or [],
            contract=active_contract_info,
            template_id=template_id,
            schema_cols=schema_cols,
        )
        exceptions = _dv["exceptions"]
        unprocessable = _dv["unprocessable"]
        # Label each row-level exception with the offending policy's number (the
        # compiled SQL for value-set/range rules doesn't emit one), so the review
        # UI shows a real policy id instead of "Dataset-level".
        exceptions = _dvc.label_exceptions(exceptions, structure, records)
        print(f"[DuckDB validation] {_dv['stats']}")
        # A rule reaches `unprocessable` here only when its created query is
        # missing or FAILS TO RUN against the data — i.e. a clause that was
        # supposed to validate but didn't. Surface each one as a highlighted
        # "not validated" notice so the user knows the clause did not work
        # (rather than silently assuming it passed).
        for u in unprocessable:
            exceptions.append({
                "severity": "warning",
                "code": u.get("rule_name") or "not_validated",
                "sheet": None, "row": None, "column": None, "field": None,
                "rule_id": u.get("rule_id"),
                "rule_name": u.get("rule_name"),
                "reason": u.get("message"),
                "message": f"This clause was NOT validated: {u.get('message')}",
                "error_class": "not_validated",
            })
    except Exception as dv_exc:
        # Never let validation failure block the export.
        import traceback as _tb
        _tb.print_exc()
        print(f"[DuckDB validation] ERROR — skipped: {dv_exc}")
        exceptions = []

    xlsx_bytes = generate_workbook(structure, policies, template_bytes=template_blob)
    # "Generate anyway": when the output still has validation exceptions, paint
    # each offending cell light-red with an explanatory comment so the problems
    # are visible in the downloaded file. No-op (and never raises) when clean.
    if exceptions:
        from exporter import highlight_exceptions
        xlsx_bytes = highlight_exceptions(xlsx_bytes, structure, exceptions)
    raw_name = filename or f"{(template_name or 'export').replace(' ', '_')}"
    # Always ensure the output file has an .xlsx extension regardless of what
    # the user typed or what the template name contains.
    fname = raw_name if raw_name.lower().endswith((".xlsx", ".xls", ".csv")) else raw_name + ".xlsx"

    # Persist the generated workbook to blob storage (Azure/Azurite) when
    # enabled; otherwise keep the bytes inline in `blob` (legacy behaviour).
    export_blob_ref, export_blob_bytes = storage.store_or_keep(
        "exports", template_tid, fname, xlsx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    with SessionLocal() as s:
        rec = OutputExport(
            tenant_id=template_tid, template_id=template_id, template_name=template_name,
            filename=fname, source_upload_id=upload_id, policy_ids=ids,
            generated_by=actor, policy_count=len(policies),
            exception_count=len(exceptions), exceptions=exceptions,
            status="has_exceptions" if exceptions else "clean",
            blob=export_blob_bytes,
            blob_ref=export_blob_ref,
        )
        s.add(rec)
        s.add(ActivityEvent(
            tenant_id=template_tid, actor=actor, action="output_generated",
            target=f"export:{template_name}",
            details={"filename": fname, "policies": len(policies),
                     "exceptions": len(exceptions)},
        ))
        s.commit()
        s.refresh(rec)
        out = _export_to_dict(rec, with_exceptions=True, mga=_tenant_name(s, rec.tenant_id))
        out["unprocessable_rules"] = unprocessable
        return out


@router.get("/export/downloads")
def export_downloads_list(mga: Optional[str] = None, limit: int = 50,
                          offset: int = 0,
                          principal: Principal = Depends(current_principal)):
    """Generated-output history, newest first. `offset` supports pagination
    (e.g. the dashboard's Recent runs). Still returns a plain list."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        q = s.query(OutputExport).filter(OutputExport.tenant_id == tid)
        return [_export_to_dict(r, mga=mga or _tenant_name(s, r.tenant_id))
                for r in q.order_by(OutputExport.id.desc())
                          .offset(max(0, offset)).limit(limit).all()]


@router.get("/export/downloads/count")
def export_downloads_count(mga: Optional[str] = None,
                           principal: Principal = Depends(current_principal)):
    """Total number of generated outputs — for `page X of N` pagination."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        q = s.query(OutputExport).filter(OutputExport.tenant_id == tid)
        return {"total": q.count()}


@router.get("/export/downloads/{export_id}")
def export_download_get(export_id: int,
                        principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        r = s.get(OutputExport, export_id)
        if not r:
            raise HTTPException(404, "export not found")
        assert_tenant_owns(principal, r.tenant_id)
        return _export_to_dict(r, with_exceptions=True, mga=_tenant_name(s, r.tenant_id))


@router.get("/export/downloads/{export_id}/file")
def export_download_file(export_id: int,
                         principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        r = s.get(OutputExport, export_id)
        if not r:
            raise HTTPException(404, "export file not found")
        assert_tenant_owns(principal, r.tenant_id)
        data = storage.resolve_bytes(r.blob_ref, r.blob)
        if not data:
            raise HTTPException(404, "export file not found")
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": _content_disposition(r.filename)},
        )


@router.get("/export/downloads/{export_id}/data")
def export_download_data(export_id: int,
                         principal: Principal = Depends(current_principal)):
    """The rendered rows of the generated file, for in-site viewing."""
    with SessionLocal() as s:
        r = s.get(OutputExport, export_id)
        if not r:
            raise HTTPException(404, "export file not found")
        assert_tenant_owns(principal, r.tenant_id)
        data = storage.resolve_bytes(r.blob_ref, r.blob)
        if not data:
            raise HTTPException(404, "export file not found")
        return {"filename": r.filename, "sheets": _xlsx_to_grid(data)}


@router.post("/export/downloads/{export_id}/rerender")
async def rerender_export(export_id: int, body: Optional[RerenderRequest] = None,
                          principal: Principal = Depends(current_principal)):
    """Re-generate a DIRECT-LANE export's output BDX with saved Fix/Approve
    corrections applied. Produces a NEW output_exports (the landing's corrections
    persist across renders, since they're keyed by landing, not export)."""
    with SessionLocal() as s:
        lr = s.execute(
            text("SELECT id, tenant_id FROM landing_record WHERE output_export_id = :e "
                 "ORDER BY id DESC LIMIT 1"), {"e": export_id},
        ).fetchone()
        landing_id = lr[0] if lr else None
        if landing_id is None:
            raise HTTPException(404, "no direct-lane landing record for this export")
        assert_tenant_owns(principal, lr[1])
    # Re-render IN PLACE so the export id/header stays stable across Re-generate.
    return await _render_landing(int(landing_id), None, None,
                                 body.actor if body else None, {},
                                 auto_ingest=False, reuse_export_id=export_id)
