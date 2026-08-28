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


def topup_variation_values(template, params, *, ai=None, minimum=None) -> dict:
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
    try:
        raw = ai(_prompt(params.get("field"), wanted, minimum),
                 label="VarValuesTopup", temperature=0, seed=_SEED,
                 max_output_tokens=2048)
    except Exception as exc:                     # network, quota, missing key…
        print(f"[VARIATION-TOPUP] field={params.get('field')!r} | skipped: {exc}")
        return params

    proposed = _parse(raw, wanted)
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
