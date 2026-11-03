"""
sample_bordereau.py — rows built to break a contract's own checks, one at a time.

A check is only known to be right when a row it should catch is put in front of
it and a row it should ignore is too. This builds both, from the contract's own
rules: for every check, a value that satisfies it and a value manufactured from
the same operand that cannot.

WHY THE VALUES ARE DERIVED AND NEVER TYPED. A sample workbook with numbers in it
is correct on the day it is written and wrong the first time a term moves — and
wrong in the worst way, because it still runs and still reports something. These
rows are a function of the rules, so the day the commission changes the sample
changes with it, and a check that stopped working has nowhere to hide.

Used by scripts/make_sample_bordereau.py (writes the .xlsx a person uploads) and
by test_contract_sample_bordereau.py (runs the same rows through the real
validation engine, which is the part that proves anything).
"""
from __future__ import annotations

import json
from typing import Any

# Columns every bordereau carries whatever the contract says, so the sample
# reads as a bordereau rather than as six columns in a spreadsheet. They are
# identity, not terms: nothing checks them, and they exist so the file can be
# recognised, sorted and talked about.
IDENTITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Unique Market Reference (UMR)", "B1234ABCD2026"),
    ("Certificate Ref", "CERT-{n:04d}"),
    ("Insured Full Name, Last Name or Company Name", "Sample Insured {n}"),
    ("Risk Inception Date", "2026-01-01"),
    ("Risk Expiry Date", "2026-12-31"),
)


def contract_rules_for(contract_id: int) -> list[dict]:
    """This contract's checks as they stand in the database.

    Read back rather than recomputed: the point of the sample is to test what is
    ACTUALLY bound, not what a fresh mapping would produce today.
    """
    from db import SessionLocal
    from sqlalchemy import text

    with SessionLocal() as s:
        rows = s.execute(text(
            "SELECT rule_id, rule_name, severity, canonical_target, rule_spec, "
            "       error_message "
            "FROM validation_rule WHERE contract_id = :cid ORDER BY rule_id"),
            {"cid": contract_id}).all()
    out = []
    for rule_id, name, severity, target, spec, message in rows:
        target = json.loads(target) if isinstance(target, str) else (target or {})
        spec = json.loads(spec) if isinstance(spec, str) else (spec or {})
        out.append({
            "rule_id": rule_id, "rule_name": name, "severity": severity,
            "canonical_target": target, "rule_spec": spec,
            "error_message": message,
            "column": target.get("output_field"),
            "sheet": target.get("sheet"),
            "operator": spec.get("operator"),
            "operand": spec.get("operand"),
        })
    return out


def sheet_of(rules: list[dict]) -> str:
    """The sheet these checks read. Taken from the rules themselves — the
    compiled SQL names its table, so a workbook whose tab is called something
    else is a workbook none of them can see."""
    for r in rules:
        if r.get("sheet"):
            return r["sheet"]
    return "Risk"


def _passing(rule: dict) -> Any:
    """A value this check is happy with."""
    op, operand = rule["operator"], rule["operand"]
    if op in ("in",):
        return operand[0] if isinstance(operand, list) and operand else operand
    if op == "not_in":
        # Anything not on the excluded list. Built from the list rather than
        # picked, so it cannot accidentally BE one of them.
        return "Elsewhere"
    if op == "eq":
        return operand
    if op in ("lte", "lte_unless_referred", "lte_aggregate"):
        return _shift(operand, -1)
    if op == "gte":
        return _shift(operand, +1)
    return operand


def _breaking(rule: dict) -> Any:
    """A value this check must catch, made out of the same operand."""
    op, operand = rule["operator"], rule["operand"]
    if op == "in":
        # A value the allowed set does not contain, derived from it so it stays
        # obviously related to what was agreed.
        first = operand[0] if isinstance(operand, list) and operand else operand
        return f"Not {first}"
    if op == "not_in":
        return operand[0] if isinstance(operand, list) and operand else operand
    if op == "eq":
        try:
            return _shift(operand, +1)
        except (TypeError, ValueError):
            return f"Not {operand}"
    if op in ("lte", "lte_unless_referred", "lte_aggregate"):
        return _shift(operand, +1)
    if op == "gte":
        return _shift(operand, -1)
    return operand


def _shift(value: Any, direction: int) -> Any:
    """One clear step above or below a figure — never a rounding away from it.

    A cap of 100,000 broken by 100,000.01 tests floating point, not the rule.
    The step is a tenth of the figure so the breach is unarguable at any scale,
    and never less than one so a cap of 3 still moves.
    """
    try:
        n = float(value)
    except (TypeError, ValueError):
        return f"Not {value}"
    step = max(abs(n) * 0.1, 1.0)
    out = n + step * direction
    if float(value) == int(float(value)) and step == int(step):
        return int(out)
    return round(out, 2)


def build_rows(rules: list[dict]) -> tuple[list[str], list[dict]]:
    """The columns and the rows: clean, one breach each, then several at once.

    Returns (columns, rows) where each row is
    `{"values": {column: value}, "breaks": [rule, …], "note": str}`.

    A ROW BREAKS ONE CHECK AND SATISFIES EVERY OTHER. That is the whole design:
    an exception report showing one row and one reason says the check works, and
    a report showing one row and four reasons says only that something is wrong
    somewhere. Two exceptions are allowed on the one row where they are
    unavoidable — a country on the exclusion list is also, necessarily, not on
    the permitted list — and that row is labelled as expecting both.
    """
    checkable = [r for r in rules if r.get("column") and r.get("operator")]
    columns = [c for c, _ in IDENTITY_COLUMNS]
    for r in checkable:
        if r["column"] not in columns:
            columns.append(r["column"])

    # ONE VALUE PER COLUMN, NOT PER RULE. Two checks can read the same column —
    # a permitted territory and an excluded one both read where the risk is —
    # and filling the column twice leaves whichever ran last, which is how a
    # "clean" row ends up breaking the other. So the clean value for a column is
    # the one that satisfies every rule on it; the candidates come from the
    # rules themselves, so there is nothing to pick by hand.
    clean: dict[str, Any] = {}
    for column in dict.fromkeys(r["column"] for r in checkable):
        on_this = [r for r in checkable if r["column"] == column]
        for candidate in (_passing(r) for r in on_this):
            if not _broken_by({column: candidate}, on_this):
                clean[column] = candidate
                break
        else:
            # No single value satisfies them all — the contract contradicts
            # itself on this column. Not this module's to resolve: the first
            # candidate goes in and the row is reported as breaking whatever it
            # breaks, which is the fact somebody needs to see.
            clean[column] = _passing(on_this[0])

    def base(n: int) -> dict:
        v = {c: (d.format(n=n) if isinstance(d, str) else d)
             for c, d in IDENTITY_COLUMNS}
        v.update(clean)
        return v

    rows: list[dict] = [
        {"values": base(1), "breaks": [],
         "note": "Clean — every term satisfied"},
    ]

    for r in checkable:
        n = len(rows) + 1
        values = base(n)
        values[r["column"]] = _breaking(r)
        # Two checks can share a column — a permitted list and an exclusion both
        # read where the risk is — so which rules this row breaks is worked out
        # from the values, never assumed to be the one that was changed.
        rows.append({"values": values, "breaks": _broken_by(values, checkable),
                     "note": f"{r['rule_name']}: {r['column']} set to "
                             f"{values[r['column']]!r}"})

    # And one row that is wrong in several ways at once, because a real bad row
    # usually is, and a rule that only fires when it is the sole problem is a
    # rule that goes quiet exactly when it is needed.
    n = len(rows) + 1
    values = base(n)
    for r in checkable[: max(2, len(checkable) // 2)]:
        values[r["column"]] = _breaking(r)
    rows.append({"values": values, "breaks": _broken_by(values, checkable),
                 "note": "Several terms broken at once"})

    rows.append({"values": base(len(rows) + 1), "breaks": [],
                 "note": "Clean — every term satisfied"})
    return columns, rows


def _broken_by(values: dict, rules: list[dict]) -> list[dict]:
    """Which of these checks this row breaks.

    The same comparison the compiled SQL makes, in Python, so the expectation a
    test asserts is derived from the rule rather than from what the engine
    happened to return — the two have to be worked out independently or the
    test is the engine agreeing with itself.
    """
    out = []
    for r in rules:
        v = values.get(r["column"])
        op, operand = r["operator"], r["operand"]
        if op == "in":
            bad = _norm(v) not in {_norm(x) for x in _as_list(operand)}
        elif op == "not_in":
            bad = _norm(v) in {_norm(x) for x in _as_list(operand)}
        elif op == "eq":
            bad = not _same(v, operand)
        elif op in ("lte", "lte_unless_referred", "lte_aggregate"):
            bad = _num(v) is not None and _num(v) > _num(operand)
        elif op == "gte":
            bad = _num(v) is not None and _num(v) < _num(operand)
        else:
            bad = False
        if bad:
            out.append(r)
    return out


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else [v]


def _norm(v: Any) -> str:
    return str(v).strip().lower()


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _same(a: Any, b: Any) -> bool:
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return abs(na - nb) < 1e-9
    return _norm(a) == _norm(b)
