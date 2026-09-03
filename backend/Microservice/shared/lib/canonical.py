"""Canonical relational schema, materialised from data_model.py (which itself
comes from Kavachio_Data_Model_Spec_v4.xlsx — the ERD-derived model).

The DATA_MODEL gives us {canonical_field: {table, column, type, source, pk, ...}}.
Here we group it back into SQLAlchemy Tables — one table per `table` value,
one column per (table, column) pair. Primary keys come from the model's "pk"
metadata (the ERD marks them), so TEXT and composite primary keys (rule_template,
vocabulary_term, rule_type_template_map, …) build correctly; a table with no
marked PK gets a synthesised `_row_id`.
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
        # `physical: False` fields are routing-only aliases. They must NOT
        # become real DB columns.
        if meta.get("physical") is False:
            continue
        by_table.setdefault(meta["table"], {})[meta["column"]] = meta

    tables: dict[str, Table] = {}
    for table_name, cols in by_table.items():
        pk_cols = [c for c, m in cols.items() if m.get("pk")]
        sa_cols = []
        for c, m in cols.items():
            sa_type = _TYPE_MAP.get(m["type"], Text)
            kwargs: dict[str, Any] = {}
            if c in pk_cols:
                kwargs["primary_key"] = True
                # Only a single integer PK is an identity column; TEXT and
                # composite PKs are natural keys supplied by the application.
                kwargs["autoincrement"] = len(pk_cols) == 1 and m["type"] == "int"
            sa_cols.append(Column(c, sa_type(), **kwargs))
        if not pk_cols:
            sa_cols.insert(0, Column("_row_id", Integer, primary_key=True, autoincrement=True))
        tables[table_name] = Table(table_name, canonical_metadata, *sa_cols)
    return tables


CANONICAL_TABLES: dict[str, Table] = _build_tables()


# Tables that can carry user-defined "extra fields" — values not represented
# in the canonical data model. In the v4 model each of these tables already
# owns a `<table>_extras` JSONB tail; the generic `extras` column is kept as
# the shared destination the mapping pipeline writes behind its `_xf:` prefix.
EXTRA_FIELD_ENTITY_TABLES = (
    "policy", "claim", "coverage", "premium_transaction",
    "risk_location", "policyholder",
)
for _t_name in EXTRA_FIELD_ENTITY_TABLES:
    _t = CANONICAL_TABLES.get(_t_name)
    if _t is not None and "extras" not in _t.c:
        _t.append_column(Column("extras", JSON, nullable=True))


# ---------------------------------------------------------------------------
# Operational tenancy column.
#
# The v4 model scopes child tables through their parents (policy → contract →
# program → tenant), so most tables carry no tenant column of their own. The
# app and the RLS policies, however, key row security on a per-row tenant_id.
# That is an OPERATIONAL concern, not model content — so every canonical table
# without a `<table>_tenant_id` (or the literal `tenant_id` on `tenant`)
# receives a plain ops `tenant_id` column here, exactly the way `extras` is
# appended above. tenant_col() returns whichever column scopes a given table.
# ---------------------------------------------------------------------------
_TENANT_COL: dict[str, str] = {}
for _t_name, _t in CANONICAL_TABLES.items():
    if _t_name == "tenant":
        _TENANT_COL[_t_name] = "tenant_id"
        continue
    canonical_tc = f"{_t_name}_tenant_id"
    if canonical_tc in _t.c:
        _TENANT_COL[_t_name] = canonical_tc
    else:
        if "tenant_id" not in _t.c:
            _t.append_column(Column("tenant_id", Integer, nullable=True))
        _TENANT_COL[_t_name] = "tenant_id"


def tenant_col(table_name: str) -> str | None:
    """Name of the column that scopes `table_name` to a tenant."""
    return _TENANT_COL.get(table_name)


def pk_column(table_name: str) -> str | None:
    t = CANONICAL_TABLES.get(table_name)
    if t is None:
        return None
    pks = list(t.primary_key.columns)
    return pks[0].name if pks else None


def column_names(table_name: str) -> set[str]:
    t = CANONICAL_TABLES.get(table_name)
    return set(t.c.keys()) if t is not None else set()
