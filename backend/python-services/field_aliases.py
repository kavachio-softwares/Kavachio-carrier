"""What a bordereau column can be called.

An output template asks for "Commission". The file that arrives says "CM". A
person reads that in a second; exact string matching does not, and reporting
"Commission is required but no Commission column was found" when CM is sitting
right there is the failure this module exists to prevent.

This is DATA, not logic. The seed table below is a starting set, versioned so a
change to it is auditable (the same discipline `vocabulary.py` applies to
canonical VALUES — this is its counterpart for column NAMES). Deployments extend
it without touching code by pointing ``KAVACHIO_FIELD_ALIASES`` at a JSON file:

    {"commission": ["cm", "comm", "commission amt"], ...}

Its entries are MERGED over the seed, so a deployment adds and overrides without
having to restate the whole table. Keys and values are normalised on load, so
the file can be written however reads best.

Deliberately not in the frontend: the same table has to be applied at setup, at
generation and by validation, and three copies would drift.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading

log = logging.getLogger("bdx.field_aliases")

# Bump when the seed changes, so a mapping decision can record which table
# produced it (see semantic_mapping.Decision.alias_version).
ALIAS_VERSION = "1"

# field key -> the names a bordereau might use for it. Written lower-case and
# normalised on load, so spelling here is for readability only.
_SEED: dict[str, list[str]] = {
    "commission": ["cm", "comm", "commission amt", "commission amount",
                   "comm amt", "commission value", "brokerage"],
    "commission_rate": ["comm %", "commission %", "commission pct",
                        "commission rate", "brokerage %"],
    "policy_number": ["policy no", "policy #", "pol no", "pol #", "policy id",
                      "policy ref", "policy reference", "certificate number",
                      "cert no"],
    "gross_premium": ["gp", "gwp", "gross prem", "gross premium amount",
                      "gross written premium", "premium gross"],
    "net_premium": ["np", "nwp", "net prem", "net premium amount",
                    "net written premium", "premium net"],
    "insured_name": ["insured", "assured", "insured party", "named insured",
                     "client name", "policyholder"],
    "effective_date": ["eff date", "inception date", "policy effective date",
                       "inception", "from date", "period from"],
    "expiry_date": ["exp date", "expiration date", "policy expiry",
                    "expiry", "to date", "period to"],
    "currency": ["curr", "ccy", "currency code", "cur"],
    "sum_insured": ["si", "tsi", "total sum insured", "insured value",
                    "limit of liability"],
    "claim_number": ["claim no", "claim #", "clm no", "claim ref",
                     "claim reference"],
    "paid_amount": ["paid", "amount paid", "claim paid", "paid to date"],
    "reserve_amount": ["reserve", "outstanding reserve", "os reserve",
                       "outstanding"],
    "deductible": ["ded", "excess", "retention"],
    "broker_name": ["broker", "producer", "producing broker", "intermediary"],
    "insurer_name": ["insurer", "carrier", "underwriter", "carrier name"],
    "tax_amount": ["tax", "ipt", "premium tax", "taxes"],
    "state": ["st", "province", "region", "state code"],
    "postcode": ["zip", "zip code", "post code", "postal code"],
    "country": ["country code", "ctry", "domicile"],
}

_lock = threading.Lock()
_cache: dict | None = None


def _norm(s) -> str:
    """Same normalisation the mapper uses, so both sides agree on 'Policy No'
    and 'policy_no' being one name."""
    t = str(s or "").lower().replace("%", " percent ").replace("$", " dollar ")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        table: dict[str, set[str]] = {
            _norm(k): {_norm(v) for v in vals} for k, vals in _SEED.items()}
        path = (os.getenv("KAVACHIO_FIELD_ALIASES") or "").strip()
        source = "seed"
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    extra = json.load(fh)
                for k, vals in (extra or {}).items():
                    key = _norm(k)
                    table.setdefault(key, set()).update(
                        _norm(v) for v in (vals or []))
                source = f"seed+{path}"
            except Exception as e:  # noqa: BLE001 — a bad file costs aliases, never a request
                log.warning("field alias file %s unreadable (%s); using the seed only",
                            path, e)
        # The key is always an alias of itself, and so is its spaced form.
        for key in list(table):
            table[key].add(key)
        # Reverse index: any known name -> the field keys it can mean. A name
        # can legitimately mean more than one field ("premium"), which is what
        # makes the ambiguity check in semantic_mapping necessary.
        reverse: dict[str, set[str]] = {}
        for key, names in table.items():
            for n in names:
                if n:
                    reverse.setdefault(n, set()).add(key)
        _cache = {"table": table, "reverse": reverse, "source": source}
        log.info("field aliases loaded from %s (%d fields, %d names)",
                 source, len(table), len(reverse))
        return _cache


def reload_aliases() -> None:
    """Drop the cache so the next call re-reads the configured file."""
    global _cache
    with _lock:
        _cache = None


def aliases_for(field: str) -> set[str]:
    """Every name this field is known by, normalised. Empty when unknown."""
    t = _load()["table"]
    key = _norm(field)
    if key in t:
        return set(t[key])
    # An output column is a HEADING ("Gross Premium"), the table is keyed by
    # field key ("gross_premium") — normalisation makes those the same string,
    # so a miss here means we genuinely do not know the field.
    return set()


def fields_named(name: str) -> set[str]:
    """Which field keys a column name could mean. Empty when unrecognised."""
    return set(_load()["reverse"].get(_norm(name), set()))


def share_an_alias(a: str, b: str) -> bool:
    """Do these two names refer to the same field in the table?

    Symmetric on purpose: "Commission" vs "CM" has to hold whichever side the
    output template is on.
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if nb in aliases_for(na) or na in aliases_for(nb):
        return True
    shared = fields_named(na) & fields_named(nb)
    return bool(shared)


def source_description() -> str:
    """Where the loaded table came from — recorded on mapping decisions."""
    return _load()["source"]
