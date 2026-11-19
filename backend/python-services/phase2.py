"""The Phase 2 switch — one setting that shows or hides every Phase 2 feature.

PHASE2_ENABLED (1/true/on/yes). Default OFF: with the key missing, Phase 2 is
hidden. Phase 2 is the "Phase 2" tab of Kavachio-Carrier-Plan-(3).xlsx
(tasks 23–54): Previous Submission comparison, reinsurance treaties and
reinsurance BDX, Financial Summary, Partner Performance, the Executive and
Operational reports, late-file cases, Policy Explorer, Open Problems and
programme rules. Audit Logs are NOT behind it.

Off means:
  * every Phase 2 API route answers 404, as if it did not exist
    (`require_phase2`, attached where the routes are registered);
  * the scheduled Operational / Executive report emails are not sent;
  * the screens hide their menus, links, tabs and cards — the frontend reads
    the setting from GET /features, so this one key drives both halves.

Nothing is deleted when it is off: data already written stays, and the
background records Phase 2 keeps (the check record, version comparisons)
carry on being written, so switching it back on loses no history.
"""
import os

from fastapi import HTTPException


def phase2_enabled() -> bool:
    """PHASE2_ENABLED (1/true/on/yes). Default OFF."""
    return os.getenv("PHASE2_ENABLED", "").strip().lower() in (
        "1", "true", "on", "yes")


def require_phase2() -> None:
    """Route dependency: 404 while Phase 2 is switched off."""
    if not phase2_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def features() -> dict:
    """What the screens may show — served to the frontend at GET /features."""
    return {"phase2": phase2_enabled()}
