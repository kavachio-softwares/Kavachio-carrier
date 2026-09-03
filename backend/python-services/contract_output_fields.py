"""What does THIS contract require the bordereau to report?

Used to build an output BDX template for a contract that has no published
standard behind it — the plan's "Contract-Based / Custom Format". The answer is
a list of field REQUIREMENTS, which the caller then merges with the standard
field library and hands to the template editor. It is never a template on its
own, and it never touches the database (plan section 8/24).

Cost shape, copied deliberately from ``missing_columns``:
  * The contract PDF is NEVER re-read. Extraction already happened at upload;
    this reads its OUTPUT — ``clauses_extracted`` and ``validation_rule`` —
    which is plain SQL.
  * ONE call to the SMALL model: temperature 0, fixed seed, a response schema so
    the shape is enforced by the API, thinking disabled, every input list capped
    so a 200-clause contract sends the same size prompt as a short one.

If the model is unavailable the caller still gets a usable answer: the fields the
contract's OWN rules already name (``validation_rule.canonical_target``) are
collected without any model call, and the model only adds to them.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable, Optional

from sqlalchemy import bindparam, text

from data_model import DATA_MODEL

log = logging.getLogger("bdx.contract_output_fields")

_MAX_CLAUSES = int(os.getenv("KAVACHIO_CONTRACT_FIELDS_MAX_CLAUSES", "60"))
_MAX_RULES = int(os.getenv("KAVACHIO_CONTRACT_FIELDS_MAX_RULES", "80"))
_MAX_CLAUSE_CHARS = int(os.getenv("KAVACHIO_CONTRACT_FIELDS_CLAUSE_CHARS", "420"))
_MAX_FIELDS = int(os.getenv("KAVACHIO_CONTRACT_FIELDS_MAX", "40"))
# A paragraph shorter than this in a freshly-read document is a page number, a
# heading or a stray caption, not a term.
_MIN_PARA_CHARS = int(os.getenv("KAVACHIO_CONTRACT_FIELDS_MIN_PARA", "40"))
_MAX_OUTPUT_TOKENS = int(os.getenv("KAVACHIO_CONTRACT_FIELDS_MAX_TOKENS", "6144"))
_SEED = 20260101

# The clause types that actually say what has to be reported. Everything else
# fills whatever prompt budget is left over.
_REPORTING_CLAUSE_TYPES = {
    "reporting", "reporting_requirements", "bordereau", "bordereaux",
    "data_requirements", "mandatory_fields", "premium", "claims",
}

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "maxItems": _MAX_FIELDS,
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "maxLength": 80},
                    "category": {"type": "string", "maxLength": 40},
                    "data_type": {"type": "string",
                                  "enum": ["string", "int", "decimal", "date",
                                           "datetime", "bool"]},
                    "required": {"type": "boolean"},
                    "reason": {"type": "string", "maxLength": 160},
                    "contract_reference": {"type": "string", "maxLength": 200},
                },
                "required": ["field", "required"],
            },
        }
    },
    "required": ["fields"],
}


def _cut(v: Any, n: int = _MAX_CLAUSE_CHARS) -> str:
    return " ".join(str(v or "").split())[:n]


def _q(sql: str):
    """`IN :cids` with an expanding bind — the ids stay bound parameters."""
    return text(sql).bindparams(bindparam("cids", expanding=True))


def _evidence(s, contract_ids: list[int]) -> tuple[list[dict], list[dict]]:
    """(clause prose, rule targets) for the contracts in scope.

    Both lookups are defensive: a table missing on an older database costs us
    hints, never an error.
    """
    clauses: list[dict] = []
    rules: list[dict] = []
    if not contract_ids:
        return clauses, rules
    try:
        rows = s.execute(
            _q("SELECT clause_type, title, text, page_number, section_header "
               "FROM clauses_extracted WHERE contract_id IN :cids ORDER BY clause_id"),
            {"cids": contract_ids}).mappings().all()
        ranked = sorted(rows, key=lambda r: 0 if (r.get("clause_type") or "")
                        in _REPORTING_CLAUSE_TYPES else 1)
        for r in ranked[:_MAX_CLAUSES]:
            body = _cut(r.get("text"))
            if body:
                clauses.append({
                    "title": _cut(r.get("title") or r.get("section_header")
                                  or r.get("clause_type"), 120),
                    "page": r.get("page_number"),
                    "text": body,
                })
    except Exception as e:  # noqa: BLE001 — evidence is best-effort
        log.warning("clause evidence unavailable: %s", e)
    try:
        rows = s.execute(
            _q("SELECT rule_name, canonical_target FROM validation_rule "
               "WHERE contract_id IN :cids AND rule_status <> 'disabled' "
               "ORDER BY rule_id"),
            {"cids": contract_ids}).mappings().all()
        for r in rows[:_MAX_RULES]:
            rules.append({"rule": _cut(r.get("rule_name"), 120),
                          "targets": _targets(r.get("canonical_target"))})
    except Exception as e:  # noqa: BLE001
        log.warning("rule evidence unavailable: %s", e)
    return clauses, rules


def _targets(canonical_target: Any) -> list[str]:
    """The field(s) one extracted rule actually points at.

    ``validation_rule.canonical_target`` is JSON, not a name: the compiler
    writes ``{"output_field": ..., "output_fields": [...], "unmapped": bool}``
    because a rule can govern more than one column ("Policy Period Duration"
    constrains inception AND expiry) and because a rule can be extracted without
    ever resolving to a column. Older rows, and other deployments, store a bare
    canonical key — both are read here, and a rule that never resolved is
    skipped rather than turned into a column nobody asked for.
    """
    if canonical_target is None:
        return []
    if isinstance(canonical_target, str):
        raw = canonical_target.strip()
        if not raw:
            return []
        if raw.startswith("{"):
            import json
            try:
                canonical_target = json.loads(raw)
            except ValueError:
                return [raw]
        else:
            return [raw]
    if not isinstance(canonical_target, dict):
        return []
    if canonical_target.get("unmapped"):
        return []
    many = canonical_target.get("output_fields")
    if isinstance(many, list):
        return [str(v).strip() for v in many if str(v or "").strip()]
    one = canonical_target.get("output_field")
    return [str(one).strip()] if str(one or "").strip() else []


def _fields_the_rules_already_name(rules: list[dict]) -> list[dict]:
    """Fields the contract's own extracted rules already point at.

    This needs no model at all: a rule that validates ``gross_premium`` is proof
    the contract cares about gross premium. It is the floor the model builds on,
    and the whole answer when the model is unavailable.
    """
    out: dict[str, dict] = {}
    for r in rules:
        for target in r.get("targets") or []:
            # Two kinds of name arrive here. A CANONICAL key ("gross_premium")
            # is the data model's own handle and needs title-casing into a
            # heading; anything else is already the heading the rule was written
            # against ("Pol Occ Limit") and must be left exactly as it is —
            # title-casing that one would quietly rename the column.
            entry = DATA_MODEL.get(target)
            label = target.replace("_", " ").title() if entry else target
            key = label.strip().lower()
            if not key or key in out:
                continue
            out[key] = {
                "field": label,
                "source_field": target if entry else None,
                "data_type": (entry or {}).get("type") or "string",
                "required": True,
                "origin": "contract_rule",
                "reason": f"The contract carries a validation rule on this field"
                          f"{' (' + r['rule'] + ')' if r.get('rule') else ''}.",
            }
    return list(out.values())


# The words that mark a passage as being about what has to be reported. Taken
# from the clause TYPES the extractor already uses rather than invented here, so
# there is one vocabulary for "this is a reporting term", not two.
_REPORTING_WORDS = {w for t in _REPORTING_CLAUSE_TYPES for w in t.split("_") if len(w) > 3}


def _mentions_reporting(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in _REPORTING_WORDS)


# A term in a contract starts at its number — "7.", "7.1", "4)". That is a
# structural convention of the documents themselves, not a vocabulary, so it
# survives contracts written in any wording.
_CLAUSE_START = re.compile(r"^\(?\d+(\.\d+)*[.)]?\s")


def clauses_from_document(pages: Iterable[dict]) -> list[dict]:
    """Clause-shaped evidence from a contract that has NOT been saved yet.

    The module's rule is that a stored contract's PDF is never re-read, and that
    still holds: this path exists for the other case, a contract staged in the
    setup form and not yet uploaded, where there is no extraction to read
    instead. Reading it here is what lets the output template be proposed from
    the contract the user is holding rather than only from one already on file.

    Extracted text does not reliably carry blank lines between terms, so
    splitting on those alone can hand back one page-sized block that is then
    truncated to the first clause. Terms are cut at a blank line, at a numbered
    heading, and at the length cap — whichever comes first — so a contract laid
    out either way arrives as the same kind of evidence, and the same shape and
    cap ``_evidence`` returns.
    """
    chunks: list[dict] = []

    def keep(page_no, parts: list[str]) -> None:
        body = _cut(" ".join(parts))
        if len(body) >= _MIN_PARA_CHARS:
            chunks.append({"title": " ".join(body.split()[:8]),
                           "page": page_no, "text": body})

    for p in pages or []:
        page_no = p.get("page")
        buf: list[str] = []
        size = 0
        for line in str(p.get("text") or "").splitlines():
            line = line.strip()
            if not line:
                keep(page_no, buf)
                buf, size = [], 0
                continue
            if buf and (_CLAUSE_START.match(line)
                        or size + len(line) + 1 > _MAX_CLAUSE_CHARS):
                keep(page_no, buf)
                buf, size = [], 0
            buf.append(line)
            size += len(line) + 1
        keep(page_no, buf)

    # Ranked the way stored clauses are: what talks about reporting first, so a
    # 90-page contract sends the same size prompt as a 3-page one.
    chunks.sort(key=lambda c: 0 if _mentions_reporting(c["text"]) else 1)
    return chunks[:_MAX_CLAUSES]


def _build_prompt(clauses: list[dict], rules: list[dict],
                  known_fields: list[str]) -> str:
    lines = [
        "You are reading an insurance contract's EXTRACTED clauses to work out "
        "which columns its bordereau (a periodic spreadsheet of policies, "
        "premiums and claims) must carry.",
        "",
        "Return ONLY fields the contract itself calls for. Do not invent standard "
        "insurance columns that the text below does not support, and do not "
        "repeat anything in ALREADY KNOWN.",
        "",
        "For each field give: `field` (the column heading a person would use), "
        "`category` (one of Policy, Premium, Claims, Tax, Regulatory, Party, "
        "Broker, Reinsurance, Contract), `data_type`, `required` (true only when "
        "the contract makes it obligatory), `reason` (short), and "
        "`contract_reference` (a short quote from the clause text below — never "
        "invent one; leave it out if nothing below supports the field).",
        "",
        "ALREADY KNOWN (do not repeat these):",
    ]
    lines.extend(f"  - {n}" for n in known_fields[:120] or ["  (none)"])
    lines.append("")
    lines.append("CONTRACT CLAUSES:")
    if clauses:
        for i, c in enumerate(clauses, 1):
            page = f" (page {c['page']})" if c.get("page") else ""
            lines.append(f"  [{i}] {c['title']}{page}: {c['text']}")
    else:
        lines.append("  (none extracted)")
    lines.append("")
    lines.append("RULES ALREADY DERIVED FROM THIS CONTRACT:")
    if rules:
        for r in rules[:40]:
            tgt = ", ".join(r.get("targets") or []) or "?"
            lines.append(f"  - {r.get('rule') or '(unnamed)'} -> {tgt}")
    else:
        lines.append("  (none)")
    return "\n".join(lines)


def _ask_model(prompt: str) -> Optional[list[dict]]:
    """Imported lazily: gemini_service builds its API client at import time, so a
    module-level import would make every route that merely reads a template need
    an API key at boot."""
    try:
        from contract_upload_services.gemini_service import call_gemini, SMALL_MODEL
    except Exception as e:  # noqa: BLE001 — no key / no SDK configured
        log.warning("contract field analysis unavailable: %s", e)
        return None
    model = os.getenv("KAVACHIO_CONTRACT_FIELDS_MODEL") or SMALL_MODEL
    try:
        raw = call_gemini(prompt, label="ContractOutputFields", temperature=0,
                          seed=_SEED, response_schema=_RESPONSE_SCHEMA, model=model,
                          max_output_tokens=_MAX_OUTPUT_TOKENS, thinking_budget=0)
    except Exception as e:  # noqa: BLE001 — never fail the caller over this
        log.warning("contract field analysis failed: %s", e)
        return None
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    # The schema asks for {"fields": [...]}, and the model frequently answers
    # with the bare array instead — which is the same answer, so read it rather
    # than throwing away a good response over its wrapper.
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return None
    items = raw.get("fields")
    return items if isinstance(items, list) else None


def analyze(s, contract_ids: list[int],
            known_fields: Optional[list[str]] = None,
            *, documents: Optional[list[dict]] = None) -> dict:
    """The contract's output-field requirements.

    `known_fields` are the headings the template already has (the standard
    library, typically) so the model is asked only for what is NOT there.

    `documents` is clause-shaped prose read from a contract that has no row yet
    (see ``clauses_from_document``). It is used ON TOP OF whatever the stored
    contracts provide, so staging a file and naming a saved contract are not
    mutually exclusive.

    Returns ``{"fields": [...], "model_used": bool, "clause_count": int}``. The
    caller VALIDATES and merges this — nothing here is written anywhere, and the
    model is never given the ability to name a canonical field directly (that is
    resolved below, against the shipped data model).
    """
    clauses, rules = _evidence(s, contract_ids)
    if documents:
        clauses = (list(documents) + clauses)[:_MAX_CLAUSES]
    fields = _fields_the_rules_already_name(rules)
    known = {f["field"].strip().lower() for f in fields}
    known |= {str(k).strip().lower() for k in (known_fields or [])}

    proposed = _ask_model(_build_prompt(clauses, rules, sorted(known)))
    model_used = proposed is not None
    for item in proposed or []:
        name = str(item.get("field") or "").strip()
        if not name or name.lower() in known:
            continue
        known.add(name.lower())
        fields.append({
            "field": name,
            # The model proposes a HEADING, never a canonical field id — the
            # mapping to the data model happens through the normal
            # propose_template_mapping pass, which is already validated.
            "source_field": None,
            "data_type": item.get("data_type") or "string",
            "required": bool(item.get("required")),
            "category": item.get("category"),
            "origin": "contract_ai",
            "reason": item.get("reason"),
            "contract_reference": item.get("contract_reference"),
        })
        if len(fields) >= _MAX_FIELDS:
            break
    return {"fields": fields, "model_used": model_used,
            "clause_count": len(clauses), "rule_count": len(rules)}
