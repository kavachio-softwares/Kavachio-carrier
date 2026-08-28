"""Regression tests for adding and REMOVING a rule's surface spellings.

Run:  cd backend/python-services && python contract_upload_services/test_rule_editor_variations.py

No LLM call and no database write — rule_editor is pure: IR in, IR out. The DB is
read only through the vocabulary lookup the compiler performs.

WHY THESE EXIST
`variation_values` mixes two different things: the values the CONTRACT names
(seeded by normalize_variation_values) and the spellings an ADMIN taught it. Only
the second kind is the admin's to take back. Everything below pins that boundary,
plus the two shapes the field actually takes in live rules.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contract_upload_services.rule_editor import (          # noqa: E402
    add_variation_value, remove_variation_value, removable_variations,
    variation_context, RuleEditError,
)


class Schema:
    """The slice of output_schema rule_editor and the compiler actually touch."""
    def __init__(self, fields, sheet="Sheet1"):
        self.field_names = list(fields)
        self.field_to_sheets = {f: [sheet] for f in fields}
        self.primary_sheet = sheet


FIELD = "Writing Company"
SCHEMA = Schema([FIELD, "Policy #"])
BASE = ["SiriusPoint Specialty Insurance Corporation",
        "SiriusPoint America Insurance Company"]


def enum_spec(variation_values=None, template="value_in_set"):
    params = {"field": FIELD}
    params["allowed" if template == "value_in_set" else "excluded"] = list(BASE)
    if variation_values is not None:
        params["variation_values"] = variation_values
    return {"kind": "ir_v1", "ir": {"template": template, "params": params}}


def cond_spec(variation_values=None):
    """conditional_value keeps spellings in a {value: [...]} map, and only the
    TARGET key belongs to the admin — the others describe the CONDITION."""
    params = {"field": FIELD, "op": "=", "value": BASE[0],
              "condition": {"field": "Policy #", "op": "=", "value": "X"}}
    if variation_values is not None:
        params["variation_values"] = variation_values
    return {"kind": "ir_v1", "ir": {"template": "conditional_value", "params": params}}


def vocab_already_collapses(spelling) -> str | None:
    """The contract value the SHARED DICTIONARY already folds `spelling` onto, if
    any.

    This matters for what the compiled SQL can be asserted to contain. The
    compiler dedupes its VALUES list by canonical token
    (rule_compiler._enum_values_sql), so when the dictionary maps a spelling onto a
    contract value the literal is dropped as redundant — the cell is normalized
    onto the same token anyway (_vocab_cell_expr). Adding or removing such a
    spelling therefore leaves the SQL byte-identical, and asserting otherwise
    couples the test to whatever happens to be in vocabulary_term.
    """
    try:
        from contract_upload_services.vocabulary import canonical_token
        tok = canonical_token(spelling, FIELD)
        for b in BASE:
            if tok and canonical_token(b, FIELD) == tok:
                return b
    except Exception:
        pass
    return None


def check(label, fn):
    try:
        fn()
    except AssertionError as e:
        print(f"FAIL  {label}\n      {e}")
        return False
    except Exception as e:
        print(f"FAIL  {label}\n      unexpected {type(e).__name__}: {e}")
        return False
    print(f"ok    {label}")
    return True


def refuses(fn, *, contains):
    try:
        fn()
    except RuleEditError as e:
        assert contains.lower() in str(e).lower(), \
            f"wrong refusal message: {e!r} (wanted {contains!r})"
        return
    raise AssertionError(f"expected a refusal mentioning {contains!r}")


def run() -> int:
    results = []

    # ── the round trip ───────────────────────────────────────────────────
    def roundtrip():
        spec, after, _ = add_variation_value(enum_spec(), SCHEMA, "SSIC")
        assert "SSIC" in after, after
        # Adding seeds the contract's own values first, so the rule never
        # narrows to the one spelling just typed.
        assert all(any(b.lower() == a.lower() for a in after) for b in BASE), after
        assert spec["compiled_sql"], "add must leave compiled SQL on the spec"

        spec2, after2, _ = remove_variation_value(spec, SCHEMA, "SSIC")
        assert "SSIC" not in after2, after2
        assert all(any(b.lower() == a.lower() for a in after2) for b in BASE), after2
        assert spec2["compiled_sql"], "remove must recompile, not drop the SQL"
        assert spec2["ir"]["params"].get("variation_values") != \
            spec["ir"]["params"].get("variation_values"), "the IR must change"

        covered = vocab_already_collapses("SSIC")
        if covered:
            # The dictionary already folds it onto a contract value, so the literal
            # was never in the SQL to begin with and the two compile identically.
            # This is the case the delete route reports as
            # `still_matched_by_dictionary` — removing it from the rule does NOT
            # stop the rule matching it.
            assert spec2["compiled_sql"] == spec["compiled_sql"], \
                "a dictionary-covered spelling contributes no SQL either way"
        else:
            assert spec2["compiled_sql"] != spec["compiled_sql"], \
                "removal must recompile the SQL, not leave the spelling in it"
            assert "SSIC" not in spec2["compiled_sql"], spec2["compiled_sql"][:400]
    results.append(check("add then remove restores the rule and recompiles", roundtrip))

    def sql_actually_changes_for_an_unknown_spelling():
        # A spelling no dictionary can know, so the SQL comparison is hermetic.
        novel = "Qzx7 Speciality Ins Corp"
        assert vocab_already_collapses(novel) is None, "fixture must stay unknown"
        base_spec = enum_spec()
        added, _a, _ = add_variation_value(base_spec, SCHEMA, novel)
        assert novel in added["compiled_sql"], "the spelling must reach the SQL"
        removed, _a, _ = remove_variation_value(added, SCHEMA, novel)
        assert novel not in removed["compiled_sql"], \
            "removal must take the spelling back out of the executed query"
    results.append(check("a spelling the dictionary does not know changes the SQL both ways",
                         sql_actually_changes_for_an_unknown_spelling))

    # ── THE guard ────────────────────────────────────────────────────────
    def contract_value_protected():
        spec, _after, _ = add_variation_value(enum_spec(), SCHEMA, "SSIC")
        for victim in (BASE[0], BASE[0].upper(), "siriuspoint  specialty insurance corporation."):
            refuses(lambda v=victim: remove_variation_value(spec, SCHEMA, v),
                    contains="this contract names")
    results.append(check("a value the CONTRACT names can never be removed",
                         contract_value_protected))

    def protected_on_prohibited_too():
        spec = enum_spec(template="value_not_in_set")
        spec, _a, _ = add_variation_value(spec, SCHEMA, "SSIC")
        refuses(lambda: remove_variation_value(spec, SCHEMA, BASE[1]),
                contains="this contract names")
    results.append(check("the guard holds for value_not_in_set as well",
                         protected_on_prohibited_too))

    # ── removable_variations is the UI's single source of truth ──────────
    def removable_excludes_base():
        spec, _a, _ = add_variation_value(enum_spec(), SCHEMA, "SSIC")
        spec, _a, _ = add_variation_value(spec, SCHEMA, "SAIC")
        rem = removable_variations(spec)
        assert sorted(rem) == ["SAIC", "SSIC"], rem
        # Every chip the UI offers must actually be removable by the server.
        working = spec
        for r in rem:
            working, _after, _ = remove_variation_value(working, SCHEMA, r)
        assert removable_variations(working) == [], removable_variations(working)
    results.append(check("removable_variations offers exactly what remove accepts",
                         removable_excludes_base))

    # ── shapes ───────────────────────────────────────────────────────────
    def absent_shape():
        # A rule with no variation_values at all: nothing to remove.
        refuses(lambda: remove_variation_value(enum_spec(), SCHEMA, "SSIC"),
                contains="not stored")
        assert removable_variations(enum_spec()) == []
    results.append(check("a rule with no spellings has nothing to remove", absent_shape))

    def empty_after_removal_drops_the_key():
        spec, _a, _ = add_variation_value(enum_spec(variation_values=["SSIC"]),
                                          SCHEMA, "SAIC")
        working = spec
        for v in ("SSIC", "SAIC"):
            working, _after, _ = remove_variation_value(working, SCHEMA, v)
        params = working["ir"]["params"]
        assert "variation_values" not in params, params
        # ...and the rule still matches the contract's own values.
        assert BASE[0].split()[0].lower() in working["compiled_sql"].lower()
    results.append(check("emptying the list returns the rule to its generated shape",
                         empty_after_removal_drops_the_key))

    def conditional_target_only():
        vv = {BASE[0]: ["SSIC"], "Some Condition Value": ["SCV"]}
        spec = cond_spec(vv)
        # The TARGET's own spelling goes...
        spec2, after, _ = remove_variation_value(spec, SCHEMA, "SSIC")
        assert "SSIC" not in after, after
        assert spec2["ir"]["params"]["variation_values"]["Some Condition Value"] == ["SCV"], \
            "the CONDITION bucket must be left completely untouched"
        # ...but a CONDITION-side spelling is not the admin's to drop from here.
        refuses(lambda: remove_variation_value(spec, SCHEMA, "SCV"),
                contains="not stored")
        assert removable_variations(spec) == ["SSIC"], removable_variations(spec)
    results.append(check("conditional rules: only the TARGET bucket is editable",
                         conditional_target_only))

    def normalization_matches_the_compiler():
        spec, _a, _ = add_variation_value(enum_spec(), SCHEMA, "S.S.I.C.")
        # Capitals and punctuation are ignored, exactly as the rule card promises.
        spec2, after, _ = remove_variation_value(spec, SCHEMA, "ssic")
        assert not any("ssic" == "".join(ch for ch in a.lower() if ch.isalnum())
                       for a in after), after
    results.append(check("removal ignores capitals and punctuation, like matching does",
                         normalization_matches_the_compiler))

    def bad_input():
        spec, _a, _ = add_variation_value(enum_spec(), SCHEMA, "SSIC")
        refuses(lambda: remove_variation_value(spec, SCHEMA, "   "), contains="pick a variation")
        refuses(lambda: remove_variation_value(spec, SCHEMA, "!!!"), contains="pick a variation")
        refuses(lambda: remove_variation_value(spec, SCHEMA, "Never Added"), contains="not stored")
        refuses(lambda: remove_variation_value({"kind": "legacy"}, SCHEMA, "SSIC"),
                contains="rule-engine")
    results.append(check("blank, junk, unknown and non-IR input all refuse cleanly", bad_input))

    def non_variation_template():
        spec = {"kind": "ir_v1", "ir": {"template": "required_field",
                                        "params": {"field": FIELD}}}
        assert variation_context(spec) is None
        assert removable_variations(spec) == []
        refuses(lambda: remove_variation_value(spec, SCHEMA, "SSIC"),
                contains="match a value against the contract")
    results.append(check("templates without spellings are refused, not crashed",
                         non_variation_template))

    print()
    bad = results.count(False)
    print(f"{'ALL ' if not bad else ''}{len(results)} CASES "
          f"{'PASSED' if not bad else f'RUN — {bad} FAILED'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(run())
