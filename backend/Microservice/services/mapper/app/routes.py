"""Route handlers owned by mapper-service. Extracted from: app_routes.py, main.py, validation_routes.py."""
from __future__ import annotations
from fastapi import APIRouter, Body
from common_app_routes import *
from common_main import *
from common_validation_routes import *
from mapper import generate_mapping_multi   # LLM engine, owned by mapper-service
from starlette.concurrency import run_in_threadpool
import storage   # blob storage abstraction (Azure/Azurite) with DB fallback

router = APIRouter()


@router.post("/internal/mapping/generate", tags=["_internal"])
def _internal_generate_mapping(payload: dict = Body(...)):
    """Internal boundary: other services POST serialized sheets here instead of
    importing the LLM engine. Body: {"sheets": {name: {"columns": [...], "rows": [[...]]}}}."""
    import pandas as pd
    sheets = {}
    for name, sh in (payload.get("sheets") or {}).items():
        cols = sh.get("columns") or []
        rows = sh.get("rows") or []
        sheets[name] = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    return generate_mapping_multi(sheets)


@router.get("/data-model")
def get_data_model(principal: Principal = Depends(current_principal)):
    return DATA_MODEL


@router.post("/bdx/sheets")
async def bdx_sheets(
    file: UploadFile = File(...),
    skip_rows: int = Form(default=0),
    principal: Principal = Depends(current_principal),
):
    """Return the list of sheets in a workbook so the UI can ask the user
    which ones to process. Returned shape:
        { "sheets": [ {"name": "...", "rows": int, "columns": int,
                       "headers": [first 8 column names]} ] }
    """
    file_bytes = await file.read()
    try:
        sheets = await run_in_threadpool(read_excel_all_sheets, file_bytes, skip_rows)
    except Exception as e:
        raise HTTPException(400, f"could not parse uploaded file: {e}")
    if not sheets:
        raise HTTPException(400, "workbook has no readable sheets")
    out = []
    for name, df in sheets.items():
        out.append({
            "name": name,
            "rows": int(df.shape[0]),
            "columns": int(df.shape[1]),
            "headers": [str(c) for c in list(df.columns)[:8]],
        })
    return {"sheets": out}


@router.post("/mapper/generate")
async def mapper_generate(
    mga: str = Form(...),
    file: UploadFile = File(...),
    name: Optional[str] = Form(default=None),
    carrier: Optional[str] = Form(default=None),
    contract: Optional[str] = Form(default=None),
    party_id: Optional[int] = Form(default=None),
    skip_rows: int = Form(default=0),
    sheets: Optional[str] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    """Generate a proposed header→canonical mapping AND save it for the MGA.

    `sheets` is an optional comma-separated list of sheet names to include;
    omit to use every sheet in the workbook.
    """
    file_bytes = await file.read()

    sheets_dict = await run_in_threadpool(read_excel_all_sheets, file_bytes, skip_rows)
    if not sheets_dict:
        raise HTTPException(400, "workbook has no readable sheets")
    sheets = _filter_sheets(sheets_dict, sheets)
    sig = signature_multi(sheets)

    # FORMAT-LEVEL CACHE: if ANY tenant has already produced a mapper with
    # the exact same column signature, clone its spec instead of calling
    # Gemini at all. Approved mappers win over drafts. Result: zero LLM
    # calls for a workbook whose layout we've seen before.
    cloned = _try_clone_existing_mapper(sig)
    cloned = None
    if cloned is not None:
        result = cloned
        log.info("Mapper format match — cloned spec from existing mapper "
                 "#%s (skipped Gemini entirely)",
                 cloned.get("_cloned_from_mapper_id"))
        # Re-derive sample dict from the current workbook so the response
        # carries fresh sample values even though the spec was cloned.
        from mapper import qualify, MAX_SAMPLES
        samples: dict[str, list[str]] = {}
        for sheet_name, df in sheets.items():
            for col in df.columns:
                q = qualify(str(sheet_name), str(col))
                samples[q] = df[col].dropna().astype(str).head(MAX_SAMPLES).tolist()
        result["samples"] = samples
        result["sheets"] = list(sheets.keys())
    else:
        result = await run_in_threadpool(generate_mapping_multi, sheets)
    source_columns = list(result["samples"].keys())  # 'sheet :: column'
    sample_rows = {
        name: df.head(5).where(lambda d: d.notna(), "").astype(str).to_dict(orient="records")
        for name, df in sheets.items()
    }
    sheets_meta = list(sheets.keys())

    # Persist the canonical fingerprint first (cross-tenant home), then
    # the operational mappers row referencing it.
    fingerprint_id: Optional[int] = None
    try:
        from fingerprint import upsert as fingerprint_upsert
        with CanonicalSession() as cs:
            fingerprint_id = fingerprint_upsert(
                cs,
                mga=mga,
                signature_tokens=sig,
                canonical_mapping=result.get("spec_by_sheet") or {},
            )
            cs.commit()
    except Exception as e:
        log.warning("canonical fingerprint upsert failed: %s", e)

    name = (name or "").strip() or None
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        # Versioning: a new row under an existing (tenant, name) becomes the next
        # version; the first version of a brand-new template auto-activates.
        if name:
            siblings = (
                s.query(Mapper)
                .filter(Mapper.tenant_id == tid, Mapper.name == name)
                .all()
            )
        else:
            siblings = [
                mm for mm in s.query(Mapper).filter(Mapper.tenant_id == tid).all()
                if not mm.name and mm.signature == sig
            ]
        version = (max((r.version or 1) for r in siblings) + 1) if siblings else 1
        is_active = 0 if siblings else 1

        # Persist the workbook to blob storage (Azure/Azurite) when enabled;
        # otherwise keep the bytes inline in source_blob (legacy behaviour).
        mapper_blob_ref, mapper_blob_bytes = await run_in_threadpool(
            storage.store_or_keep, "mappers", tid, file.filename, file_bytes,
        )
        m = Mapper(
            tenant_id=tid, name=name, version=version, is_active=is_active,
            carrier=carrier, contract=contract, party_id=party_id,
            signature=sig,
            spec=result["spec"],
            spec_by_sheet=result["spec_by_sheet"],
            candidates=result.get("candidates_by_source") or {},
            samples=result.get("samples") or {},
            approved=0,
            source_filename=file.filename,
            source_blob=mapper_blob_bytes,
            source_blob_ref=mapper_blob_ref,
            selected_sheets=sheets_meta,
            fingerprint_id=fingerprint_id,
        )
        s.add(m)
        s.commit()
        s.refresh(m)
        mapper_id = m.id
        mapper_version = m.version
        mapper_active = bool(m.is_active)

    return {
        "mapper_id": mapper_id,
        "name": name,
        "version": mapper_version,
        "is_active": mapper_active,
        "mga": mga, "carrier": carrier, "contract": contract, "party_id": party_id,
        "approved": False,
        "signature": sig,
        "sheets": sheets_meta,
        "source_columns": source_columns,
        "skip_rows": skip_rows,
        "spec": result["spec"],
        "spec_by_sheet": result["spec_by_sheet"],
        "candidates": result.get("candidates_by_source") or {},
        "categories": {
            "successful":   result["successful"],
            "likely":       result["likely"],
            "unsuccessful": result["unsuccessful"],
        },
        "canonical_unmapped": result["canonical_unmapped"],
        "samples": result["samples"],
        "sample_rows": sample_rows,
    }


@router.put("/mapper/{mapper_id}")
def mapper_update(mapper_id: int, body: UpdateMapperBody,
                  principal: Principal = Depends(current_principal)):
    """MGA submits corrected mappings (and optionally approves)."""
    if body.spec is None and body.spec_by_sheet is None and body.approved is None \
       and body.carrier is None and body.contract is None:
        raise HTTPException(400, "no fields to update")
    with SessionLocal() as s:
        m = s.get(Mapper, mapper_id)
        if not m:
            raise HTTPException(404, "mapper not found")
        assert_tenant_owns(principal, m.tenant_id)
        if body.spec is not None:
            m.spec = body.spec
        if body.spec_by_sheet is not None:
            m.spec_by_sheet = body.spec_by_sheet
            # Push every user-approved mapping into the cross-tenant cache
            # so future uploads of similar columns skip the LLM. User
            # corrections outrank the LLM via source="user".
            from mapper import sample_fingerprint, cache_store, SHEET_SEP
            # No raw samples on the saved mapper, so the cache row is keyed
            # by sheet+column only (empty fingerprint). That's enough to win
            # tier-2 lookups for the same column name on later uploads.
            for mapping in (m.spec_by_sheet or {}).values():
                for canonical, src_val in mapping.items():
                    srcs = src_val if isinstance(src_val, list) else [src_val]
                    for src in srcs:
                        s_name, _, col = src.partition(SHEET_SEP)
                        try:
                            cache_store(s, s_name, col,
                                        sample_fingerprint([]),
                                        canonical,
                                        confidence=1.0, source="user")
                        except Exception as e:
                            log.warning("cache_store(user) failed for %s → %s: %s",
                                        src, canonical, e)
        if body.approved is not None:
            m.approved = 1 if body.approved else 0
            # Approving a version makes it the live one for its template.
            if body.approved:
                _activate_mapper(s, m)
        if body.carrier is not None:
            m.carrier = body.carrier
        if body.contract is not None:
            m.contract = body.contract
        s.commit()
        s.refresh(m)

        # Mirror the approved spec into the canonical fingerprint so
        # future cross-tenant lookups serve the user-corrected mapping.
        if body.spec_by_sheet is not None:
            try:
                from fingerprint import upsert as fingerprint_upsert
                with CanonicalSession() as cs:
                    new_fp_id = fingerprint_upsert(
                        cs, mga=_tenant_name(s, m.tenant_id),
                        signature_tokens=m.signature or [],
                        canonical_mapping=m.spec_by_sheet or {},
                    )
                    cs.commit()
                if new_fp_id and new_fp_id != m.fingerprint_id:
                    m.fingerprint_id = new_fp_id
                    s.commit()
            except Exception as e:
                log.warning("fingerprint refresh on PUT failed: %s", e)
        return _mapper_to_dict(m, mga=_tenant_name(s, m.tenant_id))


@router.get("/mapper")
def mapper_list(mga: Optional[str] = None,
                principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        return [_mapper_to_dict(m, hsb, mga=mga) for m, hsb in _mapper_rows_no_blob(s, tid)]


@router.get("/mapper/templates")
def mapper_templates(mga: Optional[str] = None,
                     principal: Principal = Depends(current_principal)):
    """Input mappers grouped into templates with their versions, newest first."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        return _group_versions([_mapper_to_dict(m, hsb, mga=mga) for m, hsb in _mapper_rows_no_blob(s, tid)])


@router.post("/mapper/{mapper_id}/activate")
def mapper_activate(mapper_id: int,
                    principal: Principal = Depends(current_principal)):
    """Make this mapper version the active one for its template."""
    with SessionLocal() as s:
        m = s.get(Mapper, mapper_id)
        if not m:
            raise HTTPException(404, "mapper not found")
        assert_tenant_owns(principal, m.tenant_id)
        _activate_mapper(s, m)
        s.commit()
        s.refresh(m)
        return _mapper_to_dict(m)


@router.get("/mapper/{mapper_id}")
def mapper_get(mapper_id: int,
               principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        m = s.get(Mapper, mapper_id)
        if not m:
            raise HTTPException(404, "mapper not found")
        assert_tenant_owns(principal, m.tenant_id)
        return _mapper_to_dict(m)


@router.get("/mapper/{mapper_id}/file")
def mapper_file(mapper_id: int,
                principal: Principal = Depends(current_principal)):
    """Download the original BDX workbook that was uploaded when this
    mapper was generated."""
    with SessionLocal() as s:
        m = s.get(Mapper, mapper_id)
        if not m:
            raise HTTPException(404, "mapper not found")
        assert_tenant_owns(principal, m.tenant_id)
        data = storage.resolve_bytes(m.source_blob_ref, m.source_blob)
        if not data:
            raise HTTPException(404, "no original file stored for this mapper")
        fname = m.source_filename or f"mapper_{mapper_id}.xlsx"
        return Response(
            content=data,
            media_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if fname.lower().endswith(".xlsx")
                else "application/octet-stream"
            ),
            headers={"Content-Disposition": _content_disposition(fname)},
        )


@router.post("/bdx/preview")
async def bdx_preview(
    mga: str = Form(...),
    file: UploadFile = File(...),
    skip_rows: int = Form(default=0),
    sheets: Optional[str] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    try:
        _raw = await file.read()
        sheets_dict = await run_in_threadpool(read_excel_all_sheets, _raw, skip_rows)
    except Exception as e:
        raise HTTPException(400, f"could not parse uploaded file: {e}")
    sheets_dict = _filter_sheets(sheets_dict, sheets)
    sig = signature_multi(sheets_dict)
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        m = _find_mapper(s, tid, sig)
        if not m:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "no_matching_mapper",
                    "message": "File format does not match any saved mapper for this MGA. "
                               "Create or update a mapping via /mapper/generate.",
                    "signature": sig,
                },
            )
        per_sheet = apply_spec_multi(sheets_dict, _resolve_spec_by_sheet(m))
        preview = {sheet: rows[:50] for sheet, rows in per_sheet.items()}
        counts = {sheet: len(rows) for sheet, rows in per_sheet.items()}
        return {
            "mapper_id": m.id,
            "sheets": list(sheets_dict.keys()),
            "rows": preview,
            "counts": counts,
            "total": sum(counts.values()),
        }


@router.get("/extra-fields")
def extra_fields_list(mga: str, p: Principal = Depends(current_principal)):
    """Return every extra-field definition visible to this tenant:
    own (from tenant.internal_codes.extras) + any shared definitions from
    other tenants. Used by the Mapping page and the Output Template
    canonical-field dropdown."""
    from db import CanonicalSession
    from extras import list_definitions
    with CanonicalSession() as cs:
        tid = resolve_tenant_id(cs, p, mga)
        return list_definitions(cs, tid, include_shared=True)


@router.post("/extra-fields")
def extra_fields_create(mga: str, body: ExtraFieldBody,
                        p: Principal = Depends(current_principal)):
    from db import CanonicalSession
    from extras import upsert_definition, normalise_key
    with CanonicalSession() as cs:
        tid = resolve_tenant_id(cs, p, mga)
        result = upsert_definition(cs, tid, normalise_key(body.key),
                                   body.model_dump())
        cs.commit()
        _log(mga, None, "extra_field_saved", target=normalise_key(body.key))
        return result


@router.post("/extra-fields/{key}/adopt")
def extra_fields_adopt(mga: str, key: str, p: Principal = Depends(current_principal)):
    from db import CanonicalSession
    from extras import adopt_definition
    with CanonicalSession() as cs:
        tid = resolve_tenant_id(cs, p, mga)
        res = adopt_definition(cs, tid, key)
        if res is None:
            raise HTTPException(404, "no shared extra-field with that key")
        cs.commit()
        _log(mga, None, "extra_field_adopted", target=key)
        return res


@router.get("/mappers/{mapper_id}/sheet-bindings")
def get_sheet_bindings(mapper_id: int,
                       principal: Principal = Depends(current_principal)):
    """Return saved sheet bindings for a BDX format; sheets without a saved
    binding get a name-matched PROPOSAL the user can confirm."""
    with SessionLocal() as s:
        mp = s.get(Mapper, mapper_id)
        if not mp:
            raise HTTPException(404, "mapper not found")
        assert_tenant_owns(principal, mp.tenant_id)
        saved = {b.sheet_name: b
                 for b in s.query(SheetBinding).filter(SheetBinding.mapper_id == mapper_id).all()}
        out = []
        for sh in _mapper_sheet_names(mp):
            if sh in saved:
                out.append(_sb_binding_dict(saved[sh]))
            else:
                sched, role = _sb_propose_schedule(sh)
                out.append({
                    "sheet_name": sh, "role": role, "schedule_key": sched,
                    "contract_id": None, "output_template_id": None,
                    "depends_on": [], "reference_doc_ids": [],
                    "approved": False, "proposed": True,
                })
        # Include any saved bindings for sheets not in the mapper's current list.
        for sh, b in saved.items():
            if sh not in {o["sheet_name"] for o in out}:
                out.append(_sb_binding_dict(b))
        return {"mapper_id": mapper_id, "bindings": out}


@router.put("/mappers/{mapper_id}/sheet-bindings")
def put_sheet_bindings(mapper_id: int, body: dict = Body(...),
                       principal: Principal = Depends(current_principal)):
    """Upsert the saved sheet bindings for a BDX format."""
    items = body.get("bindings") or []
    with SessionLocal() as s:
        mp = s.get(Mapper, mapper_id)
        if not mp:
            raise HTTPException(404, "mapper not found")
        assert_tenant_owns(principal, mp.tenant_id)
        existing = {b.sheet_name: b
                    for b in s.query(SheetBinding).filter(SheetBinding.mapper_id == mapper_id).all()}
        for it in items:
            sh = it.get("sheet_name")
            if not sh:
                continue
            b = existing.get(sh) or SheetBinding(mapper_id=mapper_id, sheet_name=sh)
            b.role = it.get("role") or "schedule"
            b.schedule_key = it.get("schedule_key")
            b.contract_id = it.get("contract_id")
            b.output_template_id = it.get("output_template_id")
            b.depends_on = it.get("depends_on") or []
            b.reference_doc_ids = it.get("reference_doc_ids") or []
            b.approved = 1 if it.get("approved") else 0
            if b.id is None:
                s.add(b)
        s.commit()
        rows = s.query(SheetBinding).filter(SheetBinding.mapper_id == mapper_id).all()
        return {"mapper_id": mapper_id, "bindings": [_sb_binding_dict(b) for b in rows]}


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

        return {"ok": True, "applied": applied, "recordsVersioned": len(groups),
                "results": results, "sql": "\n\n".join(sqls)}
