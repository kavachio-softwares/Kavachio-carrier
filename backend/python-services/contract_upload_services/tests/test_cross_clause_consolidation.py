"""One requirement should produce one exception card, and only one.

A reinsurance contract is usually a BUNDLE, and extraction deliberately splits an
enumerated list into a clause per item — so a single "the reinsurer must be one
of these twelve" requirement arrives as twelve `value_in_set` rules on one
column, each rejecting the other eleven items on every row. A live contract in
the estate shows exactly that: thirteen rules on one column, twelve of them one
list.

Every name below is INVENTED. The pass recognises a split list by the lead-in
its clauses share, computed from the text at run time — it holds no vocabulary
of carriers, programmes or clause wordings, and `test_lead_in_grouping_is_
independent_of_wording` proves it by using a lead-in that is not English.

These tests pin the two things that make the pass safe rather than merely tidy:
  * clauses are merged ONLY when they quote the same list lead-in, which is read
    out of the clause text — never from a list of known wordings, so it holds for
    contracts nobody has seen yet;
  * nothing is ever dropped, and a rule that stops being enforced says why.

Run:  python contract_upload_services/tests/test_cross_clause_consolidation.py
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from contract_upload_services.rule_normalizer import (   # noqa: E402
    _consolidate_cross_clause_column_rules, _cc_group_by_lead_in, _cc_is_referral,
)

SCHEMA = types.SimpleNamespace(field_to_sheets={"Reinsurer": "Sheet1",
                                                "Carrier Name": "Sheet1",
                                                "UMR": "Sheet1",
                                                "Policy Effective Date": "Sheet1"},
                               primary_sheet="Sheet1")

# An arbitrary lead-in. Its WORDING is irrelevant — only the fact that the
# sibling clauses share it matters.
LEAD_IN = "Only the following reinsurers are approved for this programme: "


def _enum(rid, clause_id, text, allowed, field="Reinsurer", referral=False):
    ir = {"template": "value_in_set", "rule_name": f"R{rid}",
          "params": {"field": field, "allowed": allowed}}
    return {"rule_name": f"R{rid}", "ir": ir, "template": "value_in_set",
            "rule_spec": {"ir": ir, "compiled_sql": "SELECT 1", "referral": referral},
            "rule_status": "active", "is_referral": referral,
            "source_clause_id": clause_id, "source_verbatim_text": text,
            "rule_description": "", "compiled_sql": "SELECT 1", "_id": rid}


def _bound(rid, clause_id, date, op=">=", field="Policy Effective Date"):
    ir = {"template": "date_bound", "rule_name": f"D{rid}",
          "params": {"field": field, "op": op, "date": date}}
    return {"rule_name": f"D{rid}", "ir": ir, "template": "date_bound",
            "rule_spec": {"ir": ir, "compiled_sql": "SELECT 1", "referral": False},
            "rule_status": "active", "is_referral": False,
            "source_clause_id": clause_id, "source_verbatim_text": f"clause {rid}",
            "rule_description": "", "compiled_sql": "SELECT 1", "_id": rid}


def test_a_split_list_becomes_one_rule_holding_every_item():
    rules = [_enum(i, 100 + i, f"{LEAD_IN}{i}. Reinsurer {i}", [f"Reinsurer {i}"])
             for i in range(1, 5)]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    active = [r for r in out if r["rule_status"] == "active"]
    assert len(active) == 1, [r["rule_status"] for r in out]
    assert sorted(active[0]["ir"]["params"]["allowed"]) == [
        "Reinsurer 1", "Reinsurer 2", "Reinsurer 3", "Reinsurer 4"]
    # Nothing is dropped, and the paused rules explain themselves.
    assert len(out) == 4
    for r in out:
        if r["rule_status"] != "active":
            assert r["rule_status"] == "superseded"
            assert "single list" in r["rule_description"]


def test_both_sql_homes_are_rewritten_together():
    """The runtime reads rule_spec['compiled_sql']; the UI reads the IR."""
    rules = [_enum(i, 100 + i, f"{LEAD_IN}{i}. Reinsurer {i}", [f"Reinsurer {i}"])
             for i in range(1, 3)]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    s = next(r for r in out if r["rule_status"] == "active")
    assert s["compiled_sql"] == s["rule_spec"]["compiled_sql"] != "SELECT 1"
    assert s["rule_spec"]["ir"] is s["ir"]
    assert "Reinsurer 2" in s["compiled_sql"]


def test_clauses_that_do_not_share_a_lead_in_are_left_alone():
    """Two DIFFERENT statements on one column are a mapping problem, not a list.

    Merging them would assert something no clause says. A live contract pairs an
    "authorised company" clause with an "authorised writing companies" clause on
    one carrier column; both must survive untouched. Names are invented — what
    makes these two different is that their clause texts share no lead-in.
    """
    rules = [
        _enum(1, 201, "Eastgate Insurance Company, Springfield, and/or ...",
              ["Eastgate Insurance Company"], field="Carrier Name"),
        _enum(2, 202, "Northwind Specialty Insurance Company, Inc.",
              ["Northwind Specialty Insurance Company, Inc."], field="Carrier Name"),
    ]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out)
    assert len(out[0]["ir"]["params"]["allowed"]) == 1
    assert len(out[1]["ir"]["params"]["allowed"]) == 1


def test_referral_triggers_are_never_consolidated():
    """A referral is not a requirement, so two of them are not a contradiction.

    The flag lives at `is_referral` / rule_spec['referral'] — there is no
    top-level 'referral' key, and guarding on one that does not exist would let
    every referral through.
    """
    rules = [_enum(i, 300 + i, f"{LEAD_IN}{i}. Trigger {i}", [f"Trigger {i}"],
                   referral=True) for i in range(1, 4)]
    assert all(_cc_is_referral(r) for r in rules)
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out)


def test_conflicting_date_bounds_keep_the_most_permissive():
    """`op` is the COMPLIANT relation: '>=' keeps the EARLIEST, '<=' the LATEST."""
    rules = [_bound(1, 401, "2026-01-01"), _bound(2, 402, "2026-02-01"),
             _bound(3, 403, "2026-04-01")]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    active = [r for r in out if r["rule_status"] == "active"]
    assert len(active) == 1 and active[0]["ir"]["params"]["date"] == "2026-01-01"

    rules = [_bound(1, 401, "2026-01-01", op="<="),
             _bound(2, 402, "2026-06-12", op="<=")]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    active = [r for r in out if r["rule_status"] == "active"]
    assert len(active) == 1 and active[0]["ir"]["params"]["date"] == "2026-06-12"


def test_non_iso_dates_are_left_alone():
    """String ordering is only date ordering while every value is ISO."""
    rules = [_bound(1, 401, "01/04/2026"), _bound(2, 402, "2026-02-01")]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out)


def test_equality_bounds_have_no_permissive_end():
    rules = [_bound(1, 401, "2026-01-01", op="="),
             _bound(2, 402, "2026-02-01", op="=")]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out)


def test_rules_from_one_clause_are_not_touched():
    """Several rules from a SINGLE clause belong to the per-clause passes."""
    rules = [_enum(1, 500, f"{LEAD_IN}1. A", ["A"]),
             _enum(2, 500, f"{LEAD_IN}2. B", ["B"])]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out)


def test_lead_in_grouping_is_independent_of_wording():
    """The grouping holds no vocabulary — it discovers the shared prefix.

    The lead-in here is deliberately not English and names nothing real. If the
    pass recognised phrases like "approved reinsurers" it would fail this; it
    passes because it only compares the clause texts to each other.
    """
    lead = "Qx7 zzzz mmmm plph, kkkk vvvv nnnn: "
    rules = [_enum(1, 1, f"{lead}1. Alpha", ["Alpha"]),
             _enum(2, 2, f"{lead}2. Beta", ["Beta"]),
             _enum(3, 3, "An entirely unrelated sentence about something else.",
                   ["Gamma"])]
    groups = _cc_group_by_lead_in(rules, [0, 1, 2])
    assert sorted(len(g) for g in groups) == [1, 2]


def test_one_requirement_restated_elsewhere_becomes_one_rule():
    """The OTHER shape of sibling: not a split list, but the same requirement
    written twice in the document with a different value each time — a
    risk-details page and a signing page each naming the agreement the business
    was accepted under. They differ at the START (their headings), so no long
    shared prefix exists; what gives them away is that they read the same once
    each rule's OWN value is removed. Enforced separately they are unsatisfiable:
    the column holds one value per row, so each rejects the rows the other
    accepts."""
    rules = [
        _enum(1, 1, "CLASS: This Contract is to cover all business accepted by "
                    "the Reinsured under UMR B1776BL204521Q.",
              ["B1776BL204521Q"], field="UMR"),
        _enum(2, 2, "Class of Business: This Contract is to cover all business "
                    "accepted by the Reinsured under UMR B1776BL204522Q.",
              ["B1776BL204522Q"], field="UMR"),
    ]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    active = [r for r in out if r.get("rule_status") == "active"]
    assert len(active) == 1, [r.get("rule_status") for r in out]
    allowed = (active[0]["ir"]["params"]).get("allowed")
    assert sorted(allowed) == ["B1776BL204521Q", "B1776BL204522Q"], allowed


def test_a_different_requirement_on_the_same_column_is_not_absorbed():
    """The restatement test must not swallow a genuinely different requirement
    that happens to constrain the same column — that would silently widen a rule
    to accept values no clause allows."""
    rules = [
        _enum(1, 1, "CLASS: This Contract is to cover all business accepted by "
                    "the Reinsured under UMR B1776BL204521Q.",
              ["B1776BL204521Q"], field="UMR"),
        _enum(2, 2, "Class of Business: This Contract is to cover all business "
                    "accepted by the Reinsured under UMR B1776BL204522Q.",
              ["B1776BL204522Q"], field="UMR"),
        _enum(3, 3, "Approved Reinsurers: the Reinsured may cede only to "
                    "reinsurers rated A- or better by AM Best, namely "
                    "B9999XX9999999.",
              ["B9999XX9999999"], field="UMR"),
    ]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    by_name = {r["rule_name"]: r for r in out}
    third = [r for r in out if r["ir"]["params"].get("allowed") == ["B9999XX9999999"]]
    assert len(third) == 1 and third[0].get("rule_status") == "active", out
    merged = [r for r in out if r.get("rule_status") == "active"
              and sorted(r["ir"]["params"].get("allowed") or []) ==
              ["B1776BL204521Q", "B1776BL204522Q"]]
    assert len(merged) == 1, [r["ir"]["params"].get("allowed") for r in out]


# ── two clauses pinning ONE column to two different exact values ────────────
# A cell holds one value, so two "must equal X" rules on the same column reject
# precisely the rows the other accepts and every row is flagged by one of them.
# The reviewer sees two cards recommending different values for the same cell.
# Wordings below are invented; the pass reads the numbers out of whatever text a
# clause carries and holds no vocabulary.

_TREATY = ("The Reinsured shall cede and the Reinsurer(s) shall accept a Quota "
           "Share as detailed below: 45.4500% of the Reinsured's 55.0000% line "
           "of each declaration limit.")
_ORDER = "ORDER HEREON: 45.4500% of 55.000% of 100.00%"
_CASH = ("CASH LOSS: USD500,000 (100%), as defined under Notification and "
         "Settlement of Losses in Risk Details - Conditions, attached hereto.")


def _fixed(rid, clause_id, page, text, value, field="OrderPct"):
    ir = {"template": "range_check", "rule_name": f"N{rid}",
          "params": {"field": field, "min": value, "max": value}}
    return {"rule_name": f"N{rid}", "ir": ir, "template": "range_check",
            "rule_spec": {"ir": ir, "compiled_sql": "SELECT 1", "referral": False},
            "rule_status": "active", "is_referral": False,
            "source_clause_id": clause_id, "source_page_number": page,
            "source_verbatim_text": text, "rule_description": "",
            "compiled_sql": "SELECT 1", "_id": rid}


def _statuses(out):
    return {r["ir"]["params"]["min"]: r.get("rule_status") for r in out}


def test_a_quoted_value_beats_one_multiplied_out_of_the_clause():
    out = _consolidate_cross_clause_column_rules(
        [_fixed(1, 11, 2, _TREATY, 45.45), _fixed(2, 44, 14, _ORDER, 25.0005)],
        SCHEMA)
    assert _statuses(out) == {45.45: "active", 25.0005: "needs_review"}, out


def test_a_percentage_that_only_states_a_money_amounts_basis_loses():
    out = _consolidate_cross_clause_column_rules(
        [_fixed(1, 11, 2, _TREATY, 45.45), _fixed(2, 19, 3, _CASH, 100)], SCHEMA)
    assert _statuses(out) == {45.45: "active", 100: "needs_review"}, out


def test_the_basis_percentage_loses_even_when_its_clause_comes_first():
    out = _consolidate_cross_clause_column_rules(
        [_fixed(1, 3, 1, _CASH, 100), _fixed(2, 40, 9, _TREATY, 45.45)], SCHEMA)
    assert _statuses(out) == {45.45: "active", 100: "needs_review"}, out


def test_the_paused_rule_says_what_the_conflict_is():
    out = _consolidate_cross_clause_column_rules(
        [_fixed(1, 11, 2, _TREATY, 45.45), _fixed(2, 44, 14, _ORDER, 25.0005)],
        SCHEMA)
    paused = [r for r in out if r.get("rule_status") == "needs_review"][0]
    desc = paused["rule_description"]
    assert "OrderPct" in desc and "45.45" in desc and "25.0005" in desc, desc
    assert paused["rule_spec"]["consolidation"]["status"] == "needs_review"


def test_the_same_value_restated_is_not_a_conflict():
    out = _consolidate_cross_clause_column_rules(
        [_fixed(1, 11, 2, _TREATY, 45.45), _fixed(2, 51, 16, _TREATY, 45.45)],
        SCHEMA)
    assert all(r.get("rule_status") == "active" for r in out), out


def test_exact_values_on_different_columns_are_left_alone():
    out = _consolidate_cross_clause_column_rules(
        [_fixed(1, 11, 2, _TREATY, 45.45),
         _fixed(2, 19, 3, _CASH, 500000, field="LimitEEC100Pct")], SCHEMA)
    assert all(r.get("rule_status") == "active" for r in out), out


def test_a_real_range_is_not_treated_as_an_exact_value():
    lo = _fixed(1, 11, 2, _TREATY, 45.45)
    lo["ir"]["params"] = {"field": "OrderPct", "min": 0, "max": 100}
    lo["rule_spec"]["ir"] = lo["ir"]
    out = _consolidate_cross_clause_column_rules(
        [lo, _fixed(2, 44, 14, _ORDER, 25.0005)], SCHEMA)
    assert all(r.get("rule_status") == "active" for r in out), out


def test_two_exact_values_from_ONE_clause_are_left_to_the_per_clause_passes():
    out = _consolidate_cross_clause_column_rules(
        [_fixed(1, 11, 2, _TREATY, 45.45), _fixed(2, 11, 2, _TREATY, 25.0005)],
        SCHEMA)
    assert all(r.get("rule_status") == "active" for r in out), out


def test_referral_triggers_with_different_values_are_never_consolidated():
    a, b = _fixed(1, 11, 2, _TREATY, 45.45), _fixed(2, 44, 14, _ORDER, 25.0005)
    for r in (a, b):
        r["is_referral"] = True
        r["rule_spec"]["referral"] = True
    out = _consolidate_cross_clause_column_rules([a, b], SCHEMA)
    assert all(r.get("rule_status") == "active" for r in out), out


def test_differently_scoped_exact_values_are_complementary_not_conflicting():
    a, b = _fixed(1, 11, 2, _TREATY, 45.45), _fixed(2, 44, 14, _ORDER, 25.0005)
    a["ir"]["params"]["scope"] = {"Section": ["A"]}
    b["ir"]["params"]["scope"] = {"Section": ["B"]}
    out = _consolidate_cross_clause_column_rules([a, b], SCHEMA)
    assert all(r.get("rule_status") == "active" for r in out), out


# ── one limits list, one column, many ceilings ──────────────────────────────
# A contract's limits section lists a sub-limit per named coverage, but a
# bordereau carries ONE limit column for the policy — so every item of the list
# lands on that column and it ends up under eleven different ceilings at once.
# Only one of them is the limit that column reports; the other ten flag rows that
# breach a sub-limit for a coverage the column never held.
#
# Coverage names and headings below are INVENTED. The pass reads the bound out of
# each rule's own IR and recognises the list from the heading/page/type the
# extraction recorded — it holds no vocabulary of coverages or limit wordings,
# which `test_bound_grouping_is_independent_of_wording` pins.

_LIMITS_HEADING = "COVER AND LIMITS OF LIABILITY"


def _cap(rid, clause_id, text, value, *, field="Aggregate Limit",
         template="max_limit", heading=_LIMITS_HEADING, page=5,
         clause_type="sublimit", group_by=("Policy Number",), floor=False,
         referral=False, scope=None):
    params = {"field": field}
    params["min" if floor else "max"] = value
    if template == "aggregate_cap":
        params["aggregation"] = "sum"
        if group_by:
            params["group_by"] = list(group_by)
    if scope is not None:
        params["scope"] = scope
    ir = {"template": template, "rule_name": f"L{rid}", "params": params}
    return {"rule_name": f"L{rid}", "ir": ir, "template": template,
            "rule_spec": {"ir": ir, "compiled_sql": "SELECT 1", "referral": referral},
            "rule_status": "active", "is_referral": referral,
            "source_clause_id": clause_id, "source_page_number": page,
            "source_section_header": heading, "source_clause_type": clause_type,
            "source_verbatim_text": text, "rule_description": "",
            "compiled_sql": "SELECT 1", "_id": rid}


_SUBLIMITS = [                       # (coverage, "each claim", aggregate)
    ("Professional Liability", 1_000_000, 3_000_000),
    ("Medical Expense", 5_000, 25_000),
    ("Policy Aggregate", None, 10_000_000),
    ("Evacuation Expense", 50_000, 100_000),
    ("Damage to Residents Property", 10_000, 20_000),
]


def _sublimit_rules(template="max_limit"):
    return [_cap(i, 20 + i, f"{name}: ${agg:,} aggregate;", agg, template=template)
            for i, (name, _each, agg) in enumerate(_SUBLIMITS)]


def test_competing_sublimits_leave_only_the_widest_enforced():
    out = _consolidate_cross_clause_column_rules(_sublimit_rules(), SCHEMA)
    active = [r for r in out if r["rule_status"] == "active"]
    assert len(active) == 1, [(r["rule_name"], r["rule_status"]) for r in out]
    assert active[0]["ir"]["params"]["max"] == 10_000_000
    # Nothing is dropped, and every paused rule keeps its own bound intact so it
    # can be re-enabled once a human says which item the column reports.
    assert len(out) == len(_SUBLIMITS)
    assert sorted(r["ir"]["params"]["max"] for r in out) == sorted(
        agg for _n, _e, agg in _SUBLIMITS)


def test_the_paused_sublimits_say_what_the_conflict_is():
    out = _consolidate_cross_clause_column_rules(_sublimit_rules(), SCHEMA)
    paused = [r for r in out if r["rule_status"] == "needs_review"]
    assert len(paused) == len(_SUBLIMITS) - 1
    for r in paused:
        desc = r["rule_description"]
        assert "Aggregate Limit" in desc and "10000000" in desc, desc
        assert r["rule_spec"]["consolidation"]["status"] == "needs_review"


def test_a_per_row_limit_and_a_per_policy_total_are_reconciled_together():
    """One list's items can compile to different templates — a per-row max_limit
    for one, a per-policy aggregate_cap for the next. Both say "this policy's
    figure may not exceed X", so they are weighed against each other."""
    rules = [_cap(1, 21, "Policy Aggregate: $10,000,000 aggregate;", 10_000_000),
             _cap(2, 22, "Damage to Residents Property: $20,000 aggregate;",
                  20_000, template="aggregate_cap")]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    active = [r for r in out if r["rule_status"] == "active"]
    assert len(active) == 1 and active[0]["ir"]["params"]["max"] == 10_000_000


def test_a_portfolio_total_is_never_weighed_against_a_per_policy_ceiling():
    """"$1M per policy; $100M in the aggregate" are two real requirements. The
    ungrouped sum measures the whole book, the ceiling measures one policy —
    pausing either would drop a check the contract asks for."""
    rules = [_cap(1, 21, "Each policy: $1,000,000;", 1_000_000),
             _cap(2, 22, "In the aggregate: $100,000,000;", 100_000_000,
                  template="aggregate_cap", group_by=())]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out), out


def test_a_floor_never_cancels_a_ceiling():
    rules = [_cap(1, 21, "Maximum: $10,000,000;", 10_000_000),
             _cap(2, 22, "Minimum: $25,000;", 25_000, floor=True)]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out), out


def test_competing_floors_keep_the_lowest():
    """The weakest bound is the widest one — for a floor that is the SMALLEST."""
    rules = [_cap(1, 21, "Minimum premium: $25,000;", 25_000, floor=True),
             _cap(2, 22, "Minimum premium: $5,000;", 5_000, floor=True),
             _cap(3, 23, "Minimum premium: $50,000;", 50_000, floor=True)]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    active = [r for r in out if r["rule_status"] == "active"]
    assert len(active) == 1 and active[0]["ir"]["params"]["min"] == 5_000


def test_ceilings_from_different_lists_are_left_alone():
    """Two limits stated in different sections are two requirements, not one
    list — reconciling them would silently drop a real check."""
    rules = [_cap(1, 21, "Policy Aggregate: $10,000,000;", 10_000_000),
             _cap(2, 60, "Any one risk: $250,000;", 250_000,
                  heading="SPECIAL ACCEPTANCES", page=9, clause_type="limit")]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out), out


def test_a_lists_own_lead_in_clause_is_not_one_of_its_items():
    """The lead-in sentence sits under the same heading on the same page, but
    extraction types it differently — which is what keeps it out of the group."""
    rules = _sublimit_rules() + [
        _cap(99, 19, "the maximum policy limits for Policies shall not exceed "
                     "the following", 275_000_000, clause_type="limit")]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    lead_in = [r for r in out if r["_id"] == 99][0]
    assert lead_in["rule_status"] == "active", lead_in


def test_ceilings_on_different_columns_are_left_alone():
    rules = [_cap(1, 21, "Policy Aggregate: $10,000,000;", 10_000_000),
             _cap(2, 22, "Each claim: $1,000,000;", 1_000_000,
                  field="Each Claim Limit")]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out), out


def test_differently_scoped_ceilings_are_complementary_not_conflicting():
    rules = [_cap(1, 21, "Section A: $10,000,000;", 10_000_000,
                  scope={"Section": ["A"]}),
             _cap(2, 22, "Section B: $20,000;", 20_000, scope={"Section": ["B"]})]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out), out


def test_the_same_ceiling_restated_is_not_a_conflict():
    rules = [_cap(1, 21, "Policy Aggregate: $10,000,000;", 10_000_000),
             _cap(2, 22, "Policy Aggregate: $10,000,000;", 10_000_000)]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out), out


def test_two_ceilings_from_ONE_clause_are_left_to_the_per_clause_passes():
    rules = [_cap(1, 21, "…$10,000,000…$20,000…", 10_000_000),
             _cap(2, 21, "…$10,000,000…$20,000…", 20_000)]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out), out


def test_referral_ceilings_are_never_consolidated():
    rules = [_cap(1, 21, "Refer above $10,000,000;", 10_000_000, referral=True),
             _cap(2, 22, "Refer above $20,000;", 20_000, referral=True)]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out), out


def test_a_two_sided_band_is_not_a_competing_bound():
    """A band narrows rather than contradicts, so it stays out of the family."""
    band = _cap(1, 21, "between $1 and $9;", 9)
    band["ir"]["params"] = {"field": "Aggregate Limit", "min": 1, "max": 9}
    band["rule_spec"]["ir"] = band["ir"]
    out = _consolidate_cross_clause_column_rules(
        [band, _cap(2, 22, "Policy Aggregate: $10,000,000;", 10_000_000)], SCHEMA)
    assert all(r["rule_status"] == "active" for r in out), out


def test_a_consistency_invariant_is_not_weighed_against_a_money_limit():
    """distinct_count == 1 means "this must not change across the policy's rows".
    Reading its 1 as a ceiling would pit it against every real limit."""
    inv = _cap(1, 21, "Policy Effective Date does not change", 1,
               template="aggregate_cap")
    inv["ir"]["params"]["aggregation"] = "distinct_count"
    out = _consolidate_cross_clause_column_rules(
        [inv, _cap(2, 22, "Policy Aggregate: $10,000,000;", 10_000_000,
                   template="aggregate_cap")], SCHEMA)
    assert all(r["rule_status"] == "active" for r in out), out


def test_bound_grouping_is_independent_of_wording():
    """The grouping holds no vocabulary. The heading here is not English and the
    clause texts name nothing real; the pass still finds the list, because all it
    compares is where the clauses came from."""
    rules = [_cap(1, 21, "Qx7 zzzz: 400;", 400, heading="Vvvv Nnnn Plph"),
             _cap(2, 22, "Mmmm kkkk: 900;", 900, heading="Vvvv Nnnn Plph"),
             _cap(3, 23, "Plph gggg: 50;", 50, heading="Vvvv Nnnn Plph")]
    out = _consolidate_cross_clause_column_rules(rules, SCHEMA)
    active = [r for r in out if r["rule_status"] == "active"]
    assert len(active) == 1 and active[0]["ir"]["params"]["max"] == 900


def test_headingless_ceilings_fall_back_to_the_shared_lead_in():
    """With no heading recorded, the only evidence left is the lead-in the
    clauses quote — present, they are one list; absent, they are left alone."""
    lead = "the maximum policy limits shall not exceed the following: "
    siblings = [_cap(1, 21, f"{lead}1. Alpha: $10,000,000;", 10_000_000,
                     heading=None),
                _cap(2, 22, f"{lead}2. Beta: $20,000;", 20_000, heading=None)]
    out = _consolidate_cross_clause_column_rules(siblings, SCHEMA)
    active = [r for r in out if r["rule_status"] == "active"]
    assert len(active) == 1 and active[0]["ir"]["params"]["max"] == 10_000_000

    strangers = [_cap(1, 21, "Policy Aggregate: $10,000,000;", 10_000_000,
                      heading=None),
                 _cap(2, 22, "An entirely unrelated sentence: $20,000.", 20_000,
                      heading=None)]
    out = _consolidate_cross_clause_column_rules(strangers, SCHEMA)
    assert all(r["rule_status"] == "active" for r in out), out


if __name__ == "__main__":
    import traceback
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
    print("ALL PASSED" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
