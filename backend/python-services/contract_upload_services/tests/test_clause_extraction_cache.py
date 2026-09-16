"""
test_clause_extraction_cache.py
───────────────────────────────
Call 1 (clause extraction) is not repeatable on its own: the same request, at
temperature 0 with a fixed seed, was measured returning two different clause sets
for one PDF. These tests pin the guarantee that replaces the false premise — the
first COMPLETE answer for an exact request is stored in ai_cache and served back:

  * two reads of the same document give identical clauses and ONE model call,
    even when the model would answer differently the second time;
  * anything the model is sent — document text, reference documents, the model,
    the tenant, the thinking budget — changes the key;
  * a failed, partial, thin or empty read is never stored;
  * `refresh` re-asks; the resume path re-parses the re-sent file; and the
    provenance lands in the contract's `extracted` JSON.

Pure: no DB (see _offline.py — in-memory SQLite engine, rule catalog from the
seed files, ai_cache replaced by a dict) and no network (call_gemini stubbed).

Run:  python -m pytest contract_upload_services/tests/test_clause_extraction_cache.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _offline                                                  # noqa: E402,F401

import pytest                                                    # noqa: E402

from contract_upload_services import validation_rule_generator as vrg  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def store(monkeypatch):
    monkeypatch.delenv("KAVACHIO_MODEL_CLAUSE_EXTRACTION", raising=False)
    return _offline.MemoryCache().install(monkeypatch)


def _answer(*titles):
    return json.dumps({
        "program_metadata": {},
        "commercial_terms": [],
        "clauses": [{"clause_type": "limit", "title": t, "text": f"{t} text body."}
                    for t in titles],
        "external_references": [],
    })


class _Model:
    """call_gemini stand-in: returns `answers` in turn (the last one repeats) and
    records every request it was sent."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        a = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(a, Exception):
            raise a
        return a


def _doc(text="Clause one text body. Clause two text body.", pages=1):
    return {"pages": [{"page": i + 1, "text": f"{text} page {i + 1}"}
                      for i in range(pages)]}


def _gen():
    return vrg.ValidationRuleGenerator()


def _titles(section_extractions):
    return [c["title"] for s in section_extractions for c in (s.get("clauses") or [])]


# ── same request → same answer, one model call ──────────────────────────────

def test_second_read_is_served_from_cache_even_if_the_model_would_differ(store, monkeypatch):
    model = _Model(_answer("A1", "A2"), _answer("B1", "B2", "B3"))
    monkeypatch.setattr(vrg, "call_gemini", model)
    g = _gen()
    s1, r1, i1 = g._extract_clauses(_doc(), tenant_id=7)
    s2, r2, i2 = g._extract_clauses(_doc(), tenant_id=7)
    assert _titles(s1) == _titles(s2) == ["A1", "A2"]
    assert len(model.calls) == 1
    assert (i1["cache"], i1["path"], i1["stored"]) == ("miss", "whole_document", True)
    assert (i2["cache"], i2["path"]) == ("hit", "whole_document")
    assert i1["prompt_sha256"] == i2["prompt_sha256"]
    assert i1["model"] == vrg.EXTRACTION_MODEL


def test_the_request_splats_the_same_config_the_key_covers(store, monkeypatch):
    model = _Model(_answer("A1"))
    monkeypatch.setattr(vrg, "call_gemini", model)
    _gen()._extract_clauses(_doc(), tenant_id=1)
    sent = model.calls[0]
    for k, v in vrg._CALL1_CONFIG.items():
        assert sent[k] == v, k
    assert sent["model"] == vrg.EXTRACTION_MODEL


@pytest.mark.parametrize("change", ["text", "reference", "tenant", "model", "thinking"])
def test_every_input_the_model_sees_changes_the_key(store, monkeypatch, change):
    model = _Model(_answer("A1"), _answer("B1"))
    monkeypatch.setattr(vrg, "call_gemini", model)
    g = _gen()
    g._extract_clauses(_doc(), tenant_id=1)
    doc, kw = _doc(), {"tenant_id": 1}
    if change == "text":
        doc = _doc("Clause one text body, amended.")
    elif change == "reference":
        kw["reference_documents"] = [{"name": "Guidelines", "text": "Class list."}]
    elif change == "tenant":
        kw["tenant_id"] = 2
    elif change == "model":
        monkeypatch.setenv("KAVACHIO_MODEL_CLAUSE_EXTRACTION", "gemini-2.5-pro")
    elif change == "thinking":
        monkeypatch.setitem(vrg._CALL1_CONFIG, "thinking_budget", 24576)
    s, _r, info = g._extract_clauses(doc, **kw)
    assert info["cache"] == "miss"
    assert len(model.calls) == 2
    assert _titles(s) == ["B1"]
    if change == "model":
        assert model.calls[1]["model"] == "gemini-2.5-pro" == info["model"]


def test_refresh_asks_again_and_replaces_the_stored_answer(store, monkeypatch):
    model = _Model(_answer("A1"), _answer("B1"))
    monkeypatch.setattr(vrg, "call_gemini", model)
    g = _gen()
    g._extract_clauses(_doc(), tenant_id=1)
    s, _r, info = g._extract_clauses(_doc(), tenant_id=1, refresh=True)
    assert _titles(s) == ["B1"] and info.get("refresh") is True
    s3, _r, info3 = g._extract_clauses(_doc(), tenant_id=1)
    assert _titles(s3) == ["B1"] and info3["cache"] == "hit"
    assert len(model.calls) == 2


# ── only complete answers are stored ────────────────────────────────────────

def test_failed_whole_document_with_one_failed_chunk_is_not_stored(store, monkeypatch):
    monkeypatch.setenv("KAVACHIO_SECTION_PAGES", "1")
    # whole-document call fails → 2 page chunks: the first answers, the second fails
    model = _Model(Exception("MAX_TOKENS"), _answer("P1"), Exception("boom"))
    monkeypatch.setattr(vrg, "call_gemini", model)
    s, _r, info = _gen()._extract_clauses(_doc(pages=2), tenant_id=1)
    assert _titles(s) == ["P1"]
    assert info["path"] == "sections" and info["stored"] is False
    assert not store.puts


def test_fallback_where_every_chunk_answers_is_stored(store, monkeypatch):
    monkeypatch.setenv("KAVACHIO_SECTION_PAGES", "1")
    model = _Model(Exception("invalid JSON"), _answer("P1"), _answer("P2"),
                   _answer("NEVER"))
    monkeypatch.setattr(vrg, "call_gemini", model)
    g = _gen()
    s, _r, info = g._extract_clauses(_doc(pages=2), tenant_id=1)
    assert info["path"] == "sections" and info["stored"] is True
    s2, _r2, info2 = g._extract_clauses(_doc(pages=2), tenant_id=1)
    assert _titles(s2) == ["P1", "P2"] and info2["cache"] == "hit"
    assert len(model.calls) == 3


def test_thin_and_empty_reads_are_never_stored(store, monkeypatch):
    # 4 pages, 1 clause → thin → chunks that return no clauses at all.
    monkeypatch.setenv("KAVACHIO_SECTION_PAGES", "4")
    model = _Model(_answer("ONLY"), _answer())
    monkeypatch.setattr(vrg, "call_gemini", model)
    s, _r, info = _gen()._extract_clauses(_doc(pages=4), tenant_id=1)
    assert not _titles(s)
    assert info["stored"] is False and not store.puts


def test_every_call_failing_stores_nothing_and_returns_the_skeleton(store, monkeypatch):
    model = _Model(Exception("down"))
    monkeypatch.setattr(vrg, "call_gemini", model)
    s, _r, info = _gen()._extract_clauses(_doc(), tenant_id=1)
    assert s == [{"program_metadata": {}, "commercial_terms": [], "clauses": []}]
    assert info["stored"] is False and not store.puts


def test_a_malformed_stored_entry_is_treated_as_a_miss(store, monkeypatch):
    model = _Model(_answer("A1"))
    monkeypatch.setattr(vrg, "call_gemini", model)
    g = _gen()
    g._extract_clauses(_doc(), tenant_id=1)
    (kind, key), = store.puts
    store.rows[(kind, key)] = json.dumps({"section_extractions": [{"clauses": []}]})
    s, _r, info = g._extract_clauses(_doc(), tenant_id=1)
    assert info["cache"] == "miss" and _titles(s) == ["A1"]


# ── the full pipeline: provenance and the resume path ───────────────────────

def _no_rules_pipeline(monkeypatch):
    monkeypatch.setattr(vrg, "extract_rule_intents",
                        lambda clauses: [{"is_rule_bearing": False} for _ in clauses])


def test_generate_carries_provenance_and_identical_clauses(store, monkeypatch):
    _no_rules_pipeline(monkeypatch)
    model = _Model(_answer("A1", "A2"), _answer("B1"))
    monkeypatch.setattr(vrg, "call_gemini", model)
    g = _gen()
    out1 = g.generate_validation_rules_json(_doc(), "c.pdf", tenant_id=3)
    out2 = g.generate_validation_rules_json(_doc(), "c.pdf", tenant_id=3)
    strip = lambda o: [(c["title"], c["text"]) for c in o["clauses_extracted"]]  # noqa: E731
    assert strip(out1) == strip(out2) == [("A1", "A1 text body."), ("A2", "A2 text body.")]
    assert out1["metadata"]["extraction"]["cache"] == "miss"
    assert out2["metadata"]["extraction"]["cache"] == "hit"
    assert len(model.calls) == 1


def test_resume_with_a_lost_token_rebuilds_the_halted_request(store, monkeypatch):
    """A stale token (restart / other worker) now reads the re-parsed document,
    so the halted run's stored answer is served instead of an empty-text read."""
    _no_rules_pipeline(monkeypatch)
    model = _Model(_answer("A1"), _answer("SHOULD NOT BE ASKED"))
    monkeypatch.setattr(vrg, "call_gemini", model)
    g = _gen()
    g.generate_validation_rules_json(_doc(), "c.pdf", tenant_id=3)
    out = g.generate_validation_rules_json(_doc(), "c.pdf", tenant_id=3,
                                           resume_token="lost-token")
    assert [c["title"] for c in out["clauses_extracted"]] == ["A1"]
    assert out["metadata"]["extraction"]["cache"] == "hit"
    assert len(model.calls) == 1


def test_resume_token_fast_path_is_kept_and_marked(store, monkeypatch):
    _no_rules_pipeline(monkeypatch)
    monkeypatch.setattr(vrg, "call_gemini", _Model(Exception("must not be called")))
    vrg._EXTRACTION_RESUME_CACHE["tok"] = {
        "section_extractions": [json.loads(_answer("R1"))],
        "external_references": [],
        "extraction": {"model": "m", "prompt_sha256": "abc", "cache": "miss",
                       "path": "whole_document", "stored": True},
    }
    out = _gen().generate_validation_rules_json(_doc(), "c.pdf", resume_token="tok")
    assert [c["title"] for c in out["clauses_extracted"]] == ["R1"]
    assert out["metadata"]["extraction"]["path"] == "resume"
    assert out["metadata"]["extraction"]["prompt_sha256"] == "abc"


def test_process_contract_parses_the_resent_file_on_resume(monkeypatch, tmp_path):
    from contract_upload_services import contract_extraction_service as ces
    f = tmp_path / "c.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    parsed = {"pages": [{"page": 1, "text": "real text"}]}
    seen = {}
    monkeypatch.setattr(ces, "extract_document_data", lambda p: parsed)

    class _Gen:
        def generate_validation_rules_json(self, pdf_data, *a, **kw):
            seen["pdf_data"], seen["kw"] = pdf_data, kw
            return {"halted_for_references": True}

    svc = ces.ContractExtractionService.__new__(ces.ContractExtractionService)
    svc.validation_generator = _Gen()
    svc.process_contract(str(f), resume_token="tok")
    assert seen["pdf_data"] is parsed
    assert seen["kw"]["resume_token"] == "tok"
    assert seen["kw"]["refresh_extraction"] is False


def test_process_contract_resume_without_a_file_keeps_the_old_behaviour(monkeypatch):
    from contract_upload_services import contract_extraction_service as ces
    monkeypatch.setattr(ces, "extract_document_data",
                        lambda p: (_ for _ in ()).throw(AssertionError("parsed")))
    seen = {}

    class _Gen:
        def generate_validation_rules_json(self, pdf_data, *a, **kw):
            seen["pdf_data"] = pdf_data
            return {"halted_for_references": True}

    svc = ces.ContractExtractionService.__new__(ces.ContractExtractionService)
    svc.validation_generator = _Gen()
    svc.process_contract("/nonexistent/c.pdf", resume_token="tok")
    assert seen["pdf_data"] == {"pages": []}


def test_extraction_provenance_is_nested_in_the_contract_payload():
    from contract_upload_services.db_persister import build_extracted_payload
    info = {"model": "m", "prompt_sha256": "abc", "cache": "hit",
            "path": "whole_document", "stored": True}
    got = build_extracted_payload("t", "p", {}, extraction=info)
    assert got["extraction"] == info
    # nested → never one of the contract's top-level scalar constants
    assert "extraction" not in {k for k, v in got.items()
                                if isinstance(v, (str, int, float))}
    assert "extraction" not in build_extracted_payload("t", "p", {})
