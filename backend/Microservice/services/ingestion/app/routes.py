"""Route handlers owned by ingestion-service. Extracted from: direct_routes.py, main.py."""
from __future__ import annotations
from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool
import storage  # blob storage abstraction (Azure/Azurite with DB fallback)
from common_direct_routes import *
from common_main import *

router = APIRouter()


@router.post("/bdx/upload")
async def bdx_upload(
    mga: str = Form(...),
    file: UploadFile = File(...),
    skip_rows: int = Form(default=0),
    sheets: Optional[str] = Form(default=None),  # comma-separated sheet names from UI
    party_id: Optional[int] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    file_bytes = await file.read()
    try:
        sheets_dict = await run_in_threadpool(read_excel_all_sheets, file_bytes, skip_rows)
    except Exception as e:
        raise HTTPException(400, f"could not parse uploaded file: {e}")
    # Honour the user's sheet selection — filters BEFORE signature so the
    # signature matches the mapper that was trained on the same sheet set.
    sheets_dict = _filter_sheets(sheets_dict, sheets)
    sig = signature_multi(sheets_dict)
    with SessionLocal() as s:
        # Authoritative tenant from the trusted token (client `mga` ignored for
        # regular users). Used to scope the mapper lookup AND stamp all new rows.
        tid = resolve_tenant_id(s, principal, mga)
        auth_mga = _tenant_name(s, tid) or mga
        m = _find_mapper(s, tid, sig)
        if not m:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "no_matching_mapper",
                    "message": "File format does not match any saved mapper for this MGA.",
                    "signature": sig,
                },
            )

        per_sheet = apply_spec_multi(sheets_dict, _resolve_spec_by_sheet(m))
        ingested_by_sheet = {sh: len(rows) for sh, rows in per_sheet.items()}

        # Phase 2 — resolve each sheet's schedule from its saved binding. Guarded
        # so an un-migrated DB (no bdx_sheet_binding table) behaves exactly as
        # before: no bindings → schedule=None everywhere → single-scope merge.
        sheet_bindings = _sheet_bindings_for_mapper(s, m.id)

        def _skip_sheet(sh: str) -> bool:
            b = sheet_bindings.get(sh)
            return bool(b) and (b.get("role") in ("ignore", "summary", "check"))

        # Flatten across sheets (skipping non-policy sheets when bound) and merge
        # by (schedule, policy_number) so different schedules never collapse.
        flat, scopes = [], []
        for sh, rows in per_sheet.items():
            if _skip_sheet(sh):
                continue
            sched = (sheet_bindings.get(sh) or {}).get("schedule_key")
            for r in rows:
                flat.append(r)
                scopes.append(sched)
        merged = _merge_records(flat, scopes if any(scopes) else None)

        # The upload table now requires tenant_id — stamp it from the TRUSTED
        # token, never from the client-supplied `mga`.
        tenant_id_for_upload = tid

        # Persist the original workbook to blob storage (Azure/Azurite) when
        # enabled; otherwise keep it inline in source_blob (legacy behaviour).
        upload_blob_ref, upload_blob_bytes = await run_in_threadpool(
            storage.store_or_keep, "uploads", tenant_id_for_upload,
            file.filename, file_bytes,
        )

        upload = Upload(
            mapper_id=m.id, source_file=file.filename,
            sheets=list(sheets_dict.keys()),
            counts_by_sheet=ingested_by_sheet,
            total_rows=sum(ingested_by_sheet.values()),
            source_blob=upload_blob_bytes,
            source_blob_ref=upload_blob_ref,
            tenant_id=tenant_id_for_upload,
            party_id=party_id,
        )
        s.add(upload)
        s.flush()

        # Keep the legacy raw store too (handy for debugging) — one BDXRecord
        # per source row, tagged with the upload.
        for sheet_name, rows in per_sheet.items():
            for r in rows:
                s.add(BDXRecord(
                    upload_id=upload.id, tenant_id=tenant_id_for_upload, mapper_id=m.id,
                    source_file=file.filename, sheet_name=sheet_name, payload=r,
                ))

        # Canonical relational write — runs on the REMOTE Postgres database
        # in its own session/transaction.
        from datetime import datetime as _dt
        now = _dt.utcnow()
        policy_ids: list[int] = []
        ingest_errors: list[dict] = []
        with CanonicalSession() as cs:
            tenant_id = _ensure_tenant(cs, auth_mga)
            canonical_upload_id = _ensure_canonical_upload(
                cs, tenant_id, file.filename or "upload",
                now.year, now.month,
            )
            # Ingest each record inside its own SAVEPOINT so a single bad row
            # (malformed value, unexpected shape, constraint violation) is
            # logged and skipped instead of aborting the entire upload with a
            # 500. Good rows still commit.
            for idx, rec in enumerate(merged):
                try:
                    with cs.begin_nested():
                        pid = ingest_record(cs, auth_mga, rec,
                                            canonical_upload_id=canonical_upload_id)
                    if pid is not None:
                        policy_ids.append(pid)
                except Exception as ingest_exc:
                    polno = (rec.get("policy") or {}).get("policy_number")
                    log.warning(
                        "ingest_record failed for row %d (policy_number=%s): %s",
                        idx, polno, ingest_exc,
                    )
                    ingest_errors.append({
                        "row": idx, "policy_number": polno,
                        "error": str(ingest_exc),
                    })
            cs.commit()

        # Map upload → canonical policies in the local DB.
        for pid in policy_ids:
            s.add(UploadPolicy(upload_id=upload.id, policy_id=pid))

        # Phase 2 — per-upload lineage: record what each ingested sheet was bound
        # to (schedule / contract / output template). Guarded so an un-migrated DB
        # simply skips it.
        try:
            for sh in per_sheet.keys():
                b = sheet_bindings.get(sh)
                if not b:
                    continue
                s.add(UploadSheetContract(
                    upload_id=upload.id, sheet_name=sh,
                    schedule_key=b.get("schedule_key"),
                    contract_id=b.get("contract_id"),
                    output_template_id=b.get("output_template_id"),
                    was_override=False,
                ))
        except Exception as _lin_exc:
            log.warning("upload_sheet_contract lineage skipped: %s", _lin_exc)

        s.commit()
        return {
            "upload_id": upload.id,
            "mapper_id": m.id,
            "ingested_by_sheet": ingested_by_sheet,
            "policies_loaded": len(policy_ids),
            "total_source_rows": upload.total_rows,
            "rows_skipped": len(ingest_errors),
            "ingest_errors": ingest_errors[:20],
        }


@router.get("/uploads")
def uploads_list(mga: Optional[str] = None, limit: int = 50,
                 principal: Principal = Depends(current_principal)):
    """List uploads without loading blob bytes — blob presence checked via IS NOT NULL."""
    with SessionLocal() as s:
        # Select only the lightweight columns; check blob presence with SQL IS NOT NULL
        # so we never transfer the actual file bytes across the network.
        ut = Upload.__table__
        cols = [
            ut.c.upload_id, ut.c.tenant_id, ut.c.mapper_id, ut.c.source_file,
            ut.c.sheets, ut.c.counts_by_sheet, ut.c.total_rows, ut.c.ingested_at,
            case(
                (ut.c.source_blob.isnot(None) | ut.c.source_blob_ref.isnot(None), True),
                else_=False,
            ).label("has_source_blob"),
        ]
        tid = resolve_tenant_id(s, principal, mga)
        # The `upload` table is overloaded (§2a): each ingest writes an ops row
        # (mapper_id/source_file/total_rows set) AND a canonical lineage row
        # (those NULL, filename/num_rows set instead). The list must show only
        # the ops rows — the old `mga = :mga` filter did this implicitly because
        # canonical rows have mga NULL. Now that mga is gone we filter on
        # `mapper_id IS NOT NULL`, which partitions the two row types exactly.
        ops_row = ut.c.mapper_id.isnot(None)
        base = select(*cols).where(ops_row)
        q = s.execute(
            base.where(ut.c.tenant_id == tid)
            .order_by(ut.c.upload_id.desc()).limit(limit)
        )
        rows = q.mappings().all()
        return [
            {
                "id": r["upload_id"], "mga": mga, "tenant_id": r["tenant_id"],
                "mapper_id": r["mapper_id"],
                "source_file": r["source_file"], "sheets": r["sheets"],
                "counts_by_sheet": r["counts_by_sheet"], "total_rows": r["total_rows"],
                "has_source_blob": bool(r["has_source_blob"]),
                "ingested_at": _iso_utc(r["ingested_at"]),
            }
            for r in rows
        ]


@router.get("/uploads/{upload_id}")
def uploads_get(upload_id: int,
                principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        u = s.get(Upload, upload_id)
        if not u:
            raise HTTPException(404, "upload not found")
        assert_tenant_owns(principal, u.tenant_id)
        return _upload_to_dict(u, mga=_tenant_name(s, u.tenant_id))


@router.get("/uploads/{upload_id}/file")
def uploads_file(upload_id: int,
                 principal: Principal = Depends(current_principal)):
    """Download the exact original file the user ingested for this upload."""
    with SessionLocal() as s:
        u = s.get(Upload, upload_id)
        if not u:
            raise HTTPException(404, "upload not found")
        assert_tenant_owns(principal, u.tenant_id)
        data = storage.resolve_bytes(u.source_blob_ref, u.source_blob)
        if not data:
            raise HTTPException(404, "no original file stored for this upload")
        fname = u.source_file or f"upload_{upload_id}.xlsx"
        low = fname.lower()
        if low.endswith(".xlsx"):
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif low.endswith(".csv"):
            media = "text/csv"
        elif low.endswith(".xml"):
            media = "application/xml"
        else:
            media = "application/octet-stream"
        return Response(
            content=data,
            media_type=media,
            headers={"Content-Disposition": _content_disposition(fname)},
        )


@router.get("/uploads/{upload_id}/source-rows")
def uploads_source_rows(upload_id: int, policy_numbers: str = "",
                        principal: Principal = Depends(current_principal)):
    """Return the original rows from the uploaded Excel file for the given
    policy numbers (comma-separated).  Uses the mapper's spec_by_sheet to
    detect which column is the policy-number field.

    Response shape:
      {found: [{policy_number, sheet, row_number, row_data: {col: val}}],
       not_found: [<policy numbers with no matching row>]}
    """
    with SessionLocal() as s:
        u = s.get(Upload, upload_id)
        if not u:
            raise HTTPException(404, "upload not found")
        assert_tenant_owns(principal, u.tenant_id)
        # Resolve bytes inside the session so they survive the session close below.
        source_bytes = storage.resolve_bytes(u.source_blob_ref, u.source_blob)
        if not source_bytes:
            raise HTTPException(404, "no original file stored for this upload")

        mapper = s.get(Mapper, u.mapper_id) if u.mapper_id else None
        spec: dict = (mapper.spec_by_sheet or {}) if mapper else {}

    targets = {p.strip() for p in policy_numbers.split(",") if p.strip()}

    # Parse the source workbook (skip_rows not stored; try 0 first).
    try:
        sheets_dict = read_excel_all_sheets(source_bytes, 0)
    except Exception as e:
        raise HTTPException(400, f"could not parse source file: {e}")

    # Build a lookup: canonical_field → list of (sheet, bare_col_name)
    # spec_by_sheet shape: {sheet: {canonical_field: "Sheet :: Column" | ["S :: C", ...]}}
    SHEET_SEP = " :: "
    def _bare(src: str) -> tuple[str, str]:
        """'Sheet :: Column' → (sheet, column).  Plain 'Column' → ('', column)."""
        if SHEET_SEP in src:
            sh, col = src.split(SHEET_SEP, 1)
            return sh.strip(), col.strip()
        return "", src.strip()

    # Find which source column represents the policy number
    pn_cols: dict[str, str] = {}   # sheet → column_name
    for sheet, mapping in spec.items():
        for canonical, src_val in mapping.items():
            if "policy_number" in canonical.lower():
                srcs = src_val if isinstance(src_val, list) else [src_val]
                for sv in srcs:
                    sh, col = _bare(str(sv))
                    if sh == sheet or not sh:
                        pn_cols[sheet] = col
                        break

    # Fallback: auto-detect policy-number column by header keywords
    PN_KEYWORDS = ["policy ref", "policy no", "policy number", "pol ref", "pol no"]
    for sheet, df in sheets_dict.items():
        if sheet not in pn_cols:
            for col in df.columns:
                if any(kw in str(col).lower() for kw in PN_KEYWORDS):
                    pn_cols[sheet] = str(col)
                    break

    found: list[dict] = []
    found_pns: set[str] = set()

    for sheet_name, df in sheets_dict.items():
        pn_col = pn_cols.get(sheet_name)
        if not pn_col or pn_col not in df.columns:
            continue
        for idx, row in df.iterrows():
            pn = str(row[pn_col]).strip()
            if not targets or pn in targets:
                found_pns.add(pn)
                # Strip NaN / empty values to keep payload lean
                row_data = {
                    str(k): str(v)
                    for k, v in row.items()
                    if str(v).strip() not in ("", "nan", "None", "NaT", "<NA>")
                }
                found.append({
                    "policy_number": pn,
                    "sheet": sheet_name,
                    "row_number": int(idx) + 2,   # 1-based row number including header
                    "row_data": row_data,
                })

    return {
        "found": found,
        "not_found": sorted(targets - found_pns),
    }


@router.get("/dwh")
def dwh_list(
    upload_id: Optional[int] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    principal: Principal = Depends(current_principal),
):
    """Fetch canonical data straight from the relational warehouse.

    Each item is one policy reassembled by joining the canonical tables:
        policy (scalar) ← program (scalar parent)
        + insured_location[] + building[] + coverage[] + premium_transaction[]
        + claim[] + party_role_in_policy[] + …

    Filter by `upload_id` to scope to a specific /bdx/upload call. When
    `upload_id` is supplied, ALL policies for that upload are returned.
    """
    # Look up the canonical policy_ids in the LOCAL ops DB (or in Postgres if
    # no upload_id was supplied).
    if upload_id is not None:
        with SessionLocal() as s:
            # Scope to the caller's tenant: the upload must belong to them (platform
            # admin bypasses) before we hand back its canonical policies.
            u = s.get(Upload, upload_id)
            if not u:
                raise HTTPException(404, "upload not found")
            assert_tenant_owns(principal, u.tenant_id)
            rows = s.execute(
                select(UploadPolicy.policy_id)
                .where(UploadPolicy.upload_id == upload_id)
                .order_by(UploadPolicy.id.asc())
            ).fetchall()
            policy_ids = [r[0] for r in rows]
    else:
        # No upload_id → list recent canonical policies, SCOPED to the caller's
        # tenant (platform admin sees all tenants). The canonical policy table
        # carries tenant_id, so we can filter directly.
        from canonical import CANONICAL_TABLES
        pol = CANONICAL_TABLES["policy"]
        cap = limit if limit is not None else 100
        with CanonicalSession() as cs:
            q = select(pol.c.policy_id).order_by(pol.c.policy_id.desc())
            if not principal.is_platform_admin:
                q = q.where(pol.c.tenant_id == principal.tenant_id)
            rows = cs.execute(q.offset(offset).limit(cap)).fetchall()
            policy_ids = [r[0] for r in rows]

    # Reassemble policies from REMOTE Postgres.
    with CanonicalSession() as cs:
        results = fetch_policies(cs, policy_ids)
    if upload_id is not None and limit is not None:
        end = offset + limit
        results = results[offset:end]
    elif upload_id is not None:
        results = results[offset:]
    return results


@router.post("/direct/peek-sheets")
async def direct_peek_sheets(
    file: UploadFile = File(...),
    kind: str = Form(default="input"),
    skip_rows: int = Form(default=0),
    _p: Principal = Depends(current_principal),
):
    """List the sheet names in an uploaded workbook so the user can pick which
    ones to include. `kind=output` uses the template parser (drops spec/data-
    dictionary sheets, matching what the output template would actually contain);
    `kind=input` lists every readable data sheet."""
    file_bytes = await file.read()
    if kind == "output":
        structure = await run_in_threadpool(parse_template, file_bytes, file.filename)
        names = _output_sheet_names(structure)
    else:
        sheets_dict = await run_in_threadpool(read_excel_all_sheets, file_bytes, skip_rows)
        # Drop data-dictionary / spec sheets — they document the data columns and
        # are not mapping sources, so the input list matches the output list.
        specs = await run_in_threadpool(spec_sheet_names, file_bytes)
        names = [k for k in sheets_dict.keys() if k not in specs]
    return {"sheets": names}


@router.post("/direct/upload")
async def direct_upload(
    mga: str = Form(...),
    file: UploadFile = File(...),
    output_template_id: int = Form(...),
    contract_id: Optional[int] = Form(default=None),
    carrier_party_id: Optional[int] = Form(default=None),
    program_id: Optional[int] = Form(default=None),
    name: Optional[str] = Form(default=None),
    skip_rows: int = Form(default=0),
    selected_sheets: Optional[list[str]] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    """SETUP step: capture an input *template* as a landing record and propose the
    sheet routing + input→output column mapping (scoped to carrier+program).
    Reuses the learned config when the input format has been seen before.

    `selected_sheets` restricts mapping to the sheets the user chose; only those
    are landed, fingerprinted and routed."""
    file_bytes = await file.read()
    sheets_dict = await run_in_threadpool(read_excel_all_sheets, file_bytes, skip_rows)
    if not sheets_dict:
        raise HTTPException(400, "workbook has no readable sheets")
    if selected_sheets:
        keep = set(selected_sheets)
        sheets_dict = {k: v for k, v in sheets_dict.items() if k in keep}
        if not sheets_dict:
            raise HTTPException(400, "none of the selected input sheets were found in the workbook")

    landing = await run_in_threadpool(dl.build_landing_record, sheets_dict)
    sig = signature_multi(sheets_dict)
    fp = signature_hash(sig)
    input_sheets = list(landing["sheets"].keys())
    cols_by_sheet, samples_by_sheet = _cols_and_samples(sheets_dict)

    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        tpl = s.get(ExportTemplate, output_template_id)
        if not tpl:
            raise HTTPException(404, "output template not found")
        structure = _load_structure(tpl)
        output_sheets = _output_sheet_names(structure)

        # Format-level reuse: have we learned this input layout before?
        fmt = (s.query(DirectFormat)
               .filter(DirectFormat.tenant_id == tid,
                       DirectFormat.fingerprint == fp,
                       DirectFormat.output_template_id == output_template_id)
               .order_by(DirectFormat.id.desc())
               .first())
        known = bool(fmt and fmt.approved and fmt.sheet_routing and fmt.column_mapping)

        if known:
            routing = fmt.sheet_routing
            column_mapping = fmt.column_mapping
            candidates = fmt.candidates or {}
            fmt.hit_count = (fmt.hit_count or 1) + 1
        else:
            routing = dl.propose_sheet_routing(input_sheets, output_sheets)
            column_mapping, candidates = await run_in_threadpool(
                dm.propose_column_mapping, cols_by_sheet, structure, routing,
                samples_by_sheet)
            if fmt is None:
                fmt = DirectFormat(
                    tenant_id=tid, name=name, fingerprint=fp,
                    output_template_id=output_template_id, contract_id=contract_id,
                    carrier_party_id=carrier_party_id, program_id=program_id,
                    sheet_routing=routing, column_mapping=column_mapping,
                    candidates=candidates, datamodel_mapped=False, approved=0)
                s.add(fmt)
            else:
                fmt.sheet_routing = routing
                fmt.column_mapping = column_mapping
                fmt.candidates = candidates
                fmt.contract_id = contract_id or fmt.contract_id
                fmt.carrier_party_id = carrier_party_id or fmt.carrier_party_id
                fmt.program_id = program_id or fmt.program_id
            s.flush()

        datamodel_mapped = bool(fmt.datamodel_mapped)
        # DATA-MODEL mapping reuse by INPUT fingerprint: the input→canonical
        # mapping depends only on the input structure, not the output template.
        # So if THIS format isn't mapped yet but a sibling (same tenant + same
        # input fingerprint) already is, inherit its mapper here — a new output
        # template for the same input then flows into the data model without
        # re-mapping. Purely data-lane: the input→output routing/column_mapping
        # above is untouched.
        if not datamodel_mapped:
            sib = (s.query(DirectFormat)
                   .filter(DirectFormat.tenant_id == tid,
                           DirectFormat.fingerprint == fp,
                           DirectFormat.datamodel_mapped.is_(True),
                           DirectFormat.datamodel_mapper_id.isnot(None),
                           DirectFormat.id != fmt.id)
                   .order_by(DirectFormat.id.desc())
                   .first())
            if sib is not None:
                fmt.datamodel_mapped = True
                fmt.datamodel_mapper_id = sib.datamodel_mapper_id
                datamodel_mapped = True
                log.info("inherited data-model mapping from sibling format %s "
                         "(mapper %s, same input fingerprint) onto format %s",
                         sib.id, sib.datamodel_mapper_id, fmt.id)
        rec = LandingRecord(
            tenant_id=tid, format_id=fmt.id, source_filename=file.filename,
            fingerprint=fp, data=landing, row_count=landing["row_count"],
            datamodel_status="pending")
        s.add(rec)
        s.commit()
        s.refresh(rec)
        s.refresh(fmt)
        format_id, landing_id = fmt.id, rec.id
        datamodel_mapper_id = fmt.datamodel_mapper_id

    # DATA LANE (additive): if this format's input→data-model mapping is already
    # done, push this freshly-landed input straight into the data model — in a
    # background thread so the setup response never waits on ingestion. Mirrors the
    # /direct/run auto-ingest; reuses the gated _ingest_landing_background (which
    # self-checks datamodel_mapped and skips already-loaded landings). The
    # input→output setup logic above is unchanged.
    datamodel_queued = False
    if datamodel_mapped:
        log.info("data model mapping already exists (format %s, mapper %s) — "
                 "directly loading input into the data model (landing %s)",
                 format_id, datamodel_mapper_id, landing_id)
        threading.Thread(target=_ingest_landing_background,
                         args=(landing_id,), daemon=True).start()
        datamodel_queued = True
    else:
        log.info("data model mapping not done for format %s — skipping direct "
                 "input→data-model load (landing %s)", format_id, landing_id)

    return {
        "landing_id": landing_id,
        "format_id": format_id,
        "known_format": known,
        "datamodel_mapped": datamodel_mapped,
        "datamodel_queued": datamodel_queued,
        "fingerprint": fp,
        "input_sheets": input_sheets,
        "output_sheets": output_sheets,
        "input_columns": cols_by_sheet,
        "samples": samples_by_sheet,
        "sheet_routing": routing,
        "column_mapping": column_mapping,
        "candidates": candidates,
        "row_count": landing["row_count"],
    }


@router.get("/direct/format/{format_id}")
def direct_format_get(format_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        f = s.get(DirectFormat, format_id)
        if not f:
            raise HTTPException(404, "format not found")
        assert_tenant_owns(principal, f.tenant_id)
        return _format_to_dict(f)


@router.put("/direct/format/{format_id}")
def direct_format_update(format_id: int, body: DirectFormatUpdate,
                         principal: Principal = Depends(current_principal)):
    """User confirmation step: save the reviewed sheet routing + column mapping
    so every future file of this format flows straight through."""
    with SessionLocal() as s:
        f = s.get(DirectFormat, format_id)
        if not f:
            raise HTTPException(404, "format not found")
        assert_tenant_owns(principal, f.tenant_id)
        if body.sheet_routing is not None:
            f.sheet_routing = body.sheet_routing
        if body.column_mapping is not None:
            f.column_mapping = body.column_mapping
        if body.candidates is not None:
            f.candidates = body.candidates
        if body.name is not None:
            f.name = body.name
        if body.contract_id is not None:
            f.contract_id = body.contract_id
        if body.sheet_contracts is not None:
            # {output_sheet: contract_id} — coerce ids to int, drop blanks.
            f.sheet_contracts = {
                str(k): int(v) for k, v in body.sheet_contracts.items()
                if v not in (None, "", 0)
            }
        if body.carrier_party_id is not None:
            f.carrier_party_id = body.carrier_party_id
        if body.program_id is not None:
            f.program_id = body.program_id
        if body.approved is not None:
            f.approved = 1 if body.approved else 0
            # Supersede any prior active setup for the same carrier + program:
            # only one setup is active per (carrier, program) at a time.
            if f.approved and f.carrier_party_id is not None and f.program_id is not None:
                s.query(DirectFormat).filter(
                    DirectFormat.tenant_id == f.tenant_id,
                    DirectFormat.carrier_party_id == f.carrier_party_id,
                    DirectFormat.program_id == f.program_id,
                    DirectFormat.id != f.id,
                ).update({DirectFormat.approved: 0})
        f.modified_at = datetime.utcnow()
        s.commit()
        s.refresh(f)
        return _format_to_dict(f)


@router.delete("/direct/format/{format_id}")
def direct_format_delete(format_id: int,
                         principal: Principal = Depends(current_principal)):
    """Discard a setup (draft or otherwise) for a carrier + program. Used by the
    "Delete draft" action in Bordereau Setup."""
    with SessionLocal() as s:
        f = s.get(DirectFormat, format_id)
        if not f:
            raise HTTPException(404, "format not found")
        assert_tenant_owns(principal, f.tenant_id)
        s.delete(f)
        s.commit()
        return {"ok": True, "deleted_id": format_id}


@router.get("/direct/landing/{landing_id}")
def direct_landing_get(landing_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        r = s.get(LandingRecord, landing_id)
        if not r:
            raise HTTPException(404, "landing record not found")
        assert_tenant_owns(principal, r.tenant_id)
        return {
            "landing_id": r.id, "format_id": r.format_id,
            "source_filename": r.source_filename, "fingerprint": r.fingerprint,
            "row_count": r.row_count, "datamodel_status": r.datamodel_status,
            "data": r.data,
        }


@router.post("/direct/landing/{landing_id}/load-datamodel")
def direct_landing_load_datamodel(landing_id: int, background_tasks: BackgroundTasks):
    """Additive DATA-LANE trigger — push one landing into the canonical data model
    when (and only when) its format's input→data-model mapping is already done.

    Self-contained: it reuses _ingest_landing_background, which re-reads the
    approved mapper from the format and self-gates on datamodel_mapped, so this
    NEVER touches the input→output rendering/mapping path. Safe to call more than
    once — the background loader skips landings already marked loaded.

    Returns queued=False (with a reason) when the mapping isn't established yet or
    the landing is already loaded, so the caller can surface that to the user.
    """
    with SessionLocal() as s:
        rec = s.get(LandingRecord, landing_id)
        if not rec:
            raise HTTPException(404, "landing record not found")
        fmt = s.get(DirectFormat, rec.format_id) if rec.format_id else None
        mapping_ready = bool(fmt and fmt.datamodel_mapped and fmt.datamodel_mapper_id)
        status = rec.datamodel_status

    if status == "loaded":
        return {"landing_id": landing_id, "queued": False,
                "reason": "already loaded", "datamodel_status": status}
    if not mapping_ready:
        return {"landing_id": landing_id, "queued": False,
                "reason": "input→data-model mapping not done for this format",
                "datamodel_status": status}

    # Mapping exists and the landing is pending → load it in the background so the
    # caller never waits on ingestion.
    background_tasks.add_task(_ingest_landing_background, landing_id)
    return {"landing_id": landing_id, "queued": True, "datamodel_status": status}


@router.post("/direct/format/{format_id}/supplement")
async def direct_format_supplement(
    format_id: int,
    file: Optional[UploadFile] = File(default=None),
    clear: bool = Form(default=False),
    principal: Principal = Depends(current_principal),
):
    """Attach (or clear) the setup's supplementary data file. Uploaded ONCE here
    on the Setup page, alongside the input/output templates — NOT per run. The
    file is parsed now and stored with the format; every run captures its sheets
    alongside the BDX (no policy-number join)."""
    with SessionLocal() as s:
        f = s.get(DirectFormat, format_id)
        if not f:
            raise HTTPException(404, "format not found")
        assert_tenant_owns(principal, f.tenant_id)
        if clear or file is None:
            f.supplement = {"enabled": False}
        else:
            data = await file.read()
            supp_sheets = await run_in_threadpool(read_excel_all_sheets, data, 0)
            if not supp_sheets:
                raise HTTPException(400, "supplement workbook has no readable sheets")
            supp_landing = await run_in_threadpool(dl.build_landing_record, supp_sheets)
            f.supplement = {"enabled": True, "filename": file.filename,
                            "landing": supp_landing}
        f.modified_at = datetime.utcnow()
        s.commit()
        s.refresh(f)
        return {"ok": True, "supplement": _supplement_summary(f.supplement)}


@router.post("/direct/render")
async def direct_render(
    landing_id: int = Form(...),
    contract_id: Optional[int] = Form(default=None),
    filename: Optional[str] = Form(default=None),
    actor: Optional[str] = Form(default=None),
    constants: Optional[str] = Form(default=None),
    principal: Principal = Depends(current_principal),
):
    """SETUP preview: render the output for the just-mapped input template."""
    with SessionLocal() as s:
        lr = s.get(LandingRecord, landing_id)
        if not lr:
            raise HTTPException(404, "landing record not found")
        assert_tenant_owns(principal, lr.tenant_id)
    extra: dict = {}
    if constants:
        try:
            extra = json.loads(constants) or {}
        except (ValueError, TypeError):
            extra = {}
    return await _render_landing(landing_id, contract_id, filename, actor, extra)


@router.get("/direct/output-fields")
def direct_output_fields(template_id: int, contract_id: Optional[int] = None,
                         format_id: Optional[int] = None,
                         principal: Principal = Depends(current_principal)):
    """List the output template's columns (the searchable picker) with the
    contract clause bound to each — so changing the output field carries its
    clause along. Pass `format_id` to honour the setup's sheet↔contract mapping:
    each sheet's fields then only show clauses from the contract that governs
    that sheet (scoped clauses show on the schedules they name)."""
    sheet_contracts = None
    with SessionLocal() as s:
        tpl = s.get(ExportTemplate, template_id)
        if not tpl:
            raise HTTPException(404, "output template not found")
        structure = _load_structure(tpl)
        if format_id:
            fmt = s.get(DirectFormat, format_id)
            if fmt:
                assert_tenant_owns(principal, fmt.tenant_id)
                sheet_contracts = fmt.sheet_contracts
                contract_id = contract_id or fmt.contract_id
    fields: list[dict] = []
    for sh in structure.get("sheets", []):
        if is_reference_sheet(sh):
            continue          # reference/lookup tab — no rule fields shown for it
        for c in sorted(sh.get("columns", []), key=lambda x: x.get("column_index", 0)):
            name = c.get("column_name")
            if name:
                fields.append({"sheet": sh.get("sheet_name", ""), "field": name})
    _attach_clauses(fields, contract_id, sheet_contracts)
    return {"template_id": template_id, "contract_id": contract_id, "fields": fields}


@router.get("/direct/format/{format_id}/editor")
def direct_format_editor(format_id: int,
                         principal: Principal = Depends(current_principal)):
    """Rebuild the full Setup editor view for an EXISTING setup: its saved routing
    + column mapping, the output template's fields (with contract clauses), and the
    input columns/sheets from its latest landing record — so a saved setup can be
    reopened, reviewed and edited exactly like a fresh upload."""
    with SessionLocal() as s:
        f = s.get(DirectFormat, format_id)
        if not f:
            raise HTTPException(404, "format not found")
        assert_tenant_owns(principal, f.tenant_id)
        tpl = s.get(ExportTemplate, f.output_template_id) if f.output_template_id else None
        structure = _load_structure(tpl) if tpl else {"sheets": []}
        output_sheets = _output_sheet_names(structure)
        rec = (s.query(LandingRecord)
               .filter(LandingRecord.format_id == format_id)
               .order_by(LandingRecord.id.desc()).first())
        input_sheets: list[str] = []
        input_columns: dict[str, list[str]] = {}
        landing_id = None
        if rec and rec.data:
            for name, sheet in (rec.data.get("sheets") or {}).items():
                input_sheets.append(name)
                input_columns[name] = sheet.get("columns") or []
            landing_id = rec.id
        routing = f.sheet_routing or dl.propose_sheet_routing(input_sheets, output_sheets)
        column_mapping = f.column_mapping or {}
        candidates = f.candidates or {}
        contract_id = f.contract_id
        template_id = f.output_template_id
        sheet_contracts = f.sheet_contracts

    fields: list[dict] = []
    for sh in structure.get("sheets", []):
        if is_reference_sheet(sh):
            continue          # reference/lookup tab — no rule fields shown for it
        for c in sorted(sh.get("columns", []), key=lambda x: x.get("column_index", 0)):
            nm = c.get("column_name")
            if nm:
                fields.append({"sheet": sh.get("sheet_name", ""), "field": nm})
    _attach_clauses(fields, contract_id, sheet_contracts)

    return {
        "format_id": format_id, "landing_id": landing_id, "known_format": True,
        "template_id": template_id, "contract_id": contract_id,
        "input_sheets": input_sheets, "input_columns": input_columns,
        "output_sheets": output_sheets, "sheet_routing": routing,
        "column_mapping": column_mapping, "candidates": candidates,
        "fields": fields,
    }


@router.get("/direct/setup")
def direct_setup_get(mga: str, carrier_party_id: Optional[int] = None,
                     program_id: Optional[int] = None,
                     principal: Principal = Depends(current_principal)):
    """List the direct-lane setup(s) for a carrier + program (the active approved
    one is the binding used by /direct/run)."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        q = s.query(DirectFormat).filter(DirectFormat.tenant_id == tid)
        if carrier_party_id is not None:
            q = q.filter(DirectFormat.carrier_party_id == carrier_party_id)
        if program_id is not None:
            q = q.filter(DirectFormat.program_id == program_id)
        return [_format_to_dict(f) for f in q.order_by(DirectFormat.id.desc()).all()]


@router.get("/direct/runs")
def direct_runs(mga: str, carrier_party_id: Optional[int] = None,
                program_id: Optional[int] = None, limit: int = 20,
                principal: Principal = Depends(current_principal)):
    """Recent direct-lane runs (uploaded file → generated output), newest first,
    so the Direct page shows what was uploaded. Only landing records that actually
    produced an output are returned (output_export_id set) — setup samples are
    excluded. Scope to a carrier + program when given, else the whole tenant."""
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        if tid is None:
            return []
        q = (s.query(LandingRecord, DirectFormat, OutputExport)
             .join(DirectFormat, LandingRecord.format_id == DirectFormat.id)
             .join(OutputExport, LandingRecord.output_export_id == OutputExport.id)
             .filter(LandingRecord.tenant_id == tid))
        if carrier_party_id is not None:
            q = q.filter(DirectFormat.carrier_party_id == carrier_party_id)
        if program_id is not None:
            q = q.filter(DirectFormat.program_id == program_id)
        rows = q.order_by(LandingRecord.id.desc()).limit(min(limit, 100)).all()

        carrier_ids = {df.carrier_party_id for _, df, _ in rows if df.carrier_party_id}
        program_ids = {df.program_id for _, df, _ in rows if df.program_id}
        carriers = ({p.id: p.legal_name for p in
                     s.query(Party).filter(Party.id.in_(carrier_ids))}
                    if carrier_ids else {})
        programs = ({p.id: p.name for p in
                     s.query(Program).filter(Program.id.in_(program_ids))}
                    if program_ids else {})
        return [{
            "landing_id": lr.id,
            "source_filename": lr.source_filename,
            "row_count": lr.row_count or 0,
            "created_at": _iso_utc(lr.created_at),
            "carrier_party_id": df.carrier_party_id,
            "program_id": df.program_id,
            "carrier_name": carriers.get(df.carrier_party_id),
            "program_name": programs.get(df.program_id),
            "export_id": ex.id,
            "filename": ex.filename,
            "exception_count": ex.exception_count or 0,
            "status": ex.status or "clean",
            "datamodel_status": lr.datamodel_status,
        } for lr, df, ex in rows]


@router.post("/direct/run")
async def direct_run(
    mga: str = Form(...),
    carrier_party_id: int = Form(...),
    program_id: int = Form(...),
    file: UploadFile = File(...),
    filename: Optional[str] = Form(default=None),
    actor: Optional[str] = Form(default=None),
    skip_rows: int = Form(default=0),
    principal: Principal = Depends(current_principal),
):
    """DATA step: ops uploads a real data file for a carrier + program. Uses the
    active setup (DirectFormat) for that pair — no mapping review needed. If the
    setup has a supplementary data file, its sheets are captured alongside the
    BDX automatically (no per-run upload)."""
    file_bytes = await file.read()
    sheets_dict = await run_in_threadpool(read_excel_all_sheets, file_bytes, skip_rows)
    if not sheets_dict:
        raise HTTPException(400, "workbook has no readable sheets")

    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        fmt = (s.query(DirectFormat)
               .filter(DirectFormat.tenant_id == tid,
                       DirectFormat.carrier_party_id == carrier_party_id,
                       DirectFormat.program_id == program_id,
                       DirectFormat.approved == 1)
               .order_by(DirectFormat.id.desc()).first())
        if not fmt:
            raise HTTPException(
                400, "no active setup for this carrier + program — configure it "
                     "on the Setup page first")
        # Restrict to the input sheets the setup actually maps — extra sheets in
        # the ops file are ignored, so the fingerprint stays comparable to setup.
        wanted = _routing_input_sheets(fmt.sheet_routing)
        if wanted:
            filtered = {k: v for k, v in sheets_dict.items() if k in wanted}
            if filtered:
                sheets_dict = filtered
        landing = await run_in_threadpool(dl.build_landing_record, sheets_dict)

        # Supplement: capture the setup's stored supplementary sheets alongside the
        # BDX (no policy-number join — a supplement file just carries extra data).
        # Uploaded once on the Setup page; nothing to upload here.
        supp_stats = None
        supp_cfg = fmt.supplement or {}
        if supp_cfg.get("enabled") and supp_cfg.get("landing"):
            supp_stats = dl.attach_supplement(landing, supp_cfg["landing"])

        fp = signature_hash(signature_multi(sheets_dict))
        drift = bool(fmt.fingerprint and fmt.fingerprint != fp)
        eff_contract_id = fmt.contract_id
        rec = LandingRecord(
            tenant_id=tid, format_id=fmt.id, source_filename=file.filename,
            fingerprint=fp, data=landing, row_count=landing["row_count"],
            datamodel_status="pending")
        s.add(rec)
        s.commit()
        s.refresh(rec)
        landing_id = rec.id

    result = await _render_landing(landing_id, eff_contract_id, filename,
                                   actor or mga, {}, auto_ingest=True)
    result["format_drift"] = drift
    if supp_stats is not None:
        result["supplement"] = supp_stats
    return result


@router.get("/admin/mapping-tasks")
def admin_tasks_list(mga: Optional[str] = None, status: Optional[str] = None,
                     limit: int = 100,
                     _p: Principal = Depends(require_role("kavachio_admin"))):
    with SessionLocal() as s:
        q = s.query(AdminMappingTask)
        if mga:
            q = q.filter(AdminMappingTask.tenant_id == _tenant_id(s, mga))
        if status:
            q = q.filter(AdminMappingTask.status == status)
        rows = q.order_by(AdminMappingTask.id.desc()).limit(limit).all()
        fmt_ids = {t.format_id for t in rows if t.format_id}
        fmt_names = ({f.id: f.name for f in
                     s.query(DirectFormat).filter(DirectFormat.id.in_(fmt_ids))}
                    if fmt_ids else {})
        out = []
        for t in rows:
            detail = t.detail if isinstance(t.detail, dict) else {}
            out.append({
                "id": t.id, "tenant_id": t.tenant_id, "format_id": t.format_id,
                "format_name": fmt_names.get(t.format_id),
                "fingerprint": t.fingerprint, "status": t.status, "title": t.title,
                "detail": t.detail,
                "proposed_mapper_id": detail.get("proposed_mapper_id"),
                "landing_record_ids": t.landing_record_ids or [],
                "created_by": t.created_by, "resolved_by": t.resolved_by,
                "created_at": _iso_utc(t.created_at),
            })
        return out


@router.post("/admin/mapping-tasks/{task_id}/propose")
def admin_task_propose(task_id: int,
                       _p: Principal = Depends(require_role("kavachio_admin"))):
    """AI-map the format's input fields → the 850-field data model (with confidence
    scoring), persist it as a Mapper, link it to the task, and return its id — so
    the admin reviews/edits it in the standard mapper UI before approving. Same
    engine and scoring as the normal upload→mapper flow."""
    with SessionLocal() as s:
        task = s.get(AdminMappingTask, task_id)
        if not task:
            raise HTTPException(404, "task not found")
        # Learn from a landing sample: prefer the task's pending landings, else the
        # newest landing captured for this format.
        rec = None
        for lid in (task.landing_record_ids or []):
            r = s.get(LandingRecord, lid)
            if r and r.data:
                rec = r
                break
        if rec is None and task.format_id:
            rec = (s.query(LandingRecord)
                   .filter(LandingRecord.format_id == task.format_id)
                   .order_by(LandingRecord.id.desc()).first())
        if rec is None or not rec.data:
            raise HTTPException(400, "no landing sample to learn the mapping from")
        tenant_id = task.tenant_id
        fmt = s.get(DirectFormat, task.format_id) if task.format_id else None
        fmt_name = (fmt.name if fmt and fmt.name else None) or f"Format #{task.format_id}"
        # Capture the tenant Setup's input→output column map (set on Bordereau
        # Setup, saved as DirectFormat.column_mapping) while the session is open
        # (fmt detaches after the block closes).
        column_mapping = (fmt.column_mapping if fmt else None) or {}
        source_filename = rec.source_filename
        landing_data = rec.data

    # Reconstruct DataFrames from the faithful landing JSON.
    sheets_dict: dict[str, "pd.DataFrame"] = {}
    for name, sheet in (landing_data.get("sheets") or {}).items():
        cols = sheet.get("columns") or []
        rows = sheet.get("rows") or []
        sheets_dict[name] = (pd.DataFrame(rows, columns=cols) if rows
                             else pd.DataFrame(columns=cols))
    if not sheets_dict:
        raise HTTPException(400, "landing sample has no readable sheets")

    # Same AI mapping engine (+ scoring) as the normal upload flow, now owned by
    # mapper-service and reached over HTTP (no direct import of the LLM engine).
    from clients import mapper as _mapper_client
    result = _mapper_client.generate_mapping_df(sheets_dict)
    sig = signature_multi(sheets_dict)

    # Tenant "output column" per input column. The Bordereau Setup renames raw
    # input columns to the tenant's output fields via `copy` rules, stored as
    # column_mapping = {output_sheet: {output_field: {kind:"copy", source:<input_col>}}}.
    # Invert those (input col → output field), then re-qualify with the landing
    # sheet so keys line up with the mapper's "Sheet :: Column" source keys.
    # Only `copy` rules map 1:1; const/transform have no single source → skipped.
    out_by_input: dict[str, str] = {}
    for col_rules in (column_mapping or {}).values():
        if not isinstance(col_rules, dict):
            continue
        for out_field, rule in col_rules.items():
            if isinstance(rule, dict) and rule.get("kind") == "copy" and rule.get("source"):
                out_by_input.setdefault(str(rule["source"]), out_field)
    output_by_source: dict[str, str] = {}
    if out_by_input:
        for sheet_name, df in sheets_dict.items():
            for col in df.columns:
                bare = str(col)
                if bare in out_by_input:
                    output_by_source[qualify(str(sheet_name), bare)] = out_by_input[bare]

    with SessionLocal() as s:
        m = Mapper(
            tenant_id=tenant_id, name=f"{fmt_name} → data model",
            version=1, is_active=0, approved=0, signature=sig,
            spec=result.get("spec") or {},
            spec_by_sheet=result.get("spec_by_sheet") or {},
            candidates=result.get("candidates_by_source") or {},
            samples=result.get("samples") or {},
            output_by_source=output_by_source,
            source_filename=source_filename,
            selected_sheets=list(sheets_dict.keys()))
        s.add(m)
        s.flush()
        mapper_id = m.id
        task = s.get(AdminMappingTask, task_id)
        detail = dict(task.detail) if isinstance(task.detail, dict) else {}
        detail["proposed_mapper_id"] = mapper_id
        task.detail = detail
        task.status = "in_progress"
        s.commit()

    return {
        "task_id": task_id, "mapper_id": mapper_id,
        "stats": {
            "successful": len(result.get("successful") or []),
            "likely": len(result.get("likely") or []),
            "unsuccessful": len(result.get("unsuccessful") or []),
        },
    }


@router.post("/admin/mapping-tasks/{task_id}/resolve")
def admin_task_resolve(task_id: int, body: TaskResolveBody,
                       background_tasks: BackgroundTasks,
                       _p: Principal = Depends(require_role("kavachio_admin"))):
    """Approve: mark the format data-model-mapped and SCHEDULE the backfill of
    every pending landing record into the canonical warehouse — the row-loading
    runs in the BACKGROUND so the caller returns immediately. Dismiss: just
    close the task."""
    with SessionLocal() as s:
        task = s.get(AdminMappingTask, task_id)
        if not task:
            raise HTTPException(404, "task not found")
        if body.action == "dismiss":
            task.status = "dismissed"
            task.resolved_by = body.resolved_by
            task.resolved_at = datetime.utcnow()
            s.commit()
            return {"id": task.id, "status": task.status, "queued": 0}

        if body.mapper_id is None:
            raise HTTPException(400, "mapper_id required to approve")
        mapper = s.get(Mapper, body.mapper_id)
        if not mapper or not mapper.spec_by_sheet:
            raise HTTPException(400, "mapper not found or has no spec_by_sheet")

        # Flag the format data-model-mapped so BOTH this backfill and all future
        # uploads of this format auto-ingest into the warehouse.
        fmt = s.get(DirectFormat, task.format_id) if task.format_id else None
        if fmt:
            fmt.datamodel_mapped = True
            fmt.datamodel_mapper_id = body.mapper_id
            fmt.modified_at = datetime.utcnow()

        landing_ids = list(task.landing_record_ids or [])
        # Mark resolved now; the actual row-loading happens in the background
        # below so the admin isn't blocked on ingestion.
        task.status = "done"
        task.resolved_by = body.resolved_by
        task.resolved_at = datetime.utcnow()
        s.commit()

    # Load each pending file into the warehouse OFF the response path.
    # _ingest_landing_background is idempotent (skips already-loaded landings)
    # and self-contained (re-reads the approved mapper from the format), so it
    # runs safely after the response is returned.
    for lid in landing_ids:
        background_tasks.add_task(_ingest_landing_background, lid)

    return {"id": task_id, "status": "done", "queued": len(landing_ids)}
