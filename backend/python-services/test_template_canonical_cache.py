"""Building a template from the same columns twice maps them the same way.

exporter._call_gemini_for_chunk (the "which data-model field is this column"
call behind Create BDX template) stores a finished answer in ai_cache and serves
it to the next identical request. A cut-off or unreadable answer is not stored.

Offline: the gateway and ai_cache are replaced, so nothing reaches the network or
the database.

Run:  pytest test_template_canonical_cache.py
"""
import json
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import ai_cache
import exporter


class _Cache:
    def __init__(self):
        self.rows, self.puts = {}, []

    def get(self, kind, key, refresh=False):
        return None if refresh else json.loads(json.dumps(self.rows.get((kind, key)))) \
            if (kind, key) in self.rows else None

    def put(self, kind, key, payload, tenant_id=None):
        self.puts.append(kind)
        self.rows[(kind, key)] = json.loads(json.dumps(payload))


@pytest.fixture
def cache(monkeypatch):
    c = _Cache()
    monkeypatch.setattr(ai_cache, "get", c.get)
    monkeypatch.setattr(ai_cache, "put", c.put)
    return c


def _gateway(answers, finish="FinishReason.STOP"):
    calls = []

    def invoke(kwargs, label=None, gen_client=None):
        calls.append(kwargs)
        text = answers[min(len(calls) - 1, len(answers) - 1)]
        return SimpleNamespace(text=text, usage_metadata=None,
                               candidates=[SimpleNamespace(finish_reason=finish)])
    return invoke, calls


SHEET = {"sheet_name": "Risk", "row_strategy": "policy"}
COLS = [{"column_index": 0, "column_name": "Policy Number", "samples": []},
        {"column_index": 1, "column_name": "Reinsurance basis", "samples": []}]
A = json.dumps({"row_strategy": "policy", "columns": [{"index": 1, "field": "contract_type"}]})
B = json.dumps({"row_strategy": "policy", "columns": [{"index": 1, "field": "coverage_claims_basis"}]})


def test_the_same_columns_get_the_stored_answer(cache, monkeypatch):
    invoke, calls = _gateway([A, B])
    monkeypatch.setattr(exporter, "_gateway_invoke", invoke)
    first = exporter._call_gemini_for_chunk(None, SHEET, COLS, 0, {})
    second = exporter._call_gemini_for_chunk(None, SHEET, COLS, 0, {})
    assert first == second == json.loads(A)
    assert len(calls) == 1
    assert cache.puts == ["template_canonical"]


def test_different_columns_or_model_ask_again(cache, monkeypatch):
    invoke, calls = _gateway([A, B, A])
    monkeypatch.setattr(exporter, "_gateway_invoke", invoke)
    exporter._call_gemini_for_chunk(None, SHEET, COLS, 0, {})
    exporter._call_gemini_for_chunk(None, SHEET, COLS[:1], 0, {})
    monkeypatch.setenv("KAVACHIO_MODEL_TEMPLATE_MAPPING", "gemini-2.5-pro")
    exporter._call_gemini_for_chunk(None, SHEET, COLS, 0, {})
    assert len(calls) == 3


def test_a_cut_off_answer_is_not_stored(cache, monkeypatch):
    invoke, calls = _gateway([A], finish="FinishReason.MAX_TOKENS")
    monkeypatch.setattr(exporter, "_gateway_invoke", invoke)
    exporter._call_gemini_for_chunk(None, SHEET, COLS, 0, {})
    exporter._call_gemini_for_chunk(None, SHEET, COLS, 0, {})
    assert cache.puts == []
    assert len(calls) == 2
