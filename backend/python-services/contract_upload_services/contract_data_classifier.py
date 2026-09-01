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

import pipeline_log as plog

from contract_upload_services.gemini_service import (
    call_gemini, DETERMINISTIC_SEED, plan_token_batches, would_truncate,
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

# Answer/data multiple for THIS stage, fed to would_truncate(). Measured 1.46-2.41
# over 14 real calls; 2.5 sits just above the observed max. Stage 2 answers run
# large relative to their input because every clause comes back with a verdict plus
# a full intent object even when the clause turns out not to be rule-bearing.
_CALL2_OUTPUT_RATIO = float(os.getenv("KAVACHIO_CALL2_OUTPUT_RATIO", "2.5"))

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
    batches = _plan_batches(clauses, batch_size)
    n_batches = len(batches)
    for b_num, batch in enumerate(batches, 1):
        try:
            raw = call_gemini(
                build_rule_intent_prompt_batch(batch),
                label=f"Call2-Intent-{b_num}/{n_batches}",
                temperature=0,
                seed=DETERMINISTIC_SEED,
                max_output_tokens=65536,
                thinking_budget=_INTENT_THINKING_BUDGET,
                # Everything before "USER:" is the 39,024-char instruction block,
                # identical on every batch of this stage.
                cache_split="\nUSER:\n",
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


# Spot-check size: how many of the big call's "not rule-bearing" verdicts to
# re-ask in a small batch. 8 is the batch size the dilution measurements were made
# at, so a disagreement is directly comparable to the known-good configuration.
_CALL2_SPOTCHECK_N = int(os.getenv("KAVACHIO_CALL2_SPOTCHECK", "8"))
# Bounded bisection: each round re-asks at a size halfway between the last failure
# and the measured-safe floor, then verifies. Two rounds is enough to walk 55 -> 27
# -> 13 on a typical contract; the floor makes a third round pointless.
_CALL2_REASK_ROUNDS = int(os.getenv("KAVACHIO_CALL2_REASK_ROUNDS", "2"))


def _plan_batches(items, max_size):
    """Fewest batches that respect `max_size`, filled EVENLY.

    ceil(n / max_size) is the minimum batch count, and slicing at max_size does hit
    that count — but it leaves a runt: 23 items at size 18 becomes 18 + 5. Every
    batch pays the same ~9,800-token instruction prefix regardless of how many items
    it carries, so a batch of 5 costs nearly as much as a batch of 18 and buys a
    fifth as much. Worse, a thin batch is a DIFFERENT question than a full one — the
    model sees 5 clauses of context instead of 18 — so runts make results less
    consistent across the split as well as more expensive.

    Spreading evenly gives the same batch count with no runt: 23 at max 18 becomes
    12 + 11. Deterministic and order-preserving, so a re-run reproduces the split.
    """
    n = len(items)
    if n == 0:
        return []
    size = max(1, int(max_size or 1))
    k = max(1, -(-n // size))                 # minimum batches that respect the cap
    out, start = [], 0
    for i in range(k):
        take = n // k + (1 if i < n % k else 0)
        out.append(items[start:start + take])
        start += take
    return out


def _size_by_budget(clauses, diluted_at=None):
    """Pick a re-ask batch size from evidence rather than a constant.

    TWO ceilings apply, and it is worth being clear about which one actually binds:

      TOKENS — would_truncate() on this payload. For Call 2 this essentially never
        binds: a typical contract's clauses fit ~145 to a batch against the output
        budget. It is a guard against a pathological document, not the working
        constraint, and treating it as "data-driven sizing" oversells it.

      ATTENTION — the real constraint, and it has no formula. What is actually
        known: batches of 8 classify correctly; a single call diluted at 55 clauses
        on this document and at 67 historically. Everything between 12 and 55 is
        unmeasured, so any number picked there is a guess.

    So rather than guess a constant, bisect the unknown range using THIS run's own
    failure point: the single call just failed at `diluted_at` clauses, so half that
    is strictly safer than what failed and strictly larger than the 8 we have
    evidence for. The caller verifies the result (see the re-ask block in
    extract_rule_intents) and halves again if it is still diluted, so a wrong guess
    costs one round, not the rules.

    With no observed failure to bisect (`diluted_at` unset — the truncation path),
    fall back to the conservative measured-safe size.
    """
    if not clauses:
        return 1
    _over, est, limit = would_truncate(json.dumps(clauses, default=str), 0,
                                       ratio=_CALL2_OUTPUT_RATIO)
    fits = int(len(clauses) * limit / est) if est > 0 and limit > 0 else _CALL2_AUTO_BATCH
    if diluted_at and diluted_at > _CALL2_SINGLE_MAX:
        attention_cap = max(_CALL2_SINGLE_MAX, diluted_at // 2)
        why = (f"single call diluted at {diluted_at}; bisecting to {attention_cap}")
    else:
        attention_cap = _CALL2_SINGLE_MAX
        why = f"no observed failure to bisect; using measured-safe {_CALL2_SINGLE_MAX}"
    size = max(1, min(len(clauses), fits, attention_cap))
    plog.log("CALL2", "SIZING", f"{len(clauses)} clause(s) -> batch size {size}",
             f"tokens allow {fits}; attention cap {attention_cap} ({why})")
    return size


def _spot_check_diluted(clauses, results_by_id):
    """Detect the failure a completeness check cannot see.

    Returns (diluted, checked_ids) — the verdict, and the clause ids this function
    already re-asked in a small batch, so the caller does not buy them a third time.

    Call-2 dilution does not drop clauses — it ANSWERS them wrongly, marking a
    rule-bearing clause `is_rule_bearing: false`. That is a well-formed verdict, so
    the missing/errored scan above is blind to it, and every downstream stage will
    faithfully generate nothing for a clause that should have produced a rule. It is
    the one failure in this pipeline with no signal at all.

    So we buy a signal for the price of ONE small call: take the clauses the big
    call rejected — LONGEST first, because length is what dilutes attention and the
    documented loss (Authorized/Targeted/Excluded Classes carrying an embedded value
    list) was exactly that shape — and re-ask them in a batch of the size the
    original measurements were made at. If the small batch finds a rule in a clause
    the big call rejected, the big call was diluted and its whole result is suspect.

    Deliberately asymmetric: we only look for false NEGATIVES. A clause the big call
    accepted and the small batch rejects is not evidence of dilution, and acting on
    it would trade a cheap check for a coin flip.

    Disable with KAVACHIO_CALL2_SPOTCHECK=0 (accepts the single call unverified).
    """
    if _CALL2_SPOTCHECK_N <= 0:
        return False, set()
    rejected = [c for c in clauses
                if not (results_by_id.get(c["clause_id"]) or {}).get("is_rule_bearing")]
    if not rejected:
        return False, set()   # nothing was rejected, so nothing can be a false negative
    sample = sorted(rejected, key=lambda c: len(c.get("text") or ""),
                    reverse=True)[:_CALL2_SPOTCHECK_N]
    print(f"[Call 2] spot-check: re-asking {len(sample)} of {len(rejected)} rejected "
          f"clause(s) (longest first) in a batch of {len(sample)}.")
    check = _run_intent_batches(sample, len(sample))
    checked = {c["clause_id"] for c in sample}
    flipped = [c["clause_id"] for c in sample
               if (check.get(c["clause_id"]) or {}).get("is_rule_bearing")]
    plog.log("CALL2", "SPOTCHECK",
             f"re-asked {len(sample)} of {len(rejected)} rejected clause(s)",
             "longest first — length is what dilutes attention")
    if flipped:
        print(f"[Call 2] spot-check: clause(s) {flipped} are rule-bearing in a small "
              f"batch but were rejected by the single call — DILUTED.")
        plog.log("CALL2", "DILUTED",
                 f"clause(s) {flipped} rule-bearing in a batch of {len(sample)}",
                 "the single call marked them not_rule_bearing — rules would have been lost")
        # The small batch's verdicts are the better ones; keep them so the re-run
        # cannot come back worse than what we already know.
        results_by_id.update(check)
        return True, checked
    print("[Call 2] spot-check passed — single-call verdicts accepted.")
    plog.log("CALL2", "OK", f"single call accepted for all {len(clauses)} clause(s)",
             "spot-check found no false negatives")
    return False, checked


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
    # TWO independent limits, whichever trips first:
    #   COUNT — attention dilution (see above). No token arithmetic predicts it.
    #   SIZE  — the answer would not fit the output budget. The count is blind to
    #           clause LENGTH: 200 one-line clauses and 200 page-long clauses are
    #           the same number but nothing like the same answer. _CALL2_SINGLE_MAX
    #           was measured on one contract, so a contract with much longer clauses
    #           needs this second check or it silently truncates.
    # thinking_budget=0 because Call 2 runs with thinking disabled — the whole
    # output budget is available to the answer.
    if batch_size:
        print(f"\n[Call 2] Intent extraction on {total} clause(s) → forced chunks of {batch_size}.")
        results_by_id = _run_intent_batches(clauses, batch_size)
    else:
        print(f"\n[Call 2] Intent extraction on {total} clause(s) → 1 call.")
        _o, _e, _l = would_truncate(json.dumps(clauses, default=str), 0,
                                    ratio=_CALL2_OUTPUT_RATIO)
        plog.log("CALL2", "ATTEMPT", f"{total} clause(s) in ONE call",
                 f"est answer ~{_e:,} of {_l:,.0f} tok budget ({100*_e/_l:.0f}%)")
        results_by_id = _run_intent_batches(clauses, total)

        # A truncated call DROPS clauses, which is visible against the roster.
        missing = [c for c in clauses
                   if results_by_id.get(c["clause_id"], {}).get("_error")
                   or c["clause_id"] not in results_by_id]
        if missing:
            size = _size_by_budget(missing)
            print(f"[Call 2] single call incomplete ({len(missing)}/{total}); "
                  f"retrying those in budget-sized chunks of {size}.")
            plog.log("CALL2", "FALLBACK",
                     f"{len(missing)} item(s) -> {-(-len(missing)//size)} batch(es) of {size}",
                     f"single call DROPPED {len(missing)}/{total} clause(s) (truncated answer)")
            results_by_id.update(_run_intent_batches(missing, size))

        # …and then the part a roster check CANNOT catch. See _spot_check_diluted.
        else:
            _diluted, _checked = _spot_check_diluted(clauses, results_by_id)
            if _diluted:
                # Re-ask ONLY the clauses that still need a second opinion.
                #
                # Dilution produces false NEGATIVES — a rule-bearing clause marked
                # not_rule_bearing. It does not invent rules in clauses that genuinely
                # state none, which is why _spot_check_diluted looks for flips in one
                # direction only. Two consequences follow, and both cut work:
                #   • a clause the big call ACCEPTED needs no second opinion; re-running
                #     it just re-buys a verdict we already trust.
                #   • the clauses the spot-check already re-asked in a small batch
                #     carry that batch's (better) verdict already.
                # So the re-ask set is "still rejected, and not already re-checked" —
                # 23 of 55 on the measured contract, not all 55.
                recheck = [
                    c for c in clauses
                    if c["clause_id"] not in _checked
                    and not (results_by_id.get(c["clause_id"]) or {}).get("is_rule_bearing")
                ]
                # The size that just failed is the evidence we bisect from.
                failed_at = total
                for _round in range(_CALL2_REASK_ROUNDS):
                    if not recheck:
                        break
                    size = _size_by_budget(recheck, diluted_at=failed_at)
                    print(f"[Call 2] re-asking {len(recheck)} still-rejected clause(s) "
                          f"in chunks of {size} "
                          f"(skipping {total - len(recheck)} already trusted).")
                    plog.log("CALL2", "FALLBACK",
                             f"{len(recheck)} item(s) -> {-(-len(recheck)//size)} batch(es) of {size}",
                             f"round {_round + 1}/{_CALL2_REASK_ROUNDS}; "
                             f"{total - len(recheck)} of {total} already trusted "
                             f"(accepted, or re-checked by the spot-check)")
                    # Only ADOPT a verdict that finds a rule. A small batch returning
                    # "not rule-bearing" for a clause the big call also rejected is a
                    # confirmation, not new information — and must never overwrite a
                    # rule the spot-check just recovered.
                    for cid, r in _run_intent_batches(recheck, size).items():
                        if r.get("is_rule_bearing") or cid not in results_by_id:
                            results_by_id[cid] = r

                    # A bisected size is a GUESS at where attention holds, so verify
                    # it the same way the single call was verified rather than
                    # assuming. If this batch size diluted too, halve and go again;
                    # if it held, the remaining rejections are real.
                    if size <= _CALL2_SINGLE_MAX:
                        break          # already at the measured-safe size, nothing to prove
                    _still, _rechecked = _spot_check_diluted(recheck, results_by_id)
                    if not _still:
                        break
                    failed_at = size
                    recheck = [
                        c for c in recheck
                        if c["clause_id"] not in _rechecked
                        and not (results_by_id.get(c["clause_id"]) or {}).get("is_rule_bearing")
                    ]

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
