"""The deterministic half of the missing-BDX-column note.

WHAT WENT WRONG. Three complaints from one setup, and each is a different way
the note said something the reviewer could see was untrue:

  · every clause under "Clauses Awaiting a Column" read "Untitled clause",
    while the extraction had recorded a perfectly good title for each one;
  · "Underwriting Guidelines Version" was reported as a column the bordereau
    lacked, when a guidelines version is a document reference and would be the
    same on every row of the spreadsheet;
  · "Authorized Underwriter Name" was reported missing from a bordereau whose
    underwriter column is headed `UW` — the reviewer could see it on screen.

The model is asked in the prompt not to do the last two. These tests hold the
part that does not depend on it having listened: plain, generic checks over
ordinary insurance shorthand, with nothing in them naming a carrier, a
programme or a template.

    python -m pytest test_missing_columns.py
"""
from __future__ import annotations

import main  # noqa: F401  — loads .env, which db needs at import
import missing_columns as mc

# A real bordereau's headings, shortened. `UW` is the point of the exercise: a
# bordereau names its columns the way a spreadsheet does, not the way a contract
# does.
BDX = {"Sheet 2": ["AccountID", "Policy No", "Insured", "UW", "Gross Premium",
                   "Net Premium", "Commission Rate", "Reinsurer", "BtotalTIV"]}


def _names(items):
    return [i["column_name"] for i in items]


def _finding(name, severity="required"):
    return {"column_name": name, "severity": severity}


# ── a column the bordereau has under a shorthand heading ───────────────────
def test_an_abbreviated_heading_still_counts_as_present():
    """`UW` is the underwriter column. Reporting it missing sends a reviewer
    looking for a gap they can see is not there, which is worse than reporting
    nothing at all."""
    kept = _names(mc._drop_false_positives(
        [_finding("Authorized Underwriter Name")], BDX))
    assert kept == []


def test_a_qualifier_the_bordereau_does_not_carry_is_still_missing():
    """The match runs ONE WAY. `Gross Premium` covers "Premium" because the
    finding names nothing the column does not; it does not cover "Net Premium",
    which names something else entirely. A two-way match would quietly hide the
    distinction every reinsurance bordereau turns on."""
    kept = _names(mc._drop_false_positives(
        [_finding("Premium"), _finding("Ceded Premium")], BDX))
    assert kept == ["Ceded Premium"]


def test_a_gap_the_bordereau_really_has_survives():
    """The guard has to be able to say nothing. A bordereau with no fee column
    at all must still be told it has no fee column."""
    kept = _names(mc._drop_false_positives(
        [_finding("Policy Fees"), _finding("Filing Fees")], BDX))
    assert kept == ["Policy Fees", "Filing Fees"]


def test_a_bordereau_we_could_not_read_hides_nothing():
    """No columns known is not "every column present". A setup whose input
    capture is missing must report its findings, not swallow them."""
    kept = _names(mc._drop_false_positives([_finding("Policy Fees")], {}))
    assert kept == ["Policy Fees"]


# ── a document is not a column ─────────────────────────────────────────────
def test_a_document_or_its_edition_is_not_a_column():
    """Every row of the bordereau would carry the same answer, which is the
    definition of something that is not a column of it."""
    kept = _names(mc._drop_false_positives([
        _finding("Underwriting Guidelines Version"),
        _finding("Underwriting Guidelines Version (Previous)"),
        _finding("Facultative Reinsurance Placement Documentation"),
        _finding("Policy Fees"),
    ], BDX))
    assert kept == ["Policy Fees"]


# ── one column asked for twice ─────────────────────────────────────────────
def test_a_value_and_its_amount_are_one_column():
    """The prompt already says so; nothing enforced it, so a fee clause came
    back as both "Policy Fees" and "Policy Fees Amount"."""
    folded = _names(mc._collapse_respellings(
        [{"column_name": "Policy Fees", "severity": "required"},
         {"column_name": "Policy Fees Amount", "severity": "required"}]))
    assert folded == ["Policy Fees"]


def test_a_measure_word_inside_a_name_is_not_stripped():
    """Only a TRAILING one, and never from a one-word name: "Total Insured
    Value" is not the insured, and folding the two would lose a real column."""
    folded = _names(mc._collapse_respellings(
        [{"column_name": "Total Insured Value", "severity": "required"},
         {"column_name": "Insured", "severity": "required"}]))
    assert folded == ["Total Insured Value", "Insured"]


def test_folding_never_demotes_an_obligation():
    """Two spellings, one required: the survivor is required."""
    folded = mc._collapse_respellings(
        [{"column_name": "Policy Fees", "severity": "recommended"},
         {"column_name": "Policy Fees Amount", "severity": "required"}])
    assert folded[0]["severity"] == "required"


# ── the words themselves ───────────────────────────────────────────────────
def test_a_qualifier_does_not_change_which_value_a_column_holds():
    """"Insured" and "Insured Name" are one column. "Gross" and "Net" are
    two — the filler list is short on purpose."""
    assert mc._meaning_tokens("Insured Name") == mc._meaning_tokens("Insured")
    assert mc._meaning_tokens("Net Premium") != mc._meaning_tokens("Gross Premium")
