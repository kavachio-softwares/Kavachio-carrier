"""
test_limit_clause_value_column.py
─────────────────────────────────
A contract LIMIT (per-auto, terminal, per-policy) must not be checked against a
sum-insured / insured-value column: that column holds what is insured, so the
rule would flag nearly every row for a limit nobody breached. Such a binding goes
to review; a limit bound to a limit column is left alone.

Pure — see _offline.py (no DB).

Run:  python -m pytest contract_upload_services/tests/test_limit_clause_value_column.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _offline                                                  # noqa: E402,F401

import pytest                                                    # noqa: E402

from contract_upload_services import rule_normalizer as rn       # noqa: E402


class _Schema:
    def __init__(self, fields):
        self.template_fields = fields


FIELDS = [
    {"name": "Sum Insured Amount", "canonical_field": "policy_sum_insured_amount"},
    {"name": "Fleet Value", "canonical_field": "risk_location_total_insured_value"},
    {"name": "Vehicle Limit", "canonical_field": "coverage_limit_amount"},
    {"name": "Per Occurrence", "canonical_field": "coverage_occurrence_amount"},
]


def _ir(field, name="Per Vehicle Limit", template="max_limit"):
    return {"template": template, "rule_name": name, "params": {"field": field, "max": 250000}}


@pytest.mark.parametrize("field", ["Sum Insured Amount", "Fleet Value"])
def test_a_limit_on_an_insured_value_column_is_refused(field):
    reason = rn._limit_clause_on_value_column(_ir(field), _Schema(FIELDS))
    assert reason and field in reason


@pytest.mark.parametrize("field", ["Vehicle Limit", "Per Occurrence"])
def test_a_limit_on_any_other_column_passes(field):
    assert rn._limit_clause_on_value_column(_ir(field), _Schema(FIELDS)) is None


def test_a_non_limit_clause_on_sum_insured_passes():
    ir = _ir("Sum Insured Amount", name="Maximum Sum Insured per Risk")
    assert rn._limit_clause_on_value_column(ir, _Schema(FIELDS)) is None


def test_only_threshold_templates_are_judged():
    ir = _ir("Sum Insured Amount", template="required_field")
    assert rn._limit_clause_on_value_column(ir, _Schema(FIELDS)) is None


@pytest.mark.parametrize("name", ["TIV Limit per Location", "Maximum Total Insured Value Limit"])
def test_a_cap_on_insured_value_itself_passes(name):
    fields = FIELDS + [{"name": "Total Insured Value", "canonical_field": "risk_location_total_insured_value"}]
    assert rn._limit_clause_on_value_column(_ir("Total Insured Value", name=name), _Schema(fields)) is None


@pytest.mark.parametrize("field", ["Sum_Insured_Limit", "SumInsuredLimit"])
def test_limit_word_is_read_in_snake_and_camel_case(field):
    fields = FIELDS + [{"name": field, "canonical_field": "coverage_limit_amount"}]
    assert rn._limit_clause_on_value_column(_ir(field), _Schema(fields)) is None
