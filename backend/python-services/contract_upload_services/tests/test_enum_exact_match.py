"""
test_enum_exact_match.py
────────────────────────
Enum rules match a cell to a value by EQUALITY (after normalization and the
shared vocabulary), never by string similarity.

The case that motivated them: RULE-3481 "Excluded Territory" on upload 109. The
rule excludes the US territories and carries 'NMI' among its spellings for
Northern Mariana Islands. Seven New Mexico policies were reported as writing in
an excluded territory, because jaro-winkler scores

    'nm'  vs  'nmi'  =  0.91

above the 0.90 match threshold. Raising the threshold cannot fix that — the false
pair scores HIGHER than genuine variants ('nite club' vs 'night club' = 0.89) —
so the scoring is gone: identity now comes from normalization, the curated
vocabulary, and the rule's own `variation_values`.

Asserted here: `rule_compiler` compiles enum and conditional rules to queries that
carry no similarity function, still flag every real violation, and no longer flag
a value that merely resembles one. Runs the compiled SQL against an in-memory
DuckDB, so these are the rows a bordereau would really produce.

No DB and no LLM.

Run standalone:  python contract_upload_services/tests/test_enum_exact_match.py
(or under pytest — each case asserts independently).
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

import duckdb                                                     # noqa: E402

from contract_upload_services.rule_compiler import (               # noqa: E402
    compile_ir, compiled_sql_is_stale,
)

SHEET = "Policies"

# The real RULE-3481 parameters (contract 97 / upload 109).
_TERRITORY_EXCLUDED = ["Puerto Rico", "US Virgin Islands", "Guam",
                       "American Samoa", "Northern Mariana Islands",
                       "US Minor Outlying Islands"]
_TERRITORY_VARIATIONS = [
    *_TERRITORY_EXCLUDED, "UMOI", "NMI", "PR", "USVI", "VI",
    "U.S. Virgin Islands", "Virgin Islands", "GU", "AS", "CNMI", "MP",
    "Commonwealth of the Northern Mariana Islands", "UM",
    "United States Minor Outlying Islands"]


def _run(ir, column, rows):
    """Compile `ir` and run it over a one-column sheet. Returns {value: reason}
    for the rows it flags."""
    con = duckdb.connect()
    con.execute(f'CREATE TABLE "{SHEET}" (__rowid BIGINT, "{column}" VARCHAR)')
    con.executemany(f'INSERT INTO "{SHEET}" VALUES (?, ?)',
                    [(i, v) for i, v in enumerate(rows, 1)])
    sql = compile_ir(ir, {column: [SHEET]}, default_sheet=SHEET)
    assert "jaro_winkler_similarity" not in sql, "similarity is back in the SQL"
    out = con.execute(sql).fetchall()
    cols = [d[0] for d in con.description]
    return {dict(zip(cols, r))["actual_value"]: dict(zip(cols, r))["reason"]
            for r in out}


def _run2(ir, columns, rows):
    """Same, for a rule reading two columns. `rows` are tuples."""
    con = duckdb.connect()
    cols_sql = ", ".join(f'"{c}" VARCHAR' for c in columns)
    con.execute(f'CREATE TABLE "{SHEET}" (__rowid BIGINT, {cols_sql})')
    con.executemany(
        f'INSERT INTO "{SHEET}" VALUES ({", ".join(["?"] * (len(columns) + 1))})',
        [(i, *r) for i, r in enumerate(rows, 1)])
    sql = compile_ir(ir, {c: [SHEET] for c in columns}, default_sheet=SHEET)
    assert "jaro_winkler_similarity" not in sql, "similarity is back in the SQL"
    return [r[0] for r in con.execute(sql).fetchall()]


def _excluded_ir(**extra):
    params = {"field": "Risk State", "excluded": _TERRITORY_EXCLUDED,
              "variation_values": _TERRITORY_VARIATIONS}
    params.update(extra)
    return {"template": "value_not_in_set", "params": params}


# ── the failure this pins ────────────────────────────────────────────────────

def test_new_mexico_is_not_the_northern_mariana_islands():
    flagged = _run(_excluded_ir(), "Risk State",
                   ["NM", "New Mexico", "NY", "CA", "TX", "GA", "AL", "VA"])
    assert flagged == {}, flagged


def test_every_real_excluded_territory_still_flags():
    rows = ["Puerto Rico", "PR", "Guam", "GU", "Northern Mariana Islands", "MP",
            "American Samoa", "US Virgin Islands", "NM", "NY"]
    flagged = _run(_excluded_ir(), "Risk State", rows)
    assert set(flagged) == {"Puerto Rico", "PR", "Guam", "GU",
                            "Northern Mariana Islands", "MP", "American Samoa",
                            "US Virgin Islands"}
    assert "similarity" not in flagged["Guam"]
    assert "matches excluded value 'Guam'" in flagged["Guam"]


def test_punctuation_and_spacing_are_still_not_differences():
    """Normalization is kept — it is the part that carries no meaning."""
    flagged = _run(_excluded_ir(), "Risk State",
                   ["U.S. Virgin Islands", "us virgin islands", "  Guam  ",
                    "PUERTO RICO", "New Mexico"])
    assert set(flagged) == {"U.S. Virgin Islands", "us virgin islands",
                            "  Guam  ", "PUERTO RICO"}


def test_a_near_spelling_no_longer_matches_an_allowed_value():
    """The other side of the same coin: value_in_set no longer accepts a value
    just because it looks like an allowed one. 'Excess Casualty' is not 'Excess'."""
    ir = {"template": "value_in_set",
          "params": {"field": "Policy Type", "allowed": ["Primary", "Excess"]}}
    flagged = _run(ir, "Policy Type", ["Primary", "Excess", "Excesss", "Umbrella"])
    assert set(flagged) == {"Excesss", "Umbrella"}
    assert "is not in the allowed set" in flagged["Umbrella"]


def test_an_allowed_value_passes_through_its_recorded_spelling():
    ir = {"template": "value_in_set",
          "params": {"field": "Writing Company",
                     "allowed": ["Obsidian Specialty Insurance Company"],
                     "variation_values": ["Obsidian Specialty Insurance Company",
                                          "Obsidian Specialty", "OSIC"]}}
    flagged = _run(ir, "Writing Company",
                   ["Obsidian Specialty Insurance Company", "Obsidian Specialty",
                    "OSIC", "obsidian  specialty", "Obsidian Casualty Company"])
    assert set(flagged) == {"Obsidian Casualty Company"}


# ── blanks, scope and the other paths through the builder ────────────────────

def test_blank_cells_are_never_flagged():
    for ir in (_excluded_ir(),
               {"template": "value_in_set",
                "params": {"field": "Risk State", "allowed": ["TX"]}}):
        assert _run(ir, "Risk State", [None, "", "   "]) == {}


def test_a_scope_filter_still_narrows_the_rows():
    ir = {"template": "value_not_in_set",
          "params": {"field": "Risk State", "excluded": ["Guam"],
                     "scope": {"Line": "Property"}}}
    flagged = _run2(ir, ["Risk State", "Line"],
                    [("Guam", "Property"), ("Guam", "Casualty"), ("TX", "Property")])
    assert flagged == [1]


def test_both_polarities_in_one_rule_still_compile_and_run():
    """allowed + excluded on one rule emits both arms."""
    ir = {"template": "value_in_set",
          "params": {"field": "Risk State", "allowed": ["TX", "NM"],
                     "excluded": ["Guam"]}}
    flagged = _run(ir, "Risk State", ["TX", "NM", "Guam", "NY"])
    assert set(flagged) == {"Guam", "NY"}


# ── the conditional templates widen SPELLING, never meaning ──────────────────

def test_conditional_target_accepts_a_punctuation_variant_of_its_value():
    ir = {"template": "conditional_value",
          "params": {"condition": {"field": "Risk State", "op": "=",
                                   "value": "California"},
                     "field": "Paper", "op": "=",
                     "value": "Palms Specialty Insurance Company Inc"}}
    flagged = _run2(ir, ["Risk State", "Paper"], [
        ("California", "Palms Specialty Insurance Company Inc"),      # 1 exact
        ("California", "Palms Specialty Insurance Company, Inc."),    # 2 punctuation
        ("California", "PALMS  SPECIALTY  INSURANCE  COMPANY  INC"),  # 3 spacing
        ("California", "Palms Casualty Insurance Company Inc"),       # 4 different
        ("Texas",      "Palms Casualty Insurance Company Inc"),       # 5 not in scope
    ])
    assert flagged == [4]


def test_conditional_target_does_not_accept_a_lookalike_company():
    """What the removed similarity did accept: a name one letter off."""
    ir = {"template": "conditional_value",
          "params": {"condition": {"field": "Risk State", "op": "=",
                                   "value": "California"},
                     "field": "Paper", "op": "=", "value": "Demoshield Specialty"}}
    flagged = _run2(ir, ["Risk State", "Paper"], [
        ("California", "Demoshield Specialty"),      # 1 exact
        ("California", "Demoshield Specialtyy"),     # 2 typo — no longer waved through
        ("California", "Demoshield Speciality"),     # 3 typo
    ])
    assert flagged == [2, 3]


def test_conditional_all_carries_the_same_normalization():
    ir = {"template": "conditional_all",
          "params": {"conditions": [{"field": "Risk State", "op": "=",
                                     "value": "California"},
                                    {"field": "Line", "op": "=",
                                     "value": "Property"}],
                     "field": "Paper", "op": "=", "value": "Palms Specialty, Inc."}}
    flagged = _run2(ir, ["Risk State", "Line", "Paper"], [
        ("California", "Property", "Palms Specialty Inc"),
        ("California", "Property", "Palms Casualty Inc"),
        ("Texas",      "Property", "Palms Casualty Inc"),
    ])
    assert flagged == [2]


# ── every rule already in the database is fixed on its next run ──────────────

def test_a_stored_query_that_still_scores_is_stale():
    """Rules carry their compiled SQL, so removing the scoring from the compiler
    is not enough — a stored query that scores must be recompiled at run time."""
    old = ("SELECT t.__rowid FROM \"Policies\" AS t, LATERAL ("
           "SELECT x.v, jaro_winkler_similarity(x.vn, 'nm') AS s "
           "FROM (VALUES ('NMI','nmi')) AS x(v, vn)) AS m WHERE m.s >= 0.9")
    assert compiled_sql_is_stale("value_not_in_set", old)
    assert compiled_sql_is_stale("conditional_value", old)


def test_a_freshly_compiled_enum_query_is_not_stale():
    sql = compile_ir(_excluded_ir(), {"Risk State": [SHEET]}, default_sheet=SHEET)
    assert not compiled_sql_is_stale("value_not_in_set", sql)


def test_unrelated_stale_checks_are_untouched():
    assert compiled_sql_is_stale("state_validity", "SELECT 1 FROM x")
    assert not compiled_sql_is_stale("value_not_in_set", "")
    assert not compiled_sql_is_stale("max_limit", "SELECT 1 FROM x")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'OK'} — {failures} failure(s)")
    sys.exit(1 if failures else 0)
