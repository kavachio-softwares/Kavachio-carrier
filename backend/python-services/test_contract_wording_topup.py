"""
test_contract_wording_topup.py — a term agreed AFTER the wording was written.

Pure module tests: no database, no HTTP. Everything here is a function of its
arguments.

THE FAILURE THIS EXISTS FOR. The wording is written once, from the terms as
they stood at that moment, and from then on it is the carrier's text — never
rebuilt underneath them, or every edit would be thrown away by the next visit.
That is right for the words and wrong for a term set later: fill in the basics,
look at the wording (two sections), go back and agree a commission, and the
commission had no sentence anywhere in the document. It was checked on every
row, printed on the schedule, listed on Read It Through — and stated nowhere in
the clauses either side signs. Because the PDF is composed from those sections,
it was missing from the draft download and from the copy that goes out for
signature too, which is where somebody finally notices.

So the wording is TOPPED UP: a term with no sentence gets one, and nothing else
is touched. The three properties that makes it safe, each held below:

  · what the carrier typed survives, to the character;
  · a clause deleted on purpose stays deleted;
  · running it twice changes nothing the first run did not.
"""
from pathlib import Path

from dotenv import load_dotenv

# Same two lines as test_contract_terms.py: db.py builds its engine at import
# time, and contract_types is reached through it.
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

import re

import fitz

import contract_types as ct
import contract_wording as cw

BASICS = {
    "name": "Spectrum Transportation binder",
    "inception_dt": "2026-01-01",
    "expiry_dt": "2026-12-31",
}


def _limits(**raw):
    return ct.clean_agreed_limits({k: {"value": v} for k, v in raw.items()})


def _wording(**raw):
    """The wording as it would first be written, from these terms and no more."""
    return cw.build_sections(values=BASICS, limits=_limits(**raw))


def _keys(sections):
    return [s["key"] for s in sections]


def _body(sections, key):
    return next(s["body"] for s in sections if s["key"] == key)


# ── the bug, exactly as reported ────────────────────────────────────────────

def test_the_basics_alone_write_two_sections():
    """The starting point of the report: nothing agreed yet, so there is
    nothing to say about cover, authority or money."""
    secs = _wording()
    assert _keys(secs) == ["parties", "reporting"]


def test_a_term_agreed_after_the_wording_was_written_gets_a_clause():
    secs = _wording()                       # basics only
    limits = _limits(commission_max_pct=12.5)

    topped, added = cw.missing_clauses(secs, values=BASICS, limits=limits)

    assert added == ["commission_max_pct"]
    assert "financial" in _keys(topped)
    # The TOKEN, never the number: that is what keeps the sentence moving when
    # the term does.
    assert "{{commission_max_pct}}" in _body(topped, "financial")
    assert "12.5" not in _body(topped, "financial")


def test_the_new_clause_is_read_where_it_belongs():
    """Financial terms before the reporting clause, not tacked on the end. A
    contract whose money terms appear after its settlement clause is one
    somebody has to re-read to trust."""
    topped, _ = cw.missing_clauses(_wording(), values=BASICS,
                                   limits=_limits(commission_max_pct=12.5))
    assert _keys(topped).index("financial") < _keys(topped).index("reporting")


def test_the_pdf_states_the_term_that_was_added_late():
    """The whole point. The screen was never the problem — the document was."""
    limits = _limits(commission_max_pct=12.5)
    topped, _ = cw.missing_clauses(_wording(), values=BASICS, limits=limits)
    pdf = cw.compose_pdf(
        name=BASICS["name"], carrier_name="Insurisk", counterparty_name="CRC",
        sections=topped,
        tokens=cw.token_values(values=BASICS, limits=limits),
        schedule=cw.schedule_rows(values=BASICS, limits=limits))
    text = "".join(p.get_text() for p in fitz.open(stream=pdf, filetype="pdf"))
    assert "commission not exceeding 12.5%" in " ".join(text.split())


def test_before_the_top_up_the_pdf_said_nothing_about_it():
    """The other half of the test above — without it, the one above could pass
    on a document that always said this."""
    limits = _limits(commission_max_pct=12.5)
    pdf = cw.compose_pdf(
        name=BASICS["name"], carrier_name="Insurisk", counterparty_name="CRC",
        sections=_wording(),                      # the stale two sections
        tokens=cw.token_values(values=BASICS, limits=limits),
        schedule=[])                              # schedule off, so only clauses
    text = " ".join("".join(
        p.get_text() for p in fitz.open(stream=pdf, filetype="pdf")).split())
    assert "12.5%" not in text


def test_the_term_stops_being_reported_as_unstated():
    """derive_checks warns when a term is checked and stated nowhere. The
    top-up is what that warning was asking for, so it has to clear."""
    limits = _limits(commission_max_pct=12.5)
    stale = _wording()
    _, before = cw.derive_checks(BASICS, limits, stale)
    assert any("does not quote" in w["title"] for w in before)

    topped, _ = cw.missing_clauses(stale, values=BASICS, limits=limits)
    _, after = cw.derive_checks(BASICS, limits, topped)
    assert not [w for w in after if "does not quote" in w["title"]]


# ── what must not happen ────────────────────────────────────────────────────

def test_every_word_the_carrier_typed_survives():
    secs = _wording()
    typed = "1.9  Notices are given to the Broker's London office."
    secs = [{**s, "body": s["body"] + "\n" + typed,
             "origin": "from your terms · edited"} if s["key"] == "parties"
            else s for s in secs]

    topped, _ = cw.missing_clauses(secs, values=BASICS,
                                   limits=_limits(commission_max_pct=12.5))

    # The sentence, to the character. Its NUMBER may move — numbers are
    # positional and always have been — but the words are the contract.
    assert "Notices are given to the Broker's London office." in \
        _body(topped, "parties")


def test_a_clause_deleted_on_purpose_stays_deleted():
    """Otherwise deleting a clause would be a thing you cannot do: it would be
    back the next time the screen re-read the wording."""
    limits = _limits(commission_max_pct=12.5)
    topped, _ = cw.missing_clauses(_wording(), values=BASICS, limits=limits)
    without = [s for s in topped if s["key"] != "financial"]

    again, added = cw.missing_clauses(
        without, values=BASICS, limits=limits,
        dropped_sections=["financial"], dropped_terms=["commission_max_pct"])

    assert added == []
    assert "financial" not in _keys(again)


def test_running_it_twice_changes_nothing_the_first_run_did_not():
    limits = _limits(commission_max_pct=12.5, brokerage_pct=2)
    once, first = cw.missing_clauses(_wording(), values=BASICS, limits=limits)
    twice, second = cw.missing_clauses(once, values=BASICS, limits=limits)
    assert first and second == []
    assert once == twice


def test_a_second_term_lands_in_the_section_already_there():
    """Not a second Financial terms section — one heading, both sentences."""
    secs = _wording(commission_max_pct=12.5)     # financial already exists
    topped, added = cw.missing_clauses(
        secs, values=BASICS, limits=_limits(commission_max_pct=12.5, brokerage_pct=2))

    assert added == ["brokerage_pct"]
    assert _keys(topped).count("financial") == 1
    body = _body(topped, "financial")
    assert "{{commission_max_pct}}" in body and "{{brokerage_pct}}" in body


def test_boilerplate_is_not_copied_in_twice():
    """The financial section carries one sentence that quotes no term. Adding a
    term to a section that already has it must not bring a second copy."""
    secs = _wording(commission_max_pct=12.5)
    topped, _ = cw.missing_clauses(
        secs, values=BASICS, limits=_limits(commission_max_pct=12.5, brokerage_pct=2))
    body = _body(topped, "financial")
    assert body.count("Commission shall be shown separately") == 1


def test_a_section_added_whole_keeps_its_own_boilerplate():
    """The other side of it: when the section was never there, its standing
    sentence comes with it."""
    topped, _ = cw.missing_clauses(_wording(), values=BASICS,
                                   limits=_limits(commission_max_pct=12.5))
    assert "Commission shall be shown separately" in _body(topped, "financial")


def test_a_clause_the_carrier_wrote_themselves_is_left_exactly_as_typed():
    own = {"key": "custom_1", "title": "Notices",
           "body": "1. Notices shall be in writing.",
           "origin": "your own words"}
    topped, _ = cw.missing_clauses(_wording() + [own], values=BASICS,
                                   limits=_limits(commission_max_pct=12.5))
    assert next(s for s in topped if s["key"] == "custom_1") == own


def test_a_wording_that_does_not_exist_yet_is_not_invented_here():
    """An empty wording is build_sections' job — the wizard's "start again"
    button and the first visit both go through it. This one only ever adds to
    a document that already exists, or it would quietly become a second way of
    writing contracts."""
    out, added = cw.missing_clauses([], values=BASICS,
                                    limits=_limits(commission_max_pct=12.5))
    assert (out, added) == ([], [])


# ── the numbers ─────────────────────────────────────────────────────────────

def _numbers(sections):
    return [m.group(1)
            for s in sections
            for line in s["body"].split("\n")
            if (m := re.match(r"^(\d+\.\d+)\s", line.strip()))]


def test_the_clause_numbers_read_in_order_after_a_section_is_added():
    """A section inserted in the middle renumbers everything after it, or the
    document reads 3.1, 3.2, 3.1 — which looks like a clause went missing."""
    topped, _ = cw.missing_clauses(_wording(), values=BASICS,
                                   limits=_limits(commission_max_pct=12.5,
                                                  max_sum_insured=500000,
                                                  currency="GBP"))
    nums = _numbers(topped)
    assert nums == sorted(nums, key=lambda n: [int(p) for p in n.split(".")])
    assert len(nums) == len(set(nums))
    # One run of clause numbers per section, starting at n.1 each time.
    firsts = [n for n in nums if n.endswith(".1")]
    assert [int(n.split(".")[0]) for n in firsts] == \
        list(range(1, len(firsts) + 1))


def test_a_sentence_added_to_an_existing_section_is_numbered_too():
    """Appended lines arrive without a number and get one from their position.
    A single unnumbered line in a numbered contract is the tell that something
    was bolted on."""
    secs = _wording(commission_max_pct=12.5)
    topped, _ = cw.missing_clauses(
        secs, values=BASICS,
        limits=_limits(commission_max_pct=12.5, brokerage_pct=2))
    body = _body(topped, "financial")
    assert all(re.match(r"^\d+\.\d+\s", line.strip())
               for line in body.split("\n") if line.strip())


def test_a_basic_filled_in_later_does_not_rewrite_the_wording():
    """AGREED TERMS only. The parties, the period and the class of business are
    not terms that get agreed later — they are written once, with the wording —
    and completing them afterwards would be this rewriting somebody's document
    rather than finishing it."""
    secs = cw.build_sections(values=BASICS, limits={})
    later = {**BASICS, "class_of_business": "Commercial Motor"}

    topped, added = cw.missing_clauses(secs, values=later, limits={})

    assert added == []
    assert topped == secs


def test_a_wording_of_one_section_stays_a_wording_of_one_section():
    """A contract saved with a single clause — an API client, or a carrier who
    deleted the rest — is not quietly given the others back."""
    only = [{"key": "financial", "title": "Financial terms",
             "origin": "generated",
             "body": "1.1  Commission is payable at {{commission_pct}}."}]

    topped, added = cw.missing_clauses(only, values=BASICS,
                                       limits=_limits(commission_pct=11))

    assert (topped, added) == (only, [])


# ── reading a deletion off the difference ───────────────────────────────────

def test_what_a_save_took_out_is_read_from_the_difference():
    """The record screen sends the wording as it now stands and says nothing
    about what was removed, so the deletion is the difference."""
    before = _wording(commission_max_pct=12.5)
    after = [s for s in before if s["key"] != "financial"]

    gone_sections, gone_terms = cw.dropped_between(before, after)

    assert gone_sections == ["financial"]
    assert "commission_max_pct" in gone_terms


def test_a_clause_rewritten_in_plain_words_counts_as_taking_the_term_out():
    """The case a re-tie cannot rescue: the chip is gone and the sentence now
    says something else. That wants the record's warning, not a second clause
    written beside the one somebody rewrote."""
    before = _wording(commission_max_pct=12.5)
    after = [{**s, "body": "1.1  Accounts are settled as agreed."}
             if s["key"] == "financial" else s for s in before]

    _, gone_terms = cw.dropped_between(before, after)
    again, added = cw.missing_clauses(
        after, values=BASICS, limits=_limits(commission_max_pct=12.5),
        dropped_terms=gone_terms)

    assert added == []
    assert "{{commission_max_pct}}" not in _body(again, "financial")
