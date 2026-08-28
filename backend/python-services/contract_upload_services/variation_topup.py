"""
variation_topup.py
──────────────────
Ask the model for the surface spellings an enum rule is still missing, at
CONTRACT-UPLOAD time, for the values where structure could not supply them.

WHY THIS EXISTS
---------------
The rule-generation prompt asks for AT LEAST 3 spellings of EVERY authorized value
("VARIATION VALUES" in prompt_builder). That instruction sits inside a call that is
also deciding the template, the field mapping, the scope and the polarity for many
clauses at once, and it is the first thing the model drops under that load: on the
live rule set, 1642 of 3661 enum rules (45%) shipped carrying nothing but the
contract's own wording, so the check only ever matched a bordereau that spelled the
value exactly as the contract did.

Two layers now stand behind that instruction:

  1. rule_normalizer.seed_structural_variations — derives spellings from the
     value's OWN words (suffix drops, punctuation trims, initialism). Free,
     deterministic, offline. It cannot invent a spelling that isn't a cut of the
     text, so "Claims Made" → "Claims-Made" or "Volante International" → "Volante
     Intl" are beyond it.
  2. THIS module — one small, focused call for the values still short, where the
     only question asked is "how else is this written?".

The model here can only ADD candidates; every one is then put through the SAME
deterministic gates a spelling from the big generation call goes through
(a trace test + variation_is_over_generic), plus the two the seeder uses: it must
not collide with a DIFFERENT authorized value, and it must not be a form the
compiled query already matches. So this call can never widen a rule in a
way the generation path itself would not have allowed.

Fails OPEN and SILENT: no API key, no network, bad JSON, a slow model — the rule
keeps whatever it already had. A missing spelling is a false flag someone reviews;
a failed contract upload is a customer outage.

Env knobs:
  KAVACHIO_VARIATION_TOPUP        "0" disables this step entirely (default ON)
  KAVACHIO_MIN_VARIATIONS_PER_VALUE   the floor (default 3, shared with the seeder)
  KAVACHIO_VARIATION_TOPUP_MAX_VALUES max values sent in one call (default 25)
"""
from __future__ import annotations

import json
import os
import re

from contract_upload_services.rule_normalizer import (
    _vv_norm,
    _vv_already_matched,
    # The STRICT single-value trace test. The permissive set-level one
    # (variation_traces_to_base) admits anything sharing ONE word with ANY
    # authorized value — here we are asking about ONE value, so "Palms Specialty
    # Insurance Company, Inc." must not be admitted as a spelling of "Demoshield
    # Specialty" on the strength of the shared word "Specialty".
    variation_traces_to_value as _traces_to_value,
    variation_is_over_generic,
    _MIN_VARIATIONS_PER_VALUE,
    _VARIATION_MAX_WORDS,
)

_SEED = int(os.getenv("KAVACHIO_LLM_SEED", "7"))
_MAX_VALUES = int(os.getenv("KAVACHIO_VARIATION_TOPUP_MAX_VALUES", "25"))
_ENUM_TEMPLATES = ("value_in_set", "value_not_in_set")


def _on() -> bool:
    return os.getenv("KAVACHIO_VARIATION_TOPUP", "1") != "0"


def _prompt(field, values, minimum) -> str:
    return (
        "You are given the values a reinsurance contract authorizes for one "
        "bordereau column. For EACH value, list the other ways a real bordereau "
        "could spell THAT SAME value.\n\n"
        f"Column: {field or '(unknown)'}\n"
        f"Values: {json.dumps(list(values), ensure_ascii=False)}\n\n"
        "Return STRICT JSON only, no prose:\n"
        '  {"variations": {"<value exactly as given>": ["<spelling>", ...], ...}}\n\n'
        f"RULES:\n"
        f"- Up to {minimum} spellings per value; FEWER IS CORRECT when a value has "
        "no other real spelling. NEVER pad the list to reach a count.\n"
        "- Every spelling must name the SAME entity/thing as its value — an "
        "abbreviation, a dropped legal-form suffix, an accepted short form, a "
        "hyphenation or spacing variant a bordereau would actually hold.\n"
        "- Each spelling MUST keep the word that tells this value apart from the "
        "others listed. Never emit a bare word shared by two of the values, and "
        "never a bare corporate-form word (Inc, Ltd, LLC, Company, Group…).\n"
        "- NEVER invent an unrelated value, and never give a spelling of one value "
        "under a different value.\n"
        "- Case-only or punctuation-only rewrites are pointless (they already "
        "match) — do not return them.\n"
        "- Prefer an empty list over a wrong spelling: a wrong one silently "
        "disables the check."
    )


def _parse(raw, values) -> dict:
    """Parse the model's JSON into {value: [spellings]}, keyed back to the EXACT
    values we asked about (closed-list guard on the KEYS — the model cannot invent
    a value, only propose spellings for one it was given)."""
    if not raw:
        return {}
    txt = re.sub(r"\s*```$", "", re.sub(r"^```[a-zA-Z]*\s*", "", str(raw).strip())).strip()
    try:
        obj = json.loads(txt)
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    got = obj.get("variations") if isinstance(obj.get("variations"), dict) else obj
    if not isinstance(got, dict):
        return {}
    by_norm = {_vv_norm(v): v for v in values}
    out = {}
    for k, lst in got.items():
        hit = by_norm.get(_vv_norm(k))
        if hit is None:
            continue
        items = lst if isinstance(lst, list) else [lst]
        out.setdefault(hit, []).extend(
            str(x) for x in items if str(x).strip())
    return out


def _values_needing_topup(base, current, minimum) -> list:
    """The authorized values that still carry fewer than `minimum` spellings of
    their own — the same per-value accounting the deterministic seeder does."""
    need = []
    for value in base:
        own = [v for v in current
               if _vv_norm(v) != _vv_norm(value)
               and _traces_to_value(v, value, base)]
        if len(own) < minimum:
            need.append(value)
    return need


def _batch_prompt(requests, minimum) -> str:
    """One prompt covering EVERY column that still needs spellings.

    Same question as _prompt, asked once per column instead of once per call. The
    per-column keying is what keeps it safe: the answer is nested under the column
    name, so a spelling proposed for one column can never be admitted onto another
    (see _parse_batch, which keys both levels back to what we asked)."""
    cols = {r["field"] or "(unknown)": list(r["values"]) for r in requests}
    return (
        "You are given the values a reinsurance contract authorizes for several "
        "bordereau columns. For EACH value, list the other ways a real bordereau "
        "could spell THAT SAME value.\n\n"
        f"Columns and their values:\n{json.dumps(cols, ensure_ascii=False, indent=2)}\n\n"
        "Return STRICT JSON only, no prose:\n"
        '  {"variations": {"<column exactly as given>": '
        '{"<value exactly as given>": ["<spelling>", ...]}}}\n\n'
        f"RULES:\n"
        f"- Up to {minimum} spellings per value; FEWER IS CORRECT when a value has "
        "no other real spelling. NEVER pad the list to reach a count.\n"
        "- Every spelling must name the SAME entity/thing as its value — an "
        "abbreviation, a dropped legal-form suffix, an accepted short form, a "
        "hyphenation or spacing variant a bordereau would actually hold.\n"
        "- Each spelling MUST keep the word that tells this value apart from the "
        "others listed FOR ITS OWN COLUMN. Never emit a bare word shared by two of "
        "them, and never a bare corporate-form word (Inc, Ltd, LLC, Company, Group…).\n"
        "- NEVER invent an unrelated value, never give a spelling of one value under "
        "a different value, and NEVER move a spelling between columns — each column "
        "is a separate question that happens to be asked in the same message.\n"
        "- Case-only or punctuation-only rewrites are pointless (they already "
        "match) — do not return them.\n"
        "- Prefer an empty list over a wrong spelling: a wrong one silently "
        "disables the check."
    )


def _parse_batch(raw, requests) -> dict:
    """Parse the batched answer into {(field_norm, value_norm): [spellings]}.

    Closed-list guard on BOTH levels — the model can neither invent a column nor
    invent a value, it can only propose spellings for a (column, value) it was
    given. That is what makes one call as safe as N calls."""
    if not raw:
        return {}
    txt = re.sub(r"\s*```$", "", re.sub(r"^```[a-zA-Z]*\s*", "", str(raw).strip())).strip()
    try:
        obj = json.loads(txt)
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    got = obj.get("variations") if isinstance(obj.get("variations"), dict) else obj
    if not isinstance(got, dict):
        return {}

    by_field = {_vv_norm(r["field"] or "(unknown)"): r for r in requests}
    memo = {}
    for fk, values_obj in got.items():
        req = by_field.get(_vv_norm(fk))
        if req is None or not isinstance(values_obj, dict):
            continue                     # a column we did not ask about
        by_val = {_vv_norm(v): v for v in req["values"]}
        for vk, lst in values_obj.items():
            real = by_val.get(_vv_norm(vk))
            if real is None:
                continue                 # a value we did not ask about
            items = lst if isinstance(lst, list) else [lst]
            memo.setdefault((_vv_norm(req["field"] or ""), _vv_norm(real)), []).extend(
                str(x) for x in items if str(x).strip())
    return memo


def _wanted_for(template, params, minimum):
    """The values one enum rule still needs spellings for. Pure — no mutation."""
    base = list(params.get("allowed") or []) if template == "value_in_set" \
        else list(params.get("excluded") or [])
    base = [b for b in base if str(b).strip()]
    if not base:
        return []
    current = list(params.get("variation_values") or [])
    short_enough = [b for b in base if len(str(b).split()) <= _VARIATION_MAX_WORDS]
    return _values_needing_topup(short_enough, current, minimum)[:_MAX_VALUES]


def prefill_variation_topups(synth_outputs, *, ai=None, minimum=None):
    """ONE model call for every enum rule's missing spellings, instead of one each.

    Returns a memo {(field_norm, value_norm): [spelling, ...]} for
    topup_variation_values(memo=...) to read, or None when the batch could not be
    made — in which case the caller passes memo=None and every rule falls back to
    its own call, i.e. exactly today's behaviour.

    WHY A PRE-PASS AND NOT A POST-PASS. The ordering inside normalize_ir_outputs is
    load-bearing: the top-up must land BETWEEN the deterministic seeder and the
    vocabulary/abbreviation seeding that follows it. So we move the NETWORK CALL
    earlier, not the point at which the spellings are applied — each rule still
    consumes its answer at exactly the position it does today.

    Asking before the seeder runs is safe in the only direction that matters: the
    seeder only ADDS spellings, so the set of values still short BEFORE it is a
    superset of the set still short after. The memo therefore always carries at
    least what each rule ends up asking for; anything extra is simply unused.
    """
    if not _on():
        return None
    minimum = _MIN_VARIATIONS_PER_VALUE if minimum is None else minimum
    if minimum <= 0:
        return None

    requests, seen = [], set()
    for entry in (synth_outputs or []):
        for ir in (entry.get("candidates") or []):
            if not isinstance(ir, dict) or ir.get("template") not in _ENUM_TEMPLATES:
                continue
            params = ir.get("params") or {}
            field = params.get("field")
            wanted = _wanted_for(ir["template"], params, minimum)
            if not wanted:
                continue
            key = _vv_norm(field or "")
            merged = next((r for r in requests if _vv_norm(r["field"] or "") == key), None)
            if merged is None:
                requests.append({"field": field, "values": list(wanted)})
            else:
                # Two rules on the SAME column — ask once for the union.
                for v in wanted:
                    if _vv_norm(v) not in {_vv_norm(x) for x in merged["values"]}:
                        merged["values"].append(v)
            seen.add(key)

    if not requests:
        return {}

    if ai is None:
        try:
            from contract_upload_services.gemini_service import call_gemini as ai
        except Exception:
            return None

    n_vals = sum(len(r["values"]) for r in requests)
    print(f"[VARIATION-TOPUP] batching {n_vals} value(s) across {len(requests)} "
          f"column(s) into ONE call.")
    try:
        raw = ai(_batch_prompt(requests, minimum),
                 label="VarValuesTopupBatch", temperature=0, seed=_SEED,
                 # Sized for every column at once, not one. Still no thinking: the
                 # question is a lookup, and thinking comes out of this same budget.
                 max_output_tokens=int(os.getenv("KAVACHIO_VARIATION_BATCH_TOKENS",
                                                 "16384")),
                 thinking_budget=0)
    except Exception as exc:
        print(f"[VARIATION-TOPUP] batch skipped: {exc}")
        return None                       # → per-rule fallback, today's behaviour

    memo = _parse_batch(raw, requests)
    if not memo:
        print("[VARIATION-TOPUP] batch returned nothing usable; "
              "falling back to per-rule calls.")
        return None
    return memo


def topup_variation_values(template, params, *, ai=None, minimum=None, memo=None) -> dict:
    """Fill an enum rule's `variation_values` up to the floor using the model, for
    the values the deterministic seeder could not cover. Mutates and returns
    `params`; on ANY failure the params come back untouched.

    `ai` is the model callable (defaults to gemini_service.call_gemini) — inject a
    fake in tests so nothing goes over the network."""
    if not _on() or template not in _ENUM_TEMPLATES:
        return params
    minimum = _MIN_VARIATIONS_PER_VALUE if minimum is None else minimum
    if minimum <= 0:
        return params

    base = list(params.get("allowed") or []) if template == "value_in_set" \
        else list(params.get("excluded") or [])
    base = [b for b in base if str(b).strip()]
    current = list(params.get("variation_values") or [])
    if not base:
        return params

    # Clause-length values have no surface spellings worth asking about — the same
    # cut the seeder makes, for the same reason (see _VARIATION_MAX_WORDS).
    short_enough = [b for b in base if len(str(b).split()) <= _VARIATION_MAX_WORDS]
    wanted = _values_needing_topup(short_enough, current, minimum)[:_MAX_VALUES]
    if not wanted:
        return params

    if ai is None:
        try:
            from contract_upload_services.gemini_service import call_gemini as ai
        except Exception:
            return params
    # A memo from prefill_variation_topups means the ONE batched call already
    # asked this question for every rule; read the answer instead of asking again.
    # The admit gates below are untouched — a batched spelling clears exactly the
    # same bar as one from a per-rule call. memo=None (batch disabled or failed)
    # falls through to the per-rule call, i.e. the original behaviour.
    if memo is not None:
        _fk = _vv_norm(params.get("field") or "")
        proposed = {}
        for v in wanted:
            got = memo.get((_fk, _vv_norm(v)))
            if got:
                proposed[v] = list(got)
        return _admit_proposals(params, proposed, base, current, minimum)

    try:
        raw = ai(_prompt(params.get("field"), wanted, minimum),
                 label="VarValuesTopup", temperature=0, seed=_SEED,
                 max_output_tokens=2048,
                 # No thinking: this is a LOOKUP ("other ways a bordereau spells
                 # Guam"), not a reasoning task. Thinking is drawn from the same
                 # 2,048 budget as the answer, and it was routinely spending
                 # 1,900+ of it — starving a ~60-token answer and failing the call
                 # with MAX_TOKENS on ~3 rules per contract. Same reason Call 2
                 # runs with thinking disabled.
                 thinking_budget=0)
    except Exception as exc:                     # network, quota, missing key…
        print(f"[VARIATION-TOPUP] field={params.get('field')!r} | skipped: {exc}")
        return params

    proposed = _parse(raw, wanted)
    return _admit_proposals(params, proposed, base, current, minimum)


def _admit_proposals(params, proposed, base, current, minimum) -> dict:
    """Put proposed spellings through the deterministic gates and keep the ones
    that clear them. Shared by the per-rule and the batched path so both admit on
    IDENTICAL terms — batching changes where the answer comes from, never what is
    allowed in."""
    if not proposed:
        return params

    out, added = list(current), []
    for value, spellings in proposed.items():
        own = [v for v in out
               if _vv_norm(v) != _vv_norm(value)
               and _traces_to_value(v, value, base)]
        room = minimum - len(own)
        others = [b for b in base if _vv_norm(b) != _vv_norm(value)]
        accepted = [value] + own
        for cand in spellings:
            if room <= 0:
                break
            nc = _vv_norm(cand)
            if not nc or any(nc == _vv_norm(x) for x in out) or nc in {_vv_norm(b) for b in base}:
                continue                            # already carried
            if not _traces_to_value(cand, value, base):
                continue                            # not a spelling of THIS value
            is_generic, _reason = variation_is_over_generic(cand, base)
            if is_generic:
                continue                            # same bar every other spelling clears
            if others and _vv_already_matched(cand, others):
                continue                            # would confuse two authorized values
            if _vv_already_matched(cand, accepted):
                continue                            # the query already matches it
            out.append(cand)
            accepted.append(cand)
            added.append(cand)
            room -= 1

    if added:
        params["variation_values"] = out
        print(f"[VARIATION-TOPUP] field={params.get('field')!r} | AI added {added}")
    return params
