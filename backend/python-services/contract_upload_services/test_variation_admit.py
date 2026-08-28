"""Regression tests for the spelling-admission gates (variation_admit.check).

Run:  cd backend/python-services && python contract_upload_services/test_variation_admit.py

No LLM call and no database write. The DB is only read (the vocabulary lookup in
gate G3), so this is safe to run against any environment.

WHY THESE EXIST
The gates decide whether a person may widen what a validation rule accepts. A
wrong YES is silent and permanent: the rule simply stops catching something, and
nothing reports it. The cases below are the ones that actually went wrong during
development — most importantly SHORT_BASE_HOLE, which admitted an unrelated
carrier into a Yes/No rule on live data.
"""
import os
import sys

os.environ.setdefault("KAVACHIO_VARIATION_ADMIT_REQUIRE_AI", "0")   # gates only
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contract_upload_services import variation_admit as VA   # noqa: E402

BASE = ["Palms Insurance Company, Limited", "Palms Specialty Insurance Company, Inc."]
EXISTING = [*BASE, "Palms Specialty"]
CLAUSE = ("All business shall be written on Palms Insurance Company, Limited paper "
          "or Palms Specialty Insurance Company, Inc. paper.")
FIELD = "Legal Entity (Specialty vs Cayman Paper)"

CASES = [
    # (spelling, template, base, expected reason_code, why)
    ("Palms Spec Ins Co", "value_in_set", BASE, "accepted",
     "a real abbreviation that keeps a distinguishing word"),
    ("Palms Specialty Ins.", "value_in_set", BASE, "accepted",
     "names the Specialty company and nothing else. It used to be refused as "
     "'already fuzzy-matched' by the accepted 'Palms Specialty' (0.90+); the "
     "compiled rule matches on EQUALITY now, so it does NOT match this and "
     "refusing it would be the lie"),
    ("Palms Ins Co Ltd", "value_in_set", BASE, "accepted",
     "dropped legal suffix, far enough from every existing spelling to matter"),
    ("PSIC", "value_in_set", BASE, "accepted",
     "P-S-I-C is letter for letter 'Palms Specialty Insurance Company', and the "
     "initials of no other listed value — a computed fact, so it is confirmed "
     "rather than escalated as unidentifiable"),
    ("PSC", "value_in_set", BASE, "no_trace",
     "LOOKS like an initialism but the letters line up with nothing: shares no "
     "word or substring either, so only meaning could settle it"),
    ("Cayman", "value_in_set", BASE, "no_trace",
     "the column-header contamination case: a token from the COLUMN NAME that "
     "names none of the contract's companies"),
    ("Bermuda Re", "value_in_set", BASE, "no_trace", "an unrelated carrier"),
    ("Palms", "value_in_set", BASE, "ambiguous_stem",
     "shared by both companies, so it identifies neither"),
    ("Palms Insurance", "value_in_set", BASE, "ambiguous_stem",
     "both companies are a 'Palms … Insurance', so it identifies neither. The "
     "old scored matcher answered 'already covered' here, on a 0.90+ shared "
     "prefix — which was true of the matcher and never true of the words"),
    ("Limited", "value_in_set", BASE, "legal_form_token",
     "a bare corporate-form word belongs to countless companies"),
    ("PALMS SPECIALTY", "value_in_set", BASE, "duplicate",
     "capitals are ignored when matching"),
    ("Palms Specialty Insurance Company Inc.", "value_in_set", BASE, "duplicate",
     "punctuation is ignored when matching"),
    ("Palms Specialty Insurance Co., Inc", "value_in_set", BASE, "accepted",
     "'Co.' for 'Company' is a spelling the rule does not carry, so it is one "
     "worth adding — punctuation alone would have been a duplicate (below)"),
    ("", "value_in_set", BASE, "empty", ""),
    ("x" * 200, "value_in_set", BASE, "too_long", ""),
    ("Palms Spec Ins Co", "value_not_in_set", BASE, "accepted",
     "the prohibited-list template admits the same spelling"),

    # BOTH OF THESE WERE ONCE ANSWERED BY A SIMILARITY SCORE, and the scores
    # disagreed with each other depending on which library asked (difflib 0.913
    # vs jaro-winkler 0.800 for the first pair) — an admin was told a rule
    # "already matched" a spelling it did not. Neither spelling is carried by
    # its rule, so neither is matched, so both are worth adding: which is the
    # answer the gates give now, without asking any metric.
    ("CGL US Surplus Line Policies", "value_in_set",
     ["Commercial General Liability US Surplus Lines Policies"], "accepted",
     "a real abbreviation the rule does not carry"),
    ("US Surplus Lines Policie", "value_in_set", ["US Surplus Lines Policies"],
     "accepted", "a spelling seen in a bordereau; the rule does not match it "
                 "today, so recording it is exactly what this screen is for"),

    # THE SHORT-BASE HOLE — found on live rule 11295 (Referral Required (Y/N)).
    # 'N' is a substring of "holdi(n)gs", so the permissive substring test traced
    # ANY string back to a Yes/No rule and only the model stood in the way.
    ("Zurich Bermuda Holdings", "value_in_set", ["Yes", "No", "Y", "N", "1", "0"],
     "no_trace", "SHORT_BASE_HOLE: one-character contract values must not match "
                 "by substring"),
    ("Business", "value_in_set", ["US", "CA"], "no_trace",
     "SHORT_BASE_HOLE: 'US' inside 'Business' is not a trace"),
    ("Yes indeed", "value_in_set", ["Yes", "No"], "accepted",
     "a genuine shared WORD still traces even when the value is short"),
]


def run() -> int:
    failures = []
    for spelling, template, base, expect, why in CASES:
        got = VA.check(spelling, template=template, field=FIELD, base=base,
                       existing=EXISTING if base is BASE else list(base),
                       clause_text=CLAUSE)["reason_code"]
        ok = got == expect
        if not ok:
            failures.append((spelling, expect, got, why))
        print(f"{'ok  ' if ok else 'FAIL'}  {spelling[:34]:<36} {template:<17} "
              f"{expect:<17} got={got}")

    # Polarity: the two enum templates mean opposite things and must never share
    # wording, or an admin does the exact opposite of what they intend.
    a = VA.check("Cayman", template="value_in_set", field=FIELD, base=BASE,
                 existing=[], clause_text=CLAUSE)
    b = VA.check("Cayman", template="value_not_in_set", field=FIELD, base=BASE,
                 existing=[], clause_text=CLAUSE)
    assert "authorizes" in a["reason"] and "prohibits" in b["reason"], \
        "rejection wording must follow the template's polarity"
    assert a["clause_quote"], "a rejection must quote the contract clause"
    print("ok    rejection wording is polarity-aware and quotes the clause")

    os.environ["KAVACHIO_VARIATION_ADMIT_REQUIRE_AI"] = "1"
    called = []

    def ai_says_yes(prompt, **kw):
        called.append(prompt)
        return '{"accept": true, "reason": "same company"}'

    # POINTLESS input never reaches the model — it is already covered, or it is
    # ambiguous by construction. No amount of judgement changes either.
    for spelling, code in (("PALMS SPECIALTY", "duplicate"),
                           ("Palms", "ambiguous_stem")):
        called.clear()
        r = VA.check(spelling, template="value_in_set", field=FIELD, base=BASE,
                     existing=EXISTING, clause_text=CLAUSE, ai=ai_says_yes)
        assert not r["accepted"] and r["reason_code"] == code, r
        assert not called, f"{spelling!r} must never reach the model"
    print("ok    already-covered and ambiguous input never reaches the model")

    # A string that resembles an initialism but is NOT one ("PSC" is the initials
    # of neither company) cannot be settled by string comparison, so it goes to
    # the model carrying a pre-check note and the model decides.
    called.clear()
    r = VA.check("PSC", template="value_in_set", field=FIELD, base=BASE,
                 existing=EXISTING, clause_text=CLAUSE, ai=ai_says_yes)
    assert r["accepted"] and r["reason_code"] == "accepted", r
    assert len(called) == 1, "an unconfirmable candidate must be sent to the model"
    assert "AUTOMATED PRE-CHECK NOTE" in called[0], \
        "the model must be told the text checks could not confirm it"
    assert "PSC" in called[0] and CLAUSE[:40] in called[0], \
        "the prompt must carry the candidate AND the exact contract line"
    print("ok    an unconfirmable candidate is escalated, with the contract line")

    # ...and the model's NO is final.
    r = VA.check("PSC", template="value_in_set", field=FIELD, base=BASE,
                 existing=EXISTING, clause_text=CLAUSE,
                 ai=lambda p, **kw: '{"accept": false, "reason": "unrelated carrier"}')
    assert not r["accepted"] and r["reason_code"] == "ai_rejected", r
    print("ok    the model's refusal of an initialism is final")

    # ACRONYM_BLINDNESS — a REAL initialism is arithmetic, not judgement. "PICL"
    # is letter for letter "Palms Insurance Company, Limited", and it matches no
    # other listed value. Told nothing, a conservative model refuses it (live:
    # "SSIC" for "SiriusPoint Specialty Insurance Corporation" on QA setup 38).
    # So the letters are checked here and handed over as EVIDENCE: no pre-check
    # warning, and a COMPUTED INITIALS block in the prompt. The model still votes.
    called.clear()
    r = VA.check("PICL", template="value_in_set", field=FIELD, base=BASE,
                 existing=EXISTING, clause_text=CLAUSE, ai=ai_says_yes)
    assert r["accepted"] and r["reason_code"] == "accepted", r
    assert "COMPUTED INITIALS" in called[0], \
        "the model must be shown that the letters were verified"
    assert "AUTOMATED PRE-CHECK NOTE" not in called[0], \
        "a verified initialism must NOT also be flagged as unconfirmable"
    print("ok    a real initialism reaches the model as evidence, not as a warning")

    # CATEGORY-CODE BLINDNESS — a contract names a category in prose ("New"); a
    # bordereau writes it as a system code ("NEW_BUSINESS"). That is neither an
    # abbreviation nor a misspelling, so a model given only those examples refuses
    # it — while the platform's own exception recommender scored the same pair at
    # 100% (live rule 1984, 43 policies flagged). ONE added word is a qualifier, so
    # it reaches the model with the word breakdown computed for it.
    called.clear()
    cat = ["New", "Renewal"]
    r = VA.check("NEW_BUSINESS", template="value_in_set", field="New/Renewal",
                 base=cat, existing=cat, clause_text=CLAUSE, ai=ai_says_yes)
    assert r["accepted"], r
    assert "COMPUTED WORD BREAKDOWN" in called[0], \
        "the model must be shown which listed value the words cover"
    assert '"New"' in called[0], "the covered value must be named"
    assert "proves NOTHING by itself" in called[0], \
        "containment must never be presented as proof"
    print("ok    a category written as a system code reaches the model as evidence")

    # ...and ONE added word still leaves the verdict to the model, which can refuse.
    called.clear()
    r = VA.check("New Zurich", template="value_in_set", field="New/Renewal",
                 base=cat, existing=cat, clause_text=CLAUSE,
                 ai=lambda p, **kw: (called.append(p),
                                     '{"accept": false, "reason": "a carrier, not a category"}')[1])
    assert not r["accepted"] and r["reason_code"] == "ai_rejected", r
    assert "COMPUTED WORD BREAKDOWN" in called[0], "the fact is stated either way"
    print("ok    one added word is the model's call, and its refusal stands")

    # SUPERSET_NAME — the fix for a bug I introduced. Broadening the prompt to
    # admit code forms also made the model accept "<value> QZXJKV7 Holdings Group"
    # on 4 of 10 live rules. A hard refusal on extra words was tried and rejected:
    # live data is full of LEGITIMATE supersets ("Commonwealth of the Northern
    # Mariana Islands" for "Northern Mariana Islands", "Sabal Specialty Insurance
    # Company" for "Sabal Specialty"), structurally identical to the dangerous
    # ones. So it is a WARNING the model must answer, and the model's NO is final.
    called.clear()
    r = VA.check("Everest Re Holdings Group", template="value_in_set", field=FIELD,
                 base=["Everest Re"], existing=["Everest Re"], clause_text=CLAUSE,
                 ai=lambda p, **kw: (called.append(p),
                                     '{"accept": false, "reason": "a different company"}')[1])
    assert not r["accepted"] and r["reason_code"] == "ai_rejected", r
    assert "AUTOMATED PRE-CHECK NOTE" in called[0], \
        "a superset must reach the model carrying its warning"
    assert "Holdings Group" in called[0]
    print("ok    a value with extra words is escalated as a warning, and NO is final")

    # ...and the legitimate supersets in live data still get through when the model
    # confirms them — the reason this is not a hard refusal.
    r = VA.check("Commonwealth of the Northern Mariana Islands", template="value_in_set",
                 field=FIELD, base=["Northern Mariana Islands"],
                 existing=["Northern Mariana Islands"], clause_text=CLAUSE, ai=ai_says_yes)
    assert r["accepted"], r
    print("ok    a legitimate longer official name is still admissible")

    # SHORT FORM -> REGISTERED NAME. A contract names a carrier in short form and
    # the bordereau writes its registered name out in full — the very same spelling
    # the accept-list already admits in the OTHER direction ("a dropped legal
    # suffix"). It was refused anyway: the word-breakdown rule told the model to
    # reject extra words that "name an organisation", and "Insurance Company, Inc."
    # does. Live case: setup 184, column Carrier, where "Demoshield Specialty
    # Insurance Company inc." came back "specifies a different legal entity" against
    # a clause requiring "Demoshield Specialty" — while a sibling rule on the same
    # contract lists the full name as an authorized value.
    called.clear()
    r = VA.check("Sabal Specialty Insurance Company, Inc.", template="conditional_value",
                 field="Carrier", base=["Sabal Specialty"],
                 existing=["Sabal Specialty"], clause_text=CLAUSE, ai=ai_says_yes)
    assert r["accepted"] and r["reason_code"] == "accepted", r
    assert "DROPPED or ADDED" in called[0], \
        "the accept-list must be symmetric: a legal suffix ADDED is the same entity"
    assert "CORPORATE FORM" in called[0], \
        "the word breakdown must offer corporate-form extras as the SAME value"
    assert "REGISTERED name; decide which" in called[0], \
        "the pre-check note must state both directions, not only the reject one"
    # ...and the hole this could reopen stays shut: a parent/holding designation is
    # still named as a DIFFERENT entity — the "<value> QZXJKV7 Holdings Group" bug.
    assert "parent / holding / affiliate designation" in called[0], \
        "widening for corporate form must not admit a holdco or an affiliate"
    print("ok    a registered name written out in full is admissible, a holdco is not")

    # With no model reachable a superset stays refused, like every other warning.
    os.environ["KAVACHIO_VARIATION_ADMIT_REQUIRE_AI"] = "0"
    r = VA.check("Everest Re Holdings Group", template="value_in_set", field=FIELD,
                 base=["Everest Re"], existing=["Everest Re"], clause_text=CLAUSE)
    assert not r["accepted"] and r["reason_code"] == "superset_name", r
    os.environ["KAVACHIO_VARIATION_ADMIT_REQUIRE_AI"] = "1"
    print("ok    with the model off, a superset fails closed")

    # ...but an initialism fitting BOTH companies identifies neither. That is a
    # fact, so it is refused outright and never costs a token.
    called.clear()
    amb = ["Sirius Insurance Corporation", "Summit Indemnity Company"]   # both -> SIC
    r = VA.check("SIC", template="value_in_set", field=FIELD, base=amb,
                 existing=amb, clause_text=CLAUSE, ai=ai_says_yes)
    assert not r["accepted"] and r["reason_code"] == "ambiguous_acronym", r
    assert not called, "an ambiguous initialism must never reach the model"
    print("ok    an initialism matching two values is refused without a model call")

    # With no model reachable, an unconfirmable candidate stays refused.
    os.environ["KAVACHIO_VARIATION_ADMIT_REQUIRE_AI"] = "0"
    r = VA.check("PSC", template="value_in_set", field=FIELD, base=BASE,
                 existing=EXISTING, clause_text=CLAUSE)
    assert not r["accepted"] and r["reason_code"] == "no_trace", r
    print("ok    with the model off, an unconfirmable variation stays refused")
    os.environ["KAVACHIO_VARIATION_ADMIT_REQUIRE_AI"] = "1"

    r = VA.check("Palms Spec Ins Co", template="value_in_set", field=FIELD, base=BASE,
                 existing=[], clause_text=CLAUSE,
                 ai=lambda p, **kw: '{"accept": false, "reason": "different underwriter",'
                                    ' "clause_quote": "written on Palms Insurance Company"}')
    assert not r["accepted"] and r["reason_code"] == "ai_rejected", r
    print("ok    model veto is respected")

    # Unreachable / broken / empty model must FAIL CLOSED.
    for bad in (lambda p, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
                lambda p, **kw: "not json",
                lambda p, **kw: "",
                lambda p, **kw: '{"reason": "no verdict key"}'):
        r = VA.check("Palms Spec Ins Co", template="value_in_set", field=FIELD,
                     base=BASE, existing=[], clause_text=CLAUSE, ai=bad)
        assert not r["accepted"] and r["reason_code"] == "checker_unavailable", r
    print("ok    a broken or unreachable model fails closed")
    os.environ["KAVACHIO_VARIATION_ADMIT_REQUIRE_AI"] = "0"

    # ---- batch admission: several variations, ONE model call ---------------
    os.environ["KAVACHIO_VARIATION_ADMIT_REQUIRE_AI"] = "1"
    calls = []

    def batch_ai(prompt, **kw):
        calls.append(prompt)
        return ('{"verdicts": ['
                '{"variation": "Palms Ins Co Ltd", "accept": true, "reason": "same company"},'
                '{"variation": "Palms Specialty Co", "accept": true, "reason": "same company"},'
                '{"variation": "Palms Underwriters", "accept": false, '
                ' "reason": "a different entity", "clause_quote": "written on Palms Insurance Company"},'
                '{"variation": "Cayman", "accept": false, '
                ' "reason": "a place, not one of the named companies"}]}')

    batch = ["Palms Ins Co Ltd",        # text checks pass  → model accepts
             "Palms Specialty Co",      # text checks pass  → model accepts
             "Palms Underwriters",      # text checks pass  → model REJECTS
             "Cayman",                  # unconfirmable     → model REJECTS
             "Palms Ins Co Ltd"]        # repeat of #1      → caught by G2
    res = VA.check_many(batch, template="value_in_set", field=FIELD, base=BASE,
                        existing=list(BASE), clause_text=CLAUSE, ai=batch_ai)

    assert len(calls) == 1, f"a batch must cost exactly ONE model call, got {len(calls)}"
    codes = [(r["spelling"], r["reason_code"]) for r in res]
    assert codes[0] == ("Palms Ins Co Ltd", "accepted"), codes
    assert codes[1] == ("Palms Specialty Co", "accepted"), codes
    assert codes[2] == ("Palms Underwriters", "ai_rejected"), codes
    assert codes[3] == ("Cayman", "ai_rejected"), codes
    assert codes[4][1] == "duplicate", codes      # repeat inside the same batch
    # The repeat is settled deterministically, so it must not occupy a slot in
    # the batch sent to the model.
    assert calls[0].count('"Palms Ins Co Ltd"') == 1, calls[0]
    assert "AUTOMATED PRE-CHECK NOTES" in calls[0] and '"Cayman"' in calls[0], \
        "an unconfirmable candidate must be sent WITH its pre-check note"
    print(f"ok    batch of {len(batch)} judged in ONE model call "
          f"({sum(1 for r in res if r['accepted'])} accepted, "
          f"{sum(1 for r in res if not r['accepted'])} refused)")

    # A broken batch response must fail closed for every candidate.
    res = VA.check_many(["Palms Ins Co Ltd", "Palms Specialty Co"],
                        template="value_in_set", field=FIELD, base=BASE,
                        existing=list(BASE), clause_text=CLAUSE,
                        ai=lambda p, **kw: "garbage, not json")
    assert all(r["reason_code"] == "checker_unavailable" for r in res), res
    print("ok    an unparseable batch response fails closed for every candidate")
    os.environ["KAVACHIO_VARIATION_ADMIT_REQUIRE_AI"] = "0"

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for spelling, expect, got, why in failures:
            print(f"  {spelling!r}: expected {expect}, got {got}  — {why}")
        return 1
    print(f"\nALL {len(CASES)} GATE CASES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run())
