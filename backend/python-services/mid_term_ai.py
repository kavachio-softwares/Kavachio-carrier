"""
Mid-term change — the three questions only a model can answer.

  * read_terms: what each agreed term (contract_types.AGREED_LIMITS) stands at
    today, read off an UPLOADED contract's clauses. An uploaded contract has no
    typed terms, and before its Bordereau Setup it has no checks to read them
    from either, so without this the change screen opens with every term blank.
  * read_change_document: what a signed change document (an endorsement PDF)
    changes — the terms it moves, and the clauses it re-words, adds or deletes
    — matched against the contract's own clauses. Only the change document is
    read (Kavachio_Mid_Term_Change_Plan.md §6.3): the contract's original PDF
    is not kept in database storage mode, and re-reading it would buy back
    clauses that are already stored.
  * rules_for_clauses: the checks an edited or added clause makes, against a
    setup's output template — the setup's own Pipeline 2 (Call 2 intents, Call
    3 binding), run on those clauses only.

Every answer is cached on what was asked (ai_cache), so the same clauses give
the same terms and the same checks every time, and a preview repeated while the
carrier types costs nothing. A failed call is never cached and never raises:
the caller gets an error string to show, and the screen says so instead of
guessing.

The vocabulary in every prompt is the served one (contract_types.
agreed_limits_spec) — nothing about terms is restated here.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import ai_cache
import contract_types as ct

log = logging.getLogger("kavachio.contracts.mid_term_ai")

_SEED = 20261020
_MAX_OUTPUT_TOKENS = int(os.getenv("KAVACHIO_MID_TERM_MAX_TOKENS", "16384"))
# Part of each cached answer's key: bump when a prompt or schema changes, or an
# answer to the old question is served for the new one.
TERMS_VERSION = "mid_term_terms_v1"
DOCUMENT_VERSION = "mid_term_document_v1"
TERMS_KIND = "mid_term_terms"
DOCUMENT_KIND = "mid_term_document"
AI_UNAVAILABLE = ("Kavachio couldn't read this just now (the AI service did not "
                  "answer). Try again in a moment.")


def _model() -> Optional[str]:
    try:
        from contract_upload_services.gemini_service import EXTRACTION_MODEL, model_for
    except Exception:  # noqa: BLE001 — no SDK configured
        return None
    return model_for("mid_term_change",
                     os.getenv("KAVACHIO_MID_TERM_MODEL") or EXTRACTION_MODEL)


def _ask(prompt: str, schema: dict, label: str) -> Optional[Any]:
    """One deterministic call (temperature 0, fixed seed, no thinking), parsed.
    None on any failure — the caller reports it."""
    model = _model()
    if model is None:
        return None
    from contract_upload_services.gemini_service import call_gemini
    try:
        raw = call_gemini(prompt, label=label, temperature=0, seed=_SEED,
                          response_schema=schema, model=model,
                          max_output_tokens=_MAX_OUTPUT_TOKENS, thinking_budget=0)
    except Exception as e:  # noqa: BLE001 — never fail the caller over this
        log.warning("%s failed: %s", label, e)
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw


_NOT_ASKED = object()


def _cached(kind: str, version: str, prompt: str, tenant_id, schema: dict,
            label: str, refresh: bool = False, ask: bool = True) -> Optional[Any]:
    """The stored answer to this exact prompt, else a fresh one (stored). With
    `ask` False a miss returns _NOT_ASKED instead of calling the model — for
    the preview, which runs on every keystroke and must never wait on one."""
    model = _model()
    key = ai_cache.make_key(*ai_cache.model_scoped(
        (version, tenant_id, prompt, {"seed": _SEED}), model)) if model else None
    hit = ai_cache.get(kind, key, refresh=refresh) if key else None
    if hit is not None:
        return hit
    if not ask:
        return _NOT_ASKED
    answer = _ask(prompt, schema, label)
    if key and answer is not None:
        ai_cache.put(kind, key, answer, tenant_id=tenant_id)
    return answer


# =============================================================================
#  The vocabulary, as the prompts state it
# =============================================================================

def _vocabulary() -> list[dict]:
    return ct.agreed_limits_spec()


def _vocabulary_lines() -> str:
    lines = []
    for t in _vocabulary():
        how = {"percent": "a number, the percentage without the % sign (23.5)",
               "money": "a plain number, no currency symbol or separators (3000000)",
               "int": "a whole number" + (f" of {t['unit']}" if t.get("unit") else ""),
               "choice": "exactly one of: " + " | ".join(t.get("choices") or []),
               }.get(t["kind"], "a short phrase, as the clause words it")
        lines.append(f"- {t['name']}: {t['question']}"
                     + (f" ({t['sub']})" if t.get("sub") else "") + f" — {how}")
    return "\n".join(lines)


def _clean_value(key: str, value) -> Any:
    """A value the model gave, coerced the way contract_types keeps it; None
    when it is not a value of that term."""
    cleaned = ct.clean_agreed_limits({key: {"value": value}})
    entry = cleaned.get(key)
    if entry is None:
        return None
    spec = ct.AGREED_LIMITS.get(key) or {}
    if spec.get("kind") == "choice" and spec.get("choices") \
            and str(entry["value"]) not in spec["choices"]:
        return None
    return entry["value"]


# =============================================================================
#  1. Today's terms, read off the clauses
# =============================================================================

_TERMS_SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "clause_id": {"type": "integer"},
                    "quote": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        }
    },
    "required": ["terms"],
}


def _terms_prompt(clauses: list[dict]) -> str:
    body = json.dumps([{"id": c["clause_id"], "title": c.get("title") or "",
                        "text": c.get("text") or ""} for c in clauses],
                      ensure_ascii=False)
    return (
        "You read the clauses of an insurance contract (a binding authority or "
        "programme schedule) and report the value each TERM below has, ONLY where "
        "a clause states it outright.\n\n"
        "TERMS (key: what it is — how to write the value):\n"
        f"{_vocabulary_lines()}\n\n"
        "Rules:\n"
        "- Report a term only when a clause states its value. Never infer, "
        "compute, or take a value from a different term.\n"
        "- A clause saying the term does not apply (\"Not Applicable\", \"None\") "
        "is not a value: leave the term out.\n"
        "- A limit stated per policy is the per-policy term; sub-limits are not "
        "the main limit.\n"
        "- clause_id is the id of the clause that states it; quote is the words "
        "that state it (at most 200 characters).\n"
        "- Each key at most once. Keys not in the list are not allowed.\n\n"
        f"CLAUSES (JSON):\n{body}\n\n"
        'Answer as JSON: {"terms": [{"key", "value", "clause_id", "quote"}]}')


def read_terms(clauses: list[dict], tenant_id=None, refresh: bool = False,
               ask: bool = True) -> tuple[dict, Optional[str]]:
    """({term key: {value, clause_id, quote}}, error). Only clauses with words
    are sent; a value that is not a value of its term is dropped. With `ask`
    False only a stored answer is used ({} on a miss, no error)."""
    usable = [c for c in clauses or [] if (c.get("text") or "").strip() and c.get("clause_id")]
    if not usable:
        return {}, None
    answer = _cached(TERMS_KIND, TERMS_VERSION, _terms_prompt(usable), tenant_id,
                     _TERMS_SCHEMA, "MidTermTerms", refresh, ask=ask)
    if answer is _NOT_ASKED:
        return {}, None
    if answer is None:
        return {}, AI_UNAVAILABLE
    items = answer.get("terms") if isinstance(answer, dict) else answer
    ids = {c["clause_id"] for c in usable}
    out: dict = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        key = it.get("key")
        if key not in ct.AGREED_LIMITS or key in out:
            continue
        value = _clean_value(key, it.get("value"))
        if value is None:
            continue
        cid = it.get("clause_id")
        out[key] = {"value": value, "clause_id": cid if cid in ids else None,
                    "quote": (str(it.get("quote") or "")[:200]) or None}
    return out, None


# =============================================================================
#  2. A signed change document, read for what it changes
# =============================================================================

_DOCUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "effective_from": {"type": "string"},
        "reason": {"type": "string"},
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "value": {"type": "string"},
                               "quote": {"type": "string"}},
                "required": ["key", "value"],
            },
        },
        "clauses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["amended", "added", "removed"]},
                    "clause_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "section": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    "required": ["terms", "clauses"],
}


def _document_prompt(doc_text: str, clauses: list[dict]) -> str:
    existing = json.dumps([{"id": c["clause_id"], "title": c.get("title") or "",
                            "section": c.get("section") or "",
                            "text": (c.get("text") or "")[:600]}
                           for c in clauses if c.get("clause_id")], ensure_ascii=False)
    return (
        "You read a signed CHANGE DOCUMENT (an endorsement) to an insurance "
        "contract and say exactly what it changes, matched against the "
        "contract's existing clauses.\n\n"
        "TERMS the change may move (key: what it is — how to write the value):\n"
        f"{_vocabulary_lines()}\n\n"
        "Report:\n"
        "- effective_from: the date the change applies from, as YYYY-MM-DD, if "
        "the document states one.\n"
        "- reason: the reason it gives, in one short sentence, if any.\n"
        "- terms: each TERM whose value the document changes, with its NEW value "
        "(and quote: the words saying so). Leave out terms it does not change.\n"
        "- clauses: each clause the document changes:\n"
        "    amended — an existing clause re-worded or replaced: clause_id of "
        "that clause, title, and text = the clause's full NEW wording as it will "
        "read, not a description of the change;\n"
        "    added — a clause the contract did not have: title, section (the "
        "heading it goes under, if the document says), text = its full wording;\n"
        "    removed — an existing clause deleted: its clause_id.\n"
        "  Use the ids of EXISTING CLAUSES only; never invent one. A clause whose "
        "only change is a term's value is still reported as amended, with its "
        "new wording.\n"
        "Do not report anything the document leaves unchanged, nor its own "
        "boilerplate (\"all other terms remain unchanged\", signature blocks).\n\n"
        f"EXISTING CLAUSES (JSON):\n{existing}\n\n"
        f"CHANGE DOCUMENT:\n{doc_text}\n\n"
        'Answer as JSON: {"effective_from", "reason", "terms": [{"key", "value", '
        '"quote"}], "clauses": [{"action", "clause_id", "title", "section", "text"}]}')


def read_change_document(doc_text: str, clauses: list[dict], tenant_id=None,
                         refresh: bool = False) -> tuple[dict, Optional[str]]:
    """({effective_from, reason, terms: {key: value}, clauses: [edit]}, error).

    The clause edits come back in the shape the change screen sends
    (mid_term_change.clause_changes): {ref, action, title, section, text}. A
    clause id the contract does not have is dropped, never guessed at."""
    if not (doc_text or "").strip():
        return {}, "Kavachio found no text in this document."
    answer = _cached(DOCUMENT_KIND, DOCUMENT_VERSION, _document_prompt(doc_text, clauses),
                     tenant_id, _DOCUMENT_SCHEMA, "MidTermDocument", refresh)
    if not isinstance(answer, dict):
        return {}, AI_UNAVAILABLE
    refs = {c["clause_id"]: c["ref"] for c in clauses if c.get("clause_id")}
    terms: dict = {}
    quotes: dict = {}
    for it in answer.get("terms") or []:
        if not isinstance(it, dict) or it.get("key") not in ct.AGREED_LIMITS:
            continue
        value = _clean_value(it["key"], it.get("value"))
        if value is not None:
            terms[it["key"]] = value
            quotes[it["key"]] = (str(it.get("quote") or "")[:200]) or None
    edits = []
    for it in answer.get("clauses") or []:
        if not isinstance(it, dict):
            continue
        action = it.get("action")
        if action == "added":
            if (it.get("text") or "").strip():
                edits.append({"ref": None, "action": "added",
                              "title": (it.get("title") or "").strip() or None,
                              "section": (it.get("section") or "").strip() or None,
                              "text": it["text"].strip()})
        elif action in ("amended", "removed") and it.get("clause_id") in refs:
            edits.append({"ref": refs[it["clause_id"]], "action": action,
                          "title": (it.get("title") or "").strip() or None,
                          "text": (it.get("text") or "").strip() or None})
    eff = (answer.get("effective_from") or "").strip() or None
    return {"effective_from": eff, "reason": (answer.get("reason") or "").strip() or None,
            "terms": terms, "term_quotes": quotes, "clauses": edits}, None


# =============================================================================
#  3. The checks an edited or added clause makes
# =============================================================================

def rules_for_clauses(clauses: list[dict], template_fields: list[dict], *,
                      contract_id: int, tenant_id, program_id
                      ) -> tuple[dict, dict, Optional[str]]:
    """({clause id: [rule]}, {clause id: why no check}, error) — the rules
    Bordereau Setup's Pipeline 2 writes for these clauses against this
    template's columns, generated in memory (nothing is written).

    `clauses` are {clause_id, clause_type, title, text, section_header}; an
    added clause carries a stand-in id (negative, far from the generic
    library's own negative ids) that maps its rules back to it. Only rules
    read from these clauses are kept: Pipeline 2 also binds the generic rule
    library and the template's own checks, which the contract already has. A
    clause that bears a rule the template has no column for comes back in the
    second dict, with Pipeline 2's reason — that is what a new output template
    would be for. Both AI calls inside are cached on what they read, so the
    same words give the same checks."""
    if not clauses or not template_fields:
        return {}, {}, None
    from contract_upload_services.validation_rule_generator import ValidationRuleGenerator
    rows = [{"clause_id": c["clause_id"], "clause_type": c.get("clause_type") or "other",
             "title": c.get("title") or "", "text": c.get("text") or "",
             "section_header": c.get("section_header"),
             "page_number": c.get("page_number"),
             "rule_generation_status": "pending",
             "extraction_confidence": 1.0} for c in clauses]
    try:
        gen = ValidationRuleGenerator(tenant_id=tenant_id, program_id=program_id)
        out = gen.run_pipeline_2(rows, template_fields, contract_id, tenant_id=tenant_id)
    except Exception as e:  # noqa: BLE001 — reported, never raised into the screen
        log.warning("mid-term change: checks for %d clause(s) not worked out: %s",
                    len(rows), e)
        return {}, {}, AI_UNAVAILABLE
    ids = {c["clause_id"] for c in clauses}
    by_clause: dict = {cid: [] for cid in ids}
    for r in out.get("validation_rules") or []:
        if (isinstance(r, dict) and r.get("source_clause_id") in ids
                and r.get("rule_source") != "generic_library"):
            by_clause[r["source_clause_id"]].append(r)
    unbound: dict = {}
    for q in out.get("review_queue") or []:
        if not isinstance(q, dict):
            continue
        try:
            cid = int(q.get("clause_id"))
        except (TypeError, ValueError):
            continue
        if cid in ids and not by_clause.get(cid) \
                and not str(q.get("clause_text") or "").startswith("[Generic rule]"):
            unbound[cid] = str(q.get("reason") or "no column in this template measures it")
    return by_clause, unbound, None
