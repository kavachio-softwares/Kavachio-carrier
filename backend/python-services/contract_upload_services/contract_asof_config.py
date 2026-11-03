"""
contract_asof_config.py
───────────────────────
Feature 7 — Prior Period Files: every knob, in one place.

WHY THIS IS CONFIGURATION AND NOT CODE
──────────────────────────────────────
§7.1 says "resolve each transaction to the contract version in force on its
transaction date". Which column IS the transaction date is not a property of the
software — it is a property of the contract and the file type:

    premium / risk bordereau → policy inception. The contract in force when the
                               risk ATTACHED governs it for its whole term, so a
                               2025 endorsement premium on a policy bound in 2024
                               still answers to the 2024 contract.
    claims bordereau         → date of loss. The cover in force when the loss
                               occurred is the cover that responds.
    claims movement          → usually still date of loss, not the movement date.

Pick the wrong column and nothing errors — you get plausible-looking wrong money.
So the choice is stored per setup, never inferred silently, and auto-detection is
the last resort rather than the default.

RESOLUTION ORDER (most specific wins)
─────────────────────────────────────
    1. pipeline.governing_date_field        — the thing that actually runs
    2. direct_format.governing_date_field   — the setup it runs under
    3. auto-detection against DATE_FIELD_CANDIDATES
    4. None → no as-of resolution; the caller keeps the contract it already had

Every layer is optional and every failure degrades to (4), so a misconfiguration
can only ever mean "behaves as it did before Feature 7".
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Iterable, Optional


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()] if raw else default


def _env_one(name: str, default: str, allowed: Iterable[str]) -> str:
    v = os.getenv(name, "").strip().lower() or default
    return v if v in allowed else default


# Ordered candidates for auto-detection. FIRST MATCH WINS, so the order encodes
# the policy above: inception before transaction date, because for a premium
# bordereau the date the risk attached is the one that picks the contract.
# Override wholesale with CONTRACT_ASOF_DATE_FIELDS (comma-separated).
DATE_FIELD_CANDIDATES: list[str] = _env_list("CONTRACT_ASOF_DATE_FIELDS", [
    # policy inception — preferred for premium/risk bordereaux
    "PolicyTermBeginDate", "PolicyInception", "PolicyEffectiveDate",
    "policy_effective_date", "InceptionDate", "EffectiveDate",
    # loss dates — claims bordereaux
    "DateOfLoss", "LossDate", "date_of_loss",
    # transaction dates — last, because they answer a different question
    "TransactionEffectiveDate", "TransactionDate", "transaction_date",
])

# Which row's date governs a run, when the file's rows carry several.
#   min  — the earliest row. Safest default: a file that straddles a version
#          boundary resolves to the OLDER version, so a prior-period file is
#          never judged by rules that did not yet exist when it was written.
#   max  — the latest row.
#   mode — the most common date.
# Only relevant until row-level partitioning lands; then each row resolves alone.
DATE_STRATEGY: str = _env_one("CONTRACT_ASOF_DATE_STRATEGY", "min", ("min", "max", "mode"))

# What to do when NO contract version covers the resolved date.
#   pin  — keep the contract the caller already had (safe, preserves old behaviour)
#   skip — resolve to nothing, so the caller can route the rows to exceptions
# 'pin' is the default deliberately: 'skip' is more correct per §7 but changes
# what a run produces, and that should be an explicit choice.
ON_UNRESOLVED: str = _env_one("CONTRACT_ASOF_ON_UNRESOLVED", "pin", ("pin", "skip"))


def enabled(*sources: Any) -> bool:
    """Is as-of resolution on for this run?

    A per-setup `asof_config.enabled` overrides the global CONTRACT_ASOF_ENABLED
    in both directions, so one programme can be piloted without touching the
    others, or excluded when its dates are known to be unreliable.
    """
    for src in sources:
        cfg = getattr(src, "asof_config", None) or {}
        if isinstance(cfg, dict) and "enabled" in cfg:
            return bool(cfg["enabled"])
    from contract_upload_services.contract_asof import asof_enabled
    return asof_enabled()


def setting(name: str, default: str, *sources: Any) -> str:
    """Read one knob, most-specific source first, else the module default."""
    for src in sources:
        cfg = getattr(src, "asof_config", None) or {}
        if isinstance(cfg, dict) and cfg.get(name):
            return str(cfg[name]).strip().lower()
    return default


def governing_date_field(columns: Iterable[str], *sources: Any) -> Optional[str]:
    """The column whose dates decide which contract version governs.

    `sources` are checked in order (pipeline, then setup); `columns` are the
    column names actually present in the file, used only for auto-detection.
    Returns None when nothing matches — the caller then leaves the contract as
    it found it.
    """
    for src in sources:
        explicit = getattr(src, "governing_date_field", None)
        if explicit:
            return str(explicit)

    # Auto-detect: exact match first, then case-insensitive, then a normalised
    # comparison so 'Policy Term Begin Date' matches 'PolicyTermBeginDate'.
    present = list(columns or [])
    lower = {str(c).strip().lower(): c for c in present}
    squashed = {"".join(ch for ch in str(c).lower() if ch.isalnum()): c for c in present}
    for cand in DATE_FIELD_CANDIDATES:
        if cand in present:
            return cand
        hit = lower.get(cand.strip().lower())
        if hit:
            return hit
        hit = squashed.get("".join(ch for ch in cand.lower() if ch.isalnum()))
        if hit:
            return hit
    return None


def coerce_date(value: Any) -> Optional[date]:
    """Best-effort value → date. Returns None for anything unparseable, so one
    bad cell cannot decide (or block) a run."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s[:len(fmt) + 8], fmt).date()
        except ValueError:
            continue
    try:                                    # ISO with timezone / fractional secs
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def pick_date(values: Iterable[Any], strategy: Optional[str] = None) -> Optional[date]:
    """Reduce a column of dates to the ONE that governs this run.

    Interim behaviour: until row-level partitioning lands a run resolves to a
    single contract version, so a file spanning a boundary has to pick. `min`
    is the default because resolving to the OLDER version cannot judge a row by
    rules that did not exist when it was written — the failure §7 exists to
    prevent. Returns None when nothing parses.
    """
    strategy = (strategy or DATE_STRATEGY).lower()
    dates = [d for d in (coerce_date(v) for v in values) if d is not None]
    if not dates:
        return None
    if strategy == "max":
        return max(dates)
    if strategy == "mode":
        from collections import Counter
        return Counter(dates).most_common(1)[0][0]
    return min(dates)


def describe(field: Optional[str], governing: Optional[date],
             strategy: Optional[str] = None) -> str:
    """One line for the run log — so a surprising contract choice can be traced
    to the column and the date that caused it, not just observed."""
    if not field:
        return "as-of: no governing date column configured or detected"
    if governing is None:
        return f"as-of: '{field}' held no parseable dates"
    return (f"as-of: governing date {governing.isoformat()} "
            f"from '{field}' ({(strategy or DATE_STRATEGY)} of the file's rows)")
