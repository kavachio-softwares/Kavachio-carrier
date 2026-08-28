"""Business-segment taxonomy — DISPLAY-ONLY classification for the dashboard.

Scope, deliberately narrow: this module is imported by exactly one caller, the
Program Management stats endpoint, and it never writes anything. The stored
`program.business_segment` column is left untouched, so contract extraction, the
BDX field catalog, the program edit forms and every other reader keep behaving
exactly as they do today. Deleting this file and its two call sites reverts the
whole feature.

Why it exists: `business_segment` is an unconstrained TEXT column and contract
extraction asks for it as a free string, so it fills up with class-of-business
clauses rather than segments — e.g. "Excess and Surplus Lines Auto Physical
Damage (APD) and Motor Truck Cargo (MTC)". The dashboard groups its segment
chart by that column verbatim, so the chart ends up plotting sentences.

`classify()` maps that free text onto the seven segments below for display. The
raw value is still shown as the tooltip on the Program Book row, so the
contract's own wording is never hidden from the reader.

TO ADJUST THE TAXONOMY: edit `_RULES`. It is an ordered list and the FIRST match
wins, so more specific classes must sit above broader ones.
"""
from __future__ import annotations

import re
from typing import Optional

# The seven segments the Program Management dashboard is specified against.
CANONICAL_SEGMENTS: tuple[str, ...] = (
    "Property",
    "Casualty",
    "Specialty Property",
    "Professional Lines",
    "Specialty Lines",
    "Complex Treaty",
    "Other",
)

_BY_SLUG = {re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_"): s
            for s in CANONICAL_SEGMENTS}

# Spellings and legacy codes that mean a canonical value outright. Checked
# before any keyword inference. 'multiple' / 'all' come from the older
# data_model wording and mean "more than one segment", which this taxonomy has
# no bucket for — so they land in Other rather than being guessed at.
_ALIASES: dict[str, str] = {
    "prop": "Property", "property_lines": "Property",
    "cas": "Casualty", "casualty_lines": "Casualty",
    "spec_property": "Specialty Property", "specialty_prop": "Specialty Property",
    "prof_lines": "Professional Lines", "professional": "Professional Lines",
    "spec_lines": "Specialty Lines", "specialty": "Specialty Lines",
    "treaty": "Complex Treaty", "complex_treaties": "Complex Treaty",
    "assumed_re": "Complex Treaty", "assumed_reinsurance": "Complex Treaty",
    "multiple": "Other", "all": "Other", "various": "Other",
    "misc": "Other", "miscellaneous": "Other",
}

# Ordered, FIRST MATCH WINS. Patterns run against the lowercased original text
# (not a slug) so word boundaries still mean something inside a long clause.
_RULES: tuple[tuple[str, str], ...] = (
    # Deal STRUCTURE outranks the peril it happens to cover — a quota-share
    # treaty on property risks is a treaty, not a property program.
    ("Complex Treaty", r"\b(quota[\s-]?share|excess of loss|\bxol\b|retrocession|"
                       r"surplus treaty|treaty|reinsurance|assumed re)\b"),

    ("Professional Lines", r"\b(professional (liability|lines|indemnity)|"
                           r"errors and omissions|\be&o\b|\bd&o\b|"
                           r"directors and officers|\bepli\b|employment practices|"
                           r"fiduciary|(medical )?malpractice|lawyers|"
                           r"architects and engineers|miscellaneous professional|"
                           r"financial lines)\b"),

    # Transportation programs (trucking: auto physical damage + motor truck
    # cargo) sit here rather than under Specialty Property. They are written as
    # specialty transportation business, and the cargo element alone should not
    # pull the whole program into a marine bucket.
    ("Specialty Lines", r"\b(motor truck cargo|\bmtc\b|auto physical damage|\bapd\b|"
                        r"trucking|transportation|commercial auto physical)\b"),

    ("Specialty Lines", r"\b(cyber|surety|fidelity|crime|kidnap and ransom|\bk&r\b|"
                        r"political risk|trade credit|contingency|event cancellation|"
                        r"warranty|accident and health|\ba&h\b|travel|pet|"
                        r"specialty lines?)\b"),

    ("Specialty Property", r"\b(inland marine|ocean marine|builder'?s risk|"
                           r"equipment breakdown|boiler and machinery|cargo|hull|"
                           r"difference in conditions|\bdic\b|parametric)\b"),

    # `liability` last within this rule: Professional and Specialty Lines are both
    # tested earlier, so a generic liability wording that reached here is casualty.
    ("Casualty", r"\b(casualty|general liability|\bgl\b|umbrella|excess liability|"
                 r"products liability|premises|auto liability|commercial auto|"
                 r"workers'? comp(ensation)?|\bwc\b|garage liability|"
                 r"pollution|environmental|liability)\b"),

    # Broadest peril class, tested last so a specialty or treaty wording claims
    # the row first.
    ("Property", r"\b(propert(y|ies)|fire|catastroph(e|ic)|\bcat\b|windstorm|wind|"
                 r"hail|earthquake|flood|named storm|all risk|business interruption|"
                 r"commercial property|homeowners|dwelling)\b"),
)


def classify(value: Optional[str]) -> Optional[str]:
    """Map free text onto one of CANONICAL_SEGMENTS, or None.

    None means "cannot tell" — the caller shows "Unspecified" rather than
    asserting a segment the contract never stated. Never returns "Other" by
    guessing; "Other" only comes from an explicit alias.

    >>> classify("CASUALTY")
    'Casualty'
    >>> classify("Excess and Surplus Lines Auto Physical Damage (APD) "
    ...          "and Motor Truck Cargo (MTC)")
    'Specialty Lines'
    >>> classify("Renewable Energy Property and Energy-Related Property "
    ...          "including Construction and Operational exposures and "
    ...          "associated or stand-alone catastrophic perils")
    'Property'
    >>> classify("  ") is None
    True
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if not slug:
        return None
    if slug in _BY_SLUG:            # already canonical, any casing/separator
        return _BY_SLUG[slug]
    if slug in _ALIASES:            # known alias or legacy code
        return _ALIASES[slug]

    lowered = text.lower()
    for segment, pattern in _RULES:
        if re.search(pattern, lowered):
            return segment
    return None
