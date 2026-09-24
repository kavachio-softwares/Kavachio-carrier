"""
broker_tally.py
───────────────
How one generated file is counted for the carrier dashboard's Broker
Performance card. Its own module, with no imports, so the counting can be
tested without standing up a database.
"""
_RESOLVED_KINDS = {"approved", "fixed", "dismissed", "rejected"}


def _exception_settled(e: dict) -> bool:
    """Has this exception been decided? Exactly the Exception Triage screen's
    test: an explicit decision, or a bare "resolved" whose note names one."""
    st = str(e.get("status") or "").lower()
    note = str(e.get("resolution_note") or "").strip().lower()
    return st in _RESOLVED_KINDS or (
        st == "resolved"
        and note.startswith(("fixed", "approved", "dismissed", "rejected")))


def settled(e: dict) -> bool:
    """Public name for the decided test — the carrier card, the broker card and
    the per-file lists all have to apply the SAME one (see exception_tally)."""
    return _exception_settled(e)


def countable(exceptions: list) -> list:
    """The exceptions that are work: notices and unchecked rules left out."""
    return [e for e in (exceptions or [])
            if isinstance(e, dict)
            and e.get("error_class") != "not_checked"
            and not (e.get("error_class") == "not_validated"
                     and e.get("rule_id") is None)]


def broker_latest_tally(exceptions: list) -> dict:
    """Count the issues on one generated file: `issues` = `resolved` + `open`.

    Counted exactly as that file's Exception Triage screen counts them —
    notices and unchecked rules left out, the same decision test — so the card
    and the screen it opens never disagree. The card's bar is these two numbers
    as shares of the file's own issues, which is why nothing here is a share of
    the file's ROWS: an issue is raised per field, so several can land on one
    row and row shares would tell a different story.
    """
    excs = countable(exceptions)
    done = sum(1 for e in excs if _exception_settled(e))
    return {"issues": len(excs), "resolved": done, "open": len(excs) - done}
