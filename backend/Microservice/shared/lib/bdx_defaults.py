"""Appendix 2 §2.7 / §2.8 default values — ONE definition, both stages.

WHY THIS MODULE EXISTS
──────────────────────
The same defaults are applied at two different stages, by two different pieces
of code:

    BDX Process  direct_lane.eval_rule   → the value written into the OUTPUT file
    Data Model   ingester._child_defaults → the value written into the WAREHOUSE

If those two disagree, NOTHING ERRORS. Both stages succeed, the delivered file
says one thing and the sub-ledger says another, and the break surfaces weeks
later as a reconciliation the finance team cannot close. Defining the values
once removes the possibility.

The same argument applies across systems: these rows also pass through Palms'
PRS sub-ledger pipeline, which applies its own COALESCE with these exact
literals. Change them only in concert with Palms, never as part of a refactor.

    Palms BDX Ingestion BRD Validations v1.2, Appendix 2 §2.7:
        insured_name -> COALESCE(NULLIF(TRIM(insured_name), ''), 'Unknown')
        orig_curr    -> COALESCE(orig_curr, 'USD')
        amount       -> COALESCE(amount, 0)
        exchg_rate   -> COALESCE(exchg_rate, 1.0)

    §2.8:
        COALESCE(rt.prs_transaction_short_types, 'UNK')
        "Null transaction codes are not permitted in output. 'UNK' is the
         required default."

Deliberately dependency-free: importable from the render path without pulling in
SQLAlchemy or opening a database connection.
"""
from __future__ import annotations

# ── §2.7 — identical at both stages ────────────────────────────────────────
DEFAULT_CURRENCY     = "USD"      # COALESCE(orig_curr, 'USD')
DEFAULT_AMOUNT       = 0          # COALESCE(amount, 0)
DEFAULT_INSURED_NAME = "Unknown"  # COALESCE(NULLIF(TRIM(name), ''), 'Unknown')
DEFAULT_FX_RATE      = 1.0        # COALESCE(exchg_rate, 1.0)

# ── §2.8 — the ONE value that differs by stage, deliberately ───────────────
#
# WAREHOUSE: 'unknown'. We translate Palms' shortcode vocabulary at ingest
# (NB→new, EN→endorsement, ENDT→endorsement — see ingester._TXN_TYPE_MAP) and
# transaction_type_e is lowercase snake_case throughout (new, renewal,
# flat_cancellation). 'unknown' is the canonical form of their 'UNK'.
#
# OUTPUT: 'UNK'. §2.8 specifies that literal for the delivered file, and the
# file is what Palms reconciles against — so it carries THEIR vocabulary, not
# our internal enum. Writing 'unknown' into a bordereau column whose values are
# 'New'/'Renewal'/'Endorsement' would also be inconsistent with the rest of the
# sheet.
#
# If a program's output should instead carry the canonical form, change this one
# line — it is the only place the output literal is defined.
DEFAULT_TRANSACTION_TYPE        = "unknown"   # warehouse (enum value)
OUTPUT_DEFAULT_TRANSACTION_TYPE = "UNK"       # delivered file (§2.8 literal)

# What a defaulted value MEANS, for the exception text the broker reads. A
# default that cannot be told apart from real data is worse than no default;
# these are the strings the library rules flag (see generic_rule_specification
# ids 50 and 53).
DEFAULTED_MARKERS = (
    DEFAULT_INSURED_NAME,
    DEFAULT_TRANSACTION_TYPE,
    OUTPUT_DEFAULT_TRANSACTION_TYPE,
)


# ── which OUTPUT column gets which default ─────────────────────────────────
#
# Consulted in two places, which is exactly why it lives here and not in either
# of them:
#
#   direct_mapper.propose_column_mapping   → every NEW format is born with them
#   scripts/apply_output_defaults          → backfills formats created earlier
#
# A new carrier/program/contract mints a NEW direct_format with a freshly
# proposed mapping, so patching formats one at a time never catches up — 428,
# 429, 430 were created within days of each other. The proposer is the only
# place that scales.
#
# EXACT lowercase names, never substrings: "insured" must not also catch
# "insured city", and "gross premium" must not catch "carrier net/net premium".
# A column that matches nothing is left alone — silence is the safe default,
# since a wrong COALESCE writes a number into the books.
#
# DATES ARE DELIBERATELY ABSENT. §2.2 is explicit: a missing policy effective
# date "must be removed at extraction. This is a HARD FILTER, NOT A FALLBACK."
# Defaulting it would turn the blank into a value, the NotNull rule (library id
# 51) would pass over the output records, and the exception would vanish — the
# two halves would cancel each other out. Never add a date column here.
_OUTPUT_DEFAULTS: tuple[tuple[str, frozenset, object], ...] = (
    ("2.7", frozenset({"insured", "insured name", "insured full name",
                       "insured legal name"}),           DEFAULT_INSURED_NAME),
    ("2.7", frozenset({"currency", "orig curr", "original currency",
                       "policy currency"}),              DEFAULT_CURRENCY),
    ("2.7", frozenset({"gross premium", "net premium", "commission amount",
                       "apd premium", "mtc premium"}),   DEFAULT_AMOUNT),
    ("2.7", frozenset({"exchange rate", "exchg rate", "fx rate"}),
                                                         DEFAULT_FX_RATE),
    ("2.8", frozenset({"new/renewal", "transaction type", "txn type",
                       "transaction code"}),
                                        OUTPUT_DEFAULT_TRANSACTION_TYPE),
)


def default_for_output_column(column_name: str):
    """(§ref, default) for an output column, or (None, None) if it takes none.

    Matching is exact on the stripped, lowercased name — see _OUTPUT_DEFAULTS
    for why a substring match would be wrong.
    """
    lc = (column_name or "").strip().lower()
    for ref, names, value in _OUTPUT_DEFAULTS:
        if lc in names:
            return ref, value
    return None, None


def with_output_default(column_name: str, rule: dict) -> dict:
    """Attach the §2.7/§2.8 default to a copy rule, if the column takes one.

    Returns the rule unchanged when the column has no default, when the rule is
    not a `copy` (a const/transform produces its own value — a COALESCE there
    would mask a broken formula rather than a missing input), or when a default
    is already set. Never mutates the input.
    """
    if not isinstance(rule, dict) or rule.get("kind") != "copy":
        return rule
    if "default" in rule:
        return rule
    _ref, value = default_for_output_column(column_name)
    if value is None:
        return rule
    return {**rule, "default": value}
