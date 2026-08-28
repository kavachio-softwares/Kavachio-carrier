"""
test_malformed_field_refs.py
────────────────────────────
A field slot that holds something which is not a COLUMN NAME must fail as ONE
rule, never as the whole contract upload.

What went wrong: a generation run put a factor where a column belongs —
`cross_field_math{result_field: "Earned Premium", left_field: "Gross Written
Premium", right_field: 0.28}`. `rule_ir._make_fields_fn` keeps only `isinstance(v,
str)` values, so 0.28 was not merely rejected — it became invisible: the
field-existence gate saw two valid columns, `validate_ir` passed, and the rule
reached `rule_compiler`, where `field + ' must be a number, but found '` raised
`TypeError`. Nothing catches TypeError (the pipeline catches `CompileError`), so
the ENTIRE upload died — every other rule on that contract lost with it.

Two guards, tested here:
  * `rule_ir.malformed_field_refs` — reads the same declarative `field_params` the
    extractor is built from, so the bad slot is reported instead of dropped;
    `validate_ir` then routes that rule to review.
  * `rule_compiler._q` — the choke point every builder quotes its columns through
    raises `CompileError`, so any malformed param that reaches the compiler by any
    other path still degrades to one reviewable rule.

Run standalone:  python contract_upload_services/tests/test_malformed_field_refs.py
(or under pytest — each case asserts independently).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

# The catalog these checks read is DB-backed, so the service environment has to be
# present — load the service .env the same way main.py and scripts/ do. Read-only.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except ImportError:
    pass
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")

from contract_upload_services.rule_compiler import (   # noqa: E402
    CompileError, compile_ir,
)
from contract_upload_services.rule_ir import (         # noqa: E402
    TEMPLATE_CATALOG, malformed_field_refs, validate_ir,
)

_SHEET = "Sheet1"
_COLS = ["Earned Premium", "Gross Written Premium", "Unearned Premium"]
_F2S = {c: [_SHEET] for c in _COLS}
_NAMES = set(_COLS)

# Anything a model could put in a column slot that is not a column name. 0 and 0.0
# matter most: they are FALSY, so even a `if v` filter would let them through.
_NOT_COLUMN_NAMES = [0.28, 0, 0.0, 12, ["Gross Written Premium"], {"col": "x"}]


def _math_ir(right):
    return {"template": "cross_field_math",
            "params": {"result_field": "Earned Premium",
                       "left_field": "Gross Written Premium",
                       "right_field": right, "operator": "-"}}


def test_validate_ir_rejects_a_non_column_field_ref():
    for bad in _NOT_COLUMN_NAMES:
        ok, reason = validate_ir(_math_ir(bad), _NAMES)
        assert not ok, f"{bad!r} was accepted as a column name"
        assert "not a column name" in reason, reason


def test_rejection_reason_does_not_route_the_rule_to_the_unmapped_path():
    # rule_normalizer keeps a rule whose reason says "not in output template" and
    # compiles it anyway (the column may exist in the BDX). A value that is not a
    # NAME can never be that column, so its reason must not say so.
    _, reason = validate_ir(_math_ir(0.28), _NAMES)
    assert "not in output template" not in reason, reason


def test_compiler_raises_compile_error_not_type_error():
    for bad in _NOT_COLUMN_NAMES + [None, "", "   "]:
        try:
            compile_ir(_math_ir(bad), _F2S, default_sheet=_SHEET)
        except CompileError:
            continue
        except Exception as exc:                       # noqa: BLE001
            raise AssertionError(
                f"{bad!r} raised {type(exc).__name__} — an upload-killing error; "
                f"only CompileError is caught by the pipeline") from exc
        raise AssertionError(f"{bad!r} compiled into SQL as a column name")


def test_malformed_field_refs_reads_every_declared_field_slot():
    # Not just the first slot: every key the template DECLARES as field-valued.
    spec = TEMPLATE_CATALOG["cross_field_math"]
    bad = malformed_field_refs(spec, _math_ir(0.28)["params"])
    assert bad and "right_field" in bad[0], bad
    ok_params = _math_ir("Unearned Premium")["params"]
    assert malformed_field_refs(spec, ok_params) == []


def test_well_formed_rules_are_untouched():
    ir = _math_ir("Unearned Premium")
    ok, reason = validate_ir(ir, _NAMES)
    assert ok, reason
    sql = compile_ir(ir, _F2S, default_sheet=_SHEET)
    assert '"Unearned Premium"' in sql and '"Earned Premium"' in sql


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
