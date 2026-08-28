"""
rule_ir.py
──────────
The Intent Representation (IR) for contract → BDX validation rules.

The LLM no longer authors executable specs (JSON Schema / custom DSL). It acts as
a *constrained extractor*: it picks ONE template from the catalog and fills its
parameters. Deterministic SQL generation lives in `rule_compiler`.

The template catalog, the rule_type→template map, and the catalog version are
loaded from the DB via `catalog_store` (seeded from ./seeds/rule_templates.json)
— they are VERSIONED DATA, not hardcoded here. What stays in CODE is the
field-extraction interpreter below (`_make_fields_fn`, built from each row's
declarative `field_params`) and the IR validation; the SQL builders live in
`rule_compiler`.

An IR instance looks like:

    {
      "template": "max_limit",
      "params": {"field": "Occurrence Limit", "max": 2000000,
                 "scope": {"Coverage Type": "CGL"}},
      "rule_name": "...", "confidence": 0.0,   # advisory only — NOT a gate
    }

Polarity is carried by the *template name* (max_limit vs min_limit,
value_in_set vs value_not_in_set) — never inferred.
"""

from __future__ import annotations

from contract_upload_services.catalog_store import (
    get_template_rows,
    get_rule_type_map,
    catalog_version as _catalog_version,
)


# Scope keys whose value is an OR-group of per-field filters (see rule_compiler).
# Their nested keys are the real Output-Template fields, not the group key itself.
_OR_SCOPE_KEYS = ("any_of", "or", "$or", "either")


def _scope_field_names(scope) -> list:
    """Every Output-Template field a scope references — flattening `any_of` OR
    groups to their nested field names (and never treating the group key as a
    field)."""
    names = []
    if not isinstance(scope, dict):
        return names
    for k, v in scope.items():
        if str(k).strip().lower() in _OR_SCOPE_KEYS:
            for d in (v if isinstance(v, (list, tuple)) else [v]):
                if isinstance(d, dict):
                    names += [kk for kk in d.keys() if isinstance(kk, str)]
        elif isinstance(k, str):
            names.append(k)
    return names


def _remap_scope(scope, r):
    """Rewrite scope FIELD names via resolver `r`, including inside `any_of`
    groups; leaves the group key and all values untouched."""
    if not isinstance(scope, dict):
        return scope
    out = {}
    for k, v in scope.items():
        if str(k).strip().lower() in _OR_SCOPE_KEYS:
            if isinstance(v, (list, tuple)):
                out[k] = [
                    {(r(kk) if isinstance(kk, str) else kk): vv for kk, vv in d.items()}
                    if isinstance(d, dict) else d
                    for d in v
                ]
            elif isinstance(v, dict):
                out[k] = {(r(kk) if isinstance(kk, str) else kk): vv
                          for kk, vv in v.items()}
            else:
                out[k] = v
        else:
            out[(r(k) if isinstance(k, str) else k)] = v
    return out


# =====================================================================
# Field-extraction interpreter (CODE) — replaces the old per-template lambda.
# Reads each row's declarative `field_params` and returns the output-template
# field names an IR references, used by the field-existence gate + sheet lookup.
# =====================================================================

def _make_fields_fn(fp: dict):
    field_keys = fp.get("field_keys") or []
    list_field_keys = fp.get("list_field_keys") or []
    include_scope = bool(fp.get("include_scope"))
    nested_paths = fp.get("nested_field_paths") or []
    condition_list_key = fp.get("condition_list_key")

    def fn(p):
        out = []
        for k in field_keys:
            v = p.get(k)
            if isinstance(v, str) and v:
                out.append(v)
        for k in list_field_keys:
            for v in (p.get(k) or []):
                if isinstance(v, str) and v:
                    out.append(v)
        if include_scope:
            out += _scope_field_names(p.get("scope"))
        if condition_list_key:
            for c in (p.get(condition_list_key) or []):
                f = c.get("field") if isinstance(c, dict) else None
                if isinstance(f, str) and f:
                    out.append(f)
        for path in nested_paths:
            cur = p
            for part in path.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if isinstance(cur, str) and cur:
                out.append(cur)
        return out

    return fn


def _build_catalog() -> dict:
    cat: dict[str, dict] = {}
    for row in get_template_rows():
        name = row.get("name")
        if not name:
            continue
        cat[name] = {
            "engine":    row.get("engine"),
            "rule_type": row.get("rule_type"),
            "required":  row.get("required") or [],
            "field_params": row.get("field_params") or {},
            "fields":    _make_fields_fn(row.get("field_params") or {}),
            "desc":      row.get("description") or "",
        }
    return cat


def remap_ir_fields(ir: dict, resolve) -> dict:
    """Rewrite an IR's field-valued params to the EXACT canonical output-template
    names using `resolve(name) -> canonical|None`. Tolerates case/spacing/punct
    differences from the extractor so a rule isn't lost over 'occurrence limit'
    vs 'Occurrence Limit'. A field that can't be resolved is left as-is and will
    then fail validate_ir (→ review). Uses the template's declarative
    field_params, so it stays in lockstep with field extraction."""
    spec = TEMPLATE_CATALOG.get(ir.get("template"))
    if not spec:
        return ir
    fp = spec.get("field_params") or {}
    params = dict(ir.get("params") or {})

    def r(name):
        c = resolve(name) if isinstance(name, str) else None
        return c or name

    for k in fp.get("field_keys", []):
        v = params.get(k)
        if isinstance(v, str) and v:
            params[k] = r(v)

    for k in fp.get("list_field_keys", []):
        v = params.get(k)
        if isinstance(v, list):
            params[k] = [r(x) if isinstance(x, str) else x for x in v]

    if fp.get("include_scope"):
        sc = params.get("scope")
        if isinstance(sc, dict):
            params["scope"] = _remap_scope(sc, r)

    clk = fp.get("condition_list_key")
    if clk:
        conds = params.get(clk)
        if isinstance(conds, list):
            new_conds = []
            for c in conds:
                if isinstance(c, dict) and isinstance(c.get("field"), str):
                    c = {**c, "field": r(c["field"])}
                new_conds.append(c)
            params[clk] = new_conds

    for path in fp.get("nested_field_paths", []):
        parts = path.split(".")
        cur = params
        for p in parts[:-1]:
            cur = cur.get(p) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, dict):
            leaf = parts[-1]
            v = cur.get(leaf)
            if isinstance(v, str) and v:
                cur[leaf] = r(v)

    new_ir = dict(ir)
    new_ir["params"] = params
    return new_ir


# Loaded once from the DB/seed (cached in catalog_store). Call reload_catalog()
# after editing the DB rows to pick up changes.
TEMPLATE_CATALOG: dict[str, dict] = _build_catalog()
TEMPLATE_NAMES = sorted(TEMPLATE_CATALOG.keys())
RULE_TYPE_TO_TEMPLATE: dict[str, str] = dict(get_rule_type_map())
CATALOG_VERSION = _catalog_version()


def reload_catalog():
    """Rebuild the in-memory catalog from the DB (after a catalog_store.reload())."""
    global TEMPLATE_CATALOG, TEMPLATE_NAMES, RULE_TYPE_TO_TEMPLATE, CATALOG_VERSION
    from contract_upload_services import catalog_store
    catalog_store.reload()
    TEMPLATE_CATALOG = _build_catalog()
    TEMPLATE_NAMES = sorted(TEMPLATE_CATALOG.keys())
    RULE_TYPE_TO_TEMPLATE = dict(get_rule_type_map())
    CATALOG_VERSION = _catalog_version()


def template_for_rule_type(rule_type: str | None) -> str | None:
    if not rule_type:
        return None
    return RULE_TYPE_TO_TEMPLATE.get(rule_type)


# =====================================================================
# IR validation
# =====================================================================

def field_refs(ir: dict) -> list[str]:
    """Every output-template field name an IR references."""
    tmpl = TEMPLATE_CATALOG.get(ir.get("template"))
    if not tmpl:
        return []
    try:
        return [f for f in tmpl["fields"](ir.get("params") or {}) if f]
    except Exception:
        return []


def validate_ir(ir: dict, field_names: set[str]) -> tuple[bool, str | None]:
    """Structural + field-existence check. Returns (ok, reason_if_not).

    `field_names` is the Output-Template canonical field set; every field the IR
    references must be in it. This is the mapping check that closes the
    hallucinated-column gap (no template-aware bypass).
    """
    if not isinstance(ir, dict):
        return False, "IR is not an object"

    template = ir.get("template")
    if not template:
        return False, "no template selected"
    spec = TEMPLATE_CATALOG.get(template)
    if not spec:
        return False, f"unknown template: {template!r}"

    params = ir.get("params")
    if not isinstance(params, dict):
        return False, "params missing or not an object"

    for req in spec["required"]:
        v = params.get(req)
        if v is None or (isinstance(v, (list, dict, str)) and len(v) == 0):
            return False, f"missing required param: {req}"

    # Field-existence gate (closes the template-aware bypass).
    refs = field_refs(ir)
    if not refs:
        return False, "IR references no output-template fields"
    missing = [f for f in refs if f not in field_names]
    if missing:
        return False, f"fields not in output template: {missing}"

    return True, None
