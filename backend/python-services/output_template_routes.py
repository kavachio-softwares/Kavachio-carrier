"""The Output BDX Template — created, edited, validated and resolved.

An output template is the BLUEPRINT for the file a carrier delivers: which
columns it carries, where each one's value comes from, how it is formatted. It
is not the data. Records come from the ordinary processing pipeline and are
poured into this shape at run time (plan sections 20/21).

WHAT IS NEW HERE is the scope. A template is agreed at

    carrier -> programme -> broker -> contract

which is the same chain a contract already sits at, so nothing about the
hierarchy is invented — ``contract.program_id`` and ``contract.broker_party_id``
already say who a contract belongs to. The four ids are stored on the template
as well so a run can resolve one in a single indexed read, and so a template can
sit ABOVE contract level as a broker-wide default.

BACKWARD COMPATIBILITY IS THE POINT. Every scope column is nullable and nothing
is backfilled. A template made before this existed carries carrier + programme
only, and ``_resolve`` still finds it — see the fallback ladder there. No
existing setup changes behaviour.

There are three ways to get a template, and all three end in the same editable
row that the rest of the platform already understands:

  1. upload a sample workbook   — ``POST /export/template/generate`` (unchanged)
  2. adopt a reporting standard — ``POST /output-template/from-standard``
  3. build it from the contract — ``POST /output-template/from-contract``

WHICHEVER OF 2 OR 3, the field list is proposed from BOTH the output side (what
the standard or the contract demands) and the INPUT side (what the incoming
bordereau can actually fill) — see ``POST /output-template/analyze-sources`` and
``output_source_analysis``. A territory's published list is long and a good deal
of it does not apply to a given binder; deciding that at creation time is the
difference between a template that can be delivered and one full of columns
nobody can fill.
"""
from __future__ import annotations

import io
import logging
import os
import re
import tempfile
from datetime import datetime
from typing import Any, Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

import reporting_standards as standards
import output_source_analysis as osa
import output_template_fields as otf
import output_template_validation as otv
import semantic_mapping as sm
import storage
from app_routes import assert_tenant_owns, resolve_tenant_id
from auth_deps import Principal, current_principal
from db import (
    Contract, ExportTemplate, OutputExport, Party, Pipeline, Program,
    ProgramBroker, ReferenceDocument, SessionLocal,
)
from output_serializers import SUPPORTED_FORMATS, normalize_format

log = logging.getLogger("bdx.output_template")

router = APIRouter(tags=["output-template"])


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(name or "")).strip("_") or "template"


def _names(s, t: ExportTemplate) -> dict:
    """Human labels for the four scope ids, resolved once for display."""
    out: dict[str, Optional[str]] = {"carrier": t.carrier, "programme": None,
                                     "broker": None, "contract": None}
    if t.program_id:
        p = s.get(Program, t.program_id)
        out["programme"] = p.name if p else None
    if t.broker_party_id:
        b = s.get(Party, t.broker_party_id)
        out["broker"] = b.legal_name if b else None
    if t.contract_id:
        c = s.get(Contract, t.contract_id)
        out["contract"] = (c.filename or f"Contract {c.id}") if c else None
    if not out["carrier"] and t.carrier_party_id:
        cp = s.get(Party, t.carrier_party_id)
        out["carrier"] = cp.legal_name if cp else None
    return out


def _tpl_dict(s, t: ExportTemplate, *, with_structure: bool = False) -> dict:
    d = {
        "id": t.id,
        "name": t.name,
        "version": t.version or 1,
        "is_active": bool(t.is_active),
        "approved": bool(t.approved),
        "output_format": t.output_format or "xlsx",
        "source_kind": t.source_kind or "uploaded",
        "standard_meta": t.standard_meta or None,
        "scope": {
            "carrier_party_id": t.carrier_party_id,
            "program_id": t.program_id,
            "broker_party_id": t.broker_party_id,
            "contract_id": t.contract_id,
        },
        "scope_names": _names(s, t),
        "has_sample": bool(t.template_blob_ref or t.template_blob),
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }
    if with_structure:
        d["structure"] = t.structure
    return d


# ---------------------------------------------------------------------------
# Scope resolution (plan sections 18/19)
# ---------------------------------------------------------------------------

# How specific a match was. The UI shows this so a user is never left guessing
# WHY a template was picked — "this is the programme's template, not this
# contract's" is exactly the thing that would otherwise be discovered too late.
EXACT, BROKER_LEVEL, PROGRAMME_LEVEL = "contract", "broker", "programme"


def _resolve(s, tid: int, carrier_party_id: Optional[int], program_id: Optional[int],
             broker_party_id: Optional[int], contract_id: Optional[int]
             ) -> tuple[Optional[ExportTemplate], Optional[str]]:
    """The active output template for a scope, most specific first.

    The ladder is deliberately short and never widens sideways: a template
    belonging to a DIFFERENT broker or a DIFFERENT contract can never be
    returned, because each rung either matches the id or requires it to be
    unset. That is the plan's "do not silently generate an output using an
    unrelated template".
    """
    def _base():
        q = s.query(ExportTemplate).filter(ExportTemplate.tenant_id == tid)
        if carrier_party_id is not None:
            q = q.filter(ExportTemplate.carrier_party_id == carrier_party_id)
        # Newest active version wins; an inactive version is history.
        return q.order_by(ExportTemplate.is_active.desc(), ExportTemplate.id.desc())

    if contract_id:
        hit = _base().filter(ExportTemplate.contract_id == contract_id).first()
        if hit:
            return hit, EXACT
        # A contract can also be bound the other way round — the contract row
        # names its template. Honour that too: it is how templates were linked
        # before the scope columns existed.
        c = s.get(Contract, contract_id)
        if c and c.output_template_id:
            hit = s.get(ExportTemplate, c.output_template_id)
            if hit and hit.tenant_id == tid:
                return hit, EXACT

    if program_id and broker_party_id:
        hit = (_base()
               .filter(ExportTemplate.program_id == program_id,
                       ExportTemplate.broker_party_id == broker_party_id,
                       ExportTemplate.contract_id.is_(None))
               .first())
        if hit:
            return hit, BROKER_LEVEL

    if program_id:
        hit = (_base()
               .filter(ExportTemplate.program_id == program_id,
                       ExportTemplate.broker_party_id.is_(None),
                       ExportTemplate.contract_id.is_(None))
               .first())
        if hit:
            return hit, PROGRAMME_LEVEL

    # Last rung: the setup's own template. This is what keeps every template
    # made before the scope columns existed reachable — they carry no
    # program_id, so only the pipeline knows which programme they serve.
    if program_id and carrier_party_id is not None:
        pipe = (s.query(Pipeline)
                .filter(Pipeline.tenant_id == tid,
                        Pipeline.carrier_party_id == carrier_party_id,
                        Pipeline.program_id == program_id,
                        Pipeline.status == "active")
                .order_by(Pipeline.id.desc()).first())
        if pipe and pipe.output_template_id:
            hit = s.get(ExportTemplate, pipe.output_template_id)
            if hit and hit.tenant_id == tid:
                return hit, PROGRAMME_LEVEL
    return None, None


@router.get("/output-template/resolve")
def resolve_template(
    program_id: int = Query(...),
    carrier_party_id: Optional[int] = Query(None),
    broker_party_id: Optional[int] = Query(None),
    contract_id: Optional[int] = Query(None),
    mga: Optional[str] = Query(None),
    principal: Principal = Depends(current_principal),
):
    """Which output template applies to this exact scope — or none, and why.

    Never falls sideways onto an unrelated template: see `_resolve`. When
    nothing matches, the scope is echoed back so the screen can name the four
    levels it could not find a template for and offer to create one.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        prog = s.get(Program, program_id)
        if not prog:
            raise HTTPException(404, "programme not found")
        assert_tenant_owns(principal, prog.tenant_id)

        t, level = _resolve(s, tid, carrier_party_id, program_id,
                            broker_party_id, contract_id)
        scope_names = {
            "programme": prog.name,
            "carrier": (s.get(Party, carrier_party_id).legal_name
                        if carrier_party_id and s.get(Party, carrier_party_id) else None),
            "broker": (s.get(Party, broker_party_id).legal_name
                       if broker_party_id and s.get(Party, broker_party_id) else None),
            "contract": None,
        }
        if contract_id:
            c = s.get(Contract, contract_id)
            scope_names["contract"] = (c.filename or f"Contract {c.id}") if c else None
        return {
            "found": t is not None,
            # contract | broker | programme — how specific the match was.
            "match_level": level,
            "template": _tpl_dict(s, t) if t else None,
            "scope": {"carrier_party_id": carrier_party_id, "program_id": program_id,
                      "broker_party_id": broker_party_id, "contract_id": contract_id},
            "scope_names": scope_names,
            "setup": _setup_for_scope(s, tid, carrier_party_id, program_id,
                                      broker_party_id, t),
        }


def _setup_for_scope(s, tid: int, carrier_party_id: Optional[int],
                     program_id: int, broker_party_id: Optional[int],
                     agreed: Optional[ExportTemplate]) -> Optional[dict]:
    """Which Bordereau Setup would actually run here, and does it write into the
    template this scope agreed on?

    A setup's input mapping is learned against ONE output template — its column
    and sheet names are the mapping's keys. So a setup cannot be pointed at a
    different template: it would produce the right headings with nothing under
    them. `matches` is what lets the screen say that before a file is uploaded
    rather than after one is delivered.
    """
    def _active(broker):
        q = (s.query(Pipeline)
             .filter(Pipeline.tenant_id == tid,
                     Pipeline.carrier_party_id == carrier_party_id,
                     Pipeline.program_id == program_id,
                     Pipeline.status == "active"))
        q = q.filter(Pipeline.broker_party_id == broker if broker is not None
                     else Pipeline.broker_party_id.is_(None))
        return q.order_by(Pipeline.id.desc()).first()

    pipe = (_active(broker_party_id) if broker_party_id else None) or _active(None)
    if pipe is None:
        return None
    running = s.get(ExportTemplate, pipe.output_template_id) if pipe.output_template_id else None
    # Versions of one template share a name: the setup keeps using the version
    # it was built with, and that is not a mismatch.
    matches = bool(agreed and running and
                   (running.id == agreed.id or running.name == agreed.name))
    return {
        "pipeline_id": pipe.id,
        "name": pipe.name,
        "broker_party_id": pipe.broker_party_id,
        "output_template_id": pipe.output_template_id,
        "output_template_name": running.name if running else None,
        "matches": matches,
    }


# ---------------------------------------------------------------------------
# What can be created (plan section 7)
# ---------------------------------------------------------------------------

@router.get("/output-template/standards")
def list_standards(principal: Principal = Depends(current_principal)):
    """The reporting standards bundled with this deployment.

    Only version/jurisdiction combinations the bundled workbook actually
    contains are offered — every value here is read out of the file, so a
    jurisdiction that is not in it can never be selected. `available` is False
    when nothing is bundled, so the screen hides the option rather than
    presenting an action that would fail.
    """
    found = standards.discover()
    return {
        "available": bool(found),
        "output_formats": list(SUPPORTED_FORMATS),
        "standards": [{
            "id": s["id"],
            "label": s["label"],
            "version": s["version"],
            "jurisdictions": s["jurisdictions"],
            "default_jurisdiction": standards.default_jurisdiction(s["jurisdictions"]),
            "field_count": s["requirement_count"],
        } for s in found],
    }


@router.get("/output-template/standards/{standard_id}/fields")
def standard_fields(standard_id: str, jurisdiction: Optional[str] = None,
                    principal: Principal = Depends(current_principal)):
    """The published field list for one jurisdiction — a preview of exactly what
    adopting the standard would create, before creating it."""
    fields = standards.fields(standard_id, jurisdiction)
    if not fields:
        raise HTTPException(404, "no such reporting standard or jurisdiction")
    return {"standard_id": standard_id, "jurisdiction": jurisdiction,
            "fields": fields,
            "mandatory_count": sum(1 for f in fields if f["required"])}


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------

def _scope_check(s, principal: Principal, tid: int, program_id: int,
                 broker_party_id: Optional[int], contract_id: Optional[int]) -> Program:
    """Refuse a scope the hierarchy does not actually contain.

    A broker must be ON the programme, and a contract must belong to that
    programme and that broker. Without this a template could be filed against a
    pairing that has no business relationship behind it, and would then be
    resolved for runs that were never meant to see it.
    """
    prog = s.get(Program, program_id)
    if not prog:
        raise HTTPException(404, "programme not found")
    assert_tenant_owns(principal, prog.tenant_id)

    if broker_party_id:
        link = (s.query(ProgramBroker)
                .filter(ProgramBroker.program_id == program_id,
                        ProgramBroker.broker_party_id == broker_party_id)
                .first())
        if not link:
            raise HTTPException(400, "that broker is not on this programme")
    if contract_id:
        c = s.get(Contract, contract_id)
        if not c:
            raise HTTPException(404, "contract not found")
        assert_tenant_owns(principal, c.tenant_id)
        if c.program_id != program_id:
            raise HTTPException(400, "that contract is not on this programme")
        # A contract with no broker is the carrier's own — allowed at any broker
        # scope, which is what keeps pre-broker contracts usable.
        if broker_party_id and c.broker_party_id and c.broker_party_id != broker_party_id:
            raise HTTPException(400, "that contract belongs to a different broker")
    return prog


def _next_version(s, tid: int, name: str) -> tuple[int, int]:
    """(version, is_active) for a new template under an existing name — the same
    rule ``/export/template/generate`` has always used."""
    versions = [v for (v,) in s.query(ExportTemplate.version)
                .filter(ExportTemplate.tenant_id == tid, ExportTemplate.name == name)]
    return ((max((v or 1) for v in versions) + 1) if versions else 1,
            0 if versions else 1)


def _contract_label(c: Optional[Contract]) -> str:
    """A contract's name without its file extension.

    A template called "… — Schedule_A_2026.pdf" reads as if the template were a
    PDF. It is not — it is the output layout agreed under that contract, so the
    wording drops the extension and keeps the identity.
    """
    if c is None:
        return ""
    name = (c.filename or "").strip()
    if not name:
        return f"Contract {c.id}"
    return _drop_ext(name)


def _drop_ext(name: str) -> str:
    """A document's name without its extension — see ``_contract_label``."""
    name = (name or "").strip()
    for ext in (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".rtf"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def _scope_name(s, program_id: int, broker_party_id: Optional[int],
                contract_id: Optional[int], carrier_name: Optional[str],
                contract_label: Optional[str] = None) -> str:
    """A template name that says what it is FOR.

    Named after the scope, most specific part last, so a list of templates sorts
    and reads by carrier then programme then broker. Duplicated parts are
    dropped — a contract already named after its programme should not repeat it.
    """
    parts: list[str] = []
    if carrier_name:
        parts.append(carrier_name)
    p = s.get(Program, program_id)
    if p and p.name:
        parts.append(p.name)
    if broker_party_id:
        b = s.get(Party, broker_party_id)
        if b and b.legal_name:
            parts.append(b.legal_name)
    if contract_id or contract_label:
        label = contract_label or _contract_label(s.get(Contract, contract_id))
        # "Programme A — ProgrammeA_Bridge_2026" says the programme twice.
        squashed = "".join(ch for ch in label.lower() if ch.isalnum())
        if label and not any(
                "".join(ch for ch in part.lower() if ch.isalnum()) in squashed
                for part in parts if part):
            parts.append(label)
        elif label:
            parts.append(label)
    return " — ".join(parts) or "Output BDX template"


class FromStandardBody(BaseModel):
    program_id: int
    carrier_party_id: Optional[int] = None
    broker_party_id: Optional[int] = None
    contract_id: Optional[int] = None
    standard_id: Optional[str] = None
    jurisdiction: Optional[str] = None
    output_format: str = "xlsx"
    name: Optional[str] = None
    mga: Optional[str] = None
    # The reviewed field list from /analyze-sources: which of the territory's
    # published columns this binder actually reports, each optionally carrying
    # the input column it was matched to. Omitted, the whole territory is taken
    # — which is what every caller did before this existed.
    fields: Optional[list[dict]] = None


@router.post("/output-template/from-standard")
async def create_from_standard(body: FromStandardBody,
                               principal: Principal = Depends(current_principal)):
    """Adopt a bundled reporting standard as this scope's output template.

    The chosen jurisdiction tab is sliced out of the standard's workbook (its
    code row and label column dropped so the published names become the header)
    and then goes through the SAME parse + AI mapping pass an uploaded sample
    does. What comes out is an ordinary template the user edits and approves
    like any other — nothing downstream knows the difference.
    """
    std = standards.get(body.standard_id)
    if not std:
        raise HTTPException(400, "no reporting standard is bundled with this service")

    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, body.mga)
        _scope_check(s, principal, tid, body.program_id,
                     body.broker_party_id, body.contract_id)
        carrier_name = None
        if body.carrier_party_id:
            p = s.get(Party, body.carrier_party_id)
            carrier_name = p.legal_name if p else None
        name = body.name or _scope_name(s, body.program_id, body.broker_party_id,
                                        body.contract_id, carrier_name)

    label = f"{std['label']} - {body.jurisdiction or ''}".strip(" -")[:31]
    try:
        blob, jurisdiction = await run_in_threadpool(
            standards.sheet_bytes, std["id"], body.jurisdiction, label)
    except KeyError:
        raise HTTPException(400, f"'{body.jurisdiction}' is not a layout in this standard")
    except FileNotFoundError as e:
        raise HTTPException(500, str(e))

    filename = f"{_safe(std['id'])}_{_safe(jurisdiction)}.xlsx"
    from exporter import parse_template, propose_template_mapping
    structure = await run_in_threadpool(parse_template, blob, filename=filename)
    if not structure.get("sheets"):
        raise HTTPException(400, "the reporting standard produced no readable sheets")
    # The AI pass that maps each published column to a canonical field — the
    # same one an uploaded sample gets, so mapping quality is identical.
    await run_in_threadpool(propose_template_mapping, structure)
    otf.complete_structure(structure)
    otf.apply_standard(structure, standards.fields(std["id"], jurisdiction))
    # Only what the user kept. Applied AFTER apply_standard so a field the
    # standard makes mandatory survives a selection that left it out.
    selected = None
    from_contract = 0
    if body.fields is not None:
        keep = osa.keep_set(body.fields)
        if not keep:
            raise HTTPException(400, "keep at least one column")
        # The contract's own columns first, so the selection pass below sees
        # them as ordinary columns of this sheet and records their input
        # matches the same way it records the standard's.
        from_contract = osa.append_contract_fields(structure, body.fields)
        matches = {str(f.get("field") or "").strip().lower(): f
                   for f in body.fields if f.get("field")}
        osa.apply_selection(structure, keep, matches)
        selected = len(keep)

    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, body.mga)
        version, is_active = _next_version(s, tid, name)
        blob_ref, blob_bytes = await run_in_threadpool(
            storage.store_or_keep, "templates", tid, filename, blob)
        carrier_name = None
        if body.carrier_party_id:
            p = s.get(Party, body.carrier_party_id)
            carrier_name = p.legal_name if p else None
        t = ExportTemplate(
            tenant_id=tid, name=name, version=version, is_active=is_active,
            carrier=carrier_name, carrier_party_id=body.carrier_party_id,
            program_id=body.program_id, broker_party_id=body.broker_party_id,
            contract_id=body.contract_id,
            structure=structure, template_blob=blob_bytes,
            template_blob_ref=blob_ref, approved=0,
            output_format=normalize_format(body.output_format),
            source_kind="standard",
            standard_meta={"standard_id": std["id"], "standard": std["label"],
                           "version": std["version"], "jurisdiction": jurisdiction},
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        _audit(principal, t, "standard",
               {"jurisdiction": jurisdiction, "selected_fields": selected,
                "contract_columns": from_contract})
        return _tpl_dict(s, t, with_structure=True)


class AnalyzeBody(BaseModel):
    contract_id: int
    standard_id: Optional[str] = None
    jurisdiction: Optional[str] = None
    mga: Optional[str] = None


@router.post("/output-template/analyze-contract")
async def analyze_contract(body: AnalyzeBody,
                           principal: Principal = Depends(current_principal)):
    """What this contract requires the bordereau to report.

    Reads the contract's ALREADY-EXTRACTED clauses and rules — the PDF is never
    re-read — and returns a reviewable list. It writes nothing: the user sees
    the proposal, and creating the template is a separate, explicit step.
    """
    import contract_output_fields as cof
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, body.mga)
        c = s.get(Contract, body.contract_id)
        if not c:
            raise HTTPException(404, "contract not found")
        assert_tenant_owns(principal, c.tenant_id)
        library = standards.fields(body.standard_id, body.jurisdiction)
        known = [f["field"] for f in library]
        result = await run_in_threadpool(cof.analyze, s, [c.id], known)
    result["standard_field_count"] = len(known)
    return result

# ---------------------------------------------------------------------------
# Both sides at once (plan section 7/8 + prompt changes)
# ---------------------------------------------------------------------------

def _int_or_none(v: Any) -> Optional[int]:
    """A multipart form sends an unset number as an empty string, not as absent."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("null", "undefined"):
        return None
    try:
        return int(s)
    except ValueError:
        raise HTTPException(400, f"'{s}' is not a valid id")


def _document_pages(blob: bytes, filename: Optional[str]) -> list[dict]:
    """The text of a contract that has no row yet, page by page.

    The extractor works on a path, so the bytes are spooled to a temp file and
    deleted straight after — nothing is stored. This is the ONLY place a
    contract's file is read outside the upload pipeline, and it exists because
    a contract staged in the setup form has no extraction to read instead.
    """
    suffix = os.path.splitext(filename or "")[1] or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(blob)
        path = tmp.name
    try:
        from contract_upload_services.document_extractors import extract_document_data
        return extract_document_data(path).get("pages") or []
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@router.post("/output-template/analyze-sources")
async def analyze_sources(
    program_id: int = Form(...),
    mga: Optional[str] = Form(None),
    broker_party_id: Optional[str] = Form(None),
    contract_id: Optional[str] = Form(None),
    standard_id: Optional[str] = Form(None),
    jurisdiction: Optional[str] = Form(None),
    include_standard_library: bool = Form(True),
    # "full" when the standard IS the layout, "essential" when it is only
    # filling in what a contract never names. See _library.
    standard_scope: str = Form(standards.SCOPE_FULL),
    read_contract: bool = Form(True),
    input_sheets: Optional[list[str]] = Form(None),
    input_file: Optional[UploadFile] = File(None),
    contract_file: Optional[UploadFile] = File(None),
    principal: Principal = Depends(current_principal),
):
    """Propose the output template's field list from BOTH sides of the job.

    The output side says what the file is allowed and required to contain — the
    territory's published column list, the contract's own terms, or both. The
    input side says what the incoming bordereau can actually fill. Neither alone
    is enough: a territory's list carries columns this binder never reports, and
    the incoming file carries columns the recipient never asked for.

    Nothing is created and nothing is written. What comes back is a list with a
    tick, a reason and the matched input column against every field, for a
    person to agree with or change — creation is a separate, explicit call.
    """
    import contract_output_fields as cof

    cid = _int_or_none(contract_id)
    bpid = _int_or_none(broker_party_id)
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        _scope_check(s, principal, tid, program_id, bpid, cid)

    library = (standards.fields(standard_id, jurisdiction, scope=standard_scope)
               if include_standard_library else [])

    # --- what the contract asks for -----------------------------------------
    documents: list[dict] = []
    contract_source = "none"
    if read_contract and contract_file is not None:
        raw = await contract_file.read()
        if raw:
            try:
                pages = await run_in_threadpool(
                    _document_pages, raw, contract_file.filename)
            except ValueError as e:
                raise HTTPException(400, f"that contract could not be read — {e}")
            except Exception as e:  # noqa: BLE001 — a stored contract still works
                log.warning("staged contract unreadable: %s", e)
                pages = []
            documents = cof.clauses_from_document(pages)
            if documents:
                contract_source = "uploaded"

    result: dict = {"fields": [], "model_used": False,
                    "clause_count": 0, "rule_count": 0}
    if read_contract and (cid or documents):
        known = [f["field"] for f in library]
        with SessionLocal() as s:
            result = await run_in_threadpool(
                cof.analyze, s, [cid] if cid else [], known, documents=documents)
        if cid:
            contract_source = "saved and uploaded" if documents else "saved"

    # Fold first: a contract field the published list already has a column for
    # belongs ON that column, not beside it.
    extras, folded = await run_in_threadpool(
        osa.fold_contract_fields, library, result.get("fields") or [])
    merged = _merge_fields(library, extras, folded)
    if not merged:
        raise HTTPException(
            400, "nothing to propose — include a reporting standard, or pick a "
                 "contract whose terms can be read")

    # --- what the incoming bordereau carries --------------------------------
    layout: dict = {"columns": [], "samples": {}, "sheets": [], "sheets_read": []}
    if input_file is not None:
        raw = await input_file.read()
        if raw:
            try:
                layout = await run_in_threadpool(
                    osa.input_layout, raw, input_file.filename,
                    [x for x in (input_sheets or []) if str(x).strip()])
            except HTTPException:
                raise
            except Exception as e:  # noqa: BLE001
                raise HTTPException(
                    400, f"that input template could not be read — {e}")

    checked = bool(layout["columns"])
    # Threadpool: the ladder's last rung asks the model about whatever the
    # deterministic rungs could not place.
    fields = await run_in_threadpool(
        osa.cross_reference, merged, layout["columns"], layout["samples"])
    osa.recommend(fields, checked_input=checked)

    std = standards.get(standard_id) if library else None
    return {
        "fields": fields,
        "counts": osa.summarise(fields, checked_input=checked),
        "input": {"provided": input_file is not None,
                  "columns": layout["columns"],
                  "sheets": layout["sheets"],
                  "sheets_read": layout["sheets_read"]},
        "standard": ({"id": std["id"], "label": std["label"],
                      "version": std.get("version"),
                      "jurisdiction": jurisdiction,
                      "scope": standard_scope,
                      "field_count": len(library)} if std else None),
        "contract": {"source": contract_source,
                     "clause_count": result.get("clause_count", 0),
                     "rule_count": result.get("rule_count", 0),
                     "model_used": bool(result.get("model_used")),
                     "field_count": len(result.get("fields") or [])},
        "threshold": sm.min_confidence(),
    }


class FromContractBody(BaseModel):
    program_id: int
    # Optional: the contract may be one staged in the setup form and not
    # uploaded yet, in which case there is no row to point at and the reviewed
    # `fields` below ARE the answer. `contract_name` then names the scope.
    contract_id: Optional[int] = None
    contract_name: Optional[str] = None
    carrier_party_id: Optional[int] = None
    broker_party_id: Optional[int] = None
    # The standard supplies the fields a contract never mentions but every
    # bordereau needs (insurer name, reporting period, currency). Omit it to
    # build from the contract alone.
    standard_id: Optional[str] = None
    jurisdiction: Optional[str] = None
    include_standard_library: bool = True
    # Only the fields the standard marks mandatory, by default: a contract
    # template is the contract's list plus the essentials, not the whole
    # territory. Callers that want the old whole-library behaviour say so.
    standard_scope: str = standards.SCOPE_ESSENTIAL
    output_format: str = "xlsx"
    name: Optional[str] = None
    mga: Optional[str] = None
    # The reviewed field list from /analyze-contract or /analyze-sources.
    # Passing it back means the template is built from what the USER approved,
    # not from a second model call whose answer they never saw.
    fields: Optional[list[dict]] = None
    # True when `fields` is the COMPLETE reviewed list (what /analyze-sources
    # returns: standard library and contract terms already merged and pruned).
    # Left false, `fields` is contract-only and the standard library is merged
    # in underneath it — the behaviour every caller had before this existed.
    fields_reviewed: bool = False


@router.post("/output-template/from-contract")
async def create_from_contract(body: FromContractBody,
                               principal: Principal = Depends(current_principal)):
    """Build this contract's own output template.

    Contract-specific fields are merged with the standard field library — a
    contract rarely mentions the insurer's own name or the reporting period, and
    a bordereau always needs them. Duplicates are dropped on the published name,
    so a field the contract names AND the standard defines appears once, keeping
    the standard's requirement flag.
    """
    import contract_output_fields as cof

    if body.contract_id is None and body.fields is None:
        raise HTTPException(
            400, "pick a contract, or send the field list you reviewed")

    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, body.mga)
        _scope_check(s, principal, tid, body.program_id,
                     body.broker_party_id, body.contract_id)
        c = s.get(Contract, body.contract_id) if body.contract_id else None
        library = (standards.fields(body.standard_id, body.jurisdiction,
                                    scope=body.standard_scope)
                   if body.include_standard_library else [])
        contract_fields = body.fields
        if contract_fields is None:
            known = [f["field"] for f in library]
            contract_fields = (await run_in_threadpool(
                cof.analyze, s, [c.id], known))["fields"]
        carrier_name = None
        if body.carrier_party_id:
            p = s.get(Party, body.carrier_party_id)
            carrier_name = p.legal_name if p else None
        staged = _drop_ext(body.contract_name or "") if not c else None
        name = body.name or _scope_name(s, body.program_id, body.broker_party_id,
                                        body.contract_id, carrier_name,
                                        contract_label=staged or None)
        label = (c.filename if c else body.contract_name) or "Contract"
        sheet_name = f"{_drop_ext(label)[:20]} BDX"[:31]

    # A reviewed list is the whole answer — merging the library back in would
    # undo the pruning the user just did.
    if body.fields_reviewed:
        merged = list(contract_fields)
    else:
        extras, folded = await run_in_threadpool(
            osa.fold_contract_fields, library, contract_fields)
        merged = _merge_fields(library, extras, folded)
    if not merged:
        raise HTTPException(
            400, "nothing to build a template from — the contract produced no "
                 "field requirements and no standard library was included")

    structure = _structure_from_fields(sheet_name, merged)
    blob = await run_in_threadpool(_headers_workbook, sheet_name, merged)
    from exporter import propose_template_mapping
    await run_in_threadpool(propose_template_mapping, structure)
    otf.complete_structure(structure)
    if library:
        otf.apply_standard(structure, library)
    # Nothing to switch off — the list IS the selection — but record which input
    # column each field was matched to, so the template carries its own account
    # of where its values were expected to come from.
    osa.apply_selection(structure, set(),
                        {str(f.get("field") or "").strip().lower(): f
                         for f in merged if f.get("field")})

    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, body.mga)
        version, is_active = _next_version(s, tid, name)
        blob_ref, blob_bytes = await run_in_threadpool(
            storage.store_or_keep, "templates", tid,
            f"{_safe(name)}.xlsx", blob)
        carrier_name = None
        if body.carrier_party_id:
            p = s.get(Party, body.carrier_party_id)
            carrier_name = p.legal_name if p else None
        std = standards.get(body.standard_id) if library else None
        t = ExportTemplate(
            tenant_id=tid, name=name, version=version, is_active=is_active,
            carrier=carrier_name, carrier_party_id=body.carrier_party_id,
            program_id=body.program_id, broker_party_id=body.broker_party_id,
            contract_id=body.contract_id,
            structure=structure, template_blob=blob_bytes,
            template_blob_ref=blob_ref, approved=0,
            output_format=normalize_format(body.output_format),
            source_kind="contract",
            standard_meta=({"standard_id": std["id"], "standard": std["label"],
                            "version": std["version"],
                            "jurisdiction": body.jurisdiction} if std else None),
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        _audit(principal, t, "contract",
               {"contract_id": body.contract_id, "fields": len(merged)})
        return _tpl_dict(s, t, with_structure=True)


def _merge_fields(library: list[dict], contract_fields: list[dict],
                  folded: Optional[dict[str, dict]] = None) -> list[dict]:
    """Standard library + contract-specific, standard first, no duplicates.

    `folded` comes from ``osa.fold_contract_fields``: a contract requirement
    that turned out to BE one of the published columns, keyed by that column's
    name. It is stamped onto the standard's own row rather than added beside
    it, so a field the contract merely worded differently ends up as one column
    that the contract is on record as asking for.
    """
    folded = folded or {}
    out: list[dict] = []
    seen: set[str] = set()
    for f in library or []:
        key = str(f.get("field", "")).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        row = {"field": f["field"], "required": bool(f.get("required")),
               "source_field": None, "data_type": "string",
               "origin": "standard",
               "requirement": f.get("requirement"),
               "reason": f.get("comments") or None}
        c = folded.get(key)
        if c:
            row["also_in_contract"] = True
            row["contract_reference"] = c.get("contract_reference")
            if c.get("required"):
                row["contract_required"] = True
        out.append(row)
    for f in contract_fields or []:
        name = str(f.get("field") or f.get("display_name") or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append({"field": name, "required": bool(f.get("required")),
                    "source_field": f.get("source_field"),
                    "data_type": f.get("data_type") or "string",
                    "origin": f.get("origin") or "contract",
                    "category": f.get("category"),
                    # Kept so the review table can say WHY a field is there —
                    # dropped before, which left the user with a bare list.
                    "reason": f.get("reason"),
                    "contract_reference": f.get("contract_reference")})
    return out


def _structure_from_fields(sheet_name: str, fields: list[dict]) -> dict:
    """The template structure for a field list built rather than parsed."""
    return {"sheets": [{
        "sheet_name": sheet_name,
        "header_row": 0,
        "data_start_row": 1,
        "row_strategy": "policy",
        "sheet_role": "data",
        "rule_generatable": True,
        "columns": [{
            "column_index": i,
            "column_name": f["field"],
            # No sample file behind these columns, so no sample values. The AI
            # mapping pass reads names when it has nothing else, which is
            # exactly this case.
            "samples": [],
            "canonical_field": f.get("source_field"),
            "transform": None,
            "static_value": None,
            "required": bool(f.get("required")),
            "data_type": f.get("data_type") or "string",
            "field_origin": f.get("origin"),
        } for i, f in enumerate(fields)],
    }]}


def _headers_workbook(sheet_name: str, fields: list[dict]) -> bytes:
    """A one-row workbook of just the headers.

    Stored as the template's sample so the template has a real artifact behind
    it — it is what the user downloads to see the agreed layout, and what
    style-preserving generation writes into.
    """
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31] or "BDX"
    for i, f in enumerate(fields, start=1):
        ws.cell(row=1, column=i, value=f["field"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


def _audit(principal: Principal, t: ExportTemplate, source: str, extra: dict) -> None:
    try:
        from audit import log_activity, actor_email
        log_activity(t.tenant_id, actor_email(principal.user_id),
                     "output_template_generated", target=f"template:{t.name}",
                     details={"template_id": t.id, "name": t.name,
                              "version": t.version, "source": source, **extra})
    except Exception:  # noqa: BLE001 — auditing must never break the response
        pass


# ---------------------------------------------------------------------------
# The editor (plan sections 10/12/13/17)
# ---------------------------------------------------------------------------

def _standard_fields_for(t: ExportTemplate) -> list[dict]:
    meta = t.standard_meta or {}
    if not meta.get("standard_id"):
        return []
    return standards.fields(meta["standard_id"], meta.get("jurisdiction"))


def _load(s, template_id: int, principal: Principal) -> ExportTemplate:
    t = s.get(ExportTemplate, template_id)
    if not t:
        raise HTTPException(404, "output template not found")
    assert_tenant_owns(principal, t.tenant_id)
    return t


def _extra_field_keys(s, tenant_id: Optional[int]) -> set[str]:
    """Tenant-defined canonical fields ("extras") — valid mapping targets
    alongside the shipped data model.

    They live on `tenant.internal_codes.extras` in the CANONICAL database, not
    in a table of their own, so this opens that session rather than reusing the
    caller's. Best-effort: an unreachable canonical DB costs us the widened
    vocabulary, never the request.
    """
    if tenant_id is None:
        return set()
    try:
        from db import CanonicalSession
        from extras import list_definitions
        with CanonicalSession() as cs:
            return set(list_definitions(cs, tenant_id, include_shared=True).keys())
    except Exception as e:  # noqa: BLE001
        log.warning("extra-field vocabulary unavailable: %s", e)
        return set()


@router.get("/output-template/{template_id}/fields")
def get_fields(template_id: int, principal: Principal = Depends(current_principal)):
    """The editable field list, plus how the template currently validates."""
    with SessionLocal() as s:
        t = _load(s, template_id, principal)
        structure = otf.complete_structure(t.structure or {"sheets": []})
        std = _standard_fields_for(t)
        report = otv.validate_template(
            structure, standard_fields=std,
            extra_fields=_extra_field_keys(s, t.tenant_id))
        return {
            "template": _tpl_dict(s, t),
            "sheets": [sh.get("sheet_name") for sh in structure.get("sheets") or []],
            "fields": otf.flatten(structure),
            "validation": report,
            "source_types": list(otf.SOURCE_TYPES),
            "data_types": list(otf.DATA_TYPES),
            "standard": (t.standard_meta or None),
            # What was found when this template was built from both sides —
            # notably the required columns nothing in the bordereau could fill.
            # Absent on a template built before that check existed, which the
            # screen must not read as "everything is fine".
            "source_check": (structure.get("source_check") or None),
            # Once a file has been generated from this version, saving forks a
            # new one instead of rewriting history. The editor says so up front.
            "locked_by_history": _generation_count(s, t) > 0,
        }


def _generation_count(s, t: ExportTemplate) -> int:
    """How many delivered files were produced from THIS version.

    `template_version IS NULL` counts too: exports written before the column
    existed came from whatever version was current, and treating them as
    "unknown, therefore safe to overwrite" is exactly the way to lose the layout
    a historical download describes itself with.
    """
    from sqlalchemy import or_
    return (s.query(OutputExport)
            .filter(OutputExport.template_id == t.id,
                    or_(OutputExport.template_version == t.version,
                        OutputExport.template_version.is_(None)))
            .count())


class SaveFieldsBody(BaseModel):
    fields: list[dict]
    # Save without activating — the plan's edit → validate → edit loop.
    activate: bool = False


@router.put("/output-template/{template_id}/fields")
def save_fields(template_id: int, body: SaveFieldsBody,
                principal: Principal = Depends(current_principal)):
    """Save the edited field list, then validate it again.

    VERSIONING. If a file has already been generated from this version, the save
    creates version N+1 and leaves N exactly as it was — January's download must
    keep describing itself with January's layout (plan section 17). Otherwise the
    template is edited in place, because forking a version nobody has used yet
    just clutters the list.

    A template with outstanding errors can be SAVED but not ACTIVATED — the edit
    loop has to be able to pass through invalid states to reach a valid one.
    """
    with SessionLocal() as s:
        t = _load(s, template_id, principal)
        import copy
        structure = otf.complete_structure(
            copy.deepcopy(t.structure or {"sheets": []}))
        structure, errors = otf.apply_edits(structure, body.fields)

        std = _standard_fields_for(t)
        report = otv.validate_template(
            structure, standard_fields=std,
            extra_fields=_extra_field_keys(s, t.tenant_id))
        for msg in errors:
            report["findings"].insert(0, {
                "severity": otv.CRITICAL, "code": "edit_rejected",
                "message": msg, "sheet": None, "field": None})
        report["error_count"] += len(errors)
        report["valid"] = report["error_count"] == 0

        if body.activate and not report["valid"]:
            raise HTTPException(
                400, "this template still has errors, so it cannot be activated — "
                     "fix them and try again")

        forked = False
        if _generation_count(s, t) > 0:
            t = _fork(s, t, structure)
            forked = True
        else:
            t.structure = structure

        if body.activate and report["valid"]:
            s.query(ExportTemplate).filter(
                ExportTemplate.tenant_id == t.tenant_id,
                ExportTemplate.name == t.name,
                ExportTemplate.id != t.id).update({"is_active": 0})
            t.is_active = 1
            t.approved = 1
        s.commit()
        s.refresh(t)
        return {"template": _tpl_dict(s, t), "validation": report,
                "fields": otf.flatten(t.structure or {}),
                "versioned": forked}


def _fork(s, t: ExportTemplate, structure: dict) -> ExportTemplate:
    """Copy a used template into the next version, carrying the edits.

    The sample blob is carried by REFERENCE where blob storage holds it — the
    two versions describe the same sample workbook, so storing it twice would
    only mean two copies to keep in step.
    """
    version, _ = _next_version(s, t.tenant_id, t.name)
    nxt = ExportTemplate(
        tenant_id=t.tenant_id, name=t.name, version=version, is_active=0,
        carrier=t.carrier, carrier_party_id=t.carrier_party_id,
        program_id=t.program_id, broker_party_id=t.broker_party_id,
        contract_id=t.contract_id, structure=structure,
        template_blob=(None if t.template_blob_ref else t.template_blob),
        template_blob_ref=t.template_blob_ref, approved=0,
        output_format=t.output_format, source_kind=t.source_kind,
        standard_meta=t.standard_meta)
    s.add(nxt)
    s.flush()
    return nxt


class AddFieldBody(BaseModel):
    sheet: str
    display_name: str
    source_field: Optional[str] = None
    source_type: Optional[str] = None
    data_type: Optional[str] = None
    required: bool = False
    conditional: bool = False
    default_value: Optional[str] = None
    transformation_rule: Optional[str] = None
    # Where in the DELIVERY order it goes. Omitted, it appends — which is what
    # the "Add Field" button has always done. The sheet grid uses it to insert
    # a column to the left or the right of the one a person is pointing at.
    position: Optional[int] = None


@router.post("/output-template/{template_id}/fields")
def add_field(template_id: int, body: AddFieldBody,
              principal: Principal = Depends(current_principal)):
    """Add one field to a sheet (plan rule G)."""
    with SessionLocal() as s:
        t = _load(s, template_id, principal)
        import copy
        structure = otf.complete_structure(
            copy.deepcopy(t.structure or {"sheets": []}))
        structure, err = otf.add_field(structure, body.sheet, body.dict(),
                                       position=body.position)
        if err:
            raise HTTPException(400, err)
        if _generation_count(s, t) > 0:
            t = _fork(s, t, structure)
        else:
            t.structure = structure
        s.commit()
        s.refresh(t)
        return {"template": _tpl_dict(s, t), "fields": otf.flatten(t.structure or {}),
                "structure": t.structure or {}}


@router.post("/output-template/{template_id}/validate")
def validate(template_id: int, principal: Principal = Depends(current_principal)):
    """Re-check a template. Called after generation and after every edit."""
    with SessionLocal() as s:
        t = _load(s, template_id, principal)
        structure = otf.complete_structure(t.structure or {"sheets": []})
        contract_fields = None
        if t.source_kind == "contract" and t.contract_id:
            import contract_output_fields as cof
            contract_fields = cof.analyze(s, [t.contract_id])["fields"]
        return otv.validate_template(
            structure, standard_fields=_standard_fields_for(t),
            contract_fields=contract_fields,
            extra_fields=_extra_field_keys(s, t.tenant_id))


# ---------------------------------------------------------------------------
# The sample output BDX (plan sections 14/15)
# ---------------------------------------------------------------------------
# The sample is the file the RECIPIENT sends — "this is what we expect back".
# It is not the template and it is not the data, so it is kept where the
# platform already keeps supplied documents: a ReferenceDocument. Its columns
# are parsed ONCE here, at upload, and stored on the record; every later run
# then checks itself against a plain list instead of re-reading a workbook.
#
# A sample is OPTIONAL. Nothing requires one, and a generated file with no
# sample behind it is not "unverified" — it simply has nothing to be compared to.

_SAMPLE_KIND = "output_sample"


def _sample_row(s, template_id: int):
    rows = (s.query(ReferenceDocument)
            .filter(ReferenceDocument.kind == _SAMPLE_KIND)
            .order_by(ReferenceDocument.id.desc()).limit(50).all())
    for r in rows:
        if (r.extracted or {}).get("template_id") == template_id:
            return r
    return None


@router.get("/output-template/{template_id}/sample")
def get_sample(template_id: int, principal: Principal = Depends(current_principal)):
    """What sample, if any, this template is checked against."""
    with SessionLocal() as s:
        t = _load(s, template_id, principal)
        r = _sample_row(s, t.id)
        if not r:
            return {"configured": False, "sample": None}
        ex = r.extracted or {}
        return {"configured": True, "sample": {
            "id": r.id, "filename": r.filename,
            "sheets": [sh.get("sheet_name") for sh in ex.get("sheets") or []],
            "column_count": sum(len(sh.get("columns") or [])
                                for sh in ex.get("sheets") or []),
            "uploaded_at": r.created_at.isoformat() if r.created_at else None,
        }}


@router.post("/output-template/{template_id}/sample")
async def put_sample(template_id: int,
                     file: UploadFile = File(...),
                     principal: Principal = Depends(current_principal)):
    """Attach the sample output BDX the recipient supplied.

    Parsed on the way in so its columns are known; replacing an existing sample
    replaces the record rather than accumulating copies.
    """
    raw = await file.read()
    from exporter import parse_template
    parsed = await run_in_threadpool(parse_template, raw, filename=file.filename)
    sheets = [{"sheet_name": sh.get("sheet_name"),
               "columns": [{"column_name": c.get("column_name")}
                           for c in (sh.get("columns") or [])]}
              for sh in (parsed.get("sheets") or [])]
    if not sheets:
        raise HTTPException(400, "that file has no readable sheets")

    with SessionLocal() as s:
        t = _load(s, template_id, principal)
        blob_ref, blob_bytes = await run_in_threadpool(
            storage.store_or_keep, "references", t.tenant_id, file.filename, raw)
        old = _sample_row(s, t.id)
        if old is not None:
            s.delete(old)
        r = ReferenceDocument(
            tenant_id=t.tenant_id, program_id=t.program_id,
            filename=file.filename, kind=_SAMPLE_KIND,
            blob=blob_bytes, blob_ref=blob_ref,
            extracted={"template_id": t.id, "sheets": sheets})
        s.add(r)
        s.commit()
        s.refresh(r)
        return {"configured": True, "sample": {
            "id": r.id, "filename": r.filename,
            "sheets": [sh["sheet_name"] for sh in sheets],
            "column_count": sum(len(sh["columns"]) for sh in sheets)}}


@router.delete("/output-template/{template_id}/sample")
def clear_sample(template_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        t = _load(s, template_id, principal)
        r = _sample_row(s, t.id)
        if r is not None:
            s.delete(r)
            s.commit()
        return {"configured": False, "sample": None}


@router.post("/output-template/{template_id}/compare-sample")
def compare_sample(template_id: int,
                   principal: Principal = Depends(current_principal)):
    """Check the TEMPLATE's layout against the sample, before any file is made.

    The same comparison a generated file gets, run early — so a mismatch is
    found while the template can still be edited, not after a delivery.
    """
    with SessionLocal() as s:
        t = _load(s, template_id, principal)
        r = _sample_row(s, t.id)
        if not r:
            return {"configured": False, "comparison": None}
        structure = otf.complete_structure(t.structure or {"sheets": []})
        generated = [{"sheet_name": sh.get("sheet_name"),
                      # Headings, not keys — the sample is compared on what the
                      # delivered file would actually say.
                      "headers": [otf.header_of(c) for c in otf.active_columns(sh)],
                      "columns": [c.get("column_name")
                                  for c in otf.active_columns(sh)],
                      "rows": []}
                     for sh in structure.get("sheets") or []]
        return {"configured": True,
                "comparison": otv.compare_with_sample(
                    generated, {"sheets": (r.extracted or {}).get("sheets") or []})}
