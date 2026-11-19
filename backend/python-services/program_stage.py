"""Where a program stands — Onboarding, In Setup, Active, Under Review or
Runoff — from the program and the contract that speaks for it.

One rule for every screen that shows it: Program Management
(app_routes.program_management_stats) and the Executive Report
(executive_report.py). Pure: no database.
"""
from __future__ import annotations

from typing import Any, Optional


# Stages that mean "currently writing business". Onboarding / In Setup are not
# live yet; Runoff's term has already ended. This is what the Active Programs
# card and the segment chart count — deliberately NOT `is_live_program()` on its
# own, which cannot see that a term has lapsed (see below).
WRITING_STAGES = ("Active", "Under Review")


def is_live_program(program, contract) -> bool:
    """Is this program actually writing business right now?

    NOT simply `status_ops == 'active'`. Two things measured against the dev
    database make that check wrong on real rows:

      1. The column drifts. 96 programs there carry an ACTIVE contract while
         their own status_ops still says 'draft' — the program is promoted when
         a contract is activated, but rows predating that behaviour were never
         caught up (which is exactly what backfill_program_status.py exists to
         repair, and it clearly hasn't been run everywhere). Trusting the column
         alone would drop those programs out of the KPI count, the segment chart
         AND the review count, silently understating the book.
      2. It is not case-normalised — both 'active' and 'Active' exist.

    So liveness is derived from the two signals together: the program says it is
    active, or it holds an activated contract.

    LIMIT, and why callers must not use this alone as "active": it reads
    contract.STATUS, never contract.expiry_dt, and nothing flips a contract to
    inactive when its term lapses. A program whose term ended years ago still
    answers True here. That is correct for "this row is live, not a draft" — the
    question the stage ladder asks — but wrong for "is this writing business",
    which the spec defines as active AND NOT in runoff. For that, test the
    resolved stage against WRITING_STAGES.
    """
    if (program.status or "").strip().lower() == "active":
        return True
    return contract is not None and (contract.status or "").strip().lower() == "active"


def current_contract(rows: list) -> Optional[Any]:
    """The contract that speaks for a program right now.

    A program can hold several contracts (versions, or one per schedule_key).
    An `active` one wins; otherwise the most recently created row, so a program
    still mid-setup still reports a stage instead of vanishing from the donut.
    """
    if not rows:
        return None
    active = [c for c in rows if (c.status or "").strip().lower() == "active"]
    pool = active or rows
    return max(pool, key=lambda c: (c.id or 0))


def program_stage(program, contract, today, horizon_days: int = 90) -> str:
    """Where a program stands: Onboarding, In Setup, Active, Under Review or
    Runoff. One rule for every screen that shows it (Program Management, the
    Executive Report).

    Stage ladder — derived from real columns, first match wins. Deliberately
    NOT the document's five-value vocabulary: that would need a status enum
    the DB doesn't have. These are the same lifecycle positions expressed in
    the signals we actually store."""
    is_active = is_live_program(program, contract)
    contract_status = (contract.status or "").strip().lower() if contract is not None else None
    term_end = contract.expiry_dt if contract is not None else None
    days = (term_end - today).days if term_end else None
    if not is_active:
        return "Onboarding" if contract is None else "In Setup"
    if contract is None:
        return "Onboarding"                       # program, no contract yet
    if contract_status in ("drafted", "extracting"):
        return "In Setup"
    if days is None:
        return "Active"                           # live, term unknown
    if days < 0:
        return "Runoff"                           # term ended, still reporting
    if days <= horizon_days:
        return "Under Review"                     # continuation decision due
    return "Active"
