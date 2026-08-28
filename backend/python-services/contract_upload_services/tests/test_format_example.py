"""A format rule must show the reviewer an EXAMPLE, never its regex.

A pattern rule ("SIC Code must be 4 digits") has no value to recommend — every
string of the right shape is equally correct — so the review screen fell back to
the rule's machine wording and put `matches ^\\d{4}$` in the Recommendation
column. That is unreadable, and it was also treated as a value: Approve wrote it
into the cell and Fix pre-filled it.

`rule_explainer.format_hint` replaces it with one concrete value of the right
shape. These tests pin the two properties that make that safe:

  1. SOUND   — every example returned actually matches the pattern it came from
               (checked with `re`, on the live shapes the estate really carries).
  2. HONEST  — a pattern the walker cannot sample with certainty (a negated
               class, a lookaround, a backreference) yields NO example rather
               than a plausible-looking wrong one, and a pattern that accepts
               exactly ONE string is reported as that value, not as an example.

Run:  python contract_upload_services/tests/test_format_example.py
(pytest is not installed in this venv; every test file carries its own runner.)
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from contract_upload_services.rule_explainer import (   # noqa: E402
    format_hint, sample_for_pattern,
)

# The distinct pattern_check shapes live rules carry, spelling variants and all
# (\d vs [0-9], \s? vs \s* vs a literal space, (?:…) vs (…), anchors inside the
# alternation vs around it). Nothing here is matched on by name: the sampler
# walks the regex, so these are inputs, not a vocabulary.
LIVE_PATTERNS = [
    r"^\d{4}$",
    r"^\d{5}$",
    r"^\d{6}$",
    r"^\d{5}(-\d{4})?$",
    r"^\d{5}(?:-\d{4})?$",
    r"^(\d{5}|441105)$",
    r"^(?:\d{5}|441105)$",
    r"^\d{5}$|^441105$",
    r"^\d{5}$|441105",
    r"^[A-Z]{3}-[0-9]{5}-[0-9]{2}$",
    r"(?i)^(primary|excess)\b",
    r"^(?i)(Primary|Excess).*",
    r"^(\d{5}(-\d{4})?|[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})$",
    r"^(\d{5}(-\d{4})?|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})$",
    r"^(\d{5}(-\d{4})?|[A-Z]{1,2}[0-9][A-Z0-9]? [0-9][A-Z]{2})$",
    r"^((\d{5}(-\d{4})?)|([A-Z]{1,2}[0-9][A-Z0-9]?\s?[0-9][A-Z]{2}))$",
    r"^(?:\d{5}(?:-\d{4})?|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})$",
    r"^(?:\d{5}(?:-\d{4})?$|^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})$",
    r"(^\d{5}(-\d{4})?$)|(^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$)",
    r"(^\d{5}$)|(^\d{5}-\d{4}$)|(^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$)",
    r"^((\d{5}(-\d{4})?)|([A-Z]{1,2}[0-9][A-Z0-9]? [0-9][A-Z]{2}))?$",
    # Shapes the estate does not carry yet — the walk is generic, not a lookup.
    r"^[a-z]+@[a-z]+\.[a-z]{2,3}$",
    r"^GB\d{9}$",
    r"^\d{1,3}(,\d{3})*(\.\d{2})?$",
    r"^[0-9]{2}/[0-9]{2}/[0-9]{4}$",
    r"^(\d{2}){3}$",
    r"^[A-F]{2}$",
    r"^\w{3}-\d{2}$",
]


def _matches(pattern, value):
    """Does `value` satisfy `pattern` the way the compiled rule tests a cell?

    `regexp_matches` (rule_compiler._b_pattern_check) is a SEARCH, and `(?i)` is
    read as a flag because it appears mid-pattern in live rules.
    """
    flags = re.IGNORECASE if "(?i)" in pattern else 0
    return re.compile(pattern.replace("(?i)", ""), flags).search(value) is not None


def test_every_live_shape_gets_an_example():
    """The shapes reviewers actually meet must not fall back to the regex."""
    missing = [p for p in LIVE_PATTERNS if not sample_for_pattern(p)]
    assert not missing, f"no example built for: {missing}"


def test_every_example_matches_its_own_pattern():
    """An example that the rule itself would flag is worse than none at all."""
    for p in LIVE_PATTERNS:
        s = sample_for_pattern(p)
        assert s and _matches(p, s), f"{p!r} → {s!r} does not match"


def test_examples_are_deterministic():
    """The same rule shows the same example on every screen and every reload."""
    for p in LIVE_PATTERNS:
        assert sample_for_pattern(p) == sample_for_pattern(p), p


def test_unsamplable_constructs_are_refused_not_guessed():
    """A complement / lookaround / backreference cannot be sampled from the text.

    `[^0-9]` is every character EXCEPT a digit; a lookahead constrains text it
    does not consume. Guessing here produces an example the rule rejects, which
    is exactly the confident-but-wrong output this module exists to avoid.
    """
    for p in (r"^[^0-9]{3}$", r"^[^A-Z]+$", r"^(?!TEST)\w+$",
              r"^(?=.*\d)\w+$", r"^(\w+)-\1$", r"^\D{3}$"):
        assert sample_for_pattern(p) is None, f"{p!r} should not be sampled"


def test_junk_and_unbounded_repeats_never_raise():
    """Bad input yields None — a broken pattern must not break the screen.

    An unbounded repeat is refused on purpose: an example is an illustration,
    and 100 digits illustrates nothing (it would also be the widest cell on the
    screen).
    """
    for p in (None, "", "   ", "not a regex (((", r"^\d{100}$", r"^X{500}$"):
        assert sample_for_pattern(p) is None, repr(p)
    # A non-string pattern is coerced, not crashed on: 12345 reads as the regex
    # "12345", whose only value is "12345".
    assert sample_for_pattern(12345) == "12345"


def test_a_pattern_with_one_answer_is_a_value_not_an_example():
    """`^441105$` names THE value — it should be recommended, not illustrated."""
    hint = format_hint("pattern_check", {"field": "Class Code",
                                         "pattern": r"^441105$"})
    assert hint["exact"] == "441105", hint
    assert hint["example"] is None, hint


def test_a_real_format_is_an_example_not_a_value():
    hint = format_hint("pattern_check", {"field": "Account SIC",
                                         "pattern": r"^\d{4}$"})
    assert hint["exact"] is None, hint
    assert hint["example"] == "1234", hint
    assert hint["format"] == "4 digits", hint


def test_the_example_never_leaks_regex_syntax():
    """The example replaces the regex; it must not read like one."""
    for p in LIVE_PATTERNS:
        s = sample_for_pattern(p)
        assert not re.search(r"[\\\[\]{}^$*+?|]", s), f"{p!r} → {s!r}"


def test_only_format_templates_are_illustrated():
    """A rule whose expectation IS a value needs no example — an enum's allowed
    values, a bound's number and a date are already the answer."""
    for template, params in (
        ("value_in_set", {"field": "Carrier", "allowed": ["A", "B"]}),
        ("max_limit", {"field": "Limit", "max": 1000}),
        ("range_check", {"field": "Rate", "min": 0.1, "max": 0.3}),
        ("required_field", {"field": "Insured City"}),
        ("date_bound", {"field": "Inception", "op": ">=", "date": "2026-01-01"}),
        ("uniqueness", {"fields": ["Policy Number"]}),
    ):
        assert format_hint(template, params) is None, template


def test_format_hint_never_raises():
    """The hint is best-effort enrichment — it must never take a screen down."""
    for junk in (None, "", 123, [], {}, {"pattern": None}, {"pattern": 42},
                 {"pattern": "((("}, {"pattern": r"^\d{4}$", "field": None}):
        got = format_hint("pattern_check", junk)
        assert got is None or isinstance(got, dict), repr(junk)


if __name__ == "__main__":
    import traceback
    fails = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception:
                fails += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print("ALL PASSED" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
