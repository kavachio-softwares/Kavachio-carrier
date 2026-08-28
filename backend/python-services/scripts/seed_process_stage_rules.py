"""Seed the BDX-Process-stage rules for Appendix 2 §2.2, §2.7 and §2.8.

WHY
───
Each of these three rules is ENFORCED at the Data Model stage (ingest_record
drops or defaults the row) but was INVISIBLE at the BDX Process stage — the
broker was never told. A guard that silently discards a row satisfies the
letter of the requirement and fails the person who has to fix the file.
BRD §4.8 asks for both: reject it, and say why.

The enforcement half already exists in code. This script adds the reporting
half, which is a library rule the mapper binds to a column and DuckDB evaluates
over output records.

WHAT IS MISSING, AND WHY EACH ONE
─────────────────────────────────
§2.2  There is no presence check on the policy effective date at all. The
      library has 'Policy Effective Date Must Remain Consistent' (id 30, an
      INVARIANT — one distinct value per policy) and 'Transaction Effective
      Date Must Be Valid' (id 39, a DATE RELATION). Neither fires on a BLANK.
      Confirmed against contract 898's generated set: 45 rules, including
      'Policy Number Must Not Be Null', but nothing covering a missing policy
      effective date.

§2.7  'Policy Currency Must Be Valid' (id 5, Minor) checks the code is a real
      ISO currency. It does NOT check that a foreign-currency row carries a
      rate. The dangerous case — a genuine EUR amount sitting at par because
      exchg_rate defaulted to 1.0 — passes it. A default cannot prevent that;
      only a check can.

§2.8  Nothing validates the transaction type. The ingest default now records
      'unknown' rather than asserting 'new', which makes the unclassified rows
      FINDABLE — but findable is not reported. This rule turns them into an
      exception the broker sees.

NOT COVERED HERE
────────────────
§2.1 is already seeded — 'Policy Number Must Not Be a Placeholder Value'
(scripts/seed_policy_number_placeholder_rule.py).

§2.10 cannot be expressed with the classes that exist: no operator joins a
policy's effective date to a contract's [inception, expiry) band. That needs a
new _INTENT_BY_CLASS entry plus a DuckDB operator, which is the same work item
as validation_rule.effective_from / effective_to (data_model.py:1135-1136,
declared but unread) — and it unblocks 36 contract-derived Appendix 1 rules.

AFTER APPLYING
──────────────
Rule generation runs at CONTRACT UPLOAD. Existing contracts keep the rule set
they were generated with, so these bind only after the contract is re-uploaded
or generation is re-run for the program. Nothing changes for an already-loaded
BDX until then.

SCOPE
─────
Writes global rows (tenant_id IS NULL). Idempotent: an existing rule of the
same name is left alone. Never touches a tenant's own rules.

USAGE
─────
    python -m scripts.seed_process_stage_rules            # dry run
    python -m scripts.seed_process_stage_rules --apply
    python -m scripts.seed_process_stage_rules --sql      # for QA/prod
"""
from __future__ import annotations

import argparse
import os
import sys

_SVC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SVC_DIR)

# db.py reads DATABASE_URL at import time and only main.py loads the .env file,
# so a script run standalone would otherwise build the engine with None.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_SVC_DIR, ".env"), override=False)
except ImportError:
    pass

RULES = [
    {
        "ref": "2.2",
        "rule_name": "Policy Effective Date Must Not Be Null",
        "severity": "Critical",
        "class_name": "NotNull",
        "validation_logic": (
            "Every policy row must carry a policy effective date. The effective "
            "date is the join key for contract resolution, the commission band "
            "and the accounting period, so a missing one does not produce a "
            "single wrong field — it resolves all three confidently to the wrong "
            "answer. Rows without it are excluded from the data model."
        ),
    },
    {
        "ref": "2.7",
        "rule_name": "Foreign Currency Requires an Exchange Rate",
        "severity": "Critical",
        "class_name": "ConditionalRequired",
        "validation_logic": (
            "Where the transaction currency is not the reporting currency, an "
            "exchange rate and its as-of date must both be present. A non-USD "
            "amount recorded at par with no rate date has not been converted and "
            "must not reach the sub-ledger."
        ),
    },
    {
        "ref": "2.8",
        "rule_name": "Transaction Type Must Be a Recognised Value",
        "severity": "Critical",
        "class_name": "ValueInSet",
        "validation_logic": (
            "The transaction type must be one of the accepted values: new, "
            "renewal, endorsement, cancellation, installment, adjustment, "
            "reinstatement, flat cancellation or audit. A code that does not map "
            "is recorded as 'unknown', which means the row was NOT classified — "
            "it is not a valid transaction type and must be corrected at source."
        ),
    },
]


def _sql_literal(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def emit_sql() -> None:
    """Print the INSERTs so QA/prod can be done by whoever owns those DBs."""
    print("-- BDX-Process-stage rules for Appendix 2 §2.2, §2.7, §2.8.")
    print("-- Global rows (tenant_id IS NULL). Idempotent.")
    for r in RULES:
        print(f"\n-- §{r['ref']}  {r['rule_name']}")
        print("INSERT INTO generic_rule_specification")
        print("       (rule_name, severity, class_name, validation_logic, "
              "is_generic, tenant_id, is_active)")
        print("SELECT {n}, {s}, {c}, {l}, TRUE, NULL, TRUE".format(
            n=_sql_literal(r["rule_name"]), s=_sql_literal(r["severity"]),
            c=_sql_literal(r["class_name"]),
            l=_sql_literal(r["validation_logic"])))
        print("WHERE NOT EXISTS (")
        print("    SELECT 1 FROM generic_rule_specification")
        print(f"    WHERE rule_name = {_sql_literal(r['rule_name'])} "
              "AND tenant_id IS NULL")
        print(");")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the rows (default is a dry run)")
    ap.add_argument("--sql", action="store_true",
                    help="print the INSERT statements and exit (for QA/prod)")
    args = ap.parse_args()

    if args.sql:
        emit_sql()
        return 0

    from sqlalchemy.sql import text
    from db import canonical_engine
    from contract_upload_services.generic_rule_library import is_supported_class

    # A class the generator does not recognise produces NOTHING — the rule would
    # be stored, look correct in the catalogue, and never generate a check.
    for r in RULES:
        if not is_supported_class(r["class_name"]):
            print(f"ABORT: class {r['class_name']!r} (§{r['ref']}) is not in "
                  f"_INTENT_BY_CLASS — that rule would generate nothing.")
            return 1

    inserted = skipped = 0
    with canonical_engine.begin() as conn:
        for r in RULES:
            existing = conn.execute(
                text("SELECT id, severity, class_name FROM "
                     "generic_rule_specification "
                     "WHERE rule_name = :n AND tenant_id IS NULL"),
                {"n": r["rule_name"]},
            ).mappings().first()

            if existing:
                print(f"§{r['ref']}  [{existing['id']}] already seeded "
                      f"({existing['severity']} / {existing['class_name']}) "
                      f"— nothing to do.")
                skipped += 1
                continue

            print(f"§{r['ref']}  WILL INSERT (global, tenant_id IS NULL)")
            print(f"        name:     {r['rule_name']}")
            print(f"        severity: {r['severity']}")
            print(f"        class:    {r['class_name']}")

            if args.apply:
                new_id = conn.execute(
                    text("INSERT INTO generic_rule_specification "
                         "       (rule_name, severity, class_name, "
                         "        validation_logic, is_generic, tenant_id, "
                         "        is_active) "
                         "VALUES (:n, :s, :c, :l, TRUE, NULL, TRUE) "
                         "RETURNING id"),
                    {"n": r["rule_name"], "s": r["severity"],
                     "c": r["class_name"], "l": r["validation_logic"]},
                ).scalar()
                print(f"        inserted as id {new_id}.")
                inserted += 1

    if args.apply:
        print(f"\nInserted {inserted}, already present {skipped}.")
        print("REMINDER: rule generation runs at CONTRACT UPLOAD. Re-run it for "
              "the program (or re-upload the contract) before these bind.")
    else:
        print("\nDry run — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
