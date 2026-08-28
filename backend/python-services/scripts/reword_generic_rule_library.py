"""Reword the generic rule library so it reads as insurance, not as a warehouse.

WHY
───
`generic_rule_specification.validation_logic` is shown to the person triaging
exceptions — it is what appeared under the "Contract Clause" heading on the
exception screens. Thirteen of the 47 global rows were written against the source
data warehouse and leaked its column names at the underwriter:

    Transaction effective date must be present, must not fall after pol_exp_dt
    or tran_exp_dt, and must sit between 1980 and today.

    palms_gross_prem_amt × ceded_pct must equal ceded_prem_amt

`pol_exp_dt` is not a column in anyone's bordereau and `palms_` is one MGA's
brand sitting in a GLOBAL, every-tenant rule.

WHAT MAKES THIS DELICATE
────────────────────────
`validation_logic` is NOT a label — it is a PROMPT. generic_rule_library
._build_intents feeds it to the Gemini Call-3 mapper twice (as the synthetic
clause `text` and as the intent's `rule_description`), and for most classes
_INTENT_BY_CLASS carries value=None, so this prose is the ONLY source of the
template's parameters: the equation for a cross_field_math rule, the compared
date for a date_relation, the digit count for a pattern, the 1980..today bounds.
Deleting that structure would not "simplify" a rule, it would silently kill it —
the rule would route to the review queue rather than fail loudly.

So every rewrite below is a RENAMING, never a deletion:
  * a source-system token becomes the business concept it denotes
    ("pol_exp_dt" → "the policy expiration date"). This is not merely safe, it
    should help: the mapper binds columns by MEANING against the program's own
    output-template field names, and a snake_case token matches nothing there.
  * every equation, comparison, bound and enumeration is preserved verbatim in
    meaning ("A × B must equal C", "not earlier than 1980", "greater than 0").

One row is also corrected rather than reworded. Row 9 is named "Program
Administrator Must Not Be Null, Empty, or 'Unknown'", but its compiled SQL
cannot flag a blank: the excluded set is built by _enum_match_rows, which drops
the "" literal because canonical_token("") is falsy, and the enum builder skips
blank cells before scoring. Verified against DuckDB — of a blank row, a NULL row
and an 'Unknown' row, it flags only 'Unknown'. The NULL case is covered by row 15
on the same column. The name is corrected to say what the rule does; its excluded
list lives in code (_INTENT_BY_CLASS) and is untouched by this script.

WHAT IT TOUCHES
───────────────
Only `tenant_id IS NULL` rows (the platform's own), and only the ids listed in
REWORDS. Row 43 belongs to a customer and is never touched — the rule-library API
deliberately 404s a platform admin who edits a tenant's row. Nothing else in the
table changes, and no validation_rule row is rewritten: existing rules froze
their wording at generation time, and the exception screens now humanize any
surviving snake_case at read time (rule_explainer.humanize_columns).

TAKING EFFECT
─────────────
The table is read only at rule-GENERATION time, so a program picks the new
wording up when its contract is re-uploaded. Note that POST /programs/{id}/setup
short-circuits on an identical content fingerprint (which does not cover this
table), so a plain setup re-run will NOT pick it up — re-upload the contract or
set KAVACHIO_DISABLE_CONTRACT_REUSE=1.

USAGE
─────
    python scripts/reword_generic_rule_library.py            # dry run (default)
    python scripts/reword_generic_rule_library.py --apply
    python scripts/reword_generic_rule_library.py --sql      # emit SQL for QA/prod
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# id -> {"rule_name": <new name or None to keep>, "validation_logic": <new text>}
#
# Every entry preserves the parameter-bearing structure of the original (see the
# module docstring); the diff is vocabulary only, except id 9 and id 24 which are
# corrections and are commented as such.
REWORDS: dict[int, dict] = {
    1: {
        "validation_logic":
            "The accident/loss date must be present and plausible: not empty, "
            "not earlier than 1980, and not a future date. Where no accident "
            "date is supplied, the loss date or the claim-made date is used "
            "instead.",
    },
    2: {
        "validation_logic":
            "Ceded premium must reconcile to the cession: the ceded premium "
            "amount must equal the gross written premium multiplied by the "
            "ceded percentage, checked separately for each reinsurer on the "
            "risk.",
    },
    3: {
        "validation_logic":
            "Amounts already paid on a claim cannot be greater than the total "
            "incurred: the total paid amount must be less than or equal to the "
            "total incurred amount.",
    },
    4: {
        "validation_logic":
            "Claim close date must be chronologically sound: not before the "
            "reported date and not in the future. It is mandatory whenever the "
            "claim status is 'Closed'.",
    },
    7: {
        "validation_logic":
            "When the BDX supplies a net premium, it must reconcile to the "
            "gross written premium minus the commission amount minus other "
            "fees and surcharges.",
    },
    # CORRECTION, not a rewording. The compiled check excludes the placeholder
    # values only — it cannot flag a blank (see the module docstring). Row 15
    # covers the missing-value case on the same column.
    9: {
        "rule_name": "Program Administrator Must Not Be a Placeholder Value",
        "validation_logic":
            "The Program Administrator name must be a real name, not a "
            "placeholder such as 'Unknown', 'N/A' or 'None'. A completely "
            "missing name is covered by the Program Administrator Name Must "
            "Not Be Null rule.",
    },
    10: {
        "validation_logic":
            "Every claim record must reference a contract; a missing contract "
            "identifier breaks the link between the claim and the underlying "
            "insurance contract.",
    },
    13: {
        "validation_logic":
            "Every policy must resolve to a contract; the match is made on the "
            "policy effective date, so a policy whose effective date falls "
            "outside all contract periods fails.",
    },
    # DE-BRANDING. This is a global rule that applies to every tenant, so it
    # cannot be named after one MGA. Row 46 was already reworded to "own
    # occurrence limit" and maps correctly; this follows that convention.
    24: {
        "rule_name": "Own Limit Must Not Be Null",
        "validation_logic":
            "The MGA's own occurrence limit must be populated.",
    },
    25: {
        "validation_logic":
            "A claim marked Closed must carry no unpaid reserve: once the "
            "claim is closed, the total incurred amount must not exceed the "
            "total paid amount.",
    },
    30: {
        "validation_logic":
            "A policy's effective date must stay identical across all of its "
            "transactions; a shifting policy effective date indicates a "
            "mis-keyed or re-issued policy.",
    },
    31: {
        "validation_logic":
            "Both policy dates must be present, the policy expiration date "
            "must be later than the policy effective date, and the resulting "
            "term in months must fall within the bounds the contract allows.",
    },
    32: {
        "validation_logic":
            "Policy type and attachment point must agree: a policy type of "
            "'Primary' requires the excess-of attachment point to be 0 or "
            "empty, while a policy type of 'Excess' requires the excess-of "
            "attachment point to be present and greater than 0.",
    },
    39: {
        "validation_logic":
            "Transaction effective date must be present, must not fall after "
            "the policy expiration date or the transaction expiration date, "
            "and must sit between 1980 and today.",
    },
}


def _sql_literal(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def emit_sql() -> None:
    """Print the UPDATEs so QA/prod can be done by whoever owns those DBs."""
    print("-- Reword the platform's generic rule library (tenant_id IS NULL only).")
    print("BEGIN;")
    for rid, new in sorted(REWORDS.items()):
        sets = [f"validation_logic = {_sql_literal(new['validation_logic'])}"]
        if new.get("rule_name"):
            sets.append(f"rule_name = {_sql_literal(new['rule_name'])}")
        print(f"UPDATE generic_rule_specification SET {', '.join(sets)} "
              f"WHERE id = {rid} AND tenant_id IS NULL;")
    print("COMMIT;")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    ap.add_argument("--sql", action="store_true",
                    help="print the UPDATE statements and exit (for QA/prod)")
    args = ap.parse_args()

    if args.sql:
        emit_sql()
        return 0

    from sqlalchemy.sql import text
    from db import canonical_engine

    changed = skipped = missing = 0
    with canonical_engine.begin() as conn:
        for rid, new in sorted(REWORDS.items()):
            row = conn.execute(
                text("SELECT id, rule_name, validation_logic, tenant_id "
                     "FROM generic_rule_specification WHERE id = :id"),
                {"id": rid},
            ).mappings().first()
            if not row:
                print(f"[{rid}] MISSING — no such rule; skipped.")
                missing += 1
                continue
            if row["tenant_id"] is not None:
                # A customer's own rule. Never rewritten — see the docstring.
                print(f"[{rid}] TENANT-OWNED (tenant_id={row['tenant_id']}) — skipped.")
                skipped += 1
                continue

            name_new = new.get("rule_name") or row["rule_name"]
            logic_new = new["validation_logic"]
            if row["rule_name"] == name_new and row["validation_logic"] == logic_new:
                print(f"[{rid}] already reworded — skipped.")
                skipped += 1
                continue

            print(f"\n[{rid}] {row['rule_name']}")
            if name_new != row["rule_name"]:
                print(f"   name was: {row['rule_name']}")
                print(f"   name now: {name_new}")
            print(f"   was: {row['validation_logic']}")
            print(f"   now: {logic_new}")
            changed += 1

            if args.apply:
                conn.execute(
                    text("UPDATE generic_rule_specification "
                         "SET rule_name = :n, validation_logic = :l "
                         "WHERE id = :id AND tenant_id IS NULL"),
                    {"n": name_new, "l": logic_new, "id": rid},
                )

    print(f"\n{changed} rule(s) reworded, {skipped} skipped, {missing} missing.")
    if not args.apply:
        print("Dry run — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
