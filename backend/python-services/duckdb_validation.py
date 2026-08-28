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

import row_classifier



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
    # Load trusted REFERENCE tables (e.g. `uszips` for the zip↔state check,
    # `country_currency` for the currency↔country check) while external access is
    # still enabled — each loader reads a bundled file, and
    # `enable_external_access=false` is a one-way latch. Each is gated internally so
    # it only fires when the schema has the relevant column(s); the paths are fixed
    # code constants (never AI-influenced), so this opens no attack surface: by the
    # time any rule SQL runs, the sandbox below is closed. Best-effort — never blocks.
    _loaded_refs = []
    try:
        from contract_upload_services.uszips_reference import load_reference_tables
        _loaded_refs += load_reference_tables(con, records_by_sheet, schema_cols)
    except Exception:
        pass
    try:
        # MUST run after uszips: the unified postal reference copies its US slice
        # from the `uszips` table, so there is one source of truth for US data and
        # the pre-existing US-only compiled rules keep querying `uszips` untouched.
        from contract_upload_services.intl_postal_reference import (
            load_reference_tables as _load_intl_postal)
        _loaded_refs += _load_intl_postal(con, records_by_sheet, schema_cols)
    except Exception:
        pass
    try:
        from contract_upload_services.country_currency_reference import (
            load_reference_table as _load_country_currency)
        _loaded_refs += _load_country_currency(con, records_by_sheet, schema_cols)
    except Exception:
        pass
    if _loaded_refs:
        print(f"[DuckDB] loaded reference table(s): {', '.join(_loaded_refs)}")
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

        # Drop non-data rows (blank spacers, "TX Total"/"Grand Total" subtotal
        # rows, re-printed header rows) BEFORE they ever reach a rule's SQL —
        # every rule template automatically only sees real data rows this way,
        # with no per-template change needed. See row_classifier.py.
        # CRITICAL: excluded rows are SKIPPED, never renumbered — each surviving
        # row keeps its ORIGINAL 1-based position as __rowid, because exceptions
        # are painted back onto the rendered output file by row number, and that
        # file still physically contains the excluded rows. Renumbering shifted
        # every exception below an excluded row up by one, highlighting the
        # wrong cells (summary rows showed flags that belonged to their
        # neighbours).
        data_records, excluded_rows = row_classifier.classify_sheet_rows(records, cols)
        if excluded_rows:
            print(f"[DuckDB] sheet {sheet!r}: excluded {len(excluded_rows)} "
                  f"non-data row(s) from validation "
                  f"{row_classifier.excluded_summary(excluded_rows)}")
        excluded_pos = {e["position"] for e in excluded_rows}

        coldefs = ", ".join(f'{_qid(c)} VARCHAR' for c in cols)
        create = f'CREATE TABLE {_qid(sheet)} (__rowid INTEGER{"," + coldefs if coldefs else ""})'
        con.execute(create)

        if cols and records:
            placeholders = ", ".join(["?"] * (len(cols) + 1))
            ins = f'INSERT INTO {_qid(sheet)} VALUES ({placeholders})'
            rows = []
            for i, rec in enumerate(records, start=1):
                if i in excluded_pos:
                    continue
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
            for rec in data_records:
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

        tables[sheet] = {"columns": cols, "samples": samples, "excluded_rows": excluded_rows}

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
# 2. SQL safety constants (used by the guard + dry-run below)
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
        # A row flagged only because the cell is not a number never reached the
        # rule's comparison, so leading its message with the rule's own sentence
        # describes a check that did not run — "Amounts already paid on a claim
        # cannot be greater than the total incurred (Program ID must be a number,
        # but found BB2.)" is two unrelated statements. The row's own reason says
        # it all. (The review screens re-title these rows in full — see
        # rule_explainer.apply_numeric_format_identity; this keeps the message
        # that is painted into the delivered workbook honest too.)
        numeric_format = _is_numeric_format_row(d)
        message = d.get("reason")
        if rule.get("error_message") and not numeric_format:
            message = f"{rule.get('error_message')} ({d.get('reason')})"
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
            "message": message,
            # Same marker the structural type pass uses for the same problem, so
            # everything that already knows "a type failure has no writable
            # recommended value" (e.g. the review table's Approve) covers these
            # rows too, without having to learn a second name for it.
            "error_class": "type_mismatch" if numeric_format else "data_violation",
        })
    return out


def _is_numeric_format_row(d) -> bool:
    """True when this result row is the compiler's not-a-number companion check
    rather than the rule's own comparison. Recognised by re-deriving the exact
    sentence the compiler wrote for the cell, so writer and reader cannot drift
    (see rule_compiler.not_numeric_reason)."""
    try:
        from contract_upload_services.rule_compiler import is_not_numeric_reason
        return is_not_numeric_reason(d.get("reason"), d.get("field"),
                                     d.get("actual_value"))
    except Exception:
        return False


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

def _refresh_if_stale(spec, sql, sheet_names):
    """Recompile a cached query whose builder's REFERENCE TABLE has changed since
    it was compiled; return the cached SQL unchanged for everything else.

    Only reference-bound templates qualify (see
    `rule_compiler.compiled_sql_is_stale`) — a handful of data-quality rules, not
    the thousands of ordinary contract rules, which keep running their cached SQL
    untouched. Without this a postal rule compiled before CA/GB support keeps
    joining the US-only `uszips` table and false-flags every non-US row, and the
    only cure would be a DB migration or a re-upload.

    Recompiling in memory means the fix lands automatically on the next validation
    run — for rules already in the database AND for every new upload — and nothing
    is written back. Candidate sheets are the ones actually present in THIS upload
    (`sheet_names`), falling back to the sheets named in the cached query. Using the
    upload's own sheets is what lets a rule reach a sheet that did not exist when it
    was compiled — a CA and a GB sheet added to a BDX that only ever had one. It
    does not widen the rule loosely: `compile_ir` keeps only the sheets that carry
    ALL of the rule's fields and still applies the clause's own schedule scope, so
    a per-schedule rule stays on its schedule.

    Fail-safe: any problem recompiling returns the cached SQL, so a rule can never
    stop running because of this."""
    try:
        from contract_upload_services.rule_compiler import (
            collapsed_sheets_in_sql, compiled_sql_is_stale, drop_sheet_arms,
            recompile_ir_for_sheets, sheets_in_sql)
    except Exception:
        return sql
    ir = spec.get("ir") if isinstance(spec, dict) else None
    if not isinstance(ir, dict):
        return sql
    # A rule compiled before compile_ir's collapse guard can carry an arm that an
    # alias folded onto ONE column — it compares that column with itself and its
    # companion type check then flags every non-numeric cell of a column the rule
    # was never about. Cut those arms out in memory (the sound arms, aliases
    # included, are untouched) so the rule stops raising the false exception on
    # its next run instead of waiting for the contract to be re-uploaded.
    collapsed = collapsed_sheets_in_sql(ir, sql)
    if collapsed:
        pruned = drop_sheet_arms(sql, collapsed)
        if pruned:
            print(f"[DuckDB] dropped collapsed alias arm(s) on {collapsed} "
                  f"from cached SQL")
            sql = pruned
    if not compiled_sql_is_stale(ir.get("template"), sql):
        return sql
    for candidate in (list(sheet_names or []), sheets_in_sql(sql)):
        if not candidate:
            continue
        try:
            return recompile_ir_for_sheets(ir, candidate)
        except Exception:
            continue
    return sql


def _compiled_sql_for(rule, sheet_names=None):
    """Return (sql, None) using the deterministic SQL produced at contract upload
    (`compile_ir`, stored in rule_spec.compiled_sql) — NO LLM. Returns
    (None, reason) when the rule has no compiled query so the caller can flag the
    clause to the user instead of silently skipping it.

    A query whose reference table has since changed is refreshed from its IR first
    (see `_refresh_if_stale`) so it validates against current reference data."""
    spec = rule.get("rule_spec")
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except Exception:
            spec = None
    if isinstance(spec, dict) and spec.get("compiled_sql"):
        return _refresh_if_stale(spec, spec["compiled_sql"], sheet_names), None
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


# A cell that states a PERIOD as two dates ("7/1/2023 - 6/30/2024") carries date
# data — it is a range rather than an instant, not a malformed date — so it must
# satisfy a date type-check. The separator has to be flanked by WHITESPACE (or be
# a word), which is exactly what stops a dash INSIDE a date ("1-Jan-2024",
# "01-15-2024") from being mistaken for a range separator. Only the SEPARATOR is
# spelled out here: each half is then run through the same parser as a whole
# cell, so a range is accepted in precisely the date spellings already supported
# and nothing new is hardcoded about how a date may look.
_DATE_RANGE_RE = r"^(.+?)\s+(?:[-–—]|(?i:to|through|thru|until))\s+(.+)$"

# The AMOUNT counterpart of the rule above — a cell that states a LAYER or a
# RANGE as two amounts joined by a connector ("5000000 xs 45000000" = 5m excess
# of 45m, "1,000 - 2,000", "5,000,000 part of 10,000,000"). Such a cell carries
# amount data — a structure BUILT from amounts rather than a single amount, not a
# malformed one — so a whole column of it must not be reported as "expects a
# numeric amount … but found 5000000 xs 45000000".
#
# This pattern only SPLITS a cell; on its own it decides nothing. Unlike a date
# range, a pair of amounts is not self-evidently amount data: a street address
# ("5625 CR 7410", "12750 Merit Drive Suite 1000") has the identical shape — a
# number, a connector carrying no digit, a number. Nothing INSIDE such a cell
# tells the two apart, so the decision is taken from the COLUMN, whose own data
# shows whether one notation is in use (see `_amount_notation_values`).
#
# Group 1 is the first amount, group 2 the CONNECTOR (one or more whitespace-
# delimited tokens carrying no digit — "xs", "x/s", "excess of", "part of", "-",
# "/", or any other a bordereau uses; discovered from the data, never listed
# here), group 3 the second amount. Requiring the connector to be flanked by
# WHITESPACE is what stops real junk ("1-2-3", "--5", "N/A") from splitting at
# all; each half is then read by the SAME number reader as a whole cell, so
# nothing new is hardcoded about how an amount may look.
_AMOUNT_PAIR_RE = r"^(.+?)\s+((?:[^\s\d]+\s+)+)(.+)$"

# The date spellings the type check accepts. ONE list, shared by the SQL parser
# below and its Python twin (`parses_as_date`), so the check and any caller
# asking "would this pass?" can never drift apart.
_DATE_FMTS = ("%Y%m%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%d-%m-%Y",
              "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%Y/%m/%d")

# A cell may carry a TIME OF DAY after the date — an Excel date column exported
# as text reads "4/18/2019 12:00:00 AM", and a midnight timestamp is still a
# date. Stripping the trailing time and re-parsing the remainder keeps every
# spelling above valid WITH a time, instead of enumerating the date × time
# cross-product; nothing new is hardcoded about how a date may look. Matches
# both 12-hour (with AM/PM) and 24-hour clocks, with or without seconds.
_TIME_SUFFIX_RE = (r"^(.*?)[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?"
                   r"\s*(?:[AaPp]\.?[Mm]\.?)?$")

# …and a cell that is a ZERO TIME OF DAY and NOTHING ELSE is an EMPTY date cell.
# A spreadsheet stores a date as a number of days, so a cell holding no date holds
# zero, and a zero rendered through a time format comes out as "00:00:00" —
# midnight of a day that was never there. It is the same absence a blank cell
# reports, just written down: there is no date in it to be malformed, and no
# correction a reviewer could make by reading it. Flagging it says only "this
# policy has no retro date", which the empty cell beside it already says silently.
#
# ZERO only, deliberately. A cell reporting a REAL time of day ("09:30:00") lost a
# date it once had — that is a genuine defect and stays reported. Both midnight
# spellings a spreadsheet emits are covered: 24-hour "00:00[:00]" and 12-hour
# "12:00[:00] AM", with or without fractional seconds.
#
# RE2 syntax (DuckDB regexp_matches) and Python `re` both, so ONE definition
# serves the SQL check and its Python twin.
_EMPTY_DATE_RE = (r"^(?:0{1,2}:00(?::00)?(?:\.0+)?"
                  r"|12:00(?::00)?(?:\.0+)?\s*[Aa]\.?[Mm]\.?)$")


def is_empty_date_cell(value) -> bool:
    """True when a DATE column's cell states a zero time of day and no date —
    the way an empty date cell is written down (see _EMPTY_DATE_RE).

    Asked ONLY of a column being checked as a date: the same text in an AMOUNT
    column is a genuinely mis-typed cell, and is still reported as one."""
    return bool(re.match(_EMPTY_DATE_RE, str(value or "").strip()))


def _date_parse_expr(val_sql: str) -> str:
    """SQL VARCHAR expression → DATE across the common BDX spellings (superset of
    the rule engine's parser, to avoid flagging valid-but-oddly-formatted dates).
    Takes an EXPRESSION rather than a column name so the identical parser serves
    both a whole cell and each half of a date range."""
    def _by_fmt(expr):
        out = []
        for f in _DATE_FMTS:
            e = f"TRY_CAST(TRY_STRPTIME({expr}, '{f}') AS DATE)"
            if f == "%Y%m%d":
                # A COMPACT date is exactly 8 digits. Without this, TRY_STRPTIME
                # reads a 7-digit AMOUNT ("1245075") as 1245-07-05, so a plain
                # number passes the date check by accident — and a column full of
                # such amounts looks part-valid instead of plainly not-a-date.
                e = f"CASE WHEN regexp_matches({expr}, '^\\d{{8}}$') THEN {e} END"
            out.append(e)
        return out

    parts = [f"TRY_CAST({val_sql} AS DATE)", f"TRY_CAST({val_sql} AS TIMESTAMP)::DATE"]
    parts += _by_fmt(val_sql)
    # …the same spellings again, after dropping a trailing time of day. A
    # non-matching cell yields '' from regexp_extract, which parses to NULL, so
    # a plain date simply falls through to the branches above.
    parts += _by_fmt(f"regexp_extract({val_sql}, '{_TIME_SUFFIX_RE}', 1)")
    return "COALESCE(" + ", ".join(parts) + ")"


def parses_as_date(value) -> bool:
    """Python twin of `_typecheck_date_expr`: would the date type-check accept
    this cell? Same format list, same tolerance of a trailing time of day and of
    a date RANGE, so a caller can ask the question without a DuckDB connection
    (see direct_routes._column_types_from_structure, which uses it to withdraw a
    'date' classification the column's own sample values all contradict).
    Kept in step with the SQL by tests/test_date_typecheck_parity.py."""
    s = str(value or "").strip()
    if not s:
        return False
    # A PERIOD stated as two dates is date data — accepted when BOTH halves are,
    # exactly as the SQL does.
    rng = re.match(_DATE_RANGE_RE.replace("(?i:", "(?:"), s, re.IGNORECASE)
    if rng and _parses_as_plain_date(rng.group(1)) and _parses_as_plain_date(rng.group(2)):
        return True
    return _parses_as_plain_date(s)


def _parses_as_plain_date(value) -> bool:
    """One cell (not a range) against the shared format list."""
    s = str(value or "").strip()
    if not s:
        return False
    m = re.match(_TIME_SUFFIX_RE, s)
    for cand in (s, (m.group(1).strip() if m else "")):
        if not cand:
            continue
        try:                              # ISO date / datetime, as TRY_CAST does
            datetime.fromisoformat(cand)
            return True
        except ValueError:
            pass
        for fmt in _DATE_FMTS:
            # strptime accepts a SHORT numeric run for %Y%m%d (it reads
            # "1245075" as 1245-07-05), where DuckDB's TRY_STRPTIME does not.
            # Require the exact 8 digits so a plain 7-digit amount is not
            # mistaken for a compact date — that is precisely the value this
            # helper exists to recognise as NOT a date.
            if fmt == "%Y%m%d" and not (cand.isdigit() and len(cand) == 8):
                continue
            try:
                datetime.strptime(cand, fmt)
                return True
            except ValueError:
                continue
    return False


def _typecheck_date_expr(col: str) -> str:
    """VARCHAR → DATE for the type-check pass: a plain date in any supported
    spelling, OR a date RANGE whose two halves are each such a date.

    A range counts as valid only when BOTH halves parse, so a genuinely broken
    cell ("7/1/2023 - garbage", "TBD - TBD") is still flagged. Its START date
    stands in as the expression's value, keeping the result a DATE exactly as
    before — the caller only tests this for NULL."""
    c = f"TRIM({_qid(col)})"
    lo = f"regexp_extract({c}, '{_DATE_RANGE_RE}', 1)"
    hi = f"regexp_extract({c}, '{_DATE_RANGE_RE}', 2)"
    # No match → regexp_extract yields '', which parses to NULL, so a non-range
    # value simply falls through to the plain-date branch.
    as_range = (f"CASE WHEN {_date_parse_expr(lo)} IS NOT NULL "
                f"AND {_date_parse_expr(hi)} IS NOT NULL "
                f"THEN {_date_parse_expr(lo)} END")
    return f"COALESCE({_date_parse_expr(c)}, {as_range})"


def parses_as_number(value) -> bool:
    """Python twin of the AMOUNT type-check: would `value` pass `_typecheck_num_expr`?

    Answered by EXECUTING the same expression the check itself uses, on a
    throwaway DuckDB connection — so this can never drift from the SQL: a value
    the check accepts is accepted here, spelling for spelling (thousands
    separators, currency symbols, %, accounting negatives). Used to withdraw a
    'number' classification that a column's own sample values prove wrong
    (see direct_routes._samples_all_fail_number). Falls back to a plain float
    parse if DuckDB is unavailable."""
    s = str(value).strip()
    if not s:
        return False
    try:
        import duckdb
        con = duckdb.connect()
        try:
            expr = _typecheck_num_expr("v")
            row = con.execute(
                f"SELECT ({expr}) IS NOT NULL FROM (SELECT CAST(? AS VARCHAR) AS \"v\")",
                [s]).fetchone()
            return bool(row and row[0])
        finally:
            con.close()
    except Exception:
        try:
            float(re.sub(r"[\s,$%]", "", s))
            return True
        except (TypeError, ValueError):
            return False


def _numeric_reader():
    """The platform's number reader as an expression builder, with the plain cast
    as a fallback if the compiler cannot be imported (so the type check still
    runs rather than disappearing)."""
    try:
        from contract_upload_services.rule_compiler import numeric_expr
        return numeric_expr
    except Exception:
        return lambda expr: f"TRY_CAST({expr} AS DOUBLE)"


def _typecheck_num_expr(col: str) -> str:
    """VARCHAR → DOUBLE for the type-check pass.

    Uses the RULE ENGINE's own number reader (`rule_compiler.numeric_expr`), so
    the two can never disagree: an amount the rules happily compute with must not
    be reported here as a malformed amount, and an amount reported here must be
    one the rules genuinely cannot read. It accepts a thousands separator, any
    currency symbol, a percent sign, stray spaces and the ACCOUNTING NEGATIVE
    ("($4,380.00)" = -4380.00).

    A cell this reader cannot read is not reported until it has also been held
    against the COLUMN's own composite notation — see `_amount_notation_values`.

    Falls back to the previous plain cast if the compiler cannot be imported, so
    the type check still runs rather than disappearing."""
    return _numeric_reader()(_qid(col))


def _amount_notation_values(con, values):
    """Of the cells an amount column could NOT read, the ones that are the
    column's own COMPOSITE NOTATION rather than malformed amounts.

    A bordereau states a layer as two amounts and a connector — "5000000 xs
    45000000" (5m excess of 45m), "3000000 xs 2000000" — and a limit band the
    same way ("1,000 - 2,000"). Those cells hold amount data, so a column of them
    must not be reported as 122 malformed amounts. But the shape alone proves
    nothing: a street address ("5625 CR 7410") splits identically, and in a
    column typed as an amount an address IS a violation worth reporting.

    What tells them apart is the COLUMN, so that is what is asked here — of the
    column's own data, with no vocabulary of connectors and no column-name
    guessing:

      • split each unreadable value into amount · connector · amount, reading
        both halves with the platform's own number reader (`_AMOUNT_PAIR_RE`);
      • group the values by the connector they use;
      • a connector is the column's notation when at least TWO distinct values
        use it (a notation is used more than once) and it accounts for the
        MAJORITY of everything the column could not read.

    One connector running through the column is how that column writes amounts;
    anything less is prose that happens to contain numbers, and every one of
    those cells stays reported. The majority is taken over ALL the unreadable
    values, not merely the ones that split: an address column whose 46 unreadable
    cells include just two Texas farm roads ("6220 FM 2920", "545 FM 1488")
    would otherwise see `fm` win 2 out of 2 and lose both exceptions. Measured
    over every bordereau to hand, with each column read as if typed 'number',
    the honest cases are nowhere near that line — the layer column's connector
    covered 100% of its unreadable values, the best any address column managed
    was 11%. Values that do not split, or whose halves are not both amounts
    ("N/A", "7/1/2023 - 6/30/2024", "Policy 123 endorsement 456"), are never
    returned.

    Returns the set of values to treat as amount data (empty when the column has
    no such notation). Best-effort: any failure returns the empty set, leaving
    the check exactly as it was."""
    vals = sorted({str(v).strip() for v in values if str(v or "").strip()})
    if not vals:
        return set()
    num = _numeric_reader()
    lo = f"regexp_extract(v, '{_AMOUNT_PAIR_RE}', 1)"
    sep = f"regexp_extract(v, '{_AMOUNT_PAIR_RE}', 2)"
    hi = f"regexp_extract(v, '{_AMOUNT_PAIR_RE}', 3)"
    try:
        rows = con.execute(
            f"SELECT v, lower(regexp_replace(trim({sep}), '\\s+', ' ', 'g')) "
            f"FROM (SELECT unnest(CAST(? AS VARCHAR[])) AS v) "
            f"WHERE {num(lo)} IS NOT NULL AND {num(hi)} IS NOT NULL",
            [vals]).fetchall()
    except Exception:
        return set()
    by_connector: dict = {}
    for v, connector in rows:
        by_connector.setdefault(connector, set()).add(v)
    if not by_connector:
        return set()
    members = max(by_connector.values(), key=len)
    # A one-off, or a handful of pair-shaped values among a column of unrelated
    # unreadable ones, is not the column's way of writing amounts — leave every
    # unreadable cell reported, exactly as before this guard existed.
    if len(members) < 2 or len(members) * 2 <= len(vals):
        return set()
    return members


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
            # A cell whose value IS the column's own header is a repeated
            # in-sheet header band (grouped BDX sections restate their headers
            # part-way down), not data — a genuine amount/date never equals the
            # header string, so skipping it can't hide a real violation.
            hdr = str(col).strip().replace("'", "''")
            # A DATE column's EMPTY cells reach us as a zero time of day
            # ("00:00:00") when the source wrote its blanks through a time format.
            # They are blanks, not malformed dates, so they are skipped exactly as
            # a blank is — see is_empty_date_cell. Added ONLY for kind == 'date':
            # in an amount column that same text is a real type mismatch and is
            # still reported.
            empty_date = (f"AND NOT regexp_matches(TRIM({q}), '{_EMPTY_DATE_RE}') "
                          if kind == "date" else "")
            sql = (
                f"SELECT __rowid AS row_id, {q} AS actual_value "
                f"FROM {_qid(sheet)} "
                f"WHERE {q} IS NOT NULL AND TRIM({q}) <> '' "
                f"AND TRIM({q}) <> '{hdr}' {empty_date}AND ({expr}) IS NULL "
                f"LIMIT {max(0, max_rows - len(out))}"
            )
            try:
                rows = con.execute(sql).fetchall()
            except Exception:
                continue  # never let a single column abort the pass
            # An amount column may write its amounts as a COMPOSITE — a layer
            # ("5000000 xs 45000000"), a band ("1,000 - 2,000"). Those cells
            # carry amount data, not a malformed amount, so they are dropped
            # here when the column's own data shows one such notation in use.
            # Everything else it could not read stays reported.
            if kind == "number" and rows:
                notation = _amount_notation_values(con, [a for _, a in rows])
                if notation:
                    rows = [(r, a) for r, a in rows
                            if str(a).strip() not in notation]
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

            sql, why = _compiled_sql_for(rule, list(tables.keys()))
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
