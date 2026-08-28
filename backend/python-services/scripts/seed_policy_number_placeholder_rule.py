"""Seed the global "Policy Number Must Not Be a Placeholder Value" rule.

WHY
───
"Policy Number Not Null" (Critical, already in the library) catches a MISSING
policy number. It cannot catch a PLACEHOLDER one — "0", "-", "N/A" are non-empty
strings, so every presence check passes them. The row then becomes a policy AND
a party natural id ("insured::-"), so every row carrying the same placeholder
collides onto one bogus party.

This is the policy-number twin of the Program Administrator rule (id 9), which
splits the same concern the same way — see reword_generic_rule_library.py, whose
note explains why a not_in_set check cannot also cover the blank case:

    "The compiled check excludes the placeholder values only — it cannot flag a
     blank. Row 15 covers the missing-value case on the same column."

The placeholder VALUES live in code, on the NoNullOrUnknown entry of
generic_rule_library._INTENT_BY_CLASS — this script only creates the rule row
that binds that class to the policy-number column.

Requirement: Palms BDX Ingestion BRD Validations v1.2, Appendix 2 §2.1, and
Appendix 1 "Policy Number Not NaN-like or Double Digit" (Minor). Graded Critical
here to match "Policy Number Not Null", because an unusable policy identifier
breaks the link between a premium row and everything downstream of it.

SCOPE
─────
Writes ONE global row (tenant_id IS NULL). Idempotent: re-running finds the
existing rule by name and leaves it alone. Never touches a tenant's own rules.

BEFORE YOU APPLY
────────────────
Confirm no tenant legitimately uses a bare "0" as a policy number, or this will
reject real data:

    SELECT tenant_id, COUNT(*) FROM policy
    WHERE lower(btrim(policy_number)) IN ('0','-','--','tbd')
    GROUP BY tenant_id;

USAGE
─────
    python -m scripts.seed_policy_number_placeholder_rule            # dry run
    python -m scripts.seed_policy_number_placeholder_rule --apply
    python -m scripts.seed_policy_number_placeholder_rule --sql      # for QA/prod
"""
from __future__ import annotations

import argparse
import os
import sys

_SVC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SVC_DIR)

# db.py reads DATABASE_URL at import time and only main.py loads the .env file,
# so a script run standalone would otherwise build the engine with None.
# override=False, so an already-exported DATABASE_URL still wins.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_SVC_DIR, ".env"), override=False)
except ImportError:
    pass

RULE = {
    "rule_name": "Policy Number Must Not Be a Placeholder Value",
    "severity": "Critical",
    "class_name": "NoNullOrUnknown",
    "validation_logic": (
        "The policy number must be a real identifier, not a placeholder such as "
        "'0', '-', 'N/A', 'TBD' or 'Unknown'. A completely missing policy number "
        "is covered by the Policy Number Not Null rule."
    ),
}


def _sql_literal(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def emit_sql() -> None:
    """Print the INSERT so QA/prod can be done by whoever owns those DBs."""
    print("-- Seed the global policy-number placeholder rule (tenant_id IS NULL).")
    print("INSERT INTO generic_rule_specification")
    print("       (rule_name, severity, class_name, validation_logic, "
          "is_generic, tenant_id, is_active)")
    print("SELECT {n}, {s}, {c}, {l}, TRUE, NULL, TRUE".format(
        n=_sql_literal(RULE["rule_name"]), s=_sql_literal(RULE["severity"]),
        c=_sql_literal(RULE["class_name"]),
        l=_sql_literal(RULE["validation_logic"])))
    print("WHERE NOT EXISTS (")
    print("    SELECT 1 FROM generic_rule_specification")
    print(f"    WHERE rule_name = {_sql_literal(RULE['rule_name'])} "
          "AND tenant_id IS NULL")
    print(");")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the row (default is a dry run)")
    ap.add_argument("--sql", action="store_true",
                    help="print the INSERT statement and exit (for QA/prod)")
    args = ap.parse_args()

    if args.sql:
        emit_sql()
        return 0

    from sqlalchemy.sql import text
    from db import canonical_engine

    # The class must be one the generator recognises, or the rule would be
    # stored and then silently generate nothing.
    from contract_upload_services.generic_rule_library import is_supported_class
    if not is_supported_class(RULE["class_name"]):
        print(f"ABORT: class {RULE['class_name']!r} is not in _INTENT_BY_CLASS — "
              f"the rule would generate nothing.")
        return 1

    with canonical_engine.begin() as conn:
        existing = conn.execute(
            text("SELECT id, severity, class_name FROM generic_rule_specification "
                 "WHERE rule_name = :n AND tenant_id IS NULL"),
            {"n": RULE["rule_name"]},
        ).mappings().first()

        if existing:
            print(f"[{existing['id']}] already seeded "
                  f"({existing['severity']} / {existing['class_name']}) — nothing to do.")
            return 0

        print(f"WILL INSERT (global, tenant_id IS NULL)")
        print(f"   name:     {RULE['rule_name']}")
        print(f"   severity: {RULE['severity']}")
        print(f"   class:    {RULE['class_name']}")
        print(f"   logic:    {RULE['validation_logic']}")

        if args.apply:
            new_id = conn.execute(
                text("INSERT INTO generic_rule_specification "
                     "       (rule_name, severity, class_name, validation_logic, "
                     "        is_generic, tenant_id, is_active) "
                     "VALUES (:n, :s, :c, :l, TRUE, NULL, TRUE) "
                     "RETURNING id"),
                {"n": RULE["rule_name"], "s": RULE["severity"],
                 "c": RULE["class_name"], "l": RULE["validation_logic"]},
            ).scalar()
            print(f"\nInserted as id {new_id}.")
        else:
            print("\nDry run — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
