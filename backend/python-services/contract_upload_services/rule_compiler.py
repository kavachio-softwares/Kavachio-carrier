"""
rule_compiler.py
────────────────
Deterministic IR → DuckDB SQL compiler.

`compile_ir(ir, field_to_sheet)` turns a validated IR (see `rule_ir`) into a
single read-only SELECT that returns the *violating* rows (zero rows = the data
is compliant). Same IR + same schema → byte-identical SQL — that is where the
determinism comes from, NOT from the LLM behaving the same way twice.

The emitted query matches the column contract the DuckDB runtime already expects
(`duckdb_validation.execute_rule` / `dry_run`):
    row_id, sheet, field, reason            (required)
    actual_value, policy_number             (optional)

Values extracted by the LLM are written as SQL literals with strict escaping and
numeric coercion done in Python here (never taken verbatim from the model into
SQL structure), so the LLM never authors SQL and there is no injection surface.

All BDX columns are loaded as VARCHAR by the runtime, so numbers go through a
TRY_CAST that first strips commas / currency symbols, and dates through
TRY_CAST(... AS DATE).
"""

from __future__ import annotations

import os as _os
import re as _re

from contract_upload_services.rule_ir import TEMPLATE_CATALOG


class CompileError(Exception):
    """Raised when an IR cannot be turned into a runnable query."""


# ENUM MATCHING IS EXACT — a cell is one of a rule's values or it is not.
#
# It used to be scored with jaro-winkler on a 3-tier match/warn/miss decision, and
# string distance cannot tell "a different spelling of this value" from "a
# different value that happens to be spelt similarly". Both readings are always
# available and the metric has no way to prefer the right one: 'NM' (New Mexico)
# scores 0.91 against the excluded 'NMI' (Northern Mariana Islands) and every New
# Mexico policy in the book was reported as writing in an excluded territory.
# Raising the threshold does not fix it — the false pair scores HIGHER than
# genuine variants like 'nite club' ≈ 'night club' (0.89) — and neither did the
# per-case guards that preceded this (a minimum length for the warn band, a
# shared-word residue re-score): each removed one wrong pair and left the metric
# deciding value identity for all the others.
#
# So identity is decided ONLY by things that carry meaning:
#   • normalization — case, spacing and punctuation are not differences;
#   • the curated VOCABULARY — a known synonym maps onto one canonical token
#     ("United States"/"USA"/"US" → 'us'), which is where real semantic
#     equivalence belongs (see _vocab_cell_expr);
#   • the rule's own `variation_values` — the alternate spellings a bordereau
#     actually uses, seeded at generation, reconciled against the uploaded data
#     and editable by an admin, i.e. reviewable rather than inferred per row.
# A spelling nobody has recorded no longer matches by resemblance; it is added to
# the rule, once, and then matches everywhere.


# ---------------------------------------------------------------------
# Low-level SQL helpers (deterministic, injection-safe)
# ---------------------------------------------------------------------

def _q(name: str) -> str:
    """Quote an identifier (table/column).

    A blank or non-string identifier means the IR carried something that is not a
    column name where a column was required (e.g. a model that wrote a FACTOR into
    `right_field`: 0.28 instead of the column it multiplies). That is a malformed
    rule, and the caller already knows what to do with one — `CompileError` routes
    it to the review queue. Raising here, at the single choke point every builder
    quotes its columns through, is what keeps that a ONE-RULE failure: letting the
    bad value reach string concatenation deeper in a builder raises TypeError,
    which no caller catches, so a single bad candidate aborts the whole contract
    upload."""
    if not isinstance(name, str) or not name.strip():
        raise CompileError(f"expected a column/table name, got {name!r}")
    return '"' + name.replace('"', '""') + '"'


def _lit(value) -> str:
    """Quote a string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _num_lit(value) -> str:
    """Coerce a numeric literal in Python (rejects junk → CompileError)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise CompileError(f"expected a number, got {value!r}")
    return repr(int(f)) if f.is_integer() else repr(f)


# Characters that DECORATE a number without changing what it is worth: the
# thousands separator, a percent sign, any kind of SPACE and any CURRENCY symbol.
# The last two are matched by Unicode CATEGORY rather than by a list somebody has
# to keep extending — `\p{Sc}` covers $, £, €, ¥, ₹ and every other currency sign,
# and `\p{Z}` covers the non-breaking and narrow spaces Excel writes as the
# thousands separator in several locales (the engine's own `\s` is ASCII-only, so
# "1<NBSP>000" would otherwise read as text).
_NUM_DECORATION = r"[\s,%]|\p{Z}|\p{Sc}"


def numeric_expr(expr: str) -> str:
    """A VARCHAR SQL EXPRESSION → DOUBLE, reading an amount the way a person does.

    Strips the decoration above, then honours the ACCOUNTING NEGATIVE: an amount
    wrapped in parentheses is negative — "($4,380.00)" is -4380.00. That is not an
    oddity; it is what Excel's built-in Currency and Accounting formats produce for
    a negative number, so a bordereau exported from them spells every credit,
    return premium and cancellation that way. Reading those as "not a number"
    raised an exception on a perfectly valid cell AND silently left the row out of
    every numeric rule that touches the column.

    The parentheses are read as a sign ONLY when what they wrap is itself a number,
    so "(abc)", "N/A" and "#N/A" still fail the cast and stay reportable — the
    check keeps its teeth. Shared with the type-check pass
    (`duckdb_validation._typecheck_num_expr`) so a value the rules can compute with
    is never reported as a malformed amount, and vice versa."""
    s = f"regexp_replace(TRIM({expr}), '{_NUM_DECORATION}', '', 'g')"
    negated = f"{s} LIKE '(%)'"
    core = f"CASE WHEN {negated} THEN SUBSTR({s}, 2, LENGTH({s}) - 2) ELSE {s} END"
    sign = f"CASE WHEN {negated} THEN -1 ELSE 1 END"
    return f"({sign} * TRY_CAST({core} AS DOUBLE))"


def _num(col: str) -> str:
    """VARCHAR column → DOUBLE. See numeric_expr for what counts as a number."""
    return numeric_expr(_q(col))


# Money is reported to CENTS, so a numeric formula rule compares to cents: both
# the reported cell and the computed expectation are rounded to this many decimal
# places BEFORE the tolerance is applied. Digits past that point are never
# something a user reports on or can act on — they are storage noise
# (95.74203999999999 for a cell that reads 95.742) or the tail of a rate written
# to fewer decimals than the value was actually computed with. Rounding first
# means an exception is only ever raised on a difference visible in the BDX.
#
# This does NOT replace `tolerance`; it runs before it, so a rule is never
# STRICTER than it was (a pair that passed on raw values still passes rounded).
# Env-overridable fleet-wide; a single rule can override with a `decimals` param.
NUMERIC_MATCH_DECIMALS = int(_os.getenv("KAVACHIO_NUMERIC_MATCH_DECIMALS", "2"))


def _round(expr: str, p: dict | None = None) -> str:
    """Round a numeric SQL expression to the comparison precision (see
    NUMERIC_MATCH_DECIMALS). A rule may carry its own `decimals` to compare at a
    different precision — e.g. 4 for a rate column, 0 for whole units."""
    dec = NUMERIC_MATCH_DECIMALS
    raw = (p or {}).get("decimals")
    if raw is not None:
        try:
            dec = int(raw)
        except (TypeError, ValueError):
            raise CompileError(f"decimals must be an integer, got {raw!r}")
        if dec < 0:
            raise CompileError(f"decimals cannot be negative, got {raw!r}")
    return f"ROUND({expr}, {dec})"


def _abs_dev(left: str, right: str, p: dict | None = None) -> str:
    """|left - right| measured at the comparison precision.

    Both sides are rounded, AND SO IS THE DIFFERENCE. The second rounding is not
    redundant: subtracting two already-rounded doubles reintroduces a float tail
    (111.23 - 111.22 = 0.010000000000005116 in IEEE754), and that tail is enough
    to push a pair sitting exactly ON the tolerance back over it — the rounding
    would then not do what it says on rows landing on a cent boundary."""
    return _round(f"ABS({_round(left, p)} - {_round(right, p)})", p)


def _date(col: str, alias: str | None = None) -> str:
    """VARCHAR → DATE, tolerant of the date spellings BDX files actually use.

    Every BDX column is loaded as VARCHAR, so a date cell may be ISO
    ('2026-01-03'), compact 'YYYYMMDD' ('20260103'), slash 'MM/DD/YYYY', or a
    dash-separated day-month-year ('07-12-2026'). Bare TRY_CAST(... AS DATE)
    only accepts ISO and silently returns NULL for the others — which makes a
    date rule SKIP those rows (a dead check). COALESCE across the common
    spellings so a date rule evaluates every row whatever the source system's
    format. Generic: no per-file / per-column / per-contract assumption — just
    the ordered list of formats seen in practice, ISO first so already-correct
    data is untouched.

    The dash format is parsed DAY-first ('%d-%m-%Y'), not month-first: a
    dash-separated date in real BDX data is the international/ISO-adjacent
    convention, and day-first also strictly subsumes month-first for THIS
    COALESCE's purposes — whenever the day part exceeds 12 (unambiguous),
    month-first fails outright (dead check again), while day-first always
    succeeds; whenever both parts are <=12 (ambiguous), the two disagree, and a
    single column must resolve every row the same way, so the parse cannot
    depend on which particular row happens to be ambiguous.

    A cell may also carry a TIME OF DAY after the date — an Excel date column
    exported as text reads '4/18/2019 12:00:00 AM', and only ISO datetimes are
    understood by TRY_CAST. Every spelling above is therefore tried a second time
    against the cell with a trailing time stripped, so the same date is read
    whether or not the source wrote a midnight timestamp beside it. Stripping the
    time (rather than enumerating every date x time combination) keeps this
    generic — nothing new is assumed about how a date may look. The same
    tolerance is in the type-check's parser (duckdb_validation._date_parse_expr),
    so a cell the type check calls a valid date is one the RULES can also read:
    without it a valid column was reported as malformed AND its date rules
    silently skipped every row.

    `alias` qualifies the column (e.g. 't') so a DATE-operator SCOPE predicate in
    the aliased enum query resolves to the right table."""
    inner = f"{alias}.{_q(col)}" if alias else _q(col)
    c = f"TRIM({inner})"
    # date + optional time of day, 12- or 24-hour, seconds/fraction optional.
    date_only = (f"regexp_extract({c}, "
                 r"'^(.*?)[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*(?:[AaPp]\.?[Mm]\.?)?$'"
                 ", 1)")

    def _spellings(x):
        return (
            f"TRY_CAST({x} AS DATE), "                              # ISO 2026-01-03
            f"TRY_CAST({x} AS TIMESTAMP)::DATE, "                   # ISO datetime
            f"TRY_CAST(TRY_STRPTIME({x}, '%Y%m%d') AS DATE), "      # compact 20260103
            f"TRY_CAST(TRY_STRPTIME({x}, '%m/%d/%Y') AS DATE), "    # slash 01/03/2026 (US, month-first)
            f"TRY_CAST(TRY_STRPTIME({x}, '%d-%m-%Y') AS DATE)"      # dash 07-12-2026 (day-first)
        )

    # A non-matching cell yields '' from regexp_extract, which parses to NULL, so
    # a plain date simply falls through to the first group.
    return "COALESCE(" + _spellings(c) + ", " + _spellings(date_only) + ")"


def _present(col: str) -> str:
    c = _q(col)
    return f"({c} IS NOT NULL AND TRIM({c}) <> '')"


def _empty(col: str) -> str:
    c = _q(col)
    return f"({c} IS NULL OR TRIM({c}) = '')"


_CMP_OPS = {"<", "<=", ">", ">=", "=", "!=", "<>"}


# ---------------------------------------------------------------------
# Enum matching — normalize, then compare for EQUALITY. Normalization strips case
# and every non-alphanumeric character (handled identically in Python for the
# literal and in SQL for the column), so the contract's wording matches the
# sheet's spacing and punctuation:
#   "U.S. Virgin Islands"  (contract)  ==  "US Virgin Islands"  (sheet)
#   "migrant real estate"  (contract)  ==  "Migrant Real-Estate" (sheet)
# Anything beyond that is a DIFFERENT WORD, and whether two different words name
# the same thing is not a question about their letters: true synonyms belong in
# the curated vocabulary table, and a bordereau's own spellings on the rule's
# `variation_values`. See the header note on why this is not scored.
# ---------------------------------------------------------------------

def _norm_py(value) -> str:
    """Compile-time normalization of an enum literal (mirrors _norm_sql)."""
    return _re.sub(r"[^a-z0-9]", "", str(value).lower())


def _norm_sql(col: str) -> str:
    """Runtime normalization of a column value (mirrors _norm_py)."""
    return f"regexp_replace(lower(trim({_q(col)})), '[^a-z0-9]', '', 'g')"


def _vocab_cell_expr(field: str, alias: str | None = None,
                     relevant_canon: set | None = None) -> str:
    """The BDX cell, string-normalized AND THEN mapped to its vocab canonical
    token when known — so "United States"/"USA"/"US" all collapse to the same
    token the rule literal does (symmetric normalization). When the field has no
    vocab class, this is just the plain string-normalization. This is the fix for
    string-distance failing on real synonyms: known synonyms now match EXACTLY.

    `relevant_canon`, when given, restricts the synonym→canonical CASE to ONLY the
    canonical tokens this rule actually uses (derived from its own values). The
    vocab class can hold many tokens (territory = us/uk/ca …); without this, every
    rule's SQL carries the WHOLE class even when the contract names one country.
    Trimming keeps the query to the contract's own values without touching the
    shared vocabulary seed."""
    from contract_upload_services.vocabulary import class_form_map
    col = f"{alias}.{_q(field)}" if alias else _q(field)
    base = f"regexp_replace(lower(trim({col})), '[^a-z0-9]', '', 'g')"
    fmap = class_form_map(field)
    if relevant_canon is not None:
        fmap = {form: canon for form, canon in fmap.items() if canon in relevant_canon}
    if not fmap:
        return base
    whens = " ".join(f"WHEN {base} = {_lit(form)} THEN {_lit(canon)}"
                     for form, canon in fmap.items())
    return f"CASE {whens} ELSE {base} END"


def _scope_pred(k, v, alias: str | None = None) -> str | None:
    """One row-scope predicate for column `k` filtered by value `v`. Handles every
    shape the mapper emits:
      • list/tuple/set        → IN (...)          (case-insensitive text)
      • {"allowed":[...], "variation_values": {..}|[..]}  → IN over the allowed
                                                   values AND every variation spelling
                                                   (a grouped set-membership scope,
                                                   e.g. the reinsurers that share a
                                                   $25M limit). "excluded" → NOT IN.
      • {"op":OP,"value":V}   → col OP V          (an OPERATOR-OBJECT: =,!= are
                                                   case-insensitive text; <,<=,>,>=
                                                   numeric; value may itself be a
                                                   list → IN / NOT IN)
      • scalar                → col = V           (case-insensitive text)
    `alias` qualifies the column (e.g. 't') for the aliased enum query. Returns
    None when there is nothing to filter on. Without these object branches such a
    scope was dropped (over-broad rule) or str()'d into a dead literal."""
    col = f"{alias}.{_q(k)}" if alias else _q(k)
    txt = f"LOWER(TRIM({col}))"

    def _inlist(items, negate=False):
        vals, seen = [], set()
        for x in items:
            s = str(x).strip().lower()
            if s and s not in seen:
                seen.add(s)
                vals.append(s)
        if not vals:
            return None
        return f"{txt} {'NOT IN' if negate else 'IN'} ({', '.join(_lit(x) for x in vals)})"

    def _flatten_variations(vv):
        """variation_values may be a {value: [spellings]} map or a flat list."""
        out = []
        if isinstance(vv, dict):
            for lst in vv.values():
                out.extend(lst if isinstance(lst, (list, tuple, set)) else [lst])
        elif isinstance(vv, (list, tuple, set)):
            out.extend(vv)
        return out

    if isinstance(v, (list, tuple, set)):
        return _inlist(v)
    if isinstance(v, dict):
        # Set-membership scope: allowed/excluded list (+ optional variation spellings
        # so the BDX's own wording still matches). A cell equal to ANY listed form
        # passes (allowed) / fails (excluded).
        if isinstance(v.get("allowed"), (list, tuple, set)) or \
                isinstance(v.get("excluded"), (list, tuple, set)):
            base = list(v.get("allowed") or v.get("excluded") or [])
            forms = base + _flatten_variations(v.get("variation_values"))
            return _inlist(forms, negate=isinstance(v.get("excluded"), (list, tuple, set)))
        op = str(v.get("op", "=")).strip().lower()
        # DATE-operator scope: {"op": ">=", "date": "2024-01-15"} — a TEMPORAL
        # applicability window (e.g. "effective no later than Jan 15, 2024, use X
        # paper"). The date lives under a "date" key (not "value"), so without this
        # branch the predicate was silently dropped and the rule applied to EVERY
        # row regardless of date. Compare via the same multi-format date parse the
        # date builders use so a US / compact / ISO date all evaluate.
        _dv = v.get("date")
        if _dv is not None and str(_dv).strip() != "" and \
                op in ("<", "<=", ">", ">=", "=", "==", "!=", "<>"):
            dop = "<>" if op in ("!=", "<>") else ("=" if op == "==" else op)
            dexpr = _date(k, alias)
            return (f"({dexpr} IS NOT NULL AND {dexpr} {dop} "
                    f"TRY_CAST({_lit(str(_dv).strip())} AS DATE))")
        # CONTAINS / substring scope: {"op": "contains", "value": "wrap-up", ...}
        # (+ any variation spellings) — a keyword filter (e.g. only wrap-up
        # construction projects). Without this it fell through to equality and only
        # matched an EXACT cell, silently under-scoping the rule. Case-insensitive
        # LIKE over the value and every variation spelling.
        if op in ("contains", "includes", "like", "has", "startswith", "endswith"):
            forms, seen = [], set()
            for x in [v.get("value"), *_flatten_variations(v.get("variation_values"))]:
                s = str(x).strip().lower()
                if s and s not in seen:
                    seen.add(s)
                    forms.append(s)
            if not forms:
                return None
            pat = {"startswith": "{}%", "endswith": "%{}"}.get(op, "%{}%")
            likes = [f"{txt} LIKE {_lit(pat.format(x))}" for x in forms]
            return "(" + " OR ".join(likes) + ")"
        val = v.get("value")
        if isinstance(val, (list, tuple, set)):
            return _inlist(val, negate=op in ("!=", "<>", "not in", "not_in", "notin"))
        if val is None or str(val).strip() == "":
            return None
        if op in ("=", "=="):
            return f"{txt} = {_lit(str(val).strip().lower())}"
        if op in ("!=", "<>", "not"):
            return f"{txt} <> {_lit(str(val).strip().lower())}"
        if op in ("<", "<=", ">", ">="):
            num = (f"TRY_CAST(REPLACE(REPLACE(REPLACE({col}, ',', ''), '$', ''), "
                   f"' ', '') AS DOUBLE)")
            return f"({num} IS NOT NULL AND {num} {op} {_num_lit(val)})"
        return f"{txt} = {_lit(str(val).strip().lower())}"   # unknown op → equality
    if str(v).strip() == "":
        return None
    return f"{txt} = {_lit(str(v).strip().lower())}"


# Scope keys whose value is a GROUP of per-field filters combined with OR instead
# of the default AND — e.g. "the limit applies when Reinsurer NAME matches OR
# Reinsurer PAPER matches" (the same reinsurer may be reported under either
# column). The group value is a dict {field: scope-value, ...} or a list of such
# single-field dicts.
_OR_SCOPE_KEYS = ("any_of", "or", "$or", "either")


def _scope_or_preds(group, alias=None):
    """Render the OR-members of an any_of group → list of predicates."""
    dicts = group if isinstance(group, (list, tuple)) else [group]
    preds = []
    for d in dicts:
        if isinstance(d, dict):
            for kk, vv in d.items():
                p = _scope_pred(kk, vv, alias)
                if p:
                    preds.append(p)
    return preds


def _scope_parts(scope: dict, alias=None) -> list[str]:
    """Render a scope dict into AND-joined SQL parts. A normal `{field: value}`
    entry is one predicate; an `any_of` entry becomes a parenthesised OR group."""
    parts = []
    for k, v in scope.items():
        if str(k).strip().lower() in _OR_SCOPE_KEYS:
            ors = _scope_or_preds(v, alias)
            if ors:
                parts.append("(" + " OR ".join(ors) + ")")
        else:
            p = _scope_pred(k, v, alias)
            if p:
                parts.append(p)
    return parts


def _scope_clause_alias(scope: dict, alias: str) -> str:
    """Like _scope_clause but qualifies columns with a table alias (for the
    aliased enum query below)."""
    if not isinstance(scope, dict) or not scope:
        return ""
    return " AND ".join(_scope_parts(scope, alias))


def _enum_match_rows(values: list, field: str | None = None,
                     variation_values: list | None = None) -> list:
    """(display, match_token) pairs for a rule's values, deduped by token.

    `display` keeps the contract's ORIGINAL wording for the human-readable message
    ("United States of America"); `match_token` is the vocab CANONICAL token ("us")
    so it lines up with the identically-canonicalized cell (see _vocab_cell_expr).

    `variation_values` (optional) are extra surface spellings of the same values
    (AI-recommended, data-reconciled or admin-added, e.g. "Specialty" for "Palms
    Specialty Insurance Company Inc.") folded into the SAME match set, so a cell
    spelt any of those ways matches too. Rows are DEDUPED by canonical token, so a
    variation that collapses onto a token already present adds nothing."""
    from contract_upload_services.vocabulary import canonical_token
    rows, seen = [], set()
    for v in list(values) + list(variation_values or []):
        tok = canonical_token(v, field)
        if not tok or tok in seen:
            continue
        seen.add(tok)
        rows.append((str(v), tok))
    if not rows:
        raise CompileError("enum has no usable values after normalization")
    return rows


def _enum_match_select(sheet: str, field: str, values: list, *,
                       polarity: str, scope: dict | None,
                       variation_values: list | None = None) -> str:
    """Build the enum query: the cell must (not) BE one of the rule's values.

      polarity='exclude' (value_not_in_set): the cell IS one of them     → violation
      polarity='include' (value_in_set):     the cell is NOT any of them → violation

    A cell matches a value when the two are EQUAL after the shared normalization
    and the vocabulary's synonym mapping — see the module header for why identity
    is decided that way and not by string distance. There is therefore no third
    "they might be the same" outcome and no `zone` column: every row this returns
    is a violation and keeps the rule's own severity.
    """
    from contract_upload_services.vocabulary import canonical_token
    qf = _q(field)
    # Normalize the cell ONLY through the canonical tokens this rule references
    # (its own values) so the SQL carries the contract's values, not the whole
    # vocab class (territory us/uk/ca → just us when the contract says US).
    _match_vals = list(values) + list(variation_values or [])
    relevant = {t for t in (canonical_token(v, field) for v in _match_vals) if t}
    nct = _vocab_cell_expr(field, "t", relevant_canon=relevant)
    # (display, match_token) pairs. The DISPLAY is carried into the query even
    # though only the token is compared: the compiled SQL is what a reviewer,
    # a support engineer and the variation tooling read to see which spellings a
    # rule accepts, and a list of normalized tokens answers that question far worse
    # than the wording the contract used.
    rows = _enum_match_rows(values, field, variation_values)
    pairs = ", ".join(f"({_lit(d)}, {_lit(tok)})" for d, tok in rows)
    matches = (f"SELECT 1 FROM (VALUES {pairs}) AS x(v, vn) WHERE x.vn = {nct}")

    if polarity == "exclude":
        # WHICH of the rule's spellings the cell turned out to be, quoted as the
        # rule holds it — so the reviewer sees the excluded value they can look up,
        # not just the cell they are already looking at. At most one row by
        # construction, because the tokens are deduped.
        matched = (f"(SELECT x.v FROM (VALUES {pairs}) AS x(v, vn) "
                   f"WHERE x.vn = {nct} LIMIT 1)")
        match_cond = f"EXISTS ({matches})"
        reason = (f"{_lit(field)} || ' matches excluded value '''"
                  f" || COALESCE({matched}, t.{qf}) || ''''")
    elif polarity == "include":
        match_cond = f"NOT EXISTS ({matches})"
        reason = (f"{_lit(field)} || ' value ''' || t.{qf} || "
                  f"''' is not in the allowed set'")
    else:
        raise CompileError(f"enum polarity must be include|exclude, got {polarity!r}")

    where = f"(t.{qf} IS NOT NULL AND TRIM(t.{qf}) <> '') AND {match_cond}"
    # A scope filter on the rule's OWN field is self-contradictory for a set rule
    # ("exclude Puerto Rico WHERE Insured Country = 'us'" can never match) — drop
    # it so the check actually evaluates instead of silently flagging nothing.
    scope = {k: v for k, v in (scope or {}).items()
             if str(k).strip().lower() != str(field).strip().lower()}
    scope_sql = _scope_clause_alias(scope, "t")
    if scope_sql:
        where = f"{where} AND {scope_sql}"

    return (
        f"SELECT t.__rowid AS row_id, {_lit(sheet)} AS sheet, {_lit(field)} AS field, "
        f"{reason} AS reason, t.{qf} AS actual_value "
        f"FROM {_q(sheet)} AS t WHERE {where}"
    )


def _scope_clause(scope: dict) -> str:
    """Case-insensitive row-scope filter, e.g. {Coverage Type: CGL}. Both sides are
    lower/trimmed so a vocab-normalized rule value ('cgl') still binds to the BDX
    value ('CGL'). A scope value may be a LIST → an IN-filter (ONE grouped rule for
    several entities sharing a constraint), or an OPERATOR-OBJECT {op,value} →
    col OP value. An `any_of` key ORs its member filters (Reinsurer Name matches OR
    Reinsurer Paper matches). See _scope_pred."""
    if not isinstance(scope, dict) or not scope:
        return ""
    return " AND ".join(_scope_parts(scope))


def _select(sheet: str, field: str, reason: str, where: str,
            actual_col: str | None = None, zone_expr: str | None = None) -> str:
    actual = _q(actual_col) if actual_col else "NULL"
    # `zone_expr` (a SQL scalar → 'violation' | 'warning') opts a builder into the
    # per-row severity split execute_rule already understands (see _b_enum_set).
    # When None, no zone column is emitted and the row keeps the rule's severity.
    zone = f', {zone_expr} AS zone' if zone_expr else ''
    return (
        f'SELECT __rowid AS row_id, {_lit(sheet)} AS sheet, {_lit(field)} AS field, '
        f'{_lit(reason)} AS reason, {actual} AS actual_value{zone} '
        f'FROM {_q(sheet)} WHERE {where}'
    )


# The sentence `_not_numeric_select` builds, as a format — and the ONE definition
# of it. The validation run has to tell these rows apart from the rule's own
# violations (they are a different failure, and the rule's name, explanation and
# recommended value all describe the wrong thing for them — see
# rule_explainer.explain_numeric_format), and the only thing that reaches it is
# the reason string. Recognising them by re-deriving the same sentence from the
# row's own field and value keeps the writer and the reader in lockstep: change
# the wording here and both sides move together.
_NOT_NUMERIC_REASON = "{field} must be a number, but found {value}."


def not_numeric_reason(field, value) -> str:
    """The reason `_not_numeric_select` puts on a row whose cell is not a number."""
    return _NOT_NUMERIC_REASON.format(field=field, value=value)


def is_not_numeric_reason(reason, field, value) -> bool:
    """True when `reason` is exactly the not-a-number sentence for this cell."""
    if not reason or field is None or value is None:
        return False
    return str(reason).strip() == not_numeric_reason(field, value)


def _not_numeric_select(sheet: str, field: str, scope: dict | None = None,
                        zone_expr: str | None = None) -> str:
    """Companion SELECT: `field` holds a NON-BLANK value that fails the numeric
    cast a comparison needs (e.g. "ABC" where a number is required).

    Every numeric-comparison builder (max_limit, min_limit, range_check,
    cross_field_math, cross_field_compare, cross_field_or_value) has to guard its
    comparison with `_num(field) IS NOT NULL` — you cannot compare a value that
    failed to cast. But that guard also means a genuinely non-numeric cell
    satisfies NONE of the WHERE clause and is silently never flagged, which is
    backwards: it is the clearest possible violation of "this must be a number".
    UNION ALL this alongside the template's own SELECT so that case surfaces as
    its own exception instead of passing silently. Scoped to a non-blank cell via
    `_present` — a blank is `required_field`'s concern, so the two never overlap.
    """
    where = _and(f"{_num(field)} IS NULL", _present(field), _scope_clause(scope))
    # Built from the shared format so the runtime can recognise these rows by
    # re-deriving the identical sentence (see is_not_numeric_reason). The cell's
    # value is the only run-time part, so the sentence splits around it.
    head, tail = not_numeric_reason(field, "\x00").split("\x00", 1)
    reason = f"{_lit(head)} || {_q(field)} || {_lit(tail)}"
    # A non-numeric value where a number is required is always a hard violation, so
    # when a caller UNIONs this alongside a zoned select it passes zone_expr
    # "'violation'" to keep the column set aligned (see _b_cross_field_math).
    zone = f', {zone_expr} AS zone' if zone_expr else ''
    return (
        f'SELECT __rowid AS row_id, {_lit(sheet)} AS sheet, {_lit(field)} AS field, '
        f'{reason} AS reason, {_q(field)} AS actual_value{zone} '
        f'FROM {_q(sheet)} WHERE {where}'
    )


# ---------------------------------------------------------------------
# Per-template builders.  Each returns a SQL string given (sheet, params).
# The violation condition is encoded directly; polarity comes from the
# template name, so there is no operator to invert by accident.
# ---------------------------------------------------------------------

def _b_required_field(sheet, p):
    f = p["field"]
    return _select(sheet, f, f"{f} is required but empty", _empty(f), f)


def _b_value_in_set(sheet, p):
    f = p["field"]
    allowed = p.get("allowed") or []
    if not isinstance(allowed, list) or not allowed:
        raise CompileError("value_in_set needs a non-empty 'allowed' list")
    inc = _enum_match_select(sheet, f, allowed, polarity="include",
                             scope=p.get("scope"),
                             variation_values=p.get("variation_values"))
    # Optional `excluded` makes this ONE rule for "must be in allowed, EXCLUDING …"
    # (e.g. Territory: United States *excluding* Puerto Rico / USVI). A row is
    # flagged if it is NOT in the allowed set OR IS in the excluded set.
    excluded = p.get("excluded") or []
    if isinstance(excluded, list) and excluded:
        exc = _enum_match_select(sheet, f, excluded, polarity="exclude",
                                 scope=p.get("scope"))
        return f"{inc}\nUNION ALL\n{exc}"
    return inc


def _b_value_not_in_set(sheet, p):
    f = p["field"]
    excluded = p.get("excluded") or []
    if not isinstance(excluded, list) or not excluded:
        raise CompileError("value_not_in_set needs a non-empty 'excluded' list")
    return _enum_match_select(sheet, f, excluded, polarity="exclude",
                              scope=p.get("scope"),
                              variation_values=p.get("variation_values"))


def _b_max_limit(sheet, p):
    f = p["field"]
    mx = _num_lit(p["max"])
    where = _and(f"{_num(f)} IS NOT NULL AND {_num(f)} > {mx}",
                 _scope_clause(p.get("scope")))
    return (_select(sheet, f, f"{f} exceeds maximum {mx}", where, f)
            + "\nUNION ALL\n" + _not_numeric_select(sheet, f, p.get("scope")))


def _b_min_limit(sheet, p):
    f = p["field"]
    mn = _num_lit(p["min"])
    where = _and(f"{_num(f)} IS NOT NULL AND {_num(f)} < {mn}",
                 _scope_clause(p.get("scope")))
    return (_select(sheet, f, f"{f} below minimum {mn}", where, f)
            + "\nUNION ALL\n" + _not_numeric_select(sheet, f, p.get("scope")))


def _b_range_check(sheet, p):
    f = p["field"]
    mn, mx = _num_lit(p["min"]), _num_lit(p["max"])
    # min == max means "must EQUAL exactly X" — a fixed required value such as a
    # commission schedule of 23.5% (→ 0.235), a fixed fee, or an exact share.
    # Compile it as an exact-equality check (violation = present AND not == X).
    # The catalog has no numeric-equals template, and *limits* are steered to
    # max_limit/min_limit by the mapper prompt, so a min==max range here is an
    # intentional exact value, not a misread ceiling. (Numeric scale is
    # normalized upstream in rule_normalizer before this runs.)
    not_numeric = _not_numeric_select(sheet, f, p.get("scope"))
    if mn == mx:
        where = _and(f"{_num(f)} IS NOT NULL AND {_num(f)} <> {mn}",
                     _scope_clause(p.get("scope")))
        return (_select(sheet, f, f"{f} must equal {mn}", where, f)
                + "\nUNION ALL\n" + not_numeric)
    where = _and(f"{_num(f)} IS NOT NULL AND ({_num(f)} < {mn} OR {_num(f)} > {mx})",
                 _scope_clause(p.get("scope")))
    return (_select(sheet, f, f"{f} outside [{mn}, {mx}]", where, f)
            + "\nUNION ALL\n" + not_numeric)


def _b_pattern_check(sheet, p):
    f = p["field"]
    pat = p["pattern"]
    where = _and(f"{_present(f)} AND NOT regexp_matches({_q(f)}, {_lit(pat)})",
                 _scope_clause(p.get("scope")))
    return _select(sheet, f, f"{f} does not match required format", where, f)


def _b_date_relation(sheet, p):
    a, b = p["field"], p["other_field"]
    op = p["op"]
    if op not in _CMP_OPS:
        raise CompileError(f"date_relation op must be one of {_CMP_OPS}")
    op = "<>" if op == "!=" else op
    da, db = _date(a), _date(b)
    # op is the COMPLIANT relation; violation = both dates present AND NOT(a op b)
    where = f"{da} IS NOT NULL AND {db} IS NOT NULL AND NOT ({da} {op} {db})"
    return _select(sheet, a, f"{a} must be {op} {b}", where, a)


def _b_date_bound(sheet, p):
    """A date field must satisfy field <op> a FIXED calendar date — the
    contract/program period boundary that bounds every policy row (policy period
    ⊆ program period). `date` is an ISO 'YYYY-MM-DD' constant taken from the
    contract; unlike date_relation (column vs column) the RHS is a literal.
    op is the COMPLIANT relation; violation = the row's date is present AND
    NOT(field op DATE 'value')."""
    f = p["field"]
    op = p["op"]
    if op not in _CMP_OPS:
        raise CompileError(f"date_bound op must be one of {_CMP_OPS}")
    op = "<>" if op == "!=" else op
    d = _date(f)
    lit = f"TRY_CAST({_lit(str(p['date']))} AS DATE)"
    where = _and(f"{d} IS NOT NULL AND {lit} IS NOT NULL AND NOT ({d} {op} {lit})",
                 _scope_clause(p.get("scope")))
    return _select(sheet, f, f"{f} must be {op} {p['date']}", where, f)


def _b_conditional_required(sheet, p):
    cond = p.get("condition") or {}
    cf, cop, cv = cond.get("field"), cond.get("op", "="), cond.get("value")
    rf = p["required_field"]
    if not cf:
        raise CompileError("conditional_required needs condition.field")
    if cop not in _CMP_OPS:
        raise CompileError(f"condition.op must be one of {_CMP_OPS}")
    cop = "<>" if cop == "!=" else cop
    cond_sql = f"TRIM({_q(cf)}) {cop} {_lit(cv)}"
    where = f"({cond_sql}) AND {_empty(rf)}"
    return _select(sheet, rf, f"{rf} required when {cf} {cop} {cv}", where, rf)



def _cmp_bool(field, op, value, variations=None, normalized=False) -> str:
    """Boolean SQL for `field <op> value`. Equality (=, !=) is case-insensitive
    string compare; ordinal ops (<, <=, >, >=) are numeric (comma/currency
    stripped). Used by conditional_value for both the condition and the target.

    `variations` (optional) are extra surface spellings of the SAME accepted
    answer — e.g. "Palms Specialty Insurance Company, Inc." for "Palms Specialty".
    For an equality op the compare widens to an IN / NOT IN over
    {value} ∪ variations, so a cell that matches ANY listed spelling passes
    (=) / fails (!=) instead of only the one canonical form. Without this a
    conditional target compiled to a single literal and flagged every row whose
    carrier is written out in full even though it is the required carrier. Passed
    only for the TARGET side (the enforced value), never the trigger condition.
    Ignored for the ordinal ops (a numeric threshold has no spelling variants).

    `normalized` (target side only — see conditional_value / conditional_all)
    ALSO accepts a cell that equals one of those spellings once case, spacing and
    punctuation are stripped (_norm_sql / _norm_py — the SAME normalization the
    enum templates match on). A contract writes "Palms Specialty Insurance
    Company, Inc." and the bordereau writes "Palms Specialty Insurance Company
    Inc"; those are one name, and the literal compare above — which only lowers
    and trims — flagged every row carrying it. This widens SPELLING, not meaning:
    two different words never become equal, so it can no more accept a different
    carrier than the IN-list can."""
    if op not in _CMP_OPS:
        raise CompileError(f"unsupported op {op!r}")
    if op in ("=", "!=", "<>"):
        lhs = f"LOWER(TRIM({_q(field)}))"
        # variation_values may be a flat list or a {value: [spellings]} map;
        # a bare string is ONE spelling (never iterate it into characters).
        vv = variations or []
        if isinstance(vv, dict):
            vv = [s for lst in vv.values()
                  for s in (lst if isinstance(lst, (list, tuple, set)) else [lst])]
        elif isinstance(vv, str):
            vv = [vv]
        # `value` may itself be a LIST of equivalent spellings (e.g. a state
        # condition widened to ["California", "CA"] by the normalizer) — treat
        # it like variations: match ANY listed form. Without this branch a list
        # str()'d into one dead literal that matched nothing.
        base_vals = list(value) if isinstance(value, (list, tuple, set)) else [value]
        forms, seen = [], set()
        for x in [*base_vals, *vv]:
            if x is None:                   # skip a JSON null spelling, not 'none'
                continue
            s = str(x).strip().lower()
            if s and s not in seen:
                seen.add(s)
                forms.append(s)
        if not forms:                       # value itself was blank
            forms = [str(base_vals[0] if base_vals else value).strip().lower()]
        if len(forms) == 1:
            positive = f"{lhs} = {_lit(forms[0])}"
        else:
            inlist = ", ".join(_lit(x) for x in forms)
            positive = f"{lhs} IN ({inlist})"
        if normalized:
            normed = sorted({_norm_py(x) for x in forms if _norm_py(x)})
            if normed:
                inlist = ", ".join(_lit(x) for x in normed)
                positive = f"({positive} OR {_norm_sql(field)} IN ({inlist}))"
        return positive if op == "=" else f"NOT ({positive})"
    return f"({_num(field)} IS NOT NULL AND {_num(field)} {op} {_num_lit(value)})"


def _b_conditional_value(sheet, p):
    """If condition.field <op> condition.value, the target field must satisfy
    field <op> value (e.g. "unless Insured State = California, Paper must be X").
    Violation = condition holds AND target does NOT satisfy — only on rows that
    carry both values, so blanks don't false-flag."""
    cond = p.get("condition") or {}
    cf, cop, cv = cond.get("field"), cond.get("op", "="), cond.get("value")
    tf, top, tv = p.get("field"), p.get("op", "="), p.get("value")
    if not cf or not tf:
        raise CompileError("conditional_value needs condition.field and field")
    cond_holds = _cmp_bool(cf, cop, cv)
    # variation_values are surface spellings of the ENFORCED value (the target),
    # so accept any of them — the trigger condition keeps its exact literal.
    # normalized=True: the extracted variation list is rarely exhaustive (e.g. it
    # won't anticipate every "..., Inc." suffix a BDX's full legal name uses), so
    # punctuation and spacing are stripped before comparing — the same
    # normalization value_in_set/value_not_in_set match on.
    target_ok = _cmp_bool(tf, top, tv, p.get("variation_values"), normalized=True)
    where = _and(_and(_present(cf), _present(tf)),
                 f"({cond_holds}) AND NOT ({target_ok})")
    return _select(sheet, tf, f"when {cf} {cop} {cv}, {tf} must be {top} {tv}", where, tf)


def _b_conditional_all(sheet, p):
    """When ALL of `conditions` (a list of {field, op, value}) hold, the target
    field must satisfy field <op> value. A MULTI-COLUMN condition in one rule, e.g.
    "when paper = Specialty AND state = CA, Referral Indicator must be Yes".
    Violation = every condition holds AND the target is NOT satisfied — only on
    rows that carry all the values, so blanks don't false-flag. Its own template /
    builder, fully separate from conditional_value, so nothing else is affected."""
    conds = [c for c in (p.get("conditions") or [])
             if isinstance(c, dict) and c.get("field")]
    tf, top, tv = p.get("field"), p.get("op", "="), p.get("value")
    if not conds or not tf:
        raise CompileError("conditional_all needs conditions[] and field")
    holds = " AND ".join(f"({_cmp_bool(c['field'], c.get('op', '='), c.get('value'))})"
                         for c in conds)
    present = _and(*[_present(c["field"]) for c in conds], _present(tf))
    target_ok = _cmp_bool(tf, top, tv, p.get("variation_values"), normalized=True)
    where = _and(present, f"({holds}) AND NOT ({target_ok})")
    reason = ("when " + " and ".join(f"{c['field']} {c.get('op', '=')} {c.get('value')}"
                                      for c in conds)
              + f", {tf} must be {top} {tv}")
    return _select(sheet, tf, reason, where, tf)


def _b_period_duration(sheet, p):
    s, e = p["start_field"], p["end_field"]
    unit = str(p["unit"]).lower()
    if unit not in ("day", "month", "year"):
        raise CompileError("period_duration unit must be day|month|year")
    ds, de = _date(s), _date(e)
    dur = f"datediff('{unit}', {ds}, {de})"
    nonnull = f"{ds} IS NOT NULL AND {de} IS NOT NULL"

    # RANGE form: min and/or max bound the duration in ONE rule.
    # Compliant when min <= duration <= max; violation = duration < min OR > max.
    mn, mx = p.get("min"), p.get("max")
    if mn is not None or mx is not None:
        conds, parts = [], []
        if mn is not None:
            conds.append(f"{dur} < {_num_lit(mn)}")
            parts.append(f">= {_num_lit(mn)}")
        if mx is not None:
            conds.append(f"{dur} > {_num_lit(mx)}")
            parts.append(f"<= {_num_lit(mx)}")
        where = _and(f"{nonnull} AND ({' OR '.join(conds)})",
                     _scope_clause(p.get("scope")))
        reason = f"{s}->{e} duration must be {' and '.join(parts)} {unit}(s)"
        return _select(sheet, s, reason, where, s)

    # SINGLE-BOUND form: op is the COMPLIANT relation; violation = NOT(duration op value).
    op = p.get("op")
    if op not in _CMP_OPS:
        raise CompileError(
            "period_duration needs min/max (range) or a valid op + value")
    op = "<>" if op == "!=" else op
    val = _num_lit(p["value"])
    where = _and(f"{nonnull} AND NOT ({dur} {op} {val})",
                 _scope_clause(p.get("scope")))
    return _select(sheet, s, f"{s}->{e} duration must be {op} {val} {unit}(s)", where, s)


def _b_cross_field_math(sheet, p):
    res, l, r = p["result_field"], p["left_field"], p["right_field"]
    op = p["operator"]
    if op not in ("+", "-", "*", "/"):
        raise CompileError("cross_field_math operator must be + - * /")
    tol = float(p.get("tolerance_pct") or 0) / 100.0
    # Optional second band ("flag vs auto-reject"): a reported amount within
    # `tolerance_pct` of the formula is compliant; beyond `reject_pct` it is a hard
    # violation (the rule's own severity, e.g. Critical → block); the band BETWEEN
    # the two is a soft 'warning' (flag, don't reject). Bands activate only when
    # reject_pct parses to a number strictly greater than tolerance_pct — otherwise
    # behaviour is unchanged: one threshold, one severity, no zone column emitted.
    reject_raw = p.get("reject_pct")
    try:
        reject = float(reject_raw) / 100.0 if reject_raw not in (None, "") else None
    except (TypeError, ValueError):
        raise CompileError(f"reject_pct must be a number, got {reject_raw!r}")
    bands = reject is not None and reject > tol
    nres, nl, nr = _num(res), _num(l), _num(r)
    # A percent-stored operand (e.g. a Commission Rate column holding "23.5", not
    # 0.235) must be divided by 100 before the arithmetic, else the expected value
    # is 100x off. left_is_percent / right_is_percent mark such operands.
    if p.get("left_is_percent"):
        nl = f"({nl} / 100.0)"
    if p.get("right_is_percent"):
        nr = f"({nr} / 100.0)"
    # A COMPLEMENT operand is used as (1 - rate): the share of a base that remains
    # after a rate is taken off it — a premium net of ceding commission is
    # `base * (1 - cede rate)`. Applied AFTER any percent scaling, so a rate stored
    # as "13" yields (1 - 13/100), not (1 - 13). This is the only constant term the
    # template admits, and without it the everyday quota-share shape has no
    # expressible form at all: the formula collapses to `base * rate`, which is
    # wrong on every row whose rate is non-zero.
    if p.get("left_complement"):
        nl = f"(1.0 - {nl})"
    if p.get("right_complement"):
        nr = f"(1.0 - {nr})"
    expected = f"({nl} {op} {nr})"
    nonnull = f"{nres} IS NOT NULL AND {nl} IS NOT NULL AND {nr} IS NOT NULL"
    if op == "/":
        nonnull += f" AND {nr} <> 0"
    # Reporting-precision comparison (see _round): the reported amount and the
    # computed one are both rounded to cents before the ±% band is measured, so a
    # formula rule never fires on a difference invisible in the BDX. The band
    # itself is sized off the rounded expectation for the same reason.
    exp_r = _round(expected, p)
    dev = _abs_dev(nres, expected, p)
    tol_expr = f"{repr(tol)} * ABS({exp_r})"
    where = _and(f"{nonnull} AND {dev} > {tol_expr}",
                 _scope_clause(p.get("scope")))
    scope = p.get("scope")
    # Rows past the reject band are 'violation' (rule severity); rows in the
    # tolerance→reject band are a soft 'warning'. Emitted only when bands are on so
    # non-banded rules stay byte-identical to before.
    zone_expr = None
    if bands:
        reject_expr = f"{repr(reject)} * ABS({exp_r})"
        zone_expr = f"CASE WHEN {dev} > {reject_expr} THEN 'violation' ELSE 'warning' END"
    # Any of the three operands can be the non-numeric one — de-dup by field name
    # so a rule where e.g. left_field == right_field doesn't double-flag one cell.
    # When zoned, these hard "must be a number" rows carry a constant 'violation'
    # zone so every UNION arm exposes the same columns.
    not_numeric = "\nUNION ALL\n".join(
        _not_numeric_select(sheet, fld, scope,
                            zone_expr="'violation'" if bands else None)
        for fld in dict.fromkeys([res, l, r]))
    # The reason a reviewer reads must describe the arithmetic that was actually
    # checked. A complement operand is part of the formula, not a detail: without
    # it the message reads "must equal <base> * <rate>" on a rule that tested
    # <base> * (1 - <rate>) — telling the reviewer to reconcile against a figure
    # the rule never computed, and naming the very formula the complement exists
    # to avoid.
    ltxt = f"(1 - {l})" if p.get("left_complement") else l
    rtxt = f"(1 - {r})" if p.get("right_complement") else r
    return (_select(sheet, res, f"{res} must equal {ltxt} {op} {rtxt}", where, res,
                    zone_expr=zone_expr)
            + "\nUNION ALL\n" + not_numeric)


def _b_cross_field_compare(sheet, p):
    """A numeric field compared (inequality OK) to another field, optionally
    scaled by a constant: field <op> other_field [operator factor].
      "Fronting Fee >= 12.5% of GWP" → field=Fronting Fee, op='>=',
        other_field=GWP, operator='*', factor=0.125
      "Administrator fees <= Company fees" → field=Admin, op='<=', other_field=Company
    op is the COMPLIANT relation; violation = both present AND NOT(field op rhs).
    """
    f, op, other = p["field"], p["op"], p["other_field"]
    if op not in _CMP_OPS:
        raise CompileError(f"cross_field_compare op must be one of {_CMP_OPS}")
    op = "<>" if op == "!=" else op
    operator = p.get("operator")
    nf, no = _num(f), _num(other)
    if operator:
        if operator not in ("+", "-", "*", "/"):
            raise CompileError("cross_field_compare operator must be + - * /")
        rhs = f"({no} {operator} {_num_lit(p.get('factor', 1))})"
    else:
        rhs = no
    # EXACT MATCH ("="): the field must EQUAL the (scaled) other field — a
    # DEFINITIONAL "A shall BE P% of B" (e.g. a 50/50 apportionment). Use an
    # absolute float-rounding tolerance and flag a blank/non-numeric REQUIRED
    # value. "!=" is the inverse. INEQUALITIES (>, >=, <, <=) keep the original
    # "both present, skip blanks" behaviour so existing floor/ceiling rules are
    # unchanged.
    tol = _num_lit(p.get("tolerance", 0.01))
    # Compare at reporting precision (see _round): the equality branches measure
    # the gap between the ROUNDED cell and the ROUNDED expectation, so a
    # difference that does not exist at two decimals is never an exception. The
    # INEQUALITY branches are left on raw values — a floor/ceiling is a threshold
    # test, and rounding could push a value across the bound it must respect.
    dev = _abs_dev(nf, rhs, p)
    scope = p.get("scope")
    # "=" and ">"/">=" (in cross_field_or_value below) already treat a non-numeric
    # `f` as a violation via the `nf IS NULL OR ...` branch — so only `other` is
    # genuinely uncovered there. "<>" and the remaining ordinal ops don't cover
    # either side. Add a not_numeric UNION only for the side(s) not already
    # implied, so a bad cell is never flagged twice under two different reasons.
    if op == "=":
        where = f"{no} IS NOT NULL AND ({nf} IS NULL OR {dev} > {tol})"
        extra = [other]
    elif op == "<>":
        where = f"{nf} IS NOT NULL AND {no} IS NOT NULL AND {dev} <= {tol}"
        extra = [f, other]
    else:
        where = f"{nf} IS NOT NULL AND {no} IS NOT NULL AND NOT ({nf} {op} {rhs})"
        extra = [f, other]
    tail = f" {operator} {p.get('factor', 1)}" if operator else ""
    rel = "equal" if op == "=" else f"be {op}"
    where = _and(where, _scope_clause(scope))
    not_numeric = "\nUNION ALL\n".join(
        _not_numeric_select(sheet, fld, scope) for fld in dict.fromkeys(extra))
    return (_select(sheet, f, f"{f} must {rel} {other}{tail}", where, f)
            + "\nUNION ALL\n" + not_numeric)


def _b_cross_field_or_value(sheet, p):
    """A numeric field compared to the GREATER (or LESSER) of a scaled other
    field OR a constant — ONE rule for "the greater of P% of B or $C":
      field <op> GREATEST(other_field <operator> factor, value)   (bound='greater')
      field <op> LEAST(other_field <operator> factor, value)      (bound='lesser')
    e.g. "Fronting Fee >= the greater of 12.5% of GWP or $15,000" →
      field=Fronting Fee, op='>=', other_field=GWP, operator='*', factor=0.125,
      value=15000, bound='greater'. `operator`/`factor` are optional (omit for a
      direct "greater of B or C"). op is the COMPLIANT relation; violation = both
      present AND NOT(field op rhs). This replaces emitting a separate
      cross_field_compare + min/max pair, so one requirement is one exception.
    """
    f, op, other = p["field"], p["op"], p["other_field"]
    if op not in _CMP_OPS:
        raise CompileError(f"cross_field_or_value op must be one of {_CMP_OPS}")
    op = "<>" if op == "!=" else op
    operator = p.get("operator")
    nf, no = _num(f), _num(other)
    if operator:
        if operator not in ("+", "-", "*", "/"):
            raise CompileError("cross_field_or_value operator must be + - * /")
        scaled = f"({no} {operator} {_num_lit(p.get('factor', 1))})"
    else:
        scaled = no
    val = _num_lit(p["value"])
    bound = str(p.get("bound", "greater")).lower()
    fn = "GREATEST" if bound in ("greater", "greatest", "max", "larger") else "LEAST"
    rhs = f"{fn}({scaled}, {val})"
    # Absolute tolerance for float rounding on exact-match / not-equal (e.g. an
    # entered 45770.4999999 vs a computed 45770.50). Overridable via param.
    tol = _num_lit(p.get("tolerance", 0.01))
    # Same reporting-precision comparison as cross_field_compare: only the
    # exact-match / not-equal branches round, the floor and ceiling branches keep
    # comparing raw values so a bound is never crossed by rounding.
    dev = _abs_dev(nf, rhs, p)
    # EXACT MATCH ("="): the field must EQUAL the greater/lesser value (the agent
    # keys the computed amount into the cell). NOT-EQUAL ("!=") is the inverse.
    # A ">=" / ">" bound is a REQUIRED FLOOR the field must MEET. In BOTH the
    # exact-match and floor cases a blank / non-numeric value (e.g. an un-evaluated
    # Excel formula string TRY_CAST can't parse) is a REQUIRED value that FAILS →
    # flag it rather than silently skip. A "<=" / "<" ceiling leaves a blank value
    # alone (there is nothing to cap). The base must be present to size the % side.
    scope = p.get("scope")
    # See the matching comment in _b_cross_field_compare: "=" and ">"/">=" already
    # treat a non-numeric `f` as a violation, so only `other` needs a not_numeric
    # UNION there; "!="/"<>" and "<"/"<=" cover neither side yet.
    if op == "=":
        where = f"{no} IS NOT NULL AND ({nf} IS NULL OR {dev} > {tol})"
        extra = [other]
    elif op in ("!=", "<>"):
        where = f"{nf} IS NOT NULL AND {no} IS NOT NULL AND {dev} <= {tol}"
        extra = [f, other]
    elif op in (">", ">="):
        where = f"{no} IS NOT NULL AND ({nf} IS NULL OR NOT ({nf} {op} {rhs}))"
        extra = [other]
    else:
        where = f"{nf} IS NOT NULL AND {no} IS NOT NULL AND NOT ({nf} {op} {rhs})"
        extra = [f, other]
    tail = f" {operator} {p.get('factor', 1)}" if operator else ""
    word = "greater" if fn == "GREATEST" else "lesser"
    rel = "equal" if op == "=" else f"be {op}"
    where = _and(where, _scope_clause(scope))
    not_numeric = "\nUNION ALL\n".join(
        _not_numeric_select(sheet, fld, scope) for fld in dict.fromkeys(extra))
    return (_select(sheet, f,
                    f"{f} must {rel} the {word} of ({other}{tail}) or {p['value']}",
                    where, f)
            + "\nUNION ALL\n" + not_numeric)


def _row_union(sheets, cols, aliases=None):
    """A subquery that UNIONs the ROWS of several sheets, exposing __rowid, the
    referenced columns, and a __sheet label. An aggregate runs over this so it
    sums/counts the COMBINED rows across sheets — NOT one total per sheet.

    A sheet that spells a referenced column differently (aliases, see
    OutputSchema.field_aliases) selects its local column AS the canonical name,
    so the outer aggregate's column references hold across every arm."""
    aliases = aliases or {}

    def _sel(sh):
        cells = []
        for c in cols or []:
            local = (aliases.get(c) or {}).get(sh, c)
            cells.append(f", {_q(local)} AS {_q(c)}" if local != c else f", {_q(c)}")
        return "__rowid" + "".join(cells)

    parts = [f"SELECT {_sel(sh)}, {_lit(sh)} AS __sheet FROM {_q(sh)}" for sh in sheets]
    return "(" + " UNION ALL ".join(parts) + ") AS _u"


def _b_aggregate_cap(sheet, p, source=None, sheet_expr=None):
    agg = str(p["aggregation"]).lower()
    f = p["field"]
    group_by = [g for g in (p.get("group_by") or []) if g]
    mx = p.get("max")
    mn = p.get("min")
    if mx is None and mn is None:
        raise CompileError("aggregate_cap needs at least one of max/min")

    # row_reducer — how to treat a value that REPEATS across a policy's transaction
    # rows (endorsements, additional/return premium). Default "sum" adds every row,
    # which is right for a PREMIUM/FEE (each row is a genuinely different amount).
    # "max" (any non-"sum" value) means the field is a per-policy figure the BDX
    # DUPLICATES on every transaction row (a LIMIT / sublimit): collapse it to ONE
    # value per (policy, sheet) BEFORE summing across sheets, else N transaction
    # rows inflate the limit N×. Only meaningful for aggregation="sum".
    reducer = str(p.get("row_reducer") or "sum").lower()
    if agg == "sum" and reducer != "sum":
        return _b_aggregate_cap_dedup(sheet, f, group_by, mx, mn, source)

    if agg == "sum":
        agg_expr = f"SUM({_num(f)})"
    elif agg == "count":
        agg_expr = "COUNT(*)"
    elif agg == "distinct_count":
        agg_expr = f"COUNT(DISTINCT {_q(f)})"
    else:
        raise CompileError("aggregation must be sum|count|distinct_count")

    having = []
    if mx is not None:
        having.append(f"{agg_expr} > {_num_lit(mx)}")
    if mn is not None:
        having.append(f"{agg_expr} < {_num_lit(mn)}")
    having_sql = " OR ".join(having)

    group_sql = f" GROUP BY {', '.join(_q(g) for g in group_by)}" if group_by else ""
    key_expr = _q(group_by[0]) if group_by else "NULL"
    # "at most ONE distinct value per group" is an INVARIANT ("this must not change
    # across the entity's rows"), so say that instead of the aggregation mechanics —
    # the reason is what the reviewer reads on the exception.
    is_consistency_invariant = (
        agg == "distinct_count" and bool(group_by) and mx == 1 and mn is None
    )
    if is_consistency_invariant:
        reason = (f"{f} is not the same on every row of the same "
                  f"{', '.join(group_by)}")
        # For a consistency invariant the bare count ("2") is useless to the
        # reviewer — they need to see WHICH values differ (e.g. the two effective
        # dates). Emit the sorted list of the distinct values instead of the count.
        actual_expr = (
            f"array_to_string("
            f"list_sort(array_agg(DISTINCT CAST({_q(f)} AS VARCHAR))), ', ')"
        )
    else:
        reason = f"{agg}({f}) breaches limit"
        actual_expr = f"CAST({agg_expr} AS VARCHAR)"
    from_sql = source or _q(sheet)            # single sheet, or a cross-sheet union
    sheet_lit = sheet_expr or _lit(sheet)     # 'SheetName', or MIN(__sheet) for a union
    return (
        f'SELECT MIN(__rowid) AS row_id, {sheet_lit} AS sheet, {_lit(f)} AS field, '
        f'{_lit(reason)} AS reason, {key_expr} AS policy_number, '
        f'{actual_expr} AS actual_value '
        f'FROM {from_sql}{group_sql} HAVING {having_sql}'
    )


def _b_aggregate_cap_dedup(sheet, f, group_by, mx, mn, source):
    """aggregate_cap SUM where the field is a per-policy value REPEATED across the
    policy's transaction rows (a limit / sublimit). Two-level aggregation:
      inner: MAX({field}) per (group_by, sheet)  → one value per policy per schedule
      outer: SUM(that) per group_by across sheets → the true cumulative exposure
    so N endorsement rows all carrying the same $X limit count ONCE per schedule,
    not N×. A single-sheet rule synthesizes a constant __sheet so the same shape
    applies (the outer SUM is then just that sheet's one collapsed value). MAX
    assumes the limit is identical across a policy's rows on a sheet (true for a
    duplicated per-policy figure); a limit that genuinely changes mid-term would
    need a latest-by-date reducer, not yet modelled here."""
    if not group_by:
        raise CompileError("aggregate_cap row_reducer needs group_by (the policy "
                           "key the repeated value collapses to)")
    keys = [_q(g) for g in group_by]
    keys_csv = ", ".join(keys)
    # A row source that always exposes __rowid, the field/keys and a __sheet label:
    # the cross-sheet UNION already carries __sheet; a single sheet gets a literal.
    row_src = source or f'(SELECT *, {_lit(sheet)} AS __sheet FROM {_q(sheet)}) AS _u'
    inner = (
        f'SELECT {keys_csv}, __sheet, MAX({_num(f)}) AS _grp_val, '
        f'MIN(__rowid) AS _rid FROM {row_src} GROUP BY {keys_csv}, __sheet'
    )
    outer_agg = "SUM(_grp_val)"
    having = []
    if mx is not None:
        having.append(f"{outer_agg} > {_num_lit(mx)}")
    if mn is not None:
        having.append(f"{outer_agg} < {_num_lit(mn)}")
    having_sql = " OR ".join(having)
    reason = f"cumulative {f} across schedules breaches limit"
    return (
        f'SELECT MIN(_rid) AS row_id, MIN(__sheet) AS sheet, {_lit(f)} AS field, '
        f'{_lit(reason)} AS reason, {keys[0]} AS policy_number, '
        f'CAST({outer_agg} AS VARCHAR) AS actual_value '
        f'FROM ({inner}) AS _per GROUP BY {keys_csv} HAVING {having_sql}'
    )


def _b_uniqueness(sheet, p, source=None, sheet_expr=None):
    fields = [f for f in (p.get("fields") or []) if f]
    if not fields:
        raise CompileError("uniqueness needs at least one field")
    cols = ", ".join(_q(f) for f in fields)
    reason = f"duplicate {', '.join(fields)}"
    from_sql = source or _q(sheet)
    sheet_lit = sheet_expr or _lit(sheet)
    return (
        f'SELECT MIN(__rowid) AS row_id, {sheet_lit} AS sheet, '
        f'{_lit(fields[0])} AS field, {_lit(reason)} AS reason, '
        f'{_q(fields[0])} AS policy_number, CAST(COUNT(*) AS VARCHAR) AS actual_value '
        f'FROM {from_sql} GROUP BY {cols} HAVING COUNT(*) > 1'
    )


# --- Country-dispatched postal validation -----------------------------------
# The `postal_reference` / `postal_state` tables key on '<country>:<value>' so the
# per-country probes below stay SINGLE-EQUALITY anti-joins (hash-joinable) instead
# of an OR across three countries, which would force a nested scan of the reference
# for every BDX row. See intl_postal_reference.py for how the key is built.
#
# Each supported country needs two things: how to turn a raw BDX cell into that
# country's canonical postal key, and a SHAPE guard that says "this value could be
# one of ours" — the guard is what stops a Canadian 'V6B 4N9' being stripped to its
# digits ('649') and matched against a Puerto Rican ZIP. `_alnum` is the cell's
# alphanumerics upper-cased; `_digits` is its digits only. Both are materialised
# once per row. The code expressions MIRROR intl_postal_reference._canonical_code —
# the reference is built with exactly the same normalization.
_POSTAL_COUNTRY_SQL = {
    # Strip non-digits, take the first 5 (handles ZIP+4 '85009-1234'), else
    # left-pad with zeros (recovers Excel's dropped leading zero, '6390'→'06390').
    "US": {"shape": "^[0-9]+$",
           "code": "(CASE WHEN length({d}) >= 5 THEN left({d}, 5) "
                   "ELSE lpad({d}, 5, '0') END)",
           "extra": "length({d}) >= 3"},
    # Canadian FSA — the first 3 characters of a postal code ('T0A' of 'T0A 1A0'),
    # always letter-digit-letter.
    "CA": {"shape": "^[A-Z][0-9][A-Z]",
           "code": "left({a}, 3)",
           "extra": ""},
    # UK outward code — the part before the space ('SW1A' of 'SW1A 1AA'). A full UK
    # postcode is 5-7 alphanumerics with an inward code of exactly 3; an outward
    # code alone is 2-4. The length therefore decides which form the cell holds,
    # with no ambiguity.
    "GB": {"shape": "^[A-Z]{1,2}[0-9]",
           "code": "(CASE WHEN length({a}) >= 5 THEN left({a}, length({a}) - 3) "
                   "ELSE {a} END)",
           "extra": ""},
}


def _postal_countries(p):
    """The countries a postal rule may validate against — every supported country
    unless the rule narrows it with a `countries` param (e.g. a US-only program
    that wants the pre-existing strictness back). Order is fixed so the compiled
    SQL is deterministic."""
    try:
        from contract_upload_services.intl_postal_reference import SUPPORTED_COUNTRIES
    except Exception as exc:  # partial checkout — fail cleanly (→ review), never crash
        raise CompileError(f"intl_postal reference module unavailable: {exc}")
    want = p.get("countries")
    if not want:
        return list(SUPPORTED_COUNTRIES)
    want = {str(c).strip().upper() for c in want if str(c or "").strip()}
    chosen = [c for c in SUPPORTED_COUNTRIES if c in want]
    if not chosen:
        raise CompileError(f"countries {sorted(want)} include none we hold postal "
                           f"data for {list(SUPPORTED_COUNTRIES)}")
    return chosen


def _postal_country_expr(p):
    """SQL resolving the row's COUNTRY to an alpha-2 we hold postal data for:

        NULL  — no country column bound, or the cell is blank  → validate the row
                against EVERY allowed country (a match in any one passes)
        '??'  — a country IS stated but we hold no postal data for it (FR, DE, …)
                → the row is SKIPPED, mirroring how currency_country_consistency
                skips an unrecognised country rather than flagging it
        'US' / 'CA' / 'GB' — validate against that country's slice ONLY

    Resolution goes through the `postal_country` alias table (ISO alpha-2/alpha-3/
    name/official name, plus GB's constituent countries), so no country vocabulary
    is written into this SQL. MIN() keeps it a single-row scalar subquery."""
    cf = p.get("country_field")
    if not cf:
        return "CAST(NULL AS VARCHAR)"
    try:
        from contract_upload_services.intl_postal_reference import POSTAL_COUNTRY_TABLE as _C
    except Exception as exc:  # partial checkout — fail cleanly (→ review), never crash
        raise CompileError(f"intl_postal reference module unavailable: {exc}")
    qc = _q(cf)
    return (f"CASE WHEN {qc} IS NULL OR TRIM({qc}) = '' THEN NULL "
            f"ELSE COALESCE((SELECT MIN(c.country) FROM {_C} c "
            f"WHERE c.alias = upper(trim({qc}))), '??') END")


def _postal_checkable(countries, keys_null_check=True):
    """SQL deciding whether a row is IN SCOPE for a postal check at all.

    A row is checkable when EITHER:
      * its country resolves to one of this rule's allowed countries — the country
        is stated and we hold its data, so the value is judged against that country
        even if its SHAPE is wrong (a Canadian code on a row that says 'US' is a
        real error and must be flagged, not skipped); or
      * no country is stated (or the column isn't bound) AND the value's shape
        matches at least one allowed country — a match in any of them passes.

    Everything else is SKIPPED rather than flagged: a country we hold no postal
    data for ('??'), a country outside a narrowed `countries` list, and — with no
    country column — a value whose shape belongs to none of them. This is the same
    conservative stance `_b_currency_country_consistency` takes on an unrecognised
    country: these rules validate a code FOR a known country, they are not
    country-value checkers."""
    in_list = ", ".join(_lit(c) for c in countries)
    parts = [f"b._cty IN ({in_list})"]
    if keys_null_check:
        parts.append(" OR ".join(
            f"b._k{cc.lower()} IS NOT NULL" for cc in countries))
    return "(" + " OR ".join(f"({x})" for x in parts) + ")"


def _reason_country(countries):
    """The country NAMED in a postal violation message, as SQL.

    Normally the row's own country column supplies it (blank when no country
    column is bound). When the rule is narrowed to a SINGLE country — because the
    contract itself pins one — that country is what the value was judged against
    even though no column states it, so the message says so ("TN is not a valid
    GB state / province / region") instead of leaving the reviewer to guess which
    reference the check used."""
    if len(countries) == 1:
        return f"COALESCE(b._cty, {_lit(countries[0])}) || ' '"
    return "COALESCE(b._cty || ' ', '')"


def _b_zip_state_consistency(sheet, p):
    """A reported postal code must be valid FOR the state/province/region reported
    on the SAME row, checked row-by-row against the `postal_reference` table
    (US ZIPs from `uszips`, Canadian FSAs and UK outward codes from the bundled
    intl_postal Parquet — see intl_postal_reference.load_reference_tables). The
    reference is treated as the AUTHORITATIVE, complete list of valid codes.

    COUNTRY DISPATCH. The country decides which reference slice and which
    normalization apply — a US ZIP is checked exactly as it always was, a Canadian
    code against the CA data, a British one against the GB data:

      * `country_field` bound and the cell names a country we hold data for → that
        country's slice ONLY.
      * `country_field` bound but the country is one we hold no postal data for →
        the row is SKIPPED (not flagged); the same conservative stance
        `_b_currency_country_consistency` takes on an unrecognised country.
      * no `country_field` (or a blank cell) → the row passes if it is valid in ANY
        allowed country. This is safe rather than loose: US/CA/GB region codes and
        names have ZERO overlap and no CA/GB code is all-digits, so a value can
        only satisfy the country it actually belongs to.

    STRICT mode within a country (no prefix fallback): a row is flagged when the
    (code, region) pair is NOT present — i.e. either the code belongs to a
    DIFFERENT region than reported, OR it is not a valid code for that country at
    all. Rows are skipped when the postal code or the region is blank (a separate
    required-field concern) or when the value's shape matches no allowed country
    (so a foreign code in an unlabelled column is never flagged as a bad US ZIP).
    The region is matched by code OR full name — and, for GB, by county — all
    upper/trimmed.

    Both the BDX table and the reference are qualified with distinct aliases (`b`
    and `p`), so no BDX column name — even one literally called "zip"/"key" — can
    shadow a reference column inside the correlated subquery."""
    try:
        from contract_upload_services.intl_postal_reference import POSTAL_TABLE as _P
    except Exception as exc:  # partial checkout — fail cleanly (→ review), never crash
        raise CompileError(f"intl_postal reference module unavailable: {exc}")
    zf, sf = p["zip_field"], p["state_field"]
    qz, qs = _q(zf), _q(sf)
    countries = _postal_countries(p)

    # Level 0: materialise the row's normalised inputs ONCE, and pre-filter blanks /
    # scope, so the anti-joins below key on plain columns (a hash join) instead of
    # recomputing regexes for every reference-table probe — essential for large
    # (10k+ row) BDX sheets.
    alnum = f"upper(regexp_replace({qz}, '[^A-Za-z0-9]', '', 'g'))"
    digits = f"regexp_replace({qz}, '[^0-9]', '', 'g')"
    inner_where = _and(_present(zf), _present(sf), _scope_clause(p.get("scope")))
    lvl0 = (f"SELECT __rowid, {qz} AS _zipval, {qs} AS _stateval, "
            f"upper(trim({qs})) AS _s, {alnum} AS _a, {digits} AS _d, "
            f"{_postal_country_expr(p)} AS _cty "
            f"FROM {_q(sheet)} WHERE {inner_where}")

    # Level 1: one candidate KEY per allowed country, NULL when the row's country
    # rules that country out or the value's shape isn't that country's. A NULL key
    # matches nothing, so it simply contributes no pass.
    keys, probes = [], []
    for cc in countries:
        spec = _POSTAL_COUNTRY_SQL[cc]
        code = spec["code"].format(a="_a", d="_d")
        guard = _and(f"(_cty IS NULL OR _cty = {_lit(cc)})",
                     f"regexp_matches(_a, {_lit(spec['shape'])})",
                     spec["extra"].format(a="_a", d="_d"))
        alias = f"_k{cc.lower()}"
        keys.append(f"CASE WHEN {guard} THEN {_lit(cc + ':')} || {code} END AS {alias}")
        probes.append(f"NOT EXISTS (SELECT 1 FROM {_P} p "
                      f"WHERE p.key = b.{alias} AND p.state_alias = b._s)")
    lvl1 = (f"SELECT __rowid, _zipval, _stateval, _s, _cty, {', '.join(keys)} "
            f"FROM ({lvl0})")

    checkable = _postal_checkable(countries)
    reason = (f"{_lit(zf + ' ')} || b._zipval || {_lit(' is not a valid ')} || "
              f"{_reason_country(countries)} || "
              f"{_lit('postal code for ')} || b._stateval")
    return (f"SELECT b.__rowid AS row_id, {_lit(sheet)} AS sheet, {_lit(zf)} AS field, "
            f"{reason} AS reason, b._zipval AS actual_value "
            f"FROM ({lvl1}) AS b WHERE {checkable} AND {' AND '.join(probes)}")


def _b_state_validity(sheet, p):
    """A reported STATE must be a real state / province / region, checked row-by-row
    against the `postal_state` table (loaded by
    `intl_postal_reference.load_reference_tables`). Its DISTINCT per-country values
    ARE the authoritative vocabulary — every USPS code and state name (50 states +
    DC + territories), every Canadian province code and name, and every UK
    constituent country and county — so no region vocabulary is written into this
    SQL: a region added or renamed in the source data flows through with no code
    change.

    COUNTRY DISPATCH, identical to `_b_zip_state_consistency`: with a
    `country_field` bound the value must be a region OF THAT COUNTRY (so 'ON' is
    valid for Canada and invalid for the US); a country we hold no data for is
    SKIPPED; with no country column the value passes if it is a real region in ANY
    allowed country, which is unambiguous because US/CA/GB region codes and names
    do not overlap.

    STRICT within a country: a row is flagged when its value matches no region
    code, name (or, for GB, county), upper/trimmed — i.e. it is a typo ('CAL',
    'XX') or free text. Blank states are skipped (a separate required-field
    concern).

    Both sides are qualified with distinct aliases (`b` and `p`), so no BDX column
    name — even one literally called "key" — can shadow a reference column inside
    the correlated subquery."""
    try:
        from contract_upload_services.intl_postal_reference import POSTAL_STATE_TABLE as _S
    except Exception as exc:  # partial checkout — fail cleanly (→ review), never crash
        raise CompileError(f"intl_postal reference module unavailable: {exc}")
    sf = p["state_field"]
    qs = _q(sf)
    countries = _postal_countries(p)
    inner_where = _and(_present(sf), _scope_clause(p.get("scope")))
    lvl0 = (f"SELECT __rowid, {qs} AS _stateval, upper(trim({qs})) AS _s, "
            f"{_postal_country_expr(p)} AS _cty "
            f"FROM {_q(sheet)} WHERE {inner_where}")
    keys, probes = [], []
    for cc in countries:
        alias = f"_k{cc.lower()}"
        keys.append(f"CASE WHEN _cty IS NULL OR _cty = {_lit(cc)} "
                    f"THEN {_lit(cc + ':')} || _s END AS {alias}")
        probes.append(f"NOT EXISTS (SELECT 1 FROM {_S} p WHERE p.key = b.{alias})")
    lvl1 = (f"SELECT __rowid, _stateval, _s, _cty, {', '.join(keys)} FROM ({lvl0})")
    checkable = _postal_checkable(countries)
    reason = (f"{_lit(sf + ' ')} || b._stateval || {_lit(' is not a valid ')} || "
              f"{_reason_country(countries)} || {_lit('state / province / region')}")
    return (f"SELECT b.__rowid AS row_id, {_lit(sheet)} AS sheet, {_lit(sf)} AS field, "
            f"{reason} AS reason, b._stateval AS actual_value "
            f"FROM ({lvl1}) AS b WHERE {checkable} AND {' AND '.join(probes)}")


def _b_currency_country_consistency(sheet, p):
    """A reported CURRENCY must be legal tender in the COUNTRY reported on the SAME
    row, checked row-by-row against the FULL `country_currency` reference table (937
    denormalized alias->currency rows — every recognized country spelling paired
    with every currency it accepts — loaded into DuckDB from a bundled Parquet by
    `country_currency_reference.load_reference_table`) — not an inline literal
    approximation. A legitimate dual-currency country (e.g. Panama: PAB or USD; 7
    countries total) accepts EITHER, because both its rows are present.

    Violation requires ALL of: (a) the row's COUNTRY resolves to a known country
    (matches some `country_currency.alias` — alpha-2, alpha-3, full name, OR
    official name, case/space-insensitive, e.g. 'US', 'USA', 'United States' all
    match); (b) NO `country_currency` row pairs that alias with this row's CURRENCY
    (uppercased/trimmed). An UNRECOGNISED country value is SKIPPED — this rule
    validates currency FOR a known country, it is not a country-value checker (that
    is a separate concern); blank currency/country are also skipped.

    Mirrors `_b_zip_state_consistency`'s structure exactly: an inner subquery
    materialises the normalised country alias (`_alias`) and currency (`_cur`) ONCE
    per row (outer alias `b`), then two short EXISTS/NOT-EXISTS probes against the
    real reference table — so the compiled query stays small regardless of the
    reference table's size, and does not multiply when a rule fans out across
    several sheets (UNION ALL)."""
    try:
        from contract_upload_services.country_currency_reference import (
            COUNTRY_CURRENCY_TABLE as _T)
    except Exception as exc:  # partial checkout — fail cleanly (→ review), never crash
        raise CompileError(f"country_currency reference module unavailable: {exc}")
    cf, cyf = p["currency_field"], p["country_field"]
    qc, qcy = _q(cf), _q(cyf)
    inner_where = _and(f"({qc} IS NOT NULL AND TRIM({qc}) <> '')",
                       f"({qcy} IS NOT NULL AND TRIM({qcy}) <> '')",
                       _scope_clause(p.get("scope")))
    inner = (f"SELECT __rowid, {qc} AS _curval, {qcy} AS _countryval, "
             f"UPPER(TRIM({qc})) AS _cur, UPPER(TRIM({qcy})) AS _alias "
             f"FROM {_q(sheet)} WHERE {inner_where}")
    known_country = f"EXISTS (SELECT 1 FROM {_T} t WHERE t.alias = b._alias)"
    invalid = (f"NOT EXISTS (SELECT 1 FROM {_T} t "
              f"WHERE t.alias = b._alias AND t.currency = b._cur)")
    reason = (f"{_lit(cf + ' ')} || b._curval || "
             f"{_lit(' is not a valid currency for country ')} || b._countryval")
    return (f"SELECT b.__rowid AS row_id, {_lit(sheet)} AS sheet, {_lit(cf)} AS field, "
            f"{reason} AS reason, b._curval AS actual_value "
            f"FROM ({inner}) AS b WHERE {known_country} AND {invalid}")


_BUILDERS = {
    "required_field": _b_required_field,
    "value_in_set": _b_value_in_set,
    "value_not_in_set": _b_value_not_in_set,
    "max_limit": _b_max_limit,
    "min_limit": _b_min_limit,
    "range_check": _b_range_check,
    "pattern_check": _b_pattern_check,
    "date_relation": _b_date_relation,
    "date_bound": _b_date_bound,
    "conditional_required": _b_conditional_required,
    "conditional_value": _b_conditional_value,
    "conditional_all": _b_conditional_all,
    "period_duration": _b_period_duration,
    "cross_field_math": _b_cross_field_math,
    "cross_field_compare": _b_cross_field_compare,
    "cross_field_or_value": _b_cross_field_or_value,
    "aggregate_cap": _b_aggregate_cap,
    "uniqueness": _b_uniqueness,
    "zip_state_consistency": _b_zip_state_consistency,
    "state_validity": _b_state_validity,
    "currency_country_consistency": _b_currency_country_consistency,
}

# Templates that GROUP/aggregate rows. Across multiple sheets these must union
# the rows first and aggregate once (their builders accept source/sheet_expr);
# row-level templates instead fan out per sheet and UNION the results.
_AGGREGATE_TEMPLATES = {"aggregate_cap", "uniqueness"}


_SCOPE_MARKERS = ("schedule", "schedules", "sched", "sch",
                  "section", "sections", "program", "programs")
_SCOPE_STOP = set(_SCOPE_MARKERS) | {"sheet", "sheets", "the", "current", "bdx"}


def _scope_toks(s):
    """Word/letter/number tokens, lowercased — 'Palms Schedule G Current BDX' →
    ['palms','schedule','g','current','bdx']."""
    return _re.findall(r"[a-z0-9]+", str(s).lower())


def _sched_key(tokens):
    """A schedule's identifier — the token following the LAST schedule/section/
    program marker: 'Risksmith Schedule Program G' → 'g', 'Palms Sch A …' → 'a',
    'Palms Schedule G Current BDX' → 'g'. If there is no marker, a lone significant
    token IS the key (a bare 'G'). None when nothing identifies a schedule."""
    key = None
    for i, t in enumerate(tokens):
        if t in _SCOPE_MARKERS and i + 1 < len(tokens):
            key = tokens[i + 1]
    if key is not None:
        return key
    sig = [t for t in tokens if t not in _SCOPE_STOP]
    return sig[0] if len(sig) == 1 else None


def _filter_sheets_by_scope(sheets, params):
    """If the IR names specific schedules/sheets (params `scope_sheets`/`sheets`),
    restrict the target sheets to those the rule actually names — so a rule scoped
    to a named subset of schedules spans ONLY those, not every sheet carrying the
    column.

    Scope names and sheet titles rarely share their full text — the contract says
    'Risksmith Schedule Program G' while the sheet is 'Palms Schedule G Current
    BDX'. The ONE token they reliably share is the schedule identifier (the letter
    after the schedule/program marker). So both sides are reduced to that key and
    matched on it. Combined phrases the model didn't split ('Schedule G, H and J' /
    'G, H, I, J') are split on commas / 'and' / 'or' / '&' / '/' first, and a bare
    identifier ('G') is taken as its own key. This replaces a raw normalized-
    substring test that both false-matched (a bare 'G' hit the 'g' inside every
    'Schedule') and false-missed (a combined phrase matched no single sheet, then
    fell through to the full list).

    Falls back to the full list only when nothing matches (all named schedules are
    absent from this BDX)."""
    frags_raw = params.get("scope_sheets") or params.get("sheets")
    if not isinstance(frags_raw, (list, tuple)):
        return sheets
    keys = set()
    for fr in frags_raw:
        if not fr:
            continue
        for piece in _re.split(r"\s*(?:,|/|&|\band\b|\bor\b)\s*", str(fr),
                               flags=_re.I):
            k = _sched_key(_scope_toks(piece))
            if k:
                keys.add(k)
    if not keys:
        return sheets
    keep = [s for s in sheets if _sched_key(_scope_toks(s)) in keys]
    return keep or sheets


def _and(*parts: str) -> str:
    real = [p for p in parts if p]
    return " AND ".join(real)


# ---------------------------------------------------------------------
# Staleness of a CACHED compiled query
# ---------------------------------------------------------------------
# `run_validation` executes the SQL cached in `rule_spec.compiled_sql`; it never
# recompiles. That is right for ordinary rules — the query is deterministic and
# was verified at contract upload. It is WRONG the moment a builder's REFERENCE
# TABLE changes underneath it: the cached query keeps joining the old table and
# silently validates against stale reference data, with no error to notice.
#
# That is exactly what happened when the postal rules gained CA/GB support: their
# builders moved from the US-only `uszips` table to the country-dispatched
# `postal_reference` / `postal_state` tables, but every already-compiled rule kept
# querying `uszips` and false-flagged every Canadian and British row.
#
# So each reference-bound template declares the table its CURRENT builder emits.
# A cached query that does not mention it predates the change and must be
# recompiled from the IR before running. This is checked per validation run and
# costs nothing to maintain: when a builder's reference table changes again, the
# entry here changes with it and every existing rule self-heals on its next run —
# no migration, no DB write, no re-upload.
_REFERENCE_BOUND_TEMPLATES = {
    "zip_state_consistency":        "postal_reference",
    "state_validity":               "postal_state",
    "currency_country_consistency": "country_currency",
}


# Any cached query that still SCORES a cell against a value predates the move to
# exact matching (see the module header) and decides value identity by string
# distance — which is how 'NM' came to be reported as the excluded 'NMI'. The
# scoring function is its own version marker: wherever it appears in a stored
# query, that query is recompiled from its IR at validation time, so every rule
# already in the database is fixed on its next run — no migration, no DB write,
# no re-upload. Not restricted to a template list: the marker only ever appears
# in a query the old compiler wrote.
_SIMILARITY_MARKER = "jaro_winkler_similarity"


# Formula queries compiled before NUMERIC_MATCH_DECIMALS compare RAW values, so
# they raise exceptions on differences that do not exist at two decimals. The
# rounded operand is its own version marker: a cached EQUALITY query (the ABS(...)
# deviation form — the inequality branches deliberately do not round, so they must
# not be treated as stale) that lacks it predates the change and is recompiled
# from its IR at validation time. Existing rules therefore self-heal on their next
# run; no migration, no re-upload.
_ROUNDED_COMPARE_TEMPLATES = {"cross_field_compare", "cross_field_math",
                              "cross_field_or_value"}
_ROUNDED_COMPARE_MARKER = "ROUND(TRY_CAST("


def compiled_sql_is_stale(template: str, sql: str) -> bool:
    """True when a cached query for `template` predates a reference-table change or
    a compiler fix and must be recompiled from its IR. False for every other
    template, so the majority of rules keep running their cached SQL untouched."""
    if not sql:
        return False
    if _SIMILARITY_MARKER in sql:
        return True
    table = _REFERENCE_BOUND_TEMPLATES.get(template)
    if table and table not in sql:
        return True
    return (template in _ROUNDED_COMPARE_TEMPLATES and "ABS(" in sql
            and _ROUNDED_COMPARE_MARKER not in sql)


# ---------------------------------------------------------------------
# Cached queries compiled with a collapsing alias
# ---------------------------------------------------------------------
# compile_ir now refuses to compile an arm whose aliases fold two of a rule's
# fields onto one column (see _collapses_on), but rules compiled BEFORE that
# guard carry the bad arm in their cached SQL and would keep raising exceptions
# until their contract is re-uploaded. These two helpers let the validation run
# find and remove such an arm from the cached query in memory, so the fix reaches
# existing rules on their next run: no migration, no re-upload, and the rule's
# sound arms keep validating exactly as they do today.

def _arm_columns(arm: str) -> set:
    """The BDX columns one compiled arm reads — quoted identifiers minus the
    table names, the synthetic rowid and the bundled reference tables."""
    cols = set(_re.findall(r'"([^"]+)"', arm))
    cols -= set(_re.findall(r'FROM\s+"([^"]+)"', arm))
    cols -= _REFERENCE_TABLE_NAMES
    cols.discard("__rowid")
    return cols


def collapsed_sheets_in_sql(ir: dict, sql: str) -> list:
    """Sheets whose arms in a cached query cannot express the rule, because an
    alias folded its distinct fields onto ONE column ("paid <= incurred" written
    as "X <= X").

    A sheet qualifies only on BOTH counts: none of the rule's own field names
    appear in its arms (proof those arms were written in another sheet's
    spelling), and the arms together touch fewer columns than the rule has
    distinct fields (proof the spellings collapsed). A sheet compiled from the
    rule's own columns therefore never qualifies, nor does a sound alias that
    keeps the fields apart."""
    spec = TEMPLATE_CATALOG.get((ir or {}).get("template"))
    if not spec or not sql:
        return []
    try:
        refs = {f for f in spec["fields"]((ir or {}).get("params") or {})
                if isinstance(f, str)}
    except Exception:
        return []
    if len(refs) < 2:
        return []
    per_sheet: dict[str, set] = {}
    for arm in sql.split("\nUNION ALL\n"):
        for sheet in _re.findall(r'FROM\s+"([^"]+)"', arm):
            if sheet in _REFERENCE_TABLE_NAMES:
                continue
            per_sheet.setdefault(sheet, set()).update(_arm_columns(arm))
    return [sh for sh, cols in per_sheet.items()
            if not (cols & refs) and len(cols) < len(refs)]


def drop_sheet_arms(sql: str, sheets) -> str | None:
    """`sql` with every UNION arm reading from `sheets` removed, or None when that
    cannot be done safely — nothing would be left, or the query is not a plain
    union of per-arm SELECTs (an aggregate compiled over a row union reads its
    sheets inside a subquery, and cutting text out of that would corrupt it)."""
    drop = set(sheets or [])
    if not drop or not sql:
        return None
    arms = sql.split("\nUNION ALL\n")
    if len(arms) < 2:
        return None
    kept = []
    for arm in arms:
        froms = set(_re.findall(r'FROM\s+"([^"]+)"', arm)) - _REFERENCE_TABLE_NAMES
        if froms and froms <= drop:
            continue                     # arm reads only the collapsed sheet(s)
        if froms & drop:
            return None                  # mixed arm — not safe to cut
        kept.append(arm)
    if not kept or len(kept) == len(arms):
        return None
    return "\nUNION ALL\n".join(kept)


def sheets_in_sql(sql: str) -> list:
    """The BDX sheet names a compiled query reads from, in order. Sheets are always
    quoted identifiers; reference tables never are. Used to preserve a stale rule's
    original sheet targeting when nothing better is available."""
    out = []
    for name in _re.findall(r'FROM\s+"([^"]+)"', sql or ""):
        if name not in _REFERENCE_TABLE_NAMES and name not in out:
            out.append(name)
    return out


# Reference tables a compiled query may join — never a BDX sheet.
_REFERENCE_TABLE_NAMES = {"uszips", "postal_reference", "postal_state",
                          "postal_country", "country_currency"}


def recompile_ir_for_sheets(ir: dict, sheets) -> str:
    """Recompile an IR against an explicit list of sheets (used to refresh a stale
    cached query at validation time). Every field is mapped to every candidate
    sheet; `compile_ir` then keeps only the sheets that carry ALL of the rule's
    fields, exactly as it does at contract upload."""
    params = (ir.get("params") or {})
    spec = TEMPLATE_CATALOG.get(ir.get("template"))
    refs = spec["fields"](params) if spec else []
    sheets = list(sheets or [])
    if not sheets or not refs:
        raise CompileError("cannot recompile: no target sheet or no fields")
    return compile_ir(ir, {f: sheets for f in refs}, sheets[0])


# ---------------------------------------------------------------------
# Integrity guard — every catalog template must have a code builder
# ---------------------------------------------------------------------

def assert_builders_cover_catalog():
    """Fail fast if a template in the catalog has no SQL builder (or vice versa).
    A template with no builder is a non-executable rule waiting to happen; call
    this at import/startup so data (catalog) and code (builders) stay in lockstep.
    """
    cat = set(TEMPLATE_CATALOG.keys())
    code = set(_BUILDERS.keys())
    missing_builder = cat - code
    if missing_builder:
        raise RuntimeError(
            f"templates with no compiler builder: {sorted(missing_builder)}"
        )
    orphan_builder = code - cat
    if orphan_builder:
        raise RuntimeError(
            f"builders with no catalog template: {sorted(orphan_builder)}"
        )


assert_builders_cover_catalog()


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------


def _prune_scope_to_sheet(params: dict, sheet: str, field_to_sheet: dict) -> dict:
    """Drop scope predicates whose column is NOT on `sheet`.

    A rule compiles to a SINGLE-sheet SELECT, so its scope can only reference that
    sheet's columns. On a multi-sheet template the mapper may put a scope column on
    a DIFFERENT sheet than the rule runs on (e.g. a wrap-up "Project Name" lives on
    the UNIT schedule while a policy-period rule runs on the POLICY schedule) — a
    predicate on such a column would raise a DuckDB binder error and kill the whole
    rule. Dropping the un-evaluatable predicate is the safe choice: never worse than
    the pre-fix behaviour (which dropped the ENTIRE scope), and it keeps every
    same-sheet predicate that CAN be checked. Returns `params` unchanged when there
    is nothing to prune. Purely structural — no column names are hardcoded."""
    scope = params.get("scope")
    if not isinstance(scope, dict) or not scope:
        return params

    def _on_sheet(col) -> bool:
        v = field_to_sheet.get(col)
        if v is None:
            return False
        sheets = v if isinstance(v, (list, tuple, set)) else [v]
        return sheet in sheets

    pruned = {}
    for k, val in scope.items():
        if str(k).strip().lower() in _OR_SCOPE_KEYS:
            members = val if isinstance(val, (list, tuple)) else [val]
            kept = []
            for d in members:
                if isinstance(d, dict):
                    dd = {kk: vv for kk, vv in d.items() if _on_sheet(kk)}
                    if dd:
                        kept.append(dd)
            if kept:
                pruned[k] = kept
        elif _on_sheet(k):
            pruned[k] = val
    if set(pruned) == set(scope):
        return params
    out = dict(params)
    if pruned:
        out["scope"] = pruned
    else:
        out.pop("scope", None)
    return out


def compile_ir(ir: dict, field_to_sheet: dict, default_sheet: str | None = None,
               aliases: dict | None = None) -> str:
    """Compile a validated IR into one DuckDB SELECT (the violating rows).

    `field_to_sheet` maps every output-template field name to the sheet(s) it
    lives in — value may be a single sheet name OR a list of sheets. When a rule's
    field(s) appear on SEVERAL sheets (multi-sheet templates repeat the same
    columns across schedules), the rule FANS OUT: it is compiled once per sheet
    that contains ALL of its fields and the per-sheet SELECTs are UNION ALL'd, so
    one rule validates every sheet that has the column — not just the first.

    `aliases` ({field: {sheet: local_name}}, see OutputSchema.field_aliases)
    extends the fan-out to sheets carrying the SAME logical column under a
    DIFFERENT header ("POL_NO" vs "Policy number") — each such arm's SQL is
    written in that sheet's own spelling, so one rule covers every sheet with
    the concept, however the header is spelt.

    `default_sheet` is the fallback table for a field NOT in `field_to_sheet`
    (an unmapped column the template doesn't list): the rule still compiles,
    targeting that sheet, and the runtime checks the column exists before running.
    """
    template = ir.get("template")
    spec = TEMPLATE_CATALOG.get(template)
    if not spec:
        raise CompileError(f"unknown template: {template!r}")

    params = ir.get("params") or {}
    refs = spec["fields"](params)
    if not refs:
        raise CompileError("IR references no fields")

    aliases = aliases or {}

    def _sheets_of(f):
        v = field_to_sheet.get(f)
        base = [] if v is None else (
            list(v) if isinstance(v, (list, tuple, set)) else [v])
        base += [s for s in (aliases.get(f) or {}) if s not in base]
        if not base:
            return [default_sheet] if default_sheet else []
        return base

    def _params_for_sheet(sheet):
        """The IR's params with field names rewritten to this sheet's own
        spelling for any ref that is aliased there — identity otherwise."""
        if not any(sheet in (aliases.get(f) or {}) for f in refs):
            return params
        from contract_upload_services.rule_ir import remap_ir_fields
        local = remap_ir_fields(ir, lambda n: (aliases.get(n) or {}).get(sheet))
        return local.get("params") or {}

    def _collapses_on(sheet) -> bool:
        """True when this sheet's aliases fold TWO OF THE RULE'S OWN fields onto
        ONE column — "paid <= incurred" becoming "X <= X".

        Not a heuristic about which alias is right: a rule that compares two
        columns cannot be expressed against one, so the arm can only ever be
        noise. It compares the cell with itself (never a violation) while its
        companion type guard still flags every non-numeric cell of that column —
        which is how a claims rule came to report "Program ID must be a number"
        on a valid programme code, under a heading about paid and incurred loss.
        Nothing legitimate is lost by dropping the arm: the sheets that really
        carry the rule's fields are compiled from the same list."""
        local = [(aliases.get(f) or {}).get(sheet, f) for f in refs]
        return len(set(local)) < len(set(refs))

    # Sheets that contain EVERY referenced field (a rule needs all its fields on
    # the same sheet to run there). Order follows the first field's sheet order.
    per_field = [_sheets_of(f) for f in refs]
    common = set(per_field[0])
    for sl in per_field[1:]:
        common &= set(sl)
    target_sheets = [s for s in per_field[0] if s in common]
    if not target_sheets:
        raise CompileError(
            f"no single sheet contains all fields {refs}; cannot compile"
        )

    # Restrict to the schedules the clause actually names (e.g. "between Schedule
    # G, H, I, J"), if any — otherwise the rule spans every sheet with the column.
    target_sheets = _filter_sheets_by_scope(target_sheets, params)

    # Drop the sheets an alias would collapse the rule onto one column (see
    # _collapses_on). A sheet the rule reaches by its OWN spellings is never
    # dropped — _collapses_on is identity there — so this can only remove arms
    # aliasing created.
    target_sheets = [s for s in target_sheets if not _collapses_on(s)]
    if not target_sheets:
        raise CompileError(
            f"every candidate sheet folds fields {refs} onto one column; "
            "cannot compile"
        )

    builder = _BUILDERS[template]

    # AGGREGATE templates across MULTIPLE sheets: union the ROWS first, then
    # aggregate ONCE over the combined rows (a true cross-sheet SUM/COUNT). For
    # row-level templates the opposite is right — fan out per sheet and UNION the
    # per-row results.
    if template in _AGGREGATE_TEMPLATES and len(target_sheets) > 1:
        source = _row_union(target_sheets, refs, aliases)
        return builder(target_sheets[0],
                       _prune_scope_to_sheet(params, target_sheets[0], field_to_sheet),
                       source=source, sheet_expr="MIN(__sheet)")

    # Compile once per target sheet — with each ref spelt the way THAT sheet
    # spells it (aliases) — pruning each rule's scope to the columns that
    # actually live on THAT sheet (a cross-sheet scope column can't be referenced
    # in a single-sheet query — see _prune_scope_to_sheet).
    parts = [builder(sheet,
                     _prune_scope_to_sheet(_params_for_sheet(sheet), sheet,
                                           field_to_sheet))
             for sheet in target_sheets]
    return "\nUNION ALL\n".join(parts) if len(parts) > 1 else parts[0]
