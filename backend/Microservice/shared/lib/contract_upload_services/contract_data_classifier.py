"""
contract_data_classifier.py
───────────────────────────
Pipeline 2 — Stage A: Clause Classification

For every clause produced by Pipeline 1:
  1. Build a batched classification prompt
  2. Call Gemini (Flash, cheap & fast)
  3. Per-clause result: is_rule_bearing, engine, rule_types, confidence
  4. Resolve a rule_generation_status from the result

Exposed entry points:
  - classify_clauses_batch(clauses, batch_size=8) → list[dict] (one per clause)
  - resolve_status(classification)                 → str
  - save_stage_a_output(clauses, classifications, output_dir, ...) → path
  - enrich_extracted_rules(extraction_output)      → enriches legacy rules in-place

Each clause input must contain:
  clause_id, clause_type, title, text, page_number, section_header
"""

import os
import json
import datetime

from contract_upload_services.gemini_service import (
    call_gemini, DETERMINISTIC_SEED, plan_token_batches,
)
from contract_upload_services.prompt_builder import (
    build_classification_prompt_batch,
    build_rule_intent_prompt_batch,
)

# Thinking budget for Call 2 (intent extraction). A large thinking budget on
# 2.5-flash is the main token cost AND the trigger for the rare repeat-loop
# ('0000…') output that wasted ~2 min before falling back. We DISABLE thinking
# here (0) by default — cheaper and reliable on 2.5-flash, no model upgrade
# needed. Set KAVACHIO_INTENT_THINKING_BUDGET to a positive number to re-enable
# bounded thinking if intent-extraction quality ever needs it.
_INTENT_THINKING_BUDGET = int(os.getenv("KAVACHIO_INTENT_THINKING_BUDGET", "0"))

# Call-2 auto-chunking (see extract_rule_intents). At/below _CALL2_SINGLE_MAX
# clauses a single call is reliable; above it we split into _CALL2_AUTO_BATCH-sized
# chunks so borderline value-restriction clauses aren't dropped to not_rule_bearing
# by attention dilution. Tunable via env without a code change.
_CALL2_SINGLE_MAX = int(os.getenv("KAVACHIO_CALL2_SINGLE_MAX", "12"))
_CALL2_AUTO_BATCH = int(os.getenv("KAVACHIO_CALL2_AUTO_BATCH", "8"))

# Call-1 (Stage A classification) auto-chunking. Previously ALL clauses went in a
# single call (all-or-nothing: one oversized doc lost every clause). Now we mirror
# Call 2: a single call at/below _CALL1_SINGLE_MAX, else token-budgeted chunks of
# _CALL1_AUTO_BATCH, with split-and-retry so a failed chunk never fails everything.
_CALL1_SINGLE_MAX = int(os.getenv("KAVACHIO_CALL1_SINGLE_MAX", "12"))
_CALL1_AUTO_BATCH = int(os.getenv("KAVACHIO_CALL1_AUTO_BATCH", "8"))


# =========================================================
# STATUS RESOLVER
# =========================================================

def resolve_status(classification):
    """
    Map a Stage A classification result → rule_generation_status.

      pending          → rule-bearing, confidence ≥ 0.70
      review           → rule-bearing, confidence 0.50–0.69
      not_rule_bearing → LLM says not rule-bearing
      low_confidence   → confidence < 0.50
      error            → classification call itself failed
    """

    if classification.get("_error"):
        return "error"

    if not classification.get("is_rule_bearing", False):
        return "not_rule_bearing"

    confidence = classification.get("confidence", 0.0) or 0.0

    if confidence >= 0.70:
        return "pending"

    if confidence >= 0.50:
        return "review"

    return "low_confidence"


# =========================================================
# CLAUSE CLASSIFIER (BATCHED)
# =========================================================

def _classify_error_skeleton(clause_id, reason):
    return {
        "clause_id": clause_id,
        "_error": reason,
        "is_rule_bearing": False,
        "reasoning": "Classification call failed",
        "engine": None,
        "rule_types": [],
        "rule_stage": None,
        "confidence": 0.0,
    }


def _classify_batch_with_retry(batch, results_by_id, label, depth=0):
    """Classify one batch; on failure split in half and retry each half so a
    single bad/oversized chunk never loses every clause. Only when a batch is
    down to one clause (or max depth) do we record a per-clause error — matching
    Call 2's salvage behaviour instead of the old all-or-nothing failure."""
    try:
        prompt = build_classification_prompt_batch(batch)
        raw = call_gemini(prompt, label=label)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        for r in parsed.get("results", []):
            cid = r.get("clause_id")
            if cid is not None:
                results_by_id[cid] = r
        # Retry clauses this call skipped (partial return) in a smaller chunk.
        missing = [c for c in batch if c["clause_id"] not in results_by_id]
        if missing and len(missing) < len(batch) and depth < 3:
            _classify_batch_with_retry(missing, results_by_id, f"{label}-miss", depth + 1)
    except Exception as exc:
        print(f"[Stage A] {label} failed ({len(batch)} clauses): {exc}")
        if len(batch) > 1 and depth < 4:
            mid = len(batch) // 2
            _classify_batch_with_retry(batch[:mid], results_by_id, f"{label}a", depth + 1)
            _classify_batch_with_retry(batch[mid:], results_by_id, f"{label}b", depth + 1)
        else:
            for c in batch:
                results_by_id.setdefault(
                    c["clause_id"], _classify_error_skeleton(c["clause_id"], str(exc)))


def classify_clauses_batch(clauses, batch_size=8):
    """
    Classify a list of clauses in batches.
    Each clause must have: clause_id, clause_type, title, text.
    Returns: list[dict] aligned with `clauses`, each:
        {
          "clause_id": ...,
          "is_rule_bearing": bool,
          "engine": "ajv"|"custom"|None,
          "rule_types": [...],
          "rule_stage": "input"|"output"|"both"|None,
          "confidence": float,
          "reasoning": str
        }
    """

    if not clauses:
        return []

    results_by_id = {}

    # Size batches: a single call when small enough, else token-budgeted chunks
    # capped at _CALL1_AUTO_BATCH. This closes the last un-chunked pipeline call.
    hard_max = batch_size or _CALL1_AUTO_BATCH
    if len(clauses) <= _CALL1_SINGLE_MAX:
        batches = [clauses]
    else:
        batches = plan_token_batches(
            clauses,
            text_of=lambda c: f"{c.get('title', '')} {c.get('text', '')}",
            hard_max_items=hard_max,
        )

    for bnum, batch in enumerate(batches, 1):
        print(f"\n[Stage A] Classifying batch {bnum}/{len(batches)} "
              f"({len(batch)} clauses)...")
        _classify_batch_with_retry(batch, results_by_id, f"StageA-Batch{bnum}")

    # Preserve input order, fill any gaps with not_rule_bearing skeletons
    ordered = []

    for c in clauses:

        cid = c["clause_id"]

        if cid in results_by_id:
            ordered.append(results_by_id[cid])

        else:

            ordered.append({
                "clause_id": cid,
                "is_rule_bearing": False,
                "reasoning": "No classification result returned",
                "engine": None,
                "rule_types": [],
                "rule_stage": None,
                "confidence": 0.0
            })

    return ordered


# =========================================================
# CALL 2 (3-call model): rule_bearing + rule INTENT in one call
# Merges classification (is_rule_bearing) with field-agnostic intent extraction
# (subject / operator / value / scope). Output is classification-shaped (so the
# rest of the pipeline + summaries keep working) PLUS an `intents` list that
# Call 3 maps to Output-Template fields.
# =========================================================

def _run_intent_batches(clauses, batch_size):
    """Run Call-2 intent extraction over `clauses` in batches; return
    {clause_id: result}. A failed/truncated batch marks its clauses with _error."""
    results_by_id = {}
    total = len(clauses)
    n_batches = (total + batch_size - 1) // batch_size
    for b in range(0, total, batch_size):
        batch = clauses[b:b + batch_size]
        b_num = b // batch_size + 1
        try:
            raw = call_gemini(
                build_rule_intent_prompt_batch(batch),
                label=f"Call2-Intent-{b_num}/{n_batches}",
                temperature=0,
                seed=DETERMINISTIC_SEED,
                max_output_tokens=65536,
                thinking_budget=_INTENT_THINKING_BUDGET,
            )
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            for r in (parsed.get("results", []) if isinstance(parsed, dict) else []):
                cid = r.get("clause_id")
                if cid is None:
                    continue
                intents = r.get("intents") or []
                results_by_id[cid] = {
                    "clause_id": cid,
                    "is_rule_bearing": bool(r.get("is_rule_bearing")) and len(intents) > 0,
                    "reasoning": r.get("reasoning", ""),
                    "intents": intents,
                    "rule_types": [i.get("operator") for i in intents if i.get("operator")],
                    "confidence": 1.0 if intents else 0.0,
                }
        except Exception as exc:
            print(f"[Call 2] batch {b_num}/{n_batches} failed: {exc}")
            for c in batch:
                results_by_id[c["clause_id"]] = {
                    "clause_id": c["clause_id"], "_error": str(exc),
                    "is_rule_bearing": False, "reasoning": "Call 2 failed",
                    "intents": [], "rule_types": [], "confidence": 0.0,
                }
    return results_by_id


def extract_rule_intents(clauses, batch_size=None):
    """Gemini call: classify rule-bearing AND extract rule intents.

    By default this is ONE call for all clauses. A 2.5-flash run spends a large
    "thinking" budget against max_output_tokens, so a single call CAN truncate;
    if that happens we automatically retry the missing/errored clauses in chunks
    (so "one call" is the normal case but nothing is lost). Pass a number to
    `batch_size` to force chunking from the start.

    Returns a list aligned with `clauses` (see result shape below).
    """
    if not clauses:
        return []
    total = len(clauses)

    # Auto-chunk large documents. A single call over many long clauses dilutes the
    # model's attention and silently drops borderline value-restriction clauses
    # (e.g. Authorized/Targeted/Excluded Classes that reference an external guide
    # but carry their value list in an embedded "[Context from …]" block) to
    # not_rule_bearing. Those clauses are ANSWERED — just wrongly — so the
    # missing/errored retry below never recovers them. Empirically a 67-clause
    # single call marked the class clauses not_rule_bearing, while chunks of 8
    # classified the exact same clauses correctly. So for large docs we proactively
    # chunk instead of relying on one call. Small docs keep the single-call path.
    if batch_size is None and total > _CALL2_SINGLE_MAX:
        batch_size = _CALL2_AUTO_BATCH
        print(f"[Call 2] {total} clauses > {_CALL2_SINGLE_MAX}; auto-chunking to "
              f"avoid attention dilution.")

    if batch_size:
        print(f"\n[Call 2] Intent extraction on {total} clause(s) → chunks of {batch_size}.")
        results_by_id = _run_intent_batches(clauses, batch_size)
    else:
        print(f"\n[Call 2] Intent extraction on {total} clause(s) → 1 call.")
        results_by_id = _run_intent_batches(clauses, total)
        # If the single call truncated, retry just the missing/errored clauses.
        missing = [c for c in clauses
                   if results_by_id.get(c["clause_id"], {}).get("_error")
                   or c["clause_id"] not in results_by_id]
        if missing:
            print(f"[Call 2] single call incomplete ({len(missing)}/{total}); "
                  f"retrying those in chunks of 10.")
            results_by_id.update(_run_intent_batches(missing, 10))

    ordered = []
    for c in clauses:
        cid = c["clause_id"]
        ordered.append(results_by_id.get(cid, {
            "clause_id": cid, "is_rule_bearing": False,
            "reasoning": "No result returned", "intents": [],
            "rule_types": [], "confidence": 0.0,
        }))
    return ordered


# =========================================================
# Stage A output persistence
# =========================================================

def _summarize_classifications(classifications):
    """Lightweight stats embedded in the saved Stage A JSON."""

    total = len(classifications)

    return {
        "total_clauses":    total,
        "rule_bearing":     sum(
            1 for c in classifications if c.get("is_rule_bearing")
        ),
        "not_rule_bearing": sum(
            1 for c in classifications
            if not c.get("is_rule_bearing") and not c.get("_error")
        ),
        "errors":           sum(1 for c in classifications if c.get("_error")),
        "ajv":              sum(
            1 for c in classifications if c.get("engine") == "ajv"
        ),
        "custom":           sum(
            1 for c in classifications if c.get("engine") == "custom"
        )
    }


def save_stage_a_output(
    clauses,
    classifications,
    output_dir,
    contract_id="contract",
    file_base=None
):
    """
    Save Stage A clause-classification output to a JSON file alongside the
    Stage B side-car files.

    File written (inside `output_dir`):
      <file_base>_stage_a_classification.json

    Payload:
      {
        "stage": "2.2 — Stage A: Clause classification",
        "contract_id": ...,
        "generated_at": ISO timestamp,
        "summary": {total_clauses, rule_bearing, not_rule_bearing, errors, ajv, custom},
        "entries": [
          {"clause": {... "classification": ...}, "rule_generation_status": ...}
        ]
      }

    The classification is carried INSIDE each clause (clause["classification"])
    rather than as a separate top-level key, to avoid duplicating it.

    Returns: the written file path.
    """

    os.makedirs(output_dir, exist_ok=True)

    if not file_base:
        file_base = contract_id or "contract"

    entries = [
        {
            "clause":                  {**clause, "classification": classification},
            "rule_generation_status":  resolve_status(classification)
        }
        for clause, classification in zip(clauses, classifications)
    ]

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    path = os.path.join(output_dir, f"{file_base}_stage_a_classification.json")

    payload = {
        "stage":        "2.2 — Stage A: Clause classification",
        "contract_id":  contract_id,
        "generated_at": now,
        "summary":      _summarize_classifications(classifications),
        "entries":      entries
    }

    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    summary = payload["summary"]
    print(
        f"[Stage A] saved classification → {path} "
        f"(rule_bearing={summary['rule_bearing']}, "
        f"not_bearing={summary['not_rule_bearing']}, "
        f"errors={summary['errors']} "
        f"across {summary['total_clauses']} clauses)"
    )

    return path


# =========================================================
# LEGACY: enrich an extraction_output (class_name rules)
# =========================================================

def enrich_extracted_rules(extraction_output):
    """
    Backwards-compatible: takes the legacy contract_rules array
    (with class_name + clause objects) and adds a `classification`
    block to each rule plus updates clause.rule_generation_status.
    """

    rules = (
        extraction_output
        .get("extraction_output", extraction_output)
        .get("contract_rules", [])
    )

    if not rules:
        return extraction_output

    # Build a clause batch from the legacy rules' embedded clause blocks
    clauses_for_batch = []

    for idx, rule in enumerate(rules):

        clause = rule.get("clause", {}) or {}

        clauses_for_batch.append({
            "clause_id": idx,
            "clause_type": _clause_type_from_class(rule.get("class_name", "")),
            "title": clause.get("title", rule.get("rule_name", "")),
            "text": clause.get("text", ""),
            "page_number": clause.get("page_number"),
            "section_header": clause.get("section_header")
        })

    classifications = classify_clauses_batch(clauses_for_batch)

    passed = skipped = errors = 0

    for rule, classification in zip(rules, classifications):

        rule["classification"] = classification

        new_status = resolve_status(classification)

        rule.setdefault("clause", {})
        rule["clause"]["rule_generation_status"] = new_status

        if classification.get("_error"):
            errors += 1
        elif classification.get("is_rule_bearing"):
            passed += 1
        else:
            skipped += 1

    target = extraction_output.get("extraction_output", extraction_output)

    target.setdefault("metadata", {})
    target["metadata"]["stage_a_summary"] = {
        "total_rules": len(rules),
        "rule_bearing": passed,
        "not_rule_bearing": skipped,
        "errors": errors,
        "processed_at": datetime.datetime.utcnow().isoformat() + "Z"
    }

    print(
        f"\n[Stage A] Done: "
        f"rule_bearing={passed}, "
        f"not_bearing={skipped}, "
        f"errors={errors}"
    )

    return extraction_output


def _clause_type_from_class(class_name):
    """Heuristic mapping legacy class_name → spec clause_type."""

    return {
        "InRange":                    "limit",
        "AcceptedValues":             "exclusion",
        "ParticipantLimitMax":        "limit",
        "ParticipantPctShare":        "limit",
        "CommissionAmt":              "limit",
        "BrokerFeeAmt":               "limit",
        "StateCode":                  "exclusion",
        "ApprovedOriginatingCompany": "exclusion",
        "NotNull":                    "mandatory_field",
        "PatternCheck":               "mandatory_field",
        "PolicyPeriod":               "limit",
        "TransactionEffectiveDate":   "limit"
    }.get(class_name, "other")
