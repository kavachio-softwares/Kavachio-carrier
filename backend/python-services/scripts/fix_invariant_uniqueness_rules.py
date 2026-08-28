"""Repair rules that check an INVARIANT with a UNIQUENESS query.

WHY
───
"A policy's effective date must not CHANGE across its transactions" is a per-group
invariant, but it used to be modelled as `unique` (see generic_rule_library
_INTENT_BY_CLASS), so the mapper compiled it to

    GROUP BY "Policy Number", "Policy Eff Dt" HAVING COUNT(*) > 1

which flags a policy for merely HAVING more than one row. A bordereau lists many
rows per policy (endorsements, instalments, unearned-premium movements) all
carrying the same date, so the rule flagged every ordinary multi-transaction
policy and could never surface a date that actually moved. On the WKFC NYFTZ
2026Q1 statement that was 2 696 critical exceptions — every policy in the file —
and zero real defects.

The correct query is one distinct date per policy:

    GROUP BY "Policy Number" HAVING COUNT(DISTINCT "Policy Eff Dt") > 1

The generation path now emits that shape (operator `invariant` →
`aggregate_cap` with aggregation `distinct_count`). This script converts the rules
that were generated BEFORE the fix, so they do not have to be regenerated.

WHAT IT TOUCHES
───────────────
Only rules whose IR template is `uniqueness` AND whose rule_name is a
generic_rule_specification entry whose class maps to the `invariant` operator — a
user's genuine "must be unique" rule is never rewritten. Roles come from the
uniqueness key order the mapper emits (entity key first, the value that must hold
still last), which is how the composite key is specified in the mapping prompt;
every candidate is printed so the rewrite can be checked before it is applied.

USAGE
─────
    python scripts/fix_invariant_uniqueness_rules.py            # dry run (default)
    python scripts/fix_invariant_uniqueness_rules.py --apply
    python scripts/fix_invariant_uniqueness_rules.py --apply --contract 754
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _invariant_class_names():
    """Library classes whose intent operator is `invariant` — read from the code
    catalogue, so this stays correct if more invariant classes are added."""
    from contract_upload_services.generic_rule_library import _INTENT_BY_CLASS
    return {cls for cls, entry in _INTENT_BY_CLASS.items() if entry[0] == "invariant"}


def _target_rule_names(conn):
    """Rule names in the library that are invariants, not uniqueness checks."""
    from sqlalchemy.sql import text
    classes = _invariant_class_names()
    if not classes:
        return set()
    rows = conn.execute(text(
        "SELECT rule_name FROM generic_rule_specification "
        "WHERE class_name = ANY(:cls)"), {"cls": list(classes)}).fetchall()
    return {r[0] for r in rows}


def _rewrite_ir(ir):
    """uniqueness([key…, subject]) → aggregate_cap(one distinct subject per key).
    Returns None when the IR is not a rewritable shape."""
    if (ir or {}).get("template") != "uniqueness":
        return None
    fields = [f for f in ((ir.get("params") or {}).get("fields") or []) if f]
    if len(fields) < 2:
        return None
    *key, subject = fields
    new_ir = dict(ir)
    new_ir["template"] = "aggregate_cap"
    new_ir["params"] = {"aggregation": "distinct_count", "field": subject,
                        "group_by": key, "max": 1}
    new_ir["reason"] = (f"'{subject}' must not change across the rows of one "
                        f"{', '.join(key)}: at most one distinct value per group.")
    return new_ir


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the rewritten rules (default: dry run)")
    ap.add_argument("--contract", type=int, default=None,
                    help="limit to one contract_id")
    args = ap.parse_args()

    from sqlalchemy.sql import text
    from db import canonical_engine
    from contract_upload_services.rule_compiler import (
        recompile_ir_for_sheets, sheets_in_sql)

    sql = ("SELECT rule_id, contract_id, rule_name, rule_spec "
           "FROM validation_rule "
           "WHERE rule_spec->'ir'->>'template' = 'uniqueness'")
    params = {}
    if args.contract:
        sql += " AND contract_id = :cid"
        params["cid"] = args.contract
    sql += " ORDER BY rule_id"

    fixed = skipped = 0
    with canonical_engine.begin() as conn:
        names = _target_rule_names(conn)
        if not names:
            print("No invariant classes in the rule library — nothing to do.")
            return
        rows = conn.execute(text(sql), params).mappings().all()
        print(f"{len(rows)} uniqueness rule(s) found; "
              f"{len(names)} library rule name(s) are invariants: {sorted(names)}\n")

        for r in rows:
            spec = r["rule_spec"]
            if isinstance(spec, str):
                spec = json.loads(spec)
            ir = (spec or {}).get("ir") or {}
            if r["rule_name"] not in names:
                print(f"  rule {r['rule_id']:>6}  SKIP (genuine uniqueness rule): "
                      f"{r['rule_name']}")
                skipped += 1
                continue
            new_ir = _rewrite_ir(ir)
            if not new_ir:
                print(f"  rule {r['rule_id']:>6}  SKIP (not a 2+ field uniqueness): "
                      f"{ir.get('params')}")
                skipped += 1
                continue

            old_sql = spec.get("compiled_sql") or ""
            sheets = sheets_in_sql(old_sql)
            try:
                new_sql = recompile_ir_for_sheets(new_ir, sheets)
            except Exception as exc:
                print(f"  rule {r['rule_id']:>6}  SKIP (recompile failed: {exc})")
                skipped += 1
                continue

            p = new_ir["params"]
            print(f"  rule {r['rule_id']:>6}  contract {r['contract_id']}  "
                  f"{r['rule_name']}")
            print(f"      was : GROUP BY {ir['params']['fields']} HAVING COUNT(*) > 1")
            print(f"      now : GROUP BY {p['group_by']} HAVING "
                  f"COUNT(DISTINCT {p['field']!r}) > 1   [sheets: {sheets}]")

            if args.apply:
                new_spec = dict(spec)
                new_spec["ir"] = new_ir
                new_spec["compiled_sql"] = new_sql
                just = dict(new_spec.get("justification") or {})
                just["operator"] = "aggregate_cap"
                just["mapped_field"] = p["field"]
                new_spec["justification"] = just
                conn.execute(text(
                    "UPDATE validation_rule SET rule_spec = CAST(:spec AS jsonb), "
                    "updated_at = NOW() WHERE rule_id = :rid"),
                    {"spec": json.dumps(new_spec), "rid": r["rule_id"]})
            fixed += 1

    verb = "rewritten" if args.apply else "would be rewritten"
    print(f"\n{fixed} rule(s) {verb}, {skipped} left alone.")
    if not args.apply:
        print("Dry run — re-run with --apply to write the changes.")


if __name__ == "__main__":
    main()
