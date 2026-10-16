"""
Change period, Phase B — correcting a contract's dates (kind "correction").

The rules only, with no database: who may correct which date (D3), what counts
as a large correction (D4), the token that ties a typed confirmation to the
preview it followed (B2), and when an undo is safe (B4). contract_routes reads
the facts from the database and asks these functions; the tests ask them
directly.

Never touched by anything here: the termination (date, reason), the lifecycle,
status_ops and the prior-period dates (contract_effective_from/_to, B3).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from typing import Optional

SWITCH = "CONTRACT_PERIOD_CHANGE_ENABLED"
LARGE_ENV = "PERIOD_CORRECTION_LARGE_DAYS"
DEFAULT_LARGE_DAYS = 31

ACTION_CORRECTED = "contract_period_corrected"
ACTION_UNDONE = "contract_period_correction_undone"
APPROVAL_CORRECTED = "period_corrected"
APPROVAL_UNDONE = "period_correction_undone"


def enabled() -> bool:
    """Off unless switched on. Read per request, so it changes without a deploy."""
    return os.environ.get(SWITCH, "").strip().lower() in ("1", "true", "yes", "on")


def large_days() -> int:
    """A correction moving either date by MORE than this many days is large."""
    try:
        n = int(os.environ.get(LARGE_ENV, "").strip())
        return n if n >= 0 else DEFAULT_LARGE_DAYS
    except ValueError:
        return DEFAULT_LARGE_DAYS


# ── D3: which dates may be corrected, by state ─────────────────────────────

# lifecycle → (inception, expiry). Drafts are edited directly, as today; a
# contract with the broker, agreed or signed goes back through its own flow.
_EDITABLE = {
    "active": (True, True),
    "expired": (True, True),
    "superseded": (False, True),
    "terminated": (False, True),
}


def allowed(lifecycle: str) -> dict:
    inc, exp = _EDITABLE.get(lifecycle or "", (False, False))
    return {"inception_dt": inc, "expiry_dt": exp}


def field_errors(lifecycle: str, old_in: Optional[dt.date], old_ex: Optional[dt.date],
                 new_in: Optional[dt.date], new_ex: Optional[dt.date],
                 terminated_date: Optional[dt.date] = None) -> dict:
    """{field: why} for a correction that D3 refuses; {} when it may go ahead
    (the overlap check and the large-correction rules come after)."""
    ok = allowed(lifecycle)
    if not (ok["inception_dt"] or ok["expiry_dt"]):
        return {"_": (f"A contract that is {lifecycle or 'in this state'} can't have its "
                      "dates corrected here." + (
                          " Edit a draft's dates under Edit terms." if lifecycle in (
                              "draft", "pending", "changes_requested") else ""))}
    errs: dict = {}
    if new_in is None:
        errs["inception_dt"] = "The start date is required."
    if new_ex is None:
        errs["expiry_dt"] = "The end date is required."
    if errs:
        return errs
    if new_in != old_in and not ok["inception_dt"]:
        errs["inception_dt"] = (f"The start date of a {lifecycle} contract can't be "
                                "corrected — only its end date.")
    if new_ex < new_in:
        errs["expiry_dt"] = "The end date is before the start date."
    elif terminated_date is not None and new_ex < terminated_date:
        errs["expiry_dt"] = (f"The end date can't be earlier than the termination date "
                             f"({_fmt(terminated_date)}). The termination stays as recorded.")
    if not errs and new_in == old_in and new_ex == old_ex:
        errs["_"] = "These are the dates it already has — there is nothing to correct."
    return errs


# ── D4: large corrections ──────────────────────────────────────────────────

def size(old_in, old_ex, new_in, new_ex) -> dict:
    def days(a, b):
        return abs((b - a).days) if a and b else 0
    return {"inception_days": days(old_in, new_in), "expiry_days": days(old_ex, new_ex)}


def large(old_in, old_ex, new_in, new_ex, limit: Optional[int] = None) -> dict:
    """{large, limit, inception_days, expiry_days, reasons}. Large means either
    date moves by MORE than the limit: 31 days is not large, 32 is."""
    limit = large_days() if limit is None else limit
    sz = size(old_in, old_ex, new_in, new_ex)
    reasons = []
    if sz["inception_days"] > limit:
        reasons.append(f"The start date moves by {sz['inception_days']} days "
                       f"(more than {limit}).")
    if sz["expiry_days"] > limit:
        reasons.append(f"The end date moves by {sz['expiry_days']} days "
                       f"(more than {limit}).")
    return {"large": bool(reasons), "limit": limit, **sz, "reasons": reasons}


# ── B2: typed confirmation, tied to the preview ────────────────────────────

def preview_token(contract_id: int, new_in, new_ex, state) -> str:
    """A fingerprint of what the preview showed: the contract, the dates asked
    for, and the state they were judged against (this contract's and its
    neighbours' dates, the rules it would re-date). The save must send the
    same token, so a confirmation typed against an older preview — or other
    dates — is refused."""
    payload = json.dumps([contract_id, str(new_in), str(new_ex), state],
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def code_matches(typed: Optional[str], code: str) -> bool:
    """The exact contract code; only spaces around it are forgiven."""
    return bool(code) and (typed or "").strip() == code


# ── rules: the fingerprint an undo checks against ──────────────────────────

def rule_fingerprint(spec, name, message, description) -> str:
    payload = json.dumps({"spec": spec, "name": name, "msg": message,
                          "desc": description}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


# ── B4: when an undo is safe ───────────────────────────────────────────────

# What a correction records about the world it left, and what an undo compares
# it with now: a fingerprint of each kind of record (contract_routes.
# _period_marks), so ANY difference — a new row, a changed one, a removed one —
# counts as "something happened since", with no reading of clocks.
MARKS = {
    "arrivals": "files received or their handling changed",
    "exports": "bordereau results produced or changed",
    "exceptions": "exceptions raised or resolved",
    "decisions": "exception decisions made",
    "versions": "submission versions recorded",
    "releases": "received marks or deliveries recorded",
    "policies": "policy rows loaded",
    "uploads": "files uploaded",
    "bdx_uploads": "bordereau files uploaded",
    "runs": "checks run on files",
}


def undo_blockers(change: dict, now: dict) -> list[dict]:
    """Every reason this correction can't be undone now; [] when it can.

    `change` is the correction as recorded: ref, new/old dates, the stored
    lifecycle it left, its neighbours, the rules it re-dated (fingerprints
    after) and the marks. `now`: latest_ref, latest_kind, dates, lifecycle,
    neighbours, rules (id → fingerprint, None if gone), marks, and `conflicts`
    — blocking term conflicts the OLD dates would have today."""
    out: list[dict] = []

    def add(code, message):
        out.append({"code": code, "message": message})

    if now.get("latest_kind") == "undo":
        # Said alone: once undone, every other comparison is about a state the
        # undo itself produced, and listing them would only confuse.
        add("already_undone", "This correction has already been undone. An undo can't "
                              "itself be undone — to change the dates again, make a new "
                              "correction." if now.get("latest_undoes") == change.get("ref")
            else "The latest date change is an undo, which can't itself be undone.")
        return out
    if now.get("latest_ref") != change.get("ref"):
        add("not_latest", "Only the latest date correction can be undone.")
    if list(now.get("dates") or []) != list(change.get("new") or []):
        add("dates_changed", "The dates have changed since this correction, so undoing "
                             "it would no longer put back what was there.")
    if (now.get("lifecycle") or "") != (change.get("lifecycle_after") or ""):
        add("state_changed", f"The contract has become {now.get('lifecycle')} since this "
                             "correction.")
    nb_then, nb_now = change.get("neighbours") or {}, now.get("neighbours") or {}
    if (nb_then.get("next") or [None])[0] != (nb_now.get("next") or [None])[0]:
        add("renewal_changed", "A renewal of this contract was created or removed since "
                               "this correction.")
    elif nb_then != nb_now:
        add("neighbours_changed", "The version before or after this one has new dates "
                                  "since this correction.")
    # Keys are rule ids; JSON (the audit row) turns them into strings.
    rules_now = {str(k): v for k, v in (now.get("rules") or {}).items()}
    changed_rules = [rid for rid, fp in (change.get("rules_after") or {}).items()
                     if rules_now.get(str(rid)) != fp]
    if changed_rules:
        add("rules_changed", f"{len(changed_rules)} rule(s) re-dated by this correction "
                             f"have been edited, re-read or removed since: "
                             f"{', '.join(str(r) for r in sorted(changed_rules, key=str))}.")
    then_m, now_m = change.get("marks") or {}, now.get("marks") or {}
    since = [label for key, label in MARKS.items() if now_m.get(key) != then_m.get(key)]
    if since:
        add("files_since", "Files have been handled for this contract since the "
                           f"correction ({', '.join(since)}). Undoing it now could leave "
                           "those results inconsistent with the dates, so make a new "
                           "correction instead — it shows which files to review.")
    for c in now.get("conflicts") or []:
        add("overlap", "The earlier dates would now overlap another version: "
                       + c.get("message", ""))
    return out


def _fmt(d: dt.date) -> str:
    return f"{d.day} {d:%b %Y}"
