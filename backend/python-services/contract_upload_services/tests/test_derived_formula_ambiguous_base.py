"""
test_derived_formula_ambiguous_base.py
──────────────────────────────────────
derive_formula_entries picks the commission BASE premium (and the Net Premium
target) by name tokens. When several distinct columns tie on that match, a guess
(first in template order) can bind a program total instead of the transaction
premium and flag every endorsement row — so the deterministic formula stays out
and the target is left for the template's formula annotation. A single clear
match must still derive exactly as before.

Synthetic field lists only. Pure — see _offline.py (no DB) — and no model calls.

Run:  python -m pytest contract_upload_services/tests/test_derived_formula_ambiguous_base.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _offline                                                  # noqa: E402,F401

from contract_upload_services import validation_rule_generator as vrg  # noqa: E402


def _f(name, data_type="decimal", samples=()):
    return {"name": name, "sheet": "S1", "data_type": data_type,
            "canonical_field": None, "samples": list(samples),
            "samples_all": list(samples)}


def _governs(field):
    """A mapped clause rule targeting `field` (what makes a rate 'governed')."""
    return {"clause": {"clause_id": 1, "text": "x", "page_number": 1},
            "engine": "ir",
            "candidates": [{"template": "range_check",
                            "params": {"field": field, "min": 0, "max": 30}}]}


def _math(fields, synth):
    return [e["candidates"][0]["params"]
            for e in vrg.derive_formula_entries(synth, fields)
            if e["candidates"][0]["template"] == "cross_field_math"]


def test_single_base_candidate_derives_commission_and_net_unchanged():
    fields = [_f("Gross Premium", samples=["1000", "-50"]),
              _f("Commission Rate", samples=["23.5", "10"]),
              _f("Commission Amount", samples=["235", "-5"]),
              _f("Net Premium", samples=["765", "-45"]),
              _f("Fac Net Premium"),
              _f("Carrier Net/Net Premium")]
    rules = _math(fields, [_governs("Commission Rate")])
    by_result = {p["result_field"]: p for p in rules}
    assert set(by_result) == {"Commission Amount", "Net Premium"}
    com = by_result["Commission Amount"]
    assert (com["left_field"], com["operator"], com["right_field"]) == \
        ("Gross Premium", "*", "Commission Rate")
    assert com["right_is_percent"] is True
    net = by_result["Net Premium"]
    assert (net["left_field"], net["operator"], net["right_field"]) == \
        ("Gross Premium", "-", "Commission Amount")


def test_tied_base_candidates_leave_commission_and_net_ungoverned():
    # Two bare "gross … premium" columns score identically against an amount
    # column that names no entity — no way to tell which one the rate applies to.
    fields = [_f("Gross premium this transaction"),
              _f("Total gross written premium"),
              _f("Brokerage % of gross premium"),
              _f("Commission %"),
              _f("Commission Amount"),
              _f("Net Premium")]
    assert _math(fields, [_governs("Commission %")]) == []


def test_entity_tokens_still_break_a_multi_party_tie():
    fields = [_f("Alpha Gross Written Premium"),
              _f("100% Gross Written Premium"),
              _f("Alpha Commission %"),
              _f("Alpha Commission Amount")]
    rules = _math(fields, [_governs("Alpha Commission %")])
    assert [(p["result_field"], p["left_field"]) for p in rules] == \
        [("Alpha Commission Amount", "Alpha Gross Written Premium")]


def test_tied_net_targets_skip_only_the_net_rule():
    fields = [_f("Gross Premium"),
              _f("Commission Rate"),
              _f("Commission Amount"),
              _f("Net Premium Original Currency"),
              _f("Net Premium Settlement Currency")]
    rules = _math(fields, [_governs("Commission Rate")])
    assert [p["result_field"] for p in rules] == ["Commission Amount"]


def test_fin_match_ties_and_best_match_agree():
    cands = ["Total gross premium", "Gross premium", "Gross premium paid", "Gross premium"]
    ties = vrg._fin_match_ties(set(), cands)
    assert ties == ["Total gross premium", "Gross premium"]    # distinct, in order
    assert vrg._best_fin_match(set(), cands) == ties[0]
    assert vrg._best_fin_match(set(), ["Only"]) == "Only"
    assert vrg._best_fin_match(set(), []) is None
