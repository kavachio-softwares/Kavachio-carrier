"""Mid-term change — the rule plan and the dates (no DB, no network).

The case the carrier tests on screen is an UPLOADED contract whose "Commission
Schedule" check (read from its PDF) is a range_check on "Commission Rate" with
min = max = 23.5, on a template whose data-model tags are wrong: "Commission
Rate" is tagged as the commission AMOUNT and three other columns as the
commission percent. A mid-term change moving commission to 25.5 must:

  · read the current commission (23.5) off that check — and only as the
    agreed commission, not also as a cap;
  · close that check and open a version of it at 25.5 (same template, same
    column, same severity, its message moved), and touch nothing else;
  · never guess: two checks holding one term in one rule set is a question
    for the carrier, answered by `choices`;
  · close a check for a removed term, and leave alone a check it can't move.

contract_rules and contract_types are pure and used as they are; the compiler
is a stand-in that records what it was asked to compile.

    python -m pytest test_mid_term_change.py
"""
from __future__ import annotations

import datetime as dt
import json

import contract_rules as crr
import contract_types as ct
import mid_term_change as mt

D = dt.date
AUG16 = D(2026, 8, 16)

# Template 345's commission columns, as tagged on the shared DB.
FIELDS = [
    {"name": "Commission Rate", "sheet": "Sheet 2",
     "canonical_field": "premium_transaction_commission_amount"},
    {"name": "Commission Amount", "sheet": "Sheet 2", "canonical_field": None},
    {"name": "IPRM APD", "sheet": "Sheet 2",
     "canonical_field": "premium_transaction_commission_percent"},
    {"name": "IPRM MTC", "sheet": "Sheet 2",
     "canonical_field": "premium_transaction_commission_percent"},
    {"name": "QS Reinsurers Participation%", "sheet": "Sheet 2",
     "canonical_field": "premium_transaction_commission_percent"},
    {"name": "TransactionDate", "sheet": "Sheet 2",
     "canonical_field": "premium_transaction_effective_date"},
]


def _rule(rule_id, template, params, *, name="Commission Schedule", status="active",
          tag=None, source=None, limit=None, **extra):
    spec = {"ir": {"template": template, "params": params, "rule_name": name,
                   "error_message": f"Commission rate is not {params.get('max')}%."},
            "compiled_sql": "SELECT 1"}
    if source:
        spec.update({"source": source, "limit": limit, "operand": params.get("max")})
    return {"rule_id": rule_id, "rule_name": name, "rule_spec": spec, "rule_status": status,
            "output_template_id": tag, "error_message": f"Commission rate is not {params.get('max')}%.",
            "rule_description": f"The commission rate must be {params.get('max')}%.",
            "effective_from": D(2026, 10, 14), **extra}


R4131 = _rule(4131, "range_check", {"field": "Commission Rate", "min": 23.5, "max": 23.5})
CROSS = _rule(4136, "cross_field_math", {"operator": "*", "left_field": "Gross Premium",
                                         "right_field": "Commission Rate",
                                         "result_field": "Commission Amount"},
              name="Commission Amount equals Gross Premium × Commission Rate")

compiled: list = []


def _compile_for(_set):
    def run(ir):
        compiled.append(ir)
        return "SQL " + json.dumps(ir["params"], sort_keys=True)
    return run


def _plan(changes, rules, choices=None, fields=FIELDS, build_new=None):
    return mt.plan_rules(
        changes=changes, rules=rules, fields_by_set={None: fields}, effective=AUG16,
        limits_spec=ct.AGREED_LIMITS, limit_columns=crr.LIMIT_COLUMNS,
        limit_operators=crr.LIMIT_OPERATORS, find_column=crr._find_column,
        build_new=build_new or (lambda key, entry, set_id: None),
        compile_for_set=_compile_for, choices=choices)


def _change(key, old, new, kind="amended"):
    return {"key": key, "kind": kind, "from": old, "to": new,
            "severity_from": "critical", "severity_to": "critical"}


# ── the effective date ───────────────────────────────────────────────────────
def test_effective_date_bounds():
    inc, exp = D(2026, 1, 1), D(2026, 12, 31)
    assert mt.effective_date_errors(AUG16, inc, exp) == {}
    assert mt.effective_date_errors(D(2026, 12, 31), inc, exp) == {}
    assert "after the contract does" in mt.effective_date_errors(inc, inc, exp)["effective_from"]
    assert "ends on 31 Dec 2026" in mt.effective_date_errors(D(2027, 1, 1), inc, exp)["effective_from"]
    assert "already applies from" in mt.effective_date_errors(
        D(2026, 8, 1), inc, exp, latest_live=AUG16)["effective_from"]
    assert mt.effective_date_errors(None, inc, exp)


def test_boundary_says_which_side_the_15th_falls():
    b = mt.boundary_words(AUG16)
    assert b["old_until"] == "15 Aug 2026" and b["new_from"] == "16 Aug 2026"
    assert "up to and including 15 Aug 2026" in b["sentence"]


# ── reading an uploaded contract's terms off its checks ──────────────────────
def test_commission_read_off_the_check_despite_wrong_tags():
    terms = mt.current_terms_from_rules(
        [R4131, CROSS], FIELDS, ct.AGREED_LIMITS, crr.LIMIT_COLUMNS, crr.LIMIT_OPERATORS,
        crr._find_column, AUG16)
    assert terms["commission_pct"]["value"] == "23.5"
    # min = max is the agreed commission, not also a cap
    assert "commission_max_pct" not in terms


def test_a_closed_check_is_not_read_as_current():
    closed = {**R4131, "effective_to": D(2026, 8, 16)}
    terms = mt.current_terms_from_rules(
        [closed], FIELDS, ct.AGREED_LIMITS, crr.LIMIT_COLUMNS, crr.LIMIT_OPERATORS,
        crr._find_column, D(2026, 9, 1))
    assert "commission_pct" not in terms


def test_term_columns_add_the_named_column_to_the_tagged_one():
    cols = mt.term_columns("commission_pct", FIELDS, crr.LIMIT_COLUMNS, crr._find_column)
    assert cols == ["Commission Rate"]   # the tagged three are ambiguous, so not bound


# ── the plan ─────────────────────────────────────────────────────────────────
def test_uploaded_commission_change_moves_exactly_one_check():
    compiled.clear()
    plan = _plan([_change("commission_pct", 23.5, 25.5)], [R4131, CROSS])
    assert [c["rule_id"] for c in plan["close"]] == [4131]
    assert len(plan["open"]) == 1
    nv = plan["open"][0]
    assert nv["replaces"] == 4131 and nv["rule_name"] == "Commission Schedule"
    assert nv["rule_spec"]["ir"]["rule_name"] == "Commission Schedule"
    assert nv["rule_spec"]["ir"]["template"] == "range_check"
    assert nv["rule_spec"]["ir"]["params"] == {"field": "Commission Rate", "min": 25.5, "max": 25.5}
    assert nv["rule_spec"]["compiled_sql"].startswith("SQL ")
    assert nv["error_message"] == "Commission rate is not 25.5%."
    assert nv["rule_description"] == "The commission rate must be 25.5%."
    assert plan["needs_choice"] == [] and plan["unchecked"] == []
    # the original rule dict is untouched
    assert R4131["rule_spec"]["ir"]["params"]["max"] == 23.5


def test_authored_term_check_is_found_by_its_key():
    authored = _rule(10, "range_check", {"field": "Commission %", "min": 15, "max": 15},
                     name="Commission", source="contract_terms", limit="commission_pct")
    plan = _plan([_change("commission_pct", 15, 17)], [authored],
                 fields=[{"name": "Commission %", "sheet": "S",
                          "canonical_field": "premium_transaction_commission_percent"}])
    assert [c["rule_id"] for c in plan["close"]] == [10]
    assert plan["open"][0]["rule_spec"]["ir"]["params"]["max"] == 17
    assert plan["open"][0]["rule_spec"]["operand"] == 17


def test_two_checks_holding_the_term_is_a_question_not_a_guess():
    twin = _rule(4999, "range_check", {"field": "Commission Rate", "min": 23.5, "max": 23.5},
                 name="Commission (schedule B)")
    plan = _plan([_change("commission_pct", 23.5, 25.5)], [R4131, twin])
    assert plan["close"] == [] and plan["open"] == []
    assert plan["needs_choice"][0]["key"] == "commission_pct"
    assert {c["rule_id"] for c in plan["needs_choice"][0]["candidates"]} == {4131, 4999}
    picked = _plan([_change("commission_pct", 23.5, 25.5)], [R4131, twin],
                   choices={"commission_pct": [4999]})
    assert [c["rule_id"] for c in picked["close"]] == [4999]
    assert picked["open"][0]["replaces"] == 4999


def test_removed_term_only_closes():
    plan = _plan([_change("commission_pct", 23.5, "", kind="removed")], [R4131])
    assert [c["rule_id"] for c in plan["close"]] == [4131] and plan["open"] == []


def test_a_range_the_term_cannot_move_is_reported_and_left_running():
    band = _rule(5, "range_check", {"field": "Commission Rate", "min": 20, "max": 30})
    plan = _plan([_change("commission_max_pct", 30, 28)], [band])
    # "may not exceed 30" sits in a band with a floor: not a cap on its own
    assert plan["close"] == [] and plan["open"] == []


def test_new_term_with_no_column_is_reported_not_invented():
    plan = _plan([_change("commission_pct", "", 25.5, kind="added")], [CROSS])
    assert plan["open"] == []
    assert plan["unchecked"][0]["key"] == "commission_pct"


def test_new_term_with_a_column_gets_a_new_check():
    built = {"rule_name": "Commission", "rule_spec": {"ir": {}}, "error_message": "x",
             "rule_description": "y", "severity": "critical", "canonical_target": {}}
    plan = _plan([_change("commission_pct", "", 25.5, kind="added")], [CROSS],
                 build_new=lambda key, entry, set_id: built)
    assert plan["open"][0]["replaces"] is None and plan["open"][0]["rule_name"] == "Commission"


def test_switched_off_check_stays_switched_off():
    off = {**R4131, "rule_status": "disabled"}
    plan = _plan([_change("commission_pct", 23.5, 25.5)], [off])
    assert plan["open"][0]["rule_status"] == "disabled"


def test_wording_only_terms_produce_no_plan():
    plan = _plan([_change("profit_commission_pct", 5, 6)], [R4131])
    assert plan == {"close": [], "open": [], "unchecked": [], "needs_choice": []}


def test_new_version_is_named_for_the_new_value():
    # Harborline 3 Nov 2026: the 30% check went live named "…must be 25%".
    named = _rule(4487, "range_check", {"field": "Commission Rate", "min": 25, "max": 25},
                  name="Administrator Commission Percentage must be 25%")
    plan = _plan([_change("commission_pct", 25, 30)], [named])
    nv = plan["open"][0]
    assert nv["rule_name"] == "Administrator Commission Percentage must be 30%"
    assert nv["rule_spec"]["ir"]["rule_name"] == nv["rule_name"]
    assert nv["error_message"] == "Commission rate is not 30%."
    assert plan["close"][0]["rule_name"] == "Administrator Commission Percentage must be 25%"


def test_swap_value_text_moves_only_the_value():
    assert mt.swap_value_text("8) Commissions Schedule: 23.5% from 2023", 23.5, 25.5) == \
        "8) Commissions Schedule: 25.5% from 2023"
    assert mt.swap_value_text("23.50 and 123.5", 23.5, 25.5) == "25.5 and 123.5"
    assert mt.swap_value_text(None, 1, 2) is None


def test_fingerprint_moves_when_the_check_does():
    a = mt.rule_fingerprint(R4131)
    assert a == mt.rule_fingerprint(dict(R4131))
    assert a != mt.rule_fingerprint({**R4131, "rule_status": "disabled"})
    assert a != mt.rule_fingerprint({**R4131, "effective_to": D(2026, 8, 16)})


# ── clauses ─────────────────────────────────────────────────────────────────

CLAUSES = [
    {"ref": "c:7944", "clause_id": 7944, "title": "Commissions Schedule",
     "section": "Commissions Schedule", "text": "8) Commissions Schedule: 23.5%"},
    {"ref": "c:7945", "clause_id": 7945, "title": "Fees",
     "section": "Fees", "text": "9) Fees: not to exceed\n$1,500 per Policy."},
]


def test_clause_edits_become_changes_and_no_ops_drop_out():
    changes, errors = mt.clause_changes(CLAUSES, [
        {"ref": "c:7944", "action": "amended", "text": "8) Commissions Schedule: 25.5%"},
        # only the line break differs: layout, not a change
        {"ref": "c:7945", "action": "amended", "text": "9) Fees: not to exceed $1,500 per Policy."},
        {"action": "added", "title": "Flood", "section": "Exclusions",
         "text": "No risk in flood zone A."},
    ])
    assert errors == {}
    assert [(c["action"], c["clause_id"]) for c in changes] == [("amended", 7944), ("added", None)]
    am = changes[0]
    assert am["from_text"] == "8) Commissions Schedule: 23.5%"
    assert am["to_text"] == "8) Commissions Schedule: 25.5%"
    assert am["title"] == "Commissions Schedule"


def test_clause_edits_that_cannot_be_applied_are_refused_not_guessed():
    changes, errors = mt.clause_changes(CLAUSES, [
        {"ref": "c:9999", "action": "amended", "text": "x"},          # not this contract's
        {"action": "added", "title": "Empty", "text": "  "},           # heading, no words
        {"ref": "c:7944", "action": "amended", "text": ""},            # emptied, not deleted
        {"ref": "c:7945", "action": "removed"},
        {"ref": "c:7945", "action": "amended", "text": "again"},       # twice
    ])
    assert set(errors) == {"clause_0", "clause_1", "clause_2", "clause_4"}
    assert [(c["action"], c["clause_id"]) for c in changes] == [("removed", 7945)]
    assert changes[0]["from_text"].startswith("9) Fees")


def test_clause_change_lines_name_the_clause():
    changes, _ = mt.clause_changes(CLAUSES, [
        {"ref": "c:7944", "action": "amended", "text": "25.5%"},
        {"ref": "c:7945", "action": "removed"},
        {"action": "added", "title": "Flood", "text": "No flood zone A."}])
    assert [mt.clause_change_line(c) for c in changes] == [
        "Clause “Commissions Schedule” re-worded", "Clause “Fees” deleted",
        "New clause “Flood” added"]


def test_change_document_quotes_clause_changes_after_the_terms():
    import contract_wording as cw
    changes, _ = mt.clause_changes(CLAUSES, [
        {"ref": "c:7945", "action": "amended", "text": "9) Fees: not to exceed $2,000."},
        {"action": "added", "title": "Flood", "section": "Exclusions", "text": "No flood zone A."}])
    terms = cw.derive_endorsement_changes({"commission_pct": {"value": 23.5}},
                                          {"commission_pct": {"value": 25.5}})
    secs = cw.build_endorsement_sections(contract_name="C", number=1, effective_from="16 Aug 2026",
                                         changes=terms, clause_changes=changes)
    body = next(s["body"] for s in secs if s["key"] == "endorsement_changes")
    lines = body.split("\n")
    assert lines[0].startswith("1.  Commission") and "25.5%" in lines[0]
    assert lines[1] == "2.  The clause headed “Fees” is deleted and replaced by the following:"
    assert lines[2] == "9) Fees: not to exceed $2,000."
    assert lines[3].startswith("3.  The following clause is added under “Exclusions”")
    # clause changes alone still make a document
    assert cw.build_endorsement_sections(contract_name="C", number=1, effective_from=None,
                                         changes=[], clause_changes=changes)


def test_the_broker_queue_holds_exactly_the_broker_moves():
    # The broker's dashboard and contract list count an open change by
    # BROKER_STEP; _actions offers the broker the same steps. Agreeing comes
    # while it is with them, signing only once the carrier has signed.
    assert mt.BROKER_STEP == {mt.WITH_BROKER: "agree", mt.CARRIER_SIGNED: "sign"}
    assert set(mt.BROKER_STEP) <= set(mt.OPEN)
    # Every other open state is the carrier's move — none of it is the
    # broker's to see as waiting on them.
    for st in (mt.CHANGES_REQUESTED, mt.AGREED, mt.SIGNED, mt.PENDING):
        assert st in mt.OPEN and st not in mt.BROKER_STEP
    # Said from the broker's side for every state the carrier has words for.
    assert set(mt.BROKER_STATE_WORDS) == set(mt.STATE_WORDS)
    assert mt.BROKER_STATE_WORDS[mt.WITH_BROKER] == "For your review"
