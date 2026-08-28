"""Generate a plain-English Word document describing every AI prompt used in the
Kavachio contract -> rule -> validation pipeline, INCLUDING the exact prompt text
rendered with representative example data (i.e. exactly what is sent to the AI).

Run from backend/python-services:  python ../../scripts/gen_prompts_doc.py
Output: Kavachio_AI_Prompts_Explained.docx (repo root)
"""
import os
import sys

# Make the backend package importable so we can render the REAL prompts.
HERE = os.path.dirname(os.path.abspath(__file__))
# scripts/ now lives at <repo>/backend/Microservice/scripts -> repo root is 3 levels up.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
BACKEND = os.path.join(ROOT, "backend", "python-services")
sys.path.insert(0, BACKEND)

from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT

# ---- render the real prompts with example data -----------------------------
from contract_upload_services.prompt_builder import (
    build_extraction_prompt,
    _reference_documents_block,
    build_rule_intent_prompt_batch,
    build_ir_mapping_prompt_batch,
)
import direct_mapper
from duckdb_validation import _build_prompt as build_sql_prompt

# ---------- example data (RiskSmith / Palms domain) ----------
EX_REFERENCE_DOCS = [{
    "name": "Risksmith Fac Guide - 10.31.2023_Final.docx",
    "text": "ELIGIBLE APPETITE CLASSES: Real Estate: Office, Hotel, Lessors Risk; "
            "Healthcare: Hospitals, Clinics.\n"
            "Excluded Occupancies: Energy: Petroleum Risks, Chemical, Refineries; "
            "Heavy Industrial: Recycling, Mining; Other: Builders Risk, Stock Only.",
}]

EX_SECTION = {
    "section_type": "body",
    "page_start": 1,
    "page_end": 2,
    "text": (
        "d) Maximum Company Program Limit: $25,000,000 gross limit per occurrence "
        "per policy, $0 net limit per occurrence per policy in accordance with "
        "approved Facultative Purchasing Guidelines.\n"
        "g) Excluded Classes of Business: Per Facultative Purchasing Guidelines on "
        "file with the Company."
    ),
}

EX_CLAUSES = [
    {"clause_id": 13135, "clause_type": "limit",
     "title": "Maximum Company Program Limit",
     "text": "d) Maximum Company Program Limit: $25,000,000 gross limit per "
             "occurrence per policy, $0 net limit per occurrence per policy in "
             "accordance with approved Facultative Purchasing Guidelines."},
    {"clause_id": 13136, "clause_type": "exclusion",
     "title": "Excluded Classes of Business",
     "text": "g) Excluded Classes of Business: Per Facultative Purchasing "
             "Guidelines on file with the Company. [Context from Risksmith Fac "
             "Guide: Excluded Occupancies: Energy, Heavy Industrial, Other]"},
]

EX_INTENT_ITEMS = [
    {"clause_id": 13135, "intent_index": 0,
     "subject": "Company program gross limit per occurrence",
     "operator": "max", "value": 25000000, "scope": None,
     "severity": "critical", "is_referral": False,
     "rule_name": "Maximum Company Program Gross Limit",
     "rule_description": "The company's gross share of the limit must not exceed "
                         "$25,000,000 per occurrence per policy.",
     "error_message": "Company gross limit exceeds $25,000,000.",
     "clause_text": "d) Maximum Company Program Limit: $25,000,000 gross limit per "
                    "occurrence per policy."},
]

EX_TEMPLATE_FIELDS = [
    {"name": "Palms part of Limit $",
     "sheet": "Palms Schedule G Current BDX", "sheets": ["Palms Schedule G Current BDX"],
     "samples": ["25000000", "10000000"], "allowed_values": [],
     "description": "The company's own dollar share of the limit",
     "field_format": "money", "required": False, "canonical_field": None},
    {"name": "100% policy Limit",
     "sheet": "Palms Schedule G Current BDX", "sheets": ["Palms Schedule G Current BDX"],
     "samples": ["2000000"], "allowed_values": [],
     "description": "The full (100%) policy limit",
     "field_format": "money", "required": False, "canonical_field": None},
    {"name": "Palms part of Limit %",
     "sheet": "Palms Schedule G Current BDX", "sheets": ["Palms Schedule G Current BDX"],
     "samples": ["100", "50"], "allowed_values": [],
     "description": "The company's own percentage share of the limit",
     "field_format": "percentage", "required": False, "canonical_field": None},
]

EX_RULE = {
    "rule_name": "Maximum Company Program Gross Limit",
    "rule_description": "Company gross share of the limit must not exceed "
                        "$25,000,000 per occurrence.",
    "rule_spec": {"ir": {"template": "max_limit",
                         "params": {"field": "Palms part of Limit $",
                                    "max": 25000000}}},
    "canonical_target": {"output_field": "Palms part of Limit $"},
    "error_message": "Company gross limit exceeds $25,000,000.",
}

EX_TABLES = {
    "Palms Schedule G Current BDX": {
        "columns": ["Risksmith Policy Number", "Palms part of Limit $",
                    "Palms part of Limit %", "100% policy Limit"],
        "samples": {
            "Risksmith Policy Number": ["25-XSP-0094", "25-XSP-0352"],
            "Palms part of Limit $": ["25000000", "30000000"],
            "Palms part of Limit %": ["100", "100"],
            "100% policy Limit": ["2000000", "2000000"],
        },
    }
}

EX_MAP_OUTPUT_COLS = ["Risksmith Policy Number", "Palms part of Limit $",
                      "Palms part of Limit %"]
EX_MAP_INPUT_COLS = ["Policy No", "Company Share Amount", "Company Share Pct"]
EX_MAP_SAMPLES = {
    "Policy No": ["25-XSP-0094", "25-XSP-0352"],
    "Company Share Amount": ["25000000", "10000000"],
    "Company Share Pct": ["100", "50"],
}

# Render the real prompt strings.
PROMPT_0 = direct_mapper._build_prompt(EX_MAP_OUTPUT_COLS, EX_MAP_INPUT_COLS, EX_MAP_SAMPLES)
PROMPT_1 = build_extraction_prompt(EX_SECTION, reference_documents=EX_REFERENCE_DOCS)
PROMPT_1B = _reference_documents_block(EX_REFERENCE_DOCS)
PROMPT_2 = build_rule_intent_prompt_batch(EX_CLAUSES)
PROMPT_3 = build_ir_mapping_prompt_batch(EX_INTENT_ITEMS, EX_TEMPLATE_FIELDS)
PROMPT_5 = build_sql_prompt(EX_RULE, EX_TABLES)

# Real deterministic SQL produced by compile_ir() for the $25M rule (rule 3094),
# one sheet shown for readability (the live query UNION-ALLs across every sheet).
EX_COMPILED_SQL = (
    "SELECT __rowid AS row_id,\n"
    "       'Palms Sch A Current BDX' AS sheet,\n"
    "       'Palms part of Limit $' AS field,\n"
    "       'Palms part of Limit $ exceeds maximum 25000000' AS reason,\n"
    "       \"Palms part of Limit $\" AS actual_value\n"
    "FROM \"Palms Sch A Current BDX\"\n"
    "WHERE TRY_CAST(REPLACE(REPLACE(REPLACE(\"Palms part of Limit $\", ',', ''),\n"
    "               '$', ''), ' ', '') AS DOUBLE) IS NOT NULL\n"
    "  AND TRY_CAST(REPLACE(REPLACE(REPLACE(\"Palms part of Limit $\", ',', ''),\n"
    "               '$', ''), ' ', '') AS DOUBLE) > 25000000\n"
    "UNION ALL\n"
    "  ... (same check repeated for every other schedule sheet) ..."
)

# =====================================================================
# DOCUMENT
# =====================================================================
ACCENT = RGBColor(0x1F, 0x4E, 0x79)
GREY = RGBColor(0x55, 0x55, 0x55)
CODEBG = RGBColor(0x2B, 0x2B, 0x2B)

doc = Document()
normal = doc.styles["Normal"]
normal.font.name = "Calibri"
normal.font.size = Pt(11)


def h1(text):
    p = doc.add_heading(text, level=1); p.runs[0].font.color.rgb = ACCENT; return p


def h2(text):
    p = doc.add_heading(text, level=2); p.runs[0].font.color.rgb = ACCENT; return p


def para(text, italic=False, bold=False, color=None):
    p = doc.add_paragraph(); r = p.add_run(text)
    r.italic = italic; r.bold = bold
    if color is not None:
        r.font.color.rgb = color
    return p


def bullet(text, bold_lead=None):
    p = doc.add_paragraph(style="List Bullet")
    if bold_lead:
        p.add_run(bold_lead).bold = True
    p.add_run(text); return p


def field_table(rows):
    t = doc.add_table(rows=0, cols=2); t.style = "Light Grid Accent 1"
    for label, value in rows:
        cells = t.add_row().cells
        cells[0].paragraphs[0].add_run(label).bold = True
        cells[1].paragraphs[0].add_run(value)
    for row in t.rows:
        row.cells[0].width = Inches(1.6); row.cells[1].width = Inches(4.9)
    doc.add_paragraph(); return t


def code_block(text):
    """Render the EXACT prompt text in a monospace, single-cell shaded box."""
    tbl = doc.add_table(rows=1, cols=1)
    tbl.style = "Table Grid"
    cell = tbl.cell(0, 0)
    # light shading
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    shd = OxmlElement("w:shd"); shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), "F4F6F8")
    cell._tc.get_or_add_tcPr().append(shd)
    first = True
    for line in text.split("\n"):
        p = cell.paragraphs[0] if first else cell.add_paragraph()
        first = False
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.space_before = Pt(0)
        r = p.add_run(line if line != "" else " ")
        r.font.name = "Consolas"
        r.font.size = Pt(7.5)
    doc.add_paragraph()


def prompt_section(title, file_ref, summary_rows, data_note, prompt_text,
                   one_liner):
    h1(title)
    para("Code: " + file_ref, italic=True, color=GREY)
    field_table(summary_rows)
    para("Data filled into this example:", bold=True)
    para(data_note, italic=True, color=GREY)
    para("EXACT PROMPT SENT TO THE AI (instructions + the example data inline):",
         bold=True)
    code_block(prompt_text)
    para("In one line: " + one_liner, italic=True)
    doc.add_page_break()


# ---- title ----
t = doc.add_paragraph(); t.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = t.add_run("Kavachio - AI Prompts Explained"); r.bold = True
r.font.size = Pt(26); r.font.color.rgb = ACCENT
s = doc.add_paragraph(); s.alignment = WD_ALIGN_PARAGRAPH.CENTER
rs = s.add_run("Every prompt in the contract -> rules -> validation pipeline,\n"
               "in plain English, WITH the exact prompt text and the data sent.")
rs.italic = True; rs.font.color.rgb = GREY; rs.font.size = Pt(12)
doc.add_paragraph()
note = doc.add_paragraph(); note.alignment = WD_ALIGN_PARAGRAPH.CENTER
note.add_run("The prompt boxes below are the REAL prompts, rendered with a "
             "representative RiskSmith / Palms example so you can see exactly "
             "what the AI receives.").italic = True
doc.add_page_break()

# ---- big picture ----
h1("1. The Big Picture")
para("Kavachio turns a written insurance contract into automatic checks that run "
     "against the policy spreadsheets (the BDX). Google Gemini (the fast 'Flash' "
     "model) is used at a few specific steps. Each step has its own PROMPT - the "
     "instructions plus some data we send - and the AI returns a structured answer "
     "the next step uses.")
para("Order of the prompts:", bold=True)
bullet(" match the input file's columns to the output template's "
       "columns.", bold_lead="Prompt 0 - Column mapping (setup):")
bullet(" read the contract into clean clauses + program "
       "facts.", bold_lead="Prompt 1 - Extraction:")
bullet(" for each clause, decide is-it-a-rule and what it "
       "checks (plain words).", bold_lead="Prompt 2 - Intent:")
bullet(" attach each check to the correct output column.",
       bold_lead="Prompt 3 - Mapping:")
para("")
para("Important - the SQL check is NOT made by AI:", bold=True)
para("There are only FOUR AI prompts in the live pipeline (0, 1, 2, 3). The "
     "database query that actually finds the failing rows is built by CODE - a "
     "deterministic compiler (compile_ir in rule_compiler.py) - at the moment a "
     "rule is created. The AI never writes SQL. See Section 6.")
para("")
para("Model & settings:", bold=True)
para("Google Gemini Flash (model name is a configurable setting, currently "
     "gemini-2.5-flash for most steps). Run with temperature 0 and a fixed seed so "
     "answers are as repeatable as possible. The strong determinism comes from the "
     "code (templates, compiler, exact column names) around the AI, not the AI "
     "alone.")
doc.add_page_break()

# ---- Prompt 0 ----
prompt_section(
    "2. Prompt 0 - Column Mapping (Setup)",
    "backend/python-services/direct_mapper.py -> _build_prompt()",
    [("When", "During file setup, AFTER exact + fuzzy name matching - only the "
              "leftover output columns are sent here."),
     ("Why", "To pick which INPUT column feeds each remaining OUTPUT column, "
             "using the name and sample values."),
     ("Sends in", "Leftover output column names; all input columns; up to 5 "
                  "sample values per input column."),
     ("Gets back", "Compact JSON: for each output column, the best input column "
                   "(or null) and a confidence 0-1.")],
    "Output columns still unmatched, plus three input columns with sample values.",
    PROMPT_0,
    "“Here are the output columns I can't match and the input columns with "
    "samples - tell me which input feeds each.”",
)

# ---- Prompt 1 ----
prompt_section(
    "3. Prompt 1 - Extract Clauses from the Contract",
    "prompt_builder.py -> build_extraction_prompt()  (label: Pipeline1-FullDocument)",
    [("When", "First step of rule generation, after the PDF is read to text."),
     ("Why", "Break the contract into self-contained clauses and pull out program "
             "facts; note any outside documents referenced."),
     ("Sends in", "The full contract text. If reference documents were uploaded, "
                  "their content is attached (see the highlighted block)."),
     ("Gets back", "JSON: program_metadata + a list of clauses + "
                   "external_references.")],
    "A short two-clause contract section, plus one uploaded reference document "
    "(the Fac Guide).",
    PROMPT_1,
    "“Read the whole contract and give me clean clauses + program facts, "
    "resolving any uploaded reference documents into the clauses.”",
)

# ---- Prompt 1b ----
h1("3b. Supporting Block - Reference Documents")
para("Code: prompt_builder.py -> _reference_documents_block()", italic=True, color=GREY)
para("Not a separate AI call - this block is glued onto Prompt 1 when reference "
     "documents are uploaded. It tells the AI to treat those documents as the "
     "authoritative source and copy their real lists/limits into the matching "
     "clause. This is the exact block that gets appended:", )
code_block(PROMPT_1B)
doc.add_page_break()

# ---- Prompt 2 ----
prompt_section(
    "4. Prompt 2 - Is it a Rule? What does it check?",
    "prompt_builder.py -> build_rule_intent_prompt_batch()  (label: Call2-Intent)",
    [("When", "Second step, once per batch of clauses from Prompt 1."),
     ("Why", "For each clause decide (1) can it be checked on a data row, and (2) "
             "if so describe the check in plain words - no column names yet."),
     ("Sends in", "The clauses (id, type, title, text)."),
     ("Gets back", "Per clause: is_rule_bearing + a list of intents (subject, "
                   "operator, value, scope, severity, is_referral).")],
    "Two clauses: the $25,000,000 limit, and the Excluded Classes clause that "
    "carries a resolved [Context from ...] block.",
    PROMPT_2,
    "“For each clause, tell me if it can be checked on a data row, and if so "
    "describe the check in plain words.”",
)

# ---- Prompt 3 ----
prompt_section(
    "5. Prompt 3 - Map the Check to an Output Column",
    "prompt_builder.py -> build_ir_mapping_prompt_batch()  (label: Call3-Map)",
    [("When", "Third step, after Prompt 2 produces the plain-language intents."),
     ("Why", "Bind each intent to ONE exact output column and ONE rule template "
             "(e.g. $25M -> 'Palms part of Limit $' with max_limit)."),
     ("Sends in", "The intents, plus the allowed output column names (with samples "
                  "and any documented allowed values)."),
     ("Gets back", "Per intent: the chosen template + parameters using EXACT "
                   "column names, or template:null (-> human review).")],
    "One intent (the $25M company limit) and three candidate output columns "
    "(the $ share, the 100% policy limit, and the % share).",
    PROMPT_3,
    "“Attach each plain-language check to the correct real output column "
    "using one of the allowed rule shapes.”",
)

# ---- Section 6: SQL is built by CODE, not AI ----
h1("6. The SQL Check is Built by CODE - not AI")
para("Code: contract_upload_services/rule_compiler.py -> compile_ir()", italic=True,
     color=GREY)
para("This is the key correction to a common misunderstanding: the AI does NOT "
     "write the database query. When a rule is created (Prompt 3 output), a "
     "deterministic compiler turns the rule into SQL and stores it on the rule "
     "(rule_spec.compiled_sql). At validation time the system simply RUNS that "
     "stored SQL - there is no AI call.")
field_table([
    ("When", "SQL is compiled once, at rule-creation time (right after Prompt 3). "
             "At validation it is only executed."),
    ("Who builds it", "compile_ir() - plain code, no AI. Same rule + same schema "
                      "-> byte-identical SQL. That is where determinism comes "
                      "from."),
    ("Why not AI", "SQL structure must be exact and safe. Values are escaped and "
                   "numeric-coerced in Python; the model never authors SQL, so "
                   "there is no injection risk and no run-to-run drift."),
    ("At run time", "run_validation() reads rule_spec.compiled_sql and executes "
                    "it. If a rule has no compiled SQL, the clause is flagged for "
                    "re-upload - there is explicitly NO AI fallback."),
])
para("Real compiled SQL for the $25,000,000 'Maximum Company Program Gross Limit' "
     "rule (one sheet shown; the real query UNIONs the same check across every "
     "schedule sheet):", bold=True)
code_block(EX_COMPILED_SQL)
para("In one line: ", bold=True)
para("“The rule is compiled to SQL by code when it is created; validation just "
     "runs that SQL. The AI is not involved in making or running the query.”",
     italic=True)
doc.add_page_break()

# ---- Section 6b: the leftover AI-SQL prompt (NOT used) ----
h2("6b. Leftover AI-SQL prompt (present in code, NOT used)")
para("Code: duckdb_validation.py -> _build_prompt() / _generate_sql() / "
     "compile_rule()", italic=True, color=GREY)
para("For full transparency: an older prompt that asks the AI to write SQL still "
     "exists in the codebase, but it has NO callers in the current pipeline (the "
     "live path uses the compiled SQL above). It is kept here only for reference - "
     "it does NOT run today:")
code_block(PROMPT_5)
doc.add_page_break()

# ---- other prompts ----
h1("7. Other Prompts (supporting / alternate paths)")
para("These exist for specific or older paths and follow the same idea "
     "(instructions + data in, structured answer out):")
bullet("Classification prompt (build_classification_prompt_batch) - an older "
       "'is this rule-bearing / which engine?' step, now folded into Prompt 2.")
bullet("Stage-B synthesis prompts (build_ajv_synthesis_..., "
       "build_custom_synthesis_..., build_ir_synthesis_prompt_batch) - alternate "
       "one-shot clause->rule paths used in some flows instead of the Prompt 2 + 3 "
       "split.")
para("For everyday understanding, Prompts 0, 1, 2, 3 and 5 are the main journey.")
doc.add_page_break()

# ---- summary ----
h1("8. One-Page Summary")
t = doc.add_table(rows=1, cols=4); t.style = "Light Grid Accent 1"
for i, label in enumerate(["Prompt", "When", "Sends in", "Gets back"]):
    t.rows[0].cells[i].paragraphs[0].add_run(label).bold = True
for row in [
    ("0. Column mapping", "File setup", "Unmatched output cols + input cols + samples",
     "Best input column per output column"),
    ("1. Extraction", "Contract upload", "Full contract text (+ reference docs)",
     "Clauses + program facts + external refs"),
    ("2. Intent", "After extraction", "Clauses",
     "Is-it-a-rule + plain-language checks"),
    ("3. Mapping", "After intent", "Intents + output column list",
     "Rule template bound to a real column"),
    ("SQL compiler (CODE, not AI)", "Rule creation", "The validated rule (IR)",
     "Deterministic SQL stored on the rule, run as-is at validation"),
]:
    cells = t.add_row().cells
    for i, v in enumerate(row):
        cells[i].paragraphs[0].add_run(v)
doc.add_paragraph()
para("End of document.", italic=True, color=GREY)

OUT = os.path.join(ROOT, "Kavachio_AI_Prompts_Explained.docx")
doc.save(OUT)
print("Saved:", OUT)
print("Rendered prompt sizes (chars):",
      {"P0": len(PROMPT_0), "P1": len(PROMPT_1), "P1b": len(PROMPT_1B),
       "P2": len(PROMPT_2), "P3": len(PROMPT_3), "P5": len(PROMPT_5)})
