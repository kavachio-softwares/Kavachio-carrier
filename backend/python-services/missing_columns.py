"""Missing-BDX-column check for a Bordereau Setup.

A contract says what a bordereau must report. The sample BDX uploaded during
setup sometimes carries fewer columns than that — the gap is invisible today
until a run produces blanks. This module finds the gap ONCE, right after a setup
is built, and stores it in ``missing_bdx_columns`` so every later visit to the
setup shows the same NOTE without paying for the check again.

Cost/latency shape (this runs while a person waits):
  * The contract PDF is NEVER re-read. The heavy extraction already happened at
    upload time; this reads its OUTPUT — ``clauses_extracted``,
    ``validation_rule`` and ``contract_clause_routing`` — which is plain SQL.
  * ONE call to the SMALL model, thinking disabled, temperature 0 + fixed seed,
    a response schema so the shape is enforced by the API, and every input list
    capped/truncated (see the _MAX_* budgets) so the prompt stays small no matter
    how large the contract or the template is.

Nothing here is specific to any carrier, program, template or column name: the
BDX columns, the output template's columns and the contract evidence are all
read from the setup's own rows.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import bindparam, text

from db import (
    DirectFormat, ExportTemplate, LandingRecord, MissingBdxColumn,
    Pipeline, PipelineContract, SessionLocal,
)
from exporter import is_reference_sheet

log = logging.getLogger("bdx.missing_columns")

# Prompt budgets. The point of every cap here is a SMALL, fast request: a
# 90-column × 12-sheet template with a 200-clause contract must produce the same
# size prompt as a small one. Tunable per-deployment without a code change.
_MAX_BDX_COLUMNS = int(os.getenv("KAVACHIO_MISSING_COLS_MAX_BDX", "220"))
_MAX_OUTPUT_COLUMNS = int(os.getenv("KAVACHIO_MISSING_COLS_MAX_OUTPUT", "220"))
_MAX_CLAUSES = int(os.getenv("KAVACHIO_MISSING_COLS_MAX_CLAUSES", "60"))
_MAX_RULES = int(os.getenv("KAVACHIO_MISSING_COLS_MAX_RULES", "60"))
# Ceiling on the quotable pool as a whole, after the sources are merged.
_MAX_QUOTABLE = int(os.getenv("KAVACHIO_MISSING_COLS_MAX_QUOTABLE", "60"))
_MAX_CLAUSE_CHARS = int(os.getenv("KAVACHIO_MISSING_COLS_CLAUSE_CHARS", "420"))
# Upper bound on what we ask for back — a setup with 100 "missing" columns is a
# wrong template, not a report worth rendering.
_MAX_FINDINGS = int(os.getenv("KAVACHIO_MISSING_COLS_MAX_FINDINGS", "25"))
# Output budget. Truncation is the one failure mode that loses a GOOD answer (the
# JSON ends mid-string and the whole response is unusable), so this is sized well
# above _MAX_FINDINGS × a full-length entry, and the schema + prompt below cap
# entry length from the other side. Bigger is not better: every token is latency.
_MAX_OUTPUT_TOKENS = int(os.getenv("KAVACHIO_MISSING_COLS_MAX_TOKENS", "6144"))
# Per-entry prose limits, enforced in the schema AND asked for in the prompt.
_MAX_REASON_CHARS = 160
_MAX_QUOTE_CHARS = 180
_SEED = int(os.getenv("KAVACHIO_LLM_SEED", "7"))

# Clause types that actually describe WHAT MUST BE REPORTED get priority in the
# evidence budget; anything else fills the remainder. Types come from the
# extraction vocabulary (prompt_builder), not from any one contract.
_REPORTING_CLAUSE_TYPES = ("reporting_requirement", "mandatory_field")

# Kavachio's platform-wide standard BDX checks are persisted alongside a
# contract's own rules, tagged with this marker (generic_rule_library). They are
# OUR house rules, not this contract's words — they appear under every contract,
# carry no page, and treating them as contract text is what made a TPA agreement
# look like it demanded NAICS/SIC/currency columns. Excluded from the evidence
# entirely: this check answers "what does THIS contract require".
_GENERIC_RULE_TEXT = re.compile(r"^\s*\[\s*generic\s+rule\s*\]", re.I)

_SEVERITIES = ("required", "recommended")

# The API enforces this shape, so the answer can't drift. The length caps are
# what keep the response inside _MAX_OUTPUT_TOKENS — a truncated response is
# unparseable JSON, i.e. a good answer thrown away.
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "missing_columns": {
            "type": "array",
            "maxItems": _MAX_FINDINGS,
            "items": {
                "type": "object",
                "properties": {
                    "column_name": {"type": "string", "maxLength": 80},
                    "severity": {"type": "string", "enum": list(_SEVERITIES)},
                    "reason": {"type": "string", "maxLength": _MAX_REASON_CHARS},
                    "contract_reference": {"type": "string", "maxLength": _MAX_QUOTE_CHARS},
                    "clause_id": {"type": "string", "maxLength": 8},
                    "related_output_field": {"type": "string", "maxLength": 120},
                    "sheet": {"type": "string", "maxLength": 120},
                },
                "required": ["column_name", "severity", "reason"],
            },
        }
    },
    "required": ["missing_columns"],
}

# Appended for the ONE retry after an unusable (almost always: truncated)
# response. Same question, less prose — so the answer fits comfortably.
_BRIEF_RETRY = (
    "\n\nIMPORTANT: keep the answer SHORT. `reason` at most 12 words, "
    "`contract_reference` at most 12 words, and list only the most important "
    "entries. A short complete answer is far better than a long cut-off one."
)


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a stored timestamp as an explicit-UTC ISO string — same contract
    as app_routes._iso_utc (the columns are naive but always hold UTC; without
    the marker JS Date() reads them as local time). Re-stated here rather than
    imported so this module stays independent of the route layer."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _norm(name: Any) -> str:
    """Case/whitespace/punctuation-insensitive form of a column name, used both
    to dedupe findings and to test "does the BDX already have this?". Conservative
    — it only collapses separators and case, never words."""
    return re.sub(r"[^a-z0-9]+", "", str(name or "").strip().lower())


# ---- evidence gathering (plain SQL over already-extracted data) -------------

def _bdx_columns(s, format_id: Optional[int]) -> dict[str, list[str]]:
    """{input sheet: [column, ...]} from the most recent capture of this setup's
    input template — the same source the setup editor reads its input columns
    from, so what we check is exactly what the setup was built on."""
    if not format_id:
        return {}
    rec = (s.query(LandingRecord)
           .filter(LandingRecord.format_id == format_id)
           .order_by(LandingRecord.id.desc()).first())
    if not rec or not rec.data:
        return {}
    out: dict[str, list[str]] = {}
    for name, sheet in (rec.data.get("sheets") or {}).items():
        cols = [str(c) for c in (sheet.get("columns") or []) if str(c or "").strip()]
        out[str(name)] = cols
    return out


def _output_columns(s, template_id: Optional[int]) -> dict[str, list[str]]:
    """{output sheet: [column, ...]} for the setup's output template, skipping
    reference/lookup tabs (they carry no reportable rows)."""
    if not template_id:
        return {}
    tpl = s.get(ExportTemplate, template_id)
    if not tpl:
        return {}
    structure = tpl.structure
    if isinstance(structure, str):
        try:
            structure = json.loads(structure)
        except (ValueError, TypeError):
            structure = None
    out: dict[str, list[str]] = {}
    for sh in (structure or {}).get("sheets", []):
        if is_reference_sheet(sh):
            continue
        cols = [str(c.get("column_name")) for c in sh.get("columns", [])
                if c.get("column_name")]
        if cols:
            out[str(sh.get("sheet_name") or "")] = cols
    return out


def _unsourced_output_columns(fmt: Optional[DirectFormat],
                              output_cols: dict[str, list[str]]) -> list[str]:
    """Output columns the saved mapping fills from NOTHING in the BDX — no copied
    input column, no constant. These are the strongest deterministic hint that a
    reporting requirement has no data behind it, so they are handed to the model
    as a shortlist rather than left for it to re-derive.

    Empty when the setup has NO saved mapping: "nothing is mapped yet" would make
    every column look unsourced, which is a false lead, not a shortlist."""
    mapping = (fmt.column_mapping if fmt else None) or {}
    if not any(mapping.values()):
        return []
    out: list[str] = []
    for sheet, cols in output_cols.items():
        rules = mapping.get(sheet) or {}
        for col in cols:
            rule = rules.get(col)
            if isinstance(rule, dict) and rule.get("kind"):
                # A copy needs a real source; const/source_sheet fill themselves.
                if rule["kind"] != "copy" or rule.get("source"):
                    continue
            out.append(col)
    return out


def _contract_evidence(s, contract_ids: list[int]) -> dict[str, list[dict]]:
    """What the contract says about what a bordereau must report — read from the
    extraction output, never from the PDF. Returns two DIFFERENT kinds of thing,
    and the difference is what makes a citation trustworthy:

      "quotable" — REAL CONTRACT PROSE, each with an id, page and clause title:
                   clauses_extracted.text, validation_rule.source_verbatim_text
                   (the contract words a rule was derived FROM) and the review
                   bucket's clause_text. These are the only things the model may
                   quote, because only these have a page in the document.

      "signals"  — rule NAMES and the output fields they govern. Useful context
                   ("the contract governs Claim Status"), but they are OUR
                   wording, not the contract's, and they have no place in the
                   document — so they are explicitly not quotable. Letting these
                   be quoted is what produced citations like "Closed Claims Must
                   Have No Outstanding Reserves" with no page behind them.

    Every lookup is defensive: a table missing on an older database degrades to
    fewer hints, never to an error."""
    candidates: list[dict] = []   # merged + ranked below, then given ids
    signals: list[dict] = []
    if not contract_ids:
        return {"quotable": [], "signals": signals}

    def _cut(v: Any, n: int = _MAX_CLAUSE_CHARS) -> str:
        return " ".join(str(v or "").split())[:n]

    def _q(sql: str):
        """`IN :cids` with an expanding bind — dialect-agnostic, and the ids are
        still bound parameters (never interpolated into the SQL)."""
        return text(sql).bindparams(bindparam("cids", expanding=True))

    def _add_quotable(body: str, page: Any, label: str, rank: int) -> None:
        """rank orders the merged pool: 0 = clause text extracted straight from
        the document (in this data, always page-bearing), 1 = the contract words
        a rule was derived from, 2 = clauses parked for review."""
        if not body or _GENERIC_RULE_TEXT.match(body):
            return
        candidates.append({"rank": rank, "page": page, "clause": label or "",
                           "text": body})

    try:
        rows = s.execute(
            _q("SELECT clause_type, title, text, page_number, section_header "
               "FROM clauses_extracted WHERE contract_id IN :cids "
               "ORDER BY clause_id"),
            {"cids": contract_ids}).mappings().all()
        # Reporting/mandatory-field clauses first — they are the ones that name
        # what the bordereau has to carry; the rest fill whatever budget is left.
        ranked = sorted(
            rows, key=lambda r: 0 if (r.get("clause_type") or "") in
            _REPORTING_CLAUSE_TYPES else 1)
        for r in ranked[:_MAX_CLAUSES]:
            _add_quotable(_cut(r.get("text")), r.get("page_number"),
                          _cut(r.get("title") or r.get("section_header")
                               or r.get("clause_type"), 120), 0)
    except Exception as e:  # noqa: BLE001 — evidence is best-effort
        log.warning("clause evidence unavailable: %s", e)

    try:
        rows = s.execute(
            _q("SELECT rule_name, canonical_target, source_verbatim_text, "
               "source_page_number FROM validation_rule "
               "WHERE contract_id IN :cids AND rule_status <> 'disabled' "
               "ORDER BY rule_id"),
            {"cids": contract_ids}).mappings().all()
        for r in rows[:_MAX_RULES]:
            target = r.get("canonical_target")
            if isinstance(target, str):
                try:
                    target = json.loads(target)
                except (ValueError, TypeError):
                    target = {}
            target = target if isinstance(target, dict) else {}
            fields = [str(f) for f in (target.get("output_fields") or []) if f]
            if target.get("output_field"):
                fields.insert(0, str(target["output_field"]))
            name = _cut(r.get("rule_name"), 160)
            verbatim = _cut(r.get("source_verbatim_text"))
            if _GENERIC_RULE_TEXT.match(verbatim):
                continue          # a house rule, not this contract — see above
            # The contract words this rule came from ARE quotable; the rule's own
            # name is not — error_message is deliberately not used at all here,
            # since it is generated text with no page behind it.
            _add_quotable(verbatim, r.get("source_page_number"), name, 1)
            if fields:
                signals.append({"governs": fields[:4], "rule": name})
    except Exception as e:  # noqa: BLE001
        log.warning("rule evidence unavailable: %s", e)

    try:
        rows = s.execute(
            _q("SELECT rule_name, clause_text, source_page "
               "FROM contract_clause_routing WHERE contract_id IN :cids "
               "AND bucket = 'review' ORDER BY routing_id"),
            {"cids": contract_ids}).mappings().all()
        for r in rows[:_MAX_CLAUSES]:
            _add_quotable(_cut(r.get("clause_text")), r.get("source_page"),
                          _cut(r.get("rule_name"), 160), 2)
    except Exception as e:  # noqa: BLE001 — table only exists once a contract
        log.debug("clause-routing evidence unavailable: %s", e)  # has been persisted

    # Merge the sources into one pool the model reads and cites. Order and
    # trimming both favour text lifted straight from the document, and within a
    # source the entries that carry a page — because a finding a reviewer can
    # turn to in the contract is worth more than one they have to hunt for.
    # De-duped on text: the same clause commonly reaches us via two sources.
    seen_text: set[str] = set()
    quotable: list[dict] = []
    for c in sorted(candidates, key=lambda c: (c["rank"], c["page"] is None)):
        key = _text_key(c["text"])[:400]
        if not key or key in seen_text:
            continue
        seen_text.add(key)
        quotable.append({"id": f"C{len(quotable) + 1}", "page": c["page"],
                         "clause": c["clause"], "text": c["text"]})
        if len(quotable) >= _MAX_QUOTABLE:
            break
    return {"quotable": quotable, "signals": signals}


# ---- the model call --------------------------------------------------------

def _build_prompt(bdx_cols: dict[str, list[str]], output_cols: dict[str, list[str]],
                  unsourced: list[str], evidence: dict[str, list[dict]]) -> str:
    flat_bdx: list[str] = []
    seen: set[str] = set()
    for cols in bdx_cols.values():
        for c in cols:
            k = _norm(c)
            if k and k not in seen:
                seen.add(k)
                flat_bdx.append(c)
    flat_out: list[str] = []
    seen_out: set[str] = set()
    for cols in output_cols.values():
        for c in cols:
            k = _norm(c)
            if k and k not in seen_out:
                seen_out.add(k)
                flat_out.append(c)

    parts = [
        "You are reviewing a bordereau (BDX) setup for an insurance program.",
        "",
        "A bordereau is a SPREADSHEET: one row per policy, risk, transaction or "
        "claim, and one column per data point reported about that row. The "
        "CONTRACT below states what has to be reported. The BDX COLUMNS are the "
        "columns the client's actual bordereau file provides. Your job: name the "
        "COLUMNS the contract requires that the bordereau does NOT provide.",
        "",
        f"BDX COLUMNS PRESENT ({len(flat_bdx)}):",
        json.dumps(flat_bdx[:_MAX_BDX_COLUMNS], ensure_ascii=False),
    ]
    if len(bdx_cols) > 1:
        per_sheet = {sh: cols[:_MAX_BDX_COLUMNS] for sh, cols in bdx_cols.items()}
        parts += ["", "BDX COLUMNS BY SHEET:", json.dumps(per_sheet, ensure_ascii=False)]
    if flat_out:
        parts += ["", f"OUTPUT TEMPLATE COLUMNS ({len(flat_out)}) — the reporting "
                  "layout this program is delivered in:",
                  json.dumps(flat_out[:_MAX_OUTPUT_COLUMNS], ensure_ascii=False)]
    if unsourced:
        parts += ["", "OUTPUT COLUMNS WITH NO SOURCE IN THE BDX (strong candidates, "
                  "but only report the ones the contract actually requires):",
                  json.dumps(unsourced[:_MAX_OUTPUT_COLUMNS], ensure_ascii=False)]
    if evidence["quotable"]:
        parts += ["", "CONTRACT TEXT — the actual words of the contract, and the ONLY "
                  "thing you may quote. Each entry has an `id` you must cite:",
                  json.dumps(evidence["quotable"], ensure_ascii=False)]
    if evidence["signals"]:
        parts += ["", "FIELDS THE CONTRACT'S RULES ALREADY GOVERN — context only. "
                  "These `rule` names are OUR internal labels, NOT contract wording, "
                  "and they do not appear anywhere in the document. Use them to see "
                  "what the contract cares about; NEVER quote them:",
                  json.dumps(evidence["signals"], ensure_ascii=False)]

    parts += [
        "",
        "WHAT COUNTS AS A MISSING COLUMN:",
        "- A value that would DIFFER FROM ROW TO ROW and that the contract "
        "requires to be reported — per policy, per risk, per transaction or per "
        "claim — with no BDX column carrying it.",
        "- A value the contract needs in order for one of its own terms to be "
        "checkable row by row (e.g. a term that applies per location can only be "
        "checked when each row says which location it is).",
        "",
        "WHAT IS NOT A MISSING COLUMN — never report these:",
        "- A TERM OF THE AGREEMENT itself: a limit, sublimit, cap, rate, "
        "percentage, threshold, deductible or retention the contract fixes. Those "
        "are the same for every row; they are checked AGAINST the bordereau's "
        "existing columns, they are not columns of their own.",
        "- An EXCLUSION, definition, warranty, condition or obligation. An "
        "exclusion is not a data point a bordereau reports.",
        "- A SEPARATE DOCUMENT the contract asks for — a statement, reconciliation, "
        "estimate, certificate, notice or report. Those are deliverables, not "
        "columns in this spreadsheet.",
        "- Anything already covered by an existing BDX column.",
        "",
        "RULES FOR YOUR ANSWER:",
        "- Match by MEANING, not spelling: a BDX column with a different name, an "
        "abbreviation, or a code form still counts as present. When in doubt that "
        "a BDX column covers it, leave it out.",
        "- Report only what you are CONFIDENT about. A short, correct list is the "
        "goal; a long speculative one is worse than useless to the reviewer.",
        "- CITE THE CONTRACT. `contract_reference` must be a span of words copied "
        "character-for-character from ONE entry in CONTRACT TEXT above, and "
        "`clause_id` must be that entry's `id`. Quote the sentence that actually "
        "calls for this data point.",
        "- Never quote a rule name from the context block, and never write contract "
        "text of your own. If you cannot quote real contract text for a finding, "
        "leave `contract_reference` and `clause_id` empty — an honest blank is far "
        "better than a citation that isn't in the document.",
        "- `column_name` must read like a SPREADSHEET COLUMN HEADING for that data "
        "point, in business language a reviewer would recognise.",
        "- `severity`: \"required\" when the contract obliges that data point to be "
        "reported; \"recommended\" when it is needed to check a contract term row "
        "by row but not explicitly mandated.",
        "- `related_output_field` must be copied EXACTLY from the output template "
        "columns above when one corresponds, else an empty string.",
        "- `sheet` must be copied EXACTLY from a sheet name above when the gap is "
        "specific to one sheet, else an empty string.",
        "- Each data point ONCE. No duplicates, no near-duplicates of one "
        "another. Two names for the same value are ONE finding — a plain name "
        "and the same name with a qualifier added ARE the same column, and "
        "naming a value and naming its amount are the same column.",
        "- A clause that lists several variants of one thing gives you ONE "
        "finding PER VARIANT and no more. Name them in one consistent style, "
        "then stop — do not go back and list the same variants a second time in "
        "another style.",
        f"- At most {_MAX_FINDINGS} entries, most important first.",
        "- Be brief: `reason` one short sentence (at most 20 words), "
        "`contract_reference` a short quote (at most 20 words). A long answer "
        "risks being cut off and thrown away.",
        "- If the bordereau covers everything the contract requires, return an "
        "empty list. An empty list is a perfectly good answer.",
        "",
        "Return STRICT JSON only: "
        '{"missing_columns":[{"column_name":"...","severity":"required|recommended",'
        '"reason":"<one short sentence>","contract_reference":"<verbatim quote or empty>",'
        '"clause_id":"<the id of the CONTRACT TEXT entry quoted, or empty>",'
        '"related_output_field":"<exact output column or empty>","sheet":"<exact sheet or empty>"}]}',
    ]
    return "\n".join(parts)


def _json_slice(raw: Any) -> Optional[str]:
    """The JSON object inside a model reply — fences stripped, and any preamble
    or trailing chatter cut away. None when there is no object at all."""
    if not raw:
        return None
    txt = str(raw).strip()
    txt = re.sub(r"^```(?:json)?\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt).strip()
    if txt.startswith("{"):
        return txt
    i, j = txt.find("{"), txt.rfind("}")
    return txt[i:j + 1] if i >= 0 and j > i else None


def _parse(raw: Any) -> Optional[list[dict]]:
    """Parse the model's JSON defensively. None = unusable response (the caller
    persists NOTHING and the check is simply retried next time), which is very
    different from a parsed empty list (= checked, nothing missing)."""
    if not raw:
        return None
    txt = _json_slice(raw)
    if not txt:
        return None
    try:
        data = json.loads(txt)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("missing_columns"), list):
        return None
    return [item for item in data["missing_columns"] if isinstance(item, dict)]


def _text_key(s: Any) -> str:
    """Loose text form for locating a quote inside a clause: lower-case, every
    run of non-alphanumerics collapsed to one space. Makes the match survive the
    model re-typing a curly apostrophe, a dash or the spacing of a quote."""
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


# A quote shorter than this can appear inside an unrelated clause by chance, so
# it is not trusted to identify one.
_MIN_LOCATABLE_QUOTE = 24


def _evidence_index(evidence: dict[str, list[dict]]) -> list[dict]:
    """The quotable contract text, prepared for lookup: each entry keeps its id,
    page and clause label plus a normalised form of its text. Only contract
    PROSE is here — rule names never are, so a citation can always be checked
    against something that genuinely exists in the document."""
    return [{"id": str(c.get("id") or ""), "key": _text_key(c.get("text")),
             "page": c.get("page"), "clause": str(c.get("clause") or "")}
            for c in evidence.get("quotable", []) if c.get("text")]


def _locate_quote(quote: str, clause_id: str, index: list[dict]):
    """Where a cited line lives in the contract → (page, clause label).

    The page NEVER comes from the model — it is read back off the clause row, so
    a page shown beside a quote is one the contract really carries. Three ways to
    land it, strongest first:
      1. the quote is found inside the clause the model cited  → verified
      2. the quote is found in some other quotable clause      → verified
      3. the quote can't be found, but the cited id is real    → the model's own
         attribution to a clause that exists (weaker, still a real clause)
    Nothing matches → (None, None), and the UI shows the quote with no reference
    rather than one we can't stand behind."""
    q = _text_key(quote)
    cited = next((e for e in index if e["id"] and e["id"] == (clause_id or "").strip()), None)
    if cited and q and q in cited["key"]:
        return cited["page"], cited["clause"]
    if len(q) >= _MIN_LOCATABLE_QUOTE:
        for e in index:
            if q in e["key"]:
                return e["page"], e["clause"]
        # The model may have quoted across an elision or trimmed the tail; a
        # leading run of the quote is still specific enough to place it.
        head = q[:_MIN_LOCATABLE_QUOTE * 2]
        for e in index:
            if head in e["key"]:
                return e["page"], e["clause"]
    if cited:
        return cited["page"], cited["clause"]
    return None, None


def _name_tokens(name: str) -> frozenset[str]:
    """Word tokens of a column name, lower-cased and de-pluralised, so that
    "Policy Fees" and "Policy Fee" carry the same tokens. Order is dropped on
    purpose — "Modeling Fee" and "Fee, Modeling" name the same column."""
    out: set[str] = set()
    for t in re.split(r"[^a-z0-9]+", str(name or "").lower()):
        if not t:
            continue
        if len(t) > 3 and t.endswith("ies"):
            t = t[:-3] + "y"
        elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.add(t)
    return frozenset(out)


def _collapse_respellings(items: list[dict]) -> list[dict]:
    """Fold findings whose names are the SAME WORDS — reordered, re-punctuated
    or pluralised ("Fee, Modeling" / "Modeling Fees"). Nothing that changes a
    name's WORDS is folded here, on purpose: the words that repeat most across a
    bordereau's findings are the very ones carrying the meaning — `Reinsurer's`
    vs `Reinsured's` is two parties, `Ceded` vs `Gross` is two measures — so any
    "frequent word = filler" shortcut merges real, distinct columns and hides
    them from the reviewer. Two spellings of one gap are prevented in the prompt
    instead, where the clause that produced them is still in view.

    The survivor keeps the FIRST spelling (the model orders most-important-first)
    and the STRONGEST severity, so folding can never quietly demote an
    obligation the contract actually states."""
    out: list[dict] = []
    by_tokens: dict[frozenset[str], int] = {}
    for it in items:
        key = _name_tokens(it["column_name"])
        if not key:
            out.append(it)
            continue
        hit = by_tokens.get(key)
        if hit is None:
            by_tokens[key] = len(out)
            out.append(it)
            continue
        if out[hit].get("severity") != "required" and it.get("severity") == "required":
            out[hit]["severity"] = "required"
    return out


def _clean_findings(items: list[dict], bdx_cols: dict[str, list[str]],
                    output_cols: dict[str, list[str]],
                    evidence: Optional[dict[str, list[dict]]] = None) -> list[dict]:
    """Drop anything the model shouldn't have said, and normalise the rest:
    a column the BDX already has, a duplicate, an unknown sheet/output field
    (i.e. one it invented), or an empty name. Each surviving finding is then
    traced back to the contract line it was quoted from, for its page number."""
    present = {_norm(c) for cols in bdx_cols.values() for c in cols}
    known_sheets = {sh for sh in list(bdx_cols) + list(output_cols) if sh}
    known_fields = {_norm(c): c for cols in output_cols.values() for c in cols}
    index = _evidence_index(evidence or {})
    # A single-sheet bordereau has only one place a column could go, so say so
    # rather than leaving every finding unattributed.
    lone_sheet = next(iter(bdx_cols)) if len(bdx_cols) == 1 else ""
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        name = " ".join(str(item.get("column_name") or "").split())[:200]
        key = _norm(name)
        if not key or key in present or key in seen:
            continue
        seen.add(key)
        sev = str(item.get("severity") or "").strip().lower()
        related = str(item.get("related_output_field") or "").strip()
        sheet = str(item.get("sheet") or "").strip()
        # Generous vs. the schema's own maxLength on purpose: call_gemini falls
        # back to a schema-less call if the API rejects the schema, and then
        # nothing but this caps what gets stored.
        quote = " ".join(str(item.get("contract_reference") or "").split())[:600]
        page, label = (_locate_quote(quote, str(item.get("clause_id") or ""), index)
                       if quote else (None, None))
        out.append({
            "column_name": name,
            "normalized_name": key,
            "severity": sev if sev in _SEVERITIES else "required",
            "reason": " ".join(str(item.get("reason") or "").split())[:600] or None,
            "contract_reference": quote or None,
            "source_page": int(page) if isinstance(page, int) else None,
            "clause_label": (label or None) if page is not None or label else None,
            # Echoed values are only trusted when they name something that
            # really exists in this setup.
            "related_output_field": known_fields.get(_norm(related)),
            "sheet_key": (sheet if sheet in known_sheets else "") or lone_sheet,
        })
    # Fold two-spellings-of-one-gap BEFORE the cap, so a batch full of twins
    # can't push a real finding past the limit.
    return _collapse_respellings(out)[:_MAX_FINDINGS]


def _ask_model(prompt: str) -> Optional[list[dict]]:
    """One quick, deterministic call. Imported lazily: gemini_service builds its
    API client at import time, so a module-level import would make every route
    that merely READS this setup need an API key at boot."""
    try:
        from contract_upload_services.gemini_service import call_gemini, SMALL_MODEL
    except Exception as e:  # noqa: BLE001 — no key / no SDK configured
        log.warning("missing-column check unavailable: %s", e)
        return None
    model = os.getenv("KAVACHIO_MISSING_COLS_MODEL") or SMALL_MODEL
    try:
        raw = call_gemini(
            prompt, label="MissingBdxColumns", temperature=0, seed=_SEED,
            response_schema=_RESPONSE_SCHEMA, model=model,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
            # No "thinking": this is a comparison of two lists against quoted
            # clause text, and thinking tokens come out of the SAME budget as the
            # answer — they would cost seconds and risk truncating the JSON.
            thinking_budget=0)
    except Exception as e:  # noqa: BLE001 — never fail the caller over this
        log.warning("missing-column check failed: %s", e)
        return None
    return _parse(raw)


# ---- persistence + orchestration -------------------------------------------

def _row_to_dict(r: MissingBdxColumn) -> dict:
    return {
        "id": r.id, "pipeline_id": r.pipeline_id, "contract_id": r.contract_id,
        "sheet_key": r.sheet_key or None, "column_name": r.column_name,
        "severity": r.severity, "reason": r.reason,
        "contract_reference": r.contract_reference, "source_page": r.source_page,
        "clause_label": r.clause_label,
        "related_output_field": r.related_output_field,
        "analyzed_at": _iso_utc(r.analyzed_at),
    }


def _pipeline_contract_ids(s, pipe: Pipeline) -> list[int]:
    """The contracts attached to a setup, in order, de-duped — the same contract
    is commonly both a per-sheet pin and the fallback. Falls back to the input
    format's own contract when nothing is pinned."""
    ids = [pc.contract_id for pc in
           s.query(PipelineContract)
           .filter(PipelineContract.pipeline_id == pipe.id)
           .order_by(PipelineContract.position, PipelineContract.id).all()
           if pc.contract_id]
    ids = list(dict.fromkeys(ids))
    if not ids and pipe.input_format_id:
        fmt = s.get(DirectFormat, pipe.input_format_id)
        if fmt and fmt.contract_id:
            ids = [int(fmt.contract_id)]
    return ids


def _mapped_rule_index(s, pipeline_id: int) -> tuple[set[str], set[str]]:
    """What this setup's contracts have ALREADY wired to a column, as two
    normalised sets read from validation_rule in one pass:

      fields — the output-template columns those rules feed.
      labels — the rule names and source-clause titles behind them, so a finding
               can be recognised as belonging to a clause that is already
               mapped even when it names no column of its own.

    The label set is what makes the check usable in practice: 93% of stored
    findings record no output field at all, so matching on the field alone
    leaves nearly everything unfiltered. It is looser than the field match — a
    clause can carry more than one requirement — and that is a deliberate
    trade: this data is reviewed by people who know the contract, and they have
    asked for the already-covered entries gone.

    Why the read path needs this: the check reasons from contract prose and has
    no idea which clauses are already wired up (see _contract_evidence — rule
    targets reach the model only as loose `signals`), so it re-reports a data
    point that IS mapped. Filtering here rather than at analysis time means an
    entry disappears the moment someone sets a field, with no re-check and no
    model call — the stored snapshot stays the model's untouched answer.

    Best-effort: if the rule table can't be read we filter nothing, which shows
    the unfiltered note rather than hiding a real gap."""
    pipe = s.get(Pipeline, pipeline_id)
    if not pipe:
        return set(), set()
    contract_ids = _pipeline_contract_ids(s, pipe)
    if not contract_ids:
        return set(), set()
    try:
        rows = s.execute(
            text("SELECT r.canonical_target, r.rule_name, c.title AS clause_title "
                 "FROM validation_rule r "
                 "LEFT JOIN clauses_extracted c ON c.clause_id = r.source_clause_id "
                 "WHERE r.contract_id IN :cids AND r.rule_status <> 'disabled'")
            .bindparams(bindparam("cids", expanding=True)),
            {"cids": contract_ids}).mappings().all()
    except Exception as e:  # noqa: BLE001
        log.warning("mapped-rule index unavailable for pipeline %s: %s",
                    pipeline_id, e)
        return set(), set()
    fields: set[str] = set()
    labels: set[str] = set()
    for r in rows:
        target = r.get("canonical_target")
        if isinstance(target, str):
            try:
                target = json.loads(target)
            except (ValueError, TypeError):
                target = {}
        if not isinstance(target, dict):
            continue
        names = [f for f in (target.get("output_fields") or []) if f]
        if target.get("output_field"):
            names.append(target["output_field"])
        if not names:
            continue          # an unmapped rule proves nothing about coverage
        for f in names:
            key = _norm(f)
            if key:
                fields.add(key)
        for v in (r.get("rule_name"), r.get("clause_title")):
            key = _norm(v)
            if key:
                labels.add(key)
    return fields, labels


def _review_clauses(s, pipeline_id: int) -> list[dict]:
    """Rule-bearing clauses this setup's contracts carry that have NO output
    column yet — contract_clause_routing's 'review' bucket, exactly as the rule
    generator left it.

    This is the deterministic half of the note, and it needs no model at all:
    the extraction already decided a clause deserves a rule and already recorded
    why no column fitted. Being in this bucket IS the definition of unmapped, so
    there is nothing to filter — resolving a clause writes its rule and DELETEs
    the row in the same transaction (db_persister.persist_resolved_rules), so an
    entry leaves this list the moment someone picks a column.

    Best-effort: the table only exists once a contract has been persisted, and an
    older database may not have it at all."""
    pipe = s.get(Pipeline, pipeline_id)
    if not pipe:
        return []
    contract_ids = _pipeline_contract_ids(s, pipe)
    if not contract_ids:
        return []
    try:
        rows = s.execute(
            text("SELECT contract_id, clause_id, rule_name, clause_text, "
                 "       source_page, reason "
                 "FROM contract_clause_routing "
                 "WHERE contract_id IN :cids AND bucket = 'review' "
                 "ORDER BY contract_id, routing_id")
            .bindparams(bindparam("cids", expanding=True)),
            {"cids": contract_ids}).mappings().all()
    except Exception as e:  # noqa: BLE001
        log.debug("clause-routing unavailable for pipeline %s: %s", pipeline_id, e)
        return []
    # House rules from the shared generic library are NOT this contract's
    # requirements — they are checks we try to apply to every bordereau, and they
    # land here whenever the template has no column for them. Listing them as
    # "clauses awaiting a column" buries the handful the contract actually asks
    # for (on a real setup: 24 generic vs 7 contract-specific). Same marker the
    # evidence builder already excludes on.
    return [{"contract_id": r.get("contract_id"), "clause_id": r.get("clause_id"),
             "rule_name": r.get("rule_name"), "clause_text": r.get("clause_text"),
             "source_page": r.get("source_page"), "reason": r.get("reason")}
            for r in rows
            if not _GENERIC_RULE_TEXT.match(r.get("clause_text") or "")]


def _stored(s, pipeline_id: int) -> dict:
    """The saved snapshot for a pipeline, in the shape both screens render.

    TWO lists, deliberately kept apart because they answer different questions:

      unmapped_clauses — rule-bearing clauses with no output column. Derived,
                         not generated: no model call, always current.
      items            — data points the contract asks for that are in NO rule
                         at all (e.g. fee columns nobody wrote a rule for). Only
                         a model reads those out of the prose, so this half is
                         the stored answer of the MissingBdxColumns check.

    `analyzed` False = the model half was never successfully run (the clean
    marker row is what records "checked, nothing missing"). It says nothing
    about unmapped_clauses, which are available either way."""
    rows = (s.query(MissingBdxColumn)
            .filter(MissingBdxColumn.pipeline_id == pipeline_id)
            .order_by(MissingBdxColumn.id).all())
    items = [_row_to_dict(r) for r in rows if r.status == "missing"]
    mapped_fields, mapped_labels = _mapped_rule_index(s, pipeline_id)
    # Drop what is already covered, by either handle. _norm(None) is "" and no
    # empty key is ever in these sets, so a finding carrying neither a field nor
    # a clause label is always kept — nothing proves it covered.
    items = [i for i in items
             if _norm(i["related_output_field"]) not in mapped_fields
             and _norm(i["clause_label"]) not in mapped_labels]
    # Contract-mandated gaps first — the model already returns most-important
    # first, so this only lifts severity above that order, stably.
    items.sort(key=lambda i: 0 if i["severity"] == "required" else 1)
    analyzed_at = max((r.analyzed_at for r in rows if r.analyzed_at), default=None)
    clauses = _review_clauses(s, pipeline_id)
    # Keep the two groups from saying the same thing twice. A finding that names
    # a clause already listed above — by its own column name or by the clause it
    # was quoted from — is fully visible there, complete with the extraction's
    # reason for why no column fitted. Showing it again below only pads the note.
    clause_names = {_norm(c["rule_name"]) for c in clauses if c.get("rule_name")}
    if clause_names:
        items = [i for i in items
                 if _norm(i["column_name"]) not in clause_names
                 and _norm(i["clause_label"]) not in clause_names]
    return {
        "pipeline_id": pipeline_id,
        "analyzed": bool(rows),
        "analyzed_at": _iso_utc(analyzed_at),
        "items": items,
        "counts": {
            "total": len(items),
            "required": sum(1 for i in items if i["severity"] == "required"),
            "recommended": sum(1 for i in items if i["severity"] == "recommended"),
        },
        "unmapped_clauses": clauses,
        "unmapped_count": len(clauses),
    }


# One lock per pipeline, so two checks of the SAME setup (two open tabs, or a
# build's re-check landing while its page is open) serialise: the second waits,
# then finds the first one's fresh result and returns it instead of spending
# another model call and racing it into the table. Cross-process safety comes
# from the delete+insert being one transaction plus the unique index.
_locks_guard = threading.Lock()
_locks: dict[int, threading.Lock] = {}


def _pipeline_lock(pipeline_id: int) -> threading.Lock:
    # One small lock object per pipeline this process has ever checked — bounded
    # by the number of setups, so it is not worth reaping.
    with _locks_guard:
        return _locks.setdefault(int(pipeline_id), threading.Lock())


def _replace(s, pipe: Pipeline, contract_id: Optional[int],
             findings: list[dict]) -> None:
    """Swap this pipeline's snapshot for a new one in a single transaction, so a
    re-analysis can never leave stale rows behind or double up (the delete is
    what makes re-running idempotent; the unique index is the backstop)."""
    now = datetime.utcnow()
    s.query(MissingBdxColumn).filter(
        MissingBdxColumn.pipeline_id == pipe.id).delete(synchronize_session=False)
    common = dict(tenant_id=pipe.tenant_id, pipeline_id=pipe.id,
                  format_id=pipe.input_format_id, contract_id=contract_id,
                  analyzed_at=now, created_at=now)
    if not findings:
        # "Checked, nothing missing" — see MissingBdxColumn.status.
        s.add(MissingBdxColumn(status="clean", sheet_key="", column_name="",
                               normalized_name="", **common))
    for f in findings:
        s.add(MissingBdxColumn(
            status="missing", sheet_key=f["sheet_key"],
            column_name=f["column_name"], normalized_name=f["normalized_name"],
            severity=f["severity"], reason=f["reason"],
            contract_reference=f["contract_reference"],
            source_page=f["source_page"], clause_label=f["clause_label"],
            related_output_field=f["related_output_field"], **common))
    s.commit()


def get_for_pipeline(pipeline_id: int) -> dict:
    """Read the stored snapshot, minus anything already mapped to an output
    field. No model call — this is what the setup screen hits on every visit.
    It does read validation_rule (see _mapped_rule_index), which is what
    makes a set field drop out of the note immediately."""
    with SessionLocal() as s:
        return _stored(s, pipeline_id)


def analyze_pipeline(pipeline_id: int, force: bool = False) -> dict:
    """Check this setup's BDX against its contract(s) and store the result.

    Returns the same shape as ``get_for_pipeline``, plus ``ran`` (whether this
    call actually spent a model call) and ``skipped_reason`` when it could not.
    Already-analyzed setups return the stored snapshot untouched unless `force`.

    Never raises for a missing model/key/contract: the setup flow that calls this
    must continue exactly as before when the check can't run.

    Serialised per pipeline — a second check of the same setup waits, then finds
    the first one's answer already stored rather than repeating the model call."""
    with _pipeline_lock(pipeline_id):
        return _analyze_locked(pipeline_id, force)


def _analyze_locked(pipeline_id: int, force: bool) -> dict:
    with SessionLocal() as s:
        pipe = s.get(Pipeline, pipeline_id)
        if not pipe:
            return {"pipeline_id": pipeline_id, "analyzed": False, "items": [],
                    "counts": {"total": 0, "required": 0, "recommended": 0},
                    "ran": False, "skipped_reason": "setup not found"}
        existing = _stored(s, pipeline_id)
        if existing["analyzed"] and not force:
            return {**existing, "ran": False, "skipped_reason": None}

        fmt = s.get(DirectFormat, pipe.input_format_id) if pipe.input_format_id else None
        bdx_cols = _bdx_columns(s, pipe.input_format_id)
        output_cols = _output_columns(s, pipe.output_template_id)
        # De-duped, so the same contract pinned per-sheet AND as the fallback
        # doesn't just pad the prompt.
        contract_ids = _pipeline_contract_ids(s, pipe)
        primary_contract = contract_ids[0] if contract_ids else None

        if not bdx_cols:
            return {**existing, "ran": False,
                    "skipped_reason": "no sample bordereau captured for this setup yet"}
        if not contract_ids:
            return {**existing, "ran": False,
                    "skipped_reason": "no contract attached to this setup"}

        evidence = _contract_evidence(s, contract_ids)
        # Quotable contract prose is what the check reasons FROM; rule-name
        # signals alone can't ground a finding, so that isn't enough to run on.
        if not evidence["quotable"]:
            return {**existing, "ran": False,
                    "skipped_reason": "this contract has no extracted clauses to check against"}
        unsourced = _unsourced_output_columns(fmt, output_cols)
        prompt = _build_prompt(bdx_cols, output_cols, unsourced, evidence)

    # Model call OUTSIDE the DB session — no connection is held while waiting.
    parsed = _ask_model(prompt)
    if parsed is None:
        # One retry, asking for the same answer in fewer words. The realistic
        # failure is a response that ran past the output budget and ended
        # mid-JSON — a good answer lost to length, which brevity fixes.
        log.info("missing-column check: retrying briefly for pipeline %s", pipeline_id)
        parsed = _ask_model(prompt + _BRIEF_RETRY)
    if parsed is None:
        with SessionLocal() as s:
            return {**_stored(s, pipeline_id), "ran": False,
                    "skipped_reason": "the contract check could not be completed"}

    findings = _clean_findings(parsed, bdx_cols, output_cols, evidence)
    with SessionLocal() as s:
        pipe = s.get(Pipeline, pipeline_id)
        if not pipe:
            return {"pipeline_id": pipeline_id, "analyzed": False, "items": [],
                    "counts": {"total": 0, "required": 0, "recommended": 0},
                    "ran": False, "skipped_reason": "setup not found"}
        try:
            _replace(s, pipe, primary_contract, findings)
        except Exception as e:  # noqa: BLE001
            # Another worker/process committed its own snapshot for this pipeline
            # first (the unique index caught the overlap). Its answer is just as
            # current as ours, so read that back instead of failing the caller.
            s.rollback()
            log.warning("missing-column write lost a race for pipeline %s: %s",
                        pipeline_id, e)
            return {**_stored(s, pipeline_id), "ran": False, "skipped_reason": None}
        return {**_stored(s, pipeline_id), "ran": True, "skipped_reason": None}
