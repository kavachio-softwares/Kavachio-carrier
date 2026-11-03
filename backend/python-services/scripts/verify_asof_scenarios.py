"""
verify_asof_scenarios.py
────────────────────────
Runs every §7 (Prior Period Files) scenario against a throwaway in-memory
database and prints expected-vs-actual for each transaction date.

WHY THIS EXISTS. Testing as-of resolution through the UI takes an hour per
scenario: create a programme, upload two contracts, build a setup, process a
bordereau, read the exception list. That is worth doing ONCE, end to end, to
prove the wiring. It is not worth doing eight times to prove the date
arithmetic — and because it is slow, it does not get repeated, so a regression
in the arithmetic goes unnoticed until a real bordereau is wrong.

This script is the fast half of that pair. It exercises the same function the
application calls (contract_asof.resolve_as_of) on the same shapes of data, so
a green run here means the LOGIC is right and any UI failure is a WIRING
problem — which narrows debugging enormously.

    python -m scripts.verify_asof_scenarios

Exit code is 0 when every case matches, 1 otherwise, so it can go in CI.
"""
from __future__ import annotations

import sys
from datetime import date

from sqlalchemy import create_engine, text

from contract_upload_services import contract_asof as A

# Each scenario: the contract versions that exist, then the transaction dates to
# resolve and the version each MUST land on. `None` means "no approved version
# covers this date" — the row belongs in exceptions, and falling back to the
# current contract is precisely the §7 failure being guarded against.
#
# version tuple: (id, effective_from, effective_to, status_ops, limit)
SCENARIOS = [
    ("S1  Renewal — clean sequential versions", [
        (1, "2025-01-01", "2026-01-01", "active", "$1M"),
        (2, "2026-01-01", "2027-01-01", "active", "$3M"),
    ], [
        ("2025-03-01", 1, "inside the 2025 term"),
        ("2025-12-31", 1, "last day of 2025 term"),
        ("2026-01-01", 2, "BOUNDARY — belongs to the new version"),
        ("2026-06-01", 2, "inside the 2026 term"),
    ]),

    ("S2  Mid-term endorsement", [
        (1, "2025-01-01", "2026-01-01", "active", "$1M"),
        (2, "2025-07-01", "2026-01-01", "active", "$2M"),
    ], [
        ("2025-03-01", 1, "before the endorsement"),
        ("2025-06-30", 1, "day before the endorsement"),
        ("2025-07-01", 2, "BOUNDARY — endorsement takes over"),
        ("2025-08-15", 2, "after the endorsement"),
    ]),

    ("S3  Gap between versions — no cover", [
        (1, "2024-01-01", "2025-01-01", "active", "$500k"),
        (2, "2026-01-01", "2027-01-01", "active", "$3M"),
    ], [
        ("2023-06-01", None, "before inception — EXCEPTION"),
        ("2024-06-01", 1, "inside the 2024 term"),
        ("2025-06-01", None, "in the gap — EXCEPTION"),
        ("2026-06-01", 2, "inside the 2026 term"),
    ]),

    ("S4  Newer version exists but is only a DRAFT", [
        (1, "2025-01-01", "2026-01-01", "active", "$1M"),
        (2, "2026-01-01", "2027-01-01", "drafted", "$3M"),
    ], [
        ("2025-06-01", 1, "approved version governs"),
        ("2026-06-01", None, "only a draft covers it — EXCEPTION"),
    ]),

    ("S5  Backdated endorsement uploaded late", [
        (1, "2025-01-01", "2026-04-01", "active", "$1M"),
        (2, "2026-04-01", "2027-04-01", "active", "$3M"),
    ], [
        ("2026-03-31", 1, "before the backdated effective date"),
        ("2026-05-15", 2, "AFTER it — even though v1 was current that day"),
    ]),

    ("S6  Overlap — duplicate upload, identical windows", [
        (1, "2025-01-01", "2026-01-01", "active", "$1M"),
        (2, "2025-01-01", "2026-01-01", "active", "$3M"),
    ], [
        ("2025-06-01", 2, "tie broken by newest id — logs a WARNING"),
    ]),

    ("S7  Expired programme — nothing in force today", [
        (1, "2023-01-01", "2024-01-01", "active", "$1M"),
    ], [
        ("2023-06-01", 1, "late file for a closed term still resolves"),
        ("2025-06-01", None, "after expiry — EXCEPTION, no fallback"),
    ]),
]

LINEAGE = A.Lineage(99, "Schedule A", 7)

DDL = """
CREATE TABLE contract(
  contract_id INT, contract_program_id INT, schedule_key TEXT,
  contract_broker_party_id INT, filename TEXT, status_ops TEXT,
  is_current_version BOOL, contract_effective_from DATE,
  contract_effective_to DATE, contract_version_label TEXT)
"""


def run() -> int:
    failures = 0
    for title, versions, cases in SCENARIOS:
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text(DDL))
            for cid, frm, to, status, limit in versions:
                conn.execute(
                    text("INSERT INTO contract VALUES (:i,:p,:s,:b,:f,:st,1,:fr,:to,:l)"),
                    {"i": cid, "p": LINEAGE.program_id, "s": LINEAGE.schedule_key,
                     "b": LINEAGE.broker_party_id, "f": f"v{cid}.pdf", "st": status,
                     "fr": frm, "to": to, "l": f"v{cid}"},
                )

            print(f"\n{title}")
            for cid, frm, to, status, limit in versions:
                print(f"    v{cid}: {frm} -> {to}  {status:<8} limit {limit}")

            for d, expected, why in cases:
                got = A.resolve_as_of(conn, LINEAGE, date.fromisoformat(d))
                ok = got == expected
                failures += (not ok)
                mark = "ok  " if ok else "FAIL"
                shown = f"v{got}" if got else "exception"
                want = f"v{expected}" if expected else "exception"
                tail = "" if ok else f"   << expected {want}"
                print(f"    [{mark}] {d}  -> {shown:<9} {why}{tail}")

    total = sum(len(c) for _, _, c in SCENARIOS)
    print(f"\n{'─'*66}\n{total - failures}/{total} cases passed")
    if failures:
        print("FAILED — as-of resolution does not match the specification above.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
