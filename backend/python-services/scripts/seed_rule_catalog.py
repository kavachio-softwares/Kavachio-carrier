"""One-time / idempotent seeder for the rule-template catalog + vocabulary.

These tables are the runtime source of truth (read by catalog_store via SELECT
only). This script is the ONLY place that creates them and loads the seed JSON
in contract_upload_services/seeds/. Run it once per environment (and again after
editing the seed files) to apply changes.

Usage:
    cd backend/python-services
    source .venv/bin/activate
    python -m scripts.seed_rule_catalog            # create + upsert (idempotent)
    python -m scripts.seed_rule_catalog --reset    # delete existing rows first
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the backend package importable when run as a module from any cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.sql import text
from db import canonical_engine

_SEED_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "contract_upload_services", "seeds",
)

_DDL = [
    """CREATE TABLE IF NOT EXISTS rule_template (
        name              TEXT PRIMARY KEY,
        engine            TEXT,
        rule_type         TEXT,
        description       TEXT,
        required_json     TEXT,
        field_params_json TEXT,
        is_active         INTEGER DEFAULT 1,
        sort_order        INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS rule_type_template_map (
        rule_type TEXT PRIMARY KEY,
        template  TEXT
    )""",
    # `source` separates the two kinds of row that live here:
    #   'seed'  — from seeds/vocabulary.json, rewritten by every run of this script
    #   'admin' — a spelling a tenant_admin added through the Bordereau Setup screen
    #             (app_routes.rule_add_variation_value). Those are NOT in any seed
    #             file, so --reset must leave them alone or they are gone for good.
    # See docs/migrations/17_vocabulary_admin_spellings.sql for existing databases.
    """CREATE TABLE IF NOT EXISTS vocabulary_term (
        class            TEXT,
        canonical        TEXT,
        synonyms_json    TEXT,
        source           TEXT NOT NULL DEFAULT 'seed',
        created_by       INTEGER,
        source_rule_id   INTEGER,
        source_tenant_id INTEGER,
        created_at       TIMESTAMP NOT NULL DEFAULT now(),
        updated_at       TIMESTAMP NOT NULL DEFAULT now(),
        PRIMARY KEY (class, canonical)
    )""",
    """CREATE TABLE IF NOT EXISTS vocabulary_field_hint (
        hint       TEXT PRIMARY KEY,
        class      TEXT,
        sort_order INTEGER DEFAULT 0,
        source     TEXT NOT NULL DEFAULT 'seed',
        created_at TIMESTAMP NOT NULL DEFAULT now()
    )""",
]

# Tables whose rows all come from the seed files, so --reset can clear them whole.
_RESET_WHOLE = ("rule_template", "rule_type_template_map")
# Tables that ALSO hold rows people created through the app. --reset removes only
# what this script wrote; anything sourced from the UI survives.
_RESET_SEEDED_ONLY = ("vocabulary_term", "vocabulary_field_hint")


def _read_seed(name: str) -> dict:
    with open(os.path.join(_SEED_DIR, name)) as f:
        return json.load(f)


def seed(reset: bool = False):
    tseed = _read_seed("rule_templates.json")
    vseed = _read_seed("vocabulary.json")

    with canonical_engine.begin() as conn:
        for ddl in _DDL:
            conn.execute(text(ddl))

        if reset:
            for t in _RESET_WHOLE:
                conn.execute(text(f"DELETE FROM {t}"))
            for t in _RESET_SEEDED_ONLY:
                # NOT a bare DELETE: admin-added spellings exist in no seed file,
                # so wiping the table would destroy them permanently.
                conn.execute(text(
                    f"DELETE FROM {t} WHERE source IS DISTINCT FROM 'admin'"))

        # rule_template (upsert)
        for i, t in enumerate(tseed["templates"]):
            conn.execute(text("""
                INSERT INTO rule_template
                    (name, engine, rule_type, description, required_json,
                     field_params_json, is_active, sort_order)
                VALUES (:name, :engine, :rule_type, :description, :required,
                        :field_params, 1, :sort_order)
                ON CONFLICT (name) DO UPDATE SET
                    engine=EXCLUDED.engine, rule_type=EXCLUDED.rule_type,
                    description=EXCLUDED.description,
                    required_json=EXCLUDED.required_json,
                    field_params_json=EXCLUDED.field_params_json,
                    is_active=EXCLUDED.is_active, sort_order=EXCLUDED.sort_order
            """), {
                "name": t["name"], "engine": t.get("engine"),
                "rule_type": t.get("rule_type"), "description": t.get("description"),
                "required": json.dumps(t.get("required") or []),
                "field_params": json.dumps(t.get("field_params") or {}),
                "sort_order": i,
            })

        # rule_type_template_map (upsert)
        for rt, tmpl in (tseed.get("rule_type_to_template") or {}).items():
            conn.execute(text("""
                INSERT INTO rule_type_template_map (rule_type, template)
                VALUES (:rt, :tmpl)
                ON CONFLICT (rule_type) DO UPDATE SET template=EXCLUDED.template
            """), {"rt": rt, "tmpl": tmpl})

        # vocabulary_term (upsert)
        #
        # The WHERE on DO UPDATE is what stops a re-seed silently discarding
        # spellings people added through the app. `synonyms_json` is a whole-list
        # column, so overwriting it with the seed's list would drop every admin
        # addition to that same (class, canonical) — with no error and no trace.
        # An admin-touched row is therefore left ENTIRELY alone; to force a seed
        # value back over one, delete the row by hand first.
        for cls, table in (vseed.get("vocab") or {}).items():
            for canonical, synonyms in table.items():
                conn.execute(text("""
                    INSERT INTO vocabulary_term (class, canonical, synonyms_json, source)
                    VALUES (:c, :k, :s, 'seed')
                    ON CONFLICT (class, canonical) DO UPDATE
                        SET synonyms_json=EXCLUDED.synonyms_json
                        WHERE vocabulary_term.source IS DISTINCT FROM 'admin'
                """), {"c": cls, "k": canonical, "s": json.dumps(synonyms or [])})

        # vocabulary_field_hint (upsert) — same protection: a hint registered by
        # the app maps a real column to its class, and the seed file knows nothing
        # about it.
        for i, pair in enumerate(vseed.get("field_class_hints") or []):
            conn.execute(text("""
                INSERT INTO vocabulary_field_hint (hint, class, sort_order, source)
                VALUES (:h, :c, :o, 'seed')
                ON CONFLICT (hint) DO UPDATE
                    SET class=EXCLUDED.class, sort_order=EXCLUDED.sort_order
                    WHERE vocabulary_field_hint.source IS DISTINCT FROM 'admin'
            """), {"h": pair[0], "c": pair[1], "o": i})

    # Report counts.
    with canonical_engine.connect() as conn:
        for t in ("rule_template", "rule_type_template_map",
                  "vocabulary_term", "vocabulary_field_hint"):
            n = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            print(f"  {t}: {n} rows")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reset", action="store_true",
                   help="DELETE existing rows before seeding (drops removed entries)")
    args = p.parse_args()
    print(f"Seeding rule catalog + vocabulary from {_SEED_DIR} "
          f"({'reset' if args.reset else 'upsert'})…")
    seed(reset=args.reset)
    print("Done.")


if __name__ == "__main__":
    main()
