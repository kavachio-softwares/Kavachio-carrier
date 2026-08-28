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

from contract_upload_services.gemini_service import (
    call_gemini, DETERMINISTIC_SEED, STAGE_B_MODEL, plan_token_batches,
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
# Auto-retry (attempt 2): a small temperature bump so the relaxed re-map can
# actually differ from the deterministic first pass (temp 0 + fixed seed would
# otherwise reproduce the same "no field" result). Deterministic verify still
# gates whatever the retry picks, so a non-zero temp here is safe.
_CALL3_RETRY_TEMP = float(os.getenv("KAVACHIO_CALL3_RETRY_TEMP", "0.4"))


def _safe_parse(raw: str, label: str) -> dict | None:
    """Parse Gemini JSON, stripping JS-style // comments that break stdlib json."""
    text = raw if isinstance(raw, str) else json.dumps(raw)
    # Remove // line comments (Gemini sometimes writes these inside JSON)
    text = _re.sub(r"//[^\n]*", "", text)
    try:
        parsed = json.loads(text)
        # Normalise bare list → {"results": [...]} or {"rules": [...]}
        if isinstance(parsed, list):
            # Heuristic: list of rule objects → wrap as rules
            parsed = {"rules": parsed}
        return parsed
    except Exception as exc:
        print(f"INVALID JSON FROM GEMINI ({label}):\n{raw}")
        print(f"[{label}] parse error: {exc}")
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
    n_batches = (total + batch_size - 1) // batch_size
    for b in range(0, total, batch_size):
        batch = items[b:b + batch_size]
        b_num = b // batch_size + 1
        parsed = None
        try:
            raw = call_gemini(
                build_ir_mapping_prompt_batch(batch, template_fields, forced_field,
                                              relaxed=relaxed,
                                              forced_fields=forced_fields),
                label=f"Call3-Map{'-retry' if relaxed else ''}-{b_num}/{n_batches}",
                temperature=temperature,
                seed=DETERMINISTIC_SEED,
                max_output_tokens=65536,
                thinking_budget=16384,
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

    # Auto-chunk large intent sets. One mapping call over many intents dilutes the
    # model's attention and it leaves mappable intents UNMAPPED (→ review) even when
    # a good column exists. Above _CALL3_SINGLE_MAX intents, chunk proactively so
    # each intent gets enough attention. Small sets keep the single-call path.
    # (forced_field is the human review-queue path — leave it on one call.)
    if batch_size is None and forced_field is None and len(items) > _CALL3_SINGLE_MAX:
        batch_size = _CALL3_AUTO_BATCH
        print(f"[Call 3] {len(items)} intents > {_CALL3_SINGLE_MAX}; auto-chunking to "
              f"avoid attention dilution.")

    # (clause_id, intent_index) -> mapped IR dict
    mapped = {}
    if items:
        if batch_size:
            print(f"\n[Call 3] Mapping {len(items)} intent(s) → IR in chunks of {batch_size}.")
            mapped = _run_mapping_batches(items, template_fields, batch_size,
                                          forced_field, forced_fields=forced_fields)
            # A chunk may still truncate/drop an intent — retry any missing ones.
            missing = [it for it in items
                       if (it["clause_id"], it["intent_index"]) not in mapped]
            if missing:
                print(f"[Call 3] chunked pass incomplete ({len(missing)}/{len(items)}); "
                      f"retrying those in chunks of {_CALL3_AUTO_BATCH}.")
                mapped.update(_run_mapping_batches(missing, template_fields, _CALL3_AUTO_BATCH,
                                                   forced_field, forced_fields=forced_fields))
        else:
            print(f"\n[Call 3] Mapping {len(items)} intent(s) → IR in 1 call.")
            mapped = _run_mapping_batches(items, template_fields, len(items),
                                          forced_field, forced_fields=forced_fields)
            # If the single call truncated, retry just the missing intents.
            missing = [it for it in items
                       if (it["clause_id"], it["intent_index"]) not in mapped]
            if missing:
                print(f"[Call 3] single call incomplete ({len(missing)}/{len(items)}); "
                      f"retrying those in chunks of 8.")
                mapped.update(_run_mapping_batches(missing, template_fields, 8,
                                                   forced_field, forced_fields=forced_fields))

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
        still = [it for it in items if not _has_template(it)]
        if still:
            print(f"[Call 3] auto-retry (attempt 2, relaxed) for "
                  f"{len(still)} unmapped intent(s).")
            retry = _run_mapping_batches(
                still, template_fields,
                min(len(still), _CALL3_AUTO_BATCH) or 1,
                forced_field=None, relaxed=True, temperature=_CALL3_RETRY_TEMP)
            for key, ir in retry.items():
                # Overwrite a prior null/missing entry when the retry now binds a
                # real template (setdefault would keep the earlier null).
                if isinstance(ir, dict) and ir.get("template"):
                    mapped[key] = ir
                    print(f"  [retry] recovered intent {key} → "
                          f"template {ir.get('template')!r}")

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
