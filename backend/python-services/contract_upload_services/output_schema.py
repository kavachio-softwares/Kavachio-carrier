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

def is_processing_date_column(name) -> bool:
    """True when a column NAME reports WHEN A TRANSACTION WAS RECORDED (booked /
    keyed / processed) rather than when it TOOK EFFECT on the risk.

    Matched by ROLE tokens, never by hard-coded column names: the column carries
    the transaction-date role ('transaction' + 'date') WITHOUT the
    effective/expiry qualifier that would make it a date on the risk. So
    "Policy Transaction Date" / "TRANSACTION_DATE" are processing dates, while
    "Transaction Effective Date" / "Transaction Expiration Date" are not.

    This is the test validation_rule_generator.fix_backdating_period_fields has
    always used to LOCATE that column (backdating is measured inception →
    processing date). It lives here, as one definition, so the code that WANTS
    the processing date and the code that must keep it OUT of an in-period date
    bound can never drift apart: a booked/keyed date legitimately falls outside
    the policy period (a cancellation or audit is recorded after expiry, a
    renewal is keyed before inception), so bounding it by the period flags
    ordinary bookkeeping.
    """
    ln = str(name or "").lower()
    return ("transaction" in ln and "date" in ln
            and "effective" not in ln and "expir" not in ln)


class OutputSchema:
    def __init__(self, template_fields: list[dict] | None):
        self.template_fields = template_fields or []
        self.field_names: set[str] = set()
        self.field_to_sheet: dict[str, str] = {}
        self.field_to_sheets: dict[str, list[str]] = {}   # field -> EVERY sheet it lives on
        self.fields_by_sheet: dict[str, list[str]] = {}
        self.samples: dict[str, dict[str, list[str]]] = {}  # sheet -> field -> [vals]
        # Same shape, NOT capped at 3 — used only by the deterministic verify-gate
        # arithmetic (see samples_for_grounding), never by a prompt.
        self.samples_all: dict[str, dict[str, list[str]]] = {}
        # Same shape again, but ROW-ALIGNED: position i is the same row of the
        # sheet in every column, blanks included, so several columns can be read
        # together as rows. The only shape an arithmetic identity can be judged
        # on; empty for a template parsed before they were captured.
        self.row_samples: dict[str, dict[str, list[str]]] = {}
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
            # Untruncated samples, for DETERMINISTIC arithmetic checks only (never
            # for a prompt or the smoke-test rows, which stay at 3). More rows is
            # strictly more evidence: two candidate columns can agree over the
            # first few rows and diverge later.
            sa = [str(s) for s in (f.get("samples_all") or f.get("samples") or [])
                  if s is not None]
            if sa:
                self.samples_all.setdefault(sheet, {})[name] = sa
            rs = [str(s) if s is not None else "" for s in (f.get("row_samples") or [])]
            if rs:
                self.row_samples.setdefault(sheet, {})[name] = rs

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

        # ── Cross-sheet column ALIASES ─────────────────────────────────────
        # Multi-sheet templates repeat the same logical column, but not always
        # under the same header — "POL_NO" on two schedules and "Policy number"
        # on a third. Rules bind ONE spelling, and the compiler fans a rule out
        # by exact name, so the third sheet silently escaped validation.
        #
        # Group columns by CONCEPT — the canonical_field the template mapper
        # assigned, else the normalized header (case/punctuation differences) —
        # and record, for each field name, the sheets where the same concept
        # lives under a DIFFERENT header and what it is called there:
        #     field_aliases["POL_NO"] == {"Sheet3": "Policy number"}
        # compile_ir uses this to fan the rule out to those sheets too, with
        # each arm's SQL written in that sheet's own spelling.
        #
        # An alias makes a rule RUN against a column it was never written for, so
        # a wrong one is not a missed check — it is a false exception on innocent
        # data. Two guards keep the concept key honest:
        #
        # 1. AMBIGUOUS CONCEPT. A canonical_field that names MORE than one column
        #    on ANY one sheet is not identifying a column, it is a bucket — real
        #    templates park 15 unrelated claim amounts under `commission_amount`
        #    and every program column under `program_name`. The tag cannot
        #    establish identity ACROSS sheets when it fails to inside one, so the
        #    whole concept is dropped. (Skipping only the crowded sheet — the
        #    original guard — still trusted the same tag everywhere else, which is
        #    how `total_paid` came to be validated as `Program ID`.)
        #
        # 2. VALUE KIND. The same logical column holds the same kind of value on
        #    every sheet. A money column aliased to a text code, or a date aliased
        #    to a number, is a mis-tag whatever the header says — and it is
        #    exactly the alias that makes a numeric rule fire "must be a number"
        #    at a perfectly valid code. Kind is inferred from the column's own
        #    sample data (infer_value_kind), never from a list of names.
        #
        # Both are deliberately conservative in the same direction: when the
        # evidence for "same column" is weak the rule simply does not fan out to
        # that sheet, which is the behaviour before aliases existed at all.
        _c_groups: dict[str, dict[str, set]] = {}
        _name_pool: list[tuple[str, str]] = []   # (sheet, name)
        for f in self.template_fields:
            name = (f.get("name") or "").strip()
            if not name:
                continue
            sheet = (f.get("sheet") or "").strip() or "Sheet1"
            canon = (f.get("canonical_field") or "").strip().lower()
            if canon:
                _c_groups.setdefault(canon, {}).setdefault(sheet, set()).add(name)
            else:
                _name_pool.append((sheet, name))

        # Guard 1 (bucket tag) applies to the CANONICAL grouping — but a bucket
        # tag must not also erase the NAME evidence. When a canonical names 2+
        # columns on any one sheet (a real template parked 30 unrelated loss
        # amounts AND 'Feed ID' / 'Risk No' under `policy_number`), its members
        # fall back to NAME grouping below: 'Policy Number' / 'Policy number'
        # still identify one concept by their normalized header, while the
        # mis-tagged strangers group separately under their own names. Spellings
        # the normalization can't connect ("Policy No", "Assured Reference")
        # carry NO hardcoded vocabulary here — they are joined by the two
        # semantic layers instead: shared identifier values (below) and the
        # mapping LLM's per-field "field_aliases" (merged in rule_normalizer).
        _by_key: dict[str, dict[str, set]] = {}
        for canon, per_sheet in _c_groups.items():
            if any(len(ns) > 1 for ns in per_sheet.values()):
                for sh, ns in per_sheet.items():
                    for n in ns:
                        _name_pool.append((sh, n))
            else:
                _by_key[f"c:{canon}"] = per_sheet
        for sheet, name in _name_pool:
            _by_key.setdefault(f"n:{_norm_field(name)}", {}).setdefault(sheet, set()).add(name)

        # ── Third evidence layer: SHARED IDENTIFIER VALUES ────────────────
        # Canonical tags and header names both fail when a sheet spells a
        # concept completely differently ("Account Reference #" holding policy
        # numbers). But the DATA itself can prove identity: the same id-shaped
        # value ("KMDF03000014-23") sampled in two columns on DIFFERENT sheets
        # means they hold the same thing, whatever they are named. Only values
        # with the identifier signature qualify — ≥6 chars mixing letters AND
        # digits — so states, dates, amounts and yes/no flags can never create
        # an edge. Concept groups sharing a qualifying value are merged; a
        # merge is refused when the combined group would trip guard 1 (two of
        # its columns on one sheet), keeping the bucket protection intact.
        def _idlike(v) -> bool:
            s = str(v).strip()
            return (len(s) >= 6 and any(ch.isalpha() for ch in s)
                    and any(ch.isdigit() for ch in s))

        _val_to_keys: dict[str, set] = {}
        for key, per_sheet in _by_key.items():
            for sh, ns in per_sheet.items():
                for n in ns:
                    for v in (self.samples_all.get(sh, {}).get(n)
                              or self.samples.get(sh, {}).get(n) or []):
                        if _idlike(v):
                            _val_to_keys.setdefault(str(v).strip().lower(), set()).add(key)

        parent = {k: k for k in _by_key}
        def _find(k):
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k
        for keys in _val_to_keys.values():
            if len(keys) < 2:
                continue
            keys = list(keys)
            for other in keys[1:]:
                ra, rb = _find(keys[0]), _find(other)
                if ra != rb:
                    parent[rb] = ra

        from collections import defaultdict as _dd
        comps: dict = _dd(list)
        for k in _by_key:
            comps[_find(k)].append(k)
        merged_by_key: dict[str, dict[str, set]] = {}
        for root, members in comps.items():
            if len(members) == 1:
                merged_by_key[members[0]] = _by_key[members[0]]
                continue
            combined: dict[str, set] = {}
            for m in members:
                for sh, ns in _by_key[m].items():
                    combined.setdefault(sh, set()).update(ns)
            if any(len(ns) > 1 for ns in combined.values()):
                # merged group would trip guard 1 — keep the originals apart
                for m in members:
                    merged_by_key[m] = _by_key[m]
            else:
                merged_by_key[f"v:{root}"] = combined
        _by_key = merged_by_key

        self.field_aliases: dict[str, dict[str, str]] = {}
        for key, per_sheet in _by_key.items():
            all_names = {n for ns in per_sheet.values() for n in ns}
            if len(all_names) < 2:
                continue    # one spelling everywhere — field_to_sheets covers it
            if any(len(ns) > 1 for ns in per_sheet.values()):
                continue    # guard 1 — bucket tag, not a column identity
            for name in all_names:
                home = next((s for s, ns in per_sheet.items() if name in ns), None)
                for sh, ns in per_sheet.items():
                    if name in ns:
                        continue    # sheet already has this spelling
                    other = next(iter(ns))
                    if not self._same_value_kind(home, name, sh, other):
                        continue    # guard 2 — different kind of value, not the same column
                    self.field_aliases.setdefault(name, {})[sh] = other

        # The sheet the most fields map to — used as the fallback table when a
        # rule references a column the template doesn't have, so the rule still
        # compiles to SQL that targets the real data sheet (the runtime then
        # checks whether that column is actually present before running it).
        from collections import Counter
        self.primary_sheet = (
            Counter(self.field_to_sheet.values()).most_common(1)[0][0]
            if self.field_to_sheet else None
        )

    # Kinds infer_value_kind reports, folded into the three FAMILIES that decide
    # whether two columns can be the same one. The finer split (money vs fraction
    # vs percentage vs number) reflects the SAMPLE SCALE on a given sheet, and the
    # same column legitimately looks different sheet to sheet — one schedule's
    # rows all under 1, another's in the millions. The family does not: a column
    # holding amounts never holds codes on another sheet.
    _KIND_FAMILY = {"date": "date", "money": "number", "fraction (0–1)": "number",
                    "percentage (0–100)": "number", "number": "number",
                    "text/code": "text"}

    def _value_kind(self, sheet, field: str):
        """The value-kind FAMILY ('date' | 'number' | 'text') of one column, from
        its own sample values — or None when there is no evidence to judge on.

        `infer_value_kind` falls back to the column NAME when it has no data, and
        a name alone cannot tell an amount from a code, so a column with no
        captured samples reports None rather than a guess."""
        samples = ((self.samples_all.get(sheet) or {}).get(field)
                   or (self.samples.get(sheet) or {}).get(field) or [])
        # A blank says nothing about the kind of value a column holds, and a
        # column of nothing but blanks is a column with no evidence — exactly as
        # one with no samples at all.
        samples = [s for s in samples if str(s).strip()]
        if not samples:
            return None
        try:
            from contract_upload_services.prompt_builder import infer_value_kind
        except Exception:
            return None
        return self._KIND_FAMILY.get(infer_value_kind(field, samples), "text")

    def _same_value_kind(self, a_sheet, a_field, b_sheet, b_field) -> bool:
        """True unless the two columns are KNOWN to hold different kinds of value.

        Fails OPEN on purpose: only a positive, sample-backed disagreement blocks
        an alias, so a template that captured no sample data keeps exactly the
        fan-out it had rather than silently losing every alias it depends on."""
        a = self._value_kind(a_sheet, a_field)
        b = self._value_kind(b_sheet, b_field)
        return a is None or b is None or a == b

    def is_empty(self) -> bool:
        return not self.field_names

    def samples_for(self, field: str) -> list[str]:
        """Sample values collected for a field (across sheets), used by the
        verify gate's type-compatibility check."""
        for fmap in self.samples.values():
            if field in fmap:
                return fmap[field]
        return []

    def samples_for_grounding(self, field: str) -> list[str]:
        """Every sample captured for a field — for the verify gate's deterministic
        arithmetic checks (e.g. which candidate column a "share = base × rate"
        identity actually holds against).

        Prefers the ROW-ALIGNED rows: those checks read two or three columns
        together and compare them row by row, and the aligned rows are both the
        only set on which that comparison means anything and the set chosen to
        tell near-identical columns apart. Falls back to the untruncated head
        samples — for a template parsed before the aligned rows were captured, and
        for a column that happens to be EMPTY on all of them (a sparsely-populated
        column, whose head samples are still real values worth reading) — then to
        the 3-value prompt list when a caller built the schema without either."""
        for fmap in self.row_samples.values():
            if any(str(v).strip() for v in (fmap.get(field) or [])):
                return fmap[field]
        for fmap in self.samples_all.values():
            if field in fmap:
                return fmap[field]
        return self.samples_for(field)

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
