"""Contract margin (19.3) and policy totals (19.2) — applied at RUN time.

CONTRACT MARGIN. A carrier sets ONE margin per contract in Bordereau Setup
(`contract.extracted["settings"]["margin_pct"]`, a percent, 0 when unset). Every
rule that can take a tolerance — formula rules (cross_field_math) and number
limits (max_limit / min_limit / range_check, from the Rule Library, the AI or the
typed contract terms alike) — is measured with it, unless that one rule was given
a tolerance of its OWN on the setup screen. A rule "has its own" when its
rule_spec says `tolerance_source == "rule"` (the tolerance edit sets it), or — for
rules edited before the margin existed — when a `rule.tolerance_changed` audit
event names it. `tolerance_source == "contract"` (set by "Use contract margin")
always means "follow the contract", whatever the audit history says.

The settings are NESTED under "settings" because the top-level plain values of
`extracted` become output constants (direct_routes._contract_constants).

POLICY TOTALS. A money limit on an amount that adds up across a policy's
transaction rows (premium, commission amount, fees, taxes) is measured on each
multi-row policy's TOTAL — see rule_compiler._policy_total_limit. Only added when
the sheet has a policy-number column and some policy number repeats in the file.

Nothing here is written back: the stored rule keeps its compiled query, and the
run recompiles a copy in memory (`apply_rule_settings`, the same shape as
duckdb_validation._refresh_if_stale). Any problem keeps the stored query, so a
rule can never stop running because of this.
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Iterable

FORMULA_TEMPLATE = "cross_field_math"
LIMIT_TEMPLATES = ("max_limit", "min_limit", "range_check")
TOLERANCE_TEMPLATES = (FORMULA_TEMPLATE,) + LIMIT_TEMPLATES

# THE ONE SWITCH for the deliberate 19.3 change. Before it, a formula rule with
# no tolerance of its own ran with the SERVER default band
# (KAVACHIO_DEFAULT_TOLERANCE_PCT, 1%, see rule_compiler._b_cross_field_math).
# Since 19.3 it follows its contract's margin instead — 0 until the carrier sets
# one, i.e. exact to the cent (the compiler's one-cent floor still applies). Set
# this to False to switch back: formula rules without their own value then keep
# the server default band, and the contract margin reaches limit rules only.
FORMULA_RULES_FOLLOW_CONTRACT_MARGIN = True

MARGIN_MAX_PCT = 100.0


def _as_dict(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return {}
    return value if isinstance(value, dict) else {}


def contract_margin_pct(extracted: Any) -> float:
    """The contract's margin in percent — 0 when none was set or it is unreadable."""
    settings = _as_dict(extracted).get("settings")
    raw = settings.get("margin_pct") if isinstance(settings, dict) else None
    try:
        pct = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return pct if math.isfinite(pct) and pct >= 0 else 0.0


def rule_ir(spec: Any) -> dict | None:
    """The rule's IR — both shapes carry it whole under "ir": a Rule Library / AI
    rule (kind ir_v1) and a typed contract-term rule (source contract_terms)."""
    ir = _as_dict(spec).get("ir")
    return ir if isinstance(ir, dict) else None


def tolerance_template(spec: Any) -> str | None:
    """The rule's template when it can take a tolerance, else None."""
    ir = rule_ir(spec)
    t = ir.get("template") if ir else None
    return t if t in TOLERANCE_TEMPLATES else None


def server_default_pct() -> float:
    """The band a formula rule with no tolerance_pct was COMPILED with — the
    compiler's own fallback, read from the same env knob."""
    try:
        return float(os.getenv("KAVACHIO_DEFAULT_TOLERANCE_PCT", "1.0"))
    except ValueError:
        return 1.0


def compiled_tolerance(template: str, params: dict) -> float:
    """The band the STORED query was compiled with: the rule's tolerance_pct when
    it has one; absent means the server default on a formula rule, 0 on a limit."""
    raw = (params or {}).get("tolerance_pct")
    if raw in (None, ""):
        return server_default_pct() if template == FORMULA_TEMPLATE else 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def has_own_tolerance(spec: Any, rule_id: Any, legacy_ids: Iterable = ()) -> bool:
    src = _as_dict(spec).get("tolerance_source")
    if src == "rule":
        return True
    if src == "contract":
        return False
    try:
        return int(rule_id) in {int(i) for i in (legacy_ids or ())}
    except (TypeError, ValueError):
        return False


def effective_tolerance(spec: Any, margin_pct: float, own: bool) -> tuple[float, str]:
    """(percent, source) a rule is measured with: its own value ('rule'), else the
    contract margin ('contract'); 'server' only when the switch above is off.
    Only meaningful for a tolerance template (see tolerance_template)."""
    ir = rule_ir(spec) or {}
    template, params = ir.get("template"), ir.get("params") or {}
    if own:
        # An own edit that only ever set a reject band keeps the band its query
        # was compiled with.
        return compiled_tolerance(template, params), "rule"
    if template == FORMULA_TEMPLATE and not FORMULA_RULES_FOLLOW_CONTRACT_MARGIN:
        return compiled_tolerance(template, params), "server"
    return float(margin_pct or 0.0), "contract"


def rules_with_legacy_own_tolerance(session, rule_ids: Iterable) -> set[int]:
    """Rules whose tolerance a person edited BEFORE tolerance_source existed —
    one query for the whole run. Fail-open: no answer means none."""
    ids = sorted({int(i) for i in (rule_ids or ()) if i is not None})
    if not ids:
        return set()
    try:
        from sqlalchemy import text
        rows = session.execute(
            text("SELECT DISTINCT target FROM activity_events "
                 "WHERE action = 'rule.tolerance_changed' AND target = ANY(:t)"),
            {"t": [f"rule:{i}" for i in ids]}).fetchall()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        return set()
    out = set()
    for (target,) in rows:
        try:
            out.add(int(str(target).split(":", 1)[1]))
        except (IndexError, ValueError):
            continue
    return out


# ---- Policy totals: which money checks add up across a policy's rows -------

# Typed contract terms are decided by NAME: "minimum premium, per policy" is a
# total of the policy's premium; sum insured, TIV, deductible and the referral
# threshold are per-policy figures REPEATED on every transaction row, where a
# row-by-row check already gives the policy's answer (and a sum would inflate it).
_SUMMING_TERMS = {"min_premium"}

# Anything else by the column it reads: an amount (premium, commission or
# brokerage AMOUNT, fees, taxes, levies) adds up; a rate or a per-policy figure
# (limit, sum insured, TIV, deductible, excess, value) does not.
_SUMMING_FIELD = re.compile(
    r"premium|commission\s*amount|brokerage\s*amount|fee|tax|levy|levies|"
    r"\bipt\b|\bgwp\b", re.I)
_NOT_SUMMING_FIELD = re.compile(
    r"rate|%|pct|percent|limit|insured|tiv|deductible|excess|value", re.I)


def policy_total_eligible(spec: Any) -> bool:
    """True when this rule is a money limit on an amount that sums across a
    policy's transaction rows (19.2) — min_limit, max_limit, or a range with
    min != max. An exact-value range is a per-row figure and never qualifies."""
    s = _as_dict(spec)
    ir = rule_ir(s) or {}
    template, params = ir.get("template"), ir.get("params") or {}
    if template not in LIMIT_TEMPLATES:
        return False
    if template == "range_check":
        try:
            if float(params.get("min")) == float(params.get("max")):
                return False
        except (TypeError, ValueError):
            return False
    if s.get("source") == "contract_terms":
        return s.get("limit") in _SUMMING_TERMS
    field = str(params.get("field") or "")
    return bool(_SUMMING_FIELD.search(field)) and not _NOT_SUMMING_FIELD.search(field)


def multi_row_policy_columns(con, tables: dict, policy_columns: dict) -> dict:
    """{sheet: policy column} for the sheets of THIS file where some policy number
    is on more than one row. A column the setup leaves unfilled never qualifies
    (a rule reading it would be reported "not checked")."""
    out = {}
    by_key = {str(k).strip(): v for k, v in (policy_columns or {}).items() if v}
    for sheet, meta in (tables or {}).items():
        col = by_key.get(str(sheet).strip())
        if not col or col not in (meta.get("columns") or []):
            continue
        filled = meta.get("filled")
        if filled is not None and col not in filled:
            continue
        qc = '"' + col.replace('"', '""') + '"'
        qs = '"' + str(sheet).replace('"', '""') + '"'
        try:
            hit = con.execute(
                f"SELECT 1 FROM {qs} WHERE {qc} IS NOT NULL AND TRIM({qc}) <> '' "
                f"GROUP BY TRIM({qc}) HAVING COUNT(*) > 1 LIMIT 1").fetchone()
        except Exception:
            continue
        if hit:
            out[sheet] = col
    return out


# ---- The run-time step -------------------------------------------------------

def _identifiers_by_sheet(sql: str) -> dict:
    """{sheet: quoted identifiers its UNION arms read} — per sheet, because an
    aliased sheet spells the same column differently from the others."""
    out: dict = {}
    for arm in (sql or "").split("\nUNION ALL\n"):
        ids = set(re.findall(r'"([^"]+)"', arm))
        for sheet in set(re.findall(r'FROM\s+"([^"]+)"', arm)):
            out.setdefault(sheet, set()).update(ids)
    return out


def _recompiled(ir: dict, stored_sql: str, allowed_new: set) -> str | None:
    """The IR recompiled in memory onto the sheets the stored query names, or None
    when that is not safe: any error, fewer sheets than before, or a column the
    stored query never read (an aliased sheet the plain recompile would spell
    wrong) other than the policy key it was asked to group by."""
    try:
        from contract_upload_services.rule_compiler import (
            recompile_ir_for_sheets, sheets_in_sql)
        before = sheets_in_sql(stored_sql)
        if not before:
            return None
        sql = recompile_ir_for_sheets(ir, before)
        if len(sheets_in_sql(sql)) < len(before):
            return None
        was = _identifiers_by_sheet(stored_sql)
        for sheet, ids in _identifiers_by_sheet(sql).items():
            if ids - was.get(sheet, set()) - set(allowed_new):
                return None
        return sql
    except Exception:
        return None


def with_rule_settings(rule: dict, *, margins: dict | None, own_ids: set,
                       policy_keys: dict) -> dict:
    """`rule` itself when nothing changes, else a COPY whose rule_spec carries the
    effective tolerance / policy total in its IR and the query recompiled from it."""
    spec = _as_dict(rule.get("rule_spec"))
    ir = rule_ir(spec)
    stored = spec.get("compiled_sql")
    if not ir or not stored:
        return rule
    template = ir.get("template")
    params = dict(ir.get("params") or {})
    changed = False
    if margins is not None and template in TOLERANCE_TEMPLATES:
        try:
            cid = int(rule.get("contract_id"))
        except (TypeError, ValueError):
            cid = None
        margin = float((margins or {}).get(cid) or 0.0)
        own = has_own_tolerance(spec, rule.get("rule_id"), own_ids)
        eff, _src = effective_tolerance(spec, margin, own)
        if abs(eff - compiled_tolerance(template, params)) > 1e-12:
            params["tolerance_pct"] = eff
            changed = True
    keys = {}
    if policy_keys and policy_total_eligible(spec):
        from contract_upload_services.rule_compiler import sheets_in_sql
        keys = {sh: col for sh, col in policy_keys.items()
                if sh in sheets_in_sql(stored)}
        if keys:
            params["policy_total"] = {"keys": keys}
            changed = True
    if not changed:
        return rule
    new_ir = {**ir, "params": params}
    sql = _recompiled(new_ir, stored, set(keys.values()))
    if not sql:
        return rule
    new_spec = {**spec, "ir": new_ir, "compiled_sql": sql}
    if isinstance(spec.get("params"), dict):     # contract-term rules mirror it
        new_spec["params"] = params
    return {**rule, "rule_spec": new_spec}


def apply_rule_settings(con, tables: dict, rules: list, settings: dict) -> list:
    """The run's rules with the contract margin and policy totals applied in
    memory. `settings`: {"margins": {contract_id: pct} | None,
    "own_tolerance_rule_ids": [...], "policy_columns": {sheet: column}}."""
    margins = settings.get("margins")
    own_ids = {int(i) for i in (settings.get("own_tolerance_rule_ids") or ())}
    policy_keys = multi_row_policy_columns(con, tables,
                                           settings.get("policy_columns") or {})
    out = []
    for rule in rules:
        try:
            out.append(with_rule_settings(rule, margins=margins, own_ids=own_ids,
                                          policy_keys=policy_keys))
        except Exception:
            out.append(rule)
    return out


def policy_total_marker(rule: dict, sheet: Any) -> dict | None:
    """The `aggregate` marker for a policy-total exception of this rule on
    `sheet` ({level:'group', function:'sum', group_by:[key]}), or None when the
    rule was not measured on policy totals."""
    ir = rule_ir(rule.get("rule_spec")) or {}
    pt = (ir.get("params") or {}).get("policy_total")
    if not isinstance(pt, dict):
        return None
    keys = pt.get("keys") if isinstance(pt.get("keys"), dict) else {}
    key = keys.get(sheet) or keys.get(str(sheet or "").strip()) or pt.get("key")
    return {"level": "group", "function": "sum",
            "group_by": [key] if key else []}
