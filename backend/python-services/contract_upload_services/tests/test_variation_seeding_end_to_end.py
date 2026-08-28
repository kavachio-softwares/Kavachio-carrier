"""
test_variation_seeding_end_to_end.py
────────────────────────────────────
Proves the seeded spellings reach the SQL and change what the rule does on real
rows — the part a params-level test cannot show.

Each case builds the same rule twice, compiles both, and runs them against a
one-sheet DuckDB:

  BEFORE — params exactly as a model that omitted `variation_values` leaves them
  AFTER  — the same params through normalize_variation_values (structural seeding)

Run standalone:  python -m contract_upload_services.tests.test_variation_seeding_end_to_end
"""

import duckdb

from contract_upload_services.rule_compiler import compile_ir
from contract_upload_services.rule_normalizer import normalize_variation_values

_SHEET = "Policy"


def _con(field, cells):
    con = duckdb.connect(":memory:")
    con.execute(f'CREATE TABLE "{_SHEET}" (__rowid INTEGER, "{field}" VARCHAR)')
    con.executemany(f'INSERT INTO "{_SHEET}" VALUES (?, ?)',
                    [[i, c] for i, c in enumerate(cells, start=1)])
    return con


def _violations(ir, field, cells):
    sql = compile_ir(ir, {field: [_SHEET]}, default_sheet=_SHEET)
    con = _con(field, cells)
    try:
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    return sql, rows


def _rule(template, field, values, seeded):
    key = "allowed" if template == "value_in_set" else "excluded"
    params = {"field": field, key: list(values)}
    if seeded:
        params = normalize_variation_values(template, params)
    return {"template": template, "params": params,
            "severity": "critical", "error_message": "x"}


def test_allow_list_stops_false_flagging_a_short_carrier_spelling():
    """The BDX writes the carrier the way brokers do; the contract wrote the full
    legal name. Before seeding that row is a violation."""
    field, value = "Carrier Entity", "Palms Specialty Insurance Company, Inc."
    cell = ["Palms Specialty"]

    _, before = _violations(_rule("value_in_set", field, [value], False), field, cell)
    assert before, "expected the un-seeded rule to false-flag the short spelling"

    sql, after = _violations(_rule("value_in_set", field, [value], True), field, cell)
    assert "Palms Specialty" in sql
    assert not after, f"seeded rule still flags {cell}: {after}"


def test_exclusion_catches_the_initialism_it_used_to_miss():
    """An exclusion the bordereau reports by initials is silently passed before
    seeding — the dangerous direction: a violation nobody sees."""
    field, value = "Carrier Entity", "Volante Specialty Risks, LLC"
    cell = ["VSRL"]

    _, before = _violations(_rule("value_not_in_set", field, [value], False), field, cell)
    assert not before, "fixture assumption wrong: initialism already caught"

    sql, after = _violations(_rule("value_not_in_set", field, [value], True), field, cell)
    assert "VSRL" in sql
    assert after, "seeded rule still misses the excluded carrier written as VSRL"


def test_seeding_does_not_admit_an_unrelated_carrier():
    """Widening must stay narrow: a different company still fails the allow-list."""
    field, value = "Carrier Entity", "Palms Specialty Insurance Company, Inc."
    cells = ["Northwind Casualty Group", "Insurance Company", "Limited"]
    _, hits = _violations(_rule("value_in_set", field, [value], True), field, cells)
    assert len(hits) == len(cells), f"an unrelated value passed the allow-list: {hits}"


def test_compiled_sql_carries_every_kept_spelling():
    template, field = "value_in_set", "Carrier Entity"
    values = ["MS TRANSVERSE INSURANCE COMPANY",
              "MS TRANSVERSE SPECIALTY INSURANCE COMPANY"]
    ir = _rule(template, field, values, True)
    sql = compile_ir(ir, {field: [_SHEET]}, default_sheet=_SHEET)
    for v in ir["params"]["variation_values"]:
        assert v.replace("'", "''") in sql, f"{v!r} never reached the SQL"


if __name__ == "__main__":
    import sys, traceback
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
