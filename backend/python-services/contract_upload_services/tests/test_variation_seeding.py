"""
test_variation_seeding.py
─────────────────────────
The "every authorized value carries at least 3 surface spellings" floor.

The prompt has always ASKED for it; nothing enforced it, so 45% of the enum rules
on the live system shipped matching only the contract's own wording. These tests
cover the two layers that now enforce it and — more importantly — the things they
must REFUSE to seed, because a wrong spelling silently disables a check:

  • rule_normalizer.seed_structural_variations  (deterministic, offline)
  • variation_topup.topup_variation_values      (model, gated + fail-open)

Run standalone:  python -m contract_upload_services.tests.test_variation_seeding
(or under pytest).
"""

import os

from contract_upload_services.rule_normalizer import (
    flatten_variation_values,
    normalize_variation_values,
    seed_structural_variations,
    structural_variation_forms,
)
from contract_upload_services.variation_topup import topup_variation_values


def _norm(s):
    import re
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _has(forms, want):
    return any(_norm(f) == _norm(want) for f in forms)


# ── the deterministic seeder ────────────────────────────────────────────────

def test_seeds_short_forms_for_a_legal_name():
    """A carrier name shortens from the right: the legal-form tail comes off.

    Every one of those shortenings is now worth seeding. This used to expect
    "Palms Specialty Insurance Company" to be SKIPPED — it scores 0.98 against the
    full name, so the old scored matcher already treated the two as one value. The
    matcher is exact now (see rule_compiler), so a spelling the rule does not carry
    is a spelling the rule does not match, and dropping it would be the bug."""
    base = ["Palms Specialty Insurance Company, Inc."]
    out = seed_structural_variations(base, list(base))
    assert _has(out, "Palms Specialty Insurance Company")
    assert _has(out, "Palms Specialty")
    for v in base:                                  # values are never lost
        assert _has(out, v)


def test_the_acronym_is_still_derivable_for_a_legal_name():
    """The acronym is one of the forms the seeder can derive; it just is not
    needed to reach the floor of 3 when the shortenings already do."""
    forms = structural_variation_forms("Palms Specialty Insurance Company, Inc.",
                                       ["Palms Specialty Insurance Company, Inc."])
    assert _has(forms, "PSICI")


def test_never_seeds_a_bare_corporate_form_or_ambiguous_stem():
    """The two shapes that would let a WRONG carrier pass an allow-list."""
    base = ["Palms Insurance Company, Limited",
            "Palms Specialty Insurance Company, Inc."]
    out = seed_structural_variations(base, list(base))
    assert not _has(out, "Limited"), "bare legal-form token seeded"
    assert not _has(out, "Inc"), "bare legal-form token seeded"
    assert not _has(out, "Palms"), "ambiguous stem (both carriers) seeded"
    assert not _has(out, "Palms Insurance"), "ambiguous stem seeded"
    assert not _has(out, "Insurance Company"), "generic corporate phrase seeded"


def test_never_seeds_a_mid_phrase_fragment():
    """Cutting at a connector produces text no bordereau holds."""
    for value in ["Boiler and Machinery coverage", "United States of America",
                  "War and civil war risks"]:
        forms = structural_variation_forms(value, [value])
        for f in forms:
            words = f.split()
            assert not (len(words) > 1 and any(len(w) < 4 for w in words)), \
                f"fragment {f!r} derived from {value!r}"


def test_no_head_drop_on_a_single_value_rule():
    """Dropping LEADING words removes what identifies the entity."""
    value = "Palms Insurance Company, Limited"
    forms = structural_variation_forms(value, [value])
    assert not _has(forms, "Insurance Company Limited")
    assert not _has(forms, "Company Limited")


def test_initialism_only_for_names_and_only_when_long_enough():
    """'FGB' for a described thing, or a 3-letter coin-flip, is never seeded."""
    named = structural_variation_forms("Volante Specialty Risks, LLC",
                                       ["Volante Specialty Risks, LLC"])
    assert _has(named, "VSRL")

    described = structural_variation_forms("Financial guaranty business",
                                           ["Financial guaranty business"])
    assert not any(f.isupper() and len(f) <= 5 for f in described), described

    short = structural_variation_forms("Volante International Limited",
                                       ["Volante International Limited"])
    assert not _has(short, "VIL"), "3-letter invented initialism seeded"


def test_clause_length_values_are_left_alone():
    """A whole exclusion clause has no surface spellings worth deriving."""
    value = ("injury, sickness, disease, death or destruction with respect to "
             "which an insured under the policy is also an insured under a "
             "nuclear energy liability policy")
    assert structural_variation_forms(value, [value]) == []


def test_does_not_pad_with_forms_the_query_already_matches():
    """A form that changes no row is filler — the prompt's own rule. What "changes
    no row" MEANS is the compiled matcher's test, and that is now exact equality
    after normalization: a truncation of the value is not a spelling of it, while
    dropping its legal-form tail is, and is kept."""
    base = ["Volante International Limited"]
    out = seed_structural_variations(base, list(base))
    assert not _has(out, "Volante International Limite")   # mid-word truncation
    assert _has(out, "Volante International")              # a real shorter spelling
    # …and nothing that differs from the value by punctuation alone, which the
    # matcher normalizes away, so it would genuinely change no row.
    assert len(out) == len({_norm(f) for f in out}), out


def test_tops_up_per_value_not_per_rule():
    """The common failure: the model answers for the first value only. The second
    value must still be filled in — and a spelling of the FIRST one must not be
    miscounted as coverage for it just because they share a word."""
    base = ["Palms Specialty Insurance Company, Inc.",
            "Northwind Specialty Insurance Corporation"]
    kept = base + ["Palms Specialty", "PSICI", "Palms Spec"]
    out = seed_structural_variations(base, kept)
    northwind = [v for v in out if "northwind" in _norm(v)
                 and _norm(v) != _norm(base[1])]
    assert northwind, "second value got no spellings"


def test_existing_spellings_are_preserved_in_order():
    base = ["Volante Canada Limited"]
    kept = base + ["Volante Canada"]
    out = seed_structural_variations(base, kept)
    assert out[:2] == kept


def test_floor_of_zero_disables_seeding():
    base = ["Palms Specialty Insurance Company, Inc."]
    assert seed_structural_variations(base, list(base), minimum=0) == base


# ── normalize_variation_values (the generation entry point) ─────────────────

def test_map_shaped_model_output_is_no_longer_discarded():
    """The prompt's own any_of example shows the {value: [...]} shape; it used to
    be thrown away, leaving the rule with nothing but the contract's wording."""
    assert flatten_variation_values({"A Ltd": ["A Limited", "A"]}) == ["A Limited", "A"]
    params = {
        "field": "Carrier Entity",
        "allowed": ["Volante Specialty Risks, LLC"],
        "variation_values": {"Volante Specialty Risks, LLC": ["Volante Specialty"]},
    }
    out = normalize_variation_values("value_in_set", params)
    assert _has(out["variation_values"], "Volante Specialty")


def test_enum_rule_is_never_born_with_only_its_own_wording():
    params = {"field": "Carrier Entity",
              "allowed": ["MS TRANSVERSE SPECIALTY INSURANCE COMPANY"]}
    out = normalize_variation_values("value_in_set", params)
    extra = [v for v in out["variation_values"]
             if _norm(v) not in {_norm(b) for b in params["allowed"]}]
    assert extra, "rule shipped with zero spellings"


def test_excluded_values_never_leak_into_the_allowed_set():
    params = {"field": "Risk Class", "allowed": ["Hotel Operations"],
              "excluded": ["Nightclub Operations"]}
    out = normalize_variation_values("value_in_set", params)
    assert not any("nightclub" in _norm(v) for v in out["variation_values"])


# ── the AI top-up ───────────────────────────────────────────────────────────

def _fake_ai(payload):
    def _ai(prompt, **kw):
        return payload
    return _ai


def test_topup_admits_a_genuine_spelling_structure_cannot_derive():
    """"Volante Intl" is a real bordereau spelling, is not a cut of the contract's
    text (so the seeder cannot produce it), and is not already matched (0.88)."""
    params = {"field": "Carrier Entity", "allowed": ["Volante International Limited"],
              "variation_values": ["Volante International Limited"]}
    out = topup_variation_values(
        "value_in_set", params,
        ai=_fake_ai('{"variations": {"Volante International Limited": '
                    '["Volante Intl", "Volante Intl Ltd"]}}'))
    assert _has(out["variation_values"], "Volante Intl")


def test_topup_refuses_a_spelling_of_a_different_entity():
    params = {"field": "Carrier Entity",
              "allowed": ["Demoshield Specialty"],
              "variation_values": ["Demoshield Specialty"]}
    out = topup_variation_values(
        "value_in_set", params,
        ai=_fake_ai('{"variations": {"Demoshield Specialty": '
                    '["Palms Specialty Insurance Company, Inc.", "Zurich"]}}'))
    assert not any("palms" in _norm(v) or "zurich" in _norm(v)
                   for v in out["variation_values"])


def test_topup_refuses_an_ambiguous_stem_between_two_authorized_values():
    params = {"field": "Carrier Entity",
              "allowed": ["Palms Insurance Company, Limited",
                          "Palms Specialty Insurance Company, Inc."]}
    params = normalize_variation_values("value_in_set", params)
    out = topup_variation_values(
        "value_in_set", params,
        ai=_fake_ai('{"variations": {"Palms Insurance Company, Limited": '
                    '["Palms", "Palms Insurance"]}}'))
    assert not _has(out["variation_values"], "Palms")
    assert not _has(out["variation_values"], "Palms Insurance")


def test_topup_cannot_invent_a_value_it_was_not_given():
    params = {"field": "Carrier Entity", "allowed": ["Volante Canada Limited"],
              "variation_values": ["Volante Canada Limited"]}
    out = topup_variation_values(
        "value_in_set", params,
        ai=_fake_ai('{"variations": {"Some Other Carrier": ["Whatever Ltd"]}}'))
    assert not any("whatever" in _norm(v) for v in out["variation_values"])


def test_topup_fails_open_on_a_broken_model_answer():
    for payload in ["", "not json at all", "{}", '{"variations": null}']:
        params = {"field": "Carrier Entity", "allowed": ["Volante Canada Limited"],
                  "variation_values": ["Volante Canada Limited"]}
        out = topup_variation_values("value_in_set", params, ai=_fake_ai(payload))
        assert out["variation_values"] == ["Volante Canada Limited"]


def test_topup_fails_open_when_the_model_raises():
    def _boom(prompt, **kw):
        raise RuntimeError("no API key configured")
    params = {"field": "Carrier Entity", "allowed": ["Volante Canada Limited"],
              "variation_values": ["Volante Canada Limited"]}
    out = topup_variation_values("value_in_set", params, ai=_boom)
    assert out["variation_values"] == ["Volante Canada Limited"]


def test_topup_asks_nothing_when_the_floor_is_already_met():
    calls = []

    def _ai(prompt, **kw):
        calls.append(prompt)
        return "{}"

    params = {"field": "Carrier Entity", "allowed": ["Volante Canada Limited"],
              "variation_values": ["Volante Canada Limited", "Volante Canada",
                                   "Volante Can", "VCL"]}
    topup_variation_values("value_in_set", params, ai=_ai)
    assert calls == [], "spent a call on a rule that already had its spellings"


def test_topup_can_be_switched_off():
    os.environ["KAVACHIO_VARIATION_TOPUP"] = "0"
    try:
        def _ai(prompt, **kw):
            raise AssertionError("should not be called")
        params = {"field": "Carrier Entity", "allowed": ["Volante Canada Limited"]}
        topup_variation_values("value_in_set", params, ai=_ai)
    finally:
        os.environ.pop("KAVACHIO_VARIATION_TOPUP", None)


if __name__ == "__main__":
    import sys, traceback
    fails = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception:
                fails += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print(("ALL PASSED" if not fails else f"{fails} FAILED"))
    sys.exit(1 if fails else 0)
