"""One-off backfill: mark a Program 'active' if it has any active Contract.

Existing programs whose contracts were activated before the app started
promoting the parent Program still show status 'draft'. This aligns them.

Run inside the python-services container:
    python backfill_program_status.py          # apply
    python backfill_program_status.py --dry-run # preview only
"""
import sys

from db import SessionLocal, Program, Contract


def main(dry_run: bool) -> None:
    with SessionLocal() as s:
        active_program_ids = {
            pid for (pid,) in s.query(Contract.program_id)
            .filter(Contract.status == "active")
            .distinct()
            .all()
            if pid is not None
        }

        to_update = (
            s.query(Program)
            .filter(Program.id.in_(active_program_ids))
            .filter(Program.status != "active")
            .all()
        )

        for p in to_update:
            print(f"  program {p.id} ({p.name!r}): {p.status!r} -> 'active'")
            if not dry_run:
                p.status = "active"

        if dry_run:
            print(f"[dry-run] {len(to_update)} program(s) would be updated.")
        else:
            s.commit()
            print(f"Updated {len(to_update)} program(s).")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
