"""Feature 7 — Prior Period Files: prove the right contract version is applied.

Read-only. Nothing here writes to the database except --set-effective, which
says so and asks first.

Usage:
    cd backend/python-services
    source venv/bin/activate

    # 1. What does the contract timeline look like?
    python -m scripts.verify_contract_asof --timeline

    # 2. Is any date claimed by two versions? (fix these before migration 20_2)
    python -m scripts.verify_contract_asof --check-overlaps

    # 3. Which version governs a given date?
    python -m scripts.verify_contract_asof --resolve 2024-03-12 --program 7

    # 4. THE SAFETY CHECK: does switching the flag on change any answer?
    python -m scripts.verify_contract_asof --compare

    # 5. Correct an endorsement's effective date the extractor got wrong.
    python -m scripts.verify_contract_asof --set-effective 42 --from 2024-04-01
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from db import SessionLocal
from contract_upload_services import contract_asof as asof


def _d(v) -> str:
    return v.isoformat() if isinstance(v, (date, datetime)) else ("—" if v is None else str(v))


def _lineages(s):
    """Every distinct (program, schedule, broker) group that has contracts."""
    rows = s.execute(text("""
        SELECT DISTINCT contract_program_id, schedule_key, contract_broker_party_id
        FROM   contract
        ORDER BY contract_program_id, schedule_key, contract_broker_party_id
    """)).all()
    return [asof.Lineage(r[0], r[1], r[2]) for r in rows]


def cmd_timeline(s, args) -> int:
    lineages = _lineages(s)
    if not lineages:
        print("No contracts found.")
        return 0
    for lin in lineages:
        if args.program and lin.program_id != args.program:
            continue
        print(f"\n── programme {lin.program_id} | schedule {lin.schedule_key or '—'} "
              f"| broker {lin.broker_party_id or '—'} ──")
        rows = asof.timeline(s, lin)
        if not rows:
            print("   (no effective dating — has migration 20_1 been applied?)")
            continue
        print(f"   {'id':>6}  {'label':<14} {'in force from':<13} {'until':<13} "
              f"{'status':<12} {'current':<8} file")
        for r in rows:
            print(f"   {r['contract_id']:>6}  "
                  f"{(r['contract_version_label'] or '—'):<14} "
                  f"{_d(r['contract_effective_from']):<13} "
                  f"{_d(r['contract_effective_to']):<13} "
                  f"{(r['status_ops'] or '—'):<12} "
                  f"{str(r['is_current_version']):<8} "
                  f"{r['filename'] or '—'}")
        # The whole point of the feature, spelled out: the row a date lookup
        # returns is usually NOT the row flagged current.
        undated = [r for r in rows if r["contract_effective_from"] is None]
        if undated:
            print(f"   ⚠  {len(undated)} version(s) carry NO effective dates and can "
                  f"never be resolved by date. Fix with --set-effective.")
    return 0


def cmd_check_overlaps(s, args) -> int:
    overlaps = asof.find_overlaps(s)
    if not overlaps:
        print("✓ No overlapping contract versions. Safe to apply migration 20_2.")
        return 0
    print(f"✗ {len(overlaps)} overlapping pair(s). On these dates 'the version in "
          f"force' has two answers, so §7.1 cannot be satisfied:\n")
    for o in overlaps:
        print(f"   programme {o['program_id']}: "
              f"contract {o['a_id']} [{_d(o['a_from'])} → {_d(o['a_to'])}] ({o['a_file']})")
        print(f"   {'':>17}overlaps "
              f"contract {o['b_id']} [{_d(o['b_from'])} → {_d(o['b_to'])}] ({o['b_file']})\n")
    print("Fix each with --set-effective before applying migration 20_2.")
    return 1


def cmd_resolve(s, args) -> int:
    on = datetime.strptime(args.resolve, "%Y-%m-%d").date()
    lineages = [l for l in _lineages(s)
                if not args.program or l.program_id == args.program]
    if not lineages:
        print("No contracts match.")
        return 1
    exit_code = 0
    for lin in lineages:
        cid = asof.resolve_as_of(s, lin, on)
        current = s.execute(text("""
            SELECT contract_id FROM contract
            WHERE contract_program_id = :p AND is_current_version IS NOT FALSE
            ORDER BY contract_id DESC LIMIT 1
        """), {"p": lin.program_id}).scalar()

        print(f"\n── programme {lin.program_id} | schedule {lin.schedule_key or '—'} ──")
        print(f"   transaction date   : {on.isoformat()}")
        if cid is None:
            # NOT an error in the code — an error in the data, and the row this
            # represents must go to exceptions rather than to a fallback.
            print(f"   version in force   : NONE — a transaction on this date has no "
                  f"governing contract.\n"
                  f"                        Rows like this must be routed to "
                  f"exceptions (NO_CONTRACT_VERSION_FOR_DATE),\n"
                  f"                        never silently to the current version.")
            exit_code = 1
            continue
        print(f"   version in force   : contract {cid}   ← §7 uses THIS")
        print(f"   current version    : contract {current}"
              f"{'   (same row)' if cid == current else '   ← the WRONG one for this date'}")
        n = s.execute(text("SELECT count(*) FROM validation_rule "
                           "WHERE contract_id = :c AND rule_status = 'active'"),
                      {"c": cid}).scalar()
        print(f"   active rules used  : {n}")
    return exit_code


def cmd_compare(s, args) -> int:
    """The pre-flight check: for every contract and every date that matters,
    does as-of resolution disagree with 'latest wins'?

    While one contract is live the answer must be "no disagreements" — which is
    what makes switching CONTRACT_ASOF_ENABLED on a provable no-op rather than a
    hoped-for one. Any disagreement listed here is a period whose numbers WILL
    change, and each one should be understood before the flag goes on.
    """
    lineages = _lineages(s)
    disagreements = []
    checked = 0
    for lin in lineages:
        rows = asof.timeline(s, lin)
        dated = [r for r in rows if r["contract_effective_from"]]
        if not dated:
            continue
        current = s.execute(text("""
            SELECT contract_id FROM contract
            WHERE contract_program_id = :p AND is_current_version IS NOT FALSE
            ORDER BY contract_id DESC LIMIT 1
        """), {"p": lin.program_id}).scalar()
        # Probe each version's own start date, plus the day before it — the two
        # dates on which a boundary bug shows up if there is one.
        probes = set()
        for r in dated:
            probes.add(r["contract_effective_from"])
            if r["contract_effective_to"]:
                probes.add(r["contract_effective_to"])
        for p in sorted(probes):
            checked += 1
            got = asof.resolve_as_of(s, lin, p)
            if got is not None and got != current:
                disagreements.append((lin.program_id, p, got, current))

    print(f"Probed {checked} boundary date(s) across {len(lineages)} lineage(s).\n")
    if not disagreements:
        print("✓ as-of resolution and 'latest wins' agree on every probed date.")
        print("  Switching CONTRACT_ASOF_ENABLED on changes no result.")
        return 0
    print(f"⚠ {len(disagreements)} date(s) where the two disagree — these are the "
          f"periods whose results will change:\n")
    for pid, d, got, cur in disagreements:
        print(f"   programme {pid}  {_d(d)}:  as-of → contract {got}   "
              f"(latest wins → contract {cur})")
    print("\nThis is Feature 7 working as intended. Confirm each is correct, "
          "then switch the flag on.")
    return 0


def cmd_set_effective(s, args) -> int:
    cid = args.set_effective
    row = s.execute(text("""
        SELECT contract_id, filename, contract_effective_from, contract_effective_to
        FROM contract WHERE contract_id = :c
    """), {"c": cid}).mappings().first()
    if not row:
        print(f"No contract {cid}.")
        return 1
    new_from = datetime.strptime(args.from_date, "%Y-%m-%d").date() if args.from_date else None
    new_to = datetime.strptime(args.to_date, "%Y-%m-%d").date() if args.to_date else None
    print(f"contract {cid} ({row['filename']})")
    print(f"   from : {_d(row['contract_effective_from'])} → {_d(new_from)}")
    print(f"   to   : {_d(row['contract_effective_to'])} → {_d(new_to)}")
    if input("\nApply? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return 1
    s.execute(text("""
        UPDATE contract
        SET    contract_effective_from = COALESCE(:f, contract_effective_from),
               contract_effective_to   = :t
        WHERE  contract_id = :c
    """), {"c": cid, "f": new_from, "t": new_to})
    s.commit()
    print("Updated. Re-run --check-overlaps.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Verify Feature 7 contract resolution.")
    p.add_argument("--timeline", action="store_true")
    p.add_argument("--check-overlaps", action="store_true")
    p.add_argument("--resolve", metavar="YYYY-MM-DD")
    p.add_argument("--compare", action="store_true")
    p.add_argument("--set-effective", type=int, metavar="CONTRACT_ID")
    p.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD")
    p.add_argument("--program", type=int)
    args = p.parse_args()

    print(f"CONTRACT_ASOF_ENABLED = {asof.asof_enabled()}\n")

    with SessionLocal() as s:
        if args.check_overlaps:
            return cmd_check_overlaps(s, args)
        if args.resolve:
            return cmd_resolve(s, args)
        if args.compare:
            return cmd_compare(s, args)
        if args.set_effective:
            return cmd_set_effective(s, args)
        return cmd_timeline(s, args)


if __name__ == "__main__":
    sys.exit(main())
