"""
stage_b_synthesizer.py
──────────────────────
Pipeline 2 — Stage B: rule synthesis (LLM calls #3 and #4).

After Stage A classifies clauses, Stage B turns each rule-bearing clause
into one or more concrete rule candidates.

BATCHED (default, recommended):
  All AJV clauses are synthesized in a single batched Gemini call (or a few
  batches when the clause count exceeds `batch_size`). Same for custom clauses.
  This reduces token overhead, latency, and API cost by ~70% vs per-clause.

  - engine='ajv'    → synthesize_ajv_rules(clauses, classifications)
  - engine='custom' → synthesize_custom_rules(clauses, classifications)

Public entry point (unchanged — nothing outside needs to change):
  synthesize_rules(clauses, classifications)
    → list of {clause, classification, engine, candidates[]}

Each candidate still needs to be passed through
rule_normalizer.normalize_ajv_rule / normalize_custom_rule before
it becomes a validation_rule row.

Stage B raw outputs can be persisted to disk via
`save_stage_b_outputs(synth_outputs, output_dir, contract_id)`.
"""

import os
import json
import datetime

import pipeline_log as plog

from contract_upload_services.gemini_service import (
    call_gemini, DETERMINISTIC_SEED, STAGE_B_MODEL, plan_token_batches,
    would_truncate,
)
import os as _os
import json as _json
import re as _re

# IR-synthesis output budget. Raised from the old fixed 8192 (which truncated a
# batch of 6 complex clauses → all rules lost) and clamped to the model ceiling
# by the gateway. Truncation is now recovered by halve-and-retry, not dropped.
_IR_MAX_OUTPUT = int(_os.getenv("KAVACHIO_IR_MAX_OUTPUT", "16384"))

from contract_upload_services.prompt_builder import (
    build_ajv_synthesis_prompt_for_template_batch,
    build_custom_synthesis_prompt_for_template_batch,
    build_ir_synthesis_prompt_batch,
    build_ir_mapping_prompt_batch,
)

# Call-3 (intent→IR mapping) auto-chunking. At/below _CALL3_SINGLE_MAX intents a
# single mapping call is reliable; above it the one call dilutes the model's
# attention and it silently leaves intents UNMAPPED (→ review) even when a perfectly
# good column exists — e.g. limit/aggregate_cap clauses punted with "no field". A
# real RiskSmith upload sent ~49 intents in one call → 39 went to review; the same
# intents in chunks map cleanly. Mirrors the Call-2 auto-chunk. Env-tunable.
_CALL3_SINGLE_MAX = int(os.getenv("KAVACHIO_CALL3_SINGLE_MAX", "12"))
_CALL3_AUTO_BATCH = int(os.getenv("KAVACHIO_CALL3_AUTO_BATCH", "8"))
# Answer/data multiple for THIS stage, fed to would_truncate(). Measured 0.06-0.82
# over 59 real calls — Stage 3 is the one stage that SHRINKS its input: a verbose
# intent object (clause text, rationale, candidate fields) compiles down to a terse
# IR row. 1.0 sits above the observed max. Note this covers the ANSWER only; the
# thinking budget is passed to would_truncate separately and must not be folded in
# here, or it is counted twice and the gate chunks ~5x earlier than it needs to.
_CALL3_OUTPUT_RATIO = float(os.getenv("KAVACHIO_CALL3_OUTPUT_RATIO", "1.0"))
# Auto-retry (attempt 2): a small temperature bump so the relaxed re-map can
# actually differ from the deterministic first pass (temp 0 + fixed seed would
# otherwise reproduce the same "no field" result). Deterministic verify still
# gates whatever the retry picks, so a non-zero temp here is safe.
_CALL3_RETRY_TEMP = float(os.getenv("KAVACHIO_CALL3_RETRY_TEMP", "0.4"))
# Hard cap on the optimistic path's fallback ladder. Bounds the worst case: even if
# every round fails, Call 3 can cost at most one big call plus this many retry
# rounds — never an unbounded halving loop.
_CALL3_FALLBACK_ROUNDS = int(os.getenv("KAVACHIO_CALL3_FALLBACK_ROUNDS", "3"))


def _safe_parse(raw: str, label: str) -> dict | None:
    """Parse Gemini JSON, stripping JS-style // comments that break stdlib json.

    Recovery ladder, because throwing away a COMPLETE answer over one stray byte
    costs a full round of fallback calls:
      1. strict=False — the model routinely puts a literal newline or tab inside a
         string value (an error_message spanning two lines). stdlib json rejects
         raw control characters by default; they are harmless here.
      2. json-repair — same library mapper._lenient_json_loads relies on, for a
         missing brace or a trailing comma.
    Only when both fail do we give up and let the caller re-run in chunks.
    """
    text = raw if isinstance(raw, str) else json.dumps(raw)
    # Remove // line comments (Gemini sometimes writes these inside JSON)
    text = _re.sub(r"//[^\n]*", "", text)

    def _wrap(parsed):
        # Normalise bare list → {"results": [...]} or {"rules": [...]}
        if isinstance(parsed, list):
            # Heuristic: list of rule objects → wrap as rules
            return {"rules": parsed}
        return parsed

    try:
        return _wrap(json.loads(text, strict=False))
    except Exception as exc:
        first_error = exc

    try:
        import json_repair
        recovered = json_repair.loads(text)
        if recovered:
            print(f"[{label}] JSON repaired after: {first_error}")
            return _wrap(recovered)
    except Exception:
        pass

    print(f"INVALID JSON FROM GEMINI ({label}), first 2000 chars:\n{text[:2000]}")
    print(f"[{label}] parse error: {first_error}")
    return None


# =========================================================
# Stage B-A BATCHED: AJV synthesis (N clauses → 1 Gemini call)
# =========================================================

def synthesize_ajv_rules_batch(clauses, classifications, batch_size=8, template_fields=None):
    """
    Synthesize AJV rules for all `clauses` in a single template-aware batch.

    `template_fields` (list of Output Template field dicts) is REQUIRED — rules
    must target Output Template column names, since validation runs on the
    rendered output, not the internal data model. Without it we raise rather
    than silently produce unusable data-model-targeted rules.

    Returns a dict keyed by clause_id:
        { clause_id: [candidate, ...], ... }
    """
    if not template_fields:
        raise ValueError(
            "synthesize_ajv_rules_batch requires template_fields — "
            "output-template-aware synthesis is mandatory."
        )

    results_by_id = {}
    clauses_meta = _build_clauses_meta(clauses, classifications)
    batch_size = len(clauses_meta) or 1  # single Gemini call

    for batch_start in range(0, len(clauses_meta), batch_size):

        batch = clauses_meta[batch_start:batch_start + batch_size]
        batch_num = batch_start // batch_size + 1
        total_batches = (len(clauses_meta) + batch_size - 1) // batch_size

        print(
            f"\n[Stage B-AJV] Batch {batch_num}/{total_batches} "
            f"({len(batch)} clause(s), template-aware)..."
        )

        prompt = build_ajv_synthesis_prompt_for_template_batch(batch, template_fields)
        raw    = call_gemini(prompt, label=f"StageB-AJV-T-Batch{batch_num}")
        parsed = _safe_parse(raw, f"StageB-AJV-T-Batch{batch_num}")
        if parsed:
            for result in parsed.get("results", []):
                cid = result.get("clause_id")
                if cid is not None:
                    results_by_id[cid] = result.get("rules", []) or []
        for cm in batch:
            results_by_id.setdefault(cm["clause_id"], [])

    for cm in clauses_meta:
        results_by_id.setdefault(cm["clause_id"], [])

    return results_by_id


def synthesize_ajv_rules(clauses, classifications, batch_size=8, template_fields=None):
    """Backward-compatible wrapper for callers using the original name."""
    return synthesize_ajv_rules_batch(
        clauses,
        classifications,
        batch_size=batch_size,
        template_fields=template_fields,
    )


# =========================================================
# Stage B-B BATCHED: Custom synthesis (N clauses → 1 Gemini call)
# =========================================================

def synthesize_custom_rules_batch(clauses, classifications, batch_size=8, template_fields=None):
    """
    Synthesize Custom DSL rules for all `clauses` in a single template-aware batch.

    `template_fields` is REQUIRED (see `synthesize_ajv_rules_batch`): custom
    rules must also target Output Template field names. We raise rather than
    fall back to data-model-targeted synthesis.

    Returns a dict keyed by clause_id:
        { clause_id: [candidate, ...], ... }
    """
    if not template_fields:
        raise ValueError(
            "synthesize_custom_rules_batch requires template_fields — "
            "output-template-aware synthesis is mandatory."
        )

    results_by_id = {}
    clauses_meta = _build_clauses_meta(clauses, classifications)
    batch_size = len(clauses_meta) or 1  # single Gemini call

    for batch_start in range(0, len(clauses_meta), batch_size):

        batch = clauses_meta[batch_start:batch_start + batch_size]
        batch_num = batch_start // batch_size + 1
        total_batches = (len(clauses_meta) + batch_size - 1) // batch_size

        print(
            f"\n[Stage B-Custom] Batch {batch_num}/{total_batches} "
            f"({len(batch)} clause(s), template-aware)..."
        )

        prompt = build_custom_synthesis_prompt_for_template_batch(batch, template_fields)
        raw    = call_gemini(prompt, label=f"StageB-Custom-T-Batch{batch_num}")
        parsed = _safe_parse(raw, f"StageB-Custom-T-Batch{batch_num}")
        if parsed:
            for result in parsed.get("results", []):
                cid = result.get("clause_id")
                if cid is not None:
                    results_by_id[cid] = result.get("rules", []) or []
        for cm in batch:
            results_by_id.setdefault(cm["clause_id"], [])

    for cm in clauses_meta:
        results_by_id.setdefault(cm["clause_id"], [])

    return results_by_id


def synthesize_custom_rules(clauses, classifications, batch_size=8, template_fields=None):
    """Backward-compatible wrapper for callers using the original name."""
    return synthesize_custom_rules_batch(
        clauses,
        classifications,
        batch_size=batch_size,
        template_fields=template_fields,
    )


# =========================================================
# Stage B (IR): deterministic-IR extraction — single batched call
# The LLM picks ONE template per rule and fills its params; it never authors
# SQL/JSON-Schema. This replaces the ajv/custom split (the engine field is no
# longer a routing signal — every rule compiles to one DuckDB engine).
# =========================================================

# IR envelope keys — everything else the model emits is treated as a param and
# folded into `params`. This makes extraction robust to the model placing a
# param (commonly `scope`) as a sibling of `params` instead of inside it, which
# would otherwise silently drop e.g. a coverage-type restriction.
_IR_ENVELOPE_KEYS = {
    "template", "params", "rule_name", "rule_description", "severity",
    "error_message", "confidence", "reason", "citation", "stage", "polarity",
}


def _coerce_ir_envelope(rule: dict) -> dict:
    if not isinstance(rule, dict):
        return rule
    params = dict(rule.get("params") or {})
    for k, v in rule.items():
        if k in _IR_ENVELOPE_KEYS or k == "params":
            continue
        # Don't clobber a value the model already put inside params.
        params.setdefault(k, v)
    cleaned = {k: v for k, v in rule.items() if k in _IR_ENVELOPE_KEYS}
    cleaned["params"] = params
    return cleaned


def _synthesize_ir_batch(batch, template_fields, candidates_by_id, label, depth=0):
    """Extract IR for one batch. On API error OR truncated/partial output, split
    and retry the unresolved clauses (bounded depth) so a truncated batch of 6
    no longer loses all 6 rules. Only genuinely unresolvable clauses (single
    clause, retries exhausted) route to review — with an explicit reason instead
    of a silent drop.

    NOTE: still NO response_schema — `params` is polymorphic and JSON-schema
    structured output would wipe it. Determinism comes from temperature=0 + seed.
    """
    parsed = None
    try:
        raw = call_gemini(
            build_ir_synthesis_prompt_batch(batch, template_fields),
            label=label, temperature=0, seed=DETERMINISTIC_SEED,
            max_output_tokens=_IR_MAX_OUTPUT, model=STAGE_B_MODEL,
        )
        parsed = _safe_parse(raw, label)
    except Exception as exc:
        print(f"[Stage B-IR] {label} failed ({exc})")

    got = set()
    if parsed:
        for result in parsed.get("results", []):
            cid = result.get("clause_id")
            if cid is not None:
                candidates_by_id[cid] = [
                    _coerce_ir_envelope(r) for r in (result.get("rules") or [])
                ]
                got.add(cid)

    missing = [cm for cm in batch if cm["clause_id"] not in got]
    if not missing:
        return
    if len(batch) > 1 and depth < 4:
        # Total failure → split in half; partial → retry just the smaller missing set.
        retry_set = missing if 0 < len(missing) < len(batch) else None
        if retry_set is not None and depth < 3:
            _synthesize_ir_batch(retry_set, template_fields, candidates_by_id,
                                 f"{label}-miss", depth + 1)
        else:
            mid = len(batch) // 2
            _synthesize_ir_batch(batch[:mid], template_fields, candidates_by_id,
                                 f"{label}a", depth + 1)
            _synthesize_ir_batch(batch[mid:], template_fields, candidates_by_id,
                                 f"{label}b", depth + 1)
        missing = [cm for cm in batch if cm["clause_id"] not in candidates_by_id]
    for cm in missing:
        candidates_by_id.setdefault(cm["clause_id"], [])
        print(f"[Stage B-IR] clause {cm['clause_id']} → review (no IR after retries)")


def synthesize_rules_ir(clauses, classifications, template_fields=None, batch_size=6):
    """Extract an IR for every rule-bearing clause (combined extract+map path).

    Returns: list of {clause, classification, engine: 'ir'|None, candidates: [IR,...]}
    in the original input order. Non-rule-bearing / errored clauses get
    engine=None and no candidates.
    """
    if len(clauses) != len(classifications):
        raise ValueError("synthesize_rules_ir: clauses/classifications length mismatch")
    if not template_fields:
        raise ValueError(
            "synthesize_rules_ir requires template_fields — rules must target "
            "Output Template fields."
        )

    # Partition: only rule-bearing, non-errored clauses go to the extractor.
    bearing_clauses, bearing_clfs = [], []
    for clause, clf in zip(clauses, classifications):
        if clf.get("_error") or not clf.get("is_rule_bearing"):
            continue
        bearing_clauses.append(clause)
        bearing_clfs.append(clf)

    candidates_by_id = {}
    if bearing_clauses:
        clauses_meta = _build_clauses_meta(bearing_clauses, bearing_clfs)
        total = len(clauses_meta)
        # Token-size the batches (cap at batch_size items) so a batch of large
        # clauses can't overflow the output budget; small clauses pack together.
        batches = plan_token_batches(
            clauses_meta,
            text_of=lambda cm: _json.dumps(cm, default=str),
            hard_max_items=batch_size,
            model=STAGE_B_MODEL,
        )
        print(
            f"\n[Stage B-IR] Extracting IR for {total} rule-bearing clause(s) → "
            f"{len(batches)} batched Gemini call(s) of <= {batch_size} (temp 0, seed)"
        )
        for b_num, batch in enumerate(batches, 1):
            _synthesize_ir_batch(
                batch, template_fields, candidates_by_id,
                f"StageB-IR-{b_num}/{len(batches)}")

    # Reassemble in original order.
    output = []
    bearing_ids = {c.get("clause_id") for c in bearing_clauses}
    for clause, clf in zip(clauses, classifications):
        cid = clause.get("clause_id")
        if cid in bearing_ids:
            output.append({
                "clause": clause,
                "classification": clf,
                "engine": "ir",
                "candidates": candidates_by_id.get(cid, []),
            })
        else:
            output.append({
                "clause": clause,
                "classification": clf,
                "engine": None,
                "candidates": [],
            })
    return output


# =========================================================
# CALL 3 (3-call model): map rule INTENTS → IR (template + Output fields)
# Consumes the intents produced by Call 2 (extract_rule_intents) and produces
# the same synth_outputs shape normalize_ir_outputs already expects:
#   [{clause, classification, engine: 'ir'|None, candidates: [IR, ...]}]
# Each IR = {template, params, rule_name, rule_description, severity,
#            error_message, confidence, reason}.
# =========================================================

def _run_mapping_batches(items, template_fields, batch_size, forced_field=None,
                         relaxed=False, temperature=0, forced_fields=None):
    """Run Call-3 mapping over flattened intent `items` in batches; return
    {(clause_id, intent_index): IR}. A failed/truncated batch is simply absent
    from the result (its intents route to review).

    `forced_field` (human resolution): direct the mapper to bind these intents'
    primary value to that Output-Template column (see build_ir_mapping_prompt_batch).
    `relaxed` (auto-retry attempt 2): emit the relaxed "reconsider" prompt that
    binds each intent to the CLOSEST representable field instead of returning null.
    `temperature`: 0 for the deterministic first pass; a small bump on the relaxed
    retry so its output can differ."""
    mapped = {}
    total = len(items)
    # Fewest batches that respect the cap, filled EVENLY — see _plan_batches. A
    # Call-3 batch carries a ~15,800-token prefix whatever it holds, so a runt batch
    # is the most expensive way to ask about a handful of intents.
    from contract_upload_services.contract_data_classifier import _plan_batches
    batches = _plan_batches(items, batch_size)
    n_batches = len(batches)
    for b_num, batch in enumerate(batches, 1):
        parsed = None
        # A batch is generic-library-sourced when EVERY item carries a negative
        # clause_id (see generic_rule_library._build_intents) — these calls are
        # never mixed with contract-derived intents, so this is all-or-nothing.
        is_generic_batch = bool(batch) and all(it["clause_id"] < 0 for it in batch)
        try:
            raw = call_gemini(
                build_ir_mapping_prompt_batch(batch, template_fields, forced_field,
                                              relaxed=relaxed,
                                              forced_fields=forced_fields,
                                              is_generic=is_generic_batch),
                label=f"Call3-Map{'-retry' if relaxed else ''}-{b_num}/{n_batches}",
                temperature=temperature,
                seed=DETERMINISTIC_SEED,
                max_output_tokens=65536,
                thinking_budget=16384,
                # Everything before "USER:" is the ~49,000-char catalog + field list
                # + instructions. Identical across all four prompt variants now that
                # forced/relaxed/generic blocks sit at the tail.
                cache_split="\nUSER:\n",
            )
            parsed = _safe_parse(raw, f"Call3-Map-{b_num}")
        except Exception as exc:
            print(f"[Call 3] batch {b_num}/{n_batches} failed ({exc}); "
                  f"its intents route to review.")
        if parsed:
            for r in parsed.get("results", []):
                key = (r.get("clause_id"), r.get("intent_index"))
                if key[0] is None:
                    continue
                mapped[key] = {
                    "template":         r.get("template"),
                    "params":           r.get("params") or {},
                    "rule_name":        r.get("rule_name"),
                    "rule_description": r.get("rule_description"),
                    "severity":         r.get("severity"),
                    "error_message":    r.get("error_message"),
                    "confidence":       r.get("confidence"),
                    "reason":           r.get("reason"),
                    # Cross-sheet same-meaning columns for the bound field,
                    # reported by the SAME mapping call (no separate LLM pass) —
                    # harvested + guard-checked in rule_normalizer, then merged
                    # into OutputSchema.field_aliases for compile fan-out.
                    "field_aliases":    r.get("field_aliases"),
                }
    return mapped


# Deterministic guard: re-point an OWN-SHARE numeric limit that the LLM mapped
# onto a WHOLE-POLICY column ("100% …"/"Total …") back to the participant's own
# share column. Generic (token-based, no contract specifics) — fixes the
# recurring "$X Company/program limit → 100% policy Limit" mis-bind regardless of
# what the prompt does.
_LIMIT_TEMPLATES = {"max_limit", "min_limit", "range_check"}
_WHOLE_TOKENS = ("100%", "total", "whole")
_SHARE_FIELD_TOKENS = ("part", "net", "share", "participation", "retention")
# Clause/intent wording that means the amount is ONE party's own slice.
_SHARE_CLAUSE_RE = _re.compile(
    r"\b(compan|program|net|retention|participation|our|fronting)\b|\bpart of\b|\bshare\b",
    _re.I,
)
_CORE_METRIC = ("limit", "occurrence", "aggregate", "sublimit",
                "premium", "fee", "deductible", "retention")


def _field_tokens(name):
    return set(_re.findall(r"[a-z0-9%]+", (name or "").lower()))


def _repoint_own_share_field(ir, intent, clause, template_fields):
    """If a numeric-limit IR was bound to a whole-policy ("100%"/"Total") column
    but the clause is about a PARTY'S OWN share, re-point it to the matching
    own-share column. No-op otherwise. Returns the (possibly modified) ir."""
    if not isinstance(ir, dict) or ir.get("template") not in _LIMIT_TEMPLATES:
        return ir
    params = ir.get("params") or {}
    field = params.get("field")
    if not field:
        return ir
    fl = field.lower()
    # Act ONLY when the chosen field is a whole-policy column (100%/total/whole).
    # ("100%" contains "%", so a plain `"%" in fl` test would wrongly skip it.)
    if not any(tok in fl for tok in _WHOLE_TOKENS):
        return ir

    ctx = " ".join(str(x or "") for x in (
        clause.get("title"), clause.get("text"),
        intent.get("subject"), intent.get("rule_name"), intent.get("scope")))
    if not _SHARE_CLAUSE_RE.search(ctx):            # clause isn't an own-share one
        return ir

    # The participant/program prefix = the most common FIRST word across the
    # template's field names (e.g. the fronting carrier / MGA name). Lets us
    # recognise own-share columns by prefix even when they lack a part/net/share
    # word — generic, no hard-coded carrier name.
    from collections import Counter
    firsts = Counter()
    for f in (template_fields or []):
        nm = (f.get("name") or "").strip()
        first = nm.split()[0].lower() if nm else ""
        if first and not any(w in first for w in _WHOLE_TOKENS):
            firsts[first] += 1
    prefix = firsts.most_common(1)[0][0] if firsts else None

    chosen_core = _field_tokens(field) & set(_CORE_METRIC)
    candidates = []
    for f in (template_fields or []):
        nm = f.get("name") or ""
        nl = nm.lower()
        if nm == field or "%" in nl:                # keep $ basis; skip % columns
            continue
        if any(w in nl for w in _WHOLE_TOKENS):     # not another whole-policy col
            continue
        is_share = any(t in nl for t in _SHARE_FIELD_TOKENS) or (
            prefix is not None and nl.startswith(prefix))
        if not is_share:                            # must be an own-share col
            continue
        if chosen_core and not (_field_tokens(nm) & chosen_core):  # same metric
            continue
        candidates.append(nm)

    if candidates:
        best = sorted(candidates,
                      key=lambda n: (-len(_field_tokens(n) & chosen_core), len(n)))[0]
        if best != field:
            params["field"] = best
            ir["params"] = params
            ir["_repointed_field"] = {
                "from": field, "to": best,
                "reason": "own-share limit re-pointed off a 100%/total column",
            }
            print(f"  [guard] re-pointed own-share limit {field!r} → {best!r}")
    return ir


# Intent wording that marks a numeric LIMIT / CAP (max/min ceiling on an amount).
_LIMIT_INTENT_RE = _re.compile(
    r"\b(limit|maximum|max|cap|ceiling|sublimit|aggregate|up to|not exceed|"
    r"no more than|per occurrence|per policy)\b", _re.I)


def _numeric_or_none(v):
    """Parse a money/number-ish value ('$40,000,000', '25M', 0.235) → float|None."""
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip().lower().replace(",", "").replace("$", "").replace(" ", "")
    mult = 1.0
    if s.endswith("m"):
        mult, s = 1_000_000.0, s[:-1]
    elif s.endswith("k"):
        mult, s = 1_000.0, s[:-1]
    elif s.endswith("bn") or s.endswith("b"):
        mult, s = 1_000_000_000.0, s.rstrip("bn")
    try:
        return float(s) * mult
    except ValueError:
        return None


def _best_limit_field_for_intent(item, template_fields):
    """For an UNMAPPED intent that is clearly a numeric LIMIT/CAP, deterministically
    choose the best-matching limit column so the rule can be FORCED onto it instead
    of being dropped to review. Generic — picks by metric/share tokens and the
    template's own party prefix, never by a hard-coded column/carrier name. Returns a
    column name, or None when this isn't a numeric-limit intent or no limit column
    exists (then it legitimately stays in review)."""
    # 1) Must look like a numeric ceiling on an amount.
    if _numeric_or_none(item.get("value")) is None:
        return None
    ctx = " ".join(str(item.get(k) or "") for k in
                   ("subject", "rule_name", "operator", "clause_text"))
    if not _LIMIT_INTENT_RE.search(ctx):
        return None

    # 2) Candidate limit columns: name carries a limit metric, $ basis (skip % cols).
    limit_metrics = {"limit", "sublimit", "occurrence", "aggregate"}
    cands = []
    for f in (template_fields or []):
        nm = f.get("name") or ""
        nl = nm.lower()
        # Skip TRUE percentage columns (e.g. "… Part of Limit %") but KEEP a
        # "100% …" whole-policy column — "100%" contains "%" yet is a $ basis.
        if "%" in nl and "100%" not in nl:
            continue
        if not (_field_tokens(nm) & limit_metrics):
            continue
        cands.append(nm)
    if not cands:
        return None

    # 3) Own-share (a party/"part"/"net"/"share" clause) vs whole-policy.
    is_share = bool(_SHARE_CLAUSE_RE.search(ctx))
    from collections import Counter
    firsts = Counter()
    for f in (template_fields or []):
        first = (f.get("name") or "").split()[:1]
        first = first[0].lower() if first else ""
        if first and not any(w in first for w in _WHOLE_TOKENS):
            firsts[first] += 1
    prefix = firsts.most_common(1)[0][0] if firsts else None

    def is_whole(nl):
        return any(w in nl for w in _WHOLE_TOKENS)

    def is_own(nl):
        return any(t in nl for t in _SHARE_FIELD_TOKENS) or (
            prefix is not None and nl.startswith(prefix))

    if is_share:
        pool = [n for n in cands if is_own(n.lower()) and not is_whole(n.lower())]
    else:
        pool = [n for n in cands if is_whole(n.lower())]
    pool = pool or cands                    # fall back to any limit column
    # Prefer an explicit $ column, then the shorter (more specific) name.
    pool.sort(key=lambda n: (0 if "$" in n else 1, len(n)))
    return pool[0]


# A refusal that blames the SAMPLE DATA rather than the column's meaning. The
# allowed / excluded values of a value-set rule come from the CONTRACT, and the
# sample rows are a handful of illustrative cells from one bordereau — so a value
# the contract authorises or forbids very often is not among them, and catching a
# row that carries an unauthorised value is the rule's whole purpose. Call 3 is
# told this outright (see "WHAT SAMPLE VALUES ARE FOR" in
# prompt_builder.build_ir_mapping_prompt_batch); this pattern is how the
# deterministic backstop below recognises the run where it refuses anyway.
# Matches the model's own phrasings — "not found in the sample values",
# "cannot be grounded from sample data", "not present in the field's samples".
_SAMPLE_GROUNDING_REFUSAL_RE = _re.compile(
    r"sample\s+(?:value|data|row)|grounded\s+(?:from|in|to)\s+.{0,20}sample"
    r"|samples\s*[:(]", _re.I)

# A refusal about MEANING — no column of this template can hold this kind of value
# at all. That IS a valid refusal (a peril, an underwriting judgement, a party no
# bordereau reports), so it stays in review even when it also mentions samples in
# passing. Checked FIRST, so only a pure sample-grounding objection is overridden.
_NO_SUCH_FIELD_RE = _re.compile(
    r"no\s+(?:suitable\s+|single\s+|specific\s+|available\s+)?(?:field|column)\b"
    r"|no\s+field\s+in\s+the\s+catalog"
    r"|cannot\s+be\s+represented|does\s+not\s+represent|represents?\s+no\b", _re.I)

# The column the refusal NAMES, taken from the grammatical slot that actually
# denotes a column — "…values for 'Risk Class'", "the 'Detailed Coverage' field",
# "…grounded to 'Domicile State'". Reading any quoted string would sometimes pick
# up one of the VALUES the refusal also quotes, so the slot is required.
_REFUSED_COLUMN_RE = _re.compile(
    r"(?:for|of|in|to|against)\s+['\"‘’“”]([^'\"‘’“”]{1,80})['\"‘’“”]"
    r"|['\"‘’“”]([^'\"‘’“”]{1,80})['\"‘’“”]\s+(?:field|column)", _re.I)

# Only a VALUE-SET intent is rescued. The justification below is specifically that
# allowed/excluded values come from the contract rather than from the data; a
# limit, a date or a formula does not turn on that argument, and the limit rescue
# above already covers numeric ceilings.
_VALUE_SET_OPERATORS = {"in_set", "not_in_set", "equals"}


def _sample_refusal_field(ir, item, template_fields):
    """The Output-Template column an UNMAPPED intent's own refusal named, when the
    refusal was about the value not appearing in the SAMPLE DATA.

    A value-set rule exists to flag rows that deviate from the contract, so "the
    contract's value is not in today's sample values" can never be a reason to
    drop it — yet across one 621-contract corpus 228 rules in 62 programmes were
    refused exactly that way, "Authorized Classes of Business" most of all. In 157
    of them the refusal NAMED the column it had rejected: the mapper had already
    decided where the rule belongs and then talked itself out of emitting it.

    So we take the model at its word: if its reason names, in a column slot, a
    name that IS a real column of this template, that column is the intent's home
    and the rule is re-mapped with the field FORCED (the same human-resolution
    path the limit rescue uses).

    FOUR conditions, all required, so the rescue can only fire where that argument
    actually holds — anything else keeps its review verdict:
      * the intent is a VALUE-SET one (allowed/excluded values);
      * the refusal is about the sample DATA, not about MEANING (a column that
        could not hold this kind of value at all is a valid refusal);
      * the refusal names a column in a column-denoting slot (never one of the
        VALUES it also quotes);
      * that name really is a column of THIS template.
    Acts ONLY on already-unmapped intents, so it can never change a rule that
    mapped on its own, and never invents a column.
    """
    if not isinstance(ir, dict) or ir.get("template"):
        return None
    if str((item or {}).get("operator") or "").strip() not in _VALUE_SET_OPERATORS:
        return None
    reason = ir.get("reason") or ""
    if not _SAMPLE_GROUNDING_REFUSAL_RE.search(reason):
        return None
    if _NO_SUCH_FIELD_RE.search(reason):
        return None
    lower = {}
    for f in (template_fields or []):
        nm = (f.get("name") or "").strip()
        if nm:
            lower.setdefault(nm.lower(), nm)
    # Last named column wins: the phrasings put the VALUE first and the COLUMN
    # last ("the allowed value 'X' is not found in the sample values for 'Y'").
    for m in reversed(_REFUSED_COLUMN_RE.findall(reason)):
        for quoted in m:
            hit = lower.get((quoted or "").strip().lower())
            if hit:
                return hit
    return None


def map_intents_to_ir(clauses, intent_clfs, template_fields=None, batch_size=None,
                      forced_field=None, forced_fields=None):
    """Map every extracted rule intent to a template + Output-Template fields.

    `intent_clfs` is the Call 2 output (aligned with `clauses`); each carries an
    `intents` list. We flatten all intents and map them in ONE Gemini call by
    default; if that single call truncates we retry the missing intents in chunks
    (so "one call" is the normal case, nothing is lost). Pass `batch_size` to
    force chunking. Regroups the resulting IRs per clause.

    `forced_field` (human review-queue resolution): direct every intent onto the
    given Output-Template column while still exposing the full field list for
    scope/group_by binding.
    """
    if len(clauses) != len(intent_clfs):
        raise ValueError("map_intents_to_ir: clauses/intents length mismatch")
    if not template_fields:
        raise ValueError("map_intents_to_ir requires template_fields.")

    # Flatten intents → items the mapping prompt scores one-by-one.
    items = []
    for clause, clf in zip(clauses, intent_clfs):
        if clf.get("_error") or not clf.get("is_rule_bearing"):
            continue
        for idx, intent in enumerate(clf.get("intents") or []):
            items.append({
                "clause_id":        clause.get("clause_id"),
                "intent_index":     idx,
                "subject":          intent.get("subject"),
                "operator":         intent.get("operator"),
                "value":            intent.get("value"),
                "scope":            intent.get("scope"),
                "severity":         intent.get("severity") or "warning",
                "is_referral":      bool(intent.get("is_referral")),
                "rule_name":        intent.get("rule_name"),
                "rule_description": intent.get("rule_description"),
                "error_message":    intent.get("error_message"),
                "clause_text":      (clause.get("text") or "")[:600],
            })

    # NOTE: there is deliberately NO pre-emptive chunking decision here any more.
    # The old code split on a COUNT threshold (_CALL3_SINGLE_MAX) because attention
    # dilution is not predictable from token arithmetic — but that meant paying for
    # 10 batches on every upload to insure against a failure that might not happen.
    # _map_optimistically below inverts it: send everything, MEASURE how much came
    # back unbound, and only then split — sizing the retry from the data rather than
    # from a constant. The count constants are kept only as the floor/ceiling the
    # budget sizer clamps to.

    def _partition(its):
        """Contract-derived vs generic-library intents.

        _run_mapping_batches derives its `is_generic` prompt flag as "EVERY item in
        this batch has a negative clause_id". Library intents are appended AFTER the
        contract ones, so unless the contract count is a multiple of the chunk size
        ONE boundary batch used to carry both — and, being mixed, was sent with the
        CONTRACT prompt. A library intent in that batch was then judged under "always
        find the closest field" instead of the "decline rather than guess" wording
        written for it, the opposite of what the flag is for (and contrary to
        build_ir_mapping_prompt_batch's own docstring, which states the two are never
        mixed). Partitioning first makes every batch homogeneous.
        """
        return ([it for it in its if it["clause_id"] >= 0],
                [it for it in its if it["clause_id"] < 0])

    def _size_by_budget(its):
        """Batch size DERIVED FROM THIS DATA, not a fixed count.

        The real ceiling on a mapping call is the output budget: the answer plus the
        thinking budget must fit in max_output_tokens. would_truncate() measures
        exactly that for a given payload using this stage's own answer/data ratio, so
        we can ask "how many of THESE intents fit?" instead of guessing 8. A contract
        with long clause_text gets smaller batches; a terse one gets larger — no magic
        number, and it adapts to a template/contract this code has never seen.
        """
        if not its:
            return 1
        think = int(_os.getenv("KAVACHIO_CALL3_THINKING", "16384"))
        _over, est, limit = would_truncate(
            _json.dumps(its, default=str), think, model=STAGE_B_MODEL,
            ratio=_CALL3_OUTPUT_RATIO)
        answer, room = est - think, limit - think
        if answer <= 0 or room <= 0:
            return _CALL3_AUTO_BATCH
        # Fill the budget, with a floor so a pathological item still makes progress.
        size = max(1, min(len(its), int(len(its) * room / answer)))
        plog.log("CALL3", "SIZING", f"{len(its)} intent(s) -> batch size {size}",
                 f"answer ~{answer:,.0f} tok + think {think:,} of {limit:,.0f} budget; "
                 f"room for answer {room:,.0f}")
        return size

    def _map_optimistically(its, label):
        """ONE call for everything; fall back only on a MEASURED, REAL failure.

        What counts as failure matters enormously here, and only one of the two
        "unbound" outcomes is one:

          MISSING KEY  — we sent the intent and got no row back. That is a dropped
                         or truncated answer, i.e. a genuine defect, and re-asking
                         a smaller batch can fix it.
          template null — the mapper CONSIDERED the intent and declined to bind it,
                         because no column of this template can represent it. That
                         is a correct answer, not a defect. Most of the review queue
                         is made of these. Retrying them in smaller batches cannot
                         change the answer — it just re-buys the same null, which is
                         how an earlier version of this function spent 55 calls
                         halving its way to 1 on intents that were never bindable.

        So: retry the missing, never the declined. Declined intents already get ONE
        relaxed re-ask further down (the `still` / auto-retry block), which is the
        right place for them because it changes the PROMPT rather than the batch
        size. Anything still unbound after that routes to review, as designed.
        """
        if not its:
            return {}
        plog.log("CALL3", "ATTEMPT", f"{label}: {len(its)} intent(s) in ONE call")
        got = _run_mapping_batches(its, template_fields, len(its), forced_field,
                                   forced_fields=forced_fields)

        def _missing(pool):
            return [it for it in pool
                    if (it["clause_id"], it["intent_index"]) not in got]

        declined = sum(
            1 for it in its
            if (it["clause_id"], it["intent_index"]) in got
            and not (got.get((it["clause_id"], it["intent_index"])) or {}).get("template"))
        bad = _missing(its)
        if not bad:
            print(f"[Call 3] {label}: {len(its)} intent(s) answered in ONE call "
                  f"({declined} declined → relaxed retry / review).")
            plog.log("CALL3", "OK", f"{label}: all {len(its)} answered in ONE call",
                     f"{declined} DECLINED (no matching column) — a valid verdict, "
                     f"NOT a reason to re-batch; they go to relaxed retry / review")
            return got

        print(f"[Call 3] {label}: single call dropped {len(bad)}/{len(its)} intent(s) "
              f"— re-running just those in budget-sized batches.")
        size = _size_by_budget(bad)
        plog.log("CALL3", "FALLBACK",
                 f"{label}: {len(bad)} item(s) -> {-(-len(bad)//size)} batch(es) of {size}",
                 f"single call DROPPED {len(bad)}/{len(its)} (missing from the answer). "
                 f"{declined} declined intents are NOT retried here")
        # Hard round cap: a bounded ladder, never an open-ended halving loop.
        for _round in range(_CALL3_FALLBACK_ROUNDS):
            before = len(bad)
            got.update(_run_mapping_batches(bad, template_fields, size, forced_field,
                                            forced_fields=forced_fields))
            bad = _missing(bad)
            if not bad:
                print(f"[Call 3] {label}: all intents recovered.")
                break
            if len(bad) >= before:
                if size <= 1:
                    break
                size = max(1, size // 2)
                print(f"[Call 3] {label}: no progress; halving batch to {size}.")
                plog.log("CALL3", "HALVE", f"{label}: batch size -> {size}",
                         f"round {_round + 1}/{_CALL3_FALLBACK_ROUNDS} made no progress "
                         f"({len(bad)} still missing)")
        if bad:
            print(f"[Call 3] {label}: {len(bad)} intent(s) still unanswered after "
                  f"{_CALL3_FALLBACK_ROUNDS} round(s) — routing to review.")
            plog.log("CALL3", "DROPPED", f"{label}: {len(bad)} intent(s) never answered",
                     f"hit the {_CALL3_FALLBACK_ROUNDS}-round cap — routed to review "
                     f"rather than retried forever")
        return got

    # (clause_id, intent_index) -> mapped IR dict
    mapped = {}
    if items:
        if forced_field is not None:
            # Human-resolution / rescue path: the column is already chosen, so there
            # is nothing to be optimistic about — keep the existing single pass.
            mapped = _run_mapping_batches(items, template_fields,
                                          batch_size or len(items), forced_field,
                                          forced_fields=forced_fields)
        else:
            contract_items, generic_items = _partition(items)
            print(f"\n[Call 3] Mapping {len(items)} intent(s) → IR "
                  f"({len(contract_items)} contract, {len(generic_items)} library).")
            mapped = _map_optimistically(contract_items, "contract")
            mapped.update(_map_optimistically(generic_items, "library"))

    # Deterministic backstop for unmapped numeric LIMIT/CAP intents. The mapper
    # occasionally leaves a clear limit/cap clause unmapped (e.g. it won't accept an
    # own-share "… Limit $" column for a "gross limit" because the name lacks the word
    # "gross") and the intent would silently drop to review. For each such intent we
    # deterministically pick the best limit column (generic — metric/share tokens +
    # the template's own party prefix, no hard-coded names) and RE-MAP just that intent
    # with the field FORCED, reusing the human-resolution path. This acts ONLY on
    # already-unmapped intents, so it can never regress a rule that mapped on its own.
    if items and forced_field is None:
        from collections import defaultdict as _defaultdict
        # Group unmapped limit intents by their forced target column so intents
        # heading to the SAME field go in ONE batched call instead of one call
        # each (N serial calls → ceil(N/_CALL3_AUTO_BATCH) per field).
        rescue_by_col = _defaultdict(list)
        for it in items:
            if (it["clause_id"], it["intent_index"]) in mapped:
                continue
            col = _best_limit_field_for_intent(it, template_fields)
            if col:
                rescue_by_col[col].append(it)
        for col, its in rescue_by_col.items():
            recovered = _run_mapping_batches(
                its, template_fields, _CALL3_AUTO_BATCH, forced_field=col)
            if recovered:
                mapped.update(recovered)
                print(f"  [rescue] forced {len(its)} unmapped limit intent(s) "
                      f"→ field {col!r}")

    # Deterministic backstop for an intent refused over the SAMPLE DATA rather
    # than over the column's meaning ("the allowed value 'X' is not found in the
    # sample values for '<column>'"). That objection is never valid: the
    # allowed/excluded values come from the CONTRACT, and a row carrying a value
    # the contract does not authorise is what the rule exists to catch. Such a
    # refusal has ALREADY chosen the column — it names it — so the intent is
    # re-mapped with that field FORCED, reusing the same human-resolution path as
    # the limit rescue above. Acts ONLY on already-unmapped intents, and only when
    # the named column really is in this template, so it can never change a rule
    # that mapped on its own nor invent a column.
    if items and forced_field is None:
        from collections import defaultdict as _defaultdict
        sample_rescue = _defaultdict(list)
        for it in items:
            key = (it["clause_id"], it["intent_index"])
            col = _sample_refusal_field(mapped.get(key), it, template_fields)
            if col:
                sample_rescue[col].append(it)
        for col, its in sample_rescue.items():
            recovered = _run_mapping_batches(
                its, template_fields, _CALL3_AUTO_BATCH, forced_field=col)
            for key, ir in (recovered or {}).items():
                if isinstance(ir, dict) and ir.get("template"):
                    mapped[key] = ir
            print(f"  [rescue] re-mapped {len(its)} intent(s) refused over sample "
                  f"data → field {col!r} (the column the refusal itself named)")

    # AUTO-RETRY (attempt 2). Any rule-bearing intent STILL unmapped — the first
    # deterministic pass returned template null / bound nothing — gets ONE more
    # Call-3 pass with a RELAXED "reconsider" prompt and a small temperature bump,
    # so the mapper can bind to the CLOSEST representable field instead of dropping
    # to review. A retry row is accepted only if it now carries a real template;
    # anything still null stays unmapped and routes to review (→ manual field
    # selection). The deterministic verify gate still validates whatever it picks,
    # so this can only recover rules, never ship an unchecked one.
    if items and forced_field is None:
        # "Unmapped" = the mapper either DECLINED it (returned template null) or
        # dropped it entirely (truncation → key absent). BOTH must be retried — the
        # decline is the common "rule-bearing but no rule generated" case. A key that
        # is present with template null still counts as unmapped here.
        def _has_template(it):
            m = mapped.get((it["clause_id"], it["intent_index"]))
            return bool(isinstance(m, dict) and m.get("template"))
        # Generic-library intents carry a NEGATIVE clause_id (see
        # generic_rule_library._build_intents — "clause_id is the NEGATED library
        # id"). Unlike a contract-derived intent — grounded in prose that asserts
        # the field DOES exist somewhere in THIS template — a generic rule is a
        # bare CONCEPT ("Contract ID") that may legitimately have no distinct
        # column in this particular program (e.g. a program that only tracks a
        # UMR Number has no separate Contract ID). Relaxing for those just forces
        # the concept onto the nearest lookalike column (e.g. "Contract ID" →
        # "Palms UMR Number") instead of correctly staying unmapped and routing to
        # review — so only contract-derived intents get the permissive retry.
        still = [it for it in items
                 if not _has_template(it) and it["clause_id"] >= 0]
        if still:
            print(f"[Call 3] auto-retry (attempt 2, relaxed) for "
                  f"{len(still)} unmapped intent(s).")
            # LOGGED, because this pass was previously invisible. The plog
            # ATTEMPT/OK pair lives inside _map_optimistically, and the relaxed
            # retry calls _run_mapping_batches directly — so the decision log
            # showed one CALL3 attempt and nothing else, and "did the safety net
            # run?" was unanswerable after the fact. It matters: the first pass
            # is not stable at this batch size (the same 29 intents, same
            # template, temperature 0, decline a DIFFERENT subset run to run),
            # so this retry is what stands between a wobble and a missing rule.
            plog.log("CALL3", "RETRY",
                     f"relaxed retry for {len(still)} unmapped intent(s)",
                     "the strict pass left these unbound — re-asking with the "
                     "reconsider prompt before they are routed to review")
            retry = _run_mapping_batches(
                still, template_fields,
                min(len(still), _CALL3_AUTO_BATCH) or 1,
                forced_field=None, relaxed=True, temperature=_CALL3_RETRY_TEMP)
            recovered = 0
            for key, ir in retry.items():
                # Overwrite a prior null/missing entry when the retry now binds a
                # real template (setdefault would keep the earlier null).
                if isinstance(ir, dict) and ir.get("template"):
                    mapped[key] = ir
                    recovered += 1
                    print(f"  [retry] recovered intent {key} → "
                          f"template {ir.get('template')!r}")
            lost = len(still) - recovered
            plog.log("CALL3", "RECOVERED" if recovered else "UNRECOVERED",
                     f"{recovered} of {len(still)} rescued by the relaxed retry",
                     f"{lost} intent(s) still unbound — these become "
                     f"'awaiting a column' clauses on the setup screen"
                     if lost else "nothing left unbound")

    # Regroup IRs per clause (preserve input order).
    output = []
    for clause, clf in zip(clauses, intent_clfs):
        cid = clause.get("clause_id")
        if clf.get("_error") or not clf.get("is_rule_bearing"):
            output.append({"clause": clause, "classification": clf,
                           "engine": None, "candidates": []})
            continue
        candidates = []
        for idx, intent in enumerate(clf.get("intents") or []):
            ir = mapped.get((cid, idx))
            if ir is None:
                # Mapping call dropped/failed this intent → unmapped IR so the
                # verifier routes it to review with a reason (nothing silently lost).
                ir = {"template": None, "params": {},
                      "rule_name": intent.get("rule_name"),
                      "rule_description": intent.get("rule_description"),
                      "severity": intent.get("severity"),
                      "reason": "intent not returned by mapping call"}
            ir = _repoint_own_share_field(ir, intent, clause, template_fields)

            # Propagate the referral flag from the intent onto the IR — the model
            # only returns template+params, so without this the verify gate can't
            # tell a referral apart (and can't rewrite it to a conditional on the
            # referral-indicator column).
            if isinstance(ir, dict) and intent.get("is_referral"):
                ir["is_referral"] = True

            # SCOPE-DROP signal. Call 2 adds a `scope` only when the rule applies to
            # SOME rows (a row-filter). If the mapper then produced a rule WITHOUT a
            # scope, it silently dropped that filter — e.g. a per-entity limit
            # ("Reinsurer X: limit $Z") whose entity column doesn't exist, emitted as
            # a bare over-broad max_limit. Record the dropped filter so the verify
            # gate routes it to review instead of shipping the over-broad rule.
            if (isinstance(ir, dict) and intent.get("scope")
                    and not (ir.get("params") or {}).get("scope")):
                ir["_scope_dropped"] = intent.get("scope")

            # INVARIANT signal. "this value must not CHANGE across one entity's
            # rows" and "this value must be UNIQUE across rows" read alike but are
            # opposite checks, and the mapper occasionally collapses the first into
            # the second — which flags every policy that simply has more than one
            # transaction row. Record the intent's operator so the verify gate can
            # repair that deterministically (see rule_normalizer._fix_invariant_ir).
            if isinstance(ir, dict) and intent.get("operator") == "invariant":
                ir["_intent_operator"] = "invariant"

            candidates.append(ir)
        output.append({"clause": clause, "classification": clf,
                       "engine": "ir", "candidates": candidates})
    return output


# =========================================================
# Helper: build enriched clause metadata for batch prompts
# =========================================================

def _build_clauses_meta(clauses, classifications):
    """
    Merge clause fields with their Stage A classification so the batch prompt
    has everything it needs in one flat dict per clause.
    """

    return [
        {
            "clause_id":            c.get("clause_id"),
            "clause_type":          c.get("clause_type", "other"),
            "title":                c.get("title", ""),
            "text":                 c.get("text", ""),
            "section_header":       c.get("section_header"),
            "page_number":          c.get("page_number") or c.get("page", 0),
            "suggested_rule_types": cls.get("rule_types", []) or [],
            "rule_stage":           cls.get("rule_stage") or "input"
        }
        for c, cls in zip(clauses, classifications)
    ]


# =========================================================
# Dispatcher: route all classified clauses to batched synthesizers
# =========================================================

def synthesize_rules(clauses, classifications, ajv_batch_size=8, custom_batch_size=8, template_fields=None):
    """
    Given parallel lists of clauses and classifications, separate them by
    engine (AJV / custom), call the appropriate BATCHED synthesizer for
    each engine group, then reassemble results in the original input order.

    Returns: list of {clause, classification, engine, candidates[]}

    `ajv_batch_size`    — clauses per AJV Gemini call    (default 5)
    `custom_batch_size` — clauses per Custom Gemini call (default 3)
    """

    if len(clauses) != len(classifications):
        raise ValueError(
            "synthesize_rules: clauses and classifications length mismatch"
        )

    if not template_fields:
        raise ValueError(
            "synthesize_rules requires template_fields — rules must target Output "
            "Template fields (validation runs on the rendered output, not the data "
            "model). Pass the output template's fields."
        )

    # ── Partition into engine groups ──────────────────────────────────────

    ajv_clauses     = []
    ajv_clfs        = []
    custom_clauses  = []
    custom_clfs     = []
    skip_entries    = []   # non-rule-bearing / errored — no synthesis needed

    for clause, classification in zip(clauses, classifications):

        if classification.get("_error") or not classification.get("is_rule_bearing"):
            skip_entries.append({
                "clause":         clause,
                "classification": classification,
                "engine":         None,
                "candidates":     []
            })
            continue

        engine = classification.get("engine")

        if engine == "ajv":
            ajv_clauses.append(clause)
            ajv_clfs.append(classification)

        elif engine == "custom":
            custom_clauses.append(clause)
            custom_clfs.append(classification)

        else:
            print(
                f"[Stage B] clause {clause.get('clause_id')}: "
                f"unknown engine '{engine}', skipping"
            )
            skip_entries.append({
                "clause":         clause,
                "classification": classification,
                "engine":         engine,
                "candidates":     []
            })

    # ── Batched synthesis calls ───────────────────────────────────────────

    ajv_candidates_by_id = {}
    if ajv_clauses:
        print(
            f"\n[Stage B] AJV synthesis: "
            f"{len(ajv_clauses)} clause(s) → 1 Gemini call (template-aware batch)"
        )
        ajv_candidates_by_id = synthesize_ajv_rules_batch(
            ajv_clauses, ajv_clfs,
            batch_size=ajv_batch_size,
            template_fields=template_fields,
        )

    custom_candidates_by_id = {}
    if custom_clauses:
        print(
            f"\n[Stage B] Custom synthesis: "
            f"{len(custom_clauses)} clause(s) → 1 Gemini call (template-aware batch)"
        )
        custom_candidates_by_id = synthesize_custom_rules_batch(
            custom_clauses, custom_clfs,
            batch_size=custom_batch_size,
            template_fields=template_fields,
        )

    # ── Reassemble in original input order ───────────────────────────────

    # Build a fast lookup: clause_id → (engine, candidates)
    synthesis_map = {}

    for clause, classification in zip(ajv_clauses, ajv_clfs):
        cid = clause.get("clause_id")
        synthesis_map[cid] = (
            "ajv",
            ajv_candidates_by_id.get(cid, [])
        )

    for clause, classification in zip(custom_clauses, custom_clfs):
        cid = clause.get("clause_id")
        synthesis_map[cid] = (
            "custom",
            custom_candidates_by_id.get(cid, [])
        )

    output = []

    for clause, classification in zip(clauses, classifications):

        cid = clause.get("clause_id")

        if cid in synthesis_map:
            engine, candidates = synthesis_map[cid]
            output.append({
                "clause":         clause,
                "classification": classification,
                "engine":         engine,
                "candidates":     candidates
            })

        else:
            # Was in skip_entries — find it there
            output.append({
                "clause":         clause,
                "classification": classification,
                "engine":         None,
                "candidates":     []
            })

    return output


# =========================================================
# Stage B output persistence
# =========================================================

def split_synth_outputs_by_engine(synth_outputs):
    """
    Partition synth_outputs into (ajv_entries, custom_entries) for separate
    JSON persistence. Entries with engine=None (non-rule-bearing / errored)
    are excluded from both.
    """

    ajv_entries    = [e for e in synth_outputs if e.get("engine") == "ajv"]
    custom_entries = [e for e in synth_outputs if e.get("engine") == "custom"]

    return ajv_entries, custom_entries



def _strip_top_level_classification(entries):
    """
    Return shallow copies of `entries` with the redundant top-level
    "classification" key removed. The classification is already carried inside
    each entry's "clause" (clause["classification"]), so dropping the duplicate
    keeps the saved JSON lean. Originals are left untouched so the in-memory
    synth_outputs stay intact for normalization downstream.
    """

    return [
        {k: v for k, v in entry.items() if k != "classification"}
        for entry in entries
    ]


def _summarize_entries(entries):
    """Lightweight per-engine stats embedded in the saved JSON."""

    total_clauses    = len(entries)
    total_candidates = sum(len(e.get("candidates", [])) for e in entries)

    return {
        "clause_count":          total_clauses,
        "candidate_rule_count":  total_candidates,
        "with_candidates":       sum(
            1 for e in entries if e.get("candidates")
        ),
        "empty_synthesis":       sum(
            1 for e in entries if not e.get("candidates")
        )
    }


def save_stage_b_outputs(
    synth_outputs,
    output_dir,
    contract_id="contract",
    file_base=None
):
    """
    Save Stage B AJV and Custom synthesis outputs to separate JSON files
    alongside any other artifacts.

    Files written (inside `output_dir`):
      <file_base>_stage_b_ajv.json
      <file_base>_stage_b_custom.json

    Each file payload:
      {
        "stage": "2.4a — Stage B: AJV synthesis" | "2.4b — Stage B: Custom synthesis",
        "contract_id": ...,
        "generated_at": ISO timestamp,
        "summary": {clause_count, candidate_rule_count, ...},
        "entries": [
          {"clause": ..., "classification": ..., "engine": ..., "candidates": [...]}
        ]
      }

    Returns: {"ajv": <path>, "custom": <path>}
    """

    os.makedirs(output_dir, exist_ok=True)

    if not file_base:
        file_base = contract_id or "contract"

    ajv_entries, custom_entries = split_synth_outputs_by_engine(synth_outputs)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    ajv_path    = os.path.join(output_dir, f"{file_base}_stage_b_ajv.json")
    custom_path = os.path.join(output_dir, f"{file_base}_stage_b_custom.json")

    ajv_payload = {
        "stage":        "2.4a — Stage B: AJV synthesis",
        "contract_id":  contract_id,
        "generated_at": now,
        "summary":      _summarize_entries(ajv_entries),
        "entries":      _strip_top_level_classification(ajv_entries)
    }

    custom_payload = {
        "stage":        "2.4b — Stage B: Custom synthesis",
        "contract_id":  contract_id,
        "generated_at": now,
        "summary":      _summarize_entries(custom_entries),
        "entries":      _strip_top_level_classification(custom_entries)
    }

    with open(ajv_path, "w") as f:
        json.dump(ajv_payload, f, indent=2, default=str)

    with open(custom_path, "w") as f:
        json.dump(custom_payload, f, indent=2, default=str)

    print(
        f"[Stage B] saved AJV    synthesis → {ajv_path} "
        f"({ajv_payload['summary']['candidate_rule_count']} candidates "
        f"across {ajv_payload['summary']['clause_count']} clauses)"
    )
    print(
        f"[Stage B] saved Custom synthesis → {custom_path} "
        f"({custom_payload['summary']['candidate_rule_count']} candidates "
        f"across {custom_payload['summary']['clause_count']} clauses)"
    )

    return {"ajv": ajv_path, "custom": custom_path}
