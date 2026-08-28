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

import re as _re

from contract_upload_services.rule_ir import TEMPLATE_CATALOG


class CompileError(Exception):
    """Raised when an IR cannot be turned into a runnable query."""


# Jaro-Winkler thresholds for enum matching — a 3-tier decision:
#   sim >= MATCH        → the two values are the SAME (apply the rule)
#   WARN <= sim < MATCH → AMBIGUOUS → emit a 'warning' ("may differ, verify")
#   sim <  WARN         → DIFFERENT (no match)
# Tuned from real data: variants/typos score 0.88-0.98 (night club↔Nightclubs
# 0.98, nite club 0.89), genuinely different classes score ~0.61-0.77
# (Construction↔Habitational 0.61, Real Estate↔Retail 0.77) — so a 0.80 warn
# floor keeps clearly-different values out of the warning band. Env-tunable.
import os as _os
_ENUM_MATCH_THRESHOLD = float(_os.getenv("KAVACHIO_ENUM_MATCH_THRESHOLD", "0.90"))
_ENUM_WARN_THRESHOLD  = float(_os.getenv("KAVACHIO_ENUM_WARN_THRESHOLD", "0.80"))
# Minimum length (of the SHORTER of cell/value, after normalization) for the fuzzy
# WARN band to be trusted. jaro_winkler over-scores very short strings: a 2-char
# state code like 'GA' scores 0.85 vs 'Guam' (shared 'g' prefix + an 'a'), a false
# "may match excluded value" warning. Below this floor only an exact/near-exact
# (>= MATCH) hit counts, so short-code equality ('GU'->Guam) still fires while short
# near-misses do not. Env-tunable.
_ENUM_MIN_FUZZY_LEN = int(_os.getenv("KAVACHIO_ENUM_MIN_FUZZY_LEN", "4"))


# ---------------------------------------------------------------------
# Low-level SQL helpers (deterministic, injection-safe)
# ---------------------------------------------------------------------

def _q(name: str) -> str:
    """Quote an identifier (table/column)."""
    return '"' + str(name).replace('"', '""') + '"'


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


NUMERIC_MATCH_DECIMALS = int(_os.getenv("KAVACHIO_NUMERIC_MATCH_DECIMALS", "2"))


def _round(expr: str, p: dict | None = None) -> str:
    """Round a numeric SQL expression to the precision money is reported at, so a
    formula rule only ever raises an exception on a difference visible in the BDX
    (see the fuller note in python-services/.../rule_compiler.py). Runs BEFORE
    `tolerance`, so no rule becomes stricter. `decimals` overrides per rule."""
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
    """|left - right| at the comparison precision. The difference is rounded as
    well as the operands: subtracting two rounded doubles reintroduces a float
    tail (111.23 - 111.22 = 0.010000000000005116) that would push a pair sitting
    exactly ON the tolerance back over it."""
    return _round(f"ABS({_round(left, p)} - {_round(right, p)})", p)


def _num(col: str) -> str:
    """VARCHAR → DOUBLE with comma/currency/space stripping."""
    c = _q(col)
    return (f"TRY_CAST(REPLACE(REPLACE(REPLACE({c}, ',', ''), '$', ''), ' ', '') "
            f"AS DOUBLE)")


def _date(col: str) -> str:
    """VARCHAR → DATE, tolerant of the date spellings BDX files actually use.

    Every BDX column is loaded as VARCHAR, so a date cell may be ISO
    ('2026-01-03'), compact 'YYYYMMDD' ('20260103'), US 'MM/DD/YYYY', or an ISO
    timestamp. Bare TRY_CAST(... AS DATE) only accepts ISO and silently returns
    NULL for the others — which makes a date rule SKIP those rows (a dead check).
    COALESCE across the common spellings so a date rule evaluates every row
    whatever the source system's format. Generic: no per-file / per-column /
    per-contract assumption — just the ordered list of formats seen in practice,
    ISO first so already-correct data is untouched."""
    c = f"TRIM({_q(col)})"
    return (
        "COALESCE("
        f"TRY_CAST({c} AS DATE), "                              # ISO 2026-01-03
        f"TRY_CAST({c} AS TIMESTAMP)::DATE, "                   # ISO datetime
        f"TRY_CAST(TRY_STRPTIME({c}, '%Y%m%d') AS DATE), "      # compact 20260103
        f"TRY_CAST(TRY_STRPTIME({c}, '%m/%d/%Y') AS DATE), "    # US 01/03/2026
        f"TRY_CAST(TRY_STRPTIME({c}, '%m-%d-%Y') AS DATE)"      # US 01-03-2026
        ")"
    )


def _present(col: str) -> str:
    c = _q(col)
    return f"({c} IS NOT NULL AND TRIM({c}) <> '')"


def _empty(col: str) -> str:
    c = _q(col)
    return f"({c} IS NULL OR TRIM({c}) = '')"


_CMP_OPS = {"<", "<=", ">", ">=", "=", "!=", "<>"}


# ---------------------------------------------------------------------
# Enum matching — normalize + fuzzy so contract wording matches the sheet's
# actual values despite spelling / spacing / punctuation differences.
#   "Nightclubs"  (contract)  ≈  "night club"        (sheet)
#   "migrant real estate risks"  ≈  "migrant realestate"
# Normalization strips case + every non-alphanumeric char (handled identically
# in Python for the literal and in SQL for the column); jaro_winkler absorbs the
# remaining differences (plurals, dropped words, typos). This does NOT cover
# true SEMANTIC synonyms ("General Contractor" == "Construction") — those belong
# in the curated vocabulary table, not fuzzy string distance.
# ---------------------------------------------------------------------

def _norm_py(value) -> str:
    """Compile-time normalization of an enum literal (mirrors _norm_sql)."""
    return _re.sub(r"[^a-z0-9]", "", str(value).lower())


def _norm_sql(col: str) -> str:
    """Runtime normalization of a column value (mirrors _norm_py)."""
    return f"regexp_replace(lower(trim({_q(col)})), '[^a-z0-9]', '', 'g')"


def _tokens_py(value) -> list:
    """Compile-time WORD tokens of an enum literal (mirrors _tokens_sql)."""
    return [t for t in _re.split(r"[^a-z0-9]+", str(value).lower()) if t]


def _tokens_sql(field: str, alias: str | None = None) -> str:
    """Runtime WORD tokens of a column value (mirrors _tokens_py)."""
    col = f"{alias}.{_q(field)}" if alias else _q(field)
    return (f"list_filter(string_split_regex(lower(trim({col})), '[^a-z0-9]+'), "
            f"w -> w <> '')")


def _list_lit(items) -> str:
    """A typed VARCHAR[] literal (typed so an empty list still binds)."""
    return "CAST([" + ", ".join(_lit(x) for x in items) + "] AS VARCHAR[])"


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


def _enum_values_sql(values: list, field: str | None = None,
                     variation_values: list | None = None) -> str:
    """Render (display, match_token, word_tokens) triples as a VALUES list.
    `display` keeps the contract's ORIGINAL wording for the human-readable message
    ("United States of America"); `match_token` is the vocab CANONICAL token ("us")
    so it lines up with the identically-canonicalized cell (see _vocab_cell_expr);
    `word_tokens` is the value's word list (["general","liability"]) used by the
    shared-token guard in _enum_zone_select.

    `word_tokens` is deliberately EMPTY whenever the vocab remapped this value
    (canonical token != its own normalized form): in that case the scored strings
    are canonical tokens, not surface words, so the surface-word guard must not
    apply. An empty list makes the guard a no-op for that value (see below).

    `variation_values` (optional) are extra surface spellings of the same values
    (AI-recommended or data-derived, e.g. "Specialty" for "Palms Specialty
    Insurance Company Inc.") folded into the SAME match set, so a cell matching
    any variation also passes. Rows are DEDUPED by canonical token, so variations
    that collapse to a token already present do not bloat the VALUES list."""
    from contract_upload_services.vocabulary import canonical_token
    rows, seen = [], set()
    for v in list(values) + list(variation_values or []):
        tok = canonical_token(v, field)
        if not tok or tok in seen:
            continue
        seen.add(tok)
        toks = _tokens_py(v) if tok == _norm_py(v) else []
        rows.append(f"({_lit(str(v))}, {_lit(tok)}, {_list_lit(toks)})")
    if not rows:
        raise CompileError("enum has no usable values after normalization")
    return ", ".join(rows)


def _enum_zone_select(sheet: str, field: str, values: list, *,
                      polarity: str, scope: dict | None,
                      variation_values: list | None = None) -> str:
    """Build the 3-tier enum query.

    For each row, a LATERAL subquery finds the single best-matching enum value
    (`m.v`) and its jaro-winkler similarity (`m.s`) against the normalized cell.
    The row is then classified:

      polarity='exclude' (value_not_in_set):
        m.s >= MATCH  → violation  (cell IS one of the excluded values)
        WARN..MATCH   → warning    (cell may be an excluded value)
      polarity='include' (value_in_set):
        m.s <  WARN   → violation  (cell is NOT any allowed value)
        WARN..MATCH   → warning    (cell may not be an allowed value)

    A `zone` column ('violation' | 'warning') is emitted; execute_rule maps
    'violation' → the rule's severity and 'warning' → 'warning'. Numeric/date
    templates are unaffected — this fuzzy logic is string-enum only.
    """
    from contract_upload_services.vocabulary import canonical_token
    qf = _q(field)
    # Normalize the cell ONLY through the canonical tokens this rule references
    # (its own values) so the SQL carries the contract's values, not the whole
    # vocab class (territory us/uk/ca → just us when the contract says US).
    _match_vals = list(values) + list(variation_values or [])
    relevant = {t for t in (canonical_token(v, field) for v in _match_vals) if t}
    nct = _vocab_cell_expr(field, "t", relevant_canon=relevant)
    values_sql = _enum_values_sql(values, field, variation_values)
    M, W, L = _ENUM_MATCH_THRESHOLD, _ENUM_WARN_THRESHOLD, _ENUM_MIN_FUZZY_LEN

    if polarity == "exclude":
        # An exact/near-exact hit (>= MATCH) is always a violation. The fuzzy WARN
        # band only fires when the SHORTER string is long enough for jaro-winkler
        # to be meaningful (m.minlen >= L) — otherwise a 2-char code like 'GA'
        # spuriously warns against 'Guam'. Short-code equality ('GU'→Guam) still
        # lands in the MATCH band and is unaffected.
        score_filter = f"(m.s >= {M} OR (m.s >= {W} AND m.minlen >= {L}))"
        zone = f"CASE WHEN m.s >= {M} THEN 'violation' ELSE 'warning' END"
        reason = (
            f"CASE WHEN m.s >= {M} "
            f"THEN {_lit(field)} || ' matches excluded value ''' || m.v || "
            f"''' (similarity ' || round(m.s, 2) || ')' "
            f"ELSE {_lit(field)} || ' value ''' || t.{qf} || ''' may match excluded "
            f"value ''' || m.v || ''' (similarity ' || round(m.s, 2) || "
            f"') — values may differ, please verify' END"
        )
    elif polarity == "include":
        # Anything short of a strong match is flagged. The soft "may not match"
        # WARN band needs the shorter string long enough to trust jaro-winkler
        # (m.minlen >= L); a short code that only fuzzy-matches an allowed value is
        # a firm violation (it is NOT that value), not an ambiguous warning.
        score_filter = f"m.s < {M}"
        warn_cond = f"(m.s >= {W} AND m.minlen >= {L})"
        zone = f"CASE WHEN {warn_cond} THEN 'warning' ELSE 'violation' END"
        reason = (
            f"CASE WHEN {warn_cond} "
            f"THEN {_lit(field)} || ' value ''' || t.{qf} || ''' may not match allowed "
            f"value ''' || m.v || ''' (similarity ' || round(m.s, 2) || "
            f"') — values may differ, please verify' "
            f"ELSE {_lit(field)} || ' value ''' || t.{qf} || ''' is not in the allowed set' END"
        )
    else:
        raise CompileError(f"enum polarity must be include|exclude, got {polarity!r}")

    where = f"(t.{qf} IS NOT NULL AND TRIM(t.{qf}) <> '') AND {score_filter}"
    # A scope filter on the rule's OWN field is self-contradictory for a set rule
    # ("exclude Puerto Rico WHERE Insured Country = 'us'" can never match) — drop
    # it so the check actually evaluates instead of silently flagging nothing.
    scope = {k: v for k, v in (scope or {}).items()
             if str(k).strip().lower() != str(field).strip().lower()}
    scope_sql = _scope_clause_alias(scope, "t")
    if scope_sql:
        where = f"{where} AND {scope_sql}"

    # SHARED-TOKEN GUARD (the fuzzy score `s`, below the MATCH band, is the LESSER
    # of the whole-string similarity and the similarity of the DISTINGUISHING words).
    #
    # Normalization concatenates a phrase into one blob ("General Liability" →
    # 'generalliability'), so jaro-winkler scores two DIFFERENT values highly when
    # they merely share a boilerplate word — and in insurance nearly every product
    # line ends in one: 'generalliability' vs 'erisaliability' = 0.84, vs
    # 'eplliability' = 0.84. Both land in the WARN band and warn on every row of a
    # plain General Liability bordereau against exclusions the book never touches.
    #
    # The fix compares what actually distinguishes the two values: drop the words
    # they have in common and re-score the residue ('general' vs 'erisa' = 0.56 →
    # below WARN → no match). Purely structural — no word list, no per-field or
    # per-contract knowledge; the data supplies the shared words.
    #
    # Deliberately inert in four cases, so nothing that matches today stops:
    #   • raw >= MATCH  → an exact/near-exact hit is still a hit ("nite club" ≈
    #                     "night club", vocab-canonicalized pairs that score 1.0).
    #   • either residue empty → one value's words are a SUBSET of the other's
    #                     ("General Liability" ⊂ "Commercial General Liability"), a
    #                     genuine containment match, not a coincidence.
    #   • one residue PREFIXES the other → an abbreviation, not a difference
    #                     ("Arch Re" vs "Arch Reinsurance" leaves 're' / 'reinsurance';
    #                     "Workers Comp" vs "Workers Compensation" leaves 'comp' /
    #                     'compensation'). Truncation is what jaro-winkler is for.
    #   • no shared words → the residues ARE the originals, so `s` is unchanged.
    ctok = _tokens_sql(field, "t")
    scored = (
        f"SELECT x.v AS v, jaro_winkler_similarity({nct}, x.vn) AS raw, "
        f"LEAST(length({nct}), length(x.vn)) AS minlen, "
        f"{ctok} AS ct, x.vt AS vt "
        f"FROM (VALUES {values_sql}) AS x(v, vn, vt)"
    )
    residual = (
        f"SELECT v, raw, minlen, "
        f"array_to_string(list_filter(ct, w -> NOT list_contains(vt, w)), '') AS rc, "
        f"array_to_string(list_filter(vt, w -> NOT list_contains(ct, w)), '') AS rv "
        f"FROM ({scored}) AS p"
    )
    best = (
        f"SELECT v, CASE WHEN raw >= {M} OR rc = '' OR rv = '' "
        f"OR starts_with(rc, rv) OR starts_with(rv, rc) THEN raw "
        f"ELSE LEAST(raw, jaro_winkler_similarity(rc, rv)) END AS s, minlen "
        f"FROM ({residual}) AS q ORDER BY s DESC LIMIT 1"
    )
    return (
        f"SELECT t.__rowid AS row_id, {_lit(sheet)} AS sheet, {_lit(field)} AS field, "
        f"{reason} AS reason, t.{qf} AS actual_value, {zone} AS zone "
        f"FROM {_q(sheet)} AS t, LATERAL ({best}) AS m WHERE {where}"
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
    reason = (f"{_lit(field + ' must be a number, but found ')} || "
              f"{_q(field)} || {_lit('.')}")
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
    inc = _enum_zone_select(sheet, f, allowed, polarity="include",
                            scope=p.get("scope"),
                            variation_values=p.get("variation_values"))
    # Optional `excluded` makes this ONE rule for "must be in allowed, EXCLUDING …"
    # (e.g. Territory: United States *excluding* Puerto Rico / USVI). A row is
    # flagged if it is NOT in the allowed set OR IS in the excluded set.
    excluded = p.get("excluded") or []
    if isinstance(excluded, list) and excluded:
        exc = _enum_zone_select(sheet, f, excluded, polarity="exclude",
                                scope=p.get("scope"))
        return f"{inc}\nUNION ALL\n{exc}"
    return inc


def _b_value_not_in_set(sheet, p):
    f = p["field"]
    excluded = p.get("excluded") or []
    if not isinstance(excluded, list) or not excluded:
        raise CompileError("value_not_in_set needs a non-empty 'excluded' list")
    return _enum_zone_select(sheet, f, excluded, polarity="exclude",
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



def _cmp_bool(field, op, value, variations=None) -> str:
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
    Ignored for the ordinal ops (a numeric threshold has no spelling variants)."""
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
        forms, seen = [], set()
        for x in [value, *vv]:
            if x is None:                   # skip a JSON null spelling, not 'none'
                continue
            s = str(x).strip().lower()
            if s and s not in seen:
                seen.add(s)
                forms.append(s)
        if not forms:                       # value itself was blank
            forms = [str(value).strip().lower()]
        if len(forms) == 1:
            return f"{lhs} {'=' if op == '=' else '<>'} {_lit(forms[0])}"
        inlist = ", ".join(_lit(x) for x in forms)
        return f"{lhs} {'IN' if op == '=' else 'NOT IN'} ({inlist})"
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
    target_ok = _cmp_bool(tf, top, tv, p.get("variation_values"))
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
    target_ok = _cmp_bool(tf, top, tv, p.get("variation_values"))
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
        where = f"{nonnull} AND ({' OR '.join(conds)})"
        reason = f"{s}->{e} duration must be {' and '.join(parts)} {unit}(s)"
        return _select(sheet, s, reason, where, s)

    # SINGLE-BOUND form: op is the COMPLIANT relation; violation = NOT(duration op value).
    op = p.get("op")
    if op not in _CMP_OPS:
        raise CompileError(
            "period_duration needs min/max (range) or a valid op + value")
    op = "<>" if op == "!=" else op
    val = _num_lit(p["value"])
    where = f"{nonnull} AND NOT ({dur} {op} {val})"
    return _select(sheet, s, f"{s}->{e} duration must be {op} {val} {unit}(s)", where, s)


def _b_cross_field_math(sheet, p):
    res, l, r = p["result_field"], p["left_field"], p["right_field"]
    op = p["operator"]
    if op not in ("+", "-", "*", "/"):
        raise CompileError("cross_field_math operator must be + - * /")
    tol = float(p.get("tolerance_pct") or 0) / 100.0
    # Optional second band ("flag vs auto-reject"): within tolerance_pct is
    # compliant; beyond reject_pct is a hard violation (rule severity); the band
    # between is a soft 'warning'. Active only when reject_pct parses to a number
    # strictly greater than tolerance_pct; otherwise behaviour is unchanged (no
    # zone column emitted).
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
    expected = f"({nl} {op} {nr})"
    nonnull = f"{nres} IS NOT NULL AND {nl} IS NOT NULL AND {nr} IS NOT NULL"
    if op == "/":
        nonnull += f" AND {nr} <> 0"
    # Reporting-precision comparison (see _round) before the ±% band is measured.
    exp_r = _round(expected, p)
    dev = _abs_dev(nres, expected, p)
    tol_expr = f"{repr(tol)} * ABS({exp_r})"
    where = f"{nonnull} AND {dev} > {tol_expr}"
    # Rows past the reject band are 'violation' (rule severity); rows in the
    # tolerance→reject band are a soft 'warning'. Emitted only when bands are on.
    zone_expr = None
    if bands:
        reject_expr = f"{repr(reject)} * ABS({exp_r})"
        zone_expr = f"CASE WHEN {dev} > {reject_expr} THEN 'violation' ELSE 'warning' END"
    # Any of the three operands can be the non-numeric one — de-dup by field name
    # so a rule where e.g. left_field == right_field doesn't double-flag one cell.
    # When zoned, these hard "must be a number" rows carry a constant 'violation'
    # zone so every UNION arm exposes the same columns.
    not_numeric = "\nUNION ALL\n".join(
        _not_numeric_select(sheet, fld, zone_expr="'violation'" if bands else None)
        for fld in dict.fromkeys([res, l, r]))
    return (_select(sheet, res, f"{res} must equal {l} {op} {r}", where, res,
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
    # Equality is measured at reporting precision (see _round); the inequality
    # branches stay on raw values so a floor/ceiling is never crossed by rounding.
    dev = _abs_dev(nf, rhs, p)
    # "=" already treats a non-numeric `f` as a violation via the `nf IS NULL OR
    # ...` branch, so only `other` is genuinely uncovered there. "<>" and the
    # ordinal `else` branch don't cover either side. Add a not_numeric UNION only
    # for the side(s) not already implied, so a bad cell is never flagged twice
    # under two different reasons.
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
    not_numeric = "\nUNION ALL\n".join(
        _not_numeric_select(sheet, fld) for fld in dict.fromkeys(extra))
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
    # Same reporting-precision comparison as cross_field_compare — equality only.
    dev = _abs_dev(nf, rhs, p)
    # EXACT MATCH ("="): the field must EQUAL the greater/lesser value (the agent
    # keys the computed amount into the cell). NOT-EQUAL ("!=") is the inverse.
    # A ">=" / ">" bound is a REQUIRED FLOOR the field must MEET. In BOTH the
    # exact-match and floor cases a blank / non-numeric value (e.g. an un-evaluated
    # Excel formula string TRY_CAST can't parse) is a REQUIRED value that FAILS →
    # flag it rather than silently skip. A "<=" / "<" ceiling leaves a blank value
    # alone (there is nothing to cap). The base must be present to size the % side.
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
    not_numeric = "\nUNION ALL\n".join(
        _not_numeric_select(sheet, fld) for fld in dict.fromkeys(extra))
    return (_select(sheet, f,
                    f"{f} must {rel} the {word} of ({other}{tail}) or {p['value']}",
                    where, f)
            + "\nUNION ALL\n" + not_numeric)


def _row_union(sheets, cols):
    """A subquery that UNIONs the ROWS of several sheets, exposing __rowid, the
    referenced columns, and a __sheet label. An aggregate runs over this so it
    sums/counts the COMBINED rows across sheets — NOT one total per sheet."""
    sel = "__rowid" + ("".join(f", {_q(c)}" for c in cols) if cols else "")
    parts = [f"SELECT {sel}, {_lit(sh)} AS __sheet FROM {_q(sh)}" for sh in sheets]
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
    reason = f"{agg}({f}) breaches limit"
    from_sql = source or _q(sheet)            # single sheet, or a cross-sheet union
    sheet_lit = sheet_expr or _lit(sheet)     # 'SheetName', or MIN(__sheet) for a union
    return (
        f'SELECT MIN(__rowid) AS row_id, {sheet_lit} AS sheet, {_lit(f)} AS field, '
        f'{_lit(reason)} AS reason, {key_expr} AS policy_number, '
        f'CAST({agg_expr} AS VARCHAR) AS actual_value '
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


def compile_ir(ir: dict, field_to_sheet: dict, default_sheet: str | None = None) -> str:
    """Compile a validated IR into one DuckDB SELECT (the violating rows).

    `field_to_sheet` maps every output-template field name to the sheet(s) it
    lives in — value may be a single sheet name OR a list of sheets. When a rule's
    field(s) appear on SEVERAL sheets (multi-sheet templates repeat the same
    columns across schedules), the rule FANS OUT: it is compiled once per sheet
    that contains ALL of its fields and the per-sheet SELECTs are UNION ALL'd, so
    one rule validates every sheet that has the column — not just the first.

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


    def _sheets_of(f):
        v = field_to_sheet.get(f)
        if v is None:
            return [default_sheet] if default_sheet else []
        return list(v) if isinstance(v, (list, tuple, set)) else [v]

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

    builder = _BUILDERS[template]

    # AGGREGATE templates across MULTIPLE sheets: union the ROWS first, then
    # aggregate ONCE over the combined rows (a true cross-sheet SUM/COUNT). For
    # row-level templates the opposite is right — fan out per sheet and UNION the
    # per-row results.
    if template in _AGGREGATE_TEMPLATES and len(target_sheets) > 1:
        source = _row_union(target_sheets, refs)
        return builder(target_sheets[0], params,
                       source=source, sheet_expr="MIN(__sheet)")

    parts = [builder(sheet, params) for sheet in target_sheets]
    return "\nUNION ALL\n".join(parts) if len(parts) > 1 else parts[0]
