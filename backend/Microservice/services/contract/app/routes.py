"""Route handlers owned by contract-service. Extracted from: app_routes.py."""
from __future__ import annotations
from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool
import storage  # blob storage abstraction (Azure/Azurite)
from common_app_routes import *

router = APIRouter()


@router.post("/programs/{program_id}/contracts")
async def program_contract_upload(
    program_id: int,
    file: UploadFile = File(...),
    output_template_id: int = Form(...),
    schedule_key: Optional[str] = Form(default=None),
    reference_files: Optional[list[UploadFile]] = File(default=None),
    continue_anyway: bool = Form(default=False),
    resume_token: Optional[str] = Form(default=None),
    enable_reference_halt: bool = Form(default=False),
    principal: Principal = Depends(current_principal),
):
    """Upload a contract PDF linked to an existing Output Template.

    The Output Template must exist before uploading a contract.
    Contract fields are mapped to Output Template fields by the LLM —
    not to the canonical data model directly.

    Hierarchy: Contract Fields → Output Template Fields → Data Model Fields.

    Only one contract can be Active per Output Template at any time.

    Reference documents: when the contract DEFERS rule content to an external
    document ("Excluded Classes: per the Purchasing Guidelines on file"), the
    pipeline halts after extraction and returns {status:"references_required",
    external_references, resume_token} so the UI can ask the user to upload that
    document (sent back in `reference_files`) or proceed via `continue_anyway`.
    """

    if not file.filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # -------------------------------------------------
    # RESOLVE OUTPUT TEMPLATE + FETCH ITS FIELDS
    # Must exist; returns 400 if not found.
    # -------------------------------------------------

    with SessionLocal() as s:
        # Both the program and the output template must belong to the caller's
        # tenant — derived from the token, not the client.
        prog = s.get(Program, program_id)
        if not prog:
            raise HTTPException(404, "program not found")
        assert_tenant_owns(principal, prog.tenant_id)
        tmpl = s.get(ExportTemplate, output_template_id)
        if not tmpl:
            raise HTTPException(
                status_code=400,
                detail=f"Output Template {output_template_id} not found. "
                       "Create or select an Output Template before uploading a contract.",
            )
        assert_tenant_owns(principal, tmpl.tenant_id)
        # Use the shared builder so the data-dictionary enrichment (description,
        # allowed_values, format, required) reaches the LLM mapper — building the
        # list inline here previously dropped it.
        template_fields = _template_fields_from_structure(tmpl.structure)

    print(
        f"[Contract] Template-aware extraction: "
        f"output_template_id={output_template_id}, "
        f"{len(template_fields)} template field(s)"
    )

    # -------------------------------------------------
    # SAVE TEMP FILE
    # -------------------------------------------------

    suffix = os.path.splitext(file.filename)[1]

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        contents = await file.read()
        tmp.write(contents)
        temp_file_path = tmp.name

    try:

        # -------------------------------------------------
        # VALIDATE CONTRACT UPLOAD
        # -------------------------------------------------
        from contract_upload_services.upload_file_validator import (
            validate_uploaded_contract,
            UploadContractValidationError,
        )

        try:
            validate_uploaded_contract(
                file=file,
                file_bytes=contents,
                temp_file_path=temp_file_path,
            )
        except UploadContractValidationError as ve:
            raise HTTPException(status_code=400, detail=ve.to_detail())
        except HTTPException:
            raise

        # -------------------------------------------------
        # EXACT RE-UPLOAD REUSE (no LLM)
        # Fingerprint the document + template; if an identical upload already
        # exists for this program, reuse its contract + rules and skip every
        # LLM call. This is what makes re-uploading the same contract
        # deterministic instead of regenerating a fresh rule set each time.
        # -------------------------------------------------
        from contract_upload_services.contract_versioning import (
            compute_content_fingerprint,
            compute_entity_fingerprint,
        )
        from contract_upload_services.document_extractors import extract_document_data
        from contract_upload_services.prompt_builder import build_llm_context

        # Parse the PDF to text (non-LLM) so the fingerprint is over the
        # NORMALIZED document content, not the raw bytes — a re-export of the
        # same contract still matches. Parsing is cheap relative to the LLM, so
        # the reuse short-circuit below still runs before any model call.
        parsed_doc = await run_in_threadpool(extract_document_data, temp_file_path)
        document_text = build_llm_context(parsed_doc)

        content_fp = compute_content_fingerprint(
            document_text, template_fields, output_template_id
        )
        entity_fp = compute_entity_fingerprint(program_id, file.filename)

        # Contract-only re-upload ("Upload new version") must ALWAYS regenerate
        # rules — it is intentionally NOT skippable. Only the combined
        # contract + output-template flow (/setup) is allowed to skip via the
        # identical-upload reuse short-circuit. So never reuse here.
        reusable = None
        if reusable:
            cid = reusable["contract_id"]
            print(
                f"[Contract] Identical re-upload detected "
                f"(content_fingerprint={content_fp[:12]}…) — reusing "
                f"contract_id={cid}; skipping all LLM calls."
            )
            program_obj = None
            contract_obj = None
            with SessionLocal() as s:
                prog = s.get(Program, program_id)
                if prog:
                    program_obj = _program_dict(prog)
                c = s.get(Contract, cid)
                if c:
                    contract_obj = {
                        "id":                 c.id,
                        "filename":           c.filename,
                        "status":             c.status,
                        "extracted":          c.extracted,
                        "output_template_id": c.output_template_id,
                        "created_at":         _iso_utc(c.created_at),
                    }
            return {
                "success": True,
                "reused": True,
                "program": program_obj,
                "id":        (contract_obj or {}).get("id"),
                "filename":  (contract_obj or {}).get("filename", file.filename),
                "status":    (contract_obj or {}).get("status"),
                "extracted": (contract_obj or {}).get("extracted"),
                "contract":  contract_obj,
                "persisted": {"contract_id": cid, "reused": True},
                "persist_warning": None,
                "extraction_output": None,
            }

        # -------------------------------------------------
        # REFERENCE DOCUMENTS + HALT GATE
        # When the contract DEFERS rule content to an external document (e.g.
        # "Excluded Classes: per the Purchasing Guidelines on file"), pause after
        # extraction and ask the user to upload that document so the deferred
        # clauses resolve into concrete rules. Don't halt when the user already
        # attached reference doc(s) this request, or chose "Continue Anyway".
        # -------------------------------------------------
        reference_documents = await _extract_reference_documents(
            reference_files, program_id=program_id, tenant_id=principal.tenant_id)
        has_reference_files = bool(reference_documents)
        if has_reference_files:
            print(
                f"[Contract] {len(reference_documents)} reference document(s) provided: "
                f"{[rd['name'] for rd in reference_documents]}"
            )
        # Only halt for callers that can HANDLE the references_required response
        # (DirectSetup opts in via enable_reference_halt). Other callers — e.g.
        # the Programs "upload new version" flow — keep the non-halting behaviour:
        # a contract that defers to an external doc just proceeds (deferred clauses
        # won't become rules), exactly as before.
        halt = enable_reference_halt and not continue_anyway and not has_reference_files

        # -------------------------------------------------
        # PROCESS CONTRACT
        # LLM maps contract clauses → Output Template fields
        # (not the canonical data model)
        # -------------------------------------------------

        extraction_output = await run_in_threadpool(
            contract_service.process_contract,
            temp_file_path,
            template_fields=template_fields if template_fields else None,
            halt_on_external_references=halt,
            resume_token=resume_token if continue_anyway else None,
            reference_documents=reference_documents or None,
        )

        # Pipeline paused — contract references external document(s) not provided.
        # Nothing is persisted; return the referenced names so the UI can prompt
        # the user to upload them (resend as reference_files) or continue anyway.
        if isinstance(extraction_output, dict) and extraction_output.get("halted_for_references"):
            halted_refs = extraction_output.get("external_references", [])
            print(
                f"[Contract] HALTED for {len(halted_refs)} external "
                f"reference(s) — awaiting user (upload reference / continue anyway)."
            )
            return {
                "status": "references_required",
                "external_references": halted_refs,
                "resume_token": extraction_output.get("resume_token"),
                "id": None,
                "contract": None,
            }
        # print(f"[Contract] Extraction output: {extraction_output}")
        # -------------------------------------------------
        # PERSIST TO POSTGRES
        # output_template_id stored on the contract row.
        # -------------------------------------------------

        persist_result = None
        persist_warning = None

        try:
            from contract_upload_services.db_persister import persist_pipeline_output

            persist_result = persist_pipeline_output(
                extraction_output,
                program_id,
                file.filename,
                output_template_id=output_template_id,
                content_fingerprint=content_fp,
                entity_fingerprint=entity_fp,
            )
            print(f"[persist_result] {persist_result}")

        except Exception as persist_exc:
            persist_warning = f"persistence failed: {persist_exc}"
            print(f"[Persist] {persist_warning}")

        # A failed persist means no contract row was created — surface it as an
        # error instead of returning a misleading success with id=null.
        if not (persist_result or {}).get("contract_id"):
            raise HTTPException(
                status_code=500,
                detail=persist_warning or "Contract was processed but could not be saved.",
            )


        # Persist the raw contract file to blob storage (Azure/Azurite) when
        # enabled. Historically the uploaded file was discarded after
        # extraction; with blob storage on we now retain it and record the
        # pointer on the contract row (blob_ref) below. Non-fatal on failure.
        contract_blob_ref = None
        if storage.is_azure():
            try:
                contract_blob_ref = storage.build_key(
                    "contracts", principal.tenant_id, file.filename)
                await run_in_threadpool(
                    storage.put_bytes, contract_blob_ref, contents,
                    file.content_type or "application/octet-stream",
                )
            except Exception as blob_exc:                # noqa: BLE001 — best-effort
                contract_blob_ref = None
                print(f"[Contract] blob persist failed (non-fatal): {blob_exc}")

        # -------------------------------------------------
        # Auto-activate within Program scope.
        # Only ONE Active contract is allowed per Program at a time. Uploading a
        # new version supersedes every other contract in the program (including
        # ones linked to an earlier output template version).
        # -------------------------------------------------

        program_obj = None
        contract_obj = None

        with SessionLocal() as s:
            prog = s.get(Program, program_id)
            if prog:
                program_obj = _program_dict(prog)

            cid = (persist_result or {}).get("contract_id")
            if cid:
                # Supersede scope: when a schedule_key is given, only replace the
                # PRIOR contract for that SAME schedule — so one program keeps many
                # active contracts (one per schedule). With no schedule_key we keep
                # the legacy behaviour of superseding every contract in the program.
                sib_q = s.query(Contract).filter(
                    Contract.program_id == program_id,
                    Contract.id != cid,
                    Contract.status.notin_(["failed", "drafted", "extracting"]),
                )
                if schedule_key is not None:
                    sib_q = sib_q.filter(Contract.schedule_key == schedule_key)
                for sib in sib_q.all():
                    sib.status = "superseded"
                new_c = s.get(Contract, cid)
                if new_c:
                    new_c.status = "active"
                    if contract_blob_ref:
                        new_c.blob_ref = contract_blob_ref
                    if schedule_key is not None:
                        new_c.schedule_key = schedule_key
                s.commit()

                c = s.get(Contract, cid)
                if c:
                    contract_obj = {
                        "id":                 c.id,
                        "filename":           c.filename,
                        "status":             c.status,
                        "extracted":          c.extracted,
                        "output_template_id": c.output_template_id,
                        "schedule_key":       c.schedule_key,
                        "created_at":         _iso_utc(c.created_at),
                    }

        return {
            "success": True,
            "program": program_obj,
            "id":        (contract_obj or {}).get("id"),
            "filename":  (contract_obj or {}).get("filename", file.filename),
            "status":    (contract_obj or {}).get("status"),
            "extracted": (contract_obj or {}).get("extracted"),
            "contract":  contract_obj,
            "persisted": persist_result,
            "persist_warning": persist_warning,
            "extraction_output": extraction_output,
        }

    except Exception as e:
        print(f"\n[ERROR] Contract Processing Failed")
        print("Error::", str(e))
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)


@router.get("/programs/{program_id}/contracts")
def program_contracts_list(program_id: int,
                           principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        prog = s.get(Program, program_id)
        if not prog:
            raise HTTPException(404, "program not found")
        assert_tenant_owns(principal, prog.tenant_id)
        rows = s.query(Contract).filter(Contract.program_id == program_id)\
                .order_by(Contract.id.desc()).all()
        # Clause count per contract = rows in clauses_extracted (defensive: the
        # table may be absent on minimal DBs).
        counts: dict[int, int] = {}
        try:
            if rows:
                ids = [c.id for c in rows]
                res = s.execute(text(
                    "SELECT contract_id, COUNT(*) AS n FROM clauses_extracted "
                    "WHERE contract_id = ANY(:ids) GROUP BY contract_id"),
                    {"ids": ids}).mappings().all()
                counts = {r["contract_id"]: r["n"] for r in res}
        except Exception:
            counts = {}
        return [{"id": c.id, "filename": c.filename, "status": c.status,
                 "extracted": c.extracted,
                 "clause_count": counts.get(c.id, 0),
                 "output_template_id": c.output_template_id,
                 "schedule_key": c.schedule_key,
                 "created_at": _iso_utc(c.created_at)}
                for c in rows]


@router.get("/programs/{program_id}/contracts/{contract_id}")
def program_contract_detail(program_id: int, contract_id: int,
                            principal: Principal = Depends(current_principal)):
    """Return a contract detail payload for the UI.

    Includes the contract, linked Output Template, extracted commercial terms,
    field mappings inferred from generated rules, and all validation rules.
    """
    with SessionLocal() as s:
        contract = s.get(Contract, contract_id)
        if not contract or contract.program_id != program_id:
            raise HTTPException(404, "contract not found for this program")
        assert_tenant_owns(principal, contract.tenant_id)

        template = None
        template_fields: list[dict] = []
        if contract.output_template_id:
            tmpl = s.get(ExportTemplate, contract.output_template_id)
            if tmpl:
                template_fields = _template_fields_from_structure(tmpl.structure)
                template = {
                    "id": tmpl.id,
                    "name": tmpl.name,
                    "version": tmpl.version,
                    "is_active": bool(tmpl.is_active),
                    "approved": bool(tmpl.approved),
                    "fields": template_fields,
                }

        term_rows = s.execute(
            text("""
                SELECT term_id,
                       term_type            AS term_category,
                       term_definition,
                       term_source_reference AS extracted_from_clause_ref,
                       NULL                 AS extraction_confidence
                FROM contract_term
                WHERE term_contract_id = :cid
                ORDER BY term_id
            """),
            {"cid": contract_id},
        ).mappings().all()

        clause_rows = s.execute(
            text("""
                SELECT clause_id, clause_type, title, text, page_number,
                       section_header, classified_engine, classified_rule_types,
                       classification_confidence, generated_rule_count
                FROM clauses_extracted
                WHERE contract_id = :cid
                ORDER BY clause_id
            """),
            {"cid": contract_id},
        ).mappings().all()
        clauses_by_id = {row["clause_id"]: dict(row) for row in clause_rows}

        rule_rows = s.execute(
            text("""
                SELECT rule_id AS validation_rule_id, rule_engine, rule_name,
                       rule_description, validation_stage, severity,
                       canonical_target, rule_spec, error_message,
                       source_clause_id, source_verbatim_text,
                       source_page_number, generation_confidence, rule_status
                FROM validation_rule
                WHERE contract_id = :cid AND rule_status <> 'disabled'
                ORDER BY rule_id
            """),
            {"cid": contract_id},
        ).mappings().all()

        rules = []
        mappings_by_key: dict[tuple[str, str], dict] = {}

        for row in rule_rows:
            rule = dict(row)
            rule["canonical_target"] = _json_value(rule.get("canonical_target")) or {}
            rule["rule_spec"] = _json_value(rule.get("rule_spec")) or {}
            # Surface the current output-field mapping + whether it's a retargetable
            # IR rule, so the UI can offer "change field" / "remove" per rule.
            _ofields = _output_fields_from_rule(rule)
            rule["output_field"] = _ofields[0] if _ofields else None
            rule["output_fields"] = _ofields
            _spec = rule["rule_spec"]
            rule["rule_kind"] = _spec.get("kind") if isinstance(_spec, dict) else None
            clause = clauses_by_id.get(rule.get("source_clause_id"))
            if clause:
                rule["source_clause"] = {
                    "id": clause.get("clause_id"),
                    "title": clause.get("title"),
                    "text": clause.get("text"),
                    "page_number": clause.get("page_number"),
                    "section_header": clause.get("section_header"),
                }
            rules.append(rule)

            contract_field = (
                (clause or {}).get("title")
                or rule.get("source_verbatim_text")
                or rule.get("rule_name")
                or "Contract clause"
            )
            for output_field in _output_fields_from_rule(rule):
                key = (str(contract_field), output_field)
                if key not in mappings_by_key:
                    mappings_by_key[key] = {
                        "contract_field": contract_field,
                        "contract_clause_id": rule.get("source_clause_id"),
                        "contract_clause_text": (clause or {}).get("text") or rule.get("source_verbatim_text"),
                        "output_field": output_field,
                        "rule_names": [],
                    }
                mappings_by_key[key]["rule_names"].append(rule.get("rule_name"))

        terms = [
            {
                "id": row["term_id"],
                "category": row["term_category"],
                "value": _json_value(row["term_definition"]),
                "source_text": row["extracted_from_clause_ref"],
                "confidence": row["extraction_confidence"],
            }
            for row in term_rows
        ]

        # Non-validatable clauses (review = needs data the BDX lacks; control =
        # governance / obligations). Table may not exist on older DBs — tolerate.
        clause_routing: list[dict] = []
        try:
            routing_rows = s.execute(
                text("""
                    SELECT clause_id, bucket, rule_name, clause_text,
                           source_page, reason
                    FROM contract_clause_routing
                    WHERE contract_id = :cid
                    ORDER BY bucket, routing_id
                """),
                {"cid": contract_id},
            ).mappings().all()
            clause_routing = [dict(r) for r in routing_rows]
        except Exception:
            clause_routing = []

        return {
            "contract": {
                "id": contract.id,
                "program_id": contract.program_id,
                "filename": contract.filename,
                "status": contract.status,
                "output_template_id": contract.output_template_id,
                "extracted": contract.extracted,
                "template_field_mappings": contract.template_field_mappings,
                "created_at": _iso_utc(contract.created_at),
            },
            "output_template": template,
            "terms": terms,
            "clauses": [dict(row) for row in clause_rows],
            "field_mappings": list(mappings_by_key.values()),
            "rules": rules,
            "clause_routing": clause_routing,
        }


@router.put("/programs/{program_id}/contracts/{contract_id}/rules/{rule_id}/output-field")
def rule_retarget_output_field(program_id: int, contract_id: int, rule_id: int,
                               body: RuleRetargetBody,
                               principal: Principal = Depends(current_principal)):
    """Change the OUTPUT FIELD an IR validation rule is mapped to.

    Deterministically re-derives the rule's IR, compiled SQL and canonical_target
    from the new field (no LLM), so validation, formulas and the clause→field
    display all follow. Soft cache (`rule_sql`) is cleared so it recompiles clean.
    """
    from contract_upload_services.output_schema import build_output_schema
    from contract_upload_services.rule_editor import retarget_ir_rule, RetargetError

    if not body.new_field or not body.new_field.strip():
        raise HTTPException(400, "new_field is required")

    with SessionLocal() as s:
        contract = s.get(Contract, contract_id)
        if not contract or contract.program_id != program_id:
            raise HTTPException(404, "contract not found for this program")
        assert_tenant_owns(principal, contract.tenant_id)
        if not contract.output_template_id:
            raise HTTPException(400, "this contract has no output template to map fields against")
        tmpl = s.get(ExportTemplate, contract.output_template_id)
        if not tmpl:
            raise HTTPException(404, "output template not found")
        schema = build_output_schema(_template_fields_from_structure(tmpl.structure))
        tenant_id = contract.tenant_id

        row = _load_rule_for_contract(s, contract_id, rule_id)
        if row["rule_status"] == "disabled":
            raise HTTPException(400, "this rule was removed; restore it before retargeting")
        rule_spec = _json_value(row["rule_spec"]) or {}
        canonical_target = _json_value(row["canonical_target"]) or {}
        old_fields = _output_fields_from_rule(
            {"canonical_target": canonical_target, "rule_spec": rule_spec})
        old_field = body.old_field or (old_fields[0] if old_fields else None)

        try:
            new_spec, new_target, new_error, resolved_old, resolved_new = retarget_ir_rule(
                rule_spec, canonical_target, row.get("error_message"),
                old_field, body.new_field, schema)
        except RetargetError as e:
            raise HTTPException(400, str(e))

        s.execute(
            text("""UPDATE validation_rule
                       SET rule_spec = CAST(:rs AS JSONB),
                           canonical_target = CAST(:ct AS JSONB),
                           error_message = :em,
                           updated_at = now(), updated_by = :actor
                     WHERE rule_id = :rid"""),
            {"rs": json.dumps(new_spec, default=str),
             "ct": json.dumps(new_target, default=str),
             "em": new_error, "actor": body.actor or "user", "rid": rule_id},
        )
        s.commit()
        # Drop the stale compiled-SQL cache row (own tx — see _purge_rule_sql).
        _purge_rule_sql(s, rule_id)
        tenant_mga = body.mga or _tenant_name(s, tenant_id)

    _log(tenant_mga, body.actor, "rule.output_field_changed",
         target=f"rule:{rule_id}",
         details={"contract_id": contract_id, "program_id": program_id,
                  "rule_name": row["rule_name"],
                  "old_field": resolved_old, "new_field": resolved_new})
    return {"ok": True, "rule_id": rule_id,
            "output_field": new_target.get("output_field"),
            "output_fields": new_target.get("output_fields")}


@router.post("/programs/{program_id}/contracts/{contract_id}/clause-routing/{clause_id}/resolve")
def resolve_clause_routing(program_id: int, contract_id: int, clause_id: int,
                           body: ClauseResolveBody,
                           principal: Principal = Depends(current_principal)):
    """Generate a validation rule for an `in_review` (unmapped) clause by binding
    it to a user-chosen Output-Template field.

    Re-runs the real generation pipeline (intent extraction → IR mapping forced
    onto the chosen field → verify/compile) for this one clause, then persists
    the rule(s), updates the clause status to 'rules_generated' and removes it
    from the review queue. If the field genuinely cannot represent the clause,
    nothing is written and the verifier's reason is returned.
    """
    from contract_upload_services.output_schema import build_output_schema
    from contract_upload_services.manual_rule_resolution import (
        generate_rules_for_clause_field,
    )
    from contract_upload_services import db_persister

    # Accept a single field or a list (rule spanning several columns). The first
    # entry is the primary; the rest scope/condition the rule.
    raw_fields = [f for f in (body.output_fields or []) if f and f.strip()]
    if not raw_fields and body.output_field and body.output_field.strip():
        raw_fields = [body.output_field.strip()]
    if not raw_fields:
        raise HTTPException(400, "output_field (or output_fields) is required")

    with SessionLocal() as s:
        contract = s.get(Contract, contract_id)
        if not contract or contract.program_id != program_id:
            raise HTTPException(404, "contract not found for this program")
        assert_tenant_owns(principal, contract.tenant_id)
        if not contract.output_template_id:
            raise HTTPException(400, "this contract has no output template to map fields against")
        tmpl = s.get(ExportTemplate, contract.output_template_id)
        if not tmpl:
            raise HTTPException(404, "output template not found")

        template_fields = _template_fields_from_structure(tmpl.structure)
        schema = build_output_schema(template_fields)

        # Resolve every pick to an exact template column (tolerant match), keeping
        # order and dropping duplicates. The first becomes the primary.
        resolved_fields: list[str] = []
        for raw in raw_fields:
            rf = schema.resolve_field(raw.strip())
            if not rf:
                raise HTTPException(
                    400, f"'{raw}' is not a field in this contract's "
                         f"output template")
            if rf not in resolved_fields:
                resolved_fields.append(rf)
        resolved = resolved_fields[0]
        extra_fields = resolved_fields[1:]
        chosen_field = next(
            (f for f in template_fields if f.get("name") == resolved), None)
        if chosen_field is None:
            raise HTTPException(400, "selected output field could not be loaded")

        clause = s.execute(
            text("""SELECT clause_id, contract_id, clause_type, title, text,
                           page_number, section_header, rule_generation_status
                    FROM clauses_extracted
                    WHERE clause_id = :clid AND contract_id = :cid"""),
            {"clid": clause_id, "cid": contract_id},
        ).mappings().first()
        if not clause:
            raise HTTPException(404, "clause not found for this contract")

        tenant_id = contract.tenant_id
        output_template_id = contract.output_template_id
        tenant_mga = body.mga or _tenant_name(s, tenant_id)

    clause_dict = {
        "clause_id":      clause["clause_id"],
        "contract_id":    clause["contract_id"],
        "clause_type":    clause["clause_type"],
        "title":          clause["title"],
        "text":           clause["text"],
        "page_number":    clause["page_number"],
        "section_header": clause["section_header"],
    }
    contract_ctx = {"tenant_id": tenant_id, "contract_id": contract_id,
                    "program_id": program_id}

    # Heavy step (LLM calls + DuckDB verify). FastAPI runs this sync route in a
    # worker thread, so blocking here is fine.
    validation_rules, review_queue, control_register = (
        generate_rules_for_clause_field(
            clause_dict, chosen_field, template_fields, schema, contract_ctx,
            note=body.note, extra_field_names=extra_fields)
    )

    if not validation_rules:
        # Could not bind to the chosen field — surface why (clause stays in_review).
        reason = None
        for bucket in (review_queue, control_register):
            for it in bucket:
                if isinstance(it, dict) and it.get("reason"):
                    reason = it["reason"]
                    break
            if reason:
                break
        return {
            "ok": False,
            "clause_id": clause_id,
            "output_field": resolved,
            "output_fields": resolved_fields,
            "status": "in_review",
            "reason": reason or
                      f"The clause could not be expressed against '{resolved}'.",
        }

    created = db_persister.persist_resolved_rules(
        contract_id=contract_id,
        program_id=program_id,
        tenant_id=tenant_id,
        db_clause_id=clause_id,
        output_template_id=output_template_id,
        validation_rules=validation_rules,
        actor=body.actor or "user",
    )

    _log(tenant_mga, body.actor, "clause.resolved_to_field",
         target=f"clause:{clause_id}",
         details={"contract_id": contract_id, "program_id": program_id,
                  "output_field": resolved,
                  "output_fields": resolved_fields,
                  "note": (body.note or "").strip() or None,
                  "rule_ids": [c["rule_id"] for c in created],
                  "rule_names": [c["rule_name"] for c in created]})

    return {
        "ok": True,
        "clause_id": clause_id,
        "output_field": resolved,
        "output_fields": resolved_fields,
        "status": "rules_generated",
        "note": (body.note or "").strip() or None,
        "created_rules": created,
    }


@router.delete("/programs/{program_id}/contracts/{contract_id}/rules/{rule_id}")
def rule_delete(program_id: int, contract_id: int, rule_id: int,
                mga: Optional[str] = None, actor: Optional[str] = None,
                principal: Principal = Depends(current_principal)):
    """Soft-delete a validation rule (rule_status='disabled').

    Excluded from validation in every lane and hidden from the contract view,
    but the row + exception history are preserved and it is recoverable.
    """
    with SessionLocal() as s:
        contract = s.get(Contract, contract_id)
        if not contract or contract.program_id != program_id:
            raise HTTPException(404, "contract not found for this program")
        assert_tenant_owns(principal, contract.tenant_id)
        tenant_id = contract.tenant_id
        row = _load_rule_for_contract(s, contract_id, rule_id)
        changed = row["rule_status"] != "disabled"
        if changed:
            s.execute(
                text("""UPDATE validation_rule
                           SET rule_status = 'disabled', updated_at = now(), updated_by = :actor
                         WHERE rule_id = :rid"""),
                {"actor": actor or "user", "rid": rule_id})
            s.commit()
            # Drop the compiled-SQL cache row (own tx — see _purge_rule_sql).
            _purge_rule_sql(s, rule_id)
        tenant_mga = mga or _tenant_name(s, tenant_id)
        ofields = _output_fields_from_rule(
            {"canonical_target": _json_value(row["canonical_target"]) or {}})

    # Only audit a real state transition (an idempotent re-delete is a no-op).
    if changed:
        _log(tenant_mga, actor, "rule.disabled", target=f"rule:{rule_id}",
             details={"contract_id": contract_id, "program_id": program_id,
                      "rule_name": row["rule_name"],
                      "output_field": ofields[0] if ofields else None})
    return {"ok": True, "rule_id": rule_id, "already_disabled": not changed}


@router.post("/programs/{program_id}/contracts/{contract_id}/activate")
def program_contract_activate(program_id: int, contract_id: int,
                              principal: Principal = Depends(current_principal)):
    """Make one contract the active one for its Program (schedule-scoped).

    Supersedes the other contract(s) for the SAME schedule only, so a program can
    keep one active contract per schedule. A contract with no schedule_key (legacy)
    still supersedes every other contract in the program.
    Failed/drafted/extracting contracts must be re-uploaded before activation.
    """
    with SessionLocal() as s:
        target = s.get(Contract, contract_id)
        if not target or target.program_id != program_id:
            raise HTTPException(404, "contract not found for this program")
        assert_tenant_owns(principal, target.tenant_id)
        if target.status in ("failed", "drafted", "extracting"):
            raise HTTPException(400, f"cannot activate a contract with status '{target.status}'")

        # One active contract per (program, schedule) — supersede same-schedule
        # siblings. Legacy (schedule_key IS NULL) supersedes all, as before.
        sib_q = s.query(Contract).filter(
            Contract.program_id == program_id,
            Contract.id != contract_id,
        )
        if target.schedule_key is not None:
            sib_q = sib_q.filter(Contract.schedule_key == target.schedule_key)

        for sib in sib_q.all():
            if sib.status not in ("failed", "drafted", "extracting"):
                sib.status = "superseded"
        target.status = "active"
        s.commit()
        _log(_tenant_name(s, target.tenant_id) or "", None, "contract_activated",
             target=str(contract_id),
             details={"program_id": program_id, "output_template_id": target.output_template_id})
        # Return the full updated list for this program
        rows = s.query(Contract).filter(Contract.program_id == program_id)\
                .order_by(Contract.id.desc()).all()
        return [{"id": c.id, "filename": c.filename, "status": c.status,
                 "extracted": c.extracted,
                 "output_template_id": c.output_template_id,
                 "schedule_key": c.schedule_key,
                 "created_at": _iso_utc(c.created_at)}
                for c in rows]
