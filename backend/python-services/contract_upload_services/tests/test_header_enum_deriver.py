"""
test_header_enum_deriver.py
───────────────────────────
Tests for `validation_rule_generator.derive_header_enum_entries` — the
deterministic deriver that turns a closed-set marker in a COLUMN HEADER into a
`value_in_set` rule ("Policy Type (Primary/Excess)", "FAC Y/N", "New/Renewal").

The case that motivated these tests: a header can carry a parenthesised
slash-list that is NOT a value set at all but a CALCULATION note —
"Gross Written Premium (Less FAC / Mine )" is premium net of facultative and
mine-subsidence, a money column. Reading the brackets as an allowed-value set
flagged every populated row ('0', '20000', '-18500' "not in the allowed set").
The header alone cannot tell the two apart; the column's DATA can, and when the
column is empty the template's STRUCTURE can (the same measure also present as a
plain column means the brackets are qualifying it, not enumerating it).

No DB and no LLM: the deriver is pure.

Run standalone:  python contract_upload_services/tests/test_header_enum_deriver.py
(or under pytest — each case asserts independently).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

# The deriver itself is pure, but importing its module pulls in the service's
# DB-backed constants (RULE_CLASS_LIBRARY) and the Gemini client, so the usual
# service environment has to be present — load the service .env the same way
# main.py and scripts/ do. Nothing here reads or writes application data.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except ImportError:
    pass
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")

from contract_upload_services.validation_rule_generator import (   # noqa: E402
    derive_header_enum_entries,
)


def _fields(*specs):
    """[(name, [samples]), ...] → template_fields the deriver understands."""
    return [{"name": n, "sheet": "Sheet1", "samples": list(s)[:3],
             "samples_all": list(s)} for n, s in specs]


def _derive(*specs):
    """{field: allowed} for every rule the deriver emits."""
    out = {}
    for e in derive_header_enum_entries([], _fields(*specs)):
        ir = e["candidates"][0]
        out[ir["params"]["field"]] = ir["params"]["allowed"]
    return out


# ── the reported bug ────────────────────────────────────────────────────────
def test_parenthesised_calculation_note_on_a_money_column_gets_no_rule():
    got = _derive(("Gross Written Premium", ["0", "20000", "45500"]),
                  ("Gross Written Premium (Less FAC  / Mine )",
                   ["0", "20000", "45500", "-18500"]))
    assert got == {}, got


def test_empty_calculation_note_column_gets_no_rule_via_its_plain_sibling():
    # The variant column is entirely blank, so it carries no evidence of its own
    # — but "Gross Written Premium" exists as a column in its own right, which is
    # what makes the brackets a qualifier rather than a value set.
    got = _derive(("Gross Written Premium", ["0", "20000"]),
                  ("Gross Written Premium (Less FAC / TRIA / Mine )", []))
    assert got == {}, got


# ── the cases that must keep working ────────────────────────────────────────
def test_boolean_header_needs_no_data():
    got = _derive(("FAC Y/N", []), ("Mine Subsidence Policy Y/N", []),
                  ("Terrorism Coverage Y/N", []))
    assert set(got) == {"FAC Y/N", "Mine Subsidence Policy Y/N",
                        "Terrorism Coverage Y/N"}, got
    assert got["FAC Y/N"] == ["Yes", "No", "Y", "N", "1", "0"]


def test_paren_enum_backed_by_its_own_data():
    got = _derive(("Policy Type (Primary/Excess)", ["Primary", "Excess", "Primary"]))
    assert got == {"Policy Type (Primary/Excess)": ["Primary", "Excess"]}, got


def test_paren_enum_backed_by_compound_values():
    # Real BDX columns spell the alternative as a compound value; the enum
    # compiler accepts those, so the gate must too.
    got = _derive(("Policy Type (Primary/Excess)",
                   ["Primary Casualty", "Excess Property"]))
    assert list(got) == ["Policy Type (Primary/Excess)"], got


def test_paren_enum_with_no_data_and_no_plain_sibling_still_emits():
    # Nothing contradicts the header — it is the only evidence there is.
    got = _derive(("Policy Type (Primary/Excess)", []))
    assert list(got) == ["Policy Type (Primary/Excess)"], got


def test_two_paren_variants_do_not_disqualify_each_other():
    got = _derive(("Coverage Basis (Primary/Excess)", []),
                  ("Coverage Basis (Claims Made/Occurrence)", []))
    assert len(got) == 2, got


def test_bare_slash_header_backed_by_data():
    got = _derive(("Claims Made / Occurrence", ["Occurrence", "Occurrence"]))
    assert got == {"Claims Made / Occurrence": ["Claims Made", "Occurrence"]}, got


def test_bare_slash_header_backed_by_compound_values():
    # 'NEW_BUSINESS' is how a real "New/Renewal" column spells 'New'.
    got = _derive(("New/Renewal", ["NEW_BUSINESS", "NEW_BUSINESS"]))
    assert got == {"New/Renewal": ["New", "Renewal"]}, got


def test_bare_slash_compound_column_name_gets_no_rule():
    got = _derive(("APD per Occ / Terminal Limit", ["1000000", "2500000"]),
                  ("Endorsement / Cancellation Changed Date", ["2026-03-31"]))
    assert got == {}, got


def test_short_options_are_not_prefix_matched():
    # 'A' must not "support" 'A1' — a 1-char alternative would match anything.
    got = _derive(("Coverage (A/B)", ["A1", "B2"]))
    assert got == {}, got
    got = _derive(("Coverage (A/B)", ["A", "B"]))
    assert list(got) == ["Coverage (A/B)"], got


def test_column_already_governed_by_a_contract_enum_rule_is_skipped():
    governed = [{"candidates": [{"template": "value_in_set",
                                 "params": {"field": "Policy Type (Primary/Excess)"}}]}]
    entries = derive_header_enum_entries(
        governed, _fields(("Policy Type (Primary/Excess)", ["Primary"])))
    assert entries == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print("\nall tests passed" if not failures else f"\n{failures} FAILED")
    sys.exit(1 if failures else 0)
