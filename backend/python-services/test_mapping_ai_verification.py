"""Bordereau Setup: the model verifies output → input column mappings.

Offline — the one model call is replaced at `direct_mapper._ask_model`, so every
confidence band, failure mode and log line is exercised without a network.

Run:  pytest test_mapping_ai_verification.py
"""
import json
import sys

sys.path.insert(0, ".")

import pytest

import direct_mapper as dm
import semantic_mapping as sm

MONEY = ["1500.00", "2300.50", "980"]
INPUT = {"Risk BDX": ["Unique Market Reference (UMR)", "Gross Written Premium",
                      "Sum Insured", "Original Currency", "Commission %", "Endt No."]}
SAMPLES = {"Risk BDX": {"Unique Market Reference (UMR)": ["B123", "B124"],
                        "Gross Written Premium": MONEY, "Sum Insured": MONEY,
                        "Original Currency": ["USD", "USD"],
                        "Commission %": ["11", "11"], "Endt No.": ["E1", "E2"]}}


def col(name, source_type="BDX_DATA"):
    return {"column_name": name, "source_type": source_type, "required": True}


STRUCTURE = {"sheets": [{"sheet_name": "Out", "columns": [
    col("Unique Market Reference (UMR)"),
    col("Total gross written premium"),
    col("Gross premium paid this time"),
    col("Sum Insured Currency (see code list)"),
    col("Settlement Currency"),
    col("Commission Amount"),
    col("Reporting Period (End Date)", source_type="CONTRACT"),
    col("Endt No.", source_type="CONTRACT"),
]}]}
ROUTING = {"routes": [{"output_sheet": "Out", "sources": [{"input_sheet": "Risk BDX"}]}]}

ANSWER = {
    "Total gross written premium": {"in": "Gross Written Premium", "s": 0.97},
    "Gross premium paid this time": {"in": "Gross Written Premium", "s": 0.85},
    "Sum Insured Currency (see code list)": {"in": None, "s": 0.0},
    "Settlement Currency": {"in": "Original Currency", "s": 0.8},
    # "Commission Amount" is missing on purpose — an answer cut short.
}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    log = tmp_path / "decisions.log"
    monkeypatch.setenv("KAVACHIO_DECISION_LOG", str(log))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    # No cached answers here: ai_cache would reach the database.
    monkeypatch.setenv("KAVACHIO_AI_CACHE_COLUMN_CANDIDATES", "0")
    for k in ("KAVACHIO_MAPPING_AUTO_CONFIDENCE", "KAVACHIO_MAPPING_REVIEW_CONFIDENCE",
              "KAVACHIO_MAPPING_CANDIDATE_SIMILARITY"):
        monkeypatch.delenv(k, raising=False)
    return log


def run(monkeypatch, answer=ANSWER, error=None, text=None, finish="STOP",
        existing=None):
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        if error:
            raise error
        return (text if text is not None else json.dumps(answer)), finish

    monkeypatch.setattr(dm, "_ask_model", fake)
    mapping, _, decisions = dm.propose_column_mapping(
        INPUT, STRUCTURE, ROUTING, SAMPLES, existing, True)
    return mapping["Out"], {d["display_name"]: d for d in decisions["Out"]}, prompts


def test_one_call_and_loose_matches_reach_the_model(monkeypatch):
    _, by, prompts = run(monkeypatch)
    # one call, plus one re-ask for the column the answer left out — alone
    assert len(prompts) == 2
    assert json.dumps(["Commission Amount"]) in prompts[1]
    p = prompts[0]
    # the loose match is SENT to be verified, not discarded or accepted
    assert '"Total gross written premium": ["Gross Written Premium"]' in p
    assert '"Sum Insured Currency (see code list)": ["Sum Insured"]' in p
    # a same-name column is not asked about, nor is a non-bordereau field
    assert by["Unique Market Reference (UMR)"]["ai_status"] is None
    assert "Reporting Period (End Date)" not in p


def test_confidence_bands(monkeypatch):
    mapping, by, _ = run(monkeypatch)

    d = by["Total gross written premium"]                  # 97% -> auto
    assert (d["status"], d["method"]) == (sm.AUTO_MAPPED, sm.SEMANTIC)
    assert mapping["Total gross written premium"]["source"] == "Gross Written Premium"
    assert d["ai_confidence"] == 0.97 and d["similarity"] > 0.8

    d = by["Gross premium paid this time"]                 # 85% -> verify
    assert d["status"] == sm.REVIEW_REQUIRED
    assert d["suggestion"] == "Gross Written Premium"
    assert "Gross premium paid this time" not in mapping

    d = by["Settlement Currency"]                          # 80% is not above 80
    assert d["status"] == sm.UNMAPPED and "Settlement Currency" not in mapping
    assert d["reason"] == "no suitable mapping found"


def test_similar_name_is_not_proof(monkeypatch):
    mapping, by, _ = run(monkeypatch)
    d = by["Sum Insured Currency (see code list)"]
    assert d["similarity"] == 0.9                          # looks alike...
    assert d["status"] == sm.UNMAPPED                      # ...AI said no
    assert "Sum Insured Currency (see code list)" not in mapping
    assert [c["method"] for c in d["candidates"]] == [sm.SIMILAR]


def test_missing_answer_is_ai_failure_not_no_match(monkeypatch):
    _, by, _ = run(monkeypatch)
    d = by["Commission Amount"]
    assert d["status"] == sm.AI_UNAVAILABLE
    assert "left this column out" in d["ai_error"] and "STOP" in d["ai_error"]


def test_call_error_is_recorded(monkeypatch):
    mapping, by, _ = run(monkeypatch, error=RuntimeError("503 UNAVAILABLE"))
    asked = ["Total gross written premium", "Gross premium paid this time",
             "Sum Insured Currency (see code list)", "Settlement Currency",
             "Commission Amount"]
    for name in asked:
        assert by[name]["status"] == sm.AI_UNAVAILABLE, name
        assert "503 UNAVAILABLE" in by[name]["ai_error"]
        assert "no column" not in by[name]["reason"]
    # the loose match survives as a hint, never as a mapping
    assert by["Total gross written premium"]["suggestion"] == "Gross Written Premium"
    assert set(mapping) == {"Unique Market Reference (UMR)", "Endt No."}


def test_unusable_answer_and_missing_key(monkeypatch):
    _, by, _ = run(monkeypatch, text="sorry, no JSON", finish="MAX_TOKENS")
    d = by["Settlement Currency"]
    assert d["status"] == sm.AI_UNAVAILABLE and "MAX_TOKENS" in d["ai_error"]

    monkeypatch.delenv("GEMINI_API_KEY")
    _, by, prompts = run(monkeypatch)
    assert prompts == []
    assert by["Settlement Currency"]["ai_error"] == "GEMINI_API_KEY is not set"


def test_existing_mappings_preserved(monkeypatch):
    confirmed = {"Out": {"Gross premium paid this time":
                         {"kind": "copy", "source": "Gross Written Premium"}}}
    mapping, by, prompts = run(monkeypatch, existing=confirmed)
    assert by["Gross premium paid this time"]["status"] == sm.MANUALLY_CONFIRMED
    assert "Gross premium paid this time" not in prompts[0]
    assert by["Unique Market Reference (UMR)"]["method"] == sm.EXACT
    # a same-name column on a non-bordereau field is still copied, as before
    assert by["Endt No."]["status"] == sm.AUTO_MAPPED
    assert by["Reporting Period (End Date)"]["status"] == sm.NOT_FROM_INPUT
    assert "Reporting Period (End Date)" not in mapping


def test_model_key_snapped_to_the_column_asked(monkeypatch):
    report = {}
    monkeypatch.setattr(dm, "_ask_model", lambda p: (json.dumps(
        {"total GROSS written premium": {"in": "gross written premium", "s": 1}}), "STOP"))
    out = dm.model_column_candidates(["Total gross written premium"],
                                     ["Gross Written Premium"], {}, report=report)
    assert out == {"Total gross written premium":
                   {"source": "Gross Written Premium", "confidence": 1.0}}
    assert report["answered"] == ["Total gross written premium"]


def test_review_summary_states(monkeypatch):
    # direct_routes builds its DB engine on import (no connection is made)
    from dotenv import load_dotenv
    load_dotenv(".env")
    import direct_routes as dr
    _, by, _ = run(monkeypatch)
    review = dr._mapping_review({"Out": list(by.values())})
    assert review["counts"] == {"auto": 3, "verify": 1, "unmapped": 2, "ai_failed": 1}
    assert review["checked"] == 7                          # the contract field is left out
    assert review["threshold"] == 0.9 and review["review_floor"] == 0.8
    states = {e["field"]: e["state"] for e in review["entries"]}
    assert "Reporting Period (End Date)" not in states
    assert states["Commission Amount"] == "ai_failed"
    # decisions saved before the states existed still read sensibly
    old = dr._mapping_review({"S": [
        {"display_name": "A", "status": "REVIEW_REQUIRED", "candidates": []},
        {"display_name": "B", "status": "REVIEW_REQUIRED",
         "candidates": [{"source": "X", "confidence": 0.8}]}]})
    assert [e["state"] for e in old["entries"]] == ["unmapped", "verify"]


def test_every_attempt_is_logged(monkeypatch, env):
    run(monkeypatch)
    text = env.read_text()
    assert "MAPPING" in text and "5 of 8 field(s) sent to AI (2 call(s)" in text
    assert "AI-PARTIAL" in text and "unanswered: Commission Amount" in text
    line = next(l for l in text.splitlines() if "'Total gross written premium'" in l)
    for part in ("AUTO", "<- Gross Written Premium", "name 93%", "AI 97%",
                 "AI confirmed the same data"):
        assert part in line, part
    line = next(l for l in text.splitlines() if "'Commission Amount'" in l)
    assert "AI-FAILED" in line and "AI error:" in line
    line = next(l for l in text.splitlines() if "'Settlement Currency'" in l)
    assert "UNMAPPED" in line and "AI 80%" in line and "no suitable mapping" in line


def test_template_builder_ladder_unchanged():
    f = {"column_name": "Commission", "data_type": "decimal"}
    d = sm.resolve_field(f, ["Policy No"], {})
    assert (d.status, d.reason) == (sm.REVIEW_REQUIRED,
                                    "no column in this file looks like this field")
    d = sm.resolve_field(f, ["Cost"], {"Cost": MONEY},
                         semantic={"source": "Cost", "confidence": 0.95})
    assert d.status == sm.REVIEW_REQUIRED and "under the 99%" in d.reason
