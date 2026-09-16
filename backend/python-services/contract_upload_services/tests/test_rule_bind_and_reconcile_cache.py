"""
test_rule_bind_and_reconcile_cache.py
─────────────────────────────────────
The same inputs must give the same rules and the same run results, and the model
calls behind them were measured NOT to repeat themselves. Three stored answers
make them repeat, and a model switch must invalidate stored answers:

  * rule_bind      — one Call-3 batch of contract intents → IR. Stored only when
                     every intent came back; library batches are left to
                     generic_bind; any change to the prompt misses.
  * var_reconcile  — the Process Bordereau "which data spellings are the same
                     entity" question, keyed on the question with its value lists
                     sorted, the tenant and the model; thinking is capped.
  * model_scoped keys on the pre-existing kinds — unchanged while the model is the
                     one they were stored under, different once it is not.

Pure — see _offline.py (no DB) — and every model call is a stub (no network).

Run:  python -m pytest contract_upload_services/tests/test_rule_bind_and_reconcile_cache.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _offline                                                  # noqa: E402,F401

import pytest                                                    # noqa: E402

import ai_cache                                                  # noqa: E402
from contract_upload_services import stage_b_synthesizer as sb   # noqa: E402
from contract_upload_services import variation_reconcile as vr   # noqa: E402
from contract_upload_services import validation_rule_generator as vrg  # noqa: E402


@pytest.fixture
def store(monkeypatch):
    monkeypatch.delenv("KAVACHIO_MODEL_VARIATION_RECONCILE", raising=False)
    return _offline.MemoryCache().install(monkeypatch)


# ── rule_bind: Call 3 contract batches ──────────────────────────────────────

FIELDS = [
    {"name": "Policy Limit", "sheet": "S1", "samples": ["1000000"]},
    {"name": "Commission %", "sheet": "S1", "samples": ["20"]},
]


def _items(clause_ids=(1, 2)):
    return [{"clause_id": cid, "intent_index": 0, "subject": f"subject {cid}",
             "operator": "<=", "value": 100 * cid, "scope": None,
             "severity": "warning", "is_referral": False,
             "rule_name": f"rule {cid}", "rule_description": "d",
             "error_message": "e", "clause_text": f"clause {cid} text"}
            for cid in clause_ids]


def _rows(items, field):
    return {"results": [{"clause_id": it["clause_id"], "intent_index": it["intent_index"],
                         "template": "max_limit",
                         "params": {"field": field, "max": it["value"]},
                         "rule_name": it["rule_name"]} for it in items]}


class _Model:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        a = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(a, Exception):
            raise a
        return a if isinstance(a, str) else json.dumps(a)


def test_a_fully_answered_batch_is_reused(store, monkeypatch):
    items = _items()
    model = _Model(_rows(items, "Policy Limit"), _rows(items, "Commission %"))
    monkeypatch.setattr(sb, "call_gemini", model)
    first = sb._run_mapping_batches(items, FIELDS, len(items))
    second = sb._run_mapping_batches(items, FIELDS, len(items))
    assert first == second
    assert {ir["params"]["field"] for ir in first.values()} == {"Policy Limit"}
    assert len(model.calls) == 1
    assert [k for k, _ in store.puts] == ["rule_bind"]


def test_a_reread_document_with_new_clause_ids_reuses_the_answer(store, monkeypatch):
    # Re-reading a document mints new clause ids for the same clauses. The stored
    # answer must still be found, and handed back under THIS run's ids.
    items = _items()
    reread = [{**it, "clause_id": it["clause_id"] + 7000} for it in items]
    model = _Model(_rows(items, "Policy Limit"), _rows(reread, "Commission %"))
    monkeypatch.setattr(sb, "call_gemini", model)
    first = sb._run_mapping_batches(items, FIELDS, len(items))
    second = sb._run_mapping_batches(reread, FIELDS, len(reread))
    assert len(model.calls) == 1
    assert set(second) == {(7001, 0), (7002, 0)}
    assert [second[(cid + 7000, 0)] for cid, _ in first] == list(first.values())


def test_the_request_uses_the_config_and_model_the_key_covers(store, monkeypatch):
    items = _items()
    model = _Model(_rows(items, "Policy Limit"))
    monkeypatch.setattr(sb, "call_gemini", model)
    sb._run_mapping_batches(items, FIELDS, len(items))
    sent = model.calls[0]
    assert (sent["temperature"], sent["seed"], sent["thinking_budget"],
            sent["max_output_tokens"], sent["model"]) == (
        0, sb.DETERMINISTIC_SEED, 16384, 65536, sb.EXTRACTION_MODEL)


@pytest.mark.parametrize("change", ["samples", "clause_text", "relaxed", "forced", "model"])
def test_anything_in_the_prompt_or_the_model_misses(store, monkeypatch, change):
    items = _items()
    model = _Model(_rows(items, "Policy Limit"))
    monkeypatch.setattr(sb, "call_gemini", model)
    sb._run_mapping_batches(items, FIELDS, len(items))
    fields, kw = FIELDS, {}
    if change == "samples":
        fields = [dict(FIELDS[0], samples=["2000000"]), FIELDS[1]]
    elif change == "clause_text":
        items = [dict(items[0], clause_text="reworded"), items[1]]
    elif change == "relaxed":
        kw = {"relaxed": True, "temperature": 0.4}
    elif change == "forced":
        kw = {"forced_field": "Policy Limit"}
    elif change == "model":
        monkeypatch.setattr(sb, "EXTRACTION_MODEL", "gemini-2.5-pro")
    sb._run_mapping_batches(items, fields, len(items), **kw)
    assert len(model.calls) == 2


def test_a_partial_batch_is_not_stored(store, monkeypatch):
    items = _items()
    partial = _rows(items[:1], "Policy Limit")          # intent of clause 2 missing
    model = _Model(partial, _rows(items, "Policy Limit"))
    monkeypatch.setattr(sb, "call_gemini", model)
    got = sb._run_mapping_batches(items, FIELDS, len(items))
    assert list(got) == [(1, 0)] and not store.puts
    sb._run_mapping_batches(items, FIELDS, len(items))
    assert len(model.calls) == 2 and len(store.puts) == 1


def test_a_declined_intent_is_an_answer_and_is_stored(store, monkeypatch):
    items = _items()
    rows = _rows(items, "Policy Limit")
    rows["results"][1].update(template=None, params={}, reason="no column")
    model = _Model(rows)
    monkeypatch.setattr(sb, "call_gemini", model)
    sb._run_mapping_batches(items, FIELDS, len(items))
    assert len(store.puts) == 1


def test_a_failed_call_is_not_stored(store, monkeypatch):
    items = _items()
    model = _Model(Exception("MAX_TOKENS"), _rows(items, "Policy Limit"))
    monkeypatch.setattr(sb, "call_gemini", model)
    assert sb._run_mapping_batches(items, FIELDS, len(items)) == {}
    assert not store.puts
    assert len(sb._run_mapping_batches(items, FIELDS, len(items))) == 2


def test_library_batches_are_left_to_generic_bind(store, monkeypatch):
    items = _items(clause_ids=(-11, -12))
    model = _Model(_rows(items, "Policy Limit"))
    monkeypatch.setattr(sb, "call_gemini", model)
    sb._run_mapping_batches(items, FIELDS, len(items))
    sb._run_mapping_batches(items, FIELDS, len(items))
    assert len(model.calls) == 2
    assert not [g for g in store.gets if g[0] == "rule_bind"]


# ── var_reconcile: the Process Bordereau spelling question ──────────────────

AUTH = ["Acme Specialty Insurance Company", "Acme Insurance Ltd"]
CANDS = ["ACME Specialty Ins Co", "Other Carrier LLC", "Acme Ins Ltd"]


def test_same_question_same_answer_one_call(store):
    ai = _Model({"matches": ["ACME Specialty Ins Co"]},
                {"matches": ["ACME Specialty Ins Co", "Acme Ins Ltd"]})
    a = vr._ai_reconcile(ai, "Carrier", AUTH, CANDS, tenant_id=5)
    b = vr._ai_reconcile(ai, "Carrier", AUTH, CANDS, tenant_id=5)
    assert a == b == ["ACME Specialty Ins Co"]
    assert len(ai.calls) == 1


def test_thinking_is_capped_and_the_output_is_sized(store):
    ai = _Model({"matches": []})
    vr._ai_reconcile(ai, "Carrier", AUTH, CANDS)
    sent = ai.calls[0]
    assert sent["thinking_budget"] == vr._THINKING == 0
    assert sent["max_output_tokens"] == vr._MAX_OUTPUT >= 2048
    from contract_upload_services.gemini_service import EXTRACTION_MODEL
    assert sent["model"] == EXTRACTION_MODEL


def test_value_order_in_the_data_does_not_move_the_key(store):
    ai = _Model({"matches": ["Acme Ins Ltd"]}, {"matches": []})
    vr._ai_reconcile(ai, "Carrier", AUTH, CANDS, tenant_id=5)
    got = vr._ai_reconcile(ai, "Carrier", list(reversed(AUTH)),
                           list(reversed(CANDS)), tenant_id=5)
    assert got == ["Acme Ins Ltd"] and len(ai.calls) == 1


@pytest.mark.parametrize("change", ["tenant", "field", "candidates", "model"])
def test_tenant_field_values_and_model_are_in_the_key(store, monkeypatch, change):
    ai = _Model({"matches": []})
    vr._ai_reconcile(ai, "Carrier", AUTH, CANDS, tenant_id=5)
    args = {"field": "Carrier", "candidates": CANDS, "tenant_id": 5}
    if change == "tenant":
        args["tenant_id"] = 6
    elif change == "field":
        args["field"] = "Paper"
    elif change == "candidates":
        args["candidates"] = CANDS + ["Acme Specialty"]
    elif change == "model":
        monkeypatch.setenv("KAVACHIO_MODEL_VARIATION_RECONCILE", "gemini-2.5-pro")
    vr._ai_reconcile(ai, args["field"], AUTH, args["candidates"],
                     tenant_id=args["tenant_id"])
    assert len(ai.calls) == 2
    if change == "model":
        assert ai.calls[1]["model"] == "gemini-2.5-pro"


def test_an_unparseable_or_failed_answer_is_not_stored(store):
    ai = _Model("sorry, I cannot help", Exception("timeout"), {"matches": []})
    assert vr._ai_reconcile(ai, "Carrier", AUTH, CANDS) == []
    assert vr._ai_reconcile(ai, "Carrier", AUTH, CANDS) == []
    assert not store.puts
    vr._ai_reconcile(ai, "Carrier", AUTH, CANDS)
    assert len(store.puts) == 1                          # "no matches" IS an answer


def test_a_stored_pick_is_still_closed_list_filtered(store):
    ai = _Model({"matches": ["Acme Ins Ltd"]})
    vr._ai_reconcile(ai, "Carrier", AUTH, CANDS)
    (kind, key), = store.puts
    store.rows[(kind, key)] = json.dumps(["Acme Ins Ltd", "Injected Value"])
    assert vr._ai_reconcile(ai, "Carrier", AUTH, CANDS) == ["Acme Ins Ltd"]


# ── model-scoped keys on the kinds that already have stored entries ─────────

def test_formula_infer_key_is_unchanged_on_the_legacy_model_and_moves_off_it(store, monkeypatch):
    fields = [{"name": "Gross Premium", "samples": ["100"]},
              {"name": "Commission", "samples": []}]
    catalog = "- Gross Premium   e.g. [100]\n- Commission"
    monkeypatch.setattr(vrg, "call_gemini", _Model({"formulas": []}))

    monkeypatch.setattr(vrg, "EXTRACTION_MODEL", vrg.LEGACY_CACHE_MODEL)
    vrg.infer_formula_annotations(fields)
    legacy_key = store.gets[-1][1]
    assert legacy_key == ai_cache.make_key("formula_infer_v1", catalog)

    monkeypatch.setattr(vrg, "EXTRACTION_MODEL", "gemini-2.5-pro")
    vrg.infer_formula_annotations(fields)
    assert store.gets[-1][1] != legacy_key


def test_generic_bind_key_moves_only_when_the_model_does(monkeypatch):
    lib = [{"id": 1, "rule_name": "Policy Limit not null", "class_name": "NotNull",
            "validation_logic": "x", "severity": "warning", "tenant_id": None}]
    monkeypatch.setattr(vrg, "EXTRACTION_MODEL", vrg.LEGACY_CACHE_MODEL)
    k1 = vrg._generic_bind_key(lib, FIELDS)
    assert k1 == vrg._generic_bind_key(lib, FIELDS)
    monkeypatch.setattr(vrg, "EXTRACTION_MODEL", "gemini-2.5-pro")
    assert vrg._generic_bind_key(lib, FIELDS) != k1


# ── the whole Call-3 pass: one big call, fallback batches for what it dropped ─

def test_a_recovered_pass_is_stored_under_the_big_call_and_replays(store, monkeypatch):
    items = _items((1, 2, 3))
    # Big call drops clause 3; the fallback batch answers it; the next run's big
    # call would drop a DIFFERENT intent — it must never be asked.
    model = _Model(_rows(items[:2], "Policy Limit"), _rows(items[2:], "Policy Limit"),
                   _rows(items[1:], "Commission %"))
    monkeypatch.setattr(sb, "call_gemini", model)
    got = sb._run_mapping_batches(items, FIELDS, len(items))
    got.update(sb._run_mapping_batches(items[2:], FIELDS, 1))
    assert sb._store_bind_pass(items, got, FIELDS)
    replay = sb._run_mapping_batches(items, FIELDS, len(items))
    assert replay == got and len(model.calls) == 2


def test_an_unrecovered_or_library_pass_is_not_stored(store, monkeypatch):
    items = _items((1, 2))
    one = {(1, 0): {"template": "max_limit", "params": {"field": "Policy Limit"}}}
    assert not sb._store_bind_pass(items, one, FIELDS)
    lib = _items((-1, -2))
    both = {(it["clause_id"], 0): {"template": None} for it in lib}
    assert not sb._store_bind_pass(lib, both, FIELDS)
    assert not store.puts


def test_map_intents_to_ir_stores_the_recovered_pass(store, monkeypatch):
    items = _items((1, 2, 3))
    clauses = [{"clause_id": it["clause_id"], "text": it["clause_text"]} for it in items]
    clfs = [{"is_rule_bearing": True,
             "intents": [{k: it[k] for k in ("subject", "operator", "value", "scope",
                                              "severity", "rule_name",
                                              "rule_description", "error_message")}]}
            for it in items]
    flat = [dict(it, is_referral=False, clause_text=f"clause {it['clause_id']} text")
            for it in items]
    model = _Model(_rows(flat[:2], "Policy Limit"), _rows(flat[2:], "Policy Limit"))
    monkeypatch.setattr(sb, "call_gemini", model)
    first = sb.map_intents_to_ir(clauses, clfs, template_fields=FIELDS)
    assert len(model.calls) == 2            # big call (partial) + one fallback batch
    assert [k for k, _ in store.puts] == ["rule_bind", "rule_bind"]
    second = sb.map_intents_to_ir(clauses, clfs, template_fields=FIELDS)
    assert len(model.calls) == 2            # the replay asked nothing
    assert first == second


# ── var_reconcile: the value set sent is the same on every run ──────────────

def test_distinct_values_are_ordered_so_the_cap_and_spelling_are_stable():
    import duckdb
    con = duckdb.connect()
    con.execute('CREATE TABLE "S1" ("Carrier" VARCHAR)')
    con.executemany('INSERT INTO "S1" VALUES (?)',
                    [("Zeta Re",), ("ACME Ltd",), ("",), (None,), ("Acme Ltd",),
                     ("Beta Ins",)])
    assert vr._distinct_values(con, "S1", "Carrier", 3) == ["ACME Ltd", "Acme Ltd",
                                                            "Beta Ins"]
