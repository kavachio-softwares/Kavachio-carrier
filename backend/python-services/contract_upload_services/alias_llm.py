"""Merge LLM-proposed cross-sheet field aliases into OutputSchema — no LLM call.

The Call-3 mapping call (build_ir_mapping_prompt_batch) already sees every
sheet's columns with samples while it binds each rule intent to a field; it now
ALSO reports, per bound field, the same-meaning column on sheets that spell the
concept differently ("field_aliases" on each result — see stage_b_synthesizer).
So the semantic judgment happens inside the one existing mapping call, not in a
separate LLM pass.

This module is the deterministic other half: every proposal must survive the
same guards the mechanical alias layers use before it becomes an alias —
  * the sheet must exist and be a genuine GAP for that field (the field is not
    already present there by name, nor already aliased by the mechanical
    layers: canonical concept / normalized name / shared id values);
  * the proposed column must actually exist on that sheet (no hallucinations);
  * the value-kind families must not contradict (an amount never aliases to a
    code) — OutputSchema._same_value_kind, sample-driven.
An alias makes a rule FIRE against the column, so a wrong one is a false
exception on innocent data; anything not provably safe is dropped. Best-effort:
a malformed proposal set merges nothing and never blocks rule building.
"""
from __future__ import annotations


def _covered_sheets(schema, field: str) -> set:
    """Sheets where `field` is already reachable: present under its own name, or
    already aliased by the mechanical layers."""
    covered = set(schema.field_to_sheets.get(field) or [])
    covered.update((schema.field_aliases.get(field) or {}).keys())
    return covered


def merge_field_aliases(output_schema, proposals: dict, label="AliasMerge") -> int:
    """Merge the mapping call's per-result "field_aliases" proposals into
    output_schema.field_aliases. A proposal is {field: [equivalent column
    names]} — the model names WHICH columns mean the same thing, and the
    name→sheet expansion happens HERE, deterministically, from field_to_sheets
    (the prompt's deduped field list doesn't show per-sheet placement, so the
    model is never asked to produce sheet names it cannot know). A legacy
    {field: {sheet: column}} shape is tolerated. Mutates in place; returns how
    many aliases were added. Never raises."""
    added = 0
    try:
        for field, equivs in (proposals or {}).items():
            field = str(field)
            if field not in output_schema.field_names:
                continue
            # Normalize both shapes to a list of equivalent column names.
            if isinstance(equivs, dict):
                names = list(equivs.values())
            elif isinstance(equivs, (list, tuple, set)):
                names = list(equivs)
            else:
                continue
            covered = _covered_sheets(output_schema, field)
            home = output_schema.field_to_sheet.get(field)
            for other in names:
                other = str(other).strip()
                if not other or other == field:
                    continue
                if other not in output_schema.field_names:
                    continue                  # hallucinated column name
                for sheet in (output_schema.field_to_sheets.get(other) or []):
                    if sheet in covered:
                        continue              # field already reachable there
                    if home and not output_schema._same_value_kind(home, field, sheet, other):
                        continue              # amount vs code — different thing
                    output_schema.field_aliases.setdefault(field, {})[sheet] = other
                    covered.add(sheet)
                    added += 1
        if added:
            print(f"[{label}] merged {added} semantic alias(es) from the mapping call")
    except Exception as exc:  # noqa: BLE001 — advisory; rules must still build
        print(f"[{label}] skipped ({exc})")
    return added
