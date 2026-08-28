"""
validation_rule_generator.py
────────────────────────────
Orchestrates the full Kavachio core module — Pipeline 1 + Pipeline 2 —
per Kavachio_Pipeline_Architecture.png + kavachio-contract-validations docs.

Flow (per-contract):

  Pipeline 1 — Contract Extraction
    1.1 Pre-processing               (PDF → page text, done in document_extractors)
    1.2 Section identification       (prompt_builder.split_into_sections)
    1.3 Structured extraction        ← LLM call #1, per section
    1.4 Metadata synthesis & dedup
    1.5 Persistence (program_metadata, clauses_extracted)

  Pipeline 2 — Rule Generation
    2.2 Stage A: Classification      ← LLM call #2, batched over clauses
    2.4-A Stage B: AJV synthesis     ← LLM call #3, per AJV clause
    2.4-B Stage B: Custom synthesis  ← LLM call #4, per custom clause
    2.5 Normalization (deterministic — AJV compile, canonical fields, threshold)
    2.6 Rule persistence (validation_rule rows)

Output:
  - "validation_rules" : new ajv/custom rule rows (the architecture target)

The legacy class_name pipeline has been removed. For back-compat, the output
still carries "contract_rules" (empty) and an "analysis" block whose
program_name / document_type are sourced from Pipeline 1 program_metadata.
"""

import os
import re
import json
import uuid
from datetime import datetime, timezone


# Default ±% tolerance stamped onto auto-derived cross_field_math (formula) rules
# — the small rounding slack between a reported amount and base × rate / base −
# amount. Env-overridable so an operator can widen/tighten the fleet default
# without a code change; a single rule can then be tuned further via the
# tolerance-band edit endpoint (rule_editor.patch_tolerance). 1.0 == 1%.
DEFAULT_CROSS_FIELD_TOLERANCE_PCT = float(
    os.getenv("KAVACHIO_DEFAULT_TOLERANCE_PCT", "1.0"))


# In-memory cache of completed extractions, keyed by a resume_token. When the
# pipeline halts on external references, the parsed extraction is stashed here so
# "Continue Anyway" can resume WITHOUT re-calling the extraction LLM.
# Note: process-local (not shared across uvicorn workers) and cleared on restart;
# entries are popped on use. Fine for the interactive upload flow.
_EXTRACTION_RESUME_CACHE: dict = {}

from contract_upload_services.constants import (
    RULE_CLASS_LIBRARY,
    DEFAULT_RULE_AUTO_TRUST_THRESHOLD
)

from contract_upload_services.gemini_service import (
    call_gemini, DETERMINISTIC_SEED, OversizeError, should_chunk,
)

from contract_upload_services.prompt_builder import (
    split_into_sections,
    build_extraction_prompt,
    build_llm_context,
    split_pages_into_chunks
)

from contract_upload_services.contract_data_classifier import (
    classify_clauses_batch,
    extract_rule_intents,
    resolve_status,
    save_stage_a_output
)

from contract_upload_services.stage_b_synthesizer import (
    synthesize_rules_ir,
    map_intents_to_ir,
)

from contract_upload_services.rule_normalizer import (
    parse_llm_json,
    normalize_ir_outputs,
    build_reference_group_members,
)

from contract_upload_services.output_schema import build_output_schema


def _looks_percent(field: dict) -> bool:
    """True when a rate/percentage column stores values as PERCENT (e.g. "23.5")
    rather than a 0-1 fraction, so a formula using it must divide by 100."""
    fmt = (field.get("field_format") or "").lower()
    if "percent" in fmt or "%" in fmt:
        return True
    if "fraction" in fmt:
        return False
    for s in (field.get("samples") or []):
        try:
            if abs(float(str(s).replace("%", "").replace(",", "").strip())) > 1.5:
                return True   # e.g. 23.5 → percent
        except (TypeError, ValueError):
            continue
    return True   # rates are percent by convention unless proven fractional


def derive_formula_entries(synth_outputs, template_fields):
    """#6 — Derive cross-field FORMULA rules the contract implies but never spells
    out. When the output template carries a matching  <concept> Amount + base
    Premium + <concept> Rate  trio AND the contract already governs that rate
    (a mapped rule targets the rate column), add ONE cross_field_math rule
    (Amount = Premium × Rate, within tolerance) so the reported amount is checked
    against the rate — not just the rate in isolation.

    Returns a list of synth_output entries (same shape map_intents_to_ir emits) to
    append; empty when the trio/rate-rule isn't present. Deterministic, no LLM."""
    names = [f.get("name") for f in (template_fields or []) if f.get("name")]
    by_name = {f.get("name"): f for f in (template_fields or []) if f.get("name")}

    def find(*needs, avoid=()):
        for n in names:
            ln = n.lower()
            if all(w in ln for w in needs) and not any(a in ln for a in avoid):
                return n
        return None

    # Fields the rate rule already targets (so we only derive for governed rates).
    governed = set()
    for entry in synth_outputs:
        for ir in (entry.get("candidates") or []):
            fld = (ir.get("params") or {}).get("field")
            if fld:
                governed.add(fld)

    entries = []
    # Commission: Commission Amount = Gross Premium × Commission Rate.
    amount = find("commission", "amount")
    rate = find("commission", "rate") or find("commission", "%")
    base = find("gross", "premium") or find("premium", avoid=("net", "fac", "annual"))
    if amount and rate and base and rate in governed:
        ir = {
            "template": "cross_field_math",
            "params": {
                "result_field": amount, "left_field": base,
                "operator": "*", "right_field": rate,
                "right_is_percent": _looks_percent(by_name.get(rate, {})),
                "tolerance_pct": DEFAULT_CROSS_FIELD_TOLERANCE_PCT,
            },
            "rule_name": f"{amount} equals {base} × {rate}",
            "rule_description": (
                f"{amount} must equal {base} multiplied by {rate} "
                f"(within a small tolerance for rounding)."),
            "severity": "warning",
            "error_message": f"{amount} does not match {base} × {rate}.",
            "confidence": 1.0,
        }
        entries.append({
            "clause": {"clause_id": None,
                       "text": f"[Derived formula] {amount} = {base} × {rate}",
                       "page_number": None},
            "engine": "ir",
            "candidates": [ir],
        })
    return entries


class ValidationRuleGenerator:

    def __init__(
        self,
        auto_trust_threshold=DEFAULT_RULE_AUTO_TRUST_THRESHOLD,
        tenant_id=None,
        program_id=None
    ):
        self.rule_class_library    = RULE_CLASS_LIBRARY
        self.auto_trust_threshold  = auto_trust_threshold
        self.tenant_id             = tenant_id
        self.program_id            = program_id

    # =====================================================
    # PUBLIC: full pipeline
    # =====================================================

    def generate_validation_rules_json(
        self,
        pdf_data,
        source_file,
        output_dir=None,
        template_fields=None,
        halt_on_external_references=False,
        resume_token=None,
        reference_documents=None,
    ):
        """
        Run Pipeline 1 + Pipeline 2 on the parsed PDF data and return a
        single hybrid output dict. Safe to JSON-serialize.

        When `output_dir` is set, raw Stage B synthesis outputs are also
        written to two side-car JSON files:
          <output_dir>/<contract_id>_stage_b_ajv.json
          <output_dir>/<contract_id>_stage_b_custom.json
        """

        contract_id = self._derive_contract_id(source_file)
        file_base   = self._safe_filename(source_file, contract_id)

        # # Resolve output_dir — default to "validation_output_rules" so files
        # # are always written even when the caller omits the argument.
        # if output_dir is None:
        #     output_dir = "validation_output_rules"

        # os.makedirs(output_dir, exist_ok=True)

        # -------------------------------------------------
        # PIPELINE 1.2 — Section identification  (DISABLED)
        # -------------------------------------------------
        # Section-splitting + per-section extraction is commented out in favour
        # of feeding the whole document to the LLM in a single call below.
        #
        # sections = split_into_sections(pdf_data)
        #
        # if not sections:
        #     sections = split_pages_into_chunks(pdf_data, pages_per_chunk=5)
        #
        # print(
        #     f"\n[Pipeline 1] Identified {len(sections)} section(s) "
        #     f"for extraction."
        # )

        # -------------------------------------------------
        # PIPELINE 1.3 — Structured extraction per section  (DISABLED)
        # -------------------------------------------------
        # section_extractions = []
        #
        # for idx, section in enumerate(sections, start=1):
        #
        #     label = (
        #         f"Pipeline1-Sec{idx}/"
        #         f"{len(sections)}-{section.get('section_type')}"
        #     )
        #
        #     try:
        #
        #         raw = call_gemini(
        #             build_extraction_prompt(section),
        #             label=label
        #         )
        #
        #         section_extractions.append(parse_llm_json(raw))
        #
        #     except Exception as exc:
        #
        #         print(f"[Pipeline 1] section {idx} failed: {exc}")
        #         section_extractions.append({
        #             "program_metadata": {},
        #             "commercial_terms": [],
        #             "clauses": []
        #         })

        # -------------------------------------------------
        # PIPELINE 1.3 (ACTIVE) — Single whole-document extraction
        # Feed the ENTIRE document to the LLM in ONE Gemini call instead of
        # splitting into sections. _merge_section_extractions still handles a
        # one-element list, so the rest of the pipeline is unchanged.
        # -------------------------------------------------

        section_extractions = []
        external_references = []

        # -------------------------------------------------
        # RESUME PATH — reuse the cached extraction from the halted run instead
        # of calling the extraction LLM again. (Triggered by "Continue Anyway".)
        # -------------------------------------------------
        cached = _EXTRACTION_RESUME_CACHE.pop(resume_token, None) if resume_token else None

        if cached is not None:
            section_extractions = cached.get("section_extractions", [])
            external_references = cached.get("external_references", [])
            print(
                f"\n[Pipeline 1] RESUMED from cached extraction "
                f"(token={resume_token}) — skipping extraction LLM call.\n"
            )

        else:
            # -------------------------------------------------
            # PIPELINE 1.3 — WHOLE-DOCUMENT extraction (ONE call)
            # The whole contract goes to the model in a single call so it reasons
            # about the document as a COHERENT WHOLE — cross-page context,
            # definitions that qualify later limits, and clauses that span a page
            # break are all preserved. (Per-page chunking gave higher recall but
            # stripped overall meaning.) The recall problem that motivated chunking
            # was output-token TRUNCATION on long JSON, so we raise
            # max_output_tokens and rely on the prompt's strict "emit EVERY clause"
            # rule instead of fragmenting the document.
            # _merge_section_extractions still handles a one-element list, so the
            # rest of the pipeline is unchanged.
            # -------------------------------------------------
            pages = pdf_data.get("pages", [])
            whole_doc = {
                "section_type": "full_document",
                "page_start":   pages[0]["page"] if pages else 0,
                "page_end":     pages[-1]["page"] if pages else 0,
                "text":         build_llm_context(pdf_data),
            }
            print(f"\n[Pipeline 1] extracting whole document in 1 call "
                  f"({len(pages)} page(s)).")
            if reference_documents:
                print(
                    f"[Pipeline 1] with {len(reference_documents)} reference "
                    f"document(s): {[rd.get('name') for rd in reference_documents]}"
                )

            ext_prompt = build_extraction_prompt(
                whole_doc, reference_documents=reference_documents,
            )

            def _extract_by_section(reason):
                """Graceful fallback: extract page-chunks and merge, instead of
                returning an EMPTY skeleton (total loss). Accepts minor cross-page
                context loss over losing every clause."""
                pages_per = int(os.getenv("KAVACHIO_SECTION_PAGES", "5"))
                chunks = split_pages_into_chunks(pdf_data, pages_per_chunk=pages_per)
                print(f"[Pipeline 1] {reason} — extracting by section "
                      f"({len(chunks)} chunk(s) of {pages_per} page(s)).")
                added = 0
                for sec in chunks:
                    try:
                        raw = call_gemini(
                            build_extraction_prompt(
                                sec, reference_documents=reference_documents),
                            label=f"Pipeline1-Section-p{sec.get('page_start')}",
                            max_output_tokens=65536, thinking_budget=16384,
                            temperature=0, seed=DETERMINISTIC_SEED,
                        )
                        parsed_sec = parse_llm_json(raw)
                        section_extractions.append(parsed_sec)
                        added += 1
                        if isinstance(parsed_sec, dict):
                            external_references.extend(
                                parsed_sec.get("external_references", []) or [])
                    except Exception as se:
                        print(f"[Pipeline 1] section p{sec.get('page_start')} "
                              f"failed: {se}")
                return added

            # Pre-flight: if the whole document is too large for one call, go
            # straight to sectioned extraction rather than truncating.
            over, est, ceil = should_chunk(ext_prompt, 65536, 16384)
            if over:
                if _extract_by_section(f"whole-doc ~{est} tok > ceiling {ceil}") == 0:
                    section_extractions.append(
                        {"program_metadata": {}, "commercial_terms": [], "clauses": []})
            else:
                try:
                    raw = call_gemini(
                        ext_prompt,
                        label="Pipeline1-FullDocument",
                        max_output_tokens=65536,
                        thinking_budget=16384,
                        temperature=0,
                        seed=DETERMINISTIC_SEED,
                    )
                    parsed = parse_llm_json(raw)
                    section_extractions.append(parsed)
                    if isinstance(parsed, dict):
                        external_references.extend(
                            parsed.get("external_references", []) or []
                        )
                except Exception as exc:
                    # Oversize or a failed/truncated call → try sectioned extraction
                    # BEFORE giving up with an empty skeleton (total loss).
                    reason = ("oversize" if isinstance(exc, OversizeError)
                              else f"full-document extraction failed: {exc}")
                    if _extract_by_section(reason) == 0:
                        section_extractions.append({
                            "program_metadata": {},
                            "commercial_terms": [],
                            "clauses": [],
                        })

        # -------------------------------------------------
        # HALT GATE — pause before the expensive Pipeline 2 when the contract
        # defers rules to external documents. The parsed extraction is cached
        # under a resume_token so "Continue Anyway" resumes WITHOUT re-extracting.
        # The route returns the token + reference names to the UI.
        # -------------------------------------------------
        if halt_on_external_references and external_references:
            token = uuid.uuid4().hex
            _EXTRACTION_RESUME_CACHE[token] = {
                "section_extractions": section_extractions,
                "external_references": external_references,
            }
            print(
                f"[Pipeline 1] HALTED — {len(external_references)} external "
                f"reference(s) found; cached as token={token}; awaiting user action."
            )
            return {
                "halted_for_references": True,
                "external_references":   external_references,
                "resume_token":          token,
                "metadata": {
                    "generated_at":            datetime.now(timezone.utc).isoformat(),
                    "source_file":             source_file,
                    "contract_id":             contract_id,
                    "halted_for_references":   True,
                    "external_reference_count": len(external_references),
                },
                "validation_rules":  [],
                "clauses_extracted": [],
            }

        # -------------------------------------------------
        # PIPELINE 1.4 — Synthesize & dedup
        # -------------------------------------------------

        program_metadata, commercial_terms, clauses_extracted = (
            self._merge_section_extractions(section_extractions)
        )

        # Reconcile each clause's page deterministically by matching its text back
        # to the source pages (the LLM no longer sees page markers).
        self._assign_clause_pages(clauses_extracted, pdf_data)

        # Assign stable clause_ids
        for i, c in enumerate(clauses_extracted, start=1):
            c["clause_id"] = i
            c["contract_id"] = contract_id
            c["rule_generation_status"] = "pending"

        # Resolve clause hierarchy (Root B): the extractor numbers clauses with its
        # own `local_id` and points each child at its parent via `parent_local_id`.
        # Map those to the real assigned clause_ids; unknown/absent parent → None.
        _local_to_id = {c.get("local_id"): c["clause_id"]
                        for c in clauses_extracted if c.get("local_id") is not None}
        for c in clauses_extracted:
            c["parent_clause_id"] = _local_to_id.get(c.get("parent_local_id"))

        print(
            f"[Pipeline 1] merged: "
            f"{len(commercial_terms)} commercial_terms, "
            f"{len(clauses_extracted)} clauses_extracted."
        )

        # --- Console the final Pipeline 1 (extraction) output ---
        pipeline1_output = {
            "contract_id":        contract_id,
            "source_file":        source_file,
            "program_metadata":   program_metadata,
            "commercial_terms":   commercial_terms,
            "clauses_extracted":  clauses_extracted,
            "external_references": external_references,
        }
        print("\n" + "=" * 60)
        print("FINAL PIPELINE 1 OUTPUT (extraction)")
        print("=" * 60)
        print(json.dumps(pipeline1_output, indent=2, default=str))
        print("=" * 60 + "\n")

        # # -------------------------------------------------
        # # PIPELINE 1.5 — Persist Pipeline 1 output to JSON
        # # -------------------------------------------------

        # pipeline1_output = {
        #     "stage":            "Pipeline 1 — Contract Extraction",
        #     "contract_id":      contract_id,
        #     "generated_at":     datetime.now(timezone.utc).isoformat(),
        #     "source_file":      source_file,
        #     "summary": {
        #         "clauses_extracted_count": len(clauses_extracted),
        #         "commercial_terms_count":  len(commercial_terms)
        #     },
        #     "program_metadata":  program_metadata,
        #     "commercial_terms":  commercial_terms,
        #     "clauses_extracted": clauses_extracted
        # }

        # pipeline1_path = os.path.join(output_dir, f"{file_base}_pipeline1.json")

        # with open(pipeline1_path, "w") as _f:
        #     json.dump(pipeline1_output, _f, indent=2, default=str)

        # print(f"[Pipeline 1] saved extraction output → {pipeline1_path}")

        # -------------------------------------------------
        # CALL 2 — rule_bearing + rule INTENT (field-agnostic)
        # Merges Stage A classification with intent extraction in one call. The
        # result is classification-shaped (is_rule_bearing, rule_types, …) and
        # also carries an `intents` list that Call 3 maps to output fields.
        # -------------------------------------------------

        classifications = extract_rule_intents(clauses_extracted)

        # Update each clause's rule_generation_status from the verdict
        for clause, classification in zip(clauses_extracted, classifications):
            clause["rule_generation_status"] = resolve_status(classification)
            clause["classification"] = classification

        # # Persist Stage A classification side-car JSON (mirrors Stage B files).
        # save_stage_a_output(
        #     clauses_extracted,
        #     classifications,
        #     output_dir,
        #     contract_id=contract_id,
        #     file_base=file_base
        # )

        # -------------------------------------------------
        # PIPELINE 2.4 — Stage B (IR) extraction
        # -------------------------------------------------

        # No hardcoded confidence cutoff: confidence is advisory (the verify gate
        # is the real gate), and is_rule_bearing routing happens inside Stage B —
        # non-rule-bearing clauses go to the control register, nothing is dropped.
        # Pass ALL clauses; the synthesizer partitions rule-bearing vs not.
        rule_bearing = sum(
            1 for c in classifications if c.get("is_rule_bearing")
        )
        print(
            f"[Pipeline 2] Stage B IR extraction on "
            f"{rule_bearing} rule-bearing clause(s) "
            f"(of {len(clauses_extracted)} total)."
        )

        if template_fields:
            print(
                f"[Pipeline 2] Using template-aware IR extraction "
                f"({len(template_fields)} output template fields)"
            )

        # Output Template = the canonical field namespace every rule is written
        # against (field_names for the existence gate, field_to_sheet for the
        # compiler, grouped list for the prompt).
        output_schema = build_output_schema(template_fields)

        # CALL 3 — map each rule intent (from Call 2) to ONE template + Output
        # Template fields. This focused mapping step recovers checkable rules
        # (territory, policy-period, products-aggregate) that the old combined
        # "extract + map" call under-mapped. Output → IR candidates per clause.
        synth_outputs = map_intents_to_ir(
            clauses_extracted,
            classifications,
            template_fields=template_fields,
        )

        # #6 — DERIVED formula rules (deterministic, not from a single clause):
        # e.g. Commission Amount = Gross Premium × Commission Rate. Added only when
        # the template has the matching column trio AND the contract already
        # governs that rate, so the reported amount is checked, not just the rate.
        derived = derive_formula_entries(synth_outputs, template_fields)
        if derived:
            print(f"[Call 3] +{len(derived)} derived formula rule(s): "
                  f"{[e['clause']['text'] for e in derived]}")
            synth_outputs.extend(derived)

        # -------------------------------------------------
        # PIPELINE 2.5 — Verify gate + routing (deterministic)
        # -------------------------------------------------

        contract_ctx = {
            "tenant_id":   self.tenant_id,
            "contract_id": contract_id,
            "program_id":  self.program_id
        }

        # Reference-doc GROUP → MEMBERS map (data-driven, from the uploaded
        # reference documents' tables). Lets a value-set rule whose values are
        # category/group names (e.g. authorized/excluded "Occupancy Group"s) be
        # expanded to also carry every specific member the reference lists under
        # that group, so a BDX row reporting a specific class matches.
        group_members = build_reference_group_members(reference_documents)
        if group_members:
            print(f"[Pipeline 2.5] reference group→members map: "
                  f"{len(group_members)} group(s) "
                  f"{[v[0] for v in group_members.values()]}")

        # Each IR is verified (validate → vocab-normalize → field-existence →
        # compile → guard/dry-run) and routed to exactly one destination.
        validation_rules, review_queue, control_register = normalize_ir_outputs(
            synth_outputs,
            contract_ctx,
            output_schema,
            group_members=group_members,
        )
        # Kept under the legacy name for the final-output builder below.
        dropped = review_queue

        print(
            f"[Pipeline 2.5] {len(validation_rules)} proposed rule(s), "
            f"{len(review_queue)} to review, "
            f"{len(control_register)} to control register."
        )

        # # -------------------------------------------------
        # # PIPELINE 2.5 — Persist normalization output to JSON
        # # -------------------------------------------------

        # save_normalization_output(
        #     validation_rules,
        #     dropped,
        #     output_dir,
        #     contract_id=contract_id,
        #     file_base=file_base
        # )

        # -------------------------------------------------
        # Build final output
        # -------------------------------------------------

        final_output = self._build_final_output(
            source_file=source_file,
            contract_id=contract_id,
            program_metadata=program_metadata,
            commercial_terms=commercial_terms,
            clauses_extracted=clauses_extracted,
            classifications=classifications,
            validation_rules=validation_rules,
            dropped_candidates=dropped,
            review_queue=review_queue,
            control_register=control_register,
        )

        # # -------------------------------------------------
        # # PIPELINE 2.6 — Persist final output to JSON
        # # -------------------------------------------------

        # final_path = os.path.join(output_dir, f"{file_base}_final_output.json")

        # with open(final_path, "w") as _f:
        #     json.dump(final_output, _f, indent=2, default=str)

        # print(f"[Pipeline 2.6] saved final output → {final_path}")

        return final_output

    # =====================================================
    # PIPELINE 1.4 — Merge per-section extractions
    # =====================================================

    @staticmethod
    def _assign_clause_pages(clauses, pdf_data):
        """Set each clause's page_number deterministically by finding where its
        text occurs in the source pages. The extraction text is sent WITHOUT page
        markers, so the LLM can't reliably report pages; we reconcile here by
        matching a normalized snippet of the clause (its text, else its title)
        against each page's normalized text and taking the page where it STARTS.
        Falls back to whatever the model gave if no match is found."""
        import re as _re

        def _norm(s):
            return _re.sub(r"\s+", " ", (s or "")).strip().lower()

        page_texts = [
            (p.get("page"), _norm(p.get("text", "")))
            for p in (pdf_data.get("pages") or [])
        ]

        for cl in clauses:
            if not isinstance(cl, dict):
                continue
            for key in ("text", "title"):
                snippet = _norm(cl.get(key))[:60]
                if len(snippet) < 12:   # too short to match reliably
                    continue
                match = next((pg for pg, ptext in page_texts
                              if snippet in ptext), None)
                if match is not None:
                    cl["page_number"] = match
                    cl["page"] = match
                    break
        return clauses

    def _merge_section_extractions(self, section_extractions):

        merged_metadata = {}
        commercial_terms = []
        clauses = []

        for sec in section_extractions:

            # Gemini occasionally returns a JSON array (or other non-object)
            # for a section instead of the expected object. Skip those rather
            # than crashing with "'list' object has no attribute 'get'".
            if not isinstance(sec, dict):
                print(
                    f"[Pipeline 1.4] skipping non-object section extraction "
                    f"(got {type(sec).__name__})"
                )
                continue

            metadata = sec.get("program_metadata")
            if isinstance(metadata, dict):

                for k, v in metadata.items():

                    # Each field must be a {value, source_text, page, confidence}
                    # wrapper. The model sometimes emits a bare list/scalar —
                    # skip anything that isn't a dict.
                    if not isinstance(v, dict):
                        continue

                    if v.get("value") in (None, "", []):
                        continue

                    existing = merged_metadata.get(k)

                    # Keep the highest-confidence value; if equal, append both
                    if (
                        not existing
                        or (v.get("confidence", 0) or 0)
                           > (existing.get("confidence", 0) or 0)
                    ):
                        merged_metadata[k] = v

            for ct in sec.get("commercial_terms") or []:
                commercial_terms.append(ct)

            for cl in sec.get("clauses") or []:
                if isinstance(cl, dict):
                    clauses.append(cl)

        # Dedup clauses on EXACT (title, text) — only collapse entries that are
        # truly identical, so NO distinct clause is ever lost. (Deduping on text
        # alone was too aggressive: two genuinely different clauses that happen to
        # share an identical short body — e.g. "Restricted Segments Definition" and
        # "Excluded Classes Definition" both "as defined by the … Guidelines" —
        # would be wrongly merged, dropping a real clause.) Duplicate *rules* that
        # arise when two differently-worded clauses compile to the same check are
        # handled separately by the rule-level dedup in normalize_ir_outputs.
        seen = set()
        deduped_clauses = []

        for cl in clauses:

            title = cl.get("title") or ""
            text = cl.get("text") or ""

            if isinstance(title, dict):
                title = str(title)

            if isinstance(text, dict):
                text = str(text)

            norm_title = re.sub(r"\s+", " ", title).strip().lower()
            norm_text = re.sub(r"\s+", " ", text).strip().lower()
            # For a SUBSTANTIAL body, identical text = the SAME clause even if the
            # model gave it a different title (e.g. the same table row extracted
            # twice — once from the [TABLE] grid, once from the surrounding prose).
            # For a SHORT body keep the title distinction, since two genuinely
            # different clauses can share a short body ("as defined in the …
            # Guidelines").
            sig = (norm_text,) if len(norm_text) > 40 else (norm_title, norm_text)

            if sig in seen:
                continue

            seen.add(sig)
            deduped_clauses.append({
                "clause_type": cl.get("clause_type", "other"),
                "title": cl.get("title", ""),
                "text": cl.get("text", ""),
                "page_number": cl.get("page"),
                "section_header": cl.get("section_header"),
                "source_reference_document": cl.get("source_reference_document"),
                "extraction_confidence": cl.get("confidence", 0.0)
            })
            
        return merged_metadata, commercial_terms, deduped_clauses

    # =====================================================
    # Build the final output document
    # =====================================================

    def _build_final_output(
        self,
        source_file,
        contract_id,
        program_metadata,
        commercial_terms,
        clauses_extracted,
        classifications,
        validation_rules,
        dropped_candidates,
        review_queue=None,
        control_register=None,
    ):

        review_queue = review_queue or []
        control_register = control_register or []

        now = datetime.now(timezone.utc).isoformat()

        # program_name / document_type now come from Pipeline 1 program_metadata
        # (each metadata field is a {value, source_text, page, confidence} dict).
        program_name  = (program_metadata.get("program_name")  or {}).get("value")
        document_type = (program_metadata.get("document_type") or {}).get("value") \
            or "Insurance Document"

        # Stage A summary stats
        total = len(classifications)
        bearing = sum(
            1 for c in classifications if c.get("is_rule_bearing")
        )
        errors = sum(1 for c in classifications if c.get("_error"))

        # Stage B (IR) summary stats — every generated rule is a proposal pending
        # human approval (persisted as 'needs_review'); template is the unit, not
        # an ajv/custom engine.
        proposed_count = len(validation_rules)
        by_template = {}
        for r in validation_rules:
            t = r.get("template") or "unknown"
            by_template[t] = by_template.get(t, 0) + 1

        return {
            "metadata": {
                "generated_at":        now,
                "source_file":         source_file,
                "contract_id":         contract_id,
                "document_type":       document_type,
                "program_name":        program_name,
                "contract_rule_count": 0,
                "validation_rule_count": len(validation_rules),

                "pipeline_1_summary": {
                    "clauses_extracted_count":  len(clauses_extracted),
                    "commercial_terms_count":   len(commercial_terms)
                },

                "stage_a_summary": {
                    "total_clauses":    total,
                    "rule_bearing":     bearing,
                    "not_rule_bearing": total - bearing - errors,
                    "errors":           errors
                },

                "stage_b_summary": {
                    "proposed":         proposed_count,
                    "by_template":      by_template,
                    "review_queue":     len(review_queue),
                    "control_register": len(control_register),
                }
            },

            # === Pipeline 1 outputs ===
            "program_metadata":  program_metadata,
            "commercial_terms":  commercial_terms,
            "clauses_extracted": clauses_extracted,

            # === Pipeline 2 outputs (deterministic IR model) ===
            "validation_rules":   validation_rules,
            "dropped_candidates": dropped_candidates,
            "review_queue":       review_queue,
            "control_register":   control_register,

            # # === Kept for downstream-consumer back-compat ===
            # # Legacy class_name pipeline removed; these keys remain (empty /
            # # derived) so the output shape stays stable for consumers.
            # "analysis": {
            #     "program_name":  program_name,
            #     "document_type": document_type
            # },
            # "rule_class_library":    self.rule_class_library,
            # "contract_rules":        []
        }

    # =====================================================
    # Helpers
    # =====================================================

    @staticmethod
    def _safe_filename(source_file, contract_id):

        base = os.path.splitext(
            os.path.basename(source_file or "")
        )[0]

        if not base:
            base = contract_id

        safe = re.sub(r"[^a-zA-Z0-9_\-]+", "_", base)

        return safe

    @staticmethod
    def _derive_contract_id(source_file):

        base = os.path.splitext(os.path.basename(source_file or ""))[0]

        slug = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_").upper()

        if not slug:
            slug = "CONTRACT"

        return f"{slug}_v1"