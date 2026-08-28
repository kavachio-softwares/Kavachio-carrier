"""Add the ROW-ALIGNED sample rows to output templates that were parsed before
they were captured.

WHY
───
A formula rule is bound to a column by name and meaning, and a bordereau reports
one amount under several headings that agree for its opening rows ("Gross Written
Premium (Including TRIA)" and "… (Less TRIA)" are the same figure on every policy
that did not buy terrorism cover). The verify gate settles that choice against the
template's own sample rows — but only rows that are ALIGNED across columns, and
only enough of them to reach the row where the look-alikes part company, can
settle anything. exporter now captures exactly those rows as `row_samples`; a
template parsed before that has none, so the check has nothing to judge on and the
formula keeps whichever column was named first.

Each template keeps the sample workbook it was parsed from (`template_blob`), so
this re-parses that workbook and merges the aligned rows into the stored
structure. STRICTLY ADDITIVE: only a `row_samples` key is written, and only on
columns that have none — samples, canonical fields, formulas, sheet roles and
every other part of the structure are left byte-for-byte as they are. Rules are
not touched; regenerate a contract's rules afterwards for its formulas to be
re-decided.

Usage:
    cd backend/python-services
    python -m scripts.backfill_template_row_samples                  # dry run
    python -m scripts.backfill_template_row_samples --apply
    python -m scripts.backfill_template_row_samples --apply --id 701
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text                                          # noqa: E402

import exporter                                                      # noqa: E402
from db import canonical_engine                                      # noqa: E402


def _row_samples_by_column(blob, filename):
    """{(sheet, column) -> [row-aligned values]} from the sample workbook."""
    parsed = exporter.parse_template(blob, filename)
    out = {}
    for sheet in parsed.get("sheets", []):
        for col in sheet.get("columns", []):
            rs = col.get("row_samples")
            if rs:
                out[(sheet.get("sheet_name"), col.get("column_name"))] = rs
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the structures back (default: report only)")
    ap.add_argument("--id", type=int, action="append",
                    help="only this template id (repeatable)")
    args = ap.parse_args()

    with canonical_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, name, structure, template_blob
            FROM export_templates
            ORDER BY id
        """)).mappings().all()

    touched = skipped = blank = 0
    for r in rows:
        if args.id and r["id"] not in args.id:
            continue
        structure = r["structure"]
        if isinstance(structure, str):
            structure = json.loads(structure)
        if not isinstance(structure, dict) or not structure.get("sheets"):
            continue
        cols = [c for s in structure["sheets"] for c in (s.get("columns") or [])]
        if not cols:
            continue
        if all(c.get("row_samples") for c in cols):
            skipped += 1
            continue
        if not r["template_blob"]:
            blank += 1
            print(f"  [{r['id']}] {r['name'][:60]!r}: no sample workbook stored — "
                  f"re-upload it in BDX setup to capture the aligned rows")
            continue

        try:
            fresh = _row_samples_by_column(bytes(r["template_blob"]), r["name"])
        except Exception as exc:
            print(f"  [{r['id']}] {r['name'][:60]!r}: could not re-parse ({exc})")
            continue

        added = 0
        for sheet in structure["sheets"]:
            for col in (sheet.get("columns") or []):
                if col.get("row_samples"):
                    continue
                rs = fresh.get((sheet.get("sheet_name"), col.get("column_name")))
                if rs:
                    col["row_samples"] = rs
                    added += 1
        if not added:
            print(f"  [{r['id']}] {r['name'][:60]!r}: nothing matched (the stored "
                  f"structure and the stored workbook disagree) — left alone")
            continue

        depth = max(len(v) for v in fresh.values())
        print(f"  [{r['id']}] {r['name'][:60]!r}: +{added} column(s), "
              f"{depth} aligned row(s){'' if args.apply else '  (dry run)'}")
        touched += 1
        if args.apply:
            with canonical_engine.begin() as conn:
                conn.execute(
                    text("UPDATE export_templates SET structure = CAST(:s AS json) "
                         "WHERE id = :i"),
                    {"s": json.dumps(structure), "i": r["id"]})

    print(f"\n{touched} template(s) {'updated' if args.apply else 'would be updated'}"
          f"; {skipped} already had them; {blank} have no stored workbook.")
    if touched and not args.apply:
        print("Re-run with --apply to write them.")


if __name__ == "__main__":
    main()
