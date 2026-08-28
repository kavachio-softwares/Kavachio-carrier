"""
test_upload_token.py
────────────────────
The upload's correlation id: the value that lets the caller that made a contract
upload identify the contract it produced, when the response carrying that id was
lost in transit (extraction outlives the request; the pipeline task survives and
persists, the reply does not).

Two properties matter and are tested here:

  * A row written WITHOUT a token is byte-identical to what was written before
    this existed — the token must never change existing behaviour.
  * A bad token degrades to "no token", never to a failed upload. It is
    client-supplied input on the path that saves the contract, so a hostile or
    malformed value must be inert.

Pure — no DB, no network. `build_extracted_payload` is the seam precisely so
this can be checked without writing a contract row.

Run standalone:  python contract_upload_services/tests/test_upload_token.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except ImportError:
    pass
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@127.0.0.1:5432/none")

from contract_upload_services.db_persister import (        # noqa: E402
    UPLOAD_TOKEN_MAX_LEN,
    build_extracted_payload,
    extracted_upload_token,
    normalize_upload_token,
)

META = {"program_name": {"value": "Some Programme"}}


# ── the payload written to the contract row ─────────────────────────────────

def test_payload_without_a_token_is_unchanged():
    """The exact three keys, and nothing else, for every upload that sends no
    token — including every caller other than the setup screen."""
    got = build_extracted_payload("Reinsurance Contract", "Some Programme", META)
    assert got == {
        "document_type": "Reinsurance Contract",
        "program_name": "Some Programme",
        "program_metadata": META,
    }, got


def test_payload_carries_the_token_when_given():
    got = build_extracted_payload("Reinsurance Contract", "Some Programme", META,
                                  "0d7c1e2f-aaaa-bbbb-cccc-0123456789ab")
    assert got["upload"] == {"token": "0d7c1e2f-aaaa-bbbb-cccc-0123456789ab"}, got
    # …and leaves everything else exactly as it was.
    assert got["document_type"] == "Reinsurance Contract"
    assert got["program_metadata"] is META


def _contract_constants_view(extracted):
    """Exactly what direct_routes._contract_constants keeps: every TOP-LEVEL
    scalar. Mirrored here so this test fails if that filter and the payload shape
    ever drift apart."""
    return {k: v for k, v in extracted.items() if isinstance(v, (str, int, float))}


def test_token_never_becomes_a_contract_constant():
    """The run path turns every top-level scalar in `extracted` into a constant
    available to output generation. The token is upload plumbing and must never
    appear there — so it is nested, and the constants a contract offers must be
    identical with and without it."""
    without = _contract_constants_view(build_extracted_payload("t", "p", META))
    with_tok = _contract_constants_view(
        build_extracted_payload("t", "p", META, "tok-42"))
    assert with_tok == without, (with_tok, without)
    assert "tok-42" not in with_tok.values(), with_tok


def test_a_bad_token_is_dropped_not_stored():
    """Anything that isn't a plain bounded string must vanish, leaving a payload
    identical to the no-token one. It must never reach the row and must never
    raise — this runs on the path that saves the contract."""
    clean = build_extracted_payload("t", "p", META)
    for bad in (None, "", "   ", 12345, ["x"], {"a": 1}, True,
                "x" * (UPLOAD_TOKEN_MAX_LEN + 1)):
        got = build_extracted_payload("t", "p", META, bad)
        assert got == clean, (bad, got)


def test_token_is_trimmed():
    got = build_extracted_payload("t", "p", META, "  abc123  ")
    assert extracted_upload_token(got) == "abc123", got


def test_token_at_the_length_limit_is_kept():
    edge = "x" * UPLOAD_TOKEN_MAX_LEN
    assert normalize_upload_token(edge) == edge
    assert normalize_upload_token(edge + "x") is None


# ── reading it back for the contracts list ──────────────────────────────────

def test_read_back_round_trips():
    payload = build_extracted_payload("t", "p", META, "tok-42")
    assert extracted_upload_token(payload) == "tok-42"


def test_read_back_tolerates_rows_that_predate_the_token():
    """Every contract already in the database has an `extracted` without this
    key — and some may not hold an object at all. Both must read as None, not
    raise, or the contracts list breaks for existing programmes."""
    for old in (None, {}, {"document_type": "x"}, "a string", 7, [1, 2],
                {"upload": None}, {"upload": "not-a-dict"}, {"upload": {}},
                {"upload": {"token": None}}, {"upload": {"token": ""}},
                {"upload": {"token": 123}}):
        assert extracted_upload_token(old) is None, old


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    print(f"\n{'FAILED' if fails else 'all passed'} ({fails} failure(s))")
    sys.exit(1 if fails else 0)
