"""Route handlers owned by tenant-admin-service. Extracted from: app_routes.py."""
from __future__ import annotations
from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool
import storage  # blob storage abstraction
from common_app_routes import *

router = APIRouter()


@router.get("/onboarding/status")
def onboarding_status(mga: str, p: Principal = Depends(current_principal)):
    """Drive the four-step first-login flow:
      1. Tenant setup (legal name + tenant_type filled in).
      2. Party directory (at least one party added).
      3. Contract upload (at least one contract attached to a program).
      4. Raw BDX upload (at least one ingestion).
    `needs_onboarding` is true until ALL four are done."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, p, mga)
        tc = s.query(Tenant).filter(Tenant.id == tid).first() if tid else None
        # Tenant is considered configured once legal_name, tenant_type and
        # currency are all set. (The previous check that rejected
        # legal_name == mga was too aggressive — a real "Aurenity" tenant
        # IS named "Aurenity", so saving never flipped the flag.)
        tenant_ready = bool(
            tc and (tc.legal_name or "").strip()
            and tc.tenant_type and tc.currency
        )
        # Onboarding asks "did the user set these up in the app", so count only
        # app-created rows (is_app_managed) / real ingests (mapper_id), not the
        # BDX-ingested canonical rows.
        has_party = bool(tid) and (s.query(exists().where(
            and_(Party.tenant_id == tid, Party.is_app_managed.is_(True)))).scalar() or False)
        has_contract = bool(tid) and (s.query(exists().where(
            and_(Contract.tenant_id == tid, Contract.is_app_managed.is_(True)))).scalar() or False)
        has_program = bool(tid) and (s.query(exists().where(
            and_(Program.tenant_id == tid, Program.is_app_managed.is_(True)))).scalar() or False)
        has_upload = bool(tid) and (s.query(exists().where(
            and_(Upload.tenant_id == tid, Upload.mapper_id.isnot(None)))).scalar() or False)
        # Treat "BDX configured" as: either the user fully ingested a file OR
        # they approved a mapping spec (= format setup done). The onboarding
        # step is about getting the first BDX through the AI mapping flow.
        has_mapper = bool(tid) and (
            s.query(exists().where(Mapper.tenant_id == tid)).scalar() or False
        )
        # A configured Bordereau Setup = an APPROVED direct-lane format. It bundles
        # input + output + contract in one place, so it satisfies the format-setup
        # step on its own (and implies a contract + a carrier party were created).
        bordereau_ready = bool(tid) and (s.query(exists().where(
            and_(DirectFormat.tenant_id == tid, DirectFormat.approved == 1))).scalar()
            or False)
        bdx_ready = bool(has_upload or has_mapper or bordereau_ready)
        # Onboarding is complete via EITHER path: the new consolidated Bordereau
        # Setup, or the legacy (contract + sample-BDX mapper) flow.
        new_done = tenant_ready and has_party and bordereau_ready
        legacy_done = tenant_ready and has_party and has_contract and bdx_ready
        return {
            "tenant_ready": tenant_ready,
            "parties_ready": bool(has_party),
            "contract_ready": bool(has_contract),
            "bordereau_ready": bordereau_ready,
            "bdx_ready": bdx_ready,
            # Backward-compat with previous keys
            "has_program": bool(has_program),
            "has_contract": bool(has_contract),
            "has_upload": bool(has_upload),
            "needs_onboarding": not (new_done or legacy_done),
        }


@router.get("/tenants")
def tenants_list(_p: Principal = Depends(require_role("kavachio_admin"))):
    """List every tenant on the platform (Kavachio platform-admin "Tenants"
    screen). Includes per-tenant counts of users and approved (active) setups.
    Platform-admin only — enforced server-side, not just by the UI."""
    with SessionLocal() as s:
        out = []
        for t in s.query(Tenant).order_by(Tenant.tenant_name).all():
            users = s.query(func.count(AppUser.id)).filter(
                AppUser.tenant_id == t.id).scalar() or 0
            setups = s.query(func.count(DirectFormat.id)).filter(
                DirectFormat.tenant_id == t.id, DirectFormat.approved == 1).scalar() or 0
            out.append({
                "mga": t.tenant_name,
                "name": t.legal_name or (t.tenant_name or "").title(),
                "code": t.tenant_name,
                "tenant_type": t.tenant_type,
                "users": int(users),
                "setups": int(setups),
                "is_active": bool(t.is_active),
            })
        return out


@router.post("/tenants")
def tenants_create(body: NewTenantBody,
                   _p: Principal = Depends(require_role("kavachio_admin"))):
    """Provision a new tenant (Kavachio "Add tenant" screen) and, optionally,
    invite its first admin. The account code (tenant_name) is auto-slugged from
    the organization name. Platform-admin only."""
    import re
    from ingester import _ensure_tenant
    slug = re.sub(r"[^a-z0-9]+", "-", (body.name or "").strip().lower()).strip("-") or "tenant"
    with SessionLocal() as s:
        if s.query(Tenant).filter(Tenant.tenant_name == slug).first():
            raise HTTPException(409, "a tenant with a similar name already exists")
        # _ensure_tenant supplies the canonical NOT NULL defaults.
        tid = _ensure_tenant(s, slug)
        s.flush()
        t = s.query(Tenant).filter(Tenant.id == tid).first()
        t.legal_name = body.name.strip()
        if body.tenant_type:
            t.tenant_type = body.tenant_type
        if body.currency:
            t.currency = body.currency
        t.is_active = bool(body.is_active if body.is_active is not None else True)
        if body.admin_email:
            if not s.query(AppUser).filter(AppUser.email == body.admin_email.strip().lower()).first():
                s.add(AppUser(
                    email=body.admin_email.strip().lower(),
                    full_name=body.admin_name or body.admin_email.split("@")[0].title(),
                    role="admin", status="active", tenant_id=tid))
        s.commit(); s.refresh(t)
        _log(slug, None, "tenant_created", target=slug)
        return _tenant_dict(t)


@router.get("/tenants/{mga}")
def tenant_get(mga: str, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        t = _get_or_create_tenant(s, mga)
        # A tenant_admin can only read their OWN tenant; kavachio_admin any.
        # (Tenant maps its PK column `tenant_id` to the ORM attr `.id`.)
        assert_tenant_owns(principal, t.id)
        s.commit(); s.refresh(t)
        return _tenant_dict(t)


@router.put("/tenants/{mga}")
def tenant_update(mga: str, body: TenantBody,
                  principal: Principal = Depends(require_role("tenant_admin"))):
    with SessionLocal() as s:
        t = _get_or_create_tenant(s, mga)
        # Edits are limited to the caller's own tenant; kavachio_admin any.
        # (Tenant maps its PK column `tenant_id` to the ORM attr `.id`.)
        assert_tenant_owns(principal, t.id)
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(t, k, v)
        s.commit(); s.refresh(t)
        _log(mga, None, "tenant_updated", target=mga)
        return _tenant_dict(t)


@router.get("/parties")
def parties_list(
    mga: str,
    q: Optional[str] = None,
    party_type: Optional[str] = None,
    scope: Optional[str] = None,
    include_inactive: bool = False,
    p: Principal = Depends(current_principal),
):
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, p, mga)
        # Directory shows app-created parties only (+ shared globals); BDX-ingested
        # canonical parties are excluded via is_app_managed (was the old mga filter).
        query = s.query(Party).filter(or_(
            and_(Party.tenant_id == tid, Party.is_app_managed.is_(True)),
            Party.scope == "global"))
        if not include_inactive:
            # Selectors must never offer a deactivated party, so active-only is the
            # default; the directory opts in to list (and re-activate) them. NULL
            # predates the column default and still counts as active.
            query = query.filter(or_(Party.is_active.is_(True),
                                     Party.is_active.is_(None)))
        if q:
            ql = f"%{q.lower()}%"
            query = query.filter(or_(
                func.lower(Party.legal_name).like(ql),
                func.lower(Party.dba_name).like(ql),
            ))
        if party_type:
            query = query.filter(Party.party_type == party_type)
        if scope:
            query = query.filter(Party.scope == scope)
        rows = [_party_dict(p, mga if p.scope != "global" else None)
                for p in query.order_by(Party.legal_name).all()]
        from_global = sum(1 for r in rows if r["scope"] == "global")
        return {"total": len(rows), "from_global": from_global, "items": rows}


@router.post("/parties")
def parties_create(mga: str, body: PartyBody,
                   principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        # Tenant comes from the trusted token, never the client. (For a logged-in
        # user the tenant row already exists, so no _ensure_tenant create is
        # needed; the party_scope_tenant_chk constraint is satisfied.)
        tenant_id = resolve_tenant_id(s, principal, mga)
        p = Party(tenant_id=tenant_id,
                  **body.model_dump(exclude_unset=True))
        s.add(p); s.commit(); s.refresh(p)
        _log(mga, None, "party_created", target=str(p.id))
        return _party_dict(p, mga)


@router.get("/parties/{party_id}")
def parties_get(party_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        p = s.get(Party, party_id)
        if not p:
            raise HTTPException(404, "party not found")
        assert_tenant_owns(principal, p.tenant_id)
        return _party_dict(p, _tenant_name(s, p.tenant_id))


@router.put("/parties/{party_id}")
def parties_update(party_id: int, body: PartyBody,
                   principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        p = s.get(Party, party_id)
        if not p:
            raise HTTPException(404, "party not found")
        assert_tenant_owns(principal, p.tenant_id)
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(p, k, v)
        s.commit(); s.refresh(p)
        mga = _tenant_name(s, p.tenant_id)
        _log(mga, None, "party_updated", target=str(p.id))
        return _party_dict(p, mga)


@router.get("/parties/{party_id}/contacts")
def party_contacts_list(party_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        party = s.get(Party, party_id)
        if not party:
            raise HTTPException(404, "party not found")
        assert_tenant_owns(principal, party.tenant_id)
        rows = s.query(PartyContact).filter(PartyContact.party_id == party_id).all()
        return [{"id": c.id, "full_name": c.full_name, "title": c.title,
                 "email": c.email, "phone": c.phone} for c in rows]


@router.post("/parties/{party_id}/contacts")
def party_contacts_create(party_id: int, body: PartyContactBody,
                          principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        p = s.get(Party, party_id)
        if not p:
            raise HTTPException(404, "party not found")
        assert_tenant_owns(principal, p.tenant_id)
        c = PartyContact(party_id=party_id, tenant_id=p.tenant_id, **body.model_dump())
        s.add(c); s.commit(); s.refresh(c)
        return {"id": c.id, "full_name": c.full_name, "title": c.title,
                "email": c.email, "phone": c.phone}


@router.delete("/parties/{party_id}/contacts/{contact_id}")
def party_contacts_delete(party_id: int, contact_id: int,
                          principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        c = s.get(PartyContact, contact_id)
        if not c or c.party_id != party_id:
            raise HTTPException(404, "contact not found")
        assert_tenant_owns(principal, c.tenant_id)
        s.delete(c); s.commit()
        return {"ok": True}


@router.get("/parties/{party_id}/programs")
def party_programs_list(party_id: int, principal: Principal = Depends(current_principal)):
    """Return programs linked to this party, each with their contracts list."""
    with SessionLocal() as s:
        party = s.get(Party, party_id)
        if not party:
            raise HTTPException(404, "party not found")
        assert_tenant_owns(principal, party.tenant_id)
        programs = s.query(Program).filter(Program.party_id == party_id).order_by(Program.name).all()
        result = []
        for p in programs:
            contracts = s.query(Contract).filter(Contract.program_id == p.id).all()
            pd = _program_dict(p, _tenant_name(s, p.tenant_id))
            pd["contracts"] = [
                {"id": c.id, "filename": c.filename, "status": c.status,
                 "created_at": _iso_utc(c.created_at)}
                for c in contracts
            ]
            result.append(pd)
        return result


@router.get("/programs")
def programs_list(mga: str, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        return [_program_dict(p, mga) for p in
                s.query(Program).filter(Program.tenant_id == tid,
                                        Program.is_app_managed.is_(True))
                .order_by(Program.name).all()]


@router.post("/programs")
def programs_create(mga: str, body: ProgramBody,
                    principal: Principal = Depends(current_principal)):
    if not body.name:
        raise HTTPException(400, "name required")
    with SessionLocal() as s:
        p = Program(tenant_id=resolve_tenant_id(s, principal, mga),
                    **body.model_dump(exclude_unset=True))
        s.add(p); s.commit(); s.refresh(p)
        _log(mga, None, "program_created", target=str(p.id))
        return _program_dict(p, mga)


@router.get("/programs/{program_id}")
def programs_get(program_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        p = s.get(Program, program_id)
        if not p:
            raise HTTPException(404, "program not found")
        assert_tenant_owns(principal, p.tenant_id)
        return _program_dict(p, _tenant_name(s, p.tenant_id))


@router.put("/programs/{program_id}")
def programs_update(program_id: int, body: ProgramBody,
                    principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        p = s.get(Program, program_id)
        if not p:
            raise HTTPException(404, "program not found")
        assert_tenant_owns(principal, p.tenant_id)
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(p, k, v)
        s.commit(); s.refresh(p)
        mga = _tenant_name(s, p.tenant_id)
        _log(mga, None, "program_updated", target=str(p.id))
        return _program_dict(p, mga)


@router.post("/programs/{program_id}/setup")
async def program_setup(
    program_id: int,
    contract_file: UploadFile = File(...),
    template_file: Optional[UploadFile] = File(default=None),
    template_name: Optional[str] = Form(default=None),
    output_template_id: Optional[int] = Form(default=None),
    continue_anyway: bool = Form(default=False),
    resume_token: Optional[str] = Form(default=None),
    reference_files: Optional[list[UploadFile]] = File(default=None),
    principal: Principal = Depends(current_principal),
):
    """Combined output-template + contract upload in one request.

    New architecture:
      1. Output Template is created/resolved first (it is the semantic bridge).
      2. Contract is then processed with the Output Template fields as context,
         so the LLM maps contract clauses → Output Template fields
         (not to the canonical data model directly).
      3. Contract is activated within Output Template scope.

    Either supply `template_file` (create a new Output Template) or
    `output_template_id` (use an existing one). If both are given, the
    new template takes precedence. At least one must be provided.

    Returns:
      { contract: {...}, template: {...} | None, program: {...} }
    """

    # ── 1. Resolve or create the Output Template first ───────────────────────
    template_result = None
    resolved_template_id = output_template_id

    if template_file and template_file.filename:
        template_bytes = await template_file.read()
        try:
            from exporter import parse_template, propose_template_mapping

            with SessionLocal() as s:
                prog = s.get(Program, program_id)
                if not prog:
                    raise HTTPException(404, "program not found")
                assert_tenant_owns(principal, prog.tenant_id)
                tid_code = prog.tenant_id

                carrier_party_id = prog.party_id if prog else None
                carrier_name = None
                if carrier_party_id:
                    cp = s.get(Party, carrier_party_id)
                    carrier_name = cp.legal_name if cp else None

                structure = await run_in_threadpool(parse_template, template_bytes, filename=template_file.filename)
                if structure.get("sheets"):
                    await run_in_threadpool(propose_template_mapping, structure)
                    tname = (template_name or "").strip() or \
                        template_file.filename.rsplit(".", 1)[0]

                    siblings = s.query(ExportTemplate).filter(
                        ExportTemplate.tenant_id == tid_code,
                        ExportTemplate.name == tname,
                    ).all()
                    version = (max((r.version or 1) for r in siblings) + 1) if siblings else 1
                    is_active = 0 if siblings else 1

                    tmpl_ref, tmpl_bytes = await run_in_threadpool(
                        storage.store_or_keep, "templates", tid_code,
                        template_file.filename, template_bytes,
                    )
                    t = ExportTemplate(
                        tenant_id=tid_code, name=tname,
                        version=version, is_active=is_active,
                        carrier=carrier_name,
                        carrier_party_id=carrier_party_id,
                        structure=structure,
                        template_blob=tmpl_bytes,
                        template_blob_ref=tmpl_ref,
                        approved=0,
                    )
                    s.add(t)
                    s.commit()
                    s.refresh(t)
                    resolved_template_id = t.id
                    template_result = {
                        "id": t.id, "name": t.name, "version": t.version,
                        "sheets": [sh["sheet_name"] for sh in (structure.get("sheets") or [])],
                    }
        except Exception as te:
            template_result = {"error": str(te)}

    if not resolved_template_id:
        raise HTTPException(
            status_code=400,
            detail="Provide either template_file (to create a new Output Template) "
                   "or output_template_id (to use an existing one). "
                   "A contract cannot be created without an Output Template.",
        )

    # ── 2. Fetch Output Template fields for template-aware LLM extraction ─────
    template_fields: list = []
    with SessionLocal() as s:
        tmpl = s.get(ExportTemplate, resolved_template_id)
        if not tmpl:
            raise HTTPException(404, f"Output Template {resolved_template_id} not found")
        # Shared builder → carries data-dictionary enrichment (description,
        # allowed_values, format, required) through to the LLM mapper.
        template_fields = _template_fields_from_structure(tmpl.structure)

    # ── 3. Process the contract with template-aware LLM mapping ───────────────
    contract_result = None
    halted_external_refs = None

    suffix = os.path.splitext(contract_file.filename or "contract.pdf")[1]
    contract_bytes = await contract_file.read()

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(contract_bytes)
        tmp_path = tmp.name

    try:
        from contract_upload_services.upload_file_validator import (
            validate_uploaded_contract, UploadContractValidationError,
        )
        try:
            validate_uploaded_contract(
                file=contract_file, file_bytes=contract_bytes,
                temp_file_path=tmp_path,
            )
        except UploadContractValidationError as ve:
            raise HTTPException(status_code=400, detail=ve.to_detail())

        # ── Exact re-upload reuse (no LLM) — mirror of the /contracts route ──
        # Fingerprint the document + template; if an identical upload already
        # exists for this program, reuse its contract + rules and skip every LLM
        # call. Without this, /setup re-inserts a fresh contract + full clause
        # set on every upload. Skipped on a halted-extraction resume (that path
        # reuses the cached extraction, not a prior contract).
        from contract_upload_services.contract_versioning import (
            compute_content_fingerprint,
            compute_entity_fingerprint,
            find_reusable_contract,
        )
        from contract_upload_services.document_extractors import extract_document_data
        from contract_upload_services.prompt_builder import build_llm_context

        content_fp = None
        entity_fp = None
        if not (continue_anyway or resume_token):
            parsed_doc = await run_in_threadpool(extract_document_data, tmp_path)
            document_text = build_llm_context(parsed_doc)
            content_fp = compute_content_fingerprint(
                document_text, template_fields, resolved_template_id
            )
            entity_fp = compute_entity_fingerprint(program_id, contract_file.filename)

            # Set KAVACHIO_DISABLE_CONTRACT_REUSE=1 to force a full re-run (skip the
            # identical-upload short-circuit) — useful when testing pipeline changes.
            reusable = (None if os.getenv("KAVACHIO_DISABLE_CONTRACT_REUSE")
                        else find_reusable_contract(program_id, content_fp))
            if reusable:
                cid = reusable["contract_id"]
                print(
                    f"[Setup] Identical re-upload detected "
                    f"(content_fingerprint={content_fp[:12]}…) — reusing "
                    f"contract_id={cid}; skipping all LLM calls."
                )
                with SessionLocal() as s:
                    # Re-point the reused contract to the CURRENT output template
                    # and make it active, so the template page
                    # (GET /export/template/{id}/contract-mapping — which finds the
                    # active contract WHERE output_template_id = template_id) locates
                    # it and shows its rules. Reuse only fires when the template
                    # field-set matches, so re-pointing is safe — the rules
                    # reference the same fields.
                    c = s.get(Contract, cid)
                    if c:
                        c.output_template_id = resolved_template_id
                        c.status = "active"
                        for sib in s.query(Contract).filter(
                            Contract.program_id == program_id,
                            Contract.id != cid,
                            Contract.status.notin_(["failed", "drafted", "extracting"]),
                        ).all():
                            sib.status = "superseded"
                        s.commit()
                    prog = s.get(Program, program_id)
                    program_obj = _program_dict(prog) if prog else None
                    c = s.get(Contract, cid)
                    contract_result = {
                        "id": c.id, "filename": c.filename, "status": c.status,
                        "extracted": c.extracted,
                        "output_template_id": c.output_template_id,
                        "created_at": _iso_utc(c.created_at),
                    } if c else None
                return {
                    "status": "ok",
                    "reused": True,
                    "contract": contract_result,
                    "template": template_result,
                    "program": program_obj,
                }

        # ── Reference documents: extract their text (same as the contract) so
        # the extraction LLM can resolve clauses that defer to them. ──────────
        reference_documents = await _extract_reference_documents(
            reference_files, program_id=program_id, tenant_id=principal.tenant_id)
        has_reference_files = bool(reference_documents)
        if has_reference_files:
            print(
                f"[Setup] {len(reference_documents)} reference document(s) provided: "
                f"{[rd['name'] for rd in reference_documents]}"
            )

        # LLM maps contract clauses → Output Template fields.
        # HALT GATE: when the contract DEFERS rule content to an external document
        # (e.g. "Authorized / Targeted / Excluded Classes of Business: per the
        # Facultative Purchasing Guidelines on file with the Company"), pause and
        # ask the user to upload that document so the deferred clauses can be
        # resolved into concrete, checkable rules instead of being silently
        # dropped. Do NOT halt when:
        #   • the user already uploaded reference doc(s) on THIS request
        #     (has_reference_files) — they are fed to the extractor and used; or
        #   • the user chose "Continue Anyway" (continue_anyway) — proceed with the
        #     contract text alone, resuming from the cached extraction.
        halt = not continue_anyway and not has_reference_files
        extraction_output = await run_in_threadpool(
            contract_service.process_contract,
            tmp_path,
            template_fields=template_fields if template_fields else None,
            halt_on_external_references=halt,
            resume_token=resume_token if continue_anyway else None,
            reference_documents=reference_documents or None,
        )

        # Pipeline paused: return the referenced document names for the popup.
        # Nothing is persisted; the user uploads the reference doc or retries
        # this endpoint with continue_anyway=true.
        if isinstance(extraction_output, dict) and extraction_output.get("halted_for_references"):
            halted_external_refs = extraction_output.get("external_references", [])
            print(
                f"[Setup] HALTED for {len(halted_external_refs)} external "
                f"reference(s) — awaiting user (upload reference / continue anyway)."
            )
        else:
            from contract_upload_services.db_persister import persist_pipeline_output
            persist_result = None
            try:
                persist_result = persist_pipeline_output(
                    extraction_output, program_id, contract_file.filename,
                    output_template_id=resolved_template_id,
                    content_fingerprint=content_fp,
                    entity_fingerprint=entity_fp,
                )
            except Exception as pe:
                print(f"[Persist] ERROR (non-fatal): {pe}")

            with SessionLocal() as s:
                cid = (persist_result or {}).get("contract_id")
                if cid:
                    # One active contract per Program — supersede all others
                    siblings = s.query(Contract).filter(
                        Contract.program_id == program_id,
                        Contract.id != cid,
                        Contract.status.notin_(["failed", "drafted", "extracting"]),
                    ).all()
                    for sib in siblings:
                        sib.status = "superseded"
                    new_c = s.get(Contract, cid)
                    if new_c:
                        new_c.status = "active"
                    s.commit()
                    c = s.get(Contract, cid)
                    contract_result = {
                        "id": c.id, "filename": c.filename, "status": c.status,
                        "extracted": c.extracted,
                        "output_template_id": c.output_template_id,
                        "created_at": _iso_utc(c.created_at),
                    } if c else None

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    with SessionLocal() as s:
        prog = s.get(Program, program_id)
        program_obj = _program_dict(prog) if prog else None

    # Halted on external references — prompt the user (no contract persisted yet).
    if halted_external_refs is not None:
        return {
            "status": "references_required",
            "external_references": halted_external_refs,
            "resume_token": (extraction_output or {}).get("resume_token"),
            "contract": None,
            "template": template_result,
            "program": program_obj,
        }

    return {
        "status": "ok",
        "contract": contract_result,
        "template": template_result,
        "program": program_obj,
    }


@router.get("/dashboard/stats")
def dashboard_stats(mga: str, principal: Principal = Depends(current_principal)):
    """Aggregates for the home dashboard."""
    today = datetime.utcnow().date()
    week_ago = datetime.utcnow() - timedelta(days=7)
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        # `upload` is overloaded (§2a): count only real ingest rows (mapper_id set),
        # not the canonical lineage shadow rows — matches the /uploads list.
        uploads_today = s.query(Upload).filter(
            Upload.tenant_id == tid,
            Upload.mapper_id.isnot(None),
            func.date(Upload.ingested_at) == today,
        ).count()
        total_uploads = s.query(Upload).filter(
            Upload.tenant_id == tid, Upload.mapper_id.isnot(None)).count()
        programs = s.query(Program).filter(Program.tenant_id == tid,
                                           Program.is_app_managed.is_(True),
                                           Program.status == "active").count()
        # "Active setups" = activated output templates (a carrier+program config
        # that's been activated to generate BDX), plus the carriers they span.
        active_setups = s.query(ExportTemplate).filter(
            ExportTemplate.tenant_id == tid, ExportTemplate.is_active == 1).count()
        active_setup_carriers = s.query(
            func.count(func.distinct(ExportTemplate.carrier_party_id))
        ).filter(
            ExportTemplate.tenant_id == tid, ExportTemplate.is_active == 1,
            ExportTemplate.carrier_party_id.isnot(None),
        ).scalar() or 0
        parties = s.query(Party).filter(
            or_(and_(Party.tenant_id == tid, Party.is_app_managed.is_(True)),
                Party.scope == "global")
        ).count()

        # A "run" is a generated output (OutputExport), which carries the
        # validation result — matches the dashboard's "Recent runs" table.
        # "Runs this week" tile + a 7-day daily series (oldest → newest) for the spark.
        week_rows = s.query(
            func.date(OutputExport.created_at), func.count(OutputExport.id)
        ).filter(
            OutputExport.tenant_id == tid,
            OutputExport.created_at >= week_ago,
        ).group_by(func.date(OutputExport.created_at)).all()
        by_date = {str(d): c for d, c in week_rows}
        runs_by_day = [by_date.get(str(today - timedelta(days=i)), 0)
                       for i in range(6, -1, -1)]
        runs_this_week = sum(runs_by_day)

        # "Exceptions to review" tile — real exception totals across generated
        # outputs (replaces the previous hardcoded 0). Counts every flagged
        # exception; per-exception resolution state is not yet subtracted.
        exc_sum, exc_runs = s.query(
            func.coalesce(func.sum(OutputExport.exception_count), 0),
            func.count(OutputExport.id),
        ).filter(
            OutputExport.tenant_id == tid,
            OutputExport.status == "has_exceptions",
        ).one()

        # "Mapping tasks" tile (Kavachio admin) — open items in the data-model queue.
        mapping_tasks_open = s.query(AdminMappingTask).filter(
            AdminMappingTask.tenant_id == tid,
            AdminMappingTask.status.in_(("open", "in_progress")),
        ).count()

        return {
            "uploads_today": uploads_today,
            "uploads_total": total_uploads,
            "open_bdx_cycles": programs,
            "active_setups": active_setups,             # "Active setups" tile
            "active_setup_carriers": int(active_setup_carriers),
            "parties_in_directory": parties,
            "pending_exceptions": int(exc_sum or 0),
            "exception_runs": int(exc_runs or 0),
            "ai_cache_hit_rate": None,
            "runs_this_week": runs_this_week,
            "runs_by_day": runs_by_day,
            "mapping_tasks_open": mapping_tasks_open,
        }


@router.get("/activity")
def activity_list(mga: str, limit: int = 25,
                  principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        rows = (
            s.query(ActivityEvent)
            .filter(ActivityEvent.tenant_id == tid)
            .order_by(desc(ActivityEvent.created_at))
            .limit(limit).all()
        )
        return [{"id": e.id, "actor": e.actor, "action": e.action,
                 "target": e.target, "details": e.details,
                 "created_at": _iso_utc(e.created_at)}
                for e in rows]
