"""
test_derived_duplicate_same_check.py
────────────────────────────────────
`generic_rule_library.drop_derived_duplicates` removes an auto-derived rule only
when a library rule is the SAME check — same template on the same columns. It used
to key on the validated column alone, so a library shape check (pattern_check) on
a postal column displaced the derived postal-code-for-state check on that column,
and a real postal/state mismatch shipped with no rule to catch it.

Synthetic IRs only. Pure — see _offline.py (no DB) — and no model calls.

Run:  python -m pytest contract_upload_services/tests/test_derived_duplicate_same_check.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _offline                                                  # noqa: E402,F401

from contract_upload_services.generic_rule_library import (     # noqa: E402
    drop_derived_duplicates,
)


def _derived(*irs):
    return {"clause": {"clause_id": None, "text": "[Derived rule] synthetic"},
            "engine": "ir", "candidates": list(irs)}


def _library(*irs):
    return [{"clause": {"clause_id": -1, "text": "[Generic rule] synthetic"},
             "engine": "ir", "candidates": list(irs)}]


def _pair_check(a, b, **extra):
    return {"template": "zip_state_consistency",
            "params": {"zip_field": a, "state_field": b, **extra}}


def _shape(col):
    return {"template": "pattern_check", "params": {"field": col, "pattern": "^x$"}}


def test_a_shape_check_does_not_displace_a_different_check_on_the_column():
    derived = _pair_check("Col A", "Col B")
    synth = [_derived(derived)]
    assert drop_derived_duplicates(synth, _library(_shape("Col A"))) == 0
    assert synth[0]["candidates"] == [derived]


def test_the_same_check_on_the_same_columns_is_still_dropped():
    synth = [_derived(_pair_check("Col A", "Col B", country_field="Col C"))]
    library = _pair_check("col a ", "COL B")
    assert drop_derived_duplicates(synth, _library(library)) == 1
    assert synth == []
    # …and the derived rule's country dispatch still moves onto the survivor.
    assert library["params"]["country_field"] == "Col C"


def test_the_same_template_on_a_different_companion_column_is_kept():
    synth = [_derived(_pair_check("Col A", "Col B"))]
    assert drop_derived_duplicates(synth, _library(_pair_check("Col A", "Col D"))) == 0
    assert len(synth) == 1


def test_the_same_template_with_roles_swapped_is_kept():
    synth = [_derived(_pair_check("Col A", "Col B"))]
    assert drop_derived_duplicates(synth, _library(_pair_check("Col B", "Col A"))) == 0
    assert len(synth) == 1


def test_single_column_true_duplicate_is_dropped():
    derived = {"template": "required_field", "params": {"field": "Col A"}}
    library = {"template": "required_field", "params": {"field": "Col A"}}
    synth = [_derived(derived)]
    assert drop_derived_duplicates(synth, _library(library)) == 1
    assert synth == []


def test_only_the_duplicated_candidate_leaves_a_mixed_entry():
    dup = {"template": "required_field", "params": {"field": "Col A"}}
    other = _pair_check("Col A", "Col B")
    synth = [_derived(dup, other)]
    lib = _library({"template": "required_field", "params": {"field": "Col A"}},
                   _shape("Col A"))
    assert drop_derived_duplicates(synth, lib) == 1
    assert synth[0]["candidates"] == [other]


def test_non_derived_entries_are_never_touched():
    entry = {"clause": {"clause_id": 7, "text": "contract clause"},
             "engine": "ir",
             "candidates": [{"template": "required_field", "params": {"field": "Col A"}}]}
    synth = [entry]
    lib = _library({"template": "required_field", "params": {"field": "Col A"}})
    assert drop_derived_duplicates(synth, lib) == 0
    assert synth == [entry]
