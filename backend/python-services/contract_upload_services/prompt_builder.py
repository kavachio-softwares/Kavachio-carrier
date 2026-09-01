"""
prompt_builder.py
─────────────────
Builds the four LLM prompts used by Kavachio's contract pipeline,
per Kavachio_Pipeline_Architecture.png + kavachio-contract-validations-2.docx:

  Pipeline 1
    └─ 1.3 Structured extraction (per section)
          build_extraction_prompt(...)            ← LLM call #1

  Pipeline 2
    └─ 2.2 Stage A: Classification (batched)
          build_classification_prompt_batch(...)      ← LLM call #2
    └─ 2.4-A Stage B: AJV synthesis (template-aware, 1 batched call)
          build_ajv_synthesis_prompt_for_template_batch(...)    ← LLM call #3
    └─ 2.4-B Stage B: Custom synthesis (template-aware, 1 batched call)
          build_custom_synthesis_prompt_for_template_batch(...) ← LLM call #4

Rules target Output Template field names (Contract → Output Template Fields),
not the internal data model — validation runs on the rendered output.

Also exposes helpers used by Pipeline 1.1 / 1.2:
  - build_llm_context(pdf_data): flattens pages into one annotated string
  - split_pages_into_chunks(pdf_data): fallback fixed-page chunker
  - split_into_sections(pdf_data): header-regex based section detector
"""

import re
import os
import json
import datetime

from contract_upload_services.constants import (
    SECTION_HEADER_PATTERNS,
)


def _dump_mapping_output_template(unique_fields, fields_block):
    """Debug log: write the EXACT output-template field list (name, sheets,
    dictionary description, allowed values, format, required, samples) that is
    sent to the LLM for Call-3 mapping, as JSON, so it can be inspected instead of
    printed. Writes to KAVACHIO_MAPPING_DEBUG_FILE (default
    'llm_mapping_output_template.json' in the working dir). Best-effort: never
    breaks rule generation."""
    path = os.getenv("KAVACHIO_MAPPING_DEBUG_FILE",
                     "llm_mapping_output_template.json")
    try:
        data = {
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "field_count": len(unique_fields),
            "fields": [
                {
                    "name": f.get("name"),
                    "sheets": f.get("sheets") or ([f.get("sheet")] if f.get("sheet") else []),
                    "description": f.get("description"),
                    "allowed_values": f.get("allowed_values") or [],
                    "field_format": f.get("field_format"),
                    "required": f.get("required"),
                    "samples": f.get("samples") or [],
                }
                for f in unique_fields
            ],
            "rendered_block": fields_block,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
    except Exception:
        pass


# =========================================================
# PIPELINE 1.1 — Pre-processing helpers
# =========================================================

def build_llm_context(pdf_data):
    """Flatten ALL pages into ONE CONTINUOUS text block (sent to the LLM in a
    single call) — NO page markers and NO banner separators at all. The whole
    contract flows as plain continuous text so nothing splits the contract or its
    clauses. Page numbers are NOT carried in the text; they are reconciled
    afterwards by matching each extracted clause back to the source pages
    (see ValidationRuleGenerator._assign_clause_pages)."""

    pages = pdf_data.get("pages", [])

    parts = []

    for p in pages:

        text = p.get("text", "")

        cleaned = "\n".join(
            line.strip()
            for line in text.splitlines()
            if line.strip()
        )

        if cleaned:
            parts.append(cleaned)

        # Any TABLE detected on the page is ALSO appended as a clean grid. The
        # flattened text above scrambles a table's columns; the grid gives the LLM
        # exact rows/columns so each row (e.g. one approved reinsurer + its limit)
        # is unambiguous. Marked so the prompt can treat rows as self-contained.
        for tbl in (p.get("tables") or []):
            grid = _render_table_markdown(tbl)
            if grid:
                parts.append(f"[TABLE]\n{grid}\n[/TABLE]")

    return "\n".join(parts)


def _render_table_markdown(rows: list[list[str]]) -> str:
    """Render a detected table (list of cell-rows) as a markdown-style grid: the
    first non-empty row is the header, the rest are data rows. Pipes inside cells
    are neutralized so the grid stays parseable."""
    def _cells(row):
        return [str(c or "").replace("|", "/").replace("\n", " ").strip() for c in row]
    body = [r for r in rows if any(_cells(r))]
    if len(body) < 2:
        return ""
    width = max(len(r) for r in body)
    lines = []
    for i, r in enumerate(body):
        cells = _cells(r)
        cells += [""] * (width - len(cells))
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:                              # header separator
            lines.append("| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


def split_pages_into_chunks(pdf_data, pages_per_chunk=5):
    """Simple fixed-size page chunker (fallback when section headers fail)."""

    pages = pdf_data.get("pages", [])

    chunks = []

    for i in range(0, len(pages), pages_per_chunk):

        chunk_pages = pages[i:i + pages_per_chunk]

        chunk_text = "".join(
            f"\n\n--- PAGE {page['page']} ---\n\n{page['text']}"
            for page in chunk_pages
        )

        chunks.append({
            "section_type": "fixed_chunk",
            "section_header": None,
            "page_start": chunk_pages[0]["page"],
            "page_end": chunk_pages[-1]["page"],
            "text": chunk_text
        })

    return chunks


# =========================================================
# PIPELINE 1.2 — Section identification (header-regex)
# =========================================================

_HEADER_REGEXES = [re.compile(p) for p in SECTION_HEADER_PATTERNS]


def _looks_like_header(line):
    """Return the matched header text if the line is a section header."""

    stripped = line.strip()

    if not stripped or len(stripped) > 120:
        return None

    for rx in _HEADER_REGEXES:
        if rx.match(stripped):
            return stripped

    return None


def split_into_sections(pdf_data, min_section_pages=1, fallback_chunk=5):
    """
    Walk page text, detect section headers, group consecutive lines into
    sections until the next header. Falls back to fixed page chunks
    when no headers are detected.

    Returns: list of {section_type, section_header, page_start, page_end, text}
    """

    pages = pdf_data.get("pages", [])

    if not pages:
        return []

    sections = []
    current = {
        "section_type": "header",
        "section_header": None,
        "page_start": pages[0]["page"],
        "page_end": pages[0]["page"],
        "lines": []
    }

    for page in pages:

        page_no = page["page"]
        text = page.get("text", "") or ""

        for line in text.splitlines():

            header = _looks_like_header(line)

            if header:

                if current["lines"]:
                    sections.append(current)

                current = {
                    "section_type": _classify_section(header),
                    "section_header": header,
                    "page_start": page_no,
                    "page_end": page_no,
                    "lines": []
                }

            else:
                current["lines"].append((page_no, line))
                current["page_end"] = page_no

    if current["lines"]:
        sections.append(current)

    # Fallback: no headers found at all → fixed chunks
    if len(sections) <= 1 and not any(s["section_header"] for s in sections):
        return split_pages_into_chunks(pdf_data, fallback_chunk)

    # Build section text with page markers
    result = []

    for s in sections:

        if len(s["lines"]) < 1:
            continue

        last_page = None
        buf = []

        for page_no, line in s["lines"]:

            if page_no != last_page:
                buf.append(f"\n\n--- PAGE {page_no} ---\n\n")
                last_page = page_no

            buf.append(line + "\n")

        result.append({
            "section_type": s["section_type"],
            "section_header": s["section_header"],
            "page_start": s["page_start"],
            "page_end": s["page_end"],
            "text": "".join(buf).strip()
        })

    return result


def _classify_section(header):
    """Heuristic section_type label from header text."""

    up = header.upper()

    if any(w in up for w in ["RECITAL", "PARTIES", "BETWEEN", "AGREEMENT"]):
        return "header"

    if any(w in up for w in ["COMMISSION", "FEE", "BROKER", "PREMIUM"]):
        return "commercial_terms"

    if any(w in up for w in ["TERRITORY", "EXCLUSION", "LIMIT", "AGGREGATE"]):
        return "clauses"

    return "body"


# =========================================================
# PIPELINE 1.3 — Structured extraction prompt (LLM call #1)
# =========================================================

_EXTRACTION_SYSTEM = """You are an insurance contract extraction assistant for Kavachio.

Your job is to read a US commercial insurance contract section and produce
structured JSON output for that section only.

CRITICAL RULES:
1. Never invent values. If a field is not present in the text, output null
   with confidence: 0.
2. Always include 'source_text' with the exact quoted phrase supporting
   each extraction.
3. The document is ONE continuous block of text with NO page markers. Do NOT
   split a clause for any reason — keep each clause whole even if it is long.
   Include 'page' as a best-effort 1-indexed estimate (or 0 if unsure); the system
   reconciles the true page automatically by matching the clause text back to the
   source, so accuracy here is not critical.
4. Always include 'confidence' between 0.0 and 1.0.
5. If the same field appears multiple times, extract each occurrence —
   we deduplicate downstream.
6. Use exact terms from the contract; do not paraphrase commercial
   structures.
7. For dates, output ISO 8601 (YYYY-MM-DD).
8. For percentages, output decimals (a value P% becomes P/100, not the literal "P%").
9. For currencies, separate the ISO code:
       {"amount": 100000, "currency": "USD"}.
11. CLAUSE COMPLETENESS: Emit a separate entry in "clauses" for EVERY constraint
    in the document, of ANY kind, however short — including every sub-bullet or
    enumerated sub-item of a list. Do NOT skip a constraint because of its category,
    or because it reads like a heading: if it restricts, requires, limits, excludes,
    caps, or fixes a value that a bordereau row could report, it is a clause.
    Assign the closest `clause_type` from the schema enum; when unsure use "other".
    - A constraint also captured in program_metadata MUST ALSO be emitted as a
      clause — program_metadata alone never becomes a rule.
    - PROGRAM / SCHEDULE PERIOD BOUNDARY DATES ARE CONSTRAINTS, NOT JUST METADATA.
      The program's or schedule's own INCEPTION / EFFECTIVE / COMMENCEMENT date and
      its EXPIRATION / TERMINATION / END date (e.g. "Program Schedule Inception
      Date: <date>", "Effective Date: <date>", "this Schedule expires <date>")
      bound the window in which policies may attach — every policy's period must sit
      inside the program period. Capturing the date only in
      program_metadata.inception_date / expiry_date is NOT enough (metadata never
      becomes a rule): ALSO emit each stated boundary date as its OWN clause
      (clause_intent "validation_constraint", closest clause_type e.g. "other"),
      quoting the date verbatim, so a per-policy date rule can be built. Emit one
      clause per stated boundary date. Do NOT drop such a date as a mere header just
      because it sits among program-identity lines (name, writing companies).
    - A COMMERCIAL TERM that fixes a checkable value (a rate, a fee cap / floor /
      formula, a minimum or deposit) MUST ALSO be emitted as a clause, in ADDITION
      to its commercial_terms entry — a commercial_terms entry never becomes a
      rule, so without the clause the check is lost. Emit the EXACT constraint:
      an equality for a fixed required value, a cap for a maximum, a floor for a
      minimum.
    - Each maximum / sublimit and each enumerated sub-item is its OWN clause, but
      each MUST be SELF-CONTAINED (see SELF-CONTAINED ENUMERATED ITEMS below).
    KEEP ONE DIMENSION TOGETHER — DO NOT OVER-SPLIT. A constraint that scopes a
    SINGLE field/dimension by stating what IS allowed AND what is carved out (an
    inclusion together with its paired exception/exclusion) is ONE clause, not two:
    emit a single entry whose `text` quotes BOTH the permitted and the excluded
    values verbatim — never separate the inclusion from its own exclusion. Split
    into separate clauses ONLY when the constraints concern DIFFERENT
    fields/dimensions — then one clause per distinct constraint.

    SELF-CONTAINED ENUMERATED ITEMS — when an enumerated/numbered or bulleted
    list (e.g. "... as follows: 7. ... 8. ... 9. ...") is split into one clause
    per item, EACH item's clause MUST stand on its own. Build each item's `text`
    as:  <the governing lead-in / preamble that states the CONSEQUENCE or
          obligation>  +  <the specific item, verbatim>  +  <any shared trailing
          obligation that applies to the whole list>.
    Do NOT emit the bare item alone (it loses its meaning), and do NOT emit the
    lead-in/preamble as its OWN separate clause with the items stripped off.
    Put the list's lead-in heading in `section_header`.
    EXAMPLE — a section reading:
      "<lead-in stating the consequence> ... and any of the following ... as follows:
         <item 1>
         <item 2> ...
       <shared trailing obligation that applies to the whole list>."
    → ONE clause PER item. EACH clause's `text` quotes the lead-in preamble AND
      its own item AND the shared trailing obligation. NEVER a standalone clause
      that keeps only the lead-in with the items removed.

    TABLES — content provided between [TABLE] and [/TABLE] markers is STRUCTURED
    grid data pulled from the contract; its FIRST row is the column HEADER. Emit the
    WHOLE table as ONE single clause — do NOT split it into one clause per row. That
    clause's `text` MUST reproduce the ENTIRE grid AS A GitHub-Flavored Markdown
    table — copy the [TABLE] block VERBATIM: keep the leading and trailing `|` on
    every row and the `| --- | --- |` header-separator row EXACTLY as provided, the
    header row AND every data row, each row on its OWN line, cells in column order,
    so the complete table is preserved in one place AND can be re-rendered as a
    table downstream. Do NOT drop the pipes or reword the grid into prose. Skip only
    blank / header-repeat / total-subtotal rows; keep every real data row (do NOT
    summarise, truncate, or drop rows). Put the table's title / lead-in sentence in
    `section_header`, and set `clause_type` from what the table expresses (an
    approved / allowed LIST → "limit"; a prohibited / excluded LIST → "exclusion";
    a per-row constraint grid → "limit"). The grid RESTATES data that ALSO appears —
    scrambled — in the surrounding prose; use the GRID once and do NOT also emit a
    clause for those same rows out of the prose. (The downstream rule step decides
    whether the table becomes one list rule or a per-row set of rules — your job
    here is ONE faithful, complete clause for the whole table.)

    WITHIN-DOCUMENT CROSS-REFERENCES — when a clause's meaning depends on ANOTHER
    part of THIS SAME document (e.g. "exceeding the authorities granted in
    <another section>", "as defined in <another section>"), preserve the meaning,
    but CONTEXTUALLY — do NOT blindly merge whole sections:
      - Keep `text` focused on the OPERATIVE constraint of THIS clause.
      - Quote ONLY the SPECIFIC referenced sentence(s) you relied on (NOT the
        entire referenced section) and append them, clearly marked, e.g.
        "[Context — <section>: <quoted text>]".
      - Resolve ONE level only — never follow references of references, and never
        duplicate a whole section into many clauses.
    (External / "on file" documents are handled separately under EXTERNAL
    DOCUMENT REFERENCES below — this rule is ONLY for references WITHIN this
    contract.)

    CLAUSE HIERARCHY & INTENT — tag every clause so downstream can preserve
    structure and route it correctly:
      - `local_id`: a unique integer you assign to each clause (1, 2, 3, …) in
        the order you emit them.
      - `parent_local_id`: when a clause is a CHILD of another — an inner sublimit
        under a parent "Maximum Limits" heading, or an enumerated item under its
        list lead-in — set this to the parent clause's `local_id`. Top-level
        clauses use null. Link the IMMEDIATE parent only, and still keep each child
        SELF-CONTAINED per the rule above.
      - `clause_intent`: the clause's purpose —
          "validation_constraint" : restricts a value a BDX column reports.
          "referral_trigger"      : a condition that requires REFERRAL / approval
                                     to the Company.
          "grant_of_authority"    : grants capacity / authority — NOT a BDX check.
          "operational_process"   : pure conduct / process (who signs, deadlines).
          "informational"         : definitions, framing, recitals.
10. EXTERNAL DOCUMENT REFERENCES: When clauses defer their actual content to a
    SEPARATE/external document (e.g. "refer to <a named guidelines document> on
    file", "per <a named document with a date/version> on file with the Company"),
    list each such document ONCE in "external_references" — not one entry per mention.
    - Use its MOST COMPLETE name as written (if the contract uses both a full name
      and an abbreviation for the same document, use the full name and treat them
      as the SAME document).
    - Collect EVERY supporting sentence into "source_texts" and their page numbers
      into "pages".
    - Merge entries that refer to the same document (same name ignoring case, or
      one name contained in the other, with the same date/version).
    Never invent a name. If none exist, return an empty array.
11. RESTRICTIONS ARE ALSO CLAUSES: Only entries in "clauses" become validation
    rules — program_metadata fields never do. So whenever the contract RESTRICTS
    what may be written — any "allowed / eligible / permitted" set, or any
    "excluding / not eligible / not permitted / prohibited / ineligible"
    condition, on ANY attribute — ALSO emit it as a rule-bearing entry in "clauses"
    (in addition to any metadata field), choosing the closest clause_type. Quote
    the permitted/excluded values verbatim in the clause text so a concrete check
    can be built downstream.
    AUTHORIZED PARTIES/PAPER ARE A RESTRICTION: a clause that NAMES the approved
    carrier(s) / writing company(ies) / issuing company / paper a policy may be
    written on — e.g. "Writing Companies: <A> or <B> or <C>", "Issuing Carrier:
    <X>", "Approved Paper: <Y>" — is an ALLOWED-SET restriction on the carrier/
    company field. ALWAYS emit it as a rule-bearing clause (never treat it as pure
    metadata/informational), quoting the full list of named companies verbatim, so
    a value_in_set check on the carrier/writing-company column can be built. This
    holds even when the same companies are also mentioned in other clauses
    (referrals, paper requirements) — the authorized LIST itself must still be its
    own rule-bearing clause.

OUTPUT SCHEMA:
{
  "program_metadata": {
    "program_name":         {"value": string|null, "source_text": string, "page": number, "confidence": number},
    "document_type":        {"value": "Program Schedule A"|"Bordereaux"|"Policy"|"Endorsement"|string|null, "source_text": string, "page": number, "confidence": number},
    "carrier_name":         {"value": string|null, "source_text": string, "page": number, "confidence": number},
    "admin_party_name":     {"value": string|null, "source_text": string, "page": number, "confidence": number},
    "bdx_frequency":        {"value": "monthly"|"quarterly"|"weekly"|"annual"|null, "source_text": string, "page": number, "confidence": number},
    "business_segment":     {"value": string|null, "source_text": string, "page": number, "confidence": number},
    "product_line":         {"value": string|null, "source_text": string, "page": number, "confidence": number},
    "distribution_channel": {"value": string|null, "source_text": string, "page": number, "confidence": number},
    "territory":            {"value": [string]|null, "source_text": string, "page": number, "confidence": number},
    "inception_date":       {"value": "YYYY-MM-DD"|null, "source_text": string, "page": number, "confidence": number},
    "expiry_date":          {"value": "YYYY-MM-DD"|null, "source_text": string, "page": number, "confidence": number},
    "claims_basis":         {"value": "occurrence"|"claims_made"|null, "source_text": string, "page": number, "confidence": number},
    "currency":             {"value": string|null, "source_text": string, "page": number, "confidence": number}
  },
  "commercial_terms": [
    {
      "term_type": "commission"|"broker_fee"|"sliding_scale"|"profit_commission"|"deposit_premium"|"minimum_premium",
      "value": object,
      "source_text": string,
      "page": number,
      "confidence": number
    }
  ],
  "clauses": [
    {
      "local_id": number,
      "parent_local_id": number|null,
      "clause_type": "exclusion"|"limit"|"sublimit"|"mandatory_field"|"reporting_requirement"|"warranty"|"condition_precedent"|"aggregate_cap"|"other",
      "clause_intent": "validation_constraint"|"referral_trigger"|"grant_of_authority"|"operational_process"|"informational",
      "title": string,
      "text": string,
      "page": number,
      "section_header": string,
      "source_reference_document": string|null,
      "confidence": number
    }
  ],
  "external_references": [
    {
      "document_name":   string,
      "version_or_date": string|null,
      "source_texts":    [string],
      "pages":           [number],
      "confidence":      number
    }
  ]
}

Output ONLY valid JSON. No commentary, no markdown fences."""


def _reference_documents_block(reference_documents):
    """Render uploaded reference documents into a prompt block.

    `reference_documents` is a list of {"name": str, "text": str}. These are the
    external documents the contract defers to (e.g. underwriting guidelines).
    Returns "" when none are provided.
    """
    if not reference_documents:
        return ""

    provided_names = [
        (rd.get("name") or "(unnamed reference)") for rd in reference_documents
    ]

    parts = [
        "\n---BEGIN REFERENCE DOCUMENTS---",
        "The contract refers to the external document(s) below, now provided.",
        "When a contract clause defers its actual content to one of these (e.g.",
        "\"excluded classes per the Underwriting Guidelines\", \"targeted classes per",
        "the Purchasing Guidelines on file\", \"limits per the Fac Guidelines\"), the",
        "deferral is now RESOLVED — treat the matching reference document as the",
        "AUTHORITATIVE source and INLINE its concrete content into that clause so a",
        "check can be built from THIS clause alone. For EACH such deferring clause:",
        "  1. Find the section(s) of the matching reference document that supply the",
        "     values this clause is missing — the real allowed / eligible list, the",
        "     excluded / ineligible list, or the limit / threshold.",
        "  2. APPEND those concrete values to the clause's `text`, VERBATIM, as a",
        "     bracketed block in EXACTLY this form (keep the clause's own lead-in",
        "     BEFORE the block):",
        "        [Context from <document name> - <section heading>: <the actual",
        "         list / values, every item quoted — NOT a summary>]",
        "     e.g. an \"Excluded Classes: per the Guidelines on file\" clause becomes",
        "     \"g) Excluded Classes: Per the Guidelines on file. [Context from",
        "     <guide> - Excluded Occupancies: <group>: <item>; <item>; …]\".",
        "  3. Set that clause's \"source_reference_document\" to the document name.",
        "NEVER set \"source_reference_document\" WITHOUT also appending the [Context",
        "from …] block — a reference with no inlined values cannot be checked",
        "downstream and the rule is silently lost. Match a clause to a document by",
        "MEANING (the clause's subject + the cited document's name/topic), even when",
        "the contract's short name differs from the provided file name. This applies",
        "to allowed/eligible sets, excluded/ineligible sets, AND numeric",
        "limits/thresholds alike.",
        "",
        f"PROVIDED DOCUMENTS: {json.dumps(provided_names)}",
        "IMPORTANT — external_references: the documents listed in PROVIDED DOCUMENTS",
        "are now available, so DO NOT list them in \"external_references\". Only put a",
        "document in \"external_references\" if the contract refers to it AND it is NOT",
        "in the PROVIDED DOCUMENTS list above (i.e. it still needs to be uploaded).",
    ]
    for rd in reference_documents:
        name = rd.get("name") or "(unnamed reference)"
        text = rd.get("text") or ""
        parts.append(f'\n=== REFERENCE DOCUMENT: "{name}" ===\n{text}')
    parts.append("---END REFERENCE DOCUMENTS---\n")

    return "\n".join(parts)


def build_extraction_prompt(section, reference_documents=None):
    """
    Build the Pipeline 1.3 extraction prompt for one section.
    `section` is one element from split_into_sections().

    `reference_documents` (optional) is a list of {"name", "text"} for external
    documents the contract refers to; when present they are appended so the LLM
    can resolve deferred clauses against their real content.
    """

    section_type = section.get("section_type", "body")
    page_start   = section.get("page_start", 0)
    page_end     = section.get("page_end", 0)
    text         = section.get("text", "")

    references_block = _reference_documents_block(reference_documents)

    return f"""{_EXTRACTION_SYSTEM}

USER:
Contract section: {section_type}
Pages: {page_start} to {page_end}

---BEGIN SECTION TEXT---
{text}
---END SECTION TEXT---
{references_block}
Extract per schema."""


# =========================================================
# PIPELINE 2.2 — Stage A classification prompt (LLM call #2)
# =========================================================

_RULE_TYPES_LITERAL = (
    "required_field, range_check, value_list, pattern_check, format_check, "
    "date_relation, conditional_required, exclusion_single_row, "
    "mandatory_in_output, aggregate_limit, cross_field_math, uniqueness, "
    "sliding_scale, period_completeness, referential_check"
)

_CLASSIFICATION_SYSTEM = f"""You are a classification assistant for Kavachio's rule generation pipeline.

Given a batch of insurance contract clauses, decide for each one:
1. Is it rule-bearing? (produces a runnable BDX validation rule)
2. If yes, which engine should validate it at runtime?
3. What rule type(s) does it imply?

Engines:
- 'ajv'    — for row-level rules (one BDX row at a time):
             required fields, ranges, enums, patterns,
             conditional requirements, single-row exclusions,
             date relations within a row
- 'custom' — for cross-row or stateful rules:
             aggregate limits (sum, count, distinct), cross-field formulas,
             uniqueness, sliding scales, referential checks

Available rule types: {_RULE_TYPES_LITERAL}.

A clause IS rule-bearing if it:
- Restricts which risks may be written (limits, sublimits, exclusions)
- Requires specific data fields in BDX submissions
- Mandates reporting frequencies or formats
- Defines mathematical relationships (commissions, sliding scales, math)
- Triggers conditional behavior based on data values

A clause is NOT rule-bearing if it:
- Is a definition or framing
- Describes commercial relationship without operational data implications
- Is boilerplate, legal recital, governance language ,dispute-resolution, or administrative language
- Describes party identity or capacity
- References underwriting judgment without objective criteria that can be mapped to fields

OUTPUT (JSON only, no markdown, no explanation):
{{
  "results": [
    {{
      "clause_id": number,
      "is_rule_bearing": boolean,
      "reasoning": string,
      "engine": "ajv" | "custom" | null,
      "rule_types": [string],
      "rule_stage": "input" | "output" | "both" | null,
      "confidence": number
    }}
  ]
}}

If is_rule_bearing is false, set engine to null, rule_stage to null,
and leave rule_types as an empty array."""


def build_classification_prompt_batch(clauses):
    """
    Batched Stage A classifier prompt.
    `clauses` is a list of {clause_id, clause_type, title, text}.
    """

    payload = [
        {
            "clause_id": c.get("clause_id"),
            "clause_type": c.get("clause_type", "other"),
            "title": c.get("title", ""),
            "text": c.get("text", "")
        }
        for c in clauses
    ]

    return f"""{_CLASSIFICATION_SYSTEM}

USER:
Batch of {len(payload)} clauses:

{json.dumps(payload, indent=2)}

Classify each."""


# =========================================================
# TEMPLATE-AWARE PROMPTS (new architecture)
# Contract Fields → Output Template Fields (NOT → Data Model)
# =========================================================

def _coerce_number(value):
    """Parse a sample string into a float, stripping currency/percent/grouping
    punctuation. Returns None when the value isn't numeric (text/code/date-word)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    s = re.sub(r"[,$%\s]", "", s)
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def infer_value_kind(name: str, samples: list) -> str:
    """Deterministically classify a column's VALUE KIND from its name + sample
    values, so the mapper can pick a field whose kind matches the intent's value
    (a money cap → money column, a percentage → percent column, etc.).

    Returns one of: 'money', 'fraction (0–1)', 'percentage (0–100)', 'number',
    'date', 'text/code'. This is generic — it keys off symbols ($, %), numeric
    SCALE drawn from the samples, and the word 'date' in the name — never off any
    specific column name, so it stays correct for any template.
    """
    nm = (name or "").lower()
    nums = [n for n in (_coerce_number(s) for s in (samples or [])) if n is not None]
    has_pct = "%" in (name or "") or "percent" in nm or "pct" in nm
    has_money = "$" in (name or "") or any(w in nm for w in ("premium", "limit $", "amount", "fee", "usd"))

    # 'date' is reliably name-driven: BDX dates are often Excel serials (numbers),
    # so sample values can't be trusted to look like dates.
    if any(w in nm for w in ("date", " dt", "inception", "expiry", "expiration")):
        return "date"

    if nums:
        # Sample SCALE dominates the name: "100% policy Limit" holds millions, so
        # it is money despite the "%" in its label; "Part of Limit %" holds 0.2,
        # so it is a fraction. This is what disambiguates name-similar columns.
        mx = max(nums)
        if 0.0 <= mx <= 1.0 and any(n > 0 for n in nums):
            return "fraction (0–1)"
        if has_money or mx >= 1000:
            return "money"
        if has_pct and mx <= 100:
            return "percentage (0–100)"
        return "number"

    if has_pct:
        return "percentage (0–100)"
    if has_money:
        return "money"
    return "text/code"


def dedup_template_fields(template_fields: list[dict]) -> list[dict]:
    """Collapse a template_fields list (one entry per (sheet, column)) to ONE
    entry per column NAME. A BDX template repeats the same ~70 columns across
    every per-schedule sheet (70 × 12 = 800+ entries); listing every copy bloats
    the mapping prompt and confuses field selection. We merge sample values across
    all copies (so the model always sees example data) and record every sheet the
    column appears on under 'sheets'."""
    merged: dict[str, dict] = {}
    for f in template_fields or []:
        name = (f.get("name") or "").strip()
        if not name:
            continue
        cur = merged.get(name)
        if cur is None:
            cur = {
                "name": name,
                "sheet": f.get("sheet", ""),
                "sheets": [],
                "canonical_field": f.get("canonical_field"),
                "samples": [],
                "description": f.get("description"),
                "allowed_values": list(f.get("allowed_values") or []),
                "field_format": f.get("field_format"),
                "required": f.get("required"),
            }
            merged[name] = cur
        sh = f.get("sheet", "")
        if sh and sh not in cur["sheets"]:
            cur["sheets"].append(sh)
        if not cur.get("canonical_field") and f.get("canonical_field"):
            cur["canonical_field"] = f.get("canonical_field")
        # Keep the first non-empty dictionary enrichment seen for the column.
        if not cur.get("description") and f.get("description"):
            cur["description"] = f.get("description")
        if not cur.get("allowed_values") and f.get("allowed_values"):
            cur["allowed_values"] = list(f.get("allowed_values"))
        if not cur.get("field_format") and f.get("field_format"):
            cur["field_format"] = f.get("field_format")
        if not cur.get("required") and f.get("required"):
            cur["required"] = f.get("required")
        for s in (f.get("samples") or []):
            sv = str(s).strip()
            if sv and sv.lower() != "nan" and sv not in cur["samples"] and len(cur["samples"]) < 5:
                cur["samples"].append(sv)
    return list(merged.values())


def _template_fields_block(template_fields: list[dict]) -> str:
    """Render output template fields as a prompt context block.

    Each line shows the column NAME, its inferred value KIND (so the model can
    match a money cap to a money column, a percentage to a percent column, etc.),
    a few sample values, and — when the field is deduped across sheets — how many
    sheets carry it.
    """
    if not template_fields:
        return "(no output template fields provided)"
    lines = []
    for f in template_fields:
        sheet = f.get("sheet", "")
        name  = f.get("name", "")
        samples = f.get("samples", [])
        kind = infer_value_kind(name, samples)
        sample_str = f", samples: {samples[:3]}" if samples else ""
        # The column's internal data-model tag (canonical_field) is deliberately
        # NOT shown. Rule mapping keys off the RENDERED OUTPUT template — name,
        # documented meaning, samples, value-kind, allowed values (see module note
        # at top). A wrong upstream canonical tag (e.g. an insured-unit KEY column
        # mislabelled as a "percentage-share" concept) otherwise overrode the
        # field's real meaning/samples and forced impossible, dead maps.
        sheets = f.get("sheets")
        if sheets:
            where = f"on {len(sheets)} sheet(s)" if len(sheets) > 1 else f"sheet: {sheets[0]}"
        else:
            where = f"sheet: {sheet}"
        # Data-dictionary enrichment (when the template ships a spec sheet): the
        # column's documented MEANING and ALLOWED VALUES — map by meaning, not name.
        desc = f.get("description")
        desc_str = f"; means: {' '.join(str(desc).split())[:140]}" if desc else ""
        allowed = f.get("allowed_values")
        allowed_str = f"; allowed values: {allowed[:15]}" if allowed else ""
        lines.append(
            f"  - {name!r} (kind: {kind}; {where}{desc_str}{allowed_str}{sample_str})"
        )
    return "\n".join(lines)


def build_ajv_synthesis_prompt_for_template_batch(
    clauses_meta: list[dict],
    template_fields: list[dict],
) -> str:
    """
    BATCHED template-aware AJV synthesis — all clauses in ONE Gemini call.

    Response shape matches the non-template batch:
      {"results": [{"clause_id": N, "rules": [...]}, ...]}
    """
    fields_block = _template_fields_block(template_fields)
    field_names  = [f["name"] for f in template_fields if f.get("name")]

    payload = [
        {
            "clause_id":            cm["clause_id"],
            "clause_type":          cm.get("clause_type", "other"),
            "title":                cm.get("title", ""),
            "text":                 cm.get("text", ""),
            "section_header":       cm.get("section_header") or "(no section header)",
            "page":                 cm.get("page_number") or cm.get("page", 0),
            "suggested_rule_types": cm.get("suggested_rule_types", []),
            "rule_stage":           cm.get("rule_stage") or "output",
        }
        for cm in clauses_meta
    ]

    return f"""SYSTEM:
You are a JSON Schema synthesis assistant for Kavachio.

Given a BATCH of rule-bearing contract clauses, produce JSON Schema (AJV-compatible)
rules for EACH clause. Rules must validate rows of the OUTPUT TEMPLATE — NEVER use
internal database column names; use ONLY the field names listed below.

OUTPUT TEMPLATE FIELDS (use ONLY these as property keys inside json_schema):
{fields_block}

VALID FIELD NAMES: {json.dumps(field_names)}

VALUE FORMAT — live data may store a value as a full NAME or a standardized short
CODE, and a field's `samples` are not always the same form as the real rows. So when a
rule limits a field to specific values (enum / const) and the field uses a standardized
vocabulary (states & territories, countries, currencies, etc.), include BOTH forms for
each value — the full name AND its standard short code — so the rule matches either
way — include the full name AND its standard short code for each value (a region
and its abbreviation, a currency name and its ISO code, etc.). For NON-standardized
fields (classes of business, carrier names, etc.) use the field's `samples` format
when shown, otherwise the clause's wording.

JSON Schema patterns:
- Required:     {{"type":"object","required":["<OutputField>"]}}
- Range:        {{"properties":{{"<OutputField>":{{"type":"number","minimum":A,"maximum":B}}}}}}
- Enum:         {{"properties":{{"<OutputField>":{{"enum":["A","B"]}}}}}}
- Conditional:  {{"if":{{"properties":{{"<OutputField>":{{"const":"<value>"}}}}}},
                 "then":{{"required":["<OtherField>"]}}}}
- Date:         {{"properties":{{"<OutputField>":{{"type":"string","format":"date","formatMinimum":"YYYY-MM-DD"}}}}}}
- Exclusion:    {{"not":{{"properties":{{"<OutputField>":{{"enum":["v"]}}}}, "required":["<OutputField>"]}}}}

NUMERIC LIMITS — choose the operator from the clause meaning (do NOT default to `const`):
- A coverage/authority "limit of $X" — e.g. a "per occurrence limit of $<X>",
  under a "Maximum Limits …" heading, "up to / not to exceed $X" — is a CAP: use
  {{"type":"number","maximum":X}}.
- "at least / minimum / no less than $X" is a floor: use {{"type":"number","minimum":X}}.
- Use {{"const":X}} ONLY when the value must equal EXACTLY X (rare for money limits).

OUTPUT SCHEMA (strict JSON only — no markdown, no // comments, no trailing commas):
{{
  "results": [
    {{
      "clause_id": number,
      "rules": [
        {{
          "rule_name":        string,
          "rule_description": string,
          "stage":            "output",
          "severity":         "critical" | "warning" | "info",
          "canonical_target": {{"output_field": string, "description": string}},
          "json_schema":      object,
          "error_message":    string,
          "confidence":       number
        }}
      ]
    }}
  ]
}}

CRITICAL:
- Return exactly one results entry per clause_id in the input batch.
- canonical_target.output_field must be one of the VALID FIELD NAMES.
- Property keys inside json_schema must come from VALID FIELD NAMES only.
- Never use DB column names (e.g. effective_dt, premium_written).
- Every clause MUST yield at least one rule — never return an empty "rules"
  array. If no single Output Template field captures the clause exactly, pick the
  closest field(s) from the VALID FIELD NAMES above that best express its intent.
- Output ONLY the JSON object — no markdown fences, no explanations, no comments.

USER:
Batch of {len(payload)} AJV clause(s):

{json.dumps(payload, indent=2)}

Synthesize AJV rules for every clause using Output Template field names only."""


def build_custom_synthesis_prompt_for_template_batch(
    clauses_meta: list[dict],
    template_fields: list[dict],
) -> str:
    """
    BATCHED template-aware custom DSL synthesis — all clauses in ONE Gemini call.

    Response shape matches the non-template batch:
      {"results": [{"clause_id": N, "rules": [...]}, ...]}
    """
    fields_block = _template_fields_block(template_fields)
    field_names  = [f["name"] for f in template_fields if f.get("name")]

    payload = [
        {
            "clause_id":            cm["clause_id"],
            "clause_type":          cm.get("clause_type", "other"),
            "title":                cm.get("title", ""),
            "text":                 cm.get("text", ""),
            "section_header":       cm.get("section_header") or "(no section header)",
            "page":                 cm.get("page_number") or cm.get("page", 0),
            "suggested_rule_types": cm.get("suggested_rule_types", []),
            "rule_stage":           cm.get("rule_stage") or "output",
        }
        for cm in clauses_meta
    ]

    return f"""SYSTEM:
You are an aggregate-rule synthesis assistant for Kavachio.

Given a BATCH of rule-bearing contract clauses involving aggregations or cross-row
logic, produce Kavachio custom-rule DSL entries for EACH clause. Use ONLY the Output
Template field names listed below — never internal DB column names.

OUTPUT TEMPLATE FIELDS (use ONLY these in rule_spec):
{fields_block}

VALID FIELD NAMES: {json.dumps(field_names)}

CUSTOM DSL PRIMITIVES:
aggregate_limit:  {{"rule_type":"aggregate_limit","aggregation":"sum"|"count"|"distinct_count",
                   "field":"<OutputField>","group_by":["<OutputField>"],
                   "max_value":number|null,"min_value":number|null,"currency":string|null}}
cross_field_math: {{"rule_type":"cross_field_math","formula":"A = B * C","tolerance_pct":number}}
uniqueness:       {{"rule_type":"uniqueness","fields":["<OutputField>"],"scope":"per_upload"|"per_contract"}}

OUTPUT SCHEMA (strict JSON only — no markdown, no // comments, no trailing commas):
{{
  "results": [
    {{
      "clause_id": number,
      "rules": [
        {{
          "rule_name":        string,
          "rule_description": string,
          "stage":            "output",
          "severity":         "critical" | "warning" | "info",
          "canonical_target": {{"output_field": string, "scope": "per_upload"|"per_contract"}},
          "rule_type":        string,
          "rule_spec":        object,
          "error_message":    string,
          "confidence":       number
        }}
      ]
    }}
  ]
}}

CRITICAL:
- Return exactly one results entry per clause_id in the input batch.
- Use ONLY VALID FIELD NAMES in rule_spec.field, rule_spec.fields, rule_spec.group_by.
- canonical_target.output_field must be one of the VALID FIELD NAMES.
- Every clause MUST yield at least one rule — never return an empty "rules"
  array. If no single Output Template field captures the clause exactly, pick the
  closest field(s) from the VALID FIELD NAMES above that best express its intent.
- aggregate_limit MUST have at least one of max_value / min_value set.
- Output ONLY the JSON object — no markdown fences, no explanations, no comments.

USER:
Batch of {len(payload)} Custom clause(s):

{json.dumps(payload, indent=2)}

Synthesize custom DSL rules for every clause using Output Template field names only."""


# =========================================================
# PIPELINE 2.4 (IR) — Stage B: deterministic-IR extraction
# The LLM is demoted to a constrained extractor: it picks ONE template from the
# fixed catalog and fills its parameters with Output-Template field names. It
# NEVER authors JSON Schema or SQL. Determinism comes from the compiler.
# =========================================================

from contract_upload_services.rule_ir import (
    TEMPLATE_CATALOG as _IR_CATALOG,
    TEMPLATE_NAMES as _IR_TEMPLATE_NAMES,
)


def _ir_catalog_block() -> str:
    """One line per template: name, required params, and a one-line description
    (which also names the optional params like scope / max / min)."""
    lines = []
    for name in _IR_TEMPLATE_NAMES:
        spec = _IR_CATALOG[name]
        req = ", ".join(spec["required"])
        lines.append(f'- {name}: required params [{req}]. {spec["desc"]}')
    return "\n".join(lines)


def build_ir_synthesis_prompt_batch(
    clauses_meta: list[dict],
    template_fields: list[dict],
) -> str:
    """BATCHED IR extraction — all rule-bearing clauses in ONE Gemini call.

    The model picks a template from the catalog and fills its params with
    Output-Template field names. Polarity is chosen by template NAME
    (max_limit vs min_limit, value_in_set vs value_not_in_set). If no template
    fits a clause, the model returns {"template": null, "reason": ...} — it must
    NOT force a rule (the old "every clause must yield a rule" instruction is
    deliberately absent, since it manufactured junk).
    """
    # A BDX template often repeats the SAME columns across many per-schedule
    # sheets (e.g. 67 columns × 12 sheets = 800+ entries). Listing every copy
    # bloats the prompt, confuses field selection, and causes output truncation.
    # Dedupe to one entry per field name (merging samples across copies so the
    # model still sees example data).
    unique_fields = dedup_template_fields(template_fields)

    fields_block = _template_fields_block(unique_fields)
    field_names = [f["name"] for f in unique_fields if f.get("name")]

    payload = [
        {
            "clause_id":            cm["clause_id"],
            "clause_type":          cm.get("clause_type", "other"),
            "title":                cm.get("title", ""),
            "text":                 cm.get("text", ""),
            "section_header":       cm.get("section_header") or "(no section header)",
            "page":                 cm.get("page_number") or cm.get("page", 0),
            "suggested_rule_types": cm.get("suggested_rule_types", []),
        }
        for cm in clauses_meta
    ]

    return f"""SYSTEM:
You are a constrained rule EXTRACTOR for Kavachio. You do NOT write SQL or JSON
Schema. For each contract clause you pick ONE template from the fixed catalog
below and fill its parameters — nothing else.

TEMPLATE CATALOG (pick exactly one template name per rule):
{_ir_catalog_block()}

OUTPUT TEMPLATE FIELDS — every field/scope parameter MUST be one of these exact
names (grouped by sheet). Never invent or paraphrase a column name:
{fields_block}

VALID FIELD NAMES: {json.dumps(field_names)}

RULES FOR EXTRACTION:
- ONLY create a rule when the clause is a concrete, checkable constraint on a
  SPECIFIC Output Template field (a limit, an allowed/excluded set, a required
  field, a date/aggregate relation). Definitional, governance, commercial or
  vague clauses are NOT rules → return template null.
- TEMPLATE SELECTION: for each rule, evaluate the clause against EVERY template in
  the catalog above and choose the SINGLE most ELIGIBLE one — the template whose
  meaning matches the clause most precisely. The suggested_rule_types are only a
  weak hint; do NOT default to them. When more than one could apply, pick the MOST
  SPECIFIC: conditional_value (not value_in_set) when there is an unless/except
  condition; period_duration (not date_relation) for a duration in time units;
  aggregate_cap (not max_limit) for a SUM/COUNT across rows per group;
  range_check (not a lone min/max) when BOTH bounds are stated; value_not_in_set
  (not value_in_set) for an exclusion. If truly none fits, return template null.
- Map by the COLUMN — its NAME and meaning — NOT by the sample data. Pick the
  Output Template column whose NAME/meaning matches the clause's subject. The
  sample values shown are only illustrative; do NOT require the contract's value,
  scope, or list entry to appear in the samples, and do NOT return template null
  just because a value/scope/program-name isn't in the sample rows. If a column
  fits by name/meaning, USE IT.
- Still match obvious types by the column's MEANING: don't put a numeric limit on
  a name/description/text column, or a date check on a non-date column. A column
  whose name contains "%" stores a percentage — express "100%" as 1.0 if that
  column's values are fractions, else 100.
- DATE-PAIR SCOPE: templates often carry SEVERAL inception/expiry date pairs at
  different scopes (policy/risk, program, transaction, reporting, reinsurance).
  For period_duration / date_relation, bind to the pair whose SCOPE the clause is
  about — a "Policy Period" clause measures the POLICY'S/RISK'S own
  inception→expiry columns, not the program's, transaction's or reporting
  window's. Use another scope's dates ONLY when the clause itself names that
  scope, and never mix scopes across start/end.
- Polarity is chosen by the TEMPLATE NAME, never by a flag. "must not exceed X"
  → max_limit (max=X); "must be at least X" → min_limit (min=X); "must be one of"
  → value_in_set (allowed=[...]); "must not be" → value_not_in_set (excluded=[...]).
  For a binary Y/N flag column (Yes/No, Y/N, True/False, 1/0) where the clause's
  referral/violation event is the flag's AFFIRMATIVE state ("involves X", "any
  policy WITH X"), put the AFFIRMATIVE token itself in excluded=[...] — never
  encode it as the negation of the other branch (excluded=["No"] flags every
  COMPLIANT row instead of the one that needs a referral).
- If a clause states BOTH a lower and an upper bound, emit ONE bounded rule with
  BOTH bounds — never two. A numeric value "between $<A> and $<B>" → ONE
  range_check {{min:A, max:B}}. A duration "<N> … not to exceed <M> months" → ONE
  period_duration {{min:N, max:M}}. Do NOT drop a bound and do NOT split into two.
- "unless/except <condition>, <field> must be <value>" → conditional_value with the
  exemption in `condition` (e.g. "except <state>, <field> must be <value>"
  → condition {{field:"<scope field>", op:"!=", value:"<exempt value>"}},
  field:"<target field>", op:"=", value:"<required value>"). Do NOT drop the
  exemption by using a plain rule — if no template can hold the condition, return
  template null.
- TWO-COLUMN CONDITIONAL — "IF <column A> is/has <value> THEN <column B> must be
  <limit/value>" (the rule spans TWO different columns: one drives the condition,
  the other is constrained) → ONE conditional_value. The condition binds to column
  A, the target to column B, and the target op may be NUMERIC:
    "if <A> = <X> then <B> must not exceed <N>"
      → condition {{field:"<A>", op:"=", value:"<X>"}},
        field:"<B>", op:"<=", value:<N>   (violation = A is X AND B > N)
    "if <A> = <X> then <B> must be at least <N>"  → target op ">="
    "if <A> = <X> then <B> must be <Y>" (a value/code) → target op "="
  Column A and column B are DIFFERENT fields — never collapse them onto one column.
  Bind each to the column whose MEANING matches (A = the driver, B = the
  constrained metric). If either column is missing, return template null.
- MULTI-COLUMN CONDITIONAL — when the condition depends on TWO OR MORE columns AT
  ONCE (all must hold) → ONE **conditional_all**, never two rules. Put every driver
  in the `conditions` LIST and the constrained column in `field`/`op`/`value`:
    "if <A> is <X> AND <B> is <Y> then <C> must be <Z>"
      → conditions:[{{field:"<A>",op:"=",value:"<X>"}}, {{field:"<B>",op:"=",value:"<Y>"}}],
        field:"<C>", op:"=", value:"<Z>"   (violation = A=X AND B=Y AND NOT C=Z)
  Do NOT emit separate one-column rules for each driver and do NOT emit both
  polarities of a driver (e.g. one rule for B=Y and another for B≠Y) — that
  double-flags. A single conditional_all captures the exact combined condition.
- REFERRAL INTENTS ("is_referral": true) — the clause requires a REFERRAL when a
  trigger is met. Look for a REFERRAL-INDICATOR field in the output template: a
  column whose name/meaning is a referral flag (e.g. "... Referral Indicator",
  values like Yes / No / N/A). If one exists, map to **conditional_value**:
    condition = the TRIGGER {{field:"<trigger column>", op:..., value:...}}
                (bind the intent's subject/operator/value to the trigger column —
                 e.g. "facultative reinsurance secured" → a reinsurance
                 premium/limit column "> 0"; "Company net retention" → a net
                 premium/retention column "> 0"; "limit over $X" → the limit column
                 "> X"),
    field = "<the referral-indicator column>", op = "=", value = "Yes"
            (the documented "referred" value).
  This flags rows where the trigger IS met but the policy was NOT referred
  (indicator ≠ Yes). If there is NO referral-indicator column in the template,
  fall back to the plain trigger rule (e.g. max_limit on the trigger column) so the
  triggering rows are still surfaced.
  If the referral TRIGGER depends on TWO OR MORE columns together (e.g. "use
  <paper> for all policies EXCEPT home state <S>; any deviation requires Referral"
  → the referral is required when paper = <paper> AND state = <S>), use
  **conditional_all** instead: conditions = the list of driver columns
  ([{{field:"<paper column>",op:"=",value:"<paper>"}}, {{field:"<state column>",op:"=",value:"<S>"}}]),
  field = "<referral-indicator column>", op = "=", value = "Yes". ONE rule — do not
  split into a "state = <S>" rule and a "state ≠ <S>" rule.
  If that same two-column referral has NO referral-indicator column, still emit ONE
  rule — but write the REQUIREMENT, never the deviation. A conditional flags a row
  when its conditions hold and its TARGET FAILS, so the target must say what a
  COMPLIANT row looks like and the engine derives the deviation for you:
    "use <paper> for all policies EXCEPT home state <S>; any deviation requires
     Referral"
      → conditions:[{{field:"<state column>",op:"!=",value:"<S>"}}],
        field:"<paper column>", op:"=", value:"<paper>"
        (violation = outside the exemption AND the paper is not <paper> — exactly
         the deviation the clause calls out)
  Writing the target as the deviation instead (op "!=" against the REQUIRED value)
  inverts the rule: it flags every COMPLIANT row and passes the deviating ones.
  The same holds for any "must be <X> unless <exemption>" clause — target op "=",
  the exemption negated into the conditions.
- ALLOWED-WITH-CARVE-OUTS ("<allowed> excluding <X>, <Y>") is ONE rule:
  value_in_set with BOTH params — allowed=[<allowed>] AND excluded=[<X>,<Y>,...].
  Example — a territory "<region> excluding <sub-region 1>, <sub-region 2>" on the
  country field → value_in_set {{"field":"<country field>",
  "allowed":["<region>"], "excluded":["<sub-region 1>","<sub-region 2>"]}}.
  Keep the FULL proper names (not abbreviations) — value matching is canonicalized
  downstream, so write what the contract says.
- NESTED CARVE-OUT — the excluded items are a FINER level that lives in a DIFFERENT
  column than the allowed value (e.g. "<country> excluding <sub-regions>" where the
  country is a Country column but the sub-regions are STATE / TERRITORY values in a
  separate state column). Then the exclusion is CONDITIONAL on the broader value:
  the sub-regions are excluded ONLY when the country is <country>. Map it to
  **value_not_in_set on the finer (state/territory) column WITH a scope on the
  country column**:
    value_not_in_set {{"field":"<state/territory column>",
      "excluded":["<sub-region 1>","<sub-region 2>",...],
      "scope":{{"<country column>":"<country>"}}}}
  (e.g. "United States of America excluding Puerto Rico, US Virgin Islands, US
  Territories and Possessions" → field = the insured state/territory column,
  excluded = those three, scope = {{"<country column>":"United States of America"}}).
  This compiles to "flag rows where country = <country> AND state is one of the
  excluded sub-regions" — a nested exclusion, NOT a global one. Bind the country to
  the country column and the sub-regions to the state/territory column — never put
  the sub-regions on the country field. If the template has NO country column, drop
  the scope and emit a plain value_not_in_set on the state column.
- Put a row filter in `scope` as {{field: value}} when the clause applies only to
  certain rows (e.g. only <a coverage>). When the intent's scope names MULTIPLE
  entities that share the constraint (a grouped scope, e.g. several reinsurer
  papers that all have the same limit), use a LIST value:
  {{"<scope field>": ["<v1>", "<v2>", "<v3>", ...]}} — this compiles to an IN
  filter so ONE rule covers the whole group. Bind the scope field to the ONE
  column that carries those entity values.
- Use field names from VALID FIELD NAMES only. If the clause needs a field the
  Output Template does NOT have, you CANNOT express it — return template null.
- confidence is advisory only; do not inflate it. When you are NOT confident the
  clause maps cleanly to a template AND a specific correct field, prefer
  template null over a low-confidence guess.
- If NO template fits a clause, return one rule object with "template": null and
  a short "reason". DO NOT force a rule and DO NOT return an empty rules array.
  

OUTPUT (strict JSON only — no markdown, no // comments, no trailing commas):
{{
  "results": [
    {{
      "clause_id": number,
      "rules": [
        {{
          "template": "<one catalog template name>" | null,
          "params": {{ ...template-specific params using VALID FIELD NAMES... }},
          "rule_name": string,
          "rule_description": string,
          "severity": "critical" | "warning" | "info",
          "error_message": string,
          "confidence": number,
          "reason": string   // required only when template is null
        }}
      ]
    }}
  ]
}}

CRITICAL:
- Return exactly one results entry per clause_id in the input batch.
- Every field referenced in params MUST be in VALID FIELD NAMES.
- Choose the template whose NAME encodes the clause's polarity.

USER:
Batch of {len(payload)} rule-bearing clause(s):

{json.dumps(payload, indent=2)}

Extract the IR for every clause using Output Template field names only."""


# =========================================================
# PIPELINE 2 (3-CALL MODEL)
# Call 2 — rule_bearing + rule INTENT (field-agnostic)
# Call 3 — map each intent → ONE template + Output-Template fields → IR
#
# The previous single Stage-B call did "is this a rule + which template + which
# output field" all at once and under-mapped clauses that ARE checkable (it
# returned template:null whenever field-binding felt uncertain). Splitting the
# job lets call 2 capture the rule's intent without worrying about columns, and
# lets call 3 focus solely on matching that intent to the available fields.
# =========================================================

# Operators call 2 may emit. Call 3 maps each to the template whose NAME encodes
# that polarity — kept small and explicit so the two calls never drift.
_INTENT_OPERATORS = (
    "max, min, range, in_set, not_in_set, equals, required, pattern, "
    "date_relation, date_bound, duration_max, duration_min, duration_range, "
    "aggregate_max, cross_field_math, cross_field_compare, cross_field_or_value, "
    "unique, invariant, conditional_required"
)


def build_rule_intent_prompt_batch(clauses: list[dict]) -> str:
    """Call 2 — for each clause decide is_rule_bearing AND, if so, extract the
    rule INTENT (what is constrained, the operator, the value, the row scope) in
    plain language. NO output-template field names here — mapping is call 3.
    """
    payload = [
        {
            "clause_id":   c.get("clause_id"),
            "clause_type": c.get("clause_type", "other"),
            "title":       c.get("title", ""),
            "text":        c.get("text", ""),
            # When extraction resolved a deferred external document, it inlines the
            # real values into `text` and records the source here. A non-null value
            # (or an inlined "[Context …]" block in text) means the deferral is
            # ALREADY RESOLVED — the criteria are present, not deferred.
            "source_reference_document": c.get("source_reference_document"),
        }
        for c in clauses
    ]

    return f"""SYSTEM:
You are a rule analyst for Kavachio's BDX validation pipeline. For each insurance
contract clause you do TWO things:

1) Decide if it is RULE-BEARING — i.e. it can become a data check on a policy
   bordereau (BDX) row or set of rows.
   Rule-bearing: limits/sublimits, value ranges, allowed/excluded values,
   required fields, geographic territory restrictions, authorized/excluded
   classes of business, policy period/duration limits, date relationships,
   aggregate caps, uniqueness, math relationships, conditional requirements,
   participation / quota-share / percentage-of-risk / percentage-of-layer figures.
   PARTICIPATION / SHARE PERCENTAGE — a stated share the program/company takes of
   each risk is RULE-BEARING even when phrased tersely as a bare percentage, e.g.
   "Percentage of Total Risk: 100%", "100% per layer per policy", "Quota Share:
   25%". It constrains the reported participation / quota-share % field → emit an
   intent (operator equals for an exact %, or max/min if it is a ceiling/floor).
   Do NOT dismiss it as informational just because the sentence is short.
   NOT rule-bearing: framing, governance, commercial relationship, claims
   handling, signatures; definitions that introduce no checkable value; and PURE
   PROCESS obligations that name NO value a bordereau column would carry — e.g.
   who may sign, panel / TPA / vendor usage, the mechanics of a notification /
   submission workflow, reporting/accounting deadlines ("submit a report by the
   15th"). These have no testable field.
   *** "IT IS A DEFINITION" IS NOT ITSELF A REASON. *** A definition is exempt
   only when it defines something no bordereau column reports. A definition that
   NAMES A PARTY to the arrangement — "<Name>, a <state> corporation (hereinafter
   referred to as the 'Company')", "'Reinsured' shall mean <Name>", "<Name>
   (hereinafter the 'Reinsurer' / 'GA' / 'Administrator')" — states WHICH ENTITY
   every row must report in that role, which a bordereau does report on every
   row, so it IS rule-bearing (see test (e)). Judge a definition by what it
   defines, never by the fact that it is worded as one.

   DECISIVE TEST (apply in order):
   (a) Does the clause restrict a VALUE that a BDX column reports — a limit,
       premium, fee, date, the territory / home state of the insured risk, the
       class of business, the CARRIER / COMPANY / "paper" a policy is written on,
       or any identifiable field-level attribute? If YES → is_rule_bearing=TRUE,
       EVEN WHEN the clause is phrased as a referral / approval trigger or an
       operating instruction. The consequence (refer / get approval) is only the
       ACTION; the restricted value is what we CHECK. Examples that ARE
       rule-bearing: "policies must be issued on <a specific> paper", "no
       home-state policy in <state A> or <state B>", "carrier must be X",
       "backdating of more than <N> days requires referral", "quotations outside
       the Underwriting Guidelines require referral", "the underwriting of Policies
       shall be directed / overseen / performed by <a named Key Employee>" → the reported
       UW / underwriter field must be that named person (a value-set on the UW
       column). A NAMED person who must underwrite / sign / produce the business
       constrains the corresponding reported party field — it is rule-bearing.
       A prohibition or restriction on OFFERING / PROVIDING / WRITING a POLICY
       FEATURE, OPTION, MODE or TERM that a bordereau row carries as a per-policy
       ATTRIBUTE — a billing / installment / payment plan, a payment mode, an
       endorsement or coverage option, a policy form / type — is rule-bearing EVEN
       when phrased as the party's conduct ("Administrator prohibited from offering
       X", "shall not provide X"): the forbidden feature is a REPORTED per-policy
       value, so the check flags every policy that carries it. Do NOT assume the
       feature is unreported just because the sentence names the party's ACTION — a
       payment / installment plan, coverage option, or policy form IS a standard
       bordereau attribute a column reports.
   (b) Only if there is NO such reported value — the clause merely directs a
       party's conduct or process — is it NOT rule-bearing (control register).
   (c) ABSENT / NOT-APPLICABLE TERMS. If the clause's value is "Not Applicable",
       "N/A", "None", "Nil", "Waived", "0", or otherwise states the term does NOT
       apply (e.g. "Brokerage: Not Applicable"), there is NO value to validate on
       the BDX → is_rule_bearing=false. Never turn an absent term into a rule, and
       never let the mapping bind it to a column that merely shares a word with the
       term's name (e.g. "Brokerage" must NOT bind to an Agency / BOR column —
       a brokerage FEE and an agency/broker-of-record IDENTITY are unrelated).
   (d) OPERATIONAL / BUSINESS-PROCESS RESTRICTIONS are NOT rule-bearing (control
       register), EVEN when phrased as a "shall not" / "may only".
       This covers:
         • a restriction on a PARTY'S CONDUCT or business process — premium
           remittance / accounting timing, record-keeping, audit rights, claims
           handling, delegation / sub-delegation — none of which a bordereau ROW
           reports;
         • AUTHORITY / BINDING limits — limits on a party's authority to act
           ("binding authority limited to $X", "may not bind / quote without prior
           approval"), which govern the PROCESS of binding, not a reported value.
   (e) PROGRAM-IDENTITY / DESCRIPTION metadata is NOT rule-bearing. A clause that
       merely states the PROGRAM's own name, description, type or line — any
       "Program Description: <program name/line>", "Program Name: <name>",
       "Program Type: <type>", "Policies:<name>" etc related headers — are the program's IDENTITY, not a per-policy
       constraint. EVERY row in this bordereau already belongs to this program, so
       "policies must be of program <X>" is a tautology that validates nothing (and
       the BDX's program column may carry a different internal label). →
       is_rule_bearing=false. (This differs from a real value restriction such as
       authorized CLASSES of business or a territory, which DO vary row to row and
       stay rule-bearing.) NOTE: a program/contract PERIOD BOUNDARY DATE is NOT
       identity metadata — see (f).
       *** A NAMED PARTY IS NOT PROGRAM IDENTITY *** This exclusion covers the
       program's own LABEL — its name, description, type or line. It does NOT
       cover the ENTITIES that transact under the contract. A clause that names a
       PARTY TO THE ARRANGEMENT — whoever is stated to be the reinsured / cedant,
       the insurer / carrier / underwriting company, the coverholder / MGA /
       program administrator, the producing broker / intermediary, the
       underwriter — IS rule-bearing whenever a bordereau reports that party,
       which it routinely does: a bordereau names the carrier, the coverholder
       and the producer on EVERY row, so a row naming a DIFFERENT entity is
       business that does not belong under this contract, and that is the error
       this check exists to catch. A BARE STATEMENT IS ENOUGH — "Reinsured: <name>",
       "The Reinsured, being <NAME>", "<Role> as used in this Contract shall mean
       <name>", a definition, a signing-page attribution, or a party listed in the
       risk-details header all state the same requirement as "<role> must be
       <name>". Do NOT downgrade one to identity metadata because it is phrased as
       a label, a definition or a heading rather than as an obligation, and do NOT
       reason that it is a tautology "because every row is under this contract" —
       the party's name is a value the row REPORTS and can report wrongly. Emit
       one intent whose subject is the PARTY'S ROLE (e.g. "the reinsured / ceding
       company", "the program administrator / coverholder"), operator in_set, and
       value = the entity name(s) copied EXACTLY as written. The later mapping step
       decides which column reports that role — that is not your decision here.
   (f) PROGRAM / CONTRACT PERIOD BOUNDARY DATES are rule-bearing. A clause that
       states the program's / schedule's / contract's own INCEPTION / EFFECTIVE /
       COMMENCEMENT date and/or its EXPIRATION / TERMINATION / END date (e.g.
       "Program Inception Date: September 1, 2025", "Effective Date: <date>",
       "this Schedule expires <date>") defines the PROGRAM PERIOD — the window
       during which policies may attach. The program period is the SUPERSET and
       every policy period must be a SUBSET of it: a policy may NOT incept before
       the program's inception date, nor expire after the program's expiration
       date. Because each policy row reports its own inception and expiry dates,
       this bounds a reported BDX value → is_rule_bearing=TRUE. Emit an intent per
       stated boundary using operator **date_bound** (see section 2). Only a bare
       inception date with NO stated end still yields the lower-bound intent; do
       NOT invent an expiration date that the contract does not state. Even when the
       boundary DATE is bundled into a program-header clause alongside pure IDENTITY
       attributes (program name, writing companies, percentage of risk, program
       type), the clause STAYS rule-bearing for that date: emit the date_bound
       intent for the boundary date and ignore the identity attributes — never let
       the surrounding identity text downgrade the whole clause to not-rule-bearing.
       (Do NOT
       confuse this with process dates that bound a PARTY'S CONDUCT — a notice
       period, a reporting/accounting deadline, a termination-notice window — those
       stay NOT rule-bearing under (d): they name no per-policy reported date.)
   (g) COVERED SCOPE DEFINED BY REFERENCE TO ANOTHER AGREEMENT is rule-bearing,
       and the checkable value is THE REFERENCE ITSELF. When a clause says what
       business this contract covers by POINTING AT another agreement — its market
       reference / binder / lineslip / treaty / cover / contract number ("covers
       all business accepted by the Reinsured under UMR <ref>", "as per original
       binder referenced <ref>", "business bound under agreement <ref>") — a
       bordereau row reports the reference it was written under, and a row carrying
       a DIFFERENT reference does not belong to this contract at all. →
       is_rule_bearing=TRUE, with ONE intent whose subject is that REFERENCE (e.g.
       "the market reference / binder the business was accepted under"), operator
       in_set, and value = the referenced identifier(s) copied EXACTLY as written.
       THE CLAUSE'S HEADING IS NOT THE SUBJECT: such a clause is very often headed
       "Class", "Class of Business", "Exclusions" or "Territorial Scope", yet it
       names NO class, exclusion or territory — only a reference. Take the subject
       from the VALUE the clause actually states, never from its heading, and do
       NOT emit an intent about the class / exclusion / territory it is filed
       under. If the contract states SEVERAL such references (e.g. the risk-details
       page and a signing page each name one), put them ALL in the SAME in_set —
       every one of them is covered business — rather than emitting competing
       single-value rules.
       *** WHOSE REFERENCE IS IT? *** Only a reference to ANOTHER, UNDERLYING
       agreement is checkable — the binder / lineslip / cover the business was
       ACCEPTED or BOUND under, which each bordereau row reports. THIS document's
       OWN reference is NOT: a clause that cites the number of "this Contract" /
       "this Agreement" / "this Slip" (typically the same number printed in the
       document's own header, and often appearing in an offset, arbitration,
       notices or similar administrative clause — "amounts due under this Contract
       only (UMR <ref>)") is stating its own IDENTITY. Every row of this bordereau
       already belongs to this contract, so it is a tautology — and the bordereau's
       reference column reports the UNDERLYING agreement, so enforcing this
       contract's own number there flags EVERY row. → is_rule_bearing=false for
       that reference (see (e)). DECISIVE TEST: does the sentence introduce the
       reference as what the business was WRITTEN UNDER (checkable), or as what
       THIS document IS (identity)? When one contract mentions both, keep only the
       underlying agreement's reference.
       SCOPE OF THIS EXCLUSION: it applies to the REFERENCE IDENTIFIER ONLY — the
       contract / slip / agreement NUMBER. It says nothing about the contract's
       named PARTIES: "the Reinsured, being <NAME>", "Coverholder: <name>",
       "'Program Administrator' shall mean <name>" name ENTITIES a bordereau
       reports per row and stay rule-bearing under (e). Never carry "this document
       is describing itself" across from a reference number to a party name.
       (Contrast (e): the underlying agreement's number IS a per-row reported value
       that decides whether a row is in scope, so it stays rule-bearing.)

2) If rule-bearing, extract one or more INTENTS describing WHAT to check —
   WITHOUT naming any spreadsheet column (that is a later step):
   - subject: plain-language name of the thing constrained
     (e.g. "per-occurrence limit", "domicile state / territory",
      "policy duration", "class of business").
     KEEP any OWNERSHIP / SCOPE qualifier from the title in the subject — whether
     the limit is the COMPANY's / carrier's / program's OWN share vs the whole
     policy ("Company program gross limit per occurrence", "Company net
     retention"), and any party name. This qualifier decides which column the
     mapping step binds to, so do NOT drop it down to a bare "per-occurrence
     limit".
   - operator: one of [{_INTENT_OPERATORS}].
       "must not exceed X" / "Maximum Limits" / "limit of $X" / "up to X" → **max**
         (a stated limit is a CEILING, not an exact value — never emit `equals`/
         `range` with the same number on both sides for a limit).
       "at least X" / "minimum of X" → min ;
       THRESHOLD PROHIBITION / EXCLUSION — take the polarity from the FORBIDDEN
         side, NOT from a label like "limit" / "limitation" / "production
         limitation". When a clause PROHIBITS / excludes / declines / refuses (or
         refers) risks whose value lies on ONE side of a threshold, that threshold
         bounds the PERMITTED range:
           • values ABOVE it are the forbidden ones ("exceeding X", "greater than
             X", "more than X", "over X", "in excess of X", "> X" → not permitted /
             excluded / prohibited / ineligible) → the threshold is a MAXIMUM →
             operator **max** (flag values > X).
           • values BELOW it are the forbidden ones ("less than X", "under X",
             "below X", "fewer than X", "< X" → not permitted …) → the threshold is
             a MINIMUM → operator **min** (flag values < X).
         NEVER invert this: a clause forbidding the too-HIGH values is **max**, not
         min — a min rule would flag every compliant row that is correctly below the
         threshold and let the real (too-high) violations pass.
       ANY numeric value with BOTH a lower AND an upper bound (e.g. "between A and
         B", "min A and max B", "no less than A and no more than B") → ONE intent,
         operator **range**, value = {{"min": A, "max": B}} — do NOT emit two
         separate min and max intents. (This applies to every numeric value —
         limits, premiums, fees, rates, counts — not just durations.)
       "must be one of / authorized classes" → in_set ;
       "must NOT be / excluded / prohibited" → not_in_set ;
       "within N months" / "no longer than N" → duration_max ;
       "at least N months" → duration_min ;
       a duration with BOTH a lower AND an upper bound (e.g. "min N and max M",
         "between N and M months") → ONE intent, operator **duration_range**, with
         value = {{"min": N, "max": M}} — do NOT emit two separate intents.
       "X months + Y months odd time" (also "X months plus Y months odd time")
         is ONE single constraint, NOT two. The base term X and the odd-time
         allowance Y describe the SAME policy-period field: the period may run from
         X up to X+Y. Emit EXACTLY ONE intent — operator **duration_range** with
         value = {{"min": X, "max": X+Y}} (e.g. "12 months, plus 6 months odd time"
         → {{"min":12,"max":18}}). NEVER also emit a separate "X months" exact /
         equals intent for the base term — an exact-X rule (min==max==X) would
         CONTRADICT the range and wrongly flag every valid odd-time policy.
       A POLICY-PERIOD clause that gives a BASE term/range AND a LONGER allowance
         for a NAMED SUBSET of policies (e.g. "12 months + 6 months odd time;
         EXCEPT <N> months for wrap-up / project / construction policies") is STILL
         ONE duration_range — NEVER drop it or mark it not-rule-bearing because of
         the exception. Emit min = the smallest stated floor and max = the LARGEST
         stated allowance (the exception's) — the FULL permitted envelope — so no
         permitted policy type is falsely flagged (emitting only the base max would
         wrongly flag every valid long-dated policy the exception allows).
         Emit it as EXACTLY ONE UNSCOPED intent (scope = null). Do NOT split it into
         two SCOPED intents (a base range scoped to "exclude the subset" plus an
         extended range scoped to "only the subset"): the wrap-up / project /
         construction subset is a policy CHARACTERISTIC that NO bordereau column
         identifies, so BOTH scoped halves fail to map and drop to review — losing
         the whole check. One unscoped [smallest-floor, largest-allowance] range
         keeps the check runnable for every row.
       a PROGRAM / CONTRACT PERIOD BOUNDARY DATE (per DECISIVE TEST (f)) →
         operator **date_bound**, value = an OBJECT {{"op": "<compliant op>",
         "date": "YYYY-MM-DD"}} (normalize the stated date to ISO). Polarity:
         • an INCEPTION / EFFECTIVE / COMMENCEMENT date → the policy start must not
           precede it → op ">=" ; set subject to "policy inception / effective date"
           (the policy's own start date, the lower bound of its period).
         • an EXPIRATION / TERMINATION / END date → the policy end must not exceed
           it → op "<=" ; set subject to "policy expiry / expiration date" (the
           policy's own end date, the upper bound of its period).
         • an EXPIRATION / TERMINATION / END date ALSO bounds the policy's START
           date, and that is a SEPARATE requirement → op "<" (STRICTLY before,
           never "<=") ; set subject to "policy inception / effective date".
           Emit this IN ADDITION to the "<=" bound on the end date above — they
           are different checks and both are required:
             – end   <= expiry  says the policy FITS INSIDE the contract period.
             – start <  expiry  says the policy BELONGS TO THIS contract rather
               than to the next one. A policy incepting exactly ON the expiry
               date belongs to the SUCCEEDING contract, not this one.
           The "<" is load-bearing. Contracts run back to back — one expires the
           day the next incepts — so with "<=" a policy incepting on a renewal
           day satisfies BOTH contracts' windows, is counted against both, and
           the premium DOUBLES. Nothing errors; the two contracts simply each
           claim the row. (Palms BDX Ingestion BRD v1.2, Appendix 2 §2.10:
           "pol_eff_dt >= inception_date AND pol_eff_dt < expiry_date — this is a
           strict boundary join; < (not <=) on expiry date is intentional and
           must be preserved.")
         Emit ONE date_bound intent PER stated boundary PER bounded date, so a
         clause giving BOTH an inception and an expiration date yields THREE
         intents: start >= inception, start < expiry, end <= expiry. Do NOT use
         date_relation here (that is column-vs-column); the boundary is a FIXED
         contract date.
       A PARTICIPATION / SHARE clause — "<party> assumes a P% share of $B, that is
         $A" (a signed line, a quota share, a participant's proportional
         participation) — carries ONE requirement: the PROPORTION P. $A is not an
         independent requirement, it is P% arithmetic on the ONE base $B the clause
         names (usually the layer limit), so it is true of that base and of nothing
         else. Emit operator **equals** with value = P as a DECIMAL FRACTION (5.00%
         → 0.05), and write `subject` as the party's SHARE OF THE CEDED AMOUNT
         (e.g. "the subscribing reinsurer's share of each ceded amount"), so call 3
         binds it to the share column and its base column.
         *** NEVER emit the derived absolute $A as the value. *** A bordereau
         reports the share PER ROW (per policy, per transaction), and no per-row
         amount equals a whole-contract figure — a rule "every row must equal $A"
         flags 100% of rows and can never detect a mis-stated share. Keep $A and $B
         in rule_description as context, never as the value.
       "must not change / must stay the same / must remain identical ACROSS all
         <rows of one entity>" (e.g. a policy's effective date across its
         transactions, an insured's name across its endorsements) → operator
         **invariant**, value = an OBJECT
         {{"per": "<the entity the value must stay constant WITHIN, in plain
         words — e.g. 'the policy', 'the policy number'>"}}. `subject` is the
         value that must NOT change (the effective date), NOT the entity.
         *** NEVER use `unique` for this. *** `unique` means "no two ROWS may
         repeat this value", which is the opposite requirement: a bordereau lists
         MANY rows per policy (transactions, endorsements, instalments) that
         legitimately repeat the same policy number and the same effective date,
         so a `unique` rule would flag every ordinary multi-transaction policy.
         Use `unique` ONLY when the contract genuinely forbids a REPEATED row
         (e.g. "each policy must appear once per statement").
       "rate of X%" / an EXACT numeric rate or percentage → operator **equals**
         with the DECIMAL NUMBER (a percentage P% becomes P/100),
         NEVER in_set. A percentage / rate / amount is NUMERIC, so it must
         use a numeric operator (equals/max/min/range) and a numeric value — never
         a string value-set (a fuzzy text match on a numeric column flags every
         row).
     NOTE on insurance "aggregate": "general aggregate limit", "products/completed
     operations aggregate limit", "per occurrence limit" are all PER-POLICY
     ceilings on one row → operator **max** (NOT aggregate_max). Use aggregate_max
     ONLY when the contract caps a TOTAL across many policies ("sum of all
     limits", "portfolio aggregate", "total bound premium").
       "A must be >= / <= X% of B" or "A must be >= / <= B" — a field compared to a
         PERCENTAGE/MULTIPLE of, or directly to, ANOTHER field → **cross_field_compare**.
         Name BOTH fields and the factor in subject/value/description, e.g.
         "<fee A> >= <P>% of <base B>" or "<fee A> <= <amount B>".
       "the GREATER of <P>% of B or $<C>" (also "the LESSER of …") is ONE
         constraint, NOT two — the field is bound by a SINGLE combined value (the
         LARGER, or SMALLER, of a percent of B and a flat amount). Emit ONE intent,
         operator **cross_field_or_value**, value = an OBJECT {{"op": "<see VERB
         below>", "other_field": "<B in plain words, e.g. gross written premium>",
         "operator": "*", "factor": <P/100>, "value": <C>, "bound": "greater"}}
         (use "bound":"lesser" for a "lesser of" clause). Do NOT split it into a
         separate cross_field_compare + min/max pair — that reports ONE requirement
         as TWO exceptions and mis-flags a row that satisfies only the smaller side.
         *** CHOOSE `op` BY THE CLAUSE'S VERB — this is the exact-vs-comparison
         decision: ***
           • DEFINITIONAL "shall BE / must BE / is / equals / shall be set at the
             greater/lesser of …" → the field must EQUAL that computed amount →
             op "=" (an over-payment is ALSO a violation, so "=" not ">="). This is
             the fronting-fee case: "Fronting Fee shall be the greater of 12.5% GWP
             or $15,000" → op "=".
           • FLOOR "at least / no less than / minimum of / not below the greater of …"
             → op ">=".
           • CEILING "not to exceed / no more than / up to / maximum of / capped at
             the lesser/greater of …" → op "<=".
         (A plain "A >= <P>% of B" with NO flat alternative stays a
         cross_field_compare; a plain "A >= $<C>" stays a min. The exact-vs-floor
         verb test above applies the same way to those: a definitional "A shall BE
         <P>% of B" is a cross_field_compare with op "=", while "A at least <P>% of
         B" is op ">=".)
   - value: the threshold/number, the list of allowed/excluded values, or null.
   - scope: plain-language row filter when the rule applies only to some rows
     (e.g. "only <a coverage>", "only in <state(s)>"), else null.
   - DEFINED-TERM REFERENCES MUST BE RESOLVED. A clause often restricts a field
     by REFERRING to a group the document defines elsewhere ("issued by the
     <term>s", "one of the <term>s") — the actual members are named in other
     clauses of this same batch (definitions, summary tables). The intent's
     value must be the ENUMERATED member names as defined there, never the
     referring phrase itself: a phrase like "the <term>s" is a cross-reference,
     not a value any data cell will ever hold. If the document nowhere
     enumerates the members, leave value null and say so in the description —
     do not invent members and do not pass the reference through as a value.
     Resolve to ONE intent whose value lists EVERY member the document names —
     never one intent per member: per-member "must equal <name>" intents on the
     same field contradict each other (each would flag every other member's
     rows).
   - TABLES DEFINE THEIR OWN SCOPE. A clause laid out as label→value pairs (a
     key-value table) mixes two kinds of rows, distinguishable from the table's
     own content — no particular label is special: rows that IDENTIFY what the
     table is about (their value NAMES an entity, party, program, class,
     schedule, territory or category) and rows that STATE constraints for it
     (their value is an amount, limit, percentage, date, duration or list of
     allowed values). Each table's constraints hold ONLY for whatever its
     identifying rows name — a contract may carry several such tables, each
     stating different values for a different cohort. Every intent extracted
     from a constraint row of such a table MUST carry the table's identifying
     row(s) as its `scope`, phrased plainly in the table's own words. Never
     emit one table's constraint as an unscoped all-rows rule. The IDENTIFYING
     rows themselves are scope, NOT constraints: never emit "field must equal
     <the identifying value>" from an identifying row — sibling tables would
     each contradict it. When membership of the identified group is itself the
     check, it is the ONE resolved list of every such name (see the
     defined-term rule above).
   - severity: "critical" | "warning" | "info".
   - is_referral: true when the clause's consequence is a REFERRAL or prior
     APPROVAL to the Company (rather than an outright prohibition). The check
     still flags the triggering rows — but as "referral required", not a hard
     violation — so phrase error_message accordingly
     (e.g. "Referral to Company required: <the triggering condition>").
     Use severity "warning" for referrals. Default false.
     A referral whose trigger is an EXCEPT/deviation on a SECOND column is ONE
     intent, not two. E.g. "use <paper> for all policies EXCEPT home state <S>;
     any deviation requires Referral" means the referral is required exactly when
     paper = <paper> AND state = <S> — emit a SINGLE is_referral intent that
     carries BOTH drivers (put one driver in field/operator/value and the other in
     scope, keeping the clause text intact for call 3). Do NOT split it into a
     "state = <S>" intent and a "state ≠ <S>" intent — opposite polarities together
     flag every row.
   - rule_name + rule_description + error_message: short, human-readable.

IMPORTANT:
- Extract the intent even if you are unsure a matching column exists — do NOT
  self-censor here; call 3 decides mappability.
- A single clause may yield MULTIPLE intents (e.g. an inclusion plus an
  exclusion, or a cap plus a floor).
- GROUP a table's rows by SHARED constraint value — do NOT emit one intent per row
  when many rows carry the SAME constraint. When a table assigns the SAME value
  (e.g. the same maximum limit) to SEVERAL entities, emit ONE intent for that
  value whose `scope` names ALL the entities that share it as a LIST, not one
  intent per entity. E.g. an approved-list table where 7 papers have a $25M limit
  and 5 have a $10M limit → exactly TWO limit intents: one max=25M scoped to the
  list of the 7 papers, one max=10M scoped to the list of the 5 papers (plus, if
  the table is an approved/allowed list, ONE in_set intent for the whole list of
  entities). This keeps the rule count small and faithful to the contract's own
  groupings.
- Capture every DATA constraint on the insured RISK / POLICY as reported on the
  bordereau (limits, premiums, dates, territory of the risk, classes of business
  written), even if brief. Do NOT turn a party's operating procedure into a rule.
- EXEMPTIONS that EXCUSE rows ("rule X does not apply to Y", "except for Y",
  "unless Y") narrow another rule's SCOPE — do NOT flag the excused rows. Fold the
  carve-out into the parent rule's scope (e.g. "<field> must be <value> except for
  <some state/condition>" → ONE rule scoped to exclude that state/condition). Never
  flag the rows the contract excuses, and never emit an opposite-polarity intent
  for a true exemption.
- PROHIBITED CARVE-OUTS are the OPPOSITE and DO yield checks: "permitted = X,
  excluding / not / prohibited Y" means Y is FORBIDDEN and must be flagged. Emit
  BOTH intents — an inclusion (operator in_set, value = the permitted X) AND an
  exclusion (operator not_in_set, value = the forbidden Y). When Y is a FINER
  geographic level than X (Y are states / territories WITHIN the permitted country
  X — i.e. "<country> excluding <sub-regions within it>"), set the exclusion
  intent's `subject` to that finer level ("state / territory of the insured risk")
  and the inclusion's
  `subject` to the broader one ("country of the insured risk") so call 3 binds
  each to the correct column (country vs state). ALSO set the exclusion intent's
  `scope` to "only when country is <X>" — the sub-regions are excluded ONLY within
  that country (a NESTED carve-out), so the check must fire on the state column
  only for rows whose country is <X>, not globally. E.g. "United States of America
  excluding Puerto Rico, US Virgin Islands, US Territories and Possessions" → one
  in_set intent (country in [United States of America]) AND one not_in_set intent
  (subject = state/territory, value = [Puerto Rico, US Virgin Islands, US
  Territories and Possessions], scope = "only when country is United States of
  America").
- GOVERNANCE / PROCESS / OPERATIONAL clauses are NOT data validations — set
  is_rule_bearing false for: which carrier/company/"paper" issues or fronts
  policies, who may issue/bind/sign, approval/renewal carryforward conditions,
  referral submission & documentation process, reporting/accounting deadlines,
  termination, signatures. These direct a party's conduct, not a BDX row value.
- REFERRALS TO COMPANY — READ EVERY SUBPOINT SEPARATELY, THEN DECIDE. A "Referrals
  to Company" / "prior approval" clause is almost always a LIST of DISTINCT referral
  conditions — numbered/lettered items (1, 2, i, ii, iii, …) and every item after an
  "and any of the following". Do NOT compress the clause into one intent, and do NOT
  judge it from its opening line. Work through the items ONE AT A TIME. For EACH
  item:
    (a) RESTATE, in your own plain words, the SINGLE concrete condition about a
        policy that would make it require referral (what is true of that policy).
    (b) Is that condition REPORTABLE — i.e. about a value a bordereau ROW actually
        carries (a premium / limit / share / retention amount; a coverage or class
        of business; a state; a broker or placement; whether reinsurance was
        secured)? If YES → emit ONE intent for that item, with a `subject` that
        states the condition precisely, and set "is_referral": true. (The mapping
        step binds it to the right column and, when a referral-indicator column
        exists, makes it conditional: trigger met AND policy NOT referred.)
    (c) Is it VAGUE / JUDGMENTAL, or does it need data that is NOT on a bordereau
        (e.g. "outside / modifications to the underwriting guidelines", a
        reinsurer's policyholder surplus, "at underwriter discretion", "exceeds the
        authorities granted in Section X")? → SKIP it, no intent: there is no column
        to compute WHEN the referral was owed.
  A leading frame like "any risk where … exceed the authorities … AND any of the
  following items:" is NOT itself the check — the checkable conditions are the
  enumerated items beneath it, so evaluate each item on its own merit.
  Keep is_referral=true for every referral item (never convert it into a hard
  max/min/value rule that would flag every triggering row regardless of referral).
  (Contrast: a hard "maximum limit of $<X> per policy" with NO referral/approval
  language IS a plain max_limit.)
- DEFERRED CRITERIA (do NOT fabricate a rule). If the clause's actual check
  criteria — the list, threshold, values or conditions — are NOT stated in the
  clause itself but deferred to an EXTERNAL document, separate guidelines, or
  ANOTHER contract/section ("per the … Guidelines", "on file with the Company",
  "as approved", "in accordance with …", "see Section X", "as listed in … below"),
  do NOT emit an intent for that deferred criterion — it cannot be checked against
  the BDX from this contract alone. Emit an intent ONLY for criteria whose
  concrete value/list/threshold appears in THIS clause. (A clause that states a
  concrete value AND also cites a reference is fine — use the concrete value and
  ignore the reference.) If a clause's ONLY content is such a reference, return
  is_rule_bearing=false with "intents": [].
- RESOLVED REFERENCE — the deferral is ALREADY SATISFIED when the clause carries a
  bracketed context block that quotes the referenced document's concrete content
  (e.g. "[Context from <document name> - <section>: <the actual list / values>]").
  That block was injected because the cited guideline/document WAS uploaded and its
  values pulled in, so the criteria are NO LONGER deferred — they are present in
  THIS clause. TREAT THE BRACKETED CONTENT AS STATED-IN-CLAUSE and EXTRACT the
  intent(s) from it: is_rule_bearing=TRUE, with the allowed/excluded list (or
  threshold) taken from the bracketed values. This OVERRIDES the DEFERRED CRITERIA
  rule above even when the clause's lead-in is itself a bare deferral ("Per the …
  Guidelines on file"). Examples:
    • "Excluded Classes of Business: Per … Guidelines on file. [Context from <guide>
      - Excluded Occupancies: <Group A>: <items…>; <Group B>: <items…>]" → ONE
      not_in_set intent, value = those excluded occupancy groups/items.
    • "Authorized / Targeted Classes: Per … Guidelines on file. [Context from <guide>
      - ELIGIBLE … CLASSES: <Group>: <items…>; …]" → an in_set intent whose value is
      those eligible groups/items FROM THE REFERENCE — not only the contract's broad
      class wording. When the contract ALSO states its own class value, prefer the
      reference's concrete group/item list (it is the authoritative, checkable set).
  *** ALREADY-RESOLVED DEFERRAL — this is NOT deferred criteria. *** A clause may
  cite an external document ("Per the … Guidelines on file with the Company") AND
  then carry that document's ACTUAL content inlined into its own text — shown as an
  appended "[Context from <document> — …]" / "[Context — …]" block listing the real
  values, and/or a non-null "source_reference_document". When that inlined content
  is present, the deferral has ALREADY BEEN RESOLVED for you: the concrete
  list/threshold/values ARE stated in THIS clause (in the Context block). Treat
  those inlined values exactly as if they were written in the clause body — the
  clause IS rule-bearing; emit the intent over the inlined values. Do NOT mark such
  a clause not_rule_bearing merely because its lead-in says "per the Guidelines" or
  "on file" — the "only content is a reference" exclusion applies ONLY when NO
  inlined Context values and NO source_reference_document are present.
- PURE GOVERNANCE / PROCESS clauses are NOT data validations — set
  is_rule_bearing false ONLY when there is NO reported BDX value to test: who may
  issue/bind/sign, the renewal/carryforward approval mechanics, the submission &
  documentation PROCESS itself, reporting/accounting deadlines, termination,
  signatures. BUT a restriction that names an identifiable reported attribute —
  the carrier/company/"paper" a policy is written on, the home state, the class,
  a fee cap, a backdating window — IS rule-bearing (see DECISIVE TEST): emit the
  intent, and when its consequence is "refer to Company" / "requires approval",
  set is_referral=true rather than dropping it to the control register.

OUTPUT (strict JSON only — no markdown, no // comments):
{{
  "results": [
    {{
      "clause_id": number,
      "is_rule_bearing": boolean,
      "reasoning": string,
      "intents": [
        {{
          "subject": string,
          "operator": string,
          "value": (number | [string|number] | string | null),
          "scope": string | null,
          "severity": "critical" | "warning" | "info",
          "is_referral": boolean,
          "rule_name": string,
          "rule_description": string,
          "error_message": string
        }}
      ]
    }}
  ]
}}
If is_rule_bearing is false, return "intents": [].

USER:
Batch of {len(payload)} clause(s):

{json.dumps(payload, indent=2)}

Analyze each clause."""


def build_ir_mapping_prompt_batch(
    intent_items: list[dict],
    template_fields: list[dict],
    forced_field: str | None = None,
    relaxed: bool = False,
    forced_fields: list[str] | None = None,
    is_generic: bool = False,
) -> str:
    """Call 3 — map each rule INTENT (from call 2) to ONE catalog template and
    bind its params to Output-Template field names. This call does ONLY mapping:
    the intent (what to check, operator, value, scope) is already decided.

    `intent_items` is a flat list of:
        {clause_id, intent_index, subject, operator, value, scope, severity,
         rule_name, rule_description, error_message, clause_text}

    `forced_field`: when set (human review-queue resolution), the user has
    manually chosen the Output-Template field these intents must target. The
    full field list is still provided so scope/group_by params can bind, but the
    primary value/subject is directed onto this column instead of being searched.

    `forced_fields`: the FULL set of columns a reviewer selected for the rule when
    it spans several columns (e.g. a nested carve-out on country + state, or a
    multi-column conditional). `forced_field` is the primary of these; the rest are
    offered as the columns to use for scope / conditions / other operands.

    `relaxed`: auto-retry attempt 2. These intents came back UNMAPPED on the strict
    first pass. Add a "reconsider" instruction so the mapper binds each to the
    CLOSEST field that can represent its subject (by meaning + value kind) instead
    of returning null — only refusing when genuinely NO field can hold the value.

    `is_generic`: this batch is entirely Kavachio's generic rule library (never
    mixed with contract-derived intents — see stage_b_synthesizer._run_mapping_batches).
    A generic intent's subject is a bare CONCEPT ("Contract ID"), not a requirement
    a contract asserted exists in THIS program's template, so it may legitimately
    have no distinct column here. Tempers the default "always find the closest
    field" pressure with an instruction to decline rather than guess.
    """
    # Collapse the per-sheet duplicate columns to one entry per name (a BDX
    # template repeats ~70 columns across every schedule sheet). The full list is
    # 800+ noisy entries that confuse field selection and truncate output; the
    # deduped list also surfaces each column's inferred value KIND for matching.
    # BLOCK ORDER IS LOAD-BEARING — forced_block / relaxed_block / generic_block are
    # emitted at the very END of this prompt, just before the USER payload, not up by
    # the field list where they used to sit.
    #
    # They are the only parts that differ between this prompt's four variants (main /
    # generic-library / forced-column rescue / relaxed retry). While they sat at ~1.3%
    # of the prompt, the ~48,000 characters of instructions AFTER them landed at a
    # different offset in every variant, so prefix caching saw four unrelated prompts
    # and each variant paid full input price — measured: the library and retry calls
    # cached 0 and 1,022 tokens of a ~15,800-token shared prefix.
    #
    # They now sit just INSIDE the USER turn, ahead of the batch itself. That places
    # them on the payload side of the "\nUSER:\n" split that gemini_service uses for
    # context caching, so all four variants share one identical, cacheable SYSTEM
    # prefix and differ only in the small variable tail — which is the entire point.
    # The MAIN variant is unaffected either way: all three blocks render as empty
    # strings there, so its prompt is byte-identical before and after this change.
    unique_fields = dedup_template_fields(template_fields)
    fields_block = _template_fields_block(unique_fields)
    field_names = [f["name"] for f in unique_fields if f.get("name")]
    # _dump_mapping_output_template(unique_fields, fields_block)

    relaxed_block = ""
    if relaxed:
        relaxed_block = """
SECOND-ATTEMPT / RECONSIDER (these intents were UNMAPPED on the strict first pass):
You previously returned no usable field for these intents. Reconsider MORE
PERMISSIVELY now:
  • Bind each intent to the CLOSEST Output-Template field that can represent its
    subject by MEANING and value KIND (money/%/date/text-code) — an exact name
    match is NOT required. A near-synonym or a broader/adjacent column of the RIGHT
    kind is acceptable if its meaning fits.
  • Prefer emitting a rule on the best-fitting field over returning null.
  • Still respect the hard guards: never bind a value onto a structural KEY /
    identifier column, never cross DIRECT-carrier vs REINSURER parties, never put a
    country value in a state column, never put an IN-PERIOD date bound on a
    record-keeping date (see DATE ROLE — booked / entered / accounted / reported /
    as-of dates follow the reporting calendar, so they legitimately fall outside the
    policy period), never compare a rolled-up TOTAL against a single COMPONENT of
    it in a plain two-column comparison (see SAME SUBJECT, SAME LEVEL — being
    permissive here does NOT mean electing one component to stand for a total),
    and keep the correct value KIND.
  • Return "template": null ONLY when truly NO field of the right kind/meaning
    exists — and then give a one-line reason naming the missing column, so a human
    can select it.
  • NOT a valid reason, on this pass or any other: "the value does not appear in
    the field's sample values" / "cannot be grounded from sample data". The
    samples are a few illustrative cells; the allowed/excluded values come from
    the CONTRACT, and a row carrying a value the contract does not authorise is
    exactly what the rule is built to catch. If the column can HOLD that kind of
    value, bind it and emit the rule.
"""

    generic_block = ""
    if is_generic:
        generic_block = """
GENERIC RULE LIBRARY BATCH (every intent below is a Kavachio standard check, not
extracted from this contract):
  • These subjects are bare CONCEPTS ("Contract ID", "Insured Postal Code"), not a
    requirement this specific contract stated exists. Unlike a contract-derived
    intent, there is no guarantee a distinct column for this concept exists in
    THIS program's Output Template.
  • Bind ONLY when a field's NAME or documented MEANING clearly and specifically
    represents the concept — not merely because it is the closest-shaped or
    closest-sounding identifier/text column available. Two identifier-style
    columns that are NOT the same concept (e.g. a market reference / UMR number
    vs. a contract id) must never be treated as interchangeable just because both
    are unique text/codes.
  • When NO field is a clear, specific match, return "template": null with a
    one-line reason — this concept legitimately does not apply to this program's
    template, and forcing it onto the nearest lookalike column is WORSE than
    leaving it unmapped for human review.
"""

    forced_block = ""
    if forced_field:
        forced_block = f"""
USER-DIRECTED FIELD OVERRIDE (HIGHEST PRIORITY — overrides field-search rules):
A human reviewer has MANUALLY selected the Output-Template field "{forced_field}"
as the column these intents must target. Treat "{forced_field}" as the intent's
subject field:
  • Bind the template's PRIMARY parameter (the limit/value/checked column —
    e.g. `field` for max_limit/min_limit/range_check/value_in_set) to
    "{forced_field}".
  • You MAY still use OTHER columns from the field list for `scope` filters and
    for key/`group_by` parameters when the template needs them.
  • Do NOT return "template": null on the grounds that no subject field exists —
    "{forced_field}" IS the chosen subject field. Only return null if the intent
    is genuinely not expressible by ANY template even with this field, and give a
    short reason.
"""

    # Multi-field selection: the reviewer chose SEVERAL columns because the rule
    # spans them (nested carve-out, multi-column conditional, scoped exclusion).
    extra_forced = [f for f in (forced_fields or [])
                    if f and f != forced_field]
    if forced_field and extra_forced:
        _flist = ", ".join(f'"{f}"' for f in [forced_field, *extra_forced])
        forced_block += f"""
MULTI-COLUMN SELECTION — the reviewer selected THESE columns for this rule: {_flist}.
Use them TOGETHER to express the rule; the rule genuinely spans more than one column:
  • Bind the PRIMARY checked value to "{forced_field}".
  • Bind the OTHER selected columns to the rule's `scope`, `condition`/`conditions`,
    `other_field`, or `group_by` — whichever the logic needs (e.g. a country column
    scopes a state exclusion; a paper column + a state column form a two-column
    condition). Follow the reviewer's stated logic (see the clause's REVIEWER NOTE).
  • Prefer a template that can hold ALL the selected columns (conditional_value,
    conditional_all, value_in_set/value_not_in_set with `scope`, cross_field_*) over
    one that drops a column. Do NOT ignore any selected column.
"""

    return f"""SYSTEM:
You are a highly constrained MAPPER for Kavachio. Each input is a rule INTENT already
extracted from a contract (its subject, operator, value and row-scope are fixed).
Your ONLY job is to bind each intent to the data: pick ONE template from the
catalog and fill its parameters using EXACT meaning of Output-Template field names.

TEMPLATE CATALOG (pick exactly one template name per intent):
{_ir_catalog_block()}

OUTPUT TEMPLATE FIELDS — every field/scope value MUST be one of these exact
names. Never invent or paraphrase a column name:
{fields_block}

VALID FIELD NAMES: {json.dumps(field_names)}
MAPPING RULES:
- USE THE DICTIONARY. When a field shows "means: …" (its documented definition
  from the template's data dictionary), bind by that MEANING, not by a shared
  word in the name. A fee/amount intent must NOT bind to a field whose meaning is
  an identity/name/address/code (e.g. an intent about a "brokerage fee" must not
  bind to a field meaning "Name of agent of record"). If NO field's meaning fits
  the intent's subject, return "template": null — never force a lexical match.
- STRUCTURAL KEYS / IDENTIFIERS ARE NOT VALUE TARGETS. A column whose documented
  MEANING is an identifier, record number, sequence / counter, or a unit / line
  KEY (e.g. one documented as a record id, a policy-detail sequence, or a unit
  number such as one that says "use 0 for policy level") carries no business
  QUANTITY or CATEGORY to test. NEVER bind a limit / amount / percentage / share /
  date / enum value onto such a key column. A column's own documented MEANING and
  SAMPLE values — not its name and not any secondary label — decide what it can
  hold; if the ONLY candidate for the intent is a structural key, return
  "template": null with a reason rather than forcing the value onto it. (E.g. a
  "Percentage of Total Risk / participation share" intent has NO home in a
  template that only has an insured-unit key column — leave it unmapped.)
- USE DOCUMENTED ALLOWED VALUES. When the chosen field shows "allowed values:
  [...]" (its documented code set), those are the ONLY valid values. For
  value_in_set / value_not_in_set, take `allowed`/`excluded` from the CONTRACT but
  express them in the field's documented codes when the contract's wording maps to
  one (e.g. contract says "Renewal Business" and the field's codes are
  NB/RB/EN → use "RB"). Never invent a value outside the documented set.
- PROHIBITION → WHICH DOCUMENTED CODES (value_not_in_set on a CODED field). When a
  prohibition / not_in_set intent maps to a field that has a documented CODE SET
  (its "means:" / "allowed values" text pairs each code with a label, e.g.
  "1 - Annual  2 - Semi-Annual  3 - Quarterly  4 - Monthly"), READ THE LABELS and
  exclude ONLY the codes whose label denotes the prohibited practice. Three hard
  rules:
    • EMIT THE CODE, NOT THE LABEL — put the code token (the left-hand side, e.g.
      "2"), never the label ("Semi-Annual"), into `excluded`/`variation_values`. The
      BDX cells hold the CODES, so a label would match nothing and silently flag
      ZERO rows.
    • KEEP THE COMPLIANT BASELINE — NEVER exclude the code that denotes the
      ABSENCE / neutral / none / single / full / zero case; that state is COMPLIANT,
      not a violation (e.g. for "no installment plans", a single full / annual
      payment is NOT an installment plan, so its code stays ALLOWED; only the
      multi-payment codes are excluded).
    • NEVER EXCLUDE THE ENTIRE CODE SET — a value_not_in_set listing every
      documented code flags every row and is always wrong; at least the compliant
      baseline must remain allowed.
  If the labels do not let you tell which codes denote the prohibited practice,
  return "template": null with a reason rather than excluding all codes.
- Choose the template whose NAME matches the intent's operator/polarity:
    max → max_limit ; min → min_limit ;
    range → ONE range_check with BOTH bounds (params: field, min, max) — take min
      and max from the intent's value {{"min":A,"max":B}}; never split into two rules ;
    equals (a NUMERIC value) → range_check with min == max == the number ;
    equals (a TEXT value) → value_in_set with a single allowed value ;
    in_set → value_in_set ; not_in_set → value_not_in_set ;
    required → required_field ; pattern → pattern_check ;
    date_relation → date_relation ;
    date_bound → date_bound (params: field, op, date) — a date column compared to
      a FIXED contract date. Take `op` and `date` from the intent's value object
      {{"op": "...", "date": "YYYY-MM-DD"}}. Bind `field` to the POLICY's own date
      column that matches the intent subject: a lower bound (op ">=", from a
      program inception/effective date) → the policy INCEPTION / effective-date
      column; an upper bound (op "<=", from a program expiration/termination date)
      → the policy EXPIRY / expiration-date column. Use date_bound (NOT
      date_relation) whenever the RHS is a constant date rather than another
      column ;
    duration_max → period_duration (params: start_field, end_field, unit, max) ;
    duration_min → period_duration (params: start_field, end_field, unit, min) ;
    duration_range → ONE period_duration with BOTH min AND max (params:
      start_field, end_field, unit, min, max) — e.g. min 12 & max 18 →
      {{"start_field":..., "end_field":..., "unit":"month", "min":12, "max":18}} —
      choosing the date PAIR: a template often carries SEVERAL inception/expiry
      date pairs at different scopes (policy/risk, program, transaction,
      reporting, reinsurance). Bind start_field/end_field to the pair whose
      SCOPE the clause is actually about: a "Policy Period" clause measures the
      POLICY'S/RISK'S own inception→expiry columns — NOT the program's,
      transaction's, reporting-window's or reinsurance dates. Pick a program/
      transaction/reporting/reinsurance date pair ONLY when the clause itself
      names that scope. Never mix scopes (e.g. a policy inception with a
      transaction expiry) ;
    aggregate_max → aggregate_cap ; cross_field_math → cross_field_math ;
    cross_field_compare → cross_field_compare ;
    cross_field_or_value → cross_field_or_value ;
    unique → uniqueness ; conditional_required → conditional_required ;
    invariant → aggregate_cap (params: {{"aggregation":"distinct_count",
      "field":"<the column whose value must NOT change>", "group_by":["<the
      identifier column that defines one entity>"], "max":1}}).
- invariant params — the two columns have FIXED, NON-INTERCHANGEABLE roles:
  `field` is the value the intent's SUBJECT names (the one that must stay the
  same, e.g. the policy effective-date column) and `group_by[0]` is the entity
  from the intent value's "per" (e.g. the policy-number column). This compiles to
  "more than one DISTINCT `field` within the same `group_by`", which is the only
  correct shape: it flags a policy whose effective date really does move and
  ignores a policy that simply has many transaction rows carrying the SAME date.
  NEVER map an `invariant` intent to `uniqueness` — a uniqueness rule on
  ["<policy number>", "<effective date>"] flags every policy with more than one
  row, i.e. every ordinary multi-transaction policy, and finds no real violation.
  Both `field` and `group_by[0]` MUST be in VALID FIELD NAMES; if either is
  missing, return "template": null.
- cross_field_math params: {{"result_field":"<col holding the result>",
  "left_field":"<col>", "operator":"+|-|*|/", "right_field":"<col>",
  "tolerance_pct":<number>}} — for "A = B <op> C" identities.
  A PERCENTAGE OPERAND IS NOT A REASON TO DECLINE. When an operand column stores
  a rate on the 0-100 scale (a "23.5" meaning 23.5%, not 0.235), set
  "left_is_percent"/"right_is_percent": true and the operand is divided by 100
  before the arithmetic. When an operand is used as the REMAINDER after a rate is
  taken off a base (a premium net of a ceding commission is base * (1 - rate)),
  set "left_complement"/"right_complement": true. Decide the scale from the
  column's SAMPLE VALUES, not its name: samples spanning 0-100 are a percent
  operand, samples within 0-1 are already a fraction and need no flag.
  So "ceded premium = gross written premium x ceded percentage" maps to
  {{"result_field":"<ceded premium col>", "left_field":"<gross premium col>",
  "operator":"*", "right_field":"<ceded % col>", "right_is_percent":true}} —
  return "template": null ONLY when a required COLUMN is genuinely absent from
  VALID FIELD NAMES, never because of an operand's scale.
- cross_field_compare params: {{"field": "<the field>", "op": "<=|>=|<|>|=|!=",
  "other_field": "<the compared field>", "operator": "*", "factor": <number>}}.
  "<fee A> >= <P>% of <base B>" → {{"field":"<fee A col>", "op":">=",
  "other_field":"<base B col>", "operator":"*", "factor":<P/100>}}.
  For a direct A<=B (no percentage), omit operator/factor.
  Both `field` and `other_field` MUST be in VALID FIELD NAMES; if either is
  missing, return "template": null.
- cross_field_or_value params: {{"field": "<the checked field>", "op": ">=|<=|<|>|=|!=",
  "other_field": "<the compared field>", "operator": "*", "factor": <number>,
  "value": <constant>, "bound": "greater"|"lesser"}}. Take op/operator/factor/value/bound
  from the intent's value OBJECT; bind `field` to the checked field and `other_field`
  to the column for the intent's other_field. "Fronting Fee >= the greater of 12.5%
  of GWP or $15,000" → {{"field":"<fronting-fee col>","op":">=","other_field":"<GWP col>",
  "operator":"*","factor":0.125,"value":15000,"bound":"greater"}}. This is ONE rule for
  a "greater/lesser of a %-of-field or a flat amount" — never split it into
  cross_field_compare + min/max. Both `field` and `other_field` MUST be in VALID FIELD
  NAMES; if either is missing, return "template": null.
- BASE FIELD (`other_field`) — choose it by how the CONTRACT qualifies the BASE,
  NOT by the checked field's party. For "P% of <base>" (cross_field_compare /
  cross_field_or_value), an UNQUALIFIED base — "gross written premium", "written
  premium", "premium", "GWP", "limit" with no party/share word — is the WHOLE /
  TOTAL / 100% column (e.g. "100% Gross Written Premium"), NOT a party-share column
  (e.g. "<Party> Gross Written Premium $" / "<Party> … Net …"). Do NOT propagate the
  checked field's own party qualifier onto the base: a fee that belongs to a party
  ("Fronting Fee to <Party>") is still computed on the TOTAL premium unless the
  contract explicitly scopes the BASE to that party ("the Company's gross written
  premium", "<Party>'s share of premium"). Only then bind the base to that party's
  share column.
- SAME SUBJECT, SAME LEVEL — for a PLAIN, UNSCALED two-column comparison (a
  cross_field_compare with NO operator/factor: "A <= B", "A >= B"). Such a
  comparison only says something when BOTH columns report the SAME underlying
  amount and differ ONLY in the measure being compared (what has been PAID vs what
  has been INCURRED vs what is RESERVED on the same claim; the written vs the
  earned figure of the same premium). Two hard rules:
  • NEVER PUT A ROLLED-UP TOTAL ON ONE SIDE AND A SINGLE COMPONENT ON THE OTHER.
    A bordereau routinely splits one amount into COMPONENT columns — a separate
    paid / reserve / incurred trio per component (indemnity, medical, defence and
    other expense, …) — and adds a rolled-up total alongside them. The total
    INCLUDES what the component columns omit, so comparing the total against one
    component flags every row whose other components are non-zero and catches no
    real defect. Bind both operands at the SAME level: total vs total, or the SAME
    component's own two measures against each other.
  • A "TOTAL <measure>" SUBJECT IS NOT ANY ONE OF THE COMPONENTS. When the
    template carries SEVERAL columns of that measure — one per component — and no
    column that IS their total, the subject has no column at that level here. Do
    not elect one component to stand for the total: the choice is arbitrary and
    every other component silently vanishes from the comparison. Take the FIRST
    option below that this template actually supports:
      1. BOTH measures at the TOTAL level (a column that IS the total on each
         side) — always preferred when the template has them;
      2. else BOTH measures of ONE component (that component's own two columns,
         e.g. its incurred against its paid). The check then covers that
         component only, which is a genuine and honest partial check — the rule
         states the two columns it compares, and a breach it does flag is real;
      3. else "template": null with a reason naming the missing column.
    NEVER mix levels between the two sides — that is option 0 and it does not
    exist.
  DECISIVE TEST before emitting a plain two-column comparison: name the ONE amount
  both columns report. If you cannot — because one side rolls up several
  components the other side excludes, or because the two columns simply report
  different things — the comparison is wrong however plausible the two names look.
  This axis does NOT apply to a SCALED comparison ("A >= P% of B"), where the two
  columns are deliberately different quantities (see BASE FIELD above).
- Match the intent's `subject` to the BEST matching field in VALID FIELD NAMES by
  MEANING (e.g. a "per-occurrence limit" subject → the occurrence-limit column; a
  "domicile state / territory" subject → the state/territory column; a "policy
  duration" subject → the inception + expiry date columns). Match on what the
  column MEANS, not on exact words. Map to the closest field even if the wording
  differs — only refuse when NO field can represent the subject.
- *** WHAT SAMPLE VALUES ARE FOR — THEY CHOOSE THE COLUMN, THEY NEVER VETO THE
  VALUE. *** This is the single most misapplied rule below, so it is stated once,
  up front, and it OVERRIDES every sample-matching instruction that follows:
    • CHOOSING THE COLUMN — samples are evidence. Use them to tell two
      similar-looking columns apart (which one holds class names, which holds a
      status code; which holds dollars, which holds a share).
    • CHOOSING THE VALUE — samples are NOT evidence and carry NO veto. The
      allowed / excluded / scope values come from the CONTRACT. The sample rows
      are a handful of illustrative cells from ONE bordereau, so a value the
      contract authorises or forbids very often does NOT appear among them — and
      when it appears in a real row LATER, flagging it is precisely the rule's
      job. "The value is not found in the sample values", "cannot be grounded
      from sample data", "the samples do not contain this class / state /
      carrier" are NOT valid reasons to refuse: they describe today's data, not
      the column's meaning. NEVER return "template": null for that reason, and
      NEVER drop a value, a scope, or a whole rule for it.
  THE ONLY sample-based refusal that is valid is about MEANING: the column could
  not hold a value of this KIND at all (its cells are dates and the value is a
  class name; its cells are dollars and the value is a state). If the column
  COULD hold the value, bind it and emit the rule.
- MATCH ON THE FIELD'S SAMPLE VALUES, NOT JUST ITS NAME. The chosen field's
  samples must be the SAME CATEGORY as the value the clause constrains. A line /
  class / coverage / program descriptor belongs in a column whose samples are
  class/program names — NOT in a column whose samples are a different fixed set
  (e.g. a two- or three-value status/type code) that merely shares a word with the
  clause's title. When two field NAMES both look plausible, pick the one whose
  SAMPLE VALUES are the same kind of thing as the clause's value.
- EMPTY COLUMNS — map by NAME/MEANING. When the best-matching column has NO sample
  values (it is empty in this template), you CANNOT use samples: bind by the
  column's NAME and MEANING and STILL emit the rule. NEVER skip a correct-by-name
  column just because it is empty, and NEVER prefer a differently-named column only
  because it happens to carry sample data. E.g. a clause about the surplus-lines
  filing / wholesale broker maps to the column NAMED for that role (e.g. "Surplus
  Lines Filing Broker") even if it is blank, not to a generic/adjacent broker
  column that merely has data.
- GEOGRAPHIC LEVEL — never bind a value to a column of a DIFFERENT geographic
  level. A COUNTRY / nation name (e.g. "United States of America") must NOT be
  placed in a STATE / province column — that column's cells hold states, so a
  country value could never match and the rule would be dead. When a territory
  clause reads "home state within <country> … excluding <sub-regions>" and the
  ONLY available column is state-level (there is no country column), DROP the
  country inclusion (return "template": null for that inclusion intent) and keep
  ONLY the exclusion of the named sub-regions on the state column. A genuine
  state-level value (e.g. "District of Columbia") is fine to keep.
- NESTED CARVE-OUT (a not_in_set intent whose sub-regions live in a DIFFERENT,
  FINER column than the allowed country, e.g. "United States of America excluding
  Puerto Rico, US Virgin Islands, US Territories and Possessions"). The sub-regions
  are excluded ONLY WITHIN that country, so map it to **value_not_in_set on the
  finer state/territory column WITH a `scope` on the country column**:
    {{"field":"<state/territory column>",
      "excluded":["Puerto Rico","US Virgin Islands","US Territories and Possessions"],
      "scope":{{"<country column>":"United States of America"}}}}
  This compiles to "flag rows where country = <country> AND state ∈ excluded" — a
  nested exclusion, not a global one. Bind the country to the country column and the
  sub-regions to the state column — NEVER put the sub-regions on the country field.
  If the intent already carries a scope like "only when country is <X>", honor it as
  that country-column scope. If the template has NO country column, drop the scope
  and emit the plain value_not_in_set on the state column (per GEOGRAPHIC LEVEL).
- PREFER THE MOST SPECIFIC FIELD. When several columns could match the subject,
  choose the one whose NAME shares the MOST words with the subject AND carries its
  qualifiers — not a generic or merely adjacent column. E.g. a
  "<qualifier> broker" subject (a specific kind of broker) → the column that keeps
  that qualifier, NOT a bare generic "broker" column and NOT an unrelated code
  column that merely shares a word. A "<qualifier> broker / agent / carrier /
  state" subject maps to the column that keeps the <qualifier>, never to a bare or
  differently-qualified one.
- PRODUCING / DISTRIBUTION BROKER — a clause that restricts WHO produces, places,
  files or distributes the business ("produced exclusively to/through <X> brokers",
  "placed via <X>", "distributed by <X>", "filed by <X>") constrains the reported
  PRODUCING / FILING / SURPLUS-LINES broker field → bind it to the broker column
  that names that role (e.g. "Surplus Lines Filing Broker", else the producing /
  wholesale broker column, else the generic "Broker") and emit value_in_set with
  the named broker as the allowed value. Do NOT refuse on the grounds that the
  named broker reads like a "group / category / type rather than a specific name"
  — the reported broker cell WILL hold that value, and matching is fuzzy
  downstream. Only return null if the template has NO broker/producer column.
- DO NOT bind a placement / regulatory TYPE to an unrelated coded column. A
  placement / regulatory descriptor (e.g. "surplus lines", "admitted vs
  non-admitted", "E&S") describes HOW a policy is placed — it does NOT belong on a
  layer column (primary-vs-excess), a direct-vs-reinsurance column, or any binary
  column that merely shares a word. If NO column clearly represents that placement
  type, return "template": null with a reason — never force it onto an unrelated
  coded column.
- ORGANIZATION vs PERSON. When the contract names an ORGANIZATION / company (a
  carrier, reinsurer, broker, agency, insured business, etc.), bind it ONLY to a
  column that represents an organization / company — a company or entity NAME
  column. Do NOT bind it to a column that holds an INDIVIDUAL PERSON's name (a
  first/last name, a named underwriter / signatory / contact) unless a mapping that
  ties organizations to those people exists in the template. If NO suitable
  company-level column exists, return "template": null with a reason (→ manual
  review) rather than generate an incorrect SQL rule against a person column.
- DIRECT CARRIER vs REINSURER are DIFFERENT PARTIES — never cross them. A subject
  about the REINSURER / ceding / facultative / retrocession side (e.g. an
  "approved reinsurers/papers" list, the reinsurer's name, a reinsurance
  commission/limit) belongs ONLY on a column that reports THAT reinsurance party
  (its name/identity, or a "Reinsurance …" metric column). The DIRECT issuing
  carrier / "paper" a policy is written on is a SEPARATE column whose own values
  are the CEDENT's own entities (e.g. a "Legal Entity (… vs … Paper)" column whose
  samples are the cedent's paper names). NEVER bind an approved-REINSURER / reinsurance-
  paper NAME set to that direct-carrier column (its samples are the cedent's papers,
  so EVERY real policy row would falsely flag). If the template has NO column that
  reports the reinsurer's own name/identity, return "template": null with a reason
  — an approved-reinsurer list with no reinsurer-name column is NOT checkable on
  this BDX; record it for review rather than forcing it onto the direct-carrier or
  any adjacent reinsurance-metric (date/%/limit) column. (Symmetrically, a DIRECT
  issuing-paper restriction must NOT bind to a reinsurance column.)
- MATCH THE VALUE KIND shown for each field: a money/$ amount → a `money` field
  (never a `fraction`/`percentage` one); a share/% → a `fraction`/`percentage`
  field (never `money`); a date/duration → a `date` field; a category/code/name →
  a `text/code` field. Among similar-named fields pick the one whose kind AND
  samples match the intent (a dollar cap → the "$"/limit column, not the "%"
  column). If no field of the required kind exists, return "template": null.
- DATE ROLE — WHEN IT TOOK EFFECT vs WHEN IT WAS RECORDED (refines the kind rule
  above: "a date → a date field" is not enough, because a template carries TWO
  KINDS of date column and they are NOT interchangeable):
  • AN EFFECTIVE DATE says when cover — or a CHANGE to it — starts or ends ON THE
    RISK: an inception / effective date, an expiration / expiry date, and the
    effective date of the transaction itself (the date an endorsement,
    cancellation, change, reinstatement or premium movement TAKES EFFECT). These
    sit INSIDE the policy / coverage period they belong to.
  • A RECORD-KEEPING DATE says when the entry was MADE or REPORTED: booked,
    entered, keyed, processed, accounted, invoiced, collected, settled, as-of,
    statement / reporting-period / bordereau-month dates. These follow the
    REPORTING calendar, not the risk. They are routinely AFTER the policy has
    expired (a cancellation, audit, reversal or late endorsement is recorded after
    the fact) and BEFORE it incepts (business bound and keyed in advance) — that
    is normal bookkeeping, not a breach.
  An intent that places a date INSIDE a period — "must not fall after the
  expiration", "must be on or after inception", "must sit within the policy /
  coverage period" — MUST bind to an EFFECTIVE date on BOTH sides. NEVER bind such
  a bound to a record-keeping date: EVERY cancellation, audit and late endorsement
  in the file is recorded after the period it belongs to, so the rule flags a pile
  of ordinary, compliant rows and catches no real defect. DECISIVE TEST: if the
  SAME transaction had been keyed a month later, would this column's value change?
  YES → it is a record-keeping date, so it cannot carry an in-period bound; NO (the
  value is fixed by the event on the risk) → it is an effective date. Worked
  example of the difference: a cancellation's EFFECTIVE date IS the policy's
  (restated) expiration date, so it can NEVER be after it — that is why the bound
  holds; the date that same cancellation was KEYED is whatever day the operator
  processed it, days or weeks later. When BOTH kinds exist, choose the column that
  names the EVENT taking effect over a bare booking, transaction-entry, accounting
  or reporting date whose name only says WHEN IT WAS RECORDED.
  BIND IT — DO NOT REFUSE. A column that reports the date an endorsement,
  cancellation or change TAKES EFFECT **is** the transaction effective date, even
  when its name lists the transaction types it covers (those types ARE the
  transactions that change a policy) and even when its name does not contain the
  word "transaction". Bind the intent to it. Return "template": null with a reason
  (→ review) ONLY when the template has NO effective-type date column at all for
  the intent's subject — never as a way to avoid choosing between two date
  columns, and never in preference to a record-keeping date that would be wrong.
  Conversely, an intent that is genuinely ABOUT the reporting calendar ("reported
  within N days of the month end") DOES belong on the record-keeping date — match
  the intent's own subject.
- DATE BOUNDS ARE INCLUSIVE UNLESS THE WORDING EXCLUDES THE BOUNDARY. "not
  after" / "no later than" / "on or before" → op "<="; "not before" / "no earlier
  than" / "on or after" → op ">=". Use the strict "<" / ">" ONLY when the intent
  says the boundary day itself is not allowed ("strictly before", "prior to").
  The last day of a period is part of that period: a transaction effective on the
  expiration date, or on the inception date, is COMPLIANT.
- USE THE FIELD'S SCALE: if its samples are fractions (0–1) and the clause says a
  percentage ("100%"), emit the fraction (1.0), not 100 — else the rule never fires.
- ENUM VALUES COME FROM THE CONTRACT — NOT FROM THE TEMPLATE. For value_in_set /
  value_not_in_set, the `allowed`/`excluded` values (and the variation_values) MUST
  be the actual names/wording the CONTRACT authorizes or forbids — the rule EXISTS
  to flag rows that deviate from the contract. The output template is only a
  STRUCTURAL sample: its column HEADERS and SAMPLE cell values are illustrative, NOT
  real data. NEVER copy a template sample value, and NEVER copy a token taken from a
  column header, into `allowed`/`excluded`/`variation_values` — e.g. for a column
  named "Legal Entity (Specialty vs Offshore Paper)", do NOT use "Specialty" or
  "Offshore" as the allowed value; use the company names the CONTRACT actually states
  (e.g. "Acme Specialty Insurance Company, Inc."). Do NOT replace a contract name
  with a sample value to "match the data" — the real data spelling is matched
  separately at BDX time (fuzzy + reconciliation), so you never need to guess it
  here. If the contract's value is NOT among the samples, STILL emit the rule with
  the contract's value — do NOT return null just because today's sample data doesn't
  already match it. The sample rows may be non-compliant (e.g. a program/class
  column holding a value the contract does NOT authorize) and catching exactly that
  is the rule's job. As long as a field can REPRESENT the subject (right
  meaning/kind), bind to it and emit the rule. Only return "template": null when NO
  field can represent the subject at all.
- QUALITATIVE RISK-CHARACTERISTIC EXCLUSIONS ARE NOT CATEGORY VALUES (a specific
  case of the "no field can represent the subject" rule above). When a not_in_set
  clause forbids a risk by a QUALITATIVE CHARACTERISTIC / CONDITION / QUALITY of the
  exposure — an underwriting JUDGMENT about the nature of the risk (e.g. moral
  hazard, a distressed / poor condition, the transient nature of the occupants) that
  is merely a SUB-QUALITY of an otherwise-AUTHORIZED segment — no bordereau column
  REPORTS that judgment as a value. A class / segment / category column reports WHICH
  category a policy is (its samples are discrete category labels); it does NOT report
  the QUALITY of the risk within a category. Such an exclusion shares ONLY the
  generic segment head-noun with that column, and that head-noun is itself an
  AUTHORIZED category — so binding it there is either DEAD (the qualitative phrase
  never appears in any cell) or WRONG (it would flag the authorized category itself).
  Return "template": null with a reason (route to review). DECISIVE TEST: could the
  excluded item ever appear as a LITERAL CELL VALUE in the chosen column? A discrete,
  NAMEABLE category (a specific class code, carrier, state, program) could — even as
  non-compliant data — so you STILL emit it (per the rule above); a quality/judgment
  that no cell would ever spell out could NOT, so the column cannot represent it →
  null.
- POLARITY — REQUIRED value vs FORBIDDEN value (value_in_set / value_not_in_set).
  Pick the template by WHICH rows must be flagged, never by the clause's surface
  wording:
  • The clause REQUIRES/mandates a value ("Administrator to utilize X paper for
    all policies", "must be written on X", "coverage must be placed with X") and
    the referral/violation event is a DEVIATION from it → value_in_set with
    allowed=[X]: the rule flags rows NOT matching X. NEVER emit value_not_in_set
    excluding X for such a clause — that flags every COMPLIANT row and passes
    every actual deviation.
  • The clause FORBIDS a value, or makes its PRESENCE the referral event ("All
    SUPER Specialty policies require referral", "no policies in Alaska/Hawaii")
    → value_not_in_set with excluded=[X]: the rule flags rows MATCHING X.
  • An "except <subset>" carve-out on a required-value clause ("for all policies
    except home state California") is a SCOPE on the value_in_set rule — it is
    NOT a reason to invert the template.
  SELF-CHECK before emitting an enum rule: state in words which row gets
  flagged. If your own rule_description/error_message says the flagged row
  "deviates from / is not / differs from X", the template MUST be value_in_set
  (allowed=[X]); if it says the flagged row "is / uses / matches X", it MUST be
  value_not_in_set (excluded=[X]).
- BINARY Y/N FLAG FIELDS — a common trap for the POLARITY rule above. When the
  bound field is a two-valued flag column (its header or samples show it is
  Yes/No, Y/N, True/False, 1/0 — e.g. "Faculative Re(Y/N)", "Reporter Y/N") and
  the clause makes the flag's AFFIRMATIVE state the referral/violation event
  ("involves X", "any policy WITH X", "policies that use X", "X applies", "X
  placements"), put the AFFIRMATIVE token itself (whichever spelling the
  field's own samples use — Yes/Y/True/1) in excluded=[...] (or allowed=[...]).
  Do NOT encode it as the negation of the OTHER branch (e.g. excluded=["No"]
  reasoned as "flag when it is not No") — value_not_in_set flags rows that
  MATCH the excluded token, so excluded=["No"] flags every COMPLIANT row
  instead of the row that actually needs a referral, which is the exact
  opposite of the clause's intent. Re-run the SELF-CHECK above with the
  AFFIRMATIVE word itself, not its negation: the excluded/allowed token must be
  the same word you would say aloud completing "this row triggers the clause
  because the flag is ___".
- VARIATION VALUES (enum templates only — value_in_set / value_not_in_set). ALSO
  emit a `variation_values` array: meaningful surface-form variations of EACH
  allowed/excluded value, so the check still matches when the BDX spells the value
  differently (abbreviations, dropped-suffix forms, distinctive short forms) — e.g.
  "Acme Specialty Insurance Company Inc." → "Acme Specialty", "Specialty",
  "Specialty Insurance Company Inc". BE GENEROUS — real BDX spellings vary a lot,
  so for EACH value produce AT LEAST 3 genuine spellings (commonly 3–6, more when
  the name is long). 3 is a HARD MINIMUM — never emit fewer — and all 3 must be
  MEANINGFUL: each one a form a real BDX could actually hold for that entity, never
  filler and never a near-duplicate of another variation (case-only or
  punctuation-only rewrites such as "ACME SPECIALTY" or "Acme Specialty." do NOT
  count toward the 3 — those already match automatically). If a value genuinely has
  fewer than 3 meaningful spellings, emit every meaningful one you can and say so in
  `reason` — do NOT pad the list to reach 3. Those spellings should cover at least:
  (a) the FULL legal name exactly as written;
  (b) the name with the corporate-form suffix dropped (e.g. "… Insurance Company,
  Inc." → "… Insurance Company"); (c) recognizable abbreviations / accepted short
  forms. When a short reference clearly stands for a fuller legal name you can tell
  from the wording (e.g. "Northwind Specialty" is short for "Northwind Specialty Insurance
  Company, Inc."), ALSO include those fuller forms. RULES:
    • Every variation must be a genuine spelling of the SAME entity as ONE listed
      value (it must trace back to exactly one allowed/excluded value).
    • Each variation MUST keep the DISTINGUISHING word that names that entity apart
      from the others (e.g. "Specialty", "Offshore"). NEVER emit a bare COMMON STEM
      shared by several allowed values — e.g. when the companies are "Acme
      Specialty…" and "Acme Insurance Company, Limited", do NOT emit just "Acme"
      or "Acme Insurance" (they fit BOTH, so they identify neither). Prefer MANY
      SPECIFIC spellings over few — every one must stay specific to this entity;
      do NOT pad the list with generic fragments to reach a count.
    • NEVER emit a variation that is, on its own, just a LEGAL-ENTITY / CORPORATE-FORM
      word — the company-type designator (Limited, Ltd, Inc, Incorporated, LLC, Corp,
      Corporation, Company, Co, Holdings, Group, and their equivalents in any
      jurisdiction). These are shared by countless unrelated companies, so alone they
      identify no specific entity. Keep the entity's distinctive name word instead, or
      pair the form word with the family name. Example, for "Acme Insurance Company,
      Limited": emit "Acme Limited" ✓ — but NOT a bare "Limited" ✗. (By contrast
      "Specialty" ✓ is a distinctive name word, not a corporate-form word.)
    • Each variation must be a name a reader would RECOGNIZE as that company — a
      real abbreviation or accepted short form, NOT an arbitrary truncation that
      crops identifying words (e.g. "Specialty" ✓, but a bare "Acme" or "Company" ✗).
    • NEVER invent unrelated values; NEVER list a variation of a DIFFERENT allowed
      entity. Omit the key for non-enum templates.
    • MATCH THE TARGET COLUMN'S OWN SHORT FORM. When the bound column's NAME or
      SAMPLE VALUES show it stores a SHORT token rather than a full legal name —
      e.g. a "Legal Entity (Specialty vs Cayman Paper)" column whose cells are just
      "Specialty" / "Cayman", or any "<A> vs <B> Paper/Type" column — and the
      contract's value contains that token, you MUST include the BARE short token
      (e.g. "Specialty") in variation_values. That token is literally what the cell
      holds, so without it the fuzzy match never fires. This is the ONE case where a
      bare distinguishing word is required: it is the column's actual value, not a
      generic fragment.
- ENUM VALUES MUST BE LITERAL CELL VALUES. An allowed/excluded entry that merely
  REFERS to a group the contract defines elsewhere ("the <term>s") is a
  cross-reference, not a value — no cell will ever hold it. Resolve the
  reference to the member names the document itself enumerates (its definitions
  or summary tables, provided with the clause); if they are nowhere enumerated,
  return "template": null naming the unresolved term instead of shipping the
  phrase as an allowed value.
- SCOPE — distinguish DEFINING from INCIDENTAL:
  • DEFINING scope: when the constraint applies ONLY to the subset of rows
    identified by some attribute, that scope is ESSENTIAL — without it the rule
    would wrongly flag EVERY row. Keep it and bind it to the column that identifies
    those rows. If no available column can identify them, the rule cannot be
    applied — return "template": null with a reason naming the missing identifying
    column. NEVER drop a defining scope and emit a rule that applies to all rows.
  • INCIDENTAL scope: when the data is already limited to that subset, the filter
    is redundant — omit it. Never invent a filter that matches no row.
  • TABLE-DEFINED cohorts: when the clause is a label→value table, the rows
    whose values NAME what the table is about ARE the DEFINING scope for every
    rule built from its constraint rows — judged from the table's own content,
    no particular label is special, and sibling tables in the same contract
    state different values for other cohorts. Bind that scope to whichever
    output column identifies the named cohort, with the identifying value the
    table itself states. If no available column can identify it, treat it
    exactly as a dropped defining scope: return "template": null naming the
    missing identifying column — never emit the constraint as an all-rows
    rule. The INCIDENTAL test above still applies: when the whole upload is
    already that one cohort's data, the filter is redundant and may be
    omitted.
  • OR ACROSS COLUMNS (`any_of`): when the SAME subset can be identified by EITHER
    of two columns — e.g. "this $25M limit applies when the Reinsurer NAME is one of
    these OR the Reinsurer PAPER is one of these" (the reinsurer may be reported in
    either column) — put an `any_of` key in `scope` whose value is a dict of
    per-column filters, and they are OR-ed:
      "scope": {{"any_of": {{
          "Reinsurer Name":  {{"allowed": [<names>],  "variation_values": {{...}}}},
          "Reinsurer Paper": {{"allowed": [<papers>], "variation_values": {{...}}}}
      }}}}
    Other top-level scope keys are still AND-ed with the OR group. Use `any_of`
    whenever the reviewer/clause says the filter matches on one column OR another —
    do NOT return "template": null for that (OR scope IS expressible now), and do NOT
    silently drop one of the columns.
- A stated limit is a CEILING: use max_limit (max=X) / min_limit (min=X), never
  range_check with min == max.
- SCOPE ONLY WHEN GROUNDED. Add a `scope` filter only if its value appears in
  that field's sample values. Never invent a filter like {{"<Some Column>":
  "<a value present in no row>"}} that matches no row. If the whole BDX is already
  one coverage/program, omit the scope entirely rather than guess one.
- A stated LIMIT is a CEILING/FLOOR: use max_limit (max=X) / min_limit (min=X);
  do NOT use range_check with min == max for a limit. BUT an EXACT REQUIRED
  NUMERIC value that a SINGLE column DIRECTLY reports — a fixed rate / percentage /
  amount each row must equal — IS range_check with min == max == that number (for a
  RATE that is NOT directly reported as its own column, e.g. a commission / fee
  rate, see the RATE-OF-A-BASE rule immediately below instead). NEVER put a numeric
  value in value_in_set (a fuzzy string match on a numeric column flags every row).
- MONETARY RATE = RATIO OF AN AMOUNT TO ITS BASE (do NOT bind it to a coincidental
  "%" column). A clause stating a RATE as a bare "P%" for a MONETARY item that is
  conventionally a PERCENTAGE OF A BASE — a commission, brokerage, tax, or similar
  fee earned on premium — is the RATIO of that item's AMOUNT to its BASE amount, NOT
  a standalone percentage. Choose the target by what the template ACTUALLY reports:
    * If a column DIRECTLY reports THAT rate (its documented MEANING is that rate,
      e.g. a "commission rate %" column) → range_check with min == max == P/100 on it.
    * ELSE if the template reports that item as an AMOUNT ($) column AND the BASE
      amount column exists (the premium / limit the rate is earned on) → emit
      cross_field_compare, NOT range_check: {{"field": "<the item's AMOUNT $ column>",
      "op": "=", "other_field": "<the BASE $ column, e.g. the written-premium
      column>", "operator": "*", "factor": <the rate as a decimal fraction, i.e.
      the intent's numeric value — e.g. 0.265 for 26.5%>}}. Do this EVEN THOUGH the intent
      operator is "equals" — an exact rate over an amount-and-base pair is a
      cross-field identity (amount = base × P%) that range_check on ONE column cannot
      express.
    * ELSE → return "template": null (route to review).
  NEVER bind such a rate to a percentage column whose MEANING is a DIFFERENT ratio
  (e.g. a premium-adequacy "actual vs technical / manual price" percentage) merely
  because it also shows a "%". Match by the column's documented MEANING, not the
  "%" symbol.
  A PARTICIPATION SHARE is the SAME shape — a reinsurer's / participant's P% share
  or signed line, reported per row as a share AMOUNT column, is
  share $ column = base $ column × P. Treat it exactly as above.
- DERIVED ABSOLUTE — a constant that the clause itself computes from a rate and one
  named base ("P% of $B, that is $A"; "a $A share of the $B layer") belongs to THAT
  base and to nothing else. Bind $A with range_check ONLY to a column that reports
  that same base figure. On ANY per-row amount column — a share, premium, reserve
  or loss amount that varies row to row — the checkable requirement is the
  PROPORTION, so emit
    {{"template": "cross_field_compare", "params": {{"field": "<the share $ column>",
      "op": "=", "other_field": "<the per-row BASE $ column the share is taken on>",
      "operator": "*", "factor": <P as a decimal fraction>}}}}
  A per-row column can never equal a whole-contract constant, so a range_check
  there flags EVERY row and detects nothing.
- CHOOSING `other_field` FOR A "×factor" IDENTITY — decide it from the SAMPLE
  VALUES shown with each field, not from the name: pick the candidate base column
  whose samples, multiplied by the factor, actually reproduce the target column's
  samples (e.g. target samples -328.85 / 570.65 with factor 0.05 ⇒ a base whose
  samples are -6577 / 11413). When two candidates both reproduce them, take the one
  the clause's own wording points at — the amount the share is actually taken on
  (a net-of-deductions figure when the clause says the share is of the net/ceded
  liability). If NO candidate reproduces the samples, return "template": null.
- PER-POLICY vs PORTFOLIO. A ceiling on ONE policy's field (e.g. a "general
  aggregate limit of $<X>" or a "per occurrence limit of $<X>") is
  max_limit on that row. Use aggregate_cap ONLY for a TOTAL across many rows
  (sum/count of all policies). A column whose NAME contains "aggregate" (an
  "…Aggregate Limit…" column) is a per-policy value — "aggregate" in its name is
  the insurance term, NOT an instruction to SUM rows. Never SUM a per-policy limit.
- CUMULATIVE limit— when a clause caps the TOTAL of
  a field SUMMED across rows/schedules for the SAME policy or occurrence, use aggregate_cap with:
    aggregation="sum", field=<the share/limit column being summed>,
    group_by=[<the policy/occurrence KEY column, e.g. a policy number>],
    max=<the cap>,
    scope_sheets=[<each schedule the clause names>],
    row_reducer="max"   ← SET THIS for a LIMIT / SUBLIMIT.
  row_reducer decides how duplicate transaction rows are treated. A BDX row is a
  TRANSACTION (endorsement, additional/return premium), so ONE policy has many
  rows. A LIMIT is a per-policy figure REPEATED identically on every one of those
  rows — summing all rows would multiply it by the transaction count. So when the
  summed field is a limit / sublimit / any per-policy value duplicated across a
  policy's rows, set row_reducer="max": the compiler collapses it to ONE value per
  policy per schedule BEFORE summing across schedules. OMIT row_reducer (it
  defaults to plain "sum") ONLY when each row is a genuinely DIFFERENT amount that
  SHOULD add up — a PREMIUM or FEE. Rule of thumb: summing a "…Limit $" column →
  row_reducer="max"; summing a "…Premium"/"…Fee" column → omit it.
  The group_by is REQUIRED here — without it the rule sums the whole book instead
  of per policy (and row_reducer has no policy key to collapse to). scope_sheets is REQUIRED when the clause limits the total to a
  NAMED SUBSET of schedules/sheets (any clause of the form "between <X>, <Y> and
  <Z>" naming specific schedules, sections or sheets) — list those names verbatim;
  without it the sum spans EVERY sheet that has the column, not just the named
  ones. Pick the field that is the Company's OWN share for a "Company"
  cumulative — the participant/party own-share limit-$ column (the one whose name
  carries the party/program prefix or a part/share/net token, e.g. "<party> part of
  Limit $"). A limit column does NOT need the literal word "gross" to BE the gross
  limit: "gross vs net" is before/after reinsurance, NOT a separate column — so an
  own-share "… Limit $" column IS the correct field to sum for a "gross limit"
  cumulative. As long as ANY own-share limit-$ column exists, NEVER answer "no field"
  / leave it unmapped / route to review — bind that column and emit the rule. Only if
  there is truly no limit column at all, apply the validation to the overall
  sheet-level aggregate.
  *** NAMED SCHEDULES THAT ARE ABSENT — STILL EMIT THE RULE, NEVER ROUTE TO REVIEW. ***
  A cumulative clause may name several schedules/sections ("between <X>, <Y> and
  <Z>") while only SOME (or ONE) of them are present as sheets in this template — a
  single-schedule BDX has only that one schedule's sheet. This is EXPECTED and is
  NOT a reason to refuse the mapping. Still emit the aggregate_cap: set scope_sheets to
  the named schedules verbatim (the compiler automatically restricts to whichever
  of them actually exist, falling back to the available sheet) and bind `field` to
  the Company's own share/limit column on the available sheet. Enforcing the cap on
  the schedule(s) you DO have is a valid necessary check (if one schedule alone
  breaches the cap, the cumulative certainly does). Do NOT return an unmapped /
  review verdict merely because some named schedules are missing, or because the
  named programs are not verbatim sheet names — map it to the available sheet.
- *** DECISIVE — PARTICIPANT SHARE vs WHOLE POLICY (applies to ANY metric) ***
  For many metrics (limit, premium, fee, etc.) the template carries TWO columns
  for the SAME number: one for a PARTICIPANT'S OWN share and one for the WHOLE
  policy across all participants. Decide which the clause means from its
  TITLE / subject — never from a word the two columns happen to share:
    * OWN-SHARE — the title/subject scopes the amount to a specific party: it
      names a company / carrier / program / reinsurer, or says "net", "retention",
      "share", "participation", "part", or "our". Bind to the column whose NAME
      carries that same own-share signal (a party/program name, or
      "part" / "net" / "share" / "participation"), matching the $ vs % basis.
      NEVER bind an own-share amount to a "100%" / "Total" / "whole policy" column.
    * WHOLE-POLICY — the title/subject is explicitly about the entire policy /
      full placement ("100%", "total", "whole policy", "full"). Bind to the
      "100%" / "Total" column.
  A word the two columns share ("gross", "limit", "premium") is NOT the deciding
  signal — "gross vs net" means before/after reinsurance, NOT whole-vs-share. If
  still unsure between the two candidates, prefer the column whose sample
  magnitudes are consistent with the stated value (a cap most rows already satisfy).
  Generic pattern: "<party/program> <metric> of $X" → the
  "<party> / …part / share / net … $" column; "100% / total <metric> of $X" →
  the "100% / Total … $" column.
- If the subject genuinely cannot be represented by any VALID FIELD NAME, return
  that intent with "template": null and a one-line "reason" naming the missing
  data. Do NOT force a bad mapping.
- FIELD ALIASES (same-meaning columns under other names). After binding the
  intent's primary `field`, scan the REST of the fields block: when ANOTHER
  column name in VALID FIELD NAMES holds the SAME real-world value as the bound
  field — judge by the header's meaning AND the sample values (e.g. "Policy
  No" or "Assured Reference" holding the same kind of policy identifiers as
  the bound "Policy Number") — ALSO return
  "field_aliases": ["<that equivalent column name>", ...] so the rule can run
  on the sheets that use those spellings too. Each entry MUST be a name from
  VALID FIELD NAMES, different from the bound field. Only report a match you
  are confident is the SAME concept — an occurrence/claim number is NOT a
  policy number, a paid amount is NOT a premium, a tax is NOT a fee. Omit the
  key entirely when there are none.

OUTPUT (strict JSON only — no markdown, no // comments, no trailing commas):
{{
  "results": [
    {{
      "clause_id": number,
      "intent_index": number,
      "template": "<one catalog template name>" | null,
      "params": {{ ...template params using VALID FIELD NAMES...; for value_in_set / value_not_in_set ALSO include "variation_values": [ ...AT LEAST 3 meaningful surface variations of EACH allowed/excluded value... ] }},
      "rule_name": string,
      "rule_description": string,
      "severity": "critical" | "warning" | "info",
      "error_message": string,
      "confidence": number,
      "reason": string,
      "field_aliases": ["<same-meaning column name from VALID FIELD NAMES>", ...]
    }}
  ]
}}
("field_aliases" is OPTIONAL — include it only per the FIELD ALIASES rule above.)

CRITICAL:
- Return exactly one results entry per (clause_id, intent_index) in the input.
- Every field in params MUST be in VALID FIELD NAMES.

USER:
{forced_block}{relaxed_block}{generic_block}
Batch of {len(intent_items)} rule intent(s) to map:

{json.dumps(intent_items, indent=2)}

Map each intent to a template using Output Template field names only."""
