"""Plain-English explanation of a validation rule, for the reviewer's screen.

WHY THIS EXISTS
───────────────
The exception screens used to describe a rule with three things a reviewer
cannot act on:

  1. the rule NAME               "Transaction Effective Date Must Be Valid"
  2. the compiler's REASON       "Transaction Effective Date must be <= Policy
                                  Expiration Date"
  3. the raw SOURCE TEXT, shown under a "Contract Clause" heading — which for a
     standard (non-contract) rule was the library's internal wording:
     "[Generic rule] Transaction Effective Date Must Be Valid — Transaction
      effective date must be present, must not fall after pol_exp_dt or
      tran_exp_dt, and must sit between 1980 and today."

Every part of that is a problem. (3) leaks source-system column names
(`pol_exp_dt`) at a business user, mislabels a Kavachio standard check as a
contract clause, and — worst — DESCRIBES A DIFFERENT RULE than the one that
ran: the prose promises three checks, the compiled IR is a single
`date_relation`. Prose drifts from the executable spec; the IR cannot.

So the explanation here is derived from the IR — the template + params that
were actually compiled to SQL and actually ran. It is deterministic (no LLM, no
DB), so it costs nothing at read time and can never disagree with the check.

WHAT IT RETURNS  (see `explain_rule`)
    requirement   what the rule requires, as one plain sentence
    applies_to    the row scope in words, when the rule is scoped
    accepts_also  surface spellings an enum rule also accepts
    origin        contract | standard | derived
    origin_label  short provenance chip ("Contract clause · file.pdf p.3")
    origin_note   one sentence on what that provenance means
    source_text   the clause/library text with internal markers stripped
    how_to_fix    what the reviewer should do about it

Anything not derivable comes back as None — the caller keeps its existing
fallbacks, so this is purely additive.
"""
from __future__ import annotations

import json
import re

# Markers the pipeline prefixes onto a rule's source text to record where the
# rule came from. They are provenance, not clause wording, so they are stripped
# out of `source_text` and turned into `origin` instead.
_GENERIC_MARKER = "[Generic rule]"
_DERIVED_MARKERS = ("[Derived rule]", "[Derived formula]")

# Comparison operator → words. Dates and quantities read differently for the
# same operator ("on or after 1 April" vs "at least 5"), so both are kept.
_DATE_OPS = {
    ">=": "on or after", ">": "after",
    "<=": "on or before", "<": "before",
    "=":  "the same date as", "!=": "a different date from", "<>": "a different date from",
}
_NUM_OPS = {
    ">=": "at least", ">": "more than",
    "<=": "no more than", "<": "less than",
    "=":  "equal to", "!=": "different from", "<>": "different from",
}
_COND_OPS = {
    ">=": "is at least", ">": "is more than",
    "<=": "is no more than", "<": "is less than",
    "=":  "is", "!=": "is not", "<>": "is not",
}

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


# =====================================================================
# Formatting helpers
# =====================================================================

def _spec_dict(rule_spec):
    """rule_spec as a dict — it arrives as JSONB (dict) or as text (str)."""
    if isinstance(rule_spec, str):
        try:
            rule_spec = json.loads(rule_spec)
        except Exception:
            return {}
    return rule_spec if isinstance(rule_spec, dict) else {}


def _num(v):
    """A number the way a business reader writes it: 2,000,000 / 12.5 / 0.235."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,}".rstrip("0").rstrip(".")


def _pct(v):
    """A fraction rendered as a percentage when it plainly is one (0.235 → 23.5%)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if 0 < abs(f) <= 1:
        p = f * 100
        return f"{int(p)}%" if p == int(p) else f"{round(p, 4):g}%"
    return None


def _date(v):
    """'2026-04-01' → 'April 1, 2026'. Anything else is returned unchanged."""
    s = str(v).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return s
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not 1 <= mo <= 12:
        return s
    return f"{_MONTHS[mo - 1]} {d}, {y}"


def _quote_list(vals, limit=6, budget=180):
    """Values as a readable list, truncated so a 40-value enum stays readable.

    Budgeted by CHARACTERS as well as by count: six values named "Government &
    Public Entity Airports" are not the same size as six values named "CA".
    The full set is still available to the UI as recommendation_options.
    """
    # `v is None` first: str(None) is the word "None", which would be rendered
    # to the reviewer as though it were a real value in the contract's list.
    vals = [str(v).strip() for v in (vals or [])
            if v is not None and str(v).strip()]
    if not vals:
        return None
    shown, used = [], 0
    for v in vals[:limit]:
        if shown and used + len(v) > budget:
            break
        shown.append(v)
        used += len(v) + 2
    text = ", ".join(shown)
    rest = len(vals) - len(shown)
    if rest > 0:
        text += f", and {rest} more"
    return text


def _condition_text(field, op, value):
    """One condition in words, handling a NULL value as presence/absence.

    A condition of `{op: '!=', value: None}` means "the column is populated" —
    rendering it literally as "is not None" leaks a Python repr at the reader.
    """
    op = str(op or "=").strip()
    if value is None:
        return f"{field} is blank" if op in ("=", "==") else f"{field} is filled in"
    return f"{field} {_COND_OPS.get(op, op)} {_value(value)}"


def _sentence(s):
    """End a derived sentence with exactly one full stop.

    Values carry their own punctuation ("PALMS SPECIALTY INSURANCE COMPANY,
    INC."), so a blindly appended "." doubles up.
    """
    s = (s or "").strip()
    if not s:
        return None
    # A value that already ends in a full stop ("PALMS SPECIALTY … INC.") would
    # otherwise leave the sentence ending "INC..".
    while s.endswith(".."):
        s = s[:-1]
    return s if s.endswith((".", "?", "!")) else s + "."


def _value(v):
    """One param value in prose — percentages and thousands where they help."""
    if isinstance(v, (list, tuple)):
        return _quote_list(v)
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, (int, float)):
        return _num(v)
    return str(v)


def _country_names(params):
    """A postal rule's `countries` narrowing in words ('GB' → 'United Kingdom'),
    or "" when the rule is not narrowed. The narrowing exists when the CONTRACT
    itself names the country, so the reviewer needs to see which reference the
    check used — no BDX column states it."""
    codes = (params or {}).get("countries")
    if not isinstance(codes, (list, tuple)) or not codes:
        return ""
    try:
        from contract_upload_services.intl_postal_reference import (
            country_display_name)
    except Exception:                                       # pragma: no cover
        return " / ".join(str(c) for c in codes)
    return " / ".join(country_display_name(c) for c in codes)


# =====================================================================
# Row scope → words
# =====================================================================

def _scope_pred_text(col, v):
    """One scope filter in words, covering every shape rule_compiler accepts."""
    if isinstance(v, (list, tuple, set)):
        lst = _quote_list(list(v))
        return f"{col} is {lst}" if lst else None
    if isinstance(v, dict):
        # rule_compiler._scope_pred computes `base = allowed or excluded` with
        # negate=True whenever an `excluded` key is present — so a dict carrying
        # BOTH is an EXCLUSION of the allowed list, not an inclusion. Mirrored
        # exactly; reading it the other way round inverted the stated scope.
        has_allowed = isinstance(v.get("allowed"), (list, tuple, set))
        has_excluded = isinstance(v.get("excluded"), (list, tuple, set))
        if has_allowed or has_excluded:
            base = list(v.get("allowed") or v.get("excluded") or [])
            lst = _quote_list(base)
            if not lst:
                return None
            return f"{col} is not {lst}" if has_excluded else f"{col} is {lst}"
        op = str(v.get("op", "=")).strip()
        if "date" in v:
            return f"{col} is {_DATE_OPS.get(op, op)} {_date(v['date'])}"
        val = v.get("value")
        if val is None:
            return None
        return f"{col} {_COND_OPS.get(op, op)} {_value(val)}"
    # A scope value that arrived as the STRING form of a dict/list — 76 stored
    # rules carry shapes like "{'op': '!=', 'value': 'CA'}". rule_compiler does
    # not parse those either: it emits `LOWER(TRIM(col)) = '{''op'': ...}'`,
    # a literal no cell can equal. So there is no filter to describe, and
    # str()-ing it would print a Python repr at the underwriter. Refuse both.
    s = str(v).strip()
    if s.startswith(("{", "[", "(")):
        return None
    return f"{col} is {_value(v)}" if s else None


def _scope_text(scope):
    """A whole scope in words: 'Coverage Type is CGL and Risk State is TX'.

    ALL-OR-NOTHING. A scope's filters are ANDed, so describing only the ones we
    can phrase would state a WIDER rule than the SQL runs ("applies only to rows
    where State is TX", when it is really TX *and* Coverage CGL). Silence is the
    only safe partial answer, and the caller simply omits the line.
    """
    if not isinstance(scope, dict) or not scope:
        return None
    parts = []
    for k, v in scope.items():
        if str(k).strip().lower() in ("any_of", "or", "$or", "either"):
            group = []
            for d in (v if isinstance(v, (list, tuple)) else [v]):
                if not isinstance(d, dict):
                    return None
                for kk, vv in d.items():
                    t = _scope_pred_text(kk, vv)
                    if not t:
                        return None
                    group.append(t)
            if not group:
                return None
            parts.append("(" + " or ".join(group) + ")")
            continue
        t = _scope_pred_text(k, v)
        if not t:
            return None
        parts.append(t)
    if not parts:
        return None
    return "Applies only to rows where " + " and ".join(parts) + "."


# =====================================================================
# Regex → words (pattern_check)
# =====================================================================

def _split_alternatives(p):
    """Split a regex on TOP-LEVEL `|` only — `(a|b)c` stays one alternative."""
    out, depth, cur, i = [], 0, [], 0
    while i < len(p):
        ch = p[i]
        if ch == "\\" and i + 1 < len(p):
            cur.append(p[i:i + 2])
            i += 2
            continue
        if ch == "[":                       # char class: | inside is a literal
            j = p.find("]", i + 1)
            j = len(p) - 1 if j == -1 else j
            cur.append(p[i:j + 1])
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [a for a in out if a != ""]


def _strip_anchors(p):
    p = p.strip()
    while p.startswith("^"):
        p = p[1:]
    while p.endswith("$"):
        p = p[:-1]
    return p


def _unwrap_group(p):
    """'(abc)' → 'abc'; '(?:abc)' → 'abc'. Only when the group spans the whole
    string, so '(a)(b)' is left alone."""
    p = p.strip()
    for opener in ("(?:", "("):
        if p.startswith(opener) and p.endswith(")"):
            inner, depth = p[len(opener):-1], 0
            for ch in inner:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth < 0:
                        return p          # the group closed early — not a wrapper
            return inner
    return p


# How many of a thing: {n} / {n,m} / + / * / ? / bare.
def _count_phrase(quant, noun_sing, noun_plur):
    if not quant:
        return f"1 {noun_sing}"
    m = re.fullmatch(r"\{(\d+)\}", quant)
    if m:
        n = int(m.group(1))
        return f"{n} {noun_sing if n == 1 else noun_plur}"
    m = re.fullmatch(r"\{(\d+),(\d+)\}", quant)
    if m:
        return f"{m.group(1)}–{m.group(2)} {noun_plur}"
    m = re.fullmatch(r"\{(\d+),\}", quant)
    if m:
        return f"{m.group(1)} or more {noun_plur}"
    return {"+": f"one or more {noun_plur}",
            "*": f"any number of {noun_plur}",
            "?": f"an optional {noun_sing}"}.get(quant, f"1 {noun_sing}")


_ATOM = re.compile(r"""
    (?P<esc>\\[dwsDWS])                 |
    (?P<bound>\\[bBAZz])                |   # word/string boundary: no characters
    (?P<other>\\.)                      |   # any other escape, e.g. \- \. \/
    (?P<dot>\.)                         |
    (?P<cls>\[[^\]]*\])                 |
    (?P<grp>\()                         |
    (?P<lit>[^\\\[\(\)\{\}\?\*\+\|\.]+)
""", re.X)
_QUANT = re.compile(r"^(\{\d+(?:,\d*)?\}|[?*+])")


def _class_noun(cls):
    """A character class in words ([A-Z] → letter, [A-Z0-9] → letter or digit).

    Returns None for a NEGATED class: `[^0-9]` contains "0-9" and would
    otherwise be described as "digit" — the exact opposite of what it matches.
    Refusing to describe it makes the whole pattern fall back to being shown
    raw, which is honest; a confident inversion is not.
    """
    body = cls[1:-1]
    if body.startswith("^"):
        return None
    has_alpha = bool(re.search(r"a-z", body, re.I))
    has_digit = bool(re.search(r"0-9|\\d", body))
    if has_alpha and has_digit:
        return "letter or digit", "letters or digits"
    if has_alpha:
        return "letter", "letters"
    if has_digit:
        return "digit", "digits"
    return "character", "characters"


def _branch_text(branch, depth):
    """One alternative of a regex, preferring a recognised real-world shape.

    Applied at EVERY nesting level, so the ZIP/postcode shortcut works whether
    the alternation is written `(^A$)|(^B$)` or `^(A|B)$`.
    """
    body = _strip_anchors(_unwrap_group(_strip_anchors(branch)))
    return _friendly_shape(body) or _describe_sequence(body, depth + 1)


def _describe_sequence(p, depth=0):
    """Walk one alternative and describe its atoms in order. None if too deep."""
    if depth > 3:
        return None
    # Unwrapping a group can expose a top-level alternation ("\d{5}|441105").
    # Those are CHOICES, not a sequence — joining them with "then" would invert
    # the meaning, so split them off before walking atoms.
    branches = _split_alternatives(p)
    if len(branches) > 1:
        subs = [_branch_text(b, depth) for b in branches]
        if any(s is None for s in subs):
            return None
        return " or ".join(dict.fromkeys(s for s in subs if s))
    parts, i = [], 0
    while i < len(p):
        m = _ATOM.match(p, i)
        if not m:
            i += 1
            continue
        i = m.end()
        if m.group("grp"):                       # a group — find its extent
            d, j = 1, i
            while j < len(p) and d:
                if p[j] == "\\":
                    j += 2
                    continue
                if p[j] == "(":
                    d += 1
                elif p[j] == ")":
                    d -= 1
                j += 1
            inner, i = p[i:j - 1], j
            q = _QUANT.match(p[i:])
            quant = q.group(0) if q else ""
            i += len(quant)
            # A lookahead/lookbehind asserts something about text it does not
            # consume; walking it as ordinary content turns "(?!TEST)" into the
            # literal "!TEST" and inverts the rule. Non-capturing "(?:" is the
            # only group prefix that is safe to strip.
            if inner.startswith("?") and not inner.startswith("?:"):
                return None
            if inner.startswith("?:"):
                inner = inner[2:]
            alts = _split_alternatives(inner)
            subs = [_branch_text(a, depth) for a in alts]
            if any(s is None for s in subs):
                return None
            sub = " or ".join(s for s in subs if s)
            if not sub:
                continue
            if quant == "?":
                parts.append(f"optionally {sub}")
            elif quant:
                # {n} / {n,m} / + / * on a GROUP. Dropping these understated the
                # required length and turned a repeatable group into a single
                # mandatory one ("^(\d{2}){3}$" read as "2 digits", not 6).
                rep = _count_phrase(quant, "time", "times")
                parts.append(f"({sub}) repeated {rep}"
                             if quant not in ("*",) else f"({sub}) repeated any number of times")
            else:
                parts.append(sub)
            continue

        q = _QUANT.match(p[i:])
        quant = q.group(0) if q else ""
        i += len(quant)

        if m.group("bound"):
            continue                      # a boundary matches no characters
        if m.group("dot"):
            # `.` / `.*` / `.+` — "anything". A trailing one on an anchored
            # pattern is just a prefix match and adds nothing to the reading.
            if quant in ("*", "?") and i >= len(p):
                continue
            parts.append("anything" if quant in ("*", "+") else "any character")
            continue
        if m.group("other"):
            lit = m.group("other")[1]     # the escaped character itself
            if lit.isdigit():
                return None               # \1 is a BACKREFERENCE, not the text "1"
            parts.append(f"optionally “{lit}”" if quant == "?" else f"“{lit}”")
            continue
        if m.group("esc"):
            e = m.group("esc")[1]
            if e == "d":
                parts.append(_count_phrase(quant, "digit", "digits"))
            elif e == "w":
                parts.append(_count_phrase(quant, "letter or digit", "letters or digits"))
            elif e == "s":
                parts.append("a space" if quant in ("", "?") else "spaces")
            else:
                return None
        elif m.group("cls"):
            noun = _class_noun(m.group("cls"))
            if noun is None:
                return None            # negated class — never guess
            parts.append(_count_phrase(quant, *noun))
        elif m.group("lit"):
            lit = m.group("lit")
            if quant == "?":
                parts.append(f"optionally “{lit}”")
            elif lit.strip():
                parts.append(f"“{lit}”")
            else:
                parts.append("a space")
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " then " + parts[-1]


# Recognised real-world shapes get a friendly name instead of a literal
# character-by-character reading — "a UK-style postcode" beats "1–2 letters, a
# digit, optionally a letter or digit, a space, a digit then 2 letters".
#
# Matching is done on a SIGNATURE, not on the regex text: the same shape is
# written many ways across the estate ([A-Z] vs [A-Za-z], \d vs [0-9], \s? vs
# \s* vs a literal space, (?:…) vs (…)), and 27 distinct spellings of two shapes
# is exactly the kind of thing a literal match gets wrong.
#   letter class          → L      digit class      → D
#   mixed letter/digit    → X      whitespace       → S
_CLS_ALNUM = re.compile(r"\[(?=[^\]]*[A-Za-z])(?=[^\]]*(?:0-9|\\d))[^\]]*\]")
_CLS_ALPHA = re.compile(r"\[[A-Za-z\-]+\]")


def _shape_signature(body):
    s = _CLS_ALNUM.sub("X", body)
    s = _CLS_ALPHA.sub("L", s)
    s = re.sub(r"\[0-9\]|\\d", "D", s)
    s = re.sub(r"\(\?:", "(", s)
    s = re.sub(r"\\s[*?]?|[ ]", "S", s)
    return s.strip()


# (signature, friendly label, postal_only). A "5-digit ZIP code" reading is only
# correct on a POSTAL column: `^\d{5}$` is just as common for SIC / NAIC / class
# codes, and calling those a ZIP code is confidently wrong — the exact failure
# this module exists to stop. The UK shape is distinctive enough to stand alone.
# Named ONLY where the shape itself is unambiguous, never by column name —
# column names vary per programme ("Insured Zip" / "Risk Postcode" / "CP"), so
# keying off them would describe one customer's data differently from another's.
#
# A bare 5 digits is deliberately absent: it is just as likely a SIC / NAIC /
# class code as a ZIP, and calling those "a 5-digit ZIP code" was confidently
# wrong. It falls through to the literal walker and reads "5 digits". The ZIP+4
# and postcode shapes below are distinctive enough that no other identifier
# shares them.
_SHAPES = (
    (re.compile(r"^D\{5\}\(-D\{4\}\)\?$"),
     "a 5-digit ZIP code, optionally with a 4-digit extension"),
    # The `\?` are LITERAL question marks in the signature (the regex's own
    # "optional" quantifier), not this pattern's optionality.
    (re.compile(r"^L\{1,2\}D(X\?)?S?DL\{2\}$"), "a UK-style postcode"),
)


def _friendly_shape(alt):
    """A recognised postal shape, or None. Decided by shape alone."""
    sig = _shape_signature(_strip_anchors(_unwrap_group(_strip_anchors(alt))))
    for rx, label in _SHAPES:
        if rx.fullmatch(sig):
            return label
    return None


def _describe_pattern(pattern, field=None):   # noqa: ARG001 - kept for callers
    """Plain English for a whole regex, or None when it is better shown raw.

    Generic on purpose: it walks the regex rather than matching a fixed list of
    known patterns, so a pattern nobody has written yet still reads as English.
    Recognised real-world shapes (US ZIP, UK postcode) short-circuit to a
    friendly name because the literal reading, while correct, is unusable.
    """
    if not pattern:
        return None
    p = str(pattern).strip()
    case_insensitive = "(?i)" in p
    p = p.replace("(?i)", "")
    # Split BEFORE stripping anchors: `(^A$)|(^B$)` carries its anchors inside
    # each alternative, and the anchors are what distinguish "must be X" from
    # "must start with X" — dropping them silently changes the rule's meaning.
    outer = _unwrap_group(p)
    alts = _split_alternatives(outer if _split_alternatives(outer) != [outer] else p)
    described, anchored_start, anchored_end = [], [], []
    for a in alts:
        a = a.strip()
        inner = _unwrap_group(a)
        anchored_start.append(inner.lstrip("(").startswith("^") or a.startswith("^"))
        # A trailing `.*` makes an end-anchor meaningless — it matches anything.
        body = _strip_anchors(_unwrap_group(_strip_anchors(a)))
        anchored_end.append((inner.rstrip(")").endswith("$") or a.endswith("$"))
                            and not body.endswith((".*", ".+")))
        d = _friendly_shape(body) or _describe_sequence(body)
        if not d:
            return None                  # all-or-nothing: never half-explain a regex
        described.append(d)
    if not described:
        return None
    text = " or ".join(dict.fromkeys(described))
    if case_insensitive:
        text += " (upper or lower case)"
    # Anchors → what KIND of match this is. Only a fully-anchored pattern is an
    # exact match; `^(primary|excess)\b` is a PREFIX test and must read as one.
    starts, ends = all(anchored_start), all(anchored_end)
    if starts and not ends:
        return f"start with {text}"
    if ends and not starts:
        return f"end with {text}"
    if not starts and not ends:
        return f"contain {text}"
    return text


# =====================================================================
# Regex → one value that matches it (the reviewer's EXAMPLE)
# =====================================================================
#
# A format rule has no "recommended value" to offer — every string of the right
# shape is equally correct — so the review screen used to show the reviewer the
# regex itself ("matches ^\d{4}$"), which is neither readable nor writable into
# a cell. What helps is one concrete value of the right shape ("1234"): it says
# what the cell should look like without pretending to know what it should say.
#
# The sample is BUILT FROM the pattern (the same walk `_describe_pattern` uses)
# and then VERIFIED against it with `re` before it is handed out, so an example
# can never contradict the check that produced it. Anything the walk cannot
# sample with certainty — a negated class, a backreference, a lookaround —
# yields None and the caller keeps its existing fallback.

_DIGIT_SOURCE = "1234567890"          # reads as 1234 / 12345, not 00000
_ALPHA_SOURCE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_MAX_REPEAT = 40                      # a sample is an illustration, not a stress test
_MAX_SAMPLE = 64


def _take(source, n):
    """`n` characters from `source`, cycling — "1234567890" → "12345"."""
    return "".join(source[i % len(source)] for i in range(n))


def _quant_reps(quant, expand):
    """How many times to repeat an atom for the sample, or None if unusable.

    `expand` is the second pass: a pattern whose whole body is optional samples
    as the empty string on the first pass, which illustrates nothing, so the
    0-or-more quantifiers are taken once instead.
    """
    if not quant:
        return 1
    if quant in ("?", "*"):
        return 1 if expand else 0
    if quant == "+":
        return 1
    m = re.fullmatch(r"\{(\d+)\}", quant)
    if m:
        n = int(m.group(1))
        return n if n <= _MAX_REPEAT else None
    m = re.fullmatch(r"\{(\d+),(\d*)\}", quant)
    if m:
        lo = int(m.group(1))
        n = lo if lo else (1 if expand else 0)
        return n if n <= _MAX_REPEAT else None
    return None


def _class_source(cls):
    """The characters a class matches, in sample order ([A-Z] → "ABC…").

    None for a NEGATED class: `[^0-9]` is every character EXCEPT a digit, and
    the members of a complement can't be enumerated from the text — the same
    reason `_class_noun` refuses to describe one.
    """
    body = cls[1:-1]
    if not body or body.startswith("^"):
        return None
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            e = body[i + 1]
            if e == "d":
                out.append(_DIGIT_SOURCE)
            elif e == "w":
                out.append(_ALPHA_SOURCE)
            elif e == "s":
                out.append(" ")
            elif e in "DWS":
                return None            # a complement again — never guess
            else:
                out.append(e)
            i += 2
            continue
        if i + 2 < len(body) and body[i + 1] == "-":
            lo, hi = ch, body[i + 2]
            if lo == "0" and hi == "9":
                out.append(_DIGIT_SOURCE)
            elif (lo, hi) == ("A", "Z"):
                out.append(_ALPHA_SOURCE)
            elif (lo, hi) == ("a", "z"):
                out.append(_ALPHA_SOURCE.lower())
            elif ord(lo) <= ord(hi):
                out.append("".join(chr(c) for c in range(ord(lo), ord(hi) + 1)))
            else:
                return None
            i += 3
            continue
        out.append(ch)
        i += 1
    return "".join(out) or None


def _sample_branch(branch, expand, depth):
    """One alternative of a regex, with its own anchors/wrapper removed."""
    return _sample_sequence(
        _strip_anchors(_unwrap_group(_strip_anchors(branch))), expand, depth + 1)


def _sample_sequence(p, expand, depth=0):
    """Build one string matching `p`, atom by atom. None when it can't be sure.

    Mirrors `_describe_sequence` — same atoms, same group walk — but emits
    characters instead of words.
    """
    if depth > 6:
        return None
    branches = _split_alternatives(p)
    if len(branches) > 1:
        for b in branches:                 # first alternative that yields a sample
            s = _sample_branch(b, expand, depth)
            if s:
                return s
        return None
    out, i = [], 0
    while i < len(p):
        m = _ATOM.match(p, i)
        if not m:
            i += 1
            continue
        i = m.end()
        if m.group("grp"):                       # a group — find its extent
            d, j = 1, i
            while j < len(p) and d:
                if p[j] == "\\":
                    j += 2
                    continue
                if p[j] == "(":
                    d += 1
                elif p[j] == ")":
                    d -= 1
                j += 1
            inner, i = p[i:j - 1], j
            q = _QUANT.match(p[i:])
            quant = q.group(0) if q else ""
            i += len(quant)
            # A lookaround constrains text it does not consume; sampling its
            # body as content would put characters in that the pattern forbids.
            if inner.startswith("?") and not inner.startswith("?:"):
                return None
            if inner.startswith("?:"):
                inner = inner[2:]
            n = _quant_reps(quant, expand)
            if n is None:
                return None
            if n == 0:
                continue
            sub = _sample_sequence(inner, expand, depth + 1)
            if sub is None:
                return None
            out.append(sub * n)
            continue

        q = _QUANT.match(p[i:])
        quant = q.group(0) if q else ""
        i += len(quant)
        n = _quant_reps(quant, expand)
        if n is None:
            return None

        if m.group("bound"):
            continue                      # a boundary matches no characters
        if m.group("dot"):
            out.append(_take(_ALPHA_SOURCE, n))
        elif m.group("other"):
            lit = m.group("other")[1]     # the escaped character itself
            if lit.isdigit():
                return None               # \1 is a BACKREFERENCE, not the text "1"
            out.append(lit * n)
        elif m.group("esc"):
            src = {"d": _DIGIT_SOURCE, "w": _ALPHA_SOURCE, "s": " "}.get(
                m.group("esc")[1])
            if src is None:
                return None               # \D \W \S — a complement, never guessed
            out.append(_take(src, n))
        elif m.group("cls"):
            src = _class_source(m.group("cls"))
            if src is None:
                return None
            out.append(_take(src, n))
        elif m.group("lit"):
            # `^` / `$` inside a literal run are anchors (a literal one is
            # escaped, and lands in `other`), and an anchor is not a character
            # the value contains. A quantifier binds to the LAST character only:
            # "abc?" is "ab" plus an optional "c".
            lit = m.group("lit").replace("^", "").replace("$", "")
            if not lit:
                continue
            out.append(lit[:-1] + lit[-1] * n)
    s = "".join(out)
    return s if 0 < len(s) <= _MAX_SAMPLE else None


def sample_for_pattern(pattern):
    """One concrete value that MATCHES `pattern`, or None.

    Verified with `re` before it is returned, so the example is always a value
    the rule itself would accept — the sampler may fail to build one, but it
    cannot build a wrong one.
    """
    if not pattern:
        return None
    p = str(pattern).strip()
    # `(?i)` is only legal at the very start in modern Python; these patterns
    # carry it mid-string too, so read it as a flag and drop it from the text.
    flags = re.IGNORECASE if "(?i)" in p else 0
    p = p.replace("(?i)", "")
    try:
        rx = re.compile(p, flags)
    except re.error:
        return None
    for expand in (False, True):
        try:
            s = _sample_sequence(p, expand)
        except Exception:
            return None
        # `regexp_matches` (what the compiler emits) is a SEARCH, so the sample
        # is checked the same way the rule checks the cell.
        if s and rx.search(s):
            return s
    return None


def _only_value_matching(pattern, sample):
    """`sample` when `pattern` accepts nothing else, else None.

    A fully-anchored pattern with no metacharacters left (`^441105$`) is not a
    format at all — it names ONE value, which the reviewer can approve outright.
    """
    if not sample:
        return None
    p = str(pattern).strip()
    if "(?i)" in p or not (p.startswith("^") and p.endswith("$")):
        return None
    body = _strip_anchors(p)
    if re.search(r"[\\\[\](){}?*+|.^$]", body):
        return None
    return sample if body == sample else None


def format_hint(template, params):
    """What to show a reviewer when a rule's expectation is a FORMAT, not a value.

        {"example": "1234",          one value of the right shape, or None
         "format":  "4 digits",      the shape in words, or None
         "exact":   None}            the ONLY acceptable value, when there is one

    None for every template whose expectation already IS a value (an enum, a
    bound, a date) — those need no illustration.
    """
    if template != "pattern_check" or not isinstance(params, dict):
        return None
    pattern = params.get("pattern")
    if not pattern:
        return None
    sample = sample_for_pattern(pattern)
    exact = _only_value_matching(pattern, sample)
    return {
        "example": None if exact else sample,   # an exact value is not an example
        "format": _describe_pattern(pattern, params.get("field")),
        "exact": exact,
    }


# =====================================================================
# IR → the requirement, in one sentence
# =====================================================================

def describe_requirement(template, params):
    """One plain sentence for what `template`+`params` requires, or None.

    Covers the whole template catalog (rule_ir.TEMPLATE_CATALOG), including the
    three code-supplemental reference-data templates. A template that grows a new
    param is unaffected — the sentence just doesn't mention it.
    """
    return _sentence(_requirement(template, params))


def _requirement(template, params):
    if not template or not isinstance(params, dict):
        return None
    p = params
    f = p.get("field")

    if template == "required_field":
        return f"{f} must have a value on every row."

    if template == "conditional_required":
        cond = p.get("condition") or {}
        cf, cop, cv = cond.get("field"), str(cond.get("op", "=")), cond.get("value")
        rf = p.get("required_field")
        if cf:
            return (f"{rf} must have a value whenever "
                    f"{_condition_text(cf, cop, cv)}.")
        return f"{rf} must have a value."

    if template == "value_in_set":
        allowed = _quote_list(p.get("allowed"))
        if not allowed:
            return None
        s = f"{f} must be one of: {allowed}."
        excluded = _quote_list(p.get("excluded"))
        if excluded:
            s += f" It must never be: {excluded}."
        return s

    if template == "value_not_in_set":
        excluded = _quote_list(p.get("excluded"))
        if excluded:
            return f"{f} must never be any of: {excluded}."
        # The excluded set was only blanks/nulls — that is a presence check
        # written the long way round, so say what it actually means.
        if p.get("excluded"):
            return f"{f} must have a value on every row."
        return None

    if template == "max_limit":
        return (f"{f} must not be more than {_num(p.get('max'))}."
                if p.get("max") is not None else None)

    if template == "min_limit":
        mn = p.get("min")
        if mn is None:
            return None
        # "at least 0" is how the check is stored, but "must not be negative" is
        # what it means — and that is the whole point of these rules.
        try:
            if float(mn) == 0:
                return f"{f} must not be negative."
        except (TypeError, ValueError):
            pass
        return f"{f} must be at least {_num(mn)}."

    if template == "range_check":
        lo, hi = p.get("min"), p.get("max")
        if lo is not None and hi is not None:
            if _num(lo) == _num(hi):
                pct = _pct(lo)
                exact = f"{_num(lo)} ({pct})" if pct else _num(lo)
                return f"{f} must be exactly {exact}."
            return f"{f} must be between {_num(lo)} and {_num(hi)}."
        if hi is not None:
            return f"{f} must not be more than {_num(hi)}."
        if lo is not None:
            return f"{f} must be at least {_num(lo)}."
        return None

    if template == "pattern_check":
        desc = _describe_pattern(p.get("pattern"), f)
        if desc:
            # An unanchored pattern yields a VERB phrase ("start with …"), an
            # anchored one a NOUN phrase ("4 digits") — "must be start with" was
            # the giveaway that the two were being glued together identically.
            verb = desc.split(" ", 1)[0] in ("start", "end", "contain")
            return f"{f} must {desc}." if verb else f"{f} must be {desc}."
        return f"{f} must match the format the rule requires." if f else None

    if template == "date_relation":
        op = str(p.get("op", ""))
        other = p.get("other_field")
        if other:
            return f"{f} must be {_DATE_OPS.get(op, op)} {other} on the same row."
        return None

    if template == "date_bound":
        op, d = str(p.get("op", "")), p.get("date")
        return f"{f} must be {_DATE_OPS.get(op, op)} {_date(d)}." if d else None

    if template == "period_duration":
        s_f, e_f = p.get("start_field"), p.get("end_field")
        unit = str(p.get("unit") or "day")

        def _u(n):
            """"1 month", not "1 months"."""
            try:
                one = float(n) == 1
            except (TypeError, ValueError):
                one = False
            return unit if one else unit + "s"

        lo, hi = p.get("min"), p.get("max")
        span = f"The period from {s_f} to {e_f}"
        if lo is not None and hi is not None:
            if _num(lo) == _num(hi):
                return f"{span} must be exactly {_num(lo)} {_u(lo)}."
            return f"{span} must be between {_num(lo)} and {_num(hi)} {_u(hi)}."
        if hi is not None:
            return f"{span} must be no more than {_num(hi)} {_u(hi)}."
        if lo is not None:
            return f"{span} must be at least {_num(lo)} {_u(lo)}."
        op, val = str(p.get("op", "")), p.get("value")
        if val is not None:
            return f"{span} must be {_NUM_OPS.get(op, op)} {_num(val)} {_u(val)}."
        return None

    if template == "aggregate_cap":
        agg = str(p.get("aggregation") or "sum").lower()
        group_by = [g for g in (p.get("group_by") or []) if g]
        per = f" for each {' + '.join(group_by)}" if group_by else " across the file"
        # distinct_count <= 1 is the INVARIANT shape (rule_normalizer): "this
        # value must not change within the group". Say that, not "distinct count".
        if agg == "distinct_count" and p.get("max") in (1, "1", 1.0):
            if group_by:
                return (f"{f} must be the same on every row of the same "
                        f"{' + '.join(group_by)} \u2014 it must not change.")
            return f"{f} must have the same value on every row."
        word = {"sum": "total", "count": "number of rows",
                "distinct_count": "number of different values"}.get(agg, agg)
        if p.get("max") is not None:
            return f"The {word} of {f}{per} must not exceed {_num(p['max'])}."
        if p.get("min") is not None:
            return f"The {word} of {f}{per} must be at least {_num(p['min'])}."
        return None

    if template == "uniqueness":
        flds = [x for x in (p.get("fields") or []) if x]
        if not flds:
            return None
        if len(flds) == 1:
            return f"{flds[0]} must be unique \u2014 no two rows may repeat it."
        return (f"The combination of {' + '.join(flds)} must be unique \u2014 "
                f"no two rows may repeat it.")

    if template == "cross_field_math":
        res, l, r = p.get("result_field"), p.get("left_field"), p.get("right_field")
        op = {"+": "plus", "-": "minus", "*": "\u00d7", "/": "\u00f7"}.get(
            str(p.get("operator")), str(p.get("operator")))
        s = f"{res} must equal {l} {op} {r}."
        tol = p.get("tolerance_pct")
        if tol:
            s += f" A difference of up to {_num(tol)}% is accepted."
        return s

    if template == "cross_field_compare":
        op, other = str(p.get("op", "")), p.get("other_field")
        if not other:
            return None
        operator, factor = p.get("operator"), p.get("factor")
        rhs = other
        if operator and factor is not None:
            pct = _pct(factor) if operator == "*" else None
            rhs = (f"{pct} of {other}" if pct
                   else f"{other} {operator} {_num(factor)}")
        rel = "equal" if op == "=" else f"be {_NUM_OPS.get(op, op)}"
        return f"{f} must {rel} {rhs}."

    if template == "cross_field_or_value":
        op, other = str(p.get("op", "")), p.get("other_field")
        bound = str(p.get("bound", "greater")).lower()
        word = "greater" if bound in ("greater", "greatest", "max", "larger") else "lesser"
        operator, factor = p.get("operator"), p.get("factor")
        rhs = other
        if operator and factor is not None:
            pct = _pct(factor) if operator == "*" else None
            rhs = (f"{pct} of {other}" if pct
                   else f"{other} {operator} {_num(factor)}")
        rel = "equal" if op == "=" else f"be {_NUM_OPS.get(op, op)}"
        return (f"{f} must {rel} the {word} of {rhs} "
                f"or {_num(p.get('value'))}.")

    if template == "conditional_value":
        cond = p.get("condition") or {}
        cf, cop, cv = cond.get("field"), str(cond.get("op", "=")), cond.get("value")
        top, tv = str(p.get("op", "=")), p.get("value")
        target = (f"{f} must be {_value(tv)}" if top == "="
                  else f"{f} must be {_NUM_OPS.get(top, top)} {_value(tv)}")
        if cf:
            # NOT lower-cased: `target` opens with the column name, which is a
            # proper identifier the reviewer has to find in the bordereau —
            # de-capitalising "Broker Referral Indicator" to "broker Referral
            # Indicator" names a column that does not exist in the file.
            return f"Whenever {_condition_text(cf, cop, cv)}, {target}."
        return f"{target}."

    if template == "conditional_all":
        conds = [c for c in (p.get("conditions") or []) if isinstance(c, dict)]
        top, tv = str(p.get("op", "=")), p.get("value")
        target = (f"{f} must be {_value(tv)}" if top == "="
                  else f"{f} must be {_NUM_OPS.get(top, top)} {_value(tv)}")
        if conds:
            when = " and ".join(
                _condition_text(c.get("field"), c.get("op", "="), c.get("value"))
                for c in conds)
            return f"Whenever {when}, {target}."
        return f"{target}."

    if template == "zip_state_consistency":
        zf, sf = p.get("zip_field"), p.get("state_field")
        cf = p.get("country_field")
        s = f"{zf} must be a real postal code that belongs to {sf} on the same row."
        if cf:
            s += f" The postal system used is the one for {cf}."
        elif _country_names(p):
            s += f" The postal system used is {_country_names(p)}'s."
        return s

    if template == "state_validity":
        sf, cf = p.get("state_field"), p.get("country_field")
        s = f"{sf} must be a real state, province or region."
        if cf:
            s += f" It is checked against {cf} on the same row."
        elif _country_names(p):
            s += f" It is checked against the regions of {_country_names(p)}."
        return s

    if template == "currency_country_consistency":
        cur, ctry = p.get("currency_field"), p.get("country_field")
        return f"{cur} must be a currency that is legal tender in {ctry}."

    return None


# =====================================================================
# What the reviewer should do
# =====================================================================

_PRESENCE = ("required_field", "conditional_required")
_ENUM = ("value_in_set", "value_not_in_set", "conditional_value", "conditional_all",
         "pattern_check", "state_validity", "zip_state_consistency",
         "currency_country_consistency")
_DATES = ("date_relation", "date_bound", "period_duration")

_DECIDE = ("Use Approve to accept the recommended value, Fix to enter your own, "
           "or Dismiss to keep the row as it is.")


def _is_invariant(template, params):
    """The aggregate_cap shape rule_normalizer uses for "must not change"."""
    return (template == "aggregate_cap"
            and str((params or {}).get("aggregation") or "").lower() == "distinct_count"
            and (params or {}).get("max") in (1, "1", 1.0))


def _how_to_fix(template, params, origin, referral=False):
    f = (params or {}).get("field") or (params or {}).get("required_field") \
        or (params or {}).get("result_field") or (params or {}).get("state_field") \
        or (params or {}).get("zip_field") or "the flagged column"
    if referral:
        # A referral is not a data error, so "correct the value" is the wrong
        # instruction. The last sentence is deliberate: an inverted referral
        # trigger (flagging the rows that should have passed) is a known
        # generation defect, and the reviewer is the one who will spot it.
        return ("Refer these policies to the carrier, or record the referral "
                "approval you already hold, then Dismiss them. If this rule is "
                f"flagging the {f} values you would expect to be fine — and not "
                "the ones needing referral — the trigger is inverted and the "
                "rule should be regenerated rather than actioned row by row.")
    if template in _PRESENCE:
        what = f"Fill in {f} for the flagged rows."
    elif template in _ENUM:
        what = f"Correct {f} on the flagged rows so it matches what the rule allows."
    elif template in _DATES:
        what = f"Correct {f} (or the date it is compared against) on the flagged rows."
    elif template == "uniqueness":
        what = "Remove or re-key the duplicated rows."
    elif _is_invariant(template, params):
        group = " + ".join(g for g in ((params or {}).get("group_by") or []) if g)
        what = (f"Pick the correct {f} and use it on every row of the affected "
                f"{group or 'group'}.")
    elif template == "aggregate_cap":
        what = f"Correct the {f} values that push the group outside the limit."
    else:
        what = f"Correct {f} (or the amounts it is calculated from) on the flagged rows."
    if origin == "contract":
        tail = ("If the bordereau is right and the contract was read wrongly, "
                "Dismiss the rows and raise the rule for review.")
    elif origin == "standard":
        tail = ("If this standard check does not apply to your programme, "
                "an admin can switch it off in the Rule Library.")
    else:
        tail = "If the values are correct as they stand, Dismiss the rows."
    return f"{what} {_DECIDE} {tail}"


# =====================================================================
# Provenance
# =====================================================================

def _clean_source_text(text, keep=None):
    """The clause/library text with the pipeline's provenance markers removed.

    "[Generic rule] Insured City Must Not Be Null — The insured's city must be
    populated." → "The insured's city must be populated."
    A real contract clause has no marker and comes back untouched.
    """
    s = (text or "").strip()
    if not s:
        return None
    for marker in (_GENERIC_MARKER,) + _DERIVED_MARKERS:
        if s.startswith(marker):
            s = s[len(marker):].strip()
            # The library/deriver formats the body as "<rule name> — <logic>";
            # the name is already the card's heading, so drop the repeat.
            parts = re.split(r"\s+[\u2014-]\s+", s, maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                s = parts[1].strip()
            # Only library/derived text is warehouse-flavoured; a real contract
            # clause is left byte-for-byte as the contract wrote it.
            s = humanize_columns(s, keep)
            break
    return s or None


# A source-system column name embedded in prose: two or more lowercase/digit
# words joined by underscores (pol_exp_dt, palms_gross_prem_amt, ceded_pct).
# Deliberately requires an underscore — it never touches ordinary words.
_SNAKE_COLUMN = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def humanize_columns(text, keep=None):
    """Turn source-system column names in free prose into readable words.

    The generic library and older LLM-authored rule descriptions were written
    against the source warehouse ("palms_gross_prem_amt × ceded_pct must equal
    ceded_prem_amt"). That is meaningless to the underwriter reading the
    exception screen, so any surviving snake_case identifier is spelled out.
    """
    if not text:
        return text
    # `keep` holds the column names this rule ACTUALLY references. Some output
    # templates genuinely use snake_case headers (med_incurred, ben_state), and
    # rewriting those produced a card naming the same column two different ways
    # — one of which the reviewer cannot find in the bordereau.
    protected = {str(k).strip().lower() for k in (keep or []) if isinstance(k, str)}
    return _SNAKE_COLUMN.sub(
        lambda m: (m.group(0) if m.group(0).lower() in protected
                   else m.group(0).replace("_", " ")),
        text)


def classify_origin(rule_spec=None, source_text=None):
    """Where a rule came from: 'contract' | 'standard' | 'derived'.

    `rule_source` on the IR is the authoritative marker (written by
    generic_rule_library at generation time and copied verbatim onto the stored
    row); the text prefix is the fallback for rules generated before it existed.
    """
    ir = _spec_dict(rule_spec).get("ir") or {}
    if str(ir.get("rule_source") or "").strip().lower() == "generic_library":
        return "standard"
    s = (source_text or "").strip()
    if s.startswith(_GENERIC_MARKER):
        return "standard"
    if any(s.startswith(m) for m in _DERIVED_MARKERS):
        return "derived"
    return "contract"


_ORIGIN_NOTE = {
    "contract": ("Read from your contract. The wording below is the clause it "
                 "came from."),
    "standard": ("A Kavachio standard bordereau check \u2014 it applies to every "
                 "programme, not just this contract. Manage it in the Rule Library."),
    "derived":  ("Generated automatically from your output template as a "
                 "data-quality check \u2014 it is not written in the contract."),
}


def _is_referral(spec):
    """True when the rule is a REFERRAL TRIGGER rather than a compliance check.

    968 of the estate's rules are referrals: a matching row is not a breach, it
    is business the MGA may only write after referring it to the carrier. The
    reviewer's action is therefore completely different — refer or evidence the
    referral, never "correct the value" — so the two must not read alike.
    """
    if not isinstance(spec, dict):
        return False
    if spec.get("referral") is True:
        return True
    ir = spec.get("ir") or {}
    if ir.get("is_referral") is True:
        return True
    ct = spec.get("canonical_target")
    return isinstance(ct, dict) and ct.get("is_referral") is True


def _origin_label(origin, contract_filename, page):
    if origin == "standard":
        return "Kavachio standard check"
    if origin == "derived":
        return "Automatic data-quality check"
    bits = ["Contract clause"]
    if contract_filename:
        bits.append(str(contract_filename))
    if page:
        bits.append(f"p.{page}")
    return " \u00b7 ".join(bits)


# Templates whose `variation_values` are spellings of the TARGET column's value
# (so they really are "also accepted"), as opposed to spellings of a TRIGGER.
_TARGET_ENUM = ("value_in_set", "value_not_in_set", "conditional_value",
                "conditional_all")

# Templates whose compiler builder actually applies params['scope'] to the SQL.
# The others accept the param and silently ignore it, so describing their scope
# would narrow a rule that in fact runs file-wide. Kept in lockstep with the
# _b_* builders in rule_compiler that call _scope_clause / pass scope onward.
_SCOPE_HONOURING = frozenset({
    "value_in_set", "value_not_in_set", "max_limit", "min_limit", "range_check",
    "pattern_check", "date_bound", "period_duration", "cross_field_math",
    "cross_field_compare", "cross_field_or_value", "zip_state_consistency",
    "state_validity", "currency_country_consistency",
})


def _describe_problem(template, params):
    """"What is wrong with the flagged rows", in one clause.

    Complements `requirement` (what the rule wants) on the exception card. It
    exists because 1,106 stored rules carry the placeholder error_message
    "<rule name>: check failed.", which tells a reviewer nothing at all.
    """
    if not template or not isinstance(params, dict):
        return None
    p = params
    f = p.get("field")
    if template == "required_field":
        return f"These rows have no {f}."
    if template == "conditional_required":
        return f"These rows need {p.get('required_field')} but it is empty."
    if template == "value_in_set":
        return f"These rows have a {f} that is not on the allowed list."
    if template == "value_not_in_set":
        return f"These rows have a {f} the contract excludes."
    if template in ("max_limit", "range_check"):
        return f"These rows have a {f} outside the permitted range."
    if template == "min_limit":
        return f"These rows have a {f} below the permitted minimum."
    if template == "pattern_check":
        return f"These rows have a {f} that is not in the expected format."
    if template == "date_relation":
        return f"On these rows {f} and {p.get('other_field')} are the wrong way round."
    if template == "date_bound":
        return f"These rows have a {f} outside the contract period."
    if template == "period_duration":
        return "These rows cover a period the contract does not allow."
    if template == "uniqueness":
        return "These rows duplicate a value that must be unique."
    if _is_invariant(template, params):
        return f"{f} changes between rows that should all carry the same value."
    if template == "aggregate_cap":
        return f"The combined {f} breaches the limit for these rows."
    if template in ("cross_field_math", "cross_field_compare", "cross_field_or_value"):
        return f"On these rows {f} does not agree with the figures it is derived from."
    if template in ("conditional_value", "conditional_all"):
        return f"These rows meet the condition but {f} does not hold the required value."
    if template == "zip_state_consistency":
        return (f"These rows have a {p.get('zip_field')} that does not belong to "
                f"their {p.get('state_field')}.")
    if template == "state_validity":
        return f"These rows have a {p.get('state_field')} that is not a real region."
    if template == "currency_country_consistency":
        return (f"These rows use a {p.get('currency_field')} that is not legal "
                f"tender in their {p.get('country_field')}.")
    return None


# =====================================================================
# The numeric-format failure — a rule's PRECONDITION, not the rule
# =====================================================================
# Every numeric rule compiles a companion check alongside its own comparison:
# a cell holding text where the rule needs a number cannot be compared, so it is
# flagged in its own right rather than passing silently (see
# rule_compiler._not_numeric_select).
#
# Those rows are a DIFFERENT failure from the one the rule describes, and until
# now they were shown under the rule's identity — a row whose only problem was
# "Program ID is not a number" appeared beneath the heading "Paid Loss Amount
# Must Not Exceed Incurred Loss Amount", explained as "total_paid must be no more
# than total_incurred", and recommended the value "<= total_incurred". Three
# statements, none of them about the row on screen.
#
# So they get their own name and their own three sentences, derived — like
# everything else here — from what actually ran: the column, and the fact that it
# could not be read as a number. Nothing is per-column or per-programme, so this
# reads correctly for any field of any template.

def numeric_format_rule_name(field) -> str:
    """The heading for the rows a rule rejected because the cell is not a number.

    Named after the COLUMN, because that is what the reviewer has to fix, and in
    the same Title Case as the rule names it sits beside in the list."""
    return f"{field} Must Be A Number"


def explain_numeric_format(field, rule_name=None) -> dict:
    """The reviewer-facing explanation of a numeric-format failure.

    `rule_name` (the rule that needed the number) is named in `problem` rather
    than hidden: the reviewer should still be able to see WHICH check could not
    run, without the card claiming that check is what failed.
    """
    f = field or "this column"
    blocked = (f" so “{rule_name}” could not be checked on them"
               if rule_name else " so the check that needs it could not run")
    return {
        "origin": "derived",
        "origin_label": "Automatic data-quality check",
        "origin_note": _ORIGIN_NOTE["derived"],
        "requirement": f"{f} must hold a number.",
        "problem": f"These rows have a {f} that cannot be read as a number,{blocked}.",
        "how_to_fix": (
            f"Correct {f} on the flagged rows so it holds a number, using Fix to "
            "enter the value. If the column is not meant to hold numbers, the "
            "rule that expects a number is bound to the wrong column and should "
            "be regenerated or switched off rather than actioned row by row."
        ),
    }


def apply_numeric_format_identity(e, rule_spec=None) -> bool:
    """Re-title ONE exception in place when it is a numeric-format failure.

    Both exception screens call this and nothing else, so the two can never
    describe the same row differently. It replaces exactly the three things that
    were about the wrong check — the heading, the explanation and the recommended
    value — and leaves everything else (severity, decisions, write-back, the
    rule_id the row belongs to) untouched. Returns True when it applied.

    The `check_kind` it stamps is what lets the review screen list these rows
    under their own heading instead of merging them into the rule's own
    violations, which are a different failure with a different fix.
    """
    if not isinstance(e, dict):
        return False
    field = e.get("field_path") or e.get("field") or e.get("column")
    actual = e.get("actual_value")
    reason = e.get("reason")
    if not classify_numeric_format(rule_spec=rule_spec, field=field,
                                   actual_value=actual, reason=reason):
        return False
    blocked = e.get("rule_name")            # the check that needed the number
    e["check_kind"] = "numeric_format"
    e["root_cause"] = "type_mismatch"
    e["rule_name"] = numeric_format_rule_name(field)
    e["explanation"] = explain_numeric_format(field, blocked)
    # The rule's expected value ("<= total_incurred") is not what this row needs,
    # and Approve would write it into the cell. There is no single right number to
    # suggest — only the reviewer knows it — so the screen asks for one (Fix)
    # rather than offering a wrong one.
    e["recommendation"] = None
    e["recommendation_options"] = None
    e["expected_value"] = None
    return True


def classify_numeric_format(*, rule_spec=None, field=None, actual_value=None,
                            reason=None) -> bool:
    """True when one exception row is a numeric-format failure.

    Two independent readings, because the two exception screens preserve
    different things. The OUTPUT screen keeps the row's own reason, so the row is
    matched against the exact sentence the compiler wrote for it. The UPLOAD
    screen stores only the rule, the column and the value, so the same conclusion
    is reached from those: the rule's template needs `field` to be a number, and
    this row's value is not one. Both are deterministic; neither reads a list of
    column or programme names.
    """
    if reason is not None:
        try:
            from contract_upload_services.rule_compiler import is_not_numeric_reason
        except Exception:
            return False
        return is_not_numeric_reason(reason, field, actual_value)
    if field is None or actual_value is None:
        return False
    if not str(actual_value).strip():
        return False    # the SQL skips blanks too — an empty cell is required_field's
    spec = _spec_dict(rule_spec)
    ir = spec.get("ir") or {}
    params = ir.get("params") if isinstance(ir.get("params"), dict) else {}
    if ir.get("template") not in _NUMERIC_TEMPLATES:
        return False
    # Only the operands the template reads as numbers — a numeric rule's scope or
    # grouping columns are ordinary text and must not be judged by this.
    operands = {params.get(k) for k in _NUMERIC_OPERAND_KEYS}
    if field not in operands:
        return False
    return not _reads_as_number(actual_value)


# Templates whose compiler casts their operands with TRY_CAST(... AS DOUBLE) and
# therefore emit the not-a-number companion check. Kept in lockstep with the
# `_not_numeric_select` callers in rule_compiler.
_NUMERIC_TEMPLATES = frozenset({
    "max_limit", "min_limit", "range_check", "cross_field_math",
    "cross_field_compare", "cross_field_or_value",
})

# The params those templates read as numbers.
_NUMERIC_OPERAND_KEYS = ("field", "other_field", "result_field", "left_field",
                         "right_field")


def _reads_as_number(value) -> bool:
    """Whether a cell would survive the compiler's numeric cast — the same
    tolerances rule_compiler._num applies in SQL: surrounding spaces, thousands
    separators, a currency symbol, a trailing percent, and accounting negatives
    written in parentheses."""
    import unicodedata
    s = str(value).strip()
    if not s:
        return False            # blank is required_field's concern, not this one
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]             # accounting negative
    # The SQL strips whitespace, thousands separators, percent signs and any
    # currency symbol (\p{Z}, \p{Sc}); testing the Unicode CATEGORY is the same
    # rule without naming a single symbol.
    s = "".join(ch for ch in s
                if not (ch.isspace() or ch in ",%"
                        or unicodedata.category(ch) in ("Zs", "Sc")))
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


# =====================================================================
# Entry point
# =====================================================================

def explain_rule(*, rule_spec=None, source_verbatim_text=None,
                 source_page_number=None, contract_filename=None,
                 rule_description=None):
    """The reviewer-facing explanation of one rule. Never raises.

    Everything is derived from the IR that was actually compiled and run, so the
    explanation cannot drift from the check. Keys whose value could not be
    derived are omitted, and the whole thing is best-effort: any failure returns
    {} and the caller keeps its existing display.
    """
    try:
        spec = _spec_dict(rule_spec)
        ir = spec.get("ir") or {}
        template = ir.get("template")
        params = ir.get("params") if isinstance(ir.get("params"), dict) else {}

        # The column names this rule really references — never reworded below.
        own_columns = [v for v in params.values() if isinstance(v, str)]
        own_columns += [v for k in ("fields", "group_by") for v in (params.get(k) or [])
                        if isinstance(v, str)]

        origin = classify_origin(spec, source_verbatim_text)
        requirement = describe_requirement(template, params)
        # Legacy (pre-IR) rules have no template to read; their stored
        # description is the only wording available, so fall back to it —
        # humanized, because much of it was written against the warehouse.
        if not requirement:
            requirement = humanize_columns(
                _clean_source_text(ir.get("rule_description") or rule_description,
                                   own_columns), own_columns)

        referral = _is_referral(spec)
        out = {
            "origin": origin,
            "origin_label": _origin_label(origin, contract_filename,
                                          source_page_number),
            "origin_note": _ORIGIN_NOTE[origin],
        }
        if referral:
            out["is_referral"] = True
            out["kind_label"] = "Referral trigger"
        if requirement:
            out["requirement"] = requirement
        # The problem sentence always describes the rows the SQL ACTUALLY flags,
        # referral or not. An earlier version substituted "these rows match a
        # condition that must be referred", which for a referral compiled as
        # value_in_set(AK, HI) named the exact opposite row set from the one on
        # screen — two sentences on one card contradicting each other. The
        # referral framing belongs on the chip and the how-to-fix, which say what
        # the reviewer should DO, and do not re-describe which rows fired.
        problem = _describe_problem(template, params)
        if problem:
            out["problem"] = problem
        # Only claim a row restriction for templates whose COMPILER actually
        # applies one. Eight builders ignore params['scope'] entirely, and 8
        # stored aggregate_cap rules carry a scope their SQL has no WHERE for —
        # saying "applies only to rows where Program Name is PN0052" there would
        # tell the reviewer a real breach on another programme was a bug.
        if template in _SCOPE_HONOURING:
            scope = _scope_text(params.get("scope"))
            if scope:
                out["applies_to"] = scope
        # Enum rules accept extra surface spellings of the ENFORCED value; a
        # reviewer staring at "PALMS SPECIALTY" vs "Palms Specialty Inc" needs to
        # know. Restricted to the templates where variation_values describe the
        # TARGET column — on conditional_required they describe the TRIGGER, and
        # advertising those as accepted values would be simply wrong.
        if template in _TARGET_ENUM:
            variations = params.get("variation_values")
            if isinstance(variations, dict):
                variations = [v for lst in variations.values()
                              for v in (lst if isinstance(lst, (list, tuple)) else [lst])]
            known = {str(v).strip().lower() for v in
                     (params.get("allowed") or []) + (params.get("excluded") or [])
                     + ([params["value"]] if params.get("value") is not None else [])}
            extra = [v for v in (variations or [])
                     if str(v).strip() and str(v).strip().lower() not in known]
            if extra:
                out["accepts_also"] = _quote_list(extra, limit=8)
        text = _clean_source_text(source_verbatim_text, own_columns)
        if text:
            out["source_text"] = text
        out["how_to_fix"] = _how_to_fix(template, params, origin, referral)

        # NOTHING TO EXPLAIN → explain nothing. Many exceptions have no rule
        # behind them at all (structural type checks carry rule_id NULL, so the
        # LEFT JOIN yields no spec and no clause). Returning the generic
        # provenance + how-to-fix for those would REPLACE the exception's own
        # message with a "Contract clause" chip it never came from — strictly
        # worse than the display it superseded. The caller keeps its own
        # fallback when this is empty.
        if not (requirement or problem or text):
            return {}
        return out
    except Exception:
        return {}
