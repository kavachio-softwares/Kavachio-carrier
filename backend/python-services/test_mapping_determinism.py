"""The same inputs give the same column matches — setup mapping and the
"Create BDX template" tick list alike.

Offline: every model call is replaced (``direct_mapper._ask_model``,
``contract_output_fields._ask_model``, the SDK clients) and ai_cache runs on an
in-memory dict, so nothing here reaches the network or the database.

Run:  pytest test_mapping_determinism.py
"""
import json
import os
import random
import sys
import threading
import time
from types import SimpleNamespace

# No read-only PGOPTIONS here: it is process-wide, and a whole-suite pytest run
# would turn every later database test read-only. Nothing below reaches the
# database — ai_cache is replaced by the `cache` fixture.
# gemini_service builds its client at import and refuses to without a key.
os.environ.setdefault("GEMINI_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import ai_cache
import contract_output_fields as cof
import direct_mapper as dm
import output_source_analysis as osa
import semantic_mapping as sm
from contract_upload_services import gemini_service as gs


# ---- fixtures ---------------------------------------------------------------

def _names(prefix, n):
    return [f"{prefix} {i:02d}" for i in range(n)]


# Output and input names share no words, so no deterministic rung places any
# of them and every output column goes to the model.
OUT = _names("Output field", 8)
IN = _names("src", 8)
SAMPLES = {c: [f"v{i}a", f"v{i}b"] for i, c in enumerate(IN)}


def _structure(template_id, sheet):
    return {"template_id": template_id, "sheets": [{
        "sheet_name": sheet,
        "columns": [{"column_name": c, "source_type": "BDX_DATA"} for c in OUT]}]}


def _routing(sheet):
    return {"routes": [{"output_sheet": sheet, "sources": [{"input_sheet": "In"}]}]}


def asked_in(prompt):
    """The output columns one prompt asks about."""
    return json.loads(prompt.split("OUTPUT columns:\n", 1)[1].split("\n\n", 1)[0])


def answer_for(cols):
    """A deterministic model: output column i -> input column i at 95%."""
    return {c: {"in": IN[OUT.index(c)], "s": 0.95} for c in cols}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KAVACHIO_DECISION_LOG", str(tmp_path / "decisions.log"))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    for k in ("KAVACHIO_AI_CACHE", "KAVACHIO_AI_CACHE_COLUMN_CANDIDATES",
              "KAVACHIO_AI_CACHE_CONTRACT_FIELDS", "KAVACHIO_MODEL_COLUMN_MAPPING",
              "KAVACHIO_MODEL_CONTRACT_FIELDS", "KAVACHIO_CONTRACT_FIELDS_MODEL",
              "KAVACHIO_COLUMN_MAPPING_BATCH", "KAVACHIO_COLUMN_MAPPING_THINKING",
              "KAVACHIO_COLUMN_MAPPING_PARALLEL", "KAVACHIO_MODEL_TEMPLATE_MAPPING",
              "KAVACHIO_MODEL_SHEET_ROLES", "KAVACHIO_MODEL_DATAMODEL_MAPPING",
              "KAVACHIO_MAPPING_AUTO_CONFIDENCE", "KAVACHIO_MAPPING_REVIEW_CONFIDENCE",
              "KAVACHIO_MAPPING_CANDIDATE_SIMILARITY"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def cache(monkeypatch):
    """ai_cache on a dict — same contract: refresh is a miss, None is not stored."""
    store = {}
    puts = []

    def get(kind, key, refresh=False):
        return None if refresh else json.loads(json.dumps(store.get((kind, key))))

    def put(kind, key, payload, tenant_id=None):
        if payload is not None:
            store[(kind, key)] = payload
            puts.append((kind, key, tenant_id))

    monkeypatch.setattr(ai_cache, "get", get)
    monkeypatch.setattr(ai_cache, "put", put)
    return SimpleNamespace(store=store, puts=puts)


def stub_model(monkeypatch, reply=None):
    """Replace the network call. `reply(cols, n)` -> (text, finish); default is
    a complete, correct answer. Returns the list of column lists asked."""
    calls = []
    lock = threading.Lock()

    def fake(prompt):
        cols = asked_in(prompt)
        with lock:
            calls.append(cols)
            n = len(calls)
        if reply:
            return reply(cols, n)
        return json.dumps(answer_for(cols)), "STOP"

    monkeypatch.setattr(dm, "_ask_model", fake)
    return calls


def _setup(template_id, sheet, tenant_id=7):
    _, _, decisions = dm.propose_column_mapping(
        {"In": IN}, _structure(template_id, sheet), _routing(sheet),
        {"In": SAMPLES}, None, True, tenant_id=tenant_id)
    return decisions[sheet]


# ---- Bordereau Setup --------------------------------------------------------

def test_same_columns_on_a_new_template_reuse_the_answers(monkeypatch, cache):
    calls = stub_model(monkeypatch)
    first = _setup(101, "Template A sheet")
    assert calls, "the first setup must ask the model"
    assert all(d["status"] == sm.AUTO_MAPPED for d in first)

    # A different template id and sheet title, same columns and same file.
    calls.clear()
    monkeypatch.setattr(dm, "_ask_model", lambda p: pytest.fail("model called"))
    second = _setup(202, "Template B sheet")
    assert second == first


def test_a_changed_input_or_tenant_is_a_miss(monkeypatch, cache):
    calls = stub_model(monkeypatch)
    _setup(1, "S")
    n = len(calls)
    _setup(1, "S", tenant_id=8)                       # another tenant's samples
    assert len(calls) > n
    n = len(calls)
    SAMPLES[IN[0]].append("changed")                  # one sample value moves
    try:
        _setup(1, "S")
    finally:
        SAMPLES[IN[0]].pop()
    assert len(calls) > n


def test_the_model_is_part_of_the_key(monkeypatch, cache):
    calls = stub_model(monkeypatch)
    _setup(1, "S")
    n = len(calls)
    monkeypatch.setenv("KAVACHIO_MODEL_COLUMN_MAPPING", "gemini-2.5-pro")
    _setup(1, "S")
    assert len(calls) > n, "an answer from another model must not be served"


def test_refresh_bypasses_the_cache(monkeypatch, cache):
    calls = stub_model(monkeypatch)
    dm.model_column_candidates(OUT, IN, SAMPLES)
    n = len(calls)
    report = {}
    dm.model_column_candidates(OUT, IN, SAMPLES, report=report)
    assert len(calls) == n and report["cached"] == len(OUT) and report["calls"] == 0
    dm.model_column_candidates(OUT, IN, SAMPLES, refresh=True)
    assert len(calls) > n


def _truncated(cols, keep):
    """A reply cut off inside the entry after the first `keep` columns."""
    body = json.dumps(answer_for(cols[:keep]))[:-1]
    return body + f', "{cols[keep]}": {{"in": "sr', "MAX_TOKENS"


def test_a_cut_off_reply_is_partial_and_only_the_gap_is_asked_again(monkeypatch, cache):
    monkeypatch.setenv("KAVACHIO_COLUMN_MAPPING_BATCH", "4")
    # Both the reply and the re-ask are cut off.
    calls = stub_model(monkeypatch, reply=lambda cols, n: _truncated(cols, 1)
                       if n == 1 else ("{", "MAX_TOKENS"))
    report = {}
    out = dm.model_column_candidates(OUT[:4], IN, SAMPLES, report=report)

    assert calls[0] == OUT[:4]
    # the cut entry is not trusted, and only what is missing is asked again
    assert sorted(sum(calls[1:], [])) == OUT[1:4]
    assert all(len(c) <= 2 for c in calls[1:])       # in smaller batches
    assert report["status"] == "partial"
    assert report["finish"] == "MAX_TOKENS"
    assert report["answered"] == OUT[:1] and set(out) == {OUT[0]}
    assert report["unanswered"] == OUT[1:4]
    assert "unanswered" in report["error"]
    assert cache.puts == [], "a partial answer must never be cached"

    # and the decision log does not call it "AI ok"
    stub_model(monkeypatch, reply=lambda cols, n: _truncated(cols, 1)
               if n == 1 else ("{", "MAX_TOKENS"))
    decisions = _setup(1, "S")
    text = open(os.environ["KAVACHIO_DECISION_LOG"]).read()
    assert "AI-PARTIAL" in text and "AI ok" not in text
    failed = [d for d in decisions if d["status"] == sm.AI_UNAVAILABLE]
    assert failed and all("MAX_TOKENS" in d["ai_error"] for d in failed)


def test_a_complete_re_ask_is_cached_but_the_cut_reply_is_not(monkeypatch, cache):
    calls = stub_model(monkeypatch, reply=lambda cols, n: _truncated(cols, 2)
                       if n == 1 else (json.dumps(answer_for(cols)), "STOP"))
    report = {}
    out = dm.model_column_candidates(OUT[:4], IN, SAMPLES, report=report)
    assert calls == [OUT[:4], OUT[2:4]]
    assert report["status"] == "ok" and set(out) == set(OUT[:4])
    assert len(cache.puts) == 2                       # the re-asked columns only

    # next time only the columns that came from the cut reply are asked
    calls.clear()
    dm.model_column_candidates(OUT[:4], IN, SAMPLES)
    assert calls == [OUT[:2]]


def test_a_finished_reply_that_leaves_a_column_out_caches_what_it_holds(
        monkeypatch, cache):
    # A model that keeps skipping one column: its batch-mates are still whole
    # answers, so they become repeatable; only the skipped one is asked again.
    calls = stub_model(monkeypatch, reply=lambda cols, n: (
        json.dumps(answer_for([c for c in cols if c != OUT[1]])), "STOP"))
    report = {}
    dm.model_column_candidates(OUT[:3], IN, SAMPLES, report=report)
    assert calls == [OUT[:3], [OUT[1]]]
    assert report["status"] == "partial" and report["unanswered"] == [OUT[1]]
    assert "(finish: STOP)" in report["reasons"][OUT[1]]
    assert len(cache.puts) == 2                       # OUT[0] and OUT[2]

    calls.clear()
    report = {}
    dm.model_column_candidates(OUT[:3], IN, SAMPLES, report=report)
    assert report["cached"] == 2 and calls == [[OUT[1]], [OUT[1]]]


def test_batches_follow_field_order_however_calls_finish(monkeypatch, cache):
    monkeypatch.setenv("KAVACHIO_COLUMN_MAPPING_BATCH", "3")
    monkeypatch.setenv("KAVACHIO_COLUMN_MAPPING_PARALLEL", "4")
    rng = random.Random(1)

    def slow(cols, n):
        time.sleep(rng.random() / 50)                 # finish in any order
        return json.dumps(answer_for(cols)), "STOP"

    calls = stub_model(monkeypatch, reply=slow)
    report = {}
    out = dm.model_column_candidates(OUT, IN, SAMPLES, report=report)
    assert sorted(calls) == [OUT[0:3], OUT[3:6], OUT[6:8]]
    assert list(out) == OUT and report["answered"] == OUT
    assert report["calls"] == 3


def test_ask_model_config_and_model_override(monkeypatch):
    seen = []

    def invoke(kwargs, label=None, gen_client=None, **_):
        seen.append(kwargs)
        return SimpleNamespace(text="{}", candidates=[
            SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))])

    monkeypatch.setattr(gs, "invoke_with_retry", invoke)
    monkeypatch.setenv("KAVACHIO_COLUMN_MAPPING_THINKING", "2048")
    monkeypatch.setenv("KAVACHIO_COLUMN_MAPPING_BATCH", "10")
    assert dm._ask_model("p") == ("{}", "STOP")
    cfg = seen[0]["config"]
    assert seen[0]["model"] == dm.DEFAULT_MAPPING_MODEL
    assert cfg["temperature"] == 0 and cfg["seed"] == gs.DETERMINISTIC_SEED
    assert cfg["thinking_config"] == {"thinking_budget": 2048}
    assert cfg["max_output_tokens"] > 2048 + 10 * 30   # thinking AND the answer

    monkeypatch.setenv("KAVACHIO_MODEL_COLUMN_MAPPING", "gemini-2.5-pro")
    dm._ask_model("p")
    assert seen[1]["model"] == "gemini-2.5-pro"


def test_other_mapping_calls_take_their_model_from_config(monkeypatch):
    import exporter
    import mapper

    captured = []

    def generate_content(**kw):
        captured.append(kw)
        return SimpleNamespace(text='{"sheets": []}', candidates=[], usage_metadata=None)

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    monkeypatch.setenv("KAVACHIO_MODEL_SHEET_ROLES", "roles-model")
    exporter.classify_sheet_roles(client, {"sheets": [
        {"sheet_name": "A", "columns": [{"column_name": "Policy No"}]}]})
    assert captured[-1]["model"] == "roles-model"
    assert captured[-1]["config"]["temperature"] == 0
    assert captured[-1]["config"]["seed"] == gs.DETERMINISTIC_SEED

    monkeypatch.setattr(exporter, "_gateway_invoke",
                        lambda kwargs, **_: captured.append(kwargs)
                        or SimpleNamespace(text="{}", candidates=[], usage_metadata=None))
    monkeypatch.setenv("KAVACHIO_MODEL_TEMPLATE_MAPPING", "tpl-model")
    exporter._call_gemini_for_chunk(
        client, {"sheet_name": "A", "columns": []},
        [{"column_index": 0, "column_name": "Policy No", "samples": ["P1"]}], 0, {})
    assert captured[-1]["model"] == "tpl-model"
    assert captured[-1]["config"]["temperature"] == 0
    assert captured[-1]["config"]["thinking_config"] == {"thinking_budget": 0}

    monkeypatch.setenv("KAVACHIO_MODEL_DATAMODEL_MAPPING", "dm-model")
    mapper._gemini_candidates_call(client, ["Policy No"], {"Policy No": ["P1"]}, {}, {})
    assert captured[-1]["model"] == "dm-model"
    assert captured[-1]["config"]["seed"] == gs.DETERMINISTIC_SEED


# ---- Create BDX template ----------------------------------------------------

def _fields(names):
    return [{"field": n, "required": False, "origin": "standard"} for n in names]


def test_a_failed_model_call_keeps_the_open_fields_ticked(monkeypatch, cache):
    def boom(prompt):
        raise RuntimeError("503 UNAVAILABLE")
    monkeypatch.setattr(dm, "_ask_model", boom)
    rows = osa.recommend(osa.cross_reference(_fields(OUT[:3]), IN, SAMPLES),
                         checked_input=True)
    assert all(r["recommended"] and r["ai_unanswered"] for r in rows)
    assert all("the AI did not answer for this column" in r["recommend_reason"]
               for r in rows)
    assert osa.summarise(rows, checked_input=True)["ai_unanswered"] == 3


def test_a_partial_reply_keeps_only_the_unanswered_ticked(monkeypatch, cache):
    # OUT[0] matched, OUT[1] answered "nothing fits", OUT[2] left out.
    stub_model(monkeypatch, reply=lambda cols, n: (json.dumps(
        {k: v for k, v in {OUT[0]: {"in": IN[0], "s": 0.95},
                           OUT[1]: {"in": None, "s": 0.0}}.items() if k in cols}),
        "STOP"))
    rows = {r["field"]: r for r in osa.recommend(
        osa.cross_reference(_fields(OUT[:3]), IN, SAMPLES), checked_input=True)}
    assert rows[OUT[0]]["recommended"] and not rows[OUT[0]].get("ai_unanswered")
    assert not rows[OUT[1]]["recommended"]            # a real "no match"
    assert rows[OUT[2]]["recommended"] and rows[OUT[2]]["ai_unanswered"]


def test_an_unanswered_contract_field_is_kept_as_its_own_column(monkeypatch, cache):
    monkeypatch.setattr(dm, "_ask_model", lambda p: ("not json", "MAX_TOKENS"))
    library = _fields(OUT[:3])
    contract = [{"field": "Scheme Fee Split", "required": True, "origin": "contract"}]
    extras, folded = osa.fold_contract_fields(library, contract)
    assert folded == {} and [e["field"] for e in extras] == ["Scheme Fee Split"]
    assert extras[0]["merge_unchecked"]
    # the route's own path: fold -> merge -> cross-reference -> recommend
    merged = osa.merge_fields(library, extras, folded)
    rows = {r["field"]: r for r in osa.recommend(
        osa.cross_reference(merged, [], {}), checked_input=False)}
    assert "check for a duplicate" in rows["Scheme Fee Split"]["recommend_reason"]
    assert not any(rows[n].get("merge_unchecked") for n in OUT[:3])


def test_template_builder_refresh_reaches_the_model(monkeypatch, cache):
    calls = stub_model(monkeypatch)
    osa.cross_reference(_fields(OUT[:3]), IN, SAMPLES, tenant_id=3)
    n = len(calls)
    osa.cross_reference(_fields(OUT[:3]), IN, SAMPLES, tenant_id=3)
    assert len(calls) == n
    osa.cross_reference(_fields(OUT[:3]), IN, SAMPLES, tenant_id=3, refresh=True)
    assert len(calls) > n

    osa.fold_contract_fields(_fields(IN), _fields(OUT[:3]), tenant_id=3)
    n = len(calls)
    osa.fold_contract_fields(_fields(IN), _fields(OUT[:3]), tenant_id=3)
    assert len(calls) == n
    osa.fold_contract_fields(_fields(IN), _fields(OUT[:3]), tenant_id=3, refresh=True)
    assert len(calls) > n

    _setup(1, "S", tenant_id=3)
    n = len(calls)
    dm.propose_column_mapping({"In": IN}, _structure(1, "S"), _routing("S"),
                              {"In": SAMPLES}, None, True, tenant_id=3, refresh=True)
    assert len(calls) > n


def test_template_builder_answers_are_repeatable_from_the_cache(monkeypatch, cache):
    stub_model(monkeypatch, reply=lambda cols, n: (json.dumps(
        {c: {"in": IN[OUT.index(c)], "s": round(0.5 + 0.1 * (n % 5), 2)}
         for c in cols}), "STOP"))                    # a model that drifts
    runs = [json.dumps(osa.recommend(osa.cross_reference(
        _fields(OUT), IN, SAMPLES, tenant_id=3), checked_input=True), sort_keys=True)
        for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


# ---- contract fields --------------------------------------------------------

CLAUSES = [{"title": "Reporting", "page": 1,
            "text": "The coverholder shall report the scheme fee and the broker."}]
ITEMS = [{"field": "Zeta Fee", "required": False},
         {"field": "alpha ref", "required": False},
         {"field": "Beta Broker", "required": True},
         {"field": "Gamma Limit", "required": True}]


def test_contract_fields_are_cut_in_a_fixed_order_and_cached(monkeypatch, cache):
    monkeypatch.setattr(cof, "_evidence", lambda s, ids: (list(CLAUSES), []))
    monkeypatch.setattr(cof, "_MAX_FIELDS", 3)
    monkeypatch.setattr(cof, "_model", lambda: "m1")
    replies = [list(ITEMS), list(reversed(ITEMS))]
    asked = []
    monkeypatch.setattr(cof, "_ask_model", lambda p: asked.append(p) or replies[len(asked) - 1])

    a = cof.analyze(None, [1], ["Policy No"], tenant_id=5)
    assert [f["field"] for f in a["fields"]] == ["Beta Broker", "Gamma Limit", "alpha ref"]
    b = cof.analyze(None, [1], ["Policy No"], tenant_id=5)
    assert len(asked) == 1 and b == a                 # served from the cache

    monkeypatch.setattr(cof, "_model", lambda: "m2")  # another model: a miss
    c = cof.analyze(None, [1], ["Policy No"], tenant_id=5)
    assert len(asked) == 2 and c["fields"] == a["fields"]   # same order either way


def test_contract_fields_refresh_asks_again(monkeypatch, cache):
    monkeypatch.setattr(cof, "_evidence", lambda s, ids: (list(CLAUSES), []))
    monkeypatch.setattr(cof, "_model", lambda: "m1")
    asked = []
    monkeypatch.setattr(cof, "_ask_model", lambda p: asked.append(p) or list(ITEMS))
    cof.analyze(None, [1], [])
    cof.analyze(None, [1], [])
    assert len(asked) == 1
    cof.analyze(None, [1], [], refresh=True)
    assert len(asked) == 2


def test_contract_fields_failed_call_is_not_cached(monkeypatch, cache):
    monkeypatch.setattr(cof, "_evidence", lambda s, ids: (list(CLAUSES), []))
    monkeypatch.setattr(cof, "_model", lambda: "m1")
    monkeypatch.setattr(cof, "_ask_model", lambda p: None)
    assert cof.analyze(None, [1], [])["model_used"] is False
    assert cache.puts == []


def test_contract_fields_model_config(monkeypatch):
    assert cof._model() == gs.EXTRACTION_MODEL
    monkeypatch.setenv("KAVACHIO_CONTRACT_FIELDS_MODEL", "older-override")
    assert cof._model() == "older-override"
    monkeypatch.setenv("KAVACHIO_MODEL_CONTRACT_FIELDS", "purpose-override")
    assert cof._model() == "purpose-override"


def test_contract_fields_schema_has_no_size_limits():
    blob = json.dumps(cof._RESPONSE_SCHEMA)
    assert "maxItems" not in blob and "maxLength" not in blob
    assert "enum" in blob and '"required"' in blob


# ---- one input column, two different meanings --------------------------------

def test_one_input_column_is_not_silently_taken_for_two_meanings():
    # Built through the real field builder, so the column's meaning has to
    # survive the trip into resolve_sheet.
    structure = {"sheets": [{"sheet_name": "S", "columns": [
        {"column_name": "Commission Amount", "canonical_field": "commission_amount"},
        {"column_name": "Brokerage Amount", "canonical_field": "brokerage_amount"},
        {"column_name": "Net (Original)", "canonical_field": "net_premium"},
        {"column_name": "Net (Settlement)", "canonical_field": "net_premium"},
    ]}]}
    out_fields = dm._output_fields_for_sheet(structure, "S")
    semantic = {"Brokerage Amount": {"source": "Commission Amount", "confidence": 0.95},
                "Net (Original)": {"source": "Net", "confidence": 0.95},
                "Net (Settlement)": {"source": "Net", "confidence": 0.95}}
    ai = {c: {"asked": True, "ok": True} for c in semantic}
    ds = {d.display_name: d for d in sm.resolve_sheet(
        out_fields, ["Commission Amount", "Net"],
        {"Commission Amount": ["10.5", "20"], "Net": ["90", "80"]},
        semantic=semantic, threshold=0.9, review_floor=0.8, ai=ai)}
    assert ds["Commission Amount"].mapped and ds["Commission Amount"].source == "Commission Amount"
    brokerage = ds["Brokerage Amount"]
    assert not brokerage.mapped and brokerage.status == sm.REVIEW_REQUIRED
    assert brokerage.suggestion == "Commission Amount" and " — the same input column" in brokerage.reason
    # one meaning shown in two columns stays mapped
    assert ds["Net (Original)"].mapped and ds["Net (Settlement)"].mapped
