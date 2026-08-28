"""Canonical relational schema, materialised from data_model.py (which itself
comes from Kavachio_Data_Model_Spec_v2.xlsx).

The DATA_MODEL gives us {canonical_field: {table, column, type, source, ...}}.
Here we group it back into SQLAlchemy Tables — one table per `table` value,
one column per (table, column) pair. Each table gets a single integer PK,
auto-incremented; the column matching `{table}_id` is the PK when present,
otherwise we synthesise `_row_id`.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Integer, MetaData, Numeric, Table, Text,
)
from sqlalchemy.types import JSON

from data_model import DATA_MODEL

canonical_metadata = MetaData()

_TYPE_MAP = {
    "int":      Integer,
    "decimal":  Numeric,
    "date":     Date,
    "datetime": DateTime,
    "bool":     Boolean,
    "json":     JSON,
    "string":   Text,
}


def _build_tables() -> dict[str, Table]:
    by_table: dict[str, dict[str, dict[str, Any]]] = {}
    for canonical_key, meta in DATA_MODEL.items():
        # `physical: False` fields are routing-only aliases (e.g. insured/carrier
        # names that live on the party table but are addressed via the policy in
        # the mapping pipeline). They must NOT become real DB columns.
        if meta.get("physical") is False:
            continue
        by_table.setdefault(meta["table"], {})[meta["column"]] = meta

    tables: dict[str, Table] = {}
    for table_name, cols in by_table.items():
        pk_col = (
            f"{table_name}_id" if f"{table_name}_id" in cols
            else next((c for c, m in cols.items() if m["type"] == "int" and c.endswith("_id")), None)
        )
        sa_cols = []
        for c, m in cols.items():
            sa_type = _TYPE_MAP.get(m["type"], Text)
            kwargs: dict[str, Any] = {}
            if c == pk_col:
                kwargs["primary_key"] = True
                kwargs["autoincrement"] = True
            sa_cols.append(Column(c, sa_type(), **kwargs))
        if pk_col is None:
            sa_cols.insert(0, Column("_row_id", Integer, primary_key=True, autoincrement=True))
        tables[table_name] = Table(table_name, canonical_metadata, *sa_cols)
    return tables


CANONICAL_TABLES: dict[str, Table] = _build_tables()


# Tables that can carry user-defined "extra fields" — values not represented
# in the canonical data model. Stored as a JSONB column on each entity row,
# keyed by the user-supplied extra-field key (the same string the column-
# mapping canonical_mapping uses behind its `_xf:` prefix).
EXTRA_FIELD_ENTITY_TABLES = (
    "policy", "claim", "coverage", "premium_transaction",
    "insured_location", "building",
)
for _t_name in EXTRA_FIELD_ENTITY_TABLES:
    _t = CANONICAL_TABLES.get(_t_name)
    if _t is not None and "extras" not in _t.c:
        _t.append_column(Column("extras", JSON, nullable=True))


def pk_column(table_name: str) -> str | None:
    t = CANONICAL_TABLES.get(table_name)
    if t is None:
        return None
    pks = list(t.primary_key.columns)
    return pks[0].name if pks else None


def column_names(table_name: str) -> set[str]:
    t = CANONICAL_TABLES.get(table_name)
    return set(t.c.keys()) if t is not None else set()
