"""
test_tolerance_bands.py
───────────────────────
Standalone tests for the V-22 tolerance-band behaviour of the cross_field_math
compiler builder (`rule_compiler._b_cross_field_math`) — the "flag vs auto-reject"
logic:

  deviation ≤ tolerance_pct           → compliant  (row NOT flagged)
  tolerance_pct < deviation ≤ reject  → 'warning'   (flag, don't block)
  deviation > reject_pct              → 'violation' (rule's own severity → block)

No DB required. The compiler's real catalog is strict DB-only (catalog_store), so
we stub `contract_upload_services.rule_ir` with a catalog that only needs the
builder names, then exercise the builder directly and RUN its SQL in an in-memory
DuckDB — the same engine the runtime uses.

Run standalone:  python contract_upload_services/tests/test_tolerance_bands.py
(or under pytest — each case asserts independently).
"""
import os
import sys
import types
import importlib.util

import duckdb


# The compiler runs `assert_builders_cover_catalog()` at import, comparing its
# _BUILDERS against TEMPLATE_CATALOG. The real catalog is DB-only (catalog_store),
# so we hand it a stub catalog whose keys are exactly the builder names. This list
# only needs to COVER the builders; if a future builder is added the import will
# fail loudly here (add its name), which is a fine early warning.
_BUILDER_NAMES = [
    "aggregate_cap", "conditional_all", "conditional_required", "conditional_value",
    "cross_field_compare", "cross_field_math", "cross_field_or_value",
    "currency_country_consistency", "date_bound", "date_relation", "max_limit",
    "min_limit", "pattern_check", "period_duration", "range_check", "required_field",
    "state_validity", "uniqueness", "value_in_set", "value_not_in_set",
    "zip_state_consistency",
]


def _load_rule_compiler():
    """Import rule_compiler with a stub catalog so it loads without a DB.

    The stub lives in `sys.modules` only for the duration of that import and is
    then REMOVED: sys.modules is process-wide, so leaving a stub `rule_ir` behind
    breaks every test module that runs afterwards and imports the real one (its
    `field_refs` / `validate_ir` simply are not there). rule_compiler binds
    TEMPLATE_CATALOG at import time, so this module keeps the stub catalog it was
    given while everyone else keeps the real package."""
    stub = types.ModuleType("contract_upload_services.rule_ir")
    stub.TEMPLATE_CATALOG = {n: {"required": [], "field_params": {},
                                 "fields": lambda p: []} for n in _BUILDER_NAMES}
    pkg = types.ModuleType("contract_upload_services")
    pkg.__path__ = []
    _sentinel = object()
    prev_pkg = sys.modules.get("contract_upload_services", _sentinel)
    prev_ir = sys.modules.get("contract_upload_services.rule_ir", _sentinel)
    sys.modules.setdefault("contract_upload_services", pkg)
    sys.modules["contract_upload_services.rule_ir"] = stub

    try:
        here = os.path.dirname(__file__)
        path = os.path.join(here, "..", "rule_compiler.py")
        spec = importlib.util.spec_from_file_location("rc_bands_test", path)
        rc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rc)
        return rc
    finally:
        for name, prev in (("contract_upload_services", prev_pkg),
                           ("contract_upload_services.rule_ir", prev_ir)):
            if prev is _sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


rc = _load_rule_compiler()

# Commission Amount should equal Gross Premium × Commission Rate (rate stored as
# a percent, e.g. "10" = 10%). Rows chosen to land in each band.
_PARAMS = {
    "result_field": "Commission Amount", "left_field": "Gross Premium",
    "operator": "*", "right_field": "Commission Rate", "right_is_percent": True,
    "tolerance_pct": 1.0,   # ±1% compliant
    "reject_pct": 5.0,      # >5% is a hard violation
}
_ROWS = [
    # (__rowid, Gross Premium, Commission Rate, Commission Amount, expected zone)
    (0, "1000", "10", "100",   None),        # exact       → not flagged
    (1, "1000", "10", "100.5", None),        # 0.5% off    → within tol, not flagged
    (2, "1000", "10", "103",   "warning"),   # 3% off      → flag
    (3, "1000", "10", "120",   "violation"), # 20% off     → reject
    (4, "1000", "10", "abc",   "violation"), # non-numeric → hard violation
]


def _run(params):
    con = duckdb.connect(":memory:")
    con.execute(
        'CREATE TABLE "S" (__rowid INTEGER, "Gross Premium" VARCHAR, '
        '"Commission Rate" VARCHAR, "Commission Amount" VARCHAR)')
    con.executemany('INSERT INTO "S" VALUES (?,?,?,?)',
                    [r[:4] for r in _ROWS])
    sql = rc._b_cross_field_math("S", params)
    cols = [d[0] for d in con.execute(sql).description]
    rows = con.execute(sql).fetchall()
    return sql, cols, rows


def test_bands_split_warning_from_violation():
    sql, cols, rows = _run(_PARAMS)
    assert "zone" in cols, "banded rule must emit a per-row zone column"
    got = {r[cols.index("row_id")]: r[cols.index("zone")] for r in rows}
    expected = {rid: zone for (rid, *_rest, zone) in
                [(r[0], r[4]) for r in _ROWS] if zone is not None}
    assert got == expected, f"band zones wrong: {got} != {expected}"


def test_compliant_rows_not_flagged():
    _sql, cols, rows = _run(_PARAMS)
    flagged = {r[cols.index("row_id")] for r in rows}
    assert 0 not in flagged and 1 not in flagged, \
        "rows within tolerance must not be flagged"


def test_no_reject_pct_is_backwards_compatible():
    """Without reject_pct the SQL emits NO zone column (unchanged behaviour)."""
    params = {k: v for k, v in _PARAMS.items() if k != "reject_pct"}
    sql, cols, _rows = _run(params)
    assert "zone" not in cols, "non-banded rule must not emit a zone column"
    assert "AS zone" not in sql


def test_degenerate_reject_disables_bands():
    """reject_pct ≤ tolerance_pct is inert → no zone column."""
    params = dict(_PARAMS, reject_pct=0.5)  # below the 1% tolerance
    sql, cols, _rows = _run(params)
    assert "zone" not in cols


def test_bad_reject_pct_raises():
    try:
        rc._b_cross_field_math("S", dict(_PARAMS, reject_pct="abc"))
    except rc.CompileError:
        return
    raise AssertionError("non-numeric reject_pct should raise CompileError")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nAll tolerance-band tests passed.")
