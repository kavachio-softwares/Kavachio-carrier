"""
test_declared_type_value_kind.py
────────────────────────────────
A template built from a standard spec declares each column's data type but
carries no sample values. infer_value_kind ignored the type, so every sample-less
amount column was shown to the mapper as `text/code` and a limit clause could
not bind to its own limit column. Pinned here with synthetic columns:

  * no samples + a declared numeric / date type → that kind;
  * samples present → the sample-driven kind, whatever the declared type;
  * declared "string" or no type → today's name-based kind;
  * the declared type survives dedup and reaches the rendered prompt block.

Pure — see _offline.py (no DB) — and no model calls.

Run:  python -m pytest contract_upload_services/tests/test_declared_type_value_kind.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _offline                                                  # noqa: E402,F401

import pytest                                                    # noqa: E402

from contract_upload_services.prompt_builder import (            # noqa: E402
    _template_fields_block, dedup_template_fields, infer_value_kind,
)


@pytest.mark.parametrize("name, data_type, kind", [
    ("Widget Cap Limit", "decimal", "money"),
    ("Widget Cap Limit", "Decimal", "money"),
    ("Widget Cap Limit", "currency", "money"),
    ("Widget Share %", "decimal", "percentage (0–100)"),
    ("Widget Share", "percent", "percentage (0–100)"),
    ("Widget Count", "integer", "number"),
    ("Widget Amount", "int", "money"),
    ("Widget Start", "date", "date"),
    ("Widget Start", "datetime", "date"),
])
def test_no_samples_uses_declared_type(name, data_type, kind):
    assert infer_value_kind(name, [], data_type) == kind
    # Blank / "nan" cells are no evidence either.
    assert infer_value_kind(name, ["", " ", "nan"], data_type) == kind


@pytest.mark.parametrize("name, samples, data_type, kind", [
    ("Widget Cap Limit", ["ABC", "XYZ"], "decimal", "text/code"),
    ("Widget Cap Limit", ["0.2", "0.5"], "decimal", "fraction (0–1)"),
    ("Widget Cap Limit", ["5000000"], "string", "money"),
    ("Widget Code", ["12", "40"], "date", "number"),
])
def test_samples_outrank_declared_type(name, samples, data_type, kind):
    assert infer_value_kind(name, samples, data_type) == kind
    assert infer_value_kind(name, samples) == kind


@pytest.mark.parametrize("name", [
    "Widget Cap Limit", "Widget Share %", "Widget Amount", "Widget Start Date"])
def test_string_or_untyped_keeps_name_based_kind(name):
    today = infer_value_kind(name, [])
    assert infer_value_kind(name, [], "string") == today
    assert infer_value_kind(name, [], None) == today
    assert infer_value_kind(name, [], "something-unknown") == today


def test_untyped_limit_name_is_still_text():
    assert infer_value_kind("Widget Cap Limit", []) == "text/code"


def test_declared_type_survives_dedup_and_renders_kind():
    fields = [
        {"name": "Widget Cap Limit", "sheet": "S1", "samples": []},
        {"name": "Widget Cap Limit", "sheet": "S2", "samples": [],
         "data_type": "decimal"},
        {"name": "Widget Label", "sheet": "S1", "samples": []},
    ]
    deduped = dedup_template_fields(fields)
    by_name = {f["name"]: f for f in deduped}
    assert by_name["Widget Cap Limit"]["data_type"] == "decimal"
    assert "data_type" not in by_name["Widget Label"]
    block = _template_fields_block(deduped)
    assert "'Widget Cap Limit' (kind: money;" in block
    assert "'Widget Label' (kind: text/code;" in block
