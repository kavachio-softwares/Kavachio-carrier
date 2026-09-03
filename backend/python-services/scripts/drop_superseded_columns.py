"""Drop the v2 columns left behind by scripts/migrate_to_v4_model.py.

That migration is deliberately ADDITIVE: it copies each carried column into its
v4 successor and leaves the original in place so the change is reversible. This
script is the second half — it removes the originals once you have verified the
round trip. Run it only AFTER migrate_to_v4_model.py has run and you have
checked real files through the pipeline.

WHAT IT WILL NEVER TOUCH
────────────────────────
1. `PROTECTED_TABLES` — the static catalogue the rule engine reads. Their DATA
   drives rule generation, and the code reads them by their LEGACY column names
   through raw SQL (catalog_store, constants, vocabulary, seed_rule_catalog), so
   the v4 spec's renamed columns are NOT in use. Both the rows and the columns
   stay exactly as they are.
2. Any table outside the canonical model — every operational table (mappers,
   export_templates, output_exports, pipelines, notifications, …) is untouched.
3. Any column the ORM in db.py still maps, whatever its name. The ORM keeps
   stable Python attribute names over v4 physical columns AND over ops-only
   columns; both are read at runtime, so both are kept.
4. Any column of the v4 model itself.

A column is only dropped when it is none of the above AND one of:
  • RENAMED  — it has a v4 successor and every non-NULL source value has
    already been copied (verified per column, per row, before the drop).
  • EMPTY    — the v4 model removed it and the column holds no data at all.

A removed column that still holds data is REPORTED, never dropped, unless you
pass --include-lossy to say you have read the report and accept the loss.

Usage:
    DATABASE_URL=postgresql+psycopg2://... python scripts/drop_superseded_columns.py
        (dry run — prints the plan, changes nothing)

    ... python scripts/drop_superseded_columns.py --apply
    ... python scripts/drop_superseded_columns.py --apply --include-lossy
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# main.py loads the .env; this script does not import main, so load it here or
# DATABASE_URL is unset and the engine points nowhere.
try:
    from pathlib import Path as _Path
    from dotenv import load_dotenv as _load
    _load(_Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

from sqlalchemy import inspect, text

# The rule/vocabulary catalogue. Seeded reference data that rule generation
# reads at import time, by legacy column names. Never dropped, never emptied.
# (scripts/seed_rule_catalog.py owns these tables; catalog_store.py,
# constants.py and vocabulary.py read them.)
PROTECTED_TABLES = {
    # The rule/vocabulary catalogue — seeded reference data.
    "rule_template",
    "rule_type_template_map",
    "rule_class_library",
    "vocabulary_term",
    "vocabulary_field_hint",
    "generic_rule_spec",
    "generic_rule_specification",   # pre-v4 name, if it still exists
    "ref_code_list",
    "ref_code_value",
    # The rule-generation / validation subsystem. Its schema is applied by
    # MIGRATION SQL, not by the model, and is far richer than the v4 spec:
    # validation_rule alone carries rule_engine, rule_spec, canonical_target,
    # rule_status, source_clause_id … all read by app_routes and written by
    # contract_upload_services/db_persister. On a database where no rules have
    # been generated yet those columns are EMPTY, so an emptiness test would
    # happily drop the entire rule engine's storage. They are off limits.
    "validation_rule",
    "validation_run",
    "validation_exception",
    "clauses_extracted",
    "contract_clause_routing",
    "rule_sql",
    "program_field_extraction",
    "contract_term",
}


# Files that merely CATALOGUE old names rather than use them. data_model.py
# carries LEGACY_FIELD_MAP, which lists every v2 field mapped to None — a
# record of what was REMOVED. Counting that as a live reference kept 99 dead
# columns alive on the first pass, so these are excluded from the scan.
CATALOGUE_FILES = {
    "data_model.py",                 # LEGACY_FIELD_MAP
    "migrate_to_v4_model.py",        # RENAMES
    "drop_superseded_columns.py",    # this file
}


_SQL_LITERAL = re.compile(
    r"""(?:text|exec_driver_sql)\s*\(\s*("{3}|'{3}|"|')(.*?)\1""", re.S)


def _code_referenced_names() -> set[str]:
    """Identifiers that could name a column the v4 model does NOT define.

    Every ORM and canonical access goes through CANONICAL_TABLES (built from the
    v4 model) or Base.metadata. A physical column outside both is unreachable by
    that route — there is no attribute to read it through. The only way code can
    touch one is by NAMING it in raw SQL.

    So the scan is restricted to SQL: .sql files, plus the string literals handed
    to text() / exec_driver_sql(). An earlier version matched every identifier in
    every .py file, which kept 99 dead columns alive because their names appear
    in prose, in variable names, or in data_model.py's LEGACY_FIELD_MAP.

    Still deliberately coarse in one direction: a name found in ANY SQL string
    protects that column on EVERY table, so generic SCD-2 SQL
    ("SET is_current_version = FALSE") keeps `is_current_version` everywhere.
    That over-keeps a handful of columns — the right way to be wrong here.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    chunks: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".venv", "__pycache__", "node_modules", ".git")]
        for fn in filenames:
            if fn in CATALOGUE_FILES:
                continue
            path = os.path.join(dirpath, fn)
            try:
                if fn.endswith(".sql"):
                    chunks.append(open(path, encoding="utf-8", errors="ignore").read())
                    continue
                if not fn.endswith(".py"):
                    continue
                src = open(path, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            chunks.extend(m.group(2) for m in _SQL_LITERAL.finditer(src))

    names: set[str] = set()
    for chunk in chunks:
        names.update(re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", chunk))
    return names


def _renames() -> dict[str, dict[str, str]]:
    """The old→new column map the migration used, so the two cannot disagree.

    Must be the SAME merged map (the doc's plus the ones the ORM implies): the
    migration copies on that map, and a column it copied must be recognised
    here as RENAMED, not mistaken for a removed column that still holds data.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from migrate_to_v4_model import RENAMES, _orm_renames
    merged: dict[str, dict[str, str]] = {}
    for src in (_orm_renames(), RENAMES):
        for tbl, cols in src.items():
            merged.setdefault(tbl, {}).update(cols)
    return merged


def _orm_columns() -> dict[str, set[str]]:
    """Physical column names db.py's ORM still maps, per table."""
    import db
    out: dict[str, set[str]] = {}
    for t in db.Base.metadata.tables.values():
        out.setdefault(t.name, set()).update(c.name for c in t.c)
    return out


def _model_columns() -> dict[str, set[str]]:
    """Physical column names the v4 canonical model defines, per table."""
    from canonical import CANONICAL_TABLES
    return {name: set(t.c.keys()) for name, t in CANONICAL_TABLES.items()}


def _count(conn, sql, params=None):
    return conn.execute(text(sql), params or {}).scalar() or 0


_VIEW_DEPS_SQL = """
SELECT DISTINCT v.relname AS view_name, t.relname AS table_name
FROM pg_depend d
JOIN pg_rewrite r  ON r.oid = d.objid
JOIN pg_class   v  ON v.oid = r.ev_class
JOIN pg_class   t  ON t.oid = d.refobjid
JOIN pg_namespace n ON n.oid = v.relnamespace
WHERE v.relkind IN ('v', 'm')
  AND n.nspname = current_schema()
  AND t.relkind = 'r'
  AND v.relname <> t.relname
"""


def view_dependencies(engine) -> dict[str, set[str]]:
    """view name -> the tables it reads. Postgres refuses to drop a column or
    table a view depends on, and CASCADE would silently delete the view, so
    every dependent view must be dropped and recreated around the change."""
    out: dict[str, set[str]] = {}
    if engine.dialect.name != "postgresql":
        return out
    with engine.connect() as conn:
        for view, table in conn.execute(text(_VIEW_DEPS_SQL)):
            out.setdefault(view, set()).add(table)
    return out


_CONSTRAINT_SQL = """
SELECT c.conname, c.conrelid::regclass::text,
       ARRAY(SELECT a.attname FROM unnest(c.conkey) k JOIN pg_attribute a
             ON a.attrelid = c.conrelid AND a.attnum = k),
       c.contype
FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
WHERE n.nspname = current_schema() AND c.conkey IS NOT NULL
"""


def constraints_on(engine, dropping: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """(table, constraint) pairs that must go because a column they cover is
    being dropped.

    Postgres refuses `DROP COLUMN` while a constraint covers it, and the CASCADE
    the hint suggests would remove the constraint silently. Dropping it
    explicitly means the loss is visible in the SQL and in review — this is how
    contract_amendment's self-referencing FK on the old PK surfaced.
    """
    if engine.dialect.name != "postgresql":
        return []
    found: list[tuple[int, str, str]] = []
    with engine.connect() as conn:
        for name, table, cols, ctype in conn.execute(text(_CONSTRAINT_SQL)):
            if any((table, c) in dropping for c in (cols or [])):
                # ORDER MATTERS. A foreign key depends on the unique index
                # behind the primary key it points at, so dropping the PK first
                # fails ("depends on index ..._pkey"). Foreign keys go first,
                # then checks, then the unique/primary keys they rested on.
                rank = {"f": 0, "c": 1, "u": 2, "p": 3}.get(ctype, 4)
                found.append((rank, table, name))
    return [(t, n) for _r, t, n in sorted(set(found))]


def function_body_names(engine) -> set[str]:
    """Every identifier appearing in a PL/pgSQL function body.

    Postgres does NOT track column references inside a function, so a trigger
    reading OLD.broker_party_id keeps compiling after the column is dropped and
    fails only when it next fires. That is exactly how the first pass committed
    cleanly and then broke login. Treat any name a function body mentions as
    referenced, and re-point the function before dropping it.
    """
    if engine.dialect.name != "postgresql":
        return set()
    import re as _re
    out: set[str] = set()
    with engine.connect() as conn:
        for (src,) in conn.execute(text(
            "SELECT prosrc FROM pg_proc p JOIN pg_namespace n "
            "ON n.oid = p.pronamespace WHERE n.nspname = current_schema()")):
            out.update(_re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", src or ""))
    return out


def policy_dependencies(engine) -> dict[str, set[str]]:
    """table -> the columns its RLS policies read.

    Postgres refuses to drop a column a policy references. More importantly,
    a policy left pointing at a column the app no longer writes stops isolating
    anything — so these must be re-pointed, never worked around.
    """
    out: dict[str, set[str]] = {}
    if engine.dialect.name != "postgresql":
        return out
    import re as _re
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT tablename, COALESCE(qual::text,'') || ' ' || "
            "COALESCE(with_check::text,'') FROM pg_policies "
            "WHERE schemaname = current_schema()"))
        for table, body in rows:
            out.setdefault(table, set()).update(
                _re.findall(r"[A-Za-z_][A-Za-z_0-9]*", body or ""))
    return out


def plan_tables(engine):
    """Whole TABLES the v4 model removed, and which of them are safe to drop.

    Same evidence bar as the columns, plus two more:
      • it must hold NO ROWS — an empty table is the only kind whose loss can
        be reasoned about. uszip_reference (33k rows of postal reference data)
        and generic_rule_specification (the rule library) are exactly why.
      • nothing we are KEEPING may still have a foreign key into it, or the
        DROP fails and takes the whole transaction with it.

    Returns (to_drop_ordered, kept) where kept explains each refusal.
    """
    from canonical import CANONICAL_TABLES
    import db as _db

    model = set(CANONICAL_TABLES)
    orm = {t.name for t in _db.Base.metadata.tables.values()}
    referenced = _code_referenced_names() | function_body_names(engine)

    with engine.connect() as conn:
        insp = inspect(conn)
        physical = set(insp.get_table_names())
        candidates = sorted(physical - model - orm - PROTECTED_TABLES)

        drop: list[str] = []
        kept: list[tuple[str, str]] = []
        for t in candidates:
            if t in referenced:
                kept.append((t, "still referenced in the codebase"))
                continue
            rows = _count(conn, f'SELECT count(*) FROM "{t}"')
            if rows:
                kept.append((t, f"holds {rows} row(s)"))
                continue
            drop.append(t)

        # Refuse to drop anything a table we are KEEPING still points at.
        dropping = set(drop)
        keepers = physical - dropping
        blocked_by_fk: dict[str, list[str]] = {}
        for keeper in sorted(keepers):
            try:
                fks = insp.get_foreign_keys(keeper)
            except Exception:  # noqa: BLE001
                continue
            for fk in fks:
                target = fk.get("referred_table")
                if target in dropping:
                    blocked_by_fk.setdefault(target, []).append(keeper)
        for t, refs in blocked_by_fk.items():
            dropping.discard(t)
            kept.append((t, f"{', '.join(sorted(set(refs)))} still reference(s) it"))

    # A table a VIEW reads cannot be dropped either — and dropping the view to
    # get at it would delete something the drop was never asked to remove.
    vdeps = view_dependencies(engine)
    for view, tables in vdeps.items():
        for t in sorted(tables & dropping):
            dropping.discard(t)
            kept.append((t, f"view {view} reads it"))

    # No ordering needed: these are dropped as ONE `DROP TABLE a, b, c`
    # statement, which lets Postgres resolve foreign keys among them itself.
    # Ordering them by hand would only be a way to get it wrong.
    return sorted(dropping), sorted(kept)


def plan(engine, include_lossy: bool):
    model = _model_columns()
    orm = _orm_columns()
    renames = _renames()
    referenced = _code_referenced_names() | function_body_names(engine)

    to_drop: list[tuple[str, str, str]] = []      # (table, column, reason)
    lossy: list[tuple[str, str, int]] = []        # (table, column, rows_with_data)
    blocked: list[tuple[str, str, str]] = []      # (table, column, why)

    with engine.connect() as conn:
        insp = inspect(conn)
        for table in sorted(model):
            if table in PROTECTED_TABLES:
                continue
            if not insp.has_table(table):
                continue
            physical = {c["name"] for c in insp.get_columns(table)}
            keep = model[table] | orm.get(table, set())
            candidates = sorted(physical - keep)
            if not candidates:
                continue
            back = {old: new for old, new in renames.get(table, {}).items()}
            for col in candidates:
                new = back.get(col)
                if new and new in physical:
                    # RENAMED — only drop once every source value is copied.
                    unmigrated = _count(
                        conn,
                        f'SELECT count(*) FROM "{table}" '
                        f'WHERE "{col}" IS NOT NULL AND "{new}" IS NULL')
                    if unmigrated:
                        blocked.append((
                            table, col,
                            f"{unmigrated} row(s) not yet copied into {new} — "
                            "run migrate_to_v4_model.py first"))
                    else:
                        to_drop.append((table, col, f"renamed → {new}"))
                    continue
                # REMOVED by the v4 model. Empty is NOT sufficient — a column
                # nothing has written yet still reads as empty. It must also be
                # referenced nowhere in the code.
                if col in referenced:
                    blocked.append((table, col,
                                    "still referenced in the codebase — "
                                    "not part of the v4 model but actively used"))
                    continue
                rows = _count(conn,
                              f'SELECT count(*) FROM "{table}" WHERE "{col}" IS NOT NULL')
                if rows == 0:
                    to_drop.append((table, col, "removed by the v4 model (empty)"))
                elif include_lossy:
                    to_drop.append((table, col, f"removed by the v4 model ({rows} rows LOST)"))
                else:
                    lossy.append((table, col, rows))
    return to_drop, lossy, blocked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually drop (default is a dry run)")
    ap.add_argument("--include-lossy", action="store_true",
                    help="also drop removed columns that still hold data")
    ap.add_argument("--sql", metavar="FILE", nargs="?", const="-",
                    help="emit the DDL instead of executing it "
                         "(FILE, or stdout when given no path)")
    ap.add_argument("--tables", action="store_true",
                    help="also drop whole tables the v4 model removed "
                         "(empty + unreferenced + nothing points at them)")
    args = ap.parse_args()

    from db import engine
    print(f"target: {engine.url.render_as_string(hide_password=True)}"
          + ("" if args.apply else "   [DRY RUN — nothing will change]"))
    print(f"protected (never touched): {', '.join(sorted(PROTECTED_TABLES))}\n")

    to_drop, lossy, blocked = plan(engine, args.include_lossy)

    if blocked:
        print(f"== KEPT — {len(blocked)} column(s) not safe to drop ==")
        shown = blocked if len(blocked) <= 25 else blocked[:25]
        for t, c, why in shown:
            print(f"  {t}.{c}: {why}")
        if len(blocked) > len(shown):
            print(f"  … and {len(blocked) - len(shown)} more")
        print()

    if lossy:
        print("== HOLDS DATA, NOT DROPPED (pass --include-lossy to drop) ==")
        for t, c, n in lossy:
            print(f"  {t}.{c}: {n} non-null row(s)")
        print()

    if not to_drop:
        print("Nothing to drop.")
        return

    if args.sql:
        lines = [
            "-- Drop the v2 columns superseded by the v4 data model.",
            "-- Generated by scripts/drop_superseded_columns.py against",
            f"-- {engine.url.render_as_string(hide_password=True)}",
            "--",
            "-- Every column below was verified first: each renamed column's",
            "-- successor is already populated, and each removed column is both",
            "-- empty and referenced nowhere in the codebase. The rule-generation",
            "-- catalogue and the validation subsystem are untouched.",
            "--",
            "-- One transaction: it all lands, or the schema is unchanged.",
            "",
            "BEGIN;",
            "",
            "-- ALTER TABLE needs an ACCESS EXCLUSIVE lock. Without a timeout a",
            "-- single open transaction elsewhere (a running app server) leaves",
            "-- this queued while it blocks every reader behind it. Fail fast.",
            "SET lock_timeout = '15s';",
            "SET statement_timeout = '120s';",
        ]
        # Views must go first: Postgres refuses to drop a column one reads, and
        # CASCADE would delete the view rather than fix it. They are recreated
        # against the v4 names at the end, from scripts/v4_views.sql.
        pol = policy_dependencies(engine)
        pol_hits = sorted({f"{t}.{c}" for t, c, _w in to_drop
                           if c in pol.get(t, set())})
        pol_tables = sorted({h.split(".")[0] for h in pol_hits})
        if pol_tables:
            lines += ["", f"-- RLS policies on {', '.join(pol_tables)} read columns",
                      "-- being dropped. Removed here and recreated at the end against",
                      "-- the v4 tenancy columns — a policy left on a column the app no",
                      "-- longer writes would stop isolating tenants entirely."]
            with engine.connect() as _c:
                for _t in pol_tables:
                    for (_pn,) in _c.execute(text(
                        "SELECT policyname FROM pg_policies WHERE "
                        "schemaname = current_schema() AND tablename = :t"), {"t": _t}):
                        lines.append(f'DROP POLICY IF EXISTS "{_pn}" ON "{_t}";')

        cons = constraints_on(engine, {(t, c) for t, c, _w in to_drop})
        if cons:
            lines += ["", f"-- {len(cons)} constraint(s) cover columns being dropped.",
                      "-- Dropped explicitly rather than by CASCADE, so the loss is visible."]
            for tbl, cn in cons:
                lines.append(f'ALTER TABLE "{tbl}" DROP CONSTRAINT IF EXISTS "{cn}";')

        vdeps = view_dependencies(engine)
        touched = {t for t, _c, _w in to_drop}
        if args.tables:
            touched |= set(plan_tables(engine)[0])
        affected = sorted(v for v, tabs in vdeps.items() if tabs & touched)
        if affected:
            lines += ["", f"-- {len(affected)} view(s) read columns being changed;",
                      "-- dropped here and recreated at the end against v4 names."]
            for v in affected:
                lines.append(f'DROP VIEW IF EXISTS "{v}";')
        cur = None
        for t, c, why in to_drop:
            if t != cur:
                lines.append(f"\n-- {t}")
                cur = t
            lines.append(f'ALTER TABLE "{t}" DROP COLUMN "{c}";   -- {why}')
        if args.tables:
            tdrop, tkept = plan_tables(engine)
            lines.append("")
            lines.append("-- ---------------------------------------------------------------")
            lines.append(f"-- Tables the v4 model removed: {len(tdrop)} dropped, "
                         f"{len(tkept)} kept.")
            lines.append("-- Each one below is EMPTY, referenced nowhere in the code, and")
            lines.append("-- nothing that survives has a foreign key into it.")
            for t, why in tkept:
                lines.append(f"--   KEPT {t}: {why}")
            lines.append("-- ---------------------------------------------------------------")
            if tdrop:
                lines.append("")
                # One statement, so Postgres resolves FKs among them itself.
                lines.append("DROP TABLE IF EXISTS")
                for i, t in enumerate(tdrop):
                    sep = "," if i < len(tdrop) - 1 else ";"
                    lines.append(f'    "{t}"{sep}')
        if pol_tables:
            pol_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "v4_policies.sql")
            if os.path.exists(pol_file):
                lines += ["", "-- " + "-" * 60,
                          "-- RLS policies re-pointed at the v4 tenancy columns.",
                          "-- " + "-" * 60, "", open(pol_file).read()]
        if affected:
            views_sql = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "v4_views.sql")
            if os.path.exists(views_sql):
                lines += ["", "-- " + "-" * 60,
                          "-- Views rebuilt against the v4 column names.",
                          "-- " + "-" * 60, "", open(views_sql).read()]
            else:
                lines += ["", f"-- WARNING: {views_sql} missing — the views above "
                              "were dropped and are NOT recreated."]
        lines += ["", "COMMIT;", ""]
        out = "\n".join(lines)
        if args.sql == "-":
            print(out)
        else:
            with open(args.sql, "w") as fh:
                fh.write(out)
            print(f"wrote {len(to_drop)} statements to {args.sql}")
        return

    print(f"== WILL DROP {len(to_drop)} column(s) ==")
    by_table: dict[str, list] = {}
    for t, c, why in to_drop:
        by_table.setdefault(t, []).append((c, why))
    for t in sorted(by_table):
        print(f"  {t}:")
        for c, why in by_table[t]:
            print(f"    - {c}   ({why})")

    if not args.apply:
        print("\nDry run. Re-run with --apply to execute.")
        return
    unmigrated = [b for b in blocked if "not yet copied" in b[2]]
    if unmigrated:
        raise SystemExit("\nRefusing to apply: data is not fully copied — "
                         "run scripts/migrate_to_v4_model.py first.")

    # One transaction: either every drop lands or the schema is untouched. A
    # half-dropped schema is the one outcome worse than not running at all.
    # DROP COLUMN is deliberately NOT cascaded — if a view or constraint still
    # depends on a column, that is a fact worth surfacing, not silently
    # destroying along with it.
    # A column an RLS policy reads cannot be dropped, and dropping the policy
    # to get past that would delete tenant isolation. Replay the corrected
    # policies from scripts/v4_policies.sql instead.
    _pol = policy_dependencies(engine)
    _pol_hits = sorted({f"{t}.{c}" for t, c, _w in to_drop
                        if c in _pol.get(t, set())})
    _pol_sql = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "v4_policies.sql")
    if _pol_hits and not os.path.exists(_pol_sql):
        raise SystemExit(
            f"\n{len(_pol_hits)} column(s) are read by RLS policies "
            f"({', '.join(_pol_hits)}) but {_pol_sql} is missing — refusing to "
            "drop them, because a policy left on a column the app no longer "
            "writes silently stops isolating tenants.")

    vdeps = view_dependencies(engine)
    _touched = {t for t, _c, _w in to_drop}
    if args.tables:
        _touched |= set(plan_tables(engine)[0])
    _affected = sorted(v for v, tabs in vdeps.items() if tabs & _touched)
    _views_sql = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v4_views.sql")
    if _affected and not os.path.exists(_views_sql):
        raise SystemExit(
            f"\n{len(_affected)} view(s) read columns being dropped "
            f"({', '.join(_affected)}) but {_views_sql} is missing — refusing to "
            "drop views with nothing to recreate them from.")

    # Compute the table plan BEFORE the write transaction opens.
    #
    # plan_tables() opens its OWN connection. Calling it from inside the
    # transaction made the script wait on locks its own transaction was
    # holding — a self-deadlock that froze every other session on the database
    # until it was killed. Nothing that needs a second connection may run
    # between BEGIN and COMMIT.
    _tdrop: list[str] = []
    if args.tables:
        _tdrop = plan_tables(engine)[0]

    try:
        with engine.begin() as conn:
            # Never queue indefinitely for a lock. ALTER TABLE needs ACCESS
            # EXCLUSIVE, so a single open transaction elsewhere (a running app
            # server) would otherwise leave this waiting while it blocks every
            # reader behind it. Fail fast and say so instead.
            conn.exec_driver_sql("SET lock_timeout = '15s'")
            conn.exec_driver_sql("SET statement_timeout = '120s'")
            for v in _affected:
                conn.exec_driver_sql(f'DROP VIEW IF EXISTS "{v}"')
            if _pol_hits:
                for _t in sorted({h.split(".")[0] for h in _pol_hits}):
                    for (_pname,) in conn.exec_driver_sql(
                        "SELECT policyname FROM pg_policies "
                        "WHERE schemaname = current_schema() AND tablename = %s",
                        (_t,),
                    ).fetchall():
                        conn.exec_driver_sql(
                            f'DROP POLICY IF EXISTS "{_pname}" ON "{_t}"')
            for _tbl, _cn in constraints_on(engine, {(t, c) for t, c, _w in to_drop}):
                conn.exec_driver_sql(
                    f'ALTER TABLE "{_tbl}" DROP CONSTRAINT IF EXISTS "{_cn}"')
            for t, c, _ in to_drop:
                conn.exec_driver_sql(f'ALTER TABLE "{t}" DROP COLUMN "{c}"')
            if _tdrop:
                cols = ", ".join(f'"{t}"' for t in _tdrop)
                conn.exec_driver_sql(f"DROP TABLE IF EXISTS {cols}")
            if _pol_hits:
                conn.exec_driver_sql(open(_pol_sql).read())
            if _affected:
                conn.exec_driver_sql(open(_views_sql).read())
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"\nFAILED on {t}.{c} — nothing was dropped (rolled back).\n"
            f"  {type(exc).__name__}: {str(exc).splitlines()[0]}\n"
            "If a view or constraint depends on it, drop that first, or add the "
            "column to PROTECTED_TABLES / keep it.")
    print(f"\nDropped {len(to_drop)} column(s).")


if __name__ == "__main__":
    main()
