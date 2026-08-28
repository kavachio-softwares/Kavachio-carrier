"""
catalog_store.py
────────────────
Loads the rule-template catalog, the rule_type→template map, and the vocabulary
STRICTLY from the database — they are versioned data, editable without a deploy.

This module only ever runs SELECTs. It does NOT create tables, seed, or read the
JSON seed files. Populating the DB is a one-time deploy step:

    python scripts/seed_rule_catalog.py        # creates + seeds the tables

If the tables are missing or empty, loading raises a clear error telling you to
run that script (same fail-loud stance as constants.fetch_rule_class_library).

What stays in CODE (NOT in the DB): the SQL builders (rule_compiler._BUILDERS)
and the field-extraction interpreter (rule_ir). Only their *metadata* lives here.

Versions (`catalog_version`, `vocab_version`) are a hash of the loaded content,
so they change automatically when the catalog/vocab is edited — used to keep a
generated rule reproducible at a point in time.
"""

from __future__ import annotations

import json
import hashlib

from sqlalchemy.sql import text


def _version(payload) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return "sha1:" + hashlib.sha1(blob).hexdigest()[:16]


# =====================================================================
# DB readers (SELECT only)
# =====================================================================

def _load_from_db():
    from db import canonical_engine
    with canonical_engine.connect() as conn:
        trows = conn.execute(text(
            "SELECT name, engine, rule_type, description, required_json, "
            "field_params_json FROM rule_template WHERE is_active = 1 "
            "ORDER BY sort_order, name"
        )).fetchall()
        mrows = conn.execute(text(
            "SELECT rule_type, template FROM rule_type_template_map"
        )).fetchall()
        vrows = conn.execute(text(
            "SELECT class, canonical, synonyms_json FROM vocabulary_term"
        )).fetchall()
        hrows = conn.execute(text(
            "SELECT hint, class FROM vocabulary_field_hint ORDER BY sort_order, hint"
        )).fetchall()

    templates = [{
        "name": r[0], "engine": r[1], "rule_type": r[2], "description": r[3],
        "required": json.loads(r[4] or "[]"),
        "field_params": json.loads(r[5] or "{}"),
    } for r in trows]
    rule_type_map = {r[0]: r[1] for r in mrows}

    vocab: dict[str, dict] = {}
    for cls, canonical, syn in vrows:
        vocab.setdefault(cls, {})[canonical] = json.loads(syn or "[]")
    hints = [[r[0], r[1]] for r in hrows]

    return templates, rule_type_map, vocab, hints


# =====================================================================
# Public, cached load
# =====================================================================

_CACHE = None

_NOT_SEEDED_MSG = (
    "rule_template / vocabulary tables are missing or empty. "
    "Seed them once with:  python scripts/seed_rule_catalog.py"
)


def _load():
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    try:
        templates, rule_type_map, vocab, hints = _load_from_db()
    except Exception as exc:
        # Strict DB-only: no JSON fallback. Fail loudly with how to fix it.
        raise RuntimeError(f"[catalog_store] could not load from DB: {exc}. "
                           f"{_NOT_SEEDED_MSG}") from exc

    if not templates:
        raise RuntimeError(f"[catalog_store] no templates loaded. {_NOT_SEEDED_MSG}")

    _CACHE = {
        "templates": templates,
        "rule_type_map": rule_type_map,
        "vocab": vocab,
        "field_class_hints": hints,
        "catalog_version": _version([templates, rule_type_map]),
        "vocab_version": _version([vocab, hints]),
    }
    return _CACHE


def get_template_rows() -> list[dict]:
    return _load()["templates"]


def get_rule_type_map() -> dict:
    return _load()["rule_type_map"]


def get_vocab() -> dict:
    return _load()["vocab"]


def get_field_class_hints() -> list:
    return _load()["field_class_hints"]


def catalog_version() -> str:
    return _load()["catalog_version"]


def vocab_version() -> str:
    return _load()["vocab_version"]


def reload():
    """Drop the in-memory cache (e.g. after re-running the seeder)."""
    global _CACHE
    _CACHE = None
