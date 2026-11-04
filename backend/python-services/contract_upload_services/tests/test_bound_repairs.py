"""
test_bound_repairs.py
─────────────────────
Three rule-generation defects found on an uploaded binder (Program Schedule C,
contract 10809), pinned with the exact IRs the mapper produced:

  * a LIMIT clause read as "must equal" — "APD per Auto Limit = $200,000" checked
    as every row being exactly $200,000 (fix_limit_clause_exact_bounds), while a
    share the same kind of clause states ("Percentage of Total Risk: 100%") stays
    an exact value;
  * a bound one step past the clause's number — "backdating of more than five (5)
    days" kept as max = 6 (fix_bounds_off_by_one, and fix_backdating_period_fields
    reading N from the clause);
  * a strict date bound on the contract's own last / first day
    (fix_contract_period_date_bounds).

Pure — see _offline.py (no DB) — and no model calls.

Run:  python -m pytest contract_upload_services/tests/test_bound_repairs.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _offline                                                  # noqa: E402,F401

from contract_upload_services import validation_rule_generator as vrg  # noqa: E402


def _entry(clause_type, text, irs, intents=None):
    return {"clause": {"clause_id": 1, "clause_type": clause_type, "text": text},
            "classification": {"intents": intents if intents is not None
                               else [{"value": None} for _ in irs]},
            "engine": "ir", "candidates": irs}


def _exact(field, value, name):
    return {"template": "range_check",
            "params": {"field": field, "min": value, "max": value},
            "rule_name": name, "rule_description": f"The {name[:-len(' must be $200,000')]} must be $200,000.",
            "error_message": "The reported limit is not $200,000."}


# ── limit clause → ceiling ───────────────────────────────────────────────────

def test_sublimit_read_as_equal_becomes_a_ceiling():
    synth = [_entry("sublimit", "APD per Auto Limit = $200,000",
                    [_exact("APD  Unit Limit", 200000, "APD per Auto Limit must be $200,000")])]
    assert vrg.fix_limit_clause_exact_bounds(synth) == 1
    ir = synth[0]["candidates"][0]
    assert ir["template"] == "max_limit"
    assert ir["params"] == {"field": "APD  Unit Limit", "max": 200000}
    assert ir["rule_name"] == "APD per Auto Limit must not exceed $200,000"
    assert "above the contract limit of 200,000" in ir["error_message"]


def test_a_share_stated_in_a_limit_clause_stays_exact():
    text = "5) Percentage of Total Risk: 100% per layer per policy"
    for value in (100, 1.0):           # as written, and rescaled to a fraction
        synth = [_entry("limit", text, [_exact("QS Share", value, "Share must be 100%")])]
        assert vrg.fix_limit_clause_exact_bounds(synth) == 0
        assert synth[0]["candidates"][0]["template"] == "range_check"


def test_exact_value_outside_a_limit_clause_is_untouched():
    # A commission schedule is an exact rate — its clause is not a limit.
    synth = [_entry("other", "Commissions Schedule: 25% payable to Administrator",
                    [_exact("Commission Rate", 25, "Commission must be 25%")])]
    assert vrg.fix_limit_clause_exact_bounds(synth) == 0
    assert synth[0]["candidates"][0]["params"]["min"] == 25


def test_a_limit_clause_already_a_ceiling_is_untouched():
    ir = {"template": "max_limit", "params": {"field": "Pol Occ Limit", "max": 2500000},
          "rule_name": "Maximum Limit per Policy"}
    synth = [_entry("limit", "Maximum Limits: $2,500,000 per policy", [ir])]
    assert vrg.fix_limit_clause_exact_bounds(synth) == 0
    assert ir["params"] == {"field": "Pol Occ Limit", "max": 2500000}


# ── a bound one step past the stated number ──────────────────────────────────

def _backdating(max_=6):
    return {"template": "period_duration",
            "params": {"max": max_, "unit": "day", "start_field": "PolicyInception",
                       "end_field": "TransactionDate"},
            "rule_name": "Referral Required for Backdating Coverage Exceeding 5 Days"}


BACKDATING = ("Any backdating of coverage of more than five (5) days. "
              "All Referrals to Company are to be submitted in writing.")


def test_more_than_n_kept_as_n_plus_one_ceiling_is_moved_back():
    synth = [_entry("other", BACKDATING, [_backdating(6)],
                    intents=[{"operator": "min", "value": 5}])]
    assert vrg.fix_bounds_off_by_one(synth) == 1
    assert synth[0]["candidates"][0]["params"]["max"] == 5


def test_legitimate_readings_of_n_are_untouched():
    cases = [
        ("max_limit", {"field": "F", "max": 5}),        # up to 5
        ("max_limit", {"field": "F", "max": 4}),        # fewer than 5
        ("min_limit", {"field": "F", "min": 5}),        # at least 5
        ("min_limit", {"field": "F", "min": 6}),        # more than 5
        ("range_check", {"field": "F", "min": 6, "max": 6}),   # exact — not a bound
    ]
    for template, params in cases:
        ir = {"template": template, "params": dict(params)}
        synth = [_entry("other", "five (5)", [ir], intents=[{"value": 5}])]
        assert vrg.fix_bounds_off_by_one(synth) == 0, (template, params)
        assert ir["params"] == params


def test_floor_one_below_the_stated_number_is_moved_back():
    ir = {"template": "min_limit", "params": {"field": "Premium", "min": 499}}
    synth = [_entry("other", "minimum premium $500", [ir], intents=[{"value": 500}])]
    assert vrg.fix_bounds_off_by_one(synth) == 1
    assert ir["params"]["min"] == 500


def test_nothing_is_guessed_without_a_stated_whole_number():
    for intents in ([{"value": None}], [{"value": "five"}], [{"value": 5.5}], [{"value": True}],
                    [{"value": 5}, {"value": 7}]):        # misaligned with the candidates
        ir = _backdating(6)
        synth = [_entry("other", BACKDATING, [ir], intents=intents)]
        assert vrg.fix_bounds_off_by_one(synth) == 0
        assert ir["params"]["max"] == 6


def test_backdating_collapse_reads_n_from_the_clause_not_the_mapper():
    # The mapper's "minimum of 6" (its restatement of "more than 5") — the
    # collapse to a ceiling must land on the contract's 5, not on 6.
    ir = {"template": "period_duration",
          "params": {"min": 6, "unit": "day", "start_field": "A", "end_field": "B"},
          "rule_name": "Referral Required for Backdating Coverage Exceeding 5 Days"}
    synth = [_entry("other", BACKDATING, [ir], intents=[{"operator": "min", "value": 5}])]
    fields = [{"name": "PolicyInception"}, {"name": "TransactionDate"}]
    assert vrg.fix_backdating_period_fields(synth, fields) == 1
    assert ir["params"]["max"] == 5 and "min" not in ir["params"]


def test_backdating_collapse_without_a_stated_value_keeps_the_mapper_bound():
    ir = {"template": "period_duration",
          "params": {"min": 6, "unit": "day", "start_field": "A", "end_field": "B"},
          "rule_name": "Backdating"}
    synth = [_entry("other", BACKDATING, [ir])]
    fields = [{"name": "PolicyInception"}, {"name": "TransactionDate"}]
    vrg.fix_backdating_period_fields(synth, fields)
    assert ir["params"]["max"] == 6


# ── the contract's own first / last day ──────────────────────────────────────

def _date_bound(op, date, field="PolicyInception"):
    return {"template": "date_bound", "params": {"op": op, "date": date, "field": field},
            "rule_name": "Policy Inception Date must be before Program Schedule Expiration Date",
            "rule_description": "The policy inception date must be strictly before August 31, 2027.",
            "error_message": "Policy inception date {{pol_eff_dt}} is on or after the "
                             "Program Schedule Expiration Date of 2027-08-31."}


def test_strict_bound_on_the_contract_expiry_includes_the_last_day():
    ir = _date_bound("<", "2027-08-31")
    synth = [_entry("other", "Program Schedule Expiration Date: August 31, 2027", [ir])]
    assert vrg.fix_contract_period_date_bounds(
        synth, dt.date(2026, 9, 1), dt.date(2027, 8, 31)) == 1
    assert ir["params"]["op"] == "<="
    assert ir["rule_name"] == ("Policy Inception Date must be on or before "
                               "Program Schedule Expiration Date")
    assert "must be on or before August 31, 2027" in ir["rule_description"]
    assert "is after the Program Schedule" in ir["error_message"]


def test_strict_bound_on_the_contract_inception_includes_the_first_day():
    ir = {"template": "date_bound",
          "params": {"op": ">", "date": "2026-09-01", "field": "PolicyInception"},
          "rule_name": "Policy Inception must be after the inception date"}
    synth = [_entry("other", "Inception Date: September 1, 2026", [ir])]
    assert vrg.fix_contract_period_date_bounds(
        synth, dt.date(2026, 9, 1), dt.date(2027, 8, 31)) == 1
    assert ir["params"]["op"] == ">="
    assert ir["rule_name"] == "Policy Inception must be on or after the inception date"


def test_other_dates_and_inclusive_bounds_are_untouched():
    period = (dt.date(2026, 9, 1), dt.date(2027, 8, 31))
    for op, date in (("<", "2027-06-30"),     # a cut-off the contract states for something else
                     ("<=", "2027-08-31"),    # already inclusive
                     (">", "2027-08-31"),     # after the end — not a reading of the term
                     ("<", "2026-09-01")):    # before the start — likewise
        ir = _date_bound(op, date)
        synth = [_entry("other", "x", [ir])]
        assert vrg.fix_contract_period_date_bounds(synth, *period) == 0, (op, date)
        assert ir["params"]["op"] == op


def test_no_contract_dates_no_change():
    ir = _date_bound("<", "2027-08-31")
    assert vrg.fix_contract_period_date_bounds([_entry("other", "x", [ir])], None, None) == 0
    assert ir["params"]["op"] == "<"
