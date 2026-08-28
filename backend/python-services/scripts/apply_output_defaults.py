"""Add the Appendix 2 §2.7 / §2.8 output defaults to a direct_format's column_mapping.

WHY
───
§2.8 is explicit that the default belongs in the DELIVERED FILE:

    COALESCE(rt.prs_transaction_short_types, 'UNK')
    "Null transaction codes are not permitted in output. 'UNK' is the required
     default."

and §2.7 is the same shape for the insured name, currency, amount and FX rate.

The warehouse side already applies these at ingest (ingester._child_defaults).
The output side did not: every rule in a direct_format's column_mapping is
`{"kind": "copy"}`, so a blank cell in was a blank cell out — the delivered
bordereau and the sub-ledger disagreed about the same row, with nothing to
signal it.

This script adds the optional `default` key to the copy rules for the columns
those two sections name. direct_lane.eval_rule applies it only when the source
cell is blank (None / NaN / '' / whitespace-only — the NULLIF(TRIM(x),'') half).

WHY NOT A NEW RULE KIND
───────────────────────
`default` rides on the EXISTING copy rule. A rule without it behaves exactly as
before, and direct_lane.resolve_landing_cell — which powers the "Fix" write-back
from an exception to the input cell — dispatches on `kind == "copy"`. A new kind
would have made every defaulted column uneditable from the exception screen.

THE DEFAULT DOES NOT HIDE THE PROBLEM
─────────────────────────────────────
Library rules 50 ("Policy Number Must Not Be a Placeholder Value", not_in_set
includes 'Unknown') and 53 ("Transaction Type Must Be a Recognised Value",
'unknown'/'UNK' are not in the accepted set) flag the DEFAULTED VALUES. The
broker gets a filled cell AND a Critical exception saying we filled it — which
is strictly more information than a blank cell, because a blank is ambiguous
between "the carrier omitted it" and "we dropped it".

COLUMN SELECTION
────────────────
Matched by output column name, case-insensitively, against the patterns below.
Nothing is guessed: a column that matches no pattern is left untouched, and the
dry run prints exactly which columns would change before anything is written.

USAGE
─────
    python -m scripts.apply_output_defaults --format 428           # dry run
    python -m scripts.apply_output_defaults --format 428 --apply
    python -m scripts.apply_output_defaults --format 428 --sql
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_SVC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SVC_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_SVC_DIR, ".env"), override=False)
except ImportError:
    pass

# The column → default table lives in bdx_defaults, NOT here. direct_mapper
# consults the same function when it proposes a mapping, so a format created
# today is born with these defaults and this script only backfills the ones
# created earlier. Two copies of the table would drift, and a drift between the
# proposer and the backfill is invisible: both succeed and disagree.
from bdx_defaults import default_for_output_column as _default_for  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--format", type=int, required=True,
                    help="direct_format.id to update")
    ap.add_argument("--apply", action="store_true",
                    help="write the change (default is a dry run)")
    ap.add_argument("--sql", action="store_true",
                    help="print the resulting column_mapping as SQL and exit")
    args = ap.parse_args()

    from sqlalchemy.sql import text
    from db import engine

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, name, tenant_id, column_mapping FROM direct_format "
                 "WHERE id = :i"), {"i": args.format},
        ).mappings().first()

    if not row:
        print(f"direct_format {args.format} not found.")
        return 1

    cm = row["column_mapping"]
    if isinstance(cm, str):
        cm = json.loads(cm)
    cm = cm or {}

    print(f"direct_format {row['id']}  {row['name']!r}  tenant={row['tenant_id']}")
    print(f"output sheets: {list(cm.keys())}\n")

    changed = unchanged = skipped_kind = 0
    new_cm: dict = {}
    for sheet, rules in cm.items():
        new_rules = {}
        for out_col, rule in (rules or {}).items():
            if not isinstance(rule, dict):
                new_rules[out_col] = rule
                continue
            ref, value = _default_for(out_col)
            if value is None:
                new_rules[out_col] = rule
                unchanged += 1
                continue
            if rule.get("kind") != "copy":
                # const/transform produce a value themselves — a COALESCE on a
                # computed cell would mask a broken formula, not a missing input.
                print(f"  SKIP  {out_col!r}: kind={rule.get('kind')!r}, "
                      f"not a copy rule")
                new_rules[out_col] = rule
                skipped_kind += 1
                continue
            if rule.get("default") == value:
                print(f"  ok    {out_col!r}: already defaults to {value!r}")
                new_rules[out_col] = rule
                continue
            print(f"  §{ref}  {out_col!r}")
            print(f"          before: {rule}")
            merged = {**rule, "default": value}
            print(f"          after : {merged}")
            new_rules[out_col] = merged
            changed += 1
        new_cm[sheet] = new_rules

    print(f"\ncolumns changed: {changed}   untouched: {unchanged}   "
          f"skipped (not copy): {skipped_kind}")

    if args.sql:
        lit = json.dumps(new_cm).replace("'", "''")
        print("\n-- Appendix 2 §2.7/§2.8 output defaults")
        print(f"UPDATE direct_format SET column_mapping = '{lit}'::json, "
              f"modified_at = now() WHERE id = {args.format};")
        return 0

    if not changed:
        print("Nothing to do.")
        return 0

    if not args.apply:
        print("\nDry run — re-run with --apply to write.")
        return 0

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE direct_format SET column_mapping = :cm, "
                 "modified_at = now() WHERE id = :i"),
            {"cm": json.dumps(new_cm), "i": args.format},
        )
    print(f"\nWritten. {changed} column(s) now carry a §2.7/§2.8 default.")
    print("Re-run the BDX through Process BDX to see them in the output file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
