"""
test_compiler_fixtures.py
─────────────────────────
Golden / fixture tests for the deterministic IR → DuckDB compiler.

For every template in fixtures/templates.json:
  - validate_ir against the fixture schema field set,
  - compile_ir → SQL,
  - run the SQL against pass_rows ONLY  → expect ZERO violations,
  - run the SQL against pass_rows + fail_rows → expect ≥1 violation
    (and never flag a pass row at the row level).

This validates the compiler (polarity, value-cleaning, group handling) rather
than each generated rule, and catches polarity regressions in CI. It is also a
guard that every catalog template has a working builder + fixture.

Run standalone:  python -m contract_upload_services.tests.test_compiler_fixtures
(or under pytest — each template is asserted independently).
"""

import os
import json

import duckdb

from contract_upload_services.rule_ir import validate_ir, TEMPLATE_NAMES
from contract_upload_services.rule_compiler import compile_ir


_FIX_PATH = os.path.join(os.path.dirname(__file__), "..", "fixtures", "templates.json")


def _load_fixtures():
    with open(_FIX_PATH) as f:
        return json.load(f)["fixtures"]


def _make_con(schema, rows):
    """Build an in-memory DuckDB with one sheet, all VARCHAR + __rowid, and the
    given rows inserted in order."""
    con = duckdb.connect(":memory:")
    for sheet, cols in schema.items():
        coldefs = ", ".join(f'"{c}" VARCHAR' for c in cols)
        con.execute(f'CREATE TABLE "{sheet}" (__rowid INTEGER, {coldefs})')
        placeholders = ", ".join(["?"] * (len(cols) + 1))
        data = []
        for i, r in enumerate(rows, start=1):
            data.append([i] + [r.get(c) for c in cols])
        if data:
            con.executemany(f'INSERT INTO "{sheet}" VALUES ({placeholders})', data)
    return con


def _field_to_sheet(schema):
    return {c: sheet for sheet, cols in schema.items() for c in cols}


def run_one(fx):
    template = fx["template"]
    ir = fx["ir"]
    schema = fx["schema"]
    field_names = {c for cols in schema.values() for c in cols}
    f2s = _field_to_sheet(schema)

    ok, reason = validate_ir(ir, field_names)
    assert ok, f"[{template}] validate_ir failed: {reason}"

    sql = compile_ir(ir, f2s)

    # pass rows only → no violations
    con = _make_con(schema, fx["pass_rows"])
    pass_hits = con.execute(sql).fetchall()
    con.close()
    assert len(pass_hits) == 0, (
        f"[{template}] flagged a passing row: {pass_hits}\nSQL: {sql}"
    )

    # pass + fail → at least one violation
    con = _make_con(schema, fx["pass_rows"] + fx["fail_rows"])
    all_hits = con.execute(sql).fetchall()
    con.close()
    assert len(all_hits) >= 1, (
        f"[{template}] did NOT flag the failing row(s)\nSQL: {sql}"
    )
    return sql


def test_every_template_has_a_fixture():
    covered = {fx["template"] for fx in _load_fixtures()}
    missing = set(TEMPLATE_NAMES) - covered
    assert not missing, f"templates with no fixture: {sorted(missing)}"


def test_all_fixtures():
    for fx in _load_fixtures():
        run_one(fx)


def _main():
    fixtures = _load_fixtures()
    covered = {fx["template"] for fx in fixtures}
    missing = set(TEMPLATE_NAMES) - covered
    if missing:
        print(f"FAIL: templates with no fixture: {sorted(missing)}")
        return 1

    failures = 0
    for fx in fixtures:
        try:
            run_one(fx)
            print(f"  ok   {fx['template']}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL {fx['template']}: {exc}")
    print(f"\n{len(fixtures) - failures}/{len(fixtures)} templates passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
