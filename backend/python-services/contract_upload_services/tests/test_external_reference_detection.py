"""
test_external_reference_detection.py
────────────────────────────────────
`detect_deferred_external_references` is the deterministic backstop that decides
whether a contract upload PAUSES to ask for a document it defers rules to, and —
since the same list is recorded on the contract — what a saved setup later names
as "built without". Both readings are user-facing, so a sloppy name is not a
cosmetic problem: it asks someone to go find a document that does not exist.

Three things it has to get right, all structural (no document-name vocabulary):

  * A name ENDS at its document-type noun. Running to the next punctuation
    instead turned an ordinary sentence containing one of those nouns into a
    "document" — "Program Fee Schedule as soon as possible but in any event
    within 10 working days of the end of each month" was a real observed name.
  * Several of the type nouns also occur mid-title ("Transportation **Facility**
    Underwriting Guidelines"), so the name must run to the LAST one, not the first.
  * One document cited two ways is one document. Contracts rarely repeat a title
    verbatim, and listing every spelling asks for documents that don't exist.

Run standalone:  python contract_upload_services/tests/test_external_reference_detection.py
(or under pytest — each case asserts independently).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

from contract_upload_services.validation_rule_generator import (  # noqa: E402
    detect_deferred_external_references as detect,
    drop_covered_references,
    filter_external_references,
)


def _names(*texts):
    return sorted(d["document_name"]
                  for d in detect([{"text": t, "page": 1} for t in texts]))


def test_name_stops_at_its_document_noun():
    """The sentence that follows the title is not part of the title."""
    assert _names(
        "reimbursement of expenses on the same basis as provided for in the "
        "Program Fee Schedule to this Agreement, payable monthly"
    ) == []          # nothing document-shaped directly after the lead-in

    # The alias and the word after it are not part of the title either.
    assert _names(
        'Claims are handled as set forth in the Service Level Agreement ("SLA") below.'
    ) == ["Service Level Agreement"]


def test_title_runs_to_the_last_type_noun():
    """"Facility" is itself a type noun, mid-title here — the name must not
    stop there."""
    assert _names(
        "Excluded Classes of Business: as defined by the Zyvarqen "
        "Transportation Facility Underwriting Guidelines dated 8-1-2025."
    ) == ["Zyvarqen Transportation Facility Underwriting Guidelines"]


def test_one_document_cited_three_ways_is_one_document():
    got = detect([
        {"text": "Authorized Classes: as more fully defined in the Zyvarqen "
                 "Transportation Facility Underwriting Guidelines dated 8-1-2025.",
         "page": 1},
        {"text": "Target Segments: as defined by the Zyvarqen Transportation "
                 "Underwriting Guidelines dated 8-1-2025.", "page": 2},
        {"text": "Portfolio Parameters: as defined by the Zyvarqen Transportation "
                 "Facility Underwriting Guidelines effective 8-1-2025.", "page": 2},
    ])
    assert [d["document_name"] for d in got] == [
        "Zyvarqen Transportation Facility Underwriting Guidelines"]
    # the merged entry keeps every clause and page it was cited from
    assert len(got[0]["source_texts"]) == 3
    assert sorted(got[0]["pages"]) == [1, 2]


def test_date_is_captured_from_either_wording():
    dated, = detect([{"text": "per the ABC Underwriting Manual dated 8-1-2025.", "page": 1}])
    effective, = detect([{"text": "per the ABC Underwriting Manual effective 8-1-2025.", "page": 1}])
    assert dated["version_or_date"] == effective["version_or_date"] == "8-1-2025"


def test_self_reference_is_not_an_external_document():
    """A contract pointing at ITSELF must never be listed — there is nothing for
    the user to upload."""
    assert _names("The Administrator shall comply with all terms of this Agreement.") == []
    assert _names("Fees are payable pursuant to the Agreement.") == []
    assert _names("Limits are $250,000 per Auto and $2,000,000 per Terminal.") == []


def test_provision_cross_reference_is_not_a_document_to_supply():
    """Pointing at a PROVISION of the governing contract is not deferring rule
    content to an external file — there is nothing for the user to go and find."""
    assert _names(
        "Company may terminate for cause pursuant to Article 9 of the "
        "Program Administration Agreement."
    ) == []


def test_the_parent_agreement_is_not_a_reference_on_any_of_its_citations():
    """Verbatim from contract 920 — the three sentences the model offered as
    evidence for "Program Administration Agreement", none of which defers to it:

      1. the preamble reciting what this contract IS (nothing precedes the name,
         and "SUPER Specialty" even contains the bare "per" lead-in);
      2. a sentence that never names the document;
      3. a pointer to a PROVISION of it — this one DOES carry a real lead-in
         ("pursuant to"), and it is what put the entry on a user's screen.

    All three have to fail, because one survivor is enough to list it.
    """
    entry = {
        "document_name": "Program Administration Agreement",
        "version_or_date": "October 1, 2025",
        "source_texts": [
            "Program Administration Agreement by and between Demoshield Insurance "
            "Company, Limited, Demoshield Specialty Insurance Company, Inc. and "
            'SUPER Specialty Insurance Company, Inc. (collectively, the "Company")',
            "Any term not defined herein shall have the meaning set forth in the Agreement.",
            "Company may terminate for cause pursuant to Article 9 of the "
            "Program Administration Agreement.",
        ],
        "pages": [1], "confidence": 0.9,
    }
    assert filter_external_references([entry]) == []
    # and each citation on its own, so a future change can't pass by accident
    for one in entry["source_texts"]:
        assert filter_external_references([{**entry, "source_texts": [one]}]) == [], one


def test_a_lead_in_must_introduce_the_document_not_just_share_a_sentence():
    """The lead-in has to run INTO the name. Half a sentence away it belongs to
    something else."""
    assert filter_external_references([{
        "document_name": "ABC Underwriting Guidelines", "version_or_date": None,
        "source_texts": ["Premium is payable per policy, and separately the parties "
                         "may agree to amend the ABC Underwriting Guidelines."],
        "pages": [1], "confidence": 0.8,
    }]) == []


def test_a_document_type_used_as_an_ordinary_noun_is_not_a_document():
    """"placed on Quantum schedule" — lowercase, so it is not part of a title."""
    assert filter_external_references([{
        "document_name": "Quantum schedule", "version_or_date": None,
        "source_texts": ["All facultative reinsurance placements to be placed on "
                         "Quantum schedule and documented accordingly."],
        "pages": [2], "confidence": 0.8,
    }]) == []


def test_the_same_guideline_cited_with_two_dates_is_one_document():
    """The LLM emits one entry per date it sees; the user is asked to upload the
    result, so two entries means hunting for a document that does not exist."""
    kept = filter_external_references([
        {"document_name": "Zyvarqen Transportation Facility Underwriting Guidelines",
         "version_or_date": "8-1-2025", "pages": [1], "confidence": 0.9,
         "source_texts": ["as more fully defined in the Zyvarqen Transportation "
                          "Facility Underwriting Guidelines dated 8-1-2025."]},
        {"document_name": "Zyvarqen Transportation Facility Underwriting Guidelines",
         "version_or_date": "7-1-2024", "pages": [2], "confidence": 0.9,
         "source_texts": ["Any quotations/Policies outside the Zyvarqen Transportation "
                          "Facility Underwriting Guidelines on file with Company "
                          "dated 7-1-2024."]},
    ])
    assert [d["document_name"] for d in kept] == [
        "Zyvarqen Transportation Facility Underwriting Guidelines"]
    assert kept[0]["version_or_date"] == "8-1-2025"
    assert sorted(kept[0]["pages"]) == [1, 2]      # both citations kept on the one entry


def test_a_resolved_deferral_still_reads_as_one():
    """A clause whose deferral was resolved from a SUPPLIED document keeps its
    "as defined by …" wording and an inlined "[Context from …]" block, so the
    text-only detector still matches it. Callers therefore drop those clauses
    before calling — otherwise a document the user already gave us is reported
    missing. This pins the property that makes that filter necessary."""
    resolved = {"text": "Excluded Classes: as defined by the ABC Underwriting "
                        "Guidelines. [Context from ABC Underwriting Guidelines - "
                        "Table 4: Energy, Heavy Industrial]", "page": 1}
    assert [d["document_name"] for d in detect([resolved])] == ["ABC Underwriting Guidelines"]
    assert detect([c for c in [resolved] if "[Context from" not in c["text"]]) == []


def test_a_supplied_document_is_recognised_by_CONTENT_not_filename():
    """Nobody saves the file under the name the contract cites it by. It arrives
    as "Fac Guide v3 FINAL.docx", so matching on filename would keep asking for a
    document that is already attached. A document states its own title, so the
    text is what is matched."""
    ref = [{"document_name": "Zyvarqen Transportation Facility Underwriting Guidelines",
            "version_or_date": "8-1-2025"}]

    # the title is in the document, the filename says nothing
    assert drop_covered_references(ref, [{
        "name": "Fac Guide v3 FINAL.docx",
        "text": "ZYVARQEN TRANSPORTATION FACILITY UNDERWRITING GUIDELINES\n"
                "Effective 8-1-2025\nTable 3 — Eligible Occupancies"}]) == []

    # the contract cites it with an owner prefix the cover page doesn't print
    assert drop_covered_references(ref, [{
        "name": "guide.pdf",
        "text": "Transportation Facility Underwriting Guidelines — Table 3"}]) == []

    # a different document does NOT silence the request
    assert drop_covered_references(ref, [{
        "name": "Fac Guide.docx",
        "text": "Claims Handling Manual for auto physical damage"}]) == ref

    # and with nothing attached, nothing changes
    assert drop_covered_references(ref, []) == ref
    assert drop_covered_references(ref, None) == ref


def test_the_pipeline_output_carries_the_reference_documents():
    """Detecting them is worthless if they don't leave the pipeline.

    The final output is assembled by `_build_final_output`, a DIFFERENT method
    from the one that finds the references — so the two values have to be handed
    to it explicitly. Getting that wrong doesn't fail until a real upload is
    minutes deep in extraction, and then only as a bare NameError.
    """
    from contract_upload_services.validation_rule_generator import (
        ValidationRuleGenerator)

    g = ValidationRuleGenerator.__new__(ValidationRuleGenerator)  # method needs no state
    common = dict(source_file="c.pdf", contract_id="c1", program_metadata={},
                  commercial_terms=[], clauses_extracted=[], classifications=[],
                  validation_rules=[], dropped_candidates=[])

    out = ValidationRuleGenerator._build_final_output(
        g, **common,
        external_references=[{"document_name": "ABC Underwriting Guidelines",
                              "version_or_date": "8-1-2025",
                              "source_texts": ["per the ABC Underwriting Guidelines"],
                              "pages": [1]}],
        reference_documents=[{"name": "Fac Guide.docx", "text": "…"}],
    )
    assert [r["document_name"] for r in out["external_references"]] == [
        "ABC Underwriting Guidelines"]
    assert out["reference_documents_provided"] == ["Fac Guide.docx"]

    # An upload with neither still produces both keys, empty — persistence and
    # the setup screen read them unconditionally.
    bare = ValidationRuleGenerator._build_final_output(g, **common)
    assert bare["external_references"] == []
    assert bare["reference_documents_provided"] == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nAll external-reference detection cases passed.")
