"""
validation_rule_generator.py
────────────────────────────
Orchestrates the full Kavachio core module — Pipeline 1 + Pipeline 2 —
per Kavachio_Pipeline_Architecture.png + kavachio-contract-validations docs.

Flow (per-contract):

  Pipeline 1 — Contract Extraction
    1.1 Pre-processing               (PDF → page text, done in document_extractors)
    1.2 Section identification       (prompt_builder.split_into_sections)
    1.3 Structured extraction        ← LLM call #1, per section
    1.4 Metadata synthesis & dedup
    1.5 Persistence (program_metadata, clauses_extracted)

  Pipeline 2 — Rule Generation
    2.2 Stage A: Classification      ← LLM call #2, batched over clauses
    2.4-A Stage B: AJV synthesis     ← LLM call #3, per AJV clause
    2.4-B Stage B: Custom synthesis  ← LLM call #4, per custom clause
    2.5 Normalization (deterministic — AJV compile, canonical fields, threshold)
    2.6 Rule persistence (validation_rule rows)

Output:
  - "validation_rules" : new ajv/custom rule rows (the architecture target)

The legacy class_name pipeline has been removed. For back-compat, the output
still carries "contract_rules" (empty) and an "analysis" block whose
program_name / document_type are sourced from Pipeline 1 program_metadata.
"""

import os
import re
import json
import uuid
from datetime import datetime, timezone


# Default ±% tolerance stamped onto auto-derived cross_field_math (formula) rules
# — the small rounding slack between a reported amount and base × rate / base −
# amount. Env-overridable so an operator can widen/tighten the fleet default
# without a code change; a single rule can then be tuned further via the
# tolerance-band edit endpoint (rule_editor.patch_tolerance). 1.0 == 1%.
DEFAULT_CROSS_FIELD_TOLERANCE_PCT = float(
    os.getenv("KAVACHIO_DEFAULT_TOLERANCE_PCT", "1.0"))


# In-memory cache of completed extractions, keyed by a resume_token. When the
# pipeline halts on external references, the parsed extraction is stashed here so
# "Continue Anyway" can resume WITHOUT re-calling the extraction LLM.
# Note: process-local (not shared across uvicorn workers) and cleared on restart;
# entries are popped on use. Fine for the interactive upload flow.
_EXTRACTION_RESUME_CACHE: dict = {}

import ai_cache
import pipeline_log as plog

from contract_upload_services.constants import (
    RULE_CLASS_LIBRARY,
    DEFAULT_RULE_AUTO_TRUST_THRESHOLD
)

from contract_upload_services.gemini_service import (
    call_gemini, DETERMINISTIC_SEED, OversizeError, should_chunk, would_truncate,
    estimate_tokens, model_ceilings, OUTPUT_RATIO, OUTPUT_SAFETY,
)

from contract_upload_services.prompt_builder import (
    split_into_sections,
    build_extraction_prompt,
    build_llm_context,
    split_pages_into_chunks
)

from contract_upload_services.contract_data_classifier import (
    classify_clauses_batch,
    extract_rule_intents,
    resolve_status,
    save_stage_a_output
)

from contract_upload_services.stage_b_synthesizer import (
    synthesize_rules_ir,
    map_intents_to_ir,
)

from contract_upload_services.generic_rule_library import (
    derive_generic_library_entries,
    build_generic_intents,
    finish_generic_entries,
    is_generic_entry,
    drop_derived_duplicates,
    drop_uncountried_reference_rules,
)

from contract_upload_services.rule_normalizer import (
    parse_llm_json,
    normalize_ir_outputs,
    build_reference_group_members,
)

from contract_upload_services.output_schema import build_output_schema

# One shared definition of "this column is a state column" / "this column is a
# postal column", used both to emit the state_validity + zip_state_consistency
# rules below and to gate loading the reference tables they query — so the two can
# never disagree. `column_tokens` is the single camelCase-aware name tokenizer
# behind both (a Lloyd's-style "InsuredState" carries no separators at all).
from contract_upload_services.uszips_reference import (
    is_state_column, is_postal_column, column_tokens)


def _looks_percent(field: dict) -> bool:
    """True when a rate/percentage column stores values as PERCENT (e.g. "23.5")
    rather than a 0-1 fraction, so a formula using it must divide by 100."""
    fmt = (field.get("field_format") or "").lower()
    if "percent" in fmt or "%" in fmt:
        return True
    if "fraction" in fmt:
        return False
    nums = []
    for s in (field.get("samples") or []):
        try:
            nums.append(abs(float(str(s).replace("%", "").replace(",", "").strip())))
        except (TypeError, ValueError):
            continue
    if nums:
        mx = max(nums)
        if mx > 1.5:
            return True    # e.g. 23.5 → percent
        # Every sampled value sits in [0, 1] — a column storing a 0-1 FRACTION
        # (e.g. a "Part of Limit %" holding 0.2 for 20%), which must NOT be divided
        # by 100. Only decide fraction when there is a genuinely non-zero sample
        # (all-zero samples are uninformative — fall through to the default).
        if mx <= 1.0 and any(v > 0 for v in nums):
            return False
    return True   # rates are percent by convention unless proven fractional


# Generic financial/role words stripped when comparing two columns' ENTITY
# (the MGA/party/schedule a $ or % figure belongs to — "Palms", "First
# Reinsurance", …). A bare "100%" prefix is a SCALE label ("100% Gross Written
# Premium" = the whole-program premium before any split), not an entity, so it's
# stripped too — critical: without this, "100" would look like just another
# distinguishing token and never lose to a real entity like "palms". Peril/sub-
# type qualifiers (terrorism, cyber) are NOT stripped — they distinguish a
# genuinely different premium figure, so a commission/net formula must not
# casually pick a Terrorism or Cyber sub-premium as its base.
_FIN_STOPWORDS = {"commission", "commissions", "amount", "amt", "rate", "gross",
                  "written", "premium", "net", "the", "of", "100", "total",
                  "percent", "fee", "fees", "tax", "brokerage"}


def _fin_entity_toks(n):
    return {t for t in re.findall(r"[a-z0-9]+", (n or "").lower())
            if t not in _FIN_STOPWORDS}


def _best_fin_match(anchor_toks, candidates):
    """The candidate whose entity tokens overlap `anchor_toks` the most, breaking
    ties toward the candidate with the FEWEST unexplained extra tokens (i.e. the
    more general column, e.g. "Palms Gross Written Premium" over "Palms Gross
    Terrorism Premium" when the anchor itself names no peril). Falls back to the
    single/first candidate when there is nothing to disambiguate (a template with
    only one premium column at all — the common single-schedule case)."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    best, best_score = None, None
    for c in candidates:
        ctoks = _fin_entity_toks(c)
        shared = len(anchor_toks & ctoks)
        extra = len(ctoks - anchor_toks)
        score = (shared, -extra)
        if best_score is None or score > best_score:
            best, best_score = c, score
    return best


# The financial CONCEPT a $/% column measures (kept distinct from the entity
# tokens above so an amount column can be matched to ITS OWN rate column — a
# "Commission Amount" pairs with a "Commission %", never a "Fee %"). Used by the
# hardcoded-rate correction below.
_RATE_CONCEPTS = ("commission", "fee", "tax", "brokerage", "cede", "cession",
                  "ceding", "override")


def _rate_concept(name):
    ln = (name or "").lower()
    for c in _RATE_CONCEPTS:
        if c in ln:
            return c
    return None


def _is_country_column(name, by_name):
    """True when a column carries a COUNTRY value — by its own name tokens, or by
    the canonical field the template parser bound it to (so an oddly-named column
    mapped to `insured_location_country` still counts)."""
    if {"country", "countries"} & set(column_tokens(name)):
        return True
    canon = ((by_name or {}).get(name) or {}).get("canonical_field")
    return "country" in set(column_tokens(canon))


def contract_postal_countries(synth_outputs, template_fields):
    """Which country the CONTRACT itself pins the risk to, for the postal checks.

    A contract that says "COUNTRY OF ORIGIN: United Kingdom" has already decided
    which country's regions and postal codes its policies may carry, so the
    state / ZIP checks must validate against THAT country — not against whatever
    the BDX's own country column happens to say (which, when the two disagree, is
    the error being looked for). The BDX country column stays the fallback for the
    ordinary case where the contract names no country at all.

    Read off the MAPPED rules rather than the clause text: a clause that pins the
    country has already become a `value_in_set` rule on a country column, so the
    allowed values are structured and the country column is already identified. No
    country name appears here — every value is resolved through the same public
    postal/ISO reference the compiled SQL uses (`resolve_country_code`).

    Returns:
      None  — the contract pins nothing (or pins something that is not a country,
              e.g. "Worldwide") → callers keep the per-row country-column dispatch.
      []    — the contract pins ONLY countries we hold no postal data for (e.g.
              France) → callers must emit NO postal rule, because every row would
              otherwise be judged against the wrong country's reference.
      [codes] — validate against exactly these countries.
    """
    try:
        from contract_upload_services.intl_postal_reference import (
            resolve_country_code, SUPPORTED_COUNTRIES)
    except Exception:                                       # pragma: no cover
        return None
    by_name = {f.get("name"): f for f in (template_fields or []) if f.get("name")}
    codes, unsupported, saw_rule = [], [], False
    for entry in (synth_outputs or []):
        for ir in (entry.get("candidates") or []):
            if ir.get("template") != "value_in_set":
                continue
            if ir.get("is_referral"):
                # A referral TRIGGER ("refer risks outside the UK") is not a
                # prohibition — the policy may still be written.
                continue
            params = ir.get("params") or {}
            if params.get("scope"):
                # Scoped to a subset of rows ("for this class of business the
                # country must be X"), so it pins nothing for the BDX as a whole.
                continue
            field = params.get("field")
            if not field or not _is_country_column(field, by_name):
                continue
            # `allowed_values` is the alias the mapper sometimes emits; this runs
            # BEFORE normalize_ir_outputs canonicalises it, so read both spellings.
            allowed = params.get("allowed")
            if not isinstance(allowed, list) or not allowed:
                allowed = params.get("allowed_values")
            if not isinstance(allowed, list) or not allowed:
                continue
            # `variation_values` are alternative SPELLINGS of the same allowed
            # values, so they add no country and are deliberately not read.
            resolved = [resolve_country_code(v) for v in allowed]
            if any(c is None for c in resolved):
                # The allow-list holds something that names no country at all
                # ("Worldwide", "Various", a region) — not a clean country pin,
                # so fall back rather than guess.
                return None
            saw_rule = True
            for c in resolved:
                if c in SUPPORTED_COUNTRIES:
                    if c not in codes:
                        codes.append(c)
                elif c not in unsupported:
                    unsupported.append(c)
    if not saw_rule:
        return None
    if codes and unsupported:
        # The contract permits a mix we can only partly check; per-row dispatch
        # validates what we hold data for and skips the rest — strictly safer
        # than flagging every row of the countries we cannot check.
        return None
    return sorted(codes)


def derive_formula_entries(synth_outputs, template_fields):
    """#6 — Derive cross-field FORMULA rules the contract implies but never spells
    out. When the output template carries a matching  <concept> Amount + base
    Premium + <concept> Rate  trio AND the contract already governs that rate
    (a mapped rule targets the rate column), add ONE cross_field_math rule
    (Amount = Premium × Rate, within tolerance) so the reported amount is checked
    against the rate — not just the rate in isolation.

    Returns a list of synth_output entries (same shape map_intents_to_ir emits) to
    append; empty when the trio/rate-rule isn't present. Deterministic, no LLM."""
    names = [f.get("name") for f in (template_fields or []) if f.get("name")]
    by_name = {f.get("name"): f for f in (template_fields or []) if f.get("name")}

    def find_all(*needs, avoid=()):
        return [n for n in names
                if all(w in n.lower() for w in needs)
                and not any(a in n.lower() for a in avoid)]

    _best_match = _best_fin_match

    # Fields the rate rule already targets (so we only derive for governed rates).
    governed = set()
    for entry in synth_outputs:
        for ir in (entry.get("candidates") or []):
            fld = (ir.get("params") or {}).get("field")
            if fld:
                governed.add(fld)

    entries = []
    # Commission: Commission Amount = Gross Premium × Commission Rate. When the
    # template has SEVERAL parties/schedules (e.g. "Palms Commission Amount $"
    # alongside "First Reinsurance Commission %", and BOTH a Palms-specific
    # "Palms Gross Written Premium $" and a program-wide "100% Gross Written
    # Premium"), the rate and the premium BASE must be the SAME entity as the
    # amount column — never a different party's figure or the global total —
    # so every candidate is matched by shared entity tokens, not just the
    # first column that happens to contain the right keywords.
    amount_candidates = find_all("commission", "amount")
    rate_all = find_all("commission", "rate") or find_all("commission", "%")
    base_all = (find_all("gross", "premium", avoid=("terrorism", "cyber"))
               or find_all("premium", avoid=("net", "fac", "annual",
                                             "terrorism", "cyber")))

    commission_derived = False
    commission_base_entity = None   # the base's entity tokens, reused by Net Premium
    amount, rate, base = None, None, None
    for cand_amount in amount_candidates:
        at = _fin_entity_toks(cand_amount)
        cand_rate = _best_match(at, rate_all)
        if not cand_rate or cand_rate not in governed:
            continue
        cand_base = _best_match(at, base_all)
        if not cand_base:
            continue
        amount, rate, base = cand_amount, cand_rate, cand_base
        break

    if amount and rate and base:
        ir = {
            "template": "cross_field_math",
            "params": {
                "result_field": amount, "left_field": base,
                "operator": "*", "right_field": rate,
                "right_is_percent": _looks_percent(by_name.get(rate, {})),
                "tolerance_pct": DEFAULT_CROSS_FIELD_TOLERANCE_PCT,
            },
            "rule_name": f"{amount} equals {base} × {rate}",
            "rule_description": (
                f"{amount} must equal {base} multiplied by {rate} "
                f"(within a small tolerance for rounding)."),
            "severity": "warning",
            "error_message": f"{amount} does not match {base} × {rate}.",
            "confidence": 1.0,
        }
        entries.append({
            "clause": {"clause_id": None,
                       "text": f"[Derived formula] {amount} = {base} × {rate}",
                       "page_number": None},
            "engine": "ir",
            "candidates": [ir],
        })
        commission_derived = True
        commission_base_entity = _fin_entity_toks(base)

    # Net Premium: Net Premium = Gross Premium − Commission Amount. Derived only
    # when a Net Premium column exists AND the Commission Amount it subtracts is
    # itself governed — either the commission-amount formula above was emitted
    # (commission_derived) or a mapped rule already targets the amount column
    # (amount in governed). Both operands are dollar columns, so right_is_percent
    # is False (unlike the commission rate). `net` needs BOTH 'net' and 'premium'
    # so it can't collide with 'Gross Premium'; when several Net Premium columns
    # exist (multi-party templates), the one sharing the CHOSEN base's entity
    # tokens wins (same "Palms" vs "100%"/global disambiguation as above).
    net_all = find_all("net", "premium")
    if net_all and base and amount and (commission_derived or amount in governed):
        anchor = commission_base_entity if commission_base_entity is not None \
            else _fin_entity_toks(base)
        net = _best_match(anchor, net_all)
        ir = {
            "template": "cross_field_math",
            "params": {
                "result_field": net, "left_field": base,
                "operator": "-", "right_field": amount,
                "right_is_percent": False,
                "tolerance_pct": DEFAULT_CROSS_FIELD_TOLERANCE_PCT,
            },
            "rule_name": f"{net} equals {base} − {amount}",
            "rule_description": (
                f"{net} must equal {base} minus {amount} "
                f"(within a small tolerance for rounding)."),
            "severity": "warning",
            "error_message": f"{net} does not match {base} − {amount}.",
            "confidence": 1.0,
        }
        entries.append({
            "clause": {"clause_id": None,
                       "text": f"[Derived formula] {net} = {base} − {amount}",
                       "page_number": None},
            "engine": "ir",
            "candidates": [ir],
        })

    # ZIP ↔ STATE consistency (data-quality). For every ZIP/postal column, emit a
    # zip_state_consistency rule against the STATE column(s) it should agree with:
    #   1. the STATE column sharing the most name tokens (same entity —
    #      "Insured Zip Code"↔"Insured State", "…Broker Zip"↔"…Broker State"); and
    #   2. when the ZIP denotes the RISK LOCATION (insured/risk/situs/property),
    #      EVERY other risk-location STATE column too — so an "Insured Zip Code"
    #      is also checked against a "Risk State" (the covered risk sits at the
    #      insured location). Transaction-party states (broker/filing/mailing) are
    #      excluded from (2) because they legitimately differ from the risk ZIP.
    # Generic insurance reference vocabulary (same category as the zip/postal
    # tokens) — NO hardcoded column names or MGA-specific values. Deduped by pair.
    _RISK_LOCATION = ("insured", "risk", "situs", "property", "location",
                      "exposure", "subject", "physical")
    _TXN_PARTY = ("broker", "surplus", "filing", "mailing", "agent", "producer",
                  "lender", "mortgagee", "billing")

    def _entity_toks(n):
        # column_tokens splits camelCase too, so "InsuredZipCode" and "InsuredState"
        # share the {insured} entity token instead of being two opaque words.
        return {t for t in column_tokens(n)
                if t not in {"zip", "code", "postal", "postcode", "state",
                             "the", "of"}}

    def _is_risk_location(n):
        ln = (n or "").lower()
        return any(t in ln for t in _RISK_LOCATION) and not any(
            t in ln for t in _TXN_PARTY)

    # The SAME token test that gates loading the reference table (and that emits the
    # state rules below), so a ZIP is never paired with a "Real Estate" column and a
    # "RiskState" is never missed for want of a separator.
    state_cols = [n for n in names if is_state_column(n)]
    risk_state_cols = [s for s in state_cols if _is_risk_location(s)]
    seen_pairs = set()

    # A COUNTRY column, when the template has one, tells the postal check WHICH
    # country's reference to validate against (US ZIP / Canadian FSA / UK outward
    # code) instead of accepting a match in any of them. Bound by shared entity
    # tokens exactly like the ZIP↔STATE pairing itself, so an "Insured Zip Code" is
    # dispatched by "Insured Country" and never by a broker's country. `country_cols`
    # is defined with the currency rules further down; compute it here too because
    # the postal rules are emitted first.
    _country_cols_for_postal = [n for n in names if _is_country_column(n, by_name)]

    def _country_for(col):
        """The country column sharing the most entity tokens with `col`, else the
        single risk-location country column if there is exactly one, else None —
        never a guess when several unrelated country columns could apply.

        The risk-location fallback is offered only to a RISK column. A broker's or
        surplus-lines filer's address sits wherever that party is domiciled, which
        on international business is routinely NOT the risk's country — binding
        "Broker State" to "Insured Country" would judge a US broker against the UK
        reference and flag every row."""
        if not _country_cols_for_postal:
            return None
        t = _entity_toks(col)
        best, best_score = None, 0
        for cc in _country_cols_for_postal:
            score = len(t & _entity_toks(cc))
            if score > best_score:
                best, best_score = cc, score
        if best and best_score >= 1:
            return best
        if any(t2 in (col or "").lower() for t2 in _TXN_PARTY):
            return None
        risk = [c for c in _country_cols_for_postal if _is_risk_location(c)]
        if len(risk) == 1:
            return risk[0]
        # A single UNQUALIFIED country column ("Country", "Country Code") is the
        # bordereau's own country — the common shape of a one-country file, which
        # names no party because there is only one. It answers for the risk's state
        # exactly as an "Insured Country" would. Anything belonging to a named
        # transaction party is excluded first, so a lone "Broker Country" is never
        # read as the risk's country; two or more remain ambiguous → no binding.
        neutral = [c for c in _country_cols_for_postal
                   if not any(t2 in c.lower() for t2 in _TXN_PARTY)]
        return neutral[0] if len(neutral) == 1 else None

    # Which country the CONTRACT pins the risk to, if any (see
    # contract_postal_countries). Resolved ONCE for the whole template.
    _contract_countries = contract_postal_countries(synth_outputs, template_fields)

    def _country_scope(*cols):
        """How this postal rule decides WHICH country to validate against, in the
        order the contract implies:

          1. the CONTRACT names the risk's country → that country only, and the
             BDX's own country column does not get to override it (a row claiming
             another country is exactly the discrepancy the contract check exists
             to surface);
          2. otherwise the BDX COUNTRY column → per-row dispatch (unchanged);
          3. otherwise nothing → the value passes if it is valid in ANY supported
             country (unchanged).

        Returns (params_patch, scope_label) — or None when the rule must NOT be
        emitted at all, i.e. the contract pins a country we hold no postal
        reference data for, where any check would flag every single row.

        A TRANSACTION-PARTY column (broker / surplus-lines filing / mailing
        address) is deliberately left on the column dispatch even when the
        contract pins a country: the contract governs where the RISK sits, not
        where the broker placing it is domiciled."""
        risk_cols = [c for c in cols
                     if not any(t in (c or "").lower() for t in _TXN_PARTY)]
        if _contract_countries is not None and risk_cols:
            if not _contract_countries:
                return None
            from contract_upload_services.intl_postal_reference import (
                country_display_name)
            label = " / ".join(country_display_name(c) for c in _contract_countries)
            return {"countries": list(_contract_countries)}, label
        for c in cols:
            coc = _country_for(c)
            if coc:
                return {"country_field": coc}, f"the country in {coc}"
        return {}, ""

    def _emit_zip_rule(zc, sc):
        key = (zc, sc)
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        scope = _country_scope(zc, sc)
        if scope is None:
            return
        patch, label = scope
        params = {"zip_field": zc, "state_field": sc}
        params.update(patch)
        scoped = f" for {label}" if label else ""
        entries.append({
            "clause": {"clause_id": None,
                       "text": (f"[Derived rule] {zc} must be a valid postal code "
                                f"for {sc}"),
                       "page_number": None},
            "engine": "ir",
            "candidates": [{
                "template": "zip_state_consistency",
                "params": params,
                "rule_name": f"{zc} valid for {sc}",
                "rule_description": (
                    f"{zc} must be a postal code consistent with {sc} on the "
                    f"same row{scoped}."),
                "severity": "warning",
                "error_message": f"{zc} is not a valid postal code for {sc}.",
                "confidence": 1.0,
            }],
        })

    for zc in names:
        if not is_postal_column(zc):
            continue
        # (1) best shared-token state (the ZIP's own entity)
        zt = _entity_toks(zc)
        best, best_score = None, 0
        for sc in state_cols:
            score = len(zt & _entity_toks(sc))
            if score > best_score:
                best, best_score = sc, score
        if best and best_score >= 1:
            _emit_zip_rule(zc, best)
        # (2) a risk-location ZIP also validates every risk-location STATE column
        if _is_risk_location(zc):
            for sc in risk_state_cols:
                _emit_zip_rule(zc, sc)

    # STATE validity (data-quality). A STATE column must hold a real state /
    # province / region, so emit ONE state_validity rule per state column — the
    # risk's state AND every transaction party's (broker/filing/mailing), because a
    # typo is a typo whoever the column belongs to. Unlike the ZIP check above this
    # needs no pairing: a state is checkable on its own against the reference
    # table's authoritative per-country vocabulary.
    #
    # ONLY when the country IS known, though. A state rule with no country resolves
    # against the UNION of every country we hold data for, so it says nothing about
    # a region of any OTHER country — a German state or a French department is
    # reported as invalid purely because we hold no data for it. Rather than ship a
    # check that cannot tell "typo" from "country we do not cover", the rule is not
    # emitted at all when neither the contract nor a BDX country column names the
    # country. The ZIP rule above is still emitted in that case: its per-country
    # SHAPE guard skips a code belonging to none of them, so it cannot make that
    # mistake, and it validates the state column as part of the pair anyway.
    #
    # `is_state_column` matches whole name TOKENS (never the bare substring, which
    # would also fire on "Real Estate"/"Statement Date") and is imported from the
    # reference module that gates loading the table — one definition, so a rule is
    # never emitted for a column the table wasn't loaded for. Deduped by column
    # name: `names` can repeat a column across sheets, and the compiler already
    # fans a single rule out over every sheet that carries the column.
    seen_state_cols = set()
    for sc in names:
        if not is_state_column(sc) or sc in seen_state_cols:
            continue
        seen_state_cols.add(sc)
        scope = _country_scope(sc)
        if scope is None:
            continue
        patch, label = scope
        if not patch:
            continue        # no country determinable — see the note above
        params = {"state_field": sc}
        params.update(patch)
        scoped = f" of {label}" if label else ""
        entries.append({
            "clause": {"clause_id": None,
                       "text": (f"[Derived rule] {sc} must be a valid state / "
                                f"province / region"),
                       "page_number": None},
            "engine": "ir",
            "candidates": [{
                "template": "state_validity",
                "params": params,
                "rule_name": f"{sc} is a valid state / province / region",
                "rule_description": (
                    f"{sc} must be a real state, province or region{scoped} — "
                    f"either its official code or its full name."),
                "severity": "warning",
                "error_message": f"{sc} is not a valid state / province / region.",
                "confidence": 1.0,
            }],
        })

    # CURRENCY ↔ COUNTRY consistency (data-quality). Same shape as ZIP↔STATE above:
    # for every CURRENCY column, pair it with (1) the COUNTRY column sharing the
    # most name tokens (same entity, e.g. "Original Currency"↔ nothing shared →
    # falls through to (2)); and (2) when the currency is the RISK's own currency
    # (no txn-party qualifier), every RISK-LOCATION country column — so "Original
    # Currency" validates against "Insured Country" but never against a broker's
    # or filing agent's country (those legitimately differ from the risk's
    # currency). Reuses the risk-location/txn-party vocabulary defined above.
    # Word-boundary regex so "MTC per occurrence…" never matches (not "currenc").
    _CURRENCY_MARKER = re.compile(r"\bcurrenc(?:y|ies)\b", re.IGNORECASE)
    country_cols = [n for n in names if "countr" in n.lower()]
    risk_country_cols = [c for c in country_cols if _is_risk_location(c)]
    seen_curr_pairs = set()

    def _emit_currency_rule(cyc, coc):
        key = (cyc, coc)
        if key in seen_curr_pairs:
            return
        seen_curr_pairs.add(key)
        entries.append({
            "clause": {"clause_id": None,
                       "text": f"[Derived rule] {cyc} must be a valid currency for "
                               f"{coc}",
                       "page_number": None},
            "engine": "ir",
            "candidates": [{
                "template": "currency_country_consistency",
                "params": {"currency_field": cyc, "country_field": coc},
                "rule_name": f"{cyc} valid for {coc}",
                "rule_description": (
                    f"{cyc} must be a currency that is legal tender in {coc} on "
                    f"the same row."),
                "severity": "warning",
                "error_message": f"{cyc} is not a valid currency for {coc}.",
                "confidence": 1.0,
            }],
        })

    # SAMPLE-EVIDENCE gate: a "Currency"-named column qualifies only when its
    # DATA looks like currency codes. Real bordereaux name AMOUNT columns
    # "Net Payable Settlement Currency" (meaning: the net payable IN the
    # settlement currency) — the word alone bound a currency-validity rule to
    # amounts and flagged every row ("12276.6 is not a valid currency for
    # UNITED KINGDOM"). A column with NO samples keeps the rule (no evidence
    # against it); one code-looking sample is enough to keep it.
    _cur_sample_pool: dict = {}
    for _f in (template_fields or []):
        _n = _f.get("name")
        for _v in (_f.get("samples_all") or _f.get("samples") or []):
            if _n and str(_v).strip():
                _cur_sample_pool.setdefault(_n, []).append(str(_v).strip())
    _CUR_CODE_RE = re.compile(r"^[A-Za-z]{3}$")

    def _samples_look_like_currency_codes(col):
        vals = _cur_sample_pool.get(col)
        if not vals:
            return True
        return any(_CUR_CODE_RE.match(v) for v in vals)

    for cyc in names:
        if not _CURRENCY_MARKER.search(cyc):
            continue
        if not _samples_look_like_currency_codes(cyc):
            continue    # an AMOUNT column that merely mentions "currency"
        ct = _entity_toks(cyc)
        best, best_score = None, 0
        for coc in country_cols:
            score = len(ct & _entity_toks(coc))
            if score > best_score:
                best, best_score = coc, score
        if best and best_score >= 1:
            _emit_currency_rule(cyc, best)
        # Gate on "not txn-party" rather than requiring an explicit risk-location
        # token ON the currency column itself (unlike the ZIP gate above): a premium
        # currency column is very rarely named "Insured Currency" — it is usually
        # just "Original Currency"/"Currency" with no entity qualifier at all, which
        # by default means it's the RISK's own currency. Only an explicitly
        # txn-party-qualified currency column (e.g. "Broker Currency") is excluded.
        if not any(t in cyc.lower() for t in _TXN_PARTY):
            for coc in risk_country_cols:
                _emit_currency_rule(cyc, coc)
    return entries


# =====================================================================
# Derived FORMULA rules from a per-column formula annotation
# =====================================================================
# Some output templates carry a per-column FORMULA note (a row placed directly
# above the header, captured by the template parser into field["formula"]) that
# states, in plain arithmetic, how a computed column is derived — e.g.
#   "Palms part of Limit $ = 100% policy Limit * Palms Part of Limit %"
#   "Payable due AmWins Re = Palms Gross Written Premium $ - Gross Commission …"
#   "50% * (Retained Commisson + Fronting)"   (no explicit target → the column
#                                              the note is attached to)
# These are the DOMAIN EXPERT's exact definitions, so turning them into rules is
# the safest possible derivation — nothing is guessed. The parser is fully
# generic: it resolves every operand name against the REAL template columns and
# emits a cross_field_math (field op field) or cross_field_compare (field op a
# constant-scaled field) rule. No column name or MGA value is hardcoded.


def _norm_col(s):
    """Loose normalization for matching a formula operand phrase to a real column
    name — lowercase, then punctuation collapsed to single spaces. The '$' and '%'
    markers are PRESERVED as the distinct tokens 'dollar'/'pct' (NOT stripped),
    because they are the ONLY thing distinguishing an amount column from its rate
    column with the same words — e.g. "Palms part of Limit $" (the amount) vs
    "Palms Part of Limit %" (the rate) would otherwise normalize identically and
    the wrong one could be picked as an operand."""
    s = (s or "").lower().replace("%", " pct ").replace("$", " dollar ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


#: An operand phrase that still carries ARITHMETIC of its own. `_norm_col` throws
#: away operators and leaves bare numbers as ordinary tokens, so "1 - Company Cede"
#: normalises to "1 company cede" and then fuzzy-matches "Company Cede" on two
#: shared tokens — silently DISCARDING the "1 -". The resulting rule checks
#: `base * rate` where the formula said `base * (1 - rate)`, which is wrong on
#: every row that has a non-zero rate. `*` and `+` are rejected wherever they
#: appear (no column name uses them as anything but an operator once the exact
#: match above has already had its chance); `-` and `/` only when SPACED, because
#: real headers contain them unspaced ("CLAIMS MADE/OCCURRENCE", "SELF-INSURED").
_FORMULA_OPERAND_ARITH = re.compile(r"[*+]|\s[-/]\s")


def _resolve_formula_col(phrase, names, norm_pairs):
    """Resolve one operand phrase from a formula to a real template column name.
    Exact normalized match first; then the column with the most shared name tokens
    (tie-broken toward the fewest unexplained extra tokens, so "Fronting" resolves
    to "Fronting Fee" not "Retained Commisson + Fronting"). Returns None when
    nothing overlaps — the caller then skips the whole formula (safe).

    The fuzzy fallback REFUSES a phrase that is itself an expression (see
    `_FORMULA_OPERAND_ARITH`): matching it would drop the arithmetic the phrase
    carries and emit a rule that silently checks something else. The exact match
    runs first and is unaffected, so a column whose own NAME contains punctuation
    still resolves."""
    p = _norm_col(phrase)
    if not p:
        return None
    for orig, nrm in norm_pairs:
        if nrm == p:
            return orig
    if _FORMULA_OPERAND_ARITH.search(str(phrase or "")):
        return None
    ptoks = set(p.split())
    best, best_score = None, None
    for orig, nrm in norm_pairs:
        ctoks = set(nrm.split())
        shared = len(ptoks & ctoks)
        if not shared:
            continue
        extra = len(ctoks ^ ptoks)          # symmetric difference — closeness
        score = (shared, -extra)
        if best_score is None or score > best_score:
            best, best_score = orig, score
    return best


_FORMULA_DIV100 = re.compile(r"/\s*100(?:\.0+)?\s*$")

#: "(1 - <col>)" — the COMPLEMENT of a rate. A quota-share bordereau reports the
#: premium NET of a ceding commission as `base * (1 - cede rate)`, which is the
#: only shape in everyday reinsurance arithmetic that needs a constant term, and
#: the one shape neither `cross_field_math` (result = left OP right) nor
#: `cross_field_compare` (field = other OP factor) could express. A trailing
#: "/ 100" INSIDE the group marks a percent-stored rate ("(1 - Cede % / 100)").
_FORMULA_COMPLEMENT = re.compile(
    r"^1(?:\.0+)?\s*[-‐-―−]\s*(.+)$")


def _safe_eval_const(s):
    """Evaluate a PURE-numeric expression (only digits, `. + - * / ( )` and spaces)
    to a float — e.g. "50 / 100" → 0.5. Returns None for anything containing a
    letter. Safe: with no names/builtins a digits-and-operators string can't call
    anything."""
    if s and re.fullmatch(r"[0-9.+\-*/() ]+", s.strip()):
        try:
            return float(eval(s, {"__builtins__": {}}, {}))
        except Exception:
            return None
    return None


def _tokenize_formula_expr(expr, names, norm_pairs):
    """Turn the right-hand side of a formula into an ordered token stream of
    operands (('col', name) / ('num', value) / ('phrase', text-to-fuzzy-resolve))
    and operators (('op', +-*/)). A trailing "/100" is detected and returned
    separately (it marks a percent operand to divide by 100).

    A left-to-right POSITION scan matches the LONGEST column name at each boundary
    FIRST — so a column name that itself contains an operator ("Retained Commisson
    + Fronting") or begins with a number ("100% policy Limit") is matched WHOLE,
    never split on its own '+'/number. Parenthesized groups resolve to a column,
    else to a numeric constant ("(50/100)" → 0.5). An unmatched run becomes a
    'phrase' for the caller to fuzzy-resolve (handles abbreviations like
    "Fronting" → "Fronting Fee")."""
    div100 = bool(_FORMULA_DIV100.search(expr))
    if div100:
        expr = _FORMULA_DIV100.sub("", expr)
    ordered = sorted([n for n in names if n], key=len, reverse=True)
    low = expr.lower()
    tokens, pending = [], []
    i, N = 0, len(expr)

    def _flush():
        if pending:
            tokens.append(("phrase", " ".join(pending).strip()))
            pending.clear()

    while i < N:
        ch = expr[i]
        if ch.isspace():
            i += 1; continue
        if ch == "(":
            _flush()
            depth, j = 1, i + 1
            while j < N and depth > 0:
                depth += (expr[j] == "(") - (expr[j] == ")")
                j += 1
            content = expr[i + 1:j - 1].strip()
            # "(1 - <col>)" is a rate COMPLEMENT, not a column: resolving it as one
            # would throw the "1 -" away (see _FORMULA_OPERAND_ARITH). An inner
            # trailing "/100" belongs to the rate, so it is stripped here and
            # carried on the token rather than leaking into the outer div100.
            mc = _FORMULA_COMPLEMENT.match(content)
            if mc:
                inner = mc.group(1).strip()
                inner_pct = bool(_FORMULA_DIV100.search(inner))
                if inner_pct:
                    inner = _FORMULA_DIV100.sub("", inner).strip()
                col = _resolve_formula_col(inner, names, norm_pairs)
                tokens.append(("colc_pct" if inner_pct else "colc", col)
                              if col else ("col", None))
                i = j; continue
            col = _resolve_formula_col(content, names, norm_pairs)
            if col:
                tokens.append(("col", col))
            else:
                val = _safe_eval_const(content)
                tokens.append(("num", val) if val is not None else ("col", None))
            i = j; continue
        if ch in "+*/":
            _flush(); tokens.append(("op", ch)); i += 1; continue
        if ch == "-" and (i == 0 or expr[i - 1] in " (") and (
                i + 1 >= N or expr[i + 1] == " "):
            _flush(); tokens.append(("op", "-")); i += 1; continue
        matched = None
        for nm in ordered:
            if low.startswith(nm.lower(), i):
                matched = nm; break
        if matched:
            _flush(); tokens.append(("col", matched)); i += len(matched); continue
        if not pending:
            m = re.match(r"\d+(?:\.\d+)?%?", expr[i:])
            if m and m.group(0):
                tok = m.group(0); val = float(tok.replace("%", ""))
                if tok.endswith("%"):
                    val /= 100.0
                tokens.append(("num", val)); i += len(tok); continue
        m = re.match(r"[^-+*/()\s]+", expr[i:])
        if m:
            pending.append(m.group(0)); i += len(m.group(0))
        else:
            i += 1
    _flush()
    return tokens, div100


#: A "/" that is doing ARITHMETIC — spaced ("A / B"), or dividing a literal
#: ("… / 100"). Deliberately NOT a bare slash: real headers and prose notes carry
#: those ("CLAIMS MADE/OCCURRENCE", "validate zip w/ state") and admitting them
#: would send free text into the formula parser.
_FORMULA_DIV_ARITH = re.compile(r"\s/\s|/\s*\d")


def _looks_arithmetic_formula(raw):
    """True when a per-column annotation is an ARITHMETIC formula rather than a
    prose note. An explicit "=" settles it; otherwise the note must carry an
    operator: "+", "*", a SPACED "-" (unspaced hyphens are ordinary in headers),
    or a "/" that is dividing something (see `_FORMULA_DIV_ARITH`).

    "/" used to be missing from this list, so a quotient — "Palms (Direct) / 0.2",
    any "… / 100" — was classified as prose and dropped before it was ever parsed.
    The AI formula inference returns RIGHT-HAND SIDES only, which arrive here bare,
    so every inferred division was silently lost."""
    raw = (raw or "").strip()
    if not raw:
        return False
    return ("=" in raw or "+" in raw or "*" in raw or " - " in raw
            or bool(_FORMULA_DIV_ARITH.search(raw)))


def _definitional_formula_fields(synth_outputs):
    """Fields that ALREADY carry a DEFINITIONAL formula — an equality that pins the
    field's value: a cross_field_math, or a cross_field_compare / cross_field_or_value
    whose op is '='. A newly-derived formula for such a field would be a duplicate,
    so it is skipped. An INEQUALITY LIMIT on the field (≤ / ≥, e.g. "Payable due
    AmWins Re ≤ 10% of net facultative premium") is a DIFFERENT constraint and does
    NOT block the field's definitional formula — the two coexist (one caps the value,
    the other defines how it is computed)."""
    out = set()
    for entry in (synth_outputs or []):
        for ir in (entry.get("candidates") or []):
            t = ir.get("template")
            p = ir.get("params") or {}
            if t == "cross_field_math" and p.get("result_field"):
                out.add(p["result_field"])
            elif t in ("cross_field_compare", "cross_field_or_value") \
                    and p.get("op") == "=" and p.get("field"):
                out.add(p["field"])
    return out


def _has_numeric_sample(field):
    """True when a column is safe to use in an arithmetic formula: it has NO samples
    (unknown — give the benefit of the doubt) OR at least one numeric sample. False
    ONLY when it HAS samples and every one is non-numeric — a text column that must
    never enter a cross-field math rule. This stops a mis-captured note (e.g. a
    grouped/multi-row header label sitting above the real header) from binding text
    columns into an arithmetic rule that would flag every row."""
    samples = (field or {}).get("samples") or []
    if not samples:
        return True
    for s in samples:
        try:
            float(str(s).replace(",", "").replace("$", "").replace("%", "").strip())
            return True
        except (TypeError, ValueError):
            continue
    return False


def infer_formula_annotations(template_fields):
    """ONE Gemini call that identifies which output-template columns are COMPUTED
    (arithmetically derived from other columns) and returns each one's formula —
    the AI-inferred equivalent of a template's per-column formula-annotation row,
    for the common case of a clean, header-only template that carries no such row.

    The model sees every column (name + a few sample values) and returns
    {"formulas":[{"target": <exact column name>, "formula": <expression using exact
    column names + - * / and an optional /100>}]}. Fully generic — the MODEL decides
    which columns are formulas (works for columns this code has never seen); nothing
    is hardcoded. The returned expressions feed derive_annotation_formula_entries,
    which resolves every operand to a real column and emits the cross-field rule, so
    a slightly-off name or a wrong operand is caught downstream (unresolved → the
    formula is skipped). Returns {column_name: "expression"} (RHS only), validated so
    the target is a real column. Best-effort: any failure returns {} and never
    disrupts generation. Disabled with KAVACHIO_AI_FORMULAS=0."""
    if os.getenv("KAVACHIO_AI_FORMULAS", "1") == "0":
        return {}
    fields = [f for f in (template_fields or []) if f.get("name")]
    if not fields:
        return {}
    names = {f["name"] for f in fields}
    catalog = "\n".join(
        f"- {f['name']}" + (
            f"   e.g. [{', '.join(str(s) for s in (f.get('samples') or [])[:3])}]"
            if f.get("samples") else "")
        for f in fields)
    # COST: the answer depends ONLY on this column catalog — nothing from the
    # contract reaches this prompt — so every upload against a template re-bought
    # it. `catalog` is the literal prompt input, which makes it the exact and
    # complete key: two uploads building the same catalog would get the same
    # answer, and any rename / added column / changed sample rewrites it.
    _key = ai_cache.make_key("formula_infer_v1", catalog)
    _cached = ai_cache.get("formula_infer", _key)
    if _cached is not None:
        return dict(_cached)
    prompt = f"""You are analyzing the columns of an insurance bordereau (BDX) reporting template.
Each line is a column name and up to 3 sample values.

TASK: Identify every column that is a COMPUTED/DERIVED value obtained by ARITHMETIC from OTHER listed columns
(e.g. an amount = a premium/limit base multiplied by a percentage rate; a net = a gross minus a deduction;
a party's share = a total times a participation percent; a sum or difference of two money columns).
For each such column, write its formula.

STRICT RULES:
- Use ONLY column names copied VERBATIM from the list below (exact spelling, spacing, $/% included).
- Write a FLAT expression with exactly ONE operator between TWO operands, using + - * /.
- ONE exception to "flat": when a column is a base NET OF a rate (the part of the base that REMAINS
  after a rate is deducted — e.g. a premium net of a ceding commission), write it as
  "<base column> * (1 - <rate column>)". Check this against the samples: if <result> is a LARGE
  fraction of <base> (say 70-95%) and <rate> is the small remainder, it is the complement form,
  NOT "<base> * <rate>". Getting this backwards produces a formula wrong on every row.
- Look at the SAMPLE VALUES to decide scaling:
    * A rate column whose samples look like a PERCENT (e.g. 15, 30, 12.5) → append " / 100" at the very END of the formula.
    * A rate column whose samples are already a 0-1 FRACTION (e.g. 0.2, 0.15) → use it AS-IS, do NOT divide by 100.
    * A fixed fraction such as one-half → write the decimal 0.5 (never "50 / 100").
- Only output a column when you are CONFIDENT it is arithmetically derived from the named columns (the name
  and/or the sample magnitudes make the relationship clear). When unsure, OMIT it — do not guess.
- A party's SHARE of a total (e.g. that party's premium/limit for a specific peril or segment) is typically the
  corresponding 100%/total column times the party's participation percent. Include such share formulas even when
  the column's sample values are BLANK or ZERO, as long as the column NAMES make the relationship unambiguous.
- Keep operands ENTITY-consistent: a "Palms ..." amount uses Palms / 100% / total columns, not a different party's column.
- Do NOT output raw input columns (dates, names, addresses, ids, codes, or premiums/limits that are inputs).
- Output STRICT JSON only, no prose:
  {{"formulas":[{{"target":"<exact column name>","formula":"<flat expression using exact column names>"}}]}}

COLUMNS:
{catalog}
"""
    try:
        raw = call_gemini(prompt, label="FormulaInference", temperature=0,
                          seed=DETERMINISTIC_SEED, max_output_tokens=8192,
                          thinking_budget=4096)
        data = parse_llm_json(raw)
    except Exception as exc:
        print(f"[Formula AI] inference skipped ({exc})")
        return {}
    items = data.get("formulas") if isinstance(data, dict) else data
    out = {}
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        tgt = (it.get("target") or it.get("column") or "").strip()
        expr = (it.get("formula") or it.get("expression") or "").strip()
        # target must be a real column and the expression must reference OTHER
        # columns (contain an operator) — a bare copy is not a formula.
        if tgt in names and expr and any(op in expr for op in "+-*/"):
            out[tgt] = expr
    # Store even an EMPTY result: "this template has no computed columns" is a real,
    # reusable answer, and not storing it would re-ask on every upload for exactly
    # the templates where the call finds nothing. Only a FAILED call (the `except`
    # above, which returns early) skips the store, so a transient error is retried.
    ai_cache.put("formula_infer", _key, out)
    return out


def _emit_formula_entry(result, template, params, desc_bits):
    ir = {
        "template": template,
        "params": params,
        "rule_name": desc_bits["name"],
        "rule_description": desc_bits["desc"],
        "severity": "warning",
        "error_message": desc_bits["err"],
        "confidence": 1.0,
    }
    return {
        "clause": {"clause_id": None,
                   "text": f"[Derived formula] {desc_bits['name']}",
                   "page_number": None},
        "engine": "ir",
        "candidates": [ir],
    }


def derive_annotation_formula_entries(synth_outputs, template_fields):
    """Derive cross-field rules from a per-column FORMULA annotation carried on the
    output template (field["formula"]). Each annotation is either "TARGET = EXPR"
    (target named explicitly, possibly a DIFFERENT column than the one the note
    sits on) or a bare "EXPR" (target = the column the note is attached to). EXPR
    is parsed generically into a two-operand arithmetic and mapped to:
      • cross_field_math      when EXPR is  <col> <+-*/> <col>  (± a "/100")
      • cross_field_math      when EXPR is  <col> * (1 - <col>)  (a rate complement)
      • cross_field_compare   when EXPR is  <const> * <col>  (or <col> * <const>)
      • cross_field_compare(=) when EXPR is a single <col>  (a definitional copy)
    Deterministic, exact (the expert wrote the formula), generic (no hardcoded
    names/values). Skips any result field already governed by a math/compare rule
    (so it never duplicates the mapper's or another deriver's output). Returns a
    list of synth_output entries to append.

    Every reason a formula is DROPPED is logged. These were bare `continue`s, and
    a template whose columns simply do not fit the two-operand grammar produced no
    rule and no trace of why — indistinguishable, in a run log, from a template
    that carried no formulas at all."""
    names = [f.get("name") for f in (template_fields or []) if f.get("name")]
    by_name = {f.get("name"): f for f in (template_fields or []) if f.get("name")}
    norm_pairs = [(n, _norm_col(n)) for n in names]

    # Result fields already covered by a math/compare rule (mapped or derived) —
    # don't emit a second formula for the same column.
    # Only a DEFINITIONAL formula already on a field blocks a new one (a duplicate);
    # an inequality LIMIT on the same field is a different constraint and coexists.
    covered = _definitional_formula_fields(synth_outputs)

    def _skip(col, raw, why):
        print(f"  [annotation-formula] skipped {col!r} ({raw!r}) — {why}")

    entries = []
    for f in (template_fields or []):
        raw = (f.get("formula") or "").strip()
        if not raw or not _looks_arithmetic_formula(raw):
            # Not an arithmetic formula (e.g. "Validate zip code…" note) — skipped
            # here; ZIP/currency are handled by derive_formula_entries.
            continue
        # split TARGET = EXPR (first '='); bare EXPR → target is this column.
        if "=" in raw:
            lhs, _, rhs = raw.partition("=")
            target = _resolve_formula_col(lhs, names, norm_pairs)
            expr = rhs.strip()
        else:
            target = f.get("name")
            expr = raw
        if not target or target not in by_name or not expr:
            _skip(f.get("name"), raw, "target does not resolve to a template column")
            continue

        toks, div100 = _tokenize_formula_expr(expr, names, norm_pairs)
        # fuzzy-resolve any leftover phrase tokens to real columns
        resolved = []
        ok = True
        for kind, val in toks:
            if kind == "phrase":
                col = _resolve_formula_col(val, names, norm_pairs)
                if not col:
                    _skip(target, raw, f"operand {val!r} matches no template column")
                    ok = False; break
                resolved.append(("col", col))
            elif kind in ("col", "colc", "colc_pct") and not val:
                _skip(target, raw, "an operand could not be resolved")
                ok = False; break
            else:
                resolved.append((kind, val))
        if not ok:
            continue

        operands = [(k, v) for k, v in resolved
                    if k in ("col", "num", "colc", "colc_pct")]
        ops = [v for k, v in resolved if k == "op"]
        if target in covered:
            _skip(target, raw, "already governed by a definitional formula rule")
            continue
        # Every column in the formula (target + operands) must be numeric-valued —
        # a text column here means the captured note was NOT really a formula (e.g.
        # a grouped-header label above the real header), so skip rather than emit a
        # rule that would flag every row.
        involved = [target] + [v for k, v in operands if k != "num"]
        if not all(_has_numeric_sample(by_name.get(c, {})) for c in involved):
            _skip(target, raw, "a column in the formula holds no numeric sample")
            continue

        # --- single-operand definitional copy: TARGET = other_col
        if len(operands) == 1 and not ops and operands[0][0] == "col":
            oc = operands[0][1]
            if oc == target:
                continue
            params = {"field": target, "op": "=", "other_field": oc,
                      "tolerance": 0.01}
            tail = ""
            if div100:
                # "TARGET = <col> / 100" is a SCALED copy, not a plain one.
                # cross_field_compare carries the whole scale in its single
                # `factor`, so the trailing /100 has to be folded in here — dropped,
                # the rule would demand a value 100× too large on every row.
                params.update({"operator": "*", "factor": 0.01})
                tail = " * 0.01"
            entries.append(_emit_formula_entry(
                target, "cross_field_compare", params,
                {"name": f"{target} equals {oc}{tail}",
                 "desc": f"{target} must equal {oc}{tail} on the same row.",
                 "err": f"{target} does not equal {oc}{tail}."}))
            covered.add(target); continue

        # --- two operands, one operator
        if len(operands) == 2 and len(ops) == 1:
            op = ops[0]
            (k0, v0), (k1, v1) = operands
            cols = [v for k, v in operands if k == "col"]
            nums = [v for k, v in operands if k == "num"]

            # constant-scaled single field:  TARGET = k * col   /  col * k
            if op in ("*", "+", "-") and len(cols) == 1 and len(nums) == 1:
                col, factor = cols[0], nums[0]
                if col == target:
                    continue
                if div100:
                    # A trailing "/100" marks the CONSTANT as a percent written as
                    # a whole number ("<base> * 5 / 100" for a 5% share). The
                    # cross_field_math branch below passes that scale downstream as
                    # `right_is_percent`, but cross_field_compare has no such flag —
                    # its ONE `factor` is the entire scale, so the /100 must be
                    # folded into the constant right here. Left out, the rule checks
                    # <base> × 5 (100× the intended value) and flags every row.
                    if op != "*":
                        continue    # "/100" over a sum has no single-factor form
                    factor = round(factor / 100.0, 12)
                tail = f" {op} {factor}"
                entries.append(_emit_formula_entry(
                    target, "cross_field_compare",
                    {"field": target, "op": "=", "other_field": col,
                     "operator": op, "factor": factor, "tolerance": 0.01},
                    {"name": f"{target} equals {col}{tail}",
                     "desc": f"{target} must equal {col}{tail} on the same row.",
                     "err": f"{target} does not equal {col}{tail}."}))
                covered.add(target); continue

            # field op field:  cross_field_math  (either side may be a "(1 - col)"
            # complement, which rides along as left_complement/right_complement)
            if k0 in ("col", "colc", "colc_pct") and k1 in ("col", "colc", "colc_pct") \
                    and op in ("+", "-", "*", "/"):
                left, right = v0, v1
                if target in (left, right):
                    continue
                params = {"result_field": target, "left_field": left,
                          "operator": op, "right_field": right,
                          "tolerance_pct": DEFAULT_CROSS_FIELD_TOLERANCE_PCT}
                for slot, kind in (("left", k0), ("right", k1)):
                    if kind in ("colc", "colc_pct"):
                        params[f"{slot}_complement"] = True
                    if kind == "colc_pct":
                        params[f"{slot}_is_percent"] = True
                if op in ("*", "/") and div100:
                    # A trailing "/100" divides the PRODUCT once, so dividing either
                    # factor by 100 is equivalent — apply it to the right operand.
                    # We trust the formula literally (the "/100" is present exactly
                    # when a percent-stored rate is involved): a formula with NO
                    # "/100" is left un-scaled, so a 0-1 fraction column (e.g.
                    # Part-of-Limit % = 0.2) or a "100% … Limit" $ base is used as-is
                    # and never wrongly divided by 100.
                    params["right_is_percent"] = True
                sym = op
                ltxt = f"(1 - {left})" if k0 in ("colc", "colc_pct") else left
                rtxt = f"(1 - {right})" if k1 in ("colc", "colc_pct") else right
                entries.append(_emit_formula_entry(
                    target, "cross_field_math", params,
                    {"name": f"{target} equals {ltxt} {sym} {rtxt}",
                     "desc": (f"{target} must equal {ltxt} {sym} {rtxt} "
                              f"(within a small tolerance for rounding)."),
                     "err": f"{target} does not match {ltxt} {sym} {rtxt}."}))
                covered.add(target); continue
        # 3+ operands or unsupported shape — skip (rare; left for a clause rule).
        _skip(target, raw,
              f"unsupported shape: {len(operands)} operand(s), {len(ops)} operator(s) "
              f"— the deriver expresses one operator between two operands")
    return entries


# Concepts whose "<entity> <concept> Amount $" column is a per-row rate applied to
# a base — generic insurance rate/participation vocabulary, NOT column names. Used
# to structurally derive an amount = base × rate formula on templates that carry NO
# formula annotation (the clean, header-only case). "part of"/"participation" are
# the generic PARTICIPATION-share concepts (a party's share of a limit/layer), the
# same category of vocabulary as the rate concepts — not a template-specific literal.
_AMOUNT_CONCEPTS = ("commission", "brokerage", "broker", "fee", "tax", "cede",
                    "cession", "ceding", "override", "part of", "participation")


def derive_rate_amount_formulas(synth_outputs, template_fields):
    """Structural fallback for templates WITHOUT a formula annotation: for every
    "<entity> <concept> Amount $" (or "… $") column that has BOTH a sibling
    per-row "<entity> <concept> %" rate column AND an entity-matched base column
    (a Premium or a Limit), emit  amount = base × rate. Generic — the amount, its
    rate, and its base are all matched by shared ENTITY tokens AND the same
    financial CONCEPT (so "Palms part of Limit $" binds to "Palms Part of Limit %"
    and "100% policy Limit", never a commission %). Deduped against the mapper and
    the other derivers by result field, so it never double-emits a formula that
    derive_formula_entries or derive_annotation_formula_entries already produced.

    This complements derive_formula_entries (which only derives the ONE commission
    trio, and only when the rate is already governed by a clause). Here the very
    existence of the amount/rate/base trio in the template is the trigger — these
    are template-defined computed columns, not clause-governed constraints."""
    names = [f.get("name") for f in (template_fields or []) if f.get("name")]
    by_name = {f.get("name"): f for f in (template_fields or []) if f.get("name")}

    # Only a DEFINITIONAL formula already on a field blocks a new one (a duplicate);
    # an inequality LIMIT on the same field is a different constraint and coexists.
    covered = _definitional_formula_fields(synth_outputs)

    def _concept(name):
        ln = (name or "").lower()
        for c in _AMOUNT_CONCEPTS:
            if c in ln:
                return c
        return None

    pct_cols = [n for n in names if "%" in n or "rate" in n.lower()]
    # A base is a Premium/Limit column that holds a DOLLAR amount — not a rate, and
    # not itself a computed SHARE/AMOUNT. A '%' inside the name is allowed ("100%
    # policy Limit" is a scale label, not a rate); a name ENDING in '%' (or with
    # "rate") is a rate column, excluded. _looks_dollar (magnitude) discriminates a
    # $ from a rate (a rate never exceeds ~10). Crucially, a column that itself names
    # a rate/participation concept (`_concept` → e.g. another party's "… part of
    # Limit $" or a "… Commission Amount $") is a computed SHARE, never a base — the
    # base of a share is the WHOLE (the 100%/total limit or gross premium). Excluding
    # these stops one party's share being picked as another party's base (which
    # shares the "part"/"limit" tokens and would flag every row).
    base_cols = [n for n in names
                 if any(t in n.lower() for t in ("premium", "limit"))
                 and not n.rstrip().endswith("%") and "rate" not in n.lower()
                 and _concept(n) is None
                 and _looks_dollar(by_name.get(n, {}), n)]

    entries = []
    for amount in names:
        low = amount.lower()
        if "%" in amount or "rate" in low:
            continue                       # a rate column, not an amount
        if not (low.strip().endswith("$") or " amount" in low or low.endswith("amount")
                or "$" in amount):
            continue                       # must look like a $ amount column
        if amount in covered:
            continue
        concept = _concept(amount)
        if not concept:
            continue
        amt_ent = _fin_entity_toks(amount)
        # matching rate: same concept, entity-overlap (or amount has no entity)
        rate_cands = [r for r in pct_cols
                      if concept in r.lower()
                      and (not amt_ent or (amt_ent & _fin_entity_toks(r)))]
        rate = _best_fin_match(amt_ent, rate_cands)
        if not rate:
            continue
        # matching base: prefer a limit base for a "limit" concept, else a premium;
        # entity-matched, never a peril sub-premium.
        # The base is a LIMIT when the amount is a share OF a limit (its own name
        # says "limit"), else a PREMIUM. Keyed on the amount column name, not the
        # concept token, so a participation concept ("part of") still routes to the
        # limit base for a "… part of Limit $" column.
        want_limit = "limit" in low
        cand = [b for b in base_cols
                if b != amount
                and (("limit" in b.lower()) if want_limit else ("premium" in b.lower()))
                and not any(x in b.lower() for x in ("terrorism", "cyber"))]
        same_ent = [b for b in cand if amt_ent & _fin_entity_toks(b)]
        # A PARTY-specific amount (its name carries an entity like "Palms"/"risksmith")
        # must multiply the SAME party's base, never the program-wide "100%"/global
        # total (which has an empty entity and would otherwise win the fewest-extra
        # tie-break — the exact wrong-base bug that flags every row). If no same-party
        # base exists, the base is ambiguous → SKIP (better a missing rule than a
        # book-wide false positive). An entity-LESS amount (single-schedule) has no
        # such ambiguity, so it falls back to the sole/best base as before.
        if amt_ent and not same_ent:
            continue
        base = _best_fin_match(amt_ent, same_ent or cand)
        if not base or base == amount:
            continue
        if not all(_has_numeric_sample(by_name.get(c, {})) for c in (amount, rate, base)):
            continue
        params = {"result_field": amount, "left_field": base, "operator": "*",
                  "right_field": rate,
                  "right_is_percent": _looks_percent(by_name.get(rate, {})),
                  "tolerance_pct": DEFAULT_CROSS_FIELD_TOLERANCE_PCT}
        entries.append(_emit_formula_entry(
            amount, "cross_field_math", params,
            {"name": f"{amount} equals {base} × {rate}",
             "desc": (f"{amount} must equal {base} multiplied by {rate} "
                      f"(within a small tolerance for rounding)."),
             "err": f"{amount} does not match {base} × {rate}."}))
        covered.add(amount)
    return entries


#: Enough rows to believe an identity the SAMPLES alone establish — three rows of
#: exact agreement between independently-varying columns is not a coincidence you
#: hit by accident, and it is what a five-row head sample can supply.
_VERIFIED_FORMULA_MIN_ROWS = 3


def _field_sample_column(field):
    """One column's samples, preferring the ROW-ALIGNED capture. Mirrors
    output_schema.samples_for_grounding, which this deriver cannot use because it
    runs on raw template_fields before the schema is built."""
    for key in ("row_samples", "samples_all", "samples"):
        vals = (field or {}).get(key) or []
        if any(str(v).strip() for v in vals):
            return [str(v) for v in vals]
    return []


def _as_number(s):
    try:
        t = str(s).replace(",", "").replace("$", "").replace("%", "").strip()
        if t in ("", "-", "nan", "None"):
            return None
        if t.startswith("(") and t.endswith(")"):      # (1,234) accounting negative
            t = "-" + t[1:-1]
        return float(t)
    except (TypeError, ValueError):
        return None


def derive_verified_rate_formulas(synth_outputs, template_fields):
    """Find `amount = base × rate` and `amount = base × (1 − rate)` relationships by
    PROVING them against the sampled rows, for the templates the name-driven
    derivers cannot see.

    `derive_rate_amount_formulas` recognises its trio by NAME: the amount must
    carry "$" or the word "amount", the rate must carry "%" or "rate", the base
    must say "premium" or "limit". A bordereau with terse headers — WRITTEN
    PREMIUM / COMPANY CEDE / NET CEDED, where "AMT" is not "amount" and a rate
    column is spelled "CEDE" — matches none of those, so that deriver emits
    nothing at all and formula coverage falls entirely to the one inference call.

    This pass reads VALUES instead of names. A rate is a column whose samples all
    sit within ±1.5 (a 0-1 fraction or a small multiplier); a base is a column of
    money magnitude. Every (base, rate) pair is then tested against the aligned
    rows, and a rule is emitted ONLY where the arithmetic reproduces the reported
    amount to the cent on every usable row — so, unlike the name-driven path, this
    one cannot emit a formula the data contradicts.

    Both the product and its COMPLEMENT are tested: `base × (1 − rate)` is how a
    premium net of a ceding commission is computed, and it is indistinguishable
    from `base × rate` by column name alone — only the numbers tell them apart.

    OFF BY DEFAULT — enable with KAVACHIO_VERIFIED_FORMULAS=1.

    "Verified on the sampled rows" is a weaker guarantee than it sounds once this
    runs across a whole estate: a handful of rows is enough for two unrelated
    columns to sit in exact proportion by accident. Measured over all 614 output
    templates it proposes ~1,666 rules, and the failures are systematic rather
    than rare — a DATE stored as an Excel serial (45992.45…) is just a large
    number here, so a template carrying dates yields

        PolicyInception equals TransactionEffectiveDate × IPRM MTC
        Commission Rate equals TransactionDate * 0.000511

    both arithmetically exact on the samples (the dates are duplicates and the
    multiplier is 1; the rate is constant and 23.5/45987 ≈ 0.000511), and both
    meaningless as checks. The money-magnitude guard below does not exclude them
    because a date serial is larger than any threshold a money column could use.

    So the pass stays available but does not fire unless asked for, and what it
    proposes is meant to be reviewed before it is approved. The guards below (a
    money-valued result, two distinct base values, exact agreement at reporting
    precision, never a column another rule defines) are necessary, not
    sufficient."""
    if os.getenv("KAVACHIO_VERIFIED_FORMULAS", "0") != "1":
        return []
    fields = [f for f in (template_fields or []) if f.get("name")]
    by_name = {f["name"]: f for f in fields}
    covered = _definitional_formula_fields(synth_outputs)

    # One sheet at a time: row i of one sheet is not row i of another, and such a
    # formula does not compile into a per-row check across sheets anyway.
    by_sheet = {}
    for f in fields:
        by_sheet.setdefault(f.get("sheet") or "", []).append(f["name"])

    samples = {n: [_as_number(s) for s in _field_sample_column(by_name[n])]
               for n in by_name}

    def _usable(n):
        return [v for v in samples.get(n) or [] if v is not None]

    entries = []
    for sheet, cols in by_sheet.items():
        rates, bases = [], []
        for n in cols:
            vals = _usable(n)
            if len(vals) < _VERIFIED_FORMULA_MIN_ROWS:
                continue
            if all(abs(v) <= 1.5 for v in vals) and any(v != 0 for v in vals):
                rates.append(n)
            if any(abs(v) > 10 for v in vals):
                bases.append(n)

        money = set(bases)
        for amount in cols:
            # The result of `base × rate` (or of a fixed share of a base) is an
            # AMOUNT, so it must itself look like money. Without this, any rate
            # column that happens to rise in step with a premium over the few
            # sampled rows "verifies" as a fixed share of it — a 0.2/0.4/0.6 cede
            # rate against a 1000/2000/3000 premium is exactly proportional, and
            # the pass would emit "the cede rate equals the premium × 0.0002".
            # Two columns are cheap to line up by accident; the three-column
            # product below is not, but the same reasoning applies to its result.
            if amount in covered or amount not in money:
                continue
            hits = []
            for base in bases:
                if base == amount:
                    continue
                for rate in rates:
                    if rate in (amount, base):
                        continue
                    for complement in (False, True):
                        if _verified_product_holds(samples, amount, base, rate,
                                                   complement):
                            hits.append((base, rate, complement))
            amt_toks = _fin_entity_toks(amount)
            if not hits:
                # No rate COLUMN explains it — try a fixed share of a base instead
                # ("this reinsurer takes 20% of the ceded premium"), where the
                # participation is a contract constant and appears in no column.
                ratios = []
                for b in bases:
                    if b == amount:
                        continue
                    k = _verified_constant_ratio(samples, amount, b)
                    if k is not None:
                        ratios.append((b, k))
                if not ratios:
                    continue
                base, k = sorted(ratios, key=lambda x: (
                    -len(amt_toks & _fin_entity_toks(x[0])),
                    len(_fin_entity_toks(x[0]) - amt_toks), x[0]))[0]
                if len(ratios) > 1:
                    print(f"  [verified-formula] {amount!r}: {len(ratios)} constant "
                          f"ratio(s) verified; chose {base!r} × {k}")
                entries.append(_emit_formula_entry(
                    amount, "cross_field_compare",
                    {"field": amount, "op": "=", "other_field": base,
                     "operator": "*", "factor": k, "tolerance": 0.01},
                    {"name": f"{amount} equals {base} * {k}",
                     "desc": f"{amount} must equal {base} * {k} on the same row.",
                     "err": f"{amount} does not equal {base} * {k}."}))
                covered.add(amount)
                continue
            # Deterministic pick: the pair whose names sit closest to the amount's.

            def _rank(h):
                toks = _fin_entity_toks(h[0]) | _fin_entity_toks(h[1])
                return (-len(amt_toks & toks), len(toks - amt_toks), h[0], h[1], h[2])

            base, rate, complement = sorted(hits, key=_rank)[0]
            if len(hits) > 1:
                print(f"  [verified-formula] {amount!r}: {len(hits)} candidate "
                      f"pair(s) verified; chose {base!r} × {rate!r} "
                      f"(complement={complement})")
            params = {"result_field": amount, "left_field": base, "operator": "*",
                      "right_field": rate,
                      "tolerance_pct": DEFAULT_CROSS_FIELD_TOLERANCE_PCT}
            if complement:
                params["right_complement"] = True
            rtxt = f"(1 - {rate})" if complement else rate
            entries.append(_emit_formula_entry(
                amount, "cross_field_math", params,
                {"name": f"{amount} equals {base} × {rtxt}",
                 "desc": (f"{amount} must equal {base} multiplied by {rtxt} "
                          f"(within a small tolerance for rounding)."),
                 "err": f"{amount} does not match {base} × {rtxt}."}))
            covered.add(amount)
    return entries


def _verified_product_holds(samples, amount, base, rate, complement):
    """True when `amount = base × rate` (or × (1 − rate)) reproduces every usable
    sampled row EXACTLY, at the reporting precision.

    Exactly, not within the rule's tolerance band. The band exists so a live rule
    forgives rounding; used as the test for WHICH columns an identity is about, it
    is far too coarse to choose between them. On the bordereau this was built for,
    a ceding rate of 0.13 and a commission rate of 0.125 sit 0.5% apart, so a 1%
    band accepts BOTH as the rate behind the ceded premium — and the deriver would
    silently pick whichever it saw first, producing a rule that reconciles to the
    wrong column and cannot see a genuine half-percent error. Requiring the
    arithmetic to land on the cent leaves only the true one.

    Rows where any operand is missing, or where the base is zero, carry no
    information and are skipped — a zero base satisfies any rate and would let a
    column of zeros "verify" against anything. The surviving rows must number at
    least `_VERIFIED_FORMULA_MIN_ROWS` AND show at least two distinct (base,
    amount) pairs, so a block of repeated identical rows cannot pin a formula on
    its own."""
    from contract_upload_services.rule_compiler import NUMERIC_MATCH_DECIMALS
    cols = [samples.get(c) or [] for c in (amount, base, rate)]
    if not all(cols):
        return False
    seen, used = set(), 0
    for i in range(min(len(c) for c in cols)):
        a, b, r = cols[0][i], cols[1][i], cols[2][i]
        if a is None or b is None or r is None or b == 0:
            continue
        expected = b * ((1.0 - r) if complement else r)
        if round(a, NUMERIC_MATCH_DECIMALS) != round(expected, NUMERIC_MATCH_DECIMALS):
            return False
        used += 1
        seen.add((b, a))
    return used >= _VERIFIED_FORMULA_MIN_ROWS and len(seen) >= 2


#: A participation the contract fixes rather than a column carries — "20% of the
#: ceded premium". Rounded before comparison so floating-point noise in the
#: division does not split one share into several, and reported at this precision
#: in the rule text, which is why it is not looser.
_RATIO_DECIMALS = 6


def _verified_constant_ratio(samples, amount, base):
    """The constant k for which `amount = base × k` holds on EVERY usable sampled
    row, or None.

    Same exactness rule as `_verified_product_holds`, and the same guards: at least
    `_VERIFIED_FORMULA_MIN_ROWS` rows with a non-zero base, and at least two
    distinct base values, so a run of identical rows cannot pin a ratio. k=1 is
    rejected — a plain copy is a definitional-copy rule, already derived elsewhere,
    and admitting it here would restate every duplicated column as arithmetic."""
    from contract_upload_services.rule_compiler import NUMERIC_MATCH_DECIMALS
    a_col, b_col = samples.get(amount) or [], samples.get(base) or []
    if not a_col or not b_col:
        return None
    k, seen, used = None, set(), 0
    for i in range(min(len(a_col), len(b_col))):
        a, b = a_col[i], b_col[i]
        if a is None or b is None or b == 0:
            continue
        cur = round(a / b, _RATIO_DECIMALS)
        if k is None:
            k = cur
        elif cur != k:
            return None
        if round(a, NUMERIC_MATCH_DECIMALS) != round(b * k, NUMERIC_MATCH_DECIMALS):
            return None
        used += 1
        seen.add(b)
    if k is None or k == 0 or k == 1 or used < _VERIFIED_FORMULA_MIN_ROWS \
            or len(seen) < 2:
        return None
    return k


def _looks_dollar(field, name):
    """A column that holds a monetary amount (for choosing a formula base): a '$'
    in the name, or numeric samples with magnitude > ~10 (a rate/% never is)."""
    if "$" in (name or ""):
        return True
    for s in (field.get("samples") or []):
        try:
            if abs(float(str(s).replace(",", "").replace("$", "").strip())) > 10:
                return True
        except (TypeError, ValueError):
            continue
    return False


def derive_program_period_companion_rules(synth_outputs, template_fields):
    """When a clause establishes a PROGRAM-level date (the program's effective/start
    date, or its expiration/end date) and the mapper turns it into a date_bound on a
    POLICY date field (e.g. "policies must incept on/after the program effective
    date" → Risk Inception Date US >= 2025-09-01), ALSO emit a companion rule on the
    program-level date COLUMN itself: the reported Program Effective/Expiration Date
    must EQUAL that contract date. Two checks from one clause — the policy dates sit
    inside the program period, AND the reported program-period date matches the
    contract.

    Generic: the program-date column is found by ROLE tokens (program + effective /
    expiration), the trigger is any date_bound whose text is about the program's
    effective/expiration date, and the bound DATE is read from that rule — no
    hardcoded column name or date. Deduped so a program-date column that already
    carries a date_bound is left alone. Returns synth_output entries to append."""
    names = [f.get("name") for f in (template_fields or []) if f.get("name")]

    def _find(need, any_of=()):
        for n in names:
            ln = n.lower()
            if all(t in ln for t in need) and (not any_of or any(a in ln for a in any_of)):
                return n
        return None

    prog_eff_col = _find(("program", "effective"))
    prog_exp_col = _find(("program",), any_of=("expir", "expiry"))
    if not prog_eff_col and not prog_exp_col:
        return []

    bounded = set()
    for entry in (synth_outputs or []):
        for ir in (entry.get("candidates") or []):
            if ir.get("template") == "date_bound":
                f = (ir.get("params") or {}).get("field")
                if f:
                    bounded.add(f)

    entries, seen = [], set()
    for entry in (synth_outputs or []):
        clause_txt = ((entry.get("clause") or {}).get("text") or "")
        for ir in (entry.get("candidates") or []):
            if ir.get("template") != "date_bound":
                continue
            p = ir.get("params") or {}
            d = p.get("date")
            if not d:
                continue
            just = ir.get("justification") or {}
            blob = " ".join(str(x) for x in (
                clause_txt, ir.get("rule_name"), ir.get("rule_description"),
                just.get("interpreted_requirement"), just.get("contract_text"))).lower()
            if "program" not in blob:
                continue
            if any(w in blob for w in ("effective", "supersed", "incept", "commenc")):
                target = prog_eff_col
            elif any(w in blob for w in ("expir", "expiry", "terminat")):
                target = prog_exp_col
            else:
                target = None
            if not target or target == p.get("field") or target in bounded \
                    or target in seen:
                continue
            seen.add(target)
            entries.append({
                "clause": {"clause_id": (entry.get("clause") or {}).get("clause_id"),
                           "text": f"[Derived rule] {target} must equal the program "
                                   f"date stated in the contract ({d})",
                           "page_number": (entry.get("clause") or {}).get("page_number")},
                "engine": "ir",
                "candidates": [{
                    "template": "date_bound",
                    "params": {"field": target, "op": "=", "date": d},
                    "rule_name": f"{target} must equal {d}",
                    "rule_description": (
                        f"{target} must equal the program date stated in the "
                        f"contract ({d})."),
                    "severity": ir.get("severity", "warning"),
                    "error_message": (f"{target} does not equal the contract's "
                                      f"program date of {d}."),
                    "confidence": 1.0,
                }],
            })
    return entries


def _as_variation_list(vv):
    """Coerce `variation_values` to a flat list.

    The prompt asks for a list of spellings, but the model sometimes returns the
    grouped shape instead — {"<value>": ["<spelling>", ...]} — and one of those
    reaching .append() raised AttributeError out of merge_sibling_enum_rules,
    which crashed the WHOLE upload rather than degrading one rule. Accept both.
    """
    if vv is None:
        return []
    if isinstance(vv, list):
        return vv
    if isinstance(vv, dict):
        out = []
        for val in vv.values():
            out.extend(val if isinstance(val, list) else [val])
        return out
    return [vv]


def merge_sibling_enum_rules(synth_outputs) -> int:
    """Collapse contradictory unscoped single-value enums on one field into ONE
    union rule.

    Shape it repairs: sibling key-value tables each state their own cohort's
    identity, and the mapper emits, per table, an UNSCOPED value_in_set on the
    same column allowing ONLY that table's name. Rules are conjunctive — with
    disjoint allowed sets every row violates at least one of them, so the set
    as generated flags the entire book. The only reading consistent with all
    the source clauses is the UNION of the values ("must be one of the named
    cohorts").

    Deliberately narrow — ALL of these must hold before anything is touched:
      • template value_in_set, on the SAME field;
      • every rule in the group is UNSCOPED (a scoped rule is a different,
        self-consistent statement — left alone);
      • the group spans >= 2 DISTINCT clauses (the sibling-table signature —
        two lists from ONE clause are a model error this repair must not paper
        over);
      • allowed sets are pairwise DISJOINT (overlapping lists may be a broad
        rule plus a legitimate refinement — left alone).
    The first rule of the group survives, its allowed/variation_values become
    the union (order-stable, deduped); the rest are removed. Returns rules
    merged away (0 = untouched)."""
    def _n(v):
        return " ".join(str(v or "").split()).lower()

    groups: dict[str, list] = {}
    for entry in (synth_outputs or []):
        cid = (entry.get("clause") or {}).get("clause_id")
        for ir in (entry.get("candidates") or []):
            p = ir.get("params") or {}
            if (ir.get("template") == "value_in_set" and not p.get("scope")
                    and isinstance(p.get("allowed"), list) and p["allowed"]
                    and isinstance(p.get("field"), str)):
                groups.setdefault(p["field"], []).append((entry, cid, ir))

    merged = 0
    for field, rules in groups.items():
        if len(rules) < 2 or len({cid for _, cid, _ in rules}) < 2:
            continue
        sets = [{_n(v) for v in ir["params"]["allowed"]} for _, _, ir in rules]
        if any(a & b for i, a in enumerate(sets) for b in sets[i + 1:]):
            continue    # overlapping lists — not the contradictory-sibling shape
        keeper = rules[0][2]
        kp = keeper["params"]
        seen = {_n(v) for v in kp["allowed"]}
        for entry, _, ir in rules[1:]:
            for v in ir["params"]["allowed"]:
                if _n(v) not in seen:
                    seen.add(_n(v))
                    kp["allowed"].append(v)
            for v in _as_variation_list(ir["params"].get("variation_values")):
                vv = _as_variation_list(kp.get("variation_values"))
                kp["variation_values"] = vv
                if _n(v) not in {_n(x) for x in vv}:
                    vv.append(v)
            entry["candidates"].remove(ir)
            merged += 1
        if merged:
            keeper["rule_description"] = (
                f"{field} must be one of the values the contract names: "
                + ", ".join(str(v) for v in kp["allowed"]) + ".")
    # drop entries left with no candidates
    if merged:
        synth_outputs[:] = [e for e in synth_outputs if e.get("candidates")]
    return merged


def fix_backdating_period_fields(synth_outputs, template_fields):
    """#8 — Anchor a "backdating of coverage" period_duration rule to the meaningful
    date pair: POLICY INCEPTION → TRANSACTION (processing) DATE. Backdating means the
    coverage inception is set earlier than the date the policy was actually
    transacted, so the span to measure is inception → transaction date — NOT the
    transaction's own EFFECTIVE date (which usually equals inception, making the
    check a no-op). The mapper (Call 3) picks these inconsistently, so we
    canonicalise them deterministically.

    Generic: the two columns are matched by ROLE tokens — an inception/coverage-start
    column, and a transaction date column that is the PROCESSING date (has
    'transaction' + 'date' but not 'effective'/'expiration') — never hardcoded names.
    Only period_duration rules whose clause/name is about *backdating* are touched
    (a policy-period or any other duration rule is left alone). No-op when the
    template lacks a clear inception or transaction-date column. Mutates the IR in
    place; returns the count changed."""
    from contract_upload_services.output_schema import is_processing_date_column

    names = [f.get("name") for f in (template_fields or []) if f.get("name")]

    def _find(pred):
        for n in names:
            if pred(n.lower()):
                return n
        return None

    inception = _find(lambda ln: "inception" in ln) or _find(
        lambda ln: "policy" in ln and "effective" in ln and "transaction" not in ln)
    # The transaction PROCESSING date (when the policy was booked/recorded), not its
    # effective or expiration date. Same single definition the generic library uses
    # to keep that column OUT of an in-period date bound — see
    # output_schema.is_processing_date_column.
    txn_date = _find(is_processing_date_column)
    if not inception or not txn_date:
        return 0

    fixed = 0
    for entry in (synth_outputs or []):
        clause_txt = ((entry.get("clause") or {}).get("text") or "")
        for ir in (entry.get("candidates") or []):
            if ir.get("template") != "period_duration":
                continue
            blob = (clause_txt + " " + str(ir.get("rule_name", "")) + " "
                    + str(ir.get("rule_description", ""))).lower()
            if "backdat" not in blob:
                continue
            p = ir.get("params") or {}
            changed = False

            # (a) Anchor to the meaningful span: policy inception -> transaction
            # (processing) date.
            if p.get("start_field") != inception or p.get("end_field") != txn_date:
                p["start_field"] = inception
                p["end_field"] = txn_date
                changed = True

            # (b) Canonicalise the bound to an UPPER limit. Backdating is measured
            # as transaction-date MINUS inception (>= 0 when actually backdated), and
            # every backdating clause caps that span ("backdating of more than N days
            # requires referral", "up to N days allowed") — so the compliant window
            # is 0..N and the violation is span > N, i.e. a single `max = N`. The
            # mapper often emits this instead as a `min` (or a >=/< op+value) whose
            # meaning is inverted: the compiled check then flags every COMPLIANT
            # short / zero / negative span and MISSES the real over-long backdates.
            # Read N from whichever bound the mapper produced (never hardcoded) and
            # collapse it to max=N so the direction of the check is unambiguous.
            threshold = next((p[k] for k in ("max", "min", "value")
                              if p.get(k) is not None), None)
            if threshold is not None:
                before = (p.get("max"), p.get("min"), p.get("op"), p.get("value"))
                for k in ("min", "value", "op"):
                    p.pop(k, None)
                p["max"] = threshold
                if (p.get("max"), p.get("min"), p.get("op"), p.get("value")) != before:
                    changed = True

            if changed:
                ir["params"] = p
                fixed += 1
    return fixed


def fix_fixed_rate_schedule_bounds(synth_outputs, template_fields):
    """A contractual COMMISSION-family RATE stated as a bare percentage schedule —
    "Commissions Schedule: 25.05%", "Ceding Commission: 30%" — is a FIXED value the
    reported rate must EQUAL, not a ceiling or a floor. The mapper (Call 3) is
    non-deterministic: some runs it emits range_check(min==max) (correct — an exact
    value), some runs it reads the headline % as a max_limit / min_limit because the
    sampled rates happen to sit below / above it ("reported rates sit below it"), so
    the rule then flags NOTHING and a genuine rate discrepancy (a BDX rate ≠ the
    contract's scheduled rate) goes unreported.

    Deterministically collapse such a mis-typed single-bound rule to
    range_check(min==max==value) — the compiler renders that as "must equal X"
    (same shape the mapper produces on its correct runs, e.g. contract 612's
    Commission Rate == 23.5). Generic + guarded, NO hardcoded values / column
    names: fires ONLY when
      (a) the rule is max_limit or min_limit with a single numeric bound;
      (b) the clause / rule name / field marks a COMMISSION-family rate
          (commission | ceding | brokerage | override) with a schedule / rate / %
          signal; and
      (c) the clause carries NO explicit limit wording (exceed, up to, maximum,
          at least, minimum, not to exceed, no more/less than, cap, floor,
          ceiling, greater/less than, below, above) — a REAL bound is left
          untouched.
    Mutates the IR in place; returns the count changed."""
    _RATE = re.compile(r"\b(commission|ceding|brokerage|override)\b", re.I)
    _RATEISH = re.compile(r"\bschedule\b|\brate\b|%|percent", re.I)
    _LIMIT = re.compile(
        r"exceed|up\s+to|maximum|minimum|at\s+least|at\s+most|not\s+to\s+exceed|"
        r"no\s+more\s+than|no\s+less\s+than|greater\s+than|less\s+than|\bcap\b|"
        r"capped|\bfloor\b|ceiling|\bbelow\b|\babove\b", re.I)
    fixed = 0
    for entry in (synth_outputs or []):
        clause_txt = ((entry.get("clause") or {}).get("text") or "")
        for ir in (entry.get("candidates") or []):
            if ir.get("template") not in ("max_limit", "min_limit"):
                continue
            p = ir.get("params") or {}
            field = p.get("field")
            if not field:
                continue
            bound = next((p[k] for k in ("max", "min", "value")
                          if p.get(k) is not None), None)
            if bound is None:
                continue
            blob = (clause_txt + " " + str(ir.get("rule_name", "")) + " "
                    + str(ir.get("rule_description", "")) + " " + str(field))
            if not (_RATE.search(blob) and _RATEISH.search(blob)):
                continue
            if _LIMIT.search(clause_txt):
                continue
            newp = {"field": field, "min": bound, "max": bound}
            if p.get("scope"):
                newp["scope"] = p["scope"]
            ir["template"] = "range_check"
            ir["params"] = newp
            # Refresh the human-readable text to the equals semantics (generic; the
            # compiled SQL's reason is regenerated from the IR downstream).
            ir["rule_name"] = f"{field} equals {bound}"
            ir["rule_description"] = f"{field} must equal the scheduled rate {bound}."
            ir["error_message"] = (
                f"{field} does not equal the scheduled rate {bound}.")
            fixed += 1
    return fixed


def fix_hardcoded_rate_formulas(synth_outputs, template_fields):
    """#9 — Repair a formula the mapper built from a contract's HEADLINE rate.
    When a contract states a commission/fee at a fixed rate ("Commissions
    Schedule: 25% payable to Administrator") the mapper often turns it into an
    EXACT `<amount> = <base> * <constant>` check (a cross_field_compare with
    op='=', operator='*', a hardcoded `factor`, e.g. Palms Commission Amount $ =
    Palms Gross Written Premium $ * 0.25). But when the BDX carries a PER-ROW rate
    column for that same amount (e.g. "Palms Commission %" = 22.5 / 23.5), the
    headline constant is wrong for every policy whose real rate differs — flagging
    the ENTIRE book (observed: 228/228 false positives). The very existence of a
    per-row rate column proves the rate varies per policy, so the amount must be
    checked against THAT column, not a single contract-headline number.

    Rewrites such a rule in place into a `cross_field_math` `amount = base × rate`
    against the matching per-row rate column. Generic — the base and rate columns
    are matched to the amount by shared ENTITY tokens (Palms vs 100% vs another
    party) AND the same financial CONCEPT (a Commission Amount pairs only with a
    Commission %, never a Fee %); no hardcoded column names, no MGA literal. Only
    fires when such a rate column actually exists; otherwise the rule (a genuine
    fixed-rate constraint with no per-row rate to check against) is left untouched.
    Mutates the IR in place; returns the count changed."""
    names = [f.get("name") for f in (template_fields or []) if f.get("name")]
    by_name = {f.get("name"): f for f in (template_fields or []) if f.get("name")}

    # Per-row RATE columns available in the template: a "%"/"rate" column that
    # measures a known financial concept (commission/fee/…).
    rate_cols = [n for n in names
                 if ("%" in n or "rate" in n.lower()) and _rate_concept(n)]
    if not rate_cols:
        return 0

    def _numeric_samples(field):
        out = []
        for s in (by_name.get(field, {}).get("samples") or []):
            try:
                out.append(float(str(s).replace(",", "").replace("$", "")
                                 .replace("%", "").strip()))
            except (TypeError, ValueError):
                pass
        return out

    fixed = 0
    for entry in (synth_outputs or []):
        for ir in (entry.get("candidates") or []):
            p = ir.get("params") or {}

            # ---- Variant B: an EXACT rate constraint directly on the rate column
            # (e.g. a range_check min==max, "Palms Commission % must equal 25")
            # whose OWN sample data contradicts it (every sampled rate is below
            # the constant) — strong evidence the contract's headline rate is a
            # CEILING, not a per-row exact value (the real rates are 22.5/23.5).
            # Convert exact-equality to a MAXIMUM so compliant lower rates pass
            # while an over-rate is still caught. Gated on samples, so it never
            # fires when the data really does sit at the stated rate (e.g. a true
            # "Percentage of Total Risk = 100%", which also isn't a rate concept).
            if ir.get("template") == "range_check":
                rf = p.get("field")
                mn, mx = p.get("min"), p.get("max")
                if rf and _rate_concept(rf) and mn is not None and mx is not None:
                    try:
                        mnf, mxf = float(mn), float(mx)
                    except (TypeError, ValueError):
                        mnf = mxf = None
                    smp = _numeric_samples(rf)
                    if mnf is not None and mnf == mxf and smp and all(v < mxf for v in smp):
                        ir["template"] = "max_limit"
                        ir["params"] = {"field": rf, "max": mxf}
                        if p.get("scope"):
                            ir["params"]["scope"] = p["scope"]
                        ir["rule_name"] = f"{rf} at most {mxf}"
                        ir["rule_description"] = (
                            f"{rf} must not exceed {mxf} (the contract's headline "
                            f"rate read as a maximum; reported rates sit below it).")
                        ir["error_message"] = f"{rf} exceeds the maximum of {mxf}."
                        fixed += 1
                continue

            # ---- Variant A: an EXACT `amount = other * constant` check.
            if ir.get("template") != "cross_field_compare":
                continue
            if p.get("op") != "=" or p.get("operator") != "*":
                continue
            if p.get("factor") in (None, "") or p.get("right_field"):
                continue
            amount, base = p.get("field"), p.get("other_field")
            if not amount or not base:
                continue
            concept = _rate_concept(amount)
            if not concept:
                continue  # the amount must itself name a rate-bearing concept
            amt_ent = _fin_entity_toks(amount)
            # Candidate rate columns: same concept, and (when the amount carries
            # an entity like "Palms") a shared entity token — so a "Palms
            # Commission Amount" binds to "Palms Commission %", not another
            # party's rate.
            cands = [r for r in rate_cols if _rate_concept(r) == concept
                     and (not amt_ent or (amt_ent & _fin_entity_toks(r)))]
            rate_col = _best_fin_match(amt_ent, cands)
            if not rate_col:
                continue
            # Re-pick the BASE too (the mapper may also have grabbed the wrong
            # entity's premium, e.g. "100% Gross" for a Palms figure). Prefer a
            # premium column sharing the amount's entity; fall back to the
            # mapper's original other_field.
            prem_cols = [n for n in names if "premium" in n.lower()
                         and not any(x in n.lower()
                                     for x in ("terrorism", "cyber", "net"))]
            same_ent = [b for b in prem_cols if amt_ent & _fin_entity_toks(b)]
            new_base = _best_fin_match(amt_ent, same_ent or prem_cols) or base
            ir["template"] = "cross_field_math"
            ir["params"] = {
                "result_field": amount,
                "left_field": new_base,
                "operator": "*",
                "right_field": rate_col,
                "right_is_percent": _looks_percent(by_name.get(rate_col, {})),
                "tolerance_pct": DEFAULT_CROSS_FIELD_TOLERANCE_PCT,
            }
            ir["rule_name"] = f"{amount} equals {new_base} × {rate_col}"
            ir["rule_description"] = (
                f"{amount} must equal {new_base} multiplied by the reported "
                f"{rate_col} (the per-row rate, not a fixed headline rate).")
            ir["error_message"] = f"{amount} does not match {new_base} × {rate_col}."
            fixed += 1
    return fixed


# Generic structural markers for a clause that DEFERS its content to a NAMED
# external document (e.g. "as more fully defined in the XYZ Underwriting
# Guidelines dated 3-15-2025", "per the ABC Manual on file with the Company").
# Deliberately NOT a fixed document-name vocabulary — it fires on the STRUCTURE
# (a lead-in phrase + a document-type noun), never a specific MGA's document
# name, and excludes self-references ("this Agreement"/"this Schedule").
_EXTERNAL_DOC_NOUN_WORDS = ("guidelines", "guideline", "authorities", "agreement",
                            "criteria", "schedule", "facility", "wording",
                            "manual", "guide")
# Longest-first so the alternation can't stop short inside a longer noun.
_EXTERNAL_DOC_NOUN = "(?:%s)" % "|".join(
    sorted(_EXTERNAL_DOC_NOUN_WORDS, key=len, reverse=True))
_EXTERNAL_DOC_LEADIN = (r"(?:as\s+more\s+fully\s+defined\s+in|as\s+defined\s+by|"
                        r"as\s+set\s+forth\s+in|in\s+accordance\s+with|"
                        r"pursuant\s+to|subject\s+to|per)")
# Matched in two independently-anchored steps rather than one combined regex:
# a bare "per" is extremely common in ordinary insurance phrasing ("$X per
# policy", "per occurrence") and NOT a document reference there, so every
# LEADIN occurrence gets its own attempt at capturing a doc-name starting right
# after it (DOC_CAPTURE.match(text, pos)) — using finditer on the COMBINED
# pattern instead would let an early false lead-in (e.g. "per policy") greedily
# consume the text up to a later, real document name, hiding it.
_EXTERNAL_DOC_LEADIN_ONLY = re.compile(_EXTERNAL_DOC_LEADIN, re.IGNORECASE)
# The same lead-ins, but as whole words. LEADIN_ONLY is only ever used to anchor
# a capture that must start immediately after it, so a lead-in matched mid-word
# there simply fails to capture; searched loose over a sentence it does not —
# "SUPER Specialty Insurance" contains "per", which is enough to make a preamble
# look like a deferral.
_EXTERNAL_DOC_LEADIN_WORD = re.compile(rf"\b{_EXTERNAL_DOC_LEADIN}\b", re.IGNORECASE)
# The name ENDS at the document-type noun. An earlier form ran to the next
# punctuation, which turned an ordinary sentence that happened to contain one of
# these nouns into a "document name" ("Program Fee Schedule as soon as possible
# but in any event within 10 working days of the end of each month"). A title is
# a short run of words ending in its type noun, so that is what is matched: up
# to eight words before the noun (long enough for the longest real title seen —
# "<MGA> Transportation Facility Underwriting Guidelines" — and far short of the
# sentence above), greedily. Greedy matters because several of the nouns also
# occur mid-title ("Transportation **Facility** Underwriting Guidelines"), and
# the LAST one is the one that ends the name.
_EXTERNAL_DOC_CAPTURE = re.compile(
    rf"\s+((?:the\s+|a\s+|an\s+)?(?:[\w&'’()\-\.]+[ \t]+){{0,8}}{_EXTERNAL_DOC_NOUN})\b",
    re.IGNORECASE,
)
_EXTERNAL_DOC_DATE = re.compile(
    r"(?:dated|effective(?:\s+as\s+of)?)\s+"
    r"([A-Za-z]*\.?\s*\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})",
    re.IGNORECASE,
)
_EXTERNAL_DOC_TRAIL_NOISE = re.compile(
    r"\s+(?:dated\s+.+|effective\s+.+|on\s+file.+)$", re.IGNORECASE)
_EXTERNAL_DOC_LEAD_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
# "…pursuant to Article 9 of the Program Administration Agreement" points at a
# PROVISION of the governing contract — it is not deferring rule content to an
# external file, and asking the user to upload it is noise. Naming a specific
# article/section is the tell, so a citation shaped that way is rejected outright.
# Structural (a divider noun + number + "of the"), the same way LEADIN and NOUN
# are — it carries no document-name vocabulary.
_EXTERNAL_DOC_XREF_PREFIX = re.compile(
    r"^(?:article|section|clause|exhibit|appendix|annex|schedule|paragraph)\s+"
    r"[\dIVXivx]+[a-z]?\s+(?:of|to)\s+(?:the\s+|a\s+|an\s+)?", re.IGNORECASE)


def detect_deferred_external_references(clauses):
    """Deterministic backstop for Pipeline 1's LLM-extracted `external_references`.
    Pipeline 1 asks the LLM (one big JSON call) to also list every document a
    contract clause defers to (e.g. "Underwriting Guidelines dated 8-1-2025") so
    the upload flow can HALT and prompt the user to supply it. That extraction is
    probabilistic and occasionally misses a clause whose deferral phrasing the
    single-call prompt didn't flag — with no other backstop, the clause silently
    proceeds unresolved (it routes to control_register at Stage 3 with a clear
    "defers to an external document" reason, but the user is NEVER given the
    chance to supply that document, because the halt gate — which runs right
    after Pipeline 1, before Stage 3 — never saw it).

    This scans every clause's text for the STRUCTURAL pattern of a deferral — a
    lead-in phrase ("per", "as defined by", "pursuant to", …) followed by a
    Title-Case document name ending in a document-type noun (Guidelines, Manual,
    Agreement, …), excluding self-references to the current contract ("this
    Agreement"). Purely structural: no document-name vocabulary, no MGA/carrier
    literal. Returns a list of {document_name, version_or_date, source_texts,
    pages, confidence} entries — the same shape the LLM emits — deduped by
    normalized document name (case-insensitive)."""
    found = {}
    for c in clauses or []:
        text = c.get("text") or ""
        for lm in _EXTERNAL_DOC_LEADIN_ONLY.finditer(text):
            dm = _EXTERNAL_DOC_CAPTURE.match(text, lm.end())
            if not dm:
                continue
            raw = dm.group(1).strip()
            name = _EXTERNAL_DOC_LEAD_ARTICLE.sub(
                "", _EXTERNAL_DOC_TRAIL_NOISE.sub("", raw).strip()).strip()
            if not name or _EXTERNAL_DOC_XREF_PREFIX.match(name):
                continue
            # The captured phrase, in its ORIGINAL casing, must start with a
            # capital letter — a proper-noun signal that distinguishes a real
            # document name ("Administrator Underwriting Guidelines") from
            # ordinary lowercase phrasing a false lead-in like "per policy"
            # would otherwise capture.
            if not name[0].isupper() or name.split()[0].lower() in ("this", "current"):
                continue
            # A bare type noun with no qualifier ("pursuant to the Agreement",
            # "per the Schedule") is this contract referring to ITSELF. A real
            # external document is always named — "X Underwriting Guidelines",
            # "Service Level Agreement" — so one word is never one.
            if len(name.split()) < 2:
                continue
            d = _EXTERNAL_DOC_DATE.search(text, lm.end())
            key = name.lower()
            entry = found.setdefault(key, {
                "document_name": name,
                "version_or_date": d.group(1) if d else None,
                "source_texts": [],
                "pages": [],
                "confidence": 0.85,
            })
            if text not in entry["source_texts"]:
                entry["source_texts"].append(text)
            page = c.get("page_number") or c.get("page")
            if page is not None and page not in entry["pages"]:
                entry["pages"].append(page)
    return filter_external_references(list(found.values()))


def filter_external_references(entries):
    """The single gate every external-reference candidate passes through, from
    the LLM's own list and from the deterministic backstop alike.

    Both sources over-report, in ways a user immediately reads as wrong: the
    contract's PARENT agreement (named in the preamble that says what this
    schedule is attached to), a provision cross-reference ("pursuant to Article
    9 of …"), an ordinary noun that happens to be a document type ("placed on
    Quantum schedule"), and the same guideline listed twice because two clauses
    cite different dates for it. Each one asks somebody to go and find a
    document that does not exist.

    A reference document is one a clause DEFERS ITS CONTENT to. That is the
    definition applied here, structurally and with no document-name vocabulary:

      * duplicates collapse first, so a merged entry is judged on everything
        that was ever said about it;
      * a name is at least two words — a bare "the Agreement" is this contract;
      * its document-type noun is capitalised, i.e. part of a TITLE. "Quantum
        schedule" is a lowercase common noun, "Underwriting Guidelines" is not;
      * it is not a provision cross-reference;
      * and at least one sentence citing it actually defers — it carries one of
        the lead-in phrases. A preamble reciting the parent agreement does not.

    Fails OPEN where there is nothing to judge: an entry with no source text at
    all is kept, since silence is not evidence against it.
    """
    kept = []
    for e in _collapse_reference_names(entries):
        name = (e.get("document_name") or "").strip()
        words = name.split()
        if len(words) < 2 or _EXTERNAL_DOC_XREF_PREFIX.match(name):
            continue
        if not any(w[:1].isupper() and w.strip("’'\"()-.,").lower() in _EXTERNAL_DOC_NOUN_WORDS
                   for w in words):
            continue
        texts = [t for t in (e.get("source_texts") or []) if isinstance(t, str)]
        if texts and not any(_defers_to(t, name) for t in texts):
            continue
        kept.append(e)
    return kept


# What sits between a lead-in and the document name in a real deferral is an
# article at most — "as defined by ⟨the⟩ X Guidelines". "…pursuant to ⟨Article 9
# of the⟩ Program Administration Agreement" is a pointer to a PROVISION, and the
# giveaway is right there in the gap.
_EXTERNAL_DOC_XREF_GAP = re.compile(
    r"^\s*(?:of\s+|to\s+)?"
    r"(?:article|section|clause|exhibit|appendix|annex|schedule|paragraph)\s+"
    r"[\dIVXivx]+[a-z]?\s+(?:of|to)\s+(?:the\s+|a\s+|an\s+)?$", re.IGNORECASE)
# A lead-in introduces the document it defers to; it does not sit half a sentence
# away from it. Six words of slack covers "as more fully defined in the".
_MAX_LEADIN_GAP_WORDS = 6


def drop_covered_references(entries, reference_documents):
    """Drop the references a SUPPLIED document actually covers.

    Whether a document was provided cannot rest on what the file was called.
    "Zyvarqen Transportation Facility Underwriting Guidelines" is rarely saved
    under that name — it arrives as "Fac Guide v3 FINAL.docx" — and a filename
    that doesn't look like the cited title is exactly when the model is most
    likely to keep listing the document as missing. So the test is the document's
    CONTENT: a document states its own title, so if the cited name appears in the
    text of something the user uploaded, that is the document.

    Falls back to the title's last three words ("Facility Underwriting
    Guidelines"), which survives the contract citing it with an owner prefix the
    document itself doesn't print on the cover. Nothing matches → nothing is
    dropped, which is the behaviour this replaces.
    """
    if not entries or not reference_documents:
        return entries

    def norm(t):
        return re.sub(r"\W+", " ", (t or "").lower()).strip()

    texts = [norm(rd.get("text")) for rd in reference_documents if isinstance(rd, dict)]
    texts = [t for t in texts if t]
    if not texts:
        return entries

    kept = []
    for e in entries:
        name = norm(e.get("document_name"))
        words = name.split()
        tail = " ".join(words[-3:]) if len(words) > 3 else name
        if name and any(name in t or tail in t for t in texts):
            print(f"[Pipeline 1] reference '{e.get('document_name')}' is covered by "
                  f"an uploaded document (matched on content, not filename)")
            continue
        kept.append(e)
    return kept


def _defers_to(text, name):
    """True when `text` hands its content OVER to `name` — i.e. the document is
    INTRODUCED by a lead-in phrase ("as defined by the X Guidelines").

    Three ways a sentence can mention a document without deferring to it, all of
    which were seen on one real contract and all rejected here:

      * the preamble reciting what this contract IS ("Program Administration
        Agreement by and between …") — nothing precedes the name;
      * a pointer to a PROVISION of it ("…pursuant to Article 9 of the Program
        Administration Agreement") — there IS a lead-in, but what it introduces
        is an article, not a document to go and find;
      * a sentence that never names this document at all, where a lead-in
        belonging to something else would otherwise vouch for it.
    """
    low, target = text.lower(), name.lower()
    at = low.find(target)
    if at < 0:
        # Not spelled out in full — fall back to the distinctive tail (the type
        # noun, e.g. "Guidelines"), which survives an abbreviated citation. If
        # even that is absent, this sentence is not about this document.
        tail = target.split()[-1]
        at = low.find(tail)
        if at < 0:
            return False
    for m in _EXTERNAL_DOC_LEADIN_WORD.finditer(text, 0, at):
        gap = text[m.end():at]
        if _EXTERNAL_DOC_XREF_GAP.match(gap):
            continue
        if len(gap.split()) <= _MAX_LEADIN_GAP_WORDS:
            return True
    return False


def _collapse_reference_names(entries):
    """Merge entries that name the SAME document with different wording.

    One document is rarely cited identically twice — the same guideline shows up
    as "Allianz Insurisk Transportation Facility Underwriting Guidelines" in one
    clause and "Allianz Insurisk Transportation Underwriting Guidelines" in the
    next. Listing both asks the user to supply two documents that don't exist.

    An entry is folded into another when its words appear IN ORDER within the
    other's — a shortened citation of the same title — and the fuller name is
    kept. Requires at least three words so a bare "the Agreement" can't swallow
    an unrelated title. Purely positional: no document-name vocabulary.
    """
    words = {id(e): [w for w in re.split(r"\W+", e["document_name"].lower()) if w]
             for e in entries}

    def subsequence(short, long_):
        it = iter(long_)
        return all(w in it for w in short)

    kept = []
    for e in sorted(entries, key=lambda x: -len(words[id(x)])):
        me = words[id(e)]
        host = next((k for k in kept
                     if me == words[id(k)]
                     or (len(me) >= 3 and subsequence(me, words[id(k)]))), None)
        if host is None:
            kept.append(e)
            continue
        for t in e["source_texts"]:
            if t not in host["source_texts"]:
                host["source_texts"].append(t)
        for pg in e["pages"]:
            if pg not in host["pages"]:
                host["pages"].append(pg)
        host["version_or_date"] = host["version_or_date"] or e["version_or_date"]
    return kept


# A closed-set marker embedded in a column HEADER — a parenthesized, slash-separated
# list of the allowed values, e.g. "Facultative Re(Y/N)", "APD Auditable (Y/N)",
# "Reporter Y/N", "Policy Type (Primary/Excess)". The header ITSELF states the
# closed set (2+ alternatives), so no hardcoded vocabulary — the boolean spellings
# (Y/N, Yes/No, T/F, True/False) are just the most common case, handled specially
# below to also accept the numeric 1/0 encoding; every OTHER slash-list (Primary/
# Excess, Direct/Reinsurance, …) uses its own header words as the allowed set
# verbatim. Matched as a standalone token (non-word boundary on each side, or
# inside parens) so it never fires mid-word or on an unrelated "/" (e.g.
# "New/Renewal" has no enclosing parens and 2 words, but so does a real header
# like "(Primary/Excess)" — the parenthesized form is required to avoid false
# positives on bare slash-pairs that are just two related-but-independent
# columns, not a declared closed set for ONE column).
_BOOL_HEADER_MARKER = re.compile(
    r"(?<![a-z0-9])(y\s*/\s*n|yes\s*/\s*no|t\s*/\s*f|true\s*/\s*false)(?![a-z0-9])",
    re.IGNORECASE,
)
_PAREN_ENUM_MARKER = re.compile(r"\(\s*([A-Za-z][A-Za-z \-']*(?:\s*/\s*[A-Za-z][A-Za-z \-']*)+)\s*\)")
# A BARE (unparenthesised) slash-list that is the WHOLE header — "New/Renewal",
# "Primary/Excess", "Direct/Reinsurance". Structurally this is indistinguishable
# from a slash inside a compound COLUMN NAME ("APD per Occ / Terminal Limit",
# "Carrier Net/Net Premium"), so the header alone can NEVER decide: the column's
# own DATA does (see _samples_support_options). Anchored to the full header so a
# stray slash mid-name doesn't match.
_BARE_ENUM_MARKER = re.compile(
    r"^[A-Za-z][A-Za-z \-']*(?:\s*/\s*[A-Za-z][A-Za-z \-']*)+$")


def _enum_norm(v) -> str:
    """Normalise a header option / cell value for comparison (case + non-alnum)."""
    return re.sub(r"[^a-z0-9]", "", str(v).lower())


# Shortest option/value that may be compared by PREFIX rather than in full (see
# _samples_support_options). Mirrors the enum compiler's own `starts_with`
# matching, and stops a 1-2 character alternative ("A/B", "N") from "supporting"
# any value that happens to begin with that letter.
_ENUM_PREFIX_MINLEN = 3


def _samples_support_options(samples, options) -> bool:
    """True when the column's OWN sampled values are evidence that a slash header
    declares a closed set — i.e. at least one sampled value IS one of the header's
    alternatives.

    This is what separates a real enum header ("New/Renewal" → cells 'New',
    'Renewal') from a compound column NAME that merely contains a slash ("APD per
    Occ / Terminal Limit" → cells '1000000', '2500000'; "Carrier Net/Net Premium"
    → empty) and from a parenthesised CALCULATION note ("Gross Written Premium
    (Less FAC / Mine )" → cells '0', '20000', '-18500'). Purely data-driven — no
    column list, no vocabulary. A column with no sampled values yields False (no
    evidence), keeping this conservative. Only ONE match is required, so a column
    whose data also contains a genuinely BAD value still gets its rule (and the
    bad value is then flagged).

    Matching is PREFIX-tolerant in either direction (guarded by
    _ENUM_PREFIX_MINLEN) because real BDX columns spell the header's alternative
    as a COMPOUND value — a "Policy Type (Primary/Excess)" column holds 'Primary
    Casualty' / 'Excess Property', a "New/Renewal" column holds 'NEW_BUSINESS' —
    and the enum compiler already accepts those spellings, so the gate must not
    be stricter than the rule it is gating."""
    opts = {_enum_norm(o) for o in (options or []) if _enum_norm(o)}
    if not opts:
        return False
    for s in (samples or []):
        sv = _enum_norm(s)
        if not sv:
            continue
        if sv in opts:
            return True
        for o in opts:
            if len(o) >= _ENUM_PREFIX_MINLEN and sv.startswith(o):
                return True
            if len(sv) >= _ENUM_PREFIX_MINLEN and o.startswith(sv):
                return True
    return False


# The parenthesised part(s) of a column header, e.g. "Gross Written Premium
# (Less FAC / Mine )" → base "Gross Written Premium". Used to spot a header that
# QUALIFIES a measure the template already carries as a column of its own.
_HEADER_PARENS = re.compile(r"\([^()]*\)")


def _header_base_key(name: str) -> str:
    """Normalised header with every parenthesised group removed — the 'measure'
    the header is about ("Gross Written Premium (Less FAC / Mine )" and "Gross
    Written Premium" both → 'grosswrittenpremium')."""
    return _enum_norm(_HEADER_PARENS.sub(" ", str(name or "")))


def _plain_measure_sibling(col: str, names):
    """The plain column this parenthesised header is a VARIANT of, when the same
    template carries one — "Gross Written Premium (Less FAC / TRIA / Mine )" →
    "Gross Written Premium". None when there is no such twin.

    That pairing is what a calculation note looks like structurally: the same
    quantity, restated with a qualifier saying how THIS copy of it was computed.
    A genuine closed-set header ("Policy Type (Primary/Excess)") has no such twin
    — the parenthesis is the column's only definition, not a modifier of a
    sibling. Only an UNPARENTHESISED sibling counts, so two paren-variants of one
    base ("… (Primary/Excess)" beside "… (Claims Made/Occurrence)") never
    disqualify each other. Structural — no column list and no vocabulary."""
    base = _header_base_key(col)
    if not base:
        return None
    for other in names:
        if not other or other == col:
            continue
        if _PAREN_ENUM_MARKER.search(other) or _BOOL_HEADER_MARKER.search(other):
            continue
        if _header_base_key(other) == base:
            return other
    return None


_BOOL_VOCAB = {
    frozenset({"y", "n"}): (["Yes", "No", "Y", "N", "1", "0"], "Yes/No"),
    frozenset({"yes", "no"}): (["Yes", "No", "Y", "N", "1", "0"], "Yes/No"),
    frozenset({"t", "f"}): (["True", "False", "T", "F", "1", "0"], "True/False"),
    frozenset({"true", "false"}): (["True", "False", "T", "F", "1", "0"], "True/False"),
}


def derive_header_enum_entries(synth_outputs, template_fields):
    """#7 — Derive a value_in_set rule for every column whose HEADER declares a
    closed set of values via a parenthesized slash-list — "… (Y/N)", "… (Yes/No)",
    "… (T/F)", "… (Primary/Excess)", "… (Direct/Reinsurance)", etc. The header
    itself states the allowed values, so the rule is generated deterministically
    from the template — no clause and no LLM.

    The boolean spelling pairs (y/n, yes/no, t/f, true/false) are special-cased to
    the FULL logical value universe of a boolean flag — long AND short word
    spellings PLUS the 1/0 numeric encoding (Yes, No, Y, N, 1, 0) — since a Y/N
    column may legitimately be filled in with any of those. Every OTHER slash-list
    uses its own header words verbatim as the allowed set (e.g. "Primary/Excess" ->
    ["Primary","Excess"]) — nothing invented beyond what the header itself declares.
    The enum compiler's canonical-token matching then accepts any case ("NO", "no",
    "excess", "EXCESS" all pass) and flags genuinely out-of-set values.

    A slash-list in a header is only a closed-set DECLARATION when the column's own
    data agrees. "Gross Written Premium (Less FAC / Mine )" reads identically to
    "Policy Type (Primary/Excess)" but is a money column net of two deductions — a
    calculation note, not a value set — so both the bare and the parenthesised path
    are gated on the column's sampled values (_samples_support_options), and an
    empty parenthesised column falls back to a structural check for the same measure
    existing as a plain column (_is_measure_variant). Only the Y/N-style boolean
    spellings need no gate: they are unambiguous.

    Generic: detection is a regex over the column name (no hardcoded column list or
    vocabulary). A column already covered by a mapped FORMAT enum rule (a
    non-referral value_in_set/value_not_in_set) is skipped so this never duplicates
    a contract-derived constraint. A REFERRAL rule (e.g. "Facultative = Yes → refer
    to Company") or any non-enum rule expresses a DIFFERENT constraint that only
    cares about a trigger value, so it does NOT suppress the header's data-quality
    check — both coexist (a 'Yes' row triggers the referral AND passes the format
    rule). Returns a list of synth_output entries (empty when there are no flags)."""
    names = [f.get("name") for f in (template_fields or []) if f.get("name")]

    # Per-column sampled values from the template (the BDX's own data). Used ONLY to
    # decide whether a slash header is a real closed-set declaration or just a
    # compound column name / a calculation note — the header text alone cannot tell
    # them apart. `samples_all` (every sample the template parser captured) is used
    # in preference to the 3-value prompt-sized `samples`: the one row that spells
    # the header's alternative may sit past the third, and missing it silently costs
    # a legitimate rule.
    samples_by_col = {}
    for f in (template_fields or []):
        n = f.get("name")
        if not n:
            continue
        for s in (f.get("samples_all") or f.get("samples") or []):
            if s is not None and str(s).strip():
                samples_by_col.setdefault(n, []).append(str(s).strip())

    # Columns already covered by a mapped FORMAT enum rule — don't add a duplicate.
    # Referral triggers and non-enum rules are orthogonal and never suppress the
    # header check (see docstring), so they are excluded from `governed`.
    governed = set()
    for entry in (synth_outputs or []):
        for ir in (entry.get("candidates") or []):
            if ir.get("template") not in ("value_in_set", "value_not_in_set"):
                continue
            if ir.get("is_referral"):
                continue
            fld = (ir.get("params") or {}).get("field")
            if isinstance(fld, str) and fld:
                governed.add(fld.strip().lower())

    # Parenthesised headers whose brackets turned out to describe the column
    # rather than enumerate it — reported so "why has this column no rule?" is
    # answerable from the run log instead of by re-deriving it by hand.
    not_a_value_set = []
    entries = []
    for col in names:
        if col.strip().lower() in governed:
            continue
        m = _BOOL_HEADER_MARKER.search(col)
        if m:
            marker = re.sub(r"\s+", "", m.group(1)).lower()
            allowed, label = (["True", "False", "T", "F", "1", "0"], "True/False") \
                if marker in ("t/f", "true/false") else \
                (["Yes", "No", "Y", "N", "1", "0"], "Yes/No")
        else:
            pm = _PAREN_ENUM_MARKER.search(col)
            if pm:
                options = [o.strip() for o in pm.group(1).split("/") if o.strip()]
                if len(options) < 2:
                    continue
                # PARENTHESISED slash-list. Brackets are NOT proof of a closed set:
                # they just as often describe how a column was CALCULATED — "Gross
                # Written Premium (Less FAC / Mine )" is premium NET of facultative
                # and mine-subsidence, a money column, not a column whose cells read
                # 'Less FAC' or 'Mine'. Trusting the brackets there turns every
                # populated row into a false exception. The header cannot tell the
                # two apart; only the data can, in this order:
                #   1. the column's own values back the header's alternatives → real
                #      closed set (this is the "Policy Type (Primary/Excess)" case,
                #      228/228 rows 'Primary'/'Excess');
                #   2. the column HAS values and none of them do → the brackets
                #      describe the column, they do not enumerate it;
                #   3. the column is empty, so it carries no evidence of its own —
                #      fall back to the STRUCTURE: when the same measure also exists
                #      as a plain column ("Gross Written Premium"), the brackets are
                #      qualifying that measure, so no rule. Otherwise the header is
                #      the only evidence there is and it stands, exactly as before.
                own_samples = samples_by_col.get(col)
                sibling = None if own_samples else _plain_measure_sibling(col, names)
                if own_samples and not _samples_support_options(own_samples, options):
                    not_a_value_set.append(
                        f"{col!r} — its data reads {own_samples[0]!r}, "
                        f"not {'/'.join(options)}")
                    continue
                if sibling:
                    not_a_value_set.append(
                        f"{col!r} — empty column qualifying {sibling!r}")
                    continue
            else:
                # BARE slash header ("New/Renewal", "Primary/Excess"). The header is
                # only a closed-set declaration if the column's OWN DATA says so —
                # otherwise the slash is just part of a compound name ("APD per Occ /
                # Terminal Limit"). Data decides; nothing is hardcoded.
                if not _BARE_ENUM_MARKER.match(col.strip()):
                    continue
                options = [o.strip() for o in col.strip().split("/") if o.strip()]
                if len(options) < 2:
                    continue
                if not _samples_support_options(samples_by_col.get(col), options):
                    continue
            if len(options) < 2:
                continue
            key = frozenset(o.lower() for o in options)
            vocab = _BOOL_VOCAB.get(key)
            if vocab:
                allowed, label = vocab
            else:
                allowed, label = options, "/".join(options)
        ir = {
            "template": "value_in_set",
            "params": {"field": col, "allowed": allowed},
            "rule_name": f"{col} must be {label}",
            "rule_description": (
                f"{col} must be a {label} value, per the closed-set marker in its "
                f"column header."),
            "severity": "warning",
            "error_message": f"{col} must be one of: {', '.join(allowed)}.",
            "confidence": 1.0,
        }
        entries.append({
            "clause": {"clause_id": None,
                       "text": f"[Derived rule] {col} must be a {label} value "
                               f"(per column-header closed-set marker)",
                       "page_number": None},
            "engine": "ir",
            "candidates": [ir],
        })
    if not_a_value_set:
        print(f"[Call 3] header-enum: no rule for {len(not_a_value_set)} "
              f"parenthesised header(s) — the brackets describe the column, they "
              f"do not enumerate it: " + "; ".join(dict.fromkeys(not_a_value_set)))
    return entries


def derive_referral_indicator_presence_rule(synth_outputs, template_fields):
    """#8 — The referral-INDICATOR column (a per-policy flag recording whether a
    policy was referred to the Company — e.g. "… Referral Indicator", "Referral
    Flag", "Referral Status") is a mandatory data-quality field: every policy row
    must carry a value, because a blank cell means the referral decision was never
    recorded. Emit ONE deterministic `required_field` (non-blank) rule on it.

    Generic: the column is located by ROLE TOKENS ("referral" + indicator/flag/
    status) over the template's own header names — no hardcoded column name and no
    hardcoded value set. This is deliberately a PRESENCE (not-blank) check only; the
    value/format universe (Yes/No/N-A) is the header-enum deriver's job when the
    header declares one, so nothing here bakes in an allowed-value list. Only the
    INDICATOR-style column qualifies — a bare "Referral Required" TRIGGER column is
    never force-populated (it may legitimately be blank when no referral applies, so
    a not-blank rule there would flag every row). Deduped against any required_field
    already targeting the column, so it never doubles up. Returns a list with at
    most one entry (empty when the template has no referral-indicator column)."""
    names = [f.get("name") for f in (template_fields or []) if f.get("name")]

    # Locate THE referral-indicator column (same first-pass detector as
    # rule_normalizer._find_referral_indicator): "referral" + indicator/flag/status.
    # No loose "any column containing referral" fallback — that could match an empty
    # trigger column and then flag every policy row.
    indicator = None
    for n in names:
        nl = n.lower()
        if "referral" in nl and ("indicator" in nl or "flag" in nl or "status" in nl):
            indicator = n
            break
    if not indicator:
        return []

    # Skip if a required_field rule already targets this column (no duplicate).
    key = indicator.strip().lower()
    for entry in (synth_outputs or []):
        for ir in (entry.get("candidates") or []):
            if ir.get("template") == "required_field" and str(
                    (ir.get("params") or {}).get("field", "")).strip().lower() == key:
                return []

    ir = {
        "template": "required_field",
        "params": {"field": indicator},
        "rule_name": f"{indicator} must not be blank",
        "rule_description": (
            f"{indicator} is the referral indicator and must be populated on every "
            f"policy row — a blank cell means the referral decision was not recorded."),
        "severity": "warning",
        "error_message": f"{indicator} must not be blank.",
        "confidence": 1.0,
    }
    return [{
        "clause": {"clause_id": None,
                   "text": f"[Derived rule] {indicator} must not be blank "
                           f"(referral-indicator column must be populated)",
                   "page_number": None},
        "engine": "ir",
        "candidates": [ir],
    }]


def derive_territory_exclusion_entries(synth_outputs, clauses, template_fields):
    """Deterministic backstop for the "home state within <country>, excluding <US
    territories/possessions>" carve-out. The mapper (Call 3) is non-deterministic:
    some runs it declines the exclusion entirely (template=None → review, because
    it reads "US Territories and Possessions" as a category, not a cell value), and
    some runs it emits the exclusion with an unbindable country scope a guard drops
    — so the compulsory territory rule flaps run-to-run. This ALWAYS synthesizes the
    exclusion when the clause is present and the template has a home-state column,
    binding it to the SAME column the mapper aimed at (so the deterministic rule and
    any surviving mapper rule are byte-identical after territory expansion and
    collapse in dedup — no duplicate). The synthesized rule carries NO scope, so it
    never hits the scope-drop guard; the existing _expand_us_territory_exclusion /
    _seed_territory_abbreviations machinery in rule_normalizer then normalizes the
    excluded set to the full canonical list. Generic: detection is regex over the
    clause text (the US-territory reference pattern already in rule_normalizer) +
    column-name tokens — no contract/carrier/state literals."""
    from contract_upload_services.rule_normalizer import _TERRITORY_TRIGGER

    names = [f.get("name") for f in (template_fields or []) if f.get("name")]

    # (a) A territory carve-out clause: names US territories/possessions AND frames
    # them as excluded from the covered home-state set.
    terr_clause = None
    for c in (clauses or []):
        txt = c.get("text") or ""
        if (_TERRITORY_TRIGGER.search(txt)
                and re.search(r"exclud|except|outside|\bnot\b", txt, re.I)
                and re.search(r"home\s+state|territor|\bstate\b", txt, re.I)):
            terr_clause = c
            break
    if not terr_clause:
        return []

    # (b) Target the home-state column. Prefer the column the mapper already aimed a
    # state-level value_in_set/value_not_in_set at (so the two rules dedup); else the
    # insured's/risk's state column (never a filing / broker state).
    def _is_state(f):
        return isinstance(f, str) and "state" in f.lower()
    # Candidate home-state columns (never a filing / broker / surplus-lines state —
    # those are transaction-party states, not the insured's home state).
    cand = [n for n in names if "state" in n.lower()
            and not any(x in n.lower() for x in ("filing", "broker", "surplus"))]
    # The insured's HOME state column, by role token (home > insured > situs).
    home_col = None
    for key in ("home", "insured", "situs"):
        home_col = next((n for n in cand if key in n.lower()), None)
        if home_col:
            break
    # Whether THIS clause explicitly constrains the "home state" (vs a generic
    # territory/state mention). A home-state carve-out applies to the insured's
    # home state, NOT the risk-location state.
    is_home_state = bool(re.search(r"home\s+state", terr_clause.get("text") or "", re.I))
    # The column the mapper (Call 3) already aimed a state exclusion at, if any.
    mapper_col = None
    for entry in (synth_outputs or []):
        for ir in (entry.get("candidates") or []):
            if (isinstance(ir, dict)
                    and ir.get("template") in ("value_in_set", "value_not_in_set")
                    and _is_state((ir.get("params") or {}).get("field"))):
                mapper_col = (ir.get("params") or {}).get("field")
                break
        if mapper_col:
            break
    if is_home_state and home_col:
        # A "home state ... excluding territories" clause MUST bind to the insured's
        # home state, even when the (non-deterministic) mapper aimed its own state
        # exclusion at a different column (e.g. Risk State). This adds the missing
        # home-state coverage; any mapper rule on another column is left intact and
        # deduped separately by compiled-SQL signature — no duplicate, nothing removed.
        state_col = home_col
    elif mapper_col and mapper_col in names:
        # Non-home-state territory clause: align to the mapper's column so the two
        # rules dedup to one (byte-identical after territory expansion).
        state_col = mapper_col
    else:
        state_col = None
        for key in ("risk", "insured", "home", "situs"):
            state_col = next((n for n in cand if key in n.lower()), None)
            if state_col:
                break
        state_col = state_col or (cand[0] if cand else None)
    if not state_col:
        return []

    # (c) Don't ship the SAME constraint twice. If the mapper already emitted a
    # TERRITORY exclusion on the chosen column, this backstop has nothing to add.
    # (Relying on the downstream SQL-signature dedup is not enough: the two rules
    # only collapse when byte-identical, and their variation_values routinely
    # differ — which shipped "Excluded Territory - US Sub-regions" AND "Excluded
    # Territory" on the same column, flagging the same row twice.) Detection is the
    # existing territory regex over the rule's own values — no literals.
    for _e in (synth_outputs or []):
        for _ir in (_e.get("candidates") or []):
            if not isinstance(_ir, dict) or _ir.get("template") != "value_not_in_set":
                continue
            _p = _ir.get("params") or {}
            if str(_p.get("field") or "").strip().lower() != state_col.strip().lower():
                continue
            _vals = list(_p.get("excluded") or []) + list(_p.get("variation_values") or [])
            if any(isinstance(v, str) and _TERRITORY_TRIGGER.search(v) for v in _vals):
                return []

    excluded = sorted({m.group(0).strip()
                       for m in _TERRITORY_TRIGGER.finditer(terr_clause.get("text") or "")},
                      key=str.lower)
    ir = {
        "template": "value_not_in_set",
        "params": {"field": state_col,
                   "excluded": excluded or ["US Territories and Possessions"]},
        "rule_name": "Excluded Territory",
        "rule_description": (
            f"Policies must not be issued with a home state ({state_col}) in an "
            f"excluded US territory or possession."),
        "severity": "critical",
        "error_message": f"{state_col} is an excluded US territory or possession.",
        "confidence": 0.9,
    }
    return [{
        "clause": {"clause_id": terr_clause.get("clause_id"),
                   "text": terr_clause.get("text"),
                   "page_number": terr_clause.get("page_number")},
        "engine": "ir",
        "candidates": [ir],
    }]


# Answer/data multiple for THIS stage, fed to would_truncate() and to the fallback
# chunk sizer below. Measured 1.89-2.08 over 9 real calls; 2.5 sits above the
# observed max. Extraction expands its input: a sentence of contract prose becomes
# a JSON clause object with quoted keys, page refs and a rationale. Covers the
# ANSWER only — the thinking budget is accounted for separately at each use site.
_STAGE1_OUTPUT_RATIO = float(os.getenv("KAVACHIO_STAGE1_OUTPUT_RATIO", "2.5"))


def _generic_bind_key(lib_rules, template_fields):
    """Cache key for the generic-library → output-template binding.

    Covers exactly the two things that answer depends on:
      • every library row's IDENTITY AND CONTENT — id, name, class, logic, severity.
        The id alone is not enough: editing a rule's wording changes what the mapper
        binds it to while the row keeps its id. tenant_id rides along so a tenant
        rule can never be served from a global-only bind, or vice versa.
      • every template field the prompt shows the model — name, sheet, canonical
        field, and the samples it matches value KIND on. A renamed or added column
        must invalidate; re-uploading the same template must not.

    Sorted, because neither load_generic_rules nor the template parser guarantees a
    stable order across runs and an ordering flip must not look like a new answer.

    Returns None when there is nothing to bind — that disables the cache for this
    upload rather than keying on an empty set.
    """
    if not lib_rules or not template_fields:
        return None
    rules_part = sorted(
        (r.get("id"), r.get("rule_name"), r.get("class_name"),
         r.get("validation_logic"), r.get("severity"), r.get("tenant_id"))
        for r in lib_rules
    )
    # Key on EXACTLY what the mapping prompt renders for each field, and nothing
    # more — see prompt_builder._template_fields_block. Over-specifying here is not
    # "safer": it makes the key move on input the model never sees, so the cache
    # misses forever. That is precisely what canonical_field did — the prompt
    # deliberately hides it (a wrong upstream tag used to override a column's real
    # meaning), and it is assigned by the template-mapping LLM call, so it differed
    # on every run and produced a fresh key every time.
    fields_part = sorted(
        (f.get("name"),
         tuple(f.get("sheets") or [f.get("sheet")]),
         (" ".join(str(f.get("description")).split())[:140]
          if f.get("description") else None),
         tuple(f.get("allowed_values") or [])[:15],
         tuple(str(s) for s in (f.get("samples") or [])[:3]))
        for f in template_fields if f.get("name")
    )
    return ai_cache.make_key("generic_bind_v1", rules_part, fields_part)


def _derive_pages_per_chunk(pdf_data, thinking_budget=16384, default=8):
    """Pages per fallback chunk, sized from THIS document's own text density.

    A fixed page count silently assumes every contract puts the same amount of
    text on a page. It does not: 8 pages is right at ~1,600 text tokens/page, but
    a dense schedule at 4,000 tokens/page would truncate the very chunk that is
    meant to be the safety net. So measure the document in front of us — answer
    tokens run ~_STAGE1_OUTPUT_RATIO x the text sent, and the answer shares the
    output budget with thinking. KAVACHIO_SECTION_PAGES still overrides when set.
    """
    pages = pdf_data.get("pages") or []
    if not pages:
        return default
    try:
        per_page = max(1, estimate_tokens(build_llm_context(pdf_data)) // len(pages))
        _in_ceil, out_ceil = model_ceilings()
        room = (out_ceil * OUTPUT_SAFETY) - int(thinking_budget or 0)
        n = int(room // max(1, per_page * _STAGE1_OUTPUT_RATIO))
        # Floor of 1 so a very dense contract still makes progress; ceiling of 20
        # so a nearly-empty one does not put the whole document back in one chunk.
        return max(1, min(n, 20))
    except Exception:
        return default


class ValidationRuleGenerator:

    def __init__(
        self,
        auto_trust_threshold=DEFAULT_RULE_AUTO_TRUST_THRESHOLD,
        tenant_id=None,
        program_id=None
    ):
        self.rule_class_library    = RULE_CLASS_LIBRARY
        self.auto_trust_threshold  = auto_trust_threshold
        self.tenant_id             = tenant_id
        self.program_id            = program_id

    # =====================================================
    # PUBLIC: Pipeline 2 on its own (clauses in → rules out)
    # =====================================================

    def run_pipeline_2(self, clauses_extracted, template_fields, contract_id,
                       tenant_id=None, reference_documents=None):
        """Classify, synthesize, verify — everything from Call 2 onwards.

        Split out of `generate_validation_rules_json` so it can be run on its
        own, over clauses that are ALREADY in the database. That is the case a
        contract lands in when it is added before the programme has an Output
        Template: rules are written against a template's columns, so the run
        stops after the clauses and every rule-bearing one is parked awaiting a
        template. This is how that work gets finished when the template arrives
        — the source document is not retained, so re-reading it is not merely
        wasteful, it is impossible.

        `clauses_extracted` is mutated in place (each clause gains its
        rule_generation_status and classification), exactly as before.

        Returns a dict: no_template, classifications, synth_outputs,
        output_schema, validation_rules, review_queue, control_register.
        """
        # -------------------------------------------------
        # CALL 2 — rule_bearing + rule INTENT (field-agnostic)
        # Merges Stage A classification with intent extraction in one call. The
        # result is classification-shaped (is_rule_bearing, rule_types, …) and
        # also carries an `intents` list that Call 3 maps to output fields.
        # -------------------------------------------------

        with plog.stage("Call 2 intents"):
            classifications = extract_rule_intents(clauses_extracted)

        # Update each clause's rule_generation_status from the verdict
        for clause, classification in zip(clauses_extracted, classifications):
            clause["rule_generation_status"] = resolve_status(classification)
            clause["classification"] = classification

        # -------------------------------------------------
        # NO OUTPUT TEMPLATE — the run ends here, with the clauses.
        # -------------------------------------------------
        # Every rule is written against an output template's COLUMNS: Call 3
        # binds each intent to one of them, and the verify gate refuses any rule
        # referencing a column the template does not have (rule_ir.validate_ir).
        # With no template there are no columns, so Call 3 would ask the model to
        # bind every intent to an empty list and the gate would then refuse all
        # of its answers — the most expensive call in the pipeline, bought to
        # produce nothing.
        #
        # So a contract read without one stops at what it CAN produce: its
        # clauses, and the verdict on which of them carry a rule. Those verdicts
        # go to the review bucket naming the reason, so the work is visible and
        # can be finished the moment a template exists — rather than the contract
        # looking as though it had nothing to say.
        if not template_fields:
            review_queue, control_register = [], []
            for clause, classification in zip(clauses_extracted, classifications):
                entry = {
                    "clause_id":   clause.get("clause_id"),
                    "clause_text": clause.get("text"),
                    "source_page": clause.get("page_number") or clause.get("page"),
                }
                if classification.get("is_rule_bearing"):
                    review_queue.append({
                        **entry,
                        "reason": "no output template yet — this clause carries a "
                                  "rule, but a rule can only be written against an "
                                  "output template's columns",
                    })
                else:
                    control_register.append({
                        **entry,
                        "reason": classification.get("reasoning")
                                  or "non-rule-bearing clause (obligation / governance)",
                    })
            print(f"\n[Pipeline 2] No output template — stopping after clauses: "
                  f"{len(clauses_extracted)} clause(s), "
                  f"{len(review_queue)} awaiting a template, "
                  f"{len(control_register)} to control register. No rules written.")
            # The caller owns the Pipeline-1 half of the answer (metadata,
            # commercial terms, what the contract defers to), so it builds the
            # final output; this returns only what Pipeline 2 decided.
            return {
                "no_template":      True,
                "classifications":  classifications,
                "synth_outputs":    [],
                "output_schema":    None,
                "validation_rules": [],
                "review_queue":     review_queue,
                "control_register": control_register,
            }

        # # Persist Stage A classification side-car JSON (mirrors Stage B files).
        # save_stage_a_output(
        #     clauses_extracted,
        #     classifications,
        #     output_dir,
        #     contract_id=contract_id,
        #     file_base=file_base
        # )

        # -------------------------------------------------
        # PIPELINE 2.4 — Stage B (IR) extraction
        # -------------------------------------------------

        # No hardcoded confidence cutoff: confidence is advisory (the verify gate
        # is the real gate), and is_rule_bearing routing happens inside Stage B —
        # non-rule-bearing clauses go to the control register, nothing is dropped.
        # Pass ALL clauses; the synthesizer partitions rule-bearing vs not.
        rule_bearing = sum(
            1 for c in classifications if c.get("is_rule_bearing")
        )
        print(
            f"[Pipeline 2] Stage B IR extraction on "
            f"{rule_bearing} rule-bearing clause(s) "
            f"(of {len(clauses_extracted)} total)."
        )

        if template_fields:
            print(
                f"[Pipeline 2] Using template-aware IR extraction "
                f"({len(template_fields)} output template fields)"
            )

        # Output Template = the canonical field namespace every rule is written
        # against (field_names for the existence gate, field_to_sheet for the
        # compiler, grouped list for the prompt).
        output_schema = build_output_schema(template_fields)

        # CALL 3 — map each rule intent (from Call 2) to ONE template + Output
        # Template fields. This focused mapping step recovers checkable rules
        # (territory, policy-period, products-aggregate) that the old combined
        # "extract + map" call under-mapped. Output → IR candidates per clause.
        #
        # The generic rule library rides along in this SAME call. Both halves ask
        # the mapper the identical question — bind this intent to a column of this
        # Output Template — against the identical field list, so a second call was
        # buying nothing but a second bill. Library intents are appended AFTER the
        # contract ones (and are already tenant-before-global) so ordering is
        # preserved on both sides.
        #
        # The RESULT is split apart again immediately below. Merging the CALL is
        # safe; merging the pipeline POSITION is not — the library's dedup has to
        # run after every derived-rule injector further down, so the library half
        # is held back and finished at its original point. See
        # finish_generic_entries.
        # NOTE: the `tenant_id` PARAMETER, not self.tenant_id — they are distinct
        # (the parameter shadows the attribute and is what the library bind has
        # always used), so reading the attribute here would silently change scope.
        lib_clauses, lib_intents, lib_rules = build_generic_intents(
            tenant_id, template_fields)
        if lib_clauses:
            print(f"\n[Generic] {len(lib_clauses)} library rule(s) merged into the "
                  f"Call-3 mapping call (tenant_id={tenant_id}).")

        # ── COST: memoize the library half of the Call-3 bind ────────────────
        # Binding a library rule asks "which column of THIS output template means
        # 'Insured ZIP Code'?" — a question about the LIBRARY and the TEMPLATE, with
        # no input from the contract being uploaded. Every contract uploaded against
        # the same template re-bought the identical answer, and it is the most
        # expensive answer in the pipeline to buy: on a measured run the library was
        # 50 of the 80 Call-3 items, and Call 3 was ~59% of the bill.
        #
        # Safe because the key covers everything that can change the answer (see
        # _generic_bind_key) and every call runs at temperature 0 with a fixed seed,
        # so the cached answer IS what a re-ask returns. A hit skips ONLY the library
        # intents — map_intents_to_ir partitions contract from library intents, so
        # the contract half's batches are identical whether this hits or misses.
        _lib_key = _generic_bind_key(lib_rules, template_fields)
        _lib_cached = ai_cache.get("generic_bind", _lib_key) if _lib_key else None

        if _lib_cached is not None:
            print(f"[Generic] reusing cached bind for {len(lib_clauses)} library "
                  f"rule(s) — their Call-3 mapping is skipped entirely.")
            plog.log("CALL3", "SKIPPED",
                     f"library bind for {len(lib_clauses)} rule(s) served from cache",
                     "answer depends only on (library rows, template fields) — "
                     "no contract input, so it is reused rather than re-bought")
            with plog.stage("Call 3 mapping"):
                synth_outputs = map_intents_to_ir(
                    clauses_extracted,
                    classifications,
                    template_fields=template_fields,
                )
            generic_mapped = _lib_cached
        else:
            with plog.stage("Call 3 mapping"):
                synth_outputs = map_intents_to_ir(
                    clauses_extracted + lib_clauses,
                    classifications   + lib_intents,
                    template_fields=template_fields,
                )

            # Split the answer back apart. is_generic_entry keys on the sign of
            # clause_id (library rules carry the negated library row id), so this is
            # exact rather than a name match. After these two lines synth_outputs holds
            # precisely what it held before the merge, and every injector below runs
            # unchanged and never sees a library rule.
            generic_mapped = [e for e in synth_outputs if is_generic_entry(e)]
            synth_outputs  = [e for e in synth_outputs if not is_generic_entry(e)]

            # Store the library half only, and never an EMPTY one: a cache entry that
            # binds nothing would suppress every library rule on every future upload
            # against this template. A run that mapped nothing (failed or truncated
            # batch) is deliberately left uncached so the next upload retries it.
            if _lib_key and generic_mapped:
                ai_cache.put("generic_bind", _lib_key, generic_mapped,
                             tenant_id=tenant_id)

        # #7b — MERGE CONTRADICTORY SIBLING ENUMS (deterministic, generic).
        # Sibling program tables ("<identifying label>: <name A>" / "<name B>" /
        # "<name C>") each yield an UNSCOPED value_in_set on the same column
        # allowing only their own name. Rules are conjunctive, so three
        # disjoint singletons on one field flag EVERY row — the only consistent
        # reading is their union (any of the named cohorts). The prompts steer
        # the model away from emitting these, but compliance is probabilistic;
        # this repair is the guarantee. See merge_sibling_enum_rules.
        nme = merge_sibling_enum_rules(synth_outputs)
        if nme:
            print(f"[Call 3] merged {nme} contradictory sibling enum rule(s) "
                  f"into union value set(s)")

        # #8 — Re-anchor a "backdating" period rule to PolicyInception → Transaction
        # (processing) date (deterministic, generic; the mapper's field choice for
        # backdating is inconsistent). See fix_backdating_period_fields.
        nbd = fix_backdating_period_fields(synth_outputs, template_fields)
        if nbd:
            print(f"[Call 3] re-anchored {nbd} backdating period rule(s) to "
                  f"policy-inception → transaction-date")

        # #9 — Repair a headline-rate commission/fee formula (e.g. Commission
        # Amount = Gross × 0.25) to use the BDX's PER-ROW rate column instead of
        # the hardcoded contract rate, when such a column exists. Deterministic,
        # generic. See fix_hardcoded_rate_formulas.
        nhr = fix_hardcoded_rate_formulas(synth_outputs, template_fields)
        if nhr:
            print(f"[Call 3] repaired {nhr} hardcoded-rate formula(s) to use the "
                  f"per-row rate column")

        # #9b — A commission-family rate SCHEDULE stated as a bare % ("Commissions
        # Schedule: 25.05%") is a FIXED value the reported rate must EQUAL, not a
        # ceiling/floor. When the mapper mis-typed it as max_limit/min_limit (so it
        # silently flags nothing), collapse it to range_check(min==max). Generic,
        # guarded. See fix_fixed_rate_schedule_bounds.
        nrs = fix_fixed_rate_schedule_bounds(synth_outputs, template_fields)
        if nrs:
            print(f"[Call 3] collapsed {nrs} commission-schedule rate rule(s) to a "
                  f"fixed-value (equals) check")

        # #8b — Companion rule on the program-period date COLUMN. When a clause's
        # program effective/expiration date became a date_bound on a POLICY date
        # field, also require the reported Program Effective/Expiration Date column
        # to EQUAL that contract date. Deterministic, generic. See
        # derive_program_period_companion_rules.
        prog_companions = derive_program_period_companion_rules(
            synth_outputs, template_fields)
        if prog_companions:
            print(f"[Call 3] +{len(prog_companions)} program-date companion rule(s): "
                  f"{[e['candidates'][0]['params']['field'] for e in prog_companions]}")
            synth_outputs.extend(prog_companions)

        # #6 — DERIVED formula rules (deterministic, not from a single clause):
        # e.g. Commission Amount = Gross Premium × Commission Rate. Added only when
        # the template has the matching column trio AND the contract already
        # governs that rate, so the reported amount is checked, not just the rate.
        # Deterministic territory backstop FIRST (reads the pristine mapped
        # candidates to decide whether the mapper already emitted an exclusion).
        terr = derive_territory_exclusion_entries(
            synth_outputs, clauses_extracted, template_fields)
        if terr:
            print(f"[Call 3] +{len(terr)} deterministic territory exclusion rule(s) "
                  f"(mapper produced none): {[e['clause']['text'][:60] for e in terr]}")
            synth_outputs.extend(terr)

        derived = derive_formula_entries(synth_outputs, template_fields)
        if derived:
            print(f"[Call 3] +{len(derived)} derived formula rule(s): "
                  f"{[e['clause']['text'] for e in derived]}")
            synth_outputs.extend(derived)

        # #6b-AI — when the template carries NO explicit per-column formula row,
        # ask the model in ONE call which columns are arithmetically COMPUTED and
        # attach the inferred formula to each, so the SAME annotation-formula
        # machinery below turns them into cross-field rules. Generic (the model
        # decides — works for columns never seen before), additive (only fills a
        # `formula` that isn't already set from a real annotation row), and safe
        # (wrapped; a failure just yields nothing). See infer_formula_annotations.
        try:
            with plog.stage("Formula inference"):
                _ai_formulas = infer_formula_annotations(template_fields)
        except Exception as _exc:
            print(f"[Call 3] AI formula inference skipped ({_exc})")
            _ai_formulas = {}
        if _ai_formulas:
            _added = 0
            for _f in (template_fields or []):
                if not _f.get("formula") and _f.get("name") in _ai_formulas:
                    _f["formula"] = _ai_formulas[_f["name"]]
                    _added += 1
            if _added:
                print(f"[Call 3] AI inferred {_added} column formula(s) "
                      f"(template has no formula-annotation row)")

        # #6b — DERIVED formula rules from a per-column FORMULA annotation carried
        # on the output template (the exact arithmetic the template author wrote,
        # e.g. "Payable due AmWins Re = Palms Gross Written Premium $ − Gross
        # Commission …") OR the AI-inferred formula attached just above. Parsed
        # generically; each operand resolved to a real column. Runs AFTER
        # derive_formula_entries so it skips any result field that already got a
        # formula. See derive_annotation_formula_entries.
        ann_formulas = derive_annotation_formula_entries(synth_outputs, template_fields)
        if ann_formulas:
            print(f"[Call 3] +{len(ann_formulas)} annotation-formula rule(s): "
                  f"{[e['candidates'][0]['params'].get('result_field') or e['candidates'][0]['params'].get('field') for e in ann_formulas]}")
            synth_outputs.extend(ann_formulas)

        # #6c — STRUCTURAL fallback for templates with NO formula annotation: an
        # "<entity> <concept> Amount $" column with a sibling per-row rate column
        # and an entity-matched base → amount = base × rate. Deduped against the
        # mapper and the two derivers above. See derive_rate_amount_formulas.
        rate_formulas = derive_rate_amount_formulas(synth_outputs, template_fields)
        if rate_formulas:
            print(f"[Call 3] +{len(rate_formulas)} structural rate×base formula(s): "
                  f"{[e['candidates'][0]['params']['result_field'] for e in rate_formulas]}")
            synth_outputs.extend(rate_formulas)

        # #6d — VALUE-driven backstop for templates whose headers defeat every
        # name-matching gate above (terse ALL-CAPS bordereaux: "COMMISSION AMT",
        # "COMPANY CEDE", "NET CEDED"). Emits only relationships that reproduce the
        # reported amount on every sampled row, product AND complement. Runs last,
        # so it only ever fills columns nothing else defined.
        # See derive_verified_rate_formulas.
        try:
            verified = derive_verified_rate_formulas(synth_outputs, template_fields)
        except Exception as _exc:
            print(f"[Call 3] verified rate formula derivation skipped ({_exc})")
            verified = []
        if verified:
            print(f"[Call 3] +{len(verified)} sample-verified formula(s): "
                  f"{[e['candidates'][0]['rule_name'] for e in verified]}")
            synth_outputs.extend(verified)

        # #7 — Closed-set rules from the column HEADER (e.g. "Facultative
        # Re(Y/N)" → value must be Y/N; "Policy Type (Primary/Excess)" → value
        # must be Primary/Excess). Deterministic, header-driven, no LLM. Runs
        # AFTER the mapper + formula derivation so it can skip any flag column a
        # contract clause already governs (no duplicate rule).
        bool_flags = derive_header_enum_entries(synth_outputs, template_fields)
        if bool_flags:
            print(f"[Call 3] +{len(bool_flags)} derived header-enum rule(s): "
                  f"{[e['candidates'][0]['params']['field'] for e in bool_flags]}")
            synth_outputs.extend(bool_flags)

        # #8 — The referral-INDICATOR column must be populated on every policy
        # (non-blank data-quality check). Deterministic, role-token located, no LLM.
        # Runs after the header-enum deriver so it never doubles an existing rule.
        ref_ind = derive_referral_indicator_presence_rule(synth_outputs, template_fields)
        if ref_ind:
            print(f"[Call 3] +{len(ref_ind)} referral-indicator presence rule(s): "
                  f"{[e['candidates'][0]['params']['field'] for e in ref_ind]}")
            synth_outputs.extend(ref_ind)

        # GENERIC RULE LIBRARY — Kavachio's standard BDX checks from the
        # `generic_rule_specification` table. Not contract-derived: they apply to
        # every program, so they skip Call 1/2 and were bound to this program's
        # columns by the SAME Call-3 mapper — in the same call, up at CALL 3; only
        # the deterministic guards and the dedup happen here.
        #
        # This still runs LAST of the injectors, which is the whole reason the two
        # halves are separate: the dedup below must see every derived-rule entry
        # already in synth_outputs, and drop_derived_duplicates must find the
        # derived twin so _carry_dispatch_params can move its country dispatch onto
        # the surviving library rule. Only the AI call moved earlier; this did not.
        generic_entries = finish_generic_entries(
            generic_mapped, lib_rules, synth_outputs, template_fields)
        if generic_entries:
            print(f"[Call 3] +{sum(len(e['candidates']) for e in generic_entries)} "
                  f"generic library rule(s)")
            # A library rule and an auto-derived data-quality rule on the SAME
            # column say the same thing twice ("[Derived rule] Insured Zip Code
            # must be a valid postal code…" vs "[Generic rule] Insured ZIP Code
            # Must Be Valid"). The library rule wins — drop the derived twin so
            # the reviewer sees one rule per column, from the editable catalogue,
            # carrying over the country dispatch the derived rule worked out.
            drop_derived_duplicates(synth_outputs, generic_entries)
            # …and hold the library to the same bar the deriver holds itself to: a
            # reference-vocabulary check with no country to resolve against is
            # unanswerable, so it is dropped rather than shipped. See
            # drop_uncountried_reference_rules.
            drop_uncountried_reference_rules(generic_entries)
            synth_outputs.extend(generic_entries)

        # -------------------------------------------------
        # PIPELINE 2.5 — Verify gate + routing (deterministic)
        # -------------------------------------------------

        contract_ctx = {
            "tenant_id":   self.tenant_id,
            "contract_id": contract_id,
            "program_id":  self.program_id
        }

        # Reference-doc GROUP → MEMBERS map (data-driven, from the uploaded
        # reference documents' tables). Lets a value-set rule whose values are
        # category/group names (e.g. authorized/excluded "Occupancy Group"s) be
        # expanded to also carry every specific member the reference lists under
        # that group, so a BDX row reporting a specific class matches.
        group_members = build_reference_group_members(reference_documents)
        if group_members:
            print(f"[Pipeline 2.5] reference group→members map: "
                  f"{len(group_members)} group(s) "
                  f"{[v[0] for v in group_members.values()]}")

        # Each IR is verified (validate → vocab-normalize → field-existence →
        # compile → guard/dry-run) and routed to exactly one destination.
        with plog.stage("Verify + compile (no AI)"):
            validation_rules, review_queue, control_register = normalize_ir_outputs(
                synth_outputs,
                contract_ctx,
                output_schema,
                group_members=group_members,
            )
        print(
            f"[Pipeline 2.5] {len(validation_rules)} proposed rule(s), "
            f"{len(review_queue)} to review, "
            f"{len(control_register)} to control register."
        )

        return {
            "no_template":      False,
            "classifications":  classifications,
            "synth_outputs":    synth_outputs,
            "output_schema":    output_schema,
            "validation_rules": validation_rules,
            "review_queue":     review_queue,
            "control_register": control_register,
        }

    # =====================================================
    # PUBLIC: full pipeline
    # =====================================================

    def generate_validation_rules_json(
        self,
        pdf_data,
        source_file,
        output_dir=None,
        template_fields=None,
        halt_on_external_references=False,
        resume_token=None,
        reference_documents=None,
        tenant_id=None,
    ):
        """
        Run Pipeline 1 + Pipeline 2 on the parsed PDF data and return a
        single hybrid output dict. Safe to JSON-serialize.

        When `output_dir` is set, raw Stage B synthesis outputs are also
        written to two side-car JSON files:
          <output_dir>/<contract_id>_stage_b_ajv.json
          <output_dir>/<contract_id>_stage_b_custom.json
        """

        contract_id = self._derive_contract_id(source_file)
        file_base   = self._safe_filename(source_file, contract_id)

        # # Resolve output_dir — default to "validation_output_rules" so files
        # # are always written even when the caller omits the argument.
        # if output_dir is None:
        #     output_dir = "validation_output_rules"

        # os.makedirs(output_dir, exist_ok=True)

        # -------------------------------------------------
        # PIPELINE 1.2 — Section identification  (DISABLED)
        # -------------------------------------------------
        # Section-splitting + per-section extraction is commented out in favour
        # of feeding the whole document to the LLM in a single call below.
        #
        # sections = split_into_sections(pdf_data)
        #
        # if not sections:
        #     sections = split_pages_into_chunks(pdf_data, pages_per_chunk=5)
        #
        # print(
        #     f"\n[Pipeline 1] Identified {len(sections)} section(s) "
        #     f"for extraction."
        # )

        # -------------------------------------------------
        # PIPELINE 1.3 — Structured extraction per section  (DISABLED)
        # -------------------------------------------------
        # section_extractions = []
        #
        # for idx, section in enumerate(sections, start=1):
        #
        #     label = (
        #         f"Pipeline1-Sec{idx}/"
        #         f"{len(sections)}-{section.get('section_type')}"
        #     )
        #
        #     try:
        #
        #         raw = call_gemini(
        #             build_extraction_prompt(section),
        #             label=label
        #         )
        #
        #         section_extractions.append(parse_llm_json(raw))
        #
        #     except Exception as exc:
        #
        #         print(f"[Pipeline 1] section {idx} failed: {exc}")
        #         section_extractions.append({
        #             "program_metadata": {},
        #             "commercial_terms": [],
        #             "clauses": []
        #         })

        # -------------------------------------------------
        # PIPELINE 1.3 (ACTIVE) — Single whole-document extraction
        # Feed the ENTIRE document to the LLM in ONE Gemini call instead of
        # splitting into sections. _merge_section_extractions still handles a
        # one-element list, so the rest of the pipeline is unchanged.
        # -------------------------------------------------

        section_extractions = []
        external_references = []

        # -------------------------------------------------
        # RESUME PATH — reuse the cached extraction from the halted run instead
        # of calling the extraction LLM again. (Triggered by "Continue Anyway".)
        # -------------------------------------------------
        cached = _EXTRACTION_RESUME_CACHE.pop(resume_token, None) if resume_token else None

        if cached is not None:
            section_extractions = cached.get("section_extractions", [])
            external_references = cached.get("external_references", [])
            print(
                f"\n[Pipeline 1] RESUMED from cached extraction "
                f"(token={resume_token}) — skipping extraction LLM call.\n"
            )

        else:
            # -------------------------------------------------
            # PIPELINE 1.3 — WHOLE-DOCUMENT extraction (ONE call)
            # The whole contract goes to the model in a single call so it reasons
            # about the document as a COHERENT WHOLE — cross-page context,
            # definitions that qualify later limits, and clauses that span a page
            # break are all preserved. (Per-page chunking gave higher recall but
            # stripped overall meaning.) The recall problem that motivated chunking
            # was output-token TRUNCATION on long JSON, so we raise
            # max_output_tokens and rely on the prompt's strict "emit EVERY clause"
            # rule instead of fragmenting the document.
            # _merge_section_extractions still handles a one-element list, so the
            # rest of the pipeline is unchanged.
            # -------------------------------------------------
            pages = pdf_data.get("pages", [])
            whole_doc = {
                "section_type": "full_document",
                "page_start":   pages[0]["page"] if pages else 0,
                "page_end":     pages[-1]["page"] if pages else 0,
                "text":         build_llm_context(pdf_data),
            }
            print(f"\n[Pipeline 1] extracting whole document in 1 call "
                  f"({len(pages)} page(s)).")
            if reference_documents:
                print(
                    f"[Pipeline 1] with {len(reference_documents)} reference "
                    f"document(s): {[rd.get('name') for rd in reference_documents]}"
                )

            ext_prompt = build_extraction_prompt(
                whole_doc, reference_documents=reference_documents,
            )

            def _extract_by_section(reason):
                """Graceful fallback: extract page-chunks and merge, instead of
                returning an EMPTY skeleton (total loss). Accepts minor cross-page
                context loss over losing every clause."""
                # 8 pages, not 5: what chunking COSTS is cross-page context, so the
                # fallback should chunk as little as possible. 8 pages x ~3,021
                # answer tokens/page + 16,384 thinking = ~62% of the output ceiling,
                # the largest chunk that still clears the safety margin.
                _env_pages = os.getenv("KAVACHIO_SECTION_PAGES")
                pages_per = (int(_env_pages) if _env_pages
                             else _derive_pages_per_chunk(pdf_data))
                chunks = split_pages_into_chunks(pdf_data, pages_per_chunk=pages_per)
                print(f"[Pipeline 1] {reason} — extracting by section "
                      f"({len(chunks)} chunk(s) of {pages_per} page(s)).")
                added = 0
                for sec in chunks:
                    try:
                        raw = call_gemini(
                            build_extraction_prompt(
                                sec, reference_documents=reference_documents),
                            label=f"Pipeline1-Section-p{sec.get('page_start')}",
                            max_output_tokens=65536, thinking_budget=16384,
                            temperature=0, seed=DETERMINISTIC_SEED,
                        )
                        parsed_sec = parse_llm_json(raw)
                        section_extractions.append(parsed_sec)
                        added += 1
                        if isinstance(parsed_sec, dict):
                            external_references.extend(
                                parsed_sec.get("external_references", []) or [])
                    except Exception as se:
                        print(f"[Pipeline 1] section p{sec.get('page_start')} "
                              f"failed: {se}")
                return added

            # Pre-flight: gate on the OUTPUT budget, not the input ceiling. This
            # call never fails on input (a whole contract is a few thousand tokens
            # against a 1M ceiling) — it fails because the ANSWER did not fit. The
            # contract text is the data; the answer runs ~_STAGE1_OUTPUT_RATIO x
            # its size and shares max_output_tokens with the 16,384 thinking budget.
            over, est, limit = would_truncate(whole_doc["text"], 16384,
                                              ratio=_STAGE1_OUTPUT_RATIO)
            if over:
                if _extract_by_section(
                        f"whole-doc answer ~{est} tok > output budget {limit}") == 0:
                    section_extractions.append(
                        {"program_metadata": {}, "commercial_terms": [], "clauses": []})
            else:
                try:
                  with plog.stage("Call 1 extraction"):
                    raw = call_gemini(
                        ext_prompt,
                        label="Pipeline1-FullDocument",
                        max_output_tokens=65536,
                        thinking_budget=16384,
                        temperature=0,
                        seed=DETERMINISTIC_SEED,
                    )
                    parsed = parse_llm_json(raw)
                    # Completeness: valid JSON can still be a SHORT answer. Calls 2
                    # and 3 detect this by checking which clause_ids came back;
                    # Stage 1 has no such roster to check against, so use density as
                    # the proxy — a contract page yielding under half a clause means
                    # the model stopped early, and a thin extraction here silently
                    # costs every downstream rule. Deliberately lenient so a
                    # genuinely sparse document doesn't trigger a pointless re-run.
                    n_cl = (len(parsed.get("clauses") or [])
                            if isinstance(parsed, dict) else 0)
                    if n_cl < max(1, len(pages) // 2):
                        print(f"[Pipeline 1] only {n_cl} clause(s) from {len(pages)} "
                              f"page(s) — re-extracting by section.")
                        _extract_by_section("whole-doc extraction looked thin")
                    else:
                        section_extractions.append(parsed)
                        if isinstance(parsed, dict):
                            external_references.extend(
                                parsed.get("external_references", []) or []
                            )
                except Exception as exc:
                    # Oversize or a failed/truncated call → try sectioned extraction
                    # BEFORE giving up with an empty skeleton (total loss).
                    reason = ("oversize" if isinstance(exc, OversizeError)
                              else f"full-document extraction failed: {exc}")
                    if _extract_by_section(reason) == 0:
                        section_extractions.append({
                            "program_metadata": {},
                            "commercial_terms": [],
                            "clauses": [],
                        })

        # -------------------------------------------------
        # PIPELINE 1.4 — Synthesize & dedup (moved BEFORE the halt gate so the
        # deterministic external-reference backstop just below can scan the
        # merged, clean clause text before the gate decides whether to pause).
        # -------------------------------------------------

        program_metadata, commercial_terms, clauses_extracted = (
            self._merge_section_extractions(section_extractions)
        )

        # Reconcile each clause's page deterministically by matching its text back
        # to the source pages (the LLM no longer sees page markers).
        self._assign_clause_pages(clauses_extracted, pdf_data)

        # Deterministic backstop: the LLM's single big extraction call sometimes
        # misses a clause that defers to a NAMED external document (probabilistic
        # miss — see detect_deferred_external_references). Without this, such a
        # clause never reaches the halt gate below, so the user is never offered
        # the chance to supply that document — it silently proceeds unresolved.
        # Only ADDS references the LLM didn't already find (dedup by name).
        _llm_ref_names = {str(r.get("document_name", "")).strip().lower()
                          for r in external_references}
        # A clause whose deferral was already RESOLVED from a supplied reference
        # document carries that document's concrete values inlined as a
        # "[Context from <document> …]" block (see prompt_builder's reference
        # block). Its "as defined by the X Guidelines" wording is still in the
        # text, so the purely structural backstop would re-report a document the
        # user already gave us — halting for it, and later reporting the setup as
        # built without it. The LLM's own list already excludes provided
        # documents; this makes the backstop agree. Naturally inert when no
        # reference document was supplied (the block can only exist if one was).
        _backstop_refs = detect_deferred_external_references(
            [c for c in (clauses_extracted or [])
             if "[Context from" not in (c.get("text") or "")])
        _newly_added = []
        for r in _backstop_refs:
            if r["document_name"].strip().lower() not in _llm_ref_names:
                external_references.append(r)
                _llm_ref_names.add(r["document_name"].strip().lower())
                _newly_added.append(r["document_name"])
        if _newly_added:
            print(f"[Pipeline 1] external-reference backstop found "
                 f"{len(_newly_added)} deferred-document reference(s) the LLM "
                 f"missed: {_newly_added}")

        # One gate over BOTH sources. The LLM over-reports in its own ways — the
        # parent agreement from the preamble, an ordinary noun that happens to be
        # a document type, the same guideline once per date it is cited with —
        # and this list is what the user is asked to go and find, so it is
        # filtered on the same definition the backstop is built from.
        _before = len(external_references)
        external_references = drop_covered_references(
            filter_external_references(external_references), reference_documents)
        if len(external_references) != _before:
            print(f"[Pipeline 1] external references: {_before} candidate(s) → "
                  f"{len(external_references)} after filtering "
                  f"({[r['document_name'] for r in external_references]})")

        # -------------------------------------------------
        # HALT GATE — pause before the expensive Pipeline 2 when the contract
        # defers rules to external documents. The parsed extraction is cached
        # under a resume_token so "Continue Anyway" resumes WITHOUT re-extracting.
        # The route returns the token + reference names to the UI.
        # -------------------------------------------------
        if halt_on_external_references and external_references:
            token = uuid.uuid4().hex
            _EXTRACTION_RESUME_CACHE[token] = {
                "section_extractions": section_extractions,
                "external_references": external_references,
            }
            print(
                f"[Pipeline 1] HALTED — {len(external_references)} external "
                f"reference(s) found; cached as token={token}; awaiting user action."
            )
            return {
                "halted_for_references": True,
                "external_references":   external_references,
                "resume_token":          token,
                "metadata": {
                    "generated_at":            datetime.now(timezone.utc).isoformat(),
                    "source_file":             source_file,
                    "contract_id":             contract_id,
                    "halted_for_references":   True,
                    "external_reference_count": len(external_references),
                },
                "validation_rules":  [],
                "clauses_extracted": [],
            }

        # Assign stable clause_ids
        for i, c in enumerate(clauses_extracted, start=1):
            c["clause_id"] = i
            c["contract_id"] = contract_id
            c["rule_generation_status"] = "pending"

        # Resolve clause hierarchy (Root B): the extractor numbers clauses with its
        # own `local_id` and points each child at its parent via `parent_local_id`.
        # Map those to the real assigned clause_ids; unknown/absent parent → None.
        _local_to_id = {c.get("local_id"): c["clause_id"]
                        for c in clauses_extracted if c.get("local_id") is not None}
        for c in clauses_extracted:
            c["parent_clause_id"] = _local_to_id.get(c.get("parent_local_id"))

        print(
            f"[Pipeline 1] merged: "
            f"{len(commercial_terms)} commercial_terms, "
            f"{len(clauses_extracted)} clauses_extracted."
        )

        # --- Console the final Pipeline 1 (extraction) output ---
        pipeline1_output = {
            "contract_id":        contract_id,
            "source_file":        source_file,
            "program_metadata":   program_metadata,
            "commercial_terms":   commercial_terms,
            "clauses_extracted":  clauses_extracted,
            "external_references": external_references,
        }
        print("\n" + "=" * 60)
        print("FINAL PIPELINE 1 OUTPUT (extraction)")
        print("=" * 60)
        print(json.dumps(pipeline1_output, indent=2, default=str))
        print("=" * 60 + "\n")

        # # -------------------------------------------------
        # # PIPELINE 1.5 — Persist Pipeline 1 output to JSON
        # # -------------------------------------------------

        # pipeline1_output = {
        #     "stage":            "Pipeline 1 — Contract Extraction",
        #     "contract_id":      contract_id,
        #     "generated_at":     datetime.now(timezone.utc).isoformat(),
        #     "source_file":      source_file,
        #     "summary": {
        #         "clauses_extracted_count": len(clauses_extracted),
        #         "commercial_terms_count":  len(commercial_terms)
        #     },
        #     "program_metadata":  program_metadata,
        #     "commercial_terms":  commercial_terms,
        #     "clauses_extracted": clauses_extracted
        # }

        # pipeline1_path = os.path.join(output_dir, f"{file_base}_pipeline1.json")

        # with open(pipeline1_path, "w") as _f:
        #     json.dump(pipeline1_output, _f, indent=2, default=str)

        # print(f"[Pipeline 1] saved extraction output → {pipeline1_path}")

        # -------------------------------------------------
        # PIPELINE 2 — Call 2, Call 3, the derivers, and the verify gate.
        # Lifted into run_pipeline_2 so the same steps can be run over clauses
        # that are already in the database, for a contract whose Output Template
        # only turned up later. Nothing about the sequence changed.
        # -------------------------------------------------
        _p2 = self.run_pipeline_2(
            clauses_extracted, template_fields, contract_id,
            tenant_id=tenant_id, reference_documents=reference_documents,
        )
        classifications  = _p2["classifications"]
        validation_rules = _p2["validation_rules"]
        review_queue     = _p2["review_queue"]
        control_register = _p2["control_register"]
        # Kept under the legacy name for the final-output builder below.
        dropped          = review_queue

        # No template: the run ends at the clauses. Pipeline 1's half of the
        # answer is here, so the final output is built here too.
        if _p2["no_template"]:
            return self._build_final_output(
                source_file=source_file,
                contract_id=contract_id,
                program_metadata=program_metadata,
                commercial_terms=commercial_terms,
                clauses_extracted=clauses_extracted,
                classifications=classifications,
                validation_rules=[],
                dropped_candidates=[],
                review_queue=review_queue,
                control_register=control_register,
                external_references=external_references,
                reference_documents=reference_documents,
                template_fields=None,
            )


        # # -------------------------------------------------
        # # PIPELINE 2.5 — Persist normalization output to JSON
        # # -------------------------------------------------

        # save_normalization_output(
        #     validation_rules,
        #     dropped,
        #     output_dir,
        #     contract_id=contract_id,
        #     file_base=file_base
        # )

        # -------------------------------------------------
        # Build final output
        # -------------------------------------------------

        final_output = self._build_final_output(
            source_file=source_file,
            contract_id=contract_id,
            program_metadata=program_metadata,
            commercial_terms=commercial_terms,
            clauses_extracted=clauses_extracted,
            classifications=classifications,
            validation_rules=validation_rules,
            dropped_candidates=dropped,
            review_queue=review_queue,
            control_register=control_register,
            # Pipeline 1 decided these; they ride out with the rest of the
            # output so the upload response and the contract row can report
            # what the contract still defers to.
            external_references=external_references,
            reference_documents=reference_documents,
            template_fields=template_fields,
        )

        # # -------------------------------------------------
        # # PIPELINE 2.6 — Persist final output to JSON
        # # -------------------------------------------------

        # final_path = os.path.join(output_dir, f"{file_base}_final_output.json")

        # with open(final_path, "w") as _f:
        #     json.dump(final_output, _f, indent=2, default=str)

        # print(f"[Pipeline 2.6] saved final output → {final_path}")

        return final_output

    # =====================================================
    # PIPELINE 1.4 — Merge per-section extractions
    # =====================================================

    @staticmethod
    def _assign_clause_pages(clauses, pdf_data):
        """Set each clause's page_number deterministically by finding where its
        text occurs in the source pages. The extraction text is sent WITHOUT page
        markers, so the LLM can't reliably report pages; we reconcile here by
        matching a normalized snippet of the clause (its text, else its title)
        against each page's normalized text and taking the page where it STARTS.
        Falls back to whatever the model gave if no match is found."""
        import re as _re

        def _norm(s):
            return _re.sub(r"\s+", " ", (s or "")).strip().lower()

        page_texts = [
            (p.get("page"), _norm(p.get("text", "")))
            for p in (pdf_data.get("pages") or [])
        ]

        for cl in clauses:
            if not isinstance(cl, dict):
                continue
            for key in ("text", "title"):
                snippet = _norm(cl.get(key))[:60]
                if len(snippet) < 12:   # too short to match reliably
                    continue
                match = next((pg for pg, ptext in page_texts
                              if snippet in ptext), None)
                if match is not None:
                    cl["page_number"] = match
                    cl["page"] = match
                    break
        return clauses

    def _merge_section_extractions(self, section_extractions):

        merged_metadata = {}
        commercial_terms = []
        clauses = []

        for sec in section_extractions:

            # Gemini occasionally returns a JSON array (or other non-object)
            # for a section instead of the expected object. Skip those rather
            # than crashing with "'list' object has no attribute 'get'".
            if not isinstance(sec, dict):
                print(
                    f"[Pipeline 1.4] skipping non-object section extraction "
                    f"(got {type(sec).__name__})"
                )
                continue

            metadata = sec.get("program_metadata")
            if isinstance(metadata, dict):

                for k, v in metadata.items():

                    # Each field must be a {value, source_text, page, confidence}
                    # wrapper. The model sometimes emits a bare list/scalar —
                    # skip anything that isn't a dict.
                    if not isinstance(v, dict):
                        continue

                    if v.get("value") in (None, "", []):
                        continue

                    existing = merged_metadata.get(k)

                    # Keep the highest-confidence value; if equal, append both
                    if (
                        not existing
                        or (v.get("confidence", 0) or 0)
                           > (existing.get("confidence", 0) or 0)
                    ):
                        merged_metadata[k] = v

            for ct in sec.get("commercial_terms") or []:
                commercial_terms.append(ct)

            for cl in sec.get("clauses") or []:
                if isinstance(cl, dict):
                    clauses.append(cl)

        # Dedup clauses on EXACT (title, text) — only collapse entries that are
        # truly identical, so NO distinct clause is ever lost. (Deduping on text
        # alone was too aggressive: two genuinely different clauses that happen to
        # share an identical short body — e.g. "Restricted Segments Definition" and
        # "Excluded Classes Definition" both "as defined by the … Guidelines" —
        # would be wrongly merged, dropping a real clause.) Duplicate *rules* that
        # arise when two differently-worded clauses compile to the same check are
        # handled separately by the rule-level dedup in normalize_ir_outputs.
        seen = set()
        deduped_clauses = []

        for cl in clauses:

            title = cl.get("title") or ""
            text = cl.get("text") or ""

            if isinstance(title, dict):
                title = str(title)

            if isinstance(text, dict):
                text = str(text)

            norm_title = re.sub(r"\s+", " ", title).strip().lower()
            norm_text = re.sub(r"\s+", " ", text).strip().lower()
            # For a SUBSTANTIAL body, identical text = the SAME clause even if the
            # model gave it a different title (e.g. the same table row extracted
            # twice — once from the [TABLE] grid, once from the surrounding prose).
            # For a SHORT body keep the title distinction, since two genuinely
            # different clauses can share a short body ("as defined in the …
            # Guidelines").
            sig = (norm_text,) if len(norm_text) > 40 else (norm_title, norm_text)

            if sig in seen:
                continue

            seen.add(sig)
            deduped_clauses.append({
                "clause_type": cl.get("clause_type", "other"),
                "title": cl.get("title", ""),
                "text": cl.get("text", ""),
                "page_number": cl.get("page"),
                "section_header": cl.get("section_header"),
                "source_reference_document": cl.get("source_reference_document"),
                "extraction_confidence": cl.get("confidence", 0.0)
            })
            
        return merged_metadata, commercial_terms, deduped_clauses

    # =====================================================
    # Build the final output document
    # =====================================================

    def _build_final_output(
        self,
        source_file,
        contract_id,
        program_metadata,
        commercial_terms,
        clauses_extracted,
        classifications,
        validation_rules,
        dropped_candidates,
        review_queue=None,
        control_register=None,
        external_references=None,
        reference_documents=None, template_fields=None):

        review_queue = review_queue or []
        control_register = control_register or []
        external_references = external_references or []

        now = datetime.now(timezone.utc).isoformat()

        # program_name / document_type now come from Pipeline 1 program_metadata
        # (each metadata field is a {value, source_text, page, confidence} dict).
        program_name  = (program_metadata.get("program_name")  or {}).get("value")
        document_type = (program_metadata.get("document_type") or {}).get("value") \
            or "Insurance Document"

        # Stage A summary stats
        total = len(classifications)
        bearing = sum(
            1 for c in classifications if c.get("is_rule_bearing")
        )
        errors = sum(1 for c in classifications if c.get("_error"))

        # Stage B (IR) summary stats — every generated rule is a proposal pending
        # human approval (persisted as 'needs_review'); template is the unit, not
        # an ajv/custom engine.
        proposed_count = len(validation_rules)
        by_template = {}
        for r in validation_rules:
            t = r.get("template") or "unknown"
            by_template[t] = by_template.get(t, 0) + 1

        # Split contract-derived rules from the standard BDX library, so the UI can
        # show "N rules from your contract + M standard BDX validations" instead of
        # one undifferentiated list.
        by_source = {}
        for r in validation_rules:
            s = r.get("rule_source") or "contract"
            by_source[s] = by_source.get(s, 0) + 1

        return {
            "metadata": {
                "generated_at":        now,
                "source_file":         source_file,
                "contract_id":         contract_id,
                "document_type":       document_type,
                "program_name":        program_name,
                "contract_rule_count": 0,
                "validation_rule_count": len(validation_rules),
                # The output template's column names. Carried here so the
                # PERSIST side can run the same field-aware deterministic gates
                # over rules it did not generate (rules carried forward from a
                # prior version of this contract) — see
                # regen_reconcile.apply_gates. Names only: no samples, no
                # descriptions, so this stays small.
                "template_field_names": sorted(
                    {f.get("name") for f in (template_fields or []) if f.get("name")}
                ),

                "pipeline_1_summary": {
                    "clauses_extracted_count":  len(clauses_extracted),
                    "commercial_terms_count":   len(commercial_terms)
                },

                "stage_a_summary": {
                    "total_clauses":    total,
                    "rule_bearing":     bearing,
                    "not_rule_bearing": total - bearing - errors,
                    "errors":           errors
                },

                "stage_b_summary": {
                    "proposed":         proposed_count,
                    "by_template":      by_template,
                    # {"contract": N, "generic_library": M}
                    "by_source":        by_source,
                    "review_queue":     len(review_queue),
                    "control_register": len(control_register),
                }
            },

            # === Pipeline 1 outputs ===
            "program_metadata":  program_metadata,
            "commercial_terms":  commercial_terms,
            "clauses_extracted": clauses_extracted,
            # External document(s) the contract STILL defers rule content to.
            # A document that WAS provided is dropped from this list by the
            # extraction prompt, so what survives here is exactly "referenced
            # but not supplied" — the reason some clauses produced no rule.
            # Carried out of the pipeline (not just printed) so the upload
            # response and the contract row can both report it.
            "external_references": external_references,
            "reference_documents_provided": [
                (rd or {}).get("name") for rd in (reference_documents or [])
                if (rd or {}).get("name")
            ],

            # === Pipeline 2 outputs (deterministic IR model) ===
            "validation_rules":   validation_rules,
            "dropped_candidates": dropped_candidates,
            "review_queue":       review_queue,
            "control_register":   control_register,

            # # === Kept for downstream-consumer back-compat ===
            # # Legacy class_name pipeline removed; these keys remain (empty /
            # # derived) so the output shape stays stable for consumers.
            # "analysis": {
            #     "program_name":  program_name,
            #     "document_type": document_type
            # },
            # "rule_class_library":    self.rule_class_library,
            # "contract_rules":        []
        }

    # =====================================================
    # Helpers
    # =====================================================

    @staticmethod
    def _safe_filename(source_file, contract_id):

        base = os.path.splitext(
            os.path.basename(source_file or "")
        )[0]

        if not base:
            base = contract_id

        safe = re.sub(r"[^a-zA-Z0-9_\-]+", "_", base)

        return safe

    @staticmethod
    def _derive_contract_id(source_file):

        base = os.path.splitext(os.path.basename(source_file or ""))[0]

        slug = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_").upper()

        if not slug:
            slug = "CONTRACT"

        return f"{slug}_v1"