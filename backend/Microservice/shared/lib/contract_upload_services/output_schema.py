"""
output_schema.py
────────────────
The Output Template is the canonical field namespace every rule is written
against. This module derives, from the `template_fields` list (each entry is
{"name", "sheet", "canonical_field", "samples", ...} — see
app_routes._template_fields_from_structure), the three structures the rest of the
generation path needs:

    field_names     : set[str]            — for the field-existence gate (validate_ir)
    field_to_sheet  : {field -> sheet}    — for the compiler (compile_ir, single-sheet)
    fields_by_sheet : {sheet -> [field]}  — for the Stage-B prompt (grouped listing)

plus a small sample-row set (per sheet) used by the verify gate's smoke test.
"""

from __future__ import annotations

import re


def _norm_field(name: str) -> str:
    """Normalize a field name for tolerant matching: lower-case, and collapse any
    run of non-alphanumeric characters to a single space. So 'Occ. Limit',
    'occurrence  limit', 'Occurrence Limit ' all compare equal to their canonical
    form. Conservative enough not to merge genuinely different fields."""
    return re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()


class OutputSchema:
    def __init__(self, template_fields: list[dict] | None):
        self.template_fields = template_fields or []
        self.field_names: set[str] = set()
        self.field_to_sheet: dict[str, str] = {}
        self.field_to_sheets: dict[str, list[str]] = {}   # field -> EVERY sheet it lives on
        self.fields_by_sheet: dict[str, list[str]] = {}
        self.samples: dict[str, dict[str, list[str]]] = {}  # sheet -> field -> [vals]
        # Data-dictionary enrichment (populated when the template ships a spec
        # sheet documenting each column): meaning, allowed values, type, required.
        self.field_descriptions: dict[str, str] = {}
        self.field_allowed: dict[str, list[str]] = {}
        self.field_format: dict[str, str] = {}
        self.field_required: dict[str, str] = {}
        # normalized name -> canonical name (None when ambiguous: two distinct
        # fields normalize to the same key, so we refuse to auto-resolve it).
        self._norm_index: dict[str, str | None] = {}

        for f in self.template_fields:
            name = (f.get("name") or "").strip()
            if not name:
                continue
            sheet = (f.get("sheet") or "").strip() or "Sheet1"
            self.field_names.add(name)
            # First sheet wins for the (legacy) single-sheet map …
            self.field_to_sheet.setdefault(name, sheet)
            # … but record ALL sheets carrying the column so a rule fans out to
            # every sheet that has it (multi-sheet templates repeat columns).
            self.field_to_sheets.setdefault(name, [])
            if sheet not in self.field_to_sheets[name]:
                self.field_to_sheets[name].append(sheet)
            self.fields_by_sheet.setdefault(sheet, [])
            if name not in self.fields_by_sheet[sheet]:
                self.fields_by_sheet[sheet].append(name)
            sm = [str(s) for s in (f.get("samples") or []) if s is not None][:3]
            if sm:
                self.samples.setdefault(sheet, {})[name] = sm

            # Dictionary enrichment — keep the first non-empty value seen for a
            # field (a column repeats across sheets with the same definition).
            desc = f.get("description")
            if desc and name not in self.field_descriptions:
                self.field_descriptions[name] = str(desc)
            allowed = [str(v) for v in (f.get("allowed_values") or []) if v not in (None, "")]
            if allowed and name not in self.field_allowed:
                self.field_allowed[name] = allowed
            fmt = f.get("field_format")
            if fmt and name not in self.field_format:
                self.field_format[name] = str(fmt)
            req = f.get("required")
            if req and name not in self.field_required:
                self.field_required[name] = str(req)

            nk = _norm_field(name)
            if nk in self._norm_index and self._norm_index[nk] != name:
                self._norm_index[nk] = None     # ambiguous — don't auto-resolve
            else:
                self._norm_index.setdefault(nk, name)

        # The sheet the most fields map to — used as the fallback table when a
        # rule references a column the template doesn't have, so the rule still
        # compiles to SQL that targets the real data sheet (the runtime then
        # checks whether that column is actually present before running it).
        from collections import Counter
        self.primary_sheet = (
            Counter(self.field_to_sheet.values()).most_common(1)[0][0]
            if self.field_to_sheet else None
        )

    def is_empty(self) -> bool:
        return not self.field_names

    def samples_for(self, field: str) -> list[str]:
        """Sample values collected for a field (across sheets), used by the
        verify gate's type-compatibility check."""
        for fmap in self.samples.values():
            if field in fmap:
                return fmap[field]
        return []

    def resolve_field(self, name) -> str | None:
        """Map a model-emitted field name to the EXACT canonical template field
        name, tolerating case/spacing/punctuation differences. Returns None if it
        can't be resolved (genuinely not in the template, or ambiguous)."""
        if not isinstance(name, str) or not name:
            return None
        if name in self.field_names:        # exact match — fast path
            return name
        return self._norm_index.get(_norm_field(name))

    def allowed_values_for(self, field: str) -> list[str]:
        """Documented allowed values (codes) for a field, or [] if not specified
        by a dictionary sheet."""
        return self.field_allowed.get(field, [])

    def description_for(self, field: str) -> str | None:
        return self.field_descriptions.get(field)

    # -- Stage-B prompt block ------------------------------------------------
    def prompt_field_block(self) -> str:
        """Human/LLM-readable list of canonical fields grouped by sheet. Each field
        shows, where known, its DICTIONARY MEANING and ALLOWED VALUES (from a spec
        sheet) plus a couple of sample values — so the extractor picks the right
        real field by MEANING, not just a lexical name match, and knows the valid
        value set."""
        lines = []
        for sheet, fields in self.fields_by_sheet.items():
            lines.append(f'Sheet "{sheet}":')
            for name in fields:
                parts = [f'  - "{name}"']
                desc = self.field_descriptions.get(name)
                if desc:
                    one_line = " ".join(str(desc).split())
                    parts.append(f"— {one_line[:120]}")
                allowed = self.field_allowed.get(name)
                if allowed:
                    parts.append(f"[allowed: {', '.join(allowed[:12])}]")
                else:
                    ex = (self.samples.get(sheet) or {}).get(name)
                    if ex:
                        parts.append(f"e.g. {ex}")
                lines.append(" ".join(parts))
        return "\n".join(lines)

    # -- Verify-gate smoke test schema --------------------------------------
    def sheets_for_smoke(self) -> dict[str, list[str]]:
        """{sheet: [columns]} for building a DuckDB smoke-test connection."""
        return {s: list(cols) for s, cols in self.fields_by_sheet.items()}

    def sample_records(self) -> list[dict]:
        """records_by_sheet shape for duckdb_validation.build_connection:
        [{"sheet": name, "records": [ {col: val, ...}, ... ]}]. Built from the
        per-field sample values (zip aligned, padded) so smoke tests run against
        realistically-shaped rows."""
        out = []
        for sheet, cols in self.fields_by_sheet.items():
            col_samples = self.samples.get(sheet) or {}
            depth = max((len(col_samples.get(c, [])) for c in cols), default=0)
            records = []
            for i in range(depth):
                rec = {}
                for c in cols:
                    vals = col_samples.get(c) or []
                    rec[c] = vals[i] if i < len(vals) else None
                records.append(rec)
            out.append({"sheet": sheet, "records": records})
        return out


def build_output_schema(template_fields: list[dict] | None) -> OutputSchema:
    return OutputSchema(template_fields)
