"""
_offline.py
───────────
Import this FIRST in a test that must import the rule generator (or anything
else that touches db.py / constants.py / the rule catalog at import time) with
no database and no network.

  * db.py builds its engine from DATABASE_URL at import, and constants.py runs one
    SELECT against rule_class_library at import and refuses an empty answer. An
    in-memory SQLite engine holding one placeholder row satisfies both, so the
    shared Postgres is never contacted.
  * The rule-template catalog is DB-only (catalog_store); it is served from the
    seed files the database itself is seeded from.
  * A runner that EXPORTED DATABASE_URL (e.g. a whole-suite run against a local
    database) keeps it: swapping in SQLite or forcing PGOPTIONS read-only is
    process-wide and would break every database test collected after this one.
    The tests importing this module replace ai_cache and the model, so they do
    not write either way.
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")
os.environ.setdefault("KAVACHIO_DECISION_LOG", "0")

if "db" not in sys.modules and not os.environ.get("DATABASE_URL"):
    os.environ["PGOPTIONS"] = "-c default_transaction_read_only=on"
    os.environ["DATABASE_URL"] = "sqlite://"
    import db                                                   # noqa: E402
    from sqlalchemy import text                                  # noqa: E402
    if db.engine.url.get_backend_name() == "sqlite":
        with db.engine.begin() as _c:
            _c.execute(text(
                "CREATE TABLE IF NOT EXISTS rule_class_library (name TEXT, "
                "display_name TEXT, description TEXT, rule_engine TEXT, "
                "default_severity TEXT, default_stage TEXT, availability TEXT)"))
            if not _c.execute(text("SELECT 1 FROM rule_class_library")).first():
                _c.execute(text(
                    "INSERT INTO rule_class_library VALUES ('offline', 'offline', "
                    "'test placeholder', 'ir', 'warning', 'post', 'global')"))

from contract_upload_services import catalog_store               # noqa: E402

if catalog_store._CACHE is None:
    _seeds = os.path.join(os.path.dirname(__file__), "..", "seeds")
    with open(os.path.join(_seeds, "rule_templates.json")) as _f:
        _t = json.load(_f)
    with open(os.path.join(_seeds, "vocabulary.json")) as _f:
        _v = json.load(_f)
    catalog_store._CACHE = {
        "templates": _t["templates"],
        "rule_type_map": _t.get("rule_type_to_template") or {},
        "vocab": _v.get("vocab") or {},
        "field_class_hints": _v.get("field_class_hints") or [],
        "catalog_version": "seed", "vocab_version": "seed",
    }


class MemoryCache:
    """In-memory stand-in for ai_cache.get/put: JSON round-trips like the real
    table (so a caller mutating a hit cannot corrupt the stored answer) and
    honours `refresh`."""

    def __init__(self):
        self.rows, self.puts, self.gets = {}, [], []

    def get(self, kind, key, refresh=False):
        self.gets.append((kind, key))
        if refresh or (kind, key) not in self.rows:
            return None
        return json.loads(self.rows[(kind, key)])

    def put(self, kind, key, payload, tenant_id=None):
        self.puts.append((kind, key))
        self.rows[(kind, key)] = json.dumps(payload)

    def install(self, monkeypatch):
        import ai_cache
        monkeypatch.setattr(ai_cache, "get", self.get)
        monkeypatch.setattr(ai_cache, "put", self.put)
        return self
