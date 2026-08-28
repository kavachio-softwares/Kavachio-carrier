"""
duckdb_validation.py
────────────────────
DuckDB-based BDX validation engine.

The idea (see validation_excel.docx, sections 17-19): instead of a per-rule code
engine, SQL is the universal rule language and DuckDB is the engine. For each
contract validation_rule, the LLM compiles a read-only SQL SELECT that returns
the FAILING rows. We guard the SQL (read-only + schema + dry-run), retry once on
failure, and if it still fails we mark the rule "cannot_process" and tell the
user. Compiled SQL is cached per rule (keyed by rule + schema hash) so the LLM is
called only once per rule.

Public entry point:
    run_validation(records_by_sheet, rules, contract=None, template_id=None,
                   session=None, max_exc=500) -> dict

Where `records_by_sheet` is the output of exporter.build_output_records:
    [ {"sheet": <name>, "records": [ {column_name: value, ...}, ... ]}, ... ]
"""

import os
import json
import uuid
import hashlib
import re
from datetime import datetime

import duckdb

from contract_upload_services.gemini_service import call_gemini


# ── DuckDB persistence (debug/testing only) ──────────────────────────────────
# By default the validation DB is in-memory (ephemeral). For testing you can set
# KAVACHIO_DUCKDB_PERSIST=true to write each run to its OWN file under
# KAVACHIO_DUCKDB_DIR (default /tmp), so you can open it later with the DuckDB
# CLI. A UNIQUE filename per run is essential — two concurrent validations (same
# template, different data) must never share a file or they'd lock/merge. Turn
# the flag off when done; the files are not auto-deleted (that's the point).
_DUCKDB_PERSIST = os.getenv("KAVACHIO_DUCKDB_PERSIST", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
_DUCKDB_DIR = os.getenv("KAVACHIO_DUCKDB_DIR", "/tmp")


# =====================================================================
# 1. Build an in-memory DuckDB from the resolved output records
# =====================================================================

def _sanitize_label(label):
    """Keep filenames filesystem-safe: letters, digits, dot, dash, underscore."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(label)).strip("-")[:60]


def _qid(name):
    """Quote an identifier (sheet / column name) for DuckDB, doubling any embedded
    double-quote. Column names keep ALL their special characters (%, $, (), &, /,
    #, spaces, …) — DuckDB accepts them inside a quoted identifier; the only char
    that needs escaping is the double-quote itself."""
    return '"' + str(name).replace('"', '""') + '"'


def _dedup_cols(cols):
    """Drop duplicate column names (keeping the first occurrence). DuckDB column
    names are CASE-INSENSITIVE and must be unique, but real templates/BDX files
    repeat headers — e.g. a pivot-style "Summary" sheet that lists the same
    column once per schedule. Without this, CREATE TABLE raises
    'Column with name X already exists'."""
    seen, out = set(), []
    for c in cols:
        key = str(c).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def build_connection(records_by_sheet, schema_cols=None, label=None):
    """Load each sheet as a DuckDB table (all columns VARCHAR + a synthetic
    integer __rowid = the 1-based data row). Returns (connection, tables) where
    tables = { sheet_name: {"columns": [...], "samples": {col: [vals]}} }.

    `schema_cols` (optional) is {sheet: [column_name, ...]} from the TEMPLATE
    structure. When given, tables are created with the full template column set
    even for sheets/columns that have no data in this run — this keeps the schema
    (and its hash) stable and shows the LLM every column it may reference.

    Everything is VARCHAR on purpose: the LLM uses TRY_CAST(... AS DOUBLE/DATE)
    so we never crash on a stray non-numeric value, and identifiers are always
    double-quoted so spaces / symbols in column names are safe.
    """
    # Default: ephemeral in-memory DB. When KAVACHIO_DUCKDB_PERSIST is on, write
    # to a UNIQUE file per call (uuid) so concurrent validation runs never share
    # a file → no lock contention, no data merge between the two threads.
    if _DUCKDB_PERSIST:
        try:
            os.makedirs(_DUCKDB_DIR, exist_ok=True)
            # Filename encodes the RUN so you can tell 10 files apart:
            #   validation_<label>_<YYYYmmdd-HHMMSS>_<uuid6>.duckdb
            # label carries template/contract ids (passed by run_validation); the
            # timestamp orders them; the short uuid guarantees uniqueness even if
            # two runs of the same template land in the same second.
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            lbl = _sanitize_label(label) if label else "run"
            fname = f"validation_{lbl}_{ts}_{uuid.uuid4().hex[:6]}.duckdb"
            db_path = os.path.join(_DUCKDB_DIR, fname)
            con = duckdb.connect(database=db_path)
            print(f"[DuckDB] PERSIST on — {lbl} → {db_path} "
                  f"(open with:  duckdb {db_path} )")
        except Exception as exc:
            print(f"[DuckDB] persist failed ({exc}); falling back to in-memory.")
            con = duckdb.connect(database=":memory:")
    else:
        con = duckdb.connect(database=":memory:")
    # SANDBOX: block all file/URL access so an AI-generated query can never read
    # the filesystem or network (e.g. read_csv('/etc/passwd'), httpfs, ATTACH).
    # Normal queries (in-memory or persisted-file) are unaffected.
    try:
        con.execute("SET enable_external_access=false")
    except Exception:
        pass
    tables = {}
    for block in records_by_sheet:
        raw_sheet = block["sheet"]
        # Normalize the TABLE name to the stripped form, because the compiler
        # (OutputSchema) strips sheet names — so a template sheet like "ITD BDX "
        # (trailing space) must become table "ITD BDX" or the rule's
        # FROM "ITD BDX" can't find it and the whole UNION-ALL rule errors out.
        sheet = str(raw_sheet).strip()
        records = block.get("records") or []

        # Columns: prefer the template column set (complete + stable); else fall
        # back to the union of keys present in the data. (Look up by the original
        # sheet key first, then the stripped form.)
        cols = list((schema_cols or {}).get(raw_sheet)
                    or (schema_cols or {}).get(sheet) or [])
        if not cols:
            seen = set()
            for rec in records:
                for k in rec.keys():
                    if k not in seen:
                        seen.add(k)
                        cols.append(k)
        cols = _dedup_cols(cols)   # repeated headers (e.g. Summary sheets) → unique

        coldefs = ", ".join(f'{_qid(c)} VARCHAR' for c in cols)
        create = f'CREATE TABLE {_qid(sheet)} (__rowid INTEGER{"," + coldefs if coldefs else ""})'
        con.execute(create)

        if cols and records:
            placeholders = ", ".join(["?"] * (len(cols) + 1))
            ins = f'INSERT INTO {_qid(sheet)} VALUES ({placeholders})'
            rows = []
            for i, rec in enumerate(records, start=1):
                vals = [i]
                for c in cols:
                    v = rec.get(c)
                    vals.append(None if v is None else str(v))
                rows.append(vals)
            con.executemany(ins, rows)

        # Collect up to 3 distinct sample values per column to help the LLM
        # understand formats / enums actually present in the data.
        samples = {}
        for c in cols:
            seen_s, vals = set(), []
            for rec in records:
                v = rec.get(c)
                if v is None:
                    continue
                sv = str(v)
                if sv and sv not in seen_s:
                    seen_s.add(sv)
                    vals.append(sv)
                if len(vals) >= 3:
                    break
            if vals:
                samples[c] = vals

        tables[sheet] = {"columns": cols, "samples": samples}

    # Ensure EVERY template sheet exists as a table — even ones with no data in
    # this run — so a rule that fans out across sheets (UNION ALL) never
    # references a missing table. Empty tables simply contribute zero violations.
    for raw_sheet, cols in (schema_cols or {}).items():
        sheet = str(raw_sheet).strip()
        if sheet in tables:
            continue
        cols = _dedup_cols(cols or [])
        coldefs = ", ".join(f'{_qid(c)} VARCHAR' for c in cols)
        con.execute(
            f'CREATE TABLE {_qid(sheet)} (__rowid INTEGER{"," + coldefs if coldefs else ""})')
        tables[sheet] = {"columns": cols, "samples": {}}

    return con, tables


# Name tokens that mark a policy-number column as a SECONDARY / historical /
# alternate identifier rather than the current primary policy number. A template
# can carry several columns all mapped to canonical "policy_number" at equal
# confidence (e.g. "Risksmith Policy Number", "Previous Policy Number",
# "UMR Policy Number"); these tokens demote the non-primary ones so the offending
# row is labelled with the real policy id, not its prior/alternate value.
_NON_PRIMARY_POLICY_TOKENS = (
    "previous", "prior", "expiring", "expired", "expiry", "old", "renewal",
    "renewing", "master", "group", "umr", "original", "parent",
)


def _policy_number_columns(structure):
    """Map each (stripped) sheet name -> the output column that holds the policy
    number, i.e. the column whose best `candidates` entry is canonical
    "policy_number". Used to label exceptions with a real policy id instead of
    the "Dataset-level" fallback. Returns {} when the structure carries no such
    mapping.

    When several columns tie on the policy_number candidate confidence, the one
    whose NAME carries a historical/alternate qualifier (previous, prior, umr,
    master, group, …) is demoted so the PRIMARY policy number wins."""
    out = {}
    for sh in (structure or {}).get("sheets", []) or []:
        name = str(sh.get("sheet_name", "")).strip()
        best_col, best_rank = None, None
        for c in sh.get("columns", []) or []:
            col_name = c.get("column_name")
            if not col_name:
                continue
            conf = None
            for cand in (c.get("candidates") or []):
                if cand.get("canonical") == "policy_number":
                    cc = cand.get("confidence") or 0.0
                    conf = cc if conf is None else max(conf, cc)
            if conf is None:
                continue
            lname = col_name.lower()
            is_primary = not any(tok in lname for tok in _NON_PRIMARY_POLICY_TOKENS)
            # Rank: prefer primary names, then higher confidence. First column
            # wins on a full tie (stable — only replace on a strictly better rank).
            rank = (1 if is_primary else 0, conf)
            if best_rank is None or rank > best_rank:
                best_rank, best_col = rank, col_name
        if best_col:
            out[name] = best_col
    return out


def label_exceptions_with_policy(exceptions, structure, blocks):
    """Fill each exception's `policy_number` from its offending output row so the
    review UI shows a real policy id rather than "Dataset-level".

    `blocks` is the validation-block list [{"sheet", "records":[{col: val}, ...]}]
    whose per-sheet 1-based order matches the DuckDB `__rowid` carried in each
    exception's `row`. Row-level value-set / range rules don't emit policy_number
    in their compiled SQL (each row IS a policy), so we resolve it here. Rules
    that already carry a policy_number, and grouped/aggregate exceptions with no
    `row`, are left untouched. Mutates `exceptions` in place."""
    pn_cols = _policy_number_columns(structure)
    if not pn_cols or not exceptions:
        return
    rows_by_sheet = {}
    for b in (blocks or []):
        key = str(b.get("sheet") or "").strip()
        rows_by_sheet.setdefault(key, []).extend(b.get("records") or [])
    for e in exceptions:
        if e.get("policy_number"):
            continue
        sheet = str(e.get("sheet") or "").strip()
        col = pn_cols.get(sheet)
        rows = rows_by_sheet.get(sheet)
        if not col or not rows:
            continue
        try:
            idx = int(e.get("row")) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(rows):
            v = rows[idx].get(col)
            if v not in (None, ""):
                e["policy_number"] = str(v)


def schema_text(tables):
    """Human/LLM-readable description of the loaded tables, with sample values."""
    lines = []
    for sheet, meta in tables.items():
        if not meta["columns"]:
            lines.append(f'Table "{sheet}": (no columns)')
            continue
        col_parts = []
        for c in meta["columns"]:
            ex = (meta.get("samples") or {}).get(c)
            col_parts.append(f'"{c}"' + (f" e.g. {ex}" if ex else ""))
        lines.append(f'Table "{sheet}" columns: ' + ", ".join(col_parts))
    return "\n".join(lines)


def schema_hash(tables):
    payload = {s: m["columns"] for s, m in tables.items()}
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def rule_hash(rule):
    payload = {
        "name": rule.get("rule_name"),
        "desc": rule.get("rule_description"),
        "spec": rule.get("rule_spec"),
        "target": rule.get("canonical_target"),
        "msg": rule.get("error_message"),
    }
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


# =====================================================================
# 2. LLM: compile a rule into a SQL query
# =====================================================================

REQUIRED_COLS = {"row_id", "sheet", "field", "reason"}

# Statement-level keywords that must never appear. (REPLACE is intentionally
# NOT here — it is a legitimate string function; CREATE/INSERT, the only
# dangerous forms, are already blocked.)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|pragma|"
    r"install|load|export|truncate|grant|revoke|call|merge|vacuum)\b",
    re.IGNORECASE,
)


def _build_prompt(rule, tables, prev_error=None):
    spec = rule.get("rule_spec") or {}
    target = rule.get("canonical_target") or {}
    field = target.get("output_field") or target.get("field") or ""
    retry = (
        f"\nThe PREVIOUS attempt failed with this error — fix it:\n{prev_error}\n"
        if prev_error else ""
    )
    return f"""SYSTEM:
You convert ONE insurance bordereau (BDX) validation rule into a single DuckDB
SQL SELECT query.

The BDX data is already loaded into these tables. EVERY column is VARCHAR, and
EVERY table has an integer column __rowid (the 1-based data row number):
{schema_text(tables)}

Write ONE read-only SQL query that returns ONLY THE ROWS THAT FAIL the rule
(the violations — not the passing rows).

The query MUST return exactly these output columns (this exact spelling):
  row_id   -> the __rowid of the failing row (from the table the field lives in)
  sheet    -> a string literal: the table/sheet name the row is from
  field    -> a string literal: the output field the rule targets
  reason   -> a short human explanation of WHY this row failed
You MAY also add: policy_number, actual_value.

Hard rules for the SQL:
  - SELECT or WITH only. Exactly ONE statement, no semicolons.
  - NEVER use insert/update/delete/create/alter/drop/attach/copy/pragma/install/load.
  - All columns are VARCHAR. For numbers use TRY_CAST("col" AS DOUBLE); for dates
    TRY_CAST("col" AS DATE). Skip rows where a needed cast is NULL, UNLESS the
    rule is specifically about missing/empty values.
  - Reference tables and columns with double quotes EXACTLY as named above.
  - Use ONLY the tables and columns listed above. If the rule needs a field that
    is not listed, return cannot_process (do NOT invent columns).
  - Aggregation/limits -> GROUP BY / HAVING. Uniqueness -> GROUP BY ... HAVING
    COUNT(*) > 1. Cross-sheet -> JOIN on a shared key.
  - For GROUP-level violations (aggregation/uniqueness), return ONE row per
    offending group: use MIN(__rowid) AS row_id, put the group key in
    policy_number, and describe the group total in reason.

DuckDB function notes (use these — avoid non-existent functions):
  - Empty / whitespace check: TRIM("col") = '' or LEN(TRIM("col")) = 0
    (do NOT use REGEXP_FULLMATCH — it does not exist).
  - Regex test: regexp_matches("col", 'pattern')   (returns true/false)
  - Case-insensitive compare: use ILIKE, or LOWER("col") = LOWER('x').
  - Contains text: "col" LIKE '%x%'.
  - Numbers may contain commas/currency symbols, so ALWAYS strip them first:
    TRY_CAST(REPLACE(REPLACE(REPLACE("col", ',', ''), '$', ''), ' ', '') AS DOUBLE).
  - Dates/timestamps: TRY_CAST("col" AS TIMESTAMP) (handles 'YYYY-MM-DD HH:MM:SS');
    use TRY_CAST("col" AS DATE) for plain dates.
  - Do NOT use file/URL functions (read_csv, read_parquet, glob, etc.) — they are
    blocked. Query only the tables above.
  - Prefer standard SQL; if unsure a function exists, use a simpler equivalent.

If the rule targets a SPECIFIC sub-type/coverage (e.g. "Primary CGL") but no
column distinguishes it, apply the check to all rows of the target field rather
than returning cannot_process — only return cannot_process when a REQUIRED field
is genuinely absent from every table.

RULE TO IMPLEMENT:
  name        : {rule.get('rule_name')}
  description : {rule.get('rule_description')}
  target field: {field}
  severity    : {rule.get('severity')}
  spec hint   : {json.dumps(spec, default=str)}
  error msg   : {rule.get('error_message')}
{retry}
Respond with STRICT JSON only (no markdown):
  {{ "sql": "<the SELECT query>", "explain": "<one line of what it checks>" }}
If the rule CANNOT be expressed as SQL over this data (needs information not in
the tables, or is purely qualitative/subjective), respond:
  {{ "sql": null, "cannot_process": "<short reason>" }}
"""


def _generate_sql(rule, tables, prev_error=None):
    """Call the LLM; return a dict {sql|None, explain?, cannot_process?}."""
    raw = call_gemini(_build_prompt(rule, tables, prev_error),
                      label=f"DuckSQL-rule-{rule.get('rule_id')}",
                      temperature=0)
    try:
        data = json.loads(raw)
    except Exception as e:
        return {"sql": None, "cannot_process": f"LLM returned non-JSON: {e}"}
    if not isinstance(data, dict):
        return {"sql": None, "cannot_process": "LLM returned unexpected shape"}
    return data


# =====================================================================
# 3. Guard + dry-run
# =====================================================================

def guard_sql(sql):
    """Static safety check. Returns (ok, cleaned_sql_or_reason)."""
    if not sql or not isinstance(sql, str):
        return False, "empty SQL"
    s = sql.strip().rstrip(";").strip()

    # Build a scrubbed copy with string literals, quoted identifiers and comments
    # removed. ALL keyword/structure checks run against this copy so that text
    # inside a reason string or a column name can never cause a false positive
    # (e.g. a ';' or the word 'create' inside a reason literal).
    scan = re.sub(r"'(?:''|[^'])*'", " ", s)
    scan = re.sub(r'"(?:[^"])*"', " ", scan)
    scan = re.sub(r"--[^\n]*", " ", scan)                    # line comments
    scan = re.sub(r"/\*.*?\*/", " ", scan, flags=re.DOTALL)  # block comments
    scan_stripped = scan.strip()

    # Multiple statements (only count ';' that are real, not inside strings).
    if ";" in scan_stripped:
        return False, "multiple statements are not allowed"
    if not (scan_stripped.lower().startswith("select")
            or scan_stripped.lower().startswith("with")):
        return False, "query must start with SELECT or WITH"
    m = _FORBIDDEN.search(scan)
    if m:
        return False, f"forbidden keyword: {m.group(0)}"
    return True, s


def dry_run(con, sql):
    """Execute with LIMIT 1 to prove it runs and returns the required columns."""
    try:
        rel = con.execute(f"SELECT * FROM ({sql}) AS _q LIMIT 1")
    except Exception as e:
        return False, f"SQL failed to run: {e}"
    have = {d[0].lower() for d in rel.description}
    missing = REQUIRED_COLS - have
    if missing:
        return False, f"query is missing required output columns: {sorted(missing)}"
    return True, None


# =====================================================================
# 4. Compile a rule (generate -> guard -> dry-run -> retry once)
# =====================================================================

def compile_rule(con, tables, rule, max_attempts=2):
    """Returns {"status": "ok"|"cannot_process", "sql": str|None,
               "message": str|None, "attempts": int}."""
    prev_error = None
    attempts = 0
    for attempt in range(max_attempts):
        attempts += 1
        # Isolate LLM failures per attempt: a bad/empty/non-JSON response (which
        # call_gemini raises on) must not abort the whole validation run — treat
        # it as a failed attempt so we retry, then mark cannot_process.
        try:
            gen = _generate_sql(rule, tables, prev_error)
        except Exception as llm_err:
            prev_error = f"LLM generation failed: {llm_err}"
            continue

        if gen.get("cannot_process"):
            return {"status": "cannot_process", "sql": None,
                    "message": str(gen["cannot_process"]), "attempts": attempts}

        ok, cleaned = guard_sql(gen.get("sql"))
        if not ok:
            prev_error = f"guard rejected the query: {cleaned}"
            continue

        ran, err = dry_run(con, cleaned)
        if not ran:
            prev_error = err
            continue

        return {"status": "ok", "sql": cleaned, "message": gen.get("explain"),
                "attempts": attempts}

    return {"status": "cannot_process", "sql": None,
            "message": f"could not compile to a runnable query after "
                       f"{attempts} attempts: {prev_error}",
            "attempts": attempts}


# =====================================================================
# 5. Execute a compiled SQL and map rows -> exceptions
# =====================================================================

def execute_rule(con, sql, rule, contract, max_rows):
    """Run the SQL; return list of structured exception dicts."""
    rel = con.execute(sql)
    colnames = [d[0].lower() for d in rel.description]
    out = []
    for row in rel.fetchmany(max_rows):
        d = dict(zip(colnames, row))
        # Fuzzy enum rules emit a per-row `zone`: 'warning' (ambiguous match in
        # the 0.80–0.90 band → soft, non-blocking warning) vs 'violation' (a
        # confident match → the rule's real severity). Non-enum rules have no
        # zone column, so they keep the rule's severity as before.
        zone = (d.get("zone") or "").lower()
        row_severity = (
            "warning" if zone == "warning"
            else (rule.get("severity") or "warning")
        )
        out.append({
            "severity": row_severity,
            "code": rule.get("rule_name") or "contract_rule",
            "sheet": d.get("sheet"),
            "row": d.get("row_id"),
            "column": d.get("field") or (rule.get("canonical_target") or {}).get("output_field"),
            "field": d.get("field") or (rule.get("canonical_target") or {}).get("output_field"),
            "rule_id": rule.get("rule_id"),
            "rule_name": rule.get("rule_name"),
            "contract_id": (contract or {}).get("id") or rule.get("contract_id"),
            "contract_filename": (contract or {}).get("filename"),
            # Verbatim contract clause + page so the UI can show "why" and link
            # straight to the source PDF (populated from validation_rule).
            "contract_clause_text": rule.get("source_verbatim_text"),
            "contract_clause_page": rule.get("source_page_number"),
            "policy_number": d.get("policy_number"),
            "actual_value": d.get("actual_value"),
            "reason": d.get("reason"),
            "message": (
                f"{rule.get('error_message')} ({d.get('reason')})"
                if rule.get("error_message") else d.get("reason")
            ),
            "error_class": "data_violation",
        })
    return out


# =====================================================================
# 6. Caching of compiled SQL in the ops `rule_sql` table
# =====================================================================

def _get_cached(session, rule_id, sh, rh):
    from db import RuleSql
    rec = (session.query(RuleSql)
           .filter(RuleSql.rule_id == rule_id)
           .order_by(RuleSql.id.desc())
           .first())
    if rec and rec.schema_hash == sh and rec.rule_hash == rh:
        return rec
    return None


def _save_cache(session, rule, template_id, sh, rh, result):
    from db import RuleSql
    rec = (session.query(RuleSql)
           .filter(RuleSql.rule_id == rule.get("rule_id"))
           .order_by(RuleSql.id.desc())
           .first())
    if rec is None:
        rec = RuleSql(rule_id=rule.get("rule_id"))
        session.add(rec)
    rec.contract_id = rule.get("contract_id")
    rec.template_id = template_id
    rec.schema_hash = sh
    rec.rule_hash = rh
    rec.sql_text = result.get("sql")
    rec.status = result.get("status")
    rec.message = result.get("message")
    rec.attempts = result.get("attempts") or 0
    session.commit()
    return rec


# =====================================================================
# 7. PUBLIC ENTRY POINT
# =====================================================================

def _compiled_sql_for(rule):
    """Return (sql, None) using the deterministic SQL produced at contract upload
    (`compile_ir`, stored in rule_spec.compiled_sql) — NO LLM. Returns
    (None, reason) when the rule has no compiled query so the caller can flag the
    clause to the user instead of silently skipping it."""
    spec = rule.get("rule_spec")
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except Exception:
            spec = None
    if isinstance(spec, dict) and spec.get("compiled_sql"):
        return spec["compiled_sql"], None
    if rule.get("compiled_sql"):
        return rule["compiled_sql"], None
    return None, ("This clause has no compiled validation query — re-upload the "
                  "contract to regenerate its rules.")


def _missing_columns(sql, tables):
    """Columns the query references that are NOT present in the BDX it targets.

    "First check the column, then check the rule": a rule may reference a column
    the BDX doesn't have (e.g. a high-confidence rule whose Output-Template column
    was never added). Rather than error, we detect those columns up front and skip
    the rule with a clear message. Returns [] when everything it needs is present.

    Cross-sheet aggregates (aggregate_cap / uniqueness) and multi-sheet row-level
    fan-outs read from SEVERAL sheets — the SQL carries one `FROM "sheet"` per
    branch. EVERY one of those table names must be excluded from the identifier
    set; otherwise the other sheets' names are mistaken for missing columns of the
    first sheet and the whole rule is wrongly skipped (so the violation never
    shows). A column counts as present when it exists in ANY sheet the query reads
    — compile_ir only fans a rule out to sheets that carry all its fields, so a
    per-branch mismatch can't arise from correctly-compiled SQL.
    """
    sheets = re.findall(r'FROM\s+"([^"]+)"', sql)
    if not sheets:
        return []
    sheet_set = set(sheets)
    known = set()
    any_known = False
    for sh in sheet_set:
        cols = (tables.get(sh) or {}).get("columns") or []
        if cols:
            any_known = True
            known |= set(cols)
    if not any_known:
        return []  # unknown sheet(s) — let the normal path handle it
    refs = set(re.findall(r'"([^"]+)"', sql))   # double-quoted = identifiers
    refs -= sheet_set                           # table names, not columns
    refs.discard("__rowid")                     # synthetic, always present
    return [c for c in refs if c not in known]


# =====================================================================
# 5b. Deterministic column TYPE checks (date / amount) — no LLM
# =====================================================================
# Contract rules only assert business constraints, and their SQL deliberately
# SKIPS cells that don't cast (see rule_compiler._date/_num) — so a date column
# holding a string ("Specialty") or an amount column holding text silently
# passes. This pass flags exactly those rows: a NON-EMPTY cell in a typed column
# that fails a tolerant cast for its declared type. Column types come from the
# output template / canonical data model (`column_types`); columns with no known
# date/number type are left untouched. It is intentionally lenient (accepts the
# date/number spellings BDX files actually use) so it flags genuine junk, not
# merely differently-formatted-but-valid values.

_TYPE_HINT = {
    "date":   "a valid date (e.g. 2026-01-31)",
    "number": "a numeric amount (digits, optional . , $ %)",
}
_TYPE_NOUN = {"date": "date", "number": "amount"}


def _typecheck_date_expr(col: str) -> str:
    """VARCHAR → DATE across the common BDX spellings (superset of the rule
    engine's parser, to avoid flagging valid-but-oddly-formatted dates)."""
    c = f"TRIM({_qid(col)})"
    fmts = ("%Y%m%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%d-%m-%Y",
            "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%Y/%m/%d")
    parts = [f"TRY_CAST({c} AS DATE)", f"TRY_CAST({c} AS TIMESTAMP)::DATE"]
    parts += [f"TRY_CAST(TRY_STRPTIME({c}, '{f}') AS DATE)" for f in fmts]
    return "COALESCE(" + ", ".join(parts) + ")"


def _typecheck_num_expr(col: str) -> str:
    """VARCHAR → DOUBLE, stripping thousands separators, currency, % and spaces
    (so '1,250.00', '$3,000' and '15%' all read as numeric)."""
    c = _qid(col)
    # nested REPLACEs strip: comma, $, %, space
    stripped = (f"REPLACE(REPLACE(REPLACE(REPLACE({c}, ',', ''), '$', ''), "
                f"'%', ''), ' ', '')")
    return f"TRY_CAST({stripped} AS DOUBLE)"


# Excel's 1900 date system: serial 1 == 1900-01-01, day 0 == 1899-12-30.
_EXCEL_EPOCH = datetime(1899, 12, 30)


def _excel_serial_from_date(text) -> str | None:
    """If `text` is an ISO date/datetime, return the Excel SERIAL number that a
    date-formatted numeric cell almost certainly held before it was read as a
    date (e.g. a premium 75250 the source stored/formatted as 2106-01-09). Lets
    the amount type-check recommend the real number. None if not a date."""
    s = str(text).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        delta = dt - _EXCEL_EPOCH
        serial = delta.days + delta.seconds / 86400
        return str(int(serial)) if serial == int(serial) else f"{serial:.6f}".rstrip("0").rstrip(".")
    return None


def run_type_checks(con, tables, column_types, max_rows=10_000):
    """Flag cells that violate their column's declared type.

    `column_types` = {sheet_name: {column_name: "date" | "number"}}. Returns a
    list of exception dicts in the SAME shape as execute_rule (severity 'warning',
    no rule_id, recommendation prefilled). Best-effort: a failure on any one
    column is swallowed so a type check never blocks delivery.
    """
    if not column_types:
        return []
    out: list[dict] = []
    for sheet, tinfo in (tables or {}).items():
        kinds = column_types.get(sheet) or {}
        if not kinds:
            continue
        present = set(tinfo.get("columns") or [])
        for col, kind in kinds.items():
            if kind not in _TYPE_HINT or col not in present:
                continue
            if len(out) >= max_rows:
                return out
            q = _qid(col)
            expr = _typecheck_date_expr(col) if kind == "date" else _typecheck_num_expr(col)
            sql = (
                f"SELECT __rowid AS row_id, {q} AS actual_value "
                f"FROM {_qid(sheet)} "
                f"WHERE {q} IS NOT NULL AND TRIM({q}) <> '' AND ({expr}) IS NULL "
                f"LIMIT {max(0, max_rows - len(out))}"
            )
            try:
                rows = con.execute(sql).fetchall()
            except Exception:
                continue  # never let a single column abort the pass
            hint, noun = _TYPE_HINT[kind], _TYPE_NOUN[kind]
            for rowid, actual in rows:
                # For an AMOUNT column holding a date, the source cell was almost
                # certainly a number stored with a date format (Excel serial). Show
                # the recovered number as the recommendation so it can be Approved.
                serial = _excel_serial_from_date(actual) if kind == "number" else None
                if serial is not None:
                    reason = (f"“{col}” expects {hint}, but found the date “{actual}”. "
                              f"The source cell is date-formatted — the underlying "
                              f"number is {serial}.")
                    rec = serial
                else:
                    reason = f"“{col}” expects {hint}, but found “{actual}”."
                    rec = hint
                out.append({
                    "severity": "warning",
                    "code": "type_check",
                    "sheet": sheet,
                    "row": rowid,
                    "column": col,
                    "field": col,
                    "rule_id": None,
                    "rule_name": f"Type check — {noun}",
                    "contract_id": None,
                    "contract_filename": None,
                    "contract_clause_text": None,
                    "contract_clause_page": None,
                    "policy_number": None,
                    "actual_value": actual,
                    "expected_value": rec,
                    "recommendation": rec,
                    "reason": reason,
                    "message": reason,
                    "error_class": "type_mismatch",
                })
    return out


def run_validation(records_by_sheet, rules, contract=None, template_id=None,
                   session=None, max_exc=500, schema_cols=None, column_types=None):
    """Validate the resolved output records against the contract rules using
    DuckDB.

    Each rule runs the DETERMINISTIC query that was created and verified at
    contract upload (`rule_spec.compiled_sql`) — there is NO LLM call here. A
    clause whose query is missing or fails to run against this data is NOT
    silently dropped: it is returned in `unprocessable` so the UI can highlight
    that the clause was not validated.

    Returns:
      {
        "exceptions":   [ ...structured data-violation dicts (incl. fuzzy warnings)... ],
        "unprocessable":[ {rule_id, rule_name, message} ],
        "stats": {rules_total, rules_ok, rules_unprocessable, exceptions, truncated}
      }
    """
    own_session = False
    if session is None:
        from db import SessionLocal
        session = SessionLocal()
        own_session = True

    # TEMP: 500-exception cap DISABLED so every rule is evaluated and ALL
    # exceptions are returned (otherwise earlier rules exhaust the budget and later
    # rules like "Fronting Fee Minimum Amount" never run). This neutralizes max_exc
    # everywhere it's used (the loop break, the per-rule fetch limit, and the final
    # slices). To restore the cap, delete this line.
    max_exc = 10_000_000

    exceptions = []
    unprocessable = []
    rules_ok = 0
    rules = list(rules or [])
    truncated = False

    def _flag(rule, message, sh, rh):
        unprocessable.append({
            "rule_id": rule.get("rule_id"),
            "rule_name": rule.get("rule_name"),
            "message": message,
        })
        # Record the outcome for audit (no LLM was involved).
        try:
            _save_cache(session, rule, template_id, sh, rh,
                        {"sql": None, "status": "cannot_process",
                         "message": message, "attempts": 0})
        except Exception:
            pass

    try:
        _cid = (contract or {}).get("id")
        _run_label = f"t{template_id or 'NA'}_c{_cid or 'NA'}"
        con, tables = build_connection(
            records_by_sheet, schema_cols=schema_cols, label=_run_label
        )
        sh = schema_hash(tables)

        # BDX-time variation reconciliation: make enum rules match the data's OWN
        # spellings (fuzzy pre-check first, AI only for the unmatched remainder).
        # Best-effort — it must NEVER block validation. It mutates the rule dicts
        # in-memory (so this run uses the updated SQL); the DB write-back is gated
        # OFF by default (KAVACHIO_VARIATION_PERSIST). Disable entirely with
        # KAVACHIO_VARIATION_RECONCILE=0.
        try:
            from contract_upload_services.variation_reconcile import (
                reconcile as _reconcile_variations,
            )
            _reconcile_variations(con, tables, rules, session=session)
        except Exception:
            pass

        for rule in rules:
            if len(exceptions) >= max_exc:
                truncated = True
                break
            rh = rule_hash(rule)

            sql, why = _compiled_sql_for(rule)
            if not sql:
                _flag(rule, why, sh, rh)
                continue

            # Safety re-check (the query was already guarded at creation).
            good, cleaned = guard_sql(sql)
            if not good:
                _flag(rule, f"Compiled query failed the safety check: {cleaned}", sh, rh)
                continue

            # Column-first: verify every column the rule needs exists in the BDX
            # BEFORE running it. If not, skip cleanly (don't error on a binder).
            missing = _missing_columns(cleaned, tables)
            if missing:
                _flag(rule, f"Not validated — the BDX has no column for: "
                            f"{', '.join(missing)}. Add it to the output template "
                            f"to run this rule.", sh, rh)
                continue

            try:
                found = execute_rule(con, cleaned, rule, contract,
                                     max_exc - len(exceptions))
            except Exception as exec_err:
                # No LLM fallback — if the created query can't run against this
                # data, the clause is reported as not validated (highlighted).
                _flag(rule, f"This clause could not be validated against the data "
                            f"(query failed to run): {exec_err}", sh, rh)
                continue

            rules_ok += 1
            exceptions.extend(found)
            # Cache the SQL actually used (status ok) for audit / rule_sql view.
            try:
                _save_cache(session, rule, template_id, sh, rh,
                            {"sql": cleaned, "status": "ok",
                             "message": None, "attempts": 0})
            except Exception:
                pass

        # Deterministic type checks (date / amount) on declared-typed columns.
        # Runs after contract rules; never blocks delivery.
        if column_types and len(exceptions) < max_exc:
            try:
                exceptions.extend(
                    run_type_checks(con, tables, column_types,
                                    max_rows=max_exc - len(exceptions)))
            except Exception:
                pass

        con.close()
    finally:
        if own_session:
            session.close()

    return {
        "exceptions": exceptions[:max_exc],
        "unprocessable": unprocessable,
        "stats": {
            "rules_total": len(rules),
            "rules_ok": rules_ok,
            "rules_unprocessable": len(unprocessable),
            "exceptions": len(exceptions[:max_exc]),
            "truncated": truncated,
        },
    }
