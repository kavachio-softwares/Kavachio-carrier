"""
manual_rule_resolution.py
─────────────────────────
Human-in-the-loop resolution of the review queue.

When a rule-bearing clause could not be mapped to any Output-Template field
automatically it lands in `contract_clause_routing` (bucket='review') and its
clause stays `rule_generation_status='in_review'` — see db_persister and
rule_normalizer. This module lets a user pick the Output-Template field the
clause should bind to and re-runs the SAME generation pipeline (Stage A intent
extraction → Stage B IR mapping → verify/compile) for that ONE clause, but with
the candidate field set restricted to the user's choice so every intent is
forced onto it.

It returns the standard (validation_rules, review_queue, control_register)
triple that `normalize_ir_outputs` produces, so the caller can persist the rows
exactly like a fresh contract run.
"""

from __future__ import annotations

from contract_upload_services.contract_data_classifier import extract_rule_intents
from contract_upload_services.stage_b_synthesizer import map_intents_to_ir
from contract_upload_services.rule_normalizer import normalize_ir_outputs


def _augment_clause_with_note(clause, note):
    """Return a copy of `clause` whose `text` has the reviewer's note prepended as
    authoritative guidance. The note is a single free-text field that carries the
    overall RULE LOGIC to enforce for this field/clause AND any reasoning — so Call
    2 extracts the intent from what the reviewer described, not just the raw clause
    wording. No-op when the reviewer gave no note."""
    note = (note or "").strip()
    if not note:
        return clause
    directive = (
        "[REVIEWER NOTE — authoritative guidance for the chosen field. It states "
        "the rule LOGIC to enforce for this clause (and any reasoning); build the "
        "validation rule to match it, overriding any looser reading of the clause "
        "text below: " + note + "]\n\n"
    )
    out = dict(clause)
    out["text"] = directive + (clause.get("text") or "")
    return out


def generate_rules_for_clause_field(clause, chosen_field, template_fields,
                                    output_schema, contract_ctx, note=None,
                                    extra_field_names=None):
    """Re-run rule generation for a SINGLE clause, forcing it onto `chosen_field`.

    Args:
        clause:         dict for the existing DB clause — must carry `clause_id`
                        (the real clauses_extracted.clause_id) and `text`; `title`,
                        `clause_type`, `page_number`, `section_header` improve the
                        extraction prompt.
        chosen_field:   the PRIMARY template-field dict the user picked (one entry
                        from app_routes._template_fields_from_structure). Its `name`
                        MUST be a real column in `output_schema`.
        template_fields: the FULL template-field list. The mapper sees all of them
                        (so scope/group_by params can bind) but is DIRECTED to bind
                        the rule's primary value to `chosen_field`.
        output_schema:  the FULL OutputSchema (used for compile + verify so scope
                        columns and sample-data smoke tests still resolve).
        contract_ctx:   {tenant_id, contract_id, program_id}.
        note:           optional single free-text note the reviewer wrote — it holds
                        the overall RULE LOGIC to enforce for this field/clause plus
                        any reasoning. It is injected as authoritative guidance into
                        the clause so Call 2/Call 3 build the rule to it, AND recorded
                        on each generated rule (rule_spec.resolution_note) for
                        reference/audit.
        extra_field_names: additional Output-Template column names the reviewer
                        selected because the rule spans MULTIPLE columns (e.g. a
                        nested carve-out on country + state, or a two-column
                        conditional). The mapper is told to use them for the rule's
                        scope / condition(s) / other operands alongside the primary.

    Returns:
        (validation_rules, review_queue, control_register) — same shape as
        normalize_ir_outputs. `validation_rules` is empty when the clause still
        cannot be expressed against the chosen field (kind mismatch, no intent,
        etc.); the reason is then in review_queue/control_register.
    """
    forced_name = chosen_field.get("name")
    note = (note or "").strip()
    # Primary first, then the reviewer's other picks (deduped, primary excluded).
    forced_fields = [forced_name] + [
        f for f in (extra_field_names or [])
        if f and f != forced_name
    ]

    # Fold the reviewer's note (its stated logic) into the clause the extractor sees.
    clause_for_gen = _augment_clause_with_note(clause, note)

    # Stage A (Call 2): classify + extract rule intents for just this clause.
    classifications = extract_rule_intents([clause_for_gen])
    clf = classifications[0] if classifications else {}

    # Stage B (Call 3): map the clause's intents to IR with the FULL field list
    # (so the mapper can bind scope/group_by columns) but DIRECTED onto the field(s)
    # the user chose. When several were chosen the rule spans them (scope/condition).
    synth_outputs = map_intents_to_ir(
        [clause_for_gen], [clf], template_fields=template_fields,
        forced_field=forced_name,
        forced_fields=forced_fields if len(forced_fields) > 1 else None,
    )

    # Verify / compile / route against the FULL schema (deterministic).
    validation_rules, review_queue, control_register = normalize_ir_outputs(
        synth_outputs, contract_ctx, output_schema)

    # Stamp the reviewer's note + the manual-selection provenance onto every
    # generated rule so it persists (rule_spec is stored as JSONB) and is visible
    # for later reference.
    for r in validation_rules:
        if not isinstance(r, dict):
            continue
        spec = r.get("rule_spec")
        if not isinstance(spec, dict):
            spec = {}
        spec["field_resolution"] = "manual"
        spec["resolved_output_field"] = forced_name
        if len(forced_fields) > 1:
            spec["resolved_output_fields"] = forced_fields
        if note:
            spec["resolution_note"] = note
        r["rule_spec"] = spec

    return validation_rules, review_queue, control_register
