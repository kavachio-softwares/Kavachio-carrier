"""
variation_admit.py
──────────────────
Decide whether a spelling a tenant_admin typed may be added to a validation
rule's accepted surface forms (`variation_values`).

WHY THIS IS NOT JUST AN LLM CALL
--------------------------------
A wrong acceptance is the worst failure this product has, because it is SILENT.
Widen `value_in_set` with a value that is really a different carrier and the rule
stops flagging that carrier — for ever, on every future bordereau, with no error
anywhere. Nobody finds out. So the model never decides alone:

    deterministic gates  ──►  the LLM  ──►  accepted
         (G1..G5)              (G6)

Every deterministic gate runs FIRST and short-circuits the model (no tokens spent
on a spelling that is already covered, or that provably names something else).
The model only ever gets to say NO to something the deterministic gates already
said yes to. It can never say yes to something they rejected.

G4/G5 are not politeness. They are the SAME tests contract generation applies
(rule_normalizer.normalize_variation_values), so a spelling admitted here that
they would reject would silently vanish the next time the contract is
regenerated — the admin's correction would disappear with no message.

Mirrors variation_reconcile.py in shape: lazy gemini import, local seed, fail
closed, closed-list guard.
"""
from __future__ import annotations

import difflib
import json
import os
import re

from contract_upload_services.rule_normalizer import (
    variation_traces_to_base,
    variation_is_over_generic,
    acronym_matches,
    _conditional_target_traces_to_value,
)

# Same env var the rest of the pipeline reads, on purpose: one knob for
# determinism across the product (see gemini_service.DETERMINISTIC_SEED).
_SEED = int(os.getenv("KAVACHIO_LLM_SEED", "7"))
_MAX_OUT = int(os.getenv("KAVACHIO_VARIATION_ADMIT_MAX_OUTPUT", "512"))
_MAX_LEN = int(os.getenv("KAVACHIO_VARIATION_ADMIT_MAX_LEN", "120"))

_ENUM_TEMPLATES = ("value_in_set", "value_not_in_set")
_COND_TEMPLATES = ("conditional_value", "conditional_all")

# Shortest normalized value whose SUBSTRING relationship still means anything.
# Below this, "contains" is noise: a Yes/No column has values 'Y' and 'N', and
# every string on earth contains an 'n'.
_MIN_TRACE_LEN = int(os.getenv("KAVACHIO_VARIATION_MIN_TRACE_LEN", "4"))

# How many words a candidate may add on top of a listed value it fully contains.
# A bordereau CODE for a category adds one qualifier ("New" -> "NEW_BUSINESS").
# Two or more added words is how a longer NAME is built, and a longer name is a
# different legal entity ("Everest Re" -> "Everest Re Holdings Group"), which is
# the silent wrong acceptance this module exists to prevent.
_MAX_EXTRA_WORDS = int(os.getenv("KAVACHIO_VARIATION_MAX_EXTRA_WORDS", "1"))


def require_ai() -> bool:
    """The LLM gate is mandatory unless explicitly disabled. Set
    KAVACHIO_VARIATION_ADMIT_REQUIRE_AI=0 only for offline tests — with it off,
    a spelling is admitted on the deterministic gates alone."""
    return os.getenv("KAVACHIO_VARIATION_ADMIT_REQUIRE_AI", "1") != "0"


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _words(s) -> list:
    return re.findall(r"[a-z0-9]+", str(s).lower())


def _quote(clause_text, limit: int = 240) -> str | None:
    """A short, readable piece of the clause to show beside a rejection, so the
    admin sees the contract's own words rather than a bare 'no'."""
    if not clause_text:
        return None
    txt = " ".join(str(clause_text).split())
    return txt if len(txt) <= limit else txt[:limit].rsplit(" ", 1)[0] + "…"


def _best_base_match(spelling, base) -> str | None:
    """Which contract value this spelling is most plausibly another form of —
    used as the vocabulary's canonical. Word overlap first (a real variation
    shares a distinguishing word), similarity as the tie-break."""
    sw, sn = set(_words(spelling)), _norm(spelling)
    best, best_score = None, -1.0
    for b in base:
        bw, bn = set(_words(b)), _norm(b)
        if not bn:
            continue
        overlap = len(sw & bw)
        ratio = difflib.SequenceMatcher(None, sn, bn).ratio()
        contained = 1.0 if (sn and (sn in bn or bn in sn)) else 0.0
        score = overlap * 2 + contained + ratio
        if score > best_score:
            best, best_score = b, score
    return best


def _traces(spelling, base, *, conditional: bool) -> bool:
    """Does `spelling` plausibly name one of the values the contract lists?

    Same two ideas as rule_normalizer.variation_traces_to_base — share a word, or
    be a glued form of the value — but the SUBSTRING half only counts when both
    sides are at least _MIN_TRACE_LEN characters.

    That guard is not theoretical. A Yes/No column's values are ['Yes','No','Y',
    'N','1','0']; 'N' is a substring of "Zurich Bermuda Holdi(n)gs", so without
    the length floor an unrelated carrier traces to a Yes/No rule and the model
    becomes the only thing standing between a typo and a disabled check. Verified
    against live rule 11295.

    The generation-time function keeps its permissive form on purpose (its
    docstring records the ~250 legitimate variations a stricter test dropped); it
    is fed AI-proposed spellings for one rule at a time, not free text from a
    person. This path takes free text, so it carries the floor.
    """
    sw, sn = set(_words(spelling)), _norm(spelling)
    if not sw:
        return False
    # An initialism of exactly one listed value. Shares no word and no fragment
    # with it, so neither test below can see it, but the letters are checkable.
    if len(acronym_matches(spelling, base)) == 1:
        return True
    for b in base:
        bw, bn = set(_words(b)), _norm(b)
        if not bn:
            continue
        if conditional:
            # One target value: a real variation keeps the value's own head word.
            first = _words(spelling)[0]
            if first in bw:
                return True
        elif sw & bw:
            return True
        if (len(sn) >= _MIN_TRACE_LEN and len(bn) >= _MIN_TRACE_LEN
                and (sn in bn or bn in sn)):
            return True
    return False


def _dictionary_context(field, base, candidates, limit: int = 8) -> str:
    """Equivalences the SHARED DICTIONARY already knows, as prompt context.

    Without this the model is asked to judge "CGL US Surplus Lines Policies"
    against "Commercial General Liability US Surplus Lines Policies" while blind
    to the fact that this very system records cgl = commercial general liability.
    It then refuses a variation the product itself already treats as equivalent —
    the model being more ignorant than the database behind it.

    Only entries that actually touch this rule's values or the proposed
    variations are included, so an unhinted column cannot drag the whole
    vocabulary into the prompt.
    """
    try:
        from contract_upload_services import vocabulary
    except Exception:
        return ""
    haystack = " ".join(_norm(x) for x in [*base, *candidates])
    lines = []
    try:
        for cls in vocabulary._classes_for(field):
            for canonical, syns in (vocabulary.VOCAB.get(cls) or {}).items():
                forms = [canonical, *(syns or [])]
                if not syns:
                    continue
                if not any(_norm(f) and _norm(f) in haystack for f in forms):
                    continue
                lines.append("  " + " = ".join(forms))
                if len(lines) >= limit:
                    raise StopIteration
    except StopIteration:
        pass
    except Exception:
        return ""
    if not lines:
        return ""
    return ("KNOWN EQUIVALENT TERMS (this system already treats these as the same "
            "term, so a variation differing only by one of these is the SAME "
            "entity):\n" + "\n".join(lines) + "\n")


def _acronym_context(base, candidates) -> str:
    """Initialism matches this system computed LETTER BY LETTER, as prompt context.

    Without it the model is asked to rule on "SSIC" against "SiriusPoint Specialty
    Insurance Corporation" with nothing to go on: no shared word, no shared
    fragment, and a standing instruction to prefer refusal when unsure. It refuses
    — correctly, given what it was told, and wrongly, given the facts. Whether
    S-S-I-C is the initials of those four words is arithmetic, so the arithmetic is
    done here and handed over as evidence rather than left to the model to guess.

    Only unambiguous matches appear; a candidate matching two values is already
    refused outright by G4 and never reaches this prompt.
    """
    lines = []
    for c in candidates:
        m = acronym_matches(c, base)
        if len(m) == 1:
            lines.append(f"  {json.dumps(c, ensure_ascii=False)} is exactly the "
                         f"initials of {json.dumps(m[0], ensure_ascii=False)}")
    if not lines:
        return ""
    return ("COMPUTED INITIALS (checked letter by letter by this system, not "
            "guessed — and matching no other listed value, so it is not "
            "ambiguous):\n" + "\n".join(lines) + "\n")


def _is_code_shaped(s) -> bool:
    """Is this written the way a SYSTEM stores a value, rather than the way a
    person writes a name?

    The discriminator is TYPOGRAPHY, deliberately, because typography is the one
    thing that generalises. A bordereau column stores a category as a token — no
    spaces, separated by underscores or hyphens, or upper-cased. A name is written
    as words with spaces. That holds for any MGA, any line of business and any
    language, whereas naming the words themselves ("business", "holdings") would be
    a baked-in vocabulary that only fits the book of business it was written for.
    """
    t = str(s or "").strip()
    if not t or " " in t:
        return False
    if "_" in t or "-" in t:
        return True
    letters = [c for c in t if c.isalpha()]
    return bool(letters) and t.upper() == t


def _word_evidence(base, candidates) -> str:
    """Word containment this system computed, as prompt context.

    A contract names a CATEGORY in prose ("New", "Renewal"); a bordereau writes the
    same category as a system code ("NEW_BUSINESS", "RENEWAL"). The code is the
    listed value plus a qualifier word, which is neither an abbreviation nor a
    misspelling, so a model given only those examples refuses it — while the
    platform's own exception recommender scores the same pair at 100%. Verified on
    rule 1984, where 43 policies were flagged for exactly this.

    What is computable here is containment, not meaning: whether the candidate
    contains every word of exactly ONE listed value. That is handed over as a fact.
    It is deliberately NOT an acceptance — "New York Corp" also contains every word
    of "New" and is a different thing entirely — so the prompt says so plainly and
    the model still decides.

    Emitted only when exactly one listed value is contained; two or more means the
    candidate cannot identify either, and nothing is claimed.
    """
    lines, coded = [], False
    for c in candidates:
        cw = set(_words(c))
        if not cw:
            continue
        hits = [b for b in base if set(_words(b)) and set(_words(b)) <= cw
                and _norm(b) != _norm(c)]
        if len(hits) == 1:
            extra = [w for w in _words(c) if w not in set(_words(hits[0]))]
            shape = ""
            if _is_code_shaped(c) and not _is_code_shaped(hits[0]):
                # The candidate is a machine token and the listed value is prose:
                # the shape itself says "this is the stored form of that value".
                coded = True
                shape = (" — and is written as a SYSTEM TOKEN (no spaces; "
                         "separator or upper-case) while the listed value is prose")
            lines.append(
                f"  {json.dumps(c, ensure_ascii=False)} = every word of "
                f"{json.dumps(hits[0], ensure_ascii=False)} plus the extra word(s) "
                f"{json.dumps(' '.join(extra), ensure_ascii=False) if extra else 'none'}"
                f"{shape}")
    if not lines:
        return ""
    # Framed as a QUESTION about the extra words, not as support for accepting.
    # Stated as evidence it reads as endorsement, and the model duly accepted a
    # fabricated "<listed value> <nonsense> Holdings Group" on 4 of 10 live rules —
    # the silent wrong acceptance this whole module exists to prevent. What decides
    # the case is not the containment, it is what the EXTRA words are.
    #
    # NO EXAMPLE VALUES. An illustration here would be one tenant's vocabulary
    # taught to every tenant's contracts — the same reason nothing else in this
    # pipeline carries a baked-in word list. The rule is stated by KIND of word,
    # and the only concrete strings in the prompt are the ones computed above from
    # this rule's own values.
    #
    # ONE KIND OF EXTRA WORD IS NOT A DIFFERENT ENTITY: the corporate form. A
    # contract names a carrier in short form ("<carrier> Specialty") where the
    # bordereau writes its registered name in full ("<carrier> Specialty Insurance
    # Company, Inc."). Those extra words ARE an organisation name, so the earlier
    # wording refused them — the same spelling the accept-list already admits in
    # the other direction as "a dropped legal suffix". The carve-out is deliberately
    # narrow: EVERY extra word must belong to the corporate form, and a parent /
    # holding / affiliate designation is called out as a DIFFERENT entity, which is
    # what keeps the "<listed value> <nonsense> Holdings Group" hole above shut.
    out = ("COMPUTED WORD BREAKDOWN (containment only — it proves NOTHING by "
           "itself):\n" + "\n".join(lines) + "\n"
           "Decide on the EXTRA words alone. If they are a generic qualifier that "
           "leaves the meaning unchanged, it is the same value. If EVERY extra "
           "word belongs to this entity's own CORPORATE FORM — the "
           "incorporation/legal-form suffix and the business descriptor that "
           "complete its REGISTERED name — the meaning is unchanged and it is the "
           "SAME value written out in full. But if ANY extra word names a place, a "
           "product line, a parent / holding / affiliate designation, a different "
           "organisation, or any other distinct identifier, it is a DIFFERENT "
           "entity that merely begins with the same words — REJECT those.\n")
    if coded:
        # Typography carries this, not vocabulary: a bordereau column stores a
        # category as a token, and a token whose words are exactly one listed value
        # plus a qualifier IS that value as the column stores it. Said abstractly so
        # it holds for any book of business, rather than by quoting one.
        out += ("A SYSTEM TOKEN is how a bordereau column stores a value the "
                "contract states in prose. When such a token's words are exactly "
                "one listed value plus a qualifier, and no other listed value fits, "
                "it is that value in stored form — ACCEPT it. This does not apply "
                "to spaced, prose-style names, which are ordinary names and must "
                "still be judged as such.\n")
    return out


def _polarity(template: str) -> dict:
    """Wording that flips with the template. value_in_set widens what PASSES;
    value_not_in_set widens what is CAUGHT. Telling an admin "this will be
    accepted" when it actually means "this will now be flagged" is how someone
    does the exact opposite of what they intended."""
    if template == "value_not_in_set":
        return {
            "noun": "prohibited value",
            "set_label": "the values this contract prohibits",
            "effect": "Rows using this spelling will now be flagged.",
            "prompt_verb": "prohibits",
            "prompt_risk": "flags rows that are actually compliant",
            "prompt_label": "PROHIBITED",
        }
    if template in _COND_TEMPLATES:
        return {
            "noun": "required value",
            "set_label": "the value this clause requires",
            "effect": "Rows using this spelling will now satisfy the rule.",
            "prompt_verb": "requires",
            "prompt_risk": "hides a real violation",
            "prompt_label": "REQUIRED",
        }
    return {
        "noun": "authorized value",
        "set_label": "the values this contract authorizes",
        "effect": "Rows using this spelling will now pass the rule.",
        "prompt_verb": "authorizes",
        "prompt_risk": "hides a real violation",
        "prompt_label": "AUTHORIZED",
    }


def _reject(code, reason, clause_quote=None) -> dict:
    return {"accepted": False, "reason_code": code, "reason": reason,
            "clause_quote": clause_quote, "matched_value": None}


def check_many(spellings, *, template, field, base, existing, clause_text=None,
               ai=None) -> list:
    """Admit SEVERAL variations in one pass — one AI call for the whole batch.

    Each candidate is put through the deterministic gates on its own (they are
    free and independent). Only the survivors are sent to the model, together, in
    a single request: a batch of five costs one call, not five.

    Returns a list of {spelling, accepted, reason_code, reason, clause_quote,
    matched_value} in the caller's order. Never raises.
    """
    # Every line the admin typed gets an answer, including repeats — a line that
    # silently vanished would look like it was accepted. A repeat inside one batch
    # is caught by G2 below, because each acceptance joins `running` immediately.
    ordered = [re.sub(r"[\x00-\x1f\x7f]", "", str(s or "").strip())
               for s in (spellings or [])]

    results, pending = [], []
    # Each accepted candidate widens the set the NEXT one is judged against, so a
    # batch behaves exactly like adding them one at a time.
    running = list(existing or [])
    for clean in ordered:
        verdict = _check_deterministic(clean, template=template, field=field,
                                       base=base, existing=running,
                                       clause_text=clause_text)
        results.append(verdict)
        if verdict["accepted"]:
            pending.append((len(results) - 1, clean))
            running.append(clean)

    if pending and not require_ai():
        # Offline: nothing can vouch for a candidate the text checks could not
        # confirm, so it stays refused.
        for idx, clean in pending:
            if results[idx].get("concern"):
                results[idx] = _reject(results[idx].get("concern_code") or "no_trace",
                                       results[idx]["concern_reason"],
                                       _quote(clause_text))
    elif pending:
        pol = _polarity(template)
        concerns = {c: results[i]["concern"] for i, c in pending
                    if results[i].get("concern")}
        verdicts = _ai_admit_batch([c for _i, c in pending], base=base, field=field,
                                   clause_text=clause_text, existing=existing,
                                   pol=pol, ai=ai, concerns=concerns)
        for idx, clean in pending:
            v = verdicts.get(clean)
            quote = _quote(clause_text)
            if v is None or v.get("error"):
                results[idx] = _reject(
                    "checker_unavailable",
                    "The variation checker could not be reached, so nothing was "
                    "changed. Please try again in a moment.")
            elif not v.get("accept"):
                results[idx] = _reject(
                    "ai_rejected",
                    v.get("reason") or f"“{clean}” was judged to be a different "
                                       f"entity from {pol['set_label']}.",
                    v.get("clause_quote") or quote)

    for r, clean in zip(results, ordered):
        r["spelling"] = clean
    return results


def check(spelling, *, template, field, base, existing, clause_text=None,
          ai=None) -> dict:
    """Run the full admission pipeline for ONE variation.

    `base`     — the values the CONTRACT itself names (allowed / excluded /
                 the conditional target). A variation is only ever admitted as
                 another way of writing one of these.
    `existing` — the variations the rule already accepts.

    Returns {accepted, reason_code, reason, clause_quote, matched_value}.
    Never raises: a rejection is a normal outcome, not an error.
    """
    verdict = _check_deterministic(spelling, template=template, field=field,
                                   base=base, existing=existing,
                                   clause_text=clause_text)
    if not verdict["accepted"]:
        return verdict
    if not require_ai():
        # Offline: a candidate the text checks could not confirm has nothing left
        # to vouch for it, so it stays refused.
        if verdict.get("concern"):
            return _reject(verdict.get("concern_code") or "no_trace",
                           verdict["concern_reason"], _quote(clause_text))
        return verdict

    pol = _polarity(template)
    clean = re.sub(r"[\x00-\x1f\x7f]", "", str(spelling or "").strip())
    ai_verdict = _ai_admit(clean, base=base, field=field, clause_text=clause_text,
                           existing=existing, pol=pol, ai=ai,
                           concerns={clean: verdict["concern"]} if verdict.get("concern") else None)
    if ai_verdict.get("error"):
        return _reject("checker_unavailable",
                       "The variation checker could not be reached, so nothing "
                       "was changed. Please try again in a moment.")
    if not ai_verdict.get("accept"):
        return _reject("ai_rejected",
                       ai_verdict.get("reason")
                       or f"“{clean}” was judged to be a different entity from "
                          f"{pol['set_label']}.",
                       ai_verdict.get("clause_quote") or _quote(clause_text))
    return verdict


def _check_deterministic(spelling, *, template, field, base, existing,
                         clause_text=None) -> dict:
    """Gates G1–G5 only — free, instant, and no model. Every one of these
    short-circuits the AI, so a variation that is already covered or provably
    names something else never costs a token."""
    pol = _polarity(template)
    quote = _quote(clause_text)
    base = [b for b in (base or []) if str(b or "").strip()]
    existing = [e for e in (existing or []) if str(e or "").strip()]

    # ── G1 — shape ───────────────────────────────────────────────────────
    clean = str(spelling or "").strip()
    clean = re.sub(r"[\x00-\x1f\x7f]", "", clean)
    if not clean:
        return _reject("empty", "Enter a spelling to add.")
    if len(clean) > _MAX_LEN:
        return _reject("too_long",
                       f"That is longer than {_MAX_LEN} characters. A spelling is "
                       f"a way of writing one value, not a sentence.")
    if not _norm(clean):
        return _reject("empty", "A spelling needs at least one letter or digit.")

    if not base:
        return _reject("no_trace",
                       "This rule does not name any contract value to compare "
                       "against, so a spelling cannot be checked.")

    known = [*base, *existing]

    # ── G2 — already there, exactly ──────────────────────────────────────
    for k in known:
        if _norm(k) == _norm(clean):
            return _reject("duplicate",
                           f"“{k}” is already in the accepted list — punctuation "
                           f"and capitals are ignored when matching, so this "
                           f"spelling already works.")

    # ── G3 — the shared vocabulary already collapses the two ─────────────
    # If both sides canonicalize to the same token, the compiled SQL would come
    # out byte-identical. Reporting success there would be a lie.
    try:
        from contract_upload_services import vocabulary
        ct = vocabulary.canonical_token(clean, field)
        for k in known:
            if ct and vocabulary.canonical_token(k, field) == ct:
                return _reject("vocab_equivalent",
                               f"The rule already accepts this. “{clean}” is in the "
                               f"shared dictionary as another way of writing "
                               f"“{k}”, so it is matched today without being "
                               f"stored on the rule — you will see it listed above "
                               f"with a dashed outline. Adding it would produce "
                               f"exactly the same check.")
    except Exception:
        pass                        # vocabulary unavailable — fall through to G4

    # ── G4 — does it trace back to something the contract names? ─────────
    # An initialism that fits TWO listed values identifies neither, and admitting
    # it would leave the rule permanently unable to tell them apart. Like
    # ambiguous_stem below this is a fact, not a judgement, so no model is asked.
    amb = acronym_matches(clean, base)
    if len(amb) > 1:
        return _reject(
            "ambiguous_acronym",
            f"“{clean}” is the initialism of more than one of these values "
            f"({', '.join(f'“{a}”' for a in amb[:4])}), so it cannot identify "
            f"which one a row means.", quote)

    # Both tests must agree: the generation-time one (so an admitted spelling
    # survives the next regeneration) AND the length-floored one above (so a
    # one-character contract value cannot wave anything through).
    if template in _COND_TEMPLATES:
        traces = (any(_conditional_target_traces_to_value(clean, b) for b in base)
                  and _traces(clean, base, conditional=True))
    else:
        traces = (variation_traces_to_base(clean, base)
                  and _traces(clean, base, conditional=False))
    # NOT a refusal. A string test cannot tell an initialism ("PICL" for "Palms
    # Insurance Company, Limited") from an unrelated party — only meaning can. So
    # this becomes a WARNING carried into the model's prompt, and the model
    # decides. With the model unavailable it stays a refusal (fail closed).
    concern = concern_reason = concern_code = None
    if not traces:
        concern_code = "no_trace"
        concern = ("shares no word or fragment with any listed value — it may be "
                   "an initialism or acronym, or it may be an unrelated entity")
        concern_reason = (
            f"“{clean}” shares no part of its name with {pol['set_label']} "
            f"({', '.join(f'“{b}”' for b in base[:4])}"
            f"{', …' if len(base) > 4 else ''}), so it could not be confirmed "
            f"against the contract.")

    # ── G4b — a listed value with extra words bolted onto it ─────────────
    # A candidate containing every word of a listed value PLUS more is a superset,
    # and a superset is where a different entity hides: "Everest Re Holdings Group"
    # is a different company from "Everest Re".
    #
    # This cannot be settled by counting. Live data carries plenty of legitimate
    # supersets — "Commonwealth of the Northern Mariana Islands" for "Northern
    # Mariana Islands", "Sabal Specialty Insurance Company" for "Sabal Specialty",
    # "crypto currency mining" for an excluded "Mining" — and they are structurally
    # identical to the dangerous ones. A hard refusal here was tried and would have
    # rejected all of those, so it is a WARNING carried into the prompt instead.
    #
    # One extra word raises nothing, because that is how a category becomes a
    # bordereau code ("New" -> "NEW_BUSINESS").
    if not concern:
        for b in base:
            bw, cw = set(_words(b)), set(_words(clean))
            if not bw or not bw <= cw or _norm(b) == _norm(clean):
                continue
            extra = [w for w in _words(clean) if w not in bw]
            if len(extra) > _MAX_EXTRA_WORDS:
                concern_code = "superset_name"
                concern = (f"is “{b}” with “{' '.join(extra)}” added — extra words "
                           f"can build a LONGER, DIFFERENT name (a parent, an "
                           f"affiliate, another entity), or can simply complete "
                           f"this one's REGISTERED name; decide which")
                concern_reason = (
                    f"“{clean}” is “{b}” with extra words added, which could not be "
                    f"confirmed as the same entity.")
                break

    # ── G5 — too generic to identify one value ───────────────────────────
    generic, why = variation_is_over_generic(clean, base)
    if generic and why == "ambiguous_stem":
        # The ONE hard refusal left, because it is a fact rather than a judgement:
        # the text matches two or more of the contract's OWN values, so accepting
        # it would make the rule unable to tell them apart. No model can fix that.
        return _reject(
            "ambiguous_stem",
            f"“{clean}” is made only of words that several of these values share "
            f"({', '.join(f'“{b}”' for b in base[:4])}), so it cannot identify "
            f"any one of them.", quote)
    if generic and not concern:
        concern_code = why or "over_generic"
        concern = {
            "legal_form_token": "is only a company-form word on its own "
                                "(Ltd / Inc / Company), which countless unrelated "
                                "companies share",
            "short_prefix": "is a short truncation of a longer listed value",
        }.get(why, "looked too generic to a text-only check")
        concern_reason = (f"“{clean}” looked too generic to confirm against the "
                          f"contract on text alone.")

    # Deterministic work is done. The model has the final say — applied by the
    # caller: check() for one variation, check_many() for a batch in one request.
    return {"accepted": True, "reason_code": "accepted", "reason": None,
            "clause_quote": None, "matched_value": _best_base_match(clean, base),
            "concern": concern, "concern_reason": concern_reason,
            "concern_code": concern_code}


def _ai_admit(spelling, *, base, field, clause_text, existing, pol, ai=None,
              concerns=None) -> dict:
    """Ask the small model whether `spelling` names the same entity as one of
    `base`. Returns {accept, reason, clause_quote} or {error: True}.

    Fails CLOSED — an unreachable or unparseable model is never an acceptance.
    """
    if ai is None:
        # Imported lazily: gemini_service builds its API client at import time,
        # so a module-level import would make every route need a key at boot.
        from contract_upload_services.gemini_service import call_gemini, SMALL_MODEL
        model = SMALL_MODEL
        ai = call_gemini
    else:
        model = None

    prompt = (
        f"You decide whether a proposed spelling names the SAME entity as one of "
        f"the values a contract {pol['prompt_verb']}. Be conservative: a wrong "
        f"acceptance {pol['prompt_risk']}.\n\n"
        f"Column: {field}\n"
        f"{pol['prompt_label']} values (the only correct entities): "
        f"{json.dumps(list(base), ensure_ascii=False)}\n"
        f"ALREADY-ACCEPTED variations: {json.dumps(list(existing)[:20], ensure_ascii=False)}\n"
    )
    if clause_text:
        prompt += (f"CONTRACT CLAUSE these values came from:\n"
                   f"{' '.join(str(clause_text).split())[:1200]}\n")
    prompt += _dictionary_context(field, base, [spelling])
    prompt += _acronym_context(base, [spelling])
    prompt += _word_evidence(base, [spelling])
    prompt += f"\nPROPOSED variation: {json.dumps(spelling, ensure_ascii=False)}\n"
    note = (concerns or {}).get(spelling)
    if note:
        prompt += (f"\nAUTOMATED PRE-CHECK NOTE: this one {note}. A note is NOT a "
                   f"verdict — judge it on meaning. Accept it only if you can name "
                   f"which listed value it stands for and why.\n")
    prompt += (
        "\n"
        "Return STRICT JSON only, no markdown, no commentary:\n"
        '  {"accept": true, "reason": "<one short sentence>"}\n'
        "or, if it is a DIFFERENT entity or you are unsure:\n"
        '  {"accept": false, "reason": "<one short sentence a non-technical '
        'reviewer would understand>", "clause_quote": "<the exact words from the '
        'clause above that rule it out, copied VERBATIM, or null>"}\n\n'
        "Accept ONLY if the proposed variation is the SAME entity OR THE SAME "
        "CATEGORY as one listed value — an abbreviation, a legal-form suffix "
        "DROPPED or ADDED (the same entity written short, or written out in full "
        "as its registered name), "
        "an initialism, an obvious misspelling, or THE SAME VALUE WRITTEN AS A "
        "BORDEREAU/SYSTEM CODE (upper-case with underscores, hyphenated, or the "
        "listed value plus a qualifier word that names the same category: a "
        "contract states a category in prose where a bordereau column stores it as "
        "a code). Reject anything that is a different entity, "
        "that could equally mean two of the listed values, or that you are unsure "
        "about. ALSO reject it when the ALREADY-ACCEPTED list above already covers "
        "it — say which entry covers it — because adding it would change nothing. "
        "Prefer a wrong rejection over a wrong acceptance. Never invent clause "
        "text: quote it word for word or use null."
    )

    kwargs = dict(label="VarValueAdmit", temperature=0, seed=_SEED,
                  max_output_tokens=_MAX_OUT, thinking_budget=0)
    if model:
        kwargs["model"] = model
    try:
        raw = ai(prompt, **kwargs)
    except TypeError:
        # A test double with a narrower signature.
        try:
            raw = ai(prompt)
        except Exception:
            return {"error": True}
    except Exception:
        return {"error": True}

    return _parse_verdict(raw)


def _ai_admit_batch(spellings, *, base, field, clause_text, existing, pol,
                    ai=None, concerns=None) -> dict:
    """Judge SEVERAL candidates in ONE request. Returns {spelling: verdict}.

    One call for a batch instead of one per candidate — the whole reason an admin
    can paste a list. The model judges each independently, and each verdict is
    matched back to the caller's own string: a name the model invents, or one it
    fails to answer for, is never treated as an acceptance.

    Fails CLOSED for every candidate the model did not clearly accept.
    """
    if not spellings:
        return {}
    if len(spellings) == 1:
        one = _ai_admit(spellings[0], base=base, field=field, clause_text=clause_text,
                        existing=existing, pol=pol, ai=ai, concerns=concerns)
        return {spellings[0]: one}

    if ai is None:
        from contract_upload_services.gemini_service import call_gemini, SMALL_MODEL
        model = SMALL_MODEL
        ai = call_gemini
    else:
        model = None

    numbered = "\n".join(f"  {i + 1}. {json.dumps(s, ensure_ascii=False)}"
                         for i, s in enumerate(spellings))
    prompt = (
        f"You decide, for EACH proposed variation below, whether it names the SAME "
        f"entity as one of the values a contract {pol['prompt_verb']}. Be "
        f"conservative: a wrong acceptance {pol['prompt_risk']}.\n\n"
        f"Column: {field}\n"
        f"{pol['prompt_label']} values (the only correct entities): "
        f"{json.dumps(list(base), ensure_ascii=False)}\n"
        f"ALREADY-ACCEPTED variations: {json.dumps(list(existing)[:20], ensure_ascii=False)}\n"
    )
    if clause_text:
        prompt += (f"CONTRACT CLAUSE these values came from:\n"
                   f"{' '.join(str(clause_text).split())[:1200]}\n")
    prompt += _dictionary_context(field, base, spellings)
    prompt += _acronym_context(base, spellings)
    prompt += _word_evidence(base, spellings)
    prompt += f"\nPROPOSED variations:\n{numbered}\n"
    notes = [(i + 1, s_, (concerns or {}).get(s_)) for i, s_ in enumerate(spellings)
             if (concerns or {}).get(s_)]
    if notes:
        prompt += ("\nAUTOMATED PRE-CHECK NOTES (a note is NOT a verdict — judge on "
                   "meaning, and accept only if you can name which listed value it "
                   "stands for and why):\n")
        for n, s_, why in notes:
            prompt += f"  {n}. {json.dumps(s_, ensure_ascii=False)} — {why}\n"
    prompt += (
        "\n"
        "Return STRICT JSON only, no markdown, no commentary — one entry per "
        "proposed variation, in the same order:\n"
        '  {"verdicts": [{"variation": "<copied EXACTLY from the list above>", '
        '"accept": true|false, "reason": "<one short sentence a non-technical '
        'reviewer would understand>", "clause_quote": "<the exact words from the '
        'clause that rule it out, copied VERBATIM, or null>"}]}\n\n'
        "Accept a variation ONLY if it is the SAME entity OR THE SAME CATEGORY as "
        "one listed value — an abbreviation, a legal-form suffix DROPPED or ADDED "
        "(the same entity written short, or written out in full as its registered "
        "name), an initialism, "
        "an obvious misspelling, or THE SAME VALUE WRITTEN AS A BORDEREAU/SYSTEM "
        "CODE (upper-case with underscores, hyphenated, or the listed value plus a "
        "qualifier word that names the same category: a contract states a category "
        "in prose where a bordereau column stores it as a code). "
        "Reject anything that is a different entity, that could "
        "equally mean two of the listed values, or that you are unsure about. "
        "ALSO reject a variation the ALREADY-ACCEPTED list above already covers — "
        "name the entry that covers it — because adding it would change nothing. "
        "Judge each one INDEPENDENTLY. Prefer a wrong rejection over a wrong "
        "acceptance. Never invent clause text: quote it word for word or use null."
    )

    kwargs = dict(label="VarValueAdmitBatch", temperature=0, seed=_SEED,
                  max_output_tokens=min(_MAX_OUT * max(len(spellings), 1), 4096),
                  thinking_budget=0)
    if model:
        kwargs["model"] = model
    try:
        raw = ai(prompt, **kwargs)
    except TypeError:
        try:
            raw = ai(prompt)
        except Exception:
            return {s: {"error": True} for s in spellings}
    except Exception:
        return {s: {"error": True} for s in spellings}

    return _parse_batch(raw, spellings)


def _parse_batch(raw, spellings) -> dict:
    """Map the model's verdicts back onto the caller's own strings.

    Anything unparseable, unmatched, or missing becomes an error for that
    candidate — never an acceptance."""
    out = {s: {"error": True} for s in spellings}
    if not raw:
        return out
    txt = str(raw).strip()
    txt = re.sub(r"^```(?:json)?\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt).strip()
    if not txt.startswith("{"):
        i, j = txt.find("{"), txt.rfind("}")
        if i < 0 or j <= i:
            return out
        txt = txt[i:j + 1]
    try:
        data = json.loads(txt)
    except Exception:
        return out
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(verdicts, list):
        return out

    by_norm = {_norm(s): s for s in spellings}
    for i, v in enumerate(verdicts):
        if not isinstance(v, dict) or "accept" not in v:
            continue
        named = v.get("variation") or v.get("spelling")
        target = by_norm.get(_norm(named)) if named else None
        if target is None and i < len(spellings):
            target = spellings[i]          # positional fallback, same order asked
        if target is None:
            continue
        reason, quote = v.get("reason"), v.get("clause_quote")
        out[target] = {
            "accept": bool(v.get("accept")),
            "reason": str(reason).strip()[:400] if isinstance(reason, str) else None,
            "clause_quote": (str(quote).strip()[:400]
                             if isinstance(quote, str) and quote.strip() else None),
        }
    return out


def _parse_verdict(raw) -> dict:
    """Parse the model's JSON defensively. Anything unparseable is an ERROR, not
    an acceptance — and never read the spelling itself back out of the response
    (the value applied is always the caller's own string)."""
    if not raw:
        return {"error": True}
    txt = str(raw).strip()
    txt = re.sub(r"^```(?:json)?\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt).strip()
    if not txt.startswith("{"):
        i, j = txt.find("{"), txt.rfind("}")
        if i < 0 or j <= i:
            return {"error": True}
        txt = txt[i:j + 1]
    try:
        data = json.loads(txt)
    except Exception:
        return {"error": True}
    if not isinstance(data, dict) or "accept" not in data:
        return {"error": True}

    reason = data.get("reason")
    quote = data.get("clause_quote")
    return {
        "accept": bool(data.get("accept")),
        "reason": str(reason).strip()[:400] if isinstance(reason, str) else None,
        "clause_quote": str(quote).strip()[:400] if isinstance(quote, str) and quote.strip() else None,
    }
