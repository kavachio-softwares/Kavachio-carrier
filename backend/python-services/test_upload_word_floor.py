"""How short a contract may be — counted in WORDS, not bytes.

WHAT WENT WRONG. The shortest-contract rule was a 10 KB minimum FILE size. It
judged the file rather than the contract: a PDF writer that compresses well puts
a real three-page program schedule into 8.7 KB, and that contract was refused as
"too small to be a binding authority contract" before a word of it was read. The
upload screen then hid even that, and showed "please try again" — for a file
that fails identically every time.

WHAT HOLDS NOW.

  · Length is measured in words, from the same extracted text the pipeline
    reads. Under 100 is refused, and the refusal says how many there were.
  · No words at all is a DIFFERENT failure — an unreadable scan — and keeps the
    advice that fixes it, rather than calling twenty scanned pages "too short".
  · A file with nothing in it is broken, not short.

Every PDF here is built on the spot, so nothing depends on a sample file — except
the one test that proves the real contract that triggered this now passes, which
skips cleanly where that file is not on disk.

    python -m pytest test_upload_word_floor.py
"""
from __future__ import annotations

import os

import fitz
import pytest

import main  # noqa: F401 — loads .env, as every test here does
from contract_upload_services.constants import MIN_CONTRACT_WORDS
from contract_upload_services.upload_file_validator import (
    UploadContractValidationError, count_words, validate_uploaded_contract,
)

REAL_SHORT_PDF = ("/media/atdesk-72/D/Mahesh/Kavachio Carrier/demo 13 aug/"
                  "aug-13-Contract_MockRisk.pdf")


class _Upload:
    """The two attributes the validator reads off an UploadFile."""
    def __init__(self, filename: str):
        self.filename = filename
        self.content_type = "application/pdf"


def _pdf_with_words(tmp_path, n: int) -> tuple[bytes, str]:
    """A real PDF carrying exactly `n` words, wrapped across the page so none
    are clipped off its edge and silently lost to the extractor."""
    doc = fitz.open()
    page = doc.new_page()
    if n:
        body = " ".join(f"clause{i}" for i in range(n))
        left = page.insert_textbox(fitz.Rect(40, 40, 555, 800), body, fontsize=9)
        assert left >= 0, "the words did not fit on the page — the test would lie"
    path = tmp_path / f"words-{n}.pdf"
    doc.save(path)
    return path.read_bytes(), str(path)


def _verdict(data: bytes, path: str, name: str = "contract.pdf"):
    """None when the upload passes; the refusal when it does not."""
    try:
        validate_uploaded_contract(file=_Upload(name), file_bytes=data,
                                   temp_file_path=path)
        return None
    except UploadContractValidationError as e:
        return e


# ── the floor itself ───────────────────────────────────────────────────────
def test_one_word_short_is_refused_and_says_how_short(tmp_path):
    e = _verdict(*_pdf_with_words(tmp_path, MIN_CONTRACT_WORDS - 1))
    assert e is not None and e.code == "CONTRACT_TOO_SHORT"
    # The count and the minimum are both IN the sentence — "too short" alone
    # leaves the person uploading guessing by how much.
    assert f"{MIN_CONTRACT_WORDS - 1}" in e.message
    assert f"{MIN_CONTRACT_WORDS}" in e.message
    assert "too short" in e.message.lower()


def test_exactly_the_minimum_passes(tmp_path):
    assert _verdict(*_pdf_with_words(tmp_path, MIN_CONTRACT_WORDS)) is None


def test_a_small_file_with_plenty_of_words_passes(tmp_path):
    """The bug, stated as a test: the file is tiny, the contract is not."""
    data, path = _pdf_with_words(tmp_path, MIN_CONTRACT_WORDS * 3)
    assert len(data) < 10 * 1024, "fixture should be under the old 10 KB floor"
    assert _verdict(data, path) is None


@pytest.mark.skipif(not os.path.exists(REAL_SHORT_PDF),
                    reason="the MockRisk sample is not on this machine")
def test_the_contract_that_was_refused_now_passes():
    data = open(REAL_SHORT_PDF, "rb").read()
    assert _verdict(data, REAL_SHORT_PDF, os.path.basename(REAL_SHORT_PDF)) is None


# ── the failures that are NOT "too short" ─────────────────────────────────
def test_no_readable_words_keeps_the_scanned_document_advice(tmp_path):
    """A page with no text layer, which the extractor could not OCR. Telling
    that person their contract is "too short" sends them the wrong way."""
    e = _verdict(*_pdf_with_words(tmp_path, 0))
    assert e is not None and e.code == "INSUFFICIENT_TEXT"


def test_an_empty_file_is_broken_not_short(tmp_path):
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")
    e = _verdict(b"", str(path))
    assert e is not None and e.code == "CORRUPT_FILE"


def test_the_upper_size_limit_is_untouched(tmp_path):
    """Only the lower bound moved to words. The 50 MB ceiling is a different
    question — what the server will hold — and stays in bytes."""
    from contract_upload_services.constants import MAX_CONTRACT_UPLOAD_BYTES
    huge = b"%PDF-" + b"0" * MAX_CONTRACT_UPLOAD_BYTES
    e = _verdict(huge, str(tmp_path / "huge.pdf"))
    assert e is not None and e.code == "FILE_TOO_LARGE"


# ── what counts as a word ──────────────────────────────────────────────────
def test_punctuation_on_its_own_is_not_a_word():
    """A bare "—", "•" or "§" is not a word, or a page of bullets and section
    marks would pass for a contract. Anything with a letter or digit in it IS
    one — so clause numbering like "1)" counts, exactly as "10,000" does:
      1)  Fees  policy/inspection  10,000  USD   → 5
      —   •     §                               → 0"""
    assert count_words("1) Fees — • § policy/inspection 10,000 USD") == 5
    assert count_words("— • § — •") == 0


def test_words_in_other_scripts_count():
    assert count_words("Versicherungsvertrag über Äquivalenz") == 3
